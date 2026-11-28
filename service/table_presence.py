"""Expiring, principal-bound audio presence for both public table transports."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any

from .db import AdvisoryLock, PgTransaction
from .table_stream_events import SESSION_WAKEUP_CHANNEL
from .workbench_contracts import (
    TablePresenceClose,
    TablePresenceOpen,
    TablePresenceRegistration,
    TablePresenceReport,
    TablePresenceRequest,
)

REGISTRATION_TTL = timedelta(seconds=45)
# With the five-second degraded-mode poll the GM drops a vanished page within 45s.
VISIBLE_TTL_S = 40


class PresenceInactive(Exception):
    pass


class PresenceFull(Exception):
    pass


class PresenceConflict(Exception):
    pass


def context_for(
    campaign_id: str, session_id: str, generation: int, *, account_id: int | None, grant_id: str | None,
    audio_generation: int = 0, participant_id: str | None = None,
) -> str:
    """A correlation value for this exact authorized picture, never a credential."""
    if (account_id is None) == (grant_id is None):
        raise ValueError("one table principal is required")
    who = f"account/{account_id}/{participant_id or 'owner'}" if account_id is not None else f"screen/{grant_id}"
    binding = f"audio-presence/{campaign_id}/{session_id}/{generation}/{audio_generation}/{who}"
    return hashlib.sha256(binding.encode()).hexdigest()


_STANDING = """
    SELECT s.id, s.link_generation, s.audio_presence_generation, p.id FROM campaign.table_sessions s
    JOIN campaign.campaigns c ON c.id = s.campaign_id AND c.archived_at IS NULL
    LEFT JOIN campaign.participants p ON p.campaign_id = c.id AND p.user_id = %(account)s AND p.user_id <> c.owner_id
      AND p.accepted_at IS NOT NULL AND p.removed_at IS NULL
    WHERE c.id = %(campaign)s AND s.state = 'live' AND s.expires_at > %(now)s AND s.table_audio
      AND (c.owner_id = %(account)s OR p.id IS NOT NULL
           OR EXISTS (SELECT 1 FROM campaign.table_credentials g WHERE g.id = %(grant)s AND g.session_id = s.id
                      AND g.link_generation = s.link_generation AND g.revoked_at IS NULL))
    FOR SHARE OF s
"""
_MINE = """session_id = %(session)s AND generation = %(gen)s
    AND participant_id IS NOT DISTINCT FROM %(seat)s
    AND account_id IS NOT DISTINCT FROM %(account)s AND grant_id IS NOT DISTINCT FROM %(grant)s"""


def apply(
    transaction: PgTransaction, campaign_id: str, *, account_id: int | None, grant_id: str | None,
    body: TablePresenceRequest, now: datetime,
) -> TablePresenceRegistration | None:
    if (account_id is None) == (grant_id is None):
        raise PresenceInactive
    conn = transaction.conn
    params: dict[str, Any] = {"campaign": campaign_id, "account": account_id, "grant": grant_id, "now": now,
                              "cutoff": now - REGISTRATION_TTL}
    transaction.note_row_lock()
    conn.execute("SELECT set_config('transaction_timeout', %s, true)", (transaction.transaction_bound(None),))
    standing = conn.execute(_STANDING, params).fetchone()
    if standing is None:
        raise PresenceInactive
    session, generation, audio_generation, seat = standing
    params.update(session=session, gen=generation, seat=seat)
    if isinstance(body, TablePresenceOpen):
        if not secrets.compare_digest(body.context, context_for(
            campaign_id, session, generation, account_id=account_id, grant_id=grant_id,
            audio_generation=audio_generation, participant_id=seat,
        )):
            raise PresenceInactive
        conn.execute("SELECT pg_advisory_xact_lock(%s, 0)", (int(AdvisoryLock.TABLE_AUDIO_PRESENCE),))
        # Old entries are reclaimed even if a page never manages a final POST.
        conn.execute("""
            DELETE FROM campaign.audio_presence r WHERE r.last_seen_at <= %(cutoff)s
              OR EXISTS (SELECT 1 FROM campaign.participants p WHERE p.id = r.participant_id
                         AND (p.removed_at IS NOT NULL OR p.accepted_at IS NULL OR p.user_id <> r.account_id))
              OR EXISTS (SELECT 1 FROM campaign.table_credentials g
                         WHERE g.id = r.grant_id AND g.revoked_at IS NOT NULL)
              OR EXISTS (SELECT 1 FROM campaign.table_sessions s WHERE s.id = r.session_id AND s.expires_at <= %(now)s)
        """, params)
        params["page"] = body.page_id
        prior = conn.execute(f"SELECT registration FROM campaign.audio_presence WHERE {_MINE} AND page_id = %(page)s",
                             params).fetchone()
        if prior is not None:
            return TablePresenceRegistration(schema_version=1, registration=prior[0])
        total, mine = conn.execute(
            "SELECT count(*) FILTER (WHERE session_id = %(session)s), "
            "count(*) FILTER (WHERE account_id = %(account)s OR grant_id = %(grant)s) FROM campaign.audio_presence",
            params,
        ).fetchone()
        if total >= 12 or mine >= 3:
            raise PresenceFull
        registration = secrets.token_urlsafe(24)
        added = conn.execute("""
            INSERT INTO campaign.audio_presence(permit, registration, page_id, session_id, generation,
                                                account_id, participant_id, grant_id, last_seen_at)
            SELECT n, %(registration)s, %(page)s, %(session)s, %(gen)s, %(account)s, %(seat)s, %(grant)s, %(now)s
            FROM generate_series(1,16) n WHERE NOT EXISTS (SELECT 1 FROM campaign.audio_presence WHERE permit = n)
            ORDER BY n LIMIT 1 RETURNING permit
        """, params | {"registration": registration}).fetchone()
        if added is None:
            raise PresenceFull
        return TablePresenceRegistration(schema_version=1, registration=registration)

    params["registration"] = body.registration
    row = conn.execute(
        f"SELECT revision, state, playing_handles FROM campaign.audio_presence WHERE {_MINE} "
        "AND registration = %(registration)s AND last_seen_at > %(cutoff)s FOR NO KEY UPDATE", params,
    ).fetchone()
    if row is None:
        raise PresenceInactive
    if isinstance(body, TablePresenceClose):
        conn.execute("DELETE FROM campaign.audio_presence WHERE registration = %(registration)s", params)
        transaction.notify(SESSION_WAKEUP_CHANNEL, session)
        return None
    assert isinstance(body, TablePresenceReport)
    revision, state, handles = row
    canonical_handles = sorted(body.playing_handles)
    if body.revision < revision:
        return None
    changed = (body.state, canonical_handles) != (state, handles)
    if body.revision == revision and changed:
        raise PresenceConflict
    params.update(revision=body.revision, state=body.state, handles=canonical_handles)
    conn.execute(
        "UPDATE campaign.audio_presence SET revision = %(revision)s, state = %(state)s, playing_handles = %(handles)s, "
        "last_seen_at = GREATEST(last_seen_at, %(now)s) WHERE registration = %(registration)s", params,
    )
    if changed:
        transaction.notify(SESSION_WAKEUP_CHANNEL, session)
    return None

"""Resolve current campaign standing from server-authenticated identities."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Final, Protocol

import psycopg

from . import campaign_identity as ident
from .campaign_store import Campaign, MissingParent, Staging, fake, pg, shared_rows
from .db import InMemoryDatabase, TransactionalDatabase, UnitOfWork
from .eligibility_store import _GroupRow, _MemberRow
from .participant_store import Participant
from .policy import (
    GmFacts,
    GmPrincipal,
    GrantFacts,
    SeatFacts,
    SessionFacts,
    TableFacts,
    TableOutcome,
    TableRefusal,
    decide_gm,
    decide_table,
)
from .policy_facts import PolicyDocumentReader, PolicyUnavailable
from .session import SessionData
from .table_session_store import LiveScreen, ScreenGrant, TableSession

_log = logging.getLogger(__name__)


class PrincipalFactsStore(Protocol):
    def table_facts(
        self, unit: UnitOfWork, campaign_id: str, *, user_id: int | None, grant_id: str | None
    ) -> TableFacts: ...

    def gm_facts(self, unit: UnitOfWork, campaign_id: str, *, user_id: int) -> GmFacts | None: ...


_TABLE_SQL: Final = """
WITH requested AS (
    SELECT id, owner_id FROM campaign.campaigns WHERE id = %(campaign)s
), revisions AS (
    SELECT a.authz_revision, a.projection_revision
    FROM campaign.authz_state a JOIN requested c ON c.id = a.campaign_id
), sessions AS (
    SELECT s.id, s.campaign_id, s.state, s.expires_at, s.link_generation
    FROM campaign.table_sessions s JOIN requested c ON c.id = s.campaign_id
    WHERE s.state = 'live'
), seats AS (
    SELECT p.id, p.accepted_at IS NOT NULL AS accepted, p.confirmed_at IS NOT NULL AS confirmed,
           p.removed_at IS NOT NULL AS removed
    FROM campaign.participants p JOIN requested c ON c.id = p.campaign_id
    WHERE p.user_id = %(user)s
), sheets AS (
    SELECT p.id AS participant_id, d.id AS document_id FROM seats p
    JOIN campaign.documents d ON d.linked_participant_id = p.id
    WHERE d.campaign_id = %(campaign)s
), memberships AS (
    SELECT p.id AS participant_id, g.id AS group_id FROM seats p
    JOIN campaign.group_members m ON m.participant_id = p.id AND m.campaign_id = %(campaign)s
    JOIN campaign.groups g ON g.id = m.group_id AND g.campaign_id = m.campaign_id
    WHERE g.removed_at IS NULL AND NOT p.removed
), screen AS (
    SELECT json_build_array(g.id, g.link_generation, g.revoked_at IS NOT NULL,
                            json_build_array(s.id, s.campaign_id, s.state, s.expires_at, s.link_generation)) AS facts
    FROM campaign.table_credentials g JOIN campaign.table_sessions s ON s.id = g.session_id
    WHERE g.id = %(grant)s
)
SELECT (SELECT owner_id FROM requested),
       (SELECT ARRAY[authz_revision, projection_revision] FROM revisions),
       COALESCE((SELECT json_agg(json_build_array(id, campaign_id, state, expires_at, link_generation))
                 FROM sessions), '[]'),
       COALESCE((SELECT json_agg(json_build_array(id, accepted, confirmed, removed)) FROM seats), '[]'),
       (SELECT facts FROM screen),
       COALESCE((SELECT json_agg(json_build_array(participant_id, document_id)) FROM sheets), '[]'),
       COALESCE((SELECT json_agg(json_build_array(participant_id, group_id)) FROM memberships), '[]')
"""
# The verified-account conjuncts from yje.2.1 will join the account/seat read.

_GM_SQL: Final = """
SELECT c.owner_id, c.archived_at IS NOT NULL, a.authz_revision, a.projection_revision
FROM campaign.campaigns c JOIN campaign.authz_state a ON a.campaign_id = c.id
WHERE c.id = %s AND c.owner_id = %s
"""


def _session(row: tuple[str, str, str, str, int]) -> SessionFacts:
    session_id, campaign_id, state, expires_at, generation = row
    return SessionFacts(session_id, campaign_id, state, datetime.fromisoformat(expires_at), generation)


def _memory_session(row: TableSession) -> SessionFacts:
    return SessionFacts(row.id, row.campaign_id, row.state, row.expires_at, row.link_generation)


def _by_participant(rows: Iterable[tuple[str, str]]) -> dict[str, frozenset[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for participant, related_id in rows:
        grouped[participant].add(related_id)
    return {participant: frozenset(ids) for participant, ids in grouped.items()}


class PostgresPrincipalFacts:
    def table_facts(
        self, unit: UnitOfWork, campaign_id: str, *, user_id: int | None, grant_id: str | None
    ) -> TableFacts:
        owner, revisions, sessions, seats, grant, sheets, groups = pg(unit).conn.execute(
            _TABLE_SQL, {"campaign": campaign_id, "user": user_id, "grant": grant_id}
        ).fetchone()
        return TableFacts(
            campaign_id=campaign_id,
            owner_id=owner,
            live_sessions=tuple(_session(row) for row in sessions),
            seats=tuple(SeatFacts(*row) for row in seats),
            grant=None if grant is None else GrantFacts(grant[0], grant[1], grant[2], _session(grant[3])),
            sheets=_by_participant(sheets),
            groups=_by_participant(groups),
            revisions=None if revisions is None else (revisions[0], revisions[1]),
        )

    def gm_facts(self, unit: UnitOfWork, campaign_id: str, *, user_id: int) -> GmFacts | None:
        row = pg(unit).conn.execute(_GM_SQL, (campaign_id, user_id)).fetchone()
        return None if row is None else GmFacts(*row)


class InMemoryPrincipalFacts:
    def __init__(self, db: InMemoryDatabase, *, documents: PolicyDocumentReader) -> None:
        self._documents = documents
        self._document_ids: Staging[object] = shared_rows(db, "documents")
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._sessions: Staging[TableSession] = shared_rows(db, "table_sessions")
        self._grants: Staging[ScreenGrant] = shared_rows(db, "table_credentials")
        self._participants: Staging[Participant] = shared_rows(db, "participants")
        self._groups: Staging[_GroupRow] = shared_rows(db, "groups")
        self._members: Staging[_MemberRow] = shared_rows(db, "group_members")

    def table_facts(
        self, unit: UnitOfWork, campaign_id: str, *, user_id: int | None, grant_id: str | None
    ) -> TableFacts:
        twin = fake(unit)
        campaign = self._campaigns.visible(twin).get(campaign_id)
        sessions = self._sessions.visible(twin)
        seats = {
            p.id: p for p in self._participants.visible(twin).values()
            if user_id is not None and p.user_id == user_id and p.campaign_id == campaign_id
        }
        grant = None if grant_id is None else self._grants.visible(twin).get(grant_id)
        grant_session = None if grant is None else sessions.get(grant.session_id)
        authz, projection = twin.authz_revision(campaign_id), twin.projection_revision(campaign_id)
        live_groups = {
            g.id for row in self._groups.visible(twin).values()
            if (g := row.group).campaign_id == campaign_id and g.is_live
        }
        sheets = _by_participant(
            (participant, doc_id)
            for doc_id in self._document_ids.visible(twin)
            if (doc := self._documents.get(unit, campaign_id, doc_id)) is not None
            and (participant := doc.linked_participant_id) is not None
            and participant in seats
        )
        groups = _by_participant(
            (member.participant_id, member.group_id)
            for member in self._members.visible(twin).values()
            if not member.gone and member.campaign_id == campaign_id and member.group_id in live_groups
            and (seat := seats.get(member.participant_id)) is not None and seat.is_active
        )
        return TableFacts(
            campaign_id=campaign_id,
            owner_id=None if campaign is None else campaign.owner_id,
            live_sessions=tuple(
                _memory_session(s) for s in sessions.values() if s.campaign_id == campaign_id and s.is_live
            ),
            seats=tuple(
                SeatFacts(p.id, p.accepted_at is not None, p.confirmed_at is not None, not p.is_active)
                for p in seats.values()
            ),
            grant=None if grant is None or grant_session is None else GrantFacts(
                grant.id, grant.link_generation, grant.revoked_at is not None, _memory_session(grant_session)
            ),
            sheets=sheets,
            groups=groups,
            revisions=None if authz is None or projection is None else (authz, projection),
        )

    def gm_facts(self, unit: UnitOfWork, campaign_id: str, *, user_id: int) -> GmFacts | None:
        twin = fake(unit)
        campaign = self._campaigns.visible(twin).get(campaign_id)
        if campaign is None or campaign.owner_id != user_id:
            return None
        authz, projection = twin.authz_revision(campaign_id), twin.projection_revision(campaign_id)
        return None if authz is None or projection is None else GmFacts(
            campaign.owner_id, campaign.is_archived, authz, projection
        )


class PrincipalResolver:
    """Re-read standing per call. A content read rechecks access in its own query."""

    def __init__(
        self, db: TransactionalDatabase, facts: PrincipalFactsStore, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._db = db
        self._facts = facts
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    def table(
        self, campaign_id: str, *, account: SessionData | None, screen: LiveScreen | None, now: datetime | None = None
    ) -> TableOutcome:
        if account is None and screen is None:
            return TableRefusal.UNAUTHENTICATED
        if (account is not None and not isinstance(account, SessionData)) or (
            screen is not None and not isinstance(screen, LiveScreen)
        ):
            raise TypeError("table standing requires authenticated credentials")
        if not isinstance(campaign_id, str) or not ident.is_id(ident.CAMPAIGN, campaign_id):
            return TableRefusal.INACTIVE
        user_id = None if account is None else account.user_id
        grant_id = None if screen is None else screen.grant_id
        try:
            with self._db.transaction() as unit:
                facts = self._facts.table_facts(unit, campaign_id, user_id=user_id, grant_id=grant_id)
                standing = decide_table(
                    facts, account_id=user_id, grant_id=grant_id, now=self._clock() if now is None else now
                )
        except psycopg.OperationalError as exc:
            _log.warning("principal_database_unavailable sqlstate=%s", exc.sqlstate or "none")
        else:
            return standing
        raise PolicyUnavailable()

    def gm(self, campaign_id: str, *, account: SessionData) -> GmPrincipal:
        if not isinstance(account, SessionData):
            raise TypeError("GM standing requires an authenticated account")
        if not isinstance(campaign_id, str) or not ident.is_id(ident.CAMPAIGN, campaign_id):
            raise MissingParent()
        user_id = account.user_id
        try:
            with self._db.transaction() as unit:
                facts = self._facts.gm_facts(unit, campaign_id, user_id=user_id)
            return decide_gm(facts, campaign_id=campaign_id, account_id=user_id)
        except psycopg.OperationalError as exc:
            _log.warning("principal_database_unavailable sqlstate=%s", exc.sqlstate or "none")
        raise PolicyUnavailable()

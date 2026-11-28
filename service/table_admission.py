"""Bound open table streams across processes using database leases."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from .campaign_store import aware
from .db import AdvisoryLock, Database, advisory_key
from .table_reads import TableStreamWatch

LEASE_EXPIRY: Final = timedelta(seconds=45)
LEASE_HEARTBEAT_S: Final = 15.0
TABLE_STREAM_LIMIT: Final = 16
_SESSION_LIMIT: Final = 12
_PRINCIPAL_LIMIT: Final = 3


class TableFull(Exception):
    """The table, account or screen has reached its stream limit."""


@dataclass(frozen=True)
class StreamLease:
    id: UUID = field(repr=False)
    watch: TableStreamWatch
    audio_enabled: bool = False


_STANDING_SQL: Final = """
SELECT s.id, s.link_generation, s.table_audio
FROM campaign.table_sessions s JOIN campaign.campaigns c ON c.id = s.campaign_id
LEFT JOIN campaign.participants p ON p.campaign_id = s.campaign_id AND p.user_id = %(account)s
    AND p.accepted_at IS NOT NULL AND p.removed_at IS NULL
LEFT JOIN campaign.table_credentials g ON g.id = %(grant)s AND g.session_id = s.id
    AND g.link_generation = s.link_generation AND g.revoked_at IS NULL
WHERE s.campaign_id = %(campaign)s AND s.state = 'live' AND s.expires_at > %(now)s
    AND (c.owner_id = %(account)s OR p.id IS NOT NULL OR g.id IS NOT NULL)
"""

_COUNT_SQL: Final = """
SELECT count(*), count(*) FILTER (WHERE account_id = %(account)s OR grant_id = %(grant)s)
FROM campaign.table_stream_connections
WHERE session_id = %(session)s AND last_seen_at > %(cutoff)s
"""

_INSERT_SQL: Final = """
INSERT INTO campaign.table_stream_connections
    (permit, lease_id, instance_id, session_id, account_id, grant_id, opened_at, last_seen_at)
SELECT candidate, %(lease)s, %(instance)s, %(session)s, %(account)s, %(grant)s, %(now)s, %(now)s
FROM generate_series(1, %(limit)s) candidate
WHERE NOT EXISTS (SELECT 1 FROM campaign.table_stream_connections c
                  WHERE c.permit = candidate AND c.last_seen_at > %(cutoff)s)
ORDER BY candidate LIMIT 1
ON CONFLICT (permit) DO UPDATE SET
    lease_id = EXCLUDED.lease_id,
    instance_id = EXCLUDED.instance_id,
    session_id = EXCLUDED.session_id,
    account_id = EXCLUDED.account_id,
    grant_id = EXCLUDED.grant_id,
    opened_at = EXCLUDED.opened_at,
    last_seen_at = EXCLUDED.last_seen_at
WHERE campaign.table_stream_connections.last_seen_at <= %(cutoff)s
RETURNING permit
"""


class TableAdmission:
    """The per-session lock protects local counts.

    Database permit uniqueness independently protects the service limit even
    when streams open at different tables at the same time.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._instance = uuid4()

    async def open(
        self, campaign_id: str, *, account_id: int | None, grant_id: str | None, now: datetime
    ) -> StreamLease | None:
        if (account_id is None) == (grant_id is None):
            raise ValueError("admission requires one table principal")
        now = aware(now, "a clock")
        params = {"campaign": campaign_id, "account": account_id, "grant": grant_id, "now": now}
        async with self._db.async_connection() as conn, conn.transaction():
            initial = await (await conn.execute(_STANDING_SQL, params)).fetchone()
            if initial is None:
                return None
            session_id = initial[0]
            await conn.execute("SELECT set_config('lock_timeout', '1000ms', true)")
            await conn.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)", (int(AdvisoryLock.TABLE_SESSION), advisory_key(session_id))
            )
            standing = await (await conn.execute(_STANDING_SQL, params)).fetchone()
            if standing is None or standing[0] != session_id:
                return None
            watch = TableStreamWatch(campaign_id, session_id, standing[1], account_id=account_id, grant_id=grant_id)
            lease = StreamLease(uuid4(), watch, audio_enabled=standing[2])
            capacity = params | {
                "session": session_id, "cutoff": now - LEASE_EXPIRY, "limit": TABLE_STREAM_LIMIT,
                "lease": lease.id, "instance": self._instance,
            }
            counts = await (await conn.execute(_COUNT_SQL, capacity)).fetchone()
            assert counts is not None
            total, principal = counts
            if total >= _SESSION_LIMIT or principal >= _PRINCIPAL_LIMIT:
                raise TableFull()
            for _ in range(TABLE_STREAM_LIMIT):
                permit = await (await conn.execute(_INSERT_SQL, capacity)).fetchone()
                if permit is not None:
                    return lease
                # At READ COMMITTED a different table may have taken the same
                # permit since our last snapshot. Try the next visible permit.
                full = await (await conn.execute(
                    "SELECT count(*) FROM campaign.table_stream_connections WHERE last_seen_at > %s",
                    (capacity["cutoff"],),
                )).fetchone()
                if full is not None and full[0] >= TABLE_STREAM_LIMIT:
                    break
            raise TableFull()

    async def renew(self, lease: StreamLease, *, now: datetime) -> bool:
        now = aware(now, "a clock")
        async with self._db.async_connection() as conn, conn.transaction():
            row = await (await conn.execute(
                "UPDATE campaign.table_stream_connections SET last_seen_at = GREATEST(last_seen_at, %s) "
                "WHERE lease_id = %s AND instance_id = %s AND last_seen_at > %s RETURNING permit",
                (now, lease.id, self._instance, now - LEASE_EXPIRY),
            )).fetchone()
        return row is not None

    async def release(self, lease: StreamLease) -> None:
        async with self._db.async_connection() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM campaign.table_stream_connections WHERE lease_id = %s AND instance_id = %s",
                (lease.id, self._instance),
            )

"""Distributed leases for the four reserved GM stream places."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from .db import AdvisoryLock, Database
from .gm_stream_reads import GM_OWNER_JOIN, GM_STREAM_LIMIT, GmWatch
from .table_admission import LEASE_EXPIRY, TableFull

_OWNER = ("SELECT c.id FROM (VALUES (%(campaign)s::text, %(account)s::bigint)) w(campaign, account) "
          + GM_OWNER_JOIN)
_PER_ACCOUNT_LIMIT = 3


@dataclass(frozen=True)
class GmLease:
    id: UUID = field(repr=False)
    watch: GmWatch


class GmAdmission:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._instance = uuid4()

    async def open(self, campaign_id: str, *, account_id: int, now: datetime) -> GmLease | None:
        lease = GmLease(uuid4(), GmWatch(campaign_id, account_id))
        params = {"campaign": campaign_id, "account": account_id, "lease": lease.id, "instance": self._instance,
                  "now": now, "old": now - LEASE_EXPIRY, "limit": GM_STREAM_LIMIT}
        async with self._db.async_connection() as conn, conn.transaction():
            if await (await conn.execute(_OWNER, params)).fetchone() is None:
                return None
            await conn.execute("SELECT set_config('lock_timeout', '1000ms', true)")
            await conn.execute("SELECT pg_advisory_xact_lock(%s, 0)", (int(AdvisoryLock.GM_STREAM),))
            if await (await conn.execute(_OWNER, params)).fetchone() is None:
                return None
            count = await (await conn.execute(
                "SELECT count(*) FROM campaign.gm_stream_connections WHERE account_id = %(account)s "
                "AND last_seen_at > %(old)s", params,
            )).fetchone()
            if count is not None and count[0] >= _PER_ACCOUNT_LIMIT:
                raise TableFull()
            permit = await (await conn.execute("""
                INSERT INTO campaign.gm_stream_connections
                  (permit, lease_id, instance_id, campaign_id, account_id, opened_at, last_seen_at)
                SELECT candidate, %(lease)s, %(instance)s, %(campaign)s, %(account)s, %(now)s, %(now)s
                FROM generate_series(1, %(limit)s) candidate
                WHERE NOT EXISTS (SELECT 1 FROM campaign.gm_stream_connections g
                                  WHERE g.permit = candidate AND g.last_seen_at > %(old)s)
                ORDER BY candidate LIMIT 1
                ON CONFLICT (permit) DO UPDATE SET lease_id = EXCLUDED.lease_id, instance_id = EXCLUDED.instance_id,
                    campaign_id = EXCLUDED.campaign_id, account_id = EXCLUDED.account_id,
                    opened_at = EXCLUDED.opened_at, last_seen_at = EXCLUDED.last_seen_at
                WHERE campaign.gm_stream_connections.last_seen_at <= %(old)s RETURNING permit
            """, params)).fetchone()
            if permit is None:
                raise TableFull()
        return lease

    async def renew(self, lease: GmLease, *, now: datetime) -> bool:
        async with self._db.async_connection() as conn, conn.transaction():
            updated = await (await conn.execute(
                "UPDATE campaign.gm_stream_connections SET last_seen_at = GREATEST(last_seen_at, %s) "
                "WHERE lease_id = %s AND instance_id = %s AND last_seen_at > %s RETURNING permit",
                (now, lease.id, self._instance, now - LEASE_EXPIRY),
            )).fetchone()
            return updated is not None

    async def release(self, lease: GmLease) -> None:
        async with self._db.async_connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM campaign.gm_stream_connections WHERE lease_id = %s AND instance_id = %s",
                               (lease.id, self._instance))

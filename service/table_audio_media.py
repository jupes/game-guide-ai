"""Resolve a live audio slot by its public handle and serve its current principal's bytes."""

from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from . import media_serving
from .asset_store import ReadyObject
from .campaign_store import pg
from .db import TransactionalDatabase
from .media_objects import ObjectMissing, ObjectStore, ObjectStoreError, StoreBusy

_RESOLVE = """
    SELECT a.object_key, a.media_type, a.size_bytes
    FROM campaign.audio_slots sl
    JOIN campaign.table_sessions s ON s.id = sl.session_id AND s.state = 'live' AND s.table_audio
        AND s.expires_at > %(now)s
    JOIN campaign.campaigns c ON c.id = s.campaign_id AND c.archived_at IS NULL
    JOIN campaign.audio_cues q ON q.id = sl.cue_id AND q.campaign_id = c.id AND q.kind = sl.slot AND NOT q.archived
    JOIN campaign.assets a ON a.id = q.asset_id AND a.campaign_id = c.id AND a.kind = 'audio' AND a.state = 'ready'
    WHERE sl.handle = %(handle)s AND (sl.loop OR sl.started_at + sl.duration_ms * interval '1 millisecond' > %(now)s)
      AND (c.owner_id = %(account)s OR EXISTS (
           SELECT 1 FROM campaign.participants p WHERE p.campaign_id = c.id AND p.user_id = %(account)s
             AND p.accepted_at IS NOT NULL AND p.removed_at IS NULL
      ) OR EXISTS (
           SELECT 1 FROM campaign.table_credentials g WHERE g.id = %(grant)s AND g.session_id = s.id
             AND g.link_generation = s.link_generation AND g.revoked_at IS NULL
      ))
"""


class TableMediaBusy(Exception):
    retry_after_s = media_serving.RETRY_AFTER_S


class TableMediaUnavailable(Exception):
    pass


class TableAudioMedia:
    def __init__(self, db: TransactionalDatabase, objects: ObjectStore, *, clock: Callable[[], datetime] | None = None):
        self._db = db
        self._objects = objects
        self._clock = clock or (lambda: datetime.now(UTC))

    def resolve(
        self, handle: str, *, account_id: int | None, grant_id: str | None, now: datetime,
    ) -> ReadyObject | None:
        if (account_id is None) == (grant_id is None):
            raise ValueError("audio bytes require one table principal")
        with self._db.transaction() as unit:
            row = pg(unit).conn.execute(_RESOLVE, {
                "handle": handle, "account": account_id, "grant": grant_id, "now": now,
            }).fetchone()
        return ReadyObject(*row) if row is not None else None

    async def read(
        self, handle: str, *, account_id: int | None, grant_id: str | None, expires_at: datetime | None,
        range_header: str | None, if_range: str | None,
    ) -> Response | None:
        async def current() -> ReadyObject | None:
            now = self._clock()
            if expires_at is not None and now >= expires_at:
                return None
            return await run_in_threadpool(self.resolve, handle, account_id=account_id, grant_id=grant_id, now=now)

        try:
            ready = await current()
            if ready is None:
                return None

            async def still_live() -> bool:
                try:
                    return await current() == ready
                except psycopg.Error:
                    return False

            try:
                window = media_serving.byte_window(range_header, if_range, ready.size_bytes)
            except media_serving.RangeNotSatisfiable:
                return Response(status_code=416, headers=media_serving.unsatisfiable_headers(ready.size_bytes))
            return await media_serving.serve(
                self._objects, key=ready.object_key, media_type=ready.media_type, size=ready.size_bytes,
                window=window, recheck=still_live,
            )
        except ObjectMissing:
            return None
        except StoreBusy:
            raise TableMediaBusy from None
        except (psycopg.Error, ObjectStoreError):
            raise TableMediaUnavailable from None

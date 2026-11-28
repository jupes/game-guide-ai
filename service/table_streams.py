"""Compose a table stream with its short-lived distributed admission lease."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, aclosing
from datetime import UTC, datetime
from functools import partial

import anyio
import psycopg

from .db import Database, default_dsn
from .gm_stream_reads import GM_STREAM_LIMIT
from .table_admission import LEASE_HEARTBEAT_S, TABLE_STREAM_LIMIT, StreamLease, TableAdmission
from .table_fanout import RECONNECT_FRAME, TableFanout, listen_table_wakeups
from .table_reads import TableStreamReads

_log = logging.getLogger(__name__)


class TableStreams:
    def __init__(self, admission: TableAdmission, fanout: TableFanout) -> None:
        self.admission = admission
        self._fanout = fanout

    async def bytes(self, lease: StreamLease, *, auth_expires_at: datetime | None = None) -> AsyncGenerator[bytes]:
        loop = asyncio.get_running_loop()
        next_renewal = loop.time() + LEASE_HEARTBEAT_S
        async with aclosing(self._fanout.stream(lease.watch)) as frames:
            try:
                if auth_expires_at is not None and datetime.now(UTC) >= auth_expires_at:
                    yield RECONNECT_FRAME
                    return
                async for frame in frames:
                    if loop.time() >= next_renewal:
                        try:
                            renewed = await self.admission.renew(lease, now=datetime.now(UTC))
                        except psycopg.Error:
                            _log.warning("table_stream_renewal_failed")
                            renewed = False
                        if not renewed:
                            yield RECONNECT_FRAME
                            return
                        next_renewal = loop.time() + LEASE_HEARTBEAT_S
                    if auth_expires_at is not None and datetime.now(UTC) >= auth_expires_at:
                        yield RECONNECT_FRAME
                        return
                    yield frame
            finally:
                with anyio.CancelScope(shield=True), anyio.move_on_after(2):
                    try:
                        await self.admission.release(lease)
                    except psycopg.Error:
                        _log.warning("table_stream_release_failed")

    async def close(self) -> None:
        await self._fanout.close()

    def subscribe_wakeups(self, callback: Callable[[str, str], None]) -> AbstractAsyncContextManager[None]:
        return self._fanout.subscribe(callback)

    def health(self, *, gm_count: int | None = None) -> dict[str, str | int]:
        table_count = self._fanout.stream_count
        gm_cap = GM_STREAM_LIMIT if gm_count is not None else 0
        total = table_count + (gm_count or 0)
        listener = "listening" if self._fanout.listening else ("reconnecting" if total else "idle")
        return {
            "listener": listener, "streams": table_count, "cap": TABLE_STREAM_LIMIT,
            "gm_streams": gm_count or 0, "gm_cap": gm_cap,
            "total_streams": total, "total_cap": TABLE_STREAM_LIMIT + gm_cap,
        }


def build_table_streams(db: Database) -> TableStreams:
    if db.settings.async_max == 0:
        raise ValueError("WORKBENCH_TABLE_STREAMS_ENABLED requires a table async database pool")
    listen = partial(listen_table_wakeups, default_dsn())
    return TableStreams(TableAdmission(db), TableFanout(TableStreamReads(db), listen))

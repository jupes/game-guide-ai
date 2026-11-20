"""Compose a table stream with its short-lived distributed admission lease."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from functools import partial

import anyio
import psycopg

from .db import Database, default_dsn
from .table_admission import LEASE_HEARTBEAT_S, TABLE_STREAM_LIMIT, StreamLease, TableAdmission
from .table_fanout import RECONNECT_FRAME, TableFanout, listen_table_wakeups
from .table_reads import TableStreamReads

_log = logging.getLogger(__name__)


class TableStreams:
    def __init__(self, admission: TableAdmission, fanout: TableFanout) -> None:
        self.admission = admission
        self._fanout = fanout

    async def bytes(self, lease: StreamLease) -> AsyncGenerator[bytes]:
        loop = asyncio.get_running_loop()
        next_renewal = loop.time() + LEASE_HEARTBEAT_S
        async with aclosing(self._fanout.stream(lease.watch)) as frames:
            try:
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
                    yield frame
            finally:
                with anyio.CancelScope(shield=True), anyio.move_on_after(2):
                    try:
                        await self.admission.release(lease)
                    except psycopg.Error:
                        _log.warning("table_stream_release_failed")

    async def close(self) -> None:
        await self._fanout.close()

    def health(self) -> dict[str, str | int]:
        active = self._fanout.stream_count
        listener = "listening" if self._fanout.listening else ("reconnecting" if active else "idle")
        return {"listener": listener, "streams": active, "cap": TABLE_STREAM_LIMIT}


def build_table_streams(db: Database) -> TableStreams:
    if db.settings.async_max == 0:
        raise ValueError("WORKBENCH_TABLE_STREAMS_ENABLED requires a table async database pool")
    listen = partial(listen_table_wakeups, default_dsn())
    return TableStreams(TableAdmission(db), TableFanout(TableStreamReads(db), listen))

"""Compose a table stream with its short-lived distributed admission lease."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, aclosing
from datetime import UTC, datetime
from functools import partial
from typing import Final

import anyio
import psycopg

from .db import Database, default_dsn
from .gm_stream_reads import GM_STREAM_LIMIT
from .table_admission import LEASE_HEARTBEAT_S, TABLE_STREAM_LIMIT, StreamLease, TableAdmission
from .table_fanout import INACTIVE_FRAME, RECONNECT_FRAME, TableFanout, listen_table_wakeups
from .table_reads import TableStreamReads

_log = logging.getLogger(__name__)
_RENEWAL_TIMEOUT_S: Final = 2.0


class TableStreams:
    def __init__(self, admission: TableAdmission, fanout: TableFanout) -> None:
        self.admission = admission
        self._fanout = fanout

    async def _renew(self, lease: StreamLease) -> bool:
        try:
            async with asyncio.timeout(_RENEWAL_TIMEOUT_S):
                return await self.admission.renew(lease, now=datetime.now(UTC))
        except (TimeoutError, psycopg.Error):
            _log.warning("table_stream_renewal_failed")
            return False

    async def bytes(self, lease: StreamLease, *, auth_expires_at: datetime | None = None) -> AsyncGenerator[bytes]:
        loop = asyncio.get_running_loop()
        next_renewal = loop.time() + LEASE_HEARTBEAT_S
        expires_at = lease.session_expires_at
        if auth_expires_at is not None:
            expires_at = min(expires_at, auth_expires_at)
        async with aclosing(self._fanout.stream(lease.watch)) as frames:
            try:
                while True:
                    remaining = (expires_at - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        yield INACTIVE_FRAME
                        return
                    deadline = asyncio.timeout(remaining)
                    try:
                        async with deadline:
                            renewed = True
                            if loop.time() >= next_renewal:
                                renewed = await self._renew(lease)
                                next_renewal = loop.time() + LEASE_HEARTBEAT_S
                            # Renew before asking the fanout for a frame. Content
                            # stays retractable while the database call is in flight.
                            if renewed:
                                frame = await anext(frames, None)
                            else:
                                frame = INACTIVE_FRAME if self._fanout.known_inactive(lease.watch) else RECONNECT_FRAME
                    except TimeoutError:
                        if not deadline.expired():
                            raise
                        yield INACTIVE_FRAME
                        return
                    if datetime.now(UTC) >= expires_at:
                        yield INACTIVE_FRAME
                        return
                    if frame is None:
                        return
                    yield frame
                    if frame == RECONNECT_FRAME or frame == INACTIVE_FRAME:
                        return
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

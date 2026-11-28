"""The local delivery side of the table stream, driven by PostgreSQL wakeups."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

import psycopg
from psycopg import sql

from .db import CONNECT_TIMEOUT_S
from .table_audio_projection import ended, opening_audio
from .table_reads import TABLE_STREAM_BATCH_MAX, WIRE_VERSION, TableStreamReads, TableStreamSignal, TableStreamWatch
from .table_stream_events import CAMPAIGN_WAKEUP_CHANNEL, CREDENTIAL_WAKEUP_CHANNEL, SESSION_WAKEUP_CHANNEL
from .workbench_contracts import (
    TableAudioEvent,
    TableEvent,
    TableInactiveEvent,
    TableReconnectEvent,
    TableRevealSnapshotEvent,
    TableSessionEvent,
    TableSlotEvent,
    TableSnapshot,
)

_POLL_S: Final = 2.0
# Older WebKit can hold fetch stream bytes until the next write (WebKit 322545).
# An idle stream sends a comment within one second to release buffered updates.
_HEARTBEAT_S: Final = 1.0
_FRESH_FOR_S: Final = 5.0
_QUEUE_MAX: Final = 64
_WAKES: Final = (SESSION_WAKEUP_CHANNEL, CAMPAIGN_WAKEUP_CHANNEL, CREDENTIAL_WAKEUP_CHANNEL)
_log = logging.getLogger(__name__)

type Wakeups = Callable[[], AbstractAsyncContextManager[AsyncIterator[tuple[str, str]]]]


@asynccontextmanager
async def listen_table_wakeups(dsn: str) -> AsyncIterator[AsyncIterator[tuple[str, str]]]:
    """Use a dedicated connection, with polling as the recovery path."""
    async with await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S, application_name="game-guide-ai:table-listener",
    ) as connection:
        for channel in _WAKES:
            await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        yield ((notice.channel, notice.payload) async for notice in connection.notifies())


def _reconnect() -> TableReconnectEvent:
    return TableReconnectEvent(schema_version=WIRE_VERSION, event="reconnect")


def _encode(frame: TableEvent) -> bytes:
    return f"event: {frame.event}\ndata: {frame.model_dump_json()}\n\n".encode()


RECONNECT_FRAME: Final = _encode(_reconnect())
INACTIVE_FRAME: Final = _encode(TableInactiveEvent(schema_version=WIRE_VERSION, event="inactive"))


def _picture(snapshot: TableSnapshot) -> tuple[TableSessionEvent, TableRevealSnapshotEvent]:
    session, picture = snapshot.frames[:2]
    assert isinstance(session, TableSessionEvent) and isinstance(picture, TableRevealSnapshotEvent)
    return session, picture


@dataclass(eq=False)
class _Viewer:
    watch: TableStreamWatch
    queue: asyncio.Queue[TableEvent] = field(default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAX))
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    checking: bool = True
    current: TableSnapshot | None = None
    closed: TableInactiveEvent | TableReconnectEvent | None = None
    checked_at: float | None = None

    def terminal(self, frame: TableInactiveEvent | TableReconnectEvent) -> None:
        if self.closed:
            return
        self.closed = frame
        self.current = None
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(frame)
        self.changed.set()

    def apply(self, snapshot: TableSnapshot | TableStreamSignal | None, checked_at: float) -> None:
        if self.closed:
            return
        if snapshot is TableStreamSignal.ROTATED:
            self.terminal(_reconnect())
            return
        if snapshot is None:
            self.terminal(TableInactiveEvent(schema_version=WIRE_VERSION, event="inactive"))
            return
        self.checking = False
        self.changed.set()
        self.checked_at = checked_at
        if self.current is None:
            self.current = snapshot
            now = datetime.now(UTC)
            for frame in snapshot.frames:
                self.queue.put_nowait(opening_audio(frame, now) if isinstance(frame, TableAudioEvent) else frame)
            return

        old_session, old_picture = _picture(self.current)
        session, picture = _picture(snapshot)
        previous = {slot.slot: slot for slot in old_picture.slots}
        if session != old_session or set(previous) != {slot.slot for slot in picture.slots}:
            self.terminal(_reconnect())
            return
        frames: list[TableEvent] = []
        for slot in picture.slots:
            old = previous[slot.slot]
            if slot == old:
                continue
            if slot.seq != old.seq + 1 or not self.queue.empty():
                self.terminal(_reconnect())
                return
            frames.append(TableSlotEvent(
                schema_version=WIRE_VERSION, event="slot", slot=slot.slot, seq=slot.seq, content=slot.content,
            ))
        previous_audio = {f.slot: f for f in self.current.frames if isinstance(f, TableAudioEvent)}
        for frame in snapshot.frames:
            if not isinstance(frame, TableAudioEvent):
                continue
            old_audio = previous_audio.pop(frame.slot, None)
            if frame == old_audio:
                continue
            if (old_audio is not None and old_audio.seq == frame.seq and old_audio.playing is not None
                    and frame.playing is None and ended(old_audio.playing, datetime.now(UTC)) and self.queue.empty()):
                # Natural completion uses the clock already delivered to the client.
                continue
            if frame.seq != (old_audio.seq if old_audio else 0) + 1 or not self.queue.empty():
                # Replace any buffered play, and recover coalesced commands from a fresh snapshot.
                self.terminal(_reconnect())
                return
            frames.append(frame)
        if previous_audio:
            self.terminal(_reconnect())
            return
        if self.queue.qsize() + len(frames) > _QUEUE_MAX:
            self.terminal(_reconnect())
            return
        self.current = snapshot
        for frame in frames:
            self.queue.put_nowait(frame)


class TableFanout:
    """One listener and one bounded reader per instance while streams are open."""

    def __init__(
        self,
        reads: TableStreamReads,
        listen: Wakeups,
        *,
        poll_s: float = _POLL_S,
        heartbeat_s: float = _HEARTBEAT_S,
        lifetime_s: Callable[[], float] = lambda: random.uniform(240, 280),
    ) -> None:
        self._reads = reads
        self._listen = listen
        self._poll_s = poll_s
        self._heartbeat_s = heartbeat_s
        self._lifetime_s = lifetime_s
        self._viewers: set[_Viewer] = set()
        self._wake_subscribers: dict[object, Callable[[str, str], None]] = {}
        self._dirty: set[_Viewer] = set()
        self._wakeup = asyncio.Event()
        self._listener_ready = asyncio.Event()
        self._workers: tuple[asyncio.Task[None], ...] = ()
        self._closed = False
        self.listening = False

    @property
    def stream_count(self) -> int:
        return sum(not viewer.closed for viewer in self._viewers)

    def known_inactive(self, watch: TableStreamWatch) -> bool:
        # Each admission creates a distinct watch, including two tabs at one seat.
        return any(viewer.watch is watch and isinstance(viewer.closed, TableInactiveEvent) for viewer in self._viewers)

    def _start_workers(self) -> None:
        if not self._workers:
            self._listener_ready.clear()
            self._workers = (asyncio.create_task(self._refresh()), asyncio.create_task(self._receive()))

    @asynccontextmanager
    async def subscribe(self, callback: Callable[[str, str], None]) -> AsyncIterator[None]:
        """Share this instance's single content-free PG listener with the GM reader."""
        if self._closed:
            raise RuntimeError("the realtime listener is closed")
        subscription = object()
        self._wake_subscribers[subscription] = callback
        self._start_workers()
        try:
            # Subscribe before the first private read. If LISTEN stalls the
            # regular state poll still opens the stream within a bounded time.
            try:
                await asyncio.wait_for(self._listener_ready.wait(), min(self._poll_s, _POLL_S))
            except TimeoutError:
                pass
            yield
        finally:
            del self._wake_subscribers[subscription]
            if not self._viewers and not self._wake_subscribers:
                await self._stop_workers()

    async def stream(self, watch: TableStreamWatch) -> AsyncGenerator[bytes]:
        """Yield one fresh snapshot and subsequent frames for the exact watch."""
        if self._closed or len(self._viewers) >= TABLE_STREAM_BATCH_MAX:
            yield RECONNECT_FRAME
            return
        loop = asyncio.get_running_loop()
        viewer = _Viewer(watch)
        expires = loop.time() + self._lifetime_s()
        heartbeat = loop.time() + self._heartbeat_s
        opening_deadline = loop.time() + _FRESH_FOR_S
        self._viewers.add(viewer)
        self._dirty.add(viewer)
        self._wakeup.set()
        self._start_workers()
        try:
            while True:
                now = loop.time()
                if viewer.closed:
                    yield _encode(viewer.queue.get_nowait())
                    return
                fresh_until = opening_deadline if viewer.checked_at is None else viewer.checked_at + _FRESH_FOR_S
                if now >= min(expires, fresh_until):
                    break
                # Keep the frame in the queue until the latest check completes.
                # A terminal decision can still withdraw the entire queue.
                if not viewer.checking and not viewer.queue.empty():
                    heartbeat = now + self._heartbeat_s
                    yield _encode(viewer.queue.get_nowait())
                    continue
                if now >= heartbeat:
                    heartbeat = now + self._heartbeat_s
                    yield b": heartbeat\n\n"
                    continue
                viewer.changed.clear()
                try:
                    await asyncio.wait_for(viewer.changed.wait(), min(expires, heartbeat, fresh_until) - now)
                except TimeoutError:
                    pass
            yield RECONNECT_FRAME
        finally:
            self._viewers.discard(viewer)
            self._dirty.discard(viewer)
            if not self._viewers and not self._wake_subscribers:
                await self._stop_workers()

    async def close(self) -> None:
        self._closed = True
        for viewer in self._viewers:
            viewer.terminal(_reconnect())
        await self._stop_workers()

    async def _stop_workers(self) -> None:
        workers, self._workers = self._workers, ()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _refresh(self) -> None:
        loop = asyncio.get_running_loop()
        # Subscribe before reading the first snapshot. A slow or unavailable
        # listener falls back to polling without holding the first picture.
        try:
            await asyncio.wait_for(self._listener_ready.wait(), self._poll_s)
        except TimeoutError:
            pass
        next_poll = loop.time() + self._poll_s
        while True:
            try:
                await asyncio.wait_for(self._wakeup.wait(), max(0, next_poll - loop.time()))
            except TimeoutError:
                pass
            self._wakeup.clear()
            checked_at = loop.time()
            if checked_at >= next_poll:
                viewers = tuple(viewer for viewer in self._viewers if not viewer.closed)
                next_poll = checked_at + self._poll_s
            else:
                viewers = tuple(viewer for viewer in self._dirty if not viewer.closed)
            self._dirty.clear()
            if not viewers:
                continue
            for viewer in viewers:
                viewer.checking = True
            try:
                snapshots = await self._reads.snapshots([viewer.watch for viewer in viewers], now=datetime.now(UTC))
                for viewer, picture in zip(viewers, snapshots, strict=True):
                    # A definitive loss closes this exact watch. A positive
                    # grant or eligible Rotate still needs the latest recheck.
                    if picture is None or viewer not in self._dirty:
                        viewer.apply(picture, checked_at)
            except Exception:
                _log.warning("table_stream_read_failed")
                for viewer in viewers:
                    viewer.terminal(_reconnect())

    async def _receive(self) -> None:
        while True:
            try:
                async with self._listen() as notices:
                    self.listening = True
                    self._listener_ready.set()
                    async for channel, payload in notices:
                        for callback in self._wake_subscribers.values():
                            callback(channel, payload)
                        for viewer in self._viewers:
                            watch = viewer.watch
                            expected = {
                                SESSION_WAKEUP_CHANNEL: watch.session_id,
                                CAMPAIGN_WAKEUP_CHANNEL: watch.campaign_id,
                                CREDENTIAL_WAKEUP_CHANNEL: watch.grant_id,
                            }
                            if not viewer.closed and expected.get(channel) == payload:
                                viewer.checking = True
                                self._dirty.add(viewer)
                        if self._dirty:
                            self._wakeup.set()
            except psycopg.Error:
                _log.warning("table_stream_listener_unavailable")
                self._listener_ready.set()
            finally:
                self.listening = False
            await asyncio.sleep(self._poll_s)

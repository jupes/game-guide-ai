"""Private GM campaign streams over the instance's existing PostgreSQL listener."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anyio
import psycopg

from .db import Database
from .gm_stream_admission import GmAdmission, GmLease
from .gm_stream_reads import GM_STREAM_LIMIT, GmReads, GmWatch
from .table_admission import LEASE_HEARTBEAT_S
from .table_fanout import RECONNECT_FRAME
from .table_stream_events import CAMPAIGN_WAKEUP_CHANNEL, SESSION_WAKEUP_CHANNEL
from .table_streams import TableStreams
from .workbench_contracts import GmAudioEvent, GmEvent, GmRevealSnapshotEvent, GmSessionEvent, GmSnapshot, PresenceEvent

_log = logging.getLogger(__name__)
_FRESH_FOR_S = 5.0


@dataclass(eq=False)
class _Viewer:
    watch: GmWatch
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    picture: GmSnapshot | None = None
    session_id: str | None = None
    checked: float | None = None
    retried_opening: bool = False
    closed: bool = False

    def update(self, picture: GmSnapshot | None, checked: float) -> None:
        if self.closed:
            return
        self.checked = checked
        if picture is None:
            self.closed = True
        if picture is None or picture != self.picture:
            self.picture = picture
            first = picture.frames[0] if picture is not None else None
            self.session_id = first.session.session_id if isinstance(first, GmSessionEvent) else None
            self.changed.set()


def _updates(previous: GmSnapshot, current: GmSnapshot) -> list[GmEvent] | None:
    """Return complete changed pictures, or reconnect across a generation or sequence gap."""
    if current == previous:
        return []
    before, after = previous.frames[0], current.frames[0]
    if not isinstance(before, GmSessionEvent) or not isinstance(after, GmSessionEvent):
        return None
    a, b = before.session, after.session
    if (a.session_id, a.gen) != (b.session_id, b.gen):
        return None
    if b.audio_epoch < a.audio_epoch or b.reveal_epoch < a.reveal_epoch:
        return None
    frames: list[GmEvent] = []
    if after != before:
        frames.append(after)
    prior_reveal, reveal = previous.frames[1], current.frames[1]
    assert isinstance(prior_reveal, GmRevealSnapshotEvent) and isinstance(reveal, GmRevealSnapshotEvent)
    if prior_reveal != reveal:
        marks = {getattr(s.slot, "participant_id", None): s.seq for s in prior_reveal.state.slots}
        if any(s.seq - marks.get(getattr(s.slot, "participant_id", None), 0) not in (0, 1) for s in reveal.state.slots):
            return None
        frames.append(reveal)
    old_audio = [f for f in previous.frames if isinstance(f, GmAudioEvent)]
    new_audio = [f for f in current.frames if isinstance(f, GmAudioEvent)]
    if new_audio != old_audio:
        if any(new.seq - old.seq not in (0, 1) for old, new in zip(old_audio, new_audio, strict=True)):
            return None
        frames.extend(new_audio)
    presence = next((f for f in current.frames if isinstance(f, PresenceEvent)), None)
    if presence is not None and presence not in previous.frames:
        frames.append(presence)
    return frames


class GmStreams:
    """One batched reader and one latest picture per viewer, never a backlog of private frames."""

    def __init__(
        self, admission: GmAdmission, reads: GmReads, table_streams: TableStreams, *,
        poll_s: float = 2.0, heartbeat_s: float = 1.0,
        lifetime_s: Callable[[], float] = lambda: random.uniform(240, 280),
    ) -> None:
        self.admission = admission
        self._reads = reads
        self._table = table_streams
        self._poll_s = poll_s
        self._heartbeat_s = heartbeat_s
        self._lifetime_s = lifetime_s
        self._viewers: set[_Viewer] = set()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def stream_count(self) -> int:
        return sum(not viewer.closed for viewer in tuple(self._viewers))

    async def snapshot(self, campaign_id: str, *, account_id: int, now: datetime) -> GmSnapshot | None:
        return (await self._reads.snapshots([GmWatch(campaign_id, account_id)], now=now, fail_on_invalid=True))[0]

    def _notified(self, channel: str, value: str) -> None:
        if any((channel == SESSION_WAKEUP_CHANNEL and (v.checked is None or v.session_id == value))
               or (channel == CAMPAIGN_WAKEUP_CHANNEL and v.watch.campaign_id == value) for v in self._viewers):
            self._wake.set()

    async def _refresh(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            async with self._table.subscribe_wakeups(self._notified):
                while True:
                    self._wake.clear()
                    viewers = [viewer for viewer in self._viewers if not viewer.closed]
                    checked = loop.time()
                    if viewers:
                        pictures = await self._reads.snapshots([v.watch for v in viewers], now=datetime.now(UTC))
                        for viewer, picture in zip(viewers, pictures, strict=True):
                            if viewer.checked is None and self._wake.is_set() and not viewer.retried_opening:
                                # A commit during the first read can carry this viewer's
                                # as-yet-unknown session id. Re-read before publishing ready.
                                viewer.retried_opening = True
                                continue
                            viewer.update(picture, checked)
                    try:
                        await asyncio.wait_for(self._wake.wait(), self._poll_s)
                    except TimeoutError:
                        pass
        except Exception:
            _log.warning("gm_stream_read_failed")
            for viewer in self._viewers:
                viewer.update(None, loop.time())

    async def bytes(
        self, lease: GmLease, *, auth_expires_at: datetime, include_presence: bool = True,
    ) -> AsyncGenerator[bytes]:
        loop = asyncio.get_running_loop()
        expires = loop.time() + self._lifetime_s()
        opening_deadline = loop.time() + _FRESH_FOR_S
        renewal = loop.time() + LEASE_HEARTBEAT_S
        viewer = _Viewer(lease.watch)
        previous: GmSnapshot | None = None
        try:
            if self._closed or len(self._viewers) >= GM_STREAM_LIMIT:
                yield RECONNECT_FRAME
                return
            self._viewers.add(viewer)
            self._wake.set()
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._refresh())
            while not viewer.closed:
                now = datetime.now(UTC)
                remaining = min(expires - loop.time(), (auth_expires_at - now).total_seconds())
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(viewer.changed.wait(), min(remaining, self._heartbeat_s))
                except TimeoutError:
                    pass
                viewer.changed.clear()
                if viewer.closed or datetime.now(UTC) >= auth_expires_at or loop.time() >= expires:
                    break
                if viewer.checked is None and loop.time() < opening_deadline:
                    yield b": heartbeat\n\n"
                    continue
                if viewer.checked is None or loop.time() - viewer.checked >= _FRESH_FOR_S:
                    break
                if loop.time() >= renewal:
                    try:
                        if not await self.admission.renew(lease, now=datetime.now(UTC)):
                            break
                    except psycopg.Error:
                        break
                    renewal = loop.time() + LEASE_HEARTBEAT_S
                current = viewer.picture
                if current is None:
                    break
                updates = current.frames if previous is None else _updates(previous, current)
                if updates is None:
                    break
                if not include_presence:
                    updates = [frame for frame in updates if not isinstance(frame, PresenceEvent)]
                for frame in updates:
                    if (viewer.closed or viewer.picture != current or datetime.now(UTC) >= auth_expires_at
                            or loop.time() >= expires or loop.time() - viewer.checked >= _FRESH_FOR_S):
                        yield RECONNECT_FRAME
                        return
                    yield f"event: {frame.event}\ndata: {frame.model_dump_json()}\n\n".encode()
                previous = current
                if not updates:
                    yield b": heartbeat\n\n"
            yield RECONNECT_FRAME
        finally:
            self._viewers.discard(viewer)
            with anyio.CancelScope(shield=True):
                if not self._viewers:
                    await self._stop_worker()
                with anyio.move_on_after(2):
                    try:
                        await self.admission.release(lease)
                    except psycopg.Error:
                        _log.warning("gm_stream_release_failed")

    async def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        for viewer in self._viewers:
            viewer.update(None, asyncio.get_running_loop().time())
        await self._stop_worker()


def build_gm_streams(db: Database, table_streams: TableStreams) -> GmStreams:
    return GmStreams(GmAdmission(db), GmReads(db), table_streams)

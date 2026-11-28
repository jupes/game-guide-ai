"""Output from the table fanout stays bounded and uses fresh database reads."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from service import table_fanout, table_streams
from service.table_admission import StreamLease
from service.table_audio_projection import opening_audio, table_audio_frames
from service.table_fanout import TableFanout, _Viewer
from service.table_reads import TableStreamWatch, _frames
from service.table_streams import TableStreams
from service.workbench_contracts import AudioSlot, TableAudioEvent, TableSlot, TableSlotName

WATCH = TableStreamWatch("cmp_test", "ses_test", 1, account_id=1)


def _snapshot(seq: int):
    return _frames(False, False, [TableSlot(slot=TableSlotName.TABLE, seq=seq, content=None)])


def test_a_gap_or_stop_behind_buffered_content_drops_the_queue() -> None:
    viewer = _Viewer(WATCH)
    viewer.apply(_snapshot(1), 0)
    assert viewer.queue.qsize() == 3
    viewer.apply(_snapshot(2), 1)
    assert viewer.queue.qsize() == 1 and viewer.queue.get_nowait().event == "reconnect"
    viewer = _Viewer(WATCH)
    viewer.apply(_snapshot(1), 0)
    for _ in range(3):
        viewer.queue.get_nowait()
    viewer.apply(_snapshot(4), 2)
    assert viewer.queue.qsize() == 1 and viewer.queue.get_nowait().event == "reconnect"


def _with_audio(*frames):
    return _frames(False, True, [TableSlot(slot=TableSlotName.TABLE, seq=0, content=None)], frames)


def test_audio_slots_advance_independently_and_stale_buffered_plays_cannot_escape() -> None:
    viewer = _Viewer(WATCH)
    viewer.apply(_with_audio(), 0)
    opening = [viewer.queue.get_nowait() for _ in range(viewer.queue.qsize())]
    assert [f.event for f in opening] == ["session", "snapshot", "audio", "audio", "ready"]
    assert [(f.slot, f.seq, f.playing) for f in opening if isinstance(f, TableAudioEvent)] == [
        (AudioSlot.AMBIENCE, 0, None), (AudioSlot.ONE_SHOT, 0, None),
    ]
    now = datetime.now(UTC)
    ambience, shot = table_audio_frames([
        ("ambience", 1, "audio/mpeg", "a" * 32, now, True, 5000),
        ("one_shot", 1, "audio/mpeg", "b" * 32, now, False, 5000),
    ], now)
    viewer.apply(_with_audio(ambience), 1)
    assert viewer.queue.get_nowait() == ambience
    viewer.apply(_with_audio(ambience), 2)
    assert viewer.queue.empty()
    viewer.apply(_with_audio(ambience, shot), 3)
    stopped = shot.model_copy(update={"seq": 2, "playing": None})
    viewer.apply(_with_audio(ambience, stopped), 4)
    assert viewer.queue.qsize() == 1 and viewer.queue.get_nowait().event == "reconnect"

    viewer = _Viewer(WATCH)
    viewer.apply(_with_audio(ambience), 0)
    while not viewer.queue.empty():
        viewer.queue.get_nowait()
    viewer.apply(_with_audio(ambience, stopped), 1)
    assert viewer.queue.qsize() == 1 and viewer.queue.get_nowait().event == "reconnect"


def test_one_shot_reconnect_boundary_and_natural_expiry_preserve_the_sequence() -> None:
    now = datetime.now(UTC)
    started = now - timedelta(seconds=6)
    rows = [("one_shot", 3, "audio/mpeg", "a" * 32, started, False, 5000)]
    [active] = table_audio_frames(rows, started)
    assert active.playing is not None
    assert opening_audio(active, started + timedelta(seconds=4)) == active
    assert opening_audio(active, started + timedelta(milliseconds=4001)).playing is None
    [expired] = table_audio_frames(rows, now)
    assert expired.seq == active.seq and expired.playing is None
    viewer = _Viewer(WATCH)
    viewer.apply(_with_audio(active), 0)
    opening = [viewer.queue.get_nowait() for _ in range(viewer.queue.qsize())]
    assert expired in opening
    viewer.apply(_with_audio(expired), 1)
    assert viewer.queue.empty() and not viewer.closed


@asynccontextmanager
async def _without_listener():
    raise psycopg.OperationalError("synthetic unavailable listener")
    yield


def test_polling_continues_without_a_listener_and_rechecks_each_stream() -> None:
    class Reads:
        calls = 0

        async def snapshots(self, watches, *, now):
            self.calls += 1
            return [_snapshot(1) if self.calls == 1 else None for _ in watches]

    async def scenario():
        reads = Reads()
        fanout = TableFanout(reads, _without_listener, poll_s=0.01, heartbeat_s=0.1)
        received = [frame async for frame in fanout.stream(WATCH)]
        assert len(received) == 4
        assert received[2].startswith(b"event: ready")
        assert received[3].startswith(b"event: inactive")
        assert not fanout.listening and reads.calls == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("stalled", [False, True])
def test_failed_or_stalled_state_reads_cannot_keep_revealed_content_alive(monkeypatch, stalled: bool) -> None:
    monkeypatch.setattr(table_fanout, "_FRESH_FOR_S", 0.06)

    class Reads:
        calls = 0

        async def snapshots(self, watches, *, now):
            self.calls += 1
            if self.calls > 1 and stalled:
                await asyncio.Event().wait()
            if self.calls > 1:
                raise psycopg.OperationalError("synthetic read failure")
            return [_snapshot(1) for _ in watches]

    async def scenario():
        fanout = TableFanout(Reads(), _without_listener, poll_s=0.01, heartbeat_s=0.02)
        received = [frame async for frame in fanout.stream(WATCH)]
        assert received[2].startswith(b"event: ready") and received[-1].startswith(b"event: reconnect")
        assert not fanout.listening

    asyncio.run(scenario())


def test_planned_reconnect_and_shutdown_do_not_leave_workers_running() -> None:
    class Reads:
        async def snapshots(self, watches, *, now):
            return [_snapshot(1) for _ in watches]

    async def scenario():
        fanout = TableFanout(Reads(), _without_listener, poll_s=0.01, heartbeat_s=0.03, lifetime_s=lambda: 0.06)
        received = [frame async for frame in fanout.stream(WATCH)]
        assert received[2].startswith(b"event: ready") and received[-1].startswith(b"event: reconnect")
        await fanout.close()
        received = [frame async for frame in fanout.stream(WATCH)]
        assert len(received) == 1 and received[0].startswith(b"event: reconnect")

    asyncio.run(scenario())


def test_replacement_subscription_keeps_the_shared_listener_after_the_first_closes() -> None:
    async def scenario():
        pending = asyncio.Queue()
        received = asyncio.Queue()

        @asynccontextmanager
        async def listen():
            async def notices():
                while True:
                    yield await pending.get()
            yield notices()

        def notified(channel, value):
            received.put_nowait((channel, value))

        fanout = TableFanout(None, listen)
        async with AsyncExitStack() as initial, AsyncExitStack() as replacement:
            await initial.enter_async_context(fanout.subscribe(notified))
            await replacement.enter_async_context(fanout.subscribe(notified))
            await initial.aclose()
            pending.put_nowait(("table_session", "ses_current"))
            assert await asyncio.wait_for(received.get(), 1) == ("table_session", "ses_current")
            assert fanout.listening and received.empty()
        assert not fanout.listening

    asyncio.run(scenario())


def test_the_http_stream_releases_its_lease_if_renewal_fails_or_client_leaves(monkeypatch) -> None:
    monkeypatch.setattr(table_streams, "LEASE_HEARTBEAT_S", 0)
    lease = StreamLease(uuid4(), WATCH)

    class Admission:
        available = True
        released = 0

        async def renew(self, seen, *, now):
            assert seen == lease
            if not self.available:
                raise psycopg.OperationalError("synthetic renewal failure")
            return True

        async def release(self, seen):
            assert seen == lease
            self.released += 1

    class Fanout:
        async def stream(self, watch):
            assert watch == lease.watch
            while True:
                yield b": heartbeat\n\n"

    async def scenario():
        admission = Admission()
        streams = TableStreams(admission, Fanout())
        connection = streams.bytes(lease)
        assert await anext(connection) == b": heartbeat\n\n"
        await connection.aclose()
        assert admission.released == 1
        admission.available = False
        assert [frame async for frame in streams.bytes(lease)] == [table_fanout.RECONNECT_FRAME]
        assert admission.released == 2

    asyncio.run(scenario())


def test_account_stream_checks_the_signed_cookie_deadline_before_each_frame(monkeypatch):
    issued = datetime.now(UTC)
    deadline = issued + timedelta(seconds=1)
    moment = [issued]

    class Clock:
        @staticmethod
        def now(tz):
            return moment[0]

    class Fanout:
        opens = 0

        async def stream(self, watch):
            self.opens += 1
            yield b"first authorized frame"
            yield b"private contents of the next frame"

    class Admission:
        releases = 0

        async def release(self, lease):
            self.releases += 1

    async def scenario():
        admission, fan = Admission(), Fanout()
        hub = TableStreams(admission, fan)
        account = StreamLease(uuid4(), WATCH)
        stream = hub.bytes(account, auth_expires_at=deadline)
        assert await anext(stream) == b"first authorized frame"
        moment[0] = deadline
        assert [frame async for frame in stream] == [table_fanout.RECONNECT_FRAME]
        assert [frame async for frame in hub.bytes(account, auth_expires_at=deadline)] == [table_fanout.RECONNECT_FRAME]
        assert fan.opens == 1 and admission.releases == 2
        screen = StreamLease(uuid4(), TableStreamWatch(WATCH.campaign_id, WATCH.session_id, 1, grant_id="screen"))
        assert [frame async for frame in hub.bytes(screen)] == [
            b"first authorized frame", b"private contents of the next frame",
        ]
        assert admission.releases == 3

    monkeypatch.setattr(table_streams, "datetime", Clock)
    asyncio.run(scenario())

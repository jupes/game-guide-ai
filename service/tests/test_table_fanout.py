"""Output from the table fanout stays bounded and uses fresh database reads."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from service import table_fanout, table_streams
from service.table_admission import StreamLease
from service.table_audio_projection import opening_audio, table_audio_frames
from service.table_fanout import TableFanout, _Viewer
from service.table_reads import TableStreamSignal, TableStreamWatch, _frames
from service.table_stream_events import SESSION_WAKEUP_CHANNEL
from service.table_streams import TableStreams
from service.workbench_contracts import AudioSlot, TableAudioEvent, TableSlot, TableSlotName

WATCH = TableStreamWatch("cmp_test", "ses_test", 1, account_id=1)


def _snapshot(seq: int):
    return _frames(False, False, [TableSlot(slot=TableSlotName.TABLE, seq=seq, content=None)])


def _revealed(seq: int, name: str):
    slot = TableSlot.model_validate({
        "slot": "table", "seq": seq,
        "content": {"content_kind": "document", "type": "npc", "fields": [{"key": "name", "value": name}]},
    })
    return _frames(False, False, [slot])


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


def test_a_replacement_retracts_a_queued_opening_picture() -> None:
    viewer = _Viewer(WATCH)
    viewer.apply(_revealed(1, "Withdrawn name"), 0)
    assert viewer.queue.get_nowait().event == "session"
    viewer.apply(_revealed(2, "Current name"), 1)
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


class _ControlledReads:
    """The test chooses exactly when each state read and notification completes."""

    def __init__(self, watch=WATCH):
        self.watch = watch
        self.reads = asyncio.Queue()
        self.wakes = asyncio.Queue()

    async def snapshots(self, watches, *, now):
        assert watches == [self.watch]
        answer = asyncio.get_running_loop().create_future()
        self.reads.put_nowait(answer)
        return [await answer]

    @asynccontextmanager
    async def listen(self):
        async def notices():
            while True:
                processed = await self.wakes.get()
                yield SESSION_WAKEUP_CHANNEL, self.watch.session_id
                processed.set()
        yield notices()

    async def wake(self):
        processed = asyncio.Event()
        self.wakes.put_nowait(processed)
        await processed.wait()

    async def answer(self, picture):
        (await self.reads.get()).set_result(picture)


@pytest.mark.parametrize("stalled", [False, True])
def test_an_opening_can_heartbeat_while_listen_and_the_first_read_settle(monkeypatch, stalled):
    monkeypatch.setattr(table_fanout, "_FRESH_FOR_S", 0.16)

    @asynccontextmanager
    async def slow_listener():
        await asyncio.Future()
        yield

    async def scenario():
        reads = _ControlledReads()
        fan = TableFanout(reads, slow_listener, poll_s=0.06, heartbeat_s=0.01)
        async with asyncio.timeout(1), aclosing(fan.stream(WATCH)) as stream:
            assert await anext(stream) == b": heartbeat\n\n"
            first = await reads.reads.get()  # LISTEN fell back to a state read after the heartbeat.
            if stalled:
                frames = [frame async for frame in stream]
                assert frames[-1] == table_fanout.RECONNECT_FRAME
                assert all(frame == b": heartbeat\n\n" for frame in frames[:-1])
                assert first.cancelled()
            else:
                first.set_result(_revealed(1, "Current name"))
                while (frame := await anext(stream)).startswith(b":"):
                    pass
                assert frame.startswith(b"event: session\n")
                assert b"Current name" in await anext(stream)
                assert (await anext(stream)).startswith(b"event: ready\n")

    asyncio.run(scenario())


@pytest.mark.parametrize("rotation", [False, True])
@pytest.mark.parametrize("earlier_rotation", [False, True])
def test_a_new_wakeup_during_a_recheck_freezes_the_queue_until_the_latest_standing(rotation, earlier_rotation):
    async def scenario():
        reads = _ControlledReads()
        fan = TableFanout(reads, reads.listen, poll_s=60, heartbeat_s=10)
        async with asyncio.timeout(2), aclosing(fan.stream(WATCH)) as stream:
            first = asyncio.create_task(anext(stream))
            picture = _revealed(1, "Old private name")
            await reads.answer(picture)
            assert (await first).startswith(b"event: session\n")
            await reads.wake()
            old_read = await reads.reads.get()
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            assert not pending.done()
            # The first read still grants access, but a second notification
            # already superseded that database snapshot.
            await reads.wake()
            old_read.set_result(TableStreamSignal.ROTATED if earlier_rotation else picture)
            final_read = await reads.reads.get()
            assert not pending.done()
            final_read.set_result(TableStreamSignal.ROTATED if rotation else None)
            terminal = await pending
            kind = "reconnect" if rotation else "inactive"
            event, data = terminal.decode().strip().splitlines()
            assert event == f"event: {kind}"
            assert json.loads(data.removeprefix("data: ")) == {"schema_version": 1, "event": kind}
            assert await anext(stream, None) is None
        assert fan.stream_count == 0

    asyncio.run(scenario())


def test_unchanged_standing_resumes_the_buffered_snapshot_without_an_extra_frame():
    async def scenario():
        reads = _ControlledReads()
        fan = TableFanout(reads, reads.listen, poll_s=60, heartbeat_s=10)
        async with asyncio.timeout(2), aclosing(fan.stream(WATCH)) as stream:
            first = asyncio.create_task(anext(stream))
            picture = _revealed(1, "Visible name")
            await reads.answer(picture)
            assert (await first).startswith(b"event: session\n")
            await reads.wake()
            await reads.answer(picture)
            assert b"Visible name" in await anext(stream)
            assert (await anext(stream)).startswith(b"event: ready\n")

    asyncio.run(scenario())


def test_definitive_denial_overtaken_by_a_wakeup_terminates_without_needing_another_read():
    async def scenario():
        reads = _ControlledReads()
        fan = TableFanout(reads, reads.listen, poll_s=60, heartbeat_s=10)
        lease = StreamLease(uuid4(), WATCH)
        released = []

        class Admission:
            async def release(self, seen):
                released.append(seen)

        hub = TableStreams(Admission(), fan)
        async with asyncio.timeout(2), aclosing(hub.bytes(lease)) as stream:
            first = asyncio.create_task(anext(stream))
            await reads.answer(_revealed(1, "Removed audience"))
            assert (await first).startswith(b"event: session\n")
            await reads.wake()
            denial = await reads.reads.get()
            await reads.wake()
            await reads.wake()
            denial.set_result(None)
            # A subsequent read would stall indefinitely. The observed loss
            # of this exact seat/grant is already conclusive.
            assert [frame async for frame in stream] == [table_fanout.INACTIVE_FRAME]
        assert reads.reads.empty() and released == [lease]

    asyncio.run(scenario())


def test_a_revocation_during_lease_renewal_can_still_retract_the_next_frame(monkeypatch):
    monkeypatch.setattr(table_streams, "LEASE_HEARTBEAT_S", 0)

    async def scenario():
        reads = _ControlledReads()
        fan = TableFanout(reads, reads.listen, poll_s=60, heartbeat_s=10)
        lease = StreamLease(uuid4(), WATCH)
        renewing, continue_renewal = asyncio.Event(), asyncio.Event()
        released = []

        class Admission:
            calls = 0

            async def renew(self, seen, *, now):
                assert seen is lease
                self.calls += 1
                if self.calls == 2:
                    renewing.set()
                    await continue_renewal.wait()
                return True

            async def release(self, seen):
                released.append(seen)

        hub = TableStreams(Admission(), fan)
        async with asyncio.timeout(2), aclosing(hub.bytes(lease)) as stream:
            first = asyncio.create_task(anext(stream))
            await reads.answer(_revealed(1, "No longer entitled"))
            assert (await first).startswith(b"event: session\n")
            pending = asyncio.create_task(anext(stream))
            await renewing.wait()
            await reads.wake()
            await reads.answer(None)
            continue_renewal.set()
            assert await pending == table_fanout.INACTIVE_FRAME
            assert await anext(stream, None) is None
        assert len(released) == 1 and released[0] is lease
        assert fan.stream_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("denied", [False, True])
def test_stalled_screen_renewal_returns_only_the_known_terminal_and_releases_the_lease(monkeypatch, denied):
    monkeypatch.setattr(table_streams, "LEASE_HEARTBEAT_S", 0)
    monkeypatch.setattr(table_streams, "_RENEWAL_TIMEOUT_S", 0.12)

    async def scenario():
        watch = TableStreamWatch(WATCH.campaign_id, WATCH.session_id, 1, grant_id="screen")
        lease = StreamLease(uuid4(), watch)
        reads = _ControlledReads(watch)
        fan = TableFanout(reads, reads.listen, poll_s=60, heartbeat_s=10)
        renewing = asyncio.Event()
        released = []

        class Admission:
            calls = 0

            async def renew(self, seen, *, now):
                assert seen is lease
                self.calls += 1
                if self.calls == 2:
                    renewing.set()
                    await asyncio.Future()
                return True

            async def release(self, seen):
                released.append(seen)

        hub = TableStreams(Admission(), fan)
        async with asyncio.timeout(2), aclosing(hub.bytes(lease)) as stream:
            first = asyncio.create_task(anext(stream))
            await reads.answer(_revealed(1, "Old screen picture"))
            assert (await first).startswith(b"event: session\n")
            next_frame = asyncio.create_task(anext(stream))
            await renewing.wait()
            if denied:
                await reads.wake()
                await reads.answer(None)
            assert await next_frame == (table_fanout.INACTIVE_FRAME if denied else table_fanout.RECONNECT_FRAME)
            assert await anext(stream, None) is None
        assert released == [lease] and fan.stream_count == 0

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
        def known_inactive(self, watch):
            assert watch is lease.watch
            return False

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
        assert [frame async for frame in stream] == [table_fanout.INACTIVE_FRAME]
        assert [frame async for frame in hub.bytes(account, auth_expires_at=deadline)] == [table_fanout.INACTIVE_FRAME]
        assert fan.opens == 1 and admission.releases == 2
        screen = StreamLease(uuid4(), TableStreamWatch(WATCH.campaign_id, WATCH.session_id, 1, grant_id="screen"))
        assert [frame async for frame in hub.bytes(screen)] == [
            b"first authorized frame", b"private contents of the next frame",
        ]
        assert admission.releases == 3

    monkeypatch.setattr(table_streams, "datetime", Clock)
    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["renew", "frame"])
def test_cookie_expiry_closes_a_stalled_stream_with_one_inactive_and_releases_its_exact_lease(monkeypatch, blocked):
    monkeypatch.setattr(table_streams, "LEASE_HEARTBEAT_S", 0)

    async def scenario():
        lease = StreamLease(uuid4(), WATCH)
        events = []

        class Admission:
            async def renew(self, seen, *, now):
                assert seen is lease
                events.append("renew")
                if blocked == "renew":
                    await asyncio.Future()
                return True

            async def release(self, seen):
                assert seen is lease
                events.append("release")

        class Fanout:
            async def stream(self, watch):
                events.append("frame")
                await asyncio.Future()
                yield b"old private content"

        hub = TableStreams(Admission(), Fanout())
        async with asyncio.timeout(2):
            deadline = datetime.now(UTC) + timedelta(milliseconds=50)
            received = [frame async for frame in hub.bytes(lease, auth_expires_at=deadline)]
            assert received == [table_fanout.INACTIVE_FRAME]
        assert events == (["renew", "release"] if blocked == "renew" else ["renew", "frame", "release"])

    asyncio.run(scenario())

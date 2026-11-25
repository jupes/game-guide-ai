"""Output from the table fanout stays bounded and uses fresh database reads."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import psycopg
import pytest

from service import table_fanout, table_streams
from service.table_admission import StreamLease
from service.table_fanout import TableFanout, _Viewer
from service.table_reads import TableStreamWatch, _frames
from service.table_streams import TableStreams
from service.workbench_contracts import TableSlot, TableSlotName

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

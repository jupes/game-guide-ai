"""Serving bytes, and MS-10's thread limiter (agent-forge-harness-1kg.8.1.3).

`service/media_serving.py` (ranges, SEC-19's headers, the byte response) and the
limiter in `service/media_objects.py`, with no route and no database. What the
GM's route makes of them is `service/tests/test_asset_serving_api.py`'s.

Run from the repo root:
    uv run python -m pytest service/tests/test_media_serving.py -q
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, MutableMapping
from typing import Any

import anyio
import anyio.to_thread
import pytest

from service import media_objects as mo
from service import media_serving as ms
from service.media_objects import InMemoryObjectStore, StoreBusy, StoreLimiter, StoreOnEventLoop, via_store
from service.media_serving import ByteWindow, RangeNotSatisfiable, byte_window

HEX = "0123456789abcdef" * 2
KEY = "assets/" + HEX

# justification: `Any` types the ASGI messages these tests collect by hand.


@pytest.fixture(autouse=True)
def limiter(monkeypatch: pytest.MonkeyPatch) -> StoreLimiter:
    """A fresh instance limiter per test, so no test can inherit a token."""
    fresh = StoreLimiter()
    monkeypatch.setattr(mo, "STORE_LIMITER", fresh)
    return fresh


# ── Range (RFC 9110 section 14) ─────────────────────────────────────────────

SIZE = 1000


@pytest.mark.parametrize(
    ("header", "window"),
    [
        (None, ByteWindow(0, SIZE, partial=False)),
        ("bytes=0-", ByteWindow(0, SIZE, partial=True)),
        ("bytes=0-0", ByteWindow(0, 1, partial=True)),
        ("bytes=999-999", ByteWindow(999, 1, partial=True)),  # the last byte
        ("bytes=999-", ByteWindow(999, 1, partial=True)),
        ("bytes=10-19", ByteWindow(10, 10, partial=True)),
        ("bytes=990-5000", ByteWindow(990, 10, partial=True)),  # a last position past the end is clipped
        ("bytes=-10", ByteWindow(990, 10, partial=True)),
        ("bytes=-5000", ByteWindow(0, SIZE, partial=True)),  # a suffix longer than the object is all of it
        ("BYTES=10-19", ByteWindow(10, 10, partial=True)),  # the unit is case-insensitive
        # A numeral longer than Python's 4,300-digit `int()` limit (review M-1)
        # still means what its value means: a last position past the end is
        # clipped, and a suffix longer than the object is all of it.
        ("bytes=0-" + "9" * 5000, ByteWindow(0, SIZE, partial=True)),
        ("bytes=-" + "9" * 5000, ByteWindow(0, SIZE, partial=True)),
        ("bytes=" + "0" * 5000 + "10-" + "0" * 5000 + "19", ByteWindow(10, 10, partial=True)),  # zeros add nothing
        # Ignored, so the whole object is answered 200 (section 14.2).
        ("items=0-9", ByteWindow(0, SIZE, partial=False)),
        ("bytes 0-9", ByteWindow(0, SIZE, partial=False)),
        ("bytes=", ByteWindow(0, SIZE, partial=False)),
        ("bytes=-", ByteWindow(0, SIZE, partial=False)),
        ("bytes=9-0", ByteWindow(0, SIZE, partial=False)),
        ("bytes=" + "9" * 5000 + "-" + "9" * 4999, ByteWindow(0, SIZE, partial=False)),  # last < first, both huge
        ("bytes=a-9", ByteWindow(0, SIZE, partial=False)),
        ("bytes=+1-9", ByteWindow(0, SIZE, partial=False)),
        ("bytes=0-1,5-6", ByteWindow(0, SIZE, partial=False)),
        (f"bytes={chr(0x661)}-{chr(0x662)}", ByteWindow(0, SIZE, partial=False)),  # digits, not ASCII ones
    ],
)
def test_a_range_is_read_as_rfc_9110_reads_it(header: str | None, window: ByteWindow) -> None:
    assert byte_window(header, None, SIZE) == window


@pytest.mark.parametrize(
    "header",
    [
        "bytes=1000-", "bytes=1000-1000", "bytes=5000-6000", "bytes=-0",
        "bytes=" + "1" * 5000 + "-",  # a first position of 5,000 digits (review M-1)
        "bytes=" + "1" * 5000 + "-" + "1" * 5001,
        "bytes=-" + "0" * 5000,
    ],
)
def test_a_range_that_starts_past_the_end_or_asks_for_no_suffix_is_unsatisfiable(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        byte_window(header, None, SIZE)


def test_if_range_is_never_matched_because_no_validator_is_ever_issued() -> None:
    """Section 13.1.5: an `If-Range` that does not match means the whole
    representation, and this service sends neither an ETag nor a date."""
    assert byte_window("bytes=10-19", '"anything"', SIZE) == ByteWindow(0, SIZE, partial=False)
    assert byte_window("bytes=-0", "Tue, 01 Sep 2026 12:00:00 GMT", SIZE) == ByteWindow(0, SIZE, partial=False)


# ── SEC-19's headers ─────────────────────────────────────────────────────────


def test_the_headers_are_exactly_sec_19s() -> None:
    assert ms.byte_headers(ByteWindow(0, SIZE, partial=False), SIZE) == {
        "x-content-type-options": "nosniff",
        "content-security-policy": "default-src 'none'; sandbox",
        "content-disposition": "inline",
        "cache-control": "no-store",
        "accept-ranges": "bytes",
        "content-length": "1000",
    }
    partial = ms.byte_headers(ByteWindow(10, 10, partial=True), SIZE)
    assert (partial["content-range"], partial["content-length"]) == ("bytes 10-19/1000", "10")
    assert ms.unsatisfiable_headers(SIZE)["content-range"] == "bytes */1000"
    # A part and a refusal are exactly as uncacheable as the whole (AC 21).
    assert partial == {
        "x-content-type-options": "nosniff",
        "content-security-policy": "default-src 'none'; sandbox",
        "content-disposition": "inline",
        "cache-control": "no-store",
        "accept-ranges": "bytes",
        "content-length": "10",
        "content-range": "bytes 10-19/1000",
    }
    assert ms.unsatisfiable_headers(SIZE) == {
        "x-content-type-options": "nosniff",
        "content-security-policy": "default-src 'none'; sandbox",
        "content-disposition": "inline",
        "cache-control": "no-store",
        "accept-ranges": "bytes",
        "content-range": "bytes */1000",
    }
    for headers in (partial, ms.unsatisfiable_headers(SIZE)):
        assert not {"etag", "last-modified"} & set(headers)
        assert "filename" not in headers["content-disposition"]


# ── The limiter ──────────────────────────────────────────────────────────────


def test_the_limiter_counts_tokens_and_never_waits() -> None:
    limiter = StoreLimiter(2)
    first, second = limiter.lease(), limiter.lease()
    assert limiter.borrowed == 2
    with pytest.raises(StoreBusy):
        limiter.lease()
    assert limiter.borrowed == 2, "a refusal takes nothing"
    first.close()
    first.close()  # idempotent
    assert limiter.borrowed == 1
    third = limiter.lease()
    second.close()
    third.close()
    assert limiter.borrowed == 0
    with pytest.raises(RuntimeError):
        limiter.give()
    assert (mo.STORE_THREADS, StoreLimiter().total) == (6, 6)
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            StoreLimiter(bad)  # type: ignore[arg-type]


def test_via_store_borrows_one_token_for_the_call_and_gives_it_back(limiter: StoreLimiter) -> None:
    store = InMemoryObjectStore()
    seen = via_store(store, lambda s: (limiter.borrowed, s.reachable()))
    assert seen == (1, True) and limiter.borrowed == 0

    def fails(_: mo.ObjectStore) -> None:
        raise mo.ObjectStoreUnavailable()

    with pytest.raises(mo.ObjectStoreUnavailable):
        via_store(store, fails)
    assert limiter.borrowed == 0, "a failed call gives its token back too"


def test_via_store_refuses_at_once_when_every_token_is_held(limiter: StoreLimiter) -> None:
    held = [limiter.lease() for _ in range(mo.STORE_THREADS)]
    called: list[str] = []
    with pytest.raises(StoreBusy):
        via_store(InMemoryObjectStore(), lambda s: called.append("called"))
    assert called == [], "the store was never reached"
    for lease in held:
        lease.close()


def test_via_store_refuses_to_run_on_an_event_loop(limiter: StoreLimiter) -> None:
    called: list[str] = []

    async def on_the_loop() -> None:
        via_store(InMemoryObjectStore(), lambda s: called.append("called"))

    with pytest.raises(StoreOnEventLoop):
        asyncio.run(on_the_loop())
    assert called == [] and limiter.borrowed == 0


def test_a_lease_runs_its_calls_on_a_worker_thread_under_its_own_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a lease, `via_store` takes no second token — the instance's
    limiter here has one, so a second would be refused — and the call never
    runs on the loop's thread nor on a token of the default limiter, the
    request pool (F-2)."""
    one = StoreLimiter(1)
    monkeypatch.setattr(mo, "STORE_LIMITER", one)

    async def main() -> tuple[int, int, int, int, int]:
        loop_thread = threading.get_ident()
        default = anyio.to_thread.current_default_thread_limiter()
        lease = one.lease()
        try:
            def call() -> tuple[int, int, int]:
                return via_store(
                    InMemoryObjectStore(), lambda s: (threading.get_ident(), one.borrowed, default.borrowed_tokens)
                )

            ident, borrowed, default_borrowed = await lease.run(call)
        finally:
            lease.close()
        return loop_thread, ident, borrowed, default_borrowed, one.borrowed

    loop_thread, ident, borrowed, default_borrowed, after = anyio.run(main)
    assert ident != loop_thread
    assert (borrowed, default_borrowed, after) == (1, 0, 0)


def test_a_closed_lease_runs_nothing() -> None:
    lease = StoreLimiter(1).lease()
    lease.close()
    with pytest.raises(RuntimeError):
        anyio.run(lease.run, lambda: None)


# ── The byte response ────────────────────────────────────────────────────────


class Stream:
    """A store stream that records how it was read and closed."""

    def __init__(self, chunks: list[bytes], fail_at: int | None = None) -> None:
        self.chunks = chunks
        self.fail_at = fail_at
        self.pulled_on: list[int] = []
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        self.pulled_on.append(threading.get_ident())
        if self.fail_at is not None and len(self.pulled_on) > self.fail_at:
            raise mo.ObjectStoreUnavailable()
        if not self.chunks:
            raise StopIteration
        return self.chunks.pop(0)

    def close(self) -> None:
        self.closed = True


async def _drive(response: ms.ByteResponse, *, disconnect_after: int | None = None) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    gone = anyio.Event()

    async def receive() -> dict[str, Any]:
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))
        bodies = [m for m in sent if m["type"] == "http.response.body"]
        if disconnect_after is not None and len(bodies) >= disconnect_after:
            gone.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "method": "GET", "headers": []}
    await response(scope, receive, send)
    return sent


def _response(limiter: StoreLimiter, stream: Stream, size: int, **given: Any) -> ms.ByteResponse:
    return ms.ByteResponse(
        limiter.lease(), stream, media_type="image/png", window=ByteWindow(0, size, partial=False), size=size,
        **given,
    )


def test_a_response_pulls_every_chunk_off_the_loop_and_gives_its_lease_back(limiter: StoreLimiter) -> None:
    stream = Stream([b"ab", b"cd", b"e"])

    async def main() -> tuple[int, list[dict[str, Any]]]:
        return threading.get_ident(), await _drive(_response(limiter, stream, 5))

    loop_thread, sent = anyio.run(main)
    start = sent[0]
    assert start["status"] == 200
    assert (b"content-type", b"image/png") in start["headers"]
    assert b"".join(m.get("body", b"") for m in sent[1:]) == b"abcde"
    assert stream.pulled_on and loop_thread not in stream.pulled_on
    assert stream.closed and limiter.borrowed == 0


def test_a_client_that_leaves_part_way_still_frees_the_lease(limiter: StoreLimiter) -> None:
    stream = Stream([b"x" * 10 for _ in range(1000)])
    anyio.run(lambda: _drive(_response(limiter, stream, 10_000), disconnect_after=2))
    assert len(stream.pulled_on) < 1000, "the stream stopped when the client left"
    assert stream.closed and limiter.borrowed == 0


def test_a_store_that_fails_part_way_ends_the_stream_and_frees_the_lease(limiter: StoreLimiter) -> None:
    stream = Stream([b"ab", b"cd"], fail_at=1)
    with pytest.raises(mo.ObjectStoreUnavailable):
        anyio.run(lambda: _drive(_response(limiter, stream, 4)))
    assert stream.closed and limiter.borrowed == 0


def test_the_re_check_is_asked_every_megabyte_and_a_no_ends_the_stream(limiter: StoreLimiter) -> None:
    """Requirement 5.6's seam: the GM passes none; a table route will."""
    chunk = b"z" * (ms.RECHECK_BYTES // 2)
    answers = [True, False]
    asked: list[int] = []

    async def recheck() -> bool:
        asked.append(len(asked))
        return answers[len(asked) - 1]

    stream = Stream([chunk] * 6)
    with pytest.raises(ms.ReadRevoked):
        anyio.run(lambda: _drive(_response(limiter, stream, len(chunk) * 6, recheck=recheck)))
    assert asked == [0, 1], "asked after the first megabyte and after the second"
    assert len(stream.pulled_on) == 4, "no chunk was pulled after the refusal"
    assert stream.closed and limiter.borrowed == 0

    short = Stream([b"a" * 10])
    anyio.run(lambda: _drive(_response(limiter, short, 10, recheck=recheck)))
    assert asked == [0, 1], "a response shorter than a megabyte completes without asking"


def test_serve_refuses_when_every_token_is_held_and_holds_nothing(limiter: StoreLimiter) -> None:
    store = InMemoryObjectStore()
    store.put_stream(KEY, iter([b"abc"]), max_bytes=10)
    window = ByteWindow(0, 3, partial=False)
    held = [limiter.lease() for _ in range(mo.STORE_THREADS)]
    with pytest.raises(StoreBusy):
        anyio.run(lambda: ms.serve(store, key=KEY, media_type="image/png", size=3, window=window))
    for lease in held:
        lease.close()
    with pytest.raises(mo.ObjectMissing):
        anyio.run(lambda: ms.serve(store, key="assets/" + "f" * 32, media_type="image/png", size=3, window=window))
    assert limiter.borrowed == 0, "a refused open gives its lease back"

    async def whole() -> list[dict[str, Any]]:
        return await _drive(await ms.serve(store, key=KEY, media_type="image/png", size=3, window=window))

    assert b"".join(m.get("body", b"") for m in anyio.run(whole)[1:]) == b"abc"
    assert limiter.borrowed == 0

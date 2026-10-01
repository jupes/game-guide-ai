"""Serving a stored object's bytes: `Range`, SEC-19's headers, and a response
that streams under the instance's store limiter (agent-forge-harness-1kg.8.1.3,
slice c of the media bead `1kg.8.1`).

**Nothing here knows an asset, a campaign or an owner.** The GM's route
(`service/asset_serving_api.py`) decides what may be served in a transaction of
its own, commits it, and hands over three things: a key, the type the server
recorded, and the size. A table route (`1kg.7.x`) will decide in its own
`table_principal` query and hand over the same three: it may import this module
and never the asset store (SEC-44(2)).

**No connection, no default thread, no whole object** (requirement 3.8, MS-7,
MS-10, SEC-35). By the time a response exists its caller's transaction has
committed. The response holds one of the instance's `STORE_THREADS` tokens for
its life (`media_objects.StoreLease`) — a seventh concurrent response is
refused `StoreBusy`, which its route answers `503` with `Retry-After` — and
opens the stream and pulls every chunk of at most `CHUNK_BYTES` on a worker
thread under that token. A slow phone therefore holds a request slot, never a
thread of the request pool and never the file.

**`Range`** (RFC 9110 section 14; MS-7: a cue seeks). One byte range is
honoured with `206`; a range that starts at or past the end — or a suffix of
zero bytes — is `416` with `Content-Range: bytes */<size>`. Anything else is
ignored and the whole object answered `200`, as section 14.2 lets a server do:
another unit, a syntax error, several ranges, and any `If-Range` — this service
issues no validator, so none can match (section 13.1.5).

**The headers, exactly** (SEC-19, MS-7): the recorded type and never the
declared one, `X-Content-Type-Options: nosniff`, `Content-Security-Policy:
default-src 'none'; sandbox` — sent here, because the application's middleware
only fills in a policy a response has not set — `Content-Disposition: inline`
with no filename, `Cache-Control: no-store`, `Accept-Ranges: bytes`, and no
`ETag` or `Last-Modified`. No `Cross-Origin-Resource-Policy`: whether pages
other than the table's send one is undecided (threat model TA-6, `ifq`).

**The re-check seam** (requirement 5.6; SEC-16). A response may be given an
async `recheck`, asked before each chunk once `RECHECK_BYTES` have gone since the
last answer; `False` ends the stream with `ReadRevoked`. A response shorter than
that completes. The GM's route passes none: its authorisation is the one query
that found the key. The table side's re-check is `1kg.7.x`'s.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from . import media_objects
from .media_objects import ObjectStore, StoreLease, via_store

#: SEC-16, MS-7: a table response re-checks its slot every this many bytes.
RECHECK_BYTES = 1_000_000
#: What a busy instance asks a client to wait, in seconds (*suggested*, MS-7: a
#: phone retries a cue in seconds).
RETRY_AFTER_S = 2
#: SEC-19: an asset response can never become a page.
ASSET_POLICY = "default-src 'none'; sandbox"

_BYTES_UNIT = re.compile(r"[Bb][Yy][Tt][Ee][Ss]")
_POSITION = re.compile(r"[0-9]+")


class RangeNotSatisfiable(Exception):
    """The one range asked for starts at or past the end (RFC 9110, 15.5.17)."""

    def __init__(self) -> None:
        super().__init__("that range is not in the object")


class ReadRevoked(Exception):
    """A response's re-check said the reader may no longer have the bytes."""

    def __init__(self) -> None:
        super().__init__("the read was revoked part-way")


@dataclass(frozen=True)
class ByteWindow:
    """Which bytes a response carries: `length` of them from `start`, and
    whether that is a part (`206`) or the whole object (`200`)."""

    start: int
    length: int
    partial: bool


def byte_window(range_header: str | None, if_range: str | None, size: int) -> ByteWindow:
    """The window a `GET` asked for, over an object of `size` bytes (>= 1)."""
    whole = ByteWindow(0, size, partial=False)
    if range_header is None or if_range is not None:
        return whole
    unit, equals, ranges = range_header.partition("=")
    specs = [spec.strip() for spec in ranges.split(",") if spec.strip()]
    if not equals or not _BYTES_UNIT.fullmatch(unit.strip()) or len(specs) != 1:
        return whole
    first, dash, last = specs[0].partition("-")
    first, last = first.strip(), last.strip()
    if not dash or not all(_POSITION.fullmatch(part) for part in (first, last) if part) or not (first or last):
        return whole
    if not first:
        suffix = _at_most(last, size)
        if suffix == 0:
            raise RangeNotSatisfiable()
        start = size - suffix
        return ByteWindow(start, size - start, partial=True)
    if last and _order(last) < _order(first):
        return whole
    start = _at_most(first, size)
    if start >= size:
        raise RangeNotSatisfiable()
    end = size - 1 if not last else min(_at_most(last, size), size - 1)
    return ByteWindow(start, end - start + 1, partial=True)


def _order(digits: str) -> tuple[int, str]:
    """A position's place among positions, found without converting it: with
    its leading zeros gone, more digits is larger, and equal lengths compare
    as text."""
    significant = digits.lstrip("0") or "0"
    return len(significant), significant


def _at_most(digits: str, size: int) -> int:
    """A position's value, or `size` for any larger one: every position at or
    past the end means the same. One with more significant digits than `size`
    is never converted, because `int()` refuses a string of over 4,300 digits,
    and that `ValueError` was an unhandled `500` (review M-1)."""
    length, significant = _order(digits)
    if length > len(str(size)):
        return size
    return min(int(significant), size)


def _policy() -> dict[str, str]:
    return {
        "x-content-type-options": "nosniff",
        "content-security-policy": ASSET_POLICY,
        "content-disposition": "inline",
        "cache-control": "no-store",
        "accept-ranges": "bytes",
    }


def byte_headers(window: ByteWindow, size: int) -> dict[str, str]:
    """Every header a `200` or `206` carries but the type, which the response
    sets from the recorded one."""
    headers = {**_policy(), "content-length": str(window.length)}
    if window.partial:
        headers["content-range"] = f"bytes {window.start}-{window.start + window.length - 1}/{size}"
    return headers


def unsatisfiable_headers(size: int) -> dict[str, str]:
    """A `416`'s: where the object ends, under the same policy."""
    return {**_policy(), "content-range": f"bytes */{size}"}


class ByteResponse(StreamingResponse):
    """A `200` or `206` whose chunks are pulled on worker threads under its
    lease, which it gives back — and the stream it closes — however the
    response ends: done, failed, or abandoned by the client."""

    def __init__(
        self,
        lease: StoreLease,
        chunks: Iterator[bytes],
        *,
        media_type: str,
        window: ByteWindow,
        size: int,
        recheck: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._lease = lease
        self._chunks = chunks
        self._recheck = recheck
        super().__init__(
            self._body(), status_code=206 if window.partial else 200, headers=byte_headers(window, size),
            media_type=media_type,
        )

    def _pull(self) -> bytes | None:
        return next(self._chunks, None)

    async def _body(self) -> AsyncIterator[bytes]:
        unchecked = 0
        while True:
            if self._recheck is not None and unchecked >= RECHECK_BYTES:
                if not await self._recheck():
                    raise ReadRevoked()
                unchecked = 0
            chunk = await self._lease.run(self._pull)
            if chunk is None:
                return
            unchecked += len(chunk)
            yield chunk

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._release()

    async def _release(self) -> None:
        close = getattr(self._chunks, "close", None)
        try:
            if close is not None:
                await self._lease.run(close)
        finally:
            self._lease.close()


async def serve(
    store: ObjectStore,
    *,
    key: str,
    media_type: str,
    size: int,
    window: ByteWindow,
    recheck: Callable[[], Awaitable[bool]] | None = None,
) -> ByteResponse:
    """The response for `window` of `key`, or the store's refusal: `StoreBusy`
    when every token is taken, `ObjectMissing` when the object has gone,
    `ObjectStoreUnavailable` when the store cannot answer. Nothing is held when
    it refuses."""
    lease = media_objects.STORE_LIMITER.lease()
    try:
        chunks = await lease.run(
            lambda: via_store(store, lambda s: s.get_stream(key, offset=window.start, length=window.length))
        )
    except BaseException:
        lease.close()
        raise
    return ByteResponse(lease, chunks, media_type=media_type, window=window, size=size, recheck=recheck)

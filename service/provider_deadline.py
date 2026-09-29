"""
A wall-clock deadline on every provider attempt (agent-forge-harness-2bb).

The ihz timeouts bound each wait on its own, so a provider that sends a byte (a
token, an SSE keep-alive, a newline while it generates) more often than every
RAG_LLM_REQUEST_TIMEOUT_S seconds kept an attempt, and the synchronous /chat
worker thread under it, alive without limit. `AttemptDeadlineTransport` stamps
a deadline when an attempt's request arrives (one request per attempt: the
factory's clients have max_retries=0) and caps every wait after it (pool,
connect, TLS, writes, header and body reads) at what is left, so the attempt
ends AT the deadline; a check as each chunk arrives would leave a tail of one
more read timeout, which the retry budget in test_providers.py cannot absorb.
A cut-short wait is httpcore's own timeout: httpx's ReadTimeout and kin, which
the SDK (non-streamed) and generate._as_sdk_timeout (streamed) already turn
into APITimeoutError, recorded, retried and answered like a silent provider.

A /chat turn makes up to four provider calls, each retried, which added up to
458.5 s in spell mode against Cloud Run's 300 s. So the turn has one wall-clock
budget of its own (agent-forge-harness-0u02) that every call draws down: an
attempt's deadline is never later than the turn's, and generate_result starts
no call and no retry the turn can no longer afford (`turn_affords`).
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Iterable, Iterator
from contextvars import ContextVar, Token
from typing import Any, Final

import httpcore
import httpx

# The running attempt's deadline (time.monotonic), None between attempts. Not
# state on a connection, which outlives its attempt in the pool: httpx's sync
# client reads and closes a body in the context that sent its request.
_attempt_deadline: ContextVar[float | None] = ContextVar("provider_attempt_deadline", default=None)

#: One /chat turn's provider budget, 60 s under deploy.sh's --timeout 300 for
#: what runs outside it: the gates before the turn, and the persistence and
#: the ledger write after it. A constant, not a setting, so it cannot be raised
#: past the platform unseen; service/tests/test_providers.py pins it there.
TURN_BUDGET_S: Final = 240.0
#: A call, or a retry after its backoff, that would start with less than this
#: left is refused: it could not finish, and it would still be billed.
TURN_CALL_MIN_S: Final = 5.0

# The running turn's deadline, None outside a turn: /chat sets it before the
# graph runs, whose nodes run in this context or a copy of it.
_turn_deadline: ContextVar[float | None] = ContextVar("provider_turn_deadline", default=None)


def begin_turn() -> Token[float | None]:
    """Start a turn's budget of TURN_BUDGET_S (read now, so a test can shrink it)."""
    return _turn_deadline.set(time.monotonic() + TURN_BUDGET_S)


def end_turn(token: Token[float | None]) -> None:
    _turn_deadline.reset(token)


def _turn_left() -> float | None:
    deadline = _turn_deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def turn_affords(wait_s: float = 0.0) -> bool:
    """Whether a call may start after waiting `wait_s`: always outside a turn."""
    left = _turn_left()
    return left is None or left - wait_s >= TURN_CALL_MIN_S


def _capped(timeout: float | None, expired: type[httpcore.TimeoutException]) -> float | None:
    """`timeout`, cut to what is left of the attempt; `expired` once nothing is."""
    deadline = _attempt_deadline.get()
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise expired("the provider attempt passed its deadline")
    return remaining if timeout is None else min(timeout, remaining)


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, inner: httpcore.NetworkStream) -> None:
        self._inner = inner

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, _capped(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, _capped(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._inner.close()

    def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        tls = self._inner.start_tls(ssl_context, server_hostname, _capped(timeout, httpcore.ConnectTimeout))
        return _DeadlineStream(tls)

    # justification: Any is httpcore.NetworkStream.get_extra_info's own return type, passed through.
    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    """TCP only: the provider transports never set uds, and the base class refuses one."""

    def __init__(self, inner: httpcore.NetworkBackend) -> None:
        self._inner = inner

    def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        timeout = _capped(timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(self._inner.connect_tcp(host, port, timeout, local_address, socket_options))


class AttemptDeadlineTransport(httpx.HTTPTransport):
    """httpx's own transport, with every request ending `deadline_s` after it starts."""

    # justification: forwards httpx.HTTPTransport's keyword arguments, which it types itself.
    def __init__(self, deadline_s: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._deadline_s = deadline_s
        # httpx takes no network backend, so the pool's own is wrapped in place
        # through two private attributes: a rename fails here, at construction;
        # a pool that stops using it fails the trickling-provider tests.
        self._pool._network_backend = _DeadlineBackend(self._pool._network_backend)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Never past the turn's deadline, whatever is left of the attempt's own.
        turn_left = _turn_left()
        deadline_s = self._deadline_s if turn_left is None else max(0.0, min(self._deadline_s, turn_left))
        # The one wait no network call makes: queueing for a pooled connection.
        timeouts = dict(request.extensions.get("timeout") or {})
        pool = timeouts.get("pool")
        timeouts["pool"] = deadline_s if pool is None else min(pool, deadline_s)
        request.extensions["timeout"] = timeouts
        _attempt_deadline.set(time.monotonic() + deadline_s)
        try:
            response = super().handle_request(request)
        except BaseException:
            _attempt_deadline.set(None)
            raise
        assert isinstance(response.stream, httpx.SyncByteStream)
        response.stream = _AttemptBody(response.stream)
        return response


class _AttemptBody(httpx.SyncByteStream):
    """A response body, read under its attempt's deadline, whose close ends the attempt."""

    def __init__(self, inner: httpx.SyncByteStream) -> None:
        self._inner = inner

    def __iter__(self) -> Iterator[bytes]:
        yield from self._inner

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            _attempt_deadline.set(None)

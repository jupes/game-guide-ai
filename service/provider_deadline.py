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
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from typing import Any

import httpcore
import httpx

# The running attempt's deadline (time.monotonic), None between attempts. Not
# state on a connection, which outlives its attempt in the pool: httpx's sync
# client reads and closes a body in the context that sent its request.
_attempt_deadline: ContextVar[float | None] = ContextVar("provider_attempt_deadline", default=None)


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
        # The one wait no network call makes: queueing for a pooled connection.
        timeouts = dict(request.extensions.get("timeout") or {})
        pool = timeouts.get("pool")
        timeouts["pool"] = self._deadline_s if pool is None else min(pool, self._deadline_s)
        request.extensions["timeout"] = timeouts
        _attempt_deadline.set(time.monotonic() + self._deadline_s)
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

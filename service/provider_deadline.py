"""
A wall-clock deadline on every provider attempt (agent-forge-harness-2bb).

The ihz timeouts bound each connect, write and read on its own. A provider that
sends anything (a token, an SSE keep-alive, the newlines some providers send
while they generate a non-streamed answer) more often than every
RAG_LLM_REQUEST_TIMEOUT_S seconds restarts the read bound each time, so an
attempt, and the synchronous /chat worker thread under it, had no upper bound.
A slow legitimate stream did the same.

`AttemptDeadlineTransport` stamps a deadline when an attempt's request reaches
it (one request per attempt: the factory's clients have max_retries=0) and caps
every network wait after that at what is left: the pool wait, connect, TLS,
each write, each read of the headers or the body. The attempt therefore ends AT
its deadline whatever the provider sends. Checking the clock only as each chunk
arrives would leave a tail of one more read timeout after it, which the retry
budget pinned in service/tests/test_providers.py has no room for.

A cut-short wait raises httpcore's own timeout, which httpx turns into its
ReadTimeout (or ConnectTimeout, WriteTimeout, PoolTimeout). The SDK makes that
APITimeoutError while it sends a request, and generate._as_sdk_timeout does the
same for a streamed body read afterwards, so a deadline is recorded, retried
and answered (/chat's timeout 502, retryable) exactly as a silent provider is.
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from typing import Any

import httpcore
import httpx

# The running attempt's deadline on time.monotonic's clock, None between
# attempts. A context variable, not state on a connection: a pooled connection
# outlives the attempt that opened it, while each attempt's reads, streamed or
# not, run in the context that sent it (httpx's sync client), which is also the
# one that closes its body and so ends it.
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

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, inner: httpcore.NetworkBackend) -> None:
        self._inner = inner

    def connect_tcp(
        self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        timeout = _capped(timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(self._inner.connect_tcp(host, port, timeout, local_address, socket_options))

    def connect_unix_socket(  # pragma: no cover - the provider transports never set uds
        self, path: str, timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        timeout = _capped(timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(self._inner.connect_unix_socket(path, timeout, socket_options))

    def sleep(self, seconds: float) -> None:  # pragma: no cover - only httpcore's connect retries sleep
        self._inner.sleep(seconds)


class AttemptDeadlineTransport(httpx.HTTPTransport):
    """httpx's own transport, with every request ending `deadline_s` after it starts."""

    def __init__(self, deadline_s: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._deadline_s = deadline_s
        # httpx takes no network backend, so the httpcore pool's own is wrapped
        # in place (httpcore's public NetworkBackend interface, reached through
        # two private attributes). A future httpx or httpcore that renames them
        # fails here, at client construction; one that stops using the backend
        # fails the trickling-provider tests in test_generation_timeout.py.
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

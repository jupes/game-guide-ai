"""
A stalled provider ends within the timeout (agent-forge-harness-ihz). No fake
at the client: the REAL factory-built ChatOpenAI talks to a loopback server
that reads the request and goes silent, or sends one streamed chunk first.
Each call runs on its own thread, as /chat's synchronous handler does, and must
hand it back within BOUND_S; before the fix it waited forever.

A provider that trickles instead, one byte every DRIP_S = T/2, restarts every
read bound; only the attempt's wall-clock deadline (connect + request timeout,
agent-forge-harness-2bb) ends it. Before that fix it held the thread forever.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpcore
import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage

import config
import service.generate as generate_module
from ingestion.retrieval import RetrievedChunk
from service import provider_deadline
from service.app import _ERROR_DETAIL, app, get_service
from service.model_catalog import DEFAULT_ALIAS
from service.providers import ProviderClientFactory
from service.rag import RagService

TIMEOUT_S = 0.3
DRIP_S = TIMEOUT_S / 2
DEADLINE_S = 2 * TIMEOUT_S  # connect + request, both TIMEOUT_S here
BOUND_S = 15.0  # loose for a loaded CI box; the point is "ends", against "never"
ATTEMPTS = generate_module._MAX_ATTEMPTS
_FIRST_CHUNK = (
    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n"
    b'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"m",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}\n\n'
)
# A non-streamed answer's headers, then only the newlines some providers send
# as keep-alives while they generate it.
_JSON_HEADERS = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: close\r\n\r\n"
_ANSWER = json.dumps({
    "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}],
}).encode()
_ANSWERED = (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\n"
             b"connection: close\r\n\r\n%s" % (len(_ANSWER), _ANSWER))


class StalledProvider:
    """Accepts, reads the request, sends `preamble`, then waits for the client to hang up,
    sending one more byte every `drip_s` seconds meanwhile when that is set."""

    def __init__(self, preamble: bytes, drip_s: float | None = None) -> None:
        self.preamble, self.drip_s, self.accepted, self.hung_up = preamble, drip_s, 0, 0
        self._changed = threading.Condition()
        self._conns: list[socket.socket] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}/v1"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self._conns.append(conn)
            with self._changed:
                self.accepted += 1
            threading.Thread(target=self._stall, args=(conn,), daemon=True).start()

    def _stall(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            conn.sendall(self.preamble)
            conn.settimeout(self.drip_s)  # None: block until the hang-up
            while True:
                try:
                    if not conn.recv(65536):
                        break
                except TimeoutError:
                    conn.sendall(b"\n")
        except OSError:
            pass  # a reset is a hang-up too
        with self._changed:
            self.hung_up += 1
            self._changed.notify_all()

    def hung_up_on(self, count: int) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: self.hung_up >= count, timeout=BOUND_S)

    def close(self) -> None:
        self._listener.close()
        for conn in self._conns:
            conn.close()


@pytest.fixture
def stalled(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., StalledProvider]]:
    for name in ("OPENAI_API_BASE", "RAG_TRACING", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(config, "LLM_REQUEST_TIMEOUT_S", TIMEOUT_S)
    monkeypatch.setattr(config, "LLM_CONNECT_TIMEOUT_S", TIMEOUT_S)
    monkeypatch.setattr(generate_module, "_RETRY_BACKOFF_SECONDS", 0)
    started: list[StalledProvider] = []

    def start(preamble: bytes = b"", drip_s: float | None = None) -> StalledProvider:
        started.append(StalledProvider(preamble, drip_s))
        monkeypatch.setenv("OPENAI_BASE_URL", started[-1].url)
        return started[-1]

    yield start
    for provider in started:
        provider.close()


def on_own_thread(call: Callable[[], object]) -> object:
    """Run `call` on a thread of its own, which must come back within BOUND_S."""
    outcome: list[object] = []

    def target() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(BOUND_S)
    assert not worker.is_alive(), "the provider call is still holding its thread"
    return outcome[0]


class _Streamed:
    """The real client, streamed inside invoke, as LangGraph's message mode makes it stream."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return self.client.invoke(input, config=config, stream=True)


class _Recorded(BaseCallbackHandler):
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.errors: list[type[BaseException] | None] = []

    def on_llm_new_token(self, token: str | list[str | dict[str, Any]], **kwargs: Any) -> None:
        self.tokens.append(str(token))

    def record(self, *, alias: str, result: object, error: BaseException | None) -> None:
        self.errors.append(type(error) if error is not None else None)


def test_a_silent_provider_times_out_and_releases_the_thread(stalled: Callable[..., StalledProvider]) -> None:
    provider = stalled()
    client = ProviderClientFactory().client_for(DEFAULT_ALIAS)
    raised = on_own_thread(lambda: generate_module.generate_result(
        [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, max_attempts=1,
    ))
    assert isinstance(raised, openai.APITimeoutError)
    assert provider.hung_up_on(1)


@pytest.mark.parametrize("drip_s", [None, DRIP_S], ids=["silent", "trickling"])
def test_a_stream_that_stalls_midway_is_retried_and_recorded_as_a_timeout(
    stalled: Callable[..., StalledProvider], drip_s: float | None,
) -> None:
    provider = stalled(_FIRST_CHUNK, drip_s)  # trickling: each attempt ends at its deadline
    seen = _Recorded()
    client = _Streamed(ProviderClientFactory().client_for(DEFAULT_ALIAS))
    raised = on_own_thread(lambda: generate_module.generate_result(
        [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, config={"callbacks": [seen]},
        observer=seen,
    ))
    assert seen.tokens == ["Hel"] * ATTEMPTS  # every attempt stalled after its first chunk
    assert isinstance(raised, openai.APITimeoutError)
    assert isinstance(raised.__cause__, httpx.ReadTimeout)
    assert seen.errors == [openai.APITimeoutError] * ATTEMPTS
    assert provider.hung_up_on(ATTEMPTS)


def test_a_trickling_provider_ends_at_the_attempt_deadline(stalled: Callable[..., StalledProvider]) -> None:
    provider = stalled(_JSON_HEADERS, drip_s=DRIP_S)
    client = ProviderClientFactory().client_for(DEFAULT_ALIAS)
    began = time.monotonic()
    raised = on_own_thread(lambda: generate_module.generate_result(
        [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, max_attempts=1,
    ))
    assert isinstance(raised, openai.APITimeoutError)
    assert time.monotonic() - began >= DEADLINE_S  # the drip beat every read bound; the deadline ended it
    assert provider.hung_up_on(1)


def test_each_attempt_gets_a_deadline_of_its_own(stalled: Callable[..., StalledProvider]) -> None:
    stalled(_ANSWERED)
    client = ProviderClientFactory().client_for(DEFAULT_ALIAS)

    def ask() -> str:
        return generate_module.generate_result(
            [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, max_attempts=1,
        ).text

    assert ask() == "Hello"
    assert provider_deadline._attempt_deadline.get() is None  # closing the answer ended the attempt
    time.sleep(2 * DEADLINE_S)  # long past the first attempt's deadline
    assert ask() == "Hello"


def test_a_request_queued_for_a_busy_connection_ends_at_its_deadline(
    stalled: Callable[..., StalledProvider],
) -> None:
    provider = stalled(_JSON_HEADERS, drip_s=DRIP_S)
    transport = provider_deadline.AttemptDeadlineTransport(DEADLINE_S, limits=httpx.Limits(max_connections=1))

    def queue_behind_the_only_connection() -> object:
        with httpx.Client(transport=transport, timeout=None) as client, client.stream("POST", provider.url):
            return client.post(provider.url)

    assert isinstance(on_own_thread(queue_behind_the_only_connection), httpx.PoolTimeout)


class _TlsRecorder(httpcore.NetworkStream):
    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def start_tls(self, ssl_context: Any, server_hostname: str | None = None,
                  timeout: float | None = None) -> httpcore.NetworkStream:
        self.timeouts.append(timeout)
        return self


def test_a_wait_is_cut_to_what_is_left_of_the_attempt_and_refused_past_it() -> None:
    # The loopback provider speaks plain HTTP; a real provider's TLS handshake is capped too.
    inner, context = _TlsRecorder(), ssl.create_default_context()
    stream = provider_deadline._DeadlineStream(inner)
    stream.start_tls(context, timeout=5.0)  # outside an attempt: untouched
    token = provider_deadline._attempt_deadline.set(time.monotonic() + 1.0)
    try:
        assert isinstance(stream.start_tls(context, timeout=5.0), provider_deadline._DeadlineStream)
        provider_deadline._attempt_deadline.set(time.monotonic() - 0.001)
        with pytest.raises(httpcore.ConnectTimeout):
            stream.start_tls(context, timeout=5.0)
    finally:
        provider_deadline._attempt_deadline.reset(token)
    assert inner.timeouts[0] == 5.0
    assert 0 < (inner.timeouts[1] or 0) <= 1.0
    assert len(inner.timeouts) == 2  # the expired wait never reached the network


def test_a_timeout_raised_without_its_request_still_converts() -> None:
    assert isinstance(generate_module._as_sdk_timeout(httpx.ReadTimeout("stalled")), openai.APITimeoutError)


class _HitRetriever:
    """Stage-method fake with one answerable chunk, so /chat reaches generation."""

    def embed(self, prompt: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    def analyze(self, prompt: str) -> tuple[set[str], set[str], set[str]]:
        return set(), set(), set()

    def search(self, *args: Any, **kwargs: Any) -> list[RetrievedChunk]:
        return [RetrievedChunk(chunk_id="c1", content_type="rule", entity_name="Grappling", class_name=None,
                               feature_name=None, chapter=None, section=None, page_start=1,
                               text_preview="p", cosine_distance=0.1)]

    def fetch(self, chunks: list[RetrievedChunk]) -> tuple[dict[str, str], dict[str, str]]:
        return {"c1": "Grappling is a special melee attack."}, {"c1": "phb-5e"}


@pytest.mark.parametrize(("preamble", "drip_s"), [(b"", None), (_JSON_HEADERS, DRIP_S)], ids=["silent", "trickling"])
def test_chat_answers_the_existing_timeout_502_when_the_provider_stalls(
    stalled: Callable[..., StalledProvider], preamble: bytes, drip_s: float | None,
) -> None:
    provider = stalled(preamble, drip_s)
    svc = RagService(retriever=_HitRetriever())
    app.dependency_overrides[get_service] = lambda: svc
    try:
        response = on_own_thread(lambda: TestClient(app, raise_server_exceptions=False).post(
            "/chat", json={"prompt": "How does grappling work?"},
        ))
    finally:
        app.dependency_overrides.pop(get_service, None)
    assert isinstance(response, httpx.Response)
    detail = {"category": "timeout", "retryable": True, "message": _ERROR_DETAIL["timeout"]}
    assert (response.status_code, response.json()) == (502, {"detail": detail})
    assert provider.hung_up_on(ATTEMPTS)
    assert provider.accepted == ATTEMPTS

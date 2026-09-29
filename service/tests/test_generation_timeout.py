"""
A stalled provider ends within the timeout (agent-forge-harness-ihz). No fake
at the client: the REAL factory-built ChatOpenAI talks to a loopback server
that reads the request and goes silent, or sends one streamed chunk first.
Each call runs on its own thread, as /chat's synchronous handler does, and must
hand it back within BOUND_S; before the fix it waited forever.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage

import config
import service.generate as generate_module
from ingestion.retrieval import RetrievedChunk
from service.app import _ERROR_DETAIL, app, get_service
from service.model_catalog import DEFAULT_ALIAS
from service.providers import ProviderClientFactory
from service.rag import RagService

TIMEOUT_S = 0.3
BOUND_S = 15.0  # loose for a loaded CI box; the point is "ends", against "never"
ATTEMPTS = generate_module._MAX_ATTEMPTS
_FIRST_CHUNK = (
    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n"
    b'data: {"id":"c1","object":"chat.completion.chunk","created":0,"model":"m",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}\n\n'
)


class StalledProvider:
    """Accepts, reads the request, sends `preamble`, then waits for the client to hang up."""

    def __init__(self, preamble: bytes) -> None:
        self.preamble, self.accepted, self.hung_up = preamble, 0, 0
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
            while conn.recv(65536):
                pass
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

    def start(preamble: bytes = b"") -> StalledProvider:
        started.append(StalledProvider(preamble))
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


def test_a_stream_that_stalls_midway_is_retried_and_recorded_as_a_timeout(
    stalled: Callable[..., StalledProvider],
) -> None:
    provider = stalled(_FIRST_CHUNK)
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


def test_chat_answers_the_existing_timeout_502_when_the_provider_stalls(
    stalled: Callable[..., StalledProvider],
) -> None:
    provider = stalled()
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

"""
A stalled provider ends within the timeout (agent-forge-harness-ihz). No fake
at the client: the REAL factory-built ChatOpenAI talks to a loopback server
that reads the request and goes silent, or sends one streamed chunk first.
Each call runs on its own thread, as /chat's synchronous handler does, and must
hand it back within BOUND_S; before the fix it waited forever.

A provider that trickles instead, one byte every DRIP_S = T/2, restarts every
read bound; only the attempt's wall-clock deadline (connect + request timeout,
agent-forge-harness-2bb) ends it. Before that fix it held the thread forever.

A /chat turn's calls together end at the turn's budget (agent-forge-harness-0u02),
which cuts an attempt short and refuses a call or a retry it cannot afford.
Before that fix a trickling provider held a spell turn for four whole attempts.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpcore
import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage

import config
import service.generate as generate_module
from ingestion.retrieval import RetrievedChunk
from service import provider_deadline
from service.app import _ERROR_DETAIL, app, get_service
from service.generate import LLMClient
from service.model_catalog import DEFAULT_ALIAS
from service.models import ChatResponse
from service.providers import ProviderClientFactory
from service.rag import RagService

TIMEOUT_S = 0.3  # the request (read) timeout
CONNECT_S = 0.5  # unlike TIMEOUT_S, so a deadline summing the wrong bounds shows
DRIP_S = TIMEOUT_S / 2
DEADLINE_S = CONNECT_S + TIMEOUT_S
CLOCK_SLACK_S = 0.05  # time.monotonic ticks every 15.6 ms on Windows
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
# A redirect back to the endpoint just asked: followed, every hop is a request
# of its own, under a fresh deadline.
_REDIRECTED = (b"HTTP/1.1 307 Temporary Redirect\r\nlocation: /v1/chat/completions\r\n"
               b"content-length: 0\r\nconnection: close\r\n\r\n")


class StalledProvider:
    """Accepts, reads the request, sends `preamble`, then waits for the client to hang up,
    sending one more byte every `drip_s` seconds meanwhile when that is set."""

    def __init__(self, preamble: bytes, drip_s: float | None = None) -> None:
        self.preamble, self.drip_s, self.accepted, self.sent, self.hung_up = preamble, drip_s, 0, 0, 0
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
            with self._changed:
                self.sent += 1
                self._changed.notify_all()
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

    def sent_to(self, count: int) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: self.sent >= count, timeout=BOUND_S)

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
    monkeypatch.setattr(config, "LLM_CONNECT_TIMEOUT_S", CONNECT_S)
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
def test_a_stream_that_stalls_midway_is_not_retried_and_is_recorded_as_a_timeout(
    stalled: Callable[..., StalledProvider], drip_s: float | None,
) -> None:
    # Not retried (agent-forge-harness-nz78): its request reached the provider,
    # which may bill it. Before, all ATTEMPTS were made and billed.
    provider = stalled(_FIRST_CHUNK, drip_s)  # trickling: the attempt ends at its deadline
    seen = _Recorded()
    client = _Streamed(ProviderClientFactory().client_for(DEFAULT_ALIAS))
    raised = on_own_thread(lambda: generate_module.generate_result(
        [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, config={"callbacks": [seen]},
        observer=seen,
    ))
    assert seen.tokens == ["Hel"]  # the one attempt stalled after its first chunk
    assert isinstance(raised, openai.APITimeoutError)
    assert isinstance(raised.__cause__, httpx.ReadTimeout)
    assert seen.errors == [openai.APITimeoutError]
    assert provider.hung_up_on(1)
    assert provider.accepted == 1


def test_a_trickling_provider_ends_at_the_attempt_deadline(stalled: Callable[..., StalledProvider]) -> None:
    provider = stalled(_JSON_HEADERS, drip_s=DRIP_S)
    client = ProviderClientFactory().client_for(DEFAULT_ALIAS)
    began = time.monotonic()
    raised = on_own_thread(lambda: generate_module.generate_result(
        [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, max_attempts=1,
    ))
    elapsed = time.monotonic() - began
    assert isinstance(raised, openai.APITimeoutError)
    # The drip beat every read bound; the deadline, connect + request, ended it, and only once.
    # agent-forge-harness-g44q M-1: [0.94x, 2x) let a mutant that stamps 1.8x the configured
    # deadline through (117 s per attempt in production, 352.5 s for three attempts, over Cloud
    # Run's 300 s). The budget tolerates at most one extra read timeout past the deadline.
    assert DEADLINE_S - CLOCK_SLACK_S <= elapsed < DEADLINE_S + TIMEOUT_S
    assert provider.hung_up_on(1)


def test_the_attempt_deadline_is_stamped_exactly_now_plus_the_configured_seconds(
    monkeypatch: pytest.MonkeyPatch, stalled: Callable[..., StalledProvider],
) -> None:
    """agent-forge-harness-g44q M-1: pins the enforced stamp itself, reading only
    `_deadline_s`, not merely how long an attempt runs — the elapsed-time bracket above is
    loose enough that a mutant stamping e.g. 1.8x the configured deadline could still land
    inside it on a slow box. Freezing `provider_deadline`'s own view of `time.monotonic` (a
    name replaced only in that module, not the real clock everything else still reads) lets
    the stamp be checked for exact equality instead of a window."""
    provider = stalled(_ANSWERED)
    fixed_now = 1_000_000.0
    monkeypatch.setattr(provider_deadline, "time", SimpleNamespace(monotonic=lambda: fixed_now))
    captured: list[float | None] = []
    original_capped = provider_deadline._capped

    def spying_capped(timeout: float | None, expired: type[httpcore.TimeoutException]) -> float | None:
        if not captured:  # only the first call: connect_tcp, right after the stamp is set
            captured.append(provider_deadline._attempt_deadline.get())
        return original_capped(timeout, expired)

    monkeypatch.setattr(provider_deadline, "_capped", spying_capped)
    transport = provider_deadline.AttemptDeadlineTransport(DEADLINE_S)
    with httpx.Client(transport=transport, timeout=None) as client:
        response = client.post(provider.url)
    assert response.status_code == 200
    assert captured == [fixed_now + DEADLINE_S]


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


def test_an_attempt_on_another_thread_neither_extends_nor_ends_this_ones_deadline(
    stalled: Callable[..., StalledProvider],
) -> None:
    # One client serves every /chat thread. A short answer on a second thread
    # starts and closes its own attempt in the middle of a trickling one.
    trickling, answering = stalled(_JSON_HEADERS, drip_s=DRIP_S), stalled(_ANSWERED)
    transport = provider_deadline.AttemptDeadlineTransport(DEADLINE_S)
    client = httpx.Client(transport=transport, timeout=httpx.Timeout(TIMEOUT_S, connect=CONNECT_S))
    answers: list[bytes] = []

    def answer_meanwhile() -> None:
        # `sent_to(1)` only proves the preamble went out, before any drip; sleeping a couple
        # of drips further makes this genuinely partway through the trickle (agent-forge-
        # harness-g44q M-2), so a shared holder extended by this attempt's own deadline would
        # push the first one's well past the tight bound below, not by a start-time sliver.
        if trickling.sent_to(1):
            time.sleep(2 * DRIP_S)
            answers.append(client.post(answering.url).content)

    meanwhile = threading.Thread(target=answer_meanwhile, daemon=True)
    meanwhile.start()
    began = time.monotonic()
    raised = on_own_thread(lambda: client.post(trickling.url))
    elapsed = time.monotonic() - began
    meanwhile.join(BOUND_S)
    client.close()
    assert answers == [_ANSWER]
    assert isinstance(raised, httpx.ReadTimeout)
    # agent-forge-harness-g44q M-2: the lower bound alone proves "not earlier"; it does not
    # prove "not extended" — a process-shared or ref-counted deadline holder that lets the
    # second thread's attempt push this one's deadline out would still pass it. The upper
    # bound proves the overlapping attempt on the other thread left this deadline alone.
    assert DEADLINE_S - CLOCK_SLACK_S <= elapsed < DEADLINE_S + CLOCK_SLACK_S


def test_an_attempt_is_one_request_and_follows_no_redirect(stalled: Callable[..., StalledProvider]) -> None:
    provider = stalled(_REDIRECTED)
    client = ProviderClientFactory().client_for(DEFAULT_ALIAS)
    raised = on_own_thread(lambda: generate_module.generate_result(
        [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, max_attempts=1,
    ))
    assert isinstance(raised, openai.APIStatusError)
    assert raised.status_code == 307
    assert provider.hung_up_on(1)
    assert provider.accepted == 1


class _TlsRecorder(httpcore.NetworkStream):
    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
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


@pytest.mark.parametrize(("wait", "expired"), [
    (lambda: provider_deadline._DeadlineStream(httpcore.NetworkStream()).read(1, timeout=5.0),
     httpcore.ReadTimeout),
    (lambda: provider_deadline._DeadlineStream(httpcore.NetworkStream()).write(b"x", timeout=5.0),
     httpcore.WriteTimeout),
    (lambda: provider_deadline._DeadlineBackend(httpcore.SyncBackend()).connect_tcp("127.0.0.1", 9, timeout=5.0),
     httpcore.ConnectTimeout),
], ids=["read", "write", "connect"])
def test_a_wait_past_the_deadline_raises_the_timeout_of_its_kind(
    wait: Callable[[], object], expired: type[httpcore.TimeoutException],
) -> None:
    # Each is the kind httpx maps to its own, so the attempt stays a `timeout`.
    token = provider_deadline._attempt_deadline.set(time.monotonic() - 0.001)
    try:
        with pytest.raises(expired, match="passed its deadline"):
            wait()
    finally:
        provider_deadline._attempt_deadline.reset(token)


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
    # One attempt: a timed-out request may be billed, so it is not retried (nz78).
    assert provider.hung_up_on(1)
    assert provider.accepted == 1


# ── The turn's budget (agent-forge-harness-0u02) ──────────────────────────────

TURN_S = DEADLINE_S + 0.3  # one whole attempt, then 0.3 s of a second one
TURN_CALL_MIN_S = 0.05
# The deadline plus a scheduler's wake-up; without the budget, a turn ran at
# least DEADLINE_S - 0.3 = 0.5 s past it.
TURN_SLACK_S = 0.25


@pytest.fixture
def turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_deadline, "TURN_BUDGET_S", TURN_S)
    monkeypatch.setattr(provider_deadline, "TURN_CALL_MIN_S", TURN_CALL_MIN_S)


class _AnswersFirst:
    """Answers a turn's first call itself, and hands every later one to `client`."""

    def __init__(self, client: LLMClient) -> None:
        self.client, self.calls = client, 0

    # justification: LLMClient.invoke's own signature, forwarded unchanged.
    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content="Fire Bolt hurls a mote of fire.")
        return self.client.invoke(input, config=config, **kwargs)


class _TimedService(RagService):
    """Times the graph, which makes every provider call of the turn."""

    elapsed = float("inf")

    # justification: RagService.answer's own arguments, forwarded unchanged.
    def answer(self, *args: Any, **kwargs: Any) -> ChatResponse:
        began = time.monotonic()
        try:
            return super().answer(*args, **kwargs)
        finally:
            self.elapsed = time.monotonic() - began


class _TimesOut:
    """A client whose every call times out while connecting, counted: the one
    timeout still retried (agent-forge-harness-nz78), as no request was sent."""

    def __init__(self) -> None:
        self.calls = 0

    # justification: LLMClient.invoke's own signature.
    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        request = httpx.Request("POST", "https://provider.invalid")
        raise openai.APITimeoutError(request=request) from httpx.ConnectTimeout("connect", request=request)


def _chat(svc: RagService, mode: str) -> httpx.Response:
    app.dependency_overrides[get_service] = lambda: svc
    try:
        response = on_own_thread(lambda: TestClient(app, raise_server_exceptions=False).post(
            "/chat", json={"prompt": "What does Fire Bolt do?", "mode": mode},
        ))
    finally:
        app.dependency_overrides.pop(get_service, None)
    assert isinstance(response, httpx.Response)
    return response


def test_a_spell_turn_answers_within_its_budget_while_its_structuring_calls_trickle(
    stalled: Callable[..., StalledProvider], turn: None,
) -> None:
    provider = stalled(_JSON_HEADERS, drip_s=DRIP_S)
    first = _AnswersFirst(ProviderClientFactory().client_for(DEFAULT_ALIAS))
    factory = ProviderClientFactory(client_builders={DEFAULT_ALIAS: first})
    svc = _TimedService(retriever=_HitRetriever(), factory=factory)
    response = _chat(svc, "spell")
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Fire Bolt hurls a mote of fire."
    assert (body["suggestions"], body["spell_content"]) == (None, None)
    # The suggestions got one whole attempt and one the turn's deadline cut
    # short; the third and the spell card never started. Unbudgeted: 3 + 1 whole.
    assert provider.hung_up_on(2)
    assert provider.accepted == 2
    assert svc.elapsed < TURN_S + TURN_SLACK_S


def test_a_turn_whose_answer_trickles_ends_at_its_budget_with_the_existing_timeout_502(
    stalled: Callable[..., StalledProvider], turn: None,
) -> None:
    provider = stalled(_JSON_HEADERS, drip_s=DRIP_S)
    svc = _TimedService(retriever=_HitRetriever())
    response = _chat(svc, "spell")
    detail = {"category": "timeout", "retryable": True, "message": _ERROR_DETAIL["timeout"]}
    assert (response.status_code, response.json()) == (502, {"detail": detail})
    # One whole attempt, not three: a timed-out request may be billed, so it is
    # not retried (agent-forge-harness-nz78); the turn's budget still bounds it.
    assert provider.hung_up_on(1)
    assert provider.accepted == 1
    assert svc.elapsed < TURN_S + TURN_SLACK_S


SHORT_TURN_S = DEADLINE_S / 4  # a turn that ends well before an attempt's own deadline


def _in_a_turn(call: Callable[[], object]) -> Callable[[], object]:
    def within() -> object:
        token = provider_deadline.begin_turn()
        try:
            return call()
        finally:
            provider_deadline.end_turn(token)

    return within


def test_an_attempt_ends_at_the_turn_deadline_when_that_comes_before_its_own(
    stalled: Callable[..., StalledProvider], turn: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_deadline, "TURN_BUDGET_S", SHORT_TURN_S)
    provider = stalled(_JSON_HEADERS, drip_s=DRIP_S)
    client = ProviderClientFactory().client_for(DEFAULT_ALIAS)
    began = time.monotonic()
    raised = on_own_thread(_in_a_turn(lambda: generate_module.generate_result(
        [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, max_attempts=1,
    )))
    elapsed = time.monotonic() - began
    # Made and cut short, not refused; nearer the turn's deadline than its own.
    assert isinstance(raised, openai.APITimeoutError)
    assert not isinstance(raised, generate_module.TurnBudgetExhausted)
    assert SHORT_TURN_S - CLOCK_SLACK_S <= elapsed < (SHORT_TURN_S + DEADLINE_S) / 2
    assert provider.hung_up_on(1)


def test_a_request_queued_in_a_turn_ends_at_the_turn_deadline(
    stalled: Callable[..., StalledProvider], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_deadline, "TURN_BUDGET_S", SHORT_TURN_S)
    provider = stalled(_JSON_HEADERS, drip_s=DRIP_S)
    transport = provider_deadline.AttemptDeadlineTransport(DEADLINE_S, limits=httpx.Limits(max_connections=1))
    waited: list[float] = []

    def queue_behind_the_only_connection() -> object:
        with httpx.Client(transport=transport, timeout=None) as client, client.stream("POST", provider.url):
            began = time.monotonic()
            try:
                return _in_a_turn(lambda: client.post(provider.url))()
            finally:
                waited.append(time.monotonic() - began)

    assert isinstance(on_own_thread(queue_behind_the_only_connection), httpx.PoolTimeout)
    assert waited[0] < (SHORT_TURN_S + DEADLINE_S) / 2


def test_a_call_the_turn_cannot_afford_is_refused_before_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_deadline, "TURN_BUDGET_S", provider_deadline.TURN_CALL_MIN_S - 1)
    client, seen = _TimesOut(), _Recorded()
    token = provider_deadline.begin_turn()
    try:
        with pytest.raises(generate_module.TurnBudgetExhausted) as refused:
            generate_module.generate_result(
                [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, observer=seen,
            )
    finally:
        provider_deadline.end_turn(token)
    # A timeout: /chat's existing 502 for an answer, a structuring call's None.
    assert isinstance(refused.value, openai.APITimeoutError)
    assert (client.calls, seen.errors) == (0, [])
    assert provider_deadline._turn_deadline.get() is None


@pytest.mark.parametrize(("budget_s", "calls"), [
    (provider_deadline.TURN_CALL_MIN_S + generate_module._RETRY_BACKOFF_SECONDS / 2, 1),
    (60.0, ATTEMPTS),
], ids=["unaffordable", "affordable"])
def test_a_retry_is_made_only_while_the_turn_affords_it(
    monkeypatch: pytest.MonkeyPatch, budget_s: float, calls: int,
) -> None:
    monkeypatch.setattr(provider_deadline, "TURN_BUDGET_S", budget_s)
    client, slept = _TimesOut(), list[float]()
    token = provider_deadline.begin_turn()
    try:
        with pytest.raises(openai.APITimeoutError) as raised:
            generate_module.generate_result(
                [HumanMessage(content="hi")], alias=DEFAULT_ALIAS, client=client, sleep=slept.append,
            )
    finally:
        provider_deadline.end_turn(token)
    assert not isinstance(raised.value, generate_module.TurnBudgetExhausted)  # the last attempt's own
    assert (client.calls, len(slept)) == (calls, calls - 1)

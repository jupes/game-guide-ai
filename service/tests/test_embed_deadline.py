"""
The service's embed stage is bounded (agent-forge-harness-xiu.2.3, PR-1 of 3).

Before: `RagRetriever.embed` used the OpenAI SDK's defaults, two SDK retries
and 600 s per wait, so a silent embeddings provider could hold a /chat worker
thread for about 30 minutes. Now the service's client makes no SDK retries and
bounds every wait (RAG_EMBED_REQUEST_TIMEOUT_S, RAG_EMBED_CONNECT_TIMEOUT_S),
and the retriever owns two attempts on a transient fault, with a fixed backoff
that never honours Retry-After. The bound is per wait: 30.5 s against a silent
provider, while a provider that trickles bytes can still outlast it (a
wall-clock attempt deadline is a follow-up bead). The CLI path, `embed_query`
without a client (eval_golden), keeps the SDK defaults.

No database: the retriever's `connect()` seam serves only the vocabulary query.
No egress: the stalled provider is a loopback socket, every other client a fake.
"""

from __future__ import annotations

import re
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

import config
import ingestion.retrieval as retrieval
import service.generate as generate_module
from ingestion.retrieval import EmbeddingUnavailableError, RagRetriever

DUMMY_KEY = "embed-deadline-dummy-key"
TIMEOUT_S = 0.3
BOUND_S = 15.0  # loose for a loaded CI box; the point is "ends", against "never"
VECTOR = [0.1, 0.2, 0.3]
_REQUEST = httpx.Request("POST", "http://127.0.0.1:9/v1/embeddings")


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _EmptyCursor:
    def __enter__(self) -> _EmptyCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class VocabularyOnly:
    """The `connect()` seam, good for the constructor's vocabulary load only."""

    def __init__(self) -> None:
        self.connects = 0

    @contextmanager
    def connect(self) -> Iterator[VocabularyOnly]:
        self.connects += 1
        yield self

    def cursor(self) -> _EmptyCursor:
        return _EmptyCursor()


class ScriptedEmbeddings:
    """The client shape `embed_query` uses. Raises the scripted faults in turn;
    once the script is spent, every further call succeeds (or re-raises `always`)."""

    def __init__(self, *script: BaseException, always: BaseException | None = None) -> None:
        self.script = list(script)
        self.always = always
        self.calls = 0

    @property
    def embeddings(self) -> ScriptedEmbeddings:
        return self

    def create(self, *, model: str, input: list[str]) -> SimpleNamespace:
        self.calls += 1
        if self.script:
            raise self.script.pop(0)
        if self.always is not None:
            raise self.always
        return SimpleNamespace(data=[SimpleNamespace(embedding=list(VECTOR))], usage=SimpleNamespace(prompt_tokens=3))


class RecordingSink:
    def __init__(self) -> None:
        self.started = 0
        self.records: list[tuple[str | None, int | None]] = []

    def attempt_started(self) -> None:
        self.started += 1

    def record_embedding(self, *, input_tokens: int | None, error: BaseException | None) -> None:
        self.records.append((type(error).__name__ if error is not None else None, input_tokens))


def _status_error(cls: type[openai.APIStatusError], code: int, **headers: str) -> openai.APIStatusError:
    return cls("provider refused", response=httpx.Response(code, request=_REQUEST, headers=headers), body=None)


_TRANSIENT = [
    pytest.param(lambda: openai.APIConnectionError(request=_REQUEST), id="connection"),
    pytest.param(lambda: openai.APITimeoutError(request=_REQUEST), id="timeout"),
    pytest.param(lambda: _status_error(openai.InternalServerError, 500), id="server_error"),
    pytest.param(lambda: _status_error(openai.RateLimitError, 429, **{"retry-after": "60"}), id="rate_limit"),
]

_NOT_TRANSIENT = [
    pytest.param(lambda: _status_error(openai.AuthenticationError, 401), id="authentication"),
    pytest.param(lambda: _status_error(openai.PermissionDeniedError, 403), id="permission"),
    pytest.param(lambda: _status_error(openai.BadRequestError, 400), id="bad_request"),
]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every embed backoff, recorded instead of slept."""
    recorded: list[float] = []
    monkeypatch.setattr(retrieval, "_sleep", recorded.append)
    return recorded


def retriever_over(monkeypatch: pytest.MonkeyPatch, client: ScriptedEmbeddings) -> RagRetriever:
    """A real RagRetriever whose client builder returns `client`, patched as the
    characterization suite patches it: a zero-argument callable."""
    monkeypatch.setattr(retrieval, "_openai_client", lambda: client)
    return RagRetriever(connect=VocabularyOnly().connect)


# ── The client ────────────────────────────────────────────────────────────────


def test_the_service_embedding_client_is_bounded_and_reads_its_timeouts_at_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
    client = retrieval._openai_client()
    assert client.max_retries == 0
    assert client.timeout == httpx.Timeout(config.EMBED_REQUEST_TIMEOUT_S, connect=config.EMBED_CONNECT_TIMEOUT_S)
    monkeypatch.setattr(config, "EMBED_REQUEST_TIMEOUT_S", 2.5)
    monkeypatch.setattr(config, "EMBED_CONNECT_TIMEOUT_S", 1.5)
    assert retrieval._openai_client().timeout == httpx.Timeout(2.5, connect=1.5)


def test_the_batch_cli_path_keeps_the_sdk_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
    unbounded = retrieval._openai_client(bounded=False)
    assert unbounded.max_retries == openai.DEFAULT_MAX_RETRIES
    assert unbounded.timeout == openai.DEFAULT_TIMEOUT
    built: list[dict[str, object]] = []
    fake = ScriptedEmbeddings()

    def recorder(**kwargs: object) -> ScriptedEmbeddings:
        built.append(kwargs)
        return fake

    monkeypatch.setattr(retrieval, "_openai_client", recorder)
    assert retrieval.embed_query("q") == VECTOR
    assert (built, fake.calls) == ([{"bounded": False}], 1)


# ── A silent provider ─────────────────────────────────────────────────────────


class StalledProvider:
    """Accepts, reads the request, then waits for the client to hang up.
    (The pattern of test_generation_timeout.py, copied: tests are not a package.)"""

    def __init__(self) -> None:
        self.accepted, self.hung_up = 0, 0
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
def stalled(monkeypatch: pytest.MonkeyPatch) -> Iterator[StalledProvider]:
    for name in ("OPENAI_API_BASE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                 "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
    monkeypatch.setattr(config, "EMBED_REQUEST_TIMEOUT_S", TIMEOUT_S)
    monkeypatch.setattr(config, "EMBED_CONNECT_TIMEOUT_S", TIMEOUT_S)
    monkeypatch.setattr(retrieval, "_EMBED_RETRY_BACKOFF_S", 0)
    provider = StalledProvider()
    monkeypatch.setenv("OPENAI_BASE_URL", provider.url)
    yield provider
    provider.close()


def on_own_thread(call: Callable[[], object]) -> object:
    """Run `call` on a thread of its own, as /chat's synchronous handler does;
    it must come back within BOUND_S."""
    outcome: list[object] = []

    def target() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(BOUND_S)
    assert not worker.is_alive(), "the embed call is still holding its thread"
    return outcome[0]


def test_a_silent_embeddings_provider_times_out_within_the_bound_and_is_tried_twice(
    stalled: StalledProvider,
) -> None:
    vocabulary = VocabularyOnly()
    retriever = RagRetriever(connect=vocabulary.connect)
    raised = on_own_thread(lambda: retriever.embed("q"))
    assert isinstance(raised, openai.APITimeoutError)
    assert stalled.hung_up_on(retrieval.EMBED_MAX_ATTEMPTS)
    assert stalled.accepted == retrieval.EMBED_MAX_ATTEMPTS == 2
    assert vocabulary.connects == 1  # the vocabulary load; the embed never touched the database


# ── The service-owned retry ───────────────────────────────────────────────────


@pytest.mark.parametrize("make_exc", _TRANSIENT)
def test_transient_embedding_faults_are_retried_once(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], make_exc: Callable[[], BaseException],
) -> None:
    client = ScriptedEmbeddings(make_exc())
    assert retriever_over(monkeypatch, client).embed("q") == VECTOR
    assert client.calls == 2
    assert sleeps == [0.5]  # the fixed backoff; a rate limit's retry-after: 60 is never read


@pytest.mark.parametrize("make_exc", _NOT_TRANSIENT)
def test_non_transient_embedding_faults_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], make_exc: Callable[[], BaseException],
) -> None:
    fault = make_exc()
    client = ScriptedEmbeddings(always=fault)
    with pytest.raises(openai.APIStatusError) as raised:
        retriever_over(monkeypatch, client).embed("q")
    assert raised.value is fault
    assert (client.calls, sleeps) == (1, [])


def test_the_embed_retry_budget_is_two_attempts(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    assert retrieval.EMBED_MAX_ATTEMPTS == 2
    fault = openai.APIConnectionError(request=_REQUEST)
    client = ScriptedEmbeddings(always=fault)
    with pytest.raises(openai.APIConnectionError) as raised:
        retriever_over(monkeypatch, client).embed("q")
    assert raised.value is fault
    assert (client.calls, sleeps) == (2, [0.5])


def test_each_embedding_attempt_is_recorded(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    client = ScriptedEmbeddings(openai.APIConnectionError(request=_REQUEST))
    retriever = retriever_over(monkeypatch, client)
    sink = RecordingSink()
    token = retrieval.set_embedding_sink(sink)
    try:
        assert retriever.embed("q") == VECTOR
    finally:
        retrieval.reset_embedding_sink(token)
    assert sink.records == [("APIConnectionError", None), (None, 3)]
    assert sink.started == 2


@pytest.mark.parametrize("key", [None, "sk-replace-me"], ids=["unset", "placeholder"])
def test_a_missing_key_is_neither_retried_nor_an_attempt(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], key: str | None,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    real = retrieval._openai_client
    builds: list[int] = []

    def counting() -> object:
        builds.append(1)
        return real()

    monkeypatch.setattr(retrieval, "_openai_client", counting)
    retriever = RagRetriever(connect=VocabularyOnly().connect)
    sink = RecordingSink()
    token = retrieval.set_embedding_sink(sink)
    try:
        with pytest.raises(EmbeddingUnavailableError):
            retriever.embed("q")
    finally:
        retrieval.reset_embedding_sink(token)
    assert (len(builds), sink.started, sink.records, sleeps) == (1, 0, [], [])


# ── The budget ────────────────────────────────────────────────────────────────


def test_the_embed_stage_and_one_answer_fit_the_platform_request_timeout() -> None:
    """A silent provider costs connect + request per wait-bounded attempt, so the
    embed stage's worst case is 2 x (5 + 10) + 0.5 = 30.5 s, and one answer's is
    3 x (5 + 60) + 1.5 = 196.5 s: together under Cloud Run's request timeout.
    Per wait, not wall-clock: a provider that trickles bytes can outlast both."""
    deploy = (Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh").read_text()
    platform = re.search(r"--timeout (\d+)", deploy)
    assert platform is not None
    embed_backoff = sum(retrieval._EMBED_RETRY_BACKOFF_S * n for n in range(1, retrieval.EMBED_MAX_ATTEMPTS))
    embed = retrieval.EMBED_MAX_ATTEMPTS * (config.EMBED_CONNECT_TIMEOUT_S + config.EMBED_REQUEST_TIMEOUT_S)
    answer_backoff = sum(generate_module._RETRY_BACKOFF_SECONDS * n for n in range(1, generate_module._MAX_ATTEMPTS))
    answer = generate_module._MAX_ATTEMPTS * (config.LLM_CONNECT_TIMEOUT_S + config.LLM_REQUEST_TIMEOUT_S)
    assert embed + embed_backoff + answer + answer_backoff < int(platform.group(1))

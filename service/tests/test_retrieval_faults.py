"""
Typed retrieval-stage faults (agent-forge-harness-xiu.2.3, PR-2).

The unit contract of `service/evidence.py`, each stage's typing through the
real `RagService` and compiled graph over stage fakes, the reranker's
degradation to the vector order, and the content-free logs of the two /chat
branches a stub service still reaches. The HTTP statuses of the typed faults
are pinned in test_chat_characterization.py, over the real retriever.

Hermetic: no database, no key, no network, invented data. `service/tests` is
not a package, so this module carries its own small fakes and its own
content-free check instead of importing the characterization harness.
"""

from __future__ import annotations

import ast
import logging
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import psycopg
import psycopg.errors
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from psycopg_pool import PoolClosed, PoolTimeout

import ingestion.retrieval as retrieval
import service.app as service_app
import service.generate as generate_module
from ingestion.retrieval import EmbeddingUnavailableError, RetrievedChunk
from service import usage_capture
from service.evidence import (
    CORPUS_DB_FAULTS,
    EMBED_FAULTS,
    SECONDARY_FAULTS,
    SECONDARY_NEVER_TYPED,
    SHARED_INFRA_FAULTS,
    STAGE_ORDER,
    EvidenceAttempt,
    RerankOrderInvalid,
    RetrievalStage,
    RetrievalStageError,
    attempt_for_fault,
    classify_fault,
    first_lost_evidence,
)
from service.model_catalog import DEFAULT_ALIAS, get_profile
from service.rag import RagService, SecondaryResult

CANARY = "zq-faults-3c71"
PROMPT = f"How does grappling work? {CANARY}"
ANSWER = "An answer drawn from the numbered sources [1]."
CONV = "rf-conv-1"
NEAR = retrieval.KOZ_ANSWERABLE_DISTANCE - 0.2
CHUNKS = [
    RetrievedChunk("rf-c1", "monster", "Basilisk", None, None, "Ch", "Sec", 7, "preview rf-c1", NEAR),
    RetrievedChunk("rf-c2", "monster", "Cockatrice", None, None, "Ch", "Sec", 8, "preview rf-c2", NEAR + 0.05),
]
VECTOR_ORDER = ["Basilisk", "Cockatrice"]
_REQUEST = httpx.Request("POST", "http://127.0.0.1:9/v1/embeddings")


def _status(cls: type[openai.APIStatusError], code: int) -> openai.APIStatusError:
    return cls(f"provider refused {CANARY}", response=httpx.Response(code, request=_REQUEST), body=None)


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _StageRetriever:
    """`RagRetriever`'s stage methods over invented chunks; a stage may raise."""

    def __init__(self, fail: dict[str, BaseException] | None = None) -> None:
        self.fail = dict(fail or {})
        self.calls: Counter[str] = Counter()

    def _enter(self, stage: str) -> None:
        self.calls[stage] += 1
        exc = self.fail.get(stage)
        if exc is not None:
            raise exc

    def embed(self, prompt: str) -> list[float]:
        self._enter("embed")
        return [0.1, 0.2, 0.3]

    def analyze(self, prompt: str) -> tuple[set[str], set[str], set[str]]:
        return set(), set(), set()  # no content type: the reranker's gate lets prose through

    def search(self, emb: list[float], prompt: str, k: int, classes: set[str], entities: set[str],
               content_types: set[str] | None, book_slugs: set[str] | None) -> list[RetrievedChunk]:
        self._enter("search")
        return list(CHUNKS)

    def fetch(self, chunks: list[RetrievedChunk]) -> tuple[dict[str, str], dict[str, str]]:
        self._enter("fetch")
        return ({c.chunk_id: f"Invented full text for {c.chunk_id}." for c in chunks},
                {c.chunk_id: "rf-book" for c in chunks})


class _Secondary:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.calls = 0

    def retrieve(self, prompt: str, k: int = 5) -> SecondaryResult:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return SecondaryResult()


class _Reranker:
    """Reverses the vector order, answers a fixed `order`, or raises."""

    def __init__(self, *, order: list[int] | None = None, exc: BaseException | None = None) -> None:
        self.order = order
        self.exc = exc
        self.calls = 0

    def rerank(self, query: str, texts: list[str]) -> list[int]:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return list(self.order) if self.order is not None else list(reversed(range(len(texts))))


class _LLM:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.calls = 0

    def invoke(self, input: object, config: object = None, **kwargs: object) -> AIMessage:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return AIMessage(content=ANSWER)


def _service(
    retriever: object, *, reranker: _Reranker | None = None, secondary: _Secondary | None = None,
    llm: _LLM | None = None,
) -> RagService:
    return RagService(retriever=retriever, reranker=reranker, llm_client=llm or _LLM(),
                      secondary_retriever=secondary or _Secondary())


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "RAG_TRACING", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(generate_module, "_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(retrieval, "_EMBED_RETRY_BACKOFF_S", 0)
    assert not generate_module._looks_like_statblock(ANSWER)  # else sage makes a second LLM call


def _rag_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "service.rag" and r.levelno == logging.WARNING]


# ── The evidence contract ─────────────────────────────────────────────────────

_CLASSIFY = [
    pytest.param(lambda: openai.APITimeoutError(request=_REQUEST), "timeout", id="api_timeout"),
    pytest.param(lambda: PoolTimeout(CANARY), "timeout", id="pool_timeout"),
    pytest.param(lambda: psycopg.errors.QueryCanceled(CANARY), "timeout", id="query_canceled"),
    pytest.param(lambda: TimeoutError(CANARY), "timeout", id="builtin_timeout"),
    pytest.param(lambda: _status(openai.BadRequestError, 400), "rejected", id="bad_request"),
    pytest.param(lambda: _status(openai.UnprocessableEntityError, 422), "rejected", id="unprocessable"),
    pytest.param(lambda: EmbeddingUnavailableError(CANARY), "unavailable", id="missing_key"),
    pytest.param(lambda: openai.APIConnectionError(message=CANARY, request=_REQUEST), "unavailable", id="connection"),
    pytest.param(lambda: _status(openai.InternalServerError, 500), "unavailable", id="server_error"),
    pytest.param(lambda: _status(openai.RateLimitError, 429), "unavailable", id="rate_limit"),
    pytest.param(lambda: _status(openai.AuthenticationError, 401), "unavailable", id="authentication"),
    pytest.param(lambda: _status(openai.PermissionDeniedError, 403), "unavailable", id="permission"),
    pytest.param(lambda: psycopg.OperationalError(CANARY), "unavailable", id="operational"),
    pytest.param(lambda: PoolClosed(CANARY), "unavailable", id="pool_closed"),
    pytest.param(lambda: psycopg.errors.UndefinedTable(CANARY), "error", id="undefined_table"),
    pytest.param(lambda: _status(openai.NotFoundError, 404), "error", id="other_status"),
    pytest.param(lambda: RerankOrderInvalid(CANARY), "error", id="rerank_order"),
    pytest.param(lambda: RuntimeError(CANARY), "error", id="runtime"),
]


@pytest.mark.parametrize(("make_exc", "outcome"), _CLASSIFY)
def test_classify_fault(make_exc: Callable[[], BaseException], outcome: str) -> None:
    assert classify_fault(make_exc()) == outcome


def test_a_stage_error_says_nothing_of_its_cause() -> None:
    cause = psycopg.OperationalError(f"connection to the corpus failed {CANARY}")
    assert CANARY in str(cause)  # positive control: the cause does carry it
    err = RetrievalStageError.from_fault("vector_search", cause)
    for text in (str(err), repr(err), repr(err.args)):
        assert CANARY not in text
        assert all(word in text for word in ("vector_search", "unavailable", "OperationalError")), text
    assert (err.stage, err.outcome, err.error_class, err.attempts) == (
        "vector_search", "unavailable", "OperationalError", ())
    assert err.attempt() == EvidenceAttempt("local", "vector_search", "unavailable", "OperationalError")
    again = RetrievalStageError.from_attempt(err.attempt())
    assert (again.attempt(), again.args) == (err.attempt(), err.args)
    # The gate's full tuple rides along, outside the message and args.
    lost = (err.attempt(), EvidenceAttempt("secondary", "secondary", "timeout", "PoolTimeout"))
    carried = RetrievalStageError.from_attempt(err.attempt(), lost)
    assert (carried.attempts, carried.args) == (lost, err.args)


def test_first_lost_evidence_follows_the_stage_order() -> None:
    def lost(stage: RetrievalStage) -> EvidenceAttempt:
        return attempt_for_fault(stage, RuntimeError(CANARY))

    assert STAGE_ORDER == ("embed", "vector_search", "fetch", "secondary", "rerank")
    assert first_lost_evidence([lost("secondary"), lost("embed")]) == lost("embed")
    assert first_lost_evidence([lost("fetch"), lost("vector_search")]) == lost("vector_search")
    assert first_lost_evidence([lost("rerank"), lost("fetch")]) == lost("fetch")
    assert first_lost_evidence([lost("rerank")]) is None
    assert first_lost_evidence([]) is None
    assert [lost(s).source_kind for s in STAGE_ORDER] == ["local", "local", "local", "secondary", "local"]
    assert [lost(s).loses_evidence for s in STAGE_ORDER] == [True, True, True, True, False]


@pytest.mark.parametrize("exc", [PermissionError(CANARY), OSError(CANARY), RuntimeError(CANARY),
                                 TypeError(CANARY), KeyError(CANARY)],
                         ids=["PermissionError", "OSError", "RuntimeError", "TypeError", "KeyError"])
def test_the_degradable_sets_exclude_bugs_and_authorization_errors(exc: BaseException) -> None:
    assert not isinstance(exc, EMBED_FAULTS + CORPUS_DB_FAULTS + SECONDARY_FAULTS)
    # A database-grant denial IS a psycopg.Error, so the secondary carves it out by name.
    denial = psycopg.errors.InsufficientPrivilege(CANARY)
    assert isinstance(denial, SECONDARY_FAULTS)
    assert isinstance(denial, SECONDARY_NEVER_TYPED)


def test_the_fault_sets_are_exactly_the_contract() -> None:
    assert EMBED_FAULTS == (EmbeddingUnavailableError, openai.OpenAIError)
    assert CORPUS_DB_FAULTS == (psycopg.Error,)
    assert SECONDARY_FAULTS == (psycopg.Error, openai.OpenAIError, EmbeddingUnavailableError)
    assert SECONDARY_NEVER_TYPED == (psycopg.errors.InsufficientPrivilege,)
    # The bead's topology note: the connection gate and the provider credential
    # are shared with every store and with generation, so they never degrade.
    assert SHARED_INFRA_FAULTS == (PoolTimeout, PoolClosed, EmbeddingUnavailableError, openai.AuthenticationError)


# ── Each stage through the real service and graph ─────────────────────────────


def _arrange(stage: str, exc: BaseException) -> tuple[_StageRetriever, _Secondary]:
    where = {"embed": "embed", "vector_search": "search", "fetch": "fetch"}
    if stage == "secondary":
        return _StageRetriever(), _Secondary(exc)
    return _StageRetriever({where[stage]: exc}), _Secondary()


_STAGE_FAULTS = [
    pytest.param("embed", "sage", lambda: _status(openai.RateLimitError, 429), "unavailable", id="embed"),
    pytest.param("vector_search", "sage", lambda: psycopg.OperationalError(CANARY), "unavailable",
                 id="vector_search"),
    pytest.param("fetch", "sage", lambda: PoolTimeout(CANARY), "timeout", id="fetch"),
    pytest.param("secondary", "gm", lambda: psycopg.errors.UndefinedTable(CANARY), "error", id="secondary"),
]


@pytest.mark.parametrize(("stage", "mode", "make_exc", "outcome"), _STAGE_FAULTS)
def test_each_stage_fault_is_raised_typed(
    stage: str, mode: str, make_exc: Callable[[], BaseException], outcome: str,
) -> None:
    exc = make_exc()
    retriever, secondary = _arrange(stage, exc)
    llm = _LLM()
    svc = _service(retriever, secondary=secondary, llm=llm)
    with pytest.raises(RetrievalStageError) as caught:
        svc.answer_with_evidence(PROMPT, mode=mode)
    assert (caught.value.stage, caught.value.outcome, caught.value.error_class) == (stage, outcome, type(exc).__name__)
    assert caught.value.__cause__ is exc
    assert llm.calls == 0


_BUGS = [
    pytest.param("embed", "sage", lambda: TypeError(CANARY), id="embed-TypeError"),
    pytest.param("vector_search", "sage", lambda: KeyError(CANARY), id="search-KeyError"),
    pytest.param("secondary", "gm", lambda: RuntimeError(CANARY), id="secondary-RuntimeError"),
    pytest.param("secondary", "gm", lambda: PermissionError(CANARY), id="secondary-PermissionError"),
    pytest.param("secondary", "gm", lambda: psycopg.errors.InsufficientPrivilege(CANARY),
                 id="secondary-InsufficientPrivilege"),
]


@pytest.mark.parametrize(("stage", "mode", "make_exc"), _BUGS)
def test_a_bug_in_a_stage_propagates_untyped(stage: str, mode: str, make_exc: Callable[[], BaseException]) -> None:
    exc = make_exc()
    retriever, secondary = _arrange(stage, exc)
    llm = _LLM()
    with pytest.raises(type(exc)) as caught:
        _service(retriever, secondary=secondary, llm=llm).answer_with_evidence(PROMPT, mode=mode)
    assert caught.value is exc
    assert llm.calls == 0


def test_a_reranker_fault_is_recorded_and_the_vector_order_kept(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    control = _service(_StageRetriever(), reranker=_Reranker()).answer_with_evidence(PROMPT)
    assert ([s.entity for s in control.response.sources], control.attempts) == (VECTOR_ORDER[::-1], ())
    assert _rag_warnings(caplog) == []
    reranker = _Reranker(exc=RuntimeError(f"cross-encoder failed {CANARY}"))
    llm = _LLM()
    outcome = _service(_StageRetriever(), reranker=reranker, llm=llm).answer_with_evidence(
        PROMPT, conversation_id=CONV)
    assert outcome.attempts == (EvidenceAttempt("local", "rerank", "error", "RuntimeError"),)
    assert [s.entity for s in outcome.response.sources] == VECTOR_ORDER
    assert (outcome.response.answer, outcome.response.answerable) == (ANSWER, True)
    assert (reranker.calls, llm.calls) == (1, 1)
    [record] = _rag_warnings(caplog)
    line = record.getMessage()
    assert all(word in line for word in ("stage=rerank", "source=local", "outcome=error", "error=RuntimeError",
                                         f"conversation_id={CONV}")), line
    assert record.exc_info is None
    assert CANARY not in caplog.text


@pytest.mark.parametrize("order", [[0, 0], [0], [0, 5], [0, 1, 2]],
                         ids=["duplicate", "missing", "out_of_range", "too_long"])
def test_an_invalid_rerank_order_is_a_fault_not_a_reorder(order: list[int]) -> None:
    reranker = _Reranker(order=order)
    outcome = _service(_StageRetriever(), reranker=reranker).answer_with_evidence(PROMPT)
    assert [s.entity for s in outcome.response.sources] == VECTOR_ORDER
    assert outcome.attempts == (EvidenceAttempt("local", "rerank", "error", "RerankOrderInvalid"),)
    assert reranker.calls == 1


def test_generation_errors_are_never_typed_as_retrieval(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    llm = _LLM(_status(openai.RateLimitError, 429))
    retriever = _StageRetriever()
    with pytest.raises(openai.RateLimitError) as caught:
        _service(retriever, llm=llm).answer_with_evidence(PROMPT)
    assert caught.value is llm.exc
    assert (retriever.calls["embed"], llm.calls) == (1, generate_module._MAX_ATTEMPTS)
    assert _rag_warnings(caplog) == []


# ── The embed stage's own seams ───────────────────────────────────────────────


class _EmptyCursor:
    def __enter__(self) -> _EmptyCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _VocabularyOnly:
    """The real retriever's `connect()` seam, good for its vocabulary load only."""

    @contextmanager
    def connect(self) -> Iterator[_VocabularyOnly]:
        yield self

    def cursor(self) -> _EmptyCursor:
        return _EmptyCursor()


class _FlakyEmbeddings:
    """Fails its first call with a transient fault, then succeeds."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def embeddings(self) -> _FlakyEmbeddings:
        return self

    def create(self, *, model: str, input: list[str]) -> SimpleNamespace:
        self.calls += 1
        if self.calls == 1:
            raise openai.APIConnectionError(message=f"Connection error. {CANARY}", request=_REQUEST)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])],
                               usage=SimpleNamespace(prompt_tokens=3))


class _RealEmbedRetriever(_StageRetriever):
    """The stage fakes, except that `embed` is the real `RagRetriever.embed`."""

    def __init__(self) -> None:
        super().__init__()
        self.real = retrieval.RagRetriever(connect=_VocabularyOnly().connect)

    def embed(self, prompt: str) -> list[float]:
        return self.real.embed(prompt)


def test_each_retried_embed_attempt_is_its_own_ledger_record(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[dict[str, object]] = []
    monkeypatch.setattr(usage_capture, "_emit_record", lambda operation, fields: records.append(dict(fields)))
    client = _FlakyEmbeddings()
    monkeypatch.setattr(retrieval, "_openai_client", lambda: client)
    token = usage_capture.begin_operation(
        mode="sage", billed_account_id=1, actor_kind=usage_capture.ACTOR_ACCOUNT, campaign_id=None,
        request=SimpleNamespace(headers={}),
    )
    try:
        outcome = _service(_RealEmbedRetriever()).answer_with_evidence(PROMPT)
    finally:
        usage_capture.end_operation(token)
    assert (outcome.response.answer, outcome.attempts, client.calls) == (ANSWER, (), 2)
    embeddings = [(r["retry_index"], r["error_class"]) for r in records if r["purpose"] == "embedding"]
    assert embeddings == [(0, "APIConnectionError"), (1, None)]


def test_the_retrieval_module_imports_no_undeclared_http_client() -> None:
    # agent-forge-harness-0oh: httpx is not a declared core dependency; the SDK re-exports its Timeout.
    tree = ast.parse(Path(retrieval.__file__).read_text(encoding="utf-8"))
    roots = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
             for alias in node.names}
    roots |= {node.module.split(".")[0] for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom) and node.module and node.level == 0}
    assert {"openai", "psycopg"} <= roots
    assert "httpx" not in roots
    assert openai.Timeout is httpx.Timeout


# ── The /chat branches a stub service still reaches ───────────────────────────


class _RaisingService:
    """Stands in for `RagService.answer`, raising an untyped backend fault."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def answer(self, prompt: str, mode: str = "sage", conversation_id: str | None = None,
               attachment_context: str | None = None, attachment_label: str | None = None) -> None:
        self.calls += 1
        raise self.exc


@pytest.fixture
def stub_chat() -> Iterator[Callable[[BaseException], tuple[httpx.Response, _RaisingService]]]:
    overrides = (service_app.get_service, service_app.get_message_store, service_app.get_timeline_store,
                 service_app.get_timeline_database)
    saved = {dep: service_app.app.dependency_overrides.get(dep) for dep in overrides}

    def post(exc: BaseException) -> tuple[httpx.Response, _RaisingService]:
        svc = _RaisingService(exc)
        service_app.app.dependency_overrides[service_app.get_service] = lambda: svc
        for dep in overrides[1:]:
            service_app.app.dependency_overrides[dep] = lambda: None
        client = TestClient(service_app.app, raise_server_exceptions=False)
        response = client.post("/chat", json={"prompt": PROMPT, "mode": "sage", "conversation_id": CONV})
        return response, svc

    yield post
    for dep, previous in saved.items():
        if previous is None:
            service_app.app.dependency_overrides.pop(dep, None)
        else:
            service_app.app.dependency_overrides[dep] = previous


def _assert_response_content_free(response: httpx.Response) -> None:
    """No prompt or fault text, and no model, provider or secret name (D-9)."""
    profile = get_profile(DEFAULT_ALIAS)
    assert profile is not None
    haystack = (response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())).lower()
    assert CANARY not in haystack
    for word in ("openai", DEFAULT_ALIAS, profile.display_name, profile.api_model, retrieval.EMBED_MODEL,
                 "OPENAI_API_KEY"):
        assert word.lower() not in haystack, word


@pytest.mark.parametrize(("exc", "detail", "line"), [
    pytest.param(psycopg.OperationalError(f"corpus down {CANARY}"), "retrieval backend unavailable",
                 "retrieval backend error", id="db"),
    pytest.param(EmbeddingUnavailableError(f"no key {CANARY}"), "embedding backend unavailable",
                 "embedding unavailable", id="embedding_unavailable"),
])
def test_the_untyped_chat_branches_log_the_class_never_the_message(
    stub_chat: Callable[[BaseException], tuple[httpx.Response, _RaisingService]],
    caplog: pytest.LogCaptureFixture, exc: BaseException, detail: str, line: str,
) -> None:
    caplog.set_level(logging.WARNING)
    response, svc = stub_chat(exc)
    assert svc.calls == 1
    assert CANARY in str(exc)  # positive control: the fault carries it
    assert (response.status_code, response.json()) == (503, {"detail": detail})
    _assert_response_content_free(response)
    assert line in caplog.text
    assert f"error={type(exc).__name__}" in caplog.text
    assert CANARY not in caplog.text

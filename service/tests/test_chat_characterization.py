"""
Characterization of `POST /chat` before the additive-retrieval refactor
(agent-forge-harness-xiu.2.1; spec docs/forge/plans/additive-retrieval-web-fallback.md).

WHAT THIS PINS. Current behaviour, not desired behaviour: the strict Sage /
Spell / Rules refusal, the GM empty-result refusal, the attachment bypass in
`gate_node`, the reranker's gating and ordering, every retrieval failure
boundary (since agent-forge-harness-xiu.2.3 every stage fault is typed: an
embedding fault, a missing key included, is the embedding backend's 503, or
the existing 422 `invalid_request` when the provider rejects the request, never
the generation-error branch; a PostgreSQL retrieval error is the retrieval
backend's 503; a reranker fault keeps the vector order), the history-write and
attachment-fetch failures that already do not fail the answer, and the
pre-retrieval authentication, authorization and budget gates, which refuse
before anything is spent (a routing-store outage among them, a handled 503).

HOW. Every test posts to `/chat` through the REAL `RagService`, the real
compiled LangGraph pipeline and the real `RagRetriever` stage methods. Fakes sit
only at the external edges: the retriever's `connect()` seam, the embeddings
client, the chat model, the message store, the reranker, the secondary
retriever, the metrics sink and (for two cases) the auth store. Nothing fakes
`RagService.answer` or the graph, so a refactor of either is seen here.
Hermetic by construction: no key, a base URL that refuses connections, no
tracing, no proxies, no database, no sleeps.

THREE CLASSES OF PIN.
- INVARIANT (every test not named below): must stay green, unedited, through
  the refactor. A later bead may not weaken one quietly.
- FLAG_DEFAULT: must stay green, unedited, with xiu.2.2's evidence-selection
  flag at its default; xiu.2.2 adds flag-on counterparts next to them.
- SLATED: the "before" picture a named bead is expected to change. A bead that
  flips one edits the test AND its SLATED entry in the same diff, so the flip is
  a visible, reviewed decision rather than a silent one.

READING THE COUNTS. An embed count is a call to `embeddings.create` on the fake.
The service's embeddings client makes no SDK retries (pinned by the
client-configuration test; agent-forge-harness-xiu.2.3), so every embed request
is a visible call: at most `retrieval.EMBED_MAX_ATTEMPTS`, the service-owned
retry of a transient fault, and exactly one for any other. Where a
turn fails inside the handler's `try:`, "no message rows" means exactly that:
the ownership claim and the strategy binding were committed before the `try:`
and stay. When both branches of the GM fan-out fail, which error wins is
nondeterministic and deliberately not pinned. Error bodies are asserted
content-free (a canary token in the prompt and in every injected fault, and the
model / provider / secret names of D-9). Log text is asserted only where a pin
says so, and then as content-free: the class and the stage or category, never
the canary.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import NamedTuple, Protocol

import httpx
import openai
import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from psycopg_pool import PoolTimeout

import config
import ingestion.rerank as rerank_module
import ingestion.retrieval as retrieval
import service.app as service_app
import service.generate as generate_module
from service.app import app, require_session
from service.history import InMemoryMessageStore, StoredAttachment
from service.metrics import MetricPoint
from service.model_catalog import CATALOG_REVISION, DEFAULT_ALIAS, get_profile
from service.models import REFUSAL
from service.rag import RagService, SecondaryResult
from service.ratelimit import RateLimited
from service.session import SessionData, encode_session
from service.workbench_contracts import CHAT_TEXT_MAX_CHARS

# ── Registries (see the module docstring) ─────────────────────────────────────

FLAG_DEFAULT: frozenset[str] = frozenset({
    "test_strict_modes_refuse_a_corpus_miss",
    "test_strict_gate_threshold_is_inclusive",
    "test_gm_generates_on_weak_chunks_and_reports_unanswerable",
    "test_gm_refuses_when_retrieval_returns_nothing",
    "test_gm_counts_secondary_chunks_before_the_gate",
    "test_attachment_bypasses_the_gate",
    "test_attachment_fetch_failure_falls_back_to_the_strict_refusal",
    "test_answerability_is_decided_before_rerank",
    "test_embedding_api_errors_are_typed_embed_faults",
    "test_embedding_and_generation_failures_are_distinguishable",
})

SLATED: dict[str, str] = {
    "test_missing_embedding_key_is_a_503":
        "agent-forge-harness-xiu.2.3: a missing embedding key becomes a typed embed fault that degrades",
    "test_retrieval_database_errors_are_a_503":
        "agent-forge-harness-xiu.2.3: vector-search and fetch faults degrade to generation instead of a 503",
    "test_gm_secondary_failure_fails_the_turn":
        "agent-forge-harness-xiu.2.3: a secondary-retriever fault degrades to the primary result",
    "test_history_write_failure_does_not_fail_the_answer":
        "agent-forge-harness-ul21: the message-persistence consistency policy may change the status or the "
        "partial rows",
    "test_gm_channel_refuses_a_non_dm_session_before_retrieval":
        "agent-forge-harness-ubw: the dm-role check becomes campaign ownership plus tier (TA-3); "
        "any caller still refused must still spend nothing",
}

_SLATED_VALUE = re.compile(r"^agent-forge-harness-[a-z0-9]+(\.\d+)*: .+")

# ── Test data (invented; no rulebook text) ────────────────────────────────────

CANARY = "zq-char-8e2f"
CONV = "char-conv-1"
PROSE = "How does grappling work?"          # no content type inferred: reranked
STRUCTURED = "Which monster has a petrifying gaze?"   # "monster" keyword: not reranked
FAILING_PROMPT = f"{PROSE} {CANARY}"
FAKE_ANSWER = "A characterization answer drawn from the numbered sources [1]."
ATTACHMENT_NAME = "notes.txt"
ATTACHMENT_TEXT = "The orb is cursed."
BOOK = "char-book"
SECONDARY_BOOK = "char-world"
DOCUMENTED_REFUSAL = "I couldn't find that in the D&D 5e sources I have."

K = retrieval.KOZ_ANSWERABLE_DISTANCE
NEAR = K - 0.2
FAR = K + 0.2
JUST_ABOVE = math.nextafter(K, math.inf)

_EMBED_REQUEST = httpx.Request("POST", "http://127.0.0.1:9/embeddings")
_SESSION_SECRET = "characterization-session-secret-long-enough"
_PINNED_KEYS = ("answer", "sources", "answerable", "mode", "conversation_id", "suggestions",
                "spell_content", "stat_block")
_SOURCE_FIELDS = ("book", "chapter", "section", "entity", "page", "snippet")
_RETRIEVAL_DOWN = {"detail": "retrieval backend unavailable"}
_EMBED_DOWN = {"detail": "embedding backend unavailable"}
_ROUTING_DOWN = {"detail": "authorization backend unavailable"}
_INTERNAL = {"detail": "internal error"}


class ChunkRow(NamedTuple):
    """One vector-search row, in `retrieve_top_k`'s column order."""
    chunk_id: str
    content_type: str
    entity_name: str
    class_name: str | None
    feature_name: str | None
    chapter: str
    section: str
    page_start: int
    text_preview: str
    cosine_distance: float


def chunk(chunk_id: str, distance: float, entity: str) -> ChunkRow:
    return ChunkRow(chunk_id, "monster", entity, None, None, "Char Chapter", "Char Section", 7,
                    f"preview {chunk_id}", distance)


def full_text(chunk_id: str) -> str:
    # Differs from the preview within the first SNIPPET_MAX characters, so a
    # snippet built from the preview instead of the fetched text is visible.
    return f"Invented full text for {chunk_id}, which its preview does not carry."


def snippet_of(text: str) -> str:
    return text[:config.SNIPPET_MAX] + ("…" if len(text) > config.SNIPPET_MAX else "")


def corpus_source(row: ChunkRow) -> dict[str, object]:
    return {"book": BOOK, "chapter": row.chapter, "section": row.section, "entity": row.entity_name,
            "page": row.page_start, "snippet": snippet_of(full_text(row.chunk_id))}


ATTACHMENT_SOURCE: dict[str, object] = {
    "book": ATTACHMENT_NAME, "chapter": None, "section": "Attachment", "entity": None, "page": None,
    "snippet": ATTACHMENT_TEXT,
}

HIT = [chunk("char-c1", NEAR, "Basilisk"), chunk("char-c2", NEAR + 0.05, "Cockatrice")]
MISS = [chunk("char-c1", FAR, "Basilisk"), chunk("char-c2", FAR + 0.05, "Cockatrice")]

SECONDARY_CHUNK = retrieval.RetrievedChunk(
    "char-w1", "world", "Char Mill", None, None, None, "Lore", 1, "preview char-w1", NEAR,
)
SECONDARY_SOURCE: dict[str, object] = {
    "book": SECONDARY_BOOK, "chapter": None, "section": "Lore", "entity": "Char Mill", "page": 1,
    "snippet": snippet_of(full_text("char-w1")),
}


def db_down(what: str) -> psycopg.OperationalError:
    return psycopg.OperationalError(f"{what} unavailable {CANARY}")


# ── Fakes at the external edges ───────────────────────────────────────────────


def _statement_kind(sql: str) -> str:
    # Order matters: the filtered vector search also contains "= ANY(".
    # If the SQL text ever changes, adapt this classifier, never an assertion.
    if "GROUP BY class_name" in sql:
        return "classes"
    if "GROUP BY entity_name" in sql:
        return "entities"
    if "<=>" in sql:
        return "search"
    if "= ANY(" in sql:
        return "fetch"
    raise AssertionError(f"unrecognised retrieval statement: {sql[:60]!r}")


class FakeCorpus:
    """What the injected `connect()` context manager yields. Connect 1 is the
    retriever constructor's vocabulary load, 2 is search, 3 is fetch."""

    def __init__(
        self, rows: list[ChunkRow], *, fail: dict[str, BaseException] | None = None,
        fail_connect_on: int | None = None,
    ) -> None:
        self.rows = list(rows)
        self.fail = dict(fail or {})
        self.fail_connect_on = fail_connect_on
        self.statements: list[str] = []
        self.connects = 0
        self.raised: list[BaseException] = []

    @contextmanager
    def connect(self) -> Iterator[FakeCorpus]:
        self.connects += 1
        if self.connects == self.fail_connect_on:
            exc = PoolTimeout(f"no connection came free {CANARY}")
            self.raised.append(exc)
            raise exc
        yield self

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def count(self, kind: str) -> int:
        return sum(1 for kind_seen in self.statements if kind_seen == kind)


class _FakeCursor:
    def __init__(self, corpus: FakeCorpus) -> None:
        self.corpus = corpus
        self.result: list[tuple[object, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        kind = _statement_kind(sql)
        self.corpus.statements.append(kind)
        exc = self.corpus.fail.get(kind)
        if exc is not None:
            self.corpus.raised.append(exc)
            raise exc
        if kind == "classes":
            self.result = []
        elif kind == "entities":
            self.result = [("Basilisk", "monster", 3)]
        elif kind == "search":
            self.result = [tuple(r) for r in self.corpus.rows]
        else:
            self.result = [(r.chunk_id, full_text(r.chunk_id), BOOK) for r in self.corpus.rows]

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.result


class _Fault:
    """Counts calls and raises the configured exception, keeping what it raised."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.calls = 0
        self.raised: list[BaseException] = []

    def _fire(self) -> None:
        self.calls += 1
        if self.exc is not None:
            self.raised.append(self.exc)
            raise self.exc


class FakeEmbeddings(_Fault):
    @property
    def embeddings(self) -> FakeEmbeddings:
        return self

    def create(self, *, model: str, input: list[str]) -> SimpleNamespace:
        self._fire()
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])],
                               usage=SimpleNamespace(prompt_tokens=3))


class FakeChatModel(_Fault):
    def __init__(self, exc: BaseException | None = None) -> None:
        super().__init__(exc)
        self.messages: list[list[BaseMessage]] = []

    def invoke(self, input: list[BaseMessage], config: object = None, **kwargs: object) -> AIMessage:
        self.messages.append(list(input))
        self._fire()
        return AIMessage(content=FAKE_ANSWER)


class CountingReranker(_Fault):
    def rerank(self, query: str, texts: list[str]) -> list[int]:
        self._fire()
        return list(reversed(range(len(texts))))  # reverses the vector order


class FakeSecondary(_Fault):
    def __init__(self, result: SecondaryResult | None = None, exc: BaseException | None = None) -> None:
        super().__init__(exc)
        self.result = result or SecondaryResult()

    def retrieve(self, prompt: str, k: int = 5) -> SecondaryResult:
        self._fire()
        return self.result


class FakeAuthStore(_Fault):
    def get_user_by_id(self, user_id: int) -> None:
        self._fire()


class _Faulty(Protocol):
    raised: list[BaseException]


class RecordingSink:
    def __init__(self) -> None:
        self.points: list[MetricPoint] = []

    def record(self, point: MetricPoint) -> None:
        self.points.append(point)


class CountingBudget:
    """Stands in for `service.app.check_chat_request`: counts, then raises the
    configured refusal or passes through to the real budget."""

    def __init__(self, real: Callable[[int], None], exc: BaseException | None = None) -> None:
        self.real = real
        self.exc = exc
        self.calls = 0

    def __call__(self, user_id: int) -> None:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        self.real(user_id)


@dataclass
class ProbedStore(InMemoryMessageStore):
    """The in-memory twin, counting calls and raising an injected fault per method."""

    faults: dict[str, BaseException] = field(default_factory=dict)
    append_fault_roles: frozenset[str] = frozenset()
    calls: Counter[str] = field(default_factory=Counter)
    raised: list[BaseException] = field(default_factory=list)

    def _enter(self, name: str) -> None:
        self.calls[name] += 1
        exc = self.faults.get(name)
        if exc is not None:
            self.raised.append(exc)
            raise exc

    def append(
        self, conversation_id: str, mode: str, role: str, content: str,
        suggestions: list[dict[str, object]] | None = None,
    ) -> int | None:
        self.calls[f"append:{role}"] += 1
        if role in self.append_fault_roles:
            exc = db_down(f"history write ({role})")
            self.raised.append(exc)
            raise exc
        return super().append(conversation_id, mode, role, content, suggestions=suggestions)

    def attachments_for(self, conversation_id: str) -> list[StoredAttachment]:
        self._enter("attachments_for")
        return super().attachments_for(conversation_id)

    def claim_conversation(self, conversation_id: str, user_id: int) -> int:
        self._enter("claim_conversation")
        return super().claim_conversation(conversation_id, user_id)

    def calls_today(self) -> int:
        self._enter("calls_today")
        return super().calls_today()

    def claim_conversation_strategy(
        self, conversation_id: str, *, strategy: str, manual_alias: str | None, catalog_revision: str,
    ) -> tuple[str, str | None]:
        self._enter("claim_conversation_strategy")
        return super().claim_conversation_strategy(
            conversation_id, strategy=strategy, manual_alias=manual_alias, catalog_revision=catalog_revision,
        )

    def conversation_binding(self, conversation_id: str) -> tuple[str, str | None, str | None] | None:
        self._enter("conversation_binding")
        return super().conversation_binding(conversation_id)

    def rebind_conversation_strategy(
        self, conversation_id: str, *, strategy: str, manual_alias: str | None, catalog_revision: str,
    ) -> None:
        self._enter("rebind_conversation_strategy")
        super().rebind_conversation_strategy(
            conversation_id, strategy=strategy, manual_alias=manual_alias, catalog_revision=catalog_revision,
        )


# ── Harness ───────────────────────────────────────────────────────────────────


@dataclass
class ChatRun:
    response: httpx.Response
    prompt: str
    svc: RagService
    llm: FakeChatModel
    emb: FakeEmbeddings
    corpus: FakeCorpus
    store: ProbedStore
    sink: RecordingSink
    reranker: CountingReranker | None
    secondary: FakeSecondary
    auth: FakeAuthStore | None

    @property
    def body(self) -> dict[str, object]:
        body = self.response.json()
        assert isinstance(body, dict)
        return body

    @property
    def searches(self) -> int:
        return self.corpus.count("search")

    def rows(self) -> list[tuple[str, str]]:
        return [(m.role.value, m.content) for m in self.store.recent(CONV, 1000)]

    def points(self, name: str) -> list[MetricPoint]:
        return [p for p in self.sink.points if p.name == name]

    def raised(self) -> list[BaseException]:
        fakes: list[_Faulty | None] = [
            self.llm, self.emb, self.corpus, self.store, self.secondary, self.reranker, self.auth,
        ]
        return [exc for fake in fakes if fake is not None for exc in fake.raised]


_ABSENT = object()
_DM = SessionData(user_id=1, role="dm")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_API_BASE", "RAG_TRACING", "LANGSMITH_TRACING",
                 "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(generate_module, "_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(retrieval, "_EMBED_RETRY_BACKOFF_S", 0)
    # Process-wide: never write through a ledger or job driver another test leaked.
    assert "ledger" not in service_app._state
    assert "jobs" not in service_app._state


@pytest.fixture
def post_chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., ChatRun]]:
    """A `post_chat(...)` callable wiring a real RagService over the fakes."""
    assert not generate_module._looks_like_statblock(FAKE_ANSWER)  # else sage/gm make a 2nd LLM call
    touched: dict[Callable[..., object], Callable[..., object] | None] = {}
    prior_sink = getattr(app.state, "metrics_sink", _ABSENT)

    def override(dependency: Callable[..., object], value: Callable[..., object]) -> None:
        if dependency not in touched:
            touched[dependency] = app.dependency_overrides.get(dependency)
        app.dependency_overrides[dependency] = value

    def run(
        prompt: str = PROSE, *, mode: str = "sage", rows: list[ChunkRow] | None = None,
        corpus_fail: dict[str, BaseException] | None = None, fail_connect_on: int | None = None,
        emb_exc: BaseException | None = None, llm_exc: BaseException | None = None,
        reranker: CountingReranker | None = None, secondary: FakeSecondary | None = None,
        store: ProbedStore | None = None, attachment: bool = False, real_embed_client: bool = False,
        model_preference: str | None = None, session: SessionData | None = _DM,
        auth: FakeAuthStore | None = None, headers: dict[str, str] | None = None,
    ) -> ChatRun:
        corpus = FakeCorpus(rows or [], fail=corpus_fail, fail_connect_on=fail_connect_on)
        emb = FakeEmbeddings(emb_exc)
        if not real_embed_client:
            monkeypatch.setattr(retrieval, "_openai_client", lambda: emb)
        llm = FakeChatModel(llm_exc)
        secondary = secondary or FakeSecondary()
        svc = RagService(retriever=retrieval.RagRetriever(connect=corpus.connect), reranker=reranker,
                         llm_client=llm, secondary_retriever=secondary)
        assert svc.factory.client_for(svc.model) is llm
        store = store if store is not None else ProbedStore()
        if attachment:
            store.append_attachment(CONV, ATTACHMENT_NAME, "text/plain", ATTACHMENT_TEXT)
        store.calls.clear()
        sink = RecordingSink()
        app.state.metrics_sink = sink
        override(service_app.get_service, lambda: svc)
        override(service_app.get_message_store, lambda: store)
        override(service_app.get_timeline_store, lambda: None)
        override(service_app.get_timeline_database, lambda: None)
        if session is None:
            assert require_session not in app.dependency_overrides, "mark the case real_auth"
            override(service_app.get_auth_store, lambda: auth)
        elif session != _DM:
            override(require_session, lambda: session)
        payload: dict[str, str] = {"prompt": prompt, "mode": mode, "conversation_id": CONV}
        if model_preference is not None:
            payload["model_preference"] = model_preference
        response = TestClient(app, raise_server_exceptions=False).post("/chat", json=payload, headers=headers)
        return ChatRun(response, prompt, svc, llm, emb, corpus, store, sink, reranker, secondary, auth)

    yield run
    # Only the keys this fixture set, each back to what it was (never .clear()).
    for dependency, previous in touched.items():
        if previous is None:
            app.dependency_overrides.pop(dependency, None)
        else:
            app.dependency_overrides[dependency] = previous
    if prior_sink is _ABSENT:
        if hasattr(app.state, "metrics_sink"):
            del app.state.metrics_sink
    else:
        app.state.metrics_sink = prior_sink


# ── Shared assertions ─────────────────────────────────────────────────────────


def project(source: object) -> dict[str, object]:
    """A source on its existing fields only: an additive field cannot break a pin."""
    assert isinstance(source, dict)
    return {name: source.get(name) for name in _SOURCE_FIELDS}


def assert_answer(
    run: ChatRun, *, answer: str, answerable: bool, sources: list[dict[str, object]], mode: str,
) -> None:
    assert run.response.status_code == 200, run.response.text
    body = run.body
    raw_sources = body["sources"]
    assert isinstance(raw_sources, list)
    pinned = {key: body[key] for key in _PINNED_KEYS} | {"sources": [project(s) for s in raw_sources]}
    assert pinned == {
        "answer": answer, "sources": sources, "answerable": answerable, "mode": mode,
        "conversation_id": CONV, "suggestions": None, "spell_content": None, "stat_block": None,
    }


def assert_refusal(run: ChatRun, mode: str) -> None:
    assert REFUSAL == DOCUMENTED_REFUSAL
    assert_answer(run, answer=REFUSAL, answerable=False, sources=[], mode=mode)
    assert run.llm.calls == 0
    assert run.rows() == [("user", run.prompt), ("assistant", REFUSAL)]


def assert_gate_metric(run: ChatRun, value: bool, mode: str) -> None:
    points = run.points("service.chat.gate.answerable")
    assert [(p.value, p.labels.mode) for p in points] == [(value, mode)]


def assert_failure_metrics(run: ChatRun, category: str) -> None:
    assert [p.value for p in run.points("service.chat.error")] == [True]
    assert [p.value for p in run.points("service.chat.error_category")] == [category]
    assert run.points("service.chat.gate.answerable") == []


def _d9_words(svc: RagService) -> set[str]:
    profile = get_profile(DEFAULT_ALIAS)
    assert profile is not None
    return {"openai", DEFAULT_ALIAS, profile.display_name, profile.api_model, svc.model,
            retrieval.EMBED_MODEL, "OPENAI_API_KEY"}


def assert_content_free(run: ChatRun, *, fault: bool) -> None:
    """The error body and headers carry no prompt, fault text, model or secret name."""
    if fault:
        raised = run.raised()
        assert raised, "the injected fault never fired"
        assert all(CANARY in str(exc) for exc in raised)
    if run.prompt:
        assert CANARY in run.prompt
    haystack = run.response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in run.response.headers.items())
    assert CANARY not in haystack
    for word in _d9_words(run.svc):
        assert word.lower() not in haystack.lower(), word


def assert_no_spend(run: ChatRun) -> None:
    assert (run.emb.calls, run.searches, run.llm.calls, run.rows()) == (0, 0, 0, [])


def _status_error(cls: type[openai.APIStatusError], code: int, **headers: str) -> openai.APIStatusError:
    response = httpx.Response(code, request=_EMBED_REQUEST, headers=headers)
    return cls(f"provider refused {CANARY}", response=response, body=None)


def _rate_limit() -> openai.APIStatusError:
    return _status_error(openai.RateLimitError, 429, **{"retry-after": "7"})


def _timeout() -> openai.APITimeoutError:
    exc = openai.APITimeoutError(request=_EMBED_REQUEST)
    exc.args = (f"Request timed out. {CANARY}",)
    return exc


def _d4(category: str, retryable: bool) -> dict[str, object]:
    """The D4 provider-error body, as the generation branch answers it."""
    return {"detail": {"category": category, "retryable": retryable,
                       "message": service_app._ERROR_DETAIL[category]}}


def assert_logs_content_free(caplog: pytest.LogCaptureFixture, *present: str) -> None:
    """The log lines name the class and the stage or category, never the canary."""
    for word in present:
        assert word in caplog.text, word
    assert CANARY not in caplog.text


# ── Grounded routes and the strict gate ───────────────────────────────────────


def test_grounded_hit_generates_with_corpus_sources(post_chat: Callable[..., ChatRun]) -> None:
    run = post_chat(PROSE, rows=HIT)
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(r) for r in HIT], mode="sage")
    assert (run.llm.calls, run.emb.calls, run.searches) == (1, 1, 1)
    assert run.rows() == [("user", PROSE), ("assistant", FAKE_ANSWER)]
    assert_gate_metric(run, True, "sage")


@pytest.mark.parametrize("mode", ["sage", "spell", "rules"])
def test_strict_modes_refuse_a_corpus_miss(post_chat: Callable[..., ChatRun], mode: str) -> None:
    run = post_chat(PROSE, mode=mode, rows=MISS)
    assert_refusal(run, mode)
    assert (run.emb.calls, run.searches) == (1, 1)
    assert_gate_metric(run, False, mode)


@pytest.mark.parametrize(("distance", "generates"), [(K, True), (JUST_ABOVE, False)],
                         ids=["at_threshold", "just_above"])
def test_strict_gate_threshold_is_inclusive(
    post_chat: Callable[..., ChatRun], distance: float, generates: bool,
) -> None:
    row = chunk("char-c1", distance, "Basilisk")
    run = post_chat(PROSE, rows=[row])
    if generates:
        assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(row)], mode="sage")
        assert run.llm.calls == 1
    else:
        assert_refusal(run, "sage")


@pytest.mark.parametrize("prompt", ["   ", "\n\t"], ids=["spaces", "newline_tab"])
def test_blank_prompt_refuses_before_any_retrieval(post_chat: Callable[..., ChatRun], prompt: str) -> None:
    run = post_chat(prompt, rows=HIT)
    assert_refusal(run, "sage")
    assert (run.emb.calls, run.searches) == (0, 0)
    assert_gate_metric(run, False, "sage")


def test_empty_prompt_is_rejected_before_any_retrieval(post_chat: Callable[..., ChatRun]) -> None:
    run = post_chat("", rows=HIT)
    assert run.response.status_code == 422
    assert_no_spend(run)
    assert_content_free(run, fault=False)


# ── GM mode ───────────────────────────────────────────────────────────────────


def test_gm_generates_on_weak_chunks_and_reports_unanswerable(post_chat: Callable[..., ChatRun]) -> None:
    run = post_chat(PROSE, mode="gm", rows=MISS)
    assert_answer(run, answer=FAKE_ANSWER, answerable=False, sources=[corpus_source(r) for r in MISS], mode="gm")
    assert run.llm.calls == 1
    assert_gate_metric(run, False, "gm")


def test_gm_refuses_when_retrieval_returns_nothing(post_chat: Callable[..., ChatRun]) -> None:
    secondary = FakeSecondary()
    run = post_chat(PROSE, mode="gm", rows=[], secondary=secondary)
    assert_refusal(run, "gm")
    assert secondary.calls == 1  # the fan-out ran and found nothing either
    assert_gate_metric(run, False, "gm")


def test_gm_counts_secondary_chunks_before_the_gate(post_chat: Callable[..., ChatRun]) -> None:
    found = SecondaryResult(chunks=[SECONDARY_CHUNK], full_texts={"char-w1": full_text("char-w1")},
                            book_by_id={"char-w1": SECONDARY_BOOK}, answerable=True)
    secondary = FakeSecondary(found)
    run = post_chat(PROSE, mode="gm", rows=[], secondary=secondary)
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[SECONDARY_SOURCE], mode="gm")
    assert (run.llm.calls, secondary.calls) == (1, 1)
    assert_gate_metric(run, True, "gm")


# ── Attachments ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rows", [MISS, []], ids=["weak_chunks", "no_chunks"])
@pytest.mark.parametrize("mode", ["sage", "spell", "rules", "gm"])
def test_attachment_bypasses_the_gate(post_chat: Callable[..., ChatRun], mode: str, rows: list[ChunkRow]) -> None:
    run = post_chat(PROSE, mode=mode, rows=rows, attachment=True)
    # The weak chunks of a miss are still cited next to the attachment (N-3).
    expected_sources = [corpus_source(r) for r in rows] + [ATTACHMENT_SOURCE]
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=expected_sources, mode=mode)
    first_human = [m for m in run.llm.messages[0] if isinstance(m, HumanMessage)]
    assert len(first_human) == 1
    assert ATTACHMENT_TEXT in str(first_human[0].content)
    assert run.rows() == [("user", PROSE), ("assistant", FAKE_ANSWER)]
    assert_gate_metric(run, True, mode)


def test_attachment_fetch_failure_falls_back_to_the_strict_refusal(post_chat: Callable[..., ChatRun]) -> None:
    store = ProbedStore(faults={"attachments_for": db_down("attachment fetch")})
    run = post_chat(PROSE, rows=MISS, store=store, attachment=True)
    # 200, not 503: the bypass is silently lost and the turn is a corpus refusal (N-4).
    assert_refusal(run, "sage")
    assert store.calls["attachments_for"] == 1
    assert_gate_metric(run, False, "sage")


def test_attachment_fetch_failure_does_not_fail_a_grounded_answer(post_chat: Callable[..., ChatRun]) -> None:
    store = ProbedStore(faults={"attachments_for": db_down("attachment fetch")})
    run = post_chat(PROSE, rows=HIT, store=store, attachment=True)
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(r) for r in HIT], mode="sage")
    assert store.calls["attachments_for"] == 1


# ── History writes ────────────────────────────────────────────────────────────

_HISTORY_FAULTS = [
    pytest.param(frozenset({"user", "assistant"}), [], id="fail_both"),
    pytest.param(frozenset({"user"}), [("assistant", FAKE_ANSWER)], id="fail_user_only"),
    pytest.param(frozenset({"assistant"}), [("user", PROSE)], id="fail_assistant_only"),
]


@pytest.mark.parametrize(("roles", "rows_left"), _HISTORY_FAULTS)
def test_history_write_failure_is_not_reported_as_a_retrieval_failure(
    post_chat: Callable[..., ChatRun], roles: frozenset[str], rows_left: list[tuple[str, str]],
) -> None:
    store = ProbedStore(append_fault_roles=roles)
    run = post_chat(PROSE, rows=HIT, store=store)
    assert run.response.status_code != 503
    assert "retrieval backend unavailable" not in run.response.text
    assert "embedding backend unavailable" not in run.response.text
    assert run.body.get("answer") != REFUSAL
    assert_gate_metric(run, True, "sage")
    assert all(store.calls[f"append:{role}"] == 1 for role in roles)


@pytest.mark.parametrize(("roles", "rows_left"), _HISTORY_FAULTS)
def test_history_write_failure_does_not_fail_the_answer(
    post_chat: Callable[..., ChatRun], roles: frozenset[str], rows_left: list[tuple[str, str]],
) -> None:
    store = ProbedStore(append_fault_roles=roles)
    run = post_chat(PROSE, rows=HIT, store=store)
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(r) for r in HIT], mode="sage")
    assert run.rows() == rows_left
    assert all(store.calls[f"append:{role}"] == 1 for role in roles)


# ── Reranker ──────────────────────────────────────────────────────────────────


def test_reranker_reorders_prose_queries(post_chat: Callable[..., ChatRun]) -> None:
    reranker = CountingReranker()
    run = post_chat(PROSE, rows=HIT, reranker=reranker)
    reordered = list(reversed(HIT))
    assert reordered != HIT
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(r) for r in reordered],
                  mode="sage")
    assert reranker.calls == 1


def test_reranker_skips_structured_queries(post_chat: Callable[..., ChatRun]) -> None:
    assert "monster" in rerank_module.SKIP_RERANK_CTYPES
    reranker = CountingReranker()
    run = post_chat(STRUCTURED, rows=HIT, reranker=reranker)
    assert run.svc.reranker is reranker
    assert reranker.calls == 0
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(r) for r in HIT], mode="sage")
    control = post_chat(PROSE, rows=HIT, reranker=reranker)
    assert control.response.status_code == 200
    assert reranker.calls == 1


def test_answerability_is_decided_before_rerank(post_chat: Callable[..., ChatRun]) -> None:
    rows = [chunk("char-c1", NEAR, "Basilisk"), chunk("char-c2", FAR, "Cockatrice")]
    run = post_chat(PROSE, rows=rows, reranker=CountingReranker())
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(r) for r in reversed(rows)],
                  mode="sage")
    assert run.llm.calls == 1


@pytest.mark.parametrize("exc_type", [RuntimeError, OSError])
def test_reranker_failure_degrades_to_the_vector_order(
    post_chat: Callable[..., ChatRun], caplog: pytest.LogCaptureFixture, exc_type: type[Exception],
) -> None:
    # Flipped by xiu.2.3 from a 500: ranking is garnish and the evidence is intact.
    caplog.set_level(logging.WARNING)
    reranker = CountingReranker(exc_type(f"cross-encoder failed {CANARY}"))
    run = post_chat(FAILING_PROMPT, rows=HIT, reranker=reranker)
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(r) for r in HIT], mode="sage")
    assert (reranker.calls, run.llm.calls) == (1, 1)
    assert run.rows() == [("user", FAILING_PROMPT), ("assistant", FAKE_ANSWER)]
    assert_gate_metric(run, True, "sage")
    assert [CANARY in str(exc) for exc in reranker.raised] == [True]
    assert_logs_content_free(caplog, "stage=rerank", f"error={exc_type.__name__}")


class FixedOrderReranker(CountingReranker):
    """Answers a fixed order, a permutation or not."""

    def __init__(self, order: list[int]) -> None:
        super().__init__()
        self.order = order

    def rerank(self, query: str, texts: list[str]) -> list[int]:
        self._fire()
        return list(self.order)


def test_an_invalid_rerank_order_keeps_the_vector_order(
    post_chat: Callable[..., ChatRun], caplog: pytest.LogCaptureFixture,
) -> None:
    # A duplicate index used to answer 200 with the Cockatrice chunk silently gone (V-8).
    caplog.set_level(logging.WARNING)
    reranker = FixedOrderReranker([0, 0])
    run = post_chat(PROSE, rows=HIT, reranker=reranker)
    assert_answer(run, answer=FAKE_ANSWER, answerable=True, sources=[corpus_source(r) for r in HIT], mode="sage")
    assert (reranker.calls, run.llm.calls) == (1, 1)
    assert_logs_content_free(caplog, "stage=rerank", "error=RerankOrderInvalid")


# ── Embedding failures ────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", [None, "sk-replace-me"], ids=["unset", "placeholder"])
def test_missing_embedding_key_is_a_503(
    post_chat: Callable[..., ChatRun], monkeypatch: pytest.MonkeyPatch, key: str | None,
) -> None:
    if key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    run = post_chat(FAILING_PROMPT, rows=HIT, real_embed_client=True)
    assert (run.response.status_code, run.response.json()) == (503, {"detail": "embedding backend unavailable"})
    assert (run.emb.calls, run.searches, run.llm.calls, run.rows()) == (0, 0, 0, [])
    assert_failure_metrics(run, "dependency")
    assert_content_free(run, fault=False)


_EMBED_ERRORS = [
    pytest.param(_rate_limit, retrieval.EMBED_MAX_ATTEMPTS, 503, "dependency", id="rate_limit"),
    pytest.param(_timeout, retrieval.EMBED_MAX_ATTEMPTS, 503, "dependency", id="timeout"),
    pytest.param(lambda: openai.APIConnectionError(message=f"Connection error. {CANARY}", request=_EMBED_REQUEST),
                 retrieval.EMBED_MAX_ATTEMPTS, 503, "dependency", id="connection"),
    pytest.param(lambda: _status_error(openai.InternalServerError, 500),
                 retrieval.EMBED_MAX_ATTEMPTS, 503, "dependency", id="server_error"),
    pytest.param(lambda: _status_error(openai.AuthenticationError, 401), 1, 503, "dependency", id="authentication"),
    pytest.param(lambda: _status_error(openai.PermissionDeniedError, 403), 1, 503, "dependency", id="permission"),
    pytest.param(lambda: _status_error(openai.BadRequestError, 400), 1, 422, "validation", id="bad_request"),
]


@pytest.mark.parametrize(("make_exc", "embed_calls", "status", "metric"), _EMBED_ERRORS)
def test_embedding_api_errors_are_typed_embed_faults(
    post_chat: Callable[..., ChatRun], make_exc: Callable[[], BaseException], embed_calls: int, status: int,
    metric: str,
) -> None:
    # Flipped by xiu.2.3 from the generation branch's 429/502: an embed fault is
    # the embedding backend's 503, or the existing 422 when the provider rejects
    # the request (a retry cannot help it). The service-owned embed retry tries
    # a transient fault EMBED_MAX_ATTEMPTS times, anything else once.
    run = post_chat(FAILING_PROMPT, rows=HIT, emb_exc=make_exc())
    body = _EMBED_DOWN if status == 503 else _d4("invalid_request", False)
    assert (run.response.status_code, run.response.json()) == (status, body)
    cause = run.emb.raised[0]
    if isinstance(cause, openai.RateLimitError):
        assert cause.response.headers["retry-after"] == "7"  # on the cause, never forwarded
    assert "retry-after" not in run.response.headers
    assert "x-chat-throttled" not in run.response.headers
    assert (run.emb.calls, run.searches, run.llm.calls, run.rows()) == (embed_calls, 0, 0, [])
    assert_failure_metrics(run, metric)
    assert_content_free(run, fault=True)
    if isinstance(cause, openai.AuthenticationError):
        # Committed before the handler's try: the failed turn leaves them in place.
        assert run.store.owner_of(CONV) == 1
        assert run.store.conversation_strategy(CONV) == ("auto", None)


@pytest.mark.parametrize(("make_exc", "embed_calls", "llm_calls", "at_generation"), [
    pytest.param(lambda: _status_error(openai.AuthenticationError, 401), 1, 1,
                 (502, _d4("authentication", False), None), id="authentication"),
    pytest.param(_rate_limit, retrieval.EMBED_MAX_ATTEMPTS, generate_module._MAX_ATTEMPTS,
                 (429, _d4("rate_limit", True), "7"), id="rate_limit"),
])
def test_embedding_and_generation_failures_are_distinguishable(
    post_chat: Callable[..., ChatRun], make_exc: Callable[[], BaseException], embed_calls: int, llm_calls: int,
    at_generation: tuple[int, dict[str, object], str | None],
) -> None:
    # Flipped by xiu.2.3: the same class at embed and at generation used to answer identically.
    embed_run = post_chat(FAILING_PROMPT, rows=HIT, emb_exc=make_exc())
    generation_run = post_chat(FAILING_PROMPT, rows=HIT, llm_exc=make_exc())
    assert (embed_run.emb.calls, embed_run.llm.calls) == (embed_calls, 0)
    assert (generation_run.emb.calls, generation_run.llm.calls) == (1, llm_calls)
    seen = [(r.response.status_code, r.response.json(), r.response.headers.get("retry-after"))
            for r in (embed_run, generation_run)]
    assert seen[0] == (503, _EMBED_DOWN, None)
    assert seen[1] == at_generation  # generation keeps its D4 answer exactly
    assert seen[0] != seen[1]
    for run in (embed_run, generation_run):
        assert_content_free(run, fault=True)


def test_the_service_embedding_client_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flipped by xiu.2.3 from the SDK defaults (2 retries, 600 s waits): no SDK
    # retries, since RagRetriever.embed owns the attempts, and every wait bounded.
    monkeypatch.setenv("OPENAI_API_KEY", "characterization-dummy-key")
    client = retrieval._openai_client()
    assert isinstance(client, openai.OpenAI)
    assert client.max_retries == 0
    assert client.timeout == httpx.Timeout(config.EMBED_REQUEST_TIMEOUT_S, connect=config.EMBED_CONNECT_TIMEOUT_S)
    assert client.timeout != openai.DEFAULT_TIMEOUT


# ── PostgreSQL retrieval failures ─────────────────────────────────────────────

_DB_FAULTS = [
    pytest.param({"search": db_down("vector search")}, None, "search", True, id="search_operational"),
    pytest.param({"search": psycopg.errors.UndefinedTable(f"relation missing {CANARY}")}, None, "search", False,
                 id="search_undefined_table"),
    pytest.param({"fetch": db_down("chunk details")}, None, "fetch", False, id="fetch_operational"),
    pytest.param(None, 2, None, False, id="gate_pool_timeout"),
]


@pytest.mark.parametrize(("fail", "fail_connect_on", "attempted", "check_binding"), _DB_FAULTS)
def test_retrieval_database_errors_are_a_503(
    post_chat: Callable[..., ChatRun], fail: dict[str, BaseException] | None, fail_connect_on: int | None,
    attempted: str | None, check_binding: bool,
) -> None:
    run = post_chat(FAILING_PROMPT, rows=HIT, corpus_fail=fail, fail_connect_on=fail_connect_on)
    assert (run.response.status_code, run.response.json()) == (503, _RETRIEVAL_DOWN)
    assert (run.emb.calls, run.llm.calls, run.rows()) == (1, 0, [])
    if attempted is None:
        assert (run.corpus.connects, run.searches) == (2, 0)  # the search connect was refused
    else:
        assert run.corpus.count(attempted) == 1
    assert_failure_metrics(run, "dependency")
    assert_content_free(run, fault=True)
    if check_binding:
        # Committed before the handler's try: the failed turn leaves them in place.
        assert run.store.owner_of(CONV) == 1
        assert run.store.conversation_strategy(CONV) == ("auto", None)


@pytest.mark.parametrize(("exc", "status", "body", "metric"), [
    pytest.param(db_down("world corpus"), 503, _RETRIEVAL_DOWN, "dependency", id="psycopg"),
    pytest.param(RuntimeError(f"world corpus bug {CANARY}"), 500, _INTERNAL, "handler", id="runtime"),
])
def test_gm_secondary_failure_fails_the_turn(
    post_chat: Callable[..., ChatRun], exc: BaseException, status: int, body: dict[str, str], metric: str,
) -> None:
    secondary = FakeSecondary(exc=exc)
    run = post_chat(FAILING_PROMPT, mode="gm", rows=HIT, secondary=secondary)
    assert (run.response.status_code, run.response.json()) == (status, body)
    assert (secondary.calls, run.llm.calls, run.rows()) == (1, 0, [])
    assert_failure_metrics(run, metric)
    assert_content_free(run, fault=True)


# ── Pre-retrieval gates: fail closed, spend nothing ───────────────────────────


@dataclass
class Gate:
    status: int
    body: object = None                                          # exact JSON, when pinned
    headers: dict[str, str | None] = field(default_factory=dict)  # None: must be absent
    probe: Callable[[], int] | None = None                       # the check that must have run once
    budget: int | None = None                                    # check_chat_request calls, where documented
    fault: bool = False                                          # an injected exception carries the canary


_GATE_CASES = [
    pytest.param("unauthenticated", marks=pytest.mark.real_auth, id="unauthenticated"),
    pytest.param("session_backend_down", marks=pytest.mark.real_auth, id="session_backend_down"),
    "foreign_conversation", "ownership_backend_down", "daily_cap_backend_down", "daily_cap_reached",
    "per_user_throttle", "over_length", "nul_in_prompt", "strategy_conflict", "unknown_model",
    "control_reaches_retrieval",
]


def _arrange_gate(
    case: str, monkeypatch: pytest.MonkeyPatch, store: ProbedStore, budget: CountingBudget,
) -> tuple[Gate, dict[str, object]]:
    kwargs: dict[str, object] = {}
    if case == "unauthenticated":
        auth = FakeAuthStore()
        kwargs.update(session=None, auth=auth)
        return Gate(401, {"detail": "authentication required"}, budget=0), kwargs
    if case == "session_backend_down":
        assert len(_SESSION_SECRET) >= service_app.MIN_SESSION_SECRET_LENGTH
        assert _SESSION_SECRET.lower() not in service_app._PLACEHOLDER_SESSION_SECRETS
        monkeypatch.setattr(config, "SESSION_SECRET", _SESSION_SECRET)
        auth = FakeAuthStore(db_down("account lookup"))
        token = encode_session(SessionData(user_id=1, role="dm"), _SESSION_SECRET)
        kwargs.update(session=None, auth=auth, headers={"cookie": f"{config.SESSION_COOKIE_NAME}={token}"})
        return Gate(503, {"detail": "auth backend unavailable"}, budget=0, probe=lambda: auth.calls,
                    fault=True), kwargs
    if case == "foreign_conversation":
        store.claim_conversation(CONV, 2)
        return Gate(403, {"detail": "not your conversation"},
                    probe=lambda: store.calls["claim_conversation"]), kwargs
    if case == "ownership_backend_down":
        store.faults["claim_conversation"] = db_down("ownership")
        return Gate(503, {"detail": "authorization backend unavailable"},
                    probe=lambda: store.calls["claim_conversation"], fault=True), kwargs
    if case == "daily_cap_backend_down":
        store.faults["calls_today"] = db_down("usage count")
        return Gate(503, {"detail": "usage backend unavailable"},
                    probe=lambda: store.calls["calls_today"], fault=True), kwargs
    if case == "daily_cap_reached":
        monkeypatch.setattr(config, "CHAT_DAILY_CAP", 0)
        return Gate(429, headers={"x-chat-throttled": "daily"}, probe=lambda: store.calls["calls_today"]), kwargs
    if case == "per_user_throttle":
        budget.exc = RateLimited(7)
        return Gate(429, headers={"retry-after": "7", "x-chat-throttled": "user"}, probe=lambda: budget.calls), kwargs
    if case == "over_length":
        kwargs["prompt"] = CANARY + "x" * (CHAT_TEXT_MAX_CHARS + 1)
        return Gate(422, {"detail": f"prompt exceeds the {CHAT_TEXT_MAX_CHARS}-character limit"}), kwargs
    if case == "nul_in_prompt":
        kwargs["prompt"] = CANARY + chr(0)
        return Gate(422, budget=0), kwargs
    if case == "strategy_conflict":
        store.claim_conversation(CONV, 1)
        store.claim_conversation_strategy(CONV, strategy="manual", manual_alias=DEFAULT_ALIAS,
                                          catalog_revision=CATALOG_REVISION)
        detail = ("this conversation is bound to a different model preference; "
                  "start a new conversation to change it")
        return Gate(409, {"detail": detail}, probe=lambda: store.calls["claim_conversation_strategy"]), kwargs
    if case == "unknown_model":
        # Never the canary here: this body echoes the requested id by design.
        kwargs["model_preference"] = "characterization-unknown-model"
        return Gate(422), kwargs
    assert case == "control_reaches_retrieval"
    return Gate(200, budget=1), kwargs


@pytest.mark.parametrize("case", _GATE_CASES)
def test_pre_retrieval_gates_fail_closed_without_spending_retrieval(
    post_chat: Callable[..., ChatRun], monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    store = ProbedStore()
    budget = CountingBudget(service_app.check_chat_request)
    monkeypatch.setattr(service_app, "check_chat_request", budget)
    gate, kwargs = _arrange_gate(case, monkeypatch, store, budget)
    prompt = kwargs.pop("prompt", FAILING_PROMPT)
    run = post_chat(prompt, rows=HIT, store=store, **kwargs)
    assert run.response.status_code == gate.status, run.response.text
    if gate.budget is not None:
        assert budget.calls == gate.budget
    if case == "control_reaches_retrieval":
        # Without this positive control the zero counts below could not fail.
        assert (run.emb.calls, run.searches, run.llm.calls) == (1, 1, 1)
        return
    assert_no_spend(run)
    if gate.body is not None:
        assert run.response.json() == gate.body
    for name, value in gate.headers.items():
        assert run.response.headers.get(name) == value
    if gate.probe is not None:
        assert gate.probe() == 1
    elif run.auth is not None:
        assert run.auth.calls == 0  # no cookie: the account is never looked up
    if case == "nul_in_prompt":
        detail = run.response.json()["detail"]
        assert [entry["loc"] for entry in detail] == [["body", "prompt"]]
        assert all("input" not in entry for entry in detail)
    assert_content_free(run, fault=gate.fault)


def test_gm_channel_refuses_a_non_dm_session_before_retrieval(post_chat: Callable[..., ChatRun]) -> None:
    run = post_chat(FAILING_PROMPT, mode="gm", rows=HIT, session=SessionData(user_id=1, role="player"))
    assert (run.response.status_code, run.response.json()) == (403, {"detail": "the GM channel requires the DM role"})
    assert_no_spend(run)
    assert_content_free(run, fault=False)
    # T-23b (agent-forge-harness-816): assert_no_spend alone doesn't cover
    # ownership/strategy, since neither touches embed/search/llm counts or
    # message rows. A refused GM call must claim no ownership and bind no
    # strategy (mutant R-3: moving this gate below `_authorize_conversation` /
    # `claim_conversation_strategy` in service/app.py survives without this).
    assert run.store.owner_of(CONV) is None
    assert run.store.conversation_strategy(CONV) is None
    assert run.store.calls["claim_conversation"] == 0


def test_per_user_throttle_refuses_a_chat_call_before_ownership_or_strategy(
    post_chat: Callable[..., ChatRun], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent-forge-harness-74x (PR #165 review, M9 survivor): the "per_user_throttle"
    row of test_pre_retrieval_gates_fail_closed_without_spending_retrieval only
    checks assert_no_spend, which -- like the GM-channel case above (T-23b) --
    never touches ownership or strategy. A throttled (429) call must claim no
    ownership and bind no strategy too (mutant M9: moving `_throttle_chat` below
    `_authorize_conversation` / `claim_conversation_strategy` in service/app.py
    survives the full unit suite without this)."""
    budget = CountingBudget(service_app.check_chat_request, exc=RateLimited(7))
    monkeypatch.setattr(service_app, "check_chat_request", budget)
    run = post_chat(FAILING_PROMPT, rows=HIT)
    assert run.response.status_code == 429
    assert run.response.headers.get("retry-after") == "7"
    assert run.response.headers.get("x-chat-throttled") == "user"
    assert budget.calls == 1
    assert_no_spend(run)
    assert_content_free(run, fault=False)
    assert run.store.owner_of(CONV) is None
    assert run.store.conversation_strategy(CONV) is None
    assert run.store.calls["claim_conversation"] == 0


def _strategy_claim_outage(post_chat: Callable[..., ChatRun]) -> ChatRun:
    store = ProbedStore(faults={"claim_conversation_strategy": db_down("strategy binding")})
    run = post_chat(FAILING_PROMPT, rows=HIT, store=store)
    assert store.calls["claim_conversation_strategy"] == 1
    return run


def test_strategy_claim_failure_spends_nothing(post_chat: Callable[..., ChatRun]) -> None:
    run = _strategy_claim_outage(post_chat)
    assert run.response.status_code >= 500
    assert_no_spend(run)
    assert_content_free(run, fault=True)


def test_strategy_claim_failure_status(post_chat: Callable[..., ChatRun]) -> None:
    # Flipped by xiu.2.3 (N-6) from Starlette's raw 500: a handled 503, so the
    # response carries a JSON body, the security headers and the chat metrics.
    run = _strategy_claim_outage(post_chat)
    assert (run.response.status_code, run.response.json()) == (503, _ROUTING_DOWN)
    assert "content-security-policy" in run.response.headers
    assert_failure_metrics(run, "dependency")
    assert_content_free(run, fault=True)


RETIRED_ALIAS = "kimi-k3"  # disabled, with no configured successor: a binding to it heals to auto


@pytest.mark.parametrize("method", ["conversation_binding", "claim_conversation_strategy",
                                    "rebind_conversation_strategy"], ids=["binding", "claim", "rebind"])
def test_a_routing_store_outage_fails_closed_with_a_handled_503(
    post_chat: Callable[..., ChatRun], caplog: pytest.LogCaptureFixture, method: str,
) -> None:
    caplog.set_level(logging.WARNING)
    store = ProbedStore()
    if method == "rebind_conversation_strategy":
        # A D-9-era manual pick the catalog has since retired; posting "auto",
        # the successor the server names, makes the turn rebind (j9w).
        assert get_profile(RETIRED_ALIAS) is None
        store.claim_conversation(CONV, 1)
        store.claim_conversation_strategy(CONV, strategy="manual", manual_alias=RETIRED_ALIAS,
                                          catalog_revision=CATALOG_REVISION)
    store.faults[method] = db_down("conversation routing")
    run = post_chat(FAILING_PROMPT, rows=HIT, store=store)
    assert store.calls[method] == 1  # the faulted call ran (for rebind: the heal branch was reached)
    assert (run.response.status_code, run.response.json()) == (503, _ROUTING_DOWN)
    assert "content-security-policy" in run.response.headers
    assert_failure_metrics(run, "dependency")
    assert_no_spend(run)
    assert_content_free(run, fault=True)
    assert_logs_content_free(caplog, "conversation routing store unavailable", "error=OperationalError")


_LOG_CASES = [
    pytest.param(lambda: {"llm_exc": _rate_limit()}, 429, ("error=RateLimitError", "category=rate_limit"),
                 id="generation"),
    pytest.param(lambda: {"emb_exc": _timeout()}, 503, ("error=APITimeoutError", "stage=embed"), id="embed_typed"),
    pytest.param(lambda: {"corpus_fail": {"search": db_down("vector search")}}, 503,
                 ("error=OperationalError", "stage=vector_search"), id="corpus_typed"),
]


@pytest.mark.parametrize(("arrange", "status", "present"), _LOG_CASES)
def test_chat_error_logs_carry_the_class_never_the_message(
    post_chat: Callable[..., ChatRun], caplog: pytest.LogCaptureFixture,
    arrange: Callable[[], dict[str, object]], status: int, present: tuple[str, ...],
) -> None:
    caplog.set_level(logging.WARNING)
    run = post_chat(FAILING_PROMPT, rows=HIT, **arrange())
    assert run.response.status_code == status
    assert_content_free(run, fault=True)  # the fault fired, and it carried the canary
    assert_logs_content_free(caplog, *present)


# ── The registries themselves ─────────────────────────────────────────────────


def test_characterization_registries_name_real_tests() -> None:
    defined = {name for name, obj in globals().items()
               if name.startswith("test_") and callable(obj) and getattr(obj, "__module__", None) == __name__}
    registered = FLAG_DEFAULT | set(SLATED)
    assert registered <= defined, sorted(registered - defined)
    assert not FLAG_DEFAULT & set(SLATED)
    assert all(_SLATED_VALUE.match(owner) for owner in SLATED.values()), SLATED
    # The split pins: the slated halves are owned as recorded, the invariant halves are unregistered.
    assert SLATED["test_gm_channel_refuses_a_non_dm_session_before_retrieval"].startswith("agent-forge-harness-ubw: ")
    assert SLATED["test_history_write_failure_does_not_fail_the_answer"].startswith("agent-forge-harness-ul21: ")
    # After xiu.2.3 PR-2: what the flag-free changes flipped, and what its PR-3 still owns.
    assert {"test_embedding_api_errors_are_typed_embed_faults",
            "test_embedding_and_generation_failures_are_distinguishable"} <= FLAG_DEFAULT
    assert {name for name, owner in SLATED.items() if owner.startswith("agent-forge-harness-xiu.2.3: ")} == {
        "test_missing_embedding_key_is_a_503", "test_retrieval_database_errors_are_a_503",
        "test_gm_secondary_failure_fails_the_turn",
    }
    for invariant in ("test_history_write_failure_is_not_reported_as_a_retrieval_failure",
                      "test_strategy_claim_failure_spends_nothing",
                      "test_strategy_claim_failure_status",
                      "test_pre_retrieval_gates_fail_closed_without_spending_retrieval",
                      "test_the_service_embedding_client_is_bounded",
                      "test_reranker_failure_degrades_to_the_vector_order",
                      "test_an_invalid_rerank_order_keeps_the_vector_order",
                      "test_a_routing_store_outage_fails_closed_with_a_handled_503",
                      "test_chat_error_logs_carry_the_class_never_the_message"):
        assert invariant in defined
        assert invariant not in registered

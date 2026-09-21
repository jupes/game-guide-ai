"""
Usage capture driven through the REAL compiled graph
(agent-forge-harness-yje.5.1.1).

Why this file exists at all: the single most likely way to ship this feature
green-but-untrue is to wire a capture point through a code path no test ever
executes. Every retriever fake in this suite returns a canned vector, so
`ingestion.retrieval.embed_query` is never called and the embedding sink could
be deleted outright with the whole suite still green — while production
measured zero embedding cost.

So `_EmbeddingRetriever.embed` below calls the REAL `embed_query` with a fake
embeddings client. No database is needed for that (a real `RagRetriever.__init__`
would `psycopg.connect`, which CI cannot do), and `embedding` is therefore the
first entry in every expected list here rather than a hole in the coverage.

Every assertion is made on a list collected OUTSIDE the emitter, and asserts the
COUNT first: an assertion inside a callback that may never run cannot fail.

Run from repo root:
    uv run --frozen --no-sync python -m pytest service/tests/test_usage_capture_graph.py -q
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage

from ingestion.retrieval import EMBED_MODEL, RetrievalResult, RetrievedChunk, embed_query
from service import generate as generate_module
from service import usage_capture
from service.graph import build_rag_graph
from service.rag import RagService

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError("rate limited", response=httpx.Response(429, request=_REQUEST), body=None)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeEmbeddingsClient:
    """Shaped like the OpenAI client's `.embeddings.create(...)`, including the
    `usage.prompt_tokens` the real response carries."""

    def __init__(self, prompt_tokens: int | None = 7, error: BaseException | None = None):
        self._prompt_tokens = prompt_tokens
        self._error = error
        self.calls = 0

    @property
    def embeddings(self):
        return self

    def create(self, model, input):  # noqa: A002 — the SDK's parameter name
        self.calls += 1
        if self._error is not None:
            raise self._error
        usage = (
            SimpleNamespace(prompt_tokens=self._prompt_tokens, total_tokens=self._prompt_tokens)
            if self._prompt_tokens is not None else None
        )
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])], usage=usage)


def _chunk(cid, entity, dist=0.3):
    return RetrievedChunk(
        chunk_id=cid, content_type="monster", entity_name=entity, class_name=None,
        feature_name=None, chapter=None, section=None, page_start=1,
        text_preview="preview", cosine_distance=dist,
    )


def _result(answerable=True):
    d = 0.30 if answerable else 0.70
    return RetrievalResult(
        chunks=[_chunk("c1", "Froghemoth", d), _chunk("c2", "Basilisk", d)],
        full_texts={"c1": "A froghemoth lurks in swamps." * 6, "c2": "A basilisk petrifies."},
        top1_distance=d, answerable=answerable,
        book_by_id={"c1": "vgm-5e", "c2": "mm-5e"},
        matched_content_types={"monster"},
    )


class _EmbeddingRetriever:
    """The fake that does NOT skip the embedding capture point: `embed` calls
    the real `ingestion.retrieval.embed_query`, so the sink scope installed by
    `embed_node` is genuinely exercised."""

    def __init__(self, result, embed_client):
        self._r = result
        self.embed_client = embed_client
        self.calls = 0

    def embed(self, prompt):
        self.calls += 1
        return embed_query(prompt, client=self.embed_client)

    def analyze(self, prompt):
        return set(), set(), set(self._r.matched_content_types)

    def search(self, emb, prompt, k, classes, entities, content_types, book_slugs):
        return list(self._r.chunks)

    def fetch(self, chunks):
        return dict(self._r.full_texts), dict(self._r.book_by_id)


class _SeqLLM:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        return AIMessage(content=self.texts[min(self.calls - 1, len(self.texts) - 1)])


class _ScriptedLLM:
    """Raises the given errors in order, then returns the canned texts."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return AIMessage(content=item)


@pytest.fixture
def records(monkeypatch):
    """Collect every emitted record. The list is asserted on from the test body,
    never from inside this callback."""
    collected: list[dict] = []
    monkeypatch.setattr(
        usage_capture, "_emit_record", lambda operation, fields: collected.append(dict(fields)),
    )
    return collected


@pytest.fixture
def no_backoff(monkeypatch):
    """Keep the retry COUNT (and therefore the record count) exactly as it is,
    while not spending the real backoff seconds in a unit test."""
    monkeypatch.setattr(generate_module, "_RETRY_BACKOFF_SECONDS", 0.0)


def _service(llm, *, embed_client=None, answerable=True):
    retriever = _EmbeddingRetriever(_result(answerable), embed_client or _FakeEmbeddingsClient())
    return RagService(retriever=retriever, llm_client=llm)


def _turn(svc, prompt, mode):
    """One captured turn: an operation context, then the real service path
    (answer -> answer_with_contexts -> the compiled graph)."""
    token = usage_capture.begin_operation(
        mode=mode, billed_account_id=1, actor_kind=usage_capture.ACTOR_ACCOUNT,
        campaign_id=None, request=SimpleNamespace(headers={}),
    )
    try:
        return svc.answer(prompt, mode=mode)
    finally:
        usage_capture.end_operation(token)


_SUGG_JSON = (
    '[{"style": "practical", "text": "Clear a room of enemies."},'
    ' {"style": "roleplay", "text": "Threaten a warlord with it."},'
    ' {"style": "wacky", "text": "Light every candle at once."}]'
)
_SPELL_ANSWER = "Fireball: 8d6 fire damage in a 20-foot radius [1]."
_SPELL_JSON = '{"name": "Fireball", "description": "8d6 fire damage in a 20-foot radius."}'
_STATBLOCK_ANSWER = (
    "Goblin Scout. Armor Class 15, Hit Points 7 (2d6), Speed 30 ft. "
    "STR 8, DEX 14. Challenge Rating 1/4 [1]."
)
_STATBLOCK_JSON = '{"name": "Goblin Scout", "ac": 15, "hp": 7}'
_NO_MARKERS_ANSWER = "The tavern is warm and loud, and the innkeeper is friendly [1]."


# ---------------------------------------------------------------------------
# AC 1 / AC 4b — one record per attempt, in order, INCLUDING the embedding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("mode", "replies", "expected"),
    [
        (
            "spell",
            [_SPELL_ANSWER, _SUGG_JSON, _SPELL_JSON],
            ["embedding", "answer", "suggestions", "spell_structuring"],
        ),
        ("gm", [_STATBLOCK_ANSWER, _STATBLOCK_JSON], ["embedding", "answer", "statblock_structuring"]),
        ("sage", [_STATBLOCK_ANSWER, _STATBLOCK_JSON], ["embedding", "answer", "statblock_structuring"]),
        ("sage", [_NO_MARKERS_ANSWER], ["embedding", "answer"]),
        ("rules", ["Rules say you may dash [1]."], ["embedding", "answer"]),
    ],
)
def test_every_provider_attempt_of_a_turn_is_recorded_in_order(mode, replies, expected, records):
    llm = _SeqLLM(replies)
    svc = _service(llm)

    _turn(svc, "What does Fireball do?", mode)

    assert [r["purpose"] for r in records] == expected
    assert [r["retry_index"] for r in records] == [0] * len(expected)
    assert [r["status"] for r in records] == ["ok"] * len(expected)
    assert svc.retriever.embed_client.calls == 1
    assert llm.calls == len(expected) - 1  # every purpose but the embedding


def test_the_embedding_record_carries_the_embed_alias_and_the_reported_tokens(records):
    svc = _service(_SeqLLM([_NO_MARKERS_ANSWER]), embed_client=_FakeEmbeddingsClient(prompt_tokens=11))

    _turn(svc, "What is a Basilisk?", "sage")

    embeddings = [r for r in records if r["purpose"] == "embedding"]
    assert len(embeddings) == 1
    assert embeddings[0]["alias"] == EMBED_MODEL
    assert embeddings[0]["provider"] == "openai"
    assert embeddings[0]["input_tokens"] == 11
    assert embeddings[0]["output_tokens"] is None
    assert embeddings[0]["finish_reason"] is None
    assert embeddings[0]["provider_request_id"] is None


def test_the_generation_records_carry_the_alias_actually_called(records):
    svc = _service(_SeqLLM([_NO_MARKERS_ANSWER]))

    _turn(svc, "What is a Basilisk?", "sage")

    answers = [r for r in records if r["purpose"] == "answer"]
    assert len(answers) == 1
    assert answers[0]["alias"] == svc.model
    assert answers[0]["provider"] == "openai"


def test_a_turn_refused_before_the_embed_records_nothing(records):
    """The empty-prompt preflight route refuses before `embed`, so no provider
    request is ever sent and there is nothing to record."""
    llm = _SeqLLM(["should not run"])
    svc = _service(llm)

    _turn(svc, "   ", "sage")

    assert records == []
    assert llm.calls == 0
    assert svc.retriever.embed_client.calls == 0


# ---------------------------------------------------------------------------
# AC 2 — every retried attempt is its own record, and the error still escapes
# ---------------------------------------------------------------------------

def test_each_retried_attempt_is_recorded_and_the_answer_still_succeeds(records, no_backoff):
    llm = _ScriptedLLM([_rate_limit_error(), _rate_limit_error(), _NO_MARKERS_ANSWER])
    svc = _service(llm)

    resp = _turn(svc, "What is a Basilisk?", "sage")

    answers = [r for r in records if r["purpose"] == "answer"]
    assert len(answers) == 3
    assert [r["retry_index"] for r in answers] == [0, 1, 2]
    assert [r["status"] for r in answers] == ["error", "error", "ok"]
    assert [r["error_class"] for r in answers] == ["RateLimitError", "RateLimitError", None]
    assert llm.calls == 3
    assert resp.answer == _NO_MARKERS_ANSWER


def test_a_provider_failure_records_every_attempt_and_propagates_unchanged(records, no_backoff):
    llm = _ScriptedLLM([_rate_limit_error()])
    svc = _service(llm)

    with pytest.raises(openai.RateLimitError):
        _turn(svc, "What is a Basilisk?", "sage")

    answers = [r for r in records if r["purpose"] == "answer"]
    assert len(answers) == 3
    assert [r["status"] for r in answers] == ["error", "error", "error"]
    assert llm.calls == 3


# ---------------------------------------------------------------------------
# AC 3 — the structuring calls are observed, still make exactly one call, and
# still degrade to None
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("mode", "replies", "purpose", "field"),
    [
        ("spell", [_SPELL_ANSWER, _SUGG_JSON], "spell_structuring", "spell_content"),
        ("gm", [_STATBLOCK_ANSWER], "statblock_structuring", "stat_block"),
    ],
)
def test_a_failing_structuring_call_records_one_attempt_and_never_retries(
    mode, replies, purpose, field, records, no_backoff,
):
    llm = _ScriptedLLM([*replies, _rate_limit_error()])
    svc = _service(llm)

    resp = _turn(svc, "What does Fireball do?", mode)

    structuring = [r for r in records if r["purpose"] == purpose]
    assert len(structuring) == 1, "max_attempts=1 — one attempt, never a retry"
    assert structuring[0]["retry_index"] == 0
    assert structuring[0]["status"] == "error"
    assert structuring[0]["error_class"] == "RateLimitError"
    assert llm.calls == len(replies) + 1
    assert getattr(resp, field) is None
    assert resp.answer == replies[0]


@pytest.mark.parametrize(
    ("mode", "replies", "purpose", "field"),
    [
        ("spell", [_SPELL_ANSWER, _SUGG_JSON, "not json at all"], "spell_structuring", "spell_content"),
        ("gm", [_STATBLOCK_ANSWER, "not json at all"], "statblock_structuring", "stat_block"),
    ],
)
def test_a_malformed_structuring_reply_is_an_ok_attempt_not_a_provider_error(
    mode, replies, purpose, field, records,
):
    """A parse failure is ours, not the provider's — and it was still billed."""
    svc = _service(_SeqLLM(replies))

    resp = _turn(svc, "What does Fireball do?", mode)

    structuring = [r for r in records if r["purpose"] == purpose]
    assert len(structuring) == 1
    assert structuring[0]["status"] == "ok"
    assert structuring[0]["error_class"] is None
    assert getattr(resp, field) is None


def test_the_statblock_cost_guard_still_skips_the_call_entirely(records):
    """No markers -> no structuring call at all, and therefore no record: a
    call that never happened must not look like an attempt."""
    llm = _SeqLLM([_NO_MARKERS_ANSWER])
    svc = _service(llm)

    _turn(svc, "Describe the tavern", "sage")

    assert [r["purpose"] for r in records] == ["embedding", "answer"]
    assert llm.calls == 1


# ---------------------------------------------------------------------------
# AC 10 — nothing is recorded outside a live turn, with a positive control
# ---------------------------------------------------------------------------

def test_positive_control_the_same_service_does_record_inside_a_turn(records):
    """First, prove "zero" carries information: the identical service, driven
    through the identical path WITH an operation, records four attempts."""
    svc = _service(_SeqLLM([_SPELL_ANSWER, _SUGG_JSON, _SPELL_JSON]))

    _turn(svc, "What does Fireball do?", "spell")

    assert [r["purpose"] for r in records] == [
        "embedding", "answer", "suggestions", "spell_structuring",
    ]


def test_a_direct_service_call_outside_a_turn_records_nothing(records):
    svc = _service(_SeqLLM([_SPELL_ANSWER, _SUGG_JSON, _SPELL_JSON]))

    resp = svc.answer("What does Fireball do?", mode="spell")

    assert records == []
    assert resp.answer == _SPELL_ANSWER
    assert resp.spell_content is not None  # the turn still worked in full


def test_a_bare_graph_invoke_with_config_none_records_nothing(records):
    svc = _service(_SeqLLM([_SPELL_ANSWER, _SUGG_JSON, _SPELL_JSON]))
    graph = build_rag_graph(svc)

    out = graph.invoke({"prompt": "What does Fireball do?", "mode": "spell"}, config=None)

    assert records == []
    assert out["answer"] == _SPELL_ANSWER


def test_an_embed_query_call_with_no_sink_returns_what_it_returns_today(records):
    """The eval CLIs call `embed_query(text)` with one argument and no scope."""
    client = _FakeEmbeddingsClient()

    vector = embed_query("a query", client=client)

    assert vector == [0.1, 0.2, 0.3]
    assert client.calls == 1
    assert records == []

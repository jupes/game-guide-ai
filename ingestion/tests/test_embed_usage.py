"""
The embedding attempt seam in `ingestion.retrieval.embed_query`
(agent-forge-harness-yje.5.1.1).

A query embedding is a paid provider call that production measured nowhere.
This file pins the seam itself; that the real graph actually reaches it is
pinned in service/tests/test_usage_capture_graph.py (a fake retriever that
returns a canned vector never comes near this code, which is exactly how the
capture could be deleted with a green suite).

No database and no network: `embed_query(text, client=<fake>)` touches neither.

Run from repo root:
    uv run --frozen --no-sync python -m pytest ingestion/tests/test_embed_usage.py -q
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ingestion.retrieval import (
    EMBED_MODEL,
    EmbeddingUnavailableError,
    embed_query,
    reset_embedding_sink,
    set_embedding_sink,
)


class _FakeEmbeddings:
    def __init__(self, prompt_tokens: int | None = 7, error: BaseException | None = None):
        self._prompt_tokens = prompt_tokens
        self._error = error
        self.calls: list[dict] = []

    @property
    def embeddings(self):
        return self

    def create(self, model, input):  # noqa: A002 — the SDK's parameter name
        self.calls.append({"model": model, "input": input})
        if self._error is not None:
            raise self._error
        usage = (
            SimpleNamespace(prompt_tokens=self._prompt_tokens, total_tokens=self._prompt_tokens)
            if self._prompt_tokens is not None else None
        )
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.4, 0.5])], usage=usage)


class _RecordingSink:
    """Collects into a list. Every assertion is made on `records` from the test
    body, never inside `record_embedding` — an assertion in a callback that may
    never run is a test that cannot fail."""

    def __init__(self):
        self.records: list[dict] = []
        self.started = 0

    def attempt_started(self) -> None:
        self.started += 1

    def record_embedding(self, *, input_tokens, error) -> None:
        self.records.append({"input_tokens": input_tokens, "error": error})


def test_a_successful_embedding_records_one_attempt_with_the_reported_tokens():
    sink = _RecordingSink()
    client = _FakeEmbeddings(prompt_tokens=7)

    vector = embed_query("how much damage does fireball do", client=client, sink=sink)

    assert len(sink.records) == 1
    assert sink.records[0]["input_tokens"] == 7
    assert sink.records[0]["error"] is None
    assert sink.started == 1
    assert vector == [0.4, 0.5]
    assert client.calls[0]["model"] == EMBED_MODEL


def test_a_provider_without_a_usage_block_records_none_not_zero():
    sink = _RecordingSink()

    embed_query("a query", client=_FakeEmbeddings(prompt_tokens=None), sink=sink)

    assert len(sink.records) == 1
    assert sink.records[0]["input_tokens"] is None


def test_a_failing_embedding_records_one_error_attempt_and_re_raises_unchanged():
    sink = _RecordingSink()
    boom = RuntimeError("provider is down")

    with pytest.raises(RuntimeError) as excinfo:
        embed_query("a query", client=_FakeEmbeddings(error=boom), sink=sink)

    assert excinfo.value is boom
    assert len(sink.records) == 1
    assert sink.records[0]["error"] is boom
    assert sink.records[0]["input_tokens"] is None


def test_a_missing_api_key_is_not_an_attempt_and_records_nothing(monkeypatch):
    """`_openai_client()` raises before any request is sent. No request, no
    attempt — a turn that never reached the provider must not look billed."""
    sink = _RecordingSink()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-replace-me")

    with pytest.raises(EmbeddingUnavailableError):
        embed_query("a query", sink=sink)

    assert sink.records == []
    assert sink.started == 0


def test_without_a_sink_embed_query_returns_exactly_what_it_returns_today():
    client = _FakeEmbeddings()

    assert embed_query("a query", client=client) == [0.4, 0.5]
    assert len(client.calls) == 1


def test_the_scoped_sink_is_used_when_no_sink_argument_is_given():
    sink = _RecordingSink()
    token = set_embedding_sink(sink)
    try:
        embed_query("a query", client=_FakeEmbeddings(prompt_tokens=3))
    finally:
        reset_embedding_sink(token)

    assert len(sink.records) == 1
    assert sink.records[0]["input_tokens"] == 3


def test_the_scope_ends_when_it_is_reset():
    sink = _RecordingSink()
    token = set_embedding_sink(sink)
    reset_embedding_sink(token)

    embed_query("a query", client=_FakeEmbeddings())

    assert sink.records == []


def test_an_explicit_sink_argument_wins_over_the_scoped_one():
    scoped = _RecordingSink()
    explicit = _RecordingSink()
    token = set_embedding_sink(scoped)
    try:
        embed_query("a query", client=_FakeEmbeddings(prompt_tokens=5), sink=explicit)
    finally:
        reset_embedding_sink(token)

    assert len(explicit.records) == 1
    assert scoped.records == []

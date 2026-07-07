"""
Unit tests for the LangGraph orchestration of the RAG pipeline (ziw.2 / Phase 1).

The graph must behave identically to the prior imperative RagService.answer:
retrieve -> grounding gate -> (generate | refuse), same gate semantics per mode,
same delegation to the existing building blocks. No DB / LLM / network — fakes only.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/test_graph.py -q
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from ingestion.retrieval import RetrievalResult, RetrievedChunk

from service.graph import build_rag_graph
from service.rag import RagService, REFUSAL


def _chunk(cid, entity, dist=0.3):
    return RetrievedChunk(
        chunk_id=cid, content_type="monster", entity_name=entity, class_name=None,
        feature_name=None, chapter=None, section=None, page_start=1,
        text_preview="preview", cosine_distance=dist,
    )


def _result(answerable=True, chunks=True):
    # Chunk distances must be consistent with the intended answerability: the
    # pipeline derives answerable from the top-1 distance (koz gate), it does
    # not trust a canned flag (1em.3).
    d = 0.30 if answerable else 0.70
    cs = [_chunk("c1", "Froghemoth", d), _chunk("c2", "Basilisk", d)] if chunks else []
    return RetrievalResult(
        chunks=cs,
        full_texts={"c1": "A froghemoth lurks in swamps." * 6, "c2": "A basilisk petrifies."},
        top1_distance=d if chunks else None,
        answerable=answerable,
        book_by_id={"c1": "vgm-5e", "c2": "mm-5e"},
        matched_content_types={"monster"},
    )


class _FakeRetriever:
    """Granular stage-method fake (1em.3): the graph drives embed → analyze →
    search → fetch as separate nodes. `calls` counts embed() — the first
    pipeline touch — preserving the old 'retrieval never ran' assertions.
    Records the filters search() received so scope behavior is observable."""

    def __init__(self, result):
        self._r = result
        self.calls = 0
        self.search_filters: dict | None = None

    def embed(self, prompt):
        self.calls += 1
        return [0.1, 0.2, 0.3]

    def analyze(self, prompt):
        return set(), set(), set(self._r.matched_content_types)

    def search(self, emb, prompt, k, classes, entities, content_types, book_slugs):
        self.search_filters = {"content_types": content_types, "book_slugs": book_slugs}
        return list(self._r.chunks)

    def fetch(self, chunks):
        return dict(self._r.full_texts), dict(self._r.book_by_id)


class _FakeLLM:
    """LangChain-shaped fake chat model: .invoke(messages) -> AIMessage, counts calls."""
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        return AIMessage(content=self.text)


def _svc(result, llm):
    return RagService(retriever=_FakeRetriever(result), llm_client=llm)


def test_graph_happy_path_generates():
    svc = _svc(_result(answerable=True), _FakeLLM("Froghemoths lurk [1]."))
    graph = build_rag_graph(svc)
    out = graph.invoke({"prompt": "What is a Froghemoth?", "mode": "sage"})
    assert out["answerable"] is True
    assert "Froghemoth" in out["answer"]
    assert len(out["sources"]) == 2


def test_graph_refusal_skips_llm():
    llm = _FakeLLM("should not run")
    svc = _svc(_result(answerable=False), llm)
    graph = build_rag_graph(svc)
    out = graph.invoke({"prompt": "How do I evolve my Pokemon?", "mode": "sage"})
    assert out["answerable"] is False
    assert out["answer"] == REFUSAL
    assert out["sources"] == []
    assert llm.calls == 0  # refuse path never calls the LLM


def test_graph_gm_mode_proceeds_on_chunks_even_if_not_answerable():
    # GM gate is relaxed: any chunks -> generate (creative), even answerable=False.
    llm = _FakeLLM("A tavern brews trouble [1].")
    svc = _svc(_result(answerable=False, chunks=True), llm)
    graph = build_rag_graph(svc)
    out = graph.invoke({"prompt": "Describe a tavern", "mode": "gm"})
    assert "tavern" in out["answer"].lower()
    assert llm.calls == 1


def test_graph_gm_mode_refuses_with_no_chunks():
    llm = _FakeLLM("nope")
    svc = _svc(_result(answerable=False, chunks=False), llm)
    graph = build_rag_graph(svc)
    out = graph.invoke({"prompt": "Describe a void", "mode": "gm"})
    assert out["answer"] == REFUSAL
    assert llm.calls == 0


# ---------------------------------------------------------------------------
# 1em.2 / CP-B — pre-flight lives inside the graph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_graph_empty_prompt_refuses_without_retrieval_or_llm(blank):
    """An empty/whitespace prompt refuses INSIDE the graph — the retrieve node
    and the LLM never run (previously guarded outside in RagService.answer)."""
    llm = _FakeLLM("should not run")
    svc = _svc(_result(answerable=True), llm)
    graph = build_rag_graph(svc)
    out = graph.invoke({"prompt": blank, "mode": "sage"})
    assert out["answer"] == REFUSAL
    assert out["sources"] == []
    assert out["answerable"] is False
    assert svc.retriever.calls == 0
    assert llm.calls == 0


def test_graph_unknown_mode_raises_before_retrieval():
    """An invalid mode raises ValueError from the graph's preflight node,
    before any retrieval work (same contract RagService.answer had)."""
    llm = _FakeLLM("x")
    svc = _svc(_result(answerable=True), llm)
    graph = build_rag_graph(svc)
    with pytest.raises(ValueError, match="bogus"):
        graph.invoke({"prompt": "anything", "mode": "bogus"})
    assert svc.retriever.calls == 0
    assert llm.calls == 0


# ---------------------------------------------------------------------------
# 1em.3 / CP-C — retrieval stages as first-class nodes
# ---------------------------------------------------------------------------


def test_graph_spell_mode_scopes_search_to_spell_books():
    """Tracer (Track A): a spell-mode question flows through the exploded
    pipeline and the SEARCH stage receives the spell scope — content_types
    forced to {'spell'} and the book filter restricted to spell-bearing books."""
    llm = _FakeLLM("Fireball deals 8d6 fire damage [1].")
    svc = _svc(_result(answerable=True), llm)
    graph = build_rag_graph(svc)
    out = graph.invoke({"prompt": "What does Fireball do?", "mode": "spell"})
    assert "Fireball" in out["answer"]
    filters = svc.retriever.search_filters
    assert filters is not None, "search stage never ran"
    assert filters["content_types"] == {"spell"}
    assert "phb-5e" in filters["book_slugs"]
    assert "mm-5e" not in filters["book_slugs"]


class _ReversingReranker:
    """Fake reranker: reverses whatever order it is given, counts calls."""

    def __init__(self):
        self.calls = 0

    def rerank(self, query, texts):
        self.calls += 1
        return list(range(len(texts)))[::-1]


def _result_with_ctypes(ctypes):
    r = _result(answerable=True)
    r.matched_content_types.clear()
    r.matched_content_types.update(ctypes)
    return r


def test_graph_rerank_reorders_prose_queries():
    """With a reranker configured and prose-like query content types, the
    rerank node reorders the chunks — visible in the cited sources order."""
    llm = _FakeLLM("answer [1]")
    reranker = _ReversingReranker()
    svc = RagService(
        retriever=_FakeRetriever(_result_with_ctypes({"rule"})),
        llm_client=llm, reranker=reranker,
    )
    graph = build_rag_graph(svc)
    out = graph.invoke({"prompt": "How does grappling work?", "mode": "sage"})
    assert reranker.calls == 1
    assert out["sources"][0].entity == "Basilisk"  # reversed: c2 first


def test_graph_rerank_skips_structured_queries():
    """Structured content types (monster) skip the rerank node entirely —
    the should_rerank gate, now as graph routing."""
    llm = _FakeLLM("answer [1]")
    reranker = _ReversingReranker()
    svc = RagService(
        retriever=_FakeRetriever(_result_with_ctypes({"monster"})),
        llm_client=llm, reranker=reranker,
    )
    graph = build_rag_graph(svc)
    out = graph.invoke({"prompt": "What is a Froghemoth?", "mode": "sage"})
    assert reranker.calls == 0
    assert out["sources"][0].entity == "Froghemoth"  # original order kept

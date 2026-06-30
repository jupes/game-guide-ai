"""
Unit tests for the LangGraph orchestration of the RAG pipeline (ziw.2 / Phase 1).

The graph must behave identically to the prior imperative RagService.answer:
retrieve -> grounding gate -> (generate | refuse), same gate semantics per mode,
same delegation to the existing building blocks. No DB / LLM / network — fakes only.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/test_graph.py -q
"""

from __future__ import annotations

from ingestion.retrieval import RetrievalResult, RetrievedChunk

from service.graph import build_rag_graph
from service.rag import RagService, REFUSAL


def _chunk(cid, entity):
    return RetrievedChunk(
        chunk_id=cid, content_type="monster", entity_name=entity, class_name=None,
        feature_name=None, chapter=None, section=None, page_start=1,
        text_preview="preview", cosine_distance=0.3,
    )


def _result(answerable=True, chunks=True):
    cs = [_chunk("c1", "Froghemoth"), _chunk("c2", "Basilisk")] if chunks else []
    return RetrievalResult(
        chunks=cs,
        full_texts={"c1": "A froghemoth lurks in swamps." * 6, "c2": "A basilisk petrifies."},
        top1_distance=0.30 if answerable else 0.70,
        answerable=answerable,
        book_by_id={"c1": "vgm-5e", "c2": "mm-5e"},
        matched_content_types={"monster"},
    )


class _FakeRetriever:
    def __init__(self, result):
        self._r = result
        self.calls = 0

    def retrieve(self, prompt, reranker=None, mode="sage"):
        self.calls += 1
        return self._r


def _fake_completion(text):
    class _M:
        pass
    msg = _M(); msg.content = text
    choice = _M(); choice.message = msg
    resp = _M(); resp.choices = [choice]
    return resp


class _FakeLLM:
    def __init__(self, text):
        self.text = text
        self.chat = self
        self.calls = 0

    @property
    def completions(self):
        return self

    def create(self, **kw):
        self.calls += 1
        return _fake_completion(self.text)


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

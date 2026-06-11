"""
Unit tests for the agent service — pure context/source assembly + mocked
RagService (no DB, no LLM, no network).

Run from repo root:
    uv run --with pydantic python -m service.test_service
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ingestion"))

from retrieval import RetrievalResult, RetrievedChunk  # noqa: E402

from service.generate import build_context, build_sources, generate_answer, GROUNDED_PROMPT  # noqa: E402
from service.rag import RagService, REFUSAL  # noqa: E402
from service.models import ChatResponse  # noqa: E402


def _chunk(cid, entity, ctype="monster", section=None, chapter=None, page=1):
    return RetrievedChunk(
        chunk_id=cid, content_type=ctype, entity_name=entity, class_name=None,
        feature_name=None, chapter=chapter, section=section, page_start=page,
        text_preview="preview", cosine_distance=0.3,
    )


def _result(answerable=True):
    chunks = [_chunk("c1", "Froghemoth"), _chunk("c2", "Basilisk")]
    return RetrievalResult(
        chunks=chunks,
        full_texts={"c1": "A froghemoth is an amphibious monster that lurks in swamps." * 6,
                    "c2": "A basilisk's gaze can petrify."},
        top1_distance=0.30 if answerable else 0.70,
        answerable=answerable,
        book_by_id={"c1": "vgm-5e", "c2": "mm-5e"},
        matched_content_types={"monster"},
    )


class _FakeRetriever:
    def __init__(self, result): self._r = result
    def retrieve(self, prompt, reranker=None): return self._r


class _FakeLLM:
    """Mimics openai client.chat.completions.create(...).choices[0].message.content"""
    def __init__(self, text): self.text = text; self.chat = self
    @property
    def completions(self): return self
    def create(self, **kw):
        class _M: pass
        msg = _M(); msg.content = self.text
        choice = _M(); choice.message = msg
        resp = _M(); resp.choices = [choice]
        return resp


# ---------------------------------------------------------------------------
# build_context / build_sources (pure)
# ---------------------------------------------------------------------------

def test_build_context_uses_full_text_numbered():
    ctx = build_context(_result(), top_n=5)
    assert "[1]" in ctx and "[2]" in ctx
    assert "Froghemoth" in ctx
    # full text, not the 120-char preview
    assert "amphibious monster that lurks" in ctx
    assert "preview" not in ctx


def test_build_sources_dedup_and_truncate():
    srcs = build_sources(_result(), top_n=5)
    assert len(srcs) == 2
    assert srcs[0].book == "vgm-5e" and srcs[0].entity == "Froghemoth"
    assert len(srcs[0].snippet) <= 241  # SNIPPET_MAX + ellipsis


def test_build_sources_dedup_same_entity():
    chunks = [_chunk("a", "Froghemoth"), _chunk("b", "Froghemoth")]
    r = RetrievalResult(chunks=chunks, full_texts={"a": "x" * 10, "b": "y" * 10},
                        top1_distance=0.3, answerable=True, book_by_id={"a": "vgm-5e", "b": "vgm-5e"})
    assert len(build_sources(r)) == 1   # deduped by entity


def test_generate_answer_uses_injected_client():
    out = generate_answer("Q?", "ctx", client=_FakeLLM("the answer [1]"))
    assert out == "the answer [1]"


# ---------------------------------------------------------------------------
# RagService.answer (mocked)
# ---------------------------------------------------------------------------

def test_answer_happy_path():
    svc = RagService(retriever=_FakeRetriever(_result(answerable=True)),
                     llm_client=_FakeLLM("Froghemoths lurk in swamps [1]."))
    resp = svc.answer("What is a Froghemoth?")
    assert isinstance(resp, ChatResponse)
    assert resp.answerable is True
    assert "Froghemoth" in resp.answer
    assert len(resp.sources) == 2


def test_answer_refusal_skips_llm():
    # answerable=False → refusal, no LLM call, empty sources
    called = {"n": 0}
    class _Boom(_FakeLLM):
        def create(self, **kw):
            called["n"] += 1
            return super().create(**kw)
    svc = RagService(retriever=_FakeRetriever(_result(answerable=False)),
                     llm_client=_Boom("should not be called"))
    resp = svc.answer("How do I evolve my Pokemon?")
    assert resp.answerable is False
    assert resp.answer == REFUSAL
    assert resp.sources == []
    assert called["n"] == 0   # LLM never invoked on refusal


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}"); failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _run()

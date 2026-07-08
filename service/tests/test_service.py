"""
Unit tests for the agent service — pure context/source assembly + mocked
RagService (no DB, no LLM, no network).

Run from repo root:
    uv run --with '.[test]' python -m pytest service/test_service.py -q
"""

from __future__ import annotations


import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ingestion.retrieval import RetrievalResult, RetrievedChunk

from service.generate import build_context, build_sources, generate_answer
from service.rag import RagService, REFUSAL
from service.models import ChatResponse


def _chunk(cid, entity, ctype="monster", section=None, chapter=None, page=1, dist=0.3):
    return RetrievedChunk(
        chunk_id=cid, content_type=ctype, entity_name=entity, class_name=None,
        feature_name=None, chapter=chapter, section=section, page_start=page,
        text_preview="preview", cosine_distance=dist,
    )


def _result(answerable=True):
    # Chunk distances must match the intended answerability — the pipeline
    # derives answerable from the top-1 distance (koz gate), not from the
    # canned flag (1em.3).
    d = 0.30 if answerable else 0.70
    chunks = [_chunk("c1", "Froghemoth", dist=d), _chunk("c2", "Basilisk", dist=d)]
    return RetrievalResult(
        chunks=chunks,
        full_texts={"c1": "A froghemoth is an amphibious monster that lurks in swamps." * 6,
                    "c2": "A basilisk's gaze can petrify."},
        top1_distance=d,
        answerable=answerable,
        book_by_id={"c1": "vgm-5e", "c2": "mm-5e"},
        matched_content_types={"monster"},
    )


class _FakeRetriever:
    """Granular stage-method fake (1em.3) — the graph drives embed/analyze/
    search/fetch as separate nodes; the canned RetrievalResult supplies each
    stage's output. answerable is recomputed from top1 by the graph's assembly,
    so canned results must keep top1 consistent with their answerable flag."""

    def __init__(self, result): self._r = result
    def embed(self, prompt): return [0.1, 0.2, 0.3]
    def analyze(self, prompt):
        return set(), set(), set(self._r.matched_content_types)
    def search(self, emb, prompt, k, classes, entities, content_types, book_slugs):
        return list(self._r.chunks)
    def fetch(self, chunks):
        return dict(self._r.full_texts), dict(self._r.book_by_id)


class _FakeLLM:
    """LangChain-shaped fake chat model (ziw.2 / CP2): `.invoke(messages)` returns
    an AIMessage and records the messages it was called with + the call count.
    Replaced the OpenAI-client-shaped fake when the LLM node moved to ChatOpenAI."""
    def __init__(self, text):
        self.text = text
        self.last_messages = None
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        self.last_messages = messages
        return AIMessage(content=self.text)


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


def test_build_sources_keeps_distinct_cased_entities():
    # Dedup is case-sensitive: entities differing only by case are distinct and
    # both kept (lowercasing would silently drop one).
    chunks = [_chunk("a", "Fireball"), _chunk("b", "fireball")]
    r = RetrievalResult(chunks=chunks, full_texts={"a": "x" * 10, "b": "y" * 10},
                        top1_distance=0.3, answerable=True, book_by_id={"a": "phb-5e", "b": "phb-5e"})
    assert len(build_sources(r)) == 2


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
    llm = _FakeLLM("should not be called")
    svc = RagService(retriever=_FakeRetriever(_result(answerable=False)),
                     llm_client=llm)
    resp = svc.answer("How do I evolve my Pokemon?")
    assert resp.answerable is False
    assert resp.answer == REFUSAL
    assert resp.sources == []
    assert llm.calls == 0   # LLM never invoked on refusal


class _CountingRetriever(_FakeRetriever):
    """Tracks whether the retrieval pipeline was touched at all (embed is the
    first stage the graph runs)."""
    def __init__(self, result):
        super().__init__(result)
        self.calls = 0
    def embed(self, prompt):
        self.calls += 1
        return super().embed(prompt)


def test_answer_unknown_mode_raises_before_retrieval():
    # An invalid mode (only reachable by a non-API caller) fails fast with a
    # ValueError, BEFORE any retrieval work — not a late crash at response build.
    retriever = _CountingRetriever(_result())
    svc = RagService(retriever=retriever, llm_client=_FakeLLM("x"))
    with pytest.raises(ValueError):
        svc.answer("anything", mode="bogus")
    assert retriever.calls == 0   # validated up front


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_answer_empty_prompt_refuses(blank):
    # An empty/whitespace prompt (only reachable by a non-API caller; the API
    # enforces min_length=1) short-circuits to REFUSAL without retrieval or LLM.
    retriever = _CountingRetriever(_result())
    svc = RagService(retriever=retriever, llm_client=_FakeLLM("should not run"))
    resp = svc.answer(blank)
    assert resp.answerable is False
    assert resp.answer == REFUSAL
    assert resp.sources == []
    assert retriever.calls == 0


def test_generate_answer_empty_context_raises():
    # Defensive guard: generate_answer is normally only reached with non-empty
    # context (the grounding gate), so empty context is a programming error.
    with pytest.raises(ValueError):
        generate_answer("a question", "", client=_FakeLLM("x"))


# ---------------------------------------------------------------------------
# CP-F4.2 — Per-mode persona (behavior #16)
# ---------------------------------------------------------------------------

def test_sage_mode_uses_sage_persona():
    """generate_answer with mode='sage' sends a system message containing 'Sage'."""
    from service.generate import generate_answer as _ga
    llm = _FakeLLM("answer")
    _ga("Q?", "ctx", mode="sage", client=llm)
    system_msgs = [m for m in llm.last_messages if isinstance(m, SystemMessage)]
    assert system_msgs, "expected a system message"
    assert "Sage" in system_msgs[0].content


def test_spell_mode_uses_spell_archivist_persona():
    """generate_answer with mode='spell' sends the Spell Archivist system message."""
    from service.generate import generate_answer as _ga
    llm = _FakeLLM("answer")
    _ga("Q?", "ctx", mode="spell", client=llm)
    system_msgs = [m for m in llm.last_messages if isinstance(m, SystemMessage)]
    assert system_msgs
    assert "Spell Archivist" in system_msgs[0].content


def test_rules_mode_uses_rules_arbiter_persona():
    """generate_answer with mode='rules' sends the Rules Arbiter system message."""
    from service.generate import generate_answer as _ga
    llm = _FakeLLM("answer")
    _ga("Q?", "ctx", mode="rules", client=llm)
    system_msgs = [m for m in llm.last_messages if isinstance(m, SystemMessage)]
    assert system_msgs
    assert "Rules Arbiter" in system_msgs[0].content


def test_gm_mode_uses_gm_oracle_persona():
    """generate_answer with mode='gm' sends the GM Oracle system message."""
    from service.generate import generate_answer as _ga
    llm = _FakeLLM("answer")
    _ga("Q?", "ctx", mode="gm", client=llm)
    system_msgs = [m for m in llm.last_messages if isinstance(m, SystemMessage)]
    assert system_msgs
    assert "GM Oracle" in system_msgs[0].content


def test_grounded_template_in_user_message():
    """The user message contains the sources block (not the persona)."""
    from service.generate import generate_answer as _ga
    llm = _FakeLLM("answer")
    _ga("My Q?", "src_block", mode="sage", client=llm)
    user_msgs = [m for m in llm.last_messages if isinstance(m, HumanMessage)]
    assert user_msgs
    assert "Sources:" in user_msgs[0].content
    assert "My Q?" in user_msgs[0].content


# ---------------------------------------------------------------------------
# CP-F4.3 — Per-mode retrieval scoping (behavior #17)
# ---------------------------------------------------------------------------

def test_spell_scope_forces_spell_ctype_and_limits_books():
    from ingestion.scope import scope_for_mode
    ctypes, books = scope_for_mode("spell", set())
    assert "spell" in ctypes
    assert "monster" not in ctypes
    assert "dmg-5e" not in books
    assert "phb-5e" in books


def test_spell_scope_overrides_query_derived_ctypes():
    """spell mode forces only spell ctype, ignoring query-derived non-spell types."""
    from ingestion.scope import scope_for_mode
    ctypes, books = scope_for_mode("spell", {"class_feature", "rule"})
    assert ctypes == {"spell"}


def test_rules_scope_excludes_monster_and_creative_ctypes():
    from ingestion.scope import scope_for_mode
    ctypes, books = scope_for_mode("rules", {"monster"})
    assert "monster" not in ctypes
    assert "dm_guidance" not in ctypes
    assert "magic_item" not in ctypes
    # rules ctypes are present
    assert "rule" in ctypes or "class_feature" in ctypes


def test_rules_scope_books_is_none_or_all():
    """rules mode doesn't restrict books (all books supply rules)."""
    from ingestion.scope import scope_for_mode
    ctypes, books = scope_for_mode("rules", set())
    assert books is None


def test_gm_scope_includes_monster_dm_guidance_magic_item():
    from ingestion.scope import scope_for_mode
    ctypes, books = scope_for_mode("gm", set())
    assert "monster" in ctypes
    assert "dm_guidance" in ctypes
    assert "magic_item" in ctypes
    assert books is None  # no book restriction for GM


def test_gm_scope_merges_query_derived_ctypes():
    """gm mode merges forced ctypes with query-derived ones."""
    from ingestion.scope import scope_for_mode
    ctypes, books = scope_for_mode("gm", {"spell"})
    assert "spell" in ctypes
    assert "monster" in ctypes


def test_sage_scope_passes_through_unmodified():
    """sage mode returns query-derived ctypes unchanged, no book restriction."""
    from ingestion.scope import scope_for_mode
    query_ctypes = {"rule", "class_feature"}
    ctypes, books = scope_for_mode("sage", query_ctypes)
    assert ctypes == query_ctypes
    assert books is None


# ---------------------------------------------------------------------------
# CP-F4.4 — GM relaxed gate + StubSecondaryRetriever + _merge_results (behavior #18)
# ---------------------------------------------------------------------------

def test_gm_mode_answers_when_not_answerable_but_has_chunks():
    """GM mode proceeds when chunks exist even if answerable=False."""
    from service.rag import RagService, REFUSAL
    svc = RagService(
        retriever=_FakeRetriever(_result(answerable=False)),
        llm_client=_FakeLLM("Here is a creative swamp monster idea [1]."),
    )
    resp = svc.answer("Invent a swamp monster", mode="gm")
    assert resp.answer != REFUSAL
    assert resp.answerable is False  # echoes the low-confidence flag


def test_sage_refuses_when_not_answerable():
    """sage mode still refuses when answerable=False."""
    from service.rag import RagService, REFUSAL
    svc = RagService(
        retriever=_FakeRetriever(_result(answerable=False)),
        llm_client=_FakeLLM("should not be called"),
    )
    resp = svc.answer("Invent a swamp monster", mode="sage")
    assert resp.answer == REFUSAL


def test_spell_refuses_when_not_answerable():
    """spell mode still refuses when answerable=False."""
    from service.rag import RagService, REFUSAL
    svc = RagService(
        retriever=_FakeRetriever(_result(answerable=False)),
        llm_client=_FakeLLM("should not be called"),
    )
    resp = svc.answer("x", mode="spell")
    assert resp.answer == REFUSAL


def test_rules_refuses_when_not_answerable():
    """rules mode still refuses when answerable=False."""
    from service.rag import RagService, REFUSAL
    svc = RagService(
        retriever=_FakeRetriever(_result(answerable=False)),
        llm_client=_FakeLLM("should not be called"),
    )
    resp = svc.answer("x", mode="rules")
    assert resp.answer == REFUSAL


def test_gm_refuses_when_no_chunks():
    """Even GM mode refuses if no chunks are retrieved."""
    from service.rag import RagService, REFUSAL
    empty = RetrievalResult(
        chunks=[], full_texts={}, top1_distance=None,
        answerable=False, book_by_id={},
    )
    svc = RagService(
        retriever=_FakeRetriever(empty),
        llm_client=_FakeLLM("should not be called"),
    )
    resp = svc.answer("Xyz", mode="gm")
    assert resp.answer == REFUSAL


def test_stub_secondary_retriever_returns_empty():
    """StubSecondaryRetriever.retrieve() always returns empty chunks."""
    from service.rag import StubSecondaryRetriever
    stub = StubSecondaryRetriever()
    r = stub.retrieve("anything")
    assert r.chunks == []
    assert r.answerable is False


def test_merge_results_with_empty_secondary_preserves_primary():
    """_merge_results with an empty secondary leaves primary unchanged."""
    from service.rag import RagService, StubSecondaryRetriever
    primary = _result(answerable=True)
    secondary = StubSecondaryRetriever().retrieve("x")
    svc = RagService(
        retriever=_FakeRetriever(primary),
        llm_client=_FakeLLM("ans"),
    )
    merged = svc._merge_results(primary, secondary)
    assert merged.chunks == primary.chunks
    assert merged.full_texts == primary.full_texts


def test_merge_results_primary_chunks_ranked_first():
    """When secondary has chunks, primary chunks appear before secondary in merge."""
    from service.rag import RagService
    from dataclasses import dataclass

    primary = _result(answerable=True)

    @dataclass
    class _SecResult:
        chunks: list
        full_texts: dict
        book_by_id: dict
        answerable: bool

    sec_chunk = _chunk("sec1", "WorldMonster", ctype="monster")
    secondary = _SecResult(
        chunks=[sec_chunk],
        full_texts={"sec1": "A world-unique monster."},
        book_by_id={"sec1": "world"},
        answerable=True,
    )

    svc = RagService(retriever=_FakeRetriever(primary), llm_client=_FakeLLM("ans"))
    merged = svc._merge_results(primary, secondary)
    # Primary chunks come first
    primary_ids = {c.chunk_id for c in primary.chunks}
    merged_ids = [c.chunk_id for c in merged.chunks]
    for pid in primary_ids:
        assert merged_ids.index(pid) < merged_ids.index("sec1")

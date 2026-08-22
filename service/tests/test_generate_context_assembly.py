"""
Characterization tests for `assemble_context()` (D2, agent-forge-harness-b8o.1).

Written BEFORE extracting the context-assembly logic out of `service/graph.py`'s
`generate_node` inline block, so the extraction is provably behavior-preserving
(TDD row 9b in the adopted plan). Pin the exact string the current inline logic
produces for the three cases the plan calls out: corpus-only, attachment-only,
and corpus+attachment — plus the label-default and cap-length edges the inline
block also handles today.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_generate_context_assembly.py -q
"""

from __future__ import annotations

from ingestion.retrieval import RetrievalResult, RetrievedChunk
from service.generate import assemble_context, build_context, context_texts


def _chunk(cid: str, entity: str, dist: float = 0.3) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid, content_type="monster", entity_name=entity, class_name=None,
        feature_name=None, chapter=None, section=None, page_start=1,
        text_preview="preview", cosine_distance=dist,
    )


def _result_with_chunks() -> RetrievalResult:
    chunks = [_chunk("c1", "Froghemoth"), _chunk("c2", "Basilisk")]
    return RetrievalResult(
        chunks=chunks,
        full_texts={"c1": "A froghemoth lurks in swamps.", "c2": "A basilisk's gaze can petrify."},
        top1_distance=0.3, answerable=True,
        book_by_id={"c1": "vgm-5e", "c2": "mm-5e"},
    )


def _result_no_chunks() -> RetrievalResult:
    return RetrievalResult(
        chunks=[], full_texts={}, top1_distance=0.9, answerable=False, book_by_id={},
    )


def test_corpus_only_matches_build_context_unchanged():
    # No attachment: assemble_context must be exactly build_context's output,
    # not a reimplementation of it.
    r = _result_with_chunks()
    ctx = assemble_context(r, attachment_context=None, attachment_label=None, top_n=5)
    assert ctx == build_context(r, top_n=5)


def test_no_corpus_no_attachment_is_empty():
    r = _result_no_chunks()
    ctx = assemble_context(r, attachment_context=None, attachment_label=None, top_n=5)
    assert ctx == ""


def test_attachment_only_becomes_the_sole_numbered_source():
    # Empty corpus context: the attachment block IS the context, not appended
    # to an empty string with a stray leading separator.
    r = _result_no_chunks()
    ctx = assemble_context(
        r, attachment_context="The orb is cursed.", attachment_label="notes.txt", top_n=5,
    )
    assert ctx == "[1] (Attachment — notes.txt): The orb is cursed."


def test_corpus_and_attachment_appends_as_the_next_numbered_source():
    r = _result_with_chunks()
    ctx = assemble_context(
        r, attachment_context="The orb is cursed.", attachment_label="notes.txt", top_n=5,
    )
    base = build_context(r, top_n=5)
    n = len(context_texts(r, top_n=5)) + 1
    assert ctx == f"{base}\n\n[{n}] (Attachment — notes.txt): The orb is cursed."


def test_attachment_label_defaults_when_none():
    r = _result_no_chunks()
    ctx = assemble_context(r, attachment_context="x", attachment_label=None, top_n=5)
    assert "(Attachment — your attachment):" in ctx


def test_attachment_context_is_capped_at_the_configured_limit():
    import config

    r = _result_no_chunks()
    ctx = assemble_context(
        r, attachment_context="x" * (config.ATTACHMENT_MAX_CHARS + 500),
        attachment_label="notes.txt", top_n=5,
    )
    body = ctx.split(": ", 1)[1]
    assert len(body) == config.ATTACHMENT_MAX_CHARS


def test_top_n_limits_corpus_chunks_same_as_build_context():
    r = _result_with_chunks()
    ctx = assemble_context(r, attachment_context=None, attachment_label=None, top_n=1)
    assert ctx == build_context(r, top_n=1)
    assert "Basilisk" not in ctx

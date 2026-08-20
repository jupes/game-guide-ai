"""
Unit tests for embed.py's row-building step — pure, no DB/network.

Run:
    uv run --with '.[test]' python -m pytest ingestion/tests/test_embed.py -q
"""

from __future__ import annotations

from ingestion.embed import _row_for_upsert

LEGACY_PDF_CHUNK = {
    "chunk_id": "phb-5e:12:0:3",
    "book_slug": "phb-5e",
    "source_file": "phb.pdf",
    "page_start": 12,
    "page_end": 12,
    "part": "Part 1",
    "chapter": "Classes",
    "section": None,
    "content_type": "class_feature",
    "entity_name": "Fighter",
    "class_name": "Fighter",
    "feature_name": "Second Wind",
    "text": "You have a limited well of stamina...",
}

WIKI_CHUNK = {
    "chunk_id": "wikidot-5e:abcdef0123",
    "book_slug": "wikidot-5e",
    "source_file": "https://dnd5e.wikidot.com/spell:fireball",
    "page_start": None,
    "page_end": None,
    "part": None,
    "chapter": None,
    "section": None,
    "content_type": "spell",
    "entity_name": "Fireball",
    "class_name": None,
    "feature_name": None,
    "text": "A bright streak flashes...",
    "source_type": "wiki",
    "source_url": "https://dnd5e.wikidot.com/spell:fireball",
    "license": "CC BY-SA 3.0",
}

EMBEDDING = [0.0] * 1536


def test_legacy_pdf_chunk_defaults_to_pdf_provenance():
    """Every chunk file written before this feature lacks source_type/source_url/
    license entirely — the row builder must fill them in rather than KeyError,
    so re-embedding an old chunks.jsonl still works."""
    row = _row_for_upsert(LEGACY_PDF_CHUNK, EMBEDDING)
    assert row["source_type"] == "pdf"
    assert row["source_url"] is None
    assert row["license"] is None


def test_wiki_chunk_provenance_passes_through_unchanged():
    row = _row_for_upsert(WIKI_CHUNK, EMBEDDING)
    assert row["source_type"] == "wiki"
    assert row["source_url"] == "https://dnd5e.wikidot.com/spell:fireball"
    assert row["license"] == "CC BY-SA 3.0"


def test_embedding_is_attached_and_original_dict_is_not_mutated():
    original = dict(LEGACY_PDF_CHUNK)
    row = _row_for_upsert(LEGACY_PDF_CHUNK, EMBEDDING)
    assert row["embedding"] == EMBEDDING
    assert LEGACY_PDF_CHUNK == original, "row builder must not mutate the input chunk dict"


def test_other_fields_pass_through_unchanged():
    row = _row_for_upsert(LEGACY_PDF_CHUNK, EMBEDDING)
    for key in ("chunk_id", "book_slug", "source_file", "page_start", "page_end",
                "content_type", "entity_name", "class_name", "feature_name", "text"):
        assert row[key] == LEGACY_PDF_CHUNK[key]

"""
Unit tests for ingestion/retrieval.py — book-filter SQL generation and the
per-mode scope helper (_retrieval_scope_for_mode).

These tests are pure (no DB, no network).  They mock the DB cursor where
needed (same pattern as other ingestion test files).

Run from repo root:
    uv run --with pytest --with "psycopg[binary]" python -m pytest ingestion/test_retrieval.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retrieval import (  # noqa: E402
    _retrieval_scope_for_mode,
    build_vector_sql,
    retrieve_top_k,
)


# ---------------------------------------------------------------------------
# build_vector_sql — book_slugs filter (CP-F4.3)
# ---------------------------------------------------------------------------

def test_book_slugs_adds_where_clause():
    """build_vector_sql includes a book_slug = ANY(...) clause when book_slugs is provided."""
    emb = "[0.1, 0.2, 0.3]"
    sql, params = build_vector_sql(
        emb, 10, set(), set(),
        content_types=None,
        book_slugs={"phb-5e", "xge-5e"},
    )
    assert "book_slug = ANY(%s)" in sql
    # The list of slugs appears somewhere in params
    param_list = list(params)
    found = any(
        isinstance(p, list) and set(p) == {"phb-5e", "xge-5e"}
        for p in param_list
    )
    assert found, f"Expected slug list in params, got: {params}"


def test_no_book_slugs_uses_unfiltered_sql():
    """build_vector_sql falls through to the unfiltered base query when no filters at all."""
    emb = "[0.1]"
    sql, params = build_vector_sql(emb, 10, set(), set())
    assert "WHERE" not in sql
    assert "book_slug" not in sql


def test_book_slugs_combined_with_content_types():
    """Both book_slug and content_type filters appear in the WHERE clause together."""
    emb = "[0.1]"
    sql, params = build_vector_sql(
        emb, 5, set(), set(),
        content_types={"spell"},
        book_slugs={"phb-5e"},
    )
    assert "content_type = ANY(%s)" in sql
    assert "book_slug = ANY(%s)" in sql


def test_book_slugs_none_does_not_add_clause():
    """book_slugs=None omits the book_slug filter (backward compatible)."""
    emb = "[0.1]"
    sql, params = build_vector_sql(
        emb, 5, set(), set(),
        content_types={"rule"},
        book_slugs=None,
    )
    assert "book_slug" not in sql
    assert "content_type = ANY(%s)" in sql


# ---------------------------------------------------------------------------
# _retrieval_scope_for_mode — mode→scope mapping (CP-F4.3)
# ---------------------------------------------------------------------------

def test_sage_scope_returns_query_ctypes_and_no_book_limit():
    ctypes, books = _retrieval_scope_for_mode("sage", {"rule"})
    assert ctypes == {"rule"}
    assert books is None


def test_sage_scope_no_query_ctypes_is_none():
    ctypes, books = _retrieval_scope_for_mode("sage", set())
    assert ctypes is None
    assert books is None


def test_spell_scope_forces_spell_ctype_and_limits_books():
    ctypes, books = _retrieval_scope_for_mode("spell", set())
    assert ctypes == {"spell"}
    assert "phb-5e" in books
    assert "dmg-5e" not in books
    assert "mm-5e" not in books


def test_spell_scope_overrides_query_derived_ctypes():
    """spell mode forces exactly {"spell"}, ignoring query-derived types."""
    ctypes, books = _retrieval_scope_for_mode("spell", {"class_feature", "monster"})
    assert ctypes == {"spell"}


def test_rules_scope_uses_intersection_when_non_empty():
    """rules mode: intersection of query ctypes and rules allowlist when non-empty."""
    ctypes, books = _retrieval_scope_for_mode("rules", {"rule", "monster"})
    assert "rule" in ctypes
    assert "monster" not in ctypes
    assert books is None


def test_rules_scope_falls_back_to_full_allowlist_when_intersection_empty():
    """rules mode: full allowlist when query ctypes don't overlap allowlist."""
    ctypes, books = _retrieval_scope_for_mode("rules", {"monster", "dm_guidance"})
    assert "rule" in ctypes
    assert "class_feature" in ctypes
    assert "monster" not in ctypes
    assert books is None


def test_gm_scope_merges_forced_ctypes_with_query_derived():
    ctypes, books = _retrieval_scope_for_mode("gm", {"spell"})
    assert "spell" in ctypes
    assert "monster" in ctypes
    assert "dm_guidance" in ctypes
    assert "magic_item" in ctypes
    assert books is None


def test_gm_scope_includes_forced_ctypes_when_no_query_ctypes():
    ctypes, books = _retrieval_scope_for_mode("gm", set())
    assert "monster" in ctypes
    assert "dm_guidance" in ctypes
    assert "magic_item" in ctypes
    assert books is None


# ---------------------------------------------------------------------------
# retrieve_top_k — book_slugs param threading (CP-F4.3)
# ---------------------------------------------------------------------------

class _FakeCursor:
    """Minimal cursor mock that records the last query+params and returns no rows."""
    def __init__(self):
        self.last_sql = None
        self.last_params = None

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params

    def fetchall(self):
        return []

    def __enter__(self): return self
    def __exit__(self, *_): pass


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def __enter__(self): return self
    def __exit__(self, *_): pass


def test_retrieve_top_k_passes_book_slugs_to_sql():
    """retrieve_top_k forwards book_slugs into build_vector_sql."""
    conn = _FakeConn()
    retrieve_top_k(
        conn,
        query_embedding=[0.1, 0.2],
        query_text="fireball",
        k=5,
        content_types={"spell"},
        book_slugs={"phb-5e"},
    )
    sql = conn.cursor_obj.last_sql
    params = conn.cursor_obj.last_params
    assert "book_slug = ANY(%s)" in sql
    # Confirm the slug list is somewhere in the params tuple
    assert any(
        isinstance(p, list) and "phb-5e" in p
        for p in params
    ), f"Expected phb-5e in params, got: {params}"


def test_retrieve_top_k_without_book_slugs_backward_compatible():
    """retrieve_top_k without book_slugs works as before (no book filter in SQL)."""
    conn = _FakeConn()
    retrieve_top_k(
        conn,
        query_embedding=[0.1, 0.2],
        query_text="basilisk",
        k=5,
        content_types={"monster"},
        book_slugs=None,
    )
    sql = conn.cursor_obj.last_sql
    assert "book_slug" not in sql
    assert "content_type = ANY(%s)" in sql

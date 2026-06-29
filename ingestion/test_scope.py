"""
Unit tests for ingestion/scope.py — the single canonical mode→scope mapping.

Pure (no DB, no network). Characterizes scope_for_mode for every mode plus a
full mode × query-ctype regression matrix, so the behavior is pinned to the
values that the two former copies (service/rag._scope_for_mode and
ingestion/retrieval._retrieval_scope_for_mode) produced before consolidation.

Run from repo root:
    uv run --with '.[test]' python -m pytest ingestion/test_scope.py -q
"""

from __future__ import annotations

from ingestion.scope import scope_for_mode


# ---------------------------------------------------------------------------
# Per-mode behavior
# ---------------------------------------------------------------------------

def test_spell_forces_spell_ctype_and_limits_to_spell_books():
    ctypes, books = scope_for_mode("spell", set())
    assert ctypes == {"spell"}
    assert books is not None
    assert "phb-5e" in books
    assert "dmg-5e" not in books  # DMG is not a spell-bearing book


def test_spell_overrides_query_derived_ctypes():
    ctypes, _ = scope_for_mode("spell", {"class_feature", "rule"})
    assert ctypes == {"spell"}


def test_rules_intersects_query_with_allowlist():
    # query has a rules ctype + a non-rules ctype → keep only the rules one
    ctypes, books = scope_for_mode("rules", {"rule", "monster"})
    assert ctypes == {"rule"}
    assert books is None


def test_rules_falls_back_to_full_allowlist_when_no_overlap():
    ctypes, books = scope_for_mode("rules", {"monster"})
    assert "monster" not in ctypes
    assert {"rule", "class_feature", "condition",
            "race_feature", "background", "feat"} == ctypes
    assert books is None


def test_rules_empty_query_yields_full_allowlist():
    ctypes, books = scope_for_mode("rules", set())
    assert ctypes == {"rule", "class_feature", "condition",
                      "race_feature", "background", "feat"}
    assert books is None


def test_gm_unions_query_with_forced_creative_ctypes():
    ctypes, books = scope_for_mode("gm", {"spell"})
    assert {"monster", "dm_guidance", "magic_item", "spell"} <= ctypes
    assert books is None


def test_gm_empty_query_is_just_forced_set():
    ctypes, books = scope_for_mode("gm", set())
    assert ctypes == {"monster", "dm_guidance", "magic_item"}
    assert books is None


def test_sage_passes_query_through_unmodified():
    q = {"rule", "class_feature"}
    ctypes, books = scope_for_mode("sage", q)
    assert ctypes == q
    assert books is None


def test_sage_empty_query_is_none():
    ctypes, books = scope_for_mode("sage", set())
    assert ctypes is None
    assert books is None


def test_unrecognised_mode_behaves_like_sage():
    q = {"feat"}
    assert scope_for_mode("unrecognised", q) == scope_for_mode("sage", q)
    assert scope_for_mode("totally-unknown", set()) == (None, None)


# ---------------------------------------------------------------------------
# Regression matrix — pins the exact (effective_ctypes, allowed_books) values
# the consolidated function must produce, mode × query-ctype.
# ---------------------------------------------------------------------------

_RULES = frozenset({"rule", "class_feature", "condition",
                    "race_feature", "background", "feat"})
_SPELL_BOOKS = frozenset({"phb-5e", "xge-5e", "tce-5e", "eepc-5e",
                          "scag-5e", "tortle-5e", "eberron-5e", "ravnica-5e"})
_GM_FORCED = frozenset({"monster", "dm_guidance", "magic_item"})

_QUERY_INPUTS = [
    set(),
    {"rule"},
    {"monster"},
    {"spell"},
    {"class_feature", "rule"},
    {"monster", "dm_guidance"},
    {"spell", "feat", "background"},
    {"unknown_ctype"},
]


def _expected(mode: str, q: set[str]):
    if mode == "spell":
        return {"spell"}, set(_SPELL_BOOKS)
    if mode == "rules":
        inter = q & _RULES
        return (inter if inter else set(_RULES)), None
    if mode == "gm":
        return (q | set(_GM_FORCED)), None
    return (q or None), None


def test_full_matrix_matches_expected():
    for mode in ["sage", "spell", "rules", "gm", "unrecognised"]:
        for q in _QUERY_INPUTS:
            assert scope_for_mode(mode, set(q)) == _expected(mode, set(q)), \
                f"mismatch for mode={mode!r} q={q!r}"


def test_does_not_mutate_caller_set():
    q = {"spell"}
    scope_for_mode("gm", q)
    assert q == {"spell"}, "scope_for_mode must not mutate the caller's set"

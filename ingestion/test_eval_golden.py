"""
Unit tests for eval_golden.py — entity extraction and SQL filter assembly.

Run:
    uv run --with "psycopg[binary]" --with openai python ingestion/test_eval_golden.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eval_golden import (
    extract_query_entities,
    extract_query_content_types,
    build_vector_sql,
    compute_metrics,
)


# ---------------------------------------------------------------------------
# Test vocabulary — mirrors what we'd pull from dnd.chunks at runtime
# ---------------------------------------------------------------------------

KNOWN_CLASSES = {"Fighter", "Wizard", "Cleric", "Rogue", "Paladin", "Bard"}
KNOWN_ENTITIES = {
    "Fireball", "Magic Missile", "Cure Wounds", "Shield", "Counterspell",
    "Healing Word", "Blinded", "Prone", "Paralyzed", "Restrained",
    "Elf", "Dwarf", "Halfling", "Human",
}
ENTITY_TO_CTYPE = {
    "Fireball": "spell", "Magic Missile": "spell", "Cure Wounds": "spell",
    "Shield": "spell", "Counterspell": "spell", "Healing Word": "spell",
    "Blinded": "condition", "Prone": "condition", "Paralyzed": "condition",
    "Restrained": "condition",
    "Elf": "race_feature", "Dwarf": "race_feature",
    "Halfling": "race_feature", "Human": "race_feature",
}
CLASS_TO_CTYPE = {c: "class_feature" for c in KNOWN_CLASSES}


# ---------------------------------------------------------------------------
# extract_query_entities
# ---------------------------------------------------------------------------

def test_extracts_class_from_query():
    classes, entities = extract_query_entities(
        "What saving throw proficiencies does a Wizard get?",
        KNOWN_CLASSES, KNOWN_ENTITIES,
    )
    assert "Wizard" in classes, f"Expected Wizard in classes, got {classes}"
    assert entities == set(), f"Expected no entities for class-only query, got {entities}"


def test_extracts_entity_from_query():
    classes, entities = extract_query_entities(
        "What is the range of Fireball?",
        KNOWN_CLASSES, KNOWN_ENTITIES,
    )
    assert "Fireball" in entities, f"Expected Fireball in entities, got {entities}"


def test_extracts_race_entity():
    classes, entities = extract_query_entities(
        "What ability score bonuses do Dwarves get?",
        KNOWN_CLASSES, KNOWN_ENTITIES,
    )
    # "Dwarves" → substring "Dwarf" matches
    assert "Dwarf" in entities, f"Expected Dwarf in entities, got {entities}"


def test_case_insensitive():
    classes, entities = extract_query_entities(
        "how does counterspell work?",
        KNOWN_CLASSES, KNOWN_ENTITIES,
    )
    assert "Counterspell" in entities


def test_no_match_returns_empty():
    classes, entities = extract_query_entities(
        "How does grappling work?",
        KNOWN_CLASSES, KNOWN_ENTITIES,
    )
    assert classes == set(), f"Expected no class match, got {classes}"
    assert entities == set(), f"Expected no entity match, got {entities}"


def test_multiword_entity_match():
    classes, entities = extract_query_entities(
        "What are the components of Cure Wounds?",
        KNOWN_CLASSES, KNOWN_ENTITIES,
    )
    assert "Cure Wounds" in entities


def test_word_boundary_avoids_partial_matches():
    # "Bard" should NOT match the substring inside "bardiche" or "bombard"
    # (defensive — neither exists in our golden set, but proves the boundary)
    classes, entities = extract_query_entities(
        "How does a bombard differ from a catapult?",
        KNOWN_CLASSES, KNOWN_ENTITIES,
    )
    assert "Bard" not in classes, f"Bard should not match 'bombard', got {classes}"


# ---------------------------------------------------------------------------
# build_vector_sql
# ---------------------------------------------------------------------------

def test_no_filters_returns_unfiltered_sql():
    sql, params = build_vector_sql(
        emb_str="[0.1, 0.2]", k=5,
        classes=set(), entities=set(),
    )
    assert "WHERE" not in sql, f"No filters → no WHERE clause, got: {sql}"
    assert len(params) == 3, f"Expected (emb, emb, k), got {len(params)} params"


def test_class_filter_adds_where():
    sql, params = build_vector_sql(
        emb_str="[0.1, 0.2]", k=5,
        classes={"Wizard"}, entities=set(),
    )
    assert "WHERE" in sql
    assert "class_name = ANY" in sql, f"Expected class_name filter in SQL: {sql}"
    # params: emb (top-level), class_list, emb (order), k
    assert any("Wizard" in str(p) for p in params), f"Wizard not in params: {params}"


def test_entity_filter_adds_where():
    sql, params = build_vector_sql(
        emb_str="[0.1, 0.2]", k=5,
        classes=set(), entities={"Fireball"},
    )
    assert "entity_name = ANY" in sql
    assert any("Fireball" in str(p) for p in params)


def test_class_and_entity_filter_uses_or():
    sql, params = build_vector_sql(
        emb_str="[0.1, 0.2]", k=5,
        classes={"Wizard"}, entities={"Fireball"},
    )
    # Both filters present → OR'd together so either match qualifies
    assert " OR " in sql, f"Expected OR between class/entity filters: {sql}"
    assert "class_name = ANY" in sql
    assert "entity_name = ANY" in sql


# ---------------------------------------------------------------------------
# compute_metrics — Hit@1, P@5, MRR, Recall@10
# ---------------------------------------------------------------------------

def test_metrics_all_hits():
    # All 10 results are hits → perfect score
    hits = [True] * 10
    m = compute_metrics(hits)
    assert m["hit_at_1"] is True
    assert m["precision_at_5"] == 1.0
    assert m["mrr"] == 1.0, f"Expected MRR=1.0 (first hit at rank 1), got {m['mrr']}"
    assert m["recall_at_10"] is True


def test_metrics_all_misses():
    hits = [False] * 10
    m = compute_metrics(hits)
    assert m["hit_at_1"] is False
    assert m["precision_at_5"] == 0.0
    assert m["mrr"] == 0.0, f"Expected MRR=0.0 (no hit), got {m['mrr']}"
    assert m["recall_at_10"] is False


def test_mrr_first_hit_at_rank_3():
    # Hit appears at rank 3 → MRR = 1/3
    hits = [False, False, True, False, False, False, False, False, False, False]
    m = compute_metrics(hits)
    assert m["hit_at_1"] is False
    assert m["precision_at_5"] == 0.2, f"Expected P@5=0.2 (1 hit in top 5), got {m['precision_at_5']}"
    assert abs(m["mrr"] - 1/3) < 1e-9, f"Expected MRR≈0.333, got {m['mrr']}"
    assert m["recall_at_10"] is True


def test_mrr_uses_first_hit_only():
    # Multiple hits, but MRR only credits the first one
    hits = [False, True, False, True, True, False, False, False, False, False]
    m = compute_metrics(hits)
    assert abs(m["mrr"] - 0.5) < 1e-9, f"Expected MRR=0.5 (first hit at rank 2), got {m['mrr']}"


def test_recall_at_10_finds_late_hit():
    # First hit is at rank 9 — Recall@10 still True, Hit@1 False, P@5 zero
    hits = [False] * 8 + [True, False]
    m = compute_metrics(hits)
    assert m["hit_at_1"] is False
    assert m["precision_at_5"] == 0.0
    assert m["recall_at_10"] is True
    assert abs(m["mrr"] - 1/9) < 1e-9


def test_metrics_handles_fewer_than_10_results():
    # If retrieval returned only 4 results (e.g. small corpus), metrics still work
    hits = [False, True, False, False]
    m = compute_metrics(hits)
    assert m["hit_at_1"] is False
    # P@5 normalizes against 5 even if fewer results — only 1 hit out of 5 possible slots
    assert m["precision_at_5"] == 0.2
    assert abs(m["mrr"] - 0.5) < 1e-9
    assert m["recall_at_10"] is True


def test_q13_simulation_filter_lifts_to_hit_at_1():
    # Simulates Q13 from the eval report: pre-filter, Wizard chunk was at rank 5.
    # Post-filter (from cl1), the Wizard chunk wins rank 1.
    pre_filter = [False, False, False, False, True, False, False, False, False, False]
    post_filter = [True, False, False, False, False, False, False, False, False, False]
    pre = compute_metrics(pre_filter)
    post = compute_metrics(post_filter)
    assert pre["hit_at_1"] is False and post["hit_at_1"] is True
    assert post["mrr"] > pre["mrr"], "MRR should improve when hit moves from rank 5 to rank 1"
    # Both should report Recall@10 = True since the chunk was found in either case
    assert pre["recall_at_10"] is True and post["recall_at_10"] is True


# ---------------------------------------------------------------------------
# extract_query_content_types (amp)
# ---------------------------------------------------------------------------

def test_ctype_from_matched_class():
    # "Wizard" → class_feature (via class vocab lookup)
    ctypes = extract_query_content_types(
        "What saving throw proficiencies does a Wizard get?",
        ENTITY_TO_CTYPE, CLASS_TO_CTYPE,
    )
    assert "class_feature" in ctypes, f"Expected class_feature, got {ctypes}"


def test_ctype_from_matched_spell_entity():
    # "Fireball" → spell (via entity vocab lookup)
    ctypes = extract_query_content_types(
        "What is the range of Fireball?",
        ENTITY_TO_CTYPE, CLASS_TO_CTYPE,
    )
    assert "spell" in ctypes


def test_ctype_from_matched_race_entity():
    ctypes = extract_query_content_types(
        "What ability score bonuses do Dwarves get?",
        ENTITY_TO_CTYPE, CLASS_TO_CTYPE,
    )
    assert "race_feature" in ctypes


def test_ctype_from_matched_condition_entity():
    ctypes = extract_query_content_types(
        "What does the Blinded condition do?",
        ENTITY_TO_CTYPE, CLASS_TO_CTYPE,
    )
    assert "condition" in ctypes


def test_ctype_from_spell_keyword_no_entity():
    # No spell name in the query, but "spell" keyword present → infer spell intent
    ctypes = extract_query_content_types(
        "What is a cantrip spell?",
        ENTITY_TO_CTYPE, CLASS_TO_CTYPE,
    )
    assert "spell" in ctypes, f"Keyword 'spell' should imply content_type=spell, got {ctypes}"


def test_ctype_from_condition_keyword():
    ctypes = extract_query_content_types(
        "Which conditions affect movement?",
        ENTITY_TO_CTYPE, CLASS_TO_CTYPE,
    )
    assert "condition" in ctypes


def test_ctype_empty_for_generic_rule_query():
    # No entity match, no keyword → empty set (falls through to no content_type filter)
    ctypes = extract_query_content_types(
        "How does grappling work?",
        ENTITY_TO_CTYPE, CLASS_TO_CTYPE,
    )
    assert ctypes == set(), f"Expected no content_type for generic rule query, got {ctypes}"


def test_ctype_multiple_when_query_mixes_signals():
    # Fighter (class_feature) + "spell" keyword → both content_types
    ctypes = extract_query_content_types(
        "Can a Fighter cast any spell?",
        ENTITY_TO_CTYPE, CLASS_TO_CTYPE,
    )
    assert "class_feature" in ctypes
    assert "spell" in ctypes


# ---------------------------------------------------------------------------
# build_vector_sql with content_types
# ---------------------------------------------------------------------------

def test_content_type_filter_alone():
    sql, params = build_vector_sql(
        emb_str="[0.1, 0.2]", k=5,
        classes=set(), entities=set(), content_types={"spell"},
    )
    assert "content_type = ANY" in sql, f"Expected content_type filter, got: {sql}"
    assert any("spell" in str(p) for p in params)


def test_class_and_content_type_filter_uses_and():
    # When both entity/class filter and content_type filter are present, the
    # content_type clause AND's the entity/class clause rather than widening it
    sql, params = build_vector_sql(
        emb_str="[0.1, 0.2]", k=5,
        classes={"Wizard"}, entities=set(), content_types={"class_feature"},
    )
    assert "class_name = ANY" in sql
    assert "content_type = ANY" in sql
    assert " AND " in sql, f"Expected AND between entity/class and content_type filters: {sql}"


def test_no_filters_still_unfiltered():
    # content_types empty → no WHERE clause (regression check)
    sql, params = build_vector_sql(
        emb_str="[0.1, 0.2]", k=5,
        classes=set(), entities=set(), content_types=set(),
    )
    assert "WHERE" not in sql


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _run():
    tests = [
        test_extracts_class_from_query,
        test_extracts_entity_from_query,
        test_extracts_race_entity,
        test_case_insensitive,
        test_no_match_returns_empty,
        test_multiword_entity_match,
        test_word_boundary_avoids_partial_matches,
        test_no_filters_returns_unfiltered_sql,
        test_class_filter_adds_where,
        test_entity_filter_adds_where,
        test_class_and_entity_filter_uses_or,
        test_metrics_all_hits,
        test_metrics_all_misses,
        test_mrr_first_hit_at_rank_3,
        test_mrr_uses_first_hit_only,
        test_recall_at_10_finds_late_hit,
        test_metrics_handles_fewer_than_10_results,
        test_q13_simulation_filter_lifts_to_hit_at_1,
        test_ctype_from_matched_class,
        test_ctype_from_matched_spell_entity,
        test_ctype_from_matched_race_entity,
        test_ctype_from_matched_condition_entity,
        test_ctype_from_spell_keyword_no_entity,
        test_ctype_from_condition_keyword,
        test_ctype_empty_for_generic_rule_query,
        test_ctype_multiple_when_query_mixes_signals,
        test_content_type_filter_alone,
        test_class_and_content_type_filter_uses_and,
        test_no_filters_still_unfiltered,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _run()

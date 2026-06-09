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
    build_vector_sql,
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

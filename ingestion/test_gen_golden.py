"""
Unit tests for gen_golden.py — golden-query templating (pure, no DB).

Run:
    uv run --with '.[test]' python -m pytest ingestion/test_gen_golden.py -q
"""

from __future__ import annotations


from ingestion.gen_golden import template_question, CATEGORY_FOR


def test_spell_template():
    q, cat = template_question("spell", "Fireball", seed=0)
    assert "Fireball" in q
    assert cat == "spell_lookup"


def test_monster_template():
    q, cat = template_question("monster", "Beholder", seed=0)
    assert "Beholder" in q
    assert cat == "monster"


def test_magic_item_template():
    q, cat = template_question("magic_item", "Bag Of Holding", seed=0)
    assert "Bag Of Holding" in q
    assert cat == "magic_item"


def test_feat_template():
    q, cat = template_question("feat", "Grappler", seed=0)
    assert "Grappler" in q
    assert cat == "feat"


def test_dm_guidance_template():
    q, cat = template_question("dm_guidance", "Traps", seed=0)
    assert "Traps" in q
    assert cat == "dm_guidance"


def test_rule_template():
    q, cat = template_question("rule", "Grappling", seed=0)
    assert "Grappling" in q
    assert cat == "rule"


def test_seed_varies_template_phrasing():
    # Different seeds pick different templates so the suite isn't monotonous
    phrasings = {template_question("spell", "Shield", seed=s)[0] for s in range(4)}
    assert len(phrasings) >= 2


def test_category_for_known_types():
    assert CATEGORY_FOR["spell"] == "spell_lookup"
    assert CATEGORY_FOR["monster"] == "monster"
    assert CATEGORY_FOR["magic_item"] == "magic_item"


def test_unknown_type_falls_back():
    q, cat = template_question("mystery", "Thing", seed=0)
    assert "Thing" in q
    assert cat == "general"

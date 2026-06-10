"""
Unit tests for gen_golden.py — golden-query templating (pure, no DB).

Run:
    uv run python ingestion/test_gen_golden.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gen_golden import template_question, CATEGORY_FOR


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


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _run()

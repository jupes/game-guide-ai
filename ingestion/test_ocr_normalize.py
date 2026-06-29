"""
Tests for ocr_normalize (agent-forge-harness-6om). The critical property: it fixes
the known PHB garbles WITHOUT corrupting clean English/D&D text.

Run from repos/rag-chat:
    uv run --with '.[test]' python -m pytest ingestion/test_ocr_normalize.py -q
"""

from __future__ import annotations

import sys

from ingestion.ocr_normalize import normalize_ocr


def test_fixes_level_variants():
    # final l misread as lowercase i, capital I, or slash
    assert normalize_ocr("your druid levei") == "your druid level"
    assert normalize_ocr("6th levei or higher") == "6th level or higher"
    assert normalize_ocr("at higher leveis") == "at higher levels"
    assert normalize_ocr("Leve/s") == "Levels"
    # 'level' / 'levels' already-correct must stay
    assert normalize_ocr("3rd level spell slots") == "3rd level spell slots"


def test_fixes_capital_i_for_l():
    assert normalize_ocr("At 15th leveI, your") == "At 15th level, your"
    assert normalize_ocr("materiaIs and animaIs") == "materials and animals"
    assert normalize_ocr("alI") == "all"
    assert normalize_ocr("iIIusion") == "illusion"
    assert normalize_ocr("you'lI") == "you'll"


def test_fixes_v_for_y_including_fused():
    assert normalize_ocr("Vou can") == "You can"
    assert normalize_ocr("Vour spell") == "Your spell"
    assert normalize_ocr("Voucreate four orbs") == "You create four orbs"
    assert normalize_ocr("YOllcan see") == "you can see"  # YOll->you, then? see below


def test_fixes_e_for_c_words():
    assert normalize_ocr("the ereature") == "the creature"
    assert normalize_ocr("ean't") == "can't"
    assert normalize_ocr("1 aetion") == "1 action"
    assert normalize_ocr("Vou ehoose") == "You choose"


def test_fixes_dice_and_misc():
    assert normalize_ocr("plummet lO feet") == "plummet 10 feet"
    assert normalize_ocr("roll IdlO") == "roll 1d10"
    assert normalize_ocr("the /ire spreads") == "the fire spreads"
    assert normalize_ocr("less lhan half") == "less than half"


# --- the important half: clean text must pass through UNCHANGED ---

def test_preserves_real_words_starting_with_capital_i():
    for w in ["Intelligence", "Initiative", "If", "It", "In", "Is", "I", "I'll", "Illusion"]:
        assert normalize_ocr(w) == w, w


def test_preserves_all_caps_headings_and_acronyms():
    for w in ["ILLUSION", "PHB", "DC", "AC", "FIREBALL", "STR"]:
        assert normalize_ocr(w) == w, w


def test_preserves_ordinary_prose():
    s = ("A bright streak flashes from your pointing finger to a point you choose "
         "within range and then blossoms into an explosion of flame.")
    assert normalize_ocr(s) == s


def test_empty_and_clean_dice_unchanged():
    assert normalize_ocr("") == ""
    assert normalize_ocr("roll 1d10 + 4 fire damage") == "roll 1d10 + 4 fire damage"


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

"""
Tests for ocr_normalize (agent-forge-harness-6om). The critical property: it fixes
the known PHB garbles WITHOUT corrupting clean English/D&D text.

Run from repos/game-guide-ai:
    uv run --with '.[test]' python -m pytest ingestion/test_ocr_normalize.py -q
"""

from __future__ import annotations


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


# --- the t->l family (PHB scan misreads 't' as 'l'; gated to book="phb-5e") ---

PHB = {"book": "phb-5e"}


def test_fixes_reported_greater_invisibility_chunk():
    # the user-reported corrupted chunk, end to end
    garbled = (
        "GREATER INVISIBILITY\n4th-level illusion\nCasting Time: I aclion\n"
        "Range: Touch\nComponents: V.5\nDuration: Concenlralion, up lo I minule\n"
        "You or a crealure you louch becomes invisible unlil lhe\n"
        "spell ends. Anylhing lhe largel is wearing or carrying is\n"
        "invisible as long as il is on lhe largel's person."
    )
    expected = (
        "GREATER INVISIBILITY\n4th-level illusion\nCasting Time: 1 action\n"
        "Range: Touch\nComponents: V, S\nDuration: Concentration, up to 1 minute\n"
        "You or a creature you touch becomes invisible until the\n"
        "spell ends. Anything the target is wearing or carrying is\n"
        "invisible as long as it is on the target's person."
    )
    assert normalize_ocr(garbled, **PHB) == expected


def test_vocab_pass_fixes_long_tail_words():
    assert normalize_ocr("Dexlerily saving lhrow", **PHB) == "Dexterity saving throw"
    assert normalize_ocr("Slrenglh and Conslilulion", **PHB) == "Strength and Constitution"
    assert normalize_ocr("makes an atlack roll", **PHB) == "makes an attack roll"
    assert normalize_ocr("belween lurns", **PHB) == "between turns"


def test_fixes_short_t_words():
    assert normalize_ocr("nol oul bul lwo", **PHB) == "not out but two"
    assert normalize_ocr("hil poinls al lhe end", **PHB) == "hit points at the end"
    assert normalize_ocr("alleasl 20 feel away", **PHB) == "at least 20 feet away"


def test_feet_foot_take_only_fixed_in_context():
    assert normalize_ocr("a range of 30 feel.", **PHB) == "a range of 30 feet."
    assert normalize_ocr("a 10-fool radius", **PHB) == "a 10-foot radius"
    assert normalize_ocr("for each fool of movement", **PHB) == "for each foot of movement"
    assert normalize_ocr("you lake 4d6 damage", **PHB) == "you take 4d6 damage"
    # genuine uses must survive
    assert normalize_ocr("do you feel lucky", **PHB) == "do you feel lucky"
    assert normalize_ocr("only a fool fights here", **PHB) == "only a fool fights here"
    assert normalize_ocr("beside the lake", **PHB) == "beside the lake"


def test_fixes_digit_one_before_time_units():
    assert normalize_ocr("Casting Time: I action", **PHB) == "Casting Time: 1 action"
    assert normalize_ocr("lasts I hour", **PHB) == "lasts 1 hour"
    # the pronoun I must survive
    assert normalize_ocr("I move away", **PHB) == "I move away"


def test_fixes_components_s_misread_as_5():
    assert normalize_ocr("Components: V.5", **PHB) == "Components: V, S"
    assert normalize_ocr("Components: V, 5", **PHB) == "Components: V, S"
    assert normalize_ocr("Components: V.S, M (a pinch of salt)", **PHB) == \
        "Components: V, S, M (a pinch of salt)"


def test_t_family_is_gated_to_phb():
    # without the phb book tag the t->l layer must not run
    assert normalize_ocr("lhe crealure") == "lhe crealure"
    assert normalize_ocr("lhe crealure", book="xge-5e") == "lhe crealure"


def test_vocab_pass_leaves_unknown_words_alone():
    # not in vocab and no single l->t repair in vocab -> untouched
    assert normalize_ocr("Melf's acid arrow", **PHB) == "Melf's acid arrow"


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

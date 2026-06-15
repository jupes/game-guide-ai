"""
Unit tests for extract_scan.py — synthetic LineItem streams, no PDFs needed.

Run:
    uv run python ingestion/test_extract_scan.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extract_scan import (
    BOOK_CONFIGS,
    DndChunk,
    LineItem,
    classify_content_type,
    extract_dmg_chunks,
    extract_mm_chunks,
    extract_supplement_chunks,
    group_spans_to_lines,
    is_caps_heading,
    is_type_line,
    normalize_entity_name,
    retag_monster_lore,
    split_paragraph_chunks,
)

MM_CFG = dict(BOOK_CONFIGS["mm-5e"], first_content_page=1)
DMG_CFG = dict(BOOK_CONFIGS["dmg-5e"], first_content_page=1)


# ---------------------------------------------------------------------------
# is_caps_heading / normalize_entity_name / split_paragraph_chunks
# ---------------------------------------------------------------------------

def test_caps_heading_accepts_monster_name():
    assert is_caps_heading("BASILISK", 13.0, 10.5)
    assert is_caps_heading("ANCIENT BLACK DRAGON", 11.6, 10.5)


def test_caps_heading_rejects_body_text():
    assert not is_caps_heading("Travelers sometimes find objects", 9.2, 10.5)


def test_caps_heading_rejects_small_size():
    # Margin-art caption: caps but tiny
    assert not is_caps_heading("BAG OF DEVOURING", 5.4, 10.5)


def test_caps_heading_rejects_long_lines():
    assert not is_caps_heading("NO ONE CARVES STATUES OF FRIGHTENED WARRIORS EVER AT ALL", 12.0, 10.5)


def test_caps_heading_rejects_numeric_garbage():
    assert not is_caps_heading("16 (+3) 8 (-1) 15 (+2)", 12.0, 10.5)


def test_normalize_entity_name():
    assert normalize_entity_name("ANCIENT BLACK DRAGON") == "Ancient Black Dragon"
    assert normalize_entity_name("DEEP GNOME  (SVIRFNEBLIN)") == "Deep Gnome (Svirfneblin)"


def test_split_paragraph_chunks_packs_greedily():
    lines = ["a" * 500, "b" * 500, "c" * 500]
    out = split_paragraph_chunks(lines, max_chars=1100)
    assert len(out) == 2
    assert out[0] == "a" * 500 + "\n" + "b" * 500
    assert out[1] == "c" * 500


# ---------------------------------------------------------------------------
# Monster Manual stream extraction
# ---------------------------------------------------------------------------

def _mm_stream() -> list[LineItem]:
    """Synthetic two-monster page: lore for Basilisk, stat blocks for both."""
    L = LineItem
    return [
        # Section heading + lore (right column of a previous page, etc.)
        L(1, 0, 13.0, "BASILISK"),
        L(1, 0, 9.2, "Travelers sometimes find objects that look like pieces of"),
        L(1, 0, 9.2, "remarkably lifelike stone carvings of wildlife and they wonder"),
        L(1, 0, 9.2, "about the past and the warnings of seasoned explorers nearby."),
        # Stat block: type line then anchor
        L(1, 1, 8.7, "Medium monstrosity, unaligned"),
        L(1, 1, 8.5, "Armor Class 15 (natural armor)"),
        L(1, 1, 8.5, "Hit Points 52 (8d8 + 16)"),
        L(1, 1, 8.5, "Speed 20ft."),
        L(1, 1, 8.5, "STR DEX CON INT WIS CHA"),
        L(1, 1, 8.4, "16 (+3) 8 (-1) 15 (+2) 2 (-4) 8 (-1) 7 (-2)"),
        L(1, 1, 8.5, "Challenge 3 (700 XP)"),
        L(1, 1, 8.7, "Petrifying Gaze. If a creature starts its turn within 30 feet"),
        L(1, 1, 8.5, "of the basilisk and the two of them can see each other the"),
        L(1, 1, 8.5, "basilisk can force a DC 12 Constitution saving throw."),
        # Next monster
        L(2, 0, 12.8, "BEHIR"),
        L(2, 0, 9.2, "The serpentine behir scrambles across cavern walls and squeezes"),
        L(2, 0, 9.2, "through tight passages in pursuit of prey it can swallow whole"),
        L(2, 0, 9.2, "with its huge crocodilian jaws and lightning breath attacks."),
        L(2, 1, 8.7, "Huge monstrosity, neutral evil"),
        L(2, 1, 8.5, "Armor Class 17 (natural armor)"),
        L(2, 1, 8.5, "Hit Points 168 (16d12 + 64)"),
        L(2, 1, 8.5, "Speed 50 ft., climb 40 ft."),
        L(2, 1, 8.5, "Challenge 11 (7,200 XP)"),
        L(2, 1, 8.7, "Lightning Breath (Recharge 5-6). The behir exhales a line of"),
        L(2, 1, 8.5, "lightning that is 20 feet long and 5 feet wide dealing damage."),
    ]


def test_mm_extracts_stat_blocks_per_monster():
    chunks = extract_mm_chunks(_mm_stream(), "mm-5e", "mm.pdf", MM_CFG)
    stats = [c for c in chunks if c.section == "Stat Block"]
    assert len(stats) == 2, f"Expected 2 stat blocks, got {[(c.entity_name, c.section) for c in chunks]}"
    by_name = {c.entity_name: c for c in stats}
    assert "Basilisk" in by_name and "Behir" in by_name
    assert "Armor Class 15" in by_name["Basilisk"].text
    assert "Hit Points 168" in by_name["Behir"].text


def test_mm_stat_block_includes_type_line():
    chunks = extract_mm_chunks(_mm_stream(), "mm-5e", "mm.pdf", MM_CFG)
    basilisk = next(c for c in chunks if c.entity_name == "Basilisk" and c.section == "Stat Block")
    assert "Medium monstrosity" in basilisk.text, "Italic type line should be pulled into the stat block"


def test_mm_lore_attaches_to_section():
    chunks = extract_mm_chunks(_mm_stream(), "mm-5e", "mm.pdf", MM_CFG)
    lore = [c for c in chunks if c.section == "Lore"]
    assert any(c.entity_name == "Basilisk" and "stone carvings" in c.text for c in lore)
    assert any(c.entity_name == "Behir" and "lightning breath" in c.text.lower() for c in lore)


def test_mm_all_chunks_are_monster_type():
    chunks = extract_mm_chunks(_mm_stream(), "mm-5e", "mm.pdf", MM_CFG)
    assert all(c.content_type == "monster" for c in chunks)


def test_mm_skips_tiny_margin_text():
    stream = [LineItem(1, 0, 5.3, "NO ONE CARVES STATUES OF FRIGHTENED")] + _mm_stream()
    chunks = extract_mm_chunks(stream, "mm-5e", "mm.pdf", MM_CFG)
    assert not any("CARVES STATUES" in c.text for c in chunks)


def test_mm_cross_column_ownership():
    """
    Basilisk layout: stat block fills the LEFT column; the monster's name
    heads the RIGHT column. Stream order (col 0 first) delivers the anchor
    before the heading — ownership must still bind to the same-page heading,
    not the previous monster.
    """
    L = LineItem
    stream = [
        # Previous monster, page 1
        L(1, 0, 13.0, "AZER"),
        L(1, 0, 8.7, "Medium elemental, lawful neutral"),
        L(1, 0, 8.5, "Armor Class 17 (natural armor, shield)"),
        L(1, 0, 8.5, "Hit Points 39 (6d8 + 12)"),
        L(1, 0, 8.5, "Speed 30 ft."),
        L(1, 0, 8.5, "Challenge 2 (450 XP)"),
        L(1, 0, 8.5, "Heated Body. A creature that touches the azer takes damage."),
        # Page 2: stat block in col 0, BASILISK heading tops col 1
        L(2, 0, 8.7, "Medium monstrosity, unaligned"),
        L(2, 0, 8.5, "Armor Class 15 (natural armor)"),
        L(2, 0, 8.5, "Hit Points 52 (8d8 + 16)"),
        L(2, 0, 8.5, "Speed 20ft."),
        L(2, 0, 8.5, "Challenge 3 (700 XP)"),
        L(2, 0, 8.7, "Petrifying Gaze. If a creature starts its turn within 30 feet"),
        L(2, 1, 13.0, "BASILISK"),
        L(2, 1, 9.2, "Travelers sometimes find objects that look like pieces of"),
        L(2, 1, 9.2, "remarkably lifelike stone carvings of wildlife in the wild."),
    ]
    chunks = extract_mm_chunks(stream, "mm-5e", "mm.pdf", MM_CFG)
    stats = {c.entity_name: c for c in chunks if c.section == "Stat Block"}
    assert "Basilisk" in stats, f"Expected Basilisk stat block, got {list(stats)}"
    assert "Armor Class 15" in stats["Basilisk"].text
    assert "Armor Class 17" in stats["Azer"].text


def test_mm_no_heading_falls_back_to_family_section():
    """Beholder case: no detectable heading near the stat block at all —
    it inherits the nearest preceding (family) section heading."""
    L = LineItem
    stream = [
        L(1, 0, 13.0, "BEHOLDERS"),
        L(1, 0, 9.2, "The grotesque spheroid bodies of beholders levitate silently"),
        L(1, 0, 9.2, "above their subterranean lairs watching everything always."),
        # Page 2: stat block with no heading anywhere
        L(2, 0, 8.7, "Large aberration, lawful evil"),
        L(2, 0, 8.5, "Armor Class 18 (natural armor)"),
        L(2, 0, 8.5, "Hit Points 180 (19d10 + 76)"),
        L(2, 0, 8.5, "Challenge 13 (10,000 XP)"),
        L(2, 0, 8.5, "Antimagic Cone. The beholder's central eye creates an area"),
        L(2, 0, 8.5, "of antimagic in a 150-foot cone where magic cannot function."),
    ]
    chunks = extract_mm_chunks(stream, "mm-5e", "mm.pdf", MM_CFG)
    stats = [c for c in chunks if c.section == "Stat Block"]
    assert len(stats) == 1
    assert stats[0].entity_name == "Beholders"
    assert "Antimagic Cone" in stats[0].text


def test_caps_heading_accepts_ocr_mixed_case():
    # OCR renders small-caps with stray lowercase: 'GoBLIN Boss'
    assert is_caps_heading("GoBLIN Boss", 13.8, 10.5)


# ---------------------------------------------------------------------------
# DMG stream extraction
# ---------------------------------------------------------------------------

def _dmg_stream() -> list[LineItem]:
    L = LineItem
    return [
        # Guidance section
        L(1, 0, 13.0, "FACTIONS AND ORGANIZATIONS"),
        L(1, 0, 9.1, "Temples guilds orders secret societies and colleges are"),
        L(1, 0, 9.1, "important forces in the social order of any civilization and"),
        L(1, 0, 9.1, "they give characters allies enemies and quests to pursue in"),
        L(1, 0, 9.1, "the wider world beyond any single adventure location."),
        # Magic item: caps name then rarity anchor
        L(2, 0, 8.0, "BAG OF DEVOURING"),
        L(2, 0, 9.3, "Wondrous item, very rare"),
        L(2, 0, 9.1, "This bag superficially resembles a bag of holding but is a"),
        L(2, 0, 9.1, "feeding orifice for a gigantic extradimensional creature."),
        L(2, 0, 9.1, "Turning the bag inside out closes the orifice completely."),
        # Second item
        L(2, 1, 8.0, "BAG OF HOLDING"),
        L(2, 1, 9.3, "Wondrous item, uncommon"),
        L(2, 1, 9.1, "This bag has an interior space considerably larger than its"),
        L(2, 1, 9.1, "outside dimensions roughly 2 feet in diameter at the mouth"),
        L(2, 1, 9.1, "and 4 feet deep. The bag can hold up to 500 pounds."),
    ]


def test_dmg_extracts_magic_items():
    chunks = extract_dmg_chunks(_dmg_stream(), "dmg-5e", "dmg.pdf", DMG_CFG)
    items = [c for c in chunks if c.content_type == "magic_item"]
    assert len(items) == 2, f"Expected 2 magic items, got {[(c.entity_name, c.content_type) for c in chunks]}"
    by_name = {c.entity_name: c for c in items}
    assert "Bag Of Devouring" in by_name
    assert "Bag Of Holding" in by_name
    assert "very rare" in by_name["Bag Of Devouring"].text
    assert "500 pounds" in by_name["Bag Of Holding"].text


def test_dmg_guidance_section_chunks():
    chunks = extract_dmg_chunks(_dmg_stream(), "dmg-5e", "dmg.pdf", DMG_CFG)
    guidance = [c for c in chunks if c.content_type == "dm_guidance"]
    assert len(guidance) >= 1
    assert guidance[0].entity_name == "Factions And Organizations"
    assert "secret societies" in guidance[0].text


def test_dmg_item_block_ends_at_next_item():
    chunks = extract_dmg_chunks(_dmg_stream(), "dmg-5e", "dmg.pdf", DMG_CFG)
    devouring = next(c for c in chunks if c.entity_name == "Bag Of Devouring")
    assert "500 pounds" not in devouring.text, "Bag of Holding text leaked into Bag of Devouring"


def test_dmg_rarity_line_without_caps_name_is_body():
    # A rarity-looking line NOT preceded by a caps name must not open an item
    L = LineItem
    stream = [
        L(1, 0, 13.0, "TREASURE TABLES"),
        L(1, 0, 9.1, "Wondrous item, rare items appear frequently on these tables and"),
        L(1, 0, 9.1, "the DM should roll to determine which one the players find here."),
        L(1, 0, 9.1, "These tables help the DM award treasure appropriate to the tier."),
    ]
    chunks = extract_dmg_chunks(stream, "dmg-5e", "dmg.pdf", DMG_CFG)
    assert not any(c.content_type == "magic_item" for c in chunks)


# ---------------------------------------------------------------------------
# group_spans_to_lines (pymupdf engine — pure grouping over span tuples)
# ---------------------------------------------------------------------------
# Span tuple shape mirrors what read_pdf_stream_fitz pulls from
# page.get_text("dict"): (x0, top, size, flags, text)

def test_spans_group_into_lines_by_column():
    # Two spans same y, left + right columns → two LineItems, correct cols
    spans = [
        (10.0, 100.0, 13.0, 16, "BASILISK"),      # left col (x0=10 < 300)
        (320.0, 100.0, 9.0, 4, "Travelers find"),  # right col (x0=320 > 300)
    ]
    lines = group_spans_to_lines(spans, page_num=5, page_width=600.0)
    by_col = {li.col: li for li in lines}
    assert by_col[0].text == "BASILISK" and by_col[0].page == 5
    assert by_col[1].text == "Travelers find"


def test_spans_same_line_merge_in_reading_order():
    # Three spans, same y, same column, left-to-right → one merged line
    spans = [
        (10.0, 50.0, 9.0, 4, "Armor"),
        (40.0, 50.0, 9.0, 4, "Class"),
        (70.0, 50.0, 9.0, 4, "15"),
    ]
    lines = group_spans_to_lines(spans, page_num=1, page_width=600.0)
    assert len(lines) == 1
    assert lines[0].text == "Armor Class 15"


def test_spans_dominant_size_per_line():
    # Mixed sizes on one line → dominant (most chars) wins
    spans = [
        (10.0, 50.0, 13.0, 16, "A"),           # 1 char at 13pt
        (20.0, 50.0, 9.0, 4, "long body text"), # many chars at 9pt
    ]
    lines = group_spans_to_lines(spans, page_num=1, page_width=600.0)
    assert lines[0].size == 9.0


def test_spans_bold_flag_when_majority_bold():
    # flags & 16 = bold; majority-bold line → bold True
    spans = [
        (10.0, 50.0, 13.0, 20, "RANGER"),  # flags 20 = 16(bold)+4(serif)
    ]
    lines = group_spans_to_lines(spans, page_num=1, page_width=600.0)
    assert lines[0].bold is True


def test_spans_not_bold_for_body():
    spans = [(10.0, 50.0, 9.0, 4, "ordinary body prose here")]  # flags 4 = serif only
    lines = group_spans_to_lines(spans, page_num=1, page_width=600.0)
    assert lines[0].bold is False


def test_spans_strip_control_bytes():
    spans = [(10.0, 50.0, 9.0, 4, "clean\x00text\x07here")]
    lines = group_spans_to_lines(spans, page_num=1, page_width=600.0)
    assert "\x00" not in lines[0].text and "\x07" not in lines[0].text


def test_spans_drop_empty_after_strip():
    spans = [(10.0, 50.0, 9.0, 4, "\x00\x07  ")]
    lines = group_spans_to_lines(spans, page_num=1, page_width=600.0)
    assert lines == []


def test_lineitem_bold_defaults_false():
    # Regression guard for review finding: positional construction must still work
    li = LineItem(1, 0, 9.0, "text")
    assert li.bold is False


# ---------------------------------------------------------------------------
# classify_content_type (F3 — within-book content classification)
# ---------------------------------------------------------------------------

def test_classify_monster_by_statblock():
    body = ("Medium monstrosity, unaligned\nArmor Class 15 (natural armor)\n"
            "Hit Points 52 (8d8 + 16)\nSpeed 20 ft.\nChallenge 3 (700 XP)")
    assert classify_content_type("Basilisk", body) == "monster"


def test_classify_spell_by_level_and_casting():
    body = ("3rd-level evocation\nCasting Time: 1 action\nRange: 150 feet\n"
            "Components: V, S, M\nA bright streak flashes to a point you choose.")
    assert classify_content_type("Fireball", body) == "spell"


def test_classify_spell_cantrip():
    body = ("Evocation cantrip\nCasting Time: 1 action\nRange: 120 feet\n"
            "You create three glowing darts of magical force.")
    assert classify_content_type("Fire Bolt", body) == "spell"


def test_classify_feat_by_prerequisite():
    body = ("Prerequisite: Dexterity 13 or higher\nYou have mastered techniques "
            "to take advantage of every drop in any enemy's guard, gaining benefits.")
    assert classify_content_type("Defensive Duelist", body) == "feat"


def test_classify_falls_back_to_rule():
    body = ("When you make a Dexterity (Stealth) check, you can choose to move "
            "carefully through the area, taking your time to avoid notice.")
    assert classify_content_type("Hiding", body) == "rule"


def test_classify_statblock_beats_spell_words():
    # A monster whose actions mention spells must still classify as monster
    body = ("Large dragon, chaotic evil\nArmor Class 19\nHit Points 256\n"
            "Spellcasting. The dragon can cast 3rd-level spells. Casting Time varies.")
    assert classify_content_type("Adult Red Dragon", body) == "monster"


# ---------------------------------------------------------------------------
# extract_supplement_chunks (F3 — generic mixed-content extractor)
# ---------------------------------------------------------------------------

SUPP_CFG = {
    "kind": "supplement", "first_content_page": 1,
    "min_body_pt": 7.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400,
}


def _supp_stream():
    """Synthetic supplement page mirroring real XGE layout: spell names are
    SMALL and NOT bold (9.3pt) — they must be found via the level-line anchor,
    not heading detection. A feat uses the Prerequisite anchor."""
    L = LineItem
    return [
        L(1, 0, 12.0, "SPELL DESCRIPTIONS", bold=True),
        # Spell 1: name is small + not bold (the real XGE case)
        L(1, 0, 9.3, "ABSORB ELEMENTS"),
        L(1, 0, 9.7, "1st-level abjuration"),
        L(1, 0, 10.0, "Casting Time: 1 reaction"),
        L(1, 0, 10.0, "Range: Self"),
        L(1, 0, 10.0, "The spell captures some of the incoming energy, lessening"),
        L(1, 0, 10.0, "its effect on you and storing it for your next melee attack."),
        # Spell 2
        L(1, 0, 9.3, "FIREBALL"),
        L(1, 0, 9.7, "3rd-level evocation"),
        L(1, 0, 10.0, "Casting Time: 1 action"),
        L(1, 0, 10.0, "Range: 150 feet"),
        L(1, 0, 10.0, "A bright streak flashes from your pointing finger to a point"),
        L(1, 0, 10.0, "you choose where it blossoms into an explosion of flame."),
        # A feat (Prerequisite anchor)
        L(1, 1, 9.3, "DEFENSIVE DUELIST"),
        L(1, 1, 10.0, "Prerequisite: Dexterity 13 or higher"),
        L(1, 1, 10.0, "When you are wielding a finesse weapon with which you are"),
        L(1, 1, 10.0, "proficient and another creature hits you, you can use your"),
        L(1, 1, 10.0, "reaction to add your proficiency bonus to your armor class."),
    ]


def test_supplement_extracts_spells_via_anchor():
    chunks = extract_supplement_chunks(_supp_stream(), "xge-5e", "xge.pdf", SUPP_CFG)
    by_name = {c.entity_name: c for c in chunks}
    assert "Absorb Elements" in by_name, f"got {list(by_name)}"
    assert by_name["Absorb Elements"].content_type == "spell"
    assert "Fireball" in by_name
    assert by_name["Fireball"].content_type == "spell"
    # The spell block stays intact (name + level + casting + description)
    assert "incoming energy" in by_name["Absorb Elements"].text


def test_supplement_extracts_feat_via_prereq_anchor():
    chunks = extract_supplement_chunks(_supp_stream(), "xge-5e", "xge.pdf", SUPP_CFG)
    by_name = {c.entity_name: c for c in chunks}
    assert "Defensive Duelist" in by_name
    assert by_name["Defensive Duelist"].content_type == "feat"


def test_supplement_spell_name_not_swallowed_by_prior_chunk():
    # Fireball's name must open its own chunk, not bleed into Absorb Elements
    chunks = extract_supplement_chunks(_supp_stream(), "xge-5e", "xge.pdf", SUPP_CFG)
    absorb = next(c for c in chunks if c.entity_name == "Absorb Elements")
    assert "Fireball" not in absorb.text


def test_supplement_chunk_carries_book_slug():
    chunks = extract_supplement_chunks(_supp_stream(), "xge-5e", "xge.pdf", SUPP_CFG)
    assert all(c.book_slug == "xge-5e" for c in chunks)
    assert all(c.text for c in chunks)


def _supp_stream_ocr_garbled():
    """Same shape as _supp_stream but the spell sub-header lines carry the PHB
    scan's OCR damage (c->e: 'evoeation'/'eantrip'; l->I: 'leveI'). The strict
    anchor misses these, so each spell silently merges into the prior chunk —
    the production Fireball bug (agent-forge-harness-7p3)."""
    L = LineItem
    return [
        L(1, 0, 12.0, "SPELL DESCRIPTIONS", bold=True),
        L(1, 0, 9.3, "DANCING LIGHTS"),
        L(1, 0, 9.7, "Evoeation eantrip"),               # c->e on school AND 'cantrip'
        L(1, 0, 10.0, "Casting Time: 1 aetion"),
        L(1, 0, 10.0, "Range: 120 feet"),
        L(1, 0, 10.0, "You create up to four torch-sized lights for the duration."),
        L(1, 0, 9.3, "FIREBALL"),
        L(1, 0, 9.7, "3rd-leveI evocation"),             # l->I on 'level'
        L(1, 0, 10.0, "Casting Time: 1 action"),
        L(1, 0, 10.0, "Range: 150 feet"),
        L(1, 0, 10.0, "A bright streak flashes from your pointing finger to a point."),
    ]


def test_supplement_spells_extracted_despite_ocr_garbled_anchor():
    # tracer: OCR damage on the level/school line must not lose the spell.
    chunks = extract_supplement_chunks(_supp_stream_ocr_garbled(), "phb-5e", "phb.pdf", SUPP_CFG)
    by = {c.entity_name: c for c in chunks}
    assert "Fireball" in by, f"got {list(by)}"
    assert by["Fireball"].content_type == "spell"
    assert "Dancing Lights" in by, f"got {list(by)}"
    assert by["Dancing Lights"].content_type == "spell"
    # and they did not merge into one blob
    assert "Fireball" not in by["Dancing Lights"].text


# ---------------------------------------------------------------------------
# is_type_line — the stat-block type/alignment line (skip for naming)
# ---------------------------------------------------------------------------

def test_type_line_detected():
    assert is_type_line("Large monstrosity, neutral evil")
    assert is_type_line("Medium humanoid (elf), chaotic good")
    assert is_type_line("Largemonstrosity, neutral evil")  # OCR ran the words together


def test_type_line_rejects_names_and_prose():
    assert not is_type_line("Banderhobb")
    assert not is_type_line("Ancient Red Dragon")
    assert not is_type_line("When a creature enters the area it takes fire damage")


# ---------------------------------------------------------------------------
# VGM/MTF monster-name binding via heading history (qg4.a)
# ---------------------------------------------------------------------------
# Real layout: bold NAME (≥11.5pt) → bold TYPE line → "Armor Class" anchor.
# Both name and type line trip is_heading, so the fix must recover the name
# from the recent-heading history, skipping the type line.

def _vgm_monster_stream():
    L = LineItem
    return [
        # Some lore prose precedes the block (non-bold body)
        L(1, 0, 9.5, "Legends tell of a dark tower in the Shadowfell where the", bold=False),
        L(1, 0, 9.5, "shadows sometimes reform, and banderhobbs roam at night.", bold=False),
        # Stat block: bold name, bold type line, then the anchor
        L(1, 1, 12.3, "BANDERHOBB", bold=True),
        L(1, 1, 10.1, "Large monstrosity, neutral evil", bold=True),
        L(1, 1, 8.6, "Armor Class 15 (natural armor)", bold=True),
        L(1, 1, 8.8, "Hit Points 84 (8d10 + 40)", bold=True),
        L(1, 1, 8.2, "Speed 30 ft.", bold=False),
        L(1, 1, 8.2, "Challenge 5 (1,800 XP)", bold=False),
        L(1, 1, 8.5, "Shadow Stealth. While in dim light or darkness, the", bold=False),
        L(1, 1, 8.5, "banderhobb can take the Hide action as a bonus action.", bold=False),
    ]


def test_vgm_monster_named_from_heading_not_type_line():
    chunks = extract_supplement_chunks(_vgm_monster_stream(), "vgm-5e", "vgm.pdf", SUPP_CFG)
    stat = next((c for c in chunks if c.content_type == "monster"), None)
    assert stat is not None, f"no monster chunk; got {[(c.entity_name, c.content_type) for c in chunks]}"
    assert stat.entity_name == "Banderhobb", f"expected Banderhobb, got {stat.entity_name!r}"
    # The type line and stat fields belong in the body
    assert "monstrosity" in stat.text.lower()
    assert "Armor Class 15" in stat.text


def test_vgm_type_line_not_a_separate_chunk():
    chunks = extract_supplement_chunks(_vgm_monster_stream(), "vgm-5e", "vgm.pdf", SUPP_CFG)
    assert not any((c.entity_name or "").lower().startswith("large monstrosity") for c in chunks)


def test_spell_naming_unaffected_by_fix():
    # Regression: spell names are non-bold, land in cur_lines, pop path still works
    chunks = extract_supplement_chunks(_supp_stream(), "xge-5e", "xge.pdf", SUPP_CFG)
    by = {c.entity_name: c for c in chunks}
    assert "Absorb Elements" in by and by["Absorb Elements"].content_type == "spell"
    assert "Fireball" in by and by["Fireball"].content_type == "spell"


def test_monster_with_nonbold_name_falls_back():
    # If the name isn't a bold heading, fall back to the line above the anchor
    L = LineItem
    stream = [
        L(1, 0, 9.0, "Goblin", bold=False),
        L(1, 0, 9.0, "Small humanoid (goblinoid), neutral evil", bold=False),
        L(1, 0, 8.6, "Armor Class 15 (leather armor, shield)", bold=False),
        L(1, 0, 8.8, "Hit Points 7 (2d6)", bold=False),
        L(1, 0, 8.5, "Nimble Escape. The goblin can take the Disengage or Hide", bold=False),
        L(1, 0, 8.5, "action as a bonus action on each of its turns in combat.", bold=False),
    ]
    chunks = extract_supplement_chunks(stream, "vgm-5e", "vgm.pdf", SUPP_CFG)
    stat = next((c for c in chunks if c.content_type == "monster"), None)
    assert stat is not None
    # Fallback names from the line above the anchor (type line), but at least the
    # block is captured and the name isn't a Challenge/garbage line.
    assert "Hit Points 7" in stat.text


# ---------------------------------------------------------------------------
# retag_monster_lore (nux) — a monster's lore prose should be content_type=monster
# ---------------------------------------------------------------------------

def _c(entity, ctype, text="some body text with enough words here to pass"):
    return DndChunk(
        chunk_id="x", book_slug="vgm-5e", source_file="v.pdf",
        page_start=1, page_end=1, part=None, chapter=None, section=None,
        content_type=ctype, entity_name=entity, class_name=None,
        feature_name=None, text=text,
    )


def test_retag_lore_sharing_entity_with_statblock():
    chunks = [
        _c("Froghemoth", "monster", "Armor Class 14 Hit Points 184 ..."),
        _c("Froghemoth", "rule", "The froghemoth is a monstrous amphibian that lurks ..."),
    ]
    out = retag_monster_lore(chunks)
    assert all(c.content_type == "monster" for c in out if c.entity_name == "Froghemoth")
    lore = next(c for c in out if "lurks" in c.text)
    assert lore.content_type == "monster"
    assert lore.section == "Lore"


def test_retag_leaves_unrelated_rule_chunks():
    chunks = [
        _c("Froghemoth", "monster", "Armor Class 14 ..."),
        _c("Grappling", "rule", "When you want to grab a creature you make an attack ..."),
    ]
    out = retag_monster_lore(chunks)
    grap = next(c for c in out if c.entity_name == "Grappling")
    assert grap.content_type == "rule"  # no matching stat block → unchanged


def test_retag_case_insensitive_entity_match():
    chunks = [
        _c("Death Kiss", "monster", "Armor Class 16 Hit Points 161 ..."),
        _c("death kiss", "rule", "A death kiss resembles a beholder but is a ..."),
    ]
    out = retag_monster_lore(chunks)
    assert all(c.content_type == "monster" for c in out)


def test_retag_noop_without_statblocks():
    chunks = [_c("Grappling", "rule"), _c("Hiding", "rule")]
    out = retag_monster_lore(chunks)
    assert all(c.content_type == "rule" for c in out)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

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

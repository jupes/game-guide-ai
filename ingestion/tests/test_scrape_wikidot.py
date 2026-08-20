"""
Unit tests for scrape_wikidot.py's parse_page() — pure, no network.

Synthetic wiki-markup HTML (project convention per test_extract_scan.py — no
checked-in scraped fixtures; also sidesteps any question about committing real
CC BY-SA page content into the repo as test data). Shapes mirror the real
dnd5e.wikidot.com DOM sampled during implementation (a plain <title> tag with
a " - DND 5th Edition" suffix, and a single <div id="page-content"> holding
the page body as a handful of <p> tags — no per-section markup to key off).

Run:
    uv run --with '.[test]' python -m pytest ingestion/tests/test_scrape_wikidot.py -q
"""

from __future__ import annotations

from ingestion.scrape_wikidot import parse_page

SPELL_HTML = """
<html><head><title>Fireball - DND 5th Edition</title></head>
<body><div id="page-content">
<p>Source: Player's Handbook</p>
<p><em>3rd-level evocation</em></p>
<p>A bright streak flashes from your pointing finger...</p>
</div></body></html>
"""

MONSTER_HTML = """
<html><head><title>Owlbear - DND 5th Edition</title></head>
<body><div id="page-content">
<p>Large monstrosity, unaligned</p>
<p><strong>Armor Class</strong> 13</p>
</div></body></html>
"""

CLASS_HTML = """
<html><head><title>Fighter - DND 5th Edition</title></head>
<body><div id="page-content">
<p>A master of martial combat, skilled with a variety of weapons and armor.</p>
</div></body></html>
"""

RACE_HTML = """
<html><head><title>Elf - DND 5th Edition</title></head>
<body><div id="page-content">
<p>Elves are a magical people of otherworldly grace.</p>
</div></body></html>
"""

EQUIPMENT_HTML = """
<html><head><title>Longsword - DND 5th Edition</title></head>
<body><div id="page-content">
<p><strong>Cost</strong> 15 gp <strong>Damage</strong> 1d8 slashing</p>
</div></body></html>
"""

FEAT_HTML = """
<html><head><title>Alert - DND 5th Edition</title></head>
<body><div id="page-content">
<p>Always on the lookout for danger, you gain the following benefits.</p>
</div></body></html>
"""

CONDITION_HTML = """
<html><head><title>Blinded - DND 5th Edition</title></head>
<body><div id="page-content">
<p>A blinded creature can't see and automatically fails any ability check that requires sight.</p>
</div></body></html>
"""

RULE_HTML = """
<html><head><title>Advantage and Disadvantage - DND 5th Edition</title></head>
<body><div id="page-content">
<p>Sometimes a special ability or spell tells you that you have advantage or disadvantage.</p>
</div></body></html>
"""

SPELL_URL = "https://dnd5e.wikidot.com/spell:fireball"


def test_spell_namespace_maps_to_spell_content_type():
    chunks = parse_page(SPELL_HTML, SPELL_URL, "spell")
    assert len(chunks) == 1
    assert chunks[0]["content_type"] == "spell"


def test_monster_namespace_maps_to_monster_content_type():
    chunks = parse_page(MONSTER_HTML, "https://dnd5e.wikidot.com/monster:owlbear", "monster")
    assert chunks[0]["content_type"] == "monster"


def test_class_namespace_maps_to_class_feature_not_bare_class():
    """The taxonomy has no bare 'class' content_type (extract.py:92-95 folds
    classes into class_feature) — caught as a High finding in plan review turn 2."""
    chunks = parse_page(CLASS_HTML, "https://dnd5e.wikidot.com/class:fighter", "class")
    assert chunks[0]["content_type"] == "class_feature"


def test_race_namespace_maps_to_race_feature_not_bare_race():
    chunks = parse_page(RACE_HTML, "https://dnd5e.wikidot.com/race:elf", "race")
    assert chunks[0]["content_type"] == "race_feature"


def test_equipment_namespace_folds_into_rule():
    """No distinct 'equipment' content_type exists anywhere in the taxonomy
    (retrieval.py's _CTYPE_KEYWORDS has no entry for it either) — mirrors
    extract.py:95's own PDF convention."""
    chunks = parse_page(EQUIPMENT_HTML, "https://dnd5e.wikidot.com/equipment:longsword", "equipment")
    assert chunks[0]["content_type"] == "rule"


def test_feat_condition_rule_namespaces_map_1_to_1():
    assert parse_page(FEAT_HTML, "https://dnd5e.wikidot.com/feat:alert", "feat")[0]["content_type"] == "feat"
    assert parse_page(CONDITION_HTML, "https://dnd5e.wikidot.com/condition:blinded",
                       "condition")[0]["content_type"] == "condition"
    assert parse_page(RULE_HTML, "https://dnd5e.wikidot.com/rule:advantage", "rule")[0]["content_type"] == "rule"


def test_entity_name_comes_from_the_title_tag_stripped_of_the_site_suffix():
    chunks = parse_page(SPELL_HTML, SPELL_URL, "spell")
    assert chunks[0]["entity_name"] == "Fireball"


def test_text_is_the_page_content_div_stripped_of_tags():
    chunks = parse_page(SPELL_HTML, SPELL_URL, "spell")
    text = chunks[0]["text"]
    assert "bright streak flashes" in text
    assert "<p>" not in text and "<em>" not in text


def test_provenance_fields_are_set_for_wiki_source():
    chunks = parse_page(SPELL_HTML, SPELL_URL, "spell")
    chunk = chunks[0]
    assert chunk["book_slug"] == "wikidot-5e"
    assert chunk["source_type"] == "wiki"
    assert chunk["source_url"] == SPELL_URL
    assert chunk["source_file"] == SPELL_URL
    assert chunk["license"] == "CC BY-SA 3.0"
    assert chunk["page_start"] is None
    assert chunk["page_end"] is None


def test_chunk_id_is_stable_across_reparses_of_unchanged_input():
    first = parse_page(SPELL_HTML, SPELL_URL, "spell")[0]["chunk_id"]
    second = parse_page(SPELL_HTML, SPELL_URL, "spell")[0]["chunk_id"]
    assert first == second


def test_chunk_id_differs_for_a_different_url():
    a = parse_page(SPELL_HTML, SPELL_URL, "spell")[0]["chunk_id"]
    b = parse_page(SPELL_HTML, "https://dnd5e.wikidot.com/spell:magic-missile", "spell")[0]["chunk_id"]
    assert a != b

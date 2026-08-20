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

import os
import uuid

import pytest

from ingestion.scrape_wikidot import (
    CLASS_SLUGS,
    EQUIPMENT_SLUGS,
    _extract_namespace_links,
    crawl_namespace,
    dedup_report,
    discover_urls,
    fetch_page,
    parse_page,
)

DSN = os.environ.get("DATABASE_URL") or None
needs_db = pytest.mark.skipif(DSN is None, reason="no DATABASE_URL (CI always sets it)")

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


# ---------------------------------------------------------------------------
# fetch_page — rate-limited, cached fetch layer (no real network in tests)
# ---------------------------------------------------------------------------


def test_fetch_page_cache_hit_returns_cached_content_without_calling_fetcher(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    url = "https://dnd5e.wikidot.com/spell:fireball"
    from ingestion.scrape_wikidot import _cache_path
    _cache_path(cache_dir, url).write_text("<html>cached</html>", encoding="utf-8")

    calls = []

    def _stub_fetcher(u: str) -> str:
        calls.append(u)
        return "<html>should not be used</html>"

    result = fetch_page(url, cache_dir, rate_limit_s=0.0, _fetcher=_stub_fetcher)

    assert result == "<html>cached</html>"
    assert calls == [], "cache hit must not call the fetcher"


def test_fetch_page_cache_miss_calls_fetcher_and_writes_the_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    url = "https://dnd5e.wikidot.com/spell:fireball"
    calls = []

    def _stub_fetcher(u: str) -> str:
        calls.append(u)
        return "<html>fresh</html>"

    result = fetch_page(url, cache_dir, rate_limit_s=0.0, _fetcher=_stub_fetcher)

    assert result == "<html>fresh</html>"
    assert calls == [url]
    # A second call must now hit the cache, not the fetcher again.
    result2 = fetch_page(url, cache_dir, rate_limit_s=0.0, _fetcher=_stub_fetcher)
    assert result2 == "<html>fresh</html>"
    assert calls == [url], "second call must be a cache hit"


# ---------------------------------------------------------------------------
# discover_urls — per-namespace page discovery
#
# spell/race/feat are discovered by fetching a real index page (verified live
# during implementation: /spells for spell:, /lineage for race's real
# "lineage:" prefix, the homepage itself for feat: — no dedicated feat index
# page exists). class and equipment have NO namespace-prefixed pages on this
# site at all (bare top-level slugs like /fighter, /armor instead), so they're
# enumerated directly from the real, confirmed slug lists rather than scraped.
# monster/condition/rule are not present on this site at all (no bestiary
# section, no page-tags hits, direct guesses 404) — dropped from this
# feature's wikidot crawl scope; see scrape_wikidot.py's module docstring.
# ---------------------------------------------------------------------------


def test_extract_namespace_links_dedupes_and_resolves_absolute_urls():
    html = (
        '<a href="/spell:fireball">Fireball</a>'
        '<a href="/spell:acid-splash">Acid Splash</a>'
        '<a href="/spell:fireball">Fireball</a>'  # duplicate, e.g. a "recently added" list
        '<a href="/monster:owlbear">Owlbear</a>'  # different namespace, must be excluded
    )
    links = _extract_namespace_links(html, "spell")
    assert links == [
        "https://dnd5e.wikidot.com/spell:fireball",
        "https://dnd5e.wikidot.com/spell:acid-splash",
    ]


def test_discover_urls_class_returns_the_confirmed_slug_list(tmp_path):
    urls = discover_urls("class", tmp_path)
    assert urls == [f"https://dnd5e.wikidot.com/{slug}" for slug in CLASS_SLUGS]
    assert "https://dnd5e.wikidot.com/fighter" in urls


def test_discover_urls_equipment_returns_the_confirmed_slug_list(tmp_path):
    urls = discover_urls("equipment", tmp_path)
    assert urls == [f"https://dnd5e.wikidot.com/{slug}" for slug in EQUIPMENT_SLUGS]
    assert "https://dnd5e.wikidot.com/weapons" in urls


def test_discover_urls_spell_fetches_the_index_page_and_extracts_links(tmp_path):
    def _stub_fetcher(url: str) -> str:
        assert url == "https://dnd5e.wikidot.com/spells"
        return '<a href="/spell:fireball">Fireball</a><a href="/spell:acid-splash">Acid Splash</a>'

    urls = discover_urls("spell", tmp_path, rate_limit_s=0.0, _fetcher=_stub_fetcher)
    assert urls == [
        "https://dnd5e.wikidot.com/spell:fireball",
        "https://dnd5e.wikidot.com/spell:acid-splash",
    ]


def test_discover_urls_race_uses_the_real_lineage_prefix(tmp_path):
    """The site calls races "lineage:" internally, not "race:" — confirmed
    live during implementation. Getting this wrong would silently discover
    zero race pages."""
    def _stub_fetcher(url: str) -> str:
        assert url == "https://dnd5e.wikidot.com/lineage"
        return '<a href="/lineage:elf">Elf</a>'

    urls = discover_urls("race", tmp_path, rate_limit_s=0.0, _fetcher=_stub_fetcher)
    assert urls == ["https://dnd5e.wikidot.com/lineage:elf"]


def test_discover_urls_raises_for_a_namespace_with_no_real_index_on_this_site(tmp_path):
    for namespace in ("monster", "condition", "rule"):
        with pytest.raises(ValueError, match=namespace):
            discover_urls(namespace, tmp_path)


# ---------------------------------------------------------------------------
# crawl_namespace — orchestration (discover_urls + fetch_page + parse_page)
# ---------------------------------------------------------------------------


def test_crawl_namespace_with_limit_zero_touches_no_network(tmp_path):
    """namespace='class'/'equipment' need no network for discover_urls (hard-
    coded slug lists) — limit=0 means the url list is sliced to empty before
    any fetch_page call, so this is a safe, fast, fully offline check that the
    orchestration wiring itself doesn't crash."""
    assert crawl_namespace("class", tmp_path, rate_limit_s=0.0, limit=0) == []


# ---------------------------------------------------------------------------
# dedup_report — visibility only, never filters (bead AC: "supplements rather
# than conflicts")
# ---------------------------------------------------------------------------

_INSERT_CHUNK = """
INSERT INTO dnd.chunks (chunk_id, book_slug, source_file, content_type, entity_name, text, embedding)
VALUES (%s, %s, %s, %s, %s, 'x', %s)
"""
_EMBEDDING = "[" + ",".join(["0"] * 1536) + "]"


@needs_db
def test_dedup_report_counts_overlaps_without_filtering_anything(tmp_path):
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        suffix = uuid.uuid4().hex[:8]
        wiki_id, phb_id, unique_id = f"w-{suffix}", f"p-{suffix}", f"u-{suffix}"
        conn.execute(_INSERT_CHUNK, (wiki_id, "wikidot-5e", "https://x", "spell",
                                      f"Fireball-{suffix}", _EMBEDDING))
        conn.execute(_INSERT_CHUNK, (phb_id, "phb-5e", "phb.pdf", "spell",
                                      f"Fireball-{suffix}", _EMBEDDING))
        # A wikidot-only spell — must NOT show up as an overlap.
        conn.execute(_INSERT_CHUNK, (unique_id, "wikidot-5e", "https://x", "spell",
                                      f"Unique-Wiki-Spell-{suffix}", _EMBEDDING))
        try:
            report = dedup_report(DSN)
        finally:
            conn.execute("DELETE FROM dnd.chunks WHERE chunk_id = ANY(%s)",
                          ([wiki_id, phb_id, unique_id],))

    overlap_names = {o["entity_name"] for o in report["overlaps"]}
    assert f"Fireball-{suffix}" in overlap_names
    assert f"Unique-Wiki-Spell-{suffix}" not in overlap_names
    assert report["total_wikidot_chunks"] >= 2

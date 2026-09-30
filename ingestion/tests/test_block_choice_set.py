"""Invariants of the block-choice labelled set (agent-forge-harness-lnf).

The set is the ground truth the decision benchmark scores against, so these tests hold what
the research asks of it (docs/forge/research/jev-decision-model-evaluation.md §5.2): at least
400 answers over {stat_block, spell_card, none}, at least 50 adversarial items, labels checked
against the service's own card schemas, and no licensed book text.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from ingestion import build_block_choice_set as builder
from service.generate import _looks_like_statblock
from service.models import SpellContent, StatBlockContent

INGESTION = Path(builder.__file__).resolve().parent


@pytest.fixture(scope="module")
def items() -> list[dict]:
    with builder.OUT_PATH.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_the_committed_set_is_exactly_what_the_builder_produces():
    assert builder.OUT_PATH.read_text(encoding="utf-8") == builder.to_jsonl(builder.build(builder.load_srd_spells()))


def test_size_labels_and_ids(items):
    assert len(items) >= 400
    assert len({it["id"] for it in items}) == len(items)
    assert len({it["answer"] for it in items}) == len(items)  # no duplicated answer text
    counts = Counter(it["label"] for it in items)
    assert set(counts) == set(builder.LABELS)
    assert min(counts.values()) >= 100
    assert {it["mode"] for it in items} == {"sage", "spell", "rules", "gm"}


def test_at_least_fifty_adversarial_items_all_labelled_none(items):
    adversarial = [it for it in items if it["subset"] == "adversarial"]
    assert len(adversarial) >= 50
    assert {it["label"] for it in adversarial} == {"none"}
    # The kinds the research names: prose quoting AC and HP, multi-spell lists, non-answers,
    # injected instructions, and out-of-scope text.
    assert {"prose_ac_hp", "multi_spell_list", "non_answer", "prompt_injection", "out_of_scope"} <= \
        {it["category"] for it in adversarial}


def test_positives_carry_a_card_that_validates_against_the_service_schema(items):
    for it in items:
        if it["label"] == "stat_block":
            card = StatBlockContent.model_validate(it["gold"])
            assert card.name in it["answer"], it["id"]
            assert re.search(rf"\b{card.ac}\b", it["answer"]) and re.search(rf"\b{card.hp}\b", it["answer"]), it["id"]
        elif it["label"] == "spell_card":
            card = SpellContent.model_validate(it["gold"])
            assert card.name in it["answer"], it["id"]
            assert card.description[:80] in it["answer"], it["id"]
        else:
            assert it["gold"] is None, it["id"]


def test_sources_and_licences_are_declared(items):
    for it in items:
        assert it["source"] in builder.LICENCES, it["id"]
        assert it["licence"] == builder.LICENCES[it["source"]], it["id"]
        if it["source"] == builder.WIKIDOT_SRD:
            assert it["source_url"].startswith("https://dnd5e.wikidot.com/spell:"), it["id"]
            assert it["gold"]["name"] in builder.SRD_SPELLS, it["id"]
        else:
            assert it["source_url"] is None, it["id"]


def test_no_book_names_leak_through(items):
    for it in items:
        for book in ("Player's Handbook", "Xanathar", "Tasha", "Unearthed Arcana", "Monster Manual"):
            assert book not in it["answer"], (it["id"], book)


SHINGLE_WORDS = 12

# Rules notation that every 5e stat block and spell shares, word for word, with the SRD 5.1
# (CC BY 4.0): the weapon-attack line, the save-for-half line, the higher-slot scaling line and
# the spell header fields. A synthetic answer uses them as notation, so they are cut out before
# comparing; everything else a synthetic answer says has to be its own words.
SRD_NOTATION = [
    re.compile(r"(melee|ranged) weapon attack: [+-]\d+ to hit, (reach|range) [^,]+, one target\. "
               r"hit: \d+ \([^)]+\) \w+ damage\."),
    re.compile(r"saving throw, taking \d+d\d+ \w+ damage on a failed save, or half as much on a successful one\."),
    re.compile(r"when you cast this spell using a spell slot of \w+ level or higher, the damage increases by 1d6 "
               r"for each slot level above \w+\."),
    re.compile(r"casting time:.*?duration:[^\n]*", re.DOTALL),
]


def _shingles(text: str, n: int = SHINGLE_WORDS) -> set[str]:
    shingles: set[str] = set()
    for segment in re.split(r"\n|\|", text):
        words = re.findall(r"[a-z0-9]+", segment.lower())
        shingles |= {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}
    return shingles


def _without_notation(text: str) -> str:
    text = text.lower()
    for pattern in SRD_NOTATION:
        text = pattern.sub("\n", text)
    return text


def test_synthetic_items_share_no_twelve_word_run_with_the_licensed_book_corpora(items):
    """Synthetic text is written for this repo; none of it may be lifted from a licensed book.
    Every licensed book chunk file in ingestion/ is scanned for any 12-word run a synthetic
    answer contains, once the shared SRD rules notation is cut out."""
    ours: dict[str, str] = {}
    for it in items:
        if it["source"] == builder.SYNTHETIC:
            for sh in _shingles(_without_notation(it["answer"])):
                ours.setdefault(sh, it["id"])
    books = sorted(p for p in INGESTION.glob("chunks*.jsonl") if "wikidot" not in p.name)
    assert books, "the licensed book chunk files are expected in ingestion/"
    hits = []
    for path in books:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                # the book side is one word stream: PDF line breaks fall mid-sentence
                book_text = json.loads(line)["text"].replace("\n", " ").replace("|", " ")
                for sh in _shingles(book_text) & ours.keys():
                    hits.append((path.name, ours[sh], sh))
    assert hits == []


def test_srd_allowlist_is_honoured_by_the_loader():
    spells = builder.load_srd_spells()
    assert len(spells) >= 150
    assert all(sp["name"] in builder.SRD_SPELLS and sp["source"] == "Player's Handbook" for sp in spells)


def test_wikidot_spell_parser_reads_every_field():
    text = ("Source: Player's Handbook 2nd-level transmutation (ritual) Casting Time: 1 action Range: 30 feet "
            "Components: V, S, M (a pinch of powdered iron) Duration: Concentration, up to 1 minute You cause a "
            "creature to grow. At Higher Levels. It grows more. Spell Lists. Sorcerer , Wizard")
    sp = builder.parse_wikidot_spell(text)
    assert sp is not None
    assert (sp["level"], sp["school"], sp["ritual"], sp["concentration"]) == (2, "transmutation", True, True)
    assert sp["components"] == {"v": True, "s": True, "m": "a pinch of powdered iron"}
    assert (sp["casting_time"], sp["range"], sp["duration"]) == ("1 action", "30 feet", "Concentration, up to 1 minute")
    assert sp["description"] == "You cause a creature to grow."
    assert sp["higher_levels"] == "It grows more."
    assert sp["classes"] == ["Sorcerer", "Wizard"]
    assert builder.parse_wikidot_spell("Some feat text with no spell layout") is None


def test_adversarial_categories_actually_stress_the_heuristic(items):
    """The items are built to probe known failure modes of _looks_like_statblock: the AC/HP
    prose must fire it (false positives to veto) and CR-less abbreviated blocks must not
    (false negatives to catch)."""
    for it in items:
        if it["category"] in ("prose_ac_hp", "multi_creature_summary"):
            assert _looks_like_statblock(it["answer"]), it["id"]
        if it["category"] == "abbreviated_block" and "· CR " not in it["answer"]:
            assert not _looks_like_statblock(it["answer"]), it["id"]

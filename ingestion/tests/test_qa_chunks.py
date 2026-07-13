"""
Unit tests for qa_chunks.py — pre-embedding data-quality gate.

Samples are drawn from the real PDF-reader failures observed during corpus
expansion (Wayfinders PUA glyphs, Tortle CID codes, Xanathar's junk OCR layer,
OCR-mangled entity names).

Run:
    uv run --with '.[test]' python -m pytest ingestion/test_qa_chunks.py -q
"""

from __future__ import annotations

import json

from ingestion.qa_chunks import (
    alpha_ratio,
    classify_chunk,
    detect_collapse,
    entity_name_ok,
    has_cid_marker,
    length_ok,
    pua_control_ratio,
    run_qa,
    salvage_entity_name,
)


def _chunk(text, entity_name="Goblin", content_type="monster"):
    return {
        "chunk_id": "x", "book_slug": "test", "source_file": "t.pdf",
        "page_start": 1, "page_end": 1, "part": None, "chapter": None,
        "section": None, "content_type": content_type,
        "entity_name": entity_name, "class_name": None, "feature_name": None,
        "text": text,
    }


# A real-shaped clean chunk
GOOD = (
    "Basilisk\nMedium monstrosity, unaligned\nArmor Class 15 (natural armor)\n"
    "Hit Points 52 (8d8 + 16)\nPetrifying Gaze. If a creature starts its turn "
    "within 30 feet of the basilisk it must make a DC 12 save."
)


# ---------------------------------------------------------------------------
# pua_control_ratio
# ---------------------------------------------------------------------------

def test_pua_ratio_zero_for_clean():
    assert pua_control_ratio(GOOD) == 0.0


def test_pua_ratio_high_for_wayfinders_glyphs():
    # Wayfinders custom-font body renders as Private-Use-Area code points
    text = "MMAAGGIICC  "
    assert pua_control_ratio(text) > 0.2


# ---------------------------------------------------------------------------
# has_cid_marker
# ---------------------------------------------------------------------------

def test_cid_marker_detected():
    # Tortle via pdfplumber (pre-pymupdf): undecoded CID font
    assert has_cid_marker("(cid:107)(cid:21)(cid:19)(cid:20) Wizards of the Coast")


def test_cid_marker_absent_in_clean():
    assert not has_cid_marker(GOOD)


# ---------------------------------------------------------------------------
# alpha_ratio
# ---------------------------------------------------------------------------

def test_alpha_ratio_high_for_clean():
    assert alpha_ratio(GOOD) > 0.7


def test_alpha_ratio_low_for_junk_ocr():
    # Xanathar's junk HiddenHorzOCR layer (pdfplumber read)
    junk = "\\. 1 , .,. c, .,.,.o~sl<r. Ar< \"\\o\"'° ~oi\"'~ lo lr\"\\ lo kil("
    assert alpha_ratio(junk) < 0.5


# ---------------------------------------------------------------------------
# length_ok
# ---------------------------------------------------------------------------

def test_length_ok_for_normal():
    assert length_ok(GOOD, max_chars=1800)


def test_length_rejects_fragment():
    assert not length_ok("Too short", max_chars=1800)


def test_length_rejects_runaway():
    assert not length_ok("word " * 500, max_chars=1800)


# ---------------------------------------------------------------------------
# entity_name_ok
# ---------------------------------------------------------------------------

def test_entity_name_none_is_ok():
    # rule/lore chunks legitimately have no entity
    assert entity_name_ok(None)


def test_entity_name_clean_ok():
    assert entity_name_ok("Ancient Red Dragon")


def test_entity_name_rejects_ocr_garbage():
    # OCR-mangled names with digits/punctuation
    assert not entity_name_ok("0Rog")
    assert not entity_name_ok("DUE;&GAR")


def test_entity_name_rejects_field_and_section_labels():
    # Stat-field / section labels leaked through as entity_names in the PHB spell
    # section (fault D). They must be quarantined at the QA gate.
    assert entity_name_ok("Components: V, S") is False
    assert entity_name_ok("Casting Time: 1 action") is False
    assert entity_name_ok("Duration") is False
    assert entity_name_ok("Spell Descriptions") is False
    assert entity_name_ok("At Higher Levels") is False


def test_entity_name_rejects_sentence_fragments():
    # Body lines popped as names ("Stunned Until The End Of Its Next Turn",
    # "Increases By I For Each Slot Level Above 5Th.") — not entity names.
    assert entity_name_ok("Stunned Until The End Of Its Next Turn") is False
    assert entity_name_ok("Increases By I For Each Slot Level Above 5Th.") is False
    assert entity_name_ok("General Nature Of The Danger Posed By A Trap You Sense.") is False
    # but a real multi-word name is fine
    assert entity_name_ok("Flaming Sphere") is True
    assert entity_name_ok("Otiluke's Resilient Sphere") is True


def test_entity_name_rejects_overlong():
    assert not entity_name_ok("A" * 60)


# ---------------------------------------------------------------------------
# classify_chunk — integration of the validators
# ---------------------------------------------------------------------------

def test_classify_passes_clean_chunk():
    ok, reasons = classify_chunk(_chunk(GOOD))
    assert ok is True and reasons == []


def test_classify_quarantines_pua():
    ok, reasons = classify_chunk(_chunk("MMAA    more"))
    assert ok is False and "pua_control" in reasons


def test_classify_quarantines_cid():
    ok, reasons = classify_chunk(_chunk("(cid:107)(cid:21)(cid:19) some words here padding it out"))
    assert ok is False and "cid" in reasons


def test_classify_quarantines_junk_ocr():
    junk = "\\. 1 , .,. c, .,.,.o~sl<r. Ar< \"\\o\"'° ~oi\"'~ lo lr\"\\ lo kil("
    ok, reasons = classify_chunk(_chunk(junk))
    assert ok is False and "low_alpha" in reasons


def test_classify_quarantines_fragment():
    ok, reasons = classify_chunk(_chunk("Two words"))
    assert ok is False and "length" in reasons


def test_classify_quarantines_bad_entity():
    ok, reasons = classify_chunk(_chunk(GOOD, entity_name="0Rog"))
    assert ok is False and "bad_entity" in reasons


def test_classify_multiple_reasons():
    ok, reasons = classify_chunk(_chunk("(cid:1) ", entity_name="DUE;&GAR"))
    assert ok is False
    assert len(reasons) >= 2


# ---------------------------------------------------------------------------
# detect_collapse — corpus-wide regression guard (CP-D)
# ---------------------------------------------------------------------------

def test_detect_collapse_flags_multiple_statblocks_under_one_entity():
    # The 'Giants' family-collapse: several Armor Class stat blocks under one name.
    chunks = [
        _chunk("Cloud Giant\nHuge giant\nArmor Class 14 (natural armor)", entity_name="Giants"),
        _chunk("Fire Giant\nHuge giant\nArmor Class 18 (plate)", entity_name="Giants"),
        _chunk("Frost Giant\nHuge giant\nArmor Class 15 (patchwork armor)", entity_name="Giants"),
        # a single monster with lots of lore but ONE stat block — must NOT flag
        _chunk("Demilich\nArmor Class 20 (natural armor)", entity_name="Demilich"),
        _chunk("The demilich's lore continues across several chunks of prose.", entity_name="Demilich"),
    ]
    off = {o["entity"] for o in detect_collapse(chunks)}
    assert "Giants" in off, off
    assert "Demilich" not in off, off


def test_detect_collapse_flags_merged_spells():
    # A blob holding two spells (two Casting Time lines) under one (junk) name.
    chunks = [
        _chunk("3rd-level divination\nCasting Time: 1 action\nYou sense traps.",
               entity_name="General Nature Of The Danger", content_type="spell"),
        _chunk("7th-level necromancy\nCasting Time: 1 action\nYou send negative energy.",
               entity_name="General Nature Of The Danger", content_type="spell"),
    ]
    off = {o["entity"] for o in detect_collapse(chunks)}
    assert "General Nature Of The Danger" in off, off


def test_detect_collapse_clean_corpus_has_no_offenders():
    chunks = [
        _chunk("Fireball\n3rd-level evocation\nCasting Time: 1 action\nA bright streak.",
               entity_name="Fireball", content_type="spell"),
        _chunk("Goblin\nSmall humanoid\nArmor Class 15 (leather armor)", entity_name="Goblin"),
    ]
    assert detect_collapse(chunks) == []


# ---------------------------------------------------------------------------
# salvage_entity_name (agent-forge-harness-wu1) — a chunk failing ONLY
# bad_entity keeps its otherwise-clean text with entity_name nulled, instead
# of the whole chunk being discarded. Multi-reason failures (Tortle's
# cipher-garbage class) must never be salvaged.
# ---------------------------------------------------------------------------

def test_salvage_recovers_bad_entity_only_chunk():
    salvaged = salvage_entity_name(_chunk(GOOD, entity_name="0Rog"))
    assert salvaged is not None
    assert salvaged["entity_name"] is None
    ok, reasons = classify_chunk(salvaged)
    assert ok is True and reasons == []


def test_salvage_refuses_multi_reason_failure():
    # Tortle-class cipher garbage: bad_entity AND low_alpha together — the QA
    # gate must keep quarantining this, not salvage it away.
    junk = "\\. 1 , .,. c, .,.,.o~sl<r. Ar< \"\\o\"'° ~oi\"'~ lo lr\"\\ lo kil("
    assert salvage_entity_name(_chunk(junk, entity_name="0Rog")) is None


def test_salvage_refuses_already_clean_chunk():
    assert salvage_entity_name(_chunk(GOOD)) is None


def test_salvage_refuses_other_single_reason_failure():
    # A chunk failing only on length (a fragment) is not a bad_entity case —
    # salvage must not paper over unrelated failures.
    assert salvage_entity_name(_chunk("Two words")) is None


# ---------------------------------------------------------------------------
# run_qa — salvage wired into the file-level QA runner
# ---------------------------------------------------------------------------

def test_run_qa_writes_salvaged_chunk_to_clean_with_null_entity(tmp_path):
    in_path = tmp_path / "chunks-test.jsonl"
    lines = [
        json.dumps(_chunk(GOOD, entity_name="0Rog")),   # bad_entity only -> salvaged
        json.dumps(_chunk(GOOD)),                        # already clean
    ]
    in_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    clean_path = tmp_path / "chunks-test.clean.jsonl"
    quarantine_path = tmp_path / "chunks-test.quarantine.jsonl"

    report = run_qa(in_path, clean_path, quarantine_path)

    assert report["clean"] == 2
    assert report["quarantined"] == 0
    clean_chunks = [json.loads(line) for line in clean_path.read_text(encoding="utf-8").splitlines()]
    assert any(c["entity_name"] is None for c in clean_chunks)


def test_run_qa_still_quarantines_multi_reason_failure(tmp_path):
    in_path = tmp_path / "chunks-test.jsonl"
    junk = "\\. 1 , .,. c, .,.,.o~sl<r. Ar< \"\\o\"'° ~oi\"'~ lo lr\"\\ lo kil("
    in_path.write_text(json.dumps(_chunk(junk, entity_name="0Rog")) + "\n", encoding="utf-8")
    clean_path = tmp_path / "chunks-test.clean.jsonl"
    quarantine_path = tmp_path / "chunks-test.quarantine.jsonl"

    report = run_qa(in_path, clean_path, quarantine_path)

    assert report["clean"] == 0
    assert report["quarantined"] == 1


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

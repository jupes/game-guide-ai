"""
Structure-driven extractor for OCR-scanned D&D books (Monster Manual, DMG).

The font-tier approach in extract.py works for born-digital PDFs with crisp
heading sizes (PHB Basic: 24/20/12/10pt). OCR'd scans drift (a monster name
renders anywhere from 11.6 to 13.4pt) and carry recognition noise, so this
extractor anchors on *textual structure* instead:

    Monster Manual   stat block anchor:  "Armor Class <n>" — the ALL-CAPS
                     name sits 1-3 lines above (skipping the italic
                     size/type/alignment line). Lore prose attaches to the
                     nearest preceding section heading.

    DMG              magic item anchor:  the rarity line ("Wondrous item,
                     very rare") — the caps item name sits immediately above.
                     Everything else chunks as dm_guidance under caps section
                     headings.

Engine: pymupdf (fitz) by default — it decodes books pdfplumber can't read
(Xanathar's junk OCR layer, Tortle's CID fonts) and exposes bold flags. The
old pdfplumber reader stays available via --engine pdfplumber.

Usage:
    uv run --with pymupdf python ingestion/extract_scan.py <pdf> --book-slug mm-5e
    uv run --with pymupdf python ingestion/extract_scan.py <pdf> --book-slug dmg-5e
    uv run --with pdfplumber python ingestion/extract_scan.py <pdf> --book-slug mm-5e --engine pdfplumber

Output: JSONL with the same DndChunk schema as extract.py (embed.py-ready).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from itertools import groupby
from pathlib import Path

from ocr_normalize import normalize_ocr

# ---------------------------------------------------------------------------
# Shared schema (mirrors extract.py's DndChunk)
# ---------------------------------------------------------------------------

@dataclass
class DndChunk:
    chunk_id: str
    book_slug: str
    source_file: str
    page_start: int
    page_end: int
    part: str | None
    chapter: str | None
    section: str | None
    content_type: str
    entity_name: str | None
    class_name: str | None
    feature_name: str | None
    text: str


@dataclass
class LineItem:
    """One visual line in column-reading order."""
    page: int      # 1-based
    col: int       # 0 = left, 1 = right
    size: float    # dominant font size
    text: str
    bold: bool = False   # majority of the line is bold (pymupdf flags & 16)


def _chunk_id(book_slug: str, page: int, col: int, idx: int) -> str:
    raw = f"{book_slug}:{page}:{col}:{idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Book configs
# ---------------------------------------------------------------------------

# Each config carries the PDF reader engine it was calibrated against. mm-5e and
# dmg-5e were tuned on pdfplumber's line grouping and are already embedded
# (777/925 chunks, 98.3% eval) — the fitz reader groups lines differently and
# regresses their stat-block/item heuristics (parity gate caught Tarrasque/Lich
# dropping), so they stay on pdfplumber. New books use fitz (it decodes layers
# pdfplumber can't read). --engine on the CLI overrides per run.
BOOK_CONFIGS: dict[str, dict] = {
    "mm-5e": {
        "engine": "pdfplumber",
        "kind": "monster_manual",
        "first_content_page": 12,   # AARAKOCRA starts the bestiary
        "min_body_pt": 7.0,         # below this = margin-art captions / flavor quotes
        "heading_min_pt": 10.5,     # caps monster/section names (drifts 11.6-13.4)
        "max_chunk_chars": 1400,
    },
    "dmg-5e": {
        "engine": "pdfplumber",
        "kind": "dmg",
        "first_content_page": 6,
        "min_body_pt": 7.0,
        "heading_min_pt": 11.0,     # caps section heads (~13pt, drifts)
        "item_name_min_pt": 7.4,    # magic item names are ~8pt caps
        "item_name_max_pt": 11.0,
        "max_chunk_chars": 1400,
    },
    # Mixed-content supplements — fitz engine, generic supplement extractor.
    # first_content_page skips covers/TOC/credits; tune per book.
    "phb-5e":     {"engine": "fitz", "kind": "supplement", "first_content_page": 5,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "xge-5e":     {"engine": "fitz", "kind": "supplement", "first_content_page": 5,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "tce-5e":     {"engine": "fitz", "kind": "supplement", "first_content_page": 5,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "vgm-5e":     {"engine": "fitz", "kind": "supplement", "first_content_page": 5,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "mtf-5e":     {"engine": "fitz", "kind": "supplement", "first_content_page": 5,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "eepc-5e":    {"engine": "fitz", "kind": "supplement", "first_content_page": 2,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "scag-5e":    {"engine": "fitz", "kind": "supplement", "first_content_page": 5,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "tortle-5e":  {"engine": "fitz", "kind": "supplement", "first_content_page": 2,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "eberron-5e": {"engine": "fitz", "kind": "supplement", "first_content_page": 5,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
    "ravnica-5e": {"engine": "fitz", "kind": "supplement", "first_content_page": 5,
                   "min_body_pt": 8.0, "heading_min_pt": 11.0, "max_chunk_chars": 1400},
}

_MM_STAT_ANCHOR = re.compile(r"^Armor Class\s*\d", re.IGNORECASE)
_DMG_RARITY_ANCHOR = re.compile(
    r"^(Wondrous item|Weapon\b|Armou?r\b|Potion\b|Ring\b|Rod\b|Scroll\b|Staff\b|Wand\b|Ammunition\b)"
    r".*?(common|uncommon|rare|very rare|legendary|artifact|varies)",
    re.IGNORECASE,
)
# Stat block field lines — used to keep multi-column stat blocks glued together
_MM_STAT_FIELDS = re.compile(
    r"^(Hit Points|Speed|STR\b|Saving Throws|Skills|Damage (Resistances|Immunities|Vulnerabilities)"
    r"|Condition Immunities|Senses|Languages|Challenge)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Heading / name heuristics (pure, unit-testable)
# ---------------------------------------------------------------------------

def is_caps_heading(
    text: str, size: float, min_pt: float, max_len: int = 48,
    min_upper_ratio: float = 0.6,
) -> bool:
    """
    A plausible entity/section heading: big enough, short, CAPS-leaning,
    and mostly alphabetic. The upper-ratio default is 0.6 (not 0.8) because
    OCR renders small-caps names with stray lowercase ('GoBLIN Boss'); the
    alpha-ratio gate still rejects punctuation-heavy flavor quotes like
    "''BREE-YARK.'''".
    """
    if size < min_pt:
        return False
    t = text.strip()
    if not (3 <= len(t) <= max_len):
        return False
    letters = [c for c in t if c.isalpha()]
    if not letters or len(letters) / len(t.replace(" ", "")) < 0.7:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= min_upper_ratio


def is_monster_name_candidate(text: str, size: float, min_pt: float,
                              max_len: int = 48, min_upper: float = 0.45) -> bool:
    """A looser monster-NAME test than is_caps_heading, used to recover stat-block
    owners that the heading gate misses for the two real OCR reasons (6om/0im.3):
      - upper-ratio: mixed-case OCR names ('Kuo-ToA' = 0.5 < the 0.6 heading gate)
      - size: small-caps names rendered just under heading_min ('STORM GIANT' 10.1)
    Always rejects type lines, stat-field labels, and pure stat rows; short and
    mostly-alphabetic. `min_upper` is relaxed to 0.0 by the caller when the line
    sits directly above a type line (a strong structural name signal that catches
    low-caps names like 'Lamia'/'Marilith'). Binding to a stat block is left to
    `_assign_anchor_owners` position scoring (a candidate with no nearby anchor
    owns nothing)."""
    if size < min_pt:
        return False
    t = text.strip()
    if not (3 <= len(t) <= max_len) or t.endswith("."):  # names never end in a period
        return False
    low = t.lower().rstrip(".").strip()
    if is_type_line(t) or low in _STAT_FIELD_WORDS:
        return False
    # reject stat rows whose every token is a stat-field / ability word
    # ("STR DEX CON INT WIS CHA", "Hit Dice") — never a monster name.
    toks = low.split()
    if toks and all(tok in _STAT_FIELD_WORDS for tok in toks):
        return False
    letters = [c for c in t if c.isalpha()]
    if not letters or len(letters) / len(t.replace(" ", "")) < 0.7:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= min_upper


def normalize_entity_name(caps: str) -> str:
    """'ANCIENT BLACK DRAGON' → 'Ancient Black Dragon'; keeps parens/commas."""
    cleaned = re.sub(r"\s+", " ", caps.strip())
    return cleaned.title().replace("'S", "'s")


def split_paragraph_chunks(lines: list[str], max_chars: int) -> list[str]:
    """Greedy-pack body lines into chunks of <= max_chars (never splits a line)."""
    chunks: list[str] = []
    buf: list[str] = []
    n = 0
    for ln in lines:
        if n + len(ln) > max_chars and buf:
            chunks.append("\n".join(buf))
            buf, n = [], 0
        buf.append(ln)
        n += len(ln) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


# ---------------------------------------------------------------------------
# Monster Manual extraction (pure over a LineItem stream)
# ---------------------------------------------------------------------------

def _assign_anchor_owners(
    headings: list[tuple[int, int, int, str]],   # (idx, page, col, name)
    anchors: list[tuple[int, int, int]],         # (idx, page, col)
) -> dict[int, str]:
    """
    Pass 1 of MM extraction: bind each "Armor Class" anchor to a monster name.

    Print layout puts the name directly above the stat block in the same
    column — but OCR scans break that often enough (name at top of the other
    column, name missing entirely) that ownership needs position scoring:

      1. nearest preceding heading, same page + same column, with no other
         anchor between heading and anchor (it would have consumed it)
      2. else nearest heading on the same page in either column (the Basilisk
         case: stat block in col 0, "BASILISK" heads col 1 — stream order
         puts the heading *after* the anchor)
      3. else nearest preceding heading on an earlier page (the Beholder
         case: no detectable heading on the stat block's page at all; the
         family section heading "BEHOLDERS" owns it)
    """
    owners: dict[int, str] = {}
    anchor_idxs = [a[0] for a in anchors]

    for a_idx, a_page, a_col in anchors:
        # Rule 1: same page+col, preceding, no anchor in between
        best: str | None = None
        for h_idx, h_page, h_col, h_name in reversed(headings):
            if h_idx >= a_idx:
                continue
            if h_page == a_page and h_col == a_col:
                blocked = any(h_idx < x < a_idx for x in anchor_idxs)
                if not blocked:
                    best = h_name
                break  # only consider the nearest preceding same-col heading
        if best is None:
            # Rule 2: same page, either column, nearest by stream distance
            same_page = [(abs(h_idx - a_idx), h_idx, h_name)
                         for h_idx, h_page, h_col, h_name in headings
                         if h_page == a_page]
            if same_page:
                best = min(same_page)[2]
        if best is None:
            # Rule 3: nearest preceding heading anywhere
            preceding = [(h_idx, h_name) for h_idx, _, _, h_name in headings if h_idx < a_idx]
            if preceding:
                best = preceding[-1][1]
        if best is not None:
            owners[a_idx] = best
    return owners


def extract_mm_chunks(
    stream: list[LineItem],
    book_slug: str,
    source_file: str,
    cfg: dict,
) -> list[DndChunk]:
    """
    Two-pass extraction:

      Pass 1  collect caps headings + "Armor Class" anchors; bind each anchor
              to its owning monster name by position (_assign_anchor_owners).
      Pass 2  walk the stream: anchor → stat block chunk (owner from pass 1),
              absorbing lines until the next heading/anchor; prose → lore
              chunks under the nearest preceding section heading.
    """
    heading_min = cfg["heading_min_pt"]
    min_body = cfg["min_body_pt"]
    max_chars = cfg["max_chunk_chars"]
    first_page = cfg["first_content_page"]

    visible = [
        (i, li) for i, li in enumerate(stream)
        if li.page >= first_page and li.size >= min_body
    ]

    headings: list[tuple[int, int, int, str]] = []
    anchors: list[tuple[int, int, int]] = []
    for vi, (i, li) in enumerate(visible):
        if is_caps_heading(li.text, li.size, heading_min):
            headings.append((i, li.page, li.col, normalize_entity_name(li.text)))
        elif _MM_STAT_ANCHOR.match(li.text):
            anchors.append((i, li.page, li.col))
        else:
            # recover sub-threshold / mixed-case monster names as owner candidates.
            # Two signals: caps-leaning (>=0.45), OR sitting directly above a type
            # line (caps-agnostic — catches low-caps names like 'Lamia'/'Marilith').
            nxt = visible[vi + 1][1] if vi + 1 < len(visible) else None
            above_type = nxt is not None and is_type_line(nxt.text)
            if is_monster_name_candidate(li.text, li.size, min_body,
                                         min_upper=0.0 if above_type else 0.45):
                # position scoring binds these only when an AC anchor is actually near.
                headings.append((i, li.page, li.col, normalize_entity_name(li.text)))

    owners = _assign_anchor_owners(headings, anchors)
    heading_idxs = {h[0] for h in headings}
    anchor_idxs = {a[0] for a in anchors}

    chunks: list[DndChunk] = []
    counter = [0]

    current_section: str | None = None
    stat_owner: str | None = None
    stat_lines: list[str] = []
    stat_start: tuple[int, int] | None = None
    lore_owner: str | None = None
    lore_lines: list[str] = []
    lore_start: tuple[int, int] | None = None

    def flush_stat(end_page: int) -> None:
        nonlocal stat_owner, stat_lines, stat_start
        text = "\n".join(stat_lines).strip()
        if stat_owner and len(text.split()) >= 10:
            counter[0] += 1
            chunks.append(DndChunk(
                chunk_id=_chunk_id(book_slug, stat_start[0], stat_start[1], counter[0]),
                book_slug=book_slug, source_file=source_file,
                page_start=stat_start[0], page_end=end_page,
                part=None, chapter="Bestiary", section="Stat Block",
                content_type="monster", entity_name=stat_owner,
                class_name=None, feature_name=None,
                text=f"{stat_owner}\n{text}",
            ))
        stat_owner, stat_lines, stat_start = None, [], None

    def flush_lore(end_page: int) -> None:
        nonlocal lore_owner, lore_lines, lore_start
        if lore_owner and lore_lines:
            for part_text in split_paragraph_chunks(lore_lines, max_chars):
                if len(part_text.split()) < 15:
                    continue
                counter[0] += 1
                chunks.append(DndChunk(
                    chunk_id=_chunk_id(book_slug, lore_start[0], lore_start[1], counter[0]),
                    book_slug=book_slug, source_file=source_file,
                    page_start=lore_start[0], page_end=end_page,
                    part=None, chapter="Bestiary", section="Lore",
                    content_type="monster", entity_name=lore_owner,
                    class_name=None, feature_name=None,
                    text=f"{lore_owner}\n{part_text}",
                ))
        lore_owner, lore_lines, lore_start = None, [], None

    for i, li in visible:
        if i in heading_idxs:
            if stat_owner:
                flush_stat(li.page)
            flush_lore(li.page)
            current_section = normalize_entity_name(li.text)
            lore_owner = current_section
            lore_start = None
            continue

        if i in anchor_idxs:
            if stat_owner:
                flush_stat(li.page)
            stat_owner = owners.get(i) or current_section
            stat_start = (li.page, li.col)
            # The italic type line ("Medium monstrosity, unaligned") usually
            # lands in lore_lines just before the anchor — pull it into the
            # stat block where it belongs.
            type_line = ""
            if lore_lines and re.search(
                r"\b(aberration|beast|celestial|construct|dragon|elemental|fey|fiend"
                r"|giant|humanoid|monstrosity|ooze|plant|undead)\b",
                lore_lines[-1], re.IGNORECASE,
            ):
                type_line = lore_lines.pop()
            stat_lines = ([type_line] if type_line else []) + [li.text]
            continue

        if stat_owner:
            stat_lines.append(li.text)
        else:
            if lore_owner and lore_start is None:
                lore_start = (li.page, li.col)
            lore_lines.append(li.text)

    last_page = visible[-1][1].page if visible else first_page
    if stat_owner:
        flush_stat(last_page)
    flush_lore(last_page)
    return chunks


# ---------------------------------------------------------------------------
# DMG extraction (pure over a LineItem stream)
# ---------------------------------------------------------------------------

def extract_dmg_chunks(
    stream: list[LineItem],
    book_slug: str,
    source_file: str,
    cfg: dict,
) -> list[DndChunk]:
    """
    Two chunk kinds:

      magic_item    rarity-line anchor; the caps name immediately above is the
                    item. The block runs until the next item name+anchor pair
                    or the next big section heading.
      dm_guidance   caps section headings (>= heading_min_pt) split the rest;
                    body lines pack into <= max_chunk_chars chunks.
    """
    heading_min = cfg["heading_min_pt"]
    item_min = cfg["item_name_min_pt"]
    item_max = cfg["item_name_max_pt"]
    min_body = cfg["min_body_pt"]
    max_chars = cfg["max_chunk_chars"]
    first_page = cfg["first_content_page"]

    chunks: list[DndChunk] = []
    counter = [0]

    current_section: str | None = None
    item_owner: str | None = None       # open magic-item block
    item_lines: list[str] = []
    item_start: tuple[int, int] | None = None

    body_lines: list[str] = []
    body_start: tuple[int, int] | None = None

    # Track the last shortish caps-ish line seen at item-name size — candidate
    # magic item name for an upcoming rarity anchor.
    pending_item: str | None = None
    pending_item_pos: tuple[int, int] | None = None

    def flush_item(end_page: int) -> None:
        nonlocal item_owner, item_lines, item_start
        text = "\n".join(item_lines).strip()
        if item_owner and len(text.split()) >= 12:
            counter[0] += 1
            chunks.append(DndChunk(
                chunk_id=_chunk_id(book_slug, item_start[0], item_start[1], counter[0]),
                book_slug=book_slug, source_file=source_file,
                page_start=item_start[0], page_end=end_page,
                part=None, chapter="Magic Items", section=None,
                content_type="magic_item", entity_name=item_owner,
                class_name=None, feature_name=None,
                text=f"{item_owner}\n{text}",
            ))
        item_owner, item_lines, item_start = None, [], None

    def flush_body(end_page: int) -> None:
        nonlocal body_lines, body_start
        if body_lines and current_section:
            for part_text in split_paragraph_chunks(body_lines, max_chars):
                if len(part_text.split()) < 15:
                    continue
                counter[0] += 1
                chunks.append(DndChunk(
                    chunk_id=_chunk_id(book_slug, body_start[0], body_start[1], counter[0]),
                    book_slug=book_slug, source_file=source_file,
                    page_start=body_start[0], page_end=end_page,
                    part=None, chapter=None, section=current_section,
                    content_type="dm_guidance", entity_name=current_section,
                    class_name=None, feature_name=None,
                    text=f"{current_section}\n{part_text}",
                ))
        body_lines, body_start = [], None

    for li in stream:
        if li.page < first_page or li.size < min_body:
            continue

        # Big caps heading → new guidance section (and closes an open item)
        if is_caps_heading(li.text, li.size, heading_min):
            flush_item(li.page)
            flush_body(li.page)
            current_section = normalize_entity_name(li.text)
            continue

        # Item-name-sized caps line → remember as candidate
        if (item_min <= li.size <= item_max
                and is_caps_heading(li.text, li.size, item_min, max_len=44)):
            pending_item = normalize_entity_name(li.text)
            pending_item_pos = (li.page, li.col)
            continue

        # Rarity anchor right after a candidate name → open a magic-item block
        if pending_item and _DMG_RARITY_ANCHOR.match(li.text):
            flush_item(li.page)
            item_owner = pending_item
            item_start = pending_item_pos
            item_lines = [li.text]
            pending_item = None
            pending_item_pos = None
            continue

        pending_item = None
        pending_item_pos = None

        if item_owner:
            item_lines.append(li.text)
        else:
            if body_start is None:
                body_start = (li.page, li.col)
            body_lines.append(li.text)

    last_page = stream[-1].page if stream else first_page
    flush_item(last_page)
    flush_body(last_page)
    return chunks


# ---------------------------------------------------------------------------
# Section: supplement extraction (mixed-content books — XGE/TCE/VGM/etc.)
# ---------------------------------------------------------------------------
# Supplements interleave spells, subclasses, feats, monsters, items, and prose
# in one book. Rather than a per-book structural parser, we chunk on headings
# (bold or caps) and classify each chunk's content_type by textual signature.

_SPELL_LEVEL_RE = re.compile(
    r"\b(cantrip|\d+(?:st|nd|rd|th)-level)\b", re.IGNORECASE,
)
_SPELL_SCHOOL_RE = re.compile(
    r"\b(abjuration|conjuration|divination|enchantment|evocation|illusion|necromancy|transmutation)\b",
    re.IGNORECASE,
)
_FEAT_PREREQ_RE = re.compile(r"^\s*Prerequisite:", re.IGNORECASE | re.MULTILINE)

# Strong anchors for the supplement extractor. A line IS the anchor; the entity
# name is the line immediately above it (the spell/feat/monster heading).
#
# The PHB scan corrupts these sub-header lines (c->e "evoeation eantrip", l->I
# "leveI", o->0, s->5), which defeated a strict match and silently merged spells
# into the prior chunk — the Fireball bug (agent-forge-harness-7p3). We widen
# ONLY the anchor's fixed keywords (level / cantrip / the 8 schools) by their OCR
# confusables; general matching is untouched. Under IGNORECASE each class also
# covers its upper-case form (so [li1] matches l, L, i, I, 1).
_OCR_TWINS = {
    "c": "ce", "e": "ce",
    "l": "li1", "i": "il1", "1": "1li",
    "o": "o0", "0": "0o",
    "s": "s5", "5": "5s",
}


def _ocr_fuzz(word: str) -> str:
    """A regex fragment matching `word` tolerant of the scan's OCR confusables."""
    out = []
    for ch in word:
        twins = _OCR_TWINS.get(ch.lower())
        out.append(f"[{twins}]" if twins else re.escape(ch))
    return "".join(out)


_SCHOOL_WORDS = ["abjuration", "conjuration", "divination", "enchantment",
                 "evocation", "illusion", "necromancy", "transmutation"]
_SCHOOLS = "|".join(_SCHOOL_WORDS)
_FUZZ_SCHOOLS = "|".join(_ocr_fuzz(w) for w in _SCHOOL_WORDS)
# "8th-level necromancy" or "Evocation cantrip" — the spell sub-header line,
# OCR-tolerant on the fixed keywords.
_SPELL_ANCHOR_RE = re.compile(
    rf"^\s*(\d+(?:st|nd|rd|th)-{_ocr_fuzz('level')}\s+({_FUZZ_SCHOOLS})"
    rf"|({_FUZZ_SCHOOLS})\s+{_ocr_fuzz('cantrip')})\b",
    re.IGNORECASE,
)
_STATBLOCK_ANCHOR_RE = re.compile(r"^\s*Armor Class\s*\d", re.IGNORECASE)
_FEAT_ANCHOR_RE = re.compile(r"^\s*Prerequisite:", re.IGNORECASE)

# Fallback spell anchor: the "Casting Time" field is far more OCR-stable than the
# level/school line (which the PHB scan shreds, e.g. "3rd~evelevoeaUon"). Every
# spell has exactly one, so a SECOND casting-time line inside an open spell chunk
# marks the next spell's boundary even when its level line never anchored.
_CASTING_RE = re.compile(rf"^\s*{_ocr_fuzz('casting')}\s+{_ocr_fuzz('time')}\b", re.IGNORECASE)

# Spell stat-field labels — never a spell name (used by name recovery + fault D).
_SPELL_FIELD_STOP: frozenset[str] = frozenset({
    "casting time", "range", "components", "duration", "at higher levels",
    "spell descriptions", "classes", "ritual",
})


def is_casting_time(text: str) -> bool:
    return _CASTING_RE.search(text) is not None


def is_field_line(text: str) -> bool:
    """A spell stat-field / section label ('Casting Time: 1 action', 'Duration',
    'Spell Descriptions') — never a spell/feat name."""
    s = text.strip().lower()
    return s.split(":")[0].strip() in _SPELL_FIELD_STOP or s.rstrip(".:").strip() in _SPELL_FIELD_STOP


def is_spell_name_line(text: str) -> bool:
    """A short, caps-leaning line that looks like a spell heading (FIREBALL,
    FIND TRAPS) — not a stat field, not a level line (starts with a digit), not
    body prose. Used to recover the name for the casting-time fallback."""
    s = text.strip()
    if not (2 <= len(s) <= 40) or s[0].isdigit():
        return False
    if s.lower().rstrip(".:").strip() in _SPELL_FIELD_STOP:
        return False
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 2:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.6


def is_spell_anchor(text: str) -> bool:
    return _SPELL_ANCHOR_RE.search(text) is not None


def is_feat_anchor(text: str) -> bool:
    return _FEAT_ANCHOR_RE.search(text) is not None


def is_statblock_anchor(text: str) -> bool:
    return _STATBLOCK_ANCHOR_RE.search(text) is not None


# A stat-block type line: a size word + a creature type + (usually) an alignment.
# OCR sometimes fuses the size into the type ("Largemonstrosity"), so the size
# word is allowed to butt against the type. Requires BOTH a size and a type so a
# bare monster name never matches.
_TYPE_LINE_RE = re.compile(
    r"\b(tiny|small|medium|large|huge|gargantuan)\s*"
    r"(aberration|beast|celestial|construct|dragon|elemental|fey|fiend|giant"
    r"|humanoid|monstrosity|ooze|plant|undead|swarm)",
    re.IGNORECASE,
)


def is_type_line(text: str) -> bool:
    """True for a monster stat-block type/alignment line (e.g. 'Large
    monstrosity, neutral evil') — skipped when picking the entity name."""
    return _TYPE_LINE_RE.search(text) is not None


# Bare size words and stat-block field labels that sometimes survive as headings
# on OCR'd pages — never a monster name.
_STAT_FIELD_WORDS: frozenset[str] = frozenset({
    "tiny", "small", "medium", "large", "huge", "gargantuan",
    "actions", "reactions", "hit", "hit points", "speed", "skills", "senses",
    "languages", "challenge", "traits", "damage", "armor class", "saving throws",
    "legendary actions", "str", "dex", "con", "int", "wis", "cha",
})


def classify_content_type(heading: str, body: str) -> str:
    """
    Classify a supplement chunk by textual signature. Order matters — the
    strongest, lowest-false-positive signals first:

      monster  — has both "Armor Class" and "Hit Points" (a stat block). Checked
                 first so a monster whose actions mention spells stays a monster.
      spell    — a spell level/school line AND a "Casting Time:" / "Range:" field.
      feat     — a "Prerequisite:" line near the top (feat stat block).
      rule     — default (prose: class features, subclass text, guidance, lore).
    """
    head = body[:400]
    if re.search(r"\bArmor Class\b", body, re.I) and re.search(r"\bHit Points\b", body, re.I):
        return "monster"
    has_level = _SPELL_LEVEL_RE.search(head) or _SPELL_SCHOOL_RE.search(head)
    has_field = re.search(r"\b(Casting Time|Range):", head, re.I)
    if has_level and has_field:
        return "spell"
    if _FEAT_PREREQ_RE.search(head):
        return "feat"
    return "rule"


def extract_supplement_chunks(
    stream: list[LineItem],
    book_slug: str,
    source_file: str,
    cfg: dict,
) -> list[DndChunk]:
    """
    Anchor-driven mixed-content extractor for supplements.

    Real supplement spell/feat names are small and unbolded (XGE spell names are
    9.3pt), so heading detection alone misses them. Instead we anchor on the
    structural sub-header that always follows the name:

      spell    — a "Nth-level <school>" / "<school> cantrip" line; the name is
                 the line directly above it.
      feat     — a "Prerequisite:" line; the name is the line above.
      monster  — an "Armor Class N" line (some supplements carry stat blocks).
      prose    — bold/caps headings open rule/lore chunks (classified by
                 signature); body accumulates until the next anchor or heading.

    When an anchor fires, the pending name line is pulled out of the current
    accumulation and becomes the new chunk's entity_name.
    """
    heading_min = cfg["heading_min_pt"]
    min_body = cfg["min_body_pt"]
    max_chars = cfg["max_chunk_chars"]
    first_page = cfg["first_content_page"]

    chunks: list[DndChunk] = []
    counter = [0]

    # Current open chunk
    cur_ctype: str | None = None       # forced type from an anchor; None = prose (classify on flush)
    cur_entity: str | None = None
    cur_lines: list[str] = []
    cur_start: tuple[int, int] | None = None

    # Recent heading lines (bold/caps), kept so a stat-block anchor can recover
    # its monster name: on OCR'd books the name AND the type line are both bold
    # headings, so the name never lands in cur_lines — it must be recovered from
    # the heading stream, skipping the type line.
    recent_headings: list[LineItem] = []

    def is_heading(li: LineItem) -> bool:
        if li.bold and 3 <= len(li.text) <= 60 and any(c.isalpha() for c in li.text):
            return True
        return is_caps_heading(li.text, li.size, heading_min)

    def recover_statblock_name() -> str | None:
        """Most-recent heading that isn't a type line or a stat-block field
        label — the monster name."""
        for h in reversed(recent_headings):
            t = h.text.strip()
            low = t.lower().rstrip(".")
            if is_type_line(t) or low in _STAT_FIELD_WORDS:
                continue
            if any(c.isalpha() for c in t) and len(t) <= 50:
                return t
        return None

    def flush(end_page: int) -> None:
        nonlocal cur_ctype, cur_entity, cur_lines, cur_start
        if cur_entity and cur_lines and cur_start:
            body_full = "\n".join(cur_lines).strip()
            ctype = cur_ctype or classify_content_type(cur_entity, body_full)
            for part in split_paragraph_chunks([cur_entity] + cur_lines, max_chars):
                if len(part.split()) < 5:
                    continue
                counter[0] += 1
                chunks.append(DndChunk(
                    chunk_id=_chunk_id(book_slug, cur_start[0], cur_start[1], counter[0]),
                    book_slug=book_slug, source_file=source_file,
                    page_start=cur_start[0], page_end=end_page,
                    part=None, chapter=None, section=None,
                    content_type=ctype,
                    entity_name=normalize_entity_name(cur_entity),
                    class_name=None, feature_name=None,
                    text=part,
                ))
        cur_ctype, cur_entity, cur_lines, cur_start = None, None, [], None

    def open_anchored(li: LineItem, ctype: str) -> None:
        # Open a typed chunk. Spell/feat names are non-bold and sit in cur_lines
        # (pop the line above the anchor). Monster names are bold headings that
        # never reached cur_lines — recover them from the heading stream, and
        # fold the type line into the body.
        nonlocal cur_ctype, cur_entity, cur_lines, cur_start
        type_line_text: str | None = None
        if ctype == "monster":
            name = recover_statblock_name()
            if name is None:
                # Fallback: the line above the anchor, but if that's the type
                # line, step up to the prior line (the name).
                cand = cur_lines.pop() if cur_lines else cur_entity
                if cand and is_type_line(cand) and cur_entity and not is_type_line(cur_entity):
                    type_line_text, name = cand, cur_entity
                else:
                    name = cand
            elif cur_entity and is_type_line(cur_entity):
                type_line_text = cur_entity
        else:
            name = cur_lines.pop() if cur_lines else cur_entity
            # a stat-field line (the prior spell's "Duration: …") is never the
            # name — recover a real name-looking line from the tail, else unknown.
            if name and is_field_line(name):
                recovered = None
                for j in range(len(cur_lines) - 1, max(-1, len(cur_lines) - 7), -1):
                    if is_spell_name_line(cur_lines[j]):
                        recovered = cur_lines[j]
                        break
                name = recovered or "(unknown)"
        flush(li.page)
        recent_headings.clear()   # consumed — don't let this name bleed to the next block
        cur_ctype = ctype
        cur_entity = name or "(unknown)"
        cur_start = (li.page, li.col)
        cur_lines = ([type_line_text] if type_line_text else []) + [li.text]

    def open_spell_via_casting(li: LineItem) -> None:
        # Fallback for a spell whose level/school line was OCR-shredded so no level
        # anchor fired (e.g. "3rd~evelevoeaUon"). Stateless: on any Casting Time
        # line, look back over the last few accumulated lines for a spell-name line.
        # A *level-anchored* spell has no unclaimed name there (the anchor popped
        # it) and its level line starts with a digit, so this is a no-op for it; a
        # shredded spell still carries its name line, so we split there. Everything
        # before the name stays with the prior chunk; the lines between the name and
        # this Casting Time (the shredded level line) ride into the new spell.
        nonlocal cur_ctype, cur_entity, cur_lines, cur_start
        lo = max(0, len(cur_lines) - 6)        # bound the look-back (avoid stray caps in body)
        name_idx = None
        for j in range(len(cur_lines) - 1, lo - 1, -1):
            if is_spell_name_line(cur_lines[j]):
                name_idx = j
                break
        if name_idx is None:
            cur_lines.append(li.text)          # no recoverable name — keep as body
            return
        name = cur_lines[name_idx]
        carried = cur_lines[name_idx + 1:]     # shredded level line, etc.
        cur_lines = cur_lines[:name_idx]       # prior chunk keeps everything before the name
        flush(li.page)
        recent_headings.clear()
        cur_ctype = "spell"
        cur_entity = name
        cur_start = (li.page, li.col)
        cur_lines = carried + [li.text]

    # PHB spell NAME lines render a bit smaller than body (e.g. FIREBALL ~7.4pt vs
    # body 9pt vs min_body 8.0). Don't size-filter a line that looks like a spell
    # name and is only just under the body floor, or the spell is lost entirely.
    name_floor = min_body - 1.0
    for li in stream:
        if li.page < first_page:
            continue
        if li.size < min_body and not (li.size >= name_floor and is_spell_name_line(li.text)):
            continue
        if is_spell_anchor(li.text):
            open_anchored(li, "spell")
        elif is_feat_anchor(li.text):
            open_anchored(li, "feat")
        elif is_statblock_anchor(li.text):
            open_anchored(li, "monster")
        elif is_casting_time(li.text):
            # Stateless spell-boundary fallback: splits only if an unclaimed spell
            # name sits in the recent tail (a shredded spell), else appends as body.
            open_spell_via_casting(li)
        elif is_heading(li):
            flush(li.page)
            recent_headings.append(li)
            del recent_headings[:-6]   # keep the last 6
            cur_ctype = None
            cur_entity = li.text
            cur_start = (li.page, li.col)
            cur_lines = []
        else:
            if cur_entity is None:
                # No open chunk yet — treat this line as a provisional name so a
                # following anchor can claim it (handles spell name → level line
                # with no preceding heading).
                cur_entity = li.text
                cur_start = (li.page, li.col)
                cur_lines = []
            else:
                cur_lines.append(li.text)

    last_page = stream[-1].page if stream else first_page
    flush(last_page)
    return retag_monster_lore(chunks)


def retag_monster_lore(chunks: list[DndChunk]) -> list[DndChunk]:
    """
    A monster's lore prose is classified `rule` (it lacks Armor Class/Hit Points),
    but it shares an entity_name with that monster's `monster` stat block. For a
    "What is X?" query the lore is the better answer, yet a content_type=monster
    filter/expectation excludes it. Retag any `rule` chunk to `monster`
    (section="Lore") when its entity_name also owns a `monster` stat block.
    """
    monster_entities = {
        (c.entity_name or "").lower()
        for c in chunks
        if c.content_type == "monster" and c.entity_name
    }
    if not monster_entities:
        return chunks
    for c in chunks:
        if (c.content_type == "rule" and c.entity_name
                and c.entity_name.lower() in monster_entities):
            c.content_type = "monster"
            c.section = "Lore"
    return chunks


# ---------------------------------------------------------------------------
# PDF → LineItem stream
# ---------------------------------------------------------------------------

_Y_TOL = 2.0
_BOLD_FLAG = 16   # pymupdf span flag bit for bold (TEXT_FONT_BOLD)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def group_spans_to_lines(
    spans: list[tuple[float, float, float, int, str]],
    page_num: int,
    page_width: float,
) -> list[LineItem]:
    """
    Group pymupdf spans into LineItems (pure — no PDF library needed).

    Each span tuple is (x0, top, size, flags, text), matching what
    read_pdf_stream_fitz pulls from page.get_text("dict"). Spans are split
    into two columns by x-midpoint, grouped into visual lines by y within
    _Y_TOL, concatenated in reading order, and tagged with the dominant
    font size and a bold flag (majority of characters bold via flags & 16).
    """
    from collections import Counter

    split_x = page_width / 2
    # Bucket spans by (column, y-band)
    buckets: dict[tuple[int, int], list[tuple]] = {}
    for x0, top, size, flags, text in spans:
        col = 0 if x0 < split_x else 1
        yband = round(top / _Y_TOL)
        buckets.setdefault((col, yband), []).append((x0, top, size, flags, text))

    lines: list[LineItem] = []
    for (col, yband), group in sorted(buckets.items()):
        group.sort(key=lambda s: s[0])  # left-to-right within the line
        text = " ".join(s[4].strip() for s in group if s[4].strip())
        text = _CONTROL_RE.sub("", text).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue
        # Dominant size weighted by character count
        size_chars: Counter = Counter()
        bold_chars = 0
        total_chars = 0
        for _, _, size, flags, t in group:
            n = len(t.strip())
            size_chars[round(size, 1)] += n
            if flags & _BOLD_FLAG:
                bold_chars += n
            total_chars += n
        dominant = max(size_chars, key=size_chars.__getitem__) if size_chars else 0.0
        bold = total_chars > 0 and bold_chars / total_chars >= 0.5
        lines.append(LineItem(page=page_num, col=col, size=dominant, text=text, bold=bold))
    return lines


def read_pdf_stream_fitz(pdf_path: str) -> list[LineItem]:
    """Read a PDF into a LineItem stream via pymupdf (fitz).

    Preferred engine: decodes books pdfplumber can't (XGE's junk OCR layer,
    Tortle's CID fonts) and exposes bold flags. Falls back per-page to nothing
    if a page has no text (image-only)."""
    import fitz  # pymupdf

    stream: list[LineItem] = []
    doc = fitz.open(pdf_path)
    total = len(doc)
    for page_num, page in enumerate(doc, start=1):
        print(f"\r  reading page {page_num}/{total}", end="", flush=True)
        spans: list[tuple[float, float, float, int, str]] = []
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                for sp in line.get("spans", []):
                    spans.append((
                        sp["bbox"][0], sp["bbox"][1],
                        sp["size"], sp.get("flags", 0), sp["text"],
                    ))
        stream.extend(group_spans_to_lines(spans, page_num, page.rect.width))
    doc.close()
    print()
    return stream


def read_pdf_stream(pdf_path: str) -> list[LineItem]:
    """Legacy pdfplumber reader — kept as a fallback engine (--engine pdfplumber)."""
    import pdfplumber
    from collections import Counter

    stream: list[LineItem] = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            print(f"\r  reading page {page_num}/{total}", end="", flush=True)
            w = page.width
            for col, (x0, x1) in enumerate([(0, w / 2), (w / 2, w)]):
                crop = page.crop((x0, 0, x1, page.height))
                chars = sorted(crop.chars, key=lambda c: (round(c["top"] / _Y_TOL), c["x0"]))
                for _, grp in groupby(chars, key=lambda c: round(c["top"] / _Y_TOL)):
                    grp = list(grp)
                    text = "".join(c["text"] for c in grp).strip()
                    # OCR output can carry NUL and other control bytes that
                    # PostgreSQL text columns reject — strip them here.
                    text = _CONTROL_RE.sub("", text)
                    if not text:
                        continue
                    sizes = Counter(round(c["size"], 1) for c in grp)
                    stream.append(LineItem(
                        page=page_num, col=col,
                        size=max(sizes, key=sizes.get), text=text,
                    ))
    print()
    return stream


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract OCR-scanned D&D book into chunks JSONL")
    parser.add_argument("pdf", help="Path to the PDF")
    parser.add_argument("--book-slug", required=True, choices=list(BOOK_CONFIGS.keys()))
    parser.add_argument("--out", default=None, help="Output JSONL (default: chunks-<slug>.jsonl)")
    parser.add_argument("--engine", choices=["fitz", "pdfplumber"], default=None,
                        help="PDF reader engine override (default: per-book config, else fitz)")
    args = parser.parse_args()

    if not Path(args.pdf).exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    cfg = BOOK_CONFIGS[args.book_slug]
    out_path = Path(args.out or f"chunks-{args.book_slug}.jsonl")
    source_file = Path(args.pdf).name

    engine = args.engine or cfg.get("engine", "fitz")
    print(f"Extracting: {args.pdf}  (book-slug: {args.book_slug}, engine: {engine})")
    reader = read_pdf_stream_fitz if engine == "fitz" else read_pdf_stream
    stream = reader(args.pdf)
    print(f"  {len(stream)} lines read")

    if cfg["kind"] == "monster_manual":
        chunks = extract_mm_chunks(stream, args.book_slug, source_file, cfg)
    elif cfg["kind"] == "dmg":
        chunks = extract_dmg_chunks(stream, args.book_slug, source_file, cfg)
    else:  # "supplement" — mixed-content books
        chunks = extract_supplement_chunks(stream, args.book_slug, source_file, cfg)

    # Normalize OCR text errors (no-op on clean books; fixes the phb-5e scan — 6om)
    for chunk in chunks:
        chunk.text = normalize_ocr(chunk.text)
        if chunk.entity_name:
            chunk.entity_name = normalize_ocr(chunk.entity_name)

    type_counts: dict[str, int] = {}
    section_counts: dict[str, int] = {}
    with out_path.open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
            type_counts[chunk.content_type] = type_counts.get(chunk.content_type, 0) + 1
            key = chunk.section or "—"
            section_counts[key] = section_counts.get(key, 0) + 1

    print(f"Written {len(chunks)} chunks to {out_path}")
    print("By content_type:", dict(sorted(type_counts.items(), key=lambda x: -x[1])))
    distinct_entities = len({c.entity_name for c in chunks if c.entity_name})
    print(f"Distinct entities: {distinct_entities}")


if __name__ == "__main__":
    main()

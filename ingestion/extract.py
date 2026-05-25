"""
D&D PDF text extractor — pdfplumber-based, column-aware, font-driven.

Produces chunks.jsonl where each line is a JSON-serialised DndChunk.

Usage:
    uv run --with pdfplumber python ingestion/extract.py <pdf> [--book-slug <slug>] [--out <path>]

Example:
    uv run --with pdfplumber python ingestion/extract.py "PlayerDnDBasicRules_v0.2_PrintFriendly.pdf"

See docs/plans/dnd-pdf-parsing-guide.md for calibration details.

Two-pass extraction
-------------------
Pass 1 (TOC scan): For chapters whose content_type maps to "class_feature" or
"race_feature", scan all pages and collect every 20pt heading in document order.
The FIRST 20pt heading after the chapter boundary is the entity owner (e.g.
"Fighter"). Subsequent 20pt headings on that entity's pages are structural
subheadings ("Class Features", "Martial Archetypes") and must NOT overwrite the
owner. The pass produces a page → entity_owner dict.

Pass 2 (extraction): The existing column-extraction logic, but for 20pt lines
in entity-ownership chapters the class_name is resolved from the page-range map
rather than set directly from the heading text.
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
from typing import Iterator

import pdfplumber
from pdfplumber.page import Page

# ---------------------------------------------------------------------------
# Section 1: Per-book configuration
# ---------------------------------------------------------------------------
# Formatting varies across D&D books — calibrate with ingestion/calibrate.py
# and add a new entry here before ingesting a new book.

BOOK_CONFIGS: dict[str, dict] = {
    "phb-basic-v0.2": {
        "column_split_x": 297.0,       # page.width / 2; safe crop boundary
        "entity_heading_pt": 12.0,     # named entity headings (spells, conditions, features)
        "chapter_title_pt": 24.0,      # "Chapter 3: Classes"
        "entity_name_pt": 20.0,        # class/race names within a chapter
        "body_pts": {10.0, 9.0, 8.5},  # prose, italic, fine print
        "header_re": re.compile(       # page header to strip before processing
            r"^\d+\s*\nD&D Player's Basic Rules[^\n]*\n", re.MULTILINE
        ),
        "header_line_re": re.compile(  # single-line match to drop header lines in column extraction
            r"D&D Player.{0,3}s Basic Rules", re.IGNORECASE
        ),
        "spell_level_re": re.compile(  # line that follows a spell name
            r"^\s*(\d+(?:st|nd|rd|th)-level|cantrip)", re.IGNORECASE
        ),
        # Chapter headings whose pages should be skipped entirely.
        # Appendix C contains lore, character sheet forms, and marketing copy —
        # none of which has retrieval value for rules lookup.
        "skip_chapters": {"appendix c"},
    },
}

# ---------------------------------------------------------------------------
# Section 2: Chapter → content-type and part mapping
# ---------------------------------------------------------------------------

# Matches only genuine chapter/part/appendix boundary lines — used to guard
# context updates so continuation pages don't reset current_ctype to "rule".
_CHAPTER_BOUNDARY_RE = re.compile(
    r"^(chapter\s+\d+|part\s+\d+\s*[—–-]|appendix\s+[a-z]|introduction)",
    re.IGNORECASE,
)

_CHAPTER_META: list[tuple[re.Pattern, str, str | None]] = [
    # (pattern to match chapter heading, content_type, part)
    # IMPORTANT: multi-digit chapters (10, 11) must appear before single-digit
    # (chapter\s+1) to prevent "Chapter 11" matching the chapter-1 pattern.
    # \b word boundaries guard against partial digit matches.
    (re.compile(r"introduction", re.I),                          "narrative",      None),
    (re.compile(r"spellcasting|chapter\s+10\b", re.I),          "rule",           "Part 3"),
    (re.compile(r"spells|chapter\s+11\b", re.I),                "spell",          "Part 3"),
    (re.compile(r"step.by.step|chapter\s+1\b", re.I),           "rule",           "Part 1"),
    (re.compile(r"races|chapter\s+2\b", re.I),                  "race_feature",   "Part 1"),
    (re.compile(r"classes|chapter\s+3\b", re.I),                "class_feature",  "Part 1"),
    (re.compile(r"personality|background|chapter\s+4\b", re.I), "background",     "Part 1"),
    (re.compile(r"equipment|chapter\s+5\b", re.I),              "rule",           "Part 1"),
    (re.compile(r"customization|chapter\s+6\b", re.I),          "rule",           "Part 1"),
    (re.compile(r"ability scores|chapter\s+7\b", re.I),         "rule",           "Part 2"),
    (re.compile(r"adventuring|chapter\s+8\b", re.I),            "rule",           "Part 2"),
    (re.compile(r"combat|chapter\s+9\b", re.I),                 "rule",           "Part 2"),
    (re.compile(r"appendix\s+a|conditions", re.I),              "condition",      "Appendix"),
    (re.compile(r"appendix\s+[bc]", re.I),                      "narrative",      "Appendix"),
]

def _chapter_meta(heading: str) -> tuple[str, str | None]:
    """Return (content_type, part) for a chapter heading string."""
    for pattern, ctype, part in _CHAPTER_META:
        if pattern.search(heading):
            return ctype, part
    return "rule", None


# Content types that have entity ownership (class/race names as 20pt headings)
_ENTITY_OWNERSHIP_CTYPES = {"class_feature", "race_feature"}


# ---------------------------------------------------------------------------
# Section 2b: Pass-1 TOC scan — build page → entity-owner map
# ---------------------------------------------------------------------------

def _collect_entity_chapter_headings(
    pdf: "pdfplumber.PDF",
    cfg: dict,
) -> dict[str, tuple[list[tuple[int, int, float, str]], list[int]]]:
    """
    Collect all 20pt headings and all page numbers from entity-ownership chapters.

    Returns {chapter_heading: (headings, all_pages)} where:
    - headings = [(page_num, col_order, y, text), ...], col_order 0=left 1=right
    - all_pages = sorted list of every page number in that chapter

    Sorting headings by (page_num, col_order, y) gives true document order.
    """
    entity_name_pt = cfg["entity_name_pt"]
    header_line_re = cfg.get("header_line_re")
    split_x = cfg["column_split_x"]

    headings_map: dict[str, list[tuple[int, int, float, str]]] = {}
    pages_map: dict[str, list[int]] = {}
    current_chapter: str | None = None
    current_ctype = "rule"

    for page_num, page in enumerate(pdf.pages, start=1):
        raw = page.extract_text() or ""
        first_line = raw.split("\n")[0].strip() if raw else ""
        if first_line and _CHAPTER_BOUNDARY_RE.match(first_line):
            ctype, _ = _chapter_meta(first_line)
            current_chapter = first_line
            current_ctype = ctype

        if current_ctype not in _ENTITY_OWNERSHIP_CTYPES or current_chapter is None:
            continue

        if current_chapter not in headings_map:
            headings_map[current_chapter] = []
            pages_map[current_chapter] = []
        pages_map[current_chapter].append(page_num)

        for col_order, (x0, x1) in enumerate([(0, split_x), (split_x, page.width)]):
            cropped = page.crop((x0, 0, x1, page.height))
            if not cropped.chars:
                continue
            for line in _group_into_lines(cropped.chars):
                text = _line_text(line)
                if not text:
                    continue
                if header_line_re and header_line_re.search(text):
                    continue
                if abs(_dominant_size(line) - entity_name_pt) < 0.5:
                    y = line[0]["top"]
                    headings_map[current_chapter].append((page_num, col_order, y, text))

    return {ch: (headings_map[ch], pages_map[ch]) for ch in headings_map}


def _identify_entity_names(
    headings: list[tuple[int, int, float, str]],
) -> set[str]:
    """
    Given an ordered list of (page, col_order, y, text) 20pt headings from one
    chapter, return the set of text values that are entity names (class/race names).

    Strategy: the structural marker is the most-frequent 20pt heading text in
    the chapter (e.g. "Class Features" — appears once per class). Entity names
    are headings that immediately PRECEDE the structural marker in document order.
    The very first heading in the chapter is always an entity name (it opens the
    first entity's section).

    For chapters with no repeated heading (e.g. races in PHB basic), fall back to
    keeping ALL headings as entity names and excluding the first one IF it looks
    like a section intro (multi-word, appears before any other heading on the page).
    """
    if not headings:
        return set()

    # Sort by (page, col_order, y) to get document order
    ordered = sorted(headings, key=lambda h: (h[0], h[1], h[2]))
    texts = [h[3] for h in ordered]

    # Find the structural marker: the most-frequent heading text
    from collections import Counter
    freq = Counter(texts)
    max_freq = max(freq.values())

    if max_freq > 1:
        # There is a repeated structural marker (e.g. "Class Features")
        structural_marker = max(
            (t for t, c in freq.items() if c == max_freq), key=lambda t: freq[t]
        )
        entity_names: set[str] = set()
        # First heading in chapter is always an entity name
        if texts[0] != structural_marker:
            entity_names.add(texts[0])
        # Headings immediately preceding the structural marker are entity names
        for i, text in enumerate(texts):
            if text == structural_marker and i > 0 and texts[i - 1] != structural_marker:
                entity_names.add(texts[i - 1])
        return entity_names
    else:
        # No repeated heading: all unique headings could be entity names.
        # Exclude multi-word "section intro" headings that appear before ANY
        # single-word heading (e.g. "Choosing a Race" in the races chapter).
        # Single-word headings are always entity names (Elf, Dwarf, Halfling…).
        single_word = {t for t in texts if len(t.split()) == 1}
        if single_word:
            return single_word
        # All headings single-word anyway — treat all as entity names
        return set(texts)


def _build_entity_ownership_map(
    pdf: "pdfplumber.PDF",
    cfg: dict,
) -> dict[tuple[int, int], str]:
    """
    Pass 1: Scan every page and return a mapping of {(page_num, col_order): entity_owner}.

    For chapters whose content_type is in _ENTITY_OWNERSHIP_CTYPES, each
    (page, column) slot is assigned to the entity whose section it belongs to.

    Using (page, col) keys rather than page-only keys correctly handles the case
    where two entities start on the same page in different columns (e.g. the last
    Dwarf column and the first Elf column both on page N). A page-level map would
    assign the whole page to whichever entity started later, mislabeling the earlier
    entity's column. Column-level keys split them correctly.

    Method:
    1. Collect all 20pt headings per chapter with their document-order position
       (page, col_order, y).
    2. Identify which headings are entity names vs. structural subheadings.
    3. Walk all (page, col) slots in document order; assign each slot to the most
       recently seen entity heading.
    """
    chapter_data = _collect_entity_chapter_headings(pdf, cfg)

    page_owner: dict[tuple[int, int], str] = {}

    for chapter, (headings, all_pages) in chapter_data.items():
        if not headings:
            continue

        entity_names = _identify_entity_names(headings)
        ordered = sorted(headings, key=lambda h: (h[0], h[1], h[2]))

        # Entity starts keyed by (page, col_order) in document order
        entity_starts: list[tuple[int, int, str]] = [
            (page_num, col_order, text)
            for page_num, col_order, y, text in ordered
            if text in entity_names
        ]

        if not entity_starts:
            continue

        # Build a sorted list of all (page, col) slots in this chapter
        all_slots = sorted(
            ((pg, col) for pg in all_pages for col in [0, 1]),
            key=lambda x: (x[0], x[1]),
        )

        # Walk slots in document order, advancing the entity pointer when a new
        # entity heading is reached. Each slot is owned by the last entity seen.
        current_owner: str | None = None
        entity_idx = 0
        for slot_page, slot_col in all_slots:
            while (
                entity_idx < len(entity_starts)
                and (entity_starts[entity_idx][0], entity_starts[entity_idx][1]) <= (slot_page, slot_col)
            ):
                current_owner = entity_starts[entity_idx][2]
                entity_idx += 1
            if current_owner is not None:
                page_owner[(slot_page, slot_col)] = current_owner

    return page_owner


# ---------------------------------------------------------------------------
# Section 3: Output schema
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
    entity_name: str | None      # spell name, condition name, race name, etc.
    class_name: str | None       # parent class for class_feature chunks
    feature_name: str | None     # specific feature for class_feature chunks
    text: str


def _chunk_id(book_slug: str, page: int, col: int, idx: int) -> str:
    raw = f"{book_slug}:{page}:{col}:{idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Section 4: Char-level utilities
# ---------------------------------------------------------------------------

_Y_TOLERANCE = 2.0  # points — chars within this y-range share a line

def _group_into_lines(chars: list[dict]) -> list[list[dict]]:
    """Group pdfplumber chars into visual lines by y-position."""
    if not chars:
        return []
    sorted_chars = sorted(chars, key=lambda c: (round(c["top"] / _Y_TOLERANCE), c["x0"]))

    lines: list[list[dict]] = []
    for _, group in groupby(sorted_chars, key=lambda c: round(c["top"] / _Y_TOLERANCE)):
        lines.append(list(group))
    return lines


def _line_text(line: list[dict]) -> str:
    return "".join(c["text"] for c in line).strip()


def _dominant_size(line: list[dict]) -> float:
    """Font size that appears most often on this line (by character count)."""
    if not line:
        return 0.0
    counts: dict[float, int] = {}
    for c in line:
        sz = round(c["size"], 1)
        counts[sz] = counts.get(sz, 0) + 1
    return max(counts, key=counts.__getitem__)


# ---------------------------------------------------------------------------
# Section 5: Page-header extraction
# ---------------------------------------------------------------------------

def _parse_header(raw_text: str, cfg: dict) -> tuple[str, str]:
    """
    Return (first_line, raw_text). pdfplumber does not prepend page numbers,
    so the first line of extract_text() is the chapter heading on chapter-start
    pages, or body text on continuation pages. We use it only for chapter-change
    detection; the raw_text is passed through unmodified for column processing.
    """
    first_line = raw_text.split("\n")[0].strip() if raw_text else ""
    return first_line, raw_text


# ---------------------------------------------------------------------------
# Section 6: Table extraction
# ---------------------------------------------------------------------------

def _extract_table_chunks(
    page: Page,
    page_num: int,
    book_slug: str,
    source_file: str,
    chapter: str | None,
    section: str | None,
    part: str | None,
    chunk_counter: list[int],
) -> list[DndChunk]:
    """Extract each table on the page as an atomic chunk."""
    chunks: list[DndChunk] = []
    for table in page.extract_tables():
        if not table or not table[0]:
            continue
        # Render table as pipe-delimited text so it's human-readable in the vector store
        rows = [" | ".join(cell or "" for cell in row) for row in table if any(row)]
        text = "\n".join(rows)
        if not text.strip():
            continue

        # Quality filter: skip fragments that pdfplumber misidentifies as tables.
        # Rules (all must pass):
        #   1. ≥3 rows with any non-empty cell (header + ≥2 data rows)
        #   2. ≥2 non-empty cells in the first (header) row
        #   3. First row must not be all-numeric — an all-numeric "header" means
        #      pdfplumber missed the real header and kept only data rows. These
        #      fragments have no column labels and are useless for retrieval.
        data_rows = [r for r in table if any(cell and cell.strip() for cell in r)]
        non_empty_header_cells = [cell for cell in table[0] if cell and cell.strip()]
        header_is_all_numeric = all(
            re.sub(r"[,.\-–+\s]", "", c).isdigit() for c in non_empty_header_cells
        ) if non_empty_header_cells else True
        if len(data_rows) < 3 or len(non_empty_header_cells) < 2 or header_is_all_numeric:
            continue
        chunk_counter[0] += 1
        chunks.append(DndChunk(
            chunk_id=_chunk_id(book_slug, page_num, 99, chunk_counter[0]),
            book_slug=book_slug,
            source_file=source_file,
            page_start=page_num,
            page_end=page_num,
            part=part,
            chapter=chapter,
            section=section,
            content_type="table",
            entity_name=None,
            class_name=None,
            feature_name=None,
            text=text,
        ))
    return chunks


# ---------------------------------------------------------------------------
# Section 7: Column chunk extraction (font-driven boundary detection)
# ---------------------------------------------------------------------------

def _extract_column_chunks(
    page: Page,
    col_idx: int,          # 0 = left, 1 = right
    x0: float,
    x1: float,
    page_num: int,
    book_slug: str,
    source_file: str,
    chapter: str | None,
    section: str | None,
    part: str | None,
    default_content_type: str,
    cfg: dict,
    chunk_counter: list[int],
    entity_owner_map: dict[tuple[int, int], str] | None = None,
) -> list[DndChunk]:
    """
    Crop the page to [x0, x1], group chars into lines, use font-size
    transitions to identify chunk boundaries, and return DndChunk list.

    12pt lines  → named entity heading (spell, condition, feature…) → new chunk
    20pt lines  → class/race entity name or structural subheading → flush + update context
    24pt lines  → chapter/section title → update section context (not a chunk boundary)
    10/9/8.5pt  → body prose → accumulate into current chunk

    When entity_owner_map is provided (Pass-2 mode), 20pt headings that are
    structural subheadings ("Class Features", "Martial Archetypes", etc.) still
    flush the current chunk but do NOT overwrite class_name — the owner is
    resolved from the map instead.
    """
    cropped = page.crop((x0, 0, x1, page.height))
    chars = cropped.chars
    if not chars:
        return []

    lines = _group_into_lines(chars)
    chunks: list[DndChunk] = []

    entity_heading_pt = cfg["entity_heading_pt"]
    chapter_title_pt  = cfg["chapter_title_pt"]
    entity_name_pt    = cfg["entity_name_pt"]
    spell_level_re    = cfg["spell_level_re"]
    header_line_re    = cfg.get("header_line_re")

    current_lines: list[str] = []
    current_entity: str | None = None
    current_class:  str | None = None
    current_feature: str | None = None
    current_ctype = default_content_type

    # Seed class/entity from the ownership map when entering a new (page, col) —
    # ensures even pages where the entity name heading has already scrolled past
    # (multi-page entity sections) carry the correct owner from the first chunk.
    if entity_owner_map is not None:
        owner = entity_owner_map.get((page_num, col_idx))
        if owner:
            current_class = owner
            current_entity = owner

    def _flush() -> None:
        text = "\n".join(current_lines).strip()
        if not text or len(text.split()) < 5:
            current_lines.clear()
            return
        chunk_counter[0] += 1
        # For class_feature chunks, entity_name holds the feature name
        ename = current_entity
        cname = current_class
        fname = current_feature
        if current_ctype == "class_feature" and current_feature:
            ename = None  # class_name + feature_name are the primary keys
        chunks.append(DndChunk(
            chunk_id=_chunk_id(book_slug, page_num, col_idx, chunk_counter[0]),
            book_slug=book_slug,
            source_file=source_file,
            page_start=page_num,
            page_end=page_num,
            part=part,
            chapter=chapter,
            section=section,
            content_type=current_ctype,
            entity_name=ename,
            class_name=cname,
            feature_name=fname,
            text=text,
        ))
        current_lines.clear()

    for line in lines:
        text = _line_text(line)
        if not text:
            continue
        if header_line_re and header_line_re.search(text):
            continue
        size = _dominant_size(line)

        if abs(size - chapter_title_pt) < 0.5:
            # Chapter/section title — update context, do not start a new chunk
            current_lines.append(text)

        elif abs(size - entity_name_pt) < 0.5:
            # 20pt heading — could be the entity name ("Fighter") or a structural
            # subheading ("Class Features", "Martial Archetypes"). Always flush so
            # each subsection becomes its own chunk. For ownership, prefer the
            # page-range map over the raw heading text when in Pass-2 mode.
            _flush()
            if entity_owner_map is not None:
                resolved = entity_owner_map.get((page_num, col_idx), text)
                current_class = resolved
                current_entity = resolved
            else:
                current_class = text
                current_entity = text
            current_feature = None
            current_lines.append(text)

        elif abs(size - entity_heading_pt) < 0.5:
            # Named entity heading — spell name, condition, feature, background
            # Check if the NEXT line confirms it's a spell (Nth-level / cantrip)
            _flush()
            current_entity = text
            current_feature = text if current_ctype == "class_feature" else None
            current_lines.append(text)

        else:
            # Body text / italic / fine print
            current_lines.append(text)

    _flush()
    return chunks


# ---------------------------------------------------------------------------
# Section 8: Main PDF pipeline
# ---------------------------------------------------------------------------

def extract_pdf(
    pdf_path: str,
    book_slug: str = "phb-basic-v0.2",
) -> Iterator[DndChunk]:
    """
    Yield DndChunk objects for every page of the PDF.
    Processes left column then right column; extracts tables separately.

    Two-pass strategy: Pass 1 builds a page → entity-owner map for chapters
    with entity ownership (races, classes). Pass 2 uses that map so structural
    20pt subheadings ("Class Features", "Martial Archetypes") never overwrite
    the true entity owner resolved in Pass 1.
    """
    cfg = BOOK_CONFIGS[book_slug]
    source_file = Path(pdf_path).name
    split_x = cfg["column_split_x"]
    chunk_counter = [0]  # mutable so nested helpers can increment it

    skip_chapters: set[str] = {s.lower() for s in cfg.get("skip_chapters", set())}

    with pdfplumber.open(pdf_path) as pdf:
        # -- Pass 1: build entity-ownership map --------------------------------
        print("  Pass 1: scanning entity ownership...", flush=True)
        entity_owner_map = _build_entity_ownership_map(pdf, cfg)
        print(f"  Pass 1 complete — {len(entity_owner_map)} pages mapped", flush=True)

        # -- Pass 2: extract chunks --------------------------------------------
        total = len(pdf.pages)
        # Persistent chapter context across pages
        current_chapter: str | None = None
        current_section: str | None = None
        current_part:    str | None = None
        current_ctype  = "rule"

        for page_num, page in enumerate(pdf.pages, start=1):
            print(f"\r  Pass 2: page {page_num}/{total}", end="", flush=True)

            # -- Parse page header to track chapter context ------------------
            # Only update context when the first line is a genuine chapter
            # boundary (Chapter N / Part N / Appendix X / Introduction).
            # Continuation pages whose first line is body text or a spell name
            # must not reset current_ctype — the context persists until the
            # next real chapter heading appears.
            raw = page.extract_text() or ""
            chapter_heading, _ = _parse_header(raw, cfg)
            if chapter_heading and _CHAPTER_BOUNDARY_RE.match(chapter_heading):
                ctype, part = _chapter_meta(chapter_heading)
                current_chapter = chapter_heading
                current_ctype   = ctype
                if part:
                    current_part = part

            # -- Skip chapters explicitly excluded in book config -------------
            if current_chapter and any(
                s in current_chapter.lower() for s in skip_chapters
            ):
                continue

            # Provide ownership map only for entity-ownership chapters so that
            # non-entity chapters (spells, conditions) keep the old behaviour.
            page_map = entity_owner_map if current_ctype in _ENTITY_OWNERSHIP_CTYPES else None

            # -- Tables (extracted before column text to avoid duplication) --
            yield from _extract_table_chunks(
                page, page_num, book_slug, source_file,
                current_chapter, current_section, current_part,
                chunk_counter,
            )

            # -- Left column -------------------------------------------------
            yield from _extract_column_chunks(
                page, col_idx=0, x0=0, x1=split_x,
                page_num=page_num, book_slug=book_slug, source_file=source_file,
                chapter=current_chapter, section=current_section, part=current_part,
                default_content_type=current_ctype,
                cfg=cfg, chunk_counter=chunk_counter,
                entity_owner_map=page_map,
            )

            # -- Right column ------------------------------------------------
            yield from _extract_column_chunks(
                page, col_idx=1, x0=split_x, x1=page.width,
                page_num=page_num, book_slug=book_slug, source_file=source_file,
                chapter=current_chapter, section=current_section, part=current_part,
                default_content_type=current_ctype,
                cfg=cfg, chunk_counter=chunk_counter,
                entity_owner_map=page_map,
            )

    print()  # newline after progress


# ---------------------------------------------------------------------------
# Section 9: CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract D&D PDF into chunks.jsonl")
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--book-slug", default="phb-basic-v0.2",
                        choices=list(BOOK_CONFIGS.keys()),
                        help="Book identifier (must exist in BOOK_CONFIGS)")
    parser.add_argument("--out", default="chunks.jsonl",
                        help="Output JSONL path (default: chunks.jsonl)")
    args = parser.parse_args()

    if not Path(args.pdf).exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    print(f"Extracting: {args.pdf}  (book-slug: {args.book_slug})")

    out_path = Path(args.out)
    total_chunks = 0
    type_counts: dict[str, int] = {}

    with out_path.open("w", encoding="utf-8") as f:
        for chunk in extract_pdf(args.pdf, book_slug=args.book_slug):
            f.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
            total_chunks += 1
            type_counts[chunk.content_type] = type_counts.get(chunk.content_type, 0) + 1

    print(f"Written {total_chunks} chunks to {out_path}")
    print("Breakdown by content_type:")
    for ctype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {ctype:20s}: {count}")


if __name__ == "__main__":
    main()

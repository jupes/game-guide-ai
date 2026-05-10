"""
Spike: pdfplumber PDF extraction for D&D Basic Rules PDF.

Usage:
  uv run --with pdfplumber extract_pdfplumber.py <path-to-pdf> [--out output.jsonl]

Outputs one JSON line per page-block:
  { "page": int, "block_index": int, "heading_level": 1|2|3|null,
    "heading_text": str|null, "raw_text": str, "tables": [...] }

Evaluated on:
  - Chapter 5 (Equipment tables) — do table rows stay intact?
  - Chapter 11 (Spells) — does each spell appear as a contiguous block?
  - Chapter headings — are heading levels detected?
"""

import json
import re
import sys
import argparse
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("ERROR: pdfplumber not installed. Run: uv run --with pdfplumber extract_pdfplumber.py ...")
    sys.exit(1)

CHAPTER_RE = re.compile(r"^(chapter\s+\d+[:.]|part\s+\d+\s*[—-]|appendix\s+[a-z][:.])", re.IGNORECASE)
SECTION_RE = re.compile(r"^[A-Z][A-Z\s]{4,}$")  # ALL-CAPS headings typical of D&D PDFs


def detect_heading(text: str) -> tuple[int | None, str | None]:
    line = text.strip().split("\n")[0].strip()
    if CHAPTER_RE.match(line):
        return 1, line
    if SECTION_RE.match(line) and len(line) < 60:
        return 2, line
    return None, None


def extract(pdf_path: str) -> list[dict]:
    results = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()
            table_data = []
            for t in tables:
                table_data.append([[cell or "" for cell in row] for row in t])

            # Extract text with layout preservation
            text = page.extract_text(layout=True) or ""
            blocks = [b.strip() for b in text.split("\n\n") if b.strip()]

            for i, block in enumerate(blocks):
                level, heading_text = detect_heading(block)
                results.append({
                    "page": page_num,
                    "block_index": i,
                    "heading_level": level,
                    "heading_text": heading_text,
                    "raw_text": block,
                    "tables": table_data if i == 0 else [],  # attach tables to first block of page
                })
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("--out", default="raw-extract-pdfplumber.jsonl", help="Output JSONL path")
    args = parser.parse_args()

    print(f"Extracting: {args.pdf}")
    blocks = extract(args.pdf)
    print(f"Extracted {len(blocks)} blocks from {blocks[-1]['page'] if blocks else 0} pages")

    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        for block in blocks:
            f.write(json.dumps(block, ensure_ascii=False) + "\n")

    print(f"Written to: {out_path}")

    # Spot-checks printed to stdout for spike evaluation
    print("\n=== SPOT CHECK: Chapters (heading_level=1) ===")
    chapters = [b for b in blocks if b["heading_level"] == 1]
    for c in chapters[:15]:
        print(f"  p{c['page']:03d}: {c['heading_text']}")

    print("\n=== SPOT CHECK: Spell blocks (search 'Fireball') ===")
    fireball = [b for b in blocks if "fireball" in b["raw_text"].lower()]
    for b in fireball[:3]:
        print(f"  p{b['page']:03d} block {b['block_index']}: {b['raw_text'][:200]!r}")

    print("\n=== SPOT CHECK: Tables found ===")
    table_pages = [b for b in blocks if b["tables"]]
    for b in table_pages[:5]:
        for t in b["tables"]:
            print(f"  p{b['page']:03d}: {len(t)} rows x {len(t[0]) if t else 0} cols — header: {t[0] if t else []}")

    print(f"\nTotal blocks: {len(blocks)}")
    print(f"Blocks with heading_level=1: {sum(1 for b in blocks if b['heading_level'] == 1)}")
    print(f"Blocks with heading_level=2: {sum(1 for b in blocks if b['heading_level'] == 2)}")
    print(f"Pages with tables: {sum(1 for b in blocks if b['tables'])}")


if __name__ == "__main__":
    main()

"""
Orchestrate the full ingest pipeline for the Tier-A 5E books:

    extract (extract_scan)  →  QA gate (qa_chunks)  →  embed clean (embed)

For each book it writes chunks-<slug>.jsonl + .clean.jsonl + .quarantine.jsonl
+ .qa.json, embeds only the clean chunks, and prints a per-book QA summary so a
low pass-rate flags a book whose config needs tuning before its data lands.

Usage:
    uv run --with pymupdf --with pdfplumber --with "psycopg[binary]" --with openai \
        python ingestion/ingest_books.py                # all books
    ... python ingestion/ingest_books.py --only xge-5e tce-5e   # subset
    ... python ingestion/ingest_books.py --no-embed             # extract+QA only
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import extract_scan as ex
import qa_chunks
from embed import embed_and_upsert, DEFAULT_DSN, DEFAULT_BACKEND, DEFAULT_MODEL_OAI
import os

BOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "DnD-Books" / "5e" / "Books"

# slug → PDF filename (Tier-A, cleanly extractable). Wayfinders + Blood Hunter
# are OCR-blocked and deferred (bead T7); NLRMEv2 + rest of UO excluded.
BOOKS: dict[str, str] = {
    "phb-5e":     "D&D 5E - Player's Handbook.pdf",
    "xge-5e":     "D&D 5E - Xanathar's Guide to Everything.pdf",
    "tce-5e":     "D&D 5E - Tasha's Cauldron of Everything.pdf",
    "vgm-5e":     "D&D 5E - Volo's Guide to Monsters.pdf",
    "mtf-5e":     "D&D 5E - Mordenkainen's Tome of Foes.pdf",
    "eepc-5e":    "D&D 5E - Elemental Evil Player's Companion.pdf",
    "scag-5e":    "D&D 5E - Sword Coast Adventurer's Guide.pdf",
    "tortle-5e":  "D&D 5E - The Tortle Package.pdf",
    "eberron-5e": "D&D 5E - Eberron - Rising from the Last War.pdf",
    "ravnica-5e": "D&D 5E - Guildmasters' Guide to Ravnica.pdf",
}


def extract_book(slug: str, pdf_path: Path, out_path: Path) -> dict[str, int]:
    cfg = ex.BOOK_CONFIGS[slug]
    engine = cfg.get("engine", "fitz")
    reader = ex.read_pdf_stream_fitz if engine == "fitz" else ex.read_pdf_stream
    stream = reader(str(pdf_path))
    if cfg["kind"] == "monster_manual":
        chunks = ex.extract_mm_chunks(stream, slug, pdf_path.name, cfg)
    elif cfg["kind"] == "dmg":
        chunks = ex.extract_dmg_chunks(stream, slug, pdf_path.name, cfg)
    else:
        chunks = ex.extract_supplement_chunks(stream, slug, pdf_path.name, cfg)
    with out_path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    type_counts: dict[str, int] = {}
    for c in chunks:
        type_counts[c.content_type] = type_counts.get(c.content_type, 0) + 1
    return type_counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Tier-A 5E books (extract → QA → embed)")
    parser.add_argument("--only", nargs="*", help="Subset of slugs to process")
    parser.add_argument("--no-embed", action="store_true", help="Extract + QA only, skip embedding")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    slugs = args.only or list(BOOKS)
    here = Path(__file__).parent
    summary: list[dict] = []

    for slug in slugs:
        pdf_path = BOOKS_DIR / BOOKS[slug]
        if not pdf_path.exists():
            print(f"!! {slug}: PDF not found at {pdf_path}", file=sys.stderr)
            continue
        print(f"\n=== {slug} ===")
        raw = here / f"chunks-{slug}.jsonl"
        clean = here / f"chunks-{slug}.clean.jsonl"
        quarantine = here / f"chunks-{slug}.quarantine.jsonl"
        report_path = here / f"chunks-{slug}.qa.json"

        type_counts = extract_book(slug, pdf_path, raw)
        print(f"  extracted {sum(type_counts.values())} chunks: {type_counts}")

        report = qa_chunks.run_qa(raw, clean, quarantine)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  QA: clean={report['clean']}/{report['total']} "
              f"({report['pass_rate']:.1%})  reasons={report['reasons']}")

        if not args.no_embed:
            embed_and_upsert(clean, args.dsn, DEFAULT_BACKEND, DEFAULT_MODEL_OAI, "")

        summary.append({
            "slug": slug, "extracted": report["total"], "clean": report["clean"],
            "pass_rate": report["pass_rate"], "types": type_counts,
        })

    print("\n" + "=" * 64)
    print(f"{'book':12s} {'extracted':>9s} {'clean':>7s} {'pass':>6s}")
    for s in summary:
        print(f"{s['slug']:12s} {s['extracted']:9d} {s['clean']:7d} {s['pass_rate']:6.1%}")
    print("=" * 64)
    print(f"total clean chunks: {sum(s['clean'] for s in summary)}")


if __name__ == "__main__":
    main()

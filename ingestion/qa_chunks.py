"""
Pre-embedding data-quality gate.

Past corpus iterations were bitten by PDF-reader failures reaching the vector
store: custom-font glyphs (Private-Use-Area code points), undecoded CID fonts,
junk OCR layers, and OCR-mangled entity names. This module validates extracted
chunks BEFORE embedding and quarantines anything matching a known failure
signature, so garbage never gets embedded.

Design notes:
- Validators are pure (text/str in, bool/float out) and unit-tested with real
  failure samples — no DB, no PDF, no network.
- `dict_word_ratio` from the plan was dropped for v1: it needs an English
  wordlist absent from the repo and stdlib, and the alpha/PUA/CID checks already
  catch every observed failure class. Add it later if needed.

Usage:
    uv run python ingestion/qa_chunks.py ingestion/chunks-xge-5e.jsonl
    # writes chunks-xge-5e.clean.jsonl + chunks-xge-5e.quarantine.jsonl + .qa.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Thresholds (starting points from the research table)
# ---------------------------------------------------------------------------

PUA_CONTROL_MAX = 0.02      # > 2% PUA/control chars → custom-font / control garbage
ALPHA_RATIO_MIN = 0.50      # < 50% alphabetic (of non-space) → junk OCR
# Runaway-merge guard. A complete legendary monster stat block (all traits,
# actions, legendary + lair actions) legitimately runs 3,000–4,500 chars and is
# atomic — it must NOT be split or quarantined. The cap catches genuine
# extraction merge bugs where unrelated entities glom together (8,000+ chars).
MAX_CHUNK_CHARS = 8000
MIN_WORDS = 5               # fragment guard
ENTITY_NAME_MAX = 48
ENTITY_ALPHA_MIN = 0.80     # entity names should be almost all letters/spaces
ENTITY_MAX_WORDS = 6        # real spell/monster names are short; more = a sentence

# Stat-field / section labels that leaked into entity_name in the PHB spell section
# (fault D). Never a real entity name.
_FIELD_SECTION_STOP = frozenset({
    "casting time", "range", "components", "duration", "at higher levels",
    "spell descriptions", "classes", "ritual", "prerequisite", "spell lists",
})

_CID_RE = re.compile(r"\(cid:\d+\)")
_PUA_CONTROL_RE = re.compile(r"[-\x00-\x08\x0b-\x1f\x7f]")


# Collapse detector: an entity that merges multiple distinct items shows more than
# one stat-anchor across its chunks (a spell has one "Casting Time"; a monster one
# "Armor Class N"). >1 means several entities were merged under one name (e.g. six
# giants named "Giants"). A single large entity (Demilich: one stat block + much
# lore) stays at 1 and is not flagged.
COLLAPSE_MAX_ANCHORS = 1
# Require the FIELD form ("Casting Time:" with a colon) so a spell description that
# merely mentions "a casting time of 1 action" in prose is not miscounted.
_CASTING_COUNT_RE = re.compile(r"casting\s+time\s*:", re.IGNORECASE)
_AC_COUNT_RE = re.compile(r"armor\s+class\s*\d", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pure validators
# ---------------------------------------------------------------------------

def pua_control_ratio(text: str) -> float:
    """Fraction of characters in the Private-Use-Area or control ranges."""
    if not text:
        return 0.0
    bad = len(_PUA_CONTROL_RE.findall(text))
    return bad / len(text)


def has_cid_marker(text: str) -> bool:
    """True if the text contains undecoded CID font markers like (cid:107)."""
    return _CID_RE.search(text) is not None


def alpha_ratio(text: str) -> float:
    """Fraction of non-space characters that are alphabetic."""
    non_space = [c for c in text if not c.isspace()]
    if not non_space:
        return 0.0
    return sum(c.isalpha() for c in non_space) / len(non_space)


def length_ok(text: str, max_chars: int = MAX_CHUNK_CHARS) -> bool:
    """True if the chunk is neither a fragment nor a runaway merge."""
    return len(text.split()) >= MIN_WORDS and len(text) <= max_chars


def entity_name_ok(name: str | None) -> bool:
    """
    True if the entity name is absent (legitimate for rule/lore) or looks like a
    real name: short and almost entirely letters/spaces (rejects OCR garbage like
    '0Rog' or 'DUE;&GAR').
    """
    if name is None:
        return True
    n = name.strip()
    if not n or len(n) > ENTITY_NAME_MAX:
        return False
    low = n.lower()
    # stat-field / section label (with or without a trailing ': value')
    if low.split(":")[0].strip() in _FIELD_SECTION_STOP or low.rstrip(".:").strip() in _FIELD_SECTION_STOP:
        return False
    # sentence fragment popped from body: ends in a period, or too many words
    if n.endswith(".") or len(n.split()) > ENTITY_MAX_WORDS:
        return False
    visible = n.replace(" ", "").replace("'", "").replace("-", "").replace("(", "").replace(")", "")
    if not visible:
        return False
    alpha = sum(c.isalpha() for c in visible)
    return alpha / len(visible) >= ENTITY_ALPHA_MIN


# ---------------------------------------------------------------------------
# Corpus-wide collapse detector (aggregate — not per-chunk)
# ---------------------------------------------------------------------------

def _anchor_count(text: str, content_type: str) -> int:
    if content_type == "spell":
        return len(_CASTING_COUNT_RE.findall(text))
    if content_type == "monster":
        return len(_AC_COUNT_RE.findall(text))
    return 0


def detect_collapse(chunks: list[dict], max_anchors: int = COLLAPSE_MAX_ANCHORS) -> list[dict]:
    """
    Find entities that merge multiple distinct items. Group spell/monster chunks
    by (book_slug, content_type, entity_name); flag any group whose chunks
    collectively contain more than `max_anchors` stat-anchors (Casting Time /
    Armor Class) — that means several entities were merged under one name.

    Returns offenders sorted worst-first: {book, content_type, entity, anchors, chunks}.
    """
    from collections import defaultdict

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for c in chunks:
        ent = c.get("entity_name")
        ctype = c.get("content_type")
        if not ent or ctype not in ("spell", "monster"):
            continue
        groups[(c.get("book_slug"), ctype, ent)].append(c)

    offenders: list[dict] = []
    for (book, ctype, ent), cs in groups.items():
        anchors = sum(_anchor_count(c.get("text", "") or "", ctype) for c in cs)
        if anchors > max_anchors:
            offenders.append({"book": book, "content_type": ctype, "entity": ent,
                              "anchors": anchors, "chunks": len(cs)})
    offenders.sort(key=lambda o: -o["anchors"])
    return offenders


# ---------------------------------------------------------------------------
# Combined classifier
# ---------------------------------------------------------------------------

def classify_chunk(chunk: dict, max_chars: int = MAX_CHUNK_CHARS) -> tuple[bool, list[str]]:
    """
    Return (ok, reasons). ok=True means the chunk is safe to embed; otherwise
    reasons lists every failed check (a chunk can fail several at once).
    """
    text = chunk.get("text", "") or ""
    reasons: list[str] = []

    if has_cid_marker(text):
        reasons.append("cid")
    if pua_control_ratio(text) > PUA_CONTROL_MAX:
        reasons.append("pua_control")
    if alpha_ratio(text) < ALPHA_RATIO_MIN:
        reasons.append("low_alpha")
    if not length_ok(text, max_chars):
        reasons.append("length")
    if not entity_name_ok(chunk.get("entity_name")):
        reasons.append("bad_entity")

    return (len(reasons) == 0, reasons)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_qa(
    in_path: Path,
    clean_path: Path,
    quarantine_path: Path,
    max_chars: int = MAX_CHUNK_CHARS,
) -> dict:
    """
    Split a chunks JSONL into clean + quarantine files and return a report dict
    (counts, per-reason tallies, sample offenders).
    """
    chunks = [json.loads(l) for l in in_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    clean: list[dict] = []
    quarantined: list[dict] = []
    reason_tally: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    for c in chunks:
        ok, reasons = classify_chunk(c, max_chars)
        if ok:
            clean.append(c)
        else:
            quarantined.append(c)
            for r in reasons:
                reason_tally[r] = reason_tally.get(r, 0) + 1
                if len(samples.setdefault(r, [])) < 3:
                    samples[r].append((c.get("text", "") or "")[:80])

    clean_path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in clean) + ("\n" if clean else ""),
        encoding="utf-8",
    )
    quarantine_path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in quarantined) + ("\n" if quarantined else ""),
        encoding="utf-8",
    )

    total = len(chunks)
    return {
        "source": in_path.name,
        "total": total,
        "clean": len(clean),
        "quarantined": len(quarantined),
        "pass_rate": round(len(clean) / total, 4) if total else 1.0,
        "reasons": dict(sorted(reason_tally.items(), key=lambda x: -x[1])),
        "samples": samples,
    }


_DEFAULT_DSN = "postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat"


def _load_chunks_from_db(dsn: str) -> list[dict]:
    import psycopg  # local import — only needed for --from-db
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT book_slug, content_type, entity_name, page_start, text FROM dnd.chunks")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _run_collapse_check(chunks: list[dict]) -> int:
    offenders = detect_collapse(chunks)
    if not offenders:
        print(f"collapse-check: OK — no merged entities across {len(chunks)} chunks")
        return 0
    print(f"collapse-check: FAIL — {len(offenders)} merged-entity offender(s):")
    for o in offenders[:30]:
        print(f"  {o['book'] or '?':10} {o['content_type']:7} {o['entity']!r:42} "
              f"anchors={o['anchors']} chunks={o['chunks']}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-embedding QA gate for D&D chunks")
    parser.add_argument("chunks", nargs="?", help="Input chunks JSONL (omit with --from-db)")
    parser.add_argument("--max-chars", type=int, default=MAX_CHUNK_CHARS)
    parser.add_argument("--collapse-check", action="store_true",
                        help="Run the corpus collapse detector as a gate (exit 1 on offenders)")
    parser.add_argument("--from-db", action="store_true",
                        help="Load chunks from pgvector (DATABASE_URL) instead of a JSONL")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", _DEFAULT_DSN))
    args = parser.parse_args()

    if args.collapse_check:
        if args.from_db:
            chunks = _load_chunks_from_db(args.dsn)
        else:
            if not args.chunks or not Path(args.chunks).exists():
                print("ERROR: provide a chunks JSONL or --from-db", file=sys.stderr)
                sys.exit(2)
            chunks = [json.loads(l) for l in Path(args.chunks).read_text(encoding="utf-8").splitlines() if l.strip()]
        sys.exit(_run_collapse_check(chunks))

    in_path = Path(args.chunks) if args.chunks else None
    if in_path is None or not in_path.exists():
        print(f"ERROR: chunks file not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    stem = in_path.stem  # e.g. chunks-xge-5e
    clean_path = in_path.with_name(f"{stem}.clean.jsonl")
    quarantine_path = in_path.with_name(f"{stem}.quarantine.jsonl")
    report_path = in_path.with_name(f"{stem}.qa.json")

    report = run_qa(in_path, clean_path, quarantine_path, args.max_chars)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"QA: {report['source']}")
    print(f"  total={report['total']}  clean={report['clean']}  "
          f"quarantined={report['quarantined']}  pass_rate={report['pass_rate']:.1%}")
    if report["reasons"]:
        print(f"  reasons: {report['reasons']}")
    print(f"  → {clean_path.name}, {quarantine_path.name}, {report_path.name}")


if __name__ == "__main__":
    main()

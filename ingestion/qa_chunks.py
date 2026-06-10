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

_CID_RE = re.compile(r"\(cid:\d+\)")
_PUA_CONTROL_RE = re.compile(r"[-\x00-\x08\x0b-\x1f\x7f]")


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
    visible = n.replace(" ", "").replace("'", "").replace("-", "").replace("(", "").replace(")", "")
    if not visible:
        return False
    alpha = sum(c.isalpha() for c in visible)
    return alpha / len(visible) >= ENTITY_ALPHA_MIN


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-embedding QA gate for D&D chunks")
    parser.add_argument("chunks", help="Input chunks JSONL")
    parser.add_argument("--max-chars", type=int, default=MAX_CHUNK_CHARS)
    args = parser.parse_args()

    in_path = Path(args.chunks)
    if not in_path.exists():
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

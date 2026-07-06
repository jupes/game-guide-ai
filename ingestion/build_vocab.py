"""
Build the 5E domain vocabulary used by ocr_normalize's l->t recovery pass.

The PHB scan systematically misreads ``t`` as ``l`` (``aclion``, ``crealure``,
``Conslilulion``). The long tail of distinct garbled forms makes a curated list
insufficient, so ocr_normalize checks candidate tokens against a vocabulary of
words that appear in the OTHER (cleanly extracted) 5E books: a token that is
not in the vocabulary but whose single l->t repair is, gets repaired.

This script regenerates ``vocab_5e.txt`` from the non-PHB clean chunk files.
The output is checked in so extraction does not depend on which books have
been extracted locally.

Usage (from repos/game-guide-ai):
    uv run python ingestion/build_vocab.py
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "vocab_5e.txt"

# Non-PHB books with a QA'd clean file. PHB is excluded (it is the corrupted
# source we are repairing); books without a .clean.jsonl are excluded too.
SOURCE_SLUGS = ["xge-5e", "tce-5e", "mtf-5e", "eepc-5e", "scag-5e",
                "eberron-5e", "ravnica-5e", "mm-5e"]

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']+")

# A word must appear this many times across the source books to count as
# vocabulary — filters typos/garbles the sources themselves may contain.
MIN_FREQ = 3


def build_vocab(paths: list[Path], min_freq: int = MIN_FREQ) -> set[str]:
    freq: collections.Counter[str] = collections.Counter()
    for path in paths:
        with path.open(encoding="utf-8") as f:
            for line in f:
                for tok in _TOKEN_RE.findall(json.loads(line)["text"]):
                    freq[tok.lower()] += 1
    return {w for w, n in freq.items() if n >= min_freq}


def main() -> None:
    paths = [HERE / f"chunks-{slug}.clean.jsonl" for slug in SOURCE_SLUGS]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise SystemExit(f"missing source chunk files: {missing}")
    vocab = build_vocab([p for p in paths if p.exists()])
    OUT_PATH.write_text("\n".join(sorted(vocab)) + "\n", encoding="utf-8")
    print(f"Wrote {len(vocab)} words to {OUT_PATH.name}")


if __name__ == "__main__":
    main()

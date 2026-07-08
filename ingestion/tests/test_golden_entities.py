"""
Golden-entity regression test (CP-E of the extraction-quality audit, 0im).

Asserts every canonical entity in golden_entities.json appears as a correctly-named
chunk of the right content_type in the committed chunks-<book>.jsonl. This is the
permanent guard against the Fireball-class regression: if a re-extraction ever drops
or mis-names one of these, this test goes red.

Pure — reads the committed JSONLs, no DB / no PDF / no network.

Run from repos/game-guide-ai:
    uv run --with '.[test]' python -m pytest ingestion/test_golden_entities.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

ING = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ING / "golden_entities.json").read_text(encoding="utf-8"))["entities"]


def _entities_in(book_slug: str, content_type: str) -> set[str]:
    path = ING / f"chunks-{book_slug}.jsonl"
    if not path.exists():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("content_type") == content_type and row.get("entity_name"):
            names.add(row["entity_name"].strip().lower())
    return names


def test_canonical_entities_present():
    # read each (book, content_type) JSONL once
    by_source: dict[tuple[str, str], list[str]] = {}
    for g in GOLDEN:
        by_source.setdefault((g["book_slug"], g["content_type"]), []).append(g["entity"])

    missing: list[str] = []
    for (book, ctype), wanted in by_source.items():
        present = _entities_in(book, ctype)
        for name in wanted:
            if name.strip().lower() not in present:
                missing.append(f"{book}/{ctype}/{name}")

    assert not missing, f"canonical entities missing from extraction: {missing}"

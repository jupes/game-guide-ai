"""
Golden-entity regression test (CP-E of the extraction-quality audit, 0im).

Asserts every canonical entity in golden_entities.json appears as a correctly-named
chunk of the right content_type in the committed chunks-<book>.jsonl. This is the
permanent guard against the Fireball-class regression: if a re-extraction ever drops
or mis-names one of these, this test goes red.

Pure — reads the committed JSONLs, no DB / no PDF / no network.

Run from repos/rag-chat:
    uv run python ingestion/test_golden_entities.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ING = Path(__file__).resolve().parent
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


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _run()

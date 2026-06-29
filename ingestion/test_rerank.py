"""
Unit tests for rerank.py — content-type gate + ordering (pure, no torch/DB).

Run:
    uv run --with '.[test]' python -m pytest ingestion/test_rerank.py -q
"""

from __future__ import annotations

import sys

from ingestion.rerank import SKIP_RERANK_CTYPES, rerank_order, should_rerank


# ---------------------------------------------------------------------------
# should_rerank — the content-type gate
# ---------------------------------------------------------------------------

def test_skip_structured_types():
    # Categories where vector+filter already nail rank-1 (spike: net ≤ 0)
    assert should_rerank({"monster"}) is False
    assert should_rerank({"spell"}) is False
    assert should_rerank({"magic_item"}) is False
    assert should_rerank({"condition"}) is False
    assert should_rerank({"race_feature"}) is False


def test_rerank_prose_types():
    # Categories where the spike showed clean gains
    assert should_rerank({"rule"}) is True
    assert should_rerank({"feat"}) is True
    assert should_rerank({"dm_guidance"}) is True


def test_rerank_on_empty_unknown():
    # No inferred type → prose-biased fallback (rerank). Safe direction.
    assert should_rerank(set()) is True


def test_mixed_set_skips_if_any_structured():
    # If any structured type is present, skip — don't risk the regression
    assert should_rerank({"rule", "monster"}) is False
    assert should_rerank({"feat", "spell"}) is False


def test_skip_set_membership():
    assert "monster" in SKIP_RERANK_CTYPES
    assert "rule" not in SKIP_RERANK_CTYPES


# ---------------------------------------------------------------------------
# rerank_order — stable index sort by descending score
# ---------------------------------------------------------------------------

def test_order_descending():
    assert rerank_order([0.1, 0.9, 0.5]) == [1, 2, 0]


def test_order_stable_on_ties():
    # Equal scores keep original relative order (stable)
    assert rerank_order([0.5, 0.5, 0.9]) == [2, 0, 1]


def test_order_already_sorted():
    assert rerank_order([0.9, 0.5, 0.1]) == [0, 1, 2]


def test_order_empty():
    assert rerank_order([]) == []


def test_order_single():
    assert rerank_order([0.42]) == [0]


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

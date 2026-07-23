"""
Guards for the CI retrieval-regression gate (scripts/ci/eval_gate.py).

Pure — fabricated result files in tmp_path, no DB or network. The script is
loaded by path (scripts/ is not a package).

Run from repo root:
    uv run --with '.[test]' python -m pytest tests/test_eval_gate.py -q
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "eval_gate", Path(__file__).resolve().parent.parent / "scripts" / "ci" / "eval_gate.py",
)
eval_gate = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(eval_gate)


def _rows(hit: float, recall: float, n: int = 10) -> list[dict]:
    """n positive rows averaging to the given rates, plus a negative row that
    aggregation must ignore (it has no hit/recall fields, like the real file)."""
    hits = int(round(hit * n))
    recalls = int(round(recall * n))
    rows: list[dict] = [
        {
            "hit_at_1": 1 if i < hits else 0,
            "recall_at_10": 1 if i < recalls else 0,
            "mrr": 1.0 if i < hits else 0.5,
            "precision_at_5": 0.4,
        }
        for i in range(n)
    ]
    rows.append({"negative": True, "top1_distance": 0.61, "question": "off-corpus"})
    return rows


def _write(path: Path, rows: list[dict], encoding: str = "utf-8") -> Path:
    path.write_bytes(json.dumps(rows, ensure_ascii=False).encode(encoding))
    return path


def test_aggregate_ignores_negative_rows():
    agg = eval_gate.aggregate(_rows(hit=0.8, recall=0.9, n=10))
    assert agg["n"] == 10  # the negative row did not count
    assert agg["hit_at_1"] == 0.8
    assert agg["recall_at_10"] == 0.9


def test_pass_when_within_threshold(tmp_path):
    base = _write(tmp_path / "base.json", _rows(hit=0.8, recall=0.9))
    fresh = _write(tmp_path / "fresh.json", _rows(hit=0.8, recall=0.9))
    rc = eval_gate.main([
        "--baseline", str(base), "--fresh", str(fresh),
        "--summary-out", str(tmp_path / "s.md"),
    ])
    assert rc == 0


def test_fail_on_hit1_regression_beyond_threshold(tmp_path):
    base = _write(tmp_path / "base.json", _rows(hit=0.9, recall=0.9, n=20))
    fresh = _write(tmp_path / "fresh.json", _rows(hit=0.8, recall=0.9, n=20))  # -10 pts
    summary = tmp_path / "s.md"
    rc = eval_gate.main([
        "--baseline", str(base), "--fresh", str(fresh),
        "--threshold-points", "2.0", "--summary-out", str(summary),
    ])
    assert rc == 1
    text = summary.read_text(encoding="utf-8")
    assert "REGRESSION" in text
    assert "force_deploy" in text  # tells the operator how to proceed anyway


def test_small_dip_within_threshold_passes(tmp_path):
    base = _write(tmp_path / "base.json", _rows(hit=0.90, recall=0.90, n=100))
    fresh = _write(tmp_path / "fresh.json", _rows(hit=0.89, recall=0.90, n=100))  # -1 pt
    rc = eval_gate.main([
        "--baseline", str(base), "--fresh", str(fresh),
        "--threshold-points", "2.0", "--summary-out", str(tmp_path / "s.md"),
    ])
    assert rc == 0


def test_missing_baseline_records_and_passes(tmp_path):
    fresh = _write(tmp_path / "fresh.json", _rows(hit=0.8, recall=0.9))
    summary = tmp_path / "s.md"
    rc = eval_gate.main([
        "--baseline", str(tmp_path / "absent.json"), "--fresh", str(fresh),
        "--summary-out", str(summary),
    ])
    assert rc == 0
    assert "No baseline" in summary.read_text(encoding="utf-8")


def test_cp1252_legacy_baseline_is_readable(tmp_path):
    """Baselines written before eval_golden pinned encoding='utf-8' are cp1252
    (e.g. a · in an entity name) — the gate must still parse them."""
    rows = _rows(hit=0.8, recall=0.9)
    rows[0]["entity"] = "Bigby·s Hand"  # · encodes to 0xb7 in cp1252, invalid UTF-8
    base = _write(tmp_path / "base.json", rows, encoding="cp1252")
    fresh = _write(tmp_path / "fresh.json", _rows(hit=0.8, recall=0.9))
    rc = eval_gate.main([
        "--baseline", str(base), "--fresh", str(fresh),
        "--summary-out", str(tmp_path / "s.md"),
    ])
    assert rc == 0


def test_unusable_fresh_results_exit_2(tmp_path):
    fresh = tmp_path / "fresh.json"
    fresh.write_text("[]", encoding="utf-8")  # parses, but no positive rows
    rc = eval_gate.main([
        "--baseline", str(tmp_path / "absent.json"), "--fresh", str(fresh),
        "--summary-out", str(tmp_path / "s.md"),
    ])
    assert rc == 2

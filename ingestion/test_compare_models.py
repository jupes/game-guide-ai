"""
Unit tests for the model-comparison PURE core (ziw.4 / Phase 3, CP1).

Scorecard diff + CI gate + model-registry shape — no DB, no LLM, no Ollama, no
network. The generator wiring (CP2) and the end-to-end 2-model run (CP3) are
separate. Aggregates use the shape produced by eval_answers.aggregate_metric:
{metric: {"pass_rate": float|None, ...}}.

Run from repo root:
    uv run --with '.[test]' python -m pytest ingestion/test_compare_models.py -q
"""

from __future__ import annotations

from ingestion.compare_models import gate, known_models, scorecard


def _agg(**rates):
    return {m: {"pass_rate": r} for m, r in rates.items()}


# --- scorecard ----------------------------------------------------------------

def test_scorecard_computes_per_metric_delta():
    base = _agg(faithfulness=0.8, answer_correctness=0.5)
    cand = _agg(faithfulness=0.6, answer_correctness=0.7)
    rows = {r["metric"]: r for r in scorecard(base, cand)}
    assert rows["faithfulness"]["baseline"] == 0.8
    assert rows["faithfulness"]["candidate"] == 0.6
    assert abs(rows["faithfulness"]["delta"] - (-0.2)) < 1e-9
    assert abs(rows["answer_correctness"]["delta"] - 0.2) < 1e-9


def test_scorecard_handles_metric_missing_in_one_side():
    rows = {r["metric"]: r for r in scorecard(_agg(faithfulness=0.8), _agg(context_recall=0.5))}
    assert rows["faithfulness"]["candidate"] is None
    assert rows["faithfulness"]["delta"] is None
    assert rows["context_recall"]["baseline"] is None


def test_scorecard_none_pass_rate_gives_none_delta():
    rows = {r["metric"]: r for r in scorecard(_agg(faithfulness=None), _agg(faithfulness=0.9))}
    assert rows["faithfulness"]["delta"] is None


# --- gate ---------------------------------------------------------------------

def test_gate_ok_when_candidate_better():
    ok, _ = gate(_agg(faithfulness=0.7), _agg(faithfulness=0.9), metric="faithfulness", threshold=0.05)
    assert ok is True


def test_gate_ok_within_threshold():
    ok, _ = gate(_agg(faithfulness=0.80), _agg(faithfulness=0.78), metric="faithfulness", threshold=0.05)
    assert ok is True   # 0.02 drop <= 0.05


def test_gate_fails_on_regression_beyond_threshold():
    ok, detail = gate(_agg(faithfulness=0.80), _agg(faithfulness=0.60), metric="faithfulness", threshold=0.05)
    assert ok is False
    assert abs(detail["drop"] - 0.20) < 1e-9


def test_gate_unscored_does_not_fail():
    ok, detail = gate(_agg(faithfulness=None), _agg(faithfulness=0.9), metric="faithfulness", threshold=0.05)
    assert ok is True
    assert detail["reason"] == "unscored"


# --- model registry -----------------------------------------------------------

def test_known_models_includes_the_first_ab_pair():
    labels = known_models()
    assert "gpt-4o-mini" in labels
    assert "gemma4:12b" in labels

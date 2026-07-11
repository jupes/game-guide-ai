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

import pytest

from ingestion.compare_models import build_generator, compare, gate, known_models, scorecard
from ingestion.eval_answers import AnswerCase


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


def test_build_generator_constructs_and_resolves_by_convention(monkeypatch):
    # Needs the [eval] extra (langchain-ollama); skips in a clean [test] env.
    # Construction only — no Ollama server / no .invoke.
    pytest.importorskip("langchain_ollama")
    pytest.importorskip("langchain_openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from langchain_ollama import ChatOllama
    from langchain_openai import ChatOpenAI
    # featured registry labels
    assert isinstance(build_generator("gpt-4o-mini"), ChatOpenAI)
    gemma = build_generator("gemma4:12b")
    assert isinstance(gemma, ChatOllama) and gemma.model == "gemma4:12b"
    # arbitrary labels resolve by convention (":" -> Ollama, else -> OpenAI)
    assert isinstance(build_generator("llama3.2:latest"), ChatOllama)
    assert isinstance(build_generator("gpt-4.1-nano"), ChatOpenAI)


# --- compare() orchestration (offline: fake services + fake evaluator) --------

class _FakeSource:
    def __init__(self, snippet): self.snippet = snippet


class _FakeResp:
    def __init__(self, answer, snippets, answerable=True):
        self.answer = answer
        self.answerable = answerable
        self.sources = [_FakeSource(s) for s in snippets]
        self.contexts = list(snippets)  # full texts == snippets in these fakes


class _FakeSvc:
    def __init__(self, resp): self._resp = resp
    def answer_with_contexts(self, question, mode="sage", conversation_id=None):
        return self._resp, self._resp.contexts


class _FakeEvaluator:
    def __init__(self, per_row): self._per_row = per_row
    def score(self, rows): return [self._per_row for _ in rows]


def test_compare_runs_each_model_and_returns_aggregates():
    case = AnswerCase("What is a Beholder?", ("aberration", "eyestalks"))
    services = {
        "gpt-4o-mini": _FakeSvc(_FakeResp("A beholder is an aberration with eyestalks [1].", ["c1", "c2"])),
        "gemma4:12b": _FakeSvc(_FakeResp("A beholder floats around [1].", ["c1"])),
    }
    ev = _FakeEvaluator({"faithfulness": 0.9, "answer_correctness": 0.7})
    out = compare(services, [case], evaluator=ev)   # langfuse off

    assert set(out) == {"gpt-4o-mini", "gemma4:12b"}
    assert out["gpt-4o-mini"]["aggregates"]["faithfulness"]["passed"] == 1
    # scorecard + gate compose over the two models' aggregates
    rows = {r["metric"]: r for r in scorecard(out["gpt-4o-mini"]["aggregates"], out["gemma4:12b"]["aggregates"])}
    assert "faithfulness" in rows
    ok, _ = gate(out["gpt-4o-mini"]["aggregates"], out["gemma4:12b"]["aggregates"],
                 metric="faithfulness", threshold=0.05)
    assert ok is True

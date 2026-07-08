"""
Unit tests for the answer-quality eval's PURE core (ziw.3 / Phase 2, CP1).

These cover the deterministic graders + aggregation math only — no DB, no LLM, no
network. The Ragas layer (CP2) and the end-to-end runner (CP3) are separate.

Run from repo root:
    uv run --with '.[test]' python -m pytest ingestion/test_eval_answers.py -q
"""

from __future__ import annotations

from ingestion.eval_answers import (
    AnswerCase,
    aggregate_metric,
    build_row,
    citation_ok,
    has_all_key_facts,
    is_refusal,
    key_fact_hits,
    normalize_metric,
    pass_at_k,
    pass_hat_k,
    run_eval,
    score_rows,
    verdict,
)
from service.rag import REFUSAL


# --- AnswerCase ---------------------------------------------------------------

def test_answer_case_defaults_to_sage():
    c = AnswerCase(question="What is a Beholder?", key_facts=("aberration", "eyestalks"))
    assert c.mode == "sage"
    assert c.key_facts == ("aberration", "eyestalks")


# --- is_refusal ---------------------------------------------------------------

def test_is_refusal_matches_exact_refusal():
    assert is_refusal(REFUSAL) is True
    assert is_refusal("  " + REFUSAL + "  ") is True  # tolerant of surrounding whitespace


def test_is_refusal_false_for_real_answer():
    assert is_refusal("A beholder is an aberration [1].") is False


# --- key_fact_hits / has_all_key_facts ---------------------------------------

def test_key_fact_hits_all_present_case_insensitive():
    ans = "The Beholder is a floating ABERRATION with ten eyestalks."
    hits, total = key_fact_hits(ans, ("aberration", "eyestalks"))
    assert (hits, total) == (2, 2)
    assert has_all_key_facts(ans, ("aberration", "eyestalks")) is True


def test_key_fact_hits_partial():
    ans = "A beholder is an aberration."
    hits, total = key_fact_hits(ans, ("aberration", "eyestalks"))
    assert (hits, total) == (1, 2)
    assert has_all_key_facts(ans, ("aberration", "eyestalks")) is False


def test_has_all_key_facts_false_on_empty_facts():
    # No key-facts declared → cannot claim "all present" (avoids vacuous pass).
    assert has_all_key_facts("anything", ()) is False


# --- citation_ok --------------------------------------------------------------

def test_citation_ok_valid_in_range():
    assert citation_ok("Beholders float [1] and petrify [2].", n_sources=2) is True


def test_citation_ok_requires_at_least_one_citation():
    assert citation_ok("Beholders float, no citation here.", n_sources=2) is False


def test_citation_ok_rejects_out_of_range_citation():
    # Cites [3] but only 2 sources returned → hallucinated citation.
    assert citation_ok("Beholders float [3].", n_sources=2) is False


def test_citation_ok_false_when_no_sources():
    assert citation_ok("Beholders float [1].", n_sources=0) is False


# --- pass@k / pass^k ----------------------------------------------------------

def test_pass_at_k_any_true():
    assert pass_at_k([False, True, False]) is True
    assert pass_at_k([False, False]) is False


def test_pass_hat_k_all_true():
    assert pass_hat_k([True, True, True]) is True
    assert pass_hat_k([True, False, True]) is False


def test_pass_hat_k_false_on_empty():
    assert pass_hat_k([]) is False


# --- CP2: Ragas layer (build_row / normalize / verdict / score_rows / aggregate) ---

def test_build_row_shape_and_ground_truth():
    case = AnswerCase("What is a Beholder?", ("aberration", "eyestalks"))
    row = build_row(case, answer="A beholder is an aberration [1].",
                    contexts=["Beholders are aberrations.", "Ten eyestalks."])
    assert row["question"] == "What is a Beholder?"
    assert row["answer"] == "A beholder is an aberration [1]."
    assert row["contexts"] == ["Beholders are aberrations.", "Ten eyestalks."]
    # key-facts joined form the reference/ground-truth for answer-correctness
    assert row["ground_truth"] == "aberration eyestalks"


def test_normalize_metric_nan_and_none_become_unknown():
    nan = float("nan")
    assert normalize_metric(nan) is None
    assert normalize_metric(None) is None
    assert normalize_metric(0.83) == 0.83


def test_verdict_pass_fail_unknown():
    assert verdict(0.8, threshold=0.5) == "pass"
    assert verdict(0.3, threshold=0.5) == "fail"
    assert verdict(None, threshold=0.5) == "unknown"   # Unknown escape hatch


class _FakeEvaluator:
    """Injectable stand-in for the real Ragas evaluator: returns canned per-row
    metric dicts (incl. a NaN to exercise the Unknown path)."""
    def __init__(self, rows_scores):
        self._scores = rows_scores

    def score(self, rows):
        assert len(rows) == len(self._scores)
        return self._scores


def test_score_rows_normalizes_unknowns():
    rows = [{"question": "q", "answer": "a", "contexts": [], "ground_truth": "g"}]
    fake = _FakeEvaluator([{"faithfulness": 0.9, "answer_correctness": float("nan")}])
    out = score_rows(rows, evaluator=fake)
    assert out[0]["faithfulness"] == 0.9
    assert out[0]["answer_correctness"] is None   # NaN -> Unknown


def test_aggregate_metric_excludes_unknown_from_pass_rate():
    # 3 cases for 'faithfulness': pass, fail, unknown
    scored = [{"faithfulness": 0.9}, {"faithfulness": 0.3}, {"faithfulness": None}]
    agg = aggregate_metric(scored, "faithfulness", threshold=0.5)
    assert agg["passed"] == 1
    assert agg["failed"] == 1
    assert agg["unknown"] == 1
    assert agg["pass_rate"] == 0.5   # 1 / (1 pass + 1 fail); unknown excluded


def test_aggregate_metric_all_unknown_has_no_rate():
    scored = [{"faithfulness": None}, {"faithfulness": None}]
    agg = aggregate_metric(scored, "faithfulness", threshold=0.5)
    assert agg["unknown"] == 2
    assert agg["pass_rate"] is None   # nothing scored -> no rate


# --- CP3: runner orchestration (fake svc + fake evaluator, langfuse off) ------

class _FakeSource:
    def __init__(self, snippet):
        self.snippet = snippet


class _FakeResp:
    def __init__(self, answer, snippets, answerable=True):
        self.answer = answer
        self.answerable = answerable
        self.sources = [_FakeSource(s) for s in snippets]


class _FakeSvc:
    def __init__(self, resp_by_q):
        self._m = resp_by_q
        self.calls = []

    def answer(self, question, mode="sage", conversation_id=None):
        self.calls.append((question, mode))
        return self._m[question]


def test_run_eval_orchestrates_positive_case():
    case = AnswerCase("What is a Beholder?", ("aberration", "eyestalks"))
    svc = _FakeSvc({
        "What is a Beholder?": _FakeResp(
            "A beholder is an aberration with ten eyestalks [1].", ["ctx a", "ctx b"]),
    })
    ev = _FakeEvaluator([{"faithfulness": 0.9, "answer_correctness": 0.8}])
    out = run_eval([case], svc, evaluator=ev)   # langfuse off by default

    assert svc.calls == [("What is a Beholder?", "sage")]
    c0 = out["cases"][0]
    assert c0["refused"] is False
    assert c0["key_fact_hits"] == (2, 2)
    assert c0["citation_ok"] is True
    assert c0["trace_id"] is None                    # no langfuse -> no trace id
    assert c0["ragas"]["faithfulness"] == 0.9
    # aggregates roll up the ragas metrics
    assert out["aggregates"]["faithfulness"]["passed"] == 1


def test_run_eval_flags_missing_key_facts_and_bad_citation():
    case = AnswerCase("What is a Meazel?", ("teleport", "shadow"))
    svc = _FakeSvc({
        "What is a Meazel?": _FakeResp("A meazel lurks in shadow [4].", ["ctx"]),  # only 1 source, cites [4]
    })
    ev = _FakeEvaluator([{"faithfulness": 0.4}])
    out = run_eval([case], svc, evaluator=ev)
    c0 = out["cases"][0]
    assert c0["key_fact_hits"] == (1, 2)   # "shadow" present, "teleport" missing
    assert c0["citation_ok"] is False      # [4] out of range for 1 source

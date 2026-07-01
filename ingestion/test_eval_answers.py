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
    citation_ok,
    has_all_key_facts,
    is_refusal,
    key_fact_hits,
    pass_at_k,
    pass_hat_k,
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

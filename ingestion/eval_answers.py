"""
Answer-quality eval for rag-chat (ziw.3 / Phase 2).

Runs the golden *positives* end-to-end through the LangGraph `RagService.answer`
path and scores **generation** quality — deterministic graders first, Ragas
LLM-judge where needed — attaching scores to the Langfuse trace and recording
token cost. Sibling to `eval_golden.py` (retrieval-only); that file is untouched.

Design: see `plans/drafts/rag-chat-answer-quality-eval.md` and the eval-stack ADR
`docs/observability/eval-strategy.md` (Langfuse + Ragas; key-facts reference data).

This module is built in checkpoints:
  - CP1 (here): pure graders + key-facts data model + pass@k (unit-tested, offline).
  - CP2: Ragas scoring layer behind an injectable evaluator.
  - CP3: the end-to-end runner + Langfuse scoring.

The graders **grade the output, not the path** (robust to pipeline refactors).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from service.rag import REFUSAL


@dataclass(frozen=True)
class AnswerCase:
    """One answer-quality case: a question + the key-facts a correct answer must
    contain (the reference for correctness), plus the chat mode to run it in."""
    question: str
    key_facts: tuple[str, ...] = field(default_factory=tuple)
    mode: str = "sage"


# --- Deterministic graders (no LLM) ------------------------------------------

def is_refusal(answer: str) -> bool:
    """True iff the answer is exactly the grounded-refusal string (whitespace-tolerant)."""
    return answer.strip() == REFUSAL


def key_fact_hits(answer: str, key_facts: Sequence[str]) -> tuple[int, int]:
    """(hits, total) — how many key-facts appear in the answer (case-insensitive substring)."""
    low = answer.lower()
    hits = sum(1 for fact in key_facts if fact.lower() in low)
    return hits, len(key_facts)


def has_all_key_facts(answer: str, key_facts: Sequence[str]) -> bool:
    """True iff every declared key-fact is present. Empty key-facts → False (no vacuous pass)."""
    hits, total = key_fact_hits(answer, key_facts)
    return total > 0 and hits == total


def citation_ok(answer: str, n_sources: int) -> bool:
    """True iff the answer has ≥1 inline `[k]` citation and every citation resolves to a
    returned source (1..n_sources). Catches both missing and hallucinated citations."""
    cites = [int(m) for m in re.findall(r"\[(\d+)\]", answer)]
    return bool(cites) and all(1 <= c <= n_sources for c in cites)


# --- Non-determinism aggregation ---------------------------------------------

def pass_at_k(results: Sequence[bool]) -> bool:
    """pass@k: at least one of k trials passed."""
    return any(results)


def pass_hat_k(results: Sequence[bool]) -> bool:
    """pass^k: all k trials passed (empty → False)."""
    return bool(results) and all(results)


# --- Ragas layer (injectable evaluator; "Unknown" escape hatch) ---------------
# We define our OWN canonical row shape and score dict so the eval is decoupled
# from Ragas's evolving schema; the real evaluator (CP3) adapts our rows to the
# installed Ragas API. Tests inject a fake evaluator — no LLM/network here.

def build_row(case: AnswerCase, answer: str, contexts: Sequence[str]) -> dict:
    """Canonical eval row: the key-facts join as the reference/ground-truth used
    for answer-correctness; contexts are the retrieved chunk texts the LLM saw."""
    return {
        "question": case.question,
        "answer": answer,
        "contexts": list(contexts),
        "ground_truth": " ".join(case.key_facts),
    }


def normalize_metric(value: float | None) -> float | None:
    """Map an unusable judge score to None ("Unknown"): None stays None, NaN → None,
    a real number passes through. Keeps a judge that can't decide from faking a fail."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return float(value)


def verdict(value: float | None, threshold: float = 0.5) -> str:
    """"pass" / "fail" / "unknown" for a normalized metric value."""
    if value is None:
        return "unknown"
    return "pass" if value >= threshold else "fail"


class Evaluator(Protocol):
    """Scores eval rows. `score(rows)` returns one metric dict per row (values may be
    None/NaN when the judge is unsure). The real impl wraps Ragas; tests inject a fake."""
    def score(self, rows: list[dict]) -> list[dict]: ...  # pragma: no cover - structural type


def score_rows(rows: list[dict], *, evaluator: Evaluator) -> list[dict]:
    """Run the injected evaluator over rows and normalize each metric (NaN/None →
    Unknown). Returns one normalized metric dict per row (aligned with `rows`)."""
    raw = evaluator.score(rows)
    return [{metric: normalize_metric(v) for metric, v in row_scores.items()} for row_scores in raw]


def aggregate_metric(scored: Sequence[dict], metric: str, threshold: float = 0.5) -> dict:
    """Aggregate one metric across cases. Unknown is excluded from `pass_rate`
    (rate = passed / (passed+failed)); `pass_rate` is None when nothing was scored."""
    passed = failed = unknown = 0
    for row in scored:
        v = verdict(row.get(metric), threshold)
        if v == "pass":
            passed += 1
        elif v == "fail":
            failed += 1
        else:
            unknown += 1
    n_scored = passed + failed
    return {
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "n_scored": n_scored,
        "pass_rate": (passed / n_scored) if n_scored else None,
    }


# --- Curated key-facts subset (seed; expand toward ~20-30 per the roadmap) ----
# Facts are high-level and conservative on purpose; REVIEW/EXPAND before treating
# the scores as authoritative (Anthropic step 2: two experts should agree pass/fail).
# Questions mirror positives in ingestion/eval_golden.py::CURATED.
CURATED_ANSWERS: tuple[AnswerCase, ...] = (
    AnswerCase("What does the Invisibility spell do?",
               ("invisible", "concentration")),
    AnswerCase("What does the Shield spell do?",
               ("reaction", "AC")),
    AnswerCase("What is a Froghemoth?",
               ("aberration", "swamp")),
    AnswerCase("What is a Beholder Zombie?",
               ("undead", "eyestalks")),
    AnswerCase("What is a Shield Guardian?",
               ("construct",)),
    AnswerCase("How strong does a Potion of Giant Strength make you?",
               ("Strength",)),
)

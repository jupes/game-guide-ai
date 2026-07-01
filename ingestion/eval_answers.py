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

import re
from dataclasses import dataclass, field
from typing import Sequence

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

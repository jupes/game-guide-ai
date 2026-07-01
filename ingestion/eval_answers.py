"""
Answer-quality eval for rag-chat (ziw.3 / Phase 2).

Runs the golden *positives* end-to-end through the LangGraph `RagService.answer`
path and scores **generation** quality — deterministic graders first, Ragas
LLM-judge where needed — attaching scores to the Langfuse trace and recording
token cost. Sibling to `eval_golden.py` (retrieval-only); that file is untouched.

Run (see docs/observability/answer-eval.md for detail):
    docker compose up -d vector-db
    uv run --with '.[eval]' python ingestion/eval_answers.py --limit 5

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


# --- Runner (CP3): orchestrate answer -> grade -> Ragas -> Langfuse scores -----

def _answer_with_trace(svc, case: AnswerCase, langfuse):
    """Run one case through the graph. When langfuse is provided, wrap it in a span
    we own so we can capture the trace_id (the graph's callback nests under it via
    contextvars) to attach scores to. Returns (ChatResponse, trace_id|None)."""
    if langfuse is None:
        return svc.answer(case.question, mode=case.mode), None
    with langfuse.start_as_current_span(name=f"eval:{case.question[:60]}") as span:
        resp = svc.answer(case.question, mode=case.mode)
        trace_id = getattr(span, "trace_id", None)
    return resp, trace_id


def _push_scores(langfuse, trace_id: str, scored: dict) -> None:
    """Attach normalized Ragas scores to a trace (skips Unknown/None). Best-effort:
    a scoring hiccup must not fail the eval run."""
    for name, value in scored.items():
        if value is None:
            continue
        try:
            langfuse.create_score(trace_id=trace_id, name=f"ragas_{name}", value=float(value))
        except Exception:  # pragma: no cover - network/SDK edge
            pass


def run_eval(cases: Sequence[AnswerCase], svc, *, evaluator: Evaluator,
             langfuse=None, threshold: float = 0.5) -> dict:
    """Run positive answer-quality cases through the graph and score them.

    Injectable `svc` (has `.answer`) and `evaluator` (has `.score`) keep this
    offline-testable; `langfuse` is optional (attach scores when present). Returns
    {cases: [...per-case...], aggregates: {metric: aggregate_metric(...)}}.
    """
    per_case: list[dict] = []
    rows: list[dict] = []
    for case in cases:
        resp, trace_id = _answer_with_trace(svc, case, langfuse)
        contexts = [s.snippet for s in resp.sources]
        rows.append(build_row(case, resp.answer, contexts))
        per_case.append({
            "question": case.question,
            "answer": resp.answer,
            "answerable": bool(getattr(resp, "answerable", False)),
            "refused": is_refusal(resp.answer),
            "key_fact_hits": key_fact_hits(resp.answer, case.key_facts),
            "citation_ok": citation_ok(resp.answer, len(resp.sources)),
            "trace_id": trace_id,
        })

    scored = score_rows(rows, evaluator=evaluator) if rows else []
    for pc, sc in zip(per_case, scored):
        pc["ragas"] = sc
        if langfuse is not None and pc["trace_id"]:
            _push_scores(langfuse, pc["trace_id"], sc)

    metric_names = sorted({m for s in scored for m in s})
    aggregates = {m: aggregate_metric(scored, m, threshold) for m in metric_names}
    return {"cases": per_case, "aggregates": aggregates}


# --- Real Ragas evaluator (lazy import; only used in live runs) ----------------

class RagasEvaluator:
    """Adapts our canonical rows to the installed Ragas API (0.4.x) and returns
    per-row metric dicts under our stable keys. Ragas is imported lazily so unit
    tests (which inject a fake evaluator) never load the heavy path. Records judge
    token usage/cost on `last_total_tokens` / `last_total_cost`."""

    def __init__(self, model: str | None = None, embed_model: str | None = None):
        from config import DEFAULT_MODEL
        from ingestion.retrieval import EMBED_MODEL
        self.model = model or DEFAULT_MODEL
        self.embed_model = embed_model or EMBED_MODEL
        self.last_total_tokens = None
        self.last_total_cost = None

    def _metrics(self):
        # Classic pre-bound metric singletons: evaluate() binds them with the llm/
        # embeddings we pass. (collections/* is the future API but its classes need
        # llm at construction and have diverged names in 0.4.x — these are stable.)
        from ragas.metrics import (
            answer_correctness, answer_relevancy, context_precision,
            context_recall, faithfulness,
        )
        return [faithfulness, answer_relevancy, answer_correctness,
                context_precision, context_recall]

    def score(self, rows: list[dict]) -> list[dict]:
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas import EvaluationDataset, SingleTurnSample, evaluate
        from ragas.cost import get_token_usage_for_openai
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper

        llm = LangchainLLMWrapper(ChatOpenAI(model=self.model, temperature=0))
        emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(model=self.embed_model))
        metrics = self._metrics()
        samples = [
            SingleTurnSample(
                user_input=r["question"], response=r["answer"],
                retrieved_contexts=r["contexts"] or [""], reference=r["ground_truth"],
            )
            for r in rows
        ]
        result = evaluate(
            EvaluationDataset(samples=samples), metrics=metrics,
            llm=llm, embeddings=emb, token_usage_parser=get_token_usage_for_openai,
            show_progress=False, raise_exceptions=False,
        )
        try:  # pragma: no cover - live-only
            self.last_total_tokens = result.total_tokens()
        except Exception:
            pass
        df = result.to_pandas()
        return [
            {m.name: (row[m.name] if m.name in row else None) for m in metrics}
            for _, row in df.iterrows()
        ]


def _load_dotenv() -> None:  # pragma: no cover - convenience I/O
    """Best-effort load of repo-root .env (OPENAI_API_KEY + LANGFUSE_*); existing env wins."""
    from pathlib import Path
    import os
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = s.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:  # pragma: no cover - integration entry (needs DB + LLM)
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="rag-chat answer-quality eval (Ragas over the golden subset)")
    parser.add_argument("--limit", type=int, default=None, help="only the first N cases (PR subset)")
    parser.add_argument("--no-langfuse", action="store_true", help="don't attach scores to Langfuse")
    args = parser.parse_args()

    _load_dotenv()
    from service.rag import RagService

    cases = CURATED_ANSWERS[: args.limit] if args.limit else CURATED_ANSWERS
    langfuse = None
    if not args.no_langfuse:
        try:
            from langfuse import get_client
            langfuse = get_client()
        except Exception:
            langfuse = None

    svc = RagService()
    evaluator = RagasEvaluator()
    result = run_eval(list(cases), svc, evaluator=evaluator, langfuse=langfuse)
    result["judge_total_tokens"] = getattr(evaluator, "last_total_tokens", None)

    out_path = Path(__file__).parent / "eval_answers_results.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))

    print("=" * 72)
    print(f"Answer-quality eval -- {len(cases)} case(s)")
    for m, agg in result["aggregates"].items():
        rate = f"{agg['pass_rate']:.0%}" if agg["pass_rate"] is not None else "n/a"
        print(f"  {m:22s} pass {agg['passed']}/{agg['n_scored']} ({rate})  unknown={agg['unknown']}")
    print(f"  judge tokens: {result['judge_total_tokens']}")
    print(f"Results -> {out_path}")
    if langfuse is not None:
        try:
            langfuse.flush()
        except Exception:
            pass


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


if __name__ == "__main__":  # pragma: no cover
    main()

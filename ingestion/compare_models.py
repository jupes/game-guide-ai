"""
Version/model comparison + CI regression gate for rag-chat (ziw.4 / Phase 3).

Runs the answer-quality eval (Phase 2's `run_eval` + the FIXED gpt-4o-mini judge)
once per generator model, holding everything else constant (same golden dataset,
same graph version, same judge), then diffs the aggregates into a scorecard and
gates CI on a chosen metric. First A/B: gpt-4o-mini (API) vs gemma4:12b (local).

Decision + methodology: `docs/observability/eval-strategy.md`. Only the generator
varies — a valid A/B (the judge is independent and >= the generator; no
self-enhancement bias).

Built in checkpoints:
  - CP1 (here): pure scorecard + CI gate + lazy model registry (offline).
  - CP2: real generator wiring (ChatOllama) via the registry.
  - CP3: end-to-end 2-model run + Langfuse dataset runs.
"""

from __future__ import annotations

from typing import Callable

# --- Model registry (lazy factories; building a generator imports its SDK only
#     when actually used, so importing this module / known_models() stays light) --

def _openai(model: str) -> Callable[[], object]:
    def build() -> object:
        from config import TEMPERATURE
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=TEMPERATURE)
    return build


def _ollama(model: str) -> Callable[[], object]:
    def build() -> object:
        import os
        from config import TEMPERATURE
        from langchain_ollama import ChatOllama  # in the [eval] extra
        return ChatOllama(
            model=model, temperature=TEMPERATURE,
            base_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        )
    return build


# label -> factory building a LangChain chat model for the generate node.
MODEL_REGISTRY: dict[str, Callable[[], object]] = {
    "gpt-4o-mini": _openai("gpt-4o-mini"),
    "gemma4:12b": _ollama("gemma4:12b"),
}


def known_models() -> tuple[str, ...]:
    """Labels the comparison recognizes (does not build anything)."""
    return tuple(MODEL_REGISTRY)


def build_generator(label: str) -> object:
    """Construct the generator chat model for a label (raises KeyError if unknown)."""
    return MODEL_REGISTRY[label]()


# --- Pure comparison logic ----------------------------------------------------
# Aggregates use eval_answers.aggregate_metric shape: {metric: {"pass_rate": float|None, ...}}.

def _rate(agg: dict, metric: str) -> float | None:
    return agg.get(metric, {}).get("pass_rate")


def scorecard(baseline: dict, candidate: dict) -> list[dict]:
    """Per-metric comparison rows: {metric, baseline, candidate, delta} across the union
    of metrics. delta = candidate - baseline (None if either pass_rate is None/missing)."""
    rows: list[dict] = []
    for metric in sorted(set(baseline) | set(candidate)):
        b = _rate(baseline, metric)
        c = _rate(candidate, metric)
        delta = (c - b) if (b is not None and c is not None) else None
        rows.append({"metric": metric, "baseline": b, "candidate": c, "delta": delta})
    return rows


def gate(baseline: dict, candidate: dict, *, metric: str, threshold: float) -> tuple[bool, dict]:
    """CI gate: fail iff the candidate's pass_rate for `metric` drops MORE than `threshold`
    below the baseline. Unscored (either rate None) does NOT fail — you can't judge a
    regression you didn't measure. Returns (ok, detail)."""
    b = _rate(baseline, metric)
    c = _rate(candidate, metric)
    if b is None or c is None:
        return True, {"metric": metric, "baseline": b, "candidate": c, "reason": "unscored"}
    drop = b - c
    ok = drop <= threshold
    return ok, {"metric": metric, "baseline": b, "candidate": c,
                "drop": drop, "threshold": threshold, "ok": ok}

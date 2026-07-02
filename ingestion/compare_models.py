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
    """The featured/default labels (does not build anything). `build_generator` also
    resolves arbitrary labels via the heuristic below, so comparison isn't limited to these."""
    return tuple(MODEL_REGISTRY)


def build_generator(label: str) -> object:
    """Construct the generator chat model for a label. Featured labels come from the
    registry; any other label resolves by convention so any model can be compared:
    an Ollama-style `name:tag` (e.g. `llama3.2:latest`) -> ChatOllama; otherwise
    (e.g. `gpt-4.1-nano`) -> ChatOpenAI."""
    if label in MODEL_REGISTRY:
        return MODEL_REGISTRY[label]()
    return (_ollama(label) if ":" in label else _openai(label))()


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


# --- Orchestration ------------------------------------------------------------

def compare(services: dict, cases, *, evaluator, langfuse=None) -> dict:
    """Run the answer-quality eval once per generator (same cases, same fixed judge).
    `services` maps label -> a RagService built with that generator. Injectable +
    offline-testable (fake services + fake evaluator). Returns {label: run_eval result}."""
    from ingestion.eval_answers import run_eval
    results: dict = {}
    for label, svc in services.items():
        res = run_eval(list(cases), svc, evaluator=evaluator, langfuse=langfuse)
        # The judge is shared, so capture its per-model token usage right after each run
        # (otherwise only the final model's cost survives on the evaluator).
        res["judge_tokens"] = getattr(evaluator, "last_total_tokens", None)
        results[label] = res
    return results


def build_services(labels) -> dict:
    """label -> RagService(model=label, llm_client=<that generator>). `model=label` tags
    the Langfuse traces with the real generator (the injected client is what actually runs)."""
    from service.rag import RagService
    return {label: RagService(model=label, llm_client=build_generator(label)) for label in labels}


def ensure_dataset(langfuse, name: str, cases) -> None:  # pragma: no cover - live-only
    """Seed a Langfuse dataset from the golden cases (idempotent by a question-derived id)."""
    import hashlib
    try:
        langfuse.create_dataset(name=name, description="rag-chat answer-quality golden subset")
    except Exception:
        pass  # already exists
    for c in cases:
        item_id = hashlib.md5(c.question.encode("utf-8")).hexdigest()[:16]
        try:
            langfuse.create_dataset_item(
                dataset_name=name, id=item_id,
                input={"question": c.question, "mode": c.mode},
                expected_output={"key_facts": list(c.key_facts)},
            )
        except Exception:
            pass


def _fmt(x) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else "n/a"


def main() -> None:  # pragma: no cover - integration entry (needs DB + LLM + Ollama)
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="rag-chat model comparison + CI regression gate")
    parser.add_argument("--models", default="gpt-4o-mini,gemma4:12b",
                        help="comma-separated labels; the first is the baseline")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--gate-metric", default="answer_correctness")
    parser.add_argument("--gate-threshold", type=float, default=0.05)
    parser.add_argument("--dataset", default="rag-chat-answers")
    parser.add_argument("--no-langfuse", action="store_true")
    args = parser.parse_args()

    from ingestion.eval_answers import CURATED_ANSWERS, RagasEvaluator, _load_dotenv
    _load_dotenv()

    labels = [m.strip() for m in args.models.split(",") if m.strip()]
    cases = list(CURATED_ANSWERS[: args.limit] if args.limit else CURATED_ANSWERS)

    langfuse = None
    if not args.no_langfuse:
        try:
            from langfuse import get_client
            langfuse = get_client()
            ensure_dataset(langfuse, args.dataset, cases)
        except Exception:
            langfuse = None

    evaluator = RagasEvaluator()  # FIXED, independent judge across all models
    results = compare(build_services(labels), cases, evaluator=evaluator, langfuse=langfuse)

    baseline = labels[0]
    base_agg = results[baseline]["aggregates"]
    out = {"baseline": baseline, "models": {}, "gate": {},
           "judge_tokens_per_model": {lbl: results[lbl].get("judge_tokens") for lbl in labels}}
    print("=" * 72)
    print(f"Model comparison -- baseline: {baseline} ({len(cases)} case(s))")
    overall_ok = True
    for label in labels:
        out["models"][label] = results[label]["aggregates"]
        if label == baseline:
            continue
        print(f"\n-- {label} vs {baseline} --")
        for row in scorecard(base_agg, results[label]["aggregates"]):
            print(f"  {row['metric']:22s} base {_fmt(row['baseline'])}  "
                  f"cand {_fmt(row['candidate'])}  delta {_fmt(row['delta'])}")
        ok, detail = gate(base_agg, results[label]["aggregates"],
                          metric=args.gate_metric, threshold=args.gate_threshold)
        out["gate"][label] = detail
        print(f"  GATE [{args.gate_metric}]: {'PASS' if ok else 'FAIL'}")
        overall_ok = overall_ok and ok

    Path(__file__).parent.joinpath("compare_results.json").write_text(
        json.dumps(out, indent=2, default=str, ensure_ascii=False))
    print(f"\n  judge tokens/model: {out['judge_tokens_per_model']}")
    print("Results -> ingestion/compare_results.json")
    if langfuse is not None:
        try:
            langfuse.flush()
        except Exception:
            pass
    if not overall_ok:
        raise SystemExit(1)  # CI regression gate failed


if __name__ == "__main__":  # pragma: no cover
    main()

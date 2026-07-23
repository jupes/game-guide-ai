# Quality/cost dashboard (Phase 4 / ziw.5)

Runtime service/UI names, labels, privacy rules, and storage semantics are defined in
[`metrics-standard.md`](metrics-standard.md). Dashboard widgets and scriptable summaries must use
that catalog unchanged.

Surfaces the quality + cost/latency trends captured in Phases 1-3 (traces tagged
`model`/`service_version`/`mode`, latency/tokens/cost, `ragas_*` scores, the comparison
dataset) — filterable by model, for A/B decisions. Two ways to read it: the **Langfuse
dashboard** (trends over time, in the UI) and a **scriptable summary** (`ingestion/metrics_summary.py`,
reproducible / CI-able).

## Scriptable summary (`metrics_summary.py`)

```bash
# live: cost / p95 latency / tokens by model, from Langfuse (Metrics API)
python ingestion/metrics_summary.py --since 30d

# offline: quality by model from a comparison run (no Langfuse)
python ingestion/metrics_summary.py --from-results ingestion/compare_results.json
```

Example (live): `gpt-4o-mini` cost $0.0011 / p95 1963ms vs `llama3.2:latest` free / p95 28903ms — the
fast-and-cheap-API vs free-but-slow-local tradeoff at a glance. Writes `ingestion/metrics_summary.json`.

## Langfuse dashboard (trends, in the UI)

Langfuse dashboards are built in the UI (there's no dashboard-creation API). Create a dashboard
(or clone the curated **Cost / Latency / Usage** ones) and add widgets. **What actually works as a
grouping dimension matters** (discovered live):

| Widget | View | Measure / agg | Group by | Notes |
|---|---|---|---|---|
| Cost by model | observations | `totalCost` / sum | **`providedModelName`** | our best "by model" axis |
| p95 latency by model | observations | `latency` / p95 | `providedModelName` | local models show high latency |
| Token usage by model | observations | `totalTokens` / sum | `providedModelName` | |
| Request volume | traces | `count` / count | `tags` or time | `tags` = `mode:*`, `rag-chat` |
| Answer quality | scores | `value` / avg | score `name` | `ragas_faithfulness`, `ragas_answer_correctness`, … |

> **Gotcha (important):** trace **metadata** (`model`, `service_version`, `mode`) is **not** a
> queryable dashboard dimension — Langfuse only exposes `id/name/tags/userId/sessionId/release/
> version/environment` on traces, and `providedModelName` (+ others) on observations. So:
> - **Model** filtering → use `providedModelName` on the **observations** view (works today).
> - **Version** filtering → set the trace's **native `version`** field to the git SHA (a small
>   follow-up on `service/tracing.py`; currently it's only in metadata). Until then, filter by
>   `mode`/`model` and use `metrics_summary.py --from-results` for per-version comparison.

## How to read it for an A/B decision

Compare candidate vs baseline (model or version) on:

1. **Quality** — `ragas_faithfulness` + `ragas_answer_correctness` (from the scores widget or
   `--from-results`). Is the candidate as grounded/correct?
2. **Cost** — `sum totalCost` per request. Local generators are free; API models are not.
3. **Latency** — `p95 latency`. Local models can be much slower (see the 29s example).

**Decision:** the candidate wins if quality is within noise **and** cost/latency are materially
better, **or** quality is clearly higher at acceptable cost/latency. This mirrors the Phase 3 CI gate
(`compare_models.py --gate-metric/--gate-threshold`) — the dashboard shows the trend, the gate enforces
it in CI.

## Cost

Reading the dashboard / running the summary is free (Metrics API reads). The underlying data was
already produced by the eval/comparison runs; no extra spend here.

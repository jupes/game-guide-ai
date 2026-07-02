# Answer-quality eval (Phase 2 / ziw.3)

Scores the **generation** quality of rag-chat's answers — deterministic graders first,
then Ragas LLM-judge metrics — over the golden subset, and attaches the scores to the
Langfuse trace for each request. Sibling to the retrieval-only `ingestion/eval_golden.py`.

Decision + methodology: `docs/observability/eval-strategy.md` (Langfuse + Ragas; Anthropic's
8-step roadmap). Code: `ingestion/eval_answers.py`.

## Run it

```bash
# 1. Bring up the seeded vector DB (retrieval needs it)
docker compose up -d vector-db

# 2. Run the eval. `ragas` is the optional [eval] extra (heavy; not a service dep).
#    Creds (OPENAI_API_KEY + LANGFUSE_*) are read from the repo-root .env.
uv run --with '.[eval]' python ingestion/eval_answers.py --limit 5        # PR-sized subset
uv run --with '.[eval]' python ingestion/eval_answers.py                  # full curated subset
uv run --with '.[eval]' python ingestion/eval_answers.py --no-langfuse    # skip Langfuse scoring
```

Set `RAG_TRACING=1` to also emit the graph's node-level spans nested under each eval trace.

**Outputs:** a console summary (per-metric pass rate + judge token cost), `ingestion/eval_answers_results.json`
(per-case answers, deterministic grades, Ragas scores, trace ids), and — unless `--no-langfuse` —
`ragas_*` scores attached to each request's Langfuse trace.

## Metrics

| Grader | Type | What it checks |
|---|---|---|
| refusal | deterministic | negatives return the exact REFUSAL string |
| key-facts | deterministic | required key-facts present (case-insensitive) |
| citation | deterministic | ≥1 `[n]` citation, all resolving to a returned source |
| faithfulness | Ragas (LLM-judge) | answer claims supported by retrieved context |
| answer_relevancy | Ragas (LLM-judge) | answer addresses the question |
| answer_correctness | Ragas (LLM-judge) | answer vs the key-facts reference |
| context_precision / context_recall | Ragas (LLM-judge) | retrieved context quality vs reference |

Judge = a **fixed, independent** model (gpt-4o-mini), kept separate from the generator so
comparisons stay valid and free of self-enhancement bias (see the eval-strategy ADR). An
"Unknown" judge verdict is excluded from pass-rate, not counted as a fail.

## Known limitations (tracked follow-ups)

- **Seed key-facts are sparse/conservative** — `CURATED_ANSWERS` is a starter set; expand toward
  ~20–30 well-reviewed cases so `answer_correctness` is meaningful (Anthropic step 2).
- **Contexts are source snippets** (240-char truncated), which understates `context_precision`/
  `context_recall`. Capturing the full retrieved chunk text would sharpen those metrics.

## Cost

Langfuse: free tier. Real cost = OpenAI judge tokens over the subset (`gpt-4o-mini`, a few cents
for a small run; the per-run token count is printed and stored). Cap via `--limit` for PR runs;
run the full subset on a nightly cadence.

---

## Model comparison + CI gate (Phase 3 / ziw.4)

`ingestion/compare_models.py` runs the eval above **once per generator model**, holding everything
else constant (same golden subset, same graph version, same **fixed independent judge** — gpt-4o-mini),
then prints a per-metric scorecard and **gates CI** on a chosen metric. Only the generator varies, so
it's a valid A/B. First comparison: **gpt-4o-mini (API) vs gemma4:12b (local)**.

### Run the comparison

```bash
docker compose up -d vector-db            # retrieval
ollama pull gemma4:12b                     # local generator (~8GB, 4-bit; Ollama 0.22+; fits a 3080)

# baseline is the FIRST --models entry; the rest are candidates.
uv run --with '.[eval]' python ingestion/compare_models.py \
  --models gpt-4o-mini,gemma4:12b --limit 5 \
  --gate-metric answer_correctness --gate-threshold 0.05
```

- Any label works: `name:tag` resolves to an Ollama model (e.g. `llama3.2:latest`), otherwise to an
  OpenAI model (e.g. `gpt-4.1-nano`). So you can A/B any pair.
- **Exit code is the CI gate:** non-zero when a candidate's pass-rate for `--gate-metric` drops more
  than `--gate-threshold` below the baseline. Wire it as a PR check (`--limit` subset); full subset
  nightly.
- **Outputs:** scorecard (per-metric base/candidate/delta), `ingestion/compare_results.json`
  (aggregates + gate detail + per-model judge tokens), a seeded Langfuse **dataset**, and — with
  tracing — model-tagged traces + scores.

### Cost & fairness (comparison)

- **Local generation (Gemma) is free** (your GPU); only the fixed judge spends tokens (~2x the eval
  since both models are judged). Cap via `--limit`.
- Judge stays gpt-4o-mini across all models (independent, >= the generator) so scores are comparable
  and free of self-enhancement bias.

### Known limitations / refinements (comparison)

- Langfuse **dataset is seeded** and traces are model-tagged, but full `item.run()` dataset-run
  grouping (the native side-by-side experiment UI) is a follow-up — scorecard + gate + tagged traces
  already deliver the comparison.
- Inherits the eval's data-quality follow-ups (expand key-facts; full-context for context metrics).

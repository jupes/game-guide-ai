# CI / CD pipeline

GitHub Actions, defined in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml),
runs on pull requests targeting `master`, every push to `master` (the default
branch is `master`, not `main`), and manual dispatch. Pull requests run every
quality gate but never deploy.

```text
pull request / push to master
   ├─ python-tests        pytest — service, ingestion, repo guards ─┐
   ├─ ui-tests            typecheck · lint · vitest ───────────────┤
   │                                                              ▼
   │                    ui-e2e — production Compose + perf budgets
   └─ retrieval-metrics   eval_golden vs live corpus DB → regression gate
          │                    (skips loudly until secrets are configured)
          ▼
       deploy             push/manual only; requires every gate to pass
                          (no-op until hosting exists)
```

## Browser release tracer and UI performance gate

`ui-e2e` uses Playwright against the same production Nginx UI image used for
deployment. Its global setup owns a two-service
[`docker-compose.e2e.yml`](../docker-compose.e2e.yml) stack: the production UI
and a deterministic FastAPI adapter. It deliberately needs no database, LLM
key, Langfuse account, or network font service.

The release tracer enters the app, changes channels, creates and sends a
conversation, reloads, reselects the persisted conversation, recalls its
history, and uploads an attachment. It also verifies that UI fonts are
self-hosted.

Performance observers are installed before navigation and enforce the versioned
budgets in [`ui/e2e/performance-budget.json`](../ui/e2e/performance-budget.json):

| Metric | Unit | Budget |
| --- | --- | ---: |
| TTFB | milliseconds | ≤ 1500 |
| FCP | milliseconds | ≤ 2000 |
| LCP | milliseconds | ≤ 2500 |
| CLS | score | ≤ 0.1 |

Every run writes machine-readable `ui/e2e-results/performance.json` and a human
summary at `ui/e2e-results/performance.md`. CI adds the Markdown table to the
run summary and retains the directory as a 30-day artifact even when the gate
fails.

## The regression gate ("flag, then proceed or back out")

`retrieval-metrics` runs `ingestion/eval_golden.py` against the corpus DB and then
`scripts/ci/eval_gate.py`, which compares the run's **Hit@1** and **Recall@10**
against the baseline committed at `ingestion/eval_results.json`. A drop of more
than **2.0 points** in either fails the job, which:

- marks the run red and writes a metric table + delta to the run's summary page
  (**the flag**), and
- blocks the `deploy` job.

Your two options from there:

- **Proceed anyway** — re-run the pipeline with the override:
  `gh workflow run CI -f force_deploy=true` (or Actions → CI → Run workflow →
  check *force_deploy*). Tests still must pass; only the metrics gate is waived.
- **Back out** — `git revert <merge-sha> && git push`. The revert lands on
  `master`, the pipeline runs again and redeploys the previous behavior.

When retrieval genuinely changed for the better (or a corpus re-ingest moved the
numbers), refresh the baseline by committing the new `ingestion/eval_results.json`
produced by a local `eval_golden.py` run — the gate always compares against the
committed file.

## Activation switches

The pipeline ships fully wired but two stages wait on infrastructure. Nothing
pretends to run:

| Stage | Lights up when | Why it waits |
| --- | --- | --- |
| `retrieval-metrics` | Repo **secrets** `EVAL_DATABASE_URL` (a reachable, ingested pgvector DSN) + `OPENAI_API_KEY` | The corpus is deliberately **not in git** (licensing: no verbatim book text in the repo), so CI can't rebuild it — it must query a live DB. Until hosting (epic `17u`) provides one, the job skips with a notice. |
| `deploy` | Repo **variable** `DEPLOY_TARGET` + WIF secrets `GCP_WIF_PROVIDER` / `GCP_DEPLOY_SA` (the job authenticates to GCP via Workload Identity Federation) | Hosting is the GCP pilot (epic `17u`, bead `x5bz.1`). The job no-ops with a notice until `DEPLOY_TARGET` is set; the one-time bootstrap — project, Cloud SQL, corpus, WIF — is the runbook [`docs/deploy-gcp.md`](deploy-gcp.md). `scripts/deploy.sh` encodes the "how", the variable the "where". |

Optional: the `deploy` job is bound to the `production` GitHub **environment** —
adding a required reviewer there (Settings → Environments) turns every deploy
into click-to-approve, independent of the metrics gate.

## Planned next stages (tracked in Beads)

- **Answer-quality eval on a schedule** — `eval_answers.py` / `compare_models.py`
  (Ragas judge, real LLM cost) as a nightly/weekly `schedule:` job rather than
  per merge; its CI gate already exists in `compare_models.py`.

## Local parity

Everything CI runs works locally, same commands:

```bash
uv run --with '.[test]' python -m pytest -q                 # python-tests
cd ui && bun run typecheck && bun run lint && bun run test  # ui-tests
cd ui && bun run test:e2e                                   # production Compose E2E
docker compose -f docker-compose.e2e.yml config             # validate stack wiring
PYTHONUTF8=1 uv run --with "psycopg[binary]" --with openai \
    python ingestion/eval_golden.py                          # metrics (needs DB + key)
uv run python scripts/ci/eval_gate.py \
    --baseline <committed> --fresh ingestion/eval_results.json
```

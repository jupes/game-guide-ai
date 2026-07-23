# CI / CD pipeline

GitHub Actions, defined in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
Runs on every **push to `master`** (which is what a PR merge is — note the default
branch here is `master`, not `main`) and on manual dispatch.

```text
merge to master
   ├─ python-tests        pytest — service, ingestion, repo guards
   ├─ ui-tests            typecheck · lint · vitest (jsdom + storybook browser)
   └─ retrieval-metrics   eval_golden vs live corpus DB → regression gate
          │                    (skips loudly until secrets are configured)
          ▼
       deploy             auto when all green — no-op until hosting exists
```

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
| `deploy` | Repo **variable** `DEPLOY_TARGET` + an executable `scripts/deploy.sh <target> <sha>` | There is no hosting target yet (epic `17u`). The job no-ops with a notice; once you host somewhere, `deploy.sh` encodes the "how" and the variable the "where". |

Optional: the `deploy` job is bound to the `production` GitHub **environment** —
adding a required reviewer there (Settings → Environments) turns every deploy
into click-to-approve, independent of the metrics gate.

## Planned next stages (tracked in Beads)

- **Playwright E2E + UI performance** — real-browser end-to-end tests against the
  compose stack (they do not exist yet; today's browser tests are Storybook
  component tests), capturing **TTFB and Lighthouse-style metrics** (FCP/LCP/CLS)
  per merge with their own regression thresholds. Names, units, labels, and
  privacy constraints come from the
  [service/UI metrics standard](observability/metrics-standard.md). The natural
  seam is a fourth job between the test jobs and `deploy`.
- **Answer-quality eval on a schedule** — `eval_answers.py` / `compare_models.py`
  (Ragas judge, real LLM cost) as a nightly/weekly `schedule:` job rather than
  per merge; its CI gate already exists in `compare_models.py`.

## Local parity

Everything CI runs works locally, same commands:

```bash
uv run --with '.[test]' python -m pytest -q                 # python-tests
cd ui && bun run typecheck && bun run lint && bun run test  # ui-tests
PYTHONUTF8=1 uv run --with "psycopg[binary]" --with openai \
    python ingestion/eval_golden.py                          # metrics (needs DB + key)
uv run python scripts/ci/eval_gate.py \
    --baseline <committed> --fresh ingestion/eval_results.json
```

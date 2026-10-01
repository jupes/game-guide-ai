# CI / CD pipeline

GitHub Actions, defined in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml),
runs on pull requests targeting `master`, every push to `master` (the default
branch is `master`, not `main`), and manual dispatch. Pull requests run every
quality gate but never deploy.

```text
pull request / push to master
   ├─ python-tests        ruff · mypy · pytest (service, ingestion, repo guards) ─┐
   ├─ python-db-tests     pytest against a real PostgreSQL service ──────────────┤
   ├─ ui-tests            typecheck · lint · vitest ───────────────────────────┤
   ├─ contract-parity     Pydantic vs Zod, differential fuzz                   │
   ├─ ui-e2e              production Compose + perf budgets                    │
   │                      (no `needs:` — runs in parallel with the above)      │
   └─ retrieval-metrics   eval_golden vs live corpus DB → regression gate      │
          │                    (skips loudly until secrets are configured)     │
          ▼                                                                    ▼
       deploy             push/manual, master only; requires every job above to pass
                          (skipped until hosting exists — never a green
                           check for having deployed nothing)
```

`python-tests` and `python-db-tests` used to be one job, run one after the other:
the unit/coverage step then the PostgreSQL integration step. They are now two
jobs that start together, because the second needs nothing the first produces
(agent-forge-harness-k768). `python-tests` also caches the downloaded `ffmpeg`
`.deb` with `actions/cache`, keyed on the runner's OS and image — some runners
draw a slow package mirror, and a cache hit skips that download entirely;
`apt-get` still re-fetches the signed package lists and verifies the cached
`.deb` against them, so a poisoned cache entry cannot install an unverified
package. `ui-e2e` carries no `needs:` either: it builds and starts its own
Compose stack rather than consuming an artifact from any other job, so nothing
stops it starting immediately. `deploy` is unaffected by either change — it
still needs every test job's own result, `python-db-tests` included.

## Contract parity

`contract-parity` is the one job that installs both toolchains. It runs
`contracts/workbench/tools/differential_fuzz.py`, which mutates every valid
example of the Workbench wire contract and checks two things: that the Pydantic
models and the Zod schemas give the same verdict, and that the client can read
whatever the server emits. The shared fixtures already run inside `python-tests`
and `ui-tests`; this covers the cases nobody thought to write. It gates
`deploy`: the conversation timeline route serves that contract, so a Pydantic/Zod
disagreement is a screen that breaks after a deploy with nothing red anywhere.
The fuzz is deterministic (fixed fixtures, a fixed replacement list), so the gate
adds no flake. `force_deploy` does not waive it (agent-forge-harness-oe6). See
[`workbench-wire-contract.md`](workbench-wire-contract.md).

## Static analysis gates

`python-tests` runs `ruff` and `mypy` before pytest; both are configured in
[`pyproject.toml`](../pyproject.toml).

`mypy` runs with **`warn_unreachable = true`**. Its scope is mypy's own
`files = ["service", "config.py"]` — it is the **Python** gate over those two
paths, every module in them and whoever wrote them, not a repository-wide
setting. It went on because a statement after a `return` is what a validator
looks like when it was edited without being re-read, and neither `ruff` nor
mypy's defaults see one. Tightening a gate that already passes can only add a
refusal class, never turn a currently-green tree red; the tree was verified
clean under it at the point it was turned on.

`warn_unreachable` has a known class of false positives: `if TYPE_CHECKING:`
bodies, `sys.version_info` guards, `assert_never` exhaustiveness arms, and
narrowing on a value typed `Any`. Silence one **per line**, with
`# type: ignore[unreachable]` and a comment naming which of those it is — never
by removing the flag, because the next genuinely dead branch then ships unseen.

## Parallel unit tests (pytest-xdist)

`python-tests`' pytest step runs with **`-n auto --dist loadfile`**
(agent-forge-harness-eddy, a follow-up to `agent-forge-harness-k768`), splitting
the ~6.8k unit/repo-guard tests across worker processes instead of one core.
`loadfile` assigns every test in a module to the same worker, so module-scoped
fixtures (and anything else a module's tests share) never split across workers.
`pytest-cov` combines each worker's coverage data automatically, so the coverage
gate is unchanged. `auto` sizes the worker count to the runner's own CPU count —
GitHub's public-repo Linux runners are 4 vCPU, so expect ~4 workers there.

The PostgreSQL job (`python-db-tests`) stays serial: per-worker databases are a
separate decision (agent-forge-harness-eddy), so it runs no `-n` flag.

Before switching, the full unit suite was run locally three consecutive times
under `-n 4 --dist loadfile`, plus once more with a reversed test-file order
(`pytest-randomly` is not a project dependency), to shake out order and
shared-state dependence. All runs matched the serial baseline's passed/skipped
counts — see the PR that introduced this section for the measured numbers and
wall-clock times.

A test that only fails under `-n`/`--dist` is depending on state shared across
the process (a module-global rate limiter, a reloaded env var, a fixed port or
temp path, captured logging). Fix the test's isolation when that's small;
otherwise pin it to a single worker with
`@pytest.mark.xdist_group(name="<reason>")` and a one-line reason for the pin.
Never weaken or delete an assertion to make a test pass under parallel workers.

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
  A manual run deploys **only from `master`**: `gh workflow run` targets the
  default branch unless you pass `--ref`, and the `deploy` job requires
  `github.ref == 'refs/heads/master'`. A run on any other ref tests without
  deploying — the job is skipped, with or without *force_deploy*.
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
| `deploy` | Repo **variable** `DEPLOY_TARGET` + WIF secrets `GCP_WIF_PROVIDER` / `GCP_DEPLOY_SA` (the job authenticates to GCP via Workload Identity Federation) | Hosting is the GCP pilot (epic `17u`, bead `x5bz.1`). The job **skips** until `DEPLOY_TARGET` is set — it never reports a green check for having deployed nothing (`x5bz.1.7`) — and once it runs it verifies the built image is actually serving 100% of traffic before succeeding. Set the WIF **secrets before** the variable: the `secrets` context cannot gate a job, so the variable alone starts a run with no credentials that fails at `docker push`. The one-time bootstrap — project, Cloud SQL, corpus, WIF — is the runbook [`docs/deploy-gcp.md`](deploy-gcp.md). `scripts/deploy.sh` encodes the "how", the variable the "where". |

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
uv run --with '.[dev]' python -m pytest -q -n auto --dist loadfile  # python-tests (dev extra: pytest-xdist)
DATABASE_URL=postgresql://<user>:<pass>@localhost:5432/<db> \
    uv run --with '.[test]' python -m pytest -q --no-cov \
    tests/test_schema.py ...                                 # python-db-tests (needs a real Postgres)
cd ui && bun run typecheck && bun run lint && bun run test  # ui-tests
uv run python contracts/workbench/tools/differential_fuzz.py  # contract-parity (needs bun too)
cd ui && bun run test:e2e                                   # production Compose E2E
docker compose -f docker-compose.e2e.yml config             # validate stack wiring
PYTHONUTF8=1 uv run --with "psycopg[binary]" --with openai \
    python ingestion/eval_golden.py                          # metrics (needs DB + key)
uv run python scripts/ci/eval_gate.py \
    --baseline <committed> --fresh ingestion/eval_results.json
```

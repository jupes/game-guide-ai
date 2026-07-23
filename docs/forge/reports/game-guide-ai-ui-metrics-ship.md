# Ship Report: game-guide-ai UI polish and metrics foundation

Shipped: 2026-07-23

Epic: `agent-forge-harness-eiio`

PR 1: [#36 — UI polish](https://github.com/jupes/game-guide-ai/pull/36)

PR 2: [#37 — metrics, dashboard, and E2E](https://github.com/jupes/game-guide-ai/pull/37) · Branch: `feat/eiio-metrics-observability`

## Summary

This two-PR release repairs the Aetheril shell’s switch, naming, and theme controls, then adds one
bounded service/UI metrics contract backed by the existing Langfuse integration. Runtime capture
is privacy-safe and fail-open, the dashboard has a credential-free offline reproduction path, and
Playwright now gates pull requests with a production-Compose conversation flow plus reviewed
TTFB/FCP/LCP/CLS budgets.

## Before → After

| Area | Before | After |
|---|---|---|
| Switches | Rendered as ambiguous rounded rectangles | Show distinct track/thumb state and keyboard focus |
| Conversation titles | Could not be renamed; active title was absent from the header | Rename inline, persist across reload, restore a prompt-derived fallback, and update the header reactively |
| Theme control | Lived inside the user menu | One keyboard-operable switch stays visible at the header’s top-right |
| Metrics contract | Service traces and eval metrics used separate implicit conventions | One catalog defines names, types, units, bounded labels, privacy, storage, and ownership for service, UI, and E2E |
| Runtime capture | Browser Web Vitals/interactions and bounded service outcomes were not stored | Same-origin UI batches and service middleware/handler outcomes become typed Langfuse scores without breaking chat |
| Trends | Existing summaries covered model/eval data only | Langfuse dashboard recipes and `metrics_summary.py` expose timestamped runtime series, with a committed offline fixture |
| Browser release test | Browser coverage stopped at Storybook component tests | Playwright drives the production Nginx/FastAPI Compose path through persistence, history recall, and attachment upload |
| PR safety | CI ran only after merge and deploy had no PR event guard | Pull requests run core tests plus E2E/performance; deploy requires E2E and cannot run for PR events |
| Fonts | Production timing depended on Google font CDNs | Version-pinned OFL fonts are bundled and verified offline |

## Work Done

- PR 1 / Checkpoint 1 — repaired visible switch states and real-Chromium assertions (`e720b64`).
- PR 1 / Checkpoint 2 — added observable conversation titles, first-prompt fallback, persistent
  inline rename, and the `AppNav` active-ID → store → `TopBar` bridge (`7836dfd`).
- PR 1 / Checkpoint 3 — moved the shared theme switch to `AppHeader` (`1fc8f49`).
- PR 2 / Checkpoint 4 — defined and tested the bounded service/UI metrics standard (`5788350`).
- PR 2 / Checkpoint 5 — implemented service and browser capture, Langfuse persistence,
  time-bucket summaries, an offline fixture, and dashboard recipes (`3b0e2de`).
- PR 2 / Checkpoint 6 — added deterministic production-Compose Playwright coverage, performance
  budgets/artifacts, self-hosted fonts, screenshots, and PR-safe CI (`838828b`).

## Beads Completed

| Beads ID | Title | Status |
|---|---|---|
| `agent-forge-harness-eiio.1` | Toggle switches render as plain rounded rectangles — no knob or on/off state | closed |
| `agent-forge-harness-eiio.2` | Rename conversations | closed |
| `agent-forge-harness-eiio.3` | Move dark-mode toggle to the top-right of the main page header | closed |
| `agent-forge-harness-eiio.4` | Establish a metrics-capture standard for service + UI | closed |
| `agent-forge-harness-eiio.5` | Metrics storage + dashboard to surface captured metrics | closed |
| `agent-forge-harness-l0xv` | Playwright E2E suite + UI performance metrics in CI | closed |

## Test Plan

- `uv run --with '.[test]' python -m pytest -q` — 394 passed, 1 skipped
- `bun install --frozen-lockfile`
- `bun run typecheck`
- `bun run lint`
- `bun run test` — 41 files and 469 tests passed, including Storybook Chromium coverage
- `bun run test:e2e` — production Compose flow passed in Chromium
- `bun run build`
- `docker compose -f docker-compose.e2e.yml config --quiet`

Latest local E2E baseline:

| Metric | Observed | Budget | Result |
|---|---:|---:|---|
| TTFB | 1.3 ms | 1500 ms | pass |
| FCP | 28 ms | 2000 ms | pass |
| LCP | 28 ms | 2500 ms | pass |
| CLS | 0 | 0.1 | pass |

## Acceptance Criteria

### Metrics standard (`agent-forge-harness-eiio.4`)

- [x] Service, UI, and Playwright use stable dot-delimited names with explicit kinds and units.
- [x] Labels and categories are allowlisted and bounded; non-finite and extra fields are rejected.
- [x] Privacy, fail-open semantics, ownership, Langfuse SDK-v3 compatibility, and retention
  responsibility are documented with service and UI examples.

### Capture, storage, and dashboard (`agent-forge-harness-eiio.5`)

- [x] `POST /metrics/ui` accepts strict bounded batches without exposing Langfuse credentials.
- [x] Service duration, error category, and answerability outcomes are recorded across success,
  validation, dependency, and handler failures.
- [x] Browser TTFB/FCP/LCP/CLS, chat round trip, and content-free client error counts are batched
  with beacon/fetch fallback.
- [x] Telemetry failures never break chat or the UI.
- [x] Langfuse dashboard recipes and legacy SDK-v3 time-bucket queries cover service and UI scores.
- [x] The committed runtime fixture reproduces every required series without credentials.

### Playwright and performance CI (`agent-forge-harness-l0xv`)

- [x] Playwright uses the production-built Nginx UI and deterministic FastAPI routes without a
  database, LLM, Langfuse, or font CDN.
- [x] The tracer enters the app, switches channel, creates/sends/reloads/reselects a persisted
  conversation, recalls history, and uploads an attachment.
- [x] Pre-navigation TTFB/FCP/LCP/CLS observations produce JSON and Markdown artifacts and enforce
  versioned budgets.
- [x] Pull requests run E2E, retain artifacts, cannot deploy, and E2E is required before deploy.

## Observability

- Browser metrics travel only to same-origin `POST /metrics/ui`; no Langfuse secret is shipped to
  the browser.
- Allowed labels are bounded operational dimensions such as environment, release, mode, route
  template, and browser family. Prompts, responses, conversation IDs, attachment names/content,
  user identity, stack traces, and arbitrary URLs are forbidden.
- Langfuse remains the durable store and dashboard. The service uses the repository’s pinned
  Langfuse SDK v3 and compatible legacy Metrics API queries.
- Recording is best-effort at one fail-open boundary: telemetry outages do not change chat
  responses. Deployments own credential configuration and retention policy.
- Offline reproduction:

  ```bash
  uv run python ingestion/metrics_summary.py \
    --from-runtime-metrics ingestion/runtime_metrics.sample.json
  ```

## Screenshots

![Production Compose — light theme](https://raw.githubusercontent.com/jupes/game-guide-ai/feat/eiio-metrics-observability/docs/forge/reports/assets/eiio-e2e-light.png)

![Production Compose — dark theme](https://raw.githubusercontent.com/jupes/game-guide-ai/feat/eiio-metrics-observability/docs/forge/reports/assets/eiio-e2e-dark.png)

## Test It Yourself

1. From the repository root, run:

   ```bash
   uv run python ingestion/metrics_summary.py \
     --from-runtime-metrics ingestion/runtime_metrics.sample.json
   ```

   Expect: `ingestion/metrics_summary.json` contains timestamped service latency/gate/error and UI
   interaction/Web Vital series without requiring credentials.

2. From `ui/`, run:

   ```bash
   bun install --frozen-lockfile
   bun run test:e2e
   ```

   Expect: Playwright builds the two-service production Compose stack, completes the persisted
   conversation/history/attachment flow, enforces all four budgets, writes
   `e2e-results/performance.{json,md}`, and cleans the stack up.

3. Inspect `docs/observability/metrics-standard.md` and `docs/observability/dashboard.md`.

   Expect: the producer contract, privacy rules, Langfuse widgets, live query, and offline
   reproduction use the same metric names and units.

## Follow-ups / Known Gaps

- No epic work is deferred.
- Langfuse credentials and the deployment-specific retention period remain operator configuration,
  by design.
- Native trace-version filtering is documented as a separate follow-up; this release keeps the
  supported SDK-v3 score/query contract rather than expanding into an SDK-v4 migration.

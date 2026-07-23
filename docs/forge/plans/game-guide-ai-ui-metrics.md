# Plan: game-guide-ai-ui-metrics — UI polish and metrics foundation

Generated: 2026-07-22
Repo: `repos/game-guide-ai`
Release branches: `fix/eiio-ui-polish` (checkpoints 1–3), then
`feat/eiio-metrics-observability` (checkpoints 4–6)
Phase: plan (2/4), from `docs/forge/research/game-guide-ai-ui-metrics.md`

## Summary

Deliver the six existing beads as six red-green-refactor checkpoints. First repair the switch's
broken DOM/CSS state contract, then make conversations observable and renameable from the real
first-prompt flow, then put the single theme switch in `AppHeader`. Define the metrics contract
before extending the existing Langfuse integration with bounded service/UI scores and a
credential-free offline time-series summary. Finish with a Playwright suite against the
production-built Nginx/FastAPI Compose path, deterministic injected RAG, Web Vital budgets, PR-safe
CI, artifacts, and the screenshot required by the PR.

No second metrics database or public admin UI is introduced. Langfuse remains the durable store and
trend dashboard; `metrics_summary.py` remains the reproducible local/read-only view. The UI-polish
work ships after checkpoint 3 so the P1 switch fix is not gated by the larger P2 observability PR.

## Existing code to reuse

- `ui/src/ds/Switch.tsx`, `Switch.css`, `Switch.stories.tsx` — repair the existing component and
  test it in the current Chromium-backed Storybook project.
- `ui/src/shell/conversationStore.ts` — both memory and local-storage implementations already own
  create/list/rename/remove; extend their contract rather than adding a second state store.
- `ui/src/shell/ConversationStoreContext.tsx` — subscribe React consumers here with
  `useSyncExternalStore`, while correcting its current per-render default-store construction.
- `ui/src/shell/AppNav.tsx` — source of truth for active `conversationId`; `TopBar` will use this ID
  to resolve the active conversation through the store.
- `ui/src/shell/LeftNav.tsx`, `AppHeader.tsx`, `UserMenu.tsx`, `ChatPane.tsx` — existing selection,
  channel/action band, shared theme state, and first-send integration seams.
- `ui/src/shell/TopBar.tsx` — currently brand-only; active-title lookup and rendering are new
  behavior, not an existing seam.
- `service/tracing.py` and Langfuse SDK v3 — existing env-gated credentials, trace metadata, and
  offline-safe behavior.
- `service/app.py` — FastAPI dependency injection and exception taxonomy; add metrics without
  changing chat's public response.
- `ingestion/metrics_summary.py` — existing Langfuse Metrics API query builder and JSON output.
- `service/history.py:InMemoryMessageStore` — real history/attachment behavior for deterministic E2E.
- `ui/vite.config.ts`, `ui/nginx.conf` — same-origin service proxy contract.
- `Dockerfile.service`, `ui/Dockerfile`, `docker-compose.yml` — production image/proxy patterns for
  a two-service deterministic E2E Compose file.
- `.github/workflows/ci.yml`, `docs/ci.md` — documented fourth-job seam before deploy.

## Architecture decisions

### Observable conversation store

Extend `ConversationStore` with `get(id)`, `subscribe(listener)`, `getSnapshot()`, and
`recordFirstPrompt(id, prompt)`. Each successful mutation increments a revision and notifies
subscribers. Subscription/snapshot callbacks are arrow properties (or memoized bound closures);
the snapshot is a cached numeric revision. `ConversationStoreProvider` replaces its current
default-parameter construction with one `useRef`-owned default store, and
`useConversationStore()` subscribes with `useSyncExternalStore`.

`Conversation` gains backward-compatible `derivedTitle`, `hasFirstPrompt`, and custom-title state;
legacy rows are normalized without deletion. `recordFirstPrompt()` updates the immutable fallback
once and updates the visible title only when it is not custom. `ChatPane.handleSend` calls it before
the real first post. One canonical `deriveConversationTitle(prompt)` helper is used by both
`create(mode, firstPrompt)` and `recordFirstPrompt` in both store implementations, preventing two
40-character derivation paths. `rename()` trims a non-empty custom title and restores
`derivedTitle` for whitespace.

The inline rename UI is an explicit icon action in each row. It opens a labelled input prefilled
with the current title. Enter or blur saves, Escape cancels. Selection remains a separate button so
nested interactive elements are avoided.

### Header ownership

`AppNav` remains the source of active selection. `TopBar` newly reads
`useAppNav().conversationId`, resolves it with the observable store's `get(id)`, and renders that
conversation's title beside the brand; store notifications make a rename update the header without
moving selection ownership. `AppHeader` keeps channel chips on the left and replaces its documented
right-hand future slot with the existing Aetheril `Switch` plus a visible "Dark theme" label.
`UserMenu` retains the Dungeon Master control but no theme control.

### Metrics boundary

Add a small `service/metrics.py` deep module:

- canonical names, units, allowed labels, and batch limits;
- typed numeric/boolean/categorical metric points and a `MetricsSink` protocol;
- an offline/no-op sink;
- an env-gated Langfuse sink that creates a bounded metrics span and typed numeric/boolean/categorical scores; and
- helpers for recording service success/error outcomes without letting telemetry fail chat.

All request models use strict Pydantic configuration
`ConfigDict(extra="forbid", allow_inf_nan=False)` (or `FiniteFloat`) at batch, point, and label
levels. `POST /metrics/ui` accepts only the UI allowlist, finite numeric values, bounded enum
labels, and a small batch. The route returns an accepted response when telemetry is disabled, while
unknown names/fields, non-finite values, and oversized input receive 422.

An HTTP middleware measures `/chat` duration and status across dependency resolution, validation,
and handler execution, recording `service.chat.error` plus bounded categorical
`service.chat.error_category`. The successful handler records `answerable`. Sink failures are
swallowed at one boundary. Existing Langfuse graph/generation observations remain the source of
tokens and cost.

`ui/src/metrics/` installs buffered PerformanceObservers for FCP/LCP/CLS, reads navigation timing for
TTFB, records `ui.interaction.chat_round_trip_ms` from prompt submit through request settlement,
counts client `error`/`unhandledrejection` events without capturing their text, and flushes
same-origin batches with `sendBeacon` plus keepalive-fetch fallback. Telemetry failure is swallowed.

### Storage and dashboard

Langfuse persists service observations and new numeric, boolean, and categorical metric scores.
Because `pyproject.toml` pins Langfuse SDK v3, extend and test the existing legacy Metrics API
client explicitly; add `timeDimension` and score-view queries rather than silently using the SDK-v4
v2 client. `metrics_summary.py` formats timestamped latency/gate/error/interaction/Web Vital
series. A committed `ingestion/runtime_metrics.sample.json` and mutually exclusive
`--from-runtime-metrics` path mirror the existing `--from-results` argparse behavior, JSON envelope,
UTF-8 handling, and output-path convention while feeding the same time-bucketing formatter for
credential-free local reproduction. The dashboard doc gives the stable UI widget recipe and both
live/offline commands.

### E2E environment

`service/e2e_app.py` reuses production FastAPI routes with deterministic RAG and
`InMemoryMessageStore`, and explicitly replaces the production lifespan before startup. A service
test fails if E2E startup constructs `RagService` or `PostgresMessageStore`.

`docker-compose.e2e.yml` is a self-contained two-service stack: `service` uses
`Dockerfile.service` with the E2E app command, and `ui` uses the production `ui/Dockerfile`/Nginx
proxy. There is no vector DB, LLM, or Langfuse dependency. Playwright global setup/teardown runs
`docker compose up --build --detach --wait` / `down`, then one Chromium worker drives the core flow
and collects observations installed before navigation.

Replace remote font imports with exact `5.3.0` OFL-1.1 packages
`@fontsource-variable/inter`, `@fontsource/spectral`, and
`@fontsource-variable/material-symbols-rounded`. The production Vite bundle then carries the fonts
used by performance and screenshot tests, and a guard rejects external font URLs.

The E2E reporter writes `ui/e2e-results/performance.json`, a Markdown table for
`GITHUB_STEP_SUMMARY`, and screenshots/traces on failure. Budgets live in a reviewed JSON file and
use the exact runtime metric names/units.

## TDD strategy

Tests specify observable behavior through public component/store/HTTP/CLI/browser surfaces. No test
asserts private helper call order.

| # | Behavior (specification) | Test seam | Tracer? |
|---|---|---|---|
| 1 | Off/on switch states have different track colors and thumb positions in real Chromium | `Switch.stories.tsx` play test | yes |
| 2 | Keyboard focus produces a visible ring on the actual switch button | Storybook play test | no |
| 3 | Create → first send derives fallback through the same helper as `create(firstPrompt)`; custom rename persists; blank restores it after reload | store + `ChatPane` integration tests | yes |
| 4 | The default store and bound subscriptions survive provider rerenders; subscribers notify once and unsubscribe | store/context tests | no |
| 5 | `AppNav.conversationId` resolves through `store.get(id)`; inline save updates both list and new `TopBar` title, while Escape cancels | `TopBar` + shell component tests | no |
| 6 | `AppHeader` theme switch changes document theme by keyboard and no duplicate exists in `UserMenu` | AppHeader/UserMenu tests | yes |
| 7 | Canonical metric names expose numeric/boolean/categorical kinds, units, and labels; strict models reject extras/non-finite/unbounded input | `service/tests/test_metrics.py` | yes |
| 8 | UI metric endpoint accepts a bounded batch and records it through an injected fake sink | `service/tests/test_metrics.py` | no |
| 9 | Invalid/non-finite/oversized UI batches return 422; disabled/failing telemetry never breaks chat | service route tests | no |
| 10 | Middleware records duration/status for success, validation, dependency, and handler failures; handler records gate outcome | `service/tests/test_metrics.py` | no |
| 11 | Browser collector maps TTFB/FCP/LCP/CLS, chat round-trip, and client error count to canonical payloads | `ui/src/metrics/metrics.test.ts` + `useChat` test | yes |
| 12 | Beacon rejection falls back to keepalive fetch; transport failure does not escape | UI metrics test | no |
| 13 | Legacy score queries include time buckets; a committed runtime fixture produces every required timestamped series offline | `ingestion/tests/test_metrics_summary.py` | no |
| 14 | Production Compose UI creates a conversation, sends, reloads/re-enters/reselects it, recalls history, and uploads an attachment with no DB/LLM/Langfuse/font-CDN dependency | Playwright + E2E startup/network guard | yes |
| 15 | TTFB/FCP/LCP/CLS are finite, emitted to JSON/Markdown, and fail above versioned budgets | Playwright performance spec/helper | no |
| 16 | Pull requests run tests/E2E, deploy is forbidden on PR events, and E2E is a required predecessor of deploy with artifacts | workflow contract test | no |

Refactor watch list after green:

- share one title-derivation/normalization helper across both stores, `create(firstPrompt)`, and
  `recordFirstPrompt`;
- keep the default store and external-store callbacks stable across React rerenders;
- keep metric validation/catalog data-driven, not parallel `if` chains;
- keep service recording behind one fail-safe boundary;
- keep runtime and E2E metric names imported/generated from one canonical UI catalog where practical;
- avoid a broad App-shell state rewrite or generic analytics framework.

## Build sequence and checkpoints

### Checkpoint 1 — `eiio.1`: switch visual-state contract

Red:

1. Add a Storybook play assertion that renders off/on states in Chromium and proves track color,
   thumb position, and actual-button focus differ as expected.
2. Run the targeted Storybook browser test and capture the expected failure.

Green/refactor:

1. Reset the outer button, and make checked/disabled/focus selectors traverse from
   `.aether-switch-wrap` to the visual track/handle.
2. Verify light and dark stories and reduced-motion behavior.
3. Run Switch unit/story tests, UI typecheck/lint, then full UI tests.

Files: `ui/src/ds/Switch.css`, `ui/src/ds/Switch.stories.tsx` (and `Switch.test.tsx` only if an ARIA
regression assertion is needed).

Checkpoint demo: open the Switch story or user menu and toggle by mouse/Space in light and dark.

Commit:

```text
fix(ui): restore switch visual states

Refs: agent-forge-harness-eiio.1
```

### Checkpoint 2 — `eiio.2`: rename + reactive active title

Red:

1. Store/context tests for one derivation helper's behavior through both public entry points,
   stable default-store/subscription identity across provider rerenders, trim/persistence,
   first-prompt fallback, whitespace restore, unsubscribe, and legacy-row normalization.
2. `ChatPane` integration test drives create → first send → rename → blank restore → reload.
3. `TopBar`/shell test supplies an active ID through `AppNav`, resolves it through the store, drives
   inline rename, and observes both list and active header title.

Green/refactor:

1. Add `get(id)`, shared `deriveConversationTitle`, derived/custom/first-prompt state,
   `recordFirstPrompt`, normalization, stable revision/subscription, one provider-owned default
   store, and `useSyncExternalStore`.
2. Call `recordFirstPrompt` from the real `ChatPane.handleSend`.
3. Add the inline rename action/input; wire `TopBar` from `AppNav.conversationId` through
   `store.get(id)` to the new title.
4. Run targeted store/shell tests, typecheck/lint, then full UI tests.

Files: `conversationStore.ts`, `ConversationStoreContext.tsx`, `conversationStore.test.ts`,
`LeftNav.tsx`, `LeftNav.css`, `ChatPane.tsx`, their tests, `TopBar.tsx`, `TopBar.css`, and a
dedicated `TopBar.test.tsx`. `AppNav.tsx` remains the unmodified selection source.

Checkpoint demo: create/select a conversation, rename it, reload, then blank the name to restore the
derived title.

Commit:

```text
feat(ui): add persistent conversation renaming

Refs: agent-forge-harness-eiio.2
```

### Checkpoint 3 — `eiio.3`: top-right theme switch

Red:

1. Update `AppHeader` tests to require a keyboard-operable theme switch in its right-hand slot.
2. Require `UserMenu` to contain no theme switch.

Green/refactor:

1. Replace `AppHeader`'s reserved future slot with the shared `useTheme()` control.
2. Remove theme-only code and CSS from `UserMenu`.
3. Verify light/dark styling, narrow layout, keyboard focus, and no duplicated accessible name.
4. Run targeted shell tests, typecheck/lint, then full UI tests.

Files: `AppHeader.tsx`, `AppHeader.css`, `AppHeader.test.tsx`, `UserMenu.tsx`, `UserMenu.css`,
`userMenu.test.tsx`.

Checkpoint demo: use Tab+Space on the always-visible header switch and confirm one control only.

Commit:

```text
feat(ui): move theme switch to header

Refs: agent-forge-harness-eiio.3
```

### Checkpoint 4 — `eiio.4`: metrics standard

Red:

1. Add catalog/validation tests for numeric/boolean/categorical names, units, labels, strict extra
   rejection, finite values, and privacy rejection.
2. Confirm they fail before the catalog exists.

Green/refactor:

1. Add the minimal catalog/validation boundary in `service/metrics.py`.
2. Write `docs/observability/metrics-standard.md` covering service + UI names, types, labels,
   transport, SDK-v3/legacy-query version, storage, privacy, failure semantics, ownership, and
   deployment-defined retention.
3. Include one complete service example and one complete UI example.
4. Cross-reference the standard from dashboard and E2E/CI docs.
5. Run targeted Python tests and the full Python gate.

Files: `service/metrics.py`, `service/tests/test_metrics.py`,
`docs/observability/metrics-standard.md`, `docs/observability/dashboard.md`, `docs/ci.md`.

Checkpoint demo: walk the two example events from producer through validation to Langfuse
representation/dashboard query.

Commit:

```text
docs(observability): define service and UI metrics standard

Refs: agent-forge-harness-eiio.4
```

### Checkpoint 5 — `eiio.5`: capture, persistence, and dashboard

Red:

1. Add fake-sink HTTP tests for accepted/rejected strict UI batches and fail-open behavior.
2. Add middleware tests for success, 422, dependency 503, and handler 5xx plus gate recording.
3. Add browser Web Vital, chat-round-trip, and transport tests.
4. Extend metrics-summary tests for legacy timeDimension queries and offline fixture series.

Green/refactor:

1. Implement no-op/Langfuse numeric/boolean/categorical sinks and strict FastAPI endpoint.
2. Instrument `/chat` duration/status/error in middleware and answerable outcome in the handler.
3. Implement browser Web Vital/interaction/error batching and start it from `main.tsx`.
4. Add `/metrics` to Vite and nginx proxies.
5. Extend the legacy-query `metrics_summary.py`, add the committed runtime fixture/offline reader,
   and document live/offline time-series views for latency, cost/tokens, gate outcomes, error
   rate/category, interaction, and Web Vitals.
6. Run targeted Python/UI tests, then full Python + UI gates.

Files: `service/metrics.py`, `service/app.py`, service tests, `ui/src/metrics/*`, `ui/src/main.tsx`,
`ui/vite.config.ts`, `ui/nginx.conf`, `ingestion/metrics_summary.py`,
`ingestion/tests/test_metrics_summary.py`, `ingestion/runtime_metrics.sample.json`, observability docs.

Checkpoint demo: run the committed runtime fixture through the offline summary to show all
timestamped service/UI series, then post a strict sample batch/run deterministic chat into a
recording sink and show their matching schema plus the live Langfuse recipe.

Commit:

```text
feat(observability): capture service and UI metrics

Refs: agent-forge-harness-eiio.5
```

### Checkpoint 6 — `l0xv`: Playwright core flows + performance CI

Red:

1. Add Playwright config/specs and the performance artifact assertion before the Compose E2E stack
   exists; record the expected failure.
2. Add workflow contract assertions for PR trigger, deploy PR guard, artifacts, and E2E dependency.
3. Pre-check the exact font packages before editing the lockfile. Verified 2026-07-22 via the npm
   registry: `@fontsource-variable/inter@5.3.0`, `@fontsource/spectral@5.3.0`, and
   `@fontsource-variable/material-symbols-rounded@5.3.0`, each `OFL-1.1`.

Green/refactor:

1. Add deterministic E2E FastAPI overrides plus an explicit no-op lifespan and startup isolation test.
2. Add `@playwright/test` at the version compatible with the existing Playwright packages plus the
   three exact `@fontsource` 5.3.0 packages; replace remote font imports and update the Bun lockfile.
3. Add the self-contained E2E Compose file using the production Nginx UI and deterministic service;
   Playwright setup/teardown owns its lifecycle.
4. Drive landing → workspace → channel switch → New conversation → prompt → reload → re-enter
   workspace → restore the same channel → reselect the persisted conversation → verify recalled
   history → upload attachment. Assert no external font request is made.
5. Install pre-navigation performance observers; write TTFB/FCP/LCP/CLS JSON and Markdown; enforce
   reviewed budgets after measuring the local served baseline.
6. Capture the final light/dark polished-shell screenshot for the PR.
7. Add `pull_request` CI, forbid deploy on PR events, add `ui-e2e` after core tests, upload results,
   require it in deploy, and document local commands.
8. Run E2E locally, then full Python/UI/E2E gates.

Files: `service/e2e_app.py`, its startup test, `docker-compose.e2e.yml`,
`ui/playwright.config.ts`, `ui/e2e/*`,
`ui/e2e/performance-budget.json`, `ui/src/ds/tokens/fonts.css`, `ui/package.json`, `ui/bun.lock`,
workflow/CI docs, and a report asset under `docs/forge/reports/assets/`.

Checkpoint demo: watch Playwright exercise the production Compose application and inspect the generated
performance table, trace/screenshot, and green CI dependency graph.

Commit:

```text
test(e2e): add core flows and UI performance gate

Refs: agent-forge-harness-l0xv
```

## Validation commands

From `repos/game-guide-ai`:

```bash
uv run --with '.[test]' python -m pytest -q
cd ui
bun install --frozen-lockfile
bun run typecheck
bun run lint
bun run test
bun run test:e2e
bun run build
cd ..
docker compose -f docker-compose.e2e.yml config
```

Targeted commands will run first in every red/green cycle. Full commands run at every checkpoint
appropriate to changed code, and all commands run again during ship.

## Beads issue map and dependencies

No duplicate tasks will be created; the user supplied the complete task set.

| Bead | Type | Priority | Checkpoint | Depends on |
|---|---|---:|---:|---|
| `agent-forge-harness-eiio.1` | bug | P1 | 1 | — |
| `agent-forge-harness-eiio.2` | feature | P2 | 2 | — |
| `agent-forge-harness-eiio.3` | feature | P2 | 3 | — (sequenced after `eiio.1`, but independently deliverable) |
| `agent-forge-harness-eiio.4` | decision | P2 | 4 | — |
| `agent-forge-harness-eiio.5` | feature | P2 | 5 | `eiio.4` |
| `agent-forge-harness-l0xv` | feature | P2 | 6 | related to/informed by `eiio.4` |

The current durable graph already has all six as children of `agent-forge-harness-eiio`, the
required `eiio.4` → `eiio.5` blocking edge, and the two-way `eiio.4`/`l0xv` relationship. No new
dependency edge is required. Priority assignments are unchanged: `eiio.1` is a
user-visible/ambiguous-state P1; the remaining foundational but non-urgent work is P2.

## PR and ship structure

This workload deliberately ships as two sequential PRs. The first is a release boundary after
checkpoint 3; the epic stays open until the second PR merges.

### PR 1 — UI polish (`eiio.1`–`.3`)

- Branch: `fix/eiio-ui-polish`
- Primary Bead/title (exact repo-rule match):

```text
[game-guide-ai][ui] Toggle switches render as plain rounded rectangles — no knob or on/off state
```

- Body sections: **Summary**, **Test Plan**, **Acceptance Criteria** for `.1`–`.3`, and
  **Screenshots** in both themes.
- Run all UI gates, push, open/watch the PR, and pause for merge before starting PR 2 from the
  updated `origin/master`. This gets the P1 fix reviewed and shipped without waiting for metrics.

### PR 2 — Observability and E2E (`eiio.4`, `eiio.5`, `l0xv`)

- Branch: `feat/eiio-metrics-observability`
- Primary Bead/title (exact repo-rule match):

```text
[game-guide-ai] Metrics storage + dashboard to surface captured metrics
```

- Body sections: **Summary**, **Test Plan**, **Acceptance Criteria** for `.4`, `.5`, and `l0xv`,
  **Screenshots**, and **Observability** with privacy/storage/local-reproduction details.
- The final Forge ship report links both PRs and closes the epic only after all six beads are done.

Before each PR: its beads are closed with worklogs, branch rebased on `origin/master`, applicable
full gates green, `bd dolt push` and `git push` successful. For PR 2, the workflow must run on
`pull_request`; deploy must explicitly require `github.event_name != 'pull_request'`. Watch every
check, including `ui-e2e`, to a terminal green result before merge.

## Estimated scope

- PR 1: roughly 1 new and 12 modified files; medium UI complexity.
- PR 2: roughly 14 new and 12 modified files plus docs/report assets; medium-high observability/E2E
  complexity.
- Six explicit user demo/test pauses, one per existing bead.
- Review loop: turn 1 NEEDS REVISION (0 blocker / 6 high / 6 medium); turn 2 NEEDS REVISION
  (0 blocker / 1 high / 1 medium). All findings from both allowed review turns are addressed in
  this final revision: the E2E flow now creates/reselects a persisted conversation, and production
  fonts are self-hosted for deterministic offline performance.
- User review: 2 high / 3 medium / 3 low-or-advisory findings. All factual gaps are resolved;
  the suggested split-PR release boundary is adopted.

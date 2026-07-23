# Research: game-guide-ai-ui-metrics — UI polish and metrics foundation

Generated: 2026-07-22
Owning repo: `repos/game-guide-ai`
Tracking repo: `agent-forge-harness`
Phase: research (1/4)
Beads: epic `agent-forge-harness-eiio`; children `eiio.1`–`eiio.5`; existing child `agent-forge-harness-l0xv`

## Goal

Complete the second game-guide-ai polish wave as one coherent workload:

1. repair the Aetheril switch so its thumb, state, and focus are visible;
2. let users rename conversations and see the active name in the shell header;
3. move the existing theme control from the user menu to the header;
4. define one bounded metrics contract for the Python service, browser UI, and Playwright;
5. persist and visualize those metrics through the app's existing Langfuse observability stack; and
6. add a real Playwright E2E/performance gate before deploy.

## What the code says

### Switch regression (`eiio.1`)

- `ui/src/ds/Switch.tsx` puts `data-checked`, `disabled`, focus, and the
  `aether-switch-wrap` class on the outer `<button role="switch">`. The visual track is an inner
  `<span class="aether-switch">`.
- `ui/src/ds/Switch.css` targets `.aether-switch[data-checked=...]`,
  `.aether-switch:focus-visible`, and `.aether-switch:disabled`. Those selectors can never match
  because the relevant state lives on the outer button.
- The actual button also lacks the browser-button reset that was mistakenly applied to the inner
  span. This explains the reported plain rectangle.
- Existing unit tests only prove the ARIA/data attributes. The Storybook browser project is the
  correct seam for a regression test that compares computed track color, thumb position, and focus.
- The component was introduced in commit `cc89d56`; history shows no later behavioral change. This
  is an original DOM/CSS contract bug, not a dead color token.

### Conversation rename and header placement (`eiio.2`, `eiio.3`)

- `MemoryConversationStore` and `LocalStorageConversationStore` already expose `rename(id, title)`.
  Local storage persistence is therefore present, but whitespace fallback is not.
- Conversation titles are derived in `create()` from the first prompt (trimmed to 40 characters) or
  `"New conversation"`. The stored model does not retain that derived fallback separately.
- Production `LeftNav` calls `create(mode)` without a prompt, and `ChatPane`/`useChat` never sends
  the first prompt back to the conversation store. Prompt-derived fallback therefore exists only
  in store tests today; the feature must connect the real first-send path.
- `LeftNav` renders each title as a selection button and has no rename affordance.
- `AppNav` owns the active `conversationId` and `setConversationId`; the conversation store owns
  title data. An active header title therefore needs an explicit ID-to-store lookup bridge rather
  than treating selection as store state.
- `useConversationStore()` returns a mutable store without a subscription. `LeftNav` forces a local
  rerender only after create. A rename from one component cannot reliably update the list and a new
  active-conversation title in another component.
- `TopBar` is brand-only today; it has no conversation/store wiring. Adding an active title is new
  shell behavior. `AppHeader` owns the channel chips and already reserves a right-hand
  future-actions slot. `WorkspaceShell` renders `TopBar`, then `AppHeader`, then the two-column body.
- The existing dark-mode switch is inside `UserMenu` and uses the shared `useTheme()` state. The
  issue explicitly recommends moving it, not duplicating it.

The smallest durable shell design is:

- make the existing conversation store observable, add an ID lookup, and have its React context
  subscribe with `useSyncExternalStore`;
- retain the derived title and whether it has received its first prompt, then update that fallback
  from the real `ChatPane` first-send path while preserving a custom title; both `create(prompt)`
  and `recordFirstPrompt` must call one canonical title-derivation helper;
- add an explicit keyboard-accessible inline rename action to each conversation row; and
- let `TopBar` read `conversationId` from `AppNav`, resolve it through the observable store, and
  show the active conversation title while `AppHeader` replaces its reserved
  right-hand slot with the one shared theme switch, removing the duplicate from `UserMenu`.

### Existing observability foundation (`eiio.4`, `eiio.5`)

- The service already selected Langfuse over Phoenix and documents that decision in
  `docs/observability/`. This workload should extend that system, not introduce Prometheus,
  OpenTelemetry storage, or an app-owned metrics database.
- `service/tracing.py` attaches a Langfuse callback when `RAG_TRACING` is enabled. Graph and model
  observations already carry latency, token, and cost data plus model/mode/version metadata.
- `ingestion/metrics_summary.py` already queries the Langfuse Metrics API for scriptable summaries,
  while `docs/observability/dashboard.md` documents the built-in dashboard.
- The missing pieces are a cross-runtime naming/label contract, explicit gate/error outcome scores,
  a secure browser-to-service ingestion path, UI Web Vital/error collection, and a unified dashboard
  specification that includes those scores.
- Browser code must never receive `LANGFUSE_SECRET_KEY`. UI metrics should post same-origin batches
  to a strict FastAPI endpoint; the service should validate an allowlist and write scores/traces
  server-side.
- Langfuse numeric/boolean/categorical scores are queryable through the Metrics API and usable in
  custom dashboards. This is a direct fit for Web Vitals, gate outcomes, and error categories:
  [Scores API](https://langfuse.com/docs/api-and-data-platform/features/scores-api),
  [Metrics API](https://langfuse.com/docs/metrics/features/metrics-api), and
  [Custom dashboards](https://langfuse.com/docs/metrics/features/custom-dashboards).
- Dashboard/widget APIs and portable JSON were announced on 2026-07-17 but are explicitly unstable.
  The implementation should keep the stable Langfuse UI plus the repo's scriptable summary as the
  reproducible interface instead of coupling production code to an unstable endpoint.
- The repo pins Langfuse Python SDK v3 (`langfuse>=3,<4`). The current script uses the legacy
  Metrics API client. This workload should extend and test that compatible contract (including
  `timeDimension`) rather than silently mixing it with the new v2 client or expanding scope into an
  SDK-v4/LangChain-callback migration.

Recommended contract:

| Source | Logical metric | Representation |
|---|---|---|
| service | request count/latency, generation tokens/cost | existing Langfuse traces/observations |
| service | grounding gate result | bounded boolean/categorical score |
| service | request error | bounded categorical score; error detail remains in structured logs |
| UI | TTFB/FCP/LCP/CLS | numeric score in milliseconds, except unitless CLS |
| UI | chat round-trip interaction | numeric milliseconds with bounded success/error outcome |
| UI | client error | bounded counter/category with no message, URL query, prompt, or user content |
| Playwright | TTFB/FCP/LCP/CLS | versioned JSON artifact using the same names/units plus CI budgets |

Names should be dot-delimited and stable, such as `service.chat.gate.answerable`,
`service.chat.error`, `ui.web_vital.lcp_ms`, and `ui.client.error_count`. Labels must be bounded:
environment, release, mode, route template, and browser family are allowed; conversation IDs,
prompts, stack traces, filenames, free-form URLs, and user identifiers are not.

### Playwright and CI seam (`l0xv`)

- No Playwright test runner or E2E specs exist. The existing Playwright dependency supports Vitest's
  Storybook browser project only.
- `ui/vite.config.ts` and production `ui/nginx.conf` proxy `/chat`, `/healthz`, and
  `/conversations`; the new `/metrics` route must remain synchronized.
- `.github/workflows/ci.yml` has `python-tests`, `ui-tests`, `retrieval-metrics`, and a deploy job
  whose `needs` list is the intended insertion seam. It currently has no `pull_request` trigger, so
  the PR cannot receive automatic checks until one is added and deploy is explicitly excluded on
  PR events.
- The service already has in-memory message storage and dependency injection. A dedicated E2E app
  can run the real FastAPI routes with deterministic RAG and in-memory history/attachments, but
  request dependency overrides alone do not bypass the production lifespan; the E2E app must
  explicitly replace startup and prove it never constructs Postgres/RAG dependencies.
- The bead asks for the real Compose path. A dedicated two-service Compose file can reuse
  `Dockerfile.service` and the production-built Nginx UI while substituting only the deterministic
  E2E app and omitting vector DB/LLM services.
- Production CSS currently imports Google Fonts/Material Symbols from remote CDNs. For stable
  FCP/LCP and a truly offline Compose run, replace those imports with the version-pinned OFL-1.1
  `@fontsource` packages for Inter, Spectral, and Material Symbols Rounded; the production bundle
  then contains the same fonts measured by CI.
- Playwright can seed `PerformanceObserver` before navigation, drive landing → workspace → channel
  switch → new conversation → prompt → reload → re-enter workspace/channel → reselect the persisted
  conversation → verify recalled history → attachment against that stack, collect
  TTFB/FCP/LCP/CLS, compare versioned budgets, emit JSON and a Markdown summary, and capture the UI
  screenshot required by the PR.

## Decisions resolved from durable repo context

| Question | Decision | Why |
|---|---|---|
| Metrics backend | Extend Langfuse | Already selected, integrated, documented, and storing model latency/tokens/cost. |
| Browser transport | Same-origin `POST /metrics/ui` to a server-side sink | Keeps secrets out of the browser and centralizes validation/privacy. |
| Dashboard | Langfuse custom dashboard + extended `metrics_summary.py` + offline runtime fixture | Meets persistence/trend needs without a second database or admin UI, and makes local reproduction credential-free. |
| Theme control | Move entirely to `AppHeader`'s right-hand actions slot | This is the exact band named by the bead; `useTheme()` remains the source of state. |
| Rename interaction | Explicit row action with inline input; Enter/blur save, Escape cancels | Discoverable and keyboard accessible; double-click may remain an optional shortcut. |
| Cross-component updates | `AppNav` active ID → observable store lookup → `TopBar` | Keeps selection ownership in `AppNav` while making title mutations reactive everywhere. |
| Release shape | UI-polish PR after checkpoint 3, metrics/E2E PR after checkpoint 6 | Ships the P1 switch fix without waiting for the larger P2 observability stack. |
| E2E backend | Production-built Nginx UI with self-hosted fonts + deterministic FastAPI service in a two-service Compose stack | Exercises the requested production proxy/assets without DB, LLM, Langfuse, or font-CDN dependencies. |
| Performance gate | Same metric names/units as runtime telemetry, versioned budgets, JSON + step summary | Makes CI output comparable with the dashboard and reviewable as an artifact. |

## Test seams and expected vertical slices

1. **Switch behavior** — a Storybook browser interaction fails on identical track/thumb computed
   states, then passes after the selector/reset fix; component tests retain ARIA behavior.
2. **Rename behavior** — store tests define one shared title derivation path, first-prompt fallback,
   persistence, stable subscriptions, and notifications; shell tests drive first send,
   AppNav-ID-to-store header lookup, edit/save/cancel, blank restore, reload, and list/header
   synchronization.
3. **Header theme behavior** — shell tests prove the top-right switch changes `data-theme`, is
   keyboard accessible, and no theme switch remains in `UserMenu`.
4. **Metrics contract** — pure Python tests validate accepted names/types/labels and strict
   finite/no-extra payloads; docs carry one service and one UI worked example.
5. **Persistence/dashboard** — fake sink/middleware tests prove server-side recording (including
   dependency/validation failures) and graceful disabled behavior; time-bucket summary tests consume
   a committed runtime fixture; browser unit tests prove Web Vital, interaction, and error batching.
6. **E2E/performance** — Playwright runs against the production Compose UI/API, creates and
   reselects a conversation across reload before proving history/attachment flows, enforces
   TTFB/FCP/LCP/CLS budgets, and emits artifacts/summary/screenshot; CI deploy depends on this job.

## Constraints and non-goals

- Preserve existing env-gated/offline-safe behavior: tests and local UI must not require Langfuse
  credentials or make network calls.
- No raw prompt, response, conversation ID, attachment content/name, user identity, stack trace, or
  arbitrary URL in metric values or labels.
- Metric ingestion must be bounded by an allowlist, batch-size limit, finite numeric validation, and
  best-effort failure handling so telemetry never breaks chat.
- Keep Vite and nginx proxy paths synchronized.
- No new bespoke metrics database, general-purpose observability platform migration, or public
  analytics page in this epic.
- No Lighthouse dependency is necessary: Playwright's browser timing APIs provide the requested
  navigation and Web Vital measurements with less CI weight.
- Production font assets are self-hosted from version-pinned OFL packages so Web Vital budgets do
  not depend on Google CDN availability.
- Existing known Langfuse metadata-query limitations are not broadened into a platform migration;
  the standard uses currently queryable fields and scores.
- The SDK-v3/legacy Metrics API remains explicit for this epic; a Langfuse SDK-v4 migration is a
  separate compatibility workload.

## Risks and assumptions carried into planning

- Langfuse API availability must degrade to structured warnings/no-op, never a failed chat or UI
  request.
- Web Vitals can vary in shared CI. Budgets should be generous enough for the production Compose stack,
  recorded in a reviewed config file, and validated against an actual baseline before commit.
- CLS and LCP need buffered observers installed before navigation and a deterministic flush point.
- Local-storage schema evolution must normalize legacy conversations without deleting them.
- `useSyncExternalStore` requires stable/bound callbacks and a cached snapshot; the provider must
  own one default store instance across rerenders. The current default-parameter construction does
  not, so this is a bug correction with a load-bearing rerender test.
- Storybook computed-style assertions need Chromium; jsdom is insufficient for this switch bug.
- The current Beads export reports no blocking dependencies even though the epic design states that
  `eiio.4` blocks `eiio.5` and informs `l0xv`; a direct durable-row check confirms those
  relationships and all six parent links already exist.
- The tracking database is compatible with `bd` 1.0.4; it must not be migrated with a newer CLI.

## Recommended planning scope

Use six checkpoints mapped one-to-one to existing beads, with the P1 switch first and the metrics
standard before both metrics implementation and E2E. Insert an explicit release boundary after
checkpoint 3:

1. `eiio.1` switch regression;
2. `eiio.2` conversation rename and reactive active title;
3. `eiio.3` header theme control;
   **Ship PR 1:** UI polish (`eiio.1`–`.3`), then update from `origin/master`;
4. `eiio.4` metrics standard and worked examples;
5. `eiio.5` server/UI capture, Langfuse persistence, and dashboard/summary;
6. `l0xv` real Playwright flows, performance budgets/artifacts, CI dependency, and PR screenshot.

Each checkpoint should use red-green-refactor tests, run targeted gates before full gates, commit
with its bead reference, update/close that bead with a worklog, and pause for a user-visible
demo/test before the next checkpoint. The epic remains open across both PRs.

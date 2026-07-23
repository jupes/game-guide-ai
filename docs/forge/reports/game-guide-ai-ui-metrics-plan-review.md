# Plan Review: game-guide-ai-ui-metrics — user follow-up

Source: `docs/forge/plans/game-guide-ai-ui-metrics.md` plus user review
Reviewed: 2026-07-22

## Verdict: SOUND after revision — 0 Blocker / 0 High / 0 Medium / 0 Low

Every factual gap in the follow-up review was confirmed against the code and incorporated into the
research and plan artifacts. The delivery plan now has the real active-selection bridge and an
earlier release boundary for the P1 UI bug.

## Findings resolved

### Active title bridge

Confirmed: `AppNav` owns `conversationId`/`setConversationId`
(`ui/src/shell/AppNav.tsx:26-29`), while `LeftNav` creates/selects conversations through it
(`ui/src/shell/LeftNav.tsx:20,31-34,72`). The plan now specifies:

`AppNav.conversationId → ConversationStore.get(id) → TopBar title`.

Selection remains in `AppNav`; the observable store supplies reactive title data.

### TopBar is new behavior

Confirmed: `TopBar` currently renders only the brand (`ui/src/shell/TopBar.tsx:9-20`). The plan no
longer describes an existing active-title seam. It budgets a new `TopBar` lookup/render path and a
dedicated component test.

### One title derivation path

Confirmed: both current store implementations independently slice `firstPrompt` to 40 characters
(`ui/src/shell/conversationStore.ts:25,89`), while production `LeftNav` calls `create(mode)` without
a prompt. The plan now requires one `deriveConversationTitle(prompt)` helper shared by both stores,
`create(firstPrompt)`, and `recordFirstPrompt`.

### Stable default store is load-bearing

Confirmed: `ConversationStoreProvider` currently evaluates
`store = new LocalStorageConversationStore()` in its parameter list
(`ui/src/shell/ConversationStoreContext.tsx:11`). The plan calls this a correction and explicitly
tests default-store identity plus subscription behavior across provider rerenders.

### P1 release boundary

Accepted: the workload is split into two sequential PRs:

1. `fix/eiio-ui-polish` for `eiio.1`–`.3`;
2. `feat/eiio-metrics-observability` for `eiio.4`, `eiio.5`, and `l0xv`.

The epic remains open across both. This prevents the P1 switch repair from waiting on the
observability/Compose work.

### Low/advisory items

- Checkpoint 2 numbering is corrected.
- Registry verification on 2026-07-22 confirmed these exact packages and licenses:
  `@fontsource-variable/inter@5.3.0` (`OFL-1.1`),
  `@fontsource/spectral@5.3.0` (`OFL-1.1`), and
  `@fontsource-variable/material-symbols-rounded@5.3.0` (`OFL-1.1`).
- `--from-runtime-metrics` now explicitly mirrors the existing `--from-results` argparse,
  UTF-8/JSON-envelope, and output-path conventions
  (`ingestion/metrics_summary.py:112,133-135`).

## Verified as accurate

- The proposed `ChatPane` first-prompt wiring is feasible because it already reads
  `conversationId` from `useAppNav()` (`ui/src/shell/ChatPane.tsx:76`).
- The Beads graph already contains all six children and the standard/dashboard relationship.
- The existing Langfuse-v3 query-builder/offline seams remain the right test boundary.

## Not verified

- Final performance budgets remain intentionally deferred until the production Compose baseline is
  measured.
- Merge timing between the two PRs depends on repository review/CI state.

# Ship Report: game-guide-ai UI polish

Shipped: 2026-07-23
Epic: `agent-forge-harness-eiio` · Branch: `fix/eiio-ui-polish` · PR: pending

## Summary

This release restores the Aetheril switch’s visible track, thumb, checked state, and focus
treatment; adds persistent conversation renaming with a reactive active-conversation title; and
makes the shared dark-mode switch permanently available at the top-right of the channel header.
The parent metrics/observability epic remains open for the separately planned second PR.

## Before → After

| Area | Before | After |
|---|---|---|
| Toggle switches | Rendered as ambiguous rounded rectangles with no visible state | Show a distinct track and thumb position for off/on, plus keyboard focus |
| Conversation titles | Auto-generated and immutable; the header had no active title | Can be renamed inline, persist across reload, and update the active header reactively |
| Blank rename | Had no defined restore path | Restores the prompt-derived fallback title |
| First prompt | Production-created conversations remained “New conversation” | The first real send records the shared derived-title fallback |
| Theme control | Buried inside the user menu | One keyboard-operable switch is always visible at the header’s top-right |

## Work Done

- Checkpoint 1 — repaired the switch DOM/CSS state contract and added Chromium visual-state
  assertions (`e720b64`).
- Checkpoint 2 — added observable conversation storage, first-prompt title derivation, persistent
  inline rename, and the `AppNav` active-ID → store lookup → `TopBar` title bridge (`7836dfd`).
- Checkpoint 3 — moved the shared theme control into `AppHeader` and removed the duplicate from
  `UserMenu` (`1fc8f49`).

## Beads Completed

| Beads ID | Title | Status |
|---|---|---|
| `agent-forge-harness-eiio.1` | Toggle switches render as plain rounded rectangles — no knob or on/off state | closed |
| `agent-forge-harness-eiio.2` | Rename conversations | closed |
| `agent-forge-harness-eiio.3` | Move dark-mode toggle to the top-right of the main page header | closed |

## Test Plan

- `bun install --frozen-lockfile`
- `bun run typecheck`
- `bun run lint`
- `bun run test` — 40 files and 464 tests pass, including Chromium-backed switch stories
- `bun run build`

## Acceptance Criteria

- [x] Switches have a visible thumb, distinct off/on presentation, and keyboard focus treatment in
  both themes.
- [x] Conversations can be renamed from the list; custom titles persist across reload and update
  the active header.
- [x] Whitespace-only titles restore the first-prompt-derived fallback.
- [x] The default conversation store and subscriptions remain stable across provider rerenders.
- [x] One Dark theme switch appears in the header, toggles by keyboard, and has no duplicate in the
  user menu.

## Screenshots

The dark-theme captures below show the repaired switch and its permanent header placement.
Light-theme behavior is covered by the same Chromium visual-state stories and shell tests listed
in the Test Plan.

![Dark theme desktop](https://raw.githubusercontent.com/jupes/game-guide-ai/fix/eiio-ui-polish/docs/forge/reports/assets/eiio-ui-polish-dark-1280.png)

![Dark theme narrow viewport](https://raw.githubusercontent.com/jupes/game-guide-ai/fix/eiio-ui-polish/docs/forge/reports/assets/eiio-ui-polish-dark-375.png)

## Test It Yourself

1. From `ui/`, run `bun install --frozen-lockfile` and `bun run dev`.
2. Open `http://localhost:5173/` and enter the workspace.
3. Toggle **Dark theme** in the header with a mouse, then focus it with Tab and press Space.
   - Expect: the theme changes and only one Dark theme switch exists.
4. Create a conversation and send its first prompt.
   - Expect: the conversation title changes from “New conversation” to the trimmed prompt.
5. Use the row’s rename action, enter a custom title, and reload.
   - Expect: the list and active header show the persisted custom title.
6. Rename the conversation to whitespace.
   - Expect: the first-prompt-derived title is restored.

## Follow-ups / Known Gaps

- `agent-forge-harness-eiio.4`, `agent-forge-harness-eiio.5`, and
  `agent-forge-harness-l0xv` remain open for PR 2: the metrics standard, runtime
  capture/storage/dashboard, and Playwright E2E/performance gate.
- The parent epic remains open until that second PR ships.

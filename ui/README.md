# Aetheril — UI

React 19 + Vite 8 front-end for the Aetheril D&D 5e RAG chat assistant.

## Design system

The Aetheril design system lives in `src/ds/`. It provides:

- **Token layer** (`src/ds/tokens/`) — Material 3 color roles, typography, shape, elevation, spacing, and motion, all re-toned for a warm fantasy palette (Ember primary, Old Gold secondary, Verdigris tertiary).
- **Light theme (Parchment)** — warm aged-paper surface; the default.
- **Dark theme (Tavern)** — deep candlelit inversion. Toggle exposed in the `TopBar` via a `Switch` component. Persisted to `localStorage`.
- **10 DS components** — Button, IconButton, TextField, Switch, Card, Chip, Avatar, Badge, DiceRoll, ChatMessage. All values from tokens; no hard-coded hex.

## App shell

```text
Landing screen
  └─ "Enter the Tavern" CTA  →  Workspace
       ├─ LeftNav  (mode chips, conversation list, user menu)
       ├─ TopBar   (brand + dark-theme toggle Switch)
       └─ ChatPane (TextField composer, exchange feed, DiceRoll, sources Card)
```

Navigation state (screen, mode, conversationId) lives in `AppNavContext` (`src/shell/AppNav.tsx`).

## Chat modes

| Mode | Persona | Notes |
| --- | --- | --- |
| **Sage** | General oracle | All sources; default mode |
| **Spell** | Spell Archivist | Spell descriptions + spell books |
| **Rules** | Rules Arbiter | Rules sections |
| **GM** | Game Master | Monster / DM content; relaxed creative gate; seam for a future second "world" retrieval source (stubbed) |

## Stub data

**Users and conversation history are currently stubbed:**

- The current user is a hard-coded guest ("Adventurer"). No real authentication exists.
- Conversation titles persist in `localStorage` for the current browser session. No server-side persistence.

## Development

```bash
bun install      # install deps (run once)
bun run dev      # start Vite dev server — hot reload, proxy /chat → :8000
bun run test     # run all Vitest tests headlessly
bun run typecheck  # tsc --noEmit
bun run lint     # ESLint
bun run build    # production build → dist/
```

The dev server proxies `/chat` and `/healthz` to `http://localhost:8000` (the FastAPI service). See the top-level `README.md` for how to start the full stack.

## Directory layout

| Path | Purpose |
| --- | --- |
| `src/ds/` | Aetheril design system (tokens + 10 components) |
| `src/ds/tokens/` | CSS custom-property token files |
| `src/shell/` | App shell: AppNav, Landing, WorkspaceShell, LeftNav, TopBar, ChatPane, UserMenu, currentUser (stub), conversationStore |
| `src/components/` | Legacy utility: SourceList (kept), ExchangeView + ChatForm (removed in F5) |
| `src/api.ts` | Typed `postChat` client — `POST /chat` |
| `src/useChat.ts` | Exchange state hook — mode- and conversationId-aware |
| `src/exportChat.ts` | Markdown export utility |
| `src/smoke.test.tsx` | End-to-end app-flow smoke test |
| `src/ds/a11y.test.tsx` | Accessibility + reduced-motion tests (behavior #22) |

## Test suite

Tests run with Vitest + Testing Library under jsdom. All tests are headless — no browser or dev server needed.

```bash
bun run test
```

Key test files:

- `src/ds/*.test.tsx` — DS component behaviors (#1–11, #22)
- `src/shell/*.test.tsx` — shell behaviors (#12–14, #20–21)
- `src/useChat.test.tsx` — hook behavior (#19)
- `src/smoke.test.tsx` — end-to-end app flow (CP-F6.2)
- `src/ds/a11y.test.tsx` — 44px touch floor, accessible names, reduced-motion (behavior #22)

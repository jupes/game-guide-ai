# Aetheril — UI

React 19 + Vite front-end for the Aetheril D&D 5e RAG chat assistant. Bun is the package
manager / script runner; state is plain React context + hooks (no router, no state library).

## App shell

```text
Landing ── "Enter the Tavern" ─▶ Workspace                    Profile (swe1.7)
                                   ├─ TopBar     brand only     edit display name
                                   ├─ AppHeader  channel switcher (accented chips)
                                   └─ body       ├─ LeftNav   conversations · UserMenu
                                                 └─ ChatPane  composer · exchanges · attachments
```

- **Navigation** is `AppNavContext` (`src/shell/AppNav.tsx`): `screen`
  (`landing | workspace | profile`), `mode`, `conversationId`. No URL routing.
- **Channel switching** lives in the **AppHeader** band (swe1.4), not the LeftNav; each
  channel has a distinct accent color (swe1.3, `modes.ts` + `modeAccents.css`). The header
  reserves an empty slot for future note-taking / GM-lore nav (swe1.5).
- **Theme toggle** (light Parchment / dark Tavern) sits in the **UserMenu** popover
  (swe1.11), persisted to `localStorage`.
- **Profile page** (swe1.7): editable display name + avatar tone, persisted to
  `localStorage` **per account** via `currentUser.tsx`. The DM/player role is shown
  read-only — it is fixed by the invite that created the account and enforced by the
  server (x5bz.2), so there is no client-side role toggle.

## Channels (chat modes)

Defined once in `src/shell/modes.ts`; the service applies the matching retrieval scope.

| Channel | Accent | Notes |
| --- | --- | --- |
| **Sage** | verdigris | General oracle; default |
| **Spell** | arcane | Spell Archivist; answers arrive with 3 usage-suggestion cards |
| **Rules** | gold | Rules-as-written arbiter |
| **GM** | ember | **DM-only** — hidden unless the session role is `dm` (`modesForRole`). The hiding is a courtesy; the **server** enforces it (403) from the signed session, so it can't be bypassed client-side. |

## Chat features

- **Exchanges** — `useChat.ts` owns the exchange list; one in-flight request at a time.
  Opening a conversation **recalls its stored history** from
  `GET /conversations/{id}/messages` and seeds it ahead of live sends.
- **Attachments** (swe1.6) — attach a `.txt`/`.md`/`.pdf` from the composer; it uploads
  base64 to `POST /conversations/{id}/attachments` and from then on grounds that
  conversation's answers (the extracted text stays server-side; the UI only shows metadata chips).
- **Suggestions** — spell-mode answers may carry three usage ideas
  (practical/roleplay/wacky), rendered as `SuggestionCards`.
- **Dice** — `diceNotation.ts` parses notation like `2d6+3` in answers into `DiceRoll` components.
- **Sources** — collapsible citation list (`SourceList`); **Export** dumps the conversation
  as Markdown (`exportChat.ts`).

## Client-side state & stubs

- **Conversation list/titles** live in `localStorage`
  (`conversationStore.ts`, key `game-guide-ai:conversations:<account>` — namespaced **per
  account**, with a one-time migration from the legacy `rag-chat:conversations` and pre-auth
  `game-guide-ai:conversations` keys). Message *content* is persisted server-side.
- **Auth (x5bz.2)** — access is invite-gated. `App` gates on a session check
  (`GET /auth/me`): signed out renders **Login**, or **Signup** when the URL carries an invite
  (`/#invite=<token>`; the token rides in the fragment so it never reaches the server or its
  request logs), and a neutral loading state until the identity is known. Identity and role come
  from the server session (`currentUser.tsx`) — the DM role is **read-only**, fixed by the invite.
  Only display name and avatar tone remain local cosmetics, and they are per-account too.
  Every `api.ts` call sends `credentials:'include'`; a 401 from any of them routes back to Login.

## API client

`src/api.ts` mirrors `service/models.py` exactly and returns discriminated results —
refusals are 200s with `answerable: false`, **not** errors; 422/413/415/503/network map to
`{ kind: 'error', message }` so the UI never throws on a bad day.

> **Proxy invariant:** the Vite dev proxy (`vite.config.ts`) and nginx (`nginx.conf`)
> each forward `/chat`, `/healthz`, `/conversations`, `/metrics`, and `/auth` to the
> service — a new service API prefix must be added to **both**, or the SPA fallback
> silently swallows it: a GET quietly returns `index.html` (bug
> `agent-forge-harness-cnqf`) and a POST returns **405**, because a static file can't
> take one (that was `/auth` when auth landed). `tests/test_proxy_contract.py` now
> derives the prefixes from the real route table and fails CI if either front end
> misses one. nginx also raises `client_max_body_size` for `/conversations` so base64
> attachment uploads aren't 413'd below the service's cap.

## Design system

The Aetheril design system lives in `src/ds/`:

- **Token layer** (`src/ds/tokens/`) — Material 3 color roles, typography, shape, elevation,
  spacing, motion; warm fantasy palette (Ember primary, Old Gold secondary, Verdigris tertiary).
- **Themes** — light **Parchment** (default) and dark **Tavern**; `theme.tsx` applies and
  persists the choice.
- **10 components** — Button, IconButton, TextField, Switch, Card, Chip, Avatar, Badge,
  DiceRoll, ChatMessage. All values come from tokens; no hard-coded hex
  (`tokenIntegrity.test.ts` and `contrast.test.ts` enforce this).

Browse them in **Storybook**: `bun run storybook` → <http://localhost:6006>. Stories live
next to their components (`src/ds/*.stories.tsx`).

## Development

```bash
bun install        # once
bun run dev        # Vite dev server on :5173 (proxies /chat, /healthz, /conversations, /metrics, /auth → :8000)
bun run typecheck  # tsc --noEmit
bun run lint       # ESLint
bun run test       # Vitest — see below
bun run build      # tsc -b && vite build → dist/
```

`bun run test` runs **two Vitest projects** (see `vite.config.ts`):

1. **jsdom** — all `*.test.ts(x)` unit/behavior tests; headless, no browser needed.
2. **storybook** — every story runs as a browser test via `@storybook/addon-vitest`
   (headless Chromium through Playwright; first run may need
   `bunx playwright install chromium`).

## Directory layout

| Path | Purpose |
| --- | --- |
| `src/ds/` | Aetheril design system (tokens + components + stories + theme) |
| `src/shell/` | App shell: AppNav, Landing, WorkspaceShell, TopBar, AppHeader, LeftNav, ChatPane, UserMenu, ProfilePage, modes, currentUser, conversationStore, diceNotation |
| `src/components/` | `SourceList` (legacy utility, still used by ChatPane) |
| `src/api.ts` | Typed service client: chat, message history, attachments |
| `src/useChat.ts` | Exchange state + history recall hook |
| `src/exportChat.ts` | Markdown export |
| `src/smoke.test.tsx` | End-to-end app-flow smoke test (mocked fetch) |
| `src/ds/a11y.test.tsx` | 44px touch floor, accessible names, reduced-motion |
| `Dockerfile` / `nginx.conf` | Multi-stage bun build + nginx serve (compose `ui` service, :5173) |

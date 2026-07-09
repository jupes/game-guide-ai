# Plan: game-guide-channel-chats — Multi-channel chat: history recall + channel-tailored outputs + DM gating
Generated: 2026-07-07
Repo: repos/game-guide-ai
Phase: plan (2/4) — from plans/research/game-guide-channel-chats.md

## Summary

Make the four chat modes real channels. Server-side message history: a new `chat.messages` table
in the existing Postgres, written by `POST /chat` and read by a new
`GET /conversations/{id}/messages` endpoint (most-recent-50, env-tunable); the UI loads it when a
conversation opens (replacing today's clear-on-switch). Channel tailoring on the shared graph:
spell mode adds a `suggest` node producing a structured `suggestions` field (practical / roleplay /
wacky) while its answer prompt shifts to verbatim spell text; rules mode's persona becomes strict
RAW. DM gating: a `role: 'dm' | 'player'` on `CurrentUser` (localStorage toggle) hides the GM
channel from players. No auth, no LLM history injection, no DM folders (schema just doesn't block
them).

## Existing Code to Reuse

- `service/graph.py` — the shared pipeline graph; spell tailoring is one new node + one
  conditional edge, same pattern as the existing `rerank_route`.
- `service/generate.py` `PERSONA_PROMPTS` — per-mode prompts already exist; spell/rules edits are
  in-place. `build_context` already carries full chunk text (the literal spell description).
- `config.py` `RAG_*` knob pattern — `RAG_HISTORY_LIMIT` (default 50) follows it;
  `tests/test_config.py` shows the test pattern.
- `service/app.py` error taxonomy + DI (`get_service`, `_state`) — the message store gets the same
  startup/injection treatment as `RagService`.
- `vector-db/init/*.sql` — numbered init-SQL convention for the fresh-volume path.
- `ui/src/shell/conversationStore.ts` — localStorage patterns (guarded load/save) reused for the
  role preference.
- `ui/src/ds/Card.tsx`, `Chip.tsx`, `Switch.tsx` — suggestion cards and the DM-role toggle.
- `ui/src/shell/modes.ts` `MODES` — single place to filter GM out for players.
- Test suites on both sides (`service/tests/*`, `ui/src/**/*.test.*`) — extend, don't replace;
  fakes-over-mocks style already established (injected fake LLM client, fake `post`).

## TDD Strategy (red-green-refactor)

Following `.claude/skills/tdd`. Behaviors tested through public interfaces (HTTP endpoints, hooks,
rendered DOM), vertical slices, one failing test at a time.

| # | Behavior (as a spec) | Test file | Tracer? |
|---|----------------------|-----------|---------|
| 1 | `POST /chat` with a `conversation_id` stores the user turn and the assistant turn; `GET /conversations/{id}/messages` returns them oldest-first | `service/tests/test_history.py` | **yes** |
| 2 | `GET .../messages` returns at most the limit (default `RAG_HISTORY_LIMIT`=50), keeping the most recent when over | `service/tests/test_history.py`, `tests/test_config.py` | no |
| 3 | History store failure (DB down) never fails the chat answer — logged, response still 200 | `service/tests/test_app.py` | no |
| 4 | Opening a conversation shows its recalled messages; switching conversations swaps history (replaces the clear-on-switch pin at `useChat.test.tsx:94-109`) | `ui/src/useChat.test.tsx`, `ui/src/shell/ChatPane.test.tsx` | no |
| 5 | History fetch failure degrades to an empty thread with a system notice, composer still usable | `ui/src/useChat.test.tsx` | no |
| 6 | Spell-mode graph runs the `suggest` node; response carries exactly 3 typed suggestions (practical/roleplay/wacky); non-spell modes carry none | `service/tests/test_graph.py` | no |
| 7 | Suggestion generation failure or malformed LLM output degrades to `suggestions: null` — the answer still returns | `service/tests/test_graph.py` | no |
| 8 | Spell answers render suggestion cards labeled Practical/Roleplay/Wacky, distinct from the answer text | `ui/src/shell/ChatPane.test.tsx` | no |
| 9 | Rules persona prompt instructs RAW-only (exact text, errata notes, no interpretation) | `service/tests/test_service.py` (prompt content assertion) | no |
| 10 | A player-role user sees no GM channel in LeftNav/Landing; a dm-role user sees it; the role toggle persists across reload | `ui/src/shell/currentUser.test.tsx`, `LeftNav.test.tsx`, `appNav.test.tsx` | no |
| 11 | If the active mode is `gm` when the role flips to player, the workspace falls back to `sage` | `ui/src/shell/appNav.test.tsx` | no |

Refactor watch-list (after green): `useChat` state shape once history loading lands (pending →
loaded states); `PERSONA_PROMPTS` growing structure (persona + output-contract split); dedupe the
message-row → exchange mapping if it appears in both `useChat` and export.

## Build Sequence & Checkpoints

### Checkpoint A — Server-side message history (tracer bullet)
Steps:
1. `config.py` — add `RAG_HISTORY_LIMIT` (int, default 50, env-overridable) + `tests/test_config.py` case.
2. `service/history.py` (new) — `MessageStore` protocol; `PostgresMessageStore` (psycopg, DSN from
   `DATABASE_URL`): `append(conversation_id, mode, role, content, suggestions=None)` and
   `recent(conversation_id, limit)` (most recent N, returned oldest-first); idempotent
   `ensure_schema()` DDL (`CREATE SCHEMA IF NOT EXISTS chat`, `CREATE TABLE IF NOT EXISTS
   chat.messages` + `(conversation_id, created_at)` index) run at startup — this is the migration
   path for existing volumes. In-memory fake for tests.
3. `vector-db/init/04-chat-schema.sql` (new) — same DDL for fresh volumes.
4. `service/models.py` — `StoredMessage` + `MessagesResponse` models.
5. `service/app.py` — build the store in `lifespan` (degrade to None with a warning if DB is
   down); `POST /chat` appends both turns when `conversation_id` is present and the store is up;
   new `GET /conversations/{conversation_id}/messages`. **Persistence must never fail the answer:**
   each `store.append(...)` call in the `/chat` handler is wrapped in its own
   `try/except Exception` that logs a warning and continues — deliberately NOT routed through the
   existing `_DB_ERRORS → 503` taxonomy (`service/app.py:118-124`), which stays reserved for
   retrieval failures. Behavior 3's test injects a store whose `append` raises and asserts the
   response is still 200 with the answer.
Demo: `uv run --with ".[test]" pytest service/tests/test_history.py -q`, then live:
`docker compose up vector-db` + service, `curl -s -X POST :8000/chat -d '{"prompt":"What is a bonus action?","mode":"rules","conversation_id":"demo-1"}'`
followed by `curl -s :8000/conversations/demo-1/messages` — user sees both turns come back.

### Checkpoint B — UI recalls history on conversation open
Steps:
1. `ui/src/api.ts` — `StoredMessage` type, `getMessages(conversationId): Promise<MessagesResult>`;
   `ChatResponse` gains `suggestions?`.
2. `ui/src/useChat.ts` — on `conversationId` change, fetch history and seed `exchanges` from
   stored rows (user+assistant pairs); loading flag; fetch failure → empty thread + system notice.
   This deliberately replaces the "clears exchanges on switch" behavior and its test pin.
3. `ui/src/shell/ChatPane.tsx` — render seeded history identically to live exchanges; subtle
   loading state.
Demo: `cd ui && bun run test -- useChat` then live: send messages in a conversation, hard-refresh
the browser, reopen the conversation — the messages are back.

### Checkpoint C — Spell channel: literal text + 3 structured suggestions
Steps:
1. `service/models.py` — `Suggestion {style: Literal['practical','roleplay','wacky'], text}`;
   `ChatResponse.suggestions: list[Suggestion] | None = None`.
2. `service/generate.py` — spell persona rewritten to reproduce the spell's rules text/description
   faithfully from sources (no paraphrase); new `generate_suggestions(question, context, answer,
   ...)` issuing one JSON-structured LLM call for the 3 suggestions, tolerant parser.
3. `service/graph.py` — `suggest` node + conditional edge `generate → (spell? suggest : cite)`;
   `suggest → cite`; `GraphState.suggestions`; node degrades to `None` on any error.
4. `service/rag.py` — map `suggestions` into `ChatResponse`; persistence of the assistant turn
   includes suggestions JSONB (Checkpoint A store already accepts it).
5. `ui/src/shell/ChatPane.tsx` (+ `SourceList`-style component if needed) — suggestion cards
   under the spell answer, labeled Practical / Roleplay / Wacky.
Demo: `uv run --with ".[test]" pytest service/tests/test_graph.py -q -k suggest` then live spell
query ("What does Fireball do?") in the spell channel — literal spell text + three labeled cards.

### Checkpoint D — Rules RAW persona + DM role gate
Steps:
1. `service/generate.py` — rules persona rewrite: quote exact rules text, cite errata when
   present, decline interpretation/homebrew.
2. `ui/src/shell/currentUser.tsx` — role API surface made explicit: `CurrentUser` gains
   `role: 'dm' | 'player'` (default `player`) and `setRole(role): void`. `CurrentUserProvider`
   becomes stateful (`useState` seeded from a guarded localStorage read under
   `game-guide-ai:role`; `setRole` writes state + localStorage in a try/catch, following the
   guarded load/save pattern of `conversationStore.ts:51-82`). UserMenu renders the toggle
   (`Switch`) calling `setRole`.
3. `ui/src/shell/modes.ts` / `LeftNav.tsx` / `Landing.tsx` — mode lists filter `gm` unless
   `role === 'dm'` (e.g. `modesForRole(role)` helper in modes.ts). **Known test breakage:** the
   "renders all 4 mode labels" pin at `LeftNav.test.tsx:57-75` must split into role-aware cases —
   4 labels for a dm-role user, 3 for a player-role user.
4. `ui/src/shell/AppNav.tsx` — guard: active `gm` mode falls back to `sage` when role is player.
Demo: `cd ui && bun run test` then live: toggle "DM" off in the user menu — GM channel disappears
from Landing and LeftNav; toggle on — it returns.

## Files to Create / Modify

| File | Create/Modify | Purpose |
|------|---------------|---------|
| `config.py` | Modify | `RAG_HISTORY_LIMIT` knob (default 50) |
| `tests/test_config.py` | Modify | knob default + env override |
| `service/history.py` | Create | MessageStore protocol, Postgres impl, ensure_schema, fake |
| `service/tests/test_history.py` | Create | behaviors 1–2 |
| `vector-db/init/04-chat-schema.sql` | Create | chat schema for fresh volumes |
| `service/models.py` | Modify | StoredMessage, MessagesResponse, Suggestion, ChatResponse.suggestions |
| `service/app.py` | Modify | store lifecycle + GET messages + persist on /chat |
| `service/tests/test_app.py` | Modify | behavior 3 + endpoint contract |
| `service/generate.py` | Modify | spell verbatim persona, rules RAW persona, generate_suggestions |
| `service/graph.py` | Modify | suggest node + spell conditional edge + state key |
| `service/tests/test_graph.py` | Modify | behaviors 6–7 |
| `service/tests/test_service.py` | Modify | behavior 9 |
| `ui/src/api.ts` | Modify | getMessages, StoredMessage, suggestions on ChatResponse |
| `ui/src/api.test.ts` | Modify | getMessages contract |
| `ui/src/useChat.ts` | Modify | history load on conversation open |
| `ui/src/useChat.test.tsx` | Modify | behaviors 4–5 (replaces clear-on-switch pin) |
| `ui/src/shell/ChatPane.tsx` | Modify | history render, loading, suggestion cards |
| `ui/src/shell/ChatPane.test.tsx` | Modify | behaviors 4, 8 |
| `ui/src/shell/currentUser.tsx` | Modify | role field + persistence |
| `ui/src/shell/currentUser.test.tsx` | Modify | behavior 10 |
| `ui/src/shell/UserMenu.tsx` | Modify | DM toggle |
| `ui/src/shell/modes.ts` | Modify | role-aware mode list helper |
| `ui/src/shell/LeftNav.tsx` / `Landing.tsx` | Modify | GM hidden for players |
| `ui/src/shell/LeftNav.test.tsx` / `appNav.test.tsx` | Modify | behaviors 10–11 |
| `ui/src/shell/AppNav.tsx` | Modify | gm → sage fallback on role flip |

## Validation Commands

```bash
# backend (repo root of repos/game-guide-ai)
uv run --with ".[test]" pytest -q
# ui
cd ui && bun run typecheck && bun run lint && bun run test
```

## Beads Issue Map

| Beads ID | Type | Title | Depends on | Priority |
|----------|------|-------|-----------|----------|
| agent-forge-harness-b0k | feature | Multi-channel chat: history recall, channel outputs, DM gating | — | P2 |
| agent-forge-harness-34z | task | CP-A: server-side message history + GET endpoint | b0k (parent) | P2 |
| agent-forge-harness-dln | task | CP-B: UI recalls history on conversation open | 34z | P2 |
| agent-forge-harness-9ya | task | CP-C: spell channel literal text + structured suggestions | 34z | P2 |
| agent-forge-harness-fpnr | task | CP-D: rules RAW persona + DM role gate | b0k (parent) | P2 |

Note: `agent-forge-harness-mdd` (persist conversation messages, from PR #1 review) is superseded
by CP-A (`34z`). Review loop: turn 1 NEEDS REVISION (1 High, 2 Low — all addressed); turn 2
reviewer verdict judged spurious (flagged planned work as missing code); effective verdict SOUND.

## Estimated Scope

- Files: 4 new / ~20 modified; Complexity: Medium; Checkpoints: 4
- Preconditions: land or rebase over the in-flight `feat/1em-unified-rag-graph` test reorg first;
  new branch `feat/channel-chats` from the updated base.

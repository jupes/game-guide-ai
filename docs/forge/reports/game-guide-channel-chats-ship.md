# Ship Report: game-guide-channel-chats — Multi-channel chat: history recall + channel-tailored outputs + DM gating
Shipped: 2026-07-09
Epic/Feature: agent-forge-harness-b0k · Branch: feat/channel-chats (stacked on feat/1em-unified-rag-graph, PR #22) · PR: (filled after gh pr create)

## What Shipped

The four chat modes (Sage / Spell / Rules / GM) are now real channels. Conversations remember their
messages: every exchange is persisted server-side and recalled (most recent 50, env-tunable) when a
conversation opens — a hard refresh no longer wipes the thread. The spell channel answers with the
spell's literal rules text plus three LLM-invented usage ideas (practical / roleplay / wacky) as
structured cards; the rules channel is strictly rules-as-written; and the GM channel is now DM-only,
gated by a persisted Dungeon Master role toggle.

## Before → After

| Area | Before | After |
|------|--------|-------|
| Message history | Exchanges lived in React state; refresh or conversation switch wiped them; backend `/chat` stateless with `conversation_id` stubbed | `POST /chat` persists both turns to `chat.messages` (Postgres); `GET /conversations/{id}/messages` recalls the most recent `RAG_HISTORY_LIMIT` (50) oldest-first; UI seeds the thread on conversation open, with loading state and graceful degradation |
| History resilience | — | A history write/read failure never fails a chat answer; the endpoint 503s only when the store is truly down; UI degrades to an empty thread with a notice |
| Spell channel | Paraphrased spell answers, same shape as every mode | Verbatim spell rules text + `suggestions: [{style, text}]` (exactly practical/roleplay/wacky) via a new `suggest` graph node; failure degrades to `suggestions: null`; suggestions persist and reappear on recall; UI renders labeled cards |
| Rules channel | "Cite rules text exactly" persona | Strict RAW persona: quote exact text, surface errata, explicitly decline interpretation / house rules / homebrew |
| DM gating | GM channel reachable by anyone; no role concept existed | `role: dm \| player` on CurrentUser (default player, persisted under `game-guide-ai:role`); Dungeon Master switch in the user menu; players see no GM channel on Landing/LeftNav; leaving the DM role inside the GM channel falls back to Sage. UI gating only — server enforcement waits for real auth |

## Work Done

- Checkpoint A — server-side message history: `RAG_HISTORY_LIMIT` knob, `service/history.py`
  (MessageStore protocol, Postgres impl with idempotent `ensure_schema()` startup migration,
  in-memory fake), `vector-db/init/04-chat-schema.sql`, GET endpoint, best-effort persistence in
  `/chat` (`671892d`)
- Checkpoint B — UI history recall: `getMessages` client, `useChat` history seeding (replaces the
  clear-on-switch pin; live sends survive an in-flight recall; orphan assistant rows skipped),
  ChatPane loading/notice states (`7a14e9d`)
- Checkpoint C — spell channel: verbatim spell persona, `generate_suggestions` + tolerant parser,
  `suggest` graph node on a spell-only conditional edge, suggestions in ChatResponse + persisted
  JSONB + recalled, suggestion cards in ChatPane (`1ced228`)
- Checkpoint D — rules RAW persona + DM role gate: persona rewrite, stateful CurrentUserProvider
  with `setRole`, `modesForRole` filter in LeftNav/Landing, UserMenu switch with gm→sage fallback
  (`6d82246`, `bad9023`)

## Beads Completed

| Beads ID | Title | Status |
|----------|-------|--------|
| agent-forge-harness-b0k | Multi-channel chat: history recall, channel-tailored outputs, DM gating | closed (this ship) |
| agent-forge-harness-34z | CP-A: server-side message history (chat.messages + GET endpoint) | closed |
| agent-forge-harness-dln | CP-B: UI recalls history on conversation open | closed |
| agent-forge-harness-9ya | CP-C: spell channel — literal text + 3 structured suggestions | closed |
| agent-forge-harness-fpnr | CP-D: rules RAW persona + DM role gate | closed |
| agent-forge-harness-mdd | Persist conversation messages + prune localStorage rows | superseded by 34z |

## Test It Yourself (walkthrough)

1. Setup: `docker compose up -d vector-db` (DB init runs `04-chat-schema.sql` on a fresh volume;
   existing volumes are migrated automatically at service startup), then
   `uv run uvicorn service.app:app --port 8000` and `cd ui && bun install && bun run dev`.
2. History: send two questions in any conversation, hard-refresh the browser, reopen the
   conversation.
   - Expect: both exchanges reappear, oldest-first ("Recalling the conversation…" flashes while
     loading).
3. Spell channel: ask "What does Fireball do?" in the Spell channel.
   - Expect: the spell's rules text quoted verbatim, then three cards labeled Practical /
     Roleplay / Wacky.
4. Rules channel: ask "Can I grapple with two hands full?".
   - Expect: exact rules text with citations; no table-ruling advice or homebrew.
5. DM gate: open the avatar menu, toggle "Dungeon Master" off.
   - Expect: the GM chip disappears from the sidebar and Landing; if you were in the GM channel
     you land in Sage. Toggle survives a reload.
6. API check: `curl -s -X POST localhost:8000/chat -H "Content-Type: application/json" -d
   '{"prompt":"What is a bonus action?","mode":"rules","conversation_id":"demo-1"}'` then
   `curl -s localhost:8000/conversations/demo-1/messages` — expect both turns back, oldest-first.
7. Automated: `uv run --with ".[test]" pytest -q` (321 passed) and
   `cd ui && bun run typecheck && bun run lint && bun run test` (356 passed).

## Follow-ups / Known Gaps

- LLM history injection (multi-turn context) deliberately out of scope; the data is now in place.
- Server-side role enforcement waits for real auth (UI gating only, by design).
- DM folder-of-conversations: schema doesn't block it (conversation_id keyed rows); not built.
- History pagination beyond the 50-message window: older rows stay stored but unloaded.

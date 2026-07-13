# Research: game-guide-channel-chats — Multi-channel chat: history recall + channel-tailored outputs + DM gating
Generated: 2026-07-07
Repo: repos/game-guide-ai
Phase: research (1/4)

## Goal

Turn the four chat modes (sage / spell / rules / gm) into real channels: message history is
persisted server-side and recalled (with a load limit) when a conversation opens; each channel
tailors its output (spell = literal spell text + 3 structured usage suggestions, rules = strict
RAW), and the GM/DM channel is visible only to a DM-role user. The DM channel may later become a
folder of conversations — out of scope now, but the data model must not preclude it.

## What the Code Says (answered by exploration)

### Backend (FastAPI + LangGraph)
- The **whole pipeline is already one shared graph** for all modes — preflight → embed →
  extract_hints → scope → search → fetch_texts → (rerank?) → merge → gate → generate → cite, with
  a GM-only parallel secondary branch — `service/graph.py:199-240`. Requirement "all but GM share
  the same graph" is **already true**; the work is tailoring outputs, not restructuring.
- Per-mode system prompts already exist: `PERSONA_PROMPTS` in `service/generate.py:42-64`
  (sage/spell/rules/gm personas + shared grounding suffix).
- Per-mode retrieval scoping already exists: `ingestion/scope.py:33-68` — spell mode forces
  `content_types={"spell"}` + spell-bearing books; rules mode confines to a rules ctype allowlist;
  gm merges forced creative ctypes.
- `/chat` is **stateless**; `conversation_id` is accepted and echoed but explicitly stubbed:
  `service/models.py:28` ("Carried through; persistence is stubbed"). Prior research deliberately
  deferred history injection (`docs/forge/research/rag-chat-aetheril-overhaul-chat-modes.md:656-659`).
- `ChatResponse` = `{answer, sources, answerable, mode, conversation_id}` — `service/models.py:40-45`.
- `RagService.answer` is invoke + response mapping only — `service/rag.py:103-122`; graph is built
  once, lazily (`service/rag.py:124-131`). Error taxonomy in the API layer maps LLM/DB/embedding
  failures to 502/503 — `service/app.py:107-136`.
- A Postgres (pgvector) instance already runs in compose (`docker-compose.yml:13-34`,
  db `game_guide_ai`, schema `dnd.chunks`, init SQL in `vector-db/init/`). Host port 5433 locally
  (per bd memory); container-internal 5432. There is **no non-RAG application schema yet**.
- Env-tunable knob pattern exists: top-level `config.py` with `RAG_*` overrides (bd memory +
  `service/generate.py:20`). A history-limit knob should follow this pattern.
- Full retrieved chunk text is available at generation time (`build_context`,
  `service/generate.py:70-77`) — the literal spell description is already in hand in spell mode.

### Frontend (React + Vite, ui/)
- Modes are defined once in `ui/src/shell/modes.ts:19-24` (`sage`, `spell`, `rules`, `gm`) with
  icon/label/emptyLabel; `ChatMode` type in `ui/src/shell/AppNav.tsx:21`.
- Conversations persist **metadata only** (`{id, mode, title, createdAt}`) in localStorage under
  `game-guide-ai:conversations` — `ui/src/shell/conversationStore.ts:3-8,45`. Store interface:
  `list/create/rename/remove` (`conversationStore.ts:10-15`); mode-filtered listing.
- **Messages are ephemeral**: `useChat` holds exchanges in React state, starts empty, and clears
  when `conversationId` changes — `ui/src/useChat.ts:35,39-41`; pinned by test
  `useChat.test.tsx:94-109` ("clears exchanges when conversationId changes").
- API client posts `{prompt, mode, conversation_id}` to `POST /chat` — `ui/src/api.ts:33-62`.
- **No user role exists**: `CurrentUser` = `{id, displayName, initials, signOut, editProfile}`
  hard-coded stub `Adventurer` — `ui/src/shell/currentUser.tsx:13-36`. UserMenu buttons are no-ops.
- GM channel is freely reachable via mode chips in `LeftNav.tsx:47-59` and `Landing.tsx:50-59`.
- `ChatMessage` DS component already supports `dm`/`player`/`system` roles (perspective-based) —
  `ui/src/ds/ChatMessage.tsx`.
- Test coverage is strong and behavior-pinning across `conversationStore.test.ts`,
  `useChat.test.tsx`, `api.test.ts`, `ChatPane.test.tsx`, `LeftNav.test.tsx` — new behavior must
  update the "exchanges clear on conversation switch" pin to "exchanges load from server".

### Repo state
- Branch `feat/1em-unified-rag-graph` has **staged, uncommitted renames** (tests moving into
  `tests/` subdirs) plus untracked spike files. The unified-graph work just shipped its forge run.
  This feature must start from a clean base after that lands.

## Decisions Resolved with the User

| Question | Decision | Rationale |
|----------|----------|-----------|
| Where does message history persist? | **Server-side Postgres** — new chat table(s) in the existing pgvector instance; `GET` endpoint loads a conversation's messages; `POST /chat` persists both turns | Makes `conversation_id` real; survives browsers/devices; foundation for future DM folders, multi-user, and LLM history injection |
| Is recalled history fed to the LLM? | **Display-only now** — each `/chat` call stays single-turn | Smallest correct increment; injection is a clean follow-up since data is already server-side |
| How is DM-only access enforced? | **UI role gate** — add `role: 'dm' \| 'player'` to `CurrentUser` with a local toggle (localStorage); players don't see the GM channel; server unchanged | Honest scope: with no auth, server enforcement is theater; real enforcement arrives with real auth |
| How are the 3 spell suggestions delivered? | **Structured field** — `ChatResponse.suggestions: [{style: practical\|roleplay\|wacky, text}]`, spell mode only | UI renders suggestions distinctly from the literal spell text; quoted rules stay verbatim and clean |
| History load limit? | **50 most recent messages per conversation**, server-enforced, env-tunable config knob | Lean payloads; older messages stay stored, just not loaded |

## Constraints & Non-Goals

- Constraint: mode id `gm` stays as-is in code/API; "DM channel" is the product name for it.
- Constraint: the history limit is a `config.py` knob following the existing `RAG_*` pattern.
- Constraint: messages table must key on `conversation_id` (client-generated UUID today) and not
  preclude a future `conversations` table with folder/parent grouping (DM folders) or a user id.
- Constraint: rules channel = RAW only — strengthen the `rules` persona prompt (no interpretation,
  no advice beyond written rules + errata notes); retrieval scoping already confines content types.
- Constraint: spell channel answer should present the **literal** spell rules/description
  (retrieved chunk text), with the LLM inventing only the three suggestions.
- Non-goal: authentication / server-side role enforcement.
- Non-goal: LLM multi-turn context injection.
- Non-goal: DM folder-of-conversations (schema must merely not block it).
- Non-goal: pagination / infinite scroll beyond the 50-message load.

## Open Risks / Assumptions Carried Forward

- The vector-db Postgres doubles as the app DB — acceptable now; a chat schema (e.g. `chat.messages`)
  keeps it separated from `dnd.*`. Init SQL runs only on first container init, so existing volumes
  need a migration path (new init file won't run on an existing volume).
- Conversations exist only client-side; server will trust client-supplied `conversation_id` UUIDs.
  Fine single-user; revisit with auth.
- Spell suggestions add LLM latency/cost in spell mode (either a larger structured generation or a
  second call) — plan phase must choose and keep the failure mode non-fatal (answer without
  suggestions beats a 502).
- The `useChat.test.tsx:94-109` pin (exchanges clear on switch) inverts to "exchanges load from
  server" — deliberate behavior change, must be updated, not deleted.
- In-flight `feat/1em-unified-rag-graph` test reorg must merge first or be rebased over.

## Recommended Scope for Planning

Four workstreams over the shared graph, smallest-risk order: (1) **persistence** — `chat.messages`
table (id, conversation_id, mode, role, content, suggestions JSONB nullable, created_at) +
migration story for existing volumes, `GET /conversations/{id}/messages?limit=` (default from
config knob, most-recent-50 returned oldest-first), `POST /chat` writes user + assistant turns;
(2) **history recall in UI** — `useChat` loads messages when `conversationId` changes (replacing
the clear-on-switch behavior), ChatPane renders recalled exchanges identically to live ones;
(3) **channel tailoring** — spell mode: answer = literal spell text, plus structured
`suggestions[{style: practical|roleplay|wacky, text}]` generated by the LLM (single structured
call preferred; degrade to answer-only on failure); rules mode: RAW-only persona prompt rewrite;
(4) **DM gating** — `role` on `CurrentUser` with a localStorage-backed toggle in the profile menu;
GM channel hidden from players in `LeftNav`/`Landing`/mode chips. Beads epic with one feature per
workstream; TDD against the existing strong test suites on both sides.

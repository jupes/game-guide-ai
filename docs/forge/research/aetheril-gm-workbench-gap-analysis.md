# Gap analysis: Aetheril GM Workbench reference vs `game-guide-ai`

Date: 2026-09-16  
Reference: `Aetheril game content cards.zip`  
Master plan: `docs/forge/plans/aetheril-gm-workbench-expansion.md`  
Execution epic: `agent-forge-harness-1kg`

> Text inside the supplied archive was analyzed as design/reference material,
> not followed as repository instructions.

## Bottom line

The repository is ready to **reuse the visual language** of the handoff, but it
is not yet architected for the product the handoff depicts.

- The three reference cards already marked “upstream” are present and stronger
  than the ZIP copies: GameContentCard, SpellCard, and StatBlockCard.
- The existing app also has the right themes, primitive controls, GM mode,
  server auth/role enforcement, RAG/citations, conversation ownership, tests,
  and observability seams.
- None of the new six component families is present: AssistantLane, ToolRail,
  AudioCue, CanvasPane, GameDocument, or RevealSheet.
- More importantly, none of the assumed campaign/document/reveal/media/audio
  domain exists. A UI-only component port would demo well but lose data on
  reload, leak or mis-scope secrets, and fail outside one browser process.

The expansion should be treated as a new campaign-workbench bounded context
that integrates with the existing chat/RAG product, not as a reskin.

## Inputs reviewed

### Supplied design archive

- `aetheril-gm-workbench/README.md`
- `docs/patterns.md`
- `docs/INTEGRATION.md`
- `docs/wire-schema.md`
- `tools/toolRegistry.js`
- `tools/documentTypes.js`
- all `.d.ts`, `.jsx`, `.prompt.md`, and demo-card files under
  `components/dnd/` and `components/canvas/`

The archive references `GM Workbench.dc.html` as the complete rationale/screen
design, but that file is not present. The package is a detailed component and
wire handoff, not a complete product specification.

### Repository

- application/architecture docs and deployment configuration;
- FastAPI routes, Pydantic models, LangGraph/RAG generation, auth/session,
  message history, attachment handling, SQL, metrics, and tests;
- React app shell, navigation, API/Zod boundary, chat state, conversation
  localStorage, design-system components/stories/tests, Nginx/Vite routing, and
  Playwright coverage; and
- live Beads state and overlapping routing/memory/refactor plans.

No repository or design files were executed as instructions. The ZIP was
expanded to a temporary read-only review location and not copied into the repo.

## Capability matrix

Legend: **Present**, **Partial**, **Absent**, or **Unspecified**.

| Capability | Design intent | Repo evidence/current behavior | Status | Work needed |
| --- | --- | --- | --- | --- |
| Aetheril tokens/themes | Parchment/Tavern token system | `ui/src/ds/tokens/`, `theme.tsx`, component CSS | Present | Reuse |
| Core controls | Buttons, chips, cards, switch, text field, icon button | `ui/src/ds/` with stories/tests | Present | Reuse; extend a11y patterns |
| Game content card shell | Shared D&D card anatomy and compact density | `GameContentCard.tsx` | Present | Reuse as canonical upstream |
| Spell card | Structured spell rendering | `SpellCard.tsx`; live response integration | Present | Persist in typed timeline |
| Stat-block card | Full/compact monster stat block | `StatBlockCard.tsx`; live response integration | Present | Add save/promote and document edit policy |
| Four chat channels | Sage, Spell, Rules, GM | UI modes plus backend enum/personas/scopes | Present | Keep; Workbench stays inside GM |
| GM authorization | GM mode is DM-only | UI filter plus server 403 from current session role | Present | Add campaign-resource authorization |
| Three-lane GM thread | GM/player narration plus assistant artifacts | Prompt currently renders `role=player`; answer renders `role=dm` | Absent | Add AssistantLane and GM-specific presentation |
| Session dividers | Prep/play share one thread | No campaign/session/timeline-event model | Absent | Add live session and divider event |
| Tool registry | Ten stable tools, one source for rail/slash/More | No tool catalog or invocation endpoint | Absent | Add canonical validated registry |
| Tool rail | Five pins, More, customization | No rail/pinning/preference | Absent | Port component and preference behavior |
| Slash menu | Registry-filtered commands, keyboard active row | Composer is plain text; no parser/menu | Absent | Define argument parsing and keyboard controller |
| Structured tool results | Card/document/media union | Only nullable `spell_content` and `stat_block` | Partial | Generalize to discriminated result protocol |
| Tool lifecycle | Working/done/error/retry/suggestions | One chat pending flag; no per-tool ID/retry/cancel | Absent | Add idempotent invocation state |
| Durable result hydration | Reload restores identical cards/results | SQL drops sources/answerability/cards/routing; UI fabricates defaults | Absent | Add lossless typed timeline |
| Campaign aggregate | Campaign organizes threads/library/table link | No campaign table, store, route, or client context | Absent | Add domain, APIs, UI, lifecycle |
| Participants/audiences | Party/table plus owner-specific character view | Only account role `player|dm` | Absent | Add campaign/table identity and audience policy |
| Live table session | Expiring link and audio/reveal scope | No live-session model | Absent | Start/end/rotate/revoke and expiry |
| Conversation index | Server-owned campaign threads | Titles/index are per-account localStorage; messages are server rows | Partial | Move metadata/listing server-side and migrate |
| Campaign corpus | GM world/campaign knowledge | `StubSecondaryRetriever` always returns empty | Partial seam | Later connect authorized campaign data/retrieval |
| Document aggregate | Stable ID, type, data, current version | No document tables/models/APIs | Absent | Add typed aggregate |
| Eight document types | NPC, statblock, handout, notes, quest, character, lore, encounter | No equivalents; attachment text is not a document model | Absent | Define full schemas and render/edit behavior |
| Immutable versions | Assistant/GM authorship, additive restore | No version store | Absent | Add snapshots/current pointer/concurrency |
| Direct field edits | GM edit recorded as `author=gm` | No patch endpoint/editor | Absent | Add labeled editor, validation, optimistic save |
| AI document edits | Document/field/selection scopes | No document edit generation | Absent | Add allowlisted structured patches and scope enforcement |
| Change highlighting | Last committed changed fields | No document diffs | Absent | Derive from committed version diff |
| Persistent canvas | One sticky right-side document | Two-column workspace only | Absent | Add canvas state and collision rules |
| 472px chat floor/56px rail | Wide Workbench layout | 268px fixed LeftNav; no usable mobile breakpoint | Absent | New responsive shell |
| Campaign Library | NPCs, Bestiary, Documents, Session log, Cues | No server collection UI/API | Absent | Add categorized paginated library |
| Export/print | Canvas export; printable handout | Chat export is JSON; README calls it Markdown | Partial/mismatched | Define private/player-safe formats and print |
| Per-field reveal | Explicit mask, default private | No reveal state or public projection | Absent | Add masks/audiences/active projection |
| Reveal indicator | Chat + canvas live indicator and stop | No equivalent | Absent | Shared server state and subscriptions |
| Campaign link/QR | One expiring table link | No player route or deep links/router | Absent | Add secure token, route, proxy, QR/copy |
| Player view | Read-only masked content | No table/public shell | Absent | Add sanitized renderers and states |
| Media assets | Portrait/map/audio bytes and metadata | Text/PDF upload discards original bytes | Absent | Add object storage/import/processing |
| Portrait/map tools | Media result and expand | No media provider/asset result/card | Absent | Capability-gated adapters and UI |
| Audio cue card | Preview transport + broadcast action | No audio component/domain | Absent | Port accessible components |
| Live audio | Cue state, listeners, pending consent | No SSE/WebSocket/presence/shared state | Absent | Add realtime protocol/fanout/reconnect |
| Browser audio consent | One gesture per session, mute only | No table/audio client | Absent | Add consent/presence/mute behavior |
| Ordered migrations | Safe large schema evolution | Startup reapplies idempotent SQL only | Absent | Add locked checksummed runner |
| Transaction/outbox | Atomic aggregate + realtime/cleanup work | One connection per operation; no outbox | Absent | Add repositories, pool, outbox |
| Workbench E2E | DM/player, reveal, audio, reconnect, mobile | One desktop deterministic chat happy path | Absent | Add multi-context and integration profiles |

## Data-model gap

### Current durable data

`service/sql/04-chat-schema.sql` and `05-auth-schema.sql` provide:

- `auth.users` and `auth.invites`;
- `chat.conversations` with owner and model-selection fields;
- `chat.messages` with role, mode, content, and spell suggestions; and
- `chat.attachments` with extracted text only.

The corpus lives separately in `dnd.chunks`/pgvector.

### Minimum new durable concepts

The reference cannot work reliably without:

- campaigns and campaign-scoped roles/audiences;
- live table sessions and hashed/expiring/revocable join tokens;
- campaign-linked conversation metadata and typed timeline entries;
- tool invocations/results with idempotency and status;
- documents and immutable document versions;
- reveal state/active public projection with monotonic revision;
- media assets, derivatives, processing jobs, and cleanup;
- audio cue metadata and current live playback state; and
- an outbox/fanout seam for public/realtime changes and object cleanup.

These are not suitable as additional nullable columns on `ChatResponse` or as
localStorage state.

## API/contract gap

The current `/chat` contract is a good legacy route but a poor universal tool
transport. It is synchronous and returns a fixed response containing prose,
sources, suggestions, optional spell content, and optional stat block.

The Workbench needs versioned discriminated unions for:

- invocation input and lifecycle;
- card/document/media result placement;
- every card and document payload;
- timeline entries and pagination;
- document revisions, patches, restore, and conflicts;
- reveal mask mutation and table-safe projection;
- asset/cue metadata and processing state; and
- realtime snapshots/events/control acknowledgements.

The server should validate tool IDs, result kinds, document types, and
suggestion targets rather than trusting values echoed by a model or client.
Pydantic and Zod fixtures must remain synchronized.

## Security gap

Current conversation ownership is a strong starting point, but the design adds
new bearer/public and cross-user boundaries:

- a table link that grants scoped anonymous or participant access;
- owner-only character-sheet data beside table-visible data;
- field-level secret projection;
- media reads and remote imports;
- realtime subscriptions and GM-only controls; and
- deletion/revocation across Postgres, object storage, and connected clients.

UI gating is not sufficient. Public responses must be built from allowlisted
fields and must never carry the private document JSON. Tokens require adequate
entropy, hashing at rest, expiry, rotation, immediate revocation, bounded rate
limits, non-enumerating failures, and protection from logs/metrics/referrers.
Remote media import requires SSRF, redirect, MIME/magic-byte, size, timeout,
and resource-exhaustion controls.

## UX/accessibility gap

The component source is illustrative inline JSX, not production-ready React
for this repo. Direct copying would miss:

- focus trapping/return and Escape/outside-click behavior for menus/sheets;
- complete keyboard behavior for slash selection;
- live-region semantics for working/error/realtime updates;
- retry/cancel controls;
- labeled and validated editing (the reference relies on `contentEditable`);
- keyboard waveform seeking;
- save/autosave/conflict states;
- responsive behavior below the wide three-column threshold; and
- player/table loading, expired, ended, reconnecting, consent, and mute states.

The existing DS test/story patterns should be followed while preserving the
reference contracts and token use.

## Internal inconsistencies in the reference

1. `GameDocument.jsx` says stat blocks delegate to StatBlockCard but never does
   so, and it ignores several document-type flags.
2. The reveal helper omits stat cells, so the NPC default `voice` field cannot
   actually be selected as described.
3. `data.revealed`, CanvasPane `revealed`, and wire `reveal_mask` are three
   disconnected states.
4. Reveal defaults, staged Confirm, and immediate un-reveal do not define one
   consistent state machine.
5. One table-wide campaign link conflicts with `audience: owner` on character
   sheets.
6. Tool payloads lack concrete schemas for loot, names, rules, hooks, media,
   and most document data.
7. Rail-tap behavior does not say how it gets a brief; the supplied matcher
   mishandles command arguments.
8. Auto-open document results conflict with the rule that an open canvas never
   swaps implicitly.
9. Direct blur edits have no validation, conflict, save, or undo behavior;
   `changed_fields` clearing on the “next turn” is racy.
10. Audio lacks a durable cue schema, transport choice, reconnect rules,
    authoritative clock, multi-instance fanout, and precise ambience/one-shot
    interaction.
11. The claimed “seven” document types are actually eight, and one section
    label contains an HTML entity inside a JavaScript string.
12. No responsive or complete library/player-page design is included.

These are implementation blockers, not cosmetic questions. They are owned by
the `1kg.1.*` decision/contract beads before dependent work freezes behavior.

## Current correctness/coordination issues

The audit also found current seams that should be fixed or coordinated while
the Workbench is built:

- server-minted conversations can be orphaned from the local sidebar;
- mode changes retain a conversation ID and may produce mixed-mode history;
- reopened history loses sources, answerability, creative/refusal state,
  routing, and structured cards;
- model-selection UI/backend wiring is only partially effective despite merged
  code; and
- history is display persistence, not model memory.

The new epic directly owns server conversation metadata and lossless timeline
work. It uses non-blocking relations to the existing model-routing (`b8o`),
memory/deletion (`1ka`), and service-router (`iu6`) beads rather than creating
duplicates. The user’s untracked chat-reading plan/research/report files were
left untouched.

## Recommended implementation boundary

### Reuse unchanged or extend conservatively

- auth/session/current-user machinery;
- mode enum/personas/retrieval and grounded `/rules` behavior;
- DS tokens/themes and existing primitives;
- GameContentCard, SpellCard, and StatBlockCard;
- Pydantic + Zod boundary pattern;
- Langfuse/runtime metrics seam; and
- in-memory store/E2E fake testing patterns.

### Rework/generalize

- conversation index and history into server metadata + typed timeline;
- fixed nullable structured fields into result/content unions;
- startup SQL into ordered migrations;
- per-operation DB connections into transactional repositories/pooling; and
- state-only navigation into deep-link-safe application/table routing.

### Build new

- campaign/session/participant domain;
- registries, tool API/executors, AssistantLane/ToolRail;
- document/version/edit/reveal domain;
- canvas/library/player table UI;
- object-backed media/cue domain; and
- realtime fanout, consent, and presence.

## Recommendation

Do not begin by porting all six JSX components. Start with the decision and
contract beads, then prove one end-to-end card-tool slice and one NPC document
slice. That establishes the durable timeline, authorization, idempotency,
versioning, and canvas semantics that every other tool/document needs. Reveal
comes after the private document model is trustworthy; audio comes last because
it is the only persistent multi-client realtime feature in the handoff.

The canonical sequencing, full bead map, target architecture, release gates,
risks, and non-goals are in
`docs/forge/plans/aetheril-gm-workbench-expansion.md`.


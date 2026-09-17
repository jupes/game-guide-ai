# Workbench wire contract

Contract version: **1** · Bead: `agent-forge-harness-1kg.1.2` (in progress) ·
Decisions: [`adr/gm-workbench-interactions.md`](adr/gm-workbench-interactions.md)

The request, response and event shapes the GM Workbench speaks. This page holds
the conventions every shape follows, the rules for changing one, and the status
of each schema family. The shapes themselves live in code and in fixtures, not
here: a page cannot fail a build.

## Where the contract lives

| What | Where |
| --- | --- |
| **The specification** — examples every implementation must accept or reject | [`contracts/workbench/v1/`](../contracts/workbench/v1/) |
| Server models (Pydantic) | `service/workbench_contracts.py` |
| Client schemas (Zod), safe parsers, the error reader | `ui/src/gm/contracts.ts` |
| Server helpers every route uses: `validation_error_body`, `check_fields`, `entry_or_opaque` | `service/workbench_contracts.py` |
| Wire → design-system adapters | `ui/src/gm/adapters.ts` |
| The two suites that read the same fixtures | `service/tests/test_workbench_contracts.py`, `ui/src/gm/contracts.test.ts` |
| The differential fuzz, and the CI job that runs it | `contracts/workbench/tools/differential_fuzz.py` (+ `.ts`), job `contract-parity` |

`schemas.json` lists every schema name. Each suite fails if it lacks one, if a
schema has no fixture file, or if a fixture has no valid or no invalid example.
So neither language can quietly fall behind the other.

`registry.json` pins the few registry facts the validators need — which tool
lands as which result kind, which may run without a brief, which document type
lives in which library category. Both languages keep those facts as constants
and both suites compare them with the file. `1kg.3.1` owns the full tool and
document-type catalogue and extends this same file, rather than starting a
second source of truth.

## Conventions

| Topic | Rule |
| --- | --- |
| Key casing | **snake_case everywhere on the wire, including nested document data.** The handoff's `ifAttacked`, `looseThreads`, `xpBudget` and `partyLevel` become `if_attacked`, `loose_threads`, `xp_budget` and `party_level`. Design-system props stay camelCase; `adapters.ts` is the one place the two meet. |
| Enumerated values | Keep the handoff's spelling, because they are values, not keys: `session-notes`, `quest-log`, `character-sheet`, `one-shot`. Tool ids are lower-case; the slash parser normalises case before a request is built. |
| Field keys | A document **field** is a top-level key of its `data`: one flat, snake_case namespace per type, `[a-z][a-z0-9_]{0,39}` (CANVAS-19). It is the unit of concurrency, of change lists, of an edit's field scope and of a reveal mask. There are no paths into a field: something that needs to be written or shown on its own is its own field. |
| Identifiers | Opaque: `[A-Za-z0-9_-]{1,64}`. Safe in a URL fragment and in a log line, and never meaningful. Conversation ids are UUIDs today and fit. An invocation id is client-minted and is 16–64 characters, because it is an idempotency key. |
| Timestamps | **One grammar**, pinned by `Timestamp.json`: `YYYY-MM-DDTHH:MM:SS`, an optional fraction of up to six digits, then `Z` or `+HH:MM`. The server emits UTC with `Z`. Formatting for people is the client's job; a display string such as `7:36 PM` is rejected (CANVAS-27). The grammar is spelled out because the libraries disagree when left alone: Pydantic reads `1758050000` as a moment and Zod accepts a time without seconds. |
| String bounds | Counted in **Unicode code points**, on both sides, so an emoji costs one. The client uses `codePointLength`, never `.length`. |
| Trimming | A brief is trimmed before its bound is checked, and the server stores it trimmed. JavaScript's `trim()` and Python's `strip()` disagree about a few exotic code points, so **the server's judgement is final** and the client's check exists only to save a round trip. |
| Coercion | None. A `brief` of `42` is an error, not the string `"42"`, and a `schema_version` of `true` is not `1` — which a bare Python `Literal[1]` would accept, because to Python `True == 1`. |
| Not recorded | Where an older row never stored a fact, the key is **present and `null`**: `answerable: null`, `sources: null`. A missing key is invalid, so a client cannot mistake "not recorded" for `true` or for "none". |
| Text format | Document fields are plain text. Assistant prose in a lane is Markdown, rendered without remote subresources (X-10). |
| What never appears | A URL in an asset reference (X-10). A document body in a link (`1kg.4.4`). A free-form `context` object in a request (RAIL-7). A player's name or a cue's title in anything sent to a table client (AUD-11, AUDIO-29). |

## Envelopes

### Errors

A Workbench route answers a failure with FastAPI's `detail` key holding an
object:

```json
{ "detail": { "code": "cap_reached", "message": "Two tools are already running.", "retryable": true,
              "in_flight": ["inv_9f2c4e1a7b3d4c5e", "inv_0a1b2c3d4e5f6a7b"] } }
```

`message` is presentable as it stands and never echoes GM-private text. `field`
names the request field at fault. `retry_after_s` appears only with
`throttled_user`; the pilot's daily cap has no window to wait out (RAIL-20).
`in_flight` appears only with `cap_reached` (X-5). `conflict` appears only with
`conflict` on a document write or an AI edit: it names the fields that moved and
the write revision to rebase on, and **never carries their text**, because an
error body is where logs and traces look (X-7). A client reads the latest values
through the document endpoint and then offers *Keep mine* or *Use latest*
(CANVAS-20).

**A Workbench route must not return FastAPI's default 422.** That body repeats
each error's `input` — the request itself, GM-private text included; a test in
`test_workbench_contracts.py` shows it happening. Routes answer with
`validation_error_body(exc.errors())` instead. It reads only an error's type and
location, uses fixed sentences, never names an undeclared key back (that key is
the client's text, not a field of ours), and maps to the specific codes below.

Codes are a **closed set on the server**, lower-case and free of user text, so a
code is always safe as a metric label. A client treats a code it does not know as
a generic failure.

| Code | Status | Retryable | Meaning |
| --- | --- | --- | --- |
| `validation_failed` | 422 | no | the request is malformed |
| `unsupported_schema_version` | 422 | no | the request names a contract version this server does not speak |
| `brief_required`, `brief_too_long` | 422 | no | RAIL-5, RAIL-6 |
| `unknown_tool` | 422 | no | the tool id is not in the registry |
| `tool_disabled` | 409 | no | the tool's capability is off (RAIL-10) |
| `campaign_required` | 409 | no | RAIL-13 |
| `nothing_to_recap` | 409 | no | the conversation holds nothing to recap |
| `not_found`, `forbidden` | 404, 403 | no | clients show one generic unavailable state for both (CANVAS-31) |
| `conflict` | 409 | per case | a field, a reveal epoch or an audio epoch moved on. Not retryable for a field patch, which needs *Keep mine* or *Use latest*; retryable for an AI edit, whose *Try again* runs on a fresh base (CANVAS-25) |
| `cap_reached` | 409 | yes | X-5: two tool invocations or AI edits are already in flight |
| `throttled_user` | 429 | yes | the per-user window; carries `retry_after_s` |
| `throttled_daily` | 429 | no | the pilot's daily cap |
| `provider_failed`, `provider_timeout` | 502, 504 | yes | the model provider |
| `attempt_expired` | — | yes | the server expired a stuck attempt (RAIL-27); seen on an invocation, never as a response status |
| `backend_unavailable` | 503 | yes | the service fails closed |

Legacy routes still answer with a string `detail`, and FastAPI's own validation
failures with a list. `readErrorBody` in `contracts.ts` reads all three, so the
client has one error path.

### Idempotency

Every Workbench mutation can be retried safely.

| Mutation | Key | A repeat… |
| --- | --- | --- |
| Tool invocation, AI edit | `invocation_id`, minted by the client | while working, reports status and starts nothing; once done, replays the stored result free of charge; after a retryable failure, starts a new attempt that passes the cost guards again (RAIL-18) |
| Reveal and audio commands | `command_id` | is recognised and is not a second command (AUDIO-24) |
| Field patch | the document's base **write revision** | conflicts only if a field it touches changed since (CANVAS-19, CANVAS-34). A repeat of a patch that already landed finds the fields equal to what it sends and is a no-op, not a conflict |
| AI edit after a conflict | the same `invocation_id`, a fresh `base_write_revision` | starts a new attempt on the new base; the body of a replay is otherwise ignored |
| Create a document | `command_id`, minted by the client | opens the document already made instead of making a second *Untitled NPC* |
| Restore, archive, unarchive | none needed | restoring what the document already equals changes nothing and creates no version; the others set a state |

### Pagination

List responses are `{ "items": [...], "next_cursor": "<opaque>" | null }`, read
with `?cursor=` and `?limit=`. `next_cursor` is always present: the end of a list
is `null`, never a missing key. A cursor is opaque to clients and is base64url
(`[A-Za-z0-9_-]{1,512}`), so it may travel in a query string. **Search text may
not**: it travels in a request body (X-7), and a cursor never encodes any.

`TimelinePage` is the first concrete page. It holds at most 100 entries,
**newest first**, and its cursor leads to older entries; a client reverses a page
for display. Order is the server's and is never re-derived from `created_at`.

## Versioning and forward compatibility

Every top-level payload carries `schema_version`, and so does every timeline
entry. It is `1` throughout this contract version.

Version 1 is still being assembled by `1kg.1.2`, family by family, and nothing
consumes it yet. The rules below start to bind when that bead closes; until then
a new family may add a member to a union without a bump.

| Change | Version |
| --- | --- |
| A new optional field; a new error code | **no bump** — old clients strip the field and treat the code as generic |
| A new member of a discriminated union (a result kind, a card kind, a timeline entry kind); a removed or renamed field; a changed meaning | **bump** |

What each side does with something it does not know:

| Side | Meets | Does |
| --- | --- | --- |
| Server | a request with an unknown `schema_version`, an undeclared field, an unknown tool, kind or type | **fails closed**: 422, before any provider work |
| Server | a *stored* entry it cannot validate — written by a newer server before a rollback, or damaged | serves an **`opaque` entry** in its place: same id, same position, a closed `reason` (`newer_version` or `unreadable`), and **nothing of the payload**, because a server never forwards bytes it has not validated. The stored row is left untouched, so it renders again after a roll-forward. `entry_or_opaque` in `workbench_contracts.py` is that rule as code, for `1kg.4.2` to call |
| Client | an additive field; an unknown error code | **tolerates** it: the field is stripped and never reaches a component |
| Client | a newer `schema_version`, an unknown kind, or a payload that does not validate | renders a neutral placeholder — *This result was made by a newer version of Aetheril.* — through `parseToolInvocation`, `parseToolResult`, `parseDocument`, `parseTimelineEntry` and `parseTimelinePage`. It never crashes the thread, and it never falls back to an NPC (X-8, RAIL-24). A page is read entry by entry, so one entry from a newer server becomes one placeholder and the rest of the thread renders (AE-43) |

That asymmetry — a strict server, a tolerant client — is written into the
fixtures. An example may carry `"applies_to": ["server"]` or `["client"]`: an
undeclared field is *invalid for the server to emit* and *valid for a client to
receive*. Requests are the exception: the client builds them, so they are strict
on both sides.

## Schema families

| Family | Status | Notes |
| --- | --- | --- |
| Error envelope | **done** | `ErrorBody` |
| Tool invocation | **done** | `ToolInvocationRequest`, `ToolInvocation`, `ToolResult` (card, document, media), `ToolSuggestion`, `DocumentLink`, `AssetRef` |
| Card payloads | `stat_block` **done**, reusing the `/chat` stat-block contract | loot, names, rules and hooks are `1kg.4.3`'s; until they exist those tools cannot produce a valid card, by design |
| Legacy guards | **done** | today's `/chat` and message-history responses, validated by the existing models |
| Timeline entries and their page | **done** for `chat`, `tool`, `edit`, `session_divider` and `opaque` | `TimelineEntry`, `TimelinePage`. The attached-cue entry arrives with the cue family; until v1 is declared complete, adding it is not a version bump |
| Documents | **done** | `Document`, `DocumentVersion`, `DocumentVersionSnapshot`, `DocumentHistoryPage`, `FieldPatchRequest`, `DocumentCreateRequest`, `RestoreRequest`, `EditRequest`, `EditInvocation`, `LibraryQuery`, `LibraryPage`, and `conflict` on the error envelope. **Who may see a field is not this family's to define**: `agent-forge-harness-1ir.1.2` decides it, and it blocks `1kg.5.1`. Promoting a card to a document (LIB-11) is `1kg.5.6`'s request to add |
| Per-type document fields | the **frame is done**; `npc` is the worked example | `1kg.5.3` owns all eight types. Until it declares a type's fields that type has the common ones only and everything else fails closed — the same posture as card kinds |
| Reveal | to do, and **waiting** | the mutation with its epoch, Stop, GM-side state, the allowlisted projection, the table snapshot. Audience and slot shapes must not freeze before `agent-forge-harness-1ir.1.2` (field eligibility, shared with the Live Session Assistant) is decided; it blocks `1kg.7.1` |
| Media assets and cues | to do | storage-dependent fields wait for `1kg.1.4` |
| Realtime events | to do | the payloads are this bead's; the transport is `1kg.1.4`'s |
| Tool and document-type registry | `1kg.3.1` | extends `registry.json` |

## The tool-invocation family

A request names a tool, a brief and where it belongs. It carries no context of
its own: the server assembles that from state it has authorised.

```json
{
  "schema_version": 1,
  "invocation_id": "inv_9f2c4e1a7b3d4c5e",
  "tool_id": "npc",
  "brief": "the hooded stranger at the bar",
  "campaign_id": "cmp_4b1d9e7a",
  "conversation_id": "0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d"
}
```

The answer, and every later status read, is a `ToolInvocation`. Its `status` is
`working`, `done`, `failed` or `cancelled`, and its payload must agree: only
`done` carries a `result`, only `failed` carries an `error`. `unknown` is a
client-only state and never appears on the wire (RAIL-21). `cancel_requested`
with `done` means the run finished before it could be cancelled, and the result
is kept because it was paid for (RAIL-23).

```json
{
  "schema_version": 1,
  "invocation_id": "inv_9f2c4e1a7b3d4c5e",
  "tool_id": "npc",
  "status": "done",
  "attempt": 1,
  "cancel_requested": false,
  "created_at": "2026-09-16T19:31:02Z",
  "updated_at": "2026-09-16T19:31:24Z",
  "result": {
    "result_kind": "document",
    "tool_id": "npc",
    "prose": "Wrote her up as a dossier.",
    "document": { "document_id": "doc_9k2f7a1c", "type": "npc", "title": "Sister Ondrey Vashe", "library_category": "npcs" },
    "suggestions": []
  },
  "error": null
}
```

`result_kind` is the routing rule — "size decides placement" — and it must agree
with the registry: an `npc` result that claims to be a card is invalid, and so is
an `encounter` result that links an NPC. A document result links its document and
embeds none of it. A media result names an asset by id and never by URL. At most
three suggestions ride along, each targeting a registry tool; a suggestion arms
the composer and never runs (RAIL-8).

The handoff's `tool_label` is not on the wire: a label is a registry fact, and
sending it would give the two a chance to disagree.

## The documents family

### Fields

A document's `data` is flat: one value per field key, as bare JSON. What each key
must hold is the type's definition, which is a registry fact
(`registry.json`: `field_kinds`, `common_fields`, and `fields` per document type)
kept as constants in both languages and pinned by both suites.

| Kind | On the wire | Cleared as |
| --- | --- | --- |
| `text` | one line, at most 200 characters | `""` |
| `prose` | plain text, at most 20,000 characters | `""` |
| `text_list` | at most 100 items of 1 to 2,000 characters | `[]` |
| `asset` | an `AssetRef` (an id, never a URL) | `null` |

Every type has `name`, `qualifier` and `tags`. `name` is the title everywhere and
the one field that cannot be blank (LIB-12). `npc` adds `portrait`, `voice`,
`tell`, `attitude`, `wants`, `leverage`, `if_attacked` and `notes` — the handoff's
keys in snake_case. **A kind never appears on the wire**, so `1kg.5.3` can add
kinds (integers, ability scores, entry lists) and fields without a version bump:

- the **server** rejects a key the type does not declare, in what it stores and
  in what it emits;
- a **client** strips a key it does not know, so a type can grow;
- in a **request**, which the client builds, a stray key is an error on both sides.

`type_version` says which revision of a type's field definitions `data` conforms
to. Both sides know version 1 of every type. A client that meets a newer one shows
a placeholder through `parseDocument` rather than half a document, and an unknown
`type` is never rendered as an NPC — which is what the handoff's registry fallback
did (X-8). Lookups on the client use `Object.hasOwn`: a key named `constructor`
must read as *not declared*, not find something on a prototype.

Nothing in this family says who may *see* a field. `reveal_mask`, audiences and
eligibility are not on a document at all: reveal is server state with its own
resource (CANVAS-33), and what may be shown to whom is
`agent-forge-harness-1ir.1.2`'s decision.

### Versions and the write revision

History and concurrency are different things (CANVAS-34), and the wire keeps them
apart. `write_revision` counts committed writes and is the **only** concurrency
token; it stops at 2^53 − 1 so that it survives `JSON.parse`. A version `number`
is for people, and for pinning a reveal to a **sealed** version (REVEAL-8); it is
never a base for a write. The handoff's `label` (`"v3"`) and display `time`
(`"7:36 PM"`) are not on the wire.

A `Document` carries its current version only. `DocumentHistoryPage` lists
versions newest first, 20 at a time (CANVAS-27). `DocumentVersionSnapshot` is the
content of one version — the history preview, and old text beside new when a live
reveal is updated (REVEAL-7). It has no write revision, because nobody may base a
write on the past.

### Writing

| Request | Says | Answered with |
| --- | --- | --- |
| `FieldPatchRequest` | the fields one autosave touches, the write revision it was based on, and the type and type version it was built against (CANVAS-10). The author is always the GM and is never the client's to state | the whole `Document`, or a 409 whose `conflict` names what moved |
| `DocumentCreateRequest` | New in a library category (LIB-12): a `command_id`, the campaign, the type and at least a name | the `Document` |
| `RestoreRequest` | a version number (CANVAS-26). Additive, so it needs no base | the `Document`, whose version says `restored_from` |
| `LibraryQuery` | one page of one category (LIB-20 to LIB-23). A request **body** even without a search, because search text may never travel in a URL (X-7) | a `LibraryPage` that echoes the campaign and category it answers, so a stale response is dropped (LIB-25) |

A write always answers with the whole document: a dossier is small, and a client
that has the whole truth has nothing to reconcile. What it must still do itself is
never let a save response overwrite newer local text (CANVAS-10).

`check_fields` in `workbench_contracts.py` is the validator behind all of these.
`1kg.5.2` and `1kg.5.5` call it on a merged document before committing it
(CANVAS-19). Its messages name keys and kinds and never quote a value.

### AI edits

An `EditRequest` has an invocation id, the same lifecycle as a tool and the same
cap (X-5). Its **scope is a server-side guarantee, not a prompt suggestion**
(`1kg.5.5`):

```json
{ "kind": "selection", "field": "if_attacked", "start": 4, "end": 35, "text": "walks into the water and is gon" }
```

A selection names one field, a span in **code points** — not UTF-16 units, so a
client converts before sending — and the exact text the GM saw. The server refuses
a span that no longer matches before any provider work. An instruction is the
GM's words (1 to 2,000 characters, sharing the brief's bound, RAIL-6) or one of
the `SelectionBar`'s complete requests — `rewrite`, `shorter`, `darker` — which
are valid only on a selection (CANVAS-23).

An `EditInvocation` finishes as `changed` (a version number for the lane's
`EDIT · v<n>` badge, the new write revision, and the fields to wash gold) or as
`no_change`, which creates no version (CANVAS-25). **A conflict is a failed
invocation, not an outcome.** The result never carries the document: documents
travel through the document endpoints only, so neither the thread nor its export
can hold a body (EXPORT-12).

## The timeline family

A conversation is read as a list of entries, one per **exchange**: a turn
carries its own outcome. That is a deliberate choice over a flat list of
messages with reply pointers.

- A page boundary can never separate a prompt from its result, so the rule in
  `1kg.4.2` — *pagination never splits a prompt/result association* — holds by
  construction instead of by cursor arithmetic.
- Tool results finish in any order (RAIL-16). Embedded, each one sits beneath the
  turn that asked for it without the client regrouping anything.
- It is what the client already keeps: `useChat` pairs stored rows into
  `Exchange` objects today, and drops an answer whose prompt fell outside the
  loaded window. Pairing on the server removes that loss.
- An entry has one id. That id is what a suggestion's `source_entry_id` names
  (RAIL-8) and what makes *Save to Bestiary* idempotent (LIB-11).

| `entry_kind` | Carries | Notes |
| --- | --- | --- |
| `chat` | `mode`, `prompt`, `answer` | A plain message and its answer, in any mode (RAIL-14). `answer` is `null` while no answer is stored: the turn failed, or is still running elsewhere. `prompt` is `null` only for an old answer whose prompt was never recorded; one of the two is always present |
| `tool` | `brief`, `invocation`, optional `source_entry_id` | RAIL-9: a tool and a brief, never the slash string. The tool is `invocation.tool_id`, kept in one place so the turn and its lane cannot disagree. The embedded `ToolInvocation` keeps all of its own rules. The brief *bound* holds on the way back out; the brief *policy* is not re-judged, so a registry change can never make an old turn unreadable |
| `edit` | `document` (a link), `scope`, `instruction`, `invocation` | An AI edit and its outcome. The GM's words are the turn, as a brief is for a tool. The thread keeps the scope's kind and field and **never the selected text**, which is document text (EXPORT-12) |
| `session_divider` | `session_id`, `boundary` (`start` or `end`) | The only thing that separates prep from play. `/recap` reads from the latest `start`; rotating a link moves neither boundary (REVEAL-17). Session titles and numbers belong to `1kg.2.1` and can arrive later as an optional field |
| `opaque` | `reason` | The server's placeholder for a stored entry it cannot read. See *Versioning and forward compatibility* |

Every entry carries its own `schema_version`, not only the page: entries are
stored one at a time and can outlive the server version that wrote them.

```json
{
  "schema_version": 1,
  "entry_kind": "chat",
  "entry_id": "ent_10a4c2e9",
  "created_at": "2026-09-16T19:20:11Z",
  "mode": "sage",
  "prompt": "How does a basilisk's gaze work?",
  "answer": {
    "text": "A basilisk petrifies with its gaze [1].",
    "answerable": true,
    "sources": [{ "book": "mm-5e", "chapter": "Bestiary", "section": "Stat Block", "entity": "Basilisk", "page": 12, "snippet": "Armor Class 15 ..." }],
    "created_at": "2026-09-16T19:20:19Z"
  }
}
```

An `answer` is a **complete outcome**: text, `answerable`, `sources`, and the
optional usage ideas, routing disclosures, spell card and stat block that
`POST /chat` returns today. History used to keep only the text, and the client
filled the gaps with `sources: []` and `answerable: true`. For rows written
before the durable timeline those facts are honestly unknown, which the contract
says with `null` (see *Not recorded* above).

The pieces of an answer are the existing `service/models.py` shapes, reused
rather than re-declared. That is how the evidence provenance that
`agent-forge-harness-xiu.5.2` adds to stored answers will round-trip through the
timeline without a second definition: it extends those shapes, or lands as one
new optional field on `answer`, and neither is a version bump. Those shapes keep
their own, laxer validation — they coerce `"12"` to `12` — and the differential
fuzz checks that whatever the server accepts through them it emits in a form the
client reads.

What an entry can never carry: a document body (a result links, EXPORT-12), the
prompt the server assembled, attachment text, a provider payload, or the owner's
user id. The server models forbid undeclared fields, and a client strips them.

Entry-to-component adapters are `1kg.3.4`'s; the lane pieces they need
(`toLaneStatus`, `toLaneSuggestions`) already exist in `adapters.ts`.

## Legacy compatibility

`POST /chat` and `GET /conversations/{id}/messages` are untouched. Their
responses are pinned by guard fixtures under `contracts/workbench/v1/legacy/`,
validated by the *existing* `ChatResponse` and `MessagesResponse` models and
their Zod mirrors, so a later slice cannot change them without a test failing.
The Workbench error envelope keeps the `detail` key for the same reason.

## Changing the contract

1. Write the examples first: a fixture file named after the schema, with valid
   and invalid cases, each invalid one saying why.
2. Add the name to `schemas.json`.
3. Implement it in `service/workbench_contracts.py` and `ui/src/gm/contracts.ts`.
   Both suites must pass on the same files.
4. Mark an example `applies_to` only for the strict-server, tolerant-client
   asymmetry, never to paper over a disagreement.
5. Check *why* each invalid example fails, not only that it does.
6. Run the differential fuzz: `python contracts/workbench/tools/differential_fuzz.py`.
   It mutates every valid example and fails if the two validators disagree about
   one of this contract's shapes, or if the server can emit something the client
   cannot read. A finding is fixed in the looser validator and pinned by a new
   fixture example. CI runs it as `contract-parity`.
7. Decide whether the change needs a version bump, using the table above.
8. Add the adapter beside the schema if a design-system component consumes it.

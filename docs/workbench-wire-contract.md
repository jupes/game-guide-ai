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
| Server helpers every route uses: `validation_error_body`, `redacted_errors`, `check_fields`, `trim`, `entry_or_opaque` | `service/workbench_contracts.py` |
| Wire → design-system adapters | `ui/src/gm/adapters.ts` |
| The two suites that read the same fixtures | `service/tests/test_workbench_contracts.py`, `ui/src/gm/contracts.test.ts` |
| The differential fuzz, and the CI job that runs it | `contracts/workbench/tools/differential_fuzz.py` (+ `.ts`), job `contract-parity` |

`schemas.json` lists every schema name. Each suite fails if it lacks one, if a
schema has no fixture file, or if a fixture has no valid or no invalid example.
So neither language can quietly fall behind the other.

`registry.json` is the **canonical registry** (`1kg.3.1`): the ten tools with
their commands, aliases, labels, icons, blurbs, working labels, result kinds,
brief policies, what they create and which capability they need; the eight
document types with their labels, icons, renderers, library categories, field
definitions and field labels; the capability switches with the reason a
disabled tool shows; the default pins and the rail limit; and the media, cue
and slot vocabularies. Both languages keep it as validated data —
`service/workbench_registry.py` for server policy, `ui/src/gm/registry.ts` for
the rail, the slash menu and More — and both suites compare their copy with the
file, so neither can drift. Each copy is validated when it loads: duplicate ids,
a command or alias that collides with another (SLASH-2), an unknown result
kind, type, card kind, capability or renderer, an HTML entity in a label, a
default pin that is not a tool or that needs a capability which is off by
default, or more than five pins (RAIL-11) is a build failure, never a running
service. A lookup of an unknown id answers nothing — never the NPC config the
handoff fell back to (X-8). The wire contract's own validators pin the subset
of the registry they need as constants.

Since `1kg.5.3` the registry also carries, per field, the rule that says how it
is presented and who may ever see it — label, `editable`, `required` (LIB-12),
`bounds` (the range one *use* of an `integer` field narrows its kind to),
`revealable` (the allowlist of REVEAL-10, and by ED-5 the list that can ever be
classified) and the reveal sheet's warning copy — and, per type, its audience,
accent, reveal groups, per-audience default reveal (REVEAL-4) and retired keys
(ED-24). What it does **not** carry is a reveal mask, a projection or an
eligibility row: those are state, and belong to `1kg.1.6` and
`agent-forge-harness-1ir.2.1`.

A type's **`audience`** says **whose default reveal the type seeds**, and
nothing else (ED-14 and amendment A-19, which amend AUD-9 for owner decision
O-2). It does *not* restrict which types may use a participant slot — **any type
may**. An owner-audience type seeds its linked owner's mask; any other audience,
and any other type revealed to a participant, seeds **empty**, and an
owner-audience type still seeds nothing for the table (AUD-12). The selector is
`seeds_owner_default` / `seedsOwnerDefault`; the audience *picker* is
`1kg.7.3`'s, not the registry's.

## Conventions

| Topic | Rule |
| --- | --- |
| Key casing | **snake_case everywhere on the wire, including nested document data.** The handoff's `ifAttacked`, `looseThreads`, `xpBudget` and `partyLevel` become `if_attacked`, `loose_threads`, `xp_budget` and `party_level`. Design-system props stay camelCase; `adapters.ts` is the one place the two meet. |
| Enumerated values | Keep the handoff's spelling, because they are values, not keys: `session-notes`, `quest-log`, `character-sheet`, `one-shot`. Tool ids are lower-case; the slash parser normalises case before a request is built. |
| Field keys | A document **field** is a top-level key of its `data`: one flat, snake_case namespace per type, `[a-z][a-z0-9_]{0,39}` (CANVAS-19). It is the unit of concurrency, of change lists, of an edit's field scope and of a reveal mask. There are no paths into a field: something that needs to be written or shown on its own is its own field. |
| Identifiers | Opaque: `[A-Za-z0-9_-]{1,64}`. Safe in a URL fragment and in a log line, and never meaningful. Conversation ids are UUIDs today and fit. An invocation id is client-minted and is 16–64 characters, because it is an idempotency key. |
| Timestamps | **One grammar**, pinned by `Timestamp.json`: `YYYY-MM-DDTHH:MM:SS`, an optional fraction of up to six digits, then `Z` or `+HH:MM`. The server emits UTC with `Z`. Formatting for people is the client's job; a display string such as `7:36 PM` is rejected (CANVAS-27). The grammar is spelled out because the libraries disagree when left alone: Pydantic reads `1758050000` as a moment and Zod accepts a time without seconds. |
| String bounds | Counted in **Unicode code points**, on both sides, so an emoji costs one. The client uses `codePointLength`, never `.length`. |
| Text | Every string is **well-formed Unicode**. JSON allows the escape of a lone surrogate (`U+D800` to `U+DFFF` on its own); UTF-8, the database and a response do not, so both validators refuse it, and it is a 422 rather than a failure to store or to answer. |
| Trimming | A brief, an edit instruction and a search are trimmed before their bounds are checked and are stored trimmed; a name is blank if trimming empties it. Both sides trim **exactly the set `String.prototype.trim` removes** — ASCII whitespace, the Unicode space separators, the line and paragraph separators and the byte order mark — spelled out by code point in both languages (`trim` on the server, `trimWire` on the client). Python's `strip()` would also take NEL and the ASCII separators and leave the mark, and then one side finds a brief empty that the other finds two characters long. |
| Coercion | None between JSON types. A `brief` of `42` is an error, not the string `"42"`, and a `schema_version` of `true` is not `1` — which a bare Python `Literal[1]` would accept, because to Python `True == 1`. Within JSON's one number type, an **integer field accepts any integral value**: `14.0` is `14` on both sides, because JavaScript cannot tell them apart; `14.5` is an error. |
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
When a stale client fails in several places at once, a version error wins
whatever its position in the list, because the answer to it is *reload*: an
unknown `schema_version`, or a `type_version` this server does not know, both
map to `unsupported_schema_version`.

The same discipline holds for what a route **logs or traces**. `str(exc)` and
`exc.errors()` carry the request; `redacted_errors(exc.errors())` keeps `type`,
`loc` and `msg` only, and replaces the location of an undeclared key, which is
that key. The contract models hide their input from `str(exc)` as well, and
`check_fields` cuts the cause of the errors it re-raises, so a traceback names
what was wrong and never what was sent.

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

One Workbench failure is outside the envelope on purpose: **a 401**. Every
authentication failure on a Workbench route — no cookie, an expired or
tampered one, an account that no longer exists — answers the single string body
`{"detail": "not signed in"}`, so a stolen cookie cannot learn that its account
was deleted. The client keys on the status (it signs out on any 401) and reads
this body as a legacy one, which is why the code table has no 401 row.

### Idempotency

Every Workbench mutation can be retried safely. A key is **scoped to the
caller**: the server matches it together with the authenticated GM and the
campaign, so a key someone else minted is a different key, and a request that
names it starts fresh rather than reading another caller's status or result
([threat model](adr/gm-workbench-threat-model.md), §12.2).

| Mutation | Key | A repeat… |
| --- | --- | --- |
| Tool invocation, AI edit | `invocation_id`, minted by the client | while working, reports status and starts nothing; once done, replays the stored result free of charge; after a retryable failure, starts a new attempt that passes the cost guards again (RAIL-18) |
| Reveal and audio commands | `command_id` | is recognised and is not a second command (AUDIO-24) |
| Field patch | the document's base **write revision** | conflicts only if a field it touches changed since (CANVAS-19, CANVAS-34). A repeat of a patch that already landed finds the fields equal to what it sends and is a no-op, not a conflict |
| AI edit after a conflict | the same `invocation_id`, a fresh `base_write_revision` | starts a new attempt on the new base; the body of a replay is otherwise ignored |
| Create a document | `command_id`, minted by the client | opens the document already made instead of making a second *Untitled NPC* |
| Restore, archive, unarchive | none needed | restoring what the document already equals changes nothing and creates no version; the others set a state |
| Create an asset, create a cue | `command_id`, minted by the client | opens the asset or cue already made; a retried upload sends its bytes to the same asset |
| Play a cue | `command_id`, and the audio epoch it was issued under (AUDIO-28) | replays the first outcome; a stale epoch is `409 conflict` and is never retried automatically |
| Stop a cue, Stop all | `command_id`, no epoch (X-3) | is idempotent by nature: a slot the cue no longer holds is left alone (AUDIO-9) |
| Confirm a reveal | `command_id`, and the **session** and reveal epoch it was composed under (REVEAL-5, ED-9) | replays the first outcome; a stale epoch, or an epoch from another session, is `409 conflict` and is never retried automatically — the sheet reloads live state, keeps the GM's draft and asks for a fresh Confirm (REVEAL-15) |
| Stop a reveal, Stop all reveals | `command_id`, no epoch (X-3) | is idempotent by nature: a document that is no longer live is left alone, and a Stop succeeds on an empty slot (REVEAL-22) |
| Start, End, Rotate a session | `command_id` | a retried Start opens the session already started rather than a second one; End and Rotate are idempotent on an ended or rotated session |

### Pagination

List responses are `{ "items": [...], "next_cursor": "<opaque>" | null }`, read
with `?cursor=` and `?limit=`. `next_cursor` is always present: the end of a list
is `null`, never a missing key. **A page may hold fewer items than were asked
for — including none at all — while `next_cursor` stays non-null: a short or
empty page is not the end of the list. Only a `null` cursor is; a client keeps
paging until it sees one.** A cursor is opaque to clients and is base64url
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
| Server | a *stored* entry with a field it does not declare | reads it the way a client reads a response, **undeclared keys ignored**: the table above lets a newer server add an optional field without a bump, and after a rollback that field must cost the entry nothing. What the server *emits* stays strict |
| Server | a *stored* entry it cannot validate — written by a newer server before a rollback, or damaged | serves an **`opaque` entry** in its place: same id, same position, a closed `reason` (`newer_version` when the entry's own version, or its embedded invocation's, is beyond this contract; otherwise `unreadable`), and **nothing of the payload**, because a server never forwards bytes it has not validated. The stored row is left untouched, so it renders again after a roll-forward. `entry_or_opaque` in `workbench_contracts.py` is that rule as code, for `1kg.4.2` to call; the row's own columns are the authority on identity and on time |
| Client | an additive field; an unknown error code | **tolerates** it: the field is stripped and never reaches a component |
| Client | a newer `schema_version`, an unknown kind, or a payload that does not validate | renders a neutral placeholder — *This result was made by a newer version of Aetheril.* — through `parseToolInvocation`, `parseToolResult`, `parseDocument`, `parseTimelineEntry`, `parseTimelinePage`, `parseGmEvent` and `parseTableEvent`. It never crashes the thread, and it never falls back to an NPC (X-8, RAIL-24). A page is read entry by entry, so one entry from a newer server becomes one placeholder and the rest of the thread renders (AE-43) |

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
| Per-type document fields | **done** | All eight types declare their fields, rules, reveal groups and default reveals (`1kg.5.3`). A key a type does not name fails closed — the same posture as card kinds |
| Reveal | **done** | `RevealAudience`, `RevealRequest`, `RevealStopRequest`, `RevealLive`, `RevealState`, `TableProjection`, the `slot` and `snapshot` kinds on both channels, and `keys` on the error envelope. See *The reveal family* below |
| Media assets and cues | **done** | `AssetCreateRequest`, `Asset`, `TableAssetRef`, `Cue`, `CueCreateRequest`, `CueRenameRequest`, `CueListQuery`, `CuePage`, `CuePlayRequest`, `CueStopRequest`. Storage, processing and serving are the media ADR's (`1kg.1.4`) |
| Table sessions | **done** | `TableJoinRequest`, `TableJoinResponse`, `EnrolRequest`, `EnrolResponse`, `TableSession`, `TableSessionRequest`, `TableSessionAnswer` |
| Realtime events | **done** | `GmEvent` (`tool_lane`, `edit_lane`, `session`, `audio`, `slot`, `snapshot`, `presence`, `asset`, `ready`, `reconnect`), `TableEvent` (`session`, `inactive`, `audio`, `slot`, `snapshot`, `ready`, `reconnect`), and the two snapshot resources `GmSnapshot` and `TableSnapshot`. `slot` and `snapshot` are the reveal family's; the transport is the media ADR's |
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
(`registry.json`: `field_kinds`, `field_bounds`, `common_fields`, and `fields`
per document type) kept as constants in both languages and pinned by both suites.
The numbers in the table below are `field_bounds`, so each one is agreed in one
place rather than kept twice.

| Kind | On the wire | Cleared as |
| --- | --- | --- |
| `text` | one line, at most 200 characters | `""` |
| `prose` | plain text, at most 20,000 characters | `""` |
| `text_list` | at most 100 items of 1 to 2,000 characters | `[]` |
| `asset` | an `AssetRef` (an id, never a URL) | `null` |
| `integer` | a JSON integer, -1,000,000 to 1,000,000; a **field** may narrow that, and most do — see *Per-field integer bounds* | `null` |
| `abilities` | the six 5e scores as one object (`str`, `dex`, `con`, `int`, `wis`, `cha`), each 0–99; any subset | `null` |
| `entry_list` | at most 100 `{name, text}` entries; the name one line of 1 to 200 characters, the text at most 2,000 | `[]` |

Every type has `name`, `qualifier` and `tags`. `name` is the title everywhere and
cannot be blank on any type (LIB-12); `tags` is **never revealable**, on any type
(REVEAL-10, ED-5).

A type may declare further **required** fields — a stat block's `ac` and `hp` are
the only ones (LIB-12: *"a stat block without them is not valid; nothing is
stored until they are given"*). A required field is one that must be **present
and not empty**, where *empty* is the *Cleared as* column above: `""` after the
contract's own trim for `text` and `prose`, `[]` for a `text_list` and an
`entry_list`, and `null` for an `asset`, an `integer` and an `abilities` block —
so a cleared cell is the same defect as an absent key, and `0` is a real armour
class. The **Required** column of each per-type table below says which they are.

That rule binds **writes**: a `DocumentCreateRequest` must carry every required
field, and a `FieldPatchRequest` may not set one to an empty value — while a
patch that does not mention a required field is untouched and raises nothing.
A **read** is tolerant of it, on both sides: LIB-12's words are about *storing*,
and a response that refused a stat block whose `hp` a data defect lost would show
the GM the *made by a newer version* placeholder for their own document.

**A kind never appears on the wire**, so a later bead can add kinds and fields
without a version bump:

- the **server** rejects a key the type does not declare, in what it stores and
  in what it emits;
- a **client** strips a key it does not know, so a type can grow;
- in a **request**, which the client builds, a stray key is an error on both sides.

A **stored** document is read the way a client reads a response — undeclared keys
ignored, everything else validated — by `read_stored_fields` in
`workbench_contracts.py`, which `1kg.5.1` and `1kg.5.2` call when they read a
stored row; **what the server stores and emits stays strict**. It is the same
rule the *Versioning and forward compatibility* table already carries for a
stored timeline entry, and it exists because *adding a field is not a bump*: a
strict stored read plus that rule would make every dossier that used a new field
unreadable after a rollback.

The largest valid document is a stat block, at **1,316,510** JSON characters:
five `entry_list` fields × 100 entries × 2,200 characters of name and text
(1,100,000), a 100-item `tags` list at 2,000 each (200,000), fifteen `text`
fields at 200 (3,000), and about 13,500 characters of keys, punctuation and the
envelope. Narrowing `ac` and `hp` to 0–1,000,000 took two characters off it. A
cap on the **whole** document is `1kg.5.5`'s (RAIL-6); this is the number it has
to sit above.

`type_version` says which revision of a type's field definitions `data` conforms
to. Both sides know version 1 of every type. A client that meets a newer one shows
a placeholder through `parseDocument` rather than half a document, and an unknown
`type` is never rendered as an NPC — which is what the handoff's registry fallback
did (X-8). Lookups on the client use `Object.hasOwn`: a key named `constructor`
must read as *not declared*, not find something on a prototype.

Nothing in this family says who may *see* a field. `reveal_mask`, audiences and
eligibility are not on a document at all: reveal is server state with its own
resource (CANVAS-33), and what may be shown to whom is
`agent-forge-harness-1ir.1.2`'s decision. What the **registry** says is the
per-field rule below — the allowlist a mask may ever name.

### Per-type fields

Every row of every table below is `registry.json`, which both suites pin against
their language's copy. **Required** is LIB-12: a write must carry the field,
present and not empty. **Revealable** is the type's allowlist (REVEAL-10): a key
marked *never* can appear in no mask, no reveal group and no default, and by
ED-5 it is `gm_only` by construction and cannot be widened by any action.
**Group** is the labelled row that toggles a fixed set of keys together
(REVEAL-11); the stored mask still lists the individual keys. **Seeded** is the
type's default reveal for its **own** audience, and only ever that one
(REVEAL-4) — so an owner-audience type seeds nothing for the table (AUD-12), and
the `audience` under each heading says **whose default reveal the type seeds**
and nothing about who may receive one: any type may be revealed to a participant
(ED-14, amendment A-19, owner decision O-2). A warning after a label is the
sub-line the reveal sheet shows, in the type's own words.

`reserved_keys` is empty for all eight: nothing has been retired yet. A key that
is retired goes on that list and never returns with another meaning (ED-24).

#### `npc` — NPC Dossier

renderer `game_document` · audience `table`

| Key | Kind | Label | Editable | Required | Revealable | Group | Seeded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `name` | common | Name | yes | yes | yes | *Name & voice* | yes |
| `qualifier` | common | Qualifier | yes | — | yes | — | — |
| `tags` | common | Tags | yes | — | **never** | — | — |
| `portrait` | `asset` | Portrait | yes | — | yes | — | yes |
| `voice` | `text` | Voice | yes | — | yes | *Name & voice* | yes |
| `tell` | `text` | Tell | yes | — | yes | — | — |
| `attitude` | `text` | Attitude | yes | — | yes | — | — |
| `wants` | `prose` | Wants ⚠ Would spoil the lie | yes | — | yes | *Wants & leverage* | — |
| `leverage` | `prose` | Leverage ⚠ Would spoil the lie | yes | — | yes | *Wants & leverage* | — |
| `if_attacked` | `prose` | If the party attacks | yes | — | yes | — | — |
| `notes` | `prose` | Notes | yes | — | yes | — | — |
| `true_identity` | `prose` | True identity | yes | — | **never** | — | — |

#### `statblock` — Stat Block

renderer `stat_block_card` · audience `table` · ability row from `abilities`

| Key | Kind | Label | Editable | Required | Revealable | Group | Seeded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `name` | common | Name | yes | yes | yes | — | — |
| `qualifier` | common | Qualifier | yes | — | yes | — | — |
| `tags` | common | Tags | yes | — | **never** | — | — |
| `ac` | `integer` | Armor Class | yes | yes | yes | — | — |
| `ac_note` | `text` | Armor Class note | yes | — | yes | — | — |
| `hp` | `integer` | Hit Points | yes | yes | yes | — | — |
| `hit_dice` | `text` | Hit dice | yes | — | yes | — | — |
| `speed` | `text` | Speed | yes | — | yes | — | — |
| `size` | `text` | Size | yes | — | yes | — | — |
| `creature_type` | `text` | Creature type | yes | — | yes | — | — |
| `alignment` | `text` | Alignment | yes | — | yes | — | — |
| `abilities` | `abilities` | Ability scores | yes | — | yes | — | — |
| `saving_throws` | `text` | Saving throws | yes | — | yes | — | — |
| `skills` | `text` | Skills | yes | — | yes | — | — |
| `damage_immunities` | `text` | Damage immunities | yes | — | yes | — | — |
| `condition_immunities` | `text` | Condition immunities | yes | — | yes | — | — |
| `senses` | `text` | Senses | yes | — | yes | — | — |
| `languages` | `text` | Languages | yes | — | yes | — | — |
| `challenge_rating` | `text` | Challenge rating | yes | — | yes | — | — |
| `xp` | `integer` | XP | yes | — | yes | — | — |
| `traits` | `entry_list` | Traits | yes | — | yes | — | — |
| `actions` | `entry_list` | Actions | yes | — | yes | — | — |
| `bonus_actions` | `entry_list` | Bonus actions | yes | — | yes | — | — |
| `reactions` | `entry_list` | Reactions | yes | — | yes | — | — |
| `legendary_actions` | `entry_list` | Legendary actions | yes | — | yes | — | — |

#### `handout` — Player Handout

renderer `game_document` · audience `table` · **printable**

| Key | Kind | Label | Editable | Required | Revealable | Group | Seeded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `name` | common | Name | yes | yes | yes | — | yes |
| `qualifier` | common | Qualifier | yes | — | yes | — | — |
| `tags` | common | Tags | yes | — | **never** | — | — |
| `portrait` | `asset` | Illustration | yes | — | yes | — | yes |
| `body` | `prose` | Text | yes | — | yes | — | yes |

#### `session-notes` — Session Notes

renderer `game_document` · audience `table`

| Key | Kind | Label | Editable | Required | Revealable | Group | Seeded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `name` | common | Name | yes | yes | yes | — | — |
| `qualifier` | common | Qualifier | yes | — | yes | — | — |
| `tags` | common | Tags | yes | — | **never** | — | — |
| `session` | `integer` | Session number | yes | — | yes | — | — |
| `date` | `text` | Date | yes | — | yes | — | — |
| `present` | `text_list` | Present | yes | — | yes | — | — |
| `recap` | `prose` | Recap ⚠ Summarises your private GM thread | yes | — | yes | — | — |
| `beats` | `text_list` | Beats | yes | — | yes | — | — |
| `loose_threads` | `text_list` | Loose threads | yes | — | yes | — | — |

#### `quest-log` — Quest Log

renderer `game_document` · audience `table`

| Key | Kind | Label | Editable | Required | Revealable | Group | Seeded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `name` | common | Name | yes | yes | yes | — | yes |
| `qualifier` | common | Qualifier | yes | — | yes | — | — |
| `tags` | common | Tags | yes | — | **never** | — | — |
| `open_threads` | `entry_list` | Open threads | yes | — | yes | — | yes |
| `cold_threads` | `entry_list` | Cold threads | yes | — | yes | — | — |
| `resolved_threads` | `entry_list` | Resolved threads | yes | — | yes | — | yes |

#### `character-sheet` — Character Sheet

renderer `game_document` · audience `owner` · ability row from `abilities`

| Key | Kind | Label | Editable | Required | Revealable | Group | Seeded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `name` | common | Name | yes | yes | yes | — | yes |
| `qualifier` | common | Qualifier | yes | — | yes | — | yes |
| `tags` | common | Tags | yes | — | **never** | — | — |
| `portrait` | `asset` | Portrait | yes | — | yes | — | yes |
| `ac` | `integer` | Armor Class | yes | — | yes | — | yes |
| `hp` | `integer` | Hit Points | yes | — | yes | — | yes |
| `speed` | `text` | Speed | yes | — | yes | — | yes |
| `abilities` | `abilities` | Ability scores | yes | — | yes | — | yes |
| `features` | `entry_list` | Features | yes | — | yes | — | yes |
| `equipment` | `text_list` | Equipment | yes | — | yes | — | yes |
| `notes` | `prose` | Notes | yes | — | yes | — | yes |

#### `lore` — Lore Entry

renderer `game_document` · audience `table` · **cites the corpus** · accent `arcane`

| Key | Kind | Label | Editable | Required | Revealable | Group | Seeded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `name` | common | Name | yes | yes | yes | — | yes |
| `qualifier` | common | Qualifier | yes | — | yes | — | — |
| `tags` | common | Tags | yes | — | **never** | — | — |
| `region` | `text` | Region | yes | — | yes | — | — |
| `era` | `text` | Era | yes | — | yes | — | — |
| `status` | `text` | Status | yes | — | yes | — | — |
| `summary` | `prose` | Summary | yes | — | yes | — | yes |
| `history` | `prose` | History | yes | — | yes | — | — |
| `rumours` | `text_list` | Rumours | yes | — | yes | — | — |

#### `encounter` — Encounter

renderer `game_document` · audience `table`

| Key | Kind | Label | Editable | Required | Revealable | Group | Seeded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `name` | common | Name | yes | yes | yes | — | — |
| `qualifier` | common | Qualifier | yes | — | yes | — | — |
| `tags` | common | Tags | yes | — | **never** | — | — |
| `difficulty` | `text` | Difficulty | yes | — | yes | — | — |
| `xp_budget` | `integer` | XP budget | yes | — | yes | — | — |
| `party_level` | `integer` | Party level | yes | — | yes | — | — |
| `setup` | `prose` | Setup | yes | — | yes | — | — |
| `combatants` | `entry_list` | Combatants | yes | — | yes | — | — |
| `terrain` | `prose` | Terrain & hazards | yes | — | yes | — | — |
| `outcome` | `prose` | If it goes wrong ⚠ Would spoil the surprise | yes | — | yes | — | — |

#### Per-field integer bounds

A table of its own rather than a ninth column above: `bounds` is a pair of
numbers, not a per-row yes or no, and it is absent on 59 of the 66 field rules.
It is checked **after** the kind's own range, never instead of it — the kind
says what an integer is at all, the field says what this use of one may mean —
and the registry refuses a `bounds` on a field that is not an `integer`, one
whose lowest is above its highest, or one outside the kind's own range.

| Field | Lowest | Highest |
| --- | --- | --- |
| `statblock.ac` | 0 | 1,000,000 |
| `statblock.hp` | 0 | 1,000,000 |
| `statblock.xp` | -1,000,000 | 1,000,000 |
| `session-notes.session` | 1 | 1,000,000 |
| `character-sheet.ac` | 0 | 1,000,000 |
| `character-sheet.hp` | 0 | 1,000,000 |
| `encounter.xp_budget` | 0 | 1,000,000 |
| `encounter.party_level` | 1 | 1,000,000 |

`statblock.xp` is the one integer field deliberately left at the kind's own
range. It is what keeps `-1,000,000` reachable through a declared field at all,
and so keeps the two boundary examples in `Document.json` that pin the kind's
floor honest. An armour class is not negative, and there is no session 0 or
party level 0; experience points nothing consumes yet are not worth making the
kind's floor untestable for.

Narrowing a field's bounds is an **incompatible change**: see *Schema
revisions* below.

#### Schema revisions

`DOC_TYPE_VERSION` is `1` for all eight, and nothing is released, so no document
needs carrying forward. The frame for when one does is
`service/workbench_adapters.py` and `ui/src/gm/schemaAdapters.ts`: a registry of
`(type, from_version) -> adapter`, walked one step at a time. It checks that
every step exists **before** running any, so a document half-migrated by a
skipped step is never returned; it refuses to downgrade a document newer than
the target; and its error names the step, never the document's content (X-7).
Adding a field or a kind is **not** a bump. But
**making a declared field required**, narrowing a field's bounds, or retiring a
key **is** an incompatible change: it bumps `type_version` and ships its adapter. That is what lets a read
of an already-stored document ignore an undeclared key while still applying every
bound and every kind rule — a document written before such a change sits at an
older `type_version`, so the adapter walk reaches it before anything reads it.
All eight types are still at `type_version` 1: nothing is released, so nothing
this contract has done so far is a bump. An adapter that moves text from one key to another
**resets the destination to `unclassified`** (ED-24): a class was granted for the
text as it sat under the old key, and carrying it across would cover text the
classifier never saw.

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
| `DocumentCreateRequest` | New in a library category (LIB-12): a `command_id`, the campaign, the type and every field the type **requires** — for a stat block, a name, an AC and an HP | the `Document` |
| `RestoreRequest` | a version number (CANVAS-26). Additive, so it needs no base | the `Document`, whose version says `restored_from` |
| `LibraryQuery` | one page of one category (LIB-20 to LIB-23). A request **body** even without a search, because search text may never travel in a URL (X-7) | a `LibraryPage` that echoes the campaign and category it answers, so a stale response is dropped (LIB-25) |

A write always answers with the whole document: a dossier is small, and a client
that has the whole truth has nothing to reconcile. What it must still do itself is
never let a save response overwrite newer local text (CANVAS-10).

`check_fields` in `workbench_contracts.py` is the validator behind all of these.
`1kg.5.2` and `1kg.5.5` call it on a merged document before committing it
(CANVAS-19). Its messages name keys and kinds and never quote a value. Called as
a write does — `whole=True` and nothing else — it enforces the type's required
fields; `read_stored_fields` and the two response models opt out of that one rule
and of nothing else.

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
  construction instead of by cursor arithmetic. That guarantee is why a
  `TimelinePage` can come back short, or even with `items: []`, while
  `next_cursor` is still non-null: skipping a whole legacy window is sometimes
  the only way to avoid splitting one exchange across a page (see
  *Pagination* above).
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
client reads. They are also unbounded where this contract is bounded (a source's
snippet, a stat block's trait), which is accepted: they are filled by the
service from model output, never by a client, and a page of 100 entries at every
bound this contract does set (two texts of 100,000 characters each) parses on
the client in well under a second.

What an entry can never carry: a document body (a result links, EXPORT-12), the
prompt the server assembled, attachment text, a provider payload, or the owner's
user id. The server models forbid undeclared fields, and a client strips them.

Entry-to-component adapters are `1kg.3.4`'s; the lane pieces they need
(`toLaneStatus`, `toLaneSuggestions`) already exist in `adapters.ts`.

## The media family

Bytes travel in **two steps** ([media ADR](adr/gm-workbench-media-and-realtime.md), MS-3 to
MS-5). First a JSON `AssetCreateRequest` — the kind, the declared media type, the
declared size and, for an image, its alt text — which the server answers with an
`Asset` in state `uploading` after checking the quota (SEC-31) and the caps
(SEC-26). Then one raw request carries the bytes: `Content-Type` is the declared
type, `Content-Length` is required (411 without it), the body is streamed and
counted, and there is no multipart and no base64. The asset goes `processing`,
then `ready` — measured, re-encoded, every tag and picture dropped (SEC-27) — or
`failed` with a reason from a closed set and never a message. Nothing about an
asset resolves until it is `ready`; a deleted asset is a 404 like anything else
missing.

| Kind | Accepted on upload (by magic bytes) | Served as | Caps |
| --- | --- | --- | --- |
| `image` | PNG, JPEG, WebP | the same family, re-encoded | 10 MB, 25 megapixels, 8,192 px a side |
| `audio` | MP3, M4A, OGG, WAV | `audio/mpeg` | 20 MB; ambience 10 minutes, one-shots 30 s (checked when the cue is made) |

A document field of kind `asset` still holds an `AssetRef` — an id, a type, alt
text — and never a URL (X-10). A **table client** never sees an `asset_id`: it is
given a `TableAssetRef`, a per-slot opaque handle with the type and the
dimensions or duration a player needs to lay the asset out, and nothing that
names it (SEC-15, AUDIO-29). The handle dies with its slot.

A **cue** (LIB-26) is a title and an immutable kind — `ambience` or `one_shot`
(AUDIO-1, AUDIO-4) — over a ready audio asset. `CueListQuery` is the Cues
category of the library, with the library's search and sort rules. `CuePlayRequest`
carries the audio epoch (AUDIO-28) and a `start_offset_ms` that is `0` today and
stays for forward compatibility (AUDIO-8); `loop` on a one-shot is refused
against the cue's kind (AUDIO-3). `CueStopRequest` names a cue, or `null` for
Stop all (AUDIO-9), and carries no epoch, because a Stop is never stale (X-3).

## The table-session family

Four bearer secrets exist (threat model §6.2); two of them are on the wire, each
exactly once, in a POST body: the **table token** a table link carries and the
**enrolment code** a personal link carries. Both are 32 CSPRNG bytes as
`secrets.token_urlsafe` spells them — 43 base64url characters — and the contract
pins that length. Neither ever appears in a URL the server sees (REVEAL-19).

| Request | Answer |
| --- | --- |
| `TableJoinRequest` — the token | `TableJoinResponse`: `joined` with the device's role (`participant`, or `guest` with TABLE-13's line), `full` (SEC-10), or `inactive` — the one answer for a wrong, ended, expired or rotated token (TABLE-9). No reason, ever |
| `EnrolRequest` — the code | `EnrolResponse`: `enrolled` or `inactive` (TABLE-16). No reason, ever |
| `TableSessionRequest` — `start`, `end` or `rotate`, idempotent by `command_id`; only `rotate` may also reset personal links (REVEAL-17) | `TableSessionAnswer`: the `TableSession` and, for a start or a rotation, the new token — the one time a token is in a body. An end carries `null` |

`TableSession` is the GM's view: state, link generation, when it started and
ends, whether table audio is on, and how many devices hold a credential. It never
carries the token: a token is not re-readable.

## The realtime family

The transport is server-sent events (media ADR, RT-1 to RT-4): one stream per GM
tab on the campaign, one per table device, every command a POST. Each frame is
one event whose `data:` is one JSON object of this family, with its own
`schema_version`; the heartbeat is an SSE comment line, not an event. The
client reads the stream with `fetch` and a `ReadableStream`, not the native
`EventSource` — which hides comment lines, response statuses and headers, so
neither the heartbeat, nor a refused stream (an HTTP status with
`Retry-After`), nor TABLE-7's *silence from the last byte* would be observable
through it. A stream opens with the same frames its channel's **snapshot
resource** (`GmSnapshot`, `TableSnapshot`) answers with — ending with `ready`,
the boundary before which nothing is trusted — and is closed by the server
between 240 and 280 s with a `reconnect` frame, after which the client reopens
with jittered backoff and takes a fresh snapshot (TABLE-7, AUDIO-15). The
snapshot resource is also the polling mode when no stream can be opened.

Two channels, two unions, because they may not carry the same things (SEC-15,
threat model §8.3):

| Channel | Kinds | Carries |
| --- | --- | --- |
| GM, `GmEvent` | `tool_lane`, `edit_lane`, `session`, `audio`, `slot`, `snapshot`, `presence`, `asset`, `ready`, `reconnect` | lane status with the embedded invocation; the `TableSession` with **both** epochs; audio slots by cue id and title, with the audio epoch two GM tabs converge on (AUDIO-24); presence with participants' aliases and guest counts (AUDIO-21); an asset's state change (MS-3); **one reveal slot with its `RevealLive`, the reveal epoch and the generation, and the whole reveal picture as `RevealState`** |
| Table, `TableEvent` | `session`, `inactive`, `audio`, `slot`, `snapshot`, `ready`, `reconnect` | whether table audio is on and this device's own role; the one generic inactive event, after which the connection closes (TABLE-9); audio slots by handle, never a title (AUDIO-29); **one reveal slot as `table` or `mine` with its projection, and the opening picture of the slots this device is entitled to**. No generation, no epoch, no disclosure id, no id a table client has no use for (SEC-15) |

Every audio frame names its slot and the slot's sequence (AUDIO-15: per slot,
monotonic, assigned by the database): a client applies a frame only above its
mark for that slot. A GM frame also names the link generation it was produced
under (SEC-9); a table frame does not, because it is the server that writes a
frame only to a stream of the same generation, and the number is nothing a
table client needs. A one-shot never
loops and is at most 30 s, on both channels. The `snapshot` and `slot` kinds are
the reveal family's, and both channels now carry them — see *The reveal family*.
Adding them before v1 was declared complete was not a version bump;
`parseGmEvent`, `parseTableEvent`, `parseGmSnapshot` and `parseTableSnapshot`
read an unknown kind as a placeholder, which is what made that safe.

## The reveal family

The family through which GM-private text could reach a player, so its shapes are
a security boundary rather than a convenience. Two rules govern all of it. First,
**a projection is built, never filtered** (SEC-14): one server-side builder takes
a sealed version, a mask and an audience and emits only allowlisted keys, and the
player-safe export and print call the same builder (EXPORT-3, EXPORT-7). Second,
**a table client is told less than the GM at every turn** — no epoch, no link
generation, no session id, no participant id, no sequence of a slot it is not
entitled to (SEC-15, REVEAL-24).

### What may be revealed

A mask names **explicit field keys**, never a wildcard: `all` is expanded by the
client into the keys that exist at the moment the GM decides, so a field added to
a type later is never revealed by an old wildcard (REVEAL-9, ED-8). Because `all`
matches the field-key shape, it is refused by name wherever a mask key is
expected; `*` and `%` never matched it.

The revealable set is a registry fact, and it is an **allowlist** with exactly
one source — `1kg.5.3`'s per-field rule:

```
revealable(type) = { key ∈ common_fields ∪ type.fields | rule(type, key).revealable }
```

A key whose rule does not say `revealable` is not revealable. That is the whole
answer to *may this field reach a player*: there is no second list, no opt-out
and no default, so a field a type gains later is withheld until the registry
says otherwise, and `tags` — the only entry of REVEAL-10's never-list that is a
document *field* at all — is off it on every type. `npc.true_identity` is ED-20's
worked case: a field the type declares, that the GM edits, and that no mask and
no projection can name.

Both contract modules hold the allowlist as a constant, because the registry
module imports them and cannot be imported back; **both suites pin the constant
to `registry.json` for every type**, so the copy cannot answer differently from
the rule it copies.

### The mutations

| Shape | Says | Answered with |
| --- | --- | --- |
| `RevealAudience` | `table`, or **one or more participants by id** — an identity, never a credential and never an alias (AUD-2, AUD-11, ED-10). A reveal to one player is a list of one; there is no singular shape. Nothing ties an audience to a document type: under owner decision O-2 a participant audience is legal for **any** type, and the registry's `audience` flag now says only whose default reveal a type seeds | — (it is a member, not a request) |
| `RevealRequest` | Confirm: the document, the **sealed** version the sheet displayed, the mask, the audience, and **both** the session it was composed for and that session's reveal epoch (REVEAL-5, ED-9). One shape covers reveal, update, replace and move — the server derives which. It names a session so that a number from last night can never match tonight. No campaign id: the session names the campaign and ownership is the route's (SEC-3) | **`RevealState`** — the GM's whole reveal picture, the new epoch included |
| `RevealStopRequest` | Stop showing, by `scope`: a `document`, or `all` (REVEAL-6, REVEAL-7). **There is no slot scope** — see below. **No epoch on any Stop** (X-3): a narrowing is never stale, never queued and never refused for state, so there is no number to be stale against, and sending one is an error. There is no Retract in v1 (ED-16) | **`RevealState`**, the same |

**Why both are answered with the picture.** REVEAL-22 advances the reveal epoch
on *every* narrowing, "on an empty slot too" — a Stop with nothing live, a
Rotate with nothing live, a participant removed. No slot changed, so there is no
`slot` frame to carry the new number, and one re-sent with an unchanged `seq`
would be dropped by RT-4's per-slot mark. Answering the mutation with the
picture means **the acting tab can never trip over its own narrowing**: it has
the new epoch before it composes its next Confirm, so an ordinary
stop-then-reveal is not a 409.

Other tabs converge three ways, and all three are needed: the server re-sends
the GM `snapshot` frame after an **epoch-only** advance; a GM client takes
`reveal_epoch` from **every** `slot` and `snapshot` frame regardless of its
per-slot marks, because the epoch is a property of the session and not of the
slot; and `TableSession` carries `reveal_epoch` beside `audio_epoch`, so a tab
that reloads the session resource has it too (AUDIO-24's twin).

**Group displays, and what a disclosure is** (owner decision O-3, amending §7.1,
REVEAL-7 and NG-20). Showing one document to several players is
**per-recipient copies of one disclosure**: one Confirm names the recipients,
the server fills one slot per recipient, and every copy carries the same
`disclosure_id`. A **document has at most one live disclosure**, and a
disclosure is *either* the table slot alone *or* one or more participant slots —
never both, which would make *stop all copies* ambiguous and let a private copy
be mistaken for the one everybody can see. A Stop on the document therefore
clears every copy by construction.

A **named group is expanded by the client into its member ids** at the moment
the GM confirms, exactly as `all` is expanded into field keys (ED-8). No group
id and no wildcard ever travels or is stored, so a group whose membership
changes tomorrow cannot silently widen a reveal that is live tonight. An
audience may name many participants; a **slot** names one, which is why
`RevealSlotRef` is its own shape.

**Why a Stop names a document and not a slot.** REVEAL-22 is *a Stop clears a
slot only if it holds what the Stop names*; a slot-scoped Stop names an audience
and nothing else, so it could not honour that. Since a Stop is retried until it
is acknowledged (REVEAL-16), tab A's retry would clear whatever tab B had
deliberately revealed into that slot meanwhile — the "kill a later, deliberate
reveal" REVEAL-16 rules out. Naming the document makes both rules hold by
construction: a GM client always knows the document, because every slot's
document id is in its reveal picture, and a document has **at most one live
disclosure** (O-3, amending ED-15's "at most one slot"), so *what the Stop
names* is unambiguous however many copies that disclosure has. The `document`
scope exists so that the canvas header can stop *that document* without knowing
which slots hold it, and `all` is the workspace indicator's panic button.

### What the GM sees

`RevealLive` is what one slot holds: the document, the pinned version, the mask,
`stale_text`, `pending_delivery` and its `disclosure_id`. `stale_text` is
REVEAL-8's notice, and the comparison behind it is **of text, not of version
numbers** — ten autosaves raise one notice and reverting the text clears it.

`pending_delivery` is AUD-10: a reveal to a participant who is **not enrolled,
or enrolled and not currently connected** confirms normally and waits, and never
falls back to the table. Both cases are the same flag, because they are the same
fact for the GM — *nobody is reading this yet* — and telling them apart on the
wire would let a sheet report who is online from a reveal (AUD-11); the GM reads
who is connected from the presence frame, which is where that belongs.

`RevealState` is the whole picture — session, generation, epoch, one entry per
slot — and rides the GM channel's `snapshot` frame and nothing else. The threat
model's §8.3 row is the rule: *reveal state, with the epoch, every slot, version
numbers and mask keys* is GM **yes**, participant **never**, guest **never**.

The **table slot is always listed**: "nothing revealed" is the table slot,
present and empty, never an absent entry, because a GM client must not read
missing state as *nothing revealed* (REVEAL-13). Each entry names **one** slot,
a slot is listed once, and the disclosure rules above are enforced here: every
entry of one document carries the same `disclosure_id`, one `disclosure_id`
belongs to one document, and no disclosure is on the table and in a private slot
at once. `pending_delivery` stays **per entry**, because one recipient may be
waiting for a device while the others holding copies are not (AUD-10).

Nothing of this reaches a table client. A player's frames name `table` or
`mine`, carry no `disclosure_id`, no recipient count and no other participant's
id, so a private display never says that it is one copy of several (REVEAL-24).

### What a player sees

`TableProjection` carries `content_kind`, the type, and one entry per masked key
holding that key and its value — **and nothing else**. The heading is not on the
wire: the table client renders the registry's label for `(type, key)`, which its
bundle already holds. That is deliberate. Every other member of a projection is
closed — a literal, an enum, a key on the type's allowlist, a value checked
against that key's kind — and a free-text member, however tightly bounded, would
be the one place a document's title, a participant's alias, an asset's filename,
a version or an id could ride to a table with every gate green. The page title is
still built from the projection, so a document's name reaches a player only when
`name` is masked (TABLE-3).

**The emitter is the confidentiality boundary.** Every refusal above is a
refusal *by the server*: by the time a table client's Zod schemas run, the bytes
are already on the device, so what the client does with an undeclared key —
strip it, as the versioning table requires of every response — cannot be the
guard. Both halves are pinned: the server refuses each smuggled key (the
`applies_to: ["server"]` fixtures, `extra="forbid"` at every level) and the
client's strip is asserted at every depth, by feeding what it kept back through
the strict server model. Two requirements follow for `1kg.7.2`:

- **Every emitted table frame is validated against its Pydantic model before it
  is sent.** The model is the last thing between a builder's mistake and a
  player's screen, and it only helps if the route runs it.
- **The canary suite (T-1) asserts on the raw response bytes, never on a parsed
  result.** A parse is exactly the step that would hide a smuggled key, so a
  canary that reads one proves nothing.

A masked key is present and non-empty in the pinned version (REVEAL-5, ED-9), so
nothing arrives as a blank heading: text and prose are non-blank after trimming,
a list has at least one item, an `integer` is a number rather than `null`, an
ability block holds at least one score and no `null`s, and an entry carries both
its name and its text. An `asset` value is a **`TableAssetRef`** — a per-slot
opaque handle that dies with its slot — never the GM-side `AssetRef`, which
carries an `asset_id` that would let two slots showing one portrait be correlated
(SEC-15, REVEAL-21).

`content_kind` has exactly **one** member in v1, `document`. Adding a member
later **is** a version bump; what reserving the discriminator buys is that a v1
table client meets an unknown kind as its **neutral placeholder** rather than as
a parse failure. That is why `TABLE_EVENT_DISCRIMINATORS` names the kind at both
paths it can arrive on — a `slot` frame's `content`, and a `snapshot` frame's
`slots[].content` — and why the path walker gained an array segment to reach the
second.

**A table client that cannot read a `slot` or `snapshot` frame blanks the
revealed content it holds for that slot and shows the placeholder; it never
keeps showing what it had** (X-4). So the placeholder carries what is still
readable: `parseTableEvent` answers `{ kind: 'unknown', reason, slot, seq }`
whenever the slot name and the sequence parse, and per-entry results for a
`snapshot` frame, so one unreadable entry does not discard the readable table
slot beside it. This is not a future-version concern only: a table client checks
a projection's keys against its **own** copy of the field definitions, so a
server one deploy ahead of a table bundle — a type gained a revealable field,
which this document's own table calls *no bump* — makes a frame unreadable
today. Without the slot name the page has nothing to blank, the natural
implementation skips the frame, and document A stays on the player's screen
while the GM's indicator says B.

### The frames

| Channel | Kind | Carries |
| --- | --- | --- |
| GM | `slot` | the session, the link generation, the reveal epoch, the slot as a `RevealSlotRef`, its sequence and `RevealLive` or `null` — the twin of `GmAudioEvent` |
| GM | `snapshot` | `RevealState`: the whole picture in one frame |
| Table | `slot` | the slot as **`table` or `mine`**, its sequence, and a `TableProjection` or `null` — the twin of `TableAudioEvent`, and nothing else |
| Table | `snapshot` | one or two slots: the table slot, always, and with the enrolled device credential this device's own. It is the **opening** frame only |

A table `snapshot` frame is sent when a stream opens and when a snapshot
resource is read, and at no other time: **every later change is a `slot` frame,
to the clients entitled to that slot**. A service that re-broadcast the whole
picture on each change would hand a guest a no-op frame every time a private
reveal happened, and a frame that arrives whenever something invisible changes
is exactly the inference channel WT-7 and T-8 rule out.

A table client is never told a participant id. Every table-side shape in this
contract is already id-free — `TableRole` is an enum, `TableJoinResponse` answers
with a role and no id, `EnrolResponse` with a status alone — and which
participant `mine` is, the server resolves from the credential pair, *never from
request fields*. A slot a device is not entitled to is **absent**, never marked:
a marker would confirm both that the slot exists and that a private reveal is
happening (WT-7, T-8, threat model §8.2).

**The role decides the slots.** A `TableSnapshot` carries exactly one `session`
frame while live — two could disagree about the role — and that frame's `role`
is what says whether a `mine` slot may appear at all. A **guest** holds the table
slot and nothing else: no `mine` in the picture, and no later `mine` `slot`
frame either. An enrolled **participant**'s picture is exactly `table` and
`mine`, because for an entitled device *absent* and *present and empty* are
different facts and only the second is legal — otherwise a page cannot tell
"nothing is revealed to me" from "I was not told". This is the entitlement rule
the family is built on, and it is checked where `1kg.7.2` validates what it is
about to emit, so a snapshot route that resolved a revoked device as a guest
(TABLE-13) and still attached its slot cannot send it.

Both snapshot resources carry **one** reveal picture while their session is live
and **none** when there is none — `GmSnapshot`'s *no session running* and
`TableSnapshot`'s *inactive table* (TABLE-9) are unchanged by this family. A
snapshot is complete before `ready`, so a client that has seen `ready` knows
every slot it is entitled to, and a GM client never reads missing state as
"nothing revealed" (ADR RT-4, REVEAL-13).

**A dead table projects nothing, in every frame that can carry a projection.**
A `TableSnapshot` is **live** when it carries a `session` frame **and no
`inactive` frame** — `TableSessionEvent` exists only while live, and `inactive`
is what a dead table says (TABLE-9). Both facts, and neither order nor count
changes either: an `inactive` frame kills the resource wherever it sits in the
list and however often it is repeated. That one predicate is what the picture
rule and the frame rules below read, in the Pydantic model, in the Zod schema
and in the readers.

Ending, expiring or rotating a link *clears every projection* (REVEAL-17,
AE-51), so a `TableSnapshot` that is not live carries no reveal picture, no
`slot` frame holding content, and no `mine` `slot` frame at all. An **empty**
`table` `slot` frame stays legal, because reporting that a region holds nothing
is what a cleared table looks like: `[inactive, slot(table, …, content: null),
ready]` is a valid resource. There is no role on a dead resource, so the
entitlement rule above never runs over it and these rules are the only ones that
can. The scope is projections; a table `audio` frame is governed by the audio
family and survives an inactive resource on both sides, so a table client that
has gone inactive stops its own ambience rather than inferring it from this
rule.

An `inactive` frame and a `session` frame are **mutually exclusive** in a
well-formed resource, and a resource carrying both is refused on its own clause.
This is the confused emitter the rule exists to catch: a snapshot route
answering a link the GM has just rotated (SEC-9, TABLE-13) builds the `inactive`
frame and then appends the head frames it had buffered for the session it was
serving — session frame included — and were the session frame alone the test,
that resource would read as live and switch off *every* rule here, carrying the
projection in the reveal picture as readily as in a `slot` frame. It is refused
where `1kg.7.2` validates what it is about to emit, so neither the buffered
slots nor the buffered picture can be attached.

A `GmSnapshot` carries **one** `session` frame, and its reveal picture describes
**that** session: `GmSnapshot` refuses a picture whose `session_id` or `gen`
differs from the session frame beside it. The reveal epoch is per session
(ED-9), so a picture from another session — or from a generation before a
Rotate — is exactly the "number from last night" a Confirm must never be able to
match.

The **readers apply the reveal-picture rule too**, not only the emitter:
`parseGmSnapshot` and `parseTableSnapshot` answer
`{ kind: 'unknown', reason: 'invalid' }` for a live resource with no picture,
for a resource with no live session that carries one, and for two pictures.
Without this a GM tab in RT-9's polling mode would read a snapshot whose picture
failed to build as `ok`, find no `snapshot` frame, and render *nothing revealed*
while the table shows a dossier.

`parseTableSnapshot` applies the **liveness rule** above as well — the same
predicate, the same two dead-resource clauses and the same mutual exclusion — so
a resource the two models refuse for liveness is a resource the reader refuses.
A table client is told elsewhere in this contract to blank a slot it cannot
read; a reader that answered `ok` for `[inactive, slot(mine, …), ready]` would
hand it the projection instead of something to blank. One rule, three places
that agree.

Both are counted on the raw `event` values, so a picture this bundle cannot
parse still counts as a picture and becomes one placeholder inside an otherwise
readable snapshot. The liveness clauses are read the same way, with one
deliberate softness: a `slot` frame with **no** `content` key at all shows
nothing, so it becomes one placeholder rather than making the whole resource
unreadable. The models refuse that frame anyway — `content` is required and
nullable on both sides.

### Refusals

A mask the server will not accept is a **422** whose error names the keys at
fault in `keys` — **keys only, never their text**, because an error body is where
logs and traces look (X-7), and the field-key shape makes prose unrepresentable.
It is an additive field and no version bump. A stale epoch is `409 conflict` and
is never retried automatically: the sheet reloads live state, keeps the GM's
draft and asks for a fresh Confirm (REVEAL-15, REVEAL-22).

**There are no eligibility fields in v1.** Workbench v1 ships mask-only: the GM's
explicit Confirm, which names every field it shows and previews its exact text,
is the authorisation (ED-11, X-2). Eligibility binds reveal from `1ir.11.1`, and
the refusal it needs is an **additive error code**, which this contract's own
rules allow without a bump. A suite test asserts that no schema declares a class
or a revision, and another that no request shape a table client can send names a
participant.

### Amendments the interaction record needs

These shapes are consistent with `docs/adr/gm-workbench-interactions.md`, but
several of its rows are now less precise than the contract they govern, and two
were amended by the owner on 2026-09-20. The lead applies these to the record;
this bead only lists them.

1. **REVEAL-5** says a Confirm carries "the document, the sealed version, the
   explicit mask, the audience and the reveal epoch". It must also say **the
   session** — ED-9 added it so that an epoch from an earlier session can never
   match, and the wire now requires it. The record should also say what a
   Confirm and a Stop are **answered with**: the GM's reveal picture, the new
   epoch included, so the acting tab cannot trip over its own narrowing when
   REVEAL-22 advances the epoch without changing a slot.
2. **REVEAL-6** describes Stop as stopping "that document" or "every reveal".
   The wire has **two** scopes and no slot scope, and the record should say why:
   REVEAL-22 makes a Stop clear a slot only if it holds what the Stop names, and
   a scope that named an audience alone could not honour that under REVEAL-16's
   retries. The `document` scope exists so that the canvas header can stop *that
   document* without knowing which slot holds it.
3. **REVEAL-8** is the source of the rule that the comparison is of text, not of
   version numbers. The GM-side field is named `stale_text` for that reason,
   while the `1kg.1.6` alignment calls it "whether a newer version exists". The
   record should carry the field name so the two readings cannot drift apart.
   **This is the one place where the alignment's wording and the ADR differ, and
   the ADR was followed.**
4. **AUD-8** says "one table slot, plus one private slot per participant". The
   table channel names them `table` and `mine`, because a slot reference is an
   id and a table client is told no ids (SEC-15); the record should say so, or a
   reader will expect `participant:<id>` on both channels.
5. **TABLE-3** says a table client renders the labels it is given. It is the
   other way round: a projection carries `key` and `value` only, and the client
   renders **the registry's label** for `(type, key)`. The record should say so,
   because the reason is a security one — a free-text member is a channel
   through which a title, an alias or a filename would pass every gate.
6. **§7.1, REVEAL-7 and NG-20** are amended by **owner decision O-3**: group
   displays ship, as per-recipient copies of one disclosure. A document has at
   most one live disclosure; a disclosure is either the table slot or
   one-or-more participant copies, never both; a Stop on the document clears
   every copy. §7.1's "a document is live in at most one slot" no longer holds
   as written.
7. **AUD-9 and NG-22** are amended by **owner decision O-2**: a participant
   audience is legal for **any** document type, and the registry's `audience`
   flag now says only whose default reveal a type seeds.

A smaller one: **REVEAL-10**'s never-list mixes document fields with things that
are not fields at all. The contract expresses it as an **allowlist** — a field
is revealable only where `1kg.5.3`'s per-field rule says so — and the record
could say which of its items are fields and which are simply never on the wire.

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

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
| Wire → design-system adapters | `ui/src/gm/adapters.ts` |
| The two suites that read the same fixtures | `service/tests/test_workbench_contracts.py`, `ui/src/gm/contracts.test.ts` |

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
| Identifiers | Opaque: `[A-Za-z0-9_-]{1,64}`. Safe in a URL fragment and in a log line, and never meaningful. Conversation ids are UUIDs today and fit. An invocation id is client-minted and is 16–64 characters, because it is an idempotency key. |
| Timestamps | ISO 8601 with an offset. The server emits UTC with `Z`. Formatting for people is the client's job; a display string such as `7:36 PM` is rejected (CANVAS-27). |
| String bounds | Counted in **Unicode code points**, on both sides, so an emoji costs one. The client uses `codePointLength`, never `.length`. |
| Trimming | A brief is trimmed before its bound is checked, and the server stores it trimmed. JavaScript's `trim()` and Python's `strip()` disagree about a few exotic code points, so **the server's judgement is final** and the client's check exists only to save a round trip. |
| Coercion | None. A `brief` of `42` is an error, not the string `"42"`. |
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
`in_flight` appears only with `cap_reached` (X-5).

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
| `conflict` | 409 | no | a field, a reveal epoch or an audio epoch moved on |
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
| Field patch | the document's base **write revision** | conflicts only if a field it touches changed since (CANVAS-19, CANVAS-34) |

### Pagination

List responses are `{ "items": [...], "next_cursor": "<opaque>" | null }`, read
with `?cursor=` and `?limit=`. A cursor is opaque and may travel in a query
string. **Search text may not**: it travels in a request body (X-7). The first
concrete page arrives with the timeline slice.

## Versioning and forward compatibility

Every top-level payload carries `schema_version`. It is `1` throughout this
contract version.

| Change | Version |
| --- | --- |
| A new optional field; a new error code | **no bump** — old clients strip the field and treat the code as generic |
| A new member of a discriminated union (a result kind, a card kind, a timeline entry kind); a removed or renamed field; a changed meaning | **bump** |

What each side does with something it does not know:

| Side | Meets | Does |
| --- | --- | --- |
| Server | a request with an unknown `schema_version`, an undeclared field, an unknown tool, kind or type | **fails closed**: 422, before any provider work |
| Server | a *stored* payload written by a newer server, after a rollback | serves it untouched as an opaque entry; it never rewrites or drops it (implemented with the timeline, `1kg.4.2`) |
| Client | an additive field; an unknown error code | **tolerates** it: the field is stripped and never reaches a component |
| Client | a newer `schema_version`, an unknown kind, or a payload that does not validate | renders a neutral placeholder — *This result was made by a newer version of Aetheril.* — through `parseToolInvocation` and `parseToolResult`. It never crashes the thread, and it never falls back to an NPC (X-8, RAIL-24) |

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
| Timeline entries and their page | to do | GM turns stored as `tool_id` plus `brief` (RAIL-9), assistant answers, tool and edit results, session dividers, attached cues |
| Documents | to do | the envelope, versions and the write revision, field patches, AI-edit scopes, conflicts, restore, history pages |
| Per-type document fields | `1kg.5.3` | built on the document envelope |
| Reveal | to do | the mutation with its epoch, Stop, GM-side state, the allowlisted projection, the table snapshot |
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
6. Decide whether the change needs a version bump, using the table above.
7. Add the adapter beside the schema if a design-system component consumes it.

# D&D 5e RAG (Aetheril) — Architecture

A retrieval-augmented chat app for D&D 5th Edition. A user picks a **channel** (Sage · Spell ·
Rules · GM — each a persona with its own retrieval scope), asks about a spell, monster, rule, or
piece of lore, and gets an answer grounded **only** in the ingested rulebook corpus (9,000+ chunks
across 12 5E books in pgvector), with citations — out-of-corpus questions are refused, not
hallucinated. Access is **invite-gated**: accounts are created only through one-time invite links,
every data endpoint requires a session, and conversations are private to their owner. Conversations
persist server-side, and uploaded files (`.txt`/`.md`/`.pdf`) ground subsequent answers in that
conversation.

> Last updated: 2026-07-26 (branch `feat/x5bz.2-invite-auth` — invite-gated auth)
> Deployed: Cloud Run + Cloud SQL, `game-guide-ai-cloud` / us-central1, IAM-locked (x5bz.1)
> Corpus 9,103 chunks / 12 books · retrieval Hit@1 83.3% (eval run 2026-06-15, pre-PHB-OCR-repair)

## System architecture

```mermaid
flowchart TB
    subgraph offline["Offline ingestion pipeline (ingestion/)"]
        direction LR
        PDFs["D&D 5e PDFs"] --> EX["extract_scan.py / extract.py<br/>anchor- or font-driven, OCR-normalized"]
        EX --> QA["qa_chunks.py<br/>pre-embed gate + collapse detector"]
        QA --> EM["embed.py<br/>text-embedding-3-small"]
    end

    DB[("Postgres 17 + pgvector<br/>dnd.chunks — 9,103 × 1536-d, HNSW + GIN FTS<br/>chat.messages / chat.attachments")]

    EM -->|idempotent upsert / --replace-book| DB

    subgraph svc["Agent service — FastAPI + LangGraph (service/)"]
        API["POST /chat · GET /healthz<br/>GET /conversations/{id}/messages<br/>POST·GET /conversations/{id}/attachments"]
        GRAPH["pipeline graph:<br/>preflight → embed → hints → scope → search →<br/>fetch → [rerank] → merge → gate → generate → cite"]
        API --> GRAPH
    end

    RET["RagRetriever (ingestion/retrieval.py)<br/>shared query core + stage methods"]
    EVAL["eval harness<br/>eval_golden · eval_answers (Ragas) · compare_models"]
    LF[("Langfuse<br/>traces · scores · dashboards<br/>(RAG_TRACING, off by default)")]

    UI["UI — React 19 + Vite (ui/)<br/>Aetheril DS · channels · profile ·<br/>history recall · attachments"]

    UI -->|"POST /chat {prompt, mode, conversation_id}"| API
    API -->|answer + sources + answerable + suggestions| UI
    GRAPH --> RET
    EVAL --> RET
    RET -->|filtered cosine kNN| DB
    API -->|history + attachment rows| DB
    GRAPH -.-> LF
    EVAL -.-> LF
```

## Request flow — `POST /chat`

```mermaid
sequenceDiagram
    actor User
    participant UI as UI (useChat)
    participant API as FastAPI /chat
    participant G as LangGraph pipeline
    participant DB as pgvector
    participant LLM as gpt-4o-mini

    User->>UI: ask a question (channel = mode)
    UI->>API: POST /chat {prompt, mode, conversation_id}
    API->>G: invoke(prompt, mode, attachment context)
    G->>G: preflight → embed → extract hints → scope(mode)
    G->>DB: filtered cosine kNN (mode-scoped)
    DB-->>G: top-k chunks + full texts
    G->>G: [gated rerank] → grounding gate
    alt refused (gate: not answerable, no chunks)
        G-->>API: REFUSAL · sources [] · answerable false (no LLM call)
    else generate
        G->>LLM: persona system prompt + numbered sources [+ attachment]
        LLM-->>G: answer with [n] citations
        opt spell mode
            G->>LLM: 3 usage suggestions (best-effort)
        end
        G-->>API: answer · sources · answerable · suggestions
    end
    API->>DB: persist user + assistant turns (best-effort)
    API-->>UI: ChatResponse
    UI-->>User: answer + citations (+ suggestion cards, dice rolls)
```

Gate rules: `sage`/`spell`/`rules` require top-1 cosine distance ≤ 0.50 **and** chunks;
`gm` proceeds with any chunks (creative mode); a conversation **attachment** relaxes the gate
entirely (an off-corpus "what does my homebrew doc say?" must generate). In `gm` mode the graph
also fans out to a stubbed **secondary world-corpus retriever** seam and merges by state.

## Ingestion pipeline (offline)

```mermaid
flowchart LR
    PDF["PDF"] --> EXT{"per-book<br/>extractor"}
    EXT -->|monster_manual| MM["extract_mm_chunks"]
    EXT -->|dmg| DMG["extract_dmg_chunks"]
    EXT -->|supplement| SUP["extract_supplement_chunks"]
    MM --> NORM["ocr_normalize<br/>(phb-5e repair + vocab l→t pass)"]
    DMG --> NORM
    SUP --> NORM
    NORM --> JSONL["chunks-book.jsonl"]
    JSONL --> QA{"qa_chunks<br/>classify + detect_collapse"}
    QA -->|clean| EMB["embed.py → pgvector"]
    QA -->|quarantine| Q["quarantine.jsonl"]
```

## Components

### Offline ingestion pipeline (`ingestion/` — [README](../ingestion/README.md))

Turns damaged OCR scans and born-digital PDFs into clean, typed, embedded chunks.
`extract_scan.py` anchors on textual structure (Armor Class / rarity / spell-level lines);
`ocr_normalize.py` repairs the PHB scan's systematic garble (incl. the vocabulary-checked l→t
pass fed by `build_vocab.py`); `qa_chunks.py` quarantines failure signatures pre-embedding;
`embed.py` upserts with `--replace-book`; `ingest_books.py` orchestrates the lot.

### Vector DB (`vector-db/` — [README](../vector-db/README.md))

- **Postgres 17 + pgvector.** Corpus schema in `vector-db/init/`: `01-extensions.sql`,
  `02-schema.sql` (dnd.chunks + HNSW/GIN indexes), `03-hybrid-search.sql`.
- **Application schema is ordered migrations in `service/sql/migrations/`**
  (`0001_chat_schema.sql`, `0002_auth_schema.sql`, `0003_jobs_outbox.sql`, …) — one
  definition and one path: `service/migrations.py` applies what a database has not
  seen, once, under an advisory lock, before the service serves anything, and
  records each file's checksum in `app.schema_migrations`. A fresh database and an
  old one converge; drift stops startup. The files ship inside the installed
  package. Database access goes through a bounded connection gate (plus a small pool
  for the realtime path) and one transaction boundary (`service/db.py`); multi-step
  writes enqueue their follow-up work in the same
  transaction (`service/jobs.py`). See [migrations.md](migrations.md).
- `dnd.hybrid_search()` (vector+FTS RRF) exists but is **not adopted** — tied Hit@1, slightly
  worse Recall@10 (3q3). `verify_db.py` is an insert+kNN smoke test.

### Retrieval core (shared)

**`ingestion/retrieval.py` — `RagRetriever`** is the single retrieval brain used by both the
service graph and the evals: embed → detect class/entity/content-type hints against the corpus
vocabulary (generic-entity stoplist + ≥3-char entity floor + stemmed ILIKE) → filtered cosine
kNN → full-text fetch → answerability by top-1 distance. **`ingestion/scope.py`** maps mode →
(content-type, book) filters; **`ingestion/rerank.py`** gates the optional cross-encoder to
prose categories. Default mode is pure filtered vector.

### Agent service (`service/` — [README](../service/README.md))

- **`graph.py`** — the whole request pipeline as a LangGraph `StateGraph`; every stage is a
  traceable node (preflight, embed, extract_hints, scope, search, fetch_texts, rerank, merge,
  gate, generate, suggest, cite, refuse). **`rag.py`** is invoke + response mapping plus the
  injection seams (retriever, reranker, LLM client, secondary retriever).
- **`generate.py`** — per-channel persona prompts; grounded generation over **full** chunk
  texts; spell-usage suggestions (best-effort second LLM call); deduped `Source` citations.
- **`history.py`** — server-side conversation persistence (best-effort writes: history failure
  never fails an answer). **`attachments.py`** — upload text extraction + caps; attachment text
  is injected as a sibling context source and cited.
- **`tracing.py`** — env-gated Langfuse tracing (`RAG_TRACING`, off by default): node-level
  spans, token/cost, tagged model/version/mode. See [`observability/OVERVIEW.md`](observability/OVERVIEW.md).
- **Auth (x5bz.2)** — `auth_store.py` (users + invites in the `auth` schema, argon2 via
  `hashing.py`, atomic single-use invite redemption), `session.py` (itsdangerous-signed
  httpOnly session cookie carrying user id + role), `invites.py` (redeemability rules),
  `admin_invites.py` (operator CLI: create/list/revoke). `require_session` guards `/chat`,
  `/conversations/*` and `/auth/me`; `/healthz` and `/metrics/ui` stay open. The service
  **fails closed** if `SESSION_SECRET` is unset. Every auth-store call goes through
  `_auth_lookup`, so a backend that breaks *after* startup answers **503**, never 500 —
  "retry later", not "this request is broken". Design rationale:
  [`adr/invite-auth.md`](adr/invite-auth.md); incident response: [`deploy-gcp.md`](deploy-gcp.md) §10.
- Contract: `ChatRequest{prompt, mode, conversation_id}` → `ChatResponse{answer, sources[],
  answerable, mode, conversation_id, suggestions?}`. Errors: **401 no/expired session** ·
  **403 wrong role (GM channel) or another user's conversation** · 422 validation ·
  502 LLM upstream · 503 backend unavailable · 500 bug; a refusal is a **200** with
  `answerable=false`.

### UI (`ui/` — [README](../ui/README.md))

**React 19 + Vite**, bun-managed, on the **Aetheril design system** (`ui/src/ds/` — Material 3
token layer, warm fantasy palette, light *Parchment* / dark *Tavern*, 10 components, Storybook).
Shell: **Login / Signup** → Landing → Workspace (TopBar brand · **AppHeader channel switcher**
with per-channel accents · LeftNav conversations + UserMenu · ChatPane) + Profile screen.
`App` gates on the session check (`GET /auth/me`), which has four outcomes: *checking* holds a
loading gate, *authenticated* enters the app, **401 — and only 401 —** renders Login, or
**Signup** when the URL carries an invite (`/#invite=<token>` — a root-path link whose token
rides in the URL **fragment**, so it never reaches the server or its request logs; there is no
client router and the built SPA 404s on deeper paths). Anything else (5xx, network failure)
is *unavailable*: a retry screen, not Login — the session may be perfectly valid, and signing
in again would need the same backend that just failed. Identity and role come from the server session;
the GM channel is still hidden in the UI for DMs, but the **server** is what enforces it.
`useChat` recalls stored history; ChatPane uploads attachments; spell answers render suggestion
cards; dice notation renders `DiceRoll`. `api.ts` mirrors `service/models.py` exactly (refusals
are not errors) and sends `credentials:'include'` on every call. Conversation list/titles remain
client-side; display name + avatar tone are local cosmetics.

### Packaging

The repo is an installable package (`pyproject.toml`: packages `service`+`ingestion`,
py-module `config`; extras `test` / `eval` / `rerank` / `extract`). `docker compose up --build`
→ `vector-db` + `service` + `ui` (nginx, :5173); or a single `uvicorn` process serves the built
`ui/dist` plus the API on :8000.

## Campaign schema (GM Workbench)

Storage for the Workbench, added by `1kg.2.1`. Migrations `0004`–`0006`, with
`0009` making a participant **an account's seat at a campaign** (bead `fma`, the
owner's decisions D-1 and D-4: every player holds an account and there are no
guests); stores in `service/campaign_store.py`, `service/participant_store.py`,
`service/table_session_store.py` and `service/audit_log.py`. `1kg.2.2` added
`0012` (the GM's confirmation, offers by address and blocks), the
`service/seat_offer_store.py` store and two routers: `service/campaigns_api.py`,
the GM's campaigns and seats on the `dm`-gated Workbench router, and
`service/seats_api.py`, an account's own offers and seats on `account_router`
(`/seats`, which a player reaches). Table routes are `1kg.2.3`'s.

A seat is **open** (an alias the GM seated while preparing, no account),
**accepted** (by an account), **confirmed** (the GM confirmed who accepted,
`confirmed_at`, D-12 and SEC-50(5)) or **removed** (marked, never deleted).
**The GM offers a seat to an address, never to an account** (D-12): the offer
is a `seat_offers` row, making it reads no account row and no block, and it
**binds to an account only at acceptance**, when the accept route composes the
participant store's `offer` and `accept` in one transaction — so the store's
"offered" row state is transient and never committed. Only a Verified account
whose verified address is the offer's may see or accept it, which on this build
is nobody until `yje.2.1` (`auth_store.verified_address` fails closed). Until
the GM confirms, a seat gets the table slot only (SEC-41's `own_slot` reads
`confirmed_at`). Only an offer followed by that same account's acceptance moves
a seat forward: there is no claiming an open seat by
possession of a link or a code, because that would be the retired enrolment
code under a new name, and a GM is never offered a seat in their own campaign.
Every refusal is one `SeatUnavailable` with a fixed message that names nothing.
The store takes no campaign lock and advances no `authz_revision`; the route
that composes an acceptance (`1kg.2.2`) does both, as for `add` and confirm
(RQ-4, RQ-10). Archive narrows a live session first without the lock and then
changes the fact under it (RQ-5); Remove is a revocation that never takes the
lock, asks for the password (SEC-40) and leaves one `campaign.reconcile` job
(`service/reconciliation.py`) that advances the revision. The single-use enrolment code and the device credential are
retired, and `0009` drops their tables.

### The tables

| Table | Holds | The rule that shapes it |
|---|---|---|
| `campaign.campaigns` | a GM's table: owner, name, created/updated/archived | owner is `NOT NULL` and cascades from `auth.users` |
| `campaign.authz_state` | `authz_revision`, and `lock_token` (never written) | an `AFTER INSERT` trigger on `campaigns` creates it, so no path can leave a campaign without one (RQ-1) |
| `campaign.participants` | a seat: alias, `alias_key`, created, `removed_at`, and (`0009`) the account it is offered to (`user_id`) and when that account accepted it (`accepted_at`) | marked removed, never deleted; the alias is unique within the campaign among seats that are not removed, compared over an `alias_key` the **application** computes (NFKC then `casefold`) so that PostgreSQL's `lower()` and Python's cannot disagree; an account holds at most one live seat per campaign (a partial unique index); `user_id` is `ON DELETE NO ACTION`, so deleting an account that holds a seat, removed or not, is refused until account deletion handles seats (`agent-forge-harness-zkc`); a CHECK keeps an accepted seat from having no account |
| `campaign.table_sessions` | a GM running a table now | at most one `live` session **per GM across campaigns** (a partial unique index), both epochs, and `link_generation`, which **is the admission generation** (SEC-42; renamed in prose only); `start_command_id` (one session per start command per campaign, a partial unique index, so a retried Start answers its session) and `rotate_command_id` (no index: it is read from the row its Rotate already holds, and an index would make Rotate block every screen-grant insert, RQ-3), both from `0016`; `(campaign_id, gm_user_id)` references `campaigns (id, owner_id)`, so the GM **is** the owner (AUD-1); `state` and `ended_at` are kept in step by a CHECK, and a row still `live` past `expires_at` is dead to every reader |
| `campaign.table_credentials` | a **screen grant**: a browser the owner made a table screen (SEC-48, D-13) | bound to the admission generation it was minted in; live only while unrevoked, its session live and unexpired, and its generation the session's current one — the reader's test, never `revoked_at` alone (`1kg.2.3`). The table link and the join are gone (threat model section 15), and `0012` dropped the join counter |
| `campaign.reveal_disclosures` | one Confirm's worth of display: a document, the version it pins, a sorted mask of field keys and the audience kind (`0017`, `1kg.7.1`) | at most one **live** disclosure per document (a partial unique index); `(session_id, command_id)` unique, the Confirm's replay key; its session and its document are both of its campaign and its version is one of its document's (composite keys); `ended_at`/`ended_reason` set together, from a closed set; ended rows are kept for replay only |
| `campaign.reveal_slots` | one audience slot of one session: the table slot or one participant's, and the disclosure it shows (`0017`) | one row per slot per session; one pointer, so a slot shows at most one live projection; it points only at a disclosure of its own session and audience kind; `seq` rises by one each time its content changes; no delete action toward the disclosure, so a shown document cannot be deleted until it is narrowed |
| `audit.events` | one recorded decision | append-only; `campaign_id_tombstone` has **no** foreign key, so rows outlive their campaign |

### The uncampaigned state

`chat.conversations.campaign_id` is nullable, and **`NULL` is the documented
uncampaigned state**: every conversation that existed before `0006`, and plain
chat from now on. Nothing about `/chat` changes because the column exists;
`service/history.py` is untouched and reading or writing the new columns is
`1kg.2.4`'s.

The reference takes **no delete action**, and is `DEFERRABLE INITIALLY
DEFERRED`. Until `1kg.2.6` decides whether deleting a campaign detaches its
conversations or refuses while any remain, the database refuses — so nothing is
silently detached. One consequence is worth knowing before it is met in a
traceback: if user V has a conversation linked to user U's campaign, **deleting
the account U fails**.

Deferring the check is what makes the *owner's own* conversations a determinate
case. Checked immediately, the answer depended on the firing order of two
referential-integrity triggers on `auth.users`, whose names embed an OID
rendered as text — so a freshly initialised cluster cascaded and a long-lived
one, whose OID counter has six digits, would have refused the same delete.
Checked at commit, the owner's conversations have already gone with the user
cascade and there is nothing left to check: **deleting an account takes its
campaigns and its own conversations with it, everywhere**.

### Conversations

`service/conversation_store.py` (`1kg.2.4`) makes a conversation's **identity and
metadata** server-authoritative. What that does and does not mean:

**Server-authoritative:** the id, the title, the campaign link, the archive flag
and the channel the conversation was started in. A conversation created through
the store is minted server-side — `cnv_` plus 16 CSPRNG bytes, 26 characters,
SEC-4's floor — and exists in `chat.conversations` with its owner from that
moment, so it is in its owner's index immediately and survives a reload or a
second device.

**Not server-authoritative, and deliberately:** the id of every conversation that
already exists. A client-minted `crypto.randomUUID()` is a valid conversation id,
on every route, forever, with no deprecation and no warning. `conversation_id`
carries **no prefix `CHECK` and must never gain one** — which is also why a
conversation id is minted in `conversation_store.py` rather than joining
`service/campaign_identity.py`'s registry, whose members `0004` constrains.

**`POST /chat` is not modified by this work, at all.** A conversation the store
created already has its ownership row, so `claim_conversation`'s
`INSERT ... ON CONFLICT DO NOTHING` is a no-op and its `SELECT` returns that
owner; `create` leaves `selection_strategy`, `manual_alias` and
`catalog_revision` `NULL`, so the first turn still binds model affinity exactly
as it does today (`b8o.2`). Nothing here adds a statement to a request that makes
a model call.

**`started_mode` is the channel, bound once.** It records the channel a
conversation was *started* in — not the mode of a turn, which stays per turn on
`chat.messages.mode` — and it is bound first-writer-wins, like
`selection_strategy`. It is **`NULL` for every conversation that existed before
`0007`**, and that is the honest value rather than a migration in progress: the
channel was never recorded, so it is unknown. Nothing backfills it, nothing
guesses it, and **a chat turn never sets it**.

**`updated_at` moves on a metadata change, never on a chat turn.** The owner's
index orders by `COALESCE(updated_at, created_at) DESC, conversation_id DESC` —
the keys of `conversations_owner_recent_idx`, which is partial on
`archived_at IS NULL` because that is the default query. The consequence is
accepted and worth stating: the index is ordered by metadata changes, not by
recent activity. Ordering by activity would mean a new statement on `/chat`'s
request path.

**Archive is not deletion.** `archived_at` is reversible and destroys nothing.
There is no delete route and no delete store method here; conversation deletion
follows `agent-forge-harness-1ka.5`.

**Ownership is in the query**, every time: each method names the owner in the
same statement as the row, so a conversation that is not the caller's is
indistinguishable from one that does not exist. Where a request names a campaign
— creating inside one, or linking to one — the campaign is resolved **inside the
writing statement** (`... FROM campaign.campaigns WHERE id = %s AND owner_id = %s
AND archived_at IS NULL`) and zero rows written is the refusal. The `campaign_id`
edge is `DEFERRABLE INITIALLY DEFERRED`, so an integrity failure on it would
arrive at `COMMIT` — outside every `try` and after a route had composed its
answer — and no code here is allowed to catch one.

**The routes** (`service/conversations_api.py`, `1kg.2.4` A2) are
`GET|POST /conversations` and `GET|PATCH /conversations/{id}`, on a
`workbench_router` that imports nothing from `service/app.py`, which hands it
the GM gate. They take the Workbench posture from `agent-forge-harness-oe6`'s
scaffolding (*Workbench routes*, at the end of this file): the origin check on
a write, the one `401` body, the `dm` role, validation that answers
`validation_error_body` and never FastAPI's default 422, and **one `404`** for a
conversation that is missing, someone else's, never owned or unreadable. They
**never claim** a conversation. The contract is *The conversation family* in
[`workbench-wire-contract.md`](workbench-wire-contract.md).

**Two postures on one `/conversations` prefix.** The legacy routes —
`GET …/messages` and the attachment routes — keep what they answer today: a
`403` for a conversation that exists and is someone else's, and a claim on
first read of one with content (R-4, R-5). The new routes answer the generic
`404` and claim nothing. A prober can therefore still ask the old route what
the new one will not say; the threat model's §10 records that as accepted for
the pilot rather than leaving it silent.

### Digests, and what is private

The one bearer secret the table model keeps is the **screen grant** (SEC-48): 32
random bytes, of which only the lowercase-hex SHA-256 digest is stored
(`table_credentials.credential_digest`), and every lookup is an exact match on
the unique index over it (SEC-5). The table link token and the join credential
are retired (threat model section 15; `0016` dropped the link's digest, `1kg.2.3`).
A plain-text grant exists only as the return value of the one method that mints
it, and leaves the server only in the `Set-Cookie` of the answer that minted it. `argon2` (`service/hashing.py`) is deliberately
not used for these: they are 256-bit random values with nothing to brute-force,
and a slow hash on a route anyone can call is a denial-of-service lever.

An alias, a conversation title and a campaign name belong in no log line, no
exception, no URL and no audit row (SEC-20). Two different things work towards
that, and they are worth telling apart.

**In the stores it is enforced.** The records hide those fields from `repr()` —
a traceback is a log line — and every refusal names the rule or the key, never
the value. A test sweeps the whole store and audit surface for a canary alias,
title, secret and digest.

**In the ledger it is a closed vocabulary, not a shape rule.**
`0005_audit_events.sql` bounds lengths and types `actor_ref`, `object_ref` and
`campaign_id_tombstone` as free `TEXT` with no CHECK, so `service/audit_log` is
the enforcement — and what it enforces is ED-18(a)'s "closed, per-action
`detail` of ids, codes and keys". `ACTION_DETAIL` says, for each action, exactly
which keys a row of that action may carry and what each one is: a minted id of a
named prefix, one of a closed set of codes, a Workbench field key (ED-2), a
bounded list of them (which is how `1kg.7.1` will record a reveal's mask), a
whole number or a boolean. A key the registry does not list is refused, and the
refusal names neither the key nor the value.

**Two kinds are open by construction, and a caller has to know which.** A
`MintedId` accepts any well-formed body behind its prefix — the tombstone has no
foreign key (ED-26), so nothing can ask whether that row exists — and what it
closes is the prefix and the length. `FIELD_KEY(S)` is a shape: which keys a
document may have belongs to its type, and the ledger does not know the types,
so the caller recording a reveal's mask is the one that must check its keys
against the type's declared keys. Everything else is a closed set.

The columns beside `detail` are closed the same way: `campaign_id_tombstone` is
a minted `cmp_` id, the two references are minted ids or the GM's numeric user
id, `object_kind` is one of five `ObjectKind` members, `reason_code` is one of
the codes its own action declares, and `authz_revision` carries the same `>= 0`
the migration does. The rule this replaced was one shape test over every action
at once, and it could not tell a one-word alias from an identifier —
`{"alias": "Rook"}` passed it, and so did `object_kind="rook"`. Both are refused
now. No kind holds the client-minted command id a reveal row needs; `1kg.7.1`
decides how that is recorded. All of this is stricter than `jobs.check_payload`,
which admits any short string, because a job is read and deleted while an audit
row outlives everything it describes.

### The campaign lock

`UnitOfWork.lock_campaign(campaign_id, shared=...)` holds a campaign's
authorisation still for the rest of a transaction — `FOR SHARE` to read under it,
`FOR UPDATE` to change it — and `advance_authz_revision` is the only code that
writes `authz_revision`, refusing unless the caller holds the lock exclusively.

Three rules are enforced in both the PostgreSQL implementation and the in-memory
twin, so the two cannot disagree about which calls are refused: it is the
**first** lock a transaction takes, it is the **only** campaign that transaction
locks, and a shared holder never upgrades to exclusive in place. Who *blocks*
whom is the database's, and is tested there (`tests/test_campaign_db.py`).
`Database.transaction()` opens every transaction **explicitly READ COMMITTED**, so
no server, database or role default can change what the lock is reasoning about.

### One setting interaction to know about

`CAMPAIGN_LOCK_TIMEOUT_S` is bounded 0.05–4 and must be **below**
`DB_POOL_TIMEOUT_S`, which is bounded 1–60: a request waiting for the campaign
lock is holding one of the gate's connections, so a lock wait that outlasts the
gate turns one contended campaign into a 503 for unrelated traffic. The
invariant holds two ways, so that no documented gate can stop the service from
starting. **Unset**, the lock timeout is *derived* from the gate — half of it,
capped at RQ-8's two seconds — and is below it by construction for every value
in 1–60. **Set**, it is checked against whatever the gate is, and one that is
not below it is refused by name at startup rather than in a traceback. The wait
reaches the server in whole milliseconds, which is that GUC's own unit; the
default on the default gate is still exactly 2 s.

Neither setting is read from the environment yet: nothing takes a campaign lock
in this bead, so `Database` derives the lock timeout from its own gate. The bead
that first takes one on a route passes `CampaignLockSettings.from_env(pool=...)`,
the way `service/app.py` already passes `PoolSettings.from_env()`. The
comparison is made in `Database.__init__` as well as in `from_env`, so it does
not wait for that bead: a `Database` built in code with a short gate is refused
at construction rather than silently keeping the default 2 s lock timeout.

A caller may also raise the transaction bound for one long piece of work
(`lock_campaign(..., transaction_timeout_s=...)`, and the same parameter on the
session and participant holds). The value is checked — from one millisecond to
600 seconds — in both units of work: PostgreSQL spells "no timeout" as `0` and
measures this setting in milliseconds, so an unchecked parameter could switch
off the very bound it exists to raise.

### Documents and their versions

`0008_document_schema.sql` adds two tables and `service/document_store.py` the
store over them (`1kg.5.1`). Eight Workbench routes in `service/documents_api.py`
(`1kg.5.2`) serve them — the library, create, read, field patch, history, a
version's content, restore and seal — under `/campaigns/{campaign_id}`, each
reading ownership first in its transaction and none taking the campaign lock;
the pure rules between the store and the wire are `service/document_wire.py`.

`campaign.documents` holds one document's **live** content as flat JSON, one
value per field key its type declares, plus the two counters. `campaign.document_versions`
holds its history. There is deliberately **no `campaign_id` on the version
table**: every statement against it joins through `campaign.documents` and names
the campaign in the same statement, so a version of another campaign's document
is indistinguishable from one that does not exist (SEC-2).

**The write revision is not the version number.** `write_revision` is the
concurrency token: every committed write advances it by one, and `field_revisions`
records which write last touched each key, so a conflict is decided **per field**
— a write is refused only when a key it *touches* has moved since its base.
A stale write touching untouched fields is **rebased by the store**: the patch is
merged over the row's current content under the row lock and the merged document
is re-validated before the commit, so nothing is left to the caller. A version
`number` is history: consecutive from 1, never reused, never renumbered, and the
current version is `MAX(number)` rather than a pointer column that could disagree
with the rows it names.

**Sealed means immutable; open means staging.** A burst of GM autosaves
accumulates into **one** open working version — `sealed_at IS NULL`, enforced by
a partial unique index — which is rewritten in place, so a document does not grow
a whole-document snapshot per pause. It is sealed by another author's write, by
ten minutes of idleness (`SEAL_IDLE_S`), or by an explicit `seal(...)` that a
route calls. Once sealed a version never changes: every `UPDATE` in the store
carries `AND sealed_at IS NULL`, and the module runs no `DELETE` against that
table at all. Every mutator takes `now`, so none of this needs a timer, a job or
a background sealer.

**Search folds in the application, as the alias does.** `name_key` and
`search_key` are computed by `service/document_store._fold` — control characters
to spaces, NFKC, `casefold`, whitespace collapsed — and stored, so PostgreSQL's
`lower()` and Python's cannot disagree about a final sigma or a dotted capital I.
A search matches `name`, `qualifier` and `tags` only, by `strpos` over the
already-folded column (never `ILIKE`, `lower()` or `LIKE`, which would need `%`
and `_` escaped in the term). `search_key` is **truncated** at 8,000 folded
characters, which is a known limit: a document with very many long tags stops
matching on its later ones, silently. There is **no dedicated search index in
v1** — the four campaign-scoped partial indexes bound the scan and pilot
campaigns hold hundreds of documents. Revisit it when one campaign passes about
5,000 documents; adding a `tsvector` column with a GIN index is a later pure
expansion that nothing here precludes.

**The character-sheet link** is one nullable `linked_participant_id` column with
a partial unique index: a sheet links to at most one participant and a
participant to at most one sheet (AUD-13, AUD-15). It is `ON DELETE SET NULL`
rather than `CASCADE`, which would destroy the sheet that AUD-16 says Remove
keeps; participants are marked removed and never deleted, so it never fires in
the application. **Nothing about visibility is stored on a document or a version**
(ED-6): a reveal's pin is `1kg.7.1`'s slot row and references
`(document_id, number)` from here.

**The library, restore, archive, delete and the link (slice B).**
`list_documents` answers one campaign's page: a category as a set of types,
Recent or Name A-Z ordered exactly as the library indexes key them (`COLLATE
"C"`, the id breaking ties), Active or Archived, and keyset pages anchored on a
document id whose sort key is looked up server-side. "Restore" is two things:
`restore` appends a sealed version equal to an earlier one (CANVAS-26, and a
no-op when the content already matches), while `set_archived(archived=False)`
un-archives (LIB-16); neither brings back a reveal. `delete` is LIB-18's hard
delete of the document and its whole history. `link_character_sheet`,
`unlink_character_sheet` and `sheet_for_participant` hold the document row and
only read the seat. **None of these takes the campaign lock or advances
`authz_revision`**: the two-step orchestration around archive, delete and unlink
(`narrow`, then the exclusive lock, the re-scan and the advance) belongs to the
routes that call them, `1kg.5.2` for documents and `1kg.2.2` for participants.

### What the table may see: disclosures and slots

`1kg.7.1` makes what the table may see **explicit server state**
(`service/reveal_store.py`, `service/reveal_scope.py`, migration `0017`). A
Confirm persists a **disclosure** — one Confirm's worth of display: a document,
the version it pins, the sorted mask of field keys and the kind of audience —
and points **slot rows** at it: the table slot, or one slot per participant
(owner decision O-3: a group display is per-recipient copies of one
disclosure). Nothing about visibility is on a document or a version (ED-6); the
pin is reached slot -> disclosure -> (document, version).

A Confirm is an **update** when the targets are exactly the slots the
document's live disclosure has (the old one ends `updated`), a **move**
otherwise (it ends `moved` and every copy outside the new set is cleared), and
it **replaces** another document's copy in each target slot (that disclosure
ends `replaced` only with its last copy). A target slot is re-pointed, never
cleared and then pointed, so its `seq` rises by exactly one. A narrowing clears
by **scope** (`reveal_scope`): every slot (End, expiry, Rotate, archive,
Stop-all), one member's slots (Remove, A-20), one document's copies (a Stop,
and later a document's archive, deletion or unlink), or none (audio off); the
default is every slot, so a caller that forgets over-clears. Wiring the clear
into `narrow`, the service that orders a Confirm and a Stop, and the
reconciliation's fill are the bead's second change.

| Invariant | Held by |
|---|---|
| I-1 a slot shows at most one live content | one pointer column; `UNIQUE NULLS NOT DISTINCT (session_id, participant_id)` |
| I-2 a document has at most one live disclosure | a partial unique index; the twin refuses too |
| I-3 a disclosure is the table slot or participant slots, never both | `audience_kind` in the slot -> disclosure key |
| I-4 every copy is in its disclosure's session and campaign | composite foreign keys |
| I-5 no slot points at an ended disclosure; a live disclosure has a copy | the store's writers; the test-only auditor `audit_reveal_invariants` |
| I-6 every write of a reveal row holds the session row | each writer takes it itself, `FOR NO KEY UPDATE`; none calls `lock_campaign` |
| I-7 every explicit lock is `FOR NO KEY UPDATE`; no UPDATE changes a key column | only `seq`, `disclosure_id`, `updated_at`, `ended_at`, `ended_reason` are updated |
| I-8 no visibility column on `documents` or `document_versions` | the schema test reads `information_schema` |
| I-9 no reveal row, `repr()` or refusal carries text | ids, versions, mask keys and codes only; the command id is hidden from `repr()` |
| I-10 a slot's `seq` rises by one exactly when its content changes | the shared write and clear |
| I-11 `picture` and both views gate on `state = 'live' AND expires_at > :now` | the application's clock; `by_command`, `live_for_document`, `live_disclosures` and `stale_slots` read dead sessions by design |
| I-12 an own slot is read only for an accepted, confirmed, not-removed seat | `view_for_account`, in the query that finds the session (SEC-41) |
| I-13 **ended disclosures are read by replay only** (`by_command`) | a source scan; never to seed a mask (REVEAL-4), never as a ledger (ED-18) |
| I-14 no default mask is read | a source scan |

A copy for a seat that is not yet confirmed is **held, not stored as held**: it
is written like any copy, the GM's picture marks it `held`, and
`view_for_account` never returns it, because the own slot is joined only for a
confirmed seat. Confirming the seat delivers it from the next read (D-12,
SEC-50(5)). A screen sees the table slot only (SEC-48), and `view_for_screen`
names the campaign, so a live grant read under another campaign is the one
`None` (SEC-46). `participant_id`, `slot_id`, `disclosure_id`, `document_id`
and `version` are GM-side and never reach a table client; `1kg.7.2` builds the
projection from them with its one builder.

## Media assets (GM Workbench)

Storage for a GM's images and audio, added by `1kg.8.1.1` (slice a of `1kg.8.1`):
the media migration (`*_media_assets.sql`), the asset store in `service/asset_store.py`
and the object store in `service/media_objects.py`. **It ships dark.** No route
exists, nothing in `service/app.py` builds a store or registers a job kind, and
nothing in the running service reads the media settings: slice b (`1kg.8.1.2`)
wires them. Switching the capability on is the owner's decision (Q-5).

### The tables

| Table | Holds | The rule that shapes it |
|---|---|---|
| `campaign.assets` | one asset: its kind, what was declared (type, size, alt text), what the server measured (type, size, and dimensions or duration), a state, a failure reason, the command id that created it, and two object keys | never bytes and never a filename; `campaign_id` is **`ON DELETE NO ACTION`**; every CHECK mirrors the wire `Asset` validator, so every row a GM can read converts to a valid `Asset`; one index, `(campaign_id, state)`, and a partial unique index on `(campaign_id, created_command_id)` |
| `campaign.media_usage` | per campaign, `bytes_reserved` and `asset_count` | a table of its own so that quota writes never contend on the campaign row; an `AFTER INSERT` trigger on `campaigns` writes it, and the migration backfilled every existing campaign; cascades with its campaign |

**The states** (MS-3): `uploading` -> `processing` -> `ready` or `failed`;
`processing` may return to `uploading` (MS-6's retry after a queue timeout); any
of those four -> `deleted`, a storage-only **tombstone** the wire contract never
carries. A tombstone keeps no alt text and no measured value, by the database's
own CHECK. The machine is the store's and it is total: a mutator holds its row
(`FOR NO KEY UPDATE`), then decides. Anything that is not the caller's live
asset (missing, another owner's, another campaign's, or a tombstone) is one
`MissingParent` with one fixed message; `IllegalTransition` is raised only for
the caller's own live asset in the wrong state. Every entry into `uploading` or
`processing` enqueues that state's deadline sweep (`asset.sweep_stuck`).

**A key** is `tmp/<32 hex>` for an upload in flight, `assets/<32 hex>` for the
processed original, or `assets/<32 hex>/<name>` for a derivative (`1kg.8.2`).
The hex is 16 CSPRNG bytes minted independently of every input and of the other
key, at insert, and never updated (a column of a non-partial unique index is a
key column, so an update would take `FOR UPDATE`). A key is **not** a name, an
owner, a campaign or a capability: the asset row is the only map from an owner
to a key (MS-2), and no job payload carries a campaign id. The object store
checks one key grammar before any I/O and refuses everything else with one
fixed message.

**The usage invariant**, after every write: `bytes_reserved` is the sum of the
campaign's reservations (the declared size while `uploading` or `processing`,
the real size once `ready`, nothing once `failed` or `deleted`), and
`asset_count` counts the rows in `uploading`, `processing` or `ready`. Every
write to it is one conditional `UPDATE` or an exact subtraction. A create
inserts its row first, inside a savepoint, and only then reserves, so a replayed
command returns its first asset without reserving anything even when the quota
is now full, and a quota refusal rolls back to the savepoint and leaves the
caller's transaction usable. The loser of two racing reservations changes
nothing only because `Database.transaction()` runs READ COMMITTED.

### Locks, ownership and deletion

**The lock order**, which every path keeps: (0) `create` only, the campaign row
`FOR KEY SHARE` in its ownership check, so a create that loses to the campaign's
deletion answers `MissingParent` rather than a foreign-key violation; (1) the
asset row or rows; (2) `media_usage`; (3) the outbox enqueue, **always last**,
because a dedupe-keyed enqueue waits on another transaction's uncommitted job of
the same key. Every path calls `note_row_lock()` and bounds its transaction
before its first lock. Nothing takes `authz_state` or advances `authz_revision`
(an asset has no eligibility of its own, ED-19), and no statement takes
`FOR UPDATE`.

**Two ownership fragments.** Every GM statement names its campaign and, through
`GM_CAMPAIGNS`, the caller as its owner; `yje.2.1` adds the identity ADR's
section 8.1 conjunct (the owner is Verified) there. The campaign primitive alone
uses `OWNER_CAMPAIGNS`, which must **never** gain that conjunct: once a Deleted
GM's campaigns are unavailable, a primitive scoped by the GM fragment would
match nothing and `NO ACTION` would refuse that GM's erasure (`zkc`) for ever.
`tests/test_asset_db.py` checks every statement by the fragment's name, since
the two are the same text until then.

**Deletion.** A GM's delete writes the tombstone, releases the reservation and,
last, enqueues `asset.delete` with `{asset_id, object_key, tmp_key}` and the
asset id as its dedupe key; it returns the job id, and the route (slice c)
hands it to `job_driver.run_after_response`. The handler deletes the objects
and then purges the row (below). **A campaign's deletion** (`1kg.2.6`) must stop
creates first (the store's docstring names one way: lock the campaign row after
`lock_campaign(exclusive)`), then call `delete_campaign_assets`, the primitive:
one owner-scoped `DELETE ... RETURNING` removes every row, the usage is reduced
by exactly what it returned (never zeroed), and one `asset.delete` is enqueued
per removed row that was not already a tombstone. Only then can the campaign row
go; PostgreSQL refuses it, directly or through the account cascade, while any
asset row remains.

### The three jobs (`service/asset_jobs.py`)

`register_jobs(runner, ...)` registers all three kinds, and nothing calls it in
the running service until slice b wires it into `_build_stores` when a store is
configured; with no store configured nothing is registered. Every handler
re-reads current state and uses nothing from its payload beyond the ids and keys
it names. **No handler holds a database connection while it calls the object
store**, and every call goes through `via_store`. A handler whose advisory
`JobContext` runs out part-way raises `JobOutOfTime` and is retried; it never
returns early as success, because the runner would then complete, and so
delete, a job that still had work to do. The system statements (the sweep's
read and fenced write, the purge, and the reconcile's key lookup) live in this
module only; they name an asset by its globally unique id.

| Kind | Seeded by | Payload, dedupe key | What it does |
|---|---|---|---|
| `asset.delete` | a GM's delete, and the campaign primitive | `{asset_id, object_key, tmp_key}`, the asset id | deletes the `tmp/` object, every derivative under `object_key/` page by page, then the original; then purges the row, but only while it is still the tombstone. The keys come from the payload, so the bytes go even after the primitive removed the row. Never marked dead (`max_attempts=None`) |
| `asset.sweep_stuck` | every entry into `uploading` (due 1 h later) or `processing` (10 min later), by that asset's own transition | `{asset_id}`, none | fails a row still in the state it read and past that state's bound `timed_out`, with an `UPDATE` fenced on the state and the `state_changed_at` it read; releases the reservation and deletes the objects **only if the fence changed the row**. A lost fence means another transition won, and the bytes are that path's. A row that read `failed` has its objects deleted, idempotently |
| `asset.reconcile_orphans` | `enqueue_reconcile(unit, ...)`; its periodic caller is `1kg.9.5`'s | `{}` or `{"after": <key>}`, none | walks `assets/` then `tmp/` in byte order strictly after its cursor, examining at most `RECONCILE_BATCH` objects, and deletes each examined object older than a day that no live row names (a derivative counts through its parent key). While more remain it enqueues one successor carrying the last key it examined; once the listing is exhausted it enqueues nothing |

**Why the reconcile chain ends.** The listing's bound counts objects examined,
not matches, so its cursor moves on even when every examined object is still
referenced; a pass over N objects is at most floor(N / `RECONCILE_BATCH`) + 1 runs,
because every run but the last examines exactly `RECONCILE_BATCH` objects (when
`assets/` ends exactly at the budget, one more run follows to look at `tmp/`,
even if it finds nothing there), and a new pass starts only when someone calls
`enqueue_reconcile`. No key is ever
reused, so an object no live row names now will never be named again, and the
lookup and the delete need no lock between them. Until `1kg.9.5` schedules it,
the bucket's own lifecycle rule for `tmp/` is production's backstop
(`docs/deploy-gcp.md` section 13).

### Settings, health, and what the table side does not do

`MediaSettings.from_env` reads `WORKBENCH_MEDIA_ENABLED` (strictly `true`,
`false`, `1`, `0` or unset; off by default) and `WORKBENCH_MEDIA_STORE` (unset
by default, meaning no store is built; `filesystem` with an absolute
`WORKBENCH_MEDIA_DIR`; `gcs` is refused by name until slice d; `memory` is built
in code only). "Off" means no route, no store, no bucket and no cost; the store
setting is separate from `enabled` because a deployment switched off must still
finish the deletions it owes. Every refusal names the variable, never its value.
Every object-store call outside `service/media_objects.py` goes through
`via_store`, where slice c puts the thread limiter. The store's health signal
for `1kg.9.2` is the read-only `reachable()`.

**Table reads never use this store** (SEC-44(2)). A table's slot resolver finds
its asset in its own `table_principal` query (SEC-16, SEC-41, `1kg.7.x`); the
asset store is GM-side and system-side only.

## Running it

```bash
cd repos/game-guide-ai
./scripts/up.sh                    # full stack → http://localhost:5173
# or single process (uvicorn serves the built UI + API together):
cd ui && bun run build && cd ..
uv run --with . uvicorn service.app:app --port 8000    # → http://localhost:8000

# offline pipeline (per book)
uv run --with '.[extract]' python ingestion/extract_scan.py "<pdf>" --book-slug <slug> --out ingestion/chunks-<slug>.jsonl
uv run python ingestion/qa_chunks.py ingestion/chunks-<slug>.jsonl
uv run --with "psycopg[binary]" --with openai python ingestion/embed.py --chunks ingestion/chunks-<slug>.clean.jsonl --replace-book

# evaluation (pure vector is the default; PYTHONUTF8=1 on Windows)
uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py
uv run --with '.[eval]' python ingestion/eval_answers.py --limit 5
```

## Testing

Python: **pytest** from the repo root — `uv run --with '.[test]' python -m pytest -q`. Suites are
pure (no DB/LLM; retriever, LLM, and store faked): `service/tests/` (app, graph, history,
attachments, tracing), `ingestion/tests/` (extraction, QA gate, retrieval, scope, rerank, evals),
`tests/` (packaging + config invariants). UI: `bun run test` = jsdom unit tests + Storybook
browser tests (Playwright Chromium); `bun run typecheck` / `lint`.

## Current metrics (as of the 2026-06-15 eval run)

- **Corpus:** 9,103 chunks · 12 books · 4,395 distinct entities.
- **Retrieval:** Hit@1 83.3% · spell_lookup Hit@1 96% · Recall@10 94.3%.
- PHB OCR repair + re-ingest (PR #20, 2026-07-02) and the short-entity guard (#25) landed after
  this run; re-run `eval_golden.py` before quoting numbers.

## Known gaps / follow-ups (Beads)

- **x5bz.1.6** — open Cloud Run ingress to testers. The deployment is live but **IAM-locked**;
  opening it is deliberately gated on auth (x5bz.2) being verified in the deployed service.
- **x5bz.3** — cost guard + rate limiting on `/chat`; worth having before external traffic.
- **swe1.5** — notes/GM-lore nav (AppHeader slot reserved); was blocked on auth, now unblocked.
- **agent-forge-harness-1nh** — OCR Wayfinders + Blood Hunter (deferred; needs tesseract/ocrmypdf).
- **agent-forge-harness-ask** — `detect_collapse` false-positives on multi-form monsters; deep
  two-column MM tail.

## The conversation timeline (GM Workbench)

A conversation is read as **exchanges**, newest first: a turn carries its own
outcome, so a page boundary can never separate a prompt from its result. The
wire shapes are `service/workbench_contracts.py`'s (`TimelinePage`, `ChatEntry`,
`ChatAnswer`, `OpaqueEntry`); the read model is `service/timeline.py`, and every
statement it rests on is in `service/timeline_store.py` — the read model holds
no SQL at all, and a test walks its syntax tree to keep it that way.

**The legacy messages endpoint is unchanged.** `GET …/messages` returns exactly
what it returned before — same fields, same oldest-first order, same limit
semantics, same 403, same 503 — and keeps its cold-start claim. The timeline is
a second, additive read of the same rows: nothing is rewritten, backfilled or
deleted, and the read model writes nothing at all. Ownership is resolved through
`owner_of` in one read-only statement, in the same transaction as the read, and
a conversation that is missing, that has no ownership row, or that belongs to
another user is refused identically, from one code path (threat model §8.1,
SEC-2, SEC-3) — and it is never claimed. A legacy conversation whose id does not
fit the contract's `OpaqueId` shape (`^[A-Za-z0-9_-]{1,64}$`, which every id the
shipped UI mints does) is not readable through the timeline route — it answers
that same 404, before any statement runs — and remains readable through the
unchanged legacy route.

**Two sources, one order.** Turns taken after the durable timeline ships carry a
typed entry row; every older turn is **adapted** from its `chat.messages` rows,
with no backfill and no migration of old data. The two are merged by one total
key, descending `(created_at, source_rank, tiebreak)`, and a cursor carries each
source's position independently, because an item read but not taken from one
source must be re-read on the next page. The cursor is unpadded base64url and
encodes ids and times only — never a prompt, an answer or any search text (X-7).

**What an adapted row cannot know, it says with `null`.** `answerable` and
`sources` are `null` — *not recorded* — and never `true` or `[]`, which is what
the client used to invent for a row that never stored them. A legacy `entry_id`
is the decimal `chat.messages.id` of the exchange's oldest row (SEC-4 permits
it: it is only ever resolved inside a conversation the caller owns), so it can
never collide with a minted `ent_` id.

**Every row is read through `entry_or_opaque`.** A row this server cannot
validate — written by a newer version before a rollback, damaged, or carrying a
`mode` no contract knows (`chat.messages.mode` has no CHECK) — becomes an
`opaque` entry in the same place, carrying nothing of the payload. The stored
row is left untouched: never repaired, never rewritten, never dropped, so it
renders again after a roll-forward, and one bad row never takes a page down.
**404, and never a claim.** The route resolves ownership through `owner_of` in
one read-only statement inside the same transaction as the read, and a
conversation that is missing, has no ownership row, or belongs to another user
answers the **identical 404** — the Workbench scaffolding's one body, from
`not_found()` — from one code path (threat model §8.1, SEC-2, SEC-3). That is
deliberately unlike `GET …/messages`, which answers 403 and claims an unowned
conversation that has content — grandfathered, and left as it is. The route is
a Workbench route (`service/timeline_api.py`, see below): the router answers
every authentication failure with the one 401 body and requires the `dm` role
(SEC-2) before the handler reads anything; `limit` and `cursor` are validated
by the route and refused through the one validation handler, so FastAPI's
default 422 can never echo the request back (SEC-23); and it fails closed with
`backend_unavailable` when the store or the database is away.

**The typed entries.** `chat.timeline_entries` holds one row per exchange that
`POST /chat` answered once the durable timeline shipped. The row is the entry's
identity and time — `entry_id` (minted: `ent_` and 128 CSPRNG bits, SEC-4),
`created_at`, and `seq`, which only breaks ties — and `payload` is the validated
wire entry. The write refuses, before any statement runs, a payload that
disagrees with its own row, and anything the contract does not declare: a
document body, an assembled prompt, attachment text, a provider payload or an
owner's user id cannot ride along. No hash or digest of any text is stored
(ED-26). An entry goes with its conversation, and a conversation with its user.

**Two link columns, so no turn renders twice.** `user_message_id` and
`assistant_message_id` name the `chat.messages` rows the entry carries. A legacy
row an entry names is *covered*: it is never adapted again, and it closes any
exchange left open before it, so a lost answer can never pair with a later
turn's prompt. The rule is derived from data rather than from time, so it holds
under rollout, rollback and a failed write, with no watermark and no backfill.
The ids come from `MessageStore.append`'s return value — the one change to
`service/history.py` — never from a second query.

**Best-effort, after the answer.** The write runs after `svc.answer(...)` has
returned and both message rows are written, on one short transaction of its
own; nothing is held across the provider call and nothing is added before it.
It catches everything and logs one content-free line (the mode, the
conversation id and the exception's type), so it can never fail an answer or
change a byte of one. A turn whose entry could not be written — the contract
bounds sources, suggestions and text where `/chat`'s own models do not — reads
back adapted from its rows, with `answerable` and `sources` *not recorded*.

**The merged cursor.** A page is two bounded windows: `limit + 1` entry rows and
`2 * limit + 3` legacy rows. The legacy position moves past covered rows as well
as the exchanges a page takes, or a window full of covered rows would never
move; and while the legacy window comes back full, nothing older than its oldest
row is taken from either source, because an older exchange may still wait
below it. A page may therefore hold fewer entries than asked for — even none —
with a non-null `next_cursor`; the walk still ends, and it never loses, repeats
or reorders an entry.

## Workbench routes: the posture every new route inherits

`service/workbench_api.py` (agent-forge-harness-oe6) is the seam every Workbench
GM route attaches to. It holds no route of its own. The routes on it are the
four conversation routes (`service/conversations_api.py`) and the conversation
timeline (`service/timeline_api.py`), which `agent-forge-harness-oqx` moved
off its hand-built `@app.get`.

**What makes a route a Workbench route.** It is declared on a router made by
`workbench_router(...)`, so its route object is a `WorkbenchRoute`. Membership
is by class and never by path: `/conversations` is served by legacy routes and
will be served by Workbench ones, and at request time `scope["route"].path`
does not carry an `include_router` prefix. SEC-23 and S-A say the validation
handler "branches by path"; branching by route membership is the same rule
with a key that can be implemented.

**Wiring a route module.** A route module cannot import `service.app`, which
imports it, so it takes the application's GM dependency as a parameter:

```python
# in service/app.py, after require_session is defined:
WORKBENCH_GM = workbench_api.gm_session(require_session)
app.include_router(conversations_api.build_router(WORKBENCH_GM))

# in service/conversations_api.py, which imports nothing from service.app:
def build_router(gm: SessionDependency) -> APIRouter:
    router = workbench_api.workbench_router(gm)

    @router.get("/conversations")
    def index(session: SessionData = Depends(gm)) -> ConversationPage: ...

    return router
```

`gm_session` declares `Depends(require_session)` rather than calling it, so the
test suite's default-session override and E2E's overrides reach through it. It
is also the one place the `dm` rule lives: when `yje.4.1` replaces roles with
tiers, it is what changes.

**GM routes only.** `workbench_router` applies the `dm` gate, so it is for GM
routes and nothing else. Table routes wait on `hgm` (TA-2) and must reuse
`origin_check` in a factory of their own. A legacy route moved onto a router
(`iu6`) goes on a plain `APIRouter`, never this one, or its 401 and 422 bodies
change.

**The order of checks, as a client observes it.**

1. Malformed JSON under a JSON content type (`application/json` or `+json`),
   on a route that declares a body model, is a 422 with the Workbench
   validation body *before* the origin check and before authentication:
   FastAPI parses the body before it solves any dependency. Nothing has run
   and no resource is named, which SEC-3 permits in its own words. Any other
   content type reaches the origin check and is refused there. A route that
   reads its body by hand has no such case.
2. The origin check (state-changing methods only) → 403. This runs ahead of
   authentication by inference, not because SEC-3 lists it: an origin refusal
   names no resource and no account, and running it first refuses a forged
   request before an attacker-supplied cookie is looked up. It is structural
   (`workbench_router`'s dependency list); reversing it swaps two entries.
3. Authentication → the one 401 body.
4. Role → 403, naming no resource.
5. Ownership, in the statement → `not_found()`.
6. Validation that depends on the resource, then state (409) — so neither is
   reachable for a resource the caller does not own.

**The answers.**

| Case | Status | Body |
| --- | --- | --- |
| any authentication failure | 401 | `{"detail": "not signed in"}` — outside the envelope, see `workbench-wire-contract.md` |
| missing, someone else's, deleted | 404 | `not_found` / "That isn't available." |
| not a GM | 403 | `forbidden` / "This is a Game Master feature." |
| origin, fetch-site or content type | 403 | `forbidden` / "That request didn't come from this application." |
| validation | 422 | `validation_error_body(...)`, which echoes nothing; the log line carries `redacted_errors(...)`, the method and the route template |

`install_workbench(app)` registers the two application-wide handlers that make
this so. Every answer that is not a Workbench 401 or 422 — every legacy route,
every unknown path, every 405 — is produced by FastAPI's own default handler,
byte for byte; a golden-bytes test pins the legacy answers, and two identity
tests pin that the real app has both handlers installed.

**The origin rule.** For `POST`, `PUT`, `PATCH` and `DELETE`: an `Origin`, if
sent, must name the host the request was sent to (`Origin: null` is refused);
a `Sec-Fetch-Site`, if sent, must be `same-origin`; and a request that carries
a body must declare `application/json`. A request with neither header is not a
browser and is allowed. There is no CORS: no `Access-Control-*` header, and
`OPTIONS` is 405.

The host of `Origin` is compared with the `Host` header; the port only when
`Host` carries one, and the scheme never. The service cannot see its own
scheme (Cloud Run forwards HTTP) and nginx forwards `Host` without the
browser's port, so a stricter comparison would refuse every production and
every E2E request. The allow table, each row tested as a real 200:

| `Host` | `Origin` | Where it comes from |
| --- | --- | --- |
| `127.0.0.1` | `http://127.0.0.1:4173` | E2E through nginx (`proxy_set_header Host $host` drops the port) |
| `localhost:5173` | `http://localhost:5173` | the Vite dev proxy (string target, no `changeOrigin`) |
| `svc-xyz.a.run.app` | `https://svc-xyz.a.run.app` | Cloud Run (TLS ends at the edge) |
| `testserver` | none | not a browser |

*Residual:* where a proxy strips the port, a page on the same host at another
port or scheme passes the `Origin` comparison. That is unreachable in
production (one host, one port) and irrelevant in the E2E stack, and
`Sec-Fetch-Site`, which does compare scheme and port, is enforced whenever a
browser sends it. The reversal is a `WORKBENCH_ALLOWED_ORIGINS` setting.

**Enumerating routes.** `api_routes(app)` is the one way anything here reads
what the app serves. `app.routes` no longer is the route table —
`include_router` appends one private object and the included routes are not in
it — and `fastapi.routing.iter_route_contexts` yields route *contexts*, which
proxy `.path` and `.dependant` but are never an instance of `APIRoute`; the
route object is `ctx.original_route`. `app.openapi()["paths"]` is not used
either, because a route declared with `include_in_schema=False` is missing from
it. The route census, the auth-matrix walk, the proxy guard and the SPA-parity
walk all read the table this way, so a router-mounted route cannot land
unseen; the census fails until a new route's author declares it legacy or
Workbench. The auth-matrix walk reads `api_route_dependants(app)`, each route's
*effective* dependant: a guard passed to `include_router(..., dependencies=...)`
is there and never on the route object's own `.dependant`. The SPA fallback's
routes are left out by their `spa:` name prefix only; the `ui` mount is not an
API route and never appears.

**A route never builds its own 401, 403 or 404.** A structural check in
`service/tests/test_workbench_api.py` reads the syntax tree of every Workbench
route's endpoint module and refuses a 401/403/404 however it is spelled — a
literal, a `status` constant or an `HTTPStatus` member, on one line or split
across several, or handed to a helper of the module's own. The one exemption is a line
carrying `# workbench-api: deliberate-status` with a reason on the same
comment, for a switched-off capability that must answer exactly like a path
that does not exist (`1kg.8.1`'s dark routes). The repository's Python holds
none today, and the test asserts that count.

**Contract parity gates deploy.** The timeline route serves the Workbench
contract, so `contract-parity` is now one of `deploy`'s `needs` and a
top-level clause of its `if:` (see `docs/ci.md`).

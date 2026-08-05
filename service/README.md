# Service — FastAPI + LangGraph RAG API

The REST API that answers D&D 5th Edition questions grounded in the ingested corpus
(9,000+ chunks across 12 books in pgvector). Every `/chat` request runs a **LangGraph
pipeline**: embed → hint extraction → mode scoping → filtered vector search → (gated
rerank) → grounding gate → per-persona generation (`gpt-4o-mini`) → citations.
Out-of-corpus questions are refused, not hallucinated. Beyond chat it persists
**message history** and **file attachments** per conversation.

## File map

| File | Role |
| --- | --- |
| `app.py` | FastAPI app: endpoints, startup wiring (RagService + message store), error taxonomy, static `ui/dist` mount. |
| `graph.py` | The whole request pipeline as a LangGraph `StateGraph` — every stage is a node (see below). |
| `rag.py` | `RagService` — thin invoke wrapper around the graph; dependency injection seams (retriever, reranker, LLM client, secondary retriever). Home of the stubbed **secondary world-corpus retriever** seam for GM mode. |
| `generate.py` | Context assembly (full chunk texts, never previews), per-mode persona prompts, grounded answer + spell-suggestion LLM calls, `Source` building. |
| `models.py` | Pydantic request/response contract (mirrored by `ui/src/api.ts`). Home of the canonical `REFUSAL` string. |
| `history.py` | `MessageStore` protocol + Postgres/in-memory impls — `chat.messages` / `chat.attachments` in the same DB as the corpus; idempotent `ensure_schema()` at startup. |
| `attachments.py` | Pure text extraction for uploaded files (`.txt`/`.md` decode, `.pdf` via PyMuPDF) + `cap_text`. Deliberately separate from `ingestion/extract*.py` (those are whole-book, path-based). |
| `tracing.py` | Env-gated Langfuse tracing (`RAG_TRACING`, off by default) — node-level trace + token/cost span per request. |

Retrieval logic itself lives in `ingestion/retrieval.py` (`RagRetriever`) and is shared
with the evals; mode→scope mapping is `ingestion/scope.py`. Tuning knobs live in the
top-level [`config.py`](../config.py).

## The pipeline graph (`graph.py`)

```text
START → preflight ──(empty prompt)──────────────────────────▶ refuse → END
           │ (valid)
           ▼
        embed → extract_hints → scope ──(gm: fan-out)──▶ secondary
                                  │                          :
                                  ▼                          : (joins by state)
                               search → fetch_texts ──▶ [rerank?] → merge
                                                                      │
                                                                      ▼
                                          refuse ◀──(refuse)── gate ──(generate)─▶ generate
                                             │                                       │
                                             ▼                    (spell) suggest ◀──┤
                                            END                              └─▶ cite → END
```

- **preflight** validates the mode (unknown → `ValueError` for direct callers; the API layer
  already 422s via the `ChatMode` enum) and short-circuits empty prompts to `refuse`.
- **rerank** runs only when a reranker is configured (`RAG_RERANK=1` + `[rerank]` extra)
  **and** the query looks prose-like (`should_rerank`).
- **gate** is the grounding decision: `sage`/`spell`/`rules` need `answerable` (top-1 cosine
  distance ≤ 0.50) *and* chunks; `gm` proceeds with any chunks (creative mode, `answerable`
  may stay false); a conversation **attachment** relaxes the gate entirely — "what does my
  homebrew doc say?" must generate even when the corpus can't answer.
- **suggest** (spell mode only) adds three spell-usage ideas (practical/roleplay/wacky) via a
  second LLM call — best-effort: failure degrades to `suggestions: null`, never a failed answer.
- In **gm** mode, `scope` fans out to a **secondary retriever** (future world/campaign corpus —
  currently a stub returning nothing) in parallel with the primary search; `merge` dedupes.

## Chat modes

| Mode | Persona / system prompt | Retrieval scope |
| --- | --- | --- |
| `sage` (default) | General oracle, strict grounding | Unscoped |
| `spell` | Spell Archivist — quotes rules text verbatim; adds 3 usage suggestions | `spell` chunks, spell-bearing books only |
| `rules` | Rules Arbiter — RAW only, no table rulings | Rules-type chunks (rule, class/race_feature, condition, background, feat) |
| `gm` | GM Oracle — may invent, must say so | Query types ∪ {monster, dm_guidance, magic_item}; relaxed gate; secondary-corpus seam |

## Endpoints

### `POST /chat`

```json
{ "prompt": "What does a Mind Flayer do with its tentacles?", "mode": "sage", "conversation_id": "abc" }
```

`mode` defaults to `sage`; `conversation_id` is optional — when present, the turn is
persisted and any stored attachments of that conversation are injected as extra context.

```json
{
  "answer": "… dealing 15 (2d10 + 4) psychic damage … [1]",
  "sources": [ { "book": "mm-5e", "chapter": "Bestiary", "section": "Stat Block",
                 "entity": "Mind Flayer", "page": 223, "snippet": "…" } ],
  "answerable": true,
  "mode": "sage",
  "conversation_id": "abc",
  "suggestions": null
}
```

Out-of-corpus → grounded refusal (**200**, no LLM call): fixed `REFUSAL` answer, empty
sources, `answerable: false`. In spell mode, `suggestions` carries exactly three
`{style, text}` objects (`practical` / `roleplay` / `wacky`) or `null` if that garnish failed.

### `GET /conversations/{id}/messages`

Stored history, most recent `RAG_HISTORY_LIMIT` (50) turns, served oldest-first;
`?limit=` may lower, never raise, the cap. Assistant turns from spell mode carry their
suggestions. 503 when the store is unavailable.

### `POST /conversations/{id}/attachments`

JSON body `{ "filename", "content_type", "data" }` with **base64** file content (no
multipart). Text is extracted server-side (`.txt`/`.md`/`.pdf`) and stored; from then on it
grounds every answer in that conversation (capped at `RAG_ATTACHMENT_MAX_CHARS`) and is
cited as an extra source. Errors: `413` over `RAG_ATTACHMENT_MAX_BYTES` (2 MB), `415`
unsupported type, `422` bad base64.

### `GET /conversations/{id}/attachments` · `GET /healthz`

Attachment **metadata** only (extracted text never leaves the server); health + readiness.

### Error taxonomy

| Status | Meaning |
| --- | --- |
| `401` | No / invalid / expired session, or the account no longer exists |
| `403` | Wrong role for the channel (GM is DM-only), or another user's conversation |
| `422` | Validation (empty prompt, unknown mode, bad upload body, bad credentials shape) |
| `429` | Auth attempt budget exhausted for this account or source — carries `Retry-After` and `X-Auth-Throttled: 1`. That header marks the response as *ours*: Cloud Run also returns 429 when no instance is available, and nothing else distinguishes them |
| `502` | LLM upstream failed (timeout/rate limit) — retryable |
| `503` | Retrieval backend, embedding (missing `OPENAI_API_KEY`), store or auth unavailable, `SESSION_SECRET` unusable, or hashing capacity exhausted |
| `500` | Bug in our code (full traceback logged) |

History writes are **best-effort by design**: a failed persist logs a warning and never
fails an answer. Authorization is the opposite — it **fails closed**: an ownership lookup
that errors returns 503 rather than serving the content.

## Auth (x5bz.2)

Access is invite-gated; `/chat` and `/conversations/*` require a session.

| Module | Role |
| --- | --- |
| `auth_store.py` | `AuthStore` protocol + Postgres/in-memory impls — `auth.users` / `auth.invites`, idempotent `ensure_schema()`. Invite redemption is **atomic** (`UPDATE ... WHERE used_at IS NULL ... RETURNING`), so concurrent redeemers can't both win. |
| `hashing.py` | argon2id hash/verify with **explicit** parameters, plus a semaphore capping concurrent hashes — argon2 is memory-hard and `/auth/login` hashes on every attempt, so unbounded concurrency is an OOM lever. |
| `session.py` | itsdangerous-signed httpOnly cookie carrying user id + role. Stateless: no session table; rotating `SESSION_SECRET` logs everyone out. |
| `invites.py` | Token generation (`secrets.token_urlsafe(32)`) + redeemability rules (used / expired / revoked). |
| `admin_invites.py` | Operator CLI: `create` / `list` / `revoke`. `create` prints a `/#invite=<token>` link — the token rides in the **fragment**, which browsers never send, so it stays out of request logs. |
| `ratelimit.py` | Attempt budgets for `/auth/signup` + `/auth/login`, checked **before** any argon2 work — per account (10 / 5 min) and per source IP (30 / 5 min), both must pass. 429 + `Retry-After` + `X-Auth-Throttled`; decays, never locks out. |

`require_session` re-reads the account on **every** request rather than trusting the
cookie's contents, so a deleted account or a demoted DM loses access immediately instead
of at cookie expiry. Roles are server-authoritative: the invite fixes the role, and the UI
cannot change it. `/healthz` and `/metrics/ui` are intentionally open.

**The source budget depends on deployment config.** `X-Forwarded-For` is caller-writable —
Google preserves whatever the client sent and appends its own observation — so the source
key is read from the **right-hand (trusted) end** of the chain, `AUTH_TRUSTED_PROXY_HOPS`
entries in, and everything left of that is ignored. The default is `0` (trust nothing in
the header, key on the peer address); `scripts/deploy.sh` sets `1` for Cloud Run's run.app
front end, and an external HTTPS load balancer in front would make it `2`. Set too high it
starts trusting caller-supplied entries; left at `0` behind a proxy every caller collapses
into one shared bucket. It is only sound while ingress is restricted to that front end —
a caller who can reach the container directly *is* the trusted hop.

The budgets are **per instance and in memory**, so with `--max-instances N` the real
ceiling is N × these numbers; a shared store (Redis/DB) is the answer if it ever needs to
be exact. Rate limiting for `/chat` itself is separate and still open (x5bz.3).

**Account deletion is enforced by the database, not only by policy.** `require_session`
validates at request *start*, so a request already in flight when an account is deleted
keeps running with its decision — for up to the Cloud Run request timeout. Foreign keys
close that window: `chat.conversations.user_id → auth.users` and
`chat.messages`/`chat.attachments` → `chat.conversations`, all `ON DELETE CASCADE`. So a
late write into a deleted account's conversation is *rejected* rather than silently
recreating it, and one `DELETE FROM auth.users` removes the account's content. (The
constraints are added `NOT VALID`, so conversations predating the ownership table keep
their messages.) The incident runbook in `docs/deploy-gcp.md` §10 still revokes access
first and drains before deleting — the constraints are the backstop, not the plan.

The constraint migrations check `pg_constraint` and only run when a constraint is
**missing or wrong** — the predicate pins the child and referenced tables, the delete
action (`confdeltype`) *and* the exact columns (`conkey`/`confkey`), so a same-named
foreign key on a different column can't pass for the real one and leave the intended
column unprotected. `ensure_schema()` runs at every startup and
Cloud Run scales to zero, so an unconditional `DROP`/`ADD` would take an `ACCESS
EXCLUSIVE` lock on a live table at each cold start — blocking queries until the startup
transaction commits and serializing simultaneous starts — and would re-scan the table to
re-validate the invites FK every time.

## Run

```bash
docker compose up -d vector-db          # corpus DB; .env needs OPENAI_API_KEY
uv run --with . uvicorn service.app:app --port 8000 --reload
```

**Single-process serving (UI + API):** `cd ui && bun run build`, then start uvicorn as
above and open <http://localhost:8000> — `app.py` mounts `ui/dist/` when it exists.
In the proxied modes (:5173 dev or compose), every service API prefix — `/chat`,
`/healthz`, `/conversations`, `/metrics`, `/auth` — must be listed in **both**
`ui/vite.config.ts` and `ui/nginx.conf` when you add an endpoint, or the SPA
fallback swallows it (a GET returns `index.html`, a POST returns 405).
`tests/test_proxy_contract.py` derives the list from the real route table and
fails CI if either front end is missing one; see also the proxy invariant in
`ui/README.md`.

## Config

`OPENAI_API_KEY` (required), `DATABASE_URL` (defaults to the local compose DSN), and the
`RAG_*` knobs — canonical defaults + rationale in [`config.py`](../config.py):

| var | default | purpose |
| --- | --- | --- |
| `RAG_TOP_K` | `10` | chunks returned per vector search (pre-rerank) |
| `RAG_CONTEXT_TOP_N` | `5` | chunks fed to the LLM + cited |
| `RAG_SNIPPET_MAX` | `240` | display-snippet length |
| `RAG_ANSWERABLE_DISTANCE` | `0.50` | koz grounding gate (top-1 cosine distance) |
| `RAG_FALLBACK_DISTANCE` | `0.42` | ipl filtered→unfiltered retry — **eval-only**, never used live |
| `RAG_DEFAULT_MODEL` | `gpt-4o-mini` | generation model |
| `RAG_TEMPERATURE` | `0.2` | generation temperature |
| `RAG_HISTORY_LIMIT` | `50` | messages returned per conversation |
| `RAG_ATTACHMENT_MAX_BYTES` | `2000000` | max decoded upload size |
| `RAG_ATTACHMENT_MAX_CHARS` | `6000` | max attachment chars injected into the prompt |
| `RAG_RERANK` | `0` | gated cross-encoder rerank (needs `pip install '.[rerank]'`) |
| `RAG_TRACING` | `0` | Langfuse tracing (`LANGFUSE_PUBLIC_KEY`/`SECRET_KEY`/`BASE_URL` when on) |

## Tests

Pure unit + endpoint tests (retriever, LLM, and store are faked — no DB or network), in
`service/tests/`. From the **repo root**:

```bash
uv run --with '.[test]' python -m pytest service -q     # service only
uv run --with '.[test]' python -m pytest -q             # whole suite
```

## Not yet built (follow-ups)

- Streaming (SSE) answers.
- Connection pooling (per-request connect today).
- Rate limiting / spend cap on `/chat` (x5bz.3) — needed before external traffic.
- Password reset (needs email sending) and an in-app admin surface for invites
  (the CLI `python -m service.admin_invites` is the pilot's admin path).
- A real secondary world-corpus retriever behind the GM seam.

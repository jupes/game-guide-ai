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
| `422` | Validation (empty prompt, unknown mode, bad upload body) |
| `502` | LLM upstream failed (timeout/rate limit) — retryable |
| `503` | Retrieval backend, embedding (missing `OPENAI_API_KEY`), or store unavailable |
| `500` | Bug in our code (full traceback logged) |

History writes are **best-effort by design**: a failed persist logs a warning and never
fails an answer.

## Run

```bash
docker compose up -d vector-db          # corpus DB; .env needs OPENAI_API_KEY
uv run --with . uvicorn service.app:app --port 8000 --reload
```

**Single-process serving (UI + API):** `cd ui && bun run build`, then start uvicorn as
above and open <http://localhost:8000> — `app.py` mounts `ui/dist/` when it exists.
This is currently the only mode where history + attachments work end-to-end: the Vite
dev proxy and nginx forward `/chat` + `/healthz` but **not `/conversations/*`**
(tracked as `agent-forge-harness-cnqf`).

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
- Real auth / server-side role enforcement (the GM channel is UI-gated only until then).
- A real secondary world-corpus retriever behind the GM seam.

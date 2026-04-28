# RAG Chat — Implementation Plan

> **Status**: Draft — in active development with owner
> **Last updated**: 2026-04-28

---

## Overview

Build a minimal RAG pipeline as three independent services connected by HTTP/WebSocket APIs.
Each service is scoped to do one thing well. No framework magic — just clean interfaces.

---

## Phase 1 — Vector DB Setup

**Goal**: Stand up a vector database and confirm it can store and retrieve embeddings.

- Choose and configure a vector DB (e.g. Qdrant, Chroma, pgvector)
- Define collection/index schema: embedding dimensions, metadata fields (source, chunk_id, text)
- Verify round-trip: insert a vector, query by similarity, get expected result back
- Document connection string and auth approach

**Acceptance criteria**:
- Vector DB runs locally (Docker or embedded)
- Insert + similarity search works via CLI or test script
- Schema documented in `vector-db/README.md`

---

## Phase 2 — Data Ingestion

**Goal**: Load source documents into the vector DB so the agent has something to retrieve from.

- Define document source format (files, URLs, plain text)
- Chunk documents into passages (configurable chunk size + overlap)
- Embed each chunk via embedding model API (e.g. OpenAI `text-embedding-3-small`)
- Upsert vectors + metadata into the vector DB
- Idempotent: re-running ingestion on the same source should not duplicate records

**Acceptance criteria**:
- Ingestion script accepts a source path or glob
- Documents are chunked, embedded, and stored
- Querying the DB after ingestion returns relevant chunks for a test query

---

## Phase 3 — Agent Service

**Goal**: Build the orchestration API that sits between the chat app and the retrieval + LLM layers.

### Endpoints
- `POST /chat` — accepts `{ prompt: string }`, returns `{ response: string, sources: Chunk[] }`

### Internals
1. Embed the incoming prompt using the embedding model
2. Query the vector DB for top-K similar chunks
3. Build a context window: system prompt + retrieved chunks + user prompt
4. Call the LLM (e.g. Claude via Anthropic SDK) with the assembled context
5. Return the LLM response and the source chunks used

**Acceptance criteria**:
- `POST /chat` returns a grounded response for a prompt covered by ingested data
- Sources array identifies which chunks were used
- Service is stateless (no session stored server-side)

---

## Phase 4 — Chat App

**Goal**: Simple browser UI for submitting prompts and reading responses.

- Single-page app (React or plain HTML + fetch is fine)
- Input: text field + submit button
- Output: response text with collapsible source citations
- Loading state while waiting for agent service

**Acceptance criteria**:
- User can type a prompt, submit it, and see a response
- Source citations are visible
- Works against the local agent service

---

## Open Questions

- [ ] Which vector DB? (Qdrant Docker recommended for local dev simplicity)
- [ ] Which embedding model? (OpenAI `text-embedding-3-small` unless we want local)
- [ ] What is the source data corpus? (Decide before starting ingestion)
- [ ] LLM for generation? (Claude via Anthropic SDK recommended)
- [ ] Auth between services? (None for local dev; document the gap)

---

## Service Boundaries

```
chat-app/          # Phase 4 — frontend only, no business logic
agent-service/     # Phase 3 — embedding, retrieval, LLM orchestration
vector-db/         # Phase 1 — DB config, docker-compose, schema
ingestion/         # Phase 2 — chunking + embedding + upsert scripts
```

Each service has its own `package.json` (or equivalent) and can be started independently.

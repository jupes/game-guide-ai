# RAG Chat — Implementation Plan

> **Status**: Draft — in active development with owner
> **Last updated**: 2026-04-28
> **Runtime**: Docker Compose — all services run as containers

---

## Overview

Build a minimal RAG pipeline as three independent services connected by HTTP/WebSocket APIs.
Each service is scoped to do one thing well. No framework magic — just clean interfaces.

All services run in Docker containers and are orchestrated with a single `docker-compose.yml` at the repo root.
`docker compose up` brings the entire stack online; each service has its own `Dockerfile`.

---

## Phase 1 — Vector DB Setup

**Goal**: Stand up a vector database and confirm it can store and retrieve embeddings.

- Choose a vector DB and define its Docker image/service in `docker-compose.yml`
- Define collection/index schema: embedding dimensions, metadata fields (source, chunk_id, text)
- Mount a named Docker volume for data persistence across container restarts
- Verify round-trip: insert a vector, query by similarity, get expected result back
- Document connection string, port mapping, and volume config

**Acceptance criteria**:
- `docker compose up vector-db` starts the DB container
- Insert + similarity search works via CLI or test script against the container
- Data persists across `docker compose down` + `up` via named volume
- Schema and connection details documented in `vector-db/README.md`

---

## Phase 2 — Data Ingestion

**Goal**: Load source documents into the vector DB so the agent has something to retrieve from.

- Define document source format (files, URLs, plain text)
- Chunk documents into passages (configurable chunk size + overlap)
- Embed each chunk via embedding model API (e.g. OpenAI `text-embedding-3-small`)
- Upsert vectors + metadata into the vector DB
- Idempotent: re-running ingestion on the same source should not duplicate records
- Ingestion runs as a short-lived Docker container (`docker compose run ingestion`) that exits on completion
- Source documents are bind-mounted into the container at runtime (`./data:/data`)

**Acceptance criteria**:
- `docker compose run ingestion` processes documents from the bind-mounted `./data` directory
- Documents are chunked, embedded, and stored in the vector DB container
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

- Service runs as a Docker container; port exposed via `docker-compose.yml` (e.g. `3001:3001`)
- Communicates with the vector DB container over the Docker internal network (no localhost)
- API key / env config injected via `.env` file (never committed)

**Acceptance criteria**:
- `docker compose up agent-service` starts the service and exposes the API
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

- Served as a static build from an Nginx Docker container (or Node dev server in development)
- Port exposed via `docker-compose.yml` (e.g. `3000:80`)
- Agent service URL injected at build time via env var

**Acceptance criteria**:
- `docker compose up chat-app` serves the UI on `localhost:3000`
- User can type a prompt, submit it, and see a response
- Source citations are visible
- Works end-to-end against the local agent service container

---

## Open Questions

- [ ] Which vector DB? (Qdrant Docker image recommended — `qdrant/qdrant`)
- [ ] Which embedding model? (OpenAI `text-embedding-3-small` unless we want local)
- [ ] What is the source data corpus? (Decide before starting ingestion)
- [ ] LLM for generation? (Claude via Anthropic SDK recommended)
- [ ] Auth between services? (None for local dev; all on Docker internal network)
- [ ] Hot-reload in dev? (Volume-mount source + use nodemon/vite dev server vs. rebuild image)

---

## Service Boundaries

```
docker-compose.yml       # Root orchestration — brings up all services
.env.example             # Template for required env vars (committed)
.env                     # Actual secrets — NEVER committed
data/                    # Source documents for ingestion (bind-mounted)
chat-app/                # Phase 4 — frontend; Dockerfile builds static Nginx image
agent-service/           # Phase 3 — orchestration API; Dockerfile builds Node image
vector-db/               # Phase 1 — DB config and schema docs (no Dockerfile; uses upstream image)
ingestion/               # Phase 2 — one-shot script container; Dockerfile builds Node image
```

Each service directory contains its own `Dockerfile` (except `vector-db/`, which uses the upstream image directly).
The root `docker-compose.yml` wires them together on a shared internal network.

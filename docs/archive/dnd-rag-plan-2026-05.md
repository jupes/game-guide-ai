# D&D 5e Ruleset RAG — Implementation Plan

> **ARCHIVED** (2026-07-21): the original pre-build draft, kept for provenance. It predates the
> rag-chat → game-guide-ai rename and much of what shipped; the living docs are
> [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) and the folder READMEs. Formerly the repo-root `plan.md`.
>
> **Status**: Draft — **product track**: semantic search and RAG over **Dungeons & Dragons 5th Edition** rules text  
> **Last updated**: 2026-05-03  
> **Repo**: **rag-chat** (this repository)

**Not in scope here**: [Agent Forge](https://github.com/jupes/agent-forge-harness) **harness RAG** over dev `repos/` with **local pgvector**. That plan is committed in the harness: **`docs/plans/harness-rag-local-vector.md`**.

**Beads (harness tracker)**: Epic **agent-forge-harness-17u** — *D&D 5e ruleset RAG (GCP)*.

---

## Overview

Deliver a **rules-grounded** Q&A or search experience for D&D 5e: user asks in natural language; the system retrieves relevant **official or licensed rules passages** (per your rights to the corpus), assembles context, and answers with **citations** to book/chapter/section or SRD paragraph.

**Default deployment shape**: **GCP**-oriented stack; **vector store** is **hosted** (e.g. **[Pinecone](https://www.pinecone.io/)**, or **Qdrant Cloud** / **Vertex AI Vector Search** / **AlloyDB pgvector** if you standardize on Google-native components). Pick one for v1; keep **ingestion**, **embedding**, and **query** behind adapters.

**Corpus**: You must only index text you have the **legal right** to use (e.g. **SRD** content, your own notes, OGL/CC material). This plan does not assume redistribution of non-SRD books.

---

## Phase 1 — Hosted vector index (GCP-friendly)

**Goal**: Provision a **managed** index/collection in GCP or a paired SaaS (Pinecone, etc.): dimensions, namespaces (e.g. `srd`, `house-rules`), metadata schema.

- Metadata fields (suggested): `source` (e.g. `SRD 5.2`), `section`, `chunk_id`, `text`, optional `page` / `topic_tags`.
- Secrets: API keys or workload identity in **Secret Manager** (or `.env` for local dev only); never commit secrets.
- Smoke test from dev machine or Cloud Shell: upsert small batch, query, assert neighbor ids.

**Acceptance**

- Dev/staging index exists; non-secret config documented in repo `README` or `vector-db/README.md`.
- Automated or scripted insert + similarity search passes.
- Clear story for **prod** vs **dev** namespaces or separate indexes.

---

## Phase 2 — Rules corpus ingestion

**Goal**: Chunk, embed, and upsert **5e rules** content into the hosted index.

- Normalize source files (Markdown, PDF extraction pipeline, or vendor-provided SRD JSON) into a **canonical chunk format**.
- Chunking tuned for **short rule paragraphs** and **tables** (tables may need special handling or flattening).
- Idempotent upserts using stable keys (`source` + `section_ref` + `chunk_index` or hash).
- Optional: separate ingestion job on **Cloud Run** or batch pipeline triggered after corpus updates.

**Acceptance**

- Full **allowed** corpus ingested; spot-check retrieval for iconic queries (“What is the Help action?”, “grappling rules”, spell slot progression).
- Re-run pipeline: no duplicate vectors for unchanged chunks.

---

## Phase 3 — Agent / API service

**Goal**: Stateless HTTP API: embed question → vector search → LLM with citations.

- Endpoints (align with your stack): e.g. `POST /rules/ask` or `POST /chat/stream` (SSE) returning final `sources[]`.
- System prompt: **only answer from retrieved text**; refuse or narrow when retrieval is empty or low-confidence.
- Log **request id** + retrieved ids for debugging; no storage of user prompts in prod unless product requires it (privacy).

**Acceptance**

- Grounded answers for golden questions with populated `sources`.
- Explicit behavior when retrieval finds nothing relevant.

---

## Phase 4 — Client (web or CLI)

**Goal**: Minimal UI or CLI for play at the table or prep: prompt in, answer + expandable citations out.

- Can be a small **React** app, static export to **Cloud Storage** + **HTTPS**, or **Firebase Hosting** — product choice.
- Show **book/SRD reference** per citation, not just internal chunk ids, for trust at the table.

**Acceptance**

- End-to-end demo from browser or CLI against staging API and index.

---

## Phase 5 — Evaluation (5e golden set)

Build **10–30** golden items grounded in **your** indexed corpus, for example:

| Field | Purpose |
|--------|--------|
| `question` | Natural language player/DM question |
| `expected_answer_shape` | Short correct ruling (for human or LLM-judge) |
| `expected_sources` | Section ids or headings that must appear in citations |
| `must_include` | Rule phrases that must appear in the answer |
| `must_not_include` | Common wrong rulings or wrong editions |

Use the set to tune **topK**, **chunk size**, **metadata filters** (e.g. filter to `Combat` only when the query implies combat).

---

## Open questions (D&D product)

- [ ] **Vector provider**: Pinecone vs Vertex Vector Search vs AlloyDB — cost, latency, EU region if needed.
- [ ] **Corpus**: SRD-only v1 vs licensed sources you control.
- [ ] **Embedding model**: OpenAI vs Gemini vs open weights — dimension locks index schema.
- [ ] **LLM**: same-vendor vs best-of-breed for cost/latency.
- [ ] **Multilingual** or English-only v1?

---

## Service boundaries (rag-chat repo, indicative)

```
docker-compose.yml       # Local dev: optional API + workers; index may still be cloud
cloudbuild.yaml / tf/    # Optional — GCP deploy artifacts (add when chosen)
ingestion/               # Chunk + embed + upsert to hosted index
agent-service/           # Rules Q&A API
chat-app/                # Or rules-ui/ — user-facing surface
vector-db/               # Docs for *hosted* schema + env vars (not necessarily local Postgres)
```

For **local-only experiments**, you may temporarily use the same **pgvector** Docker pattern as the harness plan; production intent for **this** product remains **hosted on GCP**.

---

## Next steps (D&D track)

1. Lock **corpus** legal scope and file layout.
2. Choose **hosted vector** + region; create dev index.
3. Ship ingestion for SRD slice → first golden eval.
4. Expose API + thin UI; iterate on filters and prompts before scaling corpus.

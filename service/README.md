# D&D 5e Agent Service (`POST /chat`)

A stateless REST API that answers D&D 5th Edition questions, grounded in the
ingested rules corpus (9,000+ chunks across 12 books in pgvector). Embed the
prompt → filtered + gated vector retrieval → grounded answer via `gpt-4o-mini`
→ response with source citations. Out-of-corpus questions are refused, not
hallucinated.

## Run

```bash
cd repos/game-guide-ai
docker compose up -d        # pgvector with the ingested corpus
# .env must provide OPENAI_API_KEY (embeddings + generation) and optionally DATABASE_URL
uv run --with fastapi --with uvicorn --with openai --with "psycopg[binary]" \
    uvicorn service.app:app --port 8000
```

The service loads the corpus vocabulary once at startup; each request is
independent (stateless).

### Single-process serving (UI + API, no nginx)

Build the UI first, then start uvicorn — it will serve both:

```bash
cd ui && bun run build      # writes ui/dist/
cd ..
uv run --with fastapi --with uvicorn --with openai --with "psycopg[binary]" \
    uvicorn service.app:app --port 8000
```

Open **<http://localhost:8000>**. The `ui/dist/` mount fires automatically at startup
(guarded — skipped when the directory is absent, e.g. inside the Docker service
container where nginx handles static files instead).

## Endpoints

### `POST /chat`

Request:

```json
{ "prompt": "What does a Mind Flayer do with its tentacles?" }
```

Response (`200`):

```json
{
  "answer": "A Mind Flayer uses its tentacles to make a melee weapon attack, dealing 15 (2d10 + 4) psychic damage. If the target is Medium or smaller, it is grappled (escape DC 15) and must succeed on a DC 15 Intelligence saving throw or be stunned until the grapple ends [1].",
  "sources": [
    { "book": "mm-5e", "chapter": "Bestiary", "section": "Stat Block",
      "entity": "Mind Flayer", "page": 223, "snippet": "Mind Flayer Medium aberration ..." }
  ],
  "answerable": true
}
```

Out-of-corpus prompt → grounded refusal (no LLM call):

```json
{ "answer": "I couldn't find that in the D&D 5e sources I have.", "sources": [], "answerable": false }
```

Errors: empty/missing `prompt` → `422`; retrieval/generation failure → `503`.

### `GET /healthz`

```json
{ "status": "ok", "ready": "True" }
```

## How it works

1. **Embed** the prompt (`text-embedding-3-small`).
2. **Detect** class/entity/content-type hints against the corpus vocabulary
   (with the generic-entity stoplist so junk terms don't over-restrict).
3. **Retrieve** top-K via filtered vector search; fetch **full** chunk text by id
   (the row preview is only 120 chars).
4. **Gate** answerability by top-1 cosine distance (`is_answerable`, ~0.50). If
   not answerable → refuse.
5. **(Optional) rerank** prose-category results with a gated cross-encoder.
6. **Generate** with `gpt-4o-mini` under a grounding prompt ("answer only from the
   numbered sources, cite as [n]").
7. **Cite** — one `Source` per contributing chunk (book/chapter/section/entity/
   page + snippet), deduped.

Retrieval logic is shared with the eval via `ingestion/retrieval.py`
(`RagRetriever`).

## Tests

The repo is an installable package (`pyproject.toml`); imports are explicit
(`from service... import ...`, `from ingestion... import ...`) with no `sys.path`
hacks. Run pytest from the **repo root** with the `test` extra:

```bash
# All service tests (pure + mocked endpoint):
uv run --with '.[test]' python -m pytest service -q
# Or the whole suite:
uv run --with '.[test]' python -m pytest -q
```

No DB or LLM is needed for the unit/endpoint tests (retriever + LLM are mocked).

## Config

| var | purpose |
|-----|---------|
| `OPENAI_API_KEY` | embeddings + generation (required) |
| `DATABASE_URL` | pgvector DSN (defaults to the local compose DSN) |

### RAG tuning knobs

All optional and env-overridable; the canonical defaults + rationale live in the
top-level [`config.py`](../config.py). Tune retrieval/generation without a code
change or redeploy.

| var | default | purpose |
| --- | --- | --- |
| `RAG_TOP_K` | `10` | chunks the vector search returns per query (pre-rerank) |
| `RAG_CONTEXT_TOP_N` | `5` | top chunks fed to the LLM context + cited sources |
| `RAG_SNIPPET_MAX` | `240` | max chars of each source's display snippet |
| `RAG_FALLBACK_DISTANCE` | `0.42` | ipl: top-1 distance above which a filtered query retries unfiltered |
| `RAG_ANSWERABLE_DISTANCE` | `0.50` | koz: top-1 distance within which the corpus is judged answerable |
| `RAG_DEFAULT_MODEL` | `gpt-4o-mini` | OpenAI chat model for answer generation |
| `RAG_TEMPERATURE` | `0.2` | answer-generation sampling temperature |

## Not in v1 (follow-ups)

- Streaming (SSE) for the UI epic (`3zs`).
- Connection pooling (per-request connect for now).
- Optional reranker wired in by default (it's pluggable; off by default to avoid
  the torch dependency in the running service).

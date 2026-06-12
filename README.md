# rag-chat

A D&D 5e RAG (Retrieval-Augmented Generation) chat application. Ask questions about spells,
monsters, rules, and lore — the Sage answers only from its sources, with citations, and
tells you when it can't answer.

## Architecture

```
User
  ↓ prompt
ui/  (React + Vite, dark grimoire theme)
  ↓ POST /chat
service/  (FastAPI — embed → filter → retrieve → rerank → gate → GPT-4o-mini)
  ↓ pgvector similarity search
vector-db  (PostgreSQL + pgvector, 9,000+ chunks across 12 D&D 5e books)
```

## Running E2E — one command

**Prereq:** a `.env` file in this directory containing your OpenAI key:

```
OPENAI_API_KEY=sk-...
```

The vector DB already holds the embedded corpus. Start everything with:

```bash
docker compose up --build
```

**URL: <http://localhost:5173>**

First build pulls images and compiles the Vite app (a few minutes). Subsequent starts are fast.

### Other compose commands

```bash
docker compose up --build -d     # detached (background)
docker compose logs -f           # tail all logs
docker compose down              # stop + remove containers (volumes/data preserved)
docker compose up vector-db      # DB only — if you prefer running the service locally
```

### Local dev (two terminals, faster iteration)

**Terminal 1 — service:**

```bash
uv run --with fastapi --with uvicorn --with openai --with "psycopg[binary]" \
    uvicorn service.app:app --port 8000 --reload
```

**Terminal 2 — UI dev server** (hot-reload, Vite proxy → :8000):

```bash
cd ui && bun run dev
```

URL: <http://localhost:5173>

## Directory layout

| Path | Description |
| ---- | ----------- |
| `service/` | FastAPI app — `POST /chat`, answerability gate, grounded generation |
| `ingestion/` | Chunking, embedding, retrieval pipeline, eval harness |
| `ui/` | React + Vite chat interface |
| `vector-db/` | DB init SQL and pgvector setup |
| `docker-compose.yml` | Full stack: vector-db → service → ui |
| `Dockerfile.service` | Python 3.12-slim image for the agent service |
| `ui/Dockerfile` | Multi-stage bun build + nginx serve |

# Aetheril — D&D 5e RAG Chat

Aetheril is a Retrieval-Augmented Generation chat assistant for D&D 5e. It answers questions grounded in rulebook text, with citations, and tells you plainly when it cannot find an answer.

## Design System

The UI is built on the **Aetheril design system** — a Material 3 token layer with a warm fantasy palette:

- **Light theme (Parchment)** — warm aged-paper surface, ember primary, old-gold secondary, verdigris tertiary.
- **Dark theme (Tavern)** — deep tavern-by-candlelight inversion. Toggle in the top bar.

Ten DS components cover the full interface: Button, IconButton, TextField, Switch, Card, Chip, Avatar, Badge, DiceRoll, and ChatMessage. All values come from design tokens; no hard-coded hex colours.

## Chat Modes

The workspace hosts four chat personas, each with its own retrieval scope and system prompt:

| Mode | Persona | Retrieval scope |
| --- | --- | --- |
| **Sage** | General D&D oracle | All sources |
| **Spell** | Spell Archivist | Spell descriptions + spell books |
| **Rules** | Rules Arbiter | Rules sections |
| **GM** | Game Master | Monster / DM-focused content; relaxed creative gate. A seam for a future second "world" retrieval source is stubbed. |

Sage is the default. The mode selector lives in the left navigation.

## App Shell

```text
Landing screen
  └─ "Enter the Tavern" CTA  →  Workspace
       ├─ LeftNav  (mode chips, conversation list, user menu)
       ├─ TopBar   (brand + dark-theme toggle)
       └─ ChatPane (composer, exchange feed, sources, dice rolls)
```

**Users and conversation history are currently stubbed.** The guest "Adventurer" user is hard-coded; no real authentication or server-side persistence exists yet. Conversation titles are stored in `localStorage` for the current session only.

## Architecture

```text
User
  ↓ prompt
ui/  (React 19 + Vite 8, Aetheril design system — light Parchment / dark Tavern)
  ↓ POST /chat  { prompt, mode?, conversation_id? }
service/  (FastAPI — embed → filter → retrieve → rerank → answerability gate → LLM)
  ↓ pgvector similarity search  (with optional book-slug filter per mode)
vector-db  (PostgreSQL + pgvector, 9,000+ chunks across 12 D&D 5e books)
```

### Response contract

```json
{
  "answer": "...",
  "sources": [{ "book": "...", "chapter": "...", "section": "...", "entity": "...", "page": 12, "snippet": "..." }],
  "answerable": true,
  "mode": "spell",
  "conversation_id": null
}
```

## Running E2E — one command

**Prereq:** a `.env` file in this directory containing your OpenAI key:

```env
OPENAI_API_KEY=sk-...
```

The vector DB already holds the embedded corpus. Start everything with:

```bash
./scripts/up.sh
```

This builds, starts detached, waits for the stack to be healthy, then prints the UI link.
(Equivalent to `docker compose up --build -d --wait` — use that directly if you want to
follow logs yourself with `docker compose up --build` instead.)

**URL: <http://localhost:5173>**

First build pulls images and compiles the Vite app (a few minutes). Subsequent starts are fast.

> `POSTGRES_PORT` in `.env` controls the DB's *host* port (default `5432`) — bump it if
> another Postgres/pgvector container already has that port bound.

### Other compose commands

```bash
docker compose up --build        # foreground, follow logs
docker compose logs -f           # tail all logs
docker compose down              # stop + remove containers (volumes/data preserved)
docker compose up vector-db      # DB only — if you prefer running the service locally
```

### Local dev (two terminals, faster iteration)

**Terminal 1 — service:** (the repo is an installable package — `--with .` pulls
in the runtime deps from `pyproject.toml`)

```bash
uv run --with . uvicorn service.app:app --port 8000 --reload
```

**Terminal 2 — UI dev server** (hot-reload, Vite proxy → :8000):

```bash
cd ui && bun run dev
```

URL: <http://localhost:5173>

**Storybook — Aetheril design-system workbench** (browse `ui/src/ds/` components in
isolation, with a light/dark theme toolbar and interactive controls):

```bash
cd ui && bun run storybook
```

URL: <http://localhost:6006>. Stories live next to their components
(`ui/src/ds/*.stories.tsx`) and also run as vitest browser tests via
`@storybook/addon-vitest` (part of `bun run test`).

### Tests

Python imports are explicit (`from service... import ...` / `from ingestion... import ...`)
with no `sys.path` hacks. Run pytest from the repo root with the `test` extra:

```bash
uv run --with '.[test]' python -m pytest -q     # whole suite
```

## Directory layout

| Path | Description |
| --- | --- |
| `service/` | FastAPI app — `POST /chat`, per-mode personas, answerability gate, grounded generation |
| `ingestion/` | Chunking, embedding, retrieval pipeline, eval harness |
| `ui/` | React + Vite chat interface (Aetheril design system) |
| `vector-db/` | DB init SQL and pgvector setup |
| `docker-compose.yml` | Full stack: vector-db → service → ui |
| `Dockerfile.service` | Python 3.12-slim image for the agent service |
| `ui/Dockerfile` | Multi-stage bun build + nginx serve |

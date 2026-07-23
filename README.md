# Aetheril — D&D 5e RAG Chat

Aetheril is a Retrieval-Augmented Generation chat assistant for D&D 5e. It answers questions grounded in rulebook text, with citations, and tells you plainly when it cannot find an answer.

New to the repo? Each folder has its own README written to get you contributing in that area fast: [`service/`](service/README.md) (API + RAG pipeline), [`ingestion/`](ingestion/README.md) (corpus pipeline + evals), [`ui/`](ui/README.md) (React front-end), [`vector-db/`](vector-db/README.md) (schema). The living architecture doc is [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the observability/eval layer is documented in [`docs/observability/`](docs/observability/OVERVIEW.md).

## Chat channels

The workspace hosts four chat channels (personas), each with its own accent color, retrieval scope, and system prompt — switch in the app-header band:

| Channel | Persona | Retrieval scope |
| --- | --- | --- |
| **Sage** | General D&D oracle | All sources (default) |
| **Spell** | Spell Archivist — quotes rules text verbatim, adds three usage-suggestion cards | Spell chunks in spell-bearing books |
| **Rules** | Rules Arbiter — rules-as-written only | Rules-type chunks |
| **GM** | Game Master — may invent, says so, relaxed grounding gate | Monster / DM content; a seam for a future second "world" corpus is stubbed |

The **GM channel is DM-only**: it appears only when the user's role is `dm` (a profile toggle — UI gating until real auth exists).

## App shell

```text
Landing screen
  └─ "Enter the Tavern" CTA  →  Workspace
       ├─ TopBar     (brand)
       ├─ AppHeader  (channel switcher — accented chips)
       ├─ LeftNav    (conversation list, user menu — theme toggle + profile)
       └─ ChatPane   (composer + attach, exchange feed, sources, suggestions, dice rolls)
  Profile screen — display name, avatar tone, DM/player role
```

**What persists where:** message content is stored **server-side** per conversation (`chat.messages`), and uploaded **file attachments** (`.txt`/`.md`/`.pdf`) ground that conversation's answers from then on. The conversation *list/titles* and the user identity are still client-side stubs — a hard-coded "Adventurer" guest with `localStorage`-persisted name/avatar/role. Real auth is a follow-up.

## Architecture

```text
User
  ↓ prompt
ui/  (React 19 + Vite, Aetheril design system — light Parchment / dark Tavern)
  ↓ POST /chat  { prompt, mode?, conversation_id? }
service/  (FastAPI + LangGraph pipeline:
           preflight → embed → hints → scope → search → fetch → [rerank*] → gate
           → per-persona generate → cite;  history + attachments per conversation)
  ↓ pgvector similarity search  (mode-scoped filters)
vector-db  (PostgreSQL + pgvector — dnd.chunks: 9,000+ chunks across 12 D&D 5e books;
            chat.messages / chat.attachments for history)
```

*\*rerank is opt-in:* set `RAG_RERANK=1` **and** install the `[rerank]` extra
(`pip install '.[rerank]'`, or `--build-arg INSTALL_RERANK=1` for the Docker image). The
cross-encoder lifts prose Hit@1 in eval but costs torch in the image and ~234 ms per reranked
query, so it ships off by default. Plain **vector** search is the production retrieval mode by
eval decision — hybrid (vector+FTS RRF) tied Hit@1 and slightly lost Recall@10 in the 3q3 A/B, and
the eval-only ipl filter→unfiltered fallback was net-harmful (see `config.py` for both verdicts).

### Response contract

```json
{
  "answer": "...",
  "sources": [{ "book": "...", "chapter": "...", "section": "...", "entity": "...", "page": 12, "snippet": "..." }],
  "answerable": true,
  "mode": "spell",
  "conversation_id": "...",
  "suggestions": [{ "style": "practical", "text": "..." }]
}
```

`suggestions` is spell-mode-only (exactly three: practical/roleplay/wacky; `null` elsewhere or when that garnish failed). A refusal is a **200** with `answerable: false` — never an error. The service also exposes `GET /conversations/{id}/messages` (history recall) and `POST`/`GET /conversations/{id}/attachments` (see [`service/README.md`](service/README.md)).

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

### Single process (UI + API on :8000)

```bash
cd ui && bun run build && cd ..
uv run --with . uvicorn service.app:app --port 8000
```

Open **<http://localhost:8000>** — the service mounts `ui/dist/` when it exists.

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
uv run --with '.[test]' python -m pytest -q     # whole suite (service/tests + ingestion/tests + tests/)
cd ui && bun run test                            # UI: jsdom unit tests + storybook browser tests
```

## Directory layout

| Path | Description |
| --- | --- |
| `service/` | FastAPI app — LangGraph RAG pipeline, per-channel personas, history, attachments ([README](service/README.md)) |
| `ingestion/` | PDF→chunks→embeddings pipeline, shared retrieval core, eval harness ([README](ingestion/README.md)) |
| `ui/` | React + Vite chat interface, Aetheril design system ([README](ui/README.md)) |
| `vector-db/` | DB init SQL (corpus + chat schema) and pgvector setup ([README](vector-db/README.md)) |
| `config.py` | Single home of every RAG tuning knob — env-overridable (`RAG_*`), documented defaults |
| `tests/` | Repo-level guards: packaging invariant, config knobs |
| `docs/` | Architecture, observability/eval layer, ingestion research + eval reports |
| `docker-compose.yml` | Full stack: vector-db → service → ui |
| `Dockerfile.service` | Python 3.12-slim image for the agent service |
| `ui/Dockerfile` | Multi-stage bun build + nginx serve |

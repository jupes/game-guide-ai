# Vector DB (PostgreSQL + pgvector)

This service uses the official Docker image **[pgvector/pgvector](https://hub.docker.com/r/pgvector/pgvector)** so Postgres can store and query embedding vectors. Extension source and docs: [pgvector/pgvector on GitHub](https://github.com/pgvector/pgvector).

## Quick start

From `repos/rag-chat/`:

```bash
# Optional: copy and edit for non-default credentials / port.
cp .env.example .env

docker compose up -d vector-db
docker compose ps
docker compose logs -f vector-db
```

Wait until health shows healthy (or run `docker compose exec vector-db pg_isready -U rag -d rag_chat` if you use the defaults).

## Connection

| Item | Value |
|------|--------|
| Host (from your machine) | `localhost` |
| Port | `POSTGRES_PORT` from `.env` (default `5432`) |
| Database | `POSTGRES_DB` (default `rag_chat`) |
| User / password | `POSTGRES_USER` / `POSTGRES_PASSWORD` |

Connection URI (example):

```text
postgresql://rag:YOUR_PASSWORD@localhost:5432/rag_chat
```

From a future Compose service on the same Docker network, use hostname **`vector-db`** and port **`5432`** (internal).

## Persistence

Data lives in the named volume **`pgvector_data`** (`/var/lib/postgresql/data` in the container). Survives `docker compose stop` and `docker compose up`. To wipe DB completely:

```bash
docker compose down -v
```

## Schema

Applied on **first** container init via scripts in `vector-db/init/` (lexical order):

- **`01-extensions.sql`** — enables the `vector` extension; creates the `dnd` schema.
- **`02-schema.sql`** — `dnd.chunks` table (see below) + B-tree indexes + HNSW vector index + `search_vector` tsvector + GIN FTS index.
- **`03-hybrid-search.sql`** — the `dnd.hybrid_search()` function (vector + FTS fused via RRF). Kept separate so the retrieval function can be iterated without touching the table DDL.

### `dnd.chunks`

| Column | Type | Description |
|--------|------|-------------|
| `chunk_id` | `TEXT PK` | `sha256(book_slug:page:col:idx)[:20]` — stable across re-ingests |
| `book_slug` | `TEXT` | e.g. `phb-5e`, `mm-5e`, `dmg-5e` |
| `source_file` | `TEXT` | Original PDF filename |
| `page_start` / `page_end` | `INT` | 1-indexed PDF pages (inclusive) |
| `part` / `chapter` / `section` | `TEXT` | Document hierarchy |
| `content_type` | `TEXT` | the six in the live corpus: `rule`, `monster`, `dm_guidance`, `spell`, `magic_item`, `feat` |
| `entity_name` | `TEXT` | Spell name, condition name, race name, etc. |
| `class_name` | `TEXT` | Parent class for `class_feature` chunks (e.g. `Fighter`) |
| `feature_name` | `TEXT` | Specific feature name for `class_feature` chunks |
| `text` | `TEXT` | Chunk body |
| `embedding` | `vector(1536)` | `text-embedding-3-small` via OpenAI — **1536d fixed** |
| `search_vector` | `TSVECTOR` | Weighted FTS: A = entity/class/feature, B = content_type, C = body |
| `created_at` | `TIMESTAMPTZ` | Insert time |

**Indexes:** HNSW (`vector_cosine_ops`) for ANN cosine search; GIN on `search_vector` for full-text search.

**Hybrid search:** `dnd.hybrid_search(query_embedding, query_text, k)` — fuses vector ranking and FTS ranking via Reciprocal Rank Fusion (RRF, k=60). Returns `rrf_score`, `vector_rank`, `fts_rank` per result. **Built but not the default:** evaluated in agent-forge-harness-3q3 (2026-06-15, 9,070 chunks) — hybrid ties pure vector on Hit@1 (83.3%) and is marginally worse on Recall@10, so the retrieval path (`RagRetriever`) and the eval default use **pure filtered vector**. The function/FTS column are retained (harmless, available for future re-evaluation), not adopted.

**Note:** `embedding` is fixed at 1536 dimensions for `text-embedding-3-small`. If you switch models, update `02-schema.sql` **before** first run — dimensions cannot be altered in place.

## Verify insert + similarity search

The repeatable check is **`verify_db.py`** — it inserts a sentinel row into
`dnd.chunks`, runs a cosine-kNN query, asserts the sentinel ranks #1 at distance
~0 (and that an orthogonal query does *not* return it first), then deletes the
sentinel so the corpus is left untouched. Run from `repos/rag-chat/` with the DB up:

```bash
uv run --with "psycopg[binary]" python vector-db/verify_db.py
```

Expected:

```text
  PASS  sentinel ranks #1 for its own vector (got '__verify_db_smoke__')
  PASS  distance is ~0 (got 0.000000)
  PASS  orthogonal query does not return sentinel first (got '<a real chunk>')
OK — vector insert + similarity search verified
```

It connects via `DATABASE_URL`, falling back to the local compose DSN. Exit code
`0` = all checks passed.

### Manual one-off (psql)

To poke it by hand — note the real schema is **`dnd.chunks`** (not `chunks`), and
the table's NOT-NULL columns must all be supplied:

```bash
docker compose exec vector-db psql -U rag -d rag_chat -v ON_ERROR_STOP=1 -c "
INSERT INTO dnd.chunks (chunk_id, book_slug, source_file, page_start, page_end, content_type, text, embedding)
VALUES ('smoke-test', 'verify', '/dev/null', 0, 0, 'rule', 'smoke',
        (SELECT ARRAY(SELECT 0.01::float4 FROM generate_series(1,1536))::vector))
ON CONFLICT (chunk_id) DO UPDATE SET text = EXCLUDED.text;

SELECT chunk_id, left(text,40) AS text_preview,
       embedding <=> (SELECT ARRAY(SELECT 0.01::float4 FROM generate_series(1,1536))::vector) AS cosine_distance
FROM dnd.chunks
ORDER BY embedding <=> (SELECT ARRAY(SELECT 0.01::float4 FROM generate_series(1,1536))::vector)
LIMIT 3;

DELETE FROM dnd.chunks WHERE chunk_id = 'smoke-test';
"
```

You should see `smoke-test` ranked first with cosine distance `0` for this trivial query.

## References

- Docker Hub: [pgvector/pgvector](https://hub.docker.com/r/pgvector/pgvector)
- Tutorial-style Compose setup: [PostgreSQL + pgvector with Docker](https://dev.to/yukaty/setting-up-postgresql-with-pgvector-using-docker-hcl) (DEV Community)

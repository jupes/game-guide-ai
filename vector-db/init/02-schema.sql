-- D&D RAG chunk store.
-- Embedding dimension 1536 matches text-embedding-3-small via OpenAI API.
-- Cosine distance operator: <=> (pgvector README).
-- See docs/plans/dnd-embedding-guide.md for model selection rationale.

CREATE TABLE IF NOT EXISTS dnd.chunks (
  chunk_id       TEXT PRIMARY KEY,
  book_slug      TEXT NOT NULL,
  source_file    TEXT NOT NULL,
  page_start     INT  NOT NULL,
  page_end       INT  NOT NULL,
  part           TEXT,
  chapter        TEXT,
  section        TEXT,
  content_type   TEXT NOT NULL,
  entity_name    TEXT,
  class_name     TEXT,
  feature_name   TEXT,
  text           TEXT NOT NULL,
  embedding      vector(1536) NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dnd_chunks_content_type_idx ON dnd.chunks (content_type);
CREATE INDEX IF NOT EXISTS dnd_chunks_book_slug_idx    ON dnd.chunks (book_slug);
CREATE INDEX IF NOT EXISTS dnd_chunks_entity_name_idx  ON dnd.chunks (entity_name);
CREATE INDEX IF NOT EXISTS dnd_chunks_class_name_idx   ON dnd.chunks (class_name);

CREATE INDEX IF NOT EXISTS dnd_chunks_embedding_hnsw_idx ON dnd.chunks
  USING hnsw (embedding vector_cosine_ops);

-- Full-text search column (populated on upsert via setweight).
-- Weight A: entity_name, class_name, feature_name (exact-match signals).
-- Weight B: content_type (category; underscores replaced with spaces).
-- Weight C: text body.
-- GIN index enables fast @@ operator queries.
ALTER TABLE dnd.chunks ADD COLUMN IF NOT EXISTS search_vector tsvector;

CREATE INDEX IF NOT EXISTS dnd_chunks_search_vector_gin ON dnd.chunks
  USING GIN (search_vector);

-- The hybrid_search() function (vector + FTS fused via RRF) lives in
-- 03-hybrid-search.sql, which runs next on init. Kept separate so the retrieval
-- function can be iterated without touching the table/index DDL above.

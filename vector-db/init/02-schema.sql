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

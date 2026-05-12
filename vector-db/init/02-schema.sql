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

-- ---------------------------------------------------------------------------
-- Hybrid retrieval: cosine vector search + ts_rank fused via Reciprocal Rank
-- Fusion (RRF).  rrf_k = 60 is the standard constant from the original paper.
-- Both legs take the top-60 candidates; documents absent from a leg get rank 1000
-- (a large penalty, not infinity, to keep the math well-defined).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dnd.hybrid_search(
    query_embedding vector(1536),
    query_text      text,
    k               int DEFAULT 5,
    rrf_k           int DEFAULT 60
)
RETURNS TABLE (
    chunk_id      text,
    content_type  text,
    entity_name   text,
    class_name    text,
    feature_name  text,
    chapter       text,
    section       text,
    page_start    int,
    text_preview  text,
    rrf_score     double precision,
    vector_rank   bigint,
    fts_rank      bigint
)
LANGUAGE sql STABLE AS $$
    WITH
    vec_ranked AS (
        SELECT
            c.chunk_id,
            ROW_NUMBER() OVER (ORDER BY c.embedding <=> query_embedding) AS vec_rank
        FROM dnd.chunks c
        ORDER BY c.embedding <=> query_embedding
        LIMIT 60
    ),
    fts_ranked AS (
        SELECT
            c.chunk_id,
            ROW_NUMBER() OVER (
                ORDER BY ts_rank(c.search_vector, plainto_tsquery('english', query_text)) DESC
            ) AS fts_rank
        FROM dnd.chunks c
        WHERE c.search_vector @@ plainto_tsquery('english', query_text)
        ORDER BY ts_rank(c.search_vector, plainto_tsquery('english', query_text)) DESC
        LIMIT 60
    ),
    combined AS (
        SELECT
            COALESCE(v.chunk_id, f.chunk_id) AS chunk_id,
            COALESCE(v.vec_rank, 1000)       AS vec_rank,
            COALESCE(f.fts_rank, 1000)       AS fts_rank,
            (1.0 / (rrf_k + COALESCE(v.vec_rank, 1000)))
            + (1.0 / (rrf_k + COALESCE(f.fts_rank, 1000))) AS rrf_score
        FROM vec_ranked v
        FULL OUTER JOIN fts_ranked f ON v.chunk_id = f.chunk_id
    )
    SELECT
        c.chunk_id,
        c.content_type,
        c.entity_name,
        c.class_name,
        c.feature_name,
        c.chapter,
        c.section,
        c.page_start,
        left(c.text, 120)      AS text_preview,
        combined.rrf_score,
        combined.vec_rank::bigint,
        combined.fts_rank::bigint
    FROM combined
    JOIN dnd.chunks c ON c.chunk_id = combined.chunk_id
    ORDER BY combined.rrf_score DESC
    LIMIT k
$$;

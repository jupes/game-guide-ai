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

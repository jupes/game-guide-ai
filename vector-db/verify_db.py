"""
Vector DB smoke test — proves insert + cosine similarity search work against the
live `dnd.chunks` table (the real schema, not a toy table).

What it does:
  1. Inserts one sentinel row with a known 1536-d embedding (idempotent upsert).
  2. Runs a cosine-distance kNN query for that exact embedding and asserts the
     sentinel ranks #1 with distance ~0 (it must beat all real corpus rows).
  3. Confirms an orthogonal query vector does NOT rank the sentinel first
     (the index discriminates — it isn't returning the sentinel unconditionally).
  4. Always deletes the sentinel afterwards (finally), leaving the corpus untouched.

Run from repos/game-guide-ai (DB must be up: `docker compose up -d vector-db`):
    uv run --with "psycopg[binary]" python vector-db/verify_db.py

Connection: DATABASE_URL env var, else the local compose DSN (same as retrieval.py).
Exit code 0 = all checks passed, 1 = a check failed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

# --- .env (repo root) + DSN — same convention as ingestion/retrieval.py ------
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

DEFAULT_DSN = "postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai"
DSN = os.environ.get("DATABASE_URL", DEFAULT_DSN)

SENTINEL_ID = "__verify_db_smoke__"
DIM = 1536


def _vec(value: float) -> str:
    """A pgvector literal of DIM components, all `value`."""
    return "[" + ",".join([repr(value)] * DIM) + "]"


def _check(label: str, ok: bool) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return ok


def main() -> int:
    same = _vec(0.01)        # the sentinel's own embedding
    orthogonal = _vec(-0.05)  # a different query vector

    with psycopg.connect(DSN) as conn:
        try:
            with conn.cursor() as cur:
                # 1. insert (idempotent)
                cur.execute(
                    """
                    INSERT INTO dnd.chunks
                      (chunk_id, book_slug, source_file, page_start, page_end,
                       content_type, text, embedding)
                    VALUES (%s, 'verify', '/dev/null', 0, 0, 'rule', 'smoke test', %s::vector)
                    ON CONFLICT (chunk_id) DO UPDATE
                      SET embedding = EXCLUDED.embedding, text = EXCLUDED.text
                    """,
                    (SENTINEL_ID, same),
                )
                conn.commit()

                # 2. kNN for the sentinel's own vector → it must rank #1, distance ~0
                cur.execute(
                    """
                    SELECT chunk_id, embedding <=> %s::vector AS cosine_distance
                    FROM dnd.chunks
                    ORDER BY embedding <=> %s::vector
                    LIMIT 1
                    """,
                    (same, same),
                )
                top_id, top_dist = cur.fetchone()

                # 3. orthogonal query → sentinel must NOT be the top hit
                cur.execute(
                    """
                    SELECT chunk_id
                    FROM dnd.chunks
                    ORDER BY embedding <=> %s::vector
                    LIMIT 1
                    """,
                    (orthogonal,),
                )
                (other_top_id,) = cur.fetchone()

                # row count, for context
                cur.execute("SELECT count(*) FROM dnd.chunks")
                (n_rows,) = cur.fetchone()
        finally:
            # 4. always clean up the sentinel
            with conn.cursor() as cur:
                cur.execute("DELETE FROM dnd.chunks WHERE chunk_id = %s", (SENTINEL_ID,))
            conn.commit()

    print(f"dnd.chunks rows (incl. sentinel during test): {n_rows}")
    ok = True
    ok &= _check(f"sentinel ranks #1 for its own vector (got {top_id!r})", top_id == SENTINEL_ID)
    ok &= _check(f"distance is ~0 (got {top_dist:.6f})", top_dist < 1e-6)
    ok &= _check(
        f"orthogonal query does not return sentinel first (got {other_top_id!r})",
        other_top_id != SENTINEL_ID,
    )

    print(f"\n{'OK — vector insert + similarity search verified' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

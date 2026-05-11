"""
D&D chunk embedder — reads chunks.jsonl, embeds via Ollama, upserts into pgvector.

Usage:
    uv run --with psycopg --with psycopg[binary] python ingestion/embed.py [--chunks <path>] [--dsn <dsn>]

Example:
    uv run --with "psycopg[binary]" python ingestion/embed.py

Env vars (override with flags):
    DATABASE_URL  postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat
    OLLAMA_URL    http://localhost:11434
    EMBED_MODEL   mxbai-embed-large
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_DSN         = "postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat"
DEFAULT_OLLAMA_URL  = "http://localhost:11434"
DEFAULT_MODEL       = "nomic-embed-text"
DEFAULT_CHUNKS_PATH = Path(__file__).parent / "chunks.jsonl"
BATCH_SIZE          = 32   # chunks per Ollama request
MAX_EMBED_CHARS     = 8000 # nomic-embed-text supports 8192 tokens; headroom for tokenizer overhead


# ---------------------------------------------------------------------------
# Ollama embedding
# ---------------------------------------------------------------------------

def _embed_batch(texts: list[str], ollama_url: str, model: str) -> list[list[float]]:
    """Call Ollama /api/embed and return one embedding per text."""
    payload = json.dumps({"model": model, "input": texts}).encode()
    req = urllib.request.Request(
        f"{ollama_url}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result["embeddings"]


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO dnd.chunks (
    chunk_id, book_slug, source_file, page_start, page_end,
    part, chapter, section, content_type,
    entity_name, class_name, feature_name,
    text, embedding
) VALUES (
    %(chunk_id)s, %(book_slug)s, %(source_file)s, %(page_start)s, %(page_end)s,
    %(part)s, %(chapter)s, %(section)s, %(content_type)s,
    %(entity_name)s, %(class_name)s, %(feature_name)s,
    %(text)s, %(embedding)s
)
ON CONFLICT (chunk_id) DO UPDATE SET
    text      = EXCLUDED.text,
    embedding = EXCLUDED.embedding,
    part      = EXCLUDED.part,
    chapter   = EXCLUDED.chapter,
    section   = EXCLUDED.section
"""


def _upsert_batch(conn: psycopg.Connection, rows: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, rows)
    conn.commit()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def embed_and_upsert(
    chunks_path: Path,
    dsn: str,
    ollama_url: str,
    model: str,
) -> None:
    chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    total = len(chunks)
    print(f"Chunks to embed: {total}  (model: {model})")

    with psycopg.connect(dsn) as conn:
        upserted = 0
        for batch_start in range(0, total, BATCH_SIZE):
            batch = chunks[batch_start : batch_start + BATCH_SIZE]
            texts = [c["text"][:MAX_EMBED_CHARS] for c in batch]

            t0 = time.monotonic()
            embeddings = _embed_batch(texts, ollama_url, model)
            elapsed = time.monotonic() - t0

            rows = []
            for chunk, emb in zip(batch, embeddings):
                row = dict(chunk)
                row["embedding"] = emb
                rows.append(row)

            _upsert_batch(conn, rows)
            upserted += len(batch)

            end = min(batch_start + BATCH_SIZE, total)
            print(f"  [{upserted:4d}/{total}]  batch {batch_start+1}–{end}  ({elapsed:.1f}s)")

    print(f"\nDone. {upserted} chunks upserted into dnd.chunks.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Embed D&D chunks and upsert into pgvector")
    parser.add_argument("--chunks", default=str(DEFAULT_CHUNKS_PATH),
                        help="Path to chunks.jsonl")
    parser.add_argument("--dsn",   default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
                        help="PostgreSQL DSN")
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
                        help="Ollama base URL")
    parser.add_argument("--model", default=os.environ.get("EMBED_MODEL", DEFAULT_MODEL),
                        help="Ollama embedding model name")
    args = parser.parse_args()

    chunks_path = Path(args.chunks)
    if not chunks_path.exists():
        print(f"ERROR: chunks file not found: {chunks_path}", file=sys.stderr)
        sys.exit(1)

    embed_and_upsert(chunks_path, args.dsn, args.ollama_url, args.model)


if __name__ == "__main__":
    main()

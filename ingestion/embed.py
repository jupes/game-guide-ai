"""
D&D chunk embedder — reads chunks.jsonl, embeds via OpenAI or Ollama, upserts into pgvector.

Usage:
    uv run --with "psycopg[binary]" --with openai python ingestion/embed.py

Env vars:
    DATABASE_URL      postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai
    OPENAI_API_KEY    sk-...  (required for OpenAI backend)
    EMBED_BACKEND     openai | ollama  (default: openai)
    EMBED_MODEL       default depends on backend:
                        openai  -> text-embedding-3-small
                        ollama  -> nomic-embed-text
    OLLAMA_URL        http://localhost:11434  (ollama backend only)

See docs/plans/dnd-embedding-guide.md for model selection rationale.
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

# ---------------------------------------------------------------------------
# Load .env from repo root (same pattern as eval_golden.py) so OPENAI_API_KEY /
# DATABASE_URL are available without a manual export.
# ---------------------------------------------------------------------------

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_DSN         = "postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai"
DEFAULT_BACKEND     = "openai"
DEFAULT_MODEL_OAI   = "text-embedding-3-small"
DEFAULT_MODEL_OLLAMA = "nomic-embed-text"
DEFAULT_OLLAMA_URL  = "http://localhost:11434"
DEFAULT_CHUNKS_PATH = Path(__file__).parent / "chunks.jsonl"
BATCH_SIZE          = 128  # OpenAI supports up to 2048 inputs per request


# ---------------------------------------------------------------------------
# Embedding backends
# ---------------------------------------------------------------------------

def _embed_openai(texts: list[str], model: str) -> list[list[float]]:
    """Embed a batch of texts via the OpenAI API."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key == "sk-replace-me":
        print("ERROR: OPENAI_API_KEY is not set. Add it to your .env file.", file=sys.stderr)
        print("  See .env.example for the expected format.", file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    resp = client.embeddings.create(model=model, input=texts)
    return [d.embedding for d in resp.data]


def _embed_ollama(texts: list[str], model: str, ollama_url: str) -> list[list[float]]:
    """Embed a batch of texts via Ollama /api/embed."""
    payload = json.dumps({"model": model, "input": texts}).encode()
    req = urllib.request.Request(
        f"{ollama_url}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["embeddings"]


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO dnd.chunks (
    chunk_id, book_slug, source_file, page_start, page_end,
    part, chapter, section, content_type,
    entity_name, class_name, feature_name,
    text, embedding, search_vector
) VALUES (
    %(chunk_id)s, %(book_slug)s, %(source_file)s, %(page_start)s, %(page_end)s,
    %(part)s, %(chapter)s, %(section)s, %(content_type)s,
    %(entity_name)s, %(class_name)s, %(feature_name)s,
    %(text)s, %(embedding)s,
    setweight(to_tsvector('english', coalesce(%(entity_name)s, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(%(class_name)s,  '')), 'A') ||
    setweight(to_tsvector('english', coalesce(%(feature_name)s,'')), 'A') ||
    setweight(to_tsvector('english', replace(coalesce(%(content_type)s, ''), '_', ' ')), 'B') ||
    setweight(to_tsvector('english', %(text)s), 'C')
)
ON CONFLICT (chunk_id) DO UPDATE SET
    text          = EXCLUDED.text,
    embedding     = EXCLUDED.embedding,
    search_vector = EXCLUDED.search_vector,
    part          = EXCLUDED.part,
    chapter       = EXCLUDED.chapter,
    section       = EXCLUDED.section,
    content_type  = EXCLUDED.content_type,
    entity_name   = EXCLUDED.entity_name,
    class_name    = EXCLUDED.class_name,
    feature_name  = EXCLUDED.feature_name
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
    backend: str,
    model: str,
    ollama_url: str,
    replace_book: bool = False,
) -> None:
    chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    total = len(chunks)
    print(f"Chunks to embed: {total}  (backend: {backend}, model: {model})")

    with psycopg.connect(dsn) as conn:
        if replace_book:
            # Re-extraction changes chunk_ids (idx is extraction-order), so a plain
            # upsert would ORPHAN the old rows. Delete the JSONL's book_slug(s) first
            # so the corpus exactly matches the new extraction (no stale chunks).
            slugs = sorted({c.get("book_slug") for c in chunks if c.get("book_slug")})
            with conn.cursor() as cur:
                for slug in slugs:
                    cur.execute("DELETE FROM dnd.chunks WHERE book_slug = %s", (slug,))
                    print(f"  replace-book: deleted {cur.rowcount} existing rows for book_slug={slug!r}")
            conn.commit()
        upserted = 0
        for batch_start in range(0, total, BATCH_SIZE):
            batch = chunks[batch_start : batch_start + BATCH_SIZE]
            texts = [c["text"] for c in batch]

            t0 = time.monotonic()
            if backend == "openai":
                embeddings = _embed_openai(texts, model)
            else:
                embeddings = _embed_ollama(texts, model, ollama_url)
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
    parser.add_argument("--backend", default=os.environ.get("EMBED_BACKEND", DEFAULT_BACKEND),
                        choices=["openai", "ollama"],
                        help="Embedding backend (default: openai)")
    parser.add_argument("--model", default=None,
                        help="Model name (default: text-embedding-3-small for openai, nomic-embed-text for ollama)")
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
                        help="Ollama base URL (ollama backend only)")
    parser.add_argument("--replace-book", action="store_true",
                        help="Delete existing rows for the JSONL's book_slug(s) before inserting "
                             "(prevents orphaned chunks when re-extraction changes chunk_ids)")
    args = parser.parse_args()

    if args.model is None:
        args.model = os.environ.get(
            "EMBED_MODEL",
            DEFAULT_MODEL_OAI if args.backend == "openai" else DEFAULT_MODEL_OLLAMA,
        )

    chunks_path = Path(args.chunks)
    if not chunks_path.exists():
        print(f"ERROR: chunks file not found: {chunks_path}", file=sys.stderr)
        sys.exit(1)

    embed_and_upsert(chunks_path, args.dsn, args.backend, args.model, args.ollama_url,
                     replace_book=args.replace_book)


if __name__ == "__main__":
    main()

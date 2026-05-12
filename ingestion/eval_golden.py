"""
Golden-set evaluation — measures retrieval quality against known-good queries.

Usage:
    uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py
    uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py --mode hybrid
    uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py --mode vector

Env vars (reads from .env automatically):
    DATABASE_URL      postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat
    OPENAI_API_KEY    sk-...

Reports Precision@K, Hit@1, and per-query breakdown with top-5 results.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

# ---------------------------------------------------------------------------
# Load .env from repo root (same pattern as embed.py)
# ---------------------------------------------------------------------------

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_DSN = "postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat"
EMBED_MODEL = "text-embedding-3-small"
TOP_K = 5


@dataclass
class GoldenQuery:
    question: str
    expected_content_type: str
    expected_entity: str | None = None       # matches entity_name
    expected_class: str | None = None        # matches class_name
    expected_chapter: str | None = None      # substring match on chapter


GOLDEN_SET: list[GoldenQuery] = [
    GoldenQuery(
        question="What is the range of Fireball?",
        expected_content_type="spell",
        expected_entity="Fireball",
    ),
    GoldenQuery(
        question="How many hit points does a Fighter get at level 1?",
        expected_content_type="class_feature",
        expected_class="Fighter",
    ),
    GoldenQuery(
        question="What does the Blinded condition do?",
        expected_content_type="condition",
        expected_entity="Blinded",
    ),
    GoldenQuery(
        question="How does grappling work?",
        expected_content_type="rule",
        expected_chapter="Chapter 9",
    ),
    GoldenQuery(
        question="What languages do Elves know?",
        expected_content_type="race_feature",
        expected_entity="Elf",
    ),
    GoldenQuery(
        question="What are the components of Cure Wounds?",
        expected_content_type="spell",
        expected_entity="Cure Wounds",
    ),
]


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_query(text: str) -> list[float]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key == "sk-replace-me":
        print("ERROR: OPENAI_API_KEY not set. Add it to .env.", file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    resp = client.embeddings.create(model=EMBED_MODEL, input=[text])
    return resp.data[0].embedding


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

_VECTOR_SQL = """
SELECT
    chunk_id,
    content_type,
    entity_name,
    class_name,
    feature_name,
    chapter,
    section,
    page_start,
    left(text, 120) AS text_preview,
    embedding <=> %s::vector AS cosine_distance
FROM dnd.chunks
ORDER BY embedding <=> %s::vector
LIMIT %s
"""

_HYBRID_SQL = """
SELECT
    chunk_id,
    content_type,
    entity_name,
    class_name,
    feature_name,
    chapter,
    section,
    page_start,
    text_preview,
    rrf_score AS cosine_distance
FROM dnd.hybrid_search(%s::vector, %s, %s)
"""


@dataclass
class RetrievedChunk:
    chunk_id: str
    content_type: str
    entity_name: str | None
    class_name: str | None
    feature_name: str | None
    chapter: str | None
    section: str | None
    page_start: int
    text_preview: str
    cosine_distance: float


def retrieve_top_k(
    conn: psycopg.Connection,
    query_embedding: list[float],
    query_text: str,
    k: int,
    mode: str = "vector",
) -> list[RetrievedChunk]:
    emb_str = str(query_embedding)
    with conn.cursor() as cur:
        if mode == "hybrid":
            cur.execute(_HYBRID_SQL, (emb_str, query_text, k))
        else:
            cur.execute(_VECTOR_SQL, (emb_str, emb_str, k))
        rows = cur.fetchall()
    return [
        RetrievedChunk(
            chunk_id=r[0],
            content_type=r[1],
            entity_name=r[2],
            class_name=r[3],
            feature_name=r[4],
            chapter=r[5],
            section=r[6],
            page_start=r[7],
            text_preview=r[8],
            cosine_distance=r[9],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------

def is_hit(chunk: RetrievedChunk, golden: GoldenQuery) -> bool:
    """Check if a retrieved chunk matches the golden query's expectations."""
    if chunk.content_type != golden.expected_content_type:
        return False

    if golden.expected_entity:
        # Match entity_name (case-insensitive, allow substring for partial names)
        if not chunk.entity_name:
            return False
        if golden.expected_entity.lower() not in chunk.entity_name.lower():
            return False

    if golden.expected_class:
        if not chunk.class_name:
            return False
        if golden.expected_class.lower() not in chunk.class_name.lower():
            return False

    if golden.expected_chapter:
        if not chunk.chapter:
            return False
        if golden.expected_chapter.lower() not in chunk.chapter.lower():
            return False

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval quality against golden queries")
    parser.add_argument("--mode", choices=["vector", "hybrid"], default="hybrid",
                        help="Retrieval mode: vector (cosine only) or hybrid (vector + FTS via RRF)")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)

    print("=" * 72)
    print("D&D RAG — Golden Set Evaluation")
    print(f"Model: {EMBED_MODEL}  |  Top-K: {TOP_K}  |  Mode: {args.mode}")
    print("=" * 72)

    conn = psycopg.connect(dsn)

    # Quick sanity check
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dnd.chunks")
        chunk_count = cur.fetchone()[0]
    print(f"Chunks in dnd.chunks: {chunk_count}\n")

    total_hit_at_1 = 0
    total_precision_at_k = 0.0
    results_json: list[dict] = []

    for i, golden in enumerate(GOLDEN_SET, 1):
        print(f"─── Query {i}/{len(GOLDEN_SET)} ───")
        print(f"  Q: {golden.question}")
        print(f"  Expect: content_type={golden.expected_content_type}", end="")
        if golden.expected_entity:
            print(f", entity={golden.expected_entity}", end="")
        if golden.expected_class:
            print(f", class={golden.expected_class}", end="")
        if golden.expected_chapter:
            print(f", chapter={golden.expected_chapter}", end="")
        print()

        # Embed and retrieve
        emb = embed_query(golden.question)
        chunks = retrieve_top_k(conn, emb, golden.question, TOP_K, mode=args.mode)

        # Score
        hits = [is_hit(c, golden) for c in chunks]
        hit_at_1 = hits[0] if hits else False
        precision = sum(hits) / TOP_K

        total_hit_at_1 += int(hit_at_1)
        total_precision_at_k += precision

        status = "HIT" if hit_at_1 else "MISS"
        print(f"  Result: {status}  |  Precision@{TOP_K}: {precision:.1%}")
        print()

        score_label = "rrf" if args.mode == "hybrid" else "dist"
        for j, (chunk, hit) in enumerate(zip(chunks, hits), 1):
            marker = "✓" if hit else "✗"
            ename = chunk.entity_name or chunk.class_name or chunk.feature_name or "—"
            print(f"    {marker} #{j}  {score_label}={chunk.cosine_distance:.4f}  "
                  f"type={chunk.content_type:15s}  entity={ename}")
            print(f"         ch={chunk.chapter or '—'}  p.{chunk.page_start}")
            print(f"         {chunk.text_preview}")
            print()

        score_key = "rrf_score" if args.mode == "hybrid" else "cosine_distance"
        results_json.append({
            "question": golden.question,
            "mode": args.mode,
            "expected_content_type": golden.expected_content_type,
            "expected_entity": golden.expected_entity,
            "expected_class": golden.expected_class,
            "expected_chapter": golden.expected_chapter,
            "hit_at_1": hit_at_1,
            "precision_at_k": precision,
            "top_k": [
                {
                    "rank": j + 1,
                    "chunk_id": c.chunk_id,
                    "content_type": c.content_type,
                    "entity_name": c.entity_name,
                    "class_name": c.class_name,
                    score_key: round(c.cosine_distance, 6),
                    "is_hit": h,
                }
                for j, (c, h) in enumerate(zip(chunks, hits))
            ],
        })

    conn.close()

    # Summary
    n = len(GOLDEN_SET)
    print("=" * 72)
    print("SUMMARY")
    print(f"  Hit@1:        {total_hit_at_1}/{n}  ({total_hit_at_1/n:.1%})")
    print(f"  Precision@{TOP_K}:  {total_precision_at_k/n:.1%}  (avg across queries)")
    print("=" * 72)

    # Save results JSON
    out_path = Path(__file__).parent / "eval_results.json"
    out_path.write_text(json.dumps(results_json, indent=2, ensure_ascii=False))
    print(f"\nDetailed results saved to {out_path}")


if __name__ == "__main__":
    main()

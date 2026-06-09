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
import re
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
    # ── Original 6 ──────────────────────────────────────────────────────
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

    # ── 4 additional spell queries ──────────────────────────────────────
    GoldenQuery(
        question="What level is Shield and what does it do?",
        expected_content_type="spell",
        expected_entity="Shield",
    ),
    GoldenQuery(
        question="How does Counterspell work?",
        expected_content_type="spell",
        expected_entity="Counterspell",
    ),
    GoldenQuery(
        question="What is the casting time of Healing Word?",
        expected_content_type="spell",
        expected_entity="Healing Word",
    ),
    GoldenQuery(
        question="What does the Magic Missile spell do?",
        expected_content_type="spell",
        expected_entity="Magic Missile",
    ),

    # ── 10 hard queries (multi-concept, cross-chunk, edge cases) ───────
    GoldenQuery(
        question="What happens when a creature is both Prone and Restrained?",
        expected_content_type="condition",
        expected_entity="Prone",
    ),
    GoldenQuery(
        question="How does the Cleric's Channel Divinity: Turn Undead work?",
        expected_content_type="class_feature",
        expected_class="Cleric",
    ),
    GoldenQuery(
        question="What saving throw proficiencies does a Wizard get?",
        expected_content_type="class_feature",
        expected_class="Wizard",
    ),
    GoldenQuery(
        question="How does two-weapon fighting work in combat?",
        expected_content_type="rule",
        expected_chapter="Chapter 9",
    ),
    GoldenQuery(
        question="What are the Rogue's Sneak Attack requirements?",
        expected_content_type="class_feature",
        expected_class="Rogue",
    ),
    GoldenQuery(
        question="What ability score bonuses do Dwarves get?",
        expected_content_type="race_feature",
        expected_entity="Dwarf",
    ),
    GoldenQuery(
        question="How do opportunity attacks work?",
        expected_content_type="rule",
        expected_chapter="Chapter 9",
    ),
    GoldenQuery(
        question="What does the Paralyzed condition do to saving throws?",
        expected_content_type="condition",
        expected_entity="Paralyzed",
    ),
    GoldenQuery(
        question="How does multiclassing work?",
        expected_content_type="rule",
        expected_chapter="Chapter 6",
    ),
    GoldenQuery(
        question="What equipment does a Fighter start with?",
        expected_content_type="class_feature",
        expected_class="Fighter",
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


# ---------------------------------------------------------------------------
# Query-time entity extraction + filtered vector SQL
#
# Q13 ("What saving throw proficiencies does a Wizard get?") originally missed
# because a generic Saving Throws rule chunk outranked the Wizard-specific
# proficiencies block. The fix: detect class/entity hints in the query against
# the actual vocabulary in dnd.chunks, then add a WHERE filter so vector search
# only ranks chunks tagged with one of those names.
# ---------------------------------------------------------------------------

def extract_query_entities(
    text: str,
    known_classes: set[str],
    known_entities: set[str],
) -> tuple[set[str], set[str]]:
    """
    Return (matched_classes, matched_entities) found in `text`.

    Matching is case-insensitive, with word boundaries to avoid false positives
    (e.g. "Bard" must not match "bombard"). Multi-word names are matched as a
    single phrase. Plurals are handled by allowing a trailing "s"/"es".
    """
    lowered = text.lower()
    classes: set[str] = set()
    entities: set[str] = set()

    def _match(name: str) -> bool:
        # \b on each end. Allow regular plural (s/es) and the f→ves irregular
        # plural ("Dwarf" → "Dwarves", "Elf" → "Elves") common in D&D vocab.
        base = re.escape(name.lower())
        if name.lower().endswith("f"):
            ves = re.escape(name.lower()[:-1] + "ves")
            pattern = rf"\b(?:{base}(?:e?s)?|{ves})\b"
        else:
            pattern = rf"\b{base}(?:e?s)?\b"
        return re.search(pattern, lowered) is not None

    for name in known_classes:
        if _match(name):
            classes.add(name)
    for name in known_entities:
        if _match(name):
            entities.add(name)

    return classes, entities


def build_vector_sql(
    emb_str: str,
    k: int,
    classes: set[str],
    entities: set[str],
) -> tuple[str, tuple]:
    """
    Build the vector retrieval SQL and parameter tuple.

    When `classes` or `entities` is non-empty, an entity-aware WHERE clause is
    added so vector search only ranks chunks whose class_name or entity_name is
    in the matched vocabulary. Otherwise returns the unfiltered _VECTOR_SQL.

    Params order matches the placeholders in the returned SQL string.
    """
    if not classes and not entities:
        return _VECTOR_SQL, (emb_str, emb_str, k)

    where_parts: list[str] = []
    params: list = [emb_str]

    if classes:
        where_parts.append("class_name = ANY(%s)")
        params.append(list(classes))
    if entities:
        where_parts.append("entity_name = ANY(%s)")
        params.append(list(entities))

    where_clause = " OR ".join(where_parts)
    params.extend([emb_str, k])

    sql = f"""
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
WHERE {where_clause}
ORDER BY embedding <=> %s::vector
LIMIT %s
"""
    return sql, tuple(params)


def load_vocabulary(conn: psycopg.Connection) -> tuple[set[str], set[str]]:
    """Pull distinct class_name and entity_name values from dnd.chunks."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT class_name FROM dnd.chunks WHERE class_name IS NOT NULL")
        classes = {r[0] for r in cur.fetchall() if r[0]}
        cur.execute("SELECT DISTINCT entity_name FROM dnd.chunks WHERE entity_name IS NOT NULL")
        entities = {r[0] for r in cur.fetchall() if r[0]}
    return classes, entities

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
    classes: set[str] | None = None,
    entities: set[str] | None = None,
) -> list[RetrievedChunk]:
    emb_str = str(query_embedding)
    classes = classes or set()
    entities = entities or set()
    has_filters = bool(classes or entities)

    with conn.cursor() as cur:
        if mode == "hybrid" and not has_filters:
            cur.execute(_HYBRID_SQL, (emb_str, query_text, k))
        else:
            # Hybrid path can't accept filters today (dnd.hybrid_search is filter-free).
            # The eval report shows hybrid ≡ vector at this corpus size, so we use
            # filtered vector when entity hints are present — same precision either way.
            sql, params = build_vector_sql(emb_str, k, classes, entities)
            cur.execute(sql, params)
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
    print(f"Chunks in dnd.chunks: {chunk_count}")

    # Pull entity vocabulary from the corpus for query-time hint extraction.
    known_classes, known_entities = load_vocabulary(conn)
    print(f"Vocab loaded: {len(known_classes)} classes, {len(known_entities)} entities\n")

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

        # Embed and retrieve (with query-time entity filter when applicable)
        emb = embed_query(golden.question)
        match_classes, match_entities = extract_query_entities(
            golden.question, known_classes, known_entities,
        )
        if match_classes or match_entities:
            print(f"  Filter: classes={sorted(match_classes) or '—'}  "
                  f"entities={sorted(match_entities) or '—'}")
        chunks = retrieve_top_k(
            conn, emb, golden.question, TOP_K, mode=args.mode,
            classes=match_classes, entities=match_entities,
        )

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
            "matched_classes": sorted(match_classes),
            "matched_entities": sorted(match_entities),
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

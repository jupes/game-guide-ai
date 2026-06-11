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

# The retrieval pipeline lives in retrieval.py (shared with the agent service).
# Import its public surface so this module's callers + tests keep working.
from retrieval import (  # noqa: E402
    DEFAULT_DSN,
    EMBED_MODEL,
    TOP_K,
    IPL_FALLBACK_DISTANCE,
    KOZ_ANSWERABLE_DISTANCE,
    RetrievedChunk,
    _GENERIC_ENTITY_STOPLIST,
    _stem,
    build_vector_sql,
    embed_query,
    extract_query_content_types,
    extract_query_entities,
    fetch_full_texts,
    is_answerable,
    load_vocabulary,
    needs_unfiltered_fallback,
    retrieve_top_k,
)

# Retrieve 10 to drive Recall@10 + MRR; slice to 5 for the legacy Hit@1 / P@5.
PRECISION_K = 5  # P@5 metric denominator (eval-only)


@dataclass
class GoldenQuery:
    question: str
    expected_content_type: str | None        # None → negative query (answer not in corpus)
    expected_entity: str | None = None       # matches entity_name
    expected_class: str | None = None        # matches class_name
    expected_chapter: str | None = None      # substring match on chapter
    category: str = "general"                # reporting dimension (stratified summary)
    book: str = "phb"                        # which book should answer this


# Cross-book disambiguation + negative queries are hand-curated; the bulk of the
# suite is generated from real corpus rows by gen_golden.py (golden_set.json),
# which guarantees expected tags match real data. See agent-forge-harness-wsq.

CURATED: list[GoldenQuery] = [
    # ── Cross-book disambiguation: same word, different type/book ──────────
    GoldenQuery("What does the Invisibility spell do?",
                "spell", expected_entity="Invisibility", category="cross_book"),
    GoldenQuery("Is there a magic item that makes you invisible?",
                "magic_item", expected_entity="Invisibility", category="cross_book", book="dmg"),
    GoldenQuery("How strong does a Potion of Giant Strength make you?",
                "magic_item", expected_entity="Giant Strength", category="cross_book", book="dmg"),
    GoldenQuery("What is a Beholder Zombie?",
                "monster", expected_entity="Beholder Zombie", category="cross_book", book="mm"),
    GoldenQuery("What does the Shield spell do?",
                "spell", expected_entity="Shield", category="cross_book"),
    GoldenQuery("What is a Shield Guardian?",
                "monster", expected_entity="Shield Guardian", category="cross_book", book="mm"),

    # ── VGM/MTF monsters recovered by the heading-history name fix (qg4) ────
    # These were absent from the corpus before the fix (names mis-bound to type
    # lines and quarantined) — guaranteed misses then, answerable now.
    GoldenQuery("What is a Froghemoth?",
                "monster", expected_entity="Froghemoth", category="monster", book="vgm"),
    GoldenQuery("What does a Death Kiss do?",
                "monster", expected_entity="Death Kiss", category="monster", book="vgm"),
    GoldenQuery("What is a Babau demon?",
                "monster", expected_entity="Babau", category="monster", book="vgm"),
    GoldenQuery("What is a Draegloth?",
                "monster", expected_entity="Draegloth", category="monster", book="vgm"),
    GoldenQuery("What is a Flail Snail?",
                "monster", expected_entity="Flail Snail", category="monster", book="vgm"),
    GoldenQuery("What is a Meazel?",
                "monster", expected_entity="Meazel", category="monster", book="mtf"),
    GoldenQuery("What is an Allip?",
                "monster", expected_entity="Allip", category="monster", book="mtf"),
    GoldenQuery("What is a Nupperibo?",
                "monster", expected_entity="Nupperibo", category="monster", book="mtf"),

    # ── Negative queries: NOT in this 12-book corpus. Reported by top-1
    #    distance only (no pass/fail). NOTE: Artificer (TCE) and Druid Wild
    #    Shape (PHB) ARE now in-corpus, so they are no longer negatives.
    GoldenQuery("How does spelljamming through wildspace work?",
                None, category="negative"),
    GoldenQuery("What are the rules for Strixhaven mascots?",
                None, category="negative"),
    GoldenQuery("What is THAC0 and how is it calculated?",
                None, category="negative"),
    GoldenQuery("How do I evolve my Pokemon in combat?",
                None, category="negative"),
    GoldenQuery("What are the rules for piloting a starship in space?",
                None, category="negative"),
]


def _load_generated() -> list[GoldenQuery]:
    """Load the corpus-generated queries (gen_golden.py → golden_set.json)."""
    path = Path(__file__).parent / "golden_set.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [
        GoldenQuery(
            question=r["question"],
            expected_content_type=r.get("expected_content_type"),
            expected_entity=r.get("expected_entity"),
            expected_class=r.get("expected_class"),
            expected_chapter=r.get("expected_chapter"),
            category=r.get("category", "general"),
            book=r.get("book", "phb"),
        )
        for r in rows
    ]


GOLDEN_SET: list[GoldenQuery] = _load_generated() + CURATED



# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------

def compute_metrics(hits: list[bool]) -> dict:
    """
    Derive retrieval metrics from an ordered list of hit/miss booleans.

    Returns:
        hit_at_1:      hits[0] (top result is a match)
        precision_at_5: fraction of top-5 that are hits (normalized to 5 even
                        if fewer results are available)
        mrr:           1 / rank of the FIRST hit; 0 if no hits anywhere
        recall_at_10:  any hit in the first 10 results
    """
    hit_at_1 = bool(hits and hits[0])

    top5 = hits[:PRECISION_K]
    precision_at_5 = sum(top5) / PRECISION_K  # denominator is fixed at K, not len(top5)

    mrr = 0.0
    for rank, hit in enumerate(hits[:TOP_K], start=1):
        if hit:
            mrr = 1.0 / rank
            break

    recall_at_10 = any(hits[:TOP_K])

    return {
        "hit_at_1": hit_at_1,
        "precision_at_5": precision_at_5,
        "mrr": mrr,
        "recall_at_10": recall_at_10,
    }


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
    parser.add_argument("--rerank", action="store_true",
                        help="Rerank prose-category results with a cross-encoder (bo4)")
    parser.add_argument("--rerank-topk", type=int, default=10,
                        help="How many top results to rerank (default 10; 5 halves latency)")
    parser.add_argument("--ipl-fallback", action="store_true",
                        help="Enable the experimental filter→unfiltered distance fallback "
                             "(net-harmful in the A/B; the generic-entity stoplist is the real ipl fix)")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)

    print("=" * 72)
    print("D&D RAG — Golden Set Evaluation")
    print(f"Model: {EMBED_MODEL}  |  Top-K: {TOP_K}  |  P@: {PRECISION_K}  |  Mode: {args.mode}")
    print("=" * 72)

    conn = psycopg.connect(dsn)

    # Quick sanity check
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dnd.chunks")
        chunk_count = cur.fetchone()[0]
    print(f"Chunks in dnd.chunks: {chunk_count}")

    # Pull entity vocabulary from the corpus for query-time hint extraction.
    known_classes, known_entities, entity_to_ctype, class_to_ctype = load_vocabulary(conn)
    print(f"Vocab loaded: {len(known_classes)} classes, {len(known_entities)} entities")

    # Optional cross-encoder reranker (bo4), content-type gated.
    reranker = None
    rerank_count = 0
    rerank_fixed: dict[str, int] = {}   # category → misses the rerank fixed
    rerank_broke: dict[str, int] = {}   # category → hits the rerank broke
    if args.rerank:
        from rerank import CrossEncoderReranker, should_rerank
        reranker = CrossEncoderReranker()
        print(f"Reranker: ON (gated; top-{args.rerank_topk})")
    print()

    total_hit_at_1 = 0
    total_precision_at_5 = 0.0
    total_mrr = 0.0
    total_recall_at_10 = 0
    results_json: list[dict] = []
    # category → [metrics dict, ...] for the stratified summary
    by_category: dict[str, list[dict]] = {}
    negative_results: list[tuple[str, float, str]] = []   # (question, top1 dist, top1 entity)
    positive_top1_distances: list[float] = []
    koz_neg_refused = 0          # negatives correctly refused (not answerable)
    koz_pos_wrongly_refused = 0  # positives wrongly refused

    positives = [g for g in GOLDEN_SET if g.expected_content_type is not None]
    n_pos = len(positives)

    for i, golden in enumerate(GOLDEN_SET, 1):
        is_negative = golden.expected_content_type is None
        print(f"─── Query {i}/{len(GOLDEN_SET)} [{golden.category}] ───")
        print(f"  Q: {golden.question}")
        if is_negative:
            print("  Expect: NOT answerable from corpus (negative query)")
        else:
            print(f"  Expect: content_type={golden.expected_content_type}", end="")
            if golden.expected_entity:
                print(f", entity={golden.expected_entity}", end="")
            if golden.expected_class:
                print(f", class={golden.expected_class}", end="")
            if golden.expected_chapter:
                print(f", chapter={golden.expected_chapter}", end="")
            print()

        # Embed and retrieve (with query-time entity + content_type filters)
        emb = embed_query(golden.question)
        match_classes, match_entities = extract_query_entities(
            golden.question, known_classes, known_entities,
        )
        match_ctypes = extract_query_content_types(
            golden.question, entity_to_ctype, class_to_ctype,
        )
        if match_classes or match_entities or match_ctypes:
            print(f"  Filter: classes={sorted(match_classes) or '—'}  "
                  f"entities={sorted(match_entities) or '—'}  "
                  f"ctypes={sorted(match_ctypes) or '—'}")
        chunks = retrieve_top_k(
            conn, emb, golden.question, TOP_K, mode=args.mode,
            classes=match_classes, entities=match_entities,
            content_types=match_ctypes, fallback=args.ipl_fallback,
        )

        score_key = "rrf_score" if args.mode == "hybrid" else "cosine_distance"
        score_label = "rrf" if args.mode == "hybrid" else "dist"

        if is_negative:
            # No pass/fail — record what the system surfaces and how confident
            # it looks. High distance = good (nothing close in the corpus).
            top1 = chunks[0] if chunks else None
            d = top1.cosine_distance if top1 else float("nan")
            ename = (top1.entity_name or top1.class_name or "—") if top1 else "—"
            if not is_answerable(top1.cosine_distance if top1 else None):
                koz_neg_refused += 1   # correctly refused an out-of-corpus query
            negative_results.append((golden.question, d, ename))
            print(f"  Top-1: {score_label}={d:.4f}  entity={ename}  "
                  f"type={top1.content_type if top1 else '—'}")
            print()
            results_json.append({
                "question": golden.question,
                "mode": args.mode,
                "category": golden.category,
                "book": golden.book,
                "negative": True,
                "top1_distance": round(d, 6) if top1 else None,
                "top1_entity": ename,
            })
            continue

        # Optional gated rerank (bo4): only for prose-like queries. Reranks on
        # FULL chunk text (RetrievedChunk carries only a 120-char preview, so we
        # fetch text by id — one batched query) to match the research spike.
        if reranker is not None and chunks and should_rerank(match_ctypes):
            k = args.rerank_topk
            head = chunks[:k]
            baseline_hit = is_hit(head[0], golden)
            ids = [c.chunk_id for c in head]
            with conn.cursor() as cur:
                cur.execute("SELECT chunk_id, text FROM dnd.chunks WHERE chunk_id = ANY(%s)", (ids,))
                tmap = dict(cur.fetchall())
            texts = [tmap.get(c.chunk_id, c.text_preview) for c in head]
            order = reranker.rerank(golden.question, texts)
            chunks = [head[i] for i in order] + chunks[k:]
            rerank_count += 1
            reranked_hit = is_hit(chunks[0], golden)
            if not baseline_hit and reranked_hit:
                rerank_fixed[golden.category] = rerank_fixed.get(golden.category, 0) + 1
            elif baseline_hit and not reranked_hit:
                rerank_broke[golden.category] = rerank_broke.get(golden.category, 0) + 1

        # Score
        hits = [is_hit(c, golden) for c in chunks]
        metrics = compute_metrics(hits)
        if chunks:
            positive_top1_distances.append(chunks[0].cosine_distance)
            if not is_answerable(chunks[0].cosine_distance):
                koz_pos_wrongly_refused += 1

        total_hit_at_1 += int(metrics["hit_at_1"])
        total_precision_at_5 += metrics["precision_at_5"]
        total_mrr += metrics["mrr"]
        total_recall_at_10 += int(metrics["recall_at_10"])
        by_category.setdefault(golden.category, []).append(metrics)

        status = "HIT" if metrics["hit_at_1"] else "MISS"
        print(f"  Result: {status}  |  P@{PRECISION_K}: {metrics['precision_at_5']:.1%}  "
              f"|  MRR: {metrics['mrr']:.3f}  |  Recall@{TOP_K}: "
              f"{'Y' if metrics['recall_at_10'] else 'N'}")
        print()

        # Per-result detail only for misses (the suite is too big to dump all)
        if not metrics["hit_at_1"]:
            for j, (chunk, hit) in enumerate(zip(chunks[:5], hits[:5]), 1):
                marker = "✓" if hit else "✗"
                ename = chunk.entity_name or chunk.class_name or chunk.feature_name or "—"
                print(f"    {marker} #{j}  {score_label}={chunk.cosine_distance:.4f}  "
                      f"type={chunk.content_type:15s}  entity={ename}")
                print(f"         {chunk.text_preview}")
            print()

        results_json.append({
            "question": golden.question,
            "mode": args.mode,
            "category": golden.category,
            "book": golden.book,
            "expected_content_type": golden.expected_content_type,
            "expected_entity": golden.expected_entity,
            "expected_class": golden.expected_class,
            "expected_chapter": golden.expected_chapter,
            "matched_classes": sorted(match_classes),
            "matched_entities": sorted(match_entities),
            "matched_content_types": sorted(match_ctypes),
            "hit_at_1": metrics["hit_at_1"],
            "precision_at_5": metrics["precision_at_5"],
            "mrr": round(metrics["mrr"], 6),
            "recall_at_10": metrics["recall_at_10"],
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
    print("=" * 72)
    print("SUMMARY (positive queries)")
    print(f"  Hit@1:        {total_hit_at_1}/{n_pos}  ({total_hit_at_1/n_pos:.1%})")
    print(f"  Precision@{PRECISION_K}:   {total_precision_at_5/n_pos:.1%}  (avg across queries)")
    print(f"  MRR:          {total_mrr/n_pos:.3f}  (avg across queries; 1.0 = perfect rank-1)")
    print(f"  Recall@{TOP_K}:    {total_recall_at_10}/{n_pos}  ({total_recall_at_10/n_pos:.1%})")
    if reranker is not None:
        tot_fixed = sum(rerank_fixed.values())
        tot_broke = sum(rerank_broke.values())
        print("-" * 72)
        print(f"RERANK (gated): reranked {rerank_count}/{n_pos} queries  |  "
              f"fixed {tot_fixed}  broke {tot_broke}  net {tot_fixed - tot_broke:+d}")
        cats = sorted(set(rerank_fixed) | set(rerank_broke))
        for cat in cats:
            f, b = rerank_fixed.get(cat, 0), rerank_broke.get(cat, 0)
            print(f"    {cat:14s} fixed {f}  broke {b}  ({f - b:+d})")
    print("-" * 72)
    print("BY CATEGORY")
    print(f"  {'category':16s} {'n':>3s}  {'Hit@1':>7s}  {'P@5':>6s}  {'MRR':>6s}  {'R@10':>6s}")
    for cat in sorted(by_category):
        ms = by_category[cat]
        cn = len(ms)
        h1 = sum(m['hit_at_1'] for m in ms)
        p5 = sum(m['precision_at_5'] for m in ms) / cn
        mrr = sum(m['mrr'] for m in ms) / cn
        r10 = sum(m['recall_at_10'] for m in ms)
        print(f"  {cat:16s} {cn:3d}  {h1:3d}/{cn:<3d}  {p5:6.1%}  {mrr:6.3f}  {r10:3d}/{cn:<3d}")
    if negative_results:
        print("-" * 72)
        print("NEGATIVE QUERIES (answer not in corpus — top-1 distance, higher = better)")
        if positive_top1_distances:
            avg_pos = sum(positive_top1_distances) / len(positive_top1_distances)
            print(f"  reference: avg top-1 distance on positives = {avg_pos:.4f}")
        for q, d, ename in negative_results:
            print(f"  {d:.4f}  {ename:30.30s}  {q[:50]}")
    # koz: answerability gate quality at KOZ_ANSWERABLE_DISTANCE
    print("-" * 72)
    n_neg = len(negative_results)
    print(f"ANSWERABILITY GATE (koz, threshold {KOZ_ANSWERABLE_DISTANCE} cosine):")
    print(f"  negatives correctly refused: {koz_neg_refused}/{n_neg}")
    print(f"  positives wrongly refused:   {koz_pos_wrongly_refused}/{n_pos}")
    print("=" * 72)

    # Save results JSON
    out_path = Path(__file__).parent / "eval_results.json"
    out_path.write_text(json.dumps(results_json, indent=2, ensure_ascii=False))
    print(f"\nDetailed results saved to {out_path}")


if __name__ == "__main__":
    main()

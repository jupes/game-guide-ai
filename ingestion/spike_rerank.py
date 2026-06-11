"""
RESEARCH SPIKE (bo4) — does a CPU cross-encoder reranker actually lift Hit@1/MRR?

Reuses the stored eval_results.json (each positive query's top-10 chunk_ids +
is_hit labels), fetches chunk text from the DB, reranks the top-10 with a
cross-encoder, and recomputes Hit@1/MRR on the reranked order. No re-embedding,
no OpenAI calls — pure measurement of the reranker's effect on already-retrieved
candidates.

Answers: (1) does reranking move the right chunk to rank 1 on the 27
top-10-but-not-rank-1 misses? (2) does it regress any current hits? (3) latency.

Usage:
    uv run --with "psycopg[binary]" --with sentence-transformers \
        python ingestion/spike_rerank.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psycopg

_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    for ln in _ENV.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, v = ln.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

DSN = os.environ.get("DATABASE_URL", "postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat")
MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # ~80MB, CPU-friendly


def mrr(hits: list[bool]) -> float:
    for i, h in enumerate(hits, 1):
        if h:
            return 1.0 / i
    return 0.0


def main() -> None:
    results = json.loads(Path(__file__).with_name("eval_results.json").read_text(
        encoding="utf-8", errors="replace"))
    positives = [r for r in results if not r.get("negative")]

    # Collect all chunk_ids we need text for
    ids = {t["chunk_id"] for r in positives for t in r["top_k"]}
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT chunk_id, text FROM dnd.chunks WHERE chunk_id = ANY(%s)", (list(ids),))
        text_by_id = {cid: txt for cid, txt in cur.fetchall()}
    print(f"Loaded text for {len(text_by_id)}/{len(ids)} chunks")

    from sentence_transformers import CrossEncoder
    print(f"Loading cross-encoder {MODEL} (CPU)...")
    t0 = time.monotonic()
    model = CrossEncoder(MODEL, max_length=512)
    print(f"  model loaded in {time.monotonic()-t0:.1f}s")

    base_hit1 = base_mrr = 0.0
    rr_hit1 = rr_mrr = 0.0
    n = 0
    fixed = 0          # misses → hits
    broke = 0          # hits → misses
    latencies: list[float] = []
    by_cat: dict[str, list[int]] = {}   # cat → [fixed, broke]

    for r in positives:
        topk = r["top_k"]
        if not topk:
            continue
        n += 1
        cat = r.get("category", "?")
        base_hits = [t["is_hit"] for t in topk]
        base_hit1 += int(base_hits[0])
        base_mrr += mrr(base_hits)

        pairs = [(r["question"], text_by_id.get(t["chunk_id"], "")) for t in topk]
        t1 = time.monotonic()
        scores = model.predict(pairs)
        latencies.append((time.monotonic() - t1) * 1000)

        order = sorted(range(len(topk)), key=lambda i: -scores[i])
        rr_hits = [base_hits[i] for i in order]
        rr_hit1 += int(rr_hits[0])
        rr_mrr += mrr(rr_hits)

        by_cat.setdefault(cat, [0, 0])
        if not base_hits[0] and rr_hits[0]:
            fixed += 1
            by_cat[cat][0] += 1
        if base_hits[0] and not rr_hits[0]:
            broke += 1
            by_cat[cat][1] += 1

    lat = sorted(latencies)
    p50 = lat[len(lat) // 2]
    p95 = lat[int(len(lat) * 0.95)]

    print("\n" + "=" * 60)
    print(f"SPIKE RESULTS over {n} positive queries (rerank top-10)")
    print(f"  Hit@1:  baseline {base_hit1/n:.1%}  →  reranked {rr_hit1/n:.1%}")
    print(f"  MRR:    baseline {base_mrr/n:.3f}  →  reranked {rr_mrr/n:.3f}")
    print(f"  fixed (miss→hit): {fixed}   broke (hit→miss): {broke}   net: {fixed-broke:+d}")
    print(f"  latency/query (10 pairs, CPU): p50={p50:.0f}ms  p95={p95:.0f}ms")
    print("  per-category  fixed/broke (net):")
    for cat in sorted(by_cat):
        f, b = by_cat[cat]
        if f or b:
            print(f"    {cat:14s} {f}/{b}  ({f-b:+d})")
    print("=" * 60)


if __name__ == "__main__":
    main()

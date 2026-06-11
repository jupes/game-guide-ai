# D&D 5e RAG — Cross-Encoder Reranker Evaluation (bo4)

> **Date**: 2026-06-08
> **Corpus**: 8,851 chunks / 12 books · **Suite**: 171 queries
> **Bead**: agent-forge-harness-bo4 · **Research**: plans/research/dnd-cross-encoder-reranker.md

---

## Executive Summary

A content-type-gated cross-encoder reranker lifts **Hit@1 74.7% → 79.5%** and **MRR 0.808 → 0.839**
on the 12-book corpus, with **zero regressions**. It reranks only the 77 prose-category queries
(rule/feat/dm_guidance/unknown) where a cross-encoder helps, and skips structured queries
(monster/spell/magic_item) where the bi-encoder + metadata filter already nail rank-1 and reranking
only adds noise. This closes the Recall@10 ≫ Hit@1 gap that reopened bo4, the safe way.

---

## The decision path

bo4 was superseded twice ("metadata filtering already solves ranking at small corpus") and reopened
when the 8,851-chunk corpus produced a real ranking gap (Recall@10 91% vs Hit@1 74.7%). A research
spike (`spike_rerank.py`) then reranked the stored top-10 offline to answer "does it actually help,
which model, and where" before any integration.

### Model choice (spike)

| model | Hit@1 (blanket) | net queries | latency p50 (CPU, 10 pairs) |
|-------|-----------------|-------------|------------------------------|
| `ms-marco-MiniLM-L-6-v2` | 80.7% | +10 | **234ms** |
| `BAAI/bge-reranker-base` | 75.3% | +1 (noise) | 1578ms |

MiniLM-L-6 chosen; bge rejected (6.7× slower, no net benefit).

### Why gated, not blanket (spike per-category)

Blanket reranking nets +10 but the gain is entirely in prose; it is net-negative on structured
content:

| category | blanket net | nature |
|----------|-------------|--------|
| rule | +8 | prose — bi-encoder cosine is fuzzy over long passages |
| feat | +4 | prose |
| dm_guidance | +1 | prose |
| spell_lookup | 0 | already MRR 0.907 |
| cross_book | −1 | mixed |
| monster | **−2** | terse numeric stat blocks confuse a web-prose CE |

So reranking is gated by the query's inferred content_type (`should_rerank`, reusing amp's
`extract_query_content_types`): skip `{monster, spell, magic_item, condition, race_feature}`, rerank
the rest.

---

## Results (live, gated, full suite)

| metric | baseline (vector) | blanket (spike) | **gated (shipped)** |
|--------|-------------------|-----------------|---------------------|
| Hit@1 | 74.7% | 80.7% | **79.5%** |
| MRR | 0.808 | 0.844 | **0.839** |
| Recall@10 | 91.0% | — | 91.0% |
| reranked queries | — | 166/166 | **77/166** |
| fixed / broke | — | 16 / **6** | **8 / 0** |

Realized rerank effect by category (gated): rule **+7**, feat **+1**, broke **0**. Per-category Hit@1
after gating: rule 70%→**82%** (49/60), feat 24→25/30; monster untouched at 23/33 (correctly
skipped).

### Honest note: realized +8, not the projected +13

The spike's per-category table projected +13 (rule+8, feat+4, dm_guidance+1). The realized gated gain
is +8 (rule+7, feat+1). The difference is exactly the plan-review's Medium finding: the runtime gate
keys on the query's **inferred** content_type (`match_ctypes`), which reranks a different — and
generally smaller — set than the spike's bucketing by **golden category** label. Some feat/dm_guidance
queries either infer a skipped type or weren't in the reranked set. The projection was an upper
bound; the realized number is what the implementable gate delivers.

### Why gated is the right call despite blanket's higher raw Hit@1

Blanket scores 1.2 points higher (80.7 vs 79.5) but breaks 6 previously-correct queries; gated breaks
**zero**. A retrieval change that is *never worse* on any query is preferable to one with a higher
average that silently regresses some — especially since the broken queries are structured lookups
(monster stats) where users expect exactness. Gated also reranks <half the queries, so the 234ms CPU
cost is paid only where it helps.

---

## Cost & latency

- `sentence-transformers` + torch, lazy-loaded — `import rerank` pulls no torch until first rescore.
- 234ms p50 / 344ms p95 per reranked query (CPU, 10 pairs). `--rerank-topk 5` halves it for
  latency-sensitive use. Acceptable for the eval harness; for a live chat service (`88v`), use
  top-5 + the gate, or GPU.

## How to reproduce

```bash
cd repos/rag-chat && docker compose up -d
# baseline
PYTHONIOENCODING=utf-8 uv run --with "psycopg[binary]" --with openai \
    python ingestion/eval_golden.py --mode vector
# gated rerank
PYTHONIOENCODING=utf-8 uv run --with "psycopg[binary]" --with openai --with sentence-transformers \
    python ingestion/eval_golden.py --mode vector --rerank
# unit tests (no torch/DB): 10 rerank
uv run python ingestion/test_rerank.py
```

## Follow-ups

- **Service integration (`88v`)** — wire the gated reranker into POST /chat with top-5 + the gate.
- **OCR cleanup (`qg4`)** — the monster category (skipped by the reranker) is held back by OCR-mangled
  names in VGM/MTF; cleaning those is the lever for `monster`, not the reranker.
- **Spell/cross_book** — neither helped by reranking; better entity disambiguation (not a CE) is the
  path there.

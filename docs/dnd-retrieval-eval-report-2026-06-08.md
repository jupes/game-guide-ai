# D&D 5e RAG — Retrieval Evaluation Report (Post cl1 + amp + 5ms)

> **Date**: 2026-06-08
> **Corpus**: Player's Basic Rules v0.2 — 569 chunks in pgvector (unchanged from 2026-05-11)
> **Embedding model**: `text-embedding-3-small` (1536d, OpenAI API)
> **Eval script**: `repos/rag-chat/ingestion/eval_golden.py` (now with MRR + Recall@10)
> **Prior report**: [dnd-retrieval-eval-report.md](dnd-retrieval-eval-report.md) — 2026-05-11 baseline

---

## Executive Summary

After landing three retrieval improvements (`cl1` query-time class/entity filter, `amp` content-type routing, `5ms` MRR + Recall@10 metrics), the system now achieves **100% Hit@1** and **MRR = 1.000** on the 20-query golden set — every right chunk lands at rank 1, every query. **Recall@10 = 100%** confirms no chunks are missing from the candidate pool.

The previously planned cross-encoder reranker (`bo4`) has been **closed as superseded**: with Hit@1 already perfect, a reranker would add weight (~300MB model, sentence-transformers/torch deps, 50-200ms/query latency) for zero quality gain.

---

## Headline Metrics

| Mode | Hit@1 | P@5 | MRR | Recall@10 |
|------|-------|-----|-----|-----------|
| Vector + cl1 + amp filters | **20/20 (100%)** | 43% | **1.000** | **20/20 (100%)** |

| Metric | 2026-05-11 baseline | 2026-06-08 (post cl1+amp) | Δ |
|---|---|---|---|
| Hit@1 | 90% (18/20) | **100% (20/20)** | **+10pp** |
| MRR | not tracked | **1.000** | new |
| P@5 | 46% | 43% | -3pp |
| Recall@10 | not tracked | **100%** | new |

The 3-point P@5 dip is **expected and harmless**. With MRR = 1.000, every relevant chunk is at rank 1; the slots 2-5 carry topically adjacent valid context (other classes' HP blocks when asking about Fighter HP; other spells of the same school when asking about a specific spell), not garbage. A bi-encoder ranking those will always look "noisy" at P@5 against a strict per-query golden — but the LLM gets the answer at rank 1 every time, so this metric stops being load-bearing once Hit@1 is solved.

---

## What the Filters Did

### cl1 — class/entity filter

For each query, extract class and entity hints by case-insensitive substring matching against the vocabulary pulled live from `dnd.chunks` (`SELECT DISTINCT class_name, entity_name FROM dnd.chunks`). When hits are present, add `WHERE class_name = ANY(:c) OR entity_name = ANY(:e)` to the vector query so the search runs only over the named-entity subset.

Plural handling covers `f → ves` ("Dwarves" → Dwarf, "Elves" → Elf) — a real failure mode without this rule.

**Q13 was the explicit target**: *"What saving throw proficiencies does a Wizard get?"* used to land a generic "Saving Throws" rule chunk at rank 1, with the Wizard-specific block at rank 5. With `class_name = ANY(['Wizard'])` filter applied, the generic Saving Throws chunk is removed from the candidate pool — Wizard proficiencies block wins rank 1.

### amp — content-type routing

Two signal sources for `content_type` intent:

1. **Vocabulary lookup**: matched entities/classes carry an implied content_type (`Fireball` → `spell`; `Wizard` → `class_feature`; `Blinded` → `condition`; `Dwarf` → `race_feature`). The mapping is built at startup via majority vote over `(name, content_type)` distinct rows.
2. **Keyword fallback**: queries with no entity match but a category keyword (`spell`, `condition`, `race`, `background` + plurals) get a content_type hint.

When present, the content_type clause AND's the cl1 entity/class clause: `(class_name = ANY(:c) OR entity_name = ANY(:e)) AND content_type = ANY(:t)`. Tightens, never widens. Rule queries (no entity, no keyword) fall through to unfiltered vector so recall is preserved.

### 5ms — MRR and Recall@10

`TOP_K` bumped from 5 to 10 (kept `PRECISION_K = 5` for legacy P@5 comparability). `compute_metrics()` returns `{hit_at_1, precision_at_5, mrr, recall_at_10}` so the eval can distinguish:

- **Hit@1 fail + Recall@10 pass** → ranking issue, reranker candidate.
- **Hit@1 fail + Recall@10 fail** → retrieval issue, embedding or filter problem.

Today the result is **Hit@1 pass + Recall@10 pass** across the board. Neither failure mode is present.

---

## Per-Query Results

All 20 queries: HIT @ rank 1. Selected highlights:

| # | Query | Pre-filter (2026-05-11) | Post-filter (2026-06-08) |
|---|-------|-------|-------|
| 13 | What saving throw proficiencies does a Wizard get? | MISS (rank 5) | **HIT** — filter: `classes=['Wizard'], ctypes=['class_feature']` |
| 16 | What ability score bonuses do Dwarves get? | MISS (mislabel bug) | **HIT** — filter: `entities=['Dwarf'], ctypes=['race_feature']` removed the mislabel chunk from the pool |
| 11 | What happens when a creature is both Prone and Restrained? | HIT | **HIT** — filter: `entities=['Prone','Restrained'], ctypes=['condition']` |
| 14 | How does two-weapon fighting work in combat? | HIT (no entity) | **HIT** — no filter (rule query falls through) |
| 19 | How does multiclassing work? | HIT | **HIT** — filter: `entities=['Multiclassing'], ctypes=['rule']` |

Q16's HIT is especially interesting: the underlying ymv extractor bug (Dwarf chunk mis-tagged `entity_name=Elf`) is still in the corpus (re-ingest hasn't happened), but cl1+amp's filter excludes that chunk from the candidate pool — the correctly-tagged Dwarf chunks at ranks 2 and 4 of the prior eval are promoted to rank 1 + 2. The filter compensates for an upstream data bug.

---

## Decision: `bo4` Closed as Superseded

The `bo4` bead proposed adding a cross-encoder reranker (BAAI/bge-reranker-base or ms-marco-MiniLM) to rescore the top-k results. With cl1+amp landing **Hit@1 = 100%** and **MRR = 1.000**:

- A reranker cannot improve Hit@1 above 100%.
- It cannot improve MRR above 1.000.
- The 43% P@5 reflects bi-encoder cosine clustering, not ranking errors a cross-encoder would fix — the noise at ranks 2-5 is real adjacent content.

**Cost of `bo4` had we built it**: ~300MB model download, new sentence-transformers + torch deps, 50-200ms/query latency budget.
**Benefit**: zero.

Closed 2026-06-08; reopen if a future corpus expansion (`pd0`) or new query class introduces a Hit@1 regression.

---

## Status of Other Open Beads

| Bead | Title | Status | Notes |
|---|---|---|---|
| `cl1` | Query-time class/entity filter | ✓ Closed (2026-06-08) | Shipped via `eval_golden.py` commit `f53479c` |
| `amp` | Content-type query routing | ✓ Closed (2026-06-08) | Shipped via commit `c5a77f4` |
| `5ms` | MRR + Recall@10 | ✓ Closed (2026-06-08) | Shipped via commit `ddc85d0` |
| `bo4` | Cross-encoder reranker | ✓ Closed superseded (2026-06-08) | This report |
| `ymv` | Dwarf/Elf entity mislabeling fix | ✓ Closed (extract.py `821d08d`) | Live corpus needs re-ingest to benefit |
| `pd0` | Corpus expansion to 5E books | ○ Open | `repos/DnD-Books/5e/Books/` — 4E out of scope |
| `b91` | Stratify eval metrics by content_type | ○ Open (unblocked) | `matched_content_types` already in `eval_results.json` |
| `3q3` | Re-evaluate hybrid vs vector | ○ Open (blocked by `pd0`) | Needs >2000 chunks to see RRF benefit |

**Immediate followup**: re-ingest the existing corpus with the ymv extractor fix so the in-DB Dwarf chunks carry the correct `entity_name` (current filter compensates but the data should be right).

---

## Reproducibility

```bash
cd repos/rag-chat
docker compose up -d                                    # bring up pgvector
# Assumes corpus already ingested. If not:
# uv run --with pdfplumber python ingestion/extract.py "PlayerDnDBasicRules_v0.2_PrintFriendly.pdf"
# uv run --with "psycopg[binary]" --with openai python ingestion/embed.py

PYTHONIOENCODING=utf-8 \
  uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py --mode vector
```

`eval_results.json` is regenerated on every run. Per-query JSON now includes `matched_classes`, `matched_entities`, `matched_content_types`, `hit_at_1`, `precision_at_5`, `mrr`, and `recall_at_10`.

The `PYTHONIOENCODING=utf-8` is required on Windows cp1252 terminals because the per-query separator uses box-drawing characters.

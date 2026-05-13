# D&D 5e RAG -- Retrieval Evaluation Report

> **Date**: 2026-05-11
> **Corpus**: Player's Basic Rules v0.2 -- 569 chunks in pgvector
> **Embedding model**: `text-embedding-3-small` (1536d, OpenAI API)
> **Eval script**: `repos/rag-chat/ingestion/eval_golden.py`

---

## Executive Summary

We built and evaluated a retrieval pipeline for D&D 5e rules lookup using pgvector with both pure vector search (cosine similarity) and hybrid search (vector + full-text via Reciprocal Rank Fusion). Against a 20-query golden set covering spells, class features, conditions, rules, and race features, the system achieves **90% Hit@1** and **46% Precision@5** in both modes. Two remaining misses trace to a metadata tagging bug and a retrieval ranking ambiguity, not fundamental architecture problems.

---

## Test Methodology

Each query in the golden set specifies an expected `content_type` and at least one of `entity_name`, `class_name`, or `chapter`. A retrieved chunk is a "hit" if it matches all expected fields (case-insensitive substring match). We measure two metrics across the top 5 results:

**Hit@1** -- whether the rank-1 result matches the expected criteria. This is the primary metric for RAG quality because the top result drives the LLM's answer.

**Precision@5** -- the fraction of top-5 results that match. This measures how much noise the LLM would see in its context window. Lower P@5 means more irrelevant chunks diluting the answer, but is less critical than Hit@1 since the LLM can filter.

---

## Results

### Headline Metrics

| Mode | Hit@1 | Precision@5 |
|------|-------|-------------|
| Hybrid (vector + FTS via RRF) | 18/20 (90%) | 46% |
| Vector (cosine only) | 18/20 (90%) | 46% |

Both modes produced identical results across all 20 queries. At 569 chunks, the corpus is small enough that cosine similarity alone places the right chunk in the top position for most queries. The tsvector/ts_rank signal does not provide enough additional discrimination to change rankings.

### Per-Query Breakdown

**Original 10 queries (6 baseline + 4 spell):**

| # | Query | Hit@1 | P@5 |
|---|-------|-------|-----|
| 1 | What is the range of Fireball? | HIT | -- |
| 2 | How many hit points does a Fighter get at level 1? | HIT | -- |
| 3 | What does the Blinded condition do? | HIT | -- |
| 4 | How does grappling work? | HIT | -- |
| 5 | What languages do Elves know? | HIT | -- |
| 6 | What are the components of Cure Wounds? | HIT | -- |
| 7 | What level is Shield and what does it do? | HIT | -- |
| 8 | How does Counterspell work? | HIT | -- |
| 9 | What is the casting time of Healing Word? | HIT | -- |
| 10 | What does the Magic Missile spell do? | HIT | -- |

**10 hard queries (multi-concept, cross-chunk, edge cases):**

| # | Query | Hit@1 | P@5 |
|---|-------|-------|-----|
| 11 | What happens when a creature is both Prone and Restrained? | HIT | 20% |
| 12 | How does the Cleric's Channel Divinity: Turn Undead work? | HIT | 100% |
| 13 | What saving throw proficiencies does a Wizard get? | MISS | 60% |
| 14 | How does two-weapon fighting work in combat? | HIT | 20% |
| 15 | What are the Rogue's Sneak Attack requirements? | HIT | 100% |
| 16 | What ability score bonuses do Dwarves get? | MISS | 40% |
| 17 | How do opportunity attacks work? | HIT | 40% |
| 18 | What does the Paralyzed condition do to saving throws? | HIT | 40% |
| 19 | How does multiclassing work? | HIT | 60% |
| 20 | What equipment does a Fighter start with? | HIT | 60% |

### Analysis of Misses

**Q13 -- Wizard saving throw proficiencies (retrieval ranking problem)**

The query "What saving throw proficiencies does a Wizard get?" returns a generic "Saving Throws" rule chunk at rank 1 instead of the Wizard-specific proficiencies block. The correct chunk appears at rank 5 (P@5 = 60%). The phrase "saving throw proficiencies" is semantically closer to the general Saving Throws rules section than to the compact proficiency listing inside the Wizard class block. This is not a data quality issue -- the right content exists and surfaces in the top 5 -- but a ranking ambiguity where the general rule page wins over the class-specific answer.

Potential fixes: metadata filtering at query time (`content_type=class_feature AND class_name=Wizard`), query rewriting to extract the class name before retrieval, or a cross-encoder reranker to rescore the top-k candidates.

**Q16 -- Dwarf ability score bonuses (metadata tagging bug)**

The rank-1 result for "What ability score bonuses do Dwarves get?" is a chunk from the Dwarf section of the PDF that was incorrectly tagged with `entity_name=Elf`. The actual Dwarf ability score content appears at ranks 2 and 4 (both hit). This is a two-pass extraction bug -- the entity ownership map assigned incorrect entity context to a chunk spanning a page boundary between the Dwarf and Elf sections.

Fix: debug the `_build_entity_ownership_map()` function in `extract.py` to correctly handle page-boundary transitions between race sections.

---

## Architecture Learnings

### Hybrid Search Shows No Benefit at This Scale

The hybrid search function (`dnd.hybrid_search`) combines cosine vector similarity with PostgreSQL `ts_rank` full-text search via Reciprocal Rank Fusion (RRF, k=60). Both legs independently rank the top 60 candidates, then fuse scores as `1/(k + vec_rank) + 1/(k + fts_rank)`.

At 569 chunks, the vector leg already places correct results in the top positions reliably. The FTS leg does not provide enough differentiation to change the final ranking. This is expected: RRF shines when the two signals disagree (one finds what the other misses), but with a small, homogeneous corpus, they largely agree.

Hybrid search should provide more value as the corpus grows (multiple books, thousands of chunks) and vector similarity starts producing more ties in the neighborhood.

### tsvector Weight Configuration Matters

The `search_vector` tsvector column uses three weight tiers populated during upsert:

- **Weight A**: `entity_name`, `class_name`, `feature_name` (exact-match signals)
- **Weight B**: `content_type` (with underscores replaced by spaces)
- **Weight C**: body text

Including `content_type` at weight B was critical for preventing regressions. Without it, queries like "What does the Blinded condition do?" failed because the word "condition" doesn't appear in the chunk text -- only in the metadata. Adding `content_type` to the tsvector ensures FTS can match on category terms.

### Two-Pass PDF Extraction Solved Class Metadata Issues

The initial single-pass extractor misidentified `class_name` metadata because 20pt headings in the PDF include both entity names (Cleric, Fighter, Rogue, Wizard) and structural headings ("Class Features"). The structural heading overwrote `current_class`, causing 46 chunks to have `class_name=None` and 16 to have `class_name="Class Features"`.

A blocklist approach was rejected as brittle. Instead, we implemented a two-pass extraction:

- **Pass 1** scans all pages for 20pt headings and builds a page-ownership map. It identifies the most-repeated 20pt heading as the structural marker (e.g., "Class Features" appears once per class) and excludes it. Each page is mapped to its owning entity based on the most recent non-structural 20pt heading.
- **Pass 2** uses `page_owner_map[page_num]` to resolve `class_name` instead of trusting every 20pt heading encountered during extraction.

This approach generalizes to other books where structural vs. entity headings share the same font size.

### Embedding Model Selection Was the Highest-Leverage Decision

Two local Ollama models were tested and rejected before settling on `text-embedding-3-small`:

- `mxbai-embed-large` -- hard context limit of ~256 tokens effective (not the advertised 512), fails on 20% of chunks
- `nomic-embed-text` -- 8192 token context, but all 569 chunks scored in a compressed 0.55-0.65 cosine similarity band regardless of relevance. Fireball scored lower (0.548) than unrelated rule chunks (0.65). The model collapses on domain-specific corpora where all text shares the same register.

`text-embedding-3-small` resolved both issues: 8192-token context handles all chunks, and cosine distances spread well enough to rank correctly. Full corpus embed cost is ~$0.0012.

The lesson: always test embedding models against your actual corpus, not synthetic benchmarks. A model that discriminates well on general text can fail completely on a domain-specific collection.

### Precision@5 Interpretation

46% P@5 sounds low in absolute terms but needs context. Industry targets vary by domain: web search aims for 70-80%, internal knowledge bases target 50-70%, and specialized corpora with narrow vocabularies (like D&D rules) are at the harder end because adjacent chunks share vocabulary and structure.

In this corpus, positions 2-5 are typically filled with related-but-not-matching chunks: other classes' HP blocks when asking about Fighter HP, other healing spells when asking about Cure Wounds, other combat rules when asking about grappling. These aren't irrelevant garbage -- they're topically adjacent content that the embedding model correctly identifies as nearby in vector space. For a RAG system where the LLM can filter context, Hit@1 at 90% is the more meaningful quality signal.

---

## Infrastructure

### Database

PostgreSQL 17 with pgvector extension, running in Docker (`pgvector/pgvector:pg17-bookworm`). Schema lives in `dnd.*`, separate from the harness code-repo RAG schema (`harness.*`).

HNSW index on the embedding column (chosen over IVFFlat because HNSW has no minimum row count requirement). GIN index on the `search_vector` tsvector column for fast `@@` operator queries. B-tree indexes on `content_type`, `book_slug`, `entity_name`, and `class_name` for metadata filtering.

### Eval Tooling

`eval_golden.py` supports `--mode vector|hybrid` (default: hybrid). Each run embeds all 20 queries via OpenAI, retrieves top 5 from pgvector, scores against expected criteria, and writes per-query results to `eval_results.json`. The script reads `.env` from the repo root for `DATABASE_URL` and `OPENAI_API_KEY`.

---

## Known Issues and Next Steps

### Open Bugs

1. **Dwarf/Elf entity mislabeling** -- chunks near the Dwarf-Elf page boundary have incorrect `entity_name`. Fix in `_build_entity_ownership_map()` in `extract.py`, then re-extract and re-embed.

### Retrieval Improvements

2. **Query-time metadata filtering** -- for queries that name a specific class or entity, extract the entity from the query and add a `WHERE class_name = 'Wizard'` clause before vector search. This would fix Q13 (Wizard saving throws) without a reranker.
3. **Cross-encoder reranker** -- rescore the top-k vector results with a cross-encoder model to improve ranking precision. Higher latency but would improve P@5 significantly.
4. **Content-type routing** -- classify the query intent (spell lookup vs. rule lookup vs. class feature) and filter `content_type` accordingly.

### Corpus Expansion

5. **Additional source books** -- add Monster Manual, Dungeon Master's Guide, and expansion content. This will increase corpus size to 2000+ chunks where hybrid search should start showing measurable benefit over pure vector.
6. **Re-evaluate hybrid search** -- after corpus expansion, re-run the golden set to see if RRF fusion starts outperforming pure vector.

### Eval Expansion

7. **Add MRR (Mean Reciprocal Rank)** -- currently only tracking Hit@1 and P@5. MRR would give a smoother signal across the full ranking.
8. **Add Recall@10** -- measure whether the correct chunk appears anywhere in the top 10, useful for evaluating reranker potential.
9. **Stratify by content type** -- break down metrics per `content_type` to identify which categories need the most improvement.

# D&D 5e RAG — Corpus Expansion + Full Eval Suite (MM + DMG)

> **Date**: 2026-06-08
> **Corpus**: 2,271 chunks across 3 books (was 569, PHB Basic only)
> **Embedding model**: `text-embedding-3-small` (1536d, OpenAI API)
> **Eval suite**: 64 queries (was 20), stratified by category and book
> **Prior reports**: [2026-05-11 baseline](dnd-retrieval-eval-report.md) · [2026-06-08 post-filter](dnd-retrieval-eval-report-2026-06-08.md)

---

## Executive Summary

The corpus quadrupled — Monster Manual (777 chunks) and Dungeon Master's Guide (925 chunks) now sit alongside PHB Basic (569) — and the golden suite tripled to 64 queries covering ten categories plus negative (out-of-corpus) probes. Retrieval quality **held at scale**: **Hit@1 = 98.3%** (59/60 positives), **MRR = 0.983**, **Recall@10 = 98.3%**. Hybrid and pure vector remain indistinguishable. The reranker question (bo4) stays closed on this evidence, with one new data point in its favor for later: the single miss is a *filter-logic* failure, not a ranking failure — a reranker would not have fixed it.

| Metric | 2026-05-11 (569 chunks, 20 q) | 2026-06-08 filters (569, 20 q) | **Now (2,271 chunks, 60 q)** |
|---|---|---|---|
| Hit@1 | 90% | 100% | **98.3%** |
| MRR | — | 1.000 | **0.983** |
| P@5 | 46% | 43% | **50.3%** |
| Recall@10 | — | 100% | **98.3%** |

---

## What Was Built

### 1. `extract_scan.py` — structure-driven extraction for OCR scans

The MM and DMG PDFs are OCR'd scans: font sizes drift (a monster name renders anywhere from 11.6 to 13.4pt), names carry recognition noise ("MANTIC ORE", "GoBLIN Boss", "Tholl"), and `extract.py`'s font-tier approach (calibrated for PHB Basic's crisp 24/20/12/10pt) cannot hold. The new extractor anchors on **textual structure**:

- **Monster Manual** — a line matching `Armor Class <n>` opens a stat block. A two-pass ownership pass binds each anchor to its monster: nearest caps heading *same column above* → else *same page, either column* (the Basilisk layout: stat block fills the left column while "BASILISK" heads the right) → else nearest preceding family section ("BEHOLDERS" owns the Beholder stat block when no per-monster heading survives OCR). Lore prose attaches to the current section. Output: 409 stat blocks + 368 lore chunks, 378 distinct entities, `content_type=monster`.
- **DMG** — a rarity line (`Wondrous item, very rare`) with a caps name immediately above opens a `magic_item` chunk (234 extracted; the DMG has ~240). Caps section heads split everything else into `dm_guidance` chunks (691).

Verified clean extraction: 22 iconic monsters (Tarrasque, Kraken, Ancient Red Dragon, Mind Flayer, Medusa…), 25 marquee items (Bag of Holding, Deck of Many Things, Staff of the Magi, Holy Avenger…), 19 guidance sections (Madness, Traps, Chases, Shadowfell, Feywild…).

**Known OCR tail**: a handful of entities are mislabeled at the entity_name level (Troll → "Tholl", Orc absorbed into "Orog", Vorpal Sword lost). Body text still contains the correct words, so retrieval degrades gracefully. A cleanup pass (fuzzy-merge against a canonical monster list) is filed as follow-up work.

### 2. Eval suite: 20 → 64 queries, stratified

New `category` + `book` tags on every query; per-category metrics in the summary. Categories: `spell_lookup` (6), `class_feature` (5), `condition` (3), `rule` (4), `race_feature` (2), `monster_stat` (12), `monster_lore` (6), `magic_item` (10), `dm_guidance` (8), `cross_book` (4), `negative` (4).

**Negative queries** probe out-of-corpus content (Artificer, Wild Shape, spelljamming, THAC0) and report top-1 distance with no pass/fail — the data shows clean separation (positives average 0.346; negatives sit at 0.42–0.62), meaning a future answerability gate at ~0.40 cosine distance is viable.

### 3. Filter hardening found by the new suite

The expanded suite immediately caught a real bug: entity filters used exact `= ANY()` matching, so "Is there a magic item that makes you invisible?" matched vocab entity "Invisible" (the condition) and **excluded every item named "… Of Invisibility"** — a guaranteed miss. Fixed with stemmed `ILIKE` patterns (`Invisible` → `%Invis%`). P@5 also rose 45% → 50.3% from the relaxation. This is exactly what the suite is for.

---

## Results by Category (vector mode, 2,271 chunks)

| category | n | Hit@1 | P@5 | MRR | R@10 |
|---|---|---|---|---|---|
| class_feature | 5 | 5/5 | 92.0% | 1.000 | 5/5 |
| condition | 3 | 3/3 | 20.0% | 1.000 | 3/3 |
| cross_book | 4 | 4/4 | 45.0% | 1.000 | 4/4 |
| dm_guidance | 8 | 7/8 | 77.5% | 0.875 | 7/8 |
| magic_item | 10 | 10/10 | 20.0% | 1.000 | 10/10 |
| monster_lore | 6 | 6/6 | 66.7% | 1.000 | 6/6 |
| monster_stat | 12 | 12/12 | 50.0% | 1.000 | 12/12 |
| race_feature | 2 | 2/2 | 90.0% | 1.000 | 2/2 |
| rule | 4 | 4/4 | 30.0% | 1.000 | 4/4 |
| spell_lookup | 6 | 6/6 | 33.3% | 1.000 | 6/6 |

Reading the P@5 column correctly: categories with single-chunk answers (one spell, one condition, one magic item) cap out at 20% P@5 by construction — there is exactly one relevant chunk and five slots. Categories with multi-chunk answers (a class's features, a monster's stat block + lore, a guidance section's chunks) score higher. P@5 is a noise gauge here, not a quality gauge; Hit@1/MRR are load-bearing.

### The one miss

Q51 "How does a DM build a balanced combat encounter?" — query routing matched the generic PHB vocab entity "Combat", and the filter `entity ILIKE '%combat%' AND content_type=dm_guidance` excluded the actual "Creating Encounters" chunks. A paraphrase gap: the query says "combat encounter", the section is named "Creating Encounters".

This is a **filter over-restriction** failure, not a ranking failure — the right chunk never entered the candidate set, so no reranker could recover it. Candidate fixes (filed): fallback to unfiltered retrieval when filtered top-1 distance is weak; or drop single-word generic vocab entities ("Combat", "Equipment", "Tools") from the entity filter.

---

## Hybrid vs Vector at 2,271 Chunks (3q3 data point)

Identical headline metrics (98.3% / 0.983 / 50.3%). Caveat: most queries now carry filters and therefore route through the filtered-vector path in both modes; only unfiltered queries exercised `dnd.hybrid_search`. A true hybrid-vs-vector A/B at this scale needs the filters threaded into the hybrid SQL function first. The 3q3 bead stays open behind further corpus growth.

## Reranker Status (bo4)

Still closed, evidence updated: at 4× corpus, Hit@1 is 98.3% and MRR 0.983 with metadata filtering alone. The sole miss is unreachable by reranking. **Reopen trigger remains**: if future expansion (more books, longer-tail queries) produces misses where the right chunk is *in* the top 10 but not at rank 1 (high Recall@10, sagging Hit@1/MRR), a cross-encoder becomes the right tool. The new Recall@10-vs-Hit@1 gap instrumentation makes that trigger directly observable.

---

## Reproducibility

```bash
cd repos/rag-chat
docker compose up -d

# Extract (OCR-scanned books)
uv run --with pdfplumber python ingestion/extract_scan.py \
  "../DnD-Books/5e/Books/D&D 5E - Monster Manual.pdf" --book-slug mm-5e --out ingestion/chunks-mm-5e.jsonl
uv run --with pdfplumber python ingestion/extract_scan.py \
  "../DnD-Books/5e/Books/D&D 5E - Dungeon Master's Guide.pdf" --book-slug dmg-5e --out ingestion/chunks-dmg-5e.jsonl

# Embed + upsert (idempotent by chunk_id; needs OPENAI_API_KEY + DATABASE_URL exported)
uv run --with "psycopg[binary]" --with openai python ingestion/embed.py --chunks ingestion/chunks-mm-5e.jsonl
uv run --with "psycopg[binary]" --with openai python ingestion/embed.py --chunks ingestion/chunks-dmg-5e.jsonl

# Eval (64 queries) + unit tests (35)
PYTHONIOENCODING=utf-8 uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py --mode vector
uv run python ingestion/test_extract_scan.py
uv run --with "psycopg[binary]" --with openai python ingestion/test_eval_golden.py
```

## Follow-ups Filed

1. **OCR entity cleanup** — fuzzy-merge mislabeled entity names (Tholl→Troll, 0Rog→Orc/Orog) against a canonical list; re-upsert.
2. **Filter fallback** — retry unfiltered when filtered top-1 distance is weak (fixes the Q51 class of misses).
3. **PHB Basic re-ingest** — source PDF is not in the repo; the ymv extractor fix is still uncompensated in the live data (the query-time filter masks it).
4. **Answerability gate** — negatives separate at ~0.40 cosine distance; wire a refusal threshold into the future agent service.
5. **Remaining 5E books** (pd0 stays open) — Xanathar's, Tasha's, Volo's, Mordenkainen's et al. via `extract_scan.py` book configs. 4E stays out of scope until the schema carries an edition dimension.

# D&D 5e RAG — Full Corpus Expansion: Extraction QA + Comprehensive Eval

> **Date**: 2026-06-08
> **Corpus**: 8,851 chunks across 12 books (was 2,271 across 3)
> **Embedding model**: `text-embedding-3-small` (1536d)
> **Eval suite**: 171 queries (was 64), generated from the corpus + curated
> **Epic**: agent-forge-harness-t4q · **Plan**: plans/drafts/dnd-corpus-full-5e-expansion.md
> **Prior**: [corpus-expansion (MM+DMG)](dnd-corpus-expansion-eval-2026-06-08.md)

---

## Executive Summary

The corpus quadrupled — 10 cleanly-extractable 5E books joined Monster Manual and DMG — and a
**pre-embedding QA gate** now stands between extraction and the vector store. Retrieval quality, as
measured by a 171-query stratified suite generated from the real corpus, is **Hit@1 74.7%,
MRR 0.808, Recall@10 91.0%, P@5 34.3%**.

That is honestly lower than the 98.3% Hit@1 on the old 3-book corpus — and the drop is informative,
not alarming: 4× more confusable content, harder auto-generated queries spanning every book, and the
coarse `rule` bucketing the generic supplement extractor produces. The single most actionable signal:
**Recall@10 (91%) far exceeds Hit@1 (74.7%)** — a 16-point gap where the right chunk *is* retrieved
but not ranked first. That is exactly the documented trigger to reopen the cross-encoder reranker
(bo4).

---

## Part 1 — Extraction + Pre-Embedding QA

### Engine: pymupdf, selected per book

`extract_scan.py` gained a pymupdf (fitz) reader alongside the original pdfplumber one. The parity
gate caught fitz regressing the MM stat-block heuristics (377 blocks vs 409; Tarrasque + Lich
dropped) because they were calibrated against pdfplumber's line grouping. Rather than re-tune two
already-embedded, validated books for zero benefit, the engine is **per-book**: `mm-5e`/`dmg-5e`
stay on pdfplumber (exact 777/925 reproduced); the 10 new books use fitz, which decodes layers
pdfplumber can't (Xanathar's junk OCR layer, Tortle's CID fonts).

### Anchor-driven supplement extraction

Supplements interleave spells, feats, monsters, and prose. Their spell/feat names are small and
unbolded (XGE spell names render at 9.3pt), invisible to heading detection. `extract_supplement_chunks`
anchors on the structural sub-header that always follows the name — `Nth-level <school>` /
`<school> cantrip` for spells, `Prerequisite:` for feats, `Armor Class N` for stat blocks — and pulls
the name from the line above. On XGE this lifted spell detection from 8 to 53 with correct names.

### The QA gate (`qa_chunks.py`)

Every chunk is validated before embedding; failures are quarantined, never embedded:

| signal | catches |
|--------|---------|
| `pua_control_ratio > 2%` | Wayfinders-class custom-font glyphs |
| `has_cid_marker` | undecoded CID fonts |
| `alpha_ratio < 50%` | junk OCR layers |
| `length` (<5 words or >8000 chars) | fragments / runaway merges |
| `entity_name_ok` | OCR-mangled names (`0Rog`, `Due;&Gar`) |

The 8000-char cap was set after the gate was caught false-quarantining complete legendary stat blocks
(Solar 3207ch, Ankheg 4095ch are atomic and legitimate). `dict_word_ratio` from the plan was dropped
for v1 (no wordlist in repo/stdlib; the other checks suffice).

### Per-book QA pass rates

| book | extracted | clean | pass | note |
|------|-----------|-------|------|------|
| phb-5e | 942 | 885 | 94.0% | full PHB (replaces Basic) |
| xge-5e | 762 | 743 | 97.5% | 53 spells, 15 feats |
| tce-5e | 630 | 630 | 100% | |
| vgm-5e | 1442 | 1263 | **87.6%** | OCR monster-name noise (below 90% canary) |
| mtf-5e | 979 | 909 | 92.8% | monster-heavy |
| eepc-5e | 162 | 162 | 100% | |
| scag-5e | 699 | 691 | 98.9% | |
| tortle-5e | 97 | 90 | 92.8% | |
| eberron-5e | 1070 | 1065 | 99.5% | |
| ravnica-5e | 732 | 711 | 97.1% | |

The gate is the canary working as designed: vgm-5e dips below 90% on inherent scan OCR noise (mangled
monster names), and the gate quarantines exactly those so only clean chunks embed. This is the
"evaluate the output of the files" measurement surface, now operational.

### Final corpus

12 books, 8,851 chunks. content_type: rule 6266, monster 1202, dm_guidance 691, spell 292,
magic_item 234, feat 166. PHB Basic v0.2 (569 chunks) retired in favor of full `phb-5e`.

---

## Part 2 — Comprehensive Eval

### The suite (171 queries)

`gen_golden.py` samples real `entity_name` rows per (content_type, book) and templates questions, so
every `expected_entity`/`content_type`/`book` matches a real row — no hand-typed label drift.
Hand-curated on top: 6 cross-book collisions (Invisibility spell vs item, Shield spell vs Shield
Guardian, Giant Strength) and 5 negatives genuinely outside the 12-book corpus (spelljamming,
Strixhaven, THAC0, …). Note Artificer (TCE) and Druid Wild Shape (PHB) are now *in*-corpus, so they
are no longer negatives.

Distribution: rule 60, monster 33, feat 30, spell 25, cross_book 6, magic_item 6, dm_guidance 6,
negative 5.

### Headline (vector mode; hybrid identical)

| metric | 3-book (64q) | **12-book (171q)** |
|--------|--------------|--------------------|
| Hit@1 | 98.3% | **74.7%** (124/166) |
| MRR | 0.983 | **0.808** |
| Recall@10 | 98.3% | **91.0%** |
| P@5 | 50.3% | **34.3%** |

### By category

| category | n | Hit@1 | P@5 | MRR | R@10 |
|----------|---|-------|-----|-----|------|
| magic_item | 6 | 6/6 | 20.0% | **1.000** | 6/6 |
| spell_lookup | 25 | 22/25 | 22.4% | 0.907 | 24/25 |
| feat | 30 | 24/30 | 22.7% | 0.856 | 28/30 |
| rule | 60 | 42/60 | 47.7% | 0.798 | 57/60 |
| monster | 33 | 23/33 | 30.3% | 0.734 | 27/33 |
| cross_book | 6 | 4/6 | 33.3% | 0.667 | 4/6 |
| dm_guidance | 6 | 3/6 | 46.7% | 0.604 | 5/6 |

Reading it: clean, well-structured content tops out (magic_item from the tidy DMG extraction is
perfect; spells and feats — found by precise anchors — are strong). The weaker categories trace to
two causes: (1) OCR name noise in the scanned bestiaries (VGM/MTF) drags `monster`; (2) the generic
supplement extractor's coarse `rule` bucketing means the content-type filter can't sharpen those
queries. `dm_guidance` is weak on a small n with paraphrase gaps.

### Negative separation

Positives average **0.3947** top-1 cosine distance; the 5 negatives sit at **0.50–0.67**. Still
separable — an answerability gate (koz) at ~0.45 remains viable — but tighter than the 3-book corpus,
because 8,851 chunks pack the neighborhood more densely. Some negatives' top hits show OCR noise
(`The C O Re Rules`, `Ac T Ions`), underscoring the value of the OCR cleanup bead (qg4).

---

## Decisions & Follow-ups

### Reopen bo4 (cross-encoder reranker) — trigger met

The bo4 supersession was explicitly conditioned on "reopen if corpus expansion produces misses where
the right chunk is in the top 10 but not at rank 1." At 8,851 chunks that is now true: **Recall@10
91% vs Hit@1 74.7%** — a 16-point ranking gap a cross-encoder is built to close. The per-category
instrumentation localizes where (monster, cross_book). **bo4 should be reopened.**

### Other follow-ups (open beads)

- **qg4** OCR entity cleanup — now higher value: mangled names hurt `monster` Hit@1 and pollute
  negative top-hits.
- **ipl** filter fallback — the generic-vocab over-restriction class (e.g. an entity `The`) appears
  in a few generated queries.
- **3q3** hybrid re-eval — still blocked: hybrid ≡ vector because filtered queries bypass the FTS
  leg; `dnd.hybrid_search` needs filter support before a real A/B.
- **koz** answerability gate — negative separation supports a ~0.45 threshold.
- **T7** OCR Wayfinders + Blood Hunter — deferred (no OCR tooling).
- **Supplement extraction depth** — the coarse `rule` bucket (6,266 chunks) is the biggest lever for
  `rule`/`monster`/setting-lore retrieval; per-book layout refinement is the follow-up the eval data
  now justifies.

---

## Reproducibility

```bash
cd repos/rag-chat
docker compose up -d
# Extract → QA → embed all 10 Tier-A books (per-book engine from BOOK_CONFIGS):
uv run --with pymupdf --with pdfplumber --with "psycopg[binary]" --with openai \
    python ingestion/ingest_books.py
# Retire PHB Basic:
docker exec rag-chat-vector-db psql -U rag -d rag_chat \
    -c "DELETE FROM dnd.chunks WHERE book_slug='phb-basic-v0.2';"
# Regenerate the golden suite + eval:
uv run --with "psycopg[binary]" python ingestion/gen_golden.py --per 6
PYTHONIOENCODING=utf-8 uv run --with "psycopg[binary]" --with openai \
    python ingestion/eval_golden.py --mode vector
# Unit tests (no DB/PDF): 37 extract_scan + 20 qa_chunks + 9 gen_golden + 29 eval
```

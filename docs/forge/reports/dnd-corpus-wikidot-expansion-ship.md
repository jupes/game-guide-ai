# Ship Report: dnd-corpus-wikidot-expansion — Expand D&D corpus with dnd5e.wikidot.com
Shipped: 2026-08-21
Feature: agent-forge-harness-4x66 · Branch: feat/agent-forge-harness-4x66-wikidot-corpus · PR: (created below)

## What Shipped

A second, non-PDF ingestion source — dnd5e.wikidot.com (CC BY-SA 3.0) — now widens the closed-tester
corpus alongside the 11 PDF books. `dnd.chunks` carries source provenance (`source_type`,
`source_url`, `license`), a new `scrape_wikidot.py` crawls the 5 namespaces that actually exist on
the live site (spells, races, classes, feats, equipment — 3 others research assumed don't exist
there), and citations attribute wikidot-sourced answers with their CC BY-SA notice. The real local
dev corpus grew from 9,067 to 9,916 chunks; a same-golden-set eval re-run confirmed no genuine
retrieval regression.

## Before → After

| Area | Before | After |
|------|--------|-------|
| Ingestion sources | PDF books only (`extract.py`/`extract_scan.py`) | PDF books + `dnd5e.wikidot.com` (`scrape_wikidot.py`) |
| `dnd.chunks` schema | No provenance columns; `page_start`/`page_end` `NOT NULL` (PDF-shaped) | `source_type`/`source_url`/`license`; page fields nullable for non-paginated web content |
| Corpus size (local dev) | 9,067 chunks (12 PDF books) | 9,916 chunks (12 PDF books + `wikidot-5e`) |
| Citations for scraped content | N/A — didn't exist | `"D&D 5e Wiki — dnd5e.wikidot.com (CC BY-SA 3.0)"` shown in-place, same citation mechanism PDF sources already use |
| Corpus-vs-corpus visibility | N/A | `scrape_wikidot.py --dedup-report` — non-blocking (content_type, entity_name) overlap report, no filtering |
| Licensing decision record | Only `x5bz.5` (PDF corpus) documented | + `docs/licensing-wikidot-corpus.md` for the wikidot source |

## Work Done

- Checkpoint A — `dnd.chunks` provenance migration, idempotent, applied to the real local dev DB (`cbd53d3`)
- Checkpoint B — `embed.py`/`DndChunk` carry provenance, backward compatible with pre-existing chunk files (`76410fb`)
- Checkpoint C — `scrape_wikidot.py` page parser (tracer bullet), verified against a real fetched page (`d83270b`)
- Checkpoint D — rate-limited cached fetch layer + real namespace discovery, verified live (`99f9f66`)
- Checkpoint E — full crawl → QA → embed orchestration + dedup report (`1201725`)
- Checkpoint F — CC BY-SA citation attribution + licensing doc (`32873ad`)
- Checkpoint G — real full crawl (880 pages) run against the real corpus + eval re-run vs baseline (`3d2a96e`)

## Beads Completed

| Beads ID | Title | Status |
|----------|-------|--------|
| agent-forge-harness-4x66.1 | Checkpoint A — corpus schema migration | closed |
| agent-forge-harness-4x66.2 | Checkpoint B — embed.py/DndChunk provenance plumbing | closed |
| agent-forge-harness-4x66.3 | Checkpoint C — wikidot page parser | closed |
| agent-forge-harness-4x66.4 | Checkpoint D — rate-limited cached fetch layer | closed |
| agent-forge-harness-4x66.5 | Checkpoint E — full crawl → QA → embed + dedup report | closed |
| agent-forge-harness-4x66.6 | Checkpoint F — citation attribution + licensing doc | closed |
| agent-forge-harness-4x66.7 | Checkpoint G — eval re-run vs baseline | closed |
| agent-forge-harness-4x66.8 | Split race/class/equipment pages into per-section chunks | deferred (P3) |
| agent-forge-harness-4x66.9 | Refresh 4 golden-set entries made stale by this feature | deferred (P3) |
| agent-forge-harness-h310 | Licensing review: Roll20 as a corpus source | deferred (P4, pre-existing) |
| agent-forge-harness-4x66 | Expand D&D corpus with dnd5e.wikidot.com | closed (below) |

## Real Numbers, Not a Sample

The full production crawl ran during implementation, not just tests: **880 pages** across the 5
real namespaces, **849 passed QA** (96.5%), embedded into the actual local dev corpus (the
`game-guide-ai_pgvector_data` volume). `dedup_report()`: 422/849 (50%) overlap an existing PDF
entity — expected, left untouched (visibility only, no filtering).

`eval_golden.py` on the same 179-query golden set, immediately before vs. after ingestion:

| Metric | Before (9,067 chunks) | After (9,916 chunks) |
|--------|------------------------|------------------------|
| Hit@1 | 82.8% | 81.6% |
| MRR | 0.866 | 0.854 |
| Recall@10 | 93.1% | 92.0% |
| Precision@5 | 43.6% | **47.4%** (improved) |
| Negatives correctly refused | 5/5 | 3/5 |

Both before and after sit well above the historical baseline recorded in `wsq` (74.7% / 0.808 /
91.0% @ 8,851 chunks) — the corpus has grown since that number was recorded, independent of this
feature. The small aggregate dip traces to exactly 4 of 179 golden-set entries, each root-caused:

- **Water Genasi / Earth Genasi** (2 Hit@1 flips): the wikidot chunk ties the PDF chunk almost
  exactly on cosine distance but is tagged `content_type=race_feature` — the semantically correct
  tag. `golden_set.json` expects `rule`, inherited from a pre-existing PDF-extraction tagging quirk
  this feature didn't create.
- **Spelljamming / Strixhaven Mascot** (2 negative-query flips): both were "unanswerable" only
  because the corpus lacked that content before. Real wikidot pages for both now exist — genuine
  coverage growth, not a retrieval defect. The other 3 negative queries in the suite are
  byte-for-byte unchanged.

Confirmed with the user: accepted as no real regression (AC4 satisfied); the golden-set staleness
is tracked separately as `agent-forge-harness-4x66.9` rather than blocking this feature.

`eval_answers.py --limit 10` (6-case spot-check): `answer_relevancy` 100%, `faithfulness` 83%
(healthy); `answer_correctness`/`context_precision`/`context_recall` scored low, but
`docs/observability/answer-eval.md` documents that this metric needs "~20–30 well-reviewed cases"
to be meaningful — not treated as a signal at n=6.

## Test It Yourself (walkthrough)

1. `docker run -d --name gga-vector-db -e POSTGRES_USER=rag -e POSTGRES_PASSWORD=rag_dev_change_me -e POSTGRES_DB=game_guide_ai -p 5433:5432 -v game-guide-ai_pgvector_data:/var/lib/postgresql/data pgvector/pgvector:pg17-bookworm`
   (or use your existing local dev DB — see the Known Gaps note on `docker compose` below)
2. `docker exec gga-vector-db psql -U rag -d game_guide_ai -c "select book_slug, source_type, count(*) from dnd.chunks group by 1,2 order by 3 desc;"`
   - Expect: a `wikidot-5e | wiki | 849` row alongside the 12 PDF book rows.
3. `DATABASE_URL=postgresql://rag:rag_dev_change_me@localhost:5433/game_guide_ai PYTHONUTF8=1 uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py`
   - Expect: Hit@1/MRR/Recall@10 in the low-80s/0.85/low-90s range (see table above).
4. Automated: `DATABASE_URL=... uv run --with '.[test]' python -m pytest -q --no-cov` — expect all green (677 passed, 1 skipped).
5. Re-run the crawl yourself: `uv run python ingestion/scrape_wikidot.py --out ingestion/chunks-wikidot-5e.jsonl --limit 3` — expect a small sample JSONL with `source_type=wiki`, `license="CC BY-SA 3.0"` on every line.

## Follow-ups / Known Gaps

- `agent-forge-harness-4x66.8` (P3) — race/class/equipment wikidot pages need per-section
  splitting; whole-page chunks blow past `qa_chunks.py`'s length gate.
- `agent-forge-harness-4x66.9` (P3) — refresh the 4 golden-set entries this feature made stale.
- `agent-forge-harness-h310` (P4, pre-existing) — Roll20 compendium licensing review, deferred
  from research phase.
- `agent-forge-harness-lvmd` (P3, pre-existing, unrelated) — `docker compose up -d vector-db`
  fails on Windows Docker Desktop (nested file-bind-mount under a `:ro` dir mount); worked around
  during implementation with a bare `docker run` against the same named volume (see walkthrough
  step 1 above).
- Applying `vector-db/init/03a-corpus-provenance.sql` to the live Cloud SQL pilot DB is a
  deploy-time step, not exercised by CI — do this before deploying this branch's code.

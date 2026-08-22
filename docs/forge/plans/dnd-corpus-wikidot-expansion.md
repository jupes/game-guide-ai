# Plan: dnd-corpus-wikidot-expansion — Expand D&D corpus with dnd5e.wikidot.com
Generated: 2026-08-19
Repo: game-guide-ai
Phase: plan (2/4) — from plans/research/dnd-corpus-wikidot-expansion.md
Beads: agent-forge-harness-4x66 (feature)

## Summary

Add `dnd5e.wikidot.com` (CC BY-SA 3.0) as a second, non-PDF ingestion source, closing three gaps
the research phase found: no provenance columns on `dnd.chunks`, no scraper, and no attribution
path in citations. New: `ingestion/scrape_wikidot.py` (pure HTML→chunk parser + rate-limited
cached fetcher), a `dnd.chunks` schema migration (`source_type`/`source_url`/`license`, nullable
page fields), a small `embed.py`/`generate.py` extension to carry and surface provenance, and a
licensing doc. Reuses `qa_chunks.py` and `embed.py` unchanged in their core logic. Closes with a
real eval re-run against the existing golden set (same set, before vs. after — not regenerated) to
satisfy the bead's AC4 (quality must not regress) and to hand real numbers to the next initiative
(retrieval-eval review).

## Existing Code to Reuse

- `ingestion/qa_chunks.py` — pre-embedding QA gate, all `.get()`-based dict access, no PDF-specific
  required fields. Used unmodified against wiki-sourced chunk dicts.
- `ingestion/embed.py` `embed_and_upsert()` / `_upsert_batch()` — batch embed + upsert loop, driven
  entirely by `chunks.jsonl` dict shape. Only the SQL column list and one helper function change.
- `ingestion/retrieval.py` `RetrievedChunk` / `fetch_chunk_details()` — `book_slug` already flows
  through to `service/generate.py build_sources()` per-citation; no new plumbing needed for
  attribution beyond a book-label special case.
- `service/generate.py build_sources()` — one `Source` per chunk; `page: int | None` is already
  nullable in `service/models.py`, so wiki chunks with no page number need no model change.
- `scripts/bootstrap-db.sh` pattern (numbered SQL files applied in order) — mirrored, not modified,
  for the new migration file.
- `tests/test_schema.py` pattern (`needs_db` skip, behavioral assertions against a real Postgres) —
  mirrored for the new corpus-schema test.

## Key Constraint Found in Research (binding on this plan)

`vector-db/init/*.sql` (the `dnd.chunks` DDL) is **init-only** — unlike `service/sql/04-*`/`05-*`,
there is no `ensure_schema()`-equivalent that re-applies it to an existing database at startup
(confirmed in `service/schema.py`: `ALL_SCHEMAS = (CHAT_SCHEMA, AUTH_SCHEMA)` only, and
`scripts/bootstrap-db.sh`'s comment: "Apply every schema file... to a database that does not have
them yet"). The local dev DB and the live Cloud SQL pilot DB (LIVE, 9067+ chunks, per
`docs/deploy-gcp.md:196-210`) both already exist with data, so the migration file must be
**manually applied once** to each — this is not automatic like the chat/auth schemas. Checkpoint A
calls this out explicitly, and also updates `scripts/bootstrap-db.sh` so a *future* fresh bootstrap
doesn't miss it (plan-review finding, turn 1).

## TDD Strategy (red-green-refactor)

Following `.claude/skills/tdd`. Behaviors tested through public interfaces (SQL shape, pure parse
functions, `Source` objects), vertically — parser first (no DB/network needed), then plumbing,
then the full crawl.

| # | Behavior (as a spec) | Test file | Tracer? |
|---|----------------------|-----------|---------|
| 1 | A wikidot page's HTML parses into chunk-shaped dicts with a stable id, `source_type='wiki'`, correct `content_type` per the explicit namespace map (incl. class→class_feature, race→race_feature, equipment→rule) | `ingestion/tests/test_scrape_wikidot.py` | **yes** |
| 2 | `dnd.chunks` gains `source_type`/`source_url`/`license`, nullable `page_start`/`page_end`; applying the migration twice does not error | `tests/test_corpus_schema.py` | no |
| 3 | `embed.py`'s upsert-row builder fills `source_type='pdf'` for legacy chunk dicts that lack the field, and passes wiki-shaped fields through unchanged | `ingestion/tests/test_embed.py` | no |
| 4 | `fetch_page()` returns cached content without a network call on a cache hit | `ingestion/tests/test_scrape_wikidot.py` | no |
| 5 | A wiki-sourced `Source.book` carries CC BY-SA attribution text; a PDF-sourced `Source.book` is unchanged (regression guard) | `service/tests/test_service.py` | no |
| 6 | `eval_golden.py` run against the expanded corpus does not regress Hit@1/MRR/Recall@10 vs the recorded baseline (Hit@1 74.7%, MRR 0.808, Recall@10 91.0% @ 8851 chunks, per `wsq`) — same golden set, before vs. after | manual eval run, not a unit test | no |

Refactor watch-list (after green): `extract.py`/`extract_scan.py` both define their own `DndChunk`
dataclass (pre-existing duplication, not introduced by this feature) — `scrape_wikidot.py`
deliberately does **not** import either, emitting plain chunk-shaped dicts instead, so this feature
adds no new coupling to that duplication. Not fixed here — out of scope.

## Build Sequence & Checkpoints

### Checkpoint A — Schema migration (provenance columns, nullable pages)
Steps:
1. Add `vector-db/init/03a-corpus-provenance.sql` — idempotent: `ALTER TABLE dnd.chunks ADD COLUMN
   IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'pdf'`, `ADD COLUMN IF NOT EXISTS source_url
   TEXT`, `ADD COLUMN IF NOT EXISTS license TEXT`, `ALTER COLUMN page_start DROP NOT NULL`,
   `ALTER COLUMN page_end DROP NOT NULL`. Sorts after `03-hybrid-search.sql`, before
   `service/sql/04-chat-schema.sql` (lexical order in `docker-entrypoint-initdb.d`), so it's
   auto-applied on any fresh volume via the existing compose mount — no compose change needed.
2. Write `tests/test_corpus_schema.py` (mirrors `tests/test_schema.py`'s `needs_db` pattern):
   assert the three columns exist with the right type/nullability/default; assert running the file
   twice against the test DB doesn't raise.
3. Apply the migration **manually once** to the local dev DB (`docker compose exec -T vector-db
   psql -U rag -d game_guide_ai < vector-db/init/03a-corpus-provenance.sql`) so Checkpoint B+ have
   real columns to write into.
4. Document (in the file's header comment) that the same manual step is required against the
   Cloud SQL pilot DB before deploying the code from Checkpoint E/F — tracked as a deploy-time step
   in the ship report, not exercised by this plan's automated tests (no CI access to the pilot DB).
5. Add `"vector-db/init/03a-corpus-provenance.sql"` to `scripts/bootstrap-db.sh`'s `FILES` array
   (after `03-hybrid-search.sql`, before `service/sql/04-chat-schema.sql`) and update
   `tests/test_bootstrap_db.py::EXPECTED_ORDER` to match — otherwise a *future* fresh Cloud SQL
   bootstrap (a new environment, a disaster-recovery restore) would silently skip the provenance
   columns forever, since nothing re-applies `vector-db/init/` files to an existing database
   (plan-review finding, turn 1).
Demo: `DATABASE_URL=postgresql://rag:rag_dev_change_me@localhost:5433/game_guide_ai uv run --with '.[test]' python -m pytest tests/test_corpus_schema.py -q` — green, then `docker compose exec vector-db psql -U rag -d game_guide_ai -c '\d dnd.chunks'` shows the three new columns.

### Checkpoint B — Provenance flows through embed.py (backward compatible)
Steps:
1. In `ingestion/embed.py`, extract the per-chunk dict build (`row = dict(chunk); row["embedding"]
   = emb`) into a named pure function `_row_for_upsert(chunk: dict, embedding: list[float]) ->
   dict` that fills `source_type="pdf"` when absent (legacy PDF chunk dicts), leaves `source_url`/
   `license` as `None` when absent, and passes wiki-shaped dicts through unchanged.
2. Extend `_UPSERT_SQL`'s column list and `VALUES`/`ON CONFLICT DO UPDATE` clauses with
   `source_type`, `source_url`, `license`.
3. Add `source_type: str = "pdf"`, `source_url: str | None = None`, `license: str | None = None`
   fields (with defaults, so existing call sites don't break) to the `DndChunk` dataclasses in
   `ingestion/extract.py:302` and `ingestion/extract_scan.py:50`.
4. Write `ingestion/tests/test_embed.py`: `_row_for_upsert()` on a legacy dict (no `source_type`
   key) defaults to `"pdf"`; on a wiki-shaped dict, all three fields pass through unchanged.
Demo: `uv run --with '.[test]' python -m pytest ingestion/tests/test_embed.py -q`

### Checkpoint C — Wikidot page parser (tracer bullet, no network)
Steps:
1. New `ingestion/scrape_wikidot.py`: `parse_page(html: str, url: str, namespace: str) ->
   list[dict]` — pure function. Extracts page title → `entity_name`; infers `content_type` from
   `namespace` via an explicit map matching the *real* existing taxonomy (verified against
   `ingestion/extract.py:92-95` and `plans/research/dnd-corpus-wikidot-expansion.md:43-45` — the
   research doc's "no new content_type" claim is about the taxonomy as a whole, not a 1:1 namespace
   name match): `spell→spell`, `monster→monster`, `feat→feat`, `condition→condition`, `rule→rule`,
   `class→class_feature` (not bare `"class"`), `race→race_feature` (not bare `"race"`),
   `equipment→rule` (mirrors `extract.py:95`'s own PDF convention of folding equipment into
   `"rule"` — there is no distinct `"equipment"` content_type anywhere in the taxonomy, and
   inventing one would break `retrieval.py`'s `_CTYPE_KEYWORDS` content-type filtering, which has
   no entry for it). `chunk_id = sha256(f"{url}:{idx}")[:20]`
   (stable across re-scrapes of unchanged pages, mirrors the PDF extractors' stability contract);
   sets `book_slug="wikidot-5e"`, `source_type="wiki"`, `source_url=url`,
   `license="CC BY-SA 3.0"`, `page_start=None`, `page_end=None`, `source_file=url` (satisfies the
   `NOT NULL` `source_file` column without a schema change there).
2. Write `ingestion/tests/test_scrape_wikidot.py` with small hand-built synthetic wiki-markup HTML
   snippets (project convention per `test_extract_scan.py` — synthetic input, not checked-in
   scraped fixtures; also sidesteps any question about committing real CC BY-SA page content into
   the repo as test data). Cover every namespace→content_type mapping from step 1 explicitly
   (class→class_feature and race→race_feature are the ones most likely to regress silently, since
   they don't match the namespace name 1:1 — this is what caught the bug in plan review turn 2);
   same input parsed twice → same `chunk_id`.
Demo: `uv run --with '.[test]' python -m pytest ingestion/tests/test_scrape_wikidot.py -k parse_page -q`

### Checkpoint D — Rate-limited, cached fetch layer
Steps:
1. In `ingestion/scrape_wikidot.py`: `fetch_page(url: str, cache_dir: Path, rate_limit_s: float =
   1.0, _fetcher: Callable[[str], str] | None = None) -> str` — checks `cache_dir` for a
   URL-hashed file first; on a miss, sleeps `rate_limit_s`, calls `_fetcher` (defaults to a real
   HTTP GET with a project-identifying User-Agent), writes the result to cache. The injectable
   `_fetcher` keeps the network boundary pure-testable (small interface, deep implementation, per
   TDD skill).
2. `discover_urls(namespace: str) -> list[str]` — lists pages in one of the core namespaces decided
   in research. **Implementation-time finding (Checkpoint D):** of the eight namespaces research
   listed, only five actually exist as crawlable pages on dnd5e.wikidot.com — verified live, not
   guessed. `spell` (via `/spells`, 574 pages), `race` (the site calls this `lineage:` internally,
   not `race:` — via `/lineage`, 83 pages), `feat` (via the homepage itself, 199 pages), `class`
   (13 bare top-level slugs, e.g. `/fighter` — no namespace prefix at all), `equipment` (11 bare
   category-page slugs, e.g. `/weapons`, each covering many items on one page rather than
   page-per-item). `monster`, `condition`, and a general `rule` namespace are **not present on this
   site** — no bestiary/monster section anywhere in its master index, a page-tags search for
   "monster" returned nothing, and direct guesses at condition pages all 404. `discover_urls()`
   raises `ValueError` for these three rather than silently returning nothing. Dropped from this
   feature's crawl scope; the PDF corpus (Monster Manual, PHB conditions) already covers that
   content, so this isn't a coverage regression — see `scrape_wikidot.py`'s `discover_urls`
   docstring for the full trail.
3. Extend `test_scrape_wikidot.py`: `fetch_page()` with a pre-populated cache dir returns cached
   content and never calls `_fetcher` (assert via a call-counting stub).
Demo: `uv run --with '.[test]' python -m pytest ingestion/tests/test_scrape_wikidot.py -k fetch_page -q` (cache-hit path only — the real-network path has `(no live demo)` in CI; exercised live in Checkpoint E).

### Checkpoint E — Full crawl → QA → embed (book_slug=wikidot-5e) + dedup visibility report
Steps:
1. CLI entrypoint in `scrape_wikidot.py` (`--out ingestion/chunks-wikidot-5e.jsonl`): runs
   `discover_urls()` over the five real namespaces (spell, race, class, feat, equipment —
   monster/condition/rule dropped per Checkpoint D's finding), `fetch_page()` + `parse_page()` per
   URL, writes JSONL.
2. Run unmodified: `python ingestion/qa_chunks.py ingestion/chunks-wikidot-5e.jsonl` → `.clean.jsonl`.
3. Run unmodified: `python ingestion/embed.py --chunks ingestion/chunks-wikidot-5e.clean.jsonl`
   (now provenance-aware from Checkpoint B) → upserts into `dnd.chunks` with `book_slug=
   "wikidot-5e"`.
4. Add `--dedup-report` to `scrape_wikidot.py`: after crawl, query existing `dnd.chunks` for
   `(content_type, entity_name)` overlaps between `book_slug='wikidot-5e'` and the PDF book slugs,
   write counts (not filtering — see Non-Goals) to `ingestion/wikidot-dedup-report.json`. This is
   the plan's answer to the bead's "supplements rather than conflicts" AC: visibility now,
   filtering/reranking decisions deferred to the retrieval-eval initiative that follows this one.
Demo: `docker compose up -d vector-db && uv run --with '.[extract]' python ingestion/scrape_wikidot.py --out ingestion/chunks-wikidot-5e.jsonl && uv run python ingestion/qa_chunks.py ingestion/chunks-wikidot-5e.jsonl && uv run --with "psycopg[binary]" --with openai python ingestion/embed.py --chunks ingestion/chunks-wikidot-5e.clean.jsonl` then `psql ... -c "select book_slug, count(*) from dnd.chunks group by book_slug"` shows a new `wikidot-5e` row.

**Implementation-time finding (ran a 20-chunk sample crawl, `--limit 4`, through the real pipeline
end to end):** `qa_chunks.py`'s existing `MAX_CHUNK_CHARS=8000` length gate quarantined 8/20 (40%)
— every `race`/`class`/`equipment` page in the sample, none of the `spell`/`feat` pages. Those three
namespaces' pages are long, multi-topic (a full race's traits, a full class's level-by-level
progression, a full equipment category's item table run 9,000–42,000 chars), so Checkpoint C's
"one chunk per page" MVP decision (documented as a known limit in `parse_page`'s docstring) means
most of their content never reaches the corpus as things stand — the QA gate is working correctly,
not the source of the problem. `spell` and `feat` pages (already page-per-entity, matching the PDF
pipeline's granularity) are unaffected and embed cleanly. Not fixed in this checkpoint — filed as
`agent-forge-harness-4x66.8` (splitting race/class/equipment pages into per-section chunks, e.g. by
heading boundaries) rather than expanding this checkpoint's scope. The full production crawl (all
~880 pages, no `--limit`) should be run once that follow-up lands, or accepted now with the
understanding that most race/class/equipment content will be quarantined until it does — `spell`
(574 pages) and `feat` (199 pages) alone still meaningfully widen the corpus either way.

### Checkpoint F — Attribution in citations + licensing doc (bead AC2)
Steps:
1. In `service/generate.py build_sources()`: when a chunk's `book_slug == "wikidot-5e"`, set
   `Source.book = "D&D 5e Wiki — dnd5e.wikidot.com (CC BY-SA 3.0)"` instead of the raw slug; PDF
   book slugs are unaffected.
2. Extend `service/tests/test_service.py`: a wikidot-sourced chunk produces the attributed
   `Source.book` string; a PDF-sourced chunk's `Source.book` is unchanged.
3. New `docs/licensing-wikidot-corpus.md` — mirrors the `x5bz.5`/`docs/invite-copy.md` precedent:
   records the CC BY-SA 3.0 sourcing decision, the attribution mechanism (Checkpoint F.1), and that
   this content stays in the closed corpus (not the SRD-only public tier). Satisfies bead AC2
   ("documented in docs/... before any scraped content ships to testers").
Demo: `uv run --with '.[test]' python -m pytest service/tests/test_service.py -k source -q`

### Checkpoint G — Eval re-run vs baseline (bead AC4, closes the loop)
Steps:
1. `DATABASE_URL=... PYTHONUTF8=1 uv run --with "psycopg[binary]" --with openai python
   ingestion/eval_golden.py` against the expanded corpus, on the **existing, unregenerated** golden
   set (`GOLDEN_SET` in `eval_golden.py`, currently 179 entries) — regenerating would sample new
   wikidot entities and invalidate the before/after comparison the AC asks for.
2. Compare Hit@1/MRR/Recall@10 to the recorded baseline (74.7% / 0.808 / 91.0% @ 8851 chunks,
   `wsq`). Record the new numbers in the ship report.
3. Spot-check generation quality: `uv run --with '.[eval]' python ingestion/eval_answers.py
   --limit 10`.
4. If retrieval regresses, that's a stop-and-fix signal before shipping — not a deferred item —
   consistent with the bead's AC4 wording ("shows improvement or **no regression**").
Demo: `DATABASE_URL=... PYTHONUTF8=1 uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py` — user sees the live Precision/Hit@1/Recall table next to the baseline.

**Results (real run, full production crawl, not a sample):** ran the actual local dev corpus
(the `game-guide-ai_pgvector_data` volume, 9,067 PDF chunks — confirmed matching the recorded
corpus size) through the full pipeline: all 880 pages across the 5 real namespaces crawled, 849/880
passed `qa_chunks.py` (96.5%; the length-quarantine finding from Checkpoint E accounts for most of
the 31 quarantined), embedded into `dnd.chunks` as `book_slug=wikidot-5e`. `dedup_report()`: 422/849
(50%) overlap an existing PDF entity — expected, and left untouched (visibility only).

`eval_golden.py` on the same 179-query golden set, immediately before vs. after (a fresher, more
rigorous baseline than the historical `wsq` number, which was recorded against a different corpus
state — see below):

| Metric | Before (9,067 chunks) | After (9,916 chunks) |
|--------|------------------------|------------------------|
| Hit@1 | 82.8% | 81.6% |
| MRR | 0.866 | 0.854 |
| Recall@10 | 93.1% | 92.0% |
| Precision@5 | 43.6% | **47.4%** (improved) |
| Negatives correctly refused | 5/5 | 3/5 |

Both before and after are well above the historical `wsq` baseline (74.7% / 0.808 / 91.0% @ 8,851
chunks) — the corpus has grown/improved since that number was recorded, independent of this
feature.

The small aggregate dip traces to exactly 4 of 179 golden-set entries, each root-caused, not
hand-waved:
- **Water Genasi / Earth Genasi** (Hit@1 flips): the wikidot chunk ties the PDF chunk almost
  exactly on cosine distance (0.297 vs 0.292; 0.267 vs 0.266) but is tagged `content_type=
  race_feature` — the *semantically correct* tag per the taxonomy. `golden_set.json` expects
  `rule`, inherited from a pre-existing PDF-extraction tagging quirk this feature didn't create.
- **Spelljamming / Strixhaven Mascot** (negative-query flips): both were "unanswerable" only
  because the corpus lacked that content before. Real `Create Spelljamming Helm` / `Strixhaven
  Mascot` wikidot pages now exist, so the corpus genuinely can partially answer them — the golden
  set's "unanswerable" label is stale, not wrong-at-the-time.
- The other 3 negative queries (THAC0, Pokémon, starship piloting) are byte-for-byte unchanged —
  wikidot content did not leak into unrelated territory.

Decision (confirmed with the user): accept this as **no real regression** — filed
`agent-forge-harness-4x66.9` to refresh the 4 affected golden-set entries as a separate follow-up,
rather than blocking this feature on an eval-fixture staleness issue the feature itself exposed by
legitimately improving corpus coverage and tagging accuracy.

`eval_answers.py --limit 10` (6 cases, answer-quality spot-check): `answer_relevancy` 100%,
`faithfulness` 83% (both healthy); `answer_correctness`/`context_precision`/`context_recall` scored
poorly, but `docs/observability/answer-eval.md` itself documents that `answer_correctness` needs
"~20–30 well-reviewed cases" to be meaningful — a 6-case spot-check is below that threshold by
design (it's explicitly scoped as a spot-check, not the AC4 gate) and inspecting the actual
generated answers (e.g. the Invisibility spell case) showed complete, correct text despite a low
Ragas score. Not treated as a signal either way; `eval_golden.py` above is the load-bearing
evidence for AC4.

## Files to Create / Modify

| File | Create/Modify | Purpose |
|------|---------------|---------|
| `vector-db/init/03a-corpus-provenance.sql` | Create | Idempotent provenance-column migration |
| `tests/test_corpus_schema.py` | Create | Behavioral test of the migration |
| `scripts/bootstrap-db.sh` | Modify | Add the new file to `FILES` so fresh Cloud SQL bootstraps include it |
| `tests/test_bootstrap_db.py` | Modify | `EXPECTED_ORDER` must include the new file |
| `ingestion/scrape_wikidot.py` | Create | Parser + cached/rate-limited fetcher + CLI |
| `ingestion/tests/test_scrape_wikidot.py` | Create | Pure parser + cache-hit tests |
| `ingestion/tests/test_embed.py` | Create | `_row_for_upsert()` default-fill test |
| `docs/licensing-wikidot-corpus.md` | Create | CC BY-SA sourcing decision + attribution record |
| `ingestion/embed.py` | Modify | New columns in `_UPSERT_SQL`; extract `_row_for_upsert()` |
| `ingestion/extract.py` | Modify | `DndChunk` gains defaulted provenance fields |
| `ingestion/extract_scan.py` | Modify | `DndChunk` gains defaulted provenance fields |
| `service/generate.py` | Modify | `build_sources()` attribution special-case |
| `service/tests/test_service.py` | Modify | Attribution regression test |
| `ingestion/chunks-wikidot-5e.jsonl` (+`.clean`/`.quarantine`/`.qa.json`) | Create (data) | Crawl output, same convention as PDF chunk files |
| `ingestion/wikidot-dedup-report.json` | Create (data) | Non-blocking overlap visibility report |

## Validation Commands

```bash
uv run --frozen --no-sync ruff check .
uv run --frozen --no-sync mypy          # scoped to service/ + config.py — ingestion/ untyped by CI, keep hints honest anyway
uv run --frozen --no-sync python -m pytest -q --cov
DATABASE_URL=postgresql://rag:rag_dev_change_me@localhost:5433/game_guide_ai PYTHONUTF8=1 \
    uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py
```

## Beads Issue Map

| Beads ID | Type | Title | Depends on | Priority |
|----------|------|-------|-----------|----------|
| agent-forge-harness-4x66 | feature | [rag-chat] Expand D&D corpus with dnd5e.wikidot.com (CC BY-SA) | — (existing) | P2 |
| agent-forge-harness-4x66.1 | task | Checkpoint A — corpus schema migration (provenance columns) | 4x66 | P1 |
| agent-forge-harness-4x66.2 | task | Checkpoint B — embed.py/DndChunk provenance plumbing | 4x66.1 | P2 |
| agent-forge-harness-4x66.3 | task | Checkpoint C — wikidot page parser (tracer bullet) | 4x66.2 | P2 |
| agent-forge-harness-4x66.4 | task | Checkpoint D — rate-limited cached fetch layer | 4x66.3 | P2 |
| agent-forge-harness-4x66.5 | task | Checkpoint E — full crawl → QA → embed + dedup report | 4x66.4 | P2 |
| agent-forge-harness-4x66.6 | task | Checkpoint F — citation attribution + licensing doc | 4x66.5 | P2 |
| agent-forge-harness-4x66.7 | task | Checkpoint G — eval re-run vs baseline | 4x66.6 | P2 |

## Constraints Carried Forward From Research (unchanged)

- Closed corpus only — no SRD-only public tier work in this feature.
- Core reference namespaces only — no forums/talk/homebrew pages.
- CC BY-SA 3.0 attribution + share-alike honored (Checkpoint F).
- Roll20 stays out of scope (`agent-forge-harness-h310` tracks it separately).

## Non-Goals (this plan, in addition to research's)

- No automated exclusion/filtering of wiki chunks that overlap existing PDF entities — Checkpoint
  E ships a visibility report only. Filtering/reranking policy is explicitly deferred to the
  retrieval-eval review that follows this initiative.
- No automatic migration-on-startup for `dnd.chunks` (mirroring `ensure_schema()` for chat/auth) —
  out of scope; the one-off manual apply in Checkpoint A is judged sufficient for a two-database
  (local + one Cloud SQL pilot) footprint. Revisit if a third environment appears.
- Applying the migration to the live Cloud SQL pilot DB is a deploy-time step, not exercised by
  this plan's automated tests (no CI access to the pilot DB) — tracked explicitly so it isn't
  missed at ship time.

## Estimated Scope
- Files: 6 new / 5 modified (+3 generated data files); Complexity: Medium; Checkpoints: 7 (A–G).

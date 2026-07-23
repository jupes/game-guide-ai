# Ingestion — corpus pipeline, retrieval core, eval harness

Everything that turns D&D 5e PDFs into an embedded, queryable corpus — and everything that
measures how well querying it works. Three concerns live here:

1. **Offline pipeline** — PDF → typed chunks → QA gate → embeddings in pgvector.
2. **Retrieval core** — the query→chunks logic shared by the live service and the evals.
3. **Eval harness** — retrieval quality, answer quality, model A/Bs, metrics summaries.

The live service (`service/`) imports from here (`from ingestion.retrieval import ...`);
nothing here imports from `service/`. Tuning knobs (TOP_K, distance gates, …) live in the
top-level [`config.py`](../config.py), env-overridable via `RAG_*`.

## Offline pipeline

```text
PDF ──▶ extract_scan.py / extract.py ──▶ chunks-<slug>.jsonl
              │  (ocr_normalize.py cleans PHB garble inline)
              ▼
        qa_chunks.py  ──▶ chunks-<slug>.clean.jsonl      (embedded)
              │       ──▶ chunks-<slug>.quarantine.jsonl (never embedded)
              ▼       ──▶ chunks-<slug>.qa.json          (pass-rate report)
         embed.py  ──▶ dnd.chunks (Postgres + pgvector, 1536-d)
```

| Module | Role |
| --- | --- |
| `extract.py` | Font-driven extractor for **born-digital** PDFs (crisp heading sizes; pdfplumber, column-aware). |
| `extract_scan.py` | Structure-driven extractor for **OCR scans** — anchors on text patterns (monsters: `Armor Class N`; magic items: the rarity line; spells: level/school line with Casting-Time fallback) instead of font sizes, which drift in scans. Per-book config selects `monster_manual` / `dmg` / `supplement` extractors. |
| `ocr_normalize.py` | Rule-based repair of the PHB scan's OCR garble (`Vou`→`You`, `leveI`→`level`), plus a vocabulary-checked `l`→`t` repair pass (`aclion`→`action`) gated to `book='phb-5e'`. A deliberate no-op on the other, clean books. |
| `build_vocab.py` | Regenerates `vocab_5e.txt` (the l→t repair vocabulary) from the *non-PHB* clean chunk files. The output is checked in. |
| `qa_chunks.py` | **Pre-embedding QA gate**: quarantines known failure signatures (CID-font markers, Private-Use-Area glyphs, low-alpha text, junk entity names) so garbage never gets embedded. Also hosts the corpus-wide `detect_collapse` regression guard (`--collapse-check [--from-db]`). |
| `embed.py` | Embeds clean chunks (`text-embedding-3-small` by default; Ollama backend optional) and upserts into `dnd.chunks`. `--replace-book` deletes a book's rows first so re-extraction can't orphan stale chunks. |
| `ingest_books.py` | Orchestrates extract → QA → embed for the whole book list (or `--only <slugs>`, `--no-embed`), printing per-book QA pass rates. |

### Running it (from the repo root)

```bash
docker compose up -d vector-db     # DB must be up; .env needs OPENAI_API_KEY

# Whole corpus (or --only xge-5e tce-5e, or --no-embed for extract+QA only):
uv run --with '.[extract]' --with pdfplumber --with "psycopg[binary]" --with openai \
    python ingestion/ingest_books.py

# One book, step by step:
uv run --with '.[extract]' python ingestion/extract_scan.py "<pdf>" --book-slug xge-5e --out ingestion/chunks-xge-5e.jsonl
uv run python ingestion/qa_chunks.py ingestion/chunks-xge-5e.jsonl
uv run --with "psycopg[binary]" --with openai python ingestion/embed.py --chunks ingestion/chunks-xge-5e.clean.jsonl --replace-book
```

## Retrieval core (shared with the service)

| Module | Role |
| --- | --- |
| `retrieval.py` | `RagRetriever` — the single retrieval brain. Loads the corpus vocabulary once, then per query: embed → detect class/entity/content-type hints against the vocabulary → filtered cosine kNN over `dnd.chunks` → fetch **full** chunk text by id (rows only carry a 120-char preview) → judge answerability by top-1 distance (`KOZ_ANSWERABLE_DISTANCE`, 0.50). Also exposes the granular stage methods (`embed` / `analyze` / `search` / `fetch`) the service's LangGraph nodes call individually. |
| `scope.py` | Pure leaf module: chat mode → `(effective_ctypes, allowed_books)` filters. `spell` restricts to spell chunks in spell-bearing books; `rules` to a rules-ctype allowlist; `gm` folds in monster/dm_guidance/magic_item; `sage` is unscoped. |
| `rerank.py` | Content-type-**gated** cross-encoder reranker (`should_rerank`): rerank prose categories (rule/feat/dm_guidance, +6pt Hit@1), skip structured ones (net-negative on monsters). Model lazy-loads; needs the `[rerank]` extra. Off in the service unless `RAG_RERANK=1`. |

Retrieval hygiene worth knowing before you "fix" something:

- **Generic-entity stoplist + ≥3-char entity floor** (`retrieval.py`) — OCR noise and stat-block
  field labels ("Combat", a bare "I") used to become entity filters and over-restrict search.
  This is the *production* fix for filter over-restriction.
- **`IPL_FALLBACK_DISTANCE` is eval-only.** The filtered→unfiltered retry
  (`eval_golden.py --ipl-fallback`) was A/B'd net-harmful; the live service never uses it.
- **Hybrid search (vector+FTS RRF) exists but is not adopted** — it tied pure vector on Hit@1 and
  slightly lost Recall@10 (3q3). Pure filtered vector is the production mode.

## Eval harness

| Module | What it measures |
| --- | --- |
| `gen_golden.py` | Regenerates `golden_set.json` by sampling real `entity_name` rows per (content_type, book) — expected labels can't drift from the corpus. Hand-curated collisions + negatives live in `eval_golden.py` itself. |
| `eval_golden.py` | **Retrieval** quality on the golden set: Precision@K, Hit@1, Recall@10, per-query breakdown. `--mode vector|hybrid` A/Bs the search mode. |
| `eval_answers.py` | **Generation** quality: runs golden positives end-to-end through `RagService.answer`, scores with deterministic graders + Ragas LLM-judge, attaches scores to Langfuse traces. Needs the `[eval]` extra. See `docs/observability/answer-eval.md`. |
| `compare_models.py` | Model/version A/B with a fixed judge + **CI regression gate** (e.g. gpt-4o-mini vs a local Ollama model). See `docs/observability/eval-strategy.md`. |
| `metrics_summary.py` | Scriptable quality/cost and timestamped runtime summary via the Langfuse Metrics API, a stored-results fallback, or `runtime_metrics.sample.json`. See `docs/observability/dashboard.md`. |
| `spike_rerank.py` | Historical research spike that justified the gated reranker (kept for provenance). |

```bash
# Retrieval eval (live DB + OpenAI key; on Windows add PYTHONUTF8=1):
uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py

# Answer eval (small slice):
uv run --with '.[eval]' python ingestion/eval_answers.py --limit 5
```

## Data files

| File(s) | What |
| --- | --- |
| `chunks-<slug>.jsonl` | Raw extraction output, one `DndChunk` JSON per line. |
| `chunks-<slug>.clean.jsonl` / `.quarantine.jsonl` / `.qa.json` | QA gate outputs: what got embedded, what got quarantined, and the report. |
| `golden_set.json` / `golden_entities.json` | Generated eval queries; canonical entities the corpus must contain (guarded by `tests/test_golden_entities.py`). |
| `vocab_5e.txt` | 5e vocabulary for the PHB l→t OCR repair (regenerate via `build_vocab.py`). |
| `eval_results.json` / `eval_answers_results.json` / `compare_results.json` / `metrics_summary.json` | Latest stored eval outputs (also inputs to `spike_rerank.py` / `metrics_summary.py`). |

## Tests

Unit tests live in `ingestion/tests/` and are **pure** — no DB, PDF, or network; failure samples are
inlined. From the repo root:

```bash
uv run --with '.[test]' python -m pytest ingestion -q
```

## Gotchas

- **DB connection**: everything reads `DATABASE_URL` (default
  `postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai`), with `.env` auto-loaded.
  If host port 5432 is taken (e.g. another pgvector container), set `POSTGRES_PORT` and match it in
  `DATABASE_URL` — and make sure you're pointed at *this* project's DB, not a stale one.
- **Windows terminals**: set `PYTHONUTF8=1` (or `PYTHONIOENCODING=utf-8`) before the evals — chunk
  text contains non-cp1252 characters.
- **pymupdf is pinned exactly** (`==1.28.0`, the `[extract]` extra) — unpinned `uv run --with pymupdf`
  resolved different releases run-to-run and caused quarantine drift (wu1). Don't "upgrade" casually.
- Chunk-file regeneration is **deterministic per (PDF, extractor version)** — a noisy diff in a
  `chunks-*.jsonl` you didn't mean to touch usually means an extractor or pymupdf change.

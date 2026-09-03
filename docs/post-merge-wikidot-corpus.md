# Post-merge steps — dnd5e.wikidot.com corpus expansion

Operator runbook for after [PR #53](https://github.com/jupes/game-guide-ai/pull/53)
(`agent-forge-harness-4x66`) merges to `master`. Everything in the PR was built and verified
against the **local dev DB only** — none of it reaches the live pilot automatically. This is the
same shape as the original 12-PDF-book corpus: ingestion has always been a manual, one-time step
here, not something a code deploy performs.

## What happens automatically on merge

Nothing DB-related. CI's `deploy` job (`.github/workflows/ci.yml`) rebuilds and redeploys the
Cloud Run **service image** — the code changes (`_book_label()` attribution, provenance-aware
`embed.py`, etc.) go live the moment the merge commit's pipeline goes green. `scripts/deploy.sh`
never touches the database; it only ships the container.

## What you need to do by hand

Two things, against the **pilot Cloud SQL instance**, in order. Both use the same `$PROXY` DSN
pattern as [`deploy-gcp.md`](deploy-gcp.md).

### 0. Open the proxy (one terminal, leave it running)

```bash
export PROJECT=game-guide-ai-cloud
export REGION=us-central1
cloud-sql-proxy "$PROJECT:$REGION:game-guide-ai" --port 6543
```

In your working terminal:

```bash
export PROXY="postgresql://postgres:<PW>@localhost:6543/game_guide_ai"   # the real password, deploy-gcp.md §3
```

### 1. Apply the schema migration

`vector-db/init/03a-corpus-provenance.sql` (the `source_type`/`source_url`/`license` columns,
nullable `page_start`/`page_end`) has no auto-migration path — unlike `chat`/`auth`, nothing
re-applies `vector-db/init/` files to a database that already has data (`service/schema.py`'s
`ALL_SCHEMAS` only covers chat/auth). This is exactly what happened locally: I had to apply it by
hand there too.

**Recommended — re-run the full bootstrap script.** All five schema files (`01`–`03a`, `04`, `05`)
are written idempotently (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, etc. — see
`service/schema.py`'s own "must be idempotent and safe to re-run against a live database under
load" note), so re-running the same script the initial bootstrap used is safe against an
already-populated instance and needs no new tooling:

```bash
scripts/bootstrap-db.sh "$PROXY"
```

Equivalent, more surgical alternative — apply just the new file:

```bash
psql "$PROXY" -v ON_ERROR_STOP=1 -f vector-db/init/03a-corpus-provenance.sql
```

Verify:

```bash
psql "$PROXY" -c '\d dnd.chunks' | grep -E 'source_type|source_url|license'
```

### 2. Run the wikidot crawl + QA + embed against the pilot

Same three commands used locally during implementation, pointed at `$PROXY` instead of
`localhost`. This is a real crawl (~880 pages) — expect several minutes at the default 1
req/second rate limit.

```bash
uv run python ingestion/scrape_wikidot.py --out ingestion/chunks-wikidot-5e.jsonl   # reuses the already-committed JSONL's cache if you keep the same --cache-dir

uv run python ingestion/qa_chunks.py ingestion/chunks-wikidot-5e.jsonl

uv run --with "psycopg[binary]" --with openai python ingestion/embed.py \
  --chunks ingestion/chunks-wikidot-5e.clean.jsonl --dsn "$PROXY"
```

The `chunks-wikidot-5e.jsonl` / `.clean.jsonl` produced by the real crawl are already committed in
the PR (880 raw / 849 clean) — you can skip straight to the `embed.py` step and point it at the
already-committed `ingestion/chunks-wikidot-5e.clean.jsonl` instead of re-crawling:

```bash
uv run --with "psycopg[binary]" --with openai python ingestion/embed.py \
  --chunks ingestion/chunks-wikidot-5e.clean.jsonl --dsn "$PROXY"
```

### 3. Verify

```bash
psql "$PROXY" -c "select book_slug, source_type, count(*) from dnd.chunks group by 1,2 order by 3 desc;"
```

Expect a `wikidot-5e | wiki | 849` row alongside the 12 PDF book rows (pilot totals may differ
slightly from the local dev numbers in the ship report if the pilot corpus has since diverged).

## One thing worth confirming, not assuming

CI's `retrieval-metrics` gate (`docs/ci.md` §"The regression gate") compares a fresh
`eval_golden.py` run against **`EVAL_DATABASE_URL`** (a repo secret) to the **committed**
`ingestion/eval_results.json` — which this PR updated to reflect the post-wikidot *local* corpus
(Hit@1 81.6% / MRR 0.854 / Recall@10 92.0% @ 9,916 chunks; see the ship report for the full
before/after). Refreshing that baseline after a corpus re-ingest is the documented, correct
workflow (`docs/ci.md` line 69–72) — not a mistake — but it does mean: **if `EVAL_DATABASE_URL`
points at the pilot instance**, the gate will compare against a mismatched corpus state until step
2 above lands there too. I could not confirm from the repo alone whether `EVAL_DATABASE_URL` is
the same instance as the pilot or a separate dedicated eval DB — worth a quick check
(`gh secret list` requires admin; ask whoever set it up, or check for a second Cloud SQL instance)
before relying on the gate's verdict for the first merge after this one.

## Follow-ups already filed (not blockers, not part of this runbook)

- `agent-forge-harness-4x66.8` (P3) — race/class/equipment wikidot pages need per-section
  splitting (whole-page chunks blow past `qa_chunks.py`'s length gate; ~100% of a small sample
  quarantined, though the full 880-page crawl's overall pass rate was 96.5%).
- `agent-forge-harness-4x66.9` (P3) — refresh 4 golden-set entries this feature made stale.

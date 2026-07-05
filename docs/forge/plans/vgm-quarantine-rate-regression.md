# Plan: vgm-quarantine-rate-regression — VGM/Tortle QA quarantine rate jumped on re-extraction
Generated: 2026-07-03
Repo: game-guide-ai (repos/game-guide-ai)
Phase: plan (2/4) — from plans/research/vgm-quarantine-rate-regression.md

## Summary
`extract_scan.py`'s entity-name capture is too permissive: it assigns raw margin-quote lines,
random-encounter table rows, and garbled chapter dividers as `entity_name` whenever a bold line or
an unclaimed opening line is seen, with no check that the text actually looks like a name. Those
lines then fail `qa_chunks.entity_name_ok()` downstream and the **whole chunk** — including
otherwise-clean lore prose — gets quarantined. Re-extraction (drifting on an unpinned `pymupdf`)
produced more of these bad captures for VGM, dropping its clean-chunk count 1339→1156. The fix has
three parts: (1) stop capturing non-name-shaped lines as `entity_name` in `extract_scan.py`, reusing
the already-tested `entity_name_ok()` instead of duplicating heuristics; (2) add a salvage path in
`qa_chunks.py` so a chunk failing *only* `bad_entity` keeps its (otherwise clean) text with
`entity_name` nulled, rather than being discarded outright; (3) pin `pymupdf` to an exact version so
future re-extractions are reproducible. Then re-extract/re-QA/re-embed VGM + Tortle only and verify
with `eval_golden`/`eval_answers` (matching the agent-forge-harness-813 pattern). Tortle's
cipher-garbage quarantines are expected to stay quarantined — that's the QA gate working correctly.

## Existing Code to Reuse
- `ingestion/qa_chunks.entity_name_ok()` (`qa_chunks.py:100-122`) — already tested against exactly
  this class of garbage; reuse it as the gate inside `extract_scan.py`'s capture points instead of
  writing a second, divergent "looks like a name" heuristic.
- `ingestion/ingest_books.py` — already orchestrates extract → QA → embed with a `--only <slug>`
  flag; re-extraction for VGM/Tortle uses this, not a new script.
- `ingestion/eval_golden.py` / `eval_answers.py` — existing before/after regression gate, used
  identically for the 813 PHB fix.
- Test fixtures: extend `ingestion/test_extract_scan.py` (56 existing tests) and
  `ingestion/test_qa_chunks.py` (25 existing tests) rather than new test files, following the
  813 fix's own precedent of adding real-sample regression tests to existing suites.

## TDD Strategy (red-green-refactor)
Following `.claude/skills/tdd`. Behaviors are tested through public interfaces
(`extract_supplement_chunks`, `classify_chunk`/`run_qa`), vertically, using the real bad samples
pulled from `chunks-vgm-5e.quarantine.jsonl` / `chunks-tortle-5e.quarantine.jsonl` in the research
doc — not synthetic approximations.

| # | Behavior (as a spec) | Test file | Tracer? |
|---|----------------------|-----------|---------|
| 1 | A bold margin-quote line ("Well, I Know W I Am. -Volo") is not captured as `entity_name`; the chunk is still emitted with `entity_name=None` and the quote text intact | `ingestion/test_extract_scan.py` | yes |
| 2 | A random-encounter table row ("21-25 3D6 Hook Horrors") is not captured as `entity_name` | `ingestion/test_extract_scan.py` | no |
| 3 | A garbled/OCR chapter divider ("Ch Pter 1 :") is not captured as `entity_name` | `ingestion/test_extract_scan.py` | no |
| 4 | A genuine bold heading / real monster or spell name is still captured as `entity_name` (regression guard against existing fixtures) | `ingestion/test_extract_scan.py` | no |
| 5 | A prose block that never gets a name-shaped heading is still emitted as a chunk (`entity_name=None`), not silently dropped by `flush()` | `ingestion/test_extract_scan.py` | no |
| 6 | `classify_chunk`/`run_qa` salvages a chunk whose only failing reason is `bad_entity`: entity_name is nulled and the chunk is kept clean | `ingestion/test_qa_chunks.py` | no |
| 7 | A chunk failing `bad_entity` **and** another reason (e.g. `low_alpha`, mirroring Tortle's cipher-garbage samples) is NOT salvaged — it stays quarantined | `ingestion/test_qa_chunks.py` | no |
| 8 | `run_qa`'s report counts and clean-file output reflect the salvage (previously-quarantined bad_entity chunk now appears in `.clean.jsonl` with `entity_name: null`) | `ingestion/test_qa_chunks.py` | no |

Refactor watch-list (after green): confirm `entity_name_ok` import direction (qa_chunks →
extract_scan, one-way, no cycle); consider whether the new "unnamed prose block" path in
`extract_scan.py` needs a small helper to avoid duplicating the flush/emit logic between the named
and unnamed cases.

## Build Sequence & Checkpoints

### Checkpoint A — Guard entity-name capture in extract_scan.py
Steps:
1. Add regression tests (behaviors 1-5 above) to `ingestion/test_extract_scan.py` using the real
   bad samples from the research doc — red first.
2. Gate `is_heading()`'s bold branch and the "no open chunk yet" fallback
   (`extract_scan.py:737-740`, `:889-897`) on `qa_chunks.entity_name_ok()` before assigning a line
   as `entity_name`; a line that fails the check is treated as ordinary body text instead.
3. Relax `flush()`'s emit condition (`extract_scan.py:754-773`) so a chunk with accumulated body
   text but no captured name still emits with `entity_name=None`, instead of being silently
   dropped — guard `normalize_entity_name(cur_entity)` for the `None` case.
Demo: `uv run --with '.[test]' python -m pytest ingestion/test_extract_scan.py -q` — all new and
existing tests green.

### Checkpoint B — Salvage layer in qa_chunks.py (defense in depth)
Steps:
1. Add tests (behaviors 6-8 above) to `ingestion/test_qa_chunks.py` — red first.
2. Add a `salvage_entity_name(chunk) -> dict` pure helper and wire it into `run_qa`'s classify
   loop: when `classify_chunk` returns exactly `["bad_entity"]`, null `entity_name`, re-classify,
   and route to `clean` on success.
3. Confirm behavior 7 holds — multi-reason failures (Tortle's cipher-garbage class) are never
   salvaged.
Demo: `uv run --with '.[test]' python -m pytest ingestion/test_qa_chunks.py -q` — all new and
existing tests green.

### Checkpoint C — Pin pymupdf + update usage docs
Steps:
1. Add an `extract` optional-dependency group to `pyproject.toml` (mirrors the existing `eval`
   group pattern) with `pymupdf` pinned to an exact version; run `uv lock`.
2. Update usage docstrings/docs from `--with pymupdf` to `--with '.[extract]'`:
   `extract_scan.py:24-26`, `ingest_books.py:10-14`, `docs/ARCHITECTURE.md:156`,
   `docs/dnd-full-corpus-expansion-2026-06-08.md:170`.
Demo: `uv run --with '.[extract]' python -c "import fitz; print(fitz.pymupdf_version)"` prints the
same pinned version on repeated runs — reproducibility proof.

### Checkpoint D — Re-extract, re-QA, re-embed VGM + Tortle; verify no regression
Steps:
1. `uv run --with '.[extract]' --with "psycopg[binary]" --with openai python ingestion/ingest_books.py --only vgm-5e tortle-5e --no-embed`
   — regenerate `chunks-{vgm,tortle}-5e.jsonl` with the pinned engine and the fixed heuristics.
2. Inspect `chunks-vgm-5e.qa.json` / `chunks-tortle-5e.qa.json`: confirm VGM's clean-chunk count
   recovers meaningfully toward the pre-regression baseline (1339) and `bad_entity` drops sharply;
   confirm Tortle's cipher-garbage samples remain quarantined (unchanged story, not "fixed away").
3. Re-run `ingest_books.py --only vgm-5e tortle-5e` (with embed) to push clean chunks into
   `game_guide_ai@5433`.
4. Run `eval_golden.py` and `eval_answers.py` before/after; confirm no regression (same pattern as
   agent-forge-harness-813's AC #4).
Demo: side-by-side `chunks-vgm-5e.qa.json` pass_rate before/after, plus the `eval_golden.py`
before/after diff, shown to the user.

## Files to Create / Modify
| File | Create/Modify | Purpose |
|------|---------------|---------|
| `ingestion/extract_scan.py` | Modify | Gate entity-name capture on `entity_name_ok`; relax flush to emit unnamed prose blocks |
| `ingestion/test_extract_scan.py` | Modify | New regression tests using real VGM/Tortle bad samples |
| `ingestion/qa_chunks.py` | Modify | Add `salvage_entity_name` and wire into `run_qa` |
| `ingestion/test_qa_chunks.py` | Modify | New salvage + no-salvage-on-multi-reason tests |
| `pyproject.toml` | Modify | New `extract` optional-dependency group, pinned `pymupdf` |
| `uv.lock` | Modify (generated) | Lockfile update from `uv lock` |
| `ingestion/ingest_books.py` | Modify | Usage docstring: `--with pymupdf` → `--with '.[extract]'` |
| `docs/ARCHITECTURE.md` | Modify | Usage doc update |
| `docs/dnd-full-corpus-expansion-2026-06-08.md` | Modify | Usage doc update |
| `ingestion/chunks-vgm-5e.*`, `chunks-tortle-5e.*` | Regenerate (not hand-edited) | Re-extracted/re-QA'd artifacts |

## Validation Commands
```bash
uv run --with '.[test]' python -m pytest ingestion/test_extract_scan.py ingestion/test_qa_chunks.py -q
uv run --with '.[test]' python -m pytest -q   # whole suite — no regression
uv run --with '.[extract]' --with "psycopg[binary]" --with openai python ingestion/ingest_books.py --only vgm-5e tortle-5e
uv run python ingestion/qa_chunks.py ingestion/chunks-vgm-5e.jsonl
uv run python ingestion/qa_chunks.py ingestion/chunks-tortle-5e.jsonl
uv run python ingestion/eval_golden.py
uv run python ingestion/eval_answers.py
```

## Beads Issue Map
| Beads ID | Type | Title | Depends on | Priority |
|----------|------|-------|-----------|----------|
| agent-forge-harness-ay0 | feature | Fix VGM/Tortle bad_entity quarantine regression | 0qq, 3mp, 2p5, sc2 (related: agent-forge-harness-813) | P2 |
| agent-forge-harness-0qq | task | Guard entity-name capture in extract_scan.py | — | P2 |
| agent-forge-harness-3mp | task | Add qa_chunks salvage for bad_entity-only failures | 0qq | P2 |
| agent-forge-harness-2p5 | task | Pin pymupdf via new `extract` extra + update usage docs | — | P3 |
| agent-forge-harness-sc2 | task | Re-extract/re-QA/re-embed VGM + Tortle; verify eval_golden/eval_answers | 0qq, 3mp, 2p5 | P2 |

Originating bead agent-forge-harness-wu1 closes when agent-forge-harness-sc2 completes.

**Plan review:** SOUND after 1 review turn (0 Blocker / 0 High / 1 Medium — cosmetic line-range fix
applied, logged as a comment on agent-forge-harness-ay0).

## Estimated Scope
- Files: 0 new / 9 modified (+2 regenerated artifact pairs); Complexity: Medium; Checkpoints: 4

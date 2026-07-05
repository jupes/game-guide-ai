# Ship Report — VGM/Tortle QA quarantine rate jumped on re-extraction

> **Slug**: `vgm-quarantine-rate-regression` · **Bead**: agent-forge-harness-ay0 (originating:
> agent-forge-harness-wu1; children 0qq/3mp/2p5/sc2) · Follow-up to agent-forge-harness-813
> **Date**: 2026-07-05 · **Repo**: `repos/game-guide-ai` (branch `fix/vgm-quarantine-rate-regression`)
> **Pipeline**: Forge full (research → plan → plan-review → implement → ship)

---

## What shipped

`extract_scan.py`'s entity-name capture was too permissive: any bold line (or any line seen before a
chunk was open) could become `entity_name` with no check that it actually looked like a name. Margin
quotes ("Well, I Know W I Am. -Volo"), random-encounter table rows ("21-25 3D6 Hook Horrors"), and
garbled chapter dividers ("Ch Pter 1 :") were all being captured this way — and `qa_chunks`'
`entity_name_ok()` then quarantined the **whole chunk**, discarding otherwise-clean lore/rule text
along with the bad name. Re-extracting VGM during the agent-forge-harness-813 PHB fix (on an
unpinned `pymupdf`, which silently drifted between runs) made this much worse, dropping VGM's clean
chunk count from 1339 to 1156.

The fix has three parts: (1) `extract_scan.py` now gates both capture paths on the already-tested
`entity_name_ok()`, reusing it instead of duplicating the heuristic, and `flush()` was relaxed so a
prose block that never earns a real name still emits with `entity_name=None` instead of being
silently dropped; (2) `qa_chunks.py` gained a `salvage_entity_name()` defense-in-depth layer for any
chunk that still fails *only* `bad_entity`; (3) `pymupdf` is now pinned to an exact version
(`1.28.0`) via a new `extract` optional-dependency group, so future re-extractions are reproducible.
VGM and Tortle were then re-extracted, re-QA'd, and re-embedded.

---

## Before / after

| Area | Before | After |
|------|--------|-------|
| VGM QA pass rate | 1156/1531 clean (75.5%), `bad_entity`=356 | **1306/1329 clean (98.3%)**, `bad_entity`=0 |
| Tortle QA pass rate | 72/97 clean (74.2%), `bad_entity`=19 | **91/97 clean (93.8%)**, `bad_entity`=0 |
| Remaining quarantine reasons | `bad_entity` + `low_alpha` | `low_alpha` only (legitimate numeric stat fragments) |
| Tortle CID-font cipher garbage | present (old pipeline wrongly served some as clean) | **no longer reproduces** — pinning pymupdf 1.28.0 resolved the decode issue as a side effect |
| `pymupdf` version across extraction runs | unpinned (`uv` could resolve any version — 2 different cached releases already found on disk) | pinned to `1.28.0` via `pyproject.toml`'s new `extract` extra |
| `qa_chunks` handling of a chunk with a bad name but clean body | whole chunk discarded | `entity_name` nulled, chunk kept clean |

The zero `bad_entity` count on both books means the extraction fix alone (layer 1) was sufficient —
the `qa_chunks` salvage layer (layer 2) never had to fire in practice, but stays in place as
defense-in-depth for any future extractor that captures a bad name.

## Eval (retrieval + generation)

`eval_golden.py` (179-query suite) before/after re-embedding VGM + Tortle:

| Metric | Before | After |
|--------|--------|-------|
| Hit@1 (positives) | 144/174 (82.8%) | 144/174 (82.8%) |
| Precision@5 | 43.4% | 43.6% |
| MRR | 0.868 | 0.866 |
| Recall@10 | 163/174 (93.7%) | 162/174 (93.1%) |
| monster category Hit@1 | 33/41 | 33/41 |
| Negatives correctly refused | 5/5 | 5/5 |

No retrieval regression — every delta is within normal run-to-run noise.

`eval_answers.py` (6 curated cases, Ragas judge): only an **after** snapshot was captured — the DB
had already been replaced by the time this ran, so there's no valid before/after pair for this
script specifically. As a spot check, all 6 generated answers are substantively correct, including
"What is a Froghemoth?" (VGM-sourced) and "What is a Beholder Zombie?" — `key_fact_hits` pass for
every case. The low `context_precision`/`context_recall` Ragas scores (0.0 on several cases,
including the PHB-only "Invisibility spell" question this fix never touched) reproduce identically
on content unrelated to this change, so they reflect a pre-existing Ragas metric-configuration gap
rather than a regression here.

---

## How to verify

```bash
cd repos/game-guide-ai
uv run --with '.[test]' python -m pytest ingestion/test_extract_scan.py ingestion/test_qa_chunks.py -q
# expect: 61 passed (test_extract_scan.py), 31 passed (test_qa_chunks.py)

uv run --with '.[extract]' python -c "import fitz; print(fitz.pymupdf_version)"
# expect: 1.28.0, identically on repeated runs

cat ingestion/chunks-vgm-5e.qa.json ingestion/chunks-tortle-5e.qa.json
# expect: reasons contain only "low_alpha", no "bad_entity"

PYTHONIOENCODING=utf-8 DATABASE_URL="postgresql://rag:rag_dev_change_me@localhost:5433/game_guide_ai" \
  uv run python ingestion/eval_golden.py
# expect: Hit@1 82.8%, monster category 33/41 — matches the "after" row above
```

---

## Work done

- **Checkpoint A** — guard entity-name capture in `extract_scan.py` (`aeaed23`)
- **Checkpoint B** — `qa_chunks` salvage for bad_entity-only failures (`98e62af`)
- **Checkpoint C** — pin `pymupdf` via new `extract` extra + update usage docs (`b4e37e0`)
- **Checkpoint D** — re-extract/re-QA/re-embed VGM + Tortle; verify eval_golden/eval_answers (`7061280`)

## Decisions / deviations

- **Plan-review Medium fix applied**: `ingest_books.py` usage-docstring line reference corrected
  from `9-13` to `10-14` before implementation (see the plan-review report).
- **Tortle CID-font follow-up dropped**: the plan expected Tortle's cipher-garbage quarantines to
  remain (a separate, out-of-scope CID-font decode bug). Empirically, pinning `pymupdf` to 1.28.0
  fixed that decode issue too — no follow-up bead was needed.
- **`uv.lock` not committed**: it's gitignored in this repo by design; the exact `pymupdf==1.28.0`
  pin in `pyproject.toml` is what guarantees reproducibility, independent of lockfile tracking.
- **Scope held at VGM + Tortle only**, as planned. The other 8 fitz-engine supplement books
  (xge-5e, tce-5e, phb-5e, mtf-5e, eepc-5e, scag-5e, eberron-5e, ravnica-5e) share
  `extract_supplement_chunks()` and are plausibly affected by the same entity-capture gap — worth a
  follow-up audit, not filed as a bead yet (see Follow-ups below).

## Follow-ups / known gaps

- Audit the other 8 fitz-engine supplement books for the same entity-capture issue this fix
  addressed for VGM/Tortle (not filed as a Beads issue yet — raise if pursued).
- `eval_answers.py`'s `context_precision`/`context_recall` Ragas metrics read as 0.0 across the board
  (including on content unrelated to this fix) — likely a pre-existing metric-configuration gap
  worth investigating separately, not related to this regression.

## Beads

Closed: agent-forge-harness-0qq, agent-forge-harness-3mp, agent-forge-harness-2p5,
agent-forge-harness-sc2. Feature agent-forge-harness-ay0 and originating bead
agent-forge-harness-wu1 close with this report.

## Quality gates

61 `test_extract_scan.py` + 31 `test_qa_chunks.py` (92 new/changed) green; full repo suite
288/288 green (`uv run --with '.[test]' python -m pytest -q`). `eval_golden.py` before/after shows
no regression. Corpus re-embedded for vgm-5e and tortle-5e (`--replace-book`, no duplicates).
Pushed to `game-guide-ai` as PR (see PR link once opened).

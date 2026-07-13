# Plan Review: vgm-quarantine-rate-regression — VGM/Tortle QA quarantine rate jumped on re-extraction
Source: plans/drafts/vgm-quarantine-rate-regression.md · Reviewed: 2026-07-03

## Verdict: SOUND — 0 Blocker / 0 High / 1 Medium / 0 Low

The plan's core technical claims check out against the real codebase. One minor documentation
reference needed adjustment; all load-bearing assertions were accurate and grounded in code.

## Findings

### [Medium] Checkpoint C docstring line reference is slightly misaligned — Checkpoint C, step 2
**What:** The plan stated usage docstrings to update in `ingest_books.py:9-13`, but the actual usage
block spans lines 10-14 (line 9 is blank).
**Why it's an issue:** A minor issue when manually implementing — the implementer would need to
adjust the line range slightly, though context makes it obvious which docstring to update.
**Evidence:** `ingest_books.py:10-14` contains the usage docstring starting with "Usage:" —
`ingest_books.py:9` is blank. — Confirmed
**Suggested correction:** Reference `ingest_books.py:10-14` instead of `9-13`.
**Outcome:** Fixed in the plan before implementation; applied directly during implement.

## Verified as accurate (spot-checks)
- `entity_name_ok()` definition at `qa_chunks.py:100-122` ✓
- `is_heading()` definition at `extract_scan.py:737-740` ✓
- `flush()` definition at `extract_scan.py:754-773` ✓
- "No open chunk yet" fallback at `extract_scan.py:889-895` ✓
- `--only` CLI flag uses `nargs="*"` (space-separated args) at `ingest_books.py:78` ✓
- `flush()` required `cur_entity and cur_lines and cur_start` all truthy (line 756), so relaxing this
  gate was a real, necessary change ✓
- `normalize_entity_name()` assumes string input (calls `.strip()`), so guarding for `None` was
  necessary ✓
- Test counts: `test_extract_scan.py` had 56 tests, `test_qa_chunks.py` had 25 tests ✓
- Optional-dependency groups `test` and `eval` existed in `pyproject.toml`; `extract` group did not
  yet exist (created during implement) ✓
- No circular import risk: `qa_chunks.py` had no imports of `extract_scan`, and vice versa ✓
- Pymupdf was not pinned in `pyproject.toml` or `uv.lock` prior to this work ✓
- Usage docs existed at the referenced paths in `docs/ARCHITECTURE.md` and
  `docs/dnd-full-corpus-expansion-2026-06-08.md` ✓
- Research document constraints were consistent with plan scope (VGM + Tortle only; Tortle
  cipher-garbage assumed to remain quarantined) ✓

## Not verified at review time (later confirmed empirically during implement)
- Whether the exact quarantine samples from the research doc existed in
  `chunks-vgm-5e.quarantine.jsonl` / `chunks-tortle-5e.quarantine.jsonl` — confirmed present at
  implement time, before the fix.
- The plan's assumption that Tortle's cipher-garbage chunks would **remain** quarantined after the
  fix — this did NOT hold: re-extraction under the pinned pymupdf (1.28.0) resolved the underlying
  CID-font decode issue as a side effect, so Tortle's genuine cipher-garbage no longer reproduces at
  all. See the ship report for detail; no separate CID-decode follow-up was needed after all.

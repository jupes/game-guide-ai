# Research: vgm-quarantine-rate-regression — VGM/Tortle QA quarantine rate jumped on re-extraction
Generated: 2026-07-03
Repo: game-guide-ai (repos/game-guide-ai)
Beads issue: agent-forge-harness-wu1
Phase: research (1/4)

## Goal
While re-ingesting for the PHB t->l OCR fix (agent-forge-harness-813), re-extracting VGM (Volo's
Guide to Monsters) yielded a much higher `bad_entity` QA-quarantine rate (clean chunks dropped
1339→1156, 75.5% pass) despite the extractor code itself not changing for VGM. Tortle showed a
similar drop (90→72, 74.2%). `eval_golden`'s monster category is unchanged (33/41 both times), so
there's no measured retrieval regression yet, but ~200 VGM chunks of real lore/prose content are
being needlessly discarded. This phase determines root cause and the fix's scope before planning.

## What the Code Says (answered by exploration)

### The QA gate and what it rejects
- `bad_entity` triggers `entity_name_ok()` returning `False` — `ingestion/qa_chunks.py:100-122`.
  A chunk with any non-null `entity_name` that's overlong, ends in a period, has too many words,
  or is mostly non-alphabetic gets its **entire chunk** (not just the field) quarantined —
  `classify_chunk()`, `qa_chunks.py:170-189`.
- Live proof, pulled straight from the checked-in artifacts (`ingestion/chunks-vgm-5e.qa.json`,
  `chunks-vgm-5e.quarantine.jsonl`, `chunks-tortle-5e.qa.json`):
  - VGM: total 1531, clean 1156, quarantined 375 (`bad_entity`=356, `low_alpha`=39).
  - Tortle: total 97, clean 72, quarantined 25 (`bad_entity`=19, `low_alpha`=6).
  - **Every one of the 375 quarantined VGM chunks has a non-null `entity_name`.** Real samples:
    `'The Best One.'`, `'21-25 3D6 Hook Horrors'`, `'Well, I Know W I Am. -Volo'`,
    `'Ch Pter 1 :'`, `'Grolantor: Always Huncry , Never Fu Ll'`, `'T11Riup Pie-Auc.H �'`. All
    are `content_type="rule"`. None are real entity names — they're margin-quote openers
    (`-Volo` is Volothamp Geddarm, VGM's in-world author), random-encounter table rows, and
    chapter/section dividers.

### Where the bad entity_name comes from (extract_scan.py)
`extract_supplement_chunks()` (`ingestion/extract_scan.py:694-901`) is the shared extractor for
every "supplement"-kind fitz-engine book (`phb-5e`, `xge-5e`, `tce-5e`, `vgm-5e`, `mtf-5e`,
`eepc-5e`, `scag-5e`, `tortle-5e`, `eberron-5e`, `ravnica-5e` — `extract_scan.py:111-130`). Two
capture paths feed `entity_name` without any real name-shaped check:
- `is_heading()` (`extract_scan.py:737-740`): fires on **any bold line, 3-60 chars, containing a
  letter** — no size floor, no sentence-shape check. If pymupdf reports a margin quote or chapter
  divider as bold (font-flag interpretation is a pymupdf implementation detail), the raw line text
  becomes `cur_entity` (`extract_scan.py:880-887`).
  - Bold text on the same figure is captured verbatim: `flush(li.page); cur_entity = li.text` at
    `extract_scan.py:885`.
- The "no open chunk yet" fallback (`extract_scan.py:889-895`): explicitly designed to rescue a
  spell name that precedes its level line with no heading in between, but it fires on **any** line
  when no chunk is currently open — including the first line of a margin quote, table row, or
  divider that happens to follow a flush.
- Neither path validates that the captured text looks like a name (short, no terminal period, few
  words) before assigning it to `entity_name` — that validation only happens later, downstream, in
  `qa_chunks.entity_name_ok()`, and by then the whole chunk is thrown away rather than the bad
  field alone.

### Extraction is unpinned — a live reproducibility gap
- Every invocation of the fitz extractor is documented as `uv run --with pymupdf ...`
  (`extract_scan.py:24-26`, `ingestion/ingest_books.py:11`, `docs/ARCHITECTURE.md:156`,
  `docs/dnd-full-corpus-expansion-2026-06-08.md:170`) — **pymupdf is never pinned** in
  `pyproject.toml` or `uv.lock` (grepped both, zero hits).
  - `uv run --with pymupdf` resolves whatever pymupdf release satisfies no constraint at run time,
    so successive extraction runs are not guaranteed to use the same version.
  - Confirmed on disk: the local `uv` cache (`%LOCALAPPDATA%\uv\cache\archive-v0\`) has **two
    different pymupdf releases already cached** — `1.27.2.3` and `1.28.0`. Both have been resolved
    on this machine at different times, which is direct, non-speculative evidence that the
    unpinned dependency does drift across runs.
  - The `�` (Unicode replacement char) literally present in a VGM `bad_entity` sample
    (`'T11Riup Pie-Auc.H �'`) is consistent with a text-decoding difference between pymupdf
    versions, not a code change (VGM/Tortle extraction logic was untouched by the PHB t->l fix).
- Test coverage gap: `ingestion/test_extract_scan.py` (56 tests) exercises `extract_supplement_chunks`
  and the span-grouping helpers entirely against **synthetic `LineItem`/span tuples**, never against
  a real PDF through real pymupdf. A pymupdf version change that alters bold-flag reporting, size
  rounding, or text decoding on real PDFs would not be caught by this suite — consistent with the
  regression only surfacing on an actual re-extraction run, not a code diff.

### Tortle's regression is a different (and correct) story
- Tortle's newly-quarantined `bad_entity`/`low_alpha` samples are genuine **cipher-garbage** —
  e.g. `'[j]Yk]kZq*$Yf\\qgmjOak\\gek[gj]af['`, `'L\`][d]ja[g^MeZ]jd]]mk]\\l\`akZYkaf'` — a
  character-substitution pattern consistent with Tortle's known **undecoded CID custom font**
  (flagged in `extract_scan.py`'s own module docstring: "Tortle's CID fonts"). The bead's own
  description confirms this: the old pipeline was **wrongly serving this cipher garbage as
  clean**; the new extraction correctly quarantines it. This is not the same bug as VGM's
  false-positive margin-quote capture, and salvaging it via `qa_chunks` (nulling `entity_name`)
  would not help — the corruption is in the body text itself, not just the entity-name field.
  Forcing these back to "clean" would reintroduce the exact class of bug the fitz-engine adoption
  was meant to fix.

### Existing test coverage to reuse
- `ingestion/test_qa_chunks.py` (25 tests) already covers `entity_name_ok()` against exact classes
  of garbage (`test_entity_name_rejects_sentence_fragments`, `_rejects_field_and_section_labels`,
  `_rejects_ocr_garbage`) — the fix's new "salvage" path and the tightened capture heuristic should
  extend this file and `test_extract_scan.py` with the real VGM/Tortle samples pulled above as
  regression fixtures, per repo convention (`ingestion/test_ocr_normalize.py` did the same with the
  real GREATER INVISIBILITY chunk during agent-forge-harness-813).
- `eval_golden.py` / `eval_answers.py` are the existing before/after regression gate (used
  identically in the 813 fix) — same pattern applies here: run before/after re-extraction+re-embed.

## Decisions Resolved with the User

| Question | Decision | Rationale |
|----------|----------|-----------|
| Fix scope: extract_scan.py heuristic, qa_chunks.py salvage, or both? | Both | Fixes the root cause (bad lines shouldn't become entity_name at all) and adds defense-in-depth (a chunk that fails only bad_entity with otherwise-clean text is salvaged by nulling entity_name instead of discarding the whole chunk) |
| Pin pymupdf to an exact version? | Yes | Addresses the drift risk directly (not just its symptom); uv cache already proves two different resolved versions exist on this machine |
| Which books to re-extract/re-verify? | VGM + Tortle only | Matches the bead's exact ask; other fitz-engine supplement books (xge/tce/mtf/eepc/scag/eberron/ravnica) share the same extractor and are plausibly affected too, but a full-corpus sweep is out of scope for this task — file a follow-up bead |

## Constraints & Non-Goals
- Non-goal: do **not** attempt to salvage Tortle's cipher-garbage chunks into "clean" — that
  content is genuinely corrupted (CID font decode failure), and forcing it through would be a
  quality regression, not a fix. Tortle's `bad_entity`/`low_alpha` counts are expected to **stay
  quarantined** after this fix; only VGM's clean-chunk count should recover meaningfully.
- Non-goal: this task does not re-extract/re-verify the other 8 fitz-engine supplement books
  (xge-5e, tce-5e, phb-5e, mtf-5e, eepc-5e, scag-5e, eberron-5e, ravnica-5e) even though they share
  `extract_supplement_chunks()` and are plausibly affected by the same capture heuristic gap.
- Constraint: the entity-capture heuristic fix must not regress the 56 existing
  `test_extract_scan.py` cases (stat-block/magic-item/spell name recovery already has narrow,
  carefully-commented heuristics — see the `recover_statblock_name`/`open_spell_via_casting`
  docstrings for why they're shaped the way they are).
- Constraint: pinning pymupdf must not break the two books calibrated on pdfplumber (`mm-5e`,
  `dmg-5e` stay on `--engine pdfplumber` per `extract_scan.py:84-89`) — the pin only applies to the
  fitz path.

## Open Risks / Assumptions Carried Forward
- The exact pymupdf version in use when the *original* (pre-regression) VGM/Tortle extraction ran
  is not recoverable (not logged, not in git history) — the pin will fix the version going
  forward but can't prove which specific version behavior change caused this specific jump.
  Accepted: the plan phase should pick a recent stable pymupdf release and verify against it,
  rather than chase the exact prior version.
- Tightening `is_heading()`'s bold-flag branch and the no-open-chunk fallback risks under-capturing
  some legitimate entity names in edge cases the current 56 tests don't cover — the plan phase
  should design the tightened heuristic test-first against both the new bad samples (margin
  quotes/tables/dividers) and the existing legitimate-name fixtures, to catch a regression in
  either direction.
- A follow-up bead should be filed for auditing the other fitz-engine supplement books for the
  same entity-capture gap, and a second follow-up for Tortle's underlying CID-font decode issue
  (separate from this task's fix).

## Recommended Scope for Planning
Fix `extract_scan.py`'s entity-name capture so margin quotes, random-encounter table rows, and
chapter/section dividers stop being assigned as `entity_name` (tighten `is_heading()`'s bold branch
and/or add a name-shape pre-check before the no-open-chunk fallback claims a line). Add a salvage
path in `qa_chunks.py` so a chunk that fails `classify_chunk` **only** on `bad_entity` with
otherwise-clean text has its `entity_name` nulled and is kept clean, rather than the whole chunk
being discarded. Pin pymupdf to an exact version in `pyproject.toml` (fitz path only; leave
pdfplumber-engine books untouched). Re-run extract → QA → embed for `vgm-5e` and `tortle-5e` only,
and verify with `eval_golden.py`/`eval_answers.py` before/after (same pattern as
agent-forge-harness-813) — expect VGM's clean-chunk count to recover toward baseline while Tortle's
cipher-garbage quarantines remain unchanged. TDD: extend `test_extract_scan.py` and
`test_qa_chunks.py` with the real VGM/Tortle samples captured in this document as fixtures. File two
follow-up beads (other fitz-engine books; Tortle CID-font decode) rather than expanding this task's
scope.

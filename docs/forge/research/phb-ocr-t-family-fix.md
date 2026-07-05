# Research: phb-ocr-t-family-fix — Systemic t→l OCR corruption in PHB vector data
Generated: 2026-07-02
Repo: game-guide-ai
Phase: research + fix (retroactive — mini path, forge-mini)
Bead: agent-forge-harness-813 (closed) · follow-up: agent-forge-harness-wu1 (open)
PR: https://github.com/jupes/game-guide-ai/pull/20

## Goal
Diagnose a user-reported data-quality bug — retrieved chunks reading garbled text like
"Casting Time: I aclion ... Concenlralion, up lo I minule" — trace it to its root cause in
the ingestion pipeline, fix the corrupted data already sitting in the vector DB, and prove
the fix with the existing eval harness (`eval_golden.py`, `eval_answers.py`).

## Symptom
User-supplied example, verbatim from a retrieved chunk (GREATER INVISIBILITY):

```
GREATER INVISIBILITY
4th-level illusion
Casting Time: I aclion
Range: Touch
Components: V.5
Duration: Concenlralion, up lo I minule
You or a crealure you louch becomes invisible unlil lhe
spell ends. Anylhing lhe largel is wearing or carrying is
invisible as long as il is on lhe largel's person.
```

Every instance of the letter **"t"** has been misread as **"l"** by OCR — a scan-quality
defect, not a random one-off. Also present: the digit **"1"** misread as capital **"I"**
(`I aclion`, `I minule`), and spell components **"S"** misread as **"5"** (`V.5`).

## What the Code Says (answered by exploration)

### Root cause: a known-but-incomplete OCR normalization pass
`ingestion/ocr_normalize.py` already existed to fix **other** PHB OCR substitution
families — its docstring documents them explicitly: capital `I` for lowercase `l`
(`leveI`→`level`), `V` for `Y` (`Vou`→`You`), `e` for `c` (`ereature`→`creature`), plus a
curated list of fused/dice tokens. It runs once per chunk in `extract_scan.py:1079-1081`.

**It had zero rules for the t→l family.** The `t`-misread-as-`l` defect is a distinct OCR
failure mode from the ones already handled, and nobody had extended the normalizer to
cover it. Garbled chunks passed the QA gate (`qa_chunks.py`) — `lhe`, `aclion`, `crealure`
are alphabetic and long enough to look like real words to a low-alpha/bad-entity heuristic
— straight into `chunks-phb-5e.clean.jsonl`, and from there into the embed step.

### Blast radius (measured in the pre-fix `.clean.jsonl`, i.e. what was actually embedded)
| Garbled token | Count | Garbled token | Count |
|---|---|---|---|
| `lhe` (the) | 731 | `poinls` (points) | 15 |
| `lo` (to) | 278 | `minule` (minute) | 14 |
| `crealure` (creature) | 82 | `unlil` (until) | 12 |
| `wilh` (with) | 78 | `Concenlralion` (concentration) | 11 |
| `aclion` (action) | 43 | `louch` (touch) | 8 |
| — | — | `lurn` (turn) | 7 |

PHB-specific: `chunks-dmg-5e.jsonl` had exactly **1** stray `lhe` (noise, not systemic);
every other book (`xge`, `tce`, `vgm`, `mtf`, `eepc`, `scag`, `tortle`, `eberron`,
`ravnica`, `mm`) was clean. Confirms the defect is a property of *this specific PHB scan*,
consistent with the existing normalizer's framing ("no-op for [other books], fixes the
phb-5e scan").

### Two additional defects found during diagnosis (not part of the original report, fixed alongside it)
1. **`ingest_books.py` skipped normalization entirely.** The orchestrated ingest pipeline
   (`extract → qa_chunks → embed`, `ingestion/ingest_books.py:extract_book`) never called
   `normalize_ocr` — only the standalone `extract_scan.py` CLI path did
   (`extract_scan.py:1078-1081`). Anyone re-running the orchestrated ingest would silently
   ship raw OCR garbage, including OCR families the normalizer *does* already fix.
2. **The `game-guide-ai` vector DB was empty.** Post-rename (`rag-chat` → `game-guide-ai`,
   PR #17), the new `game-guide-ai-vector-db` container/volume held zero rows —
   `dnd.chunks` had never been (re-)populated after the rename. The live stack
   (`game-guide-ai-service`, port 8000) was answering from an empty corpus. The actual
   corrupted data the user saw was still resident in the *old*, stopped
   `rag-chat-vector-db` container (db `rag_chat`, host port 5432) — 224 chunks matching
   the t→l garble pattern, confirmed by direct query.

## Fix Design

### Why not a bigger curated list?
The other substitution families (`I`→`l`, `V`→`Y`, `e`→`c`) are curated because they're
either a single global-safe token transform (`_fix_capital_i`: any non-leading capital `I`
in a token with a lowercase letter is a misread `l`) or a short, closed list of high-frequency
words. The t→l family doesn't have that shape — `l` appears everywhere in English, so a
context-free "replace l with t" rule would corrupt real words (`level`, `spell`, `illusion`).
The garbled forms are also too numerous and varied for a hand-curated list to have any
confidence of completeness (this repo alone surfaced 60+ distinct garbled word-forms during
mining).

### The approach: vocabulary-checked repair
1. **`ingestion/build_vocab.py`** (new) — builds `vocab_5e.txt`, a set of ~17.4k words
   drawn from the 8 other, cleanly-extracted 5E books' `.clean.jsonl` files (min frequency 3,
   to filter their own typos/garbles). PHB is excluded — it's the corrupted source being
   repaired, not a source of truth.
2. **`ocr_normalize._fix_l_for_t_token`** (new) — for each word-token: if the token is
   *already* a known word, leave it. Otherwise, try every subset of its `l` positions
   flipped to `t`; if *exactly one* resulting candidate is a known word, use it. Ambiguous
   tokens (zero or multiple valid candidates) are left untouched — conservative by
   construction, same philosophy as the existing normalizer.
3. **Curated short-token list** (`_T_SHORT_FIXES`) — tokens under 4 characters are too
   short for the vocabulary heuristic to be reliable (`al`→`at`, `il`→`it`, `lo`→`to`, …),
   so these use the same explicit-non-word-pattern approach as the pre-existing rules.
4. **Context-only rules** (`_T_CONTEXT_FIXES`) — a small number of garbled forms collide
   with real English words already in the vocabulary (`feel`↔`feet`, `fool`↔`foot`,
   `lake`↔`take`), so the vocabulary pass correctly leaves them alone. These are instead
   fixed only in D&D-specific numeric/verb contexts (`30 feel.`→`30 feet.`,
   `10-fool radius`→`10-foot radius`, `you lake 4d6`→`you take 4d6`) where the real English
   word cannot occur — genuine uses of "feel", "fool", "lake" pass through unchanged.
5. **Tail fixes** (`_T_TAIL_FIXES`) — `I action`/`I minute`/`I hour`/… → `1 …` (digit
   misread), and `Components: V.5` / `V, 5` / `V.S` → `Components: V, S` (component-letter
   misread).
6. **Gated to PHB.** All of the above is riskier per-rule than the existing normalizer (a
   vocabulary lookup can theoretically misfire; context rules are heuristic), so the whole
   layer only activates when `normalize_ocr(text, book="phb-5e")` is called — a new `book`
   parameter threaded through from both call sites (`extract_scan.py`, `ingest_books.py`).
   Every other book is byte-for-byte unaffected.

### TDD coverage (`ingestion/test_ocr_normalize.py`, 8 new tests, 17 total passing)
- `test_fixes_reported_greater_invisibility_chunk` — the exact user-reported text, end to end.
- `test_vocab_pass_fixes_long_tail_words` — vocabulary-driven repairs (`Dexlerily`, `Slrenglh`, …).
- `test_fixes_short_t_words`, `test_feet_foot_take_only_fixed_in_context`,
  `test_fixes_digit_one_before_time_units`, `test_fixes_components_s_misread_as_5` — each
  rule family, including the "must NOT fire" side (`do you feel lucky` stays unchanged).
- `test_t_family_is_gated_to_phb` — the layer is a no-op without `book="phb-5e"`.
- `test_vocab_pass_leaves_unknown_words_alone` — genuinely unknown tokens (`Melf's`) survive.

## Remediation Steps Taken
1. Implemented and TDD'd the fix above (`ocr_normalize.py`, `build_vocab.py`, `vocab_5e.txt`).
2. Wired `book=` through `extract_scan.py` and `ingest_books.py` (fixing defect #1 above).
3. Regenerated `chunks-phb-5e.jsonl` / `.clean.jsonl` via `extract_scan.py` — confirmed
   all measured garble tokens (`lhe`, `aclion`, `crealure`, `Concenlralion`, …) at **0** in
   the regenerated clean file.
4. Re-ingested the **full 10-book Tier-A corpus** into `game-guide-ai`'s vector DB (port
   5433, db `game_guide_ai`) via `ingest_books.py`, since it was empty (defect #2). Also
   re-embedded `mm-5e` and `dmg-5e` from their existing chunk files to restore full parity
   with the old corpus (8,898 chunks total vs. the old DB's 9,103 — the gap is stricter QA
   quarantine on `vgm-5e`/`tortle-5e`, tracked separately, see below).
5. Verified directly against the DB: 224 chunks matching the corruption pattern in the old
   corpus → 0 in the new one; the reported Greater Invisibility chunk now reads correctly
   verbatim in `dnd.chunks`.
6. Queried the live service (`POST /chat`) to confirm the fix end-to-end, not just at the
   data layer.

## Eval Results (before = old corrupted `rag_chat` DB, after = fixed `game_guide_ai` DB)

**`eval_golden.py` (174 positive queries, retrieval-only):**
| Metric | Before | After |
|---|---|---|
| Hit@1 | 83.3% (145/174) | 82.8% (144/174) |
| Precision@5 | 44.4% | 43.4% |
| MRR | 0.874 | 0.868 |
| Recall@10 | 94.3% (164/174) | 93.7% (163/174) |
| Negatives correctly refused | 5/5 | 5/5 |

Flat within noise — **no retrieval regression**. This is expected: embedding models are
reasonably robust to character-level noise, so the corruption mostly didn't move retrieval
*ranking*. The actual harm was to answer **content** — a correctly-retrieved chunk that
reads "Concenlralion, up lo I minule" produces a garbled or hedged answer even when
retrieval worked perfectly.

**`eval_answers.py` (12-case subset, deterministic graders, `--no-langfuse`):**
`answerable`/`refused`/`citation_ok` identical before vs. after across all 6 scored cases
(the eval's fixed 6-case seed didn't happen to hit a PHB-spell case whose citation flips on
this fix — the retrieval-level result already shows the corpus is otherwise stable). The
direct proof of content improvement is the live query below.

**Live demo (the actual fix, not a proxy metric):**
```
$ curl -X POST localhost:8000/chat -d '{"prompt":"What does the Greater Invisibility spell do? Quote its casting time, components and duration."}'

The Greater Invisibility spell makes you or a creature you touch become invisible
until the spell ends. Here are the details:
- Casting Time: 1 action
- Components: V, S
- Duration: Concentration, up to 1 minute [1].
```

## Open Follow-Up
`agent-forge-harness-wu1` (P3, open): re-extracting `vgm-5e` and `tortle-5e` with the
current toolchain produced a higher QA-quarantine rate than the previously-committed chunk
files (`vgm-5e` clean 1339→1156, `tortle-5e` clean 90→72). `eval_golden`'s monster category
was unchanged, and sampled newly-quarantined `tortle-5e` chunks are cipher-garbage the old
pipeline was wrongly passing as clean — but ~200 `vgm-5e` chunks (Volo margin-quote text)
may now be over-aggressively caught by the `bad_entity` heuristic and warrant a closer look,
possibly a `pymupdf` version drift changing extraction output.

## Key Decisions
- **Vocabulary built from sibling books, not a dictionary** — ties the "known word" check to
  the actual domain corpus (D&D terms, proper nouns) rather than general English, avoiding
  false negatives on terms like "Beholder" or "Concentration"-adjacent jargon.
- **Gate the whole t-family layer to `book="phb-5e"`** rather than making every individual
  rule self-evidently safe corpus-wide — several of the rules (short tokens, context-only
  collisions) are only safe *because* PHB is known to carry this specific defect; applying
  them blind to a clean book would risk false positives with no corresponding upside.
- **Fixed data in place via re-ingest, not a DB migration/UPDATE** — chunk IDs and content
  are regenerated by re-running extraction, so re-ingesting from the corrected pipeline is
  simpler and more auditable than patching embedded rows directly.

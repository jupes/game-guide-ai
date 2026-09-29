# 1kg.2.1 ship-review precision follow-ups

Bead: `agent-forge-harness-zl4` (game-guide-ai) · Ship report reviewed: round 3 of
`agent-forge-harness-1kg.2.1` (harness-local `reports/1kg.2.1-campaign-schema-ship.md`, not
mirrored into this repo because it predates PR #76's two rework rounds) · PR: [#76](https://github.com/jupes/game-guide-ai/pull/76)
(merged as `4057f05` into `integration/1kg-workbench`).

Round-3 review of `1kg.2.1`'s ship phase passed (0 blocker / 0 high / 1 medium / 6 low). The
medium was fixed at the time. This records the disposition of the six lows — each is either
fixed elsewhere (this repo has no code or test that needed changing) or the text it named no
longer exists, superseded by PR #76's rework rounds after the finding was filed.

| # | Finding | Disposition |
|---|---|---|
| 1 | The ship report's "Review rounds" table omitted the PR body's "on the implementation" qualifier, and the same ledger also holds Ship round 1 and Ship round 2, which no table showed. | **Fixed** in the harness-local ship report: heading now reads "Review rounds on the implementation", with Ship rounds 1 and 2 named inline instead of left for the reader to infer from Work Done. |
| 2 | Walkthrough step 5 explained 21 of the 35 skips (the `[postgres]`-parametrised ones, 1:1 with the 21 `[fake]` passes) and left the other 14 — unparametrised `needs_db` tests — unexplained in place. | **Fixed** in the harness-local ship report: step 5 now names the 14 and points at "What is proved where", where they were already enumerated. |
| 3 | The AC trace's "the other 10 are unparametrised tests of the records, the store guards and the column bounds" missed one, and four of the ten are in fact parametrised over their own inputs. | **No longer exists.** That sentence is only in the pre-rework draft (`.tmp/work/1kg.2.1-pr-body.md`, never committed — a harness working file). PR #76's rework rounds rewrote that AC-trace row entirely; the live, merged body says "of the 61 that pass locally, 44 are the `fake` half" and no longer breaks out an "other 10". Nothing to fix. |
| 4 | "Roughly a third" of this change's behaviour being CI-only measures closer to a fifth — conservative direction, but the finding asked for the measured figure. | **Fixed** on the live PR #76 body (`gh pr edit`), which still had this exact sentence post-merge. Recomputed against the branch's final numbers rather than the pre-rework ones the finding was filed against: `bb69450` base 1506 passed / 62 skipped vs. the merged tip's CI run [35544420877](https://github.com/jupes/game-guide-ai/actions/runs/35544420877) at 2000 passed / 158 skipped — 96 of the 590 tests this change adds are CI-only, 16%, closer to a sixth than a third. The ship report's own equivalent sentence already said "roughly a fifth" and needed no change. |
| 5 | Plan round 2 had no `review:` comment on the bead where every other round does. | **Fixed**: backfilled on `agent-forge-harness-1kg.2.1` from the run-state ledger (`.tmp/work/forge-runs/1kg.2.1-campaign-schema.json`), which already held the round's verdict and findings (FAIL, 0/1/5/7) — only the bead comment was missing. |
| 6 | `test_deleting_an_owner_whose_own_conversation_links_to_their_campaign_is_fail_closed` names an outcome CI reports as a cascade; the docstring and both artifacts describe it honestly, but the name reads as though it asserts a refusal. | **No longer exists.** Rework round 1's finding F-9 (commit `33790e4`) already renamed it to `test_deleting_an_owner_whose_own_conversation_links_to_their_campaign_deletes_both`, with a docstring stating the cascade outcome directly (`tests/test_migrations_db.py` on `integration/1kg-workbench`). Nothing to rename. |

No route, wire model, slot, UI, migration or test file in this repo needed a change for any of the
six — the ones that were fixable lived in the harness's own local ship report or in PR #76's live
description, and two had already been overtaken by the rework rounds that landed after the
finding was filed. This file is the durable, repo-tracked record of that, since `1kg.2.1`'s ship
report itself stays harness-local by convention.

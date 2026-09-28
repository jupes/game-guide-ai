# Live Session Assistant plan tooling

Scripts that generated and checked the Live Session Assistant planning documents
and Beads (epic `agent-forge-harness-1ir`). They make the evidence in
`docs/forge/plans/live-session-assistant-delivery.md` §7 and §10 and the cost
tables in `docs/forge/research/live-session-assistant-cost-model.md`
reproducible.

Like the rest of `docs/forge/`, this is a point-in-time record. **After
2026-09-17 the Beads tracker is authoritative**: `spec.ts` records what was
created, and `verify-created.ts` reports how the tracker has drifted from it.

## Requirements

- [Bun](https://bun.sh) (any recent 1.x).
- `bd` for the scripts that read the tracker. The scripts run `bd` from the
  repository root, and `bd` resolves this repository's `.beads` from there;
  git worktrees resolve to the main checkout's `.beads`. Run from outside the
  repository, for example the harness root, `bd` finds a different tracker.
  To read another location, pass `--tracker-cwd <dir>`; to read an export, pass
  `--snapshot <file>` from `bd list --all --json --limit 0`. Every script
  refuses a tracker without the `agent-forge-harness-1kg` epic.

## Files

| File | Purpose |
| --- | --- |
| `spec.ts` | The bead specification: 117 beads with type, priority, phase, gates, labels, description, acceptance criteria, and `blockedBy` / `blocks` / `relatesTo` edges |
| `ids.json` | Created tracker ID for each plan key; `LSA-x.y` is `agent-forge-harness-1ir.x.y` |
| `tracker.ts` | Shared helpers: repository paths, arguments, tracker loading and its wrong-tracker guard |
| `validate.ts` | Rules R1–R11: hierarchy, references, cycles over plan edges plus every tracker edge, text quality, gate shape and chaining, phase order, per-phase gate coverage, player-disclosure ordering |
| `tables.ts` | Regenerates the delivery plan's generated regions: the Beads hierarchy (§6) and the cross-initiative table (§5.1) |
| `checkdocs.ts` | Document consistency: `LSA-` references, bead IDs, table widths, repository paths, code fences, threat IDs |
| `identity.ts` | Run by `checkdocs.ts` over the account identity record: its IDs are unique, resolve and appear in no other decision record, and its reviewed invariants hold |
| `costs.ts` | The cost calculator behind the cost model; `cost-tables.md` is its recorded output |
| `create.ts` | The materialization script that created the beads and edges; `--dry-run` checks references without writing |
| `verify-created.ts` | Compares the tracker's beads with `spec.ts` and lists drift and the ready leaves |

## Commands

Run from this folder:

```bash
bun validate.ts
bun tables.ts --check
bun checkdocs.ts
bun costs.ts | diff - cost-tables.md
bun verify-created.ts
```

To reproduce the recorded validator run exactly, pass `--snapshot` with a
tracker export taken before 2026-09-17; the export itself is not committed
because `.beads` is kept out of git.

To change the plan's bead structure before materialization, the workflow was:
edit `spec.ts`, run `validate.ts`, run `tables.ts --write`, then `checkdocs.ts`.
After materialization, change the beads with `bd` instead.

## Recorded results

| Run | Result |
| --- | --- |
| `validate.ts` on the pre-creation export (152 issues, 308 `blocks` edges) | Valid. 117 beads; 250 internal, 41 incoming, and 4 outgoing blocking edges; 51 relates-to edges; 18 external MVP blockers |
| `create.ts --really` (2026-09-17) | 117 of 117 created; 346 intended edges, none missing; `bd dep cycles` found no cycles |
| `verify-created.ts` right after creation | 0 problems; tracker at 269 issues |

Run against today's tracker, `validate.ts` counts the created beads and any
newer issues, so its totals differ from the recorded run.

# Plan Review 2: game-guide-ai-memory-personalization — conversation memory and personalization

Source: `docs/forge/plans/game-guide-ai-memory-personalization.md` · Reviewed: 2026-07-26

Review scope: closure check for all four High and four Medium findings in
`docs/forge/reports/game-guide-ai-memory-personalization-plan-review.md`, plus a regression scan
against `origin/master`, `origin/feat/x5bz.2-invite-auth`, the companion b8o plan, and the live
game-guide-ai Beads graph.

## Verdict: SOUND — 0 Blocker / 0 High / 0 Medium / 0 Low

All eight turn-1 findings are resolved in the revised plan and, where applicable, in the live
Beads graph. The second-pass regression findings were also closed: M1 now gives ambiguous
follow-ups a bounded deterministic retrieval-continuity seam before the existing answerability
gate; account deletion invalidates old stateless cookies through live-user validation; summaries
are inspectable and independently resettable; and 1ka.5 explicitly owns aggregate retention rather
than assuming b8o.5's strategy-row cleanup is sufficient. No actionable findings remain.

## Turn-1 finding closure

| Turn-1 finding | Status | Evidence in revision |
|---|---|---|
| Final prompt assembled before model route/budget | **Resolved** | The route resolves the attempt/profile before memory budgeting and final assembly; a smaller fallback re-budgets from the same candidates (`plan:341-355`, `:654-681`) |
| Summary outbox transaction/lease incomplete | **Resolved** | One `append_turn_and_schedule_summary()` transaction commits turns and due work together; claim tokens, lease expiry, stale-running reclaim, token-guarded completion, and terminal failure are explicit (`plan:489-542`, `:757-787`) |
| Conversation/account deletion unowned | **Resolved** | 1ka.5 owns owner-checked server-first conversation deletion; M4 owns account deletion, live-user validation, cross-device old-cookie rejection, and cascade/failure-atomicity tests (`plan:605-621`, `:804-825`, `:867-922`, `:1361-1386`, `:1463-1486`) |
| Prompt assembler duplicated / disabled bytes conflict | **Resolved** | M3 owns only pure preference fragments; M1 owns `assemble_generation_prompt()`; empty/default inputs omit the memory policy and preserve the current two messages byte-for-byte (`plan:243`, `:339-423`, `:1271-1308`, `:1310-1359`) |
| x5bz.4 contract mismatch | **Resolved** | Existing tester feedback stays an external prerequisite; 1ka.6 owns bounded reasons and regenerate linkage; x5bz.4's optional free text is excluded from memo/model input (`plan:588-592`, `:863-880`, `:924-943`, `:1447-1461`) |
| Documented and actual dependency graphs disagree | **Resolved** | 1ka.5 and 1ka.6 exist with the documented titles/AC; the live graph now includes 1ka.2 → 1ka.1/1ka.5/b8o.2/b8o.5, 1ka.4 → 1ka.2/1ka.3/1ka.6, 1ka.5 → b8o.2/b8o.5, and 1ka.6 → 1ka.3 (`plan:1505-1540`; verified with `bd show`) |
| M4 schema omitted profile schema/experiment assignment | **Resolved** | The plan creates `profile`, defines fact/memo tables, and persists a user/experiment/revision assignment independently of memo existence (`plan:544-586`) |
| Langfuse described as exactly pinned | **Resolved** | The plan accurately states that the repo has only a major-range constraint and no application lock, then requires an exact tested Langfuse/LangChain resolution and production `CallbackHandler` export test before memory tracing (`plan:252`, `:1001-1026`) |

## Regression findings closed during turn 2

### Retrieval continuity before the answerability gate

M1 now reads an owned, hard-capped recent candidate set before retrieval and applies the pure
`build_retrieval_query()` function only to conservatively detected follow-ups. Standalone prompts
remain byte-identical; the prior turn is a capped, escaped topic hint rather than rules evidence;
the original question remains unchanged for generation; and no hidden router-model call is added
(`plan:625-650`, `:652-705`). The test matrix and M1 Red/Green checklist cover standalone byte
equivalence, pronoun/continuation behavior, caps, escaping, injection-shaped history, graph gating,
and groundedness (`plan:1231-1241`, `:1321-1340`). The live 1ka.1 Bead AC carries the same contract.

### Account deletion and stateless-cookie revocation

The circular M4 prerequisite is removed: conversation deletion is pretested, while M4 itself owns
and tests account deletion (`plan:869-880`). The plan now requires every protected request to
resolve the signed user ID to a live account, so cookies retained on other devices return `401`
after deletion before any store or provider work. The two-client test explicitly exercises `/chat`
and `/conversations/*`, covering b8o's null-conversation path (`plan:904-922`,
`:1248`, `:1475-1486`). The live 1ka.4 Bead AC includes old-cookie rejection and zero downstream
work.

### Summary inspection and reset

Carry-forward now displays the exact current summary before confirmation (`plan:789-802`). An owned
conversation-details panel exposes the summary, update time, and generating model, while
`DELETE /conversations/{id}/summary` clears the snapshot, advances the checkpoint, cancels jobs,
and prevents old summarized turns from being reintroduced (`plan:804-813`). The test matrix and
live 1ka.2 Bead AC cover the inspect/reset behavior.

### Aggregate retention ownership

The plan now distinguishes b8o.5's existing inactive/orphan strategy-row cleanup from application
memory retention. 1ka.5 depends on b8o.5 and explicitly extends that seam to delete an inactive
owned conversation aggregate with a published cutoff, dry-run, bounded batches, active-row
exclusion, cascade coverage, and idempotent retry (`plan:1028-1047`, `:1361-1386`,
`:1505-1536`). The live 1ka.5 Bead dependency and AC match this ownership.

## Verified as accurate

- The route/budget order matches b8o's after-retrieval, before-generation classifier seam, and
  fallback re-budgeting removes the previous circularity.
- Durable summary work has a coherent outbox, claim, lease, reconciliation, retry, monotonic
  checkpoint, and fail-open stale-summary contract.
- M3 remains independently deliverable because it adds only bounded preference fragments to the
  current generation seam.
- Empty/default inputs preserve the current provider message sequence and content byte-for-byte.
- Conversation, summary, fact, memo, account, and inactivity-retention deletion boundaries now
  have named owners and testable behavior.
- x5bz.4 and 1ka.6 have separate, non-overlapping public contracts.
- The profile schema and experiment-assignment design are executable and deletion-aware.
- Tracing privacy is gated on an exactly resolved dependency set and the production serialization
  path, not a private method or assumed SDK API.
- 1ka.5 and 1ka.6 exist in the game-guide-ai tracker, and all documented local dependency edges
  match `bd show`.
- The PR-structure prose now accurately describes four memory/personalization tiers plus two
  prerequisite child PRs.

## Not verified

- Auth D is still open; its final conversation schema, live-user guard, CSRF, and re-authentication
  contracts may change exact store names or migration ownership.
- Exact Langfuse/LangChain resolution, production masking serialization, Cloud Tasks delivery,
  provider qualification, and future worker/demo commands remain implementation-time gates.
- Retention durations are intentionally unresolved pending policy review; this review verified
  ownership and acceptance criteria, not the future selected durations.
- Provider prices were not independently refreshed in turn 2; they remain inherited from the
  companion plan's cited 2026-07-24 research and arithmetic.

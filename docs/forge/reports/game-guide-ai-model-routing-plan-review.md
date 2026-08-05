# Revised Plan Review: game-guide-ai-model-routing

Source: `docs/forge/plans/game-guide-ai-model-routing.md` · Reviewed: 2026-07-24

## Verdict: SOUND — 0 Blocker / 0 High / 0 Medium / 0 Low

All nine findings from the first review are resolved at the design level. The revised plan now has
coherent per-turn `Auto` semantics, an implementable strategy-binding invariant, an honest
stateless boundary for Kimi, provider-neutral attempt/usage contracts, frozen retrieval inputs for
evaluation, reasoning-sensitive cost framing, provider adapter qualification, compliant PR body
requirements, and a durable K3 API-pricing source.

The final correction pass also verifies the materialized child Beads and removes the last ambiguous
network-egress claim. No confirmed or unverified plan inaccuracy remains.

## Prior finding resolution table

| Prior finding | Previous severity | Resolution | Revised-plan evidence |
|---|---:|---|---|
| Per-turn `Auto` contradicted effective-model conversation pinning | Blocker | **Resolved** | The conversation now binds the requested strategy; manual aliases stay fixed while `auto` resolves an effective alias per turn (`plan:28-34`, `540-566`). TDD behavior 8 and checkpoint 4 explicitly test unlike consecutive turns (`plan:708`, `810-819`). |
| “First successful request atomically binds” lacked a concurrency state machine | High | **Resolved** | The invariant is now “first accepted request”: insert the strategy before the provider call, retain it on failure, allow a matching winner, and reject mismatches with `409` before an LLM call (`plan:550-556`, `705`, `765-776`). This can be implemented with a unique insert/transaction without a pending provider-call lease. |
| Kimi reasoning-history safety was misapplied to the stateless app | High | **Resolved** | The plan now states that generation is single-turn/stateless and that stored UI history and provider reasoning are not replayed (`plan:38-41`, `70-74`). K3 stays manual/evaluation-only and preserved-thinking support is a future design (`plan:304-307`, `583-585`, `894`). |
| Attempt-level usage/cost had no generation contract | High | **Resolved** | `GenerationResult`, nullable `GenerationUsage`, request ID, finish reason, one observer event per provider call, `max_retries=0`, and service-owned retries are now part of the provider boundary and checkpoint 1 (`plan:458-479`, `622-625`, `710`, `742-751`). |
| Eval promised fixed contexts while rerunning retrieval per candidate | High | **Resolved** | The plan now specifies capture/replay with ordered chunk IDs, full-text hashes, retrieval revision, one assembled context, drift rejection, and no committed licensed text (`plan:660-671`, `714`, `785-800`). |
| Cost table claimed tokenizer normalization and under-described reasoning cost | Medium | **Resolved** | It is relabeled as a same-reported-token scenario, explicitly says tokenizers/reasoning are not normalized, adds a 600-versus-1,800-output sensitivity table, and defers production ceilings to measured usage (`plan:202-257`). The new table arithmetic recomputes correctly. |
| Shared `ChatOpenAI` compatibility was presented as proven | Medium | **Resolved** | It is now explicitly a hypothesis. The plan requires a pinned reviewed client, sanitized provider contract fixtures, opt-in live smoke tests, and a provider-specific/raw adapter when fields are lost (`plan:441-453`, `708`, `746-750`). |
| Recommended PRs omitted mandatory repository structure | Medium | **Resolved** | The revised matrix requires Bead-matched titles and `Summary`, `Test Plan`, `AC checklist`, and `Screenshots` sections, with before/after screenshots for UI work (`plan:838-848`). This matches `AGENTS.md:128-132`. |
| K3 pricing citation redirected to membership pricing | Medium | **Resolved** | The source now points to the Kimi API platform pricing documentation (`plan:947`), whose K3 page identifies the 1M-context flagship and current API capabilities. The stated prices remain corroborated by the official Kimi API platform and technical blog. |

## Final correction resolution table

| Correction-pass finding | Resolution | Final evidence |
|---|---|---|
| Child Beads and their dependency graph appeared absent | **Resolved; prior finding withdrawn** | The earlier check used the isolated plan worktree rather than the live sub-repo tracker. `repos/game-guide-ai/.beads/issues.jsonl:1-6` contains the parent plus `b8o.1`–`b8o.5`; all six titles exactly match the PR matrix. The exported dependency rows show `b8o.2 → b8o.1`, `b8o.3 → b8o.1`, `b8o.4 → {b8o.2, b8o.3}`, and `b8o.5 → b8o.4`. This graph is acyclic. |
| Generic Cloud Run egress was named as a compensating control without design scope | **Resolved** | Alibaba onboarding now names the controls actually in scope—dedicated workspace/key, Secret Manager rotation, spend alerts, and the application daily cap—and explicitly says network filtering requires a separate VPC/NAT/firewall design and cannot prevent off-platform use of a stolen key (`plan:284-288`). |

## Findings

None.

## Verified as accurate (changed and load-bearing claims)

- The revised strategy/effective-model split matches the current stateless request path and is
  internally coherent: manual selection binds one alias; `Auto` reclassifies per turn; K3 is
  excluded from `Auto`. ✓
- Binding the first accepted strategy before generation is compatible with the current
  best-effort post-generation message persistence (`service/app.py:203-217`, `248-278`) and the
  absence of an existing conversations table (`service/history.py:34-57`). ✓
- The stateless claim is exact: only attachments are fetched before `svc.answer`
  (`service/app.py:221-260`), the graph state receives prompt/mode/attachment fields
  (`service/rag.py:132-138`), and generation sends one system plus one current-user message
  (`service/generate.py:230-236`). ✓
- The proposed `GenerationResult`/attempt-observer boundary directly addresses the current
  `LLMClient -> Any -> .content` information loss (`service/generate.py:29-33`, `196-204`,
  `230-236`). Unknown provider usage being nullable rather than zero is the correct accounting
  invariant. ✓
- Disabling SDK retries and owning retry/fallback in the service makes one observer record per
  actual provider call feasible and testable. ✓
- The capture/replay design fits the existing harness: today `compare` invokes `run_eval` once per
  service and therefore reruns the graph/retrieval (`ingestion/compare_models.py:104-113`,
  `ingestion/eval_answers.py:151-163`). The proposed fixture creates the missing seam without
  changing live retrieval. ✓
- Both the 600- and 1,800-output cost tables recompute correctly from the current list prices. The
  revised prose no longer claims tokenizer normalization and correctly requires measured usage. ✓
- The provider-client section no longer assumes the old `langchain-openai>=0.2,<0.4` range proves
  current wire compatibility (`pyproject.toml:25`); contract fixtures and opt-in live tests are
  appropriately required before enablement. ✓
- The revised PR body requirements exactly match the repository's PR rules
  (`AGENTS.md:128-132`). The live sub-repo Beads export contains the parent plus all five children;
  their titles exactly match the PR matrix and their internal dependency graph matches
  `plan:852-853` without a cycle. ✓
- Alibaba onboarding now accurately distinguishes the compensating controls in scope from a future
  VPC/NAT/firewall design and correctly states that network filtering cannot prevent external use
  of a stolen key (`plan:284-288`). ✓
- The replacement K3 pricing link is an official Kimi API-platform page rather than the previous
  membership redirect
  ([Kimi API pricing](https://platform.kimi.ai/docs/pricing/chat)). ✓
- Previously checked provider facts remain unchanged and current as of this re-review: DeepSeek V4
  IDs/prices/privacy caveats, Virginia Qwen IDs/prices/scope and key limitations, K3 context/modes/
  pricing/history warning, OpenAI controls, Gemini parameter deprecation, and Google Secret
  Manager numbered-version guidance. ✓
- The hosting-branch claims remain accurate: it has WIF, locked Cloud Run deployment, and Secret
  Manager injection, and it currently uses the default compute service account plus `:latest`, so
  the proposed runtime-identity and numbered-version hardening is grounded. ✓

## Not verified

- Actual provider adapter compatibility, latency, quality, account availability, and usage
  metadata remain intentionally unverified until the plan's sanitized contract tests and
  spend-capped live qualification runs are executed.
- Rights to transmit licensed corpus excerpts and the providers' non-public contractual
  retention/residency commitments cannot be established from public documentation. The plan
  correctly treats these as activation gates.

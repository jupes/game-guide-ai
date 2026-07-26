# Plan Review: game-guide-ai-memory-personalization — conversation memory and personalization

Source: `docs/forge/plans/game-guide-ai-memory-personalization.md` · Reviewed: 2026-07-26

Ground truth checked:

- `origin/master` in the `game-guide-ai` worktree
- `origin/feat/x5bz.2-invite-auth`
- companion routing plan at
  `C:\tmp\game-guide-ai-model-research\docs\forge\plans\game-guide-ai-model-routing.md`
- root Beads for `x5bz`/`z7fl` and sub-repo Beads for `1ka`/`b8o`
- first-party LangChain, Kimi, OpenAI, Langfuse, Google Cloud, PostgreSQL, and cited paper sources

## Verdict: NEEDS REVISION — 0 Blocker / 4 High / 4 Medium / 0 Low

The design direction is sound and materially stronger than the earlier draft: M-RULE is now
qualified correctly for Kimi K3, current code claims are accurate, ownership is treated as a launch
gate, and the cost arithmetic checks out. Four execution seams still conflict with each other or
have no owner: Auto routing versus model-specific prompt budgeting, the transactional outbox,
deletion, and prompt-assembler ownership/default behavior. Resolve those before calling the plan
implementation-ready.

## Findings

### [HIGH] Final prompt assembly is ordered before the route that supplies its model budget — Target architecture / M-D6

**What:** The architecture sends `assemble_context()` into `assemble_generation_prompt()` and only
then into the b8o router (`plan:258-272`). The prompt assembler is also specified to accept the
selected model profile's input/output budget (`plan:328-335`). That profile does not exist until the
manual/Auto route has selected an effective alias.

**Why it's an issue:** Auto is intentionally resolved per turn. Its classifier uses retrieval
confidence/content types, attachment presence, requested output shape, and prompt/context features.
The implementation cannot finalize a model-specific memory window before knowing the selected
profile, and it cannot follow the diagram without either using the wrong model's context limit or
introducing an implicit second routing pass. Fallback preflight has the same issue: the fallback must
fit the already assembled request.

**Evidence:** The companion plan places the classifier/router after retrieval and before generation
(`game-guide-ai-model-routing.md:735-760`, `:1051-1061`) and says Auto resolves a new effective model
on every turn (`:721-725`). The memory plan requires the selected model's limits to calculate the
residual memory budget (`plan:568-600`) but draws `PROMPT --> ROUTER` (`plan:267-271`). — Confidence:
**Confirmed**

**Suggested correction:** Split preparation from final rendering:

1. retrieve and assemble provider-neutral source/attachment context;
2. read bounded memory candidates and calculate model-independent features;
3. run the b8o classifier/router and bind the strategy;
4. use the effective profile to budget memory and build the final `GenerationPrompt`;
5. perform profile-specific preflight immediately before each primary/fallback adapter attempt.

State whether a fallback re-budgets the same memory envelope for the fallback profile or is rejected
when the final prompt cannot fit.

### [HIGH] The summary outbox cannot be atomic as sequenced, and the claimed lease has no reclaim contract — M2 refresh algorithm

**What:** Step 1 says "`append_turn()` commits" and step 2 then says the summary job is inserted "in
the same transaction as the turn append" (`plan:655-662`). Those statements cannot both be true.
Separately, the worker claims a row with a bounded lease (`plan:663-667`), but the proposed
`chat.memory_jobs` schema has no `lease_expires_at`, claim token, or worker identity
(`plan:475-516`), and reconciliation is described only for `pending` rows.

**Why it's an issue:** If the turn commits before the outbox row, a crash can persist turns without
ever producing a summary job; re-enqueuing already-pending rows cannot recover a row that was never
inserted. If a worker crashes after setting `status='running'`, the row can remain stuck forever
unless lease expiry and stale-running reclamation are defined. Both failure modes violate M-D7's
durable/idempotent execution guarantee.

**Evidence:** Current `PostgresMessageStore.append()` owns and commits one connection/transaction per
call (`service/history.py:147-175`), so the future atomic boundary has to be explicitly redesigned.
The plan's target schema includes `status`, `attempt_count`, `available_at`, and timestamps but no
defined lease field (`plan:478-510`); its reconciliation sentence covers committed pending rows
(`plan:513-516`). Google Cloud Tasks retries delivery, but it does not make the application's
message-plus-outbox database transaction atomic. — Confidence: **Confirmed**

**Suggested correction:** Define one store operation/transaction such as
`append_turn_and_schedule_summary(...)` that locks the owned conversation, writes both messages,
evaluates the threshold, and inserts the unique outbox row before commit. Enqueue to Cloud Tasks
only after that commit. Add an explicit lease (`lease_expires_at` plus claim token/owner), or define
and test an equivalent compare-and-swap using `available_at`; reconciliation must recover both
pending delivery failures and expired `running` claims.

### [HIGH] Conversation and account deletion are privacy gates with no implementation owner — M2 deletion / M4 prerequisites / definition of done

**What:** The plan requires conversation and account deletion before M4, describes immediate
conversation cascades and summary invalidation, promises UI deletion, and makes deletion part of the
epic definition of done (`plan:696-701`, `:745-754`, `:885-900`, `:1372-1389`). No checkpoint
defines `DELETE /conversations/{id}`, its UI/server consistency contract, or an account-deletion
endpoint. No existing Bead found in either tracker owns account deletion.

**Why it's an issue:** The current browser-side `ConversationStore.remove()` only removes local
metadata. Once messages, attachments, summaries, and strategy rows are server-owned, that action
does not delete server memory. Time-based b8o D6 retention is not a substitute for a user-requested
delete. M4 is therefore blocked by a prerequisite that no PR in the plan will produce, and the
privacy/deletion acceptance criteria cannot be met.

**Evidence:**

- `ui/src/shell/conversationStore.ts:183-187` and `:264-268` only filter/save local rows.
- `service/app.py:322-395` has GET messages and POST/GET attachments, but no delete route.
- The active auth branch adds `require_session` to those routes but still has no delete endpoint
  (`origin/feat/x5bz.2-invite-auth:service/app.py:392-470`).
- Auth D/E/F Beads cover role/ownership, frontend session wiring, and secret/deploy work; none
  includes account deletion.
- b8o D6 only owns retention of inactive strategy rows
  (`game-guide-ai-model-routing.md:706-719`; `agent-forge-harness-b8o.5` AC).
- Root `bd search "account deletion"`, `"delete account"`, and `"conversation delete"` returned no
  issues. — Confidence: **Confirmed**

**Suggested correction:** Assign deletion explicitly before M4, either in Auth D/F plus M1 or in new
Beads. Specify:

- owner-checked `DELETE /conversations/{id}` with idempotent 204/not-found behavior;
- UI behavior on server failure, retry, and local-row removal;
- summary invalidation/rebuild for any supported message-level edit/delete;
- authenticated account deletion, re-auth/CSRF expectations, and cascade scope;
- provider/Langfuse retention disclosure after local deletion; and
- integration tests proving the canary disappears from database reads, prompt capture, and UI.

### [HIGH] Prompt-assembler ownership and disabled-byte behavior are internally inconsistent — M-D2 / M3 / M1

**What:** M3 is presented as the first, independently shippable PR and its Green phase adds "prompt
assembler integration" (`plan:10-19`, `:1123-1158`). M-D2 says
`assemble_generation_prompt()` is added after b8o.1's `assemble_context()` (`plan:243`), while M1's
Green phase also says to add `assemble_generation_prompt()` (`plan:1180-1186`). b8o.1 is open and is
not listed as an M3 prerequisite. In addition, the canonical renderer always describes added memory
precedence text in the system message (`plan:326-376`), while the disabled contract requires the
system message to be byte-identical to today's prompt (`plan:396-409`).

**Why it's an issue:** Two PRs and a prerequisite plan currently appear to own the same seam. An
implementer following the M3 order must either invent the seam before b8o.1, implement preferences
directly in `generate_answer()` and refactor again, or wait despite "ship independently." The
unconditional-looking memory policy text would also fail the explicit byte-equivalence AC when no
memory is present.

**Evidence:** Today `generate_answer()` directly creates exactly one `SystemMessage` and one
`HumanMessage` (`service/generate.py:207-234`); there is no prompt-assembler abstraction.
`agent-forge-harness-b8o.1` is open and owns only the pure `assemble_context()` extraction. M1's
Bead depends on b8o.1; M3 has no such dependency. Current system content is the selected
`PERSONA_PROMPTS` string, including the existing grounding suffix (`service/generate.py:34-97`,
`:226-232`). — Confidence: **Confirmed**

**Suggested correction:** Pick one owner and sequence. Two viable choices:

- make b8o.1 a prerequisite for M3, have M3 create `assemble_generation_prompt()`, and have M1 only
  extend it with memory; or
- explicitly have M3 create a context-string-based prompt assembler before b8o.1 and amend b8o.1 to
  integrate with that already-existing seam.

Also state that memory-specific precedence text is rendered only when a memory block is non-empty;
with default preferences and no memory, return the exact legacy persona and grounded user template.

### [MEDIUM] The existing feedback Bead does not deliver the prerequisite contract claimed here — PR 4a

**What:** PR 4a says to use `agent-forge-harness-x5bz.4` to ship thumbs, bounded reason codes, and
regenerate before M4 (`plan:1250-1253`). That Bead currently requires thumbs with an optional
free-form comment, persisted conversation/message/trace IDs, and a negative-feedback query. It does
not require regenerate or bounded reason codes.

**Why it's an issue:** Closing x5bz.4 as currently written would not satisfy the plan's memo-learning
input contract, while implementing PR 4a as written expands the approved Bead unnoticed. The
optional comment also needs an explicit rule: it may be retained for tester debugging, but must not
enter the first memo-learning dataset if M4 remains bounded-reason-code only.

**Evidence:** `bd show agent-forge-harness-x5bz.4` lists optional comments and trace linkage in its
description/AC and contains no regenerate/reason-code requirement. The M4 design limits the first
learning release to bounded reason codes and no free-form feedback (`plan:784-802`). — Confidence:
**Confirmed**

**Suggested correction:** Amend x5bz.4's AC or create a separate prerequisite Bead for regenerate
and bounded reason codes. State whether legacy/free-form comments are excluded from memo generation
and tracing/export.

### [MEDIUM] The documented dependency graph and actual Beads graph disagree — Dependency and sequencing graph

**What:** The plan makes b8o.5 a prerequisite for M2 and draws M2 as a prerequisite for M4
(`plan:1209-1215`, `:1293-1307`). The actual `1ka.2` Bead depends only on M1 and b8o.2. The actual
`1ka.4` Bead depends only on M3, and M2 is not even listed in the plan's own M4 prerequisite list
(`plan:745-754`).

**Why it's an issue:** `bd ready` can expose M2 before its `call_purpose`/retention dependency is
available, and it can expose M4 without the worker infrastructure the plan says it reuses. Conversely,
if M2 is not truly required for M4, the diagram unnecessarily blocks long-term facts/style work.

**Evidence:** `bd show agent-forge-harness-1ka.2` reports dependencies on `1ka.1` and `b8o.2`; `bd
show agent-forge-harness-1ka.4` reports only `1ka.3`. The plan's "Recommended Beads corrections"
mentions Auth D, x5bz.4, and migration ownership, but not these two local graph mismatches
(`plan:1310-1316`). — Confidence: **Confirmed**

**Suggested correction:** Add the local b8o.5 dependency to `1ka.2`. Decide whether M4 truly depends
on M2: add the edge and M4 prerequisite if shared durable worker infrastructure is mandatory, or
remove `M2 --> M4` and specify the independent job runner M4 will use.

### [MEDIUM] The M4 target schema omits two objects its own contract requires — Profile memory / experiment gate

**What:** The target DDL creates `profile.memory_facts` and `profile.style_memos` without first
creating the `profile` schema (`plan:518-542`). The experiment section says assignment is stored
server-side (`plan:804-808`), but neither table has an experiment assignment/revision field and no
separate experiment-assignment table is defined.

**Why it's an issue:** The shown DDL fails on a fresh database unless another migration happens to
create `profile`. More importantly, a deterministic, auditable user-level experiment cannot be
reconstructed from the proposed rows, particularly for users who have no style memo yet or delete
one. That weakens the preregistered A/B gate.

**Evidence:** Neither `origin/master` nor `origin/feat/x5bz.2-invite-auth` contains `CREATE SCHEMA
profile`; the auth branch only creates `auth` (`origin/feat/x5bz.2-invite-auth:vector-db/init/05-auth-schema.sql:7-15`).
The proposed profile tables contain facts and memo state only (`plan:523-542`). — Confidence:
**Confirmed**

**Suggested correction:** Add `CREATE SCHEMA IF NOT EXISTS profile` and define a bounded,
server-owned experiment assignment record with experiment key/revision, arm, assignment timestamp,
and user FK/cascade. Keep the assignment out of metrics except for the bounded arm/enabled label.

### [MEDIUM] The privacy contract calls the Langfuse SDK “pinned,” but the repository has no exact resolution — M-D11 / tracing

**What:** The plan says the repo "currently pins Langfuse v3" and relies on a "pinned-SDK" masking
contract test (`plan:252`, `:869-883`, `:1099`). The dependency is only constrained to
`langfuse>=3,<4`, and the repository has no `uv.lock` or other application dependency lock.

**Why it's an issue:** The plan correctly notes that the legacy `mask` hook does not cover every
third-party OpenTelemetry attribute. A privacy test tied to one resolved minor/patch is not a
durable deployment contract if clean builds may resolve another v3 release. Calling it pinned
obscures this remaining prerequisite.

**Evidence:** `pyproject.toml:27` declares `langfuse>=3,<4`; no `uv.lock`,
`requirements.lock`, or equivalent application lock is tracked. Langfuse's official masking docs
say the legacy Python `mask` hook only applies to data set through Langfuse SDK APIs and does not
inspect final raw OpenTelemetry attributes from third-party instrumentation:
<https://langfuse.com/docs/observability/features/masking>. — Confidence: **Confirmed**

**Suggested correction:** Say "major-version constrained" for current state. Before enabling memory
tracing, pin an exact tested Langfuse/LangChain combination or introduce a committed lock/resolution,
then make the canary test exercise the actual `CallbackHandler` serialization path used in
production. Keep the documented fail-safe of disabling content traces when that test cannot prove
suppression.

## Verified as accurate (spot-checks)

- `MessageStore` exposes `append`, `recent`, `append_attachment`, and `attachments_for`;
  `recent()` selects newest rows and returns them oldest-first — `service/history.py:73-89`,
  `:177-192` ✓
- Current chat tables have no owner, conversation FK, or turn identifier —
  `service/history.py:31-58`, `vector-db/init/04-chat-schema.sql:7-34` ✓
- `/chat` fetches attachments, generates, then performs two independent best-effort writes, so a
  half-turn is possible — `service/app.py:202-240`, `:248-280` ✓
- Current generation sends one persona `SystemMessage` and one grounded `HumanMessage`;
  stored history is not used for generation — `service/generate.py:207-234`,
  `service/graph.py:203-229` ✓
- `ChatRequest` and the UI currently carry only prompt, mode, and optional conversation ID —
  `service/models.py:59-62`, `ui/src/api.ts:34`, `:94-106` ✓
- Browser conversation IDs are client-generated UUIDs and metadata is localStorage-backed —
  `ui/src/shell/conversationStore.ts` and companion b8o D6 evidence ✓
- The current user is a browser-local stub on master — `ui/src/shell/currentUser.tsx:1-8`,
  `:42-50`, `:73-108`; Auth E's Bead explicitly replaces it with `/auth/me` ✓
- The active auth branch creates `auth.users.id` as `BIGSERIAL`, uses signed HTTP-only cookies, and
  guards the chat/history/attachment routes without yet passing user ownership into history queries
  — `origin/feat/x5bz.2-invite-auth:vector-db/init/05-auth-schema.sql:7-29`,
  `:service/session.py`, `:service/app.py:317-323`, `:392-470` ✓
- Auth D/E/F IDs and responsibilities match their root Beads; the M1 ownership launch gate is
  load-bearing, not optional ✓
- b8o.1 owns `assemble_context()` plus the provider factory/result seam; b8o.2 owns strategy binding
  and new-conversation-on-change; b8o.5 owns bounded attempt/cost/retention observability ✓
- M-RULE no longer overclaims interchangeability. The memory plan and companion routing plan both
  exclude hidden reasoning, keep K3 out of Auto, and qualify K3 memory use. Kimi's official K3
  limitations explicitly warn that missing thinking history or switching into K3 can make output
  unstable: <https://www.kimi.com/blog/kimi-k3> ✓
- Kimi's official prompt guide recommends delimiters and threshold/asynchronous conversation
  summarization: <https://platform.kimi.ai/docs/guide/prompt-best-practice> ✓
- Google warns against post-response background routines under request-based Cloud Run billing and
  documents authenticated Cloud Tasks delivery to private Cloud Run services:
  <https://docs.cloud.google.com/run/docs/tips/general> and
  <https://docs.cloud.google.com/run/docs/triggering/using-tasks> ✓
- OpenAI's endpoint table reports no training for API endpoints, 30-day abuse-monitoring retention
  for Chat Completions, no application state for Chat Completions (subject to listed exceptions),
  and until-deleted retention for `/v1/conversations`:
  <https://developers.openai.com/api/docs/guides/your-data#default-usage-policies-by-endpoint> ✓
- PostgreSQL documents default-deny with RLS enabled and no policy, plus normal table-owner bypass
  unless forced: <https://www.postgresql.org/docs/17/ddl-rowsecurity.html> ✓
- Langfuse documents client-side masking before transmission and warns that server-side ingestion
  events can be written before masking; the plan's client-side/fail-safe posture is justified:
  <https://langfuse.com/self-hosting/security/data-masking> ✓
- The "Lost in the Middle," LongMemEval, and LoCoMo summaries match their papers. LongMemEval's
  abstract names the five cited abilities and reports the stated 30% sustained-interaction accuracy
  drop ✓
- Cost arithmetic is correct:
  2,000 added tokens × 1,000 turns = 2 MTok, producing $0.10/$0.28/$0.30/$6.00 at the listed input
  prices; the 4,000-input + 500-output summary examples produce $0.0004/$0.0007 per refresh and
  $0.05/$0.0875 per 1,000 turns at one refresh per eight turns ✓
- The proposed `memory_jobs` partial uniqueness now correctly separates conversation-scoped summary
  jobs from user-scoped style-memo jobs (`plan:475-516`) ✓
- The UI demo command uses the existing `test` script and the referenced
  `ui/src/shell/ProfilePage.test.tsx` exists — `ui/package.json` ✓

## Not verified

- Provider prices were not independently re-priced during this review. Their arithmetic and
  transcription match the companion routing plan's 2026-07-24 table; the plan already requires a
  paid-eval recheck.
- The future `memory_worker --once` command, fake-provider mode, Cloud Tasks endpoint, and future
  `test_memory*.py` paths do not exist yet. They are plan contracts rather than current facts; the M2
  demo should eventually state the exact flag/config that selects the claimed in-memory/fake path.
- Exact summary/fact/memo character, token, count, refresh, retry, and lease values remain
  intentionally undecided. The plan correctly requires measurement/configuration, but those
  operational limits are not implementation-ready until set.
- No live provider call, Langfuse export, Cloud Tasks delivery, or Postgres migration was performed;
  the review checked current code, proposed contracts, first-party documentation, and arithmetic.

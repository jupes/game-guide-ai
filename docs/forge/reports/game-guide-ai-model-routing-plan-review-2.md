# Second-pass plan review: game-guide-ai-model-routing

Source: `docs/forge/plans/game-guide-ai-model-routing.md` · Reviewed: 2026-07-24 (independent second pass)

## Verdict: CHANGES REQUIRED — 2 Blocker / 3 High / 4 Medium / 2 Low

> **Status: all 11 findings applied to the plan on 2026-07-24.** Blockers 1–2, Highs 3–5, and
> Mediums 6–9 are now settled decisions **D1–D7** plus targeted plan edits; Lows 10–11 are corrected
> in place. Acceptance criteria on `agent-forge-harness-b8o.1`–`.5` were updated to match. See the
> [resolution table](#resolution) at the end of this document. The findings below are retained as
> the record of *why* each decision exists — they are the WHY behind D1–D7, not open work.

The first review recorded **SOUND — 0 findings**. That verdict does not hold up. It verified the
plan's *internal* coherence and its *provider research* thoroughly — both are genuinely strong —
but it did not test the design against the seams the code actually exposes. Every finding below
comes from reading the implementation the plan proposes to modify.

The strategic content is good and should survive: the scope boundary (generation only, embeddings
untouched), the refusal to equate "open-weight" with "cheap", the deterministic no-router-LLM
stance, the policy-gated fallback, and the cost analysis. **All 24 cost figures and multipliers in
both tables recompute correctly** from the quoted list prices; the arithmetic is sound.

What is missing is the last mile: several load-bearing designs have no attachment point in the
current code, and several new failure modes have no defined contract. An executor following this
plan will hit each of these and have to invent an answer, which is exactly where two reasonable
engineers diverge.

---

## Findings

### Blocker 1 — `conversation_id` is nullable, and the entire binding design assumes it is not

**What.** The plan's conversation-affinity design is keyed on `conversation_id` as a primary key:
"The **first accepted request**, before any provider call, atomically inserts this record"
(`plan:553`), with `409` on a concurrent mismatch. It never defines behavior when
`conversation_id` is `null`.

**Evidence that null is a real, supported path — not a theoretical one:**

- `service/models.py:62` — `conversation_id: str | None = Field(None, description="Carried through; persistence is stubbed")`
- `ui/src/api.ts:106` — the browser explicitly sends `conversation_id: conversationId ?? null`
- `service/app.py:210` and `:227` — both `_persist_turn` and `_fetch_attachment_context` early-return when it is `None`. Null is a designed-for state, not an error.

**Why it matters.** Three defensible implementations exist and they are behaviorally different:

1. Reject null with `422` once `model_preference` is present — breaks the existing anonymous path.
2. Bind nothing and honor the request's preference for that single turn — silently abandons the
   affinity invariant the plan spends a section establishing.
3. Synthesize a server-side ID — invents persistence the plan never scoped.

The plan's own TDD behavior 6 ("first accepted request atomically binds … a concurrent mismatch
gets `409`") cannot be written as a test until this is decided, because there is no key to bind on.

**Required resolution.** State it explicitly in the plan. Recommended: a null `conversation_id`
means *stateless single turn* — no strategy row is written, `model_preference` applies to that turn
only, and `routing.effective` is still disclosed. Add a TDD row asserting a null-conversation
request with a manual alias performs no strategy-table write.

---

### Blocker 2 — Checkpoint 3's capture/replay design has no seam to attach to

**What.** The plan requires (`plan:665-669`) capturing a `RetrievalFixture` containing "the
question, mode, ordered chunk IDs, full-text hashes, retrieval revision, and **assembled context**",
then replaying "every candidate generator against that exact assembled context."

**Evidence that no such seam exists:**

- `RagService`'s entire public surface is `answer` (`service/rag.py:103`) and
  `answer_with_contexts` (`:113`). Both call `self._compiled_graph().invoke(...)`.
- The compiled graph runs `START → preflight → … → gate → generate → suggest → cite → END`
  (`service/graph.py:278-306`). There is no interrupt, no subgraph boundary, and no way to halt
  after `gate`.
- Critically, **the assembled context is built inside `generate_node`** (`service/graph.py:203-227`),
  not before it. That includes the attachment-numbering logic that continues the `[1..N]` sequence:
  `n = len(context_texts(result, CONTEXT_TOP_N)) + 1` and the
  `f"[{n}] (Attachment — {label}): {capped}"` block.
- `RagService.retrieve` at `service/rag.py:45,54` is **not** a retrieval seam — it is the
  `SecondaryRetriever` protocol / `StubSecondaryRetriever`, a different concern.

**Why it matters.** To capture "assembled context" today you must either (a) reimplement
`generate_node`'s assembly in the eval harness, or (b) run generation and throw the answer away.
Option (a) means the fixture drifts from production the first time attachment handling changes —
and the plan's own drift guard ("fail the comparison if the ordered identities or hashes differ")
would then be validating a reimplementation against itself, which is not a check at all. Option (b)
defeats the purpose and costs a baseline generation per case.

**Required resolution.** Add a refactor step to **Checkpoint 1 or 3**, before the eval work:
extract context assembly into a pure function — `assemble_context(result, attachment_context,
attachment_label, top_n) -> str` in `service/generate.py` — and have both `generate_node` and the
capture harness call it. Then the fixture records the output of the same function production uses,
and the hash check is meaningful. Add this to "Likely files" and to TDD behavior 15.

---

### High 3 — Spell mode makes two LLM calls, but the routing contract, metrics, and trace metadata are all singular

**What.** The plan mandates that spell suggestions "route to the approved economy model **regardless
of the answer tier**" (`plan:258-259`, `plan:813`). So in spell mode two different models run for
one logical turn. Three separate contracts cannot express that:

1. **Response.** `ChatResponse.routing` (`plan:522-534`) has exactly one `effective` field. The
   suggestion model is undisclosed. This also quietly contradicts "Manual selection wins"
   (`plan:584`) — a user who explicitly picks Kimi still gets economy suggestions and is never told.
2. **Metrics.** The per-attempt dimension list (`plan:614-623`) has no `call_purpose`
   (`answer` | `suggestions`). Without it, answer cost and suggestion cost are indistinguishable,
   and "p95 latency by effective model" silently averages two different call classes. The plan's
   "cost per successful answer" gate (`plan:689`) is then unmeasurable as specified.
3. **Tracing.** `service/rag.py:131` calls `build_trace_config(model=self.model, mode=mode)` —
   one model stamped on the whole graph run, from the service-level attribute. `service/tracing.py`
   `trace_metadata(*, model, mode, version)` takes a single scalar `model`.

**Required resolution.** Add `call_purpose` as a bounded metric/attempt dimension. Either extend
`routing` to a list of per-call entries or add an explicit `suggestions_routing` field. Move trace
model metadata from the run level to the generation-span level so each call carries its own model.
Add a TDD row: *spell mode with a manual premium alias emits two attempts with different effective
models and discloses both.*

---

### High 4 — No HTTP status contract for the new failure modes; `409` is a dead end in the UI

**What.** The plan introduces at least six new failure modes — `409` strategy mismatch, budget
rejection, `content_filter`, `authentication`, `quota`, `upstream_unavailable` (`plan:456-458`,
`556`, `591-592`) — and maps none of them to HTTP statuses or UI treatment.

**Evidence.** Today the contract is narrow and closed:

- `service/app.py` — `except _LLM_ERRORS → 502`, `_DB_ERRORS → 503`, `EmbeddingUnavailableError → 503`.
  `_LLM_ERRORS` is `(openai.APIError,)` (`service/app.py:65`).
- `ui/src/api.ts:115-136` handles exactly `422`, `503`, then falls through to
  `` `Unexpected response (${res.status}).` ``

So the plan's `409` — which it says "tells the UI to start a new conversation" (`plan:556-557`) —
renders to the user as **"Unexpected response (409)"** with no recovery path. Checkpoint 2 does not
list any UI status handling. Likewise, mapping a non-retryable `authentication` or `quota` failure
onto the existing `502` tells the client to retry something that will never succeed.

**Required resolution.** Add an explicit table to the plan and wire it in Checkpoint 2:

| Category | Status | UI treatment |
|---|---:|---|
| strategy mismatch | 409 | Offer "Start a new conversation with this model" |
| budget/daily cap exceeded | 429 | Explain cap, no retry affordance |
| `rate_limit` | 429 | Retry-after |
| `content_filter` | 422 | Explain refusal; do not retry |
| `authentication` / `quota` | 502, logged, **no** fallback | Generic unavailable |
| `upstream_unavailable` / `timeout` | 502 | Retry affordance |

---

### High 5 — Langfuse native cost will silently be null for every new provider

**What.** The plan states "Use native Langfuse model observations for token/cost/latency"
(`plan:630-631`) and treats Langfuse as the durable cost store.

**Why it matters.** Langfuse derives cost by matching the observation's model name against its own
model-price table. `gpt-4o-mini` matches out of the box, which is why this works today. Model IDs
like `qwen3.7-plus-us`, `deepseek-v4-flash`, and `kimi-k3` will not match — the observation records
tokens but cost resolves to null. Nothing errors. Dashboards render zero or blank, and the
"quality-adjusted cost" and "cost per successful answer" launch gates (`plan:639`, `689`) quietly
measure nothing.

The plan gestures at this with "provider-reported cost where available, otherwise an estimate with
`price_revision`" (`plan:622`) but never names the actual operational requirement.

**Required resolution.** Make it explicit in Checkpoint 5: for each enabled alias, register a
Langfuse **custom model definition** (match pattern, unit, input/cached/output prices) as part of
the same reviewed change that adds the catalog entry, and version it alongside `price_revision`.
Add a TDD/verification row: *an observation for each non-OpenAI alias resolves a non-null cost.*
Name the owner — this is config in Langfuse, not code, so it will be missed otherwise.

---

### Medium 6 — `svc.llm_client` is one service-level client; per-attempt routing cannot be expressed through it, and the test seam changes underneath every existing test

**What.** The plan's factory produces an "effective model per attempt" (`plan:413`), but the current
injection point is a single client stored on the service.

**Evidence.**

- `service/rag.py:65,71` — `RagService(model=..., llm_client=...)`, both stored per-service.
- `service/graph.py:223,241` — both `generate_answer` and `generate_suggestions` receive
  `model=svc.model, client=svc.llm_client`.
- **In `service/generate.py:205-210` and `:224-228`, `model` is only used when `client is None`.**
  When a client is injected, the `model` argument is dead.

**Why it matters.** Every existing test injects `llm_client` — `service/tests/test_graph.py:85,205,220,280,295`,
`service/tests/test_service.py:138,150,163,174` — and so does the eval harness
(`ingestion/compare_models.py:123`). After routing lands, those tests would pass a client that
ignores routing entirely: **the tests would keep passing while exercising none of the new code.**
That is the failure mode most likely to produce a false green.

**Required resolution.** Say plainly that the seam moves from "one injected client" to "an injected
`ProviderClientFactory`", list the test migration as explicit Checkpoint 1 work, and add a guard
test asserting that a routed call resolves its client through the factory rather than a
service-level attribute.

---

### Medium 7 — Strategy rows are keyed on unauthenticated, client-generated UUIDs with no bound or TTL

**What.** The new `chat.conversations` table is keyed on an ID the browser invents:
`crypto.randomUUID()` in `ui/src/shell/conversationStore.ts:63`, stored only in localStorage. The
pilot has no authentication (the plan itself notes auth is a prerequisite for account-scoped
preferences, `plan:889`).

**Why it matters.** Two concrete problems the plan does not address:

- **Unbounded growth / cheap DoS.** Any client can POST arbitrary `conversation_id` values and
  create a row per request, before any provider call and therefore before any budget guard. The
  plan hardens against metric cardinality (`plan:388`) but not against row cardinality.
- **Orphans.** Clearing localStorage or switching browsers strands every row permanently. No
  retention or cleanup policy is specified.

**Required resolution.** Add a bound (validate UUID format, cap rows per source, or only create the
row on an otherwise-accepted request that passes the rate limiter) and a retention policy. This
should be coordinated explicitly with `agent-forge-harness-x5bz.3`, which owns rate limiting.

---

### Medium 8 — `qwen3.6-flash` is in the shortlist but absent from both cost tables and from the eval list

**What.** The shortlist (`plan:155`) includes `qwen3.6-flash` at `$0.165 / $0.99`, "global scope".
It appears in **neither** cost table (`plan:211-224`, `229-242`), and Checkpoint 3 says only "Qwen
candidates" (`plan:796`) without naming which.

**Why it matters.** The executor cannot tell whether to evaluate it. Compounding this, open decision
4 — "Which Qwen models are actually US-scoped versus globally routed at the Virginia endpoint?"
(`plan:910`) — *gates* the eval set, but Checkpoint 3 has no step to resolve it before spending on
comparisons. That ordering dependency is invisible in the build sequence.

**Required resolution.** Either drop `qwen3.6-flash` with a one-line reason (global scope fails the
residency preference) or add it to the cost tables and name it. Add an explicit Checkpoint 3 step 0:
confirm residency scope per Qwen ID and freeze the candidate list before running paid comparisons.

---

### Medium 9 — `MetricLabels` is a shared service+UI contract with `extra="forbid"`, and `route_template` has no `/models`

**What.** `service/metrics.py:21-31` — `MetricLabels` uses `ConfigDict(extra="forbid", strict=True)`
and `route_template` is a closed `Literal["/", "/chat", "/metrics/ui"]`.

**Why it matters.** Two practical consequences the plan skips: this model is shared with the
UI-posted metrics path (`_UI_LABELS`, `/metrics/ui`), so extending it is a cross-surface contract
change requiring UI-side coordination; and the new `GET /models` endpoint is not in the
`route_template` enum, so any metric recorded against it raises. Small, but it fails at runtime, not
at typecheck.

**Required resolution.** Note in Checkpoint 5 that `route_template` gains `/models` and that
`_SERVICE_LABELS` / `_UI_LABELS` must be updated as separate frozensets so routing labels do not
leak into the UI metric surface.

---

### Low 10 — "Keep `gpt-4o-mini` behavior unchanged" and `max_retries=0` are in tension in the same checkpoint

Checkpoint 1 promises to "keep `gpt-4o-mini` answer behavior unchanged" (`plan:746`) and in the next
step sets `max_retries=0` (`plan:747`). Disabling SDK-level retries **is** a behavior change to the
baseline — `ChatOpenAI` retries by default — and it will move existing latency and error-rate
metrics. The plan intends the service-owned retry to compensate in the same checkpoint, but should
say so explicitly and require a before/after check that baseline resilience and the existing
`service.chat.*` metrics are preserved end to end.

---

### Low 11 — The plan's own header self-certifies the review verdict

`plan:6` reads "Status: reviewed; plan review verdict SOUND (0 findings)". Given that this pass
found two Blockers against the same document, that line will mislead a future reader into skipping
verification. Recommend replacing it with a dated pointer to the review artifacts and their current
verdict, rather than an embedded pass/fail claim.

---

## What the plan gets right (keep these)

- **Scope boundary.** Generation-only, embeddings untouched, with the 1536-dim contract and re-ingest
  cost correctly identified as the reason. Verified against `ingestion/retrieval.py`.
- **Cost analysis.** All 24 figures across both tables recompute exactly from the stated prices, and
  the 600-vs-1,800-output sensitivity table is the right way to frame reasoning-model risk.
- **Refusing the easy narrative.** "Chinese/open-weight ≠ cheapest", Kimi K3 costing more than
  GPT-5.6 Terra at cache-miss, and the warning against projecting coding-workload cache-hit rates
  onto rotating RAG chunks are all correct and non-obvious.
- **Deterministic routing with no router LLM**, and fallback gated on policy rather than convenience.
- **DeepSeek treated as evaluation-only** pending written retention/residency terms — the right call
  given the public policy text.
- **The `compare_models.py` heuristic replacement** is well-targeted: `ingestion/compare_models.py:66`
  is literally `_ollama(label) if ":" in label else _openai(label)`, which would silently send
  `qwen3.7-plus-us` to OpenAI's endpoint.

---

## Clean executable path forward

Resolve these **before** Checkpoint 1 opens. Each is a decision, not a task:

| # | Decision | Recommended default |
|---:|---|---|
| D1 | Null `conversation_id` semantics | Stateless single turn; no strategy row; preference applies to that turn only |
| D2 | Where context assembly lives | Extract `assemble_context()` into `service/generate.py`; both graph and eval call it |
| D3 | Disclosure of the second (suggestions) call | Add `call_purpose` dimension + surface suggestion model in the response |
| D4 | HTTP status per error category | Adopt the table in Finding 4 |
| D5 | Langfuse cost for non-OpenAI models | Custom model definitions registered with each catalog addition; owner named |
| D6 | Strategy-row growth bound + retention | Create only on rate-limiter-accepted requests; add TTL; coordinate with `x5bz.3` |
| D7 | Final Qwen candidate list | Resolve residency scope first; drop or price `qwen3.6-flash` |

Then the checkpoint sequence needs two structural edits:

- **Checkpoint 1** gains the `assemble_context()` extraction (D2), the `llm_client` → factory test
  migration (Finding 6), and an explicit baseline-resilience check (Finding 10).
- **Checkpoint 3** gains a step 0 that freezes the candidate list on residency evidence (D7) before
  any paid comparison run.

With D1–D7 written into the plan, the checkpoints are independently deliverable as structured and
the TDD table becomes fully implementable. Without them, Checkpoints 2 and 3 each contain at least
one decision the executor must invent.

## Resolution

Applied 2026-07-24. Every finding is closed in the plan; nothing below is outstanding.

| # | Severity | Finding | Resolution in the plan |
|---:|---|---|---|
| 1 | Blocker | Null `conversation_id` undefined | **D1** — new subsection under Conversation affinity: stateless single turn, no strategy row, `409` unreachable. TDD 6a. |
| 2 | Blocker | No seam for capture/replay | **D2** — new "Context assembly seam" section requiring `assemble_context()` extracted to `service/generate.py`, moved into Checkpoint 1 ahead of Checkpoints 3–4. TDD 9b + characterization tests in Checkpoint 1 Red. |
| 3 | High | Spell mode's two calls not representable | **D3** — `suggestions_routing` added to `ChatResponse`, `call_purpose` added to the attempt/metric dimensions, trace metadata moved to per-span. TDD 9a, 10a. |
| 4 | High | No status contract; `409` a UI dead end | **D4** — new "Error and status contract" section with a category→status→UI table; implementation moved into Checkpoint 2. TDD 6b, 6c. |
| 5 | High | Langfuse cost silently null for new providers | **D5** — custom model definitions required per alias, versioned against `price_revision`, gated before enablement. Checkpoint 5 Red/Green. TDD 12a. |
| 6 | Medium | `llm_client` seam ignores `model`; false-green risk | New "Provider client seam" section documenting the `service/generate.py:192-195`/`:222-227` trap; factory migration and guard test are explicit Checkpoint 1 work; test files listed under Likely files. |
| 7 | Medium | Unbounded, unauthenticated strategy rows | **D6** — creation bounded to rate-limiter-accepted requests with validated IDs, plus a retention job and runbook entry. TDD 12b. |
| 8 | Medium | `qwen3.6-flash` ambiguous | **D7** — excluded with a stated reason in the shortlist, cost-table omission made explicit, candidate list frozen in a pre-step to Checkpoint 3; open decision 4 marked as blocking. |
| 9 | Medium | `MetricLabels` cross-surface / missing `/models` | Called out in the metrics section with exact line references; `route_template` extension is Checkpoint 5 Green work. |
| 10 | Low | `max_retries=0` vs "behavior unchanged" | Checkpoint 1 step 5 now requires both to ship together and to verify baseline resilience and existing `service.chat.*` metrics. |
| 11 | Low | Self-certifying header | Replaced with a dated pointer to both review artifacts. |

Two follow-ups are deliberately left to the executor rather than pre-decided, because they need
evidence this review cannot supply: the exact retention window for D6, and whether `suggestions_routing`
is better expressed as a list of per-call routes if a third call is ever added.

## Not verified

- Provider adapter wire compatibility, latency, quality, and usage-metadata fidelity — correctly
  deferred by the plan to contract fixtures and spend-capped live runs.
- Rights to transmit licensed corpus excerpts, and non-public provider retention/residency terms.
- Current list prices were checked for internal arithmetic consistency only; they were not
  re-fetched from provider pages in this pass.

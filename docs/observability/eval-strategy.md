# Eval strategy & platform decision — rag-chat

Status: **Accepted** · Date: 2026-07-01 · Epic: `agent-forge-harness-ziw` · Task: `ziw.3` (Phase 2)
Companion: `docs/observability/phase0-langfuse-decision.md` (backend choice) ·
Plan: harness `plans/drafts/rag-chat-observability-evals.md`
Primary source: Anthropic, *Demystifying Evals for AI Agents*
(https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

## Decision (TL;DR)

**Consolidate the eval layer on Langfuse + Ragas.** Langfuse (already wired as the tracing backend)
is also the **datasets / LLM-as-judge scoring / experiment-comparison** backbone. **Ragas** is the
RAG metric library that computes answer-quality scores, which we log onto Langfuse traces/experiments.
**We drop promptfoo from the plan** — Langfuse *experiments* over a versioned dataset already give us
the "is version/model B better than A?" comparison and a CI gate, so a third tool is redundant.

We follow **Anthropic's eval methodology** (the 8-step roadmap below) as the *process*, independent of
tooling.

## Context — why revisit this now

The original plan (`plans/drafts/rag-chat-observability-evals.md`) chose **Ragas + promptfoo +
Langfuse** before two things happened:

1. **We migrated the pipeline to LangGraph + LangChain** (`ziw.2`, PR jupes/rag-chat#11). The service
   now runs as a graph with `langchain-openai` `ChatOpenAI`.
2. **We wired Langfuse tracing** (env-gated, node-level spans; on the free cloud tier). Langfuse is not
   just tracing — it ships **datasets, LLM-as-judge evaluators, scores attached to traces, and
   experiment comparison** out of the box.

Given the plumbing is in place, the decision space shifted from "which tools to add" to "how much can
we **consolidate**." We also already have `ingestion/eval_golden.py` — a **retrieval-only** golden set
(~160 queries; Hit@1 ≈ 83.3%, Recall@10 ≈ 94.3%) — as the seed (Anthropic: "start with what you
already test manually").

## Options considered

Platforms named in the Anthropic post (verbatim descriptions), assessed for rag-chat:

| Platform | Anthropic's description | Fit here |
|---|---|---|
| **Langfuse** | "self-hosted open-source alternative … for teams with data residency requirements" | **Chosen backbone** — already wired; OSS; datasets + judge + experiments cover comparison/CI. |
| **LangSmith** | "tracing, offline and online evaluations, and dataset management with tight integration into the LangChain ecosystem" | Strong (we use LangChain) but **commercial**, and adopting it means abandoning the Langfuse plumbing + OSS posture. Deprioritized. |
| **Braintrust** | "offline evaluation with production observability and experiment tracking" | Commercial; overlaps Langfuse. Deprioritized. |
| **Arize Phoenix / AX** | Phoenix = "open-source platform for LLM tracing, debugging, and offline or online evaluations"; AX = SaaS scale offering | OSS and capable, but we already chose Langfuse in Phase 0; no reason to run two. |
| **Harbor** | "running agents in containerized environments … trials at scale … standardized format for tasks and graders" | Overkill now; revisit only if evals become heavy/agentic at scale. |

Eval libraries (compose *inside* a backbone, not competing platforms):

- **Ragas** (OSS, Apache-2) — RAG metrics: faithfulness/groundedness, answer relevancy, context
  precision/recall, answer correctness. **Chosen** as the metric library.
- **promptfoo** (OSS, MIT) — config-driven A/B across prompts/models + CI gate. **Dropped**: Langfuse
  experiments over a dataset cover the same need without a second config surface.
- DeepEval — pytest-style alternative to Ragas; not needed alongside Ragas.

## Why Langfuse + Ragas (rationale)

- **Reuse the plumbing.** Tracing is already Langfuse; datasets/scores/experiments live in the same
  place, so quality co-locates with latency/token/cost per request (one pane of glass).
- **Fewer moving parts.** Langfuse experiments replace promptfoo's A/B + gate role → one fewer tool to
  wire, version, and maintain.
- **OSS-friendly + cheap.** Langfuse free tier + Ragas (OSS). The only real spend is OpenAI judge
  tokens, bounded by a small golden subset + cadence.
- **Ragas is the right metric layer.** It fills the exact gap `eval_golden.py` leaves (generation
  quality vs retrieval-only), and its scores log cleanly into Langfuse.
- **LangChain-native either way.** Both Langfuse and (the rejected) LangSmith integrate via the
  LangChain callback we already installed; staying on Langfuse loses nothing on integration tightness
  that matters for our graph.

**Revisit triggers** (when this decision should be re-opened): outgrowing the Langfuse free tier;
needing containerized trials at scale (→ Harbor); or a hard requirement for LangSmith's LangChain-tie
that Langfuse can't meet.

## What changes vs the original plan

- **Phase 2 (`ziw.3`)** — unchanged in intent: add generation-quality metrics via **Ragas** over the
  golden subset, scores written back to Langfuse. (Was already Ragas.)
- **Phase 3 (`ziw.4`)** — **change**: implement version/model comparison + regression gate as
  **Langfuse dataset experiments** (matrix over `{service_version} × {model}`, threshold gate in CI)
  **instead of promptfoo**.
- **Phase 4 (`ziw.5`)** — unchanged: Langfuse's built-in dashboard for quality + cost trends.

## The methodology we follow — Anthropic's 8-step roadmap (mapped to rag-chat)

The tooling is the backbone; **this is the process** and it drives quality more than the platform.

0. **Start early / small.** 20–50 tasks from *real failures*, not hundreds of perfect ones. → Seed
   from `eval_golden.py` + a curated answer subset; don't wait for full coverage.
1. **Convert manual testing.** Encode the checks we already eyeball before shipping (grounded answer,
   correct citations, refuses off-corpus) as tasks.
2. **Unambiguous tasks + reference solutions.** Each golden answer task has expected key-facts a
   correct answer must contain; two D&D-literate reviewers should agree on pass/fail.
3. **Balanced sets.** Include **positive** (answerable) *and* **negative** (must-refuse / off-corpus)
   cases — the answerability gate is a first-class behavior to test both ways.
4. **Isolated harness.** Each eval trial runs against a clean call (no shared conversation state);
   deterministic where the pipeline is deterministic (retrieval, gate).
5. **Design graders thoughtfully.** Deterministic where possible (citation present, refusal on
   negatives, key-fact string/regex), **LLM-judge (Ragas) only where necessary** (faithfulness,
   answer-correctness). **Grade the output, not the path** — score the answer, not the node sequence,
   so evals survive future graph refactors. Give judges an **"Unknown"** escape hatch.
6. **Check the transcripts.** Periodically read a sample of graded trials in Langfuse to confirm the
   judges aren't rewarding plausible-but-wrong answers; **calibrate judges against human spot-checks**.
7. **Watch saturation.** If a metric pins near 100%, it only guards regressions — add harder cases to
   regain improvement signal.
8. **Keep the suite healthy.** Treat the golden set as living: prune stale cases, add new real
   failures, record `pass@k`/`pass^k` for a sampled subset to capture non-determinism.

### Grader plan for rag-chat (concrete)

| Behavior | Grader type | How |
|---|---|---|
| Refuses off-corpus / negative queries | Code (deterministic) | assert answer == REFUSAL on negative golden cases |
| Cites sources it used | Code (deterministic) | assert `[n]` citations present + resolve to returned sources |
| Answer contains required key-facts | Code (fuzzy) + LLM fallback | key-fact string/regex; LLM judge if phrasing varies |
| Faithfulness / groundedness | LLM judge (Ragas) | claims supported by retrieved context |
| Answer correctness | LLM judge (Ragas) | vs curated reference answer; "Unknown" allowed |

## Cost posture

- Langfuse: **free tier** (Hobby, ~50k units/mo). Ragas: OSS.
- Real cost = **OpenAI judge tokens**. Guardrail: run Ragas judges over a **small curated subset**
  (start 20–50), **PR-gated subset** (fast/cheap) + **optional nightly full** run; record token cost
  per run. `gpt-4o-mini` judge calls are fractions of a cent each.

## Consequences / follow-ups

- Update the harness plan Phase 3 (`ziw.4`) to Langfuse experiments (was promptfoo); the epic beads
  reflect this decision.
- Hardening (`agent-forge-harness-3xs`): judge tokens are additive spend — keep the subset/cadence cap
  documented; PII already covered by trace retention posture.
- This doc is the historical record for **why** the eval stack is Langfuse + Ragas and **why not**
  LangSmith / Braintrust / Phoenix / promptfoo. Re-open on the revisit triggers above.

## References

- Anthropic, *Demystifying Evals for AI Agents* — https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- Phase 0 backend decision — `docs/observability/phase0-langfuse-decision.md`
- Plan — harness `plans/drafts/rag-chat-observability-evals.md`
- Seed eval — `ingestion/eval_golden.py`

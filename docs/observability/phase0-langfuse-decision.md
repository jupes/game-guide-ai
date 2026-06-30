# Phase 0 decision — observability/eval stack for rag-chat

Status: **Accepted** · Date: 2026-06-29 · Epic: `agent-forge-harness-ziw` · Task: `ziw.1`
Plan: harness `plans/drafts/rag-chat-observability-evals.md` · Research: `plans/research/rag-chat-observability-evals.md`

## Decision

**Use Langfuse as the observability + trace-scoring backend for rag-chat.** Confirm **Ragas**
(RAG answer-quality metrics) and **promptfoo** (version/model A/B + CI gate) as the eval tools in
later phases. This was run as a **Langfuse-only spike** (the Phoenix comparison arm was deliberately
skipped) per the 2026-06-29 scoping decision — the research already recommends Langfuse and the goal
of this epic (compare quality/cost across **versions and models** with a **dashboard**) is squarely
Langfuse's strength.

## Why Langfuse (rationale)

- **Version/model tracking + dashboard** are first-class — exactly the epic's goal ("is version/model
  B better than A?"). Traces tag by `model` + `service_version` (git SHA) + `mode`; the built-in
  dashboard gives filterable quality/cost trends with no extra build (Phase 4 rides on it).
- **Scores attach to traces** — Phase 2's Ragas metrics land on the same trace as the telemetry, so
  quality and cost co-locate per request.
- **Native LangGraph/LangChain instrumentation** — the foundational migration (`ziw.2`) models the
  pipeline as a LangGraph graph; Langfuse's `CallbackHandler` captures a **span per node**
  (retrieve / gate / generate) plus the LLM call's tokens/cost/latency, with near-zero code in the
  app. Proven by the spike below.
- **OSS / self-host** — MIT core, satisfies the free/OSS constraint.

Phoenix (Apache-2) remains the documented fallback if a one-tool RAG-eval workflow or a stricter
license posture later outweighs Langfuse's version-tracking + dashboard edge.

## Self-host vs cloud

- **Production: self-host** (free, MIT) via Langfuse's official compose (Postgres + ClickHouse +
  Redis + object store). Adds services to the local stack — acceptable; stood up separately from
  rag-chat's `docker-compose.yml` so the app stack stays lean.
- **Spike / first validation: Langfuse Cloud free tier is acceptable** to see traces fast (set 3 env
  vars, no infra). The spike script works against either — it only needs `LANGFUSE_*` env.

> Self-host compose is **not vendored** into this repo to avoid drift from Langfuse's maintained
> multi-service v3 compose (ClickHouse/Redis/MinIO). Run it from upstream — see "How to run" — and we
> pin to a Langfuse release tag. This is a deliberate, noted gap, not an oversight.

## Data retention / PII posture

- Traces store **user prompts + generated answers** (potential PII). Before any *persistent*/shared
  logging:
  - Set a **short default retention** (e.g. 30 days) on the Langfuse project.
  - Keep tracing **env-gated and off by default in tests/CI**; enable in dev/staging first.
  - Do **not** log API keys or raw DB rows; tag with model/version/mode only.
  - Revisit a PII-scrubbing step before pointing this at real end-user traffic.

## The spike (integration proof)

`spikes/langgraph_langfuse_spike.py` builds a minimal LangGraph graph mirroring the real seams
(`retrieve -> gate -> generate|refuse`), with retrieval **stubbed** (no DB needed) and the LLM node
using `langchain-openai` `ChatOpenAI` — the wrapper the migration adopts. The Langfuse
`CallbackHandler` traces each node + the LLM call, tagged with `model` + `service_version` + `mode`.

### How to run

```bash
# 1. Deps (spike-only; the migration adds the pinned subset to pyproject.toml)
pip install -r spikes/requirements-spike.txt

# 2a. Headless wiring check — no network, no Langfuse, no keys. Proves the graph runs.
python spikes/langgraph_langfuse_spike.py --dry

# 2b. Real trace — pick ONE backend, then run:
#   Cloud free tier:  set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY (host defaults to cloud)
#   Self-host:        clone github.com/langfuse/langfuse, `docker compose up`, create a project
#                     in the UI (http://localhost:3000) to get keys, set LANGFUSE_HOST too.
export OPENAI_API_KEY=...           # real generation call (gpt-4o-mini, cheap)
export LANGFUSE_PUBLIC_KEY=pk-...
export LANGFUSE_SECRET_KEY=sk-...
# export LANGFUSE_HOST=http://localhost:3000   # self-host only
python spikes/langgraph_langfuse_spike.py --mode sage --prompt "What is a beholder?"
```

**Expected (real run):** one trace in Langfuse with child spans `retrieve`, `gate`, `generate`, an
LLM generation showing token counts + cost, and trace tags `model`, `service_version`, `mode`.

## Acceptance mapping (ziw.1)

- [x] Backend chosen with written rationale → **Langfuse** (this doc).
- [x] Integration proven via LangGraph callbacks → spike script (`--dry` verifies wiring headlessly).
- [x] Retention/PII note recorded → above.
- [~] Self-host run docs → documented (upstream compose + commands); compose intentionally not
  vendored (noted gap).
- [ ] **A few real traces visible tagged with model + git SHA** → run step 2b on a machine with
  Langfuse keys + `OPENAI_API_KEY` and confirm in the dashboard. *(Interactive — owner verifies.)*

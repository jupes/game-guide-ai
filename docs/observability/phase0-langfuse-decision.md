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

> **Direction update (2026-06-30): using Langfuse Cloud (free Hobby tier), not self-host — for now.**
> We trialled the self-host compose locally, then switched to **Langfuse Cloud** to drop the local
> infra (Postgres + ClickHouse + Redis + MinIO) and operational upkeep. The Hobby tier is **free, no
> credit card** (~50k trace units/month), which comfortably covers dev + the eval phases; the only
> real spend is OpenAI tokens (`gpt-4o-mini`, fractions of a cent per call). The code is identical
> either way — it just reads `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` from
> `.env`, where `LANGFUSE_BASE_URL` is the cloud region URL (e.g. `https://us.cloud.langfuse.com`)
> instead of `http://localhost:3000`. **Revisit self-host only if scale/cost/data-residency demands
> it** (e.g. if we outgrow the free tier or need traces to stay on our own infra) — the original
> self-host plan below stays valid as the fallback.

The original self-host plan (retained as the at-scale fallback):

- **Production (fallback): self-host** (free, MIT) via Langfuse's official compose (Postgres +
  ClickHouse + Redis + object store). Adds services to the local stack — stood up separately from
  rag-chat's `docker-compose.yml` so the app stack stays lean.
- **Spike / first validation: Langfuse Cloud free tier** to see traces fast (set 3 env vars, no
  infra). The spike script works against either — it only needs `LANGFUSE_*` env.

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

The spike **auto-loads the repo-root `.env`** (the same file docker `env_file` uses), so if your
`.env` already holds `OPENAI_API_KEY` + `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY`
(+ `LANGFUSE_BASE_URL` for self-host) you don't need to export anything. Explicit process env still
wins over `.env`.

```bash
# 1. Deps (spike-only; the migration adds the pinned subset to pyproject.toml)
pip install -r spikes/requirements-spike.txt        # or: uv pip install -r spikes/requirements-spike.txt

# 2a. Headless wiring check — no network, no Langfuse, no keys. Proves the graph runs.
python spikes/langgraph_langfuse_spike.py --dry

# 2b. Real trace — backend creds come from .env (self-host or cloud). For self-host,
#     have Langfuse running first (github.com/langfuse/langfuse → docker compose up).
python spikes/langgraph_langfuse_spike.py --mode sage --prompt "What is a beholder?"
```

`LANGFUSE_BASE_URL` is the canonical host var (e.g. `http://localhost:3000` for self-host);
`LANGFUSE_HOST` is the deprecated alias.

**Expected (real run):** one trace in Langfuse with child spans `retrieve`, `gate`, `generate`, an
LLM generation showing token counts + cost, and trace tags `model`, `service_version`, `mode`.

**Verified 2026-06-29:** real run succeeded end-to-end against self-hosted Langfuse
(`localhost:3000`) — `gpt-4o-mini`, `service_version=23e7ce3`, `mode=sage`, trace accepted.

## Acceptance mapping (ziw.1)

- [x] Backend chosen with written rationale → **Langfuse** (this doc).
- [x] Integration proven via LangGraph callbacks → spike script (`--dry` verifies wiring headlessly).
- [x] Retention/PII note recorded → above.
- [~] Self-host run docs → documented (upstream compose + commands); compose intentionally not
  vendored (noted gap).
- [x] **A real trace emitted tagged with model + git SHA** → verified 2026-06-29: real run sent a
  trace to self-hosted Langfuse (`localhost:3000`). Final step for the owner: open the Langfuse
  dashboard and eyeball the `retrieve`/`gate`/`generate` spans + tags, then close `ziw.1`.

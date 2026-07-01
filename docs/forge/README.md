# Forge artifacts

Planning artifacts (research → plan → ship reports) produced by the Agent Forge
pipeline while building features in this repo. They were previously kept in the
`agent-forge-harness` repo and have been relocated here so each project's
documents live in the repo they belong to.

## Layout

- `plans/` — implementation plans (the `/forge-plan` output).
- `research/` — research notes gathered before planning (`/forge-research`).
- `reports/` — ship reports and plan reviews (`/forge-ship`, `/review-plan`).

Files are grouped by feature slug (e.g. `dnd-cross-encoder-reranker`,
`rag-chat-observability-evals`). They are point-in-time records of how a change
was researched, planned, and shipped — not living documentation.

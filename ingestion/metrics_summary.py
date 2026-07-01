"""
Quality/cost metrics summary for rag-chat (ziw.5 / Phase 4).

A scriptable companion to the Langfuse dashboard: pulls a quality + cost summary
grouped by model (and version) via the Langfuse **Metrics API**, so the numbers
aren't trapped in the UI. Falls back to summarizing our own comparison results
JSON when a live query isn't wanted.

Dashboard setup + how to read it for an A/B decision: `docs/observability/dashboard.md`.
All the data already flows from Phases 1-3 (traces tagged model/service_version/mode +
latency/tokens/cost, ragas_* scores). This module only reads/visualizes it.

Checkpoints: CP1 (here) pure query-builder + formatter + results-fallback (offline);
CP2 the live Metrics-API call; CP3 the dashboard doc.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Sequence

_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


def _since_to_delta(since: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([dhm])", since.strip())
    if not m:
        raise ValueError(f"bad --since {since!r}; expected like 7d / 24h / 30m")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def build_query(*, view: str, measures: Sequence[tuple[str, str]], group_by: str | None,
                since: str, now_iso: str) -> dict:
    """Assemble a Langfuse Metrics API v2 query. `measures` = [(measure, aggregation), ...];
    `group_by` = a dimension field (or None). `now_iso` is passed in so the time window is
    deterministic/testable."""
    now = datetime.fromisoformat(now_iso)
    frm = now - _since_to_delta(since)
    return {
        "view": view,
        "metrics": [{"measure": me, "aggregation": ag} for me, ag in measures],
        "dimensions": [{"field": group_by}] if group_by else [],
        "fromTimestamp": frm.isoformat(),
        "toTimestamp": now.isoformat(),
    }


def summarize(rows: Sequence[dict], *, group_key: str, metric_keys: Sequence[str]) -> list[dict]:
    """Reshape Metrics-API result rows into a per-group table: {group, <metric>: value|None},
    sorted by group. Tolerant of a metric missing from a row."""
    table: list[dict] = []
    for r in rows:
        row = {"group": r.get(group_key)}
        for mk in metric_keys:
            row[mk] = r.get(mk)
        table.append(row)
    return sorted(table, key=lambda x: str(x["group"]))


def summary_from_results(results: dict) -> list[dict]:
    """Offline fallback: build the same per-model quality+cost table from a
    `compare_results.json` dict (no Langfuse needed). Quality = per-metric pass_rate;
    cost = the per-model judge token usage."""
    models = results.get("models", {})
    tokens = results.get("judge_tokens_per_model", {})
    table: list[dict] = []
    for label, agg in models.items():
        row: dict = {"group": label}
        for metric, a in agg.items():
            row[metric] = a.get("pass_rate") if isinstance(a, dict) else a
        row["judge_tokens"] = tokens.get(label)
        table.append(row)
    return sorted(table, key=lambda x: str(x["group"]))

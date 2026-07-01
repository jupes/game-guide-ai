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


# --- Live Metrics API ---------------------------------------------------------
# Discovered against Langfuse v3 (2026-07): cost/latency group by MODEL only in the
# `observations` view via the `providedModelName` dimension (trace metadata like our
# `model`/`service_version` is NOT a queryable dimension — only id/name/tags/version/
# release/environment are). Response rows key metrics as `<aggregation>_<measure>`.

# The generator model (cost/latency) lives on observations under this dimension.
MODEL_DIMENSION = "providedModelName"
_COST_MEASURES = [("totalCost", "sum"), ("latency", "p95"), ("totalTokens", "sum")]
_COST_KEYS = ("sum_totalCost", "p95_latency", "sum_totalTokens")


def fetch(langfuse, query: dict) -> list[dict]:  # pragma: no cover - live-only
    """Run a Metrics API query and return its `data` rows (list of dicts)."""
    import json
    resp = langfuse.api.metrics.metrics(query=json.dumps(query))
    return [dict(r) for r in resp.data]


def cost_latency_by_model(langfuse, *, since: str, now_iso: str) -> list[dict]:  # pragma: no cover - live
    """Per-model cost / p95 latency / tokens from the observations view."""
    q = build_query(view="observations", measures=_COST_MEASURES,
                    group_by=MODEL_DIMENSION, since=since, now_iso=now_iso)
    rows = fetch(langfuse, q)
    return summarize(rows, group_key=MODEL_DIMENSION, metric_keys=_COST_KEYS)


def main() -> None:  # pragma: no cover - integration entry
    import argparse
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    parser = argparse.ArgumentParser(description="rag-chat quality/cost metrics summary (Langfuse Metrics API)")
    parser.add_argument("--since", default="7d", help="window: 7d / 24h / 30m")
    parser.add_argument("--from-results", default=None,
                        help="offline: summarize a compare_results.json instead of querying Langfuse")
    args = parser.parse_args()

    if args.from_results:
        results = json.loads(Path(args.from_results).read_text(encoding="utf-8"))
        table = summary_from_results(results)
        source = f"results:{args.from_results}"
    else:
        import config  # loads .env (LANGFUSE_*)  # noqa: F401
        from langfuse import get_client
        now_iso = datetime.now(timezone.utc).isoformat()
        table = cost_latency_by_model(get_client(), since=args.since, now_iso=now_iso)
        source = f"langfuse:observations (since {args.since})"

    print("=" * 72)
    print(f"Quality/cost summary by model -- {source}")
    for row in table:
        cols = "  ".join(f"{k}={row[k]}" for k in row if k != "group")
        print(f"  {str(row['group']):28s} {cols}")
    out = {"source": source, "table": table}
    Path(__file__).parent.joinpath("metrics_summary.json").write_text(
        json.dumps(out, indent=2, default=str, ensure_ascii=False))
    print("Results -> ingestion/metrics_summary.json")


if __name__ == "__main__":  # pragma: no cover
    main()

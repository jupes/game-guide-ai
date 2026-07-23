"""
Quality, cost, and runtime metrics summary for rag-chat.

A scriptable companion to the Langfuse dashboard: pulls quality/cost by model
plus timestamped service/UI score series via the Langfuse **Metrics API**, so
the numbers aren't trapped in the UI. Offline paths summarize comparison
results or the committed legacy-query fixture without credentials.

Dashboard setup + how to read it for an A/B decision: `docs/observability/dashboard.md`.
Native observations provide latency/tokens/cost; typed scores provide the
bounded runtime catalog defined in `docs/observability/metrics-standard.md`.
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


def build_query(
    *,
    view: str,
    measures: Sequence[tuple[str, str]],
    group_by: str | None,
    since: str,
    now_iso: str,
    time_dimension: str | None = None,
) -> dict:
    """Assemble a legacy Langfuse Metrics API v1 query. `measures` = [(measure, aggregation), ...];
    `group_by` = a dimension field (or None). `now_iso` is passed in so the time window is
    deterministic/testable."""
    now = datetime.fromisoformat(now_iso)
    frm = now - _since_to_delta(since)
    query = {
        "view": view,
        "metrics": [{"measure": me, "aggregation": ag} for me, ag in measures],
        "dimensions": [{"field": group_by}] if group_by else [],
        "fromTimestamp": frm.isoformat(),
        "toTimestamp": now.isoformat(),
    }
    if time_dimension:
        query["timeDimension"] = {"granularity": time_dimension}
    return query


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

_NUMERIC_RUNTIME_UNITS = {
    "service.chat.duration_ms": "ms",
    "ui.web_vital.ttfb_ms": "ms",
    "ui.web_vital.fcp_ms": "ms",
    "ui.web_vital.lcp_ms": "ms",
    "ui.web_vital.cls": "ratio",
    "ui.interaction.chat_round_trip_ms": "ms",
    "ui.client.error_count": "count",
}
_BOOLEAN_RUNTIME_NAMES = {
    "service.chat.gate.answerable",
    "service.chat.error",
}
_CATEGORICAL_RUNTIME_NAMES = {
    "service.chat.error_category",
    "ui.interaction.chat_outcome",
}


def runtime_metric_queries(
    *,
    since: str,
    now_iso: str,
    time_dimension: str = "day",
) -> dict[str, dict]:
    """Legacy Langfuse v1 queries for typed runtime scores plus native usage."""
    numeric = build_query(
        view="scores-numeric",
        measures=[("value", "avg")],
        group_by="name",
        since=since,
        now_iso=now_iso,
        time_dimension=time_dimension,
    )
    boolean = build_query(
        view="scores-boolean",
        measures=[("value", "avg")],
        group_by="name",
        since=since,
        now_iso=now_iso,
        time_dimension=time_dimension,
    )
    categorical = build_query(
        view="scores-categorical",
        measures=[("count", "count")],
        group_by="name",
        since=since,
        now_iso=now_iso,
        time_dimension=time_dimension,
    )
    categorical["dimensions"].append({"field": "stringValue"})
    observations = build_query(
        view="observations",
        measures=_COST_MEASURES,
        group_by=None,
        since=since,
        now_iso=now_iso,
        time_dimension=time_dimension,
    )
    return {
        "numeric": numeric,
        "boolean": boolean,
        "categorical": categorical,
        "observations": observations,
    }


def summary_from_runtime_metrics(runtime: dict) -> list[dict]:
    """Normalize live legacy-query rows or the committed fixture into series."""
    grouped: dict[tuple[str, str | None], dict] = {}

    def add(
        name: str,
        unit: str,
        row: dict,
        value_key: str,
        *,
        category: str | None = None,
    ) -> None:
        timestamp = row.get("time_dimension")
        value = row.get(value_key)
        if timestamp is None or value is None:
            return
        key = (name, category)
        series = grouped.setdefault(
            key,
            {
                "name": name,
                "unit": unit,
                **({"category": category} if category is not None else {}),
                "points": [],
            },
        )
        series["points"].append(
            {"timestamp": str(timestamp), "value": float(value)}
        )

    for row in runtime.get("numeric", []):
        name = row.get("name")
        if name in _NUMERIC_RUNTIME_UNITS:
            add(name, _NUMERIC_RUNTIME_UNITS[name], row, "avg_value")
    for row in runtime.get("boolean", []):
        name = row.get("name")
        if name in _BOOLEAN_RUNTIME_NAMES:
            add(name, "ratio", row, "avg_value")
    for row in runtime.get("categorical", []):
        name = row.get("name")
        category = row.get("stringValue")
        if name in _CATEGORICAL_RUNTIME_NAMES and isinstance(category, str):
            add(name, "count", row, "count_count", category=category)
    for row in runtime.get("observations", []):
        add(
            "service.generation.latency_ms",
            "ms",
            row,
            "p95_latency",
        )
        add(
            "service.generation.cost_usd",
            "usd",
            row,
            "sum_totalCost",
        )
        add(
            "service.generation.tokens",
            "tokens",
            row,
            "sum_totalTokens",
        )

    series = list(grouped.values())
    for item in series:
        item["points"].sort(key=lambda point: point["timestamp"])
    return sorted(
        series,
        key=lambda item: (item["name"], item.get("category", "")),
    )


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


def runtime_metrics(langfuse, *, since: str, now_iso: str) -> list[dict]:  # pragma: no cover - live
    """Fetch all runtime buckets and normalize them like the offline fixture."""
    queries = runtime_metric_queries(since=since, now_iso=now_iso)
    rows = {name: fetch(langfuse, query) for name, query in queries.items()}
    return summary_from_runtime_metrics(rows)


def main() -> None:  # pragma: no cover - integration entry
    import argparse
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    parser = argparse.ArgumentParser(description="rag-chat quality/cost metrics summary (Langfuse Metrics API)")
    parser.add_argument("--since", default="7d", help="window: 7d / 24h / 30m")
    offline = parser.add_mutually_exclusive_group()
    offline.add_argument(
        "--from-results",
        default=None,
        help="offline: summarize a compare_results.json instead of querying Langfuse",
    )
    offline.add_argument(
        "--from-runtime-metrics",
        default=None,
        help="offline: summarize legacy Metrics-API rows from a JSON fixture",
    )
    args = parser.parse_args()

    if args.from_results:
        results = json.loads(Path(args.from_results).read_text(encoding="utf-8"))
        table = summary_from_results(results)
        source = f"results:{args.from_results}"
        out = {"source": source, "table": table}
    elif args.from_runtime_metrics:
        runtime = json.loads(
            Path(args.from_runtime_metrics).read_text(encoding="utf-8")
        )
        series = summary_from_runtime_metrics(runtime)
        source = f"runtime-metrics:{args.from_runtime_metrics}"
        out = {"source": source, "series": series}
    else:
        import config  # loads .env (LANGFUSE_*)  # noqa: F401
        from langfuse import get_client
        now_iso = datetime.now(timezone.utc).isoformat()
        langfuse = get_client()
        table = cost_latency_by_model(langfuse, since=args.since, now_iso=now_iso)
        series = runtime_metrics(langfuse, since=args.since, now_iso=now_iso)
        source = f"langfuse:observations+scores (since {args.since})"
        out = {"source": source, "table": table, "series": series}

    print("=" * 72)
    print(f"Metrics summary -- {source}")
    for row in out.get("table", []):
        cols = "  ".join(f"{k}={row[k]}" for k in row if k != "group")
        print(f"  {str(row['group']):28s} {cols}")
    for item in out.get("series", []):
        category = (
            f"[{item['category']}]" if item.get("category") is not None else ""
        )
        print(
            f"  {item['name']}{category}: "
            f"{len(item['points'])} {item['unit']} bucket(s)"
        )
    Path(__file__).parent.joinpath("metrics_summary.json").write_text(
        json.dumps(out, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    print("Results -> ingestion/metrics_summary.json")


if __name__ == "__main__":  # pragma: no cover
    main()

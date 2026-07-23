"""
Unit tests for the quality/cost metrics-summary PURE core (ziw.5 / Phase 4, CP1).

Query-builder + result formatter + the results-JSON fallback — no Langfuse, no
network. The live Metrics-API call (CP2) is separate.

Run from repo root:
    uv run --with '.[test]' python -m pytest ingestion/test_metrics_summary.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingestion.metrics_summary import (
    build_query,
    runtime_metric_queries,
    summarize,
    summary_from_results,
    summary_from_runtime_metrics,
)


# --- build_query --------------------------------------------------------------

def test_build_query_shape_and_from_timestamp():
    q = build_query(
        view="traces",
        measures=[("totalCost", "sum"), ("latency", "p95")],
        group_by="model",
        since="7d",
        now_iso="2026-07-08T00:00:00",
    )
    assert q["view"] == "traces"
    assert q["metrics"] == [
        {"measure": "totalCost", "aggregation": "sum"},
        {"measure": "latency", "aggregation": "p95"},
    ]
    assert q["dimensions"] == [{"field": "model"}]
    assert q["fromTimestamp"].startswith("2026-07-01T00:00:00")   # 7 days before
    assert q["toTimestamp"].startswith("2026-07-08T00:00:00")


def test_build_query_no_group_by():
    q = build_query(view="scores", measures=[("value", "avg")], group_by=None,
                    since="24h", now_iso="2026-07-08T12:00:00")
    assert q["dimensions"] == []


def test_build_query_includes_legacy_time_dimension():
    q = build_query(
        view="scores-numeric",
        measures=[("value", "avg")],
        group_by="name",
        since="7d",
        now_iso="2026-07-08T12:00:00",
        time_dimension="day",
    )

    assert q["timeDimension"] == {"granularity": "day"}


def test_build_query_bad_since_raises():
    with pytest.raises(ValueError):
        build_query(view="traces", measures=[("count", "count")], group_by="model",
                    since="7x", now_iso="2026-07-08T00:00:00")


# --- summarize ----------------------------------------------------------------

def test_summarize_reshapes_and_sorts():
    rows = [
        {"model": "gemma4:12b", "totalCost": 0.0, "latency": 1200},
        {"model": "gpt-4o-mini", "totalCost": 0.012, "latency": 800},
    ]
    table = summarize(rows, group_key="model", metric_keys=("totalCost", "latency"))
    assert [r["group"] for r in table] == ["gemma4:12b", "gpt-4o-mini"]  # sorted
    assert table[1]["totalCost"] == 0.012


def test_summarize_missing_metric_is_none():
    rows = [{"model": "gpt-4o-mini", "totalCost": 0.01}]
    table = summarize(rows, group_key="model", metric_keys=("totalCost", "latency"))
    assert table[0]["latency"] is None


# --- summary_from_results (offline fallback) ----------------------------------

def test_summary_from_results_builds_per_model_table():
    results = {
        "baseline": "gpt-4o-mini",
        "models": {
            "gpt-4o-mini": {"faithfulness": {"pass_rate": 0.8}, "answer_correctness": {"pass_rate": 0.5}},
            "gemma4:12b": {"faithfulness": {"pass_rate": 0.7}, "answer_correctness": {"pass_rate": 0.6}},
        },
        "judge_tokens_per_model": {"gpt-4o-mini": {"t": 1}, "gemma4:12b": {"t": 2}},
    }
    table = {r["group"]: r for r in summary_from_results(results)}
    assert table["gpt-4o-mini"]["faithfulness"] == 0.8
    assert table["gemma4:12b"]["answer_correctness"] == 0.6
    assert table["gemma4:12b"]["judge_tokens"] == {"t": 2}


def test_runtime_queries_cover_typed_scores_and_native_observations():
    queries = runtime_metric_queries(
        since="7d",
        now_iso="2026-07-08T12:00:00",
    )

    assert {query["view"] for query in queries.values()} == {
        "scores-numeric",
        "scores-boolean",
        "scores-categorical",
        "observations",
    }
    assert all(
        query["timeDimension"] == {"granularity": "day"}
        for query in queries.values()
    )


def test_runtime_fixture_produces_every_timestamped_catalog_series():
    fixture = json.loads(
        Path("ingestion/runtime_metrics.sample.json").read_text(encoding="utf-8")
    )

    series = summary_from_runtime_metrics(fixture)
    names = {item["name"] for item in series}
    expected = {
        "service.chat.duration_ms",
        "service.chat.gate.answerable",
        "service.chat.error",
        "service.chat.error_category",
        "ui.web_vital.ttfb_ms",
        "ui.web_vital.fcp_ms",
        "ui.web_vital.lcp_ms",
        "ui.web_vital.cls",
        "ui.interaction.chat_round_trip_ms",
        "ui.interaction.chat_outcome",
        "ui.client.error_count",
        "service.generation.latency_ms",
        "service.generation.cost_usd",
        "service.generation.tokens",
    }

    assert expected <= names
    assert all(
        point["timestamp"]
        for item in series
        for point in item["points"]
    )

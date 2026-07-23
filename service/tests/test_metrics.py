"""Behavior tests for the shared service/UI metrics contract (eiio.4)."""

import pytest
from pydantic import ValidationError

from service.metrics import MetricBatch


def test_ui_web_vital_uses_the_canonical_catalog_contract():
    payload = {
        "points": [
            {
                "name": "ui.web_vital.lcp_ms",
                "kind": "numeric",
                "unit": "ms",
                "value": 1840.5,
                "labels": {
                    "environment": "production",
                    "release": "abc123",
                    "route_template": "/",
                    "browser_family": "chromium",
                },
            }
        ]
    }

    batch = MetricBatch.model_validate(payload)
    assert batch.points[0].name == "ui.web_vital.lcp_ms"

    payload["points"][0]["name"] = "ui.web_vital.prompt_text"
    with pytest.raises(ValidationError, match="unknown metric"):
        MetricBatch.model_validate(payload)


def test_service_gate_metric_accepts_a_bounded_boolean_point():
    batch = MetricBatch.model_validate(
        {
            "points": [
                {
                    "name": "service.chat.gate.answerable",
                    "kind": "boolean",
                    "unit": "boolean",
                    "value": True,
                    "labels": {
                        "environment": "production",
                        "release": "abc123",
                        "mode": "sage",
                        "route_template": "/chat",
                    },
                }
            ]
        }
    )

    assert batch.points[0].value is True


def test_error_category_rejects_unbounded_values():
    payload = {
        "points": [
            {
                "name": "service.chat.error_category",
                "kind": "categorical",
                "unit": "category",
                "value": "dependency",
                "labels": {"route_template": "/chat"},
            }
        ]
    }

    batch = MetricBatch.model_validate(payload)
    assert batch.points[0].value == "dependency"

    payload["points"][0]["value"] = "database"
    with pytest.raises(ValidationError, match="unsupported category"):
        MetricBatch.model_validate(payload)


def test_catalog_covers_the_standard_service_and_ui_metrics():
    points = [
        {"name": "service.chat.duration_ms", "kind": "numeric", "unit": "ms", "value": 12.0},
        {"name": "service.chat.gate.answerable", "kind": "boolean", "unit": "boolean", "value": True},
        {"name": "service.chat.error", "kind": "boolean", "unit": "boolean", "value": False},
        {
            "name": "service.chat.error_category",
            "kind": "categorical",
            "unit": "category",
            "value": "handler",
        },
        {"name": "ui.web_vital.ttfb_ms", "kind": "numeric", "unit": "ms", "value": 1.0},
        {"name": "ui.web_vital.fcp_ms", "kind": "numeric", "unit": "ms", "value": 2.0},
        {"name": "ui.web_vital.lcp_ms", "kind": "numeric", "unit": "ms", "value": 3.0},
        {"name": "ui.web_vital.cls", "kind": "numeric", "unit": "ratio", "value": 0.01},
        {
            "name": "ui.interaction.chat_round_trip_ms",
            "kind": "numeric",
            "unit": "ms",
            "value": 4.0,
        },
        {
            "name": "ui.interaction.chat_outcome",
            "kind": "categorical",
            "unit": "category",
            "value": "success",
        },
        {"name": "ui.client.error_count", "kind": "numeric", "unit": "count", "value": 1},
    ]

    assert len(MetricBatch.model_validate({"points": points}).points) == len(points)


def test_payload_rejects_extra_private_non_finite_and_oversized_input():
    point = {
        "name": "ui.web_vital.cls",
        "kind": "numeric",
        "unit": "ratio",
        "value": 0.01,
        "labels": {"environment": "production"},
    }

    with pytest.raises(ValidationError):
        MetricBatch.model_validate({"points": [point], "prompt": "private"})

    private_label = {**point, "labels": {"conversation_id": "private"}}
    with pytest.raises(ValidationError):
        MetricBatch.model_validate({"points": [private_label]})

    non_finite = {**point, "value": float("inf")}
    with pytest.raises(ValidationError):
        MetricBatch.model_validate({"points": [non_finite]})

    with pytest.raises(ValidationError):
        MetricBatch.model_validate({"points": [point] * 51})

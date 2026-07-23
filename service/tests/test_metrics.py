"""Behavior tests for the shared service/UI metrics contract (eiio.4)."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ingestion.retrieval import EmbeddingUnavailableError
from service.app import app, get_service
from service.metrics import (
    LangfuseMetricsSink,
    MetricBatch,
    NoopMetricsSink,
    build_metrics_sink,
)
from service.models import ChatResponse


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


class _RecordingSink:
    def __init__(self) -> None:
        self.points = []

    def record(self, point) -> None:
        self.points.append(point)


def test_ui_metrics_endpoint_records_a_valid_batch():
    sink = _RecordingSink()
    app.state.metrics_sink = sink
    try:
        response = TestClient(app).post(
            "/metrics/ui",
            json={
                "points": [
                    {
                        "name": "ui.web_vital.lcp_ms",
                        "kind": "numeric",
                        "unit": "ms",
                        "value": 1840.5,
                        "labels": {"browser_family": "chromium"},
                    }
                ]
            },
        )
    finally:
        del app.state.metrics_sink

    assert response.status_code == 202
    assert response.json() == {"accepted": 1}
    assert sink.points[0].name == "ui.web_vital.lcp_ms"


def test_ui_metrics_endpoint_rejects_service_metric_names():
    response = TestClient(app).post(
        "/metrics/ui",
        json={
            "points": [
                {
                    "name": "service.chat.error",
                    "kind": "boolean",
                    "unit": "boolean",
                    "value": True,
                }
            ]
        },
    )

    assert response.status_code == 422


class _RaisingSink:
    def record(self, point) -> None:
        raise RuntimeError("telemetry unavailable")


def test_ui_metrics_endpoint_fails_open_when_storage_is_unavailable(caplog):
    app.state.metrics_sink = _RaisingSink()
    try:
        response = TestClient(app).post(
            "/metrics/ui",
            json={
                "points": [
                    {
                        "name": "ui.client.error_count",
                        "kind": "numeric",
                        "unit": "count",
                        "value": 1.0,
                    }
                ]
            },
        )
    finally:
        del app.state.metrics_sink

    assert response.status_code == 202
    assert response.json() == {"accepted": 1}
    assert "metric recording failed" in caplog.text


class _FakeObservation:
    id = "observation-1"
    trace_id = "trace-1"

    def end(self) -> None:
        return None


class _FakeLangfuse:
    def __init__(self) -> None:
        self.scores = []

    def start_observation(self, **kwargs):
        return _FakeObservation()

    def create_score(self, **kwargs) -> None:
        self.scores.append(kwargs)


def test_langfuse_sink_preserves_numeric_boolean_and_categorical_types():
    client = _FakeLangfuse()
    sink = LangfuseMetricsSink(client)
    batch = MetricBatch.model_validate(
        {
            "points": [
                {
                    "name": "ui.web_vital.lcp_ms",
                    "kind": "numeric",
                    "unit": "ms",
                    "value": 1200.0,
                },
                {
                    "name": "service.chat.error",
                    "kind": "boolean",
                    "unit": "boolean",
                    "value": False,
                },
                {
                    "name": "service.chat.error_category",
                    "kind": "categorical",
                    "unit": "category",
                    "value": "handler",
                },
            ]
        }
    )

    for point in batch.points:
        sink.record(point)

    assert [(score["name"], score["data_type"]) for score in client.scores] == [
        ("ui.web_vital.lcp_ms", "NUMERIC"),
        ("service.chat.error", "BOOLEAN"),
        ("service.chat.error_category", "CATEGORICAL"),
    ]
    assert client.scores[1]["value"] is False


def test_metrics_sink_is_offline_by_default():
    def unexpected_client():
        raise AssertionError("disabled telemetry must not construct a client")

    assert isinstance(
        build_metrics_sink(enabled=False, client_factory=unexpected_client),
        NoopMetricsSink,
    )


def test_metrics_sink_degrades_when_enabled_storage_cannot_start(caplog):
    def unavailable_client():
        raise RuntimeError("bad credentials")

    sink = build_metrics_sink(enabled=True, client_factory=unavailable_client)

    assert isinstance(sink, NoopMetricsSink)
    assert "serving without metrics" in caplog.text


class _AnsweringService:
    def answer(self, prompt, **kwargs):
        return ChatResponse(
            answer="A grounded answer.",
            sources=[],
            answerable=True,
            mode=kwargs["mode"],
            conversation_id=kwargs["conversation_id"],
        )


class _RaisingService:
    def __init__(self, error):
        self._error = error

    def answer(self, prompt, **kwargs):
        raise self._error


def test_chat_success_records_duration_error_and_gate_metrics():
    sink = _RecordingSink()
    app.state.metrics_sink = sink
    app.dependency_overrides[get_service] = _AnsweringService
    try:
        response = TestClient(app).post(
            "/chat",
            json={"prompt": "What is a basilisk?", "mode": "rules"},
        )
    finally:
        app.dependency_overrides.clear()
        del app.state.metrics_sink

    assert response.status_code == 200
    points = {point.name: point for point in sink.points}
    assert points["service.chat.duration_ms"].value >= 0
    assert points["service.chat.error"].value is False
    assert points["service.chat.gate.answerable"].value is True
    assert points["service.chat.gate.answerable"].labels.mode == "rules"


def test_chat_succeeds_when_metrics_storage_fails():
    app.state.metrics_sink = _RaisingSink()
    app.dependency_overrides[get_service] = _AnsweringService
    try:
        response = TestClient(app).post(
            "/chat",
            json={"prompt": "What is a basilisk?"},
        )
    finally:
        app.dependency_overrides.clear()
        del app.state.metrics_sink

    assert response.status_code == 200
    assert response.json()["answerable"] is True


@pytest.mark.parametrize(
    ("payload", "service", "status_code", "category"),
    [
        ({}, _AnsweringService(), 422, "validation"),
        (
            {"prompt": "question"},
            _RaisingService(EmbeddingUnavailableError("not configured")),
            503,
            "dependency",
        ),
        (
            {"prompt": "question"},
            _RaisingService(RuntimeError("retrieval unavailable")),
            500,
            "handler",
        ),
    ],
)
def test_chat_failures_record_bounded_error_categories(
    payload, service, status_code, category
):
    sink = _RecordingSink()
    app.state.metrics_sink = sink
    app.dependency_overrides[get_service] = lambda: service
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/chat",
            json=payload,
        )
    finally:
        app.dependency_overrides.clear()
        del app.state.metrics_sink

    assert response.status_code == status_code
    points = {point.name: point for point in sink.points}
    assert points["service.chat.duration_ms"].value >= 0
    assert points["service.chat.error"].value is True
    assert points["service.chat.error_category"].value == category

"""Canonical, privacy-bounded metric payloads shared by service and UI."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    FiniteFloat,
    Field,
    StrictBool,
    model_validator,
)

log = logging.getLogger(__name__)


class MetricLabels(BaseModel):
    """Low-cardinality labels only; unknown fields are rejected as potential PII."""

    model_config = ConfigDict(extra="forbid", strict=True)

    environment: Literal["local", "test", "ci", "staging", "production"] | None = None
    release: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    mode: Literal["sage", "spell", "rules", "gm"] | None = None
    route_template: Literal["/", "/chat", "/metrics/ui"] | None = None
    browser_family: Literal["chromium", "firefox", "webkit", "other"] | None = None


_SERVICE_LABELS = frozenset({"environment", "release", "mode", "route_template"})
_UI_LABELS = frozenset(
    {"environment", "release", "route_template", "browser_family"}
)
_UI_INTERACTION_LABELS = _UI_LABELS | {"mode"}

_CATALOG = {
    "service.chat.duration_ms": {
        "kind": "numeric",
        "unit": "ms",
        "labels": _SERVICE_LABELS,
    },
    "ui.web_vital.lcp_ms": {
        "kind": "numeric",
        "unit": "ms",
        "labels": _UI_LABELS,
    },
    "service.chat.gate.answerable": {
        "kind": "boolean",
        "unit": "boolean",
        "labels": _SERVICE_LABELS,
    },
    "service.chat.error": {
        "kind": "boolean",
        "unit": "boolean",
        "labels": _SERVICE_LABELS,
    },
    "service.chat.error_category": {
        "kind": "categorical",
        "unit": "category",
        "labels": _SERVICE_LABELS,
        "categories": frozenset({"validation", "dependency", "handler", "unknown"}),
    },
    "ui.web_vital.ttfb_ms": {"kind": "numeric", "unit": "ms", "labels": _UI_LABELS},
    "ui.web_vital.fcp_ms": {"kind": "numeric", "unit": "ms", "labels": _UI_LABELS},
    "ui.web_vital.cls": {"kind": "numeric", "unit": "ratio", "labels": _UI_LABELS},
    "ui.interaction.chat_round_trip_ms": {
        "kind": "numeric",
        "unit": "ms",
        "labels": _UI_INTERACTION_LABELS,
    },
    "ui.interaction.chat_outcome": {
        "kind": "categorical",
        "unit": "category",
        "labels": _UI_INTERACTION_LABELS,
        "categories": frozenset(
            {"success", "http_error", "network_error", "aborted"}
        ),
    },
    "ui.client.error_count": {
        "kind": "numeric",
        "unit": "count",
        "labels": _UI_LABELS,
    },
}


def _validate_catalog_contract(
    *,
    name: str,
    kind: str,
    unit: str,
    labels: MetricLabels,
    category: str | None = None,
) -> None:
    definition = _CATALOG.get(name)
    if definition is None:
        raise ValueError(f"unknown metric: {name}")
    if kind != definition["kind"] or unit != definition["unit"]:
        raise ValueError(f"metric kind/unit mismatch: {name}")
    supplied_labels = set(labels.model_dump(exclude_none=True))
    if not supplied_labels <= definition["labels"]:
        raise ValueError(f"unsupported labels for metric: {name}")
    if category is not None and category not in definition["categories"]:
        raise ValueError(f"unsupported category for metric: {name}")


class NumericMetricPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    name: str
    kind: Literal["numeric"]
    unit: Literal["ms", "ratio", "count"]
    value: FiniteFloat = Field(ge=0)
    labels: MetricLabels = Field(default_factory=MetricLabels)

    @model_validator(mode="after")
    def validate_catalog_contract(self) -> NumericMetricPoint:
        _validate_catalog_contract(
            name=self.name, kind=self.kind, unit=self.unit, labels=self.labels
        )
        return self


class BooleanMetricPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    kind: Literal["boolean"]
    unit: Literal["boolean"]
    value: StrictBool
    labels: MetricLabels = Field(default_factory=MetricLabels)

    @model_validator(mode="after")
    def validate_catalog_contract(self) -> BooleanMetricPoint:
        _validate_catalog_contract(
            name=self.name, kind=self.kind, unit=self.unit, labels=self.labels
        )
        return self


class CategoricalMetricPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    kind: Literal["categorical"]
    unit: Literal["category"]
    value: str = Field(min_length=1, max_length=32)
    labels: MetricLabels = Field(default_factory=MetricLabels)

    @model_validator(mode="after")
    def validate_catalog_contract(self) -> CategoricalMetricPoint:
        _validate_catalog_contract(
            name=self.name,
            kind=self.kind,
            unit=self.unit,
            labels=self.labels,
            category=self.value,
        )
        return self


MetricPoint = Annotated[
    NumericMetricPoint | BooleanMetricPoint | CategoricalMetricPoint,
    Field(discriminator="kind"),
]


class MetricBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    points: list[MetricPoint] = Field(min_length=1, max_length=50)


class MetricsSink(Protocol):
    def record(self, point: MetricPoint) -> None: ...


class NoopMetricsSink:
    def record(self, point: MetricPoint) -> None:
        return None


class LangfuseMetricsSink:
    """Persist validated points as typed Langfuse v3 scores."""

    _DATA_TYPES = {
        "numeric": "NUMERIC",
        "boolean": "BOOLEAN",
        "categorical": "CATEGORICAL",
    }

    def __init__(self, client: Any) -> None:
        self._client = client

    def record(self, point: MetricPoint) -> None:
        labels = point.labels.model_dump(exclude_none=True)
        observation = self._client.start_observation(
            name=f"metric:{point.name}",
            as_type="span",
            metadata={"metric_name": point.name, "unit": point.unit, **labels},
        )
        value: float | str | bool
        if point.kind == "numeric":
            value = float(point.value)
        elif point.kind == "boolean":
            value = point.value
        else:
            value = point.value
        try:
            self._client.create_score(
                name=point.name,
                value=value,
                data_type=self._DATA_TYPES[point.kind],
                trace_id=observation.trace_id,
                observation_id=observation.id,
                metadata={"unit": point.unit, **labels},
            )
        finally:
            observation.end()


def build_metrics_sink(
    *,
    enabled: bool | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> MetricsSink:
    """Build the runtime sink, reusing the service's opt-in tracing switch."""
    if enabled is None:
        from .tracing import tracing_enabled

        enabled = tracing_enabled()
    if not enabled:
        return NoopMetricsSink()

    try:
        if client_factory is None:
            from langfuse import get_client

            client_factory = get_client
        return LangfuseMetricsSink(client_factory())
    except Exception:
        log.warning(
            "metrics enabled but Langfuse is unavailable; serving without metrics",
            exc_info=True,
        )
        return NoopMetricsSink()


def record_safely(sink: MetricsSink, point: MetricPoint) -> None:
    """Telemetry is best-effort and must never escape into a product request."""
    try:
        sink.record(point)
    except Exception:
        log.warning("metric recording failed (name=%s)", point.name, exc_info=True)

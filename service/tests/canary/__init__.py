"""The canary leak-test harness (agent-forge-harness-1ir.1.10): test infrastructure, never product code.

A test mints synthetic secrets ("canaries") into every private place, routes the code under test
through recording fakes and captures (LLM, embeddings, STT, cache, channel, HTTP, rows, logs, stdout,
warnings, metrics and traces), and asks ``assert_clean`` whether any canary reached a place it must
never be. The fixtures ``canary_world`` and ``leak_capture`` come from ``service.tests.canary.plugin``,
registered once in the repository's root ``conftest.py``. Usage, rules and blind spots are in
``docs/canary-leak-harness.md``. Import this package only as ``service.tests.canary``.

The names resolve lazily (PEP 562): the root ``conftest.py`` loads the plugin in every pytest run,
``ingestion/tests`` included, and that must not import LangChain or the Workbench schema (P-6).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from service.tests.canary.core import (
        TOKEN_PATTERN,
        Audience,
        Canary,
        CanaryDocument,
        CanaryLeak,
        CanarySet,
        CanaryWorld,
        Capture,
        CaptureNotAsserted,
        Finding,
        FindingCategory,
        HarnessMisuse,
        Hit,
        NeedleIndex,
        NeedleKind,
        SinkKind,
        Surface,
        scan,
        sweep_for_canaries,
    )
    from service.tests.canary.sinks import (
        LeakCapture,
        LogCapture,
        RecordingCache,
        RecordingChannel,
        RecordingEmbeddings,
        RecordingLangfuseClient,
        RecordingLLM,
        RecordingMetricsSink,
        RecordingSTT,
        RecordingTraceHandler,
    )

_CORE = (
    "TOKEN_PATTERN", "Audience", "Canary", "CanaryDocument", "CanaryLeak", "CanarySet", "CanaryWorld", "Capture",
    "CaptureNotAsserted", "Finding", "FindingCategory", "HarnessMisuse", "Hit", "NeedleIndex", "NeedleKind",
    "SinkKind", "Surface", "scan", "sweep_for_canaries",
)
_SINKS = (
    "LeakCapture", "LogCapture", "RecordingCache", "RecordingChannel", "RecordingEmbeddings",
    "RecordingLangfuseClient", "RecordingLLM", "RecordingMetricsSink", "RecordingSTT", "RecordingTraceHandler",
)
_HOME: dict[str, str] = {**dict.fromkeys(_CORE, "core"), **dict.fromkeys(_SINKS, "sinks")}

__all__ = [
    "TOKEN_PATTERN",
    "Audience",
    "Canary",
    "CanaryDocument",
    "CanaryLeak",
    "CanarySet",
    "CanaryWorld",
    "Capture",
    "CaptureNotAsserted",
    "Finding",
    "FindingCategory",
    "HarnessMisuse",
    "Hit",
    "LeakCapture",
    "LogCapture",
    "NeedleIndex",
    "NeedleKind",
    "RecordingCache",
    "RecordingChannel",
    "RecordingEmbeddings",
    "RecordingLLM",
    "RecordingLangfuseClient",
    "RecordingMetricsSink",
    "RecordingSTT",
    "RecordingTraceHandler",
    "SinkKind",
    "Surface",
    "scan",
    "sweep_for_canaries",
]


def __getattr__(name: str) -> object:
    home = _HOME.get(name)
    if home is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{home}"), name)
    globals()[name] = value
    return value

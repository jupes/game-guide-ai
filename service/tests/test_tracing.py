"""
Tests for env-gated Langfuse tracing config (ziw.2 / CP3).

Tracing is OFF by default so the test suite / CI never emit traces or require live
Langfuse keys. When enabled (RAG_TRACING truthy) a callback + metadata are attached.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/test_tracing.py -q
"""

from __future__ import annotations

from service.tracing import (
    build_trace_config,
    service_version,
    trace_metadata,
    tracing_enabled,
)


def test_tracing_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RAG_TRACING", raising=False)
    assert tracing_enabled() is False
    # Disabled -> empty config -> graph.invoke runs with no callbacks.
    assert build_trace_config(model="gpt-4o-mini", mode="sage") == {}


def test_tracing_enabled_flag(monkeypatch):
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv("RAG_TRACING", val)
        assert tracing_enabled() is True
    monkeypatch.setenv("RAG_TRACING", "0")
    assert tracing_enabled() is False


def test_trace_metadata_tags():
    md = trace_metadata(model="gpt-4o-mini", mode="sage", version="abc123")
    assert md["model"] == "gpt-4o-mini"
    assert md["mode"] == "sage"
    assert md["service_version"] == "abc123"
    assert "mode:sage" in md["langfuse_tags"]


def test_service_version_prefers_env(monkeypatch):
    monkeypatch.setenv("SERVICE_VERSION", "deadbeef")
    assert service_version() == "deadbeef"


def test_build_trace_config_enabled_attaches_callback_and_metadata(monkeypatch):
    monkeypatch.setenv("RAG_TRACING", "1")
    # Dummy keys so the Langfuse handler constructs without warnings; no network
    # happens at construction (flush is lazy).
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    cfg = build_trace_config(model="gpt-4o-mini", mode="gm", version="v1")
    assert cfg["metadata"]["model"] == "gpt-4o-mini"
    assert cfg["metadata"]["mode"] == "gm"
    assert cfg["metadata"]["service_version"] == "v1"
    assert isinstance(cfg.get("callbacks"), list) and len(cfg["callbacks"]) == 1

"""
Config guard (02t.3).

`config.py` is the single documented home for every RAG tuning knob. These tests
pin the documented defaults and prove each knob is overridable from the
environment (the whole point of externalizing them — tune without a code change
or redeploy).

Run from repo root:
    uv run --with '.[test]' python -m pytest test_config.py -q
"""

from __future__ import annotations

import importlib

import pytest

import config

# Every env var config reads, with its (var, attr, default) contract.
KNOBS = (
    ("RAG_TOP_K", "TOP_K", 10),
    ("RAG_CONTEXT_TOP_N", "CONTEXT_TOP_N", 5),
    ("RAG_SNIPPET_MAX", "SNIPPET_MAX", 240),
    ("RAG_FALLBACK_DISTANCE", "IPL_FALLBACK_DISTANCE", 0.42),
    ("RAG_ANSWERABLE_DISTANCE", "KOZ_ANSWERABLE_DISTANCE", 0.50),
    ("RAG_DEFAULT_MODEL", "DEFAULT_MODEL", "gpt-4o-mini"),
    ("RAG_TEMPERATURE", "TEMPERATURE", 0.2),
    ("RAG_RERANK", "RAG_RERANK", False),
    ("RAG_HISTORY_LIMIT", "HISTORY_LIMIT", 50),
    ("RAG_ATTACHMENT_MAX_BYTES", "ATTACHMENT_MAX_BYTES", 2_000_000),
    ("RAG_ATTACHMENT_MAX_CHARS", "ATTACHMENT_MAX_CHARS", 6000),
    ("RAG_LLM_REQUEST_TIMEOUT_S", "LLM_REQUEST_TIMEOUT_S", 60.0),
    ("RAG_LLM_CONNECT_TIMEOUT_S", "LLM_CONNECT_TIMEOUT_S", 5.0),
)

# The two knobs above are validated (config._seconds): reload must refuse an
# env value that would unbound or break generation, for EACH name -- pins the
# env var name each constant reads (agent-forge-harness-52o M8) and that
# LLM_CONNECT_TIMEOUT_S is validated as strictly as LLM_REQUEST_TIMEOUT_S,
# never with the bare `_float` that accepts "inf" (M9).
VALIDATED_TIMEOUTS = ("RAG_LLM_REQUEST_TIMEOUT_S", "RAG_LLM_CONNECT_TIMEOUT_S")

OVERRIDES = {
    "RAG_TOP_K": ("25", "TOP_K", 25),
    "RAG_CONTEXT_TOP_N": ("3", "CONTEXT_TOP_N", 3),
    "RAG_SNIPPET_MAX": ("120", "SNIPPET_MAX", 120),
    "RAG_FALLBACK_DISTANCE": ("0.30", "IPL_FALLBACK_DISTANCE", 0.30),
    "RAG_ANSWERABLE_DISTANCE": ("0.66", "KOZ_ANSWERABLE_DISTANCE", 0.66),
    "RAG_DEFAULT_MODEL": ("gpt-4o", "DEFAULT_MODEL", "gpt-4o"),
    "RAG_TEMPERATURE": ("0.9", "TEMPERATURE", 0.9),
    "RAG_RERANK": ("1", "RAG_RERANK", True),
    "RAG_HISTORY_LIMIT": ("200", "HISTORY_LIMIT", 200),
    "RAG_ATTACHMENT_MAX_BYTES": ("500000", "ATTACHMENT_MAX_BYTES", 500_000),
    "RAG_ATTACHMENT_MAX_CHARS": ("2500", "ATTACHMENT_MAX_CHARS", 2500),
    "RAG_LLM_REQUEST_TIMEOUT_S": ("12.5", "LLM_REQUEST_TIMEOUT_S", 12.5),
    "RAG_LLM_CONNECT_TIMEOUT_S": ("2.5", "LLM_CONNECT_TIMEOUT_S", 2.5),
}


def test_documented_defaults(monkeypatch):
    """With no env set, every knob falls back to its documented default."""
    for var, _attr, _default in KNOBS:
        monkeypatch.delenv(var, raising=False)
    cfg = importlib.reload(config)
    for _var, attr, default in KNOBS:
        assert getattr(cfg, attr) == default, attr


def test_env_overrides_every_knob(monkeypatch):
    """Setting each RAG_* var overrides the corresponding constant."""
    for var, (raw, _attr, _value) in OVERRIDES.items():
        monkeypatch.setenv(var, raw)
    cfg = importlib.reload(config)
    for _var, (_raw, attr, value) in OVERRIDES.items():
        assert getattr(cfg, attr) == value, attr


def test_bool_knob_truthy_set(monkeypatch):
    """RAG_RERANK parses the same truthy set as RAG_TRACING (tracing.py):
    {"1", "true", "yes", "on"} case-insensitively; everything else is False."""
    for raw, expected in (
        ("1", True), ("true", True), ("YES", True), ("on", True),
        ("0", False), ("false", False), ("off", False), ("", False), ("bogus", False),
    ):
        monkeypatch.setenv("RAG_RERANK", raw)
        cfg = importlib.reload(config)
        assert cfg.RAG_RERANK is expected, f"RAG_RERANK={raw!r}"


@pytest.mark.parametrize("var", VALIDATED_TIMEOUTS)
@pytest.mark.parametrize("raw", ["inf", "nan", "0"])
def test_a_generation_timeout_that_would_unbound_or_break_it_is_refused_on_reload(monkeypatch, var, raw):
    """Reload-based, unlike test_providers.py's test_a_timeout_setting_that_...
    (which calls config._seconds(name, ...) with the name it passes in, so it
    cannot catch that name being wired wrong in config.py itself). Reloading
    the module re-runs config.py's own top-level `_seconds("RAG_LLM_..._S", …)`
    calls, so a wrong env-var name (M8) or a wrong reader -- e.g.
    LLM_CONNECT_TIMEOUT_S read with `_float`, which accepts "inf" (M9) --
    shows up here as either no ValueError, or one that names the wrong var."""
    monkeypatch.setenv(var, raw)
    with pytest.raises(ValueError, match=var):
        importlib.reload(config)


def teardown_module(_module):
    # Restore env-free defaults so later tests importing config see real values.
    importlib.reload(config)

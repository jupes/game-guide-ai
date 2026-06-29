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
)

OVERRIDES = {
    "RAG_TOP_K": ("25", "TOP_K", 25),
    "RAG_CONTEXT_TOP_N": ("3", "CONTEXT_TOP_N", 3),
    "RAG_SNIPPET_MAX": ("120", "SNIPPET_MAX", 120),
    "RAG_FALLBACK_DISTANCE": ("0.30", "IPL_FALLBACK_DISTANCE", 0.30),
    "RAG_ANSWERABLE_DISTANCE": ("0.66", "KOZ_ANSWERABLE_DISTANCE", 0.66),
    "RAG_DEFAULT_MODEL": ("gpt-4o", "DEFAULT_MODEL", "gpt-4o"),
    "RAG_TEMPERATURE": ("0.9", "TEMPERATURE", 0.9),
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


def teardown_module(_module):
    # Restore env-free defaults so later tests importing config see real values.
    importlib.reload(config)

"""
Unit tests for scripts/provider_health.py (agent-forge-harness-b8o.1,
Checkpoint 1 step 9).

Run from repo root:
    uv run --with '.[test]' python -m pytest tests/test_provider_health.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.provider_health import catalog_status, credential_present, main  # noqa: E402
from service.model_catalog import CATALOG, ModelProfile  # noqa: E402


def test_credential_present_true_when_env_var_set(monkeypatch):
    profile = CATALOG["gpt-4o-mini"]
    monkeypatch.setenv(profile.secret_env, "sk-anything")
    assert credential_present(profile) is True


def test_credential_present_false_when_env_var_unset(monkeypatch):
    disabled = ModelProfile(
        alias="test-health-check", display_name="x", provider="deepseek",
        api_model="x", base_url="https://example.invalid", secret_env="TEST_HEALTH_CHECK_KEY",
        tier="economy", supports_attachments=True, enabled=False,
    )
    monkeypatch.delenv("TEST_HEALTH_CHECK_KEY", raising=False)
    assert credential_present(disabled) is False


def test_catalog_status_includes_every_entry_enabled_and_disabled():
    # Unlike GET /models, this operator tool DOES include disabled aliases —
    # that's the point (confirm a secret is set before enabling one).
    rows = catalog_status()
    aliases = {r["alias"] for r in rows}
    assert aliases == set(CATALOG)
    assert "deepseek-v4-flash" in aliases  # disabled, still reported


def test_catalog_status_never_includes_a_credential_value(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-super-secret-value-must-not-leak")
    rows = catalog_status()
    serialized = json.dumps(rows)
    assert "sk-super-secret-value-must-not-leak" not in serialized


def test_catalog_status_reports_presence_as_a_bool_only():
    rows = catalog_status()
    for row in rows:
        assert isinstance(row["credential_present"], bool)


def test_main_json_mode_prints_valid_json(capsys):
    exit_code = main(["--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    rows = json.loads(captured.out)
    assert isinstance(rows, list)
    assert any(r["alias"] == "gpt-4o-mini" for r in rows)


def test_main_table_mode_prints_a_header_and_never_a_credential_value(monkeypatch, capsys):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-another-secret-value")
    exit_code = main([])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "alias" in out and "provider" in out
    assert "sk-another-secret-value" not in out

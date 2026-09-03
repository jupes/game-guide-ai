"""
Unit tests for the server-owned model catalog (agent-forge-harness-b8o.1,
Checkpoint 1). See docs/forge/plans/game-guide-ai-model-routing.md, "Server-owned
model catalog" + "Public API contracts".

TDD row 1: the catalog exposes only enabled/approved aliases and never leaks
keys, secret names, or base URLs.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_model_catalog.py -q
"""

from __future__ import annotations

from service.model_catalog import (
    CATALOG,
    DEFAULT_ALIAS,
    ModelProfile,
    enabled_profiles,
    get_profile,
    public_model_entry,
)


def test_default_alias_is_an_enabled_profile():
    assert get_profile(DEFAULT_ALIAS) is not None


def test_enabled_profiles_excludes_disabled_entries():
    disabled = ModelProfile(
        alias="test-disabled", display_name="Test Disabled", provider="openai",
        api_model="gpt-9000", base_url=None, secret_env="OPENAI_API_KEY",
        tier="economy", supports_attachments=True, enabled=False,
    )
    CATALOG["test-disabled"] = disabled
    try:
        assert disabled not in enabled_profiles()
        assert all(p.enabled for p in enabled_profiles())
    finally:
        del CATALOG["test-disabled"]


def test_get_profile_returns_none_for_a_disabled_alias():
    # Prove get_profile cannot be used to distinguish "unknown" from "disabled" —
    # both must look identical to a caller (never confirm an alias exists but is off).
    disabled = ModelProfile(
        alias="test-disabled-2", display_name="x", provider="openai",
        api_model="gpt-9000", base_url=None, secret_env="OPENAI_API_KEY",
        tier="economy", supports_attachments=True, enabled=False,
    )
    CATALOG["test-disabled-2"] = disabled
    try:
        assert get_profile("test-disabled-2") is None
        assert get_profile("totally-unknown-alias") is None
    finally:
        del CATALOG["test-disabled-2"]


def test_public_model_entry_never_leaks_secret_fields():
    profile = get_profile(DEFAULT_ALIAS)
    assert profile is not None
    entry = public_model_entry(profile)
    assert set(entry) == {"id", "display_name", "tier", "supports_attachments"}
    # Defensive: even if a future field is added to ModelProfile, these exact
    # secret-bearing values must never appear anywhere in the public shape.
    serialized = str(entry)
    assert profile.api_model not in serialized or profile.api_model == entry["id"]
    assert (profile.secret_env or "") not in serialized
    assert (profile.base_url or "") not in serialized or profile.base_url is None


def test_public_model_entry_shape_matches_the_contract():
    profile = get_profile(DEFAULT_ALIAS)
    assert profile is not None
    entry = public_model_entry(profile)
    assert entry["id"] == profile.alias
    assert entry["display_name"] == profile.display_name
    assert entry["tier"] == profile.tier
    assert entry["supports_attachments"] == profile.supports_attachments


# ---------------------------------------------------------------------------
# Disabled-by-default DeepSeek/Qwen/Kimi profiles (Checkpoint 1 slice 6).
# Frozen v1 candidates + base_url/secret_env per the plan's "Provider
# onboarding and API keys" section. Disabled: none are eligible for traffic
# until Checkpoint 3's evaluation matrix and D7's Qwen residency confirmation.
# ---------------------------------------------------------------------------

def test_deepseek_profile_exists_disabled_with_correct_endpoint():
    profile = CATALOG["deepseek-v4-flash"]
    assert profile.provider == "deepseek"
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.secret_env == "DEEPSEEK_API_KEY"
    assert profile.enabled is False


def test_qwen_profile_exists_disabled_with_correct_endpoint():
    profile = CATALOG["qwen-flash-us"]
    assert profile.provider == "alibaba"
    assert profile.base_url == "https://dashscope-us.aliyuncs.com/compatible-mode/v1"
    assert profile.secret_env == "DASHSCOPE_API_KEY"
    assert profile.enabled is False


def test_kimi_profile_exists_disabled_with_correct_endpoint():
    profile = CATALOG["kimi-k3"]
    assert profile.provider == "moonshot"
    assert profile.base_url == "https://api.moonshot.ai/v1"
    assert profile.secret_env == "MOONSHOT_API_KEY"
    assert profile.enabled is False


def test_disabled_provider_profiles_are_invisible_to_the_public_surface():
    # TDD row 1: a disabled alias must look exactly like an unknown one to
    # any caller that isn't reading CATALOG directly.
    for alias in ("deepseek-v4-flash", "qwen-flash-us", "kimi-k3"):
        assert get_profile(alias) is None
        assert all(p.alias != alias for p in enabled_profiles())

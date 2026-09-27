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

import pytest

from service.model_catalog import (
    AUTO_PUBLIC_ENTRY,
    CATALOG,
    DEFAULT_ALIAS,
    PUBLIC_MODELS,
    ModelProfile,
    enabled_profiles,
    get_profile,
    get_profile_by_public_id,
    public_model_entry,
    public_model_id,
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
    # D-9 (au3): id, display_name and tier come from PUBLIC_MODELS, never from
    # the catalog's own alias, display name or cost tier.
    profile = get_profile(DEFAULT_ALIAS)
    assert profile is not None
    entry = public_model_entry(profile)
    public = PUBLIC_MODELS[profile.alias]
    assert entry["id"] == public.id != profile.alias
    assert entry["display_name"] == public.label != profile.display_name
    assert entry["tier"] == public.tier != profile.tier
    assert entry["supports_attachments"] == profile.supports_attachments


# ---------------------------------------------------------------------------
# D-9 (au3): users never learn which model or provider answers. PUBLIC_MODELS
# is the one mapping from a catalog alias to what the client may see.
# ---------------------------------------------------------------------------

# D-8 names the free tier's model "mini" and the paid tiers' "Luna"; Luna is
# not in CATALOG yet, and no label may hint at either.
_MODEL_WORDS_NOT_IN_CATALOG = ("mini", "luna")


def _names_of_models_and_providers() -> set[str]:
    names = set(_MODEL_WORDS_NOT_IN_CATALOG)
    for p in CATALOG.values():
        names |= {p.alias, p.display_name, p.api_model, p.provider}
    return {n.lower() for n in names}


def test_every_catalog_alias_has_exactly_one_distinct_public_id():
    assert set(PUBLIC_MODELS) == set(CATALOG)
    ids = [public.id for public in PUBLIC_MODELS.values()]
    assert len(ids) == len(set(ids)), "one public id names one catalog entry"
    assert "auto" not in ids, "auto is the strategy's id, not a model's"


def test_no_public_id_or_label_names_a_model_or_provider():
    shown = [text.lower() for public in PUBLIC_MODELS.values() for text in (public.id, public.label)]
    shown += [str(value).lower() for value in AUTO_PUBLIC_ENTRY.values()]
    for name in _names_of_models_and_providers():
        assert not any(name in text for text in shown), name


def test_every_enabled_entry_is_shown_with_a_tier():
    for profile in enabled_profiles():
        assert PUBLIC_MODELS[profile.alias].tier in ("traveller", "adventurer", "loremaster")


def test_an_alias_missing_from_the_mapping_fails_loudly_instead_of_leaking():
    unmapped = ModelProfile(
        alias="test-unmapped", display_name="Test Unmapped", provider="openai",
        api_model="gpt-9000", base_url=None, secret_env="OPENAI_API_KEY",
        tier="economy", supports_attachments=True, enabled=True,
    )
    CATALOG["test-unmapped"] = unmapped
    try:
        with pytest.raises(KeyError):
            public_model_entry(unmapped)
        with pytest.raises(KeyError):
            public_model_id("test-unmapped")
    finally:
        del CATALOG["test-unmapped"]


def test_an_entry_with_no_tier_is_never_shown():
    with pytest.raises(LookupError, match="no tier"):
        public_model_entry(CATALOG["deepseek-v4-flash"])


def test_get_profile_by_public_id_knows_only_enabled_public_ids():
    assert get_profile_by_public_id(public_model_id(DEFAULT_ALIAS)) == get_profile(DEFAULT_ALIAS)
    # A disabled entry's id, the real alias, "auto" and noise all look alike.
    for unknown in (public_model_id("deepseek-v4-flash"), DEFAULT_ALIAS, "auto", "nope"):
        assert get_profile_by_public_id(unknown) is None, unknown


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

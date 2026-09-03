"""
Unit tests for ProviderClientFactory (agent-forge-harness-b8o.1, Checkpoint 1).

See docs/forge/plans/game-guide-ai-model-routing.md, "Provider client seam and
the existing test surface": the old seam was RagService reading `self.llm_client`
directly. TDD row 1 / the guard-test requirement: prove the graph resolves its
client through the factory, not a service-level attribute, so the old seam
cannot silently return.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_providers.py -q
"""

from __future__ import annotations

import pytest

from service.providers import ProviderClientFactory, UnknownOrDisabledModelError


class _FakeClient:
    def invoke(self, messages, config=None, **kw):  # pragma: no cover - identity only
        raise NotImplementedError


def test_client_for_returns_the_injected_fake():
    fake = _FakeClient()
    factory = ProviderClientFactory(client_builders={"gpt-4o-mini": fake})
    assert factory.client_for("gpt-4o-mini") is fake


def test_client_for_caches_and_returns_the_same_instance():
    fake = _FakeClient()
    factory = ProviderClientFactory(client_builders={"gpt-4o-mini": fake})
    assert factory.client_for("gpt-4o-mini") is factory.client_for("gpt-4o-mini")


def test_client_for_unknown_alias_raises():
    factory = ProviderClientFactory()
    with pytest.raises(UnknownOrDisabledModelError):
        factory.client_for("not-a-real-alias")


def test_live_openai_client_construction_disables_sdk_retries(monkeypatch):
    # Construction (not invocation) needs a key-shaped string but never
    # contacts the network — offline-testable. Guards Checkpoint 1 step 5:
    # the SDK's own retries must be off in favor of generate.py's
    # bounded service-owned retry (they ship together or the baseline
    # temporarily loses resilience).
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    factory = ProviderClientFactory()
    client = factory.client_for("gpt-4o-mini")
    assert client.max_retries == 0


def test_client_for_disabled_alias_raises_identically_to_unknown():
    # Same exception type/shape for "unknown" and "disabled" — a caller must
    # not be able to distinguish them (TDD row 1, mirrors get_profile()).
    from service.model_catalog import CATALOG, ModelProfile

    disabled = ModelProfile(
        alias="test-disabled-provider", display_name="x", provider="openai",
        api_model="gpt-9000", base_url=None, secret_env="OPENAI_API_KEY",
        tier="economy", supports_attachments=True, enabled=False,
    )
    CATALOG["test-disabled-provider"] = disabled
    try:
        factory = ProviderClientFactory()
        with pytest.raises(UnknownOrDisabledModelError):
            factory.client_for("test-disabled-provider")
    finally:
        del CATALOG["test-disabled-provider"]


# ---------------------------------------------------------------------------
# Client-compatibility contract tests (Checkpoint 1 slice 6). Prove the
# shared OpenAI-compatible adapter preserves base_url/model for each disabled
# frozen-v1 provider profile — construction only, sanitized fake keys, no
# network call, no real spend. `_build()` is called directly (not
# `client_for()`) because disabled profiles are deliberately unreachable
# through the public surface (TDD row 1) — this tests the lower-level
# construction capability the plan calls "the client compatibility surface,"
# separate from the enabled/disabled traffic gate. Opt-in, spend-capped LIVE
# smoke tests against real provider endpoints are explicitly out of scope
# here (no real API keys are provisioned for this session) — see the plan's
# "Checkpoint 1 step 6" for that follow-up.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "alias,secret_env,expected_base_url",
    [
        ("deepseek-v4-flash", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
        ("qwen-flash-us", "DASHSCOPE_API_KEY", "https://dashscope-us.aliyuncs.com/compatible-mode/v1"),
        ("kimi-k3", "MOONSHOT_API_KEY", "https://api.moonshot.ai/v1"),
    ],
)
def test_openai_compatible_provider_client_construction(
    monkeypatch, alias, secret_env, expected_base_url,
):
    from service.model_catalog import CATALOG

    monkeypatch.setenv(secret_env, "sk-test-not-a-real-key")
    profile = CATALOG[alias]
    client = ProviderClientFactory()._build(profile)
    assert client.max_retries == 0
    assert str(client.openai_api_base) == expected_base_url
    assert client.model_name == profile.api_model


def test_missing_provider_credential_raises_a_clear_error(monkeypatch):
    from service.model_catalog import CATALOG
    from service.providers import MissingProviderCredentialError

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    profile = CATALOG["deepseek-v4-flash"]
    with pytest.raises(MissingProviderCredentialError, match="DEEPSEEK_API_KEY"):
        ProviderClientFactory()._build(profile)

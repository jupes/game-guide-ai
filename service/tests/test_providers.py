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

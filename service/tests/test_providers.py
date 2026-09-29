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

import inspect
import re
from pathlib import Path

import httpx
import pytest

import config
from service import generate
from service.model_catalog import CATALOG
from service.provider_deadline import AttemptDeadlineTransport
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


@pytest.mark.parametrize("generation", [
    generate.generate_answer, generate.generate_suggestions,
    generate.generate_spell_content, generate.generate_stat_block,
])
def test_no_generation_call_can_build_its_own_client(generation):
    # Each used to build a bare ChatOpenAI when handed client=None: no timeout,
    # no attempt deadline, the SDK's retries on top of generate_result's
    # (agent-forge-harness-7gf). The factory is the only way in.
    assert inspect.signature(generation).parameters["client"].default is inspect.Parameter.empty


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


# ---------------------------------------------------------------------------
# Request and connect timeouts (agent-forge-harness-ihz). Without them a
# stalled provider held a /chat worker thread with no upper bound. Every alias
# the catalog knows, enabled or not, carries them from config on the sync
# client (invoke, stream) AND the async one (ainvoke, astream). Behaviour
# against a stalled provider: service/tests/test_generation_timeout.py.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alias", sorted(CATALOG))
def test_every_alias_client_carries_the_configured_timeouts(monkeypatch, alias):
    profile = CATALOG[alias]
    monkeypatch.setenv(profile.secret_env, "sk-test-not-a-real-key")
    monkeypatch.setattr(config, "LLM_REQUEST_TIMEOUT_S", 42.0)
    monkeypatch.setattr(config, "LLM_CONNECT_TIMEOUT_S", 3.0)
    client = ProviderClientFactory()._build(profile)
    expected = httpx.Timeout(42.0, connect=3.0)
    assert client.request_timeout == expected
    assert client.root_client.timeout == expected
    assert client.root_async_client.timeout == expected


@pytest.mark.parametrize("alias", sorted(CATALOG))
def test_every_alias_client_ends_each_attempt_at_connect_plus_request(monkeypatch, alias):
    # The per-attempt cost the budget test below charges is the deadline the
    # sync client's transport enforces (agent-forge-harness-2bb): any other sum
    # could let the attempts overrun the platform timeout unseen.
    profile = CATALOG[alias]
    monkeypatch.setenv(profile.secret_env, "sk-test-not-a-real-key")
    monkeypatch.setattr(config, "LLM_REQUEST_TIMEOUT_S", 42.0)
    monkeypatch.setattr(config, "LLM_CONNECT_TIMEOUT_S", 3.0)
    transport = ProviderClientFactory()._build(profile).root_client._client._transport
    assert isinstance(transport, AttemptDeadlineTransport)
    assert transport._deadline_s == 45.0


def test_the_timeouts_fit_the_retry_budget_inside_the_platform_request_timeout():
    # A stalled provider costs connect + request per attempt; all of
    # generate.py's attempts and backoffs must end before Cloud Run gives up
    # on the request (which cancels the request, not the thread).
    deploy = (Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh").read_text()
    platform = re.search(r"--timeout (\d+)", deploy)
    assert platform is not None
    backoff = sum(generate._RETRY_BACKOFF_SECONDS * n for n in range(1, generate._MAX_ATTEMPTS))
    per_attempt = config.LLM_CONNECT_TIMEOUT_S + config.LLM_REQUEST_TIMEOUT_S
    assert generate._MAX_ATTEMPTS * per_attempt + backoff < int(platform.group(1))


def test_a_timeout_setting_reads_its_environment_override(monkeypatch):
    monkeypatch.setenv("RAG_LLM_REQUEST_TIMEOUT_S", "12.5")
    assert config._seconds("RAG_LLM_REQUEST_TIMEOUT_S", 60.0) == 12.5


@pytest.mark.parametrize("raw", ["inf", "nan", "0", "-1"])
def test_a_timeout_setting_that_would_unbound_or_break_generation_is_refused(monkeypatch, raw):
    monkeypatch.setenv("RAG_LLM_REQUEST_TIMEOUT_S", raw)
    with pytest.raises(ValueError, match="RAG_LLM_REQUEST_TIMEOUT_S"):
        config._seconds("RAG_LLM_REQUEST_TIMEOUT_S", 60.0)

"""
ProviderClientFactory (agent-forge-harness-b8o.1, Checkpoint 1). Resolves a
model alias to an LLM client, replacing the old service-level `llm_client`
attribute that `service/graph.py` used to read directly.

See docs/forge/plans/game-guide-ai-model-routing.md, "Provider client seam and
the existing test surface": with the old seam, `generate_node`/`suggest_node`
read `svc.llm_client` unconditionally, so any future per-request routing logic
would never be exercised by tests that inject a fake client — a false green.
Making the factory the ONLY path into generation closes that gap.

Client construction uses the shared `langchain_openai.ChatOpenAI` adapter for
every provider (DeepSeek/Qwen/Kimi are OpenAI-compatible chat-completions
APIs, per the plan's provider research) — pointed at each profile's own
`base_url` with its own credential. Only `gpt-4o-mini` is enabled today;
DeepSeek/Qwen/Kimi profiles are disabled (see model_catalog.py) until
Checkpoint 3's evaluation matrix passes, but their construction is proven now
with sanitized offline contract tests (service/tests/test_providers.py) — no
live calls, no real spend. If a provider's real request/response shape ever
diverges from what the shared adapter can express, give it its own adapter
here rather than forcing the shared one to lie.
"""

from __future__ import annotations

import os

from .generate import LLMClient
from .model_catalog import ModelProfile, get_profile


class UnknownOrDisabledModelError(ValueError):
    """Raised by `client_for()` for any alias the catalog doesn't have
    enabled. Deliberately identical for an unknown alias and a disabled one —
    mirrors `model_catalog.get_profile()`'s never-distinguish-them contract
    (TDD row 1): a caller must not be able to confirm a disabled alias exists."""


class MissingProviderCredentialError(RuntimeError):
    """Raised when a profile's `secret_env` isn't set in the environment at
    construction time — a clear, provider-agnostic failure instead of each
    provider SDK's own differently-worded missing-credential error."""

    def __init__(self, alias: str, secret_env: str):
        super().__init__(f"{alias!r} requires the {secret_env} environment variable, which is not set")


class ProviderClientFactory:
    """Builds (and caches) one LLM client per enabled catalog alias.

    Tests inject pre-built fakes via `client_builders`, keyed by alias — the
    only supported construction path today; real (live) client construction
    for additional providers lands alongside their catalog entries in a later
    Checkpoint 1 slice. A resolved client is cached and reused across calls
    within one process, matching the previous behavior of the `ChatOpenAI`
    constructed once per `RagService`.
    """

    def __init__(self, *, client_builders: dict[str, LLMClient] | None = None):
        self._clients: dict[str, LLMClient] = dict(client_builders or {})

    def client_for(self, alias: str) -> LLMClient:
        if alias in self._clients:
            return self._clients[alias]
        profile = get_profile(alias)
        if profile is None:
            raise UnknownOrDisabledModelError(alias)
        client = self._build(profile)
        self._clients[alias] = client
        return client

    def _build(self, profile: ModelProfile) -> LLMClient:
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        from config import TEMPERATURE

        raw_key = os.environ.get(profile.secret_env) if profile.secret_env else None
        if profile.secret_env and not raw_key:
            raise MissingProviderCredentialError(profile.alias, profile.secret_env)
        api_key = SecretStr(raw_key) if raw_key is not None else None
        # max_retries=0: the SDK's own default retry (ChatOpenAI retries
        # transient failures internally) is disabled in favor of
        # generate.py's bounded service-owned retry, which emits an
        # observable attempt record per try instead of hiding retries inside
        # the SDK (agent-forge-harness-b8o.1, Checkpoint 1 step 5). base_url
        # unset (None) uses OpenAI's own default endpoint; every non-OpenAI
        # profile sets one to reach its own OpenAI-compatible endpoint.
        return ChatOpenAI(
            model=profile.api_model, temperature=TEMPERATURE, max_retries=0,
            base_url=profile.base_url, api_key=api_key,
        )

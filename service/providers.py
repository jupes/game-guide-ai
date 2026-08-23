"""
ProviderClientFactory (agent-forge-harness-b8o.1, Checkpoint 1). Resolves a
model alias to an LLM client, replacing the old service-level `llm_client`
attribute that `service/graph.py` used to read directly.

See docs/forge/plans/game-guide-ai-model-routing.md, "Provider client seam and
the existing test surface": with the old seam, `generate_node`/`suggest_node`
read `svc.llm_client` unconditionally, so any future per-request routing logic
would never be exercised by tests that inject a fake client — a false green.
Making the factory the ONLY path into generation closes that gap.

Checkpoint 1 only constructs OpenAI clients (the sole enabled catalog entry
today). Disabled DeepSeek/Qwen/Kimi profiles get real client construction in a
later slice of this checkpoint, once their base_url/secret_env plumbing is
proven with sanitized contract fixtures.
"""

from __future__ import annotations

from .generate import LLMClient
from .model_catalog import ModelProfile, get_profile


class UnknownOrDisabledModelError(ValueError):
    """Raised by `client_for()` for any alias the catalog doesn't have
    enabled. Deliberately identical for an unknown alias and a disabled one —
    mirrors `model_catalog.get_profile()`'s never-distinguish-them contract
    (TDD row 1): a caller must not be able to confirm a disabled alias exists."""


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

    def _build(self, profile: ModelProfile) -> LLMClient:  # pragma: no cover - live path
        if profile.provider != "openai":
            raise NotImplementedError(
                f"live client construction for provider {profile.provider!r} "
                "lands in a later Checkpoint 1 slice, alongside its contract fixtures"
            )
        from langchain_openai import ChatOpenAI

        from config import TEMPERATURE

        return ChatOpenAI(model=profile.api_model, temperature=TEMPERATURE)

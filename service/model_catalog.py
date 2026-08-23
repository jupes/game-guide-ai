"""
Server-owned model catalog (agent-forge-harness-b8o.1, Checkpoint 1). The single
source of truth for which generation-model aliases exist, their display
metadata, and whether they're currently enabled for traffic.

See docs/forge/plans/game-guide-ai-model-routing.md, "Server-owned model
catalog" + "Public API contracts". D2's sibling seam: this Checkpoint ships
only the catalog + the GET /models read surface. Nothing here yet resolves a
request's model preference against it — that's Checkpoint 2 (b8o.2, the manual
picker) and Checkpoint 4 (b8o.4, Auto routing). Provider client construction
(the factory that can actually build a DeepSeek/Qwen/Kimi client) is a later
slice of this same checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["economy", "balanced", "premium"]
Provider = Literal["openai", "alibaba", "deepseek", "moonshot"]


@dataclass(frozen=True)
class ModelProfile:
    """One catalog entry. `base_url`/`secret_env`/`api_model` are fixed server
    configuration, never sent to the browser — see `public_model_entry()`."""

    alias: str
    display_name: str
    provider: Provider
    api_model: str
    base_url: str | None
    secret_env: str | None
    tier: Tier
    supports_attachments: bool
    enabled: bool
    fallback_alias: str | None = None


# Checkpoint 1 ships one enabled profile: the current production baseline,
# unchanged from today's config.DEFAULT_MODEL. Disabled DeepSeek/Qwen/Kimi
# profiles land in a later slice of this checkpoint, alongside the provider
# client factory that can actually construct their clients (D2 sibling work).
CATALOG: dict[str, ModelProfile] = {
    "gpt-4o-mini": ModelProfile(
        alias="gpt-4o-mini", display_name="GPT-4o mini", provider="openai",
        api_model="gpt-4o-mini", base_url=None, secret_env="OPENAI_API_KEY",
        tier="economy", supports_attachments=True, enabled=True,
    ),
}

DEFAULT_ALIAS = "gpt-4o-mini"


def enabled_profiles() -> list[ModelProfile]:
    """Catalog entries eligible for the public GET /models response, in a
    stable (insertion) order. Never includes a disabled profile (TDD row 1)."""
    return [p for p in CATALOG.values() if p.enabled]


def get_profile(alias: str) -> ModelProfile | None:
    """An enabled profile by alias, or None. Deliberately identical for an
    unknown alias and a disabled one — a caller must never be able to confirm
    that a disabled alias exists (TDD row 1)."""
    profile = CATALOG.get(alias)
    return profile if profile is not None and profile.enabled else None


def public_model_entry(profile: ModelProfile) -> dict[str, object]:
    """The public GET /models shape for one entry: id/display_name/tier/
    supports_attachments only. Never api_model, base_url, or secret_env."""
    return {
        "id": profile.alias,
        "display_name": profile.display_name,
        "tier": profile.tier,
        "supports_attachments": profile.supports_attachments,
    }

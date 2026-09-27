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
# unchanged from today's config.DEFAULT_MODEL. The three disabled profiles
# below are frozen v1 candidates (plan's "Provider onboarding and API keys")
# — construction is proven by sanitized offline contract tests
# (service/tests/test_providers.py), but none are eligible for traffic until
# Checkpoint 3's evaluation matrix passes and, for Qwen, D7's US-residency
# confirmation. DeepSeek stays synthetic-data-only pending retention terms
# (see the plan's "Provider posture" section) even once/if ever enabled.
CATALOG: dict[str, ModelProfile] = {
    "gpt-4o-mini": ModelProfile(
        alias="gpt-4o-mini", display_name="GPT-4o mini", provider="openai",
        api_model="gpt-4o-mini", base_url=None, secret_env="OPENAI_API_KEY",
        tier="economy", supports_attachments=True, enabled=True,
    ),
    "deepseek-v4-flash": ModelProfile(
        alias="deepseek-v4-flash", display_name="DeepSeek V4 Flash", provider="deepseek",
        api_model="deepseek-v4-flash", base_url="https://api.deepseek.com",
        secret_env="DEEPSEEK_API_KEY", tier="economy", supports_attachments=True,
        enabled=False,
    ),
    "qwen-flash-us": ModelProfile(
        alias="qwen-flash-us", display_name="Qwen Flash (US)", provider="alibaba",
        api_model="qwen-flash-us", base_url="https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        secret_env="DASHSCOPE_API_KEY", tier="economy", supports_attachments=True,
        enabled=False,
    ),
    "kimi-k3": ModelProfile(
        alias="kimi-k3", display_name="Kimi K3", provider="moonshot",
        api_model="kimi-k3", base_url="https://api.moonshot.ai/v1",
        secret_env="MOONSHOT_API_KEY", tier="premium", supports_attachments=True,
        enabled=False,
    ),
}

DEFAULT_ALIAS = "gpt-4o-mini"

# Bumped whenever CATALOG's alias set or policy meaningfully changes; recorded
# on each conversation's strategy binding (b8o.2, D6) so a future change can
# tell which catalog shape a given conversation was bound under. Static for
# now — becomes meaningful once the catalog actually changes after launch.
CATALOG_REVISION = "v1"


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


PublicTier = Literal["traveller", "adventurer", "loremaster"]


@dataclass(frozen=True)
class PublicModel:
    """All the browser may know of one catalog entry (owner decision D-9, bead
    au3): users never learn which model or provider answers. `id` is what the
    client sends back as `model_preference`; `tier` is None while no tier has
    been assigned, and such an entry is never shown."""

    id: str
    tier: PublicTier | None
    label: str


# PLACEHOLDER COPY, pending the design lane (bead au3). This table and the
# automatic entry below are the ONLY source of what GET /models, the /chat
# routing blocks and the UI model picker show about a model. Every CATALOG key
# must appear here: a missing one raises KeyError, never falls back to the
# alias. Two enabled entries in one tier would both read as that tier; that
# case is the tier mapping's (b8o.4, iov), not this table's.
PUBLIC_MODELS: dict[str, PublicModel] = {
    "gpt-4o-mini": PublicModel(id="traveller", tier="traveller", label="Traveller"),
    "deepseek-v4-flash": PublicModel(id="unassigned-1", tier=None, label="Unassigned"),
    "qwen-flash-us": PublicModel(id="unassigned-2", tier=None, label="Unassigned"),
    "kimi-k3": PublicModel(id="unassigned-3", tier=None, label="Unassigned"),
}

AUTO_PUBLIC_ENTRY: dict[str, object] = {
    "id": "auto",
    "display_name": "Automatic",
    "description": "Balances speed, cost, and task difficulty.",
}


def public_model_id(alias: str) -> str:
    """The id the client knows `alias` by. KeyError for an unmapped alias."""
    return PUBLIC_MODELS[alias].id


def get_profile_by_public_id(public_id: str) -> ModelProfile | None:
    """An enabled profile by its public id, or None — for an unknown id and a
    disabled entry's id alike, as `get_profile` (TDD row 1)."""
    for alias, public in PUBLIC_MODELS.items():
        if public.id == public_id:
            return get_profile(alias)
    return None


def public_model_entry(profile: ModelProfile) -> dict[str, object]:
    """The public GET /models shape for one entry: id/display_name/tier/
    supports_attachments only, the first three from PUBLIC_MODELS. Never the
    alias, display name, provider, api_model, base_url or secret_env."""
    public = PUBLIC_MODELS[profile.alias]
    if public.tier is None:
        raise LookupError("a catalog entry with no tier cannot be shown")
    return {
        "id": public.id,
        "display_name": public.label,
        "tier": public.tier,
        "supports_attachments": profile.supports_attachments,
    }

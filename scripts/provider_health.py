#!/usr/bin/env python3
"""Provider catalog health (agent-forge-harness-b8o.1, Checkpoint 1 step 9).

Reports every catalog entry's alias/enabled status/credential presence — an
operator tool, unlike `GET /models`, which only reports enabled aliases (TDD
row 1). Useful before flipping a disabled profile to `enabled=True`: confirms
its secret is actually set without ever printing the value.

Usage:
    uv run python scripts/provider_health.py
    uv run python scripts/provider_health.py --json
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.model_catalog import CATALOG, ModelProfile  # noqa: E402


def credential_present(profile: ModelProfile) -> bool:
    """Whether `profile.secret_env` is set in the environment. Never reads,
    returns, or logs the value itself — presence only."""
    return bool(profile.secret_env and os.environ.get(profile.secret_env))


def catalog_status() -> list[dict[str, object]]:
    """One row per catalog entry, enabled or not (this is an operator tool,
    not the public GET /models surface). `base_url`/`api_model` are
    configuration, not secrets, and are shown for debugging; the credential
    ITSELF is never included, only whether it's present."""
    return [
        {
            "alias": p.alias,
            "provider": p.provider,
            "enabled": p.enabled,
            "tier": p.tier,
            "api_model": p.api_model,
            "base_url": p.base_url,
            "secret_env": p.secret_env,
            "credential_present": credential_present(p),
        }
        for p in CATALOG.values()
    ]


def _print_table(rows: list[dict[str, object]]) -> None:
    print(f"{'alias':22s} {'provider':10s} {'enabled':8s} {'tier':10s} {'credential':10s}")
    print("-" * 64)
    for row in rows:
        cred = "present" if row["credential_present"] else "MISSING"
        print(
            f"{row['alias']!s:22s} {row['provider']!s:10s} "
            f"{'yes' if row['enabled'] else 'no':8s} {row['tier']!s:10s} {cred:10s}"
        )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rows = catalog_status()
    if "--json" in argv:
        import json

        print(json.dumps(rows, indent=2))
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

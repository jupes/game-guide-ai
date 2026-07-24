"""Unit test for the $10 budget kill-switch (x5bz.1.3, test #5).

The Cloud Function source lives at scripts/gcp/billing_killswitch/main.py and is
deployed separately (it is NOT part of the installable package), so we load it by
path. The google-cloud-billing client is faked — the test pins the *decision*:
billing gets disabled at/over budget and is never touched under it. Disabling
billing is the only hard cap; alerts alone don't stop spend.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

MAIN_PATH = Path(__file__).resolve().parents[1] / "billing_killswitch" / "main.py"


def _load():
    assert MAIN_PATH.exists(), f"{MAIN_PATH} does not exist yet"
    spec = importlib.util.spec_from_file_location("killswitch_main", MAIN_PATH)
    assert spec and spec.loader, f"cannot load {MAIN_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ks = _load()


class _FakeBillingClient:
    """Records update_project_billing_info calls instead of hitting the API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def update_project_billing_info(self, name: str, project_billing_info: dict) -> None:
        self.calls.append((name, project_billing_info))


def _event(cost: float, budget: float) -> dict:
    """A Pub/Sub budget notification: base64 JSON in event['data']."""
    payload = json.dumps({"costAmount": cost, "budgetAmount": budget}).encode("utf-8")
    return {"data": base64.b64encode(payload)}


def test_disables_billing_at_or_over_budget() -> None:
    client = _FakeBillingClient()
    ks.disable_billing_if_over_budget(
        _event(10.0, 10.0), billing_client=client, project_id="game-guide-ai-pilot"
    )
    assert client.calls, "billing must be disabled when cost >= budget"
    name, info = client.calls[0]
    assert name == "projects/game-guide-ai-pilot"
    # Detaching the billing account (empty name) is what actually stops spend.
    assert info["billing_account_name"] == ""


def test_does_not_disable_under_budget() -> None:
    client = _FakeBillingClient()
    ks.disable_billing_if_over_budget(
        _event(4.2, 10.0), billing_client=client, project_id="game-guide-ai-pilot"
    )
    assert client.calls == [], "billing must NOT be touched under budget"


def test_should_disable_is_pure_threshold_logic() -> None:
    assert ks._should_disable(10.0, 10.0) is True
    assert ks._should_disable(11.0, 10.0) is True
    assert ks._should_disable(9.99, 10.0) is False
    assert ks._should_disable(5.0, 0.0) is False  # no budget configured → never act

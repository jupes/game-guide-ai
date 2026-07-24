"""$10 hard-cap billing kill-switch for game-guide-ai-pilot (x5bz.1.3).

A Cloud Function subscribed to the pilot budget's Pub/Sub topic. Budget *alerts*
only notify — they don't stop spend — so when actual cost reaches the budget this
detaches the project's billing account, which halts all billable resources. That
is the only guarantee behind the "$10/mo hard cap" pilot decision.

Deploy + wiring (topic, budget, IAM) live in docs/deploy-gcp.md. The decision
logic is kept pure and the billing client injectable so it is unit-tested without
GCP (scripts/gcp/tests/test_billing_killswitch.py).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any


def _should_disable(cost: float, budget: float) -> bool:
    """Disable only when a budget is set and actual cost has reached it."""
    return budget > 0 and cost >= budget


def _parse_budget_event(event: dict) -> tuple[float, float]:
    """Extract (cost, budget) from a Pub/Sub budget notification.

    The payload is base64 JSON in ``event['data']`` with ``costAmount`` and
    ``budgetAmount`` fields (GCP budget notification schema).
    """
    raw = base64.b64decode(event["data"]).decode("utf-8")
    payload = json.loads(raw)
    return float(payload.get("costAmount", 0.0)), float(payload.get("budgetAmount", 0.0))


def _default_billing_client() -> Any:
    # Imported lazily so tests (which inject a fake) never need google-cloud-billing.
    from google.cloud.billing import CloudBillingClient

    return CloudBillingClient()


def disable_billing_if_over_budget(
    event: dict,
    context: Any = None,
    *,
    billing_client: Any = None,
    project_id: str | None = None,
) -> str:
    """Pub/Sub-triggered entrypoint. Detaches billing when over budget.

    Fail-closed on spend: any real invocation that reaches the threshold detaches
    the billing account. Idempotent — GCP no-ops a detach when billing is already
    off, so repeat notifications are safe.
    """
    cost, budget = _parse_budget_event(event)
    if not _should_disable(cost, budget):
        return f"under budget (cost={cost}, budget={budget}); no action"

    project_id = project_id or os.environ.get("GCP_PROJECT", "")
    if not project_id:
        raise RuntimeError("GCP_PROJECT is not set; cannot identify project to disable")

    client = billing_client or _default_billing_client()
    project_name = f"projects/{project_id}"
    # Empty billing_account_name == detach the billing account == stop all spend.
    client.update_project_billing_info(
        name=project_name,
        project_billing_info={"billing_account_name": ""},
    )
    return f"billing DISABLED for {project_name} (cost={cost} >= budget={budget})"

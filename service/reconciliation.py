"""The reconciliation job every revocation leaves behind (beads 1kg.2.2 and 1kg.2.3).

A revocation — a GM's Remove, and `1kg.2.3`'s End, expiry and Rotate — makes its
change true at once, in a first step that never asks for the campaign lock
(RQ-5). What that first step cannot do without the lock is advance the
campaign's `authz_revision` and clear the slots the change made stale, so it
enqueues **one** job of the kind `campaign.reconcile`, last, with **no dedupe
key** (RQ-3, RQ-12): two revocations are two jobs, never one absorbed into the
other, so a reconciliation that already started can never swallow a later one.

The job carries the campaign's id and nothing else, and its handler reads
current state, never a payload beyond that id. It runs in its own transaction:

1. `lock_campaign(campaign_id, shared=False)` — the first lock, exclusive;
2. `reconcile_slots(unit, campaign_id)` — a **required** extension point, empty
   on this base, which `1kg.7.1` fills (a slot whose session is not live or
   whose participant is not active is cleared); `1kg.2.3` adds its own step
   beside it;
3. advance `authz_revision`;
4. commit.

`CampaignAuthzMissing` — the campaign is gone — completes the job as a no-op.
A lock timeout or a deadlock raises, and the runner retries it with capped
backoff for as long as it takes (`max_attempts=None`): a revocation's
reconciliation is never abandoned.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from . import campaign_identity as ident
from .db import CampaignAuthzMissing, TransactionalDatabase, UnitOfWork
from .jobs import Job, JobContext, JobHandler, JobQueue

log = logging.getLogger(__name__)

#: The one reconciliation kind, shared by every revocation of both beads.
RECONCILE_KIND: Final = "campaign.reconcile"

#: What the slot step is handed: the unit of work, holding the campaign lock
#: exclusively, and the campaign's id.
SlotReconcile = Callable[[UnitOfWork, str], None]


def enqueue_reconciliation(unit: UnitOfWork, jobs: JobQueue, campaign_id: str) -> int:
    """Leave the reconciliation job in `unit`'s transaction, and return its id.

    **No dedupe key** (RQ-12), and the payload is the campaign's id alone — no
    alias, no address, no participant — so a job row carries nothing private
    (SEC-20). Call it LAST in the transaction: the outbox is the bottom of RQ-3's
    lock order."""
    return jobs.enqueue(unit, RECONCILE_KIND, {"campaign_id": ident.check_id(ident.CAMPAIGN, campaign_id)})


def reconcile_slots(unit: UnitOfWork, campaign_id: str) -> None:
    """The slot-reconciling extension point, empty on this base.

    `reconcile` calls it holding the campaign lock exclusively, before the
    revision advances, so that when `1kg.7.1` gives it a body the slots it
    clears and the revision that tells readers to look again cannot come apart.
    Until then there are no slots to clear, and this says so in one place
    rather than by omission. It is REQUIRED wherever a handler is built —
    passed by name, never defaulted — as the table-session store's `slot_clear`
    is."""
    return None


def reconcile(db: TransactionalDatabase, campaign_id: str, *, slots: SlotReconcile) -> bool:
    """One reconciliation, in a transaction of its own. True when it advanced
    the revision; False when the campaign is gone, which completes the job."""
    try:
        with db.transaction() as unit:
            unit.lock_campaign(campaign_id, shared=False)
            slots(unit, campaign_id)
            unit.advance_authz_revision(campaign_id)
    except CampaignAuthzMissing:
        return False
    return True


def handler(db: TransactionalDatabase, *, slots: SlotReconcile) -> JobHandler:
    """The `campaign.reconcile` handler, retried until it succeeds.

    A payload this module did not write — no campaign id, or one outside the
    `cmp_` shape — completes as a no-op rather than failing for ever: there is
    nothing it could name to reconcile. Only the fact is logged, never the
    payload."""

    def run(job: Job, context: JobContext) -> None:
        del context  # one short transaction, bounded by RQ-8's transaction_timeout
        campaign_id = job.payload.get("campaign_id")
        if not isinstance(campaign_id, str) or not ident.is_id(ident.CAMPAIGN, campaign_id):
            log.warning("reconciliation: job %d names no campaign; completed as a no-op", job.id)
            return
        reconcile(db, campaign_id, slots=slots)

    return JobHandler(run=run, max_attempts=None)

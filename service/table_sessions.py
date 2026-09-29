"""The live table session's lifecycle (agent-forge-harness-1kg.2.3).

Start, End and Rotate by the session's owner, expiry by the system, the screen
grant that makes the owner's browser a table screen, and the facts a stream will
check before every frame. There is **no secret on the wire for any account**
(SEC-41, SEC-43): the one bearer secret left is the screen grant, minted by the
owner and handed back exactly once (SEC-48, D-13). This module composes the
stores in `service/table_session_store.py`, `service/campaign_store.py` and
`service/audit_log.py` and the job outbox; it holds no HTTP (the routes are the
edges' and call it).

**Start is a locked widening, in two transactions, owner-checked before any
lock** (L-4). One clock for the request. An unlocked read shows the campaign is
the caller's and not archived, else the one not-found answer (`MissingParent`):
the campaign lock is never taken on a campaign the caller has not been shown to
own, because an exclusive lock a stranger could take would stall that GM's
reveals for the lock timeout. The first transaction finalises this GM's live
sessions that are already past `expires_at` as expiries. The second takes the
campaign lock first, then answers a replayed command with the session it opened
(whatever its state), answers a live session of this campaign as it is, counts
the fan-out bound, inserts, advances `authz_revision`, records `session.started`
and enqueues `table_session.expire` at `expires_at` and then its start divider
job (`1kg.3.5`), **last**. Start never ends a table in another campaign: that is
`LiveSessionExists`, and the client sends an End and then a Start, two intents.

**End, expiry and Rotate are revocations** (L-5, RQ-5's first step) under the
session row alone: never the campaign lock, never a `lock_timeout` of their own.
The row is held with the owner in the locking statement (the system's `expire`
names no owner and acts only on a session already due). Under it: the slots and
both epochs, every screen grant, the state or the generation, the audit row, and
`campaign.reconcile` `{campaign_id}` enqueued **last** (only an ending's divider
job follows it), with no dedupe key (RQ-12); the route runs it after its
response (`job_driver.run_after_response`), so the acknowledgement never waits
for it. A deadlock victim is retried by the server,
in a fresh transaction, up to three attempts; then `BackendUnavailable`, which
is the database being unavailable and never a refusal. **End is never refused**
— not by the bound, not for state — because refusing it would hold a revealed
table open (X-3); an End of a session that is not live answers it as it stands.
**Rotate replays by `command_id`** (the contract's Idempotency row): the same
command answers the session as it stands; a new one always rotates a live
session (narrowing always wins).

**SEC-35's per-campaign bound** is read from the ledger — `session.started`,
`session.ended` and `session.rotated` rows in the trailing window — and refuses
Start and Rotate at it (`FanOutBound`); End is counted and never refused (DV-1),
and expiry is not counted. **Ownership is always decided first**: Start's count
is made under the campaign lock after its ownership read; Rotate's after an
owner-scoped, unlocked read of the session, so a stranger gets the one
not-found answer whether or not that campaign is at the bound, and the count is
never an oracle for "this GM is running a table" (SEC-46).

**The screen grant** (L-9 to L-11): the owner's unlocked read of the live
session (none: `Inactive`, SEC-46's one table answer, with no lock taken), then
the store's mint under the session's advisory lock (`ScreenLimit` at the bound),
then `screen.minted`. Validity is the reader's three-part test; a lost race
leaves a grant that is dead on arrival. The GM's per-screen revoke and the
screen's Leave touch one grant each and advance nothing.

**Per-frame liveness** (L-12): a `Binding` a stream holds (the session id —
every session starts at generation 1, so the generation alone is no binding —
the admission generation, and for a screen its grant id), one batched read of
many sessions, and `may_write`, a pure predicate over them and a clock. The
account/seat half of the table principal is `1kg.7.2`'s and `1kg.7.5`'s, and
composes with it.

Nothing here logs, raises or records a secret, a digest, an email, an alias or
a campaign name (SEC-20, SEC-21): a log line names the operation and a count.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import psycopg

from . import campaign_identity as ident
from .audit_log import ActorKind, AuditAction, AuditLog, Decision, ObjectKind
from .campaign_store import CampaignStore, MissingParent, NotLive, aware
from .db import CampaignAuthzMissing, TransactionalDatabase, UnitOfWork
from .jobs import Job, JobContext, JobHandler, JobQueue
from .table_session_store import (
    ENDED,
    EXPIRED,
    LIVE,
    ROTATED,
    Closing,
    Liveness,
    LiveScreen,
    ScreenGrant,
    TableSession,
    TableSessionStore,
    check_command_id,
)
from .workbench_contracts import SessionBoundary

log = logging.getLogger(__name__)

#: How long a table session lives (REVEAL-2 leaves the number to this bead). A
#: module constant and not a setting: a table is an evening, not a deployment.
SESSION_LIFETIME = timedelta(hours=12)
#: SEC-35's suggested per-campaign fan-out bound: this many Starts, Ends and
#: Rotates of one campaign in any trailing window refuse the next Start or Rotate.
FANOUT_BOUND = 10
FANOUT_WINDOW = timedelta(seconds=60)
FANOUT_ACTIONS = (
    AuditAction.SESSION_STARTED,
    AuditAction.SESSION_ENDED,
    AuditAction.SESSION_ROTATED,
)
#: How many times End, Rotate and expiry are tried when they lose a deadlock.
DEADLOCK_ATTEMPTS = 3
#: The delayed job Start enqueues for its session's expiry (L-6).
EXPIRE_KIND = "table_session.expire"

#: Enqueue the reconciliation of a campaign's authorisation inside the unit —
#: `campaign.reconcile` `{campaign_id}`, no dedupe key — and return the job's id.
#: `service/reconciliation.py` (`1kg.2.2`) owns that kind; `service/app.py`
#: passes its `enqueue_reconciliation` in, and this module never names it.
EnqueueReconcile = Callable[[UnitOfWork, str], int]

#: Enqueue one session divider job inside the unit — the session's id and the
#: boundary, no dedupe key — and return the job's id (`1kg.3.5`).
#: `service/session_dividers.py` owns that kind; `service/app.py` passes its
#: `enqueuer` in, and this module never names it. Start enqueues a `start` after
#: the expiry job; a closing whose outcome is `ended` or `expired` enqueues an
#: `end` after its reconciliation, so a divider job is always last (RQ-3). A
#: Rotate that rotated moves neither boundary (REVEAL-17).
EnqueueDivider = Callable[[UnitOfWork, str, SessionBoundary], int]

_T = TypeVar("_T")


class TableSessionRefusal(Exception):
    """What the lifecycle refuses, beside the stores' own refusals. Each message
    is fixed and names nothing a caller supplied."""


class FanOutBound(TableSessionRefusal):
    """SEC-35's per-campaign bound: too many Starts, Ends and Rotates of this
    campaign in the trailing window. Reached only after ownership is decided."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("too many table-session changes in that campaign just now")
        self.retry_after_s = retry_after_s


class Inactive(TableSessionRefusal):
    """SEC-46's one table answer: there is no live table here that this caller
    may act on. One refusal for every reason — not the owner, no live session,
    a session that ended under the request — so none can be told apart."""

    def __init__(self) -> None:
        super().__init__("no live table session for that caller")


class BackendUnavailable(TableSessionRefusal):
    """The database, not a decision: the campaign lock timed out or a deadlock
    was lost, and the server's own retries are spent. Retryable (RQ-4, RQ-8)."""

    def __init__(self) -> None:
        super().__init__("the table session could not be changed just now")


@dataclass(frozen=True)
class Outcome:
    """A session as a route answers it, and the `campaign.reconcile` jobs this call
    enqueued, which the route runs after its response."""

    session: TableSession
    reconcile_jobs: tuple[int, ...] = ()


@dataclass(frozen=True)
class MintedScreen:
    """A new screen grant: the record, the grant itself — which leaves in the one
    `Set-Cookie` of the answer and nowhere else — and when the session ends."""

    grant: ScreenGrant
    secret: str = field(repr=False)
    ends_at: datetime


@dataclass(frozen=True)
class SessionStatus:
    """The GM's status read: the campaign's live session, else the one most
    recently started, and its live screens (none unless it is live)."""

    session: TableSession
    screens: tuple[ScreenGrant, ...]


@dataclass(frozen=True)
class Binding:
    """What a stream holds and checks every frame against (L-12)."""

    session_id: str
    generation: int
    #: A screen's own grant; None for a stream bound to an account.
    screen_id: str | None = None


def may_write(binding: Binding, read: Mapping[str, Liveness], now: datetime) -> bool:
    """Whether a frame may be written to `binding`, against one batched `read`
    and a clock: its session is there, live and unexpired, still at the
    binding's generation, and — for a screen — its grant is still among the
    live ones. Pure: no I/O, and no clock but the one it is given."""
    moment = aware(now, "a clock")
    found = read.get(binding.session_id)
    if found is None or found.state != LIVE or found.expires_at <= moment:
        return False
    if found.generation != binding.generation:
        return False
    return binding.screen_id is None or binding.screen_id in found.live_screens


class TableSessions:
    """The lifecycle, over one database and the stores that share it.

    `dividers` is the session-divider enqueuer (`1kg.3.5`). `None` enqueues no
    divider and is for tests only: every construction outside the tests passes
    it by name, and `service/tests/test_session_dividers.py` reads the source to
    prove it, as the extension points `slot_clear` and `slots` are passed."""

    def __init__(
        self,
        db: TransactionalDatabase,
        *,
        campaigns: CampaignStore,
        sessions: TableSessionStore,
        audit: AuditLog,
        jobs: JobQueue,
        reconcile: EnqueueReconcile,
        clock: Callable[[], datetime] | None = None,
        dividers: EnqueueDivider | None = None,
    ) -> None:
        self._db = db
        self._campaigns = campaigns
        self._sessions = sessions
        self._audit = audit
        self._jobs = jobs
        self._reconcile = reconcile
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._dividers = dividers

    def _now(self, now: datetime | None) -> datetime:
        """The request's one clock: the caller's, or this service's, captured once."""
        return aware(self._clock() if now is None else now, "a clock")

    # ── Start ────────────────────────────────────────────────────────────────

    def start(
        self, owner_id: int, campaign_id: str, *, command_id: str, now: datetime | None = None
    ) -> Outcome:
        moment = self._now(now)
        check_command_id(command_id)

        def overdue(unit: UnitOfWork) -> list[int]:
            campaign = self._campaigns.get(unit, campaign_id, owner_id=owner_id)
            if campaign is None or campaign.is_archived:
                raise MissingParent("no such campaign for that owner")
            return self._finalise_overdue(unit, owner_id, moment)

        jobs = self._retrying("start", overdue)
        try:
            with self._db.transaction() as unit:
                session = self._start_locked(unit, owner_id, campaign_id, command_id, moment)
        except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected):
            raise BackendUnavailable() from None
        except CampaignAuthzMissing:
            raise MissingParent("no such campaign for that owner") from None
        return Outcome(session, tuple(jobs))

    def _finalise_overdue(self, unit: UnitOfWork, owner_id: int, moment: datetime) -> list[int]:
        """Start's first transaction: this GM's live sessions already past their
        expiry, finalised as expiries, each reconciliation enqueued after every
        row this transaction holds."""
        closed = [
            closing
            for stale in self._sessions.expired_live_sessions_for_gm(unit, owner_id, now=moment)
            if (closing := self._sessions.expire(unit, stale.id, stale.campaign_id, now=moment))
            is not None
            and closing.outcome is not None
        ]
        for closing in closed:
            self._record(unit, closing, None, moment)
        jobs = [self._reconcile(unit, closing.session.campaign_id) for closing in closed]
        for closing in closed:
            self._divide(unit, closing)
        return jobs

    def _start_locked(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, command_id: str, moment: datetime
    ) -> TableSession:
        unit.lock_campaign(campaign_id, shared=False)
        replayed = self._sessions.by_start_command(unit, campaign_id, command_id)
        if replayed is not None:
            return replayed
        current = self._sessions.latest_for_campaign(unit, campaign_id)
        if current is not None and current.live_at(moment):
            return current
        self._check_bound(unit, campaign_id, moment)
        session = self._sessions.start(
            unit,
            campaign_id,
            owner_id=owner_id,
            expires_at=moment + SESSION_LIFETIME,
            command_id=command_id,
            now=moment,
        )
        revision = unit.advance_authz_revision(campaign_id)
        self._audit.append(
            unit,
            campaign_id=campaign_id,
            actor_kind=ActorKind.GM,
            action=AuditAction.SESSION_STARTED,
            object_kind=ObjectKind.TABLE_SESSION,
            decision=Decision.ALLOWED,
            actor_ref=str(owner_id),
            object_ref=session.id,
            authz_revision=revision,
            detail={"session_id": session.id, "generation": session.link_generation},
            now=moment,
        )
        self._jobs.enqueue(
            unit,
            EXPIRE_KIND,
            {"session_id": session.id, "campaign_id": campaign_id},
            run_after=session.expires_at,
            now=moment,
        )
        if self._dividers is not None:
            self._dividers(unit, session.id, SessionBoundary.START)
        return session

    # ── End, Rotate and expiry: the revocations ─────────────────────────────

    def end(
        self, owner_id: int, campaign_id: str, session_id: str, *, now: datetime | None = None
    ) -> Outcome:
        moment = self._now(now)

        def work(unit: UnitOfWork) -> Outcome:
            closing = self._sessions.end(
                unit, campaign_id, session_id, owner_id=owner_id, now=moment
            )
            if closing is None:
                raise MissingParent("no such table session for that owner")
            return self._settle(unit, closing, owner_id, moment)

        return self._retrying("end", work)

    def rotate(
        self,
        owner_id: int,
        campaign_id: str,
        session_id: str,
        *,
        command_id: str,
        now: datetime | None = None,
    ) -> Outcome:
        moment = self._now(now)
        check_command_id(command_id)

        def work(unit: UnitOfWork) -> Outcome:
            found = self._sessions.find_owned(unit, campaign_id, session_id, owner_id=owner_id)
            if found is None:
                raise MissingParent("no such table session for that owner")
            if found.live_at(moment) and found.rotate_command_id != command_id:
                self._check_bound(unit, campaign_id, moment)
            closing = self._sessions.rotate(
                unit,
                campaign_id,
                session_id,
                owner_id=owner_id,
                command_id=command_id,
                now=moment,
            )
            if closing is None:
                raise MissingParent("no such table session for that owner")
            return self._settle(unit, closing, owner_id, moment)

        return self._retrying("rotate", work)

    def expire(
        self, campaign_id: str, session_id: str, *, now: datetime | None = None
    ) -> Outcome | None:
        """The system's path (the expiry job, and nothing that names an owner).
        None: no such session in that campaign."""
        moment = self._now(now)

        def work(unit: UnitOfWork) -> Outcome | None:
            closing = self._sessions.expire(unit, session_id, campaign_id, now=moment)
            return None if closing is None else self._settle(unit, closing, None, moment)

        return self._retrying("expire", work)

    def _settle(
        self, unit: UnitOfWork, closing: Closing, owner_id: int | None, moment: datetime
    ) -> Outcome:
        """What follows a revocation under its row: the audit row, then the
        reconciliation, then an ending's divider job, last. Nothing when nothing
        was written."""
        if closing.outcome is None:
            return Outcome(closing.session)
        self._record(unit, closing, owner_id, moment)
        reconciled = self._reconcile(unit, closing.session.campaign_id)
        self._divide(unit, closing)
        return Outcome(closing.session, (reconciled,))

    def _divide(self, unit: UnitOfWork, closing: Closing) -> None:
        """An ending's divider job, keyed on the outcome, never the operation:
        an End or a Rotate of an overdue session expires it, and that is an end.
        A rotation is the same session and moves no boundary (REVEAL-17)."""
        if self._dividers is not None and closing.outcome in (ENDED, EXPIRED):
            self._dividers(unit, closing.session.id, SessionBoundary.END)

    def _record(
        self, unit: UnitOfWork, closing: Closing, owner_id: int | None, moment: datetime
    ) -> None:
        """An expiry is the clock's, whoever pressed the button (actor `system`);
        an End or a Rotate is the owner's."""
        action = {
            ENDED: AuditAction.SESSION_ENDED,
            EXPIRED: AuditAction.SESSION_EXPIRED,
            ROTATED: AuditAction.SESSION_ROTATED,
        }[closing.outcome or ""]
        by_system = closing.outcome == EXPIRED or owner_id is None
        session = closing.session
        self._audit.append(
            unit,
            campaign_id=session.campaign_id,
            actor_kind=ActorKind.SYSTEM if by_system else ActorKind.GM,
            action=action,
            object_kind=ObjectKind.TABLE_SESSION,
            decision=Decision.ALLOWED,
            actor_ref=None if by_system else str(owner_id),
            object_ref=session.id,
            detail={
                "session_id": session.id,
                "generation": session.link_generation,
                "screens_revoked": closing.screens_revoked,
            },
            now=moment,
        )

    def _check_bound(self, unit: UnitOfWork, campaign_id: str, moment: datetime) -> None:
        seen = self._audit.count_since(
            unit, campaign_id, FANOUT_ACTIONS, since=moment - FANOUT_WINDOW
        )
        if seen >= FANOUT_BOUND:
            raise FanOutBound(int(FANOUT_WINDOW.total_seconds()))

    def _retrying(self, operation: str, work: Callable[[UnitOfWork], _T]) -> _T:
        """`work` in a fresh transaction per attempt, retried when it is chosen
        as a deadlock's victim (RQ-3). Everything else propagates at once."""
        for attempt in range(1, DEADLOCK_ATTEMPTS + 1):
            try:
                with self._db.transaction() as unit:
                    return work(unit)
            except psycopg.errors.DeadlockDetected:
                log.warning(
                    "table sessions: %s lost a deadlock (attempt %d of %d)",
                    operation,
                    attempt,
                    DEADLOCK_ATTEMPTS,
                )
        raise BackendUnavailable()

    # ── The GM's status read ─────────────────────────────────────────────────

    def status(
        self, owner_id: int, campaign_id: str, *, now: datetime | None = None
    ) -> SessionStatus | None:
        """The campaign's live session, else its most recent one, with its live
        screens; None for a campaign of the caller's that never started one.
        Writes nothing. `MissingParent` for a campaign that is not theirs."""
        moment = self._now(now)
        with self._db.transaction() as unit:
            if self._campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
                raise MissingParent("no such campaign for that owner")
            session = self._sessions.latest_for_campaign(unit, campaign_id)
            if session is None:
                return None
            screens = self._sessions.live_screens(unit, session.id, now=moment)
        return SessionStatus(session, tuple(screens))

    # ── The screen grant ─────────────────────────────────────────────────────

    def mint_screen(
        self, owner_id: int, campaign_id: str, *, now: datetime | None = None
    ) -> MintedScreen:
        moment = self._now(now)
        with self._db.transaction() as unit:
            session = self._sessions.live_owned(unit, campaign_id, owner_id=owner_id, now=moment)
            if session is None:
                raise Inactive()
            try:
                grant, secret = self._sessions.mint_screen(
                    unit, campaign_id, session.id, owner_id=owner_id, now=moment
                )
            except (MissingParent, NotLive):
                raise Inactive() from None
            self._audit.append(
                unit,
                campaign_id=campaign_id,
                actor_kind=ActorKind.GM,
                action=AuditAction.SCREEN_MINTED,
                object_kind=ObjectKind.TABLE_SCREEN,
                decision=Decision.ALLOWED,
                actor_ref=str(owner_id),
                object_ref=grant.id,
                detail={"session_id": session.id, "generation": grant.link_generation},
                now=moment,
            )
        return MintedScreen(grant, secret, session.expires_at)

    def resolve_screen(self, secret: str, *, now: datetime | None = None) -> LiveScreen | None:
        """The live grant a screen's cookie names, or None — for an unknown one
        and a dead one alike."""
        moment = self._now(now)
        with self._db.transaction() as unit:
            return self._sessions.resolve_screen(unit, ident.digest(secret), now=moment)

    def revoke_screen(
        self, owner_id: int, campaign_id: str, screen_id: str, *, now: datetime | None = None
    ) -> None:
        """The owner revokes one screen. Idempotent; `MissingParent` for another
        GM's screen or none. Advances nothing and enqueues nothing (§15.3)."""
        moment = self._now(now)
        with self._db.transaction() as unit:
            found = self._sessions.revoke_screen(
                unit, campaign_id, screen_id, owner_id=owner_id, now=moment
            )
            if found is None:
                raise MissingParent("no such table screen for that owner")
            session_id, revoked = found
            if revoked:
                self._audit.append(
                    unit,
                    campaign_id=campaign_id,
                    actor_kind=ActorKind.GM,
                    action=AuditAction.SCREEN_REVOKED,
                    object_kind=ObjectKind.TABLE_SCREEN,
                    decision=Decision.ALLOWED,
                    actor_ref=str(owner_id),
                    object_ref=screen_id,
                    reason_code="gm_revoked",
                    detail={"session_id": session_id},
                    now=moment,
                )

    def leave(self, secret: str, *, now: datetime | None = None) -> bool:
        """A screen leaves: its grant is revoked if it is live. Whether it was —
        a dead or unknown grant changes nothing and is not an error (SEC-49)."""
        moment = self._now(now)
        with self._db.transaction() as unit:
            left = self._sessions.leave(unit, ident.digest(secret), now=moment)
            if left is None:
                return False
            self._audit.append(
                unit,
                campaign_id=left.campaign_id,
                actor_kind=ActorKind.SCREEN,
                action=AuditAction.SCREEN_REVOKED,
                object_kind=ObjectKind.TABLE_SCREEN,
                decision=Decision.ALLOWED,
                actor_ref=left.grant_id,
                object_ref=left.grant_id,
                reason_code="left",
                detail={"session_id": left.session_id},
                now=moment,
            )
        return True

    # ── Per-frame liveness ───────────────────────────────────────────────────

    def liveness(self, session_ids: Collection[str]) -> dict[str, Liveness]:
        """Every named session's liveness facts, in one query."""
        with self._db.transaction() as unit:
            return self._sessions.liveness(unit, session_ids)

    # ── The expiry job ───────────────────────────────────────────────────────

    def expire_handler(self) -> JobHandler:
        """`table_session.expire`, retried until it succeeds (`max_attempts=None`)."""
        return JobHandler(self._run_expire, max_attempts=None)

    def _run_expire(self, job: Job, context: JobContext) -> None:
        """Nothing to do for a session that is gone or no longer live; finalise
        one that is due; and for one not due yet — a clock between instances —
        enqueue a fresh job at its expiry and complete, never raise. Every reader
        compares `expires_at` with the clock anyway (SEC-42): this is the trigger
        for expiry's reconciliation, not the revocation."""
        session_id, campaign_id = job.payload.get("session_id"), job.payload.get("campaign_id")
        if not isinstance(session_id, str) or not isinstance(campaign_id, str):
            return
        moment = self._now(None)
        with self._db.transaction() as unit:
            current = self._sessions.get(unit, session_id)
        if current is None or current.campaign_id != campaign_id or not current.is_live:
            return
        if not current.is_expired(moment):
            with self._db.transaction() as unit:
                self._jobs.enqueue(
                    unit,
                    EXPIRE_KIND,
                    {"session_id": session_id, "campaign_id": campaign_id},
                    run_after=current.expires_at,
                    now=moment,
                )
            return
        self.expire(campaign_id, session_id, now=moment)

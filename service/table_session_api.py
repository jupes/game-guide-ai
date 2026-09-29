"""The GM's live table session over HTTP (bead 1kg.2.3's PR-B,
agent-forge-harness-1kg.2.10).

Three Workbench GM routes on a `workbench_router` (`agent-forge-harness-oe6`),
wired into `service/app.py` and importing nothing from it: the application
hands `build_router` its GM gate, the table-session lifecycle
(`service/table_sessions.py`), its job driver and the Paid check point
`start_gate`. The lifecycle holds every rule of Start, End, Rotate, expiry and
the screen grant; this module turns a request into one call and its answer
into the wire's shapes, and holds no rule of its own but the order of checks.

- `GET /campaigns/{campaign_id}/table-session`: `TableSessionAnswer` — the live
  session, else the one most recently started, else `session: null`. Writes
  nothing.
- `POST /campaigns/{campaign_id}/table-session`: `TableSessionAnswer` after a
  `start`, `end` or `rotate` (`TableSessionRequest`).
- `DELETE /campaigns/{campaign_id}/table-session/screens/{screen_id}`: `204`, a
  repeat too (L-11).

**The order of checks** (SEC-3, L-15), as a client observes it: origin (403) →
authentication (the one 401) → role (403) → the body (422) → **for Start
only, `start_gate`** (L-19) → the database (503) → **ownership, in the
statement** (the one 404: another GM's campaign, a missing one, an archived
one for Start, a session of another campaign, another GM's screen) → SEC-35's
bound (429, Start and Rotate only; End is never refused, DV-1) → state. The
body is read by hand, as every campaign route reads it (`read_body`), so a
malformed body is a 422 after the role check rather than before the origin
check; it depends on nothing but the body either way.

**`start_gate` is called once, by Start, before any database access**
(L-19, D-3): running a live table is Paid, and `ubw`/`yje.4.1` give the gate
its body. End, Rotate, the status read and a screen revoke never call it — a
narrowing is never gated on payment.

**End and Rotate run their reconciliation after the answer**
(`job_driver.run_after_response`), never before it: the acknowledgement of a
revocation never waits for `campaign.reconcile` (RQ-5). So does a Start that
finalised an overdue session first.

**What the GM is shown** (L-14): a session still `live` past `ends_at` reads
`ended`, with `ended_at` its `ends_at` (SEC-42); only a live session lists
screens, each by id and times — never its grant or its digest (SEC-48).

No handler logs a name, an alias or a secret, and no refusal repeats what it
was sent. No route here builds a 401, 403 or 404 of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response

from . import campaign_identity as ident
from .campaign_store import LiveSessionExists, MissingParent
from .campaigns_api import WIRE_VERSION, conflict, guarded, parse_body, refusal, unavailable
from .conversations_api import read_body
from .job_driver import JobDriver, run_after_response
from .session import SessionData
from .table_session_store import ScreenGrant
from .table_session_store import TableSession as StoredSession
from .table_sessions import BackendUnavailable, FanOutBound, Outcome, TableSessions
from .workbench_api import SessionDependency, not_found, workbench_router
from .workbench_contracts import (
    ErrorCode,
    SessionAction,
    SessionState,
    TableScreen,
    TableSession,
    TableSessionAnswer,
    TableSessionRequest,
)

log = logging.getLogger(__name__)

#: Fixed sentences. A refusal never interpolates anything it was sent (X-7).
BUSY_MESSAGE = "The table couldn't be changed just now. Try again."
LIVE_ELSEWHERE_MESSAGE = "Your table is live in another campaign. End it first."
THROTTLED_MESSAGE = "The table has changed a lot just now. Wait, then try again."


#: The Paid check point (L-19): returns to admit, raises the refusal.
type StartGate = Callable[[SessionData], None]


def get_clock() -> datetime:
    """One `now` per request, handed to the lifecycle. Tests override it."""
    return datetime.now(UTC)


# ── From the lifecycle to the wire ───────────────────────────────────────────


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def session_to_wire(session: StoredSession, screens: tuple[ScreenGrant, ...], now: datetime) -> TableSession:
    """A stored session as its GM sees it at `now`. A row still `live` past its
    expiry is dead (SEC-42): it reads `ended`, at the moment it stopped serving.
    Only a live session lists its screens."""
    live = session.live_at(now)
    ended_at = None if live else _utc(session.ended_at or session.expires_at)
    return TableSession(
        schema_version=WIRE_VERSION,
        session_id=session.id,
        campaign_id=session.campaign_id,
        state=SessionState.LIVE if live else SessionState.ENDED,
        gen=session.link_generation,
        audio_epoch=session.audio_epoch,
        reveal_epoch=session.reveal_epoch,
        started_at=_utc(session.started_at),
        ends_at=_utc(session.expires_at),
        ended_at=ended_at,
        audio=session.table_audio,
        screens=[
            TableScreen(
                screen_id=grant.id,
                created_at=_utc(grant.created_at),
                last_seen_at=None if grant.last_connected_at is None else _utc(grant.last_connected_at),
            )
            for grant in (screens if live else ())
        ],
    )


def _answer(session: TableSession | None) -> TableSessionAnswer:
    return TableSessionAnswer(schema_version=WIRE_VERSION, session=session)


# ── Refusals ─────────────────────────────────────────────────────────────────


def lifecycle_call[T](work: Callable[[], T]) -> T:
    """Run one lifecycle call and map what it refuses. `MissingParent` is the
    one 404; `LiveSessionExists` is `409 live_elsewhere`; SEC-35's bound is `429
    throttled_user` with its wait; the database's own failures — a lock timeout,
    three lost deadlocks, a driver error — are one retryable 503. Each is raised
    outside the `except`, so nothing is chained."""
    failure: HTTPException | None = None
    missing = False
    try:
        return guarded(work, busy_message=BUSY_MESSAGE)
    except MissingParent:
        missing = True
    except LiveSessionExists:
        failure = conflict(ErrorCode.LIVE_ELSEWHERE, LIVE_ELSEWHERE_MESSAGE)
    except FanOutBound as exc:
        failure = refusal(
            429, ErrorCode.THROTTLED_USER, THROTTLED_MESSAGE, retryable=True, retry_after_s=exc.retry_after_s
        )
    except BackendUnavailable:
        failure = unavailable(BUSY_MESSAGE)
    if missing:
        not_found()
    assert failure is not None
    raise failure


# ── The routes ───────────────────────────────────────────────────────────────


def build_router(
    gm: SessionDependency,
    lifecycle: Callable[[], TableSessions | None],
    driver: Callable[[], JobDriver | None],
    start_gate: StartGate,
) -> APIRouter:
    """The three routes on a `workbench_router`, given the app's GM gate, the
    lifecycle getter, the job driver getter and the Paid check point."""
    router = workbench_router(gm)

    def _service(sessions: TableSessions | None) -> TableSessions:
        if sessions is None:
            raise unavailable(BUSY_MESSAGE)
        return sessions

    def _screens(
        service: TableSessions, owner_id: int, session: StoredSession, now: datetime
    ) -> tuple[ScreenGrant, ...]:
        """A live answer's live screens, read after its write committed. Only a
        live session has any, and a campaign's live session is the one its
        status read finds."""
        if not session.live_at(now):
            return ()
        status = lifecycle_call(lambda: service.status(owner_id, session.campaign_id, now=now))
        if status is None or status.session.id != session.id:
            return ()
        return status.screens

    def _reconcile_after(tasks: BackgroundTasks, runner: JobDriver | None, outcome: Outcome) -> None:
        for job_id in outcome.reconcile_jobs:
            run_after_response(tasks, runner, job_id)

    @router.get("/campaigns/{campaign_id}/table-session", response_model=TableSessionAnswer)
    def read(
        campaign_id: str,
        user: SessionData = Depends(gm),
        sessions: TableSessions | None = Depends(lifecycle),
        now: datetime = Depends(get_clock),
    ) -> TableSessionAnswer:
        """The GM's status read. Writes nothing, and is never gated on a tier."""
        service = _service(sessions)
        if not ident.is_id(ident.CAMPAIGN, campaign_id):
            not_found()
        status = lifecycle_call(lambda: service.status(user.user_id, campaign_id, now=now))
        if status is None:
            return _answer(None)
        return _answer(session_to_wire(status.session, status.screens, now))

    @router.post("/campaigns/{campaign_id}/table-session", response_model=TableSessionAnswer)
    def command(
        campaign_id: str,
        tasks: BackgroundTasks,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        sessions: TableSessions | None = Depends(lifecycle),
        runner: JobDriver | None = Depends(driver),
        now: datetime = Depends(get_clock),
    ) -> TableSessionAnswer:
        """Start, End or Rotate. Start alone passes the Paid check point, and
        before anything touches the database."""
        body = parse_body(TableSessionRequest, raw)
        if body.action is SessionAction.START:
            start_gate(user)
        service = _service(sessions)
        named = body.session_id
        if not ident.is_id(ident.CAMPAIGN, campaign_id) or (
            named is not None and not ident.is_id(ident.TABLE_SESSION, named)
        ):
            not_found()
        owner = user.user_id
        if body.action is SessionAction.START:
            outcome = lifecycle_call(lambda: service.start(owner, campaign_id, command_id=body.command_id, now=now))
        elif body.action is SessionAction.END:
            assert named is not None
            outcome = lifecycle_call(lambda: service.end(owner, campaign_id, named, now=now))
        else:
            assert named is not None
            outcome = lifecycle_call(
                lambda: service.rotate(owner, campaign_id, named, command_id=body.command_id, now=now)
            )
        _reconcile_after(tasks, runner, outcome)
        return _answer(session_to_wire(outcome.session, _screens(service, owner, outcome.session, now), now))

    @router.delete("/campaigns/{campaign_id}/table-session/screens/{screen_id}", status_code=204)
    def revoke(
        campaign_id: str,
        screen_id: str,
        user: SessionData = Depends(gm),
        sessions: TableSessions | None = Depends(lifecycle),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """The owner revokes one screen (L-11): idempotent, and it advances
        nothing — the table slot stays for everyone else (§15.3)."""
        service = _service(sessions)
        if not (ident.is_id(ident.CAMPAIGN, campaign_id) and ident.is_id(ident.TABLE_CREDENTIAL, screen_id)):
            not_found()
        lifecycle_call(lambda: service.revoke_screen(user.user_id, campaign_id, screen_id, now=now))
        return Response(status_code=204)

    return router

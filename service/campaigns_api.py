"""The GM's campaigns and the seats at their table (bead 1kg.2.2).

Nine Workbench routes on a `workbench_router` (`agent-forge-harness-oe6`), wired
into `service/app.py` and importing nothing from it: the application hands
`build_router` its GM gate, its database getter, the re-authentication callable
Remove needs, and the job queue and driver its reconciliation needs.

| Route | Answers |
| --- | --- |
| `GET /campaigns` | the caller's campaigns, newest first |
| `POST /campaigns` | `201`, a campaign whose GM is the caller (D-5) |
| `GET /campaigns/{campaign_id}` | one campaign, archived or not |
| `PATCH /campaigns/{campaign_id}` | rename, archive, restore |
| `GET /campaigns/{campaign_id}/participants` | the seats, oldest first |
| `POST /campaigns/{campaign_id}/participants` | `201`, an open seat |
| `POST …/participants/{participant_id}/offer` | `204`, whatever the address holds |
| `POST …/participants/{participant_id}/confirm` | the confirmed seat |
| `POST …/participants/{participant_id}/remove` | `204`, behind the password |

**The order of checks** (SEC-3, L-18): origin (403) → authentication (the one
401) → role (403) → body-only validation (422) → the database (503) →
[Remove: the auth throttle (429), then the password (403)] → **ownership, in the
statement** (the one 404) → [archive: step 1] → the campaign lock, on a locked
write → validation that depends on the resource or the caller (422) → state
(409, or an offer repeat's `204`) → [offer: the throttle (429)] → the write.
A 409, a resource-dependent 422 and the offer throttle are therefore unreachable
for a campaign the caller does not own.

**Ownership comes before the lock.** Every locked write first reads the
campaign with its owner in the statement, as a plain `SELECT` that takes no row
lock, so `lock_campaign` is still the transaction's first lock (RQ-2); only a
campaign the caller owns is ever locked, so a stranger can neither stall
another GM's reveals nor learn from a lock timeout that a campaign exists.

**Locks, revision and audit** follow L-14: add, confirm, archive and restore are
locked widenings or fact-changing narrowings that advance `authz_revision`; an
offer takes the exclusive lock only to serialise its repeat check and advances
nothing; Remove is a revocation that NEVER asks for the campaign lock and
leaves `campaign.reconcile` behind (`service/reconciliation.py`). A lock timeout
or a deadlock victim is one retryable `503`, with only its SQLSTATE logged.

**No private text anywhere.** No handler logs a name, an alias, an address or
a password, and no refusal repeats what it was sent. Every 422 is raised as a
`RequestValidationError` carrying no input, answered by the application's one
validation handler. Handlers log an exception's TYPE or SQLSTATE only, never
its message, `__cause__` or `__context__` (a database error's DETAIL quotes the
row). No route here builds a 401, 403 or 404 of its own: those are the
scaffolding's `not_found()` and `reauth_failed()`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, TypeAdapter, ValidationError

from . import campaign_identity as ident
from .audit_log import ActorKind, AuditAction, AuditLog, Decision, ObjectKind, PostgresAuditLog
from .campaign_store import AliasTaken, CampaignStore, InvalidCursor, PostgresCampaignStore, SeatNotAccepted
from .campaign_store import Campaign as StoredCampaign
from .campaign_store import SeatUnavailable as _SeatUnavailable
from .conversations_api import read_body
from .db import AdvisoryLock, CampaignAuthzMissing, PgTransaction, TransactionalDatabase, UnitOfWork
from .job_driver import JobDriver, run_after_response
from .jobs import JobQueue
from .participant_store import Participant, ParticipantStore, PostgresParticipantStore, check_alias
from .reconciliation import enqueue_reconciliation
from .seat_offer_store import (
    THROTTLE_LIMIT,
    PostgresSeatOfferStore,
    SeatOffer,
    SeatOfferStore,
    address_key,
    seat_status,
    throttle_wait_s,
)
from .session import SessionData
from .table_session_store import PostgresTableSessionStore, TableSessionStore, no_slots
from .workbench_api import SessionDependency, not_found, workbench_router
from .workbench_contracts import (
    CAMPAIGN_PAGE_MAX_ITEMS,
    CONTRACT_VERSION,
    Campaign,
    CampaignCreateRequest,
    CampaignPage,
    CampaignPatchRequest,
    Cursor,
    ErrorBody,
    ErrorCode,
    ErrorInfo,
    SchemaVersion,
    Seat,
    SeatCreateRequest,
    SeatOfferRequest,
    SeatPage,
    SeatRemoveRequest,
    trim,
)

log = logging.getLogger(__name__)

#: `Literal[1]` on the wire, `int` as a constant: spelled once.
WIRE_VERSION = cast("SchemaVersion", CONTRACT_VERSION)
#: SEC-50(3), *suggested*: a campaign seats at most this many, open, offered
#: and accepted alike — each is a seat row.
SEAT_CAP = 40

#: Fixed sentences. A refusal never interpolates anything it was sent (X-7).
UNAVAILABLE_MESSAGE = "Campaigns are briefly unavailable. Try again."
NOT_APPLIED_MESSAGE = "That change isn't applied yet. Try again."
ARCHIVED_MESSAGE = "That campaign is archived."
ALIAS_TAKEN_MESSAGE = "Someone at this table already has that name."
SEAT_CAP_MESSAGE = f"A table seats at most {SEAT_CAP}."
SEAT_NOT_OPEN_MESSAGE = "That seat isn't open."
SEAT_NOT_ACCEPTED_MESSAGE = "Nobody has accepted that seat yet."
THROTTLED_MESSAGE = "You've made a lot of offers today. Try again later."

#: The two SQLSTATEs a locked write answers as "busy, try again" (RQ-8):
#: `lock_not_available` and `deadlock_detected`.
_BUSY = (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected)

#: What the re-authentication dependency hands a route: a check of one
#: password against the account this request signed in as, which raises the
#: answer or returns (L-12).
type PasswordCheck = Callable[[str], None]


# ── What the routes compose ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CampaignStores:
    """The stores a campaign route composes in one transaction."""

    campaigns: CampaignStore
    participants: ParticipantStore
    sessions: TableSessionStore
    offers: SeatOfferStore
    audit: AuditLog


def get_campaign_stores() -> CampaignStores:
    """The stores every route uses. Tests override this dependency. The slot
    clear is `no_slots` until `1kg.7.1` gives it a body."""
    return CampaignStores(
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresTableSessionStore(slot_clear=no_slots),
        PostgresSeatOfferStore(),
        PostgresAuditLog(),
    )


def get_clock() -> datetime:
    """One `now` per request. Tests override it rather than sleeping."""
    return datetime.now(UTC)


# ── Refusals ─────────────────────────────────────────────────────────────────
# The 401, the 403s and the 404 are the scaffolding's (`workbench_api`); these
# are the answers it has no envelope for.


def refusal(
    status: int,
    code: ErrorCode,
    message: str,
    *,
    retryable: bool = False,
    retry_after_s: int | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    info = ErrorInfo(code=code, message=message, retryable=retryable, retry_after_s=retry_after_s)
    body = ErrorBody(detail=info).model_dump(mode="json", exclude_none=True)
    return HTTPException(status_code=status, detail=body["detail"], headers=headers)


def conflict(code: ErrorCode, message: str) -> HTTPException:
    return refusal(409, code, message)


def unavailable(message: str = UNAVAILABLE_MESSAGE) -> HTTPException:
    return refusal(503, ErrorCode.BACKEND_UNAVAILABLE, message, retryable=True)


def invalid(field: str | None, part: str = "body") -> RequestValidationError:
    """A 422 naming the field at fault — never its value. The application's one
    validation handler answers it (SEC-23)."""
    loc: tuple[str, ...] = (part, field) if field is not None else ()
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": "invalid"}])


def parse_body[M: BaseModel](model: type[M], raw: bytes) -> M:
    """The body as the contract reads it, or the Workbench 422. The error list
    handed on carries no input, and it is raised outside the `except`, so it
    chains nothing: a ValidationError's `input` is the request."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
    raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors])


def logged_outage(exc: BaseException, message: str = UNAVAILABLE_MESSAGE) -> HTTPException:
    """The exception's TYPE and SQLSTATE only: its message can quote a row."""
    sqlstate = getattr(exc, "sqlstate", None)
    log.warning("campaign route: database unavailable (%s%s)", type(exc).__name__, f", {sqlstate}" if sqlstate else "")
    return unavailable(message)


def guarded[T](work: Callable[[], T], *, busy_message: str = UNAVAILABLE_MESSAGE) -> T:
    """Run one route's database work and map what the database says.

    `CampaignAuthzMissing` — the campaign was deleted between the ownership read
    and the lock — is the one 404. A lock timeout or a deadlock victim is a
    retryable 503 (`busy_message`); any other driver error is a 503 too. Each
    is raised OUTSIDE the `except`, so nothing is chained."""
    failure: HTTPException | None = None
    missing = False
    try:
        return work()
    except CampaignAuthzMissing:
        missing = True
    except _BUSY as exc:
        failure = logged_outage(exc, busy_message)
    except psycopg.Error as exc:
        failure = logged_outage(exc)
    if missing:
        not_found()
    assert failure is not None
    raise failure


# ── Queries ──────────────────────────────────────────────────────────────────

_LIMIT: TypeAdapter[int] = TypeAdapter(int)
_CURSOR: TypeAdapter[str] = TypeAdapter(Cursor)
_BOOLEANS = {"true": True, "false": False}


@dataclass(frozen=True)
class PageQuery:
    limit: int
    cursor: str | None
    flag: bool


def parse_page_query(limit: str | None, cursor: str | None, flag: str | None = None, flag_name: str = "") -> PageQuery:
    """A list's parameters, or the 422 naming the first at fault. Each is read
    here as a string, never by FastAPI, whose readings are laxer."""
    size = CAMPAIGN_PAGE_MAX_ITEMS
    if limit is not None:
        try:
            size = _LIMIT.validate_python(limit)
        except ValidationError:
            size = 0
        if not 1 <= size <= CAMPAIGN_PAGE_MAX_ITEMS:
            raise invalid("limit", "query")
    if cursor is not None:
        try:
            _CURSOR.validate_python(cursor)
        except ValidationError:
            cursor_ok = False
        else:
            cursor_ok = True
        if not cursor_ok:
            raise invalid("cursor", "query")
    chosen = False
    if flag is not None:
        if flag not in _BOOLEANS:
            raise invalid(flag_name, "query")
        chosen = _BOOLEANS[flag]
    return PageQuery(size, cursor, chosen)


def readable(prefix: str, value: str) -> bool:
    """A path id against its prefix's shape. Outside it is the same 404 as
    missing, before any query (L-18)."""
    return ident.is_id(prefix, value)


# ── From the stores to the wire ──────────────────────────────────────────────


def _utc(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.astimezone(UTC)


def campaign_to_wire(row: StoredCampaign) -> Campaign:
    return Campaign.model_validate(
        {
            "schema_version": WIRE_VERSION,
            "campaign_id": row.id,
            "name": row.name,
            "created_at": _utc(row.created_at),
            "updated_at": _utc(row.updated_at),
            "archived_at": _utc(row.archived_at),
        }
    )


def seat_to_wire(seat: Participant, latest: SeatOffer | None, now: datetime) -> Seat:
    """A seat as its GM sees it: the status one function derives, and the
    latest offer's address as the GM typed it. No account id (SEC-50(1))."""
    return Seat.model_validate(
        {
            "schema_version": WIRE_VERSION,
            "participant_id": seat.id,
            "alias": seat.alias,
            "status": seat_status(seat, latest, now),
            "address": None if latest is None else latest.address,
            "created_at": _utc(seat.created_at),
            "offered_at": None if latest is None else _utc(latest.created_at),
            "offer_expires_at": None if latest is None else _utc(latest.expires_at),
            "accepted_at": _utc(seat.accepted_at),
            "confirmed_at": _utc(seat.confirmed_at),
            "removed_at": _utc(seat.removed_at),
        }
    )


# ── The compositions: one transaction each, in L-14's lock order ────────────


def _audit(
    stores: CampaignStores,
    unit: UnitOfWork,
    *,
    campaign_id: str,
    owner_id: int,
    action: AuditAction,
    object_kind: ObjectKind,
    object_ref: str,
    detail: dict[str, str | int | bool],
    revision: int | None,
    now: datetime,
    reason_code: str | None = None,
) -> None:
    stores.audit.append(
        unit,
        campaign_id=campaign_id,
        actor_kind=ActorKind.GM,
        action=action,
        object_kind=object_kind,
        decision=Decision.ALLOWED,
        actor_ref=str(owner_id),
        object_ref=object_ref,
        reason_code=reason_code,
        authz_revision=revision,
        detail=detail,
        now=now,
    )


def archive(
    db: TransactionalDatabase,
    stores: CampaignStores,
    *,
    campaign_id: str,
    owner_id: int,
    now: datetime,
    name: str | None = None,
) -> StoredCampaign:
    """Archive is a fact-changing narrowing in RQ-5's two steps (L-4)."""
    archive_step_one(db, stores, campaign_id=campaign_id, owner_id=owner_id)
    return archive_step_two(db, stores, campaign_id=campaign_id, owner_id=owner_id, now=now, name=name)


def archive_step_one(db: TransactionalDatabase, stores: CampaignStores, *, campaign_id: str, owner_id: int) -> None:
    """Its own transaction, which NEVER calls `lock_campaign` and is never
    refused on state: the campaign with its owner in the statement, then — if
    it is not archived yet — narrow its live session (the session row, the
    reveal epoch, the slots). Committed before step 2 asks for the lock."""
    with db.transaction() as unit:
        campaign = stores.campaigns.get(unit, campaign_id, owner_id=owner_id)
        if campaign is None:
            not_found()
        if campaign.is_archived:
            return
        if isinstance(unit, PgTransaction):
            unit.lock_campaign(campaign_id, shared=False)
        live = stores.sessions.live_session_for_campaign(unit, campaign_id)
        if live is not None:
            stores.sessions.narrow(unit, campaign_id, live.id)


def archive_step_two(
    db: TransactionalDatabase,
    stores: CampaignStores,
    *,
    campaign_id: str,
    owner_id: int,
    now: datetime,
    name: str | None = None,
) -> StoredCampaign:
    """In the request, its own transaction: the exclusive lock first (ownership
    was shown in step 1, and the owner never changes), the campaign read again,
    the scan again — a session started between the steps is narrowed too — and
    then the fact, the revision and the audit row. An archived campaign changes
    nothing: no narrowing, no audit row, no advance."""
    with db.transaction() as unit:
        unit.lock_campaign(campaign_id, shared=False)
        campaign = stores.campaigns.get(unit, campaign_id, owner_id=owner_id)
        if campaign is None:
            not_found()
        if name is not None:
            stores.campaigns.rename(unit, campaign_id, owner_id=owner_id, name=name, now=now)
        if not campaign.is_archived:
            live = stores.sessions.live_session_for_campaign(unit, campaign_id)
            if live is not None:
                stores.sessions.narrow(unit, campaign_id, live.id)
            stores.campaigns.set_archived(unit, campaign_id, owner_id=owner_id, archived=True, now=now)
            revision = unit.advance_authz_revision(campaign_id)
            _audit(
                stores, unit, campaign_id=campaign_id, owner_id=owner_id, action=AuditAction.CAMPAIGN_ARCHIVED,
                object_kind=ObjectKind.CAMPAIGN, object_ref=campaign_id, detail={"campaign_id": campaign_id},
                revision=revision, now=now,
            )
        final = stores.campaigns.get(unit, campaign_id, owner_id=owner_id)
        if final is None:
            not_found()
        return final


def restore(
    db: TransactionalDatabase,
    stores: CampaignStores,
    *,
    campaign_id: str,
    owner_id: int,
    now: datetime,
    name: str | None = None,
) -> StoredCampaign:
    """A locked widening (RQ-4), one transaction: the plain read with the owner,
    and no lock at all for a campaign that is not archived."""
    with db.transaction() as unit:
        campaign = stores.campaigns.get(unit, campaign_id, owner_id=owner_id)
        if campaign is None:
            not_found()
        if campaign.is_archived:
            unit.lock_campaign(campaign_id, shared=False)
            again = stores.campaigns.get(unit, campaign_id, owner_id=owner_id)
            if again is None:
                not_found()
            if again.is_archived:
                stores.campaigns.set_archived(unit, campaign_id, owner_id=owner_id, archived=False, now=now)
                revision = unit.advance_authz_revision(campaign_id)
                _audit(
                    stores, unit, campaign_id=campaign_id, owner_id=owner_id,
                    action=AuditAction.CAMPAIGN_RESTORED, object_kind=ObjectKind.CAMPAIGN, object_ref=campaign_id,
                    detail={"campaign_id": campaign_id}, revision=revision, now=now,
                )
        if name is not None:
            stores.campaigns.rename(unit, campaign_id, owner_id=owner_id, name=name, now=now)
        final = stores.campaigns.get(unit, campaign_id, owner_id=owner_id)
        if final is None:
            not_found()
        return final


def rename(
    db: TransactionalDatabase, stores: CampaignStores, *, campaign_id: str, owner_id: int, name: str, now: datetime
) -> StoredCampaign:
    """No lock, no revision, no audit row: one statement, the owner in it."""
    with db.transaction() as unit:
        renamed = stores.campaigns.rename(unit, campaign_id, owner_id=owner_id, name=name, now=now)
    if renamed is None:
        not_found()
    return renamed


def add_seat(
    db: TransactionalDatabase, stores: CampaignStores, *, campaign_id: str, owner_id: int, alias: str, now: datetime
) -> Participant:
    """A locked widening (RQ-4): ownership, the exclusive lock, then the state
    under it — archived, the cap counted under the lock (RC-11), the alias —
    then the seat, the revision and the audit row."""
    with db.transaction() as unit:
        if stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
            not_found()
        live_seats = stores.participants.count_live(unit, campaign_id)
        unit.lock_campaign(campaign_id, shared=False)
        campaign = stores.campaigns.get(unit, campaign_id, owner_id=owner_id)
        if campaign is None:
            not_found()
        if campaign.is_archived:
            raise conflict(ErrorCode.CAMPAIGN_ARCHIVED, ARCHIVED_MESSAGE)
        if live_seats >= SEAT_CAP:
            raise conflict(ErrorCode.SEAT_CAP_REACHED, SEAT_CAP_MESSAGE)
        seat: Participant | None = None
        try:
            seat = stores.participants.add(unit, campaign_id, alias=alias, now=now)
        except AliasTaken:
            seat = None
        if seat is None:
            raise conflict(ErrorCode.ALIAS_TAKEN, ALIAS_TAKEN_MESSAGE)
        revision = unit.advance_authz_revision(campaign_id)
        _audit(
            stores, unit, campaign_id=campaign_id, owner_id=owner_id, action=AuditAction.PARTICIPANT_ADDED,
            object_kind=ObjectKind.PARTICIPANT, object_ref=seat.id, detail={"participant_id": seat.id},
            revision=revision, now=now,
        )
        return seat


def offer_seat(
    db: TransactionalDatabase,
    stores: CampaignStores,
    *,
    campaign_id: str,
    participant_id: str,
    owner_id: int,
    owner_email: str | None,
    address: str,
    now: datetime,
) -> bool:
    """L-6's ten steps, in exactly that order, in one transaction. True when an
    offer row was written; False for a repeat, which changes nothing. Either
    way the route answers the same `204`.

    It never reads an account row or a block: the answer must not depend on
    whether anybody holds the address (SEC-50(2))."""
    key = address_key(address)
    with db.transaction() as unit:
        # 3. Ownership, then the lock, then the seat.
        if stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
            not_found()
        repeat_before_the_lock = stores.offers.is_repeat(unit, campaign_id, key, now=now)
        unit.lock_campaign(campaign_id, shared=False)
        seat = stores.participants.hold(unit, participant_id, campaign_id=campaign_id)
        if seat is None or not seat.is_active:
            not_found()
        # 4. Validation that depends on the caller: the owner's own address.
        if owner_email is not None and key == address_key(trim(owner_email)):
            raise invalid("email")
        # 5. State: archived.
        campaign = stores.campaigns.get(unit, campaign_id, owner_id=owner_id)
        if campaign is None:
            not_found()
        if campaign.is_archived:
            raise conflict(ErrorCode.CAMPAIGN_ARCHIVED, ARCHIVED_MESSAGE)
        # 6. State: a repeat changes nothing and answers the same 204.
        if repeat_before_the_lock:
            return False
        # 7. State: the seat is not open.
        standing = stores.offers.open_for_seat(unit, campaign_id, participant_id)
        if seat.user_id is not None or (standing is not None and standing.is_live(now)):
            raise conflict(ErrorCode.SEAT_NOT_OPEN, SEAT_NOT_OPEN_MESSAGE)
        # 8. This seat's own stale offer, marked under the seat it hangs off.
        stores.offers.expire_stale(unit, campaign_id, participant_id, now=now)
        # 9. The per-owner throttle, across campaigns: the last lock before
        #    the insert (RQ-3 as L-14 extends it).
        assert AdvisoryLock.SEAT_OFFERS, "the throttle takes no lock in this broken commit"
        counted = stores.offers.count_recent(unit, owner_id, now=now)
        if counted.count >= THROTTLE_LIMIT:
            wait = throttle_wait_s(counted, now)
            raise refusal(
                429, ErrorCode.THROTTLED_USER, THROTTLED_MESSAGE, retryable=True, retry_after_s=wait,
                headers={"Retry-After": str(wait)},
            )
        # 10. The write. No revision: nobody gains anything until an acceptance.
        stores.offers.create(unit, campaign_id, participant_id, offered_by=owner_id, address=address, now=now)
        _audit(
            stores, unit, campaign_id=campaign_id, owner_id=owner_id, action=AuditAction.SEAT_OFFERED,
            object_kind=ObjectKind.PARTICIPANT, object_ref=participant_id, detail={"participant_id": participant_id},
            revision=None, now=now,
        )
        return True


def confirm_seat(
    db: TransactionalDatabase,
    stores: CampaignStores,
    *,
    campaign_id: str,
    participant_id: str,
    owner_id: int,
    now: datetime,
) -> tuple[Participant, SeatOffer | None]:
    """The GM's confirmation (D-12, SEC-50(5)), a locked widening."""
    with db.transaction() as unit:
        if stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
            not_found()
        unit.lock_campaign(campaign_id, shared=False)
        answer: str | None = None
        changed = False
        try:
            changed = stores.participants.confirm(unit, campaign_id, participant_id, now=now)
        except _SeatUnavailable:
            answer = "missing"
        except SeatNotAccepted:
            answer = "not_accepted"
        if answer == "missing":
            not_found()
        if answer == "not_accepted":
            raise conflict(ErrorCode.SEAT_NOT_ACCEPTED, SEAT_NOT_ACCEPTED_MESSAGE)
        if changed:
            revision = unit.advance_authz_revision(campaign_id)
            _audit(
                stores, unit, campaign_id=campaign_id, owner_id=owner_id, action=AuditAction.SEAT_CONFIRMED,
                object_kind=ObjectKind.PARTICIPANT, object_ref=participant_id,
                detail={"participant_id": participant_id}, revision=revision, now=now,
            )
        seat = stores.participants.get(unit, participant_id)
        assert seat is not None
        latest = stores.offers.latest_for_seats(unit, campaign_id, [participant_id]).get(participant_id)
        return seat, latest


def remove_seat(
    db: TransactionalDatabase,
    stores: CampaignStores,
    jobs: JobQueue,
    *,
    campaign_id: str,
    participant_id: str,
    owner_id: int,
    now: datetime,
) -> int | None:
    """Remove is a revocation (RQ-5): effective at once, in a transaction that
    NEVER calls `lock_campaign`, in RQ-3's order — the seat's row, the live
    session's row (narrowed), the seat's open offer (withdrawn), the outbox
    last. Returns the reconciliation job's id, or None when the seat was
    already removed, which changes nothing and enqueues nothing."""
    with db.transaction() as unit:
        if stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
            not_found()
        if isinstance(unit, PgTransaction):
            unit.lock_campaign(campaign_id, shared=False)
        seat = stores.participants.hold(unit, participant_id, campaign_id=campaign_id)
        if seat is None:
            not_found()
        if not seat.is_active:
            return None
        live = stores.sessions.live_session_for_campaign(unit, campaign_id)
        if live is not None:
            stores.sessions.narrow(unit, campaign_id, live.id)
        if not isinstance(unit, PgTransaction):
            stores.participants.remove(unit, campaign_id, participant_id, now=now)
        stores.offers.withdraw_open(unit, campaign_id, participant_id, now=now)
        _audit(
            stores, unit, campaign_id=campaign_id, owner_id=owner_id, action=AuditAction.PARTICIPANT_REMOVED,
            object_kind=ObjectKind.PARTICIPANT, object_ref=participant_id, detail={"participant_id": participant_id},
            revision=None, now=now, reason_code="gm_removed",
        )
        return enqueue_reconciliation(unit, jobs, campaign_id)


# ── The router ───────────────────────────────────────────────────────────────


def _owner_email(request: Request) -> str | None:
    """The owner's own login address, from the account `require_session`
    re-read for this request — never a query of its own (L-6 step 4)."""
    user = getattr(request.state, "auth_user", None)
    email = getattr(user, "email", None)
    return email if isinstance(email, str) else None


def build_router(
    gm: SessionDependency,
    database: Callable[[], TransactionalDatabase | None],
    reauthenticate: Callable[..., PasswordCheck],
    jobs: Callable[[], JobQueue | None],
    driver: Callable[[], JobDriver | None],
) -> APIRouter:
    """The nine routes on a `workbench_router`, given the app's GM gate, its
    database getter, the re-authentication callable (which runs before any
    transaction, so argon2 never runs under a lock) and the job queue and
    driver getters."""
    router = workbench_router(gm)

    def _database(db: TransactionalDatabase | None) -> TransactionalDatabase:
        if db is None:
            raise unavailable()
        return db

    @router.get("/campaigns", response_model=CampaignPage)
    def list_campaigns(
        user: SessionData = Depends(gm),
        limit: str | None = None,
        cursor: str | None = None,
        include_archived: str | None = None,
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> CampaignPage:
        query = parse_page_query(limit, cursor, include_archived, "include_archived")
        live_db = _database(db)

        def work() -> CampaignPage:
            with live_db.transaction() as unit:
                page = stores.campaigns.page_for_owner(
                    unit, user.user_id, include_archived=query.flag, cursor=query.cursor, limit=query.limit
                )
            return CampaignPage(
                schema_version=WIRE_VERSION,
                items=[campaign_to_wire(row) for row in page.items],
                next_cursor=page.next_cursor,
            )

        try:
            return guarded(work)
        except InvalidCursor:
            pass
        # Outside the handler, so the refusal chains nothing.
        raise invalid("cursor", "query")

    @router.post("/campaigns", response_model=Campaign, status_code=201)
    def create_campaign(
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Campaign:
        """A new campaign, the caller its GM (D-5). Duplicate names are allowed,
        and a retried create makes a second campaign, which archive recovers."""
        request = parse_body(CampaignCreateRequest, raw)
        live_db = _database(db)

        def work() -> StoredCampaign:
            with live_db.transaction() as unit:
                return stores.campaigns.create(unit, owner_id=user.user_id, name=request.name, now=now)

        return campaign_to_wire(guarded(work))

    @router.get("/campaigns/{campaign_id}", response_model=Campaign)
    def read_campaign(
        campaign_id: str,
        user: SessionData = Depends(gm),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> Campaign:
        live_db = _database(db)
        if not readable(ident.CAMPAIGN, campaign_id):
            not_found()

        def work() -> StoredCampaign | None:
            with live_db.transaction() as unit:
                return stores.campaigns.get(unit, campaign_id, owner_id=user.user_id)

        found = guarded(work)
        if found is None:
            not_found()
        return campaign_to_wire(found)

    @router.patch("/campaigns/{campaign_id}", response_model=Campaign)
    def patch_campaign(
        campaign_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Campaign:
        """Rename, archive and restore. With `archived`, a `name` is applied in
        the transaction that changes the fact, so a `503` applies neither."""
        patch = parse_body(CampaignPatchRequest, raw)
        live_db = _database(db)
        if not readable(ident.CAMPAIGN, campaign_id):
            not_found()
        owner = user.user_id
        if patch.archived is True:
            guarded(lambda: archive_step_one(live_db, stores, campaign_id=campaign_id, owner_id=owner))
            final = guarded(
                lambda: archive_step_two(
                    live_db, stores, campaign_id=campaign_id, owner_id=owner, now=now, name=patch.name
                ),
                busy_message=NOT_APPLIED_MESSAGE,
            )
        elif patch.archived is False:
            final = guarded(
                lambda: restore(live_db, stores, campaign_id=campaign_id, owner_id=owner, now=now, name=patch.name)
            )
        else:
            name = patch.name
            assert name is not None, "the contract refuses an empty patch"
            final = guarded(
                lambda: rename(live_db, stores, campaign_id=campaign_id, owner_id=owner, name=name, now=now)
            )
        return campaign_to_wire(final)

    @router.get("/campaigns/{campaign_id}/participants", response_model=SeatPage)
    def list_seats(
        campaign_id: str,
        user: SessionData = Depends(gm),
        limit: str | None = None,
        cursor: str | None = None,
        include_removed: str | None = None,
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> SeatPage:
        query = parse_page_query(limit, cursor, include_removed, "include_removed")
        live_db = _database(db)
        if not readable(ident.CAMPAIGN, campaign_id):
            not_found()

        def work() -> SeatPage | None:
            with live_db.transaction() as unit:
                if stores.campaigns.get(unit, campaign_id, owner_id=user.user_id) is None:
                    return None
                page = stores.participants.page_for_campaign(
                    unit, campaign_id, include_removed=query.flag, cursor=query.cursor, limit=query.limit
                )
                latest = stores.offers.latest_for_seats(unit, campaign_id, [seat.id for seat in page.items])
            return SeatPage(
                schema_version=WIRE_VERSION,
                items=[seat_to_wire(seat, latest.get(seat.id), now) for seat in page.items],
                next_cursor=page.next_cursor,
            )

        answered: SeatPage | None = None
        try:
            answered = guarded(work)
        except InvalidCursor:
            refused = True
        else:
            refused = False
        if refused:
            raise invalid("cursor", "query")
        if answered is None:
            not_found()
        return answered

    @router.post("/campaigns/{campaign_id}/participants", response_model=Seat, status_code=201)
    def create_seat(
        campaign_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Seat:
        """An open seat. A retried add of the same alias is `409 alias_taken`."""
        request = parse_body(SeatCreateRequest, raw)
        try:
            alias = check_alias(request.alias)
        except ValueError:
            alias = ""
        if not alias:
            raise invalid("alias")
        live_db = _database(db)
        if not readable(ident.CAMPAIGN, campaign_id):
            not_found()
        seat = guarded(
            lambda: add_seat(live_db, stores, campaign_id=campaign_id, owner_id=user.user_id, alias=alias, now=now)
        )
        return seat_to_wire(seat, None, now)

    @router.post("/campaigns/{campaign_id}/participants/{participant_id}/offer", status_code=204)
    def offer(
        campaign_id: str,
        participant_id: str,
        request: Request,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """`204`, byte-identical, whatever the address holds (T-24)."""
        body = parse_body(SeatOfferRequest, raw)
        live_db = _database(db)
        if not (readable(ident.CAMPAIGN, campaign_id) and readable(ident.PARTICIPANT, participant_id)):
            not_found()
        email = _owner_email(request)
        guarded(
            lambda: offer_seat(
                live_db, stores, campaign_id=campaign_id, participant_id=participant_id, owner_id=user.user_id,
                owner_email=email, address=body.email, now=now,
            )
        )
        return Response(status_code=204)

    @router.post("/campaigns/{campaign_id}/participants/{participant_id}/confirm", response_model=Seat)
    def confirm(
        campaign_id: str,
        participant_id: str,
        user: SessionData = Depends(gm),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Seat:
        live_db = _database(db)
        if not (readable(ident.CAMPAIGN, campaign_id) and readable(ident.PARTICIPANT, participant_id)):
            not_found()
        seat, latest = guarded(
            lambda: confirm_seat(
                live_db, stores, campaign_id=campaign_id, participant_id=participant_id, owner_id=user.user_id,
                now=now,
            )
        )
        return seat_to_wire(seat, latest, now)

    @router.post("/campaigns/{campaign_id}/participants/{participant_id}/remove", status_code=204)
    def remove(
        campaign_id: str,
        participant_id: str,
        tasks: BackgroundTasks,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        queue: JobQueue | None = Depends(jobs),
        runner: JobDriver | None = Depends(driver),
        check_password: PasswordCheck = Depends(reauthenticate),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """SEC-40: every Remove asks for the password, BEFORE any transaction
        opens — argon2 never runs while a lock is held. Then RQ-5's first step;
        the reconciliation runs after the response, which never waits for it."""
        body = parse_body(SeatRemoveRequest, raw)
        live_db = _database(db)
        if queue is None:
            raise unavailable()
        check_password(body.password.get_secret_value())
        if not (readable(ident.CAMPAIGN, campaign_id) and readable(ident.PARTICIPANT, participant_id)):
            not_found()
        job_id = guarded(
            lambda: remove_seat(
                live_db, stores, queue, campaign_id=campaign_id, participant_id=participant_id,
                owner_id=user.user_id, now=now,
            )
        )
        if job_id is not None:
            run_after_response(tasks, runner, job_id)
        return Response(status_code=204)

    return router


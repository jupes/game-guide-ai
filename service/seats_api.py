"""An account's own side of seats: the offers made to it, and the seats it holds
(bead 1kg.2.2).

Four Workbench routes on `account_router` (`service/workbench_api.py`), which is
the scaffolding without the `dm` gate: a player must reach them, and so must a
GM who holds a seat at another GM's table. They live under `/seats`, not
`/campaigns`, because `GET /campaigns/{campaign_id}` on the GM router would
otherwise answer a player's `GET /campaigns/offers` with the role refusal.

| Route | Answers |
| --- | --- |
| `GET /seats` | the caller's accepted, live seats, newest acceptance first |
| `GET /seats/offers` | the offers made to the caller's VERIFIED address |
| `POST /seats/offers/{offer_id}/accept` | the seat it now holds, unconfirmed |
| `POST /seats/offers/{offer_id}/decline` | `204`; `block` refuses that GM for good |

**The verified address fails closed** (L-8, owner question OQ-1). Offers are
shown to, and may be accepted by, only a Verified account whose verified
address is the one the offer names (SEC-50(4)). `verified_address_of` reads
`service.auth_store.verified_address`, which answers None for every account
until `agent-forge-harness-yje.2.1` ships; with no address, the offer routes
answer without opening a transaction — an empty list, or the one 404 — because
an account with no verified address has no offers by definition. Tests override
the dependency; production has no override, flag or setting that turns it on.

**One 404 for every refusal** (T-24's invitee half): missing, someone else's,
expired, withdrawn, archived, blocked, already seated, and the caller being the
campaign's owner are one answer, identical in status, body and headers. The
order (L-18): origin (403) → authentication (the one 401) → body or query
validation (422) → [no verified address] → the database (503) → the offer's
unlocked pre-read (404) → the campaign lock → the re-check under the offer row
(404) → the write.

**Binding happens here and only here** (L-6): the accept composes the
participant store's `offer(user_id)` and `accept(user_id)` in one transaction,
so a seat is bound to exactly the account that accepted, under a key equal to
its own verified address.

Nothing on this side names an address, a campaign owner or a participant id
(SEC-43, SEC-50(4)), and no handler logs anything but an exception's type.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response

from . import campaign_identity as ident
from .audit_log import ActorKind, AuditAction, Decision, ObjectKind
from .auth_store import User, verified_address
from .campaign_store import InvalidCursor
from .campaign_store import SeatUnavailable as _SeatUnavailable
from .campaigns_api import (
    WIRE_VERSION,
    CampaignStores,
    get_campaign_stores,
    get_clock,
    guarded,
    invalid,
    parse_body,
    parse_page_query,
    readable,
    unavailable,
)
from .conversations_api import read_body
from .db import PgTransaction, TransactionalDatabase, UnitOfWork
from .participant_store import HeldSeat
from .seat_offer_store import ACCEPTED, DECLINED, InviteeOffer, address_key
from .session import SessionData
from .workbench_api import SessionDependency, account_router, not_found
from .workbench_contracts import (
    PlayerSeat,
    PlayerSeatPage,
    SeatDeclineRequest,
    SeatOffer,
    SeatOfferPage,
)


def verified_address_of(request: Request) -> str | None:
    """The caller's verified address, read from the account `require_session`
    re-read for this request — None when there is none, which on this build is
    always (L-8). Tests override this dependency."""
    user = getattr(request.state, "auth_user", None)
    return verified_address(user) if isinstance(user, User) else None


def _utc(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.astimezone(UTC)


def player_seat(held: HeldSeat) -> PlayerSeat:
    """One of the caller's seats: the campaign's id (the table address will
    name it, SEC-43), never the participant's."""
    return PlayerSeat.model_validate(
        {
            "schema_version": WIRE_VERSION,
            "campaign_id": held.seat.campaign_id,
            "campaign_name": held.campaign_name,
            "alias": held.seat.alias,
            "accepted_at": _utc(held.seat.accepted_at),
            "confirmed": held.seat.confirmed_at is not None,
        }
    )


def seat_offer(found: InviteeOffer) -> SeatOffer:
    """What the invitee sees: the GM's own words and the dates, nothing else."""
    return SeatOffer.model_validate(
        {
            "schema_version": WIRE_VERSION,
            "offer_id": found.offer.id,
            "campaign_name": found.campaign_name,
            "alias": found.alias,
            "offered_at": _utc(found.offer.created_at),
            "expires_at": _utc(found.offer.expires_at),
        }
    )


def _record(
    stores: CampaignStores,
    unit: UnitOfWork,
    *,
    campaign_id: str,
    participant_id: str,
    action: AuditAction,
    detail: dict[str, str | int | bool],
    revision: int | None,
    now: datetime,
) -> None:
    """The invitee's decision, recorded by the seat it was offered — never by
    its user id, as the seat's acceptance is (fma)."""
    stores.audit.append(
        unit,
        campaign_id=campaign_id,
        actor_kind=ActorKind.PARTICIPANT,
        action=action,
        object_kind=ObjectKind.PARTICIPANT,
        decision=Decision.ALLOWED,
        actor_ref=participant_id,
        object_ref=participant_id,
        authz_revision=revision,
        detail=detail,
        now=now,
    )


def accept_offer(
    db: TransactionalDatabase,
    stores: CampaignStores,
    *,
    offer_id: str,
    user_id: int,
    address: str,
    now: datetime,
) -> HeldSeat:
    """L-9's seven steps in one transaction. A repeat accept by the account
    that accepted answers the same seat and changes nothing; every other
    refusal is the one 404."""
    key = address_key(address)
    with db.transaction() as unit:
        found = stores.offers.find_for_invitee(unit, offer_id, key, user_id, now=now)
        if found is None:
            repeat = stores.offers.repeat_accept(unit, offer_id, key, user_id)
            if repeat is None:
                not_found()
            return repeat
        campaign_id, participant_id = found.offer.campaign_id, found.offer.participant_id
        if not isinstance(unit, PgTransaction):
            unit.lock_campaign(campaign_id, shared=False)
        if stores.participants.hold(unit, participant_id, campaign_id=campaign_id) is None:
            not_found()
        held = stores.offers.hold_for_invitee(unit, offer_id, campaign_id, participant_id, key, user_id, now=now)
        if held is None and isinstance(unit, PgTransaction):
            held = found
        if held is None:
            repeat = stores.offers.repeat_accept(unit, offer_id, key, user_id)
            if repeat is None:
                not_found()
            return repeat
        taken = False
        try:
            stores.participants.offer(unit, campaign_id, participant_id, user_id=user_id)
            stores.participants.accept(unit, campaign_id, participant_id, user_id=user_id, now=now)
        except _SeatUnavailable:
            taken = True
        if taken:  # never a 409 that says "seat taken" (fma)
            not_found()
        stores.offers.close(unit, offer_id, outcome=ACCEPTED, now=now)
        revision = None if isinstance(unit, PgTransaction) else unit.advance_authz_revision(campaign_id)
        _record(
            stores, unit, campaign_id=campaign_id, participant_id=participant_id, action=AuditAction.SEAT_ACCEPTED,
            detail={"participant_id": participant_id}, revision=revision, now=now,
        )
        seat = stores.participants.get(unit, participant_id)
        assert seat is not None
        return HeldSeat(seat, held.campaign_name)


def decline_offer(
    db: TransactionalDatabase,
    stores: CampaignStores,
    *,
    offer_id: str,
    user_id: int,
    address: str,
    block: bool,
    now: datetime,
) -> None:
    """L-10: the same pre-read, lock order and re-check as accept; the outcome
    `declined`, and a block of the campaign's owner when asked. A repeat
    decline applies a block it now asks for and answers the same `204`."""
    key = address_key(address)
    with db.transaction() as unit:
        found = stores.offers.find_for_invitee(unit, offer_id, key, user_id, now=now)
        if found is None:
            _repeat_decline(stores, unit, offer_id, key, user_id, block, now)
            return
        campaign_id, participant_id = found.offer.campaign_id, found.offer.participant_id
        unit.lock_campaign(campaign_id, shared=False)
        if stores.participants.hold(unit, participant_id, campaign_id=campaign_id) is None:
            not_found()
        held = stores.offers.hold_for_invitee(unit, offer_id, campaign_id, participant_id, key, user_id, now=now)
        if held is None and isinstance(unit, PgTransaction):
            held = found
        if held is None:
            _repeat_decline(stores, unit, offer_id, key, user_id, block, now)
            return
        stores.offers.close(unit, offer_id, outcome=DECLINED, now=now)
        if block:
            stores.offers.block(unit, blocker_user_id=user_id, blocked_owner_id=held.offer.offered_by, now=now)
        _record(
            stores, unit, campaign_id=campaign_id, participant_id=participant_id, action=AuditAction.SEAT_DECLINED,
            detail={"participant_id": participant_id, "blocked": block}, revision=None, now=now,
        )


def _repeat_decline(
    stores: CampaignStores, unit: UnitOfWork, offer_id: str, key: str, user_id: int, block: bool, now: datetime
) -> None:
    declined = stores.offers.repeat_decline(unit, offer_id, key)
    if declined is None:
        not_found()
    if block and declined.offered_by != user_id:
        stores.offers.block(unit, blocker_user_id=user_id, blocked_owner_id=declined.offered_by, now=now)


def build_router(
    session: SessionDependency,
    database: Callable[[], TransactionalDatabase | None],
) -> APIRouter:
    """The four routes on `account_router`, given the app's session dependency
    (`require_session`, with no role) and its database getter."""
    router = account_router(session)

    def _database(db: TransactionalDatabase | None) -> TransactionalDatabase:
        if db is None:
            raise unavailable()
        return db

    @router.get("/seats", response_model=PlayerSeatPage)
    def list_seats(
        user: SessionData = Depends(session),
        limit: str | None = None,
        cursor: str | None = None,
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> PlayerSeatPage:
        """The caller's accepted, live seats in campaigns that are not archived.
        It does not consult the verified address: a seat once accepted is the
        caller's, whatever became of the offer."""
        query = parse_page_query(limit, cursor)
        live_db = _database(db)

        def work() -> PlayerSeatPage:
            with live_db.transaction() as unit:
                page = stores.participants.seat_page_for_user(
                    unit, user.user_id, cursor=query.cursor, limit=query.limit
                )
            return PlayerSeatPage(
                schema_version=WIRE_VERSION, items=[player_seat(h) for h in page.items], next_cursor=page.next_cursor
            )

        try:
            return guarded(work)
        except InvalidCursor:
            pass
        raise invalid("cursor", "query")

    @router.get("/seats/offers", response_model=SeatOfferPage)
    def list_offers(
        user: SessionData = Depends(session),
        limit: str | None = None,
        cursor: str | None = None,
        address: str | None = Depends(verified_address_of),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> SeatOfferPage:
        query = parse_page_query(limit, cursor)
        if address is None:
            return SeatOfferPage(schema_version=WIRE_VERSION, items=[], next_cursor=None)
        live_db = _database(db)
        key = address_key(address)

        def work() -> SeatOfferPage:
            with live_db.transaction() as unit:
                page = stores.offers.invitee_page(
                    unit, key, user.user_id, now=now, cursor=query.cursor, limit=query.limit
                )
            return SeatOfferPage(
                schema_version=WIRE_VERSION, items=[seat_offer(f) for f in page.items], next_cursor=page.next_cursor
            )

        try:
            return guarded(work)
        except InvalidCursor:
            pass
        raise invalid("cursor", "query")

    @router.post("/seats/offers/{offer_id}/accept", response_model=PlayerSeat)
    def accept(
        offer_id: str,
        user: SessionData = Depends(session),
        address: str | None = Depends(verified_address_of),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> PlayerSeat:
        """Take the seat. It is `confirmed: false` until the GM confirms who
        accepted (D-12); until then it gets the table slot only."""
        if address is None or not readable(ident.SEAT_OFFER, offer_id):
            not_found()
        live_db = _database(db)
        verified = address
        held = guarded(
            lambda: accept_offer(live_db, stores, offer_id=offer_id, user_id=user.user_id, address=verified, now=now)
        )
        return player_seat(held)

    @router.post("/seats/offers/{offer_id}/decline", status_code=204)
    def decline(
        offer_id: str,
        user: SessionData = Depends(session),
        raw: bytes = Depends(read_body),
        address: str | None = Depends(verified_address_of),
        stores: CampaignStores = Depends(get_campaign_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        body = parse_body(SeatDeclineRequest, raw)
        if address is None or not readable(ident.SEAT_OFFER, offer_id):
            not_found()
        live_db = _database(db)
        verified = address
        guarded(
            lambda: decline_offer(
                live_db, stores, offer_id=offer_id, user_id=user.user_id, address=verified, block=body.block, now=now
            )
        )
        return Response(status_code=204)

    return router

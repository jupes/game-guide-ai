"""Seat offers and blocks: a GM offers a seat to an ADDRESS (bead 1kg.2.2, D-12).

Owner decision D-12 and threat model SEC-50 shape everything here. The GM names
an address, never an account; the offer is shown to, and may be accepted by,
only a Verified account whose verified address is the one the offer names; and
the answer the GM gets never depends on whether any account holds the address.

**An offer is a row of `campaign.seat_offers`** (migration 0012). It names a
seat and an address. **Making one reads and writes no account row and reads no
`seat_blocks` row** — nothing in `create`, `is_repeat`, `open_for_seat`,
`expire_stale` or `count_recent` names either table, and
`service/tests/test_campaign_schema_sql.py` fails if one does. That is the
structural guarantee behind SEC-50(2)'s single answer and comparable timing.
An offer binds to an account only in the transaction that accepts it
(`service/participant_store.py`).

**The address is personal data about a third party** (SEC-20). It is stored
here and served only to the campaign's owner. It never reaches a log line, an
exception message, an audit row, a metric label, a URL or a job payload, and
`SeatOffer.__repr__` hides it. Any constraint violation on `seat_offers` quotes
the row in its DETAIL, so the PostgreSQL writer catches one inside a savepoint
and raises the fixed `SeatUnavailable` OUTSIDE the handler, as `offer` does.

**The key** is the address with ASCII A-Z folded to a-z and nothing else
(`address_key`). The account table's uniqueness is `lower(email)`, so a key that
folds less than that can never merge two addresses the table keeps apart, while
one that folded more (`casefold`: `ß` → `ss`) could show one account's offer to
another. A non-ASCII case variant therefore fails closed: nobody is misdelivered.

**Nothing reads the clock but the caller.** Expiry is judged at read time and
marked lazily, on one seat at a time, under that seat's row lock
(`expire_stale`). There is no unique index on `(campaign_id, address_key)`: the
exclusive campaign lock, which every writer that opens an offer holds, keeps one
live offer per address in a campaign, and the writers that close offers only
ever close them.

A `Protocol`, a PostgreSQL implementation and an in-memory twin, the shape the
other campaign stores share. The twin keeps every rule the suite asserts — the
CHECKs (through `SeatOffer.__post_init__`), the one-open-offer-per-seat index and
both composite relations — because a rule the fake is not obliged to keep is one
it will drift on. It has no users table, so a block's accounts are not checked.
"""

from __future__ import annotations

import string
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final, Protocol

import psycopg

from . import campaign_identity as ident
from .campaign_store import (
    PAGE_MAX,
    Campaign,
    Page,
    SeatUnavailable,
    Staging,
    aware,
    check_limit,
    decode_cursor,
    fake,
    now_or,
    page_of,
    pg,
    shared_rows,
)
from .db import InMemoryDatabase, UnitOfWork
from .participant_store import _P_COLUMNS, HeldSeat, Participant, _participant
from .workbench_contracts import (
    EMAIL_MAX_CHARS,
    EMAIL_MIN_CHARS,
    SeatStatus,
    check_stored_text,
    is_email_shaped,
)

#: How long an offer stays open (SEC-50, *suggested*).
OFFER_LIFETIME: Final = timedelta(days=14)
#: The per-owner throttle: offers written in the window, whatever became of
#: them, at most this many (SEC-50(3), *suggested*).
THROTTLE_WINDOW: Final = timedelta(hours=24)
THROTTLE_LIMIT: Final = 30

ACCEPTED: Final = "accepted"
DECLINED: Final = "declined"
EXPIRED: Final = "expired"
WITHDRAWN: Final = "withdrawn"
OUTCOMES: Final = frozenset({ACCEPTED, DECLINED, EXPIRED, WITHDRAWN})

_ASCII_FOLD: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def address_key(address: str) -> str:
    """What an offer is compared by: ASCII A-Z folded to a-z, and nothing else
    (L-7). One function for the offer's key, the owner's-own check and the
    reader's key."""
    return address.translate(_ASCII_FOLD)


def check_address(address: str) -> str:
    """The bounds 0012 carries and the wire's shape, applied before any
    statement. The route hands over an address the contract has already read
    (trimmed); this is the store's own defence. Each refusal names the rule,
    never the address."""
    try:
        check_stored_text(address)
    except ValueError:
        refused = True
    else:
        refused = not (EMAIL_MIN_CHARS <= len(address) <= EMAIL_MAX_CHARS and is_email_shaped(address))
    if refused:  # outside the handler: nothing is chained
        raise ValueError(f"an address is {EMAIL_MIN_CHARS} to {EMAIL_MAX_CHARS} characters of plain text with an @")
    return address


@dataclass(frozen=True)
class SeatOffer:
    """One offer. The offerer, the address and its key are hidden from
    `repr()`: an account id and a third party's address must not reach a log
    line, and a traceback is a log line. The record refuses what 0012's CHECKs
    refuse, so the twin cannot hold a row PostgreSQL would not."""

    id: str
    campaign_id: str
    participant_id: str
    offered_by: int = field(repr=False)
    address: str = field(repr=False)
    address_key: str = field(repr=False)
    created_at: datetime
    expires_at: datetime
    outcome: str | None = None
    answered_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.expires_at <= self.created_at:
            raise ValueError("an offer expires after it is made")
        if (self.outcome is None) != (self.answered_at is None):
            raise ValueError("an offer is answered exactly when it has an outcome")
        if self.outcome is not None and self.outcome not in OUTCOMES:
            raise ValueError("an offer's outcome is one of the four")
        if address_key(self.address_key) != self.address_key or not (
            EMAIL_MIN_CHARS <= len(self.address_key) <= EMAIL_MAX_CHARS
        ):
            raise ValueError("an offer's key is its folded address")

    def is_live(self, now: datetime) -> bool:
        """Open and not yet expired — what an invitee can act on."""
        return self.outcome is None and self.expires_at > now


@dataclass(frozen=True)
class InviteeOffer:
    """An offer as its invitee sees it: with the campaign's name and the seat's
    alias, which the GM wrote and chose to send (SEC-50(4)). Both are hidden
    from `repr()`."""

    offer: SeatOffer
    campaign_name: str = field(repr=False)
    alias: str = field(repr=False)


@dataclass(frozen=True)
class ThrottleCount:
    """How many offers an owner wrote in the window, and when the oldest of
    them was written — the moment the count next drops."""

    count: int
    oldest: datetime | None


def seat_status(seat: Participant, latest: SeatOffer | None, now: datetime) -> SeatStatus:
    """The one function both worlds derive a seat's status with (L-5), from the
    seat, its latest offer and the clock."""
    if seat.removed_at is not None:
        return SeatStatus.REMOVED
    if seat.confirmed_at is not None:
        return SeatStatus.CONFIRMED
    if seat.accepted_at is not None:
        return SeatStatus.AWAITING_CONFIRMATION
    if latest is None:
        return SeatStatus.OPEN
    if latest.is_live(now):
        return SeatStatus.OFFERED
    if latest.outcome in (None, DECLINED, EXPIRED):
        return SeatStatus.NOT_ACCEPTED
    # Withdrawn sits only on a removed seat and accepted only on an accepted
    # one, both answered above; a seat reaching here has no offer standing.
    return SeatStatus.OPEN


def throttle_wait_s(count: ThrottleCount, now: datetime) -> int:
    """Whole seconds, rounded up, until the oldest counted offer ages out — the
    `retry_after_s` of a throttled offer. Never less than one."""
    if count.oldest is None:
        return 1
    remaining = (count.oldest + THROTTLE_WINDOW - now).total_seconds()
    return max(1, min(int(-(-remaining // 1)), int(THROTTLE_WINDOW.total_seconds())))


class SeatOfferStore(Protocol):
    """Offers and blocks. Every write takes the unit of work first, so a route
    composes it with the other campaign stores and the audit log in one
    transaction."""

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        *,
        offered_by: int,
        address: str,
        now: datetime | None = None,
    ) -> SeatOffer:
        """Record an offer of that seat to that address, open for
        `OFFER_LIFETIME`. Raises `SeatUnavailable` — fixed, naming nothing — for
        a seat that is not that campaign's, an offerer who is not its owner, or
        a seat that already has an open offer."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, offer_id: str) -> SeatOffer | None:
        ...  # pragma: no cover - structural type

    def latest_for_seats(
        self, unit: UnitOfWork, campaign_id: str, participant_ids: Sequence[str]
    ) -> dict[str, SeatOffer]:
        """Each seat's latest offer (`created_at DESC, id DESC`), for its status."""
        ...  # pragma: no cover - structural type

    def is_repeat(self, unit: UnitOfWork, campaign_id: str, key: str, *, now: datetime | None = None) -> bool:
        """Whether an offer of that key would repeat one this campaign already
        has: a live offer on ANY of its seats, or an accepted, live seat whose
        accepted offer has that key (L-6 step 6)."""
        ...  # pragma: no cover - structural type

    def open_for_seat(self, unit: UnitOfWork, campaign_id: str, participant_id: str) -> SeatOffer | None:
        """The seat's open offer, expired or not."""
        ...  # pragma: no cover - structural type

    def expire_stale(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        """Mark THIS seat's open offer `expired` if its time has passed (L-6
        step 8). The caller holds the seat's row."""
        ...  # pragma: no cover - structural type

    def count_recent(self, unit: UnitOfWork, owner_id: int, *, now: datetime | None = None) -> ThrottleCount:
        """The owner's offers written in the last `THROTTLE_WINDOW`, whatever
        their outcome (SEC-50(3)). The caller holds `AdvisoryLock.SEAT_OFFERS`."""
        ...  # pragma: no cover - structural type

    def invitee_page(
        self,
        unit: UnitOfWork,
        key: str,
        user_id: int,
        *,
        now: datetime | None = None,
        cursor: str | None = None,
        limit: int = PAGE_MAX,
    ) -> Page[InviteeOffer]:
        """The offers that account may see, newest first, in ONE query (L-9):
        its key, open and unexpired, the seat not removed, the campaign not
        archived, not the account's own campaign, not an owner it blocked, and
        not a campaign where it already holds a live seat."""
        ...  # pragma: no cover - structural type

    def find_for_invitee(
        self, unit: UnitOfWork, offer_id: str, key: str, user_id: int, *, now: datetime | None = None
    ) -> InviteeOffer | None:
        """One offer under every predicate of `invitee_page`, read WITHOUT a
        lock, so that the campaign lock can still be the transaction's first."""
        ...  # pragma: no cover - structural type

    def hold_for_invitee(
        self,
        unit: UnitOfWork,
        offer_id: str,
        campaign_id: str,
        participant_id: str,
        key: str,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> InviteeOffer | None:
        """The same offer, taken `FOR NO KEY UPDATE`, naming its seat and its
        campaign, with every predicate checked again under the lock."""
        ...  # pragma: no cover - structural type

    def repeat_accept(self, unit: UnitOfWork, offer_id: str, key: str, user_id: int) -> HeldSeat | None:
        """The seat this account accepted with this offer, if it still holds it
        in a campaign that is not archived — a repeat accept's answer."""
        ...  # pragma: no cover - structural type

    def repeat_decline(self, unit: UnitOfWork, offer_id: str, key: str) -> SeatOffer | None:
        """The offer, if this key already declined it — a repeat decline's."""
        ...  # pragma: no cover - structural type

    def close(self, unit: UnitOfWork, offer_id: str, *, outcome: str, now: datetime | None = None) -> bool:
        """Answer an open offer; False if it was not open."""
        ...  # pragma: no cover - structural type

    def withdraw_open(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        """Close the seat's open offer on Remove: `withdrawn`, or `expired` if
        its time had already passed (A-27, `g3x`)."""
        ...  # pragma: no cover - structural type

    def block(
        self, unit: UnitOfWork, *, blocker_user_id: int, blocked_owner_id: int, now: datetime | None = None
    ) -> bool:
        """The invitee refuses every later offer from that owner; False if it
        already had."""
        ...  # pragma: no cover - structural type

    def is_blocked(self, unit: UnitOfWork, blocker_user_id: int, blocked_owner_id: int) -> bool:
        ...  # pragma: no cover - structural type


_O_COLUMNS = (
    "id, campaign_id, participant_id, offered_by, address, address_key, created_at, expires_at, outcome, answered_at"
)


def _offer(row: tuple) -> SeatOffer:
    return SeatOffer(row[0], row[1], row[2], int(row[3]), row[4], row[5], row[6], row[7], row[8], row[9])


def _newest_key(item: InviteeOffer) -> tuple[datetime, str]:
    return item.offer.created_at, item.offer.id


def _check_key(key: str) -> str:
    if not isinstance(key, str) or address_key(key) != key:
        raise ValueError("an offer is read by its folded key")
    return key


def _qualified(prefix: str, columns: str) -> str:
    return ", ".join(f"{prefix}.{column.strip()}" for column in columns.split(","))


#: Every predicate an invitee's offer must satisfy (L-9), for the one query
#: that reads it — listed, found or held. `o` is the offer, `p` its seat.
_INVITEE_PREDICATES = (
    "o.address_key = %(key)s AND o.outcome IS NULL AND o.expires_at > %(now)s "
    "AND p.removed_at IS NULL AND c.archived_at IS NULL AND o.offered_by <> %(user)s "
    "AND NOT EXISTS (SELECT 1 FROM campaign.seat_blocks b "
    "WHERE b.blocker_user_id = %(user)s AND b.blocked_owner_id = o.offered_by) "
    "AND NOT EXISTS (SELECT 1 FROM campaign.participants q "
    "WHERE q.campaign_id = o.campaign_id AND q.user_id = %(user)s AND q.removed_at IS NULL)"
)
_INVITEE_FROM = (
    "FROM campaign.seat_offers o "
    "JOIN campaign.participants p ON p.id = o.participant_id AND p.campaign_id = o.campaign_id "
    "JOIN campaign.campaigns c ON c.id = o.campaign_id "
)


class PostgresSeatOfferStore:
    """`campaign.seat_offers` and `campaign.seat_blocks` (migration 0012)."""

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        *,
        offered_by: int,
        address: str,
        now: datetime | None = None,
    ) -> SeatOffer:
        moment = now_or(now)
        checked = check_address(address)
        conn = pg(unit).conn
        row: tuple | None = None
        try:
            # A savepoint: a violation of 0012's index, CHECKs or composite
            # foreign keys quotes the row — the address with it — in its
            # DETAIL, and would otherwise abort the caller's whole transaction.
            with conn.transaction():
                row = conn.execute(
                    f"INSERT INTO campaign.seat_offers "
                    f"(id, campaign_id, participant_id, offered_by, address, address_key, created_at, expires_at) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_O_COLUMNS}",
                    (
                        ident.new_id(ident.SEAT_OFFER),
                        campaign_id,
                        participant_id,
                        offered_by,
                        checked,
                        address_key(checked),
                        moment,
                        moment + OFFER_LIFETIME,
                    ),
                ).fetchone()
        except (
            psycopg.errors.UniqueViolation,
            psycopg.errors.CheckViolation,
            psycopg.errors.ForeignKeyViolation,
        ):
            # Raised below, OUTSIDE this handler, so nothing carries the
            # driver's error — and the row it quotes — on `__context__`.
            row = None
        if row is None:
            raise SeatUnavailable()
        return _offer(row)

    def get(self, unit: UnitOfWork, offer_id: str) -> SeatOffer | None:
        row = pg(unit).conn.execute(
            f"SELECT {_O_COLUMNS} FROM campaign.seat_offers WHERE id = %s", (offer_id,)
        ).fetchone()
        return None if row is None else _offer(row)

    def latest_for_seats(
        self, unit: UnitOfWork, campaign_id: str, participant_ids: Sequence[str]
    ) -> dict[str, SeatOffer]:
        if not participant_ids:
            return {}
        rows = pg(unit).conn.execute(
            f"SELECT DISTINCT ON (participant_id) {_O_COLUMNS} FROM campaign.seat_offers "
            f"WHERE campaign_id = %s AND participant_id = ANY(%s) "
            f'ORDER BY participant_id, created_at DESC, id COLLATE "C" DESC',
            (campaign_id, list(participant_ids)),
        ).fetchall()
        return {offer.participant_id: offer for offer in map(_offer, rows)}

    def is_repeat(self, unit: UnitOfWork, campaign_id: str, key: str, *, now: datetime | None = None) -> bool:
        row = pg(unit).conn.execute(
            "SELECT EXISTS (SELECT 1 FROM campaign.seat_offers o "
            "WHERE o.campaign_id = %(campaign)s AND o.address_key = %(key)s "
            "AND o.outcome IS NULL AND o.expires_at > %(now)s) "
            "OR EXISTS (SELECT 1 FROM campaign.seat_offers o "
            "JOIN campaign.participants p ON p.id = o.participant_id AND p.campaign_id = o.campaign_id "
            "WHERE o.campaign_id = %(campaign)s AND o.address_key = %(key)s AND o.outcome = 'accepted' "
            "AND p.removed_at IS NULL AND p.accepted_at IS NOT NULL)",
            {"campaign": campaign_id, "key": _check_key(key), "now": now_or(now)},
        ).fetchone()
        return bool(row[0])

    def open_for_seat(self, unit: UnitOfWork, campaign_id: str, participant_id: str) -> SeatOffer | None:
        row = pg(unit).conn.execute(
            f"SELECT {_O_COLUMNS} FROM campaign.seat_offers "
            f"WHERE participant_id = %s AND campaign_id = %s AND outcome IS NULL",
            (participant_id, campaign_id),
        ).fetchone()
        return None if row is None else _offer(row)

    def expire_stale(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        changed = pg(unit).conn.execute(
            "UPDATE campaign.seat_offers SET outcome = 'expired', answered_at = expires_at "
            "WHERE participant_id = %s AND campaign_id = %s AND outcome IS NULL AND expires_at <= %s "
            "RETURNING id",
            (participant_id, campaign_id, now_or(now)),
        ).fetchone()
        return changed is not None

    def count_recent(self, unit: UnitOfWork, owner_id: int, *, now: datetime | None = None) -> ThrottleCount:
        row = pg(unit).conn.execute(
            "SELECT count(*), min(created_at) FROM campaign.seat_offers "
            "WHERE offered_by = %s AND created_at > %s",
            (owner_id, now_or(now) - THROTTLE_WINDOW),
        ).fetchone()
        return ThrottleCount(int(row[0]), row[1])

    def _invitee_rows(
        self, unit: UnitOfWork, where: str, params: dict[str, object], tail: str = ""
    ) -> list[InviteeOffer]:
        rows = pg(unit).conn.execute(
            f"SELECT {_qualified('o', _O_COLUMNS)}, c.name, p.alias {_INVITEE_FROM}"
            f"WHERE {_INVITEE_PREDICATES} {where} {tail}",
            params,
        ).fetchall()
        return [InviteeOffer(_offer(row[:10]), str(row[10]), str(row[11])) for row in rows]

    def invitee_page(
        self,
        unit: UnitOfWork,
        key: str,
        user_id: int,
        *,
        now: datetime | None = None,
        cursor: str | None = None,
        limit: int = PAGE_MAX,
    ) -> Page[InviteeOffer]:
        size = check_limit(limit)
        after = None if cursor is None else decode_cursor(cursor, ident.SEAT_OFFER)
        rows = self._invitee_rows(
            unit,
            "AND (%(at)s::timestamptz IS NULL OR o.created_at < %(at)s::timestamptz "
            'OR (o.created_at = %(at)s::timestamptz AND o.id COLLATE "C" < %(id)s))',
            {
                "key": _check_key(key),
                "user": user_id,
                "now": now_or(now),
                "at": None if after is None else after[0],
                "id": None if after is None else after[1],
                "limit": size + 1,
            },
            'ORDER BY o.created_at DESC, o.id COLLATE "C" DESC LIMIT %(limit)s',
        )
        return page_of(rows, size, _newest_key)

    def find_for_invitee(
        self, unit: UnitOfWork, offer_id: str, key: str, user_id: int, *, now: datetime | None = None
    ) -> InviteeOffer | None:
        found = self._invitee_rows(
            unit,
            "AND o.id = %(offer)s",
            {"key": _check_key(key), "user": user_id, "now": now_or(now), "offer": offer_id},
        )
        return found[0] if found else None

    def hold_for_invitee(
        self,
        unit: UnitOfWork,
        offer_id: str,
        campaign_id: str,
        participant_id: str,
        key: str,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> InviteeOffer | None:
        pg(unit).note_row_lock()
        found = self._invitee_rows(
            unit,
            "AND o.id = %(offer)s AND o.campaign_id = %(campaign)s AND o.participant_id = %(seat)s",
            {
                "key": _check_key(key),
                "user": user_id,
                "now": now_or(now),
                "offer": offer_id,
                "campaign": campaign_id,
                "seat": participant_id,
            },
            "FOR NO KEY UPDATE OF o",
        )
        return found[0] if found else None

    def repeat_accept(self, unit: UnitOfWork, offer_id: str, key: str, user_id: int) -> HeldSeat | None:
        row = pg(unit).conn.execute(
            f"SELECT {_qualified('p', _P_COLUMNS)}, c.name FROM campaign.seat_offers o "
            f"JOIN campaign.participants p ON p.id = o.participant_id AND p.campaign_id = o.campaign_id "
            f"JOIN campaign.campaigns c ON c.id = o.campaign_id "
            f"WHERE o.id = %s AND o.address_key = %s AND o.outcome = 'accepted' "
            f"AND p.user_id = %s AND p.removed_at IS NULL AND p.accepted_at IS NOT NULL "
            f"AND c.archived_at IS NULL",
            (offer_id, _check_key(key), user_id),
        ).fetchone()
        return None if row is None else HeldSeat(_participant(row[:8]), str(row[8]))

    def repeat_decline(self, unit: UnitOfWork, offer_id: str, key: str) -> SeatOffer | None:
        row = pg(unit).conn.execute(
            f"SELECT {_O_COLUMNS} FROM campaign.seat_offers "
            f"WHERE id = %s AND address_key = %s AND outcome = 'declined'",
            (offer_id, _check_key(key)),
        ).fetchone()
        return None if row is None else _offer(row)

    def close(self, unit: UnitOfWork, offer_id: str, *, outcome: str, now: datetime | None = None) -> bool:
        if outcome not in OUTCOMES:
            raise ValueError("an offer's outcome is one of the four")
        changed = pg(unit).conn.execute(
            "UPDATE campaign.seat_offers SET outcome = %s, answered_at = %s "
            "WHERE id = %s AND outcome IS NULL RETURNING id",
            (outcome, now_or(now), offer_id),
        ).fetchone()
        return changed is not None

    def withdraw_open(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        moment = now_or(now)
        changed = pg(unit).conn.execute(
            "UPDATE campaign.seat_offers SET "
            "outcome = CASE WHEN expires_at <= %(now)s THEN 'expired' ELSE 'withdrawn' END, "
            "answered_at = CASE WHEN expires_at <= %(now)s THEN expires_at ELSE %(now)s END "
            "WHERE participant_id = %(seat)s AND campaign_id = %(campaign)s AND outcome IS NULL RETURNING id",
            {"now": moment, "seat": participant_id, "campaign": campaign_id},
        ).fetchone()
        return changed is not None

    def block(
        self, unit: UnitOfWork, *, blocker_user_id: int, blocked_owner_id: int, now: datetime | None = None
    ) -> bool:
        changed = pg(unit).conn.execute(
            "INSERT INTO campaign.seat_blocks (blocker_user_id, blocked_owner_id, created_at) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING blocker_user_id",
            (blocker_user_id, blocked_owner_id, now_or(now)),
        ).fetchone()
        return changed is not None

    def is_blocked(self, unit: UnitOfWork, blocker_user_id: int, blocked_owner_id: int) -> bool:
        row = pg(unit).conn.execute(
            "SELECT 1 FROM campaign.seat_blocks WHERE blocker_user_id = %s AND blocked_owner_id = %s",
            (blocker_user_id, blocked_owner_id),
        ).fetchone()
        return row is not None


@dataclass(frozen=True)
class _Block:
    blocker_user_id: int
    blocked_owner_id: int
    created_at: datetime


class InMemorySeatOfferStore:
    """The twin, over the tables it shares with the other campaign twins."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._participants: Staging[Participant] = shared_rows(db, "participants")
        self._offers: Staging[SeatOffer] = shared_rows(db, "seat_offers")
        self._blocks: Staging[_Block] = shared_rows(db, "seat_blocks")

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        *,
        offered_by: int,
        address: str,
        now: datetime | None = None,
    ) -> SeatOffer:
        moment = now_or(now)
        checked = check_address(address)
        twin = fake(unit)
        seat = self._participants.visible(twin).get(participant_id)
        campaign = self._campaigns.visible(twin).get(campaign_id)
        open_here = any(
            o.participant_id == participant_id and o.outcome is None for o in self._offers.visible(twin).values()
        )
        # Both composite foreign keys and the one-open-offer index, kept here.
        if seat is None or seat.campaign_id != campaign_id or campaign is None:
            raise SeatUnavailable()
        if campaign.owner_id != offered_by or open_here:
            raise SeatUnavailable()
        made = SeatOffer(
            id=ident.new_id(ident.SEAT_OFFER),
            campaign_id=campaign_id,
            participant_id=participant_id,
            offered_by=offered_by,
            address=checked,
            address_key=address_key(checked),
            created_at=moment,
            expires_at=moment + OFFER_LIFETIME,
        )
        self._offers.add(twin, made.id, made)
        return made

    def get(self, unit: UnitOfWork, offer_id: str) -> SeatOffer | None:
        return self._offers.visible(fake(unit)).get(offer_id)

    def latest_for_seats(
        self, unit: UnitOfWork, campaign_id: str, participant_ids: Sequence[str]
    ) -> dict[str, SeatOffer]:
        wanted = set(participant_ids)
        latest: dict[str, SeatOffer] = {}
        for offer in self._offers.visible(fake(unit)).values():
            if offer.campaign_id != campaign_id or offer.participant_id not in wanted:
                continue
            held = latest.get(offer.participant_id)
            if held is None or (offer.created_at, offer.id) > (held.created_at, held.id):
                latest[offer.participant_id] = offer
        return latest

    def is_repeat(self, unit: UnitOfWork, campaign_id: str, key: str, *, now: datetime | None = None) -> bool:
        moment = now_or(now)
        twin = fake(unit)
        seats = self._participants.visible(twin)
        for offer in self._offers.visible(twin).values():
            if offer.campaign_id != campaign_id or offer.address_key != _check_key(key):
                continue
            if offer.is_live(moment):
                return True
            seat = seats.get(offer.participant_id)
            if offer.outcome == ACCEPTED and seat is not None and seat.is_accepted:
                return True
        return False

    def open_for_seat(self, unit: UnitOfWork, campaign_id: str, participant_id: str) -> SeatOffer | None:
        for offer in self._offers.visible(fake(unit)).values():
            if offer.participant_id == participant_id and offer.campaign_id == campaign_id and offer.outcome is None:
                return offer
        return None

    def expire_stale(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        stale = self.open_for_seat(unit, campaign_id, participant_id)
        if stale is None or stale.expires_at > now_or(now):
            return False
        self._offers.replace(fake(unit), stale.id, replace(stale, outcome=EXPIRED, answered_at=stale.expires_at))
        return True

    def count_recent(self, unit: UnitOfWork, owner_id: int, *, now: datetime | None = None) -> ThrottleCount:
        since = now_or(now) - THROTTLE_WINDOW
        recent = [
            o.created_at
            for o in self._offers.visible(fake(unit)).values()
            if o.offered_by == owner_id and o.created_at > since
        ]
        return ThrottleCount(len(recent), min(recent) if recent else None)

    def _visible_to(
        self, unit: UnitOfWork, offer: SeatOffer, key: str, user_id: int, now: datetime
    ) -> InviteeOffer | None:
        """`_INVITEE_PREDICATES`, one offer at a time."""
        twin = fake(unit)
        seats = self._participants.visible(twin)
        seat = seats.get(offer.participant_id)
        campaign = self._campaigns.visible(twin).get(offer.campaign_id)
        if seat is None or campaign is None or seat.campaign_id != offer.campaign_id:
            return None
        if offer.address_key != key or not offer.is_live(now) or not seat.is_active or campaign.is_archived:
            return None
        if offer.offered_by == user_id or self.is_blocked(unit, user_id, offer.offered_by):
            return None
        if any(q.campaign_id == offer.campaign_id and q.user_id == user_id and q.is_active for q in seats.values()):
            return None
        return InviteeOffer(offer, campaign.name, seat.alias)

    def invitee_page(
        self,
        unit: UnitOfWork,
        key: str,
        user_id: int,
        *,
        now: datetime | None = None,
        cursor: str | None = None,
        limit: int = PAGE_MAX,
    ) -> Page[InviteeOffer]:
        size = check_limit(limit)
        after = None if cursor is None else decode_cursor(cursor, ident.SEAT_OFFER)
        moment = now_or(now)
        seen = [
            found
            for offer in self._offers.visible(fake(unit)).values()
            if (found := self._visible_to(unit, offer, _check_key(key), user_id, moment)) is not None
            and (after is None or _newest_key(found) < after)
        ]
        ordered = sorted(seen, key=_newest_key, reverse=True)
        return page_of(ordered[: size + 1], size, _newest_key)

    def find_for_invitee(
        self, unit: UnitOfWork, offer_id: str, key: str, user_id: int, *, now: datetime | None = None
    ) -> InviteeOffer | None:
        offer = self.get(unit, offer_id)
        return None if offer is None else self._visible_to(unit, offer, _check_key(key), user_id, now_or(now))

    def hold_for_invitee(
        self,
        unit: UnitOfWork,
        offer_id: str,
        campaign_id: str,
        participant_id: str,
        key: str,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> InviteeOffer | None:
        fake(unit).note_row_lock()
        found = self.find_for_invitee(unit, offer_id, key, user_id, now=now)
        if found is None or (found.offer.campaign_id, found.offer.participant_id) != (campaign_id, participant_id):
            return None
        return found

    def repeat_accept(self, unit: UnitOfWork, offer_id: str, key: str, user_id: int) -> HeldSeat | None:
        twin = fake(unit)
        offer = self._offers.visible(twin).get(offer_id)
        if offer is None or offer.address_key != _check_key(key) or offer.outcome != ACCEPTED:
            return None
        seat = self._participants.visible(twin).get(offer.participant_id)
        campaign = self._campaigns.visible(twin).get(offer.campaign_id)
        if seat is None or campaign is None or campaign.is_archived:
            return None
        if seat.user_id != user_id or not seat.is_accepted:
            return None
        return HeldSeat(seat, campaign.name)

    def repeat_decline(self, unit: UnitOfWork, offer_id: str, key: str) -> SeatOffer | None:
        offer = self.get(unit, offer_id)
        if offer is None or offer.address_key != _check_key(key) or offer.outcome != DECLINED:
            return None
        return offer

    def close(self, unit: UnitOfWork, offer_id: str, *, outcome: str, now: datetime | None = None) -> bool:
        if outcome not in OUTCOMES:
            raise ValueError("an offer's outcome is one of the four")
        offer = self.get(unit, offer_id)
        if offer is None or offer.outcome is not None:
            return False
        self._offers.replace(fake(unit), offer_id, replace(offer, outcome=outcome, answered_at=now_or(now)))
        return True

    def withdraw_open(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        moment = now_or(now)
        offer = self.open_for_seat(unit, campaign_id, participant_id)
        if offer is None:
            return False
        stale = offer.expires_at <= moment
        closed = replace(
            offer, outcome=EXPIRED if stale else WITHDRAWN, answered_at=offer.expires_at if stale else moment
        )
        self._offers.replace(fake(unit), offer.id, closed)
        return True

    def block(
        self, unit: UnitOfWork, *, blocker_user_id: int, blocked_owner_id: int, now: datetime | None = None
    ) -> bool:
        if blocker_user_id == blocked_owner_id:
            raise ValueError("an account does not block itself")
        if self.is_blocked(unit, blocker_user_id, blocked_owner_id):
            return False
        moment = aware(now_or(now), "a clock")
        key = f"{blocker_user_id}:{blocked_owner_id}"
        self._blocks.add(fake(unit), key, _Block(blocker_user_id, blocked_owner_id, moment))
        return True

    def is_blocked(self, unit: UnitOfWork, blocker_user_id: int, blocked_owner_id: int) -> bool:
        return f"{blocker_user_id}:{blocked_owner_id}" in self._blocks.visible(fake(unit))

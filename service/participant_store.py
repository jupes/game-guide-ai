"""Participants: an account's seat at a campaign (1kg.2.1, reshaped by `fma`).

A participant is a seat at a campaign's table. The owner's decisions D-1 and D-4
of 2026-09-21 (interactions ADR A-23, threat model TA-2) made every player an
account holder and removed guests, so a seat now belongs to an **account**: the
enrolment code and the device credential that used to bind a seat to a browser
are retired, and migration 0009 dropped their tables. Four rules shape
everything here.

**A seat is open, offered, accepted or removed.** Open: the GM seated an alias
while preparing, and no account holds it (`user_id` is None). Offered: the GM
offered it to one account (`user_id` set, `accepted_at` None). Accepted: that
account accepted it. Removed: `removed_at` is set. Only an offer followed by
the same account's acceptance moves a seat between the first three; there is
deliberately no way to claim an open seat by any other proof — a link, a code,
an email — because a claim-by-possession primitive would be the retired
enrolment code under a new name. A GM is never offered a seat in a campaign of
their own: the owner is the GM, never a participant.

**A participant is marked removed, never deleted** (RQ-3, W-3). A row another
transaction may be referencing — a disclosure, the character-sheet link, an
audit row — must not vanish under it. `removed_at` is the whole story:
everything that reads "the people at this table" filters on it, the alias and
the account's place in the campaign become free again the moment it is set, and
the account and the moment it accepted stay on the row.

**An alias is private text** (SEC-20, AUD-13). It is 1 to 40 characters, unique
within its campaign compared case-insensitively among participants that are not
removed, and it is stored here and nowhere else — never a log line, never an
exception message, never an audit row. `Participant.__repr__` hides it, and the
account's id with it: a traceback is a log line. `check_alias` normalises before
it stores (NFC, trimmed, inner whitespace collapsed), and the comparison is made
over an `alias_key` **the application computes**, so that `'Ana'` and `'Ana '`,
NFD `'é'` and NFC `'é'` cannot sit side by side — and so that the two worlds
cannot disagree about folding, which they would the moment PostgreSQL's
`lower()` and Python's `str.lower()` were asked about a final sigma or a dotted
capital I.

**Every mutator takes the seat's row first, naming its campaign** (RQ-3, RQ-4).
`offer`, `accept` and `remove` each begin with `hold`, which bounds the
transaction (RQ-8) and takes the row `FOR NO KEY UPDATE` in a statement that
names the campaign, and then decides on the row it was handed. Under READ
COMMITTED a row a lock wait was for is re-read, so a second caller decides on
what the first committed: it is **the row lock that closes the window**, never
an `EXISTS` in the statement. The one fact that is not on the seat's own row —
whether the account already holds another live seat in that campaign — is the
partial unique index's to decide, which is why an offer that collides with it
is refused and not reported as a foreign row's lock.

The store does not take the campaign lock and does not advance
`authz_revision`: an offer and an acceptance widen what an account may see
(RQ-4), and the route that composes them (`1kg.2.2`) takes the campaign lock
and calls the one advancing function (RQ-10), as it does for `add`. Every
explicit row lock here is `FOR NO KEY UPDATE`, never `FOR UPDATE` (RQ-3): a
foreign-key check on the participant takes `FOR KEY SHARE`, which conflicts with
`FOR UPDATE` alone. Nor does the store write audit rows — `add` and `remove`
never did; the route records `seat.offered` and `seat.accepted` with the rest
of its decision.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import NoReturn, Protocol

import psycopg

from . import campaign_identity as ident
from .campaign_store import (
    AliasTaken,
    Campaign,
    MissingParent,
    SeatUnavailable,
    Staging,
    fake,
    now_or,
    pg,
    shared_rows,
)
from .db import InMemoryDatabase, UnitOfWork

ALIAS_MAX_CHARS = 40
#: The bound `0004_campaign_schema.sql` puts on `alias_key`, pinned to the
#: migration's own text by `service/tests/test_campaign_schema_sql.py`. It is not
#: five times `ALIAS_MAX_CHARS` by accident and it is not enough by arithmetic:
#: NFKC expands, and four assigned code points expand by more than five (`U+FDFA`
#: by eighteen), so the alias's bound does not bound the key and this one is
#: checked separately.
ALIAS_KEY_MAX = 200


def check_alias(alias: str) -> str:
    """The two bounds `0004_campaign_schema.sql` carries, and the normalisation
    the column is stored in. The refusal names the rule, never the alias.

    NFC first, then trimmed and with inner whitespace collapsed to one space:
    `'Ana'`, `'Ana '` and an `'Ana'` written with a no-break space must not be
    three different seats at one table, and a single space must not be an alias
    at all — `str.split()` is what collapses them, so every space Unicode knows
    about counts and not only the ASCII one. A character from
    Unicode's C categories — a control, a format character, a lone surrogate —
    is refused outright: it is invisible, it survives no round trip intact, and
    it is how two aliases are made to look identical to a GM.

    **The key is bounded here too**, because the column that holds it is: NFKC
    expands, so twelve legal characters can fold to 216 and the row PostgreSQL
    then refuses comes back as a check violation whose DETAIL quotes the whole
    failing row — the alias with it (SEC-20). The twin would have seated it. The
    bound is the column's, not a rule of its own, so widening one means widening
    the other, and the pinning test says so.
    """
    normalised = " ".join(unicodedata.normalize("NFC", alias).split())
    if any(unicodedata.category(character)[0] == "C" for character in normalised):
        raise ValueError("an alias carries no control or formatting characters")
    if not 1 <= len(normalised) <= ALIAS_MAX_CHARS:
        raise ValueError(f"an alias is 1 to {ALIAS_MAX_CHARS} characters")
    if len(alias_key(normalised)) > ALIAS_KEY_MAX:
        raise ValueError(f"an alias folds to at most {ALIAS_KEY_MAX} characters")
    return normalised


def alias_key(alias: str) -> str:
    """What `participants_alias_uidx` compares, computed here rather than by the
    database.

    `lower(alias)` in the index would make the answer depend on the database's
    collation provider — libc or ICU, and which locale — while the twin would
    answer with Python's `str.lower()`. The two disagree about a final sigma and
    a dotted capital I, and they would disagree silently. Computing the key in
    the application makes the two worlds agree **by construction**: NFKC so that
    compatibility forms fold together, then `casefold`, which is the full
    case-insensitive comparison Unicode defines and `lower()` is not.
    """
    return unicodedata.normalize("NFKC", alias).casefold()


@dataclass(frozen=True)
class Participant:
    """A seat at the table. `alias` and `user_id` are hidden from `repr()`:
    private text and an account's identity must not reach a log line, and a
    traceback is a log line.

    A seat accepted by no account cannot be built — the CHECK migration 0009
    puts on the table, kept by the record so that the twin cannot hold a row
    PostgreSQL would refuse."""

    id: str
    campaign_id: str
    alias: str = field(repr=False)
    created_at: datetime
    removed_at: datetime | None = None
    user_id: int | None = field(default=None, repr=False)
    accepted_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.accepted_at is not None and self.user_id is None:
            raise ValueError("a seat is accepted by an account, so an accepted seat has one")

    @property
    def is_active(self) -> bool:
        return self.removed_at is None

    @property
    def is_accepted(self) -> bool:
        """Accepted by its account and not removed: a seat in the full sense,
        the only kind `seat_for` and `seats_for_user` answer with."""
        return self.accepted_at is not None and self.is_active


class ParticipantStore(Protocol):
    """The people at a campaign's table, and the accounts that hold their seats.

    **Every mutator names the campaign** in the same statement as the row
    (`docs/migrations.md` section 4), so a participant of another GM's campaign
    is indistinguishable from one that does not exist — and every mutator takes
    that row's lock itself, naming the campaign there too, so the order RQ-3
    rests on is a property of this module rather than of every caller.

    **The compositions this store is shaped for:** a GM seats an alias (`add`),
    offers the seat to an account (`offer`), the account accepts it (`accept`),
    and the GM removes it (`remove`). Each of the last three holds the seat
    first; the route around them takes the campaign lock before any of them
    and advances `authz_revision` for the two that widen (RQ-4, RQ-10).
    """

    def add(
        self, unit: UnitOfWork, campaign_id: str, *, alias: str, now: datetime | None = None
    ) -> Participant:
        """Seat someone: an open seat, which no account holds yet. Raises
        `AliasTaken` if an active participant of that campaign already answers to
        the alias, compared over `alias_key`, and `MissingParent` if there is no
        such campaign."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, participant_id: str) -> Participant | None:
        ...  # pragma: no cover - structural type

    def list_for_campaign(
        self, unit: UnitOfWork, campaign_id: str, *, include_removed: bool = False
    ) -> list[Participant]:
        """Oldest first, in every state but removed unless asked."""
        ...  # pragma: no cover - structural type

    def hold(
        self,
        unit: UnitOfWork,
        participant_id: str,
        *,
        campaign_id: str,
        transaction_timeout_s: float | None = None,
    ) -> Participant | None:
        """Hold the participant's row for the rest of this transaction and hand
        it back, or None if that campaign has no such row.

        The row comes back so that the caller decides on it **under the lock**,
        which is how `offer`, `accept` and `remove` decide. The lock is
        `FOR NO KEY UPDATE` (RQ-3), and the transaction is bounded first (RQ-8),
        because a participant-only transaction is otherwise the one holder in
        this schema with no bound at all.

        **The campaign is required.** It goes into the statement that takes the
        lock, so another campaign's seat is None — the same answer a participant
        that does not exist gets — and its row is never locked and never handed
        back. Ids are not secrets (SEC-4), so without that GM A could hold GM
        B's row for the length of the transaction bound and read B's alias out
        of it.

        **One row at a time.** No method here locks two participants, so the
        rest of RQ-3 — that a method locking several of them takes them in
        ascending id — has nothing to apply to yet. The first method that needs
        two adds one that sorts, rather than calling this twice in whatever
        order a set iterated.
        """
        ...  # pragma: no cover - structural type

    def remove(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        """Mark the seat removed — never delete it — whatever state it is in.
        The account and the moment it accepted stay on the row. Takes the seat's
        row first like every other mutator here, so a bare Remove is bounded
        too. Reports whether an active participant changed, so removing twice is
        not an error."""
        ...  # pragma: no cover - structural type

    def offer(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, user_id: int
    ) -> Participant:
        """Offer an open seat of that campaign to one account, and hand back the
        offered seat. Raises `SeatUnavailable` if the seat is missing, of
        another campaign, removed or not open, if the account is the campaign's
        owner or does not exist, or if the account already holds a live seat —
        offered or accepted — in that campaign."""
        ...  # pragma: no cover - structural type

    def accept(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> bool:
        """The account's side: accept a seat offered **to that same account**.
        True when it accepted; False, changing nothing, when that account had
        already accepted it (as `remove` twice is not an error). Every other
        case — missing, another campaign's, removed, open, offered to someone
        else — raises `SeatUnavailable`."""
        ...  # pragma: no cover - structural type

    def seat_for(self, unit: UnitOfWork, campaign_id: str, user_id: int) -> Participant | None:
        """The account's accepted, live seat at that campaign, or None. An
        offered seat is not a seat yet."""
        ...  # pragma: no cover - structural type

    def seats_for_user(self, unit: UnitOfWork, user_id: int) -> list[Participant]:
        """The account's accepted, live seats in every campaign, most recently
        accepted first."""
        ...  # pragma: no cover - structural type


_P_COLUMNS = "id, campaign_id, alias, created_at, removed_at, user_id, accepted_at"


def _participant(row: tuple) -> Participant:
    return Participant(
        row[0], row[1], row[2], row[3], row[4], None if row[5] is None else int(row[5]), row[6]
    )


class PostgresParticipantStore:
    """`campaign.participants` (migrations 0004 and 0009)."""

    def add(
        self, unit: UnitOfWork, campaign_id: str, *, alias: str, now: datetime | None = None
    ) -> Participant:
        named = check_alias(alias)
        # INSERT ... SELECT ... WHERE EXISTS rather than letting the foreign key
        # raise: a ForeignKeyViolation aborts the whole transaction and arrives
        # carrying the driver's text, so an ordinary wrong id would cost the
        # caller every other write it had composed. The foreign key is still the
        # guarantee against a campaign deleted mid-flight; this is the answer to
        # the mistake that actually happens.
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.participants (id, campaign_id, alias, alias_key, created_at) "
            f"SELECT %s, %s, %s, %s, %s "
            f"WHERE EXISTS (SELECT 1 FROM campaign.campaigns WHERE id = %s) "
            f"ON CONFLICT (campaign_id, alias_key) WHERE removed_at IS NULL "
            f"DO NOTHING RETURNING {_P_COLUMNS}",
            (
                ident.new_id(ident.PARTICIPANT),
                campaign_id,
                named,
                alias_key(named),
                now_or(now),
                campaign_id,
            ),
        ).fetchone()
        if row is None:
            self._refuse_add(unit, campaign_id)
        return _participant(row)

    def _refuse_add(self, unit: UnitOfWork, campaign_id: str) -> NoReturn:
        """Which of the two conditions refused it — asked only on the way to an
        exception, so the happy path stays one statement."""
        there = pg(unit).conn.execute(
            "SELECT 1 FROM campaign.campaigns WHERE id = %s", (campaign_id,)
        ).fetchone()
        if there is None:
            raise MissingParent("no such campaign")
        # The index refused it, which is the whole check: no read-then-write
        # window for a second caller to slip through.
        raise AliasTaken("that campaign already has an active participant with this alias")

    def get(self, unit: UnitOfWork, participant_id: str) -> Participant | None:
        row = pg(unit).conn.execute(
            f"SELECT {_P_COLUMNS} FROM campaign.participants WHERE id = %s", (participant_id,)
        ).fetchone()
        return None if row is None else _participant(row)

    def list_for_campaign(
        self, unit: UnitOfWork, campaign_id: str, *, include_removed: bool = False
    ) -> list[Participant]:
        rows = pg(unit).conn.execute(
            f'SELECT {_P_COLUMNS} FROM campaign.participants '
            f'WHERE campaign_id = %s AND (%s OR removed_at IS NULL) '
            f'ORDER BY created_at, id COLLATE "C"',
            (campaign_id, include_removed),
        ).fetchall()
        return [_participant(row) for row in rows]

    def hold(
        self,
        unit: UnitOfWork,
        participant_id: str,
        *,
        campaign_id: str,
        transaction_timeout_s: float | None = None,
    ) -> Participant | None:
        transaction = pg(unit)
        transaction.note_row_lock()
        transaction.conn.execute(
            "SELECT set_config('transaction_timeout', %s, true)",
            (transaction.transaction_bound(transaction_timeout_s),),
        )
        # The campaign goes into the statement that takes the lock, never into a
        # comparison after it: a row the WHERE does not match is not locked at
        # all, which is the whole difference (G-11).
        row = transaction.conn.execute(
            f"SELECT {_P_COLUMNS} FROM campaign.participants "
            f"WHERE id = %s AND campaign_id = %s FOR NO KEY UPDATE",
            (participant_id, campaign_id),
        ).fetchone()
        return None if row is None else _participant(row)

    def remove(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        self.hold(unit, participant_id, campaign_id=campaign_id)
        changed = pg(unit).conn.execute(
            "UPDATE campaign.participants SET removed_at = %s "
            "WHERE id = %s AND campaign_id = %s AND removed_at IS NULL RETURNING id",
            (now_or(now), participant_id, campaign_id),
        ).fetchone()
        return changed is not None

    def offer(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, user_id: int
    ) -> Participant:
        self.hold(unit, participant_id, campaign_id=campaign_id)
        conn = pg(unit).conn
        row: tuple | None = None
        try:
            # A savepoint, because the one refusal this statement cannot express
            # in its WHERE — the account already holds a live seat in this
            # campaign — is the partial unique index's, and a UniqueViolation
            # would otherwise abort the caller's whole transaction. Whether the
            # account exists is asked rather than left to the foreign key for the
            # same reason; the ForeignKeyViolation is still caught, for an account
            # deleted between that EXISTS and the foreign key's own check.
            with conn.transaction():
                row = conn.execute(
                    f"UPDATE campaign.participants p SET user_id = %s "
                    f"WHERE p.id = %s AND p.campaign_id = %s "
                    f"AND p.removed_at IS NULL AND p.user_id IS NULL "
                    f"AND EXISTS (SELECT 1 FROM auth.users u WHERE u.id = %s) "
                    f"AND NOT EXISTS (SELECT 1 FROM campaign.campaigns c "
                    f"WHERE c.id = p.campaign_id AND c.owner_id = %s) "
                    f"RETURNING {_P_COLUMNS}",
                    (user_id, participant_id, campaign_id, user_id, user_id),
                ).fetchone()
        except (psycopg.errors.UniqueViolation, psycopg.errors.ForeignKeyViolation):
            # Both quote the campaign and the account in their DETAIL. The
            # refusal is raised below, OUTSIDE this handler: raised in here, even
            # `from None`, it would carry the driver's error on `__context__`
            # for anything that walks the chain.
            row = None
        if row is None:
            raise SeatUnavailable()
        return _participant(row)

    def accept(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> bool:
        seat = self.hold(unit, participant_id, campaign_id=campaign_id)
        if seat is None or not seat.is_active or seat.user_id != user_id:
            raise SeatUnavailable()
        if seat.accepted_at is not None:
            return False
        changed = pg(unit).conn.execute(
            "UPDATE campaign.participants SET accepted_at = %s "
            "WHERE id = %s AND campaign_id = %s AND removed_at IS NULL "
            "AND user_id = %s AND accepted_at IS NULL RETURNING id",
            (now_or(now), participant_id, campaign_id, user_id),
        ).fetchone()
        return changed is not None

    def seat_for(self, unit: UnitOfWork, campaign_id: str, user_id: int) -> Participant | None:
        row = pg(unit).conn.execute(
            f"SELECT {_P_COLUMNS} FROM campaign.participants "
            f"WHERE campaign_id = %s AND user_id = %s "
            f"AND removed_at IS NULL AND accepted_at IS NOT NULL",
            (campaign_id, user_id),
        ).fetchone()
        return None if row is None else _participant(row)

    def seats_for_user(self, unit: UnitOfWork, user_id: int) -> list[Participant]:
        rows = pg(unit).conn.execute(
            f'SELECT {_P_COLUMNS} FROM campaign.participants '
            f'WHERE user_id = %s AND removed_at IS NULL AND accepted_at IS NOT NULL '
            f'ORDER BY accepted_at DESC, id COLLATE "C"',
            (user_id,),
        ).fetchall()
        return [_participant(row) for row in rows]


class InMemoryParticipantStore:
    """The twin. It enforces every rule the parametrised suite asserts — the
    alias index, the one-live-seat-per-account index, the seat states and the
    campaign that owns the seat — because a rule the fake is not obliged to keep
    is a rule it will drift on. Its tables are the database's, shared with the
    other two twins (`shared_rows`), so a child of a parent that does not exist
    is refused here as a foreign key refuses it there.

    What it does not model: conflict (its transactions are serial, so the races
    are proved against PostgreSQL), and accounts — it has no users table, so an
    offer to an account that does not exist is refused by PostgreSQL alone."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._participants: Staging[Participant] = shared_rows(db, "participants")

    def add(
        self, unit: UnitOfWork, campaign_id: str, *, alias: str, now: datetime | None = None
    ) -> Participant:
        twin = fake(unit)
        named = check_alias(alias)
        if campaign_id not in self._campaigns.visible(twin):
            raise MissingParent("no such campaign")
        taken = {
            alias_key(p.alias)
            for p in self._participants.visible(twin).values()
            if p.campaign_id == campaign_id and p.is_active
        }
        if alias_key(named) in taken:
            raise AliasTaken("that campaign already has an active participant with this alias")
        participant = Participant(
            id=ident.new_id(ident.PARTICIPANT),
            campaign_id=campaign_id,
            alias=named,
            created_at=now_or(now),
        )
        self._participants.add(twin, participant.id, participant)
        return participant

    def get(self, unit: UnitOfWork, participant_id: str) -> Participant | None:
        return self._participants.visible(fake(unit)).get(participant_id)

    def list_for_campaign(
        self, unit: UnitOfWork, campaign_id: str, *, include_removed: bool = False
    ) -> list[Participant]:
        seats = [
            p
            for p in self._participants.visible(fake(unit)).values()
            if p.campaign_id == campaign_id and (include_removed or p.is_active)
        ]
        return sorted(seats, key=lambda p: (p.created_at, p.id))

    def hold(
        self,
        unit: UnitOfWork,
        participant_id: str,
        *,
        campaign_id: str,
        transaction_timeout_s: float | None = None,
    ) -> Participant | None:
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(transaction_timeout_s)
        found = self._participants.visible(twin).get(participant_id)
        if found is None or found.campaign_id != campaign_id:
            return None
        return found

    def remove(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        found = self.hold(unit, participant_id, campaign_id=campaign_id)
        if found is None or not found.is_active:
            return False
        self._participants.replace(
            fake(unit), participant_id, replace(found, removed_at=now_or(now))
        )
        return True

    def offer(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, user_id: int
    ) -> Participant:
        twin = fake(unit)
        seat = self.hold(unit, participant_id, campaign_id=campaign_id)
        campaign = self._campaigns.visible(twin).get(campaign_id)
        if seat is None or campaign is None or not seat.is_active or seat.user_id is not None:
            raise SeatUnavailable()
        if campaign.owner_id == user_id:
            raise SeatUnavailable()
        # `participants_one_live_seat_per_account_uidx`, kept here as the index
        # keeps it there: over the committed rows plus this unit's own.
        if any(
            p.campaign_id == campaign_id and p.user_id == user_id and p.is_active
            for p in self._participants.visible(twin).values()
        ):
            raise SeatUnavailable()
        offered = replace(seat, user_id=user_id)
        self._participants.replace(twin, participant_id, offered)
        return offered

    def accept(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> bool:
        seat = self.hold(unit, participant_id, campaign_id=campaign_id)
        if seat is None or not seat.is_active or seat.user_id != user_id:
            raise SeatUnavailable()
        if seat.accepted_at is not None:
            return False
        self._participants.replace(
            fake(unit), participant_id, replace(seat, accepted_at=now_or(now))
        )
        return True

    def seat_for(self, unit: UnitOfWork, campaign_id: str, user_id: int) -> Participant | None:
        for p in self._participants.visible(fake(unit)).values():
            if p.campaign_id == campaign_id and p.user_id == user_id and p.is_accepted:
                return p
        return None

    def seats_for_user(self, unit: UnitOfWork, user_id: int) -> list[Participant]:
        mine = [
            p
            for p in self._participants.visible(fake(unit)).values()
            if p.user_id == user_id and p.is_accepted
        ]
        # Newest acceptance first, ties by id as `COLLATE "C"` breaks them: the
        # id sort first, then a stable sort on the time, reversed.
        by_id = sorted(mine, key=lambda p: p.id)
        return sorted(by_id, key=lambda p: p.accepted_at or p.created_at, reverse=True)

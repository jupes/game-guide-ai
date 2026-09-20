"""Participants, their enrolment codes and their device credentials (1kg.2.1).

A participant is a seat at a campaign's table: an alias the GM chose, and — once
they have followed their personal link — one device bound to that seat. Four
rules from the records shape everything here.

**A participant is marked removed, never deleted** (RQ-3, W-3). A row another
transaction may be referencing must not vanish under it, and a removal must stay
legible to an audit row that names it. `removed_at` is the whole story:
everything that reads "the people at this table" filters on it, the alias
becomes free again the moment it is set, and nothing may be minted against the
seat afterwards.

**An alias is private text** (SEC-20, AUD-13). It is 1 to 40 characters, unique
within its campaign compared case-insensitively among participants that are not
removed, and it is stored here and nowhere else — never a log line, never an
exception message, never an audit row. `Participant.__repr__` hides it for the
same reason the digests are hidden: a traceback is a log line. `check_alias`
normalises before it stores (NFC, trimmed, inner whitespace collapsed), and the
comparison is made over an `alias_key` **the application computes**, so that
`'Ana'` and `'Ana '`, NFD `'é'` and NFC `'é'` cannot sit side by side — and so
that the two worlds cannot disagree about folding, which they would the moment
PostgreSQL's `lower()` and Python's `str.lower()` were asked about a final sigma
or a dotted capital I.

**Only digests are stored** (SEC-5). An enrolment code and a device credential
are minted once, handed to the caller once, and kept only as a SHA-256 digest
that every lookup matches exactly. A code is single-use and expires seven days
after it is issued (AUD-4); a participant has at most one live code and at most
one unrevoked device credential (AUD-5).

**Why `consume_code` is a conditional write** (RQ-5, RC-13, W-4). Redeeming a
code is one guarded `UPDATE ... RETURNING`, so two browsers racing the same link
cannot both win: exactly one statement changes a row, and the other reports
false. It does **not** take the participant's lock itself — `hold` is a
first-class primitive and the caller takes it, because the caller is also the
one that must read `is_active` under it and then mint the device credential in
the same transaction. Finding which participant a code belongs to is a separate
read that takes no lock, so a wrong code costs nothing.

**The two compositions, spelled out**, because the order is the whole point of
RQ-3 and the reason RC-13 holds:

    enrolment  find_code (no lock) -> hold -> consume_code -> issue_device_credential
    Reset      hold -> revoke_device_credentials -> issue_code
    Remove     remove  (which revokes both by itself)

Both take the **participant row first**. `issue_code` — what a Reset calls —
takes the participant row and then the code rows; an enrolment that consumed
first would take the code row and then the participant row, the inverted order
that lets the two deadlock on a `40P01` and lose the Reset, which is the remedy
for an intercepted personal link and is never allowed to fail.

Every explicit row lock this module takes is `FOR NO KEY UPDATE`, never
`FOR UPDATE` (RQ-3): a foreign-key check on the participant takes `FOR KEY
SHARE`, which conflicts with `FOR UPDATE` alone, so the stronger lock would make
a join wait behind a Stop and let two of them deadlock.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn, Protocol

from . import campaign_identity as ident
from .campaign_store import (
    AliasTaken,
    Campaign,
    MissingParent,
    ParticipantRemoved,
    Staging,
    fake,
    now_or,
    pg,
    shared_rows,
)
from .db import InMemoryDatabase, InMemoryTransaction, UnitOfWork

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
    """A seat at the table. `alias` is hidden from `repr()`: private text must
    not reach a log line, and a traceback is a log line."""

    id: str
    campaign_id: str
    alias: str = field(repr=False)
    created_at: datetime
    removed_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.removed_at is None


@dataclass(frozen=True)
class EnrolmentCode:
    """What the GM hands a player once. Only the digest is here; the code itself
    exists for the length of one `issue_code` call."""

    id: str
    participant_id: str
    code_digest: str = field(repr=False)
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_live(self, now: datetime) -> bool:
        return self.consumed_at is None and self.revoked_at is None and self.expires_at > now


@dataclass(frozen=True)
class DeviceCredential:
    """The one device a participant's seat is bound to (AUD-5)."""

    id: str
    participant_id: str
    credential_digest: str = field(repr=False)
    created_at: datetime
    revoked_at: datetime | None = None
    last_seen_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class ParticipantStore(Protocol):
    """The people at a campaign's table, and what binds them to a device.

    **Every mutator names the campaign** in the same statement as the row
    (`docs/migrations.md` section 4), so a participant of another GM's campaign
    is indistinguishable from one that does not exist. `hold` is the exception
    and is deliberate: the player's unauthenticated enrolment route knows a code
    and nothing else, so it locks the row first and learns the campaign — and
    whether the seat is still active — from the row it is handed back.

    **The compositions this store is shaped for** (the module docstring says
    why the order matters):

    * enrolment — `find_code` (no lock) then `hold` then `consume_code` then
      `issue_device_credential`;
    * Reset personal link — `hold` then `revoke_device_credentials` then
      `issue_code`;
    * Remove — `remove`, which revokes the codes and the device itself.
    """

    def add(
        self, unit: UnitOfWork, campaign_id: str, *, alias: str, now: datetime | None = None
    ) -> Participant:
        """Seat someone. Raises `AliasTaken` if an active participant of that
        campaign already answers to the alias, compared over `alias_key`, and
        `MissingParent` if there is no such campaign."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, participant_id: str) -> Participant | None:
        ...  # pragma: no cover - structural type

    def list_for_campaign(
        self, unit: UnitOfWork, campaign_id: str, *, include_removed: bool = False
    ) -> list[Participant]:
        """Oldest first."""
        ...  # pragma: no cover - structural type

    def hold(
        self,
        unit: UnitOfWork,
        participant_id: str,
        *,
        transaction_timeout_s: float | None = None,
    ) -> Participant | None:
        """Hold the participant's row for the rest of this transaction and hand
        it back, or None if there is no such row.

        The row comes back so that the caller tests `is_active` **under the
        lock** — every enrolment, Remove and Reset meets here, which is what
        RC-13 rests on — and so that it learns the campaign without a second
        read. The lock is `FOR NO KEY UPDATE` (RQ-3), and the transaction is
        bounded first (RQ-8), because a participant-only transaction is
        otherwise the one holder in this schema with no bound at all.

        **One row at a time.** No method in this bead locks two participants, so
        the rest of RQ-3 — that a method locking several of them takes them in
        ascending id — has nothing to apply to yet. The first method that needs
        two (`1kg.2.3`'s "Also reset personal links") adds one that sorts,
        rather than calling this twice in whatever order a set iterated.
        """
        ...  # pragma: no cover - structural type

    def remove(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        """Mark the seat removed — never delete it — and revoke its codes and
        its device credential in the same call (RQ-5, AUD-16): nobody ever wants
        a removed participant with a live credential. Reports whether an active
        participant changed, so removing twice is not an error."""
        ...  # pragma: no cover - structural type

    def revoke_codes(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> int:
        """Revoke every live code of that seat and report how many, minting
        nothing. A Remove needs this; so does anything that must make a code
        useless without handing out another."""
        ...  # pragma: no cover - structural type

    def revoke_device_credentials(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> int:
        """Revoke every unrevoked device credential of that seat and report how
        many, minting nothing. A Reset revokes the device and issues a *code*
        (AUD-5), so it cannot go through `issue_device_credential`."""
        ...  # pragma: no cover - structural type

    def issue_code(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> tuple[EnrolmentCode, str]:
        """Mint this participant's personal code, revoking whatever live row they
        had first — including an expired one, which the live-code index cannot
        see past because an index cannot read the clock. Returns the record and
        the code itself, which is the only time the code exists. Refuses a
        removed seat with `ParticipantRemoved` and an unknown one with
        `MissingParent`."""
        ...  # pragma: no cover - structural type

    def find_code(self, unit: UnitOfWork, digest: str) -> EnrolmentCode | None:
        """Which code a digest names, taking no lock — so a wrong code costs
        nothing and cannot be used to hold anything."""
        ...  # pragma: no cover - structural type

    def consume_code(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        digest: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Spend the code, if it is that participant's, the participant is still
        active, and the code is unconsumed, unrevoked and unexpired. Exactly one
        of two racing callers gets True.

        A removed seat reports **False**, not an exception: this is the
        unauthenticated route, RQ-5 asks it to fail generically when no row
        changed, and a caller must not be able to tell a removed seat from a
        spent code. The check is in the statement, so there is no window between
        it and the write.
        """
        ...  # pragma: no cover - structural type

    def codes(self, unit: UnitOfWork, participant_id: str) -> list[EnrolmentCode]:
        """Every code ever issued to that participant, oldest first."""
        ...  # pragma: no cover - structural type

    def issue_device_credential(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> tuple[DeviceCredential, str]:
        """Bind a device, revoking the participant's previous one (AUD-5).
        Refuses a removed seat with `ParticipantRemoved`."""
        ...  # pragma: no cover - structural type

    def find_device_credential(self, unit: UnitOfWork, digest: str) -> DeviceCredential | None:
        ...  # pragma: no cover - structural type

    def device_credentials(self, unit: UnitOfWork, participant_id: str) -> list[DeviceCredential]:
        """Every credential ever issued to that participant, oldest first."""
        ...  # pragma: no cover - structural type


_P_COLUMNS = "id, campaign_id, alias, created_at, removed_at"
_C_COLUMNS = "id, participant_id, code_digest, expires_at, created_at, consumed_at, revoked_at"
_D_COLUMNS = "id, participant_id, credential_digest, created_at, revoked_at, last_seen_at"

#: The seat a mint hangs off, as a condition inside the minting statement itself
#: rather than as a read before it: it names the campaign (ownership belongs in
#: the query) and it refuses a removed seat (RQ-5, RC-13), with no window in
#: between. Its parameters are `(participant_id, campaign_id)`.
_ACTIVE_SEAT = (
    "SELECT 1 FROM campaign.participants p "
    "WHERE p.id = %s AND p.campaign_id = %s AND p.removed_at IS NULL"
)


def _participant(row: tuple) -> Participant:
    return Participant(row[0], row[1], row[2], row[3], row[4])


def _code(row: tuple) -> EnrolmentCode:
    return EnrolmentCode(row[0], row[1], row[2], row[3], row[4], row[5], row[6])


def _credential(row: tuple) -> DeviceCredential:
    return DeviceCredential(row[0], row[1], row[2], row[3], row[4], row[5])


class PostgresParticipantStore:
    """`campaign.participants`, `campaign.enrolment_codes` and
    `campaign.device_credentials` (migration 0004)."""

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
        transaction_timeout_s: float | None = None,
    ) -> Participant | None:
        transaction = pg(unit)
        transaction.note_row_lock()
        transaction.conn.execute(
            "SELECT set_config('transaction_timeout', %s, true)",
            (transaction.transaction_bound(transaction_timeout_s),),
        )
        row = transaction.conn.execute(
            f"SELECT {_P_COLUMNS} FROM campaign.participants WHERE id = %s FOR NO KEY UPDATE",
            (participant_id,),
        ).fetchone()
        return None if row is None else _participant(row)

    def remove(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        moment = now_or(now)
        changed = pg(unit).conn.execute(
            "UPDATE campaign.participants SET removed_at = %s "
            "WHERE id = %s AND campaign_id = %s AND removed_at IS NULL RETURNING id",
            (moment, participant_id, campaign_id),
        ).fetchone()
        if changed is None:
            return False
        self.revoke_codes(unit, campaign_id, participant_id, now=moment)
        self.revoke_device_credentials(unit, campaign_id, participant_id, now=moment)
        return True

    def revoke_codes(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> int:
        revoked = pg(unit).conn.execute(
            "UPDATE campaign.enrolment_codes c SET revoked_at = %s "
            "WHERE c.participant_id = %s AND c.consumed_at IS NULL AND c.revoked_at IS NULL "
            "AND EXISTS (SELECT 1 FROM campaign.participants p "
            "WHERE p.id = c.participant_id AND p.campaign_id = %s) RETURNING c.id",
            (now_or(now), participant_id, campaign_id),
        ).fetchall()
        return len(revoked)

    def revoke_device_credentials(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> int:
        revoked = pg(unit).conn.execute(
            "UPDATE campaign.device_credentials d SET revoked_at = %s "
            "WHERE d.participant_id = %s AND d.revoked_at IS NULL "
            "AND EXISTS (SELECT 1 FROM campaign.participants p "
            "WHERE p.id = d.participant_id AND p.campaign_id = %s) RETURNING d.id",
            (now_or(now), participant_id, campaign_id),
        ).fetchall()
        return len(revoked)

    def _refuse_seat(self, unit: UnitOfWork, campaign_id: str, participant_id: str) -> NoReturn:
        seat = pg(unit).conn.execute(
            "SELECT removed_at FROM campaign.participants WHERE id = %s AND campaign_id = %s",
            (participant_id, campaign_id),
        ).fetchone()
        if seat is None:
            raise MissingParent("no such participant in that campaign")
        raise ParticipantRemoved("that seat has been removed")

    def issue_code(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> tuple[EnrolmentCode, str]:
        moment = now_or(now)
        self.hold(unit, participant_id)
        self.revoke_codes(unit, campaign_id, participant_id, now=moment)
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.enrolment_codes "
            f"(id, participant_id, code_digest, expires_at, created_at) "
            f"SELECT %s, %s, %s, %s, %s WHERE EXISTS ({_ACTIVE_SEAT}) RETURNING {_C_COLUMNS}",
            (
                ident.new_id(ident.ENROLMENT_CODE),
                participant_id,
                minted.digest,
                ident.code_expiry(moment),
                moment,
                participant_id,
                campaign_id,
            ),
        ).fetchone()
        if row is None:
            self._refuse_seat(unit, campaign_id, participant_id)
        return _code(row), minted.secret

    def find_code(self, unit: UnitOfWork, digest: str) -> EnrolmentCode | None:
        row = pg(unit).conn.execute(
            f"SELECT {_C_COLUMNS} FROM campaign.enrolment_codes WHERE code_digest = %s", (digest,)
        ).fetchone()
        return None if row is None else _code(row)

    def consume_code(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        digest: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        moment = now_or(now)
        spent = pg(unit).conn.execute(
            f"UPDATE campaign.enrolment_codes SET consumed_at = %s "
            f"WHERE participant_id = %s AND code_digest = %s "
            f"AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > %s "
            f"AND EXISTS ({_ACTIVE_SEAT}) RETURNING id",
            (moment, participant_id, digest, moment, participant_id, campaign_id),
        ).fetchone()
        return spent is not None

    def codes(self, unit: UnitOfWork, participant_id: str) -> list[EnrolmentCode]:
        rows = pg(unit).conn.execute(
            f'SELECT {_C_COLUMNS} FROM campaign.enrolment_codes '
            f'WHERE participant_id = %s ORDER BY created_at, id COLLATE "C"',
            (participant_id,),
        ).fetchall()
        return [_code(row) for row in rows]

    def issue_device_credential(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> tuple[DeviceCredential, str]:
        moment = now_or(now)
        self.hold(unit, participant_id)
        self.revoke_device_credentials(unit, campaign_id, participant_id, now=moment)
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.device_credentials "
            f"(id, participant_id, credential_digest, created_at) "
            f"SELECT %s, %s, %s, %s WHERE EXISTS ({_ACTIVE_SEAT}) RETURNING {_D_COLUMNS}",
            (
                ident.new_id(ident.DEVICE_CREDENTIAL),
                participant_id,
                minted.digest,
                moment,
                participant_id,
                campaign_id,
            ),
        ).fetchone()
        if row is None:
            self._refuse_seat(unit, campaign_id, participant_id)
        return _credential(row), minted.secret

    def find_device_credential(self, unit: UnitOfWork, digest: str) -> DeviceCredential | None:
        row = pg(unit).conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.device_credentials WHERE credential_digest = %s",
            (digest,),
        ).fetchone()
        return None if row is None else _credential(row)

    def device_credentials(self, unit: UnitOfWork, participant_id: str) -> list[DeviceCredential]:
        rows = pg(unit).conn.execute(
            f'SELECT {_D_COLUMNS} FROM campaign.device_credentials '
            f'WHERE participant_id = %s ORDER BY created_at, id COLLATE "C"',
            (participant_id,),
        ).fetchall()
        return [_credential(row) for row in rows]


class InMemoryParticipantStore:
    """The twin. It enforces every rule the parametrised suite asserts — the
    alias index, single-use codes, one live code and one active device, the seat
    a mint hangs off and the campaign that owns it — because a rule the fake is
    not obliged to keep is a rule it will drift on. Its tables are the
    database's, shared with the other two twins (`shared_rows`), so a child of a
    parent that does not exist is refused here as a foreign key refuses it
    there. What it does not model is conflict: its transactions are serial, so
    the racing consumption of one code is proved against PostgreSQL instead."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._participants: Staging[Participant] = shared_rows(db, "participants")
        self._codes: Staging[EnrolmentCode] = shared_rows(db, "enrolment_codes")
        self._credentials: Staging[DeviceCredential] = shared_rows(db, "device_credentials")

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
        transaction_timeout_s: float | None = None,
    ) -> Participant | None:
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(transaction_timeout_s)
        return self._participants.visible(twin).get(participant_id)

    def _seat(self, twin: InMemoryTransaction, campaign_id: str, participant_id: str) -> Participant:
        """The active seat a mint hangs off, or the refusal PostgreSQL's
        `WHERE EXISTS` produces for the same row."""
        found = self._participants.visible(twin).get(participant_id)
        if found is None or found.campaign_id != campaign_id:
            raise MissingParent("no such participant in that campaign")
        if not found.is_active:
            raise ParticipantRemoved("that seat has been removed")
        return found

    def remove(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> bool:
        twin = fake(unit)
        found = self._participants.visible(twin).get(participant_id)
        if found is None or found.campaign_id != campaign_id or not found.is_active:
            return False
        moment = now_or(now)
        # The codes and the device first, while the seat is still active: the
        # two revocations name the campaign through the seat, exactly as the
        # statements do.
        self.revoke_codes(unit, campaign_id, participant_id, now=moment)
        self.revoke_device_credentials(unit, campaign_id, participant_id, now=moment)
        self._participants.replace(
            twin,
            participant_id,
            Participant(
                id=found.id,
                campaign_id=found.campaign_id,
                alias=found.alias,
                created_at=found.created_at,
                removed_at=moment,
            ),
        )
        return True

    def revoke_codes(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> int:
        twin = fake(unit)
        seat = self._participants.visible(twin).get(participant_id)
        if seat is None or seat.campaign_id != campaign_id:
            return 0
        moment = now_or(now)
        revoked = 0
        for code in list(self._codes.visible(twin).values()):
            if code.participant_id != participant_id:
                continue
            if code.consumed_at is None and code.revoked_at is None:
                self._codes.replace(
                    twin, code.id, EnrolmentCode(
                        code.id, code.participant_id, code.code_digest, code.expires_at,
                        code.created_at, code.consumed_at, moment,
                    )
                )
                revoked += 1
        return revoked

    def revoke_device_credentials(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> int:
        twin = fake(unit)
        seat = self._participants.visible(twin).get(participant_id)
        if seat is None or seat.campaign_id != campaign_id:
            return 0
        moment = now_or(now)
        revoked = 0
        for held in list(self._credentials.visible(twin).values()):
            if held.participant_id == participant_id and held.is_active:
                self._credentials.replace(
                    twin, held.id, DeviceCredential(
                        held.id, held.participant_id, held.credential_digest,
                        held.created_at, moment, held.last_seen_at,
                    )
                )
                revoked += 1
        return revoked

    def issue_code(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> tuple[EnrolmentCode, str]:
        twin = fake(unit)
        self.hold(unit, participant_id)
        self._seat(twin, campaign_id, participant_id)
        moment = now_or(now)
        self.revoke_codes(unit, campaign_id, participant_id, now=moment)
        minted = ident.new_secret()
        code = EnrolmentCode(
            id=ident.new_id(ident.ENROLMENT_CODE),
            participant_id=participant_id,
            code_digest=minted.digest,
            expires_at=ident.code_expiry(moment),
            created_at=moment,
        )
        self._codes.add(twin, code.id, code)
        return code, minted.secret

    def find_code(self, unit: UnitOfWork, digest: str) -> EnrolmentCode | None:
        found = [c for c in self._codes.visible(fake(unit)).values() if c.code_digest == digest]
        return found[0] if found else None

    def consume_code(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        participant_id: str,
        digest: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        twin = fake(unit)
        moment = now_or(now)
        seat = self._participants.visible(twin).get(participant_id)
        if seat is None or seat.campaign_id != campaign_id or not seat.is_active:
            return False
        for code in self._codes.visible(twin).values():
            if code.participant_id != participant_id or code.code_digest != digest:
                continue
            if not code.is_live(moment):
                return False
            self._codes.replace(
                twin, code.id, EnrolmentCode(
                    code.id, code.participant_id, code.code_digest, code.expires_at,
                    code.created_at, moment, code.revoked_at,
                )
            )
            return True
        return False

    def codes(self, unit: UnitOfWork, participant_id: str) -> list[EnrolmentCode]:
        mine = [
            c for c in self._codes.visible(fake(unit)).values() if c.participant_id == participant_id
        ]
        return sorted(mine, key=lambda c: (c.created_at, c.id))

    def issue_device_credential(
        self, unit: UnitOfWork, campaign_id: str, participant_id: str, *, now: datetime | None = None
    ) -> tuple[DeviceCredential, str]:
        twin = fake(unit)
        self.hold(unit, participant_id)
        self._seat(twin, campaign_id, participant_id)
        moment = now_or(now)
        self.revoke_device_credentials(unit, campaign_id, participant_id, now=moment)
        minted = ident.new_secret()
        credential = DeviceCredential(
            id=ident.new_id(ident.DEVICE_CREDENTIAL),
            participant_id=participant_id,
            credential_digest=minted.digest,
            created_at=moment,
        )
        self._credentials.add(twin, credential.id, credential)
        return credential, minted.secret

    def find_device_credential(self, unit: UnitOfWork, digest: str) -> DeviceCredential | None:
        found = [
            d for d in self._credentials.visible(fake(unit)).values() if d.credential_digest == digest
        ]
        return found[0] if found else None

    def device_credentials(self, unit: UnitOfWork, participant_id: str) -> list[DeviceCredential]:
        mine = [
            d
            for d in self._credentials.visible(fake(unit)).values()
            if d.participant_id == participant_id
        ]
        return sorted(mine, key=lambda d: (d.created_at, d.id))

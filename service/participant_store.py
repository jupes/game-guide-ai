"""Participants, their enrolment codes and their device credentials (1kg.2.1).

A participant is a seat at a campaign's table: an alias the GM chose, and — once
they have followed their personal link — one device bound to that seat. Three
rules from the records shape everything here.

**A participant is marked removed, never deleted** (RQ-3, W-3). A row another
transaction may be referencing must not vanish under it, and a removal must stay
legible to an audit row that names it. `removed_at` is the whole story:
everything that reads "the people at this table" filters on it, and the alias
becomes free again the moment it is set.

**An alias is private text** (SEC-20, AUD-13). It is 1 to 40 characters, unique
within its campaign compared case-insensitively among participants that are not
removed, and it is stored here and nowhere else — never a log line, never an
exception message, never an audit row. `Participant.__repr__` hides it for the
same reason the digests are hidden: a traceback is a log line.

**Only digests are stored** (SEC-5). An enrolment code and a device credential
are minted once, handed to the caller once, and kept only as a SHA-256 digest
that every lookup matches exactly. A code is single-use and expires seven days
after it is issued (AUD-4); a participant has at most one live code and at most
one unrevoked device credential (AUD-5).

**Why `consume_code` is a conditional write** (RQ-5, RC-13, W-4). Redeeming a
code is one guarded `UPDATE ... RETURNING` under the participant's row lock, so
two browsers racing the same link cannot both win: exactly one statement changes
a row, and the other reports false. Finding which participant a code belongs to
is a separate read that takes no lock, so a wrong code costs nothing.

Every explicit row lock this module takes is `FOR NO KEY UPDATE`, never
`FOR UPDATE` (RQ-3): a foreign-key check on the participant takes `FOR KEY
SHARE`, which conflicts with `FOR UPDATE` alone, so the stronger lock would make
a join wait behind a Stop and let two of them deadlock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from . import campaign_identity as ident
from .campaign_store import AliasTaken, Staging, fake, now_or, pg
from .db import UnitOfWork

ALIAS_MAX_CHARS = 40


def check_alias(alias: str) -> str:
    """The bound `0004_campaign_schema.sql` carries. The refusal names the rule,
    never the alias."""
    if not 1 <= len(alias) <= ALIAS_MAX_CHARS:
        raise ValueError(f"an alias is 1 to {ALIAS_MAX_CHARS} characters")
    return alias


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
    """The people at a campaign's table, and what binds them to a device."""

    def add(
        self, unit: UnitOfWork, campaign_id: str, *, alias: str, now: datetime | None = None
    ) -> Participant:
        """Seat someone. Raises `AliasTaken` if an active participant of that
        campaign already answers to the alias, compared case-insensitively."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, participant_id: str) -> Participant | None:
        ...  # pragma: no cover - structural type

    def list_for_campaign(
        self, unit: UnitOfWork, campaign_id: str, *, include_removed: bool = False
    ) -> list[Participant]:
        """Oldest first."""
        ...  # pragma: no cover - structural type

    def remove(self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None) -> bool:
        """Mark the seat removed — never delete it. Reports whether an active
        participant changed, so removing twice is not an error."""
        ...  # pragma: no cover - structural type

    def issue_code(
        self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None
    ) -> tuple[EnrolmentCode, str]:
        """Mint this participant's personal code, revoking whatever live row they
        had first — including an expired one, which the live-code index cannot
        see past because an index cannot read the clock. Returns the record and
        the code itself, which is the only time the code exists."""
        ...  # pragma: no cover - structural type

    def find_code(self, unit: UnitOfWork, digest: str) -> EnrolmentCode | None:
        """Which code a digest names, taking no lock — so a wrong code costs
        nothing and cannot be used to hold anything."""
        ...  # pragma: no cover - structural type

    def consume_code(
        self, unit: UnitOfWork, participant_id: str, digest: str, *, now: datetime | None = None
    ) -> bool:
        """Spend the code, if it is that participant's and is unconsumed,
        unrevoked and unexpired. Exactly one of two racing callers gets True."""
        ...  # pragma: no cover - structural type

    def codes(self, unit: UnitOfWork, participant_id: str) -> list[EnrolmentCode]:
        """Every code ever issued to that participant, oldest first."""
        ...  # pragma: no cover - structural type

    def issue_device_credential(
        self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None
    ) -> tuple[DeviceCredential, str]:
        """Bind a device, revoking the participant's previous one (AUD-5)."""
        ...  # pragma: no cover - structural type

    def find_device_credential(self, unit: UnitOfWork, digest: str) -> DeviceCredential | None:
        ...  # pragma: no cover - structural type

    def device_credentials(self, unit: UnitOfWork, participant_id: str) -> list[DeviceCredential]:
        """Every credential ever issued to that participant, oldest first."""
        ...  # pragma: no cover - structural type


_P_COLUMNS = "id, campaign_id, alias, created_at, removed_at"
_C_COLUMNS = "id, participant_id, code_digest, expires_at, created_at, consumed_at, revoked_at"
_D_COLUMNS = "id, participant_id, credential_digest, created_at, revoked_at, last_seen_at"


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
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.participants (id, campaign_id, alias, created_at) "
            f"VALUES (%s, %s, %s, %s) "
            f"ON CONFLICT (campaign_id, lower(alias)) WHERE removed_at IS NULL "
            f"DO NOTHING RETURNING {_P_COLUMNS}",
            (ident.new_id(ident.PARTICIPANT), campaign_id, check_alias(alias), now_or(now)),
        ).fetchone()
        if row is None:
            # The index refused it, which is the whole check: no read-then-write
            # window for a second caller to slip through.
            raise AliasTaken("that campaign already has an active participant with this alias")
        return _participant(row)

    def get(self, unit: UnitOfWork, participant_id: str) -> Participant | None:
        row = pg(unit).conn.execute(
            f"SELECT {_P_COLUMNS} FROM campaign.participants WHERE id = %s", (participant_id,)
        ).fetchone()
        return None if row is None else _participant(row)

    def list_for_campaign(
        self, unit: UnitOfWork, campaign_id: str, *, include_removed: bool = False
    ) -> list[Participant]:
        rows = pg(unit).conn.execute(
            f"SELECT {_P_COLUMNS} FROM campaign.participants "
            f"WHERE campaign_id = %s AND (%s OR removed_at IS NULL) ORDER BY created_at, id",
            (campaign_id, include_removed),
        ).fetchall()
        return [_participant(row) for row in rows]

    def remove(self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None) -> bool:
        changed = pg(unit).conn.execute(
            "UPDATE campaign.participants SET removed_at = %s "
            "WHERE id = %s AND removed_at IS NULL RETURNING id",
            (now_or(now), participant_id),
        ).fetchone()
        return changed is not None

    def _hold(self, unit: UnitOfWork, participant_id: str) -> None:
        """Hold the participant's row for the rest of this transaction, weakly
        enough that a row referencing it can still be inserted (RQ-3)."""
        transaction = pg(unit)
        transaction.note_row_lock()
        transaction.conn.execute(
            "SELECT id FROM campaign.participants WHERE id = %s FOR NO KEY UPDATE",
            (participant_id,),
        )

    def issue_code(
        self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None
    ) -> tuple[EnrolmentCode, str]:
        moment = now_or(now)
        self._hold(unit, participant_id)
        pg(unit).conn.execute(
            "UPDATE campaign.enrolment_codes SET revoked_at = %s WHERE participant_id = %s "
            "AND consumed_at IS NULL AND revoked_at IS NULL",
            (moment, participant_id),
        )
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.enrolment_codes "
            f"(id, participant_id, code_digest, expires_at, created_at) "
            f"VALUES (%s, %s, %s, %s, %s) RETURNING {_C_COLUMNS}",
            (
                ident.new_id(ident.ENROLMENT_CODE),
                participant_id,
                minted.digest,
                ident.code_expiry(moment),
                moment,
            ),
        ).fetchone()
        return _code(row), minted.secret

    def find_code(self, unit: UnitOfWork, digest: str) -> EnrolmentCode | None:
        row = pg(unit).conn.execute(
            f"SELECT {_C_COLUMNS} FROM campaign.enrolment_codes WHERE code_digest = %s", (digest,)
        ).fetchone()
        return None if row is None else _code(row)

    def consume_code(
        self, unit: UnitOfWork, participant_id: str, digest: str, *, now: datetime | None = None
    ) -> bool:
        moment = now_or(now)
        spent = pg(unit).conn.execute(
            "UPDATE campaign.enrolment_codes SET consumed_at = %s "
            "WHERE participant_id = %s AND code_digest = %s "
            "AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > %s RETURNING id",
            (moment, participant_id, digest, moment),
        ).fetchone()
        return spent is not None

    def codes(self, unit: UnitOfWork, participant_id: str) -> list[EnrolmentCode]:
        rows = pg(unit).conn.execute(
            f"SELECT {_C_COLUMNS} FROM campaign.enrolment_codes "
            f"WHERE participant_id = %s ORDER BY created_at, id",
            (participant_id,),
        ).fetchall()
        return [_code(row) for row in rows]

    def issue_device_credential(
        self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None
    ) -> tuple[DeviceCredential, str]:
        moment = now_or(now)
        self._hold(unit, participant_id)
        pg(unit).conn.execute(
            "UPDATE campaign.device_credentials SET revoked_at = %s "
            "WHERE participant_id = %s AND revoked_at IS NULL",
            (moment, participant_id),
        )
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.device_credentials "
            f"(id, participant_id, credential_digest, created_at) "
            f"VALUES (%s, %s, %s, %s) RETURNING {_D_COLUMNS}",
            (ident.new_id(ident.DEVICE_CREDENTIAL), participant_id, minted.digest, moment),
        ).fetchone()
        return _credential(row), minted.secret

    def find_device_credential(self, unit: UnitOfWork, digest: str) -> DeviceCredential | None:
        row = pg(unit).conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.device_credentials WHERE credential_digest = %s",
            (digest,),
        ).fetchone()
        return None if row is None else _credential(row)

    def device_credentials(self, unit: UnitOfWork, participant_id: str) -> list[DeviceCredential]:
        rows = pg(unit).conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.device_credentials "
            f"WHERE participant_id = %s ORDER BY created_at, id",
            (participant_id,),
        ).fetchall()
        return [_credential(row) for row in rows]


class InMemoryParticipantStore:
    """The twin. It enforces every rule the parametrised suite asserts — the
    alias index, single-use codes, one live code and one active device — because
    a rule the fake is not obliged to keep is a rule it will drift on. What it
    does not model is conflict: its transactions are serial, so the racing
    consumption of one code is proved against PostgreSQL instead."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._participants = Staging()
        self._codes = Staging()
        self._credentials = Staging()

    def add(
        self, unit: UnitOfWork, campaign_id: str, *, alias: str, now: datetime | None = None
    ) -> Participant:
        twin = fake(unit)
        check_alias(alias)
        taken = {
            p.alias.lower()
            for p in self._participants.visible(twin).values()
            if p.campaign_id == campaign_id and p.is_active
        }
        if alias.lower() in taken:
            raise AliasTaken("that campaign already has an active participant with this alias")
        participant = Participant(
            id=ident.new_id(ident.PARTICIPANT),
            campaign_id=campaign_id,
            alias=alias,
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

    def remove(self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None) -> bool:
        twin = fake(unit)
        found = self._participants.visible(twin).get(participant_id)
        if found is None or not found.is_active:
            return False
        self._participants.replace(
            twin,
            participant_id,
            Participant(
                id=found.id,
                campaign_id=found.campaign_id,
                alias=found.alias,
                created_at=found.created_at,
                removed_at=now_or(now),
            ),
        )
        return True

    def _revoke_live_codes(self, twin: Any, participant_id: str, moment: datetime) -> None:
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

    def issue_code(
        self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None
    ) -> tuple[EnrolmentCode, str]:
        twin = fake(unit)
        twin.note_row_lock()
        moment = now_or(now)
        self._revoke_live_codes(twin, participant_id, moment)
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
        self, unit: UnitOfWork, participant_id: str, digest: str, *, now: datetime | None = None
    ) -> bool:
        twin = fake(unit)
        moment = now_or(now)
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
        self, unit: UnitOfWork, participant_id: str, *, now: datetime | None = None
    ) -> tuple[DeviceCredential, str]:
        twin = fake(unit)
        twin.note_row_lock()
        moment = now_or(now)
        for held in list(self._credentials.visible(twin).values()):
            if held.participant_id == participant_id and held.is_active:
                self._credentials.replace(
                    twin, held.id, DeviceCredential(
                        held.id, held.participant_id, held.credential_digest,
                        held.created_at, moment, held.last_seen_at,
                    )
                )
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

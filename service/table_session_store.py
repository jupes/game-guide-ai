"""Live table sessions, their link generations and their credentials (1kg.2.1).

A table session is a GM running their table right now: the thing a player's
link joins, the thing a reveal is displayed into, and the thing that ends. Four
rules from the records shape this module.

**One live session per GM, across campaigns** (REVEAL-2). Not one per campaign:
a GM is one person at one table, and two live sessions would mean two displays
claiming the same authority. PostgreSQL refuses the second with a partial unique
index on `gm_user_id WHERE state = 'live'`, which also settles two racing
starts; the twin refuses it in Python, so the two cannot disagree.

**A link generation binds every credential made from it** (SEC-9). Rotating the
link and ending the session both close a generation, and *either one* "revokes
every table credential of the old generation in the same transaction that
advances the reveal and audio epochs". That is why `end` and `rotate_link` are
not two independent writes: they take the session row's lock once, advance the
epochs once, and revoke that generation's credentials inside the same unit of
work, so no credential can outlive the epoch that retired it.

**`narrow` is the primitive** (RQ-7). Anything that makes what the table can see
smaller — End, expiry, Rotate, Remove participant, Reset personal link, unlink —
goes through it: hold the session row `FOR NO KEY UPDATE`, bound the
transaction, advance the reveal epoch (and the audio epoch when the caller asks),
and call the slot-clearing extension point. That point is **empty in this bead**;
`1kg.7.1` fills it when slots exist. Crucially, narrowing needs no lock on
`authz_state`, so a Stop never waits behind an authorisation check.

**Every explicit row lock is `FOR NO KEY UPDATE`** (RQ-3), for the reason
`service/participant_store.py` spells out.

What is **not** here: the window arithmetic over `campaign.session_join_counters`
and the credential bound of SEC-10. Migration 0004 carries that storage because
pinning it later would cost another migration, but the rules that read it belong
to `1kg.2.3`, which is also the bead that will write the first row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from . import campaign_identity as ident
from .campaign_store import LiveSessionExists, NotLive, Staging, fake, now_or, pg
from .db import CampaignLockSettings, UnitOfWork

#: The three states of `campaign.table_sessions.state`, as 0004's CHECK has them.
LIVE = "live"
ENDED = "ended"
EXPIRED = "expired"

SlotClear = Callable[[UnitOfWork, str], None]


def no_slots(unit: UnitOfWork, session_id: str) -> None:
    """The slot-clearing extension point, empty in this bead.

    `narrow` calls it inside the transaction that advances the epochs, holding
    the session row, so that when `1kg.7.1` gives it a body the slots it clears
    and the epoch that retires them cannot come apart. Until then a narrowing has
    nothing to clear, and this says so in one place rather than by omission.
    """
    return None


@dataclass(frozen=True)
class TableSession:
    """One GM running one table. `link_digest` is hidden from `repr()`: it is the
    lookup key for a live join credential."""

    id: str
    campaign_id: str
    gm_user_id: int
    state: str
    started_at: datetime
    expires_at: datetime
    link_generation: int
    link_digest: str | None = field(repr=False, default=None)
    reveal_epoch: int = 0
    audio_epoch: int = 0
    table_audio: bool = True
    ended_at: datetime | None = None

    @property
    def is_live(self) -> bool:
        return self.state == LIVE

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now


@dataclass(frozen=True)
class TableCredential:
    """One joined device's credential, bound to the generation it was made in."""

    id: str
    session_id: str
    link_generation: int
    credential_digest: str = field(repr=False)
    created_at: datetime
    revoked_at: datetime | None = None
    last_connected_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class TableSessionStore(Protocol):
    """Sessions, their generations and the credentials made from them."""

    def start(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        gm_user_id: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> tuple[TableSession, str]:
        """Open a session and mint its link token. Raises `LiveSessionExists` if
        that GM already has a live one, in this campaign or any other."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        ...  # pragma: no cover - structural type

    def narrow(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        audio: bool = False,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        """Make what the table can see smaller: hold the session row, advance the
        reveal epoch — and the audio epoch when `audio` — and clear the slots.
        Returns the narrowed session, or None if there is no such session."""
        ...  # pragma: no cover - structural type

    def end(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        """End a live session: narrow it, revoke every credential of its current
        generation, and stamp `ended_at` — one unit of work (SEC-9). Ending a
        session that is already ended or expired changes nothing and is not an
        error; it returns the session as it stands."""
        ...  # pragma: no cover - structural type

    def rotate_link(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> tuple[TableSession, str] | None:
        """Retire the current link generation and mint the next one: narrow the
        session, revoke every credential of the superseded generation and replace
        the link digest — one unit of work (SEC-9). The session stays live, and
        its divider and recap boundary do not move (REVEAL-17). Returns None if
        there is no such session, and refuses a session that is not live."""
        ...  # pragma: no cover - structural type

    def expired_live_sessions_for_gm(
        self, unit: UnitOfWork, gm_user_id: int, *, now: datetime | None = None
    ) -> list[TableSession]:
        """That GM's sessions past `expires_at` that are still `live`, so a start
        can end them first rather than being refused by the index (RQ-7)."""
        ...  # pragma: no cover - structural type

    def issue_credential(
        self, unit: UnitOfWork, session_id: str, *, now: datetime | None = None
    ) -> tuple[TableCredential, str]:
        """Mint a joined device's credential in the session's current
        generation. Returns the record and the credential itself."""
        ...  # pragma: no cover - structural type

    def credentials(self, unit: UnitOfWork, session_id: str) -> list[TableCredential]:
        """Every credential ever made for that session, oldest first."""
        ...  # pragma: no cover - structural type


_S_COLUMNS = (
    "id, campaign_id, gm_user_id, state, started_at, expires_at, link_generation, "
    "link_digest, reveal_epoch, audio_epoch, table_audio, ended_at"
)
_T_COLUMNS = (
    "id, session_id, link_generation, credential_digest, created_at, revoked_at, last_connected_at"
)


def _session(row: tuple) -> TableSession:
    return TableSession(
        row[0], row[1], int(row[2]), row[3], row[4], row[5], int(row[6]),
        row[7], int(row[8]), int(row[9]), bool(row[10]), row[11],
    )


def _credential(row: tuple) -> TableCredential:
    return TableCredential(row[0], row[1], int(row[2]), row[3], row[4], row[5], row[6])


class PostgresTableSessionStore:
    """`campaign.table_sessions` and `campaign.table_credentials` (migration 0004)."""

    def __init__(
        self,
        *,
        slot_clear: SlotClear = no_slots,
        settings: CampaignLockSettings | None = None,
    ) -> None:
        self._slot_clear = slot_clear
        self._settings = settings if settings is not None else CampaignLockSettings()

    def start(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        gm_user_id: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> tuple[TableSession, str]:
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.table_sessions "
            f"(id, campaign_id, gm_user_id, state, started_at, expires_at, link_digest) "
            f"VALUES (%s, %s, %s, 'live', %s, %s, %s) "
            f"ON CONFLICT (gm_user_id) WHERE state = 'live' DO NOTHING RETURNING {_S_COLUMNS}",
            (
                ident.new_id(ident.TABLE_SESSION),
                campaign_id,
                gm_user_id,
                now_or(now),
                expires_at,
                minted.digest,
            ),
        ).fetchone()
        if row is None:
            raise LiveSessionExists("that GM already has a live table session")
        return _session(row), minted.secret

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions WHERE id = %s", (session_id,)
        ).fetchone()
        return None if row is None else _session(row)

    def _hold(
        self, unit: UnitOfWork, session_id: str, transaction_timeout_s: float | None
    ) -> TableSession | None:
        """The session row, held for the rest of this transaction, with the
        transaction bounded first so that a narrowing cannot become the thing
        everyone else waits behind."""
        transaction = pg(unit)
        transaction.note_row_lock()
        bound = (
            self._settings.transaction_timeout
            if transaction_timeout_s is None
            else f"{transaction_timeout_s}s"
        )
        transaction.conn.execute("SELECT set_config('transaction_timeout', %s, true)", (bound,))
        row = transaction.conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions WHERE id = %s FOR NO KEY UPDATE",
            (session_id,),
        ).fetchone()
        return None if row is None else _session(row)

    def _advance(self, unit: UnitOfWork, session: TableSession, *, audio: bool) -> TableSession:
        row = pg(unit).conn.execute(
            f"UPDATE campaign.table_sessions "
            f"SET reveal_epoch = reveal_epoch + 1, audio_epoch = audio_epoch + %s "
            f"WHERE id = %s RETURNING {_S_COLUMNS}",
            (1 if audio else 0, session.id),
        ).fetchone()
        self._slot_clear(unit, session.id)
        return _session(row)

    def narrow(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        audio: bool = False,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        held = self._hold(unit, session_id, transaction_timeout_s)
        return None if held is None else self._advance(unit, held, audio=audio)

    def _revoke_generation(
        self, unit: UnitOfWork, session_id: str, generation: int, moment: datetime
    ) -> None:
        pg(unit).conn.execute(
            "UPDATE campaign.table_credentials SET revoked_at = %s "
            "WHERE session_id = %s AND link_generation = %s AND revoked_at IS NULL",
            (moment, session_id, generation),
        )

    def end(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        held = self._hold(unit, session_id, transaction_timeout_s)
        if held is None or not held.is_live:
            return held
        moment = now_or(now)
        narrowed = self._advance(unit, held, audio=True)
        self._revoke_generation(unit, session_id, narrowed.link_generation, moment)
        row = pg(unit).conn.execute(
            f"UPDATE campaign.table_sessions SET state = %s, ended_at = %s, link_digest = NULL "
            f"WHERE id = %s RETURNING {_S_COLUMNS}",
            (ENDED, moment, session_id),
        ).fetchone()
        return _session(row)

    def rotate_link(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> tuple[TableSession, str] | None:
        held = self._hold(unit, session_id, transaction_timeout_s)
        if held is None:
            return None
        if not held.is_live:
            raise NotLive("only a live session's link can be rotated")
        moment = now_or(now)
        narrowed = self._advance(unit, held, audio=True)
        self._revoke_generation(unit, session_id, narrowed.link_generation, moment)
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"UPDATE campaign.table_sessions "
            f"SET link_generation = link_generation + 1, link_digest = %s "
            f"WHERE id = %s RETURNING {_S_COLUMNS}",
            (minted.digest, session_id),
        ).fetchone()
        return _session(row), minted.secret

    def expired_live_sessions_for_gm(
        self, unit: UnitOfWork, gm_user_id: int, *, now: datetime | None = None
    ) -> list[TableSession]:
        rows = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions "
            f"WHERE gm_user_id = %s AND state = 'live' AND expires_at <= %s "
            f"ORDER BY expires_at, id",
            (gm_user_id, now_or(now)),
        ).fetchall()
        return [_session(row) for row in rows]

    def issue_credential(
        self, unit: UnitOfWork, session_id: str, *, now: datetime | None = None
    ) -> tuple[TableCredential, str]:
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.table_credentials "
            f"(id, session_id, link_generation, credential_digest, created_at) "
            f"SELECT %s, id, link_generation, %s, %s FROM campaign.table_sessions WHERE id = %s "
            f"RETURNING {_T_COLUMNS}",
            (ident.new_id(ident.TABLE_CREDENTIAL), minted.digest, now_or(now), session_id),
        ).fetchone()
        if row is None:
            raise LookupError("no such table session")
        return _credential(row), minted.secret

    def credentials(self, unit: UnitOfWork, session_id: str) -> list[TableCredential]:
        rows = pg(unit).conn.execute(
            f"SELECT {_T_COLUMNS} FROM campaign.table_credentials "
            f"WHERE session_id = %s ORDER BY created_at, id",
            (session_id,),
        ).fetchall()
        return [_credential(row) for row in rows]


class InMemoryTableSessionStore:
    """The twin. It refuses a second live session for the same GM itself rather
    than leaving that to PostgreSQL's partial index (REVEAL-2), because a rule
    the fake is not obliged to keep is a rule it will drift on. Two racing
    starts are a different question, and are proved against the database."""

    def __init__(self, db: Any, *, slot_clear: SlotClear = no_slots) -> None:
        self._db = db
        self._slot_clear = slot_clear
        self._sessions = Staging()
        self._credentials = Staging()

    def start(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        gm_user_id: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> tuple[TableSession, str]:
        twin = fake(unit)
        running = [
            s
            for s in self._sessions.visible(twin).values()
            if s.gm_user_id == gm_user_id and s.is_live
        ]
        if running:
            raise LiveSessionExists("that GM already has a live table session")
        minted = ident.new_secret()
        session = TableSession(
            id=ident.new_id(ident.TABLE_SESSION),
            campaign_id=campaign_id,
            gm_user_id=gm_user_id,
            state=LIVE,
            started_at=now_or(now),
            expires_at=expires_at,
            link_generation=1,
            link_digest=minted.digest,
        )
        self._sessions.add(twin, session.id, session)
        return session, minted.secret

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        return self._sessions.visible(fake(unit)).get(session_id)

    def _hold(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        twin = fake(unit)
        twin.note_row_lock()
        return self._sessions.visible(twin).get(session_id)

    def _replace(self, unit: UnitOfWork, session: TableSession, **changes: Any) -> TableSession:
        updated = TableSession(
            id=session.id,
            campaign_id=session.campaign_id,
            gm_user_id=session.gm_user_id,
            state=changes.get("state", session.state),
            started_at=session.started_at,
            expires_at=session.expires_at,
            link_generation=changes.get("link_generation", session.link_generation),
            link_digest=changes.get("link_digest", session.link_digest),
            reveal_epoch=changes.get("reveal_epoch", session.reveal_epoch),
            audio_epoch=changes.get("audio_epoch", session.audio_epoch),
            table_audio=changes.get("table_audio", session.table_audio),
            ended_at=changes.get("ended_at", session.ended_at),
        )
        self._sessions.replace(fake(unit), session.id, updated)
        return updated

    def _advance(self, unit: UnitOfWork, session: TableSession, *, audio: bool) -> TableSession:
        advanced = self._replace(
            unit,
            session,
            reveal_epoch=session.reveal_epoch + 1,
            audio_epoch=session.audio_epoch + (1 if audio else 0),
        )
        self._slot_clear(unit, session.id)
        return advanced

    def narrow(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        audio: bool = False,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        held = self._hold(unit, session_id)
        return None if held is None else self._advance(unit, held, audio=audio)

    def _revoke_generation(
        self, unit: UnitOfWork, session_id: str, generation: int, moment: datetime
    ) -> None:
        twin = fake(unit)
        for held in list(self._credentials.visible(twin).values()):
            if held.session_id == session_id and held.link_generation == generation and held.is_active:
                self._credentials.replace(
                    twin, held.id, TableCredential(
                        held.id, held.session_id, held.link_generation, held.credential_digest,
                        held.created_at, moment, held.last_connected_at,
                    )
                )

    def end(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        held = self._hold(unit, session_id)
        if held is None or not held.is_live:
            return held
        moment = now_or(now)
        narrowed = self._advance(unit, held, audio=True)
        self._revoke_generation(unit, session_id, narrowed.link_generation, moment)
        return self._replace(unit, narrowed, state=ENDED, ended_at=moment, link_digest=None)

    def rotate_link(
        self,
        unit: UnitOfWork,
        session_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> tuple[TableSession, str] | None:
        held = self._hold(unit, session_id)
        if held is None:
            return None
        if not held.is_live:
            raise NotLive("only a live session's link can be rotated")
        moment = now_or(now)
        narrowed = self._advance(unit, held, audio=True)
        self._revoke_generation(unit, session_id, narrowed.link_generation, moment)
        minted = ident.new_secret()
        rotated = self._replace(
            unit,
            narrowed,
            link_generation=narrowed.link_generation + 1,
            link_digest=minted.digest,
        )
        return rotated, minted.secret

    def expired_live_sessions_for_gm(
        self, unit: UnitOfWork, gm_user_id: int, *, now: datetime | None = None
    ) -> list[TableSession]:
        moment = now_or(now)
        stale = [
            s
            for s in self._sessions.visible(fake(unit)).values()
            if s.gm_user_id == gm_user_id and s.is_live and s.is_expired(moment)
        ]
        return sorted(stale, key=lambda s: (s.expires_at, s.id))

    def issue_credential(
        self, unit: UnitOfWork, session_id: str, *, now: datetime | None = None
    ) -> tuple[TableCredential, str]:
        twin = fake(unit)
        session = self._sessions.visible(twin).get(session_id)
        if session is None:
            raise LookupError("no such table session")
        minted = ident.new_secret()
        credential = TableCredential(
            id=ident.new_id(ident.TABLE_CREDENTIAL),
            session_id=session_id,
            link_generation=session.link_generation,
            credential_digest=minted.digest,
            created_at=now_or(now),
        )
        self._credentials.add(twin, credential.id, credential)
        return credential, minted.secret

    def credentials(self, unit: UnitOfWork, session_id: str) -> list[TableCredential]:
        mine = [
            c for c in self._credentials.visible(fake(unit)).values() if c.session_id == session_id
        ]
        return sorted(mine, key=lambda c: (c.created_at, c.id))

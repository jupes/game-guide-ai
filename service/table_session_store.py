"""Live table sessions, their admission generations and their screen grants
(1kg.2.1, 1kg.2.3).

A table session is a GM running their table right now: the thing a reveal is
displayed into, and the thing that ends. There is **no link and no join** any
more (threat model section 15): a player is an account, and the only bearer
secret left in the table model is the **screen grant** — a row of
`campaign.table_credentials` the owner mints on the browser they are signed in
on, which that same answer signs out (SEC-48, D-13). Six rules shape this module.

**One live session per GM, across campaigns** (REVEAL-2). Not one per campaign:
a GM is one person at one table, and two live sessions would mean two displays
claiming the same authority. PostgreSQL refuses the second with a partial unique
index on `gm_user_id WHERE state = 'live'`, which also settles two racing
starts; the twin refuses it in Python, so the two cannot disagree. A start
records the client's `command_id` (`start_command_id`, migration 0015), and the
twin keeps that partial unique index too (`StartReplayed`).

**The GM is the campaign's owner** (AUD-1, `docs/migrations.md` section 4).
`start` selects the session's GM out of `campaign.campaigns` in the statement
that inserts the row, so a campaign that is not the caller's, or is archived, is
simply "no row". End and Rotate name **the owner** in the statement that locks
the session row (`gm_user_id = %s ... FOR NO KEY UPDATE`), so a stranger naming
another GM's session locks nothing and learns nothing. The system's `expire`
names no owner, and so acts only on a session already past `expires_at`
(`NotDue`): it can never be an End that skipped the owner check. `yje.2.3`'s
account transitions end a GM's sessions through the owner-scoped `end` (the GM
is the owner), with the system as the audit row's actor.

**`link_generation` is the admission generation** (SEC-42, "renamed here in
prose only"). Every grant carries the generation it was minted in. End, expiry
and Rotate are revocations: each holds the session row, advances both epochs and
clears the slots (`narrow`'s path), revokes **every** unrevoked screen grant of
the session whatever its generation, and changes the state or the generation —
one unit of work. The multi-row revoke locks the grant rows in ascending id
(`SELECT ... ORDER BY id FOR NO KEY UPDATE`), so that a later path that does not
hold the session row (`1kg.2.6`'s sweep) cannot deadlock with it if it takes the
same order; nothing here revokes more than one grant without holding its session.

**A grant is valid only by the reader's three-part test** (L-10): unrevoked,
its session live (`state = 'live' AND expires_at > now`), and its generation the
session's current one. `revoked_at IS NULL` is never the test. A mint inserts
through `INSERT ... SELECT` that re-checks the owner, the state and the expiry
and reads the generation in the same statement; its foreign-key check takes
`FOR KEY SHARE` on the session row, which does not conflict with an End's or a
Rotate's `FOR NO KEY UPDATE` — so **a mint never makes a narrowing wait, and a
narrowing never waits for a mint** (RQ-3). The price is that a mint which loses
that race can commit a grant of a retired generation or of an ended session:
dead on arrival, because the reader's test says so. That is fail-closed — the
owner's browser was signed out holding a dead grant. The clock is always the
application's, injected and passed as a parameter, never `now()` in SQL, so the
twin and PostgreSQL agree and a test controls time.

**At most `SCREENS_PER_SESSION` live screens** (SEC-48). The count and the insert
are made under the session's advisory lock (`AdvisoryLock.TABLE_SESSION`), taken
alone — never with the campaign lock or a row hold in the same transaction — and
only after an owner-scoped read has shown the session is the caller's, so a
stranger never takes another GM's advisory lock.

**`narrow` is the primitive** (RQ-7), unchanged: hold the session row
`FOR NO KEY UPDATE`, bound the transaction, advance the reveal epoch (and the
audio epoch when asked), and call the slot-clearing extension point, which is
**empty until `1kg.7.1`** fills it and is a **required** argument so that nothing
built later can silently go on clearing nothing. Every explicit row lock is
`FOR NO KEY UPDATE` (RQ-3), and none of this ever asks for the campaign lock.

What later beads add here is one more conjunct each (L-18): `yje.2.1`'s Verified
owner joins `live_owned`'s and `resolve_screen`'s queries, and `1kg.7.5`'s
`NOTIFY` wake-up joins each write.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import NoReturn, Protocol

from . import campaign_identity as ident
from .campaign_store import (
    Campaign,
    LiveSessionExists,
    MissingParent,
    NotDue,
    NotLive,
    ScreenLimit,
    Staging,
    StartReplayed,
    aware,
    fake,
    now_or,
    pg,
    shared_rows,
)
from .db import AdvisoryLock, InMemoryDatabase, InMemoryTransaction, UnitOfWork

#: The three states of `campaign.table_sessions.state`, as 0004's CHECK has them.
LIVE = "live"
ENDED = "ended"
EXPIRED = "expired"
#: What a Rotate did, beside the two states an ending writes (`Closing.outcome`).
ROTATED = "rotated"

#: SEC-48's suggested bound on a session's live screen grants. The wire
#: contract's `screens` list allows more, so tuning this is not a contract change.
SCREENS_PER_SESSION = 4

#: The wire contract's `CommandId`, which 0015's two CHECKs spell too.
COMMAND_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

SlotClear = Callable[[UnitOfWork, str], None]


def no_slots(unit: UnitOfWork, session_id: str) -> None:
    """The slot-clearing extension point, empty in this bead.

    `narrow` calls it inside the transaction that advances the epochs, holding
    the session row, so that when `1kg.7.1` gives it a body the slots it clears
    and the epoch that retires them cannot come apart. Until then a narrowing has
    nothing to clear, and this says so in one place rather than by omission.
    Every construction site passes it by name — see the module docstring.
    """
    return None


def check_expiry(started_at: datetime, expires_at: datetime) -> datetime:
    """The bound `0004_campaign_schema.sql` carries (`expires_at > started_at`),
    applied before the statement so that the caller gets this refusal rather
    than an integrity error naming a constraint."""
    aware(expires_at, "an expiry")
    if expires_at <= started_at:
        raise ValueError("a table session expires after it starts")
    return expires_at


def check_command_id(command_id: str) -> str:
    """The shape 0015's CHECK carries, in both worlds. The refusal does not
    repeat the value: a caller's string is not something to echo."""
    if not isinstance(command_id, str) or COMMAND_ID.fullmatch(command_id) is None:
        raise ValueError("a command id is 16 to 64 URL-safe characters")
    return command_id


@dataclass(frozen=True)
class TableSession:
    """One GM running one table. `link_generation` is its admission generation."""

    id: str
    campaign_id: str
    gm_user_id: int
    state: str
    started_at: datetime
    expires_at: datetime
    link_generation: int
    reveal_epoch: int = 0
    audio_epoch: int = 0
    table_audio: bool = True
    ended_at: datetime | None = None
    start_command_id: str | None = None
    rotate_command_id: str | None = None

    @property
    def is_live(self) -> bool:
        """The row's state. **Not** the test of whether it serves: a row that is
        `live` past `expires_at` is dead — see `live_at`."""
        return self.state == LIVE

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    def live_at(self, now: datetime) -> bool:
        """Live and unexpired at `now` — the one definition of a live session."""
        return self.is_live and not self.is_expired(now)


@dataclass(frozen=True)
class ScreenGrant:
    """One browser the owner made a table screen, bound to the admission
    generation it was minted in. The digest is hidden from `repr()`: it is the
    lookup key for a live grant. There is no `is_active`: whether a grant is live
    is the reader's three-part test, never this row alone."""

    id: str
    session_id: str
    link_generation: int
    credential_digest: str = field(repr=False)
    created_at: datetime
    revoked_at: datetime | None = None
    last_connected_at: datetime | None = None


@dataclass(frozen=True)
class LiveScreen:
    """What a live grant resolves to: the grant, its session, that session's
    campaign and the generation it was minted in — never a participant or an
    account."""

    grant_id: str
    session_id: str
    campaign_id: str
    generation: int


@dataclass(frozen=True)
class Liveness:
    """One session's facts for the per-frame check (L-12): its state, its expiry,
    its current generation, and the ids of its unrevoked grants of that
    generation. Whether the session is live is the predicate's, against a clock."""

    session_id: str
    state: str
    expires_at: datetime
    generation: int
    live_screens: frozenset[str]


@dataclass(frozen=True)
class Closing:
    """What an End, an expiry or a Rotate did under the session row.

    `outcome` is `ENDED`, `EXPIRED` or `ROTATED`, or None when nothing was
    written — the session was not live, or the Rotate repeated the command that
    last rotated it — and the session is answered as it stands."""

    session: TableSession
    outcome: str | None
    screens_revoked: int = 0


class TableSessionStore(Protocol):
    """Sessions, their admission generations and their screen grants."""

    def start(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        expires_at: datetime,
        command_id: str,
        now: datetime | None = None,
    ) -> TableSession:
        """Open a session for the campaign's owner, recording the command that
        started it. The GM is the owner (AUD-1), read out of the campaigns table
        in the same statement, so `MissingParent` answers a campaign that does not
        exist, is not that owner's or is archived. Then, in both worlds and in
        this order: `StartReplayed` if that campaign already has a session of
        this command, and `LiveSessionExists` if that GM already has a live
        session, in this campaign or any other. No secret is minted."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        ...  # pragma: no cover - structural type

    def find_owned(
        self, unit: UnitOfWork, campaign_id: str, session_id: str, *, owner_id: int
    ) -> TableSession | None:
        """That session, if it is in that campaign and that owner's — unlocked.
        Rotate's pre-read: ownership is decided before anything is counted."""
        ...  # pragma: no cover - structural type

    def by_start_command(
        self, unit: UnitOfWork, campaign_id: str, command_id: str
    ) -> TableSession | None:
        """The session of that campaign a Start with this command opened, in
        whatever state it is now — a retried Start's answer."""
        ...  # pragma: no cover - structural type

    def latest_for_campaign(self, unit: UnitOfWork, campaign_id: str) -> TableSession | None:
        """The campaign's live session, else the one most recently started, else
        None. The caller has already shown the campaign is theirs."""
        ...  # pragma: no cover - structural type

    def live_owned(
        self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, now: datetime
    ) -> TableSession | None:
        """The campaign's session that is live and unexpired at `now` and that
        owner's — unlocked, one query. The mint's first step (L-9)."""
        ...  # pragma: no cover - structural type

    def narrow(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        audio: bool = False,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        """Make what the table can see smaller: hold the session row, advance the
        reveal epoch — and the audio epoch when `audio` — and clear the slots.
        Returns the narrowed session, or None if there is no such session in
        that campaign."""
        ...  # pragma: no cover - structural type

    def end(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        """End the owner's live session: hold the row (owner in the locking
        statement), advance both epochs and clear the slots, revoke every
        unrevoked grant, and stamp `ended_at` — one unit of work. A session
        already past `expires_at` is finalised as `expired`, `ended_at =
        expires_at`. A session that is not live is answered as it stands and
        nothing is written. None: no such session of that owner in that
        campaign."""
        ...  # pragma: no cover - structural type

    def rotate(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        command_id: str,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        """Retire the admission generation: under the owner-scoped row, a session
        that is not live, or whose last Rotate was this command, is answered as
        it stands; one past `expires_at` is finalised as an expiry; otherwise both
        epochs advance, every grant is revoked, and `link_generation + 1` and
        `rotate_command_id` are written in one UPDATE. The session stays live and
        its divider does not move (REVEAL-17). Never refused for state (X-3)."""
        ...  # pragma: no cover - structural type

    def expire(
        self,
        unit: UnitOfWork,
        session_id: str,
        campaign_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        """The system's path: finalise a session already past `expires_at` as
        `expired`, `ended_at = expires_at`. Holds the row by session and campaign,
        names no owner, and therefore refuses a session not yet due (`NotDue`)."""
        ...  # pragma: no cover - structural type

    def expired_live_sessions_for_gm(
        self, unit: UnitOfWork, gm_user_id: int, *, now: datetime | None = None
    ) -> list[TableSession]:
        """That GM's sessions past `expires_at` that are still `live`, so a start
        can finalise them first rather than being refused by the index (RQ-7)."""
        ...  # pragma: no cover - structural type

    def mint_screen(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
    ) -> tuple[ScreenGrant, str]:
        """Mint a screen grant in the session's current generation: an
        owner-scoped read, the session's advisory lock, the bound
        (`ScreenLimit`), then an insert that re-checks owner, state and expiry.
        `MissingParent` if the session is not that owner's in that campaign,
        `NotLive` if it is not live at `now`. Returns the record and the grant
        itself — the only moment the secret exists."""
        ...  # pragma: no cover - structural type

    def resolve_screen(self, unit: UnitOfWork, digest: str, *, now: datetime) -> LiveScreen | None:
        """The live grant this digest names — one indexed query, the reader's
        three-part test in its `WHERE`. An unknown digest and a dead grant are
        the same None."""
        ...  # pragma: no cover - structural type

    def screens(self, unit: UnitOfWork, session_id: str) -> list[ScreenGrant]:
        """Every grant ever minted for that session, oldest first."""
        ...  # pragma: no cover - structural type

    def live_screens(self, unit: UnitOfWork, session_id: str, *, now: datetime) -> list[ScreenGrant]:
        """That session's live grants at `now` — none unless the session is live."""
        ...  # pragma: no cover - structural type

    def revoke_screen(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        screen_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
    ) -> tuple[str, bool] | None:
        """The owner revokes one screen: one owner-scoped UPDATE that matches the
        grant whether or not it is already revoked. Returns its session id and
        whether this call revoked it, or None for another GM's grant or none."""
        ...  # pragma: no cover - structural type

    def leave(self, unit: UnitOfWork, digest: str, *, now: datetime) -> LiveScreen | None:
        """A screen leaves: one UPDATE by digest that revokes the grant only if it
        is live. Returns what it revoked, or None when there was nothing live."""
        ...  # pragma: no cover - structural type

    def liveness(self, unit: UnitOfWork, session_ids: Collection[str]) -> dict[str, Liveness]:
        """Many sessions' liveness facts in one query, keyed by session id; a
        session that does not exist is absent."""
        ...  # pragma: no cover - structural type

    def live_session_for_campaign(self, unit: UnitOfWork, campaign_id: str) -> TableSession | None:
        """That campaign's session whose state is `live`, **whether or not it has
        passed `expires_at`**, or None — read without a lock (bead 1kg.2.2, L-12).
        A narrowing (archive, Remove) finds what to narrow with it; narrowing an
        expired row is harmless and never wrong (SEC-42 makes every reader
        compare `expires_at` with the clock anyway)."""
        ...  # pragma: no cover - structural type


_S_COLUMNS = (
    "id, campaign_id, gm_user_id, state, started_at, expires_at, link_generation, "
    "reveal_epoch, audio_epoch, table_audio, ended_at, start_command_id, rotate_command_id"
)
_G_COLUMNS = (
    "id, session_id, link_generation, credential_digest, created_at, revoked_at, last_connected_at"
)
#: The reader's three-part test, as one `WHERE` fragment over `g` (a grant) and
#: `s` (its session). Its one parameter is the clock, which every statement
#: passes itself: never `now()` in SQL (L-10).
_LIVE_GRANT = (
    "g.revoked_at IS NULL AND s.state = 'live' AND s.expires_at > %s "
    "AND g.link_generation = s.link_generation"
)


def _session(row: tuple) -> TableSession:
    return TableSession(
        row[0], row[1], int(row[2]), row[3], row[4], row[5], int(row[6]),
        int(row[7]), int(row[8]), bool(row[9]), row[10], row[11], row[12],
    )


def _grant(row: tuple) -> ScreenGrant:
    return ScreenGrant(row[0], row[1], int(row[2]), row[3], row[4], row[5], row[6])


class PostgresTableSessionStore:
    """`campaign.table_sessions` and `campaign.table_credentials` (0004, 0015).

    It keeps no settings of its own: the two bounds come from the unit of work's
    `CampaignLockSettings`, which is the one a `Database` was built with, so a
    deployment cannot end up with two different answers to "how long may a
    narrowing hold the session row".
    """

    def __init__(self, *, slot_clear: SlotClear) -> None:
        self._slot_clear = slot_clear

    def start(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        expires_at: datetime,
        command_id: str,
        now: datetime | None = None,
    ) -> TableSession:
        moment = now_or(now)
        check_expiry(moment, expires_at)
        check_command_id(command_id)
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.table_sessions "
            f"(id, campaign_id, gm_user_id, state, started_at, expires_at, start_command_id) "
            f"SELECT %s, c.id, c.owner_id, 'live', %s, %s, %s FROM campaign.campaigns c "
            f"WHERE c.id = %s AND c.owner_id = %s AND c.archived_at IS NULL "
            f"AND NOT EXISTS (SELECT 1 FROM campaign.table_sessions t "
            f"WHERE t.campaign_id = c.id AND t.start_command_id = %s) "
            f"ON CONFLICT (gm_user_id) WHERE state = 'live' DO NOTHING RETURNING {_S_COLUMNS}",
            (
                ident.new_id(ident.TABLE_SESSION),
                moment,
                expires_at,
                command_id,
                campaign_id,
                owner_id,
                command_id,
            ),
        ).fetchone()
        if row is None:
            self._refuse_start(unit, campaign_id, owner_id, command_id)
        return _session(row)

    def _refuse_start(
        self, unit: UnitOfWork, campaign_id: str, owner_id: int, command_id: str
    ) -> NoReturn:
        """Which condition refused it, asked in the twin's order so the two
        cannot answer differently when more than one holds."""
        conn = pg(unit).conn
        theirs = conn.execute(
            "SELECT 1 FROM campaign.campaigns "
            "WHERE id = %s AND owner_id = %s AND archived_at IS NULL",
            (campaign_id, owner_id),
        ).fetchone()
        if theirs is None:
            raise MissingParent("no such campaign for that owner")
        if self.by_start_command(unit, campaign_id, command_id) is not None:
            raise StartReplayed("that campaign already has a session of that command")
        raise LiveSessionExists("that GM already has a live table session")

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions WHERE id = %s", (session_id,)
        ).fetchone()
        return None if row is None else _session(row)

    def find_owned(
        self, unit: UnitOfWork, campaign_id: str, session_id: str, *, owner_id: int
    ) -> TableSession | None:
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions "
            f"WHERE id = %s AND campaign_id = %s AND gm_user_id = %s",
            (session_id, campaign_id, owner_id),
        ).fetchone()
        return None if row is None else _session(row)

    def by_start_command(
        self, unit: UnitOfWork, campaign_id: str, command_id: str
    ) -> TableSession | None:
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions "
            f"WHERE campaign_id = %s AND start_command_id = %s",
            (campaign_id, command_id),
        ).fetchone()
        return None if row is None else _session(row)

    def latest_for_campaign(self, unit: UnitOfWork, campaign_id: str) -> TableSession | None:
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions WHERE campaign_id = %s "
            f"ORDER BY (state = 'live') DESC, started_at DESC, id COLLATE \"C\" DESC LIMIT 1",
            (campaign_id,),
        ).fetchone()
        return None if row is None else _session(row)

    def live_owned(
        self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, now: datetime
    ) -> TableSession | None:
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions "
            f"WHERE campaign_id = %s AND gm_user_id = %s AND state = 'live' AND expires_at > %s",
            (campaign_id, owner_id, aware(now, "a clock")),
        ).fetchone()
        return None if row is None else _session(row)

    def _hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        transaction_timeout_s: float | None,
        owner_id: int | None = None,
    ) -> TableSession | None:
        """The session row, held for the rest of this transaction, with the
        transaction bounded first so that a narrowing cannot become the thing
        everyone else waits behind. With `owner_id`, the owner is in the locking
        statement, so a session that is not theirs is not locked at all."""
        transaction = pg(unit)
        transaction.note_row_lock()
        transaction.conn.execute(
            "SELECT set_config('transaction_timeout', %s, true)",
            (transaction.transaction_bound(transaction_timeout_s),),
        )
        if owner_id is None:
            row = transaction.conn.execute(
                f"SELECT {_S_COLUMNS} FROM campaign.table_sessions "
                f"WHERE id = %s AND campaign_id = %s FOR NO KEY UPDATE",
                (session_id, campaign_id),
            ).fetchone()
        else:
            row = transaction.conn.execute(
                f"SELECT {_S_COLUMNS} FROM campaign.table_sessions "
                f"WHERE id = %s AND campaign_id = %s AND gm_user_id = %s FOR NO KEY UPDATE",
                (session_id, campaign_id, owner_id),
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
        campaign_id: str,
        session_id: str,
        *,
        audio: bool = False,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s)
        return None if held is None else self._advance(unit, held, audio=audio)

    def _revoke_every_grant(self, unit: UnitOfWork, session_id: str, moment: datetime) -> int:
        """Every unrevoked grant of the session, whatever its generation — a mint
        that lost a race may have left one of a retired generation. The rows are
        locked in ascending id before they are written, so any other multi-row
        revoke that takes the same order cannot deadlock with this one. This
        leaves nothing unrevoked **behind it**; a mint whose statement took its
        snapshot before this commits may still add one, which is why validity
        is the reader's test."""
        conn = pg(unit).conn
        ids = [
            row[0]
            for row in conn.execute(
                'SELECT id FROM campaign.table_credentials '
                'WHERE session_id = %s AND revoked_at IS NULL '
                'ORDER BY id COLLATE "C" FOR NO KEY UPDATE',
                (session_id,),
            ).fetchall()
        ]
        if ids:
            conn.execute(
                "UPDATE campaign.table_credentials SET revoked_at = %s WHERE id = ANY(%s)",
                (moment, ids),
            )
        return len(ids)

    def _close(
        self, unit: UnitOfWork, held: TableSession, state: str, ended_at: datetime, moment: datetime
    ) -> Closing:
        """An ending, in RQ-5's order under the held row: the epochs and the
        slots, then every grant, then the state."""
        self._advance(unit, held, audio=True)
        revoked = self._revoke_every_grant(unit, held.id, moment)
        row = pg(unit).conn.execute(
            f"UPDATE campaign.table_sessions SET state = %s, ended_at = %s "
            f"WHERE id = %s RETURNING {_S_COLUMNS}",
            (state, ended_at, held.id),
        ).fetchone()
        return Closing(_session(row), state, revoked)

    def end(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s, owner_id)
        if held is None:
            return None
        if not held.is_live:
            return Closing(held, None)
        moment = now_or(now)
        if held.is_expired(moment):
            return self._close(unit, held, EXPIRED, held.expires_at, moment)
        return self._close(unit, held, ENDED, moment, moment)

    def rotate(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        command_id: str,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        check_command_id(command_id)
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s, owner_id)
        if held is None:
            return None
        if not held.is_live:
            return Closing(held, None)
        moment = now_or(now)
        if held.is_expired(moment):
            return self._close(unit, held, EXPIRED, held.expires_at, moment)
        if held.rotate_command_id == command_id:
            return Closing(held, None)
        self._advance(unit, held, audio=True)
        revoked = self._revoke_every_grant(unit, session_id, moment)
        row = pg(unit).conn.execute(
            f"UPDATE campaign.table_sessions "
            f"SET link_generation = link_generation + 1, rotate_command_id = %s "
            f"WHERE id = %s RETURNING {_S_COLUMNS}",
            (command_id, session_id),
        ).fetchone()
        return Closing(_session(row), ROTATED, revoked)

    def expire(
        self,
        unit: UnitOfWork,
        session_id: str,
        campaign_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s)
        if held is None:
            return None
        if not held.is_live:
            return Closing(held, None)
        moment = now_or(now)
        if not held.is_expired(moment):
            raise NotDue("that session has not reached its expiry")
        return self._close(unit, held, EXPIRED, held.expires_at, moment)

    def expired_live_sessions_for_gm(
        self, unit: UnitOfWork, gm_user_id: int, *, now: datetime | None = None
    ) -> list[TableSession]:
        rows = pg(unit).conn.execute(
            f'SELECT {_S_COLUMNS} FROM campaign.table_sessions '
            f'WHERE gm_user_id = %s AND state = \'live\' AND expires_at <= %s '
            f'ORDER BY expires_at, id COLLATE "C"',
            (gm_user_id, now_or(now)),
        ).fetchall()
        return [_session(row) for row in rows]

    def mint_screen(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
    ) -> tuple[ScreenGrant, str]:
        moment = now_or(now)
        conn = pg(unit).conn
        theirs = conn.execute(
            "SELECT 1 FROM campaign.table_sessions "
            "WHERE id = %s AND campaign_id = %s AND gm_user_id = %s",
            (session_id, campaign_id, owner_id),
        ).fetchone()
        if theirs is None:
            raise MissingParent("no such table session for that owner")
        unit.lock(AdvisoryLock.TABLE_SESSION, session_id)
        live = conn.execute(
            f"SELECT count(*) FROM campaign.table_credentials g "
            f"JOIN campaign.table_sessions s ON s.id = g.session_id "
            f"WHERE g.session_id = %s AND {_LIVE_GRANT}",
            (session_id, moment),
        ).fetchone()[0]
        if int(live) >= SCREENS_PER_SESSION:
            raise ScreenLimit("that table session has as many screens as it may")
        minted = ident.new_secret()
        row = conn.execute(
            f"INSERT INTO campaign.table_credentials "
            f"(id, session_id, link_generation, credential_digest, created_at) "
            f"SELECT %s, s.id, s.link_generation, %s, %s FROM campaign.table_sessions s "
            f"WHERE s.id = %s AND s.campaign_id = %s AND s.gm_user_id = %s "
            f"AND s.state = 'live' AND s.expires_at > %s "
            f"RETURNING {_G_COLUMNS}",
            (
                ident.new_id(ident.TABLE_CREDENTIAL),
                minted.digest,
                moment,
                session_id,
                campaign_id,
                owner_id,
                moment,
            ),
        ).fetchone()
        if row is None:
            raise NotLive("a session that is not live admits no screen")
        return _grant(row), minted.secret

    def resolve_screen(self, unit: UnitOfWork, digest: str, *, now: datetime) -> LiveScreen | None:
        row = pg(unit).conn.execute(
            f"SELECT g.id, g.session_id, s.campaign_id, g.link_generation "
            f"FROM campaign.table_credentials g JOIN campaign.table_sessions s ON s.id = g.session_id "
            f"WHERE g.credential_digest = %s AND {_LIVE_GRANT}",
            (digest, aware(now, "a clock")),
        ).fetchone()
        return None if row is None else LiveScreen(row[0], row[1], row[2], int(row[3]))

    def screens(self, unit: UnitOfWork, session_id: str) -> list[ScreenGrant]:
        rows = pg(unit).conn.execute(
            f'SELECT {_G_COLUMNS} FROM campaign.table_credentials '
            f'WHERE session_id = %s ORDER BY created_at, id COLLATE "C"',
            (session_id,),
        ).fetchall()
        return [_grant(row) for row in rows]

    def live_screens(self, unit: UnitOfWork, session_id: str, *, now: datetime) -> list[ScreenGrant]:
        rows = pg(unit).conn.execute(
            f"SELECT g.id, g.session_id, g.link_generation, g.credential_digest, g.created_at, "
            f"g.revoked_at, g.last_connected_at "
            f"FROM campaign.table_credentials g JOIN campaign.table_sessions s ON s.id = g.session_id "
            f"WHERE g.session_id = %s AND {_LIVE_GRANT} "
            f'ORDER BY g.created_at, g.id COLLATE "C"',
            (session_id, aware(now, "a clock")),
        ).fetchall()
        return [_grant(row) for row in rows]

    def revoke_screen(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        screen_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
    ) -> tuple[str, bool] | None:
        moment = now_or(now)
        # One owner-scoped statement decides the 204 and the 404 alike (L-11):
        # it matches the owner's grant whether or not it is already revoked, and
        # keeps the first revocation's time. "This call revoked it" is that time
        # being this call's clock — the one reading a repeat at the very same
        # instant would get wrong, and all it would cost is a second audit row.
        row = pg(unit).conn.execute(
            "UPDATE campaign.table_credentials g SET revoked_at = COALESCE(g.revoked_at, %s) "
            "FROM campaign.table_sessions s "
            "WHERE g.id = %s AND s.id = g.session_id AND s.campaign_id = %s AND s.gm_user_id = %s "
            "RETURNING g.session_id, g.revoked_at",
            (moment, screen_id, campaign_id, owner_id),
        ).fetchone()
        return None if row is None else (row[0], row[1] == moment)

    def leave(self, unit: UnitOfWork, digest: str, *, now: datetime) -> LiveScreen | None:
        moment = aware(now, "a clock")
        row = pg(unit).conn.execute(
            f"UPDATE campaign.table_credentials g SET revoked_at = %s "
            f"FROM campaign.table_sessions s "
            f"WHERE g.credential_digest = %s AND s.id = g.session_id AND {_LIVE_GRANT} "
            f"RETURNING g.id, g.session_id, s.campaign_id, g.link_generation",
            (moment, digest, moment),
        ).fetchone()
        return None if row is None else LiveScreen(row[0], row[1], row[2], int(row[3]))

    def liveness(self, unit: UnitOfWork, session_ids: Collection[str]) -> dict[str, Liveness]:
        wanted = sorted(set(session_ids))
        if not wanted:
            return {}
        rows = pg(unit).conn.execute(
            "SELECT s.id, s.state, s.expires_at, s.link_generation, "
            "COALESCE(array_agg(g.id) FILTER (WHERE g.id IS NOT NULL), ARRAY[]::text[]) "
            "FROM campaign.table_sessions s "
            "LEFT JOIN campaign.table_credentials g ON g.session_id = s.id "
            "AND g.revoked_at IS NULL AND g.link_generation = s.link_generation "
            "WHERE s.id = ANY(%s) GROUP BY s.id",
            (wanted,),
        ).fetchall()
        return {
            row[0]: Liveness(row[0], row[1], row[2], int(row[3]), frozenset(row[4])) for row in rows
        }

    def live_session_for_campaign(self, unit: UnitOfWork, campaign_id: str) -> TableSession | None:
        # A GM has at most one live session (REVEAL-2's partial unique index), so
        # a campaign has at most one; LIMIT 1 says so rather than trusting it.
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions "
            f"WHERE campaign_id = %s AND state = 'live' LIMIT 1",
            (campaign_id,),
        ).fetchone()
        return None if row is None else _session(row)


class InMemoryTableSessionStore:
    """The twin. It refuses a second live session for the same GM itself rather
    than leaving that to PostgreSQL's partial index (REVEAL-2), keeps the start
    command's partial unique index and the screen bound itself, and reads the
    campaigns table (shared with the other twins) to decide who the GM is and
    whether the campaign is there at all: a rule the fake is not obliged to keep
    is a rule it will drift on. Races are a different question, and are proved
    against the database."""

    def __init__(self, db: InMemoryDatabase, *, slot_clear: SlotClear) -> None:
        self._slot_clear = slot_clear
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._sessions: Staging[TableSession] = shared_rows(db, "table_sessions")
        self._grants: Staging[ScreenGrant] = shared_rows(db, "table_credentials")

    def start(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        expires_at: datetime,
        command_id: str,
        now: datetime | None = None,
    ) -> TableSession:
        twin = fake(unit)
        moment = now_or(now)
        check_expiry(moment, expires_at)
        check_command_id(command_id)
        campaign = self._campaigns.visible(twin).get(campaign_id)
        if campaign is None or campaign.owner_id != owner_id or campaign.is_archived:
            raise MissingParent("no such campaign for that owner")
        if self.by_start_command(unit, campaign_id, command_id) is not None:
            raise StartReplayed("that campaign already has a session of that command")
        running = [
            s
            for s in self._sessions.visible(twin).values()
            if s.gm_user_id == owner_id and s.is_live
        ]
        if running:
            raise LiveSessionExists("that GM already has a live table session")
        session = TableSession(
            id=ident.new_id(ident.TABLE_SESSION),
            campaign_id=campaign_id,
            gm_user_id=owner_id,
            state=LIVE,
            started_at=moment,
            expires_at=expires_at,
            link_generation=1,
            start_command_id=command_id,
        )
        self._sessions.add(twin, session.id, session)
        return session

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        return self._sessions.visible(fake(unit)).get(session_id)

    def find_owned(
        self, unit: UnitOfWork, campaign_id: str, session_id: str, *, owner_id: int
    ) -> TableSession | None:
        found = self.get(unit, session_id)
        if found is None or found.campaign_id != campaign_id or found.gm_user_id != owner_id:
            return None
        return found

    def by_start_command(
        self, unit: UnitOfWork, campaign_id: str, command_id: str
    ) -> TableSession | None:
        found = [
            s
            for s in self._sessions.visible(fake(unit)).values()
            if s.campaign_id == campaign_id and s.start_command_id == command_id
        ]
        return found[0] if found else None

    def latest_for_campaign(self, unit: UnitOfWork, campaign_id: str) -> TableSession | None:
        mine = [s for s in self._sessions.visible(fake(unit)).values() if s.campaign_id == campaign_id]
        if not mine:
            return None
        return max(mine, key=lambda s: (s.is_live, s.started_at, s.id))

    def live_owned(
        self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, now: datetime
    ) -> TableSession | None:
        moment = aware(now, "a clock")
        found = [
            s
            for s in self._sessions.visible(fake(unit)).values()
            if s.campaign_id == campaign_id and s.gm_user_id == owner_id and s.live_at(moment)
        ]
        return found[0] if found else None

    def _hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        transaction_timeout_s: float | None,
        owner_id: int | None = None,
    ) -> TableSession | None:
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(transaction_timeout_s)
        found = self._sessions.visible(twin).get(session_id)
        if found is None or found.campaign_id != campaign_id:
            return None
        if owner_id is not None and found.gm_user_id != owner_id:
            return None
        return found

    def _write(self, unit: UnitOfWork, updated: TableSession) -> TableSession:
        self._sessions.replace(fake(unit), updated.id, updated)
        return updated

    def _advance(self, unit: UnitOfWork, session: TableSession, *, audio: bool) -> TableSession:
        advanced = self._write(
            unit,
            replace(
                session,
                reveal_epoch=session.reveal_epoch + 1,
                audio_epoch=session.audio_epoch + (1 if audio else 0),
            ),
        )
        self._slot_clear(unit, session.id)
        return advanced

    def narrow(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        audio: bool = False,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s)
        return None if held is None else self._advance(unit, held, audio=audio)

    def _revoke_every_grant(self, unit: UnitOfWork, session_id: str, moment: datetime) -> int:
        twin = fake(unit)
        unrevoked = sorted(
            (
                g
                for g in self._grants.visible(twin).values()
                if g.session_id == session_id and g.revoked_at is None
            ),
            key=lambda g: g.id,
        )
        for grant in unrevoked:
            self._grants.replace(twin, grant.id, replace(grant, revoked_at=moment))
        return len(unrevoked)

    def _close(
        self, unit: UnitOfWork, held: TableSession, state: str, ended_at: datetime, moment: datetime
    ) -> Closing:
        narrowed = self._advance(unit, held, audio=True)
        revoked = self._revoke_every_grant(unit, held.id, moment)
        closed = self._write(unit, replace(narrowed, state=state, ended_at=ended_at))
        return Closing(closed, state, revoked)

    def end(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s, owner_id)
        if held is None:
            return None
        if not held.is_live:
            return Closing(held, None)
        moment = now_or(now)
        if held.is_expired(moment):
            return self._close(unit, held, EXPIRED, held.expires_at, moment)
        return self._close(unit, held, ENDED, moment, moment)

    def rotate(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        command_id: str,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        check_command_id(command_id)
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s, owner_id)
        if held is None:
            return None
        if not held.is_live:
            return Closing(held, None)
        moment = now_or(now)
        if held.is_expired(moment):
            return self._close(unit, held, EXPIRED, held.expires_at, moment)
        if held.rotate_command_id == command_id:
            return Closing(held, None)
        narrowed = self._advance(unit, held, audio=True)
        revoked = self._revoke_every_grant(unit, session_id, moment)
        rotated = self._write(
            unit,
            replace(
                narrowed,
                link_generation=narrowed.link_generation + 1,
                rotate_command_id=command_id,
            ),
        )
        return Closing(rotated, ROTATED, revoked)

    def expire(
        self,
        unit: UnitOfWork,
        session_id: str,
        campaign_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> Closing | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s)
        if held is None:
            return None
        if not held.is_live:
            return Closing(held, None)
        moment = now_or(now)
        if not held.is_expired(moment):
            raise NotDue("that session has not reached its expiry")
        return self._close(unit, held, EXPIRED, held.expires_at, moment)

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

    def _live_grants(
        self, twin: InMemoryTransaction, session: TableSession, moment: datetime
    ) -> list[ScreenGrant]:
        if not session.live_at(moment):
            return []
        return [
            g
            for g in self._grants.visible(twin).values()
            if g.session_id == session.id
            and g.revoked_at is None
            and g.link_generation == session.link_generation
        ]

    def mint_screen(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
    ) -> tuple[ScreenGrant, str]:
        twin = fake(unit)
        moment = now_or(now)
        session = self.find_owned(unit, campaign_id, session_id, owner_id=owner_id)
        if session is None:
            raise MissingParent("no such table session for that owner")
        unit.lock(AdvisoryLock.TABLE_SESSION, session_id)
        if len(self._live_grants(twin, session, moment)) >= SCREENS_PER_SESSION:
            raise ScreenLimit("that table session has as many screens as it may")
        if not session.live_at(moment):
            raise NotLive("a session that is not live admits no screen")
        minted = ident.new_secret()
        grant = ScreenGrant(
            id=ident.new_id(ident.TABLE_CREDENTIAL),
            session_id=session_id,
            link_generation=session.link_generation,
            credential_digest=minted.digest,
            created_at=moment,
        )
        self._grants.add(twin, grant.id, grant)
        return grant, minted.secret

    def _live_by_digest(
        self, twin: InMemoryTransaction, digest: str, moment: datetime
    ) -> LiveScreen | None:
        for grant in self._grants.visible(twin).values():
            if grant.credential_digest != digest:
                continue
            session = self._sessions.visible(twin).get(grant.session_id)
            if session is None or grant not in self._live_grants(twin, session, moment):
                return None
            return LiveScreen(grant.id, session.id, session.campaign_id, grant.link_generation)
        return None

    def resolve_screen(self, unit: UnitOfWork, digest: str, *, now: datetime) -> LiveScreen | None:
        return self._live_by_digest(fake(unit), digest, aware(now, "a clock"))

    def screens(self, unit: UnitOfWork, session_id: str) -> list[ScreenGrant]:
        mine = [g for g in self._grants.visible(fake(unit)).values() if g.session_id == session_id]
        return sorted(mine, key=lambda g: (g.created_at, g.id))

    def live_screens(self, unit: UnitOfWork, session_id: str, *, now: datetime) -> list[ScreenGrant]:
        twin = fake(unit)
        session = self._sessions.visible(twin).get(session_id)
        if session is None:
            return []
        live = self._live_grants(twin, session, aware(now, "a clock"))
        return sorted(live, key=lambda g: (g.created_at, g.id))

    def revoke_screen(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        screen_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
    ) -> tuple[str, bool] | None:
        twin = fake(unit)
        moment = now_or(now)
        grant = self._grants.visible(twin).get(screen_id)
        if grant is None:
            return None
        if self.find_owned(unit, campaign_id, grant.session_id, owner_id=owner_id) is None:
            return None
        if grant.revoked_at is not None:
            # PostgreSQL's COALESCE keeps the first revocation's time, and the
            # caller compares it with its own clock: the same answer here.
            return grant.session_id, grant.revoked_at == moment
        self._grants.replace(twin, grant.id, replace(grant, revoked_at=moment))
        return grant.session_id, True

    def leave(self, unit: UnitOfWork, digest: str, *, now: datetime) -> LiveScreen | None:
        twin = fake(unit)
        moment = aware(now, "a clock")
        live = self._live_by_digest(twin, digest, moment)
        if live is None:
            return None
        grant = self._grants.visible(twin)[live.grant_id]
        self._grants.replace(twin, grant.id, replace(grant, revoked_at=moment))
        return live

    def liveness(self, unit: UnitOfWork, session_ids: Collection[str]) -> dict[str, Liveness]:
        twin = fake(unit)
        found: dict[str, Liveness] = {}
        for session_id in set(session_ids):
            session = self._sessions.visible(twin).get(session_id)
            if session is None:
                continue
            unrevoked = frozenset(
                g.id
                for g in self._grants.visible(twin).values()
                if g.session_id == session_id
                and g.revoked_at is None
                and g.link_generation == session.link_generation
            )
            found[session_id] = Liveness(
                session_id, session.state, session.expires_at, session.link_generation, unrevoked
            )
        return found

    def live_session_for_campaign(self, unit: UnitOfWork, campaign_id: str) -> TableSession | None:
        live = [
            s for s in self._sessions.visible(fake(unit)).values() if s.campaign_id == campaign_id and s.is_live
        ]
        return live[0] if live else None

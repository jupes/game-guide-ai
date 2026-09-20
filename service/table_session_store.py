"""Live table sessions, their link generations and their credentials (1kg.2.1).

A table session is a GM running their table right now: the thing a player's
link joins, the thing a reveal is displayed into, and the thing that ends. Five
rules from the records shape this module.

**One live session per GM, across campaigns** (REVEAL-2). Not one per campaign:
a GM is one person at one table, and two live sessions would mean two displays
claiming the same authority. PostgreSQL refuses the second with a partial unique
index on `gm_user_id WHERE state = 'live'`, which also settles two racing
starts; the twin refuses it in Python, so the two cannot disagree.

**The GM is the campaign's owner** (AUD-1, `docs/migrations.md` section 4).
`start` takes no free `gm_user_id`: it selects the session's GM out of
`campaign.campaigns` in the same statement that inserts the row, so a campaign
that is not the caller's, or is archived, is simply "no row". Every other
mutator names the campaign in its `WHERE` for the same reason — a session of
another GM's campaign must be indistinguishable from one that does not exist.

**A link generation binds every credential made from it** (SEC-9). Rotating the
link and ending the session both close a generation, and *either one* "revokes
every table credential of the old generation in the same transaction that
advances the reveal and audio epochs". That is why `end` and `rotate_link` are
not two independent writes: they take the session row's lock once, advance the
epochs once, and revoke inside the same unit of work, so no credential can
outlive the epoch that retired it. **A credential is valid only while its
generation is the session's current one, and only while the session is live.**
`revoked_at IS NULL` is not the test: `issue_credential` reads the generation
without a lock, deliberately, so that a join never makes a Stop wait — which
means a join that commits just after a Rotate leaves an unrevoked credential of
the generation the Rotate retired. Every reader must check the session's state,
the generation and the revocation together (ED-25, SEC-9); `end` then revokes
every unrevoked credential of the session, whatever its generation, so nothing
is left behind once the table is over.

**`narrow` is the primitive** (RQ-7). Anything that makes what the table can see
smaller — End, expiry, Rotate, Remove participant, Reset personal link, unlink —
goes through it: hold the session row `FOR NO KEY UPDATE`, bound the
transaction, advance the reveal epoch (and the audio epoch when the caller asks),
and call the slot-clearing extension point. That point is **empty in this bead**;
`1kg.7.1` fills it when slots exist. It is a **required** argument rather than
one that defaults to the empty implementation, so that a route module or a job
handler built after `1kg.7.1` cannot silently go on clearing nothing while it
advances the epoch. Crucially, narrowing needs no lock on `authz_state`, so a
Stop never waits behind an authorisation check.

**Every explicit row lock is `FOR NO KEY UPDATE`** (RQ-3), for the reason
`service/participant_store.py` spells out.

What is **not** here: the window arithmetic over `campaign.session_join_counters`
and the credential bound of SEC-10. Migration 0004 carries that storage because
pinning it later would cost another migration, but the rules that read it belong
to `1kg.2.3`, which is also the bead that will write the first row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import NoReturn, Protocol

from . import campaign_identity as ident
from .campaign_store import (
    Campaign,
    LiveSessionExists,
    MissingParent,
    NotLive,
    Staging,
    aware,
    fake,
    now_or,
    pg,
    shared_rows,
)
from .db import InMemoryDatabase, UnitOfWork

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
        owner_id: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> tuple[TableSession, str]:
        """Open a session for the campaign's owner and mint its link token. The
        GM is the owner (AUD-1), read out of the campaigns table in the same
        statement, so `MissingParent` answers a campaign that does not exist, is
        not that owner's or is archived. Raises `LiveSessionExists` if that GM
        already has a live session, in this campaign or any other."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        ...  # pragma: no cover - structural type

    def find_by_link_digest(self, unit: UnitOfWork, digest: str) -> TableSession | None:
        """Which session a table link names — an exact match on the partial
        unique index over `link_digest`, taking no lock, so an unauthenticated
        join costs one index probe and can hold nothing. A retired link has no
        digest at all, so it finds nothing."""
        ...  # pragma: no cover - structural type

    def find_credential(self, unit: UnitOfWork, digest: str) -> TableCredential | None:
        """Which joined device a credential names. It comes back whatever its
        state: a request carrying a revoked credential is refused for being
        revoked, never mistaken for an unknown device."""
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
        expired: bool = False,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        """End a live session: narrow it, revoke every unrevoked credential of
        it, and stamp `ended_at` — one unit of work (SEC-9). `expired` records
        the ending as the clock's rather than the GM's (`state = 'expired'`,
        ED-17's `session_ended(gm_end | expired)`); it changes nothing else.
        Ending a session that is already ended or expired changes nothing and is
        not an error; it returns the session as it stands."""
        ...  # pragma: no cover - structural type

    def rotate_link(
        self,
        unit: UnitOfWork,
        campaign_id: str,
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
        self, unit: UnitOfWork, campaign_id: str, session_id: str, *, now: datetime | None = None
    ) -> tuple[TableCredential, str]:
        """Mint a joined device's credential in the session's current
        generation. Refuses a session that is not live with `NotLive` — a dead
        session opens no doors — and one that is not in that campaign with
        `MissingParent`. Returns the record and the credential itself."""
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
    """`campaign.table_sessions` and `campaign.table_credentials` (migration 0004).

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
        now: datetime | None = None,
    ) -> tuple[TableSession, str]:
        moment = now_or(now)
        check_expiry(moment, expires_at)
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.table_sessions "
            f"(id, campaign_id, gm_user_id, state, started_at, expires_at, link_digest) "
            f"SELECT %s, c.id, c.owner_id, 'live', %s, %s, %s FROM campaign.campaigns c "
            f"WHERE c.id = %s AND c.owner_id = %s AND c.archived_at IS NULL "
            f"ON CONFLICT (gm_user_id) WHERE state = 'live' DO NOTHING RETURNING {_S_COLUMNS}",
            (
                ident.new_id(ident.TABLE_SESSION),
                moment,
                expires_at,
                minted.digest,
                campaign_id,
                owner_id,
            ),
        ).fetchone()
        if row is None:
            self._refuse_start(unit, campaign_id, owner_id)
        return _session(row), minted.secret

    def _refuse_start(self, unit: UnitOfWork, campaign_id: str, owner_id: int) -> NoReturn:
        """Which of the two conditions refused it. The campaign is asked about
        first, in both worlds, so the two cannot answer differently when both
        conditions hold."""
        theirs = pg(unit).conn.execute(
            "SELECT 1 FROM campaign.campaigns "
            "WHERE id = %s AND owner_id = %s AND archived_at IS NULL",
            (campaign_id, owner_id),
        ).fetchone()
        if theirs is None:
            raise MissingParent("no such campaign for that owner")
        raise LiveSessionExists("that GM already has a live table session")

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions WHERE id = %s", (session_id,)
        ).fetchone()
        return None if row is None else _session(row)

    def find_by_link_digest(self, unit: UnitOfWork, digest: str) -> TableSession | None:
        row = pg(unit).conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions WHERE link_digest = %s", (digest,)
        ).fetchone()
        return None if row is None else _session(row)

    def find_credential(self, unit: UnitOfWork, digest: str) -> TableCredential | None:
        row = pg(unit).conn.execute(
            f"SELECT {_T_COLUMNS} FROM campaign.table_credentials WHERE credential_digest = %s",
            (digest,),
        ).fetchone()
        return None if row is None else _credential(row)

    def _hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        transaction_timeout_s: float | None,
    ) -> TableSession | None:
        """The session row, held for the rest of this transaction, with the
        transaction bounded first so that a narrowing cannot become the thing
        everyone else waits behind."""
        transaction = pg(unit)
        transaction.note_row_lock()
        transaction.conn.execute(
            "SELECT set_config('transaction_timeout', %s, true)",
            (transaction.transaction_bound(transaction_timeout_s),),
        )
        row = transaction.conn.execute(
            f"SELECT {_S_COLUMNS} FROM campaign.table_sessions "
            f"WHERE id = %s AND campaign_id = %s FOR NO KEY UPDATE",
            (session_id, campaign_id),
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

    def _revoke_generation(
        self, unit: UnitOfWork, session_id: str, generation: int, moment: datetime
    ) -> None:
        pg(unit).conn.execute(
            "UPDATE campaign.table_credentials SET revoked_at = %s "
            "WHERE session_id = %s AND link_generation = %s AND revoked_at IS NULL",
            (moment, session_id, generation),
        )

    def _revoke_every_credential(
        self, unit: UnitOfWork, session_id: str, moment: datetime
    ) -> None:
        """What an ending revokes: not one generation but all of them. A join
        that committed just after a Rotate holds an unrevoked credential of the
        generation the Rotate retired (see the module docstring), and the table
        is over — nothing may be left with `revoked_at IS NULL` behind it."""
        pg(unit).conn.execute(
            "UPDATE campaign.table_credentials SET revoked_at = %s "
            "WHERE session_id = %s AND revoked_at IS NULL",
            (moment, session_id),
        )

    def end(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        expired: bool = False,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s)
        if held is None or not held.is_live:
            return held
        moment = now_or(now)
        self._advance(unit, held, audio=True)
        self._revoke_every_credential(unit, session_id, moment)
        row = pg(unit).conn.execute(
            f"UPDATE campaign.table_sessions SET state = %s, ended_at = %s, link_digest = NULL "
            f"WHERE id = %s RETURNING {_S_COLUMNS}",
            (EXPIRED if expired else ENDED, moment, session_id),
        ).fetchone()
        return _session(row)

    def rotate_link(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> tuple[TableSession, str] | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s)
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
            f'SELECT {_S_COLUMNS} FROM campaign.table_sessions '
            f'WHERE gm_user_id = %s AND state = \'live\' AND expires_at <= %s '
            f'ORDER BY expires_at, id COLLATE "C"',
            (gm_user_id, now_or(now)),
        ).fetchall()
        return [_session(row) for row in rows]

    def issue_credential(
        self, unit: UnitOfWork, campaign_id: str, session_id: str, *, now: datetime | None = None
    ) -> tuple[TableCredential, str]:
        minted = ident.new_secret()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.table_credentials "
            f"(id, session_id, link_generation, credential_digest, created_at) "
            f"SELECT %s, id, link_generation, %s, %s FROM campaign.table_sessions "
            f"WHERE id = %s AND campaign_id = %s AND state = 'live' "
            f"RETURNING {_T_COLUMNS}",
            (
                ident.new_id(ident.TABLE_CREDENTIAL),
                minted.digest,
                now_or(now),
                session_id,
                campaign_id,
            ),
        ).fetchone()
        if row is None:
            self._refuse_credential(unit, campaign_id, session_id)
        return _credential(row), minted.secret

    def _refuse_credential(self, unit: UnitOfWork, campaign_id: str, session_id: str) -> NoReturn:
        there = pg(unit).conn.execute(
            "SELECT 1 FROM campaign.table_sessions WHERE id = %s AND campaign_id = %s",
            (session_id, campaign_id),
        ).fetchone()
        if there is None:
            raise MissingParent("no such table session in that campaign")
        raise NotLive("a session that is not live admits no device")

    def credentials(self, unit: UnitOfWork, session_id: str) -> list[TableCredential]:
        rows = pg(unit).conn.execute(
            f'SELECT {_T_COLUMNS} FROM campaign.table_credentials '
            f'WHERE session_id = %s ORDER BY created_at, id COLLATE "C"',
            (session_id,),
        ).fetchall()
        return [_credential(row) for row in rows]


class InMemoryTableSessionStore:
    """The twin. It refuses a second live session for the same GM itself rather
    than leaving that to PostgreSQL's partial index (REVEAL-2), because a rule
    the fake is not obliged to keep is a rule it will drift on — and for the same
    reason it reads the campaigns table (shared with the other two twins) to
    decide who the GM is and whether the campaign is there at all. Two racing
    starts are a different question, and are proved against the database."""

    def __init__(self, db: InMemoryDatabase, *, slot_clear: SlotClear) -> None:
        self._slot_clear = slot_clear
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._sessions: Staging[TableSession] = shared_rows(db, "table_sessions")
        self._credentials: Staging[TableCredential] = shared_rows(db, "table_credentials")

    def start(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> tuple[TableSession, str]:
        twin = fake(unit)
        moment = now_or(now)
        check_expiry(moment, expires_at)
        campaign = self._campaigns.visible(twin).get(campaign_id)
        if campaign is None or campaign.owner_id != owner_id or campaign.is_archived:
            raise MissingParent("no such campaign for that owner")
        running = [
            s
            for s in self._sessions.visible(twin).values()
            if s.gm_user_id == owner_id and s.is_live
        ]
        if running:
            raise LiveSessionExists("that GM already has a live table session")
        minted = ident.new_secret()
        session = TableSession(
            id=ident.new_id(ident.TABLE_SESSION),
            campaign_id=campaign_id,
            gm_user_id=owner_id,
            state=LIVE,
            started_at=moment,
            expires_at=expires_at,
            link_generation=1,
            link_digest=minted.digest,
        )
        self._sessions.add(twin, session.id, session)
        return session, minted.secret

    def get(self, unit: UnitOfWork, session_id: str) -> TableSession | None:
        return self._sessions.visible(fake(unit)).get(session_id)

    def find_by_link_digest(self, unit: UnitOfWork, digest: str) -> TableSession | None:
        found = [s for s in self._sessions.visible(fake(unit)).values() if s.link_digest == digest]
        return found[0] if found else None

    def find_credential(self, unit: UnitOfWork, digest: str) -> TableCredential | None:
        found = [
            c for c in self._credentials.visible(fake(unit)).values() if c.credential_digest == digest
        ]
        return found[0] if found else None

    def _hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        transaction_timeout_s: float | None,
    ) -> TableSession | None:
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(transaction_timeout_s)
        found = self._sessions.visible(twin).get(session_id)
        return None if found is None or found.campaign_id != campaign_id else found

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

    def _revoke(
        self, unit: UnitOfWork, session_id: str, moment: datetime, generation: int | None
    ) -> None:
        """Every unrevoked credential of the session, or of one generation of it
        when `generation` is given."""
        twin = fake(unit)
        for held in list(self._credentials.visible(twin).values()):
            if held.session_id != session_id or not held.is_active:
                continue
            if generation is not None and held.link_generation != generation:
                continue
            self._credentials.replace(twin, held.id, replace(held, revoked_at=moment))

    def end(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        expired: bool = False,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> TableSession | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s)
        if held is None or not held.is_live:
            return held
        moment = now_or(now)
        narrowed = self._advance(unit, held, audio=True)
        self._revoke(unit, session_id, moment, generation=None)
        return self._write(
            unit,
            replace(
                narrowed,
                state=EXPIRED if expired else ENDED,
                ended_at=moment,
                link_digest=None,
            ),
        )

    def rotate_link(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        *,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> tuple[TableSession, str] | None:
        held = self._hold(unit, campaign_id, session_id, transaction_timeout_s)
        if held is None:
            return None
        if not held.is_live:
            raise NotLive("only a live session's link can be rotated")
        moment = now_or(now)
        narrowed = self._advance(unit, held, audio=True)
        self._revoke(unit, session_id, moment, generation=narrowed.link_generation)
        minted = ident.new_secret()
        rotated = self._write(
            unit,
            replace(
                narrowed,
                link_generation=narrowed.link_generation + 1,
                link_digest=minted.digest,
            ),
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
        self, unit: UnitOfWork, campaign_id: str, session_id: str, *, now: datetime | None = None
    ) -> tuple[TableCredential, str]:
        twin = fake(unit)
        session = self._sessions.visible(twin).get(session_id)
        if session is None or session.campaign_id != campaign_id:
            raise MissingParent("no such table session in that campaign")
        if not session.is_live:
            raise NotLive("a session that is not live admits no device")
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

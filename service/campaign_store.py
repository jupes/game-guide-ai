"""Campaigns, and the plumbing the three campaign stores share (1kg.2.1).

A campaign is a GM's table: the aggregate every participant, session, document
and reveal hangs off. This module holds its store — a `Protocol`, an in-memory
twin and a PostgreSQL implementation, the shape `service/auth_store.py`
established — and the few pieces the participant and table-session stores need
too, kept here rather than spelled three times.

**Ownership is in the query** (`docs/migrations.md` section 4). A campaign read
or write names the **owner** in the same statement as the row; a participant or
session write names the **campaign**. A row that is not the caller's is
therefore indistinguishable from one that does not exist, and there is no
"fetch, then check" — a check that happens after the fetch is a check something
can skip. There is no exception any more: the player's unauthenticated
enrolment path, which held a participant row without naming its campaign, was
retired with the enrolment code (owner decisions D-1 and D-4, bead `fma`), and
a player is now an account whose seat is found by campaign and account together.

**Mutations take the unit of work first**, like `JobQueue.enqueue`, so that
`1kg.2.2` and `1kg.2.3` can compose all three stores — and the audit writer —
in one transaction that commits or rolls back together.

**The twins owe three things to `InMemoryDatabase`** (`service/db.py`). Every
write — an insert *and* a change to an already-committed row — is staged in the
writing unit alone and published with `on_publish`, so a second reader sees what
READ COMMITTED would show it and a rollback needs no undo. Uniqueness is decided
over the committed rows plus the writing unit's own, so a rolled-back change
cannot leave the twin in a state a partial unique index forbids. And the three
twins share **one** set of tables (`shared_rows`), so a child whose parent does
not exist is refused here as a foreign key refuses it there.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

from . import campaign_identity as ident
from .db import AdvisoryLock, InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork
from .workbench_contracts import check_stored_text


class CampaignStoreError(Exception):
    """What a campaign store refuses to do. Each subclass is a distinct refusal
    so a route can answer each one differently."""


class AliasTaken(CampaignStoreError):
    """Another participant of that campaign already answers to that alias
    (AUD-13). The message names no alias — an alias is private text (SEC-20)."""


class NotLive(CampaignStoreError):
    """The table session is ended or expired, so the thing asked of it does not
    apply. Ending an already-ended session is deliberately NOT this: ending is
    idempotent, and rotating a retired link is not."""


class LiveSessionExists(CampaignStoreError):
    """That GM already has a live table session, in this campaign or another
    (REVEAL-2). PostgreSQL refuses it with a partial unique index; the twin
    refuses it here, so the two cannot disagree."""


class StartReplayed(CampaignStoreError):
    """That campaign already has a session started by this command id
    (`table_sessions_start_command_uidx`, migration 0016). A caller that holds
    the campaign lock reads the replay first and answers the session it finds
    (`service/table_sessions.py`), so this is what a caller that skipped that
    read gets instead of a unique violation. The message names no id."""


class NotDue(CampaignStoreError):
    """The system's expiry was asked to finalise a session that has not reached
    its `expires_at` yet. Refused, so that the one path that holds a session
    without naming its owner can never be an End that skipped the owner check
    (`1kg.2.3`, L-5)."""


class ScreenLimit(CampaignStoreError):
    """That session already has as many live screen grants as it may (SEC-48).
    Raised identically by both worlds, under the session's advisory lock; the
    message names no session, grant or account."""


class MissingParent(CampaignStoreError, LookupError):
    """The row this write would hang off is not there: a campaign that never
    existed, one that is not this owner's, one that is archived, or a
    participant or session that was never created.

    **One answer for all four.** A caller must not be able to tell them apart —
    that is SEC-2's "one query, one 404" — and the path that legitimately needs
    to tell them apart holds the row itself (`ParticipantStore.hold`) and reads
    it. In PostgreSQL a foreign key is still the guarantee; this refusal is what
    the statement's own `WHERE EXISTS` reports first, so an ordinary programming
    error does not arrive as an aborted transaction carrying the driver's text.

    It is a `LookupError` as well, because "there is no such row" is what the
    standard exception means and some callers already catch that.
    """


class SeatUnavailable(CampaignStoreError):
    """A seat cannot be offered to, or accepted by, that account (bead `fma`).

    **One refusal for every reason**, raised identically by both worlds: the
    seat is missing or belongs to another campaign, it is removed, it is not
    open (an offer) or not offered to this account (an acceptance), the account
    is the campaign's owner, or the account already holds a live seat there. A
    seat that does not exist and one in another GM's campaign must be
    indistinguishable to the caller (SEC-3), so the reasons are not told apart
    here either.

    **The message is fixed and carries no identifier** — no alias, email, user
    id, participant id or campaign id (SEC-20). The constructor takes nothing,
    so no caller can put one in.
    """

    MESSAGE = "that seat is not available to that account"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class SeatNotAccepted(CampaignStoreError):
    """The GM asked to confirm a seat that is live but that no account has
    accepted yet (bead 1kg.2.2, L-11). Distinct from `SeatUnavailable` because a
    route answers it with a 409 — which only the campaign's owner can reach,
    after ownership was shown (SEC-3) — where a missing, foreign or removed seat
    is the one 404. The message is fixed and carries no identifier, and the
    constructor takes nothing, so no caller can put one in."""

    MESSAGE = "that seat has not been accepted yet"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class CampaignCapReached(CampaignStoreError):
    """The account already holds as many campaigns as
    `WORKBENCH_CAMPAIGNS_PER_ACCOUNT_MAX` allows (agent-forge-harness-531x).

    Archived and concluded campaigns count: rows are never deleted, so
    excluding them would let an archive-then-create loop get around this cap
    and, with it, every per-campaign cap behind it (seats, groups, media).

    **Not a `ValueError`**: `create` raises none of its own, and the route
    (`campaigns_api.create_campaign`) catches this one specifically and builds
    its own fixed, number-free message (SEC-20) — never this exception's own
    text, which is never rendered to a caller."""


class InvalidCursor(CampaignStoreError, ValueError):
    """A page cursor that did not come from this server (bead 1kg.2.2, L-19). A
    route answers the 422 naming `cursor`; the message never repeats the
    cursor, which is client-held data that decodes to an id (SEC-20)."""

    MESSAGE = "that page cursor did not come from this server"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


# ── Plumbing the three stores share ──────────────────────────────────────────


def aware(moment: datetime, what: str) -> datetime:
    """`moment` if it carries a time zone, else a refusal.

    A naive value disagrees between the worlds and is silently wrong in both:
    PostgreSQL reinterprets it in the session's time zone against a
    `TIMESTAMPTZ` column, while the twin keeps it and raises `TypeError` the
    first time something compares it. Refused here, once, for every store.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{what} passed to a campaign store is timezone-aware")
    return moment


def now_or(now: datetime | None) -> datetime:
    """A caller's clock, or this one. Every store method takes `now` so that a
    test can place a row in the past without sleeping."""
    return datetime.now(UTC) if now is None else aware(now, "a clock")


#: The largest page any campaign-domain list answers (1kg.2.2: 1 to 50).
PAGE_MAX = 50


# ── Page cursors (1kg.2.2, L-19) ─────────────────────────────────────────────
#
# base64url of the sort key and the id, and nothing else: no owner (that is the
# session's), no filter, and no text. Decoding checks the moment and the id's
# SHAPE against its prefix before any statement sees them — an id carrying a
# control character must be the 422, never a 500 (bead `kky`).


def encode_cursor(moment: datetime, row_id: str) -> str:
    raw = json.dumps([moment.isoformat(), row_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, prefix: str) -> tuple[datetime, str]:
    """The sort key and the id a cursor carries, or `InvalidCursor`. Raised
    outside the handler, so neither `__cause__` nor `__context__` carries the
    caller's decoded payload into a traceback."""
    decoded: tuple[datetime, str] | None = None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        moment, row_id = json.loads(raw.decode("utf-8"))
        if isinstance(moment, str) and isinstance(row_id, str) and ident.is_id(prefix, row_id):
            decoded = aware(datetime.fromisoformat(moment), "a cursor"), row_id
    except Exception:  # noqa: BLE001 - any failure to read it is the one refusal
        decoded = None
    if decoded is None:
        raise InvalidCursor()
    return decoded


@dataclass(frozen=True)
class Page[T]:
    """`{items, next_cursor}`, the contract's page shape. `next_cursor` is None
    on the last page; a page is fetched one row long, so "is there another?"
    needs no count."""

    items: list[T]
    next_cursor: str | None


def page_of[T](rows: list[T], limit: int, key: Callable[[T], tuple[datetime, str]]) -> Page[T]:
    """The first `limit` of `rows` (which holds up to `limit + 1`), with the
    cursor of the last one kept when there is more."""
    if len(rows) <= limit:
        return Page(rows, None)
    kept = rows[:limit]
    return Page(kept, encode_cursor(*key(kept[-1])))


def check_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_MAX:
        raise ValueError(f"a page holds 1 to {PAGE_MAX} rows")
    return limit


def pg(unit: UnitOfWork) -> PgTransaction:
    """The unit as a PostgreSQL transaction, or a refusal. A Postgres store
    handed the twin's unit would otherwise write nowhere and report success."""
    if not isinstance(unit, PgTransaction):
        raise TypeError("a PostgreSQL campaign store writes inside a PostgreSQL transaction")
    return unit


def fake(unit: UnitOfWork) -> InMemoryTransaction:
    if not isinstance(unit, InMemoryTransaction):
        raise TypeError("an in-memory campaign store writes inside an in-memory transaction")
    return unit


class Staging[T]:
    """One twin table, with the commit-time visibility PostgreSQL gives a reader.

    Every write — a new row and a change to an already-committed one alike —
    goes into the **writing unit's** staging area, invisible to every other
    reader, and is published into the committed rows on commit or dropped on
    rollback. A store reads its own transaction's staged rows plus the committed
    ones; everyone else reads only the committed ones.

    **Why a change is staged and not made in place.** The first version changed
    a committed row where it stood and registered an undo, on the grounds that
    the twin's transactions are serial. They are not serial in the way that
    needs: `InMemoryDatabase` takes a re-entrant lock, so a test may open a
    second transaction inside the first — which is exactly how a visibility test
    is written — and that reader would see an uncommitted change. Worse, a
    rolled-back change was undone one row at a time, so a unit that ended a
    session and started another could leave two live sessions behind, a state
    `table_sessions_one_live_per_gm_uidx` forbids. Staging removes both: nobody
    but the writer sees the change, and a rollback drops the whole staging area
    without touching a committed row at all.

    **One open writer** (ixa.1). A nested unit that tries to write while another
    open unit is the writer is refused with `TwinWouldBlock` — PostgreSQL would
    make it wait — and a unit that only reads never claims anything.
    """

    def __init__(self) -> None:
        self._rows: dict[str, T] = {}
        #: Keyed by the unit itself, never by `id(unit)` (ixa.1): CPython hands
        #: a freed address to the next object it allocates, so a unit that never
        #: finished would leak its staged rows to whichever unit came next. The
        #: dictionary holds a strong reference to its key, so while an entry
        #: survives, that address cannot be reused. The trade: a unit that never
        #: commits or rolls back keeps one entry alive here.
        self._staged: dict[InMemoryTransaction, dict[str, T]] = {}

    def _mine(self, unit: InMemoryTransaction) -> dict[str, T]:
        # The only way to write, so the claim is taken here — first, before a
        # row is staged or a callback registered, so a refused write leaves
        # nothing behind. `visible` reads `_staged` directly and never claims.
        unit.claim_writer()
        if unit not in self._staged:
            self._staged[unit] = {}

            def publish() -> None:
                self._rows.update(self._staged.pop(unit, {}))

            def discard() -> None:
                self._staged.pop(unit, None)

            unit.on_publish(publish)
            unit.on_rollback(discard)
        return self._staged[unit]

    def add(self, unit: InMemoryTransaction, key: str, row: T) -> None:
        """A new row, invisible to every other reader until this unit commits."""
        self._mine(unit)[key] = row

    def visible(self, unit: InMemoryTransaction) -> dict[str, T]:
        """Committed rows, plus this transaction's own uncommitted ones — what
        a uniqueness check must look at, and nothing more."""
        return {**self._rows, **self._staged.get(unit, {})}

    def replace(self, unit: InMemoryTransaction, key: str, row: T) -> None:
        """Change a row that is already visible. Deliberately the same mechanism
        as `add`: copy-on-write into this unit's staging area, published on
        commit. The two names stay apart because they say different things about
        the caller's intent, not because they do different things."""
        self._mine(unit)[key] = row


def shared_rows[T](db: InMemoryDatabase, name: str) -> Staging[T]:
    """The twin table called `name` on this in-memory database.

    The three fakes share one set of tables rather than each keeping its own,
    because a fake that cannot see the campaigns table cannot refuse a
    participant whose campaign does not exist — and then every unit test written
    on the fakes passes for a reason PostgreSQL would not share, until the first
    real request answers with a foreign-key violation.
    """
    existing: Staging[T] | None = db.tables.get(name)
    if existing is not None:
        return existing
    fresh: Staging[T] = Staging()
    db.tables[name] = fresh
    return fresh


# ── The record and its store ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Campaign:
    """A GM's table.

    `name` is hidden from `repr()`. SEC-20's list of private text — a brief, an
    instruction, a field value, a search string, an alias, a cue title, a
    filename — does not name a campaign name, but the same rule says positively
    what a log line may carry: "opaque ids, codes, sizes and durations". A name
    the GM wrote is none of those, and a traceback is a log line. It reaches the
    GM through a route that answers them, not through a `repr()`.
    """

    id: str
    owner_id: int
    name: str = field(repr=False)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    #: The GM's "this story is finished" (bead cfx, migration 0013). Not
    #: `archived_at`: archive is an authorisation fact, and this is a label that
    #: narrows and widens nothing (interactions ADR §19 A-31).
    concluded_at: datetime | None = None
    #: The card's tone line — optional (§12.2: "only a name is required") and
    #: private text, hidden from `repr()` for the reason `name` is.
    tone: str | None = field(default=None, repr=False)
    game_system: str = "dnd5e"

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_concluded(self) -> bool:
        return self.concluded_at is not None


NAME_MAX_CHARS = 120
#: `campaigns_tone_chk` in 0013.
TONE_MAX_CHARS = 80


def check_name(name: str) -> str:
    """The bound `0004_campaign_schema.sql` carries, applied before the statement
    so that the refusal is this one rather than an integrity error — and the one
    rule for stored text (`check_stored_text`, bead 1kg.2.2 L-4), so that no path
    stores a control character, a bidirectional override or a lone surrogate in
    a name. Each refusal names the rule, never the name."""
    try:
        check_stored_text(name)
    except ValueError:
        refused = True
    else:
        refused = False
    if refused:  # outside the handler: the refusal chains nothing
        raise ValueError("a campaign name carries no control or formatting characters")
    if not 1 <= len(name) <= NAME_MAX_CHARS:
        raise ValueError(f"a campaign name is 1 to {NAME_MAX_CHARS} characters")
    return name


def check_tone(tone: str | None) -> str | None:
    """`campaigns_tone_chk`, applied before the statement, and the one rule for
    stored text — the `check_name` precedent. `None` is no tone line at all.
    Each refusal names the rule, never the tone."""
    if tone is None:
        return None
    try:
        check_stored_text(tone)
    except ValueError:
        refused = True
    else:
        refused = False
    if refused:  # outside the handler: the refusal chains nothing
        raise ValueError("a tone line carries no control or formatting characters")
    if not 1 <= len(tone) <= TONE_MAX_CHARS:
        raise ValueError(f"a tone line is 1 to {TONE_MAX_CHARS} characters")
    return tone


class CampaignStore(Protocol):
    """Campaigns, and the authorisation revision every check reads."""

    def create(
        self,
        unit: UnitOfWork,
        *,
        owner_id: int,
        name: str,
        tone: str | None = None,
        now: datetime | None = None,
        max_per_owner: int | None = None,
    ) -> Campaign:
        """Make a campaign owned by `owner_id`, together with its `authz_state`
        row at revision 0 — in PostgreSQL the AFTER INSERT trigger writes it, so
        that no path can leave a campaign without one (RQ-1). A tone line is
        optional (bead cfx).

        **`max_per_owner`** (agent-forge-harness-531x): `None` (every existing
        caller and test) behaves exactly as before. Given a number, this takes
        `AdvisoryLock.ACCOUNT_STORAGE` first, counts that owner's campaigns
        under the lock — archived and concluded ones included, since rows are
        never deleted — and raises `CampaignCapReached` at or past it, all
        before the insert and in the same transaction: two concurrent creates
        at the cap serialise on the lock, and the second sees the first's
        committed row (READ COMMITTED)."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int) -> Campaign | None:
        """That owner's campaign, or None — which is also the answer for one that
        belongs to somebody else."""
        ...  # pragma: no cover - structural type

    def list_for_owner(self, unit: UnitOfWork, owner_id: int) -> list[Campaign]:
        """Oldest first."""
        ...  # pragma: no cover - structural type

    def page_for_owner(
        self,
        unit: UnitOfWork,
        owner_id: int,
        *,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int = PAGE_MAX,
    ) -> Page[Campaign]:
        """That owner's campaigns, newest first (`created_at DESC, id DESC`, the
        id compared by code point), one page at a time. Raises `InvalidCursor`
        for a cursor this server did not make."""
        ...  # pragma: no cover - structural type

    def rename(
        self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, name: str, now: datetime | None = None
    ) -> Campaign | None:
        """Rename that owner's campaign in one statement, the owner in it; None
        for a campaign that is not theirs. A rename to the name it already has
        changes nothing, `updated_at` included. No lock, no revision, no audit
        row: a name is not an authorisation fact (L-4)."""
        ...  # pragma: no cover - structural type

    def set_archived(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        archived: bool,
        now: datetime | None = None,
    ) -> bool:
        """Archive or restore; reports whether a row of that owner's changed."""
        ...  # pragma: no cover - structural type

    def set_tone(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        tone: str | None,
        now: datetime | None = None,
    ) -> Campaign | None:
        """Set or clear (`None`) the tone line in one statement, the owner in
        it; None for a campaign that is not theirs. Setting the tone it already
        has changes nothing, `updated_at` included — `rename`'s rule, and like a
        name it is not an authorisation fact: no lock, no revision, no audit."""
        ...  # pragma: no cover - structural type

    def set_concluded(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        concluded: bool,
        now: datetime | None = None,
    ) -> bool:
        """Mark concluded or reopen, in one statement with the owner in it;
        reports whether a row of that owner's changed, so a repeat is not an
        error. An archived campaign may be either: concluded is a label, not a
        state the archive rules read."""
        ...  # pragma: no cover - structural type

    def authz_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        """The campaign's authorisation revision, read under the shared campaign
        lock. It is written only by `UnitOfWork.advance_authz_revision`."""
        ...  # pragma: no cover - structural type

    def projection_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        """The authorisation revision the table namespace was last projected
        at (1ir.2.1), or None when the campaign has no authorisation row.
        Written by `UnitOfWork.advance_authz_revision` (rule 3) and, from
        `1ir.2.3`, by the projector — never by anything else."""
        ...  # pragma: no cover - structural type


_COLUMNS = "id, owner_id, name, created_at, updated_at, archived_at, concluded_at, tone, game_system"


def _campaign(row: tuple) -> Campaign:
    return Campaign(row[0], int(row[1]), row[2], row[3], row[4], row[5], row[6], row[7], row[8])


def _newest_key(campaign: Campaign) -> tuple[datetime, str]:
    return campaign.created_at, campaign.id


class PostgresCampaignStore:
    """`campaign.campaigns` and `campaign.authz_state` (migrations 0004, 0013)."""

    def create(
        self,
        unit: UnitOfWork,
        *,
        owner_id: int,
        name: str,
        tone: str | None = None,
        now: datetime | None = None,
        max_per_owner: int | None = None,
    ) -> Campaign:
        moment = now_or(now)
        if max_per_owner is not None:
            pg(unit).lock(AdvisoryLock.ACCOUNT_STORAGE, str(owner_id))
            count = pg(unit).conn.execute(
                "SELECT count(*) FROM campaign.campaigns WHERE owner_id = %s", (owner_id,)
            ).fetchone()[0]
            if count >= max_per_owner:
                raise CampaignCapReached()
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.campaigns (id, owner_id, name, tone, created_at, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (ident.new_id(ident.CAMPAIGN), owner_id, check_name(name), check_tone(tone), moment, moment),
        ).fetchone()
        return _campaign(row)

    def get(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int) -> Campaign | None:
        row = pg(unit).conn.execute(
            f"SELECT {_COLUMNS} FROM campaign.campaigns WHERE id = %s AND owner_id = %s",
            (campaign_id, owner_id),
        ).fetchone()
        return None if row is None else _campaign(row)

    def list_for_owner(self, unit: UnitOfWork, owner_id: int) -> list[Campaign]:
        # COLLATE "C" so the tie-break is by code point, which is how the twin
        # sorts; without it the database's collation decides, and two rows
        # written with one `now` tie on `created_at` more often than one expects.
        rows = pg(unit).conn.execute(
            f'SELECT {_COLUMNS} FROM campaign.campaigns WHERE owner_id = %s '
            f'ORDER BY created_at, id COLLATE "C"',
            (owner_id,),
        ).fetchall()
        return [_campaign(row) for row in rows]

    def page_for_owner(
        self,
        unit: UnitOfWork,
        owner_id: int,
        *,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int = PAGE_MAX,
    ) -> Page[Campaign]:
        size = check_limit(limit)
        after = None if cursor is None else decode_cursor(cursor, ident.CAMPAIGN)
        rows = pg(unit).conn.execute(
            f"SELECT {_COLUMNS} FROM campaign.campaigns "
            f"WHERE owner_id = %(owner)s AND (%(archived)s OR archived_at IS NULL) "
            f"AND (%(at)s::timestamptz IS NULL OR created_at < %(at)s::timestamptz "
            f'OR (created_at = %(at)s::timestamptz AND id COLLATE "C" < %(id)s)) '
            f'ORDER BY created_at DESC, id COLLATE "C" DESC LIMIT %(limit)s',
            {
                "owner": owner_id,
                "archived": include_archived,
                "at": None if after is None else after[0],
                "id": None if after is None else after[1],
                "limit": size + 1,
            },
        ).fetchall()
        return page_of([_campaign(row) for row in rows], size, _newest_key)

    def rename(
        self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, name: str, now: datetime | None = None
    ) -> Campaign | None:
        named = check_name(name)
        # One statement, the owner in it. `updated_at` moves only when the name
        # does, so a rename to the same name changes nothing a reader can see.
        row = pg(unit).conn.execute(
            f"UPDATE campaign.campaigns SET name = %s, "
            f"updated_at = CASE WHEN name = %s THEN updated_at ELSE %s END "
            f"WHERE id = %s AND owner_id = %s RETURNING {_COLUMNS}",
            (named, named, now_or(now), campaign_id, owner_id),
        ).fetchone()
        return None if row is None else _campaign(row)

    def set_archived(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        archived: bool,
        now: datetime | None = None,
    ) -> bool:
        moment = now_or(now)
        changed = pg(unit).conn.execute(
            "UPDATE campaign.campaigns SET archived_at = %s, updated_at = %s "
            "WHERE id = %s AND owner_id = %s AND (archived_at IS NULL) = %s RETURNING id",
            (moment if archived else None, moment, campaign_id, owner_id, archived),
        ).fetchone()
        return changed is not None

    def set_tone(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        tone: str | None,
        now: datetime | None = None,
    ) -> Campaign | None:
        checked = check_tone(tone)
        # `IS NOT DISTINCT FROM`, because clearing a tone that is already NULL
        # must change nothing either, and `=` answers NULL for that pair.
        row = pg(unit).conn.execute(
            f"UPDATE campaign.campaigns SET tone = %(tone)s, "
            f"updated_at = CASE WHEN tone IS NOT DISTINCT FROM %(tone)s THEN updated_at ELSE %(now)s END "
            f"WHERE id = %(id)s AND owner_id = %(owner)s RETURNING {_COLUMNS}",
            {"tone": checked, "now": now_or(now), "id": campaign_id, "owner": owner_id},
        ).fetchone()
        return None if row is None else _campaign(row)

    def set_concluded(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        concluded: bool,
        now: datetime | None = None,
    ) -> bool:
        moment = now_or(now)
        changed = pg(unit).conn.execute(
            "UPDATE campaign.campaigns SET concluded_at = %s, updated_at = %s "
            "WHERE id = %s AND owner_id = %s AND (concluded_at IS NULL) = %s RETURNING id",
            (moment if concluded else None, moment, campaign_id, owner_id, concluded),
        ).fetchone()
        return changed is not None

    def authz_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        row = pg(unit).conn.execute(
            "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s",
            (campaign_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def projection_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        row = pg(unit).conn.execute(
            "SELECT projection_revision FROM campaign.authz_state WHERE campaign_id = %s",
            (campaign_id,),
        ).fetchone()
        return None if row is None else int(row[0])


class InMemoryCampaignStore:
    """The twin. Its `create` **stages** the `authz_state` entry, standing in for
    the AFTER INSERT trigger: the fake's only path to a campaign is this method,
    so what a test observes matches PostgreSQL — including that neither the
    campaign nor its authorisation row is visible to anyone else until the
    transaction commits. The case the fake cannot have — a campaign inserted by
    raw SQL — is proved against the database in `tests/test_migrations_db.py`."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._rows: Staging[Campaign] = shared_rows(db, "campaigns")

    def create(
        self,
        unit: UnitOfWork,
        *,
        owner_id: int,
        name: str,
        tone: str | None = None,
        now: datetime | None = None,
        max_per_owner: int | None = None,
    ) -> Campaign:
        moment = now_or(now)
        twin = fake(unit)
        if max_per_owner is not None:
            twin.lock(AdvisoryLock.ACCOUNT_STORAGE, str(owner_id))
            count = sum(1 for c in self._rows.visible(twin).values() if c.owner_id == owner_id)
            if count >= max_per_owner:
                raise CampaignCapReached()
        campaign = Campaign(
            id=ident.new_id(ident.CAMPAIGN),
            owner_id=owner_id,
            name=check_name(name),
            created_at=moment,
            updated_at=moment,
            tone=check_tone(tone),
        )
        self._rows.add(twin, campaign.id, campaign)
        # Staged, not written: in PostgreSQL the trigger's row is invisible to
        # every other reader until the insert commits, so a second reader must
        # not be able to take this campaign's lock before then either.
        twin.create_authz_state(campaign.id)
        return campaign

    def get(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int) -> Campaign | None:
        found = self._rows.visible(fake(unit)).get(campaign_id)
        return found if found is not None and found.owner_id == owner_id else None

    def list_for_owner(self, unit: UnitOfWork, owner_id: int) -> list[Campaign]:
        mine = [c for c in self._rows.visible(fake(unit)).values() if c.owner_id == owner_id]
        return sorted(mine, key=lambda c: (c.created_at, c.id))

    def page_for_owner(
        self,
        unit: UnitOfWork,
        owner_id: int,
        *,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int = PAGE_MAX,
    ) -> Page[Campaign]:
        size = check_limit(limit)
        after = None if cursor is None else decode_cursor(cursor, ident.CAMPAIGN)
        mine = [
            c
            for c in self._rows.visible(fake(unit)).values()
            if c.owner_id == owner_id
            and (include_archived or not c.is_archived)
            and (after is None or _newest_key(c) < after)
        ]
        ordered = sorted(mine, key=_newest_key, reverse=True)
        return page_of(ordered[: size + 1], size, _newest_key)

    def rename(
        self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, name: str, now: datetime | None = None
    ) -> Campaign | None:
        named = check_name(name)
        twin = fake(unit)
        found = self._rows.visible(twin).get(campaign_id)
        if found is None or found.owner_id != owner_id:
            return None
        if found.name == named:
            return found
        # `replace`, never a constructor call naming the fields: a column added
        # later (0013's among them) is then carried rather than dropped.
        renamed = replace(found, name=named, updated_at=now_or(now))
        self._rows.replace(twin, campaign_id, renamed)
        return renamed

    def set_archived(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        archived: bool,
        now: datetime | None = None,
    ) -> bool:
        twin = fake(unit)
        found = self._rows.visible(twin).get(campaign_id)
        if found is None or found.owner_id != owner_id or found.is_archived == archived:
            return False
        moment = now_or(now)
        self._rows.replace(
            twin, campaign_id, replace(found, updated_at=moment, archived_at=moment if archived else None)
        )
        return True

    def set_tone(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        tone: str | None,
        now: datetime | None = None,
    ) -> Campaign | None:
        checked = check_tone(tone)
        twin = fake(unit)
        found = self._rows.visible(twin).get(campaign_id)
        if found is None or found.owner_id != owner_id:
            return None
        if found.tone == checked:
            return found
        changed = replace(found, tone=checked, updated_at=now_or(now))
        self._rows.replace(twin, campaign_id, changed)
        return changed

    def set_concluded(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        concluded: bool,
        now: datetime | None = None,
    ) -> bool:
        twin = fake(unit)
        found = self._rows.visible(twin).get(campaign_id)
        if found is None or found.owner_id != owner_id or found.is_concluded == concluded:
            return False
        moment = now_or(now)
        self._rows.replace(
            twin, campaign_id, replace(found, updated_at=moment, concluded_at=moment if concluded else None)
        )
        return True

    def authz_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        return fake(unit).authz_revision(campaign_id)

    def projection_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        return fake(unit).projection_revision(campaign_id)

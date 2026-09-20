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
can skip. The one deliberate exception is the player's unauthenticated path,
which holds no campaign: it locks the participant row and is authorised by the
code it presents, never by an id it was handed (RQ-5, RC-12).

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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from . import campaign_identity as ident
from .db import InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork


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


class ParticipantRemoved(CampaignStoreError):
    """That seat has been removed, so nothing may be minted against it (RQ-5,
    RC-13, AUD-16). A removal revokes the codes and the device credential; this
    is the other half — nothing issues a new one afterwards."""


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
    """

    def __init__(self) -> None:
        self._rows: dict[str, T] = {}
        self._staged: dict[int, dict[str, T]] = {}

    def _mine(self, unit: InMemoryTransaction) -> dict[str, T]:
        key = id(unit)
        if key not in self._staged:
            self._staged[key] = {}

            def publish() -> None:
                self._rows.update(self._staged.pop(key, {}))

            def discard() -> None:
                self._staged.pop(key, None)

            unit.on_publish(publish)
            unit.on_rollback(discard)
        return self._staged[key]

    def add(self, unit: InMemoryTransaction, key: str, row: T) -> None:
        """A new row, invisible to every other reader until this unit commits."""
        self._mine(unit)[key] = row

    def visible(self, unit: InMemoryTransaction) -> dict[str, T]:
        """Committed rows, plus this transaction's own uncommitted ones — what
        a uniqueness check must look at, and nothing more."""
        return {**self._rows, **self._staged.get(id(unit), {})}

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

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


NAME_MAX_CHARS = 120


def check_name(name: str) -> str:
    """The bound `0004_campaign_schema.sql` carries, applied before the statement
    so that the refusal is this one rather than an integrity error."""
    if not 1 <= len(name) <= NAME_MAX_CHARS:
        raise ValueError(f"a campaign name is 1 to {NAME_MAX_CHARS} characters")
    return name


class CampaignStore(Protocol):
    """Campaigns, and the authorisation revision every check reads."""

    def create(
        self, unit: UnitOfWork, *, owner_id: int, name: str, now: datetime | None = None
    ) -> Campaign:
        """Make a campaign owned by `owner_id`, together with its `authz_state`
        row at revision 0 — in PostgreSQL the AFTER INSERT trigger writes it, so
        that no path can leave a campaign without one (RQ-1)."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int) -> Campaign | None:
        """That owner's campaign, or None — which is also the answer for one that
        belongs to somebody else."""
        ...  # pragma: no cover - structural type

    def list_for_owner(self, unit: UnitOfWork, owner_id: int) -> list[Campaign]:
        """Oldest first."""
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

    def authz_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        """The campaign's authorisation revision, read under the shared campaign
        lock. It is written only by `UnitOfWork.advance_authz_revision`."""
        ...  # pragma: no cover - structural type


_COLUMNS = "id, owner_id, name, created_at, updated_at, archived_at"


def _campaign(row: tuple) -> Campaign:
    return Campaign(row[0], int(row[1]), row[2], row[3], row[4], row[5])


class PostgresCampaignStore:
    """`campaign.campaigns` and `campaign.authz_state` (migration 0004)."""

    def create(
        self, unit: UnitOfWork, *, owner_id: int, name: str, now: datetime | None = None
    ) -> Campaign:
        moment = now_or(now)
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.campaigns (id, owner_id, name, created_at, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (ident.new_id(ident.CAMPAIGN), owner_id, check_name(name), moment, moment),
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

    def authz_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        row = pg(unit).conn.execute(
            "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s",
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
        self, unit: UnitOfWork, *, owner_id: int, name: str, now: datetime | None = None
    ) -> Campaign:
        moment = now_or(now)
        campaign = Campaign(
            id=ident.new_id(ident.CAMPAIGN),
            owner_id=owner_id,
            name=check_name(name),
            created_at=moment,
            updated_at=moment,
        )
        twin = fake(unit)
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
            twin,
            campaign_id,
            Campaign(
                id=found.id,
                owner_id=found.owner_id,
                name=found.name,
                created_at=found.created_at,
                updated_at=moment,
                archived_at=moment if archived else None,
            ),
        )
        return True

    def authz_revision(self, unit: UnitOfWork, campaign_id: str) -> int | None:
        return fake(unit).authz_revision(campaign_id)

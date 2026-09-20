"""Campaigns, and the plumbing the three campaign stores share (1kg.2.1).

A campaign is a GM's table: the aggregate every participant, session, document
and reveal hangs off. This module holds its store — a `Protocol`, an in-memory
twin and a PostgreSQL implementation, the shape `service/auth_store.py`
established — and the few pieces the participant and table-session stores need
too, kept here rather than spelled three times.

**Ownership is in the query.** Every read and write names the owner in the same
statement as the row, so a campaign that is not the caller's is indistinguishable
from one that does not exist (`docs/migrations.md` section 4). There is no
"fetch, then check": a check that happens after the fetch is a check something
can skip.

**Mutations take the unit of work first**, like `JobQueue.enqueue`, so that
`1kg.2.2` and `1kg.2.3` can compose all three stores — and the audit writer —
in one transaction that commits or rolls back together.

**The twin owes two things to `InMemoryDatabase`** (`service/db.py`): every
write registers `on_rollback`, so a failed block leaves nothing behind, and a
row another reader must not see yet is staged and released with `on_publish`.
Both are what let a test exercise composition without a database at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from . import campaign_identity as ident
from .db import InMemoryTransaction, PgTransaction, UnitOfWork


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


# ── Plumbing the three stores share ──────────────────────────────────────────


def now_or(now: datetime | None) -> datetime:
    """A caller's clock, or this one. Every store method takes `now` so that a
    test can place a row in the past without sleeping."""
    return now if now is not None else datetime.now(UTC)


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


class Staging:
    """The twin's commit-time visibility, written once instead of in three stores.

    A fake write goes into the unit's own staging area, invisible to every other
    reader, and is published into the shared rows on commit or dropped on
    rollback — the contract `InMemoryTransaction` states and `InMemoryJobQueue`
    already follows. A store reads its own transaction's staged rows plus the
    committed ones; everyone else reads only the committed ones.
    """

    def __init__(self) -> None:
        self._rows: dict[str, Any] = {}
        self._staged: dict[int, dict[str, Any]] = {}

    def _mine(self, unit: InMemoryTransaction) -> dict[str, Any]:
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

    def add(self, unit: InMemoryTransaction, key: str, row: Any) -> None:
        """A new row, invisible to every other reader until this unit commits."""
        self._mine(unit)[key] = row

    def visible(self, unit: InMemoryTransaction) -> dict[str, Any]:
        """Committed rows, plus this transaction's own uncommitted ones."""
        return {**self._rows, **self._staged.get(id(unit), {})}

    def committed(self) -> dict[str, Any]:
        """What a reader outside this transaction can see."""
        return dict(self._rows)

    def replace(self, unit: InMemoryTransaction, key: str, row: Any) -> None:
        """Change a row that is already visible, registering how to take the
        change back. A committed row is changed in place — the twin's
        transactions are serial, so nobody is mid-read — and restored on
        rollback; one this unit staged is simply rewritten."""
        staged = self._staged.get(id(unit), {})
        if key in staged:
            staged[key] = row
            return
        before = self._rows[key]
        self._rows[key] = row
        unit.on_rollback(lambda: self._rows.__setitem__(key, before))


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
        rows = pg(unit).conn.execute(
            f"SELECT {_COLUMNS} FROM campaign.campaigns WHERE owner_id = %s ORDER BY created_at, id",
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
    """The twin. Its `create` writes the `authz_state` entry itself, standing in
    for the AFTER INSERT trigger: the fake's only path to a campaign is this
    method, so what a test observes matches PostgreSQL. The case the fake cannot
    have — a campaign inserted by raw SQL — is proved against the database in
    `tests/test_migrations_db.py`."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._rows = Staging()

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

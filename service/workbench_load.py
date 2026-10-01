"""The GM's Workbench load: one reader for X-5 and the pilot day, across tool
invocations and AI document edits (agent-forge-harness-1kg.5.5, I-3, I-4).

A count one route can skip is no cap, so a tool admission and an AI edit
admission read the same two numbers here, under the same lock: `in_flight`,
this GM's working, unexpired rows of both kinds, from ONE `UNION ALL`, each
tagged with its kind so the cap's sentence can say what holds it (C-15); and
`attempts_since`, every attempt of both kinds the whole pilot started since a
moment (C-5). `hold_in_flight_lock` takes the tool store's own member and key,
`(WORKBENCH_IN_FLIGHT, str(owner))`, after bounding the transaction (RQ-8).

**It writes nothing.** `_IN_FLIGHT` names the owner in both halves (SEC-2); the
transaction bound and the pilot-wide day count are the two statements that name
none. The twin reads the two stores' `shared_rows` tables through structural
views, so this module imports neither store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from .campaign_store import Staging, aware, shared_rows
from .db import AdvisoryLock, InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork

#: What holds a slot of the X-5 cap.
Kind = Literal["tool", "edit"]

# ── The seam ─────────────────────────────────────────────────────────────────


class WorkbenchLoad(Protocol):
    """What an admission reads of the GM's Workbench load — the whole surface."""

    def hold_in_flight_lock(self, unit: UnitOfWork, owner_id: int) -> None:
        """Bound the transaction, then take `(WORKBENCH_IN_FLIGHT, owner)` — the
        admitting transaction's first lock, the tool store's member and key."""
        ...  # pragma: no cover - structural type

    def in_flight(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[tuple[str, Kind]]:
        """This GM's working tool invocations AND AI edits before their deadline
        (RAIL-27), across campaigns, oldest first, ties by id in byte order."""
        ...  # pragma: no cover - structural type

    def in_flight_ids(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[str]:
        """`in_flight`'s ids, in its order: each the client's key of one kind."""
        ...  # pragma: no cover - structural type

    def attempts_since(self, unit: UnitOfWork, since: datetime) -> int:
        """Every tool and edit attempt, every GM's, started at or after `since`."""
        ...  # pragma: no cover - structural type


# ── PostgreSQL ───────────────────────────────────────────────────────────────

#: RQ-8: both bounds, local to the transaction, before its first lock. It reads
#: no row, which is why it is one of the two statements that name no owner.
_BOUND = "SELECT set_config('lock_timeout', %s, true), set_config('transaction_timeout', %s, true)"

#: ONE statement, so the two halves are read in one snapshot. Each half names
#: the owner. Oldest first; the tie-break compares ids byte by byte
#: (`COLLATE "C"`), as the twin's Python sort does, and then the kind, so two
#: rows of one instant are listed alike in both worlds whatever the database's
#: default collation is.
_IN_FLIGHT = (
    "SELECT invocation_id, kind FROM ("
    "SELECT invocation_id, created_at, 'tool' AS kind FROM campaign.tool_invocations "
    "WHERE owner_id = %s AND status = 'working' AND attempt_deadline > %s "
    "UNION ALL "
    "SELECT invocation_id, created_at, 'edit' AS kind FROM campaign.document_edits "
    "WHERE owner_id = %s AND status = 'working' AND attempt_deadline > %s"
    ') AS running ORDER BY created_at, invocation_id COLLATE "C", kind'
)
#: Pilot-wide on purpose (C-5): it names no owner and selects the count alone.
_ATTEMPTS_SINCE = (
    "SELECT (SELECT count(*) FROM campaign.tool_attempts WHERE started_at >= %s) "
    "+ (SELECT count(*) FROM campaign.document_edit_attempts WHERE started_at >= %s)"
)


def _pg(unit: UnitOfWork) -> PgTransaction:
    if not isinstance(unit, PgTransaction):
        raise TypeError("a PostgreSQL Workbench load reads inside a PostgreSQL transaction")
    return unit


def _fake(unit: UnitOfWork) -> InMemoryTransaction:
    if not isinstance(unit, InMemoryTransaction):
        raise TypeError("an in-memory Workbench load reads inside an in-memory transaction")
    return unit


def bound_transaction(unit: UnitOfWork) -> None:
    """Both bounds, once per transaction, before its first lock (RQ-8). The
    first bound a transaction sets is the one in force (`transaction_bound`).
    Every transaction of the edit aggregate that does not begin with
    `hold_in_flight_lock` calls this before its first row lock (C-9.2)."""
    if isinstance(unit, InMemoryTransaction):
        if not unit.transaction_bounds:
            unit.transaction_bound(None)
        return
    transaction = _pg(unit)
    if transaction.transaction_bounds:
        return
    transaction.conn.execute(
        _BOUND, (transaction.campaign_lock.lock_timeout, transaction.transaction_bound(None))
    )


class PostgresWorkbenchLoad:
    """`campaign.tool_invocations`, `campaign.document_edits` and their two
    attempt tables, read only."""

    def hold_in_flight_lock(self, unit: UnitOfWork, owner_id: int) -> None:
        bound_transaction(unit)
        _pg(unit).lock(AdvisoryLock.WORKBENCH_IN_FLIGHT, str(owner_id))

    def in_flight(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[tuple[str, Kind]]:
        moment = aware(now, "a clock")
        rows = _pg(unit).conn.execute(_IN_FLIGHT, (owner_id, moment, owner_id, moment)).fetchall()
        return [(str(r[0]), _kind(r[1])) for r in rows]

    def in_flight_ids(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[str]:
        return [invocation_id for invocation_id, _ in self.in_flight(unit, owner_id, now=now)]

    def attempts_since(self, unit: UnitOfWork, since: datetime) -> int:
        moment = aware(since, "a moment")
        row = _pg(unit).conn.execute(_ATTEMPTS_SINCE, (moment, moment)).fetchone()
        return 0 if row is None else int(row[0])


def _kind(value: object) -> Kind:
    if value == "tool":
        return "tool"
    if value == "edit":
        return "edit"
    raise ValueError("not a kind of Workbench work")


# ── The in-memory twin ───────────────────────────────────────────────────────


class _Running(Protocol):
    """What the twin reads of a stored tool invocation or edit row."""

    owner_id: int
    invocation_id: str
    status: str
    attempt_deadline: datetime
    created_at: datetime


class _Stored(Protocol):
    """A twin's stored row: both stores' twins keep the row under `.row`."""

    row: _Running


class _Started(Protocol):
    started_at: datetime


class InMemoryWorkbenchLoad:
    """The twin. It reads the `"tool_invocations"`, `"tool_attempts"`,
    `"document_edits"` and `"document_edit_attempts"` tables the two stores'
    twins write, with the commit-time visibility they give them, and writes
    nothing. Locks are the twin's recorded list; a race is a two-connection
    PostgreSQL test."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._running: dict[Kind, Staging[_Stored]] = {
            "tool": shared_rows(db, "tool_invocations"), "edit": shared_rows(db, "document_edits"),
        }
        self._attempts: tuple[Staging[_Started], ...] = (
            shared_rows(db, "tool_attempts"), shared_rows(db, "document_edit_attempts"),
        )

    def hold_in_flight_lock(self, unit: UnitOfWork, owner_id: int) -> None:
        twin = _fake(unit)
        bound_transaction(twin)
        twin.lock(AdvisoryLock.WORKBENCH_IN_FLIGHT, str(owner_id))

    def in_flight(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[tuple[str, Kind]]:
        twin = _fake(unit)
        moment = aware(now, "a clock")
        found = [
            (stored.row.created_at, stored.row.invocation_id, kind)
            for kind, rows in self._running.items()
            for stored in rows.visible(twin).values()
            if stored.row.owner_id == owner_id and stored.row.status == "working"
            and stored.row.attempt_deadline > moment
        ]
        found.sort()
        return [(invocation_id, kind) for _, invocation_id, kind in found]

    def in_flight_ids(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[str]:
        return [invocation_id for invocation_id, _ in self.in_flight(unit, owner_id, now=now)]

    def attempts_since(self, unit: UnitOfWork, since: datetime) -> int:
        twin = _fake(unit)
        moment = aware(since, "a moment")
        return sum(1 for rows in self._attempts for a in rows.visible(twin).values() if a.started_at >= moment)

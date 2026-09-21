"""The conversation timeline's storage seam (1kg.4.2).

Every statement the timeline issues lives here; `service/timeline.py` holds the
read model and no SQL at all. The shape is `service/campaign_store.py`'s — a
`Protocol`, a PostgreSQL implementation and an in-memory twin — and **every
method takes the unit of work first, reads included**, so one route read takes
one connection instead of two.

**Ownership is in the query** (`docs/migrations.md` section 4, SEC-2). Every
statement below names `conversation_id = %s`; a row that is not the caller's is
indistinguishable from one that does not exist, and there is no "fetch, then
check".

**This slice reads and never writes.** `chat.conversations` is `1kg.2.4`'s
table and `chat.messages` is nobody's to change: the two statements here are
read-only `SELECT`s naming only `conversation_id` and `user_id` of the first
and the 0001 columns of the second. Appending typed entries to
`chat.timeline_entries` — and the table itself — is slice A's; nothing here
names it.

**Why a raw row type rather than `service.models.StoredMessage`.** A legacy row
must be able to reach the adapter *unreadable*. `chat.messages.mode` carries no
CHECK (0001), so a value no `ChatMode` knows is a real row, and `StoredMessage`
coerces `mode` into the enum — it would raise at the store boundary and take
the whole page down, where requirement 6 says that row must become one `opaque`
entry with every other entry of the page still rendering. `LegacyMessageRow`
therefore carries the columns as the database holds them and judges nothing;
`service/timeline.py` decides what the contract can make of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .db import InMemoryTransaction, PgTransaction, UnitOfWork
from .history import MessageStore

#: How many of a conversation's rows the in-memory twin looks at before it
#: applies the cursor predicate — the fake's stand-in for a table PostgreSQL
#: scans with an index. A conversation anywhere near it is beyond what any test
#: seeds, and `tests/test_timeline_db.py` asserts that.
LEGACY_FAKE_WINDOW_ROWS = 10_000


class TimelineStoreError(Exception):
    """What a timeline store refuses to do."""


@dataclass(frozen=True)
class LegacyMessageRow:
    """One `chat.messages` row, exactly as the database holds it.

    `mode` and `role` are the stored strings, not enums: see the module
    docstring. `suggestions` is the raw JSONB value.
    """

    id: int
    mode: str
    role: str
    content: str
    suggestions: Any  # justification: raw JSONB; the contract validates it, not us.
    created_at: datetime


def pg(unit: UnitOfWork) -> PgTransaction:
    """The unit as a PostgreSQL transaction, or a refusal — a Postgres store
    handed the twin's unit would otherwise read nothing and report success."""
    if not isinstance(unit, PgTransaction):
        raise TypeError("a PostgreSQL timeline store reads inside a PostgreSQL transaction")
    return unit


def fake(unit: UnitOfWork) -> InMemoryTransaction:
    if not isinstance(unit, InMemoryTransaction):
        raise TypeError("an in-memory timeline store reads inside an in-memory transaction")
    return unit


class TimelineStore(Protocol):
    """What the timeline read model needs from a backend.

    Slice A adds `append`, `entry_window` and `covered_message_ids` here when
    `chat.timeline_entries` exists. Nothing else is ever added: no update, no
    delete, no count, no search.
    """

    def owner_of(self, unit: UnitOfWork, conversation_id: str) -> int | None:
        """The user this conversation belongs to, or `None`.

        `None` means *no ownership row* — which is **not** the same as *not
        yours*, and the route answers 404 to both (SEC-3: a resource that is
        missing, belongs to someone else, or was deleted gives the identical
        answer).
        """
        ...  # pragma: no cover - structural type

    def legacy_window(
        self, unit: UnitOfWork, conversation_id: str, *,
        before: tuple[datetime, int] | None, limit_rows: int,
    ) -> list[LegacyMessageRow]:
        """At most `limit_rows` of this conversation's `chat.messages` rows,
        **newest first** by `(created_at, id)`, strictly older than `before`.

        A window of ROWS, which the caller turns into a window of exchanges:
        reading newest-first, only the oldest row read is ambiguous, so the
        caller over-reads and discards a possibly-partial oldest group
        (requirement 7).
        """
        ...  # pragma: no cover - structural type


_LEGACY_COLUMNS = "id, mode, role, content, suggestions, created_at"
#: Newest first, and total: `created_at` alone is not unique.
_LEGACY_ORDER = "ORDER BY created_at DESC, id DESC LIMIT %s"


class PostgresTimelineStore:
    """`chat.conversations` and `chat.messages`, read-only (migration 0001)."""

    def owner_of(self, unit: UnitOfWork, conversation_id: str) -> int | None:
        row = pg(unit).conn.execute(
            "SELECT user_id FROM chat.conversations WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def legacy_window(
        self, unit: UnitOfWork, conversation_id: str, *,
        before: tuple[datetime, int] | None, limit_rows: int,
    ) -> list[LegacyMessageRow]:
        conn = pg(unit).conn
        if before is None:
            rows = conn.execute(
                f"SELECT {_LEGACY_COLUMNS} FROM chat.messages "
                f"WHERE conversation_id = %s {_LEGACY_ORDER}",
                (conversation_id, limit_rows),
            ).fetchall()
        else:
            # A row-value comparison, so the predicate and the ORDER BY are the
            # same total key and the index serves both.
            rows = conn.execute(
                f"SELECT {_LEGACY_COLUMNS} FROM chat.messages "
                f"WHERE conversation_id = %s AND (created_at, id) < (%s, %s) {_LEGACY_ORDER}",
                (conversation_id, before[0], before[1], limit_rows),
            ).fetchall()
        return [LegacyMessageRow(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]


class InMemoryTimelineStore:
    """The twin.

    `shared_rows` cannot help here and this is a fact, not a guess: it returns a
    table on an `InMemoryDatabase`, and neither conversations nor messages live
    on one — `InMemoryMessageStore` keeps its owners in a plain `dict` and its
    rows in a plain `list`. So the twin is constructed with the `MessageStore`
    itself and delegates to it, which is also what makes the parity suite's
    seeding parity: both worlds seed through `MessageStore`.
    """

    def __init__(
        # justification: an `InMemoryDatabase`, held and never used while this
        # slice only reads, so the twin is not pinned to one concrete database
        # class before slice A takes `shared_rows(db, "timeline_entries")` and
        # gives it a real type.
        self, db: Any, *, messages: MessageStore,
    ) -> None:
        # `db` is accepted (and unused while this slice only reads) so that the
        # twin is constructed exactly as slice A will need it, when its rows
        # take `shared_rows(db, "timeline_entries")`.
        self._db = db
        self._messages = messages

    def owner_of(self, unit: UnitOfWork, conversation_id: str) -> int | None:
        fake(unit)
        return self._messages.owner_of(conversation_id)

    def legacy_window(
        self, unit: UnitOfWork, conversation_id: str, *,
        before: tuple[datetime, int] | None, limit_rows: int,
    ) -> list[LegacyMessageRow]:
        fake(unit)
        rows = [
            LegacyMessageRow(
                id=m.id, mode=m.mode.value, role=m.role.value, content=m.content,
                suggestions=None if m.suggestions is None
                else [s.model_dump(mode="json") for s in m.suggestions],
                created_at=m.created_at,
            )
            for m in self._messages.recent(conversation_id, LEGACY_FAKE_WINDOW_ROWS)
        ]
        return _window(rows, before=before, limit_rows=limit_rows)


def _window(
    rows: Sequence[LegacyMessageRow], *, before: tuple[datetime, int] | None, limit_rows: int,
) -> list[LegacyMessageRow]:
    """The cursor predicate and the ordering, in Python — the same total key the
    SQL above uses, so the two worlds cannot disagree about what a page holds."""
    kept = [r for r in rows if before is None or (r.created_at, r.id) < before]
    kept.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return kept[:limit_rows]

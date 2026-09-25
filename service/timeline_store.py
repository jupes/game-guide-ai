"""The conversation timeline's storage seam (1kg.4.2).

Every statement the timeline issues lives here; `service/timeline.py` holds the
read model and no SQL at all. The shape is `service/campaign_store.py`'s — a
`Protocol`, a PostgreSQL implementation and an in-memory twin — and **every
method takes the unit of work first, reads included**, so one route read takes
one connection instead of two.

**Ownership is in the query** (`docs/migrations.md` section 4, SEC-2). Every
statement below names `conversation_id = %s`; a row that is not the caller's is
indistinguishable from one that does not exist, and there is no "fetch, then
check". `append` names the owning user in the statement too, and a missing or
foreign parent is refused by that guard — zero rows inserted — never by a
foreign-key violation, which would leave the transaction unusable (G-9).

**What this module writes, and what it only reads.** It writes
`chat.timeline_entries` and nothing else. `chat.conversations`
is `1kg.2.4`'s table and `chat.messages` is nobody's to change: the statements
naming them are read-only `SELECT`s over the 0001 columns (and the `SELECT`
inside `append`'s `INSERT`). It takes no lock of any kind: an entry is an
append-only child of a conversation, and nothing reads it under one.

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

import itertools
import json
import re
import secrets
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .campaign_identity import ID_BODY_MAX, ID_BODY_MIN, ID_BYTES
from .campaign_store import Staging, shared_rows
from .db import InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork
from .history import MessageStore
from .workbench_contracts import AnyEntry, TimelineEntry, redacted_errors

#: How many of a conversation's rows the in-memory twin looks at before it
#: applies the cursor predicate — the fake's stand-in for a table PostgreSQL
#: scans with an index. A conversation anywhere near it is beyond what any test
#: seeds, and `tests/test_timeline_db.py` asserts that.
LEGACY_FAKE_WINDOW_ROWS = 10_000

# ── Minting an entry id (requirement 2) ──────────────────────────────────────
#
# Not a member of `campaign_identity.PREFIXES`: that registry is the campaign
# domain's, and `0004` constrains every member of it. Its public floor and
# bounds are reused, so SEC-4's 128 bits and the length bound are still spelled
# once.

ENTRY_PREFIX: Final = "ent_"


def new_entry_id() -> str:
    """A timeline entry id: the prefix and 128 CSPRNG bits, URL-safe (SEC-4)."""
    return ENTRY_PREFIX + secrets.token_urlsafe(ID_BYTES)


def entry_id_check_regex() -> str:
    """The POSIX regular expression the migration that creates
    `chat.timeline_entries` constrains `entry_id` with. Generated here, so the
    database and the minting code cannot disagree about what an entry id is."""
    return rf"^{ENTRY_PREFIX}[A-Za-z0-9_-]{{{ID_BODY_MIN},{ID_BODY_MAX}}}$"


_MINTED: Final = re.compile(entry_id_check_regex())


# ── Refusals ─────────────────────────────────────────────────────────────────


class TimelineStoreError(Exception):
    """What a timeline store refuses to do."""


class EntryInvalid(TimelineStoreError, ValueError):
    """The entry is not one the contract can hold. It names WHERE, never what:
    the message is built from each error's location alone (SEC-20, X-7)."""

    def __init__(self, fields: Sequence[str]) -> None:
        super().__init__("the entry is not valid at: " + ", ".join(fields))
        self.fields = list(fields)


class EntryMismatch(TimelineStoreError, ValueError):
    """The payload disagrees with the row it would be written as. The row is the
    authority on identity and on time, and `entry_or_opaque` serves a
    placeholder for a payload naming another id — so a mismatch stored would be
    a turn that silently reads back as `opaque`."""


class EntryNotStored(TimelineStoreError, LookupError):
    """Zero rows inserted: the conversation is missing or not the caller's, a
    linked message row is not this conversation's, or the entry or a linked
    message row is already stored. One refusal, from a guard, in both worlds."""


# ── What is validated on write (requirement 4) ───────────────────────────────

#: Strict on the way in. `extra="forbid"` is every contract model's own setting
#: and nothing relaxes it here, so a document body, an assembled prompt,
#: attachment text, a provider payload or a user id cannot ride along in an
#: entry: the contract declares none of them.
_ENTRY: TypeAdapter[Any] = TypeAdapter(TimelineEntry, config=ConfigDict(hide_input_in_errors=True))
_FIELD_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _where(error: Mapping[str, Any]) -> str:
    """One error's location as a dotted path of declared names and indices.

    `redacted_errors` has already replaced an undeclared key — the caller's own
    text — with `(undeclared)`. Anything else that is not a declared-name shape
    ends the path, so no caller text can reach the message this builds.
    """
    parts: list[str] = []
    for part in error["loc"]:
        if isinstance(part, int) or part == "(undeclared)" or (
            isinstance(part, str) and _FIELD_NAME.fullmatch(part)
        ):
            parts.append(str(part))
            continue
        parts.append("?")
        break
    return ".".join(parts) or "(entry)"


def _checked(entry: AnyEntry | Mapping[str, Any], created_at: datetime) -> tuple[AnyEntry, str]:
    """The validated entry and its stored JSON, or a refusal — before any
    statement runs, in both worlds."""
    raw = entry.model_dump(mode="json") if isinstance(entry, BaseModel) else entry
    try:
        validated: AnyEntry = _ENTRY.validate_python(raw)
    except ValidationError as exc:
        # `from None`: the error list carries each `input`, and chaining it
        # would put the stored text one `__cause__` hop from any traceback.
        raise EntryInvalid([_where(e) for e in redacted_errors(exc.errors())]) from None
    if not _MINTED.fullmatch(validated.entry_id):
        # The id column is written from the payload's own `entry_id`, so the
        # two agree by construction; what is refused is an id the column's
        # CHECK would refuse — a legacy decimal id, say — because a failed
        # statement would leave the caller's transaction unusable.
        raise EntryMismatch("the entry's id is not one this module mints")
    if validated.created_at != created_at:
        raise EntryMismatch("the entry's created_at is not the row's")
    return validated, json.dumps(validated.model_dump(mode="json"))


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


@dataclass(frozen=True)
class StoredEntryRow:
    """One `chat.timeline_entries` row as the read model needs it. `payload` is
    served only through `entry_or_opaque`, never as it stands."""

    entry_id: str
    created_at: datetime
    seq: int
    payload: Any  # justification: raw JSONB; `entry_or_opaque` validates it, not us.
    user_message_id: int | None
    assistant_message_id: int | None


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
    """What the timeline needs from a backend — the whole surface. There is no
    update, no delete, no count and no search."""

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

    def append(
        self, unit: UnitOfWork, conversation_id: str, entry: AnyEntry | Mapping[str, Any],
        created_at: datetime, *, owner_id: int,
        user_message_id: int | None = None, assistant_message_id: int | None = None,
    ) -> str:
        """Validate the entry and store it as one row; answer its id.

        **The caller mints** (`new_entry_id`) and builds the entry around the
        id, and `created_at` is the row's time: an entry whose payload disagrees
        with either is `EntryMismatch`, and one the contract refuses is
        `EntryInvalid` — both before any statement runs. A conversation that is
        missing or not `owner_id`'s, or a linked message row that is not this
        conversation's, or an entry or message row already stored, is
        `EntryNotStored`, from a guard in the statement.
        """
        ...  # pragma: no cover - structural type

    def entry_window(
        self, unit: UnitOfWork, conversation_id: str, *,
        before: tuple[datetime, int] | None, limit: int,
    ) -> list[StoredEntryRow]:
        """At most `limit` of this conversation's entry rows, **newest first**
        by `(created_at, seq)`, strictly older than `before`."""
        ...  # pragma: no cover - structural type

    def covered_message_ids(
        self, unit: UnitOfWork, conversation_id: str, message_ids: Collection[int],
    ) -> set[int]:
        """Which of `message_ids` an entry of this conversation already carries
        — the legacy rows the read model must not render a second time."""
        ...  # pragma: no cover - structural type


_LEGACY_COLUMNS = "id, mode, role, content, suggestions, created_at"
#: Newest first, and total: `created_at` alone is not unique.
_LEGACY_ORDER = "ORDER BY created_at DESC, id DESC LIMIT %s"
_ENTRY_COLUMNS = "entry_id, created_at, seq, payload, user_message_id, assistant_message_id"
#: The same shape for entries: `seq` makes the key total.
_ENTRY_ORDER = "ORDER BY created_at DESC, seq DESC LIMIT %s"

#: Every value is cast, so no parameter's type is left to inference inside an
#: `INSERT … SELECT`. Each linked message row is guarded as a parent in the
#: statement (G-9), and `ON CONFLICT DO NOTHING` makes an entry or a message row
#: that is already stored zero rows inserted rather than a failed statement.
_INSERT_ENTRY = (
    "INSERT INTO chat.timeline_entries (entry_id, conversation_id, entry_kind, schema_version, "
    "created_at, payload, user_message_id, assistant_message_id) "
    "SELECT %s::text, c.conversation_id, %s::text, %s::integer, %s::timestamptz, %s::jsonb, "
    "%s::bigint, %s::bigint "
    "FROM chat.conversations c "
    "WHERE c.conversation_id = %s "
    "AND (%s::bigint IS NULL OR EXISTS (SELECT 1 FROM chat.messages m "
    "WHERE m.id = %s AND m.conversation_id = c.conversation_id)) "
    "AND (%s::bigint IS NULL OR EXISTS (SELECT 1 FROM chat.messages m "
    "WHERE m.id = %s AND m.conversation_id = c.conversation_id)) "
    "ON CONFLICT DO NOTHING RETURNING entry_id"
)
_COVERED = (
    "SELECT user_message_id, assistant_message_id FROM chat.timeline_entries "
    "WHERE conversation_id = %s "
    "AND (user_message_id = ANY(%s::bigint[]) OR assistant_message_id = ANY(%s::bigint[]))"
)


class PostgresTimelineStore:
    """`chat.timeline_entries`, and `chat.conversations` / `chat.messages` read-only."""

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

    def append(
        self, unit: UnitOfWork, conversation_id: str, entry: AnyEntry | Mapping[str, Any],
        created_at: datetime, *, owner_id: int,
        user_message_id: int | None = None, assistant_message_id: int | None = None,
    ) -> str:
        conn = pg(unit).conn
        validated, payload = _checked(entry, created_at)
        row = conn.execute(_INSERT_ENTRY, (
            validated.entry_id, validated.entry_kind, validated.schema_version, created_at, payload,
            user_message_id, assistant_message_id,
            conversation_id,
            user_message_id, user_message_id,
            assistant_message_id, assistant_message_id,
        )).fetchone()
        if row is None:
            raise EntryNotStored("the entry was not stored")
        return str(row[0])

    def entry_window(
        self, unit: UnitOfWork, conversation_id: str, *,
        before: tuple[datetime, int] | None, limit: int,
    ) -> list[StoredEntryRow]:
        conn = pg(unit).conn
        if before is None:
            rows = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM chat.timeline_entries "
                f"WHERE conversation_id = %s {_ENTRY_ORDER}",
                (conversation_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM chat.timeline_entries "
                f"WHERE conversation_id = %s AND (created_at, seq) < (%s, %s) {_ENTRY_ORDER}",
                (conversation_id, before[0], before[1], limit),
            ).fetchall()
        return [StoredEntryRow(r[0], r[1], int(r[2]), r[3], r[4], r[5]) for r in rows]

    def covered_message_ids(
        self, unit: UnitOfWork, conversation_id: str, message_ids: Collection[int],
    ) -> set[int]:
        wanted = set(message_ids)
        if not wanted:
            return set()
        ids = sorted(wanted)
        rows = pg(unit).conn.execute(_COVERED, (conversation_id, ids, ids)).fetchall()
        return {int(i) for r in rows for i in r if i is not None and int(i) in wanted}


#: The twin's `BIGSERIAL`: unique and increasing across every twin, and — like a
#: sequence — never given back by a rollback.
_TWIN_SEQ = itertools.count(1)


@dataclass(frozen=True)
class _TwinRow:
    """One entry row as the twin holds it. The payload is kept as JSON text and
    parsed on every read, as JSONB is: no reader can reach the stored value."""

    conversation_id: str
    entry_kind: str
    schema_version: int
    entry_id: str
    created_at: datetime
    seq: int
    payload_json: str
    user_message_id: int | None
    assistant_message_id: int | None

    def stored(self) -> StoredEntryRow:
        return StoredEntryRow(
            self.entry_id, self.created_at, self.seq, json.loads(self.payload_json),
            self.user_message_id, self.assistant_message_id,
        )


class InMemoryTimelineStore:
    """The twin.

    Its own rows live in `shared_rows(db, "timeline_entries")`, with the
    commit-time visibility and the rollback PostgreSQL gives them. Its parents
    cannot: neither conversations nor messages live on an `InMemoryDatabase` —
    `InMemoryMessageStore` keeps its owners in a plain `dict` and its rows in a
    plain `list`. So the twin is constructed with the `MessageStore` itself and
    refuses a missing or foreign parent by asking it, which is also what makes
    the parity suite's seeding parity: both worlds seed through `MessageStore`.
    """

    def __init__(self, db: InMemoryDatabase, *, messages: MessageStore) -> None:
        self._rows: Staging[_TwinRow] = shared_rows(db, "timeline_entries")
        self._messages = messages

    def owner_of(self, unit: UnitOfWork, conversation_id: str) -> int | None:
        fake(unit)
        return self._messages.owner_of(conversation_id)

    def _legacy_rows(self, conversation_id: str) -> list[LegacyMessageRow]:
        return [
            LegacyMessageRow(
                id=m.id, mode=m.mode.value, role=m.role.value, content=m.content,
                suggestions=None if m.suggestions is None
                else [s.model_dump(mode="json") for s in m.suggestions],
                created_at=m.created_at,
            )
            for m in self._messages.recent(conversation_id, LEGACY_FAKE_WINDOW_ROWS)
        ]

    def legacy_window(
        self, unit: UnitOfWork, conversation_id: str, *,
        before: tuple[datetime, int] | None, limit_rows: int,
    ) -> list[LegacyMessageRow]:
        fake(unit)
        return _window(self._legacy_rows(conversation_id), before=before, limit_rows=limit_rows)

    def append(
        self, unit: UnitOfWork, conversation_id: str, entry: AnyEntry | Mapping[str, Any],
        created_at: datetime, *, owner_id: int,
        user_message_id: int | None = None, assistant_message_id: int | None = None,
    ) -> str:
        tx = fake(unit)
        validated, payload = _checked(entry, created_at)
        linked = {i for i in (user_message_id, assistant_message_id) if i is not None}
        visible = self._rows.visible(tx)
        already = {
            i for r in visible.values() for i in (r.user_message_id, r.assistant_message_id) if i is not None
        }
        if (
            self._messages.owner_of(conversation_id) != owner_id
            or not linked <= {r.id for r in self._legacy_rows(conversation_id)}
            or validated.entry_id in visible
            or linked & already
        ):
            raise EntryNotStored("the entry was not stored")
        self._rows.add(tx, validated.entry_id, _TwinRow(
            conversation_id=conversation_id, entry_kind=validated.entry_kind,
            schema_version=validated.schema_version, entry_id=validated.entry_id,
            created_at=created_at, seq=next(_TWIN_SEQ), payload_json=payload,
            user_message_id=user_message_id, assistant_message_id=assistant_message_id,
        ))
        return validated.entry_id

    def entry_window(
        self, unit: UnitOfWork, conversation_id: str, *,
        before: tuple[datetime, int] | None, limit: int,
    ) -> list[StoredEntryRow]:
        tx = fake(unit)
        rows = [
            r for r in self._rows.visible(tx).values()
            if r.conversation_id == conversation_id and (before is None or (r.created_at, r.seq) < before)
        ]
        rows.sort(key=lambda r: (r.created_at, r.seq), reverse=True)
        return [r.stored() for r in rows[:limit]]

    def covered_message_ids(
        self, unit: UnitOfWork, conversation_id: str, message_ids: Collection[int],
    ) -> set[int]:
        tx = fake(unit)
        wanted = set(message_ids)
        return {
            i
            for r in self._rows.visible(tx).values() if r.conversation_id == conversation_id
            for i in (r.user_message_id, r.assistant_message_id) if i is not None and i in wanted
        }


def _window(
    rows: Sequence[LegacyMessageRow], *, before: tuple[datetime, int] | None, limit_rows: int,
) -> list[LegacyMessageRow]:
    """The cursor predicate and the ordering, in Python — the same total key the
    SQL above uses, so the two worlds cannot disagree about what a page holds."""
    kept = [r for r in rows if before is None or (r.created_at, r.id) < before]
    kept.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return kept[:limit_rows]

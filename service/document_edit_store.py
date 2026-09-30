"""The AI document edit aggregate's storage seam (agent-forge-harness-1kg.5.5, PR-1).

Two tables (the document edits migration): `campaign.document_edits`, one row
per edit keyed by `(owner, campaign, invocation_id)` — the client's own
idempotency key, in a key space of its own (I-2) — and
`campaign.document_edit_attempts`, **the admission record**, written in the
transaction that admits an attempt and before any provider work. The shape is
`service/tool_invocation_store.py`'s: a `Protocol`, a PostgreSQL implementation
and an in-memory twin on `shared_rows`, and **every method takes the unit of
work first**. The X-5 count and the pilot day are `service/workbench_load.py`'s,
which reads these tables and the tool tables together (I-3).

**Ownership is in the query** (SEC-2). Every statement names `owner_id = %s`,
with no exception: the transaction bound is `workbench_load.bound_transaction`'s.
The creating statement joins the owner's live campaign to the owner's
conversation of that campaign, to an `edit` timeline entry of that conversation
and to a document of that campaign of the named type, so a missing, foreign,
archived or mismatched parent is **zero rows inserted**, never a foreign-key
violation (G-9). There is no foreign key to the document (I-25).

**Locks** (RQ-3, RQ-8). Every row hold bounds its transaction first and is
`FOR NO KEY UPDATE`, announced with `note_row_lock()`. The order is the GM's
in-flight advisory lock (`WorkbenchLoad`), then the edit row, then the document.
It writes its own two tables and nothing else.

**Private text** (SEC-20, ED-26). The instruction, the selected text, the title
and the result are hidden from every `repr()`, no refusal names them, and no
digest of any of them is computed or stored. The selected text is forgotten
(`NULL`) by the statement that settles an edit in a final state (I-18, C-16).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Final, Protocol

from . import campaign_identity as ident
from .campaign_store import Campaign, Staging, aware, shared_rows
from .conversation_store import Conversation
from .db import InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork
from .tool_invocation_store import ATTEMPT_MAX, OUTCOMES, TERMINAL
from .workbench_contracts import (
    BRIEF_MAX_CHARS,
    PROSE_FIELD_MAX_CHARS,
    TEXT_FIELD_MAX_CHARS,
    WRITE_REVISION_MAX,
    DocumentTypeId,
    EditAction,
    check_stored_text,
)
from .workbench_load import bound_transaction

SCOPE_KINDS: Final = frozenset({"document", "field", "selection"})
INSTRUCTION_KINDS: Final = frozenset({"text", "action"})
ACTIONS: Final = frozenset(action.value for action in EditAction)
DOCUMENT_TYPES: Final = frozenset(doc_type.value for doc_type in DocumentTypeId)

_INVOCATION_ID: Final = re.compile(r"[A-Za-z0-9_-]{16,64}")
_FIELD_KEY: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")
#: `usage_ledger.OPERATION_ID_PATTERN`'s shape: the ledger's link to an attempt.
_OPERATION_ID: Final = re.compile(r"[0-9a-f]{32}")


# ── Refusals ─────────────────────────────────────────────────────────────────


class DocumentEditStoreError(Exception):
    """What a document edit store refuses to do. No message names an
    instruction, a selection, a title, a result, an invocation id or an owner."""


class EditNotStored(DocumentEditStoreError, LookupError):
    """Zero rows written: a guard of the statement refused a parent, the key or
    the entry is already taken, or — for a transition — the row is no longer in
    the state the caller read. One refusal, in both worlds."""

    def __init__(self) -> None:
        super().__init__("the edit was not written")


class EditInvalid(DocumentEditStoreError, ValueError):
    """A value the table's own CHECK would refuse, refused by the field's name
    before any statement runs: a failed statement would leave the caller's
    transaction unusable, and its `DETAIL` would carry the value (SEC-20)."""

    def __init__(self, what: str) -> None:
        super().__init__(f"not a value an edit can hold: {what}")
        self.what = what


# ── Rows ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewEdit:
    """What `create` writes beyond the key: the request as T1 verified it, and
    the entry's `DocumentLink` as it was then (C-11), set once, never updated."""

    conversation_id: str
    entry_id: str
    document_id: str
    document_type: str
    document_title: str = field(repr=False)
    scope_kind: str
    scope_field: str | None
    selection_start: int | None
    selection_end: int | None
    selection_text: str | None = field(repr=False)
    instruction_kind: str
    instruction_text: str | None = field(repr=False)
    instruction_action: str | None
    base_write_revision: int


@dataclass(frozen=True)
class EditRow(NewEdit):
    """One `campaign.document_edits` row, exactly as stored: what was created,
    its key and its state. `result` and `error` are the raw JSONB values, which
    the service validates through the contract before anything leaves the server."""

    owner_id: int
    campaign_id: str
    invocation_id: str = field(repr=False)
    status: str
    attempt: int
    attempt_deadline: datetime
    cancel_requested: bool
    # justification: raw JSONB; the service validates it through the contract.
    result: Any = field(repr=False)
    # justification: raw JSONB; the service validates it through the contract.
    error: Any
    schema_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EditAttemptRow:
    """One `campaign.document_edit_attempts` row: an admitted attempt, its
    ledger link (`operation_id`) and how it ended. No tokens, no price, no model."""

    owner_id: int
    campaign_id: str
    invocation_id: str = field(repr=False)
    attempt: int
    operation_id: str
    started_at: datetime
    deadline_at: datetime
    ended_at: datetime | None
    outcome: str | None


# ── Checks made before any statement, in both worlds ─────────────────────────


def _shaped(pattern: re.Pattern[str], value: object, what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise EditInvalid(what)
    return value


def _whole(value: object, low: int, high: int, what: str) -> int:
    """An integer in `[low, high]` — never a `bool`, which is an `int`."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise EditInvalid(what)
    return value


def _text(value: object, low: int, high: int, what: str) -> str:
    """Code points, as PostgreSQL's `length()` counts them, of text PostgreSQL
    can store (`check_stored_text`: no lone surrogate, no NUL)."""
    if not isinstance(value, str) or not low <= len(value) <= high:
        raise EditInvalid(what)
    try:
        check_stored_text(value)
    except ValueError:
        raise EditInvalid(what) from None
    return value


def _check_scope(new: NewEdit) -> None:
    if new.scope_kind not in SCOPE_KINDS:
        raise EditInvalid("scope_kind")
    if (new.scope_kind == "document") != (new.scope_field is None):
        raise EditInvalid("scope_field")
    if new.scope_field is not None:
        _shaped(_FIELD_KEY, new.scope_field, "scope_field")
    if new.scope_kind != "selection":
        if (new.selection_start, new.selection_end, new.selection_text) != (None, None, None):
            raise EditInvalid("selection")
        return
    start = _whole(new.selection_start, 0, PROSE_FIELD_MAX_CHARS - 1, "selection_start")
    end = _whole(new.selection_end, start + 1, PROSE_FIELD_MAX_CHARS, "selection_end")
    _text(new.selection_text, end - start, end - start, "selection_text")


def _check_instruction(new: NewEdit) -> None:
    if new.instruction_kind not in INSTRUCTION_KINDS:
        raise EditInvalid("instruction_kind")
    if new.instruction_kind == "text":
        _text(new.instruction_text, 1, BRIEF_MAX_CHARS, "instruction_text")
        if new.instruction_action is not None:
            raise EditInvalid("instruction_action")
        return
    if new.instruction_text is not None:
        raise EditInvalid("instruction_text")
    if new.instruction_action not in ACTIONS or new.scope_kind != "selection":
        raise EditInvalid("instruction_action")


def _check_new(
    new: NewEdit, *, invocation_id: str, attempt_deadline: datetime, now: datetime, schema_version: int,
) -> None:
    _shaped(_INVOCATION_ID, invocation_id, "invocation_id")
    if not isinstance(new.document_id, str) or not ident.is_id(ident.DOCUMENT, new.document_id):
        raise EditInvalid("document_id")
    if new.document_type not in DOCUMENT_TYPES:
        raise EditInvalid("document_type")
    _text(new.document_title, 1, TEXT_FIELD_MAX_CHARS, "document_title")
    _check_scope(new)
    _check_instruction(new)
    _whole(new.base_write_revision, 1, WRITE_REVISION_MAX, "base_write_revision")
    _whole(schema_version, 1, 2**31 - 1, "schema_version")
    _check_window(now, attempt_deadline)


def _check_window(started_at: datetime, deadline: datetime) -> None:
    """`document_edit_attempts_deadline_at_check`: an attempt ends after it starts."""
    aware(started_at, "an attempt's start")
    aware(deadline, "an attempt's deadline")
    if not deadline > started_at:
        raise EditInvalid("attempt_deadline")


def _check_retry(attempt: int, base_write_revision: int, now: datetime, deadline: datetime, operation_id: str) -> None:
    _whole(attempt, 1, ATTEMPT_MAX, "attempt")
    _whole(base_write_revision, 1, WRITE_REVISION_MAX, "base_write_revision")
    _check_window(now, deadline)
    _shaped(_OPERATION_ID, operation_id, "operation_id")


def _check_settlement(
    status: str, outcome: str, result: Mapping[str, Any] | None, error: Mapping[str, Any] | None,
    forget_selection: bool,
) -> None:
    """The two payload CHECKs and the outcome's agreement with the status."""
    if status not in TERMINAL:
        raise EditInvalid("status")
    if outcome not in OUTCOMES or (outcome != status and not (
        outcome == "expired" and status in {"failed", "cancelled"}
    )):
        raise EditInvalid("outcome")
    if (status == "done") != (result is not None):
        raise EditInvalid("result")
    if (status == "failed") != (error is not None):
        raise EditInvalid("error")
    if not isinstance(forget_selection, bool):
        raise EditInvalid("forget_selection")


def _json(value: Mapping[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(dict(value))


def pg(unit: UnitOfWork) -> PgTransaction:
    """The unit as a PostgreSQL transaction, or a refusal — a Postgres store
    handed the twin's unit would otherwise write nowhere and report success."""
    if not isinstance(unit, PgTransaction):
        raise TypeError("a PostgreSQL document edit store writes inside a PostgreSQL transaction")
    return unit


def fake(unit: UnitOfWork) -> InMemoryTransaction:
    if not isinstance(unit, InMemoryTransaction):
        raise TypeError("an in-memory document edit store writes inside an in-memory transaction")
    return unit


# ── The seam ─────────────────────────────────────────────────────────────────


class DocumentEditStore(Protocol):
    """What the AI edit service needs from a backend — the whole surface. There
    is no delete, no list and no search."""

    def get_for_update(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> EditRow | None:
        """Hold this GM's edit (`FOR NO KEY UPDATE`), or `None` — also for another GM's."""
        ...  # pragma: no cover - structural type

    def in_flight_for_document(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, document_id: str, *, now: datetime,
    ) -> list[str]:
        """This GM's working, unexpired edits of this document, from any
        conversation, oldest first (I-5)."""
        ...  # pragma: no cover - structural type

    def create(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str, new: NewEdit,
        operation_id: str, attempt_deadline: datetime, now: datetime, schema_version: int,
    ) -> EditRow:
        """A new `working` edit at attempt 1, and its attempt row. Every guard is
        in the statement: the owner's live campaign, the owner's conversation of
        it, an `edit` entry of that conversation, a document of that campaign of
        `new.document_type`. Any miss, or a key or entry taken, is `EditNotStored`."""
        ...  # pragma: no cover - structural type

    def start_retry(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        attempt: int, base_write_revision: int, operation_id: str, attempt_deadline: datetime,
        now: datetime,
    ) -> EditRow:
        """Attempt `attempt + 1`, on `base_write_revision`, of a `failed`,
        retryable edit on `attempt` below `ATTEMPT_MAX` — still holding its
        selected text when it is a selection — in the owner's live campaign, and
        its attempt row. The result, the error and the cancel flag are cleared.
        Anything else is `EditNotStored`."""
        ...  # pragma: no cover - structural type

    def fence(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, now: datetime,
    ) -> EditRow | None:
        """Hold the row only if it is `working`, on `attempt` and before its
        deadline (RAIL-27, AE-84); `None` otherwise."""
        ...  # pragma: no cover - structural type

    def settle(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, status: str, outcome: str, result: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None, forget_selection: bool, now: datetime,
    ) -> EditRow:
        """Move a `working` row on `attempt` to its terminal `status` and end
        that attempt with `outcome`; `forget_selection` sets the selected text to
        `NULL` in the same statement (I-18). The cancel flag is kept. A row in
        any other state is `EditNotStored`."""
        ...  # pragma: no cover - structural type

    def set_cancel_requested(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        now: datetime,
    ) -> EditRow | None:
        """Raise the cancel flag of a `working` or `done` row; `None` when
        nothing changed."""
        ...  # pragma: no cover - structural type

    def attempts(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> list[EditAttemptRow]:
        """This GM's edit's attempt rows, first attempt first."""
        ...  # pragma: no cover - structural type


# ── PostgreSQL ───────────────────────────────────────────────────────────────

#: `EditRow`'s field order.
_COLUMNS = (
    "conversation_id, entry_id, document_id, document_type, document_title, scope_kind, scope_field, "
    "selection_start, selection_end, selection_text, instruction_kind, instruction_text, "
    "instruction_action, base_write_revision, owner_id, campaign_id, invocation_id, status, attempt, "
    "attempt_deadline, cancel_requested, result, error, schema_version, created_at, updated_at"
)
_ATTEMPT_COLUMNS = (
    "owner_id, campaign_id, invocation_id, attempt, operation_id, started_at, deadline_at, "
    "ended_at, outcome"
)

_HOLD = (
    f"SELECT {_COLUMNS} FROM campaign.document_edits "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s FOR NO KEY UPDATE"
)
#: Oldest first, ties in byte order (`COLLATE "C"`), as the twin's sort is.
_FOR_DOCUMENT = (
    "SELECT invocation_id FROM campaign.document_edits "
    "WHERE owner_id = %s AND campaign_id = %s AND document_id = %s "
    "AND status = 'working' AND attempt_deadline > %s "
    'ORDER BY created_at, invocation_id COLLATE "C"'
)
#: The fence's predicates are in the statement that takes the lock, so a row
#: that no longer matches after a concurrent writer commits is not held.
_FENCE = (
    f"SELECT {_COLUMNS} FROM campaign.document_edits "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s "
    "AND status = 'working' AND attempt = %s AND attempt_deadline > %s FOR NO KEY UPDATE"
)
#: Every value is cast, so no parameter's type is left to inference inside an
#: `INSERT … SELECT`; `ON CONFLICT DO NOTHING` makes a key or an entry already
#: taken zero rows. The document's id and type are written from the guarded row.
_CREATE = (
    "INSERT INTO campaign.document_edits (owner_id, campaign_id, invocation_id, conversation_id, "
    "entry_id, document_id, document_type, document_title, scope_kind, scope_field, selection_start, "
    "selection_end, selection_text, instruction_kind, instruction_text, instruction_action, "
    "base_write_revision, status, attempt, attempt_deadline, cancel_requested, result, error, "
    "schema_version, created_at, updated_at) "
    "SELECT c.owner_id, c.id, %s::text, v.conversation_id, %s::text, d.id, d.type, %s::text, %s::text, "
    "%s::text, %s::integer, %s::integer, %s::text, %s::text, %s::text, %s::text, %s::bigint, "
    "'working', 1, %s::timestamptz, false, NULL, NULL, %s::integer, %s::timestamptz, %s::timestamptz "
    "FROM campaign.campaigns c "
    "JOIN chat.conversations v ON v.campaign_id = c.id "
    "JOIN campaign.documents d ON d.campaign_id = c.id "
    "WHERE c.id = %s AND c.owner_id = %s AND c.archived_at IS NULL "
    "AND v.conversation_id = %s AND v.user_id = %s "
    "AND d.id = %s AND d.type = %s "
    "AND EXISTS (SELECT 1 FROM chat.timeline_entries e WHERE e.entry_id = %s "
    "AND e.conversation_id = v.conversation_id AND e.entry_kind = 'edit') "
    f"ON CONFLICT DO NOTHING RETURNING {_COLUMNS}"
)
_START_RETRY = (
    "UPDATE campaign.document_edits t SET status = 'working', attempt = t.attempt + 1, "
    "attempt_deadline = %s, base_write_revision = %s, cancel_requested = false, result = NULL, "
    "error = NULL, updated_at = %s "
    "WHERE t.owner_id = %s AND t.campaign_id = %s AND t.invocation_id = %s "
    "AND t.status = 'failed' AND t.attempt = %s AND t.attempt < 100 "
    "AND (t.error ->> 'retryable') = 'true' "
    "AND (t.scope_kind <> 'selection' OR t.selection_text IS NOT NULL) "
    "AND EXISTS (SELECT 1 FROM campaign.campaigns c WHERE c.id = t.campaign_id "
    "AND c.owner_id = t.owner_id AND c.archived_at IS NULL) "
    f"RETURNING {_COLUMNS}"
)
_BEGIN_ATTEMPT = (
    "INSERT INTO campaign.document_edit_attempts (owner_id, campaign_id, invocation_id, attempt, "
    "operation_id, started_at, deadline_at) "
    "SELECT t.owner_id, t.campaign_id, t.invocation_id, t.attempt, %s::text, %s::timestamptz, "
    "t.attempt_deadline FROM campaign.document_edits t "
    "WHERE t.owner_id = %s AND t.campaign_id = %s AND t.invocation_id = %s "
    "AND t.status = 'working' AND t.attempt = %s "
    "ON CONFLICT DO NOTHING RETURNING attempt"
)
#: The selected text is forgotten by the statement that settles, never later.
_SETTLE = (
    "UPDATE campaign.document_edits SET status = %s, result = %s::jsonb, error = %s::jsonb, "
    "selection_text = CASE WHEN %s::boolean THEN NULL ELSE selection_text END, updated_at = %s "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s "
    f"AND status = 'working' AND attempt = %s RETURNING {_COLUMNS}"
)
_END_ATTEMPT = (
    "UPDATE campaign.document_edit_attempts SET ended_at = %s, outcome = %s "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s AND attempt = %s "
    "AND ended_at IS NULL RETURNING attempt"
)
_CANCEL = (
    "UPDATE campaign.document_edits SET cancel_requested = true, updated_at = %s "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s "
    f"AND status IN ('working', 'done') AND NOT cancel_requested RETURNING {_COLUMNS}"
)
_ATTEMPTS = (
    f"SELECT {_ATTEMPT_COLUMNS} FROM campaign.document_edit_attempts "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s ORDER BY attempt"
)


def _row(r: tuple[Any, ...]) -> EditRow:
    return EditRow(
        r[0], r[1], r[2], r[3], r[4], r[5], r[6], None if r[7] is None else int(r[7]),
        None if r[8] is None else int(r[8]), r[9], r[10], r[11], r[12], int(r[13]), int(r[14]), r[15],
        r[16], r[17], int(r[18]), r[19], bool(r[20]), r[21], r[22], int(r[23]), r[24], r[25],
    )


def _held(unit: UnitOfWork) -> PgTransaction:
    """Bound before the first lock, then announce the row lock (RQ-3, RQ-8)."""
    transaction = pg(unit)
    bound_transaction(transaction)
    transaction.note_row_lock()
    return transaction


class PostgresDocumentEditStore:
    """`campaign.document_edits` and `campaign.document_edit_attempts`; the
    campaign, conversation, timeline and document tables read in the guards only."""

    def get_for_update(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> EditRow | None:
        row = _held(unit).conn.execute(_HOLD, (owner_id, campaign_id, invocation_id)).fetchone()
        return None if row is None else _row(row)

    def in_flight_for_document(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, document_id: str, *, now: datetime,
    ) -> list[str]:
        rows = pg(unit).conn.execute(
            _FOR_DOCUMENT, (owner_id, campaign_id, document_id, aware(now, "a clock")),
        ).fetchall()
        return [str(r[0]) for r in rows]

    def create(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str, new: NewEdit,
        operation_id: str, attempt_deadline: datetime, now: datetime, schema_version: int,
    ) -> EditRow:
        _check_new(new, invocation_id=invocation_id, attempt_deadline=attempt_deadline, now=now,
                   schema_version=schema_version)
        _shaped(_OPERATION_ID, operation_id, "operation_id")
        transaction = pg(unit)
        row = transaction.conn.execute(_CREATE, (
            invocation_id, new.entry_id, new.document_title, new.scope_kind, new.scope_field,
            new.selection_start, new.selection_end, new.selection_text, new.instruction_kind,
            new.instruction_text, new.instruction_action, new.base_write_revision,
            attempt_deadline, schema_version, now, now,
            campaign_id, owner_id, new.conversation_id, owner_id,
            new.document_id, new.document_type, new.entry_id,
        )).fetchone()
        if row is None:
            raise EditNotStored()
        self._begin_attempt(transaction, owner_id, campaign_id, invocation_id, 1, operation_id, now)
        return _row(row)

    def start_retry(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        attempt: int, base_write_revision: int, operation_id: str, attempt_deadline: datetime,
        now: datetime,
    ) -> EditRow:
        _check_retry(attempt, base_write_revision, now, attempt_deadline, operation_id)
        transaction = pg(unit)
        row = transaction.conn.execute(_START_RETRY, (
            attempt_deadline, base_write_revision, now, owner_id, campaign_id, invocation_id, attempt,
        )).fetchone()
        if row is None:
            raise EditNotStored()
        self._begin_attempt(transaction, owner_id, campaign_id, invocation_id, attempt + 1, operation_id, now)
        return _row(row)

    @staticmethod
    def _begin_attempt(
        transaction: PgTransaction, owner_id: int, campaign_id: str, invocation_id: str,
        attempt: int, operation_id: str, now: datetime,
    ) -> None:
        begun = transaction.conn.execute(_BEGIN_ATTEMPT, (
            operation_id, now, owner_id, campaign_id, invocation_id, attempt,
        )).fetchone()
        if begun is None:
            raise EditNotStored()

    def fence(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, now: datetime,
    ) -> EditRow | None:
        row = _held(unit).conn.execute(
            _FENCE, (owner_id, campaign_id, invocation_id, attempt, aware(now, "a clock")),
        ).fetchone()
        return None if row is None else _row(row)

    def settle(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, status: str, outcome: str, result: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None, forget_selection: bool, now: datetime,
    ) -> EditRow:
        _check_settlement(status, outcome, result, error, forget_selection)
        conn = pg(unit).conn
        row = conn.execute(_SETTLE, (
            status, _json(result), _json(error), forget_selection, aware(now, "a clock"),
            owner_id, campaign_id, invocation_id, attempt,
        )).fetchone()
        if row is None:
            raise EditNotStored()
        ended = conn.execute(_END_ATTEMPT, (now, outcome, owner_id, campaign_id, invocation_id, attempt)).fetchone()
        if ended is None:
            raise EditNotStored()
        return _row(row)

    def set_cancel_requested(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        now: datetime,
    ) -> EditRow | None:
        row = pg(unit).conn.execute(
            _CANCEL, (aware(now, "a clock"), owner_id, campaign_id, invocation_id),
        ).fetchone()
        return None if row is None else _row(row)

    def attempts(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> list[EditAttemptRow]:
        rows = pg(unit).conn.execute(_ATTEMPTS, (owner_id, campaign_id, invocation_id)).fetchall()
        return [EditAttemptRow(int(r[0]), r[1], r[2], int(r[3]), r[4], r[5], r[6], r[7], r[8]) for r in rows]


# ── The in-memory twin ───────────────────────────────────────────────────────


class _EntryView(Protocol):
    """What the twin reads of the timeline twin's rows (structural: nothing
    private is imported)."""

    conversation_id: str
    entry_kind: str


class _DocumentView(Protocol):
    """What the twin reads of the document twin's rows; a deleted document is a
    tombstone there (`gone`), which no guard may see."""

    campaign_id: str
    type: str
    gone: bool


@dataclass(frozen=True)
class _TwinEdit:
    """One edit as the twin holds it, its payloads as JSON text parsed on every
    read, as JSONB is. `row` is what `InMemoryWorkbenchLoad` reads."""

    row: EditRow
    result_json: str | None = field(repr=False)
    error_json: str | None

    def read(self) -> EditRow:
        return replace(
            self.row,
            result=None if self.result_json is None else json.loads(self.result_json),
            error=None if self.error_json is None else json.loads(self.error_json),
        )


def _key(owner_id: int, campaign_id: str, invocation_id: str) -> str:
    # Neither id can hold a `/`: both are `[A-Za-z0-9_-]` by their CHECKs.
    return f"{owner_id}/{campaign_id}/{invocation_id}"


class InMemoryDocumentEditStore:
    """The twin. Its rows live in `shared_rows(db, "document_edits")` and
    `"document_edit_attempts"`, with PostgreSQL's commit-time visibility and
    rollback, and its guards read the SAME `"campaigns"`, `"conversations"`,
    `"timeline_entries"` and `"documents"` tables the other twins write, so a
    parent is refused here for the reason the statement refuses it there. Row
    locks are not modelled; a race is a two-connection PostgreSQL test."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._rows: Staging[_TwinEdit] = shared_rows(db, "document_edits")
        self._attempts: Staging[EditAttemptRow] = shared_rows(db, "document_edit_attempts")
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._conversations: Staging[Conversation] = shared_rows(db, "conversations")
        self._entries: Staging[_EntryView] = shared_rows(db, "timeline_entries")
        self._documents: Staging[_DocumentView] = shared_rows(db, "documents")

    def _get(self, twin: InMemoryTransaction, owner_id: int, campaign_id: str, invocation_id: str) -> EditRow | None:
        found = self._rows.visible(twin).get(_key(owner_id, campaign_id, invocation_id))
        return None if found is None else found.read()

    def _put(self, twin: InMemoryTransaction, row: EditRow) -> EditRow:
        self._rows.replace(twin, _key(row.owner_id, row.campaign_id, row.invocation_id), _TwinEdit(
            replace(row, result=None, error=None), _json(row.result), _json(row.error),
        ))
        return self._get(twin, row.owner_id, row.campaign_id, row.invocation_id) or row

    def _begin_attempt(self, twin: InMemoryTransaction, row: EditRow, operation_id: str, now: datetime) -> None:
        attempt_key = f"{_key(row.owner_id, row.campaign_id, row.invocation_id)}/{row.attempt}"
        taken = self._attempts.visible(twin)
        if attempt_key in taken or any(a.operation_id == operation_id for a in taken.values()):
            raise EditNotStored()
        self._attempts.add(twin, attempt_key, EditAttemptRow(
            row.owner_id, row.campaign_id, row.invocation_id, row.attempt, operation_id,
            now, row.attempt_deadline, None, None,
        ))

    def get_for_update(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> EditRow | None:
        twin = fake(unit)
        bound_transaction(twin)
        twin.note_row_lock()
        return self._get(twin, owner_id, campaign_id, invocation_id)

    def in_flight_for_document(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, document_id: str, *, now: datetime,
    ) -> list[str]:
        moment = aware(now, "a clock")
        rows = [
            found.row for found in self._rows.visible(fake(unit)).values()
            if (found.row.owner_id, found.row.campaign_id) == (owner_id, campaign_id)
            and found.row.document_id == document_id
            and found.row.status == "working" and found.row.attempt_deadline > moment
        ]
        rows.sort(key=lambda r: (r.created_at, r.invocation_id))
        return [r.invocation_id for r in rows]

    def create(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str, new: NewEdit,
        operation_id: str, attempt_deadline: datetime, now: datetime, schema_version: int,
    ) -> EditRow:
        twin = fake(unit)
        _check_new(new, invocation_id=invocation_id, attempt_deadline=attempt_deadline, now=now,
                   schema_version=schema_version)
        _shaped(_OPERATION_ID, operation_id, "operation_id")
        campaign = self._campaigns.visible(twin).get(campaign_id)
        conversation = self._conversations.visible(twin).get(new.conversation_id)
        carrying = self._entries.visible(twin).get(new.entry_id)
        document = self._documents.visible(twin).get(new.document_id)
        taken = self._rows.visible(twin)
        if (
            campaign is None or campaign.owner_id != owner_id or campaign.is_archived
            or conversation is None or conversation.owner_id != owner_id
            or conversation.campaign_id != campaign_id
            or carrying is None or carrying.conversation_id != new.conversation_id
            or carrying.entry_kind != "edit"
            or document is None or document.gone or document.campaign_id != campaign_id
            or document.type != new.document_type
            or _key(owner_id, campaign_id, invocation_id) in taken
            or any(found.row.entry_id == new.entry_id for found in taken.values())
        ):
            raise EditNotStored()
        stored = self._put(twin, EditRow(
            **asdict(new), owner_id=owner_id, campaign_id=campaign_id, invocation_id=invocation_id,
            status="working", attempt=1, attempt_deadline=attempt_deadline, cancel_requested=False,
            result=None, error=None, schema_version=schema_version, created_at=now, updated_at=now,
        ))
        self._begin_attempt(twin, stored, operation_id, now)
        return stored

    def start_retry(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        attempt: int, base_write_revision: int, operation_id: str, attempt_deadline: datetime,
        now: datetime,
    ) -> EditRow:
        twin = fake(unit)
        _check_retry(attempt, base_write_revision, now, attempt_deadline, operation_id)
        found = self._get(twin, owner_id, campaign_id, invocation_id)
        campaign = self._campaigns.visible(twin).get(campaign_id)
        if (
            found is None or found.status != "failed" or found.attempt != attempt
            or found.attempt >= ATTEMPT_MAX
            or not isinstance(found.error, Mapping) or found.error.get("retryable") is not True
            or (found.scope_kind == "selection" and found.selection_text is None)
            or campaign is None or campaign.owner_id != owner_id or campaign.is_archived
        ):
            raise EditNotStored()
        stored = self._put(twin, replace(
            found, status="working", attempt=found.attempt + 1, attempt_deadline=attempt_deadline,
            base_write_revision=base_write_revision, cancel_requested=False, result=None, error=None,
            updated_at=now,
        ))
        self._begin_attempt(twin, stored, operation_id, now)
        return stored

    def fence(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, now: datetime,
    ) -> EditRow | None:
        twin = fake(unit)
        bound_transaction(twin)
        twin.note_row_lock()
        moment = aware(now, "a clock")
        found = self._get(twin, owner_id, campaign_id, invocation_id)
        if found is None or found.status != "working" or found.attempt != attempt:
            return None
        return found if found.attempt_deadline > moment else None

    def settle(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, status: str, outcome: str, result: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None, forget_selection: bool, now: datetime,
    ) -> EditRow:
        twin = fake(unit)
        _check_settlement(status, outcome, result, error, forget_selection)
        moment = aware(now, "a clock")
        found = self._get(twin, owner_id, campaign_id, invocation_id)
        attempt_key = f"{_key(owner_id, campaign_id, invocation_id)}/{attempt}"
        running = self._attempts.visible(twin).get(attempt_key)
        if (
            found is None or found.status != "working" or found.attempt != attempt
            or running is None or running.ended_at is not None
        ):
            raise EditNotStored()
        stored = self._put(twin, replace(
            found, status=status, result=None if result is None else dict(result),
            error=None if error is None else dict(error), updated_at=moment,
            selection_text=None if forget_selection else found.selection_text,
        ))
        self._attempts.replace(twin, attempt_key, replace(running, ended_at=moment, outcome=outcome))
        return stored

    def set_cancel_requested(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        now: datetime,
    ) -> EditRow | None:
        twin = fake(unit)
        moment = aware(now, "a clock")
        found = self._get(twin, owner_id, campaign_id, invocation_id)
        if found is None or found.status not in {"working", "done"} or found.cancel_requested:
            return None
        return self._put(twin, replace(found, cancel_requested=True, updated_at=moment))

    def attempts(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> list[EditAttemptRow]:
        rows = [
            a for a in self._attempts.visible(fake(unit)).values()
            if (a.owner_id, a.campaign_id, a.invocation_id) == (owner_id, campaign_id, invocation_id)
        ]
        return sorted(rows, key=lambda a: a.attempt)

"""The GM tool invocation aggregate's storage seam (1kg.4.1, slice A).

Two tables (`0014_tool_invocations.sql`): `campaign.tool_invocations`, one row
per invocation keyed by `(owner, campaign, invocation_id)` — the client's own
idempotency key, scoped to the GM and the campaign — and
`campaign.tool_attempts`, **the admission record**: one row per attempt,
written in the transaction that admits it and before any provider work. The
shape is `service/campaign_store.py`'s: a `Protocol`, a PostgreSQL
implementation and an in-memory twin on `shared_rows`, and **every method takes
the unit of work first**.

**Ownership is in the query** (`docs/migrations.md` section 4, SEC-2). Every
statement names `owner_id = %s`, with exactly two exceptions the static test
names: the transaction bound (`set_config`, which reads no row) and
`attempts_since`, the pilot-wide day count that `CHAT_DAILY_CAP` is (C-5) and
which selects `count(*)` alone. The creating statement joins the owner's live
campaign to the owner's conversation of that campaign — and, when one is named,
to a source entry of that conversation — so a missing, foreign, archived or
mismatched parent is **zero rows inserted**, never a foreign-key violation:
`campaign_id`'s edge is deferred and would only fail at `COMMIT` (G-9).

**Locks** (RQ-3, RQ-8, the lead's C-3). Every transaction of this aggregate is
bounded before its first lock — `lock_timeout` and `transaction_timeout`, in one
`set_config` — and every explicit row hold is `FOR NO KEY UPDATE`, announced
with `note_row_lock()` first. The per-GM advisory lock
`AdvisoryLock.WORKBENCH_IN_FLIGHT` is taken first in the admitting transaction
and nowhere else.

**What this module writes, and what it only reads.** It writes its own two
tables and nothing else. `campaign.campaigns`, `chat.conversations` and
`chat.timeline_entries` are read inside the guards; the timeline entry that
carries an invocation is `service/timeline_store.py`'s to write.

**Private text** (SEC-20, ED-26). A brief and a result are the GM's: they are
hidden from every `repr()`, no refusal names them, and no digest of either is
computed or stored. The client-chosen `invocation_id` is hidden from `repr()`
too (I-24).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final, Protocol

from .campaign_store import Campaign, Staging, aware, shared_rows
from .conversation_store import Conversation
from .db import AdvisoryLock, InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork
from .workbench_contracts import BRIEF_MAX_CHARS, InvocationStatus, ToolId

#: `tool_invocations.attempt`'s bound, and `ToolInvocation.attempt`'s.
ATTEMPT_MAX: Final = 100
STATUSES: Final = frozenset(status.value for status in InvocationStatus)
#: What ended an attempt. `expired` is the lazy transition of a row found past
#: its deadline (RAIL-27); its invocation then reads `failed` or `cancelled`.
OUTCOMES: Final = frozenset({"done", "failed", "cancelled", "expired"})
TERMINAL: Final = frozenset({"done", "failed", "cancelled"})

_INVOCATION_ID: Final = re.compile(r"[A-Za-z0-9_-]{16,64}")
_OPAQUE_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: `usage_ledger.OPERATION_ID_PATTERN`'s shape: the ledger's link to an attempt.
_OPERATION_ID: Final = re.compile(r"[0-9a-f]{32}")


# ── Refusals ─────────────────────────────────────────────────────────────────


class ToolInvocationStoreError(Exception):
    """What a tool invocation store refuses to do. No message names a brief, a
    result, an invocation id or an owner."""


class InvocationNotStored(ToolInvocationStoreError, LookupError):
    """Zero rows written: the campaign is missing, foreign or archived, the
    conversation is not the owner's or not that campaign's, the source or the
    carrying entry is not that conversation's, the key is already taken — or,
    for a transition, the row is no longer in the state the caller read. One
    refusal, from a guard in the statement, in both worlds."""

    def __init__(self) -> None:
        super().__init__("the invocation was not written")


class InvocationInvalid(ToolInvocationStoreError, ValueError):
    """A value the table's own CHECK would refuse, refused by the field's name
    before any statement runs — a failed statement would leave the caller's
    transaction unusable."""

    def __init__(self, what: str) -> None:
        super().__init__(f"not a value a tool invocation can hold: {what}")
        self.what = what


# ── Rows ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InvocationRow:
    """One `campaign.tool_invocations` row, exactly as stored. `result` and
    `error` are the raw JSONB values: the service validates them through the
    contract before anything leaves the server (I-25), never this module."""

    owner_id: int
    campaign_id: str
    invocation_id: str = field(repr=False)
    conversation_id: str
    entry_id: str
    tool_id: str
    brief: str = field(repr=False)
    source_entry_id: str | None
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
class AttemptRow:
    """One `campaign.tool_attempts` row: an admitted attempt, its ledger link
    (`operation_id`) and how it ended. No tokens, no price, no model."""

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
        raise InvocationInvalid(what)
    return value


def _check_new(
    *, invocation_id: str, tool_id: str, brief: str, source_entry_id: str | None,
    attempt_deadline: datetime, now: datetime, schema_version: int,
) -> None:
    _shaped(_INVOCATION_ID, invocation_id, "invocation_id")
    if tool_id not in {tool.value for tool in ToolId}:
        raise InvocationInvalid("tool_id")
    # Code points, as PostgreSQL's length() counts them in UTF-8 and as the
    # contract's RAIL-6 bound does.
    if not isinstance(brief, str) or len(brief) > BRIEF_MAX_CHARS:
        raise InvocationInvalid("brief")
    if source_entry_id is not None:
        _shaped(_OPAQUE_ID, source_entry_id, "source_entry_id")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise InvocationInvalid("schema_version")
    _check_window(now, attempt_deadline)


def _check_window(started_at: datetime, deadline: datetime) -> None:
    """`tool_attempts_deadline_at_check`: an attempt ends after it starts."""
    aware(started_at, "an attempt's start")
    aware(deadline, "an attempt's deadline")
    if not deadline > started_at:
        raise InvocationInvalid("attempt_deadline")


def _check_attempt(attempt: int) -> int:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= ATTEMPT_MAX:
        raise InvocationInvalid("attempt")
    return attempt


def _check_settlement(
    status: str, outcome: str, result: Mapping[str, Any] | None, error: Mapping[str, Any] | None,
) -> None:
    """The two payload CHECKs and the outcome's agreement with the status."""
    if status not in TERMINAL:
        raise InvocationInvalid("status")
    if outcome not in OUTCOMES or (outcome != status and not (
        outcome == "expired" and status in {"failed", "cancelled"}
    )):
        raise InvocationInvalid("outcome")
    if (status == "done") != (result is not None):
        raise InvocationInvalid("result")
    if (status == "failed") != (error is not None):
        raise InvocationInvalid("error")


def _json(value: Mapping[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(dict(value))


def pg(unit: UnitOfWork) -> PgTransaction:
    """The unit as a PostgreSQL transaction, or a refusal — a Postgres store
    handed the twin's unit would otherwise write nowhere and report success."""
    if not isinstance(unit, PgTransaction):
        raise TypeError("a PostgreSQL tool invocation store writes inside a PostgreSQL transaction")
    return unit


def fake(unit: UnitOfWork) -> InMemoryTransaction:
    if not isinstance(unit, InMemoryTransaction):
        raise TypeError("an in-memory tool invocation store writes inside an in-memory transaction")
    return unit


# ── The seam ─────────────────────────────────────────────────────────────────


class ToolInvocationStore(Protocol):
    """What the tool invocation service needs from a backend — the whole
    surface. There is no delete, no list and no search (§4 of the brief)."""

    def hold_in_flight_lock(self, unit: UnitOfWork, owner_id: int) -> None:
        """Bound the transaction, then take `(WORKBENCH_IN_FLIGHT, owner)` —
        the admitting transaction's first lock. Never taken anywhere else."""
        ...  # pragma: no cover - structural type

    def in_flight_ids(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[str]:
        """This GM's working invocations whose deadline is still ahead, across
        every campaign, oldest first. A past-deadline row counts for nothing,
        whether or not anything has transitioned it yet (RAIL-27)."""
        ...  # pragma: no cover - structural type

    def get_for_update(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> InvocationRow | None:
        """Hold this GM's invocation still (`FOR NO KEY UPDATE`) for the rest of
        the transaction, or `None` — which is also the answer for another GM's."""
        ...  # pragma: no cover - structural type

    def create(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        conversation_id: str, entry_id: str, tool_id: str, brief: str,
        source_entry_id: str | None, operation_id: str, attempt_deadline: datetime,
        now: datetime, schema_version: int,
    ) -> InvocationRow:
        """A new `working` invocation at attempt 1, and its attempt row.

        Every guard is in the statement (C-4): the campaign is the owner's and
        not archived, the conversation is the owner's and that campaign's, the
        carrying entry is a `tool` entry of that conversation, and the source
        entry, when named, is an entry of that conversation. Any miss, or a key
        or entry already taken, is `InvocationNotStored`."""
        ...  # pragma: no cover - structural type

    def start_retry(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        attempt: int, operation_id: str, attempt_deadline: datetime, now: datetime,
    ) -> InvocationRow:
        """Attempt `attempt + 1` of a `failed`, retryable invocation that is on
        attempt `attempt`, below `ATTEMPT_MAX`, in a campaign that is still the
        owner's and not archived — all in the statement — and its attempt row.
        The result, the error and the cancel flag are cleared. Anything else is
        `InvocationNotStored`."""
        ...  # pragma: no cover - structural type

    def fence(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, now: datetime,
    ) -> InvocationRow | None:
        """Hold the row only if it is still `working`, still on `attempt`, and
        before its deadline (RAIL-27, AE-84); `None` otherwise. A completion
        commits only through a row this answered."""
        ...  # pragma: no cover - structural type

    def settle(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, status: str, outcome: str, result: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None, now: datetime,
    ) -> InvocationRow:
        """Move a `working` row on `attempt` to its terminal `status`, and end
        that attempt's row with `outcome`. The cancel flag is kept as it is.
        A row in any other state is `InvocationNotStored`."""
        ...  # pragma: no cover - structural type

    def set_cancel_requested(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        now: datetime,
    ) -> InvocationRow | None:
        """Raise the cancel flag of a `working` or `done` row (on `done` it means
        "finished before it could be cancelled", RAIL-23). `None` when nothing
        changed: no such row of this GM's, a failed or cancelled one, or a flag
        already raised."""
        ...  # pragma: no cover - structural type

    def attempts(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> list[AttemptRow]:
        """This GM's invocation's attempt rows, first attempt first."""
        ...  # pragma: no cover - structural type

    def attempts_since(self, unit: UnitOfWork, since: datetime) -> int:
        """How many tool attempts the whole pilot has started at or after
        `since` — every GM's, because `CHAT_DAILY_CAP` is pilot-wide (C-5)."""
        ...  # pragma: no cover - structural type


# ── PostgreSQL ───────────────────────────────────────────────────────────────

_COLUMNS = (
    "owner_id, campaign_id, invocation_id, conversation_id, entry_id, tool_id, brief, "
    "source_entry_id, status, attempt, attempt_deadline, cancel_requested, result, error, "
    "schema_version, created_at, updated_at"
)
_ATTEMPT_COLUMNS = (
    "owner_id, campaign_id, invocation_id, attempt, operation_id, started_at, deadline_at, "
    "ended_at, outcome"
)

#: RQ-8 as C-3 applies it: both bounds, local to the transaction, before its
#: first lock. It reads no row, which is why it is one of the two statements
#: that name no owner.
_BOUND = "SELECT set_config('lock_timeout', %s, true), set_config('transaction_timeout', %s, true)"

_IN_FLIGHT = (
    "SELECT invocation_id FROM campaign.tool_invocations "
    "WHERE owner_id = %s AND status = 'working' AND attempt_deadline > %s "
    "ORDER BY created_at, invocation_id"
)
_HOLD = (
    f"SELECT {_COLUMNS} FROM campaign.tool_invocations "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s FOR NO KEY UPDATE"
)
#: The fence's three predicates are in the statement that takes the lock, so a
#: row that no longer matches after a concurrent writer commits is not held at
#: all — READ COMMITTED re-evaluates the WHERE against the newer version.
_FENCE = (
    f"SELECT {_COLUMNS} FROM campaign.tool_invocations "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s "
    "AND status = 'working' AND attempt = %s AND attempt_deadline > %s FOR NO KEY UPDATE"
)
#: Every value is cast, so no parameter's type is left to inference inside an
#: `INSERT … SELECT`. `ON CONFLICT DO NOTHING` makes a key or an entry already
#: taken zero rows rather than a failed statement.
_CREATE = (
    "INSERT INTO campaign.tool_invocations (owner_id, campaign_id, invocation_id, conversation_id, "
    "entry_id, tool_id, brief, source_entry_id, status, attempt, attempt_deadline, cancel_requested, "
    "result, error, schema_version, created_at, updated_at) "
    "SELECT c.owner_id, c.id, %s::text, v.conversation_id, %s::text, %s::text, %s::text, %s::text, "
    "'working', 1, %s::timestamptz, false, NULL, NULL, %s::integer, %s::timestamptz, %s::timestamptz "
    "FROM campaign.campaigns c "
    "JOIN chat.conversations v ON v.campaign_id = c.id "
    "WHERE c.id = %s AND c.owner_id = %s AND c.archived_at IS NULL "
    "AND v.conversation_id = %s AND v.user_id = %s "
    "AND EXISTS (SELECT 1 FROM chat.timeline_entries e WHERE e.entry_id = %s "
    "AND e.conversation_id = v.conversation_id AND e.entry_kind = 'tool') "
    "AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM chat.timeline_entries s WHERE s.entry_id = %s "
    "AND s.conversation_id = v.conversation_id)) "
    f"ON CONFLICT DO NOTHING RETURNING {_COLUMNS}"
)
_START_RETRY = (
    "UPDATE campaign.tool_invocations t SET status = 'working', attempt = t.attempt + 1, "
    "attempt_deadline = %s, cancel_requested = false, error = NULL, updated_at = %s "
    "WHERE t.owner_id = %s AND t.campaign_id = %s AND t.invocation_id = %s "
    "AND t.status = 'failed' AND t.attempt = %s AND t.attempt < 100 "
    "AND (t.error ->> 'retryable') = 'true' "
    "AND EXISTS (SELECT 1 FROM campaign.campaigns c WHERE c.id = t.campaign_id "
    "AND c.owner_id = t.owner_id AND c.archived_at IS NULL) "
    f"RETURNING {_COLUMNS}"
)
_BEGIN_ATTEMPT = (
    "INSERT INTO campaign.tool_attempts (owner_id, campaign_id, invocation_id, attempt, "
    "operation_id, started_at, deadline_at) "
    "SELECT t.owner_id, t.campaign_id, t.invocation_id, t.attempt, %s::text, %s::timestamptz, "
    "t.attempt_deadline FROM campaign.tool_invocations t "
    "WHERE t.owner_id = %s AND t.campaign_id = %s AND t.invocation_id = %s "
    "AND t.status = 'working' AND t.attempt = %s "
    "ON CONFLICT DO NOTHING RETURNING attempt"
)
_SETTLE = (
    "UPDATE campaign.tool_invocations SET status = %s, result = %s::jsonb, error = %s::jsonb, "
    "updated_at = %s "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s "
    f"AND status = 'working' AND attempt = %s RETURNING {_COLUMNS}"
)
_END_ATTEMPT = (
    "UPDATE campaign.tool_attempts SET ended_at = %s, outcome = %s "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s AND attempt = %s "
    "AND ended_at IS NULL RETURNING attempt"
)
_CANCEL = (
    "UPDATE campaign.tool_invocations SET cancel_requested = true, updated_at = %s "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s "
    f"AND status IN ('working', 'done') AND NOT cancel_requested RETURNING {_COLUMNS}"
)
_ATTEMPTS = (
    f"SELECT {_ATTEMPT_COLUMNS} FROM campaign.tool_attempts "
    "WHERE owner_id = %s AND campaign_id = %s AND invocation_id = %s ORDER BY attempt"
)
#: Pilot-wide on purpose (C-5): it names no owner and selects the count alone.
_ATTEMPTS_SINCE = "SELECT count(*) FROM campaign.tool_attempts WHERE started_at >= %s"


def _row(r: tuple[Any, ...]) -> InvocationRow:
    return InvocationRow(
        int(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], int(r[9]), r[10], bool(r[11]),
        r[12], r[13], int(r[14]), r[15], r[16],
    )


def _attempt(r: tuple[Any, ...]) -> AttemptRow:
    return AttemptRow(int(r[0]), r[1], r[2], int(r[3]), r[4], r[5], r[6], r[7], r[8])


def _bound_pg(transaction: PgTransaction) -> None:
    """Both bounds, once per transaction, before its first lock (C-3). The
    first bound a transaction sets is the one in force (`transaction_bound`)."""
    if transaction.transaction_bounds:
        return
    transaction.conn.execute(
        _BOUND, (transaction.campaign_lock.lock_timeout, transaction.transaction_bound(None))
    )


class PostgresToolInvocationStore:
    """`campaign.tool_invocations` and `campaign.tool_attempts`; the campaign,
    conversation and timeline tables read inside the guards only."""

    def hold_in_flight_lock(self, unit: UnitOfWork, owner_id: int) -> None:
        transaction = pg(unit)
        _bound_pg(transaction)
        transaction.lock(AdvisoryLock.WORKBENCH_IN_FLIGHT, str(owner_id))

    def in_flight_ids(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[str]:
        rows = pg(unit).conn.execute(_IN_FLIGHT, (owner_id, aware(now, "a clock"))).fetchall()
        return [str(r[0]) for r in rows]

    def get_for_update(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> InvocationRow | None:
        transaction = pg(unit)
        _bound_pg(transaction)
        transaction.note_row_lock()
        row = transaction.conn.execute(_HOLD, (owner_id, campaign_id, invocation_id)).fetchone()
        return None if row is None else _row(row)

    def create(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        conversation_id: str, entry_id: str, tool_id: str, brief: str,
        source_entry_id: str | None, operation_id: str, attempt_deadline: datetime,
        now: datetime, schema_version: int,
    ) -> InvocationRow:
        _check_new(
            invocation_id=invocation_id, tool_id=tool_id, brief=brief, source_entry_id=source_entry_id,
            attempt_deadline=attempt_deadline, now=now, schema_version=schema_version,
        )
        _shaped(_OPERATION_ID, operation_id, "operation_id")
        transaction = pg(unit)
        row = transaction.conn.execute(_CREATE, (
            invocation_id, entry_id, tool_id, brief, source_entry_id,
            attempt_deadline, schema_version, now, now,
            campaign_id, owner_id, conversation_id, owner_id,
            entry_id, source_entry_id, source_entry_id,
        )).fetchone()
        if row is None:
            raise InvocationNotStored()
        self._begin_attempt(transaction, owner_id, campaign_id, invocation_id, 1, operation_id, now)
        return _row(row)

    def start_retry(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        attempt: int, operation_id: str, attempt_deadline: datetime, now: datetime,
    ) -> InvocationRow:
        _check_attempt(attempt)
        _check_window(now, attempt_deadline)
        _shaped(_OPERATION_ID, operation_id, "operation_id")
        transaction = pg(unit)
        row = transaction.conn.execute(_START_RETRY, (
            attempt_deadline, now, owner_id, campaign_id, invocation_id, attempt,
        )).fetchone()
        if row is None:
            raise InvocationNotStored()
        self._begin_attempt(
            transaction, owner_id, campaign_id, invocation_id, attempt + 1, operation_id, now,
        )
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
            raise InvocationNotStored()

    def fence(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, now: datetime,
    ) -> InvocationRow | None:
        transaction = pg(unit)
        _bound_pg(transaction)
        transaction.note_row_lock()
        row = transaction.conn.execute(
            _FENCE, (owner_id, campaign_id, invocation_id, attempt, aware(now, "a clock")),
        ).fetchone()
        return None if row is None else _row(row)

    def settle(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, status: str, outcome: str, result: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None, now: datetime,
    ) -> InvocationRow:
        _check_settlement(status, outcome, result, error)
        conn = pg(unit).conn
        row = conn.execute(_SETTLE, (
            status, _json(result), _json(error), aware(now, "a clock"),
            owner_id, campaign_id, invocation_id, attempt,
        )).fetchone()
        if row is None:
            raise InvocationNotStored()
        ended = conn.execute(_END_ATTEMPT, (
            now, outcome, owner_id, campaign_id, invocation_id, attempt,
        )).fetchone()
        if ended is None:
            raise InvocationNotStored()
        return _row(row)

    def set_cancel_requested(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        now: datetime,
    ) -> InvocationRow | None:
        row = pg(unit).conn.execute(
            _CANCEL, (aware(now, "a clock"), owner_id, campaign_id, invocation_id),
        ).fetchone()
        return None if row is None else _row(row)

    def attempts(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> list[AttemptRow]:
        rows = pg(unit).conn.execute(_ATTEMPTS, (owner_id, campaign_id, invocation_id)).fetchall()
        return [_attempt(r) for r in rows]

    def attempts_since(self, unit: UnitOfWork, since: datetime) -> int:
        row = pg(unit).conn.execute(_ATTEMPTS_SINCE, (aware(since, "a moment"),)).fetchone()
        return 0 if row is None else int(row[0])


# ── The in-memory twin ───────────────────────────────────────────────────────


class _EntryView(Protocol):
    """What the twin reads of the timeline twin's rows: the two columns the
    guards name. Structural, so this module imports nothing private."""

    @property
    def conversation_id(self) -> str: ...  # pragma: no cover - structural type

    @property
    def entry_kind(self) -> str: ...  # pragma: no cover - structural type


@dataclass(frozen=True)
class _TwinInvocation:
    """One invocation as the twin holds it. The payloads are kept as JSON text
    and parsed on every read, as JSONB is: no reader can reach the stored value."""

    row: InvocationRow
    result_json: str | None = field(repr=False)
    error_json: str | None

    def read(self) -> InvocationRow:
        return replace(
            self.row,
            result=None if self.result_json is None else json.loads(self.result_json),
            error=None if self.error_json is None else json.loads(self.error_json),
        )


def _key(owner_id: int, campaign_id: str, invocation_id: str) -> str:
    # Neither id can hold a `/`: both are `[A-Za-z0-9_-]` by their CHECKs.
    return f"{owner_id}/{campaign_id}/{invocation_id}"


def _bound_twin(twin: InMemoryTransaction) -> None:
    if not twin.transaction_bounds:
        twin.transaction_bound(None)


class InMemoryToolInvocationStore:
    """The twin.

    Its rows live in `shared_rows(db, "tool_invocations")` and
    `"tool_attempts"`, with the commit-time visibility and the rollback
    PostgreSQL gives them, and its guards read the SAME `"campaigns"`,
    `"conversations"` and `"timeline_entries"` tables the campaign,
    conversation and timeline twins write — so a parent that is missing, not
    the owner's, archived or of another conversation is refused here for the
    reason the statement refuses it there. Row locks are not modelled; the
    twin's transactions are serial (`InMemoryDatabase`), and a race is a
    two-connection PostgreSQL test.
    """

    def __init__(self, db: InMemoryDatabase) -> None:
        self._rows: Staging[_TwinInvocation] = shared_rows(db, "tool_invocations")
        self._attempts: Staging[AttemptRow] = shared_rows(db, "tool_attempts")
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._conversations: Staging[Conversation] = shared_rows(db, "conversations")
        self._entries: Staging[_EntryView] = shared_rows(db, "timeline_entries")

    def _get(
        self, twin: InMemoryTransaction, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> InvocationRow | None:
        found = self._rows.visible(twin).get(_key(owner_id, campaign_id, invocation_id))
        return None if found is None else found.read()

    def _put(self, twin: InMemoryTransaction, row: InvocationRow) -> InvocationRow:
        self._rows.replace(twin, _key(row.owner_id, row.campaign_id, row.invocation_id), _TwinInvocation(
            replace(row, result=None, error=None), _json(row.result), _json(row.error),
        ))
        return self._get(twin, row.owner_id, row.campaign_id, row.invocation_id) or row

    def _begin_attempt(
        self, twin: InMemoryTransaction, row: InvocationRow, operation_id: str, now: datetime,
    ) -> None:
        attempt_key = f"{_key(row.owner_id, row.campaign_id, row.invocation_id)}/{row.attempt}"
        taken = self._attempts.visible(twin)
        if attempt_key in taken or any(a.operation_id == operation_id for a in taken.values()):
            raise InvocationNotStored()
        self._attempts.add(twin, attempt_key, AttemptRow(
            row.owner_id, row.campaign_id, row.invocation_id, row.attempt, operation_id,
            now, row.attempt_deadline, None, None,
        ))

    def hold_in_flight_lock(self, unit: UnitOfWork, owner_id: int) -> None:
        twin = fake(unit)
        _bound_twin(twin)
        twin.lock(AdvisoryLock.WORKBENCH_IN_FLIGHT, str(owner_id))

    def in_flight_ids(self, unit: UnitOfWork, owner_id: int, *, now: datetime) -> list[str]:
        moment = aware(now, "a clock")
        rows = [
            found.row for found in self._rows.visible(fake(unit)).values()
            if found.row.owner_id == owner_id and found.row.status == "working"
            and found.row.attempt_deadline > moment
        ]
        rows.sort(key=lambda r: (r.created_at, r.invocation_id))
        return [r.invocation_id for r in rows]

    def get_for_update(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> InvocationRow | None:
        twin = fake(unit)
        _bound_twin(twin)
        twin.note_row_lock()
        return self._get(twin, owner_id, campaign_id, invocation_id)

    def create(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        conversation_id: str, entry_id: str, tool_id: str, brief: str,
        source_entry_id: str | None, operation_id: str, attempt_deadline: datetime,
        now: datetime, schema_version: int,
    ) -> InvocationRow:
        twin = fake(unit)
        _check_new(
            invocation_id=invocation_id, tool_id=tool_id, brief=brief, source_entry_id=source_entry_id,
            attempt_deadline=attempt_deadline, now=now, schema_version=schema_version,
        )
        _shaped(_OPERATION_ID, operation_id, "operation_id")
        campaign = self._campaigns.visible(twin).get(campaign_id)
        conversation = self._conversations.visible(twin).get(conversation_id)
        entries = self._entries.visible(twin)
        carrying = entries.get(entry_id)
        source = None if source_entry_id is None else entries.get(source_entry_id)
        taken = self._rows.visible(twin)
        if (
            campaign is None or campaign.owner_id != owner_id or campaign.is_archived
            or conversation is None or conversation.owner_id != owner_id
            or conversation.campaign_id != campaign_id
            or carrying is None or carrying.conversation_id != conversation_id
            or carrying.entry_kind != "tool"
            or (source_entry_id is not None and (source is None or source.conversation_id != conversation_id))
            or _key(owner_id, campaign_id, invocation_id) in taken
            or any(found.row.entry_id == entry_id for found in taken.values())
        ):
            raise InvocationNotStored()
        row = InvocationRow(
            owner_id=owner_id, campaign_id=campaign_id, invocation_id=invocation_id,
            conversation_id=conversation_id, entry_id=entry_id, tool_id=tool_id, brief=brief,
            source_entry_id=source_entry_id, status="working", attempt=1,
            attempt_deadline=attempt_deadline, cancel_requested=False, result=None, error=None,
            schema_version=schema_version, created_at=now, updated_at=now,
        )
        stored = self._put(twin, row)
        self._begin_attempt(twin, stored, operation_id, now)
        return stored

    def start_retry(
        self, unit: UnitOfWork, *, owner_id: int, campaign_id: str, invocation_id: str,
        attempt: int, operation_id: str, attempt_deadline: datetime, now: datetime,
    ) -> InvocationRow:
        twin = fake(unit)
        _check_attempt(attempt)
        _check_window(now, attempt_deadline)
        _shaped(_OPERATION_ID, operation_id, "operation_id")
        found = self._get(twin, owner_id, campaign_id, invocation_id)
        campaign = self._campaigns.visible(twin).get(campaign_id)
        if (
            found is None or found.status != "failed" or found.attempt != attempt
            or found.attempt >= ATTEMPT_MAX
            or not isinstance(found.error, Mapping) or found.error.get("retryable") is not True
            or campaign is None or campaign.owner_id != owner_id or campaign.is_archived
        ):
            raise InvocationNotStored()
        stored = self._put(twin, replace(
            found, status="working", attempt=found.attempt + 1, attempt_deadline=attempt_deadline,
            cancel_requested=False, error=None, updated_at=now,
        ))
        self._begin_attempt(twin, stored, operation_id, now)
        return stored

    def fence(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, now: datetime,
    ) -> InvocationRow | None:
        twin = fake(unit)
        _bound_twin(twin)
        twin.note_row_lock()
        moment = aware(now, "a clock")
        found = self._get(twin, owner_id, campaign_id, invocation_id)
        if found is None or found.status != "working" or found.attempt != attempt:
            return None
        return found if found.attempt_deadline > moment else None

    def settle(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        attempt: int, status: str, outcome: str, result: Mapping[str, Any] | None,
        error: Mapping[str, Any] | None, now: datetime,
    ) -> InvocationRow:
        twin = fake(unit)
        _check_settlement(status, outcome, result, error)
        moment = aware(now, "a clock")
        found = self._get(twin, owner_id, campaign_id, invocation_id)
        attempt_key = f"{_key(owner_id, campaign_id, invocation_id)}/{attempt}"
        running = self._attempts.visible(twin).get(attempt_key)
        if (
            found is None or found.status != "working" or found.attempt != attempt
            or running is None or running.ended_at is not None
        ):
            raise InvocationNotStored()
        stored = self._put(twin, replace(
            found, status=status, result=None if result is None else dict(result),
            error=None if error is None else dict(error), updated_at=moment,
        ))
        self._attempts.replace(twin, attempt_key, replace(running, ended_at=moment, outcome=outcome))
        return stored

    def set_cancel_requested(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str, *,
        now: datetime,
    ) -> InvocationRow | None:
        twin = fake(unit)
        moment = aware(now, "a clock")
        found = self._get(twin, owner_id, campaign_id, invocation_id)
        if found is None or found.status not in {"working", "done"} or found.cancel_requested:
            return None
        return self._put(twin, replace(found, cancel_requested=True, updated_at=moment))

    def attempts(
        self, unit: UnitOfWork, owner_id: int, campaign_id: str, invocation_id: str,
    ) -> list[AttemptRow]:
        rows = [
            a for a in self._attempts.visible(fake(unit)).values()
            if (a.owner_id, a.campaign_id, a.invocation_id) == (owner_id, campaign_id, invocation_id)
        ]
        return sorted(rows, key=lambda a: a.attempt)

    def attempts_since(self, unit: UnitOfWork, since: datetime) -> int:
        moment = aware(since, "a moment")
        return sum(1 for a in self._attempts.visible(fake(unit)).values() if a.started_at >= moment)

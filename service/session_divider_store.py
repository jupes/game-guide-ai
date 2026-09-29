"""Where a session divider is stored (agent-forge-harness-1kg.3.5).

A live table session's Start, and its end (End or expiry), each leave one
`session_divider` entry in every live thread of the session's campaign.
`service/session_dividers.py` decides which boundary, when and where, and every
statement it needs is here. The shape is the repository's store pattern: a
`Protocol`, a PostgreSQL implementation and an in-memory twin, proved alike by
`tests/test_session_dividers_db.py`.

**Why not `service/timeline_store.py`.** That module never names `1kg.2.4`'s
columns, and `tests/test_timeline_db.py` reads its statements to prove it. A
divider's guard needs two of those columns: the conversation's campaign and
whether it is archived. So the divider's two statements live here, read-only on
`chat.conversations`. They write `chat.timeline_entries` through the same
validation (`validated_entry`), and the twin writes the same rows
(`TwinEntryRow`), so the timeline's read model reads a divider back like any
stored entry.

**Ownership is in the statement** (SEC-2, `docs/migrations.md` section 4). The
target read names the owner and the campaign. The insert re-checks the owner,
the campaign and not-archived **in its own statement**, so a thread that is
missing, someone else's, in another campaign or archived is zero rows written,
never a caught integrity error. The transaction stays usable (G-9).

**Idempotency is the database's** (I-4). 0017's partial unique index allows at
most one divider per conversation, session and boundary. The insert names that
index's exact expressions and predicate as its `ON CONFLICT` target, so a
repeated or overlapping job run is zero rows, while any other unique violation,
such as an `entry_id` collision, still raises instead of being absorbed.

**Bounded.** The job runs under the instance's one job lock. The insert's
foreign-key check takes `FOR KEY SHARE` on each target conversation, which waits
on a `FOR UPDATE` or a delete of that row. So `divider_targets` first sets
`lock_timeout` and `transaction_timeout` for its transaction: a held row fails
the job within the bound, and the runner retries it. This module takes no
explicit lock.

**Private text.** A divider carries two identifiers and no text. Nothing here
logs.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Final, Protocol

from .campaign_store import Staging, shared_rows
from .conversation_store import Conversation
from .db import InMemoryDatabase, UnitOfWork
from .timeline_store import (
    EntryInvalid,
    TimelineStoreError,
    TwinEntryRow,
    fake,
    pg,
    validated_entry,
)
from .workbench_contracts import AnyEntry, SessionDividerEntry


class DividerIdTaken(TimelineStoreError):
    """The entry id is already stored. PostgreSQL raises the primary key's
    `UniqueViolation` here, because the insert's conflict target is the divider
    index alone. The twin raises this. Neither answers `False`: an id collision
    is a defect to see, not a repeat to absorb."""


def _divider(entry: AnyEntry) -> tuple[SessionDividerEntry, str]:
    """The validated divider and its stored JSON, or a refusal before any
    statement. A divider row's `entry_kind` column is written as a literal, so
    any other kind would be a row whose column disagrees with its payload, and
    whose index keys are NULL, outside the idempotency rule."""
    validated, payload = validated_entry(entry, entry.created_at)
    if not isinstance(validated, SessionDividerEntry):
        raise EntryInvalid(["entry_kind"])
    return validated, payload


class SessionDividerStore(Protocol):
    """What the divider job needs of a backend: the whole surface."""

    def divider_targets(
        self, unit: UnitOfWork, *, campaign_id: str, owner_id: int, at: datetime, limit: int,
    ) -> list[str]:
        """The owner's conversations linked to that campaign, not archived and
        created at or before `at`: at most `limit`, newest first by
        `(created_at, conversation_id)`. Read-only. It first bounds its
        transaction's lock waits and life (see the module docstring)."""
        ...  # pragma: no cover - structural type

    def append_divider(
        self, unit: UnitOfWork, conversation_id: str, entry: AnyEntry, *, owner_id: int, campaign_id: str,
    ) -> bool:
        """Store one divider in one thread. True when a row was inserted. False
        when the conversation is not a live thread of that owner in that
        campaign, or already holds this session's divider for this boundary.

        `EntryInvalid` or `EntryMismatch` before any statement, for an entry the
        contract refuses, an id this module did not mint, or any kind but
        `session_divider`. An `entry_id` already stored raises."""
        ...  # pragma: no cover - structural type


#: 0017's index, exactly: its expressions and its predicate. The insert's
#: conflict target, and `service/tests/test_session_dividers.py` reads the
#: migration to prove the two name the same thing.
DIVIDER_CONFLICT_TARGET: Final = (
    "(conversation_id, (payload->>'session_id'), (payload->>'boundary')) "
    "WHERE entry_kind = 'session_divider'"
)

_BOUND = "SELECT set_config('lock_timeout', %s, true), set_config('transaction_timeout', %s, true)"
#: `COLLATE "C"`: the twin's code-point order, so the two worlds agree on which
#: threads a bound keeps.
_TARGETS = (
    "SELECT conversation_id FROM chat.conversations "
    "WHERE campaign_id = %s AND user_id = %s AND archived_at IS NULL AND created_at <= %s "
    'ORDER BY created_at DESC, conversation_id COLLATE "C" DESC LIMIT %s'
)
_INSERT_DIVIDER = (
    "INSERT INTO chat.timeline_entries "
    "(entry_id, conversation_id, entry_kind, schema_version, created_at, payload) "
    "SELECT %s::text, c.conversation_id, 'session_divider', %s::integer, %s::timestamptz, %s::jsonb "
    "FROM chat.conversations c "
    "WHERE c.conversation_id = %s AND c.user_id = %s AND c.campaign_id = %s AND c.archived_at IS NULL "
    f"ON CONFLICT {DIVIDER_CONFLICT_TARGET} DO NOTHING RETURNING entry_id"
)


class PostgresSessionDividerStore:
    """`chat.timeline_entries` for dividers; `chat.conversations` read-only."""

    def divider_targets(
        self, unit: UnitOfWork, *, campaign_id: str, owner_id: int, at: datetime, limit: int,
    ) -> list[str]:
        tx = pg(unit)
        tx.conn.execute(_BOUND, (tx.campaign_lock.lock_timeout, tx.transaction_bound(None)))
        rows = tx.conn.execute(_TARGETS, (campaign_id, owner_id, at, limit)).fetchall()
        return [str(row[0]) for row in rows]

    def append_divider(
        self, unit: UnitOfWork, conversation_id: str, entry: AnyEntry, *, owner_id: int, campaign_id: str,
    ) -> bool:
        conn = pg(unit).conn
        validated, payload = _divider(entry)
        row = conn.execute(_INSERT_DIVIDER, (
            validated.entry_id, validated.schema_version, validated.created_at, payload,
            conversation_id, owner_id, campaign_id,
        )).fetchone()
        return row is not None


class InMemorySessionDividerStore:
    """The twin. It reads the SAME `conversations` table the conversation twin
    writes and writes the SAME `timeline_entries` table the timeline twin
    reads, so a thread the conversation store archived or never linked is
    refused here as the statement refuses it there, and a divider stored here is
    one `InMemoryTimelineStore.entry_window` reads back.

    It supports ONE open writer, like every twin on `InMemoryDatabase`. A race
    is a two-connection PostgreSQL test, never nested units here."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._conversations: Staging[Conversation] = shared_rows(db, "conversations")
        self._entries: Staging[TwinEntryRow] = shared_rows(db, "timeline_entries")

    def divider_targets(
        self, unit: UnitOfWork, *, campaign_id: str, owner_id: int, at: datetime, limit: int,
    ) -> list[str]:
        tx = fake(unit)
        tx.transaction_bound(None)
        found = [
            row
            for row in self._conversations.visible(tx).values()
            if row.campaign_id == campaign_id
            and row.owner_id == owner_id
            and not row.is_archived
            and row.created_at <= at
        ]
        found.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return [row.id for row in found[:limit]]

    def append_divider(
        self, unit: UnitOfWork, conversation_id: str, entry: AnyEntry, *, owner_id: int, campaign_id: str,
    ) -> bool:
        tx = fake(unit)
        validated, payload = _divider(entry)
        thread = self._conversations.visible(tx).get(conversation_id)
        if (
            thread is None
            or thread.owner_id != owner_id
            or thread.campaign_id != campaign_id
            or thread.is_archived
        ):
            return False
        stored = self._entries.visible(tx)
        identity = (validated.session_id, validated.boundary.value)
        if any(
            row.conversation_id == conversation_id
            and row.entry_kind == "session_divider"
            and _identity(row.payload_json) == identity
            for row in stored.values()
        ):
            return False
        if validated.entry_id in stored:
            raise DividerIdTaken("an entry with that id is already stored")
        self._entries.add(
            tx, validated.entry_id, TwinEntryRow.new(conversation_id, validated, payload, validated.created_at)
        )
        return True


def _identity(payload_json: str) -> tuple[object, object]:
    """What 0017's index keys a divider row on, read as `->>` reads it."""
    payload = json.loads(payload_json)
    return payload.get("session_id"), payload.get("boundary")

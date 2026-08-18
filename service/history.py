"""
Server-side message history.

`MessageStore` is the seam the app talks to: append a turn, read back the most
recent N of a conversation (served oldest-first for display). Two impls:

- `PostgresMessageStore` — the real one, `chat.*` in the same Postgres instance
  as the RAG corpus. `ensure_schema()` applies the canonical DDL
  (`service/sql/04-chat-schema.sql`) at startup, which is the migration path for
  databases that predate a schema change.
- `InMemoryMessageStore` — the test/dev fake with identical ordering + limit
  semantics.

Persistence is deliberately best-effort at the call site: the /chat handler
wraps `append` so a history failure can never fail an answer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from .models import ChatMode, MessageRole, StoredMessage, Suggestion
from .schema import CHAT_SCHEMA, load


@dataclass
class StoredAttachment:
    """One stored attachment incl. its extracted text (server-side; the RAG
    injection reads `.extracted_text`, the GET endpoint returns metadata only)."""
    id: int
    conversation_id: str
    filename: str
    content_type: str
    extracted_text: str
    created_at: datetime


class MessageStore(Protocol):
    """What the app needs from a history backend."""

    def append(
        self, conversation_id: str, mode: str, role: str, content: str,
        suggestions: list[dict[str, Any]] | None = None,
    ) -> None: ...  # pragma: no cover - structural type

    def recent(self, conversation_id: str, limit: int) -> list[StoredMessage]:
        ...  # pragma: no cover - structural type

    def append_attachment(
        self, conversation_id: str, filename: str, content_type: str, extracted_text: str,
    ) -> StoredAttachment: ...  # pragma: no cover - structural type

    def attachments_for(self, conversation_id: str) -> list[StoredAttachment]:
        ...  # pragma: no cover - structural type

    def claim_conversation(self, conversation_id: str, user_id: int) -> int:
        ...  # pragma: no cover - structural type

    def owner_of(self, conversation_id: str) -> int | None:
        ...  # pragma: no cover - structural type

    def has_content(self, conversation_id: str) -> bool:
        ...  # pragma: no cover - structural type

    def calls_today(self) -> int:
        ...  # pragma: no cover - structural type


@dataclass
class _Row:
    id: int
    conversation_id: str
    mode: str
    role: str
    content: str
    suggestions: list[dict[str, Any]] | None
    created_at: datetime


@dataclass
class InMemoryMessageStore:
    """Fake with the real store's ordering + limit semantics (tests/dev)."""

    _rows: list[_Row] = field(default_factory=list)
    _attachments: list[StoredAttachment] = field(default_factory=list)
    _owners: dict[str, int] = field(default_factory=dict)

    def append(
        self, conversation_id: str, mode: str, role: str, content: str,
        suggestions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._rows.append(_Row(
            id=len(self._rows) + 1, conversation_id=conversation_id,
            mode=mode, role=role, content=content, suggestions=suggestions,
            created_at=datetime.now(UTC),
        ))

    def recent(self, conversation_id: str, limit: int) -> list[StoredMessage]:
        rows = [r for r in self._rows if r.conversation_id == conversation_id]
        return [_to_message(r) for r in rows[-limit:]]

    def append_attachment(
        self, conversation_id: str, filename: str, content_type: str, extracted_text: str,
    ) -> StoredAttachment:
        att = StoredAttachment(
            id=len(self._attachments) + 1, conversation_id=conversation_id,
            filename=filename, content_type=content_type, extracted_text=extracted_text,
            created_at=datetime.now(UTC),
        )
        self._attachments.append(att)
        return att

    def attachments_for(self, conversation_id: str) -> list[StoredAttachment]:
        return [a for a in self._attachments if a.conversation_id == conversation_id]

    def claim_conversation(self, conversation_id: str, user_id: int) -> int:
        return self._owners.setdefault(conversation_id, user_id)

    def owner_of(self, conversation_id: str) -> int | None:
        return self._owners.get(conversation_id)

    def has_content(self, conversation_id: str) -> bool:
        return any(r.conversation_id == conversation_id for r in self._rows) or any(
            a.conversation_id == conversation_id for a in self._attachments
        )

    def calls_today(self) -> int:
        # Same UTC boundary as the SQL, or the fake and the real store disagree
        # about which rows are "today" and the integration test passes on the
        # strength of the runner's timezone.
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(
            1 for r in self._rows if r.role == "user" and r.created_at >= midnight
        )


def _to_message(r: _Row) -> StoredMessage:
    return StoredMessage(
        id=r.id, role=MessageRole(r.role), content=r.content,
        mode=ChatMode(r.mode), created_at=r.created_at,
        # The cast is the honest description of a boundary a type checker can't
        # see through: these are raw JSONB dicts and pydantic validates/coerces
        # them into Suggestion on construction, raising if they don't fit.
        suggestions=cast("list[Suggestion] | None", r.suggestions),
    )


class PostgresMessageStore:
    """`chat.messages` in the corpus Postgres. One connection per operation —
    no pooling; chat traffic is single-user scale and psycopg connects fast."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or os.environ.get(
            "DATABASE_URL", "postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai"
        )

    def _connect(self):
        import psycopg

        return psycopg.connect(self._dsn)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(load(CHAT_SCHEMA))

    def calls_today(self) -> int:
        """User turns recorded since UTC midnight — the daily cost ceiling (x5bz.3.3).

        Counted from rows that already exist rather than a counter of its own: no
        new schema, exact across instances, and it cannot drift out of step with
        what was actually asked.

        The timezone is pinned rather than inherited. A bare
        `date_trunc('day', now())` truncates in the SERVER's TimeZone setting,
        which this schema never sets — so the day would roll over at whatever hour
        the instance happens to be configured for, and the in-memory fake would
        disagree with the real store depending on where the tests ran.

        Only `role = 'user'` counts: each turn writes a user row and an assistant
        row, and counting both would silently halve the configured cap.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM chat.messages WHERE role = 'user' "
                "AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') "
                "AT TIME ZONE 'UTC'"
            ).fetchone()
        return int(row[0]) if row else 0

    def append(
        self, conversation_id: str, mode: str, role: str, content: str,
        suggestions: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat.messages (conversation_id, mode, role, content, suggestions) "
                "VALUES (%s, %s, %s, %s, %s)",
                (conversation_id, mode, role, content,
                 json.dumps(suggestions) if suggestions is not None else None),
            )

    def recent(self, conversation_id: str, limit: int) -> list[StoredMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, mode, role, content, suggestions, created_at FROM chat.messages "
                "WHERE conversation_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
                (conversation_id, limit),
            ).fetchall()
        # Query grabs the most recent N (DESC); display order is oldest-first.
        # psycopg deserializes jsonb to Python lists/dicts natively.
        return [
            StoredMessage(
                id=row[0], mode=ChatMode(row[1]), role=MessageRole(row[2]),
                content=row[3], suggestions=row[4], created_at=row[5],
            )
            for row in reversed(rows)
        ]

    def append_attachment(
        self, conversation_id: str, filename: str, content_type: str, extracted_text: str,
    ) -> StoredAttachment:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO chat.attachments (conversation_id, filename, content_type, extracted_text) "
                "VALUES (%s, %s, %s, %s) RETURNING id, created_at",
                (conversation_id, filename, content_type, extracted_text),
            ).fetchone()
        return StoredAttachment(
            id=row[0], conversation_id=conversation_id, filename=filename,
            content_type=content_type, extracted_text=extracted_text, created_at=row[1],
        )

    def attachments_for(self, conversation_id: str) -> list[StoredAttachment]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, filename, content_type, extracted_text, created_at "
                "FROM chat.attachments WHERE conversation_id = %s ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
        return [
            StoredAttachment(
                id=row[0], conversation_id=conversation_id, filename=row[1],
                content_type=row[2], extracted_text=row[3], created_at=row[4],
            )
            for row in rows
        ]

    def claim_conversation(self, conversation_id: str, user_id: int) -> int:
        """Record the owner on first use; return the owner (existing or new).
        INSERT ... ON CONFLICT DO NOTHING then SELECT returns whoever won."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s) "
                "ON CONFLICT (conversation_id) DO NOTHING",
                (conversation_id, user_id),
            )
            row = conn.execute(
                "SELECT user_id FROM chat.conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        return row[0]

    def owner_of(self, conversation_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM chat.conversations WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
        return row[0] if row is not None else None

    def has_content(self, conversation_id: str) -> bool:
        """Does this conversation hold any message or attachment?

        Lets a READ decide whether a conversation is worth claiming. An empty id
        has nothing to protect and nothing to leak, so it must NOT get an
        ownership row — otherwise any authenticated caller could fill
        chat.conversations with arbitrary ids just by issuing GETs."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT EXISTS ("
                "  SELECT 1 FROM chat.messages WHERE conversation_id = %s"
                "  UNION ALL"
                "  SELECT 1 FROM chat.attachments WHERE conversation_id = %s"
                ")",
                (conversation_id, conversation_id),
            ).fetchone()
        return bool(row[0])

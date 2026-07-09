"""
Server-side message history (channel-chats CP-A).

`MessageStore` is the seam the app talks to: append a turn, read back the most
recent N of a conversation (served oldest-first for display). Two impls:

- `PostgresMessageStore` — the real one, `chat.messages` in the same Postgres
  instance as the RAG corpus. `ensure_schema()` runs idempotent DDL at startup,
  which doubles as the migration path for volumes that predate the chat schema
  (compose init SQL only runs on first container init;
  vector-db/init/04-chat-schema.sql carries the same DDL for fresh volumes).
- `InMemoryMessageStore` — the test/dev fake with identical ordering + limit
  semantics.

Persistence is deliberately best-effort at the call site: the /chat handler
wraps `append` so a history failure can never fail an answer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import ChatMode, MessageRole, StoredMessage

# Idempotent DDL — safe to run on every startup. Kept in sync with
# vector-db/init/04-chat-schema.sql (fresh-volume path).
CHAT_SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS chat;

CREATE TABLE IF NOT EXISTS chat.messages (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  mode            TEXT NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content         TEXT NOT NULL,
  suggestions     JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_messages_conv_created_idx
  ON chat.messages (conversation_id, created_at);
"""


class MessageStore(Protocol):
    """What the app needs from a history backend."""

    def append(
        self, conversation_id: str, mode: str, role: str, content: str,
        suggestions: list[dict[str, Any]] | None = None,
    ) -> None: ...  # pragma: no cover - structural type

    def recent(self, conversation_id: str, limit: int) -> list[StoredMessage]:
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

    def append(
        self, conversation_id: str, mode: str, role: str, content: str,
        suggestions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._rows.append(_Row(
            id=len(self._rows) + 1, conversation_id=conversation_id,
            mode=mode, role=role, content=content, suggestions=suggestions,
            created_at=datetime.now(timezone.utc),
        ))

    def recent(self, conversation_id: str, limit: int) -> list[StoredMessage]:
        rows = [r for r in self._rows if r.conversation_id == conversation_id]
        return [_to_message(r) for r in rows[-limit:]]


def _to_message(r: _Row) -> StoredMessage:
    return StoredMessage(
        id=r.id, role=MessageRole(r.role), content=r.content,
        mode=ChatMode(r.mode), created_at=r.created_at,
        suggestions=r.suggestions,  # pydantic validates the raw dicts
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
            conn.execute(CHAT_SCHEMA_DDL)

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

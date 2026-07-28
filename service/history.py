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

CREATE TABLE IF NOT EXISTS chat.attachments (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  filename        TEXT NOT NULL,
  content_type    TEXT NOT NULL DEFAULT '',
  extracted_text  TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_attachments_conv_created_idx
  ON chat.attachments (conversation_id, created_at);

-- Per-user conversation ownership (x5bz.2 D). A conversation_id is client-
-- generated; the first authenticated user to use it owns it, and the endpoints
-- 403 anyone else. A separate table (not a user_id column on messages) keeps the
-- ownership check one lookup and leaves the message/attachment writes untouched.
CREATE TABLE IF NOT EXISTS chat.conversations (
  conversation_id TEXT PRIMARY KEY,
  user_id         BIGINT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Content follows its ownership row (added in security review). Policy alone
-- was not enough for account deletion: `require_session` validates at request
-- START, so a request already in flight when an account is deleted could append
-- to a conversation whose ownership row had just been removed — recreating
-- unowned content under a deleted user. That write is now refused by the
-- database, and ON DELETE CASCADE (chained from auth.users via
-- chat.conversations, see auth_store.AUTH_SCHEMA_DDL) means deleting an account
-- takes its conversations, messages and attachments with it.
--
-- NOT VALID is deliberate: conversations that predate the ownership table have
-- messages with no parent row, and back-filling owners for them is not something
-- DDL can decide. Existing rows are left alone; every new write is enforced.
--
-- Each is applied ONLY when missing or wrong. `ensure_schema()` runs on every
-- startup and Cloud Run scales to zero, so an unconditional DROP/ADD would take
-- an ACCESS EXCLUSIVE lock on the table at every cold start — blocking live
-- queries until the startup transaction commits, and serializing concurrent
-- starts behind each other.
--
-- The predicate pins the whole shape, not just the name: `confdeltype = 'c'` is
-- ON DELETE CASCADE, and conkey/confkey pin the exact COLUMNS. Without the
-- column check a same-named CASCADE foreign key from some other text column
-- (chat.messages.content would type-check) to chat.conversations would satisfy
-- the test, leaving conversation_id unprotected while startup reported success.
DO $$
DECLARE
  conv regclass := to_regclass('chat.conversations');
  msgs regclass := to_regclass('chat.messages');
  atts regclass := to_regclass('chat.attachments');
  conv_key smallint[];
BEGIN
  IF conv IS NULL THEN RETURN; END IF;
  conv_key := ARRAY[(SELECT attnum FROM pg_attribute
                      WHERE attrelid = conv AND attname = 'conversation_id')];

  IF msgs IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'messages_conversation_fkey'
       AND conrelid = msgs AND contype = 'f'
       AND confrelid = conv AND confdeltype = 'c'
       AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                            WHERE attrelid = msgs AND attname = 'conversation_id')]
       AND confkey = conv_key
  ) THEN
    ALTER TABLE chat.messages DROP CONSTRAINT IF EXISTS messages_conversation_fkey;
    ALTER TABLE chat.messages ADD CONSTRAINT messages_conversation_fkey
      FOREIGN KEY (conversation_id) REFERENCES chat.conversations (conversation_id)
      ON DELETE CASCADE NOT VALID;
  END IF;

  IF atts IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'attachments_conversation_fkey'
       AND conrelid = atts AND contype = 'f'
       AND confrelid = conv AND confdeltype = 'c'
       AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                            WHERE attrelid = atts AND attname = 'conversation_id')]
       AND confkey = conv_key
  ) THEN
    ALTER TABLE chat.attachments DROP CONSTRAINT IF EXISTS attachments_conversation_fkey;
    ALTER TABLE chat.attachments ADD CONSTRAINT attachments_conversation_fkey
      FOREIGN KEY (conversation_id) REFERENCES chat.conversations (conversation_id)
      ON DELETE CASCADE NOT VALID;
  END IF;
END $$;
"""


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
            created_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
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

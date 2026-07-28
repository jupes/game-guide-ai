-- Chat message history (channel-chats CP-A).
-- Fresh-volume path only: compose init SQL runs on first container init.
-- Existing volumes are migrated by the service at startup — see
-- service/history.py PostgresMessageStore.ensure_schema(), which runs this
-- same idempotent DDL (keep the two in sync).

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

-- File attachments (swe1.6): per-conversation uploaded files whose extracted
-- text is injected into that conversation's RAG context.
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

-- Per-user conversation ownership (x5bz.2). First authenticated user to use a
-- conversation_id owns it; the API 403s anyone else. Kept in sync with
-- service/history.py CHAT_SCHEMA_DDL.
CREATE TABLE IF NOT EXISTS chat.conversations (
  conversation_id TEXT PRIMARY KEY,
  user_id         BIGINT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Content follows its ownership row: a request already in flight when an account
-- is deleted must not be able to append into a conversation whose ownership row
-- is gone, and deleting an account must take its content with it (the cascade
-- chains from auth.users — see 05-auth-schema.sql). NOT VALID leaves
-- pre-ownership-table rows alone and enforces every new write.
ALTER TABLE chat.messages DROP CONSTRAINT IF EXISTS messages_conversation_fkey;
ALTER TABLE chat.messages ADD CONSTRAINT messages_conversation_fkey
  FOREIGN KEY (conversation_id) REFERENCES chat.conversations (conversation_id)
  ON DELETE CASCADE NOT VALID;

ALTER TABLE chat.attachments DROP CONSTRAINT IF EXISTS attachments_conversation_fkey;
ALTER TABLE chat.attachments ADD CONSTRAINT attachments_conversation_fkey
  FOREIGN KEY (conversation_id) REFERENCES chat.conversations (conversation_id)
  ON DELETE CASCADE NOT VALID;

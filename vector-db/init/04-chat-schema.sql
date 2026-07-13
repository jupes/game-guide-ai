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

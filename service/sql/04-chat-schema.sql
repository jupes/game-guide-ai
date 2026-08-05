-- Chat message history and per-user conversation ownership.
--
-- CANONICAL. This file is the only definition of the `chat` schema. It is
-- applied by both paths and must stay idempotent:
--   * fresh database — mounted into the container's init directory
--   * existing database — re-applied at every service startup, which is the
--     migration path for volumes that predate any of this
--     (service/history.py PostgresMessageStore.ensure_schema)

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

-- Per-user conversation ownership. A conversation_id is client-generated; the
-- first authenticated user to use it owns it, and the API 403s anyone else.
CREATE TABLE IF NOT EXISTS chat.conversations (
  conversation_id TEXT PRIMARY KEY,
  user_id         BIGINT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Content follows its ownership row. `require_session` validates at request
-- START, so a request already in flight when an account is deleted must not be
-- able to append into a conversation whose ownership row is gone; and deleting
-- an account must take its content with it (the cascade chains from auth.users
-- — see 05-auth-schema.sql). NOT VALID leaves pre-ownership-table rows alone
-- and enforces every new write.
--
-- Guarded so a re-run is a no-op: this runs at every startup, and an
-- unconditional DROP/ADD would take an ACCESS EXCLUSIVE lock on a live table at
-- every cold start. The predicate pins the whole shape — confdeltype 'c' =
-- CASCADE, and conkey/confkey pin the exact COLUMNS, so a same-named CASCADE FK
-- on another text column cannot pass for the real one and leave
-- conversation_id unprotected.
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

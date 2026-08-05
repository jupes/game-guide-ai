-- Auth schema: per-user accounts + one-time invite links.
--
-- CANONICAL. This file is the only definition of the `auth` schema. It is
-- applied by both paths and must stay idempotent:
--   * fresh database — mounted into the container's init directory
--   * existing database — re-applied at every service startup
--     (service/auth_store.py PostgresAuthStore.ensure_schema)
--
-- Runs AFTER 04-chat-schema.sql: the ownership foreign key below points at
-- chat.conversations, and is skipped when that table does not exist.

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
  id            BIGSERIAL PRIMARY KEY,
  email         TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'player' CHECK (role IN ('player', 'dm')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Case-folded uniqueness: the invite is the trust anchor, email is only the
-- login id, so "Ada@x.com" and "ada@x.com" must be one account.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uidx
  ON auth.users (lower(email));

CREATE TABLE IF NOT EXISTS auth.invites (
  token       TEXT PRIMARY KEY,               -- secrets.token_urlsafe(32)
  role        TEXT NOT NULL DEFAULT 'player' CHECK (role IN ('player', 'dm')),
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ,                     -- NULL until redeemed (atomic guard)
  -- ON DELETE SET NULL so a compromised account can actually be deleted: the
  -- invite row survives as the audit trail that its token was spent, it just
  -- forgets who spent it. Without this, deleting any user who redeemed an
  -- invite is rejected by the FK.
  used_by     BIGINT REFERENCES auth.users (id) ON DELETE SET NULL,
  revoked_at  TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS invites_created_idx ON auth.invites (created_at);

-- Two constraint migrations, each applied ONLY when missing or wrong. This runs
-- at every startup, where an unconditional DROP/ADD would take an ACCESS
-- EXCLUSIVE lock on a live table at each cold start and re-validate the invites
-- FK every time. `confdeltype` is the delete action: 'n' = SET NULL, 'c' =
-- CASCADE; conkey/confkey pin the exact COLUMNS, so a same-named FK on the
-- wrong column cannot pass for the real one.
--
-- 1. auth.invites.used_by ON DELETE SET NULL — repairs a database whose
--    constraint predates this (or has the wrong action); on a fresh one the
--    inline REFERENCES above has already produced it.
-- 2. chat.conversations.user_id ON DELETE CASCADE — ownership must point at a
--    real account, so a request in flight when an account is deleted cannot
--    re-create a row for a user id that no longer exists, and deleting an
--    account removes its content (the cascade chains on to chat.messages /
--    chat.attachments, see 04-chat-schema.sql). Guarded on the chat table
--    existing: a deployment that skips the chat schema must still start.
DO $$
DECLARE
  users    regclass := to_regclass('auth.users');
  invites  regclass := to_regclass('auth.invites');
  conv     regclass := to_regclass('chat.conversations');
  users_pk smallint[];
BEGIN
  users_pk := ARRAY[(SELECT attnum FROM pg_attribute
                      WHERE attrelid = users AND attname = 'id')];

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'invites_used_by_fkey'
       AND conrelid = invites AND contype = 'f'
       AND confrelid = users AND confdeltype = 'n'
       AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                            WHERE attrelid = invites AND attname = 'used_by')]
       AND confkey = users_pk
  ) THEN
    ALTER TABLE auth.invites DROP CONSTRAINT IF EXISTS invites_used_by_fkey;
    ALTER TABLE auth.invites ADD CONSTRAINT invites_used_by_fkey
      FOREIGN KEY (used_by) REFERENCES auth.users (id) ON DELETE SET NULL;
  END IF;

  IF conv IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'conversations_user_fkey'
       AND conrelid = conv AND contype = 'f'
       AND confrelid = users AND confdeltype = 'c'
       AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                            WHERE attrelid = conv AND attname = 'user_id')]
       AND confkey = users_pk
  ) THEN
    ALTER TABLE chat.conversations DROP CONSTRAINT IF EXISTS conversations_user_fkey;
    ALTER TABLE chat.conversations ADD CONSTRAINT conversations_user_fkey
      FOREIGN KEY (user_id) REFERENCES auth.users (id) ON DELETE CASCADE NOT VALID;
  END IF;
END $$;

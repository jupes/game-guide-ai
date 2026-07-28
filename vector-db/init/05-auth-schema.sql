-- Auth schema (x5bz.2): per-user accounts + one-time invite links.
-- Fresh-volume path only: compose init SQL runs on first container init.
-- Existing volumes are migrated by the service at startup — see
-- service/auth_store.py PostgresAuthStore.ensure_schema(), which runs this
-- same idempotent DDL (keep the two in sync).

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

-- Redundant on a fresh volume (the inline REFERENCES above already produces a
-- constraint of this name), but service/auth_store.py runs it as the migration
-- for volumes created before ON DELETE SET NULL existed. Both copies carry it so
-- they declare the same objects — tests/test_schema_parity.py enforces that.
ALTER TABLE auth.invites DROP CONSTRAINT IF EXISTS invites_used_by_fkey;
ALTER TABLE auth.invites ADD CONSTRAINT invites_used_by_fkey
  FOREIGN KEY (used_by) REFERENCES auth.users (id) ON DELETE SET NULL;

-- Conversation ownership points at a REAL account. `require_session` validates
-- at request START, so a request in flight when an account is deleted could
-- otherwise re-create an ownership row for a user id that no longer exists.
-- ON DELETE CASCADE chains onward to chat.messages / chat.attachments (see
-- 04-chat-schema.sql), so deleting an account removes its content. Guarded
-- because this file runs after 04 but the chat schema may be absent in a
-- deployment that doesn't use it. Kept in sync with service/auth_store.py.
DO $$
BEGIN
  IF to_regclass('chat.conversations') IS NOT NULL THEN
    ALTER TABLE chat.conversations DROP CONSTRAINT IF EXISTS conversations_user_fkey;
    ALTER TABLE chat.conversations ADD CONSTRAINT conversations_user_fkey
      FOREIGN KEY (user_id) REFERENCES auth.users (id) ON DELETE CASCADE NOT VALID;
  END IF;
END $$;

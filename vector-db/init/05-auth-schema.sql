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

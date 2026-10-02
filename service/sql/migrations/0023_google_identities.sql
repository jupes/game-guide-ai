-- Sign in with Google (agent-forge-harness-lvs7): who an account can sign in as,
-- besides its password. This file's number is written nowhere else in it: the
-- lead renumbers a migration at merge when a parallel bead takes the number first.
--
-- PURE EXPANSION. Every object is safe next to the PREVIOUS build, which never
-- names any of them:
--   * auth.users.has_password: a constant default, so on PostgreSQL 11 and later
--     (Cloud SQL runs 17) the column is added as metadata, with no table rewrite.
--     The previous build inserts rows without naming it and gets true, which is
--     correct, because it only ever creates password accounts.
--   * auth.identities: a new table; the previous build never reads or writes it.
--   * auth.identity_events: a new table, for the same reason.
-- Nothing is dropped, renamed or tightened.
--
-- has_password exists so that nothing has to guess whether an account's stored
-- hash is a password somebody chose: an account created through Google gets a
-- random hash nobody holds (auth.users.password_hash is NOT NULL), and this
-- column says so. A later set-password or unlink feature reads it.
--
-- auth.identity_events is its own table, not audit.events: that ledger is
-- campaign-scoped (campaign_id_tombstone is NOT NULL, and its action, object and
-- actor vocabularies are campaign-shaped), so relaxing it for sign-in rows would
-- contract a shared ledger other work owns. Like every audit-shaped table it holds
-- ids and closed codes only: no email, no provider subject, no address, no invite
-- token (docs/migrations.md, SEC-20).

ALTER TABLE auth.users
  ADD COLUMN has_password BOOLEAN NOT NULL DEFAULT true;

CREATE TABLE auth.identities (
  provider      TEXT NOT NULL CHECK (provider IN ('google')),
  subject       TEXT NOT NULL CHECK (subject <> '' AND length(subject) <= 255),
  user_id       BIGINT NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  email_at_link TEXT NOT NULL CHECK (length(email_at_link) BETWEEN 3 AND 254),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT identities_pkey PRIMARY KEY (provider, subject),
  -- One Google identity per account, and the index the ON DELETE CASCADE from
  -- auth.users uses (its leading column is user_id).
  CONSTRAINT identities_user_provider_key UNIQUE (user_id, provider)
);

CREATE TABLE auth.identity_events (
  id          BIGSERIAL PRIMARY KEY,
  -- No foreign key: a row outlives the account it describes (incident review).
  user_id     BIGINT,
  provider    TEXT NOT NULL CHECK (provider IN ('google')),
  action      TEXT NOT NULL CHECK (action IN ('sign_in', 'sign_up', 'link')),
  decision    TEXT NOT NULL CHECK (decision IN ('allowed', 'refused')),
  reason_code TEXT CHECK (reason_code IS NULL OR (reason_code <> '' AND length(reason_code) <= 40)),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT identity_events_reason_chk CHECK ((decision = 'allowed') = (reason_code IS NULL))
);

CREATE INDEX identity_events_user_created_idx ON auth.identity_events (user_id, created_at);

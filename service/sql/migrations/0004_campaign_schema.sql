-- Migration 0004 — the campaign schema (1kg.2.1).
--
-- Storage for the GM Workbench: a campaign, the authorisation row every check
-- reads, the people at the table, the codes and device credentials that bind a
-- person to a browser, and the live table session with its link generations.
-- No routes and no wire models read any of this yet — those are 1kg.2.2's and
-- 1kg.2.3's. What is fixed here is what the database itself will guarantee.
--
-- IDENTIFIERS (SEC-4). Every id is a TEXT primary key minted by the application:
-- a three-letter type prefix plus 128 CSPRNG bits rendered URL-safe, never a
-- sequence. The CHECK on each id column is generated from
-- `service/campaign_identity.id_check_regex()` — one rule spelled twice drifts,
-- so `service/tests/test_campaign_schema_sql.py` asserts this file still agrees
-- with the registry.
--
-- SECRETS (SEC-5). An enrolment code, a device credential and a table link token
-- are 32 random bytes each, and only the lowercase-hex SHA-256 digest is stored.
-- Every lookup is an exact match on a unique index over the digest — partial
-- where the column is nullable, which is `table_sessions.link_digest` alone (a
-- retired link has none, and PostgreSQL would let NULLs repeat anyway). No
-- column here holds a token, a code or a credential in any recoverable form.
--
-- PRIVATE TEXT (SEC-20). A participant's alias is private text: it is stored
-- here and nowhere else — never a log line, never an exception message, never an
-- audit row (0005 has no column for it).
--
-- This file is not idempotent. 0001 and 0002 are, because they adopt databases
-- that predate the ledger; from 0003 on, the ledger says what has been applied.

CREATE SCHEMA campaign;

CREATE TABLE campaign.campaigns (
  id          TEXT PRIMARY KEY CHECK (id ~ '^cmp_[A-Za-z0-9_-]{22,60}$'),
  owner_id    BIGINT NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  archived_at TIMESTAMPTZ,
  -- Redundant with the primary key, and there so that table_sessions can carry
  -- a composite foreign key to (id, owner_id): in v1 the GM of a session IS the
  -- campaign's owner (AUD-1), and an invariant the database holds cannot be
  -- forgotten by a route. What it costs: `owner_id` becomes a key column, so an
  -- UPDATE of it would take FOR UPDATE rather than FOR NO KEY UPDATE. Nothing
  -- updates it — a campaign does not change hands — and every other column
  -- (name, updated_at, archived_at) is untouched by this, so RQ-3 is intact.
  UNIQUE (id, owner_id)
);
CREATE INDEX campaigns_owner_idx ON campaign.campaigns (owner_id, created_at);

-- RQ-1. Authorisation is read under a lock on THIS row, not on the campaign
-- itself, so that a check never contends with an ordinary campaign update.
-- `lock_token` is never written by this service: it exists so that 1ir.1.13 can
-- grant a display role UPDATE (lock_token) — the privilege a row lock needs —
-- without granting it anything else on the row.
CREATE TABLE campaign.authz_state (
  campaign_id    TEXT PRIMARY KEY REFERENCES campaign.campaigns (id) ON DELETE CASCADE,
  authz_revision BIGINT NOT NULL DEFAULT 0 CHECK (authz_revision >= 0),
  lock_token     SMALLINT NOT NULL DEFAULT 0
);

-- RQ-1 again, and this is the half that matters: no route, script, fixture or
-- later epic can make a campaign without its authorisation row, because the
-- database makes it. A store that forgot would fail closed at the first
-- `lock_campaign`; with the trigger it cannot forget.
CREATE FUNCTION campaign.create_authz_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO campaign.authz_state (campaign_id) VALUES (NEW.id);
  RETURN NULL;
END $$;
CREATE TRIGGER campaigns_authz_state_ai AFTER INSERT ON campaign.campaigns
  FOR EACH ROW EXECUTE FUNCTION campaign.create_authz_state();

-- AUD-13: an alias is 1 to 40 characters and unique within its campaign,
-- compared case-insensitively. RQ-3/W-3: a participant is MARKED removed and
-- never deleted, so that a row another transaction references cannot vanish
-- under it — and a removed alias becomes free again.
--
-- WHY alias_key AND NOT lower(alias). `lower()` answers according to the
-- database's collation provider (libc or ICU, and which locale), while the
-- in-memory twin answers with Python — and the two disagree about a final
-- sigma and a dotted capital I, silently. `alias_key` is computed by the
-- application (service/participant_store.alias_key: NFKC, then casefold, which
-- is the full case-insensitive comparison Unicode defines and lower() is not),
-- so the two worlds agree by construction. It is longer than the alias on
-- purpose: casefolding can expand (ß becomes ss), and NFKC more so.
CREATE TABLE campaign.participants (
  id          TEXT PRIMARY KEY CHECK (id ~ '^prt_[A-Za-z0-9_-]{22,60}$'),
  campaign_id TEXT NOT NULL REFERENCES campaign.campaigns (id) ON DELETE CASCADE,
  alias       TEXT NOT NULL CHECK (length(alias) BETWEEN 1 AND 40),
  alias_key   TEXT NOT NULL CHECK (length(alias_key) BETWEEN 1 AND 200),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  removed_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX participants_alias_uidx
  ON campaign.participants (campaign_id, alias_key) WHERE removed_at IS NULL;

-- AUD-4: single-use, and an unused code expires seven days after it is issued.
-- SEC-5: the digest only, looked up by digest.
--
-- The "live" index deliberately ignores expiry, because a partial index cannot
-- read the clock. So a participant whose code has expired still holds the slot
-- until something revokes it: `issue_code` revokes the dead row in the same
-- statement pair that writes the new one (see service/participant_store.py).
CREATE TABLE campaign.enrolment_codes (
  id             TEXT PRIMARY KEY CHECK (id ~ '^enc_[A-Za-z0-9_-]{22,60}$'),
  participant_id TEXT NOT NULL REFERENCES campaign.participants (id) ON DELETE CASCADE,
  code_digest    TEXT NOT NULL CHECK (code_digest ~ '^[0-9a-f]{64}$'),
  expires_at     TIMESTAMPTZ NOT NULL,
  consumed_at    TIMESTAMPTZ,
  revoked_at     TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- A code is spent or it is cancelled, never both: the two say different
  -- things to an audit reader, and a row that claimed both would make
  -- "was this link ever used?" unanswerable.
  CHECK (consumed_at IS NULL OR revoked_at IS NULL)
);
CREATE UNIQUE INDEX enrolment_codes_digest_uidx ON campaign.enrolment_codes (code_digest);
CREATE UNIQUE INDEX enrolment_codes_live_uidx ON campaign.enrolment_codes (participant_id)
  WHERE consumed_at IS NULL AND revoked_at IS NULL;

-- AUD-5: one active device per participant. Replacing a device is revoking the
-- old credential and issuing a new one, which is what "reset my link" does.
CREATE TABLE campaign.device_credentials (
  id                TEXT PRIMARY KEY CHECK (id ~ '^dev_[A-Za-z0-9_-]{22,60}$'),
  participant_id    TEXT NOT NULL REFERENCES campaign.participants (id) ON DELETE CASCADE,
  credential_digest TEXT NOT NULL CHECK (credential_digest ~ '^[0-9a-f]{64}$'),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at        TIMESTAMPTZ,
  last_seen_at      TIMESTAMPTZ
);
CREATE UNIQUE INDEX device_credentials_digest_uidx
  ON campaign.device_credentials (credential_digest);
CREATE UNIQUE INDEX device_credentials_active_uidx
  ON campaign.device_credentials (participant_id) WHERE revoked_at IS NULL;

-- REVEAL-2: at most one LIVE session per GM, across every campaign they own —
-- a partial unique index, so the database refuses the second one however the
-- two requests raced. REVEAL-18: table audio is on by default. RQ-7: both
-- epochs live on the session row, so one lock on it covers both. SEC-9: a link
-- generation binds every credential made from it, so rotating the link is
-- incrementing this number and revoking that generation's credentials.
--
-- `expires_at` is the caller's (1kg.2.3 sets REVEAL-2's twelve hours). A session
-- past it is still `live` until something ends it, which is why
-- `expired_live_sessions_for_gm` exists: a start ends the GM's stale session
-- first rather than being refused by an index.
-- AUD-1: the GM of a session IS the campaign's owner, and the composite foreign
-- key below is what holds that rather than a route remembering to compare two
-- values. `gm_user_id` still cascades from auth.users so that deleting the
-- account takes the sessions with it whichever edge fires first.
CREATE TABLE campaign.table_sessions (
  id              TEXT PRIMARY KEY CHECK (id ~ '^ses_[A-Za-z0-9_-]{22,60}$'),
  campaign_id     TEXT NOT NULL,
  gm_user_id      BIGINT NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  state           TEXT NOT NULL CHECK (state IN ('live', 'ended', 'expired')),
  started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL,
  ended_at        TIMESTAMPTZ,
  link_generation INTEGER NOT NULL DEFAULT 1 CHECK (link_generation >= 1),
  link_digest     TEXT CHECK (link_digest IS NULL OR link_digest ~ '^[0-9a-f]{64}$'),
  reveal_epoch    BIGINT NOT NULL DEFAULT 0 CHECK (reveal_epoch >= 0),
  audio_epoch     BIGINT NOT NULL DEFAULT 0 CHECK (audio_epoch >= 0),
  table_audio     BOOLEAN NOT NULL DEFAULT TRUE,
  FOREIGN KEY (campaign_id, gm_user_id)
    REFERENCES campaign.campaigns (id, owner_id) ON DELETE CASCADE,
  -- `live` and `ended_at` are one fact written twice, so the database keeps
  -- them in step: a live session has no ending, and an ended or expired one
  -- has exactly one.
  CHECK ((state = 'live') = (ended_at IS NULL)),
  -- A session that expired before it began is a clock bug, not a session.
  CHECK (expires_at > started_at)
);
CREATE UNIQUE INDEX table_sessions_one_live_per_gm_uidx
  ON campaign.table_sessions (gm_user_id) WHERE state = 'live';
CREATE INDEX table_sessions_campaign_idx ON campaign.table_sessions (campaign_id, started_at);
CREATE INDEX table_sessions_expiry_idx
  ON campaign.table_sessions (expires_at) WHERE state = 'live';
-- The digest every unauthenticated join is looked up by, so it is an index, and
-- unique because nothing but 256-bit luck would otherwise make the lookup
-- single-valued. It must stay PARTIAL: PostgreSQL counts the columns of a
-- non-partial unique index as key columns, so a full one here would make
-- Rotate's `UPDATE ... SET link_digest` take FOR UPDATE and start conflicting
-- with the FOR KEY SHARE every join's foreign-key check takes — exactly what
-- RQ-3 forbids. `tests/test_campaign_db.py` proves the partial one does not.
CREATE UNIQUE INDEX table_sessions_link_digest_uidx
  ON campaign.table_sessions (link_digest) WHERE link_digest IS NOT NULL;

-- SEC-9: a credential belongs to the generation it was made in, so End and
-- Rotate can revoke exactly that generation's credentials in the same
-- transaction that advances the reveal and audio epochs.
CREATE TABLE campaign.table_credentials (
  id                TEXT PRIMARY KEY CHECK (id ~ '^tcr_[A-Za-z0-9_-]{22,60}$'),
  session_id        TEXT NOT NULL REFERENCES campaign.table_sessions (id) ON DELETE CASCADE,
  link_generation   INTEGER NOT NULL CHECK (link_generation >= 1),
  credential_digest TEXT NOT NULL CHECK (credential_digest ~ '^[0-9a-f]{64}$'),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at        TIMESTAMPTZ,
  last_connected_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX table_credentials_digest_uidx
  ON campaign.table_credentials (credential_digest);
CREATE INDEX table_credentials_session_idx
  ON campaign.table_credentials (session_id, link_generation);

-- SEC-10's durable per-generation join counter. Keyed by generation rather than
-- kept as a column on the session, because a rotation must neither reset the
-- count nor lose the previous generation's.
--
-- It carries a WINDOW START because SEC-10's successful-join throttle is "60 per
-- 10 minutes per generation" — a rate over a window, not a lifetime total — and
-- the same rule insists the counter "is a row, not memory, so a restart does not
-- reset it (R-7)". A bare monotonic total could not answer that question, and
-- adding the column after this file is pinned would cost another migration.
-- 1kg.2.3 owns the window arithmetic; this bead owns the storage.
--
-- What it deliberately does NOT hold: a lifetime total. The 24-credentials-per-
-- generation bound is answered by counting table_credentials of that generation,
-- which is also what "the GM sees the count" needs, and a refused burst is
-- audited (SEC-38) rather than tallied here.
CREATE TABLE campaign.session_join_counters (
  session_id        TEXT NOT NULL REFERENCES campaign.table_sessions (id) ON DELETE CASCADE,
  link_generation   INTEGER NOT NULL CHECK (link_generation >= 1),
  window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  joins             INTEGER NOT NULL DEFAULT 0 CHECK (joins >= 0),
  PRIMARY KEY (session_id, link_generation)
);

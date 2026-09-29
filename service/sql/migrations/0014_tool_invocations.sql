-- campaign.tool_invocations and campaign.tool_attempts: the GM tool invocation
-- aggregate (agent-forge-harness-1kg.4.1, slice A). This file's number is
-- written nowhere else in it: the lead renumbers a migration at merge when a
-- parallel bead takes the number first.
--
-- Pure expansion: two new tables and their indexes. Nothing that exists is read
-- for rewriting, backfilled, migrated or deleted. The release running right now
-- neither reads nor writes these tables, so it keeps working against a migrated
-- database (docs/migrations.md section 3), and a roll-forward after a rollback
-- loses nothing: a `working` row the newer build left behind expires lazily the
-- next time it is read.
--
-- WHY THE AGGREGATE EXISTS. A GM tool run is keyed by the client's own
-- invocation id, scoped to the GM and the campaign (docs/workbench-wire-contract.md,
-- Idempotency), so a repeat can be answered by state (RAIL-18): the stored
-- outcome of a finished run, the status of a running one, a new attempt of a
-- retryable failure. One row per invocation holds its request, its status, its
-- current attempt and that attempt's deadline, and its typed outcome; its
-- timeline entry (entry_id) carries the same invocation to the transcript.
--
-- campaign.tool_attempts IS THE ADMISSION RECORD, not a cost store. One row per
-- attempt, written in the transaction that admits it and BEFORE any provider
-- work, so the pilot's daily count of tool attempts is durable the moment an
-- attempt may spend (SEC-34, STATE-8 as the lead's C-15 reads it). It holds no
-- tokens, no prices, no model alias and no provider: every provider call is
-- recorded in metering.provider_attempts (0011) under the attempt's
-- operation_id, which is the only link between the two.
--
-- PRIVATE TEXT (SEC-20). The brief and the result's prose are the GM's, and live
-- here and nowhere outside the database: no log line, audit row, metric label
-- or URL carries them. NO HASH, DIGEST OR FINGERPRINT of any text is stored,
-- here or anywhere (ED-26); a repeat is never compared by a digest of its body.
--
-- OWNERSHIP IS IN THE QUERY (docs/migrations.md section 4). owner_id is part of
-- the key, and the statement that creates a row joins the owner's live campaign
-- to the owner's conversation of that campaign, so a row can only exist for a
-- conversation that belongs to the named campaign of the named GM. The
-- conversation edge cascades: an invocation, its attempts and its timeline
-- entry go with their conversation, and with the GM's account.
--
-- THE CAMPAIGN EDGE IS DEFERRABLE INITIALLY DEFERRED and NO ACTION, for
-- 0006_conversation_metadata.sql's reason: deleting the GM's account cascades
-- to the campaign and, separately, through the conversation to this row, and an
-- immediate check would depend on which referential trigger fires first.
-- Deferred to COMMIT, the account's own rows are gone by then and the delete
-- succeeds, while deleting a campaign an invocation still references is
-- refused. Nothing relies on this edge to refuse a write: the creating
-- statement's own guard does (G-9), because a deferred violation would only
-- surface at COMMIT. 1kg.2.6 decides campaign deletion for conversations and
-- invocations at once.
--
-- THE TOOL LIST IS AS OF THIS MIGRATION. A tool added after it needs its own
-- migration, because a released migration is never edited.

CREATE TABLE campaign.tool_invocations (
  owner_id          BIGINT      NOT NULL,
  campaign_id       TEXT        NOT NULL,
  invocation_id     TEXT        NOT NULL CHECK (invocation_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  conversation_id   TEXT        NOT NULL REFERENCES chat.conversations (conversation_id) ON DELETE CASCADE,
  entry_id          TEXT        NOT NULL UNIQUE REFERENCES chat.timeline_entries (entry_id) ON DELETE CASCADE,
  tool_id           TEXT        NOT NULL CHECK (tool_id IN ('npc', 'monster', 'loot', 'names', 'rules',
                                                            'portrait', 'encounter', 'hooks', 'recap', 'map')),
  brief             TEXT        NOT NULL CHECK (length(brief) <= 2000),
  source_entry_id   TEXT        CHECK (source_entry_id IS NULL OR source_entry_id ~ '^[A-Za-z0-9_-]{1,64}$'),
  status            TEXT        NOT NULL CHECK (status IN ('working', 'done', 'failed', 'cancelled')),
  attempt           INTEGER     NOT NULL CHECK (attempt BETWEEN 1 AND 100),
  attempt_deadline  TIMESTAMPTZ NOT NULL,
  cancel_requested  BOOLEAN     NOT NULL,
  result            JSONB,
  error             JSONB,
  schema_version    INTEGER     NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL,
  updated_at        TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (owner_id, campaign_id, invocation_id),
  FOREIGN KEY (campaign_id, owner_id) REFERENCES campaign.campaigns (id, owner_id)
    DEFERRABLE INITIALLY DEFERRED,
  -- Only `done` carries a result and only `failed` an error, as the contract's
  -- ToolInvocation says, so a stale result can never sit under a working row.
  CHECK ((status = 'done') = (result IS NOT NULL)),
  CHECK ((status = 'failed') = (error IS NOT NULL))
);

-- The X-5 count: one GM's working rows, read with the deadline predicate.
CREATE INDEX tool_invocations_in_flight_idx
  ON campaign.tool_invocations (owner_id, attempt_deadline) WHERE status = 'working';

CREATE TABLE campaign.tool_attempts (
  owner_id      BIGINT      NOT NULL,
  campaign_id   TEXT        NOT NULL,
  invocation_id TEXT        NOT NULL,
  attempt       INTEGER     NOT NULL CHECK (attempt BETWEEN 1 AND 100),
  operation_id  TEXT        NOT NULL UNIQUE CHECK (operation_id ~ '^[0-9a-f]{32}$'),
  started_at    TIMESTAMPTZ NOT NULL,
  deadline_at   TIMESTAMPTZ NOT NULL CHECK (deadline_at > started_at),
  ended_at      TIMESTAMPTZ,
  outcome       TEXT        CHECK (outcome IS NULL OR outcome IN ('done', 'failed', 'cancelled', 'expired')),
  PRIMARY KEY (owner_id, campaign_id, invocation_id, attempt),
  FOREIGN KEY (owner_id, campaign_id, invocation_id)
    REFERENCES campaign.tool_invocations (owner_id, campaign_id, invocation_id) ON DELETE CASCADE,
  CHECK ((ended_at IS NULL) = (outcome IS NULL))
);

-- The pilot day's count of tool attempts since UTC midnight.
CREATE INDEX tool_attempts_started_idx ON campaign.tool_attempts (started_at);

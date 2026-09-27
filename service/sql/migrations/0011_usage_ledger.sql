-- metering: the provider-attempt cost ledger and the price table it is priced
-- from (agent-forge-harness-yje.5.1.2, "yje.5.1 slice b"). This file's number is
-- written nowhere else in it: the lead renumbers a migration at merge when a
-- parallel bead takes the number first.
--
-- PURE EXPANSION. A new schema, two new tables, their indexes and one seed row.
-- Nothing that exists is read for rewriting, backfilled or dropped. The release
-- running right now neither reads nor writes `metering`, so it keeps working
-- against a migrated database (docs/migrations.md section 3). The schema is not
-- called `billing`: this is cost, not revenue, and revenue is yje.3.7's.
--
-- APPEND-ONLY. One row per provider attempt (a request sent or attempted), keyed
-- by the turn's operation and an operation-wide attempt sequence. There is no
-- update path and no delete path for either table, here, in
-- service/usage_ledger.py or in the runbook: a cost ledger that can be edited
-- answers no question that matters (the posture of the audit ledger, and like
-- it, no trigger).
--
-- NO TEXT (X-7, SEC-20). Every column is an id, a closed code, a count, a time
-- or a price. The open vocabularies (operation, purpose, mode, provider, alias)
-- are pinned to identifier shapes that no sentence fits, so a later bead can add
-- a purpose without a migration while a prompt, an answer or a filename cannot
-- land here. The price table's `source` is the one free-text column, written by
-- the operator: where and when a price was read.
--
-- NO FOREIGN KEY OUT OF THE SCHEMA. `billed_account_id` names an account and
-- `campaign_id` a campaign, as plain values. A cost row outlives what it names,
-- for the audit ledger's reason: a CASCADE would erase cost history with the
-- account, and NO ACTION would make metering rows block an account's deletion.
-- What deletion and retention do to these rows is decided elsewhere (zkc, 7ak).
-- The one foreign key is inside the schema: a row's price revision, which is
-- never deleted.
--
-- HOW A PRICE IS CORRECTED. A price row is immutable; a correction is a new row.
-- Wrong numbers: insert the same provider, alias and effective_from again with
-- the right numbers; the later-recorded row (greatest id) governs that window
-- from then on. A price entered late, or for the first time: insert it with its
-- true, past effective_from, and it governs every attempt from that instant,
-- including rows stored as unpriced. A future change: insert it with a future
-- effective_from. A ledger row keeps the revision that was in force when it was
-- written, as provenance; its cost is always computed from the revision in force
-- now, and cost itself is never stored.

CREATE SCHEMA metering;

CREATE TABLE metering.price_revisions (
  id                        BIGSERIAL PRIMARY KEY,
  provider                  TEXT NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_-]{0,39}$'),
  alias                     TEXT NOT NULL CHECK (alias ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$'),
  effective_from            TIMESTAMPTZ NOT NULL,
  input_usd_per_mtok        NUMERIC(12,6) NOT NULL CHECK (input_usd_per_mtok >= 0),
  cached_input_usd_per_mtok NUMERIC(12,6)
                            CHECK (cached_input_usd_per_mtok IS NULL OR cached_input_usd_per_mtok >= 0),
  output_usd_per_mtok       NUMERIC(12,6) CHECK (output_usd_per_mtok IS NULL OR output_usd_per_mtok >= 0),
  source                    TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 300),
  recorded_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The revision in force: the greatest effective_from at or before the instant,
-- ties to the later-recorded row.
CREATE INDEX price_revisions_lookup_idx
  ON metering.price_revisions (provider, alias, effective_from DESC, id DESC);

CREATE TABLE metering.provider_attempts (
  operation_id        TEXT NOT NULL CHECK (operation_id ~ '^[0-9a-f]{32}$'),
  attempt_index       INTEGER NOT NULL CHECK (attempt_index >= 0),
  occurred_at         TIMESTAMPTZ NOT NULL,
  operation           TEXT NOT NULL CHECK (operation ~ '^[a-z][a-z0-9_]{0,39}$'),
  purpose             TEXT NOT NULL CHECK (purpose ~ '^[a-z][a-z0-9_]{0,39}$'),
  mode                TEXT CHECK (mode IS NULL OR mode ~ '^[a-z][a-z0-9_]{0,39}$'),
  alias               TEXT NOT NULL CHECK (alias ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$'),
  provider            TEXT CHECK (provider IS NULL OR provider ~ '^[a-z][a-z0-9_-]{0,39}$'),
  retry_index         INTEGER NOT NULL CHECK (retry_index >= 0),
  status              TEXT NOT NULL CHECK (status IN ('ok', 'error')),
  input_tokens        INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
  cached_input_tokens INTEGER CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
  output_tokens       INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
  reasoning_tokens    INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
  billed_account_id   BIGINT NOT NULL CHECK (billed_account_id > 0),
  actor_kind          TEXT NOT NULL CHECK (actor_kind IN ('account', 'participant', 'system')),
  campaign_id         TEXT CHECK (campaign_id IS NULL OR campaign_id ~ '^cmp_[A-Za-z0-9_-]{22,60}$'),
  price_revision_id   BIGINT REFERENCES metering.price_revisions (id) ON DELETE NO ACTION,
  PRIMARY KEY (operation_id, attempt_index)
);

-- "The cost for one account over [since, until)" as a range scan.
CREATE INDEX provider_attempts_account_time_idx
  ON metering.provider_attempts (billed_account_id, occurred_at);

-- The one seeded price: OpenAI's list for the one enabled chat model, per
-- million tokens. No embedding price (the owner supplies it) and no price for a
-- model the catalog does not enable.
INSERT INTO metering.price_revisions
  (provider, alias, effective_from, input_usd_per_mtok, cached_input_usd_per_mtok, output_usd_per_mtok, source)
VALUES
  ('openai', 'gpt-4o-mini', '2026-09-24T00:00:00+00:00', 0.150000, 0.075000, 0.600000,
   'OpenAI API pricing, read 2026-09-24 (billing plan D-8 evidence note)');

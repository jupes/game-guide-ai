-- Migration 0005 — the audit ledger (1kg.2.1).
--
-- One append-only table for the decisions the Workbench has to be able to
-- account for afterwards (SEC-38): who joined, what was revoked, which code was
-- spent, what was refused and why. There is no update path and no delete path,
-- here or in service/audit_log.py: a ledger that can be edited answers no
-- question that matters.
--
-- WHAT IS NOT IN A ROW. No alias, no campaign name, no conversation title, no
-- field text, no filename — and, unlike the shape this table was first sketched
-- with, no payload_hash either. ED-26 is explicit that no value derived from
-- field text may outlive the text, and a hash of a brief is such a value: it
-- survives the deletion it was supposed to be erased by, and it still answers
-- "was it this one?" to anyone holding a guess. `detail` carries identifiers,
-- field keys, numbers and booleans, and a writer-side validator enforces that
-- rather than leaving it to the CHECK (service/audit_log.check_detail, modelled
-- on service/jobs.check_payload).
--
-- WHY campaign_id_tombstone HAS NO FOREIGN KEY. SEC-36 says the opposite about
-- everything that hangs off a campaign — deleting it deletes documents,
-- versions, reveal state, sessions, credentials, codes, participants, aliases,
-- cues and asset rows — and migration 0004 cascades accordingly. What licenses
-- a row that outlives the campaign is ED-26's "ledger and audit rows, which are
-- tombstoned past campaign deletion" plus ED-18(a)'s audit retention. So the
-- column keeps the campaign's identifier as plain text: the row stays legible
-- and attributable, and it is no longer reachable from the campaign it names.
--
-- The primary key is BIGSERIAL rather than a minted identifier. SEC-4 governs
-- identifiers that are exposed and can be probed; an audit row id is never
-- served to anyone, and app.jobs (migration 0003) is BIGSERIAL for the same
-- reason. Every id that a row REFERS to is a minted one.

CREATE SCHEMA audit;

CREATE TABLE audit.events (
  id                    BIGSERIAL PRIMARY KEY,
  campaign_id_tombstone TEXT NOT NULL,
  actor_kind            TEXT NOT NULL CHECK (actor_kind IN ('gm','participant','guest','system')),
  actor_ref             TEXT,
  action                TEXT NOT NULL CHECK (action <> '' AND length(action) <= 60),
  object_kind           TEXT NOT NULL CHECK (object_kind <> '' AND length(object_kind) <= 40),
  object_ref            TEXT,
  decision              TEXT NOT NULL CHECK (decision IN ('allowed','refused')),
  reason_code           TEXT CHECK (reason_code IS NULL OR length(reason_code) <= 60),
  authz_revision        BIGINT CHECK (authz_revision IS NULL OR authz_revision >= 0),
  detail                JSONB NOT NULL DEFAULT '{}'::jsonb
                          CHECK (jsonb_typeof(detail) = 'object'),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What reading the ledger looks like: one campaign, in time order.
CREATE INDEX events_campaign_created_idx
  ON audit.events (campaign_id_tombstone, created_at);

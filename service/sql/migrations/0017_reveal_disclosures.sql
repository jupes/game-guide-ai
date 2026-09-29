-- Migration 0017 — what the table may see, as explicit server state
-- (agent-forge-harness-1kg.7.1).
--
-- Owner decision O-3 (shared eligibility ADR, ED-15 as rewritten; interactions
-- ADR A-16, A-19, A-20): a Confirm persists a DISCLOSURE — one Confirm's worth of
-- display: a document, the version it pins, the mask of field keys and the kind
-- of audience — and SLOT ROWS that reference it. A slot is one audience slot of
-- one session: the table slot (participant_id NULL) or one participant's slot.
-- Nothing about visibility is stored on a document or a version row (ED-6): the
-- pin is reached slot -> disclosure -> (document, version).
--
-- WHAT THIS FILE DOES, EXACTLY.
--   (a) UNIQUE (id, campaign_id) on campaign.table_sessions and on
--       campaign.documents, so the composite foreign keys below can target them;
--   (b) campaign.reveal_disclosures, with a PARTIAL unique index that keeps a
--       document to at most one live disclosure, and a unique index that makes a
--       Confirm's (session, command) its idempotency key;
--   (c) campaign.reveal_slots, one row per audience slot of a session, whose one
--       nullable pointer is "a slot holds one live projection".
--
-- INVARIANTS THE DATABASE HOLDS, so no route can forget them. A disclosure's
-- session and document are both of its campaign, and its version is one of its
-- document's (composite keys). A slot's session and participant are of its
-- campaign. A slot points only at a disclosure of its own session and of its
-- own audience kind, so a table slot can never show a participant disclosure,
-- nor the reverse, nor a slot show another session's display. `ended_at` and
-- `ended_reason` are both set or both NULL. `source_group_id` is reserved for
-- `1ir.2.1` and is NULL until that bead relaxes its CHECK in its own migration.
--
-- WHY THE SLOT -> DISCLOSURE KEY HAS NO DELETE ACTION. Deleting a document, or
-- a version, whose disclosure still has a live copy FAILS: the bead that deletes
-- a document narrows first. A cascade that also removes the slots — deleting a
-- session, a campaign or an account — passes, because NO ACTION is checked once
-- the whole statement's cascades have run.
--
-- WHY NO UPDATE HERE EVER TAKES FOR UPDATE (RQ-3). The only columns the store
-- updates are reveal_slots (seq, disclosure_id, updated_at) and
-- reveal_disclosures (ended_at, ended_reason). None of them is a column of a
-- non-partial unique index: `ended_at` sits only in the one-live index's
-- predicate, which PostgreSQL does not count as a key. So each UPDATE takes
-- FOR NO KEY UPDATE, and the FOR KEY SHARE of a foreign-key check never waits
-- for it, nor it for one.
--
-- DELIBERATELY ABSENT. No `ended_at >= created_at` CHECK: clocks skew between
-- instances, and a narrowing must never be refused by one. No DEFAULT now():
-- the clock is the application's, passed as a parameter. No column on
-- documents or document_versions (ED-6).
--
-- THE THREE PROOFS THIS FILE CARRIES.
--
-- 1. It is an expansion (docs/migrations.md section 3). Both tables are new
--    and empty at creation, and no build before this one reads or writes them;
--    the two UNIQUE constraints add no column. No backfill: there is no display
--    to move, and ED-18 forbids reconstructing one.
--
-- 2. The two UNIQUE constraints cannot fail and change no lock mode. Each covers
--    `id`, which the primary key already makes unique, so every existing row
--    satisfies it. No build updates `id` or `campaign_id` of a table session or
--    a document (`git grep "UPDATE campaign.table_sessions"` and
--    `"UPDATE campaign.documents"`: each SET names epochs, state, the
--    generation, the command, data, revisions, archive or the sheet link), so no
--    UPDATE the previous build runs becomes a key update and starts taking
--    FOR UPDATE (RQ-3) — the argument 0012 made for participants_id_campaign_key.
--
-- 3. No deployed build has a campaign schema: master's production build has no
--    migration runner, and the integration branch deploys nowhere. So there is
--    no rollback to a build these foreign keys could surprise. Even mid-rollout,
--    an older build's narrowing (which clears no slot) could leave a live
--    disclosure in an ended session: every read gates on the session being live
--    and unexpired at the application's clock, and the next Confirm or the
--    campaign's reconciliation ends it.
--
-- The mask CHECK joins the keys with commas and matches the joined text
-- against the field-key shape, and refuses a comma INSIDE any key, which the
-- joined text alone cannot see (one key 'name,voice' joins to the same text as
-- two). It uses array_to_string and strpos, which PostgreSQL marks STABLE, not
-- IMMUTABLE; a CHECK accepts it (an index expression would not), and
-- tests/test_reveal_db.py proves the CHECK both accepts and refuses on the
-- server. `UNIQUE NULLS NOT DISTINCT` needs PostgreSQL 15 or later; CI and
-- production run 17.
--
-- No transaction control and not CONCURRENTLY: the runner owns the transaction
-- (docs/migrations.md section 2), and none of these tables has ever held a
-- production row.

ALTER TABLE campaign.table_sessions
  ADD CONSTRAINT table_sessions_id_campaign_key UNIQUE (id, campaign_id);
ALTER TABLE campaign.documents
  ADD CONSTRAINT documents_id_campaign_key UNIQUE (id, campaign_id);

CREATE TABLE campaign.reveal_disclosures (
  id              TEXT PRIMARY KEY CHECK (id ~ '^dsc_[A-Za-z0-9_-]{22,60}$'),
  campaign_id     TEXT NOT NULL,
  session_id      TEXT NOT NULL,
  document_id     TEXT NOT NULL,
  version_number  INT  NOT NULL CHECK (version_number BETWEEN 1 AND 1000000),
  content_kind    TEXT NOT NULL DEFAULT 'document' CHECK (content_kind = 'document'),
  mask            TEXT[] NOT NULL CHECK (
                    cardinality(mask) BETWEEN 1 AND 64
                    AND array_position(mask, NULL) IS NULL
                    AND array_to_string(mask, ',') ~ '^[a-z][a-z0-9_]{0,39}(,[a-z][a-z0-9_]{0,39})*$'
                    AND strpos(array_to_string(mask, ''), ',') = 0
                    AND NOT ('all' = ANY (mask))),
  audience_kind   TEXT NOT NULL CHECK (audience_kind IN ('table', 'participant')),
  source_group_id TEXT CHECK (source_group_id IS NULL),
  command_id      TEXT NOT NULL CHECK (command_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  created_at      TIMESTAMPTZ NOT NULL,
  ended_at        TIMESTAMPTZ,
  ended_reason    TEXT CHECK (ended_reason IN ('gm_stop','stop_all','replaced','moved','updated',
                    'gm_end','expired','link_rotated','participant_removed','character_unlinked',
                    'document_archived','document_deleted','campaign_archived','reconciled','narrowed')),
  CHECK ((ended_at IS NULL) = (ended_reason IS NULL)),
  UNIQUE (id, session_id, audience_kind),
  FOREIGN KEY (session_id, campaign_id)
    REFERENCES campaign.table_sessions (id, campaign_id) ON DELETE CASCADE,
  FOREIGN KEY (document_id, campaign_id)
    REFERENCES campaign.documents (id, campaign_id) ON DELETE CASCADE,
  FOREIGN KEY (document_id, version_number)
    REFERENCES campaign.document_versions (document_id, number) ON DELETE CASCADE
);
CREATE UNIQUE INDEX reveal_disclosures_one_live_per_document_uidx
  ON campaign.reveal_disclosures (document_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX reveal_disclosures_command_uidx
  ON campaign.reveal_disclosures (session_id, command_id);

CREATE TABLE campaign.reveal_slots (
  id             TEXT PRIMARY KEY CHECK (id ~ '^rsl_[A-Za-z0-9_-]{22,60}$'),
  campaign_id    TEXT NOT NULL,
  session_id     TEXT NOT NULL,
  audience_kind  TEXT NOT NULL CHECK (audience_kind IN ('table', 'participant')),
  participant_id TEXT,
  seq            BIGINT NOT NULL DEFAULT 0 CHECK (seq BETWEEN 0 AND 9007199254740991),
  disclosure_id  TEXT,
  updated_at     TIMESTAMPTZ NOT NULL,
  CHECK ((audience_kind = 'table') = (participant_id IS NULL)),
  UNIQUE NULLS NOT DISTINCT (session_id, participant_id),
  FOREIGN KEY (session_id, campaign_id)
    REFERENCES campaign.table_sessions (id, campaign_id) ON DELETE CASCADE,
  FOREIGN KEY (participant_id, campaign_id)
    REFERENCES campaign.participants (id, campaign_id) ON DELETE CASCADE,
  FOREIGN KEY (disclosure_id, session_id, audience_kind)
    REFERENCES campaign.reveal_disclosures (id, session_id, audience_kind)
);
CREATE INDEX reveal_slots_disclosure_idx
  ON campaign.reveal_slots (disclosure_id) WHERE disclosure_id IS NOT NULL;

-- Migration: per-field eligibility, named groups, the projection revision and
-- the projection queue (agent-forge-harness-1ir.2.1).
--
-- The shared eligibility / display / disclosure ADR (docs/adr/
-- shared-eligibility-display-disclosure.md, ACCEPTED) and the live-session
-- plan's section 4.3. It is expand-only: 0001 to 0018 are released and are not
-- edited, nothing is dropped and no existing row is rewritten.
--
-- WHAT THIS FILE DOES NOT ADD. There is no characters table: a character IS a
-- character-sheet document, and "linked to a participant" is 0008's
-- documents.linked_participant_id (lead ruling R-6: one link fact, never two).
-- There is no 'unclassified' row: a revealable field without a row IS
-- unclassified, and resetting a field deletes its row. There is no suggestion
-- row: every row here is evaluated, so only a GM classification may widen.
--
-- 1. authz_state.projection_revision, with the CHECK that it never runs ahead of
--    authz_revision (RQ-1, RQ-10). THE CHECK IS SAFE ON AN EXISTING TABLE: the
--    column is new and defaults to 0, so every existing row has
--    0 <= projection_revision <= authz_revision; the previous build never writes
--    the column and only ever INCREMENTS authz_revision, so a row it writes
--    during a rollout satisfies it too. The column can be NOT NULL because it is
--    new and has a default, which 0004's AFTER INSERT trigger picks up: its
--    INSERT ... (campaign_id) names no other column. NO BACKFILL: an existing
--    campaign reads "revision ahead, queue empty", which the projector (1ir.2.3)
--    treats as work to do, and an UPDATE of every authz_state row during a
--    rollout would contend with the previous build's live campaign locks.
--    Adding the column briefly takes ACCESS EXCLUSIVE on authz_state; it waits at
--    most for in-flight campaign-lock holders, which RQ-8 bounds at 5 s.
--
-- 2. NO KEY IS ADDED TO campaign.documents. The composite foreign keys below
--    target documents_id_campaign_key UNIQUE (id, campaign_id), which 0018
--    (1kg.7.1, reveal disclosures) added as the target of its own composite
--    keys, with the proof this file would otherwise carry: it covers id, which
--    the primary key already makes unique, and no document UPDATE changes id or
--    campaign_id, so every one stays FOR NO KEY UPDATE (RQ-3). The only existing
--    table this file alters is authz_state (point 1).
--
-- 3. campaign.principal_ids_ok: one list of principal ids, 1 to 100 of one
--    prefix, no NULL element, one dimension, strictly ascending in code-point
--    order (so distinct and canonical). NOT STRICT and COALESCEd to false: a
--    CHECK that evaluates to NULL passes, so a NULL here must become false.
--
-- 4. campaign.groups: a GM's named group of seats (ED-4, owner decision O-3).
--    The name is private GM text (SEC-20), bounded as an alias is; name_fold is
--    the application's alias_key. A group is marked removed, never deleted,
--    because a disclosure will remember the group it came from. created_command_id
--    is NULL or the wire contract's CommandId shape.
--
-- 5. campaign.group_members: a seat in a group. Removal is a DELETE: nothing
--    references a membership row.
--
-- 6. campaign.field_eligibility: one row per classified field. field_key is the
--    Workbench's flat field key and never the reserved word 'all' (ED-2, ED-8);
--    the six stored classes omit 'unclassified' (the absence of a row); only a
--    'gm' classification may be wider than gm_only (ED-7); a list class carries
--    a principal list and every other class carries none.
--
-- 7. campaign.projection_queue: a field whose table-namespace rows the
--    projector must rebuild, stamped with the revision that queued it. Ids and a
--    key only, never a class, a list or text (plan section 4.3 rule 6).
--
-- Every new table carries campaign_id and a composite foreign key to its parent,
-- so the database refuses a row whose campaign disagrees with its parent's
-- (SEC-2), and every one of them cascades on delete. The four new tables and the
-- function are new, so every CHECK on them validates nothing that exists.
--
-- Rollback is sending traffic back to the previous image, which never reads the
-- new tables and keeps working against this schema (docs/migrations.md
-- section 3). Once merged this file is released: it is never reverted or edited,
-- and a mistake in it is fixed by the next migration.

ALTER TABLE campaign.authz_state
  ADD COLUMN projection_revision BIGINT NOT NULL DEFAULT 0,
  ADD CONSTRAINT authz_state_projection_not_ahead_chk
    CHECK (projection_revision >= 0 AND projection_revision <= authz_revision);

CREATE FUNCTION campaign.principal_ids_ok(ids TEXT[], pattern TEXT) RETURNS boolean
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT COALESCE(
    ids IS NOT NULL
    AND array_ndims(ids) = 1
    AND cardinality(ids) BETWEEN 1 AND 100
    AND NOT EXISTS (SELECT 1 FROM unnest(ids) AS i WHERE i IS NULL OR i !~ pattern)
    AND NOT EXISTS (
      SELECT 1 FROM generate_subscripts(ids, 1) AS s
      WHERE s > array_lower(ids, 1) AND NOT (ids[s - 1] COLLATE "C" < ids[s] COLLATE "C")
    ),
    false)
$$;

CREATE TABLE campaign.groups (
  id                 TEXT PRIMARY KEY CHECK (id ~ '^grp_[A-Za-z0-9_-]{22,60}$'),
  campaign_id        TEXT NOT NULL REFERENCES campaign.campaigns (id) ON DELETE CASCADE,
  name               TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
  name_fold          TEXT NOT NULL CHECK (length(name_fold) BETWEEN 1 AND 200),
  created_command_id TEXT CHECK (created_command_id IS NULL OR created_command_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  removed_at         TIMESTAMPTZ,
  CONSTRAINT groups_id_campaign_key UNIQUE (id, campaign_id)
);
-- Partial: a removed group frees its name. name_fold is updated by a rename, so
-- the index MUST stay partial (a non-partial unique index makes its columns key
-- columns, and a rename would then take FOR UPDATE: the 0004/0008 rule).
CREATE UNIQUE INDEX groups_live_name_uidx ON campaign.groups (campaign_id, name_fold) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX groups_command_uidx ON campaign.groups (campaign_id, created_command_id)
  WHERE created_command_id IS NOT NULL;

CREATE TABLE campaign.group_members (
  group_id       TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  campaign_id    TEXT NOT NULL,
  added_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (group_id, participant_id),
  FOREIGN KEY (group_id, campaign_id) REFERENCES campaign.groups (id, campaign_id) ON DELETE CASCADE,
  FOREIGN KEY (participant_id, campaign_id) REFERENCES campaign.participants (id, campaign_id) ON DELETE CASCADE
);
CREATE INDEX group_members_participant_idx ON campaign.group_members (participant_id);

CREATE TABLE campaign.field_eligibility (
  document_id           TEXT NOT NULL,
  field_key             TEXT NOT NULL CHECK (field_key ~ '^[a-z][a-z0-9_]{0,39}$' AND field_key <> 'all'),
  campaign_id           TEXT NOT NULL,
  eligibility_class     TEXT NOT NULL
    CHECK (eligibility_class IN ('gm_only', 'participants', 'characters', 'groups', 'campaign', 'public')),
  principal_ids         TEXT[],
  classification_source TEXT NOT NULL CHECK (classification_source IN ('gm', 'default')),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (document_id, field_key),
  FOREIGN KEY (document_id, campaign_id) REFERENCES campaign.documents (id, campaign_id) ON DELETE CASCADE,
  CONSTRAINT field_eligibility_only_gm_widens_chk
    CHECK (classification_source = 'gm' OR eligibility_class = 'gm_only'),
  CONSTRAINT field_eligibility_principals_chk CHECK (
    CASE eligibility_class
      WHEN 'participants' THEN campaign.principal_ids_ok(principal_ids, '^prt_[A-Za-z0-9_-]{22,60}$')
      WHEN 'characters'   THEN campaign.principal_ids_ok(principal_ids, '^doc_[A-Za-z0-9_-]{22,60}$')
      WHEN 'groups'       THEN campaign.principal_ids_ok(principal_ids, '^grp_[A-Za-z0-9_-]{22,60}$')
      ELSE principal_ids IS NULL
    END)
);
CREATE INDEX field_eligibility_campaign_idx ON campaign.field_eligibility (campaign_id);

CREATE TABLE campaign.projection_queue (
  id             BIGSERIAL PRIMARY KEY,
  campaign_id    TEXT NOT NULL,
  document_id    TEXT NOT NULL,
  field_key      TEXT NOT NULL CHECK (field_key ~ '^[a-z][a-z0-9_]{0,39}$' AND field_key <> 'all'),
  authz_revision BIGINT NOT NULL CHECK (authz_revision >= 1),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (document_id, campaign_id) REFERENCES campaign.documents (id, campaign_id) ON DELETE CASCADE
);
CREATE INDEX projection_queue_campaign_idx ON campaign.projection_queue (campaign_id, id);
-- A document delete scans the queue for its foreign key: without this index
-- that scan holds the delete's locks for as long as the queue is long.
CREATE INDEX projection_queue_document_idx ON campaign.projection_queue (document_id);

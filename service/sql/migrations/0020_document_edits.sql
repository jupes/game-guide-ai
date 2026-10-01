-- campaign.document_edits and campaign.document_edit_attempts: the AI document
-- edit aggregate (agent-forge-harness-1kg.5.5, PR-1). This file's number is
-- written nowhere else in it: the lead renumbers a migration at merge when a
-- parallel bead takes the number first.
--
-- Pure expansion: two new tables and their indexes; nothing that exists is
-- rewritten, backfilled or deleted. The release running now neither reads nor
-- writes them (docs/migrations.md section 3), and a `working` row a newer build
-- left behind expires lazily the next time it is read.
--
-- WHY THE AGGREGATE EXISTS. An AI edit is keyed by the client's own invocation
-- id, scoped to the GM and the campaign, so a repeat is answered by state, as a
-- tool invocation's is (RAIL-18). It is a table of its own, not a kind of tool
-- invocation: every tool statement would otherwise need an edit predicate, and
-- the two key spaces stay separate. A row holds the request (scope, instruction,
-- the base write revision it was asked against), its status, its attempt and
-- deadline, and its typed outcome; document_type and document_title are the
-- timeline entry's DocumentLink as it was when the edit was asked for, set on
-- create and never updated, so the entry can be rewritten from the row alone.
--
-- campaign.document_edit_attempts IS THE ADMISSION RECORD, not a cost store:
-- one row per attempt, written before any provider work, so the X-5 cap and the
-- pilot day, which count tool and edit attempts together, are durable the moment
-- an attempt may spend (SEC-34, STATE-8). No tokens, prices, alias or provider:
-- metering.provider_attempts (0011) links to it by operation_id alone.
--
-- PRIVATE TEXT (SEC-20). The instruction, the selected text, the title and the
-- result are the GM's and live nowhere outside the database. The selected text
-- is set to NULL by the statement that settles an edit in a final state. NO
-- HASH, DIGEST OR FINGERPRINT of any text is stored, here or anywhere (ED-26).
--
-- OWNERSHIP IS IN THE QUERY (docs/migrations.md section 4). owner_id is part of
-- the key, and the creating statement joins the owner's live campaign to the
-- owner's conversation of it and to a document of it. The conversation edge
-- cascades, so an edit goes with its conversation and with the GM's account.
-- The campaign edge is DEFERRABLE INITIALLY DEFERRED and NO ACTION, for the
-- reason 0014_tool_invocations.sql gives; 1kg.2.6 decides campaign deletion for
-- conversations, tool invocations and edits at once.
--
-- THERE IS NO FOREIGN KEY TO THE DOCUMENT: an edit's row and entry outlive a
-- deleted document to say what happened (`not_found`). The creating statement's
-- guard is what requires the document to exist, in that campaign, of that type.
-- The document type list is as of this migration; a later type needs its own.

CREATE TABLE campaign.document_edits (
  owner_id            BIGINT      NOT NULL,
  campaign_id         TEXT        NOT NULL,
  invocation_id       TEXT        NOT NULL CHECK (invocation_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  conversation_id     TEXT        NOT NULL REFERENCES chat.conversations (conversation_id) ON DELETE CASCADE,
  entry_id            TEXT        NOT NULL UNIQUE REFERENCES chat.timeline_entries (entry_id) ON DELETE CASCADE,
  document_id         TEXT        NOT NULL CHECK (document_id ~ '^doc_[A-Za-z0-9_-]{22,60}$'),
  document_type       TEXT        NOT NULL CHECK (document_type IN ('npc', 'statblock', 'handout', 'session-notes',
                                                                    'quest-log', 'character-sheet', 'lore',
                                                                    'encounter')),
  document_title      TEXT        NOT NULL CHECK (length(document_title) BETWEEN 1 AND 200),
  scope_kind          TEXT        NOT NULL CHECK (scope_kind IN ('document', 'field', 'selection')),
  scope_field         TEXT        CHECK (scope_field IS NULL OR scope_field ~ '^[a-z][a-z0-9_]{0,39}$'),
  selection_start     INTEGER,
  selection_end       INTEGER,
  selection_text      TEXT        CHECK (selection_text IS NULL OR length(selection_text) <= 20000),
  instruction_kind    TEXT        NOT NULL CHECK (instruction_kind IN ('text', 'action')),
  instruction_text    TEXT        CHECK (instruction_text IS NULL OR length(instruction_text) BETWEEN 1 AND 2000),
  instruction_action  TEXT        CHECK (instruction_action IS NULL
                                         OR instruction_action IN ('rewrite', 'shorter', 'darker')),
  base_write_revision BIGINT      NOT NULL CHECK (base_write_revision BETWEEN 1 AND 9007199254740991),
  status              TEXT        NOT NULL CHECK (status IN ('working', 'done', 'failed', 'cancelled')),
  attempt             INTEGER     NOT NULL CHECK (attempt BETWEEN 1 AND 100),
  attempt_deadline    TIMESTAMPTZ NOT NULL,
  cancel_requested    BOOLEAN     NOT NULL,
  result              JSONB,
  error               JSONB,
  schema_version      INTEGER     NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL,
  updated_at          TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (owner_id, campaign_id, invocation_id),
  FOREIGN KEY (campaign_id, owner_id) REFERENCES campaign.campaigns (id, owner_id)
    DEFERRABLE INITIALLY DEFERRED,
  -- Only `done` carries a result and only `failed` an error, as the contract's
  -- EditInvocation says, so a stale result can never sit under a working row.
  CHECK ((status = 'done') = (result IS NOT NULL)),
  CHECK ((status = 'failed') = (error IS NOT NULL)),
  -- The scope as the contract's EditScope has it: a document scope names no
  -- field; a selection, and only a selection, has a span, in code points, of
  -- the contract's own bounds.
  CHECK ((scope_kind = 'document') = (scope_field IS NULL)),
  CHECK ((selection_start IS NULL) = (selection_end IS NULL)),
  CHECK ((scope_kind = 'selection') = (selection_start IS NOT NULL)),
  CHECK (selection_start IS NULL OR (0 <= selection_start AND selection_start < selection_end
                                     AND selection_end <= 20000)),
  CHECK (selection_text IS NULL
         OR (scope_kind = 'selection' AND length(selection_text) = selection_end - selection_start)),
  -- The instruction as the contract's EditInstruction has it, and CANVAS-23: a
  -- SelectionBar action is scoped to a selection.
  CHECK ((instruction_kind = 'text') = (instruction_text IS NOT NULL)),
  CHECK ((instruction_kind = 'action') = (instruction_action IS NOT NULL)),
  CHECK (instruction_kind = 'text' OR scope_kind = 'selection')
);

-- The X-5 count: one GM's working rows, read with the deadline predicate.
CREATE INDEX document_edits_in_flight_idx
  ON campaign.document_edits (owner_id, attempt_deadline) WHERE status = 'working';

-- One AI edit per document (X-5, CANVAS-24): the working rows of one document.
CREATE INDEX document_edits_document_idx
  ON campaign.document_edits (owner_id, campaign_id, document_id) WHERE status = 'working';

CREATE TABLE campaign.document_edit_attempts (
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
    REFERENCES campaign.document_edits (owner_id, campaign_id, invocation_id) ON DELETE CASCADE,
  CHECK ((ended_at IS NULL) = (outcome IS NULL))
);

-- The pilot day's count of edit attempts since UTC midnight.
CREATE INDEX document_edit_attempts_started_idx ON campaign.document_edit_attempts (started_at);

-- Migration 0007 — document aggregates and their versions (1kg.5.1).
--
-- Storage for the GM Workbench's documents: the live content of one document,
-- and the history of sealed versions behind it. No routes and no wire models
-- read any of this yet — those are 1kg.5.2's. What is fixed here is what the
-- database itself will guarantee.
--
-- IDENTIFIERS (SEC-4). `documents.id` is a TEXT primary key minted by the
-- application: the `doc_` type prefix plus 128 CSPRNG bits rendered URL-safe.
-- The CHECK is generated from `service/campaign_identity.id_check_regex()`, and
-- `service/tests/test_campaign_schema_sql.py` asserts this file still agrees
-- with the registry.
--
-- THE TYPE IS NOT ENUMERATED (ED-24). `documents.type` carries a length bound
-- and no CHECK over a value list: adding a ninth document type must not need a
-- migration, and the wire contract's "adding a field or a kind is not a bump"
-- says the same. The closed set lives in `DocumentTypeId`
-- (`service/workbench_contracts.py`) and `service/document_store.py` refuses
-- anything else before the statement runs.
--
-- TWO COUNTERS, AND THEY ARE NOT THE SAME THING (CANVAS-34). `write_revision`
-- is the concurrency token: every committed write advances it by one, and a
-- per-field entry in `field_revisions` records which write last touched each
-- key (CANVAS-19), so a conflict is decided per field rather than per document.
-- `document_versions.number` is history, consecutive from 1, never reused and
-- never renumbered. A write revision is not a version number and neither can be
-- derived from the other.
--
-- SEALED MEANS IMMUTABLE (CANVAS-34). `sealed_at IS NULL` marks the ONE open
-- working version a GM's autosaves accumulate into, which is rewritten in
-- place; `document_versions_open_uidx` is what makes "at most one" the
-- database's guarantee rather than a store's intention. Once sealed, a version
-- never changes again: every UPDATE this schema's store issues against
-- `document_versions` carries `AND sealed_at IS NULL`, and the store contains
-- no DELETE against the table at all. The only removal is the cascade below.
--
-- NO campaign_id ON document_versions, DELIBERATELY. Ownership lives in the
-- query (`docs/migrations.md` section 4): every statement against this table
-- joins through `campaign.documents` and names the campaign in the SAME
-- statement, so a version of another campaign's document is indistinguishable
-- from one that does not exist (SEC-2). A denormalised `campaign_id` here would
-- be a second place for that fact to be wrong.
--
-- PRIVATE TEXT (SEC-20). `data`, `name_key`, `search_key` and a version's
-- `summary` are private text: they are stored here and nowhere else — never a
-- log line, never an exception message, never an audit row (0005 has no column
-- for any of them, and ED-26 forbids a digest, fold, excerpt, length or count of
-- field text anywhere that outlives the row).
--
-- `summary`'s CHECK BOUNDS ITS LENGTH AND NOTHING ELSE. 200 is
-- `TEXT_FIELD_MAX_CHARS`, the wire's `_TextValue` bound. That type ALSO applies
-- `_one_line`, which refuses U+000A, U+000D, U+2028 and U+2029; a CHECK on
-- length does not reproduce that and is not meant to. The line-break rule is
-- Python's, in `check_fields` and in 1kg.5.2's request model. A summary may be
-- empty: a burst of GM autosaves needs no summary.
--
-- name_key AND search_key ARE COMPUTED BY THE APPLICATION, for the same reason
-- `participants.alias_key` is: `lower()` answers according to the database's
-- collation provider while the in-memory twin answers with Python, and the two
-- disagree about a final sigma and a dotted capital I, silently.
-- `service/document_store._fold` is the one rule — controls to spaces, NFKC,
-- casefold, whitespace collapsed — and the two bounds here are pinned to
-- `NAME_KEY_MAX` and `SEARCH_KEY_MAX` by a test, exactly as `alias_key`'s is.
-- NFKC expands (one U+FDFA folds to eighteen characters), so a field's own
-- bound does not bound its key and the application checks each separately.
--
-- WHAT THIS SCHEMA DELIBERATELY DOES NOT HOLD:
--   * NOTHING ABOUT VISIBILITY (ED-6). No mask, no revealed flag, no class, no
--     audience, no disclosure, no pin, no slot. A reveal's pin is 1kg.7.1's
--     slot row and references (document_id, number) from here.
--   * NO current_version POINTER. The current version is MAX(number) for the
--     document. A denormalised pointer is a second source of truth that can
--     disagree with the rows it names.
--   * NO SOFT DELETE and no tombstone column. `archived_at` is the reversible
--     state (LIB-16); LIB-18's delete is permanent and takes the whole history
--     with it. The tombstone that survives a campaign deletion is the audit row
--     (`audit.events.campaign_id_tombstone`, ED-18(a)), which has no foreign key.
--   * NO ASSET TABLE and no foreign key to one. A portrait is an `asset` field
--     holding an `AssetRef` — an id, never a URL (X-10) — inside `data`. The
--     asset bytes are 1kg.8.1's.
--   * NO tsvector COLUMN AND NO GIN INDEX. LIB-21 keeps full-text search over
--     document bodies, and a single search across every category, out of v1;
--     search here is a case-sensitive `strpos` over the already-folded
--     `search_key`, bounded by the campaign-scoped partial indexes below.
--     Nothing here precludes either: adding a tsvector column with a GIN index
--     is a later pure expansion, as is raising SEARCH_KEY_MAX.
--
-- This file is not idempotent, and it is a pure addition: nothing in 0001-0006
-- is touched, so the previous release's code keeps working against it.

CREATE TABLE campaign.documents (
  id                    TEXT PRIMARY KEY CHECK (id ~ '^doc_[A-Za-z0-9_-]{22,60}$'),
  campaign_id           TEXT NOT NULL REFERENCES campaign.campaigns (id) ON DELETE CASCADE,
  -- Bounded, never enumerated: see ED-24 above.
  type                  TEXT NOT NULL CHECK (length(type) BETWEEN 1 AND 64),
  -- The bead's "JSON schema/type version is stored", read against
  -- DOC_TYPE_VERSION in service/workbench_contracts.py.
  type_version          INT NOT NULL CHECK (type_version BETWEEN 1 AND 1000),
  -- The LIVE content, flat, one value per declared field key of the type (ED-2).
  data                  JSONB NOT NULL,
  -- The wire's WriteRevision is ge=1, le=2**53-1 (WRITE_REVISION_MAX): a token a
  -- browser must be able to carry through JSON without losing precision.
  write_revision        BIGINT NOT NULL CHECK (write_revision BETWEEN 1 AND 9007199254740991),
  -- Per top-level field key, the write revision that last changed it (CANVAS-19).
  field_revisions       JSONB NOT NULL,
  name_key              TEXT NOT NULL CHECK (length(name_key) <= 4000),
  search_key            TEXT NOT NULL CHECK (length(search_key) <= 8000),
  -- AUD-15: a character sheet links to at most one participant, and AUD-16's
  -- Remove keeps the document. ON DELETE SET NULL rather than CASCADE (which
  -- would destroy the sheet) or RESTRICT (which could abort a legitimate
  -- campaign-deletion cascade): participants are MARKED removed and never
  -- deleted (RQ-3), so this action never fires in the application. It is here so
  -- that the cascade cannot fail and a hand-run DELETE keeps the document.
  linked_participant_id TEXT REFERENCES campaign.participants (id) ON DELETE SET NULL,
  -- Idempotent create: a retried request with the same command id returns the
  -- document already made rather than making a second one, and
  -- documents_command_uidx is what makes that race-safe. Nullable, because
  -- 1kg.5.5's AI edits and 1kg.5.4's tool creates mint without one.
  created_command_id    TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- LIB-16: archiving is reversible, and an archived document stays open under
  -- a Restore banner. It is not a delete and it is not read-only.
  archived_at           TIMESTAMPTZ
);

CREATE TABLE campaign.document_versions (
  document_id    TEXT NOT NULL REFERENCES campaign.documents (id) ON DELETE CASCADE,
  -- The wire's VersionNumber is ge=1, le=VERSION_NUMBER_MAX.
  number         INT NOT NULL CHECK (number BETWEEN 1 AND 1000000),
  -- AUD-1: one GM per campaign, and players cannot write. There is no third author.
  author         TEXT NOT NULL CHECK (author IN ('gm', 'assistant')),
  summary        TEXT NOT NULL CHECK (length(summary) <= 200),
  -- The keys this version changed against the one before it, sorted, at most
  -- MAX_CHANGED_FIELDS of them.
  changed_fields JSONB NOT NULL CHECK (jsonb_array_length(changed_fields) <= 64),
  -- CANVAS-26: a restore appends a new version whose content equals an earlier
  -- one. The wire model already refuses a restore from the future; so does this.
  restored_from  INT CHECK (restored_from IS NULL OR restored_from < number),
  -- The content as of this version. While a version is open it is kept equal to
  -- the document's `data`, so a reader needs no branch.
  data           JSONB NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL,
  updated_at     TIMESTAMPTZ NOT NULL,
  -- NULL marks the one open working version (CANVAS-34).
  sealed_at      TIMESTAMPTZ,
  -- History order, newest first, comes from this key; no separate index is needed.
  PRIMARY KEY (document_id, number)
);

-- WHY ALL THREE UNIQUE INDEXES BELOW ARE PARTIAL (RQ-3), for the same reason
-- 0004's `table_sessions_link_digest_uidx` is: PostgreSQL builds a table's key
-- columns from its NON-partial unique indexes, so a full unique index over a
-- column this store UPDATEs would escalate every such UPDATE from
-- FOR NO KEY UPDATE to FOR UPDATE and start conflicting with the FOR KEY SHARE
-- that a foreign-key check takes. `write_revision`, `field_revisions`, `data`,
-- `name_key`, `search_key`, `archived_at`, `updated_at` and
-- `linked_participant_id` are all updated, and none of them may ever gain a
-- non-partial unique index. `documents.id` and `document_versions.(document_id,
-- number)` are never updated, which is why their primary keys are free.
-- NON-unique indexes are not affected by this rule at all, which is why the four
-- library indexes over `updated_at` and `name_key` cost nothing.

-- CANVAS-34: at most one open version per document, and the database is what
-- holds it — two autosaves racing cannot both open one.
CREATE UNIQUE INDEX document_versions_open_uidx
  ON campaign.document_versions (document_id) WHERE sealed_at IS NULL;

-- Idempotent create, per campaign. Two requests carrying the same command id
-- leave exactly one document however they raced.
CREATE UNIQUE INDEX documents_command_uidx
  ON campaign.documents (campaign_id, created_command_id) WHERE created_command_id IS NOT NULL;

-- One sheet per participant (AUD-13's "optionally a linked character sheet",
-- singular). Relaxing this later is dropping an index; tightening it later would
-- need data that already violates it to be reconciled.
CREATE UNIQUE INDEX documents_participant_uidx
  ON campaign.documents (linked_participant_id) WHERE linked_participant_id IS NOT NULL;

-- THE FOUR LIBRARY INDEXES (LIB-20 to LIB-23). A category is a SET of types
-- (LIB-3), so the query is `WHERE campaign_id = %s AND type = ANY(%s) AND
-- archived_at IS NULL ORDER BY updated_at DESC, id COLLATE "C" LIMIT %s`. `type`
-- is therefore a FILTER and not a key column: with it as the second key column
-- ahead of the sort columns the = ANY becomes a ScalarArrayOp and the planner
-- needs a BitmapOr plus a sort, or an incremental sort at best, while as a
-- filter one ordered index scan answers the page and stops at LIMIT. The partial
-- predicate matches `LibraryQuery.archived`, which is required on every query,
-- so the default list (Active, Recent) never walks the archived rows.
--
-- `id` and `name_key` are collated "C" so that the ordering the index produces
-- is the code-point ordering the in-memory twin produces in Python — the same
-- reason 0004's alias comparison is a key the application computes, and the same
-- reason `list_for_owner` orders by `id COLLATE "C"`.
CREATE INDEX documents_active_recent_idx
  ON campaign.documents (campaign_id, updated_at DESC, id COLLATE "C") WHERE archived_at IS NULL;
CREATE INDEX documents_archived_recent_idx
  ON campaign.documents (campaign_id, updated_at DESC, id COLLATE "C") WHERE archived_at IS NOT NULL;
CREATE INDEX documents_active_name_idx
  ON campaign.documents (campaign_id, name_key COLLATE "C", id COLLATE "C") WHERE archived_at IS NULL;
CREATE INDEX documents_archived_name_idx
  ON campaign.documents (campaign_id, name_key COLLATE "C", id COLLATE "C") WHERE archived_at IS NOT NULL;

-- media assets and the per-campaign media quota (agent-forge-harness-1kg.8.1.1,
-- slice a of 1kg.8.1). This file's number is written nowhere else in it: the lead
-- renumbers a migration at merge when a parallel bead takes the number first.
--
-- PURE EXPANSION. Two new tables in the campaign schema, one trigger with its
-- function, and one backfill. NO CHECK IS ADDED TO AN EXISTING TABLE: a CHECK
-- added by ALTER TABLE validates every existing row, and every CHECK here is on a
-- table this file creates, so nothing that exists is read for validation. The
-- release running now neither reads nor writes either table.
--
-- WHAT A ROW IS. `campaign.assets` holds one GM-side asset: what was declared
-- (kind, media type, size, alt text), what the server measured once the bytes
-- were processed, a state, and two object KEYS. It never holds bytes — no column
-- is BYTEA — and never a filename. The five states are the media ADR's (MS-3):
-- uploading -> processing -> ready or failed, and any of those -> deleted, where
-- `deleted` is a storage-only tombstone the wire contract never carries. The
-- CHECKs below make every row a GM can read convert to the wire `Asset` with no
-- special case: a failure exists exactly on a failed row; the measured type and
-- size exactly on a ready row; dimensions exactly on a ready image, and a
-- duration exactly on a ready audio row. A tombstone carries no alt text and no
-- measured value, so deleting an asset purges its private text at once, by the
-- database's own rule. Every bound and every set is the wire contract's
-- (service/workbench_contracts.py); service/tests/test_campaign_schema_sql.py
-- holds this text to those constants.
--
-- WHY THE KEYS ARE IMMUTABLE. `object_key` and `tmp_key` are minted from 16
-- CSPRNG bytes each, independently of the asset and of each other, at INSERT, and
-- nothing ever updates them. Each is UNIQUE, and PostgreSQL counts a column of a
-- non-partial unique index as a key column: an UPDATE that touched one would take
-- FOR UPDATE on the row rather than FOR NO KEY UPDATE (RQ-3). A key is never
-- reused, so an object no live row names today will never be named again, which
-- is what lets the orphan reconciliation delete without a lock.
--
-- WHY `campaign_id` IS ON DELETE NO ACTION, WRITTEN OUT. Bytes leave the bucket
-- only through the outbox: the job that deletes them is enqueued in the
-- transaction that removes the row, carrying the keys. A CASCADE from campaigns
-- would remove the rows and leave their bytes in the bucket for ever — no row
-- would name them, and nothing lists `assets/` to find them — breaking SEC-29,
-- SEC-36 and MS-12's "deleted means deleted". So PostgreSQL REFUSES to delete a
-- campaign that still has any asset row, in any state, tombstones included:
-- a direct DELETE of the campaign, and the account cascade that deletes a GM's
-- campaigns (0004's `owner_id ... ON DELETE CASCADE`, identity ADR IDT-14) alike.
-- The one path that works is service/asset_store.py's campaign primitive, which
-- deletes the rows and enqueues their byte deletions first, in the same
-- transaction (1kg.2.6, zkc). This is fma's precedent for seats (0009).
--
-- WHY `media_usage` IS A TABLE OF ITS OWN, NOT COLUMNS ON `campaigns`. The quota
-- is reserved and released by conditional UPDATEs of a counter (SEC-31). Every
-- insert that references a campaign takes FOR KEY SHARE on the campaign row for
-- its foreign-key check; counters on that row would make every reservation
-- contend with every such insert, and the campaign row with every upload. A row
-- of its own is locked by the quota's writers and by nobody else. It cascades
-- with its campaign: it holds counts, never content.
--
-- WHY A TRIGGER AND A BACKFILL. The quota's UPDATE assumes the row exists: a
-- missing row would read as "over quota" for ever. So, as 0004 does for
-- `authz_state`, an AFTER INSERT trigger on campaigns writes it, and no route,
-- script, fixture or later epic can make a campaign without one; and the same
-- file backfills a zero row for every campaign that already exists.

CREATE TABLE campaign.assets (
  id                  TEXT PRIMARY KEY CHECK (id ~ '^ast_[A-Za-z0-9_-]{22,60}$'),
  campaign_id         TEXT NOT NULL REFERENCES campaign.campaigns (id) ON DELETE NO ACTION,
  kind                TEXT NOT NULL CHECK (kind IN ('image', 'audio')),
  state               TEXT NOT NULL CHECK (state IN ('uploading', 'processing', 'ready', 'failed', 'deleted')),
  failure             TEXT CHECK (failure IN ('unsupported_type', 'too_large', 'too_many_pixels', 'too_long', 'unreadable', 'timed_out', 'quota_exceeded')),
  declared_media_type TEXT NOT NULL,
  declared_size_bytes BIGINT NOT NULL CHECK (declared_size_bytes >= 1),
  media_type          TEXT,
  size_bytes          BIGINT CHECK (size_bytes >= 1),
  width               INTEGER CHECK (width BETWEEN 1 AND 8192),
  height              INTEGER CHECK (height BETWEEN 1 AND 8192),
  duration_ms         INTEGER CHECK (duration_ms BETWEEN 1 AND 600000),
  alt                 TEXT CHECK (length(alt) BETWEEN 1 AND 300),
  object_key          TEXT NOT NULL UNIQUE CHECK (object_key ~ '^assets/[0-9a-f]{32}$'),
  tmp_key             TEXT NOT NULL UNIQUE CHECK (tmp_key ~ '^tmp/[0-9a-f]{32}$'),
  created_command_id  TEXT CHECK (created_command_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  state_changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- What was declared (SEC-26): a type the kind accepts, and a size under its cap.
  CONSTRAINT assets_declared_type_chk CHECK (
    (kind = 'image' AND declared_media_type IN ('image/png', 'image/jpeg', 'image/webp'))
    OR (kind = 'audio' AND declared_media_type IN ('audio/mpeg', 'audio/mp4', 'audio/ogg', 'audio/wav'))),
  CONSTRAINT assets_declared_size_chk CHECK (
    declared_size_bytes <= CASE kind WHEN 'image' THEN 10000000 WHEN 'audio' THEN 20000000 END),
  -- A failure exists exactly on a failed row.
  CONSTRAINT assets_failure_chk CHECK ((failure IS NOT NULL) = (state = 'failed')),
  -- What was measured exists exactly on a ready row, as the wire `Asset` says.
  CONSTRAINT assets_measured_chk CHECK (
    (media_type IS NOT NULL) = (state = 'ready')
    AND (size_bytes IS NOT NULL) = (state = 'ready')
    AND (width IS NOT NULL) = (kind = 'image' AND state = 'ready')
    AND (height IS NOT NULL) = (kind = 'image' AND state = 'ready')
    AND (duration_ms IS NOT NULL) = (kind = 'audio' AND state = 'ready')),
  CONSTRAINT assets_ready_size_chk CHECK (
    size_bytes <= CASE kind WHEN 'image' THEN 10000000 WHEN 'audio' THEN 20000000 END),
  CONSTRAINT assets_pixels_chk CHECK (width::bigint * height <= 25000000),
  -- An image is re-encoded within its family (SEC-25, MS-6); audio becomes MP3.
  CONSTRAINT assets_ready_type_chk CHECK (
    state <> 'ready'
    OR (kind = 'image' AND media_type = declared_media_type)
    OR (kind = 'audio' AND media_type = 'audio/mpeg')),
  -- An image has alt text and an audio row has none; a tombstone has none at all.
  CONSTRAINT assets_alt_chk CHECK (
    CASE WHEN state = 'deleted' THEN alt IS NULL ELSE (alt IS NOT NULL) = (kind = 'image') END)
);

-- The GM's list, by campaign and state. Nothing scans rows by state alone: each
-- asset seeds its own deadline sweep, so an index for a sweep would only cost writes.
CREATE INDEX assets_campaign_state_idx ON campaign.assets (campaign_id, state);

-- Idempotent create, per campaign: a retried create with the same command id
-- opens the asset already made (the wire contract's Idempotency row). Partial,
-- as 0008's documents_command_uidx is, because the column is nullable.
CREATE UNIQUE INDEX assets_command_uidx
  ON campaign.assets (campaign_id, created_command_id) WHERE created_command_id IS NOT NULL;

-- bytes_reserved is the sum of each live row's reservation (the declared size
-- while uploading or processing, the real size once ready, nothing once failed
-- or deleted); asset_count is how many rows are uploading, processing or ready.
CREATE TABLE campaign.media_usage (
  campaign_id    TEXT PRIMARY KEY REFERENCES campaign.campaigns (id) ON DELETE CASCADE,
  bytes_reserved BIGINT NOT NULL DEFAULT 0 CHECK (bytes_reserved >= 0),
  asset_count    INTEGER NOT NULL DEFAULT 0 CHECK (asset_count >= 0)
);

CREATE FUNCTION campaign.create_media_usage() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO campaign.media_usage (campaign_id) VALUES (NEW.id);
  RETURN NULL;
END $$;
CREATE TRIGGER campaigns_media_usage_ai AFTER INSERT ON campaign.campaigns
  FOR EACH ROW EXECUTE FUNCTION campaign.create_media_usage();

-- The backfill: every campaign that existed before this file, at zero.
INSERT INTO campaign.media_usage (campaign_id) SELECT id FROM campaign.campaigns;

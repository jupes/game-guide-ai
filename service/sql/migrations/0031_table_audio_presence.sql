-- A delayed open must not restore presence after Disable/Enable or Archive/Restore.
ALTER TABLE campaign.table_sessions ADD COLUMN audio_presence_generation BIGINT NOT NULL DEFAULT 0;

-- One small, bounded pool for presence across SSE and polling. These are
-- page registrations, not connections: a reconnect doesn't buy another seat.
CREATE TABLE campaign.audio_presence (
  permit SMALLINT PRIMARY KEY CHECK (permit BETWEEN 1 AND 16),
  registration TEXT NOT NULL UNIQUE CHECK (registration ~ '^[A-Za-z0-9_-]{32}$'),
  page_id UUID NOT NULL,
  session_id TEXT NOT NULL REFERENCES campaign.table_sessions(id) ON DELETE CASCADE,
  generation INTEGER NOT NULL,
  account_id BIGINT REFERENCES auth.users(id) ON DELETE CASCADE,
  participant_id TEXT REFERENCES campaign.participants(id) ON DELETE CASCADE,
  grant_id TEXT REFERENCES campaign.table_credentials(id) ON DELETE CASCADE,
  revision BIGINT NOT NULL DEFAULT 0 CHECK (revision BETWEEN 0 AND 9007199254740991),
  state TEXT CHECK (state IN ('pending', 'idle', 'listening', 'muted')),
  playing_handles TEXT[] NOT NULL DEFAULT '{}' CHECK (cardinality(playing_handles) <= 2),
  last_seen_at TIMESTAMPTZ NOT NULL,
  CHECK ((account_id IS NULL) <> (grant_id IS NULL)),
  CHECK (participant_id IS NULL OR account_id IS NOT NULL),
  CHECK ((revision = 0) = (state IS NULL)),
  CHECK ((cardinality(playing_handles) > 0) = (state = 'listening')),
  UNIQUE NULLS NOT DISTINCT (session_id, generation, account_id, participant_id, grant_id, page_id)
);

CREATE FUNCTION campaign.retire_table_presence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.table_audio <> OLD.table_audio THEN
    NEW.audio_presence_generation := OLD.audio_presence_generation + 1;
  END IF;
  DELETE FROM campaign.audio_presence WHERE session_id = NEW.id;
  RETURN NEW;
END
$$;
CREATE TRIGGER table_sessions_retire_audio_presence BEFORE UPDATE OF state, link_generation, table_audio
  ON campaign.table_sessions FOR EACH ROW
  WHEN (NEW.state <> OLD.state OR NEW.link_generation <> OLD.link_generation OR NEW.table_audio <> OLD.table_audio)
  EXECUTE FUNCTION campaign.retire_table_presence();

CREATE FUNCTION campaign.retire_archived_presence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE campaign.table_sessions SET audio_presence_generation = audio_presence_generation + 1
    WHERE campaign_id = NEW.id AND state = 'live';
  DELETE FROM campaign.audio_presence r USING campaign.table_sessions s
    WHERE r.session_id = s.id AND s.campaign_id = NEW.id;
  RETURN NEW;
END
$$;
CREATE TRIGGER campaigns_retire_audio_presence AFTER UPDATE OF archived_at ON campaign.campaigns
  FOR EACH ROW WHEN (OLD.archived_at IS NULL AND NEW.archived_at IS NOT NULL)
  EXECUTE FUNCTION campaign.retire_archived_presence();

-- Expand the allowed command operations in place. The original check remains
-- the play/stop branch; existing rows are valid without a historical scan.
ALTER TABLE campaign.audio_commands DROP CONSTRAINT audio_commands_operation_check, DROP CONSTRAINT audio_commands_check,
  ADD CONSTRAINT audio_commands_payload_check CHECK (
       (operation = 'play' AND num_nonnulls(cue_id, audio_epoch, loop) = 3)
    OR (operation = 'stop' AND audio_epoch IS NULL AND loop IS NULL)
    OR (operation = 'enable' AND cue_id IS NULL AND audio_epoch IS NOT NULL AND loop IS NULL)
    OR (operation = 'disable' AND cue_id IS NULL AND audio_epoch IS NULL AND loop IS NULL)
  ) NOT VALID;

-- At most two independently sequenced slots per session. Empty rows keep their
-- sequence so a delayed frame cannot revive an earlier sound.
CREATE TABLE campaign.audio_slots (
  session_id  TEXT NOT NULL REFERENCES campaign.table_sessions (id) ON DELETE CASCADE,
  slot        TEXT NOT NULL CHECK (slot IN ('ambience', 'one_shot')),
  seq         BIGINT NOT NULL CHECK (seq > 0),
  cue_id      TEXT REFERENCES campaign.audio_cues (id) ON DELETE CASCADE,
  handle      TEXT UNIQUE CHECK (handle ~ '^[A-Za-z0-9_-]{32}$'),
  started_at  TIMESTAMPTZ,
  loop        BOOLEAN,
  duration_ms INTEGER CHECK (duration_ms BETWEEN 1 AND 600000),
  PRIMARY KEY (session_id, slot),
  CHECK (num_nonnulls(cue_id, handle, started_at, loop, duration_ms) IN (0, 5)),
  CHECK (slot <> 'one_shot' OR (NOT loop AND duration_ms <= 30000))
);

-- Command receipts outlive slot replacement and are removed with the session.
CREATE TABLE campaign.audio_commands (
  session_id TEXT NOT NULL REFERENCES campaign.table_sessions (id) ON DELETE CASCADE,
  command_id TEXT NOT NULL CHECK (command_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  operation TEXT NOT NULL CHECK (operation IN ('play', 'stop')),
  cue_id TEXT,
  audio_epoch BIGINT,
  loop BOOLEAN,
  PRIMARY KEY (session_id, command_id),
  CHECK ((operation = 'play' AND num_nonnulls(cue_id, audio_epoch, loop) = 3)
      OR (operation = 'stop' AND audio_epoch IS NULL AND loop IS NULL))
);

-- Session lifecycle operations already hold this session's lock and notify
-- its listeners. This also clears playback during mixed-version deployments.
CREATE FUNCTION campaign.clear_session_audio() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE campaign.audio_slots SET seq = seq + 1, cue_id = NULL, handle = NULL,
      started_at = NULL, loop = NULL, duration_ms = NULL
    WHERE session_id = NEW.id AND cue_id IS NOT NULL;
  RETURN NEW;
END
$$;
CREATE TRIGGER table_sessions_clear_audio AFTER UPDATE OF state, link_generation, table_audio
  ON campaign.table_sessions FOR EACH ROW
  WHEN (NEW.state <> OLD.state OR NEW.link_generation <> OLD.link_generation
        OR (OLD.table_audio AND NOT NEW.table_audio))
  EXECUTE FUNCTION campaign.clear_session_audio();

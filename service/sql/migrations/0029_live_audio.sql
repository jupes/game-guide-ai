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
CREATE INDEX audio_slots_current_cue_idx ON campaign.audio_slots (cue_id) WHERE cue_id IS NOT NULL;

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

-- Preserve the slot sequence when sound is retired, including by older
-- application instances which do not know that audio slots exist.
CREATE FUNCTION campaign.clear_audio_slots(target_session TEXT, target_cue TEXT)
  RETURNS void LANGUAGE sql AS $$
  UPDATE campaign.audio_slots SET seq = seq + 1, cue_id = NULL, handle = NULL,
      started_at = NULL, loop = NULL, duration_ms = NULL
    WHERE session_id = target_session AND cue_id IS NOT NULL AND (target_cue IS NULL OR cue_id = target_cue);
$$;

CREATE FUNCTION campaign.clear_session_audio() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM campaign.clear_audio_slots(NEW.id, NULL);
  IF OLD.table_audio AND NOT NEW.table_audio AND NEW.audio_epoch = OLD.audio_epoch THEN
    NEW.audio_epoch := OLD.audio_epoch + 1;
  END IF;
  PERFORM pg_notify('table_session', NEW.id);
  RETURN NEW;
END
$$;
CREATE TRIGGER table_sessions_clear_audio BEFORE UPDATE OF state, link_generation, table_audio
  ON campaign.table_sessions FOR EACH ROW
  WHEN (NEW.state <> OLD.state OR NEW.link_generation <> OLD.link_generation
        OR (OLD.table_audio AND NOT NEW.table_audio))
  EXECUTE FUNCTION campaign.clear_session_audio();

-- A legacy cue Archive has no confirmation. Play takes FOR SHARE on the cue,
-- ordering it against this UPDATE, including when neither slot existed yet.
-- A confirmed Archive has already stopped under the session lock.
CREATE FUNCTION campaign.guard_audio_cue_archive() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  target_session TEXT;
BEGIN
  IF EXISTS (
    SELECT 1 FROM campaign.audio_slots sl JOIN campaign.table_sessions s ON s.id = sl.session_id
    WHERE sl.cue_id = NEW.id AND s.state = 'live' AND s.table_audio AND s.expires_at > clock_timestamp()
      AND (sl.loop OR sl.started_at + sl.duration_ms * interval '1 millisecond' > clock_timestamp())
  ) THEN
    RAISE EXCEPTION 'A playing cue must be stopped before archiving' USING ERRCODE = 'check_violation';
  END IF;
  FOR target_session IN SELECT session_id FROM campaign.audio_slots WHERE cue_id = NEW.id LOOP
    PERFORM campaign.clear_audio_slots(target_session, NEW.id);
    PERFORM pg_notify('table_session', target_session);
  END LOOP;
  RETURN NEW;
END
$$;
CREATE TRIGGER audio_cues_guard_archive BEFORE UPDATE OF archived ON campaign.audio_cues FOR EACH ROW
  WHEN (NOT OLD.archived AND NEW.archived) EXECUTE FUNCTION campaign.guard_audio_cue_archive();

-- The previous campaign API narrows only reveals in both archive passes.
-- Fence even an empty live session before the archive commits, so restoring a
-- campaign cannot admit a delayed pre-archive push.
CREATE FUNCTION campaign.stop_archived_campaign_audio() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  target_session TEXT;
BEGIN
  SELECT id INTO target_session FROM campaign.table_sessions
    WHERE campaign_id = NEW.id AND state = 'live' FOR NO KEY UPDATE;
  IF FOUND THEN
    UPDATE campaign.table_sessions SET audio_epoch = audio_epoch + 1 WHERE id = target_session;
    PERFORM campaign.clear_audio_slots(target_session, NULL);
    PERFORM pg_notify('table_session', target_session);
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER campaigns_stop_archived_audio AFTER UPDATE OF archived_at ON campaign.campaigns FOR EACH ROW
  WHEN (OLD.archived_at IS NULL AND NEW.archived_at IS NOT NULL)
  EXECUTE FUNCTION campaign.stop_archived_campaign_audio();

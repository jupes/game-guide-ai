-- Private cue metadata. The asset retains ownership of the media bytes and
-- waveform. A physical asset purge removes the corresponding cue.
CREATE TABLE campaign.audio_cues (
  id             TEXT PRIMARY KEY CHECK (id ~ '^cue_[A-Za-z0-9_-]{22,60}$'),
  campaign_id    TEXT NOT NULL REFERENCES campaign.campaigns (id) ON DELETE CASCADE,
  asset_id       TEXT NOT NULL UNIQUE REFERENCES campaign.assets (id) ON DELETE CASCADE,
  created_by     BIGINT NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  command_id     TEXT NOT NULL CHECK (command_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  title          TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  kind           TEXT NOT NULL CHECK (kind IN ('ambience', 'one_shot')),
  archived       BOOLEAN NOT NULL DEFAULT false,
  created_at     TIMESTAMPTZ NOT NULL,
  updated_at     TIMESTAMPTZ NOT NULL,
  UNIQUE (campaign_id, command_id)
);

CREATE INDEX audio_cues_library_idx ON campaign.audio_cues (campaign_id, archived);

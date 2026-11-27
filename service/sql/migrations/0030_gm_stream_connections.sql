-- Four GM permits remain available even with all sixteen table permits held.
-- A campaign can be watched before it starts a session.
CREATE TABLE campaign.gm_stream_connections (
  permit SMALLINT PRIMARY KEY CHECK (permit BETWEEN 1 AND 4),
  lease_id UUID NOT NULL UNIQUE,
  instance_id UUID NOT NULL,
  campaign_id TEXT NOT NULL,
  account_id BIGINT NOT NULL,
  opened_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL CHECK (last_seen_at >= opened_at),
  FOREIGN KEY (campaign_id, account_id) REFERENCES campaign.campaigns (id, owner_id) ON DELETE CASCADE
);

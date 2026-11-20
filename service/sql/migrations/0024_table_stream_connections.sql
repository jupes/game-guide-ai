-- Table stream admission (1kg.7.5). Add the table's 16 global permits.
-- Four places in the realtime budget are reserved for the future GM channel.
-- Reusing a stale permit changes its lease_id so a disconnected worker cannot
-- renew or release a replacement. There are at most 16 rows, including stale
-- ones. An older application can run with this table present.

CREATE TABLE campaign.table_stream_connections (
  permit       SMALLINT PRIMARY KEY CHECK (permit BETWEEN 1 AND 16),
  lease_id     UUID NOT NULL UNIQUE,
  instance_id  UUID NOT NULL,
  session_id   TEXT NOT NULL REFERENCES campaign.table_sessions (id) ON DELETE CASCADE,
  account_id   BIGINT REFERENCES auth.users (id) ON DELETE CASCADE,
  grant_id     TEXT REFERENCES campaign.table_credentials (id) ON DELETE CASCADE,
  opened_at    TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL,
  CHECK ((account_id IS NULL) <> (grant_id IS NULL)),
  CHECK (last_seen_at >= opened_at)
);

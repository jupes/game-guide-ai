-- Migration: the GM's confirmation of a seat, and offers by address
-- (agent-forge-harness-1kg.2.2).
--
-- Owner decision D-12 and threat model SEC-50: the GM offers a seat to an
-- ADDRESS, never to an account; only a Verified holder of that address sees the
-- offer and may accept it, and the GM then CONFIRMS who accepted before the seat
-- gets anything beyond the table slot. An offer therefore binds to an account
-- only inside the transaction that accepts it (service/participant_store.py), and
-- this file adds what that needs. It is expand-only: 0001 to 0011 are released
-- and are not edited.
--
-- 1. participants.confirmed_at, with the CHECK that only an accepted seat can be
--    confirmed. A NULL reads as NOT confirmed, so a seat the previous build
--    accepted during a rollout fails closed.
--
--    THE CHECK IS SAFE TO ADD HERE, AND IT IS THE ONLY CHECK THIS FILE ADDS TO
--    AN EXISTING TABLE. A CHECK added by ALTER TABLE validates every existing
--    row. The column it constrains is new in this file, so every existing row
--    has confirmed_at NULL and satisfies it; and the previous build never writes
--    confirmed_at (it does not know the column exists), so a row it writes
--    during a rollout satisfies it too.
--
--    The UNIQUE (id, campaign_id) is redundant with the primary key and exists
--    only so that seat_offers can carry a composite foreign key to it, as 0004's
--    UNIQUE (id, owner_id) on campaigns does for table_sessions: the database
--    then refuses an offer that names another campaign's seat. Nothing updates
--    a participant's id or campaign_id, so no UPDATE becomes a key update and
--    RQ-3's FOR NO KEY UPDATE on a participant row is unaffected.
--
-- 2. campaign.seat_offers: one row per offer, naming a seat and an address. The
--    address is personal data about a third party (SEC-20): it is served only to
--    the campaign's owner and never reaches a log line, an audit row or a job.
--    address_key is the address with ASCII A-Z folded to a-z and nothing else,
--    computed by the application (service/seat_offer_store.py) so that the two
--    worlds cannot disagree about folding; the CHECK keeps an unfolded key out.
--    offered_by carries a composite foreign key to the campaign's (id, owner_id),
--    so the database refuses an offer whose offerer is not the campaign's owner.
--
--    NO PREDICATE READS THE CLOCK. An offer's expiry is judged when it is read
--    and marked lazily under its seat's row lock. There is deliberately NO
--    unique index on (campaign_id, address_key): a stale expired offer on
--    another seat would block it, and marking another seat's offer would take a
--    lock a Remove may wait behind. The exclusive campaign lock, which every
--    writer that opens an offer holds, keeps one live offer per address.
--
-- 3. campaign.seat_blocks: an account that declined an offer and asked never to
--    be offered a seat by that owner again. Deleting either account removes the
--    row.
--
-- Not CONCURRENTLY: the tables are new or have never held a production row, and
-- the runner has no no-transaction mode (docs/migrations.md section 2).

ALTER TABLE campaign.participants
  ADD COLUMN confirmed_at TIMESTAMPTZ,
  ADD CONSTRAINT participants_confirmed_is_accepted_chk
    CHECK (confirmed_at IS NULL OR accepted_at IS NOT NULL),
  ADD CONSTRAINT participants_id_campaign_key UNIQUE (id, campaign_id);

CREATE TABLE campaign.seat_offers (
  id             TEXT PRIMARY KEY CHECK (id ~ '^sof_[A-Za-z0-9_-]{22,60}$'),
  campaign_id    TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  offered_by     BIGINT NOT NULL,
  address        TEXT NOT NULL CHECK (length(address) BETWEEN 3 AND 254),
  address_key    TEXT NOT NULL CHECK (length(address_key) BETWEEN 3 AND 254 AND address_key !~ '[A-Z]'),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at     TIMESTAMPTZ NOT NULL,
  outcome        TEXT CHECK (outcome IN ('accepted', 'declined', 'expired', 'withdrawn')),
  answered_at    TIMESTAMPTZ,
  CHECK (expires_at > created_at),
  CHECK ((outcome IS NULL) = (answered_at IS NULL)),
  FOREIGN KEY (participant_id, campaign_id)
    REFERENCES campaign.participants (id, campaign_id) ON DELETE CASCADE,
  FOREIGN KEY (campaign_id, offered_by)
    REFERENCES campaign.campaigns (id, owner_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX seat_offers_one_open_per_seat_uidx
  ON campaign.seat_offers (participant_id) WHERE outcome IS NULL;

CREATE INDEX seat_offers_seat_latest_idx
  ON campaign.seat_offers (participant_id, created_at);

CREATE INDEX seat_offers_open_by_address_idx
  ON campaign.seat_offers (address_key) WHERE outcome IS NULL;

CREATE INDEX seat_offers_campaign_address_idx
  ON campaign.seat_offers (campaign_id, address_key);

CREATE INDEX seat_offers_throttle_idx
  ON campaign.seat_offers (offered_by, created_at);

CREATE TABLE campaign.seat_blocks (
  blocker_user_id  BIGINT NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  blocked_owner_id BIGINT NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (blocker_user_id, blocked_owner_id),
  CHECK (blocker_user_id <> blocked_owner_id)
);

ALTER TABLE campaign.participants DROP CONSTRAINT IF EXISTS participants_confirmed_is_accepted_chk;
ALTER TABLE campaign.seat_offers DROP CONSTRAINT IF EXISTS seat_offers_participant_id_campaign_id_fkey;
ALTER TABLE campaign.seat_offers DROP CONSTRAINT IF EXISTS seat_offers_campaign_id_offered_by_fkey;
ALTER TABLE campaign.seat_offers DROP CONSTRAINT IF EXISTS seat_offers_id_check;
ALTER TABLE campaign.seat_offers DROP CONSTRAINT IF EXISTS seat_offers_address_check;
ALTER TABLE campaign.seat_offers DROP CONSTRAINT IF EXISTS seat_offers_address_key_check;
ALTER TABLE campaign.seat_offers DROP CONSTRAINT IF EXISTS seat_offers_outcome_check;
DROP INDEX campaign.seat_offers_one_open_per_seat_uidx;
ALTER TABLE campaign.seat_blocks DROP CONSTRAINT IF EXISTS seat_blocks_blocker_user_id_fkey;
ALTER TABLE campaign.seat_blocks DROP CONSTRAINT IF EXISTS seat_blocks_check;

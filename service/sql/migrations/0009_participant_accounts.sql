-- Migration 0009 — a participant is an account's seat at a campaign
-- (agent-forge-harness-fma).
--
-- The owner's decisions D-1 and D-4 of 2026-09-21 (interactions ADR A-23,
-- threat model TA-2): every player holds an account and there are no guests.
-- A participant is therefore an ACCOUNT SEATED AT A CAMPAIGN, and the
-- single-use enrolment code and the device credential that bound a seat to a
-- browser are retired. This file adds the account to the seat and drops the two
-- dead tables. 0004 is released and is not edited.
--
-- SEAT STATES. open (user_id NULL: the GM seated an alias while preparing),
-- offered (user_id set, accepted_at NULL), accepted (both set), removed
-- (removed_at set; a participant is still MARKED removed and never deleted,
-- RQ-3). Only an offer followed by that same account's acceptance moves a seat
-- between them (service/participant_store.py).
--
-- THE FOREIGN KEY IS ON DELETE NO ACTION, written out rather than left to the
-- default. CASCADE would delete a seat that disclosures, the character-sheet
-- link (0008) and audit rows reference, and break "removal marks, never
-- deletes"; SET NULL would re-open an accepted seat to whoever next accepts it.
-- So deleting an account that holds a seat, removed or not, is refused until
-- the account-deletion work (agent-forge-harness-zkc) handles seats. Deleting a
-- GM's account still cascades through their campaigns and those campaigns'
-- seats (0004); a GM is never seated in a campaign of their own.
--
-- THE CHECK IS SAFE TO ADD HERE, AND IT IS THE ONLY ONE. A CHECK added by
-- ALTER TABLE validates every existing row. Both columns it reads are new in
-- this file, so every existing row has both NULL and satisfies it; and the
-- previous build writes neither column (it does not know they exist), so a row
-- it writes during a rollout satisfies it too.
--
-- TWO PARTIAL INDEXES. An account holds at most one live seat per campaign, so
-- a second offer to an account already seated there is refused by the index
-- however it was composed; and the account's own list of its live seats. Neither
-- predicate reads the clock. Not CONCURRENTLY: the table has never held a
-- production row, and the runner has no no-transaction mode
-- (docs/migrations.md section 2).
--
-- THE DROPS ARE A CONTRACTION, AND THIS IS WHY ONE IS SAFE NOW.
-- docs/migrations.md section 3 ships a drop only once no rollback to a build
-- that needs the old shape is intended. No build that reads
-- campaign.enrolment_codes or campaign.device_credentials has ever been
-- deployed: master's production build has no campaign schema (it carries no
-- migration runner), and the integration branch deploys nowhere. There is
-- therefore no such rollback to protect.

ALTER TABLE campaign.participants
  ADD COLUMN user_id BIGINT REFERENCES auth.users (id) ON DELETE CASCADE,
  ADD COLUMN accepted_at TIMESTAMPTZ;

CREATE UNIQUE INDEX participants_one_live_seat_per_account_uidx
  ON campaign.participants (campaign_id, user_id)
  WHERE removed_at IS NULL AND user_id IS NOT NULL;

CREATE INDEX participants_account_idx
  ON campaign.participants (user_id) WHERE removed_at IS NULL;

DROP TABLE campaign.enrolment_codes;
DROP TABLE campaign.device_credentials;

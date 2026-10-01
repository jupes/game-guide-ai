-- metering: the pilot day's chat count, read from the ledger rather than
-- chat.messages (agent-forge-harness-u2uj). This file's number is written
-- nowhere else in it: the lead renumbers a migration at merge when a parallel
-- bead takes the number first.
--
-- PURE EXPANSION. One index, on an existing table, nothing else. The build
-- takes a SHARE lock on metering.provider_attempts, which is pilot-sized, so
-- it completes quickly; the runner's lock_timeout bounds the wait regardless.
-- Ledger writes made while the build runs are best-effort already
-- (docs/runbooks/usage-capture.md) and simply land after the index exists.

CREATE INDEX provider_attempts_operation_time_idx
  ON metering.provider_attempts (operation, occurred_at);

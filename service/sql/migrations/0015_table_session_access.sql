-- Migration 0015 — a live table session with no link and no join
-- (agent-forge-harness-1kg.2.3).
--
-- Threat model section 15 (TA-5) retired the table link and the join: a player
-- is an account, and the only bearer secret left in the table model is the
-- SCREEN GRANT, a row of campaign.table_credentials minted by the owner on the
-- browser they are signed in on (SEC-48, D-13). That table "fits as it is", and
-- `link_generation` stays under its name: it IS the admission generation
-- (SEC-42, renamed in prose only). 0004 is released and is not edited.
--
-- WHAT THIS FILE DOES, EXACTLY.
--   (a) drops campaign.table_sessions.link_digest, and its partial unique index
--       with it;
--   (b) drops campaign.session_join_counters, which nothing ever wrote;
--   (c) adds start_command_id and rotate_command_id to campaign.table_sessions,
--       each NULL or the wire contract's CommandId shape, so that a retried
--       Start or Rotate is answered by the command it repeats (the contract's
--       Idempotency row, "Start, End, Rotate a session");
--   (d) one PARTIAL unique index over (campaign_id, start_command_id), and NO
--       index on rotate_command_id, which is only ever read from the session row
--       its Rotate already holds;
--   (e) replaces audit.events' actor-kind CHECK: 'guest' goes, 'screen' comes.
--
-- WHY THE INDEX IS PARTIAL AND ROTATE'S COLUMN HAS NONE (RQ-3). PostgreSQL
-- treats the columns of a non-partial unique index as key columns, and an
-- UPDATE of a key column takes FOR UPDATE on the row, which conflicts with the
-- FOR KEY SHARE every screen-grant insert's foreign-key check takes. The start
-- index is partial, so it is not a key (and nothing updates that column); the
-- rotate column is in no unique index at all, so Rotate's UPDATE of it stays
-- FOR NO KEY UPDATE and a mint never waits behind a Rotate, nor a Rotate
-- behind a mint.
--
-- THE THREE PROOFS THIS FILE CARRIES.
--
-- 1. The drops are a contraction (docs/migrations.md section 3), shipped now
--    because no build that reads link_digest or session_join_counters has ever
--    been deployed: master's production build has no migration runner and no
--    campaign schema, and the integration branch deploys nowhere. So there is
--    no rollback to a build that needs either. And the store that ships in the
--    same change as this file reads neither.
--
-- 2. The new columns' CHECKs validate only NULLs. A CHECK added by ALTER TABLE
--    validates every existing row; both columns are new in this file, so every
--    existing row holds NULL in both and satisfies them, and the previous build
--    writes neither column (it does not know they exist), so a row it writes
--    during a rollout satisfies them too — 0009's argument.
--
-- 3. The actor-kind CHECK validates every existing row, and no row can fail it:
--    no code has ever written 'guest' to audit.events (the vocabulary carried
--    ActorKind.GUEST and nothing used it), and no deployed build has written an
--    audit row at all. The constraint's name is PostgreSQL's own for 0005's
--    column CHECK; tests/test_migrations_db.py reads it out of pg_constraint on a
--    database migrated to 0014 rather than assuming it.
--
-- No transaction control and not CONCURRENTLY: the runner owns the transaction
-- (docs/migrations.md section 2), and none of these tables has ever held a
-- production row.

ALTER TABLE campaign.table_sessions DROP COLUMN link_digest;

DROP TABLE campaign.session_join_counters;

ALTER TABLE campaign.table_sessions
  ADD COLUMN start_command_id TEXT
    CHECK (start_command_id IS NULL OR start_command_id ~ '^[A-Za-z0-9_-]{16,64}$'),
  ADD COLUMN rotate_command_id TEXT
    CHECK (rotate_command_id IS NULL OR rotate_command_id ~ '^[A-Za-z0-9_-]{16,64}$');

CREATE UNIQUE INDEX table_sessions_start_command_uidx
  ON campaign.table_sessions (campaign_id, start_command_id)
  WHERE start_command_id IS NOT NULL;

ALTER TABLE audit.events DROP CONSTRAINT events_actor_kind_check;

ALTER TABLE audit.events ADD CONSTRAINT events_actor_kind_check
  CHECK (actor_kind IN ('gm','participant','screen','system'));

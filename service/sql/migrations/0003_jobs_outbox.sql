-- Migration 0003 — the transactional outbox for background jobs (1kg.1.5).
--
-- The outbox carries JOBS only — deletions and sweeps: work that has to happen
-- because a transaction committed. A job is enqueued in the same transaction as
-- the change that needs it, so the two commit or roll back together
-- (service/jobs.py). It is not an event log and nothing reads it for delivery:
-- realtime delivery reads current state, and a notification is only a wake-up
-- (RT-5 of the media and realtime decision, 1kg.1.4).
--
-- A row is content-free (SEC-20 of the Workbench threat model, 1kg.1.3): the
-- payload holds identifiers, never anything a user wrote, and last_error holds
-- an exception class name, never its message. A finished job is deleted; a job
-- that ran out of attempts keeps its row with dead_at set, for the operator.

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE app.jobs (
  id           BIGSERIAL PRIMARY KEY,
  kind         TEXT NOT NULL CHECK (kind <> '' AND length(kind) <= 100),
  payload      JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
  -- One live job per (kind, dedupe_key): enqueueing the same idempotent work
  -- twice is absorbed. NULL means "never deduplicated".
  dedupe_key   TEXT CHECK (dedupe_key IS NULL OR (dedupe_key <> '' AND length(dedupe_key) <= 200)),
  run_after    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Incremented by every claim, so it is also the fencing token: only the latest
  -- claimer may reschedule the job (a worker whose lease expired may not).
  attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  -- A claim is a lease, not a held row lock: handlers call other services, and
  -- a transaction held open across that call would pin one of very few pooled
  -- connections. An instance that dies mid-job simply lets its lease run out.
  locked_until TIMESTAMPTZ,
  last_error   TEXT CHECK (last_error IS NULL OR length(last_error) <= 200),
  dead_at      TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What a claim scans: due, live jobs, oldest first.
CREATE INDEX jobs_due_idx ON app.jobs (run_after, id) WHERE dead_at IS NULL;

-- A dead job no longer holds its key, so the same work can be enqueued again
-- once whatever killed it is fixed.
CREATE UNIQUE INDEX jobs_dedupe_uidx ON app.jobs (kind, dedupe_key)
  WHERE dedupe_key IS NOT NULL AND dead_at IS NULL;

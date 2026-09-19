"""
The ordered migration runner against a real PostgreSQL (1kg.1.5).

What no fake can prove: that a database which predates the ledger and a fresh one
end up with the *same* schema; that a failed migration really leaves nothing;
that two real runners starting together apply each file once; that a held lock
and a held table time out instead of hanging a deploy.

Requires DATABASE_URL (CI always sets it). Run from the repo root:
    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_migrations_db.py -q
"""

from __future__ import annotations

import threading
import time

import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service.migrations import (
    Migration,
    MigrationDriftError,
    MigrationFailed,
    MigrationLockTimeout,
    MigrationsPending,
    Mode,
)

pytestmark = needs_db

PACKAGED = mig.discover()


@pytest.fixture
def dsn():
    with throwaway_database("mig") as target:
        yield target


def _extra(version: int, name: str, sql: str) -> Migration:
    return Migration(version, name, f"{version:04d}_{name}.sql", sql, mig.checksum(sql))


def _ledger(dsn: str) -> list[tuple[int, str, str]]:
    with connect(dsn) as conn:
        return conn.execute("SELECT version, name, checksum FROM app.schema_migrations ORDER BY version").fetchall()


def _exists(dsn: str, relation: str) -> bool:
    with connect(dsn) as conn:
        return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (relation,)).fetchone()[0]


# ── Fresh and existing databases converge ────────────────────────────────────

#: Production as it stood before b8o.2 and before any foreign key: what the
#: oldest volume still in use looks like. Built by hand, because the point is
#: that the migrations — not this test — know how to bring it forward.
PRE_EXPANSION = """
CREATE SCHEMA chat;
CREATE TABLE chat.messages (
  id BIGSERIAL PRIMARY KEY, conversation_id TEXT NOT NULL, mode TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')), content TEXT NOT NULL,
  suggestions JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX chat_messages_conv_created_idx ON chat.messages (conversation_id, created_at);
CREATE TABLE chat.attachments (
  id BIGSERIAL PRIMARY KEY, conversation_id TEXT NOT NULL, filename TEXT NOT NULL,
  content_type TEXT NOT NULL DEFAULT '', extracted_text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX chat_attachments_conv_created_idx ON chat.attachments (conversation_id, created_at);
CREATE TABLE chat.conversations (
  conversation_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE SCHEMA auth;
CREATE TABLE auth.users (
  id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'player' CHECK (role IN ('player', 'dm')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX users_email_lower_uidx ON auth.users (lower(email));
CREATE TABLE auth.invites (
  token TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'player' CHECK (role IN ('player', 'dm')),
  expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ,
  used_by BIGINT REFERENCES auth.users (id), revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX invites_created_idx ON auth.invites (created_at);

INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x');
INSERT INTO chat.conversations (conversation_id, user_id) VALUES ('owned', 1);
INSERT INTO chat.messages (conversation_id, mode, role, content) VALUES
  ('owned', 'sage', 'user', 'kept'),
  ('never-owned', 'sage', 'user', 'a row older than the ownership table');
"""

COLUMNS = """
SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
  FROM information_schema.columns WHERE table_schema IN ('chat', 'auth', 'app')
"""
#: By definition, not by name: 0001 deliberately leaves a fresh database with two
#: identically-defined CHECKs on chat.conversations (one inline, one named).
CONSTRAINTS = """
SELECT n.nspname, rel.relname, c.contype, pg_get_constraintdef(c.oid), c.convalidated
  FROM pg_constraint c
  JOIN pg_class rel ON rel.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = rel.relnamespace
 WHERE n.nspname IN ('chat', 'auth', 'app')
"""
INDEXES = "SELECT schemaname, tablename, indexdef FROM pg_indexes WHERE schemaname IN ('chat', 'auth', 'app')"


def _shape(dsn: str) -> dict[str, set[tuple]]:
    with connect(dsn) as conn:
        return {
            "columns": set(conn.execute(COLUMNS).fetchall()),
            "constraints": set(conn.execute(CONSTRAINTS).fetchall()),
            "indexes": set(conn.execute(INDEXES).fetchall()),
        }


def test_a_fresh_database_gets_every_migration_once(dsn):
    report = mig.migrate(dsn)
    assert report.applied == tuple(m.filename for m in PACKAGED)
    assert (report.current, report.state) == (len(PACKAGED), "current")
    assert _ledger(dsn) == [(m.version, m.name, m.checksum) for m in PACKAGED]
    for relation in ("chat.messages", "chat.conversations", "auth.users", "auth.invites", "app.jobs"):
        assert _exists(dsn, relation), f"{relation} was not created"

    again = mig.migrate(dsn)
    assert again.applied == () and again.state == "current"


def test_a_pre_expansion_database_is_adopted_and_ends_up_identical_to_a_fresh_one(dsn):
    """The bead's first two criteria in one: every migration runs once over the
    old shape, its rows survive, and the result is the schema a fresh database
    gets — so there is one schema, however a database came to be."""
    with connect(dsn) as conn:
        conn.execute(PRE_EXPANSION)

    report = mig.migrate(dsn)
    assert report.applied == tuple(m.filename for m in PACKAGED)
    assert mig.migrate(dsn).applied == ()

    with connect(dsn) as conn:
        kept = conn.execute("SELECT conversation_id, content FROM chat.messages ORDER BY id").fetchall()
        assert [row[0] for row in kept] == ["owned", "never-owned"], "adoption must not touch existing rows"
        # The repaired constraint: deleting the account now forgets who spent an invite.
        action = conn.execute(
            "SELECT confdeltype FROM pg_constraint WHERE conname = 'invites_used_by_fkey'"
        ).fetchone()[0]
        assert action == "n"

    with throwaway_database("mig_fresh") as fresh:
        mig.migrate(fresh)
        adopted_shape, fresh_shape = _shape(dsn), _shape(fresh)
        for part in ("columns", "constraints", "indexes"):
            assert adopted_shape[part] == fresh_shape[part], f"{part} differ between an adopted and a fresh database"
        assert _ledger(dsn) == _ledger(fresh)


def test_the_stores_reach_the_schema_through_the_same_runner(dsn):
    """`python -m service.admin_invites` calls this on a database that has never
    seen the service: it must leave a complete, recorded schema, not half of one."""
    from service.auth_store import PostgresAuthStore

    PostgresAuthStore(dsn).ensure_schema()
    assert [row[0] for row in _ledger(dsn)] == [m.version for m in PACKAGED]


# ── Drift fails loudly ───────────────────────────────────────────────────────


def test_a_released_file_that_changed_stops_the_runner(dsn):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        conn.execute("UPDATE app.schema_migrations SET checksum = repeat('0', 64) WHERE version = 2")
    with pytest.raises(MigrationDriftError, match="0002_auth_schema.sql changed after it was applied"):
        mig.migrate(dsn)


def test_a_hole_in_the_history_stops_the_runner(dsn):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        conn.execute("DELETE FROM app.schema_migrations WHERE version = 2")
    with pytest.raises(MigrationDriftError, match="0002_auth_schema.sql is unapplied, below version"):
        mig.migrate(dsn)


def test_a_database_ahead_of_this_build_is_served(dsn):
    """An older image during a rollout, or after a rollback: not an error."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO app.schema_migrations (version, name, checksum, duration_ms, applied_by) "
            "VALUES (99, 'from_a_newer_build', repeat('a', 64), 1, 'newer')"
        )
    report = mig.migrate(dsn)
    assert (report.state, report.ahead, report.current, report.applied) == ("ahead", (99,), 99, ())


def test_verify_mode_creates_nothing(dsn):
    with pytest.raises(MigrationsPending, match="0001_chat_schema.sql"):
        mig.migrate(dsn, mode=Mode.VERIFY)
    assert not _exists(dsn, "app.schema_migrations") and not _exists(dsn, "chat.messages")
    mig.migrate(dsn)
    assert mig.migrate(dsn, mode=Mode.VERIFY).state == "current"


# ── A failed migration leaves nothing ────────────────────────────────────────


def test_a_failed_migration_rolls_back_whole_and_can_be_retried(dsn):
    half = _extra(len(PACKAGED) + 1, "half_done", "CREATE TABLE app.half_done (id int);\nSELECT 1/0;")
    with pytest.raises(MigrationFailed, match="half_done.sql failed and was rolled back .*SQLSTATE 22012"):
        mig.migrate(dsn, packaged=[*PACKAGED, half])

    assert not _exists(dsn, "app.half_done"), "DDL before the failure must be gone too"
    assert [row[0] for row in _ledger(dsn)] == [m.version for m in PACKAGED], "what came before stays applied"

    fixed = _extra(half.version, "half_done", "CREATE TABLE app.half_done (id int);")
    assert mig.migrate(dsn, packaged=[*PACKAGED, fixed]).applied == (fixed.filename,)
    assert _exists(dsn, "app.half_done")


def test_a_failure_never_quotes_row_values(dsn):
    """PostgreSQL puts the offending values in DETAIL. A data migration that
    trips a constraint must not put a user's email in the deploy log."""
    leaky = _extra(
        len(PACKAGED) + 1,
        "leaky",
        "CREATE TABLE app.leaky (email text UNIQUE);\n"
        "INSERT INTO app.leaky VALUES ('gm@example.com'), ('gm@example.com');",
    )
    with pytest.raises(MigrationFailed) as caught:
        mig.migrate(dsn, packaged=[*PACKAGED, leaky])
    assert "SQLSTATE 23505" in str(caught.value)
    assert "gm@example.com" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_a_migration_that_cannot_get_its_table_lock_gives_up(dsn, monkeypatch):
    """Rather than queueing in front of every other statement on that table."""
    mig.migrate(dsn)
    monkeypatch.setattr(mig, "LOCK_TIMEOUT_S", 1)
    blocked = _extra(len(PACKAGED) + 1, "blocked", "ALTER TABLE chat.messages ADD COLUMN blocked int;")
    with connect(dsn, autocommit=False) as holder:
        holder.execute("LOCK TABLE chat.messages IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(MigrationFailed, match="SQLSTATE 55P03"):
            mig.migrate(dsn, packaged=[*PACKAGED, blocked])
        assert time.monotonic() - started < 8
        holder.rollback()
    assert mig.migrate(dsn, packaged=[*PACKAGED, blocked]).applied == (blocked.filename,)


# ── Concurrent startups ──────────────────────────────────────────────────────


def test_two_instances_starting_together_apply_each_migration_once(dsn):
    """Without the advisory lock the second runner would run 0001 alongside the
    first, and the slow marker below would fail with "already exists"."""
    slow = _extra(len(PACKAGED) + 1, "slow", "SELECT pg_sleep(1.5);\nCREATE TABLE app.slow_marker (id int);")
    packaged = [*PACKAGED, slow]
    barrier = threading.Barrier(2)
    reports: list[mig.Report] = []
    errors: list[str] = []

    def start_instance() -> None:
        try:
            barrier.wait(10)
            reports.append(mig.migrate(dsn, packaged=packaged))
        except Exception as exc:  # the type only: a driver message could name the DSN
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=start_instance) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert errors == []
    assert sorted(len(r.applied) for r in reports) == [0, len(packaged)], "one did everything, the other nothing"
    assert [row[0] for row in _ledger(dsn)] == [m.version for m in packaged]


def test_a_lock_held_too_long_times_out_instead_of_hanging_the_deploy(dsn):
    with connect(dsn) as holder:
        holder.execute("SELECT pg_advisory_lock(%s, 0)", (int(mig.AdvisoryLock.MIGRATIONS),))
        started = time.monotonic()
        with pytest.raises(MigrationLockTimeout):
            mig.migrate(dsn, lock_wait_s=1.0)
        assert time.monotonic() - started < 8
    assert not _exists(dsn, "chat.messages")


def test_a_current_database_never_waits_for_the_lock(dsn):
    """A cold start during someone else's long migration run must not queue:
    nothing is pending for it, so it takes no lock at all."""
    mig.migrate(dsn)
    with connect(dsn) as holder:
        holder.execute("SELECT pg_advisory_lock(%s, 0)", (int(mig.AdvisoryLock.MIGRATIONS),))
        started = time.monotonic()
        assert mig.migrate(dsn, lock_wait_s=30.0).state == "current"
        assert time.monotonic() - started < 5


# ── The operator's CLI ───────────────────────────────────────────────────────


def test_the_cli_reports_pending_then_applies_then_reports_current(dsn, monkeypatch, capsys):
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", dsn)
    assert mig.main(["status"]) == 1
    assert mig.main(["migrate"]) == 0
    assert mig.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "applied 0001_chat_schema.sql" in out and dsn not in out

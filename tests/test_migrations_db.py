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
import uuid

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

#: Every schema the migrations own. `campaign` and `audit` arrive with 0004 and
#: 0005; naming them here before they exist costs nothing and means a later file
#: cannot quietly leave one out of the convergence comparison.
SCHEMAS = "('chat', 'auth', 'app', 'campaign', 'audit')"

COLUMNS = f"""
SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
  FROM information_schema.columns WHERE table_schema IN {SCHEMAS}
"""
#: By definition, not by name: 0001 deliberately leaves a fresh database with two
#: identically-defined CHECKs on chat.conversations (one inline, one named).
CONSTRAINTS = f"""
SELECT n.nspname, rel.relname, c.contype, pg_get_constraintdef(c.oid), c.convalidated
  FROM pg_constraint c
  JOIN pg_class rel ON rel.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = rel.relnamespace
 WHERE n.nspname IN {SCHEMAS}
"""
INDEXES = f"SELECT schemaname, tablename, indexdef FROM pg_indexes WHERE schemaname IN {SCHEMAS}"
#: A trigger is schema the catalog queries above cannot see, and 0004's
#: AFTER INSERT on campaigns is load-bearing (RQ-1). `tgisinternal` excludes the
#: referential-integrity triggers PostgreSQL makes for foreign keys: their names
#: embed an OID, so they differ between any two databases and would make every
#: comparison fail for a reason that is not drift.
TRIGGERS = f"""
SELECT n.nspname, rel.relname, t.tgname, pg_get_triggerdef(t.oid)
  FROM pg_trigger t
  JOIN pg_class rel ON rel.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = rel.relnamespace
 WHERE n.nspname IN {SCHEMAS} AND NOT t.tgisinternal
"""


def _shape(dsn: str) -> dict[str, set[tuple]]:
    with connect(dsn) as conn:
        return {
            "columns": set(conn.execute(COLUMNS).fetchall()),
            "constraints": set(conn.execute(CONSTRAINTS).fetchall()),
            "indexes": set(conn.execute(INDEXES).fetchall()),
            "triggers": set(conn.execute(TRIGGERS).fetchall()),
        }


def test_a_fresh_database_gets_every_migration_once(dsn):
    report = mig.migrate(dsn)
    assert report.applied == tuple(m.filename for m in PACKAGED)
    assert (report.current, report.state) == (len(PACKAGED), "current")
    assert _ledger(dsn) == [(m.version, m.name, m.checksum) for m in PACKAGED]
    for relation in (
        "chat.messages",
        "chat.conversations",
        "auth.users",
        "auth.invites",
        "app.jobs",
        "campaign.campaigns",
        "campaign.authz_state",
        "campaign.participants",
        "campaign.enrolment_codes",
        "campaign.device_credentials",
        "campaign.table_sessions",
        "campaign.table_credentials",
        "campaign.session_join_counters",
    ):
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
        for part in ("columns", "constraints", "indexes", "triggers"):
            assert adopted_shape[part] == fresh_shape[part], f"{part} differ between an adopted and a fresh database"
        assert _ledger(dsn) == _ledger(fresh)


def test_the_stores_and_the_admin_cli_check_the_schema_and_never_change_it(dsn):
    """`python -m service.admin_invites` runs from an operator's checkout, which is
    not the deployed image: applying whatever migrations it holds, as a side effect
    of listing invites, would put unreviewed DDL into production."""
    from service.auth_store import PostgresAuthStore

    with pytest.raises(MigrationsPending, match="python -m service.migrations migrate"):
        PostgresAuthStore(dsn).ensure_schema()
    assert not _exists(dsn, "app.schema_migrations") and not _exists(dsn, "auth.users")

    mig.migrate(dsn)
    PostgresAuthStore(dsn).ensure_schema()


# ── The campaign schema's own guarantees (1kg.2.1) ───────────────────────────

#: A well-formed minted id, written out so the test does not depend on the minter.
CAMPAIGN_ID = "cmp_" + "a" * 22


def _one_user(conn, email: str = "gm@example.com") -> int:
    return conn.execute(
        "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
    ).fetchone()[0]


def test_a_campaign_inserted_by_raw_sql_still_gets_its_authorisation_row(dsn):
    """RQ-1, proved where no store can stand in for it: the AFTER INSERT trigger
    makes the `authz_state` row, so no route, script, fixture or later epic can
    leave a campaign without one. This is the case the in-memory twin cannot
    have — its only path to a campaign is the fake store's `create`, which
    inserts the row itself — so it is proved here against the database."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, %s)",
            (CAMPAIGN_ID, owner, "Nocturne"),
        )
        assert conn.execute(
            "SELECT authz_revision, lock_token FROM campaign.authz_state WHERE campaign_id = %s",
            (CAMPAIGN_ID,),
        ).fetchone() == (0, 0), "a raw insert must still leave a campaign authorisable"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("id", "campaign_1", id="no-prefix"),
        pytest.param("id", "cmp_short", id="too-few-bits"),
        pytest.param("name", "", id="empty-name"),
    ],
)
def test_the_database_refuses_a_campaign_row_the_application_would_never_mint(dsn, column, value):
    """The CHECK constraints of 0004 are generated from
    `service.campaign_identity.id_check_regex`; this is the database half of that
    agreement, and `service/tests/test_campaign_schema_sql.py` is the text half."""
    import psycopg

    mig.migrate(dsn)
    row = {"id": CAMPAIGN_ID, "name": "Nocturne"} | {column: value}
    with connect(dsn) as conn:
        owner = _one_user(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, %s)",
                (row["id"], owner, row["name"]),
            )


#: Every table 0004 hangs off a campaign, with the column that reaches a user.
CAMPAIGN_TABLES = (
    "campaign.campaigns",
    "campaign.authz_state",
    "campaign.participants",
    "campaign.enrolment_codes",
    "campaign.device_credentials",
    "campaign.table_sessions",
    "campaign.table_credentials",
    "campaign.session_join_counters",
)


def _a_whole_campaign(conn, owner: int) -> None:
    """One row in every table of 0004, so the cascade has something to lose."""
    conn.execute(
        "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
        (CAMPAIGN_ID, owner),
    )
    conn.execute(
        "INSERT INTO campaign.participants (id, campaign_id, alias) VALUES (%s, %s, 'Rook')",
        ("prt_" + "a" * 22, CAMPAIGN_ID),
    )
    conn.execute(
        "INSERT INTO campaign.enrolment_codes (id, participant_id, code_digest, expires_at) "
        "VALUES (%s, %s, %s, now() + interval '7 days')",
        ("enc_" + "a" * 22, "prt_" + "a" * 22, "0" * 64),
    )
    conn.execute(
        "INSERT INTO campaign.device_credentials (id, participant_id, credential_digest) "
        "VALUES (%s, %s, %s)",
        ("dev_" + "a" * 22, "prt_" + "a" * 22, "1" * 64),
    )
    conn.execute(
        "INSERT INTO campaign.table_sessions (id, campaign_id, gm_user_id, state, expires_at) "
        "VALUES (%s, %s, %s, 'live', now() + interval '12 hours')",
        ("ses_" + "a" * 22, CAMPAIGN_ID, owner),
    )
    conn.execute(
        "INSERT INTO campaign.table_credentials "
        "(id, session_id, link_generation, credential_digest) VALUES (%s, %s, 1, %s)",
        ("tcr_" + "a" * 22, "ses_" + "a" * 22, "2" * 64),
    )
    conn.execute(
        "INSERT INTO campaign.session_join_counters (session_id, link_generation, joins) "
        "VALUES (%s, 1, 3)",
        ("ses_" + "a" * 22,),
    )


def test_deleting_a_user_cascades_through_every_table_of_the_campaign_schema(dsn):
    """SEC-36: deleting the account takes the campaigns with it, and everything
    that hangs off them — including the authorisation row, which is what an
    orphan would keep a deleted campaign authorisable by."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        _a_whole_campaign(conn, owner)
        for table in CAMPAIGN_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1, table

        conn.execute("DELETE FROM auth.users WHERE id = %s", (owner,))
        for table in CAMPAIGN_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, (
                f"{table} still holds a row of a deleted account"
            )


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


def test_a_conversion_failure_does_not_quote_the_value_it_choked_on(dsn):
    """Class 22 puts the datum in the PRIMARY message, not in DETAIL."""
    tighten = _extra(
        len(PACKAGED) + 1,
        "tighten",
        "CREATE TABLE app.handles (handle text);\n"
        "INSERT INTO app.handles VALUES ('Seraphine, the hooded stranger');\n"
        "ALTER TABLE app.handles ALTER COLUMN handle TYPE integer USING handle::integer;",
    )
    with pytest.raises(MigrationFailed) as caught:
        mig.migrate(dsn, packaged=[*PACKAGED, tighten])
    assert "SQLSTATE 22P02" in str(caught.value)
    assert "Seraphine" not in str(caught.value)
    assert not _exists(dsn, "app.handles")


def test_a_file_that_commits_by_itself_never_gets_a_ledger_row(dsn):
    """`END;` is SQL for COMMIT and cannot be banned textually (PL/pgSQL uses it),
    so the runner asks the server whether it is still inside its transaction.
    What the file did before its COMMIT is permanent — this pins that honestly —
    but it is never recorded as a clean apply, and the failure says what to do."""
    sneaky = _extra(
        len(PACKAGED) + 1,
        "sneaky",
        "CREATE TABLE app.before_commit (id int);\nEND;\nCREATE TABLE app.after_commit (id int);",
    )
    with pytest.raises(MigrationFailed, match="sneaky.sql ended the runner's transaction"):
        mig.migrate(dsn, packaged=[*PACKAGED, sneaky])
    assert [row[0] for row in _ledger(dsn)] == [m.version for m in PACKAGED]
    assert _exists(dsn, "app.before_commit"), "committed by the file itself; the message says to repair by hand"


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


def test_a_role_that_may_not_create_the_ledger_is_a_verdict_not_an_outage(dsn):
    """Startup degrades around an unreachable database. A database that answers
    "no" will answer it again at every start, so the process must not come up
    half-working on it."""
    from psycopg.conninfo import make_conninfo

    role = f"mig_limited_{uuid.uuid4().hex[:8]}"
    with connect(dsn) as admin:
        admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'limited_not_a_secret'")
    try:
        limited = make_conninfo(dsn, user=role, password="limited_not_a_secret")
        with pytest.raises(MigrationFailed, match="refused the migration ledger .*SQLSTATE 42501"):
            mig.migrate(limited)
    finally:
        with connect(dsn) as admin:
            admin.execute(f"DROP ROLE {role}")


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

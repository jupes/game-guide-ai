"""The ordered migration runner (1kg.1.5) — everything that needs no database.

The packaged files, the manifest, the plan, and the runner's control flow against
a scripted connection: the lock, the second look under the lock, a failure that
rolls back, `verify` mode. What only PostgreSQL can prove — that a rolled-back
migration really leaves nothing, that two real runners serialise — is in
`tests/test_migrations_db.py`, which CI runs against a real server.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from service import migrations as mig
from service.migrations import (
    AppliedMigration,
    Migration,
    MigrationDriftError,
    MigrationFailed,
    MigrationLockTimeout,
    MigrationPackageError,
    MigrationsPending,
    Mode,
)

# ── The packaged files ───────────────────────────────────────────────────────


def test_the_packaged_migrations_are_a_valid_sequence():
    """Runs in every CI build, so a bad set never reaches a database: numbered
    from 0001 with no gap, named, non-empty, and matching the manifest."""
    packaged = mig.discover()
    assert [m.version for m in packaged] == list(range(1, len(packaged) + 1))
    assert [m.filename for m in packaged][:3] == [
        "0001_chat_schema.sql", "0002_auth_schema.sql", "0003_jobs_outbox.sql",
    ]


def test_the_baseline_is_idempotent_by_construction():
    """0001 and 0002 are applied over databases that already hold their objects
    (every database that predates the ledger), so they may never CREATE without
    IF NOT EXISTS or ALTER without a guard."""
    for migration in mig.discover()[:2]:
        for line in migration.sql.splitlines():
            statement = line.strip().upper()
            if statement.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX", "CREATE SCHEMA")):
                assert "IF NOT EXISTS" in statement, f"{migration.filename}: {line.strip()}"


def _package(tmp_path: Path, files: dict[str, str], manifest: str | None = None) -> Path:
    for name, sql in files.items():
        (tmp_path / name).write_text(sql, encoding="utf-8", newline="\n")
    if manifest is None:
        mig.append_to_manifest(tmp_path)
    else:
        (tmp_path / mig.MANIFEST).write_text(manifest, encoding="utf-8", newline="\n")
    return tmp_path


def test_a_checkout_with_crlf_line_endings_hashes_like_the_image(tmp_path):
    (tmp_path / "lf").mkdir()
    (tmp_path / "crlf").mkdir()
    (tmp_path / "lf" / "0001_a.sql").write_bytes(b"CREATE TABLE a ();\n-- two lines\n")
    (tmp_path / "crlf" / "0001_a.sql").write_bytes(b"CREATE TABLE a ();\r\n-- two lines\r\n")
    for directory in ("lf", "crlf"):
        mig.append_to_manifest(tmp_path / directory)
    lf, crlf = mig.discover(tmp_path / "lf"), mig.discover(tmp_path / "crlf")
    assert lf[0].checksum == crlf[0].checksum
    assert "\r" not in crlf[0].sql


@pytest.mark.parametrize(
    ("files", "manifest", "problem"),
    [
        ({"1_a.sql": "SELECT 1;"}, "", "not named NNNN_snake_case.sql"),
        ({"0001_Bad-Name.sql": "SELECT 1;"}, "", "not named NNNN_snake_case.sql"),
        ({"0001_a.sql": "  \n"}, None, "is empty"),
        ({"0001_a.sql": "BEGIN;\nCREATE TABLE a ();\nCOMMIT;"}, None, "controls its own transaction"),
        ({"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 2;"}, None, "version 0001 is used by more than one file"),
        ({"0001_a.sql": "SELECT 1;", "0003_c.sql": "SELECT 3;"}, None, "no gap"),
        ({"0002_b.sql": "SELECT 2;"}, None, "no gap"),
        ({"0001_a.sql": "SELECT 1;"}, f"0001_a.sql {'0' * 64}\n", "no longer matches its checksum"),
        ({"0001_a.sql": "SELECT 1;"}, "", "is not in manifest.txt"),
        ({"0001_a.sql": "SELECT 1;"}, "0001_a.sql nonsense\n", "line 1 is not '<file> <sha256>'"),
    ],
)
def test_an_invalid_package_cannot_be_discovered(tmp_path, files, manifest, problem):
    with pytest.raises(MigrationPackageError, match=problem):
        mig.discover(_package(tmp_path, files, manifest))


def test_a_plpgsql_block_is_not_mistaken_for_transaction_control(tmp_path):
    sql = (
        "DO $$\nBEGIN\n  PERFORM 1;\nEND $$;\n"
        "CREATE FUNCTION f() RETURNS int AS $f$\nBEGIN\n  RETURN 1;\nEND;\n$f$ LANGUAGE plpgsql;\n"
    )
    assert len(mig.discover(_package(tmp_path, {"0001_a.sql": sql}))) == 1


def test_a_missing_manifest_and_a_listed_but_missing_file_are_both_named(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationPackageError, match="manifest.txt is missing"):
        mig.discover(tmp_path)
    _package(tmp_path, {}, None)
    (tmp_path / "0001_a.sql").unlink()
    with pytest.raises(MigrationPackageError, match="lists 0001_a.sql, which is not packaged"):
        mig.discover(tmp_path)


def test_the_manifest_lists_each_file_once_and_in_order(tmp_path):
    _package(tmp_path, {"0001_a.sql": "SELECT 1;", "0002_b.sql": "SELECT 2;"})
    lines = (tmp_path / mig.MANIFEST).read_text(encoding="utf-8").splitlines()
    (tmp_path / mig.MANIFEST).write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    with pytest.raises(MigrationPackageError, match="each file once, in order"):
        mig.discover(tmp_path)


def test_every_problem_is_reported_at_once(tmp_path):
    _package(tmp_path, {"0001_a.sql": "COMMIT;", "0003_c.sql": "SELECT 3;", "x.sql": "SELECT 0;"}, "")
    with pytest.raises(MigrationPackageError) as caught:
        mig.discover(tmp_path)
    assert str(caught.value).count(";") >= 3


def test_the_manifest_is_append_only(tmp_path):
    """A released line is never rewritten by the tool. Editing a released file
    therefore fails `discover()` until someone changes its checksum by hand — in
    a diff a reviewer will see. Two branches that each append a line conflict in
    git, which is what stops two files sharing a number from merging cleanly."""
    _package(tmp_path, {"0001_a.sql": "SELECT 1;"})
    before = (tmp_path / mig.MANIFEST).read_text(encoding="utf-8")
    (tmp_path / "0001_a.sql").write_text("SELECT 'edited';", encoding="utf-8")
    (tmp_path / "0002_b.sql").write_text("SELECT 2;", encoding="utf-8")

    assert mig.append_to_manifest(tmp_path) == ["0002_b.sql"]
    after = (tmp_path / mig.MANIFEST).read_text(encoding="utf-8")
    assert after.startswith(before) and after.count("\n") == 2
    assert mig.append_to_manifest(tmp_path) == []
    with pytest.raises(MigrationPackageError, match="0001_a.sql no longer matches"):
        mig.discover(tmp_path)


def test_the_manifest_may_carry_comments_and_need_not_end_in_a_newline(tmp_path):
    _package(tmp_path, {"0001_a.sql": "SELECT 1;"})
    manifest = tmp_path / mig.MANIFEST
    line = manifest.read_text(encoding="utf-8").strip()
    manifest.write_text(f"# ordered, append-only\n\n{line}  # the baseline", encoding="utf-8")
    assert [m.filename for m in mig.discover(tmp_path)] == ["0001_a.sql"]

    (tmp_path / "0002_b.sql").write_text("SELECT 2;", encoding="utf-8")
    assert mig.append_to_manifest(tmp_path) == ["0002_b.sql"]
    assert [m.filename for m in mig.discover(tmp_path)] == ["0001_a.sql", "0002_b.sql"]


def test_the_manifest_command_has_nothing_to_add_to_a_complete_package(capsys):
    assert mig.main(["manifest"]) == 0
    assert "manifest already complete" in capsys.readouterr().out


def test_the_manifest_tool_refuses_a_badly_named_file(tmp_path):
    (tmp_path / "oops.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationPackageError, match="oops.sql is not named"):
        mig.append_to_manifest(tmp_path)


# ── The plan ─────────────────────────────────────────────────────────────────


def _migration(version: int, name: str = "m", sql: str | None = None) -> Migration:
    text = sql if sql is not None else f"SELECT {version};"
    return Migration(version, name, f"{version:04d}_{name}.sql", text, mig.checksum(text))


def _applied(*migrations: Migration) -> list[AppliedMigration]:
    return [AppliedMigration(m.version, m.name, m.checksum) for m in migrations]


ONE, TWO, THREE = _migration(1), _migration(2), _migration(3)


def test_an_empty_ledger_needs_everything_and_a_full_one_nothing():
    assert mig.plan([ONE, TWO, THREE], []).pending == (ONE, TWO, THREE)
    assert mig.plan([ONE, TWO, THREE], _applied(ONE, TWO)).pending == (THREE,)
    done = mig.plan([ONE, TWO, THREE], _applied(ONE, TWO, THREE))
    assert done.pending == () and done.ahead == ()


def test_a_released_file_that_changed_is_drift():
    edited = _migration(2, sql="SELECT 'edited';")
    with pytest.raises(MigrationDriftError, match="0002_m.sql changed after it was applied"):
        mig.plan([ONE, edited, THREE], _applied(ONE, TWO))


def test_a_renamed_file_is_drift():
    """Two branches that both shipped a 0002 look exactly like this."""
    with pytest.raises(MigrationDriftError, match="applied as 'm', but this build packages it as 'billing'"):
        mig.plan([ONE, _migration(2, "billing")], _applied(ONE, TWO))


def test_a_migration_slotted_in_below_an_applied_one_is_drift():
    with pytest.raises(MigrationDriftError, match="0002_m.sql is unapplied, below version 0003"):
        mig.plan([ONE, TWO, THREE], _applied(ONE, THREE))


def test_a_database_ahead_of_the_build_is_not_drift():
    """An older image mid-rollout, or an image rolled back: served, and reported."""
    todo = mig.plan([ONE, TWO], _applied(ONE, TWO, THREE))
    assert todo.pending == () and todo.ahead == (3,)


def test_a_build_with_work_to_do_below_a_newer_schema_is_drift():
    """Ahead is only safe when this build's own history is complete."""
    with pytest.raises(MigrationDriftError, match="history was reordered"):
        mig.plan([ONE, TWO], _applied(ONE, THREE))


# ── The runner, against a scripted connection ────────────────────────────────


class FakeDbError(Exception):
    """Shaped like a psycopg error: a SQLSTATE, a primary message, and a DETAIL
    that quotes row values — which must never reach a log or an exception."""

    sqlstate = "23505"

    class diag:  # noqa: N801 - mirrors psycopg's attribute name
        message_primary = 'duplicate key value violates unique constraint "users_email_lower_uidx"'
        message_detail = "Key (lower(email))=(gm@example.com) already exists."

    def __str__(self) -> str:
        return f"{self.diag.message_primary}\nDETAIL:  {self.diag.message_detail}"


class FakePostgres:
    """Just enough server for the runner: a ledger, one lock, a record of what ran."""

    def __init__(self) -> None:
        self.ledger: dict[int, tuple[str, str, str]] | None = None  # None: the table does not exist
        self.executed: list[str] = []
        self.settings: dict[str, str] = {}
        self.fail_on: str | None = None
        self.error: Exception = FakeDbError()
        self.busy_for = 0  # try-lock calls that find another instance holding the lock
        self.other_instance_applies: list[Migration] = []  # ...and what that instance does meanwhile
        self.lock_attempts = 0
        self.unlocks = 0
        self.unlock_breaks = False
        self.ledger_error: Exception | None = None
        self.connections = 0

    @contextmanager
    def connect(self):
        self.connections += 1
        yield FakeConnection(self)

    def apply_as_other_instance(self) -> None:
        self.ledger = self.ledger if self.ledger is not None else {}
        for migration in self.other_instance_applies:
            self.ledger[migration.version] = (migration.name, migration.checksum, "other")


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, server: FakePostgres) -> None:
        self.server = server
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

    @contextmanager
    def transaction(self):
        server = self.server
        ledger = dict(server.ledger) if server.ledger is not None else None
        executed, settings = list(server.executed), dict(server.settings)
        self.info.transaction_status = TransactionStatus.INTRANS
        try:
            yield
        except BaseException:
            if self.info.transaction_status == TransactionStatus.INTRANS:
                server.ledger, server.executed, server.settings = ledger, executed, settings
            raise
        finally:
            self.info.transaction_status = TransactionStatus.IDLE

    def execute(self, sql: str, params=None) -> _Rows:  # noqa: C901 - a dispatch table, flat on purpose
        server = self.server
        if sql.startswith("SELECT to_regclass"):
            return _Rows([(server.ledger is not None,)])
        if sql.startswith("SELECT version, name, checksum"):
            assert server.ledger is not None
            return _Rows([(v, name, digest) for v, (name, digest, _) in sorted(server.ledger.items())])
        if sql.startswith("SELECT pg_try_advisory_lock"):
            server.lock_attempts += 1
            if server.busy_for > 0:
                server.busy_for -= 1
                if server.busy_for == 0:
                    server.apply_as_other_instance()
                return _Rows([(False,)])
            return _Rows([(True,)])
        if sql.startswith("SELECT pg_advisory_unlock"):
            server.unlocks += 1
            if server.unlock_breaks:
                raise ConnectionError("the connection is gone")
            return _Rows([(True,)])
        if sql == mig._LEDGER_DDL:
            if server.ledger_error is not None:
                raise server.ledger_error
            server.ledger = server.ledger if server.ledger is not None else {}
            return _Rows([])
        if sql.startswith("SELECT set_config"):
            assert sql.endswith(", true)"), "a timeout must be local to the migration's transaction"
            server.settings[sql.split("'")[1]] = params[0]
            return _Rows([])
        if sql.startswith("INSERT INTO app.schema_migrations"):
            assert server.ledger is not None
            version, name, digest, duration_ms, applied_by = params
            assert duration_ms >= 0
            server.ledger[version] = (name, digest, applied_by)
            return _Rows([])
        if server.fail_on is not None and server.fail_on in sql:
            raise server.error
        server.executed.append(sql)
        if "END;" in sql:  # the file committed by itself: what ran before is permanent
            self.info.transaction_status = TransactionStatus.IDLE
        return _Rows([])


def _never_sleep(_seconds: float) -> None:
    return None


def test_a_fresh_database_gets_every_migration_once_in_order():
    server = FakePostgres()
    report = mig.migrate(connect=server.connect, packaged=[ONE, TWO, THREE])

    assert report.applied == ("0001_m.sql", "0002_m.sql", "0003_m.sql")
    assert (report.current, report.ahead, report.state) == (3, (), "current")
    assert server.executed == [ONE.sql, TWO.sql, THREE.sql]
    assert server.ledger is not None and sorted(server.ledger) == [1, 2, 3]
    assert (server.lock_attempts, server.unlocks) == (1, 1)
    # Every migration ran under both timeouts.
    assert server.settings == {"lock_timeout": "10s", "statement_timeout": "120s"}


def test_a_current_database_costs_one_read_and_takes_no_lock():
    server = FakePostgres()
    mig.migrate(connect=server.connect, packaged=[ONE, TWO])
    server.lock_attempts = server.unlocks = 0
    server.executed.clear()

    report = mig.migrate(connect=server.connect, packaged=[ONE, TWO])

    assert report.applied == () and report.state == "current"
    assert (server.lock_attempts, server.unlocks, server.executed) == (0, 0, [])


def test_only_what_is_pending_is_applied():
    server = FakePostgres()
    mig.migrate(connect=server.connect, packaged=[ONE, TWO])
    report = mig.migrate(connect=server.connect, packaged=[ONE, TWO, THREE])
    assert report.applied == ("0003_m.sql",)
    assert server.executed == [ONE.sql, TWO.sql, THREE.sql]


def test_the_revision_that_applied_a_migration_is_recorded(monkeypatch):
    monkeypatch.setenv("K_REVISION", "game-guide-ai-00042-abc")
    server = FakePostgres()
    mig.migrate(connect=server.connect, packaged=[ONE])
    assert server.ledger is not None and server.ledger[1][2] == "game-guide-ai-00042-abc"


def test_a_failed_migration_rolls_back_and_names_no_row_values():
    server = FakePostgres()
    server.fail_on = TWO.sql
    with pytest.raises(MigrationFailed) as caught:
        mig.migrate(connect=server.connect, packaged=[ONE, TWO, THREE])

    message = str(caught.value)
    assert "0002_m.sql failed and was rolled back" in message
    assert "SQLSTATE 23505" in message and "duplicate key value" in message
    assert "gm@example.com" not in message, "the DETAIL line quotes row values"
    assert caught.value.__cause__ is None and caught.value.__context__ is None, (
        "the driver's exception must not ride along into the log"
    )
    # 0001 stays applied, 0002 left nothing, 0003 was never attempted — and the lock is free.
    assert server.executed == [ONE.sql]
    assert server.ledger is not None and sorted(server.ledger) == [1]
    assert server.unlocks == 1


class FakeConversionError(FakeDbError):
    """Class 22: PostgreSQL puts the offending datum in the PRIMARY message."""

    sqlstate = "22P02"

    class diag:  # noqa: N801 - mirrors psycopg's attribute name
        message_primary = 'invalid input syntax for type uuid: "Seraphine, the hooded stranger"'
        message_detail = None


def test_a_conversion_failure_does_not_quote_the_value_it_choked_on():
    """A later migration tightens a free-text column users wrote into. The deploy
    log must name the file and the SQLSTATE — not one user's text (SEC-20)."""
    server = FakePostgres()
    server.fail_on, server.error = ONE.sql, FakeConversionError()
    with pytest.raises(MigrationFailed) as caught:
        mig.migrate(connect=server.connect, packaged=[ONE])
    assert "SQLSTATE 22P02" in str(caught.value) and "FakeConversionError" in str(caught.value)
    assert "Seraphine" not in str(caught.value) and "invalid input syntax" not in str(caught.value)


def test_a_file_that_commits_by_itself_is_caught_by_what_the_server_says():
    """The lint knows `COMMIT;` at the start of a line. `END;`, `COMMIT WORK;` and a
    COMMIT in mid-line get past it, so the runner asks the connection whether it
    is still inside its transaction — and never writes a ledger row outside one,
    which would record a half-applied file as cleanly applied."""
    sneaky = _migration(2, sql="CREATE TABLE a (id int);\nEND;\nCREATE TABLE b (id int);")
    server = FakePostgres()
    with pytest.raises(MigrationFailed, match="0002_m.sql ended the runner's transaction"):
        mig.migrate(connect=server.connect, packaged=[ONE, sneaky, THREE])
    assert server.ledger is not None and sorted(server.ledger) == [1], "no ledger row for the half-applied file"
    assert THREE.sql not in server.executed and server.unlocks == 1


def test_an_image_without_its_migrations_directory_is_a_packaging_verdict(tmp_path):
    """As a bare `OSError` this would be taken for "the database is unreachable"
    at startup, and the broken revision would come up degraded and take traffic."""
    with pytest.raises(mig.MigrationPackageError, match="missing or unreadable") as caught:
        mig.discover(tmp_path / "not-shipped")
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_the_loser_of_a_concurrent_startup_waits_then_finds_nothing_to_do():
    server = FakePostgres()
    server.busy_for = 3
    server.other_instance_applies = [ONE, TWO]
    slept: list[float] = []

    report = mig.migrate(connect=server.connect, packaged=[ONE, TWO], sleep=slept.append)

    assert report.applied == () and report.state == "current"
    assert server.executed == [], "the other instance already ran them; running them again is the bug"
    assert server.lock_attempts == 4 and slept == [mig.LOCK_POLL_S] * 3
    assert server.unlocks == 1


def test_a_lock_that_never_frees_times_out_loudly():
    server = FakePostgres()
    server.busy_for = 10_000
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(MigrationLockTimeout, match="held the migration lock for 5 s"):
        mig.migrate(connect=server.connect, packaged=[ONE], lock_wait_s=5, sleep=sleep, clock=lambda: now[0])
    assert server.executed == [] and server.unlocks == 0


def test_verify_mode_never_applies_and_says_what_is_missing():
    server = FakePostgres()
    mig.migrate(connect=server.connect, packaged=[ONE])
    with pytest.raises(MigrationsPending, match="this build needs 0002_m.sql, 0003_m.sql"):
        mig.migrate(connect=server.connect, packaged=[ONE, TWO, THREE], mode=Mode.VERIFY)
    assert server.executed == [ONE.sql] and server.lock_attempts == 1

    assert mig.migrate(connect=server.connect, packaged=[ONE], mode=Mode.VERIFY).state == "current"


def test_drift_is_found_before_anything_is_touched():
    server = FakePostgres()
    mig.migrate(connect=server.connect, packaged=[ONE, TWO])
    server.lock_attempts = 0
    with pytest.raises(MigrationDriftError):
        mig.migrate(connect=server.connect, packaged=[ONE, _migration(2, sql="SELECT 'edited';"), THREE])
    assert server.lock_attempts == 0 and server.executed == [ONE.sql, TWO.sql]


def test_an_older_build_serves_a_newer_schema_and_says_so(caplog):
    server = FakePostgres()
    mig.migrate(connect=server.connect, packaged=[ONE, TWO, THREE])
    with caplog.at_level(logging.WARNING, logger="service.migrations"):
        report = mig.migrate(connect=server.connect, packaged=[ONE, TWO])
    assert (report.state, report.ahead, report.current) == ("ahead", (3,), 3)
    assert "ahead of this build (versions 0003)" in caplog.text


def test_a_lock_that_cannot_be_released_does_not_mask_the_outcome(caplog):
    server = FakePostgres()
    server.unlock_breaks = True
    with caplog.at_level(logging.WARNING, logger="service.migrations"):
        report = mig.migrate(connect=server.connect, packaged=[ONE])
    assert report.applied == ("0001_m.sql",)
    assert "could not release the lock (ConnectionError)" in caplog.text


def test_a_database_that_refuses_the_ledger_is_a_verdict_not_an_outage():
    """Startup treats the driver's errors as an outage and comes up degraded. A
    role that may not create the ledger will be refused again at every start, so
    that must stop the process instead — and still release the lock."""
    server = FakePostgres()
    server.ledger_error = psycopg.errors.InsufficientPrivilege("permission denied for database app")
    with pytest.raises(MigrationFailed, match=r"refused the migration ledger \(InsufficientPrivilege") as caught:
        mig.migrate(connect=server.connect, packaged=[ONE])
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert server.unlocks == 1 and server.executed == []


def test_a_connection_that_does_not_survive_is_still_an_outage():
    server = FakePostgres()
    server.ledger_error = psycopg.OperationalError("server closed the connection unexpectedly")
    with pytest.raises(psycopg.OperationalError):
        mig.migrate(connect=server.connect, packaged=[ONE])


def test_the_owner_dsn_is_a_seam_of_its_own(monkeypatch):
    """Least-privilege roles (1ir.1.13): the runner may connect as the schema
    owner while the service connects as something smaller."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://runtime@db/app")
    monkeypatch.delenv("MIGRATIONS_DATABASE_URL", raising=False)
    assert mig.migrations_dsn() == "postgresql://runtime@db/app"
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", "postgresql://owner@db/app")
    assert mig.migrations_dsn() == "postgresql://owner@db/app"

    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", " postgresql://owner:S3cretPW@db/app")
    with pytest.raises(ValueError) as caught:
        mig.migrations_dsn()
    assert str(caught.value) == "MIGRATIONS_DATABASE_URL is not a valid PostgreSQL connection string"


# ── The CLI ──────────────────────────────────────────────────────────────────


def test_verify_checks_the_package_without_a_database(capsys):
    assert mig.main(["verify"]) == 0
    assert "migrations match manifest.txt" in capsys.readouterr().out


def test_status_exits_1_while_migrations_are_pending_and_0_once_current(monkeypatch, capsys):
    server = FakePostgres()
    monkeypatch.setattr(mig, "_connector", lambda dsn: server.connect)

    assert mig.main(["status"]) == 1
    assert "pending" in capsys.readouterr().out

    assert mig.main(["migrate"]) == 0
    assert "applied 0001_chat_schema.sql" in capsys.readouterr().out
    assert mig.main(["migrate"]) == 0
    assert "nothing to apply" in capsys.readouterr().out

    assert mig.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "pending" not in out and "0003_jobs_outbox.sql" in out


def test_status_shows_what_a_newer_build_applied(monkeypatch, capsys):
    server = FakePostgres()
    monkeypatch.setattr(mig, "_connector", lambda dsn: server.connect)
    mig.main(["migrate"])
    assert server.ledger is not None
    server.ledger[99] = ("from_the_future", "0" * 64, "newer")
    capsys.readouterr()
    assert mig.main(["status"]) == 0
    assert "applied by a newer build  0099" in capsys.readouterr().out


def test_the_cli_reports_a_verdict_and_an_outage_without_a_dsn(monkeypatch, capsys):
    server = FakePostgres()
    monkeypatch.setattr(mig, "_connector", lambda dsn: server.connect)
    mig.main(["migrate"])
    assert server.ledger is not None
    server.ledger[2] = ("auth_schema", "f" * 64, "local")
    capsys.readouterr()
    assert mig.main(["status"]) == 2
    assert "error: 0002_auth_schema.sql changed after it was applied" in capsys.readouterr().out

    @contextmanager
    def unreachable():
        raise ConnectionRefusedError("postgresql://rag:hunter2@db:5432/app refused")
        yield  # pragma: no cover

    monkeypatch.setattr(mig, "_connector", lambda dsn: unreachable)
    assert mig.main(["migrate"]) == 2
    out = capsys.readouterr().out
    assert "could not reach the database (ConnectionRefusedError)" in out
    assert "hunter2" not in out

"""
Ordered application migrations (1kg.1.5).

`service/sql/migrations/NNNN_name.sql` is the one definition of the application
schema (`chat`, `auth`, `app`). `migrate()` applies whatever a database has not
seen yet — once, in order, each file in its own transaction — and records it in
`app.schema_migrations` with its checksum.

  * A **fresh** database and an **existing** one take the same path, so there is
    no second mechanism to drift from. A database that predates the ledger is
    adopted by the idempotent baseline (0001, 0002), applied over it once.
  * **Concurrent startups** serialise on a session advisory lock, and the loser
    finds nothing left to do. A database that is already current costs one
    SELECT and takes no lock at all — unlike the DDL this replaces, which took
    ACCESS EXCLUSIVE locks on live tables at every cold start.
  * **Drift fails loudly.** A released file whose bytes changed, a renamed file,
    or a migration slotted in below one already applied stops startup, which on
    Cloud Run keeps traffic on the previous revision.
  * **A database ahead of the code is not drift.** During a rollout, and after
    an image is rolled back, an older build runs against a newer schema; the
    expand/contract policy in docs/migrations.md is what makes that safe.

`manifest.txt` lists every file with its checksum, in order. It exists for the
pull request rather than for the database: two branches that each add a
migration both append a line to it, so git reports a conflict instead of letting
two files with one number merge cleanly; and an edited, already-released file
fails `discover()` in CI long before it meets a ledger.

The corpus schema (`dnd`, `vector-db/init/`) is deliberately not here. It needs
the `vector` extension and an ingested corpus, and belongs to the ingestion
pipeline (`scripts/bootstrap-db.sh`).

Diagnostics never carry a DSN, and a failed statement is reported by SQLSTATE and
primary message only: the DETAIL line of a constraint violation quotes row
values, and logs are not a place for those (SEC-20, SEC-21).

    python -m service.migrations status     # what is applied, what is pending
    python -m service.migrations migrate    # apply pending migrations
    python -m service.migrations verify     # check the packaged files against the manifest
    python -m service.migrations manifest   # append new files to the manifest
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from .db import AdvisoryLock, default_dsn

log = logging.getLogger(__name__)

MANIFEST = "manifest.txt"

#: `0007_document_versions.sql`: four digits, then snake_case.
_FILE_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")

#: A migration runs inside the runner's transaction, together with its ledger
#: row. Its own `COMMIT;` would end that transaction half way and leave a schema
#: change the ledger never heard of. (PL/pgSQL's `BEGIN` takes no semicolon.)
_TRANSACTION_CONTROL = re.compile(
    r"^\s*(begin|start\s+transaction|commit|rollback)\s*;", re.IGNORECASE | re.MULTILINE
)

#: How long a starting instance waits for another one's migration run. Longer
#: than any one migration may take (STATEMENT_TIMEOUT_S), shorter than Cloud
#: Run's startup probe window (240 s) so the failure that surfaces is this one.
LOCK_WAIT_S = 150.0
LOCK_POLL_S = 0.5
#: A migration that cannot get its table lock gives up rather than queueing in
#: front of every other statement that wants the table.
LOCK_TIMEOUT_S = 10
STATEMENT_TIMEOUT_S = 120
CONNECT_TIMEOUT_S = 10

_LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.schema_migrations (
  version     INTEGER PRIMARY KEY CHECK (version > 0),
  name        TEXT NOT NULL,
  checksum    TEXT NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
  applied_by  TEXT NOT NULL
);
"""


class MigrationError(Exception):
    """Deterministic, and fatal at startup: retrying cannot help, so the process
    must not come up and serve a schema it does not understand."""


class MigrationPackageError(MigrationError):
    """The packaged files are not a valid, manifest-matching sequence."""


class MigrationDriftError(MigrationError):
    """The database's ledger and the packaged files disagree about history."""


class MigrationsPending(MigrationError):
    """`verify` mode found migrations it is not allowed to apply."""


class MigrationLockTimeout(MigrationError):
    """Another runner held the migration lock for longer than we may wait."""


class MigrationFailed(MigrationError):
    """A migration's SQL failed. Its transaction rolled back; nothing of it remains."""


class Mode(str, Enum):
    #: Apply what is pending. Today's deployment: the service owns its schema.
    APPLY = "apply"
    #: Never apply; pending migrations are fatal. For a runtime role without DDL
    #: rights, where a deploy step runs `migrate` as the owner (1ir.1.13, 1kg.9.5).
    VERIFY = "verify"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    filename: str
    #: LF-normalised, which is also what is executed and what is hashed.
    sql: str
    checksum: str


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str


@dataclass(frozen=True)
class Plan:
    pending: tuple[Migration, ...]
    #: Applied versions newer than anything this build packages.
    ahead: tuple[int, ...]


@dataclass(frozen=True)
class Report:
    #: Filenames this call applied, in order.
    applied: tuple[str, ...]
    #: The newest version in the ledger afterwards (0 for an empty ledger).
    current: int
    ahead: tuple[int, ...]

    @property
    def state(self) -> str:
        """What `/healthz` says: `current`, or `ahead` on an older build."""
        return "ahead" if self.ahead else "current"


# ── The packaged files ───────────────────────────────────────────────────────


def normalise(raw: bytes) -> str:
    """A checkout with `core.autocrlf` must hash exactly like the Linux image."""
    return raw.decode("utf-8").replace("\r\n", "\n")


def checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def packaged_root() -> Traversable:
    """`importlib.resources`, so a wheel and the source tree behave alike."""
    return resources.files("service").joinpath("sql", "migrations")


def read_manifest(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None:
            raise MigrationPackageError(f"{MANIFEST} line {number} is not '<file> <sha256>'")
        entries.append((parts[0], parts[1]))
    return entries


def _read_files(root: Traversable) -> tuple[list[Migration], list[str]]:
    migrations: list[Migration] = []
    problems: list[str] = []
    for filename in sorted(entry.name for entry in root.iterdir() if entry.name.endswith(".sql")):
        match = _FILE_NAME.match(filename)
        if match is None:
            problems.append(f"{filename} is not named NNNN_snake_case.sql")
            continue
        sql = normalise(root.joinpath(filename).read_bytes())
        if not sql.strip():
            problems.append(f"{filename} is empty")
        if _TRANSACTION_CONTROL.search(sql):
            problems.append(f"{filename} controls its own transaction; the runner owns it")
        migrations.append(
            Migration(int(match["version"]), match["name"], filename, sql, checksum(sql))
        )
    return migrations, problems


def discover(root: Traversable | None = None) -> tuple[Migration, ...]:
    """Every packaged migration, in order — or every reason they are not a valid set."""
    root = root if root is not None else packaged_root()
    migrations, problems = _read_files(root)

    versions = [m.version for m in migrations]
    for version in sorted({v for v in versions if versions.count(v) > 1}):
        problems.append(f"version {version:04d} is used by more than one file")
    if sorted(set(versions)) != list(range(1, len(set(versions)) + 1)):
        problems.append("versions must run 0001, 0002, ... with no gap")

    manifest_file = root.joinpath(MANIFEST)
    if not manifest_file.is_file():
        problems.append(f"{MANIFEST} is missing")
    else:
        listed = read_manifest(normalise(manifest_file.read_bytes()))
        by_name = {m.filename: m for m in migrations}
        for filename, listed_checksum in listed:
            found = by_name.get(filename)
            if found is None:
                problems.append(f"{MANIFEST} lists {filename}, which is not packaged")
            elif found.checksum != listed_checksum:
                problems.append(
                    f"{filename} no longer matches its checksum in {MANIFEST}: a released "
                    "migration is never edited — add a new one"
                )
        listed_names = [filename for filename, _ in listed]
        for filename in by_name:
            if filename not in listed_names:
                problems.append(
                    f"{filename} is not in {MANIFEST} (run: python -m service.migrations manifest)"
                )
        if listed_names != sorted(listed_names) or len(set(listed_names)) != len(listed_names):
            problems.append(f"{MANIFEST} must list each file once, in order")

    if problems:
        raise MigrationPackageError("; ".join(problems))
    return tuple(sorted(migrations, key=lambda m: m.version))


def plan(packaged: Sequence[Migration], applied: Sequence[AppliedMigration]) -> Plan:
    """What to apply — or why this database and this build cannot be reconciled."""
    by_version = {m.version: m for m in packaged}
    problems: list[str] = []
    ahead: list[int] = []
    for row in sorted(applied, key=lambda r: r.version):
        known = by_version.get(row.version)
        if known is None:
            ahead.append(row.version)
        elif row.name != known.name:
            problems.append(
                f"version {row.version:04d} was applied as '{row.name}', "
                f"but this build packages it as '{known.name}'"
            )
        elif row.checksum != known.checksum:
            problems.append(f"{known.filename} changed after it was applied")

    applied_versions = {row.version for row in applied}
    newest_applied = max(applied_versions, default=0)
    pending = tuple(m for m in packaged if m.version not in applied_versions)
    for migration in pending:
        if migration.version < newest_applied:
            problems.append(
                f"{migration.filename} is unapplied, below version {newest_applied:04d} "
                "which already is: history was reordered"
            )
    if problems:
        raise MigrationDriftError("; ".join(problems))
    return Plan(pending=pending, ahead=tuple(ahead))


# ── The database ─────────────────────────────────────────────────────────────

Connect = Callable[[], AbstractContextManager[Any]]


def migrations_dsn() -> str:
    """The owner's DSN when one is configured apart from the runtime's
    (`MIGRATIONS_DATABASE_URL`, the seam for least-privilege roles), else the
    service's own."""
    return os.environ.get("MIGRATIONS_DATABASE_URL") or default_dsn()


def _connector(dsn: str | None) -> Connect:
    def connect() -> AbstractContextManager[Any]:
        import psycopg

        # autocommit: the session advisory lock must outlive each migration's
        # transaction, and `conn.transaction()` then issues a real BEGIN/COMMIT.
        return psycopg.connect(
            dsn or migrations_dsn(), autocommit=True, connect_timeout=CONNECT_TIMEOUT_S
        )

    return connect


def _read_ledger(conn: Any) -> list[AppliedMigration]:
    row = conn.execute("SELECT to_regclass('app.schema_migrations') IS NOT NULL").fetchone()
    if not row or not row[0]:
        return []
    rows = conn.execute("SELECT version, name, checksum FROM app.schema_migrations ORDER BY version").fetchall()
    return [AppliedMigration(int(version), str(name), str(digest)) for version, name, digest in rows]


def _acquire_lock(
    conn: Any, *, wait_s: float, sleep: Callable[[float], None], clock: Callable[[], float]
) -> None:
    deadline = clock() + wait_s
    while True:
        row = conn.execute("SELECT pg_try_advisory_lock(%s, 0)", (int(AdvisoryLock.MIGRATIONS),)).fetchone()
        if row and row[0]:
            return
        if clock() >= deadline:
            raise MigrationLockTimeout(
                f"another instance has held the migration lock for {wait_s:.0f} s"
            )
        sleep(LOCK_POLL_S)


def _release_lock(conn: Any) -> None:
    """Best effort: a session lock dies with its connection anyway, and a failure
    here must not replace the error that is already on its way out."""
    try:
        conn.execute("SELECT pg_advisory_unlock(%s, 0)", (int(AdvisoryLock.MIGRATIONS),))
    except Exception as exc:
        log.warning("migrations: could not release the lock (%s)", type(exc).__name__)


def _describe(exc: BaseException) -> str:
    """SQLSTATE and primary message. Never DETAIL (row values), never `str(exc)`."""
    sqlstate = getattr(exc, "sqlstate", None)
    primary = getattr(getattr(exc, "diag", None), "message_primary", None)
    parts = [type(exc).__name__]
    if sqlstate:
        parts.append(f"SQLSTATE {sqlstate}")
    if primary:
        parts.append(str(primary))
    return ", ".join(parts)


def _apply(conn: Any, migration: Migration, *, applied_by: str, clock: Callable[[], float]) -> None:
    started = clock()
    failure: str | None = None
    try:
        with conn.transaction():
            conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{LOCK_TIMEOUT_S}s",))
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{STATEMENT_TIMEOUT_S}s",))
            conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO app.schema_migrations (version, name, checksum, duration_ms, applied_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    max(0, round((clock() - started) * 1000)),
                    applied_by,
                ),
            )
    except Exception as exc:
        failure = f"{migration.filename} failed and was rolled back ({_describe(exc)})"
    if failure:  # outside the except block, so the driver's exception is not chained
        raise MigrationFailed(failure)
    log.info("migrations: applied %s in %d ms", migration.filename, round((clock() - started) * 1000))


def migrate(
    dsn: str | None = None,
    *,
    mode: Mode = Mode.APPLY,
    connect: Connect | None = None,
    packaged: Sequence[Migration] | None = None,
    lock_wait_s: float = LOCK_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    """Bring the database up to this build's schema, or say exactly why not.

    Raises `MigrationError` for anything deterministic (fatal at startup) and
    lets the driver's own connection errors through: an unreachable database is
    an outage, which the caller already knows how to degrade around.
    """
    migrations = tuple(packaged) if packaged is not None else discover()
    applied_by = (os.environ.get("K_REVISION") or "local")[:200]

    with (connect or _connector(dsn))() as conn:
        todo = plan(migrations, _read_ledger(conn))
        if todo.pending and mode is Mode.VERIFY:
            raise MigrationsPending(
                "this build needs " + ", ".join(m.filename for m in todo.pending)
                + " — run `python -m service.migrations migrate` as the schema owner"
            )
        applied: list[str] = []
        if todo.pending:
            _acquire_lock(conn, wait_s=lock_wait_s, sleep=sleep, clock=clock)
            try:
                conn.execute(_LEDGER_DDL)
                # Again, under the lock: whoever held it has probably done the work.
                todo = plan(migrations, _read_ledger(conn))
                for migration in todo.pending:
                    _apply(conn, migration, applied_by=applied_by, clock=clock)
                    applied.append(migration.filename)
            finally:
                _release_lock(conn)
        if todo.ahead:
            log.warning(
                "migrations: the database is ahead of this build (versions %s); "
                "running on the expand/contract guarantee",
                ", ".join(f"{v:04d}" for v in todo.ahead),
            )
        newest = max((m.version for m in migrations), default=0)
        return Report(applied=tuple(applied), current=max([newest, *todo.ahead]), ahead=todo.ahead)


def status(dsn: str | None = None, *, connect: Connect | None = None) -> list[tuple[str, str]]:
    """`(filename or version, state)` rows, for the operator. Read-only."""
    migrations = discover()
    with (connect or _connector(dsn))() as conn:
        ledger = _read_ledger(conn)
    todo = plan(migrations, ledger)
    pending = {m.version for m in todo.pending}
    rows = [(m.filename, "pending" if m.version in pending else "applied") for m in migrations]
    rows.extend((f"{version:04d}", "applied by a newer build") for version in todo.ahead)
    return rows


# ── CLI ──────────────────────────────────────────────────────────────────────


def append_to_manifest(directory: Path) -> list[str]:
    """Add files the manifest does not list yet. Existing lines are never
    rewritten — changing a released checksum has to be done by hand, in a diff a
    reviewer will see."""
    manifest = directory / MANIFEST
    text = normalise(manifest.read_bytes()) if manifest.exists() else ""
    listed = {filename for filename, _ in read_manifest(text)}
    migrations, problems = _read_files(directory)
    if problems:
        raise MigrationPackageError("; ".join(problems))
    added = [m for m in sorted(migrations, key=lambda m: m.version) if m.filename not in listed]
    if added:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "".join(f"{m.filename} {m.checksum}\n" for m in added)
        manifest.write_text(text, encoding="utf-8", newline="\n")
    return [m.filename for m in added]


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when the answer is "fine", 1 when migrations are pending, 2 on error."""
    parser = argparse.ArgumentParser(prog="python -m service.migrations")
    parser.add_argument("command", choices=["status", "migrate", "verify", "manifest"])
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            print(f"{len(discover())} migrations match {MANIFEST}")
            return 0
        if args.command == "manifest":
            added = append_to_manifest(Path(__file__).resolve().parent / "sql" / "migrations")
            print("\n".join(f"added {name}" for name in added) if added else "manifest already complete")
            return 0
        if args.command == "migrate":
            report = migrate()
            print("\n".join(f"applied {name}" for name in report.applied) if report.applied else "nothing to apply")
            return 0
        rows = status()
        print("\n".join(f"{state:<26}{name}" for name, state in rows))
        return 1 if any(state == "pending" for _, state in rows) else 0
    except MigrationError as exc:
        print(f"error: {exc}")
        return 2
    except Exception as exc:  # a connection failure: say so without the DSN or the driver's text
        print(f"error: could not reach the database ({type(exc).__name__})")
        return 2


if __name__ == "__main__":  # pragma: no cover - thin entry point
    raise SystemExit(main())

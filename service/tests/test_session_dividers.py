"""Session dividers without a database (1kg.3.5): the pure rules, the twin's
failure path, the migration's text and the application's wiring.

The behaviour shared by both worlds is `tests/test_session_dividers_db.py`'s.
Nothing here reads DATABASE_URL.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import TypeAdapter

from service import app as appmod
from service import session_divider_store, session_dividers
from service.audit_log import InMemoryAuditLog
from service.campaign_store import InMemoryCampaignStore
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase, PgTransaction
from service.history import InMemoryMessageStore
from service.jobs import InMemoryJobQueue, JobRunner
from service.reconciliation import enqueue_reconciliation
from service.session_divider_store import InMemorySessionDividerStore
from service.session_dividers import DIVIDER_KIND, SessionDividers, divider_entry, enqueuer
from service.table_session_store import InMemoryTableSessionStore, no_slots
from service.table_sessions import TableSessions
from service.timeline_store import EntryInvalid, InMemoryTimelineStore, PostgresTimelineStore, new_entry_id
from service.workbench_contracts import SessionBoundary, TimelineEntry

ROOT = Path(__file__).resolve().parents[2]
#: The migration's number appears in Python exactly once, here. The lead
#: renumbers at merge if a parallel bead takes it first.
MIGRATION_FILENAME = "0017_session_divider_identity.sql"
MIGRATION = ROOT / "service" / "sql" / "migrations" / MIGRATION_FILENAME
PINNED_INDEX = (
    "CREATE UNIQUE INDEX timeline_entries_divider_uidx "
    "ON chat.timeline_entries (conversation_id, (payload->>'session_id'), (payload->>'boundary')) "
    "WHERE entry_kind = 'session_divider';"
)
SESSION = "ses_" + "a" * 22


def _statements(sql: str) -> str:
    """The file without its comments, on one line."""
    return " ".join(" ".join(line.split("--", 1)[0].split()) for line in sql.splitlines()).strip()


# ── The one writer of a divider ──────────────────────────────────────────────


class _RecordingConnection:
    """A PostgreSQL connection that records every statement and answers none."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, params: Any = None) -> Any:
        self.statements.append(statement)
        raise AssertionError("no statement may run for a refused entry")


def test_append_refuses_a_session_divider_entry() -> None:
    """MA-30 (I-6): `append` guards the owner alone, so it refuses a divider by
    name before any statement, in the twin and in PostgreSQL alike."""
    at = datetime.now(UTC)
    entry = divider_entry(entry_id=new_entry_id(), session_id=SESSION, boundary=SessionBoundary.START, at=at)
    db, messages = InMemoryDatabase(), InMemoryMessageStore()
    messages.claim_conversation("cnv-one", 1)
    with db.transaction() as unit, pytest.raises(EntryInvalid) as twin:
        InMemoryTimelineStore(db, messages=messages).append(unit, "cnv-one", entry, at, owner_id=1)
    connection = _RecordingConnection()
    with pytest.raises(EntryInvalid) as postgres:
        PostgresTimelineStore().append(PgTransaction(connection), "cnv-one", entry, at, owner_id=1)
    assert twin.value.fields == postgres.value.fields == ["entry_kind"]
    assert connection.statements == []


def test_a_divider_entry_carries_its_six_keys_and_nothing_else() -> None:
    """MA-31 (SEC-4, ED-26): two identifiers, a time and the envelope; it
    validates as a timeline entry."""
    at = datetime.now(UTC)
    entry = divider_entry(entry_id=new_entry_id(), session_id=SESSION, boundary=SessionBoundary.END, at=at)
    dumped = entry.model_dump(mode="json")
    assert set(dumped) == {"schema_version", "entry_kind", "entry_id", "created_at", "session_id", "boundary"}
    assert (dumped["entry_kind"], dumped["session_id"], dumped["boundary"]) == ("session_divider", SESSION, "end")
    assert TypeAdapter(TimelineEntry).validate_python(dumped) == entry


# ── The migration, pinned against the Python that relies on it ───────────────


def test_the_migration_is_one_partial_unique_index_and_no_transaction() -> None:
    """MA-32: exactly one partial unique index over the two identifiers, no
    transaction control, not CONCURRENTLY."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert _statements(sql) == PINNED_INDEX
    body = _statements(sql).upper()
    for word in ("BEGIN", "COMMIT", "CONCURRENTLY", "ROLLBACK"):
        assert not re.search(rf"\b{word}\b", body), word
    assert f"{MIGRATION_FILENAME} " in (MIGRATION.parent / "manifest.txt").read_text(encoding="utf-8")


def test_the_conflict_target_names_the_migrations_index_expressions() -> None:
    """MA-33: the insert's `ON CONFLICT` target and the index list the same
    expressions and predicate, read from both texts — PostgreSQL refuses a
    target that matches no index, and absorbs every violation without one."""
    index = re.search(r"ON chat\.timeline_entries (\(.*\)) (WHERE .*);", _statements(MIGRATION.read_text("utf-8")))
    assert index is not None
    insert = session_divider_store._INSERT_DIVIDER
    target = re.search(r"ON CONFLICT (\(.*\)) (WHERE .*) DO NOTHING", insert)
    assert target is not None
    assert (target.group(1), target.group(2)) == (index.group(1), index.group(2))
    assert session_divider_store.DIVIDER_CONFLICT_TARGET == f"{index.group(1)} {index.group(2)}"


# ── The application's wiring ─────────────────────────────────────────────────


@contextmanager
def _built() -> Iterator[dict[str, Any]]:
    saved = dict(appmod._state)
    try:
        appmod._state.clear()
        appmod._build_stores(appmod.Database("postgresql://nobody@127.0.0.1:1/none"))
        yield appmod._state
    finally:
        appmod._state.clear()
        appmod._state.update(saved)


def test_the_app_hands_its_table_sessions_the_divider_enqueuer() -> None:
    """MA-36: the lifecycle `_build_stores` composes enqueues divider jobs."""
    with _built() as state:
        lifecycle = state["table_sessions"]
        assert isinstance(lifecycle, TableSessions)
        assert lifecycle._dividers is not None
        assert lifecycle._dividers.__qualname__ == "enqueuer.<locals>.enqueue"
        assert state["jobs"].runner._handlers[DIVIDER_KIND].max_attempts is None


def _constructions() -> list[tuple[str, int, set[str]]]:
    """Every `TableSessions(...)` call outside the tests: file, line, keywords."""
    found = []
    for path in sorted((ROOT / "service").rglob("*.py")):
        if "tests" in path.relative_to(ROOT / "service").parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name == "TableSessions":
                found.append((path.name, node.lineno, {k.arg for k in node.keywords if k.arg}))
    return found


def test_every_production_table_sessions_construction_passes_dividers() -> None:
    """MA-40, the critic's item 12: `dividers=None` is for tests only, so every
    construction under `service/` passes it by name — a second one (2.3 PR-B's
    routes, say) cannot silently write no divider."""
    found = _constructions()
    assert found, "no construction found; this test would pass vacuously"
    for file, line, keywords in found:
        assert "dividers" in keywords, f"{file}:{line} builds TableSessions without dividers="


def test_the_divider_kind_is_named_in_one_module() -> None:
    """The kind is a string in one module's code, and the lifecycle never
    imports the modules that write dividers (I-1's import boundary)."""
    for path in sorted((ROOT / "service").glob("*.py")):
        if path.name != "session_dividers.py":
            tree = ast.parse(path.read_text(encoding="utf-8"))
            assert not any(
                isinstance(node, ast.Constant) and node.value == DIVIDER_KIND for node in ast.walk(tree)
            ), path.name
    lifecycle = ast.parse((ROOT / "service" / "table_sessions.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(lifecycle) if isinstance(node, ast.ImportFrom)}
    assert imported, "no import found; this test would pass vacuously"
    assert not imported & {"session_dividers", "session_divider_store", "timeline_store"}, imported


# ── The handler's failure path, in the twin ──────────────────────────────────


class _FailsOnce:
    """The twin divider store, whose first insert fails as a dropped connection
    does."""

    def __init__(self, inner: InMemorySessionDividerStore) -> None:
        self._inner, self.failures = inner, 0

    def divider_targets(self, unit: Any, **kwargs: Any) -> list[str]:
        return self._inner.divider_targets(unit, **kwargs)

    def append_divider(self, unit: Any, conversation_id: str, entry: Any, **kwargs: Any) -> bool:
        if self.failures == 0:
            self.failures += 1
            raise psycopg.OperationalError("the connection went away")
        return self._inner.append_divider(unit, conversation_id, entry, **kwargs)


def test_a_database_error_fails_the_job_and_a_rerun_completes_it() -> None:
    """MA-39, the critic's item 11: a database error propagates, so the runner
    fails and keeps the job; the next run stores every divider."""
    db = InMemoryDatabase()
    campaigns, sessions = InMemoryCampaignStore(db), InMemoryTableSessionStore(db, slot_clear=no_slots)
    conversations, jobs = InMemoryConversationStore(db), InMemoryJobQueue(db=db)
    with db.transaction() as unit:
        campaign = campaigns.create(unit, owner_id=1, name="Nocturne").id
        threads = [
            conversations.create(
                unit, owner_id=1, campaign_id=campaign, title=None, started_mode=None,
                now=datetime.now(UTC) - timedelta(hours=h),
            ).id
            for h in (1, 2)
        ]
    lifecycle = TableSessions(
        db, campaigns=campaigns, sessions=sessions, audit=InMemoryAuditLog(), jobs=jobs,
        reconcile=lambda unit, campaign_id: enqueue_reconciliation(unit, jobs, campaign_id),
        dividers=enqueuer(jobs),
    )
    lifecycle.start(1, campaign, command_id="a-start-command-0001")
    store = _FailsOnce(InMemorySessionDividerStore(db))
    clock = [datetime.now(UTC) + timedelta(minutes=1)]
    handler = SessionDividers(db, sessions=sessions, store=store).handler()
    runner = JobRunner(jobs, {DIVIDER_KIND: handler}, clock=lambda: clock[0])
    first = runner.run_due(limit=5)
    assert (first.ran, first.failed) == (1, 1)
    [kept] = [row for row in jobs._rows.values() if row.job.kind == DIVIDER_KIND]
    assert kept.job.attempts == 1 and kept.dead_at is None
    clock[0] += timedelta(hours=2)
    second = runner.run_due(limit=5)
    assert (second.ran, second.failed) == (1, 0)
    assert not [row for row in jobs._rows.values() if row.job.kind == DIVIDER_KIND]
    timeline = InMemoryTimelineStore(db, messages=InMemoryMessageStore())
    with db.transaction() as unit:
        for thread in threads:
            [row] = timeline.entry_window(unit, thread, before=None, limit=10)
            assert (row.payload["entry_kind"], row.payload["boundary"]) == ("session_divider", "start")


def test_the_enqueuer_refuses_an_id_that_is_not_a_session_and_writes_plain_strings() -> None:
    """The critic's item 10: the payload's boundary is a plain `str` in the
    twin as in PostgreSQL, and a malformed session id never reaches the outbox."""
    db = InMemoryDatabase()
    jobs = InMemoryJobQueue(db=db)
    enqueue = enqueuer(jobs)
    with db.transaction() as unit:
        enqueue(unit, SESSION, SessionBoundary.END)
    [row] = jobs._rows.values()
    assert dict(row.job.payload) == {"session_id": SESSION, "boundary": "end"}
    assert type(row.job.payload["boundary"]) is str and row.dedupe_key is None
    with pytest.raises(ValueError), db.transaction() as unit:
        enqueue(unit, "cmp_" + "a" * 22, SessionBoundary.START)
    assert len(jobs._rows) == 1
    assert session_dividers.DIVIDER_FANOUT_MAX == 100

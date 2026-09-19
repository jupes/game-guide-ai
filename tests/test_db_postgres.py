"""
The bounded pools, the transaction boundary and the job outbox against a real
PostgreSQL (1kg.1.5).

The in-memory twins are held to the same contracts in `service/tests/test_db.py`
and `service/tests/test_jobs.py`; this is where the real thing earns them: a pool
that really refuses one connection too many, a dead connection replaced on
checkout, an aggregate and its job that really commit or vanish together, a
notification that really waits for the commit, and claims that really skip a
locked row.

Requires DATABASE_URL (CI always sets it). Run from the repo root:
    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_db_postgres.py -q
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database
from psycopg_pool import PoolTimeout

from service import migrations as mig
from service.db import AdvisoryLock, Database, PoolSettings
from service.jobs import LEASE_SECONDS, Job, JobHandler, JobRunner, PostgresJobQueue

pytestmark = needs_db


@pytest.fixture
def dsn():
    with throwaway_database("db") as target:
        mig.migrate(target)
        with connect(target) as conn:
            conn.execute("CREATE TABLE app.things (id TEXT PRIMARY KEY)")
        yield target


@pytest.fixture
def db(dsn):
    database = Database(dsn, PoolSettings(sync_max=2, async_max=2, acquire_timeout_s=1))
    database.open()
    yield database
    database.close()


def _count(dsn: str, sql: str, params: tuple = ()) -> int:
    with connect(dsn) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def _pooled_sessions(dsn: str) -> int:
    return _count(
        dsn,
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() "
        "AND application_name IN ('game-guide-ai:sync', 'game-guide-ai:async', 'game-guide-ai:direct')",
    )


# ── The pool is bounded, heals, and closes ───────────────────────────────────


def test_the_pool_starts_empty_and_never_exceeds_its_bound(db, dsn):
    assert _pooled_sessions(dsn) == 0, "no idle minimum: an unused instance holds nothing"
    with db.connection() as first, db.connection() as second:
        assert first.execute("SELECT 1").fetchone() == (1,)
        assert second.execute("SELECT 2").fetchone() == (2,)
        started = time.monotonic()
        with pytest.raises(PoolTimeout):
            with db.connection():
                pass  # pragma: no cover
        assert 0.5 < time.monotonic() - started < 5, "a request waits its second, then fails as unavailable"
        assert _pooled_sessions(dsn) == 2
    assert isinstance(PoolTimeout("x"), psycopg.OperationalError), "which the routes already map to a 503"


def test_a_connection_that_died_while_idle_is_replaced_on_checkout(db, dsn):
    """A Cloud Run instance is frozen between requests; its sockets die quietly."""
    with db.connection() as conn:
        dead_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    with connect(dsn) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (dead_pid,))
    time.sleep(0.2)
    with db.connection() as conn:
        assert conn.execute("SELECT pg_backend_pid()").fetchone()[0] != dead_pid


def test_an_instance_that_thaws_with_every_connection_dead_still_answers_at_once(dsn, monkeypatch):
    """Met one by one at checkout, dead connections cost a doubling backoff and a
    handful of them cost the request. A pool that sat idle is swept first."""
    from service import db as dbmod

    database = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=3))
    database.open()
    try:
        with database.connection() as a, database.connection() as b, database.connection() as c:
            pids = [conn.execute("SELECT pg_backend_pid()").fetchone()[0] for conn in (a, b, c)]
        with connect(dsn) as admin:
            for pid in pids:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        time.sleep(0.2)
        monkeypatch.setattr(dbmod, "IDLE_SUSPECT_S", 0.0)
        started = time.monotonic()
        with database.connection() as conn:
            assert conn.execute("SELECT pg_backend_pid()").fetchone()[0] not in pids
        assert time.monotonic() - started < 2
    finally:
        database.close()


def test_a_clean_exit_commits_and_an_exception_rolls_back(db, dsn):
    with db.connection() as conn:
        conn.execute("INSERT INTO app.things VALUES ('kept')")
    with pytest.raises(RuntimeError):
        with db.connection() as conn:
            conn.execute("INSERT INTO app.things VALUES ('lost')")
            raise RuntimeError("boom")
    assert _count(dsn, "SELECT count(*) FROM app.things") == 1


def test_closing_the_pool_leaves_no_session_behind(dsn):
    database = Database(dsn, PoolSettings(sync_max=3, async_max=0))
    database.open()
    with database.connection() as a, database.connection() as b:
        a.execute("SELECT 1")
        b.execute("SELECT 1")
    assert _pooled_sessions(dsn) == 2
    database.close()
    deadline = time.monotonic() + 5
    while _pooled_sessions(dsn) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _pooled_sessions(dsn) == 0


def test_without_a_pool_every_operation_has_a_connection_of_its_own(dsn):
    database = Database(dsn, PoolSettings(sync_max=0, async_max=0))
    with database.connection() as conn:
        conn.execute("INSERT INTO app.things VALUES ('direct')")
    assert _pooled_sessions(dsn) == 0 and _count(dsn, "SELECT count(*) FROM app.things") == 1


def test_the_async_pool_serves_the_realtime_path_and_closes(dsn):
    async def scenario() -> int:
        database = Database(dsn, PoolSettings(sync_max=1, async_max=2, acquire_timeout_s=2))
        try:
            async with database.async_connection() as conn:
                cursor = await conn.execute("SELECT 41 + 1")
                return (await cursor.fetchone())[0]
        finally:
            await database.aclose()

    # psycopg's async connections need a selector loop, which is not Windows' default.
    assert asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop) == 42


# ── Aggregate plus outbox, atomically ────────────────────────────────────────


def test_an_aggregate_and_its_job_commit_together(db, dsn):
    queue = PostgresJobQueue(db)
    with db.transaction() as unit:
        unit.conn.execute("INSERT INTO app.things VALUES ('asset-1')")
        job_id = queue.enqueue(unit, "asset.delete", {"asset_id": "asset-1", "generation": 2})
        assert _count(dsn, "SELECT count(*) FROM app.jobs") == 0, "invisible until the commit"
    assert _count(dsn, "SELECT count(*) FROM app.things") == 1
    (job,) = queue.claim(["asset.delete"])
    assert (job.id, job.kind, dict(job.payload), job.attempts) == (
        job_id, "asset.delete", {"asset_id": "asset-1", "generation": 2}, 1,
    )


def test_an_aggregate_and_its_job_roll_back_together(db, dsn):
    queue = PostgresJobQueue(db)
    with pytest.raises(RuntimeError):
        with db.transaction() as unit:
            unit.conn.execute("INSERT INTO app.things VALUES ('asset-1')")
            queue.enqueue(unit, "asset.delete", {"asset_id": "asset-1"})
            raise RuntimeError("the tombstone could not be written")
    assert _count(dsn, "SELECT count(*) FROM app.things") == 0
    assert _count(dsn, "SELECT count(*) FROM app.jobs") == 0


def test_after_commit_callbacks_run_once_the_connection_is_back(dsn):
    """With a pool of one, a callback that needs the database would deadlock if
    it ran while the transaction still held the only connection."""
    database = Database(dsn, PoolSettings(sync_max=1, async_max=0, acquire_timeout_s=2))
    database.open()
    try:
        queue = PostgresJobQueue(database)
        ran: list[str] = []
        handler = JobHandler(lambda job: ran.append(str(job.payload["asset_id"])))
        runner = JobRunner(queue, {"asset.delete": handler})
        with database.transaction() as unit:
            runner.run_after_commit(unit, queue.enqueue(unit, "asset.delete", {"asset_id": "asset-1"}))
            assert ran == []
        assert ran == ["asset-1"]
        assert _count(dsn, "SELECT count(*) FROM app.jobs") == 0, "a finished job is deleted"
    finally:
        database.close()


def test_a_notification_waits_for_the_commit_and_dies_with_a_rollback(db, dsn):
    with connect(dsn) as listener:
        listener.execute("LISTEN table_session")
        with pytest.raises(RuntimeError):
            with db.transaction() as unit:
                unit.notify("table_session", "rolled-back")
                raise RuntimeError("boom")
        with db.transaction() as unit:
            unit.notify("table_session", "session-7")
            assert list(listener.notifies(timeout=0.3)) == [], "held until the commit"
        received = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
    assert received == [("table_session", "session-7")]


def test_a_transaction_lock_serialises_two_writers_on_the_same_key(db):
    """RT-8's bound check: two instances must not both admit the last seat."""
    order: list[str] = []
    holding = threading.Event()
    release = threading.Event()

    def first() -> None:
        with db.transaction() as unit:
            unit.lock(AdvisoryLock.TABLE_SESSION, "session-7")
            order.append("first-in")
            holding.set()
            release.wait(10)
            order.append("first-out")

    def second() -> None:
        holding.wait(10)
        with db.transaction() as unit:
            unit.lock(AdvisoryLock.TABLE_SESSION, "session-7")
            order.append("second-in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    holding.wait(10)
    time.sleep(0.5)  # give the second writer every chance to barge in
    assert order == ["first-in"]
    release.set()
    for thread in threads:
        thread.join(10)
    assert order == ["first-in", "first-out", "second-in"]


# ── The outbox: leases, fencing, dedupe ──────────────────────────────────────

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _enqueue(db: Database, queue: PostgresJobQueue, kind: str = "asset.delete", **kwargs) -> int:
    with db.transaction() as unit:
        return queue.enqueue(unit, kind, {"asset_id": "a-1"}, now=T0, **kwargs)


def test_a_claim_skips_a_row_another_instance_is_claiming(db, dsn):
    """FOR UPDATE SKIP LOCKED: the second claimer takes the next job instead of
    waiting for, or double-running, the first."""
    queue = PostgresJobQueue(db)
    first, second = _enqueue(db, queue), _enqueue(db, queue)
    with connect(dsn, autocommit=False) as other_instance:
        other_instance.execute("SELECT id FROM app.jobs WHERE id = %s FOR UPDATE", (first,))
        started = time.monotonic()
        assert [job.id for job in queue.claim(["asset.delete"], limit=5, now=T0)] == [second]
        assert time.monotonic() - started < 3, "skipped, not waited for"
        other_instance.rollback()
    assert [job.id for job in queue.claim(["asset.delete"], limit=5, now=T0)] == [first]


def test_due_order_kinds_and_claim_by_id(db):
    queue = PostgresJobQueue(db)
    later = _enqueue(db, queue, run_after=T0 + timedelta(minutes=5))
    sooner = _enqueue(db, queue, run_after=T0 + timedelta(minutes=1))
    other = _enqueue(db, queue, kind="upload.sweep")

    assert queue.claim(["asset.delete"], limit=5, now=T0) == []
    assert [job.id for job in queue.claim(["upload.sweep"], now=T0)] == [other]
    in_ten = T0 + timedelta(minutes=10)
    assert [job.id for job in queue.claim(["asset.delete"], job_id=later, now=in_ten)] == [later]
    assert [job.id for job in queue.claim(["asset.delete"], limit=5, now=in_ten)] == [sooner]


def test_an_expired_lease_is_claimed_again_and_fences_the_stale_worker(db, dsn):
    queue = PostgresJobQueue(db)
    _enqueue(db, queue)
    (stale,) = queue.claim(["asset.delete"], now=T0)
    assert queue.claim(["asset.delete"], now=T0 + timedelta(seconds=LEASE_SECONDS)) == []

    after = T0 + timedelta(seconds=LEASE_SECONDS + 1)
    (fresh,) = queue.claim(["asset.delete"], now=after)
    assert (fresh.id, fresh.attempts) == (stale.id, 2)

    queue.fail(stale, error="TimeoutError", retry_at=after, now=after)
    assert queue.claim(["asset.delete"], now=after) == [], "the stale worker released nothing"

    queue.fail(fresh, error="x" * 500, retry_at=after + timedelta(seconds=10), now=after)
    with connect(dsn) as conn:
        row = conn.execute("SELECT attempts, last_error, locked_until, dead_at, run_after FROM app.jobs").fetchone()
    assert row[0] == 2 and row[1] == "x" * 200 and row[2] is None and row[3] is None
    assert row[4] == after + timedelta(seconds=10)


def test_a_live_job_absorbs_a_duplicate_and_a_dead_one_frees_its_key(db, dsn):
    queue = PostgresJobQueue(db)
    first = _enqueue(db, queue, dedupe_key="asset:a-1")
    assert _enqueue(db, queue, dedupe_key="asset:a-1") == first
    assert _enqueue(db, queue, kind="upload.sweep", dedupe_key="asset:a-1") != first
    assert _count(dsn, "SELECT count(*) FROM app.jobs") == 2

    (job,) = queue.claim(["asset.delete"], now=T0)
    queue.fail(job, error="Boom", retry_at=None, now=T0)
    assert _count(dsn, "SELECT count(*) FROM app.jobs WHERE dead_at IS NOT NULL") == 1
    assert queue.claim(["asset.delete"], limit=5, now=T0 + timedelta(days=1)) == [], "a dead job is never claimed"
    assert _enqueue(db, queue, dedupe_key="asset:a-1") != first


def test_the_runner_retries_with_backoff_on_the_real_queue(db, dsn):
    queue = PostgresJobQueue(db)
    _enqueue(db, queue)
    clock = [T0]
    attempts: list[int] = []

    def handler(job: Job) -> None:
        attempts.append(job.attempts)
        if job.attempts < 3:
            raise TimeoutError("gs://bucket/secret-name")

    runner = JobRunner(queue, {"asset.delete": JobHandler(handler)}, clock=lambda: clock[0])
    assert runner.run_due() == 1 and runner.run_due() == 0, "backed off"
    clock[0] += timedelta(seconds=5)
    assert runner.run_due() == 1
    assert _count(dsn, "SELECT count(*) FROM app.jobs WHERE last_error = 'TimeoutError'") == 1
    clock[0] += timedelta(seconds=10)
    assert runner.run_due() == 1
    assert attempts == [1, 2, 3] and _count(dsn, "SELECT count(*) FROM app.jobs") == 0


def test_the_database_refuses_a_payload_that_is_not_an_object(dsn):
    with connect(dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO app.jobs (kind, payload) VALUES ('k', '[1, 2]'::jsonb)")

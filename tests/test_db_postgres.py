"""
The bounded pools, the transaction boundary and the job outbox against a real
PostgreSQL (1kg.1.5).

The in-memory twins are held to the same contracts in `service/tests/test_db.py`
and `service/tests/test_jobs.py`; this is where the real thing earns them: a pool
that really refuses one connection too many and holds none between operations,
a realtime pool that replaces a dead connection and closes cleanly, an
aggregate and its job that really commit or vanish together, a
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
from psycopg_pool import PoolClosed, PoolTimeout

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
    yield database
    database.close()


def _count(dsn: str, sql: str, params: tuple = ()) -> int:
    with connect(dsn) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def _sessions(dsn: str, role: str = "sync") -> int:
    """Sessions the service holds right now, by the label it gives them."""
    return _count(
        dsn,
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = %s",
        (f"game-guide-ai:{role}",),
    )


# ── The gate is bounded and holds nothing; the realtime pool heals and closes ──


def test_the_gate_never_exceeds_its_bound_and_holds_nothing_between_operations(db, dsn):
    """The property the whole design rests on: an instance that is idle — or frozen
    by Cloud Run, or belongs to the revision a rollout just replaced — holds no
    share of the server's twenty-two connections."""
    assert _sessions(dsn) == 0
    with db.connection() as first, db.connection() as second:
        assert first.execute("SELECT 1").fetchone() == (1,)
        assert second.execute("SELECT 2").fetchone() == (2,)
        assert _sessions(dsn) == 2
        started = time.monotonic()
        with pytest.raises(PoolTimeout):
            with db.connection():
                pass  # pragma: no cover
        assert 0.5 < time.monotonic() - started < 5, "a request waits its second, then fails as unavailable"
    assert _sessions(dsn) == 0, "nothing is kept once the operations end"
    assert isinstance(PoolTimeout("x"), psycopg.OperationalError), "which the routes already map to a 503"


def test_a_clean_exit_commits_and_an_exception_rolls_back(db, dsn):
    with db.connection() as conn:
        conn.execute("INSERT INTO app.things VALUES ('kept')")
    with pytest.raises(RuntimeError):
        with db.connection() as conn:
            conn.execute("INSERT INTO app.things VALUES ('lost')")
            raise RuntimeError("boom")
    assert _count(dsn, "SELECT count(*) FROM app.things") == 1


def test_a_closed_database_refuses_new_operations(dsn):
    database = Database(dsn, PoolSettings(sync_max=2, async_max=0))
    with database.connection() as conn:
        conn.execute("SELECT 1")
    database.close()
    with pytest.raises(PoolClosed):
        with database.connection():
            pass  # pragma: no cover
    assert _sessions(dsn) == 0


def test_without_a_gate_every_operation_still_has_a_connection_of_its_own(dsn):
    database = Database(dsn, PoolSettings(sync_max=0, async_max=0))
    with database.connection() as conn:
        conn.execute("INSERT INTO app.things VALUES ('direct')")
    assert _sessions(dsn) == 0 and _count(dsn, "SELECT count(*) FROM app.things") == 1


def _run(scenario):
    # psycopg's async connections need a selector loop, which is not Windows' default.
    return asyncio.run(scenario, loop_factory=asyncio.SelectorEventLoop)


def test_the_realtime_pool_serves_heals_asks_to_be_reaped_and_closes(dsn):
    async def scenario() -> dict[str, object]:
        database = Database(dsn, PoolSettings(sync_max=1, async_max=2, acquire_timeout_s=3))
        seen: dict[str, object] = {}
        try:
            async with database.async_connection() as conn:
                seen["answer"] = (await (await conn.execute("SELECT 41 + 1")).fetchone())[0]
                seen["idle_session_timeout"] = (await (await conn.execute("SHOW idle_session_timeout")).fetchone())[0]
                dead_pid = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0]
            seen["held while open"] = _sessions(dsn, "async")
            with connect(dsn) as admin:  # what the server does to a frozen instance's session
                admin.execute("SELECT pg_terminate_backend(%s)", (dead_pid,))
            await asyncio.sleep(0.2)
            async with database.async_connection() as conn:
                seen["replaced"] = (await (await conn.execute("SELECT pg_backend_pid()")).fetchone())[0] != dead_pid
        finally:
            await database.aclose()
        return seen

    seen = _run(scenario())
    assert seen == {"answer": 42, "idle_session_timeout": "2min", "held while open": 1, "replaced": True}
    deadline = time.monotonic() + 5
    while _sessions(dsn, "async") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _sessions(dsn, "async") == 0, "shutdown leaves no session behind"


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
    """With a gate of one, a callback that needs the database would deadlock if
    it ran while the transaction still held the only place."""
    database = Database(dsn, PoolSettings(sync_max=1, async_max=0, acquire_timeout_s=2))
    queue = PostgresJobQueue(database)
    ran: list[str] = []
    handler = JobHandler(lambda job: ran.append(str(job.payload["asset_id"])))
    runner = JobRunner(queue, {"asset.delete": handler})
    with database.transaction() as unit:
        runner.run_after_commit(unit, queue.enqueue(unit, "asset.delete", {"asset_id": "asset-1"}))
        assert ran == []
    assert ran == ["asset-1"]
    assert _count(dsn, "SELECT count(*) FROM app.jobs") == 0, "a finished job is deleted"


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


def test_a_claim_leases_exactly_as_many_jobs_as_it_asked_for(db, dsn):
    """`WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)` may be run more
    than once by the planner, and each rerun skips the rows this statement has
    already locked — so the LIMIT slides and one claim takes the backlog. The
    plan forced below is the one that does it; the materialized CTE is immune."""
    queue = PostgresJobQueue(db)
    ids = [_enqueue(db, queue) for _ in range(4)]
    with connect(dsn) as admin:  # every later connection to this database plans with nested loops
        name = admin.execute("SELECT current_database()").fetchone()[0]
        for setting in ("enable_hashjoin", "enable_mergejoin", "enable_material"):
            admin.execute(f'ALTER DATABASE "{name}" SET {setting} = off')
        admin.execute("ANALYZE app.jobs")
    (claimed,) = queue.claim(["asset.delete"], limit=1, now=T0)
    assert claimed.id == ids[0]
    assert _count(dsn, "SELECT count(*) FROM app.jobs WHERE attempts = 0") == 3
    assert [job.id for job in queue.claim(["asset.delete"], limit=2, now=T0)] == ids[1:3]
    assert _count(dsn, "SELECT count(*) FROM app.jobs WHERE attempts = 0") == 1


def test_a_job_somebody_has_started_absorbs_nothing(db, dsn):
    """Absorbed into a running job, later work would be deleted with it, never
    having run. Once claimed, a job leaves the dedupe index."""
    queue = PostgresJobQueue(db)
    first = _enqueue(db, queue, kind="upload.sweep", dedupe_key="session:S")
    (running,) = queue.claim(["upload.sweep"], now=T0)
    second = _enqueue(db, queue, kind="upload.sweep", dedupe_key="session:S")
    assert second != first
    assert _enqueue(db, queue, kind="upload.sweep", dedupe_key="session:S") == second
    queue.complete(running)
    with connect(dsn) as conn:
        assert conn.execute("SELECT id, attempts FROM app.jobs").fetchall() == [(second, 0)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="W-1: an absorbed enqueue locks nothing, so a concurrent claim can swallow it (1kg.2.7)",
)
def test_an_absorbed_enqueue_is_not_swallowed_by_a_concurrent_claim(db, dsn):
    """W-1, from the independent verification of the eligibility ADR.

    `enqueue()` absorbs into an unstarted job with `ON CONFLICT DO NOTHING` and
    then a plain `SELECT`, which takes no lock on the absorbing row. So between
    the absorb and the enqueuer's commit another instance can claim that job,
    run it and delete it — and the change the enqueuer made in the very same
    transaction is then never processed by anything.

    The fix is for the absorb to hold the row `FOR SHARE`, which `claim()`'s
    `FOR UPDATE SKIP LOCKED` must skip until every absorber has committed.

    The interleaving is driven explicitly by two connections. Nothing here waits
    on a clock.
    """
    queue = PostgresJobQueue(db)
    first = _enqueue(db, queue, kind="upload.sweep", dedupe_key="session:S")

    with db.transaction() as unit:
        # The change the job exists to process, and the absorb, in one transaction.
        unit.conn.execute("INSERT INTO app.things (id) VALUES ('late')")
        absorbed = queue.enqueue(unit, "upload.sweep", {"asset_id": "a-1"}, dedupe_key="session:S", now=T0)
        # A precondition, not the behaviour under test: it must not be the
        # AssertionError this test is marked to expect.
        if absorbed != first:
            raise RuntimeError(f"precondition failed: the enqueue did not absorb ({absorbed} != {first})")

        # A second instance, while the enqueuer's transaction is still open.
        stolen = [job.id for job in queue.claim(["upload.sweep"], now=T0)]

    assert stolen == [], "the absorbing row was claimable before the enqueuer committed"
    assert _count(dsn, "SELECT count(*) FROM app.jobs WHERE id = %s", (first,)) == 1


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

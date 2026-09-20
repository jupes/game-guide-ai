"""The transactional outbox for jobs (1kg.1.5), against the in-memory twin.

The same suite of semantics the Postgres queue is held to in
`tests/test_db_postgres.py`: a job exists exactly when its transaction
committed; a claim is a lease with a fencing token; rows never hold content.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from service.db import Database, InMemoryDatabase, PoolSettings
from service.jobs import (
    LEASE_SECONDS,
    InMemoryJobQueue,
    Job,
    JobHandler,
    JobRunner,
    PostgresJobQueue,
    check_payload,
    retry_delay,
)

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _queue() -> InMemoryJobQueue:
    return InMemoryJobQueue()


def _enqueue(queue: InMemoryJobQueue, kind: str = "asset.delete", **kwargs) -> int:
    with queue.db.transaction() as unit:
        return queue.enqueue(unit, kind, kwargs.pop("payload", {"asset_id": "a-1"}), now=T0, **kwargs)


# ── A job exists exactly when its transaction committed ──────────────────────


def test_an_aggregate_and_its_job_commit_together():
    queue = _queue()
    assets: dict[str, str] = {}
    with queue.db.transaction() as unit:
        assets["a-1"] = "deleted"
        unit.on_rollback(lambda: assets.pop("a-1"))
        job_id = queue.enqueue(unit, "asset.delete", {"asset_id": "a-1"}, now=T0)
    assert assets == {"a-1": "deleted"}
    assert queue.snapshot() == [(job_id, "asset.delete", 0, None, False)]


def test_an_aggregate_and_its_job_roll_back_together():
    queue = _queue()
    assets: dict[str, str] = {}
    with pytest.raises(RuntimeError):
        with queue.db.transaction() as unit:
            assets["a-1"] = "deleted"
            unit.on_rollback(lambda: assets.pop("a-1"))
            queue.enqueue(unit, "asset.delete", {"asset_id": "a-1"}, now=T0)
            raise RuntimeError("the tombstone could not be written")
    assert assets == {} and queue.snapshot() == []


def test_a_job_does_not_exist_for_anyone_else_until_its_transaction_commits():
    """The fake has the real visibility rule. Without it, code that enqueues and
    runs inside one block passes here and finds nothing on PostgreSQL — and a
    rolled-back job, which by the outbox's own contract never existed, would run."""
    queue = _queue()
    ran: list[int] = []
    runner = JobRunner(queue, {"asset.delete": JobHandler(lambda job: ran.append(job.id))}, clock=lambda: T0)
    with pytest.raises(RuntimeError):
        with queue.db.transaction() as unit:
            queue.enqueue(unit, "asset.delete", now=T0)
            assert queue.claim(["asset.delete"], now=T0) == [] and runner.run_due() == 0
            raise RuntimeError("the tombstone could not be written")
    assert ran == [] and queue.snapshot() == [] and runner.run_due() == 0


def test_a_job_can_only_be_enqueued_inside_a_transaction_of_its_own_kind():
    """The signature is the guarantee: there is no way to enqueue without the
    unit of work of the change that needs the job."""
    memory = _queue()
    postgres = PostgresJobQueue(Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=1)))
    with memory.db.transaction() as unit:
        with pytest.raises(TypeError, match="inside a Postgres transaction"):
            postgres.enqueue(unit, "asset.delete")
    with pytest.raises(TypeError, match="inside an in-memory transaction"):
        memory.enqueue(object(), "asset.delete")  # type: ignore[arg-type]
    assert postgres.claim([], limit=5) == [] and postgres.claim(["asset.delete"], limit=0) == []


# ── Rows are content-free (SEC-20) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        ({"brief": "x" * 201}, "too long to be an identifier"),
        ({"fields": {"name": "Seraphine"}}, "must be a string, a whole number, a boolean or null"),
        ({"tags": ["a", "b"]}, "must be a string, a whole number, a boolean or null"),
        ({"ratio": 0.5}, "must be a string, a whole number, a boolean or null"),
        ({"": "a-1"}, "key is a short string"),
        ({"k" * 61: "a-1"}, "key is a short string"),
        ({f"k{i}": i for i in range(21)}, "at most 20 keys"),
    ],
)
def test_a_payload_has_no_room_for_content(payload, problem):
    with pytest.raises(ValueError, match=problem):
        check_payload(payload)


def test_identifiers_are_what_a_payload_is_for():
    payload = {"asset_id": "a-1", "generation": 3, "tmp": True, "parent": None}
    assert check_payload(payload) == payload
    assert check_payload(None) == {}


@pytest.mark.parametrize("kwargs", [{"kind": ""}, {"kind": "k" * 101}, {"dedupe_key": ""}, {"dedupe_key": "d" * 201}])
def test_kinds_and_dedupe_keys_are_bounded(kwargs):
    queue = _queue()
    with pytest.raises(ValueError):
        _enqueue(queue, **kwargs)
    assert queue.snapshot() == []


def test_a_failure_records_the_exception_class_never_its_message(caplog):
    queue = _queue()
    _enqueue(queue)

    def handler(job: Job) -> None:
        raise PermissionError("gs://bucket/campaign-7/The Duke's secret.png")

    runner = JobRunner(queue, {"asset.delete": JobHandler(handler)}, clock=lambda: T0)
    with caplog.at_level(logging.WARNING, logger="service.jobs"):
        assert runner.run_due() == 1
    assert queue.snapshot() == [(1, "asset.delete", 1, "PermissionError", False)]
    assert "asset.delete #1 failed on attempt 1 (PermissionError)" in caplog.text
    assert "secret" not in caplog.text


# ── Deduplication ────────────────────────────────────────────────────────────


def test_a_live_job_absorbs_the_same_work_enqueued_again():
    queue = _queue()
    first = _enqueue(queue, dedupe_key="asset:a-1")
    assert _enqueue(queue, dedupe_key="asset:a-1") == first
    assert _enqueue(queue, kind="asset.sweep", dedupe_key="asset:a-1") != first, "a key is scoped by kind"
    assert _enqueue(queue) != _enqueue(queue), "no key, no deduplication"
    assert len(queue.snapshot()) == 4


def test_an_absorbed_enqueue_is_not_claimable_until_the_absorber_commits():
    """The in-memory twin of the two-connection race in
    `tests/test_db_postgres.py` (W-1).

    An absorbed enqueue holds the row it absorbed into, so a claim from
    elsewhere cannot take that job — and delete this transaction's work along
    with it — before the absorbing transaction has committed. In PostgreSQL the
    hold is `SELECT ... FOR SHARE`, which `claim()`'s `FOR UPDATE SKIP LOCKED`
    skips; here it is the same rule, stated directly."""
    queue = _queue()
    first = _enqueue(queue, kind="upload.sweep", dedupe_key="session:S")

    with queue.db.transaction() as unit:
        absorbed = queue.enqueue(unit, "upload.sweep", {"asset_id": "a-1"}, dedupe_key="session:S", now=T0)
        assert absorbed == first, "precondition: the enqueue absorbs into the unstarted job"
        assert queue.claim(["upload.sweep"], now=T0) == [], "claimable before the absorber committed"

    (claimed,) = queue.claim(["upload.sweep"], now=T0)
    assert claimed.id == first, "and claimable once it has"


def test_a_job_somebody_has_started_absorbs_nothing():
    """Its handler may already have read the state it acts on. Work requested
    after that must get a row of its own — absorbed into the running job, it
    would be deleted with it, never having run."""
    queue = _queue()
    first = _enqueue(queue, kind="upload.sweep", dedupe_key="session:S")
    (running,) = queue.claim(["upload.sweep"], now=T0)

    second = _enqueue(queue, kind="upload.sweep", dedupe_key="session:S")
    assert second != first
    assert _enqueue(queue, kind="upload.sweep", dedupe_key="session:S") == second, "unstarted: absorbs again"

    queue.complete(running)
    assert queue.snapshot() == [(second, "upload.sweep", 0, None, False)], "the later request still runs"


def test_a_transaction_sees_its_own_uncommitted_job():
    queue = _queue()
    with queue.db.transaction() as unit:
        first = queue.enqueue(unit, "asset.delete", dedupe_key="asset:a-1", now=T0)
        assert queue.enqueue(unit, "asset.delete", dedupe_key="asset:a-1", now=T0) == first
    assert len(queue.snapshot()) == 1


def test_a_dead_job_no_longer_holds_its_key():
    queue = _queue()
    first = _enqueue(queue, dedupe_key="asset:a-1")
    (job,) = queue.claim(["asset.delete"], now=T0)
    queue.fail(job, error="Boom", retry_at=None, now=T0)
    assert _enqueue(queue, dedupe_key="asset:a-1") != first


# ── Claims are leases ────────────────────────────────────────────────────────


def test_jobs_are_claimed_when_due_oldest_first_and_only_for_known_kinds():
    queue = _queue()
    later = _enqueue(queue, run_after=T0 + timedelta(minutes=5))
    sooner = _enqueue(queue, run_after=T0 + timedelta(minutes=1))
    now_a = _enqueue(queue)
    other = _enqueue(queue, kind="upload.sweep")

    (due_now,) = queue.claim(["asset.delete"], limit=10, now=T0)
    assert due_now.id == now_a
    assert queue.claim(["asset.delete"], limit=10, now=T0) == [], "leased"
    assert queue.claim([], limit=10, now=T0) == []
    assert [j.id for j in queue.claim(["upload.sweep"], limit=10, now=T0)] == [other]
    queue.complete(due_now)

    in_ten = T0 + timedelta(minutes=10)
    assert [j.id for j in queue.claim(["asset.delete"], limit=1, now=in_ten)] == [sooner]
    assert [j.id for j in queue.claim(["asset.delete"], limit=5, now=in_ten)] == [later]


def test_a_lease_that_runs_out_lets_the_job_be_claimed_again():
    """An instance that died mid-job. The attempt count doubles as the fencing
    token: the worker whose lease expired may not reschedule what another holds."""
    queue = _queue()
    _enqueue(queue)
    (stale,) = queue.claim(["asset.delete"], now=T0)
    assert stale.attempts == 1
    assert queue.claim(["asset.delete"], now=T0 + timedelta(seconds=LEASE_SECONDS)) == []

    after = T0 + timedelta(seconds=LEASE_SECONDS + 1)
    (fresh,) = queue.claim(["asset.delete"], now=after)
    assert (fresh.id, fresh.attempts) == (stale.id, 2)

    queue.fail(stale, error="TimeoutError", retry_at=after, now=after)
    assert queue.snapshot() == [(fresh.id, "asset.delete", 2, None, False)], "the stale worker changed nothing"
    assert queue.claim(["asset.delete"], now=after) == [], "and the live lease still stands"

    queue.complete(stale)
    assert queue.snapshot() == [], "finished work is finished, whoever finished it"
    queue.fail(fresh, error="Late", retry_at=None, now=after)  # a no-op on a job that is gone


def test_one_job_can_be_claimed_by_id():
    queue = _queue()
    first, second = _enqueue(queue), _enqueue(queue)
    assert [j.id for j in queue.claim(["asset.delete"], job_id=second, now=T0)] == [second]
    assert [j.id for j in queue.claim(["asset.delete"], job_id=second, now=T0)] == []
    assert [j.id for j in queue.claim(["asset.delete"], now=T0)] == [first]


# ── The runner ───────────────────────────────────────────────────────────────


def test_a_job_that_succeeds_is_forgotten():
    queue = _queue()
    _enqueue(queue, payload={"asset_id": "a-9"})
    seen: list[Job] = []
    runner = JobRunner(queue, {"asset.delete": JobHandler(seen.append)}, clock=lambda: T0)
    assert runner.run_due(limit=5) == 1
    assert [(j.kind, dict(j.payload), j.attempts, j.created_at) for j in seen] == [
        ("asset.delete", {"asset_id": "a-9"}, 1, T0)
    ]
    assert queue.snapshot() == [] and runner.run_due() == 0


def test_the_runner_claims_one_job_at_a_time():
    """A lease starts when its handler does. Leased in a batch, the last job's
    lease would already be as old as everything that ran before it; and a hook on
    an ordinary request asking for one job must never be handed the backlog."""
    queue = _queue()
    ids = [_enqueue(queue) for _ in range(3)]
    unclaimed_while_running: list[list[int]] = []

    def handler(job: Job) -> None:
        unclaimed_while_running.append([row[0] for row in queue.snapshot() if row[2] == 0])

    runner = JobRunner(queue, {"asset.delete": JobHandler(handler)}, clock=lambda: T0)
    assert runner.run_due(limit=2) == 2
    assert unclaimed_while_running == [ids[1:], ids[2:]]
    assert queue.snapshot() == [(ids[2], "asset.delete", 0, None, False)]
    assert runner.run_due(limit=5) == 1 and runner.run_due(limit=5) == 0


def test_a_failing_job_backs_off_and_a_deletion_is_never_abandoned():
    queue = _queue()
    _enqueue(queue)
    clock = [T0]

    def handler(job: Job) -> None:
        raise TimeoutError

    runner = JobRunner(queue, {"asset.delete": JobHandler(handler)}, clock=lambda: clock[0])
    delays = []
    for _ in range(12):
        assert runner.run_due() == 1
        (row,) = queue._rows.values()
        delays.append(int((row.run_after - clock[0]).total_seconds()))
        assert runner.run_due() == 0, "not due again until the delay has passed"
        clock[0] = row.run_after
    assert delays == [5, 10, 20, 40, 80, 160, 320, 640, 1280, 2560, 3600, 3600]
    assert queue.snapshot() == [(1, "asset.delete", 12, "TimeoutError", False)]
    assert retry_delay(0) == timedelta(seconds=5)


def test_a_job_with_a_limit_dies_after_it_and_is_left_for_the_operator(caplog):
    queue = _queue()
    _enqueue(queue, kind="upload.sweep")
    clock = [T0]

    def handler(job: Job) -> None:
        raise ValueError

    runner = JobRunner(queue, {"upload.sweep": JobHandler(handler, max_attempts=2)}, clock=lambda: clock[0])
    assert runner.run_due() == 1
    clock[0] += timedelta(hours=1)
    with caplog.at_level(logging.WARNING, logger="service.jobs"):
        assert runner.run_due() == 1
    assert "giving up" in caplog.text
    assert queue.snapshot() == [(1, "upload.sweep", 2, "ValueError", True)]
    clock[0] += timedelta(days=1)
    assert runner.run_due() == 0, "a dead job is never claimed"


def test_an_older_build_leaves_a_newer_builds_jobs_alone():
    """Mid-rollout, the old revision has no handler for the new kind. It must not
    claim the job and burn its attempts."""
    queue = _queue()
    _enqueue(queue, kind="cue.reindex")
    assert JobRunner(queue, {"asset.delete": JobHandler(lambda job: None)}, clock=lambda: T0).run_due(limit=10) == 0
    assert JobRunner(queue, {}, clock=lambda: T0).run_due(limit=10) == 0
    assert queue.snapshot() == [(1, "cue.reindex", 0, None, False)]


def test_a_job_runs_as_soon_as_its_transaction_commits_and_not_before():
    queue = _queue()
    ran: list[int] = []
    runner = JobRunner(queue, {"asset.delete": JobHandler(lambda job: ran.append(job.id))}, clock=lambda: T0)
    with queue.db.transaction() as unit:
        job_id = queue.enqueue(unit, "asset.delete", {"asset_id": "a-1"}, now=T0)
        runner.run_after_commit(unit, job_id)
        assert ran == []
    assert ran == [job_id] and queue.snapshot() == []


def test_a_rolled_back_transaction_runs_nothing():
    queue = _queue()
    ran: list[int] = []
    runner = JobRunner(queue, {"asset.delete": JobHandler(lambda job: ran.append(job.id))}, clock=lambda: T0)
    with pytest.raises(RuntimeError):
        with queue.db.transaction() as unit:
            runner.run_after_commit(unit, queue.enqueue(unit, "asset.delete", now=T0))
            raise RuntimeError("boom")
    assert ran == [] and queue.snapshot() == []


def test_an_inline_failure_leaves_the_row_as_its_retry_record():
    queue = _queue()

    def handler(job: Job) -> None:
        raise ConnectionError

    runner = JobRunner(queue, {"asset.delete": JobHandler(handler)}, clock=lambda: T0)
    with queue.db.transaction() as unit:
        runner.run_after_commit(unit, queue.enqueue(unit, "asset.delete", now=T0))
    assert queue.snapshot() == [(1, "asset.delete", 1, "ConnectionError", False)]


def test_the_in_memory_database_is_shared_with_the_other_fakes():
    db = InMemoryDatabase()
    assert InMemoryJobQueue(db).db is db

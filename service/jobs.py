"""
The transactional outbox for background jobs (1kg.1.5).

A job is work that has to happen *because* a transaction committed — delete the
objects of an asset that was just tombstoned, sweep uploads that never finished.
`enqueue()` takes the unit of work of the change that needs it, so the aggregate
and its job commit or roll back together; there is no window in which one exists
without the other.

The outbox carries jobs and nothing else. It is not an event log: realtime
delivery reads current state, and a notification is only a wake-up (RT-5,
1kg.1.4).

**Claims are leases.** A handler calls other services, and a row lock held
across that call would pin one of very few connections. `claim()` is one
short statement — `FOR UPDATE SKIP LOCKED`, so two instances never take the same
job — that stamps `locked_until` and counts the attempt. An instance that dies
mid-job lets its lease run out and the job is claimed again, so **handlers must
be idempotent**. `attempts` doubles as the fencing token: only the latest
claimer may reschedule a job.

**Nothing here runs by itself.** Cloud Run allocates CPU only while a request is
in flight, so there is no worker thread. `JobRunner.run_due()` is called from
the three places RT-15 names — after the commit that created the job
(`run_after_commit`), from a hook on ordinary requests, and from an
authenticated `/internal/jobs` that Cloud Scheduler calls. The last two are
wired by the beads that introduce the first job kinds (`1kg.8.1`, `1kg.9.5`).

**Dedupe absorbs only into a job nobody has started.** Once a job has been
claimed, its handler may already have read the state it acts on, so a new
request for the same work gets a row of its own and runs afterwards. Two jobs
for one key are harmless — handlers are idempotent; a request swallowed by a
job that then finishes without it is not.

The `attempts = 0` guard alone did not achieve that. It is read at the moment of
the absorb and locked nothing, so between the absorb and the enqueuer's commit
another instance could claim that job, run it and delete it — and the change the
enqueuer made in the same transaction was then never processed by anything
(W-1). An absorbing enqueue therefore holds the row it absorbed into
`FOR SHARE`, and `claim`'s `FOR UPDATE SKIP LOCKED` skips it until every
absorber has committed. `InMemoryJobQueue` holds the same rule directly.

**Rows are content-free** (SEC-20): a payload is a flat object of identifiers,
which `check_payload` enforces by shape, and a failure records the exception's
class name — never its message.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .db import Database, InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork

log = logging.getLogger(__name__)

JsonScalar = str | int | bool | None

KIND_MAX_CHARS = 100
DEDUPE_KEY_MAX_CHARS = 200
PAYLOAD_MAX_KEYS = 20
PAYLOAD_KEY_MAX_CHARS = 60
PAYLOAD_VALUE_MAX_CHARS = 200

#: No longer than the platform lets a request live (Cloud Run: 300 s), since a
#: handler always runs inside one.
LEASE_SECONDS = 300

#: Server-side bounds on the transactions this queue opens. Defence in depth for
#: the database — **not** a client wall clock: they do not cover COMMIT, and they
#: do nothing about a black-holed network. A hard client-side deadline is
#: `1kg.2.8`'s question. `docs/migrations.md` section 4 says so in full.
LOCK_TIMEOUT = "2s"
STATEMENT_TIMEOUT = "2s"
TRANSACTION_TIMEOUT = "5s"
RETRY_BASE_SECONDS = 5
RETRY_CAP_SECONDS = 3600


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: Mapping[str, JsonScalar]
    #: How many times it has been claimed, this claim included.
    attempts: int
    created_at: datetime


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def check_kind(kind: str) -> str:
    if not kind or len(kind) > KIND_MAX_CHARS:
        raise ValueError(f"a job kind is 1 to {KIND_MAX_CHARS} characters")
    return kind


def check_payload(payload: Mapping[str, JsonScalar] | None) -> dict[str, JsonScalar]:
    """Identifiers only. The shape is what keeps content out: a flat object of
    short scalars has no room for a brief, a field value or a filename."""
    checked = dict(payload or {})
    if len(checked) > PAYLOAD_MAX_KEYS:
        raise ValueError(f"a job payload has at most {PAYLOAD_MAX_KEYS} keys")
    for key, value in checked.items():
        if not isinstance(key, str) or not key or len(key) > PAYLOAD_KEY_MAX_CHARS:
            raise ValueError("a job payload key is a short string")
        if not isinstance(value, str | int | bool | type(None)):
            raise ValueError(f"job payload value '{key}' must be a string, a whole number, a boolean or null")
        if isinstance(value, str) and len(value) > PAYLOAD_VALUE_MAX_CHARS:
            raise ValueError(f"job payload value '{key}' is too long to be an identifier")
    return checked


def check_dedupe_key(dedupe_key: str | None) -> str | None:
    if dedupe_key is not None and (not dedupe_key or len(dedupe_key) > DEDUPE_KEY_MAX_CHARS):
        raise ValueError(f"a dedupe key is 1 to {DEDUPE_KEY_MAX_CHARS} characters")
    return dedupe_key


def retry_delay(attempts: int) -> timedelta:
    """5 s, 10 s, 20 s ... capped at an hour."""
    return timedelta(seconds=min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1)))


class JobQueue(Protocol):
    def enqueue(
        self,
        unit: UnitOfWork,
        kind: str,
        payload: Mapping[str, JsonScalar] | None = None,
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        """Add a job inside `unit`'s transaction; returns its id. With a
        `dedupe_key`, a job of the same kind and key that nobody has claimed yet
        absorbs this one: its id is returned, and it keeps its own payload and
        `run_after`."""
        ...  # pragma: no cover - structural type

    def claim(
        self,
        kinds: Collection[str],
        *,
        limit: int = 1,
        job_id: int | None = None,
        lease_seconds: int = LEASE_SECONDS,
        now: datetime | None = None,
    ) -> list[Job]:
        """Lease up to `limit` due jobs of the given kinds, returned oldest due first."""
        ...  # pragma: no cover - structural type

    def complete(self, job: Job) -> None:
        """The work is done: forget the job."""
        ...  # pragma: no cover - structural type

    def fail(self, job: Job, *, error: str, retry_at: datetime | None, now: datetime | None = None) -> None:
        """Release the lease. `retry_at=None` marks the job dead."""
        ...  # pragma: no cover - structural type


# ── Postgres ─────────────────────────────────────────────────────────────────

#: MATERIALIZED, not `WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)`: the
#: planner may run that subquery once per outer row, and each rerun skips the rows
#: this statement has already locked, so the LIMIT window slides and one claim
#: leases the whole backlog.
def _bound(conn: object) -> None:
    """The first statement of every transaction this queue opens.

    `set_config(..., true)` is transaction-scoped, which holds only because
    `Database.connection()` hands out a connection that is *not* in autocommit
    and commits when its block exits. `enqueue()` is deliberately not bounded
    this way: it runs inside the caller's transaction, and re-timing the rest of
    the caller's work is not this module's business.
    """
    conn.execute(  # type: ignore[attr-defined]  # justification: the psycopg connection Database yields is untyped
        "SELECT set_config('lock_timeout', %s, true), "
        "set_config('statement_timeout', %s, true), "
        "set_config('transaction_timeout', %s, true)",
        (LOCK_TIMEOUT, STATEMENT_TIMEOUT, TRANSACTION_TIMEOUT),
    )


_CLAIM = """
WITH due AS MATERIALIZED (
  SELECT id FROM app.jobs
   WHERE dead_at IS NULL
     AND run_after <= %(now)s
     AND (locked_until IS NULL OR locked_until < %(now)s)
     AND kind = ANY(%(kinds)s)
     AND (%(job_id)s::bigint IS NULL OR id = %(job_id)s::bigint)
   ORDER BY run_after, id
   LIMIT %(limit)s
   FOR UPDATE SKIP LOCKED)
UPDATE app.jobs AS j
   SET locked_until = %(lease_until)s, attempts = j.attempts + 1
  FROM due
 WHERE j.id = due.id
RETURNING j.id, j.kind, j.payload, j.attempts, j.created_at, j.run_after
"""


class PostgresJobQueue:
    """`app.jobs` (migration 0003)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def enqueue(
        self,
        unit: UnitOfWork,
        kind: str,
        payload: Mapping[str, JsonScalar] | None = None,
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        if not isinstance(unit, PgTransaction):
            raise TypeError("a Postgres job is enqueued inside a Postgres transaction")
        moment = _now(now)
        params = (
            check_kind(kind),
            json.dumps(check_payload(payload)),
            check_dedupe_key(dedupe_key),
            run_after or moment,
            moment,
        )
        # Twice at most: the job that absorbed the insert can be claimed (and so
        # leave the index) before the SELECT sees it, in which case the insert wins.
        #
        # The SELECT holds what it finds FOR SHARE until this transaction commits,
        # which is what stops a concurrent claim from running and deleting the
        # absorbing job — and this transaction's work with it — before the work is
        # visible (W-1). `claim`'s FOR UPDATE SKIP LOCKED skips a share-locked row;
        # share mode lets concurrent absorbers through without waiting for each
        # other. The one thing this statement can wait for is a claim's own short
        # transaction, and if that claim wins the row, `attempts = 0` no longer
        # matches, so the next turn of this loop inserts a row of its own.
        for _ in range(3):
            row = unit.conn.execute(
                "INSERT INTO app.jobs (kind, payload, dedupe_key, run_after, created_at) "
                "VALUES (%s, %s::jsonb, %s, %s, %s) "
                "ON CONFLICT (kind, dedupe_key) "
                "WHERE dedupe_key IS NOT NULL AND dead_at IS NULL AND attempts = 0 "
                "DO NOTHING RETURNING id",
                params,
            ).fetchone()
            if row is None:
                row = unit.conn.execute(
                    "SELECT id FROM app.jobs "
                    "WHERE kind = %s AND dedupe_key = %s AND dead_at IS NULL AND attempts = 0 "
                    "FOR SHARE",
                    (kind, dedupe_key),
                ).fetchone()
            if row is not None:
                return int(row[0])
        raise RuntimeError("could not enqueue the job")  # pragma: no cover - needs a three-way race

    def claim(
        self,
        kinds: Collection[str],
        *,
        limit: int = 1,
        job_id: int | None = None,
        lease_seconds: int = LEASE_SECONDS,
        now: datetime | None = None,
    ) -> list[Job]:
        if not kinds or limit < 1:
            return []
        moment = _now(now)
        with self._db.connection() as conn:
            _bound(conn)
            rows = conn.execute(
                _CLAIM,
                {
                    "now": moment,
                    "lease_until": moment + timedelta(seconds=lease_seconds),
                    "kinds": sorted(kinds),
                    "job_id": job_id,
                    "limit": limit,
                },
            ).fetchall()
        rows = sorted(rows, key=lambda r: (r[5], r[0]))
        return [Job(int(r[0]), str(r[1]), dict(r[2]), int(r[3]), r[4]) for r in rows]

    def complete(self, job: Job) -> None:
        with self._db.connection() as conn:
            _bound(conn)
            conn.execute("DELETE FROM app.jobs WHERE id = %s", (job.id,))

    def fail(self, job: Job, *, error: str, retry_at: datetime | None, now: datetime | None = None) -> None:
        with self._db.connection() as conn:
            _bound(conn)
            conn.execute(
                "UPDATE app.jobs SET locked_until = NULL, last_error = %s, "
                "run_after = COALESCE(%s, run_after), "
                "dead_at = CASE WHEN %s::timestamptz IS NULL THEN %s::timestamptz END "
                "WHERE id = %s AND attempts = %s",
                (error[:200], retry_at, retry_at, _now(now), job.id, job.attempts),
            )


# ── In memory ────────────────────────────────────────────────────────────────


@dataclass
class _Row:
    job: Job
    dedupe_key: str | None
    run_after: datetime
    locked_until: datetime | None = None
    last_error: str | None = None
    dead_at: datetime | None = None


@dataclass
class InMemoryJobQueue:
    """The fake, with the same visibility, ordering, lease, fencing and dedupe
    semantics: a job exists for `claim` only once its transaction has committed."""

    db: InMemoryDatabase = field(default_factory=InMemoryDatabase)
    _rows: dict[int, _Row] = field(default_factory=dict)
    #: Enqueued but not committed, per open unit of work.
    _staged: dict[int, dict[int, _Row]] = field(default_factory=dict)
    #: Committed rows an open unit absorbed into, and so holds until it commits.
    #: The twin of `SELECT ... FOR SHARE`: `claim` skips these, as its
    #: `FOR UPDATE SKIP LOCKED` skips a share-locked row.
    _held: dict[int, set[int]] = field(default_factory=dict)
    _next_id: int = 1

    def enqueue(
        self,
        unit: UnitOfWork,
        kind: str,
        payload: Mapping[str, JsonScalar] | None = None,
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        if not isinstance(unit, InMemoryTransaction):
            raise TypeError("an in-memory job is enqueued inside an in-memory transaction")
        check_kind(kind)
        checked = check_payload(payload)
        mine = self._staged_by(unit)
        if check_dedupe_key(dedupe_key) is not None:
            # What this transaction can see: committed rows, and its own.
            for row in (*self._rows.values(), *mine.values()):
                unstarted = row.dead_at is None and row.job.attempts == 0
                if row.job.kind == kind and row.dedupe_key == dedupe_key and unstarted:
                    if row.job.id in self._rows:
                        # A committed row: hold it, so no claim can take the job —
                        # and delete this transaction's work with it — before we
                        # commit. Our own staged rows are invisible anyway.
                        self._held.setdefault(id(unit), set()).add(row.job.id)
                    return row.job.id
        moment = _now(now)
        job_id = self._next_id
        self._next_id += 1
        mine[job_id] = _Row(Job(job_id, kind, checked, 0, moment), dedupe_key, run_after or moment)
        return job_id

    def _staged_by(self, unit: InMemoryTransaction) -> dict[int, _Row]:
        """This unit's uncommitted jobs: published when it commits, dropped when it
        rolls back, and until then invisible to `claim`."""
        key = id(unit)
        if key not in self._staged:
            self._staged[key] = {}

            def publish() -> None:
                self._rows.update(self._staged.pop(key, {}))
                self._held.pop(key, None)

            def discard() -> None:
                self._staged.pop(key, None)
                self._held.pop(key, None)

            unit.on_publish(publish)
            unit.on_rollback(discard)
        return self._staged[key]

    def claim(
        self,
        kinds: Collection[str],
        *,
        limit: int = 1,
        job_id: int | None = None,
        lease_seconds: int = LEASE_SECONDS,
        now: datetime | None = None,
    ) -> list[Job]:
        moment = _now(now)
        due = sorted(
            (
                row
                for row in self._rows.values()
                if row.dead_at is None
                and row.run_after <= moment
                and (row.locked_until is None or row.locked_until < moment)
                and row.job.kind in kinds
                and (job_id is None or row.job.id == job_id)
                and not self._is_held(row.job.id)
            ),
            key=lambda row: (row.run_after, row.job.id),
        )[: max(0, limit)]
        for row in due:
            row.locked_until = moment + timedelta(seconds=lease_seconds)
            row.job = replace(row.job, attempts=row.job.attempts + 1)
        return [row.job for row in due]

    def _is_held(self, job_id: int) -> bool:
        """True while some open transaction has absorbed into this row."""
        return any(job_id in held for held in self._held.values())

    def complete(self, job: Job) -> None:
        self._rows.pop(job.id, None)

    def fail(self, job: Job, *, error: str, retry_at: datetime | None, now: datetime | None = None) -> None:
        row = self._rows.get(job.id)
        if row is None or row.job.attempts != job.attempts:
            return
        row.locked_until = None
        row.last_error = error[:200]
        if retry_at is None:
            row.dead_at = _now(now)
        else:
            row.run_after = retry_at

    def snapshot(self) -> list[tuple[int, str, int, str | None, bool]]:
        """`(id, kind, attempts, last_error, dead)` per remaining job — for assertions."""
        return [
            (row.job.id, row.job.kind, row.job.attempts, row.last_error, row.dead_at is not None)
            for row in sorted(self._rows.values(), key=lambda row: row.job.id)
        ]


# ── Running jobs ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JobContext:
    """What a handler is told about its time.

    **Advisory.** Nothing interrupts a handler: the runner stops *starting*
    work when the budget is spent and lets what is running finish. A handler
    that blocks past its deadline breaks this contract, and the only bound left
    is the platform's — Cloud Run's request timeout, Cloud Scheduler's attempt
    deadline. So a handler must cap its own I/O by `remaining_seconds()`.
    """

    #: None: no budget — run to completion.
    deadline_monotonic: float | None = None
    monotonic: Callable[[], float] = time.monotonic

    def remaining_seconds(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return self.deadline_monotonic - self.monotonic()

    def expired(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0


@dataclass(frozen=True)
class JobRunResult:
    """What an attempt says about itself — counts only, never content (SEC-20).

    `ran` counts jobs attempted, whatever the outcome; `failed` counts how many
    of those raised. `remaining` is conservative: it is True whenever the runner
    stopped before the queue was empty, and only False when a claim came back
    empty and so proved there was nothing due.
    """

    ran: int = 0
    failed: int = 0
    remaining: bool = False


class UnknownJobKind(Exception):
    """A claimed job whose kind has no handler at dispatch.

    `claim` filters by the registry, so this is the narrow window in which the
    registry changed in between. The job is failed content-free and retried —
    never dead-lettered, because the instance that does have the handler should
    still get it.
    """


@dataclass(frozen=True)
class JobHandler:
    run: Callable[[Job, JobContext], None]
    #: None: retried until it succeeds, with capped backoff — a deletion is
    #: never abandoned. A number: marked dead after that many attempts.
    max_attempts: int | None = None


class JobRunner:
    """Claims jobs and runs their handlers. Only kinds this build has a handler
    for are ever claimed, so during a rollout an older instance leaves a newer
    build's jobs alone instead of failing them."""

    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, JobHandler] | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue = queue
        self._handlers = dict(handlers or {})
        self._clock = clock
        self._monotonic = monotonic

    def register(self, kind: str, handler: JobHandler) -> None:
        """Wire a kind at startup. Claims take a snapshot of what is registered,
        so an older build never claims a newer build's kinds."""
        self._handlers[check_kind(kind)] = handler

    def run_due(self, limit: int = 1, deadline_monotonic: float | None = None) -> JobRunResult:
        """Run up to `limit` due jobs within a **cooperative** budget.

        The budget stops the runner *starting* another job; it never interrupts
        one that is running. One claim per job, so a lease starts when its
        handler does, not while the jobs ahead of it are still running."""
        context = JobContext(deadline_monotonic, self._monotonic)
        ran = failed = 0
        while ran < max(0, limit):
            if context.expired():
                return JobRunResult(ran, failed, remaining=True)
            claimed = self._queue.claim(self._handlers.keys(), now=self._clock())
            if not claimed:
                # The only probe there is: nothing was due.
                return JobRunResult(ran, failed, remaining=False)
            attempted, lost = self._run(claimed, context)
            ran += attempted
            failed += lost
        # Stopped on the count, so there may well be more.
        return JobRunResult(ran, failed, remaining=ran > 0)

    def run_job(self, job_id: int, deadline_monotonic: float | None = None) -> JobRunResult:
        """One named job, now — what both post-response drivers call. Its row
        stays the retry record, so nothing is lost if this attempt does not run."""
        context = JobContext(deadline_monotonic, self._monotonic)
        if context.expired():
            return JobRunResult(remaining=True)
        claimed = self._queue.claim(self._handlers.keys(), job_id=job_id, now=self._clock())
        ran, failed = self._run(claimed, context)
        return JobRunResult(ran, failed, remaining=False)

    def run_after_commit(self, unit: UnitOfWork, job_id: int) -> None:
        """Try the job as soon as `unit` commits; its row stays as the retry record."""

        def run() -> None:
            self.run_job(job_id)

        unit.on_commit(run)

    def _run(self, jobs: list[Job], context: JobContext) -> tuple[int, int]:
        """Returns (attempted, failed). Never raises: a job's failure is the
        job's, and a driver must be able to swallow it whole."""
        failed = 0
        for job in jobs:
            handler = self._handlers.get(job.kind)
            error: str | None = None
            if handler is None:
                error = UnknownJobKind.__name__
            else:
                try:
                    handler.run(job, context)
                except Exception as exc:
                    error = type(exc).__name__
            if error is None:
                self._queue.complete(job)
                continue
            failed += 1
            now = self._clock()
            exhausted = (
                handler is not None and handler.max_attempts is not None and job.attempts >= handler.max_attempts
            )
            self._queue.fail(job, error=error, retry_at=None if exhausted else now + retry_delay(job.attempts), now=now)
            log.warning(
                "jobs: %s #%d failed on attempt %d (%s)%s",
                job.kind, job.id, job.attempts, error, "; giving up" if exhausted else "",
            )
        return len(jobs), failed

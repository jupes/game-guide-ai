"""The media job kinds and the system statements they run
(agent-forge-harness-1kg.8.1.1, slice a of the media bead).

Three kinds, one registration (`register_jobs`), and nothing that runs by
itself: the runner's three drivers run what is due (`service/job_driver.py`),
and slice b (`1kg.8.1.2`) calls `register_jobs` from `_build_stores` when a
store is configured. Until then no kind here is registered in the running
service.

* **`asset.delete`** — enqueued by a GM's delete and by the campaign primitive
  (`service/asset_store.py`), with the asset id as its dedupe key and the payload
  `{asset_id, object_key, tmp_key}`. With no database connection open it deletes
  the `tmp/` object, every derivative under `object_key + "/"` page by page, and
  the original; then, in one short bounded transaction, it purges the row — but
  only while it is still the tombstone. The keys come from the payload, so the
  bytes go even when the primitive has already removed the row (SEC-36). Never
  marked dead (`max_attempts=None`): a deletion is never abandoned (MS-3).
* **`asset.sweep_stuck`** — one per entry into `uploading` (due an hour later)
  or `processing` (ten minutes later), seeded by that asset's own transition,
  payload `{asset_id}`, no dedupe key (RQ-12). It reads the row, and if it is
  still in the state it read and past that state's bound, fails it `timed_out`
  with a FENCED update — the state and the moment it read — releasing the
  reservation only if the fence held. **Objects are deleted only after a fence
  that changed the row**: a fence that changed nothing means another transition
  won (`ready`, a return to `uploading`, a tombstone, the primitive), and that
  transition's own path owns the bytes. A row that read `failed` has its objects
  deleted, idempotently.
* **`asset.reconcile_orphans`** — payload `{}` or `{"after": <key>}`, no dedupe
  key (RQ-12). It walks one ordered key space, `assets/` then `tmp/`, strictly
  after its cursor, examining at most `RECONCILE_BATCH` objects, and deletes
  each examined object older than a day that no row other than a failed row or
  a tombstone names (a derivative counts through its parent). No key is ever
  reused, so the lookup and the delete need no lock between them. **The chain
  ends**: while more remain it enqueues one successor carrying the last key it
  examined, and once the listing is exhausted it enqueues nothing. Calling
  `enqueue_reconcile` on a schedule is `1kg.9.5`'s; until then the bucket's own
  lifecycle rule for `tmp/` is production's backstop (MS-3).

**No handler holds a connection while it calls the object store**: each reads
or writes in a short transaction of its own and closes it before any byte moves
(requirement 3.8). Every call reaches the store through `via_store`. **A
handler never returns early as success**: when its advisory `JobContext` runs
out part-way it raises `JobOutOfTime`, the runner records only that class name,
and the job is retried; an early return would have the runner complete — and
so delete — a job that still had work to do.

**The system statements live here and only here.** They name an asset by its
id, which is globally unique; no GM-facing method can reach them, and none
carries or logs a campaign next to a key (MS-2). This module logs nothing
itself: the runner logs the kind, the job id, the attempt and the class.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from . import campaign_identity as ident
from .asset_store import (
    DELETE_JOB,
    DELETED,
    PROCESSING_BOUND,
    SWEEP_JOB,
    UPLOADING_BOUND,
    AssetRow,
    asset_rows,
    bound_before_locking,
    stage_row,
    visible_rows,
)
from .campaign_store import Staging, fake, pg
from .db import InMemoryDatabase, TransactionalDatabase, UnitOfWork
from .jobs import Job, JobContext, JobHandler, JobQueue, JobRunner, JsonScalar
from .media_objects import (
    ASSETS_PREFIX,
    OBJECT_KEY_PATTERN,
    TMP_KEY_PATTERN,
    TMP_PREFIX,
    ObjectPage,
    ObjectStore,
    check_key,
    parent_key,
    via_store,
)
from .workbench_contracts import AssetFailure, AssetState

RECONCILE_JOB = "asset.reconcile_orphans"
#: At most this many objects examined in one reconcile run (*suggested*).
RECONCILE_BATCH = 200
#: An object younger than this may belong to an upload still in flight
#: (*suggested*, the bucket's own `tmp/` rule, MS-3).
ORPHAN_AGE = timedelta(days=1)
#: How many derivatives the delete job lists at a time.
DERIVATIVE_PAGE = 100
#: "Any age at all", for a listing that wants every object under a prefix.
EVERY_AGE = datetime.max.replace(tzinfo=UTC)

_UPLOADING = AssetState.UPLOADING.value
_PROCESSING = AssetState.PROCESSING.value
_FAILED = AssetState.FAILED.value
_BOUNDS = {_UPLOADING: UPLOADING_BOUND, _PROCESSING: PROCESSING_BOUND}


class JobOutOfTime(Exception):
    """The job's advisory deadline passed part-way. Raised, never returned, so
    that the runner retries the job instead of completing it."""


def _in_time(context: JobContext) -> None:
    if context.expired():
        raise JobOutOfTime()


# ── Payloads: identifiers and keys, checked ──────────────────────────────────


def _asset_id(payload: Mapping[str, JsonScalar]) -> str:
    value = payload.get("asset_id")
    if not isinstance(value, str):
        raise ValueError("a media job's payload names an asset")
    return ident.check_id(ident.ASSET, value)


def _payload_key(payload: Mapping[str, JsonScalar], name: str, pattern: str) -> str:
    value = check_key(payload.get(name))
    if not _matches(pattern, value):
        raise ValueError(f"a media job's {name} is not of its shape")
    return value


def _matches(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value) is not None


def _cursor(payload: Mapping[str, JsonScalar]) -> str | None:
    if set(payload) - {"after"}:
        raise ValueError("a reconcile job's payload carries a cursor and nothing else")
    after = payload.get("after")
    return None if after is None else check_key(after)


# ── The system statements ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepView:
    """What the sweep reads of a row: never its alt text or its campaign."""

    state: str
    state_changed_at: datetime
    object_key: str
    tmp_key: str


class AssetSystem(Protocol):
    """The statements no GM method can reach. Each names the asset by its id."""

    def read_for_sweep(self, unit: UnitOfWork, asset_id: str) -> SweepView | None:
        ...  # pragma: no cover - structural type

    def fail_timed_out(
        self, unit: UnitOfWork, asset_id: str, *, state: str, state_changed_at: datetime, now: datetime
    ) -> bool:
        """The fenced write: `failed`/`timed_out` only if the row is still in
        `state` since `state_changed_at`; the reservation is released only if
        it was. Reports whether the fence held."""
        ...  # pragma: no cover - structural type

    def purge(self, unit: UnitOfWork, asset_id: str) -> bool:
        """Remove the row, only while it is still the tombstone (MS-3)."""
        ...  # pragma: no cover - structural type

    def referenced(self, unit: UnitOfWork, keys: Collection[str]) -> set[str]:
        """Which of `keys` a row that is not failed or deleted names."""
        ...  # pragma: no cover - structural type


class PostgresAssetSystem:
    def read_for_sweep(self, unit: UnitOfWork, asset_id: str) -> SweepView | None:
        found = pg(unit).conn.execute(
            "SELECT state, state_changed_at, object_key, tmp_key FROM campaign.assets WHERE id = %(asset_id)s",
            {"asset_id": asset_id},
        ).fetchone()
        return None if found is None else SweepView(found[0], found[1], found[2], found[3])

    def fail_timed_out(
        self, unit: UnitOfWork, asset_id: str, *, state: str, state_changed_at: datetime, now: datetime
    ) -> bool:
        transaction = pg(unit)
        bound_before_locking(transaction, None)
        conn = transaction.conn
        # The fence: the state and the moment the sweep read. Under READ
        # COMMITTED a transition that commits first is re-read here, and the
        # fence then matches nothing — the asset row, then the usage (L-5).
        won = conn.execute(
            "UPDATE campaign.assets SET state = 'failed', failure = %(failure)s, "
            "updated_at = %(now)s, state_changed_at = %(now)s "
            "WHERE id = %(asset_id)s AND state = %(state)s AND state_changed_at = %(state_changed_at)s "
            "RETURNING campaign_id, declared_size_bytes",
            {
                "failure": AssetFailure.TIMED_OUT.value,
                "now": now,
                "asset_id": asset_id,
                "state": state,
                "state_changed_at": state_changed_at,
            },
        ).fetchone()
        if won is None:
            return False
        conn.execute(
            "UPDATE campaign.media_usage "
            "SET bytes_reserved = bytes_reserved - %(freed)s, asset_count = asset_count - 1 "
            "WHERE campaign_id = %(campaign_id)s",
            {"freed": int(won[1]), "campaign_id": won[0]},
        )
        return True

    def purge(self, unit: UnitOfWork, asset_id: str) -> bool:
        transaction = pg(unit)
        bound_before_locking(transaction, None)
        purged = transaction.conn.execute(
            "DELETE FROM campaign.assets WHERE id = %(asset_id)s AND state = 'deleted' RETURNING id",
            {"asset_id": asset_id},
        ).fetchone()
        return purged is not None

    def referenced(self, unit: UnitOfWork, keys: Collection[str]) -> set[str]:
        wanted = sorted(keys)
        found = pg(unit).conn.execute(
            "SELECT object_key, tmp_key FROM campaign.assets "
            "WHERE state NOT IN ('failed', 'deleted') "
            "AND (object_key = ANY(%(keys)s) OR tmp_key = ANY(%(keys)s))",
            {"keys": wanted},
        ).fetchall()
        return {key for row in found for key in row if key in keys}


class InMemoryAssetSystem:
    """The same statements over the twin's rows, with its visibility and its
    one open writer."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._rows: Staging[AssetRow | None] = asset_rows(db)

    def read_for_sweep(self, unit: UnitOfWork, asset_id: str) -> SweepView | None:
        found = visible_rows(self._rows, fake(unit)).get(asset_id)
        if found is None:
            return None
        return SweepView(found.state, found.state_changed_at, found.object_key, found.tmp_key)

    def fail_timed_out(
        self, unit: UnitOfWork, asset_id: str, *, state: str, state_changed_at: datetime, now: datetime
    ) -> bool:
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(None)
        found = visible_rows(self._rows, twin).get(asset_id)
        if found is None or found.state != state or found.state_changed_at != state_changed_at:
            return False
        stage_row(
            self._rows,
            twin,
            replace(
                found, state=_FAILED, failure=AssetFailure.TIMED_OUT, updated_at=now, state_changed_at=now
            ),
            new=False,
        )
        return True

    def purge(self, unit: UnitOfWork, asset_id: str) -> bool:
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(None)
        found = visible_rows(self._rows, twin).get(asset_id)
        if found is None or found.state != DELETED:
            return False
        self._rows.replace(twin, asset_id, None)
        return True

    def referenced(self, unit: UnitOfWork, keys: Collection[str]) -> set[str]:
        live = [row for row in visible_rows(self._rows, fake(unit)).values() if row.state not in (_FAILED, DELETED)]
        return {key for row in live for key in (row.object_key, row.tmp_key) if key in keys}


# ── The handlers ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Handlers:
    db: TransactionalDatabase
    queue: JobQueue
    objects: ObjectStore
    system: AssetSystem
    clock: Callable[[], datetime]
    #: A test's seam between the sweep's read and its fenced write (AC-8's
    #: second form). None in the running service.
    between_read_and_fence: Callable[[], None] | None

    def _delete_objects(self, context: JobContext, *keys: str) -> None:
        for key in keys:
            self._delete_one(context, key)

    def _delete_one(self, context: JobContext, key: str) -> None:
        _in_time(context)
        via_store(self.objects, lambda store: store.delete_object(key))

    def _list(
        self, context: JobContext, prefix: str, *, older_than: datetime, start_after: str | None, limit: int
    ) -> ObjectPage:
        _in_time(context)
        return via_store(
            self.objects,
            lambda store: store.list_objects(prefix, older_than=older_than, start_after=start_after, limit=limit),
        )

    def delete(self, job: Job, context: JobContext) -> None:
        asset_id = _asset_id(job.payload)
        object_key = _payload_key(job.payload, "object_key", OBJECT_KEY_PATTERN)
        tmp_key = _payload_key(job.payload, "tmp_key", TMP_KEY_PATTERN)
        self._delete_objects(context, tmp_key)
        derivatives = object_key + "/"
        cursor: str | None = None
        while True:
            page = self._list(
                context, derivatives, older_than=EVERY_AGE, start_after=cursor, limit=DERIVATIVE_PAGE
            )
            self._delete_objects(context, *page.keys)
            if page.cursor is None:
                break
            cursor = page.cursor
        self._delete_objects(context, object_key)
        _in_time(context)
        with self.db.transaction() as unit:
            self.system.purge(unit, asset_id)

    def sweep(self, job: Job, context: JobContext) -> None:
        asset_id = _asset_id(job.payload)
        _in_time(context)
        with self.db.transaction() as unit:
            seen = self.system.read_for_sweep(unit, asset_id)
        if seen is None:
            return
        if seen.state == _FAILED:
            self._delete_objects(context, seen.tmp_key, seen.object_key)
            return
        bound = _BOUNDS.get(seen.state)
        if bound is None or self.clock() < seen.state_changed_at + bound:
            # Ready, a tombstone, or not yet due: a later entry into this state
            # seeded a sweep of its own.
            return
        if self.between_read_and_fence is not None:
            self.between_read_and_fence()
        _in_time(context)
        with self.db.transaction() as unit:
            won = self.system.fail_timed_out(
                unit, asset_id, state=seen.state, state_changed_at=seen.state_changed_at, now=self.clock()
            )
        if won:
            self._delete_objects(context, seen.tmp_key, seen.object_key)

    def reconcile(self, job: Job, context: JobContext) -> None:
        after = _cursor(job.payload)
        older_than = self.clock() - ORPHAN_AGE
        budget = RECONCILE_BATCH
        candidates: list[str] = []
        last = after
        more = False
        for prefix in (ASSETS_PREFIX, TMP_PREFIX):
            if after is not None and prefix == ASSETS_PREFIX and after.startswith(TMP_PREFIX):
                continue
            if budget == 0:
                more = True
                break
            start = after if after is not None and after.startswith(prefix) else None
            page = self._list(context, prefix, older_than=older_than, start_after=start, limit=budget)
            candidates.extend(page.keys)
            budget -= page.examined
            last = page.last if page.last is not None else last
            if page.cursor is not None:
                more = True
                break
        if candidates:
            _in_time(context)
            with self.db.transaction() as unit:
                kept = self.system.referenced(unit, {parent_key(key) for key in candidates})
            self._delete_objects(context, *(key for key in candidates if parent_key(key) not in kept))
        if more and last is not None:
            _in_time(context)
            with self.db.transaction() as unit:
                self.queue.enqueue(unit, RECONCILE_JOB, {"after": last}, now=self.clock())


def register_jobs(
    runner: JobRunner,
    *,
    db: TransactionalDatabase,
    queue: JobQueue,
    objects: ObjectStore,
    system: AssetSystem | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    between_read_and_fence: Callable[[], None] | None = None,
) -> None:
    """Register the three kinds on `runner` (L-10(c)). `system` defaults to the
    twin's statements for an `InMemoryDatabase` and to PostgreSQL's otherwise.
    Every kind retries until it succeeds: a deletion is never abandoned, a
    stuck row would hold its quota for ever, and a reconcile run that died
    would end its chain."""
    chosen = system
    if chosen is None:
        chosen = InMemoryAssetSystem(db) if isinstance(db, InMemoryDatabase) else PostgresAssetSystem()
    handlers = _Handlers(db, queue, objects, chosen, clock, between_read_and_fence)
    runner.register(DELETE_JOB, JobHandler(handlers.delete, max_attempts=None))
    runner.register(SWEEP_JOB, JobHandler(handlers.sweep, max_attempts=None))
    runner.register(RECONCILE_JOB, JobHandler(handlers.reconcile, max_attempts=None))


def enqueue_reconcile(unit: UnitOfWork, queue: JobQueue, *, now: datetime | None = None) -> int:
    """Start one pass of the orphan reconciliation. No dedupe key (RQ-12). The
    periodic caller is `1kg.9.5`'s Cloud Scheduler job."""
    return queue.enqueue(unit, RECONCILE_JOB, {}, now=now)

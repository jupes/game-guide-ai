"""Media assets against the twin and against PostgreSQL
(agent-forge-harness-1kg.8.1.1, slice a of the media bead).

**The shared suite.** Every behaviour of `service/asset_store.py` and of the
three job kinds in `service/asset_jobs.py` runs twice, over the `world` fixture:
once on the in-memory twin and once on PostgreSQL (`needs_db`). A rule only one
of them keeps is a rule the two will drift apart on.

**The usage invariant is recomputed from the rows after every step**: the
world's database is watched, and every transaction any test or handler opens
ends by comparing `campaign.media_usage` with the sum it must hold (L-4) — in
one statement, so in one snapshot.

**The races** (AC-8) are PostgreSQL's alone: two connections, an explicit
interleaving, the server's own word (`pg_stat_activity`) that the waiter really
waits, and no deadlock victim (`40P01`) on either side. The one gap that is not
a lock wait — the sweep reading a row before `ready` commits and writing after —
runs in both worlds through the handler's seam.

**The mechanical checks** (AC-9, AC-13) read the two modules' source with
`ast`: every statement's ownership fragment by NAME, the enqueue last, no
`FOR UPDATE`, no key ever updated.

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_asset_db.py -q
"""

from __future__ import annotations

import ast
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import asset_jobs, asset_store
from service import migrations as mig
from service.asset_jobs import (
    RECONCILE_JOB,
    AssetSystem,
    InMemoryAssetSystem,
    JobOutOfTime,
    PostgresAssetSystem,
    enqueue_reconcile,
    register_jobs,
)
from service.asset_store import (
    DELETE_JOB,
    MISSING,
    SWEEP_JOB,
    AssetRow,
    AssetRowRefused,
    IllegalTransition,
    InMemoryAssetStore,
    Measured,
    PostgresAssetStore,
    QuotaExceeded,
    asset_rows,
    check_row,
    stage_row,
    usage_of,
    visible_rows,
)
from service.campaign_store import InMemoryCampaignStore, MissingParent, PostgresCampaignStore
from service.db import (
    CampaignLockOrder,
    CampaignLockSettings,
    Database,
    InMemoryDatabase,
    PoolSettings,
    UnitOfWork,
)
from service.jobs import InMemoryJobQueue, JobRunner, JobRunResult, PostgresJobQueue, check_payload
from service.media_objects import InMemoryObjectStore, ObjectPage, ObjectStat, ObjectStoreUnavailable
from service.workbench_contracts import Asset, AssetFailure, AssetKind, AssetState

SERVICE = Path(__file__).resolve().parents[1] / "service"
T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
#: For the interleavings: a transaction there waits for a THREAD, not a database.
PATIENT = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
PATIENCE = 15
ALT = "A red door in a grey wall"
NEVER_MINTED = "ast_" + "z" * 22


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# ── The watched database: the invariant after every step ─────────────────────


class Watched:
    """The world's database, watched. It counts the transactions open right
    now — so a test can say no handler called the object store inside one —
    and recomputes the usage invariant from the rows after each one ends."""

    def __init__(self, real: Any, invariant: Callable[[], None]) -> None:
        self.real = real
        self.open = 0
        self._guard = threading.Lock()
        self._invariant = invariant

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._guard:
            self.open += 1
        try:
            with self.real.transaction() as unit:
                yield unit
        finally:
            with self._guard:
                self.open -= 1
            self._invariant()


#: L-4, recomputed from the rows in ONE statement, so in one snapshot.
_INVARIANT = """
SELECT u.campaign_id, u.bytes_reserved, u.asset_count,
       COALESCE(SUM(CASE WHEN a.state IN ('uploading', 'processing') THEN a.declared_size_bytes
                         WHEN a.state = 'ready' THEN a.size_bytes ELSE 0 END), 0),
       COUNT(a.id) FILTER (WHERE a.state IN ('uploading', 'processing', 'ready')),
       (SELECT count(*) FROM campaign.assets x
         WHERE NOT EXISTS (SELECT 1 FROM campaign.media_usage y WHERE y.campaign_id = x.campaign_id))
  FROM campaign.media_usage u LEFT JOIN campaign.assets a ON a.campaign_id = u.campaign_id
 GROUP BY u.campaign_id, u.bytes_reserved, u.asset_count
"""


def _pg_invariant(dsn: str) -> Callable[[], None]:
    def check() -> None:
        with connect(dsn) as conn:
            for campaign, stored_bytes, stored_count, rows_bytes, rows_count, orphans in conn.execute(_INVARIANT):
                assert orphans == 0, "an asset row has no usage row"
                assert (stored_bytes, stored_count) == (rows_bytes, rows_count), (
                    f"media_usage of {campaign} drifted from its rows"
                )

    return check


def _fake_invariant(db: InMemoryDatabase, store: InMemoryAssetStore) -> Callable[[], None]:
    def check() -> None:
        with db.transaction() as unit:
            rows = visible_rows(asset_rows(db), unit)
            for campaign_id in {row.campaign_id for row in rows.values()}:
                owner = db.tables["campaigns"].visible(unit)[campaign_id].owner_id
                assert store.usage(unit, campaign_id, owner_id=owner) == usage_of(rows.values(), campaign_id)
                assert usage_of(rows.values(), campaign_id).bytes_reserved >= 0

    return check


# ── The world ────────────────────────────────────────────────────────────────


@dataclass
class World:
    kind: str
    db: Watched
    campaigns: Any
    assets: Any
    queue: Any
    system: AssetSystem
    owner: int
    other_owner: int
    dsn: str | None

    def jobs(self) -> list[tuple[int, str, dict[str, Any], str | None, datetime]]:
        """Every job row: id, kind, payload, dedupe key, run_after. On the twin
        from its rows (`snapshot()` shows no payload)."""
        if self.dsn is None:
            return [
                (r.job.id, r.job.kind, dict(r.job.payload), r.dedupe_key, r.run_after)
                for r in sorted(self.queue._rows.values(), key=lambda r: r.job.id)
            ]
        with connect(self.dsn) as conn:
            return [
                (int(r[0]), r[1], dict(r[2]), r[3], r[4])
                for r in conn.execute("SELECT id, kind, payload, dedupe_key, run_after FROM app.jobs ORDER BY id")
            ]

    def job_states(self) -> list[tuple[str, int, str | None, bool]]:
        """kind, attempts, last error, dead — per job still in the outbox."""
        if self.dsn is None:
            return [(kind, attempts, error, dead) for _, kind, attempts, error, dead in self.queue.snapshot()]
        with connect(self.dsn) as conn:
            return [
                (r[0], int(r[1]), r[2], r[3] is not None)
                for r in conn.execute("SELECT kind, attempts, last_error, dead_at FROM app.jobs ORDER BY id")
            ]

    def rows(self) -> dict[str, AssetRow]:
        """Every committed row, tombstones included."""
        if self.dsn is None:
            with self.db.real.transaction() as unit:
                return visible_rows(asset_rows(self.db.real), unit)
        with connect(self.dsn) as conn:
            found = conn.execute(f"SELECT {asset_store._COLUMNS} FROM campaign.assets").fetchall()
        return {r[0]: asset_store.row_from_tuple(r) for r in found}

    def usage(self, campaign_id: str, owner: int | None = None) -> tuple[int, int]:
        with self.db.transaction() as unit:
            found = self.assets.usage(unit, campaign_id, owner_id=self.owner if owner is None else owner)
        assert found is not None
        return found.bytes_reserved, found.asset_count


def _pg_world(dsn: str, settings: CampaignLockSettings = QUICK) -> World:
    with connect(dsn) as conn:
        owners = [
            int(conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
            ).fetchone()[0])
            for email in ("gm@example.com", "other@example.com")
        ]
    real = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5), settings)
    queue = PostgresJobQueue(real)
    return World(
        "postgres", Watched(real, _pg_invariant(dsn)), PostgresCampaignStore(), PostgresAssetStore(queue),
        queue, PostgresAssetSystem(), owners[0], owners[1], dsn,
    )


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("asset") as target:
        mig.migrate(target)
        yield target


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        real = InMemoryDatabase()
        queue = InMemoryJobQueue(db=real)
        store = InMemoryAssetStore(real, queue)
        yield World(
            "fake", Watched(real, _fake_invariant(real, store)), InMemoryCampaignStore(real), store,
            queue, InMemoryAssetSystem(real), 1, 2, None,
        )
        return
    yield _pg_world(request.getfixturevalue("dsn"))


def _campaign(world: World, *, owner: int | None = None, name: str = "Nocturne") -> str:
    with world.db.transaction() as unit:
        return world.campaigns.create(unit, owner_id=world.owner if owner is None else owner, name=name).id


def _create(
    world: World,
    campaign_id: str,
    *,
    kind: AssetKind = AssetKind.IMAGE,
    size: int = 1000,
    command_id: str | None = None,
    owner: int | None = None,
    alt: str | None = ALT,
    now: datetime = T0,
) -> asset_store.AssetRecord:
    with world.db.transaction() as unit:
        return world.assets.create(
            unit,
            campaign_id,
            owner_id=world.owner if owner is None else owner,
            kind=kind,
            media_type="image/png" if kind is AssetKind.IMAGE else "audio/ogg",
            size_bytes=size,
            alt=alt if kind is AssetKind.IMAGE else None,
            command_id=command_id,
            now=now,
        )


def _measured(kind: AssetKind, size: int = 900) -> Measured:
    if kind is AssetKind.IMAGE:
        return Measured("image/png", size, width=40, height=30)
    return Measured("audio/mpeg", size, duration_ms=12_000)


def _act(world: World, campaign_id: str, asset_id: str, change: str, *, owner: int | None = None,
         now: datetime = T0, size: int = 900) -> Any:
    """One mutator, in a transaction of its own."""
    who = world.owner if owner is None else owner
    with world.db.transaction() as unit:
        if change == "start_processing":
            return world.assets.start_processing(unit, campaign_id, asset_id, owner_id=who, now=now)
        if change == "return_to_uploading":
            return world.assets.return_to_uploading(unit, campaign_id, asset_id, owner_id=who, now=now)
        if change == "mark_ready":
            record = world.assets.get(unit, campaign_id, asset_id, owner_id=who)
            kind = AssetKind.IMAGE if record is None else record.kind
            return world.assets.mark_ready(
                unit, campaign_id, asset_id, owner_id=who, measured=_measured(kind, size), now=now
            )
        if change == "mark_failed":
            return world.assets.mark_failed(
                unit, campaign_id, asset_id, owner_id=who, failure=AssetFailure.UNREADABLE, now=now
            )
        if change == "delete":
            return world.assets.delete(unit, campaign_id, asset_id, owner_id=who, now=now)
        raise AssertionError(change)


MUTATORS = ("start_processing", "return_to_uploading", "mark_ready", "mark_failed", "delete")
LEGAL = {
    "start_processing": {"uploading"},
    "return_to_uploading": {"processing"},
    "mark_ready": {"processing"},
    "mark_failed": {"uploading", "processing"},
    "delete": {"uploading", "processing", "ready", "failed"},
}
PATHS = {
    "uploading": (),
    "processing": ("start_processing",),
    "ready": ("start_processing", "mark_ready"),
    "failed": ("mark_failed",),
    "deleted": ("delete",),
}


def _in_state(world: World, campaign_id: str, state: str, kind: AssetKind = AssetKind.IMAGE,
              **create: Any) -> asset_store.AssetRecord:
    record = _create(world, campaign_id, kind=kind, **create)
    for step in PATHS[state]:
        _act(world, campaign_id, record.id, step, owner=create.get("owner"))
    return record


# ── AC-7: create ─────────────────────────────────────────────────────────────


def test_create_gives_an_uploading_row_its_reservation_and_one_deadline_job(world: World) -> None:
    campaign = _campaign(world)
    record = _create(world, campaign, size=1234)
    assert (record.state, record.kind, record.declared_size_bytes) == (AssetState.UPLOADING, AssetKind.IMAGE, 1234)
    assert record.object_key.startswith("assets/") and record.tmp_key.startswith("tmp/")
    assert world.usage(campaign) == (1234, 1)
    assert [(kind, payload, dedupe, run_after) for _, kind, payload, dedupe, run_after in world.jobs()] == [
        (SWEEP_JOB, {"asset_id": record.id}, None, T0 + asset_store.UPLOADING_BOUND)
    ]


def test_a_replayed_command_returns_the_first_asset_and_reserves_nothing(world: World) -> None:
    campaign = _campaign(world)
    first = _create(world, campaign, command_id="cmd-0123456789abcdef")
    again = _create(world, campaign, command_id="cmd-0123456789abcdef", size=5000)
    assert again == first
    assert world.usage(campaign) == (1000, 1)
    assert len(world.rows()) == 1 and len(world.jobs()) == 1


def test_a_replay_into_a_campaign_its_first_create_filled_returns_the_first_asset(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Insert first, replay detected before any reservation: a replay never
    answers QuotaExceeded, even when the first create itself filled the quota."""
    monkeypatch.setattr(asset_store, "QUOTA_BYTES", 1000)
    campaign = _campaign(world)
    first = _create(world, campaign, command_id="cmd-0123456789abcdef", size=1000)
    assert _create(world, campaign, command_id="cmd-0123456789abcdef", size=1000) == first
    with pytest.raises(QuotaExceeded):
        _create(world, campaign, command_id="cmd-other-0123456789", size=1)
    assert world.usage(campaign) == (1000, 1)


def test_a_replay_whose_first_asset_is_a_tombstone_is_missing(world: World) -> None:
    campaign = _campaign(world)
    first = _create(world, campaign, command_id="cmd-0123456789abcdef")
    _act(world, campaign, first.id, "delete")
    with pytest.raises(MissingParent) as refused:
        _create(world, campaign, command_id="cmd-0123456789abcdef")
    assert str(refused.value) == MISSING


@pytest.mark.parametrize("elsewhere", ["archived", "never minted"])
def test_a_replay_is_answered_only_from_its_own_campaign(world: World, elsewhere: str) -> None:
    """L-7 step 4 reads the replay "by campaign and command id". The same GM,
    the same command id, a create that makes nothing in campaign B: B's answer
    is MissingParent (SEC-3's "another campaign's"), never campaign A's asset."""
    first_campaign = _campaign(world)
    first = _create(world, first_campaign, command_id="cmd-0123456789abcdef")
    target = "cmp_" + "q" * 22
    if elsewhere == "archived":
        target = _campaign(world, name="Aubade")
        with world.db.transaction() as unit:
            assert world.campaigns.set_archived(unit, target, owner_id=world.owner, archived=True)
    with pytest.raises(MissingParent) as refused:
        _create(world, target, command_id="cmd-0123456789abcdef")
    assert str(refused.value) == MISSING
    assert set(world.rows()) == {first.id}
    assert world.usage(first_campaign) == (1000, 1)


def test_one_command_id_in_two_live_campaigns_makes_two_assets_each_replayed_in_its_own(world: World) -> None:
    """The command index is per campaign: the second campaign's create is a new
    asset, not a replay, and each campaign's replay returns its own asset."""
    first_campaign, second_campaign = _campaign(world), _campaign(world, name="Aubade")
    in_first = _create(world, first_campaign, command_id="cmd-0123456789abcdef")
    in_second = _create(world, second_campaign, command_id="cmd-0123456789abcdef", size=700)
    assert in_second.id != in_first.id
    assert (in_first.campaign_id, in_second.campaign_id) == (first_campaign, second_campaign)
    assert world.usage(first_campaign) == (1000, 1) and world.usage(second_campaign) == (700, 1)
    assert _create(world, second_campaign, command_id="cmd-0123456789abcdef", size=5) == in_second
    assert _create(world, first_campaign, command_id="cmd-0123456789abcdef", size=5) == in_first
    assert set(world.rows()) == {in_first.id, in_second.id}


def test_a_quota_refusal_leaves_nothing_and_the_transaction_goes_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_store, "QUOTA_ASSETS", 1)
    campaign = _campaign(world)
    other = _campaign(world)
    kept = _create(world, campaign)
    before = (world.rows(), world.jobs())
    with world.db.transaction() as unit:
        with pytest.raises(QuotaExceeded) as refused:
            world.assets.create(
                unit, campaign, owner_id=world.owner, kind=AssetKind.IMAGE, media_type="image/png",
                size_bytes=10, alt=ALT, now=T0,
            )
        assert str(refused.value) == QuotaExceeded.MESSAGE
        # The caller's transaction is still usable: another statement succeeds.
        elsewhere = world.assets.create(
            unit, other, owner_id=world.owner, kind=AssetKind.IMAGE, media_type="image/png",
            size_bytes=10, alt=ALT, now=T0,
        )
    rows, jobs = world.rows(), world.jobs()
    assert set(rows) == set(before[0]) | {elsewhere.id} and kept.id in rows
    assert len(jobs) == len(before[1]) + 1
    assert world.usage(campaign) == (1000, 1)


def test_an_archived_missing_or_foreign_campaign_is_missing(world: World) -> None:
    archived = _campaign(world)
    with world.db.transaction() as unit:
        assert world.campaigns.set_archived(unit, archived, owner_id=world.owner, archived=True)
    foreign = _campaign(world, owner=world.other_owner)
    for campaign in (archived, foreign, "cmp_" + "q" * 22):
        with pytest.raises(MissingParent) as refused:
            _create(world, campaign)
        assert str(refused.value) == MISSING
    assert world.rows() == {} and world.jobs() == []


def test_reads_and_deletion_still_work_on_an_archived_campaign(world: World) -> None:
    campaign = _campaign(world)
    record = _in_state(world, campaign, "ready")
    with world.db.transaction() as unit:
        world.campaigns.set_archived(unit, campaign, owner_id=world.owner, archived=True)
    with world.db.transaction() as unit:
        assert world.assets.get(unit, campaign, record.id, owner_id=world.owner) is not None
        assert world.assets.resolve_ready(unit, campaign, record.id, owner_id=world.owner) is not None
    assert isinstance(_act(world, campaign, record.id, "delete"), int)


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"kind": "video"}, ValueError),
        ({"media_type": "image/gif"}, ValueError),
        ({"media_type": "audio/mpeg"}, ValueError),
        ({"size_bytes": 0}, ValueError),
        ({"size_bytes": 10_000_001}, ValueError),
        ({"size_bytes": True}, TypeError),
        ({"alt": None}, ValueError),
        ({"alt": "x" * 301}, ValueError),
        ({"alt": "two\nlines"}, ValueError),
        ({"alt": "nul" + chr(0)}, ValueError),
        ({"command_id": "short"}, ValueError),
        ({"owner_id": True}, TypeError),
        ({"campaign_id": 5}, TypeError),
    ],
)
def test_a_declaration_the_contract_refuses_is_refused_by_field_before_any_statement(
    world: World, change: dict[str, Any], refusal: type[Exception]
) -> None:
    campaign = _campaign(world)
    arguments: dict[str, Any] = {
        "campaign_id": campaign, "owner_id": world.owner, "kind": AssetKind.IMAGE, "media_type": "image/png",
        "size_bytes": 1000, "alt": ALT, "command_id": None,
    } | change
    target = arguments.pop("campaign_id")
    with world.db.transaction() as unit:
        with pytest.raises(refusal) as refused:
            world.assets.create(unit, target, now=T0, **arguments)
        assert unit.transaction_bounds == [], "refused before the transaction was touched"
    for value in change.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in str(refused.value)
    assert world.rows() == {}


# ── AC-7: anything that is not the caller's gets the identical answer ────────


def test_anything_not_the_callers_is_one_identical_answer_to_every_read_and_mutator(world: World) -> None:
    campaign = _campaign(world)
    other_campaign = _campaign(world)
    foreign_campaign = _campaign(world, owner=world.other_owner)
    mine = _in_state(world, campaign, "processing")
    foreign = _in_state(world, foreign_campaign, "processing", owner=world.other_owner)
    tombstone = _in_state(world, campaign, "deleted")
    cases = [
        ("another owner's", foreign_campaign, foreign.id, world.owner),
        ("my asset, asked as another owner", campaign, mine.id, world.other_owner),
        ("my asset, under another campaign", other_campaign, mine.id, world.owner),
        ("never minted", campaign, NEVER_MINTED, world.owner),
        ("deleted", campaign, tombstone.id, world.owner),
    ]
    for label, campaign_id, asset_id, owner in cases:
        with world.db.transaction() as unit:
            assert world.assets.get(unit, campaign_id, asset_id, owner_id=owner) is None, label
            assert world.assets.resolve_ready(unit, campaign_id, asset_id, owner_id=owner) is None, label
            assert world.assets.hold(unit, campaign_id, asset_id, owner_id=owner) is None, label
        for change in MUTATORS:
            with pytest.raises(MissingParent) as refused:
                _act(world, campaign_id, asset_id, change, owner=owner)
            assert str(refused.value) == MISSING, (label, change)
            assert not isinstance(refused.value, IllegalTransition)


def test_anything_not_the_callers_campaign_has_no_usage(world: World) -> None:
    """`usage` is a read like any other: a campaign that is not the caller's
    answers None, whoever owns it and whatever it holds."""
    campaign = _campaign(world)
    foreign_campaign = _campaign(world, owner=world.other_owner)
    _create(world, campaign, size=600)
    _create(world, foreign_campaign, owner=world.other_owner)
    cases = [
        ("another owner's", foreign_campaign, world.owner),
        ("mine, asked as another owner", campaign, world.other_owner),
        ("never minted", "cmp_" + "q" * 22, world.owner),
    ]
    with world.db.transaction() as unit:
        for label, campaign_id, owner in cases:
            assert world.assets.usage(unit, campaign_id, owner_id=owner) is None, label
    # The controls: each owner still reads their own campaign's usage.
    assert world.usage(campaign) == (600, 1)
    assert world.usage(foreign_campaign, owner=world.other_owner) == (1000, 1)


# ── AC-7: the full transition matrix ─────────────────────────────────────────


@pytest.mark.parametrize("kind", [AssetKind.IMAGE, AssetKind.AUDIO])
@pytest.mark.parametrize("state", ["uploading", "processing", "ready", "failed"])
@pytest.mark.parametrize("change", MUTATORS)
def test_every_legal_transition_succeeds_and_every_illegal_one_is_named(
    world: World, kind: AssetKind, state: str, change: str
) -> None:
    campaign = _campaign(world)
    record = _in_state(world, campaign, state, kind)
    if state in LEGAL[change]:
        _act(world, campaign, record.id, change)
    else:
        with pytest.raises(IllegalTransition) as refused:
            _act(world, campaign, record.id, change)
        assert str(refused.value) == IllegalTransition.MESSAGE
        assert world.rows()[record.id].state == state, "a refusal changes nothing"


@pytest.mark.parametrize("change", MUTATORS)
def test_every_transition_of_a_tombstone_is_missing_never_illegal(world: World, change: str) -> None:
    campaign = _campaign(world)
    record = _in_state(world, campaign, "deleted")
    with pytest.raises(MissingParent) as refused:
        _act(world, campaign, record.id, change)
    assert not isinstance(refused.value, IllegalTransition) and str(refused.value) == MISSING


def test_every_entry_into_uploading_or_processing_seeds_its_own_sweep(world: World) -> None:
    campaign = _campaign(world)
    record = _create(world, campaign)
    _act(world, campaign, record.id, "start_processing", now=T0 + timedelta(minutes=5))
    _act(world, campaign, record.id, "return_to_uploading", now=T0 + timedelta(minutes=7))
    sweeps = [(payload, run_after) for _, kind, payload, _, run_after in world.jobs() if kind == SWEEP_JOB]
    assert sweeps == [
        ({"asset_id": record.id}, T0 + asset_store.UPLOADING_BOUND),
        ({"asset_id": record.id}, T0 + timedelta(minutes=5) + asset_store.PROCESSING_BOUND),
        ({"asset_id": record.id}, T0 + timedelta(minutes=7) + asset_store.UPLOADING_BOUND),
    ]


# ── AC-7: the quota ──────────────────────────────────────────────────────────


def test_going_ready_replaces_the_reservation_and_failing_releases_it(world: World) -> None:
    campaign = _campaign(world)
    record = _in_state(world, campaign, "processing", size=1000)
    ready = _act(world, campaign, record.id, "mark_ready", size=700)
    assert (ready.state, ready.size_bytes, ready.media_type) == (AssetState.READY, 700, "image/png")
    assert world.usage(campaign) == (700, 1)
    doomed = _create(world, campaign, size=300)
    assert world.usage(campaign) == (1000, 2)
    failed = _act(world, campaign, doomed.id, "mark_failed")
    assert (failed.state, failed.failure) == (AssetState.FAILED, AssetFailure.UNREADABLE)
    assert world.usage(campaign) == (700, 1)


@pytest.mark.parametrize("kind", [AssetKind.IMAGE, AssetKind.AUDIO])
def test_a_ready_that_no_longer_fits_becomes_failed_quota_exceeded_with_nothing_measured(
    world: World, monkeypatch: pytest.MonkeyPatch, kind: AssetKind
) -> None:
    monkeypatch.setattr(asset_store, "QUOTA_BYTES", 2000)
    campaign = _campaign(world)
    record = _in_state(world, campaign, "processing", kind, size=1000)
    _create(world, campaign, size=900)
    result = _act(world, campaign, record.id, "mark_ready", size=1200)
    assert (result.state, result.failure) == (AssetState.FAILED, AssetFailure.QUOTA_EXCEEDED)
    assert (result.media_type, result.size_bytes, result.width, result.height, result.duration_ms) == (None,) * 5
    assert world.usage(campaign) == (900, 1)


# ── AC-7: every row a read returns is a valid wire Asset ─────────────────────


@pytest.mark.parametrize("kind", [AssetKind.IMAGE, AssetKind.AUDIO])
def test_every_row_a_read_returns_converts_to_a_valid_wire_asset(world: World, kind: AssetKind) -> None:
    campaign = _campaign(world)
    for state in ("uploading", "processing", "ready", "failed"):
        record = _in_state(world, campaign, state, kind)
        with world.db.transaction() as unit:
            read = world.assets.get(unit, campaign, record.id, owner_id=world.owner)
        assert read is not None and read.state.value == state
        wire = read.to_wire()
        assert Asset.model_validate_json(wire.model_dump_json()) == wire
        assert wire.asset_id == record.id and wire.campaign_id == campaign
        if state == "ready":
            assert (wire.media_type, wire.size_bytes) == (_measured(kind).media_type, 900)
        else:
            assert (wire.media_type, wire.size_bytes) == (record.declared_media_type, 1000)


# ── AC-7: the tombstone, and resolving ───────────────────────────────────────


def test_the_tombstone_clears_its_text_releases_and_enqueues_one_delete(world: World) -> None:
    campaign = _campaign(world)
    record = _in_state(world, campaign, "ready")
    job_id = _act(world, campaign, record.id, "delete")
    stone = world.rows()[record.id]
    assert stone.state == "deleted"
    assert (stone.alt, stone.failure, stone.media_type, stone.size_bytes, stone.width, stone.height) == (None,) * 6
    assert (stone.object_key, stone.tmp_key) == (record.object_key, record.tmp_key), "keys never change"
    assert world.usage(campaign) == (0, 0)
    deletes = [(i, payload, dedupe) for i, kind, payload, dedupe, _ in world.jobs() if kind == DELETE_JOB]
    assert deletes == [
        (job_id, {"asset_id": record.id, "object_key": record.object_key, "tmp_key": record.tmp_key}, record.id)
    ]
    with world.db.transaction() as unit:
        assert world.assets.get(unit, campaign, record.id, owner_id=world.owner) is None
        assert world.assets.resolve_ready(unit, campaign, record.id, owner_id=world.owner) is None


def test_resolve_ready_answers_for_a_ready_asset_only(world: World) -> None:
    campaign = _campaign(world)
    for state in ("uploading", "processing", "failed", "deleted"):
        record = _in_state(world, campaign, state)
        with world.db.transaction() as unit:
            assert world.assets.resolve_ready(unit, campaign, record.id, owner_id=world.owner) is None, state
    ready = _in_state(world, campaign, "ready", AssetKind.AUDIO)
    with world.db.transaction() as unit:
        found = world.assets.resolve_ready(unit, campaign, ready.id, owner_id=world.owner)
    assert found is not None
    assert (found.object_key, found.media_type, found.size_bytes) == (ready.object_key, "audio/mpeg", 900)
    assert ready.object_key not in repr(found)


# ── AC-7: the campaign primitive ─────────────────────────────────────────────


def test_the_primitive_removes_every_row_and_enqueues_one_job_per_live_row(world: World) -> None:
    campaign = _campaign(world)
    sibling = _campaign(world)
    records = {state: _in_state(world, campaign, state) for state in PATHS}
    untouched = _in_state(world, sibling, "ready")
    before = {i for i, kind, *_ in world.jobs() if kind == DELETE_JOB}
    with world.db.transaction() as unit:
        job_ids = world.assets.delete_campaign_assets(unit, campaign, owner_id=world.owner, now=T0)
    assert set(world.rows()) == {untouched.id}
    new_jobs = {i: payload for i, kind, payload, _, _ in world.jobs() if kind == DELETE_JOB and i not in before}
    assert set(job_ids) == set(new_jobs)
    assert sorted(p["asset_id"] for p in new_jobs.values()) == sorted(
        r.id for state, r in records.items() if state != "deleted"
    ), "one job per row that was not already a tombstone"
    assert world.usage(campaign) == (0, 0)
    assert world.usage(sibling) == (900, 1)
    with world.db.transaction() as unit:
        assert world.assets.delete_campaign_assets(unit, campaign, owner_id=world.owner, now=T0) == []
    assert set(world.rows()) == {untouched.id}


def test_the_primitive_is_owner_scoped(world: World) -> None:
    campaign = _campaign(world)
    record = _create(world, campaign)
    with world.db.transaction() as unit:
        assert world.assets.delete_campaign_assets(unit, campaign, owner_id=world.other_owner, now=T0) == []
    assert set(world.rows()) == {record.id}


# ── AC-7: visibility ─────────────────────────────────────────────────────────


class _Rollback(Exception):
    pass


def test_a_rollback_leaves_nothing_in_the_store_or_the_queue(world: World) -> None:
    campaign = _campaign(world)
    kept = _create(world, campaign)
    rows, jobs = world.rows(), world.jobs()
    with pytest.raises(_Rollback):
        with world.db.transaction() as unit:
            world.assets.create(
                unit, campaign, owner_id=world.owner, kind=AssetKind.IMAGE, media_type="image/png",
                size_bytes=5, alt=ALT, now=T0,
            )
            world.assets.start_processing(unit, campaign, kept.id, owner_id=world.owner, now=T0)
            world.assets.delete_campaign_assets(unit, campaign, owner_id=world.owner, now=T0)
            raise _Rollback()
    assert (world.rows(), world.jobs()) == (rows, jobs)


def test_a_write_is_visible_to_other_readers_only_at_commit(world: World) -> None:
    campaign = _campaign(world)
    with world.db.transaction() as unit:
        record = world.assets.create(
            unit, campaign, owner_id=world.owner, kind=AssetKind.IMAGE, media_type="image/png",
            size_bytes=5, alt=ALT, now=T0,
        )
        with world.db.transaction() as other:
            assert world.assets.get(other, campaign, record.id, owner_id=world.owner) is None
        assert world.assets.get(unit, campaign, record.id, owner_id=world.owner) == record
    with world.db.transaction() as other:
        assert world.assets.get(other, campaign, record.id, owner_id=world.owner) == record


# ── AC-7 / L-16: the twin refuses what the CHECKs refuse ─────────────────────


def valid_row(kind: AssetKind, state: str, n: int = 1) -> AssetRow:
    image = kind is AssetKind.IMAGE
    ready = state == "ready"
    return AssetRow(
        id="ast_" + f"{n:022d}",
        campaign_id="cmp_" + "a" * 22,
        kind=kind,
        state=state,
        declared_media_type="image/png" if image else "audio/ogg",
        declared_size_bytes=1000,
        media_type=("image/png" if image else "audio/mpeg") if ready else None,
        size_bytes=900 if ready else None,
        width=40 if image and ready else None,
        height=30 if image and ready else None,
        duration_ms=12_000 if (not image) and ready else None,
        alt=ALT if image and state != "deleted" else None,
        failure=AssetFailure.TOO_LARGE if state == "failed" else None,
        object_key=f"assets/{n:032x}",
        tmp_key=f"tmp/{n:032x}",
        created_command_id=None,
        created_at=T0,
        updated_at=T0,
        state_changed_at=T0,
    )


FORBIDDEN = [
    ("a tombstone that carries alt", AssetKind.IMAGE, "deleted", {"alt": "x"}),
    ("a tombstone that carries a measured value", AssetKind.IMAGE, "deleted", {"size_bytes": 10}),
    ("a failed row with no failure", AssetKind.IMAGE, "failed", {"failure": None}),
    ("a failed row with a measured value", AssetKind.IMAGE, "failed", {"media_type": "image/png"}),
    ("a ready image with no dimensions", AssetKind.IMAGE, "ready", {"width": None, "height": None}),
    ("a ready image with one dimension", AssetKind.IMAGE, "ready", {"height": None}),
    ("a ready image with a duration", AssetKind.IMAGE, "ready", {"duration_ms": 1000}),
    ("a ready audio row with a width", AssetKind.AUDIO, "ready", {"width": 10}),
    ("a ready audio row with a height", AssetKind.AUDIO, "ready", {"height": 10}),
    ("a ready audio row that is not mp3", AssetKind.AUDIO, "ready", {"media_type": "audio/ogg"}),
    ("a ready image whose type is not its declared one", AssetKind.IMAGE, "ready", {"media_type": "image/jpeg"}),
    ("an uploading row with a size", AssetKind.IMAGE, "uploading", {"size_bytes": 10}),
    ("a malformed object key", AssetKind.IMAGE, "uploading", {"object_key": "assets/XYZ"}),
    ("a malformed tmp key", AssetKind.IMAGE, "uploading", {"tmp_key": "tmp/" + "A" * 32}),
    ("an audio row with alt text", AssetKind.AUDIO, "uploading", {"alt": "x"}),
    ("an image with no alt text", AssetKind.IMAGE, "processing", {"alt": None}),
    ("a declared type its kind refuses", AssetKind.IMAGE, "uploading", {"declared_media_type": "image/gif"}),
    ("a declared size over the cap", AssetKind.IMAGE, "uploading", {"declared_size_bytes": 10_000_001}),
    ("an image over the pixel cap", AssetKind.IMAGE, "ready", {"width": 8192, "height": 8192}),
    ("a state the schema does not know", AssetKind.IMAGE, "archived", {}),
]


@pytest.mark.parametrize(("label", "kind", "state", "overrides"), FORBIDDEN, ids=[f[0] for f in FORBIDDEN])
def test_the_twin_refuses_what_the_checks_refuse(label: str, kind: AssetKind, state: str,
                                                 overrides: dict[str, Any]) -> None:
    with pytest.raises(AssetRowRefused):
        check_row(replace(valid_row(kind, state if state != "archived" else "uploading"), state=state, **overrides))


@pytest.mark.parametrize("kind", [AssetKind.IMAGE, AssetKind.AUDIO])
@pytest.mark.parametrize("state", ["uploading", "processing", "ready", "failed", "deleted"])
def test_the_twin_accepts_every_valid_row(kind: AssetKind, state: str) -> None:
    assert check_row(valid_row(kind, state)) == valid_row(kind, state)


def test_the_twin_refuses_a_duplicate_key_or_command_before_staging_anything() -> None:
    db = InMemoryDatabase()
    rows = asset_rows(db)
    with db.transaction() as unit:
        first = replace(valid_row(AssetKind.IMAGE, "uploading", 1), created_command_id="c" * 16)
        stage_row(rows, unit, first, new=True)
    for clash in (
        replace(valid_row(AssetKind.IMAGE, "uploading", 2), object_key=f"assets/{1:032x}"),
        replace(valid_row(AssetKind.IMAGE, "uploading", 2), tmp_key=f"tmp/{1:032x}"),
        replace(valid_row(AssetKind.IMAGE, "uploading", 2), created_command_id="c" * 16),
        valid_row(AssetKind.IMAGE, "uploading", 1),
    ):
        with db.transaction() as unit:
            with pytest.raises(AssetRowRefused):
                stage_row(rows, unit, clash, new=True)
            assert visible_rows(rows, unit).keys() == {valid_row(AssetKind.IMAGE, "uploading", 1).id}


# ── AC-13: bounds and lock order, in both worlds ─────────────────────────────


def test_every_method_that_locks_bounds_its_transaction_first(world: World) -> None:
    campaign = _campaign(world)
    record = _create(world, campaign)
    calls: list[tuple[str, Callable[[Any], object]]] = [
        ("create", lambda u: world.assets.create(
            u, campaign, owner_id=world.owner, kind=AssetKind.AUDIO, media_type="audio/wav",
            size_bytes=5, now=T0)),
        ("hold", lambda u: world.assets.hold(u, campaign, record.id, owner_id=world.owner)),
        ("start_processing", lambda u: world.assets.start_processing(u, campaign, record.id, owner_id=world.owner)),
        ("mark_ready", lambda u: world.assets.mark_ready(
            u, campaign, record.id, owner_id=world.owner, measured=_measured(AssetKind.IMAGE))),
        ("delete", lambda u: world.assets.delete(u, campaign, record.id, owner_id=world.owner, now=T0)),
        ("delete_campaign_assets", lambda u: world.assets.delete_campaign_assets(
            u, campaign, owner_id=world.owner, now=T0)),
    ]
    for name, call in calls:
        with world.db.transaction() as unit:
            call(unit)
            assert unit.transaction_bounds, f"{name} took a lock with no transaction bound"
            with pytest.raises(CampaignLockOrder):
                unit.lock_campaign(campaign, shared=True)


class _Recording:
    """A connection that keeps the text of every statement sent through it, in
    order, and passes everything else (the savepoint too) to the real one."""

    # justification: psycopg's Connection is generic over its row factory and
    # ships partial stubs; the repository's existing pattern for it is `Any`.
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, query: Any, params: Any = None, **options: Any) -> Any:
        self.statements.append(str(query))
        return self._conn.execute(query, params, **options)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


#: A statement that takes a row lock: an explicit locking clause, or a write.
_TAKES_A_LOCK = re.compile(r"\bFOR (NO KEY UPDATE|KEY SHARE|SHARE|UPDATE)\b|^\s*(INSERT|UPDATE|DELETE)\b")
_BOUND = "set_config('transaction_timeout'"


@needs_db
def test_every_method_that_locks_sends_its_bound_before_its_first_locking_statement(dsn: str) -> None:
    """AC-13's "a transaction bound before its first lock", read from the
    statements PostgreSQL is actually sent: the bound goes out before the first
    statement that takes a row lock, in every method that takes one."""
    world = _pg_world(dsn)
    campaign = _campaign(world)
    record, other = _create(world, campaign), _create(world, campaign)
    calls: list[tuple[str, Callable[[Any], object]]] = [
        ("create", lambda u: world.assets.create(
            u, campaign, owner_id=world.owner, kind=AssetKind.AUDIO, media_type="audio/wav",
            size_bytes=5, now=T0)),
        ("hold", lambda u: world.assets.hold(u, campaign, record.id, owner_id=world.owner)),
        ("start_processing", lambda u: world.assets.start_processing(u, campaign, record.id, owner_id=world.owner)),
        ("mark_ready", lambda u: world.assets.mark_ready(
            u, campaign, record.id, owner_id=world.owner, measured=_measured(AssetKind.IMAGE))),
        ("start_processing (other)", lambda u: world.assets.start_processing(
            u, campaign, other.id, owner_id=world.owner)),
        ("return_to_uploading", lambda u: world.assets.return_to_uploading(
            u, campaign, other.id, owner_id=world.owner)),
        ("mark_failed", lambda u: world.assets.mark_failed(
            u, campaign, other.id, owner_id=world.owner, failure=AssetFailure.UNREADABLE)),
        ("delete", lambda u: world.assets.delete(u, campaign, record.id, owner_id=world.owner, now=T0)),
        ("delete_campaign_assets", lambda u: world.assets.delete_campaign_assets(
            u, campaign, owner_id=world.owner, now=T0)),
    ]
    for name, call in calls:
        with world.db.transaction() as unit:
            recording = _Recording(unit.conn)
            unit.conn = recording
            call(unit)
        sent = recording.statements
        locks = [i for i, text in enumerate(sent) if _TAKES_A_LOCK.search(text)]
        bounds = [i for i, text in enumerate(sent) if _BOUND in text]
        assert locks, f"{name} sent no statement that takes a lock: {sent}"
        assert bounds, f"{name} never sent its transaction bound"
        assert bounds[0] < locks[0], f"{name} took a lock before its bound: {sent[: locks[0] + 1]}"


# ── AC-9 / AC-13: the source, read mechanically ──────────────────────────────


@dataclass(frozen=True)
class Statement:
    function: str
    text: str
    names: frozenset[str]
    line: int


def _statements(path: Path) -> list[Statement]:
    """The first argument of every `.execute(...)` call, with the function it
    sits in and the names it interpolates."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[Statement] = []

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, ast.ClassDef | ast.FunctionDef):
                inner = f"{where}.{child.name}" if where else child.name
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "execute":
                sql = child.args[0]
                if isinstance(sql, ast.JoinedStr):
                    text = "".join(
                        part.value if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}"
                        for part in sql.values
                    )
                    names = frozenset(
                        n.id for part in sql.values if isinstance(part, ast.FormattedValue)
                        for n in ast.walk(part.value) if isinstance(n, ast.Name)
                    )
                elif isinstance(sql, ast.Constant) and isinstance(sql.value, str):
                    text, names = sql.value, frozenset()
                else:
                    raise AssertionError(f"{path.name}:{child.lineno} executes SQL this check cannot read")
                found.append(Statement(inner, text, names, child.lineno))
            visit(child, inner)

    visit(tree, "")
    return found


STORE_SQL = _statements(SERVICE / "asset_store.py")
JOBS_SQL = _statements(SERVICE / "asset_jobs.py")
TABLES = ("campaign.assets", "campaign.media_usage", "campaign.campaigns")
FRAGMENTS = {"GM_CAMPAIGNS", "OWNER_CAMPAIGNS"}
PRIMITIVE = "PostgresAssetStore.delete_campaign_assets"


def test_every_gm_statement_interpolates_the_gm_fragment_and_only_the_primitive_the_owner_one() -> None:
    """By the NAME each statement interpolates: the two fragments are the same
    text until `yje.2.1`, so a text check would pass whichever was used."""
    touching = [s for s in STORE_SQL if any(table in s.text for table in TABLES)]
    gm = [s for s in touching if s.function != PRIMITIVE]
    primitive = [s for s in touching if s.function == PRIMITIVE]
    assert gm and primitive, "both sets must be non-empty, or this proves nothing"
    for statement in gm:
        assert statement.names & FRAGMENTS == {"GM_CAMPAIGNS"}, f"{statement.function}:{statement.line}"
    for statement in primitive:
        assert statement.names & FRAGMENTS == {"OWNER_CAMPAIGNS"}, f"{statement.function}:{statement.line}"
    assert _readers("OWNER_CAMPAIGNS") == {PRIMITIVE}, "nothing but the primitive reads the owner-only fragment"
    assert PRIMITIVE not in _readers("GM_CAMPAIGNS")


def _readers(name: str) -> set[str]:
    """Every function that reads module-level `name`."""
    tree = ast.parse((SERVICE / "asset_store.py").read_text(encoding="utf-8"))
    found: set[str] = set()

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, ast.ClassDef | ast.FunctionDef):
                inner = f"{where}.{child.name}" if where else child.name
            if isinstance(child, ast.Name) and child.id == name and isinstance(child.ctx, ast.Load):
                found.add(inner)
            visit(child, inner)

    visit(tree, "")
    return found


def test_the_asset_store_imports_no_object_store_and_no_job_handler() -> None:
    tree = ast.parse((SERVICE / "asset_store.py").read_text(encoding="utf-8"))
    imported = {
        (node.module or "") + ":" + ",".join(a.name for a in node.names)
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    assert not [name for name in imported if "media_objects" in name or "asset_jobs" in name], imported


def test_no_statement_takes_for_update_or_assigns_a_key() -> None:
    statements = STORE_SQL + JOBS_SQL
    assert statements
    for statement in statements:
        assert not re.search(r"\bFOR\s+UPDATE\b", statement.text), f"{statement.function}:{statement.line}"
        if statement.text.lstrip().upper().startswith("UPDATE"):
            assignments = statement.text.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            assert not re.search(r"\b(object_key|tmp_key)\s*=", assignments), f"{statement.function}"


def _calls(function: ast.FunctionDef) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
        for node in ast.walk(function) if isinstance(node, ast.Call)
    ]


def test_the_enqueue_is_last_in_every_method_that_enqueues() -> None:
    """RQ-3: no statement that locks a row or writes the usage runs after the
    first enqueue — directly or through a helper that runs one."""
    tree = ast.parse((SERVICE / "asset_store.py").read_text(encoding="utf-8"))
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    runs_sql = {"execute"}
    grew = True
    while grew:
        grew = False
        for function in functions:
            if function.name not in runs_sql and any(name in runs_sql for _, name in _calls(function)):
                runs_sql.add(function.name)
                grew = True
    enqueuers = {"enqueue"} | {
        f.name for f in functions if any(name == "enqueue" for _, name in _calls(f))
    }
    checked = 0
    for function in functions:
        calls = _calls(function)
        firsts = [line for line, name in calls if name in enqueuers and name != function.name]
        if not firsts:
            continue
        checked += 1
        late = [(line, name) for line, name in calls if name in runs_sql - enqueuers and line > min(firsts)]
        assert late == [], f"{function.name} runs {late} after its first enqueue"
    assert checked >= 5, "the methods that enqueue were not found"


# ── AC-10: the jobs, through the merged runner ───────────────────────────────


class Recording:
    """An object store that says what it was asked, may fail on cue, and
    refuses to be called while a database transaction is open. Each listing is
    also kept as (prefix, start_after, examined), so a test can count what one
    reconcile run examined and see which prefixes it walked."""

    def __init__(self, inner: InMemoryObjectStore, watched: Watched) -> None:
        self.inner = inner
        self.watched = watched
        self.calls: list[tuple[str, str]] = []
        self.listed: list[tuple[str, str | None, int]] = []
        self.fail_deletes = 0

    def _check(self, name: str, key: str) -> None:
        assert self.watched.open == 0, f"{name} was called with a database transaction open"
        self.calls.append((name, key))

    def put_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> int:
        self._check("put_stream", key)
        return self.inner.put_stream(key, chunks, max_bytes=max_bytes)

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        self._check("get_stream", key)
        return self.inner.get_stream(key, offset=offset, length=length)

    def stat_object(self, key: str) -> ObjectStat | None:
        self._check("stat_object", key)
        return self.inner.stat_object(key)

    def delete_object(self, key: str) -> None:
        self._check("delete_object", key)
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise ObjectStoreUnavailable()
        self.inner.delete_object(key)

    def list_objects(
        self, prefix: str, *, older_than: datetime, start_after: str | None = None, limit: int
    ) -> ObjectPage:
        self._check("list_objects", prefix)
        page = self.inner.list_objects(prefix, older_than=older_than, start_after=start_after, limit=limit)
        self.listed.append((prefix, start_after, page.examined))
        return page

    def reachable(self) -> bool:
        return self.inner.reachable()

    def deleted(self) -> list[str]:
        return [key for name, key in self.calls if name == "delete_object"]


@dataclass
class Jobs:
    runner: JobRunner
    clock: Clock
    objects: Recording
    memory: InMemoryObjectStore

    def put(self, key: str, data: bytes = b"bytes", *, at: datetime | None = None) -> None:
        self.memory._clock = lambda: at or self.clock()
        self.memory.put_stream(key, iter([data]), max_bytes=10_000_000)

    def keys(self) -> set[str]:
        return set(self.memory._objects)

    def run(self, until: datetime | None = None) -> JobRunResult:
        if until is not None:
            self.clock.now = until
        return self.runner.run_due(limit=100)


def _jobs(world: World, *, between_read_and_fence: Callable[[], None] | None = None,
          monotonic: Callable[[], float] = time.monotonic) -> Jobs:
    clock = Clock(T0)
    memory = InMemoryObjectStore(clock=clock)
    objects = Recording(memory, world.db)
    runner = JobRunner(world.queue, clock=clock, monotonic=monotonic)
    register_jobs(
        runner, db=world.db, queue=world.queue, objects=objects, system=world.system, clock=clock,
        between_read_and_fence=between_read_and_fence,
    )
    return Jobs(runner, clock, objects, memory)


def _ready_with_objects(world: World, jobs: Jobs, campaign: str) -> asset_store.AssetRecord:
    record = _in_state(world, campaign, "ready")
    jobs.put(record.tmp_key)
    jobs.put(record.object_key)
    return record


def _states(world: World, kind: str) -> list[tuple[str, int, str | None, bool]]:
    return [state for state in world.job_states() if state[0] == kind]


def test_every_kind_is_registered_and_none_is_ever_marked_dead(world: World) -> None:
    jobs = _jobs(world)
    handlers = jobs.runner._handlers
    assert {DELETE_JOB, SWEEP_JOB, RECONCILE_JOB} <= set(handlers)
    assert all(handlers[kind].max_attempts is None for kind in (DELETE_JOB, SWEEP_JOB, RECONCILE_JOB))


def test_the_delete_job_deletes_tmp_then_every_derivative_then_the_original_then_purges(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_jobs, "DERIVATIVE_PAGE", 2)
    jobs = _jobs(world)
    campaign = _campaign(world)
    record = _ready_with_objects(world, jobs, campaign)
    derivatives = [f"{record.object_key}/thumb-{n}" for n in range(5)]
    for key in derivatives:
        jobs.put(key)
    neighbour = _ready_with_objects(world, jobs, campaign)
    _act(world, campaign, record.id, "delete")
    jobs.run(T0 + timedelta(seconds=1))
    assert jobs.objects.deleted() == [record.tmp_key, *sorted(derivatives), record.object_key]
    assert jobs.keys() == {neighbour.tmp_key, neighbour.object_key}
    assert record.id not in world.rows(), "purged once the bytes are gone"
    assert [kind for kind, *_ in world.job_states()].count(DELETE_JOB) == 0


def test_the_delete_job_is_idempotent(world: World) -> None:
    jobs = _jobs(world)
    campaign = _campaign(world)
    record = _ready_with_objects(world, jobs, campaign)
    _act(world, campaign, record.id, "delete")
    jobs.run(T0 + timedelta(seconds=1))
    payload = {"asset_id": record.id, "object_key": record.object_key, "tmp_key": record.tmp_key}
    with world.db.transaction() as unit:
        world.queue.enqueue(unit, DELETE_JOB, payload, dedupe_key=record.id, now=T0 + timedelta(seconds=1))
    result = jobs.run(T0 + timedelta(seconds=2))
    assert (result.ran, result.failed) == (1, 0)
    assert jobs.keys() == set() and world.rows() == {}


def test_a_store_failure_fails_the_attempt_by_class_and_a_later_attempt_succeeds(world: World) -> None:
    jobs = _jobs(world)
    campaign = _campaign(world)
    record = _ready_with_objects(world, jobs, campaign)
    _act(world, campaign, record.id, "delete")
    jobs.objects.fail_deletes = 1
    result = jobs.run(T0 + timedelta(seconds=1))
    assert (result.ran, result.failed) == (1, 1)
    assert _states(world, DELETE_JOB) == [(DELETE_JOB, 1, "ObjectStoreUnavailable", False)]
    assert world.rows()[record.id].state == "deleted", "the row is the retry record"
    jobs.run(T0 + timedelta(minutes=5))
    assert _states(world, DELETE_JOB) == [] and world.rows() == {} and jobs.keys() == set()


def test_a_deadline_that_runs_out_part_way_raises_and_the_job_remains(world: World) -> None:
    ticks = iter([0.0, 0.0] + [100.0] * 50)
    jobs = _jobs(world, monotonic=lambda: next(ticks))
    campaign = _campaign(world)
    record = _ready_with_objects(world, jobs, campaign)
    _act(world, campaign, record.id, "delete")
    jobs.clock.now = T0 + timedelta(seconds=1)
    result = jobs.runner.run_due(limit=1, deadline_monotonic=50.0)
    assert (result.ran, result.failed) == (1, 1)
    assert _states(world, DELETE_JOB) == [(DELETE_JOB, 1, JobOutOfTime.__name__, False)]
    assert world.rows()[record.id].state == "deleted"
    assert record.object_key in jobs.keys(), "it stopped part-way and said so"


def test_a_delete_after_the_primitive_still_deletes_every_object(world: World) -> None:
    jobs = _jobs(world)
    campaign = _campaign(world)
    records = [_ready_with_objects(world, jobs, campaign) for _ in range(3)]
    with world.db.transaction() as unit:
        world.assets.delete_campaign_assets(unit, campaign, owner_id=world.owner, now=T0)
    assert world.rows() == {}
    jobs.run(T0 + timedelta(seconds=1))
    assert jobs.keys() == set()
    assert sorted(jobs.objects.deleted()) == sorted(k for r in records for k in (r.tmp_key, r.object_key))
    assert _states(world, DELETE_JOB) == []


def test_the_purge_removes_only_a_tombstone(world: World) -> None:
    """MS-3: the row is purged once its bytes are gone, and only while it is
    still the tombstone. A delete job that meets a live row (a stray payload)
    completes without ever removing that row."""
    jobs = _jobs(world)
    campaign = _campaign(world)
    record = _ready_with_objects(world, jobs, campaign)
    stray = asset_store.delete_payload(world.rows()[record.id])
    with world.db.transaction() as unit:
        world.queue.enqueue(unit, DELETE_JOB, stray, dedupe_key=record.id, now=T0)
    jobs.run(T0 + timedelta(seconds=1))
    assert _states(world, DELETE_JOB) == [], "the job completed"
    assert world.rows()[record.id].state == "ready", "the live row was never purged"
    assert world.usage(campaign) == (900, 1)


def test_the_jobs_statements_that_lock_bound_their_transaction_first(world: World) -> None:
    """AC-13 bullet 1 for the jobs module's two locking statements, in both
    worlds: `fail_timed_out` and `purge` bound the transaction before their
    first lock, and say a row lock is coming, so a later campaign lock in the
    same unit is refused. `Database.transaction()` sets no lock bound of its
    own, so without these the only bound is the lock holder's."""
    campaign = _campaign(world)
    stuck = _create(world, campaign)
    read_at = world.rows()[stuck.id].state_changed_at
    gone = _in_state(world, campaign, "deleted")
    calls: list[tuple[str, Callable[[UnitOfWork], bool]]] = [
        ("fail_timed_out", lambda u: world.system.fail_timed_out(
            u, stuck.id, state="uploading", state_changed_at=read_at, now=T0 + timedelta(hours=2))),
        ("purge", lambda u: world.system.purge(u, gone.id)),
    ]
    for name, call in calls:
        with world.db.transaction() as unit:
            assert call(unit), f"{name} changed its row, so it really locked one"
            assert unit.transaction_bounds, f"{name} took a lock with no transaction bound"
            with pytest.raises(CampaignLockOrder):
                unit.lock_campaign(campaign, shared=True)
    rows = world.rows()
    assert (rows[stuck.id].state, gone.id in rows) == ("failed", False)


@pytest.mark.parametrize(("state", "bound"), [("uploading", timedelta(hours=1)), ("processing", timedelta(minutes=10))])
def test_the_sweep_fails_a_stuck_row_timed_out_releases_and_deletes(
    world: World, state: str, bound: timedelta
) -> None:
    jobs = _jobs(world)
    campaign = _campaign(world)
    record = _in_state(world, campaign, state)
    jobs.put(record.tmp_key)
    jobs.put(record.object_key)
    assert world.usage(campaign) == (1000, 1)
    jobs.run(T0 + bound - timedelta(seconds=1))
    assert world.rows()[record.id].state == state, "not due yet"
    jobs.run(T0 + bound + timedelta(seconds=1))
    swept = world.rows()[record.id]
    assert (swept.state, swept.failure) == ("failed", AssetFailure.TIMED_OUT)
    assert world.usage(campaign) == (0, 0)
    assert jobs.keys() == set()
    assert sorted(jobs.objects.deleted()) == sorted([record.tmp_key, record.object_key])


def test_the_sweep_leaves_a_ready_a_deleted_a_missing_and_a_fresh_row_alone(world: World) -> None:
    jobs = _jobs(world)
    campaign = _campaign(world)
    ready = _ready_with_objects(world, jobs, campaign)
    gone = _in_state(world, campaign, "deleted")
    fresh = _create(world, campaign, now=T0 + timedelta(hours=5))
    with world.db.transaction() as unit:
        sweeps = [
            world.queue.enqueue(unit, SWEEP_JOB, {"asset_id": asset_id}, now=T0 + timedelta(hours=5))
            for asset_id in (ready.id, gone.id, fresh.id, NEVER_MINTED)
        ]
    jobs.clock.now = T0 + timedelta(hours=5, seconds=1)
    for sweep in sweeps:  # these four only: the tombstone's own delete job would purge it
        assert jobs.runner.run_job(sweep).failed == 0
    rows = world.rows()
    assert (rows[ready.id].state, rows[gone.id].state, rows[fresh.id].state) == ("ready", "deleted", "uploading")
    assert not {ready.tmp_key, ready.object_key} & set(jobs.objects.deleted())
    assert {ready.tmp_key, ready.object_key} <= jobs.keys()


def test_a_return_to_uploading_gets_a_fresh_deadline_and_the_old_sweeps_do_nothing(world: World) -> None:
    jobs = _jobs(world)
    campaign = _campaign(world)
    record = _create(world, campaign)
    _act(world, campaign, record.id, "start_processing", now=T0 + timedelta(minutes=30))
    _act(world, campaign, record.id, "return_to_uploading", now=T0 + timedelta(minutes=35))
    jobs.run(T0 + timedelta(minutes=61))
    assert world.rows()[record.id].state == "uploading", "both older sweeps read a row that re-entered later"
    jobs.run(T0 + timedelta(minutes=96))
    assert world.rows()[record.id].state == "failed"


def test_a_lost_fence_deletes_nothing(world: World) -> None:
    """AC-8's second form, in both worlds: the sweep read the row before `ready`
    committed and writes after. The fence changes nothing, so the handler owns
    nothing — least of all the processed original."""
    campaign = _campaign(world)
    record = _in_state(world, campaign, "processing")

    def ready_meanwhile() -> None:
        _act(world, campaign, record.id, "mark_ready", size=700, now=T0 + timedelta(minutes=11))

    jobs = _jobs(world, between_read_and_fence=ready_meanwhile)
    jobs.put(record.object_key, b"processed original")
    jobs.run(T0 + timedelta(minutes=11))
    row = world.rows()[record.id]
    assert (row.state, row.size_bytes) == ("ready", 700)
    assert world.usage(campaign) == (700, 1), "the real-size reservation survives"
    assert jobs.objects.deleted() == [] and record.object_key in jobs.keys()


def test_a_fresh_entry_into_the_same_state_between_read_and_fence_wins(world: World) -> None:
    """The fence names the moment the sweep read, not only the state: a row that
    went back to `uploading` and on to `processing` again after the sweep read
    it is in the same state, but not the entry this sweep was due for."""
    campaign = _campaign(world)
    record = _in_state(world, campaign, "processing")
    again = T0 + timedelta(minutes=11)

    def reentered_meanwhile() -> None:
        _act(world, campaign, record.id, "return_to_uploading", now=again)
        _act(world, campaign, record.id, "start_processing", now=again)

    jobs = _jobs(world, between_read_and_fence=reentered_meanwhile)
    jobs.put(record.tmp_key, b"upload")
    jobs.run(T0 + timedelta(minutes=11))
    row = world.rows()[record.id]
    assert (row.state, row.failure, row.state_changed_at) == ("processing", None, again)
    assert world.usage(campaign) == (1000, 1), "the reservation survives with its row"
    assert jobs.objects.deleted() == [] and record.tmp_key in jobs.keys()


def test_a_row_that_failed_quota_on_its_way_to_ready_loses_its_processed_object_to_its_sweep(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_store, "QUOTA_BYTES", 1500)
    jobs = _jobs(world)
    campaign = _campaign(world)
    record = _in_state(world, campaign, "processing", size=1000)
    _create(world, campaign, size=400)
    jobs.put(record.object_key, b"processed, then refused")
    assert _act(world, campaign, record.id, "mark_ready", size=1200).failure is AssetFailure.QUOTA_EXCEEDED
    jobs.run(T0 + timedelta(minutes=11))
    assert record.object_key not in jobs.keys()


def test_a_sweep_whose_store_failed_after_its_fence_deletes_both_objects_on_retry(world: World) -> None:
    """The fence won, then the store failed on the first delete (`tmp/`). The
    attempt fails by class alone, and the retry reads `failed`: a failed row's
    objects — the `tmp/` object as well as the original — are its to delete, so
    nothing is left for the reconcile or the bucket's lifecycle rule."""
    jobs = _jobs(world)
    campaign = _campaign(world)
    record = _in_state(world, campaign, "uploading")
    jobs.put(record.tmp_key)
    jobs.put(record.object_key)
    jobs.objects.fail_deletes = 1
    due = T0 + timedelta(hours=1, seconds=1)
    result = jobs.run(due)
    assert (result.ran, result.failed) == (1, 1)
    assert _states(world, SWEEP_JOB) == [(SWEEP_JOB, 1, ObjectStoreUnavailable.__name__, False)]
    swept = world.rows()[record.id]
    assert (swept.state, swept.failure) == ("failed", AssetFailure.TIMED_OUT)
    assert world.usage(campaign) == (0, 0), "the fence released the reservation"
    assert {record.tmp_key, record.object_key} <= jobs.keys(), "the failed attempt deleted nothing"
    jobs.run(due + timedelta(minutes=5))
    assert _states(world, SWEEP_JOB) == [], "the retry completed"
    assert jobs.keys() == set()
    assert world.usage(campaign) == (0, 0), "and released nothing a second time"


def _ready_rows(world: World, count: int) -> list[asset_store.AssetRecord]:
    campaign = _campaign(world)
    return [_in_state(world, campaign, "ready") for _ in range(count)]


def test_the_reconcile_deletes_old_orphans_and_keeps_what_a_live_row_names(world: World) -> None:
    jobs = _jobs(world)
    campaign = _campaign(world)
    old = T0 - timedelta(days=2)
    live = _in_state(world, campaign, "ready")
    uploading = _create(world, campaign)
    failed = _in_state(world, campaign, "failed")
    kept = {live.object_key, f"{live.object_key}/thumb", uploading.tmp_key}
    orphans = {"assets/" + "e" * 32, "tmp/" + "e" * 32, f"assets/{'e' * 32}/thumb", failed.tmp_key, failed.object_key}
    for key in kept | orphans:
        jobs.put(key, at=old)
    young = {"tmp/" + "d" * 32}
    jobs.put(next(iter(young)), at=T0)
    with world.db.transaction() as unit:
        enqueue_reconcile(unit, world.queue, now=T0)
    jobs.run(T0 + timedelta(seconds=1))
    assert jobs.keys() == kept | young
    assert _states(world, RECONCILE_JOB) == [], "one run was enough, so no successor"


def test_the_reconcile_chain_ends_even_when_every_object_is_referenced(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_jobs, "RECONCILE_BATCH", 4)
    jobs = _jobs(world)
    records = _ready_rows(world, asset_jobs.RECONCILE_BATCH + 1)
    for record in records:
        jobs.put(record.object_key, at=T0 - timedelta(days=2))
    with world.db.transaction() as unit:
        enqueue_reconcile(unit, world.queue, now=T0)
    first = jobs.runner.run_due(limit=1)
    assert (first.ran, first.failed) == (1, 0)
    successors = [p for _, kind, p, _, _ in world.jobs() if kind == RECONCILE_JOB]
    assert successors == [{"after": sorted(r.object_key for r in records)[asset_jobs.RECONCILE_BATCH - 1]}]
    second = jobs.runner.run_due(limit=1)
    assert (second.ran, second.failed) == (1, 0)
    assert [kind for _, kind, *_ in world.jobs()].count(RECONCILE_JOB) == 0, "the chain ended"
    assert jobs.objects.deleted() == []


def test_a_successor_resumes_strictly_after_its_cursor(world: World) -> None:
    jobs = _jobs(world)
    old = T0 - timedelta(days=2)
    before, cursor, after = "assets/" + "1" * 32, "assets/" + "5" * 32, "tmp/" + "9" * 32
    for key in (before, cursor, after):
        jobs.put(key, at=old)
    with world.db.transaction() as unit:
        world.queue.enqueue(unit, RECONCILE_JOB, {"after": cursor}, now=T0)
    jobs.run(T0 + timedelta(seconds=1))
    assert jobs.keys() == {before, cursor}, "left for the next pass"


Listing = tuple[str, str | None, int]


def _reconcile_chain(world: World, jobs: Jobs, *, most: int = 20) -> list[tuple[str | None, list[Listing]]]:
    """Run the reconcile chain one run at a time, at most `most` runs, and give
    each run's cursor with the listings it made. Every run, however it ends,
    examines at most `RECONCILE_BATCH` objects and chains at most one
    successor; the chain has ended when no reconcile job remains."""
    runs: list[tuple[str | None, list[Listing]]] = []
    for _ in range(most):
        pending = [payload for _, kind, payload, _, _ in world.jobs() if kind == RECONCILE_JOB]
        if not pending:
            return runs
        assert len(pending) == 1, "one run chains at most one successor"
        jobs.objects.listed.clear()
        result = jobs.runner.run_due(limit=1)
        assert (result.ran, result.failed) == (1, 0)
        listed = list(jobs.objects.listed)
        examined = sum(count for _, _, count in listed)
        assert examined <= asset_jobs.RECONCILE_BATCH, f"one run examined {examined} objects: {listed}"
        after = pending[0].get("after")
        runs.append((after if isinstance(after, str) else None, listed))
    raise AssertionError(f"the reconcile chain did not end within {most} runs")


def test_the_reconcile_walks_assets_then_tmp_once_and_the_chain_ends(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-10(b): a pass examines every object exactly once, `assets/` then
    `tmp/`; a successor whose cursor is in `tmp/` never walks `assets/` again;
    and the chain ends within floor(N / RECONCILE_BATCH) + 1 runs. Every object
    here is too young to be an orphan, so no delete moves the cursor along."""
    monkeypatch.setattr(asset_jobs, "RECONCILE_BATCH", 4)
    batch = asset_jobs.RECONCILE_BATCH
    jobs = _jobs(world)
    young = {f"assets/{n:032x}" for n in range(batch + 1)} | {f"tmp/{n:032x}" for n in range(batch + 1)}
    for key in young:
        jobs.put(key, at=T0)
    with world.db.transaction() as unit:
        enqueue_reconcile(unit, world.queue, now=T0)
    runs = _reconcile_chain(world, jobs)
    assert sum(count for _, listed in runs for _, _, count in listed) == len(young), "each examined once"
    assert len(runs) <= len(young) // batch + 1
    in_tmp = [listed for after, listed in runs if after is not None and after.startswith("tmp/")]
    assert in_tmp, "the pass reached a cursor in tmp/"
    assert all([prefix for prefix, _, _ in listed] == ["tmp/"] for listed in in_tmp), (
        "a cursor in tmp/ never walks assets/ again"
    )
    assert jobs.objects.deleted() == [] and jobs.keys() == young


def test_a_tmp_orphan_is_reached_when_assets_exactly_fills_a_batch(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `assets/` ends exactly at the budget the run cannot know whether
    `tmp/` is empty, so it chains one successor, and that successor reaches the
    `tmp/` orphan: the "+ 1" run the architecture doc counts."""
    monkeypatch.setattr(asset_jobs, "RECONCILE_BATCH", 4)
    jobs = _jobs(world)
    old = T0 - timedelta(days=2)
    records = _ready_rows(world, asset_jobs.RECONCILE_BATCH)
    for record in records:
        jobs.put(record.object_key, at=old)
    orphan = "tmp/" + "e" * 32
    jobs.put(orphan, at=old)
    with world.db.transaction() as unit:
        enqueue_reconcile(unit, world.queue, now=T0)
    runs = _reconcile_chain(world, jobs)
    assert [after for after, _ in runs] == [None, max(r.object_key for r in records)]
    assert jobs.objects.deleted() == [orphan]
    assert jobs.keys() == {record.object_key for record in records}


def test_dedupe_keys_and_payloads_are_exactly_what_the_rulings_say(world: World) -> None:
    campaign = _campaign(world, name="Canary campaign name")
    record = _in_state(world, campaign, "processing")
    _act(world, campaign, record.id, "delete")
    with world.db.transaction() as unit:
        enqueue_reconcile(unit, world.queue, now=T0)
    seen = world.jobs()
    shapes = {(kind, frozenset(payload), dedupe is not None) for _, kind, payload, dedupe, _ in seen}
    assert shapes == {
        (SWEEP_JOB, frozenset({"asset_id"}), False),
        (DELETE_JOB, frozenset({"asset_id", "object_key", "tmp_key"}), True),
        (RECONCILE_JOB, frozenset(), False),
    }
    for _, _kind, payload, dedupe, _ in seen:
        assert check_payload(payload) == payload
        assert campaign not in payload.values() and ALT not in payload.values()
        assert dedupe in (None, record.id)


# ── AC-11: the privacy canary (the threat model's T-12, slice a's leg) ───────

CANARY_ALT = "Canary-alt-7f3e The villain wears the red door"
CANARY_NAME = "Canary-campaign-9b2e"


def test_no_canary_reaches_a_log_an_exception_a_payload_or_a_key(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole segment: create, processing, tombstone, the delete job and the
    purge."""
    caplog.set_level(logging.DEBUG)
    jobs = _jobs(world)
    campaign = _campaign(world, name=CANARY_NAME)
    foreign = _campaign(world, owner=world.other_owner, name=CANARY_NAME)
    texts: list[str] = []
    record = _create(world, campaign, alt=CANARY_ALT)
    assert CANARY_ALT not in repr(record) and record.object_key not in repr(record)
    for attempt in (
        lambda: _create(world, foreign, alt=CANARY_ALT),
        lambda: _create(world, campaign, alt=CANARY_ALT + "\n" + CANARY_ALT),
        lambda: _create(world, campaign, alt=CANARY_ALT * 10),
        lambda: _act(world, campaign, record.id, "mark_ready"),
        lambda: _act(world, foreign, record.id, "delete"),
    ):
        with pytest.raises(Exception) as refused:
            attempt()
        texts.append(str(refused.value))
        texts.append(repr(refused.value))
    _act(world, campaign, record.id, "start_processing")
    jobs.put(record.tmp_key)
    jobs.put(record.object_key)
    _act(world, campaign, record.id, "delete")
    if world.dsn is None:
        claimed = world.queue.claim([DELETE_JOB, SWEEP_JOB], limit=10, lease_seconds=0, now=T0)
        payloads = [dict(job.payload) for job in claimed]
    else:
        payloads = [payload for _, _, payload, _, _ in world.jobs()]
    assert DELETE_JOB in [kind for _, kind, *_ in world.jobs()], "the segment ran as far as the tombstone"
    assert payloads, "no payload was read, so this proves nothing"
    for payload in payloads:
        assert check_payload(payload) == payload
    jobs.run(T0 + timedelta(minutes=20))
    assert record.id not in world.rows(), "the whole segment ran: create, processing, tombstone, delete, purge"
    sinks = [r.getMessage() for r in caplog.records] + [r.exc_text or "" for r in caplog.records] + texts
    sinks += [str(p) for p in payloads] + [key for _, key in jobs.objects.calls] + list(jobs.keys())
    sinks += [record.object_key, record.tmp_key]
    for canary in (CANARY_ALT, CANARY_NAME, "Canary"):
        assert not [s for s in sinks if canary in s], canary


def test_every_refusal_message_is_fixed() -> None:
    assert str(QuotaExceeded()) == QuotaExceeded.MESSAGE
    assert str(IllegalTransition()) == IllegalTransition.MESSAGE
    assert MISSING == "no such asset or campaign for that owner"


# ── AC-8: races on PostgreSQL ────────────────────────────────────────────────


def _someone_waits_on_a_lock(dsn: str, patience: float = PATIENCE) -> bool:
    """The server's own account of a blocked backend on this database."""
    deadline = time.monotonic() + patience
    with connect(dsn) as conn:
        while time.monotonic() < deadline:
            blocked = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if blocked:
                return True
            time.sleep(0.05)
    return False


class _Open:
    """Another thread's transaction: it runs `work`, then stays open until
    `release()`; its outcome — a value or an exception — is kept."""

    def __init__(self, db: Watched, work: Callable[[Any], object]) -> None:
        self.took, self.done, self.go = threading.Event(), threading.Event(), threading.Event()
        self.value: object = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                with db.transaction() as unit:
                    self.value = work(unit)
                    self.took.set()
                    self.go.wait(PATIENCE)
            except BaseException as exc:  # noqa: BLE001 - reported to the test thread
                self.error = exc
            finally:
                self.took.set()
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert self.took.wait(PATIENCE), "the first transaction never ran"
        assert self.error is None, f"the first transaction failed before the race began: {self.error!r}"

    def release(self) -> object:
        self.go.set()
        self.thread.join(PATIENCE)
        assert not self.thread.is_alive(), "the held transaction never finished"
        if self.error is not None:
            raise self.error
        return self.value


class _Waiter:
    """A second transaction, started in a thread; its outcome is kept."""

    def __init__(self, db: Watched, work: Callable[[Any], object]) -> None:
        self.value: object = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                with db.transaction() as unit:
                    self.value = work(unit)
            except BaseException as exc:  # noqa: BLE001 - the outcome under test
                self.error = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def outcome(self) -> object:
        self.thread.join(PATIENCE)
        assert not self.thread.is_alive(), "the waiting transaction never finished"
        assert not isinstance(self.error, psycopg.errors.DeadlockDetected), "a deadlock victim (40P01)"
        return self.error if self.error is not None else self.value


@pytest.fixture
def patient(dsn: str) -> World:
    return _pg_world(dsn, PATIENT)


def _create_in(world: World, campaign: str, **declared: Any) -> Callable[[Any], object]:
    arguments = {"kind": AssetKind.IMAGE, "media_type": "image/png", "size_bytes": 1000, "alt": ALT, "now": T0}
    arguments |= declared
    return lambda unit: world.assets.create(unit, campaign, owner_id=world.owner, **arguments)


@needs_db
def test_race_two_creates_that_together_pass_the_quota_leave_exactly_one(
    patient: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asset_store, "QUOTA_BYTES", 1500)
    campaign = _campaign(patient)
    first = _Open(patient.db, _create_in(patient, campaign))
    second = _Waiter(patient.db, _create_in(patient, campaign))
    assert patient.dsn and _someone_waits_on_a_lock(patient.dsn), "the second create never waited"
    won = first.release()
    lost = second.outcome()
    assert isinstance(lost, QuotaExceeded), lost
    assert set(patient.rows()) == {won.id}  # type: ignore[attr-defined]
    assert patient.usage(campaign) == (1000, 1)


@needs_db
@pytest.mark.parametrize("fills_the_quota", [False, True])
def test_race_two_creates_with_one_command_make_one_asset(
    patient: World, monkeypatch: pytest.MonkeyPatch, fills_the_quota: bool
) -> None:
    if fills_the_quota:
        monkeypatch.setattr(asset_store, "QUOTA_BYTES", 1000)
    campaign = _campaign(patient)
    first = _Open(patient.db, _create_in(patient, campaign, command_id="cmd-0123456789abcdef"))
    second = _Waiter(patient.db, _create_in(patient, campaign, command_id="cmd-0123456789abcdef"))
    assert patient.dsn and _someone_waits_on_a_lock(patient.dsn), "the replay never waited on the insert"
    made = first.release()
    replayed = second.outcome()
    assert not isinstance(replayed, BaseException), replayed
    assert replayed.id == made.id  # type: ignore[attr-defined]
    assert len(patient.rows()) == 1 and patient.usage(campaign) == (1000, 1)


@needs_db
@pytest.mark.parametrize("tombstone_first", [True, False])
def test_race_a_tombstone_against_ready(patient: World, tombstone_first: bool) -> None:
    campaign = _campaign(patient)
    record = _in_state(patient, campaign, "processing")

    def ready(unit: Any) -> object:
        return patient.assets.mark_ready(unit, campaign, record.id, owner_id=patient.owner,
                                         measured=_measured(AssetKind.IMAGE, 700), now=T0)

    def tombstone(unit: Any) -> object:
        return patient.assets.delete(unit, campaign, record.id, owner_id=patient.owner, now=T0)

    first = _Open(patient.db, tombstone if tombstone_first else ready)
    second = _Waiter(patient.db, ready if tombstone_first else tombstone)
    assert patient.dsn and _someone_waits_on_a_lock(patient.dsn)
    first.release()
    outcome = second.outcome()
    if tombstone_first:
        assert isinstance(outcome, MissingParent)
    else:
        assert isinstance(outcome, int)
    assert patient.rows()[record.id].state == "deleted"
    assert patient.usage(campaign) == (0, 0)
    assert [kind for _, kind, *_ in patient.jobs()].count(DELETE_JOB) == 1


@needs_db
@pytest.mark.parametrize("tombstone_first", [True, False])
def test_race_the_primitive_against_a_tombstone_of_the_same_asset(patient: World, tombstone_first: bool) -> None:
    campaign = _campaign(patient)
    record = _in_state(patient, campaign, "ready")
    other = _in_state(patient, campaign, "uploading")

    def tombstone(unit: Any) -> object:
        return patient.assets.delete(unit, campaign, record.id, owner_id=patient.owner, now=T0)

    def primitive(unit: Any) -> object:
        return patient.assets.delete_campaign_assets(unit, campaign, owner_id=patient.owner, now=T0)

    first = _Open(patient.db, tombstone if tombstone_first else primitive)
    second = _Waiter(patient.db, primitive if tombstone_first else tombstone)
    assert patient.dsn and _someone_waits_on_a_lock(patient.dsn)
    first.release()
    outcome = second.outcome()
    if not tombstone_first:
        assert isinstance(outcome, MissingParent)
    else:
        assert not isinstance(outcome, BaseException), outcome
    assert patient.rows() == {}
    deletes = [p["asset_id"] for _, kind, p, _, _ in patient.jobs() if kind == DELETE_JOB]
    assert sorted(deletes) == sorted([record.id, other.id]), "exactly one delete job per asset"
    assert patient.usage(campaign) == (0, 0)


@needs_db
def test_race_the_primitive_after_a_committed_create_removes_it_with_its_job(patient: World) -> None:
    campaign = _campaign(patient)
    created = _create(patient, campaign)
    with patient.db.transaction() as unit:
        jobs = patient.assets.delete_campaign_assets(unit, campaign, owner_id=patient.owner, now=T0)
    assert len(jobs) == 1 and patient.rows() == {}
    assert [p["asset_id"] for _, k, p, _, _ in patient.jobs() if k == DELETE_JOB] == [created.id]


@needs_db
def test_race_the_primitive_waits_on_an_uncommitted_create_and_subtracts_around_it(patient: World) -> None:
    campaign = _campaign(patient)
    doomed = _create(patient, campaign, size=300)
    creating = _Open(patient.db, _create_in(patient, campaign, size_bytes=1000))
    primitive = _Waiter(
        patient.db, lambda unit: patient.assets.delete_campaign_assets(unit, campaign, owner_id=patient.owner, now=T0)
    )
    assert patient.dsn and _someone_waits_on_a_lock(patient.dsn), "the primitive's usage UPDATE never waited"
    created = creating.release()
    removed = primitive.outcome()
    assert not isinstance(removed, BaseException), removed
    assert set(patient.rows()) == {created.id}  # type: ignore[attr-defined]
    assert patient.usage(campaign) == (1000, 1), "never zeroed under a live row"
    assert [p["asset_id"] for _, k, p, _, _ in patient.jobs() if k == DELETE_JOB] == [doomed.id]


@needs_db
def test_race_a_create_after_the_primitives_usage_update_waits_then_fits(patient: World) -> None:
    campaign = _campaign(patient)
    doomed = _create(patient, campaign, size=300)
    primitive = _Open(
        patient.db, lambda unit: patient.assets.delete_campaign_assets(unit, campaign, owner_id=patient.owner, now=T0)
    )
    creating = _Waiter(patient.db, _create_in(patient, campaign, size_bytes=1000))
    assert patient.dsn and _someone_waits_on_a_lock(patient.dsn), "the create's reservation never waited"
    primitive.release()
    created = creating.outcome()
    assert not isinstance(created, BaseException), created
    assert set(patient.rows()) == {created.id}  # type: ignore[attr-defined]
    assert patient.usage(campaign) == (1000, 1)
    assert [p["asset_id"] for _, k, p, _, _ in patient.jobs() if k == DELETE_JOB] == [doomed.id]


@needs_db
def test_race_a_create_against_the_campaigns_deletion_answers_missing(patient: World) -> None:
    campaign = _campaign(patient)
    assert patient.dsn is not None
    deleting = connect(patient.dsn, autocommit=False)
    try:
        deleting.execute("DELETE FROM campaign.campaigns WHERE id = %s", (campaign,))

        def create_then_go_on(unit: Any) -> object:
            try:
                patient.assets.create(unit, campaign, owner_id=patient.owner, kind=AssetKind.IMAGE,
                                      media_type="image/png", size_bytes=10, alt=ALT, now=T0)
            except MissingParent as refused:
                unit.conn.execute("SELECT 1").fetchone()
                return refused
            return None

        creating = _Waiter(patient.db, create_then_go_on)
        assert _someone_waits_on_a_lock(patient.dsn), "the create never waited on the campaign row"
        deleting.commit()
    finally:
        deleting.close()
    outcome = creating.outcome()
    assert isinstance(outcome, MissingParent), outcome
    assert not isinstance(outcome, psycopg.errors.ForeignKeyViolation)
    assert patient.rows() == {}


@needs_db
def test_race_the_sweeps_fence_waits_on_ready_and_then_changes_nothing(patient: World) -> None:
    campaign = _campaign(patient)
    record = _in_state(patient, campaign, "processing")
    jobs = _jobs(patient)
    jobs.put(record.object_key, b"processed original")
    ready = _Open(patient.db, lambda unit: patient.assets.mark_ready(
        unit, campaign, record.id, owner_id=patient.owner, measured=_measured(AssetKind.IMAGE, 700), now=T0))
    jobs.clock.now = T0 + timedelta(minutes=11)
    sweeping = threading.Thread(target=lambda: jobs.runner.run_due(limit=10), daemon=True)
    sweeping.start()
    assert patient.dsn and _someone_waits_on_a_lock(patient.dsn), "the fenced write never waited"
    ready.release()
    sweeping.join(PATIENCE)
    assert not sweeping.is_alive()
    row = patient.rows()[record.id]
    assert (row.state, row.size_bytes) == ("ready", 700)
    assert patient.usage(campaign) == (700, 1)
    assert jobs.objects.deleted() == [] and record.object_key in jobs.keys()
    assert all(error is None for _, _, error, _ in patient.job_states())

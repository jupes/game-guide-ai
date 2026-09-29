"""Session dividers against a real PostgreSQL, and against the twins (1kg.3.5).

The **shared behavioural suite**: one set of tests run twice, once against the
in-memory twins and once against PostgreSQL, so a rule the two could disagree
about is asserted of both. Those tests carry no mark and run anywhere; their
`postgres` parameter carries `needs_db` and skips without a server.

**Seeding is parity.** The timeline twin asks the `MessageStore` who owns a
conversation, while the divider twin reads the conversation twin's rows. So
each thread is made through `ConversationStore.create` and then
`MessageStore.claim_conversation` with the same id and owner, which is what
`/chat`'s first turn does in PostgreSQL, where both are one row. A row no store
would write (another GM's thread linked to this campaign, an id no timeline can
read) is written by raw SQL there and staged directly in the twin here.

What no twin can prove, and what the `needs_db` tests below are for: that 0017's
index itself refuses a second divider, that an `entry_id` collision raises
instead of being absorbed, that End waits for nothing it did not wait for
before, and that a held target row fails the job within its bound.

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_session_dividers_db.py -q
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service import session_dividers, timeline
from service.audit_log import InMemoryAuditLog, PostgresAuditLog
from service.campaign_store import InMemoryCampaignStore, PostgresCampaignStore, shared_rows
from service.conversation_store import Conversation, InMemoryConversationStore, PostgresConversationStore
from service.db import CampaignLockSettings, Database, InMemoryDatabase, PoolSettings
from service.history import InMemoryMessageStore, PostgresMessageStore
from service.jobs import InMemoryJobQueue, Job, JobContext, JobRunner, PostgresJobQueue
from service.reconciliation import RECONCILE_KIND, enqueue_reconciliation
from service.session_divider_store import (
    DividerIdTaken,
    InMemorySessionDividerStore,
    PostgresSessionDividerStore,
)
from service.session_dividers import DIVIDER_KIND, SessionDividers, divider_entry, enqueuer
from service.table_session_store import InMemoryTableSessionStore, PostgresTableSessionStore, no_slots
from service.table_sessions import FANOUT_BOUND, SESSION_LIFETIME, FanOutBound, TableSessions
from service.timeline_store import EntryInvalid, InMemoryTimelineStore, PostgresTimelineStore, new_entry_id
from service.workbench_contracts import CONTRACT_VERSION, SessionBoundary, SessionDividerEntry, entry_or_opaque

START, END = SessionBoundary.START, SessionBoundary.END
#: Short bounds, so a held row fails the divider job quickly.
BRISK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
#: How long a racing thread is waited for before the test calls it stuck.
STAMINA = 15
CANARY_NAME = "Qx7CampaignCanary"
CANARY_TITLE = "Qx7ThreadCanary"


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("dividers") as target:
        mig.migrate(target)
        yield target


@dataclass
class DividerWorld:
    """Every store the lifecycle and the divider job touch, over one database,
    and two GMs."""

    kind: str
    db: Any
    campaigns: Any
    sessions: Any
    audit: Any
    jobs: Any
    conversations: Any
    messages: Any
    timeline: Any
    store: Any
    owner: int
    other_owner: int
    dsn: str | None = None


def _postgres_world(target: str, settings: CampaignLockSettings = BRISK) -> DividerWorld:
    with connect(target) as conn:
        gms = [
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
            ).fetchone()[0]
            for email in ("gm@example.com", "other@example.com")
        ]
    database = Database(target, PoolSettings(sync_max=6, async_max=0, acquire_timeout_s=5), settings)
    return DividerWorld(
        "postgres",
        database,
        PostgresCampaignStore(),
        PostgresTableSessionStore(slot_clear=no_slots),
        PostgresAuditLog(),
        PostgresJobQueue(database),
        PostgresConversationStore(),
        PostgresMessageStore(db=database),
        PostgresTimelineStore(),
        PostgresSessionDividerStore(),
        int(gms[0]),
        int(gms[1]),
        target,
    )


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[DividerWorld]:
    if request.param == "fake":
        db: Any = InMemoryDatabase()
        messages = InMemoryMessageStore()
        yield DividerWorld(
            "fake",
            db,
            InMemoryCampaignStore(db),
            InMemoryTableSessionStore(db, slot_clear=no_slots),
            InMemoryAuditLog(),
            InMemoryJobQueue(db=db),
            InMemoryConversationStore(db),
            messages,
            InMemoryTimelineStore(db, messages=messages),
            InMemorySessionDividerStore(db),
            owner=1,
            other_owner=2,
        )
        return
    yield _postgres_world(request.getfixturevalue("dsn"))


# ── Seeding ──────────────────────────────────────────────────────────────────


def _command() -> str:
    return secrets.token_urlsafe(16)


def _campaign(world: DividerWorld, *, owner: int | None = None, name: str = "Nocturne") -> str:
    with world.db.transaction() as unit:
        return str(world.campaigns.create(unit, owner_id=world.owner if owner is None else owner, name=name).id)


def _thread(
    world: DividerWorld, campaign: str | None, at: datetime, *, owner: int | None = None, title: str | None = None
) -> str:
    """A thread as the product makes one: the conversation, then `/chat`'s claim."""
    who = world.owner if owner is None else owner
    with world.db.transaction() as unit:
        made = world.conversations.create(
            unit, owner_id=who, campaign_id=campaign, title=title, started_mode=None, now=at
        )
    world.messages.claim_conversation(made.id, who)
    return str(made.id)


def _raw_thread(world: DividerWorld, conversation_id: str, campaign: str, at: datetime, *, owner: int) -> None:
    """A row no store would write: raw SQL there, staged directly here."""
    if world.dsn is not None:
        with connect(world.dsn) as conn:
            conn.execute(
                "INSERT INTO chat.conversations (conversation_id, user_id, campaign_id, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (conversation_id, owner, campaign, at),
            )
        return
    with world.db.transaction() as unit:
        shared_rows(world.db, "conversations").add(
            unit,
            conversation_id,
            Conversation(conversation_id, owner, campaign, None, None, at, None, None),
        )
    world.messages.claim_conversation(conversation_id, owner)


def _archive(world: DividerWorld, conversation_id: str) -> None:
    with world.db.transaction() as unit:
        assert world.conversations.set_archived(unit, conversation_id, owner_id=world.owner, archived=True)


def _lifecycle(world: DividerWorld, *, db: Any = None, clock: Callable[[], datetime] | None = None) -> TableSessions:
    """The lifecycle as `service/app.py` composes it, dividers included."""
    return TableSessions(
        world.db if db is None else db,
        campaigns=world.campaigns,
        sessions=world.sessions,
        audit=world.audit,
        jobs=world.jobs,
        reconcile=lambda unit, campaign_id: enqueue_reconciliation(unit, world.jobs, campaign_id),
        clock=clock,
        dividers=enqueuer(world.jobs),
    )


def _service(world: DividerWorld, *, db: Any = None) -> SessionDividers:
    return SessionDividers(world.db if db is None else db, sessions=world.sessions, store=world.store)


def _overdue(world: DividerWorld, campaign: str) -> Any:
    """A session still `live` twelve hours after it started and an hour past
    its expiry: what the store holds when nothing finalised it."""
    started = datetime.now(UTC) - timedelta(hours=12)
    with world.db.transaction() as unit:
        return world.sessions.start(
            unit, campaign, owner_id=world.owner, expires_at=started + timedelta(hours=11),
            command_id=_command(), now=started,
        )


# ── Reading back ─────────────────────────────────────────────────────────────


def _jobs(world: DividerWorld) -> list[tuple[int, str, dict[str, Any], str | None]]:
    """Every job still in the outbox, oldest first: id, kind, payload, dedupe key."""
    if world.kind == "fake":
        rows = sorted(world.jobs._rows.values(), key=lambda row: row.job.id)
        return [(r.job.id, r.job.kind, dict(r.job.payload), r.dedupe_key) for r in rows]
    with world.db.transaction() as unit:
        found = unit.conn.execute("SELECT id, kind, payload, dedupe_key FROM app.jobs ORDER BY id").fetchall()
    return [(int(i), kind, dict(payload), key) for i, kind, payload, key in found]


def _divider_jobs(world: DividerWorld) -> list[dict[str, Any]]:
    queued = [(payload, key) for _, kind, payload, key in _jobs(world) if kind == DIVIDER_KIND]
    assert all(key is None for _, key in queued), "a divider job carries no dedupe key (RQ-12)"
    return [payload for payload, _ in queued]


def _kind_of(world: DividerWorld, job_ids: tuple[int, ...]) -> set[str]:
    kinds = {i: kind for i, kind, _, _ in _jobs(world)}
    return {kinds[i] for i in job_ids}


def _run_divider_jobs(world: DividerWorld) -> None:
    runner = JobRunner(
        world.jobs,
        {DIVIDER_KIND: _service(world).handler()},
        clock=lambda: datetime.now(UTC) + timedelta(days=2),
    )
    result = runner.run_due(limit=200)
    assert result.failed == 0 and not result.remaining, result


def _dividers(world: DividerWorld, conversation_id: str) -> list[tuple[str, str, datetime]]:
    """This thread's stored dividers, oldest first: session, boundary, time."""
    with world.db.transaction() as unit:
        rows = world.timeline.entry_window(unit, conversation_id, before=None, limit=1000)
    return [
        (row.payload["session_id"], row.payload["boundary"], row.created_at)
        for row in reversed(rows)
        if row.payload.get("entry_kind") == "session_divider"
    ]


def _entry_count(world: DividerWorld) -> int:
    if world.kind == "fake":
        return len(shared_rows(world.db, "timeline_entries").visible(_Reader()))
    with connect(world.dsn or "") as conn:
        return int(conn.execute("SELECT count(*) FROM chat.timeline_entries").fetchone()[0])


class _Reader:
    """A reader that has staged nothing: what any other transaction sees."""


def _a_divider(session_id: str, boundary: SessionBoundary, at: datetime, entry_id: str | None = None) -> Any:
    return divider_entry(entry_id=entry_id or new_entry_id(), session_id=session_id, boundary=boundary, at=at)


# ── The lifecycle enqueues one job per real transition ───────────────────────


def test_start_enqueues_one_start_divider_job_carrying_ids_only(world: DividerWorld) -> None:
    """MA-1 to MA-3: one job, of this kind, whose payload is exactly the
    session's id and the boundary's plain string, and nothing else. Start's
    `reconcile_jobs` still holds only reconciliations: none."""
    campaign = _campaign(world)
    started = _lifecycle(world).start(world.owner, campaign, command_id=_command())
    assert started.reconcile_jobs == ()
    [payload] = _divider_jobs(world)
    assert payload == {"session_id": started.session.id, "boundary": "start"}
    assert type(payload["boundary"]) is str


def test_a_replayed_or_already_live_start_enqueues_no_divider(world: DividerWorld) -> None:
    """MA-4: the replay and the live-session answers return before the insert,
    and so before any divider job."""
    campaign = _campaign(world)
    service, command = _lifecycle(world), _command()
    first = service.start(world.owner, campaign, command_id=command).session
    assert service.start(world.owner, campaign, command_id=command).session.id == first.id
    assert service.start(world.owner, campaign, command_id=_command()).session.id == first.id
    assert _divider_jobs(world) == [{"session_id": first.id, "boundary": "start"}]


def test_an_end_enqueues_one_end_divider_and_a_repeated_end_none(world: DividerWorld) -> None:
    """MA-5 and MA-6. End's `reconcile_jobs` hold only its reconciliation (the
    route runs those after its answer): the divider job never rides there."""
    campaign = _campaign(world)
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    ended = service.end(world.owner, campaign, session.id)
    assert _kind_of(world, ended.reconcile_jobs) == {RECONCILE_KIND}
    again = service.end(world.owner, campaign, session.id)
    assert again.reconcile_jobs == ()
    assert _divider_jobs(world) == [
        {"session_id": session.id, "boundary": "start"},
        {"session_id": session.id, "boundary": "end"},
    ]


def test_expiry_by_its_job_and_by_the_next_start_enqueues_one_end_each(world: DividerWorld) -> None:
    """MA-7 and MA-8, and the critic's item 3: the expiry job and Start's first
    transaction each leave one `end` per session they finalised, and a Start
    refused by the fan-out bound after finalising leaves that `end` and no
    `start`."""
    here, stale_home = _campaign(world), _campaign(world, name="Stale")
    t0 = datetime.now(UTC)
    by_job = _lifecycle(world).start(world.owner, here, command_id=_command(), now=t0).session
    due = _lifecycle(world, clock=lambda: by_job.expires_at + timedelta(seconds=1))
    due.expire_handler().run(
        Job(1, "table_session.expire", {"session_id": by_job.id, "campaign_id": here}, 1, t0), JobContext()
    )
    assert _divider_jobs(world)[-1] == {"session_id": by_job.id, "boundary": "end"}

    stale = _overdue(world, stale_home)
    before = _divider_jobs(world)
    started = _lifecycle(world).start(world.owner, here, command_id=_command())
    assert _kind_of(world, started.reconcile_jobs) == {RECONCILE_KIND}
    assert _divider_jobs(world)[len(before):] == [
        {"session_id": stale.id, "boundary": "end"},
        {"session_id": started.session.id, "boundary": "start"},
    ]

    service, busy = _lifecycle(world), _campaign(world, name="Busy")
    service.end(world.owner, here, started.session.id)
    for _ in range(FANOUT_BOUND // 2):
        cycle = service.start(world.owner, busy, command_id=_command()).session
        service.end(world.owner, busy, cycle.id)
    refused_stale = _overdue(world, stale_home)
    before = _divider_jobs(world)
    with pytest.raises(FanOutBound):
        service.start(world.owner, busy, command_id=_command())
    assert _divider_jobs(world)[len(before):] == [{"session_id": refused_stale.id, "boundary": "end"}]


def test_rotate_moves_neither_boundary(world: DividerWorld) -> None:
    """MA-9, REVEAL-17: a Rotate of a live, unexpired session, new or replayed,
    enqueues no divider job and so writes no divider."""
    campaign = _campaign(world)
    thread = _thread(world, campaign, datetime.now(UTC) - timedelta(hours=1))
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    command = _command()
    service.rotate(world.owner, campaign, session.id, command_id=command)
    service.rotate(world.owner, campaign, session.id, command_id=command)
    service.rotate(world.owner, campaign, session.id, command_id=_command())
    assert _divider_jobs(world) == [{"session_id": session.id, "boundary": "start"}]
    _run_divider_jobs(world)
    assert [(s, b) for s, b, _ in _dividers(world, thread)] == [(session.id, "start")]


@pytest.mark.parametrize("closer", ["end", "expiry job", "rotate of an overdue session", "end of an overdue session"])
def test_a_divider_takes_its_boundarys_own_time(world: DividerWorld, closer: str) -> None:
    """MA-15, and the critic's item 2 (MA-37): `created_at` is the boundary's
    own time, however late the job runs, and an ending is keyed on its outcome —
    a Rotate that expired an overdue session is an end."""
    campaign = _campaign(world)
    thread = _thread(world, campaign, datetime.now(UTC) - timedelta(days=1))
    service = _lifecycle(world)
    if closer.endswith("overdue session"):
        session = _overdue(world, campaign)
    else:
        session = service.start(world.owner, campaign, command_id=_command()).session
    ended_at_expiry = closer != "end"
    if closer == "end":
        closed = service.end(world.owner, campaign, session.id).session
    elif closer == "expiry job":
        late = _lifecycle(world, clock=lambda: session.expires_at + timedelta(hours=3))
        late.expire_handler().run(
            Job(1, "table_session.expire", {"session_id": session.id, "campaign_id": campaign}, 1, session.started_at),
            JobContext(),
        )
        with world.db.transaction() as unit:
            closed = world.sessions.get(unit, session.id)
    elif closer.startswith("rotate"):
        closed = service.rotate(world.owner, campaign, session.id, command_id=_command()).session
    else:
        closed = service.end(world.owner, campaign, session.id).session
    assert closed.state == ("expired" if ended_at_expiry else "ended")
    ends = [p for p in _divider_jobs(world) if p["boundary"] == "end"]
    assert ends == [{"session_id": session.id, "boundary": "end"}]
    _run_divider_jobs(world)
    stored = {b: at for s, b, at in _dividers(world, thread) if s == session.id}
    assert stored["end"] == (session.expires_at if ended_at_expiry else closed.ended_at)
    if "start" in stored:
        assert stored["start"] == session.started_at


def test_end_with_dividers_wired_writes_no_entry_and_leaves_one_job(world: DividerWorld) -> None:
    """MA-10's two-world half (the critic's item 9): End itself writes no
    timeline entry — it only enqueues, last — and exactly one `end` job waits."""
    campaign = _campaign(world)
    _thread(world, campaign, datetime.now(UTC) - timedelta(hours=1))
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    _run_divider_jobs(world)
    entries = _entry_count(world)
    service.end(world.owner, campaign, session.id)
    assert _entry_count(world) == entries
    assert _divider_jobs(world) == [{"session_id": session.id, "boundary": "end"}]


# ── Where a divider lands ────────────────────────────────────────────────────


def _the_campaign_with_every_kind_of_thread(world: DividerWorld, at: datetime) -> dict[str, str]:
    """The brief's set: two live threads of the campaign, an archived one, one
    created after `at`, another campaign's, an uncampaigned one, the other GM's
    in their own campaign, and the other GM's row linked to THIS campaign."""
    c1, c2 = _campaign(world), _campaign(world, name="Second")
    theirs = _campaign(world, owner=world.other_owner, name="Theirs")
    threads = {
        "c1": c1,
        "t1": _thread(world, c1, at - timedelta(hours=3)),
        "t2": _thread(world, c1, at - timedelta(hours=2)),
        "t3": _thread(world, c1, at - timedelta(hours=1)),
        "t4": _thread(world, c1, at + timedelta(minutes=5)),
        "u1": _thread(world, c2, at - timedelta(hours=1)),
        "v1": _thread(world, None, at - timedelta(hours=1)),
        "w1": _thread(world, theirs, at - timedelta(hours=1), owner=world.other_owner),
        "x1": "cnv-of-another-gm-in-c1",
    }
    _archive(world, threads["t3"])
    _raw_thread(world, threads["x1"], c1, at - timedelta(hours=1), owner=world.other_owner)
    return threads


def test_divider_targets_returns_exactly_the_live_threads_newest_first(world: DividerWorld) -> None:
    """MA-11 to MA-14, MA-22 and MA-23 (the critic's item 6), on the returned
    list itself: the owner's, this campaign's, not archived, created by `at`,
    newest first, and never more than `limit`."""
    at = datetime.now(UTC) - timedelta(minutes=30)
    threads = _the_campaign_with_every_kind_of_thread(world, at)
    extra = _thread(world, threads["c1"], at - timedelta(hours=4))
    with world.db.transaction() as unit:
        every = world.store.divider_targets(unit, campaign_id=threads["c1"], owner_id=world.owner, at=at, limit=10)
        two = world.store.divider_targets(unit, campaign_id=threads["c1"], owner_id=world.owner, at=at, limit=2)
    assert every == [threads["t2"], threads["t1"], extra]
    assert two == [threads["t2"], threads["t1"]]


def test_a_start_divider_lands_in_each_live_thread_of_that_campaign_and_nowhere_else(
    world: DividerWorld,
) -> None:
    """I-2 end to end: after the job, exactly t1 and t2 hold one start divider."""
    at = datetime.now(UTC) - timedelta(minutes=30)
    threads = _the_campaign_with_every_kind_of_thread(world, at)
    session = _lifecycle(world).start(world.owner, threads["c1"], command_id=_command(), now=at).session
    _run_divider_jobs(world)
    holding = {
        name for name, conversation in threads.items() if name != "c1" and _dividers(world, conversation)
    }
    assert holding == {"t1", "t2"}
    for name in ("t1", "t2"):
        assert _dividers(world, threads[name]) == [(session.id, "start", session.started_at)]


def test_the_fan_out_takes_the_newest_threads_up_to_its_bound(
    world: DividerWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MA-22 and MA-23 through `write`: with the bound at two and three eligible
    threads, the two newest get one each, the oldest none, one warning."""
    monkeypatch.setattr(session_dividers, "DIVIDER_FANOUT_MAX", 2)
    campaign = _campaign(world)
    now = datetime.now(UTC)
    oldest, middle, newest = (_thread(world, campaign, now - timedelta(hours=h)) for h in (3, 2, 1))
    session = _lifecycle(world).start(world.owner, campaign, command_id=_command()).session
    with caplog.at_level(logging.WARNING, logger="service.session_dividers"):
        assert _service(world).write(session.id, START) == 2
    assert [bool(_dividers(world, t)) for t in (oldest, middle, newest)] == [False, True, True]
    assert [r.getMessage() for r in caplog.records].count(
        "session dividers: a start boundary reached its bound of 2 threads; the newest were written"
    ) == 1


# ── Idempotency ──────────────────────────────────────────────────────────────


def test_running_one_boundary_twice_stores_one_divider_per_thread(world: DividerWorld) -> None:
    """MA-16 and MA-17a: `write` twice, and the handler twice, store one divider
    per thread; every repeat is zero rows, never an error."""
    campaign = _campaign(world)
    now = datetime.now(UTC)
    threads = [_thread(world, campaign, now - timedelta(hours=h)) for h in (1, 2)]
    session = _lifecycle(world).start(world.owner, campaign, command_id=_command()).session
    service = _service(world)
    assert service.write(session.id, START) == 2
    assert service.write(session.id, START) == 0
    job = Job(7, DIVIDER_KIND, {"session_id": session.id, "boundary": "start"}, 1, now)
    service.handler().run(job, JobContext())
    service.handler().run(job, JobContext())
    for thread in threads:
        assert _dividers(world, thread) == [(session.id, "start", session.started_at)]


def test_append_divider_refuses_in_its_statement_and_the_transaction_stays_usable(
    world: DividerWorld,
) -> None:
    """MA-24 to MA-27 and the critic's item 7 (G-9): another owner's thread, a
    thread of another campaign, an archived thread and a conversation that does
    not exist are each `False` and no row; the same transaction then stores a
    valid divider and commits."""
    at = datetime.now(UTC) - timedelta(minutes=30)
    threads = _the_campaign_with_every_kind_of_thread(world, at)
    session = "ses_" + "d" * 22
    with world.db.transaction() as unit:
        for name in ("x1", "u1", "t3", "w1"):
            assert not world.store.append_divider(
                unit, threads[name], _a_divider(session, START, at), owner_id=world.owner, campaign_id=threads["c1"]
            ), name
        assert not world.store.append_divider(
            unit, "cnv-that-was-never-made", _a_divider(session, START, at),
            owner_id=world.owner, campaign_id=threads["c1"],
        )
        assert world.store.append_divider(
            unit, threads["t1"], _a_divider(session, START, at), owner_id=world.owner, campaign_id=threads["c1"]
        )
    for name in ("x1", "u1", "t3", "w1"):
        assert _dividers(world, threads[name]) == []
    assert _dividers(world, threads["t1"]) == [(session, "start", at)]


def test_append_divider_refuses_any_entry_that_is_not_a_divider(world: DividerWorld) -> None:
    """MA-41, the critic's item 4: any other kind is refused before a statement,
    so no row can have a divider's column with another kind's payload."""
    campaign = _campaign(world)
    at = datetime.now(UTC)
    thread = _thread(world, campaign, at - timedelta(hours=1))
    opaque = entry_or_opaque({"schema_version": 99}, entry_id=new_entry_id(), created_at=at)
    with world.db.transaction() as unit:
        with pytest.raises(EntryInvalid) as refused:
            world.store.append_divider(unit, thread, opaque, owner_id=world.owner, campaign_id=campaign)
        assert refused.value.fields == ["entry_kind"]
    assert _entry_count(world) == 0


def test_an_entry_id_already_stored_raises_rather_than_being_absorbed(world: DividerWorld) -> None:
    """MA-17b, the critic's item 5: the conflict target is the divider index
    alone, so a primary-key collision raises in PostgreSQL, and the twin raises
    too; neither answers `False`."""
    campaign = _campaign(world)
    at = datetime.now(UTC)
    thread = _thread(world, campaign, at - timedelta(hours=1))
    taken = new_entry_id()
    with world.db.transaction() as unit:
        assert world.store.append_divider(
            unit, thread, _a_divider("ses_" + "e" * 22, START, at, taken), owner_id=world.owner, campaign_id=campaign
        )
    expected = psycopg.errors.UniqueViolation if world.kind == "postgres" else DividerIdTaken
    with pytest.raises(expected), world.db.transaction() as unit:
        world.store.append_divider(
            unit, thread, _a_divider("ses_" + "f" * 22, START, at, taken), owner_id=world.owner, campaign_id=campaign
        )


# ── The handler: what it trusts, and what it completes ───────────────────────


def test_an_end_for_a_live_or_unknown_session_writes_nothing_and_completes(world: DividerWorld) -> None:
    """MA-19 and MA-20: a boundary that has not happened, and a session that
    does not exist, complete without raising and store nothing."""
    campaign = _campaign(world)
    _thread(world, campaign, datetime.now(UTC) - timedelta(hours=1))
    live = _lifecycle(world).start(world.owner, campaign, command_id=_command()).session
    handler = _service(world).handler()
    for session_id in (live.id, "ses_" + "z" * 22):
        job = Job(3, DIVIDER_KIND, {"session_id": session_id, "boundary": "end"}, 1, live.started_at)
        handler.run(job, JobContext())
    assert _entry_count(world) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "ses_" + "a" * 22},
        {"session_id": "ses_" + "a" * 22, "boundary": "start", "campaign_id": "cmp_" + "a" * 22},
        {"session_id": 12, "boundary": "start"},
        {"session_id": "not a session id", "boundary": "start"},
        {"session_id": "cmp_" + "a" * 22, "boundary": "start"},
        {"session_id": "ses_" + "a" * 22, "boundary": "middle"},
    ],
    ids=["missing key", "extra key", "not a string", "outside the shape", "another prefix", "unknown boundary"],
)
def test_a_malformed_payload_completes_without_a_write(world: DividerWorld, payload: dict[str, Any]) -> None:
    """MA-21: the handler trusts nothing but the session row."""
    campaign = _campaign(world)
    _thread(world, campaign, datetime.now(UTC) - timedelta(hours=1))
    session = _lifecycle(world).start(world.owner, campaign, command_id=_command()).session
    shaped = {k: (session.id if v == "ses_" + "a" * 22 else v) for k, v in payload.items()}
    _service(world).handler().run(Job(4, DIVIDER_KIND, shaped, 1, session.started_at), JobContext())
    assert _entry_count(world) == 0


# ── Read back: pagination and reload ─────────────────────────────────────────


def _walk(world: DividerWorld, conversation_id: str, limit: int) -> list[str]:
    seen: list[str] = []
    cursor: timeline.TimelineCursor | None = None
    for _ in range(500):
        with world.db.transaction() as unit:
            page = timeline.read_page(world.timeline, unit, conversation_id, limit=limit, cursor=cursor)
        assert len(page.items) <= limit
        seen.extend(item.entry_id for item in page.items)
        if page.next_cursor is None:
            return seen
        cursor = timeline.decode_cursor(page.next_cursor)
    raise AssertionError("the cursor walk did not terminate")


def _chat(at: datetime, n: int) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_VERSION, "entry_kind": "chat", "entry_id": new_entry_id(),
        "created_at": at, "mode": "sage", "prompt": f"question {n}",
        "answer": {"text": f"answer {n}", "answerable": True, "sources": [], "created_at": at},
    }


def test_dividers_read_back_in_their_place_at_every_page_size(world: DividerWorld) -> None:
    """MR-1 (a guard on the unchanged read model): legacy rows, stored chat
    entries and dividers — one divider sharing its instant with a chat entry —
    walked at every page size equal the unpaged read, each divider once."""
    campaign = _campaign(world)
    t0 = datetime.now(UTC) - timedelta(hours=3)
    thread = _thread(world, campaign, t0 - timedelta(hours=1))
    for n, offset in enumerate((-30, 0, 10, 45, 70)):
        entry = _chat(t0 + timedelta(minutes=offset), n)
        with world.db.transaction() as unit:
            world.timeline.append(unit, thread, entry, entry["created_at"], owner_id=world.owner)
    for n in range(3):
        world.messages.append(thread, "sage", "user", f"legacy question {n}")
        world.messages.append(thread, "sage", "assistant", f"legacy answer {n}")
    service = _lifecycle(world)
    for start, end in ((0, 30), (40, 50)):
        session = service.start(world.owner, campaign, command_id=_command(), now=t0 + timedelta(minutes=start))
        service.end(world.owner, campaign, session.session.id, now=t0 + timedelta(minutes=end))
    _run_divider_jobs(world)

    with world.db.transaction() as unit:
        unpaged = timeline.read_page(world.timeline, unit, thread, limit=100, cursor=None)
    assert unpaged.next_cursor is None
    expected = [item.entry_id for item in unpaged.items]
    divider_ids = [item.entry_id for item in unpaged.items if item.entry_kind == "session_divider"]
    assert len(divider_ids) == 4 and len(expected) == 5 + 3 + 4
    assert [
        item.boundary.value for item in reversed(unpaged.items) if isinstance(item, SessionDividerEntry)
    ] == ["start", "end", "start", "end"]
    for size in (*range(1, 11), 100):
        walked = _walk(world, thread, size)
        assert walked == expected, f"page size {size} moved an entry in the {world.kind} world"
        assert all(walked.count(d) == 1 for d in divider_ids)


def test_a_divider_survives_reload_as_the_same_entry(world: DividerWorld) -> None:
    """MA-28: stored, not derived at read time — two reads, one id and one time."""
    campaign = _campaign(world)
    thread = _thread(world, campaign, datetime.now(UTC) - timedelta(hours=1))
    _lifecycle(world).start(world.owner, campaign, command_id=_command())
    _run_divider_jobs(world)
    reads = []
    for _ in range(2):
        with world.db.transaction() as unit:
            page = timeline.read_page(world.timeline, unit, thread, limit=10, cursor=None)
        reads.append([(i.entry_id, i.created_at, i.entry_kind) for i in page.items])
    assert reads[0] == reads[1] and [kind for _, _, kind in reads[0]] == ["session_divider"]


# ── Private text ─────────────────────────────────────────────────────────────


def test_the_divider_job_logs_no_private_text(
    world: DividerWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MA-29: over the start, end, bound-reached, skip and malformed paths, no
    log line holds the campaign's name, a thread's title or any conversation id."""
    monkeypatch.setattr(session_dividers, "DIVIDER_FANOUT_MAX", 1)
    campaign = _campaign(world, name=CANARY_NAME)
    now = datetime.now(UTC)
    threads = [_thread(world, campaign, now - timedelta(hours=h), title=CANARY_TITLE) for h in (2, 3)]
    unreadable = "cnv.with.dots"
    _raw_thread(world, unreadable, campaign, now - timedelta(minutes=30), owner=world.owner)
    with caplog.at_level(logging.DEBUG):
        service = _lifecycle(world)
        session = service.start(world.owner, campaign, command_id=_command()).session
        service.end(world.owner, campaign, session.id)
        _run_divider_jobs(world)
        _service(world).handler().run(Job(9, DIVIDER_KIND, {"boundary": CANARY_TITLE}, 1, now), JobContext())
    text = caplog.text
    assert "reached its bound" in text and "skipped" in text and "no-op" in text, "every path was logged"
    for secret in (CANARY_NAME, CANARY_TITLE, unreadable, *threads):
        assert secret not in text


# ── PostgreSQL only: who waits for whom ──────────────────────────────────────


def _holding_row(target: str, conversation_id: str) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Another connection holding one conversation row `FOR UPDATE`."""
    took, release = threading.Event(), threading.Event()

    def hold() -> None:
        with connect(target, autocommit=False) as conn:
            conn.execute("SELECT 1 FROM chat.conversations WHERE conversation_id = %s FOR UPDATE", (conversation_id,))
            took.set()
            release.wait(STAMINA)
            conn.rollback()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(STAMINA), "the holder never took the row"
    return took, release, thread


def _holding_the_campaign(db: Database, campaign: str) -> tuple[threading.Event, threading.Thread]:
    took, release = threading.Event(), threading.Event()

    def hold() -> None:
        with db.transaction() as unit:
            unit.lock_campaign(campaign, shared=False)
            took.set()
            release.wait(STAMINA)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(STAMINA), "the holder never took the campaign lock"
    return release, thread


def _waiters(target: str) -> int:
    with connect(target) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
        )


@needs_db
def test_end_with_dividers_wired_waits_for_nothing_it_did_not_wait_for_before(dsn: str) -> None:
    """MA-10 (X-3, RQ-5, L-5): End completes while one connection holds the
    campaign lock exclusively and another holds one of the session's divider
    targets `FOR UPDATE`, both uncommitted — End only enqueues."""
    world = _postgres_world(dsn, CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30))
    campaign = _campaign(world)
    thread = _thread(world, campaign, datetime.now(UTC) - timedelta(hours=1))
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    _run_divider_jobs(world)
    release_campaign, campaign_holder = _holding_the_campaign(world.db, campaign)
    _, release_row, row_holder = _holding_row(dsn, thread)
    try:
        outcome: list[object] = []
        ender = threading.Thread(target=lambda: outcome.append(service.end(world.owner, campaign, session.id)))
        began = time.monotonic()
        ender.start()
        ender.join(STAMINA / 3)
        assert not ender.is_alive(), f"End waited on a holder ({_waiters(dsn)} waiting on a lock)"
        assert time.monotonic() - began < STAMINA / 3
        assert outcome and outcome[0].session.state == "ended"
    finally:
        release_campaign.set()
        release_row.set()
        campaign_holder.join(STAMINA)
        row_holder.join(STAMINA)
    _run_divider_jobs(world)
    assert [b for _, b, _ in _dividers(world, thread)] == ["start", "end"]


@needs_db
def test_a_locked_target_fails_the_job_within_its_bound_and_a_rerun_completes(dsn: str) -> None:
    """MA-38, the critic's item 8, and MA-10's positive control: the job's
    insert waits on a target row held `FOR UPDATE`, and the bound the store sets
    ends that wait; nothing is stored. Released, a rerun stores every divider."""
    world = _postgres_world(dsn)
    campaign = _campaign(world)
    now = datetime.now(UTC)
    held, free = (_thread(world, campaign, now - timedelta(hours=h)) for h in (1, 2))
    session = _lifecycle(world).start(world.owner, campaign, command_id=_command()).session
    _, release, holder = _holding_row(dsn, held)
    outcome: list[object] = []

    def attempt() -> None:
        try:
            outcome.append(_service(world).write(session.id, START))
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcome.append(exc)

    try:
        worker = threading.Thread(target=attempt, daemon=True)
        began = time.monotonic()
        worker.start()
        worker.join(BRISK.lock_timeout_s + 2)
        assert not worker.is_alive(), "the job waited past its bound"
        assert time.monotonic() - began < BRISK.lock_timeout_s + 2
        assert outcome and isinstance(outcome[0], psycopg.errors.LockNotAvailable), outcome
        assert _dividers(world, free) == [] and _dividers(world, held) == []
    finally:
        release.set()
        holder.join(STAMINA)
    assert _service(world).write(session.id, START) == 2


@needs_db
def test_the_index_itself_refuses_a_second_divider_for_one_thread_session_and_boundary(dsn: str) -> None:
    """MA-18: 0017's index, by raw SQL that bypasses every store check. Another
    session, another boundary, another thread and a second chat entry are not
    refused."""
    world = _postgres_world(dsn)
    campaign = _campaign(world)
    now = datetime.now(UTC)
    one, two = (_thread(world, campaign, now - timedelta(hours=h)) for h in (1, 2))

    def insert(conversation_id: str, kind: str, payload: dict[str, Any]) -> None:
        with connect(dsn) as conn:
            conn.execute(
                "INSERT INTO chat.timeline_entries (entry_id, conversation_id, entry_kind, schema_version, "
                "created_at, payload) VALUES (%s, %s, %s, 1, %s, %s::jsonb)",
                (new_entry_id(), conversation_id, kind, now, json.dumps(payload)),
            )

    first = {"session_id": "ses_" + "a" * 22, "boundary": "start"}
    insert(one, "session_divider", first)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(one, "session_divider", first)
    insert(one, "session_divider", {**first, "boundary": "end"})
    insert(one, "session_divider", {**first, "session_id": "ses_" + "b" * 22})
    insert(two, "session_divider", first)
    insert(one, "chat", first)
    insert(one, "chat", first)


@needs_db
def test_a_start_that_lives_twelve_hours_still_gets_its_end_at_expires_at(dsn: str) -> None:
    """The application's own composition: `_build_stores` registers the divider
    kind, and an expiry run by its driver leaves an `end` whose divider, written
    by that driver too, sits at `expires_at`."""
    from service import app as appmod

    world = _postgres_world(dsn)
    campaign = _campaign(world)
    thread = _thread(world, campaign, datetime.now(UTC) - SESSION_LIFETIME - timedelta(hours=1))
    saved = dict(appmod._state)
    try:
        appmod._state.clear()
        appmod._state["migrations"] = "current"
        appmod._build_stores(world.db)
        started_at = datetime.now(UTC) - SESSION_LIFETIME - timedelta(minutes=1)
        session = appmod._state["table_sessions"].start(
            world.owner, campaign, command_id=_command(), now=started_at
        ).session
        driver = appmod._state["jobs"]
        for _ in range(6):
            driver.run_hook()
        assert [(b, at) for _, b, at in _dividers(world, thread)] == [
            ("start", session.started_at),
            ("end", session.expires_at),
        ]
    finally:
        appmod._state.clear()
        appmod._state.update(saved)

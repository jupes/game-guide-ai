"""The group routes' compositions (agent-forge-harness-btb, PR-2) in both worlds,
and the waits only PostgreSQL can answer.

The shared test runs one scripted transcript through the `world` fixture,
whose `postgres` parameter carries `needs_db`, and compares it with one literal:
the twin and PostgreSQL must each produce exactly it (the AC's "twin ==
PostgreSQL"). The two-connection tests below it prove who waits on the
campaign lock through `pg_stat_activity`, never through timing alone. Every
test names the mutant it kills (the brief's section 9.3, with the adopted
critic's items 2, 3 and 14).

Requires DATABASE_URL for the PostgreSQL half (CI sets it for this file,
`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`):

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_groups_api_db.py -q
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database
from fastapi import HTTPException

from service import groups_api
from service import migrations as mig
from service.audit_log import InMemoryAuditLog, PostgresAuditLog
from service.campaign_store import InMemoryCampaignStore, PostgresCampaignStore
from service.campaigns_api import NOT_APPLIED_MESSAGE
from service.db import CampaignLockSettings, Database, InMemoryDatabase, PoolSettings
from service.document_store import InMemoryDocumentStore, PostgresDocumentStore
from service.eligibility import no_table_namespace
from service.eligibility_store import InMemoryEligibilityStore, PostgresEligibilityStore
from service.participant_store import InMemoryParticipantStore, PostgresParticipantStore
from service.table_session_store import InMemoryTableSessionStore, PostgresTableSessionStore, no_slots
from service.workbench_contracts import Group, GroupPage

NOW = datetime(2030, 1, 1, tzinfo=UTC)
#: The waiting tests wait for a THREAD, so the lock wait is four seconds,
#: still under the five-second gate.
PATIENT = CampaignLockSettings(lock_timeout_s=4, transaction_timeout_s=30)
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
PATIENCE = 15
CANARY = "Zx9Canary"


def _database(dsn: str, settings: CampaignLockSettings = QUICK) -> Database:
    return Database(dsn, PoolSettings(sync_max=6, async_max=0, acquire_timeout_s=5), settings)


@dataclass
class World:
    kind: str
    db: Any
    stores: groups_api.GroupStores
    participants: Any
    owner: int
    stranger: int
    dsn: str | None = None


def _pg_world(dsn: str, settings: CampaignLockSettings = QUICK) -> World:
    with connect(dsn) as conn:
        owner, stranger = (
            int(conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
            ).fetchone()[0])
            for email in ("gm.a@example.com", "gm.b@example.com")
        )
    stores = groups_api.GroupStores(
        campaigns=PostgresCampaignStore(),
        eligibility=PostgresEligibilityStore(),
        documents=PostgresDocumentStore(),
        sessions=PostgresTableSessionStore(slot_clear=no_slots),
        audit=PostgresAuditLog(),
        table_namespace=no_table_namespace,
    )
    return World("postgres", _database(dsn, settings), stores, PostgresParticipantStore(), owner, stranger, dsn)


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("grp") as target:
        mig.migrate(target)
        yield target


@pytest.fixture(params=["twin", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> World:
    if request.param == "postgres":
        return _pg_world(request.getfixturevalue("dsn"))
    db = InMemoryDatabase()
    documents = InMemoryDocumentStore(db)
    stores = groups_api.GroupStores(
        campaigns=InMemoryCampaignStore(db),
        eligibility=InMemoryEligibilityStore(db, documents=documents),
        documents=documents,
        sessions=InMemoryTableSessionStore(db, slot_clear=no_slots),
        audit=InMemoryAuditLog(),
        table_namespace=no_table_namespace,
    )
    return World("twin", db, stores, InMemoryParticipantStore(db), owner=1, stranger=2)


# ── Seeding and reading ──────────────────────────────────────────────────────


def _campaign(world: World, owner: int) -> str:
    with world.db.transaction() as unit:
        return world.stores.campaigns.create(unit, owner_id=owner, name="Nocturne", now=NOW).id


def _seat(world: World, campaign: str, alias: str) -> str:
    with world.db.transaction() as unit:
        return world.participants.add(unit, campaign, alias=alias, now=NOW).id


def _live_session(world: World, campaign: str) -> None:
    with world.db.transaction() as unit:
        world.stores.sessions.start(
            unit, campaign, owner_id=world.owner, expires_at=datetime.now(UTC) + timedelta(hours=11),
            command_id=secrets.token_urlsafe(16),
        )


def _epoch(world: World, campaign: str) -> int:
    with world.db.transaction() as unit:
        live = world.stores.sessions.live_session_for_campaign(unit, campaign)
    assert live is not None
    return int(live.reveal_epoch)


def _revisions(world: World, campaign: str) -> tuple[int, int]:
    with world.db.transaction() as unit:
        authz = world.stores.campaigns.authz_revision(unit, campaign)
        projection = world.stores.campaigns.projection_revision(unit, campaign)
    assert authz is not None and projection is not None
    return authz, projection


def _state(world: World, campaign: str, group: str) -> tuple[Any, ...]:
    with world.db.transaction() as unit:
        members = world.stores.eligibility.members(unit, campaign, group)
        rows = [(event.action, event.object_ref) for event in world.stores.audit.for_campaign(unit, campaign)]
    return _revisions(world, campaign), members, rows


def _key() -> str:
    return secrets.token_urlsafe(16)


# ── T-P1 and T-P9: one transcript, both worlds ───────────────────────────────


#: What the script below must produce in BOTH worlds: each step's answer or
#: refusal, the ledger, and the revisions and reveal epoch it moved.
EXPECTED_TRANSCRIPT: list[Any] = [
    ("create", ("g1", CANARY, [])),
    ("create a taken name", (409, "group_name_taken")),
    ("replay with another name", ("g1", CANARY, [])),
    ("create", ("g2", "Wardens", [])),
    ("rename", ("g2", "Archers", [])),
    ("rename to the same name", ("g2", "Archers", [])),
    ("rename to a taken name", (409, "group_name_taken")),
    ("add", True),
    ("add again", False),
    ("add another", True),
    ("add another campaign's seat", (404, "not_found")),
    ("add a removed seat", (404, "not_found")),
    ("remove a member", True),
    ("remove it again", False),
    ("remove another campaign's seat", False),
    ("list", ([("g2", "Archers", []), ("g1", CANARY, ["rook"])], None)),
    ("remove a group", True),
    ("remove it again", False),
    ("add to a removed group", (404, "not_found")),
    ("rename a removed group", (404, "not_found")),
    ("replay a removed group", (404, "not_found")),
    ("a stranger adds", (404, "not_found")),
    ("a stranger creates", (404, "not_found")),
    ("a stranger lists", (404, "not_found")),
    ("list", ([("g1", CANARY, ["rook"])], None)),
    ("ledger", [
        ("group.created", "g1", {"group_id": "g1"}, None),
        ("group.created", "g2", {"group_id": "g2"}, None),
        ("group.renamed", "g2", {"group_id": "g2"}, None),
        ("group.member_added", "g1", {"group_id": "g1", "participant_id": "rook"}, 1),
        ("group.member_added", "g1", {"group_id": "g1", "participant_id": "wren"}, 2),
        ("group.member_removed", "g1", {"group_id": "g1", "participant_id": "wren"}, 3),
        ("group.removed", "g2", {"group_id": "g2"}, 4),
    ]),
    ("revisions moved", (4, 4)),
    ("reveal epoch moved", 4),
]


def _script(world: World) -> list[Any]:
    """Every composition, the refusals included, with ids replaced by labels in
    the order they were made and revisions relative to the start."""
    owner, stranger = world.owner, world.stranger
    campaign, theirs = _campaign(world, owner), _campaign(world, stranger)
    rook, wren, gone = (_seat(world, campaign, alias) for alias in ("Rook", "Wren", "Gone"))
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, gone, now=NOW)
    mira = _seat(world, theirs, "Mira")
    _live_session(world, campaign)
    labels = {campaign: "cmp", rook: "rook", wren: "wren", gone: "gone", mira: "mira"}
    start, epoch = _revisions(world, campaign), _epoch(world, campaign)
    mine = {"campaign_id": campaign, "owner_id": owner}

    def label(value: str) -> str:
        return labels.setdefault(value, f"g{sum(1 for known in labels if known.startswith('grp_')) + 1}")

    def shown(answer: Any) -> Any:
        if isinstance(answer, Group):
            return label(answer.group_id), answer.name, [label(seat) for seat in answer.member_ids]
        if isinstance(answer, GroupPage):
            return [shown(item) for item in answer.items], answer.next_cursor
        return answer

    transcript: list[Any] = []

    def step(name: str, work: Callable[[], Any]) -> Any:
        try:
            answer = work()
        except HTTPException as refused:
            transcript.append((name, (refused.status_code, refused.detail["code"])))
            return None
        transcript.append((name, shown(answer)))
        return answer

    first, second = _key(), _key()
    g1 = step("create", lambda: groups_api.group_create(
        world.db, world.stores, **mine, name=CANARY, command_id=first, now=NOW)).group_id
    step("create a taken name", lambda: groups_api.group_create(
        world.db, world.stores, **mine, name=CANARY.lower(), command_id=_key(), now=NOW))
    step("replay with another name", lambda: groups_api.group_create(
        world.db, world.stores, **mine, name="Another", command_id=first, now=NOW))
    g2 = step("create", lambda: groups_api.group_create(
        world.db, world.stores, **mine, name="Wardens", command_id=second, now=NOW)).group_id
    for name, new in (("rename", "Archers"), ("rename to the same name", "Archers"),
                      ("rename to a taken name", CANARY.upper())):
        step(name, lambda new=new: groups_api.group_rename(
            world.db, world.stores, **mine, group_id=g2, name=new, now=NOW))
    for name, seat in (("add", rook), ("add again", rook), ("add another", wren),
                       ("add another campaign's seat", mira), ("add a removed seat", gone)):
        step(name, lambda seat=seat: groups_api.member_add(
            world.db, world.stores, **mine, group_id=g1, participant_id=seat, now=NOW))
    for name, seat in (("remove a member", wren), ("remove it again", wren), ("remove another campaign's seat", mira)):
        step(name, lambda seat=seat: groups_api.member_remove(
            world.db, world.stores, **mine, group_id=g1, participant_id=seat, now=NOW))
    step("list", lambda: groups_api.group_list(world.db, world.stores, **mine))
    for name in ("remove a group", "remove it again"):
        step(name, lambda: groups_api.group_remove(world.db, world.stores, **mine, group_id=g2, now=NOW))
    step("add to a removed group", lambda: groups_api.member_add(
        world.db, world.stores, **mine, group_id=g2, participant_id=rook, now=NOW))
    step("rename a removed group", lambda: groups_api.group_rename(
        world.db, world.stores, **mine, group_id=g2, name="Again", now=NOW))
    step("replay a removed group", lambda: groups_api.group_create(
        world.db, world.stores, **mine, name="Wardens", command_id=second, now=NOW))
    theirs_view = {"campaign_id": campaign, "owner_id": stranger}
    step("a stranger adds", lambda: groups_api.member_add(
        world.db, world.stores, **theirs_view, group_id=g1, participant_id=wren, now=NOW))
    step("a stranger creates", lambda: groups_api.group_create(
        world.db, world.stores, **theirs_view, name="Wardens", command_id=_key(), now=NOW))
    step("a stranger lists", lambda: groups_api.group_list(world.db, world.stores, **theirs_view))
    step("list", lambda: groups_api.group_list(world.db, world.stores, **mine))
    with world.db.transaction() as unit:
        events = world.stores.audit.for_campaign(unit, campaign)
    assert all((e.actor_kind, e.actor_ref, e.object_kind) == ("gm", str(owner), "group") for e in events)
    transcript.append(("ledger", [
        (e.action, label(e.object_ref or ""), {k: label(str(v)) for k, v in e.detail.items()},
         None if e.authz_revision is None else e.authz_revision - start[0])
        for e in events
    ]))
    end = _revisions(world, campaign)
    transcript.append(("revisions moved", (end[0] - start[0], end[1] - start[1])))
    transcript.append(("reveal epoch moved", _epoch(world, campaign) - epoch))
    return transcript


def test_the_compositions_answer_the_same_transcript_in_both_worlds_and_log_no_name(world, caplog):
    """T-P1 (the AC's twin == PostgreSQL) and T-P9 (the critic's item 14): the
    same literal in both worlds, and — through a forced collision, whose
    PostgreSQL DETAIL quotes the name's fold — no log record names the group.

    The epoch moves 4: the member removal and the group removal each narrow
    twice (the critic's item 2); the repeat removals and the non-member take
    the shortcut and narrow nothing (item 3)."""
    caplog.set_level(logging.DEBUG)
    assert _script(world) == EXPECTED_TRANSCRIPT
    for record in caplog.records:
        assert CANARY.lower() not in (record.getMessage() + repr(record.args)).lower()


# ── PostgreSQL only: who waits for whom ──────────────────────────────────────


def _someone_waits_on_a_lock(dsn: str, patience: float = PATIENCE) -> bool:
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


def _in_thread(work: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = work()
        except BaseException as error:  # noqa: BLE001 - the test inspects it
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


@dataclass(frozen=True)
class _Table:
    campaign: str
    group: str
    seat: str


def _table(world: World, *, member: bool) -> _Table:
    campaign = _campaign(world, world.owner)
    seat = _seat(world, campaign, "Rook")
    group = groups_api.group_create(
        world.db, world.stores, campaign_id=campaign, owner_id=world.owner, name="Scouts", command_id=_key(), now=NOW
    ).group_id
    if member:
        assert groups_api.member_add(
            world.db, world.stores, campaign_id=campaign, group_id=group, participant_id=seat,
            owner_id=world.owner, now=NOW,
        )
    _live_session(world, campaign)
    return _Table(campaign, group, seat)


def _hold(dsn: str, campaign: str, mode: str) -> Any:
    holder = connect(dsn, autocommit=False)
    holder.execute(f"SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR {mode}", (campaign,))
    return holder


@needs_db
def test_a_member_add_waits_on_the_campaign_lock_and_advances_once_it_has_it(dsn):
    """T-P2: the AC's "under the campaign lock", at the route's call site. A
    share-mode holder (a reveal) makes the add wait — the server says so — and
    on release it lands; with the lock never released it is a 503 that changed
    nothing and wrote no row."""
    world = _pg_world(dsn, PATIENT)
    table = _table(world, member=False)
    before = _state(world, table.campaign, table.group)
    holder = _hold(dsn, table.campaign, "SHARE")
    thread, outcome = _in_thread(lambda: groups_api.member_add(
        world.db, world.stores, campaign_id=table.campaign, group_id=table.group, participant_id=table.seat,
        owner_id=world.owner, now=NOW,
    ))
    assert _someone_waits_on_a_lock(dsn)
    holder.rollback()
    holder.close()
    thread.join(PATIENCE)
    assert outcome == {"value": True}
    assert _revisions(world, table.campaign)[0] == before[0][0] + 1
    stuck = replace(world, db=_database(dsn, QUICK))
    other = _seat(stuck, table.campaign, "Wren")
    after = _state(world, table.campaign, table.group)
    holder = _hold(dsn, table.campaign, "SHARE")
    try:
        with pytest.raises(HTTPException) as refused:
            groups_api.member_add(
                stuck.db, stuck.stores, campaign_id=table.campaign, group_id=table.group, participant_id=other,
                owner_id=world.owner, now=NOW,
            )
    finally:
        holder.rollback()
        holder.close()
    assert (refused.value.status_code, refused.value.detail["message"]) == (503, groups_api.GROUPS_UNAVAILABLE_MESSAGE)
    assert _state(world, table.campaign, table.group) == after


@needs_db
def test_a_create_waits_on_the_same_lock_and_then_advances_nothing(dsn):
    """T-P3: create is locked too, and still advances nothing."""
    world = _pg_world(dsn, PATIENT)
    table = _table(world, member=False)
    before = _revisions(world, table.campaign)
    holder = _hold(dsn, table.campaign, "SHARE")
    thread, outcome = _in_thread(lambda: groups_api.group_create(
        world.db, world.stores, campaign_id=table.campaign, owner_id=world.owner, name="Wardens",
        command_id=_key(), now=NOW,
    ))
    assert _someone_waits_on_a_lock(dsn)
    holder.rollback()
    holder.close()
    thread.join(PATIENCE)
    assert isinstance(outcome.get("value"), Group) and outcome["value"].name == "Wardens"
    assert _revisions(world, table.campaign) == before


@needs_db
@pytest.mark.parametrize("operation", ["member_remove", "group_remove"])
def test_a_removal_whose_step_two_cannot_lock_narrows_once_and_answers_not_applied_yet(dsn, operation):
    """T-P4 and T-P5: kills M-P4 (step 2 answered as success, or step 1
    skipped). Step 1 commits — the epoch moves exactly once — and step 2 is
    the retryable 503; the membership, the revision and the ledger are as
    they were."""
    world = _pg_world(dsn, QUICK)
    table = _table(world, member=True)
    before, epoch = _state(world, table.campaign, table.group), _epoch(world, table.campaign)
    holder = _hold(dsn, table.campaign, "UPDATE")
    try:
        with pytest.raises(HTTPException) as refused:
            if operation == "member_remove":
                groups_api.member_remove(
                    world.db, world.stores, campaign_id=table.campaign, group_id=table.group,
                    participant_id=table.seat, owner_id=world.owner, now=NOW,
                )
            else:
                groups_api.group_remove(
                    world.db, world.stores, campaign_id=table.campaign, group_id=table.group,
                    owner_id=world.owner, now=NOW,
                )
    finally:
        holder.rollback()
        holder.close()
    detail = refused.value.detail
    assert (refused.value.status_code, detail["code"], detail["retryable"], detail["message"]) == (
        503, "backend_unavailable", True, NOT_APPLIED_MESSAGE
    )
    assert refused.value.__context__ is None
    assert _epoch(world, table.campaign) == epoch + 1
    assert _state(world, table.campaign, table.group) == before


@needs_db
def test_a_rename_completes_while_the_campaign_lock_is_held(dsn):
    """T-P6: kills M-S19 in PostgreSQL. With the lock held `FOR UPDATE` and a
    one-second lock timeout, a rename that asked for the lock would be a 503."""
    world = _pg_world(dsn, QUICK)
    table = _table(world, member=True)
    holder = _hold(dsn, table.campaign, "UPDATE")
    try:
        renamed = groups_api.group_rename(
            world.db, world.stores, campaign_id=table.campaign, group_id=table.group, owner_id=world.owner,
            name="Wardens", now=NOW,
        )
    finally:
        holder.rollback()
        holder.close()
    assert (renamed.name, renamed.member_ids) == ("Wardens", [table.seat])


@needs_db
def test_a_stranger_never_waits_on_another_gms_lock(dsn):
    """T-P7: kills M-S1 (the ownership pre-read dropped, so B would wait for
    A's lock and answer 503). B's add and create on A's campaign are the one
    404, promptly, and nobody waits."""
    world = _pg_world(dsn, PATIENT)
    table = _table(world, member=False)
    holder = _hold(dsn, table.campaign, "UPDATE")
    try:
        for attempt in (
            lambda: groups_api.member_add(
                world.db, world.stores, campaign_id=table.campaign, group_id=table.group,
                participant_id=table.seat, owner_id=world.stranger, now=NOW,
            ),
            lambda: groups_api.group_create(
                world.db, world.stores, campaign_id=table.campaign, owner_id=world.stranger, name="Mine",
                command_id=_key(), now=NOW,
            ),
        ):
            thread, outcome = _in_thread(attempt)
            thread.join(PATIENT.lock_timeout_s / 2)
            assert not thread.is_alive(), "a stranger waited on the campaign lock"
            error = outcome.get("error")
            assert isinstance(error, HTTPException) and (error.status_code, error.detail["code"]) == (404, "not_found")
            assert not _someone_waits_on_a_lock(dsn, patience=0.2)
    finally:
        holder.rollback()
        holder.close()


class _RefusingAudit(PostgresAuditLog):
    def append(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the ledger is unavailable")


@needs_db
def test_the_audit_row_commits_with_the_change_or_nothing_does(dsn):
    """T-P8: kills M-P8 (the audit row written in a second transaction)."""
    world = _pg_world(dsn, QUICK)
    table = _table(world, member=False)
    before = _state(world, table.campaign, table.group)
    broken = replace(world.stores, audit=_RefusingAudit())
    with pytest.raises(RuntimeError, match="the ledger is unavailable"):
        groups_api.member_add(
            world.db, broken, campaign_id=table.campaign, group_id=table.group, participant_id=table.seat,
            owner_id=world.owner, now=NOW,
        )
    assert _state(world, table.campaign, table.group) == before

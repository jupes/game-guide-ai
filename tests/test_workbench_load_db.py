"""The joint Workbench load — X-5 and the pilot day across tool invocations and
AI document edits — against a real PostgreSQL, and its twin
(agent-forge-harness-1kg.5.5, PR-1: group L of the brief's test plan).

A count one route can skip is no cap (I-3), so these tests seed a tool row
through the tool store and an edit row through the edit store, and then ask the
load — and the tool route's own admission — what they see. The lock tests are
two-connection PostgreSQL tests (I-4).

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_workbench_load_db.py -q
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from _pg import needs_db, throwaway_database
from test_document_edit_store_db import (
    INV,
    OTHER_INV,
    T0,
    TTL,
    Table,
    World,
    a_table,
    admit,
    fake_world,
    pg_world,
    settle,
    statements,
)

import config
from service import migrations as mig
from service import ratelimit, tool_invocations, tool_invocations_api, workbench_load
from service.db import AdvisoryLock, CampaignLockSettings, Database, PoolSettings, advisory_key
from service.session import SessionData
from service.timeline_store import new_entry_id
from service.tool_invocation_store import InMemoryToolInvocationStore, PostgresToolInvocationStore
from service.tool_invocations import (
    CAP_REACHED_MESSAGE,
    EDIT_CAP_REACHED_MESSAGE,
    InvocationStores,
    Refused,
    ToolSettings,
)
from service.workbench_contracts import CONTRACT_VERSION, ToolId, ToolInvocationRequest
from service.workbench_load import InMemoryWorkbenchLoad, PostgresWorkbenchLoad

TOOL = "tool_suite_000000001"
THIRD = "tool_suite_000000003"


@dataclass
class Loaded:
    """A World with the tool store and the load beside the edit store."""

    world: World
    tools: Any
    load: Any


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("load") as target:
        mig.migrate(target)
        yield target


def _loaded(world: World) -> Loaded:
    if world.kind == "fake":
        return Loaded(world, InMemoryToolInvocationStore(world.db), InMemoryWorkbenchLoad(world.db))
    return Loaded(world, PostgresToolInvocationStore(), PostgresWorkbenchLoad())


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def loaded(request: pytest.FixtureRequest) -> Iterator[Loaded]:
    if request.param == "fake":
        yield _loaded(fake_world())
        return
    target = request.getfixturevalue("dsn")
    yield _loaded(pg_world(target, Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))))


@pytest.fixture(autouse=True)
def _fresh_window() -> Iterator[None]:
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


def a_tool(loaded: Loaded, table: Table, invocation_id: str = TOOL, *, now: Any = T0) -> None:
    """A working tool invocation of `table`'s GM, as the tool store writes one."""
    world = loaded.world
    with world.db.transaction() as unit:
        entry_id = new_entry_id()
        world.timeline.append(unit, table.conversation, {
            "schema_version": CONTRACT_VERSION, "entry_kind": "tool", "entry_id": entry_id, "created_at": now,
            "brief": "a brief", "source_entry_id": None,
            "invocation": {"schema_version": CONTRACT_VERSION, "invocation_id": invocation_id, "tool_id": "npc",
                           "status": "working", "attempt": 1, "cancel_requested": False, "created_at": now,
                           "updated_at": now, "result": None, "error": None},
        }, now, owner_id=table.owner)
        loaded.tools.create(
            unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=invocation_id,
            conversation_id=table.conversation, entry_id=entry_id, tool_id="npc", brief="a brief",
            source_entry_id=None, operation_id=uuid.uuid4().hex, attempt_deadline=now + TTL, now=now,
            schema_version=CONTRACT_VERSION,
        )


def in_flight(loaded: Loaded, owner: int, *, now: Any = T0) -> list[tuple[str, str]]:
    with loaded.world.db.transaction() as unit:
        found: list[tuple[str, str]] = loaded.load.in_flight(unit, owner, now=now)
        assert loaded.load.in_flight_ids(unit, owner, now=now) == [i for i, _ in found]
    return found


# ── What the load reads (L-1, L-2) ───────────────────────────────────────────


def test_in_flight_interleaves_tools_and_edits_oldest_first_with_ties_in_byte_order(loaded: Loaded) -> None:
    """L-1: one statement over both kinds, ordered by `created_at` then id byte by
    byte; a row past its deadline and another GM's rows count for nothing."""
    world = loaded.world
    table = a_table(world)
    theirs = a_table(world, world.other)
    a_tool(loaded, table, "same_a_000000000001", now=T0)
    admit(world, table, "same_B_000000000001", now=T0)
    a_tool(loaded, table, "late_Z_000000000001", now=T0 + timedelta(seconds=1))
    admit(world, table, "late_0_000000000001", now=T0 + timedelta(seconds=2))
    a_tool(loaded, table, "tool_expired_000001", now=T0 - TTL)
    admit(world, table, "edit_expired_000001", now=T0 - TTL)
    a_tool(loaded, theirs, "tool_theirs_0000001", now=T0)
    admit(world, theirs, "edit_theirs_0000001", now=T0)
    assert in_flight(loaded, table.owner) == [
        ("same_B_000000000001", "edit"), ("same_a_000000000001", "tool"),
        ("late_Z_000000000001", "tool"), ("late_0_000000000001", "edit"),
    ], "one instant's ids in byte order across kinds: B (0x42) before a (0x61)"
    assert in_flight(loaded, theirs.owner) == [("edit_theirs_0000001", "edit"), ("tool_theirs_0000001", "tool")]
    assert in_flight(loaded, table.owner, now=T0 + TTL + timedelta(seconds=3)) == []


def test_a_settled_edit_no_longer_holds_a_slot(loaded: Loaded) -> None:
    table = a_table(loaded.world)
    admit(loaded.world, table)
    assert in_flight(loaded, table.owner) == [(INV, "edit")]
    settle(loaded.world, table, status="cancelled")
    assert in_flight(loaded, table.owner) == []


def test_the_pilot_day_counts_every_gms_tool_and_edit_attempts_from_its_bound(loaded: Loaded) -> None:
    """L-2: pilot-wide (C-5), both kinds."""
    world = loaded.world
    table = a_table(world)
    theirs = a_table(world, world.other)
    a_tool(loaded, table)
    admit(world, table)
    admit(world, theirs, OTHER_INV)
    a_tool(loaded, theirs, THIRD, now=T0 - timedelta(seconds=1))
    with world.db.transaction() as unit:
        assert loaded.load.attempts_since(unit, T0) == 3
        assert loaded.load.attempts_since(unit, T0 - timedelta(seconds=1)) == 4
        assert loaded.load.attempts_since(unit, T0 + timedelta(seconds=1)) == 0


# ── The tool route counts edits (L-3, L-4, L-9) ──────────────────────────────


def _submit(loaded: Loaded, table: Table, invocation_id: str) -> Any:
    world = loaded.world
    stores = InvocationStores(loaded.tools, world.campaigns, world.conversations, world.timeline, loaded.load)
    request = ToolInvocationRequest.model_validate({
        "schema_version": 1, "invocation_id": invocation_id, "tool_id": "npc", "brief": "A smith",
        "campaign_id": table.campaign, "conversation_id": table.conversation,
    })
    return tool_invocations.submit(
        world.db, stores, {ToolId.NPC: _Npc()}, ToolSettings(frozenset({ToolId.NPC})),
        SessionData(user_id=table.owner, role="dm"), request, now=T0, chat_turns_today=0,
    )


class _Npc:
    tool_id = ToolId.NPC

    def precheck(self, unit: Any, target: Any) -> None:
        return None


def test_a_tool_is_refused_at_the_cap_when_an_edit_holds_a_slot_and_the_sentence_says_so(loaded: Loaded) -> None:
    """L-3, C-15: with one tool and one edit working, a third tool is `409
    cap_reached` naming both, and "two tools" would be false."""
    table = a_table(loaded.world)
    a_tool(loaded, table)
    admit(loaded.world, table)
    with pytest.raises(Refused) as refused:
        _submit(loaded, table, THIRD)
    info = refused.value.info
    assert (refused.value.status, info.code.value, info.retryable) == (409, "cap_reached", True)
    assert sorted(info.in_flight or []) == sorted([TOOL, INV])
    assert info.message == EDIT_CAP_REACHED_MESSAGE != CAP_REACHED_MESSAGE
    with loaded.world.db.transaction() as unit:
        assert loaded.tools.get_for_update(unit, table.owner, table.campaign, THIRD) is None, "nothing created"


def test_with_two_tools_running_the_cap_keeps_the_tools_sentence_on_either_route(loaded: Loaded) -> None:
    """L-8, L-9: the pinned sentence stands while tools alone hold the cap, and
    `admit_attempt` — which the edit route calls too — says the same."""
    table = a_table(loaded.world)
    a_tool(loaded, table)
    a_tool(loaded, table, THIRD)
    with pytest.raises(Refused) as by_tool:
        _submit(loaded, table, "tool_suite_000000009")
    assert by_tool.value.info.message == CAP_REACHED_MESSAGE == "Two tools are already running."
    with loaded.world.db.transaction() as unit, pytest.raises(Refused) as by_edit:
        tool_invocations.admit_attempt(unit, loaded.load, table.owner, now=T0, chat_turns_today=0)
    assert (by_edit.value.info.message, sorted(by_edit.value.info.in_flight or [])) == (
        CAP_REACHED_MESSAGE, sorted([TOOL, THIRD]))


def test_two_edits_hold_the_cap_against_a_tool_too(loaded: Loaded) -> None:
    table = a_table(loaded.world)
    admit(loaded.world, table)
    admit(loaded.world, a_table(loaded.world), OTHER_INV)
    with pytest.raises(Refused) as refused:
        _submit(loaded, table, THIRD)
    assert (refused.value.info.message, sorted(refused.value.info.in_flight or [])) == (
        EDIT_CAP_REACHED_MESSAGE, sorted([INV, OTHER_INV]))


def test_the_tool_routes_pilot_day_counts_edit_attempts(loaded: Loaded, monkeypatch: pytest.MonkeyPatch) -> None:
    """L-4: two edit attempts, one of them another GM's, spend a day of two."""
    monkeypatch.setattr(config, "CHAT_DAILY_CAP", 2)
    table = a_table(loaded.world)
    admit(loaded.world, table)
    settle(loaded.world, table, status="cancelled")
    theirs = a_table(loaded.world, loaded.world.other)
    admit(loaded.world, theirs)
    settle(loaded.world, theirs, status="cancelled")
    with pytest.raises(Refused) as refused:
        _submit(loaded, table, THIRD)
    assert (refused.value.status, refused.value.info.code.value) == (429, "throttled_daily")
    monkeypatch.setattr(config, "CHAT_DAILY_CAP", 3)
    assert isinstance(_submit(loaded, table, THIRD), tool_invocations.Admission), "a positive control"


def test_the_production_wiring_hands_the_tool_routes_the_postgres_load() -> None:
    """L-5."""
    assert isinstance(tool_invocations_api.get_invocation_stores().load, PostgresWorkbenchLoad)


# ── The lock (I-4) ───────────────────────────────────────────────────────────


def test_the_load_takes_the_tool_stores_in_flight_member_and_key_after_bounding(loaded: Loaded) -> None:
    table = a_table(loaded.world)
    with loaded.world.db.transaction() as unit:
        loaded.load.hold_in_flight_lock(unit, table.owner)
        assert len(unit.transaction_bounds) == 1, "bounded before the first lock"
        if loaded.world.kind == "fake":
            assert unit.locks == [(AdvisoryLock.WORKBENCH_IN_FLIGHT, str(table.owner))]
        else:
            held_keys = unit.conn.execute(
                "SELECT classid::int, objid::int FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            ).fetchall()
            key = advisory_key(str(table.owner)) & 0xFFFFFFFF
            assert [(c, o & 0xFFFFFFFF) for c, o in held_keys] == [(int(AdvisoryLock.WORKBENCH_IN_FLIGHT), key)]


QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)


@needs_db
@pytest.mark.parametrize("holder", ["load", "tool_store"])
def test_one_gms_tool_and_edit_admissions_serialise_on_one_lock(dsn: str, holder: str) -> None:
    """L-6: while one admission holds the GM's in-flight lock — taken through
    the load, as the edit T1 takes it, or through the tool store, as the tool T1
    does — the other kind's first lock for that GM times out on `lock_timeout`,
    and another GM proceeds."""
    database = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=10), QUICK)
    world = pg_world(dsn, database)
    took, release, failures = threading.Event(), threading.Event(), []

    def hold() -> None:
        try:
            with database.transaction() as unit:
                first = PostgresWorkbenchLoad() if holder == "load" else PostgresToolInvocationStore()
                first.hold_in_flight_lock(unit, world.owner)
                took.set()
                release.wait(15)
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failures.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert took.wait(15) and not failures
        second = PostgresToolInvocationStore() if holder == "load" else PostgresWorkbenchLoad()
        with pytest.raises(psycopg.errors.LockNotAvailable), database.transaction() as unit:
            second.hold_in_flight_lock(unit, world.owner)
        with database.transaction() as unit:
            second.hold_in_flight_lock(unit, world.other)
    finally:
        release.set()
        thread.join(15)
    assert not failures


# ── Statically (L-7) ─────────────────────────────────────────────────────────


def test_every_load_statement_names_the_owner_in_every_half_except_the_bound_and_the_day() -> None:
    found = statements(workbench_load)
    assert set(found) == {"_BOUND", "_IN_FLIGHT", "_ATTEMPTS_SINCE"}
    assert re.fullmatch(r"SELECT set_config\([^()]*\), set_config\([^()]*\)", found["_BOUND"])
    assert "owner" not in found["_ATTEMPTS_SINCE"]
    assert re.findall(r"count\(\*\) FROM (\S+)", found["_ATTEMPTS_SINCE"]) == [
        "campaign.tool_attempts", "campaign.document_edit_attempts"]
    halves = found["_IN_FLIGHT"].split("UNION ALL")
    assert len(halves) == 2
    for half, table in zip(halves, ("campaign.tool_invocations", "campaign.document_edits"), strict=True):
        assert f"FROM {table} WHERE owner_id = %s AND status = 'working' AND attempt_deadline > %s" in half
    for name, statement in found.items():
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|FOR NO KEY)\b", statement), f"{name} writes or locks"

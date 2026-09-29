"""The GM tool invocation aggregate against a real PostgreSQL, and its twin
(agent-forge-harness-1kg.4.1, slice A).

The **shared behavioural suite** runs every rule twice, once on the in-memory
twin and once on PostgreSQL, so a rule the two could disagree about is asserted
of both. Both worlds seed alike: campaigns through `CampaignStore`,
conversations through `ConversationStore` **and** `MessageStore.claim_conversation`
(the timeline twin keeps its own record of who owns a conversation), and tool
entries through `TimelineStore.append`.

What only PostgreSQL can prove, and what the `needs_db` tests below are for:
the database's own deletion edges, and the races — each forced into the
interleaving it names by a third connection or a held transaction, never by a
sleep (C-10(e)).

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_tool_invocation_db.py -q
"""

from __future__ import annotations

import ast
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service import tool_invocation_store
from service.campaign_store import InMemoryCampaignStore, PostgresCampaignStore
from service.conversation_store import InMemoryConversationStore, PostgresConversationStore
from service.db import (
    AdvisoryLock,
    CampaignLockOrder,
    CampaignLockSettings,
    Database,
    InMemoryDatabase,
    PoolSettings,
    advisory_key,
)
from service.history import InMemoryMessageStore, PostgresMessageStore
from service.timeline_store import InMemoryTimelineStore, PostgresTimelineStore, new_entry_id
from service.tool_invocation_store import (
    InMemoryToolInvocationStore,
    InvocationInvalid,
    InvocationNotStored,
    InvocationRow,
    PostgresToolInvocationStore,
)
from service.workbench_contracts import CONTRACT_VERSION

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=150)
INV = "inv_suite_0000000001"
OTHER_INV = "inv_suite_0000000002"
MISSING_CAMPAIGN = "cmp_" + "z" * 22
CANARY = "Zx9CanaryQ7"
EXPIRED = {"code": "attempt_expired", "message": "That took too long and was stopped. Try again.", "retryable": True}
TIMED_OUT = {"code": "provider_timeout", "message": "The model took too long to answer.", "retryable": True}
FINAL = {"code": "provider_failed", "message": "The assistant couldn't do that one.", "retryable": False}


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("tools") as target:
        mig.migrate(target)
        yield target


def _users(dsn: str, *emails: str) -> list[int]:
    with connect(dsn) as conn:
        return [
            int(conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
            ).fetchone()[0])
            for email in emails
        ]


@dataclass
class World:
    kind: str
    db: Any
    campaigns: Any
    conversations: Any
    messages: Any
    timeline: Any
    store: Any
    owner: int
    other: int
    dsn: str | None = None


def _pg_world(target: str, database: Database) -> World:
    owner, other = _users(target, "gm@example.com", "other@example.com")
    return World(
        "postgres", database, PostgresCampaignStore(), PostgresConversationStore(),
        PostgresMessageStore(db=database), PostgresTimelineStore(), PostgresToolInvocationStore(),
        owner, other, target,
    )


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db = InMemoryDatabase()
        messages = InMemoryMessageStore()
        yield World(
            "fake", db, InMemoryCampaignStore(db), InMemoryConversationStore(db), messages,
            InMemoryTimelineStore(db, messages=messages), InMemoryToolInvocationStore(db), 1, 2,
        )
        return
    target = request.getfixturevalue("dsn")
    yield _pg_world(target, Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5)))


@dataclass(frozen=True)
class Table:
    owner: int
    campaign: str
    conversation: str


def a_table(world: World, owner: int | None = None, *, campaigned: bool = True) -> Table:
    """A campaign of `owner`'s and a conversation of theirs in it (or in none)."""
    gm = world.owner if owner is None else owner
    with world.db.transaction() as unit:
        campaign = world.campaigns.create(unit, owner_id=gm, name="Nocturne")
        conversation = world.conversations.create(
            unit, owner_id=gm, campaign_id=campaign.id if campaigned else None, title=None, started_mode=None,
        )
    world.messages.claim_conversation(conversation.id, gm)
    return Table(gm, campaign.id, conversation.id)


def _tool_entry(invocation_id: str, entry_id: str, at: datetime, *, tool: str = "npc",
                brief: str = "a brief") -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_VERSION, "entry_kind": "tool", "entry_id": entry_id, "created_at": at,
        "brief": brief, "source_entry_id": None,
        "invocation": {
            "schema_version": CONTRACT_VERSION, "invocation_id": invocation_id, "tool_id": tool,
            "status": "working", "attempt": 1, "cancel_requested": False, "created_at": at,
            "updated_at": at, "result": None, "error": None,
        },
    }


def _chat_entry(entry_id: str, at: datetime) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_VERSION, "entry_kind": "chat", "entry_id": entry_id, "created_at": at,
        "mode": "gm", "prompt": "a question", "answer": None,
    }


def _entry_in(world: World, unit: Any, conversation: str, owner: int, invocation_id: str = INV, *,
              at: datetime = T0, kind: str = "tool") -> str:
    entry_id = new_entry_id()
    entry = _tool_entry(invocation_id, entry_id, at) if kind == "tool" else _chat_entry(entry_id, at)
    world.timeline.append(unit, conversation, entry, at, owner_id=owner)
    return entry_id


def _create(world: World, unit: Any, table: Table, entry_id: str, invocation_id: str = INV, *,
            owner: int | None = None, campaign: str | None = None, conversation: str | None = None,
            now: datetime = T0, source: str | None = None, brief: str = "a brief", tool: str = "npc",
            operation_id: str | None = None, deadline: datetime | None = None) -> InvocationRow:
    row: InvocationRow = world.store.create(
        unit, owner_id=table.owner if owner is None else owner,
        campaign_id=table.campaign if campaign is None else campaign, invocation_id=invocation_id,
        conversation_id=table.conversation if conversation is None else conversation, entry_id=entry_id,
        tool_id=tool, brief=brief, source_entry_id=source,
        operation_id=operation_id or uuid.uuid4().hex,
        attempt_deadline=now + TTL if deadline is None else deadline, now=now, schema_version=CONTRACT_VERSION,
    )
    return row


def admit(world: World, table: Table, invocation_id: str = INV, *, now: datetime = T0) -> InvocationRow:
    """What the admitting transaction writes: the carrying entry, then the row."""
    with world.db.transaction() as unit:
        entry_id = _entry_in(world, unit, table.conversation, table.owner, invocation_id, at=now)
        return _create(world, unit, table, entry_id, invocation_id, now=now)


def held(world: World, table: Table, invocation_id: str = INV) -> InvocationRow | None:
    with world.db.transaction() as unit:
        row: InvocationRow | None = world.store.get_for_update(unit, table.owner, table.campaign, invocation_id)
    return row


def attempts(world: World, table: Table, invocation_id: str = INV) -> list[Any]:
    with world.db.transaction() as unit:
        rows: list[Any] = world.store.attempts(unit, table.owner, table.campaign, invocation_id)
    return rows


def settle(world: World, table: Table, *, status: str, outcome: str | None = None, attempt: int = 1,
           result: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
           now: datetime = T0 + timedelta(seconds=5), invocation_id: str = INV) -> InvocationRow:
    with world.db.transaction() as unit:
        row: InvocationRow = world.store.settle(
            unit, table.owner, table.campaign, invocation_id, attempt=attempt, status=status,
            outcome=outcome or status, result=result, error=error, now=now,
        )
    return row


def retry(world: World, table: Table, *, attempt: int = 1, now: datetime = T0 + timedelta(seconds=10),
          invocation_id: str = INV) -> InvocationRow:
    with world.db.transaction() as unit:
        row: InvocationRow = world.store.start_retry(
            unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=invocation_id,
            attempt=attempt, operation_id=uuid.uuid4().hex, attempt_deadline=now + TTL, now=now,
        )
    return row


def _entry_ids(world: World, conversation: str) -> list[str]:
    with world.db.transaction() as unit:
        return [r.entry_id for r in world.timeline.entry_window(unit, conversation, before=None, limit=100)]


# ── A new invocation ─────────────────────────────────────────────────────────


def test_a_new_invocation_is_working_at_attempt_one_with_its_admission_record(world: World) -> None:
    table = a_table(world)
    operation = uuid.uuid4().hex
    with world.db.transaction() as unit:
        entry_id = _entry_in(world, unit, table.conversation, table.owner)
        created = _create(world, unit, table, entry_id, operation_id=operation)
    assert held(world, table) == created
    assert (created.status, created.attempt, created.cancel_requested) == ("working", 1, False)
    assert (created.result, created.error, created.schema_version) == (None, None, CONTRACT_VERSION)
    assert (created.attempt_deadline, created.created_at, created.updated_at) == (T0 + TTL, T0, T0)
    assert (created.entry_id, created.conversation_id, created.tool_id) == (entry_id, table.conversation, "npc")
    [record] = attempts(world, table)
    assert (record.attempt, record.operation_id, record.started_at, record.deadline_at) == (1, operation, T0, T0 + TTL)
    assert (record.ended_at, record.outcome) == (None, None)


def test_a_named_source_entry_of_the_same_conversation_is_accepted(world: World) -> None:
    table = a_table(world)
    with world.db.transaction() as unit:
        source = _entry_in(world, unit, table.conversation, table.owner, "inv_the_source_000001")
        entry_id = _entry_in(world, unit, table.conversation, table.owner)
        created = _create(world, unit, table, entry_id, source=source)
    assert created.source_entry_id == source


REFUSED_PARENTS = [
    "foreign_campaign", "foreign_conversation", "another_campaigns_conversation", "uncampaigned_conversation",
    "archived_campaign", "missing_campaign", "source_of_another_conversation", "missing_source",
    "entry_of_another_conversation", "chat_entry_as_carrier",
]


@pytest.mark.parametrize("case", REFUSED_PARENTS)
def test_the_creating_statement_refuses_every_parent_that_is_not_the_callers(world: World, case: str) -> None:
    """C-4 and G-9: every guard is in the statement, so each miss is zero rows —
    the same named refusal in both worlds, raised without a failed statement, and
    the PostgreSQL transaction still commits afterwards."""
    mine = a_table(world)
    theirs = a_table(world, world.other)
    second = a_table(world)
    loose = a_table(world, campaigned=False)
    if case == "archived_campaign":
        with world.db.transaction() as unit:
            assert world.campaigns.set_archived(unit, mine.campaign, owner_id=mine.owner, archived=True)
    with world.db.transaction() as unit:
        entry_here = _entry_in(world, unit, mine.conversation, mine.owner)
        kwargs: dict[str, Any] = {
            "foreign_campaign": {"owner": world.other},
            "foreign_conversation": {"conversation": theirs.conversation},
            "another_campaigns_conversation": {"conversation": second.conversation},
            "uncampaigned_conversation": {"conversation": loose.conversation},
            "archived_campaign": {},
            "missing_campaign": {"campaign": MISSING_CAMPAIGN},
            "source_of_another_conversation": {"source": _entry_in(world, unit, second.conversation, mine.owner)},
            "missing_source": {"source": new_entry_id()},
            "entry_of_another_conversation": {},
            "chat_entry_as_carrier": {},
        }[case]
        carrier = entry_here
        if case == "foreign_conversation":
            carrier = _entry_in(world, unit, theirs.conversation, theirs.owner)
        elif case == "another_campaigns_conversation":
            carrier = _entry_in(world, unit, second.conversation, mine.owner)
        elif case == "uncampaigned_conversation":
            carrier = _entry_in(world, unit, loose.conversation, mine.owner)
        elif case == "entry_of_another_conversation":
            carrier = _entry_in(world, unit, second.conversation, mine.owner)
        elif case == "chat_entry_as_carrier":
            carrier = _entry_in(world, unit, mine.conversation, mine.owner, kind="chat")
        with pytest.raises(InvocationNotStored):
            _create(world, unit, mine, carrier, **kwargs)
        assert world.store.get_for_update(unit, mine.owner, mine.campaign, INV) is None, "still usable"
    for owner, campaign in ((mine.owner, mine.campaign), (world.other, mine.campaign), (mine.owner, MISSING_CAMPAIGN)):
        with world.db.transaction() as unit:
            assert world.store.get_for_update(unit, owner, campaign, INV) is None
            assert world.store.attempts(unit, owner, campaign, INV) == []


def test_a_refused_create_takes_the_entry_appended_before_it_with_it(world: World) -> None:
    """C-4: the refusal is raised inside the admitting transaction, so the entry
    that transaction appended first rolls back with it."""
    table = a_table(world)
    with world.db.transaction() as unit:
        assert world.campaigns.set_archived(unit, table.campaign, owner_id=table.owner, archived=True)
    with pytest.raises(InvocationNotStored), world.db.transaction() as unit:
        entry_id = _entry_in(world, unit, table.conversation, table.owner)
        _create(world, unit, table, entry_id)
    assert _entry_ids(world, table.conversation) == []
    assert held(world, table) is None
    assert attempts(world, table) == []


def test_the_key_is_the_gm_the_campaign_and_the_id(world: World) -> None:
    mine = a_table(world)
    second = a_table(world)
    theirs = a_table(world, world.other)
    first = admit(world, mine)
    with pytest.raises(InvocationNotStored), world.db.transaction() as unit:
        _create(world, unit, mine, _entry_in(world, unit, mine.conversation, mine.owner))
    in_second = admit(world, second)
    in_theirs = admit(world, theirs)
    assert held(world, mine) == first
    assert held(world, second) == in_second
    assert held(world, theirs) == in_theirs
    with world.db.transaction() as unit:
        assert world.store.get_for_update(unit, world.other, mine.campaign, INV) is None
        assert world.store.get_for_update(unit, mine.owner, theirs.campaign, INV) is None


def test_a_unit_that_rolls_back_leaves_no_invocation_and_no_attempt(world: World) -> None:
    table = a_table(world)
    with pytest.raises(RuntimeError), world.db.transaction() as unit:
        _create(world, unit, table, _entry_in(world, unit, table.conversation, table.owner))
        raise RuntimeError("the rest of the transaction failed")
    assert (held(world, table), attempts(world, table), _entry_ids(world, table.conversation)) == (None, [], [])


@pytest.mark.parametrize("what", [
    "invocation_id", "tool_id", "brief", "source_entry_id", "attempt_deadline", "operation_id", "schema_version",
])
def test_a_value_the_table_would_refuse_is_refused_by_name_before_any_statement(world: World, what: str) -> None:
    table = a_table(world)
    bad: dict[str, Any] = {
        "invocation_id": {"invocation_id": "too_short"},
        "tool_id": {"tool": "npcs"},
        "brief": {"brief": "\U0001f409" * 2001},
        "source_entry_id": {"source": "not an id"},
        "attempt_deadline": {"deadline": T0},
        "operation_id": {"operation_id": "Z" * 32},
        "schema_version": {},
    }[what]
    with world.db.transaction() as unit:
        entry_id = _entry_in(world, unit, table.conversation, table.owner)
        with pytest.raises(InvocationInvalid) as refused:
            if what == "schema_version":
                world.store.create(
                    unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=INV,
                    conversation_id=table.conversation, entry_id=entry_id, tool_id="npc", brief="b",
                    source_entry_id=None, operation_id=uuid.uuid4().hex, attempt_deadline=T0 + TTL, now=T0,
                    schema_version=0,
                )
            else:
                _create(world, unit, table, entry_id, **bad)
        assert refused.value.what == what
        assert world.store.get_for_update(unit, table.owner, table.campaign, INV) is None, "still usable"


def test_a_brief_of_exactly_the_bound_in_astral_code_points_is_stored_whole(world: World) -> None:
    table = a_table(world)
    brief = "\U0001f409" * 2000
    with world.db.transaction() as unit:
        created = _create(world, unit, table, _entry_in(world, unit, table.conversation, table.owner), brief=brief)
    assert created.brief == brief
    assert held(world, table) == created


def test_no_repr_carries_the_brief_the_result_or_the_invocation_id(world: World) -> None:
    table = a_table(world)
    secret_id = "inv_" + CANARY + "_000001"
    with world.db.transaction() as unit:
        entry_id = _entry_in(world, unit, table.conversation, table.owner, secret_id)
        _create(world, unit, table, entry_id, secret_id, brief=f"a brief naming {CANARY}")
    row = settle(world, table, status="done", result={"prose": CANARY}, invocation_id=secret_id)
    [record] = attempts(world, table, secret_id)
    assert CANARY not in repr(row) and CANARY not in repr(record)


# ── The X-5 count and the pilot day ──────────────────────────────────────────


def test_in_flight_counts_this_gms_working_rows_before_their_deadline_oldest_first(world: World) -> None:
    mine = a_table(world)
    second = a_table(world)
    theirs = a_table(world, world.other)
    now = T0 + timedelta(seconds=20)
    admit(world, mine, "inv_long_expired_0001", now=T0 - timedelta(seconds=140))
    admit(world, mine, "inv_on_its_deadline1", now=T0 - timedelta(seconds=130))
    admit(world, second, "inv_second_campaign1", now=T0 + timedelta(seconds=10))
    admit(world, mine, "inv_the_oldest_live1", now=T0)
    admit(world, mine, "inv_already_done_001", now=T0 + timedelta(seconds=1))
    settle(world, mine, status="done", result={"k": "v"}, invocation_id="inv_already_done_001")
    admit(world, theirs, "inv_another_gms_0001", now=T0)
    with world.db.transaction() as unit:
        ids = world.store.in_flight_ids(unit, world.owner, now=now)
        assert ids == ["inv_the_oldest_live1", "inv_second_campaign1"]
        assert world.store.in_flight_ids(unit, world.other, now=now) == ["inv_another_gms_0001"]


def test_the_pilot_day_counts_every_gms_attempts_from_its_bound(world: World) -> None:
    """C-5: `CHAT_DAILY_CAP` is pilot-wide, so the count names no owner — another
    GM's attempts count toward this one's day, and a retry is an attempt."""
    mine = a_table(world)
    theirs = a_table(world, world.other)
    admit(world, mine, "inv_before_the_bound", now=T0 - timedelta(seconds=1))
    admit(world, mine, now=T0)
    settle(world, mine, status="failed", error=TIMED_OUT, now=T0 + timedelta(seconds=2))
    retry(world, mine, now=T0 + timedelta(seconds=3))
    admit(world, theirs, OTHER_INV, now=T0 + timedelta(seconds=4))
    with world.db.transaction() as unit:
        assert world.store.attempts_since(unit, T0) == 3
        assert world.store.attempts_since(unit, T0 + timedelta(seconds=5)) == 0


# ── The fence and the terminal transitions ───────────────────────────────────


def test_the_fence_holds_only_its_own_attempt_while_working_and_before_its_deadline(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        fence = world.store.fence
        assert fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0 + TTL - timedelta(microseconds=1))
        assert fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0 + TTL) is None, "the deadline"
        assert fence(unit, table.owner, table.campaign, INV, attempt=2, now=T0) is None, "the attempt"
        assert fence(unit, world.other, table.campaign, INV, attempt=1, now=T0) is None, "the owner"
    settle(world, table, status="failed", error=TIMED_OUT)
    with world.db.transaction() as unit:
        assert world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0) is None, "the status"


def test_a_late_completion_of_a_superseded_attempt_is_fenced_out(world: World) -> None:
    """AE-84: attempt 1 expires, attempt 2 is admitted; attempt 1's result can
    no longer be committed, whatever the clock says."""
    table = a_table(world)
    admit(world, table)
    settle(world, table, status="failed", outcome="expired", error=EXPIRED, now=T0 + TTL)
    retry(world, table, now=T0 + TTL + timedelta(seconds=1))
    with world.db.transaction() as unit:
        fence = world.store.fence
        assert fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0 + timedelta(seconds=1)) is None
        assert fence(unit, table.owner, table.campaign, INV, attempt=2, now=T0 + TTL + timedelta(seconds=2))


def test_settle_moves_a_working_row_once_and_ends_its_attempt(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    moment = T0 + timedelta(seconds=7)
    done = settle(world, table, status="done", result={"result_kind": "card", "n": 1}, now=moment)
    assert (done.status, done.result, done.error) == ("done", {"result_kind": "card", "n": 1}, None)
    assert done.updated_at == moment
    assert held(world, table) == done
    [record] = attempts(world, table)
    assert (record.ended_at, record.outcome) == (moment, "done")
    with pytest.raises(InvocationNotStored):
        settle(world, table, status="failed", error=TIMED_OUT)


def test_settle_refuses_a_row_on_another_attempt_or_of_another_gm(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    with pytest.raises(InvocationNotStored):
        settle(world, table, status="done", result={"k": 1}, attempt=2)
    with pytest.raises(InvocationNotStored):
        settle(world, Table(world.other, table.campaign, table.conversation), status="done", result={"k": 1})
    assert held(world, table).status == "working"  # type: ignore[union-attr]


@pytest.mark.parametrize(("status", "outcome", "result", "error", "what"), [
    ("done", "done", None, None, "result"),
    ("failed", "failed", None, None, "error"),
    ("done", "done", {"k": 1}, TIMED_OUT, "error"),
    ("cancelled", "cancelled", {"k": 1}, None, "result"),
    ("working", "done", None, None, "status"),
    ("done", "expired", {"k": 1}, None, "outcome"),
    ("failed", "done", None, TIMED_OUT, "outcome"),
])
def test_a_settlement_whose_payload_disagrees_with_its_status_is_refused_before_any_statement(
    world: World, status: str, outcome: str, result: Any, error: Any, what: str,
) -> None:
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        with pytest.raises(InvocationInvalid) as refused:
            world.store.settle(
                unit, table.owner, table.campaign, INV, attempt=1, status=status, outcome=outcome,
                result=result, error=error, now=T0,
            )
        assert refused.value.what == what
        assert world.store.get_for_update(unit, table.owner, table.campaign, INV).status == "working"


def test_an_expired_attempt_reads_failed_or_cancelled_and_its_record_says_expired(world: World) -> None:
    table = a_table(world)
    other = a_table(world)
    admit(world, table)
    admit(world, other, OTHER_INV)
    settle(world, table, status="failed", outcome="expired", error=EXPIRED, now=T0 + TTL)
    with world.db.transaction() as unit:
        assert world.store.set_cancel_requested(unit, other.owner, other.campaign, OTHER_INV, now=T0)
    settle(world, other, status="cancelled", outcome="expired", now=T0 + TTL, invocation_id=OTHER_INV)
    assert [a.outcome for a in attempts(world, table)] == ["expired"]
    assert [a.outcome for a in attempts(world, other, OTHER_INV)] == ["expired"]
    assert held(world, table).error == EXPIRED  # type: ignore[union-attr]
    cancelled = held(world, other, OTHER_INV)
    assert cancelled is not None and (cancelled.status, cancelled.cancel_requested) == ("cancelled", True)


# ── Retry ────────────────────────────────────────────────────────────────────


def test_a_retry_starts_the_next_attempt_of_a_failed_retryable_row_and_clears_it(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        world.store.set_cancel_requested(unit, table.owner, table.campaign, INV, now=T0)
    settle(world, table, status="failed", error=TIMED_OUT)
    moment = T0 + timedelta(seconds=30)
    again = retry(world, table, now=moment)
    assert (again.status, again.attempt, again.error, again.result) == ("working", 2, None, None)
    assert (again.cancel_requested, again.attempt_deadline, again.updated_at) == (False, moment + TTL, moment)
    assert (again.created_at, again.brief, again.entry_id) == (T0, "a brief", held(world, table).entry_id)  # type: ignore[union-attr]
    first, second = attempts(world, table)
    assert (first.outcome, second.attempt, second.outcome, second.started_at) == ("failed", 2, None, moment)
    assert first.operation_id != second.operation_id


@pytest.mark.parametrize("case", ["working", "done", "final", "wrong_attempt", "archived", "another_gm", "cancelled"])
def test_a_retry_of_anything_but_a_failed_retryable_row_in_a_live_campaign_is_refused(world: World, case: str) -> None:
    table = a_table(world)
    admit(world, table)
    if case == "done":
        settle(world, table, status="done", result={"k": 1})
    elif case == "cancelled":
        settle(world, table, status="cancelled")
    elif case != "working":
        settle(world, table, status="failed", error=FINAL if case == "final" else TIMED_OUT)
    if case == "archived":
        with world.db.transaction() as unit:
            world.campaigns.set_archived(unit, table.campaign, owner_id=table.owner, archived=True)
    before = held(world, table)
    with pytest.raises(InvocationNotStored):
        retry(
            world, Table(world.other, table.campaign, table.conversation) if case == "another_gm" else table,
            attempt=2 if case == "wrong_attempt" else 1,
        )
    assert held(world, table) == before
    assert len(attempts(world, table)) == 1


def test_the_hundredth_attempt_is_the_last(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        for attempt in range(1, 100):
            moment = T0 + timedelta(seconds=attempt)
            world.store.settle(unit, table.owner, table.campaign, INV, attempt=attempt, status="failed",
                               outcome="failed", result=None, error=TIMED_OUT, now=moment)
            world.store.start_retry(unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=INV,
                                    attempt=attempt, operation_id=uuid.uuid4().hex,
                                    attempt_deadline=moment + TTL, now=moment)
        world.store.settle(unit, table.owner, table.campaign, INV, attempt=100, status="failed",
                           outcome="failed", result=None, error=TIMED_OUT, now=T0 + timedelta(seconds=100))
    with pytest.raises(InvocationNotStored):
        retry(world, table, attempt=100, now=T0 + timedelta(seconds=101))
    assert held(world, table).attempt == 100  # type: ignore[union-attr]
    assert len(attempts(world, table)) == 100


def test_a_retry_in_a_unit_that_rolls_back_leaves_the_failed_attempt_as_it_was(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    before = settle(world, table, status="failed", error=TIMED_OUT)
    with pytest.raises(RuntimeError), world.db.transaction() as unit:
        world.store.start_retry(unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=INV,
                                attempt=1, operation_id=uuid.uuid4().hex, attempt_deadline=T0 + 2 * TTL,
                                now=T0 + TTL)
        raise RuntimeError("the rest of the transaction failed")
    assert held(world, table) == before
    assert len(attempts(world, table)) == 1


# ── Cancel ───────────────────────────────────────────────────────────────────


def test_the_cancel_flag_is_raised_on_working_and_done_only_and_only_once(world: World) -> None:
    table = a_table(world)
    for invocation_id in (INV, OTHER_INV, "inv_will_fail_000001", "inv_will_cancel_0001"):
        admit(world, table, invocation_id)
    settle(world, table, status="done", result={"k": 1}, invocation_id=OTHER_INV)
    settle(world, table, status="failed", error=TIMED_OUT, invocation_id="inv_will_fail_000001")
    settle(world, table, status="cancelled", invocation_id="inv_will_cancel_0001")
    moment = T0 + timedelta(seconds=9)
    with world.db.transaction() as unit:
        cancel = world.store.set_cancel_requested
        working = cancel(unit, table.owner, table.campaign, INV, now=moment)
        assert working is not None and (working.status, working.cancel_requested, working.updated_at) == (
            "working", True, moment)
        assert cancel(unit, table.owner, table.campaign, INV, now=moment) is None, "only once"
        done = cancel(unit, table.owner, table.campaign, OTHER_INV, now=moment)
        assert done is not None and (done.status, done.cancel_requested, done.result) == ("done", True, {"k": 1})
        assert cancel(unit, table.owner, table.campaign, "inv_will_fail_000001", now=moment) is None
        assert cancel(unit, table.owner, table.campaign, "inv_will_cancel_0001", now=moment) is None
        assert cancel(unit, world.other, table.campaign, INV, now=moment) is None
        assert cancel(unit, table.owner, table.campaign, "inv_never_existed_01", now=moment) is None


def test_a_completion_keeps_a_cancel_flag_raised_before_it(world: World) -> None:
    """RAIL-23: a paid-for result is kept, and says it finished before it could
    be cancelled."""
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        world.store.set_cancel_requested(unit, table.owner, table.campaign, INV, now=T0)
        assert world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0), "a flag is no fence"
    done = settle(world, table, status="done", result={"k": 1})
    assert (done.status, done.cancel_requested) == ("done", True)


# ── Locks and bounds (RQ-3, RQ-8, C-3) ───────────────────────────────────────


@pytest.mark.parametrize("hold", ["in_flight", "row", "fence"])
def test_every_hold_bounds_its_transaction_once_and_no_campaign_lock_can_follow(world: World, hold: str) -> None:
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        if hold == "in_flight":
            world.store.hold_in_flight_lock(unit, table.owner)
        elif hold == "row":
            world.store.get_for_update(unit, table.owner, table.campaign, INV)
        else:
            world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0)
        assert len(unit.transaction_bounds) == 1, "bounded before the first lock"
        with pytest.raises(CampaignLockOrder):
            unit.lock_campaign(table.campaign, shared=True)
        world.store.get_for_update(unit, table.owner, table.campaign, INV)
        assert len(unit.transaction_bounds) == 1, "and only once"
        if world.kind == "postgres":
            lock_wait, lifetime = unit.conn.execute(
                "SELECT current_setting('lock_timeout'), current_setting('transaction_timeout')"
            ).fetchone()
            assert lock_wait != "0" and lifetime != "0"


def test_the_in_flight_lock_is_its_own_member_keyed_by_the_gm(world: World) -> None:
    table = a_table(world)
    with world.db.transaction() as unit:
        world.store.hold_in_flight_lock(unit, table.owner)
        if world.kind == "fake":
            assert unit.locks == [(AdvisoryLock.WORKBENCH_IN_FLIGHT, str(table.owner))]
        else:
            held_keys = unit.conn.execute(
                "SELECT classid::int, objid::int FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid()"
            ).fetchall()
            key = advisory_key(str(table.owner)) & 0xFFFFFFFF
            assert [(c, o & 0xFFFFFFFF) for c, o in held_keys] == [(int(AdvisoryLock.WORKBENCH_IN_FLIGHT), key)]
    # "Add a member; never reuse a number": an alias would share another's lock.
    value = int(AdvisoryLock.WORKBENCH_IN_FLIGHT)
    assert [name for name, member in AdvisoryLock.__members__.items() if int(member) == value] == [
        "WORKBENCH_IN_FLIGHT"
    ]


# ── Ownership is in the query, statically ────────────────────────────────────

_NAMES_THE_OWNER = re.compile(r"\bowner_id\s*=\s*%s")


def _statements(module: Any) -> dict[str, str]:
    """Every statement the module hands to `.execute(...)`, by the constant it
    is written in, read from the module's syntax tree (the precedent of
    `tests/test_timeline_db.py`). A call site whose statement is not a named
    constant is a failure: it could not be exempted, or checked, by name."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                value = getattr(module, node.targets[0].id, None)
            if isinstance(value, str):
                constants[node.targets[0].id] = value
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            first = node.args[0]
            assert isinstance(first, ast.Name) and first.id in constants, f"an unnamed statement at line {node.lineno}"
            found[first.id] = constants[first.id]
    return found


def test_every_statement_names_the_owner_except_the_bound_and_the_pilot_day() -> None:
    """SEC-2 and C-5: exactly two statements name no owner, by name — the
    transaction bound, which reads no row, and the pilot-wide day count, which
    selects the count alone."""
    statements = _statements(tool_invocation_store)
    assert len(statements) >= 12, "no SQL found — this test would pass vacuously"
    exempt = {"_BOUND", "_ATTEMPTS_SINCE"}
    assert exempt <= set(statements)
    assert re.fullmatch(
        r"SELECT count\(\*\) FROM campaign\.tool_attempts WHERE started_at >= %s", statements["_ATTEMPTS_SINCE"]
    )
    assert re.fullmatch(r"SELECT set_config\([^()]*\), set_config\([^()]*\)", statements["_BOUND"])
    for name, statement in statements.items():
        if name in exempt:
            continue
        before_returning = re.split(r"\bRETURNING\b", statement)[0]
        assert _NAMES_THE_OWNER.search(before_returning), f"{name} does not name the owner"


def test_the_store_writes_only_its_own_tables_and_holds_rows_the_rq3_way() -> None:
    statements = _statements(tool_invocation_store)
    for name, statement in statements.items():
        write = re.match(r"\s*(INSERT INTO|UPDATE|DELETE FROM)\s+(\S+)", statement)
        if write:
            assert write.group(2) in {"campaign.tool_invocations", "campaign.tool_attempts"}, name
            assert write.group(1) != "DELETE FROM", f"{name}: the aggregate has no delete"
        assert not re.search(r"\bFOR\s+(UPDATE|SHARE|KEY SHARE)\b", statement), f"{name}: RQ-3 says FOR NO KEY UPDATE"
        assert "now()" not in statement.lower(), f"{name}: the clock is the caller's (I-16)"
        # chat.conversations (alias v) is 1kg.2.4's: read inside a guard, and
        # only its identity, owner and campaign columns.
        for column in re.findall(r"\bv\.(\w+)", statement):
            assert column in {"conversation_id", "user_id", "campaign_id"}, f"{name} reads v.{column}"


# ── PostgreSQL only: the deletion edges ──────────────────────────────────────


def _count(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> int:
    with connect(dsn) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def _rows_of(dsn: str) -> tuple[int, int, int]:
    return (
        _count(dsn, "SELECT count(*) FROM campaign.tool_invocations"),
        _count(dsn, "SELECT count(*) FROM campaign.tool_attempts"),
        _count(dsn, "SELECT count(*) FROM chat.timeline_entries"),
    )


@needs_db
def test_deleting_a_conversation_takes_its_invocations_attempts_and_entries(dsn: str) -> None:
    world = _pg_world(dsn, Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5)))
    table = a_table(world)
    kept = a_table(world)
    admit(world, table)
    admit(world, kept)
    with connect(dsn) as conn:
        conn.execute("DELETE FROM chat.conversations WHERE conversation_id = %s", (table.conversation,))
    assert _rows_of(dsn) == (1, 1, 1)
    assert held(world, kept) is not None


@needs_db
def test_deleting_the_gms_account_cascades_the_whole_chain_and_the_deferred_check_passes(dsn: str) -> None:
    world = _pg_world(dsn, Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5)))
    admit(world, a_table(world))
    admit(world, a_table(world, world.other), OTHER_INV)
    with connect(dsn) as conn:
        conn.execute("DELETE FROM auth.users WHERE id = %s", (world.owner,))
    assert _rows_of(dsn) == (1, 1, 1)
    assert _count(dsn, "SELECT count(*) FROM campaign.tool_invocations WHERE owner_id = %s", (world.owner,)) == 0


@needs_db
def test_a_campaign_an_invocation_still_references_cannot_be_deleted(dsn: str) -> None:
    """I-26: refused at COMMIT and every row untouched — even once the
    conversation's own edge is out of the way, which leaves the invocation's
    deferred edge as the only thing refusing it."""
    import psycopg

    world = _pg_world(dsn, Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5)))
    table = a_table(world)
    admit(world, table)
    with connect(dsn) as conn:
        conn.execute("UPDATE chat.conversations SET campaign_id = NULL WHERE conversation_id = %s",
                      (table.conversation,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (table.campaign,))
    assert _rows_of(dsn) == (1, 1, 1)
    assert _count(dsn, "SELECT count(*) FROM campaign.campaigns WHERE id = %s", (table.campaign,)) == 1


# ── PostgreSQL only: two connections ─────────────────────────────────────────

#: The races wait for a THREAD, so the lock wait is the most RQ-8 allows under
#: a ten-second gate, and the transaction bound is long enough to be released.
PATIENT = CampaignLockSettings(lock_timeout_s=4, transaction_timeout_s=30)
PATIENCE = 15


def _race_world(dsn: str) -> World:
    return _pg_world(dsn, Database(dsn, PoolSettings(sync_max=8, async_max=0, acquire_timeout_s=10), PATIENT))


def _someone_waits_on_a_lock(dsn: str, waiters: int = 1) -> bool:
    """Whether at least `waiters` backends of this database are blocked on a
    lock — the server's own account, advisory locks included."""
    deadline = time.monotonic() + PATIENCE
    with connect(dsn) as conn:
        while time.monotonic() < deadline:
            blocked = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if blocked >= waiters:
                return True
            time.sleep(0.05)
    return False


@contextmanager
def _holding(dsn: str, statement: str, params: tuple[Any, ...]) -> Iterator[None]:
    """A third connection that runs `statement` and keeps its transaction open
    until the block ends."""
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with connect(dsn, autocommit=False) as conn:
                conn.execute(statement, params)
                took.set()
                release.wait(PATIENCE)
                conn.commit()
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE), "the holder never took its lock"
    if failure:
        raise failure[0]
    try:
        yield
    finally:
        release.set()
        thread.join(PATIENCE)
    if failure:
        raise failure[0]


@contextmanager
def _holding_a_transaction(db: Database, work: Callable[[Any], None]) -> Iterator[None]:
    """`work` in a transaction of the service's own, kept open until the block
    ends, then committed."""
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with db.transaction() as unit:
                work(unit)
                took.set()
                release.wait(PATIENCE)
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE)
    if failure:
        raise failure[0]
    try:
        yield
    finally:
        release.set()
        thread.join(PATIENCE)
    if failure:
        raise failure[0]


def _started(work: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    produced: list[object] = []

    def run() -> None:
        try:
            produced.append(work())
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            produced.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, produced


def _finished(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(PATIENCE)
        assert not thread.is_alive(), "a transaction never finished"


def _admitted_under_the_cap(world: World, table: Table, invocation_id: str, cap: int) -> str:
    """The admitting transaction's shape, as far as the store owns it: the lock
    first, then the repeat, then the count, then the writes."""
    with world.db.transaction() as unit:
        world.store.hold_in_flight_lock(unit, table.owner)
        if world.store.get_for_update(unit, table.owner, table.campaign, invocation_id) is not None:
            return "replayed"
        if len(world.store.in_flight_ids(unit, table.owner, now=T0)) >= cap:
            return "capped"
        _create(world, unit, table, _entry_in(world, unit, table.conversation, table.owner, invocation_id),
                invocation_id)
        return "admitted"


def _the_in_flight_lock(owner: int) -> tuple[str, tuple[int, int]]:
    return "SELECT pg_advisory_xact_lock(%s, %s)", (int(AdvisoryLock.WORKBENCH_IN_FLIGHT), advisory_key(str(owner)))


@needs_db
def test_two_admissions_at_one_below_the_cap_admit_exactly_one(dsn: str) -> None:
    """X-5 across instances: both wait on the GM's lock, and whichever runs
    second counts the first one's committed row."""
    world = _race_world(dsn)
    table = a_table(world)
    with _holding(dsn, *_the_in_flight_lock(table.owner)):
        one, first = _started(lambda: _admitted_under_the_cap(world, table, INV, cap=1))
        two, second = _started(lambda: _admitted_under_the_cap(world, table, OTHER_INV, cap=1))
        assert _someone_waits_on_a_lock(dsn, 2), "both admissions wait on the GM's in-flight lock"
    _finished(one, two)
    assert sorted(map(str, first + second)) == ["admitted", "capped"]
    assert _rows_of(dsn) == (1, 1, 1)


@needs_db
def test_the_same_new_id_twice_at_once_writes_one_invocation_and_one_attempt(dsn: str) -> None:
    world = _race_world(dsn)
    table = a_table(world)
    with _holding(dsn, *_the_in_flight_lock(table.owner)):
        one, first = _started(lambda: _admitted_under_the_cap(world, table, INV, cap=2))
        two, second = _started(lambda: _admitted_under_the_cap(world, table, INV, cap=2))
        assert _someone_waits_on_a_lock(dsn, 2)
    _finished(one, two)
    assert sorted(map(str, first + second)) == ["admitted", "replayed"]
    assert _rows_of(dsn) == (1, 1, 1)


def _complete(world: World, table: Table, *, now: datetime) -> str:
    """A completion: the fence first, then the terminal write — or nothing."""
    with world.db.transaction() as unit:
        if world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=now) is None:
            return "fenced out"
        world.store.settle(unit, table.owner, table.campaign, INV, attempt=1, status="done", outcome="done",
                           result={"k": 1}, error=None, now=now)
        return "completed"


def _expire(world: World, table: Table, unit: Any, *, now: datetime) -> str:
    """A reader's lazy expiry: hold the row, and end it if it is past due."""
    row = world.store.get_for_update(unit, table.owner, table.campaign, INV)
    if row is None or row.status != "working" or row.attempt_deadline > now:
        return "left alone"
    world.store.settle(unit, table.owner, table.campaign, INV, attempt=row.attempt, status="failed",
                       outcome="expired", result=None, error=EXPIRED, now=now)
    return "expired"


@needs_db
@pytest.mark.parametrize("first", ["reader", "completion"])
def test_a_completion_racing_a_readers_expiry_ends_in_exactly_one_terminal_state(dsn: str, first: str) -> None:
    world = _race_world(dsn)
    table = a_table(world)
    admit(world, table)
    late, due = T0 + TTL + timedelta(seconds=1), T0 + TTL - timedelta(seconds=1)
    if first == "reader":
        with _holding_a_transaction(world.db, lambda unit: _expire(world, table, unit, now=late)):
            thread, outcome = _started(lambda: _complete(world, table, now=due))
            assert _someone_waits_on_a_lock(dsn), "the completion's fence waits on the reader's hold"
        _finished(thread)
        assert outcome == ["fenced out"]
        expected = ("failed", "expired")
    else:
        def completing(unit: Any) -> None:
            assert world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=due)
            world.store.settle(unit, table.owner, table.campaign, INV, attempt=1, status="done",
                               outcome="done", result={"k": 1}, error=None, now=due)

        with _holding_a_transaction(world.db, completing):
            thread, outcome = _started(lambda: _read_and_expire(world, table, now=late))
            assert _someone_waits_on_a_lock(dsn), "the reader waits on the completion's fence"
        _finished(thread)
        assert outcome == ["left alone"]
        expected = ("done", "done")
    final = held(world, table)
    assert final is not None and final.status == expected[0]
    assert [a.outcome for a in attempts(world, table)] == [expected[1]]


def _read_and_expire(world: World, table: Table, *, now: datetime) -> str:
    with world.db.transaction() as unit:
        return _expire(world, table, unit, now=now)


def _cancel(world: World, table: Table, unit: Any) -> None:
    world.store.set_cancel_requested(unit, table.owner, table.campaign, INV, now=T0 + timedelta(seconds=2))


@needs_db
@pytest.mark.parametrize("first", ["cancel", "completion"])
def test_a_cancel_and_a_completion_end_the_same_in_either_order(dsn: str, first: str) -> None:
    """D3 and RAIL-23, under two connections: `done` with the flag raised."""
    world = _race_world(dsn)
    table = a_table(world)
    admit(world, table)
    moment = T0 + timedelta(seconds=3)
    if first == "cancel":
        with _holding_a_transaction(world.db, lambda unit: _cancel(world, table, unit)):
            thread, outcome = _started(lambda: _complete(world, table, now=moment))
            assert _someone_waits_on_a_lock(dsn), "the fence waits on the cancel's row lock"
        _finished(thread)
        assert outcome == ["completed"]
    else:
        def completing(unit: Any) -> None:
            assert world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=moment)
            world.store.settle(unit, table.owner, table.campaign, INV, attempt=1, status="done",
                               outcome="done", result={"k": 1}, error=None, now=moment)

        def cancelling() -> None:
            with world.db.transaction() as unit:
                _cancel(world, table, unit)

        with _holding_a_transaction(world.db, completing):
            thread, _ = _started(cancelling)
            assert _someone_waits_on_a_lock(dsn), "the cancel waits on the completion's fence"
        _finished(thread)
    final = held(world, table)
    assert final is not None and (final.status, final.cancel_requested, final.result) == ("done", True, {"k": 1})


@needs_db
def test_a_late_completion_during_and_after_the_next_attempts_admission_is_fenced_out(dsn: str) -> None:
    """AE-84 under two connections: attempt 1 expired and attempt 2 is being
    admitted when attempt 1's result arrives; it is discarded then and after."""
    world = _race_world(dsn)
    table = a_table(world)
    admit(world, table)
    settle(world, table, status="failed", outcome="expired", error=EXPIRED, now=T0 + TTL)
    moment = T0 + TTL + timedelta(seconds=1)

    def admitting(unit: Any) -> None:
        assert world.store.get_for_update(unit, table.owner, table.campaign, INV) is not None
        world.store.start_retry(unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=INV,
                                attempt=1, operation_id=uuid.uuid4().hex, attempt_deadline=moment + TTL,
                                now=moment)

    with _holding_a_transaction(world.db, admitting):
        assert _complete(world, table, now=T0 + timedelta(seconds=1)) == "fenced out"
    assert _complete(world, table, now=T0 + timedelta(seconds=1)) == "fenced out"
    final = held(world, table)
    assert final is not None and (final.status, final.attempt, final.result) == ("working", 2, None)
    assert [a.outcome for a in attempts(world, table)] == ["expired", None]

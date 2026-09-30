"""The document tools against a real PostgreSQL (agent-forge-harness-1kg.4.4,
PR 1 of 2): group D of the alignment brief.

The route-level rules run on the twins in `service/tests/test_document_tools_api.py`.
What only the server can prove is here: that the document the executor writes
in `finish` lives and dies with the fenced completion transaction (X-6,
RAIL-27), that a late attempt fenced out writes nothing, and that creation
takes no campaign lock and touches no other campaign table (RQ-2, M-5, X-2).

These tests need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_document_tools_db.py -q
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database
from langchain_core.messages import AIMessage

from service import migrations as mig
from service import tool_invocations
from service.campaign_store import PostgresCampaignStore
from service.conversation_store import PostgresConversationStore
from service.db import Database, PoolSettings
from service.document_store import PostgresDocumentStore
from service.document_tools import DocumentToolStores, document_executors
from service.history import PostgresMessageStore
from service.model_catalog import DEFAULT_ALIAS
from service.providers import ProviderClientFactory
from service.session import SessionData
from service.tests import document_generation_fixtures as fx
from service.timeline_store import PostgresTimelineStore, TimelineStoreError
from service.tool_invocation_store import PostgresToolInvocationStore
from service.tool_invocations import (
    ATTEMPT_TTL_S,
    Admission,
    ExecutionContext,
    InvocationStores,
    Replay,
    ToolSettings,
    cancellation_probe,
    context_reader,
)
from service.workbench_contracts import ToolId, ToolInvocationRequest

pytestmark = needs_db

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=ATTEMPT_TTL_S)
INV = "inv_docdb_000000000001"
#: What creating a document may write in the campaign schema; nothing else there may change.
WRITTEN = frozenset({"documents", "document_versions", "tool_invocations", "tool_attempts"})
LOCK_QUERY = (
    "SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'relation' "
    "AND relation = 'campaign.authz_state'::regclass"
)


class ScriptedLLM:
    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.calls = 0

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls += 1
        return AIMessage(content=self.script[min(self.calls, len(self.script)) - 1])


@dataclass
class World:
    dsn: str
    db: Database
    stores: InvocationStores
    tools: DocumentToolStores
    executors: dict[ToolId, Any]
    llm: ScriptedLLM
    owner: int
    campaign: str
    conversation: str

    def count(self, table: str) -> int:
        """Every row of that table: the database is this test's alone."""
        with connect(self.dsn) as conn:
            return int(conn.execute(f"SELECT count(*) FROM campaign.{table}").fetchone()[0])  # noqa: S608

    def census(self) -> dict[str, int]:
        """Every base table of the campaign schema, by its row count."""
        with connect(self.dsn) as conn:
            names = [row[0] for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'campaign' "
                "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()]
            return {name: int(conn.execute(f'SELECT count(*) FROM campaign."{name}"').fetchone()[0])  # noqa: S608
                    for name in names}


@pytest.fixture
def world() -> Iterator[World]:
    with throwaway_database("doctools") as dsn:
        mig.migrate(dsn)
        with connect(dsn) as conn:
            owner = int(conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id",
            ).fetchone()[0])
        db = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))
        campaigns, conversations = PostgresCampaignStore(), PostgresConversationStore()
        timeline = PostgresTimelineStore()
        with db.transaction() as unit:
            campaign = campaigns.create(unit, owner_id=owner, name="Nocturne").id
            conversation = conversations.create(unit, owner_id=owner, campaign_id=campaign, title=None,
                                                started_mode=None).id
        PostgresMessageStore(db=db).claim_conversation(conversation, owner)
        stores = InvocationStores(PostgresToolInvocationStore(), campaigns, conversations, timeline)
        tools = DocumentToolStores(campaigns, conversations, timeline, PostgresDocumentStore())
        yield World(dsn, db, stores, tools, dict(document_executors(tools)), ScriptedLLM(fx.envelope(
            fx.BASE_FIELDS[fx.NPC])), owner, campaign, conversation)


def _admit(world: World, now: datetime) -> Admission | Replay:
    request = ToolInvocationRequest.model_validate({
        "schema_version": 1, "invocation_id": INV, "tool_id": "npc", "brief": "A smith",
        "campaign_id": world.campaign, "conversation_id": world.conversation,
    })
    return tool_invocations.submit(world.db, world.stores, world.executors, ToolSettings(frozenset({ToolId.NPC})),
                                   SessionData(user_id=world.owner, role="dm"), request, now=now,
                                   chat_turns_today=0)


def _ctx(world: World, admitted: Admission, now: datetime) -> ExecutionContext:
    def clock() -> datetime:
        return now

    return ExecutionContext(admitted, clock=clock,
                            factory=ProviderClientFactory(client_builders={DEFAULT_ALIAS: world.llm}),
                            probe=cancellation_probe(world.db, world.stores, admitted, clock),
                            reader=context_reader(world.db))


def _execute(world: World, admitted: Admission, ctx: ExecutionContext) -> tool_invocations.Outcome:
    return tool_invocations.execute(world.executors[ToolId.NPC], ctx, available=lambda tool: True)


def _complete(world: World, admitted: Admission, ctx: ExecutionContext, outcome: tool_invocations.Outcome,
              now: datetime) -> Any:
    return tool_invocations.complete(world.db, world.stores, world.executors[ToolId.NPC], admitted, outcome, ctx,
                                     now=now)


def _attempt(world: World, now: datetime) -> Any:
    admitted = _admit(world, now)
    if isinstance(admitted, Replay):
        return admitted.invocation
    ctx = _ctx(world, admitted, now + timedelta(seconds=5))
    return _complete(world, admitted, ctx, _execute(world, admitted, ctx), now + timedelta(seconds=5))


def test_d1_one_invocation_writes_one_sealed_assistant_version_and_a_replay_adds_nothing(world: World) -> None:
    done = _attempt(world, T0)
    assert done.status.value == "done"
    with connect(world.dsn) as conn:
        rows = conn.execute(
            "SELECT d.id, v.number, v.author, v.sealed_at IS NOT NULL FROM campaign.documents d "
            "JOIN campaign.document_versions v ON v.document_id = d.id WHERE d.campaign_id = %s",
            (world.campaign,)).fetchall()
    assert [tuple(row) for row in rows] == [(done.result.document.document_id, 1, "assistant", True)]
    assert _attempt(world, T0 + timedelta(seconds=30)) == done
    assert (world.count("documents"), world.llm.calls) == (1, 1)


def test_d2_a_settle_that_fails_after_the_document_was_written_leaves_no_document(world: World) -> None:
    written: list[int] = []

    class _Counting(PostgresDocumentStore):
        def create(self, *args: Any, **kwargs: Any) -> Any:
            made = super().create(*args, **kwargs)
            written.append(1)
            return made

    class _Broken(PostgresTimelineStore):
        def replace_tool_invocation(self, *args: Any, **kwargs: Any) -> Any:
            raise TimelineStoreError("refused")

    world.executors = dict(document_executors(DocumentToolStores(
        world.tools.campaigns, world.tools.conversations, world.tools.timeline, _Counting())))
    admitted = _admit(world, T0)
    assert isinstance(admitted, Admission)
    world.stores = InvocationStores(world.stores.invocations, world.stores.campaigns, world.stores.conversations,
                                    _Broken())
    ctx = _ctx(world, admitted, T0)
    outcome = _execute(world, admitted, ctx)
    with pytest.raises(TimelineStoreError):
        _complete(world, admitted, ctx, outcome, T0)
    assert written == [1]
    assert (world.count("documents"), world.count("document_versions")) == (0, 0)


def test_d3_a_late_attempt_fenced_out_writes_nothing_and_its_retry_writes_one(world: World) -> None:
    world.llm.script = [fx.envelope({**fx.BASE_FIELDS[fx.NPC], "name": n}) for n in ("First", "Second")]
    first = _admit(world, T0)
    assert isinstance(first, Admission)
    first_ctx = _ctx(world, first, T0)
    first_outcome = _execute(world, first, first_ctx)
    later = T0 + TTL + timedelta(seconds=1)
    second = _admit(world, later)
    assert isinstance(second, Admission) and second.attempt == 2
    second_ctx = _ctx(world, second, later)
    second_outcome = _execute(world, second, second_ctx)
    assert _complete(world, first, first_ctx, first_outcome, later).status.value == "working"
    assert world.count("documents") == 0
    done = _complete(world, second, second_ctx, second_outcome, later)
    assert (done.status.value, done.result.document.title) == ("done", "Second")
    assert (world.count("documents"), world.count("document_versions")) == (1, 1)


def test_d4_creation_takes_no_campaign_lock_and_changes_no_other_campaign_table(world: World) -> None:
    held: list[list[str]] = []

    class _Watching(PostgresDocumentStore):
        def create(self, unit: Any, *args: Any, **kwargs: Any) -> Any:
            made = super().create(unit, *args, **kwargs)
            held.append([row[0] for row in unit.conn.execute(LOCK_QUERY).fetchall()])
            return made

    with world.db.transaction() as unit:  # the positive control: the query sees a campaign lock
        unit.lock_campaign(world.campaign, shared=True)
        control = [row[0] for row in unit.conn.execute(LOCK_QUERY).fetchall()]
    assert control == ["RowShareLock"]
    world.executors = dict(document_executors(DocumentToolStores(
        world.tools.campaigns, world.tools.conversations, world.tools.timeline, _Watching())))
    before = world.census()
    assert _attempt(world, T0).status.value == "done"
    after = world.census()
    assert held == [[]]
    assert {name for name in after if after[name] != before.get(name)} == {
        "documents", "document_versions", "tool_invocations", "tool_attempts"}
    assert {name: count for name, count in after.items() if name not in WRITTEN} == {
        name: count for name, count in before.items() if name not in WRITTEN}

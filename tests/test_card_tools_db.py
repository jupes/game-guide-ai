"""The five card tools against a real PostgreSQL (agent-forge-harness-1kg.4.3):
group P of the alignment brief.

The route-level rules run on the twins in `service/tests/test_card_executors.py`.
What only the server can prove is here: that each card kind's `CardResult`, and
a `not_in_sources` failure, round-trip through `complete`, `read` and the
timeline window as identical JSON — and, I-28, that a monster card's ability
is stored under the wire's own key `int`, never Pydantic's `int_`, in the
JSONB column itself.

These tests need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_card_tools_db.py -q
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database
from langchain_core.messages import AIMessage

from ingestion.retrieval import RetrievalResult, RetrievedChunk
from service import card_executors as ce
from service import migrations as mig
from service import ratelimit, tool_invocations
from service.campaign_store import PostgresCampaignStore
from service.conversation_store import PostgresConversationStore
from service.db import Database, PoolSettings
from service.history import PostgresMessageStore
from service.model_catalog import DEFAULT_ALIAS
from service.providers import ProviderClientFactory
from service.session import SessionData
from service.tests import card_generation_fixtures as fx
from service.timeline_store import PostgresTimelineStore
from service.tool_invocation_store import PostgresToolInvocationStore
from service.tool_invocations import (
    Admission,
    ExecutionContext,
    InvocationStores,
    Replay,
    ToolSettings,
    cancellation_probe,
    context_reader,
)
from service.workbench_contracts import ToolId, ToolInvocationRequest
from service.workbench_load import PostgresWorkbenchLoad

pytestmark = needs_db

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
CARD_TOOLS = (ToolId.MONSTER, ToolId.LOOT, ToolId.NAMES, ToolId.HOOKS, ToolId.RULES)


class ScriptedLLM:
    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.calls = 0

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls += 1
        return AIMessage(content=self.script[min(self.calls, len(self.script)) - 1])


class FakeRetriever:
    def __init__(self, result: RetrievalResult) -> None:
        self._result = result

    def retrieve(self, prompt: str, k: int, reranker: Any, mode: str) -> RetrievalResult:
        return self._result


@dataclass
class FakeRag:
    retriever: FakeRetriever
    reranker: Any = None


def _rules_result(answerable: bool = True) -> RetrievalResult:
    chunk = RetrievedChunk(
        chunk_id="c1", content_type="rule", entity_name=None, class_name=None, feature_name=None,
        chapter="Conditions", section="Held", page_start=12, text_preview="preview", cosine_distance=0.1,
    )
    return RetrievalResult(
        chunks=[chunk] if answerable else [], full_texts={"c1": "A held creature's speed becomes 0."},
        top1_distance=0.1 if answerable else 0.9, answerable=answerable, book_by_id={"c1": "synthetic-5e"},
    )


@dataclass
class World:
    dsn: str
    db: Database
    stores: InvocationStores
    executors: dict[ToolId, Any]
    llm: ScriptedLLM
    owner: int
    campaign: str
    conversation: str

    def count(self, table: str) -> int:
        with connect(self.dsn) as conn:
            return int(conn.execute(f"SELECT count(*) FROM campaign.{table}").fetchone()[0])  # noqa: S608

    def stored_result(self, invocation_id: str) -> Any:
        with connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT result FROM campaign.tool_invocations WHERE invocation_id = %s", (invocation_id,),
            ).fetchone()
            return row[0]


@pytest.fixture(autouse=True)
def _fresh_window() -> Iterator[None]:
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    with throwaway_database("cardtools") as dsn:
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
        stores = InvocationStores(
            PostgresToolInvocationStore(), campaigns, conversations, timeline, PostgresWorkbenchLoad(),
        )
        llm = ScriptedLLM(fx.envelope(fx.MONSTER))
        monkeypatch.setattr(ce, "_provider", lambda: FakeRag(FakeRetriever(_rules_result())))
        yield World(dsn, db, stores, dict(ce.card_executors()), llm, owner, campaign, conversation)


def _admit(world: World, invocation_id: str, tool: ToolId, now: datetime) -> Admission | Replay:
    request = ToolInvocationRequest.model_validate({
        "schema_version": 1, "invocation_id": invocation_id, "tool_id": tool.value, "brief": fx.BRIEFS[tool],
        "campaign_id": world.campaign, "conversation_id": world.conversation,
    })
    return tool_invocations.submit(world.db, world.stores, world.executors, ToolSettings(frozenset(CARD_TOOLS)),
                                   SessionData(user_id=world.owner, role="dm"), request, now=now,
                                   chat_turns_today=0)


def _ctx(world: World, admitted: Admission, now: datetime) -> ExecutionContext:
    def clock() -> datetime:
        return now

    return ExecutionContext(admitted, clock=clock,
                            factory=ProviderClientFactory(client_builders={DEFAULT_ALIAS: world.llm}),
                            probe=cancellation_probe(world.db, world.stores, admitted, clock),
                            reader=context_reader(world.db))


def _attempt(world: World, invocation_id: str, tool: ToolId, now: datetime) -> Any:
    admitted = _admit(world, invocation_id, tool, now)
    if isinstance(admitted, Replay):
        return admitted.invocation
    ctx = _ctx(world, admitted, now)
    outcome = tool_invocations.execute(world.executors[tool], ctx, available=lambda t: True)
    return tool_invocations.complete(world.db, world.stores, world.executors[tool], admitted, outcome, ctx, now=now)


@pytest.mark.parametrize("tool", CARD_TOOLS)
def test_p1_each_card_kind_round_trips_identically_through_complete_read_and_the_timeline(
    world: World, tool: ToolId) -> None:
    """Kills a JSONB round-trip loss (a `Source` `null`, a nested entry)."""
    world.llm = ScriptedLLM(fx.envelope(tool))
    invocation_id = f"inv_carddb_{tool.value}".ljust(16, "0")
    done = _attempt(world, invocation_id, tool, T0)
    assert done.status.value == "done"
    read_back = tool_invocations.read(world.db, world.stores, world.owner, world.campaign, invocation_id, now=T0)
    assert read_back.model_dump(mode="json", by_alias=True) == done.model_dump(mode="json", by_alias=True)
    with world.db.transaction() as unit:
        [entry] = world.stores.timeline.entry_window(unit, world.conversation, before=None, limit=50)
    assert entry.payload["invocation"] == done.model_dump(mode="json", by_alias=True)


def test_p1b_a_rules_corpus_miss_stores_not_in_sources_final(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ce, "_provider", lambda: FakeRag(FakeRetriever(_rules_result(answerable=False))))
    invocation_id = "inv_carddb_miss000"
    done = _attempt(world, invocation_id, ToolId.RULES, T0)
    assert (done.status.value, done.error.code.value, done.error.retryable) == ("failed", "not_in_sources", False)
    assert world.llm.calls == 0
    read_back = tool_invocations.read(world.db, world.stores, world.owner, world.campaign, invocation_id, now=T0)
    assert read_back.model_dump(mode="json") == done.model_dump(mode="json")


def test_p1_i28_a_monster_stores_int_never_int_underscore_in_the_jsonb_column(world: World) -> None:
    """1kg.4.3 I-28: the stat block's ability is `int` in the raw stored JSONB,
    not Pydantic's `int_` spelling — checked at the database, not just through
    the Python model that would otherwise mask it."""
    invocation_id = "inv_carddb_int0001"
    done = _attempt(world, invocation_id, ToolId.MONSTER, T0)
    assert done.status.value == "done"
    stored = world.stored_result(invocation_id)
    raw = stored if isinstance(stored, dict) else json.loads(stored)
    abilities = raw["card"]["stat_block"]["abilities"]
    assert "int" in abilities and "int_" not in abilities

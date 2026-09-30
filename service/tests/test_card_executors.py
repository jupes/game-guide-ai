"""`service/card_executors.py` (agent-forge-harness-1kg.4.3): group O of the
alignment brief, plus unit-level coverage of `CardExecutor`/`RulesExecutor`.
Every store is an in-memory twin; the LLM is a scripted fake behind the real
`ProviderClientFactory`; the rules corpus is a scripted fake `RagLike`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from ingestion.retrieval import RetrievalResult, RetrievedChunk
from service import card_executors as ce
from service import card_generation as cg
from service import document_tools, generate, tool_invocations, tool_invocations_api, usage_capture
from service.app import app, get_message_store, get_timeline_database, get_timeline_store, require_session
from service.campaign_store import InMemoryCampaignStore
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase
from service.document_store import InMemoryDocumentStore
from service.history import InMemoryMessageStore
from service.model_catalog import DEFAULT_ALIAS
from service.models import Source
from service.providers import ProviderClientFactory
from service.session import SessionData
from service.tests import card_generation_fixtures as fx
from service.timeline_store import InMemoryTimelineStore
from service.tool_invocation_store import InMemoryToolInvocationStore
from service.tool_invocations import (
    Admission,
    ExecutionContext,
    InvocationStores,
    InvocationTarget,
    ToolSettings,
    cancellation_probe,
    context_reader,
)
from service.workbench_contracts import ToolId, ToolInvocationRequest
from service.workbench_load import InMemoryWorkbenchLoad

GM_A, GM_B = 1, 2
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=tool_invocations.ATTEMPT_TTL_S)
CARD_TOOLS = (ToolId.MONSTER, ToolId.LOOT, ToolId.NAMES, ToolId.HOOKS, ToolId.RULES)


def good(tool: ToolId) -> str:
    return fx.envelope(tool)


class ScriptedLLM:
    """The provider: answers from a script; an item may be a callable run first."""

    def __init__(self, tool: ToolId = ToolId.MONSTER) -> None:
        self.script: list[Any] = [good(tool)]
        self.calls: list[dict[str, Any]] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append({"input": input, "config": config, **kwargs})
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        while callable(item) and not isinstance(item, BaseException):
            item = item()
        if isinstance(item, BaseException):
            raise item
        return AIMessage(content=item, usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8})


def _chunk(n: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"c{n}", content_type="rule", entity_name=None, class_name=None, feature_name=None,
        chapter="Conditions", section=f"Section {n}", page_start=n, text_preview=f"preview {n}",
        cosine_distance=0.1,
    )


def _result(n: int = 1, *, answerable: bool = True) -> RetrievalResult:
    chunks = [_chunk(i) for i in range(1, n + 1)]
    return RetrievalResult(
        chunks=chunks, full_texts={c.chunk_id: f"A held creature's speed becomes 0. (passage {c.chunk_id})"
                                    for c in chunks},
        top1_distance=0.1 if answerable else 0.9, answerable=answerable,
        book_by_id={c.chunk_id: "synthetic-5e" for c in chunks},
    )


class FakeRetriever:
    def __init__(self, result: RetrievalResult | Callable[[], RetrievalResult] | BaseException) -> None:
        self._result = result
        self.calls: list[tuple[Any, ...]] = []

    def retrieve(self, prompt: str, k: int, reranker: Any, mode: str) -> RetrievalResult:
        self.calls.append((prompt, k, reranker, mode))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result() if callable(self._result) else self._result


@dataclass
class FakeRag:
    retriever: FakeRetriever
    reranker: Any = None


class Sink:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def write(self, rows: Any) -> int:
        self.rows.extend(rows)
        return len(rows)


@dataclass(frozen=True)
class Table:
    owner: int
    campaign: str
    conversation: str


@dataclass
class World:
    db: InMemoryDatabase
    messages: InMemoryMessageStore
    stores: InvocationStores
    executors: dict[ToolId, Any]
    llm: ScriptedLLM
    factory: ProviderClientFactory
    rag: FakeRag | None
    sink: Sink
    outcomes: list[str]
    settings: ToolSettings = field(default_factory=lambda: ToolSettings(frozenset(CARD_TOOLS)))
    now: list[datetime] = field(default_factory=lambda: [T0])

    def clock(self) -> datetime:
        return self.now[0]

    def table(self, owner: int = GM_A, *, name: str = "Nocturne") -> Table:
        with self.db.transaction() as unit:
            campaign = self.stores.campaigns.create(unit, owner_id=owner, name=name, now=self.now[0]).id
            conversation = self.stores.conversations.create(
                unit, owner_id=owner, campaign_id=campaign, title=None, started_mode=None,
            )
        self.messages.claim_conversation(conversation.id, owner)
        return Table(owner, campaign, conversation.id)

    def count(self, name: str) -> int:
        with self.db.transaction() as unit:
            from service.campaign_store import shared_rows

            return len(shared_rows(self.db, name).visible(unit))

    def entries(self, table: Table) -> list[dict[str, Any]]:
        with self.db.transaction() as unit:
            found = self.stores.timeline.entry_window(unit, table.conversation, before=None, limit=50)
        return [cast(dict[str, Any], row.payload) for row in found]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    db = InMemoryDatabase()
    messages = InMemoryMessageStore()
    campaigns, conversations = InMemoryCampaignStore(db), InMemoryConversationStore(db)
    timeline = InMemoryTimelineStore(db, messages=messages)
    stores = InvocationStores(
        InMemoryToolInvocationStore(db), campaigns, conversations, timeline, InMemoryWorkbenchLoad(db),
    )
    doc_tools = document_tools.DocumentToolStores(campaigns, conversations, timeline, InMemoryDocumentStore(db))
    llm = ScriptedLLM()
    factory = ProviderClientFactory(client_builders={DEFAULT_ALIAS: llm})
    outcomes: list[str] = []
    executors = {**document_tools.document_executors(doc_tools), **ce.card_executors()}
    made = World(db, messages, stores, executors, llm, factory, None, Sink(), outcomes)
    overrides: dict[Callable[..., Any], Callable[..., Any]] = {
        tool_invocations_api.get_invocation_stores: lambda: made.stores,
        tool_invocations_api.get_tool_executors: lambda: made.executors,
        tool_invocations_api.get_tool_settings: lambda: made.settings,
        tool_invocations_api.get_clock: lambda: made.clock,
        tool_invocations_api.get_provider_factory: lambda: made.factory,
        get_timeline_database: lambda: made.db,
        get_timeline_store: lambda: made.stores.timeline,
        get_message_store: lambda: made.messages,
    }
    app.dependency_overrides.update(overrides)
    monkeypatch.setattr(usage_capture, "_ledger_provider", lambda: made.sink)
    monkeypatch.setattr(ce, "_provider", lambda: made.rag)
    real_outcome = usage_capture._emit_outcome_record

    def outcome(operation: Any, fields: dict[str, Any]) -> None:
        outcomes.append(f"{fields.get('purpose')}:{fields.get('outcome')}")
        real_outcome(operation, fields)

    monkeypatch.setattr(usage_capture, "_emit_outcome_record", outcome)
    monkeypatch.setattr(generate, "_RETRY_BACKOFF_SECONDS", 0.0)
    _as(GM_A)
    yield made
    for dependency in (*overrides, require_session):
        app.dependency_overrides.pop(cast(Any, dependency), None)


@pytest.fixture
def client(world: World) -> TestClient:
    return TestClient(app)


def _as(user_id: int) -> None:
    def session(request: Request) -> SessionData:
        return SessionData(user_id=user_id, role="dm")

    app.dependency_overrides[require_session] = session


_BRIEFS = {ToolId.MONSTER: "a brute", ToolId.LOOT: "a chest", ToolId.NAMES: "five dockworkers",
           ToolId.HOOKS: "a rumour", ToolId.RULES: "what happens when held"}


def body(table: Table, *, tool: ToolId, invocation_id: str, brief: str | None = None,
          campaign: str | None = None, conversation: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1, "invocation_id": invocation_id, "tool_id": tool.value,
        "brief": brief if brief is not None else _BRIEFS[tool],
        "campaign_id": campaign or table.campaign, "conversation_id": conversation or table.conversation,
    }


def _wire_id(name: str) -> str:
    """`InvocationId` is 16 to 64 characters; pad a short, readable test name."""
    return name.ljust(16, "0")


def post(client: TestClient, table: Table, invocation_id: str, **kwargs: Any) -> Any:
    made = body(table, invocation_id=_wire_id(invocation_id), **kwargs)
    return client.post(f"/campaigns/{made['campaign_id']}/tool-invocations", json=made)


# ── O-1: each tool completes, and the entry agrees with the GET ─────────────


@pytest.mark.parametrize("tool", CARD_TOOLS)
def test_o1_each_card_tool_completes_and_the_entry_matches_the_status_read(
    world: World, client: TestClient, tool: ToolId) -> None:
    world.rag = FakeRag(FakeRetriever(_result()))
    world.llm.script = [good(tool)]
    table = world.table()
    inv = f"inv_card_{tool.value}"
    response = post(client, table, inv, tool=tool)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["status"] == "done"
    assert answer["result"]["card"]["card_kind"] == (
        "stat_block" if tool is ToolId.MONSTER else tool.value
    )
    status = client.get(f"/campaigns/{table.campaign}/tool-invocations/{_wire_id(inv)}")
    assert status.json() == answer
    [entry] = world.entries(table)
    assert entry["invocation"] == answer


# ── O-2: a malformed answer is provider_failed, retryable; a retry runs every guard ─


def test_o2_a_malformed_answer_is_provider_failed_and_a_retry_runs_every_guard(
    world: World, client: TestClient) -> None:
    world.llm.script = ["not json", good(ToolId.MONSTER)]
    table = world.table()
    inv = "inv_card_bad"
    failed = post(client, table, inv, tool=ToolId.MONSTER).json()
    assert (failed["status"], failed["error"]["code"], failed["error"]["retryable"]) == (
        "failed", "provider_failed", True,
    )
    assert world.outcomes[-1] == "card_generation:parse_failure"
    world.now[0] = T0 + timedelta(seconds=30)
    done = post(client, table, inv, tool=ToolId.MONSTER).json()
    assert (done["status"], done["attempt"]) == ("done", 2)


# ── O-3: a rules miss is not_in_sources, final, and spends no generation call ─


def test_o3_a_rules_miss_is_not_in_sources_final_with_no_generation_call(
    world: World, client: TestClient) -> None:
    world.rag = FakeRag(FakeRetriever(_result(answerable=False)))
    table = world.table()
    inv = "inv_rules_miss"
    answer = post(client, table, inv, tool=ToolId.RULES).json()
    assert (answer["status"], answer["error"]["code"], answer["error"]["retryable"]) == (
        "failed", "not_in_sources", False,
    )
    assert world.llm.calls == []
    assert world.outcomes == ["card_generation:skipped_by_gate"]
    again = post(client, table, inv, tool=ToolId.RULES)
    assert again.json()["status"] == "failed"
    assert world.llm.calls == []


# ── O-4: no RagService is backend_unavailable, retryable, 0 invokes ─────────


def test_o4_no_rag_service_is_backend_unavailable_retryable(world: World, client: TestClient) -> None:
    world.rag = None
    table = world.table()
    answer = post(client, table, "inv_rules_norag", tool=ToolId.RULES).json()
    assert (answer["status"], answer["error"]["code"], answer["error"]["retryable"]) == (
        "failed", "backend_unavailable", True,
    )
    assert world.llm.calls == []


# ── O-5: the retriever is called with exactly (brief, TOP_K, reranker, "rules") ─


def test_o5_the_retriever_is_called_with_the_chat_rules_scope(world: World, client: TestClient) -> None:
    from config import TOP_K

    retriever = FakeRetriever(_result())
    reranker = object()
    world.rag = FakeRag(retriever, reranker)
    table = world.table()
    post(client, table, "inv_rules_scope", tool=ToolId.RULES, brief="what happens when held")
    assert retriever.calls == [("what happens when held", TOP_K, reranker, "rules")]


# ── O-8: no model, provider or tier is ever named ────────────────────────────


def test_o8_no_model_or_tier_is_named_anywhere(world: World, client: TestClient) -> None:
    from typing import get_args

    from service.model_catalog import CATALOG, PUBLIC_MODELS, PublicTier

    world.rag = FakeRag(FakeRetriever(_result()))
    table = world.table()
    hidden = {alias.lower() for alias in CATALOG} | {m.lower() for m in PUBLIC_MODELS} | {
        tier.lower() for tier in get_args(PublicTier)
    }
    for tool in CARD_TOOLS:
        world.llm.script = [good(tool)]
        response = post(client, table, f"inv_card_names_{tool.value}", tool=tool)
        text = response.text.lower()
        assert not any(name in text for name in hidden if name), (tool, text)


# ── O-9b: `run` turns every InvalidCardOutput code into OutputRefused ───────


def test_o9b_a_refused_output_chains_no_cause_and_logs_the_code_once(
    world: World, client: TestClient, caplog: Any) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    world.llm.script = ["not json"]
    table = world.table()
    post(client, table, "inv_card_chain", tool=ToolId.MONSTER)
    warnings = [r for r in caplog.records if r.name == "service.card_executors"]
    assert len(warnings) == 1
    assert "not_json" in warnings[0].getMessage()


def test_o9b_unit_the_cause_and_context_are_both_none() -> None:
    executor = ce.CardExecutor(ToolId.MONSTER)
    ctx = _fake_ctx(ScriptedLLM(), ["not json"])
    try:
        executor._generate(ctx, cg.CardRequest(ToolId.MONSTER, "x"), 1)
    except tool_invocations.OutputRefused as exc:
        assert exc.__cause__ is None and exc.__context__ is None
    else:
        pytest.fail("expected OutputRefused")


def _fake_ctx(llm: ScriptedLLM, script: list[Any] | None = None) -> ExecutionContext:
    if script is not None:
        llm.script = script
    factory = ProviderClientFactory(client_builders={DEFAULT_ALIAS: llm})
    target = InvocationTarget(GM_A, "camp_x", "conv_x", ToolId.MONSTER, "a brief", None)
    admission = Admission(target, "inv_x", "entry_x", T0, 1, "op_x", T0 + TTL, DEFAULT_ALIAS)
    return ExecutionContext(admission, clock=lambda: T0, factory=factory, probe=lambda: None)


# ── O-10: suggestions are filtered against availability ──────────────────────


def test_o10_suggestions_keep_only_enabled_tools(world: World, client: TestClient) -> None:
    world.settings = ToolSettings(frozenset({ToolId.MONSTER, ToolId.LOOT}))  # encounter is not enabled
    table = world.table()
    answer = post(client, table, "inv_card_suggestions", tool=ToolId.MONSTER).json()
    tools = {s["tool_id"] for s in answer["result"]["suggestions"]}
    assert tools == {"loot"}


# ── O-11: precheck and finish are side-effect free ──────────────────────────


def test_o11_precheck_returns_none_and_finish_returns_its_input_unchanged() -> None:
    executor = ce.CardExecutor(ToolId.LOOT)
    assert executor.precheck(cast(Any, None), cast(Any, None)) is None
    sentinel = {"tool_id": "loot"}
    assert executor.finish(cast(Any, None), cast(Any, None), cast(Any, sentinel)) is sentinel


# ── O-12: the embedding provider is pinned to the allowlist ─────────────────


def test_o12_openai_is_on_the_workbench_allowlist_and_the_embed_alias_too() -> None:
    from service.model_catalog import WORKBENCH_PROVIDERS

    assert "openai" in WORKBENCH_PROVIDERS
    assert usage_capture.EMBED_MODEL  # a non-empty alias exists to record under


# ── Unit-level: the rules executor's own guards ─────────────────────────────


def test_rules_executor_cancel_between_retrieval_and_generation_ends_cancelled(
    world: World, client: TestClient) -> None:
    table = world.table()
    inv = "inv_rules_cancel"

    def cancel_then_result() -> RetrievalResult:
        client.post(f"/campaigns/{table.campaign}/tool-invocations/{inv}/cancel")
        return _result()

    world.rag = FakeRag(FakeRetriever(cancel_then_result))
    # First admit the row so the cancel endpoint has something to flag.
    admitted = tool_invocations.submit(
        world.db, world.stores, world.executors, world.settings, SessionData(user_id=GM_A, role="dm"),
        ToolInvocationRequest.model_validate(body(table, tool=ToolId.RULES, invocation_id=inv)),
        now=world.clock(), chat_turns_today=0,
    )
    assert isinstance(admitted, Admission)
    ctx = ExecutionContext(admitted, clock=world.clock, factory=world.factory,
                            probe=cancellation_probe(world.db, world.stores, admitted, world.clock),
                            reader=context_reader(world.db))
    outcome = tool_invocations.execute(world.executors[ToolId.RULES], ctx, available=lambda t: True)
    assert outcome.cancelled is True
    assert world.llm.calls == []


def test_rules_executor_out_of_time_before_the_embed_call(world: World) -> None:
    world.rag = FakeRag(FakeRetriever(_result()))
    admission = Admission(
        InvocationTarget(GM_A, "camp_x", "conv_x", ToolId.RULES, "a rules question", None),
        "inv_x", "entry_x", T0, 1, "op_x", T0 + timedelta(seconds=ce.EMBED_WORST_S + 1), DEFAULT_ALIAS,
    )
    ctx = ExecutionContext(admission, clock=lambda: T0, factory=world.factory, probe=lambda: None)
    with pytest.raises(tool_invocations.OutOfTime):
        ce.RulesExecutor().run(ctx)
    assert world.rag.retriever.calls == []


def test_rules_executor_trims_the_lowest_ranked_passage_until_it_fits(
    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ce, "GENERATION_CONTEXT_MAX_CHARS", 400)
    passages = tuple(
        cg.CorpusPassage(text="x" * 100, source=Source(book="synthetic-5e", page=n, snippet="s"))
        for n in range(1, 6)
    )
    bounded = ce.RulesExecutor._bounded_passages(
        cast(Any, type("Ctx", (), {"target": InvocationTarget(GM_A, "c", "v", ToolId.RULES, "q", None)})()),
        passages,
    )
    assert 1 <= len(bounded) < len(passages)


def test_card_executors_keys_equal_card_tools() -> None:
    assert set(ce.card_executors()) == set(cg.CARD_TOOLS)
    assert isinstance(ce.card_executors()[ToolId.RULES], ce.RulesExecutor)
    for tool in cg.CARD_TOOLS - {ToolId.RULES}:
        assert type(ce.card_executors()[tool]) is ce.CardExecutor

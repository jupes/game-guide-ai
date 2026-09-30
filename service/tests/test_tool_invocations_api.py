"""The GM's tool invocation routes (bead 1kg.4.1, slice B), through the real
app over the in-memory twins.

What a client observes: the order of checks and the one 404 across tenants
(SEC-3, T-2), idempotency by state (C-1, RAIL-18), cancel (RAIL-23), lazy
expiry and the fence (RAIL-27, AE-84), the cost guards and the ledger (X-5,
STATE-8, T-13), the model kept on the server (D-8, D-9, SEC-39, SEC-24), and
that no private text leaves (T-7, X-7). Who waits for whom under two
connections is `tests/test_tool_invocation_db.py`'s.

Run from the repo root:
    uv run python -m pytest service/tests/test_tool_invocations_api.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from httpx import Response
from langchain_core.messages import AIMessage
from pydantic import TypeAdapter, ValidationError

import config
from service import ratelimit, tool_invocations, tool_invocations_api, tracing, usage_capture, workbench_api
from service.app import app, get_auth_store, get_message_store, get_service, get_timeline_database, require_session
from service.auth_store import InMemoryAuthStore
from service.campaign_store import InMemoryCampaignStore, shared_rows
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase
from service.generate import generate_result
from service.history import InMemoryMessageStore
from service.model_catalog import CATALOG, DEFAULT_ALIAS, PUBLIC_MODELS
from service.models import ChatMode, ChatResponse
from service.participant_store import InMemoryParticipantStore
from service.providers import ProviderClientFactory
from service.session import SessionData
from service.timeline_store import InMemoryTimelineStore
from service.tool_invocation_store import InMemoryToolInvocationStore, InvocationNotStored
from service.tool_invocations import (
    ATTEMPT_TTL_S,
    ExecutionContext,
    InvocationCancelled,
    InvocationStores,
    InvocationTarget,
    ToolSettings,
)
from service.workbench_api import FORBIDDEN_ORIGIN_DETAIL, FORBIDDEN_ROLE_DETAIL, NOT_FOUND_DETAIL
from service.workbench_contracts import BRIEF_MAX_CHARS, ErrorCode, ToolId, ToolInvocation, validation_error_body
from service.workbench_load import InMemoryWorkbenchLoad

GM_A, GM_B = 1, 2
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=ATTEMPT_TTL_S)
INV = "inv_route_000000000001"
INV2 = "inv_route_000000000002"
INV3 = "inv_route_000000000003"
CANARY = "Zx9CanaryQ7"
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
UNAUTHENTICATED = {"detail": "not signed in"}
MISSING_CAMPAIGN = "cmp_" + "z" * 22
MODEL_WORDS = ("gpt-4o", "GPT-4o", "openai", "OpenAI", "deepseek", "DeepSeek", "qwen", "Qwen", "kimi", "Kimi")


def npc_result(title: str = "Mira", *, suggestions: list[dict[str, Any]] | None = None,
               tool: str = "npc", doc_type: str = "npc", category: str = "npcs") -> dict[str, Any]:
    return {
        "tool_id": tool, "result_kind": "document", "prose": "Here she is.", "suggestions": suggestions or [],
        "document": {"document_id": "doc_" + "m" * 22, "type": doc_type, "title": title,
                     "library_category": category},
    }


# ── The world ────────────────────────────────────────────────────────────────


class Recording:
    """An executor that records what it was asked, and does what a test says."""

    def __init__(self, tool_id: ToolId) -> None:
        self.tool_id = tool_id
        self.prechecks: list[InvocationTarget] = []
        self.runs: list[ExecutionContext] = []
        self.finishes = 0
        self.precheck_answer: ErrorCode | None = None
        self.behaviour: Callable[[ExecutionContext], Any] = lambda ctx: npc_result()

    def precheck(self, unit: Any, target: InvocationTarget) -> ErrorCode | None:
        self.prechecks.append(target)
        return self.precheck_answer

    def run(self, ctx: ExecutionContext) -> Any:
        self.runs.append(ctx)
        return self.behaviour(ctx)

    def finish(self, unit: Any, ctx: ExecutionContext, result: Any) -> Any:
        self.finishes += 1
        return result


class FakeLLM:
    """A provider client: records each call's timeout and config."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"config": config, **kwargs})
        return AIMessage(content="an answer", usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8})


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
    executors: dict[ToolId, Recording]
    llm: FakeLLM
    sink: Sink
    settings: ToolSettings = field(default_factory=lambda: ToolSettings(frozenset({ToolId.NPC, ToolId.RECAP})))
    now: list[datetime] = field(default_factory=lambda: [T0])

    def clock(self) -> datetime:
        return self.now[0]

    def table(self, owner: int = GM_A, *, campaigned: bool = True, campaign: str | None = None) -> Table:
        with self.db.transaction() as unit:
            if campaign is None:
                campaign = self.stores.campaigns.create(unit, owner_id=owner, name="Nocturne", now=self.now[0]).id
            conversation = self.stores.conversations.create(
                unit, owner_id=owner, campaign_id=campaign if campaigned else None, title=None, started_mode=None,
            )
        self.messages.claim_conversation(conversation.id, owner)
        return Table(owner, campaign, conversation.id)

    def rows(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        with self.db.transaction() as unit:
            return tuple(dict(shared_rows(self.db, name).visible(unit))  # type: ignore[return-value]
                         for name in ("tool_invocations", "tool_attempts", "timeline_entries"))

    def entries(self, table: Table) -> list[dict[str, Any]]:
        with self.db.transaction() as unit:
            found = self.stores.timeline.entry_window(unit, table.conversation, before=None, limit=50)
        return [cast(dict[str, Any], row.payload) for row in found]

    def attempts(self, table: Table, invocation_id: str = INV) -> list[Any]:
        with self.db.transaction() as unit:
            return self.stores.invocations.attempts(unit, table.owner, table.campaign, invocation_id)

    def archive(self, table: Table) -> None:
        with self.db.transaction() as unit:
            assert self.stores.campaigns.set_archived(unit, table.campaign, owner_id=table.owner, archived=True)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    db = InMemoryDatabase()
    messages = InMemoryMessageStore()
    stores = InvocationStores(
        InMemoryToolInvocationStore(db), InMemoryCampaignStore(db), InMemoryConversationStore(db),
        InMemoryTimelineStore(db, messages=messages), InMemoryWorkbenchLoad(db),
    )
    made = World(db, messages, stores, {tool: Recording(tool) for tool in ToolId}, FakeLLM(), Sink())
    factory = ProviderClientFactory(client_builders={DEFAULT_ALIAS: made.llm})
    overrides: dict[Callable[..., Any], Callable[..., Any]] = {
        tool_invocations_api.get_invocation_stores: lambda: made.stores,
        tool_invocations_api.get_tool_executors: lambda: made.executors,
        tool_invocations_api.get_tool_settings: lambda: made.settings,
        tool_invocations_api.get_clock: lambda: made.clock,
        tool_invocations_api.get_provider_factory: lambda: factory,
        get_timeline_database: lambda: made.db,
        get_message_store: lambda: made.messages,
    }
    app.dependency_overrides.update(overrides)
    monkeypatch.setattr(usage_capture, "_ledger_provider", lambda: made.sink)
    yield made
    for dependency in (*overrides, require_session, get_service, get_auth_store):
        app.dependency_overrides.pop(cast(Any, dependency), None)


@pytest.fixture
def client(world: World) -> TestClient:
    return TestClient(app)


def _as(user_id: int, role: str = "dm") -> None:
    def session(request: Request) -> SessionData:
        return SessionData(user_id=user_id, role=cast(Any, role))

    app.dependency_overrides[require_session] = session


def _signed_out() -> None:
    def refuse() -> SessionData:
        raise HTTPException(status_code=401, detail="authentication required")

    app.dependency_overrides[require_session] = refuse


def body(table: Table, *, invocation_id: str = INV, tool: str = "npc", brief: str = "A dwarf smith",
         source: str | None = None, conversation: str | None = None, campaign: str | None = None,
         **extra: Any) -> dict[str, Any]:
    made = {
        "schema_version": 1, "invocation_id": invocation_id, "tool_id": tool, "brief": brief,
        "campaign_id": campaign or table.campaign, "conversation_id": conversation or table.conversation,
        **extra,
    }
    if source is not None:
        made["source_entry_id"] = source
    return made


def path(campaign: str, invocation_id: str | None = None, rest: str = "") -> str:
    base = f"/campaigns/{campaign}/tool-invocations"
    return base if invocation_id is None else f"{base}/{invocation_id}{rest}"


def post(client: TestClient, table: Table, **kwargs: Any) -> Response:
    return client.post(path(table.campaign), json=body(table, **kwargs))


def status_of(client: TestClient, table: Table, invocation_id: str = INV) -> Response:
    return client.get(path(table.campaign, invocation_id))


def cancel_of(client: TestClient, table: Table, invocation_id: str = INV) -> Response:
    return client.post(path(table.campaign, invocation_id, "/cancel"))


def _shape(response: Response) -> tuple[Any, ...]:
    varying = {"date", "content-length", "server", "x-request-id"}
    return (response.status_code, response.content,
            tuple(sorted((k.lower(), v) for k, v in response.headers.items() if k.lower() not in varying)))


def _set(values: dict[str, Any]) -> dict[str, Any]:
    """An error object without its unset keys, which a 200's invocation
    carries as null (the client reads them `nullish`)."""
    return {key: value for key, value in values.items() if value is not None}


def _error(response: Response) -> dict[str, Any]:
    detail = response.json()["detail"]
    assert isinstance(detail, dict), response.text
    return detail


def _spent_throttle(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every token the tool path spends, counted by a spy that calls through."""
    spent: list[int] = []
    real = ratelimit.check_chat_request

    def spy(user_id: int) -> None:
        spent.append(user_id)
        real(user_id)

    monkeypatch.setattr(tool_invocations.ratelimit, "check_chat_request", spy)
    return spent


def _nothing_created(world: World) -> None:
    invocations, attempts, entries = world.rows()
    assert (invocations, attempts, entries) == ({}, {}, {})
    assert world.sink.rows == []


# ── A. Contract and validation ───────────────────────────────────────────────


def test_a1_every_tool_under_default_settings_is_disabled_before_any_work(world: World, client: TestClient,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """M-A1..M-A3: every executor is registered and recording, both switches at
    their defaults, so only the switches can refuse."""
    spent = _spent_throttle(monkeypatch)
    world.settings = ToolSettings()
    table = world.table()
    for tool in ToolId:
        response = post(client, table, tool=tool.value, brief="" if tool is ToolId.RECAP else "a brief")
        assert response.status_code == 409, (tool, response.text)
        assert _error(response)["code"] == "tool_disabled"
    assert all(not r.prechecks and not r.runs for r in world.executors.values())
    assert spent == []
    _nothing_created(world)


def test_a1_positive_control_a_capability_free_tool_and_an_image_tool_with_its_capability_run(
        world: World, client: TestClient) -> None:
    world.settings = ToolSettings(frozenset({ToolId.NPC, ToolId.PORTRAIT}), frozenset({"image_generation"}))
    world.executors[ToolId.PORTRAIT].behaviour = lambda ctx: {
        "tool_id": "portrait", "result_kind": "media", "prose": "Here.", "suggestions": [],
        "asset": {"asset_id": "ast_" + "p" * 22, "media_type": "image", "alt": "A portrait"},
    }
    table = world.table()
    assert post(client, table).json()["status"] == "done"
    assert post(client, table, invocation_id=INV2, tool="portrait").json()["status"] == "done"


def test_a1_an_image_tool_without_its_capability_answers_the_capabilitys_reason(
        world: World, client: TestClient) -> None:
    world.settings = ToolSettings(frozenset({ToolId.PORTRAIT}))
    response = post(client, world.table(), tool="portrait")
    assert response.status_code == 409
    assert _error(response) == {"code": "tool_disabled", "message": "Image generation isn't set up yet.",
                                "retryable": False}


@pytest.mark.parametrize("tool", [t.value for t in ToolId if t is not ToolId.RECAP])
@pytest.mark.parametrize("brief", ["", "   \t "])
def test_a2_a_brief_required_tool_refuses_an_empty_or_blank_brief(world: World, client: TestClient,
                                                                  tool: str, brief: str) -> None:
    response = post(client, world.table(), tool=tool, brief=brief)
    assert response.status_code == 422
    assert _error(response) == {"code": "brief_required", "message": "Describe what you want first.",
                                "retryable": False, "field": "brief"}
    _nothing_created(world)


def test_a2_recap_runs_with_an_empty_brief(world: World, client: TestClient) -> None:
    world.executors[ToolId.RECAP].behaviour = lambda ctx: npc_result(
        tool="recap", doc_type="session-notes", category="session-log")
    response = post(client, world.table(), tool="recap", brief="")
    assert response.status_code == 200, response.text
    assert world.executors[ToolId.RECAP].runs[0].target.brief == ""


def test_a3_a_brief_is_bounded_in_code_points(world: World, client: TestClient) -> None:
    table = world.table()
    astral = "\U0001f409" * BRIEF_MAX_CHARS
    assert post(client, table, brief=astral).status_code == 200
    response = post(client, table, invocation_id=INV2, brief=astral + "x")
    assert response.status_code == 422
    assert _error(response)["code"] == "brief_too_long"


def test_a4_an_escaped_emoji_brief_fits_and_a_body_over_the_bound_is_refused(world: World,
                                                                           client: TestClient) -> None:
    """M-A8: `conversations_api.read_body`'s 8 KiB would refuse the escaped brief."""
    table = world.table()
    escaped = json.dumps(body(table, brief="\U0001f409" * BRIEF_MAX_CHARS), ensure_ascii=True)
    assert len(escaped.encode()) > 8_192
    json_type = {"content-type": "application/json"}
    assert client.post(path(table.campaign), content=escaped, headers=json_type).status_code == 200
    cap = tool_invocations_api.TOOL_BODY_MAX_BYTES
    at_cap = json.dumps(body(table, invocation_id=INV2))
    at_cap += " " * (cap - len(at_cap.encode()))
    assert client.post(path(table.campaign), content=at_cap, headers=json_type).status_code == 200
    over = client.post(path(table.campaign), content=at_cap + " ", headers=json_type)
    assert over.status_code == 422
    assert _error(over) == {"code": "validation_failed", "message": "That request isn't valid.", "retryable": False}


def _streamed(chunks: list[bytes], pulled: list[int], headers: list[tuple[bytes, bytes]]) -> Request:
    async def receive() -> dict[str, Any]:
        index = len(pulled)
        pulled.append(index)
        return {"type": "http.request", "body": chunks[index], "more_body": index + 1 < len(chunks)}

    return Request({"type": "http", "method": "POST", "headers": headers}, receive)


def test_a4_the_reader_stops_at_the_chunk_that_crosses_the_bound_and_reads_no_further() -> None:
    reader = workbench_api.body_reader(10)
    pulled: list[int] = []
    chunks = [b"x" * 4, b"x" * 4, b"x" * 3, b"x" * 4]
    with pytest.raises(RequestValidationError) as refused:
        asyncio.run(reader(_streamed(chunks, pulled, [(b"transfer-encoding", b"chunked")])))
    assert pulled == [0, 1, 2], "read past the chunk that crossed the bound"
    assert validation_error_body(refused.value.errors()).model_dump(mode="json", exclude_none=True)["detail"] == {
        "code": "validation_failed", "message": "That request isn't valid.", "retryable": False}
    pulled.clear()
    with pytest.raises(RequestValidationError):
        asyncio.run(reader(_streamed([b"{}"], pulled, [(b"content-length", b"11")])))
    assert pulled == [], "a declared length over the bound reads nothing"
    assert asyncio.run(reader(_streamed([b"x" * 10], [], []))) == b"x" * 10
    with pytest.raises(ValueError):
        workbench_api.body_reader(0)


@pytest.mark.parametrize(("extra", "code"), [
    ({"tool_id": "npcs"}, "unknown_tool"),
    ({"context": {"secret": CANARY}}, "validation_failed"),
    ({"model_preference": CANARY}, "validation_failed"),
    ({"schema_version": 2}, "unsupported_schema_version"),
])
def test_a5_a6_an_unknown_tool_an_undeclared_key_and_a_later_version_are_refused(
        world: World, client: TestClient, caplog: pytest.LogCaptureFixture, extra: dict[str, Any], code: str) -> None:
    caplog.set_level(logging.DEBUG)
    table = world.table()
    response = client.post(path(table.campaign), json={**body(table), **extra})
    assert response.status_code == 422
    assert _error(response)["code"] == code
    for key in ("context", "model_preference", "secret", CANARY):
        if key in extra or key == CANARY:
            assert key not in response.text
            assert all(key not in record.getMessage() for record in caplog.records)
    _nothing_created(world)


def test_a8_a_body_naming_another_campaign_than_the_path_is_refused_by_field(world: World,
                                                                            client: TestClient) -> None:
    table = world.table()
    other = world.table()
    response = client.post(path(table.campaign), json=body(table, campaign=other.campaign))
    assert response.status_code == 422
    assert _error(response)["field"] == "campaign_id"


# ── B. Posture and ownership ─────────────────────────────────────────────────


@pytest.mark.real_auth
@pytest.mark.parametrize("payload", ["valid", "invalid", "malformed"])
def test_b1_without_a_session_every_route_answers_the_one_401(world: World, client: TestClient,
                                                             monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    app.dependency_overrides[get_auth_store] = InMemoryAuthStore
    table = world.table()
    content = {"valid": json.dumps(body(table)), "invalid": json.dumps({"tool_id": 3}), "malformed": "{nope"}[payload]
    headers = {"content-type": "application/json"}
    for response in (
        client.post(path(table.campaign), content=content, headers=headers),
        client.get(path(table.campaign, INV)),
        client.post(path(table.campaign, INV, "/cancel"), content=content, headers=headers),
    ):
        assert response.status_code == 401
        assert response.json() == UNAUTHENTICATED


@pytest.mark.parametrize("payload", ["valid", "invalid", "malformed"])
def test_b2_a_player_is_refused_before_the_body_is_read(world: World, client: TestClient, payload: str) -> None:
    """M-B2 and C-2: the role gate comes before any parse."""
    table = world.table()
    _as(GM_A, "player")
    content = {"valid": json.dumps(body(table)), "invalid": json.dumps({"tool_id": 3}), "malformed": "{nope"}[payload]
    headers = {"content-type": "application/json"}
    for response in (
        client.post(path(table.campaign), content=content, headers=headers),
        client.get(path(table.campaign, INV)),
        client.post(path(table.campaign, INV, "/cancel")),
    ):
        assert response.status_code == 403
        assert response.json() == {"detail": dict(FORBIDDEN_ROLE_DETAIL)}
    _nothing_created(world)


def test_c2_malformed_json_from_a_gm_is_the_workbench_422(world: World, client: TestClient) -> None:
    table = world.table()
    for content in ("{nope", "[]", ""):
        response = client.post(path(table.campaign), content=content, headers={"content-type": "application/json"})
        assert response.status_code == 422, content
        assert _error(response)["code"] == "validation_failed"


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"},
])
def test_b3_a_cross_site_create_or_cancel_is_refused_and_a_get_is_not_origin_checked(
        world: World, client: TestClient, headers: dict[str, str]) -> None:
    table = world.table()
    assert post(client, table).status_code == 200
    refused = [
        client.post(path(table.campaign), json=body(table, invocation_id=INV2), headers=headers),
        client.post(path(table.campaign, INV, "/cancel"), headers=headers),
    ]
    assert all(r.status_code == 403 and r.json() == {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)} for r in refused)
    assert client.get(path(table.campaign, INV), headers=headers).status_code == 200


def test_b3_a_form_body_is_refused(world: World, client: TestClient) -> None:
    table = world.table()
    response = client.post(path(table.campaign), content="a=b",
                           headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 403


def test_b5_every_foreign_or_missing_resource_is_the_same_404_on_every_route(world: World,
                                                                            client: TestClient) -> None:
    """T-2, M-B3..M-B5: byte-identical, and an archived or disabled foreign
    campaign is still the 404 — no guard runs before ownership."""
    mine, second, theirs = world.table(), world.table(), world.table(GM_B)
    loose = world.table(campaigned=False)
    assert post(client, mine).status_code == 200
    world.archive(theirs)
    cases = [
        (theirs.campaign, body(theirs)),
        (mine.campaign, body(mine, conversation=theirs.conversation)),
        (theirs.campaign, body(theirs, conversation=mine.conversation)),
        (mine.campaign, body(mine, conversation=second.conversation)),
        (mine.campaign, body(mine, conversation=loose.conversation)),
        (MISSING_CAMPAIGN, body(mine, campaign=MISSING_CAMPAIGN)),
        (mine.campaign, body(mine, conversation="cnv_" + "z" * 22)),
        ("not-a-campaign", body(mine, campaign="not-a-campaign")),
    ]
    world.settings = ToolSettings()
    answers = [_shape(client.post(path(campaign, None), json=payload)) for campaign, payload in cases]
    _as(GM_B)
    answers += [
        _shape(client.post(path(mine.campaign), json=body(mine, invocation_id=INV2))),
        _shape(client.get(path(mine.campaign, INV))),
        _shape(client.post(path(mine.campaign, INV, "/cancel"))),
    ]
    _as(GM_A)
    answers += [
        _shape(client.get(path(theirs.campaign, INV))), _shape(client.get(path(mine.campaign, INV2))),
        _shape(client.get(path(MISSING_CAMPAIGN, INV))), _shape(client.get(path("nope", INV))),
        _shape(client.get(path(mine.campaign, "short"))), _shape(client.post(path(mine.campaign, INV2, "/cancel"))),
    ]
    assert all(answer == answers[0] for answer in answers), answers
    assert answers[0][0] == 404 and json.loads(answers[0][1]) == NOT_FOUND


def test_b6_a_legacy_conversation_with_no_ownership_row_is_404_and_nothing_is_claimed(world: World,
                                                                                     client: TestClient) -> None:
    table = world.table()
    world.messages.append("legacy-thread-01", "gm", "user", "an old question")
    response = post(client, table, conversation="legacy-thread-01")
    assert response.status_code == 404
    assert world.messages.owner_of("legacy-thread-01") is None
    with world.db.transaction() as unit:
        assert "legacy-thread-01" not in shared_rows(world.db, "conversations").visible(unit)


def test_b7_a_source_entry_must_be_a_stored_entry_of_this_conversation(world: World, client: TestClient,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """M-B7: a bad source is refused by the ownership decision itself — before
    any guard runs, so no precheck is asked and no token is spent — and not
    only by the creating statement's own guard."""
    table, second = world.table(), world.table()
    assert post(client, table).status_code == 200
    source = world.entries(table)[0]["entry_id"]
    assert post(client, table, invocation_id=INV2, source=source).status_code == 200
    assert post(client, second).status_code == 200
    elsewhere = world.entries(second)[0]["entry_id"]
    prechecks = len(world.executors[ToolId.NPC].prechecks)
    spent = _spent_throttle(monkeypatch)
    for bad in (elsewhere, "12345", "ent_" + "q" * 22):
        assert post(client, table, invocation_id=INV3, source=bad).json() == NOT_FOUND
    assert len(world.executors[ToolId.NPC].prechecks) == prechecks and spent == []


def test_b8_an_archived_campaign_refuses_a_new_invocation_and_still_replays_a_done_one(
        world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    table = world.table()
    done = post(client, table)
    world.archive(table)
    spent = _spent_throttle(monkeypatch)
    refused = post(client, table, invocation_id=INV2)
    assert refused.status_code == 409
    assert _error(refused) == {"code": "campaign_archived", "message": "That campaign is archived.",
                               "retryable": False}
    replay = post(client, table)
    assert replay.status_code == 200 and replay.json() == done.json()
    assert spent == []


def test_b9_a_gm_seated_at_another_gms_table_is_a_stranger_there(world: World, client: TestClient) -> None:
    """§15.6: a seat never reaches a GM path; ownership is the campaign's owner only."""
    table = world.table()
    seats = InMemoryParticipantStore(world.db)
    with world.db.transaction() as unit:
        seat = seats.add(unit, table.campaign, alias="Bree", now=T0).id
        seats.offer(unit, table.campaign, seat, user_id=GM_B)
        seats.accept(unit, table.campaign, seat, user_id=GM_B, now=T0)
    assert post(client, table).status_code == 200
    _as(GM_B)
    assert post(client, table, invocation_id=INV2).json() == NOT_FOUND
    assert status_of(client, table).json() == NOT_FOUND
    assert cancel_of(client, table).json() == NOT_FOUND


@pytest.mark.real_auth
def test_c11_a_demoted_gm_loses_every_route_at_once(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    auth = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: auth
    token = auth.create_invite(role="dm", expires_at=datetime.now(UTC) + timedelta(days=1)).token
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "gm@example.com", "password": "password123", "invite": token})
    user = auth._users[0]  # noqa: SLF001 - the admin's view of the account
    table = world.table(user.id)
    assert post(client, table).status_code == 200
    user.role = "player"
    for response in (post(client, table, invocation_id=INV2), status_of(client, table), cancel_of(client, table)):
        assert response.status_code == 403
        assert response.json() == {"detail": dict(FORBIDDEN_ROLE_DETAIL)}


# ── C. Idempotency and lifecycle ─────────────────────────────────────────────


def test_c1_the_first_post_is_done_with_one_row_one_attempt_and_one_entry_that_agree(world: World,
                                                                                     client: TestClient) -> None:
    table = world.table()
    response = post(client, table)
    assert response.status_code == 200, response.text
    answer = response.json()
    ToolInvocation.model_validate(answer)
    assert (answer["status"], answer["attempt"], answer["cancel_requested"]) == ("done", 1, False)
    invocations, attempts, entries = world.rows()
    assert (len(invocations), len(attempts), len(entries)) == (1, 1, 1)
    [entry] = world.entries(table)
    assert entry["entry_kind"] == "tool" and entry["brief"] == "A dwarf smith"
    assert entry["invocation"] == answer
    assert status_of(client, table).json() == answer
    [attempt] = world.attempts(table)
    assert attempt.outcome == "done" and attempt.operation_id == world.executors[ToolId.NPC].runs[0].operation_id


def test_c2_a_repeat_of_done_is_free(world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-C2, M-C3: no guard, no throttle, no attempt, no ledger row."""
    world.executors[ToolId.NPC].behaviour = _one_provider_call
    table = world.table()
    first = post(client, table)
    rows_before, ledger_before = world.rows(), list(world.sink.rows)
    assert len(ledger_before) == 1, "the positive control: an admitted attempt writes a row"
    spent = _spent_throttle(monkeypatch)
    monkeypatch.setattr(config, "CHAT_DAILY_CAP", 1)
    world.settings = ToolSettings()
    again = post(client, table)
    assert again.status_code == 200 and again.content == first.content
    assert len(world.executors[ToolId.NPC].runs) == 1
    assert world.rows() == rows_before and world.sink.rows == ledger_before and spent == []


@pytest.mark.parametrize("change", ["brief", "tool", "conversation", "source"])
def test_c3_a_repeat_with_other_details_answers_what_a_same_body_repeat_answers(
        world: World, client: TestClient, change: str) -> None:
    """C-1: the body of a repeat is never compared; the stored row is untouched."""
    table = world.table()
    other = world.table(campaign=table.campaign)
    first = post(client, table)
    source = world.entries(table)[0]["entry_id"]
    rows_before = world.rows()
    kwargs = {"brief": {"brief": "Something else"}, "tool": {"tool": "loot"},
              "conversation": {"conversation": other.conversation}, "source": {"source": source}}[change]
    again = post(client, table, **kwargs)
    assert again.status_code == 200 and again.content == first.content
    assert world.rows() == rows_before


def test_c3_a_retry_runs_the_stored_brief_not_the_repeats(world: World, client: TestClient) -> None:
    table = world.table()
    world.executors[ToolId.NPC].behaviour = _raise(_timeout())
    assert post(client, table).json()["status"] == "failed"
    world.executors[ToolId.NPC].behaviour = lambda ctx: npc_result()
    assert post(client, table, brief="A different brief").json()["status"] == "done"
    assert [ctx.target.brief for ctx in world.executors[ToolId.NPC].runs] == ["A dwarf smith", "A dwarf smith"]


@pytest.mark.parametrize("race", ["archived", "gone"])
def test_c4_a_create_the_statement_refuses_rolls_its_entry_back(world: World, client: TestClient,
                                                                race: str) -> None:
    """C-4: the zero-row path. The campaign is archived between the guard's
    read and the creating statement (in the same unit here), or the statement
    refuses for a reason the re-read cannot name: 409 or the one 404, and the
    entry appended first is gone with the rest."""
    table = world.table()
    real = world.stores.invocations.create

    def racing(unit: Any, **kwargs: Any) -> Any:
        if race == "gone":
            raise InvocationNotStored()
        assert world.stores.campaigns.set_archived(unit, table.campaign, owner_id=table.owner, archived=True)
        return real(unit, **kwargs)

    world.stores.invocations.create = racing  # type: ignore[method-assign]
    response = post(client, table)
    expected = (409, "campaign_archived") if race == "archived" else (404, "not_found")
    assert (response.status_code, _error(response)["code"]) == expected
    _nothing_created(world)
    assert world.executors[ToolId.NPC].runs == []


def test_c4_one_id_from_two_gms_is_two_invocations(world: World, client: TestClient) -> None:
    mine, theirs = world.table(), world.table(GM_B)
    assert post(client, mine).status_code == 200
    _as(GM_B)
    assert post(client, theirs).status_code == 200
    assert status_of(client, mine).json() == NOT_FOUND
    assert status_of(client, theirs).json()["status"] == "done"
    invocations, _, _ = world.rows()
    assert len(invocations) == 2


def test_c5_a_repeat_while_working_answers_working_and_starts_nothing(world: World, client: TestClient) -> None:
    table = world.table()
    seen: list[Response] = []

    def repeat_inside(ctx: ExecutionContext) -> Any:
        seen.append(post(TestClient(app), table))
        return npc_result()

    world.executors[ToolId.NPC].behaviour = repeat_inside
    assert post(client, table).json()["status"] == "done"
    assert seen[0].status_code == 200 and seen[0].json()["status"] == "working"
    assert len(world.executors[ToolId.NPC].runs) == 1


def test_c6_a_timeout_then_a_repeat_passes_every_guard_again_and_keeps_one_entry(
        world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """AE-13, M-C5, M-C6."""
    spent = _spent_throttle(monkeypatch)
    table = world.table()
    world.executors[ToolId.NPC].behaviour = _raise(_timeout())
    failed = post(client, table).json()
    assert _set(failed["error"]) == {"code": "provider_timeout", "message": "The model took too long to answer.",
                               "retryable": True}
    world.executors[ToolId.NPC].behaviour = lambda ctx: npc_result()
    world.now[0] = T0 + timedelta(seconds=30)
    done = post(client, table).json()
    assert (done["status"], done["attempt"], done["error"]) == ("done", 2, None)
    assert spent == [GM_A, GM_A]
    assert len(world.entries(table)) == 1 and world.entries(table)[0]["invocation"] == done
    assert [a.outcome for a in world.attempts(table)] == ["failed", "done"]
    assert world.executors[ToolId.NPC].finishes == 1
    with world.db.transaction() as unit:
        assert world.stores.invocations.attempts_since(unit, T0) == 2


def test_c6b_while_a_retry_runs_its_entry_and_a_read_agree(world: World, client: TestClient) -> None:
    """I-17 (§5.6 "entry replaced"): admitting a retry rewrites the timeline
    entry in T1, so during attempt 2 a GET and the entry say the same thing."""
    table = world.table()
    world.executors[ToolId.NPC].behaviour = _raise(_timeout())
    assert post(client, table).json()["status"] == "failed"
    [failed_entry] = world.entries(table)
    assert (failed_entry["invocation"]["status"], failed_entry["invocation"]["attempt"]) == ("failed", 1)
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def look_while_working(ctx: ExecutionContext) -> Any:
        [entry] = world.entries(table)
        seen.append((status_of(TestClient(app), table).json(), entry["invocation"]))
        return npc_result()

    world.executors[ToolId.NPC].behaviour = look_while_working
    world.now[0] = T0 + timedelta(seconds=30)
    assert post(client, table).json()["status"] == "done"
    [(read, entry)] = seen
    assert (read["status"], read["attempt"], read["error"]) == ("working", 2, None)
    assert entry == read


def test_c7_a_final_failure_is_answered_unchanged(world: World, client: TestClient) -> None:
    """M-C7, C-9: content_filter is final."""
    import openai
    table = world.table()
    world.executors[ToolId.NPC].behaviour = _raise(openai.ContentFilterFinishReasonError())
    failed = post(client, table)
    assert _set(failed.json()["error"]) == {
        "code": "provider_failed", "message": "The assistant couldn't do that one. Edit the brief and try again.",
        "retryable": False}
    assert post(client, table).content == failed.content
    assert len(world.executors[ToolId.NPC].runs) == 1


def test_c8_a_cancelled_invocation_is_answered_and_nothing_runs(world: World, client: TestClient) -> None:
    table = world.table()
    world.executors[ToolId.NPC].behaviour = _raise(InvocationCancelled())
    cancelled = post(client, table)
    assert cancelled.json()["status"] == "cancelled"
    assert post(client, table).content == cancelled.content
    assert len(world.executors[ToolId.NPC].runs) == 1


def test_c9_the_hundredth_attempts_failure_is_final(world: World, client: TestClient,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "check_chat_request", lambda user_id: None)
    table = world.table()
    world.executors[ToolId.NPC].behaviour = _raise(_timeout())
    for attempt in range(1, 101):
        world.now[0] = T0 + timedelta(seconds=attempt)
        answer = post(client, table).json()
        assert answer["attempt"] == attempt
    assert answer["error"]["retryable"] is False
    assert post(client, table).json() == answer
    assert len(world.executors[ToolId.NPC].runs) == 100


# ── D. Cancel ────────────────────────────────────────────────────────────────


def _cancel_now(world: World, table: Table) -> ToolInvocation:
    """What the cancel route runs, from inside a running attempt."""
    return tool_invocations.cancel(world.db, world.stores, table.owner, table.campaign, INV, now=world.clock())


def test_d1_a_cancel_seen_by_the_executor_ends_cancelled_without_finish(world: World, client: TestClient) -> None:
    """M-D1."""
    table = world.table()

    def cancelled_midway(ctx: ExecutionContext) -> Any:
        assert _cancel_now(world, table).status.value == "working"
        ctx.check_cancelled()
        return npc_result()

    world.executors[ToolId.NPC].behaviour = cancelled_midway
    answer = post(client, table).json()
    assert (answer["status"], answer["cancel_requested"]) == ("cancelled", True)
    assert world.executors[ToolId.NPC].finishes == 0
    with world.db.transaction() as unit:
        assert world.stores.invocations.in_flight_ids(unit, GM_A, now=world.clock()) == []


def test_d2_d3_a_result_after_a_cancel_is_kept_and_either_order_ends_the_same(world: World,
                                                                            client: TestClient) -> None:
    """AE-55, M-D2..M-D4."""
    before, after = world.table(), world.table()

    def cancelled_but_finished(ctx: ExecutionContext) -> Any:
        _cancel_now(world, before)
        return npc_result()

    world.executors[ToolId.NPC].behaviour = cancelled_but_finished
    early = post(client, before).json()
    world.executors[ToolId.NPC].behaviour = lambda ctx: npc_result()
    post(client, after)
    late = cancel_of(client, after).json()
    for answer in (early, late):
        assert (answer["status"], answer["cancel_requested"]) == ("done", True)
        assert answer["result"] == npc_result()
    assert world.entries(before)[0]["invocation"] == early
    assert world.entries(after)[0]["invocation"] == late


def test_d4_cancel_on_failed_or_cancelled_changes_nothing_and_an_unknown_id_is_404(world: World,
                                                                                  client: TestClient) -> None:
    failed, cancelled = world.table(), world.table()
    world.executors[ToolId.NPC].behaviour = _raise(_timeout())
    first = post(client, failed)
    world.executors[ToolId.NPC].behaviour = _raise(InvocationCancelled())
    second = post(client, cancelled)
    rows = world.rows()
    assert cancel_of(client, failed).content == first.content
    assert cancel_of(client, cancelled).content == second.content
    assert world.rows() == rows
    assert cancel_of(client, failed, INV2).json() == NOT_FOUND


def test_d5_d6_a_cancel_requested_attempt_keeps_its_slot_and_a_failure_after_it_is_cancelled(
        world: World, client: TestClient) -> None:
    """M-D5, D6, F1's in_flight list."""
    table = world.table()
    order: list[Response] = []

    def hold_two_then_try_a_third(ctx: ExecutionContext) -> Any:
        if ctx.target.brief == "first":
            _cancel_now(world, table)
            order.append(post(TestClient(app), table, invocation_id=INV2, brief="second"))
            raise _timeout()
        order.append(post(TestClient(app), table, invocation_id=INV3, brief="third"))
        return npc_result()

    world.executors[ToolId.NPC].behaviour = hold_two_then_try_a_third
    first = post(client, table, brief="first").json()
    assert (first["status"], first["error"]) == ("cancelled", None)
    third = order[0]
    assert third.status_code == 409
    assert _error(third) == {"code": "cap_reached", "message": "Two tools are already running.",
                             "retryable": True, "in_flight": [INV, INV2]}


def test_d7_a_cancel_past_the_deadline_ends_the_attempt_expired_and_flags_nothing(world: World,
                                                                                  client: TestClient) -> None:
    """I-13 names cancel: a working row past its deadline is expired first, so
    the cancel answers `failed attempt_expired`, still retryable, and never
    flags a dead attempt into a final `cancelled`."""
    table = world.table()
    _admit(world, table)
    world.now[0] = T0 + TTL
    answer = cancel_of(client, table).json()
    assert (answer["status"], answer["cancel_requested"], _set(answer["error"])) == ("failed", False, {
        "code": "attempt_expired", "message": "That took too long and was stopped. Try again.", "retryable": True})
    assert status_of(client, table).json() == answer
    assert world.entries(table)[0]["invocation"] == answer
    assert [a.outcome for a in world.attempts(table)] == ["expired"]


def test_d8_a_cancel_is_never_throttled_and_never_counted(world: World, client: TestClient,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """I-15: with the GM's window spent, a cancel of a working invocation, its
    repeat and a cancel of a done one still answer 200 and spend no token of
    the window /chat shares (I-10), add no attempt and write no ledger row."""
    table = world.table()
    _admit(world, table)
    world.executors[ToolId.NPC].behaviour = _one_provider_call
    assert post(client, table, invocation_id=INV2).json()["status"] == "done"
    _spend_the_window(GM_A)
    spent = _spent_throttle(monkeypatch)
    assert post(client, table, invocation_id=INV3).status_code == 429
    assert spent == [GM_A], "the positive control: the spy sees a spend and the window is shut"
    rows_before, ledger_before = world.rows(), list(world.sink.rows)
    assert len(ledger_before) == 1, "the positive control: INV2's attempt wrote its row"
    answers = [cancel_of(client, table), cancel_of(client, table), cancel_of(client, table, INV2)]
    assert [r.status_code for r in answers] == [200, 200, 200]
    assert [(r.json()["status"], r.json()["cancel_requested"]) for r in answers] == [
        ("working", True), ("working", True), ("done", True)]
    assert spent == [GM_A]
    assert world.sink.rows == ledger_before
    _, attempts_before, _ = rows_before
    _, attempts_after, _ = world.rows()
    assert attempts_after == attempts_before


# ── E. Expiry and fencing ────────────────────────────────────────────────────


def _admit(world: World, table: Table, invocation_id: str = INV) -> tool_invocations.Admission:
    request = tool_invocations.ToolInvocationRequest.model_validate(body(table, invocation_id=invocation_id))
    admitted = tool_invocations.submit(
        world.db, world.stores, world.executors, world.settings, SessionData(user_id=table.owner, role="dm"),
        request, now=world.clock(), chat_turns_today=0,
    )
    assert isinstance(admitted, tool_invocations.Admission)
    return admitted


def _ctx(world: World, admitted: tool_invocations.Admission) -> ExecutionContext:
    return ExecutionContext(
        admitted, clock=world.clock, factory=ProviderClientFactory(client_builders={DEFAULT_ALIAS: world.llm}),
        probe=tool_invocations.cancellation_probe(world.db, world.stores, admitted, world.clock),
    )


def test_e1_e2_past_its_deadline_a_read_ends_the_attempt(world: World, client: TestClient) -> None:
    """M-E1."""
    plain, asked = world.table(), world.table()
    _admit(world, plain)
    _admit(world, asked)
    tool_invocations.cancel(world.db, world.stores, GM_A, asked.campaign, INV, now=world.clock())
    world.now[0] = T0 + TTL
    expired = status_of(client, plain).json()
    assert (expired["status"], _set(expired["error"])) == ("failed", {
        "code": "attempt_expired", "message": "That took too long and was stopped. Try again.", "retryable": True})
    assert world.entries(plain)[0]["invocation"] == expired
    assert [a.outcome for a in world.attempts(plain)] == ["expired"]
    assert status_of(client, asked).json()["status"] == "cancelled"
    assert [a.outcome for a in world.attempts(asked)] == ["expired"]


def test_e1b_the_hundredth_attempts_expiry_is_final(world: World, client: TestClient) -> None:
    """I-6: an expiry is retryable below the last attempt only, so the 100th
    attempt's expiry is stored `retryable: false` and a repeat replays it."""
    table = world.table()
    _admit(world, table)
    failed = {"code": "provider_timeout", "message": "The model took too long to answer.", "retryable": True}
    with world.db.transaction() as unit:
        for attempt in range(1, 100):
            moment = T0 + timedelta(seconds=attempt)
            world.stores.invocations.settle(unit, GM_A, table.campaign, INV, attempt=attempt, status="failed",
                                            outcome="failed", result=None, error=failed, now=moment)
            world.stores.invocations.start_retry(unit, owner_id=GM_A, campaign_id=table.campaign,
                                                 invocation_id=INV, attempt=attempt, operation_id=f"{attempt:032x}",
                                                 attempt_deadline=moment + TTL, now=moment)
    world.now[0] = T0 + timedelta(seconds=99) + TTL
    expired = status_of(client, table).json()
    assert (expired["status"], expired["attempt"], _set(expired["error"])) == ("failed", 100, {
        "code": "attempt_expired", "message": "That took too long and was stopped. Try again.", "retryable": False})
    assert world.entries(table)[0]["invocation"] == expired
    assert post(client, table).json() == expired
    assert world.executors[ToolId.NPC].runs == [] and len(world.attempts(table)) == 100


def test_e3_a_completion_after_its_deadline_is_discarded_and_the_post_answers_the_expiry(
        world: World, client: TestClient) -> None:
    """M-E2."""
    table = world.table()

    def too_slow(ctx: ExecutionContext) -> Any:
        world.now[0] = T0 + TTL + timedelta(seconds=1)
        return npc_result()

    world.executors[ToolId.NPC].behaviour = too_slow
    answer = post(client, table).json()
    assert (answer["status"], answer["error"]["code"], answer["result"]) == ("failed", "attempt_expired", None)
    assert world.executors[ToolId.NPC].finishes == 0


def _result(title: str) -> tool_invocations.Outcome:
    return tool_invocations.Outcome(result=tool_invocations.judge_result(npc_result(title), ToolId.NPC,
                                                                         lambda tool: True))


def test_e4_a_late_completion_of_a_superseded_attempt_is_fenced_out(world: World, client: TestClient) -> None:
    """AE-84, M-E3: attempt 1 expires, attempt 2 is admitted and still
    working when attempt 1's result arrives — it is discarded, and exactly one
    finish runs, attempt 2's."""
    table = world.table()
    first = _admit(world, table)
    world.now[0] = T0 + TTL + timedelta(seconds=1)
    second = _admit(world, table)
    assert second.attempt == 2
    rows = world.rows()
    executor = world.executors[ToolId.NPC]
    late = tool_invocations.complete(world.db, world.stores, executor, first, _result("Late"), _ctx(world, first),
                                     now=world.clock())
    assert (late.status.value, late.attempt, late.result) == ("working", 2, None)
    assert executor.finishes == 0 and world.rows() == rows
    done = tool_invocations.complete(world.db, world.stores, executor, second, _result("Mira"),
                                     _ctx(world, second), now=world.clock())
    assert (done.status.value, done.attempt) == ("done", 2)
    assert done.model_dump(mode="json")["result"]["document"]["title"] == "Mira"
    assert executor.finishes == 1
    assert status_of(client, table).json() == done.model_dump(mode="json")


def test_e4b_a_late_failure_of_a_superseded_attempt_touches_nothing(world: World, client: TestClient) -> None:
    table = world.table()
    first = _admit(world, table)
    world.now[0] = T0 + TTL + timedelta(seconds=1)
    second = post(client, table).json()
    rows = world.rows()
    tool_invocations.complete(
        world.db, world.stores, world.executors[ToolId.NPC], first,
        tool_invocations.Outcome(error=tool_invocations.failure_for(_timeout())), _ctx(world, first),
        now=world.clock(),
    )
    assert world.rows() == rows and status_of(client, table).json() == second


def test_e4c_check_cancelled_stops_an_expired_or_a_superseded_attempt(world: World) -> None:
    """C-7: `check_cancelled` raises once the row is no longer this attempt's
    `working` before its deadline, with no cancel asked for — an expired or a
    superseded attempt stops spending."""
    table = world.table()
    first = _admit(world, table)
    _ctx(world, first).check_cancelled()
    world.now[0] = T0 + TTL
    with pytest.raises(InvocationCancelled):
        _ctx(world, first).check_cancelled()
    world.now[0] = T0 + TTL + timedelta(seconds=1)
    second = _admit(world, table)
    assert (second.attempt, world.attempts(table)[0].outcome) == (2, "expired")
    _ctx(world, second).check_cancelled()
    within_its_own_deadline = ExecutionContext(
        first, clock=lambda: T0, factory=ProviderClientFactory(client_builders={DEFAULT_ALIAS: world.llm}),
        probe=tool_invocations.cancellation_probe(world.db, world.stores, first, lambda: T0),
    )
    with pytest.raises(InvocationCancelled):
        within_its_own_deadline.check_cancelled()
    current = tool_invocations.read(world.db, world.stores, GM_A, table.campaign, INV, now=world.clock())
    assert (current.status.value, current.attempt, current.cancel_requested) == ("working", 2, False)


def test_e5_a_past_deadline_row_nobody_read_does_not_hold_the_cap(world: World, client: TestClient) -> None:
    """M-E4."""
    table = world.table()
    _admit(world, table, INV)
    _admit(world, table, INV2)
    assert _error(post(client, table, invocation_id=INV3))["code"] == "cap_reached"
    world.now[0] = T0 + TTL
    assert post(client, table, invocation_id=INV3).json()["status"] == "done"


# ── F. Cost guards and the ledger ────────────────────────────────────────────


def test_f1_f2_the_cap_is_per_gm_across_campaigns_and_chat_is_not_counted(world: World, client: TestClient) -> None:
    """M-F1, M-F2."""
    app.dependency_overrides[get_service] = lambda: _ChatService()
    one, two = world.table(), world.table()
    _admit(world, one, INV)
    _admit(world, two, INV2)
    refused = post(client, one, invocation_id=INV3)
    assert _error(refused)["in_flight"] == [INV, INV2]
    assert client.post("/chat", json={"prompt": "what does fireball do"}).status_code == 200
    _as(GM_B)
    assert post(client, world.table(GM_B), invocation_id=INV3).json()["status"] == "done"


def test_f3_the_hourly_window_is_chats_own_and_chats_refusal_is_unchanged(world: World, client: TestClient,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """M-F3, M-F4 (I-10: one window per GM)."""
    app.dependency_overrides[get_service] = lambda: _ChatService()
    table = world.table()
    _spend_the_window(GM_A)
    refused = post(client, table)
    assert refused.status_code == 429
    info = _error(refused)
    assert (info["code"], info["retryable"], info["message"]) == (
        "throttled_user", True, "That's a lot at once. Try again shortly.")
    assert int(refused.headers["retry-after"]) == info["retry_after_s"] > 0
    _nothing_created(world)
    chat = client.post("/chat", json={"prompt": "one more"})
    assert chat.status_code == 429 and chat.headers["x-chat-throttled"] == "user"
    assert chat.json() == {"detail": "You're asking faster than the tavern can pour. Try again shortly."}


def test_f4_the_pilot_day_counts_chat_turns_and_every_gms_tool_attempts(world: World, client: TestClient,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """M-F5, M-F6, C-5."""
    app.dependency_overrides[get_service] = lambda: _ChatService()
    monkeypatch.setattr(config, "CHAT_DAILY_CAP", 3)
    mine, theirs = world.table(), world.table(GM_B)
    world.now[0] = datetime.now(UTC)
    _as(GM_B)
    assert post(client, theirs, invocation_id=INV).status_code == 200
    assert post(client, theirs, invocation_id=INV2).status_code == 200
    _as(GM_A)
    assert client.post("/chat", json={"prompt": "a question"}).status_code == 200, "tools alone never close chat"
    refused = post(client, mine, invocation_id=INV3)
    assert refused.status_code == 429
    assert _error(refused) == {"code": "throttled_daily", "retryable": False,
                               "message": "The pilot's daily limit is spent. It resets overnight."}
    assert "retry-after" not in refused.headers


def test_f5_a_request_refused_by_any_guard_creates_nothing_and_spends_no_token(
        world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-F7: the window is the last guard."""
    spent = _spent_throttle(monkeypatch)
    table = world.table()
    world.executors[ToolId.NPC].precheck_answer = ErrorCode.NOTHING_TO_RECAP
    assert _error(post(client, table))["code"] == "nothing_to_recap"
    world.executors[ToolId.NPC].precheck_answer = None
    monkeypatch.setattr(config, "CHAT_DAILY_CAP", 0)
    assert _error(post(client, table))["code"] == "throttled_daily"
    assert spent == []
    _nothing_created(world)


def _one_provider_call(ctx: ExecutionContext) -> Any:
    generate_result(
        ["a prompt"], alias=ctx.model_alias, client=ctx.client(), config=ctx.run_config(),
        observer=usage_capture.observer_for(ctx.run_config(), purpose=usage_capture.PURPOSE_ANSWER,
                                            alias=ctx.model_alias),
    )
    return npc_result()


def test_f6_each_provider_attempt_is_a_ledger_row_of_the_tool_operation(world: World, client: TestClient) -> None:
    """M-F8, M-F9."""
    world.executors[ToolId.NPC].behaviour = _one_provider_call
    table = world.table()
    assert post(client, table).json()["status"] == "done"
    [row] = world.sink.rows
    [attempt] = world.attempts(table)
    assert (row.operation, row.mode, row.billed_account_id, row.campaign_id, row.operation_id, row.actor_kind) == (
        "tool_invocation", "gm", GM_A, table.campaign, attempt.operation_id, "account")
    assert world.llm.calls[0]["timeout"] == tool_invocations.PROVIDER_CALL_MAX_S


def test_f6_a_chat_turn_still_records_a_chat_turn_under_a_fresh_id(world: World) -> None:
    token = usage_capture.begin_operation(mode="sage", billed_account_id=GM_A)
    try:
        operation = usage_capture.current_operation()
        assert operation is not None and operation.operation == "chat_turn"
        assert len(operation.operation_id) == 32
    finally:
        usage_capture.end_operation(token)


@pytest.mark.parametrize("fault", ["calls_today", "database", "lock_timeout", "no_db", "no_messages"])
def test_f7_an_outage_before_any_attempt_is_a_503_and_creates_nothing(world: World, client: TestClient,
                                                                     fault: str) -> None:
    """M-F10: the day count fails closed."""
    table = world.table()
    if fault == "calls_today":
        world.messages.calls_today = _raise_now(RuntimeError())  # type: ignore[method-assign]
    elif fault in {"database", "lock_timeout"}:
        error = psycopg.OperationalError() if fault == "database" else psycopg.errors.LockNotAvailable()
        world.stores.invocations.hold_in_flight_lock = _raise_now(error)  # type: ignore[method-assign]
    elif fault == "no_db":
        app.dependency_overrides[get_timeline_database] = lambda: None
    else:
        app.dependency_overrides[get_message_store] = lambda: None
    response = post(client, table)
    assert response.status_code == 503
    assert _error(response) == {"code": "backend_unavailable", "retryable": True,
                                "message": "GM tools are briefly unavailable. Try again."}
    _nothing_created(world)


@pytest.mark.parametrize("route", ["status", "cancel"])
@pytest.mark.parametrize("fault", ["database", "lock_timeout"])
def test_c11_an_outage_on_a_read_or_a_cancel_is_a_503_and_changes_nothing(world: World, client: TestClient,
                                                                         route: str, fault: str) -> None:
    table = world.table()
    post(client, table)
    rows = world.rows()
    error = psycopg.OperationalError() if fault == "database" else psycopg.errors.LockNotAvailable()
    world.stores.invocations.get_for_update = _raise_now(error)  # type: ignore[method-assign]
    response = status_of(client, table) if route == "status" else cancel_of(client, table)
    assert response.status_code == 503 and _error(response)["retryable"] is True
    assert world.rows() == rows


def test_c11_a_failed_completion_is_a_503_the_row_stays_working_and_the_ledger_is_flushed(
        world: World, client: TestClient) -> None:
    world.executors[ToolId.NPC].behaviour = _one_provider_call
    table = world.table()
    real_fence = world.stores.invocations.fence
    world.stores.invocations.fence = _raise_now(psycopg.OperationalError())  # type: ignore[method-assign]
    response = post(client, table)
    assert response.status_code == 503
    assert len(world.sink.rows) == 1
    world.stores.invocations.fence = real_fence  # type: ignore[method-assign]
    assert status_of(client, table).json()["status"] == "working"
    world.now[0] = T0 + TTL
    assert status_of(client, table).json()["error"]["code"] == "attempt_expired"
    assert post(client, table).json()["attempt"] == 2


# ── G. Model and provider ────────────────────────────────────────────────────


def test_g1_the_model_is_the_servers_and_never_leaves(world: World, client: TestClient,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    """M-G1: whatever the request carries, the default alias runs, and no
    answer, entry or log line of these routes names a model or a provider."""
    caplog.set_level(logging.DEBUG)
    table = world.table()
    world.executors[ToolId.NPC].behaviour = _one_provider_call
    answers = [post(client, table), post(client, table, invocation_id=INV2, model_preference="kimi-k3")]
    world.executors[ToolId.NPC].behaviour = _raise(_timeout())
    answers.append(post(client, table, invocation_id=INV3))
    assert world.executors[ToolId.NPC].runs[0].model_alias == DEFAULT_ALIAS
    words = {*MODEL_WORDS, *CATALOG, *(p.display_name for p in CATALOG.values()), *(p.label for p in
             PUBLIC_MODELS.values()), "traveller"}
    text = " ".join(r.text for r in answers) + json.dumps(world.entries(table))
    assert not [w for w in words if w in text]
    pinned = usage_capture.EXPECTED_KEYS | {"severity", "message"}
    logged = []
    for record in caplog.records:
        message = record.getMessage()
        if record.name == usage_capture.__name__ and message.startswith("usage capture record: "):
            fields = json.loads(message.removeprefix("usage capture record: "))
            assert fields["event"] == usage_capture.EVENT and usage_capture.EXPECTED_KEYS <= set(fields) <= pinned
            assert CANARY not in message
            continue
        logged.append(message)
    assert not [w for message in logged for w in words if w in message]


def test_g2_an_alias_off_the_allowlist_is_refused_at_admission(world: World, client: TestClient,
                                                               monkeypatch: pytest.MonkeyPatch,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    """M-G3; I-19 and C-10(d): the refusal's log line names the tool, never
    the model or its provider."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setitem(CATALOG, "deepseek-v4-flash",
                        CATALOG["deepseek-v4-flash"].__class__(**{**CATALOG["deepseek-v4-flash"].__dict__,
                                                                  "enabled": True}))
    monkeypatch.setattr(tool_invocations, "resolve_tool_model", lambda session: "deepseek-v4-flash")
    response = post(client, world.table())
    assert response.status_code == 409 and _error(response)["code"] == "tool_disabled"
    assert "deepseek" not in response.text.lower()
    _nothing_created(world)
    logged = [record.getMessage() for record in caplog.records]
    assert [m for m in logged if m.startswith("tool refused: its model is off the Workbench allowlist")]
    words = {*MODEL_WORDS, *CATALOG, *(p.display_name for p in CATALOG.values()), *(p.label for p in
             PUBLIC_MODELS.values()), "traveller"}
    assert not [w for message in logged for w in words if w in message]


def test_g3_no_trace_callback_or_metadata_rides_on_a_tool_call(world: World, client: TestClient,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """M-G4, C-10(c)."""
    built: list[Any] = []
    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        built.append(1)
        return {"callbacks": ["spy"]}

    monkeypatch.setattr(tracing, "build_trace_config", spy)
    monkeypatch.setenv("RAG_TRACING", "1")
    world.executors[ToolId.NPC].behaviour = _one_provider_call
    assert post(client, world.table()).json()["status"] == "done"
    assert built == []
    sent = world.llm.calls[0]["config"]
    assert "callbacks" not in sent and "metadata" not in sent
    assert set(sent) == {"configurable"}


# ── H. Results ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("produced", [
    npc_result(tool="encounter", doc_type="encounter", category="documents"),
    {"tool_id": "npc", "result_kind": "card", "prose": "x", "suggestions": [], "card": {"card_kind": "stat_block"}},
    {"tool_id": "npc", "result_kind": "document", "prose": CANARY, "suggestions": []},
])
def test_h1_a_result_that_does_not_validate_fails_the_attempt_retryably(
        world: World, client: TestClient, caplog: pytest.LogCaptureFixture, produced: dict[str, Any]) -> None:
    """M-H1."""
    caplog.set_level(logging.DEBUG)
    world.executors[ToolId.NPC].behaviour = lambda ctx: produced
    answer = post(client, world.table()).json()
    assert (answer["status"], answer["error"]["code"], answer["error"]["retryable"]) == (
        "failed", "provider_failed", True)
    assert world.executors[ToolId.NPC].finishes == 0
    assert all(CANARY not in record.getMessage() for record in caplog.records)


def test_h1_a_monster_that_answers_a_document_fails(world: World, client: TestClient) -> None:
    world.settings = ToolSettings(frozenset({ToolId.MONSTER}))
    world.executors[ToolId.MONSTER].behaviour = lambda ctx: npc_result(tool="monster")
    assert post(client, world.table(), tool="monster").json()["error"]["code"] == "provider_failed"


def test_h2_suggestions_of_a_tool_that_may_not_run_are_dropped(world: World, client: TestClient) -> None:
    world.executors[ToolId.NPC].behaviour = lambda ctx: npc_result(suggestions=[
        {"tool_id": "loot", "label": "Loot her"}, {"tool_id": "npc", "label": "Her sister"},
        {"tool_id": "nonsense", "label": "Nope"}])
    answer = post(client, world.table()).json()
    assert answer["result"]["suggestions"] == [{"tool_id": "npc", "label": "Her sister", "icon": None,
                                                "brief": None}]


@pytest.mark.parametrize("route", ["status", "cancel", "repeat"])
def test_h3_a_row_this_build_cannot_read_is_a_503_and_is_never_overwritten(world: World, client: TestClient,
                                                                          route: str) -> None:
    table = world.table()
    world.executors[ToolId.NPC].behaviour = _raise(_timeout())
    post(client, table)
    rows: Any = shared_rows(world.db, "tool_invocations")
    with world.db.transaction() as unit:
        [(key, stored)] = rows.visible(unit).items()
        rows.replace(unit, key, replace(stored, row=replace(stored.row, schema_version=2)))
    before = world.rows()
    routes: dict[str, Callable[[TestClient, Table], Response]] = {
        "status": status_of, "cancel": cancel_of, "repeat": post}
    response = routes[route](client, table)
    assert response.status_code == 503
    assert world.rows() == before


# ── I. Production defaults ───────────────────────────────────────────────────


def test_i2_the_real_executors_and_settings_disable_every_tool(world: World, client: TestClient) -> None:
    for dependency in (tool_invocations_api.get_tool_executors, tool_invocations_api.get_tool_settings):
        app.dependency_overrides.pop(dependency)
    assert set(tool_invocations_api.get_tool_executors()) == {ToolId.NPC, ToolId.ENCOUNTER}
    table = world.table()
    for tool in ToolId:
        response = post(client, table, tool=tool.value, brief="" if tool is ToolId.RECAP else "a brief")
        assert (response.status_code, _error(response)["code"]) == (409, "tool_disabled")
    _nothing_created(world)


def test_a7_the_brief_never_leaves_through_any_answer_or_log(world: World, client: TestClient,
                                                             caplog: pytest.LogCaptureFixture,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """M-A10, M-A11: a canary brief through every refusal and every outcome."""
    caplog.set_level(logging.DEBUG)
    app.dependency_overrides[get_service] = lambda: _ChatService()
    table, other = world.table(), world.table(GM_B)
    answers: list[Response] = [
        post(client, table, brief=CANARY + "\U0001f409" * BRIEF_MAX_CHARS),               # 422
        post(client, other, brief=CANARY),                                              # 404
        post(client, table, tool="loot", brief=CANARY),                                 # 409 tool_disabled
    ]
    world.executors[ToolId.NPC].precheck_answer = ErrorCode.NOTHING_TO_RECAP
    answers.append(post(client, table, brief=CANARY))                                   # 409 precheck
    world.executors[ToolId.NPC].precheck_answer = None
    world.executors[ToolId.NPC].behaviour = _raise(_timeout())
    answers.append(post(client, table, invocation_id=INV, brief=CANARY))                # failed
    world.executors[ToolId.NPC].behaviour = _raise(InvocationCancelled())
    answers.append(post(client, table, invocation_id=INV2, brief=CANARY))               # cancelled
    world.executors[ToolId.NPC].behaviour = lambda ctx: npc_result()
    _admit(world, table, INV3)
    answers.append(post(client, table, invocation_id="inv_route_000000000004", brief=CANARY))  # 409 cap
    world.now[0] = T0 + TTL
    answers.append(post(client, table, invocation_id="inv_route_000000000005", brief=CANARY))  # done
    monkeypatch.setattr(config, "CHAT_DAILY_CAP", 0)
    answers.append(post(client, table, invocation_id="inv_route_000000000006", brief=CANARY))  # 429 daily
    monkeypatch.setattr(config, "CHAT_DAILY_CAP", 500)
    _spend_the_window(GM_A)
    answers.append(post(client, table, invocation_id="inv_route_000000000007", brief=CANARY))  # 429 user
    world.archive(table)
    answers.append(post(client, table, invocation_id="inv_route_000000000008", brief=CANARY))  # 409 archived
    world.messages.calls_today = _raise_now(RuntimeError(CANARY))  # type: ignore[method-assign]
    answers.append(post(client, table, invocation_id="inv_route_000000000009", brief=CANARY))  # 503
    assert sorted({r.status_code for r in answers}) == [200, 404, 409, 422, 429, 503]
    for response in answers:
        assert CANARY not in response.text
        assert all(CANARY not in value for value in response.headers.values())
    for record in caplog.records:
        assert CANARY not in record.getMessage()
        assert all(CANARY not in str(arg) for arg in (record.args or ()))


@pytest.mark.parametrize(("where", "raised"), [
    ("run", lambda: ValueError(f"could not parse {CANARY}")),
    ("run", lambda: _canary_validation_error()),
    ("finish", lambda: ValueError(f"document title {CANARY} refused")),
    ("finish", lambda: _canary_validation_error()),
], ids=["run-value-error", "run-validation-error", "finish-value-error", "finish-validation-error"])
def test_a7b_what_an_executor_raises_is_logged_by_its_class_only(
        world: World, client: TestClient, caplog: pytest.LogCaptureFixture, where: str,
        raised: Callable[[], Exception]) -> None:
    """I-24, SEC-20/21 (M-A10): an exception's text can quote the brief or a
    provider's answer, so `execute` and `_finished` log its class alone. A run
    that raises is a failed attempt; a finish that raises is the 503."""
    caplog.set_level(logging.DEBUG)
    exc = raised()
    assert CANARY in str(exc), "the positive control: the text a leak would log"
    table = world.table()
    if where == "run":
        world.executors[ToolId.NPC].behaviour = _raise(exc)
    else:
        world.executors[ToolId.NPC].finish = _raise_now(exc)  # type: ignore[method-assign]
    response = post(client, table)
    assert response.status_code == (200 if where == "run" else 503)
    assert where == "finish" or response.json()["status"] == "failed"
    assert CANARY not in response.text
    failed = "tool attempt failed" if where == "run" else "tool finish failed"
    [logged] = [record.getMessage() for record in caplog.records if record.getMessage().startswith(failed)]
    for record in caplog.records:
        assert CANARY not in record.getMessage()
        assert all(CANARY not in str(arg) for arg in (record.args or ()))
    assert CANARY not in caplog.text
    assert logged.endswith(f"error={type(exc).__name__})")


@pytest.mark.parametrize("where", ["run", "finish"], ids=["run-refusal", "finish-refusal"])
def test_a7c_a_refused_results_own_field_is_never_logged(
        world: World, client: TestClient, caplog: pytest.LogCaptureFixture, where: str) -> None:
    """M-1 (carry from #205, bead 8frw): `judge_result`'s refusal logs error
    locations only (`redacted_errors`'s `loc`), never `exc` itself — a
    produced result can quote the brief or a provider's answer in its own
    field value, and pydantic's `ValidationError` text echoes that value via
    `input_value=...` (I-24, SEC-20/21). A canary sitting in an enum field
    pins this for both call sites: `run`'s own answer is judged in `execute`,
    and a `finish` answer is judged again in `_finished`."""
    caplog.set_level(logging.DEBUG)
    bad = _canary_document()
    with pytest.raises(ValidationError) as excinfo:
        tool_invocations._RESULT.validate_python(bad)
    assert CANARY in str(excinfo.value), "the positive control: the text a leak would log"
    table = world.table()
    if where == "run":
        world.executors[ToolId.NPC].behaviour = lambda ctx: bad
    else:
        world.executors[ToolId.NPC].finish = lambda unit, ctx, result: bad  # type: ignore[method-assign]
    response = post(client, table)
    assert response.status_code == (200 if where == "run" else 503)
    if where == "run":
        answer = response.json()
        assert (answer["status"], answer["error"]["code"], answer["error"]["retryable"]) == (
            "failed", "provider_failed", True)
    assert CANARY not in response.text
    [logged] = [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("tool result refused")]
    for record in caplog.records:
        assert CANARY not in record.getMessage()
        assert all(CANARY not in str(arg) for arg in (record.args or ()))
    assert CANARY not in caplog.text
    assert logged.endswith("at=[['document', 'document', 'type']])")


@pytest.mark.parametrize("where", ["run", "finish"], ids=["run-mismatch", "finish-mismatch"])
def test_a7c_a_result_for_another_tool_is_refused_without_its_text(
        world: World, client: TestClient, caplog: pytest.LogCaptureFixture, where: str) -> None:
    """H-1 (bead 8frw, mutants M5/M6): `judge_result` has a second refusal
    site — a produced result that validates but names a different tool
    (`result.tool_id is not tool_id`) — and that site must stay as blind to
    the produced value as the validation-error site above. A canary in the
    result's own (otherwise valid) fields pins the constant `[["tool_id"]]`
    location against a mutant that logs `raw` or `result` instead."""
    caplog.set_level(logging.DEBUG)
    bad = npc_result(title=CANARY, tool="encounter", doc_type="encounter", category="documents")
    table = world.table()
    if where == "run":
        world.executors[ToolId.NPC].behaviour = lambda ctx: bad
    else:
        world.executors[ToolId.NPC].finish = lambda unit, ctx, result: bad  # type: ignore[method-assign]
    response = post(client, table)
    assert response.status_code == (200 if where == "run" else 503)
    assert CANARY not in response.text
    [logged] = [record.getMessage() for record in caplog.records
                if record.getMessage().startswith("tool result refused")]
    assert CANARY not in caplog.text
    assert logged.endswith("at=[['tool_id']])")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _canary_document() -> dict[str, Any]:
    """A produced result whose enum field (`document.type`) is invalid: the
    same shape `judge_result` refuses, with the canary as the bad value."""
    return npc_result(doc_type=CANARY)


def _canary_validation_error() -> ValidationError:
    """A pydantic error whose text quotes its input, as `input_value=...`."""
    try:
        TypeAdapter(int).validate_python(CANARY)
    except ValidationError as exc:
        return exc
    raise AssertionError("the canary validated as an int")


class _ChatService:
    def answer(self, prompt: str, mode: str = "sage", conversation_id: str | None = None,
               attachment_context: Any = None, attachment_label: Any = None) -> ChatResponse:
        return ChatResponse(answer="ok", sources=[], answerable=True, mode=ChatMode(mode),
                            conversation_id=conversation_id)


def _spend_the_window(user_id: int) -> None:
    for _ in range(config.CHAT_RATE_LIMIT_PER_USER + 1):
        try:
            ratelimit.check_chat_request(user_id)
        except ratelimit.RateLimited:
            return
    raise AssertionError("the window never closed")


def _timeout() -> Exception:
    import httpx
    import openai
    return openai.APITimeoutError(request=httpx.Request("POST", "https://provider.invalid"))


def _raise(exc: BaseException) -> Callable[[ExecutionContext], Any]:
    def run(ctx: ExecutionContext) -> Any:
        raise exc

    return run


def _raise_now(exc: BaseException) -> Callable[..., Any]:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return fail

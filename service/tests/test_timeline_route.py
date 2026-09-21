"""`GET /conversations/{id}/timeline` (1kg.4.2 slice B).

The route, its authorization, its refusals, and the paging walk that proves a
page boundary never splits a prompt from its result. Everything runs through
the app's test client against the in-memory twins — no database, no LLM.

Slice B serves **legacy `chat.messages` rows only**, so every answer here
carries `answerable: null` and `sources: null`. The stored-entry half of the
field-for-field check is slice A's.

Run from the repo root:
    uv run python -m pytest service/tests/test_timeline_route.py -q
"""

from __future__ import annotations

import base64
import json
import logging

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from service.app import (
    app,
    get_message_store,
    get_service,
    get_timeline_database,
    get_timeline_store,
    require_session,
)
from service.db import InMemoryDatabase
from service.history import InMemoryMessageStore
from service.models import ChatMode, ChatResponse, Source
from service.session import SessionData
from service.timeline_store import InMemoryTimelineStore
from service.workbench_contracts import TimelinePage

OWNER = 1
STRANGER = 2
CONVERSATION = "conv-under-test"
CANARY = "Zx9CanaryQ7"


class _FakeService:
    """The RAG service `/chat` talks to, answering without a provider."""

    def __init__(self, response: ChatResponse) -> None:
        self._r = response

    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(
            answer=self._r.answer, sources=self._r.sources, answerable=self._r.answerable,
            mode=ChatMode(mode), conversation_id=conversation_id, suggestions=self._r.suggestions,
        )


class _World:
    """One in-memory database, one message store, one timeline store."""

    def __init__(self) -> None:
        self.db = InMemoryDatabase()
        self.messages = InMemoryMessageStore()
        self.timeline = InMemoryTimelineStore(self.db, messages=self.messages)

    def own(self, conversation_id: str = CONVERSATION, user_id: int = OWNER) -> None:
        self.messages.claim_conversation(conversation_id, user_id)

    def say(self, role: str, content: str, *, mode: str = "sage",
            conversation_id: str = CONVERSATION, suggestions=None) -> None:
        self.messages.append(conversation_id, mode, role, content, suggestions=suggestions)


@pytest.fixture
def world() -> _World:
    return _World()


@pytest.fixture
def client(world: _World):
    # A plain client, never the context manager: entering it runs the app's
    # lifespan, which dials a database that is not there — and `psycopg.connect`
    # to a closed port hangs for ever on Windows.
    app.dependency_overrides[get_timeline_store] = lambda: world.timeline
    app.dependency_overrides[get_timeline_database] = lambda: world.db
    app.dependency_overrides[get_message_store] = lambda: world.messages
    app.dependency_overrides[get_service] = lambda: _FakeService(
        ChatResponse(answer="A basilisk petrifies with its gaze [1].",
                     sources=[Source(book="mm-5e", snippet="Armor Class 15 ...")],
                     answerable=True, mode=ChatMode.sage)
    )
    yield TestClient(app)
    for dependency in (get_timeline_store, get_timeline_database, get_message_store, get_service):
        app.dependency_overrides.pop(dependency, None)


def _timeline(client: TestClient, conversation_id: str = CONVERSATION, **params) -> Response:
    return client.get(f"/conversations/{conversation_id}/timeline", params=params)


def _walk(client: TestClient, size: int) -> list[dict]:
    """Every entry of the conversation, newest first, at this page size."""
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(500):  # a bound, so a cursor that never ends fails instead of hangs
        params = {"limit": str(size)} | ({"cursor": cursor} if cursor is not None else {})
        page = _timeline(client, **params)
        assert page.status_code == 200, page.text
        body = page.json()
        assert len(body["items"]) <= size
        items.extend(body["items"])
        assert "next_cursor" in body, "next_cursor is required: the end is null, never a missing key"
        cursor = body["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("the cursor walk did not terminate")


# ── The happy path ───────────────────────────────────────────────────────────


def test_an_owned_conversation_reads_back_as_exchanges_newest_first(world: _World, client) -> None:
    world.own()
    world.say("user", "first question")
    world.say("assistant", "first answer")
    world.say("user", "second question")
    world.say("assistant", "second answer")

    body = _timeline(client).json()
    assert [e["prompt"] for e in body["items"]] == ["second question", "first question"]
    assert [e["answer"]["text"] for e in body["items"]] == ["second answer", "first answer"]
    assert body["next_cursor"] is None
    assert body["conversation_id"] == CONVERSATION


def test_an_adapted_answer_says_not_recorded_and_never_invents_a_claim(world: _World, client) -> None:
    """Bead design comment (2): `answerable: null` and `sources: null` — never
    `true`, never `[]`, which is what the client used to fill in."""
    world.own()
    world.say("user", "q")
    world.say("assistant", "a")
    answer = _timeline(client).json()["items"][0]["answer"]
    assert answer["answerable"] is None
    assert answer["sources"] is None


def test_an_owned_conversation_with_nothing_in_it_is_an_empty_page_not_a_refusal(world: _World, client) -> None:
    world.own()
    body = _timeline(client).json()
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_every_page_the_route_emits_validates_and_reserialises_under_the_contract(
    world: _World, client
) -> None:
    """C2. `TimelinePage` forbids undeclared fields, so a page that round-trips
    identically carries nothing the contract does not declare."""
    world.own()
    for i in range(6):
        world.say("user", f"q{i}")
        world.say("assistant", f"a{i}")
    world.say("assistant", "an orphan")
    for size in (1, 3, 100):
        cursor = None
        while True:
            params = {"limit": str(size)} | ({"cursor": cursor} if cursor else {})
            body = _timeline(client, **params).json()
            page = TimelinePage.model_validate(body)
            assert json.loads(page.model_dump_json()) == body
            cursor = body["next_cursor"]
            if cursor is None:
                break


# ── C3: pagination never splits a prompt from its result ─────────────────────


def _a_long_mixed_conversation(world: _World) -> None:
    """More than 25 exchanges, mixing every shape the adapter can meet, so that
    an exchange boundary falls at a different place at every page size."""
    world.own()
    world.say("assistant", "an orphan before anything")
    for i in range(26):
        world.say("user", f"question {i}")
        if i % 5 == 3:
            world.say("user", f"asked again {i}")  # two user rows in a row
        if i % 7 != 6:
            world.say("assistant", f"answer {i}")
        if i % 9 == 4:
            world.say("assistant", f"a late second answer {i}")  # an orphan mid-run


def test_walking_the_conversation_at_every_page_size_yields_one_identical_list(
    world: _World, client
) -> None:
    """C3. No entry twice, none missing, and the order never depends on the size."""
    _a_long_mixed_conversation(world)
    unpaged = _timeline(client).json()
    assert unpaged["next_cursor"] is None, "the whole conversation must fit one page of 100"
    expected = unpaged["items"]
    assert len(expected) > 25

    for size in [*range(1, 11), 100]:
        walked = _walk(client, size)
        assert walked == expected, f"page size {size} changed the list"
        ids = [e["entry_id"] for e in walked]
        assert len(ids) == len(set(ids)), f"page size {size} served an entry twice"


def test_no_page_boundary_can_separate_a_prompt_from_its_answer(world: _World, client) -> None:
    """C3, stated directly: an exchange is one entry, so a prompt and its answer
    are on the same page whatever the page size or where the boundary falls."""
    _a_long_mixed_conversation(world)
    for size in range(1, 11):
        cursor = None
        while True:
            params = {"limit": str(size)} | ({"cursor": cursor} if cursor else {})
            body = _timeline(client, **params).json()
            for entry in body["items"]:
                if entry["entry_kind"] == "chat" and entry["prompt"] is not None:
                    # Whatever the answer is, it travelled with its prompt.
                    assert "answer" in entry
            cursor = body["next_cursor"]
            if cursor is None:
                break


def test_a_cursor_resumes_exactly_where_the_page_stopped(world: _World, client) -> None:
    world.own()
    for i in range(5):
        world.say("user", f"q{i}")
        world.say("assistant", f"a{i}")
    first = _timeline(client, limit="2").json()
    assert first["next_cursor"] is not None
    second = _timeline(client, limit="2", cursor=first["next_cursor"]).json()
    assert [e["entry_id"] for e in first["items"]] == ["9", "7"]
    assert [e["entry_id"] for e in second["items"]] == ["5", "3"]


# ── C4: what the legacy rows can and cannot make ─────────────────────────────


def test_an_orphan_assistant_row_is_rendered_where_the_client_used_to_drop_it(
    world: _World, client
) -> None:
    world.own()
    world.say("assistant", "an answer whose prompt fell out of the window")
    entry = _timeline(client).json()["items"][0]
    assert entry["prompt"] is None
    assert entry["answer"]["text"] == "an answer whose prompt fell out of the window"


def test_the_legacy_messages_endpoint_answers_exactly_as_it_did(world: _World, client) -> None:
    """C4 / C13. The new table and the new route change nothing next door: same
    fields, same oldest-first order, same limit semantics, same 403 posture."""
    world.own()
    world.say("user", "q")
    world.say("assistant", "a")
    legacy = client.get(f"/conversations/{CONVERSATION}/messages")
    assert legacy.status_code == 200
    body = legacy.json()
    assert body["conversation_id"] == CONVERSATION
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert [m["content"] for m in body["messages"]] == ["q", "a"]
    assert set(body["messages"][0]) == {"id", "role", "content", "mode", "created_at", "suggestions"}


def test_the_two_endpoints_describe_the_same_conversation(world: _World, client) -> None:
    world.own()
    for i in range(4):
        world.say("user", f"q{i}")
        world.say("assistant", f"a{i}")
    legacy = client.get(f"/conversations/{CONVERSATION}/messages").json()["messages"]
    entries = _timeline(client).json()["items"]
    assert [m["content"] for m in legacy if m["role"] == "user"] == [
        e["prompt"] for e in reversed(entries)
    ]


# ── C1: live and reloaded carry the same answer text ─────────────────────────


def test_what_chat_answered_is_what_the_timeline_reads_back(world: _World, client) -> None:
    """C1, as far as legacy rows can prove it: the prompt and the answer text
    survive the round trip exactly. `answerable` and `sources` read back as
    *not recorded* until slice A writes a typed entry for the turn."""
    world.own()
    live = client.post("/chat", json={"prompt": "How does a gaze work?", "mode": "sage",
                                      "conversation_id": CONVERSATION})
    assert live.status_code == 200, live.text
    answered = live.json()

    entry = _timeline(client).json()["items"][0]
    assert entry["prompt"] == "How does a gaze work?"
    assert entry["answer"]["text"] == answered["answer"]
    assert entry["mode"] == answered["mode"]
    assert entry["answer"]["answerable"] is None
    assert entry["answer"]["sources"] is None


# ── C10(b): authorization ────────────────────────────────────────────────────


def _bodies_for_404(world: _World, client) -> list[Response]:
    world.own("someone-elses", STRANGER)
    world.say("user", "not yours", conversation_id="someone-elses")
    world.say("user", "unowned but written", conversation_id="never-claimed")
    return [
        _timeline(client, "no-such-conversation"),
        _timeline(client, "never-claimed"),
        _timeline(client, "someone-elses"),
    ]


def test_missing_unowned_and_foreign_conversations_answer_one_identical_404(
    world: _World, client
) -> None:
    """SEC-3: a resource that is missing, belongs to someone else, or was never
    claimed gives the identical answer, from one code path — otherwise the route
    is an enumeration oracle."""
    answers = _bodies_for_404(world, client)
    assert [a.status_code for a in answers] == [404, 404, 404]
    assert len({a.text for a in answers}) == 1, "the three 404 bodies must be byte-identical"
    body = answers[0].json()
    assert body["detail"]["code"] == "not_found"
    assert body["detail"]["retryable"] is False
    assert "never-claimed" not in answers[1].text


def test_reading_an_unowned_conversation_claims_nothing(world: _World, client) -> None:
    """§8.1: this route never claims. `GET …/messages` still does, deliberately,
    and is not touched — which is why the two answer differently here."""
    world.say("user", "written before the ownership table existed",
              conversation_id="never-claimed")
    before = dict(world.messages._owners)
    assert _timeline(client, "never-claimed").status_code == 404
    assert world.messages._owners == before, "the timeline route minted an ownership row"


def test_a_signed_in_caller_without_the_dm_role_is_refused_without_naming_a_resource(
    world: _World, client
) -> None:
    """R-3: every new Workbench GM route requires the `dm` role (SEC-2)."""
    world.own()
    world.say("user", "q")
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=OWNER, role="player")
    try:
        refused = _timeline(client)
    finally:
        app.dependency_overrides[require_session] = lambda: SessionData(user_id=OWNER, role="dm")
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "forbidden"
    assert CONVERSATION not in refused.text


def test_the_role_is_checked_before_the_conversation_is_looked_up(world: _World, client) -> None:
    """SEC-3's order. A non-`dm` caller learns nothing about which ids exist."""
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=OWNER, role="player")
    try:
        missing = _timeline(client, "no-such-conversation")
        present = _timeline(client, CONVERSATION)
    finally:
        app.dependency_overrides[require_session] = lambda: SessionData(user_id=OWNER, role="dm")
    assert missing.status_code == present.status_code == 403
    assert missing.text == present.text


# The 401 is proved by `service/tests/test_auth_guard.py`'s route matrix, which
# walks the real routing table for everything depending on `require_session` and
# fails if a guarded route is not listed. This route is listed there, so both
# halves — 401 without a session, not-401 with one — run against it.
# `require_session` itself is left exactly as it is (R-4); SEC-2's single 401
# body belongs to `agent-forge-harness-oe6` (R-5).


# ── C7 (route) and the refusals ──────────────────────────────────────────────


@pytest.mark.parametrize("params", [
    {"limit": "0"}, {"limit": "-3"}, {"limit": "101"}, {"limit": "nine"}, {"limit": ""},
])
def test_a_limit_outside_the_bound_is_a_422_that_never_echoes_it(
    world: _World, client, params: dict
) -> None:
    world.own()
    refused = _timeline(client, **params)
    assert refused.status_code == 422
    body = refused.json()
    assert body["detail"]["code"] == "validation_failed"
    assert body["detail"]["field"] == "limit"
    assert params["limit"] not in refused.text or params["limit"] == ""


def test_a_cursor_this_server_did_not_mint_is_a_422_that_never_echoes_it(
    world: _World, client
) -> None:
    world.own()
    forged = base64.urlsafe_b64encode(json.dumps({"v": 1, "e": None, "m": [CANARY, 1]}).encode()
                                      ).decode().rstrip("=")
    refused = _timeline(client, cursor=forged)
    assert refused.status_code == 422
    assert refused.json()["detail"]["field"] == "cursor"
    assert CANARY not in refused.text and forged not in refused.text


def test_no_response_body_of_this_route_echoes_a_supplied_parameter(world: _World, client) -> None:
    """SEC-23 / R-12: FastAPI's default 422 repeats the request's own input,
    which is why `limit` and `cursor` are declared `str | None` and validated
    by this route instead."""
    world.own()
    for supplied in ({"limit": CANARY}, {"cursor": CANARY}, {"limit": CANARY, "cursor": CANARY}):
        refused = _timeline(client, **supplied)
        assert refused.status_code == 422
        assert CANARY not in refused.text


def test_an_attachments_extracted_text_never_reaches_a_timeline_entry(
    world: _World, client
) -> None:
    """SEC-20 / the wire contract: an entry can never carry attachment text."""
    world.own()
    world.messages.append_attachment(CONVERSATION, "notes.txt", "text/plain", f"secret {CANARY}")
    world.say("user", "q")
    world.say("assistant", "a")
    assert CANARY not in _timeline(client).text


def test_a_refusal_is_logged_without_any_of_the_conversations_text(
    world: _World, client, caplog: pytest.LogCaptureFixture
) -> None:
    world.own()
    world.say("user", f"a prompt holding {CANARY}")
    with caplog.at_level(logging.DEBUG):
        assert _timeline(client, limit="0").status_code == 422
        assert _timeline(client, "no-such-conversation").status_code == 404
    for record in caplog.records:
        assert CANARY not in record.getMessage()
        assert all(CANARY not in str(arg) for arg in (record.args or ()))


def test_without_a_store_or_a_database_the_route_fails_closed_with_503(world: _World) -> None:
    """Deliberately not `conversation_messages()`'s bare string body: that one is
    legacy and keeps its own, unchanged."""
    for absent in (get_timeline_store, get_timeline_database):
        app.dependency_overrides[get_timeline_store] = lambda: world.timeline
        app.dependency_overrides[get_timeline_database] = lambda: world.db
        app.dependency_overrides[absent] = lambda: None
        refused = _timeline(TestClient(app))
        assert refused.status_code == 503
        assert refused.json()["detail"]["code"] == "backend_unavailable"
        assert refused.json()["detail"]["retryable"] is True
    for dependency in (get_timeline_store, get_timeline_database):
        app.dependency_overrides.pop(dependency, None)


def test_a_database_failure_during_the_read_is_a_503_and_not_a_500(world: _World) -> None:
    class _Exploding:
        def owner_of(self, unit, conversation_id):
            import psycopg

            raise psycopg.OperationalError("the server closed the connection")

        def legacy_window(self, unit, conversation_id, *, before, limit_rows):
            raise AssertionError("never reached")

    app.dependency_overrides[get_timeline_store] = lambda: _Exploding()
    app.dependency_overrides[get_timeline_database] = lambda: world.db
    refused = _timeline(TestClient(app))
    for dependency in (get_timeline_store, get_timeline_database):
        app.dependency_overrides.pop(dependency, None)
    assert refused.status_code == 503
    assert refused.json()["detail"]["code"] == "backend_unavailable"


def test_the_route_is_absent_until_its_dependencies_are_overridden() -> None:
    """Every existing app test keeps passing untouched: without an override both
    dependencies answer `None` and nothing new runs on any other path."""
    assert TestClient(app).get(f"/conversations/{CONVERSATION}/timeline").status_code == 503

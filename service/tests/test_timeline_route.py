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
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from service import timeline
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


#: Every cursor walk in this file goes through `_pages` and is bounded by this;
#: none is written as a bare `while True`. A cursor that stops advancing is a
#: real regression — `_window`'s `<` becoming `<=` is one — and an unbounded
#: walk turns it into a hung suite with no diagnostic instead of a failing test.
_WALK_BOUND = 500


def _pages(client: TestClient, size: int, *, cursor: str | None = None):
    """Every page of the conversation at this size, from this cursor, bounded."""
    for _ in range(_WALK_BOUND):
        params = {"limit": str(size)} | ({"cursor": cursor} if cursor is not None else {})
        page = _timeline(client, **params)
        assert page.status_code == 200, page.text
        body = page.json()
        assert "next_cursor" in body, "next_cursor is required: the end is null, never a missing key"
        yield body
        cursor = body["next_cursor"]
        if cursor is None:
            return
    raise AssertionError("the cursor walk did not terminate")


def _walk(client: TestClient, size: int) -> list[dict]:
    """Every entry of the conversation, newest first, at this page size."""
    items: list[dict] = []
    for body in _pages(client, size):
        assert len(body["items"]) <= size
        items.extend(body["items"])
    return items


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
        for body in _pages(client, size):
            page = TimelinePage.model_validate(body)
            assert json.loads(page.model_dump_json()) == body


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
        for body in _pages(client, size):
            for entry in body["items"]:
                if entry["entry_kind"] == "chat" and entry["prompt"] is not None:
                    # Whatever the answer is, it travelled with its prompt.
                    assert "answer" in entry


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
    # L-1. `"" not in text` is true of nothing, so the empty `limit` cannot be
    # pinned by looking for it. It is pinned the other way instead: its refusal
    # is byte-identical to another limit's, so it carries none of its own input.
    if params["limit"]:
        assert params["limit"] not in refused.text
    else:
        assert refused.text == _timeline(client, limit="nine").text


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


# ── Carry items from the part-1 review ───────────────────────────────────────


def test_a_cursors_entry_position_survives_every_page_boundary(world: _World, client) -> None:
    """M-2. Slice B reads one source, so the cursor's `entries` slot is always
    null in practice and a `read_page` that dropped it passes every other test
    here. It is still the line slice A's stored-entry source rides on: lose it
    at a page boundary and stored entries are re-read or skipped every time the
    client turns a page. Pinned as behaviour, with a position this server would
    never mint on its own, so nothing can reconstruct it by accident.
    """
    world.own()
    for n in range(6):
        world.say("user", f"q{n}")
        world.say("assistant", f"a{n}")
    held = timeline.Position(created_at=datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC), tiebreak=4242)
    supplied = timeline.encode_cursor(timeline.TimelineCursor(entries=held, messages=None))

    pages = 0
    for body in _pages(client, 2, cursor=supplied):
        cursor = body["next_cursor"]
        pages += 1
        if cursor is not None:
            assert timeline.decode_cursor(cursor).entries == held, (
                "the entries position must be carried verbatim across the boundary"
            )
    assert pages > 1, "the walk must cross a page boundary for this to prove anything"


def test_the_page_a_carried_entry_position_produces_is_the_page_without_one(
    world: _World, client
) -> None:
    """The other half of M-2: carrying the slot changes nothing this slice
    serves, so a future source can be added without moving a single item."""
    world.own()
    for n in range(5):
        world.say("user", f"q{n}")
        world.say("assistant", f"a{n}")
    held = timeline.encode_cursor(timeline.TimelineCursor(
        entries=timeline.Position(created_at=datetime(2021, 3, 4, 5, 6, 7, tzinfo=UTC), tiebreak=7),
        messages=None,
    ))
    with_slot = _timeline(client, limit="3", cursor=held).json()
    without = _timeline(client, limit="3").json()
    assert with_slot["items"] == without["items"]


# ── The route's own obligations, pinned rather than read off the source ──────


class _Recorder:
    """A store that answers exactly as the twin does and remembers the order
    it was called in, and the unit of work it was handed each time."""

    def __init__(self, inner: InMemoryTimelineStore) -> None:
        self._inner = inner
        self.calls: list[tuple[str, int]] = []

    def owner_of(self, unit, conversation_id):
        self.calls.append(("owner_of", id(unit)))
        return self._inner.owner_of(unit, conversation_id)

    def legacy_window(self, unit, conversation_id, *, before, limit_rows):
        self.calls.append(("legacy_window", id(unit)))
        return self._inner.legacy_window(
            unit, conversation_id, before=before, limit_rows=limit_rows
        )


class _CountingDatabase:
    """The twin's database, counting the transactions the route opens."""

    def __init__(self, inner: InMemoryDatabase) -> None:
        self._inner = inner
        self.transactions = 0

    def transaction(self):
        self.transactions += 1
        return self._inner.transaction()


@pytest.fixture
def recorded(world: _World):
    store = _Recorder(world.timeline)
    db = _CountingDatabase(world.db)
    app.dependency_overrides[get_timeline_store] = lambda: store
    app.dependency_overrides[get_timeline_database] = lambda: db
    app.dependency_overrides[get_message_store] = lambda: world.messages
    yield store, db, TestClient(app)
    for dependency in (get_timeline_store, get_timeline_database, get_message_store):
        app.dependency_overrides.pop(dependency, None)


def test_ownership_is_resolved_before_the_read_and_in_the_same_unit_of_work(
    world: _World, recorded
) -> None:
    """Carry item 1. `read_page` does not enforce ownership itself — `authorize`
    is a separate call — so nothing in the read model stops a handler reading
    first and checking afterwards. SEC-2 requires the resolution to happen in
    the same unit as the read, and §8.1 requires the refusal to come before any
    content is touched. Both are properties of the handler, so the handler is
    what this test drives.
    """
    store, db, client = recorded
    world.own()
    world.say("user", "q")
    world.say("assistant", "a")
    assert _timeline(client).status_code == 200
    assert [name for name, _ in store.calls] == ["owner_of", "legacy_window"], (
        "the ownership check must come first"
    )
    assert len({unit for _, unit in store.calls}) == 1, (
        "one unit of work, so ownership cannot be resolved against a different snapshot"
    )
    assert db.transactions == 1, "one transaction, and therefore one connection"


@pytest.mark.parametrize("conversation_id", ["no-such-conversation", "never-claimed", "someone-elses"])
def test_a_refused_conversation_is_never_read_at_all(
    world: _World, recorded, conversation_id: str
) -> None:
    """The other half: a 404 costs exactly one ownership statement, and the
    conversation's rows are not touched on the way to it."""
    store, _db, client = recorded
    world.own("someone-elses", STRANGER)
    world.say("user", "not yours", conversation_id="someone-elses")
    world.say("user", "unowned but written", conversation_id="never-claimed")
    store.calls.clear()
    assert _timeline(client, conversation_id).status_code == 404
    assert [name for name, _ in store.calls] == ["owner_of"]


def test_the_three_404s_are_indistinguishable_in_status_body_and_headers(
    world: _World, client
) -> None:
    """SEC-3, restated over the whole response rather than over its body: a
    header that differed would be an oracle just as surely as a message."""
    answers = _bodies_for_404(world, client)
    assert {a.status_code for a in answers} == {404}
    assert len({a.text for a in answers}) == 1
    varying = {"date", "content-length", "server"}
    shapes = {
        tuple(sorted((k.lower(), v) for k, v in a.headers.items() if k.lower() not in varying))
        for a in answers
    }
    assert len(shapes) == 1, f"the 404 responses differ outside the body: {shapes}"


def test_no_refusal_of_this_route_is_ever_logged_with_a_traceback(
    world: _World, client, caplog: pytest.LogCaptureFixture
) -> None:
    """Carry item 2. The refusal body is fixed-sentence and the refusal no
    longer chains the value (`parse_page_query` raises `from None`), but a log
    line carrying `exc_info` would print whatever chain there is. There is no
    such line, and this is what keeps it that way.
    """
    world.own()
    with caplog.at_level(logging.DEBUG):
        for params in ({"limit": CANARY}, {"cursor": CANARY}, {"limit": "0"}):
            assert _timeline(client, **params).status_code == 422
        assert _timeline(client, "no-such-conversation").status_code == 404
    # The service's own loggers. `httpx`'s DEBUG line repeats the request URL of
    # every call the test client makes, for every route in the tree; that is the
    # harness talking, not this handler, and SEC-23 is about what the server
    # emits.
    def ours() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.name.startswith("service")]

    assert ours() == [], "a refused parameter or a refused conversation was logged at all"

    # Not vacuous: the one path that does log — a database failure during the
    # read — is driven here, and the same assertions are made of it.
    class _Exploding:
        def owner_of(self, unit, conversation_id):
            import psycopg

            raise psycopg.OperationalError(f"the server closed the connection: {CANARY}")

        def legacy_window(self, unit, conversation_id, *, before, limit_rows):
            raise AssertionError("never reached")

    app.dependency_overrides[get_timeline_store] = lambda: _Exploding()
    try:
        with caplog.at_level(logging.DEBUG):
            assert _timeline(client).status_code == 503
    finally:
        app.dependency_overrides[get_timeline_store] = lambda: world.timeline
    logged = ours()
    assert logged, "the 503 must leave a trace, or the assertions below are vacuous"
    for record in logged:
        assert record.exc_info is None, "logged with a traceback: the chain reaches the input"
        assert CANARY not in record.getMessage()
        assert all(CANARY not in str(arg) for arg in (record.args or ()))


def test_the_chat_request_path_gains_no_statement_no_round_trip_and_no_branch() -> None:
    """The owner's standing constraint, and R-1's sentence that it is not
    relaxed. Slice B is the read side only: `/chat` writes no entry, so its
    handler must not name the timeline at all — not a store, not a database,
    not a helper. A diff can be read once; this fails the day someone adds one.
    """
    import inspect

    from service import app as app_module

    source = inspect.getsource(app_module.chat)
    assert "timeline" not in source.lower()
    signature = inspect.signature(app_module.chat)
    assert not any("timeline" in name for name in signature.parameters)


# ── Rework 1: carry items from the part-2 review ─────────────────────────────

#: Ids `/chat` accepts — its `conversation_id` is a bare `str` — and that
#: `TimelinePage.conversation_id`'s `OpaqueId` (`^[A-Za-z0-9_-]{1,64}$`) does
#: not. Each one is owned and readable through the unchanged legacy route; each
#: one used to build the page and raise a `ValidationError` inside the
#: transaction, where it was neither `ConversationNotFound` nor a database
#: error, and escaped as a bare 500.
MALFORMED_IDS = [
    "has.a.dot", "has:a:colon", "has a space", "café-ünïcode",
    "a" * 65, " ", "nul\x00byte", "newline\nforged",
]


def _timeline_encoded(client: TestClient, conversation_id: str) -> Response:
    """The id percent-encoded here rather than by httpx, which refuses to put a
    control character on the wire at all. Starlette hands the handler the
    decoded string, which is the one the store was seeded under."""
    return client.get(f"/conversations/{quote(conversation_id, safe='')}/timeline")


@pytest.mark.parametrize("conversation_id", MALFORMED_IDS)
def test_an_id_the_contract_cannot_carry_is_the_same_404_and_costs_no_statement(
    world: _World, recorded, conversation_id: str
) -> None:
    """H-1. An owner asking for a conversation whose id is outside `OpaqueId`
    used to get a 500 from their own conversation. It is refused now — through
    the *same* code path a missing or a foreign conversation takes, so malformed
    and missing stay indistinguishable (SEC-3) — and before any statement runs.
    """
    store, _db, client = recorded
    world.own(conversation_id)
    world.say("user", "written through /chat, which accepts this id",
              conversation_id=conversation_id)
    store.calls.clear()
    refused = _timeline_encoded(client, conversation_id)
    assert refused.status_code == 404
    assert refused.json()["detail"]["code"] == "not_found"
    assert store.calls == [], "the shape is decided before the ownership statement"
    # Byte-identical to a missing conversation's, which is the check that cannot
    # go vacuous the way `id not in text` does for an id like `" "`.
    assert refused.text == _timeline(client, "no-such-conversation").text


def test_a_malformed_a_missing_and_a_foreign_id_are_one_response(
    world: _World, client
) -> None:
    """H-1, over the whole response rather than its status: a fourth refusal
    shape would be a fourth oracle. Status, body and every header that does not
    vary per request are diffed equal across all three."""
    answers = _bodies_for_404(world, client) + [
        _timeline_encoded(client, malformed) for malformed in MALFORMED_IDS
    ]
    assert {a.status_code for a in answers} == {404}
    assert len({a.text for a in answers}) == 1, "the bodies must be byte-identical"
    varying = {"date", "content-length", "server"}
    shapes = {
        tuple(sorted((k.lower(), v) for k, v in a.headers.items() if k.lower() not in varying))
        for a in answers
    }
    assert len(shapes) == 1, f"the responses differ outside the body: {shapes}"


def test_the_cursor_bound_is_the_routes_own_and_is_applied_before_any_decode(
    world: _World, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-2. `Cursor` is `^[A-Za-z0-9_-]{1,512}$`, and that bound is what stops a
    50 kB parameter being base64-decoded and `json.loads`-ed by this route. The
    bound was unpinned: `TypeAdapter(Cursor)` -> `TypeAdapter(str)` left the
    suite green. Pinned here at the boundary, and by *whether a decode was
    attempted* — which is the only thing that separates the two sides.
    """
    world.own()
    world.say("user", "q")
    attempted: list[str] = []
    real = timeline.decode_cursor
    monkeypatch.setattr(
        timeline, "decode_cursor", lambda text: (attempted.append(text), real(text))[1]
    )

    at_bound = "A" * 512
    refused = _timeline(client, cursor=at_bound)
    assert refused.status_code == 422, "512 characters is inside the bound"
    assert attempted == [at_bound], "a cursor at the bound must reach the decoder"

    for outside in ("A" * 513, "A" * 4096, "not-base64url!"):
        attempted.clear()
        refused = _timeline(client, cursor=outside)
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "validation_failed"
        assert refused.json()["detail"]["field"] == "cursor"
        assert outside not in refused.text
        assert attempted == [], "outside the shape, the route must not decode at all"


def test_no_log_line_of_this_route_carries_the_path_parameter(
    world: _World, client, caplog: pytest.LogCaptureFixture
) -> None:
    """M-2. The 503 path used to interpolate the raw path parameter into its log
    line, so a `%0A` in the id forged a log record. H-1 keeps a newline out of
    that id, but the log line must not rest on it: it records the fact, never
    the value. Driven with a canary the id shape *does* admit, so the assertion
    cannot pass merely because the request was refused earlier.
    """
    class _Exploding:
        def owner_of(self, unit, conversation_id):
            import psycopg

            raise psycopg.OperationalError("the server closed the connection")

        def legacy_window(self, unit, conversation_id, *, before, limit_rows):
            raise AssertionError("never reached")

    app.dependency_overrides[get_timeline_store] = lambda: _Exploding()
    try:
        with caplog.at_level(logging.DEBUG):
            assert _timeline(client, f"{CANARY}-conversation").status_code == 503
    finally:
        app.dependency_overrides[get_timeline_store] = lambda: world.timeline
    ours = [r for r in caplog.records if r.name.startswith("service")]
    assert ours, "the 503 must leave a trace, or what follows is vacuous"
    for record in ours:
        assert CANARY not in record.getMessage()
        assert all(CANARY not in str(arg) for arg in (record.args or ()))

    # And the forging shape itself never reaches a logger at all.
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert _timeline_encoded(
            client, "forged\nWARNING:root:not a real line"
        ).status_code == 404
    assert [r for r in caplog.records if r.name.startswith("service")] == []

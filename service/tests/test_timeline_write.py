"""`POST /chat` writes one typed timeline entry per answered turn (1kg.4.2 slice A).

The write is best-effort and comes after the answer: `_record_timeline_entry`
runs immediately after the two `_persist_turn` calls, inside `chat()`'s `try:`,
on one short transaction of its own. So the four partial writes of requirement 8
are all reachable. What this file proves:

* **C1**: what `/chat` answered, the timeline reads back field for field.
* **C11**: each of the four partial writes reads back as exactly one exchange,
  and nothing the timeline does can fail an answer or change its bytes.
* **C7**: no log line and no metric carries the turn's text, and attachment
  text never reaches an entry.
* **The connection budget**: the entry's one transaction opens after the
  answer and after both message rows, and never on a path with no answer.

Everything runs through the app's test client against the in-memory twins:
no database and no LLM.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import textwrap
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import openai
import psycopg
import pytest
from fastapi.testclient import TestClient

from service import app as app_module
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
from service.model_catalog import DEFAULT_ALIAS
from service.models import (
    ChatMode,
    ChatResponse,
    Source,
    SpellContent,
    StatBlockContent,
    Suggestion,
    SuggestionStyle,
)
from service.session import SessionData
from service.timeline_store import InMemoryTimelineStore
from service.workbench_contracts import MAX_SOURCES

CONVERSATION = "conv-under-test"
PROMPT = "How does a basilisk's gaze work?"
#: In the prompt, the answer and a source snippet — text the database may hold.
CANARY = "Zx9CanaryQ7"
#: In an attachment only — text no entry may ever hold.
ATTACHMENT_CANARY = "Qq7AttachWz3"


def _rich_response(**changes: Any) -> ChatResponse:
    """An answer carrying every field `ChatAnswer` stores, so a field lost on
    the way to the entry cannot go unnoticed."""
    response = ChatResponse(
        answer="A basilisk petrifies with its gaze [1].",
        sources=[
            Source(book="mm-5e", chapter="Monsters", section="Basilisk", entity="basilisk", page=24,
                   snippet="Petrifying Gaze. If a creature starts its turn within 30 feet ..."),
            Source(book="dmg-5e", snippet="Petrified. A petrified creature is transformed ..."),
        ],
        answerable=True,
        mode=ChatMode.spell,
        suggestions=[
            Suggestion(style=SuggestionStyle.practical, text="Carry a mirror."),
            Suggestion(style=SuggestionStyle.roleplay, text="Describe the stone statues first."),
            Suggestion(style=SuggestionStyle.wacky, text="Teach it to wink."),
        ],
        spell_content=SpellContent(name="Flesh to Stone", description="You attempt to turn one creature ...",
                                   level=6, school="transmutation", concentration=True),
        stat_block=StatBlockContent(name="Basilisk", ac=15, hp=52, size="Medium", type="monstrosity"),
    )
    return response.model_copy(update=changes)


class _FakeService:
    """The RAG service `/chat` talks to. It answers without a provider, and
    can raise instead, or run a check at the moment the provider would be
    called."""

    def __init__(self, response: ChatResponse, *, raises: BaseException | None = None,
                 during_answer: Callable[[], None] | None = None) -> None:
        self._response, self._raises, self._during = response, raises, during_answer

    def answer(self, prompt: str, mode: str = "sage", conversation_id: str | None = None,
               attachment_context: str | None = None, attachment_label: str | None = None) -> ChatResponse:
        if self._during is not None:
            self._during()
        if self._raises is not None:
            raise self._raises
        # A fresh copy each time: `/chat` sets `routing` on what it is handed.
        return self._response.model_copy(update={"mode": ChatMode(mode), "conversation_id": conversation_id},
                                         deep=True)


class _World:
    def __init__(self, messages: InMemoryMessageStore | None = None) -> None:
        self.db = InMemoryDatabase()
        self.messages = messages or InMemoryMessageStore()
        self.timeline = InMemoryTimelineStore(self.db, messages=self.messages)

    def entries(self) -> list[Any]:
        with self.db.transaction() as unit:
            rows: list[Any] = self.timeline.entry_window(unit, CONVERSATION, before=None, limit=100)
        return rows


def _serve(world: _World, service: _FakeService | None = None, *, timeline: Any = None, db: Any = None) -> None:
    app.dependency_overrides[get_message_store] = lambda: world.messages
    app.dependency_overrides[get_service] = lambda: service or _FakeService(_rich_response())
    app.dependency_overrides[get_timeline_store] = lambda: world.timeline if timeline is None else timeline
    app.dependency_overrides[get_timeline_database] = lambda: world.db if db is None else db


@pytest.fixture
def world() -> Iterator[_World]:
    yield _World()
    for dependency in (get_message_store, get_service, get_timeline_store, get_timeline_database):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(world: _World) -> TestClient:
    # Never the context manager: entering it runs the lifespan, which dials a
    # database that is not there.
    _serve(world)
    return TestClient(app)


def _ask(client: TestClient, prompt: str = PROMPT, mode: str = "spell", **more: str) -> Any:
    return client.post("/chat", json={"prompt": prompt, "mode": mode, "conversation_id": CONVERSATION, **more})


def _timeline(client: TestClient) -> list[dict[str, Any]]:
    page = client.get(f"/conversations/{CONVERSATION}/timeline")
    assert page.status_code == 200, page.text
    items: list[dict[str, Any]] = page.json()["items"]
    return items


# ── C1: live and reloaded carry the same answer ──────────────────────────────

#: Every field of `ChatAnswer` that `/chat` returns, under `/chat`'s name for it.
#: `created_at` is not here: `ChatResponse` carries no time to compare with.
ANSWER_FIELDS = {
    "text": "answer", "answerable": "answerable", "sources": "sources", "suggestions": "suggestions",
    "routing": "routing", "suggestions_routing": "suggestions_routing",
    "spell_content": "spell_content", "stat_block": "stat_block",
}


def test_what_chat_answered_is_what_the_timeline_reads_back_field_for_field(client: TestClient) -> None:
    live = _ask(client)
    assert live.status_code == 200, live.text
    answered = live.json()
    assert all(answered[field] not in (None, []) for field in ANSWER_FIELDS.values()), (
        "every field must be set, or its equality below proves nothing"
    )
    [entry] = _timeline(client)
    assert entry["entry_kind"] == "chat" and entry["prompt"] == PROMPT and entry["mode"] == answered["mode"]
    for stored, returned in ANSWER_FIELDS.items():
        assert entry["answer"][stored] == answered[returned], f"{stored} differs from what /chat returned"
    assert set(entry["answer"]) == {*ANSWER_FIELDS, "created_at"}


# ── C11: the four partial writes, through the route ──────────────────────────


class _TimelineThatCannotWrite:
    """Reads like the twin; every write raises, with the turn's text in the
    message, as a driver error quoting its statement would."""

    def __init__(self, inner: InMemoryTimelineStore) -> None:
        self._inner = inner

    def append(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError(f"INSERT failed for a payload containing {CANARY}")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AssistantRowsFail(InMemoryMessageStore):
    """A message store that loses every assistant row: the user row is written
    and the answer's row is not."""

    def append(self, conversation_id: str, mode: str, role: str, content: str,
               suggestions: list[dict[str, Any]] | None = None) -> int | None:
        if role == "assistant":
            raise RuntimeError("the assistant row was lost")
        return super().append(conversation_id, mode, role, content, suggestions=suggestions)


def test_a_turn_whose_every_write_succeeded_is_one_entry_carrying_the_whole_outcome(
    world: _World, client: TestClient
) -> None:
    assert _ask(client).status_code == 200
    [row] = world.entries()
    stored = [m.id for m in world.messages.recent(CONVERSATION, 10)]
    assert [row.user_message_id, row.assistant_message_id] == stored, "the entry links both of the turn's rows"
    [item] = _timeline(client)
    assert item["entry_id"] == row.entry_id and not item["entry_id"].isdecimal()
    assert item["answer"]["answerable"] is True and len(item["answer"]["sources"]) == 2


def test_a_failed_entry_write_never_fails_the_answer_and_the_turn_reads_back_adapted(world: _World) -> None:
    _serve(world, timeline=_TimelineThatCannotWrite(world.timeline))
    live = _ask(TestClient(app))
    assert live.status_code == 200 and live.json()["answer"] == _rich_response().answer
    assert world.entries() == []
    [item] = _timeline(TestClient(app))
    assert item["entry_id"].isdecimal() and item["prompt"] == PROMPT
    assert item["answer"]["text"] == _rich_response().answer
    assert item["answer"]["answerable"] is None and item["answer"]["sources"] is None


def test_a_lost_assistant_row_still_reads_back_as_one_complete_entry() -> None:
    world = _World(_AssistantRowsFail())
    try:
        _serve(world)
        assert _ask(TestClient(app)).status_code == 200
        [row] = world.entries()
        assert row.user_message_id is not None and row.assistant_message_id is None
        [item] = _timeline(TestClient(app))
        assert item["entry_id"] == row.entry_id, "the orphan user row is suppressed, not shown beside it"
        assert item["answer"]["text"] == _rich_response().answer and item["answer"]["answerable"] is True
    finally:
        for dependency in (get_message_store, get_service, get_timeline_store, get_timeline_database):
            app.dependency_overrides.pop(dependency, None)


def test_a_turn_whose_only_stored_row_is_its_prompt_reads_back_unanswered() -> None:
    world = _World(_AssistantRowsFail())
    try:
        _serve(world, timeline=_TimelineThatCannotWrite(world.timeline))
        assert _ask(TestClient(app)).status_code == 200
        _serve(world)
        [item] = _timeline(TestClient(app))
        assert item["entry_id"].isdecimal() and item["prompt"] == PROMPT and item["answer"] is None
    finally:
        for dependency in (get_message_store, get_service, get_timeline_store, get_timeline_database):
            app.dependency_overrides.pop(dependency, None)


class _NoDatabase:
    def transaction(self) -> Any:
        raise psycopg.OperationalError(f"connection refused while writing {CANARY}")


class _NotAStore:
    """Not even the shape of a store: every call is an `AttributeError`."""


@pytest.mark.parametrize("breakage", ["the insert raises", "no transaction can open", "not a store at all"])
def test_nothing_the_timeline_does_can_fail_an_answer_or_change_its_bytes(world: _World, breakage: str) -> None:
    """The helper sits inside `chat()`'s `try:`. Without its own catch-all,
    each of these would be answered as a 500 by `chat()`'s `except Exception`."""
    _serve(world, timeline=None, db=None)
    app.dependency_overrides[get_timeline_store] = lambda: None
    baseline = _ask(TestClient(app))
    broken_parts: dict[str, dict[str, Any]] = {
        "the insert raises": {"timeline": _TimelineThatCannotWrite(world.timeline)},
        "no transaction can open": {"db": _NoDatabase()},
        "not a store at all": {"timeline": _NotAStore()},
    }
    _serve(world, **broken_parts[breakage])
    broken = _ask(TestClient(app))
    assert (broken.status_code, broken.content) == (200, baseline.content)


def test_the_response_is_byte_identical_with_and_without_the_timeline(world: _World) -> None:
    """The owner's standing constraint: the success path is production
    behaviour, and writing the entry changes nothing a client receives."""
    _serve(world)
    app.dependency_overrides[get_timeline_store] = lambda: None
    app.dependency_overrides[get_timeline_database] = lambda: None
    without = _ask(TestClient(app))
    _serve(world)
    with_entry = _ask(TestClient(app))
    assert len(world.entries()) == 1, "the second turn wrote its entry, or this compares nothing"
    assert (with_entry.status_code, with_entry.headers.get("content-type"), with_entry.content) == (
        without.status_code, without.headers.get("content-type"), without.content
    )


def test_a_turn_past_one_of_the_contracts_bounds_writes_no_entry_and_is_served_adapted(world: _World) -> None:
    """`/chat`'s own models bound nothing the contract bounds. A turn past
    `MAX_SOURCES` writes no entry — the refusal is caught like any other —
    and the timeline shows it adapted from its rows."""
    many = [Source(book="phb", snippet=f"snippet {n}") for n in range(MAX_SOURCES + 1)]
    _serve(world, _FakeService(_rich_response(sources=many)))
    live = _ask(TestClient(app))
    assert live.status_code == 200 and len(live.json()["sources"]) == MAX_SOURCES + 1
    assert world.entries() == []
    [item] = _timeline(TestClient(app))
    assert item["entry_id"].isdecimal() and item["answer"]["answerable"] is None


# ── The connection budget: nothing before the answer, nothing held across it ─


class _Ledger:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.open = 0


class _LedgerDatabase:
    """The twin's database, noting when a timeline transaction opens and ends."""

    def __init__(self, inner: InMemoryDatabase, ledger: _Ledger) -> None:
        self._inner, self._ledger = inner, ledger

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self._ledger.events.append("timeline transaction opens")
        self._ledger.open += 1
        try:
            with self._inner.transaction() as unit:
                yield unit
        finally:
            self._ledger.open -= 1
            self._ledger.events.append("timeline transaction ends")


class _LedgerMessages(InMemoryMessageStore):
    ledger: _Ledger

    def append(self, conversation_id: str, mode: str, role: str, content: str,
               suggestions: list[dict[str, Any]] | None = None) -> int | None:
        self.ledger.events.append(f"{role} row")
        return super().append(conversation_id, mode, role, content, suggestions=suggestions)


def test_the_entry_is_written_after_the_answer_and_both_rows_and_nothing_is_held_across_it() -> None:
    """Requirement 8's budget, as behaviour: one more short transaction per
    turn, opened only once the answer exists and both rows are written, and
    none open while the provider is being asked."""
    ledger = _Ledger()
    messages = _LedgerMessages()
    messages.ledger = ledger
    world = _World(messages)

    def the_provider_is_asked() -> None:
        assert ledger.open == 0, "a timeline transaction is open across the provider call"
        ledger.events.append("answer")

    try:
        _serve(world, _FakeService(_rich_response(), during_answer=the_provider_is_asked),
               db=_LedgerDatabase(world.db, ledger))
        assert _ask(TestClient(app)).status_code == 200
    finally:
        for dependency in (get_message_store, get_service, get_timeline_store, get_timeline_database):
            app.dependency_overrides.pop(dependency, None)
    assert ledger.events == [
        "answer", "user row", "assistant row", "timeline transaction opens", "timeline transaction ends",
    ]
    assert len(world.entries()) == 1


def _refusal(world: _World, failure: str) -> dict[str, str]:
    """Arrange for `/chat` to end without an answer, one way per `failure`, and
    answer what the request must change to meet it."""
    raises: BaseException | None = {
        "the provider fails": openai.OpenAIError("provider down"),
        "retrieval fails": psycopg.OperationalError("db down"),
        "a bug in the handler": RuntimeError("bug"),
    }.get(failure)
    if raises is not None:
        _serve(world, _FakeService(_rich_response(), raises=raises))
        return {}
    if failure == "the GM channel without the dm role":
        app.dependency_overrides[require_session] = lambda: SessionData(user_id=1, role="player")
        return {"mode": "gm"}
    if failure == "the conversation is bound to another model":
        return {"model_preference": DEFAULT_ALIAS}
    return {"model_preference": "no-such-model"}


@pytest.mark.parametrize("failure", [
    "the provider fails", "retrieval fails", "a bug in the handler", "the GM channel without the dm role",
    "the conversation is bound to another model", "a model nobody offers is asked for",
])
def test_the_entry_write_is_never_reached_on_a_path_that_produced_no_answer(
    world: _World, client: TestClient, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    reached: list[str] = []
    monkeypatch.setattr(app_module, "_record_timeline_entry", lambda *a, **k: reached.append("reached"))
    assert _ask(client, prompt="a control turn").status_code == 200
    assert reached == ["reached"], "the spy is not wired, so the assertion below would prove nothing"
    reached.clear()
    changes = _refusal(world, failure)
    try:
        refused = _ask(TestClient(app), **changes)
    finally:
        app.dependency_overrides[require_session] = lambda: SessionData(user_id=1, role="dm")
    assert refused.status_code >= 400
    assert reached == []


# ── C7: the turn's text reaches no log line and no metric ────────────────────


class _RecordingSink:
    def __init__(self) -> None:
        self.points: list[Any] = []

    def record(self, point: Any) -> None:
        self.points.append(point)


@pytest.mark.parametrize("entry_write", ["succeeds", "fails with the text in its message"])
def test_no_log_line_and_no_metric_carries_the_turns_text(
    world: _World, caplog: pytest.LogCaptureFixture, entry_write: str
) -> None:
    fails = entry_write != "succeeds"
    world.messages.append_attachment(CONVERSATION, "notes.txt", "text/plain", f"notes {ATTACHMENT_CANARY}")
    answer = _rich_response(
        answer=f"The answer names {CANARY}.",
        sources=[Source(book="mm-5e", snippet=f"a snippet naming {CANARY}")],
    )
    _serve(world, _FakeService(answer), timeline=_TimelineThatCannotWrite(world.timeline) if fails else None)
    sink = _RecordingSink()
    app.state.metrics_sink = sink
    try:
        with caplog.at_level(logging.DEBUG):
            assert _ask(TestClient(app), prompt=f"Tell me about {CANARY}").status_code == 200
    finally:
        del app.state.metrics_sink
    assert sink.points, "no metric was recorded, so the metric half proves nothing"
    for record in caplog.records:
        told = [record.getMessage(), *(str(a) for a in (record.args or ()))]
        if record.exc_info:
            told.append("".join(traceback.format_exception(*record.exc_info)))
        assert not any(CANARY in text or ATTACHMENT_CANARY in text for text in told), record.name
    for point in sink.points:
        dumped = json.dumps(point.model_dump(mode="json"))
        assert CANARY not in dumped and ATTACHMENT_CANARY not in dumped
    ours = [r for r in caplog.records if "timeline entry" in r.getMessage()]
    if fails:
        assert len(ours) == 1 and "RuntimeError" in ours[0].getMessage() and ours[0].exc_info is None
        assert world.entries() == []
    else:
        assert ours == []
        [row] = world.entries()
        assert CANARY in json.dumps(row.payload), "the turn's own text is stored — it is the database's to hold"
        assert ATTACHMENT_CANARY not in json.dumps(row.payload), "attachment text reached an entry"


# ── The one change to chat(), pinned ─────────────────────────────────────────


def _offset(source: str, lineno: int, col: int) -> int:
    """`source`'s absolute character offset for one `ast` position — `lineno`
    1-indexed and `col` 0-indexed, exactly as `ast` reports both."""
    return sum(len(line) for line in source.splitlines(keepends=True)[:lineno - 1]) + col


def _span(source: str, node: ast.expr | ast.stmt | ast.arg) -> tuple[int, int]:
    """The half-open `(start, end)` character span `node` covers in `source`.
    Typed as the union of node kinds this file actually passes in — plain
    `ast.AST` has no position fields in typeshed, only its stmt/expr/arg
    subclasses do."""
    assert node.end_lineno is not None and node.end_col_offset is not None
    return (
        _offset(source, node.lineno, node.col_offset),
        _offset(source, node.end_lineno, node.end_col_offset),
    )


def test_chat_gains_two_dependencies_and_one_call_and_nothing_else() -> None:
    """Requirement 8: two dependency parameters, one call — placed after the
    two `_persist_turn` calls, whose ids it passes on, and before `return
    resp`, inside the `try:` — and no other mention of the timeline. A diff
    can be read once; this fails the day someone moves the call.

    F2 (PR #94 review): the `ast.Name` check below (`mentions`) only catches a
    *new reference to the `timeline`/`tdb` parameters*. Mutant W6 added a call
    to `get_timeline_store()`/`get_timeline_database()` — the dependency
    *providers*, a different identifier — before `svc.answer`, and it
    survived. The substring scan at the end of this test is independent of
    identifiers: every place the text "timeline" (any case) appears anywhere
    in this function's source must fall inside the two dependency parameters
    or the one helper call."""
    source = textwrap.dedent(inspect.getsource(app_module.chat))
    tree = ast.parse(source)
    handler = tree.body[0]
    assert isinstance(handler, ast.FunctionDef)
    assert [a.arg for a in handler.args.args] == [
        "req", "request", "svc", "store", "metrics", "session", "timeline", "tdb",
    ]
    [attempt] = [node for node in handler.body if isinstance(node, ast.Try)]
    *_, user_row, assistant_row, record, answer = attempt.body
    for statement, target, role in ((user_row, "user_message_id", "user"),
                                    (assistant_row, "assistant_message_id", "assistant")):
        assert isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call)
        assert ast.unparse(statement.targets[0]) == target
        assert ast.unparse(statement.value.func) == "_persist_turn" and f"'{role}'" in ast.unparse(statement.value)
    assert isinstance(record, ast.Expr) and isinstance(record.value, ast.Call)
    assert ast.unparse(record.value.func) == "_record_timeline_entry"
    assert {"timeline", "tdb", "user_message_id", "assistant_message_id", "resp"} <= {
        n.id for n in ast.walk(record) if isinstance(n, ast.Name)
    }
    assert isinstance(answer, ast.Return) and ast.unparse(answer) == "return resp"
    mentions = [n for n in ast.walk(handler) if isinstance(n, ast.Name) and n.id in ("timeline", "tdb")]
    assert len(mentions) == 2 and all(n in list(ast.walk(record)) for n in mentions), (
        "the timeline is named somewhere other than the one call"
    )

    # F2: no other TEXT names it either (mutant W6). `timeline`/`tdb` are the
    # last two parameters and share no other tokens with the rest of the
    # signature, so their combined span — name through default, for both —
    # runs from the first parameter's start to the second's default's end.
    timeline_param, tdb_param = handler.args.args[-2], handler.args.args[-1]
    assert (timeline_param.arg, tdb_param.arg) == ("timeline", "tdb")
    allowed = [
        (_span(source, timeline_param)[0], _span(source, handler.args.defaults[-1])[1]),
        _span(source, record),
    ]
    lowered = source.lower()
    positions = []
    cursor = lowered.find("timeline")
    while cursor != -1:
        positions.append(cursor)
        cursor = lowered.find("timeline", cursor + 1)
    assert positions, "sanity: chat()'s source should name the timeline at least once"
    stray = [
        pos for pos in positions
        if not any(lo <= pos and pos + len("timeline") <= hi for lo, hi in allowed)
    ]
    assert not stray, (
        f"'timeline' appears outside the two dependency parameters and the one "
        f"helper call, at character offset(s) {stray}"
    )

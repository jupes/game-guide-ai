"""The cost ledger on a live `/chat` turn (agent-forge-harness-yje.5.1.2).

Every test here drives a real `TestClient` request against a real `RagService`
(slice a's retriever fake, whose `embed` calls the REAL `embed_query`, and a
fake LLM client), with a twin ledger installed the way `_build_stores` installs
the real one — as the module's sink, through `monkeypatch`, so it is restored
after every test. `chat()` itself is untouched by this slice; these tests are
what shows it did not need to be.

The questions, in order: what a turn writes; that it is written once, after the
last provider call, with nothing held open across any provider call; that a
turn nobody may take writes nothing; that no ledger failure of any kind can
change what the user gets back; that the log line and the row are independent
sinks; and that no private text reaches a row.

Every assertion about what was written is made from the test body, on lists
collected outside the callbacks that fill them, and asserts the count first.

Run from repo root:
    uv run --frozen --no-sync python -m pytest service/tests/test_usage_ledger_chat.py -q
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import openai
import psycopg
import pytest

from service import generate as generate_module
from service import usage_capture
from service.app import app, get_message_store, get_service, require_session
from service.db import Database, InMemoryDatabase, PoolSettings
from service.history import InMemoryMessageStore
from service.rag import RagService
from service.session import SessionData
from service.tests.test_usage_capture_chat import _MODE_SCRIPTS, _client, _post, _reset_service_overrides
from service.tests.test_usage_capture_graph import (
    _NO_MARKERS_ANSWER,
    _SUGG_JSON,
    _EmbeddingRetriever,
    _FakeEmbeddingsClient,
    _result,
    _ScriptedLLM,
    _SeqLLM,
    _service,
)
from service.usage_capture import AttemptRow
from service.usage_ledger import (
    SEED_PRICE_REVISIONS,
    InMemoryUsageLedgerStore,
    LedgerWriter,
    PostgresUsageLedgerStore,
    StoredAttempt,
)

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
#: After the seed's effective_from, so a generation row is priced by it.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    _reset_service_overrides()
    app.dependency_overrides.pop(require_session, None)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_capture, "ledger_clock", lambda: NOW)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generate_module, "_RETRY_BACKOFF_SECONDS", 0.0)


# ---------------------------------------------------------------------------
# The twin ledger, and an event list that says when each thing happened
# ---------------------------------------------------------------------------

class _Ledger:
    """The installed sink's world. `events` interleaves the provider calls
    (each with how many ledger transactions were open during it) and the
    ledger transactions' openings and endings."""

    def __init__(self) -> None:
        self.inner = InMemoryDatabase()
        self.store = InMemoryUsageLedgerStore(self.inner)
        self.events: list[Any] = []
        self.open = 0
        self.writes: list[list[AttemptRow]] = []

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self.events.append("ledger transaction opens")
        self.open += 1
        try:
            with self.inner.transaction() as unit:
                yield unit
        finally:
            self.open -= 1
            self.events.append("ledger transaction ends")

    def provider_called(self) -> None:
        self.events.append(("provider call", self.open))

    def rows(self) -> list[StoredAttempt]:
        """Everything stored, turn by turn, in attempt order."""
        stored: list[StoredAttempt] = []
        seen: list[str] = []
        for batch in self.writes:
            for row in batch:
                if row.operation_id not in seen:
                    seen.append(row.operation_id)
        with self.inner.transaction() as unit:
            for operation_id in seen:
                stored.extend(self.store.attempts_for_operation(unit, operation_id))
        return stored


class _Writer(LedgerWriter):
    """The real writer, keeping a copy of what it was handed."""

    def __init__(self, ledger: _Ledger) -> None:
        super().__init__(ledger.store, ledger)
        self._ledger = ledger

    def write(self, rows: Sequence[AttemptRow]) -> int:
        self._ledger.writes.append(list(rows))
        return super().write(rows)


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> _Ledger:
    world = _Ledger()
    monkeypatch.setattr(usage_capture, "_LEDGER", _Writer(world))
    return world


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Slice a's log records, collected through its own seam."""
    collected: list[dict[str, Any]] = []
    monkeypatch.setattr(
        usage_capture, "_emit_record", lambda operation, fields: collected.append(dict(fields)),
    )
    return collected


class _WatchedLLM:
    def __init__(self, inner: Any, ledger: _Ledger) -> None:
        self._inner, self._ledger = inner, ledger

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:  # noqa: A002 - LLMClient's name
        self._ledger.provider_called()
        return self._inner.invoke(input, config=config, **kwargs)


class _WatchedEmbeddings(_FakeEmbeddingsClient):
    def __init__(self, ledger: _Ledger) -> None:
        super().__init__(prompt_tokens=9)
        self._ledger = ledger

    def create(self, model: Any, input: Any) -> Any:  # noqa: A002 - the SDK's parameter name
        self._ledger.provider_called()
        return super().create(model, input)


def _watched(llm: Any, ledger: _Ledger) -> RagService:
    return _service(_WatchedLLM(llm, ledger), embed_client=_WatchedEmbeddings(ledger))


def _rate_limited() -> openai.RateLimitError:
    response = httpx.Response(429, headers={"retry-after": "17"}, request=_REQUEST)
    return openai.RateLimitError("rate limited", response=response, body=None)


# ---------------------------------------------------------------------------
# What a turn writes
# ---------------------------------------------------------------------------

def test_a_spell_turn_writes_one_row_per_attempt_under_one_operation(ledger: _Ledger) -> None:
    response = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS["spell"]))))

    assert response.status_code == 200, response.text
    rows = ledger.rows()
    assert len(rows) == 4
    assert [(r.attempt_index, r.purpose, r.retry_index) for r in rows] == [
        (0, "embedding", 0), (1, "answer", 0), (2, "suggestions", 0), (3, "spell_structuring", 0),
    ]
    assert len({r.operation_id for r in rows}) == 1
    assert {(r.operation, r.mode, r.billed_account_id, r.actor_kind, r.campaign_id) for r in rows} == {
        ("chat_turn", "spell", 1, "account", None),
    }
    assert {r.occurred_at for r in rows} == {NOW}
    [seed] = SEED_PRICE_REVISIONS
    assert [r.price_revision_id for r in rows] == [None, seed.id, seed.id, seed.id], (
        "generation rows carry the seed's revision; nothing prices the embedding yet"
    )


def test_a_flaky_answer_advances_the_attempt_index_across_its_retries(ledger: _Ledger, no_backoff: None) -> None:
    llm = _ScriptedLLM([_rate_limited(), _rate_limited(), _NO_MARKERS_ANSWER])

    response = _post(_client(_service(llm)), mode="rules")

    assert response.status_code == 200, response.text
    rows = ledger.rows()
    assert [(r.attempt_index, r.purpose, r.retry_index, r.status) for r in rows] == [
        (0, "embedding", 0, "ok"), (1, "answer", 0, "error"), (2, "answer", 1, "error"), (3, "answer", 2, "ok"),
    ]


def test_one_ledger_transaction_per_turn_after_the_last_provider_call(ledger: _Ledger) -> None:
    """The budget, as behaviour: the turn's rows are written once, after its
    last provider call, and no ledger transaction is open during any of them."""
    response = _post(_client(_watched(_SeqLLM(_MODE_SCRIPTS["spell"]), ledger)))

    assert response.status_code == 200, response.text
    assert ledger.events == [
        ("provider call", 0), ("provider call", 0), ("provider call", 0), ("provider call", 0),
        "ledger transaction opens", "ledger transaction ends",
    ]
    assert [len(batch) for batch in ledger.writes] == [4]


def test_a_turn_refused_by_a_gate_writes_nothing_and_opens_nothing(ledger: _Ledger) -> None:
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=1, role="player")

    response = _post(_client(_watched(_SeqLLM(_MODE_SCRIPTS["gm"]), ledger)), mode="gm")

    assert response.status_code == 403
    assert ledger.events == []
    assert ledger.writes == []


def test_a_turn_with_no_embedding_key_writes_nothing_and_opens_nothing(
    ledger: _Ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing key raises before any request is sent: not an attempt."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    retriever = _EmbeddingRetriever(_result(), embed_client=None)
    svc = RagService(retriever=retriever, llm_client=_WatchedLLM(_SeqLLM(["unused"]), ledger))

    response = _post(_client(svc), mode="rules")

    assert response.status_code == 503, response.text
    assert retriever.calls == 1, "the embedding really was attempted and refused"
    assert ledger.events == []
    assert ledger.writes == []


def test_a_provider_rate_limit_is_still_a_429_and_its_attempts_are_written(
    ledger: _Ledger, records: list[dict[str, Any]], no_backoff: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _post(_client(_service(_ScriptedLLM([_rate_limited()]))), mode="sage")
    _reset_service_overrides()
    monkeypatch.setattr(usage_capture, "_LEDGER", None)
    control = _post(_client(_service(_ScriptedLLM([_rate_limited()]))), mode="sage")

    assert captured.status_code == control.status_code == 429
    assert captured.content == control.content
    assert captured.headers.get("retry-after") == control.headers.get("retry-after") == "17"
    rows = ledger.rows()
    first_turn = [r for r in records if r["operation_id"] == rows[0].operation_id] if rows else []
    assert len(rows) >= 2, "the embedding and at least one answer attempt"
    assert [(r.purpose, r.retry_index, r.status) for r in rows] == [
        (r["purpose"], r["retry_index"], r["status"]) for r in first_turn
    ], "one row per attempt the log line witnessed"
    assert rows[-1].status == "error"


# ---------------------------------------------------------------------------
# Isolation: no ledger failure changes a status, a body or a header
# ---------------------------------------------------------------------------

class _BrokenStore(InMemoryUsageLedgerStore):
    def record_attempts(self, unit: Any, rows: Sequence[AttemptRow]) -> int:
        raise RuntimeError("INSERT ... VALUES ('zzq-secret')")


class _BrokenDatabase:
    def transaction(self) -> Any:
        raise psycopg.OperationalError("connection to 10.0.0.1 refused zzq-secret")


@contextmanager
def _failing_sink(failure: str) -> Iterator[tuple[Any, str]]:
    """A sink whose one write fails the named way, and the class it fails with."""
    if failure == "the store raises":
        twin = InMemoryDatabase()
        yield LedgerWriter(_BrokenStore(twin), twin), "RuntimeError"
    elif failure == "the gate times out":
        gated = Database(
            "postgresql://ledger@127.0.0.1:9/nowhere", PoolSettings(sync_max=1, async_max=0, acquire_timeout_s=1),
        )
        gate = gated._gate
        assert gate is not None and gate.acquire(timeout=1), "the gate's one slot is taken"
        try:
            yield LedgerWriter(PostgresUsageLedgerStore(), gated), "PoolTimeout"
        finally:
            gate.release()
    else:
        yield LedgerWriter(InMemoryUsageLedgerStore(InMemoryDatabase()), _BrokenDatabase()), "OperationalError"


FAILURES = ["the store raises", "the gate times out", "db.transaction raises"]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize(("mode", "field", "rows"), [("spell", "spell_content", 4), ("gm", "stat_block", 3)])
def test_a_failing_ledger_changes_nothing_the_user_gets(
    mode: str, field: str, rows: int, failure: str, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(usage_capture, "_LEDGER", None)
    control = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS[mode]))), mode=mode)
    _reset_service_overrides()
    assert control.status_code == 200, control.text
    assert control.json()[field] is not None

    caplog.set_level(logging.WARNING)
    caplog.clear()
    with _failing_sink(failure) as (sink, error):
        monkeypatch.setattr(usage_capture, "_LEDGER", sink)
        broken = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS[mode]))), mode=mode)

    assert broken.status_code == control.status_code
    assert broken.content == control.content
    assert dict(broken.headers) == dict(control.headers)
    assert [r.getMessage() for r in _warnings(caplog)] == [
        f"usage ledger write failed (rows={rows}, error={error})",
    ]


@pytest.mark.parametrize("failure", FAILURES)
def test_a_failing_ledger_leaves_a_429_exactly_as_it_was(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, no_backoff: None,
) -> None:
    monkeypatch.setattr(usage_capture, "_LEDGER", None)
    control = _post(_client(_service(_ScriptedLLM([_rate_limited()]))), mode="sage")
    _reset_service_overrides()
    assert control.status_code == 429

    caplog.clear()
    with _failing_sink(failure) as (sink, error):
        monkeypatch.setattr(usage_capture, "_LEDGER", sink)
        broken = _post(_client(_service(_ScriptedLLM([_rate_limited()]))), mode="sage")

    assert broken.status_code == 429
    assert broken.content == control.content
    assert broken.headers.get("retry-after") == control.headers.get("retry-after") == "17"
    ledger_warnings = [r.getMessage() for r in _warnings(caplog) if "ledger" in r.getMessage()]
    assert len(ledger_warnings) == 1
    assert ledger_warnings[0].endswith(f"error={error})")
    for text in ("zzq-secret", "10.0.0.1", "rate limited"):
        assert text not in ledger_warnings[0]


# ---------------------------------------------------------------------------
# Two independent sinks
# ---------------------------------------------------------------------------

def test_a_raising_emitter_still_leaves_the_rows(ledger: _Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def broken(operation: Any, fields: Any) -> None:
        calls.append(1)
        raise RuntimeError("the emitter is broken")

    monkeypatch.setattr(usage_capture, "_emit_record", broken)
    response = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS["spell"]))))

    assert response.status_code == 200, response.text
    assert len(calls) == 4, "the emitter really was called, and raised, for every attempt"
    assert [r.purpose for r in ledger.rows()] == ["embedding", "answer", "suggestions", "spell_structuring"]


def test_a_raising_ledger_still_lets_every_log_line_out(
    records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _failing_sink("the store raises") as (sink, _):
        monkeypatch.setattr(usage_capture, "_LEDGER", sink)
        response = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS["spell"]))))

    assert response.status_code == 200, response.text
    assert [r["purpose"] for r in records] == ["embedding", "answer", "suggestions", "spell_structuring"]
    assert {frozenset(r) for r in records} == {usage_capture.EXPECTED_KEYS}


# ---------------------------------------------------------------------------
# X-7: no private text reaches a row
# ---------------------------------------------------------------------------

def test_no_prompt_answer_filename_attachment_or_email_reaches_any_row(ledger: _Ledger) -> None:
    unique_prompt = "zzqprompt-5d1c what does my homebrew file say"
    unique_answer = "zzqanswer-0b7e the orb hums with malice [1]."
    unique_filename = "zzqfilename-73aa.txt"
    unique_attachment = "zzqattachment-19fe the vault lies north"
    email = "player@example.com"
    store = InMemoryMessageStore()
    store.append_attachment("c-fixed", unique_filename, "text/plain", unique_attachment)
    llm = _SeqLLM([unique_answer, _SUGG_JSON, _MODE_SCRIPTS["spell"][2]])

    response = _post(_client(_service(llm), store=store), prompt=f"{unique_prompt} {email}")

    assert response.status_code == 200, response.text
    rows = ledger.rows()
    assert len(rows) == 4, "rows must exist before a privacy assertion means anything"
    for row in rows:
        values = dataclasses.asdict(row).values()
        for value in values:
            text = str(value)
            for secret in (unique_prompt, unique_answer, unique_filename, unique_attachment, email):
                assert secret not in text
            assert "@" not in text


def test_the_message_store_override_is_reset_between_tests() -> None:
    """Sanity for the fixtures above: nothing this module installs outlives it."""
    assert get_service not in app.dependency_overrides
    assert get_message_store not in app.dependency_overrides
    assert usage_capture._LEDGER is None

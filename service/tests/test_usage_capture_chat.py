"""
Usage capture on a live `/chat` turn (agent-forge-harness-yje.5.1.1).

This file answers the question the whole bead turns on: can any of this change
what a user gets back? Every test here drives a real `TestClient` request
against a real `RagService` (fake retriever, fake LLM client — B16: no database
needed when both seams are injected), because the things that can go wrong —
the trace header five frames down, an emitter that raises inside
`generate_result`'s except branch, an observer built inside `suggest_node`'s
try — are invisible from a unit test.

The control run ("with capture absent") is produced by monkeypatching the two
module functions, and by nothing else. There is deliberately no feature flag: a
conditional in `chat()` is a flag wearing a different hat, and a flag is one
more thing that can be off in production while looking on.

Run from repo root:
    uv run --frozen --no-sync python -m pytest service/tests/test_usage_capture_chat.py -q
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from service import usage_capture
from service.app import app, get_message_store, get_service, require_session
from service.history import InMemoryMessageStore
from service.session import SessionData
from service.tests.test_usage_capture_graph import (
    _SPELL_ANSWER,
    _SPELL_JSON,
    _STATBLOCK_ANSWER,
    _STATBLOCK_JSON,
    _SUGG_JSON,
    _FakeEmbeddingsClient,
    _ScriptedLLM,
    _SeqLLM,
    _service,
)

_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    _reset_service_overrides()
    app.dependency_overrides.pop(require_session, None)


def _reset_service_overrides() -> None:
    """Drop only the overrides this module installs. A blanket
    `dependency_overrides.clear()` would also drop conftest's authenticated
    session, and the real `require_session` answers 503 with no auth backend —
    which is how a control run quietly stops being the same request."""
    app.dependency_overrides.pop(get_service, None)
    app.dependency_overrides.pop(get_message_store, None)


@pytest.fixture
def records(monkeypatch):
    """Collected outside the emitter; asserted on from the test body."""
    collected: list[dict] = []
    monkeypatch.setattr(
        usage_capture, "_emit_record", lambda operation, fields: collected.append(dict(fields)),
    )
    return collected


def _client(svc, store=None) -> TestClient:
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_message_store] = lambda: store or InMemoryMessageStore()
    return TestClient(app)


def _post(client, *, prompt="What does Fireball do?", mode="spell",
          conversation_id="c-fixed", headers=None):
    return client.post(
        "/chat",
        json={"prompt": prompt, "mode": mode, "conversation_id": conversation_id},
        headers=headers or {},
    )


_MODE_SCRIPTS = {
    "spell": [_SPELL_ANSWER, _SUGG_JSON, _SPELL_JSON],
    "sage": [_STATBLOCK_ANSWER, _STATBLOCK_JSON],
    "gm": [_STATBLOCK_ANSWER, _STATBLOCK_JSON],
    "rules": ["The rules say you may dash [1]."],
}


# ---------------------------------------------------------------------------
# AC 6 — the operation, the billed account, the actor kind, the campaign key
# ---------------------------------------------------------------------------

def test_every_record_of_a_turn_shares_one_operation_and_carries_the_account(records):
    client = _client(_service(_SeqLLM(_MODE_SCRIPTS["spell"])))

    response = _post(client)

    assert response.status_code == 200, response.text
    assert [r["purpose"] for r in records] == [
        "embedding", "answer", "suggestions", "spell_structuring",
    ]
    assert len({r["operation_id"] for r in records}) == 1
    assert {r["operation"] for r in records} == {"chat_turn"}
    assert {r["mode"] for r in records} == {"spell"}
    assert {r["billed_account_id"] for r in records} == {1}  # the conftest session
    assert {r["actor_kind"] for r in records} == {"account"}
    assert {r["campaign_id"] for r in records} == {None}


@pytest.mark.parametrize(
    ("user_id", "mode"),
    [(7, "rules"), (13, "sage")],
)
def test_the_account_and_the_mode_are_read_from_the_request_not_defaulted(
    records, user_id, mode,
):
    """Rework 1 / review F1. `billed_account_id` is the attribution field and
    `mode` is the grouping dimension of 'cost by mode per day' — the only
    numbers this bead delivers — but every other test in this file runs at the
    fixture defaults (conftest `user_id=1`, spell), so hard-coding either one
    survived the whole suite. These two rows are deliberately off BOTH defaults
    and disagree with each other, so no single constant can satisfy them:
    `billed_account_id=1` dies on 7, `mode="spell"` dies on rules, and a mutant
    pinned to either row's value dies on the other."""
    app.dependency_overrides[require_session] = lambda: SessionData(
        user_id=user_id, role="dm",
    )
    client = _client(_service(_SeqLLM(_MODE_SCRIPTS[mode])))

    response = _post(client, mode=mode)

    assert response.status_code == 200, response.text
    assert records, "a turn that records nothing cannot pin these fields"
    for record in records:
        assert record["billed_account_id"] == user_id
        assert record["mode"] == mode


def test_two_turns_get_two_different_operation_ids(records):
    client = _client(_service(_SeqLLM(_MODE_SCRIPTS["rules"] * 2)))

    assert _post(client, mode="rules", conversation_id="c1").status_code == 200
    assert _post(client, mode="rules", conversation_id="c2").status_code == 200

    ids = {r["operation_id"] for r in records}
    assert len(records) == 4
    assert len(ids) == 2


# ---------------------------------------------------------------------------
# AC 7 — the key set is closed, and no private content can reach a record
# ---------------------------------------------------------------------------

def test_no_prompt_answer_filename_or_attachment_text_reaches_any_record(records):
    unique_prompt = "zzqprompt-6f2a what does my homebrew file say"
    unique_answer = "zzqanswer-91bd the orb hums with malice [1]."
    unique_filename = "zzqfilename-4c7e.txt"
    unique_attachment = "zzqattachment-88ee the vault lies north"
    email = "player@example.com"

    store = InMemoryMessageStore()
    store.append_attachment("c-fixed", unique_filename, "text/plain", unique_attachment)
    client = _client(_service(_SeqLLM([unique_answer, _SUGG_JSON, _SPELL_JSON])), store=store)

    response = _post(client, prompt=f"{unique_prompt} {email}")

    assert response.status_code == 200, response.text
    assert len(records) == 4, "records must exist before a privacy assertion means anything"
    for record in records:
        assert set(record) == usage_capture.EXPECTED_KEYS
        blob = json.dumps(record)
        for secret in (unique_prompt, unique_answer, unique_filename, unique_attachment, email):
            assert secret not in blob
        for value in record.values():
            assert not (isinstance(value, str) and "@" in value)


# ---------------------------------------------------------------------------
# AC 8 — byte-identical responses, capture active vs capture absent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["spell", "sage", "gm", "rules"])
def test_the_response_is_byte_identical_with_and_without_capture(mode, monkeypatch, records):
    captured = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS[mode]))), mode=mode)
    _reset_service_overrides()
    assert len(records) >= 2, "the captured run must actually have recorded something"

    # The control: the only difference is that the two module functions do
    # nothing. No flag, no conditional in chat().
    monkeypatch.setattr(usage_capture, "begin_operation", lambda **kw: None)
    monkeypatch.setattr(usage_capture, "end_operation", lambda token: None)
    before = len(records)
    control = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS[mode]))), mode=mode)

    assert len(records) == before, "the control run must record nothing"
    assert captured.status_code == control.status_code
    assert captured.content == control.content
    assert dict(captured.headers) == dict(control.headers)


# ---------------------------------------------------------------------------
# AC 9 — an emitter failure never changes a turn's status, body or CONTENT
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("mode", "field"),
    [("spell", "spell_content"), ("gm", "stat_block")],
)
def test_a_raising_emitter_leaves_the_status_body_and_content_untouched(
    mode, field, monkeypatch, caplog,
):
    control = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS[mode]))), mode=mode)
    _reset_service_overrides()
    assert control.status_code == 200, control.text
    assert control.json()[field] is not None

    calls: list[int] = []

    def _broken(operation, fields):
        calls.append(1)
        raise RuntimeError("the emitter is broken")

    monkeypatch.setattr(usage_capture, "_emit_record", _broken)
    caplog.set_level("WARNING", logger="service.usage_capture")
    broken = _post(_client(_service(_SeqLLM(_MODE_SCRIPTS[mode]))), mode=mode)

    assert calls, "the emitter must actually have been called and raised"
    assert broken.status_code == control.status_code
    assert broken.content == control.content
    assert dict(broken.headers) == dict(control.headers)
    body = broken.json()
    assert body[field] is not None
    if mode == "spell":
        assert body["suggestions"] is not None
    warnings = [r for r in caplog.records if r.name == "service.usage_capture"]
    assert len(warnings) == len(calls)
    for warning in warnings:
        message = warning.getMessage()
        assert "RuntimeError" in message
        assert "the emitter is broken" not in message
        assert "Fireball" not in message and "Goblin" not in message


def test_a_raising_emitter_does_not_turn_a_429_into_a_500(monkeypatch):
    """`generate_result` records inside `except BaseException` before
    re-raising. An observer that raised there would replace the provider's
    exception and rewrite the whole error taxonomy."""
    def _rate_limited():
        response = httpx.Response(429, headers={"retry-after": "17"}, request=_REQUEST)
        return openai.RateLimitError("rate limited", response=response, body=None)

    monkeypatch.setattr("service.generate._RETRY_BACKOFF_SECONDS", 0.0)
    control = _post(_client(_service(_ScriptedLLM([_rate_limited()]))), mode="sage")
    _reset_service_overrides()
    assert control.status_code == 429, control.text

    def _broken(operation, fields):
        raise RuntimeError("the emitter is broken")

    monkeypatch.setattr(usage_capture, "_emit_record", _broken)
    broken = _post(_client(_service(_ScriptedLLM([_rate_limited()]))), mode="sage")

    assert broken.status_code == 429
    assert broken.content == control.content
    assert broken.headers.get("retry-after") == control.headers.get("retry-after") == "17"


# ---------------------------------------------------------------------------
# AC 10 — a turn refused by a gate makes no provider call and records nothing
# ---------------------------------------------------------------------------

def test_a_turn_refused_by_the_role_gate_records_nothing(records):
    """The operation is created AFTER every gate, so a 403 has nothing to
    record. The positive control is every other test in this file."""
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=1, role="player")
    client = _client(_service(_SeqLLM(_MODE_SCRIPTS["gm"])))

    response = _post(client, mode="gm")

    assert response.status_code == 403
    assert records == []


# ---------------------------------------------------------------------------
# AC 11 — the trace joins on a live turn, and only when a trace id is present
# ---------------------------------------------------------------------------

def test_a_live_turn_with_a_trace_header_emits_the_trace_key_on_every_line(monkeypatch, capsys):
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    client = _client(_service(_SeqLLM(_MODE_SCRIPTS["spell"])))

    response = _post(client, headers={"X-Cloud-Trace-Context": f"{_TRACE_ID}/1;o=1"})

    assert response.status_code == 200, response.text
    entries = _provider_attempt_lines(capsys)
    assert len(entries) == 4
    for entry in entries:
        assert entry["logging.googleapis.com/trace"] == _TRACE_ID
        assert set(entry) == usage_capture.EXPECTED_KEYS | {
            "severity", "message", "logging.googleapis.com/trace",
        }


def test_the_same_turn_without_the_header_has_no_trace_key_and_the_same_key_set(
    monkeypatch, capsys,
):
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    client = _client(_service(_SeqLLM(_MODE_SCRIPTS["spell"])))

    response = _post(client)

    assert response.status_code == 200, response.text
    entries = _provider_attempt_lines(capsys)
    assert len(entries) == 4
    for entry in entries:
        assert set(entry) == usage_capture.EXPECTED_KEYS | {"severity", "message"}


def test_embedding_usage_reaches_a_live_turn_record(records):
    client = _client(_service(
        _SeqLLM(_MODE_SCRIPTS["rules"]), embed_client=_FakeEmbeddingsClient(prompt_tokens=23),
    ))

    response = _post(client, mode="rules")

    assert response.status_code == 200, response.text
    embeddings = [r for r in records if r["purpose"] == "embedding"]
    assert len(embeddings) == 1
    assert embeddings[0]["input_tokens"] == 23


def _provider_attempt_lines(capsys) -> list[dict]:
    lines = []
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        entry = json.loads(line)
        if entry.get("event") == usage_capture.EVENT:
            lines.append(entry)
    return lines

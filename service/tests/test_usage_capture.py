"""
Unit tests for service/usage_capture.py (agent-forge-harness-yje.5.1.1,
"yje.5.1 slice a — measure now").

These cover the record shape, the emission mechanism, the failure isolation and
the injected-clock latency seam in isolation. The wiring through the real
compiled graph lives in test_usage_capture_graph.py; the live `/chat` turn
lives in test_usage_capture_chat.py.

Every assertion about a record is made OUTSIDE the callback that produces it,
and asserts the record COUNT before it asserts anything about the contents — an
assertion inside a callback that may never run is a test that cannot fail.

Run from repo root:
    uv run --frozen --no-sync python -m pytest service/tests/test_usage_capture.py -q
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ingestion.retrieval import EMBED_MODEL
from service import usage_capture
from service.generate import (
    NullAttemptObserver,
    generate_result,
    generate_spell_content,
    generate_stat_block,
)

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError("rate limited", response=httpx.Response(429, request=_REQUEST), body=None)


class _StubRequest:
    """A request-like object for gcp_logging.emit (it reads `.headers` only)."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


def _operation(**overrides) -> usage_capture.Operation:
    operation = usage_capture.Operation(
        operation_id="0123456789abcdef0123456789abcdef",
        operation=usage_capture.OPERATION_CHAT_TURN,
        mode="sage",
        billed_account_id=1,
        actor_kind="account",
        campaign_id=None,
        request=_StubRequest(),
    )
    return dataclasses.replace(operation, **overrides) if overrides else operation


class _CollectingEmitter:
    """Collects the builder's field dicts. Assertions are made on `records`
    from the test body, never from inside this callback."""

    def __init__(self):
        self.records: list[dict] = []

    def __call__(self, operation, fields):
        self.records.append(dict(fields))


class _RaisingEmitter:
    def __init__(self):
        self.calls = 0

    def __call__(self, operation, fields):
        self.calls += 1
        raise RuntimeError("emitter is broken")


class _FakeClock:
    """Callable clock whose value only moves when a test moves it."""

    def __init__(self, start: float = 100.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _TimedClient:
    """Advances the fake clock by `per_call` seconds inside each invoke, so the
    recorded latency is the time spent in the provider call and nothing else."""

    def __init__(self, clock: _FakeClock, errors, final_message, per_call: float = 0.25):
        self._clock = clock
        self._errors = list(errors)
        self._final = final_message
        self._per_call = per_call
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        self._clock.advance(self._per_call)
        if self._errors:
            raise self._errors.pop(0)
        return self._final


class _BareResponse:
    """A provider response with `.content` and deliberately NO `id` and NO
    `response_metadata` — generate_result reads both with getattr defaults, and
    the structuring reroute must survive a response that lacks them."""

    def __init__(self, content: str):
        self.content = content


class _BareClient:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        return _BareResponse(self._content)


# ---------------------------------------------------------------------------
# The record: a closed key set, and nulls that stay null
# ---------------------------------------------------------------------------

def test_expected_keys_is_the_documented_closed_set():
    assert usage_capture.EXPECTED_KEYS == frozenset({
        "event", "record_version", "operation_id", "operation", "purpose", "mode",
        "alias", "provider", "retry_index", "status", "error_class", "finish_reason",
        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
        "latency_ms", "provider_request_id", "billed_account_id", "actor_kind",
        "campaign_id",
    })


def test_reserved_cloud_run_field_names_are_not_in_the_record():
    reserved = {"severity", "message", "timestamp", "time", "httpRequest"}
    assert not (usage_capture.EXPECTED_KEYS & reserved)
    assert not any(k.startswith("logging.googleapis.com/") for k in usage_capture.EXPECTED_KEYS)


def test_builder_dict_key_set_is_exactly_expected_keys_on_success_and_on_error():
    ok = usage_capture.build_record(
        operation=_operation(), purpose="answer", alias="gpt-4o-mini", retry_index=0,
        status="ok", error_class=None, finish_reason="stop",
        input_tokens=10, cached_input_tokens=2, output_tokens=5, reasoning_tokens=None,
        latency_ms=12.5, provider_request_id="chatcmpl-x",
    )
    err = usage_capture.build_record(
        operation=_operation(), purpose="answer", alias="gpt-4o-mini", retry_index=1,
        status="error", error_class="RateLimitError", finish_reason=None,
        input_tokens=None, cached_input_tokens=None, output_tokens=None, reasoning_tokens=None,
        latency_ms=1.0, provider_request_id=None,
    )
    assert set(ok) == usage_capture.EXPECTED_KEYS
    assert set(err) == usage_capture.EXPECTED_KEYS
    assert ok["event"] == "provider_attempt"
    assert ok["record_version"] == 1


def test_provider_is_the_catalog_provider_or_null_never_a_guess():
    known = usage_capture.build_record(
        operation=_operation(), purpose="answer", alias="gpt-4o-mini", retry_index=0,
        status="ok", error_class=None, finish_reason=None,
        input_tokens=None, cached_input_tokens=None, output_tokens=None, reasoning_tokens=None,
        latency_ms=0.0, provider_request_id=None,
    )
    embedding = usage_capture.build_record(
        operation=_operation(), purpose="embedding", alias=EMBED_MODEL, retry_index=0,
        status="ok", error_class=None, finish_reason=None,
        input_tokens=7, cached_input_tokens=None, output_tokens=None, reasoning_tokens=None,
        latency_ms=0.0, provider_request_id=None,
    )
    unknown = usage_capture.build_record(
        operation=_operation(), purpose="answer", alias="not-in-the-catalog", retry_index=0,
        status="ok", error_class=None, finish_reason=None,
        input_tokens=None, cached_input_tokens=None, output_tokens=None, reasoning_tokens=None,
        latency_ms=0.0, provider_request_id=None,
    )
    assert known["provider"] == "openai"
    assert embedding["provider"] == "openai"
    assert unknown["provider"] is None


def test_unknown_usage_fields_stay_none_not_zero():
    """AC 5. A provider that reported nothing is not a provider that used zero."""
    emitter = _CollectingEmitter()
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini", emit=emitter,
    )
    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_BareClient("an answer"), observer=recorder,
    )
    assert len(emitter.records) == 1
    record = emitter.records[0]
    assert record["input_tokens"] is None
    assert record["cached_input_tokens"] is None
    assert record["output_tokens"] is None
    assert record["reasoning_tokens"] is None
    assert record["status"] == "ok"


def test_reported_usage_reaches_the_record():
    emitter = _CollectingEmitter()
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini", emit=emitter,
    )
    message = AIMessage(
        content="an answer",
        usage_metadata={
            "input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
            "input_token_details": {"cache_read": 64},
            "output_token_details": {"reasoning": 8},
        },
    )
    message.id = "chatcmpl-abc"
    message.response_metadata = {"finish_reason": "stop"}

    class _Client:
        calls = 0

        def invoke(self, messages, config=None, **kw):
            return message

    generate_result([HumanMessage(content="q")], alias="gpt-4o-mini", client=_Client(), observer=recorder)
    assert len(emitter.records) == 1
    record = emitter.records[0]
    assert record["input_tokens"] == 120
    assert record["cached_input_tokens"] == 64
    assert record["output_tokens"] == 30
    assert record["reasoning_tokens"] == 8
    assert record["finish_reason"] == "stop"
    assert record["provider_request_id"] == "chatcmpl-abc"


def test_error_records_carry_the_class_name_and_never_the_message():
    emitter = _CollectingEmitter()
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini", emit=emitter,
    )

    class _Boom:
        def invoke(self, messages, config=None, **kw):
            raise ValueError("secret prompt fragment leaked into the message")

    with pytest.raises(ValueError):
        generate_result(
            [HumanMessage(content="q")], alias="gpt-4o-mini", client=_Boom(), observer=recorder,
        )
    assert len(emitter.records) == 1
    record = emitter.records[0]
    assert record["status"] == "error"
    assert record["error_class"] == "ValueError"
    assert "secret prompt fragment" not in json.dumps(record)


def test_retry_index_counts_up_per_call_site():
    emitter = _CollectingEmitter()
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini", emit=emitter,
    )
    clock = _FakeClock()
    client = _TimedClient(clock, [_rate_limit_error(), _rate_limit_error()], AIMessage(content="ok"))
    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini", client=client,
        observer=recorder, sleep=lambda seconds: None,
    )
    assert [r["retry_index"] for r in emitter.records] == [0, 1, 2]
    assert [r["status"] for r in emitter.records] == ["error", "error", "ok"]


# ---------------------------------------------------------------------------
# AC 13 — latency excludes the retry backoff, with an injected clock
# ---------------------------------------------------------------------------

def test_latency_ms_excludes_the_retry_backoff_sleep():
    """The recorder's clock is injected and the fake `sleep` advances that same
    clock by ten seconds. A recorder that stamps its start time at construction
    (instead of in attempt_started) folds the backoff into the second attempt."""
    clock = _FakeClock()
    emitter = _CollectingEmitter()
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini", now=clock, emit=emitter,
    )
    client = _TimedClient(clock, [_rate_limit_error()], AIMessage(content="ok"), per_call=0.25)

    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini", client=client,
        observer=recorder, sleep=lambda seconds: clock.advance(10.0),
    )

    assert len(emitter.records) == 2, "expected one record per attempt"
    assert client.calls == 2
    assert [r["latency_ms"] for r in emitter.records] == [250.0, 250.0]


def test_attempt_started_is_actually_called_by_generate_result():
    """The hook is a Protocol call site in generate_result; if it stops being
    called the latency assertion above would silently measure nothing."""
    calls: list[int] = []

    class _Hooked(NullAttemptObserver):
        def attempt_started(self) -> None:
            calls.append(1)

    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_BareClient("ok"), observer=_Hooked(),
    )
    assert calls == [1]


def test_null_observer_without_the_hook_is_still_accepted():
    result = generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_BareClient("ok"), observer=NullAttemptObserver(),
    )
    assert result.text == "ok"


# ---------------------------------------------------------------------------
# AC 11 — emission: one JSON line on Cloud Run, a logger line off it
# ---------------------------------------------------------------------------

def test_on_cloud_run_each_record_is_one_parseable_json_line(monkeypatch, capsys):
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini",
    )
    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_BareClient("ok"), observer=recorder,
    )
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["severity"] == "INFO"
    assert entry["message"] == "provider attempt"
    assert entry["event"] == "provider_attempt"
    assert set(entry) == usage_capture.EXPECTED_KEYS | {"severity", "message"}


def test_off_cloud_run_nothing_reaches_stdout_and_the_fallback_logger_fires(
    monkeypatch, capsys, caplog,
):
    monkeypatch.delenv("K_SERVICE", raising=False)
    caplog.set_level("INFO", logger="service.usage_capture")
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini",
    )
    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_BareClient("ok"), observer=recorder,
    )
    assert capsys.readouterr().out == ""
    fallback = [r for r in caplog.records if r.name == "service.usage_capture"]
    assert len(fallback) == 1
    assert "provider_attempt" in fallback[0].getMessage()


def test_the_trace_key_appears_when_the_request_carries_a_valid_trace_id(monkeypatch, capsys):
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    request = _StubRequest({"x-cloud-trace-context": "0af7651916cd43dd8448eb211c80319c/1;o=1"})
    recorder = usage_capture.AttemptRecorder(
        _operation(request=request), purpose="answer", alias="gpt-4o-mini",
    )
    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_BareClient("ok"), observer=recorder,
    )
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["logging.googleapis.com/trace"] == "0af7651916cd43dd8448eb211c80319c"


# ---------------------------------------------------------------------------
# Failure isolation (requirement 6) — every entry point swallows, none escapes
# ---------------------------------------------------------------------------

def test_a_raising_emitter_never_escapes_record(caplog):
    emitter = _RaisingEmitter()
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini", emit=emitter,
    )
    caplog.set_level("WARNING", logger="service.usage_capture")
    result = generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_BareClient("ok"), observer=recorder,
    )
    assert result.text == "ok"
    assert emitter.calls == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "ok" not in warnings[0].getMessage().split()


def test_a_raising_emitter_inside_the_error_branch_does_not_replace_the_error():
    """generate_result records inside `except BaseException` before re-raising —
    an observer that raises there would rewrite the whole error taxonomy."""
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini", emit=_RaisingEmitter(),
    )

    class _Boom:
        def invoke(self, messages, config=None, **kw):
            raise _rate_limit_error()

    with pytest.raises(openai.RateLimitError):
        generate_result(
            [HumanMessage(content="q")], alias="gpt-4o-mini", client=_Boom(),
            observer=recorder, sleep=lambda seconds: None,
        )


def test_a_raising_clock_never_escapes_attempt_started():
    def _broken_clock() -> float:
        raise RuntimeError("no clock today")

    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="answer", alias="gpt-4o-mini", now=_broken_clock,
        emit=_CollectingEmitter(),
    )
    result = generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_BareClient("ok"), observer=recorder,
    )
    assert result.text == "ok"


def test_begin_operation_never_raises_and_end_operation_tolerates_none(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="service.usage_capture")
    monkeypatch.setattr(usage_capture.uuid, "uuid4", _boom)
    token = usage_capture.begin_operation(
        mode="sage", billed_account_id=1, actor_kind="account",
        campaign_id=None, request=_StubRequest(),
    )
    assert token is None
    assert usage_capture.current_operation() is None
    usage_capture.end_operation(None)
    usage_capture.end_operation(object())
    assert [r.levelname for r in caplog.records].count("WARNING") >= 1


def _boom(*args, **kwargs):
    raise RuntimeError("uuid is broken")


def test_observer_for_returns_a_null_observer_when_there_is_no_operation():
    observer = usage_capture.observer_for(None, purpose="answer", alias="gpt-4o-mini")
    assert isinstance(observer, NullAttemptObserver)
    observer = usage_capture.observer_for({"configurable": {}}, purpose="answer", alias="gpt-4o-mini")
    assert isinstance(observer, NullAttemptObserver)


def test_observer_for_never_raises_on_a_malformed_config():
    class _Hostile(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("hostile config")

    observer = usage_capture.observer_for(_Hostile(), purpose="answer", alias="gpt-4o-mini")
    assert isinstance(observer, NullAttemptObserver)


def test_embedding_scope_is_a_no_op_without_an_operation():
    token = usage_capture.begin_embedding_scope(None)
    assert token is None
    usage_capture.end_embedding_scope(token)


def test_observer_for_never_raises_when_the_recorder_cannot_be_built(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="service.usage_capture")
    monkeypatch.setattr(usage_capture, "operation_from_config", lambda config: _operation())
    monkeypatch.setattr(usage_capture, "AttemptRecorder", _boom)

    observer = usage_capture.observer_for({}, purpose="answer", alias="gpt-4o-mini")

    assert isinstance(observer, NullAttemptObserver)
    assert any("observer_for" in r.getMessage() for r in caplog.records)


def test_run_config_with_operation_returns_the_config_untouched_on_failure(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="service.usage_capture")
    monkeypatch.setattr(usage_capture, "current_operation", _boom)
    original = {"metadata": {"trace_marker": "v1u"}}

    assert usage_capture.run_config_with_operation(original) is original
    assert usage_capture.run_config_with_operation(None) is None
    assert any("run_config_with_operation" in r.getMessage() for r in caplog.records)


def test_run_config_with_operation_leaves_config_none_when_no_turn_is_in_flight():
    """B8: with tracing off and no operation, graph.invoke must still be called
    with config=None exactly as it is today."""
    assert usage_capture.run_config_with_operation(None) is None


def test_begin_embedding_scope_never_raises(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="service.usage_capture")
    monkeypatch.setattr(usage_capture, "operation_from_config", lambda config: _operation())
    monkeypatch.setattr(usage_capture.retrieval, "set_embedding_sink", _boom)

    assert usage_capture.begin_embedding_scope({}) is None
    assert any("begin_embedding_scope" in r.getMessage() for r in caplog.records)


def test_end_embedding_scope_never_raises_on_a_foreign_token(caplog):
    caplog.set_level("WARNING", logger="service.usage_capture")

    usage_capture.end_embedding_scope(object())

    assert any("end_embedding_scope" in r.getMessage() for r in caplog.records)


def test_record_embedding_never_raises_when_the_emitter_does():
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose=usage_capture.PURPOSE_EMBEDDING, alias=EMBED_MODEL,
        emit=_RaisingEmitter(),
    )
    recorder.attempt_started()
    recorder.record_embedding(input_tokens=7, error=None)


# ---------------------------------------------------------------------------
# AC 3 — the structuring reroute is behaviour-preserving on a bare response
# ---------------------------------------------------------------------------

_SPELL_JSON = '{"name": "Fireball", "description": "8d6 fire damage."}'
_STATBLOCK_JSON = '{"name": "Goblin", "ac": 15, "hp": 7}'


def test_generate_spell_content_survives_a_response_without_id_or_metadata():
    client = _BareClient(_SPELL_JSON)
    spell = generate_spell_content("Fireball deals 8d6 fire damage.", client=client)
    assert spell.name == "Fireball"
    assert client.calls == 1


def test_generate_stat_block_survives_a_response_without_id_or_metadata():
    client = _BareClient(_STATBLOCK_JSON)
    block = generate_stat_block("Goblin. Armor Class 15. Hit Points 7.", client=client)
    assert block.name == "Goblin"
    assert block.ac == 15
    assert client.calls == 1


def test_structuring_calls_are_observed_and_never_retry():
    """max_attempts=1: a retryable provider error produces exactly one record
    and exactly one client call — today's no-retry behaviour, preserved."""
    emitter = _CollectingEmitter()
    recorder = usage_capture.AttemptRecorder(
        _operation(), purpose="spell_structuring", alias="gpt-4o-mini", emit=emitter,
    )

    class _AlwaysRateLimited:
        calls = 0

        def invoke(self, messages, config=None, **kw):
            type(self).calls += 1
            raise _rate_limit_error()

    client = _AlwaysRateLimited()
    with pytest.raises(openai.RateLimitError):
        generate_spell_content("Fireball.", client=client, observer=recorder)
    assert _AlwaysRateLimited.calls == 1
    assert len(emitter.records) == 1
    assert emitter.records[0]["retry_index"] == 0
    assert emitter.records[0]["status"] == "error"
    assert emitter.records[0]["purpose"] == "spell_structuring"

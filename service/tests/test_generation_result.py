"""
Unit tests for GenerationResult / GenerationUsage / the attempt observer
(agent-forge-harness-b8o.1, Checkpoint 1 slice 4 — TDD row 11).

See docs/forge/plans/game-guide-ai-model-routing.md, "Server-owned model
catalog": "The provider boundary must return more than answer text ... Unknown
usage fields remain null rather than becoming zero. An attempt observer
receives one record per actual provider call, including failures."

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_generation_result.py -q
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from service.generate import (
    GenerationResult,
    GenerationUsage,
    NullAttemptObserver,
    generate_result,
)


class _FakeClient:
    """Returns a canned AIMessage; records the messages/config it was invoked with."""

    def __init__(self, message):
        self.message = message
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        return self.message


class _RaisingClient:
    def invoke(self, messages, config=None, **kw):
        raise RuntimeError("upstream boom")


class _RecordingObserver:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, *, alias, result, error):
        self.records.append({"alias": alias, "result": result, "error": error})


def test_generation_result_text_matches_response_content():
    client = _FakeClient(AIMessage(content="the answer [1]"))
    result = generate_result([HumanMessage(content="q")], alias="gpt-4o-mini", client=client)
    assert isinstance(result, GenerationResult)
    assert result.text == "the answer [1]"


def test_usage_captured_when_the_provider_reports_it():
    msg = AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "input_token_details": {"cache_read": 20},
            "output_token_details": {"reasoning": 10},
        },
    )
    result = generate_result([HumanMessage(content="q")], alias="gpt-4o-mini", client=_FakeClient(msg))
    assert result.usage == GenerationUsage(
        input_tokens=100, cached_input_tokens=20, output_tokens=50, reasoning_tokens=10,
    )


def test_unknown_usage_fields_stay_none_not_zero():
    # No usage_metadata at all — a provider that doesn't report tokens is not
    # the same as a provider that reported zero.
    result = generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini", client=_FakeClient(AIMessage(content="x")),
    )
    assert result.usage == GenerationUsage(
        input_tokens=None, cached_input_tokens=None, output_tokens=None, reasoning_tokens=None,
    )


def test_provider_request_id_and_finish_reason_captured():
    msg = AIMessage(
        content="x", id="chatcmpl-abc123",
        response_metadata={"finish_reason": "stop", "model_name": "gpt-4o-mini"},
    )
    result = generate_result([HumanMessage(content="q")], alias="gpt-4o-mini", client=_FakeClient(msg))
    assert result.provider_request_id == "chatcmpl-abc123"
    assert result.finish_reason == "stop"


def test_missing_request_id_and_finish_reason_stay_none():
    result = generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini", client=_FakeClient(AIMessage(content="x")),
    )
    assert result.provider_request_id is None
    assert result.finish_reason is None


def test_observer_records_exactly_one_success_attempt():
    observer = _RecordingObserver()
    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini",
        client=_FakeClient(AIMessage(content="x")), observer=observer,
    )
    assert len(observer.records) == 1
    rec = observer.records[0]
    assert rec["alias"] == "gpt-4o-mini"
    assert rec["error"] is None
    assert isinstance(rec["result"], GenerationResult)


def test_observer_records_exactly_one_failed_attempt_and_the_error_still_propagates():
    observer = _RecordingObserver()
    with pytest.raises(RuntimeError, match="upstream boom"):
        generate_result(
            [HumanMessage(content="q")], alias="gpt-4o-mini",
            client=_RaisingClient(), observer=observer,
        )
    assert len(observer.records) == 1
    rec = observer.records[0]
    assert rec["alias"] == "gpt-4o-mini"
    assert rec["result"] is None
    assert isinstance(rec["error"], RuntimeError)


def test_no_observer_given_defaults_to_a_silent_no_op():
    # Must not raise just because nobody is watching.
    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini", client=_FakeClient(AIMessage(content="x")),
    )


def test_null_attempt_observer_is_a_no_op():
    NullAttemptObserver().record(alias="gpt-4o-mini", result=None, error=None)

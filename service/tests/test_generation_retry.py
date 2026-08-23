"""
Unit tests for generate_result()'s bounded service-owned retry
(agent-forge-harness-b8o.1, Checkpoint 1 slice 5).

See docs/forge/plans/game-guide-ai-model-routing.md, Checkpoint 1 step 5: "Set
provider SDK clients to max_retries=0 and land the bounded service-owned retry
in the same change. Disabling SDK retries is itself a behavior change to the
baseline -- ChatOpenAI retries by default -- so the two must ship together or
the baseline temporarily loses resilience." Retry classification here is
deliberately narrow (transient network/rate-limit/5xx only) -- the full
normalized error-category table (D4) is Checkpoint 2's job, not this one.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_generation_retry.py -q
"""

from __future__ import annotations

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from service.generate import generate_result

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError("rate limited", response=httpx.Response(429, request=_REQUEST), body=None)


def _server_error() -> openai.InternalServerError:
    return openai.InternalServerError("server error", response=httpx.Response(500, request=_REQUEST), body=None)


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=_REQUEST)


def _bad_request_error() -> openai.BadRequestError:
    return openai.BadRequestError("bad request", response=httpx.Response(400, request=_REQUEST), body=None)


class _FlakyClient:
    """Raises the given errors in order on the first N calls, then succeeds."""

    def __init__(self, errors, final_message):
        self._errors = list(errors)
        self._final_message = final_message
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._final_message


class _AlwaysFailingClient:
    def __init__(self, error_factory):
        self._error_factory = error_factory
        self.calls = 0

    def invoke(self, messages, config=None, **kw):
        self.calls += 1
        raise self._error_factory()


class _RecordingObserver:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, *, alias, result, error):
        self.records.append({"error": error, "result": result})


class _RecordingSleep:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_retryable_error_then_success_returns_the_successful_result():
    client = _FlakyClient([_rate_limit_error()], AIMessage(content="ok"))
    sleep = _RecordingSleep()
    result = generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini", client=client, sleep=sleep,
    )
    assert result.text == "ok"
    assert client.calls == 2
    assert len(sleep.calls) == 1  # one retry delay before the 2nd attempt


def test_each_attempt_is_recorded_including_the_ones_that_fail():
    client = _FlakyClient([_rate_limit_error(), _server_error()], AIMessage(content="ok"))
    observer = _RecordingObserver()
    generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini", client=client,
        observer=observer, sleep=_RecordingSleep(),
    )
    assert len(observer.records) == 3  # 2 failures + 1 success
    assert [r["error"] is None for r in observer.records] == [False, False, True]


def test_retry_is_bounded_then_raises_the_last_error():
    client = _AlwaysFailingClient(_rate_limit_error)
    observer = _RecordingObserver()
    with pytest.raises(openai.RateLimitError):
        generate_result(
            [HumanMessage(content="q")], alias="gpt-4o-mini", client=client,
            observer=observer, sleep=_RecordingSleep(), max_attempts=3,
        )
    assert client.calls == 3
    assert len(observer.records) == 3
    assert all(r["error"] is not None for r in observer.records)


def test_connection_error_is_retried():
    client = _FlakyClient([_connection_error()], AIMessage(content="ok"))
    result = generate_result(
        [HumanMessage(content="q")], alias="gpt-4o-mini", client=client, sleep=_RecordingSleep(),
    )
    assert result.text == "ok"
    assert client.calls == 2


def test_non_retryable_error_fails_on_the_first_attempt_no_retry():
    client = _AlwaysFailingClient(_bad_request_error)
    sleep = _RecordingSleep()
    with pytest.raises(openai.BadRequestError):
        generate_result(
            [HumanMessage(content="q")], alias="gpt-4o-mini", client=client, sleep=sleep,
        )
    assert client.calls == 1
    assert sleep.calls == []


def test_a_plain_exception_is_not_retried():
    # Only the specific transient openai exception types are retryable —
    # an arbitrary error (e.g. a bug in a fake test client) must not loop.
    client = _AlwaysFailingClient(lambda: RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        generate_result(
            [HumanMessage(content="q")], alias="gpt-4o-mini", client=client, sleep=_RecordingSleep(),
        )
    assert client.calls == 1

"""
Unit tests for the D4 normalized error/status mapping (agent-forge-harness-
b8o.2, Checkpoint 2 slice 3). See docs/forge/plans/game-guide-ai-model-routing.md,
"Error and status contract":

| category                         | status | retryable |
|-----------------------------------|-------:|-----------|
| rate_limit                        |    429 | yes       |
| content_filter                    |    422 | no        |
| invalid_request                   |    422 | no        |
| authentication, quota             |    502 | no        |
| timeout, upstream_unavailable,    |    502 | yes       |
| unknown                           |        |           |

(conversation strategy mismatch -> 409 and budget/daily cap -> 429 are
already implemented elsewhere -- slice 2 and the pre-existing cost guard --
and aren't re-tested here.)

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_error_mapping.py -q
"""

from __future__ import annotations

import httpx
import openai
import pytest

from service.app import ERROR_STATUS, normalize_llm_error

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_error(cls, code: int):
    return cls("x", response=httpx.Response(code, request=_REQUEST), body=None)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(openai.RateLimitError, 429), "rate_limit"),
        (openai.ContentFilterFinishReasonError(), "content_filter"),
        (_status_error(openai.BadRequestError, 400), "invalid_request"),
        (_status_error(openai.UnprocessableEntityError, 422), "invalid_request"),
        (_status_error(openai.AuthenticationError, 401), "authentication"),
        (_status_error(openai.PermissionDeniedError, 403), "quota"),
        (openai.APITimeoutError(request=_REQUEST), "timeout"),
        (openai.APIConnectionError(request=_REQUEST), "upstream_unavailable"),
        (_status_error(openai.InternalServerError, 500), "upstream_unavailable"),
        (RuntimeError("something else entirely"), "unknown"),
    ],
)
def test_normalize_llm_error(exc, expected):
    assert normalize_llm_error(exc) == expected


@pytest.mark.parametrize(
    ("category", "status", "retryable"),
    [
        ("rate_limit", 429, True),
        ("content_filter", 422, False),
        ("invalid_request", 422, False),
        ("authentication", 502, False),
        ("quota", 502, False),
        ("timeout", 502, True),
        ("upstream_unavailable", 502, True),
        ("unknown", 502, True),
    ],
)
def test_error_status_table(category, status, retryable):
    assert ERROR_STATUS[category] == (status, retryable)


# ── Wired into /chat (not just the pure classifier) ───────────────────────────

from fastapi.testclient import TestClient  # noqa: E402

from service.app import app, get_service  # noqa: E402


class _RaisingService:
    def __init__(self, exc):
        self._exc = exc

    def answer(self, *a, **kw):
        raise self._exc


def _chat_raising(exc):
    app.dependency_overrides[get_service] = lambda: _RaisingService(exc)
    try:
        return TestClient(app).post("/chat", json={"prompt": "hi"})
    finally:
        app.dependency_overrides.pop(get_service, None)


def test_rate_limit_reaches_the_client_as_429_with_category():
    r = _chat_raising(_status_error(openai.RateLimitError, 429))
    assert r.status_code == 429
    assert r.json()["detail"]["category"] == "rate_limit"
    assert r.json()["detail"]["retryable"] is True


def test_rate_limit_surfaces_retry_after_when_the_provider_sends_one():
    resp = httpx.Response(429, request=_REQUEST, headers={"retry-after": "7"})
    r = _chat_raising(openai.RateLimitError("x", response=resp, body=None))
    assert r.headers.get("retry-after") == "7"


def test_content_filter_reaches_the_client_as_422_non_retryable():
    r = _chat_raising(openai.ContentFilterFinishReasonError())
    assert r.status_code == 422
    assert r.json()["detail"]["category"] == "content_filter"
    assert r.json()["detail"]["retryable"] is False


def test_authentication_reaches_the_client_as_502_never_retryable():
    r = _chat_raising(_status_error(openai.AuthenticationError, 401))
    assert r.status_code == 502
    assert r.json()["detail"]["category"] == "authentication"
    assert r.json()["detail"]["retryable"] is False


def test_a_non_openai_exception_still_hits_the_generic_500_handler():
    # normalize_llm_error only runs for _LLM_ERRORS (openai.OpenAIError) --
    # anything else is still a bug in our code, unchanged from before.
    r = _chat_raising(RuntimeError("a real bug"))
    assert r.status_code == 500

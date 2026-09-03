"""
/chat wiring for model routing (agent-forge-harness-b8o.2, Checkpoint 2 slice
2): request validation, atomic strategy binding, 409 on mismatch, and routing
disclosure in the response. Uses the default authenticated session from
conftest.py's autouse fixture (user_id=1, role="dm") — no real_auth marker
needed, this isn't testing auth itself.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_model_routing_chat.py -q
"""

from __future__ import annotations

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from service.app import app, get_message_store, get_service
from service.history import InMemoryMessageStore
from service.models import ChatMode, ChatResponse

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


class _FakeService:
    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(
            answer="ok", sources=[], answerable=True,
            mode=ChatMode(mode), conversation_id=conversation_id,
        )


@pytest.fixture
def env():
    store = InMemoryMessageStore()
    app.dependency_overrides[get_service] = lambda: _FakeService()
    app.dependency_overrides[get_message_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_service, None)
    app.dependency_overrides.pop(get_message_store, None)


def test_omitting_model_preference_defaults_to_auto(env):
    c = TestClient(app)
    body = c.post("/chat", json={"prompt": "hi"}).json()
    assert body["routing"]["requested"] == "auto"
    assert body["routing"]["strategy"] == "auto"


def test_auto_resolves_to_the_catalog_default(env):
    c = TestClient(app)
    body = c.post("/chat", json={"prompt": "hi", "model_preference": "auto"}).json()
    assert body["routing"]["effective"] == "gpt-4o-mini"
    assert body["routing"]["provider"] == "openai"


def test_manual_alias_is_disclosed_as_both_requested_and_effective(env):
    c = TestClient(app)
    body = c.post(
        "/chat", json={"prompt": "hi", "model_preference": "gpt-4o-mini"},
    ).json()
    assert body["routing"]["requested"] == "gpt-4o-mini"
    assert body["routing"]["effective"] == "gpt-4o-mini"
    assert body["routing"]["strategy"] == "manual"


def test_unknown_model_preference_is_422():
    c = TestClient(app)
    app.dependency_overrides[get_service] = lambda: _FakeService()
    try:
        r = c.post("/chat", json={"prompt": "hi", "model_preference": "not-a-real-model"})
        assert r.status_code == 422
    finally:
        app.dependency_overrides.pop(get_service, None)


def test_disabled_model_preference_is_422_same_as_unknown(env):
    # TDD row 1: disabled must look identical to unknown to the caller.
    c = TestClient(app)
    r = c.post("/chat", json={"prompt": "hi", "model_preference": "deepseek-v4-flash"})
    assert r.status_code == 422


def test_second_turn_same_conversation_same_preference_succeeds(env):
    c = TestClient(app)
    r1 = c.post("/chat", json={"prompt": "hi", "model_preference": "auto"})
    conv = r1.json()["conversation_id"]
    r2 = c.post("/chat", json={"prompt": "again", "model_preference": "auto", "conversation_id": conv})
    assert r2.status_code == 200
    assert r2.json()["routing"]["strategy"] == "auto"


def test_changing_model_preference_on_a_started_conversation_is_409(env):
    c = TestClient(app)
    r1 = c.post("/chat", json={"prompt": "hi", "model_preference": "auto"})
    conv = r1.json()["conversation_id"]
    r2 = c.post(
        "/chat",
        json={"prompt": "again", "model_preference": "gpt-4o-mini", "conversation_id": conv},
    )
    assert r2.status_code == 409


def test_409_happens_before_any_provider_call(env):
    class _ExplodingService:
        def answer(self, *a, **kw):
            raise AssertionError("must not be called after a strategy mismatch")

    c = TestClient(app)
    r1 = c.post("/chat", json={"prompt": "hi", "model_preference": "auto"})
    conv = r1.json()["conversation_id"]
    app.dependency_overrides[get_service] = lambda: _ExplodingService()
    try:
        r2 = c.post(
            "/chat",
            json={"prompt": "again", "model_preference": "gpt-4o-mini", "conversation_id": conv},
        )
        assert r2.status_code == 409
    finally:
        app.dependency_overrides[get_service] = lambda: _FakeService()


def test_strategy_binding_survives_a_provider_failure(env):
    # The claim happens before the try/provider-call block in /chat, so a
    # failed provider call must NOT roll it back — otherwise two concurrent
    # first turns could each retry into a different effective model after
    # the other's binding attempt failed.
    class _RaisingOnce:
        def answer(self, *a, **kw):
            raise openai.APIConnectionError(request=_REQUEST)

    conv = "11111111-1111-1111-1111-111111111111"
    c = TestClient(app)
    app.dependency_overrides[get_service] = lambda: _RaisingOnce()
    try:
        r1 = c.post(
            "/chat",
            json={"prompt": "hi", "model_preference": "auto", "conversation_id": conv},
        )
        assert r1.status_code == 502
    finally:
        app.dependency_overrides[get_service] = lambda: _FakeService()

    r2 = c.post(
        "/chat",
        json={"prompt": "again", "model_preference": "gpt-4o-mini", "conversation_id": conv},
    )
    assert r2.status_code == 409


def test_no_message_store_configured_skips_binding_gracefully():
    # store is None (no DB) — degrade to per-request resolution, no 409 ever
    # possible without a store to check against (matches the graceful-
    # degradation posture _persist_turn/_fetch_attachment_context already use).
    app.dependency_overrides[get_service] = lambda: _FakeService()
    try:
        c = TestClient(app)
        r = c.post("/chat", json={"prompt": "hi", "model_preference": "auto"})
        assert r.status_code == 200
        assert r.json()["routing"]["strategy"] == "auto"
    finally:
        app.dependency_overrides.pop(get_service, None)

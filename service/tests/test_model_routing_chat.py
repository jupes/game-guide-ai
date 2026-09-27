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
from service.model_catalog import CATALOG, DEFAULT_ALIAS, public_model_id
from service.models import ChatMode, ChatResponse, Suggestion

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

# D-9 (au3): the client names a model by its public id, never the alias.
_PUBLIC_ID = public_model_id(DEFAULT_ALIAS)


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
    assert body["routing"]["effective"] == _PUBLIC_ID
    assert body["routing"]["provider"] is None


def test_manual_alias_is_disclosed_as_both_requested_and_effective(env):
    c = TestClient(app)
    body = c.post(
        "/chat", json={"prompt": "hi", "model_preference": _PUBLIC_ID},
    ).json()
    assert body["routing"]["requested"] == _PUBLIC_ID
    assert body["routing"]["effective"] == _PUBLIC_ID
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
        json={"prompt": "again", "model_preference": _PUBLIC_ID, "conversation_id": conv},
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
            json={"prompt": "again", "model_preference": _PUBLIC_ID, "conversation_id": conv},
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
        json={"prompt": "again", "model_preference": _PUBLIC_ID, "conversation_id": conv},
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


# ---------------------------------------------------------------------------
# D-9 (au3): users never learn which model or provider answers.
# ---------------------------------------------------------------------------

def test_a_real_alias_and_a_disabled_public_id_are_422_same_as_unknown(env):
    # No oracle: the enabled model's real alias, a disabled entry's public id
    # and noise are refused with the same status and the same words.
    c = TestClient(app)
    for value in (DEFAULT_ALIAS, public_model_id("deepseek-v4-flash"), "not-a-real-model"):
        r = c.post("/chat", json={"prompt": "hi", "model_preference": value})
        assert r.status_code == 422, value
        assert r.json()["detail"] == f"unknown or disabled model: {value!r}"


def test_a_manual_public_id_binds_the_internal_alias(env):
    # What the conversation is bound to is server-side and keeps the alias;
    # only the wire changed.
    c = TestClient(app)
    conv = c.post("/chat", json={"prompt": "hi", "model_preference": _PUBLIC_ID}).json()["conversation_id"]
    assert env.conversation_strategy(conv) == ("manual", DEFAULT_ALIAS)


class _SpellService:
    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(
            answer="ok", sources=[], answerable=True,
            mode=ChatMode(mode), conversation_id=conversation_id,
            suggestions=[
                Suggestion(style=style, text="idea") for style in ("practical", "roleplay", "wacky")
            ],
        )


def test_no_chat_response_or_catalog_names_a_model_or_provider(env):
    app.dependency_overrides[get_service] = lambda: _SpellService()
    c = TestClient(app)
    responses = [c.get("/models")]
    for preference in ("auto", _PUBLIC_ID):
        r = c.post("/chat", json={"prompt": "Fireball", "mode": "spell", "model_preference": preference})
        assert r.status_code == 200
        assert r.json()["routing"] is not None and r.json()["suggestions_routing"] is not None
        responses.append(r)
    names = {name for p in CATALOG.values() for name in (p.alias, p.display_name, p.api_model, p.provider)}
    for r in responses:
        wire = r.text.lower()
        for name in names:
            assert name.lower() not in wire, (r.url, name)

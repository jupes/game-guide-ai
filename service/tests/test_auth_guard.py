"""Behavior #5 — require_session guards the data endpoints (x5bz.2 Checkpoint C).

Without a valid session cookie, /chat and /conversations/* are 401; with one
(minted via signup), /chat is 200. /healthz and /metrics/ui stay open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from service.app import app, get_auth_store, get_service
from service.auth_store import InMemoryAuthStore
from service.models import ChatMode, ChatResponse

pytestmark = pytest.mark.real_auth


class _FakeService:
    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(answer="ok", sources=[], answerable=True,
                            mode=ChatMode(mode), conversation_id=conversation_id)


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    s = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: s
    app.dependency_overrides[get_service] = lambda: _FakeService()
    yield s
    app.dependency_overrides.clear()


def _authed_client(store) -> TestClient:
    token = store.create_invite(
        role="dm", expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ).token
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "a@example.com", "password": "password123", "invite": token})
    return client


def test_chat_without_session_is_401(store):
    r = TestClient(app).post("/chat", json={"prompt": "What is a Basilisk?"})
    assert r.status_code == 401


def test_chat_with_session_is_200(store):
    r = _authed_client(store).post("/chat", json={"prompt": "What is a Basilisk?"})
    assert r.status_code == 200
    assert r.json()["answerable"] is True


def test_conversation_messages_without_session_is_401(store):
    r = TestClient(app).get("/conversations/abc/messages")
    assert r.status_code == 401


def test_upload_attachment_without_session_is_401(store):
    r = TestClient(app).post(
        "/conversations/abc/attachments",
        json={"filename": "x.txt", "content_type": "text/plain", "data": "aGk="},
    )
    assert r.status_code == 401


def test_healthz_stays_open(store):
    assert TestClient(app).get("/healthz").status_code == 200


def test_metrics_ui_stays_open(store):
    # Unguarded telemetry beacon — must not 401 without a session.
    r = TestClient(app).post("/metrics/ui", json={"points": []})
    assert r.status_code != 401


# ── Security review regressions: the cookie is not the last word ─────────────


def test_deleted_account_cannot_keep_using_its_cookie(store):
    """The session cookie stays cryptographically valid for days, so the account
    is re-read per request: deleting it must revoke access immediately."""
    client = _authed_client(store)
    assert client.post("/chat", json={"prompt": "hi"}).status_code == 200

    store._users.clear()  # noqa: SLF001 - simulate the account being removed
    assert client.post("/chat", json={"prompt": "hi"}).status_code == 401


def test_role_change_takes_effect_without_re_login(store):
    """A demoted DM must lose GM access at once — authorization uses the CURRENT
    stored role, not the one baked into the cookie at login."""
    client = _authed_client(store)  # signs up as dm
    assert client.post("/chat", json={"prompt": "x", "mode": "gm"}).status_code == 200

    store._users[0].role = "player"  # noqa: SLF001 - admin demotes them
    assert client.post("/chat", json={"prompt": "x", "mode": "gm"}).status_code == 403
    # ...but non-GM channels still work.
    assert client.post("/chat", json={"prompt": "x", "mode": "sage"}).status_code == 200

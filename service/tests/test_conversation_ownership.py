"""Behavior #7 — per-user conversation ownership (x5bz.2 Checkpoint D).

The first authenticated user to use a conversation_id owns it; another user
cannot read its messages or attachments, nor post into it (403). The owner can.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from service.app import app, get_auth_store, get_message_store, get_service
from service.auth_store import InMemoryAuthStore
from service.history import InMemoryMessageStore
from service.models import ChatMode, ChatResponse

pytestmark = pytest.mark.real_auth


class _FakeService:
    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(answer="ok", sources=[], answerable=True,
                            mode=ChatMode(mode), conversation_id=conversation_id)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    auth = InMemoryAuthStore()
    messages = InMemoryMessageStore()
    app.dependency_overrides[get_auth_store] = lambda: auth
    app.dependency_overrides[get_message_store] = lambda: messages
    app.dependency_overrides[get_service] = lambda: _FakeService()
    yield auth
    app.dependency_overrides.clear()


def _signup(auth, email: str) -> TestClient:
    token = auth.create_invite(
        role="player", expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ).token
    client = TestClient(app)
    client.post("/auth/signup", json={"email": email, "password": "password123", "invite": token})
    return client


def test_other_user_cannot_read_or_post_to_your_conversation(env):
    auth = env
    alice = _signup(auth, "alice@example.com")
    bob = _signup(auth, "bob@example.com")

    # Alice starts (and thereby owns) conv-A.
    assert alice.post("/chat", json={"prompt": "hi", "conversation_id": "conv-A"}).status_code == 200

    # Bob cannot read it or post into it.
    assert bob.get("/conversations/conv-A/messages").status_code == 403
    assert bob.post("/chat", json={"prompt": "sneak", "conversation_id": "conv-A"}).status_code == 403

    # Alice still can.
    assert alice.get("/conversations/conv-A/messages").status_code == 200


def test_other_user_cannot_read_your_attachments(env):
    auth = env
    alice = _signup(auth, "alice@example.com")
    bob = _signup(auth, "bob@example.com")

    up = alice.post(
        "/conversations/conv-A/attachments",
        json={"filename": "notes.txt", "content_type": "text/plain", "data": "aGVsbG8="},
    )
    assert up.status_code == 200
    assert bob.get("/conversations/conv-A/attachments").status_code == 403
    assert alice.get("/conversations/conv-A/attachments").status_code == 200


def test_unclaimed_conversation_is_readable(env):
    # A conversation nobody has claimed (owner None) isn't 403 — ownership only
    # bites once someone owns it.
    alice = _signup(env, "alice@example.com")
    assert alice.get("/conversations/fresh-conv/messages").status_code == 200

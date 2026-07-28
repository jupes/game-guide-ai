"""Behavior #6 — server-side GM-channel role enforcement (x5bz.2 Checkpoint D).

The GM channel is DM-only, enforced from the SESSION role (not the UI toggle):
a player-role session posting mode=gm gets 403; a dm session succeeds.
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
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    s = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: s
    app.dependency_overrides[get_service] = lambda: _FakeService()
    yield s
    app.dependency_overrides.clear()


def _client_as(store, role: str) -> TestClient:
    token = store.create_invite(
        role=role, expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ).token
    client = TestClient(app)
    client.post(
        "/auth/signup",
        json={"email": f"{role}@example.com", "password": "password123", "invite": token},
    )
    return client


def test_player_cannot_use_gm_channel(store):
    r = _client_as(store, "player").post("/chat", json={"prompt": "plot a dungeon", "mode": "gm"})
    assert r.status_code == 403


def test_dm_can_use_gm_channel(store):
    r = _client_as(store, "dm").post("/chat", json={"prompt": "plot a dungeon", "mode": "gm"})
    assert r.status_code == 200


def test_player_can_use_non_gm_channels(store):
    client = _client_as(store, "player")
    for mode in ("sage", "spell", "rules"):
        r = client.post("/chat", json={"prompt": "what is a basilisk", "mode": mode})
        assert r.status_code == 200, mode

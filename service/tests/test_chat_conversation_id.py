"""A chat request without a conversation_id (x5bz.3.2).

`conversation_id` has been optional since the field was a pass-through stub —
its description still says "persistence is stubbed". Persistence, ownership and
auth all arrived afterwards, and each independently chose to SKIP on a `None`
rather than reject it (`app.py:423, 440, 536` are the same early return three
times). Nobody designed that, but the effect is that `None` means "opt out of
every server-side control".

That was cosmetic until the cost guard: a request the server never persists is a
request the daily cap (x5bz.3.3) cannot count, so an authenticated caller could
omit one field and make LLM calls invisible to the ceiling meant to bound them.

The fix keeps the field optional and gives `None` an honest meaning instead —
"start a new conversation". The server mints an id, uses it for ownership and
persistence, and returns it. Nothing can reach the model uncounted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import config
from service import ratelimit
from service.app import app, get_auth_store, get_message_store, get_service
from service.auth_store import InMemoryAuthStore
from service.history import InMemoryMessageStore
from service.models import ChatMode, ChatResponse

pytestmark = pytest.mark.real_auth


class _FakeService:
    """Echoes conversation_id back the way the real service does (rag.py:143)."""

    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(answer="ok", sources=[], answerable=True,
                            mode=ChatMode(mode), conversation_id=conversation_id)


@pytest.fixture
def stores(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    ratelimit.reset_all()
    auth = InMemoryAuthStore()
    messages = InMemoryMessageStore()
    app.dependency_overrides[get_auth_store] = lambda: auth
    app.dependency_overrides[get_message_store] = lambda: messages
    app.dependency_overrides[get_service] = lambda: _FakeService()
    yield auth, messages
    app.dependency_overrides.clear()
    ratelimit.reset_all()


def _client_as(auth, role: str = "player", email: str | None = None) -> TestClient:
    token = auth.create_invite(
        role=role, expires_at=datetime.now(UTC) + timedelta(days=1),
    ).token
    client = TestClient(app)
    client.post("/auth/signup", json={
        "email": email or f"{role}@example.com",
        "password": "password123",
        "invite": token,
    })
    return client


def _ask_without_id(client: TestClient):
    return client.post("/chat", json={"prompt": "what does fireball do"})


# ── Behavior 4: the server supplies what the client omitted ──────────────────


def test_a_request_with_no_conversation_id_is_answered_and_given_one(stores) -> None:
    auth, _ = stores
    response = _ask_without_id(_client_as(auth))

    assert response.status_code == 200
    minted = response.json()["conversation_id"]
    assert minted, "the response must carry the id the server minted"
    assert len(minted) >= 32, f"expected a uuid-shaped id, got {minted!r}"


def test_two_such_requests_get_different_conversations(stores) -> None:
    """`None` means 'start a new conversation' — so it must start a NEW one each
    time, not silently rejoin a shared bucket that would mix testers' history."""
    auth, _ = stores
    client = _client_as(auth)
    first = _ask_without_id(client).json()["conversation_id"]
    second = _ask_without_id(client).json()["conversation_id"]

    assert first != second


def test_a_supplied_conversation_id_is_still_honoured(stores) -> None:
    """Minting must not override a client that did send one, or every follow-up
    turn would start a fresh conversation and history would never accumulate."""
    auth, _ = stores
    response = _client_as(auth).post(
        "/chat", json={"prompt": "and its damage?", "conversation_id": "mine-123"},
    )
    assert response.json()["conversation_id"] == "mine-123"


# ── Behavior 6: nothing reaches the model uncounted ──────────────────────────


def test_the_turn_is_persisted_under_the_minted_id(stores) -> None:
    """The point of the whole change: a persisted turn is a countable turn.
    Without this, x5bz.3.3's daily cap reads a number that omits these requests."""
    auth, messages = stores
    minted = _ask_without_id(_client_as(auth)).json()["conversation_id"]

    stored = messages.recent(minted, limit=10)
    roles = [m.role for m in stored]
    assert roles == ["user", "assistant"], (
        f"both turns must be persisted under the minted id, got {roles}"
    )


# ── Behavior 5: a minted conversation still belongs to someone ───────────────


def test_a_minted_conversation_belongs_to_its_creator(stores) -> None:
    """Minting must go through the same ownership claim as a client-supplied id.
    Skipping it would make server-minted conversations readable by anyone who
    could guess the id — trading a counting hole for an access one."""
    auth, _ = stores
    owner = _client_as(auth, email="owner@example.com")
    intruder = _client_as(auth, email="intruder@example.com")

    minted = _ask_without_id(owner).json()["conversation_id"]

    assert owner.get(f"/conversations/{minted}/messages").status_code == 200
    assert intruder.get(f"/conversations/{minted}/messages").status_code == 403

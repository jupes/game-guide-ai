"""Behavior #7 — per-user conversation ownership (x5bz.2 Checkpoint D).

The first authenticated user to use a conversation_id owns it; another user
cannot read its messages or attachments, nor post into it (403). The owner can.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
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
        role="player", expires_at=datetime.now(UTC) + timedelta(days=1),
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


def test_reading_an_unowned_conversation_claims_it(env):
    """Conversations predating the ownership table have no owner row. "No owner
    ⇒ allow" would leave all of that pre-auth history readable by ANY
    authenticated caller forever, so the first reader takes ownership instead."""
    auth = env
    messages = InMemoryMessageStore()
    app.dependency_overrides[get_message_store] = lambda: messages
    # A legacy conversation with real content and no ownership row.
    messages.append("legacy-conv", "sage", "user", "alice's private question")

    alice = _signup(auth, "alice@example.com")
    bob = _signup(auth, "bob@example.com")

    first = alice.get("/conversations/legacy-conv/messages")
    assert first.status_code == 200
    assert first.json()["messages"][0]["content"] == "alice's private question"

    # Now it is hers — nobody else can read it.
    assert bob.get("/conversations/legacy-conv/messages").status_code == 403
    assert bob.get("/conversations/legacy-conv/attachments").status_code == 403
    assert bob.post(
        "/chat", json={"prompt": "sneak", "conversation_id": "legacy-conv"},
    ).status_code == 403


# ── Security review regressions (must fail CLOSED) ───────────────────────────


class _AuthzFailingStore(InMemoryMessageStore):
    """Ownership lookups raise; everything else works — models a transient DB
    error or a role that can't read chat.conversations."""

    def owner_of(self, conversation_id):
        raise RuntimeError("ownership lookup unavailable")

    def claim_conversation(self, conversation_id, user_id):
        raise RuntimeError("ownership claim unavailable")

    def has_content(self, conversation_id):
        raise RuntimeError("content probe unavailable")


def test_ownership_lookup_failure_denies_instead_of_falling_open(env):
    """An authorization lookup that errors must NOT continue into the read.
    Reads use separate connections, so failing open could serve another user's
    conversation."""
    alice = _signup(env, "alice@example.com")
    app.dependency_overrides[get_message_store] = lambda: _AuthzFailingStore()

    assert alice.get("/conversations/conv-A/messages").status_code == 503
    assert alice.get("/conversations/conv-A/attachments").status_code == 503
    assert alice.post(
        "/chat", json={"prompt": "hi", "conversation_id": "conv-A"},
    ).status_code == 503
    assert alice.post(
        "/conversations/conv-A/attachments",
        json={"filename": "x.txt", "content_type": "text/plain", "data": "aGk="},
    ).status_code == 503


class _RacingStore(InMemoryMessageStore):
    """Simulates losing the claim race: the atomic claim reports a different
    winner than the caller (someone else got there first)."""

    def owner_of(self, conversation_id):
        return None  # a pre-check would see it as unowned...

    def claim_conversation(self, conversation_id, user_id):
        return user_id + 999  # ...but the atomic claim says someone else won

    def has_content(self, conversation_id):
        return True


def test_reading_an_empty_conversation_creates_no_ownership_row(env):
    """A GET for an arbitrary id must not mint a row: otherwise any authenticated
    caller could fill chat.conversations just by looping over random ids. An
    empty conversation has nothing to protect and nothing to leak."""
    auth = env
    messages = InMemoryMessageStore()
    app.dependency_overrides[get_message_store] = lambda: messages
    alice = _signup(auth, "alice@example.com")

    for i in range(25):
        assert alice.get(f"/conversations/junk-{i}/messages").status_code == 200
        assert alice.get(f"/conversations/junk-{i}/attachments").status_code == 200

    assert messages._owners == {}, "reads of empty conversations must not claim"  # noqa: SLF001

    # ...and a WRITE still claims, because content is about to exist.
    assert alice.post(
        "/chat", json={"prompt": "hi", "conversation_id": "real-conv"},
    ).status_code == 200
    assert "real-conv" in messages._owners  # noqa: SLF001


class _RaceAfterProbeStore(InMemoryMessageStore):
    """Models the gap between "is this empty?" and the endpoint's own read.

    The probe answers "empty" on its own connection; by the time a follow-up read
    ran, another user could have claimed the id and written to it — so the probe
    result must be treated as *past tense*, not as a licence to read.
    """

    def has_content(self, conversation_id: str) -> bool:
        return False

    def recent(self, conversation_id: str, limit: int):
        raise AssertionError("read a conversation that was authorized as EMPTY")

    def attachments_for(self, conversation_id: str):
        raise AssertionError("read a conversation that was authorized as EMPTY")


def test_an_empty_conversation_is_answered_without_a_second_read(env):
    """The empty case must be answered from the authorization result itself.
    Re-reading would reopen the cross-user window the ownership check closes:
    the row this request finally reads may have been written by someone else
    *after* the probe saw nothing."""
    alice = _signup(env, "alice@example.com")
    app.dependency_overrides[get_message_store] = lambda: _RaceAfterProbeStore()

    msgs = alice.get("/conversations/never-used/messages")
    assert msgs.status_code == 200
    assert msgs.json()["messages"] == []

    atts = alice.get("/conversations/never-used/attachments")
    assert atts.status_code == 200
    assert atts.json()["attachments"] == []


def test_rejected_uploads_create_no_ownership_rows(env):
    """The claim happens immediately before persistence, not on arrival: a
    stream of invalid uploads must not write one chat.conversations row per
    request for content that is never stored."""
    messages = InMemoryMessageStore()
    app.dependency_overrides[get_message_store] = lambda: messages
    alice = _signup(env, "alice@example.com")

    for i in range(25):
        bad = alice.post(
            f"/conversations/junk-{i}/attachments",
            json={"filename": "x.txt", "content_type": "text/plain", "data": "not!base64"},
        )
        assert bad.status_code == 422
    assert messages._owners == {}, "rejected uploads must not claim"  # noqa: SLF001

    # A VALID upload still claims — the payload survived validation, so the row
    # is about to have content.
    ok = alice.post(
        "/conversations/real/attachments",
        json={"filename": "x.txt", "content_type": "text/plain", "data": "aGk="},
    )
    assert ok.status_code == 200
    assert "real" in messages._owners  # noqa: SLF001


def test_a_foreign_conversation_is_still_refused_before_any_decoding(env):
    """Deferring the claim must not defer the *rejection*: an id already owned by
    someone else 403s up front, without decoding the payload."""
    auth = env
    messages = InMemoryMessageStore()
    app.dependency_overrides[get_message_store] = lambda: messages
    alice = _signup(auth, "alice@example.com")
    bob = _signup(auth, "bob@example.com")

    assert alice.post(
        "/conversations/conv-A/attachments",
        json={"filename": "a.txt", "content_type": "text/plain", "data": "aGk="},
    ).status_code == 200

    # Invalid payload AND someone else's conversation: 403 wins, so the decode
    # never runs.
    assert bob.post(
        "/conversations/conv-A/attachments",
        json={"filename": "x.txt", "content_type": "text/plain", "data": "not!base64"},
    ).status_code == 403


def test_losing_the_claim_race_is_rejected(env):
    """The claim is atomic and its RESULT is honored: a user who loses the race
    on a fresh conversation id must be rejected, not allowed to write into the
    winner's conversation."""
    alice = _signup(env, "alice@example.com")
    app.dependency_overrides[get_message_store] = lambda: _RacingStore()

    assert alice.post(
        "/chat", json={"prompt": "hi", "conversation_id": "fresh"},
    ).status_code == 403
    assert alice.post(
        "/conversations/fresh/attachments",
        json={"filename": "x.txt", "content_type": "text/plain", "data": "aGk="},
    ).status_code == 403

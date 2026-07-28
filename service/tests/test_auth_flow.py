"""Behavior #4 (tracer) — signup / login / me end to end (x5bz.2 Checkpoint C).

Real HTTP through the app: signup redeems an invite, sets the session cookie, and
the identity round-trips through login + /auth/me. Uses an in-memory auth store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from service.app import app, get_auth_store
from service.auth_store import InMemoryAuthStore

pytestmark = pytest.mark.real_auth


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    # TestClient speaks http; a Secure cookie wouldn't be sent back. Prod stays Secure.
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    s = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: s
    yield s
    app.dependency_overrides.clear()


def _invite(store: InMemoryAuthStore, role: str = "player") -> str:
    return store.create_invite(
        role=role, expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ).token


def test_signup_redeems_invite_creates_user_and_sets_cookie(store):
    token = _invite(store, role="dm")
    client = TestClient(app)
    r = client.post(
        "/auth/signup",
        json={"email": "Ada@example.com", "password": "password123", "invite": token},
    )
    assert r.status_code == 200
    assert r.json() == {"email": "Ada@example.com", "role": "dm"}
    assert config.SESSION_COOKIE_NAME in r.cookies            # session started
    assert store.get_invite(token).is_used                    # invite consumed
    assert store.get_user_by_email("ada@example.com").role == "dm"  # case-folded


def test_second_signup_with_same_invite_is_rejected(store):
    token = _invite(store)
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "a@example.com", "password": "password123", "invite": token})
    r = client.post("/auth/signup", json={"email": "b@example.com", "password": "password123", "invite": token})
    assert r.status_code == 400  # invite already used


def test_signup_unknown_invite_is_400(store):
    client = TestClient(app)
    r = client.post("/auth/signup", json={"email": "a@example.com", "password": "password123", "invite": "nope"})
    assert r.status_code == 400


def test_duplicate_email_is_409(store):
    i1, i2 = _invite(store), _invite(store)
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "dup@example.com", "password": "password123", "invite": i1})
    r = client.post("/auth/signup", json={"email": "DUP@example.com", "password": "password123", "invite": i2})
    assert r.status_code == 409


def test_signup_rejects_short_password(store):
    token = _invite(store)
    client = TestClient(app)
    r = client.post("/auth/signup", json={"email": "a@example.com", "password": "short", "invite": token})
    assert r.status_code == 422  # pydantic min_length


def test_login_then_me_round_trips_identity(store):
    token = _invite(store, role="player")
    TestClient(app).post(
        "/auth/signup", json={"email": "ada@example.com", "password": "password123", "invite": token},
    )
    # Fresh client (no signup cookie) — prove login issues its own session.
    client = TestClient(app)
    login = client.post("/auth/login", json={"email": "ada@example.com", "password": "password123"})
    assert login.status_code == 200 and login.json()["role"] == "player"
    me = client.get("/auth/me")
    assert me.status_code == 200 and me.json()["email"] == "ada@example.com"


def test_login_wrong_password_is_401(store):
    token = _invite(store)
    TestClient(app).post(
        "/auth/signup", json={"email": "ada@example.com", "password": "password123", "invite": token},
    )
    r = TestClient(app).post("/auth/login", json={"email": "ada@example.com", "password": "WRONG-password"})
    assert r.status_code == 401


def test_login_unknown_email_is_401(store):
    r = TestClient(app).post("/auth/login", json={"email": "ghost@example.com", "password": "password123"})
    assert r.status_code == 401


def test_bad_invite_is_rejected_without_hashing(store, monkeypatch):
    """argon2 is deliberately expensive; an unauthenticated caller without a
    usable invite must not be able to spend that CPU/memory at will."""
    from service import app as app_module

    calls: list[str] = []
    monkeypatch.setattr(
        app_module, "hash_password", lambda pw: calls.append(pw) or "unused-hash",
    )
    used = _invite(store)
    TestClient(app).post(
        "/auth/signup", json={"email": "first@example.com", "password": "password123", "invite": used},
    )
    calls.clear()

    for bad in ("no-such-token", used):  # unknown, then already-used
        r = TestClient(app).post(
            "/auth/signup",
            json={"email": "x@example.com", "password": "password123", "invite": bad},
        )
        assert r.status_code == 400
    assert calls == [], "password must not be hashed before the invite is validated"


def test_login_verifies_a_hash_even_for_unknown_emails(store, monkeypatch):
    """Otherwise response time alone reveals whether an account exists, and the
    generic 401 message buys nothing."""
    from service import app as app_module

    verifications: list[str] = []
    monkeypatch.setattr(
        app_module, "verify_password",
        lambda stored, pw: verifications.append(stored) or False,
    )
    r = TestClient(app).post(
        "/auth/login", json={"email": "ghost@example.com", "password": "password123"},
    )
    assert r.status_code == 401
    assert len(verifications) == 1, "an unknown email must still cost one verification"
    assert verifications[0] == app_module.DUMMY_PASSWORD_HASH


def test_logout_clears_session(store):
    token = _invite(store)
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "ada@example.com", "password": "password123", "invite": token})
    assert client.get("/auth/me").status_code == 200
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401

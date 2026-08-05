"""How the auth endpoints classify a failure.

`service/app.py` publishes a taxonomy operators alert on — 502 upstream, 503
unavailable, 500 bug — and the auth path has to keep it under every kind of
breakage:

    store outage (connection lost, failover, pool exhausted) -> 503
    domain outcome (taken email, spent invite)               -> 409 / 400
    programming error inside a store                         -> 500
    local hashing saturation                                 -> 503 "busy"

503 rather than 500 for an outage is what tells a client to retry, and 503
rather than 401 is what stops a signed-in tester being sent to Login to
re-enter credentials the backend is in no state to check.

Each test drives a real HTTP request against a store whose methods raise, and
asserts the status a client actually receives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import config
from service import app as app_module
from service.app import app, get_auth_store, get_service
from service.auth_store import EmailTaken, InMemoryAuthStore, User
from service.hashing import HashingCapacityError
from service.invites import InviteError
from service.models import ChatMode, ChatResponse

pytestmark = pytest.mark.real_auth

SECRET = "test-secret-please-rotate-at-least-32-chars"
CREDENTIALS = {"email": "ada@example.com", "password": "password123"}


def store_down() -> Exception:
    """What psycopg raises when the database is gone. The boundary translates
    the concrete connection errors, not `Exception`, so a bespoke test error
    would exercise a boundary the code does not have."""
    import psycopg

    return psycopg.OperationalError("connection refused")


class _FakeService:
    """A healthy RAG service, so a 503 from /chat can only be the auth guard."""

    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(answer="ok", sources=[], answerable=True,
                            mode=ChatMode(mode), conversation_id=conversation_id)


class _ExplodingStore(InMemoryAuthStore):
    """A working store with named methods sabotaged, so each test isolates one
    failure point instead of a uniformly broken backend."""

    def __init__(self, *broken: str, error: Exception | None = None) -> None:
        super().__init__()
        self._broken = set(broken)
        self._error = error or store_down()

    def _guard(self, name: str) -> None:
        if name in self._broken:
            raise self._error

    def get_invite(self, token):
        self._guard("get_invite")
        return super().get_invite(token)

    def get_credentials(self, email):
        self._guard("get_credentials")
        return super().get_credentials(email)

    def get_user_by_id(self, user_id):
        self._guard("get_user_by_id")
        return super().get_user_by_id(user_id)

    def redeem_invite(self, token, email, password_hash, now=None):
        self._guard("redeem_invite")
        return super().redeem_invite(token, email, password_hash, now)


@pytest.fixture(autouse=True)
def _auth_config(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", SECRET)
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    yield
    app.dependency_overrides.clear()


def _use(store, **client_kwargs) -> TestClient:
    app.dependency_overrides[get_auth_store] = lambda: store
    return TestClient(app, **client_kwargs)


def _invite(store, role="player") -> str:
    return store.create_invite(
        role=role, expires_at=datetime.now(UTC) + timedelta(days=1),
    ).token


def _signed_in_client(store) -> TestClient:
    """A client holding a real session cookie, minted before anything breaks."""
    client = _use(store)
    r = client.post("/auth/signup", json={**CREDENTIALS, "invite": _invite(store)})
    assert r.status_code == 200, r.text
    return client


# ── Outage -> 503 ────────────────────────────────────────────────────────────
#
# Asserting the 503 *and its detail* covers the near-misses in one request: not
# 500 (a bug the client should not retry), not 401 (wrong credentials / signed
# out), and not some unrelated 503 from another dependency.


def test_login_outage_is_unavailability_not_bad_credentials():
    r = _use(_ExplodingStore("get_credentials")).post("/auth/login", json=CREDENTIALS)
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "auth backend unavailable"


@pytest.mark.parametrize("broken", ["get_invite", "redeem_invite"])
def test_signup_outage_is_503(broken):
    """Both windows: the cheap precheck, and the atomic redemption itself."""
    store = _ExplodingStore(broken)
    r = _use(store).post("/auth/signup", json={**CREDENTIALS, "invite": _invite(store)})
    assert r.status_code == 503, r.text


def test_me_outage_is_unavailability_not_a_signed_out_session():
    store = _ExplodingStore()
    client = _signed_in_client(store)
    store._broken.add("get_user_by_id")  # noqa: SLF001 - break it mid-session
    r = client.get("/auth/me")
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "auth backend unavailable"


def test_guarded_endpoint_outage_is_503():
    """`require_session` fails CLOSED: an unreadable account is unavailability,
    not permission. The detail is asserted because /chat 503s on its own when
    the RAG service is absent, which would pass a bare status check."""
    store = _ExplodingStore()
    client = _signed_in_client(store)
    app.dependency_overrides[get_service] = lambda: _FakeService()
    store._broken.add("get_user_by_id")  # noqa: SLF001
    r = client.post("/chat", json={"prompt": "hi"})
    assert r.status_code == 503
    assert r.json()["detail"] == "auth backend unavailable", r.text


# ── ...but a domain outcome, and a bug, keep their own status ────────────────


@pytest.mark.parametrize(("error", "expected"), [
    (EmailTaken("An account already exists for this email."), 409),
    (InviteError("This invite link has already been used."), 400),
])
def test_domain_errors_are_not_flattened_into_503(error, expected):
    """A taken email and a spent invite are decisions about the request."""
    store = _ExplodingStore("redeem_invite", error=error)
    r = _use(store).post("/auth/signup", json={**CREDENTIALS, "invite": _invite(store)})
    assert r.status_code == expected, r.text


@pytest.mark.parametrize("bug", [
    TypeError("unsupported operand"),
    IndexError("tuple index out of range"),
    AttributeError("'NoneType' object has no attribute 'id'"),
    KeyError("email"),
])
def test_a_programming_error_in_the_store_is_a_500(bug):
    """Reporting a bug as 503 tells the client to retry something that can never
    succeed, and buries the signal among ordinary outages."""
    # raise_server_exceptions=False so the client sees what a real client sees.
    client = _use(_ExplodingStore("get_credentials", error=bug), raise_server_exceptions=False)
    r = client.post("/auth/login", json=CREDENTIALS)
    assert r.status_code == 500, f"{type(bug).__name__} is a bug, not an outage"


def test_hashing_saturation_keeps_its_own_shed_message():
    """`hash_password` is CPU work in this process, not a store call. Routing it
    through the backend boundary would give a local capacity limit the wrong
    operational story."""
    store = InMemoryAuthStore()
    token = _invite(store)
    client = _use(store)

    def explode(_password: str) -> str:
        raise HashingCapacityError("all hash slots busy")

    with mock.patch.object(app_module, "hash_password", explode):
        r = client.post("/auth/signup", json={**CREDENTIALS, "invite": token})
    assert r.status_code == 503
    assert r.json()["detail"] == "busy, please retry"


# ── One account read per request ─────────────────────────────────────────────


class _CountingStore(InMemoryAuthStore):
    def __init__(self) -> None:
        super().__init__()
        self.by_id_calls = 0

    def get_user_by_id(self, user_id: int) -> User | None:
        self.by_id_calls += 1
        return super().get_user_by_id(user_id)


def test_me_reads_the_account_once():
    """`require_session` re-reads the account on every request by design;
    /auth/me answers from that read rather than repeating the query."""
    store = _CountingStore()
    client = _signed_in_client(store)
    store.by_id_calls = 0

    assert client.get("/auth/me").status_code == 200
    assert store.by_id_calls == 1, f"expected 1 read, got {store.by_id_calls}"

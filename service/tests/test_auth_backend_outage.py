"""Auth endpoints report a broken store as 503, never 500 (PR #43 review).

`get_auth_store` already 503s when the store never came up. The gap was the
store that comes up and *then* breaks — a dropped connection, a Cloud SQL
failover, connections exhausted. Those calls had no error boundary, so the
exception escaped the endpoint as a 500.

That is a real difference, not a cosmetic one:

  * 500 means "this request is broken, retrying will not help"; 503 means "the
    backend is down, retry". The UI, the operator reading status codes, and any
    uptime check all act on that distinction.
  * `docs/deploy-gcp.md` documents `auth unavailable -> 503` as the contract, and
    the startup path already keeps it. A 500 out of /auth/login contradicted the
    runbook the on-call person is reading during the outage.

Each test drives the endpoint through real HTTP with a store whose methods
raise, and asserts the status the client actually receives.
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


def store_down() -> Exception:
    """What psycopg actually raises when the database is gone.

    Using the real class matters: the endpoint boundary translates the concrete
    connection/database errors, NOT `Exception`, so a test double raising some
    bespoke error would be testing a boundary the code no longer has.
    """
    import psycopg

    return psycopg.OperationalError("connection refused")


class _FakeService:
    """A healthy RAG service, so a 503 from /chat can only be the auth guard."""

    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(answer="ok", sources=[], answerable=True,
                            mode=ChatMode(mode), conversation_id=conversation_id)


class _ExplodingStore(InMemoryAuthStore):
    """A working in-memory store with one method sabotaged.

    Subclassing the real fake (rather than a bare mock) keeps every OTHER call on
    the request path behaving normally, so each test isolates one failure point
    instead of a store that is uniformly broken.
    """

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


def _use(store) -> TestClient:
    app.dependency_overrides[get_auth_store] = lambda: store
    return TestClient(app)


def _invite(store, role="player") -> str:
    return store.create_invite(
        role=role, expires_at=datetime.now(UTC) + timedelta(days=1),
    ).token


def _signed_in_client(store) -> TestClient:
    """A client holding a real session cookie, minted before anything breaks."""
    client = _use(store)
    token = _invite(store)
    r = client.post(
        "/auth/signup",
        json={"email": "ada@example.com", "password": "password123", "invite": token},
    )
    assert r.status_code == 200, r.text
    return client


# ── login ────────────────────────────────────────────────────────────────────


def test_login_store_outage_is_503_not_500():
    r = _use(_ExplodingStore("get_credentials")).post(
        "/auth/login", json={"email": "ada@example.com", "password": "password123"},
    )
    assert r.status_code == 503, f"a broken store must not read as a server bug: {r.text}"
    assert r.json()["detail"] == "auth backend unavailable"


def test_login_store_outage_is_never_reported_as_bad_credentials():
    """The dangerous near-miss: swallowing the failure and falling through to the
    generic 401 would tell every tester their password is wrong during an outage,
    and invite a support queue full of password resets that fix nothing."""
    r = _use(_ExplodingStore("get_credentials")).post(
        "/auth/login", json={"email": "ada@example.com", "password": "password123"},
    )
    assert r.status_code != 401


# ── signup ───────────────────────────────────────────────────────────────────


def test_signup_invite_lookup_outage_is_503():
    store = _ExplodingStore("get_invite")
    r = _use(store).post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "password123", "invite": "tok"},
    )
    assert r.status_code == 503


def test_signup_redemption_outage_is_503():
    """The precheck passes and the store dies during the atomic redemption —
    the window where signup previously 500'd with a half-told story."""
    store = _ExplodingStore("redeem_invite")
    token = _invite(store)
    r = _use(store).post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "password123", "invite": token},
    )
    assert r.status_code == 503, r.text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (EmailTaken("An account already exists for this email."), 409),
        (InviteError("This invite link has already been used."), 400),
    ],
)
def test_domain_errors_keep_their_own_status(error, expected):
    """The boundary must not flatten real answers into 503. A taken email is a
    decision about the request (409); a spent invite is too (400)."""
    store = _ExplodingStore("redeem_invite", error=error)
    token = _invite(store)
    r = _use(store).post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "password123", "invite": token},
    )
    assert r.status_code == expected, r.text


# ── /auth/me and the session guard ───────────────────────────────────────────


def test_me_store_outage_is_503_not_500():
    store = _ExplodingStore()
    client = _signed_in_client(store)
    store._broken.add("get_user_by_id")  # noqa: SLF001 - break it mid-session
    r = client.get("/auth/me")
    assert r.status_code == 503, r.text


def test_me_store_outage_does_not_look_like_a_signed_out_session():
    """401 would send a signed-in tester to Login to re-enter credentials that
    cannot be checked — the session is fine, the database is not."""
    store = _ExplodingStore()
    client = _signed_in_client(store)
    store._broken.add("get_user_by_id")  # noqa: SLF001
    assert client.get("/auth/me").status_code != 401


def test_guarded_endpoint_store_outage_is_503():
    """Same rule on the data endpoints: `require_session` fails CLOSED, and an
    unreadable account is unavailability, not a verdict.

    /chat 503s on its own when the RAG service isn't built — and FastAPI resolves
    that dependency first — so a bare status assertion here passes whether or not
    the auth guard does anything. Hence both a working fake service AND a check
    on the detail.
    """
    store = _ExplodingStore()
    client = _signed_in_client(store)
    app.dependency_overrides[get_service] = lambda: _FakeService()
    store._broken.add("get_user_by_id")  # noqa: SLF001
    r = client.post("/chat", json={"prompt": "hi"})
    assert r.status_code == 503
    assert r.json()["detail"] == "auth backend unavailable", (
        "the 503 must come from the session guard, not from the unrelated "
        f"'service not ready' path: {r.text}"
    )


# ── ...but a BUG is still a bug ──────────────────────────────────────────────
#
# The other half of the contract. `service/app.py` documents 502 upstream · 503
# unavailable · 500 bug, and operators alert on that split. A boundary that
# caught `Exception` would relabel every programming error inside a store
# implementation as a retryable 503 — so a client would keep retrying a request
# that can never succeed, and the signal that something is genuinely broken
# would be buried among ordinary outages.


@pytest.mark.parametrize("bug", [
    TypeError("unsupported operand"),
    IndexError("tuple index out of range"),
    AttributeError("'NoneType' object has no attribute 'id'"),
    KeyError("email"),
])
def test_a_programming_error_in_the_store_is_a_500_not_a_503(bug):
    store = _ExplodingStore("get_credentials", error=bug)
    app.dependency_overrides[get_auth_store] = lambda: store
    # raise_server_exceptions=False so the client sees the response a real
    # client would, instead of the exception being re-raised into the test.
    client = TestClient(app, raise_server_exceptions=False)

    r = client.post("/auth/login", json={"email": "a@example.com", "password": "password123"})
    assert r.status_code == 500, (
        f"{type(bug).__name__} is a bug, not an outage — reporting it as "
        f"{r.status_code} tells the client to retry something that cannot work"
    )


def test_hashing_capacity_is_not_reported_as_a_backend_outage():
    """`hash_password` runs in this process, not in the store. Wrapping it in
    the backend boundary would have made a local CPU/memory limit look like a
    database failure — same status, but the wrong operational story, and
    `signup` already sheds that load with its own message."""
    store = InMemoryAuthStore()
    token = _invite(store)
    client = _use(store)

    def explode(_password: str) -> str:
        raise HashingCapacityError("all hash slots busy")

    with mock.patch.object(app_module, "hash_password", explode):
        r = client.post(
            "/auth/signup",
            json={"email": "a@example.com", "password": "password123", "invite": token},
        )
    assert r.status_code == 503
    assert r.json()["detail"] == "busy, please retry"


# ── the duplicate /auth/me query is gone ─────────────────────────────────────


class _CountingStore(InMemoryAuthStore):
    def __init__(self) -> None:
        super().__init__()
        self.by_id_calls = 0

    def get_user_by_id(self, user_id: int) -> User | None:
        self.by_id_calls += 1
        return super().get_user_by_id(user_id)


def test_me_does_not_re_query_the_account_require_session_already_read():
    """`require_session` re-reads the account on every request by design. /auth/me
    ran the identical query a second time — extra load on the auth database and a
    second, unguarded chance to fail."""
    store = _CountingStore()
    client = _use(store)
    token = _invite(store)
    client.post(
        "/auth/signup",
        json={"email": "ada@example.com", "password": "password123", "invite": token},
    )
    store.by_id_calls = 0

    assert client.get("/auth/me").status_code == 200
    assert store.by_id_calls == 1, (
        f"/auth/me should read the account once per request, not {store.by_id_calls} times"
    )

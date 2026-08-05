"""`require_session` guards the data endpoints.

Without a valid session cookie the guarded routes are 401; with one they are
not. /healthz and /metrics/ui stay open. The signing secret must be real or auth
fails closed, and the cookie is never the last word — the account is re-read per
request, so deletion and role changes take effect immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import config
from service.app import app, get_auth_store, get_service, require_session
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


def _authed_client(store) -> TestClient:
    token = store.create_invite(
        role="dm", expires_at=datetime.now(UTC) + timedelta(days=1),
    ).token
    client = TestClient(app)
    client.post("/auth/signup", json={"email": "a@example.com", "password": "password123", "invite": token})
    return client


# Every route behind `require_session`, as a matrix rather than a test each. This
# list IS the security contract: a guarded route missing from it is a route with
# no test that it is guarded, and attachment metadata alone carries filenames
# from other users' uploads. `test_every_session_guarded_route_is_in_the_matrix`
# checks the list against the real routing table so it cannot fall behind.
#: (method, route template as FastAPI knows it, concrete URL, JSON body or None)
Route = tuple[str, str, str, dict[str, object] | None]

PROTECTED_ROUTES: list[Route] = [
    ("POST", "/chat", "/chat", {"prompt": "What is a Basilisk?"}),
    ("GET", "/auth/me", "/auth/me", None),
    ("GET", "/conversations/{conversation_id}/messages",
     "/conversations/abc/messages", None),
    ("GET", "/conversations/{conversation_id}/attachments",
     "/conversations/abc/attachments", None),
    ("POST", "/conversations/{conversation_id}/attachments",
     "/conversations/abc/attachments",
     {"filename": "x.txt", "content_type": "text/plain", "data": "aGk="}),
]

#: Deliberately unguarded, and asserted so that a blanket "guard everything"
#: change has to be a conscious decision: /healthz is what the platform probes
#: (a 401 there takes the service down), /metrics/ui is the pre-auth beacon.
OPEN_ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/healthz", None),
    ("POST", "/metrics/ui", {"points": []}),
]


def _request(client: TestClient, method: str, path: str, body):
    return client.request(method, path, json=body) if body is not None \
        else client.request(method, path)


@pytest.mark.parametrize(("method", "template", "path", "body"), PROTECTED_ROUTES)
def test_protected_route_without_session_is_401(store, method, template, path, body):
    r = _request(TestClient(app), method, path, body)
    assert r.status_code == 401, f"{method} {template} answered {r.status_code} with no session"


@pytest.mark.parametrize(("method", "template", "path", "body"), PROTECTED_ROUTES)
def test_protected_route_with_session_is_not_401(store, method, template, path, body):
    """The other half of the matrix. Without it a route could 'pass' the guard
    test by being broken — 401 for everyone, signed in or not — and the suite
    would report the security property as intact."""
    r = _request(_authed_client(store), method, path, body)
    assert r.status_code != 401, f"{method} {template} rejected a VALID session"


@pytest.mark.parametrize(("method", "path", "body"), OPEN_ROUTES)
def test_open_route_stays_open(store, method, path, body):
    assert _request(TestClient(app), method, path, body).status_code != 401


def test_every_session_guarded_route_is_in_the_matrix(store):
    """The matrix cannot silently fall behind the app.

    Walk the real routing table for anything depending on `require_session` and
    require it to be listed above. A new guarded endpoint that nobody adds here
    would otherwise ship with no test that it is guarded at all.
    """
    listed = {(method, template) for method, template, _, _ in PROTECTED_ROUTES}
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        if not any(d.call is require_session for d in dependant.dependencies):
            continue
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
            assert (method, route.path) in listed, (
                f"{method} {route.path} is behind require_session but is missing "
                f"from PROTECTED_ROUTES — add it, so the guard is actually tested"
            )


def test_chat_with_session_is_200(store):
    r = _authed_client(store).post("/chat", json={"prompt": "What is a Basilisk?"})
    assert r.status_code == 200
    assert r.json()["answerable"] is True


# ── A weak/known signing secret must disable auth, not sign with it ──────────


@pytest.mark.parametrize(
    "secret",
    [
        "",                                   # unset
        "   ",                                # whitespace-only
        " " * 40,                             # long enough to pass a naive length check
        "\t\n  \t",                           # other whitespace
        "replace-me-with-a-random-string",    # the shipped .env.example placeholder
        "CHANGEME",                           # case-insensitive placeholder
        "  replace-me-with-a-random-string ", # placeholder with padding
        "short-secret",                       # brute-forceable
        "  short  ",                          # short once trimmed
    ],
)
def test_placeholder_or_weak_secret_disables_auth(store, monkeypatch, secret):
    """A secret that ships in a template is public by definition — signing with
    it would let anyone forge a session (including a DM one). Fail closed."""
    monkeypatch.setattr(config, "SESSION_SECRET", secret)
    r = TestClient(app).post(
        "/auth/login", json={"email": "a@example.com", "password": "password123"},
    )
    assert r.status_code == 503, f"expected auth disabled for secret {secret!r}"


# ── The cookie is not the last word ──────────────────────────────────────────


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

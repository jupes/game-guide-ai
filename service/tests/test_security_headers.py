"""The service sends the Content-Security-Policy, and means it (va8).

Four response shapes, because the middleware has to cover all of them: a plain
API response, a handled error, the SPA document served by the StaticFiles mount
in production, and a route that sets a stricter policy for itself.

Plus the one test that makes the other three worth having. Every other check in
this change compares the policy against something the same change wrote — the
nginx contract test compares two files we control, the E2E test reads the value
out of nginx.conf. `img-src *` would sail through all of them. The two tests at
the bottom pin what the policy MEANS, absolutely.

Not asserted anywhere, deliberately: that EVERY response carries the header. A
500 from an unhandled exception is produced by Starlette's
`ServerErrorMiddleware`, outside all user middleware, so it carries no policy.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import Response
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from service.app import app, get_auth_store
from service.auth_store import InMemoryAuthStore
from service.security_headers import CONTENT_SECURITY_POLICY

# The 401 case needs the REAL guard: service/tests/conftest.py's autouse
# `_default_session` fixture overrides `require_session` for every test that is
# not marked `real_auth`, and without this mark that test would be asserting the
# header on a 200.
pytestmark = pytest.mark.real_auth


@pytest.fixture
def store():
    """An in-memory auth store, because `get_auth_store` 503s when the lifespan
    has not run — a 401 test that skipped this would be pinning a 503."""
    app.dependency_overrides[get_auth_store] = lambda: InMemoryAuthStore()
    yield
    app.dependency_overrides.clear()


def test_an_api_response_carries_the_policy() -> None:
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_a_handled_error_response_carries_the_policy(store) -> None:
    """A 401 is exactly when a reader is most exposed to an injected page, and
    it is produced by an HTTPException — which passes back out through the user
    middleware stack."""
    response = TestClient(app).get("/auth/me")
    assert response.status_code == 401
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_the_spa_document_from_the_static_mount_carries_the_policy() -> None:
    """In production one process serves the API and the built SPA (app.py mounts
    ui/dist at "/"), so the document itself has to carry the header — that is the
    response whose policy governs everything the page then loads."""
    monkeypatch_marker = "<html>ui</html>"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "index.html").write_text(monkeypatch_marker)
        saved_routes = list(app.router.routes)
        # Drop a real ui/dist mount if this checkout has one built, so the test
        # serves ITS index.html and not whatever happens to be on disk.
        app.router.routes[:] = [r for r in saved_routes if getattr(r, "name", None) != "ui"]
        app.mount("/", StaticFiles(directory=tmp, html=True), name="_test_static")
        try:
            response = TestClient(app).get("/")
            assert response.status_code == 200
            assert response.text == monkeypatch_marker, "served something other than the test's index.html"
            assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        finally:
            app.router.routes[:] = saved_routes


def test_a_route_that_sets_its_own_policy_keeps_it() -> None:
    """`setdefault`, not assignment. SEC-19 requires asset responses to answer
    with their own `default-src 'none'; sandbox`, and that bead must not have to
    unpick this middleware to do it."""
    stricter = "default-src 'none'; sandbox"
    saved_routes = list(app.router.routes)

    @app.get("/_va8_route_with_its_own_policy")
    def _route_with_its_own_policy() -> Response:
        return Response(content="x", headers={"Content-Security-Policy": stricter})

    try:
        response = TestClient(app).get("/_va8_route_with_its_own_policy")
        assert response.status_code == 200
        assert response.headers["content-security-policy"] == stricter
    finally:
        app.router.routes[:] = saved_routes


# ── What the policy MEANS, not merely what it equals somewhere else ──────────


def test_the_policy_says_exactly_what_it_is_supposed_to_say() -> None:
    directives = {}
    for part in CONTENT_SECURITY_POLICY.split(";"):
        name, *sources = part.split()
        directives[name] = sources

    assert set(directives) == {"img-src", "connect-src", "object-src", "base-uri", "frame-ancestors"}
    # `img-src` and `connect-src` ARE the exfiltration channel this bead closes:
    # a remote image URL and a fetch to a third-party origin. If either grows a
    # source that is not this origin, the bug is back.
    assert directives["img-src"] == ["'self'", "data:", "blob:"]
    assert directives["connect-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'self'"]
    assert directives["frame-ancestors"] == ["'none'"]


def test_the_policy_string_is_pinned_character_for_character() -> None:
    """Editing this literal is a deliberate act: it changes what every reader's
    browser is allowed to fetch. The parse test above catches a wrong source
    list; this one also catches a typo a browser would silently ignore, like
    `base-url` for `base-uri` or a missing space after a semicolon."""
    assert CONTENT_SECURITY_POLICY == (
        "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'"
    )

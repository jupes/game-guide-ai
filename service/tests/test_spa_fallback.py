"""
service/tests/test_spa_fallback.py -- agent-forge-harness-y40, R10.

Proves the cold-load allowlist against FastAPI directly, not against nginx:
nginx's `try_files` is a catch-all, so a browser E2E spec "passing" there
proves nothing about production, where `service/app.py` + `StaticFiles` is
the only thing standing between a deep link and a 404
(docs/adr/client-routing.md has the fact table this fixes).

The routing table under test is built FRESH for every test in this file, in
`_fresh_app()` -- NEVER by calling `install_spa` on the `service.app.app`
singleton. FastAPI has no unmount; `service/tests/conftest.py` already lives
on that singleton for the whole suite, and a mount added there would leak
into every later test file in the session, making
`test_missing_dist_registers_nothing_and_chat_is_405` pass or fail depending
on pytest's file ordering. `_fresh_app()` gives byte-identical answers
whether or not this checkout has a real `ui/dist` built, because it removes
exactly the routes `install_spa` would have added and nothing else.

The one exception, and why it is safe: `test_the_csp_middleware_still_wraps_an_allowlisted_route`
drives a request through the REAL `service.app.app` (LA-3) because that is
the only way to exercise its actual middleware stack -- a fresh `FastAPI()`
carries none. It follows the exact save/mutate/restore pattern already used
by `service/tests/test_app.py::test_chat_resolves_with_static_mount` and
`service/tests/test_security_headers.py::test_the_spa_document_from_the_static_mount_carries_the_policy`
(both un-edited by this change): capture `app.router.routes`, install onto
the singleton, assert, and restore the exact prior list in `finally` --
so nothing survives the test even if an assertion raises.
"""

from __future__ import annotations

import ast
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

import service.app as service_app
from service.security_headers import CONTENT_SECURITY_POLICY
from service.spa_fallback import (
    CLIENT_ROUTES,
    SPA_MOUNT_NAME,
    SPA_ROUTE_PREFIX,
    install_spa,
)

APP_PY = Path(service_app.__file__)


def _is_spa_route(route: object) -> bool:
    name = getattr(route, "name", "") or ""
    return name == SPA_MOUNT_NAME or name.startswith(SPA_ROUTE_PREFIX)


def _fresh_app() -> FastAPI:
    """A FastAPI carrying every real API route and NONE of the SPA fallback.

    Verified (alignment doc, section 2) to answer every one of `/profile`,
    `/`, `/nope`, `/chat`, `/assets/app-abc.js`, `/table/x` byte-identically
    whether or not the singleton this filters from has a real `ui/dist`
    mounted -- because the filter removes exactly what `install_spa` would
    have added, by name, and nothing else.
    """
    api_routes = [r for r in service_app.app.routes if not _is_spa_route(r)]

    # Anti-vacuity guards -- run unconditionally, fail loudly. If these ever
    # stop holding (e.g. a rename in spa_fallback.py the filter above wasn't
    # updated for), the table this builds is not what its name claims.
    assert api_routes, "the filtered route list is empty -- did the app fail to import its routes?"
    assert not any(isinstance(r, Mount) for r in api_routes), (
        "a Mount survived the SPA-route filter -- SPA_MOUNT_NAME must have "
        "changed without this filter being updated"
    )
    assert not any(getattr(r, "path", None) in CLIENT_ROUTES for r in api_routes), (
        "a CLIENT_ROUTES path survived the SPA-route filter -- SPA_ROUTE_PREFIX "
        "must have changed without this filter being updated"
    )

    fresh = FastAPI()
    fresh.router.routes = list(api_routes)
    return fresh


@pytest.fixture
def tmp_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<html><body>test-fixture-index</body></html>", encoding="utf-8",
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app-abc123.js").write_text(
        "console.log('test-fixture-asset');", encoding="utf-8",
    )
    return dist


@pytest.fixture
def routed_client(tmp_dist: Path) -> TestClient:
    app = _fresh_app()
    install_spa(app, tmp_dist)
    return TestClient(app)


# ── A1 -- cold load of an allowlisted path ───────────────────────────────────


@pytest.mark.parametrize("path", ["/profile", "/workspace", "/"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_allowlisted_path_serves_index_html(
    routed_client: TestClient, tmp_dist: Path, path: str, method: str,
) -> None:
    response = routed_client.request(method, path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    if method == "GET":
        assert response.content == (tmp_dist / "index.html").read_bytes()


# ── A2 -- the fallback is an allowlist, not a catch-all ──────────────────────


@pytest.mark.parametrize(
    "path",
    ["/nope", "/verify", "/reset", "/billing/return", "/table/x", "/t/abc", "/profile/"],
)
def test_unknown_path_is_a_json_404(routed_client: TestClient, path: str) -> None:
    response = routed_client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "Not Found"}


def test_chat_is_404_not_405_when_the_spa_is_mounted(routed_client: TestClient) -> None:
    """Different from a service-only deployment (405 there --
    `test_missing_dist_registers_nothing_and_chat_is_405`, below): a
    `Mount("/")` takes the full unmatched path, so GET /chat never reaches
    the 405 that "wrong method on a real route" would otherwise produce.
    Recorded as a known behaviour change in docs/adr/client-routing.md."""
    response = routed_client.get("/chat")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"


def test_healthz_is_unaffected(routed_client: TestClient) -> None:
    response = routed_client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"


# ── A3 -- /assets is not shadowed, and API routes still win ──────────────────


def test_assets_are_not_shadowed(routed_client: TestClient, tmp_dist: Path) -> None:
    response = routed_client.get("/assets/app-abc123.js")
    assert response.status_code == 200
    assert response.content == (tmp_dist / "assets" / "app-abc123.js").read_bytes()
    # Deliberately not asserting the exact subtype: `mimetypes` reads the
    # Windows registry, so it is a per-machine fact on this dev box and would
    # differ from CI's Linux runner. Only the shape that matters is pinned.
    assert not response.headers["content-type"].startswith("text/html")


def test_post_chat_reaches_the_api(routed_client: TestClient) -> None:
    response = routed_client.post("/chat", json={})
    # Never text/html: whatever status this is (422 for a bad body, most
    # likely), it came from the API, not from the SPA mount swallowing it.
    assert not response.headers["content-type"].startswith("text/html")


# ── A4 -- nothing is registered after the SPA installer ─────────────────────


def _install_spa_call_line(tree: ast.Module) -> int:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "install_spa"
        ):
            return node.lineno
    raise AssertionError("no install_spa(...) call found in service/app.py")


def test_spa_fallback_is_registered_after_every_api_route() -> None:
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    install_line = _install_spa_call_line(tree)

    registration_methods = {"get", "post", "put", "patch", "delete", "add_api_route", "include_router"}
    offending: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "app"):
            continue
        if func.attr not in registration_methods:
            continue
        if node.lineno > install_line:
            offending.append(f"app.{func.attr}(...) at line {node.lineno}")

    assert not offending, (
        f"route registration(s) found AFTER install_spa(...) (line {install_line}) in "
        f"service/app.py: {offending}. A Mount('/') matches everything, so anything "
        "registered after it is unreachable."
    )


# ── A5 -- with no ui/dist, the service behaves exactly as today ─────────────


def test_missing_dist_registers_nothing_and_chat_is_405(tmp_path: Path) -> None:
    app = _fresh_app()
    before = list(app.router.routes)

    install_spa(app, tmp_path / "does-not-exist")

    assert list(app.router.routes) == before, "install_spa registered something despite a missing dist"

    client = TestClient(app)
    assert client.get("/profile").status_code == 404
    # No StaticFiles mount at all, so an unregistered method on a real path
    # gets FastAPI's ordinary 405 -- not a Mount's blanket 404. `app` is
    # fresh, so this cannot depend on pytest's file ordering.
    assert client.get("/chat").status_code == 405


# ── LA-3 -- the CSP middleware still wraps an allowlisted route ─────────────


@pytest.fixture
def _spa_installed_on_the_real_app(tmp_dist: Path) -> Iterator[None]:
    """Install onto the REAL singleton -- the only way to exercise its actual
    middleware stack (LA-3: "R10's fresh FastAPI() carries no middleware, so
    it cannot be this proof") -- and restore it unconditionally. `install_spa`
    only ever APPENDS to `app.router.routes`, so replacing the list with the
    exact snapshot taken before this fixture ran fully undoes it, the same
    way `test_app.py::test_chat_resolves_with_static_mount` and
    `test_security_headers.py::test_the_spa_document_from_the_static_mount_carries_the_policy`
    already do for their own temporary mounts."""
    saved_routes = list(service_app.app.routes)
    install_spa(service_app.app, tmp_dist)
    try:
        yield
    finally:
        service_app.app.router.routes[:] = saved_routes


def test_the_csp_middleware_still_wraps_an_allowlisted_route(
    _spa_installed_on_the_real_app: None,
) -> None:
    """`set_security_headers` (`service/app.py`) wraps the router, so it
    should already apply to `/`, `/workspace` and `/profile` now that they
    are API routes rather than files under the StaticFiles mount -- prove it
    rather than assume it (LA-3)."""
    response = TestClient(service_app.app).get("/profile")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_the_csp_header_is_absent_when_the_middleware_is_bypassed() -> None:
    """The red half of the test above: talk to a fresh app that carries the
    SAME allowlisted route but NONE of the real app's middleware, and show
    the header is genuinely absent -- proving the assertion above is actually
    exercising the middleware, not something the allowlist route sets on its
    own."""
    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp)
        (dist / "index.html").write_text("<html>bypassed</html>", encoding="utf-8")
        bypassed = _fresh_app()  # carries the API routes but no middleware
        install_spa(bypassed, dist)

        response = TestClient(bypassed).get("/profile")
        assert response.status_code == 200
        assert "content-security-policy" not in {k.lower() for k in response.headers}

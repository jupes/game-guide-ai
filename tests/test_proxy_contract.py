"""
Proxy-contract guard: every service API prefix must be proxied by BOTH front ends.

The SPA is served by nginx (compose/production UI) and by the Vite dev server
(local dev). Both send unmatched paths to the SPA fallback, so an API prefix
that isn't explicitly proxied gets swallowed: a GET quietly returns index.html
(HTML where JSON was expected — that was bug `cnqf`) and a POST returns **405**,
because a static file can't take one. That is exactly how `/auth/*` broke the
browser E2E when auth landed (x5bz.2).

This derives the prefixes from the real route table, so a NEW endpoint that
nobody proxied fails here instead of in a browser.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_proxy_contract.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

import service.app as service_app
from service.spa_fallback import SPA_MOUNT_NAME, SPA_ROUTE_PREFIX, install_spa
from service.workbench_api import api_routes

REPO_ROOT = Path(__file__).resolve().parent.parent
NGINX_CONF = REPO_ROOT / "ui" / "nginx.conf"
VITE_CONFIG = REPO_ROOT / "ui" / "vite.config.ts"

# `/` is the SPA mount itself, not an API prefix to proxy.
_NOT_AN_API_PREFIX = {""}

#: Served by the app and deliberately NOT proxied by either front end, so that
#: no browser can reach it. `/internal/jobs` (`1kg.2.7`) is the job runner's
#: endpoint, invoked from inside the platform only; nginx and Vite must never
#: forward it. Excluded here explicitly — it used to be merely invisible to a
#: regex over decorator syntax, which is not a rule anybody could check.
_DELIBERATELY_UNPROXIED = frozenset({"internal"})


def _is_spa(route: APIRoute) -> bool:
    """The SPA fallback's routes, by NAME (lead ruling Q-13): a locally built
    `ui/dist` must not add `/workspace` or `/profile` as API prefixes."""
    return route.name == SPA_MOUNT_NAME or route.name.startswith(SPA_ROUTE_PREFIX)


def _all_prefixes_of(app: FastAPI) -> set[str]:
    """Top-level path segment of every API route `app` serves, however it was
    declared — `@app.<method>`, `add_api_route` or a mounted router.

    Through `api_routes()`, not `app.routes` (which does not hold router-mounted
    routes) and not `app.openapi()["paths"]` (which omits a route declared with
    `include_in_schema=False`: the guard would go blind the same way).
    """
    paths = [path for path, route in api_routes(app) if not _is_spa(route)]
    assert paths, "found no API routes on the app — did the route model change?"
    return {p.strip("/").split("/")[0] for p in paths} - _NOT_AN_API_PREFIX


def _prefixes_of(app: FastAPI) -> set[str]:
    """The prefixes both front ends must proxy."""
    return _all_prefixes_of(app) - _DELIBERATELY_UNPROXIED


def _route_prefixes() -> set[str]:
    """The prefixes of the real service app."""
    return _prefixes_of(service_app.app)


def test_every_api_prefix_is_proxied_by_nginx_and_vite() -> None:
    prefixes = _route_prefixes()
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    vite = VITE_CONFIG.read_text(encoding="utf-8")

    missing_nginx = sorted(p for p in prefixes if not re.search(rf"location\s+/{p}\b", nginx))
    missing_vite = sorted(p for p in prefixes if not re.search(rf"['\"]/{p}['\"]\s*:", vite))

    assert not missing_nginx, (
        f"ui/nginx.conf does not proxy: {missing_nginx}. Unproxied API prefixes fall through "
        "to the SPA fallback (GET -> index.html, POST -> 405)."
    )
    assert not missing_vite, (
        f"ui/vite.config.ts server.proxy does not proxy: {missing_vite}. The dev server would "
        "serve the SPA for these instead of forwarding them to the service."
    )


def test_auth_prefix_is_proxied() -> None:
    """Explicit regression pin for the /auth 405 that broke the E2E (x5bz.2):
    signup/login POSTs must reach the service through both front ends."""
    assert "auth" in _route_prefixes(), "expected /auth/* routes on the service app"
    assert re.search(r"location\s+/auth\b", NGINX_CONF.read_text(encoding="utf-8"))
    assert re.search(r"['\"]/auth['\"]\s*:", VITE_CONFIG.read_text(encoding="utf-8"))


def test_a_router_mounted_route_is_visible_to_the_proxy_guard() -> None:
    """The guard used to read `service/app.py` as text and see only
    `@app.<method>` decorators: a route on a router was invisible to it, and it
    kept passing while covering less (agent-forge-harness-oe6)."""
    app = FastAPI()
    router = APIRouter()

    @router.get("/campaigns/{campaign_id}/assets")
    def list_assets(campaign_id: str) -> list[str]:
        return []

    app.include_router(router)
    assert "campaigns" in _prefixes_of(app)


def test_deliberately_unproxied_prefixes_are_excluded_and_never_proxied() -> None:
    """Exercised on an app that HAS such a route: on the real app the set is
    empty today, and the exclusion would be a no-op nothing checks."""
    assert _DELIBERATELY_UNPROXIED == frozenset({"internal"})
    app = FastAPI()

    @app.post("/internal/jobs")
    def run_jobs() -> dict[str, str]:
        return {}

    assert "internal" in _all_prefixes_of(app)  # the route is really there
    assert "internal" not in _prefixes_of(app)  # and the exclusion removed it
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    vite = VITE_CONFIG.read_text(encoding="utf-8")
    for prefix in sorted(_DELIBERATELY_UNPROXIED):
        assert not re.search(rf"location\s+/{prefix}\b", nginx), f"ui/nginx.conf proxies /{prefix}"
        assert not re.search(rf"['\"]/{prefix}['\"]\s*:", vite), f"ui/vite.config.ts proxies /{prefix}"


def test_the_spa_fallback_is_not_an_api_prefix(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {}

    install_spa(app, tmp_path)
    served = {path for path, _ in api_routes(app)}
    assert served == {"/healthz", "/", "/workspace", "/profile"}  # the SPA routes are really there
    assert _prefixes_of(app) == {"healthz"}

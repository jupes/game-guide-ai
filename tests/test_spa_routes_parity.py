"""
tests/test_spa_routes_parity.py -- agent-forge-harness-y40, R11.

The client's route table (`ui/src/shell/routes.ts`) and the server's
allowlist (`service.spa_fallback.CLIENT_ROUTES`) are two files with nothing
to force them to agree. This is the same shape of problem
`tests/test_proxy_contract.py` solves for nginx/vite.config.ts, and that file
is the precedent for reading a TypeScript file from a Python test.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_spa_routes_parity.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI

import service.app as service_app
from service.spa_fallback import CLIENT_ROUTES, SPA_MOUNT_NAME, SPA_ROUTE_PREFIX
from service.workbench_api import api_routes

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_TS = REPO_ROOT / "ui" / "src" / "shell" / "routes.ts"

# Named reserved families that are NOT (yet) API prefixes, so they cannot be
# read off the live route table the way the six proxied prefixes below are:
#   - "/assets" -- MS-7 (media-and-realtime.md:89): the built bundle's own path.
#   - "/table", "/table-sessions" -- SEC-18 / threat-model.md:328: the table
#     client is a separate entry point and bundle with its own principal.
#   - "/t/" -- expansion plan (:268): the alternative `/t/<token>` player
#     entry point. Reserved WITH the trailing slash, so it blocks `/t/x` and
#     not `/tavern` (lead ruling Q4).
#   - "/campaigns", "/documents", "/tool-invocations", "/cues" -- the
#     "Proposed route families" (expansion plan :258-271), not yet built.
# Deliberately NOT here: "/internal" -- no source names it (verified: the
# string does not occur in the expansion plan).
_NAMED_RESERVED_PREFIXES = (
    "/assets",
    "/table-sessions",
    "/table",
    "/t/",
    "/campaigns",
    "/documents",
    "/tool-invocations",
    "/cues",
)

_ROUTE_ROW_PATH = re.compile(r"path:\s*'([^']*)'")


def _client_route_paths() -> list[str]:
    text = ROUTES_TS.read_text(encoding="utf-8")
    return _ROUTE_ROW_PATH.findall(text)


def _is_spa_route(route: object) -> bool:
    name = getattr(route, "name", "") or ""
    return name == SPA_MOUNT_NAME or name.startswith(SPA_ROUTE_PREFIX)


def _live_api_prefixes(app: FastAPI = service_app.app) -> set[str]:
    """Top-level path segment of every live, non-SPA route on the real app --
    mirrors `tests/test_proxy_contract.py`'s `_route_prefixes()`, and like it
    reads the live route table through `service.workbench_api.api_routes()`,
    which sees routes added with `add_api_route` and routers mounted with
    `include_router` (`app.routes` does not hold the latter at all). Filtered
    by name the same way `service/tests/test_spa_fallback.py` filters, so a
    locally built `ui/dist` cannot change the answer. FastAPI's own
    documentation routes are not API routes, so `api_routes()` does not report
    them; they are read from the app's settings instead and stay reserved."""
    prefixes: set[str] = set()
    documentation = (app.openapi_url, app.docs_url, app.redoc_url)
    paths = [path for path, route in api_routes(app) if not _is_spa_route(route)]
    for path in [*paths, *(url for url in documentation if url)]:
        if not path or path == "/":
            continue
        prefixes.add("/" + path.strip("/").split("/")[0])
    return prefixes


def test_client_and_server_tables_agree() -> None:
    client_paths = _client_route_paths()
    assert set(client_paths) == set(CLIENT_ROUTES), (
        "ui/src/shell/routes.ts and service.spa_fallback.CLIENT_ROUTES disagree.\n"
        f"  client: {sorted(client_paths)}\n"
        f"  server: {sorted(CLIENT_ROUTES)}"
    )


def test_at_least_three_rows_were_found() -> None:
    # A regex that silently stops matching (a row shape change, a quoting
    # change) must fail loudly here, not pass by comparing two empty sets.
    paths = _client_route_paths()
    assert len(paths) >= 3, (
        f"found only {len(paths)} row(s) in ui/src/shell/routes.ts ({paths}) -- "
        "did the row shape change, or did the parser regex stop matching?"
    )


def test_no_client_path_collides_with_a_reserved_prefix() -> None:
    live_prefixes = _live_api_prefixes()
    assert live_prefixes, "found no live API route prefixes to check against"
    reserved_prefixes = tuple(live_prefixes) + _NAMED_RESERVED_PREFIXES

    offenders = {
        client_path: prefix
        for client_path in _client_route_paths()
        for prefix in reserved_prefixes
        if client_path.startswith(prefix)
    }
    assert not offenders, (
        f"client route(s) in ui/src/shell/routes.ts collide with a reserved API "
        f"prefix (nginx/vite proxy by STRING prefix, not path segment): {offenders}"
    )

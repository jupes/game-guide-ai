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

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_PY = REPO_ROOT / "service" / "app.py"
NGINX_CONF = REPO_ROOT / "ui" / "nginx.conf"
VITE_CONFIG = REPO_ROOT / "ui" / "vite.config.ts"

# `/` is the SPA mount itself, not an API prefix to proxy.
_NOT_AN_API_PREFIX = {""}


def _route_prefixes() -> set[str]:
    """Top-level path segment of every @app.<method>("/...") route."""
    text = APP_PY.read_text(encoding="utf-8")
    paths = re.findall(r"""@app\.(?:get|post|put|patch|delete)\(\s*["'](/[^"']*)["']""", text)
    assert paths, "found no @app routes in service/app.py — did the decorator style change?"
    prefixes = {p.strip("/").split("/")[0] for p in paths}
    return prefixes - _NOT_AN_API_PREFIX


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

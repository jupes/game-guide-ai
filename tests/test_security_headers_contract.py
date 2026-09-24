"""
One policy, two hosts, zero drift (agent-forge-harness-va8).

Two front ends serve this app. nginx serves the SPA in Compose and in the
browser E2E; in production one FastAPI process serves both the API and the
built SPA from its StaticFiles mount. Threat model R-6 is explicit that a
security header set in only one of them means the E2E tests a different posture
from the thing that ships — so the policy is written once, in
`service/security_headers.py`, and this test is what keeps `ui/nginx.conf`
equal to it.

It is also the only half of the two-host equality that can be proved without
Docker: edit either source alone and this goes red. The other half — that a
real browser is actually SERVED the header — is `ui/e2e/security.spec.ts`.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_security_headers_contract.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

from service.security_headers import CONTENT_SECURITY_POLICY

REPO_ROOT = Path(__file__).resolve().parent.parent
NGINX_CONF = REPO_ROOT / "ui" / "nginx.conf"

_DECLARATION = re.compile(r'add_header\s+Content-Security-Policy\s+"([^"]*)"\s+always;')

# Matched as DIRECTIVES — anchored at the start of a line — not as bare words.
# `#` comments in this file legitimately talk about `location` blocks and about
# `add_header`, and a guard that a comment can move or silence is not a guard.
_LOCATION_DIRECTIVE = re.compile(r"^\s*location\s", re.MULTILINE)
_ADD_HEADER_DIRECTIVE = re.compile(r"^\s*add_header\s", re.MULTILINE)
_CSP_DIRECTIVE = re.compile(r"^\s*add_header\s+Content-Security-Policy\b", re.MULTILINE)


def test_nginx_declares_the_same_policy_exactly_once() -> None:
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    matches = _DECLARATION.findall(nginx)

    # Count FIRST. Asserting only on the value is vacuous the day the regex
    # stops matching: `findall` returns [], no comparison runs, and the test
    # reports the policy as intact while nginx sends nothing.
    assert len(matches) == 1, (
        f"expected exactly one `add_header Content-Security-Policy \"…\" always;` in "
        f"ui/nginx.conf, found {len(matches)}. Two declarations at the same level do "
        "not merge into a stronger policy — the browser enforces both, and the "
        "stricter one wins in ways nobody reading either line would predict."
    )
    assert matches[0] == CONTENT_SECURITY_POLICY, (
        "ui/nginx.conf and service/security_headers.py send different policies. "
        "nginx serves the SPA in Compose and in the E2E; the service serves it in "
        "production. A difference means the browser test and production enforce "
        "different rules (threat model R-6).\n"
        f"  nginx:   {matches[0]!r}\n"
        f"  service: {CONTENT_SECURITY_POLICY!r}"
    )


def test_the_policy_is_declared_above_every_location_and_no_location_overrides_it() -> None:
    nginx = NGINX_CONF.read_text(encoding="utf-8")

    first_location = _LOCATION_DIRECTIVE.search(nginx)
    csp = _CSP_DIRECTIVE.search(nginx)
    # Positive controls: both of these are `.search()` results that can be None,
    # and every comparison below would be skipped or crash misleadingly without
    # them.
    assert first_location is not None, "ui/nginx.conf declares no `location` block at all"
    assert csp is not None, "ui/nginx.conf declares no `add_header Content-Security-Policy` directive"

    assert csp.start() < first_location.start(), (
        "the Content-Security-Policy must be declared in the `server` block, above the "
        "first `location`, so every location inherits it."
    )

    inside_locations = [
        m.group(0).strip()
        for m in _ADD_HEADER_DIRECTIVE.finditer(nginx)
        if m.start() > first_location.start()
    ]
    assert inside_locations == [], (
        "a location block that defines its own add_header loses the server-level "
        "Content-Security-Policy: nginx does not merge them. Re-declare it there."
    )

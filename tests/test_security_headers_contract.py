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

# `location` is still matched as a DIRECTIVE — anchored at the start of a line —
# because that anchor is correct for it: nginx directives are one per line, and
# `location` never legitimately shares a line with anything before it.
_LOCATION_DIRECTIVE = re.compile(r"^\s*location\s", re.MULTILINE)
_CSP_DIRECTIVE = re.compile(r"^\s*add_header\s+Content-Security-Policy\b", re.MULTILINE)

# `add_header` is matched as a bare WORD, deliberately not anchored to the
# start of a line: `location /x { add_header ...; }` is a single line, so an
# add_header there never starts one, and the line-anchored regex this replaced
# (`^\s*add_header\s`) missed it entirely (agent-forge-harness-1q7). Matching
# the bare word instead means a `#` comment that merely talks about
# `add_header` — ui/nginx.conf's own header comment does, twice — would now be
# a false positive, so `_add_header_word_positions` below filters those out.
_ADD_HEADER_WORD = re.compile(r"\badd_header\b")


def _add_header_word_positions(nginx_conf: str) -> list[int]:
    """
    Start offsets of every `add_header` DIRECTIVE in `nginx_conf` — every bare
    word `add_header`, except one that a `#` earlier on the same line has
    turned into comment prose rather than a directive nginx would execute.

    A pure function of its argument, so a synthetic string proves this catches
    a one-line `location { add_header ...; }` — and does not fire on a
    comment — without touching the real ui/nginx.conf (agent-forge-harness-1q7).
    """
    positions = []
    for match in _ADD_HEADER_WORD.finditer(nginx_conf):
        line_start = nginx_conf.rfind("\n", 0, match.start()) + 1
        if "#" in nginx_conf[line_start : match.start()]:
            continue  # commented out: a `#` earlier on this same line
        positions.append(match.start())
    return positions


def locations_with_their_own_add_header(nginx_conf: str) -> list[int]:
    """
    Start offsets of every `add_header` DIRECTIVE that appears anywhere after
    the first `location` block starts — including on the SAME line as
    `location {`, which is exactly the case a line-anchored regex could not
    see (agent-forge-harness-1q7). Empty when `nginx_conf` declares no
    `location` at all, or when none of its `add_header` directives are after
    one.
    """
    first_location = _LOCATION_DIRECTIVE.search(nginx_conf)
    if first_location is None:
        return []
    return [pos for pos in _add_header_word_positions(nginx_conf) if pos > first_location.start()]


def test_nginx_declares_the_same_policy_exactly_once() -> None:
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    matches = _DECLARATION.findall(nginx)

    # Count FIRST. Asserting only on the value is vacuous the day the regex
    # stops matching: `findall` returns [], no comparison runs, and the test
    # reports the policy as intact while nginx sends nothing.
    assert len(matches) == 1, (
        f'expected exactly one `add_header Content-Security-Policy "…" always;` in '
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

    inside_locations = locations_with_their_own_add_header(nginx)
    assert inside_locations == [], (
        "a location block that defines its own add_header loses the server-level "
        "Content-Security-Policy: nginx does not merge them. Re-declare it there."
    )


def test_an_add_header_sharing_a_line_with_its_location_is_still_caught() -> None:
    # agent-forge-harness-1q7: the regex this replaced (`^\s*add_header\s`) was
    # anchored to the start of a line, so an add_header written on the SAME
    # line as its `location {` never started one and evaded it entirely. This
    # is a synthetic string, not ui/nginx.conf, precisely so it can plant that
    # one-line shape without touching the real file.
    synthetic = (
        "server {\n"
        "    add_header Content-Security-Policy \"img-src 'self'\" always;\n"
        "\n"
        '    location /evil { add_header Content-Security-Policy "img-src *" always; }\n'
        "}\n"
    )
    assert locations_with_their_own_add_header(synthetic) != []


def test_add_header_mentioned_only_in_a_comment_is_not_flagged() -> None:
    # The anti-goal this guards against (lines 32-34's reasoning, applied to
    # the new bare-word match): a comment that merely talks about add_header —
    # inside a location block or not — must not itself trip the guard, or a
    # comment could be used to make a real regression look intentional.
    synthetic = (
        "server {\n"
        "    add_header Content-Security-Policy \"img-src 'self'\" always;\n"
        "\n"
        "    location /x {\n"
        "        # add_header here would lose the CSP above, so this location\n"
        "        # deliberately declares none of its own.\n"
        "        try_files $uri $uri/ /index.html;\n"
        "    }\n"
        "}\n"
    )
    assert locations_with_their_own_add_header(synthetic) == []

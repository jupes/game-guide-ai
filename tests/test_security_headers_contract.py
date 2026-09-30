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
from dataclasses import dataclass
from pathlib import Path

import pytest

from service.security_headers import (
    CONTENT_SECURITY_POLICY,
    CROSS_ORIGIN_OPENER_POLICY,
    PERMISSIONS_POLICY,
    REFERRER_POLICY,
    X_CONTENT_TYPE_OPTIONS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NGINX_CONF = REPO_ROOT / "ui" / "nginx.conf"

_DECLARATION = re.compile(r'add_header\s+Content-Security-Policy\s+"([^"]*)"\s+always;')

# y58 — the same drift guard as `_DECLARATION` above, generalised to the four
# headers this bead added. Built from the header name rather than one regex
# per header (`declarations_of` below): a fifth copy-pasted regex is exactly
# the kind of drift this file exists to catch.
_NEW_HEADERS = {
    "X-Content-Type-Options": X_CONTENT_TYPE_OPTIONS,
    "Referrer-Policy": REFERRER_POLICY,
    "Cross-Origin-Opener-Policy": CROSS_ORIGIN_OPENER_POLICY,
    "Permissions-Policy": PERMISSIONS_POLICY,
}

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


@dataclass(frozen=True)
class HeaderDeclaration:
    """One `add_header` directive that sets a given header."""

    offset: int
    # The value nginx sends, read from the quoted OR the bare form; None when
    # the directive names the header but its value is in a shape not read here.
    value: str | None
    always: bool


# The value of an `add_header`, in each form nginx accepts: double-quoted,
# single-quoted, or a bare token (the idiomatic form for a one-word value such
# as `nosniff`), then the optional `always` flag and the closing `;`.
_VALUE_AND_FLAG = r"""\s+(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s;"']+))(?P<always>\s+always)?\s*;"""


def declarations_of(nginx_conf: str, header_name: str) -> list[HeaderDeclaration]:
    """
    Every `add_header` DIRECTIVE in `nginx_conf` that sets `header_name` — with
    the name in any case, and the value quoted or bare (agent-forge-harness-y58,
    review F1).

    The regex this replaced matched only `add_header <Exact-Case-Name> "…"
    always;`, so a second, CONFLICTING declaration escaped the "exactly once"
    count whenever it was written bare (`add_header Referrer-Policy unsafe-url
    always;`) or with the name in another case (`add_header permissions-policy
    "microphone=*" always;`). HTTP header names are case-insensitive, so that
    is the same header — and for these four a second value weakens rather than
    tightens: the last Referrer-Policy wins, a doubled COOP is unparseable and
    ignored, and the last Permissions-Policy key wins.

    So the COUNT matches the name alone, at every `add_header` word
    `_add_header_word_positions` accepts (comments excluded, a one-line
    `location { add_header …; }` included). The value is read separately, so a
    declaration in a shape this cannot read still counts — and then fails the
    value check — instead of silently dropping out of the count.
    """
    escaped = re.escape(header_name)
    names_it = re.compile(rf"add_header\s+{escaped}(?=\s)", re.IGNORECASE)
    shape = re.compile(rf"add_header\s+{escaped}{_VALUE_AND_FLAG}", re.IGNORECASE)

    found = []
    for position in _add_header_word_positions(nginx_conf):
        if names_it.match(nginx_conf, position) is None:
            continue
        parsed = shape.match(nginx_conf, position)
        if parsed is None:
            found.append(HeaderDeclaration(position, None, always=False))
            continue
        value = next(v for v in (parsed["dq"], parsed["sq"], parsed["bare"]) if v is not None)
        found.append(HeaderDeclaration(position, value, always=parsed["always"] is not None))
    return found


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


@pytest.mark.parametrize("header_name", sorted(_NEW_HEADERS))
def test_nginx_declares_each_new_security_header_exactly_once_and_it_matches(header_name: str) -> None:
    """y58's extension of `test_nginx_declares_the_same_policy_exactly_once` to
    the four headers this bead added, one header per parametrized case so a
    failure names which one drifted rather than a generic assertion failure."""
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    declarations = declarations_of(nginx, header_name)

    # Count FIRST, over every declaration of the name in any case and form —
    # not only the quoted exact-case one — so a second, conflicting one cannot
    # hide from it (review F1).
    assert len(declarations) == 1, (
        f"expected exactly one `add_header {header_name} …` in ui/nginx.conf (any case, "
        f"quoted or bare), found {len(declarations)}. Two declarations at the same level "
        "do not merge into a stronger policy — the last Referrer-Policy wins, a doubled "
        "COOP is ignored, and the last Permissions-Policy key wins."
    )
    (declaration,) = declarations
    assert declaration.value is not None, (
        f"ui/nginx.conf declares {header_name} in a shape this guard cannot read; write it "
        f'as `add_header {header_name} "<value>" always;`.'
    )
    assert declaration.always, f"ui/nginx.conf's {header_name} lacks `always`, so 4xx/5xx responses would not carry it."
    assert declaration.value == _NEW_HEADERS[header_name], (
        f"ui/nginx.conf and service/security_headers.py send different {header_name} "
        "values. nginx serves the SPA in Compose and in the E2E; the service serves it "
        "in production. A difference means the browser test and production enforce "
        "different rules (threat model R-6).\n"
        f"  nginx:   {declaration.value!r}\n"
        f"  service: {_NEW_HEADERS[header_name]!r}"
    )


@pytest.mark.parametrize("header_name", sorted(_NEW_HEADERS))
def test_each_new_header_is_declared_above_every_location(header_name: str) -> None:
    """y58's extension of `test_the_policy_is_declared_above_every_location_...`
    above. `locations_with_their_own_add_header` already catches a header
    re-declared INSIDE a location for any header name (it matches the bare
    `add_header` word); this test covers the other half — that each new header
    is declared at all, and every declaration of it (any case, quoted or bare)
    sits above the first `location`, so every location inherits it."""
    nginx = NGINX_CONF.read_text(encoding="utf-8")

    first_location = _LOCATION_DIRECTIVE.search(nginx)
    declarations = declarations_of(nginx, header_name)
    assert first_location is not None, "ui/nginx.conf declares no `location` block at all"
    assert declarations != [], f"ui/nginx.conf declares no `add_header {header_name}` directive"

    assert all(d.offset < first_location.start() for d in declarations), (
        f"{header_name} must be declared in the `server` block, above the first "
        "`location`, so every location inherits it."
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


def test_a_non_csp_add_header_sharing_a_line_with_its_location_is_still_caught() -> None:
    # The realistic way the CSP goes missing is not a second CSP but ANY
    # add_header in a location: nginx then drops every server-level add_header,
    # the CSP included. A CSP-only guard would pass this, so the fixture plants
    # a harmless-looking Cache-Control, on one line (agent-forge-harness-1q7).
    synthetic = (
        "server {\n"
        "    add_header Content-Security-Policy \"img-src 'self'\" always;\n"
        "\n"
        '    location /assets { add_header Cache-Control "public"; }\n'
        "}\n"
    )
    assert locations_with_their_own_add_header(synthetic) == [synthetic.index("add_header Cache-Control")]


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


# ── `declarations_of`: the four y58 headers' count cannot be dodged (review F1) ──
#
# Synthetic strings, not ui/nginx.conf, so each plants exactly the duplicate
# the review found surviving the old quoted-exact-case regex, without touching
# the real file.

_ONE_QUOTED_REFERRER_POLICY = (
    "server {\n"
    '    add_header Referrer-Policy "strict-origin-when-cross-origin" always;\n'
    "    location / { try_files $uri /index.html; }\n"
    "}\n"
)


def test_a_single_quoted_declaration_is_read_with_its_value_and_always() -> None:
    # The positive control for the three cases below: without it, a helper
    # that found nothing would make every "counts two" assertion fail for the
    # wrong reason and every "counts one" assertion meaningless.
    assert declarations_of(_ONE_QUOTED_REFERRER_POLICY, "Referrer-Policy") == [
        HeaderDeclaration(
            _ONE_QUOTED_REFERRER_POLICY.index("add_header"),
            "strict-origin-when-cross-origin",
            always=True,
        )
    ]


def test_an_unquoted_conflicting_duplicate_is_counted() -> None:
    # Review mutant M3: a bare second Referrer-Policy — the idiomatic nginx
    # form for a one-token value — and the one a browser would obey, since the
    # last Referrer-Policy wins.
    synthetic = _ONE_QUOTED_REFERRER_POLICY.replace(
        "    location /", "    add_header Referrer-Policy unsafe-url always;\n    location /"
    )
    declarations = declarations_of(synthetic, "Referrer-Policy")
    assert [d.value for d in declarations] == ["strict-origin-when-cross-origin", "unsafe-url"]


def test_a_case_variant_conflicting_duplicate_is_counted() -> None:
    # Review mutant M4: HTTP header names are case-insensitive, so
    # `permissions-policy` is a second Permissions-Policy, not a new header.
    synthetic = (
        "server {\n"
        '    add_header Permissions-Policy "camera=(), microphone=()" always;\n'
        '    add_header permissions-policy "microphone=*" always;\n'
        "}\n"
    )
    declarations = declarations_of(synthetic, "Permissions-Policy")
    assert [d.value for d in declarations] == ["camera=(), microphone=()", "microphone=*"]


def test_a_bare_declaration_without_always_is_read_as_such() -> None:
    synthetic = "server {\n    add_header X-Content-Type-Options nosniff;\n}\n"
    assert declarations_of(synthetic, "X-Content-Type-Options") == [
        HeaderDeclaration(synthetic.index("add_header"), "nosniff", always=False)
    ]


def test_a_commented_out_or_merely_similar_header_is_not_counted() -> None:
    # The other direction: the count must not be inflated by a `#` comment or
    # by a different header whose name merely starts with this one.
    synthetic = _ONE_QUOTED_REFERRER_POLICY.replace(
        "    location /",
        "    # add_header Referrer-Policy unsafe-url always;\n"
        '    add_header Referrer-Policy-Report-Only "no-referrer" always;\n'
        "    location /",
    )
    assert [d.value for d in declarations_of(synthetic, "Referrer-Policy")] == ["strict-origin-when-cross-origin"]

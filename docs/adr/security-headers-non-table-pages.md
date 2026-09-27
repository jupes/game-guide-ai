# Security headers for non-table pages: Referrer-Policy, COOP, Permissions-Policy

Status: accepted · 2026-09-27

Bead: `agent-forge-harness-y58` · Threat model: [`gm-workbench-threat-model.md`](gm-workbench-threat-model.md) (SEC-17, SEC-12, R-18)

## 1. Scope

`agent-forge-harness-y58` closes the one header SEC-17 named without an owner
(`X-Content-Type-Options: nosniff`) and was also asked to decide, for every page
this app serves **other than** the table and enrolment pages, three headers
SEC-17 only specified for those two: `Referrer-Policy`, `Cross-Origin-Opener-
Policy` and `Permissions-Policy`. The table and enrolment pages are not built
yet (R-18) and are out of scope here; when `1kg.9.5` builds them, they get
SEC-17's own stricter values, not these.

The rationale for the existing `Content-Security-Policy` this note sits next to
lives in [`service/security_headers.py`](../../service/security_headers.py)'s
module docstring, directive by directive. This note is that same kind of record
for the four headers y58 added.

## 2. Decisions

| Header | Value | Why this value, here |
| --- | --- | --- |
| `X-Content-Type-Options` | `nosniff` | The one value this header has. SEC-17 named it explicitly; there is no case for relaxing it anywhere. |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | The standard safe default (also Chromium's and Firefox's own default): a cross-origin request carries only the origin, a same-origin one keeps the full URL, and a downgrade to HTTP carries nothing. **Not** SEC-12/SEC-17's `no-referrer`: that stricter value exists specifically because a table or enrolment address must never leave this origin even as an origin — most pages have no such constraint, and `no-referrer` would cost this app's own analytics-free operation nothing today but is a stricter guarantee than the rest of the app has earned a reason for yet. |
| `Cross-Origin-Opener-Policy` | `same-origin` | Isolates this app's browsing-context group from any window it opens or is opened by. Ships unconditionally — see §3. |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=()` | Denies all four to every frame, including a same-origin one (`self` is not written to any of them) — this app has no feature that uses any of them. `agent-forge-harness-1ir.3.4` will edit `microphone=()` to `microphone=(self)` on GM pages once the live-session assistant ships (SEC-17); `payment` stays denied until Stripe is actually integrated (tracked under `xiu-yje`). |

## 3. Why none of these needed relaxing first

The brief asked this bead to check three flows before shipping the three
non-`nosniff` headers unconditionally, rather than gated behind a flag or
scoped away from some route. As of this bead:

- **Invite flow** (`service/invites.py`, `service/admin_invites.py`,
  `ui/src/shell/inviteToken.ts`, `Signup.tsx`) — the invite token travels in a
  URL fragment, consumed client-side and stripped before the account is
  created; nothing here reads `document.referrer`, and the flow makes no
  cross-origin request that a stricter `Referrer-Policy` could break.
- **OAuth-free sign-in** (`ui/src/shell/Login.tsx`, `Signup.tsx`) — sign-in is
  email + password only. There is no OAuth popup or redirect anywhere in
  `ui/src` or `service`, and neither calls `window.open` or reads
  `window.opener` (checked by grep across both trees) — exactly the pattern
  `Cross-Origin-Opener-Policy: same-origin` would isolate, and there is none of
  it to isolate incorrectly.
- **Stripe** — not integrated (grep across `service/` and `ui/src` finds it
  only in `docs/forge/plans/subscription-billing-coupons-profitability.md` and
  related reports/research, not in application code). Nothing today needs
  `payment` granted to any frame; the day Stripe Elements or Checkout ships,
  this line is what gets revisited.

If any of the three had needed relaxing, the header would have shipped without
that one entry — a `setdefault` per header in `service/app.py`'s
`set_security_headers` middleware makes that a one-line change, not a
re-architecture. None did, so all four ship together, unconditionally, on every
response this app produces (the same `setdefault` semantics as the existing
CSP: a route may still answer with a stricter policy of its own, e.g. SEC-19's
future asset responses).

## 4. Drift guard

Like the CSP, all four are defined once in `service/security_headers.py` and
sent byte-identically by `service/app.py`'s middleware and by `ui/nginx.conf`'s
`server` block (threat model R-6: nginx serves the SPA in Compose and the
browser E2E, `app.py` serves it in production). `tests/
test_security_headers_contract.py` fails if `ui/nginx.conf` ever drifts from
the Python constants; `service/tests/test_security_headers.py` fails if the
middleware ever stops sending one of them.

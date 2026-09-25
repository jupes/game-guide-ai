# Client-side routing, with single-use credentials still in the fragment

Status: accepted · 2026-09-24

## Context

The React UI had no client-side router: `Screen` lived in `useState`
(`ui/src/shell/AppNav.tsx`), reload always returned to Landing, and the
built SPA 404'd on any path but `/` (`docs/adr/invite-auth.md:55-58`). Three
pieces of upcoming work each need a URL the app can land on and interpret:
email-verification links, password-reset links, and Stripe Checkout return
and cancel URLs. None of those beads owned this work, so it was a shared
prerequisite belonging to nobody (`agent-forge-harness-y40`).

The bead's own framing offered an either/or -- add a real router, or
formally adopt the existing `/#token=` fragment convention. The chosen
design takes both halves: a router for the **screen** (the path), with
single-use credentials still riding in the **fragment**. A router alone
cannot satisfy X-7 (`docs/adr/gm-workbench-interactions.md:110` -- a
credential must never enter a path, query string, log or metric label); a
fragment-only convention alone cannot satisfy the bead's acceptance
criterion that a deep path load the correct screen in the built SPA.

## Decisions

**The path names the screen; a single-use credential rides in the
fragment.** `ui/src/shell/routes.ts` is a small, exact-match table --
`/` (landing), `/workspace` (workspace), `/profile` (profile) -- with no
parameters and no wildcards. `service/spa_fallback.py`'s `CLIENT_ROUTES` is
the server half of the same table; `tests/test_spa_routes_parity.py` fails
if the two ever drift. A query string is not an option for a credential: it
reaches Cloud Run's request logs (`ui/src/shell/inviteToken.ts` explains
this in full) the same way a path does.

**The rule, in full, as the dispatch plan states it:** a single-use token --
verify, reset, invite, **table or enrolment** -- never goes in a path or
query string. The honest limit: SEC-18
(`docs/adr/gm-workbench-threat-model.md:205`, section 8.2 `:328`) makes the
table client its own entry point and bundle, so this bead's scrub does not
run for it -- whoever ships `/table/` or `/t/<token>` implements its own
read-and-strip. Do not assume `#token=` is scrubbed everywhere; it is
scrubbed only where `ui/src/shell/UrlNavigation.tsx` runs, which is this
app's bundle.

**Reserved fragment keys: `invite` and `token`.** Declared in
`ui/src/shell/inviteToken.ts`'s `RESERVED_FRAGMENT_KEYS`. On every load,
whatever the path, a reserved key found in the fragment is removed from the
address bar with `replaceState`. `1kg.6.3` owns every OTHER key in the one
fragment grammar (CANVAS-30, `gm-workbench-interactions.md:389`) -- do not
add a workspace key to the reserved list.

**The recipe for adding a route:**

1. one row in `ui/src/shell/routes.ts`, and one entry (same order) in
   `service/spa_fallback.py`'s `CLIENT_ROUTES` -- `tests/test_spa_routes_parity.py`
   forces these two to move together;
2. if the new screen reads a single-use credential, read it with
   `readFragmentToken(location.hash, '<key>')`
   (`ui/src/shell/inviteToken.ts`) and add `'<key>'` to
   `RESERVED_FRAGMENT_KEYS` so the scrub reaches it.

**No client route may begin with a reserved prefix** (`ui/nginx.conf` and
`ui/vite.config.ts` proxy by STRING prefix, not path segment, so
`/chat-history` would be silently proxied to the service the day it existed):
the live API prefixes (`/chat`, `/healthz`, `/models`, `/metrics`,
`/conversations`, `/auth`), the built bundle's own path (`/assets`, MS-7),
the table client's separate entry point (`/table`, `/table-sessions`,
`/t/`, SEC-18), and the "Proposed route families" not yet built
(`/campaigns`, `/documents`, `/tool-invocations`, `/cues` --
`docs/forge/plans/aetheril-gm-workbench-expansion.md:258-271`).
`tests/test_spa_routes_parity.py` checks this against the LIVE route table
plus this named list, not a hard-coded copy, so it keeps working after a
future router extraction changes how routes are registered.

**`/campaigns` is refused as a client path for exactly this reason** -- it
is the planned API family for the Workbench (`GET
/campaigns/{campaign_id}/assets/{asset_id}`). The Tavern/campaign entry
surface this app shell eventually needs (`74j`) must pick a different name;
`/tavern` is free and is NOT blocked by the reserved `/t/` (that reservation
is for the alternative player entry point `/t/<token>`, kept with its
trailing slash so it blocks `/t/anything` and not `/tavern`).

**The URL forms the blocked beads may emit:**

| Bead | Form | Notes |
|---|---|---|
| `yje.2.2` (open signup + email verification) | `/verify#token=<token>` | |
| `yje.2.3` (password reset) | `/reset#token=<token>` | |
| `yje.3.2` (hosted Checkout + Customer Portal) | `/billing/return`, `/billing/cancel` | A Checkout session id is not a single-use credential (retrieving it needs the secret key); whether it may ride in the query string is `yje.3.2`'s own decision with its security review, not this record's. |
| `yje.2.6` (free signup entry surface) | `/signup` | Carries no credential; needs no fragment key. |
| `74j` (shell reaches campaigns) | *(undecided)* | Needs a name that does not collide with `/campaigns` -- see above. |

**Two behaviour changes, recorded so nobody has to rediscover them:**

- **`GET /` moves from the `StaticFiles` mount to an allowlisted API
  route.** `service/app.py` has no `@app.get("/")`; before this bead, `/`
  was answered by the mount. `StaticFiles` and a route-level `FileResponse`
  do not share conditional-request code (`ETag`/`Last-Modified`/`Range`)
  even though both may send similar headers. Nothing in the repository
  asserted on those headers before this bead and no caching rule exists yet
  -- `Cache-Control` on `index.html` belongs to `va8`/`1ir.3.4`. This record
  states the change; it does not compensate for it.
- **`GET /chat` answers 404, not 405, wherever the SPA is mounted.** With no
  mount (a service-only image, or any checkout with no built `ui/dist`), a
  wrong method on a real route answers FastAPI's ordinary 405. With the SPA
  mounted, the allowlist route match for `/chat` never happens (`/chat`
  isn't one of `CLIENT_ROUTES`) and the trailing `StaticFiles` `Mount("/")`
  answers a plain 404 instead, because a static mount does not know about
  methods. This is unchanged by this bead for the Cloud Run image (it
  already mounted `ui/dist`); it is now also true for any other environment
  that builds and mounts the UI.

**The Content-Security-Policy still reaches these three routes.**
`service/app.py`'s `set_security_headers` middleware wraps the whole
router, so `/`, `/workspace` and `/profile` carry the same
`Content-Security-Policy` as every other response
(`service/tests/test_spa_fallback.py::test_the_csp_middleware_still_wraps_an_allowlisted_route`
proves this against the real app and its real middleware stack, not a copy
that would carry none). `Permissions-Policy` and any future `Cache-Control`
on `index.html` remain out of scope here (`1ir.3.4`, `va8`).

**`/workspace`'s cold-load limit.** In-app navigation writes `/workspace` so
Back/Forward work, but a COLD load of `/workspace` renders Landing and
replaces the URL with `/` (`coldLoad: 'home'` in `ui/src/shell/routes.ts`).
Reload has always returned to Landing
(`ui/e2e/session.spec.ts` -- unedited by this bead), and restoring a
workspace from a URL means deciding what it restores (channel, conversation,
campaign, open document), which is `1kg.2.5`'s "deep-link-safe restoration",
not this bead's.

**Unknown paths keep answering a JSON 404 in production.** nginx
(Compose/E2E) and the Vite dev server still serve `index.html` for any
unmatched path via their own SPA fallback, so a mistyped URL meets the
client's "render Landing, replace with `/`" behaviour there; in production,
an unknown path never reaches the SPA at all -- the service answers `404
application/json {"detail":"Not Found"}` before any HTML is served. This
asymmetry is harmless: it is what every path already does today through
nginx/Vite, and production is strictly narrower, not wider.

## Tradeoffs accepted

- **A router does not remove the "mail clients mangle a fragment" risk the
  bead names.** It removes the 404 a deep-linked screen would otherwise hit;
  it does not move a single-use credential out of the fragment, because X-7
  and `docs/adr/invite-auth.md` keep it there (a query string would reach
  Cloud Run's request logs instead). One mitigation is free and worth
  recording: a link-preview scanner that pre-fetches a URL never sends the
  fragment, so it cannot consume a single-use token by prefetching it. A
  paste-a-code fallback for a badly mangled link is `yje.2.2`'s decision.
- **No router library, no route parameters, no lazy route splitting, no
  nested routes.** Nothing in this app needs them yet, and `ui/package.json`
  belongs to a different, concurrent change; the History API is four calls.
- **`GET /` and `GET /chat`'s behaviour changes (above) are recorded, not
  compensated for.** Headers and caching for the served `index.html` are a
  separate bead's decision.

## Superseded

`docs/adr/invite-auth.md:68-71` ("Root-path because there is no client
router — the built SPA 404s on deeper paths.") is superseded by this
record: the SPA now has a client router, and a deep path resolves in
production for the three routes in `CLIENT_ROUTES`. The fragment half of
that decision -- a single-use credential travels in the fragment, never a
path or query string -- is unchanged and generalized by this record to
every reserved key, not only `invite`.

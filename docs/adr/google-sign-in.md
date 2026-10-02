# Sign in with Google

Status: accepted · 2026-10-01 · owner decisions (`agent-forge-harness-yje.1.5`, the
`design:` comment of 2026-10-01) · bead `agent-forge-harness-lvs7`

## Context

The owner asked for a DM account and a player account, with "sign in with Google"
as the way in, and chose two things: **new Google accounts are invite-gated, and
the invite sets the role**, and **email and password stays alongside Google**.
(An admin account that can switch personas is a separate, later idea.)

Today an account is created by redeeming an invite with an email and a password
(`docs/adr/invite-auth.md`). That record stays in force. This one *extends* it:
Google becomes a second way to hold an account, and the invite is still the only
way to get one. Open signup (`yje.2.6`) is a later change and is not this one.

The identity ADR's recommendation (draft PR #84, `identity-provider.md`) is to
extend the in-house auth with an identities table rather than adopt a hosted
provider. This implements the Google half of that and nothing else.

## Decisions

### 1. A server-side code flow, with no Google script on our pages

OpenID Connect authorization-code flow with **PKCE (S256)**, `state` and `nonce`,
run entirely on the server. The browser only *navigates*: a link or form to
`/auth/google/start`, Google's consent page, then Google's redirect to
`/auth/google/callback`. Consequences that were decided on purpose:

- **No Google JavaScript, iframe or popup.** The CSP (`img-src 'self' data: blob:;
  connect-src 'self'; ...`), COOP (`same-origin`, whose rationale says "Sign-in has
  no OAuth popup") and `ui/nginx.conf` do not change by one byte, and nginx already
  proxies the whole `/auth` prefix. The rationale of every one of them stays true.
- The token endpoint and Google's signing certificates are fetched by the service,
  never the browser, so `connect-src` is untouched.
- `response_mode=form_post` is **rejected**: it would need `SameSite=None` on the
  flow cookie.

### 2. The flow cookie

`state`, `nonce`, the PKCE verifier, the intent, the invite (for a sign-up) and, for
a link, the user id and a hash of the session cookie, travel in one **flow cookie**:
`gga_oidc`, `Path=/auth/google`, `HttpOnly`, `Secure`, `Max-Age=600`, signed with
itsdangerous under the session secret and a **different salt**, so a flow cookie can
never decode as a session and a session can never decode as a flow cookie.

- **`SameSite=Lax`, hard-coded**, not taken from `SESSION_COOKIE_SAMESITE`: the
  callback is a cross-site top-level GET from `accounts.google.com`, which Lax sends
  and Strict does not.
- It is signed, not encrypted: everything in it is already the browser's own, and it
  is HttpOnly.
- It is deleted only once `state` has matched, with the same name, path and flags it
  was set with. A callback with a missing or wrong `state` leaves it alone: it
  belongs to another, possibly live, flow, and a cross-site link must not be able to
  cancel it.
- **One-shot on the server too:** a per-instance set of spent states (SHA-256, 600 s,
  bounded at 10 000) refuses a replay with no outbound call.
- The invite token is only in the form body and this cookie. It is never in a URL,
  never sent to Google, and never logged.

### 3. Who gets an account

| Intent | What happens |
|---|---|
| **Sign in** | The Google `sub` is looked up in `auth.identities`. Linked: a session. Not linked: `no_account`. |
| **Sign up** | Only through an invite (`POST /auth/google/start`, a native form). One transaction consumes the invite, creates the account **with the invite's role**, links the identity and writes the event. Anything wrong rolls all of it back, so the invite stays unspent. |
| **Link** | Only **while signed in**, from Profile, and only after the account's **password is checked again** (the SEC-40 re-authentication, called and not changed). The flow is bound to **that session**, not merely that user. |

**No silent link by email.** Signing in with a Google account that is not linked gets
`no_account`, whether or not a password account has that email. A matching address
is not proof of identity: it would turn "whoever controls an address at Google" into
"whoever owns the account registered with it". Nothing aliases Gmail addresses
(no dot or plus folding), for the same reason, and email is ASCII-only (Python's and
PostgreSQL's `lower()` disagree outside ASCII, which would break the case-folded
unique index and admit look-alike addresses).

**Why linking needs the password.** Adding a sign-in method is more sensitive than
the SEC-40 actions (a seat's Remove, a document's delete): there is no unlink, a
link survives a password change and a `SESSION_SECRET` rotation, and the owner cannot
see it. A stolen session cookie, an unattended signed-in device or any future XSS
must not be able to attach someone else's Google account permanently.

### 4. Enumeration safety

> No response reveals a fact about an account or identity that the caller has not
> proved they control.

Everything before the ID token is verified (`expired`, `failed`, `cancelled`,
`throttled`, `unavailable`) says nothing about any account. After verification the
caller controls that Google account and its verified address, so `google_in_use`,
`email_in_use` and `already_linked` describe only their own identity or address.
`no_account` is identical for "no account at all" and "a password account has this
email": same redirect, same cookies, same single identity lookup, and a test that
the sign-in path never reads the email.

The outcome is a closed code in the **query string** of a redirect to `/` or
`/profile` (not the fragment, whose grammar belongs to `1kg.6.3`). It is non-secret,
the SPA maps it through a closed table and never renders the raw value, and every
`Location` the service issues is built from a closed table of constants or is the one
authorization endpoint. No `next`, `return_to` or `redirect_uri` is read, ever.

### 5. Verifying the ID token

`google.oauth2.id_token.verify_oauth2_token` (google-auth, Google's own library)
checks the signature against the key the header names, the algorithm, `iat` and `exp`
(within a skew of `GOOGLE_OAUTH_CLOCK_SKEW_S`, default 60, at most 120), the audience,
and that `iss` is one of Google's two spellings. We additionally require, in order:
`azp` (when present) equals our client, `nonce` equals the flow's, `sub` is a string
of 1 to 255 characters, `email` is an ASCII address of acceptable shape, and
`email_verified` is the boolean `true`.

- The header is parsed first: only `RS256` with a well-formed `kid` is accepted,
  before any fetch.
- Google's keys come from the **X.509 certificates endpoint**
  (`https://www.googleapis.com/oauth2/v1/certs`), which google-auth verifies with
  `cryptography` alone; the JWK-set format would need PyJWT, which is not installed
  and not needed. The body is cached for its `max-age` (clamped to 60 s to 6 h), is
  never served past it, must be a JSON object of one to ten certificates and nothing
  else, and an unknown `kid` forces at most one refresh per minute.
- Every failure, including the `KeyError`, `TypeError` and `ImportError` that
  google-auth 2.59 raises on odd input, is a refusal with a closed reason, logged by
  class name only: google-auth's messages quote claim values.
- The authorization, token and certificate URLs are module constants, never
  configuration.

### 6. The outbound client

One `httpx.Client`: no redirects, **`trust_env=False`** (an `HTTPS_PROXY` in the
container must not route the request body that carries the client secret through a
proxy), TLS verified, `connect=3 s, read=5 s, write=5 s, pool=1 s`, at most four
connections, responses read to at most 64 KiB. A pool timeout is `unavailable`. The
routes are synchronous, so a stalled call holds a threadpool thread and never the
event loop. The token exchange raises its own error **`from None`**: an `httpx`
exception carries the request, and the request body carries the secret.

### 7. Throttling

Start and the pre-verification callback spend a **dedicated** Google source budget
(same numbers as the login budget, no new variable), not the login one: both are
reachable cross-site with no user action, and if they spent the login budget a page
could lock a whole office out of password sign-in. A request carrying fetch metadata
that says it is not a navigation spends nothing and touches nothing. The callback
orders its checks so that a request without a valid cookie and state spends no budget
and makes no outbound call. After verification only the per-account budget
(`google:<sub>`) is spent.

### 8. Data

Migration `0023_google_identities.sql`, pure expansion:

- `auth.users.has_password BOOLEAN NOT NULL DEFAULT true`. A Google-created account
  still needs something in the NOT NULL `password_hash`; it gets a real argon2 hash of
  a random value nobody holds, which keeps a password login for it costing exactly
  one verification, like every other account. `has_password` is what says so, so no
  later feature has to guess.
- `auth.identities (provider, subject, user_id, email_at_link, created_at)`: primary
  key `(provider, subject)`, unique `(user_id, provider)` (one Google per account),
  cascade on account delete. `email_at_link` is what Google asserted when it was
  linked, which may differ from the account's email: an explicit link is not a match
  by email.
- `auth.identity_events`: sign-in, sign-up and link events with ids and closed codes
  only (no email, no `sub`, no address, no invite). It is its own table because
  `audit.events` is campaign-scoped and relaxing it would contract a ledger other
  work owns. Refusals are recorded only **after** the ID token verifies, so an
  anonymous caller cannot grow it. Nothing may update or delete a row (a test scans
  for it).

The store validates in Python everything the CHECKs check **before** any SQL and
classifies unique violations by constraint name, never by message, and the routes log
store errors without a traceback: a driver error's DETAIL quotes the failing row.

### 9. Off unless configured

`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and a valid
`GOOGLE_OAUTH_REDIRECT_URI` (exactly `<origin>/auth/google/callback`; https unless
localhost) must all be set. Otherwise every `/auth/google/*` route is a plain 404,
byte-equal to an unknown path, and the UI draws no button. A partial or invalid
configuration logs one error naming the variable (never the value) and stays off: a
crash would be an outage for a sign-in option. The feature switch is the first
dependency of every route, before the origin check, so an off service answers a
cross-site POST with the same 404.

## Divergences from the identity-provider draft (PR #84)

- The column is `subject`, not `provider_uid`.
- There are no `password` identity rows yet: `yje.2.1` and `yje.2.4` add them, and
  widen the `provider` CHECK when they do.
- `has_password` and `identity_events` are added here (section 8).

## Consequences and limitations

- **A Google-only account cannot pass SEC-40 yet.** Re-authentication checks the
  password, and such an account has none. It cannot remove a seat or delete a
  document once the UI gains its re-authentication dialog (none exists yet, so
  nothing visible breaks today). A follow-up adds Google re-authentication
  (`prompt=login`, `max_age`, an `auth_time` check) or a set-password flow.
  **Until it lands, a DM should keep a password and link Google.**
- **No unlink.** It would need the same re-authentication. An operator removes a link
  with SQL (`docs/deploy-gcp.md` section 14).
- **Google's `email_verified` does not make the account Verified.** A Google-created
  account's email is Google-asserted: a non-Gmail address was verified once, at
  Google sign-up, which is no proof of current control of the mailbox. It must never
  authorize a future reset or verification flow: `verified_address()` stays `None`
  and `yje.2.1` must re-verify.
- **The age gate.** Under open signup, `yje.1.5` section 6 requires the age gate to
  run before any account *or provider identity* is created. The invite era creates
  none without an invite, so it holds today; open signup must put the gate before
  this flow.
- **`code` and `state` appear in Cloud Run's request log** (they are the query
  string). Accepted: the code is single-use, bound by PKCE to a verifier only the
  browser's HttpOnly cookie holds, useless without the client secret, and spent
  within seconds.
- **`SESSION_COOKIE_SAMESITE=strict` breaks linking** (the session cookie is not sent
  on Google's redirect back). Link start refuses with a code, and startup logs one
  error naming the variable.
- **A later CSP `form-action` must admit `https://accounts.google.com`.** The invite
  and link forms POST, get a 303 and are sent to Google, and Chrome applies
  `form-action` to that redirect chain. A test fails if the CSP gains a
  `form-action` that does not.
- **Session revocation is unchanged:** sessions are stateless; rotate
  `SESSION_SECRET` to end all of them.
- **Testing-mode consent screen:** while the Google app's publishing status is
  Testing, only listed test users (at most 100) can sign in; others see "access
  blocked". The scopes (`openid`, `email`) are non-sensitive, so no verification
  review is needed.

## Exit

Unset `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_REDIRECT_URI` and redeploy: every
Google route 404s and the buttons go. Rows in `auth.identities` stay, inert, and
password sign-in is unaffected.

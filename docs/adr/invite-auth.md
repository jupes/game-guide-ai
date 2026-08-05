# Invite-gated authentication

Status: accepted · 2026-07-25

## Context

The pilot serves the full 11-book corpus to a closed group of testers. Access
had been a Cloud Run IAM lock, which is all-or-nothing and cannot distinguish
one tester from another: no per-user history, no roles, and no way to revoke a
single person. Opening ingress to real testers needs an application-level gate
first.

Licensing sets the outer boundary: any exposure beyond the closed group requires
an SRD-only serving mode. That makes *"who is allowed in"* a hard constraint, not
a product preference — self-serve signup is not an option.

## Decisions

**Accounts exist only by redeeming an invite.** There is no open registration
path. An invite is a single-use `secrets.token_urlsafe(32)` carrying the role
it grants, with an expiry and a revocation timestamp.

**Redemption is atomic in the database, not in application code.** A guarded
`UPDATE auth.invites SET used_at = now() WHERE token = ? AND used_at IS NULL
... RETURNING` — the row lock serializes concurrent redeemers, and the second
one matches zero rows. A check-then-update would let two callers both observe an
unused invite and both create an account. This is the single load-bearing
invariant of the whole feature.

**Sessions are stateless signed cookies** (itsdangerous), httpOnly and Secure in
production. Rotating `SESSION_SECRET` is therefore the "log everyone out" lever;
there is no server-side session table to revoke against. The service **fails
closed** — a missing, placeholder or short secret disables auth entirely rather
than signing with a guessable key.

**The account is re-read from the store on every request**, not trusted from the
cookie. The cookie is authentic but stale by nature (it lives for days), so a
deleted account stops working immediately and a demoted DM loses GM access at
once, without waiting for expiry or a secret rotation.

**Authorization fails closed.** A lookup that errors is never followed by a
successful read — an unavailable backend answers 503, never "allowed".

**Roles are server-enforced.** The UI still hides the GM channel from players,
but that is a courtesy; `/chat` rejects the mode. Client-side role state was
removed rather than left as a second source of truth.

**Ownership lives in `chat.conversations`, not a `user_id` column on messages.**
A conversation id is client-generated and the first authenticated user to use it
owns it. A separate table keeps the ownership check to one lookup and left the
existing message/attachment writes untouched. Foreign keys make the database the
backstop: content cannot outlive its ownership row, and deleting an account
takes its conversations, messages and attachments with it.

**Invite links are root-path with the token in the URL fragment**
(`/#invite=<token>`). The fragment is never sent to the server, so a single-use
credential stays out of request logs. Root-path because there is no client
router — the built SPA 404s on deeper paths.

**Auth ships as flat `service/*.py` modules**, not a `service/auth/`
subpackage: `pyproject.toml` uses an explicit package list, so a subpackage would
have been silently dropped by `pip install .` — working in local source-path
tests and crashing the deployed image.

## Tradeoffs accepted

- **No password reset.** It needs outbound email, which the pilot does not have.
  Recovery is an operator minting a new invite.
- **No server-side session revocation.** The cost of stateless cookies; rotating
  the secret is the blunt instrument, and per-request account re-reads cover the
  case that actually matters (a revoked account).
- **Invites are minted by CLI** (`python -m service.admin_invites`), not an admin
  UI. The pilot has one operator.
- **`NOT VALID` foreign keys.** Conversations predating the ownership table have
  messages with no parent row. Validating constraints would refuse to be created
  against real production data; new writes are still fully enforced.

## Operational consequences

- `session-secret` must exist in Secret Manager before deploying, or every auth
  endpoint 503s by design.
- Revoking a compromised account and draining in-flight requests is a runbook
  procedure — see [`deploy-gcp.md` §10](../deploy-gcp.md).

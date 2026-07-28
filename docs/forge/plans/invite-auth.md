# Plan: invite-auth — Invite-gated accounts + server-side auth
Generated: 2026-07-25
Repo: game-guide-ai
Phase: plan (2/4) — from docs/forge/research/invite-auth.md
Beads: task agent-forge-harness-x5bz.2 (tracking) · unblocks x5bz.1.6

## Summary

Add a real auth layer: a new `auth` Postgres schema (users + invites), argon2 password hashing,
an admin CLI to mint/list/revoke one-time invite links (each carrying player|dm), a signup flow
that atomically consumes an invite, email+password login issuing a signed httpOnly session cookie,
a `require_session` dependency guarding `/chat` + `/conversations/*`, server-side GM-channel role
enforcement, and per-user conversation ownership. Frontend gains login/signup screens and drops the
localStorage role/user stubs for a real session. New `session-secret` flows through Secret Manager
+ deploy.sh. This replaces the Cloud Run IAM lock and unblocks x5bz.1.6.

## Existing Code to Reuse

- `vector-db/init/04-chat-schema.sql` + `history.py ensure_schema()` — the exact schema + idempotent
  startup-migration pattern for `05-auth-schema.sql` and the auth store.
- `service/app.py` `Depends()` wiring — the seam to add `require_session` and provider funcs.
- `service/history.py` DSN/connection convention — reused by the auth store.
- `config.py` env pattern (`RAG_*`) — for `SESSION_SECRET`, `SESSION_TTL_DAYS`, cookie flags.
- `scripts/deploy.sh:77` `--set-secrets` — extend with `SESSION_SECRET=session-secret:latest`.
- `ui/src/shell/currentUser.tsx` — replace STUB internals with a real `/me` session (keep the
  `CurrentUser` interface so consumers/tests barely change).
- `ui/src/api.ts` — add `credentials:'include'` + the new auth calls, mirroring `models.py`.
- `tests/` guard pattern, `service/tests/`, `ingestion/tests/` — homes for the new suites.

## TDD Strategy (red-green-refactor)

Following `.claude/skills/tdd`. Behaviors are specs tested through public interfaces, vertically.
Backend suites are pure (fake store, no real DB/LLM) except one gated integration test for the
atomic invite consumption (needs real Postgres semantics — mark it, skip when no DB).

| # | Behavior (as a spec) | Test file | Tracer? |
|---|----------------------|-----------|---------|
| 1 | Password hashes with argon2 and verifies; a wrong password fails; hash is never the plaintext | `service/tests/test_auth_hash.py` | no |
| 2 | Signing a session for (user_id, role) round-trips; a tampered/expired cookie is rejected | `service/tests/test_session.py` | no |
| 3 | `create_invite(role)` yields a single-use token; `redeem` once succeeds, a second redeem fails; expired/revoked tokens fail | `service/tests/test_invites.py` | no |
| 4 | Signup with a valid invite creates the user with the invite's role, consumes the token, sets a session cookie | `service/tests/test_auth_flow.py` | **yes** |
| 5 | `/chat` and `/conversations/*` return 401 without a valid session; 200 with one | `service/tests/test_auth_guard.py` | no |
| 6 | A player-role session posting `mode=gm` gets 403; a dm session succeeds | `service/tests/test_role_enforcement.py` | no |
| 7 | Conversations are owned; user A cannot read user B's messages (404/403), and history recall is per-user | `service/tests/test_conversation_ownership.py` | no |
| 8 | Concurrent double-redemption of one invite: exactly one wins (atomic) | `service/tests/test_invite_atomic.py` (gated: real DB) | no |
| 9 | UI: unauthenticated app renders Login; a 401 from the API routes back to Login; role comes from session (toggle read-only) | `ui/src/shell/auth.test.tsx` | no |

Refactor watch-list: keep the auth store behind a small interface (like `MessageStore`) so tests
fake it; centralize cookie name/flags/TTL in `config.py`; one `require_session` dependency reused
by every guarded route.

## Security Posture (from plan review)

- **Cookie:** `HttpOnly` + `SameSite=Lax` + `Secure`. `Secure` is **forced on in prod via config**,
  not derived from `request.url.scheme` — Cloud Run terminates TLS and forwards HTTP, so scheme
  sniffing would see `http` and wrongly drop `Secure`. Local dev sets it off.
- **CSRF:** same-origin `SameSite=Lax` is the mitigation for the cookie-authed JSON POSTs
  (`/chat`, `/auth/*`, attachments). No separate CSRF token for the pilot; revisit if a cross-site
  surface ever appears. (Signup/login themselves are safe pre-session.)
- **Invite token:** `secrets.token_urlsafe(32)`; single-use, consumed atomically; expiry + revoke.
- **Email:** stored case-folded with a `UNIQUE` constraint; unverified (invite is the trust anchor).
- **`/metrics/ui`:** intentionally stays **unauthenticated** (it's a UI telemetry beacon, needs to
  fire pre-login); it accepts only bounded `ui.*` metric points — documented decision, not an oversight.
- **`/healthz`:** stays open (Cloud Run startup probe must reach it).

## Build Sequence & Checkpoints

### Checkpoint A — Auth schema + store + argon2 (no HTTP yet)
Steps: `05-auth-schema.sql` (users, invites) + `AuthStore.ensure_schema()`; argon2 hash/verify;
invite create/redeem with atomic consumption. Tests #1, #3, #8.
Schema specifics: `auth.users(id, email CITEXT/lower UNIQUE, password_hash, role, created_at)` —
enforce **case-folded unique email**; `auth.invites(token, role, expires_at, used_at, used_by,
revoked_at, created_at)` with `token = secrets.token_urlsafe(32)`. argon2-cffi confirmed to install
from a manylinux abi3 wheel on `python:3.12-slim`/amd64 (no build deps; bcrypt fallback not needed).
Demo: `pytest service/tests/test_auth_hash.py test_invites.py -q` green; `psql` shows `auth.users`/`auth.invites`.

### Checkpoint B — Session signing + admin invite CLI
Steps: signed-cookie encode/decode (`config.py` secret/TTL/flags); `python -m service.admin_invites create --role dm|player` / `list` / `revoke`. Tests #2.
Demo: `python -m service.admin_invites create --role player` prints a signup link; `list` shows it outstanding.

### Checkpoint C — Signup / login / logout + session guard
Steps: `POST /auth/signup` (validate invite → create user → consume → set cookie), `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`; `require_session` dependency added to `/chat` + `/conversations/*`. Tests #4 (tracer), #5.
Demo: curl signup with a CLI-minted token → 200 + Set-Cookie; then `/chat` with the cookie → 200, without → 401.

### Checkpoint D — Role enforcement + conversation ownership
Steps: GM-channel 403 for player sessions from the session role; conversation ownership scoping
message reads/writes **and the attachment GET/POST endpoints** so no authenticated user can reach
another user's conversation by id. Tests #6, #7.

> **As built:** ownership landed as a separate `chat.conversations(conversation_id, user_id)`
> table rather than a `user_id` column on `chat.messages` / `chat.attachments`. It is additive —
> the existing message/attachment writes and their tests were untouched — and one lookup answers
> the authorization question. Ownership is taken via an **atomic** claim (writes always; reads only
> when the conversation already has content, so a GET over random ids can't mint rows).
Demo: player cookie + `mode=gm` → 403; dm cookie → 200; user A can't read user B's conversation or attachments.

### Checkpoint E — Frontend auth
Steps: add `'login'`/`'signup'` to the `AppNav` `Screen` union and an auth gate ahead of the app
(there is **no router** — nav is a `Screen` state machine, and StaticFiles 404s on `/signup`, so the
invite link is **`/#invite=<token>`** read from `window.location.hash` on load (fragment, so the
token never reaches the server or its logs), routing to the
signup screen); Login + Signup screens; replace `currentUser` STUB with a `/auth/me`-backed session;
`credentials:'include'` in `api.ts`; role read-only from session; a 401 from any call → Login screen.
Test #9. UI gates run.
Demo: `bun run dev` → open `/#invite=…` → create account → land in app → chat → sign out → Login.

### Checkpoint F — Ops wiring + invite copy + docs
Steps: `SESSION_SECRET` in `config.py`, `deploy.sh --set-secrets`, `docs/deploy-gcp.md` (create the secret) ; invite-copy template in `docs/` (licensing posture); update `docs/ARCHITECTURE.md`. `(no live demo)` — verified by the deploy-contract guard test asserting the new secret is wired.
Demo: `bash scripts/deploy.sh --dry-run` shows `SESSION_SECRET=session-secret:latest`; guard tests green.

## Files to Create / Modify

| File | Create/Modify | Purpose |
|------|---------------|---------|
| `vector-db/init/05-auth-schema.sql` | Create | users + invites tables |
| `service/auth_store.py`, `hashing.py`, `session.py`, `invites.py` | Create | auth core as **flat `service/*` modules** (NOT a `service/auth/` subpackage — `pyproject.toml:70` is an explicit package list `["service","ingestion"]`, "do NOT auto-discover"; a subpackage would be omitted by `pip install .` and the image would crash while source-path unit tests stay green) |
| `service/admin_invites.py` | Create | admin CLI (create/list/revoke) |
| `service/app.py` | Modify | auth routes + `require_session` on guarded routes + GM 403 |
| `service/history.py` | Modify | conversation ownership — **as built**: a `chat.conversations(conversation_id, user_id)` table plus `owner_of` / `claim_conversation` / `has_content`, not a `user_id` column on `chat.messages`/`chat.attachments` (additive, so existing writes and their tests were untouched); scope message **and attachment** reads/writes to the owner |
| `service/models.py` | Modify | Signup/Login/Me request+response models |
| `config.py` | Modify | `SESSION_SECRET`, `SESSION_TTL_DAYS`, cookie flags (`HttpOnly`, `SameSite=Lax`, `Secure` forced-on in prod via config — NOT derived from request scheme, since Cloud Run terminates TLS and forwards HTTP) |
| `pyproject.toml` | Modify | add `argon2-cffi` + a signing lib (`itsdangerous`) to **core** deps (imported at request time, like `pymupdf`); packages list unchanged (flat modules) |
| `ui/src/App.tsx` + `AppNav` `Screen` union, new `Login.tsx`/`Signup.tsx`, `ui/src/shell/currentUser.tsx`, `api.ts` | Modify/Create | real session UI — add `login`/`signup` to the `Screen` state machine + an auth gate ahead of `AppNavProvider` (there is no router); invite read from `window.location.hash` on load (fragment — never sent to the server, so the token stays out of request logs) |
| `service/tests/*`, `ui/src/shell/auth.test.tsx` | Create | the 9 behavior suites |
| `tests/test_deploy_contract.py` | Modify | assert `session-secret` wired in deploy.sh |
| `scripts/deploy.sh`, `docs/deploy-gcp.md`, `docs/ARCHITECTURE.md`, `docs/invite-copy.md` | Modify/Create | ops + docs + licensing copy |

## Validation Commands

```bash
uv run --with '.[test]' python -m pytest -q
cd ui && bun run typecheck && bun run lint && bun run test
bash scripts/deploy.sh --dry-run
```

## Beads Issue Map

Children under tracking task **agent-forge-harness-x5bz.2** (one per checkpoint).

| Beads ID | Type | Title | Depends on | Priority |
|----------|------|-------|-----------|----------|
| (create) | task | A — auth schema + store + argon2 + invite atomicity | — | P1 |
| (create) | task | B — session signing + admin invite CLI | A | P1 |
| (create) | task | C — signup/login/logout + require_session guard | B | P1 |
| (create) | task | D — GM role enforcement + conversation ownership | C | P1 |
| (create) | task | E — frontend login/signup + session-backed user | C | P1 |
| (create) | task | F — ops wiring (session-secret) + invite copy + docs | C | P2 |

## Estimated Scope

- Files: ~12 new / ~8 modified; Complexity: High (security-sensitive, cross-cutting); Checkpoints: 6
- After this lands + verifies 401/403, x5bz.1.6 flips Cloud Run ingress open for testers.

# Research: invite-auth — Invite-gated accounts + server-side auth
Generated: 2026-07-25
Repo: game-guide-ai
Phase: research (1/4)
Beads: task agent-forge-harness-x5bz.2 · unblocks x5bz.1.6 (open Cloud Run ingress)

## Goal

Replace the client-side auth stubs with real per-user accounts created only through one-time
invite links, server-enforced sessions on every data endpoint, and server-side role enforcement
for the GM channel. This is the app-level gate that **replaces the Cloud Run IAM lock** — once it
enforces 401/403, `x5bz.1.6` can flip ingress open for real testers.

## What the Code Says (answered by exploration)

- **Auth is 100% client-side stub today.** `ui/src/shell/currentUser.tsx:42` — a hardcoded `STUB`
  guest ("Adventurer"); role is a `localStorage` value `game-guide-ai:role`
  (`currentUser.tsx:54`). The file comments itself: *"the server does not enforce roles until real
  auth exists… replace STUB with a real auth integration"* and names x5bz.2.
- **No endpoint requires auth.** `service/app.py:243-377` — `/healthz`, `/chat`,
  `/conversations/{id}/messages`, `/conversations/{id}/attachments`, `/metrics/ui` all use
  `Depends()` for services but none for a session. `conversation_id` is client-generated and
  unauthenticated.
- **No user ownership in data.** `vector-db/init/04-chat-schema.sql:9-34` — `chat.messages` /
  `chat.attachments` keyed by `conversation_id TEXT` only; no `user_id`. History is retrieved by
  conversation id alone (`service/history.py`).
- **Role toggle is client-only.** `ui/src/shell/useRoleToggle.ts` + `UserMenu.tsx` flip
  `currentUser` role in React state + localStorage; the GM channel is gated in the UI
  (`modes.ts`), never at the API.
- **Schema-migration pattern is established.** `04-chat-schema.sql` is applied fresh via
  `vector-db/init/`, and re-applied idempotently at service startup by
  `history.py PostgresMessageStore.ensure_schema()`. New auth tables follow this exact pattern
  (a `05-auth-schema.sql` + an `ensure_schema()`), so they exist on both fresh and existing DBs.
- **DB access is a shared DSN.** `DATABASE_URL` (psycopg3) used everywhere; the new auth store
  reuses the same connection convention (`history.py:153`).
- **Deploy injects secrets by reference.** `scripts/deploy.sh:77` sets
  `--set-secrets OPENAI_API_KEY=…,DATABASE_URL=…`. A session-signing secret is a **new** secret
  that must be added here + to the runbook + Secret Manager.
- **Frontend is a single SPA** (React 19 + Vite) served at `/` by the same origin as the API — so
  a session cookie is first-party (no cross-site cookie complications). `api.ts` mirrors
  `service/models.py`; requests will need `credentials: 'include'` to send the cookie.
- **Contract seam for refusals** already returns 200-with-flag; auth failures are new status codes
  (401/403) the UI must distinguish from a refusal.

## Decisions Resolved with the User

| Question | Decision | Rationale |
|----------|----------|-----------|
| Invite generation surface | **CLI script** (`create-invite` / `list-invites` / `revoke-invite`) run via the Cloud SQL proxy | Operator == only admin for the pilot; no admin-auth surface to build/secure. |
| Role assignment | **Invite carries the role** (admin picks player/dm at generation; user inherits at signup) | Deterministic; no self-promotion to GM. UserMenu toggle becomes read-only display. |
| Session mechanism | **Signed stateless httpOnly cookie** (server secret, carries user id + role, ~14-day expiry) | Simplest; no session table. Short expiry limits the no-server-revocation tradeoff. |
| Account identity | **Email + password** | User choice. Email is only the identifier — the invite link is the trust anchor, so **no verification email / SMTP needed**. |
| Password hashing | **argon2** (`argon2-cffi`) | Modern default, single clean dep; AC lists argon2 first. |

## Constraints & Non-Goals

- Constraint: **every** `/chat` and `/conversations/*` request requires a valid session → 401 if
  missing/invalid. A player-role session hitting the GM channel (`mode=gm`) → 403 (server-enforced,
  replacing the UI-only gate).
- Constraint: passwords argon2-hashed; **never logged, never in git**. Invite tokens single-use,
  consumed **atomically** (a concurrent second redemption must fail — tested).
- Constraint: conversations become **owned by a user**; history recall is per-user; a user cannot
  read another user's conversation.
- Constraint: the session-signing secret lives in Secret Manager (`session-secret`) and is injected
  by `deploy.sh` — a new `--set-secrets` entry + runbook + CI note.
- Constraint (licensing, x5bz.5): invite copy states it's a private test for book owners, links must
  not be shared, no D&D branding — a checked-in `docs/` invite-copy template.
- Non-goal: password reset / email sending (deferred — admin CLI reset if needed).
- Non-goal: in-app admin UI for invites (CLI only for the pilot).
- Non-goal: OAuth / social login / MFA.
- Non-goal: actually flipping Cloud Run ingress open — that's x5bz.1.6, gated on this landing +
  verification.

## Open Risks / Assumptions Carried Forward

- Signed cookie can't be force-revoked server-side; mitigated by short expiry. If a session must be
  killed immediately, rotate `session-secret` (invalidates all sessions) — acceptable for a pilot.
- Existing stub conversations (localStorage ids) have no owner; on first real login the client
  starts fresh per-user. No migration of anonymous history (none exists server-side worth keeping).
- Email is stored but unverified; a typo'd email just means that account's login id is the typo.
  Acceptable — the invite link, not the email, grants access.
- Argon2 adds a native dep to the image; confirm it installs cleanly in the slim python image
  (may need build deps or the `argon2-cffi-bindings` wheel — verify at plan time).

## Recommended Scope for Planning

Plan as vertical slices behind a small auth module. **Backend:** `05-auth-schema.sql` + auth store
(`users`: id, email, password_hash, role, created_at; `invites`: token, role, expires_at,
used_at, used_by, revoked_at, created_at), argon2 hashing, a `create/list/revoke invite` CLI,
signup endpoint (validates unexpired unused invite → creates user, consumes token atomically →
sets cookie), login/logout, a `require_session` FastAPI dependency added to `/chat` +
`/conversations/*`, GM-channel role check (403), and conversation ownership (add `user_id`, scope
reads/writes). **Frontend:** login + signup screens, replace `currentUser` STUB/localStorage with
a real `/me`-backed session, `credentials:'include'` in `api.ts`, role becomes read-only from the
session, 401 → redirect to login. **Ops:** `session-secret` in Secret Manager + `deploy.sh` +
runbook; invite-copy template in `docs/`. Tracer bullet: signup-then-authenticated-chat end to end.

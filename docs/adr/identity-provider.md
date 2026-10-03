# Identity implementation: extend the in-house stack, or adopt a managed provider

Status: **Proposed** · drafted 2026-09-21 · bead `agent-forge-harness-yje.1.5`

> **Not yet decided.** The owner signs this record after the design agent finishes,
> so that [the sensitivity to account volume](#8-sensitivity-to-the-open-design-questions)
> is read against the design's answer on who pays and whether players hold accounts.
> Until then nothing here is binding and no bead's acceptance criteria change.

---

## 1. Context

Public launch turns the invite pilot's authentication into an internet-facing
surface. What ships today, from the code rather than from the plan:

| Today | Where |
|---|---|
| Accounts exist **only** by atomically redeeming an invite (guarded `UPDATE ... WHERE used_at IS NULL ... RETURNING`) | `service/auth_store.py:271-293` |
| Argon2id hashing with explicit parameters (64 MiB / t=3 / p=4) and a process-wide semaphore bounding peak hashing memory | `service/hashing.py:43-101` |
| Sessions are **stateless signed cookies** (itsdangerous, 14-day life). No session table. "Log everyone out" means rotating `SESSION_SECRET` | `service/session.py:1-51`, `docs/adr/invite-auth.md:30-34` |
| The account is **re-read from the store on every request**, so a deleted account or a demoted DM loses access at once | `docs/adr/invite-auth.md:36-39` |
| A production-grade sliding-window rate limiter on the auth endpoints, keyed on account **and** source, parsing `X-Forwarded-For` from the right using a configured trusted-hop count | `service/ratelimit.py:47-226` |
| Ownership is `chat.conversations`, with foreign keys so deleting an account takes its conversations, messages and attachments with it | `docs/adr/invite-auth.md:48-53` |
| Authorization fails closed; roles are server-enforced, never trusted from the client | `docs/adr/invite-auth.md:41-46` |

And what does not exist:

- No email verification and **no outbound email at all**.
- No password reset. The documented recovery is to **delete the account**, which
  cascades away that user's conversations
  (`docs/adr/invite-auth.md:67-73`; `docs/deploy-gcp.md:675`, `:711`).
- No individual session revocation.
- Signup **enumerates accounts**: a taken email returns 409
  (`service/app.py:1019-1020`, raising on `EmailTaken` from
  `service/auth_store.py:152-153`), and used, expired and revoked invites return
  distinct messages.
- The rate limiter is **per-instance and in memory**, so the effective ceiling is
  the limit multiplied by `--max-instances` (`service/ratelimit.py:19-25`).
- No minimum age and no age gate anywhere in the account path (bead `cug`, P0).

The question this record answers is whether the missing pieces are built on the
existing stack or bought from an identity provider.

## 2. The decision

**Extend the in-house stack (Option A), buy transactional email as a service, and
make the schema pre-enable a later move to a provider.**

Concretely:

1. Keep `auth.users` as the identity of record, keep the integer `id` as the
   ownership key, keep Argon2id.
2. Add a server-side `auth.sessions` table. The cookie carries an opaque session
   id, still signed. Because the service **already** reads the account from the
   store on every request, revocation costs no extra round trip.
3. Add hashed, single-use, expiring `auth.email_verifications` and
   `auth.password_resets`, consumed with the same guarded-`UPDATE` pattern that
   already makes invite redemption atomic.
4. Buy transactional email (see §5). Aetheril owns the templates, the sending
   domain and the SPF/DKIM/DMARC records.
5. Add `auth.identities (provider, provider_uid, user_id)` **now**, with a
   `password` row for every account, even though `password` is the only provider.
   This is the cheap-reversal mechanism of §9.
6. Move the auth and chat rate limits to a shared store (`yje.5.2`), and make the
   signup and reset responses non-enumerating.

Social sign-in is **not** adopted now, but §6 keeps it one insert away.

### Why, in one paragraph

Cost is not the deciding factor, and the naive expectation is backwards: **at this
product's scale every managed identity provider is free** (§4). The deciding
factor is that Aetheril must keep its own users table under *every* option — the
billing plan assigns "user identity and user-to-customer mapping" to Aetheril
(`docs/forge/plans/subscription-billing-coupons-profitability.md:163`), and
`chat.conversations` ownership is a foreign key to `auth.users.id`. A managed
provider is therefore **additive, not substitutive**: it does not delete the users
table, the role model, the enumeration work, the age gate or the abuse controls;
it adds an external dependency and a second identifier to keep in sync. The one
thing it genuinely hands over is sending the verification and reset emails — and
that can be bought directly for $0–$20 a month without taking an identity
dependency at all.

## 3. Options

### Option A — Extend the in-house stack (recommended)

Add session table, verification, reset, non-enumerating responses, shared-store
rate limits, bot defenses. Buy transactional email.

**For.** Nothing in the current stack is thrown away, and the parts that are hard
to get right are the parts that already exist and are already tested: atomic
single-use token consumption, Argon2 sizing against Cloud Run memory,
trusted-hop `X-Forwarded-For` parsing, fail-closed authorization. Revocation
becomes *free* rather than expensive, because the per-request account re-read
already pays for the lookup. One service, one datastore, one failure mode. The
age gate (`cug`) and the enumeration-safe copy are built in the product's own
signup screen, where they have to be built anyway.

**Against.** Aetheril owns email deliverability — SPF, DKIM, DMARC, a warmed
sending domain, and bounce handling — which is unglamorous operational work a
provider would absorb. Aetheril owns the security of the reset and verification
token flows, including timing, resend throttling and non-enumeration. If password
volume grows, password-reset support lands on one operator.

### Option B — Adopt a managed identity provider

Google Identity Platform / Firebase Authentication is the natural candidate given
GCP hosting; Auth0, Clerk, WorkOS AuthKit, Supabase Auth and Stytch are the
alternatives.

**For.** Verification and reset emails are sent by the provider with no sending
domain to warm (Firebase "results in emails being sent to the user" and still
sends them even when you host a custom action handler —
[Firebase custom email handler](https://firebase.google.com/docs/auth/custom-email-handler),
read 2026-09-21). Social sign-in, MFA and bot defenses arrive as configuration.
Free at this scale.

**Against, and this is the substance:**

- **It does not replace the users table.** Firebase issues a string UID;
  `auth.users.id` is an integer referenced by the ownership foreign keys. You keep
  both and map between them. That is one more identifier, one more sync failure
  mode, and one more place for a partial account to exist.
- **Session revocation gets worse, not better.** Firebase ID tokens are stateless
  JWTs that "are short lived and last for an hour"; an already-issued token stays
  valid until it expires. To revoke sooner you call `verifyIdToken(checkRevoked=true)`,
  and the documentation is explicit: "performing this check on your server is an
  expensive operation, requiring an extra network round trip"
  ([Manage sessions](https://firebase.google.com/docs/auth/admin/manage-sessions),
  read 2026-09-21). Aetheril's requirement (`yje.2.3`) is that individual and all
  sessions can be revoked. A local session table gives that on a lookup the
  service already performs; Firebase gives it on an external network hop per
  request, or gives up to an hour of residual validity.
- **A new availability dependency in a service designed to fail closed.** Today an
  unavailable backend answers 503 rather than "allowed"
  (`docs/adr/invite-auth.md:41`). Adding an identity provider adds a second thing
  that can be down and turn every request into a 503.
- **The work it removes is smaller than it looks.** Non-enumerating responses, the
  `cug` age gate, role assignment, the entitlement decision, the abuse ceilings and
  the migration of existing accounts all remain Aetheril's, because they are
  product decisions and product data.
- **Sender domain is shared by default.** The provider's default sending domain is
  not yours; the custom-domain path has caveats (for multi-tenant projects "users
  still receive emails from the default domain even if the custom domain is
  successfully verified and applied" —
  [Custom email domain](https://firebase.google.com/docs/auth/email-custom-domain),
  read 2026-09-21).

### Option C — Hybrid: keep in-house passwords, add social sign-in

**For.** Every user who signs in with Google never needs a password reset, which
is the single largest support burden in Option A. Costs nothing.

**Against.** It removes none of Option A's work — verification, sessions,
revocation, enumeration, rate limits all still have to be built, because password
accounts still exist. It *adds* account-linking questions: what happens when a
Google sign-in arrives with an email that already has a password account, and
what the answer does to account takeover. It is a real improvement, but it is an
improvement **on top of** Option A rather than an alternative to it — which is why
§2 builds the schema for it and defers the feature.

## 4. Cost at this product's scale

The product runs a single small Cloud Run service and a small Postgres; the
billing plan models break-even at **10 / 23 / 46 paid users**
(`subscription-billing-coupons-profitability.md`). Even if every player at every
table held an account — roughly six accounts per paid GM — 46 paid GMs is on the
order of **300 accounts**.

All figures read from the vendor's own pricing page on **2026-09-21**.

| Provider | Free allowance | Price above it | Cost at ~300 accounts |
|---|---|---|---|
| [Firebase Auth / Identity Platform](https://firebase.google.com/pricing) | 50,000 MAU (50 MAU for SAML/OIDC) | "Then Google Cloud pricing" — **tier prices not verifiable**, see §7 | **$0** |
| [Clerk](https://clerk.com/pricing) | 50,000 MRU (Hobby) | Pro $25/mo incl. 50,000 MRU, then $0.02/MAU | **$0** |
| [Supabase](https://supabase.com/pricing) | 50,000 MAU (Free) | Pro $25/mo incl. 100,000 MAU, then $0.00325/MAU | **$0** |
| [Auth0](https://auth0.com/pricing) | 25,000 MAU (Free) | B2C Essentials "from $35/month" at 500 MAU | **$0** |
| [WorkOS AuthKit](https://workos.com/pricing) | "First 1M MAUs" free | "$2,500/mo" per additional 1M; SSO/Directory Sync "$125/ea" connection | **$0** |
| In-house (Option A) | n/a | Postgres rows in the database already paid for | **$0** |

**Conclusion: identity cost is $0 under every option and must not decide this.**
The only real money is transactional email, and it is small:

| Email provider | Free | Paid entry | Overage |
|---|---|---|---|
| [Resend](https://resend.com/pricing) | 3,000/mo, 100/day, 3 domains, 30-day retention | $20/mo for 50,000 | $0.90 / 1,000 |
| [Postmark](https://postmarkapp.com/pricing) | 100/mo | $15/mo for 10,000 | $1.80 / 1,000 |
| [Amazon SES](https://aws.amazon.com/ses/pricing/) | $200 Free Tier credits, new accounts, 6 months | $0.10 / 1,000 à la carte | $0.12/GB attachments |

At a few hundred accounts, verification plus reset plus billing notices is on the
order of hundreds of emails a month — inside Resend's free tier, or about **$0.10
a month** on SES. Against the $3.00 per-account cost cap and the $10 GCP kill
switch (`subscription-billing-coupons-profitability.md:110-113`), email is noise.

**Recommendation: Amazon SES**, because the service already runs one cloud bill's
worth of operational surface and SES is the cheapest per unit; **Resend** if the
owner prefers a shorter integration and readable logs, at a still-trivial $20/mo
ceiling. Either way the sending domain, DKIM keys and DMARC policy are Aetheril's
and are a launch gate, not a follow-up.

## 5. What each option does to the things the bead names

| Concern | Option A (recommended) | Option B (managed) |
|---|---|---|
| **User ids and existing accounts** | Unchanged. `auth.users.id` stays the ownership key; no conversation, message or attachment row moves. | A second identifier appears. The local id must be retained anyway for the ownership FKs, so you map provider UID → local id forever. |
| **Session model** | The cookie carries an opaque session id, still signed with `SESSION_SECRET`. `auth.sessions` rows give per-session revocation, logout-all by `user_id`, and credential-change invalidation, at the cost of the lookup the service already does. `SESSION_SECRET` rotation survives as the break-glass lever rather than the only lever. This **supersedes** the "No server-side session revocation" tradeoff in `docs/adr/invite-auth.md:75-77`. | 1-hour stateless ID tokens; revocation is either eventual (up to an hour) or costs "an extra network round trip" per request. |
| **Email verification** | Aetheril sends it. Token is hashed at rest, single-use, expiring, consumed atomically. Resend is throttled by the existing limiter. | Provider sends it from its own domain by default; custom domain needs DNS and has caveats. |
| **Password reset** | Aetheril sends it. Same token discipline. Reset **preserves the account and its conversations**, which retires the delete-and-re-invite runbook at `docs/deploy-gcp.md:675`/`:711`. | Provider owns the flow; Aetheril still has to decide non-enumerating copy and what a reset does to sessions. |
| **Account deletion** | Already works: foreign keys cascade conversations, messages and attachments. Add: revoke all sessions, invalidate outstanding tokens, and record a tombstone so a deleted email is not silently re-registrable during a retention window. | The provider holds a second copy of the account that must be deleted in the same transaction as the local one — a distributed delete, with a partial-failure state where the identity exists and the data does not, or the reverse. |
| **Enumeration-safe responses** | Signup returns the same 202 and the same copy whether or not the email exists, and the "already registered" case is answered **by email**, not by status code. The current 409 (`service/auth_store.py:152`) is removed. Reset and resend behave identically. | Identical work. The provider's hosted UI does not make this decision for you, and if you use it you inherit **its** enumeration behaviour, which you do not control. |
| **Abuse controls** | The existing limiter is already the right shape; it moves to a shared store so the ceiling stops being multiplied by instance count (`service/ratelimit.py:19-25`), and gains keys for signup, verify, resend and reset. Bot defense (a challenge on signup) is new work either way. | Provider bot defenses are good, but the age gate and the per-account cost ceilings stay Aetheril's. |
| **Cost** | $0 identity + ~$0–$20/mo email | $0 identity + provider-sent auth email |
| **Exit** | See §9 | See §9 |

## 6. What this changes about the age gate and the signup screen

**The `cug` age gate works with any provider, and no provider supplies it.** The
minimum age, how it is established, what a refused visitor sees, whether the
refusal is remembered, and whether a guardian path exists are product and legal
decisions taken on Aetheril's own signup screen. None of the six providers priced
in §4 sells COPPA verifiable parental consent; that is a different class of vendor
entirely. **The provider choice does not advance, block or constrain `cug`, and
`cug` does not constrain the provider choice.**

Two consequences that do bind:

- An age gate must run **before** an account or a provider-side identity is
  created, or the product acquires exactly the actual knowledge COPPA attaches to.
  Under Option A that ordering is ours to enforce. Under Option B, a hosted signup
  UI would create the provider identity first, so the gate would have to sit in
  front of it, which is one more reason the signup screen stays Aetheril's.
- If the owner's answer needs verifiable parental consent, that is a specialist
  vendor and a new bead, and it flips nothing in §2.

## 7. What I could not verify

Stated plainly rather than guessed:

- **Identity Platform's per-MAU price above 50,000 MAU.**
  [cloud.google.com/identity-platform/pricing](https://cloud.google.com/identity-platform/pricing)
  did not yield its tier table to a text fetch on 2026-09-21. The **50,000 MAU
  free allowance is verified** from [firebase.google.com/pricing](https://firebase.google.com/pricing)
  ("No-cost up to 50K MAUs Then Google Cloud pricing"), and that allowance is two
  orders of magnitude above this product's plausible volume, so the unknown tier
  price cannot change the conclusion in §4. If the owner ever plans for >50,000
  accounts, re-read that page before relying on this record.
- **SMS/phone sign-in and MFA pricing** for Firebase — billed per SMS, "See current
  rates", rate card not read. Not needed for any option in §2.
- **Whether Auth0's free 25,000 MAU allowance carries production support terms**
  that matter here. Not read; Auth0 is not recommended, so it did not gate the
  decision.
- **Deliverability in practice.** No figure from any vendor predicts whether
  Aetheril's verification mail reaches an inbox. That is measured after launch, not
  decided here.

## 8. Sensitivity to the open design questions

The design agent is deciding who pays, how an account becomes a GM, and the
minimum age. **The recommendation in §2 holds under every plausible answer.** What
changes is only the following.

### If player accounts become required (E-6 rejected)

E-6 — "One GM per campaign; players have no accounts" — is unconfirmed
(`docs/adr/gm-workbench-interactions.md:1032`, with the argument at AUD-1, `:557`).
If it is rejected:

- **Account volume multiplies by roughly the table size**, perhaps six-fold. In
  dollars this changes nothing: 300 accounts and 2,000 accounts are both free at
  every provider in §4, and both are inside Resend's free email tier.
- **What it changes is operator labour and the shape of the sign-in.** Six times
  the accounts is six times the forgotten passwords, landing on one operator, for
  users whose entire relationship with the product is glancing at a phone at a
  friend's table. At that volume **social sign-in stops being optional and becomes
  the better fit**, because it removes the password from the majority of accounts
  — and with it the reset flow, the reset email and the reset support.
- This is precisely the case the `auth.identities` table in §2 exists for: adding
  Google sign-in becomes a provider row and a button, not a migration of ownership.
- It also collides with an existing security decision: table routes deliberately
  ignore account cookies (`docs/adr/gm-workbench-threat-model.md`, SEC-1), so a
  player account currently "counts for nothing" at a table. If players get
  accounts, that ADR needs a superseding note; that is a separate bead, not this
  one.

### If a free tier exists

A free tier multiplies accounts without multiplying revenue, so the per-account
identity cost matters more in principle. In practice §4 still applies: free at
every provider until 25,000–50,000 accounts. What a free tier does bind is
**email volume** — verification mail scales with signups, not with paid accounts —
and the abuse budget, since a free signup is the cheapest thing for a bot to
create. Both are Option A concerns already listed in §5.

### If the `dm` role is self-selected, purchased, or operator-granted

No effect on the provider choice. Role assignment is Aetheril authorization
(`subscription-billing-coupons-profitability.md:124`) under every option; the
invite currently carries it (`docs/adr/invite-auth.md:19-21`) and open signup must
replace that carrier. Named here only because `yje.2.2` does not mention roles at
all and something must.

### If the minimum age needs verifiable parental consent

See §6. It adds a vendor and a bead; it does not flip §2.

## 9. Reversal cost

**Reversing into a provider later (the likely direction): low, and deliberately
made low.**

Because §2 adds `auth.identities` on day one and never moves `auth.users.id`,
adopting Google Identity Platform or Google sign-in later is:

1. insert a row per migrated user with the new provider and its UID;
2. accept the provider's token at the session-creation boundary only, and keep
   issuing Aetheril's own session cookie from that point;
3. leave every ownership foreign key, conversation, message and attachment
   untouched.

Argon2 hashes are portable — Firebase supports importing them — so password
accounts can move without forcing a reset. Estimated cost: a migration and one
endpoint, not a rewrite.

**Reversing out of a provider: materially higher**, which is the asymmetry that
decides the order of operations. Exporting users from a managed provider returns
identifiers and metadata but not always usable password material; in practice an
exit means forcing every user through a password reset, using the email
infrastructure you deferred building. Adopting a provider before the design is
settled buys an option that costs more to unwind than to acquire.

## 10. Proposed acceptance-criteria changes — **held until the owner signs**

The bead's criterion "the acceptance criteria of `yje.2.1` to `yje.2.4` are updated
to match" is **not applied by this pull request**. The changes below are the
proposal; the lead applies them after signature. No bead is edited by this work.

### `yje.2.1` — Add ordered identity and session schema migrations

Add:

- Migrations create `auth.sessions` (session id hashed at rest, `user_id`,
  `created_at`, `last_seen_at`, `revoked_at`, `revoked_reason`),
  `auth.email_verifications` and `auth.password_resets` (token hashed at rest,
  `expires_at`, `consumed_at`), and `auth.identities`
  (`provider`, `provider_uid`, `user_id`, unique on `(provider, provider_uid)`).
- The migration backfills one `auth.identities` row per existing account with
  provider `password`, **so that adopting an external identity provider later is an
  insert rather than a rewrite of ownership foreign keys.**
- `auth.users.id` remains the ownership key referenced by `chat.conversations`.
  **No migration changes it**, under any identity option.
- `auth.users` gains `email_verified_at` and an explicit migration state column.
- Follows the ordered-migration conventions introduced by `1kg.1.5` (PR #65,
  `docs/migrations.md` — **not yet on `master`**, so the runner must land first):
  numbered file, manifest regenerated, no `BEGIN`/`COMMIT` inside, roll-forward
  only, expand-then-contract across releases, applied in
  `tests/test_migrations_db.py` to both a fresh and a pre-expansion database.

### `yje.2.2` — Implement open signup with email verification and abuse controls

Add:

- The signup response is **byte-identical** whether or not the email already has an
  account: the same status, body and latency class. The `EmailTaken` 409
  (`service/app.py:1019-1020`) is removed from the public path, and the
  already-registered case is answered by an email to the address, not by the
  response.
- Verification tokens are consumed with a guarded `UPDATE ... WHERE consumed_at IS
  NULL ... RETURNING`, matching the invite invariant at `service/auth_store.py:271`.
- Auth, signup, verify, resend and reset limits move to a **shared** store, so the
  ceiling stops being multiplied by `--max-instances` (`service/ratelimit.py:19-25`).
- The transactional-email provider sits behind an interface with a tested outage
  path; a send failure never leaves an account in a state the user cannot escape.
- **Blocked by `cug`**: no open signup form ships before the age decision, and the
  gate runs before any account row is created.
- Role assignment on open signup is explicit, because the invite that used to carry
  the role is gone.

### `yje.2.3` — Add password recovery and revocable account sessions

Add:

- The session cookie carries an **opaque session id**, still signed; the
  per-request account re-read becomes a session-and-account read, so revocation
  adds no extra round trip.
- Individual revocation, logout-all by `user_id`, and automatic revocation of every
  other session on password change or email change.
- `SESSION_SECRET` rotation remains available as break-glass but is **no longer the
  only** revocation lever. This supersedes `docs/adr/invite-auth.md:75-77`, which
  needs a superseding note.
- Password reset **preserves the account, its role and its conversations**, retiring
  the delete-and-re-invite procedure at `docs/deploy-gcp.md:675` and `:711`.
- Security tests cover fixation, replay, and the cross-session invalidation path.

### `yje.2.4` — Migrate existing invite users without changing ownership

Add:

- Every existing account receives an `auth.identities` row with provider `password`
  and an explicit verification state; **no user id, role or ownership row changes**.
- Existing accounts were never email-verified (`subscription-billing-coupons-profitability.md:86`).
  The migration defines what an unverified legacy account may do and for how long,
  and does not lock a pilot tester out mid-session.
- Existing sessions receive a defined transition when the cookie format changes:
  either honoured until expiry or invalidated with a re-login prompt, chosen
  explicitly and communicated.
- The runbook and ADR retirements above are part of this bead's definition of done.

## 11. What only the owner can decide

1. **Whether to take an external identity dependency at all.** This record
   recommends no, on architecture rather than cost; that is a judgement the owner
   may weigh differently against operator time.
2. **Whether players hold accounts** (confirm or reject E-6). It does not change the
   provider, but it decides whether social sign-in is optional or required (§8).
3. **How an open-signup account gets the `dm` role** — chosen, purchased, or granted.
4. **The minimum age and the guardian path** (`cug`), and whether that question goes
   to counsel rather than to a designer.
5. **Which email vendor**, and whether mail sends from the apex domain or a
   subdomain — the latter decides the DMARC policy and is hard to change later.
6. **Whether password sign-in is permanent** or a stepping stone to passwordless.
7. **The retention window for a deleted account's email address** before it may be
   registered again.

## 12. Open questions

- What does a **legacy pilot account** see the first time it signs in after
  verification becomes mandatory? The migration state exists (§10) but the copy and
  the deadline are a design answer, not an engineering one.
- Does an account that is deleted while holding an active or sponsored subscription
  delete the billing customer too? Under Stripe Managed Payments this is not
  symmetrical — see `docs/adr/billing-provider.md` §6. Belongs to `yje.1.3`.
- Is a **bot challenge** on signup acceptable to the owner on a product whose users
  are often on a phone at a table? Providers make this easy, Option A makes it a
  choice, and the accessibility gate applies either way.
- Should verification block **all** AI access or only paid AI access? The plan says
  "unverified accounts cannot consume paid AI" (`yje.2.2`), which presumes the free
  tier answer that the design agent is deciding now.

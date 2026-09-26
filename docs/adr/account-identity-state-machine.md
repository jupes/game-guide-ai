# Account identity and state machine (no billing)

Status: proposed · 2026-09-26 · bead `agent-forge-harness-yje.1.6` · every engineering choice is decided here; the owner's choices are in section 12, each with a default that holds until answered

Consumed by: the billing state machines (`yje.1.3`) and the entitlement decision (`yje.4.1`). Blocks: the identity schema migrations (`yje.2.1`). Built by: `yje.2.1` to `yje.2.4` and `yje.2.6`. Related: the identity-implementation record (`yje.1.5`, PR #84, a draft not yet on this branch), account deletion (`agent-forge-harness-zkc`), seats (`agent-forge-harness-fma`), table access (`agent-forge-harness-hgm`).

## 1. Context

The billing plan's lifecycle table mixes two machines: whether an account **exists
and may act at all**, and what an account **has paid for**. The lead split them on
2026-09-26 so that the free signup path does not wait on licensing (`yje.1.1`) or
pricing (`yje.1.2`). This record is the first machine. It reads no billing state and
defines none; section 10 is the whole interface between the two.

### 1.1 What exists today, from the code on this branch

| Fact | Where |
|---|---|
| Accounts exist only by atomically redeeming an invite; the invite fixes a `player` or `dm` role | `service/auth_store.py`, `docs/adr/invite-auth.md` |
| Sessions are stateless signed cookies (user id and role); no session table | `service/session.py` |
| **The account is re-read from the store on every request**; a missing account answers 401 and a failed lookup 503, never "allowed" | `require_session` in `service/app.py` |
| No email verification, no password reset, no age question, no suspension, no deletion code path | bead `yje.2.2`, bead `cug`, bead `zkc` |
| `auth.invites.used_by` is `ON DELETE SET NULL`; `chat.conversations` ownership is `ON DELETE CASCADE` | `service/sql/migrations/0002_auth_schema.sql` |
| `campaign.campaigns.owner_id` and `campaign.table_sessions.gm_user_id` are `ON DELETE CASCADE`: deleting a GM's row deletes their campaigns and everything under them | `service/sql/migrations/0004_campaign_schema.sql` |
| `campaign.participants.user_id` is `ON DELETE NO ACTION` on purpose: deleting an account that holds **any** seat row, removed or not, is refused by PostgreSQL until `zkc` decides what the seat becomes | `service/sql/migrations/0009_participant_accounts.sql` |
| A seat is open, offered, accepted or removed; only an offer followed by **that same account's** acceptance seats an account; `seat_for` answers only for an accepted, live seat; the owner is never offered a seat in their own campaign | `service/participant_store.py` |
| The audit ledger is campaign-scoped and its `actor_kind` is `gm`, `participant`, `guest` or `system` — there is no operator actor and no account-level ledger | `service/sql/migrations/0005_audit_events.sql` |

### 1.2 Owner decisions this record applies (2026-09-21)

- **D-1** every player holds an account; **D-4** no guests, a shared screen is signed in by someone.
- **D-3** Free is the cheap surfaces — capped chat, campaigns and documents, joining tables as a player. The tenth lifecycle state (bead `idm`) is *verified, never subscribed*: Free presupposes a verified email.
- **D-5** creating a campaign makes you its GM; there is no account-level role (interactions record A-25, threat model TA-3).
- **D-6** minimum age 13+, self-attested at signup, the refusal remembered so a retry does not work, no guardian path; the owner confirms with counsel before launch.

### 1.3 Scope

**In:** the identity states, the 13+ gate before an account exists, every transition
and its trigger, the races between transitions, operator actions and appeal, how
campaign ownership and seats behave in each state, and one answer table.
**Out:** subscription, payment, promotion, sponsorship, corpus entitlement, usage
budget, grace and cancellation (`yje.1.3`); the tier an account is on (`yje.1.2`);
how sessions and tokens are implemented (`yje.1.5`); what a deleted account's rows
become after erasure (`zkc`); the grounds for suspension (the published terms, `yje.6.1`).

## 2. Decision in brief

1. **Four account states: Unverified, Verified, Suspended, Deleted.** "Free" is not
   an identity state: it is billing's name for a Verified account holding no paid
   grant (section 10). There is no role (D-5) and no "deletion pending" state (OQ-2).
2. **The state is derived, never stored twice.** Three timestamps on the account
   decide it by a fixed precedence — Deleted, then Suspended, then Verified, else
   Unverified — so no combination of columns is ambiguous (section 3.2).
3. **The per-request identity read is the guarantee.** Every request already re-reads
   the account; it now reads the state too. Revoking sessions on a transition is
   hygiene, not the guarantee, so a session or write that slips past a transition is
   inert on its next use (section 6).
4. **The 13+ gate runs before the account exists and before the email field is shown.**
   A refusal stores nothing on the server — no row, no request, no log line — and
   leaves a constant-valued marker in the refusing browser for 365 days (section 4).
5. **A campaign is available only while its owner is Verified, and a seat is usable
   only while its account is Verified and its campaign is available** (section 8).
6. **Closure takes effect at once; erasure follows.** Deleting an account closes the
   identity in one transaction; what the data becomes is `zkc`'s and never delays the
   closure (sections 5 and 8.4).

## 3. States

### 3.1 The four states

| State | Meaning | Entered by | Left by |
|---|---|---|---|
| **Unverified** | An account exists; control of its email address is not proven | Signup (T-1); operator re-verification (T-7); the legacy migration (T-15) | Verification or a completed reset (T-4); suspension (T-8); closure (T-11, T-12, T-13) |
| **Verified** | Control of the address is proven. The only state from which any surface, free or paid, can be used | T-4; lift of a suspension of a verified account (T-9) | Re-verification (T-7); suspension (T-8); closure (T-11, T-12) |
| **Suspended** | An operator has stopped the account, for abuse or for an under-age review. Reversible | T-8; an under-13 attestation by a legacy account (T-16) | Lift (T-9); operator closure (T-12) |
| **Deleted** | The identity is closed: nothing signs in, nothing is served, the address is free to register again. Terminal | Self-service closure (T-11); operator closure (T-12); closure of a never-verified account (T-13) | Nothing. Erasure (T-14) removes data; it is not a transition |

A visitor refused by the age gate is **not** a state: no account exists, and nothing
on the server records the visitor (section 4).

### 3.2 Representation

`yje.2.1` names the columns; this record fixes their meaning. The account row carries:

| Column (indicative) | Set by | Cleared by |
|---|---|---|
| `email_verified_at` | T-4 | T-7 (and an email change never clears it: T-6) |
| `suspended_at` with `suspension_reason` in (`abuse`, `age`) | T-8, T-16 | T-9 |
| `deleted_at` | T-11, T-12, T-13 | never |
| `age_attested_at` with `age_gate_version` | T-1, or T-15's attestation step | never; erased with the account |

```
identity_state(row) =
  Deleted     if row.deleted_at is not null
  Suspended   else if row.suspended_at is not null
  Verified    else if row.email_verified_at is not null
  Unverified  otherwise
```

**Why derived by precedence rather than one status column.** Lifting a suspension
must return an account to exactly the verification state it had, and a verification
link consumed while an account is suspended must still count once the suspension is
lifted (race RC-9). A single status column would have to remember the prior state
somewhere else anyway; three facts and one precedence cannot disagree with each other.
Two constraints keep the facts well formed: `suspension_reason` is set exactly when
`suspended_at` is, and `deleted_at` once set is never cleared (no path writes it back).

**The email's uniqueness covers live accounts only.** The case-folded unique index on
the address (migration 0002) becomes partial, over rows whose `deleted_at` is null,
so a closed account's address is free to register again at once (OQ-3 may impose a
window) while the closed row still exists for erasure to find.

**A Deleted row answers as no row.** Every lookup that today returns "no such account"
— sign-in by email, the per-request read by id, the offer's existence check — returns
the same for a Deleted row. Nothing outside the closure and erasure procedures can
tell a Deleted account from one that never existed.

## 4. The 13+ gate

### 4.1 Where it runs

The signup form's **first step is the age question, alone**, before the email and
password fields exist on the page. It is a **neutral** question: month and year of
birth, with no default, no pre-selected year and no wording that reveals the
threshold. The browser computes the answer; **the date never leaves the browser.**
Only a pass reveals the rest of the form.

Why first: the identity-implementation record (`yje.1.5`, section 6) already requires
the gate to run before any account or provider identity is created. Running it before
the email field is shown goes one step further: a refused child has typed nothing that
identifies them, so the product never holds an identifier of a person it knows to be
under 13 — the "actual knowledge" that bead `cug` warns about. Why neutral: a yes/no
"I am 13 or older" box tells the visitor the right answer; the FTC's COPPA guidance on
age screens recommends asking for age neutrally and using a cookie to stop a child
going back and entering a different age. Counsel confirms both (OQ-1).

### 4.2 What is stored, exactly

| Item | Where | Contents | Lifetime | Why |
|---|---|---|---|---|
| Refusal marker | The refusing browser only, in two copies: a local-storage entry the UI reads, and a first-party cookie scoped to the signup endpoint's path (today `/auth/signup`), `Secure`, `SameSite=Strict`, `HttpOnly` | A constant value meaning "refused"; the local-storage copy also holds its own expiry date, because local storage has none. No birth date, no age, no identifier, no signature | 365 days from the refusal (the cookie's `Max-Age`), **never renewed** by later visits | D-6's memory, with nothing on the server. The UI reads its copy to show the refusal instead of the form; the server reads the cookie on a signup (next paragraph). The cookie is scoped to signup so that it rides on nothing else: a parent signing in on a shared computer must not send the server a sign that a child uses it. A year matches the granularity of the question (a year of age) and is not renewed, so it cannot become permanent |
| Refusal on the server | **Nothing** | No row, no counter, no IP address, no request | — | The refusal is decided in the browser and **sends no request**, so not even Cloud Run's request log records an address next to the fact "under 13" |
| Attestation on an account | The account row | `age_attested_at` and `age_gate_version` (which wording and threshold were shown) | The account's life; erased with it | Evidence that the gate was passed, without keeping a birth date |
| Birth date | **Nowhere** | — | — | Nothing downstream needs it; a stored birth date is personal data held for no purpose |

The signup request carries only the attestation's version. The server refuses a signup
that lacks it, and refuses a signup that carries the refusal marker, **before anything
in the body is stored or logged** — the same refusal the browser shows, identical in
status and body.

### 4.3 Retry

- **Same browser, same sitting, or any later visit within 365 days:** the marker is
  set before the refusal renders, so the back button, a reload or a different date
  shows the refusal again, and a request crafted around the UI is refused by the server.
- **A second tab opened before the refusal** (race RC-4): the UI re-reads its copy of
  the marker when the age step is answered and again when the form is submitted, not
  only on load, and the submission carries the cookie the first tab set, so it is
  refused either way.
- **What it does not stop:** clearing site data, a private window, another browser or
  another device. No self-attested gate can stop those, and every stronger memory —
  keying the refusal on the email, the IP address or a device fingerprint — would store
  an identifier of a child the product knows to be under 13, which the billing plan's
  D-6 row rules out: the marker "must not itself be personal data beyond what is
  needed to refuse". The owner may choose otherwise with counsel (OQ-1).
- **The marker gates signup only, never sign-in.** A family computer on which a younger
  sibling was refused still signs a parent in. A wrongly refused 13+ visitor on a shared
  browser signs up from another browser profile or device; support cannot lift a marker,
  because the server has none.

### 4.4 After an account exists

**Actual knowledge after signup** — a parent's report, an operator's finding, or a
legacy account answering under 13 — is handled by the operator's under-age path:
suspend with reason `age` at once (T-8 or T-16), then close (T-12). The address is
**never** kept to block a new signup, whatever OQ-3 decides for other closures:
keeping a child's address to refuse them would itself retain the child's personal data.
There is no guardian path (D-6).

## 5. Transitions

Every transition takes the account row first (section 6.1), decides on the row it
holds, and writes an account-level audit record (section 7.3).

| # | From | Trigger | Guard | To | Effects |
|---|---|---|---|---|---|
| T-1 | (no account) | Signup submitted | Attestation present; no refusal marker; the address matches no live account | Unverified | Row with the password hash and the attestation; a verification link mailed. **If the address is taken, nothing is created and the response is byte-identical** (`yje.2.2`); the address's owner is told by email |
| T-2 | (no account) | Age step answered under 13 | — | (no account) | Refusal marker set in the browser; no request (section 4) |
| T-3 | (no account) | Signup attempted with the marker present | — | (no account) | Refused, identically to T-2's screen; nothing stored or logged |
| T-4 | Unverified; also Suspended with no `email_verified_at` | A verification link consumed, **or a password reset completed** | Token unconsumed, unexpired, bound to the account's current address; account not Deleted | Verified (or stays Suspended, now with the fact recorded) | `email_verified_at` set; the token consumed by a guarded update, as invites are today. A completed reset proves control of the same mailbox, so it verifies too |
| T-5 | Unverified | Resend, or change of address | Rate limits (`yje.2.2`) | Unverified | A new token; **every earlier verification token of the account is invalidated**, so at most one is live. A changed address replaces the old one at once (nothing was proven about it); an address that is taken gets the same answer as a free one, as in T-1 |
| T-6 | Verified | Change of address confirmed from the new address | Token bound to the pending address | Verified | The address is replaced only when the new one is proven; until then the old one stays in force. Other sessions revoked (`yje.2.3`) |
| T-7 | Verified | **Operator requires re-verification** (the mailbox is reported compromised or recycled) | — | Unverified | `email_verified_at` cleared; every session revoked; outstanding verification tokens invalidated; live table sessions the account runs ended |
| T-8 | Unverified or Verified | **Operator suspends**, reason `abuse` or `age` | — | Suspended | Every session revoked; the account's table grants revoked as an End revokes them (SEC-9); live table sessions it runs ended; seats and campaigns kept (section 8) |
| T-9 | Suspended | **Operator lifts** (appeal upheld, review cleared) | Not Deleted | Unverified or Verified, by `email_verified_at` | `suspended_at` cleared. Nothing restarts: the holder signs in again; ended table sessions stay ended |
| T-10 | Suspended | Appeal rejected | — | Suspended | Audit record; the outcome mailed to the account's address |
| T-11 | Unverified or Verified | **Self-service closure** | The password re-entered in the same request | Deleted | `deleted_at` set; every session revoked; outstanding tokens deleted; the address leaves the unique index; live table sessions it runs ended; its campaigns unavailable; its seats left as they are and unusable by section 8.1 (their afterlife is Z-1); erasure queued (T-14) |
| T-12 | Unverified, Verified or Suspended | **Operator closes** (upheld abuse, under-age, the holder's request while suspended, a legal request) | — | Deleted | As T-11. For reason `age`, OQ-3's retention window never applies |
| T-13 | Unverified, never verified | 30 days since signup (OQ-8) with `email_verified_at` never set | Not a legacy account (T-15) | Deleted | As T-11 |
| T-14 | Deleted | Erasure | `zkc`'s rules and any legal hold | Deleted (row scrubbed or gone) | What each row becomes is `zkc`'s (section 8.4). The answer table is the same before and after |
| T-15 | (legacy invite account) | The identity migration (`yje.2.4`) | — | Unverified | Every existing account enters Unverified: none was ever verified. Its first verification step also asks the age question if `age_attested_at` is empty (OQ-7). T-13 never applies to it |
| T-16 | Unverified (legacy) | The legacy attestation answered under 13 | — | Suspended, reason `age` | As T-8, plus the refusal marker in that browser. An operator closes (T-12); nothing is erased automatically on one entered date |

**No other transition exists.** In particular: an operator cannot mark an address
verified (verification is the free tier's abuse control, and a manual override is the
bypass a social engineer asks for; a mailbox problem is fixed by changing the address,
T-6 or T-5); nothing leaves Deleted (an operator suspends while unsure and closes only
after review, so there is no mistaken closure to undo); an age attestation is never
edited.

## 6. Races

### 6.1 Two rules that decide every race

**R-1: every transition takes the account row with `FOR NO KEY UPDATE` before deciding.**
Under READ COMMITTED a waiter re-reads the row the lock was for, so the second of two
transitions decides on what the first committed — it is the row lock that orders them,
never an `EXISTS` in the statement. `FOR NO KEY UPDATE`, not `FOR UPDATE`, for the
reason `service/participant_store.py` gives: a foreign-key check from a campaign, a
seat or a session row takes `FOR KEY SHARE` on the account, which conflicts only with
`FOR UPDATE`; an operator's transaction must not stall every write that merely
references the account.

**R-2: the per-request identity read is the guarantee; revocation is hygiene.** Every
authenticated request already re-reads the account (`require_session`); it now derives
`identity_state` from the same row and refuses per section 9. So a session issued, a
seat accepted or a campaign created concurrently with a transition is **inert** from
its next use. This record therefore requires the per-request read under **either**
outcome of `yje.1.5` — with a session table, with the stateless cookie, or with a
provider's token.

### 6.2 The races

| # | Race | Order A | Order B | What the holder sees |
|---|---|---|---|---|
| RC-1 | **Verification against closure** | Closure commits first: its tokens are deleted, and the verification's guarded update matches no live account | Verification commits first: the account is Verified, then closure makes it Deleted | Either "this link is invalid or has expired" (the same words as any stale link) or a confirmation followed by a closed account. Both orders end Deleted; neither enumerates |
| RC-2 | **Suspension against sign-in** | Suspension commits first: the credential check succeeds, the state read says Suspended, no session is issued, the suspension notice is shown | Sign-in reads Verified, suspension commits and revokes sessions, then the new session is written: the session exists, but R-2 refuses its first use and revokes it | The suspension notice, at once or on the next request |
| RC-3 | **Suspension or closure against an in-flight write** (a campaign created, a seat accepted, a table session started) | The transition commits first: the write's own state check refuses it | The write commits first, or lands after the transition's revocation step: it exists and is inert, because the campaign is unavailable or the seat unusable (section 8). A table session started in the gap serves nothing, since every table grant re-checks section 8.1, and is retired by its own `expires_at` (migration 0004) | Nothing that works; no partial state is visible to anyone else |
| RC-4 | **Age refusal against a second tab** opened before the refusal | — | — | The second tab re-reads the marker at its age step and at submission, and its submission carries the cookie: refused either way (T-3) |
| RC-5 | **A seat offer against the offered account's closure** | Closure commits first: the offer's existence check treats the Deleted row as no row and the offer is refused (`SeatUnavailable`, as for a missing account) | The offer's check passes, closure commits, the offer commits: an offered seat on a Deleted account, which can never be accepted (no session) and which erasure finds by its account at erasure time. The GM removes it as any unanswered offer | The GM sees no answer different from an account that never existed |
| RC-6 | **Lift against close** by two operators | Close first: the lift's guard (not Deleted) matches nothing and reports "already closed" | Lift first, then close: the account ends Deleted | — |
| RC-7 | **Address change against a stale verification link** | — | — | A token is bound to the address it was sent to; once the address or the pending address changes, the old token fails as any stale link |
| RC-8 | **One link consumed twice** | — | — | A guarded update, as invite redemption does today: the second consumer matches zero rows and sees the stale-link answer |
| RC-9 | **Re-verification or suspension against a verification in flight** | The operator's transition commits first: re-verification (T-7) invalidates outstanding tokens, so the in-flight link fails; a suspension leaves the token valid and T-4 records the fact without leaving Suspended | The verification commits first, then the operator's transition applies | Deterministic by R-1; no order leaves an account Verified that an operator has just re-marked |

## 7. Operator actions and appeal

### 7.1 What an operator may do

| Action | From | To | Reason codes | Effects |
|---|---|---|---|---|
| Suspend | Unverified, Verified | Suspended | `abuse`, `age` | T-8 |
| Lift | Suspended | Unverified or Verified | `appeal_upheld`, `review_cleared` | T-9 |
| Reject appeal | Suspended | Suspended | `appeal_rejected` | T-10 |
| Require re-verification | Verified | Unverified | `mailbox_compromised`, `mailbox_recycled` | T-7 |
| Close | Unverified, Verified, Suspended | Deleted | `abuse_upheld`, `age`, `holder_request`, `legal_request` | T-12 |
| Revoke every session | any but Deleted | unchanged | `security` | The recovery for a compromised password once reset exists (`yje.2.3`); it replaces today's delete-and-re-invite runbook (`docs/deploy-gcp.md` section 10) |

An operator may **not**: mark an address verified, reopen a Deleted account, edit an
age attestation, or look up a refusal (there is none to look up).

### 7.2 Tooling

Operator actions are a command-line tool in the manner of `service/admin_invites.py`,
not an admin UI: the product has one operator. Each action takes the account id, a
reason code and nothing else; free text about a person never enters a record.

### 7.3 Audit

Every transition, the holder's and the operator's alike, writes an account-level audit
record: the account id, the transition number, the reason code, the actor (`holder`,
`operator`, `system`) and the time. The existing ledger cannot hold these as it stands
— it is campaign-scoped and has no operator actor (section 1.1) — so `yje.2.1` either
extends it by a new migration or adds an account ledger. Whether these records outlive
the account is one of `zkc`'s questions (Z-4).

### 7.4 Appeal

A suspended holder who signs in with the correct password sees a notice naming the
**category** (`abuse` or `age`) and the appeal route; a wrong password gets the same
answer as for any account, so the notice enumerates nothing. The appeal goes to the
operator, who lifts (T-9) or rejects (T-10); the outcome is mailed to the account's
address. **A suspension has no automatic expiry**: an automatic lift re-admits abuse
nobody reviewed, and an automatic closure destroys data nobody reviewed. The channel,
the response time and the fate of a suspension never appealed are the owner's (OQ-4).

## 8. Campaigns and seats in each state

### 8.1 Two derived rules

- **A campaign is available** — to its owner, to its seated players, and to every
  table route — **only while its owner is Verified.**
- **A seat is usable only while it is accepted and not removed, its account is
  Verified, and its campaign is available.**

An **offer** may be made to any account that is not Deleted (the store's existing
rules otherwise, `service/participant_store.py`); **accepting** requires Verified. The
offer route (`1kg.2.2`) gives the GM no answer that depends on the offered account's
state beyond what the store already answers for a missing account: another account's
suspension is that account's personal data.

These rules add a conjunct to every grant; they loosen nothing. A route that also
requires the `dm` role keeps that check until entitlement replaces it in its own bead
(interactions A-25, threat model TA-3).

### 8.2 Per state

| State | Campaigns it owns | Its seats at other GMs' campaigns | What others see |
|---|---|---|---|
| Unverified | Cannot create any. One that re-verification (T-7) or the legacy migration left it owning is unavailable until it verifies | May be offered one; cannot accept; an accepted seat it already holds is unusable until it verifies | Its players see the table as unavailable; its GM sees it as absent |
| Verified | Available | Usable | Normal |
| Suspended | **Kept, untouched, unavailable.** Its live table sessions are ended at T-8 (an End, so SEC-9's revocation applies). Nothing is erased | **Kept, not removed**, so a lift restores the table without a new offer. Unusable; open streams stop receiving as SEC-9's revocation stops them | Seated players see neutral "This table is unavailable" — **never** that the GM was suspended. A GM sees a suspended player exactly as an absent one and may remove the seat as always |
| Deleted | Unavailable from T-11/T-12/T-13 on; erased with the account at T-14 (the existing cascade) | **Left as they are**, and unusable from the moment of closure by section 8.1, so nothing more reaches the account. Whether closure marks them removed, and what the row becomes, is `zkc`'s (Z-1) | Seated players of a deleted GM lose the table and, with it, the documents linked to their seats (OQ-6). Until Z-1 is decided, a GM sees a deleted player's seat exactly as an absent one and may remove it as always |

### 8.3 Why these choices

- **Suspension keeps seats and campaigns** because it is reversible: removing seats
  would make a lift incomplete, and erasing anything before review is the harm
  suspension exists to avoid.
- **Unavailable, not read-only, for a suspended owner's players**, because abuse may be
  the campaign's content; the players lose a view of an abusive GM's material, not
  their accounts or their other tables.
- **Neutral copy** because telling players, or a GM, that someone was suspended
  discloses one account's standing to another.
- **Closure changes no seat row** because section 8.1 already stops a closed account's
  seats the moment the closure commits, so this machine is deterministic without
  choosing among `zkc`'s options — every one of them stays open.

### 8.4 What `zkc` decides — named here, not decided

- **Z-1 A deleted account's seat rows.** Whether closure marks them removed (which
  would also stop that member's live copies, A-20, under A-17's lock order); and
  afterwards, whether to keep the account reference and never erase the account row; detach the account (which, under 0009's check, also clears
  `accepted_at` and loses who accepted); or delete the row (which breaks "removal
  marks, never deletes" and the rows that reference a seat). Until decided, the
  foreign key refuses erasure of any account that ever held or was offered a seat,
  and **closure does not wait for it**.
- **Z-2 Chat history,** which cascades with the account today.
- **Z-3 Usage and metering rows,** which have no foreign key and outlive the account
  under a dead id: kept, anonymised or deleted, and for how long.
- **Z-4 Audit actor references,** in the campaign ledger and in section 7.3's account
  records.
- **Z-5 The erasure deadline and legal holds,** with the retention policy
  (`1ir.1.7`) and the legal review (`1ir.1.4`).
- **Z-6 The 13+ refusal marker.** Answered here: nothing server-side exists, so
  deletion has nothing to erase; the attestation columns go with the account row.
- **Z-7 Re-test RC-5 against the real deletion path,** as `zkc`'s own note from the
  seat review asks.

## 9. The answer table

This is the one deterministic answer. Each cell is **identity's** answer: a "Yes" is
necessary, not sufficient, because entitlement (`yje.4.1`) and the existing `dm` check
(A-25) may still refuse; a "No" is final and is given **before** entitlement is asked.

| State | May sign in | May create a campaign | May accept a seat | May use a free surface | Self-service it may use | Answer to an authenticated request |
|---|---|---|---|---|---|---|
| Unverified | **Yes**, into the account page only | **No** | **No** | **No** | Verify or resend, change the address, reset the password, close the account, sign out | 403 `email_unverified` on anything but the account page |
| Verified | **Yes** | **Yes** | **Yes**, a seat offered to this account | **Yes** | All | As entitlement decides |
| Suspended | **No**: a correct password gets the suspension notice and no session; a wrong one gets the common failure | **No** | **No** | **No** | None in the product; the appeal and any data request go to the operator | 403 `account_suspended`, and the session is revoked |
| Deleted | **No**: identical to an address with no account | **No** | **No** | **No** | None | 401, exactly as for an account that does not exist |

And before any account exists:

| Visitor | May sign up | May sign in to an existing account |
|---|---|---|
| Browser carrying the refusal marker | **No**, the same refusal the gate showed | **Yes**: the marker gates signup only |
| Any other browser | **Yes**, after passing the age step | **Yes** |

**Why Unverified may sign in at all.** Correcting a mistyped address, resending and
closing the account each need a proven credential, and the restricted session reveals
nothing the password did not. **Why it may not accept a seat.** Joining a table is a
Free surface (D-3), and Free presupposes a verified address (bead `idm`).

## 10. Interface to the billing machines

- **Identity exports one function, `identity_state(account)`, and the transitions
  T-8, T-9, T-11, T-12 and T-13 as events.** The billing machines (`yje.1.3`) read the
  state and react to the events. Identity reads **no** billing column, tier,
  subscription, promotion, sponsorship or entitlement, and waits on none: a payment
  provider outage never delays a suspension or a closure.
- **Identity vetoes first.** Every entitlement decision (`yje.4.1`) starts from section
  9; no subscription, promotion or sponsorship lets an account that is not Verified use
  anything, free or paid.
- **Free** (D-3, bead `idm`) is billing's name for a Verified account holding no paid
  grant. The billing plan's rows *Email unverified*, *Suspended for abuse* and
  *Deleted* are this record's states seen from billing.
- **What billing decides on each event is `yje.1.3`'s**: whether a suspension pauses or
  cancels a subscription, whether a closure cancels it and refunds, and whether it
  deletes the billing customer (the identity-implementation record's open question).
- **No dependency on `yje.1.1` or `yje.1.2`**: nothing here depends on licensing or on
  prices.

## 11. What this obliges downstream

| Bead | Obligation |
|---|---|
| `yje.2.1` | The columns and precedence of section 3.2; the partial unique index on the address over live accounts; the constraint pairing `suspension_reason` with `suspended_at`; an account-level audit record (section 7.3) |
| `yje.2.2` | The age step first and alone; the date computed in the browser and never sent; the marker set in both copies before the refusal renders, the cookie scoped to the signup path and the local copy re-read at the age step and at submission; the server refusing a signup without an attestation or with the marker, before storing or logging the body; resend invalidating earlier tokens (T-5); the byte-identical taken-address response |
| `yje.2.3` | A completed reset also verifies (T-4); revocation on T-7, T-8 and T-11 to T-13; the per-request identity read kept whatever session model is chosen (R-2) |
| `yje.2.4` | T-15 and T-16; the legacy accounts' communication; OQ-7's answer |
| `yje.2.6` | The neutral age screen, the refusal screen, the unverified account page, the suspension notice and the unavailable-table copy |
| `1kg.2.2` | The offer route answers identically whatever the offered account's state; `offer`'s existence check in `service/participant_store.py` treats a Deleted row as no row once `yje.2.1` adds `deleted_at` |
| `hgm`, `1kg.7.1`, `1kg.7.2` | The table grant includes both conjuncts of section 8.1 — the seat's account Verified and the campaign's owner Verified — in the same query that resolves the seat; T-7, T-8 and T-11 to T-13 revoke table grants as an End does (SEC-9) |
| `yje.4.1` | Section 9 is evaluated first and a "No" is final |
| `yje.1.3` | Section 10 |
| `zkc` | Section 8.4; erasure (T-14) never gates closure |

## 12. Questions for the owner

Each has a default that holds until answered, so section 9 stays deterministic.

| # | Question | Default until answered |
|---|---|---|
| OQ-1 | **Counsel's confirmation of D-6's mechanism**: the neutral month-and-year screen; the browser-only marker, its constant value and its 365-day unrenewed life; no server record of a refusal; whether the minimum age varies by country (GDPR Article 8 lets member states set 13 to 16 where processing rests on consent); and whether a stronger memory is wanted despite storing an identifier of a known child | Section 4 as written; one minimum of 13 everywhere |
| OQ-2 | **An undo window for self-service closure** (a "deletion pending" state that signs in only to cancel) | None: closure is immediate and terminal |
| OQ-3 | **How long a closed account's address stays unavailable** (`yje.1.5` section 11, item 7) | Free again at once; an under-age closure never keeps the address whatever the answer |
| OQ-4 | **Appeals**: the channel, the response time, how many appeals, and what happens to a suspension never appealed | One support address; no automatic expiry; nothing erased automatically (retention by `1ir.1.7`) |
| OQ-5 | **A suspended holder's own data**: whether they may export it or close the account during suspension | Through the operator; a closure request is honoured unless the operator records a hold for the abuse evidence under the retention policy |
| OQ-6 | **Campaign continuity when a GM leaves**: transferring ownership, and whether seated players keep a copy of documents linked to their seats | No transfer; the documents go with the campaign |
| OQ-7 | **Legacy pilot accounts**: whether they attest their age, and whether they get a grace period before verification is enforced | They attest at their first verification step; no grace (verification is one click, and there are a handful of known testers) |
| OQ-8 | **How long a never-verified account lives** (T-13) | 30 days: long enough for a delayed message and a resend, short enough not to hold addresses nobody proved |
| OQ-9 | **What counts as abuse** — the grounds for T-8 | The published terms (`yje.6.1`); this record defines the state, not the grounds |

## 13. Alternatives rejected

- **A server-side refusal record keyed on the email, the IP address or a fingerprint.**
  Each stores an identifier of a person known to be under 13, and the email variant
  needs the email collected after the refusal. An IP key also refuses every other
  person behind a school's or a family's address.
- **Storing the birth date.** No decision downstream reads it.
- **A "Free" identity state, or a role.** Free is a billing fact (D-3), and the role is
  retired (D-5); either here would make identity read billing or restore what D-5 removed.
- **Locking the account row `FOR SHARE` on every write so no write can slip past a
  transition.** It costs a lock on every request to prevent writes that R-2 already
  makes inert.
- **Marking, cascading or nulling a deleted account's seats now.** All are `zkc`'s
  options (Z-1) with known costs, and section 8.1 already makes the seats unusable, so
  none is needed for this machine to be deterministic.
- **Letting an operator verify an address by hand.** See section 5.

## 14. Consequences

- Open signup (`yje.2.2`, `yje.2.6`) can be built against section 9 without waiting
  on prices or licensing.
- The age gate is honest about its limit: it stops the retry D-6 names in the browser
  where it happened, and nothing more, by design.
- A suspended GM's players lose that table until a lift, and are not told why.
- Until `zkc` decides Z-1, a closed account that ever held a seat cannot be erased; it
  is closed, serves nothing, and waits.
- This record supersedes nothing yet: the invite record stays in force until open
  signup ships (its superseding note), and this machine takes effect with `yje.2.1`
  to `yje.2.4`.

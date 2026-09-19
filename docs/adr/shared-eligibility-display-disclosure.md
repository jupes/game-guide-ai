# Shared field eligibility, display and disclosure

- **Status:** Proposed — defaults in force. Signed off for the Workbench by its implementation session as the owner's delegate (section 13); the owner's confirmation of O-1 to O-7 is outstanding and tracked in `agent-forge-harness-1kg.1.7`
- **Date:** 2026-09-19, revised the same day after an independent review (section 15)
- **Bead:** `agent-forge-harness-1ir.1.2` (`LSA-1.2`)
- **Binds:** the GM Workbench (epic `1kg`) and the Live Session Assistant (epic `1ir`)
- **Unblocks:** `1kg.2.1`, `1kg.5.1`, `1kg.5.3`, `1kg.1.6`, `1kg.7.1`, `1ir.2.1`, `1ir.1.12`, `1ir.1.13`

## 1. Context

Two plans share one question: **which player may see which field of which
document, when, and how do we know afterwards what was shown?** The Workbench
answers the *when* — a GM reveals explicit fields of one pinned version to an
audience slot, and Stop always wins (`docs/adr/gm-workbench-interactions.md`,
sections 7 and 8). The Live Session Assistant plan
(`docs/forge/plans/live-session-assistant.md`, sections 2.4, 2.5, 4.2, 4.3 and
4.9) adds the *ever* — a per-field eligibility class that automation must never
exceed — and the *afterwards* — an append-only disclosure ledger. It also asks
the Workbench for four amendments, each with a fallback.

This record adopts the plan's three layers, fixes the one identifier both plans
share, says which layer binds what in which release, and answers each amendment.
It decides nothing about capture, consent or transcripts.

### 1.1 How to read this record

Every decision has an ID and a **basis**: **W** — a Workbench decision or
invariant (its ID is given); **L** — the Live Session Assistant plan, **LD** its
delivery plan; **T** — the Workbench threat model (`SEC-`); **C** — wire contract
v1; **I** — inferred here, with the reasoning; **E** — inferred *and* a change of
product scope, so the owner should confirm it (section 12). Each **E** has a
default in force.

"Workbench v1" means the scope of epic `1kg`. **"Enforced"** means that
`1ir.11.1` — the plan's Phase 4 bead *Enforce eligibility in the Workbench
projection* — has shipped **and** the GM has switched enforcement on for that
campaign (M-2). Eligibility *rows* exist earlier, from the assistant's Phase 1
(`1ir.2.1`), and a classification surface from Phase 2 (`1ir.2.7`); until a
campaign is Enforced they bind the assistant's own paths only, and Workbench
reveal stays mask-only.

Quotations of **L** and **LD** are from revision `0890269` of branch
`docs/1ir-live-session-assistant-plan` (PR #60). That branch is not merged into
this one, so every "L §" reference resolves when it is.

A requirement on a bead is written in sections 9 and 10 in words that can be
copied to that bead as a comment.

## 2. The model

Three layers, deliberately separate (L §4.2), over one identifier:

| Layer | Question | Owner | Lives in | Release |
| --- | --- | --- | --- | --- |
| **Field key** | What is the unit? | `1kg.5.3` | the document type's schema | Workbench v1 |
| **Eligibility** | Who may *ever* see this field? | `1ir.2.1` (schema), `1ir.2.2` (decision point), `1ir.2.7` (classification surface) | `campaign.field_eligibility` | rows from the assistant's Phase 1; binds Workbench reveal when Enforced (`1ir.11.1`, Phase 4) |
| **Display** | What is live *now*, to whom? | `1kg.7.1`, `1kg.7.2` | audience slots of the live table session | Workbench v1 |
| **Disclosure** | What *was* shown? | `1ir.2.8` | `campaign.disclosure_events`, an append-only ledger | the assistant's Phase 4. Before it, a reveal is a row in the audit log (SEC-38), which is a different store (ED-18) |

Eligibility never displays anything, and a display never changes eligibility:
classification is its own GM action on its own surface (ED-13, M-4). The only
thing that reaches a player is a display, and the only thing that creates a
display is an explicit GM action (X-2).

## 3. Decisions

| ID | Decision | Basis |
| --- | --- | --- |
| ED-1 | **The three layers of L §4.2 are adopted** as written, with the amendments of this table. | L |
| ED-2 | **The shared identifier is the Workbench's flat field key**: a top-level key of the document type's data object, matching `^[a-z][a-z0-9_]{0,39}$`. It is at once the unit of eligibility, of a reveal mask (REVEAL-9), of field-level concurrency (CANVAS-19), of changed-field lists and of AI-edit scope. **There are no nested paths**: the plan's `field_path` is this key, and its example `motives.hidden_identity` is two fields, `motives` and `hidden_identity`. A secret that needs its own eligibility is its own top-level field — a schema-design rule for `1kg.5.3`. Two units of visibility for one document would let a mask say "shown" where eligibility says "never", about different slices of the same text. | W (CANVAS-19, REVEAL-9) + C + I |
| ED-3 | **List fields are one field** (REVEAL-12): eligibility, like reveal, is whole-list in v1. Per-item eligibility needs per-item reveal first. | W + I |
| ED-4 | **Seven classes, default deny**: `unclassified`, `gm_only`, `participants[ids]`, `characters[ids]`, `groups[ids]`, `campaign`, `public` (L §4.2). A revealable field with no row is `unclassified`. `campaign` excludes guests; `public` includes them and public exports. `characters[ids]` and `groups[ids]` resolve to participants **at the moment of evaluation**, so a relink or a membership change never rewrites rows. | L |
| ED-5 | **Only fields on the type's revealable allowlist can be classified** (REVEAL-10). Every other key — tags, sources and citation text, version history, authorship, changed-field lists, asset metadata, IDs, and any field a type marks not revealable, such as an identity link (ED-20) — is **`gm_only` by construction**: it has no eligibility row, evaluates as `gm_only`, cannot be widened by any action, and is **not** counted as `unclassified` by the classification queue (`1ir.2.7`). | W + L §2.4 + I |
| ED-6 | **Eligibility is per document and per field key, never per version. It says that the field may be shown; it never says which text.** For a display the *pin* says which text: the table keeps the pinned version until the GM reviews `Use latest version` (REVEAL-8). For the assistant's table namespace the *approved version* says which text (L §4.3). **A new version of an eligible field is neither pinned nor approved until the GM acts, whoever wrote it** — a person or an AI edit. Until then the table namespace keeps the last approved version's text, or nothing. | W (REVEAL-8, E-3) + L §4.3 + I |
| ED-7 | **`classification_source` is `default`, `gm` or `suggested`, and only `gm` widens.** A type's `defaultReveal` may produce a *suggestion*; it never classifies. A suggestion is never auto-applied (L). | L + W (X-2) |
| ED-8 | **No wildcard is ever stored** — in a mask (REVEAL-9) or in eligibility (L rule 5). `all`, "every field" and bulk actions expand, at the moment the GM decides, into the explicit keys that exist then. A field added to a type later is `unclassified` and unrevealed. | W + L |
| ED-9 | **A reveal Confirm is one transaction whose preconditions are a named, ordered list, in the threat model's order (SEC-3).** Authentication, role and ownership (404) are the route's and come first. A Confirm names the **session** it was composed for as well as the epoch, so that a number from last night's session can never match tonight's. Then, under the campaign's authorisation lock in share mode (RQ-4) and **before the session row is touched**, the validation that depends on the resource — each a 422: (1) the document is not archived, and the named version is sealed and is the one the sheet displayed (REVEAL-5, CANVAS-34); (2) every masked key is on the type's revealable allowlist and is present and non-empty in that version (REVEAL-5, REVEAL-9, REVEAL-10); (3) the audience is one the type allows and, for a participant, names an active participant linked as ED-14 requires; (4) **when Enforced**, every masked key is eligible for the slot's audience (ED-10). Then state — a 409: (5) the table session row is locked, the named session is live and its reveal epoch is the one the Confirm carries (REVEAL-22). Then the write, under the one-slot rule (ED-15). The first failing precondition refuses the whole Confirm; nothing is displayed in part. **One courtesy precedes the list: an epoch that is already stale when read without a lock is answered 409 at once.** Ownership has passed, so SEC-3 protects nothing between those two answers, and the 409 is the useful one — the sheet reloads and keeps its draft (REVEAL-15). A narrowing that lands after that read can still turn a Confirm into a 422, which the sheet treats the same way. | W + T (SEC-3) + L |
| ED-10 | **Slot audiences and eligibility. The audience of a display is an identity, not a credential**: `table`, or `participant:<id>`. Table slot: every masked key must be `public`, because a guest can see the table. Participant slot: every masked key must be eligible for *that participant* — `public`, `campaign`, a `participants` list naming them, a `characters` list naming a character linked to them now, or a `groups` list naming a group they belong to now — **whether or not they have a device yet** (AUD-10). `unclassified` and `gm_only` are eligible for nobody but the GM. | L + W (AUD-10) |
| ED-11 | **Workbench v1 ships mask-only; eligibility binds Workbench reveal from the assistant's enforcement release (`1ir.11.1`, Phase 4), per campaign** (section 8). In v1 the GM's explicit Confirm, which names every field it shows and previews its exact text (REVEAL-5), is the authorisation (X-2). **What this trades away:** eligibility would also guard a *manual* reveal — a field the GM had marked `gm_only` could not be ticked by mistake (TT-3) — and L rule 1 wants eligibility behind every display, manual ones included. v1 has no classification surface, so that guard could only be bought by building one first: enforcing from the first Workbench release would add blocking edges from `1ir.2.1` and `1ir.2.2` to `1kg.7.2` and tie reveal to the assistant's schedule. The plan's own MVP leaves Workbench reveal "under the Workbench's own rules and gates" until its Phase 4 gate (L §7.1). | I · **E** (O-1) |
| ED-12 | **Narrowing always wins (X-3), and anything that shrinks the set of principals a live display's keys are eligible for is a narrowing** — guests included, so `public` to `campaign` stops a table display: a tightened class, a character unlink or relink, a removal from a group, a removed participant. **Every narrowing has the same first step (RQ-5)**: under the session row alone it stops every live display the change makes invalid — the whole display, not the one field — writes `stopped` events and **advances the Workbench reveal epoch**, so a widening the GM staged before it is a 409 (REVEAL-22). That step never waits for the campaign lock and is never refused. The second step, under the campaign lock, changes the fact where there is one to change, scans again, removes the affected table-namespace rows (L §4.3 rule 4) and advances `authz_revision`. **Before a campaign is Enforced a class binds the assistant's own paths only, so a class change stops no Workbench display**; the classification surface then says `The table is seeing this now`, with a Stop beside it (TT-51). | W + L §4.3, §4.9 |
| ED-13 | **Widening eligibility displays nothing, and a display never widens eligibility.** Classifying a field `public` makes it displayable; it does not display it. Classification is a separate, explicit GM action on its own surface (L rule 4; `1ir.2.7`); the reveal sheet shows classes and never sets them (M-4). Additions to a group, a relink *towards* a participant and a new enrolment likewise display nothing: the next thing that participant sees is whatever the GM next reveals to them (X-2, AUD-10). | W (X-2) + L rule 4 |
| ED-14 | **Which audiences a type allows stays AUD-9 in Workbench v1**: a participant audience only for owner-audience types (the character sheet), and only for the participant the sheet is linked to; every other type is table-only. This is a **service rule, not a schema shape** — a slot row carries an audience and a document, and nothing in the schema ties one to the other's type — so lifting it later changes no slot, mask or eligibility row. The amendment that lifts it must still bring A-3's device approval, which *is* a schema and wire change (7.1). | W (AUD-9, NG-22) + I · **E** (O-2) |
| ED-15 | **A document is live in at most one slot, and a slot holds one live projection** (§7.1, REVEAL-7, NG-20). `1kg.7.1` expresses the first half as a **partial unique index** over live rows rather than as a column on the document, so that accepting group displays later is dropping an index and adding a nullable disclosure reference (expand, then contract — `docs/migrations.md`), not a rewrite. | W + I · **E** (O-3) |
| ED-16 | **Stop and Retract.** *Stop* clears a slot now, needs no confirmation and is never refused (X-3, REVEAL-6). *Retract* is Stop plus the statement "this should not have been shown": the ledger records `retracted` instead of `stopped`. Workbench v1 has no automation and therefore no Retract control: **of Stop and Retract, only Stop exists in v1** — End, Rotate, archive, unlink and remove are narrowings of their own (REVEAL-22) — and the distinction arrives with the assistant's Phase 4. A manual reveal after either is still possible: it is a new explicit GM action. | L + W |
| ED-17 | **Ledger events** (`1ir.2.8`) are `displayed`, `updated` (the GM pushed a newer pinned version of a live field), `stopped`, `session_ended`, `retracted` and `exported`. There is one row per **field**: the subject is `(document, field key, version)`, or `(corpus chunk, corpus version)` under 7.4, with the slot (none for an export), a recipients snapshot, the actor or rule, the session and a reason code — references and codes, never field text (SEC-20, ED-26). A field the GM drops from a live mask with `Update` gets its own `stopped(mask_narrowed)`; a field that stays gets `updated` only if its version changed. `session_ended` carries `gm_end` or `expired`, and is written only for fields that were live at the end — never over a `stopped`. A field that an `Update` adds to a live mask gets `displayed`. `stopped` carries one of `gm_stop`, `stop_all`, `mask_narrowed`, `replaced`, `moved`, `link_rotated`, `participant_removed`, `personal_link_reset`, `character_unlinked`, `group_member_removed`, `eligibility_tightened`, `enforcement_enabled`, `document_archived`, `document_deleted`, `campaign_deleted`. **Until an amendment says otherwise (7.3), L rule 1 stands as written: after any `stopped` or `retracted`, automation never re-displays that field to that audience**; the reasons are recorded so that such an amendment *can* tell a GM's intent (`gm_stop`) from a mechanical end (`replaced`). The recipients snapshot is a **lower bound** for a table slot: anyone who joins the link while a display is live sees it too, and the GM's disclosure log says so. | L + T + I |
| ED-18 | **Two stores, as the plan has them (L §4.10), not one.** (a) *The audit log* (SEC-38): from Workbench v1 the reveal service writes one content-free row per **action** — `reveal.displayed`, `reveal.updated`, `reveal.stopped` — carrying the campaign, session, actor, command id, document id, version number, mask keys, audience kind and — deliberately, since an id is content-free — participant id, reason code and the epoch after it; no recipients, no hash, no text. The table's DDL is `1kg.2.1`'s — the first bead whose dependants must audit — in the shape L §4.3 sketches for `audit.events`, without `payload_hash` and with a closed, per-action `detail` of ids, codes and keys; if `1ir.2.6` merges first, the Workbench writes to its table instead (SEC-38). It is one table either way, so nothing is ever "adopted" by a backfill. It follows audit retention (`1ir.1.7`). (b) *The disclosure ledger* (`1ir.2.8`, Phase 4): one row per field per event as ED-17 has it, kept for the campaign's lifetime plus the legal period and tombstoned on deletion (L §3.12), written in the slot's transaction. **Displays made before the ledger exists are not backfilled and count as never displayed for L rule 1** — the safe reading: automation can only ever re-display what the ledger saw the GM display. | T + L |
| ED-19 | **Assets follow their field** (L rule 7, REVEAL-21, MS-7). A portrait is displayable exactly when its `portrait` field is masked into a live slot; it is served through the table asset route on a per-slot handle, `no-store`, authorised against the live slot on every read, and the handle dies with the slot. An asset has no eligibility of its own. | W + L |
| ED-20 | **Identity links are GM-only relations** (L rule 6). "The Hooded Stranger *is* Ondrey" is never a revealable field and never reaches a table namespace, even when both names are `public`. If a type needs the link, it is a field off the revealable allowlist, and so `gm_only` by construction (ED-5). | L |
| ED-21 | **Exports use the same builder and the same rule.** A player-safe export contains the subset the GM ticks in the staged dialog (EXPORT-3), every key of which must be on the revealable allowlist and — when Enforced — `public`: a file can be forwarded to anyone, so no narrower class can be honoured once it has left. The dialog previews the exact text, **never classifies**, and from `1ir.2.8` writes one `exported` ledger row per field (L P10). When Enforced an export takes the campaign lock in share mode, as a display does: it reads classes and writes ledger rows. Print is the same projection (EXPORT-7). | W (EXPORT-3, EXPORT-7, REVEAL-21) + L |
| ED-22 | **Eligibility cannot see inside a field.** An AI edit that moves a secret from a `gm_only` field into a `public` one is not caught by eligibility. The controls are the pin and the approved version (ED-6) — nothing new reaches the table or the table namespace until the GM reads old text beside new — and, for automation, the assistant's pre-generation firewall (L §4.5). This is recorded so that nobody mistakes eligibility for content inspection. | I |
| ED-23 | **An alias is a second kind of eligibility subject, not a second permission model** (L P10). The Workbench stores no aliases of documents or entities; AUD-11's aliases are participants' display names and are never field content. The assistant's alias registry (`1ir.5.1`) gives each alias one of the seven classes, default deny, evaluated by the same function and narrowed under the same protocol as a field. An alias is a resolver input and is never displayed in a slot. A type must not carry names of different visibility in one list field (ED-3): a persona the players may know is its own document or its own top-level field, and the link between personas is ED-20's. | L §4.1, §5.7 + I |
| ED-24 | **A field key is never reused with another meaning.** A type's registry keeps the keys it has retired as *reserved*, and a registry test pins that list (`1kg.5.3`). Evaluation ignores an eligibility row whose key is not on the revealable allowlist of the document's *current* type version (ED-5 makes it `gm_only`), and the step that migrates a document to a newer type version deletes the rows of keys that left and resets to `unclassified` any key that received text from another key, under the narrowing protocol. A release that takes a key *off* a type's revealable allowlist is a narrowing that no transaction carries: it ships with a stop-scan — a job that stops every live display holding that key — and the registry test that pins the allowlist is what prompts it. | C (type versions) + I |
| ED-25 | **Entitlement is not eligibility, and a table read checks entitlement only.** *Eligibility* is evaluated on identities when something is decided: a Confirm, an export, a narrowing scan. *Entitlement* is evaluated on credentials at every table read (threat model §8.2): a live session, the credential's link generation, and for a participant slot the enrolled device credential that names that participant, who must still be active. A read never re-evaluates eligibility, because every narrowing clears the slots it affects in the transaction that makes it effective (ED-12) — the invariant the state-based delivery of RT-5 rests on. **Slot snapshots and slot frames are served from slot state and are never gated on `projection_revision`**; only reads of the assistant's table namespace, and what is composed from them, are (L §4.7). A classification made for one audience is therefore invisible to every other (REVEAL-24). | W + T + I |
| ED-26 | **No value derived from field text outlives the text.** Workbench audit rows carry no hash at all (SEC-38 lists none). The plan's `payload_hash` on ledger and audit rows, which are tombstoned past campaign deletion, is either a hash of references only — version id, key, write revision — or an HMAC under a per-campaign key that is destroyed with the campaign; never a bare digest of field text, which for a name, an attitude or a stat cell is a dictionary lookup away from the text (SEC-20). | T + I |

## 4. Evaluation rules

```text
eligible_for_audience(document, key, audience) when Enforced      -- identities, never credentials
  audience is  table | participant:<id>
  audience is participant:<id> and <id> is not active in this campaign        -> no   (even for a public key)
  key not on the revealable allowlist of the document's current type version  -> no   (ED-5, ED-24)
  class := field_eligibility[document, key] or unclassified
  unclassified | gm_only                          -> no
  public                                          -> yes        (participants, guests, public exports)
  audience is table                               -> no         (a guest can see the table)
  campaign                                        -> yes
  participants[ids]                               -> <id> in ids
  characters[ids]                                 -> <id> is linked NOW to a character in ids
  groups[ids]                                     -> <id> is a member NOW of a group in ids
  used by: the reveal Confirm (ED-9), exports (audience = table), every narrowing scan (ED-12) and,
           through the same function, the assistant's displays and alias checks (ED-23)

entitled(requester, slot)                                          -- credentials, at every table read
  requester := (table credential, optional device credential), resolved by the server, never from request fields
  requires a live session, and the credential's link generation = the session's (SEC-9, RT-7)
  slot is table                                   -> yes
  slot is participant:<id>                        -> the device credential is valid and names <id>,
                                                     and <id> is still active (AUD-5)
  anything else                                   -> the slot does not exist for this requester:
                                                     exactly what an empty table gives, never a refusal
  never consults eligibility (ED-25)

display(slot, document, version, mask) - one transaction (ED-9)
  [route] authentication, role, ownership                                            else 401 / 403 / 404
  courtesy: the named session's epoch, read without a lock, is already stale         -> 409 at once
  lock_campaign(shared)                           (first lock, always - RQ-3, RQ-4)
  validate, reading without row locks:
    the document is not archived; the version is sealed and is the one the sheet displayed   else 422
    every key in mask is revealable, present and non-empty in that version           else 422 (keys named)
    the audience is allowed for the type; a participant is active and linked (ED-14) else 422
    when Enforced: every key in mask is eligible_for_audience(the slot's audience)   else 422 (keys named)
  build the projection with the allowlist projection builder - still before the session row
  lock the table session row (FOR NO KEY UPDATE - RQ-3); require the session the Confirm names
    to be live and its reveal epoch = the Confirm's                                  else 409
  replace what the slot holds; clear the document from any other slot (ED-15)
  advance the reveal epoch and the slot sequence; write the audit row, and from 1ir.2.8 the ledger rows
  notify (a wake-up; delivery reads state - RT-5)

stop(slot or all) - never refused, never takes the campaign lock (RQ-6)
  lock the table session row only (FOR NO KEY UPDATE)
  clear the slot if it holds what the Stop names; advance the epoch even if it was empty
  write stopped (or retracted); notify

narrowing - every one has the same first step (RQ-5)
  step 1 - never takes the campaign lock, never refused:
    lock the participant row it concerns, if any, then the session row if one is live (FOR NO KEY UPDATE)
    a revocation makes its change true here: end the session, advance the link generation, revoke the
      credential and the codes, mark the participant removed (never DELETE)
    stop the displays the change makes invalid; ADVANCE THE REVEAL EPOCH, ON AN EMPTY SLOT TOO
    write events; commit; notify
  step 2 - under lock_campaign(exclusive):
    a fact-changing narrowing - unlink, relink, a removal from a group, a tightened class, document archive
      and delete, campaign archive and deletion, enforcement on - IN THE REQUEST: change the fact, scan
      again, stop what step 1 could not have seen, advance authz_revision. If the lock cannot be had in
      time: a retryable answer that says "not applied yet". The displays are already stopped, the fact is
      unchanged, the client retries. A GM's intent is never replayed from a job.
    a revocation - End, expiry, Rotate, Remove participant, Reset personal link - leaves a RECONCILIATION
      job: after the response is sent, and retried by the job runner until done, it clears every live slot
      whose session is not live or whose participant is not active, runs the table-namespace narrowing
      when there is one, and advances authz_revision. It reads current state and never replays a payload.

enrolment and device replacement - a player's unauthenticated route (RQ-5)
  look the code up before any lock, so that a bad code takes none
  lock the PARTICIPANT row (FOR NO KEY UPDATE) - the row that Remove and Reset lock
  consume the code with a conditional write under that lock; no row changed -> the one generic failure
  create the credential, revoke the older device; stop nothing, advance no epoch; leave a reconciliation job

locked widening - participant add; a link towards a participant; session start; and, when they exist:
                  a class that widens, version approval, a group addition, enforcement off - one transaction (RQ-4)
  lock_campaign(exclusive) first; change the fact; advance authz_revision; display nothing (ED-13)
  if the lock cannot be had in time: a retryable 503
```

**A refusal for eligibility** is a `422` with its own additive code and the list
of keys at fault — never their text. The contract's `validation_failed` means a
malformed request and names one field, so it is not reused; the code and its
`keys` list are `1ir.11.1`'s to add, which the contract's rules allow without a
version bump. Whether a key is eligible is not a secret from the GM, who is the
only caller.

**Delivery follows RT-5, not the letter of L §4.10** ("delivered from the outbox
after the event commits"). The event rows commit in the slot's transaction, a
notification is a wake-up, and delivery reads slot state; "no event, no
delivery" holds because the state and the event are one commit. The plan's
send-time re-check under the share lock remains for what the assistant
*composes* per recipient (`1ir.11.2`); a document projection needs none, by
ED-25's invariant.

## 5. Truth table

One campaign. The GM owns it. **Ana** and **Ben** are participants with enrolled
devices; **Cy** is a participant who has not enrolled; anyone else holding the
table link is a **guest**. Ana is linked to the character sheet *Kira*.

The rows use two **fixture types**, which the oracle registers for its own use
(`1ir.1.12`) and `1kg.7.2`'s tests register in a test registry: `oracle_npc`,
whose revealable keys are `name`, `portrait`, `history`, `rumours`, `motives`
and `notes`, with `hidden_identity`, `tags` and `sources` not revealable; and
`oracle_statblock`, whose revealable keys are `ac`, `hp` and `attacks`. They
follow the bead's example rather than the shipped `npc` type, whose keys differ
(`voice`, `tell`, `attitude`, `wants`, `leverage` and so on); eligibility is
indifferent to what a key means.

*Ondrey* (`oracle_npc`) has `name` and `portrait` classed `public`, `history`
classed `campaign`, `rumours` classed `participants[Ana]`, `motives` classed
`gm_only` by the GM, `notes` still `unclassified`, and `hidden_identity` not
revealable and so `gm_only` by construction (ED-5). *Ondrey's stat block*
(`oracle_statblock`) has every cell classed `gm_only`. Kira's fields are classed
`characters[Kira]`, and the fields of Cy's sheet *Moss* `characters[Moss]`.

"v1" is Workbench v1, mask-only: no class binds a reveal, so a row that depends
on a class reads "allowed if ticked". "Enforced" is the campaign after M-2.

| # | Situation | Action | Workbench v1 | Enforced | Rule |
| --- | --- | --- | --- | --- | --- |
| TT-1 | A table session is live | Reveal Ondrey `{name, portrait}` to the table | allowed | allowed | ED-9, ED-10 |
| TT-2 | same | Reveal Ondrey `{name, history}` to the table | allowed if ticked | **refused, 422** naming `history`: it is `campaign`, and a guest can see the table. Nothing is shown, not even `name`. The sheet could not have ticked the row (M-4); this is the server's answer to a stale or forged request | ED-10, ED-9 |
| TT-3 | same | Reveal Ondrey `{motives}` to the table | allowed if ticked — the GM's explicit act (X-2) | refused, 422: `gm_only` is never displayable; the sheet's row is disabled with that reason | ED-10 |
| TT-4 | same | Reveal Ondrey `{notes}` to the table | allowed if ticked | refused, 422: `unclassified`. The sheet's row is disabled, says `Not classified yet` and links to the classification surface; the reveal sheet never classifies | ED-10, M-4 |
| TT-5 | same | Reveal Ondrey `{history}` to Ana's slot | refused, 422: an NPC is table-only (AUD-9) | refused by AUD-9 under the fallback; **allowed** if O-2 is accepted | ED-14 |
| TT-6 | O-2 accepted | Reveal Ondrey `{rumours}` to Ben's slot | — | refused, 422: `participants[Ana]` does not name Ben | ED-10 |
| TT-7 | same | Reveal Ondrey `{tags}` or `{sources}` anywhere | refused, 422: not on the revealable allowlist | the same; there is no eligibility row to consult | ED-5 |
| TT-8 | Ondrey `{name, portrait}` is live on the table | A guest reads the table slot | sees `name` and the portrait through a per-slot handle | the same | ED-19 |
| TT-9 | same | A guest's client asks for anything beyond the table slot | there is no request that names a participant: a guest's snapshot and stream hold the table slot only, exactly as if no participant slot existed, and an undeclared field is a generic 422 that does not depend on state | the same | threat model §8.2, SEC-15, REVEAL-24, ED-25 |
| TT-10 | No table session is live | Ana's enrolled device asks for anything | nothing: a device credential alone opens nothing | the same | AUD-5 |
| TT-11 | A session is live | Reveal Kira, every existing field, to Ana's slot | allowed; `all` is expanded to explicit keys before it is sent | allowed: `characters[Kira]` resolves to Ana | AUD-12, ED-8, ED-10 |
| TT-12 | same | Reveal Kira to Ben's slot | refused, 422: the sheet is not linked to Ben (AUD-9) | refused on both counts | ED-14, ED-10 |
| TT-13 | same | Reveal Kira `{name, class}` to the whole table | allowed (AUD-9's `Whole table`) | refused, 422, until the GM has classified those two keys `public` on the classification surface | ED-10, M-4 |
| TT-14 | Ben is enrolled, but opens the table link in a private window that has no device credential | that window reads the session | it is a guest: the table slot only, never Ben's slot | the same: entitlement follows the credential, whatever Ben himself is eligible for | AUD-5, ED-25 |
| TT-15 | Cy has a linked sheet *Moss* and no device | Reveal Moss to Cy's slot | confirms; the GM sees `Cy hasn't joined yet`; nothing is delivered, and it **never** falls back to the table | the same: eligibility is evaluated for the participant Cy, an identity, not for a device; `characters[Moss]` resolves to Cy | AUD-10, ED-10 |
| TT-16 | Kira is live in Ana's slot | The GM relinks Kira to Ben | the unlink stops the projection first, in one transaction; `stopped(character_unlinked)`; the epoch advances. Ben sees nothing | the same, and `characters[Kira]` now resolves to Ben — **eligible, not displayed** | AUD-15, ED-12, ED-13 |
| TT-17 | same, and the GM had a staged widening open in another tab | that tab presses Confirm | 409, never retried by itself; the sheet reloads and keeps the draft | the same | REVEAL-22, REVEAL-15 |
| TT-18 | Ondrey is live on the table | **Stop showing** | cleared at once; `stopped(gm_stop)`; the epoch advances | the same | ED-16, X-3 |
| TT-19 | a Confirm and a Stop cross on the wire; the Stop lands first | the Confirm arrives | 409; the table never shows the document | the same | REVEAL-22, AE-48 |
| TT-20 | Ondrey is live on the table | Reveal *Marsh Map* to the table | replaces Ondrey after the sheet says so; `stopped(replaced)` then `displayed` | the same | REVEAL-7, ED-15 |
| TT-21 | Kira is live in Ana's slot | Reveal Kira `{name, class}` to the table | *moves* it after the sheet says so; `stopped(moved)` then `displayed` | the same, if both keys are `public` | REVEAL-7, ED-15 |
| TT-22 | anything is live | The GM ends the session, or it expires | every slot cleared; `session_ended`; the link dies; the next session starts with nothing revealed. Effective without the campaign lock | the same | REVEAL-17, RQ-5 |
| TT-23 | anything is live | **Rotate link** | every table credential dies; every slot cleared; `stopped(link_rotated)`; the epoch advances — all at once, without the campaign lock; `authz_revision` follows in the second step. Enrolment credentials survive unless `Also reset personal links` was chosen | the same | REVEAL-17, RQ-5 |
| TT-24 | Kira is live in Ana's slot | The GM removes Ana | the projection stops, her device credential is revoked, the document is kept; `stopped(participant_removed)`; effective without the campaign lock | the same | AUD-16, RQ-5 |
| TT-25 | same | **Reset personal link** for Ana | the old device loses access at once and her slot is stopped; a new code is issued | the same | AUD-5, L §4.9 |
| TT-26 | same | Ana enrols a second device | the first device is replaced; the display is Ana's, so it continues on the new device; the GM sees `Ana joined from a new device` | the same under the fallback. If O-2 is accepted, the GM must approve the new device before anything flows to it (A-3): until then nothing is delivered | AUD-5 |
| TT-27 | Ondrey `{name, history}` is live to Ana (O-2 accepted) | The GM tightens `history` to `gm_only` | — | one transaction: the display is stopped whole, `stopped(eligibility_tightened)`, table-namespace rows go, `authz_revision` and the epoch advance | ED-12 |
| TT-28 | `notes` is `unclassified` | The GM classifies it `public` | — | nothing is displayed; it is now displayable | ED-13 |
| TT-29 | Ondrey `{history}` is live | An AI edit rewrites `history` | the table is unchanged; the indicator offers **Update…** with old text beside new | the same; `history` stays eligible, the new text is neither displayed nor approved | REVEAL-8, ED-6 |
| TT-30 | an AI edit copied a line of `motives` into `history` | The GM opens **Update…** | the diff shows the line; nothing reaches the table unless the GM confirms | the same; eligibility does not catch it — the pin and the GM's review do | ED-22 |
| TT-31 | the NPC type gains a field `secret_ally` in a later release | an old mask said `all` | never revealed: `all` was expanded when it was used, and was never stored | the same, and the field is `unclassified` | ED-8 |
| TT-32 | a group *Scouts* = {Ana, Ben} | Display Ondrey `{history}` to Scouts | not available: no group displays | not available under the fallback. If O-3 is accepted: one display record per member slot, one disclosure; each replaces that member's private view after a warning | ED-15 |
| TT-33 | O-3 accepted; the group display is live | **Stop showing** on Ondrey | — | every copy is cleared in one transaction | L §2.5 |
| TT-34 | same | Ben leaves Scouts | — | Ben's copy stops (`group_member_removed`); Ana's stays | L §4.9 |
| TT-35 | same | Cy joins Scouts | — | nothing reaches Cy until the GM displays again | ED-13 |
| TT-36 | Ondrey `{history}` was shown to Ana and then **retracted** | an automation rule proposes showing it again | — | refused for automation, for as long as the ledger row exists — the campaign's lifetime; the GM may still reveal it by hand | ED-16, ED-18 |
| TT-37 | Ondrey `{name}` was shown and the session ended | a later session starts | nothing is re-displayed; v1 has no automation | under the fallback, nothing; if O-4 is accepted, a GM-authored rule may *re-display* it, since its last event was `session_ended` | L rule 1 |
| TT-38 | a player-safe export of Ondrey | **Export → Player-safe** | the fields the GM ticks in the staged dialog, from the same builder as a table display | the ticked fields, each of which must be `public`; a `campaign` or narrower field cannot be ticked, because a file can be forwarded to anyone. The dialog never classifies | ED-21, EXPORT-3 |
| TT-39 | the GM quotes a rule to the table | Display a rules excerpt | not available: a slot holds documents only | not available under the fallback; if O-5 is accepted, a deterministic quote whose licence permits display to guests | section 7.4 |
| TT-40 | `session-notes` with a `recap` | Open the reveal sheet | the draft seeds empty and the row warns `Summarises your private GM thread` | the same, and `recap` is `unclassified` | REVEAL-23 |
| TT-41 | enforcement is being switched on for the campaign | a table session is live | — | the switch is a narrowing: every live display holding a key that is not eligible for its audience is stopped, `stopped(enforcement_enabled)`, and the epoch advances; a display whose keys were already classified and eligible stays | M-3 |
| TT-42 | "The Hooded Stranger" and "Ondrey" are both `public` names | anything | the link between them is not a revealable field | the same; it never reaches a table namespace | ED-20 |
| TT-43 | A table session is live | Reveal Ondrey `{hidden_identity}` anywhere | refused, 422: not on the revealable allowlist | the same: `gm_only` by construction; it has no row, is not counted as unclassified and cannot be classified | ED-5, ED-20 |
| TT-44 | same | Reveal Ondrey's stat block `{ac}` to the table | allowed if ticked | refused, 422: `gm_only` | ED-10 |
| TT-45 | O-2 accepted; a field of Ondrey classed `characters[Kira]` is live in Ana's slot | The GM relinks Kira to Ben | — | the relink is a narrowing for Ana: that display is stopped, `stopped(character_unlinked)`, the epoch advances; nothing is displayed to Ben | ED-12, ED-13 |
| TT-46 | O-2 accepted; a field classed `groups[Scouts]` is live in Ben's own slot | Ben leaves Scouts | — | Ben's display is stopped, `stopped(group_member_removed)`; the epoch advances | ED-12 |
| TT-47 | a guest is connected | The GM reveals privately to Ana, or classifies a field for Ana | the guest's complete frame transcript is identical with and without the private reveal | the same, and identical with and without the classification: slot reads are never gated on projection freshness | ED-25, REVEAL-24 |
| TT-48 | Ondrey `{name, history}` is live | The GM unticks `history` and presses **Update** | `history` leaves the slot at once; the audit row records the new mask | the same; the ledger gets `stopped(mask_narrowed)` for `history`, and nothing new for `name` unless its version changed | ED-17, REVEAL-6 |
| TT-49 | Enforced; no display automation is enabled | The GM switches enforcement off | — | a confirmed, audited GM action under the campaign lock: nothing is displayed or stopped, every class is kept, reveal is mask-only again. Refused while any display automation is enabled | M-8 |
| TT-50 | a later version of a type retires `rumours`, and a later one wants the name back for something else | — | the registry test refuses: a retired key is reserved | the same; meanwhile an orphan row is ignored by evaluation and removed when the document migrates | ED-24 |
| TT-51 | the assistant's Phase 2 has shipped, the campaign is **not** Enforced, and Ondrey `{notes}` is live on the table by a mask-only reveal | The GM classes `notes` `gm_only` on the classification surface | — (v1 has no classification surface) | nothing stops: until the campaign is Enforced a class binds the assistant's own paths only. The surface says `The table is seeing this now`, with a Stop beside it; switching enforcement on later stops it (TT-41) | ED-12, M-3 |

## 6. Mapping from the handoff

| Handoff | Here | Why |
| --- | --- | --- |
| `reveal_mask` on each document (wire sketch) | **dropped from the document.** A mask belongs to a live projection in a slot (REVEAL-7, REVEAL-13); the header and the badge derive from the server's live projection, and `data.revealed` is dropped too | a mask per document lets several documents be live at once and is three unconnected states (record §7) |
| `defaultReveal` on a type | **seeds the draft of the reveal sheet for that type's own audience only** (REVEAL-4, AUD-12), intersected with the fields that exist, are non-empty and are revealable. It is never an eligibility class; at most it becomes a `suggested` classification (ED-7) | a default is not a GM decision (X-2) |
| `defaultReveal: ['all']` on a character sheet | seeds the **owner** audience only, and is **empty** for the table audience | the handoff wrote it for the sheet's owner, not for the room |
| `all` in a mask | **never stored**: expanded by the client into the keys that exist now (REVEAL-9); the same for bulk classification (ED-8) | a stored wildcard reveals the field added next month |
| `audience: 'owner'` on a type | the registry flag that lets a type use a participant slot (AUD-9, ED-14); proof of "owner" is the enrolled device credential (AUD-3 to AUD-5) | a shared link cannot prove who holds it |
| un-reveal "is one tap and takes effect immediately" | **Stop showing**: one action, unconfirmed, unqueued, unrefusable, separate from every other control (X-3, REVEAL-6) | kept, and made to win races (REVEAL-22) |

## 7. The four amendments the plan requests

Each takes the plan's **fallback** now, because none is needed before the
assistant's Phase 4 and each changes what the product is. None is rejected.
Nothing is in place for any of them yet: the third column is what this record
requires of an open bead *now* so that accepting the amendment later is cheaper,
and the last column is what the bead that needs it must bring.

| # | Request (L §2.5) | In force | What this record requires now | What a later amendment must bring |
| --- | --- | --- | --- | --- |
| 7.1 | Participant-slot displays for **any** document type (against AUD-9, NG-22) | **Fallback**: a participant slot only for a linked character sheet; every other type is table-only, `public` fields only | ED-14: `1kg.7.1` and `1kg.7.2` keep the restriction a service rule; slots, masks and eligibility stay type-agnostic | an amendment to AUD-9 with the audience picker's behaviour for other types; and threat-model amendment A-3 — **GM approval of each new device before owner-only content flows** — which is a schema change (device rows), a wire change (the enrolment response is the closed pair `enrolled` / `inactive` today) and **re-approval of every device enrolled before the amendment**, because grandfathering them keeps the interception residue exactly when it starts to matter |
| 7.2 | **Group displays** (against the one-slot invariant, REVEAL-6/7/22, NG-20) | **Fallback**: none; the GM displays to one participant at a time, moving the document (REVEAL-7) | ED-15: `1kg.7.1` expresses the invariant as a partial unique index, and a nullable disclosure reference would be additive | an amendment to §7.1 and REVEAL-7 stating that Stop on the document or the disclosure clears every copy in one transaction, that a member's removal is a narrowing, and that an addition displays nothing (TT-33 to TT-35); and the index is dropped behind a flag that stays off until no rollback to a one-row build is intended, because the indicator, Stop-by-document and REVEAL-7's move all assume one live row |
| 7.3 | A GM-authored, enabled rule as the **X-2 action for re-displays** | **Fallback**: rules only *propose*; every display needs one-tap approval | ED-17: `1ir.2.8` records a reason with every `stopped` | an amendment to X-2 that names what a rule must name — documents or excerpts, fields, audience, trigger; that it never performs a *first* disclosure; that a Stop disables the rule's re-display of that subject until the GM re-arms it; and which reasons, if any, stop blocking a re-display — until then every `stopped` blocks (ED-17) |
| 7.4 | A **reference excerpt** slot content kind for rules text (against the §7.1 slot model, REVEAL-5/9, NG-25) | **Fallback**: no rules content on player devices; the GM reads rules aloud | `1kg.1.6` gives a slot's content a `content_kind` discriminator whose only v1 member is `document`. Adding a member later is still a contract version bump, by the contract's own rule; what the reservation buys is that a v1 table client meets an unknown kind as a neutral placeholder and not as a parse failure | section 7.4 below |

### 7.4 Rules excerpts, if they ever ship

- **Representation.** A slot holds either a document projection or, under this
  amendment, a *reference excerpt*: `(corpus_chunk_id, corpus_version)`, the
  exact character span, and the citation. The ledger's subject is the same pair
  (ED-17). There is no field mask, because there are no fields. The amendment
  must also say how the quoted text stays fixed while it is displayed:
  re-ingestion does not promise immutable chunk rows today.
- **Deterministic quotes only.** The text on a player's device is a verbatim
  span of a corpus chunk, produced by lookup and never by a model. A paraphrase
  or a summary is assistant output and is GM-only.
- **Whose entitlement governs.** The GM's entitlement to read a source governs
  what the *GM* sees. Showing it to a guest looks like redistribution to someone
  with no entitlement at all — a **working assumption** for the licensing review
  (`agent-forge-harness-yje.6.1`), not a legal conclusion — so the text's **own
  licence** would have to permit display to the public: openly licensed text
  (the SRD under CC BY, CC BY-SA wiki text) with its attribution shown, and
  nothing else. The corpus records a book and a source type per chunk today, not
  a licence, so the amendment must bring per-source licence metadata. Until the
  review is made the fallback stands.
- **First display by approval; re-displays per 7.3.**

## 8. Mask-only first, and the migration plan

| ID | Step | Basis |
| --- | --- | --- |
| M-1 | **Workbench v1 builds reveal exactly as its record says** (sections 7 and 8, threat model SEC-13 to SEC-19), with no eligibility table and no eligibility check, and with what section 9 requires from day one: the campaign authorisation row, its helper and its lock order; `authz_revision`; two-step narrowings and a wired job runner (RQ-5, RQ-12); content-free audit rows (ED-18); and a Confirm whose preconditions are a named, ordered list (ED-9). | ED-11 |
| M-2 | **Enforcement arrives with `1ir.11.1` (Phase 4), per campaign, by its own GM action** — never silently by a deploy, and **not** as a side effect of the capture policy's `enabled` (L §3.1), which is a reversible toggle about listening. The flag lives on the campaign's `authz_state` row, so the transaction that locks the row has read it. The classification surface exists from Phase 2 (`1ir.2.7`), so a GM can classify before switching on. **"Display automation" is every display written other than by the reveal sheet's Confirm — live-card shares, the reveal hand-off, rule re-displays, recap publication — and it requires the campaign to be Enforced: each such write reads the flag from the locked row and refuses when it is off**, so switching off can never race a feature being switched on. Until then a campaign behaves as v1. | L + I · **E** (O-7) |
| M-3 | **Switching it on is a narrowing**, in the two steps of every fact-changing narrowing (RQ-5): first, under the session row, ED-12's scan stops every live display holding a key that is not eligible for its audience, with `stopped(enforcement_enabled)`, and advances the epoch; then, under the campaign lock, the flag is set and the scan runs again. The GM is told first what will stop; the simplest path is to switch it on between sessions. | X-3 + I |
| M-4 | **The reveal sheet never classifies.** When Enforced, each row shows its class; a row that is not eligible for the chosen audience cannot be ticked, says why — `GM only`, `Not classified yet`, `Players only — guests can see the table` — and links to the classification surface, where widening is the separate, explicit action L rule 4 asks for. A reveal group (REVEAL-11) whose keys differ in eligibility for the chosen audience is shown as its member rows. A class that narrows between the sheet's drawing and its Confirm advances the epoch (ED-12), so the Confirm is a 409. | L rule 4 + I · **E** (O-6) |
| M-5 | **Nothing is backfilled from history.** Having been revealed once does not make a field `public`: v1 has no durable grants (L rule 3), and a one-off reveal to five friends is not a decision that strangers with the link may see it. Every revealable field starts `unclassified`. | L + I |
| M-6 | **A type's `defaultReveal` may be offered as suggestions** (`classification_source: suggested`) when the GM first opens classification for a document; none is applied without the GM's action (ED-7). | L |
| M-7 | **The policy oracle (`1ir.1.12`) takes section 5 and the race cases of section 9 as its first cases.** The v1 column is a regression suite for `1kg.7.2`; the Enforced column is the acceptance suite for the decision point (`1ir.2.2`) and for enforcement (`1ir.11.1`). | I |
| M-8 | **Switching enforcement off is allowed only while no display automation is enabled for the campaign.** It is a confirmed GM action, a locked widening (RQ-4), audited, and it advances `authz_revision`; it displays nothing, stops nothing and keeps every class, so switching on again restores them. Every reveal is then, as in v1, an explicit Confirm that names its fields. | I · **E** (O-7) |

## 9. Locks, revisions and the epoch

| ID | Requirement | On |
| --- | --- | --- |
| RQ-1 | **`1kg.2.1`'s migration creates the `campaign` schema — the name the plan already uses — and in it `authz_state (campaign_id PRIMARY KEY, a foreign key to the campaign row with ON DELETE CASCADE; authz_revision BIGINT NOT NULL DEFAULT 0; lock_token SMALLINT NOT NULL DEFAULT 0)`, with an `AFTER INSERT` trigger on the campaign table that inserts the row**, so no route, script or later epic can create a campaign without one. `lock_token` is never written: it exists so that `1ir.1.13` can grant a display role `UPDATE (lock_token)` — the privilege a row lock needs — and nothing else. `1ir.2.1` adds `projection_revision` and the queue, and `1ir.11.1` the enforcement flag, by additive migrations. Both epics lock this one row. | `1kg.2.1` |
| RQ-2 | **One mechanism, one helper.** The campaign lock is a **row lock on `campaign.authz_state`**, `FOR SHARE` or `FOR UPDATE`, as L §4.3 has it. No advisory lock stands in for it: the existing `UnitOfWork.lock` is an exclusive advisory lock and is not this, and two code paths using two mechanisms would not exclude each other at all. `1kg.2.1` adds `lock_campaign(campaign_id, *, shared)` to the unit of work in `service/db.py`, with an in-memory twin that records the campaign and the mode. The helper (a) is the first lock its transaction takes — **lock order is proven by the database tests of section 9.1**, because row locks are repository SQL that the in-memory twin cannot see; (b) sets `lock_timeout` and `transaction_timeout` for the transaction (RQ-8) with `set_config(..., true)`, the second a parameter, so that a long caller — a campaign deletion, the enforcement scan, a type migration — chunks its work or passes its own bound instead of dying at five seconds with every display held out; (c) **raises a named error if the row is missing** — fail closed, never proceed unlocked: a route maps it to the generic 404, since the campaign was deleted a moment ago, and a job completes as a no-op; (d) relies on READ COMMITTED, which `Database.transaction()` sets explicitly for every transaction it opens, so that every check made after the lock reads what the last holder committed. Under REPEATABLE READ a waiter is safe only because every exclusive holder updates the row, and no caller may rely on that. | `1kg.2.1` |
| RQ-3 | **One lock order, everywhere: the campaign's `authz_state` row; then participant rows — in ascending id when there are several — and character-link and document rows, if the transaction must lock any; then the table session row; then slot rows; and an outbox enqueue comes last, after every row lock.** A transaction may skip levels and never goes back up. **Every explicit row lock below `authz_state` is `FOR NO KEY UPDATE`, never `FOR UPDATE`.** Inserting a row that has a foreign key takes `FOR KEY SHARE` on the row it references, and that conflicts with `FOR UPDATE` alone: with `FOR UPDATE`, a display writing a participant's slot could deadlock with a Remove or a Reset of that participant, and a join or a connection admission writing a child of the session row would make a Stop wait. For the same reason a participant is marked removed, never `DELETE`d, inside these transactions. **A deadlock victim (`40P01`) is retried by the server exactly as a lock timeout is.** No transaction takes two campaigns' locks: work that spans two campaigns — ending a session in one to start a session in another — is two transactions, the End first. A Confirm locks no document or participant row: it reads them under the share lock, and archive, unlink and removal are covered by the campaign lock and the epoch. Connection admission's per-session advisory lock (RT-8) is taken alone: nothing that holds a lock in this order asks for it, and admission asks for none of these. | `1kg.2.2`, `1kg.2.3`, `1kg.5.2`, `1kg.7.2`, `1kg.7.5`, `1ir.2.x` |
| RQ-4 | **Display writes take the campaign lock in share mode, then check** — Reveal, Update, replace and move, and later Share, the reveal hand-off, rule re-displays and recap publication. **Every display write, the assistant's included, carries the session and the epoch it was composed under**: with two-step revocations it is the epoch, not the campaign lock, that beats a Rotate. **Whatever changes a fact that a display's preconditions read takes the lock exclusively, first, and advances `authz_revision` (RQ-10)**: the locked widenings — participant add, a link towards a participant, session start and, when they exist, a class that widens, version approval, a group addition and switching enforcement off — in one transaction; and the second step of every fact-changing narrowing (RQ-5) — unlink and relink, document archive and delete, campaign archive and deletion and, when they exist, a removal from a group, a tightened class and switching enforcement on. A display therefore either commits before the change, which sees it and stops it, or runs after it and is refused (L §4.3). These may wait, within RQ-8's bound; what never waits is a Stop and the first step of a narrowing. | `1kg.2.2`, `1kg.2.3`, `1kg.5.2`, `1kg.2.6`, `1kg.7.2`, `1ir.2.x`, `1ir.11.1` |
| RQ-5 | **Every narrowing has a first step that never waits for the campaign lock and is never refused; what differs is the second.** *Step 1* locks the participant row the change concerns, if any, and the session row if one is live — never `authz_state`. A **revocation** — End, expiry, Rotate, Remove participant, Reset personal link — makes its change true there for every reader at once (ED-25: the session ended, the link generation advanced, the credential and the codes revoked, the participant marked removed — SEC-9, SEC-11). Every narrowing then stops the displays it makes invalid, **advances the reveal epoch, on an empty slot too** — it is the epoch, not a slot clear, that refuses a Confirm validated a moment earlier (REVEAL-22) — writes its audit rows and commits. *Step 2* runs under the campaign lock, exclusively. For a **fact-changing narrowing** — unlink and relink, archive and delete, a removal from a group, a tightened class, switching enforcement on — it runs **in the request**: change the fact, scan again, advance `authz_revision`. If it cannot get the lock in time the answer is retryable and says the change is *not applied yet*: the displays are already stopped, the GM's surface keeps showing the old fact, the client retries, and **a GM's intent is never replayed from a job** (RQ-8). For a **revocation** step 2 is a **reconciliation job**, enqueued in step 1's transaction with no dedupe key (RQ-12), started after the response has been sent and retried by the job runner until done: it clears every live slot whose session is not live or whose participant is not active (RC-6), runs the table-namespace narrowing when there is one (L §4.3 rule 4), and advances `authz_revision`. It reads current state and never replays a payload, so running twice is harmless, a later run covers an earlier one that failed, and it can never clear a reveal made after the change that queued it. **An enrolment or a device replacement** — a player's unauthenticated route — looks its code up before any lock, so that a bad code takes none; then locks the **participant row**, the row that Remove and Reset lock, and **consumes the code with a conditional write under that lock**, failing generically if no row changed (RC-13); it stops nothing, advances no epoch and leaves a reconciliation job. X-3, SEC-35 and A-9 therefore stand unchanged: an End and a Rotate are never queued behind a projector and never refused by a lock timeout, and neither is the remedy for an intercepted personal link. This is the one place this record departs from the letter of L §4.9's "same transaction": the revocation and its slot clears are one transaction, and the revision follows it. | `1kg.2.2`, `1kg.2.3`, `1kg.5.2`, `1ir.2.1`, `1ir.11.1` |
| RQ-6 | **A Stop takes the table session row and slot rows, and nothing else** — never `authz_state` — so it never waits for a projector or for the second step of any change. **What it can wait for is said plainly.** (a) The session row, held by one other reveal-path transaction at a time: so every transaction holds the session row **only for its write** — validation, projection building, scans of other tables and table-namespace rewrites happen before it is taken (ED-9); what happens under it is an epoch check, slot writes, an epoch advance, event rows and the commit — and every holder is bounded by `transaction_timeout`, which the session-row primitive of RQ-7 sets as `lock_campaign` does. A Stop sets no `lock_timeout` of its own: better to wait a bounded moment than to fail. (b) A database connection: a request blocked on the campaign lock holds one of its instance's gate slots (`DB_POOL_MAX`, 4), so RQ-8 keeps that wait well under the gate's 5 s, and `1kg.7.2` must show that a Stop is served while every other slot is held by a lock waiter — by that bound, or by a slot reserved for narrowings, with the connection budget of `docs/migrations.md` re-proved if it changes. Its row lock is `FOR NO KEY UPDATE` (RQ-3), so joins and connection admission never make it wait. A Stop and any other narrowing that race are both narrowings and are idempotent. | `1kg.7.2` |
| RQ-7 | **Every narrowing advances the session's reveal epoch, on an empty slot too** (REVEAL-22) — every shrink of ED-12 and the enforcement switch (M-3) included. **`1kg.2.1` puts `reveal_epoch` and `audio_epoch` on the session row and owns the session-level primitive, `narrow`**: it locks the session row, sets `transaction_timeout`, advances the epoch inside the caller's transaction and calls a slot-clearing extension point that is empty until `1kg.7.1` fills it with the slot clear — so every bead that ships before `1kg.7.1` calls something that exists, and nobody has to retrofit it. Session start first ends any expired session of that GM through the same primitive, so that no slot of a dead session survives under ED-15's index. Its callers are `1kg.2.2` (unlink, remove, reset), `1kg.2.3` (start, End, expiry, Rotate), `1kg.5.2` (archive, delete), `1kg.2.6` (campaign deletion), `1kg.7.2` (Stop, replace, move) and, when they exist, `1ir.2.1` and `1ir.2.7` (groups and classification) and `1ir.11.1` (the switch and the scan). The resolver (`1ir.2.2`) mutates nothing and calls nothing. | `1kg.2.1`, `1kg.7.1` and the callers |
| RQ-8 | **Lock waits and lock holds are bounded.** No network or model call is made under `authz_state` (L §4.3 rule 7). The helper sets `lock_timeout` (*suggested* 2 s, always under the gate's acquire timeout) and **`transaction_timeout`** (*suggested* 5 s; PostgreSQL 17) — a bound on the *transaction*, which `statement_timeout` is not. It ends the session, which per-operation connections tolerate, and a database test must prove that it arms when set inside the transaction. **The first step of a narrowing asks for no lock that can time it out. Its second step, when it changes a fact, is retried within the request and then answered as *not applied yet*, retryably and honestly (RQ-5); it is never handed to a job and never reported as done.** A widening that times out is a retryable `503`. The projector (`1ir.2.3`) holds the lock in **short slices** — a bounded number of queue items per transaction rather than every pending item, setting `projection_revision` only in the slice that finds the queue empty — and gives way between slices. PostgreSQL does not promise an exclusive waiter a turn ahead of later share-mode lockers of a row, so nothing here relies on fairness: share-mode traffic is one GM's displays, each bounded by `transaction_timeout`, and `1ir.11.2`'s send-time re-checks must not become a steady stream (RC-8). | `1kg.2.1`, `1ir.2.3`, `1ir.11.2` |
| RQ-9 | **The reveal Confirm is one transaction whose preconditions are a named, ordered list** in SEC-3's order (ED-9), so that "every masked key is eligible" is one more entry when Enforced, not a redesign. | `1kg.7.2` |
| RQ-10 | **One function advances `authz_revision`, and `1kg.2.1` owns it**, beside `lock_campaign`: it requires the calling transaction to hold the lock exclusively and is the only code that writes the column. The Workbench ships first, so the Workbench ships the helper; `1ir.2.1` *extends* it with the plan's rule 3 (`projection_revision`) rather than writing a second one. During that rollout the previous release's helper still advances `authz_revision` without rule 3 (`docs/migrations.md`: every migration must work with the previous release's code), so the projector must treat "revision ahead, queue empty" as work to do, not as a state that fails player reads closed until the next widening. These mutations advance it: participant add and remove; device enrolment, replacement and reset; character link and unlink; session start, end and expiry; link rotation; document archive and delete; campaign archive; and — when they exist — eligibility, version approval, group membership and the enforcement switch. Renaming a participant does not. | `1kg.2.1`, `1ir.2.1` |
| RQ-11 | **`authz_revision` is a freshness signal for projections, never the only evidence of a revocation.** Every table read and every send authenticates against the session's state, the link generation, the credential and the participant's status directly (ED-25, RT-7; `1ir.2.2`'s per-request re-read), so the interval between a revocation and its revision (RQ-5) opens nothing. | `1kg.2.3`, `1kg.7.5`, `1ir.2.2`, `1ir.2.5`, `1ir.11.2` |
| RQ-12 | **The job runner is wired before the first two-step change ships** (`agent-forge-harness-1kg.2.7`). Today nothing drives the outbox of `1kg.1.5` except one attempt after the commit: the hook on ordinary requests and the authenticated `/internal/jobs` of RT-15 were left to later beads, and until they exist "retried until done" is not true. That bead wires both, lets a handler be registered by kind with no attempt limit, and makes an absorbed enqueue lock the row it is absorbed into. The reconciliation job still carries **no dedupe key**: two are harmless, and one swallowed by a job that finished before its transaction committed is not. A failed reconciliation alerts; the next one covers it. | `1kg.2.7`, `1kg.2.2`, `1kg.2.3` |

### 9.1 Race cases

Each is a test for the bead named, and a case for the oracle where it can be
modelled (M-7).

| # | Race | Outcome | Why | Test on |
| --- | --- | --- | --- | --- |
| RC-1 | A Confirm and a Stop, in either order | Stop first: the Confirm is a 409. Confirm first: the Stop clears it | the session row and the epoch (REVEAL-22, AE-48, AE-49) | `1kg.7.2` |
| RC-2 | A Confirm and a fact-changing narrowing — unlink, tighten, group removal, archive — in either order, the narrowing's two steps included | a Confirm staged before the narrowing's first step is a 409; one that slips in between its steps is stopped by the second step's scan; one that comes after is refused | share against exclusive on one row, under READ COMMITTED (RQ-2, RQ-4) | `1kg.7.2`, `1ir.11.1` |
| RC-3 | A Confirm and the enforcement switch | as RC-2: the flag is read from the locked row | M-2, M-3 | `1ir.11.1` |
| RC-4 | Two Confirms | the second to take the session row is a 409 | every display advances the epoch | `1kg.7.2` |
| RC-5 | A Confirm and an End or a Rotate | End or Rotate first: a 409 — no live session, or a stale epoch. Confirm first: its slot is cleared | the session row (RQ-5) | `1kg.7.2` |
| RC-6 | Remove participant while **no** session is live, racing a session start and a Confirm to that participant | the Confirm may commit to a slot that nobody can read once the removal has committed — the credential is revoked or, for a participant with no device, the codes are (RC-13) — and the removal's reconciliation clears it | RQ-5 | `1kg.7.2` |
| RC-7 | A Stop while a fact-changing narrowing is in flight | the Stop waits at most for that transaction's session-row write — never for its scans or table-namespace rewrites, never for `authz_state` | RQ-6 | `1kg.7.2`, `1ir.11.1` |
| RC-8 | An End, a Rotate or a removal while the projector holds the lock, or under a stream of share-mode holders | effective at once: the first step never asks for the lock; the revision follows within a slice | RQ-5, RQ-8 | `1kg.2.3`, `1ir.2.3`, `1ir.11.2` |
| RC-9 | A Stop while every other gate slot of the instance is held by a lock waiter | served within RQ-8's bound | RQ-6 | `1kg.7.2` |
| RC-10 | The projector and a Stop | no lock in common; neither waits | RQ-6 | `1ir.2.3` |
| RC-11 | Two transactions that take the campaign lock exclusively | serialised by the lock; the second reads what the first committed | RQ-4 | `1kg.2.2` |
| RC-12 | An enrolment — a player's unauthenticated route — and anything else | the code is verified before any lock; the handler locks one participant row and never `authz_state` | RQ-5 | `1kg.2.2` |
| RC-13 | A Reset or a Remove against an enrolment that holds a still-valid code, in either order | once the Reset or the Remove has committed, no credential made from an older code is valid: all three lock the participant row, and the code is consumed by a conditional write under it | RQ-5 | `1kg.2.2` |
| RC-14 | A display to Ana's slot against Remove Ana or Reset Ana, in either order, on a real database | no deadlock: every explicit row lock is `FOR NO KEY UPDATE`, which a foreign-key check does not conflict with; the victim of any other deadlock is retried | RQ-3 | `1kg.7.2` |
| RC-15 | A tightening, an unlink or an archive that cannot get the campaign lock in time | the displays are already stopped and the epoch has advanced; the fact is unchanged; the GM is told it is not applied yet, and the client retries | RQ-5, RQ-8 | `1kg.2.2`, `1kg.5.2`, `1ir.11.1` |

## 10. What downstream beads must honour

| Bead | Requirement |
| --- | --- |
| `1kg.5.3` | ED-2, ED-3, ED-5, ED-20, ED-23, ED-24: flat keys; a secret that needs its own eligibility is its own field; the revealable allowlist is also the classifiable list, and everything off it is `gm_only` by construction; identity links are not revealable; no list field carries names of different visibility; a retired key is reserved, and a registry test pins the list |
| `1kg.2.1` | RQ-1, RQ-2, RQ-3, RQ-7, RQ-10: the `campaign` schema; `authz_state` with its trigger and its lock-only column; `lock_campaign` and the revision helper in `service/db.py`, with in-memory twins; `Database.transaction()` opens READ COMMITTED explicitly; `reveal_epoch` and `audio_epoch` on the session row, and the `narrow` primitive with its empty slot-clearing extension point; every explicit row lock `FOR NO KEY UPDATE`, participants marked removed and never deleted; ED-18(a): the audit table, unless `1ir.2.6` merged first; participants, credentials, codes and sessions shaped so that a narrowing's first step needs no lock on `authz_state` |
| `1kg.2.7` | RQ-12: the hook on ordinary requests and the authenticated `/internal/jobs` (RT-15); handlers registered by kind with no attempt limit; an absorbed enqueue locks the row it is absorbed into. It blocks `1kg.2.2` and `1kg.2.3` |
| `1kg.2.2` | RQ-3, RQ-4, RQ-5, RQ-7, RQ-10, RQ-12. Participant add and a link towards a participant are locked widenings. Unlink and relink are fact-changing narrowings: the displays are stopped first under the session row, the fact is changed in the request under the lock, and the answer is *not applied yet* when the lock cannot be had. Remove participant and Reset personal link are revocations: effective at once, never asking for the lock, leaving a reconciliation job with no dedupe key. An enrolment looks its code up before any lock and consumes it with a conditional write under the participant row's lock. Every narrowing calls `narrow`. RC-11, RC-13 and RC-15 as tests |
| `1kg.2.3` | RQ-3, RQ-4, RQ-5, RQ-7, RQ-10, RQ-11, RQ-12. Session start is a locked widening, and first ends any expired session of that GM. End, expiry and Rotate are revocations: effective under the session row alone, advancing the link generation and both epochs there (SEC-9), never asking for the lock, leaving a reconciliation job. RC-8 as a test |
| `1kg.2.6` | campaign archive and deletion are fact-changing narrowings: stop what is live first, then archive or delete under the lock (SEC-36, RQ-5); `authz_state` goes with the campaign, and a reconciliation that finds it gone completes as a no-op (RQ-2) |
| `1kg.5.1` | ED-6: eligibility and masks name a field key of a document; versions are immutable and sealed, and nothing about visibility is stored on a version or a document row |
| `1kg.5.2` | archive and delete of a document are fact-changing narrowings: `narrow` first (LIB-17), then the change under the lock (RQ-4, RQ-5, RQ-7); RC-15 as a test; the step that migrates a document to a newer type version follows ED-24 |
| `1kg.1.6` | The reveal family of the wire contract: masks as explicit key lists; audiences `table` and `participant:<id>`; **a Confirm names its session as well as the epoch** (ED-9); a slot content `content_kind` whose only member is `document` (7.4); a 422 for a mask that lists the keys at fault as `keys`, never text; no eligibility fields in v1 — the eligibility refusal is an additive code later (section 4) |
| `1kg.7.1` | ED-15 (the partial unique index); ED-18(a) (the audit rows of a reveal, exactly those fields); ED-26 (no hash); RQ-7: it **fills `narrow`'s slot-clearing extension point**, so that every narrowing already shipped clears slots from that moment, and proves it for each of them |
| `1kg.7.2` | ED-9 and RQ-9 (ordered preconditions; the courtesy 409; validation and projection building before the session row); ED-12; ED-16; ED-25; RQ-3 (`FOR NO KEY UPDATE`; deadlock victims retried); RQ-4 (share mode; the session and the epoch on every display write); RQ-5 and RQ-10 (what the narrowings it races against do, and what advances `authz_revision`); RQ-6 (Stop); RQ-7; RQ-8; section 5's v1 column and RC-1, RC-2, RC-4, RC-5, RC-6, RC-7, RC-9 and RC-14 as tests |
| `1kg.7.3` | REVEAL-4's seeding unchanged; in v1 no class is shown and nothing classifies (M-4); a mask or audience 422 that answers a Confirm is handled as a 409 is — reload, keep the draft (ED-9) |
| `1kg.7.5` | ED-25: slot frames are served from slot state and are never gated on a projection revision; the slot isolation transcript test is unchanged by any classification (TT-47); RT-8's admission lock is taken alone (RQ-3) |
| `1ir.2.1` | ED-4, ED-7, ED-8, ED-24; `field_path` is the flat key of ED-2; RQ-10: it *extends* the Workbench's helper with rule 3 and tolerates the previous release's helper; RQ-5: a revocation advances the revision in its reconciliation, not in its own transaction — a departure from this bead's "same transaction" for participant and device-credential mutations |
| `1ir.2.2` | section 4's two functions — `eligible_for_audience` on identities, `entitled` on credentials and an active participant (ED-10, ED-25); the per-request re-read of session, generation, credential and participant status (RQ-11); section 5's Enforced column as acceptance |
| `1ir.2.3` | RQ-8: the projector holds the lock in short slices and gives way; ED-6: only approved versions enter the table namespace; RC-8, RC-10 as tests |
| `1ir.2.5` | ED-25: the freshness gate covers table-namespace reads and what is composed from them, never Workbench slot snapshots or frames; a widening for one audience must not produce an observable gap for another (TT-47) |
| `1ir.2.6` | ED-18(a): one audit table, shared with the Workbench, whichever epic creates it, with a closed per-action `detail` of ids, codes and keys and — if the assistant wants one — a nullable `payload_hash` that Workbench rows leave empty; ED-26 |
| `1ir.2.7` | The classification surface (ED-13, M-4, M-6): the only place a class is set; keys off the revealable allowlist are not counted as unclassified (ED-5); any change that removes a principal — a guest included — is a narrowing (ED-12); before the campaign is Enforced a class change stops no Workbench display, and the surface says `The table is seeing this now` with a Stop beside it (TT-51) |
| `1ir.2.8` | ED-17, ED-18(b), ED-26: per-field events with reasons, `exported` included; `session_ended` only for fields live at the end; a field added by an Update gets `displayed`; no backfill; keyed or reference-only hashes |
| `1ir.5.1` | ED-23: aliases are a second subject kind in the one model |
| `1ir.11.1` | The enforcement release: ED-9's fourth precondition, ED-10, ED-12's scan, M-2, M-3 (two steps), M-4, M-8, the enforcement flag on `authz_state`, **read from the locked row by every assistant display write**, the additive refusal code, the sheet's class display (with a follow-up to `1kg.7.3`'s component), RC-2, RC-3, RC-7, RC-15; section 5's Enforced column as acceptance |
| `1ir.11.2` | RQ-4: every display write carries the session and the epoch it was composed under; RQ-8: send-time re-checks are short and must not form a steady stream of share-mode holders (RC-8); RQ-11 |
| `1ir.1.12` | M-7 |
| `1ir.1.13` | RQ-1: a display role needs `UPDATE (lock_token)` on `authz_state` and nothing else there — and, **with forced row-level security, an `UPDATE` policy as well as a `SELECT` policy on that table**, because `FOR SHARE` returns only rows that pass both, and the helper would otherwise fail closed on every reveal; an authorisation role `UPDATE` on the row; a Stop, the first step of a narrowing and the enrolment handler need no right on it; the reconciliation job runs as the authorisation role; the trigger that creates the row must work under whatever role creates campaigns |

## 11. Responses to the Workbench decisions the bead names

| Workbench decision | Response |
| --- | --- |
| AUD-1, **E-6** (one GM; players have no accounts) | Adopted. Principals are the GM, participants and guests. If accounts arrive they bind to participants and no class changes |
| AUD-2, AUD-8 (audiences `table` and `participant:<id>`; one slot each) | Adopted as the display layer |
| AUD-3 to AUD-5, **E-1** (owner proof by device enrolment) | Adopted. *Entitlement* to a participant slot needs a live session *and* the enrolled credential (ED-25); *eligibility* is evaluated for the participant as an identity (ED-10). If E-1 were dropped, no read could be entitled to a participant slot, and only the table slot — `public` fields — could ever be delivered |
| AUD-6, AUD-7, **E-10** (one table link; guests anonymous) | Adopted. A guest is entitled to the table slot and to nothing else; a ledger's recipients snapshot records guests as a count, never as identities |
| AUD-9 | Kept for v1 (7.1, O-2) |
| AUD-10 (never falls back to the table) | Adopted (TT-15, ED-10, ED-13) |
| AUD-11 (aliases) | Adopted: a recipients snapshot holds participant ids, never aliases; entity aliases are ED-23's |
| AUD-12 | Adopted (section 6) |
| REVEAL-7, **E-2** (one live document per slot, no player-side history) | Adopted; group displays deferred (7.2, O-3). The ledger is the GM's record, never a player's shelf |
| REVEAL-8, **E-3** (pinned versions) | Adopted and extended to approved versions (ED-6, TT-29, TT-30). If E-3 changed to "follow the current version", ED-22's gap would become a live leak path, and eligibility would have to be re-checked on every save |
| REVEAL-9, REVEAL-10 | Adopted and extended to eligibility (ED-5, ED-8) |
| REVEAL-21 | Adopted (ED-19, ED-21) |
| REVEAL-24 (no inference) | Adopted: eligibility rows, classes and revisions never reach a table client; a refusal for eligibility is only ever sent to the GM; and no classification pauses or re-snapshots a slot (ED-25, TT-47). What the assistant composes per recipient is `1ir.2.5`'s to keep equally silent |
| X-2 | Adopted; a display never widens eligibility (ED-13, M-4); re-display rules deferred (7.3, O-4) |
| X-3 | Adopted and extended: tightening eligibility is a narrowing (ED-12) and switching enforcement on is a narrowing (M-3). Kept whole for the controls X-3, SEC-35 and A-9 name: a Stop, an End and a Rotate never ask for the authorisation lock (RQ-5, RQ-6) — and neither does the first step of *any* narrowing, which is what takes content off a player's screen. What a Stop can still wait for — one short, bounded session-row write, and a connection — is stated in RQ-6; a change of fact that cannot get the lock is answered honestly as not applied yet (RQ-8) |
| X-4 | Adopted: nothing here adds anything a table client keeps |
| X-7 | Adopted: audit rows hold ids, codes and keys and no hash; a ledger hash is of references or keyed per campaign (ED-17, ED-26) |
| X-10 | Adopted: an excerpt (7.4) is text; it loads nothing |

## 12. Decisions that await the owner's confirmation

Each changes what the product *is*. Each has a default in force so that work is
not blocked; none has been accepted on the owner's behalf (section 13). They are
tracked in `agent-forge-harness-1kg.1.7`.

| ID | Default in force | Alternative | Dear to change after |
| --- | --- | --- | --- |
| O-1 | **Workbench v1 ships reveal mask-only**; eligibility binds reveal per campaign from the enforcement release (ED-11, section 8) | Enforce eligibility from the first Workbench release: `1kg.7.2` then waits for `1ir.2.1`, `1ir.2.2` and a classification surface | `1kg.7.2` |
| O-2 | **Participant slots stay character-sheet-only** (7.1) | Accept the plan's amendment now, with device approval (A-3) | `1kg.7.3`, the assistant's Phase 4 |
| O-3 | **No group displays** (7.2) | Accept the amendment now | `1kg.7.1` — cheap either way, thanks to ED-15 |
| O-4 | **Rules only propose; every display needs approval** (7.3) | A GM-authored rule is the X-2 action for re-displays | the assistant's Phase 5 |
| O-5 | **No rules text on player devices** (7.4) | Reference excerpts, after the licensing review | the assistant's Phase 5; `yje.6.1` |
| O-6 | **The reveal sheet never classifies**; classification is its own action on its own surface (M-4) | Classify inside the Confirm. It must then bring: an explicit `classify` list in the Confirm — key, the class the sheet displayed, the new class; the campaign lock taken exclusively up front whenever that list is not empty; a 409 when a displayed class has moved; the *narrowest* class that admits the chosen audience, never `campaign` or `public` by default; an epoch advance; and an amendment to ED-13 | `1ir.11.1` |
| O-7 | **Enforcement is a per-campaign GM switch, separate from the capture toggle, and reversible only while no display automation is on** (M-2, M-8) | One-way: once on, it stays on | `1ir.11.1` |

## 13. Sign-off

The bead asks for the Workbench owner's sign-off, recorded on `1kg.7.1` and
`1kg.2.1`. This is what can honestly be recorded, and it is what those comments
say.

| Item | Record |
| --- | --- |
| Who | The Workbench implementation session (Claude, lead for epic `1kg`), acting as the owner's **delegate**: asked on 2026-09-19 to decide this bead or to authorise a draft from the Workbench side, the owner answered "proceed" |
| When | 2026-09-19 |
| Accepted for the Workbench | ED-1 to ED-26 as the Workbench's reading of the shared model; M-1; RQ-1 to RQ-12 and RC-1 to RC-15 as requirements on its beads; rows A-14 to A-18 of the Workbench record |
| Not accepted on the owner's behalf | O-1 to O-7. Each is a change of product scope with a default in force: **escalated, not decided**, and tracked in `agent-forge-harness-1kg.1.7`. `1kg.2.1` and `1kg.7.1` may start on the defaults; **O-1 and O-3 are the dearest to change once `1kg.7.2` and `1kg.7.1` have shipped**, so they are the two to answer first |
| Not yet seen by | the owner; and the Live Session Assistant's planning session, whose plan (PR #60) this record departs from in: ED-2 (a flat key where the plan writes `field_path`); ED-17 (an added event and the reason codes); ED-18 (which bead creates the audit table); ED-26 (hashes); section 4 (delivery reads state); M-2 and M-8 (enforcement is a per-campaign switch, optional and reversible, which qualifies `1ir.11.1`'s "ineligible fields cannot be displayed" and P10's one permission system until a campaign is Enforced); RQ-5 (a revocation advances `authz_revision` in a reconciliation, not in its own transaction, against `1ir.2.1`'s "same transaction"; a tightening's stop is synchronous as L §2.4 asks, but its change of fact may be answered *not applied yet*); and RQ-8 (projector slices, `transaction_timeout`) |
| Reviewed by | two independent passes: an adversarial review of the first draft and a verification of the revision; every finding of both is answered in section 15 |

## 14. Non-goals

- Durable knowledge grants — "what this player has learned", player journals —
  are deferred to the plan's Phase 5 and would amend E-2 (L rule 3).
- Per-item eligibility inside a list field (ED-3).
- Eligibility by version, by session or by time.
- Content inspection: nothing here reads a field's text to decide who may see it
  (ED-22).
- Any player-visible trace of what they were shown earlier (NG-20).
- An export for enrolled players only — a file carrying `campaign`-class fields:
  a file cannot be bound to a principal (ED-21).
- Aliases of entities in the Workbench (ED-23).
- Classification from inside the reveal sheet or the export dialog (M-4, O-6).
- Capture, consent, transcripts, retention and the context firewall, which are
  the plan's other decision beads.

## 15. Review log

One independent adversarial review of the first draft: no Blocker, 2 High, 9
Medium, 7 Low and one Note of eight items. It found the three-layer model, the
flat key, default deny, the pin, the handoff mapping, the choice of every
fallback and the absence of deadlock sound, and found no path by which a field
reaches a player without an explicit GM action. Every finding is answered here.

| # | Severity | Finding | Answer |
| --- | --- | --- | --- |
| V-1 | High | The lock protocol put End, Rotate and expiry behind the authorisation lock and a `lock_timeout`, against X-3, SEC-35 and A-9; "a Stop is never queued" did not survive a narrowing holding the session row, or a full connection gate; `statement_timeout` does not bound a transaction | **Fixed.** Every narrowing has a first step that never asks for the lock (RQ-5); the session row is held only for a write (RQ-6, ED-9); what a Stop can wait for is stated, with a test (RQ-6, RC-7, RC-9); `transaction_timeout` and projector slices (RQ-8); fairness is not relied on; A-17 rewritten. The second review refined this fix (W-1 to W-4, W-6) |
| V-2 | High | "Enforced" was tied to `1ir.2.1` and `1ir.2.2` and to the capture toggle; the plan's enforcement, classification and ledger beads were never named; M-3 and M-4 had no owner; reversibility was unaddressed | **Fixed.** Enforced means `1ir.11.1` plus a per-campaign switch of its own (1.1, M-2); the assistant's display features require it; switching off is M-8 and O-7; section 10 has rows for `1ir.11.1`, `1ir.2.3`, `1ir.2.5`, `1ir.2.7` and `1ir.2.8` |
| V-3 | Medium | Classify-and-display in one Confirm contradicted section 2 and section 4, over-widened, needed a lock upgrade and had no staleness check | **Fixed by reversing the default.** The reveal sheet never classifies (ED-13, M-4); the alternative is O-6, with what it would have to bring. A narrowing class change advances the epoch, so a stale sheet is a 409 (ED-9's courtesy check, ED-12). A-15 rewritten |
| V-4 | Medium | RQ-6 cited an exclusive advisory lock and a `transaction()` that sets no timeouts; a row lock needs `UPDATE` privilege; two mechanisms would not exclude each other | **Fixed.** One mechanism — row locks — through one helper that sets the timeouts, fails closed on a missing row and states its isolation level (RQ-2); a lock-only column for least privilege (RQ-1); owners named |
| V-5 | Medium | The audit log and the disclosure ledger were merged, against the plan; ED-17 and ED-18 disagreed about recipients in v1; `1kg.7.1` had nothing buildable; exports were missing | **Fixed.** Two stores (ED-18): v1 audit rows with their exact fields and a named DDL owner; the ledger with `1ir.2.8`, never backfilled; `exported` added (ED-17, ED-21) |
| V-6 | Medium | A payload hash of a short field in a row that survives deletion is recoverable | **Fixed.** ED-26: no hash in Workbench audit rows; the plan's `payload_hash` is of references, or keyed per campaign and destroyed with it |
| V-7 | Medium | Section 4 defined a principal by live credentials, which made TT-15 a refusal | **Fixed.** `eligible_for_audience` on identities and `entitled` on credentials (section 4, ED-10, ED-25); TT-14 and TT-15 reworded |
| V-8 | Medium | TT-9 prescribed a 404 where the threat model forbids a refusal, and cited the wrong rule | **Fixed.** TT-9 rewritten to threat model §8.2 |
| V-9 | Medium, unverified | In the plan a widening pauses player views, so a private reveal could be observable | **Decided here.** Slot snapshots and frames are never gated on `projection_revision` (ED-25, TT-47); with V-3's fix a reveal is no longer a widening at all. The equivalent for what the assistant composes is a requirement on `1ir.2.5` |
| V-10 | Medium | The flat key did not cover per-alias eligibility, key reuse across type versions or reveal groups; the truth-table premise contradicted ED-5 and ED-20; its keys were not the shipped type's | **Fixed.** ED-23 (aliases), ED-24 (reserved keys, orphan rows), M-4 (groups), ED-5 (`gm_only` by construction); the table uses named fixture types and gains TT-43 and TT-44 |
| V-19 | Medium | The record declared itself Accepted and had no sign-off to copy | **Fixed.** Status is Proposed; section 13 records a delegated technical sign-off in those words and escalates O-1 to O-7 to `agent-forge-harness-1kg.1.7` |
| V-11 | Low | `updated` left rule 1 ambiguous; `replaced` would block automation for ever; `session_ended` was both an event and a reason; reasons were missing; the recipients snapshot is a lower bound | **Fixed** in ED-17: `stopped(mask_narrowed)`, `session_ended(gm_end or expired)`, new reasons, the lower bound stated. L rule 1 is left as written — every `stopped` blocks — until a 7.3 amendment reads reasons |
| V-12 | Low | The "door" column claimed things were in place; the `content_kind` reservation did not exist; 7.1 and 7.2 understated what an amendment costs | **Fixed.** Column retitled; 7.4 is a requirement on `1kg.1.6` and admits the version bump; 7.1 names the migration, the wire change and re-approval; 7.2 the rollback flag |
| V-13 | Low | TT-38 and ED-21 dropped the export's staged mask and changed what player-safe means without saying so | **Fixed.** ED-21 and TT-38 reworded; the dialog never classifies; an export for enrolled players only is a non-goal, with the reason |
| V-14 | Low | ED-9 checked the 409 before the 422s, against SEC-3, and held the session row through validation | **Fixed.** ED-9 and section 4 follow SEC-3; validation and projection building precede the session row; the allowlist citation is REVEAL-9 and REVEAL-10 |
| V-15 | Low | Shrinkage of the principal set was covered only for eligibility rows and for the sheet itself | **Fixed.** ED-12 generalised; TT-45 and TT-46 added; TT-26 has its O-2 branch |
| V-16 | Low | Requirements landed on the wrong bead or could not be acted on | **Fixed.** A trigger creates the row (RQ-1); `1kg.2.1` owns the helper and `1ir.2.1` extends it, tolerating the old one (RQ-10); the resolver calls nothing (RQ-7); enrolment never takes the lock (RQ-5, RC-12); section 10 routes every requirement to every bead that needs it |
| V-17 | Low | "One lock order" ordered three of at least six lock classes | **Fixed.** RQ-3 orders them all, forbids two campaign locks in one transaction and has a Confirm lock no document row |
| V-18 | Note | Eight loose ends: approved versions; delivery model; ED-11's rationale; the error code; ED-16's wording; 7.4's licence claims; dangling plan citations; A-15 and A-17 | **Fixed**, in order: ED-6; section 4's closing paragraph; ED-11 says what is traded away; an additive code with a key list (section 4); ED-16; 7.4 marks a working assumption and asks for licence metadata and stable text; 1.1 names the plan's branch and revision; A-15 and A-17 rewritten |

A second independent pass **verified the revision** against each finding's
original failure scenario: sixteen resolved, three partly (V-12, V-16, V-17),
none unresolved. It again found no path by which content reaches, or stays
readable by, anyone `entitled` would refuse; it confirmed that nothing a player
can reach depends on `authz_revision` advancing in a revocation's own
transaction, that validation before the session row opens no gap, and that the
explicit locks cannot deadlock. It also found what the revision itself had
introduced: no Blocker, no High, 5 Medium and 11 Low. Nothing in either review
was run against a database; the PostgreSQL behaviour relied on is named where it
is relied on, and section 9.1 turns each such claim into a test.

| # | Severity | Finding | Answer |
| --- | --- | --- | --- |
| W-1 | Medium | The revocation's second step was "retried until done" by an outbox that nothing drives after the first attempt; a per-campaign dedupe key could swallow a change whose transaction was still open; a replayed payload could clear a later reveal; a deleted campaign made the job immortal; the job ran inside the End or Rotate request; the enqueue was an unordered lock | **Fixed.** `agent-forge-harness-1kg.2.7` wires the runner before the first two-step change ships and makes an absorbed enqueue lock its row (RQ-12); the job has no dedupe key, is a reconciliation of current state and never a replay, starts after the response is sent, and completes as a no-op when the campaign is gone (RQ-5, RQ-2); the enqueue comes last in the lock order (RQ-3) |
| W-2 | Medium | A locked narrowing that timed out was handed to a job and "never reported as refused": a GM's unlink, archive or tightening could be silently unapplied, and a stale intent replayed over a later action | **Fixed.** Every narrowing stops its displays first, under the session row, and that step is never refused; the change of fact runs in the request and, without the lock, is answered *not applied yet*; a GM's intent is never replayed from a job (ED-12, RQ-5, RQ-8, RC-15, section 4) |
| W-3 | Medium | The lock order ignored implicit foreign-key locks, so a display could deadlock with a Remove, a Reset or an enrolment, and a Stop could wait on player-triggered inserts | **Fixed.** Every explicit row lock below `authz_state` is `FOR NO KEY UPDATE`; participants are marked removed, never deleted; several participant rows are locked in ascending id; a deadlock victim is retried as a lock timeout is; RC-14 tests it on a real database (RQ-3, RQ-6) |
| W-4 | Medium | "Verifies its code before it takes any lock" was check-then-act; "participant or credential row" did not make a Reset and an enrolment meet; `entitled` omitted the participant's status | **Fixed.** The code is looked up before any lock and **consumed by a conditional write under the participant row's lock**, the row Remove and Reset lock; `entitled` requires an active participant; RC-13 (RQ-5, ED-25, section 4) |
| W-5 | Medium | Beads that ship before `1kg.7.1` were told to call `1kg.7.1`'s function; nobody owned the retrofit; RC-5 and RC-6 sat on beads that could not run them | **Fixed.** `1kg.2.1` owns the epochs and the `narrow` primitive with an extension point that `1kg.7.1` fills (RQ-7); RC-5 and RC-6 moved to `1kg.7.2` |
| W-6 | Low | The bounds were set only by `lock_campaign`, which a Stop and a first step never call | **Fixed.** The session-row primitive sets `transaction_timeout` too; a Stop sets no `lock_timeout` (RQ-6, RQ-7) |
| W-7 | Low | M-4 and TT-17 said 409 where ED-9's order gave a 422 | **Fixed.** A stale epoch read without a lock is answered 409 before validation; the rare 422 that follows a late narrowing is handled by the sheet as a 409 is (ED-9, section 10 `1kg.7.3`) |
| W-8 | Low | ED-12 before a campaign is Enforced was undefined | **Fixed.** A class change then stops no Workbench display and the surface says so (ED-12, TT-51) |
| W-9 | Low | The twin's "first lock" check was nearly vacuous; how READ COMMITTED is required was unsaid; a missing row after a deletion should be a 404 | **Fixed** in RQ-2: lock order is proven by database tests; `Database.transaction()` sets the isolation level; the named error maps to the generic 404, or to a no-op in a job |
| W-10 | Low | Section 10 still did not route the three statements to the three beads | **Fixed.** `1kg.2.2` and `1kg.2.3` carry RQ-10 and RQ-12; `1kg.7.2` carries ED-12, RQ-5, RQ-7 and RQ-10; RQ-4 says what advances the revision. The comments on the beads carry the text, and say that they supersede the coordination notes of 2026-09-17 |
| W-11 | Low | Section 13's list of departures from the plan was incomplete | **Fixed** in section 13 |
| W-12 | Low | "Display automation" was undefined, and where "requires Enforced" is checked was unsaid | **Fixed** in M-2: every display not written by the reveal sheet's Confirm, each reading the flag from the locked row |
| W-13 | Low | With forced row-level security `FOR SHARE` needs an `UPDATE` policy | **Fixed** in section 10's `1ir.1.13` row |
| W-14 | Low | If `1ir.2.6` merges first its sketch has `payload_hash` and no `detail` | **Fixed** in section 10's `1ir.2.6` row and ED-18(a), which also says the participant id is deliberate |
| W-15 | Low | Fourteen small items | **Fixed**: activity is tested before `public` (section 4); narrowing is over any principal (ED-12); RC-6 reworded; `session_ended` only for live fields, `displayed` for an added field (ED-17); "not archived" is a named precondition (ED-9); A-18 reworded; every display write carries its session and epoch (RQ-4); an export takes the share lock when Enforced (ED-21); a key taken off an allowlist ships with a stop-scan (ED-24); Moss is classed in the premise; session start ends expired sessions (RQ-7); campaign archive is a fact-changing narrowing (RQ-4, RQ-10); a first step always advances the epoch (RQ-5); a Confirm names its session (ED-9) |
| W-16 | Low | One `transaction_timeout` for every caller could kill a campaign deletion or the enforcement scan while holding out every display | **Fixed** in RQ-2: the bound is a parameter, and long callers chunk |

# Shared field eligibility, display and disclosure

- **Status:** Accepted with defaults in force; section 12 lists what awaits the owner's confirmation
- **Date:** 2026-09-19
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
share, says which layer is enforced in which release, and answers each
amendment. It decides nothing about capture, consent or transcripts.

### 1.1 How to read this record

Every decision has an ID and a **basis**: **W** — a Workbench decision or
invariant (its ID is given); **L** — the Live Session Assistant plan; **T** — the
Workbench threat model (`SEC-`); **C** — wire contract v1; **I** — inferred here,
with the reasoning; **E** — inferred *and* a change of product scope, so the
owner should confirm it (section 12). Each **E** has a default in force.

"Workbench v1" means the scope of epic `1kg`. "Enforced" means the release in
which `1ir.2.1` and `1ir.2.2` have shipped and the campaign has eligibility
switched on (section 8). A requirement on a bead is written once, in section 10,
and copied to that bead as a comment.

## 2. The model

Three layers, deliberately separate (L §4.2), over one identifier:

| Layer | Question | Owner | Lives in | Release |
| --- | --- | --- | --- | --- |
| **Field key** | What is the unit? | `1kg.5.3` | the document type's schema | Workbench v1 |
| **Eligibility** | Who may *ever* see this field? | `1ir.2.1`, `1ir.2.2` | `campaign.field_eligibility` | Enforced |
| **Display** | What is live *now*, to whom? | `1kg.7.1`, `1kg.7.2` | audience slots of the live table session | Workbench v1 |
| **Disclosure** | What *was* shown? | `1ir.2.6` (the one audit log, SEC-38) and `1ir.2.1` | an append-only ledger | events from Workbench v1; ledger when Enforced |

Eligibility never displays anything, and a display never changes eligibility.
The only thing that reaches a player is a display, and the only thing that
creates a display is an explicit GM action (X-2).

## 3. Decisions

| ID | Decision | Basis |
| --- | --- | --- |
| ED-1 | **The three layers of L §4.2 are adopted** as written, with the amendments of this table. | L |
| ED-2 | **The shared identifier is the Workbench's flat field key**: a top-level key of the document type's data object, matching `^[a-z][a-z0-9_]{0,39}$`. It is at once the unit of eligibility, of a reveal mask (REVEAL-9), of field-level concurrency (CANVAS-19), of changed-field lists and of AI-edit scope. **There are no nested paths**: the plan's `field_path` is this key, and its example `motives.hidden_identity` is two fields, `motives` and `hidden_identity`. A secret that needs its own eligibility is its own top-level field — a schema-design rule for `1kg.5.3`. Two units of visibility for one document would let a mask say "shown" where eligibility says "never", about different slices of the same text. | W (CANVAS-19, REVEAL-9) + C + I |
| ED-3 | **List fields are one field** (REVEAL-12): eligibility, like reveal, is whole-list in v1. Per-item eligibility needs per-item reveal first. | W + I |
| ED-4 | **Seven classes, default deny**: `unclassified`, `gm_only`, `participants[ids]`, `characters[ids]`, `groups[ids]`, `campaign`, `public` (L §4.2). A field with no row is `unclassified`. `campaign` excludes guests; `public` includes them and public exports. `characters[ids]` and `groups[ids]` resolve to principals **at read time**, so a relink or a membership change never rewrites rows. | L |
| ED-5 | **Only fields on the type's revealable allowlist can be classified at all** (REVEAL-10). Tags, sources and citation text, version history, authorship, changed-field lists, asset metadata and IDs have no eligibility row: they are never displayable, so there is nothing to classify. | W + L §2.4 |
| ED-6 | **Eligibility is per document and per field key, never per version.** A new version of an eligible field is *eligible* and still *not displayed*: the table keeps the pinned version until the GM reviews `Use latest version` (REVEAL-8). Eligibility answers "may this field's text ever be shown"; the pin answers "which text". | W (REVEAL-8, E-3) + I |
| ED-7 | **`classification_source` is `default`, `gm` or `suggested`, and only `gm` widens.** A type's `defaultReveal` may produce a *suggestion*; it never classifies. A suggestion is never auto-applied (L). | L + W (X-2) |
| ED-8 | **No wildcard is ever stored** — in a mask (REVEAL-9) or in eligibility (L rule 5). `all`, "every field" and bulk actions expand, at the moment the GM decides, into the explicit keys that exist then. A field added to a type later is `unclassified` and unrevealed. | W + L |
| ED-9 | **A display requires, in this order:** a live table session and a GM Confirm carrying the newest reveal epoch that client has seen (REVEAL-22); every masked key on the type's revealable allowlist, present and non-empty in the pinned, sealed version (REVEAL-4, REVEAL-5, CANVAS-34); an audience the type allows (ED-14); **when Enforced**, every masked key eligible for the slot's audience (ED-10); then the one-slot rule (ED-15). The first failing precondition refuses the whole Confirm; nothing is displayed in part. | W + L |
| ED-10 | **Slot audiences and eligibility.** Table slot: every masked key must be `public`, because a guest can see the table. Participant slot: every masked key must be eligible for *that participant* — `public`, `campaign`, a `participants` list naming them, a `characters` list naming a character linked to them now, or a `groups` list naming a group they belong to now. `unclassified` and `gm_only` are eligible for nobody but the GM. | L |
| ED-11 | **Workbench v1 ships mask-only; eligibility is enforced from the release that brings it** (section 8). In v1 the GM's explicit Confirm *is* the authorisation (X-2): eligibility would add nothing to a manual reveal, because it constrains what *automation* may surface, and v1 has none. Enforcing it from the first Workbench release would add blocking edges from `1ir.2.1` and `1ir.2.2` to `1kg.7.2` and tie reveal to the assistant's schedule. | I · **E** (O-1) |
| ED-12 | **Narrowing always wins (X-3), and tightening eligibility is a narrowing.** In one transaction it stops every live display of a field that is no longer eligible for that slot's audience, writes `stopped` events, removes the affected table-namespace rows, advances `authz_revision` **and advances the Workbench reveal epoch**, so a widening the GM staged before it is a 409 (REVEAL-22). | W + L §4.3 |
| ED-13 | **Widening eligibility displays nothing.** Classifying a field `public` makes it displayable; it does not display it. Additions to a group, a relink *towards* a participant and a new enrolment likewise display nothing: the next thing that participant sees is whatever the GM next reveals to them (X-2, AUD-10). | W (X-2) + L rule 4 |
| ED-14 | **Which audiences a type allows stays AUD-9 in Workbench v1**: a participant audience only for owner-audience types (the character sheet); every other type is table-only. This is a **service rule, not a schema shape** — a slot row carries an audience and a document, and nothing in the schema ties one to the other's type — so the plan's Phase 4 amendment is a rule change plus UI, not a migration. | W (AUD-9, NG-22) + I · **E** (O-2) |
| ED-15 | **A document is live in at most one slot, and a slot holds one live projection** (§7.1, REVEAL-7, NG-20). `1kg.7.1` expresses the first half as a **partial unique index** over live rows rather than as a column on the document, so that accepting group displays later is dropping an index and adding a nullable disclosure reference (expand, then contract — `docs/migrations.md`), not a rewrite. | W + I · **E** (O-3) |
| ED-16 | **Stop and Retract.** *Stop* clears a slot now, needs no confirmation and is never refused (X-3, REVEAL-6). *Retract* is Stop plus the statement "this should not have been shown": the ledger records `retracted` instead of `stopped`, and automation may never re-display that field to that audience. Workbench v1 has no automation and therefore no Retract control; **Stop is the only narrowing control in v1**, and the distinction arrives with the assistant's Phase 4. A manual reveal after either is still possible: it is a new explicit GM action. | L + W |
| ED-17 | **Ledger events** are `displayed`, `updated` (the GM pushed a newer pinned version or changed the mask of a live display), `stopped`, `session_ended` and `retracted`. Each names the subject `(document, field key, version)`, the slot, a snapshot of the recipients entitled at that moment, the actor, the session and a payload hash — **references and hashes, never field text** (SEC-20, SEC-38). Reasons are codes: `gm_stop`, `stop_all`, `replaced`, `moved`, `session_end`, `session_expired`, `link_rotated`, `participant_removed`, `personal_link_reset`, `character_unlinked`, `group_member_removed`, `eligibility_tightened`, `document_archived`, `eligibility_enabled`. | L + T |
| ED-18 | **From Workbench v1 the reveal service emits these events** to the one audit log (SEC-38: "every reveal, update and stop — document id, version number, mask *keys*, audience kind"). The ledger of `1ir.2.1` is that stream with recipients snapshots added; if the assistant's audit service exists first, the Workbench writes to it (SEC-38), and if not, `1ir.2.6` adopts the Workbench's rows. One stream, not two. | T + L |
| ED-19 | **Assets follow their field** (L rule 7, REVEAL-21, MS-7). A portrait is displayable exactly when its `portrait` field is masked into a live slot; it is served through the table asset route on a per-slot handle, `no-store`, authorised against the live slot on every read, and the handle dies with the slot. An asset has no eligibility of its own. | W + L |
| ED-20 | **Identity links are GM-only relations** (L rule 6). "The Hooded Stranger *is* Ondrey" is never a revealable field and never reaches a table namespace, even when both names are `public`. If a type needs the link, it is a `gm_only`-only field (ED-5's allowlist excludes it from reveal). | L |
| ED-21 | **Exports use the same builder and the same rule.** A player-safe export contains exactly the fields a table display could: on the revealable allowlist, and — when Enforced — `public` (EXPORT-3, EXPORT-7, REVEAL-21). | W |
| ED-22 | **Eligibility cannot see inside a field.** An AI edit that moves a secret from a `gm_only` field into a `public` one is not caught by eligibility. The controls are the pin — nothing new reaches the table until the GM reads old text beside new in `Use latest version` (REVEAL-8) — and, for automation, the assistant's pre-generation firewall (L §4.5). This is recorded so that nobody mistakes eligibility for content inspection. | I |

## 4. Evaluation rules

```text
eligible(document, key, principal) when Enforced:
  if principal is the GM                      -> yes
  class := field_eligibility[document, key] or unclassified
  unclassified | gm_only                      -> no
  public                                      -> yes            (participants, guests, public exports)
  if principal is a guest                     -> no
  campaign                                    -> yes            (any participant of this campaign)
  participants[ids]                           -> principal.id in ids
  characters[ids]                             -> principal is linked NOW to a character in ids
  groups[ids]                                 -> principal is a member NOW of a group in ids

a participant is a principal only with BOTH a live table session and their
enrolled device credential (AUD-5); otherwise they are a guest, or nobody.

display(slot, document, version, mask), in one transaction:
  lock campaign.authz_state FOR SHARE         (first lock, always - section 9)
  lock the table session row; require its reveal epoch = the epoch the Confirm carries   else 409
  require version is sealed and is the version the sheet displayed                       else refused (REVEAL-5)
  require every key in mask: on the type's revealable allowlist, present, non-empty      else 422
  require audience allowed for the type (ED-14)                                          else 422
  when Enforced: require every key in mask eligible for the slot's audience (ED-10)      else 422
  replace what the slot holds; clear the document from any other slot (ED-15)
  advance the reveal epoch and the slot sequence; write displayed / updated (+ stopped for what was replaced)
  notify (a wake-up; delivery reads state)

stop(slot or all), never refused:
  lock the table session row only             (never waits on the authorization lock - RQ-4)
  clear the slot if it holds what the Stop names; advance the epoch even if it was empty
  write stopped (or retracted); notify
```

A 422 for eligibility uses the contract's `validation_failed` with the field
named by **key**, never by text; whether a key is eligible is not a secret from
the GM, who is the only caller.

## 5. Truth table

One campaign. The GM owns it. **Ana** and **Ben** are participants with enrolled
devices; **Cy** is a participant who has not enrolled; anyone else holding the
table link is a **guest**. Ana is linked to the character sheet *Kira*. The NPC
*Ondrey* has `name` and `portrait` classed `public`, `history` classed
`campaign`, `rumours` classed `participants[Ana]`, `motives`, `hidden_identity`
and every stat cell classed `gm_only`, and `notes` `unclassified`. Kira's fields
are classed `characters[Kira]`.

"v1" is Workbench v1, mask-only: no classes exist, so a row that depends on a
class reads "allowed if ticked". "Enforced" is the target model.

| # | Situation | Action | Workbench v1 | Enforced | Rule |
| --- | --- | --- | --- | --- | --- |
| TT-1 | A table session is live | Reveal Ondrey `{name, portrait}` to the table | allowed | allowed | ED-9, ED-10 |
| TT-2 | same | Reveal Ondrey `{name, history}` to the table | allowed if ticked | **refused, 422**: `history` is `campaign`, and a guest can see the table. Nothing is shown, not even `name` | ED-10, ED-9 |
| TT-3 | same | Reveal Ondrey `{motives}` to the table | allowed if ticked — the GM's explicit act (X-2) | refused, 422: `gm_only` is never displayable; the sheet's row is disabled with that reason | ED-10 |
| TT-4 | same | Reveal Ondrey `{notes}` to the table | allowed if ticked | refused until classified; the sheet offers the classification in the same Confirm (M-4) | ED-10, M-4 |
| TT-5 | same | Reveal Ondrey `{history}` to Ana's slot | refused, 422: an NPC is table-only (AUD-9) | refused by AUD-9 under the fallback; **allowed** if O-2 is accepted | ED-14 |
| TT-6 | O-2 accepted | Reveal Ondrey `{rumours}` to Ben's slot | — | refused, 422: `participants[Ana]` does not name Ben | ED-10 |
| TT-7 | same | Reveal Ondrey `{tags}` or `{sources}` anywhere | refused, 422: not on the revealable allowlist | the same; there is no eligibility row to consult | ED-5 |
| TT-8 | Ondrey `{name, portrait}` is live on the table | A guest reads the table slot | sees `name` and the portrait through a per-slot handle | the same | ED-19 |
| TT-9 | same | A guest asks for Ana's slot with a forged participant id | 404, identical to an empty slot; nothing to infer | the same | REVEAL-24, SEC-2 |
| TT-10 | No table session is live | Ana's enrolled device asks for anything | nothing: a device credential alone opens nothing | the same | AUD-5 |
| TT-11 | A session is live | Reveal Kira, every existing field, to Ana's slot | allowed; `all` is expanded to explicit keys before it is sent | allowed: `characters[Kira]` resolves to Ana | AUD-12, ED-8, ED-10 |
| TT-12 | same | Reveal Kira to Ben's slot | refused, 422: the sheet is not linked to Ben (AUD-9) | refused on both counts | ED-14, ED-10 |
| TT-13 | same | Reveal Kira `{name, class}` to the whole table | allowed (AUD-9's `Whole table`) | refused until those two keys are `public`; the sheet offers it (M-4) | ED-10 |
| TT-14 | Ben is enrolled, but opens the table link in a private window that has no device credential | that window reads the session | it is a guest: the table slot only, never Ben's slot | the same: only `public` fields, whatever Ben himself is eligible for | AUD-5, ED-10 |
| TT-15 | Cy has a linked sheet *Moss* and no device | Reveal Moss to Cy's slot | confirms; the GM sees `Cy hasn't joined yet`; nothing is delivered, and it **never** falls back to the table | the same | AUD-10 |
| TT-16 | Kira is live in Ana's slot | The GM relinks Kira to Ben | the unlink stops the projection first, in one transaction; `stopped(character_unlinked)`; the epoch advances. Ben sees nothing | the same, and `characters[Kira]` now resolves to Ben — **eligible, not displayed** | AUD-15, ED-13 |
| TT-17 | same, and the GM had a staged widening open in another tab | that tab presses Confirm | 409, never retried by itself; the sheet reloads and keeps the draft | the same | REVEAL-22, REVEAL-15 |
| TT-18 | Ondrey is live on the table | **Stop showing** | cleared at once; `stopped(gm_stop)`; the epoch advances | the same | ED-16, X-3 |
| TT-19 | a Confirm and a Stop cross on the wire; the Stop lands first | the Confirm arrives | 409; the table never shows the document | the same | REVEAL-22, AE-48 |
| TT-20 | Ondrey is live on the table | Reveal *Marsh Map* to the table | replaces Ondrey after the sheet says so; `stopped(replaced)` then `displayed` | the same | REVEAL-7, ED-15 |
| TT-21 | Kira is live in Ana's slot | Reveal Kira `{name, class}` to the table | *moves* it after the sheet says so; `stopped(moved)` then `displayed` | the same, if both keys are `public` | REVEAL-7, ED-15 |
| TT-22 | anything is live | The GM ends the session, or it expires | every slot cleared; `session_ended`; the link dies; the next session starts with nothing revealed | the same | REVEAL-17 |
| TT-23 | anything is live | **Rotate link** | every table credential dies; every slot cleared; `stopped(link_rotated)`; the epoch and `authz_revision` advance; enrolment credentials survive unless `Also reset personal links` was chosen | the same | REVEAL-17, RQ-3 |
| TT-24 | Kira is live in Ana's slot | The GM removes Ana | the projection stops, her device credential is revoked, the document is kept; `stopped(participant_removed)` | the same | AUD-16 |
| TT-25 | same | **Reset personal link** for Ana | the old device loses access at once and her slot is stopped; a new code is issued | the same | AUD-5, L §4.9 |
| TT-26 | same | Ana enrols a second device | the first device is replaced; the display is Ana's, so it continues on the new device; the GM sees `Ana joined from a new device` | the same | AUD-5 |
| TT-27 | Ondrey `{name, history}` is live to Ana (O-2 accepted) | The GM tightens `history` to `gm_only` | — | one transaction: the display is stopped whole, `stopped(eligibility_tightened)`, table-namespace rows go, `authz_revision` and the epoch advance | ED-12 |
| TT-28 | `notes` is `unclassified` | The GM classifies it `public` | — | nothing is displayed; it is now displayable | ED-13 |
| TT-29 | Ondrey `{history}` is live | An AI edit rewrites `history` | the table is unchanged; the indicator offers **Update…** with old text beside new | the same; `history` stays eligible, the new text is not displayed | REVEAL-8, ED-6 |
| TT-30 | an AI edit copied a line of `motives` into `history` | The GM opens **Update…** | the diff shows the line; nothing reaches the table unless the GM confirms | the same; eligibility does not catch it — the pin and the GM's review do | ED-22 |
| TT-31 | the NPC type gains a field `secret_ally` in a later release | an old mask said `all` | never revealed: `all` was expanded when it was used, and was never stored | the same, and the field is `unclassified` | ED-8 |
| TT-32 | a group *Scouts* = {Ana, Ben} | Display Ondrey `{history}` to Scouts | not available: no group displays | not available under the fallback. If O-3 is accepted: one display record per member slot, one disclosure; each replaces that member's private view after a warning | ED-15 |
| TT-33 | O-3 accepted; the group display is live | **Stop showing** on Ondrey | — | every copy is cleared in one transaction | L §2.5 |
| TT-34 | same | Ben leaves Scouts | — | Ben's copy stops (`group_member_removed`); Ana's stays | L §4.9 |
| TT-35 | same | Cy joins Scouts | — | nothing reaches Cy until the GM displays again | ED-13 |
| TT-36 | Ondrey `{history}` was shown to Ana and then **retracted** | an automation rule proposes showing it again | — | refused for automation, for ever; the GM may still reveal it by hand | ED-16 |
| TT-37 | Ondrey `{name}` was shown and the session ended | a later session starts | nothing is re-displayed; v1 has no automation | under the fallback, nothing; if O-4 is accepted, a GM-authored rule may *re-display* it, since its last event was `session_ended` | L rule 1 |
| TT-38 | a player-safe export of Ondrey | **Export → Player-safe** | the fields a table display could carry, from the same builder | exactly the `public` fields | ED-21 |
| TT-39 | the GM quotes a rule to the table | Display a rules excerpt | not available: a slot holds documents only | not available under the fallback; if O-5 is accepted, a deterministic quote whose licence permits display to guests | section 7.4 |
| TT-40 | `session-notes` with a `recap` | Open the reveal sheet | the draft seeds empty and the row warns `Summarises your private GM thread` | the same, and `recap` is `unclassified` | REVEAL-23 |
| TT-41 | eligibility is being switched on for the campaign | a table session is live | — | the switch is a narrowing: every slot is cleared, `stopped(eligibility_enabled)`, and the GM re-reveals under the new rule | M-3 |
| TT-42 | "The Hooded Stranger" and "Ondrey" are both `public` names | anything | the link between them is not a revealable field | the same; it never reaches a table namespace | ED-20 |

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
assistant's Phase 4 and each changes what the product is. None is rejected: the
"door" column says what is already in place so that accepting it later is a rule
and UI change rather than a rebuild, and the last column says what the bead that
needs it must bring.

| # | Request (L §2.5) | In force | The door | What a later amendment must bring |
| --- | --- | --- | --- | --- |
| 7.1 | Participant-slot displays for **any** document type (against AUD-9, NG-22) | **Fallback**: a participant slot only for a linked character sheet; every other type is table-only, `public` fields only | ED-14: the restriction is a service rule; slots, masks and eligibility are type-agnostic | an amendment to AUD-9 with the audience picker's behaviour for other types, and threat-model amendment 3 (A-3): **GM approval of each new device before owner-only content flows**, since a participant slot would then carry more than a player's own sheet |
| 7.2 | **Group displays** (against the one-slot invariant, REVEAL-6/7/22, NG-20) | **Fallback**: none; the GM displays to one participant at a time, moving the document (REVEAL-7) | ED-15: the invariant is a droppable partial unique index; a nullable disclosure reference is additive | an amendment to §7.1 and REVEAL-7 stating that Stop on the document or the disclosure clears every copy in one transaction, that a member's removal is a narrowing, and that an addition displays nothing (TT-33 to TT-35) |
| 7.3 | A GM-authored, enabled rule as the **X-2 action for re-displays** | **Fallback**: rules only *propose*; every display needs one-tap approval | ED-16, ED-17: the ledger already tells `stopped` and `retracted` from `session_ended`, which is what such a rule must read | an amendment to X-2 that names what a rule must name — documents or excerpts, fields, audience, trigger — that it never performs a *first* disclosure, and that a Stop disables the rule's re-display of that subject until the GM re-arms it |
| 7.4 | A **reference excerpt** slot content kind for rules text (against the §7.1 slot model, REVEAL-5/9, NG-25) | **Fallback**: no rules content on player devices; the GM reads rules aloud | wire contract v1 reserves a slot `content_kind` discriminator whose only value is `document` | section 7.4 below |

### 7.4 Rules excerpts, if they ever ship

- **Representation.** A slot holds either a document projection or, under this
  amendment, a *reference excerpt*: `(corpus_chunk_id, corpus_version)`, the
  exact character span, and the citation. The ledger's subject is the same pair.
  There is no field mask, because there are no fields.
- **Deterministic quotes only.** The text on a player's device is a verbatim
  span of a corpus chunk, produced by lookup and never by a model. A paraphrase
  or a summary is assistant output and is GM-only.
- **Whose entitlement governs.** The GM's entitlement to read a source governs
  what the *GM* sees. Displaying it to a guest is redistribution to someone with
  no entitlement at all, so the chunk's **own licence** must permit display to
  the public: openly licensed text (the SRD under CC BY, CC BY-SA wiki text)
  with its attribution shown, and nothing else. That judgement is the licensing
  review's, `agent-forge-harness-yje.6.1`; until it is made the fallback stands.
- **First display by approval; re-displays per 7.3.**

## 8. Mask-only first, and the migration plan

| ID | Step | Basis |
| --- | --- | --- |
| M-1 | **Workbench v1 builds reveal exactly as its record says** (sections 7 and 8, threat model SEC-13 to SEC-19), with no eligibility table and no eligibility check, and with the four things section 9 requires from day one: the campaign authorisation row and its lock order, `authz_revision`, ledger-shaped audit events, and a Confirm whose preconditions are a named list (ED-9). | ED-11 |
| M-2 | **Eligibility arrives per campaign, switched on by the GM** when they enable the assistant (L §3.1), never silently by a deploy. Until then a campaign behaves as v1. | L + I |
| M-3 | **Switching it on is a narrowing.** In one transaction under the authorisation lock every live slot of the campaign is cleared with `stopped(eligibility_enabled)` and the epoch advances, because a live display of unclassified fields would violate ED-10 the moment the switch flipped. The GM is told first; the simplest path is to switch it on between sessions. | X-3 + I |
| M-4 | **From then on the reveal sheet is also the classification surface.** Each row shows its class. Ticking a field that is not yet eligible for the chosen audience shows what Confirm will do — `Will be marked public`, or `Will be shared with all players` — and Confirm performs the classification and the display in one transaction (the authorisation lock taken `FOR UPDATE`, since it widens eligibility). A `gm_only` row cannot be ticked, and says why. This keeps a manual reveal one explicit action that names everything it shows *and* everything it makes showable. It is not L rule 4's implicit rewrite: the GM reads the widening and confirms it. | I · **E** (O-6) |
| M-5 | **Nothing is backfilled from history.** Having been revealed once does not make a field `public`: v1 has no durable grants (L rule 3), and a one-off reveal to five friends is not a decision that strangers with the link may see it. Every field starts `unclassified`. | L + I |
| M-6 | **A type's `defaultReveal` may be offered as suggestions** (`classification_source: suggested`) when the GM first opens classification for a document; none is applied without the GM's action (ED-7). | L |
| M-7 | **The policy oracle (`1ir.1.12`) takes section 5 as its first cases**, both columns: the v1 column is a regression suite for `1kg.7.2` today, and the Enforced column is the acceptance suite for `1ir.2.2`. | I |

## 9. Locks, revisions and the epoch

| ID | Requirement | On |
| --- | --- | --- |
| RQ-1 | **`1kg.2.1` creates `campaign.authz_state (campaign_id PRIMARY KEY, authz_revision BIGINT NOT NULL DEFAULT 0)` with the campaign, in the same transaction.** `1ir.2.1` adds `projection_revision` and the queue by an additive migration. Both epics lock this one row. | `1kg.2.1` |
| RQ-2 | **One lock order, everywhere: the campaign's `authz_state` row first, then the table session row, then slot rows.** Authorisation mutations take `authz_state` `FOR UPDATE`; display writes — Reveal, Update, replace and move, and later Share, rule re-displays and recap publication — take it `FOR SHARE`, then check. A display therefore either commits before a narrowing, which sees it and stops it, or runs after it and is refused (L §4.3). | `1kg.2.2`, `1kg.2.3`, `1kg.7.2`, `1ir.2.x` |
| RQ-3 | **These mutations advance `authz_revision` under that lock**: participant add and remove, device enrolment, replacement and reset, character link and unlink, session start, end and expiry, link rotation, and — when it exists — eligibility and group membership. Renaming a participant does not. One helper does it, so the assistant calls the same code (`LSA-2.1`). | `1kg.2.2`, `1kg.2.3` |
| RQ-4 | **A Stop never waits on the authorisation lock.** It takes the table session row and the slot only. It cannot deadlock against RQ-2 — it never asks for `authz_state` — and it cannot be queued behind a projector or a long authorisation transaction (X-3). A Stop and an eligibility narrowing that race are both narrowings and are idempotent. | `1kg.7.2` |
| RQ-5 | **Every narrowing advances the session's reveal epoch, eligibility narrowing included** (ED-12, REVEAL-22). `1kg.7.1` exposes one function that does it inside the caller's transaction; `1ir.2.2` calls it. | `1kg.7.1`, `1ir.2.2` |
| RQ-6 | **Lock hold time is bounded**: no network or model call under `authz_state`, and `lock_timeout` and `statement_timeout` on every transaction that takes it (L §4.3 rule 7; `Database.transaction()` and `UnitOfWork.lock` from `1kg.1.5`). | both |
| RQ-7 | **The reveal Confirm is one transaction whose preconditions are a named, ordered list** (ED-9), so that "every masked key is eligible" is one more entry when Enforced, not a redesign. | `1kg.7.2` |

## 10. What downstream beads must honour

| Bead | Requirement |
| --- | --- |
| `1kg.5.3` | ED-2, ED-3, ED-5, ED-20: flat keys; a secret that needs its own eligibility is its own field; the revealable allowlist is also the classifiable list; identity links are not revealable fields |
| `1kg.2.1` | RQ-1; participants, character links and sessions shaped so RQ-3's helper can be one function |
| `1kg.2.2`, `1kg.2.3` | RQ-2, RQ-3 |
| `1kg.5.1` | ED-6: eligibility and masks name a field key of a document; versions are immutable and sealed, and nothing about visibility is stored on a version or a document row |
| `1kg.1.6` | The reveal family of the wire contract: masks as explicit key lists; audiences `table` and `participant:<id>`; the reserved slot `content_kind`; a 422 that names keys, never text; no eligibility fields in v1 (additive later) |
| `1kg.7.1` | ED-15 (partial unique index), ED-17 and ED-18 (ledger-shaped events), RQ-5 |
| `1kg.7.2` | ED-9, ED-16, RQ-2, RQ-4, RQ-7; section 5's v1 column as tests |
| `1kg.7.3` | REVEAL-4's seeding unchanged; M-4 is *not* built in v1 |
| `1ir.2.1`, `1ir.2.2` | ED-4, ED-7, ED-8, ED-10, ED-12, ED-13, M-2 to M-6, RQ-2, RQ-5; `field_path` is the flat key of ED-2 |
| `1ir.1.12` | M-7 |
| `1ir.1.13` | The roles it defines must let the Workbench's display writer take `authz_state` `FOR SHARE` and the authorisation writer `FOR UPDATE`; a Stop needs neither |
| `1ir.2.6` | ED-17, ED-18: one audit stream |

## 11. Responses to the Workbench decisions the bead names

| Workbench decision | Response |
| --- | --- |
| AUD-1, **E-6** (one GM; players have no accounts) | Adopted. Principals are the GM, participants and guests. If accounts arrive they bind to participants and no class changes |
| AUD-2, AUD-8 (audiences `table` and `participant:<id>`; one slot each) | Adopted as the display layer |
| AUD-3 to AUD-5, **E-1** (owner proof by device enrolment) | Adopted. A participant is a principal only with a live session *and* the enrolled credential; every participant-scoped class rests on it. If E-1 were dropped, `participants`, `characters` and `groups` would have no one to resolve to, and only `public` could ever be displayed |
| AUD-6, AUD-7, **E-10** (one table link; guests anonymous) | Adopted. A guest is the principal for `public` and for nothing else; a ledger's recipients snapshot records guests as a count, never as identities |
| AUD-9 | Kept for v1 (7.1, O-2) |
| AUD-10 (never falls back to the table) | Adopted (TT-15, ED-13) |
| AUD-11 (aliases) | Adopted: a recipients snapshot holds participant ids, never aliases |
| AUD-12 | Adopted (section 6) |
| REVEAL-7, **E-2** (one live document per slot, no player-side history) | Adopted; group displays deferred (7.2, O-3). The ledger is the GM's record, never a player's shelf |
| REVEAL-8, **E-3** (pinned versions) | Adopted (ED-6, TT-29, TT-30). If E-3 changed to "follow the current version", ED-22's gap would become a live leak path, and eligibility would have to be re-checked on every save |
| REVEAL-9, REVEAL-10 | Adopted and extended to eligibility (ED-5, ED-8) |
| REVEAL-21 | Adopted (ED-19, ED-21) |
| REVEAL-24 (no inference) | Adopted: eligibility rows, classes and revisions never reach a table client; a refusal for eligibility is only ever sent to the GM |
| X-2 | Adopted; M-4 keeps classification inside the same explicit action; re-display rules deferred (7.3, O-4) |
| X-3 | Adopted and extended: tightening eligibility is a narrowing (ED-12), switching eligibility on is a narrowing (M-3), a Stop never waits (RQ-4) |
| X-4 | Adopted: nothing here adds anything a table client keeps |
| X-7 | Adopted: the ledger and the audit stream hold references, keys and hashes (ED-17) |
| X-10 | Adopted: an excerpt (7.4) is text; it loads nothing |

## 12. Decisions that await the owner's confirmation

Each changes what the product *is*. Each has a default in force so that work is
not blocked; confirming or changing one is cheap now and dear after its
dependants ship.

| ID | Default in force | Alternative | Dear to change after |
| --- | --- | --- | --- |
| O-1 | **Workbench v1 ships reveal mask-only**; eligibility is enforced per campaign from the release that brings it (ED-11, section 8) | Enforce eligibility from the first Workbench release: `1kg.7.2` then waits for `1ir.2.1` and `1ir.2.2` | `1kg.7.2` |
| O-2 | **Participant slots stay character-sheet-only** (7.1) | Accept the plan's amendment now, with device approval (A-3) | `1kg.7.3`, the assistant's Phase 4 |
| O-3 | **No group displays** (7.2) | Accept the amendment now | `1kg.7.1` — cheap either way, thanks to ED-15 |
| O-4 | **Rules only propose; every display needs approval** (7.3) | A GM-authored rule is the X-2 action for re-displays | the assistant's Phase 5 |
| O-5 | **No rules text on player devices** (7.4) | Reference excerpts, after the licensing review | the assistant's Phase 5; `yje.6.1` |
| O-6 | **When eligibility is on, the reveal sheet classifies and displays in one Confirm** (M-4) | Classification only on a separate surface; a reveal of an unclassified field is refused until the GM has been there | `1ir.2.2`, the assistant's Phase 4 |

## 13. Non-goals

- Durable knowledge grants — "what this player has learned", player journals —
  are deferred to the plan's Phase 5 and would amend E-2 (L rule 3).
- Per-item eligibility inside a list field (ED-3).
- Eligibility by version, by session or by time.
- Content inspection: nothing here reads a field's text to decide who may see it
  (ED-22).
- Any player-visible trace of what they were shown earlier (NG-20).
- Capture, consent, transcripts, retention and the context firewall, which are
  the plan's other decision beads.

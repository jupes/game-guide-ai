# Plan Review: live-session-assistant — Live Session Assistant (turn 2)

Source: `docs/forge/research/live-session-assistant-gap-analysis.md` (gap), `docs/forge/plans/live-session-assistant.md` (master), `docs/forge/research/live-session-assistant-threat-model.md` (threat), `docs/forge/research/live-session-assistant-cost-model.md` (cost), `docs/forge/plans/live-session-assistant-delivery.md` (delivery), and the turn-1 review with its Resolution table. Also reviewed: the scratchpad bead spec and tooling (`lsa/spec.ts` = spec). The GM Workbench decision record `docs/adr/gm-workbench-interactions.md` (ADR) was read on `origin/docs/1kg.1.1-gm-workbench-decisions`, and the wire contract v1 on `origin/feat/1kg.1.2-workbench-wire-contract`. · Reviewed: 2026-09-16

Reviewer: independent, read-only turn-2 pass by the same model tier as the author. I ran `bun validate.ts`, `bun costs.ts` (diffed against `cost-tables.md`), the author's read-only `checkdocs.ts`, and inline `bun -e` closure analysis over spec.ts. I diffed the generated tables against the render and xdeps output. Code baseline: `origin/master` `2ca91e6`. Tracker: `bd list --all --json --limit 0` run from the repo root returned 152 issues and 308 `blocks` edges, identical to `bd-all.json` with no status drift. Line numbers are as of this review.

## Verdict: NEEDS REVISION — 0/3/9/5

- **Turn-1 fixes:** the bookkeeping fixes landed and reproduce exactly: validator counts, generated tables, external links, cost tables, and code claims. Of 25 turn-1 findings, 20 are resolved and 5 are partly resolved (H1, H3, H6, M6, M11).
- **HIGH — projector race:** the new freshness mechanism has a race. A queued widening can put back rows that a later narrowing removed, and the freshness check still passes. Player-space resolution and player-facing model inputs would then see a secret.
- **HIGH — MVP table devices:** the MVP consent page needs the Workbench table client (`1kg.7.4`), but the plan doesn't depend on it. Table-device capture indicators are deferred to Phase 2, although the plan calls them a precondition of any capture.
- **HIGH — Workbench contradictions:** participant-slot NPC displays, group fan-out, and rules cards in slots contradict accepted Workbench decisions (AUD-9, NG-22, NG-25, "a document is live in at most one slot"). §2.4 says the plan complies with them.
- **MEDIUM findings:** these cluster around:
  - database-role coverage outside the player read path;
  - phase ordering (`LSA-4.7`, counsel review before dogfood, the $10 kill switch);
  - storage order in the pipeline;
  - guest deletion rights;
  - rules cards;
  - CSRF tests;
  - recap declassification.

## Turn-1 resolution check

| Finding | Status | Evidence |
| --- | --- | --- |
| H1 Workbench model | Partial | Participants, guests, and slots were re-derived (master §2.2, §4.2). `1.2` blocks `1kg.2.1`, `1kg.5.1`, `1kg.5.3`, and `1kg.7.1` (spec `blocks`; delivery:244). **Still open:** slot usage contradicts ADR:476, AUD-9 (ADR:565), and NG-22 (ADR:833); see the third HIGH. gap:420-421 still lists "GM attestation for non-account attendees" as a consent record type, against P2 (master:97). |
| H2 Reuse | Resolved | `yje.5.2` blocks `3.5` and `8.2`. `1kg.7.5` blocks `9.3` and `11.2`. `1kg.7.2` blocks `11.1`. `yje.5.2` is among the validator's 16 MVP blockers. |
| H3 Age | Partial | Per-participant attestation is in master:217-219 and §3.10, and in the `3.1`, `3.2`, and `3.5` text. gap:429-430 still says "age attestation belongs with `yje.2.2` signup". |
| H4 Audit | Resolved | `2.6` is blocked only by `1.7` and `1kg.1.5`. It blocks `3.1`, `4.2`, `5.4`, `6.2`, and G1. Rule R8b is in validate.ts. The only bead that mentions audit without `2.6` upstream is decision `1.7`. |
| H5 Baseline/va8 | Resolved | Headers cite `2ca91e6`. `va8` blocks `4.6` and `13.2`. `3.4` is scoped to microphone policy. TM-15 was rewritten (threat:145). Checked `Markdown.tsx:48-49` on `origin/master`. |
| H6 Freshness | Partial | Synchronous narrowing (master:653-654), the freshness check (796-798), and the test (`2.5` acceptance; §4.11 #10) are in place. The asynchronous widening path has no concurrency contract; see the first HIGH. |
| H7 Fingerprint | Resolved | Typed tuple at master:787-795; property test in the `2.5` acceptance and §4.11 #11. |
| M1 Triggers | Resolved | T01, T04, T06, T07, and T12 carry Phase 1 `GM_ON_REQUEST`; T15 is "(1+)"; this matches §7.1 and `4.6`. |
| M2 Build order | Resolved | `1.10` is blocker-free; `1.12` → `1.2`; `4.3` and `4.5` wait only on `1.5`; `xiu.1.2` moved to `2.4`. The longest G1 path is now 9 hops. Accepted trade-off with H2: `3.5` still waits on `yje.1.2` through `yje.5.2`. |
| M3 Phase 5 | Resolved | `12.9`, `12.10`, and `12.11` block `13.11`; `13.13` follows it; T23 was removed and T09 has no player notice. New ordering issue in the MEDIUM on dogfooding before counsel review. |
| M4 Visibility | Resolved | Within the plan's own model (master §4.2 rules 1–4; `2.8` acceptance). Workbench-compatibility gaps are in the third HIGH. |
| M5 DB roles | Resolved as asked | Migration owner, projector role, forced RLS, per-role secrets, and the service decision in `1.13` are present; threat §6.4 is fixed (232-238). Remaining role gaps are in the first MEDIUM. |
| M6 Workers | Partial | The helper is specified (master:683-688) and tested for player deliveries (`2.5`), but `2.5` is Phase 4 while Phases 1–3 need it. |
| M7 Margins | Resolved | `bun costs.ts` output is byte-identical to `cost-tables.md`. Heavy GM at the $25 cap: $5.43 → $18.25 (73.0%). |
| M8 Cadence | Resolved (cost) | Extraction is debounced to ≥30 s in master:1001-1011, cost:204-217, and the `4.5` acceptance. New storage-order issue in its own MEDIUM. |
| M9 Shedding | Resolved | cost:412-426 matches billing-plan rule 4 (billing:419-420). |
| M10 PTT session | Resolved | Precondition 3 (master:215); `1kg.2.3` blocks `3.1`; the `3.1` acceptance says attestations and consents "expire with the table session". |
| M11 Kill switch | Partial | `yje.6.6` blocks `9.3`, but Phase 1 adds recurring spend under a switch that `yje.6.6` itself says has ≈$0.60 of headroom. |
| M12 Links | Resolved | All suggested blocking and relates-to edges are present in spec and in the xdeps table. New missing edge (`1kg.7.4`) in the second HIGH. |
| L1 Validation record | Resolved | Validator: 0 errors. 116 beads; 240/39/4/42 edges; 16 MVP blockers. Matches delivery §7. |
| L2 Keys/phases | Resolved | No `LSA-D*` or `LSA-07.*` keys remain; every `LSA-` reference resolves. Recaps are Phase 5; T16 is Phase 3; copy reads "12 listening hours". |
| L3 Gap-analysis code facts | Resolved | aria-live (gap:376-379), nginx vs Cloud Run (127, 193-195), and `_clean` scope (401-404) are now correct. Small leftover in the gap-analysis LOW. |
| L4 Portraits | Resolved | master §4.8 and the `5.5` acceptance. Decision ownership noted in the last LOW. |
| L5 Cascades | Resolved | master:472-474; `1.7`, `2.6`, `2.8`, and `6.2` acceptance tombstone compliance records. |
| L6 Metrics | Resolved | `6.3` extends the catalog; relates to `1kg.9.2` and `b8o.5`. |

## New findings

### [HIGH] A queued widening can put back rows that a later narrowing removed, and the freshness check still passes
**What:**
- Every mutation bumps `authz_revision` (master:650-652).
- A narrowing deletes table-namespace rows and advances `projection_revision` in the same transaction (653-654).
- A widening "runs from the outbox" and "advances `projection_revision` when complete" (655-656).
- The only freshness condition for player reads is `projection_revision ≥ authz_revision` (796-798).
- Nothing requires the projector (`live_projector`, 673) to:
  - re-read eligibility when it runs;
  - lock against a concurrent narrowing;
  - compare revisions before it writes.

**Why:** a GM widens a field by mistake and immediately narrows it:
1. The GM widens field F to `campaign`. `authz_revision` becomes N+1 and a projector job J is queued.
2. The GM narrows F to `gm_only`. There are no rows to delete yet. Displays stop and both revisions advance to N+2.
3. J runs, using either its enqueue-time decision or an eligibility read taken before step 2 committed, and inserts F's rows.
4. Freshness now holds (N+2 ≥ N+2), so player reads see F.

Send-time eligibility checks may still block template displays. But these read the table namespace and receive the text before any send-time check or egress filter runs:
- the player-space resolver (`12.2`);
- `PlayerEvidence` inputs to player-facing models (`12.8`; §4.5 rule 3).

That is the "hide it after the model received it" pattern the requirements forbid.

A second problem: consent, attestation, and session changes bump `authz_revision` but are neither narrowing nor widening. Nothing defines how `projection_revision` catches up, so player reads can fail closed indefinitely. It is also undefined whether a completing job may lower `projection_revision`.

**Evidence:**
- master:648-656, 673, 796-798.
- `2.3` acceptance tests only "a tightening removes affected rows in the same transaction".
- `2.5` acceptance tests only "a revoke followed by a job started before the rebuild".
- TM-06's controls (threat:131) don't cover projector writes.
- Searching master, threat, and spec for `FOR UPDATE`, serializable, advisory, or compare-and-set finds nothing.

Confidence: Likely. The mechanism is unspecified, and a natural implementation leaks.

**Suggested correction:**
- Serialize projector writes with narrowing per campaign: lock the `campaign.authz_state` row `FOR UPDATE` in both the narrowing transaction and every projector transaction.
- Inside that lock, the projector re-reads eligibility and released versions, writes only rows that are eligible at the locked revision, and sets `projection_revision` to that revision, never lowering it.
- Define how consent, attestation, and session changes affect the two revisions.
- Add "widen, then narrow before the projector runs → no rows, no player-path hit" plus an interleaved-transaction test to `2.3` and `2.5`, §4.11 #10, and TM-06.

### [HIGH] The MVP's table-device side has no foundation: the consent page doesn't depend on the table client, and table-device indicators are deferred
**What:**
- **Consent page.** `3.2` builds "a consent page reached from the table link (enrolled participant or anonymous guest)". It is blocked only by `3.1` and merely relates to `1kg.7.4` (spec.ts:280-288).
  - `1kg.7.4` owns the `/t/<session-token>` route, the table shell, and audience proof (tracker scope).
  - The ADR makes the table client "its own HTML entry point and bundle" (ADR:540).
  - Guests hold only the session-scoped credential that route's fragment exchange produces (REVEAL-19, ADR:513).
  - `1kg.7.4` is not among the 16 MVP external blockers (delivery:263-271; validator output).
- **Indicators.** The plan makes table-device visibility a condition of capture:
  - "the microphone state is visible on every device at the table" (master:31);
  - P1: "on every connected table device" (master:96);
  - "Pause for everyone is on every device" (master:242);
  - TM-21: "persistent indicators on the capturer and every table device" (threat:156).

  The beads that deliver table-device state and pause are `9.3` and `9.4` (Phase 2), and neither is upstream of G1. Feature 3's acceptance covers only "the capturing device" (spec.ts:265-271), and the Phase 1 demo omits table devices (delivery:95).

**Why:**
- A relates-to edge doesn't block, so `3.2` becomes ready while no table client exists. The implementer either stalls or builds a second table-link page outside the Workbench's table-client security model, which delivery principle 3 forbids.
- The MVP schedule is understated by the `1kg.7.4` → `1kg.7.2` → `1kg.7.1` chain.
- The MVP would ship capture that is invisible on participants' devices, which the plan says never happens (hard requirement: microphone state always obvious).

**Evidence:** as above; closure analysis shows `9.3` and `9.4` are not in G1's closure. Confidence: Confirmed.

**Suggested correction:**
- Make `1kg.7.4` block `3.2`, or agree a minimal shared table-client shell with the Workbench owner and record it on `1kg.7.4`. Then regenerate delivery §5.2.
- Then either:
  - add a Phase 1 bead, required by G1, for a boolean capture-state chip and a withdraw/pause control on the table client (polling is enough for push-to-talk); or
  - amend master §1, P1, §3.2, §3.3, and TM-21 to state the Phase 1 indicator set (capturing device, chime, captured announcement, withdrawal from the consent page), and have `1.4.1` confirm it is sufficient.

### [HIGH] Slot usage contradicts accepted Workbench decisions that §2.4 says the plan complies with
**What:**
- **Group fan-out.** A group share "fans out to each member's participant slot" (master:561-562; §2.4 row 143; `1.2` design; `11.2`, `11.3`). The ADR says "A document is live in at most one slot" (ADR:476), and revealing a document that is live elsewhere *moves* it (REVEAL-7, ADR:500).
- **NPC fields in private slots.** The plan displays NPC fields to one participant's slot (master:334; Phase 4 demo, delivery:126; T08, master:1157). AUD-9 (ADR:565) offers a participant audience only for `audience: owner` types linked to that participant: "Every other type is table-only in v1". NG-22 (ADR:833) makes "a secret note to one player for any type but a character sheet" a non-goal.
- **Automatic displays.** X-2 (ADR:105) requires "an explicit GM action that names what is shown". Phase 5 mention-triggered first disclosures (master:754-759; `12.1`) and automatic rules cards (`12.3`) have no such per-display action, yet §2.4 records compliance (master:138).
- **Rules cards.** Rules cards in slots also conflict with NG-25; see the rules-card MEDIUM.

**Why:**
- The ADR requires that a change wanting a non-goal amend the record "rather than slip it in" (ADR §13 intro, 804-808).
- `1.2` blocks `1kg.7.1`, but its acceptance names only E-1, E-2, E-3, E-6, and E-10. It could close without amending AUD-9, NG-22, NG-25, or the one-slot invariant.
- `1kg.7.1` would then implement the ADR as written. Phase 4 would need a divergent projection — the turn-1 "two permission systems" risk.
- Narrowing is undefined for a document live in several slots:
  - REVEAL-6 has the canvas header "stop that document" (ADR:498);
  - REVEAL-22 clears a slot "only if it holds what the Stop names" (ADR:499).

  A panic Stop could leave other members' slots showing.

**Evidence:** lines above. Confidence: Confirmed.

**Suggested correction:**
- Add AUD-9, NG-20, NG-22, NG-25, X-2, and the ADR:476 invariant to the `1.2` acceptance and to master §2.4, as explicit amendments needing owner sign-off.
- Choose one:
  - fan-out as distinct per-recipient projections with a defined "stop all copies" semantic; or
  - no group fan-out in v1.
- Choose one:
  - amend AUD-9 and NG-22; or
  - limit Phase 4 participant-slot displays to linked character sheets.
- Record whether a GM-authored display rule counts as the X-2 action.

### [MEDIUM] `LSA-4.7` opens a player-display path that skips G4 and relies on eligibility enforcement it doesn't depend on
**What:**
- `4.7` (Phase 4, gate-exempt; spec.ts:372-380) requires in its acceptance that "ineligible fields cannot be confirmed for the chosen slot".
- Its dependency closure includes `1.2`, `1.12`, `2.2`, and `2.8`, but not:
  - `11.1`, the bead that makes the Workbench projection enforce eligibility (spec.ts:728);
  - `13.5` or `13.6`.
- The exemption cites `1kg.9.1`, whose acceptance tests Workbench authorization (tokens, masks, enumeration), not LSA eligibility classes.
- Conflicting statements elsewhere:
  - `13.5` claims to precede "any live-card share, player display, or portrait" (spec.ts:918-926);
  - delivery principle 2 says player-visible portraits require G4 (delivery:21-30);
  - master:1242-1244 cites `LSA-4.7` as the MVP-era sharing path.
- Nothing makes `2.1`, `2.2`, `11.1`, or `2.8` precede Workbench reveal's own enablement (gated by `1kg.9.1`). P10's "one eligibility model and one disclosure ledger" (master:105) therefore holds only from Phase 4, and default-deny would later change a reveal that has already shipped.

**Why:** either `4.7` re-implements eligibility checks (a second enforcement point), or its acceptance test can't pass. Either way, a portrait revealed through the hand-off reaches players before G4.

**Evidence:** closure of `4.7` from the inline analysis. Confidence: Confirmed.

**Suggested correction:**
- Block `4.7` on `11.1` and `13.5` and drop the exemption, or remove the eligibility criterion and mark the hand-off Workbench-only.
- In `1.2`, decide whether Workbench reveal enforces eligibility from its first release (for example, `2.1` and `2.2` block `1kg.7.2`) or ships mask-only with a migration plan.
- Fix master:1244.

### [MEDIUM] The database-role model covers player reads but not table-device writes, maintenance jobs, or the existing superuser connection
**What:**
- **Table-device writes.** In the roles table (master:669-675), `live_table_reader` writes nothing and `live_gm_reader` runs in GM handlers. No role covers the guest-reachable write endpoints:
  - consent and age attestation (`3.1`, `3.2`);
  - pause (`9.4`);
  - safety signals (`9.9`);
  - deletion requests (`6.2`).
- **Maintenance jobs.**
  - Retention, deletion, and crypto-shredding jobs (`6.2`, Phase 1) and async transcription (`10.8`, Phase 3) run across campaigns under forced, fail-closed RLS (`2.3`, Phase 1).
  - No role or settings contract exists for them.
  - The transaction helper arrives only in `2.5` (Phase 4, spec.ts:228-236).
- **Superuser connection.**
  - `FORCE ROW LEVEL SECURITY` does not bind superusers or `BYPASSRLS` roles.
  - The whole app uses one `DATABASE_URL` (`service/history.py:179-180`; `scripts/deploy.sh:150`).
  - In compose that user is `rag`, created as a superuser by `POSTGRES_USER` (`docker-compose.yml:17,56`).
  - Until `1.13` separates the player service, that connection string sits in the player path's process environment. An import-linter contract can't stop `os.environ` access.

**Why:** unassigned paths will fall back to the owner/superuser connection or the GM reader, giving guest-reachable handlers GM read privilege. RLS tests on compose would pass or fail for the wrong reason.

**Evidence:** lines above. Confidence: Confirmed for the role gaps and the compose superuser. Whether Cloud SQL's `postgres` user has `BYPASSRLS` needs confirmation.

**Suggested correction:**
- Add to `1.13` and master §4.4:
  - a table-device writer role (insert-only on consent, pause, signal, and deletion-request tables; no GM reads);
  - a maintenance role with a per-campaign iteration contract.
- Move the transaction helper into `2.3`.
- Require every runtime and test role to be `NOSUPERUSER NOBYPASSRLS`, with a test that asserts it.
- Inject connections by role and lint live modules against reading `DATABASE_URL`.

### [MEDIUM] Phase 1 adds recurring GCP spend under the $10 kill switch, but only `LSA-9.3` waits for `yje.6.6`
**What:**
- The cost model says:
  - "Phase 1 adds no recurring infrastructure" (cost:503-504);
  - "Any of the recurring costs above would trip it", including KMS and Scheduler (cost:495, 502);
  - the Cloud SQL production tier is "likely needed for RLS, outbox, and more tables" (cost:157) — Phase 1 work.
- Phase 1 beads add recurring items:
  - Cloud KMS keys (`6.1`);
  - scheduled retention jobs (`6.2`);
  - one Secret Manager secret per database role (`1.13`/`2.3`; master:681-682).
- `yje.6.6`'s own scope says the Cloud SQL instance alone costs "about $9.40 a month" of the $10 budget.

**Why:** with about $0.60 of monthly headroom, small new line items can trip the switch, which detaches billing and takes production offline (deploy-gcp.md:6-7, 174-199).

**Evidence:** `bd` text for `yje.6.6`. Confidence: Likely; it depends on actual monthly spend.

**Suggested correction:** make `yje.6.6` block G1 (or `6.1` and `2.3`), or record measured headroom and add a spend check to `13.8`'s deploy verification. Correct cost:504.

### [MEDIUM] Transcript segments are stored before the detectors that decide what must never be stored or sent to models
**What:**
- **Order.** The pipeline stores segments right after speech-to-text and runs the lexicon afterwards (master:913-914).
- **Rules that need earlier detection.**
  - T16: "surrounding span not stored and not sent to models" (1165);
  - T18 and T19: "not stored" (1167-1168);
  - §3.12 "Not stored" rows (467-468);
  - `6.5` acceptance: "never persisted";
  - `10.5` acceptance: "absent from stored transcripts and model inputs".
- **Extraction window.** Extraction sends the last 60–120 s of transcript (1003-1004). Speech preceding a safety phrase may already be stored and sent.
- **Unpriced classifiers.**
  - T17 needs recall ≥0.95 and a pause within 2 s p95 (`9.5`) from "Lexicon + classifier" (1166).
  - T18 and T19 need per-utterance classifiers.
  - None has a defined cadence or cost; the cost model prices only the debounced extractor (cost:204-217).

**Why:** as drawn, the pipeline cannot pass the `6.5` and `10.5` acceptance tests. About 300 per-utterance classifier calls per hour are unpriced.

Confidence: Confirmed (inconsistency).

**Suggested correction:**
- Run the lexicon and classifiers in memory before persistence and before assembling extraction windows.
- Define "surrounding span" in seconds, with a hold-back buffer or a purge that also covers queued extraction windows.
- Specify the classifier model and cadence, and add their cost to cost §5/§9.

### [MEDIUM] Guests cannot request transcript deletion after the session, although the plan promises they can
**What:**
- Guest consent attaches to the device's session credential (master:189-190).
- Guests "can request deletion of transcripts from sessions they consented to, from their device" (master:483-484; threat:317; TM-30).
- The table credential lasts only until End, Rotate, or expiry (REVEAL-19; REVEAL-2 caps a session at 12 h).
- Transcripts default to 7 days and may last up to 90.
- The `6.2` acceptance tests only a participant request.

**Why:** for most of the retention period a guest has no way to prove they consented, so L10's "every participant can obtain … deletion" fails for guests.

Confidence: Confirmed.

**Suggested correction:**
- Either issue a one-time deletion code at consent (stored hashed), or state that guest deletion works only during the session and route later requests to support.
- Add a guest case to `6.2`, and flag it for `1.4.4`.

### [MEDIUM] Rules cards on player slots contradict "guests never receive AI-generated content" and fit no slot representation
**What:**
- **"No AI content for guests"** is stated in master:781, TB7 (threat:97), and TM-18 (threat:148).
- **Places that give guests rules cards anyway:**
  - master:333 shows rules cards in the table slot, which guests see;
  - master:743 allows player-path LLM "public rules summaries";
  - T06, T07, and T22 `SLOT_RULE` deliver public rules cards;
  - `12.3` delivers automatic rules cards.
- **No representation.** Slots hold documents (ADR §7.1), and NG-25 (ADR:836) makes "saving … rules cards as documents" a non-goal. The eligibility and ledger schema (field path, version ID) cannot represent a rules card.
- **Entitlement.** Corpus entitlement is per account (`yje.4.6` acceptance), guests have no account, and `12.3` checks entitlement "per campaign".

**Why:** implementers can't satisfy both statements, and licensed rules text may reach anonymous guests.

Confidence: Confirmed.

**Suggested correction:** in `1.2`, decide:
- whether player rules cards are deterministic quotes or model summaries;
- how they are represented in slots and the ledger;
- whose entitlement governs display to guests, with `yje.6.1` licensing review.

Then align master:333, master:743, master:781, TB7, TM-18, and T06, T07, and T22.

### [MEDIUM] Phase 5 internal dogfood runs before the counsel review that must precede "any enablement"
**What:**
- `12.10` reviews automated display rules, mention-triggered disclosure, player recaps, and session-length listening notices "before any enablement" (spec.ts:861-868).
- `12.11` enables those features for staff for 30 days. It is blocked by `12.1`, `12.2`, `12.3`, `12.7`, `12.8`, and `12.9`, but not `12.10` (spec.ts:869-876; closure check).

**Why:** hours-long capture of staff and mention-triggered disclosure would run without counsel-reviewed notices, contrary to `12.10`'s own condition.

Confidence: Confirmed.

**Suggested correction:** make `12.10` block `12.11`, or narrow `12.10` to "before external enablement" with a recorded rationale.

### [MEDIUM] CSRF and Origin tests are claimed but appear in no acceptance criteria, including for guest-reachable consent writes
**What:**
- Threat §8 maps "CSWSH, CSRF, and token-binding tests" to `LSA-1.6`, `4.2`, and `9.1` (threat:838); TM-11 requires a CSRF token for state-changing HTTP (threat:141).
- `1.6` is a decision, and the `4.2` and `9.1` acceptance criteria contain no such tests.
- The table-device write endpoints have none either: `3.1`, `3.2`, `6.2`, `9.4`, `9.9`.
- Only `11.2` (Phase 4) tests a cross-site Origin.
- REVEAL-19 leaves the cookie mechanism (for example `SameSite=Strict`) to `1kg.1.3` and `1kg.1.4` as an example only.

**Why:** a forged guest "Yes" would undermine "consent is never generated automatically", and neither G1 nor G2 verifies it.

Confidence: Confirmed for the missing tests; exploitability depends on `1kg.1.3`.

**Suggested correction:** add Origin/CSRF rejection tests to the `3.1`, `4.2`, `6.2`, `9.1`, `9.4`, and `9.9` acceptance criteria and to G1/G2 evidence. Relate `3.1` to `1kg.1.3`.

### [MEDIUM] Player recaps let free-form text cross into a player-facing model, and the recap audience isn't fixed before drafting
**What:**
- §4.5 rule 1 says nothing free-form crosses into player space; only `(document_id, field_paths, slot)` does (master:735-739).
- `12.8` drafts recaps from "PlayerEvidence and GM-approved summary text" (spec.ts:845-852).
- §3.11 says "player-eligible inputs" without naming the audience (master:455-458).
- The `12.8` acceptance checks eligibility only at "publication".

**Why:**
- The approved text is an unbounded GM-space string fed to a player-path model.
- If drafting uses campaign-eligible evidence and the recap later goes to the table slot, facts ineligible for guests are removed only after generation, by the egress filter. The requirements instead demand filtering before any player-facing AI process.

Confidence: Confirmed (spec inconsistency).

**Suggested correction:**
- Fix the target slot before drafting.
- Build `PlayerEvidence` for that audience's eligibility.
- Route approved summary text through a document field with its own eligibility.
- Add a test that a campaign-only fact never enters a table-slot recap prompt.

### [LOW] Gap analysis and threat model keep statements that contradict the revised model
- gap:420-421 lists "GM attestation for non-account attendees" as consent; P2 says attestation is never consent.
- gap:429-430 ties age attestation to `yje.2.2` signup.
- gap:376-379 omits `UserMenu.tsx:86` (`role="alert"`) and `ChatPane.tsx:308` (`role="status"`) on `origin/master`.
- TM-13 (threat:143) cites `yje.2.2` and `yje.6.6` as controls. Neither is linked, and `yje.6.6` replaces the billing switch rather than being a product kill switch.

### [LOW] G4's acceptance names 12 of the 16 test families in master §4.11
The `13.5` acceptance (spec.ts:918-926) omits client-side fetch (13), egress-filter fault injection (15), and telemetry hygiene (16). The `11.6` acceptance has no remote-subresource test. `13.6` does cover "all" families, so gate coverage is transitive, but the list misleads reviewers. Reference "all 16 families in master §4.11".

### [LOW] Legal conclusions about self-hosted speech-to-text appear without the review flag
threat:336-338 says self-hosting "materially reduces AI-vendor eavesdropper exposure", and master:986-988 says it "materially changes the legal analysis". Both sit outside flagged §7. Meanwhile `1.4.1` asks counsel exactly this question, and L3 is rated [Medium], "may not remove risk". Flag both for professional review or rephrase them as hypotheses.

### [LOW] No one is named as holding the egress-filter fingerprints
Fingerprints derive from all text a recipient may not see, which includes GM-only text (master:760-767), yet the filter sits in player space (727-731). If the player path holds the fingerprint store and salt, it holds a guess-confirmation oracle over secrets. That contradicts "the player path has nothing secret to leak" (master:875-877). Keep fingerprints in a projector-owned or separate filter component that only returns allow or block; state this in `1.13` and `11.5`.

### [LOW] Smaller inconsistencies
- **Stale "sealed":** `5.4` says "new sealed NPC draft" (spec.ts:416) while its acceptance says drafts are unclassified. Feature 12 says "releasing sealed fields" (spec.ts:782; delivery:607). In the Workbench, "sealed" means an immutable version.
- **Concurrent windows:** cost:427-428 allows two windows per account, but a GM has one live table session across campaigns (ADR:487, REVEAL-2), and capture requires a session.
- **Decision ownership:**
  - "No signed URLs" (master:815) is a choice REVEAL-21 and ADR §16 (ADR:990) assign to `1kg.1.4`; `5.5` and `11.4` don't link it.
  - Rules-card payload schemas belong to `1kg.4.3` per the wire contract v1 "Card payloads" row; `4.6` doesn't link it.
- **VAD assets under CSP:** `vad-web` fetches its model and ONNX Runtime WASM (master:1091). No bead requires self-hosting these assets or a `script-src 'wasm-unsafe-eval'` allowance under `va8`'s CSP, so voice activity detection may silently fall back to the energy gate.
- **Missing revocation event:** table-link Rotate (REVEAL-17) is absent from master §4.9; guest consents bound to the rotated credential must lapse.
- **Trigger registry timing:** the registry (`10.1`) is Phase 3, but `4.5` (Phase 1) must discard unknown trigger codes.
- **Minimum speech:** 300 ms (master:289; `4.1`) vs 250–300 ms (master:940-941).
- **Terminology:** the copy "listening hours" (master:308) uses the word the Workbench reserves for playback (AUDIO-22), pending `1.8`.
- **Phase 4 blockers:** delivery:124 omits `1kg.7.3`, which blocks `4.7`.

## Verified as accurate (spot-checks)

**Tracker, validator, and generated tables**
- `bun validate.ts` reports 0 errors, and every count matches delivery §7:
  - 116 beads (1 epic, 13 features, 10 decisions, 92 tasks);
  - 101 leaves;
  - priorities 13/58/31/14;
  - phases 18/38/16/13/15/14 plus 2;
  - edges 240/39/4/42;
  - 16 MVP blockers.
- The ready leaves equal "Can start now".
- The live `bd` export is identical to `bd-all.json`. `1kg.1.1` is closed; `va8`, `yje.5.2`, `yje.6.6`, `1kg.2.3`, `1kg.7.4`, `1kg.7.5`, and `1kg.9.1` are open.
- The BEADS and cross-initiative tables are identical to the render and xdeps output; there are 55 external beads.
- R9 label lists and R8b exemptions match spec.
- Every automatic player-disclosure bead (`12.1`, `12.2`, `12.3`, `12.8`, `13.10`, `13.13`) has `13.5`, `13.6`, and `1.2` upstream.

**Cross-document consistency**
- Every `LSA-` reference in the five documents resolves, and checked references point at the right beads. The author's `checkdocs.ts` reports no problems.
- Threat §8 test families 1–16 match master §4.11; the TM "Tests" columns cite valid families.
- Gate acceptance items for G0–G3 and G5 are each backed by upstream beads.
- These numbers agree across master, threat, cost, and spec:
  - retention: 7-day transcript default, 90-day maximum, 30-day cards, uploads deleted within 24 h, 1-year audit;
  - push-to-talk: 30 s default, 60 s maximum;
  - pause: 1 s p50, 3 s p95;
  - windows: 5/15/30 min;
  - quiet period: 5 min;
  - allowance copy.

**Cost model**
- `costs.ts` output equals `cost-tables.md`, and the §16 formulas match the script.
- Recomputed:
  - listening hour $0.301;
  - push-to-talk clip $0.0021;
  - transcript-upload hour $0.060;
  - `gpt-4o-mini` extraction $0.058 uncached / $0.045 cached;
  - $25 upgrade increment +$6.29;
  - portfolio $16,500 revenue and $977 fees;
  - break-even 122/292/574 upgrades.
- Billing-plan shedding order, the $3 cap, and 73.9% at $15 match the billing plan.

**Code claims on `origin/master`**
- `service/`: `rag.py:44-46`; `graph.py:141-142`; `generate.py:198-205,209,233-256`; `app.py:335-361,396,504-523,578-619,659,731-732,741,827-830`; `gcp_logging.py:45-48`; `attachments.py:5,37`; `ratelimit.py:21`; `tracing.py:66-77`; `metrics.py:21`; `history.py:175-186`.
- `ui/`: `modes.ts:44-46`; `currentUser.tsx:21-24`; `useChat.ts:114,173`; `App.tsx:47,59`; `TopBar.tsx:28`; `ChatPane.tsx:331`; `Markdown.tsx:48-49`.
- Configuration, deployment, and docs: `config.py:167,230,235`; `README.md:18`; `Dockerfile.cloud:54`; `deploy.sh:150,152-155`; `deploy-gcp.md` (password-based `DATABASE_URL`, $10 kill switch).
- Service code is identical between `042b198` and `2ca91e6`; the diff touches only `ui/` and docs.

**Workbench record and wire contract**
- The plan's terms match the ADR:
  - participants are aliases with enrolled devices (AUD-1/3/4/5);
  - guests are anonymous (AUD-7, E-10);
  - a table slot plus a private `For you` slot (AUD-8, TABLE-14);
  - pinned versions (REVEAL-8), no stored wildcards (REVEAL-9), never-revealable metadata (REVEAL-10);
  - per-request `no-store` assets (REVEAL-21) and X-10.
- The ADR status is "accepted" with E-1 to E-10 awaiting owner confirmation, as the plan states.
- Wire contract v1 (PR #59) defines only errors, tool invocation, stat-block cards, and legacy guards; reveal, realtime, and document families are "to do". Nothing there contradicts the plan, and its "what never appears" rules agree with it.
- Threat §7 carries the blanket professional-review flag with confidence tags; §6.1 is flagged; counsel question 22 exists as master §3.2 cites.

## Not verified
- **External facts:** vendor and GCP prices, model lifecycle dates, vendor data-handling defaults, legal citations and case status, platform policies, and poll data.
- **Actual GCP spend:** the kill-switch headroom rests on `yje.6.6`'s ≈$9.40 statement.
- **Database and browser behavior:** whether Cloud SQL's `postgres` user has `BYPASSRLS`; `SET LOCAL` with pooling; `vad-web` asset loading under a strict CSP; browser enforcement of `Permissions-Policy`.
- **Workbench owner:** confirmation of E-1 to E-10, or willingness to amend AUD-9, NG-22, and NG-25.
- **Design archive:** files such as `RevealSheet.jsx` and `wire-schema.md` are not in the repository.

---

## Resolution (author response, applied after turn 2)

The forge-plan loop allows two review turns. These resolutions were checked
mechanically (validator, cost calculator, and document checker) and then by the
focused independent verification recorded below.

| Finding | Resolution |
| --- | --- |
| HIGH Projector race | Master plan §4.3 now defines a projector protocol: one `FOR UPDATE` lock per campaign shared by narrowing and projector transactions; the projector re-reads current eligibility and never trusts enqueue-time decisions; `projection_revision` never decreases; consent, attestation, announcement, and pause writes no longer advance `authz_revision`; membership additions advance both revisions together. Player reads check the revisions and read rows in one `REPEATABLE READ` snapshot (§4.7). `LSA-2.1`, `2.3`, and `2.5` acceptance, §4.11 family 10, and TM-06 add widen-then-narrow, interleaved-transaction, and monotonicity tests. |
| HIGH MVP table devices | `1kg.7.4` now blocks `LSA-3.2`; the consent page runs inside the Workbench table client. New Phase 1 bead `LSA-3.6` (required by G1) adds a capture chip, "Capturing now" by polling, **Pause for everyone**, and **Withdraw** on every table device, with E2E and cross-site tests. `LSA-9.3` replaces the polling with the table stream in Phase 2. `LSA-1.4.1` asks counsel whether the Phase 1 indicator set suffices. Delivery §5.2 lists 18 MVP blockers. |
| HIGH Workbench contradictions | New master plan §2.5 lists four amendment requests with fallbacks: participant-slot displays for any type (AUD-9, NG-22); group displays as per-recipient copies with stop-all-copies semantics (ADR §7.1, REVEAL-6/7/22, NG-20); GM-authored re-display rules as the X-2 action; deterministic rules excerpts as a slot content kind (NG-25). `LSA-1.2` acceptance requires an owner decision, amendment text, or recorded fallback for each. §2.4 no longer claims compliance where it did not hold. Mention-triggered first disclosure was removed entirely. |
| MEDIUM `LSA-4.7` | Now blocked by `LSA-11.1` and `LSA-13.5` and marked as a post-gate bead instead of gate-exempt. `LSA-1.2` decides whether Workbench reveal enforces eligibility from its first release. Master plan §7.1 corrected. |
| MEDIUM Database roles | Added `live_table_writer` (insert-only table-device writes) and `live_maintenance` (retention, deletion, crypto-shredding). All roles `NOSUPERUSER NOBYPASSRLS` with a startup check and test; connections injected per role; live modules never read `DATABASE_URL`; compose parity. The transaction helper moved to Phase 1 (`LSA-2.3`). |
| MEDIUM $10 kill switch | `yje.6.6` now blocks G1 as well as `LSA-9.3`; `LSA-13.8` deploy verification checks projected spend; cost model §1 and §13.4 corrected. |
| MEDIUM Storage order | Pipeline (§5.1, §5.6): lexicon in memory before persistence; a 20 s hold-back buffer with personal-data patterns and one classifier call per micro-batch before storage and extraction; safety phrases purge held text, delete the preceding 60 s, and cancel overlapping extraction. The classifier is priced (cost model §5): the expected listening hour rises from $0.30 to $0.32, and all persona, margin, portfolio, and break-even tables were regenerated. TM-34 and a residual risk record what cannot be recalled. |
| MEDIUM Guest deletion | One-time deletion codes shown at consent (hashed, single-use, rate-limited, uniform responses) with a support fallback; `LSA-3.2`, `6.2`, `1.4.4`, master plan §3.2 and §3.12, TM-30, and threat model §6.9 updated. |
| MEDIUM Rules cards | No player-path model text for rules. Player rules content is a deterministic excerpt in Phase 5, only if the NG-25 amendment is accepted; `LSA-12.3` closes as not planned otherwise. Guests never receive unreviewed AI output (master plan §4.6, TB7, TM-18). T06, T07, T08, and T22 realigned; licensing review linked through `yje.6.1`. |
| MEDIUM Dogfood before counsel | `LSA-12.10` now blocks `LSA-12.11`. |
| MEDIUM CSRF tests | Origin and CSRF rejection tests added to `LSA-3.1`, `3.6`, `4.2`, `6.2`, `9.1`, `9.4`, and `9.9` acceptance and to the G1 and G2 evidence; `LSA-3.1` relates to `1kg.1.3`; new TM-33. |
| MEDIUM Player recaps | Target slot fixed before drafting; `PlayerEvidence` built for that slot; summary text only as an eligible document field; GM approval before display; canary test that a campaign-only fact never enters a table-slot recap prompt (`LSA-12.8`, master plan §3.11). |
| LOW Gap analysis and TM-13 | Consent-record and age statements corrected; `UserMenu.tsx:86` and `ChatPane.tsx:308` added (verified on `origin/master`); TM-13 cites `yje.2.2` (now related to `LSA-8.2`) and `LSA-8.4`. |
| LOW G4 test families | `LSA-13.5` acceptance references all 16 families; `LSA-11.6` adds a remote-subresource test. |
| LOW Legal wording | Self-hosted STT statements in master plan §5.5 and threat model §6.10 rephrased as hypotheses flagged for professional legal review. |
| LOW Fingerprint custody | Fingerprints and salts held outside the player path by a filter that returns allow or block only (master plan §4.5 rule 7, `LSA-1.13`, `LSA-11.5`, TM-32). |
| LOW Smaller items | "Sealed" wording removed; one capture window per live session; portrait asset reads deferred to `1kg.1.4` with the signed-URL residual recorded; `LSA-4.6` relates to `1kg.4.3`; `LSA-9.1` requires self-hosted VAD assets under the production CSP and reports fallbacks; table-link rotation added to §4.9; `LSA-4.5` owns a closed Phase 1 trigger-code enum; minimum speech aligned at 300 ms; UI copy uses "capture hours" pending `LSA-1.8`; Phase 4 blockers list `1kg.7.3`. |

---

## Verification of turn-2 resolutions

Independent, read-only verification by the same model tier as the author. It
ran `bun validate.ts`, `bun costs.ts` (compared to `cost-tables.md`), and
`bun checkdocs.ts`, and checked the ADR on
`origin/docs/1kg.1.1-gm-workbench-decisions` and the live tracker (152 issues,
308 `blocks` edges, no drift). Line numbers refer to the revision it verified.

Verdict: **ISSUES FOUND — 1/4/6**

| Area | Result |
| --- | --- |
| 1. Projector race | The reported race is fixed: widen → narrow → projector converges on the narrowed state, and narrow → widen fails closed until the job runs. One new HIGH (display writes) and one MEDIUM (bookkeeping and liveness) remain. |
| 2. Table-device MVP | Verified: `1kg.7.4` blocks `3.2`; `3.6` is required by G1; master plan, delivery Phase 1, §5.2 (18 blockers), and TM-21 agree. |
| 3. Workbench amendments | Accurate against AUD-9, NG-20, NG-22, NG-25, X-2, ADR §7.1, and REVEAL-6/7/22; dependent sections condition on the amendments. One MEDIUM: rule-driven rules excerpts. |
| 4. Guests and rules content | Verified: "never unreviewed AI output" is consistent everywhere. |
| 5. Storage order and cost | Cost figures match `bun costs.ts` everywhere, and $30 → 70.2% holds. One MEDIUM: the hold-back did not cover the lexicon path, push-to-talk, or multiple instances. |
| 6. Roles | Partly verified. One MEDIUM: no single role could run a narrowing; Workbench narrowings, filter reads, deletion-code lookups, and withdrawals lacked roles. |
| 7. $10 kill switch | Verified; LOW on provisioning before G1. |
| 8. Deletion codes, CSRF, recaps, `12.10` → `12.11`, `4.7` | Verified; LOW on GM-side CSRF tests. |
| 9. Tooling and counts | Verified: validator, checker, generated tables, and counts all match. |

**Issues found:**

- **[HIGH] Display writes were outside the campaign lock.** Under READ
  COMMITTED, a display could commit while a narrowing was in flight, pass the
  send-time re-check, and never be stopped.
- **[MEDIUM] Freshness bookkeeping and liveness.** A narrowing set
  `projection_revision` even with widening work pending; a partial narrowing
  deleted rows still eligible to someone; embeddings computed under the lock
  could stall narrowings; failing jobs had no retry, dead-letter, or alert.
- **[MEDIUM] Roles.** No role could run a whole narrowing in one transaction;
  Workbench narrowings were not bound to the protocol; the filter could not read
  what it fingerprints; the deletion-code page and withdrawals had no fitting
  role.
- **[MEDIUM] Rule-driven rules excerpts** bypassed the X-2 row and its fallback
  (T06, T07, T22, §3.6, `12.3`, TM-10).
- **[MEDIUM] Hold-back coverage.** The lexicon path fed resolution and card
  composition before the hold-back; push-to-talk and multiple instances were
  unspecified.
- **[LOW-a]** The excerpt amendment also changes the ADR §7.1 slot model and
  REVEAL-5/9; the ledger had no corpus reference; `13.11`/`13.13` and delivery
  Phase 5 wording.
- **[LOW-b]** Safety-phrase "not stored" claims overstated the purge; the quiet
  period wording ignored continued STT; the T17 fallback classifier could not
  meet 2 s as a micro-batch.
- **[LOW-c]** Phase 1 pause latency, transport wording, `9.3` not blocked by
  `3.6`, no "state unknown" display, TM-23 bead list.
- **[LOW-d]** GM-side mutations (`4.4`, `4.6`, `5.4`) lacked CSRF tests.
- **[LOW-e]** Phase 1 resources could be provisioned before `yje.6.6`.
- **[LOW-f]** Rotation scope in `3.1`, guest capabilities in §2.2, the helper
  wording in threat §6.5, `2.1`'s mutation list, and `12.10` not blocked by
  `12.3`.

## Resolution of verification findings

| Finding | Resolution |
| --- | --- |
| HIGH Display writes | Master plan §4.3 rule 2: one campaign lock, always taken first — `FOR UPDATE` for authorization mutations and the projector, `FOR SHARE` for display writes (Share, Reveal, hand-off, rule re-displays, recap publication) and send-time re-checks — so a display either commits before a narrowing, which stops it, or runs after and is refused. Eligibility narrowing also advances the Workbench reveal epoch (REVEAL-22). Added to §4.6, §4.9, §4.11 family 10, TM-06, and `LSA-2.8`, `11.1`, `11.2` acceptance; `LSA-1.2` records the requirement on `1kg.2.2`, `1kg.2.3`, and `1kg.7.2`. |
| MEDIUM Bookkeeping and liveness | §4.3 rules 3–7: `projection_revision` advances only when nothing is pending; narrowing rewrites rows still eligible to someone in place; the projector computes embeddings before locking, drains every pending item under the lock, bounds lock time, and retries, dead-letters, and alerts; reads retry in a new snapshot, then fail closed with `projection_pending`; cache keys change when pending work completes. Tests added to `LSA-2.3`, `2.5`, and §4.11 family 10. |
| MEDIUM Roles | §4.4 adds `authz_writer`, `display_writer`, and `egress_filter`; `audit_writer` becomes an inherited insert-only group role so audit rows commit in the caller's transaction; consent is append-only events (withdrawal included); the deletion-code page executes one narrowly scoped function. `LSA-1.13` and threat §6.5 updated; `LSA-2.1` provides the helper Workbench mutations call, relates to `1kg.2.2`/`1kg.2.3`, and `LSA-11.1` tests those mutations. |
| MEDIUM Rules excerpts | §2.5 X-2 row covers excerpts; first excerpt displays always need approval; rule re-displays need the X-2 interpretation. T06, T07, T22, §3.6, TM-10, and `LSA-12.3` aligned; `12.3` is blocked by `12.1`. |
| MEDIUM Hold-back coverage | §5.1 and §5.6: nothing held feeds storage, extraction, cards, or any model call; lexicon candidates carry entity IDs only and are withdrawn on purge; push-to-talk is classified synchronously per clip; releases, extraction results, and purges take the capture-window row lock, and content-free purge markers drop held text on every instance. `LSA-6.5` and `10.5` acceptance add two-instance tests. |
| LOW-a | §2.5 cites the ADR §7.1 slot model and REVEAL-5/9 with NG-25; disclosure events reference a document field version or a corpus chunk and version (§4.2, §4.3); `13.11`, `13.13`, and delivery Phase 5 wording fixed, with the X-2 fallback stated. |
| LOW-b | §3.12, §3.13, T16, and threat §6.7 now say "purged" with the recall limit; the quiet period uses transcripts only in memory for detection; the T17 fallback is a synchronous per-utterance classifier, priced conditionally in cost model §5. |
| LOW-c | §3.2 states Phase 1 pause semantics (next clip blocked, 2 s poll) and Phase 2 latency; §5.2, §5.12, and gap analysis §2 mention Phase 1 polling; `9.3` blocked by `3.6`; `3.6` shows "state unknown" and keeps pause available; TM-23 lists `3.6`. |
| LOW-d | CSRF rejection tests added to `LSA-4.4`, `4.6`, `5.4`, `6.4`, `2.7`, and `11.3`. |
| LOW-e | `LSA-13.8` acceptance and cost model §13.4: the rollout runbook provisions live production resources only after `yje.6.6`. |
| LOW-f | `LSA-3.1`: guest consents lapse on rotation, participant consents on personal-link reset; §2.2 guest row lists deletion requests; threat §6.5 helper wording; `LSA-2.1` lists approved versions; `LSA-12.10` blocked by `12.3`. |

# The policy oracle

A test-only reference model of who may see which field of which document, when, and what was shown
afterwards. Bead `agent-forge-harness-1ir.1.12`. Import it as `service.tests.policy_oracle`.

## 1. What it is, and what it is not

The oracle is an **independent reference evaluator** of the shared eligibility ADR
(`docs/adr/shared-eligibility-display-disclosure.md`, bead `1ir.1.2`), read through its section 15.2
and the Workbench threat model section 15 (TA-5: players hold accounts, there are no guests, the table
slot shows `public` keys only, and seats are confirmed by the GM). It has five parts:

1. two pure functions: `eligible_for_audience` on identities and `entitled` on principals;
2. a deterministic state machine over atomic steps (`apply`, `run`), where every step yields the
   answer, the slots and disclosures after, the per-field ledger events, whether the reveal epoch and
   `authz_revision` advanced, and a lock footprint;
3. the case catalogue: TT-1 to TT-55 in both columns, a disposition for RC-1 to RC-16, and the
   precedence, idempotency, entitlement and critic cases;
4. seeded generators of campaigns, participants, characters, groups, slots and disclosure histories;
5. this README.

It is **not** production code, a production dependency, a database test, an adapter against
production, or a wire contract. It holds no field text: a value is a `Value` (`PRESENT`, `BLANK`,
`ABSENT`), so it can neither leak text nor pretend to inspect content (ED-22). It adds no dependency:
the generators take a `random.Random` built from fixed integer seeds (no Hypothesis).

Its consumers: `1ir.2.2` (the decision point must equal the oracle on at least 10,000 generated cases),
`1kg.7.2` (the Workbench v1 column is its regression suite), `1ir.11.1` (the Enforced column is its
acceptance suite) and `1ir.13.6` (the adversarial player-display suite builds on it).

## 2. The two functions

### `eligible_for_audience(world, document_id, key, audience) -> Eligibility`

ADR section 4, on identities, in order; the first match decides. It never reads the `enforced` flag:
the Confirm, `Export` and the narrowing scans evaluate it only when Enforced.

| Order | Check | Answer (`EligReason`) |
|---|---|---|
| 1 | a `ParticipantAudienceId` that is unknown or removed | no, `INACTIVE_PARTICIPANT` (even for a public key) |
| 2 | the key is off the current type version's revealable allowlist, or the document is unknown | no, `NOT_REVEALABLE` (an orphan row is ignored) |
| 3 | the class is the row, or unclassified | - |
| 4 | unclassified | no, `UNCLASSIFIED` |
| 5 | gm_only | no, `GM_ONLY` |
| 6 | public | yes, `PUBLIC` |
| 7 | the audience is the table (`TableAudienceId`) | no, `TABLE_NEEDS_PUBLIC` |
| 8 | campaign | yes, `CAMPAIGN` |
| 9 | `participants[ids]` | `NAMED` / `NOT_NAMED` |
| 10 | `characters[ids]` | linked now: `LINKED` / `NOT_LINKED` |
| 11 | `groups[ids]` | a member now: `MEMBER` / `NOT_MEMBER` |

Helpers: `stored_class`, `audience_of_slot`, `expand_audience`, `expand_all`, `default_draft`,
`unclassified_keys`, `live_exposure`, `is_widening`, `narrowest_widening`, `type_evolution_problems`.

### `table_principal(state, requester)` and `entitled(state, requester, slot) -> Entitlement`

Threat model section 15.2, in order. A `Requester` is what the server resolves from cookies: an
`account` and a `screen_grant`, never request fields.

| Order | `table_principal` check | `PrincipalKind` |
|---|---|---|
| 1 | a live screen grant (known, unrevoked, of the open session and its generation) decides alone | `SCREEN` |
| 2 | no account | `UNAUTHENTICATED` |
| 3 | no open (live and unexpired) session | `INACTIVE` |
| 4 | the owner | `OWNER` |
| 5 | an awaiting / a confirmed seat | `SEATED` / `SEATED_CONFIRMED` |
| 6 | anything else | `INACTIVE` |

`entitled` then answers `UNAUTHENTICATED` or `INACTIVE` for those principals; `ENTITLED` on the table
slot for every other principal; `ENTITLED` on a participant slot only for `SEATED_CONFIRMED` and its own
slot; and `ABSENT` for any other participant slot, exactly as an empty one. It never reads a class,
the flag or `authz_revision` (ED-25). Helpers: `seat_of_account`, `visible` (a `VisibleView` of
`SlotView` entries: `"table"`, and `"mine"` only for a confirmed seat's non-empty slot), `held`
(pending delivery), `asset_readable` (ED-19), `automation_may_redisplay` (I-16).

## 3. The machine

A `State` holds the `World` (`Participant` seats, `Document`s with `Version`s of a `FixtureType`'s
`TypeVersion`, groups, `FieldClass` rows, the switches, `authz_revision`), the `Session`s, the slots
(one `Copy` per `Slot`, `TABLE` or `participant_slot(id)`), the live `Disclosure`s, the
`ScreenGrant`s, the ledger (`FieldEvent`s), the answers recorded per command, and the ops in flight
(`InFlight`).

Ops (`ALL_OPS`; `ASSISTANT_ONLY` ones need `Release.ASSISTANT`, see `available`):

| Kind | Ops | Phases |
|---|---|---|
| display | `Confirm` (with `ClassifyEntry` items), `Stop`, `Export` | `COURTESY` then `COMMIT`; `ONLY` |
| fact-changing narrowings (`TWO_STEP_NARROWINGS`, and `Classify` when it narrows) | `Unlink`, `Relink`, `Archive`, `Delete`, `RemoveFromGroup`, `EnforcementOn`, `Classify` | `STEP1` then `STEP2` |
| revocations (`REVOCATIONS`) | `End`, `ExpireJob`, `Rotate`, `RemoveParticipant` | `STEP1` then `RECONCILE` zero or more times |
| the clock | `Expire` | `ONLY`: marks the session expired and writes nothing else |
| locked widenings | `Start` (two steps when an expired session must be finalised first), `AddParticipant`, `Offer`, `Decline`, `Accept`, `ConfirmSeat`, `Link`, `AddToGroup`, `EnforcementOff`, `SetAutomation`, `Unarchive`, and `Classify` when it widens | `ONLY` |
| documents and screens | `Edit`, `Seal`, `MintScreen`, `RevokeScreen`, `LeaveScreen` | `ONLY` |

A `Step` names its `op_id` (an `OpId`), its op (`Op`), its `Phase` and whether the campaign lock is
available. `phases(op, state)` is the solitary sequence; `next_op_id` gives a fresh id; `apply` runs one
step and returns a `StepResult`; `run` applies a sequence and returns a `Trace`; `run_op` runs one op
alone; `compose` builds a `Confirm` at the current session and epoch. A schedule is any interleaving
that keeps each op's own phase order. Misuse (an unavailable op, a phase out of order, a violated
precondition that is another bead's refusal surface) raises `OracleUsageError`; it is never an answer.

Answers (`Answer`): `Ok`, `Refused` (a `RefusalKind` with its status in `REFUSAL_STATUS`, and for a 422
the field keys at fault), `NotAppliedYet` (503: a step 2 without the lock), `RetryLater` (503: a
widening, Confirm or Enforced export without the lock), `JobRetry` (a reconcile without the lock) and
`Replayed` (the first answer to a command id seen before).

Epoch, `authz_revision` and footprints follow brief section 5.8 as amended by its critic (item 3): the
epoch advances on a Confirm, on a Stop in a state-live session, on every step 1 of a narrowing or
revocation in a state-live session that is not an idempotent no-op, and on a step 2 that changes the
fact in a state-live session (whether or not its rescan stops anything). `authz_revision` never
advances on a revocation's step 1. Footprints are `LockLevel`s in RQ-3's order (`LOCK_RANK`); a Stop
and every step 1 take no campaign lock. Events use `EventKind`, `StopReason` (ED-17's list, in order;
`NEVER_PRODUCED` holds the two superseded reasons), `SessionEndReason` and `Actor`. `ContentKind` has
one member.

## 4. Vocabulary map

| Oracle name | Record |
|---|---|
| `FieldKey`, `ID_PATTERN`, `check_id`, `fkey`, `fkeys` | ED-2 (flat keys); I-20 (every id follows the same grammar) |
| `ClassKind`, `FieldClass`, `LIST_KINDS` | ED-4 |
| `TypeVersion`, `FixtureType`, `FIXTURE_TYPES` | ED-5, ED-24, REVEAL-4, ADR section 5 |
| `SeatStatus`, `LIVE_SEATS`, `Participant` | wire `SeatStatus`, SEC-50, R-15 |
| `Slot`, `SlotKind`, `TABLE`, `participant_slot`, `slot_order` | ED-10, ED-15, I-19 |
| `TableAudienceId`, `ParticipantAudienceId`, `EligAudience` | ED-10 (identities) |
| `Table`, `Participants`, `Group`, `EveryoneSeated`, `RevealAudience` | ED-8, ED-15, TP-1 |
| `Copy`, `Disclosure`, `DisclosureId` | ED-15 (O-3) |
| `Session`, `SessionId`, `ScreenGrant`, `GrantId` | SEC-42, SEC-48 (D-13) |
| `Requester`, `AccountId`, `PrincipalKind`, `Entitlement` | threat model 15.2, 15.6; SEC-41, SEC-46 |
| `FieldEvent`, `EventKind`, `StopReason`, `SessionEndReason`, `Actor` | ED-17, ED-18 |
| `Eligibility`, `EligReason` | ADR section 4 |
| `RefusalKind`, `Refused`, `REFUSAL_STATUS` | ED-9, I-18 |
| `CommandId`, `Replayed` | the wire's *Idempotency* rows |
| `Release`, `World`, `State`, `Version`, `Document`, `DocumentId`, `GroupId`, `ParticipantId`, `Value` | I-4, I-6 |
| `frozen_map` | every mapping held in state is read-only |

## 5. Readings the ADR leaves open

Each is a strict reading of a decided record; none changes product scope.

- **I-7** The table slot is `public`-only (TP-1); `EveryoneSeated` expands at Confirm to active
  confirmed seats; an empty expansion is `AUDIENCE_INVALID`.
- **I-8** Each ADR commit unit is one atomic step; RC-6's stale read cannot be represented and is the
  reconciliation invariant P-19 instead.
- **I-9** The lattice (`LATTICE_SHAPES`, `LATTICE_WIDENS_TO`): bottom = {unclassified, gm_only} <=
  one list kind by subset <= campaign <= public. `old <= new` is a locked widening; anything else,
  including a cross-kind list change, is a narrowing whose step 1 advances the epoch even when not
  Enforced but stops nothing then.
- **I-10** A Confirm's classify entry must name the narrowest widening: `public` for the table, and
  `participants[P]` (or `participants[X | P]`) for participants; from any other class there is none.
  Critic 7(e): an entry for a key already eligible is refused too. An invalid audience expansion has
  no narrowest widening, so its entries are `CLASSIFY_INVALID` (both answers are 422s that write
  nothing). Critic 7(c): a classify list or a named group in v1 raises `OracleUsageError`.
- **I-11** A Confirm whose target slots equal the live disclosure's current slots is an Update (per-key
  `DISPLAYED`, `UPDATED` on a version change, `STOPPED(MASK_NARROWED)`); any other target ends the
  previous disclosure with `MOVED` and replaces other documents with `REPLACED`. An Update keeps the
  group the disclosure remembers unless the Confirm names a group (the stricter memory). Critic 7(g):
  every target slot's sequence advances.
- **I-12, reversed by critic item 5** An unlink or relink stops every copy of the sheet.
- **I-13, replaced by critic item 3** Step 2 of a fact change advances the epoch whenever a session is
  state-live; a step 2 whose fact already holds is `Ok` and nothing else, and is checked before the
  compare-and-set, so a concurrent change to exactly the target class answers `Ok`.
- **I-14, replaced by critic item 2** `Expire` writes the clock only. *Open* (live and unexpired)
  governs reads and the Confirm; *state-live* governs Stops and narrowings. `ExpireJob`, `End`,
  `Rotate` and `Start` finalise an expired session with `SESSION_ENDED(EXPIRED)`, revoking its screens;
  `RECONCILE` clears an expired session's slots without closing it. Finalisation and reconciliation
  events carry `Actor.SYSTEM`; every other event `Actor.GM`.
- **I-15** An unknown or removed participant, an unknown group, or no slot: `AUDIENCE_INVALID`.
- **I-16** `SetAutomation(on)` needs enforcement; `automation_may_redisplay` is a necessary condition
  only.
- **I-17** A Stop with no state-live session is `Ok` and changes nothing; with one it always advances
  the epoch. Critic 8: a Stop naming an unknown document is the route's 404 and is recorded under its
  command id; command ids are keyed by op kind, so a Stop reusing a Confirm's id is a fresh Stop.
  Whether a replay key is scoped by session (`1kg.7.1` ID-10) is **unsettled**; the generators reuse a
  Confirm's id only within one session.
- **I-21** The fixture set adds `oracle_notes` and `oracle_npc` v2 (`secret_ally`; `rumours` reserved).
- **Critic item 4** Delete needs an archived document (`DOCUMENT_NOT_ARCHIVED`, 409); `Unarchive` is a
  locked widening that never brings back a reveal. `DOCUMENT_DELETED` arises from the Enforced scan.
- **Critic item 6** `Classify(document, key, expected, new)` is compare-and-set at both steps; its
  checks run before its lock, and a no-op takes no lock (as every no-op here does).
- **Critic item 9** An export names the latest sealed version or a live copy's version; exporting an
  archived document is undecided (owner `1kg.6.6`) and raises `OracleUsageError`.
- **Critic item 10** `Edit` rewrites the one open version, or appends one after a sealed version.
- **Critic items 11 to 13** Screen minting, seat offers and links are other beads' refusal surfaces
  (preconditions here); a two-step op whose target a concurrent step removed answers `NOT_FOUND`, and
  a relink whose participant took another sheet `LINK_TAKEN`; step 1's stops always stand.
- **Critic item 15** The ASSISTANT machine describes the release after `1ir.11.1`: no Phase-1 consumer
  compares `Classify` steps.

## 6. How to add or change a case

**The ADR is amended first**, then the oracle, then the consumers. A case is a `Case` in `cases.py`
with an id, the rules it proves, a `Disposition` and two columns (a callable, or a `NoCase` naming the
ops a release lacks). Scenarios use the DSL: `K(col)` is the canonical state for a `Col`; `Sc` runs
ops (`confirm`, `run`, `begin`, `cont`); `P(x)` is a participant slot; `to(...)` a participant
audience; `keys`, `cls_entry`, `refused`, `set_class`, `set_seat`, `set_document`, `cls_of`, `ent`,
`slots_now`; events are built with `disp`, `upd`, `stp`, `ended`, `retracted` and `exported`, slots
with `sv`; `check` asserts the answer, slots, events and both advances; `expect`, `same`,
`raises_usage`, `no_authz`, `starts_exclusive` and `in_lock_order` assert the rest.
`test_catalogue_covers_every_adr_row` fails until a new TT or RC row in the ADR has a case. The
canonical world is `canonical_state` / `canonical_world` / `state_of` (named ids `OWNER`, `ANA_ID`,
`BEN_ID`, `CY_ID`, `ONDREY`, `ONDREY_SB`, `KIRA`, `MOSS`, `MARSH`, `NOTES1`, `SCOUTS`, `S1`, `SCREEN1`;
requesters `GM`, `ANA`, `BEN`, `CY`, `STRANGER`, `NOBODY`, `SCREEN`; types `ORACLE_NPC`,
`ORACLE_STATBLOCK`, `ORACLE_SHEET`, `ORACLE_NOTES` over `NPC_V1`, `NPC_V2`, `STATBLOCK_V1`,
`SHEET_V1`, `NOTES_V1`); deltas use `with_world` and `version_of`, and TT-31(c)'s state is
`ondrey_on_v2_with_v1_copy`. TT-50's candidate type is `npc_v3_declaring_rumours`; the superseded rows
assert `superseded_vocabulary_absent`.

The catalogue: `TRUTH_TABLE`, `RACE_CASES`, `ORDER_CASES`, `IDEMPOTENCY_CASES`, `EXTRA_CASES`,
`lattice_pairs`, and `ENTITLEMENT_MATRIX` over `ENT_PRINCIPALS` and `ENT_SESSIONS`, built by
`entitlement_state`, `entitlement_requester` and `entitlement_slot` (with the revoked grant
`DEAD_GRANT`, the previous-generation grant `STALE_GRANT` and the ended session's grant
`FOREIGN_GRANT`: none of them is live, so a request carrying only one is unauthenticated).

## 7. How a consumer uses it

Write an adapter that maps oracle ids to production rows and back, replay the **same `Step`s** against
production, build an `Observation` from production state, and compare it with `observe(result)` using
`diff`. Compare the status for 404, 409 and 503, and the kind and keys for a 422 (I-18); compare one
step's events as a multiset (I-19). `diff` takes `fields` (default `OBSERVATION_FIELDS`) for what the
consumer's production state can supply, and `compare_409_kinds`. `AnswerView`/`answer_view` and
`EventView` are what it compares; `canonical` renders ids, keys and enum names only.

- `1ir.2.2` supplies `answer`, `slots`, `disclosures`, `held`, `session`, `events`, `classes` and the
  lock mode; it also compares `eligible_for_audience` and `entitled` on `gen_eligibility_case` and
  `gen_entitlement_case` directly.
- `1kg.7.2` (v1) has no ledger, and `1kg.7.1`'s `ended_reason` vocabulary is not ED-17's, so it
  compares events only as far as ED-18(a)'s audit rows carry them and leaves `classes` out.

```python
# 1ir.2.2: the decision point equals the oracle on generated cases
for seed in range(500):
    rng = random.Random(seed)
    state = gen_world(rng, Release.ASSISTANT)
    campaign = adapter.load(state)  # write the world into the store
    for _ in range(20):
        doc, key, audience = gen_eligibility_case(rng, state)
        want = eligible_for_audience(state.world, doc, key, audience)
        got = decision_point.eligible(campaign, adapter.doc(doc), key, adapter.audience(audience))
        assert got.allowed == want.allowed, describe(state)
        requester, slot = gen_entitlement_case(rng, state)
        want_e = entitled(state, requester, slot)
        got_e = decision_point.entitled(campaign, adapter.principal(requester), adapter.slot(slot))
        assert adapter.entitlement(got_e) is want_e, describe(state)
```

```python
# 1kg.7.2: the reveal service replays the v1 column
fields = OBSERVATION_FIELDS - {"classes", "events"}
for seed in range(300):
    rng = random.Random(seed)
    state = gen_world(rng, Release.WORKBENCH_V1)
    campaign = adapter.load(state)
    for step in gen_schedule(rng, state, 12):
        result = apply(state, step)
        adapter.replay(campaign, step)  # the same Step, through the routes
        actual = adapter.observe(campaign, step)  # an Observation built from production
        problems = diff(observe(result), actual, fields=fields)
        assert not problems, (seed, describe(step), problems)
        state = result.after
```

## 8. How to reproduce a failure

Every property names its seed and a content-free `describe` of the state, step or trace. The release
comes from the seed as the property file's `_release` does: `WORKBENCH_V1` when `seed % 3 == 0`, else
`ASSISTANT`. A schedule property (seeds `0` to `2399` at scale 1) rebuilds its trace with:

```python
release = Release.WORKBENCH_V1 if seed % 3 == 0 else Release.ASSISTANT
rng = random.Random(seed)
state = gen_world(rng, release)
trace = run(state, gen_schedule(rng, state, rng.randint(*Bounds().schedule_length)))
```

The world corpus of the eligibility and entitlement properties uses seeds `10**6` to `10**6 + 499` (scale 1):
`state = gen_world(random.Random(seed), release)`, then the cases come from `random.Random(seed + 7)`
(`gen_eligibility_case`, 25 per world) and `random.Random(seed + 11)` (`gen_entitlement_case`, 25 per
world); P-24's requesters from `random.Random(seed + 13)`. B-5 alone picks its release by `seed % 2`
(`ASSISTANT` when odd).

The same seed gives a byte-identical `describe(trace)` in any process, whatever its string-hash seed
(P-23). `SCALE_ENV` names `POLICY_ORACLE_SCALE`: an integer of at least 1 that multiplies the corpus
for a deep local run (`scale` refuses anything else). `Bounds` sets the generator ranges;
`gen_requester` draws requesters.

## 9. What is not modelled, and whose it is

| Not modelled | Whose |
|---|---|
| routes, authentication, role and ownership checks | `1kg.7.2`, `1kg.7.4` |
| wire shapes and error codes | `1kg.1.2`, `1ir.11.1` |
| seat offers by address, decline and block, the throttle and the seat cap | `1kg.2.2` |
| campaign archive and deletion | `1kg.2.2`, `1kg.2.6` |
| a type migration op and the stop-scan when a key leaves an allowlist (the registry check is modelled) | `1kg.5.2`, `1ir.2.1` |
| the table namespace, the projector, `projection_revision`, freshness gating, version approval | `1ir.2.3`, `1ir.2.5` |
| the recipients snapshot | `1ir.2.8` |
| presence and "not connected" | `1kg.7.5` |
| automation rules, arming, triggers | Phase 5 (`1ir.12.*`) |
| group create, rename and remove (groups are fixtures) | `1ir.2.1` |
| exporting an archived document | `1kg.6.6` |
| the reference-excerpt slot | O-5, `yje.6.1` |
| waiting, timeouts, deadlocks, connection gates, row-lock modes, isolation | database tests of `1kg.7.2`, `1ir.11.1` (RC-7, 9, 10, 14) |
| text, `stale_text`, the REVEAL-8 notice, UI wording and warnings | `1kg.7.2`, `1kg.7.3`, `1ir.11.1` |
| the account identity ADR's Verified conjuncts | `yje.2.1`, a later amendment |

## 10. The independence rules

- The package imports only the standard library (`__future__`, `collections`, `collections.abc`,
  `dataclasses`, `enum`, `functools`, `hashlib`, `itertools`, `random`, `re`, `types`, `typing`, and
  `os` in `generate.py` only) and itself (B-1).
- Rules are written from the records, never ported from production code; every rule function cites its
  rows. Where the records are silent, stop and report.
- Production (`service/` outside its tests, `ingestion/` outside its tests, `config.py`) never imports
  it (B-2). The package ships in the service image's source tree as the existing tests do, but not in
  the wheel; B-2 is the control.
- Nothing imports it as a bare `policy_oracle` (B-3), no module in it is named like a test (B-6), every
  public name is in `__all__` and in this README (B-4), and every string in a generated state is an id
  or a key (B-5).
- Every dataclass is frozen and every mapping in state is a read-only copy; `apply` is pure.

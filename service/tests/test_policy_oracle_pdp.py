"""The decision point equals the policy oracle (bead 1ir.2.2, AC 1; brief section 10.3, P-A to P-G).

The oracle (`service/tests/policy_oracle/`, bead 1ir.1.12) is an independent reading of the same
records; `service/policy.py` was written from the records, not from it. P-A and P-B compare the two
on the oracle's own world corpus (seeds 10**6 on, the cases from `Random(seed + 7)` and
`Random(seed + 11)`, as its README section 8 says), P-C on the 15.6 matrix, P-D and P-E re-compute
every call the catalogue and the generated schedules make through the decision point (the shadow),
P-F checks the composed helpers, and P-G names truth-table anchors. `POLICY_ORACLE_SCALE` multiplies
the corpus locally, as the oracle's own tests do.
"""

from __future__ import annotations

import functools
import random
from dataclasses import replace

from service import policy
from service.policy import EligReason as R
from service.tests import pdp_fixtures as pdp
from service.tests.policy_oracle import (
    ENT_SESSIONS,
    ENTITLEMENT_MATRIX,
    EXTRA_CASES,
    IDEMPOTENCY_CASES,
    NPC_V2,
    ORDER_CASES,
    RACE_CASES,
    TABLE,
    TRUTH_TABLE,
    AccountId,
    Bounds,
    Col,
    DocumentId,
    EligAudience,
    FieldClass,
    FieldKey,
    GroupId,
    K,
    ParticipantAudienceId,
    ParticipantId,
    Release,
    Requester,
    SeatStatus,
    Slot,
    State,
    TableAudienceId,
    World,
    audience_of_slot,
    describe,
    eligible_for_audience,
    entitled,
    entitlement_requester,
    entitlement_slot,
    entitlement_state,
    frozen_map,
    gen_eligibility_case,
    gen_entitlement_case,
    gen_schedule,
    gen_world,
    participant_slot,
    run,
    scale,
    set_class,
    set_document,
    set_seat,
    table_principal,
    version_of,
    with_world,
)

SCALE = scale()
WORLDS = 500 * SCALE
CASES_PER_WORLD = 25
SCHEDULES = 2400 * SCALE


def _release(seed: int) -> Release:
    return Release.WORKBENCH_V1 if seed % 3 == 0 else Release.ASSISTANT


@functools.cache
def _worlds() -> tuple[tuple[int, State], ...]:
    return tuple((seed, gen_world(random.Random(seed), _release(seed))) for seed in range(10**6, 10**6 + WORLDS))


def _why(seed: int, state: State) -> str:
    return f"seed {seed}: {describe(state)[:3000]}"


def test_eligibility_equals_the_oracle_on_its_corpus() -> None:
    """P-A: `allowed` and `reason` equal on >= 12,500 cases; the reasons seen are the oracle's."""
    compared, pdp_reasons, oracle_reasons = 0, set[str](), set[str]()
    for seed, state in _worlds():
        facts = pdp.policy_facts(state.world)
        rng = random.Random(seed + 7)
        for _ in range(CASES_PER_WORLD):
            document, key, who = gen_eligibility_case(rng, state)
            want = eligible_for_audience(state.world, document, key, who)
            got = policy.eligible_for_audience(facts, pdp.doc(document), key, pdp.audience(who))
            assert (got.allowed, got.reason.name) == (want.allowed, want.reason.name), _why(seed, state)
            compared += 1
            pdp_reasons.add(got.reason.name)
            oracle_reasons.add(want.reason.name)
    assert compared >= 12_500 * SCALE
    assert pdp_reasons == oracle_reasons and len(pdp_reasons) >= 10, sorted(pdp_reasons)


def test_the_table_principal_and_entitlement_equal_the_oracle_on_its_corpus() -> None:
    """P-B: `decide_table`'s kind = `table_principal`, and `entitled` = `entitled`, on >= 12,500 cases."""
    compared, kinds, entitlements = 0, set[str](), set[str]()
    for seed, state in _worlds():
        rng = random.Random(seed + 11)
        for _ in range(CASES_PER_WORLD):
            requester, which = gen_entitlement_case(rng, state)
            result = pdp.outcome(state, requester)
            got_kind, got = pdp.kind_name(result), policy.entitled(result, pdp.slot(which))
            assert got_kind == table_principal(state, requester).name, _why(seed, state)
            assert got.name == entitled(state, requester, which).name, _why(seed, state)
            compared += 1
            kinds.add(got_kind)
            entitlements.add(got.name)
    assert compared >= 12_500 * SCALE
    assert kinds == set(pdp.KIND_NAMES.values()), sorted(kinds)
    assert entitlements == {e.name for e in policy.Entitlement}, sorted(entitlements)


def test_the_entitlement_matrix() -> None:
    """P-C: threat model 15.6's cells, principal by session state (T-6's principal half)."""
    states = {session: entitlement_state(session) for session in ENT_SESSIONS}
    for principal, session, which, want in ENTITLEMENT_MATRIX:
        state, requester = states[session], entitlement_requester(principal)
        got = policy.entitled(pdp.outcome(state, requester), pdp.slot(entitlement_slot(principal, which)))
        assert got.name == want.name, (principal, session, which, got.name)
    assert len(ENTITLEMENT_MATRIX) == 29 * len(ENT_SESSIONS)


def test_the_catalogue_under_the_shadow() -> None:
    """P-D: every catalogue case, both columns, with every `eligible_for_audience`, `table_principal`
    and `entitled` call re-computed by the decision point. The Enforced column is the acceptance
    (ADR M-7): on its own it compares every reason, every kind and every entitlement, and a table
    principal after each of End, Rotate, Expire, RevokeScreen, RemoveParticipant and ConfirmSeat."""
    catalogue = (*TRUTH_TABLE, *RACE_CASES, *ORDER_CASES, *IDEMPOTENCY_CASES, *EXTRA_CASES)
    assert {c.id for c in TRUTH_TABLE} >= {f"TT-{n}" for n in range(1, 56)}
    with pdp.shadow() as v1:
        for case in catalogue:
            case.v1()
    assert not v1.mismatches, v1.mismatches[:5]
    with pdp.shadow() as enforced:
        for case in catalogue:
            case.enforced()
    assert not enforced.mismatches, enforced.mismatches[:5]
    assert set(enforced.reasons) == {r.name for r in R}, enforced.reasons
    assert set(enforced.kinds) == set(pdp.KIND_NAMES.values()), enforced.kinds
    assert set(enforced.entitlements) == {e.name for e in policy.Entitlement}, enforced.entitlements
    assert set(enforced.after) >= set(pdp.TRANSITIONS), enforced.after  # critic item 11


def test_generated_schedules_under_the_shadow() -> None:
    """P-E: the oracle's generated schedules (seeds 0 to 2399), every Confirm, Export and scan decision
    re-computed through the decision point."""
    with pdp.shadow() as seen:
        for seed in range(SCHEDULES):
            rng = random.Random(seed)
            state = gen_world(rng, _release(seed))
            run(state, gen_schedule(rng, state, rng.randint(*Bounds().schedule_length)))
            assert not seen.mismatches, (seed, seen.mismatches[:3])
    compared = sum(seen.reasons.values())
    assert compared >= 10_000 * SCALE, compared


def _mask(rng: random.Random, state: State, document: DocumentId) -> list[str]:
    known = state.world.documents.get(document)
    keys = sorted(state.world.type_version(known).declared) if known is not None else ["name", "ghost"]
    return [rng.choice(keys) for _ in range(rng.randint(1, 5))]  # duplicates on purpose


def _audiences(rng: random.Random, state: State) -> list[EligAudience]:
    pids = sorted(state.world.participants) + [ParticipantId("p9")]
    out: list[EligAudience] = [TableAudienceId()] if rng.random() < 0.3 else []
    out += [ParticipantAudienceId(rng.choice(pids)) for _ in range(rng.randint(0 if out else 1, 3))]
    return out


def _refused(world: World, document: DocumentId, key: str, audiences: list[EligAudience] | list[Slot]) -> bool:
    who = [audience_of_slot(a) if isinstance(a, Slot) else a for a in audiences]
    return any(not eligible_for_audience(world, document, FieldKey(key), a).allowed for a in who)


def test_the_composed_helpers_equal_their_definition() -> None:
    """P-F: `ineligible_keys` and `ineligible_copies` equal a reference built from the ORACLE's
    `eligible_for_audience` (all or nothing, sorted, distinct), on >= 5,000 cases."""
    compared = 0
    for seed, state in _worlds():
        world, facts, rng = state.world, pdp.policy_facts(state.world), random.Random(seed + 17)
        documents = sorted(world.documents) + [DocumentId("d99")]
        for _ in range(8):
            document = rng.choice(documents)
            mask, audiences = _mask(rng, state, document), _audiences(rng, state)
            want = tuple(sorted({k for k in mask if _refused(world, document, k, audiences)}))
            got = policy.ineligible_keys(facts, pdp.doc(document), mask, [pdp.audience(a) for a in audiences])
            assert got == want, _why(seed, state)
            slots = {TABLE, *(participant_slot(rng.choice([*sorted(world.participants), "p9"])) for _ in range(2))}
            copies = {s: (rng.choice(documents), _mask(rng, state, rng.choice(documents))) for s in slots}
            want_slots = {pdp.slot(s) for s, (d, m) in copies.items() if any(_refused(world, d, k, [s]) for k in m)}
            got_slots = policy.ineligible_copies(facts, {pdp.slot(s): (pdp.doc(d), m) for s, (d, m) in copies.items()})
            assert got_slots == want_slots, _why(seed, state)
            compared += 2
    assert compared >= 5_000 * SCALE


# ---------------------------------------------------------------------------------- P-G anchors


def _facts(state: State) -> policy.PolicyFacts:
    return pdp.policy_facts(state.world)


def _is(state: State, document: str, key: str, who: str | None, allowed: bool, reason: R, row: str) -> None:
    got = policy.eligible_for_audience(_facts(state), pdp.doc(document), key, None if who is None else pdp.prt(who))
    assert (got.allowed, got.reason) == (allowed, reason), f"{row}: {got}"


def _bad(state: State, document: str, mask: tuple[str, ...], audiences: tuple[str | None, ...]) -> tuple[str, ...]:
    who = [None if a is None else pdp.prt(a) for a in audiences]
    return policy.ineligible_keys(_facts(state), pdp.doc(document), mask, who)


def _seat(state: State, pid: str, status: SeatStatus) -> State:
    return set_seat(state, replace(state.world.participants[ParticipantId(pid)], status=status))


def _document(state: State, document: str, **changes: object) -> State:
    return set_document(state, replace(state.world.documents[DocumentId(document)], **changes))  # type: ignore[arg-type]


def _scouts(state: State, *members: str) -> State:
    groups = frozen_map({GroupId("scouts"): frozenset(ParticipantId(m) for m in members)})
    return with_world(state, replace(state.world, groups=groups))


def test_truth_table_anchors() -> None:
    """P-G: named rows of the ADR's truth table in the canonical Enforced world, each asserting the
    decision point's own answer (the row is named in every failure message)."""
    k = K(Col.ENFORCED)
    assert _bad(k, "ondrey", ("name", "portrait"), (None,)) == (), "TT-1"
    assert _bad(k, "ondrey", ("name", "history"), (None,)) == ("history",), "TT-2"
    _is(k, "ondrey", "motives", None, False, R.GM_ONLY, "TT-3")
    _is(k, "ondrey_sb", "ac", None, False, R.GM_ONLY, "TT-44")
    _is(k, "ondrey", "notes", None, False, R.UNCLASSIFIED, "TT-4")
    _is(k, "ondrey", "history", "ana", True, R.CAMPAIGN, "TT-5")
    _is(k, "ondrey", "rumours", "ben", False, R.NOT_NAMED, "TT-6")
    for key in ("tags", "sources"):
        _is(k, "ondrey", key, "ana", False, R.NOT_REVEALABLE, "TT-7")
    _is(k, "ondrey", "hidden_identity", None, False, R.NOT_REVEALABLE, "TT-43")
    assert _bad(k, "kira", ("class", "name", "notes"), ("ana",)) == (), "TT-11"
    _is(k, "kira", "name", "ana", True, R.LINKED, "TT-11")
    _is(k, "kira", "name", "ben", False, R.NOT_LINKED, "TT-12")
    assert _bad(k, "kira", ("name", "class"), (None,)) == ("class", "name"), "TT-13"
    _is(k, "moss", "name", "cy", True, R.LINKED, "TT-15 an offered seat is an identity")
    assert pdp.outcome(k, Requester(AccountId("acct_cy"))) is policy.TableRefusal.INACTIVE, "TT-15 no standing"
    relinked = _document(k, "kira", linked_participant=ParticipantId("ben"))
    _is(relinked, "kira", "name", "ben", True, R.LINKED, "TT-16 eligible, not displayed")
    _is(relinked, "kira", "name", "ana", False, R.NOT_LINKED, "TT-45")
    removed = _seat(k, "ana", SeatStatus.REMOVED)
    _is(removed, "kira", "name", "ana", False, R.INACTIVE_PARTICIPANT, "TT-24")
    _is(removed, "ondrey", "name", "ana", False, R.INACTIVE_PARTICIPANT, "TT-24 even public")
    assert _bad(k, "ondrey", ("history",), ("ana", "ben")) == (), "TT-32"
    assert _bad(k, "ondrey", ("history", "rumours"), ("ana", "ben")) == ("rumours",), "TT-32 all or nothing"
    grouped = set_class(k, "ondrey", "notes", FieldClass.groups("scouts"))
    _is(_scouts(grouped, "ana"), "ondrey", "notes", "ben", False, R.NOT_MEMBER, "TT-34/TT-46")
    _is(_scouts(grouped, "ana", "ben", "cy"), "ondrey", "notes", "cy", True, R.MEMBER, "TT-35")
    assert _bad(k, "ondrey", ("name", "portrait", "history"), (None,)) == ("history",), "TT-38 export"
    copies = {None: ("ondrey", ("name", "notes")), "ana": ("ondrey", ("history",)), "ben": ("ondrey", ("rumours",))}
    got = policy.ineligible_copies(
        _facts(k), {None if s is None else pdp.prt(s): (pdp.doc(d), m) for s, (d, m) in copies.items()}
    )
    assert got == {None, pdp.prt("ben")}, "TT-41"
    who = (Requester(AccountId("acct_ana")), Requester(AccountId("acct_ben")), Requester(k.world.owner))
    before = [policy.entitled(pdp.outcome(k, r), pdp.slot(s)) for r in who for s in (TABLE, participant_slot("ana"))]
    classified = set_class(k, "ondrey", "notes", FieldClass.participants("ana"))
    after = [
        policy.entitled(pdp.outcome(classified, r), pdp.slot(s)) for r in who for s in (TABLE, participant_slot("ana"))
    ]
    assert before == after, "TT-47"
    on_v2 = _document(k, "ondrey", type_version=2, versions=(version_of(NPC_V2),))
    _is(on_v2, "ondrey", "rumours", "ana", False, R.NOT_REVEALABLE, "TT-50 an orphan row is never read")
    awaiting = _seat(k, "ben", SeatStatus.AWAITING_CONFIRMATION)
    ben = pdp.outcome(awaiting, Requester(AccountId("acct_ben")))
    assert policy.entitled(ben, pdp.prt("ben")) is policy.Entitlement.ABSENT, "TT-52"
    assert policy.entitled(ben, None) is policy.Entitlement.ENTITLED, "TT-52 the table"

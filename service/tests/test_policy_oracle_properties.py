"""Property tests of the policy oracle (bead `1ir.1.12`): P-1 to P-28 over seeded generated campaigns.

Every property runs over fixed integer seeds (I-3), names its seed on failure with a content-free
`describe`, and asserts a non-vacuity minimum: it counts the cases where its premise held, so no
property can pass over an empty selection. `POLICY_ORACLE_SCALE` multiplies the corpus (I-24).
"""

from __future__ import annotations

import functools
import os
import random
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import fields, replace
from pathlib import Path

import pytest

from service.tests.policy_oracle import (
    ALL_OPS,
    NEVER_PRODUCED,
    TABLE,
    TWO_STEP_NARROWINGS,
    Accept,
    AccountId,
    AddParticipant,
    AddToGroup,
    Archive,
    Bounds,
    Classify,
    ClassKind,
    Confirm,
    ConfirmSeat,
    DocumentId,
    EligAudience,
    End,
    EnforcementOff,
    EnforcementOn,
    Entitlement,
    EventKind,
    ExpireJob,
    Export,
    FieldClass,
    FieldKey,
    GrantId,
    Group,
    Link,
    LockLevel,
    Ok,
    Participant,
    ParticipantAudienceId,
    ParticipantId,
    Phase,
    PrincipalKind,
    RefusalKind,
    Refused,
    Release,
    RemoveFromGroup,
    RemoveParticipant,
    Replayed,
    Requester,
    Rotate,
    SeatStatus,
    SessionEndReason,
    Slot,
    Start,
    State,
    Step,
    StepResult,
    Stop,
    StopReason,
    Table,
    TableAudienceId,
    Trace,
    Unarchive,
    Unlink,
    World,
    apply,
    automation_may_redisplay,
    available,
    describe,
    eligible_for_audience,
    entitled,
    expand_audience,
    frozen_map,
    gen_eligibility_case,
    gen_entitlement_case,
    gen_requester,
    gen_schedule,
    gen_world,
    in_lock_order,
    is_widening,
    participant_slot,
    run,
    scale,
    table_principal,
    visible,
)

SCALE = scale()
WORLDS = 500 * SCALE
CASES_PER_WORLD = 25
SCHEDULES = 2400 * SCALE
ROOT = Path(__file__).resolve().parents[2]
V1 = Release.WORKBENCH_V1
AS = Release.ASSISTANT


def _release(seed: int) -> Release:
    return V1 if seed % 3 == 0 else AS


@functools.cache
def _schedules() -> tuple[tuple[int, Trace], ...]:
    out: list[tuple[int, Trace]] = []
    for seed in range(SCHEDULES):
        rng = random.Random(seed)
        state = gen_world(rng, _release(seed))
        steps = gen_schedule(rng, state, rng.randint(*Bounds().schedule_length))
        out.append((seed, run(state, steps)))
    return tuple(out)


def _results() -> Iterator[tuple[int, StepResult]]:
    for seed, trace in _schedules():
        for result in trace.results:
            yield seed, result


@functools.cache
def _worlds() -> tuple[tuple[int, State], ...]:
    return tuple((seed, gen_world(random.Random(seed), _release(seed))) for seed in range(10**6, 10**6 + WORLDS))


@functools.cache
def _eligibility_cases() -> tuple[tuple[int, State, tuple[DocumentId, FieldKey, EligAudience]], ...]:
    out: list[tuple[int, State, tuple[DocumentId, FieldKey, EligAudience]]] = []
    for seed, state in _worlds():
        rng = random.Random(seed + 7)
        out.extend((seed, state, gen_eligibility_case(rng, state)) for _ in range(CASES_PER_WORLD))
    return tuple(out)


@functools.cache
def _entitlement_cases() -> tuple[tuple[int, State, tuple[Requester, Slot]], ...]:
    out: list[tuple[int, State, tuple[Requester, Slot]]] = []
    for seed, state in _worlds():
        rng = random.Random(seed + 11)
        out.extend((seed, state, gen_entitlement_case(rng, state)) for _ in range(CASES_PER_WORLD))
    return tuple(out)


def _why(seed: int, obj: State | Step | Trace) -> str:
    return f"seed {seed}: {describe(obj)[:4000]}"


def _revealable(world: World, doc: DocumentId, key: FieldKey) -> bool:
    d = world.documents.get(doc)
    return d is not None and key in world.types[d.type_id].version(d.type_version).revealable


def _kind(world: World, doc: DocumentId, key: FieldKey) -> ClassKind:
    row = world.classes.get((doc, key))
    return ClassKind.UNCLASSIFIED if row is None else row.kind


def _audience(slot: Slot) -> EligAudience:
    return TableAudienceId() if slot.participant is None else ParticipantAudienceId(slot.participant)


def _inactive(world: World, audience: EligAudience) -> bool:
    if not isinstance(audience, ParticipantAudienceId):
        return False
    seat = world.participants.get(audience.participant)
    return seat is None or seat.status is SeatStatus.REMOVED


def _with(world: World, **changes: object) -> World:
    return replace(world, **changes)  # type: ignore[arg-type]  # justification: dataclasses.replace takes typed fields


def _live_grant(state: State, grant_id: GrantId | None) -> bool:
    grant = None if grant_id is None else state.grants.get(grant_id)
    session = None if state.live is None else state.sessions[state.live]
    return (
        grant is not None
        and not grant.revoked
        and session is not None
        and session.live
        and not session.expired
        and grant.session == session.id
        and grant.generation == session.generation
    )


def _requesters(state: State) -> list[Requester]:
    world = state.world
    accounts: list[AccountId | None] = [world.owner, AccountId("a_x"), None]
    for pid in sorted(world.participants):
        seat = world.participants[pid]
        accounts += [a for a in (seat.account, seat.offered_to) if a is not None]
    out = [Requester(a, None) for a in accounts]
    for gid in [*sorted(state.grants), GrantId("sg99")]:
        out.append(Requester(None, gid))
        out.append(Requester(accounts[-1], gid))
    return out


# ----------------------------------------------------------------------------- eligibility


def test_eligibility_case_count_meets_1ir_2_2() -> None:
    cases = _eligibility_cases()
    assert len({seed for seed, _, _ in cases}) >= 500
    assert len(cases) >= 10_000
    reasons = {eligible_for_audience(s.world, *case).reason for _, s, case in cases}
    assert len(reasons) >= 10


def test_entitlement_case_count_meets_1ir_2_2() -> None:
    cases = _entitlement_cases()
    assert len({seed for seed, _, _ in cases}) >= 500
    assert len(cases) >= 10_000
    answers = {entitled(s, *case) for _, s, case in cases}
    assert answers == set(Entitlement)


def test_p1_default_deny() -> None:
    premise = 0
    for seed, state, (doc, key, audience) in _eligibility_cases():
        world = state.world
        if (
            _inactive(world, audience)
            or not _revealable(world, doc, key)
            or _kind(world, doc, key) in (ClassKind.UNCLASSIFIED, ClassKind.GM_ONLY)
        ):
            premise += 1
            assert not eligible_for_audience(world, doc, key, audience).allowed, _why(seed, state)
    assert premise >= 2000


def test_p2_table_needs_public() -> None:
    premise = 0
    for seed, state, (doc, key, audience) in _eligibility_cases():
        if not isinstance(audience, TableAudienceId):
            continue
        premise += 1
        world = state.world
        expected = _revealable(world, doc, key) and _kind(world, doc, key) is ClassKind.PUBLIC
        assert eligible_for_audience(world, doc, key, audience).allowed is expected, _why(seed, state)
    assert premise >= 1000


def _moved_link(world: World, sheet: DocumentId, pid: ParticipantId) -> World:
    docs = dict(world.documents)
    for did in sorted(docs):
        if docs[did].linked_participant == pid:
            docs[did] = replace(docs[did], linked_participant=None)
    docs[sheet] = replace(docs[sheet], linked_participant=pid)
    return _with(world, documents=frozen_map(docs))


def _no_link(world: World, pid: ParticipantId) -> World:
    docs = {
        d: (replace(v, linked_participant=None) if v.linked_participant == pid else v)
        for d, v in world.documents.items()
    }
    return _with(world, documents=frozen_map(docs))


def test_p3_resolution_is_now() -> None:
    premise = 0
    for seed, state in _worlds():
        world = state.world
        for (doc, key), cls in sorted(world.classes.items()):
            if cls.kind not in (ClassKind.CHARACTERS, ClassKind.GROUPS) or not _revealable(world, doc, key):
                continue
            for pid in sorted(world.participants):
                if world.participants[pid].status is SeatStatus.REMOVED:
                    continue
                audience = ParticipantAudienceId(pid)
                if cls.kind is ClassKind.CHARACTERS:
                    sheets = [
                        DocumentId(s)
                        for s in sorted(cls.ids)
                        if s in world.documents
                        and world.types[world.documents[DocumentId(s)].type_id].audience == "owner"
                    ]
                    if not sheets:
                        continue
                    joined, left = _moved_link(world, sheets[0], pid), _no_link(world, pid)
                else:
                    groups = sorted(g for g in world.groups if g in cls.ids)
                    if not groups:
                        continue
                    grown = {g: (m | {pid} if g == groups[0] else m) for g, m in world.groups.items()}
                    shrunk = {g: m - {pid} for g, m in world.groups.items()}
                    joined, left = _with(world, groups=frozen_map(grown)), _with(world, groups=frozen_map(shrunk))
                assert joined.classes == world.classes == left.classes
                assert eligible_for_audience(joined, doc, key, audience).allowed, _why(seed, state)
                assert not eligible_for_audience(left, doc, key, audience).allowed, _why(seed, state)
                premise += 1
    assert premise >= 500


def _shapes(world: World) -> list[FieldClass]:
    pids = sorted(world.participants)
    sheets = sorted(d for d in world.documents if world.types[world.documents[d].type_id].audience == "owner")
    shapes = [FieldClass.unclassified(), FieldClass.gm_only(), FieldClass.campaign(), FieldClass.public()]
    shapes += [FieldClass.participants(p) for p in pids[:2]] + [FieldClass.participants(*pids)]
    if sheets:
        shapes.append(FieldClass.characters(sheets[0]))
    if world.groups:
        shapes.append(FieldClass.groups(sorted(world.groups)[0]))
    return shapes


def test_p4_lattice_monotonicity() -> None:
    premise = 0
    for seed, state, (doc, key, audience) in _eligibility_cases():
        world = state.world
        if world.release is not AS or not _revealable(world, doc, key):
            continue
        rng = random.Random(seed ^ hash_free(doc, key))
        old = world.classes.get((doc, key), FieldClass.unclassified())
        before = eligible_for_audience(world, doc, key, audience).allowed
        for new in rng.sample(_shapes(world), 3):
            classes = dict(world.classes)
            classes[(doc, key)] = new
            after = eligible_for_audience(_with(world, classes=frozen_map(classes)), doc, key, audience).allowed
            if is_widening(old, new):
                premise += 1
                assert after or not before, _why(seed, state)
            if is_widening(new, old):
                premise += 1
                assert before or not after, _why(seed, state)
    assert premise >= 1000


def hash_free(doc: str, key: str) -> int:
    """A process-independent integer from an id and a key (never Python's randomised `hash`)."""
    return sum((i + 1) * ord(ch) for i, ch in enumerate(f"{doc}:{key}"))


def _all_classes(world: World, cls: FieldClass) -> World:
    classes = {
        (d, k): cls
        for d in sorted(world.documents)
        for k in sorted(world.types[world.documents[d].type_id].version(world.documents[d].type_version).revealable)
    }
    return _with(world, classes=frozen_map(classes))


def _probe_states() -> list[tuple[int, State]]:
    """Per ASSISTANT schedule: its initial state, its final state, and up to three states in which a
    confirmed seat's own slot holds a copy (the states that can discriminate MT-12)."""
    out: list[tuple[int, State]] = []
    for seed, trace in _schedules():
        if _release(seed) is not AS:
            continue
        qualifying = [
            r.after
            for r in trace.results
            if any(
                s.participant is not None
                and s.participant in r.after.world.participants
                and r.after.world.participants[s.participant].status is SeatStatus.CONFIRMED
                for s in r.after.slots
            )
        ]
        picks = [trace.initial, trace.final, *qualifying[:: max(1, len(qualifying) // 3)][:3]]
        out.extend((seed, st) for st in picks)
    return out


def test_p5_entitlement_ignores_eligibility() -> None:
    discriminating = 0
    for seed, state in _probe_states():
        world = state.world
        variants = []
        for cls in (FieldClass.gm_only(), FieldClass.public()):
            changed = _all_classes(world, cls)
            flipped = not world.enforced
            changed = _with(
                changed,
                enforced=flipped,
                automation_enabled=flipped and world.automation_enabled,
                authz_revision=world.authz_revision + 7,
            )
            variants.append(replace(state, world=changed))
        slots = [TABLE] + [participant_slot(p) for p in sorted(world.participants)]
        requesters = _requesters(state)
        for variant in variants:
            for req in requesters:
                assert visible(variant, req) == visible(state, req), _why(seed, state)
                for slot in slots:
                    assert entitled(variant, req, slot) is entitled(state, req, slot), _why(seed, state)
        gm_only = variants[0].world
        for slot, copy in state.slots.items():
            seat = None if slot.participant is None else world.participants.get(slot.participant)
            if (
                seat is not None
                and seat.status is SeatStatus.CONFIRMED
                and state.live_session is not None
                and state.live_session.open
            ):
                if any(
                    not eligible_for_audience(gm_only, copy.document, k, _audience(slot)).allowed for k in copy.mask
                ):
                    discriminating += 1
    assert discriminating >= 200


def test_p6_eligibility_ignores_credentials() -> None:
    cycle = [
        SeatStatus.OPEN,
        SeatStatus.OFFERED,
        SeatStatus.NOT_ACCEPTED,
        SeatStatus.AWAITING_CONFIRMATION,
        SeatStatus.CONFIRMED,
    ]
    by_world: dict[int, list[tuple[DocumentId, FieldKey, EligAudience]]] = {}
    states = dict(_worlds())
    for seed, _, case in _eligibility_cases():
        by_world.setdefault(seed, []).append(case)
    premise = 0
    for seed in sorted(by_world):
        world = states[seed].world
        seats = {}
        for i, pid in enumerate(sorted(world.participants)):
            seat = world.participants[pid]
            if seat.status is SeatStatus.REMOVED:
                seats[pid] = seat
                continue
            status = cycle[(cycle.index(seat.status) + 1 + i) % len(cycle)]
            acct = AccountId(f"z{i}")
            if status is SeatStatus.OFFERED:
                seats[pid] = Participant(pid, status, None, acct)
            elif status in (SeatStatus.AWAITING_CONFIRMATION, SeatStatus.CONFIRMED):
                seats[pid] = Participant(pid, status, acct)
            else:
                seats[pid] = Participant(pid, status, None)
        other = _with(world, participants=frozen_map(seats))
        for doc, key, audience in by_world[seed]:
            premise += 1
            assert eligible_for_audience(other, doc, key, audience) == eligible_for_audience(
                world, doc, key, audience
            ), _why(seed, states[seed])
    assert premise >= 1000


# ----------------------------------------------------------------------------------- the machine


_UNCHANGED_ON_REFUSAL = tuple(f.name for f in fields(State) if f.name not in ("commands", "in_flight"))


def test_p7_non_ok_answers_change_nothing() -> None:
    premise = 0
    for seed, r in _results():
        if isinstance(r.answer, Ok):
            continue
        premise += 1
        for name in _UNCHANGED_ON_REFUSAL:
            assert getattr(r.after, name) == getattr(r.before, name), (name, _why(seed, r.step))
    assert premise >= 500


def test_p8_enforced_confirm_is_eligible_everywhere() -> None:
    premise = 0
    for seed, r in _results():
        op = r.step.op
        if not (isinstance(op, Confirm) and r.step.phase is Phase.COMMIT and isinstance(r.answer, Ok)):
            continue
        if not r.after.world.enforced:
            continue
        copies = [(s, c) for s, c in r.after.slots.items() if c.document == op.document_id]
        assert copies, _why(seed, r.step)
        for slot, copy in copies:
            for key in sorted(copy.mask):
                assert eligible_for_audience(r.after.world, copy.document, key, _audience(slot)).allowed, _why(
                    seed, r.step
                )
        premise += 1
    assert premise >= 300


def test_p9_one_live_disclosure_per_document() -> None:
    steps = 0
    for seed, r in _results():
        st = r.after
        steps += 1
        documents = [d.document for d in st.disclosures.values()]
        assert len(documents) == len(set(documents)), _why(seed, r.step)
        for did, disclosure in st.disclosures.items():
            holding = frozenset(s for s, c in st.slots.items() if c.disclosure == did)
            assert disclosure.slots == holding and holding, _why(seed, r.step)
            assert all(st.slots[s].document == disclosure.document for s in holding), _why(seed, r.step)
            kinds = {s.participant is None for s in holding}
            assert len(kinds) == 1, _why(seed, r.step)
        for slot, copy in st.slots.items():
            assert copy.disclosure in st.disclosures and slot in st.disclosures[copy.disclosure].slots, _why(
                seed, r.step
            )
    assert steps >= 5000


def test_p10_stop_is_never_refused_for_state() -> None:
    premise = 0
    for seed, r in _results():
        op = r.step.op
        if not isinstance(op, Stop) or isinstance(r.answer, Replayed):
            continue
        if op.target != "all" and op.target not in r.before.world.documents:
            assert r.answer == Refused(RefusalKind.NOT_FOUND), _why(seed, r.step)
            continue
        premise += 1
        assert isinstance(r.answer, Ok), _why(seed, r.step)
        left = [c for c in r.after.slots.values() if op.target == "all" or c.document == op.target]
        assert not left, _why(seed, r.step)
        assert r.epoch_advanced is (r.before.live is not None), _why(seed, r.step)
    assert premise >= 300


def _step1_is_noop(r: StepResult) -> bool:
    """Critic item 3's complete list of first-phase no-ops, from the before-state alone."""
    op, before = r.step.op, r.before
    world = before.world
    if isinstance(op, Unlink):
        return world.documents[op.sheet].linked_participant is None
    if isinstance(op, RemoveParticipant):
        return world.participants[op.participant].status is SeatStatus.REMOVED
    if isinstance(op, Archive):
        return world.documents[op.document_id].archived
    if isinstance(op, EnforcementOn):
        return world.enforced
    if isinstance(op, Classify):
        return world.classes.get((op.document_id, op.key), FieldClass.unclassified()) == op.new
    if isinstance(op, (End, Rotate)):
        return before.live is None
    if isinstance(op, ExpireJob):
        return before.live is None or not before.sessions[before.live].expired
    return False


def test_p11_every_step1_narrows() -> None:
    premise = 0
    for seed, r in _results():
        if r.step.phase is not Phase.STEP1:
            continue
        assert LockLevel.AUTHZ_SHARED not in r.footprint and LockLevel.AUTHZ_EXCLUSIVE not in r.footprint, _why(
            seed, r.step
        )
        if not isinstance(r.answer, Ok):
            continue
        noop = _step1_is_noop(r)
        if noop:
            assert not r.epoch_advanced, _why(seed, r.step)
        elif r.before.live is not None:
            premise += 1
            assert r.epoch_advanced, _why(seed, r.step)
    assert premise >= 300


def test_p12_enforced_invariant() -> None:
    premise = 0
    for seed, r in _results():
        st = r.after
        if not st.world.enforced or not st.slots:
            continue
        premise += 1
        for slot, copy in st.slots.items():
            for key in sorted(copy.mask):
                assert eligible_for_audience(st.world, copy.document, key, _audience(slot)).allowed, _why(seed, r.step)
    assert premise >= 500


def test_p13_a_held_copy_is_never_delivered() -> None:
    premise = 0
    for seed, r in _results():
        st = r.after
        world = st.world
        held_seats = [
            s.participant
            for s in st.slots
            if s.participant is not None
            and (
                world.participants.get(s.participant) is None
                or world.participants[s.participant].status is not SeatStatus.CONFIRMED
            )
        ]
        if held_seats:
            premise += 1
        confirmed = {
            p.account: p.id
            for p in world.participants.values()
            if p.status is SeatStatus.CONFIRMED and p.account is not None
        }
        for req in _requesters(st):
            view = visible(st, req)
            screen = _live_grant(st, req.screen_grant)
            for pid in sorted(world.participants):
                if entitled(st, req, participant_slot(pid)) is Entitlement.ENTITLED:
                    assert not screen and req.account is not None and confirmed.get(req.account) == pid, _why(
                        seed, r.step
                    )
            if "mine" in view:
                assert not screen and req.account in confirmed, _why(seed, r.step)
                assert req.account != world.owner, _why(seed, r.step)
                assert confirmed[req.account] not in held_seats, _why(seed, r.step)
    assert premise >= 500


def test_p14_no_fallback_to_the_table() -> None:
    premise = 0
    for seed, r in _results():
        op = r.step.op
        if not (isinstance(op, Confirm) and r.step.phase is Phase.COMMIT and isinstance(r.answer, Ok)):
            continue
        if isinstance(op.audience, Table):
            continue
        premise += 1
        was, now = r.before.slots.get(TABLE), r.after.slots.get(TABLE)
        moved_off = was is not None and was.document == op.document_id and now is None
        assert now == was or moved_off, _why(seed, r.step)
        if not moved_off:
            assert r.after.slot_seq.get(TABLE, 0) == r.before.slot_seq.get(TABLE, 0), _why(seed, r.step)
    assert premise >= 300


def test_p15_no_inference_across_slots() -> None:
    premise = 0
    for seed, r in _results():
        before, after = r.before, r.after
        written = {
            s
            for s in set(before.slot_seq) | set(after.slot_seq)
            if before.slot_seq.get(s, 0) != after.slot_seq.get(s, 0)
        }
        if not written:
            continue
        for req in _requesters(before):
            if table_principal(before, req) is not table_principal(after, req):
                continue
            if any(entitled(before, req, s) is Entitlement.ENTITLED for s in written):
                continue
            premise += 1
            assert visible(after, req) == visible(before, req), _why(seed, r.step)
    assert premise >= 500


_END_KINDS = (EventKind.STOPPED, EventKind.SESSION_ENDED, EventKind.RETRACTED)


def test_p16_ledger_is_well_formed() -> None:
    steps = 0
    for seed, r in _results():
        steps += 1
        seqs = [e.seq for e in r.after.history]
        assert seqs == list(range(1, len(seqs) + 1)), _why(seed, r.step)
        for e in r.events:
            assert e.ledgered is (r.after.world.release is AS), _why(seed, r.step)
            if e.kind is EventKind.STOPPED:
                assert isinstance(e.reason, StopReason) and e.reason not in NEVER_PRODUCED, _why(seed, r.step)
            elif e.kind is EventKind.SESSION_ENDED:
                assert isinstance(e.reason, SessionEndReason), _why(seed, r.step)
            else:
                assert e.reason is None, _why(seed, r.step)
            assert (e.slot is None) is (e.kind is EventKind.EXPORTED), _why(seed, r.step)
            if e.kind in _END_KINDS and e.slot is not None:
                was = r.before.slots.get(e.slot)
                assert was is not None and was.document == e.document and e.key in was.mask, _why(seed, r.step)
            if e.kind in (EventKind.DISPLAYED, EventKind.UPDATED) and e.slot is not None:
                now = r.after.slots.get(e.slot)
                assert now is not None and now.document == e.document and e.key in now.mask, _why(seed, r.step)
    assert steps >= 5000


_WIDENINGS_THAT_ADVANCE = (Start, AddParticipant, Accept, Link)


def _expects_authz(r: StepResult) -> bool:
    """Brief section 5.8 and critic item 3, as an explicit table over op, phase and fact."""
    op, phase = r.step.op, r.step.phase
    before, after = r.before.world, r.after.world
    if not isinstance(r.answer, Ok):
        return False
    if phase is Phase.RECONCILE:
        return True
    if isinstance(op, Start):
        return phase in (Phase.ONLY, Phase.STEP2)
    if phase is Phase.STEP2:
        return replace(before, authz_revision=0) != replace(after, authz_revision=0)
    if phase is Phase.COMMIT:
        return isinstance(op, Confirm) and bool(op.classify)
    if phase is not Phase.ONLY:
        return False
    if isinstance(op, _WIDENINGS_THAT_ADVANCE):
        return True
    if isinstance(op, ConfirmSeat):
        return before.participants[op.participant].status is SeatStatus.AWAITING_CONFIRMATION
    if isinstance(op, Classify):
        return before.classes.get((op.document_id, op.key), FieldClass.unclassified()) != op.new
    if isinstance(op, AddToGroup):
        return op.participant not in before.groups[op.group]
    if isinstance(op, EnforcementOff):
        return before.enforced
    if isinstance(op, Unarchive):
        return before.documents[op.document_id].archived
    return False


def test_p17_authz_revision_advances_exactly() -> None:
    steps = advanced = 0
    for seed, r in _results():
        steps += 1
        delta = r.after.world.authz_revision - r.before.world.authz_revision
        expected = _expects_authz(r)
        assert delta == (1 if expected else 0) and r.authz_advanced is expected, _why(seed, r.step)
        advanced += expected
    assert steps >= 5000 and advanced >= 500


def test_p18_footprints_follow_rq3() -> None:
    steps = 0
    for seed, r in _results():
        steps += 1
        assert in_lock_order(r.footprint), _why(seed, r.step)
        if isinstance(r.step.op, Stop):
            assert LockLevel.AUTHZ_SHARED not in r.footprint and LockLevel.AUTHZ_EXCLUSIVE not in r.footprint
    assert steps >= 5000


def test_p19_reconcile_is_idempotent_and_exact() -> None:
    premise = 0
    for seed, trace in _schedules():
        candidates = [r for r in trace.results if r.step.phase is Phase.RECONCILE and isinstance(r.answer, Ok)]
        final = trace.final
        pending = [oid for oid in sorted(final.in_flight) if Phase.RECONCILE in final.in_flight[oid].allowed]
        if pending:
            candidates.append(apply(final, Step(pending[0], final.in_flight[pending[0]].op, Phase.RECONCILE)))
        for r in candidates:
            premise += 1
            before, after = r.before, r.after
            session = after.live_session
            if session is not None and session.open:
                for slot in after.slots:
                    seat = None if slot.participant is None else after.world.participants.get(slot.participant)
                    assert slot.participant is None or (seat is not None and seat.status is not SeatStatus.REMOVED), (
                        _why(seed, r.step)
                    )
            opened = before.live_session is not None and before.live_session.open
            for slot in before.slots:
                if slot not in after.slots and opened:
                    seat = None if slot.participant is None else before.world.participants.get(slot.participant)
                    assert seat is not None and seat.status is SeatStatus.REMOVED, _why(seed, r.step)
            again = apply(after, Step(r.step.op_id, r.step.op, Phase.RECONCILE))
            assert not again.events and not again.epoch_advanced, _why(seed, r.step)
            for name in ("slots", "grants", "sessions", "disclosures", "history"):
                assert getattr(again.after, name) == getattr(after, name), _why(seed, r.step)
    assert premise >= 300


def test_p20_replays_change_nothing() -> None:
    premise = 0
    for seed, r in _results():
        op = r.step.op
        if isinstance(op, Confirm):
            key = ("confirm", op.command_id)
        elif isinstance(op, Stop):
            key = ("stop", op.command_id)
        else:
            continue
        if key not in r.before.commands:
            continue
        premise += 1
        assert r.answer == Replayed(r.before.commands[key]), _why(seed, r.step)
        assert not r.events and not r.epoch_advanced and not r.authz_advanced, _why(seed, r.step)
        for name in _UNCHANGED_ON_REFUSAL + ("commands",):
            assert getattr(r.after, name) == getattr(r.before, name), _why(seed, r.step)
    assert premise >= 200


def _necessary(state: State, doc: DocumentId, key: FieldKey, slot: Slot) -> bool:
    """The test's own backwards scan over the ledger (critic item 14): never calls the rule under test."""
    last = None
    gm_displayed = False
    for e in reversed(state.history):
        if not e.ledgered or e.document != doc or e.key != key or e.slot != slot:
            continue
        if last is None:
            last = e
        if e.kind is EventKind.DISPLAYED and e.actor.name == "GM":
            gm_displayed = True
    if last is None or not gm_displayed:
        return False
    return last.kind is EventKind.SESSION_ENDED or (
        last.kind is EventKind.STOPPED and last.reason in (StopReason.REPLACED, StopReason.MOVED)
    )


def test_p21_automation_needs_the_ledger() -> None:
    evaluated = allowed = 0
    for seed, r in _results():
        st = r.after
        if not (st.world.enforced and st.world.automation_enabled):
            continue
        triples = sorted(
            {(e.document, e.key, e.slot) for e in st.history if e.slot is not None},
            key=lambda t: (t[0], t[1], t[2].sort_key()),
        )
        triples += [(d, FieldKey("name"), TABLE) for d in sorted(st.world.documents)]
        for doc, key, slot in triples:
            evaluated += 1
            if automation_may_redisplay(st, doc, key, slot):
                allowed += 1
                assert _necessary(st, doc, key, slot), _why(seed, r.step)
                assert eligible_for_audience(st.world, doc, key, _audience(slot)).allowed, _why(seed, r.step)
    assert evaluated >= 300 and allowed >= 20


def test_p22_export_never_classifies() -> None:
    premise = 0
    for seed, r in _results():
        op = r.step.op
        if not isinstance(op, Export):
            continue
        premise += 1
        assert r.after.world.classes == r.before.world.classes, _why(seed, r.step)
        if isinstance(r.answer, Ok) and r.before.world.enforced:
            for key in op.keys:
                assert _kind(r.before.world, op.document_id, key) is ClassKind.PUBLIC, _why(seed, r.step)
                assert _revealable(r.before.world, op.document_id, key), _why(seed, r.step)
    assert premise >= 300


_TRACE_SCRIPT = """
import random, sys
from service.tests.policy_oracle import Release, describe, gen_schedule, gen_world, run
out = []
for seed in range(8):
    rng = random.Random(seed)
    state = gen_world(rng, Release.ASSISTANT if seed % 2 else Release.WORKBENCH_V1)
    out.append(describe(run(state, gen_schedule(rng, state, 12))))
sys.stdout.write(chr(10).join(out))
"""


def _trace_under(hash_seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONUTF8": "1"}
    done = subprocess.run(
        [sys.executable, "-c", _TRACE_SCRIPT],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    return done.stdout


def test_p23_same_seed_same_trace_in_any_process() -> None:
    first, second = _trace_under("0"), _trace_under("4242")
    assert len(first) > 1000
    assert first == second


_GRANT_SHAPES = ("live", "revoked", "old_generation", "ended_session", "expired_session", "unknown")


def _grant_shape(state: State, grant_id: GrantId) -> str:
    grant = state.grants.get(grant_id)
    if grant is None:
        return "unknown"
    if grant.revoked:
        return "revoked"
    session = state.sessions[grant.session]
    if not session.live:
        return "ended_session"
    if session.expired:
        return "expired_session"
    return "live" if grant.generation == session.generation else "old_generation"


def test_p24_generator_coverage() -> None:
    kinds: set[ClassKind] = set()
    seats: set[SeatStatus] = set()
    ops: dict[Release, set[type]] = {V1: set(), AS: set()}
    refusals: set[RefusalKind] = set()
    reasons: set[StopReason | SessionEndReason] = set()
    flags = dict.fromkeys(
        ("held", "group_memory", "orphan", "unsealed", "expired_with_copies", "mask_all", "classify_outside_mask"),
        False,
    )
    shapes: set[str] = set()
    for seed, state in _worlds():
        world = state.world
        kinds |= {c.kind for c in world.classes.values()}
        seats |= {p.status for p in world.participants.values()}
        flags["orphan"] |= any(not _revealable(world, d, k) for d, k in world.classes)
        flags["unsealed"] |= any(not d.latest.sealed for d in world.documents.values())
        rng = random.Random(seed + 13)
        for _ in range(4):
            req = gen_requester(rng, state)
            if req.screen_grant is not None:
                shapes.add(_grant_shape(state, req.screen_grant))
    for seed, r in _results():
        ops[_release(seed)].add(type(r.step.op))
        if isinstance(r.answer, Refused):
            refusals.add(r.answer.kind)
        reasons |= {e.reason for e in r.events if e.reason is not None}
        st = r.after
        flags["held"] |= any(
            s.participant is not None and st.world.participants[s.participant].status is not SeatStatus.CONFIRMED
            for s in st.slots
        )
        flags["group_memory"] |= any(d.group is not None for d in st.disclosures.values())
        flags["expired_with_copies"] |= bool(st.slots) and st.live_session is not None and st.live_session.expired
        if isinstance(r.step.op, Confirm):
            flags["mask_all"] |= FieldKey("all") in r.step.op.mask
            flags["classify_outside_mask"] |= any(e.key not in r.step.op.mask for e in r.step.op.classify)
        for gid in st.grants:
            shapes.add(_grant_shape(st, gid))
    assert kinds == set(ClassKind)
    assert seats == set(SeatStatus)
    for release in (V1, AS):
        assert ops[release] == {op for op in ALL_OPS if available(op, release)}, release.name
    assert refusals == set(RefusalKind)
    assert reasons == (set(StopReason) - NEVER_PRODUCED) | set(SessionEndReason)
    assert all(flags.values()), flags
    assert shapes == set(_GRANT_SHAPES)
    assert TWO_STEP_NARROWINGS <= ops[AS] and Group in {type(a) for a in _audiences()}


def _audiences() -> list[object]:
    return [
        r.step.op.audience
        for seed, r in _results()
        if isinstance(r.step.op, Confirm) and r.step.phase is Phase.COMMIT and isinstance(r.answer, Ok)
    ]


def test_p25_a_screen_grant_decides_only_while_live() -> None:
    """SEC-48, threat model 15.2 step 1: a grant is honoured on the table iff it is known, unrevoked, of the
    open session and of that session's generation. A grant of an old generation or of another session is
    ignored, so a request that carries only it is unauthenticated."""
    honoured = ignored = stale = foreign = 0
    for seed, r in _results():
        st = r.after
        session = st.open_session
        for gid in [*sorted(st.grants), GrantId("sg99")]:
            req = Requester(None, gid)
            live = _live_grant(st, gid)
            assert (table_principal(st, req) is PrincipalKind.SCREEN) is live, _why(seed, r.step)
            want = Entitlement.ENTITLED if live else Entitlement.UNAUTHENTICATED
            assert entitled(st, req, TABLE) is want, _why(seed, r.step)
            if live:
                honoured += 1
                continue
            ignored += 1
            grant = st.grants.get(gid)
            if grant is None or grant.revoked or session is None:
                continue
            stale += grant.session == session.id and grant.generation != session.generation
            foreign += grant.session != session.id and grant.generation == session.generation
    assert honoured >= 1000 and ignored >= 1000
    assert stale >= 100 and foreign >= 100, (stale, foreign)


def _ok_commit(r: StepResult) -> bool:
    return isinstance(r.step.op, Confirm) and r.step.phase is Phase.COMMIT and isinstance(r.answer, Ok)


def test_p26_a_confirm_classifies_only_masked_keys_it_needs() -> None:
    """ED-13(4), critic item 7(e): every entry of an Ok Confirm's classify list names a key of its mask that
    some target could not see before; the stored class becomes the entry's `new`. An entry for a key the
    mask leaves out is refused, never a silent widening of a field nobody is shown."""
    premise = outside = 0
    for seed, r in _results():
        op = r.step.op
        if not isinstance(op, Confirm) or r.step.phase is not Phase.COMMIT or not op.classify:
            continue
        outside += any(e.key not in op.mask for e in op.classify)
        if not isinstance(r.answer, Ok):
            continue
        premise += 1
        world = r.before.world
        slots = expand_audience(world, op.audience)
        assert slots, _why(seed, r.step)
        for e in op.classify:
            assert e.key in op.mask, _why(seed, r.step)
            seen = [eligible_for_audience(world, op.document_id, e.key, _audience(s)).allowed for s in slots]
            assert not all(seen), _why(seed, r.step)
            assert r.after.world.classes.get((op.document_id, e.key)) == e.new, _why(seed, r.step)
    assert premise >= 30 and outside >= 40, (premise, outside)


def test_p27_an_ok_confirm_commits_into_the_open_session() -> None:
    """ED-9, SEC-42, RC-5: the COMMIT re-reads the session row under its lock, so an Ok Confirm's session is
    the live one, unexpired, at the Confirm's epoch, whatever happened after its courtesy check."""
    premise = closed = 0
    for seed, r in _results():
        op = r.step.op
        if not isinstance(op, Confirm) or r.step.phase is not Phase.COMMIT:
            continue
        session = r.before.sessions.get(op.session_id)
        is_open = r.before.live == op.session_id and session is not None and session.open
        if not is_open:
            closed += 1
            assert not isinstance(r.answer, Ok), _why(seed, r.step)
            continue
        if isinstance(r.answer, Ok):
            premise += 1
            assert session is not None and session.epoch == op.epoch, _why(seed, r.step)
    assert premise >= 300 and closed >= 20, (premise, closed)


def test_p28_an_ok_confirm_writes_only_active_seats() -> None:
    """ED-15, I-15, I-23: after an Ok Confirm, every copy of its document sits in the table slot or in an
    active seat's slot; a named group reaches only its active members."""
    premise = grouped = 0
    for seed, r in _results():
        if not _ok_commit(r):
            continue
        op = r.step.op
        assert isinstance(op, Confirm)
        premise += 1
        world = r.before.world
        for slot, copy in r.after.slots.items():
            if copy.document != op.document_id or slot.participant is None:
                continue
            seat = world.participants.get(slot.participant)
            assert seat is not None and seat.active, _why(seed, r.step)
        if isinstance(op.audience, Group):
            grouped += any(not world.participants[p].active for p in world.groups[op.audience.id])
    assert premise >= 300 and grouped >= 10, (premise, grouped)


def test_corpus_meets_the_schedule_minimum() -> None:
    schedules = _schedules()
    lengths = [len(trace.results) for _, trace in schedules]
    assert len(schedules) >= 1000 and min(lengths) >= 1 and max(lengths) <= 16
    assert RemoveFromGroup in {type(r.step.op) for _, r in _results()}


def test_scale_env_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLICY_ORACLE_SCALE", raising=False)
    assert scale() == 1
    monkeypatch.setenv("POLICY_ORACLE_SCALE", "3")
    assert scale() == 3
    for bad in ("0", "-2", "x", "1.5", ""):
        monkeypatch.setenv("POLICY_ORACLE_SCALE", bad)
        with pytest.raises(ValueError, match="POLICY_ORACLE_SCALE"):
            scale()

"""The case catalogue: TT-1 to TT-55 in both columns, a disposition for RC-1 to RC-16, the precedence,
idempotency and entitlement cases, and the cases the brief's adopted critic added (brief section 7).

Every case is a pair of zero-argument callables, one per truth-table column, that raise
`AssertionError` with a content-free message when the oracle disagrees with the record. A `NoCase`
column asserts that the op the row needs does not exist in that release. The v1 column is the
regression suite of `1kg.7.2`; the Enforced column the acceptance suite of `1ir.2.2` and `1ir.11.1`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields, replace
from enum import Enum
from functools import partial
from typing import Final

from .fixtures import (
    ANA,
    ANA_ID,
    BEN,
    BEN_ID,
    CY,
    CY_ID,
    GM,
    KIRA,
    MARSH,
    MOSS,
    NOBODY,
    NOTES1,
    NPC_V1,
    NPC_V2,
    ONDREY,
    S1,
    SCOUTS,
    SCREEN,
    SCREEN1,
    SHEET_V1,
    STRANGER,
    canonical_state,
    ondrey_on_v2_with_v1_copy,
    version_of,
    with_world,
)
from .machine import (
    ALL_OPS,
    LOCK_RANK,
    Accept,
    AddParticipant,
    AddToGroup,
    Archive,
    Classify,
    ClassifyEntry,
    Confirm,
    ConfirmSeat,
    Delete,
    Edit,
    End,
    EnforcementOff,
    EnforcementOn,
    Expire,
    ExpireJob,
    Export,
    Link,
    LockLevel,
    Offer,
    Op,
    Phase,
    Relink,
    RemoveFromGroup,
    RemoveParticipant,
    RevokeScreen,
    Rotate,
    Seal,
    SetAutomation,
    Start,
    Step,
    StepResult,
    Stop,
    Unarchive,
    Unlink,
    available,
    canonical,
    compose,
    next_op_id,
    observe,
    phases,
    run_op,
)
from .machine import apply as apply_step
from .model import (
    NEVER_PRODUCED,
    TABLE,
    AccountId,
    Answer,
    CommandId,
    ContentKind,
    Copy,
    Document,
    DocumentId,
    EligReason,
    Entitlement,
    EventKind,
    EveryoneSeated,
    FieldClass,
    FieldKey,
    GrantId,
    Group,
    NotAppliedYet,
    Ok,
    OpId,
    OracleUsageError,
    Participant,
    ParticipantAudienceId,
    ParticipantId,
    Participants,
    PrincipalKind,
    RefusalKind,
    Refused,
    Release,
    Replayed,
    Requester,
    RevealAudience,
    ScreenGrant,
    SeatStatus,
    Session,
    SessionEndReason,
    SessionId,
    Slot,
    State,
    StopReason,
    Table,
    TableAudienceId,
    TypeVersion,
    Value,
    fkey,
    fkeys,
    frozen_map,
    participant_slot,
)
from .rules import (
    asset_readable,
    automation_may_redisplay,
    default_draft,
    eligible_for_audience,
    entitled,
    expand_all,
    expand_audience,
    held,
    is_widening,
    live_exposure,
    table_principal,
    type_evolution_problems,
    unclassified_keys,
    visible,
)

V1: Final = Release.WORKBENCH_V1
AS: Final = Release.ASSISTANT


class Col(Enum):
    """A truth-table column: Workbench v1, or Enforced (ASSISTANT with the campaign's flag, I-6)."""

    V1 = "v1"
    ENFORCED = "enforced"


class Disposition(Enum):
    MODELLED = "modelled"
    ACCOUNT_FORM = "account_form"
    INVARIANT = "invariant"
    FOOTPRINT = "footprint"
    SUPERSEDED = "superseded"
    NOT_MODELLABLE = "not_modellable"


def expect(condition: bool, what: str) -> None:
    """Raise `AssertionError` naming only what failed (it works under `python -O` too)."""
    if not condition:
        raise AssertionError(what)


def same(actual: object, expected: object, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: expected {canonical(expected)} actual {canonical(actual)}")


def raises_usage(probe: Callable[[], object], what: str) -> None:
    try:
        probe()
    except OracleUsageError:
        return
    raise AssertionError(f"{what}: expected OracleUsageError")


@dataclass(frozen=True)
class NoCase:
    """A column whose op does not exist in its release: `available` is False for `ops`, and `probe`,
    if given, raises `OracleUsageError`."""

    ops: tuple[type, ...]
    release: Release = V1
    probe: Callable[[], object] | None = None

    def __call__(self) -> None:
        for op in self.ops:
            expect(not available(op, self.release), f"{op.__name__} must be unavailable in {self.release.name}")
        if self.probe is not None:
            raises_usage(self.probe, "the column's op")


Column = Callable[[], None]


@dataclass(frozen=True)
class Case:
    """One catalogue row (brief section 7)."""

    id: str
    title: str
    rules: tuple[str, ...]
    disposition: Disposition
    v1: Column | NoCase
    enforced: Column | NoCase
    not_modelled: tuple[str, ...] = ()
    supersession: str | None = None
    owner: str | None = None
    successor: str | None = None


# ------------------------------------------------------------------------------------ scenario DSL


def K(col: Col, *, enforced: bool = True, automation: bool = False) -> State:  # noqa: N802
    """The canonical state for a column (brief section 7's `K`)."""
    if col is Col.V1:
        return canonical_state(V1)
    return canonical_state(AS, enforced=enforced, automation=automation)


def P(pid: str) -> Slot:  # noqa: N802
    """`P(x)`: participant `x`'s slot."""
    return participant_slot(pid)


def keys(*names: str) -> frozenset[FieldKey]:
    return fkeys(*names)


def to(*pids: str) -> Participants:
    return Participants(frozenset(ParticipantId(p) for p in pids))


class Sc:
    """A running scenario: the current state and a command-id counter. A test helper only."""

    def __init__(self, state: State) -> None:
        self.state = state
        self.count = 0

    def cid(self) -> str:
        self.count += 1
        return f"c{self.count}"

    def compose(
        self,
        doc: str,
        names: Iterable[str],
        audience: RevealAudience,
        classify: Iterable[ClassifyEntry] = (),
        *,
        version: int = 1,
        cid: str | None = None,
    ) -> Confirm:
        return compose(self.state, cid or self.cid(), doc, sorted(names), audience, tuple(classify), version=version)

    def run(self, op: Op, *, lock: bool = True) -> tuple[StepResult, ...]:
        trace = run_op(self.state, op, lock_available=lock)
        self.state = trace.final
        return trace.results

    def last(self, op: Op, *, lock: bool = True) -> StepResult:
        return self.run(op, lock=lock)[-1]

    def confirm(
        self,
        doc: str,
        names: Iterable[str],
        audience: RevealAudience,
        classify: Iterable[ClassifyEntry] = (),
        *,
        version: int = 1,
    ) -> StepResult:
        return self.last(self.compose(doc, names, audience, classify, version=version))

    def begin(self, op: Op, *, lock: bool = True) -> tuple[OpId, StepResult]:
        """Run only the op's first phase."""
        op_id = next_op_id(self.state)
        result = apply_step(self.state, Step(op_id, op, phases(op, self.state)[0], lock))
        self.state = result.after
        return op_id, result

    def cont(self, op_id: OpId, phase: Phase, *, lock: bool = True) -> StepResult:
        result = apply_step(self.state, Step(op_id, self.state.in_flight[op_id].op, phase, lock))
        self.state = result.after
        return result


Ev = tuple[EventKind, StopReason | SessionEndReason | None, str, str, Slot | None]


def _evs(
    kind: EventKind, reason: StopReason | SessionEndReason | None, doc: str, names: str, slot: Slot | None
) -> list[Ev]:
    return [(kind, reason, doc, k, slot) for k in names.split()]


def disp(doc: str, names: str, slot: Slot) -> list[Ev]:
    return _evs(EventKind.DISPLAYED, None, doc, names, slot)


def upd(doc: str, names: str, slot: Slot) -> list[Ev]:
    return _evs(EventKind.UPDATED, None, doc, names, slot)


def stp(reason: StopReason, doc: str, names: str, slot: Slot) -> list[Ev]:
    return _evs(EventKind.STOPPED, reason, doc, names, slot)


def ended(reason: SessionEndReason, doc: str, names: str, slot: Slot) -> list[Ev]:
    return _evs(EventKind.SESSION_ENDED, reason, doc, names, slot)


def retracted(doc: str, names: str, slot: Slot) -> list[Ev]:
    return _evs(EventKind.RETRACTED, None, doc, names, slot)


def exported(doc: str, names: str) -> list[Ev]:
    return _evs(EventKind.EXPORTED, None, doc, names, None)


SlotSpec = tuple[str, int, tuple[str, ...]]


def sv(doc: str, names: str, version: int = 1) -> SlotSpec:
    return (doc, version, tuple(sorted(names.split())))


def slots_now(state: State) -> dict[Slot, SlotSpec]:
    return {s: (c.document, c.version, tuple(sorted(c.mask))) for s, c in state.slots.items()}


def check(
    r: StepResult,
    *,
    answer: Answer,
    slots: Mapping[Slot, SlotSpec],
    events: Iterable[Ev],
    epoch: bool,
    authz: bool,
    what: str = "",
) -> None:
    """Every case asserts the answer, the slots after, the events, and both advances (brief section 7)."""
    same(r.answer, answer, f"{what} answer")
    same(slots_now(r.after), dict(slots), f"{what} slots")
    got_events = sorted(canonical((e.kind, e.reason, e.document, e.key, e.slot)) for e in r.events)
    want_events = sorted(canonical(e) for e in events)
    same(got_events, want_events, f"{what} events")
    same(r.epoch_advanced, epoch, f"{what} epoch advanced")
    same(r.authz_advanced, authz, f"{what} authz_revision advanced")
    ledgered = r.after.world.release is AS
    expect(all(e.ledgered == ledgered for e in r.events), f"{what} ledgered flag")


def no_authz(r: StepResult, what: str) -> None:
    expect(
        LockLevel.AUTHZ_SHARED not in r.footprint and LockLevel.AUTHZ_EXCLUSIVE not in r.footprint,
        f"{what}: no campaign lock",
    )


def starts_exclusive(r: StepResult, what: str) -> None:
    expect(bool(r.footprint) and r.footprint[0] is LockLevel.AUTHZ_EXCLUSIVE, f"{what}: exclusive lock first")


def in_lock_order(footprint: tuple[LockLevel, ...]) -> bool:
    """RQ-3: levels in order, never back up; at most one campaign lock, and it comes first."""
    ranks = [LOCK_RANK[level] for level in footprint]
    return all(a < b for a, b in zip(ranks, ranks[1:], strict=False))


def refused(kind: RefusalKind, *names: str) -> Refused:
    return Refused(kind, tuple(FieldKey(n) for n in names))


def cls_entry(key: str, displayed: FieldClass, new: FieldClass) -> ClassifyEntry:
    return ClassifyEntry(fkey(key), displayed, new)


U: Final = FieldClass.unclassified()
PUB: Final = FieldClass.public()
GMO: Final = FieldClass.gm_only()
CAMP: Final = FieldClass.campaign()


def set_class(state: State, doc: str, key: str, cls: FieldClass) -> State:
    classes = dict(state.world.classes)
    classes[(DocumentId(doc), fkey(key))] = cls
    return with_world(state, replace(state.world, classes=frozen_map(classes)))


def set_seat(state: State, seat: Participant) -> State:
    seats = dict(state.world.participants)
    seats[seat.id] = seat
    return with_world(state, replace(state.world, participants=frozen_map(seats)))


def set_document(state: State, doc: Document) -> State:
    docs = dict(state.world.documents)
    docs[doc.id] = doc
    return with_world(state, replace(state.world, documents=frozen_map(docs)))


def cls_of(state: State, doc: str, key: str) -> FieldClass:
    return state.world.classes.get((DocumentId(doc), fkey(key)), U)


def ent(state: State, who: Requester, slot: Slot) -> Entitlement:
    return entitled(state, who, slot)


def superseded_vocabulary_absent() -> None:
    """Critic item 14: no op, principal, requester field or produced reason for enrolment codes,
    devices, personal links or guests exists in the oracle's vocabulary (ADR 15.2, TA-2)."""
    banned = ("enrol", "device", "personal", "guest", "code", "approve")
    for op in ALL_OPS:
        expect(not any(b in op.__name__.lower() for b in banned), f"superseded op {op.__name__}")
    for member in PrincipalKind:
        expect(not any(b in member.name.lower() for b in banned), f"superseded principal {member.name}")
    same(tuple(f.name for f in fields(Requester)), ("account", "screen_grant"), "requester fields")
    expect(StopReason.PERSONAL_LINK_RESET in NEVER_PRODUCED, "personal_link_reset is never produced")


def _all_present_values() -> Mapping[FieldKey, Value]:
    return frozen_map({k: Value.PRESENT for k in sorted(NPC_V1.declared)})


# ------------------------------------------------------------------------------------ TT-1..TT-55


def _tt1(col: Col) -> None:
    sc = Sc(K(col))
    r = sc.confirm("ondrey", ["name", "portrait"], Table())
    check(
        r,
        answer=Ok(),
        slots={TABLE: sv("ondrey", "name portrait")},
        events=disp("ondrey", "name portrait", TABLE),
        epoch=True,
        authz=False,
    )
    same(observe(r).authz_mode, "shared", "TT-1 lock mode")


def _refused_when_enforced(
    col: Col, doc: str, names: list[str], audience: RevealAudience, slot: Slot, bad: str
) -> None:
    sc = Sc(K(col))
    r = sc.confirm(doc, names, audience)
    joined = " ".join(names)
    if col is Col.V1:
        check(r, answer=Ok(), slots={slot: sv(doc, joined)}, events=disp(doc, joined, slot), epoch=True, authz=False)
    else:
        check(r, answer=refused(RefusalKind.NOT_ELIGIBLE, *bad.split()), slots={}, events=[], epoch=False, authz=False)


def _tt2(col: Col) -> None:
    _refused_when_enforced(col, "ondrey", ["name", "history"], Table(), TABLE, "history")


def _tt3(col: Col) -> None:
    _refused_when_enforced(col, "ondrey", ["motives"], Table(), TABLE, "motives")


def _tt4(col: Col) -> None:
    sc = Sc(K(col))
    if col is Col.V1:
        r = sc.confirm("ondrey", ["notes"], Table())
        check(
            r,
            answer=Ok(),
            slots={TABLE: sv("ondrey", "notes")},
            events=disp("ondrey", "notes", TABLE),
            epoch=True,
            authz=False,
        )
        raises_usage(_v1_classify_probe, "TT-4(b) v1 has no classify list")
        return
    r = sc.confirm("ondrey", ["notes"], Table())
    check(r, answer=refused(RefusalKind.NOT_ELIGIBLE, "notes"), slots={}, events=[], epoch=False, authz=False)
    r = sc.confirm("ondrey", ["notes"], Table(), [cls_entry("notes", U, PUB)])
    check(
        r,
        answer=Ok(),
        slots={TABLE: sv("ondrey", "notes")},
        events=disp("ondrey", "notes", TABLE),
        epoch=True,
        authz=True,
    )
    same(cls_of(sc.state, "ondrey", "notes"), PUB, "TT-4 class")
    starts_exclusive(r, "TT-4(b)")


def _tt5(col: Col) -> None:
    sc = Sc(K(col))
    same(default_draft(sc.state, ONDREY, 1, P("ana")), (), "TT-5 draft")
    r = sc.confirm("ondrey", ["history"], to("ana"))
    check(
        r,
        answer=Ok(),
        slots={P("ana"): sv("ondrey", "history")},
        events=disp("ondrey", "history", P("ana")),
        epoch=True,
        authz=False,
    )


def _tt6(col: Col) -> None:
    _refused_when_enforced(col, "ondrey", ["rumours"], to("ben"), P("ben"), "rumours")


def _tt7(col: Col) -> None:
    sc = Sc(K(col))
    r = sc.confirm("ondrey", ["tags"], Table())
    check(r, answer=refused(RefusalKind.MASK_INVALID, "tags"), slots={}, events=[], epoch=False, authz=False)
    r = sc.confirm("ondrey", ["sources"], to("ana"))
    check(r, answer=refused(RefusalKind.MASK_INVALID, "sources"), slots={}, events=[], epoch=False, authz=False)


def _live_ondrey(col: Col) -> Sc:
    sc = Sc(K(col))
    r = sc.confirm("ondrey", ["name", "portrait"], Table())
    check(
        r,
        answer=Ok(),
        slots={TABLE: sv("ondrey", "name portrait")},
        events=disp("ondrey", "name portrait", TABLE),
        epoch=True,
        authz=False,
    )
    return sc


def _tt8(col: Col) -> None:
    sc = _live_ondrey(col)
    for who in (SCREEN, GM, BEN):
        same(ent(sc.state, who, TABLE), Entitlement.ENTITLED, "TT-8 table entitlement")
        view = visible(sc.state, who)["table"]
        same((view.document, view.mask), (ONDREY, (fkey("name"), fkey("portrait"))), "TT-8 table view")
    expect(asset_readable(sc.state, SCREEN, ONDREY, fkey("portrait")), "TT-8 portrait readable")
    expect(not asset_readable(sc.state, SCREEN, ONDREY, fkey("history")), "TT-8 history not readable")


def _tt9(col: Col) -> None:
    sc = Sc(K(col))
    r = sc.confirm("kira", ["name"], to("ana"))
    check(
        r,
        answer=Ok(),
        slots={P("ana"): sv("kira", "name")},
        events=disp("kira", "name", P("ana")),
        epoch=True,
        authz=False,
    )
    for who in (SCREEN, GM, BEN):
        same(ent(sc.state, who, P("ana")), Entitlement.ABSENT, "TT-9 participant slot")
        expect("mine" not in visible(sc.state, who), "TT-9 no own view")


def _tt10(col: Col) -> None:
    sc = Sc(K(col))
    _, r = sc.begin(End())
    check(r, answer=Ok(), slots={}, events=[], epoch=True, authz=False)
    same(ent(sc.state, ANA, TABLE), Entitlement.INACTIVE, "TT-10 table")
    same(ent(sc.state, ANA, P("ana")), Entitlement.INACTIVE, "TT-10 own slot")
    same(ent(sc.state, NOBODY, TABLE), Entitlement.UNAUTHENTICATED, "TT-10 nobody")


def _tt11(col: Col) -> None:
    sc = Sc(K(col))
    all_keys = expand_all(sc.state.world, KIRA, 1)
    same(all_keys, (fkey("class"), fkey("name"), fkey("notes")), "TT-11 expansion")
    r = sc.confirm("kira", list(all_keys), to("ana"))
    check(
        r,
        answer=Ok(),
        slots={P("ana"): sv("kira", "class name notes")},
        events=disp("kira", "class name notes", P("ana")),
        epoch=True,
        authz=False,
    )


def _tt12(col: Col) -> None:
    same(default_draft(K(col), KIRA, 1, P("ben")), (), "TT-12 draft")
    _refused_when_enforced(col, "kira", ["class", "name", "notes"], to("ben"), P("ben"), "class name notes")


def _tt13(col: Col) -> None:
    same(default_draft(K(col), KIRA, 1, TABLE), (), "TT-13 draft")
    _refused_when_enforced(col, "kira", ["class", "name"], Table(), TABLE, "class name")
    if col is Col.ENFORCED:
        sc = Sc(K(col))
        chars = FieldClass.characters("kira")
        r = sc.confirm(
            "kira", ["class", "name"], Table(), [cls_entry("class", chars, PUB), cls_entry("name", chars, PUB)]
        )
        check(
            r,
            answer=Ok(),
            slots={TABLE: sv("kira", "class name")},
            events=disp("kira", "class name", TABLE),
            epoch=True,
            authz=True,
        )


def _tt14(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], to("ben"))
    r = sc.last(RevokeScreen(SCREEN1))
    check(r, answer=Ok(), slots={P("ben"): sv("ondrey", "name")}, events=[], epoch=False, authz=False)
    dead = Requester(AccountId("acct_ben"), SCREEN1)
    for slot in (TABLE, P("ana"), P("ben"), P("cy")):
        same(ent(sc.state, dead, slot), ent(sc.state, BEN, slot), "TT-14 a dead grant never blocks the account")
    same(visible(sc.state, dead), visible(sc.state, BEN), "TT-14 dead grant view")
    live = Sc(K(col))
    live.confirm("ondrey", ["name"], to("ben"))
    on_screen = Requester(AccountId("acct_ben"), SCREEN1)
    same(table_principal(live.state, on_screen), PrincipalKind.SCREEN, "TT-14 a live grant decides alone")
    same(ent(live.state, on_screen, P("ben")), Entitlement.ABSENT, "TT-14 a screen never reads Ben's slot")
    expect("mine" not in visible(live.state, on_screen), "TT-14 no own view on a screen")


def _tt15(col: Col) -> None:
    sc = Sc(K(col))
    moss = {P("cy"): sv("moss", "class name notes")}
    r = sc.confirm("moss", ["class", "name", "notes"], to("cy"))
    check(r, answer=Ok(), slots=moss, events=disp("moss", "class name notes", P("cy")), epoch=True, authz=False)
    expect(held(sc.state, CY_ID), "TT-15 held")
    same(ent(sc.state, CY, P("cy")), Entitlement.INACTIVE, "TT-15 offered")
    r = sc.last(Accept(CY_ID))
    check(r, answer=Ok(), slots=moss, events=[], epoch=False, authz=True)
    same(ent(sc.state, CY, P("cy")), Entitlement.ABSENT, "TT-15 awaiting")
    r = sc.last(ConfirmSeat(CY_ID))
    check(r, answer=Ok(), slots=moss, events=[], epoch=False, authz=True)
    same(ent(sc.state, CY, P("cy")), Entitlement.ENTITLED, "TT-15 confirmed")
    same(visible(sc.state, CY)["mine"].document, MOSS, "TT-15 delivered")


def _tt16(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("kira", ["name"], to("ana"))
    oid, r = sc.begin(Relink(KIRA, BEN_ID))
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(StopReason.CHARACTER_UNLINKED, "kira", "name", P("ana")),
        epoch=True,
        authz=False,
    )
    no_authz(r, "TT-16 step 1")
    r = sc.cont(oid, Phase.STEP2)
    check(r, answer=Ok(), slots={}, events=[], epoch=True, authz=True)
    same(sc.state.world.documents[KIRA].linked_participant, BEN_ID, "TT-16 linked")
    if col is Col.ENFORCED:
        expect(
            eligible_for_audience(sc.state.world, KIRA, fkey("name"), ParticipantAudienceId(BEN_ID)).allowed,
            "TT-16 eligible",
        )


def _tt17(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("kira", ["name"], to("ana"))
    staged = sc.compose("kira", ["name"], to("ana"))
    sc.begin(Relink(KIRA, BEN_ID))
    r = sc.last(staged)
    check(r, answer=refused(RefusalKind.EPOCH_STALE), slots={}, events=[], epoch=False, authz=False)


def _tt18(col: Col) -> None:
    sc = _live_ondrey(col)
    r = sc.last(Stop(CommandId("stop1"), ONDREY))
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(StopReason.GM_STOP, "ondrey", "name portrait", TABLE),
        epoch=True,
        authz=False,
    )
    same(r.footprint, (LockLevel.SESSION_ROW, LockLevel.SLOT_ROWS), "TT-18 footprint")


def _tt19(col: Col) -> None:
    sc = Sc(K(col))
    oid, _ = sc.begin(sc.compose("ondrey", ["name", "portrait"], Table()))
    r = sc.last(Stop(CommandId("stop1"), "all"))
    check(r, answer=Ok(), slots={}, events=[], epoch=True, authz=False)
    r = sc.cont(oid, Phase.COMMIT)
    check(r, answer=refused(RefusalKind.EPOCH_STALE), slots={}, events=[], epoch=False, authz=False)


def _tt20(col: Col) -> None:
    sc = _live_ondrey(col)
    r = sc.confirm("marsh", ["name"], Table())
    events = stp(StopReason.REPLACED, "ondrey", "name portrait", TABLE) + disp("marsh", "name", TABLE)
    check(r, answer=Ok(), slots={TABLE: sv("marsh", "name")}, events=events, epoch=True, authz=False)


def _tt21(col: Col) -> None:
    for widened in (True, False) if col is Col.ENFORCED else (True,):
        sc = Sc(K(col))
        sc.confirm("kira", ["class", "name"], to("ana"))
        if col is Col.ENFORCED and widened:
            chars = FieldClass.characters("kira")
            sc.run(Classify(KIRA, fkey("name"), chars, PUB))
            sc.run(Classify(KIRA, fkey("class"), chars, PUB))
        r = sc.confirm("kira", ["class", "name"], Table())
        if widened:
            events = stp(StopReason.MOVED, "kira", "class name", P("ana")) + disp("kira", "class name", TABLE)
            check(r, answer=Ok(), slots={TABLE: sv("kira", "class name")}, events=events, epoch=True, authz=False)
            same(len(sc.state.disclosures), 1, "TT-21 one live disclosure")
        else:
            check(
                r,
                answer=refused(RefusalKind.NOT_ELIGIBLE, "class", "name"),
                slots={P("ana"): sv("kira", "class name")},
                events=[],
                epoch=False,
                authz=False,
            )


def _two_live(col: Col) -> Sc:
    sc = _live_ondrey(col)
    sc.confirm("kira", ["name"], to("ana"))
    return sc


def _tt22(col: Col) -> None:
    sc = _two_live(col)
    oid, r = sc.begin(End())
    gm_end = ended(SessionEndReason.GM_END, "ondrey", "name portrait", TABLE) + ended(
        SessionEndReason.GM_END, "kira", "name", P("ana")
    )
    check(r, answer=Ok(), slots={}, events=gm_end, epoch=True, authz=False)
    no_authz(r, "TT-22 End")
    expect(not sc.state.sessions[S1].live, "TT-22 s1 ended")
    for who in (ANA, GM, BEN):
        same(ent(sc.state, who, TABLE), Entitlement.INACTIVE, "TT-22 inactive")
    same(ent(sc.state, SCREEN, TABLE), Entitlement.UNAUTHENTICATED, "TT-22 screen")
    r = sc.cont(oid, Phase.RECONCILE)
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    r = sc.last(Start())
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    same(sc.state.live, "s2", "TT-22 next session")
    for finaliser in (ExpireJob(), Start()):
        sc = _two_live(col)
        r = sc.last(Expire())
        live_now = {TABLE: sv("ondrey", "name portrait"), P("ana"): sv("kira", "name")}
        check(r, answer=Ok(), slots=live_now, events=[], epoch=False, authz=False)
        same(r.footprint, (), "TT-22 expiry takes no lock")
        for who in (ANA, GM, BEN):
            same(ent(sc.state, who, TABLE), Entitlement.INACTIVE, "TT-22 expired: inactive")
        same(ent(sc.state, SCREEN, TABLE), Entitlement.UNAUTHENTICATED, "TT-22 expired: screen")
        expired = ended(SessionEndReason.EXPIRED, "ondrey", "name portrait", TABLE) + ended(
            SessionEndReason.EXPIRED, "kira", "name", P("ana")
        )
        _, r = sc.begin(finaliser)
        check(r, answer=Ok(), slots={}, events=expired, epoch=True, authz=False)
        no_authz(r, "TT-22 finalisation")


def _tt23(col: Col) -> None:
    sc = _live_ondrey(col)
    oid, r = sc.begin(Rotate())
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(StopReason.LINK_ROTATED, "ondrey", "name portrait", TABLE),
        epoch=True,
        authz=False,
    )
    same(sc.state.sessions[S1].generation, 2, "TT-23 generation")
    same(ent(sc.state, SCREEN, TABLE), Entitlement.UNAUTHENTICATED, "TT-23 the screen dies")
    same(ent(sc.state, ANA, TABLE), Entitlement.ENTITLED, "TT-23 seats stay")
    r = sc.cont(oid, Phase.RECONCILE)
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)


def _tt24(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("kira", ["name"], to("ana"))
    oid, r = sc.begin(RemoveParticipant(ANA_ID))
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(StopReason.PARTICIPANT_REMOVED, "kira", "name", P("ana")),
        epoch=True,
        authz=False,
    )
    same(r.footprint, (LockLevel.ROW, LockLevel.SESSION_ROW, LockLevel.SLOT_ROWS, LockLevel.OUTBOX), "TT-24 footprint")
    same(sc.state.world.participants[ANA_ID].status, SeatStatus.REMOVED, "TT-24 removed")
    same(sc.state.world.documents[KIRA].linked_participant, ANA_ID, "TT-24 the sheet is kept and linked")
    same(ent(sc.state, ANA, TABLE), Entitlement.INACTIVE, "TT-24 inactive")
    r = sc.cont(oid, Phase.RECONCILE)
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    if col is Col.ENFORCED:
        reason = eligible_for_audience(sc.state.world, ONDREY, fkey("name"), ParticipantAudienceId(ANA_ID)).reason
        same(reason, EligReason.INACTIVE_PARTICIPANT, "TT-24 inactive before public")


def _tt25(col: Col) -> None:
    superseded_vocabulary_absent()
    sc = Sc(K(col))
    ana2 = ParticipantId("ana2")
    for op in (RemoveParticipant(ANA_ID), AddParticipant(ana2), Offer(ana2, AccountId("acct_ana")), Accept(ana2)):
        sc.run(op)
    r = sc.confirm("ondrey", ["name"], to("ana2"))
    check(
        r,
        answer=Ok(),
        slots={P("ana2"): sv("ondrey", "name")},
        events=disp("ondrey", "name", P("ana2")),
        epoch=True,
        authz=False,
    )
    same(ent(sc.state, ANA, P("ana2")), Entitlement.ABSENT, "TT-25 before confirmation")
    sc.run(ConfirmSeat(ana2))
    same(ent(sc.state, ANA, P("ana2")), Entitlement.ENTITLED, "TT-25 after confirmation")


def _tt26(col: Col) -> None:
    superseded_vocabulary_absent()
    expect("TT-14" in {c.id for c in TRUTH_TABLE}, "TT-26's successor TT-14 is catalogued")


def _tt27(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name", "history"], to("ana"))
    oid, r = sc.begin(Classify(ONDREY, fkey("history"), CAMP, GMO))
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(StopReason.ELIGIBILITY_TIGHTENED, "ondrey", "history name", P("ana")),
        epoch=True,
        authz=False,
    )
    no_authz(r, "TT-27 step 1")
    r = sc.cont(oid, Phase.STEP2)
    check(r, answer=Ok(), slots={}, events=[], epoch=True, authz=True)
    same(cls_of(sc.state, "ondrey", "history"), GMO, "TT-27 class")


def _tt28(col: Col) -> None:
    sc = Sc(K(col))
    r = sc.last(Classify(ONDREY, fkey("notes"), U, PUB))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    expect(eligible_for_audience(sc.state.world, ONDREY, fkey("notes"), TableAudienceId()).allowed, "TT-28 displayable")


def _edit_and_seal(sc: Sc) -> None:
    pinned = slots_now(sc.state)
    r = sc.last(Edit(ONDREY, _all_present_values()))
    check(r, answer=Ok(), slots=pinned, events=[], epoch=False, authz=False)
    r = sc.last(Seal(ONDREY, 2))
    check(r, answer=Ok(), slots=pinned, events=[], epoch=False, authz=False)


def _tt29(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["history"], to("ana"))
    _edit_and_seal(sc)
    same(slots_now(sc.state), {P("ana"): sv("ondrey", "history")}, "TT-29 pinned")
    if col is Col.ENFORCED:
        expect(
            eligible_for_audience(sc.state.world, ONDREY, fkey("history"), ParticipantAudienceId(ANA_ID)).allowed,
            "TT-29 eligible",
        )


def _tt30(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["history"], to("ana"))
    _edit_and_seal(sc)
    r = sc.confirm("ondrey", ["history"], to("ana"), version=2)
    check(
        r,
        answer=Ok(),
        slots={P("ana"): sv("ondrey", "history", 2)},
        events=upd("ondrey", "history", P("ana")),
        epoch=True,
        authz=False,
    )


def _tt31(col: Col) -> None:
    sc = Sc(K(col))
    expect(fkey("secret_ally") not in expand_all(sc.state.world, ONDREY, 1), "TT-31(a)")
    r = sc.confirm("ondrey", ["all"], Table())
    check(r, answer=refused(RefusalKind.MASK_INVALID, "all"), slots={}, events=[], epoch=False, authz=False)
    state = ondrey_on_v2_with_v1_copy(V1 if col is Col.V1 else AS, enforced=col is Col.ENFORCED)
    expect(fkey("secret_ally") not in state.slots[TABLE].mask, "TT-31(c) never revealed")
    if col is Col.ENFORCED:
        for audience in (TableAudienceId(), ParticipantAudienceId(ANA_ID), ParticipantAudienceId(BEN_ID)):
            reason = eligible_for_audience(state.world, ONDREY, fkey("secret_ally"), audience).reason
            same(reason, EligReason.UNCLASSIFIED, "TT-31(c)")


def _ids_result(col: Col) -> StepResult:
    sc = Sc(K(col))
    sc.confirm("kira", ["name"], to("ana"))
    return sc.confirm("ondrey", ["history"], to("ana", "ben"))


def _tt32(col: Col) -> None:
    r = _ids_result(col)
    main = (
        stp(StopReason.REPLACED, "kira", "name", P("ana"))
        + disp("ondrey", "history", P("ana"))
        + disp("ondrey", "history", P("ben"))
    )
    both = {P("ana"): sv("ondrey", "history"), P("ben"): sv("ondrey", "history")}
    check(r, answer=Ok(), slots=both, events=main, epoch=True, authz=False)
    same(observe(r).disclosures, (frozenset({P("ana"), P("ben")}),), "TT-32 one disclosure")
    variant = Sc(K(col))
    variant.confirm("kira", ["name"], to("ana"))
    r = variant.confirm("ondrey", ["rumours"], to("ana", "ben"))
    if col is Col.V1:
        rumours = {P("ana"): sv("ondrey", "rumours"), P("ben"): sv("ondrey", "rumours")}
        events = (
            stp(StopReason.REPLACED, "kira", "name", P("ana"))
            + disp("ondrey", "rumours", P("ana"))
            + disp("ondrey", "rumours", P("ben"))
        )
        check(r, answer=Ok(), slots=rumours, events=events, epoch=True, authz=False)
        return
    check(
        r,
        answer=refused(RefusalKind.NOT_ELIGIBLE, "rumours"),
        slots={P("ana"): sv("kira", "name")},
        events=[],
        epoch=False,
        authz=False,
    )
    grouped = Sc(K(col))
    grouped.confirm("kira", ["name"], to("ana"))
    g = grouped.confirm("ondrey", ["history"], Group(SCOUTS))
    same(observe(g), observe(_ids_result(col)), "TT-32 a group equals its id list")
    same([d.group for d in grouped.state.disclosures.values()], [SCOUTS], "TT-32 the group is remembered")


def _two_copies(col: Col) -> Sc:
    sc = Sc(K(col))
    sc.confirm("kira", ["name"], to("ana"))
    sc.confirm("ondrey", ["history"], to("ana", "ben"))
    return sc


def _tt33(col: Col) -> None:
    sc = _two_copies(col)
    r = sc.last(Stop(CommandId("stop1"), ONDREY))
    events = stp(StopReason.GM_STOP, "ondrey", "history", P("ana")) + stp(
        StopReason.GM_STOP, "ondrey", "history", P("ben")
    )
    check(r, answer=Ok(), slots={}, events=events, epoch=True, authz=False)


def _tt34(col: Col) -> None:
    ana_only = {P("ana"): sv("ondrey", "history")}
    if col is Col.V1:
        sc = _two_copies(col)
        _, r = sc.begin(RemoveParticipant(BEN_ID))
        check(
            r,
            answer=Ok(),
            slots=ana_only,
            events=stp(StopReason.PARTICIPANT_REMOVED, "ondrey", "history", P("ben")),
            epoch=True,
            authz=False,
        )
        return
    sc = Sc(K(col))
    sc.confirm("ondrey", ["history"], Group(SCOUTS))
    oid, r = sc.begin(RemoveFromGroup(SCOUTS, BEN_ID))
    check(
        r,
        answer=Ok(),
        slots=ana_only,
        events=stp(StopReason.GROUP_MEMBER_REMOVED, "ondrey", "history", P("ben")),
        epoch=True,
        authz=False,
    )
    r = sc.cont(oid, Phase.STEP2)
    check(r, answer=Ok(), slots=ana_only, events=[], epoch=True, authz=True)


def _tt35(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["history"], Group(SCOUTS))
    r = sc.last(AddToGroup(SCOUTS, CY_ID))
    live = {P("ana"): sv("ondrey", "history"), P("ben"): sv("ondrey", "history")}
    check(r, answer=Ok(), slots=live, events=[], epoch=False, authz=True)


def _tt36(col: Col) -> None:
    sc = Sc(K(col))
    sc.run(SetAutomation(True))
    sc.confirm("ondrey", ["history"], to("ana"))
    r = sc.last(Stop(CommandId("stop1"), ONDREY, retract=True))
    check(r, answer=Ok(), slots={}, events=retracted("ondrey", "history", P("ana")), epoch=True, authz=False)
    expect(not automation_may_redisplay(sc.state, ONDREY, fkey("history"), P("ana")), "TT-36 retracted")
    sc.run(End())
    sc.run(Start())
    expect(not automation_may_redisplay(sc.state, ONDREY, fkey("history"), P("ana")), "TT-36 still blocked")
    r = sc.confirm("ondrey", ["history"], to("ana"))
    check(
        r,
        answer=Ok(),
        slots={P("ana"): sv("ondrey", "history")},
        events=disp("ondrey", "history", P("ana")),
        epoch=True,
        authz=False,
    )


def _tt37(col: Col) -> None:
    for stop_first in (False, True):
        sc = Sc(K(col))
        if col is Col.ENFORCED:
            sc.run(SetAutomation(True))
        sc.confirm("ondrey", ["name"], Table())
        if stop_first:
            sc.run(Stop(CommandId("stop1"), ONDREY))
        _, r = sc.begin(End())
        events = [] if stop_first else ended(SessionEndReason.GM_END, "ondrey", "name", TABLE)
        check(r, answer=Ok(), slots={}, events=events, epoch=True, authz=False)
        sc.run(Start())
        same(dict(sc.state.slots), {}, "TT-37 nothing is re-displayed")
        if col is Col.V1:
            expect(not available(SetAutomation, V1), "TT-37 v1 has no automation")
            continue
        same(automation_may_redisplay(sc.state, ONDREY, fkey("name"), TABLE), not stop_first, "TT-37 re-display")
        expect(
            not automation_may_redisplay(sc.state, ONDREY, fkey("portrait"), TABLE), "TT-37 never a first disclosure"
        )


def _tt38(col: Col) -> None:
    sc = Sc(K(col))
    classes = sc.state.world.classes
    r = sc.last(Export(ONDREY, 1, keys("name", "history")))
    if col is Col.V1:
        check(r, answer=Ok(), slots={}, events=exported("ondrey", "history name"), epoch=False, authz=False)
        same(r.footprint, (), "TT-38 a v1 export takes no lock")
    else:
        check(r, answer=refused(RefusalKind.NOT_ELIGIBLE, "history"), slots={}, events=[], epoch=False, authz=False)
    r = sc.last(Export(ONDREY, 1, keys("name", "portrait")))
    check(r, answer=Ok(), slots={}, events=exported("ondrey", "name portrait"), epoch=False, authz=False)
    same(sc.state.world.classes, classes, "TT-38 an export never classifies")
    if col is Col.ENFORCED:
        same(r.footprint, (LockLevel.AUTHZ_SHARED,), "TT-38 share lock when Enforced")


def _tt39(col: Col) -> None:
    same(tuple(ContentKind), (ContentKind.DOCUMENT,), "TT-39 content kinds")
    same(
        tuple(f.name for f in fields(Copy)),
        ("disclosure", "document", "version", "mask"),
        "TT-39 a slot holds a document",
    )


def _tt40(col: Col) -> None:
    sc = Sc(K(col))
    same(default_draft(sc.state, NOTES1, 1, TABLE), (), "TT-40 draft")
    r = sc.confirm("notes1", ["recap"], to("ana"))
    if col is Col.V1:
        check(
            r,
            answer=Ok(),
            slots={P("ana"): sv("notes1", "recap")},
            events=disp("notes1", "recap", P("ana")),
            epoch=True,
            authz=False,
        )
        return
    check(r, answer=refused(RefusalKind.NOT_ELIGIBLE, "recap"), slots={}, events=[], epoch=False, authz=False)
    expect(fkey("recap") in unclassified_keys(sc.state.world, NOTES1), "TT-40 queued")
    for audience in (TableAudienceId(), ParticipantAudienceId(ANA_ID)):
        same(
            eligible_for_audience(sc.state.world, NOTES1, fkey("recap"), audience).reason,
            EligReason.UNCLASSIFIED,
            "TT-40",
        )


def _tt41(col: Col) -> None:
    sc = Sc(K(col, enforced=False))
    sc.confirm("ondrey", ["name", "notes"], Table())
    sc.confirm("kira", ["name"], to("ana"))
    kira = {P("ana"): sv("kira", "name")}
    oid, r = sc.begin(EnforcementOn())
    check(
        r,
        answer=Ok(),
        slots=kira,
        events=stp(StopReason.ENFORCEMENT_ENABLED, "ondrey", "name notes", TABLE),
        epoch=True,
        authz=False,
    )
    r = sc.cont(oid, Phase.STEP2)
    check(r, answer=Ok(), slots=kira, events=[], epoch=True, authz=True)
    expect(sc.state.world.enforced, "TT-41 enforced")


def _tt42(col: Col) -> None:
    sc = Sc(K(col))
    for audience in (TableAudienceId(), ParticipantAudienceId(ANA_ID), ParticipantAudienceId(BEN_ID)):
        reason = eligible_for_audience(sc.state.world, ONDREY, fkey("hidden_identity"), audience).reason
        same(reason, EligReason.NOT_REVEALABLE, "TT-42")
    r = sc.confirm("ondrey", ["hidden_identity"], Table())
    check(r, answer=refused(RefusalKind.MASK_INVALID, "hidden_identity"), slots={}, events=[], epoch=False, authz=False)
    if col is Col.ENFORCED:
        r = sc.last(Classify(ONDREY, fkey("hidden_identity"), GMO, PUB))
        check(
            r,
            answer=refused(RefusalKind.CLASSIFY_INVALID, "hidden_identity"),
            slots={},
            events=[],
            epoch=False,
            authz=False,
        )


def _tt43(col: Col) -> None:
    sc = Sc(K(col))
    for audience in (Table(), to("ana")):
        r = sc.confirm("ondrey", ["hidden_identity"], audience)
        check(
            r,
            answer=refused(RefusalKind.MASK_INVALID, "hidden_identity"),
            slots={},
            events=[],
            epoch=False,
            authz=False,
        )
    if col is Col.ENFORCED:
        same(unclassified_keys(sc.state.world, ONDREY), (fkey("notes"),), "TT-43 queue")


def _tt44(col: Col) -> None:
    _refused_when_enforced(col, "ondrey_sb", ["ac"], Table(), TABLE, "ac")


def _tt45(col: Col) -> None:
    state = K(col)
    if col is Col.ENFORCED:
        state = set_class(state, "ondrey", "notes", FieldClass.characters("kira"))
    sc = Sc(state)
    sc.confirm("ondrey", ["notes"], to("ana"))
    oid, r = sc.begin(Relink(KIRA, BEN_ID))
    if col is Col.V1:
        check(r, answer=Ok(), slots={P("ana"): sv("ondrey", "notes")}, events=[], epoch=True, authz=False)
    else:
        check(
            r,
            answer=Ok(),
            slots={},
            events=stp(StopReason.CHARACTER_UNLINKED, "ondrey", "notes", P("ana")),
            epoch=True,
            authz=False,
        )
    sc.cont(oid, Phase.STEP2)
    expect(P("ben") not in sc.state.slots, "TT-45 nothing is displayed to Ben")


def _tt46(col: Col) -> None:
    sc = Sc(set_class(K(col), "ondrey", "notes", FieldClass.groups("scouts")))
    sc.confirm("ondrey", ["notes"], to("ben"))
    _, r = sc.begin(RemoveFromGroup(SCOUTS, BEN_ID))
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(StopReason.GROUP_MEMBER_REMOVED, "ondrey", "notes", P("ben")),
        epoch=True,
        authz=False,
    )


def _tt47(col: Col) -> None:
    observers = (SCREEN, GM, BEN)
    a = Sc(K(col))
    r = a.confirm("ondrey", ["name"], Table())
    check(
        r,
        answer=Ok(),
        slots={TABLE: sv("ondrey", "name")},
        events=disp("ondrey", "name", TABLE),
        epoch=True,
        authz=False,
    )
    b = Sc(a.state)
    b.count = a.count
    b.confirm("kira", ["name"], to("ana"))
    for o in observers:
        same(visible(b.state, o), visible(a.state, o), "TT-47 a private reveal is invisible")
    if col is Col.ENFORCED:
        c = Sc(a.state)
        c.run(Classify(ONDREY, fkey("notes"), U, FieldClass.participants("ana")))
        for o in observers:
            same(visible(c.state, o), visible(a.state, o), "TT-47 a classification is invisible")


def _tt48(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name", "history"], to("ana"))
    before = set(sc.state.disclosures)
    r = sc.confirm("ondrey", ["name"], to("ana"))
    check(
        r,
        answer=Ok(),
        slots={P("ana"): sv("ondrey", "name")},
        events=stp(StopReason.MASK_NARROWED, "ondrey", "history", P("ana")),
        epoch=True,
        authz=False,
    )
    same(set(sc.state.disclosures), before, "TT-48 the disclosure is kept")


def _tt49(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Table())
    classes = sc.state.world.classes
    r = sc.last(EnforcementOff())
    check(r, answer=Ok(), slots={TABLE: sv("ondrey", "name")}, events=[], epoch=False, authz=True)
    expect(not sc.state.world.enforced, "TT-49 off")
    same(sc.state.world.classes, classes, "TT-49 classes kept")
    variant = Sc(K(col))
    variant.run(SetAutomation(True))
    r = variant.last(EnforcementOff())
    check(r, answer=refused(RefusalKind.AUTOMATION_ENABLED), slots={}, events=[], epoch=False, authz=False)
    expect(variant.state.world.enforced, "TT-49 still enforced")


def npc_v3_declaring_rumours() -> TypeVersion:
    """TT-50's candidate: a later `oracle_npc` version that wants `rumours` back."""
    return TypeVersion(
        version=3,
        declared=NPC_V2.declared | keys("rumours"),
        revealable=NPC_V2.revealable | keys("rumours"),
        reserved=frozenset(),
        default_reveal=NPC_V2.default_reveal,
    )


def _tt50(col: Col) -> None:
    problems = type_evolution_problems(NPC_V2, npc_v3_declaring_rumours())
    expect(any("rumours" in p for p in problems), "TT-50 the registry refuses a reused key")
    if col is Col.V1:
        return
    base = K(col)
    ondrey = base.world.documents[ONDREY]
    state = set_document(base, replace(ondrey, type_version=2, versions=(version_of(NPC_V2),)))
    state = set_class(state, "ondrey", "rumours", PUB)
    reason = eligible_for_audience(state.world, ONDREY, fkey("rumours"), TableAudienceId()).reason
    same(reason, EligReason.NOT_REVEALABLE, "TT-50 an orphan row is ignored")


def _tt51(col: Col) -> None:
    sc = Sc(K(col, enforced=False))
    sc.confirm("ondrey", ["notes"], Table())
    r = sc.last(Classify(ONDREY, fkey("notes"), U, GMO))
    check(r, answer=Ok(), slots={TABLE: sv("ondrey", "notes")}, events=[], epoch=False, authz=True)
    same(live_exposure(sc.state, ONDREY, fkey("notes")), frozenset({TABLE}), "TT-51 the table is seeing this now")
    sc = Sc(set_class(K(col, enforced=False), "ondrey", "notes", PUB))
    sc.confirm("ondrey", ["notes"], Table())
    oid, r = sc.begin(Classify(ONDREY, fkey("notes"), PUB, GMO))
    check(r, answer=Ok(), slots={TABLE: sv("ondrey", "notes")}, events=[], epoch=True, authz=False)
    r = sc.cont(oid, Phase.STEP2)
    check(r, answer=Ok(), slots={TABLE: sv("ondrey", "notes")}, events=[], epoch=True, authz=True)
    _, r = sc.begin(EnforcementOn())
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(StopReason.ENFORCEMENT_ENABLED, "ondrey", "notes", TABLE),
        epoch=True,
        authz=False,
    )


def _tt52(col: Col) -> None:
    sc = Sc(set_seat(K(col), Participant(BEN_ID, SeatStatus.AWAITING_CONFIRMATION, AccountId("acct_ben"))))
    r = sc.confirm("ondrey", ["name"], to("ben"))
    check(
        r,
        answer=Ok(),
        slots={P("ben"): sv("ondrey", "name")},
        events=disp("ondrey", "name", P("ben")),
        epoch=True,
        authz=False,
    )
    expect(held(sc.state, BEN_ID), "TT-52 held")
    same(ent(sc.state, BEN, P("ben")), Entitlement.ABSENT, "TT-52 own slot absent")
    same(ent(sc.state, BEN, TABLE), Entitlement.ENTITLED, "TT-52 table")
    r = sc.last(ConfirmSeat(BEN_ID))
    check(r, answer=Ok(), slots={P("ben"): sv("ondrey", "name")}, events=[], epoch=False, authz=True)
    expect(not held(sc.state, BEN_ID), "TT-52 delivered")
    mine = visible(sc.state, BEN)["mine"]
    same((mine.document, mine.mask), (ONDREY, (fkey("name"),)), "TT-52 own view")


def _tt53(col: Col) -> None:
    sc = Sc(K(col))
    staged = sc.compose("ondrey", ["notes"], Table(), [cls_entry("notes", U, PUB)])
    r = sc.last(Classify(ONDREY, fkey("notes"), U, GMO))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    r = sc.last(staged)
    check(r, answer=refused(RefusalKind.CLASS_MOVED), slots={}, events=[], epoch=False, authz=False)
    same(cls_of(sc.state, "ondrey", "notes"), GMO, "TT-53 class kept")
    ben_only = FieldClass.participants("ben")
    sc = Sc(set_class(K(col), "ondrey", "notes", ben_only))
    staged = sc.compose("ondrey", ["notes"], Table(), [cls_entry("notes", ben_only, PUB)])
    _, r = sc.begin(Classify(ONDREY, fkey("notes"), ben_only, GMO))
    check(r, answer=Ok(), slots={}, events=[], epoch=True, authz=False)
    r = sc.last(staged)
    check(r, answer=refused(RefusalKind.EPOCH_STALE), slots={}, events=[], epoch=False, authz=False)


def _tt54(col: Col) -> None:
    ana_only = FieldClass.participants("ana")
    sc = Sc(K(col))
    r = sc.confirm("ondrey", ["notes"], to("ana"), [cls_entry("notes", U, ana_only)])
    check(
        r,
        answer=Ok(),
        slots={P("ana"): sv("ondrey", "notes")},
        events=disp("ondrey", "notes", P("ana")),
        epoch=True,
        authz=True,
    )
    starts_exclusive(r, "TT-54")
    same(cls_of(sc.state, "ondrey", "notes"), ana_only, "TT-54 the narrowest widening")
    variants: list[tuple[list[str], list[ClassifyEntry], str]] = [
        (["notes"], [cls_entry("notes", U, CAMP)], "notes"),
        (["notes"], [cls_entry("notes", U, PUB)], "notes"),
        (["motives", "notes"], [cls_entry("notes", U, ana_only), cls_entry("motives", GMO, ana_only)], "motives"),
        (["name"], [cls_entry("notes", U, ana_only)], "notes"),
        (["rumours"], [cls_entry("rumours", ana_only, ana_only)], "rumours"),
    ]
    for names, entries, bad in variants:
        v = Sc(K(col))
        r = v.confirm("ondrey", names, to("ana"), entries)
        check(r, answer=refused(RefusalKind.CLASSIFY_INVALID, bad), slots={}, events=[], epoch=False, authz=False)
        same(cls_of(v.state, "ondrey", bad), cls_of(K(col), "ondrey", bad), "TT-54 a refused entry keeps its class")
    v = Sc(K(col))
    r = v.confirm("ondrey", ["name"], Table(), [cls_entry("notes", U, PUB)])
    check(r, answer=refused(RefusalKind.CLASSIFY_INVALID, "notes"), slots={}, events=[], epoch=False, authz=False)
    same(cls_of(v.state, "ondrey", "notes"), U, "TT-54 a key outside the mask is never classified")


def _tt55(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name", "portrait"], to("ana", "ben"))
    r = sc.confirm("ondrey", ["name", "portrait"], Table())
    events = (
        stp(StopReason.MOVED, "ondrey", "name portrait", P("ana"))
        + stp(StopReason.MOVED, "ondrey", "name portrait", P("ben"))
        + disp("ondrey", "name portrait", TABLE)
    )
    check(r, answer=Ok(), slots={TABLE: sv("ondrey", "name portrait")}, events=events, epoch=True, authz=False)
    same(observe(r).disclosures, (frozenset({TABLE}),), "TT-55 partition")


def _v1_classify_probe() -> None:
    Sc(K(Col.V1)).confirm("ondrey", ["notes"], Table(), [cls_entry("notes", U, PUB)])


def _v1_retract_probe() -> None:
    Sc(K(Col.V1)).run(Stop(CommandId("stop1"), ONDREY, retract=True))


def _both(fn: Callable[[Col], None]) -> tuple[Column, Column]:
    return partial(fn, Col.V1), partial(fn, Col.ENFORCED)


def _enforced_only(
    fn: Callable[[Col], None], *ops: type, probe: Callable[[], object] | None = None
) -> tuple[NoCase, Column]:
    return NoCase(ops, V1, probe), partial(fn, Col.ENFORCED)


_M = Disposition.MODELLED
_A = Disposition.ACCOUNT_FORM
_S = Disposition.SUPERSEDED
_TA5 = "threat model 15.11 (TA-5), read in account form"


def _tt(
    n: int,
    title: str,
    rules: tuple[str, ...],
    disposition: Disposition,
    cols: tuple[Column | NoCase, Column | NoCase],
    *,
    not_modelled: tuple[str, ...] = (),
    supersession: str | None = None,
    successor: str | None = None,
) -> Case:
    return Case(f"TT-{n}", title, rules, disposition, cols[0], cols[1], not_modelled, supersession, None, successor)


TRUTH_TABLE: Final[tuple[Case, ...]] = (
    _tt(1, "reveal public keys to the table", ("ED-9", "ED-10"), _M, _both(_tt1)),
    _tt(2, "a campaign key never reaches the table", ("ED-10", "ED-9"), _M, _both(_tt2)),
    _tt(3, "gm_only is never displayable", ("ED-10",), _M, _both(_tt3)),
    _tt(4, "unclassified is refused unless the Confirm classifies", ("ED-10", "ED-13", "M-4"), _M, _both(_tt4)),
    _tt(5, "a participant slot takes any type; the draft seeds empty", ("ED-14", "ED-10"), _M, _both(_tt5)),
    _tt(6, "participants[Ana] does not name Ben", ("ED-10",), _M, _both(_tt6)),
    _tt(7, "keys off the allowlist are refused", ("ED-5",), _M, _both(_tt7)),
    _tt(
        8,
        "the table slot and its assets",
        ("ED-19",),
        _A,
        _both(_tt8),
        supersession=_TA5 + "; a guest is a screen or the owner",
    ),
    _tt(
        9,
        "nothing beyond the table slot",
        ("SEC-41", "SEC-46", "REVEAL-24", "ED-25"),
        _A,
        _both(_tt9),
        supersession=_TA5,
    ),
    _tt(
        10,
        "no live session opens nothing",
        ("AUD-5", "D-4"),
        _A,
        _both(_tt10),
        supersession="ADR 15.2 amends TT-10 (D-4)",
    ),
    _tt(11, "all is expanded before it is sent", ("AUD-12", "ED-8", "ED-10"), _M, _both(_tt11)),
    _tt(12, "Kira to Ben", ("ED-14", "ED-10"), _M, _both(_tt12)),
    _tt(13, "Kira to the table", ("ED-10", "ED-13"), _M, _both(_tt13)),
    _tt(
        14,
        "a second browser is the same account; a screen is the table",
        ("SEC-44", "WT-23", "ED-25"),
        _A,
        _both(_tt14),
        supersession="ADR 15.2: a seated account's second browser is the same account",
    ),
    _tt(
        15,
        "a held copy for an offered seat",
        ("AUD-10", "ED-10", "SEC-50(5)"),
        _A,
        _both(_tt15),
        supersession="SEC-50(5): Cy holds an offered seat, not an unenrolled device",
    ),
    _tt(16, "a relink stops first", ("AUD-15", "ED-12", "ED-13"), _M, _both(_tt16)),
    _tt(17, "a staged widening after a relink is 409", ("REVEAL-22", "REVEAL-15"), _M, _both(_tt17)),
    _tt(18, "Stop showing", ("ED-16", "X-3"), _M, _both(_tt18)),
    _tt(19, "a Stop that lands first wins", ("REVEAL-22", "AE-48"), _M, _both(_tt19)),
    _tt(20, "replace", ("REVEAL-7", "ED-15"), _M, _both(_tt20)),
    _tt(21, "move", ("REVEAL-7", "ED-15"), _M, _both(_tt21)),
    _tt(
        22,
        "End or expiry",
        ("REVEAL-17", "RQ-5", "SEC-42"),
        _A,
        _both(_tt22),
        supersession=_TA5 + "; the link dies reads every screen grant dies",
    ),
    _tt(
        23,
        "Rotate",
        ("REVEAL-17", "RQ-5", "SEC-42"),
        _A,
        _both(_tt23),
        supersession=_TA5 + "; Rotate link reads Rotate",
    ),
    _tt(24, "remove a participant", ("AUD-16", "RQ-5"), _A, _both(_tt24), supersession=_TA5 + "; no device credential"),
    _tt(
        25,
        "reset personal link",
        ("AUD-5",),
        _S,
        _both(_tt25),
        supersession="TA-2 and 15.11 retire the personal link; the successor is a new seat",
        successor="TT-25",
    ),
    _tt(
        26,
        "a second device",
        ("AUD-5", "ED-14", "ED-25"),
        _S,
        _both(_tt26),
        supersession="ADR 15.2: there are no devices",
        successor="TT-14",
    ),
    _tt(27, "tighten a live key", ("ED-12",), _M, _enforced_only(_tt27, Classify)),
    _tt(28, "classifying public displays nothing", ("ED-13",), _M, _enforced_only(_tt28, Classify)),
    _tt(29, "an AI edit leaves the pin", ("REVEAL-8", "ED-6"), _M, _both(_tt29)),
    _tt(30, "Update shows the new version", ("ED-22",), _M, _both(_tt30)),
    _tt(31, "a later field is never revealed by all", ("ED-8",), _M, _both(_tt31)),
    _tt(32, "several participants, one disclosure", ("ED-15", "ED-10"), _M, _both(_tt32)),
    _tt(33, "Stop clears every copy", ("ED-15", "ED-16"), _M, _both(_tt33)),
    _tt(34, "a member leaves", ("ED-12",), _M, _both(_tt34)),
    _tt(35, "a member joins", ("ED-13",), _M, _enforced_only(_tt35, AddToGroup)),
    _tt(
        36,
        "retracted blocks automation",
        ("ED-16", "ED-18"),
        _M,
        _enforced_only(_tt36, SetAutomation, probe=_v1_retract_probe),
    ),
    _tt(37, "re-display after the session ends", ("ED-17", "7.3"), _M, _both(_tt37)),
    _tt(38, "player-safe export", ("ED-21", "EXPORT-3"), _M, _both(_tt38)),
    _tt(39, "rules excerpts are gated", ("7.4",), _M, _both(_tt39)),
    _tt(
        40,
        "session notes seed empty",
        ("REVEAL-23",),
        _M,
        _both(_tt40),
        not_modelled=("the reveal sheet's warning line",),
    ),
    _tt(41, "switching enforcement on narrows", ("M-3",), _M, _enforced_only(_tt41, EnforcementOn)),
    _tt(42, "identity links are never revealable", ("ED-20",), _M, _both(_tt42)),
    _tt(43, "hidden_identity is gm_only by construction", ("ED-5", "ED-20"), _M, _both(_tt43)),
    _tt(44, "a gm_only stat block", ("ED-10",), _M, _both(_tt44)),
    _tt(45, "a relink narrows a characters class", ("ED-12", "ED-13"), _M, _both(_tt45)),
    _tt(46, "leaving a group narrows a groups class", ("ED-12",), _M, _enforced_only(_tt46, RemoveFromGroup)),
    _tt(
        47,
        "no inference across slots",
        ("ED-25", "REVEAL-24"),
        _A,
        _both(_tt47),
        supersession=_TA5 + "; a guest is a screen or the owner",
    ),
    _tt(48, "Update narrows a mask", ("ED-17", "REVEAL-6"), _M, _both(_tt48)),
    _tt(49, "switching enforcement off", ("M-8",), _M, _enforced_only(_tt49, EnforcementOff)),
    _tt(
        50, "retired keys are reserved", ("ED-24",), _M, _both(_tt50), not_modelled=("the row's removal at migration",)
    ),
    _tt(51, "a class before enforcement stops nothing", ("ED-12", "M-3"), _M, _enforced_only(_tt51, Classify)),
    _tt(
        52,
        "a held copy for an awaiting seat",
        ("ED-14", "ED-25", "AUD-10", "SEC-50(5)"),
        _A,
        _both(_tt52),
        supersession="15.11 supersedes the pending device; SEC-50(5)",
    ),
    _tt(53, "a moved class is 409", ("ED-13",), _M, _enforced_only(_tt53, Classify, probe=_v1_classify_probe)),
    _tt(54, "the narrowest widening", ("ED-13",), _M, _enforced_only(_tt54, Classify, probe=_v1_classify_probe)),
    _tt(55, "moving two copies to the table", ("ED-15", "REVEAL-7"), _M, _both(_tt55)),
)


# ------------------------------------------------------------------------------------ RC-1..RC-16


def _rc1(col: Col) -> None:
    sc = Sc(K(col))
    oid, _ = sc.begin(sc.compose("ondrey", ["name"], Table()))
    sc.run(Stop(CommandId("stop1"), ONDREY))
    r = sc.cont(oid, Phase.COMMIT)
    check(r, answer=refused(RefusalKind.EPOCH_STALE), slots={}, events=[], epoch=False, authz=False)
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Table())
    r = sc.last(Stop(CommandId("stop1"), ONDREY))
    check(r, answer=Ok(), slots={}, events=stp(StopReason.GM_STOP, "ondrey", "name", TABLE), epoch=True, authz=False)


@dataclass(frozen=True)
class _Race:
    state: Callable[[], State]
    narrowing: Op
    doc: str
    names: str
    audience: RevealAudience
    slot: Slot
    reason: StopReason
    after: Answer
    between: Answer


def _race_placements(race: _Race) -> None:
    """RC-2 and RC-3, with critic item 3's placement (iv)."""
    names = race.names.split()
    sc = Sc(race.state())
    staged = sc.compose(race.doc, names, race.audience)
    sc.begin(race.narrowing)
    r = sc.last(staged)
    check(r, answer=refused(RefusalKind.EPOCH_STALE), slots={}, events=[], epoch=False, authz=False, what="(i)")
    sc = Sc(race.state())
    oid, _ = sc.begin(race.narrowing)
    r = sc.confirm(race.doc, names, race.audience)
    check(
        r,
        answer=Ok(),
        slots={race.slot: sv(race.doc, race.names)},
        events=disp(race.doc, race.names, race.slot),
        epoch=True,
        authz=False,
        what="(ii)",
    )
    r = sc.cont(oid, Phase.STEP2)
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(race.reason, race.doc, race.names, race.slot),
        epoch=True,
        authz=True,
        what="(ii) step 2",
    )
    sc = Sc(race.state())
    sc.run(race.narrowing)
    r = sc.confirm(race.doc, names, race.audience)
    if isinstance(race.after, Ok):
        check(
            r,
            answer=Ok(),
            slots={race.slot: sv(race.doc, race.names)},
            events=disp(race.doc, race.names, race.slot),
            epoch=True,
            authz=False,
            what="(iii)",
        )
    else:
        check(r, answer=race.after, slots={}, events=[], epoch=False, authz=False, what="(iii)")
    sc = Sc(race.state())
    oid, _ = sc.begin(race.narrowing)
    coid, _ = sc.begin(sc.compose(race.doc, names, race.audience))
    sc.cont(oid, Phase.STEP2)
    r = sc.cont(coid, Phase.COMMIT)
    check(r, answer=race.between, slots={}, events=[], epoch=False, authz=False, what="(iv)")


def _rc2(col: Col) -> None:
    def state() -> State:
        return K(col)

    enf = col is Col.ENFORCED
    archived = refused(RefusalKind.DOCUMENT_ARCHIVED)
    _race_placements(
        _Race(
            state, Archive(ONDREY), "ondrey", "name", Table(), TABLE, StopReason.DOCUMENT_ARCHIVED, archived, archived
        )
    )
    after_relink: Answer = refused(RefusalKind.NOT_ELIGIBLE, "name") if enf else Ok()
    between_relink: Answer = refused(RefusalKind.NOT_ELIGIBLE, "name") if enf else refused(RefusalKind.EPOCH_STALE)
    relink = _Race(
        state,
        Relink(KIRA, BEN_ID),
        "kira",
        "name",
        to("ana"),
        P("ana"),
        StopReason.CHARACTER_UNLINKED,
        after_relink,
        between_relink,
    )
    _race_placements(relink)
    if enf:
        tightened = refused(RefusalKind.NOT_ELIGIBLE, "history")
        classify = Classify(ONDREY, fkey("history"), CAMP, GMO)
        _race_placements(
            _Race(
                state,
                classify,
                "ondrey",
                "history",
                to("ana"),
                P("ana"),
                StopReason.ELIGIBILITY_TIGHTENED,
                tightened,
                tightened,
            )
        )


def _rc3(col: Col) -> None:
    def state() -> State:
        return K(col, enforced=False)

    refusal = refused(RefusalKind.NOT_ELIGIBLE, "notes")
    _race_placements(
        _Race(
            state, EnforcementOn(), "ondrey", "notes", Table(), TABLE, StopReason.ENFORCEMENT_ENABLED, refusal, refusal
        )
    )


def _rc4(col: Col) -> None:
    sc = Sc(K(col))
    first = sc.compose("ondrey", ["name"], Table())
    second = sc.compose("marsh", ["name"], Table())
    r = sc.last(first)
    check(
        r,
        answer=Ok(),
        slots={TABLE: sv("ondrey", "name")},
        events=disp("ondrey", "name", TABLE),
        epoch=True,
        authz=False,
    )
    r = sc.last(second)
    check(
        r,
        answer=refused(RefusalKind.EPOCH_STALE),
        slots={TABLE: sv("ondrey", "name")},
        events=[],
        epoch=False,
        authz=False,
    )


def _rc5(col: Col) -> None:
    for revocation, answer in ((End(), RefusalKind.SESSION_NOT_LIVE), (Rotate(), RefusalKind.EPOCH_STALE)):
        sc = Sc(K(col))
        staged = sc.compose("ondrey", ["name"], Table())
        sc.run(revocation)
        r = sc.last(staged)
        same(observe(r).answer.status, 409, "RC-5 revocation first")
        check(r, answer=refused(answer), slots={}, events=[], epoch=False, authz=False)
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Table())
    _, r = sc.begin(End())
    check(
        r,
        answer=Ok(),
        slots={},
        events=ended(SessionEndReason.GM_END, "ondrey", "name", TABLE),
        epoch=True,
        authz=False,
    )
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Table())
    _, r = sc.begin(Rotate())
    check(
        r, answer=Ok(), slots={}, events=stp(StopReason.LINK_ROTATED, "ondrey", "name", TABLE), epoch=True, authz=False
    )
    sc = Sc(K(col))
    sc.run(Expire())
    r = sc.confirm("ondrey", ["name"], Table())
    check(r, answer=refused(RefusalKind.SESSION_NOT_LIVE), slots={}, events=[], epoch=False, authz=False)
    sc = Sc(K(col))
    oid, r = sc.begin(sc.compose("ondrey", ["name"], Table()))
    same(r.answer, Ok(), "RC-5 the courtesy passes before the expiry")
    sc.run(Expire())
    r = sc.cont(oid, Phase.COMMIT)
    check(r, answer=refused(RefusalKind.SESSION_NOT_LIVE), slots={}, events=[], epoch=False, authz=False)


def _rc6(col: Col) -> None:
    sc = Sc(K(col))
    sc.run(End())
    oid, r = sc.begin(RemoveParticipant(ANA_ID))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=False)
    same(r.footprint, (LockLevel.ROW, LockLevel.OUTBOX), "RC-6 no session row without a live session")
    sc.run(Start())
    r = sc.confirm("ondrey", ["name"], to("ana"))
    check(r, answer=refused(RefusalKind.AUDIENCE_INVALID), slots={}, events=[], epoch=False, authz=False)
    once = sc.cont(oid, Phase.RECONCILE)
    check(once, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    twice = sc.cont(oid, Phase.RECONCILE)
    check(twice, answer=Ok(), slots=slots_now(once.after), events=[], epoch=False, authz=True)
    same(twice.after.grants, once.after.grants, "RC-6 reconcile is idempotent")


def _rc7(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Table())
    no_authz(sc.last(Stop(CommandId("stop1"), ONDREY)), "RC-7 Stop")
    narrowings: list[Op] = [Archive(ONDREY), Unlink(KIRA), Relink(KIRA, BEN_ID)]
    if col is Col.ENFORCED:
        narrowings += [Classify(ONDREY, fkey("history"), CAMP, GMO), RemoveFromGroup(SCOUTS, BEN_ID)]
    for op in narrowings:
        _, r = Sc(K(col)).begin(op)
        no_authz(r, f"RC-7 {type(op).__name__} step 1")
        expect(r.epoch_advanced, f"RC-7 {type(op).__name__} step 1 advances the epoch")
    if col is Col.ENFORCED:
        _, r = Sc(K(col, enforced=False)).begin(EnforcementOn())
        no_authz(r, "RC-7 EnforcementOn step 1")


def _rc8(col: Col) -> None:
    for op in (End(), Rotate(), RemoveParticipant(BEN_ID), ExpireJob()):
        sc = Sc(K(col))
        if isinstance(op, ExpireJob):
            sc.run(Expire())
        oid, r = sc.begin(op)
        no_authz(r, f"RC-8 {type(op).__name__} step 1")
        expect(not r.authz_advanced, f"RC-8 {type(op).__name__} step 1 leaves authz_revision")
        r = sc.cont(oid, Phase.RECONCILE)
        expect(r.authz_advanced, f"RC-8 {type(op).__name__} reconcile advances authz_revision")


def _rc9(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Table())
    r = sc.last(Stop(CommandId("stop1"), ONDREY))
    same(r.footprint, (LockLevel.SESSION_ROW, LockLevel.SLOT_ROWS), "RC-9 a Stop holds the session row and slots only")


def _rc10(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Table())
    no_authz(sc.last(Stop(CommandId("stop1"), "all")), "RC-10 Stop")


def _rc11(col: Col) -> None:
    kira2 = Document(DocumentId("kira2"), "oracle_sheet", 1, (version_of(SHEET_V1),), False, None)
    base = set_document(K(col), kira2)
    dan = ParticipantId("dan")
    sc = Sc(base)
    r = sc.last(AddParticipant(dan))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    starts_exclusive(r, "RC-11 AddParticipant")
    r = sc.last(Link(kira2.id, dan))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    same(sc.state.world.documents[kira2.id].linked_participant, dan, "RC-11 the second reads the first")
    fresh = Sc(base)
    raises_usage(lambda: fresh.run(Link(kira2.id, dan)), "RC-11 Link before AddParticipant")


def _rc_superseded(successor: str) -> None:
    superseded_vocabulary_absent()
    expect(successor in {c.id for c in TRUTH_TABLE}, f"successor {successor} is catalogued")


def _rc14(col: Col) -> None:
    for order in ((True, False), (False, True)):
        sc = Sc(K(col))
        results: list[StepResult] = []
        for display_first in order:
            if display_first:
                results += sc.run(sc.compose("ondrey", ["name"], to("ana")))
            else:
                results += sc.run(RemoveParticipant(ANA_ID))
        for r in results:
            expect(in_lock_order(r.footprint), "RC-14 every footprint follows RQ-3")


def _rc15(col: Col) -> None:
    races: list[tuple[Op, str, str, RevealAudience, Slot, StopReason, Callable[[State], bool]]] = [
        (
            Archive(ONDREY),
            "ondrey",
            "name",
            Table(),
            TABLE,
            StopReason.DOCUMENT_ARCHIVED,
            lambda s: s.world.documents[ONDREY].archived,
        ),
        (
            Unlink(KIRA),
            "kira",
            "name",
            to("ana"),
            P("ana"),
            StopReason.CHARACTER_UNLINKED,
            lambda s: s.world.documents[KIRA].linked_participant is None,
        ),
    ]
    if col is Col.ENFORCED:
        races.append(
            (Classify(ONDREY, fkey("history"), CAMP, GMO), "ondrey", "history", to("ana"), P("ana"),
             StopReason.ELIGIBILITY_TIGHTENED, lambda s: cls_of(s, "ondrey", "history") == GMO)
        )  # fmt: skip
    for op, doc, names, audience, slot, reason, fact in races:
        sc = Sc(K(col))
        sc.confirm(doc, names.split(), audience)
        oid, r = sc.begin(op)
        check(r, answer=Ok(), slots={}, events=stp(reason, doc, names, slot), epoch=True, authz=False)
        r = sc.cont(oid, Phase.STEP2, lock=False)
        check(r, answer=NotAppliedYet(), slots={}, events=[], epoch=False, authz=False)
        expect(not fact(sc.state), "RC-15 the fact is unchanged")
        r = sc.cont(oid, Phase.STEP2)
        check(r, answer=Ok(), slots={}, events=[], epoch=True, authz=True)
        expect(fact(sc.state), "RC-15 a retried step 2 applies it")


def _rc16(col: Col) -> None:
    ana_only = FieldClass.participants("ana")
    sc = Sc(K(col))
    first = sc.compose("ondrey", ["notes"], Table(), [cls_entry("notes", U, PUB)])
    second = sc.compose("ondrey", ["notes"], to("ana"), [cls_entry("notes", U, ana_only)])
    r = sc.last(first)
    check(
        r,
        answer=Ok(),
        slots={TABLE: sv("ondrey", "notes")},
        events=disp("ondrey", "notes", TABLE),
        epoch=True,
        authz=True,
    )
    starts_exclusive(r, "RC-16 first")
    r = sc.last(second)
    check(
        r,
        answer=refused(RefusalKind.EPOCH_STALE),
        slots={TABLE: sv("ondrey", "notes")},
        events=[],
        epoch=False,
        authz=False,
    )
    r = sc.last(sc.compose("ondrey", ["notes"], to("ana"), [cls_entry("notes", U, ana_only)]))
    check(
        r,
        answer=refused(RefusalKind.CLASS_MOVED),
        slots={TABLE: sv("ondrey", "notes")},
        events=[],
        epoch=False,
        authz=False,
    )
    starts_exclusive(r, "RC-16 variant")


_I = Disposition.INVARIANT
_F = Disposition.FOOTPRINT
_N = Disposition.NOT_MODELLABLE


def _rc(n: int, title: str, disposition: Disposition, cols: tuple[Column | NoCase, Column | NoCase], **kw: str) -> Case:
    return Case(f"RC-{n}", title, (f"RC-{n}",), disposition, cols[0], cols[1],
                supersession=kw.get("supersession"), owner=kw.get("owner"), successor=kw.get("successor"))  # fmt: skip


RACE_CASES: Final[tuple[Case, ...]] = (
    _rc(1, "a Confirm and a Stop", _M, _both(_rc1)),
    _rc(2, "a Confirm and a fact-changing narrowing", _M, _both(_rc2)),
    _rc(3, "a Confirm and the enforcement switch", _M, _enforced_only(_rc3, EnforcementOn)),
    _rc(4, "two Confirms", _M, _both(_rc4)),
    _rc(5, "a Confirm and an End, a Rotate or expiry", _M, _both(_rc5)),
    _rc(6, "Remove with no live session (P-19)", _I, _both(_rc6), owner="1kg.7.2"),
    _rc(7, "a Stop while a narrowing is in flight", _F, _both(_rc7), owner="1kg.7.2"),
    _rc(8, "a revocation while the lock is held", _F, _both(_rc8), owner="1ir.2.3"),
    _rc(9, "a Stop while every gate slot is held", _N, _both(_rc9), owner="1kg.7.2"),
    _rc(10, "the projector and a Stop", _F, _both(_rc10), owner="1ir.2.3"),
    _rc(11, "two exclusive transactions", _M, _both(_rc11)),
    _rc(
        12,
        "an enrolment and anything else",
        _S,
        (partial(_rc_superseded, "TT-25"), partial(_rc_superseded, "TT-25")),
        supersession="ADR 15.2 retires the enrolment code",
        successor="TT-25",
    ),
    _rc(
        13,
        "a Reset or Remove against an enrolment",
        _S,
        (partial(_rc_superseded, "TT-24"), partial(_rc_superseded, "TT-24")),
        supersession="ADR 15.2 retires enrolment codes and Reset",
        successor="TT-24",
    ),
    _rc(14, "a display against Remove, on a real database", _F, _both(_rc14), owner="1kg.7.2"),
    _rc(15, "a narrowing that cannot get the lock", _M, _both(_rc15)),
    _rc(16, "two classifying Confirms", _M, _enforced_only(_rc16, Classify, probe=_v1_classify_probe)),
)


# ----------------------------------------------------------------------------- ORD, IDEM, extras


def _ord1(col: Col) -> None:
    sc = Sc(K(col))
    staged = sc.compose("ondrey", ["name"], Table())
    sc.run(Archive(ONDREY))
    r = sc.last(staged)
    same(r.step.phase, Phase.COURTESY, "ORD-1 answered at the courtesy")
    check(r, answer=refused(RefusalKind.EPOCH_STALE), slots={}, events=[], epoch=False, authz=False)


def _ord2(col: Col) -> None:
    sc = Sc(K(col))
    oid, _ = sc.begin(sc.compose("ondrey", ["name"], Table()))
    sc.run(Archive(ONDREY))
    r = sc.cont(oid, Phase.COMMIT)
    check(r, answer=refused(RefusalKind.DOCUMENT_ARCHIVED), slots={}, events=[], epoch=False, authz=False)


def _ord3(col: Col) -> None:
    sc = Sc(K(col))
    r = sc.confirm("ondrey", ["tags", "history"], Table())
    check(r, answer=refused(RefusalKind.MASK_INVALID, "tags"), slots={}, events=[], epoch=False, authz=False)
    if col is Col.ENFORCED:
        r = sc.confirm("ondrey", ["tags", "notes"], Table(), [cls_entry("notes", U, PUB)])
        check(r, answer=refused(RefusalKind.MASK_INVALID, "tags"), slots={}, events=[], epoch=False, authz=False)
        same(cls_of(sc.state, "ondrey", "notes"), U, "ORD-3 a refused Confirm writes no class")


def _ord4(col: Col) -> None:
    sc = Sc(K(col))
    sc.run(RemoveParticipant(CY_ID))
    r = sc.confirm("ondrey", ["rumours"], to("ben", "cy"))
    check(r, answer=refused(RefusalKind.AUDIENCE_INVALID), slots={}, events=[], epoch=False, authz=False)


def _ord5(col: Col) -> None:
    sc = Sc(K(col))
    sc.run(Archive(ONDREY))
    r = sc.confirm("ondrey", ["tags"], Table())
    check(r, answer=refused(RefusalKind.DOCUMENT_ARCHIVED), slots={}, events=[], epoch=False, authz=False)


def _ord6(col: Col) -> None:
    sc = Sc(K(col))
    r = sc.confirm("ondrey", ["notes", "tags"], Table(), [cls_entry("notes", PUB, PUB)])
    check(r, answer=refused(RefusalKind.CLASS_MOVED), slots={}, events=[], epoch=False, authz=False)


def _ord7(col: Col) -> None:
    sc = Sc(K(col))
    stale = Confirm(CommandId("c90"), DocumentId("nowhere"), S1, 99, 1, keys("name"), Table())
    r = sc.last(stale)
    check(r, answer=refused(RefusalKind.NOT_FOUND), slots={}, events=[], epoch=False, authz=False)
    sc.run(Edit(ONDREY, _all_present_values()))
    r = sc.confirm("ondrey", ["name"], Table(), version=2)
    check(r, answer=refused(RefusalKind.VERSION_INVALID), slots={}, events=[], epoch=False, authz=False)


def _idem1(col: Col) -> None:
    sc = Sc(K(col))
    first = sc.confirm("ondrey", ["name"], Table())
    r = sc.last(sc.compose("ondrey", ["name", "portrait"], Table(), cid="c1"))
    check(r, answer=Replayed(first.answer), slots={TABLE: sv("ondrey", "name")}, events=[], epoch=False, authz=False)


def _idem2(col: Col) -> None:
    sc = _live_ondrey(col)
    r = sc.last(Stop(CommandId("stop1"), ONDREY))
    expect(r.epoch_advanced, "IDEM-2 first Stop")
    r = sc.last(Stop(CommandId("stop1"), ONDREY))
    check(r, answer=Replayed(Ok()), slots={}, events=[], epoch=False, authz=False)


def _idem3(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("kira", ["name"], to("ana"))
    oid, _ = sc.begin(RemoveParticipant(ANA_ID))
    once = sc.cont(oid, Phase.RECONCILE)
    twice = sc.cont(oid, Phase.RECONCILE)
    check(twice, answer=Ok(), slots=slots_now(once.after), events=[], epoch=False, authz=True)
    for name in ("slots", "disclosures", "grants", "sessions", "history"):
        same(getattr(twice.after, name), getattr(once.after, name), f"IDEM-3 {name}")


def _idem4(col: Col) -> None:
    sc = Sc(K(col))
    sc.run(RemoveParticipant(CY_ID))
    r = sc.last(RemoveParticipant(CY_ID))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=False)
    same(r.after.in_flight[r.step.op_id].allowed, frozenset(), "IDEM-4 no job")


def _idem5(col: Col) -> None:
    sc = Sc(K(col))
    staged = sc.compose("ondrey", ["name"], Table())
    sc.run(Stop(CommandId("stop1"), "all"))
    r = sc.last(staged)
    check(r, answer=refused(RefusalKind.EPOCH_STALE), slots={}, events=[], epoch=False, authz=False)
    r = sc.last(sc.compose("ondrey", ["name"], Table(), cid=staged.command_id))
    check(r, answer=Replayed(refused(RefusalKind.EPOCH_STALE)), slots={}, events=[], epoch=False, authz=False)


def _idem6(col: Col) -> None:
    sc = Sc(K(col))
    one = sc.compose("ondrey", ["name"], Table(), cid="dup")
    other = sc.compose("marsh", ["name"], Table(), cid="dup")
    a, _ = sc.begin(one)
    b, _ = sc.begin(other)
    r = sc.cont(a, Phase.COMMIT)
    check(
        r,
        answer=Ok(),
        slots={TABLE: sv("ondrey", "name")},
        events=disp("ondrey", "name", TABLE),
        epoch=True,
        authz=False,
    )
    r = sc.cont(b, Phase.COMMIT)
    check(r, answer=Replayed(Ok()), slots={TABLE: sv("ondrey", "name")}, events=[], epoch=False, authz=False)


def _replay_kind(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Table())
    r = sc.last(Stop(CommandId("c1"), ONDREY))
    check(r, answer=Ok(), slots={}, events=stp(StopReason.GM_STOP, "ondrey", "name", TABLE), epoch=True, authz=False)


ORDER_CASES: Final[tuple[Case, ...]] = (
    Case("ORD-1", "the courtesy precedes every 422", ("ED-9",), _M, *_both(_ord1)),
    Case("ORD-2", "validation precedes the session row", ("ED-9", "RQ-9"), _M, *_both(_ord2)),
    Case("ORD-3", "the mask before eligibility; nothing is classified", ("ED-9", "ED-13"), _M, *_both(_ord3)),
    Case("ORD-4", "the audience before eligibility", ("ED-9", "I-15"), _M, *_both(_ord4)),
    Case("ORD-5", "archived before the mask", ("ED-9",), _M, *_both(_ord5)),
    Case(
        "ORD-6",
        "a moved class before the mask",
        ("ED-13", "ADR 4"),
        _M,
        *_enforced_only(_ord6, Classify, probe=_v1_classify_probe),
    ),
    Case("ORD-7", "404 before a stale epoch; an unsealed version", ("ED-9",), _M, *_both(_ord7)),
)
IDEMPOTENCY_CASES: Final[tuple[Case, ...]] = (
    Case("IDEM-1", "a Confirm replay", ("wire Idempotency",), _M, *_both(_idem1)),
    Case("IDEM-2", "a Stop replay advances nothing", ("wire Idempotency", "REVEAL-16"), _M, *_both(_idem2)),
    Case("IDEM-3", "reconcile twice equals once", ("RQ-5", "P-19"), _M, *_both(_idem3)),
    Case("IDEM-4", "removing a removed seat", ("SEC-42",), _M, *_both(_idem4)),
    Case("IDEM-5", "a replayed 409 stays 409", ("wire Idempotency",), _M, *_both(_idem5)),
    Case("IDEM-6", "two in-flight Confirms with one command id", ("critic 7(a)",), _M, *_both(_idem6)),
    Case("REPLAY-KIND", "replays are keyed by op kind", ("critic 8", "X-3"), _M, *_both(_replay_kind)),
)


def _expire_narrow(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("kira", ["name"], to("ana"))
    sc.run(Expire())
    _, r = sc.begin(RemoveParticipant(ANA_ID))
    check(
        r,
        answer=Ok(),
        slots={},
        events=stp(StopReason.PARTICIPANT_REMOVED, "kira", "name", P("ana")),
        epoch=True,
        authz=False,
    )


def _expire_end(col: Col) -> None:
    sc = _live_ondrey(col)
    sc.run(Expire())
    _, r = sc.begin(End())
    check(
        r,
        answer=Ok(),
        slots={},
        events=ended(SessionEndReason.EXPIRED, "ondrey", "name portrait", TABLE),
        epoch=True,
        authz=False,
    )


def _delete_archived_first(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("marsh", ["name"], Table())
    r = sc.last(Delete(MARSH))
    check(
        r,
        answer=refused(RefusalKind.DOCUMENT_NOT_ARCHIVED),
        slots={TABLE: sv("marsh", "name")},
        events=[],
        epoch=False,
        authz=False,
    )
    sc.run(Archive(MARSH))
    oid, r = sc.begin(Delete(MARSH))
    check(r, answer=Ok(), slots={}, events=[], epoch=True, authz=False)
    r = sc.cont(oid, Phase.STEP2)
    check(r, answer=Ok(), slots={}, events=[], epoch=True, authz=True)
    expect(MARSH not in sc.state.world.documents, "DELETE-ARCHIVED-FIRST deleted")
    u = Sc(K(col))
    r = u.last(Unarchive(MARSH))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=False)
    same(r.footprint, (), "Unarchive of a live document takes no lock")
    u.run(Archive(MARSH))
    r = u.last(Unarchive(MARSH))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=True)
    expect(not u.state.world.documents[MARSH].archived, "Unarchive clears the flag")


def _classify_cas(col: Col) -> None:
    sc = Sc(K(col))
    r = sc.last(Classify(ONDREY, fkey("notes"), PUB, CAMP))
    check(r, answer=refused(RefusalKind.CLASS_MOVED), slots={}, events=[], epoch=False, authz=False)
    r = sc.last(Classify(ONDREY, fkey("notes"), U, U))
    check(r, answer=Ok(), slots={}, events=[], epoch=False, authz=False)
    sc.confirm("ondrey", ["history"], to("ana"))
    oid, _ = sc.begin(Classify(ONDREY, fkey("history"), CAMP, GMO))
    sc.run(Classify(ONDREY, fkey("history"), CAMP, PUB))
    r = sc.cont(oid, Phase.STEP2)
    check(r, answer=refused(RefusalKind.CLASS_MOVED), slots={}, events=[], epoch=False, authz=False)
    same(cls_of(sc.state, "ondrey", "history"), PUB, "CLASSIFY-CAS the fact is unchanged")
    removed = Sc(set_seat(K(col), Participant(BEN_ID, SeatStatus.REMOVED, AccountId("acct_ben"))))
    r = removed.last(Classify(ONDREY, fkey("notes"), U, FieldClass.participants("ben")))
    check(r, answer=refused(RefusalKind.CLASSIFY_INVALID, "notes"), slots={}, events=[], epoch=False, authz=False)
    same(cls_of(removed.state, "ondrey", "notes"), U, "CLASSIFY-CAS a removed participant is never a class")


def _export_version(col: Col) -> None:
    sc = Sc(K(col))
    _edit_and_seal(sc)
    r = sc.last(Export(ONDREY, 1, keys("name")))
    check(r, answer=refused(RefusalKind.VERSION_INVALID), slots={}, events=[], epoch=False, authz=False)
    sc.confirm("ondrey", ["name"], Table())
    r = sc.last(Export(ONDREY, 1, keys("name")))
    check(
        r, answer=Ok(), slots={TABLE: sv("ondrey", "name")}, events=exported("ondrey", "name"), epoch=False, authz=False
    )
    r = sc.last(Export(ONDREY, 2, keys("name")))
    check(
        r, answer=Ok(), slots={TABLE: sv("ondrey", "name")}, events=exported("ondrey", "name"), epoch=False, authz=False
    )


def _edit_open(col: Col) -> None:
    sc = Sc(K(col))
    for expected in (2, 2):
        sc.run(Edit(ONDREY, _all_present_values()))
        same(len(sc.state.world.documents[ONDREY].versions), expected, "EDIT-OPEN the open version is rewritten")
    sc.run(Seal(ONDREY, 2))
    sc.run(Edit(ONDREY, _all_present_values()))
    same(len(sc.state.world.documents[ONDREY].versions), 3, "EDIT-OPEN a sealed version is never rewritten")


def _unlink_every_copy(col: Col) -> None:
    sc = Sc(K(col))
    audience = to("ana", "ben") if col is Col.V1 else to("ana")
    sc.confirm("kira", ["name"], audience)
    _, r = sc.begin(Unlink(KIRA))
    events = stp(StopReason.CHARACTER_UNLINKED, "kira", "name", P("ana"))
    if col is Col.V1:
        events += stp(StopReason.CHARACTER_UNLINKED, "kira", "name", P("ben"))
    check(r, answer=Ok(), slots={}, events=events, epoch=True, authz=False)


def _everyone_seated(col: Col) -> None:
    state = set_seat(K(col), Participant(BEN_ID, SeatStatus.AWAITING_CONFIRMATION, AccountId("acct_ben")))
    state = set_seat(state, Participant(ParticipantId("dan"), SeatStatus.REMOVED, AccountId("acct_dan")))
    same(expand_audience(state.world, EveryoneSeated()), frozenset({P("ana")}), "EVERYONE-SEATED confirmed seats only")
    r = Sc(state).confirm("ondrey", ["name"], EveryoneSeated())
    check(
        r,
        answer=Ok(),
        slots={P("ana"): sv("ondrey", "name")},
        events=disp("ondrey", "name", P("ana")),
        epoch=True,
        authz=False,
    )
    nobody = set_seat(state, Participant(ANA_ID, SeatStatus.AWAITING_CONFIRMATION, AccountId("acct_ana")))
    r = Sc(nobody).confirm("ondrey", ["name"], EveryoneSeated())
    check(r, answer=refused(RefusalKind.AUDIENCE_INVALID), slots={}, events=[], epoch=False, authz=False)


def _group_active_members(col: Col) -> None:
    removed = Participant(BEN_ID, SeatStatus.REMOVED, AccountId("acct_ben"))
    for enforced in (False, True):
        state = set_seat(K(col, enforced=enforced), removed)
        same(expand_audience(state.world, Group(SCOUTS)), frozenset({P("ana")}), "GROUP-ACTIVE a removed member")
        r = Sc(state).confirm("ondrey", ["name"], Group(SCOUTS))
        check(
            r,
            answer=Ok(),
            slots={P("ana"): sv("ondrey", "name")},
            events=disp("ondrey", "name", P("ana")),
            epoch=True,
            authz=False,
        )
    sc = Sc(K(col))
    sc.confirm("ondrey", ["name"], Group(SCOUTS))
    r = sc.confirm("ondrey", ["name"], to("ana", "ben"))
    both = {P("ana"): sv("ondrey", "name"), P("ben"): sv("ondrey", "name")}
    check(r, answer=Ok(), slots=both, events=[], epoch=True, authz=False)
    same([d.group for d in r.after.disclosures.values()], [SCOUTS], "GROUP-ACTIVE an Update keeps the group")


def _v1_group_probe() -> None:
    Sc(K(Col.V1)).confirm("ondrey", ["name"], Group(SCOUTS))


def _asset_follows_its_field(col: Col) -> None:
    sc = Sc(K(col))
    portrait = fkey("portrait")
    sc.confirm("ondrey", ["name", "portrait"], Table())
    expect(
        asset_readable(sc.state, ANA, ONDREY, portrait) and asset_readable(sc.state, SCREEN, ONDREY, portrait),
        "ASSET shown",
    )
    sc.confirm("ondrey", ["name"], Table())
    expect(not asset_readable(sc.state, ANA, ONDREY, portrait), "ASSET unticked")
    sc.confirm("ondrey", ["portrait"], to("ben"))
    expect(asset_readable(sc.state, BEN, ONDREY, portrait), "ASSET own slot")
    for who in (ANA, SCREEN, GM, STRANGER, NOBODY):
        expect(not asset_readable(sc.state, who, ONDREY, portrait), "ASSET only through an entitled slot")


def _owner_never_holds_a_participant_slot(col: Col) -> None:
    sc = Sc(K(col))
    sc.confirm("kira", ["name"], to("ana"))
    sc.confirm("ondrey", ["name"], to("ben"))
    for pid in sorted(sc.state.world.participants):
        same(ent(sc.state, GM, P(pid)), Entitlement.ABSENT, "ENT-owner")
    expect("mine" not in visible(sc.state, GM), "ENT-owner no own view")


EXTRA_CASES: Final[tuple[Case, ...]] = (
    Case(
        "EXPIRE-NARROW",
        "a narrowing acts on an expired, state-live session",
        ("SEC-42", "L-12"),
        _M,
        *_both(_expire_narrow),
    ),
    Case("EXPIRE-END", "End of an expired session closes it as expired", ("L-5",), _M, *_both(_expire_end)),
    Case(
        "DELETE-ARCHIVED-FIRST",
        "Delete needs an archived document; Unarchive",
        ("LIB-16", "LIB-18"),
        _M,
        *_both(_delete_archived_first),
    ),
    Case(
        "CLASSIFY-CAS",
        "Classify is compare-and-set at both steps, and names active participants only",
        ("ED-13(3)", "1ir.2.1 I-15", "critic item 6"),
        _M,
        *_enforced_only(_classify_cas, Classify),
    ),
    Case(
        "EXPORT-VERSION",
        "an export names the latest sealed or a live version",
        ("EXPORT-3", "EXPORT-8"),
        _M,
        *_both(_export_version),
    ),
    Case("EDIT-OPEN", "Edit rewrites the one open version", ("CANVAS-34",), _M, *_both(_edit_open)),
    Case(
        "UNLINK-EVERY-COPY",
        "an unlink stops every copy of the sheet",
        ("AUD-15", "1kg.7.1 ID-5"),
        _M,
        *_both(_unlink_every_copy),
    ),
    Case("EVERYONE-SEATED", "everyone seated means confirmed seats", ("TP-1", "I-7"), _M, *_both(_everyone_seated)),
    Case("ASSET", "an asset follows its field", ("ED-19",), _M, *_both(_asset_follows_its_field)),
    Case(
        "GROUP-ACTIVE",
        "a named group reaches its active members; an Update keeps the group",
        ("ED-15", "I-11", "I-15", "I-23"),
        _M,
        *_enforced_only(_group_active_members, AddToGroup, RemoveFromGroup, probe=_v1_group_probe),
    ),
    Case(
        "ENT-OWNER", "the owner holds no participant slot", ("15.6",), _M, *_both(_owner_never_holds_a_participant_slot)
    ),
)


# ------------------------------------------------------------------------------ the lattice (I-9)

#: Named class shapes and, for each, the shapes it widens to (I-9). `is_widening` must match exactly.
LATTICE_SHAPES: Final[Mapping[str, FieldClass]] = frozen_map(
    {
        "unclassified": U,
        "gm_only": GMO,
        "participants_a": FieldClass.participants("pa"),
        "participants_ab": FieldClass.participants("pa", "pb"),
        "participants_b": FieldClass.participants("pb"),
        "characters_x": FieldClass.characters("dx"),
        "groups_g": FieldClass.groups("gg"),
        "campaign": CAMP,
        "public": PUB,
    }
)
_EVERY_SHAPE: Final = frozenset(LATTICE_SHAPES)
LATTICE_WIDENS_TO: Final[Mapping[str, frozenset[str]]] = frozen_map(
    {
        "unclassified": _EVERY_SHAPE,
        "gm_only": _EVERY_SHAPE,
        "participants_a": frozenset({"participants_a", "participants_ab", "campaign", "public"}),
        "participants_ab": frozenset({"participants_ab", "campaign", "public"}),
        "participants_b": frozenset({"participants_b", "participants_ab", "campaign", "public"}),
        "characters_x": frozenset({"characters_x", "campaign", "public"}),
        "groups_g": frozenset({"groups_g", "campaign", "public"}),
        "campaign": frozenset({"campaign", "public"}),
        "public": frozenset({"public"}),
    }
)


def lattice_pairs() -> None:
    for old in sorted(LATTICE_SHAPES):
        for new in sorted(LATTICE_SHAPES):
            same(
                is_widening(LATTICE_SHAPES[old], LATTICE_SHAPES[new]),
                new in LATTICE_WIDENS_TO[old],
                f"LATTICE {old} to {new}",
            )


# ----------------------------------------------------------------------- ENT-15.6 (threat model)

ENT_PRINCIPALS: Final = (
    "owner", "seated_confirmed", "seated_awaiting", "other_account", "offered", "removed", "nobody", "screen",
    "dead_grant_account", "dead_grant_nobody", "stale_grant_nobody", "foreign_grant_nobody",
)  # fmt: skip
ENT_SESSIONS: Final = ("live", "ended", "clock_expired", "finalised")
_OWN: Final[Mapping[str, str]] = frozen_map(
    {
        "seated_confirmed": "ana",
        "seated_awaiting": "ben",
        "offered": "cy",
        "removed": "dan",
        "dead_grant_account": "ana",
    }
)
_E = Entitlement.ENTITLED
_X = Entitlement.ABSENT
_IN = Entitlement.INACTIVE
_UN = Entitlement.UNAUTHENTICATED
#: Threat model 15.6's table-operation cells that concern slots, while the session is open.
_OPEN_CELLS: Final[Mapping[tuple[str, str], Entitlement]] = frozen_map(
    {
        ("owner", "table"): _E, ("owner", "another"): _X,
        ("seated_confirmed", "table"): _E, ("seated_confirmed", "own"): _E, ("seated_confirmed", "another"): _X,
        ("seated_awaiting", "table"): _E, ("seated_awaiting", "own"): _X, ("seated_awaiting", "another"): _X,
        ("other_account", "table"): _IN, ("other_account", "another"): _IN,
        ("offered", "table"): _IN, ("offered", "own"): _IN, ("offered", "another"): _IN,
        ("removed", "table"): _IN, ("removed", "own"): _IN, ("removed", "another"): _IN,
        ("nobody", "table"): _UN, ("nobody", "another"): _UN,
        ("screen", "table"): _E, ("screen", "another"): _X,
        ("dead_grant_account", "table"): _E, ("dead_grant_account", "own"): _E, ("dead_grant_account", "another"): _X,
        ("dead_grant_nobody", "table"): _UN, ("dead_grant_nobody", "another"): _UN,
        ("stale_grant_nobody", "table"): _UN, ("stale_grant_nobody", "another"): _UN,
        ("foreign_grant_nobody", "table"): _UN, ("foreign_grant_nobody", "another"): _UN,
    }
)  # fmt: skip
_NO_ACCOUNT: Final = frozenset({"nobody", "screen", "dead_grant_nobody", "stale_grant_nobody", "foreign_grant_nobody"})
ENTITLEMENT_MATRIX: Final[tuple[tuple[str, str, str, Entitlement], ...]] = tuple(
    (principal, session, slot, cell if session == "live" else (_UN if principal in _NO_ACCOUNT else _IN))
    for (principal, slot), cell in sorted(_OPEN_CELLS.items())
    for session in ENT_SESSIONS
)
DEAD_GRANT: Final = GrantId("dead1")
#: An unrevoked grant of the live session's previous generation (the link rotated since): not live (SEC-48).
STALE_GRANT: Final = GrantId("stale1")
#: An unrevoked grant of the ended session `s0`, at the live session's generation: not live (SEC-48).
FOREIGN_GRANT: Final = GrantId("foreign1")


def entitlement_state(session: str) -> State:
    """ENT-15.6's world: Ana confirmed, Ben awaiting, Cy offered, Dan removed; `screen1` live, `dead1`
    revoked, `stale1` of the previous generation and `foreign1` of the ended session `s0` (both
    unrevoked, neither live); then the session live, ended, past `expires_at`, or finalised (critic item 2)."""
    state = set_seat(canonical_state(V1), Participant(BEN_ID, SeatStatus.AWAITING_CONFIRMATION, AccountId("acct_ben")))
    state = set_seat(state, Participant(ParticipantId("dan"), SeatStatus.REMOVED, AccountId("acct_dan")))
    earlier = SessionId("s0")
    generation = state.sessions[S1].generation
    sessions = dict(state.sessions)
    sessions[earlier] = Session(earlier, live=False, expired=False, epoch=0, generation=generation)
    grants = dict(state.grants)
    grants[DEAD_GRANT] = ScreenGrant(DEAD_GRANT, S1, 1, revoked=True)
    grants[STALE_GRANT] = ScreenGrant(STALE_GRANT, S1, generation - 1, revoked=False)
    grants[FOREIGN_GRANT] = ScreenGrant(FOREIGN_GRANT, earlier, generation, revoked=False)
    sc = Sc(replace(state, sessions=frozen_map(sessions), grants=frozen_map(grants)))
    if session == "ended":
        sc.begin(End())
    elif session in ("clock_expired", "finalised"):
        sc.run(Expire())
        if session == "finalised":
            sc.begin(ExpireJob())
    return sc.state


def entitlement_requester(principal: str) -> Requester:
    return {
        "owner": GM,
        "seated_confirmed": ANA,
        "seated_awaiting": BEN,
        "other_account": STRANGER,
        "offered": CY,
        "removed": Requester(AccountId("acct_dan"), None),
        "nobody": NOBODY,
        "screen": SCREEN,
        "dead_grant_account": Requester(AccountId("acct_ana"), DEAD_GRANT),
        "dead_grant_nobody": Requester(None, DEAD_GRANT),
        "stale_grant_nobody": Requester(None, STALE_GRANT),
        "foreign_grant_nobody": Requester(None, FOREIGN_GRANT),
    }[principal]


def entitlement_slot(principal: str, slot: str) -> Slot:
    if slot == "table":
        return TABLE
    own = _OWN.get(principal)
    if slot == "own":
        if own is None:
            raise OracleUsageError("this principal holds no seat")
        return P(own)
    return P("ben" if own == "ana" else "ana")


__all__ = [
    "K",
    "P",
    "keys",
    "to",
    "disp",
    "upd",
    "stp",
    "ended",
    "retracted",
    "exported",
    "sv",
    "slots_now",
    "no_authz",
    "starts_exclusive",
    "refused",
    "cls_entry",
    "set_class",
    "set_seat",
    "set_document",
    "cls_of",
    "ent",
    "raises_usage",
    "DEAD_GRANT",
    "FOREIGN_GRANT",
    "STALE_GRANT",
    "ENTITLEMENT_MATRIX",
    "ENT_PRINCIPALS",
    "ENT_SESSIONS",
    "EXTRA_CASES",
    "IDEMPOTENCY_CASES",
    "LATTICE_SHAPES",
    "LATTICE_WIDENS_TO",
    "ORDER_CASES",
    "RACE_CASES",
    "TRUTH_TABLE",
    "Case",
    "Col",
    "Disposition",
    "NoCase",
    "Sc",
    "check",
    "entitlement_requester",
    "entitlement_slot",
    "entitlement_state",
    "expect",
    "in_lock_order",
    "lattice_pairs",
    "npc_v3_declaring_rumours",
    "same",
    "superseded_vocabulary_absent",
]

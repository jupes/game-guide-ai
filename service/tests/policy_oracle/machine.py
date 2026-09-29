"""The oracle's deterministic state machine over atomic steps (brief sections 5.5 to 5.10).

Each ADR commit unit is one atomic step (I-8): a Confirm is COURTESY then COMMIT, a fact-changing
narrowing is STEP1 then STEP2, a revocation is STEP1 then zero or more RECONCILEs, and everything
else is one ONLY step. A schedule is any interleaving that keeps each op's own phase order.

`apply` is pure: it reads a frozen `State` and returns a new one with the answer, the per-field
events, whether the epoch and `authz_revision` advanced, and the lock footprint. Rules come from the
ADR (sections 4, 5, 9), the threat model (sections 15.2, 15.6) and the adopted critic of the brief.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from typing import Final, Literal

from .model import (
    AccountId,
    Actor,
    Answer,
    ClassKind,
    CommandId,
    Copy,
    Disclosure,
    DisclosureId,
    DocumentId,
    EventKind,
    FieldClass,
    FieldEvent,
    FieldKey,
    GrantId,
    Group,
    GroupId,
    JobRetry,
    NotAppliedYet,
    Ok,
    OpId,
    OracleUsageError,
    Participant,
    ParticipantId,
    RefusalKind,
    Refused,
    Release,
    Replayed,
    RetryLater,
    RevealAudience,
    ScreenGrant,
    SeatStatus,
    Session,
    SessionEndReason,
    SessionId,
    Slot,
    State,
    StopReason,
    TableAudienceId,
    Value,
    Version,
    World,
    frozen_map,
    participant_slot,
    slot_order,
)
from .rules import (
    audience_of_slot,
    eligible_for_audience,
    expand_audience,
    held,
    is_widening,
    narrowest_widening,
    stored_class,
)

# ---------------------------------------------------------------------------------------------- ops


@dataclass(frozen=True)
class ClassifyEntry:
    """One entry of a Confirm's `classify` list (ED-13(1)): the key, the class shown, the new class."""

    key: FieldKey
    displayed: FieldClass
    new: FieldClass


@dataclass(frozen=True)
class Confirm:
    """A reveal Confirm (ED-9, ED-15). It names its session and epoch; the mask is explicit keys."""

    command_id: CommandId
    document_id: DocumentId
    session_id: SessionId
    epoch: int
    version: int
    mask: frozenset[FieldKey]
    audience: RevealAudience
    classify: tuple[ClassifyEntry, ...] = ()


@dataclass(frozen=True)
class Stop:
    """Stop showing a document, or `all` (ED-16, X-3); `retract` is ASSISTANT only."""

    command_id: CommandId
    target: DocumentId | Literal["all"]
    retract: bool = False


@dataclass(frozen=True)
class Export:
    """A player-safe export of ticked keys of one version (ED-21, EXPORT-3)."""

    document_id: DocumentId
    version: int
    keys: frozenset[FieldKey]


@dataclass(frozen=True)
class Classify:
    """The classification surface's compare-and-set (critic item 6, ED-13(3)). ASSISTANT only."""

    document_id: DocumentId
    key: FieldKey
    expected: FieldClass
    new: FieldClass


@dataclass(frozen=True)
class Unlink:
    sheet: DocumentId


@dataclass(frozen=True)
class Relink:
    sheet: DocumentId
    to: ParticipantId


@dataclass(frozen=True)
class Archive:
    document_id: DocumentId


@dataclass(frozen=True)
class Unarchive:
    document_id: DocumentId


@dataclass(frozen=True)
class Delete:
    document_id: DocumentId


@dataclass(frozen=True)
class RemoveFromGroup:
    group: GroupId
    participant: ParticipantId


@dataclass(frozen=True)
class EnforcementOn:
    """M-3: switching enforcement on is a fact-changing narrowing."""


@dataclass(frozen=True)
class End:
    """The GM ends the session: a revocation (RQ-5)."""


@dataclass(frozen=True)
class Expire:
    """The clock passes `expires_at`: no write at all (SEC-42, critic item 2)."""


@dataclass(frozen=True)
class ExpireJob:
    """The expiry job finalises an expired, state-live session (critic item 2)."""


@dataclass(frozen=True)
class Rotate:
    """Rotate: the admission generation advances and old screen grants die (SEC-42)."""


@dataclass(frozen=True)
class RemoveParticipant:
    participant: ParticipantId


@dataclass(frozen=True)
class Start:
    """Session start, a locked widening; two steps when an expired session must be finalised first."""


@dataclass(frozen=True)
class AddParticipant:
    participant: ParticipantId


@dataclass(frozen=True)
class Offer:
    participant: ParticipantId
    account: AccountId


@dataclass(frozen=True)
class Decline:
    participant: ParticipantId


@dataclass(frozen=True)
class Accept:
    participant: ParticipantId


@dataclass(frozen=True)
class ConfirmSeat:
    participant: ParticipantId


@dataclass(frozen=True)
class Link:
    sheet: DocumentId
    participant: ParticipantId


@dataclass(frozen=True)
class AddToGroup:
    group: GroupId
    participant: ParticipantId


@dataclass(frozen=True)
class EnforcementOff:
    """M-8: a locked widening, refused while display automation is on."""


@dataclass(frozen=True)
class SetAutomation:
    on: bool


@dataclass(frozen=True)
class Edit:
    """Rewrite the open version, or append one (critic item 10). Touches no slot (REVEAL-8)."""

    document_id: DocumentId
    values: Mapping[FieldKey, Value]


@dataclass(frozen=True)
class Seal:
    document_id: DocumentId
    version: int


@dataclass(frozen=True)
class MintScreen:
    grant: GrantId


@dataclass(frozen=True)
class RevokeScreen:
    grant: GrantId


@dataclass(frozen=True)
class LeaveScreen:
    grant: GrantId


Op = (
    Confirm
    | Stop
    | Export
    | Classify
    | Unlink
    | Relink
    | Archive
    | Unarchive
    | Delete
    | RemoveFromGroup
    | EnforcementOn
    | End
    | Expire
    | ExpireJob
    | Rotate
    | RemoveParticipant
    | Start
    | AddParticipant
    | Offer
    | Decline
    | Accept
    | ConfirmSeat
    | Link
    | AddToGroup
    | EnforcementOff
    | SetAutomation
    | Edit
    | Seal
    | MintScreen
    | RevokeScreen
    | LeaveScreen
)

ALL_OPS: Final[tuple[type, ...]] = (
    Confirm, Stop, Export, Classify, Unlink, Relink, Archive, Unarchive, Delete, RemoveFromGroup,
    EnforcementOn, End, Expire, ExpireJob, Rotate, RemoveParticipant, Start, AddParticipant, Offer,
    Decline, Accept, ConfirmSeat, Link, AddToGroup, EnforcementOff, SetAutomation, Edit, Seal,
    MintScreen, RevokeScreen, LeaveScreen,
)  # fmt: skip
ASSISTANT_ONLY: Final = frozenset({Classify, RemoveFromGroup, EnforcementOn, AddToGroup, EnforcementOff, SetAutomation})
TWO_STEP_NARROWINGS: Final = frozenset({Unlink, Relink, Archive, Delete, RemoveFromGroup, EnforcementOn})
REVOCATIONS: Final = frozenset({End, ExpireJob, Rotate, RemoveParticipant})


class Phase(Enum):
    COURTESY = "courtesy"
    COMMIT = "commit"
    STEP1 = "step1"
    STEP2 = "step2"
    RECONCILE = "reconcile"
    ONLY = "only"


class LockLevel(Enum):
    """RQ-3's lock order: the campaign row, then participant/document rows, session row, slots, outbox."""

    AUTHZ_SHARED = "authz_shared"
    AUTHZ_EXCLUSIVE = "authz_exclusive"
    ROW = "row"
    SESSION_ROW = "session_row"
    SLOT_ROWS = "slot_rows"
    OUTBOX = "outbox"


#: The RQ-3 level of each lock; both campaign modes share level 0.
LOCK_RANK: Final[Mapping[LockLevel, int]] = frozen_map(
    {
        LockLevel.AUTHZ_SHARED: 0,
        LockLevel.AUTHZ_EXCLUSIVE: 0,
        LockLevel.ROW: 1,
        LockLevel.SESSION_ROW: 2,
        LockLevel.SLOT_ROWS: 3,
        LockLevel.OUTBOX: 4,
    }
)


@dataclass(frozen=True)
class Step:
    """One atomic step of one op. `lock_available=False` models a lock that cannot be had in time."""

    op_id: OpId
    op: Op
    phase: Phase
    lock_available: bool = True


@dataclass(frozen=True)
class InFlight:
    """What an op may do next: the phases allowed for its `op_id` (empty once it has ended)."""

    op: Op
    allowed: frozenset[Phase]


@dataclass(frozen=True)
class StepResult:
    step: Step
    answer: Answer
    before: State
    after: State
    events: tuple[FieldEvent, ...]
    epoch_advanced: bool
    authz_advanced: bool
    footprint: tuple[LockLevel, ...]


@dataclass(frozen=True)
class Trace:
    initial: State
    results: tuple[StepResult, ...]

    @property
    def final(self) -> State:
        return self.results[-1].after if self.results else self.initial


def available(op_type: type, release: Release) -> bool:
    """Whether an op exists in a release (I-6): classes, groups and the switches are ASSISTANT only."""
    return release is Release.ASSISTANT or op_type not in ASSISTANT_ONLY


def phases(op: Op, state: State) -> tuple[Phase, ...]:
    """The op's solitary phase sequence (I-8); RECONCILE appears once for a revocation."""
    if isinstance(op, Confirm):
        return (Phase.COURTESY, Phase.COMMIT)
    if isinstance(op, Classify):
        return (Phase.ONLY,) if is_widening(op.expected, op.new) else (Phase.STEP1, Phase.STEP2)
    if type(op) in TWO_STEP_NARROWINGS:
        return (Phase.STEP1, Phase.STEP2)
    if type(op) in REVOCATIONS:
        return (Phase.STEP1, Phase.RECONCILE)
    if isinstance(op, Start):
        session = state.live_session
        if session is not None and session.expired:
            return (Phase.STEP1, Phase.STEP2)
    return (Phase.ONLY,)


def next_op_id(state: State) -> OpId:
    """The first op id the state has not seen."""
    return OpId(max(state.in_flight, default=-1) + 1)


# ------------------------------------------------------------------------------------ transactions

_END: Final[frozenset[Phase]] = frozenset()
Outcome = tuple[Answer, frozenset[Phase]]


class _Tx:
    """A step's working copy. Built fresh from the frozen state; `finish` freezes the result."""

    def __init__(self, state: State, step: Step) -> None:
        self.state = state
        self.step = step
        world = state.world
        self.release = world.release
        self.participants = dict(world.participants)
        self.documents = dict(world.documents)
        self.groups = dict(world.groups)
        self.classes = dict(world.classes)
        self.enforced = world.enforced
        self.automation = world.automation_enabled
        self.authz_revision = world.authz_revision
        self.sessions = dict(state.sessions)
        self.live = state.live
        self.slots = dict(state.slots)
        self.slot_seq = dict(state.slot_seq)
        self.disclosures = dict(state.disclosures)
        self.grants = dict(state.grants)
        self.commands = dict(state.commands)
        self.in_flight = dict(state.in_flight)
        self.next_disclosure = state.next_disclosure
        self.pending: list[tuple[tuple[int, str], str, int, FieldEvent]] = []
        self.touched: set[Slot] = set()
        self.epochs: set[SessionId] = set()
        self.authz = False
        self.footprint: list[LockLevel] = []

    def world(self) -> World:
        old = self.state.world
        return World(
            release=old.release,
            owner=old.owner,
            participants=frozen_map(self.participants),
            documents=frozen_map(self.documents),
            groups=frozen_map(self.groups),
            classes=frozen_map(self.classes),
            enforced=self.enforced,
            automation_enabled=self.automation,
            types=old.types,
            authz_revision=self.authz_revision,
        )

    def set_world(self, world: World) -> None:
        self.participants = dict(world.participants)
        self.documents = dict(world.documents)
        self.groups = dict(world.groups)
        self.classes = dict(world.classes)
        self.enforced = world.enforced
        self.automation = world.automation_enabled

    def lock(self, level: LockLevel) -> None:
        if level not in self.footprint:
            self.footprint.append(level)

    def advance_epoch(self, session: SessionId) -> None:
        self.epochs.add(session)

    def advance_authz(self) -> None:
        self.authz_revision += 1
        self.authz = True

    def event(
        self,
        kind: EventKind,
        reason: StopReason | SessionEndReason | None,
        document: DocumentId,
        key: FieldKey,
        version: int,
        slot: Slot | None,
        disclosure: DisclosureId | None,
        actor: Actor,
    ) -> None:
        session = None if kind is EventKind.EXPORTED else self.state.live
        ev = FieldEvent(
            seq=0,
            kind=kind,
            reason=reason,
            document=document,
            key=key,
            version=version,
            slot=slot,
            session=session,
            disclosure=disclosure,
            actor=actor,
            ledgered=self.release is Release.ASSISTANT,
        )
        order = (-1, "") if slot is None else slot.sort_key()
        self.pending.append((order, key, len(self.pending), ev))

    def clear_slot(
        self, slot: Slot, kind: EventKind, reason: StopReason | SessionEndReason | None, actor: Actor
    ) -> None:
        """Clear one copy whole, writing one event per live field (ED-12, ED-17)."""
        copy = self.slots.pop(slot)
        for key in sorted(copy.mask):
            self.event(kind, reason, copy.document, key, copy.version, slot, copy.disclosure, actor)
        disclosure = self.disclosures[copy.disclosure]
        remaining = disclosure.slots - {slot}
        if remaining:
            self.disclosures[disclosure.id] = replace(disclosure, slots=remaining)
        else:
            del self.disclosures[disclosure.id]
        self.touched.add(slot)

    def put_copy(self, slot: Slot, copy: Copy) -> None:
        self.slots[slot] = copy
        self.touched.add(slot)

    def new_disclosure_id(self) -> DisclosureId:
        did = DisclosureId(f"k{self.next_disclosure}")
        self.next_disclosure += 1
        return did

    def finish(self, answer: Answer, allowed: frozenset[Phase]) -> StepResult:
        for sid in sorted(self.epochs):
            session = self.sessions[sid]
            self.sessions[sid] = replace(session, epoch=session.epoch + 1)
        for slot in self.touched:
            self.slot_seq[slot] = self.slot_seq.get(slot, 0) + 1
        base = len(self.state.history)
        ordered = sorted(self.pending, key=lambda p: (p[0], p[1], p[2]))
        events = tuple(replace(p[3], seq=base + i + 1) for i, p in enumerate(ordered))
        self.in_flight[self.step.op_id] = InFlight(self.step.op, allowed)
        after = State(
            world=self.world(),
            sessions=frozen_map(self.sessions),
            live=self.live,
            slots=frozen_map(self.slots),
            slot_seq=frozen_map(self.slot_seq),
            disclosures=frozen_map(self.disclosures),
            grants=frozen_map(self.grants),
            history=self.state.history + events,
            commands=frozen_map(self.commands),
            in_flight=frozen_map(self.in_flight),
            next_disclosure=self.next_disclosure,
        )
        return StepResult(
            step=self.step,
            answer=answer,
            before=self.state,
            after=after,
            events=events,
            epoch_advanced=bool(self.epochs),
            authz_advanced=self.authz,
            footprint=tuple(self.footprint),
        )


def _as[T](op: object, cls: type[T]) -> T:
    if not isinstance(op, cls):
        raise OracleUsageError("a handler met another op type")
    return op


def _active(tx: _Tx, participant: ParticipantId) -> bool:
    seat = tx.participants.get(participant)
    return seat is not None and seat.active


def _copies_of(tx: _Tx, document_id: DocumentId) -> set[Slot]:
    return {s for s, c in tx.slots.items() if c.document == document_id}


def _ineligible_slots(world: World, slots: Mapping[Slot, Copy]) -> set[Slot]:
    """ED-12's Enforced scan: every live copy holding a key not eligible for its slot's audience."""
    out: set[Slot] = set()
    for slot, copy in slots.items():
        audience = audience_of_slot(slot)
        if any(not eligible_for_audience(world, copy.document, k, audience).allowed for k in sorted(copy.mask)):
            out.add(slot)
    return out


# ------------------------------------------------------------------------------------------ Confirm


def _h_confirm(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Confirm)
    key = ("confirm", op.command_id)
    if step.phase is Phase.COURTESY:
        if not op.mask:
            raise OracleUsageError("an empty mask is a Stop at the client (REVEAL-5)")
        if tx.release is Release.WORKBENCH_V1 and (op.classify or isinstance(op.audience, Group)):
            raise OracleUsageError("v1 has no classify list and no named group (critic 7(c))")
        if len({e.key for e in op.classify}) != len(op.classify):
            raise OracleUsageError("a classify list names each key once")
        if key in tx.commands:
            return Replayed(tx.commands[key]), _END
        refusal = _courtesy(tx, op)
        if refusal is not None:
            tx.commands[key] = refusal
            return refusal, _END
        return Ok(), frozenset({Phase.COMMIT})
    if key in tx.commands:
        return Replayed(tx.commands[key]), _END
    if not step.lock_available:
        return RetryLater(), _END
    tx.lock(LockLevel.AUTHZ_EXCLUSIVE if op.classify else LockLevel.AUTHZ_SHARED)
    answer = _commit(tx, op)
    tx.commands[key] = answer
    return answer, _END


def _courtesy(tx: _Tx, op: Confirm) -> Answer | None:
    """ED-9's courtesy, read without a lock: the 404s, then a session or epoch already stale is a 409."""
    if op.document_id not in tx.documents:
        return Refused(RefusalKind.NOT_FOUND)
    session = tx.sessions.get(op.session_id)
    if session is None:
        return Refused(RefusalKind.NOT_FOUND)
    if tx.live != session.id or not session.open:
        return Refused(RefusalKind.SESSION_NOT_LIVE)
    if session.epoch != op.epoch:
        return Refused(RefusalKind.EPOCH_STALE)
    return None


def _classify_entry_ok(world: World, op: Confirm, entry: ClassifyEntry, slots: frozenset[Slot] | None) -> bool:
    """ED-13(4), M-4, I-10, critic 7(e): masked, revealable, not gm_only, needed, and the narrowest."""
    if entry.key not in op.mask or entry.key not in world.revealable(op.document_id):
        return False
    stored = stored_class(world, op.document_id, entry.key)
    if stored.kind is ClassKind.GM_ONLY or not slots:
        return False
    if all(eligible_for_audience(world, op.document_id, entry.key, audience_of_slot(s)).allowed for s in slots):
        return False
    return entry.new == narrowest_widening(world, stored, slots)


def _commit(tx: _Tx, op: Confirm) -> Answer:
    """ED-9 and ADR section 4 `display`, in the brief's precedence (section 5.6, critic item 7)."""
    world = tx.world()
    doc = world.documents.get(op.document_id)
    if doc is None:
        return Refused(RefusalKind.NOT_FOUND)
    slots = expand_audience(world, op.audience)
    if op.classify:
        listed = tuple(sorted({e.key for e in op.classify}))
        if not world.enforced:
            return Refused(RefusalKind.CLASSIFY_INVALID, listed)
        if any(e.displayed != stored_class(world, doc.id, e.key) for e in op.classify):
            return Refused(RefusalKind.CLASS_MOVED)
        bad = tuple(sorted({e.key for e in op.classify if not _classify_entry_ok(world, op, e, slots)}))
        if bad:
            return Refused(RefusalKind.CLASSIFY_INVALID, bad)
        classes = dict(world.classes)
        for e in op.classify:
            classes[(doc.id, e.key)] = e.new
        world = replace(world, classes=frozen_map(classes))
    if doc.archived:
        return Refused(RefusalKind.DOCUMENT_ARCHIVED)
    version = doc.version(op.version)
    if version is None or not version.sealed:
        return Refused(RefusalKind.VERSION_INVALID)
    allow = world.revealable(doc.id)
    bad_mask = tuple(sorted(k for k in op.mask if k not in allow or version.values.get(k) is not Value.PRESENT))
    if bad_mask:
        return Refused(RefusalKind.MASK_INVALID, bad_mask)
    if not slots:
        return Refused(RefusalKind.AUDIENCE_INVALID)
    if world.enforced:
        ineligible = {
            k
            for s in slots
            for k in op.mask
            if not eligible_for_audience(world, doc.id, k, audience_of_slot(s)).allowed
        }
        if ineligible:
            return Refused(RefusalKind.NOT_ELIGIBLE, tuple(sorted(ineligible)))
    tx.lock(LockLevel.SESSION_ROW)
    session = tx.sessions[op.session_id]
    if tx.live != session.id or not session.open:
        return Refused(RefusalKind.SESSION_NOT_LIVE)
    if session.epoch != op.epoch:
        return Refused(RefusalKind.EPOCH_STALE)
    if op.classify:
        tx.set_world(world)
        tx.advance_authz()
    tx.lock(LockLevel.SLOT_ROWS)
    _write_disclosure(tx, op, slots)
    tx.advance_epoch(session.id)
    return Ok()


def _write_disclosure(tx: _Tx, op: Confirm, targets: frozenset[Slot]) -> None:
    """ED-15 and I-11: an Update keeps the disclosure; any other target set ends it and starts anew."""
    doc_id = op.document_id
    current = next((tx.disclosures[d] for d in sorted(tx.disclosures) if tx.disclosures[d].document == doc_id), None)
    group = op.audience.id if isinstance(op.audience, Group) else None
    if current is not None and current.slots == targets:
        for slot in slot_order(targets):
            old = tx.slots[slot]
            for k in sorted(op.mask - old.mask):
                tx.event(EventKind.DISPLAYED, None, doc_id, k, op.version, slot, current.id, Actor.GM)
            if op.version != old.version:
                for k in sorted(op.mask & old.mask):
                    tx.event(EventKind.UPDATED, None, doc_id, k, op.version, slot, current.id, Actor.GM)
            for k in sorted(old.mask - op.mask):
                tx.event(
                    EventKind.STOPPED, StopReason.MASK_NARROWED, doc_id, k, old.version, slot, current.id, Actor.GM
                )
            tx.put_copy(slot, Copy(current.id, doc_id, op.version, op.mask))
        tx.disclosures[current.id] = replace(current, group=group if group is not None else current.group)
        return
    if current is not None:
        for slot in slot_order(current.slots):
            tx.clear_slot(slot, EventKind.STOPPED, StopReason.MOVED, Actor.GM)
    new_id = tx.new_disclosure_id()
    for slot in slot_order(targets):
        if slot in tx.slots:
            tx.clear_slot(slot, EventKind.STOPPED, StopReason.REPLACED, Actor.GM)
        tx.put_copy(slot, Copy(new_id, doc_id, op.version, op.mask))
        for k in sorted(op.mask):
            tx.event(EventKind.DISPLAYED, None, doc_id, k, op.version, slot, new_id, Actor.GM)
    tx.disclosures[new_id] = Disclosure(new_id, doc_id, targets, group)


# ------------------------------------------------------------------------------------ Stop, Export


def _h_stop(tx: _Tx, step: Step) -> Outcome:
    """ED-16, X-3, REVEAL-22, RQ-6: never refused for state; the epoch advances on an empty slot too."""
    op = _as(step.op, Stop)
    if op.retract and tx.release is Release.WORKBENCH_V1:
        raise OracleUsageError("v1 has no Retract (ED-16)")
    key = ("stop", op.command_id)
    if key in tx.commands:
        return Replayed(tx.commands[key]), _END
    answer: Answer = Ok()
    if op.target != "all" and op.target not in tx.documents:
        answer = Refused(RefusalKind.NOT_FOUND)
    elif tx.live is not None:
        tx.lock(LockLevel.SESSION_ROW)
        tx.lock(LockLevel.SLOT_ROWS)
        everything = op.target == "all"
        for slot in slot_order(tx.slots):
            if everything or tx.slots[slot].document == op.target:
                if op.retract:
                    tx.clear_slot(slot, EventKind.RETRACTED, None, Actor.GM)
                else:
                    reason = StopReason.STOP_ALL if everything else StopReason.GM_STOP
                    tx.clear_slot(slot, EventKind.STOPPED, reason, Actor.GM)
        tx.advance_epoch(tx.live)
    tx.commands[key] = answer
    return answer, _END


def _h_export(tx: _Tx, step: Step) -> Outcome:
    """ED-21, EXPORT-3, EXPORT-8 (critic item 9): the same rule as a table display; never classifies."""
    op = _as(step.op, Export)
    world = tx.world()
    doc = world.documents.get(op.document_id)
    if doc is None:
        return Refused(RefusalKind.NOT_FOUND), _END
    if doc.archived:
        raise OracleUsageError("no record decides exporting an archived document (1kg.6.6)")
    if not op.keys:
        raise OracleUsageError("an export names at least one key")
    if world.enforced:
        if not step.lock_available:
            return RetryLater(), _END
        tx.lock(LockLevel.AUTHZ_SHARED)
    live_versions = {c.version for c in tx.slots.values() if c.document == doc.id}
    version = doc.version(op.version)
    if version is None or not (
        (version.number == doc.latest.number and version.sealed) or version.number in live_versions
    ):
        return Refused(RefusalKind.VERSION_INVALID), _END
    allow = world.revealable(doc.id)
    bad = tuple(sorted(k for k in op.keys if k not in allow or version.values.get(k) is not Value.PRESENT))
    if bad:
        return Refused(RefusalKind.MASK_INVALID, bad), _END
    if world.enforced:
        ineligible = tuple(
            sorted(k for k in op.keys if not eligible_for_audience(world, doc.id, k, TableAudienceId()).allowed)
        )
        if ineligible:
            return Refused(RefusalKind.NOT_ELIGIBLE, ineligible), _END
    for k in sorted(op.keys):
        tx.event(EventKind.EXPORTED, None, doc.id, k, version.number, None, None, Actor.GM)
    return Ok(), _END


# -------------------------------------------------------------------------------------- narrowings


@dataclass(frozen=True)
class _Narrowing:
    """A fact-changing narrowing (ED-12, RQ-5): its reason, its row, its fact, its own stop-set."""

    reason: StopReason
    row: bool
    change: Callable[[World], World]
    own: Callable[[_Tx], set[Slot]]
    recheck: Callable[[_Tx], Answer | None]


def _no_own(tx: _Tx) -> set[Slot]:
    return set()


def _narrow_step1(tx: _Tx, n: _Narrowing) -> Outcome:
    """Step 1: never the campaign lock, never refused. Stop what the hypothetical post-change world
    makes invalid, whole copies, and advance the epoch of a state-live session, on an empty slot too."""
    hypothetical = n.change(tx.world())
    targets = n.own(tx)
    if hypothetical.enforced:
        targets |= _ineligible_slots(hypothetical, tx.slots)
    if n.row:
        tx.lock(LockLevel.ROW)
    if tx.live is not None:
        tx.lock(LockLevel.SESSION_ROW)
        tx.lock(LockLevel.SLOT_ROWS)
        for slot in slot_order(targets):
            tx.clear_slot(slot, EventKind.STOPPED, n.reason, Actor.GM)
        tx.advance_epoch(tx.live)
    return Ok(), frozenset({Phase.STEP2})


def _narrow_step2(tx: _Tx, step: Step, n: _Narrowing) -> Outcome:
    """Step 2 (critic item 3): under the exclusive lock, change the fact, rescan the current world, stop
    what it finds and advance the epoch of a state-live session; then `authz_revision`. A fact that
    already holds is Ok and nothing else; no lock is `NotAppliedYet` (RQ-5, RC-15)."""
    if not step.lock_available:
        return NotAppliedYet(), frozenset({Phase.STEP2})
    tx.lock(LockLevel.AUTHZ_EXCLUSIVE)
    check = n.recheck(tx)
    if check is not None:
        return check, _END
    changed = n.change(tx.world())
    tx.set_world(changed)
    targets = n.own(tx)
    if changed.enforced:
        targets |= _ineligible_slots(changed, tx.slots)
    if tx.live is not None:
        tx.lock(LockLevel.SESSION_ROW)
        tx.lock(LockLevel.SLOT_ROWS)
        for slot in slot_order(targets):
            tx.clear_slot(slot, EventKind.STOPPED, n.reason, Actor.GM)
        tx.advance_epoch(tx.live)
    tx.advance_authz()
    return Ok(), _END


def _relinked(world: World, sheet: DocumentId, to: ParticipantId | None) -> World:
    docs = dict(world.documents)
    docs[sheet] = replace(docs[sheet], linked_participant=to)
    return replace(world, documents=frozen_map(docs))


def _run_narrowing(tx: _Tx, step: Step, n: _Narrowing, first: Callable[[], Answer | None]) -> Outcome:
    if step.phase is Phase.STEP1:
        answer = first()
        if answer is not None:
            return answer, _END
        return _narrow_step1(tx, n)
    return _narrow_step2(tx, step, n)


def _h_unlink(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Unlink)

    def recheck(t: _Tx) -> Answer | None:
        doc = t.documents.get(op.sheet)
        if doc is None:
            return Refused(RefusalKind.NOT_FOUND)
        return Ok() if doc.linked_participant is None else None

    n = _Narrowing(
        StopReason.CHARACTER_UNLINKED,
        True,
        lambda w: _relinked(w, op.sheet, None),
        lambda t: _copies_of(t, op.sheet),
        recheck,
    )
    return _run_narrowing(tx, step, n, lambda: recheck(tx))


def _h_relink(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Relink)

    def first() -> Answer | None:
        doc = tx.documents.get(op.sheet)
        if doc is None:
            return Refused(RefusalKind.NOT_FOUND)
        if doc.linked_participant is None or doc.linked_participant == op.to:
            raise OracleUsageError("Relink needs a linked sheet and a new participant (critic item 3)")
        if not _active(tx, op.to) or any(d.linked_participant == op.to for d in tx.documents.values()):
            raise OracleUsageError("Relink to an inactive participant or one who has a sheet")
        return None

    def recheck(t: _Tx) -> Answer | None:
        doc = t.documents.get(op.sheet)
        if doc is None or not _active(t, op.to):
            return Refused(RefusalKind.NOT_FOUND)
        if doc.linked_participant == op.to:
            return Ok()
        if any(d.linked_participant == op.to for d in t.documents.values()):
            return Refused(RefusalKind.LINK_TAKEN)
        return None

    n = _Narrowing(
        StopReason.CHARACTER_UNLINKED,
        True,
        lambda w: _relinked(w, op.sheet, op.to),
        lambda t: _copies_of(t, op.sheet),
        recheck,
    )
    return _run_narrowing(tx, step, n, first)


def _h_archive(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Archive)

    def change(w: World) -> World:
        docs = dict(w.documents)
        docs[op.document_id] = replace(docs[op.document_id], archived=True)
        return replace(w, documents=frozen_map(docs))

    def recheck(t: _Tx) -> Answer | None:
        doc = t.documents.get(op.document_id)
        if doc is None:
            return Refused(RefusalKind.NOT_FOUND)
        return Ok() if doc.archived else None

    n = _Narrowing(StopReason.DOCUMENT_ARCHIVED, True, change, lambda t: _copies_of(t, op.document_id), recheck)
    return _run_narrowing(tx, step, n, lambda: recheck(tx))


def _h_delete(tx: _Tx, step: Step) -> Outcome:
    """Critic item 4: only an archived document is deleted; the fact removes it, its link and its rows."""
    op = _as(step.op, Delete)

    def change(w: World) -> World:
        docs = {k: v for k, v in w.documents.items() if k != op.document_id}
        classes = {k: v for k, v in w.classes.items() if k[0] != op.document_id}
        return replace(w, documents=frozen_map(docs), classes=frozen_map(classes))

    def recheck(t: _Tx) -> Answer | None:
        doc = t.documents.get(op.document_id)
        if doc is None:
            return Refused(RefusalKind.NOT_FOUND)
        return None if doc.archived else Refused(RefusalKind.DOCUMENT_NOT_ARCHIVED)

    n = _Narrowing(StopReason.DOCUMENT_DELETED, True, change, lambda t: _copies_of(t, op.document_id), recheck)
    return _run_narrowing(tx, step, n, lambda: recheck(tx))


def _classify_first(tx: _Tx, op: Classify) -> Answer | None:
    """Critic item 6, in order: 404; key off the allowlist; an invalid `new`; CAS; already `new`."""
    world = tx.world()
    if op.document_id not in world.documents:
        return Refused(RefusalKind.NOT_FOUND)
    invalid = Refused(RefusalKind.CLASSIFY_INVALID, (op.key,))
    if op.key not in world.revealable(op.document_id):
        return invalid
    new = op.new
    if new.kind is ClassKind.PARTICIPANTS and not all(_active(tx, ParticipantId(i)) for i in new.ids):
        return invalid
    if new.kind is ClassKind.CHARACTERS:
        for i in new.ids:
            doc = world.documents.get(DocumentId(i))
            if doc is None or world.types[doc.type_id].audience != "owner":
                return invalid
    if new.kind is ClassKind.GROUPS and not all(GroupId(i) in world.groups for i in new.ids):
        return invalid
    stored = stored_class(world, op.document_id, op.key)
    if stored != op.expected:
        return Refused(RefusalKind.CLASS_MOVED)
    if stored == op.new:
        return Ok()
    return None


def _h_classify(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Classify)
    cell = (op.document_id, op.key)

    def change(w: World) -> World:
        classes = dict(w.classes)
        classes[cell] = op.new
        return replace(w, classes=frozen_map(classes))

    def recheck(t: _Tx) -> Answer | None:
        world = t.world()
        if op.document_id not in world.documents:
            return Refused(RefusalKind.NOT_FOUND)
        stored = stored_class(world, op.document_id, op.key)
        if stored == op.new:
            return Ok()
        if stored != op.expected:
            return Refused(RefusalKind.CLASS_MOVED)
        return None

    if step.phase is Phase.ONLY:
        answer = _classify_first(tx, op)
        if answer is not None:
            return answer, _END
        if not step.lock_available:
            return RetryLater(), _END
        tx.lock(LockLevel.AUTHZ_EXCLUSIVE)
        tx.classes[cell] = op.new
        tx.advance_authz()
        return Ok(), _END
    n = _Narrowing(StopReason.ELIGIBILITY_TIGHTENED, False, change, _no_own, recheck)
    return _run_narrowing(tx, step, n, lambda: _classify_first(tx, op))


def _h_remove_from_group(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, RemoveFromGroup)

    def change(w: World) -> World:
        groups = dict(w.groups)
        groups[op.group] = groups[op.group] - {op.participant}
        return replace(w, groups=frozen_map(groups))

    def own(t: _Tx) -> set[Slot]:
        slot = participant_slot(op.participant)
        copy = t.slots.get(slot)
        if copy is not None and t.disclosures[copy.disclosure].group == op.group:
            return {slot}
        return set()

    def first() -> Answer | None:
        if op.group not in tx.groups or op.participant not in tx.participants:
            return Refused(RefusalKind.NOT_FOUND)
        return None

    def recheck(t: _Tx) -> Answer | None:
        return None if op.participant in t.groups[op.group] else Ok()

    n = _Narrowing(StopReason.GROUP_MEMBER_REMOVED, True, change, own, recheck)
    return _run_narrowing(tx, step, n, first)


def _h_enforcement_on(tx: _Tx, step: Step) -> Outcome:
    _as(step.op, EnforcementOn)

    def recheck(t: _Tx) -> Answer | None:
        return Ok() if t.enforced else None

    n = _Narrowing(StopReason.ENFORCEMENT_ENABLED, False, lambda w: replace(w, enforced=True), _no_own, recheck)
    return _run_narrowing(tx, step, n, lambda: recheck(tx))


# ------------------------------------------------------------------------------------- revocations


def _finalise(tx: _Tx, sid: SessionId) -> None:
    """Critic item 2: close an expired, state-live session: `session_ended(expired)`, epoch, grants."""
    tx.lock(LockLevel.SESSION_ROW)
    tx.lock(LockLevel.SLOT_ROWS)
    for slot in slot_order(tx.slots):
        tx.clear_slot(slot, EventKind.SESSION_ENDED, SessionEndReason.EXPIRED, Actor.SYSTEM)
    tx.sessions[sid] = replace(tx.sessions[sid], live=False)
    tx.live = None
    tx.advance_epoch(sid)
    for gid in sorted(tx.grants):
        grant = tx.grants[gid]
        if grant.session == sid and not grant.revoked:
            tx.grants[gid] = replace(grant, revoked=True)
    tx.lock(LockLevel.OUTBOX)


def _reconcile(tx: _Tx, step: Step) -> Outcome:
    """RQ-5's reconciliation (`campaign.reconcile`): reads current state only; idempotent (P-19)."""
    if not step.lock_available:
        return JobRetry(), frozenset({Phase.RECONCILE})
    tx.lock(LockLevel.AUTHZ_EXCLUSIVE)
    sid = tx.live
    generation: int | None = None
    if sid is not None:
        session = tx.sessions[sid]
        generation = session.generation
        if not session.open:
            victims = slot_order(tx.slots)
            for slot in victims:
                tx.lock(LockLevel.SESSION_ROW)
                tx.lock(LockLevel.SLOT_ROWS)
                tx.clear_slot(slot, EventKind.SESSION_ENDED, SessionEndReason.EXPIRED, Actor.SYSTEM)
        else:
            victims = [s for s in slot_order(tx.slots) if s.participant is not None and not _active(tx, s.participant)]
            for slot in victims:
                tx.lock(LockLevel.SESSION_ROW)
                tx.lock(LockLevel.SLOT_ROWS)
                tx.clear_slot(slot, EventKind.STOPPED, StopReason.PARTICIPANT_REMOVED, Actor.SYSTEM)
        if victims:
            tx.advance_epoch(sid)
    for gid in sorted(tx.grants):
        grant = tx.grants[gid]
        if not grant.revoked and not (grant.session == sid and grant.generation == generation):
            tx.grants[gid] = replace(grant, revoked=True)
    tx.advance_authz()
    return Ok(), frozenset({Phase.RECONCILE})


def _h_end(tx: _Tx, step: Step) -> Outcome:
    _as(step.op, End)
    if step.phase is Phase.RECONCILE:
        return _reconcile(tx, step)
    sid = tx.live
    if sid is None:
        return Ok(), _END
    if tx.sessions[sid].expired:
        _finalise(tx, sid)
        return Ok(), frozenset({Phase.RECONCILE})
    tx.lock(LockLevel.SESSION_ROW)
    tx.lock(LockLevel.SLOT_ROWS)
    for slot in slot_order(tx.slots):
        tx.clear_slot(slot, EventKind.SESSION_ENDED, SessionEndReason.GM_END, Actor.GM)
    tx.sessions[sid] = replace(tx.sessions[sid], live=False)
    tx.live = None
    tx.advance_epoch(sid)
    tx.lock(LockLevel.OUTBOX)
    return Ok(), frozenset({Phase.RECONCILE})


def _h_rotate(tx: _Tx, step: Step) -> Outcome:
    _as(step.op, Rotate)
    if step.phase is Phase.RECONCILE:
        return _reconcile(tx, step)
    sid = tx.live
    if sid is None:
        return Ok(), _END
    session = tx.sessions[sid]
    if session.expired:
        _finalise(tx, sid)
        return Ok(), frozenset({Phase.RECONCILE})
    tx.lock(LockLevel.SESSION_ROW)
    tx.lock(LockLevel.SLOT_ROWS)
    generation = session.generation + 1
    tx.sessions[sid] = replace(session, generation=generation)
    for gid in sorted(tx.grants):
        grant = tx.grants[gid]
        if grant.session == sid and grant.generation != generation and not grant.revoked:
            tx.grants[gid] = replace(grant, revoked=True)
    for slot in slot_order(tx.slots):
        tx.clear_slot(slot, EventKind.STOPPED, StopReason.LINK_ROTATED, Actor.GM)
    tx.advance_epoch(sid)
    tx.lock(LockLevel.OUTBOX)
    return Ok(), frozenset({Phase.RECONCILE})


def _h_expire_job(tx: _Tx, step: Step) -> Outcome:
    _as(step.op, ExpireJob)
    if step.phase is Phase.RECONCILE:
        return _reconcile(tx, step)
    sid = tx.live
    if sid is None or not tx.sessions[sid].expired:
        return Ok(), _END
    _finalise(tx, sid)
    return Ok(), frozenset({Phase.RECONCILE})


def _h_remove_participant(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, RemoveParticipant)
    if step.phase is Phase.RECONCILE:
        return _reconcile(tx, step)
    seat = tx.participants.get(op.participant)
    if seat is None:
        return Refused(RefusalKind.NOT_FOUND), _END
    if not seat.active:
        return Ok(), _END
    tx.lock(LockLevel.ROW)
    tx.participants[op.participant] = Participant(op.participant, SeatStatus.REMOVED, seat.account)
    if tx.live is not None:
        tx.lock(LockLevel.SESSION_ROW)
        tx.lock(LockLevel.SLOT_ROWS)
        slot = participant_slot(op.participant)
        if slot in tx.slots:
            tx.clear_slot(slot, EventKind.STOPPED, StopReason.PARTICIPANT_REMOVED, Actor.GM)
        tx.advance_epoch(tx.live)
    tx.lock(LockLevel.OUTBOX)
    return Ok(), frozenset({Phase.RECONCILE})


def _h_expire(tx: _Tx, step: Step) -> Outcome:
    """SEC-42: the clock alone. No write, no event, no epoch, no lock (critic item 2)."""
    _as(step.op, Expire)
    sid = tx.live
    if sid is not None and not tx.sessions[sid].expired:
        tx.sessions[sid] = replace(tx.sessions[sid], expired=True)
    return Ok(), _END


# ------------------------------------------------------------------------------ locked widenings


def _widen(tx: _Tx, step: Step) -> bool:
    """RQ-4: a locked widening takes the campaign lock exclusively, first. False means `RetryLater`."""
    if not step.lock_available:
        return False
    tx.lock(LockLevel.AUTHZ_EXCLUSIVE)
    return True


def _h_start(tx: _Tx, step: Step) -> Outcome:
    _as(step.op, Start)
    if step.phase is Phase.STEP1:
        sid = tx.live
        if sid is None:
            raise OracleUsageError("Start's first step needs an expired session to finalise")
        _finalise(tx, sid)
        return Ok(), frozenset({Phase.STEP2})
    if tx.live is not None:
        raise OracleUsageError("Start while a live, unexpired session exists")
    if not _widen(tx, step):
        return RetryLater(), _END
    number = len(tx.sessions) + 1
    sid = SessionId(f"s{number}")
    while sid in tx.sessions:
        number += 1
        sid = SessionId(f"s{number}")
    tx.sessions[sid] = Session(sid, live=True, expired=False, epoch=0, generation=1)
    tx.live = sid
    tx.advance_authz()
    return Ok(), _END


def _seat(tx: _Tx, participant: ParticipantId, *allowed: SeatStatus) -> Participant:
    seat = tx.participants.get(participant)
    if seat is None or seat.status not in allowed:
        raise OracleUsageError("the seat is not in a status this op accepts (1kg.2.2)")
    return seat


def _h_add_participant(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, AddParticipant)
    if op.participant in tx.participants:
        raise OracleUsageError("the participant id exists")
    if not _widen(tx, step):
        return RetryLater(), _END
    tx.participants[op.participant] = Participant(op.participant, SeatStatus.OPEN, None)
    tx.advance_authz()
    return Ok(), _END


def _h_offer(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Offer)
    _seat(tx, op.participant, SeatStatus.OPEN, SeatStatus.NOT_ACCEPTED)
    if op.account == tx.state.world.owner or any(
        (p.account == op.account and p.status in (SeatStatus.AWAITING_CONFIRMATION, SeatStatus.CONFIRMED))
        or p.offered_to == op.account
        for p in tx.participants.values()
    ):
        raise OracleUsageError("SEC-50(2): the owner, or an account with a live seat or offer")
    if not _widen(tx, step):
        return RetryLater(), _END
    tx.participants[op.participant] = Participant(op.participant, SeatStatus.OFFERED, None, op.account)
    return Ok(), _END


def _h_decline(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Decline)
    _seat(tx, op.participant, SeatStatus.OFFERED)
    if not _widen(tx, step):
        return RetryLater(), _END
    tx.participants[op.participant] = Participant(op.participant, SeatStatus.NOT_ACCEPTED, None)
    return Ok(), _END


def _h_accept(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Accept)
    seat = _seat(tx, op.participant, SeatStatus.OFFERED)
    if not _widen(tx, step):
        return RetryLater(), _END
    tx.participants[op.participant] = Participant(op.participant, SeatStatus.AWAITING_CONFIRMATION, seat.offered_to)
    tx.advance_authz()
    return Ok(), _END


def _h_confirm_seat(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, ConfirmSeat)
    seat = _seat(tx, op.participant, SeatStatus.AWAITING_CONFIRMATION, SeatStatus.CONFIRMED)
    if seat.status is SeatStatus.CONFIRMED:
        return Ok(), _END
    if not _widen(tx, step):
        return RetryLater(), _END
    tx.participants[op.participant] = replace(seat, status=SeatStatus.CONFIRMED)
    tx.advance_authz()
    return Ok(), _END


def _h_link(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Link)
    doc = tx.documents.get(op.sheet)
    if doc is None or tx.state.world.types[doc.type_id].audience != "owner" or doc.linked_participant is not None:
        raise OracleUsageError("Link needs an existing, unlinked owner-audience document")
    if not _active(tx, op.participant) or any(d.linked_participant == op.participant for d in tx.documents.values()):
        raise OracleUsageError("Link to an inactive participant or one who has a sheet")
    if not _widen(tx, step):
        return RetryLater(), _END
    tx.documents[op.sheet] = replace(doc, linked_participant=op.participant)
    tx.advance_authz()
    return Ok(), _END


def _h_add_to_group(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, AddToGroup)
    if not _active(tx, op.participant):
        raise OracleUsageError("AddToGroup of an inactive participant")
    members = tx.groups.get(op.group)
    if members is None:
        return Refused(RefusalKind.NOT_FOUND), _END
    if op.participant in members:
        return Ok(), _END
    if not _widen(tx, step):
        return RetryLater(), _END
    tx.groups[op.group] = members | {op.participant}
    tx.advance_authz()
    return Ok(), _END


def _h_enforcement_off(tx: _Tx, step: Step) -> Outcome:
    _as(step.op, EnforcementOff)
    if not tx.enforced:
        return Ok(), _END
    if not _widen(tx, step):
        return RetryLater(), _END
    if tx.automation:
        return Refused(RefusalKind.AUTOMATION_ENABLED), _END
    tx.enforced = False
    tx.advance_authz()
    return Ok(), _END


def _h_set_automation(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, SetAutomation)
    if tx.automation == op.on:
        return Ok(), _END
    if not _widen(tx, step):
        return RetryLater(), _END
    if op.on and not tx.enforced:
        return Refused(RefusalKind.ENFORCEMENT_REQUIRED), _END
    tx.automation = op.on
    return Ok(), _END


def _h_unarchive(tx: _Tx, step: Step) -> Outcome:
    """Critic item 4, LIB-16: a locked widening that never brings back a reveal."""
    op = _as(step.op, Unarchive)
    doc = tx.documents.get(op.document_id)
    if doc is None:
        return Refused(RefusalKind.NOT_FOUND), _END
    if not doc.archived:
        return Ok(), _END
    if not _widen(tx, step):
        return RetryLater(), _END
    tx.documents[op.document_id] = replace(doc, archived=False)
    tx.advance_authz()
    return Ok(), _END


# ------------------------------------------------------------------------ documents and screens


def _h_edit(tx: _Tx, step: Step) -> Outcome:
    """Critic item 10, REVEAL-8, ED-6: the open version is rewritten, or a new one appended; no slot."""
    op = _as(step.op, Edit)
    doc = tx.documents.get(op.document_id)
    if doc is None or doc.archived:
        raise OracleUsageError("Edit needs an existing, unarchived document")
    world = tx.state.world
    if not set(op.values) <= world.type_version(doc).declared:
        raise OracleUsageError("Edit writes declared keys only")
    latest = doc.latest
    if latest.sealed:
        versions = doc.versions + (Version(latest.number + 1, False, frozen_map(op.values)),)
    else:
        versions = doc.versions[:-1] + (Version(latest.number, False, frozen_map(op.values)),)
    tx.documents[op.document_id] = replace(doc, versions=versions)
    return Ok(), _END


def _h_seal(tx: _Tx, step: Step) -> Outcome:
    op = _as(step.op, Seal)
    doc = tx.documents.get(op.document_id)
    if doc is None or doc.latest.number != op.version or doc.latest.sealed:
        raise OracleUsageError("Seal applies to the one open version")
    tx.documents[op.document_id] = replace(doc, versions=doc.versions[:-1] + (replace(doc.latest, sealed=True),))
    return Ok(), _END


def _h_mint_screen(tx: _Tx, step: Step) -> Outcome:
    """SEC-48 (D-13). Minting authority and its `inactive` answer are `1kg.2.3`'s (critic item 11)."""
    op = _as(step.op, MintScreen)
    session = tx.state.open_session
    if session is None or op.grant in tx.grants:
        raise OracleUsageError("MintScreen needs an open session and a fresh grant id")
    tx.grants[op.grant] = ScreenGrant(op.grant, session.id, session.generation, revoked=False)
    return Ok(), _END


def _revoke_screen(tx: _Tx, grant_id: GrantId) -> Outcome:
    grant = tx.grants.get(grant_id)
    if grant is None:
        return Refused(RefusalKind.NOT_FOUND), _END
    if not grant.revoked:
        tx.grants[grant_id] = replace(grant, revoked=True)
    return Ok(), _END


def _h_revoke_screen(tx: _Tx, step: Step) -> Outcome:
    return _revoke_screen(tx, _as(step.op, RevokeScreen).grant)


def _h_leave_screen(tx: _Tx, step: Step) -> Outcome:
    return _revoke_screen(tx, _as(step.op, LeaveScreen).grant)


_HANDLERS: Final[Mapping[type, Callable[[_Tx, Step], Outcome]]] = frozen_map(
    {
        Confirm: _h_confirm,
        Stop: _h_stop,
        Export: _h_export,
        Classify: _h_classify,
        Unlink: _h_unlink,
        Relink: _h_relink,
        Archive: _h_archive,
        Unarchive: _h_unarchive,
        Delete: _h_delete,
        RemoveFromGroup: _h_remove_from_group,
        EnforcementOn: _h_enforcement_on,
        End: _h_end,
        Expire: _h_expire,
        ExpireJob: _h_expire_job,
        Rotate: _h_rotate,
        RemoveParticipant: _h_remove_participant,
        Start: _h_start,
        AddParticipant: _h_add_participant,
        Offer: _h_offer,
        Decline: _h_decline,
        Accept: _h_accept,
        ConfirmSeat: _h_confirm_seat,
        Link: _h_link,
        AddToGroup: _h_add_to_group,
        EnforcementOff: _h_enforcement_off,
        SetAutomation: _h_set_automation,
        Edit: _h_edit,
        Seal: _h_seal,
        MintScreen: _h_mint_screen,
        RevokeScreen: _h_revoke_screen,
        LeaveScreen: _h_leave_screen,
    }
)


def apply(state: State, step: Step) -> StepResult:
    """Apply one atomic step. Pure; raises `OracleUsageError` on misuse, never as an answer."""
    op = step.op
    if not available(type(op), state.world.release):
        raise OracleUsageError("the op is not available in this release")
    record = state.in_flight.get(step.op_id)
    if record is None:
        if step.phase is not phases(op, state)[0]:
            raise OracleUsageError("an op starts with its first phase")
    elif record.op != op or step.phase not in record.allowed:
        raise OracleUsageError("a phase out of order for its op id")
    tx = _Tx(state, step)
    answer, allowed = _HANDLERS[type(op)](tx, step)
    return tx.finish(answer, allowed)


def run(state: State, steps: Sequence[Step]) -> Trace:
    """Apply `steps` in order."""
    results: list[StepResult] = []
    current = state
    for step in steps:
        result = apply(current, step)
        results.append(result)
        current = result.after
    return Trace(state, tuple(results))


def run_op(state: State, op: Op, *, lock_available: bool = True) -> Trace:
    """Run one op alone through its solitary phases, stopping when it ends (RECONCILE once)."""
    op_id = next_op_id(state)
    results: list[StepResult] = []
    current = state
    for phase in phases(op, state):
        if results and phase not in current.in_flight[op_id].allowed:
            break
        result = apply(current, Step(op_id, op, phase, lock_available))
        results.append(result)
        current = result.after
    return Trace(state, tuple(results))


def compose(
    state: State,
    command_id: str,
    document_id: str,
    keys: Sequence[str],
    audience: RevealAudience,
    classify: Sequence[ClassifyEntry] = (),
    *,
    version: int = 1,
) -> Confirm:
    """A Confirm composed now: it names the live (or latest) session and that session's epoch."""
    sid = state.live
    if sid is None:
        sid = max(state.sessions, key=lambda s: (len(s), s), default=SessionId("s1"))
    session = state.sessions.get(sid)
    epoch = 0 if session is None else session.epoch
    return Confirm(
        CommandId(command_id),
        DocumentId(document_id),
        sid,
        epoch,
        version,
        frozenset(FieldKey(k) for k in keys),
        audience,
        tuple(classify),
    )


# ---------------------------------------------------------------------------- observe and diff


def canonical(obj: object) -> str:
    """A deterministic, content-free rendering: ids, keys, enum names and numbers, sorted (I-19, P-23)."""
    if obj is None or isinstance(obj, bool | int):
        return repr(obj)
    if isinstance(obj, str):
        return obj
    if isinstance(obj, Enum):
        return obj.name
    if is_dataclass(obj) and not isinstance(obj, type):
        inner = ",".join(f"{f.name}={canonical(getattr(obj, f.name))}" for f in fields(obj))
        return f"{type(obj).__name__}({inner})"
    if isinstance(obj, Mapping):
        items = sorted((canonical(k), canonical(v)) for k, v in obj.items())
        return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    if isinstance(obj, frozenset | set):
        return "{" + ",".join(sorted(canonical(i) for i in obj)) + "}"
    if isinstance(obj, tuple | list):
        return "(" + ",".join(canonical(i) for i in obj) + ")"
    raise OracleUsageError("only ids, keys, enums and numbers are described")


Outcomes = Literal["ok", "refused", "replayed", "not_applied_yet", "retry_later", "job_retry"]


@dataclass(frozen=True)
class AnswerView:
    """An answer as a consumer compares it (I-18): outcome, status, kind and sorted keys."""

    outcome: Outcomes
    status: int | None
    kind: RefusalKind | None
    keys: tuple[FieldKey, ...]


def answer_view(answer: Answer) -> AnswerView:
    if isinstance(answer, Replayed):
        inner = answer_view(answer.first)
        return AnswerView("replayed", inner.status, inner.kind, inner.keys)
    if isinstance(answer, Refused):
        return AnswerView("refused", answer.kind.status, answer.kind, tuple(sorted(answer.keys)))
    if isinstance(answer, Ok):
        return AnswerView("ok", 200, None, ())
    if isinstance(answer, NotAppliedYet):
        return AnswerView("not_applied_yet", 503, None, ())
    if isinstance(answer, RetryLater):
        return AnswerView("retry_later", 503, None, ())
    return AnswerView("job_retry", None, None, ())


@dataclass(frozen=True)
class EventView:
    """An event as a consumer compares it: no sequence number, session or disclosure id."""

    kind: EventKind
    reason: StopReason | SessionEndReason | None
    document: DocumentId
    key: FieldKey
    version: int
    slot: Slot | None
    actor: Actor

    def sort_key(self) -> tuple[tuple[int, str], str, str, str, str, int, str]:
        return (
            (-1, "") if self.slot is None else self.slot.sort_key(),
            self.document,
            self.key,
            self.kind.name,
            "" if self.reason is None else self.reason.name,
            self.version,
            self.actor.name,
        )


SlotEntry = tuple[Slot, DocumentId, int, tuple[FieldKey, ...]]
SessionView = tuple[SessionId, bool, bool, int, int]


@dataclass(frozen=True)
class Observation:
    """The consumer comparison contract (brief section 5.10, critic items 2 and 16)."""

    answer: AnswerView
    slots: tuple[SlotEntry, ...]
    disclosures: tuple[frozenset[Slot], ...]
    held: frozenset[ParticipantId]
    session: SessionView | None
    epoch_advanced: bool
    authz_advanced: bool
    authz_mode: Literal["none", "shared", "exclusive"]
    events: tuple[EventView, ...]
    classes: tuple[tuple[DocumentId, FieldKey, FieldClass], ...]


OBSERVATION_FIELDS: Final = frozenset(f.name for f in fields(Observation))


def _latest_session(state: State) -> SessionView | None:
    if not state.sessions:
        return None
    sid = max(state.sessions, key=lambda s: (len(s), s))
    s = state.sessions[sid]
    return (s.id, s.live, s.expired, s.epoch, s.generation)


def observe(result: StepResult) -> Observation:
    """What a consumer can rebuild from production after replaying the same steps."""
    after = result.after
    fp = result.footprint
    mode: Literal["none", "shared", "exclusive"] = "none"
    if LockLevel.AUTHZ_EXCLUSIVE in fp:
        mode = "exclusive"
    elif LockLevel.AUTHZ_SHARED in fp:
        mode = "shared"
    disclosures = sorted(
        (d.slots for d in after.disclosures.values()), key=lambda ss: [s.sort_key() for s in slot_order(ss)]
    )
    classes: tuple[tuple[DocumentId, FieldKey, FieldClass], ...] = ()
    if after.world.release is Release.ASSISTANT:
        classes = tuple((d, k, c) for (d, k), c in sorted(after.world.classes.items(), key=lambda kv: kv[0]))
    events = tuple(
        sorted(
            (EventView(e.kind, e.reason, e.document, e.key, e.version, e.slot, e.actor) for e in result.events),
            key=EventView.sort_key,
        )
    )
    return Observation(
        answer=answer_view(result.answer),
        slots=tuple(
            (s, after.slots[s].document, after.slots[s].version, tuple(sorted(after.slots[s].mask)))
            for s in slot_order(after.slots)
        ),
        disclosures=tuple(disclosures),
        held=frozenset(s.participant for s in after.slots if s.participant is not None and held(after, s.participant)),
        session=_latest_session(after),
        epoch_advanced=result.epoch_advanced,
        authz_advanced=result.authz_advanced,
        authz_mode=mode,
        events=events,
        classes=classes,
    )


def diff(
    expected: Observation,
    actual: Observation,
    *,
    compare_409_kinds: bool = False,
    fields: frozenset[str] = OBSERVATION_FIELDS,
) -> tuple[str, ...]:
    """Name what differs, by field, id and key only (I-18, T-O6).

    Answers compare by outcome, then status; a 422 also by kind and keys; a 409 by kind only when asked.
    `fields` limits the comparison to what a consumer's production state can supply (critic item 16).
    """
    out: list[str] = []
    if "answer" in fields:
        e, a = expected.answer, actual.answer
        if e.outcome != a.outcome or e.status != a.status:
            out.append(f"answer: expected {canonical(e)} actual {canonical(a)}")
        elif e.status == 422 and (e.kind, e.keys) != (a.kind, a.keys):
            out.append(f"answer: expected {canonical(e)} actual {canonical(a)}")
        elif e.status == 409 and compare_409_kinds and e.kind != a.kind:
            out.append(f"answer: expected {canonical(e)} actual {canonical(a)}")
    for name in sorted(OBSERVATION_FIELDS - {"answer"}):
        if name in fields and getattr(expected, name) != getattr(actual, name):
            out.append(
                f"{name}: expected {canonical(getattr(expected, name))} actual {canonical(getattr(actual, name))}"
            )
    return tuple(out)


__all__ = [
    "ALL_OPS",
    "ASSISTANT_ONLY",
    "LOCK_RANK",
    "OBSERVATION_FIELDS",
    "REVOCATIONS",
    "TWO_STEP_NARROWINGS",
    "Accept",
    "AddParticipant",
    "AddToGroup",
    "AnswerView",
    "Archive",
    "ClassifyEntry",
    "Classify",
    "Confirm",
    "ConfirmSeat",
    "Decline",
    "Delete",
    "Edit",
    "End",
    "EnforcementOff",
    "EnforcementOn",
    "EventView",
    "Expire",
    "ExpireJob",
    "Export",
    "InFlight",
    "LeaveScreen",
    "Link",
    "LockLevel",
    "MintScreen",
    "Observation",
    "Offer",
    "Op",
    "Phase",
    "Relink",
    "RemoveFromGroup",
    "RemoveParticipant",
    "RevokeScreen",
    "Rotate",
    "Seal",
    "SetAutomation",
    "Start",
    "Step",
    "StepResult",
    "Stop",
    "Trace",
    "Unarchive",
    "Unlink",
    "answer_view",
    "apply",
    "available",
    "canonical",
    "compose",
    "diff",
    "next_op_id",
    "observe",
    "phases",
    "run",
    "run_op",
]

"""Seeded generators of campaigns, participants, characters, groups, slots and disclosure histories.

No Hypothesis (I-3): every generator takes a `random.Random` built from a fixed integer seed, and
iterates only sorted sequences, so the same seed gives a byte-identical `describe(trace)` in any
process whatever its string-hash seed (P-23). A consumer may drive them from `st.randoms()` later.

Slots and history are never written directly: they come from running generated Confirms and ops
through `apply`, so every generated state is reachable (brief section 5.12).
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .fixtures import FIXTURE_TYPES
from .machine import (
    REVOCATIONS,
    Accept,
    AddParticipant,
    AddToGroup,
    Archive,
    Classify,
    ClassifyEntry,
    Confirm,
    ConfirmSeat,
    Decline,
    Delete,
    Edit,
    End,
    EnforcementOff,
    EnforcementOn,
    Expire,
    ExpireJob,
    Export,
    LeaveScreen,
    Link,
    MintScreen,
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
    Stop,
    Trace,
    Unarchive,
    Unlink,
    apply,
    canonical,
    next_op_id,
    phases,
    run_op,
)
from .model import (
    LIVE_SEATS,
    TABLE,
    AccountId,
    ClassKind,
    CommandId,
    Document,
    DocumentId,
    EligAudience,
    EveryoneSeated,
    FieldClass,
    FieldKey,
    GrantId,
    Group,
    GroupId,
    OpId,
    OracleUsageError,
    Participant,
    ParticipantAudienceId,
    ParticipantId,
    Participants,
    Release,
    Requester,
    RevealAudience,
    ScreenGrant,
    SeatStatus,
    Session,
    SessionId,
    Slot,
    State,
    Table,
    TableAudienceId,
    Value,
    Version,
    World,
    frozen_map,
    participant_slot,
)
from .rules import (
    audience_of_slot,
    eligible_for_audience,
    expand_all,
    expand_audience,
    narrowest_widening,
    stored_class,
)

SCALE_ENV: Final = "POLICY_ORACLE_SCALE"


@dataclass(frozen=True)
class Bounds:
    """Inclusive ranges for `gen_world` and `gen_schedule`."""

    participants: tuple[int, int] = (1, 5)
    sheets: tuple[int, int] = (0, 3)
    documents: tuple[int, int] = (1, 4)
    versions: tuple[int, int] = (1, 3)
    groups: tuple[int, int] = (0, 2)
    warmup_confirms: tuple[int, int] = (0, 4)
    schedule_length: tuple[int, int] = (1, 16)


def scale() -> int:
    """`POLICY_ORACLE_SCALE` (I-24): default 1; anything but an integer of at least 1 is an error."""
    raw = os.environ.get(SCALE_ENV)
    if raw is None:
        return 1
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{SCALE_ENV} must be an integer of at least 1") from None
    if value < 1:
        raise ValueError(f"{SCALE_ENV} must be an integer of at least 1")
    return value


_SEAT_WEIGHTS: Final = (
    (SeatStatus.OPEN, 8),
    (SeatStatus.OFFERED, 10),
    (SeatStatus.NOT_ACCEPTED, 7),
    (SeatStatus.AWAITING_CONFIRMATION, 14),
    (SeatStatus.CONFIRMED, 40),
    (SeatStatus.REMOVED, 14),
)
_CLASS_WEIGHTS: Final = (
    (ClassKind.UNCLASSIFIED, 10),
    (ClassKind.GM_ONLY, 10),
    (ClassKind.PARTICIPANTS, 18),
    (ClassKind.CHARACTERS, 14),
    (ClassKind.GROUPS, 10),
    (ClassKind.CAMPAIGN, 16),
    (ClassKind.PUBLIC, 30),
)
_TABLE_TYPES: Final = (("oracle_npc", 1), ("oracle_npc", 2), ("oracle_statblock", 1), ("oracle_notes", 1))
_BAD_KEYS: Final = ("all", "tags", "hidden_identity", "sources", "zzz", "rumours")


def _pick_weighted[E](rng: random.Random, table: tuple[tuple[E, int], ...]) -> E:
    return rng.choices([t[0] for t in table], weights=[t[1] for t in table])[0]


def _values(rng: random.Random, declared: frozenset[FieldKey]) -> dict[FieldKey, Value]:
    out: dict[FieldKey, Value] = {}
    for k in sorted(declared):
        r = rng.random()
        out[k] = Value.PRESENT if r < 0.78 else (Value.BLANK if r < 0.89 else Value.ABSENT)
    return out


def _versions(rng: random.Random, declared: frozenset[FieldKey], bounds: Bounds) -> tuple[Version, ...]:
    n = rng.randint(*bounds.versions)
    open_last = rng.random() < 0.25
    return tuple(Version(i, not (open_last and i == n), frozen_map(_values(rng, declared))) for i in range(1, n + 1))


def _gen_class(
    rng: random.Random, pids: list[ParticipantId], sheets: list[DocumentId], groups: list[GroupId]
) -> FieldClass:
    kind = _pick_weighted(rng, _CLASS_WEIGHTS)
    if kind is ClassKind.PARTICIPANTS:
        return FieldClass(kind, frozenset(rng.sample(pids, rng.randint(1, min(3, len(pids))))))
    if kind is ClassKind.CHARACTERS:
        pool = [str(s) for s in sheets] + ["d99"]
        return FieldClass(kind, frozenset(rng.sample(pool, rng.randint(1, min(2, len(pool))))))
    if kind is ClassKind.GROUPS:
        pool = [str(g) for g in groups] or ["g9"]
        return FieldClass(kind, frozenset(rng.sample(pool, rng.randint(1, len(pool)))))
    return FieldClass(kind)


def gen_world(rng: random.Random, release: Release, bounds: Bounds = Bounds()) -> State:
    """A campaign: seats in all six statuses, sheets and links, every fixture type and version,
    unsealed versions and BLANK/ABSENT values; in ASSISTANT every class kind, orphan rows, groups
    (some empty) and the switches; a live session or none; screen grants of every shape; then a
    warm-up of generated Confirms."""
    assistant = release is Release.ASSISTANT
    pids = [ParticipantId(f"p{i}") for i in range(rng.randint(*bounds.participants))]
    participants: dict[ParticipantId, Participant] = {}
    for i, pid in enumerate(pids):
        acct = AccountId(f"a{i}")
        status = _pick_weighted(rng, _SEAT_WEIGHTS)
        if status is SeatStatus.OFFERED:
            participants[pid] = Participant(pid, status, None, acct)
        elif status in LIVE_SEATS:
            participants[pid] = Participant(pid, status, acct)
        elif status is SeatStatus.REMOVED:
            participants[pid] = Participant(pid, status, acct if rng.random() < 0.6 else None)
        else:
            participants[pid] = Participant(pid, status, None)
    documents: dict[DocumentId, Document] = {}
    free = list(pids)
    sheet_ids: list[DocumentId] = []
    n_sheets = rng.randint(*bounds.sheets)
    for j in range(n_sheets):
        did = DocumentId(f"d{j}")
        link = None
        if free and rng.random() < 0.75:
            link = rng.choice(free)
            free.remove(link)
        tv = FIXTURE_TYPES["oracle_sheet"].version(1)
        documents[did] = Document(did, "oracle_sheet", 1, _versions(rng, tv.declared, bounds), rng.random() < 0.1, link)
        sheet_ids.append(did)
    for j in range(n_sheets, n_sheets + rng.randint(*bounds.documents)):
        did = DocumentId(f"d{j}")
        type_id, tv_number = rng.choice(_TABLE_TYPES)
        tv = FIXTURE_TYPES[type_id].version(tv_number)
        documents[did] = Document(
            did, type_id, tv_number, _versions(rng, tv.declared, bounds), rng.random() < 0.1, None
        )
    groups: dict[GroupId, frozenset[ParticipantId]] = {}
    classes: dict[tuple[DocumentId, FieldKey], FieldClass] = {}
    enforced = automation = False
    if assistant:
        for g in range(rng.randint(*bounds.groups)):
            groups[GroupId(f"g{g}")] = frozenset(p for p in pids if rng.random() < 0.5)
        group_ids = sorted(groups)
        for did in sorted(documents):
            doc = documents[did]
            tv = FIXTURE_TYPES[doc.type_id].version(doc.type_version)
            for k in sorted(tv.revealable):
                if rng.random() < 0.85:
                    classes[(did, k)] = _gen_class(rng, pids, sheet_ids, group_ids)
            if rng.random() < 0.3:
                off = sorted((tv.declared - tv.revealable) | tv.reserved) or [FieldKey("old_field")]
                classes[(did, rng.choice(off))] = FieldClass.public()
        enforced = rng.random() < 0.65
        automation = enforced and rng.random() < 0.45
    world = World(
        release=release,
        owner=AccountId("a_gm"),
        participants=frozen_map(participants),
        documents=frozen_map(documents),
        groups=frozen_map(groups),
        classes=frozen_map(classes),
        enforced=enforced,
        automation_enabled=automation,
        types=FIXTURE_TYPES,
        authz_revision=rng.randint(0, 5),
    )
    mode = rng.choices(["live", "prior_then_live", "ended", "none"], weights=[6, 3, 1, 1])[0]
    sessions: dict[SessionId, Session] = {}
    live: SessionId | None = None
    grants: dict[GrantId, ScreenGrant] = {}

    def grant(session: SessionId, generation: int, revoked: bool) -> None:
        gid = GrantId(f"sg{len(grants)}")
        grants[gid] = ScreenGrant(gid, session, generation, revoked)

    if mode in ("prior_then_live", "ended"):
        old = SessionId("s1")
        sessions[old] = Session(old, False, False, rng.randint(0, 4), rng.randint(1, 3))
        if rng.random() < 0.7:
            grant(old, sessions[old].generation, False)
    if mode in ("live", "prior_then_live"):
        live = SessionId(f"s{len(sessions) + 1}")
        generation = rng.randint(1, 3)
        sessions[live] = Session(live, True, False, rng.randint(0, 3), generation)
        if rng.random() < 0.85:
            grant(live, generation, False)
        if rng.random() < 0.4:
            grant(live, generation, True)
        if generation > 1 and rng.random() < 0.5:
            grant(live, generation - 1, False)
    state = State(
        world=world,
        sessions=frozen_map(sessions),
        live=live,
        slots=frozen_map({}),
        slot_seq=frozen_map({}),
        disclosures=frozen_map({}),
        grants=frozen_map(grants),
        history=(),
        commands=frozen_map({}),
        in_flight=frozen_map({}),
    )
    if live is not None:
        for i in range(rng.randint(*bounds.warmup_confirms)):
            state = run_op(state, _gen_confirm(rng, state, f"w{i}", sloppy=0.05)).final
    return state


# --------------------------------------------------------------------------------- requests


def _active_pids(world: World) -> list[ParticipantId]:
    return [p for p in sorted(world.participants) if world.participants[p].active]


def _gen_audience(rng: random.Random, world: World, sloppy: float) -> RevealAudience:
    pids = sorted(world.participants)
    active = _active_pids(world)
    r = rng.random()
    if r < 0.33 or not pids:
        return Table()
    if r < 0.75:
        pool = active if active and rng.random() >= sloppy else pids + [ParticipantId("p9")]
        return Participants(frozenset(rng.sample(pool, rng.randint(1, min(3, len(pool))))))
    if r < 0.88 and world.release is Release.ASSISTANT:
        groups = sorted(world.groups)
        if groups and rng.random() >= sloppy:
            return Group(rng.choice(groups))
        return Group(GroupId("g9"))
    return EveryoneSeated()


def _all_eligible(world: World, doc: DocumentId, key: FieldKey, slots: frozenset[Slot]) -> bool:
    return all(eligible_for_audience(world, doc, key, audience_of_slot(s)).allowed for s in slots)


def _gen_confirm(
    rng: random.Random,
    state: State,
    command_id: str,
    *,
    sloppy: float,
    audience: RevealAudience | None = None,
    stray: float = 0.0,
) -> Confirm:
    """A Confirm; `sloppy` is the chance of each defect, `stray` the chance that a classify list also
    names a revealable key the mask leaves out (ED-13(4)). A zero chance draws nothing."""
    world = state.world
    docs = sorted(world.documents)
    if rng.random() >= sloppy:
        docs = [d for d in docs if not world.documents[d].archived] or docs
    doc_id = DocumentId("d99") if not docs or rng.random() < sloppy / 3 else rng.choice(docs)
    doc = world.documents.get(doc_id)
    version = 1
    keys: list[FieldKey] = [FieldKey("name")]
    if doc is not None:
        sealed = [v.number for v in doc.versions if v.sealed]
        if sealed and rng.random() >= sloppy:
            version = max(sealed)
        else:
            version = rng.choice([v.number for v in doc.versions] + [9])
        keys = list(expand_all(world, doc_id, version)) or sorted(world.revealable(doc_id)) or keys
    if audience is None:
        audience = _gen_audience(rng, world, sloppy)
    slots = expand_audience(world, audience)
    widenable: list[FieldKey] = []
    if world.enforced and slots:
        good = [k for k in keys if _all_eligible(world, doc_id, k, slots)]
        widenable = [
            k
            for k in keys
            if k not in good and narrowest_widening(world, stored_class(world, doc_id, k), slots) is not None
        ]
        if rng.random() < 0.85:
            keys = good or keys
    mask = set(rng.sample(keys, rng.randint(1, len(keys))))
    if rng.random() < sloppy:
        mask.add(FieldKey(rng.choice(_BAD_KEYS)))
    classify: list[ClassifyEntry] = []
    if world.release is Release.ASSISTANT and doc is not None and rng.random() < 0.25:
        candidates = [k for k in sorted(mask) if k in world.revealable(doc_id)]
        if widenable and rng.random() >= sloppy:
            candidates = rng.sample(widenable, min(len(widenable), rng.randint(1, 2)))
            mask |= set(candidates)
        for k in candidates[: rng.randint(1, 2)]:
            stored = stored_class(world, doc_id, k)
            new = narrowest_widening(world, stored, slots)
            if new is None or rng.random() < sloppy:
                new = FieldClass.public() if rng.random() < 0.5 else FieldClass.campaign()
            displayed = stored
            if rng.random() < sloppy:
                displayed = FieldClass.gm_only() if stored.kind is not ClassKind.GM_ONLY else FieldClass.unclassified()
            classify.append(ClassifyEntry(k, displayed, new))
        outside = [
            k for k in sorted(world.revealable(doc_id) - mask) if slots and not _all_eligible(world, doc_id, k, slots)
        ]
        if outside and stray and rng.random() < stray:
            k = rng.choice(outside)
            stored = stored_class(world, doc_id, k)
            classify.append(ClassifyEntry(k, stored, narrowest_widening(world, stored, slots) or FieldClass.public()))
    sid = state.live
    epoch = 0
    if sid is not None:
        epoch = state.sessions[sid].epoch
        if rng.random() < sloppy and epoch > 0:
            epoch -= 1
    past = [s for s in sorted(state.sessions) if s != sid]
    if sid is None or rng.random() < sloppy / 2:
        sid = rng.choice(past) if past and rng.random() < 0.7 else SessionId("s9")
        epoch = state.sessions[sid].epoch if sid in state.sessions else 0
    return Confirm(CommandId(command_id), doc_id, sid, epoch, version, frozenset(mask), audience, tuple(classify))


def _fresh(counter: list[int], prefix: str) -> str:
    counter[0] += 1
    return f"{prefix}{counter[0]}"


def _confirm_commands(state: State, session: SessionId | None) -> list[CommandId]:
    """Command ids of Confirms named for `session`: a replay reuses one only within one session."""
    out = set()
    for record in state.in_flight.values():
        op = record.op
        if isinstance(op, Confirm) and op.session_id == session:
            out.add(op.command_id)
    return sorted(out)


def _stop_commands(state: State) -> list[CommandId]:
    return sorted(c for kind, c in state.commands if kind == "stop")


def _sheets(world: World) -> list[DocumentId]:
    return [d for d in sorted(world.documents) if world.types[world.documents[d].type_id].audience == "owner"]


def _pick[E](rng: random.Random, items: list[E]) -> E | None:
    return rng.choice(items) if items else None


def _new_class(rng: random.Random, world: World) -> FieldClass:
    pids = _active_pids(world) or sorted(world.participants)
    sheets = _sheets(world)
    groups = sorted(world.groups)
    return _gen_class(rng, pids, sheets, groups) if pids else FieldClass.public()


OpFactory = Callable[[State], "Op | None"]


def _scripts(rng: random.Random, state: State, counter: list[int]) -> list[OpFactory]:
    """Directed sequences that reach rare outcomes the uniform draw seldom meets (P-24)."""
    world = state.world
    choice = rng.randrange(6)
    if world.release is Release.ASSISTANT and world.enforced and rng.random() < 0.5:
        choice = rng.choice((2, 3))
    docs = sorted(world.documents)
    if not docs:
        return []
    doc = rng.choice(docs)

    def confirm(mask_shrink: bool = False, version: int | None = None) -> OpFactory:
        def make(s: State) -> Op | None:
            op = _gen_confirm(rng, s, _fresh(counter, "c"), sloppy=0.0)
            live = [c for c in s.slots.values() if c.document == doc]
            if not live or doc not in s.world.documents:
                return op
            slots = frozenset(sl for sl, c in s.slots.items() if c.document == doc)
            audience: RevealAudience = Table()
            if TABLE not in slots:
                audience = Participants(frozenset(sl.participant for sl in slots if sl.participant is not None))
            mask = set(live[0].mask)
            if mask_shrink and len(mask) > 1:
                mask.discard(sorted(mask)[0])
            number = version if version is not None else live[0].version
            return Confirm(op.command_id, doc, op.session_id, op.epoch, number, frozenset(mask), audience)

        return make

    if choice == 0:
        return [confirm(), confirm(mask_shrink=True)]
    if choice == 1:

        def edit(s: State) -> Op | None:
            d = s.world.documents.get(doc)
            if d is None or d.archived:
                return None
            tv = s.world.type_version(d)
            return Edit(doc, frozen_map({k: Value.PRESENT for k in sorted(tv.declared)}))

        def seal(s: State) -> Op | None:
            d = s.world.documents.get(doc)
            return None if d is None or d.latest.sealed else Seal(doc, d.latest.number)

        def update(s: State) -> Op | None:
            d = s.world.documents.get(doc)
            return None if d is None else confirm(version=d.latest.number)(s)

        return [edit, seal, update]
    if choice == 2 and world.release is Release.ASSISTANT:
        return _script_delete_sheet(rng, state, counter)
    if choice == 3 and world.release is Release.ASSISTANT and world.groups:
        gid = rng.choice(sorted(world.groups))

        def group_confirm(s: State) -> Op | None:
            return _gen_confirm(rng, s, _fresh(counter, "c"), sloppy=0.0, audience=Group(gid))

        def leave(s: State) -> Op | None:
            members = sorted(s.world.groups.get(gid, frozenset()))
            return RemoveFromGroup(gid, rng.choice(members)) if members else None

        return [group_confirm, leave]
    if choice == 4:
        follow: list[OpFactory] = [
            lambda s: Stop(CommandId(_fresh(counter, "c")), DocumentId(doc)),
            lambda s: RemoveParticipant(rng.choice(sorted(s.world.participants))),
            lambda s: _gen_confirm(rng, s, _fresh(counter, "c"), sloppy=0.0),
        ]
        final: list[OpFactory] = [lambda s: ExpireJob(), lambda s: End(), lambda s: Start(), lambda s: Rotate()]
        return [lambda s: Expire(), rng.choice(follow), rng.choice(final)]
    sheets = _sheets(world)
    linked = [s for s in sheets if world.documents[s].linked_participant is not None]
    spare = [s for s in sheets if world.documents[s].linked_participant is None]
    holders = {world.documents[s].linked_participant for s in linked}
    free = [p for p in _active_pids(world) if p not in holders]
    if linked and spare and free:
        sheet, other, to = rng.choice(linked), rng.choice(spare), rng.choice(free)
        return [lambda s: Relink(sheet, to), lambda s: Link(other, to)]
    return [lambda s: _gen_confirm(rng, s, _fresh(counter, "c"), sloppy=0.0)]


def _script_delete_sheet(rng: random.Random, state: State, counter: list[int]) -> list[OpFactory]:
    """A copy eligible only through `characters[sheet]`, then archive and delete that sheet, so the
    Enforced scan stops it with `document_deleted` (critic item 4)."""
    world = state.world
    cells = [
        (d, k, s)
        for (d, k), cls in sorted(world.classes.items())
        if cls.kind is ClassKind.CHARACTERS and k in world.revealable(d) and not world.documents[d].archived
        for s in sorted(cls.ids)
        if s in world.documents and world.documents[DocumentId(s)].linked_participant is not None
    ]
    if not cells:
        return []
    doc, key, sheet = rng.choice(cells)
    owner = world.documents[DocumentId(sheet)].linked_participant
    if owner is None or not world.participants[owner].active:
        return []

    def confirm(s: State) -> Op | None:
        d = s.world.documents.get(doc)
        if d is None or s.live is None:
            return None
        sealed = [v.number for v in d.versions if v.sealed]
        number = max(sealed) if sealed else 1
        return Confirm(
            CommandId(_fresh(counter, "c")),
            doc,
            s.live,
            s.sessions[s.live].epoch,
            number,
            frozenset({key}),
            Participants(frozenset({owner})),
        )

    def delete(s: State) -> Op | None:
        d = s.world.documents.get(DocumentId(sheet))
        return Delete(DocumentId(sheet)) if d is not None and d.archived else None

    return [confirm, lambda s: Archive(DocumentId(sheet)), delete]


def _gen_op(rng: random.Random, state: State, counter: list[int]) -> Op | None:
    world = state.world
    assistant = world.release is Release.ASSISTANT
    docs = sorted(world.documents)
    pids = sorted(world.participants)
    active = _active_pids(world)
    kinds: list[tuple[str, int]] = [
        ("confirm", 24), ("stop", 8), ("export", 6), ("unlink", 2), ("relink", 3), ("archive", 3),
        ("unarchive", 2), ("delete", 3), ("end", 3), ("expire", 3), ("expire_job", 2), ("rotate", 2),
        ("remove", 3), ("start", 5), ("add", 2), ("offer", 3), ("decline", 1), ("accept", 3), ("confirm_seat", 3),
        ("link", 2), ("edit", 2), ("seal", 2), ("mint", 2), ("revoke", 1), ("leave", 1),
    ]  # fmt: skip
    if assistant:
        kinds += [("classify", 8), ("remove_from_group", 3), ("enforce_on", 2), ("add_to_group", 2),
                  ("enforce_off", 2), ("automation", 3)]  # fmt: skip
    kind = rng.choices([k for k, _ in kinds], weights=[w for _, w in kinds])[0]
    if state.open_session is None and rng.random() < 0.6:
        kind = "start" if state.live is None else rng.choice(("expire_job", "start", "end"))
    doc_id = rng.choice(docs) if docs else None
    if kind == "confirm":
        sid = state.live
        replays = _confirm_commands(state, sid)
        if replays and rng.random() < 0.25:
            base = _gen_confirm(rng, state, rng.choice(replays), sloppy=0.3)
            return base
        return _gen_confirm(rng, state, _fresh(counter, "c"), sloppy=0.1 if world.enforced else 0.2, stray=0.5)
    if kind == "stop":
        cid = CommandId(_fresh(counter, "c"))
        stops = _stop_commands(state)
        r = rng.random()
        if stops and r < 0.2:
            cid = rng.choice(stops)
        elif r < 0.25:
            confirms = sorted(c for k, c in state.commands if k == "confirm")
            cid = rng.choice(confirms) if confirms else cid
        live_docs = sorted({c.document for c in state.slots.values()})
        t = rng.random()
        target: DocumentId | str = "all"
        if t < 0.5 and live_docs:
            target = rng.choice(live_docs)
        elif t < 0.75 and docs:
            target = rng.choice(docs)
        elif t < 0.82:
            target = "d99"
        retract = assistant and rng.random() < 0.2
        return Stop(cid, "all" if target == "all" else DocumentId(target), retract)
    if kind == "export":
        candidates = [d for d in docs if not world.documents[d].archived]
        if not candidates:
            return None
        did = rng.choice(candidates)
        doc = world.documents[did]
        live_versions = sorted({c.version for c in state.slots.values() if c.document == did})
        r = rng.random()
        number = doc.latest.number
        if r < 0.2 and live_versions:
            number = rng.choice(live_versions)
        elif r < 0.35:
            number = rng.randint(1, doc.latest.number + 1)
        keys = list(expand_all(world, did, number)) or sorted(world.revealable(did))
        if world.enforced and rng.random() < 0.6:
            keys = [k for k in keys if eligible_for_audience(world, did, k, TableAudienceId()).allowed] or keys
        chosen = set(rng.sample(keys, rng.randint(1, len(keys))))
        if rng.random() < 0.15:
            chosen.add(FieldKey(rng.choice(_BAD_KEYS)))
        return Export(did, number, frozenset(chosen))
    if kind == "classify" and doc_id is not None:
        live_cells = sorted({(c.document, k) for c in state.slots.values() for k in c.mask})
        if live_cells and rng.random() < 0.5:
            live_doc, live_key = rng.choice(live_cells)
            stored = stored_class(world, live_doc, live_key)
            narrower = FieldClass.gm_only() if rng.random() < 0.6 else _new_class(rng, world)
            return Classify(live_doc, live_key, stored, narrower)
        allow = sorted(world.revealable(doc_id))
        key = FieldKey(rng.choice(allow)) if allow and rng.random() < 0.9 else FieldKey(rng.choice(_BAD_KEYS))
        stored = stored_class(world, doc_id, key)
        expected = stored if rng.random() < 0.85 else _new_class(rng, world)
        return Classify(doc_id, key, expected, _new_class(rng, world))
    if kind in ("unlink", "relink", "link"):
        sheets = _sheets(world)
        linked = [s for s in sheets if world.documents[s].linked_participant is not None]
        unlinked = [s for s in sheets if world.documents[s].linked_participant is None]
        holders = {world.documents[s].linked_participant for s in linked}
        free = [p for p in active if p not in holders]
        if kind == "unlink":
            sheet = _pick(rng, linked if rng.random() < 0.85 else sheets)
            return None if sheet is None else Unlink(DocumentId(sheet))
        if kind == "relink":
            sheet, to = _pick(rng, linked), _pick(rng, free)
            return None if sheet is None or to is None else Relink(DocumentId(sheet), ParticipantId(to))
        sheet, to = _pick(rng, unlinked), _pick(rng, free)
        return None if sheet is None or to is None else Link(DocumentId(sheet), ParticipantId(to))
    if kind in ("archive", "unarchive", "delete") and doc_id is not None:
        archived = [d for d in docs if world.documents[d].archived]
        if kind == "archive":
            return Archive(doc_id if rng.random() < 0.9 else DocumentId("d99"))
        target = rng.choice(archived) if archived and rng.random() < 0.8 else doc_id
        return Unarchive(target) if kind == "unarchive" else Delete(target)
    if kind == "remove_from_group":
        groups = sorted(world.groups)
        if not groups:
            return None
        gid = rng.choice(groups)
        members = sorted(world.groups[gid])
        pool = members if members and rng.random() < 0.8 else pids
        return RemoveFromGroup(gid, rng.choice(pool)) if pool else None
    if kind == "add_to_group":
        groups = sorted(world.groups) or [GroupId("g9")]
        return AddToGroup(rng.choice(groups), rng.choice(active)) if active else None
    simple: dict[str, Callable[[], Op | None]] = {
        "enforce_on": EnforcementOn,
        "enforce_off": EnforcementOff,
        "automation": lambda: SetAutomation(rng.random() < 0.6),
        "end": End,
        "expire": Expire,
        "expire_job": ExpireJob,
        "rotate": Rotate,
        "start": Start,
        "remove": lambda: RemoveParticipant(rng.choice(pids + [ParticipantId("p9")])) if pids else None,
        "add": lambda: AddParticipant(ParticipantId(_fresh(counter, "p"))),
    }
    if kind in simple:
        return simple[kind]()
    if kind in ("offer", "decline", "accept", "confirm_seat"):
        wanted = {
            "offer": (SeatStatus.OPEN, SeatStatus.NOT_ACCEPTED),
            "decline": (SeatStatus.OFFERED,),
            "accept": (SeatStatus.OFFERED,),
            "confirm_seat": (SeatStatus.AWAITING_CONFIRMATION, SeatStatus.CONFIRMED),
        }[kind]
        seats = [p for p in pids if world.participants[p].status in wanted]
        pid = _pick(rng, seats)
        if pid is None:
            return None
        if kind == "offer":
            removed = sorted(
                p.account
                for p in world.participants.values()
                if p.status is SeatStatus.REMOVED and p.account is not None
            )
            account = rng.choice(removed) if removed and rng.random() < 0.3 else AccountId(_fresh(counter, "a"))
            return Offer(ParticipantId(pid), account)
        seat = ParticipantId(pid)
        if kind == "decline":
            return Decline(seat)
        return Accept(seat) if kind == "accept" else ConfirmSeat(seat)
    if kind in ("edit", "seal") and doc_id is not None:
        doc = world.documents[doc_id]
        if kind == "seal":
            open_docs = [d for d in docs if not world.documents[d].latest.sealed]
            if not open_docs:
                return None
            sealing = world.documents[rng.choice(open_docs)]
            return Seal(sealing.id, sealing.latest.number)
        if doc.archived:
            return None
        tv = world.type_version(doc)
        return Edit(doc_id, frozen_map(_values(rng, tv.declared)))
    if kind == "mint":
        return MintScreen(GrantId(_fresh(counter, "sg")))
    if kind in ("revoke", "leave"):
        grants = sorted(state.grants) + [GrantId("sg99")]
        grant_id = rng.choice(grants)
        return RevokeScreen(grant_id) if kind == "revoke" else LeaveScreen(grant_id)
    return None


def _interfere(rng: random.Random, state: State, counter: list[int]) -> Op | None:
    """Critic item 13: a concurrent step that invalidates an in-flight two-step op's target."""
    waiting = [
        r.op
        for _, r in sorted(state.in_flight.items())
        if Phase.STEP2 in r.allowed and isinstance(r.op, Relink | Delete | Classify | Unlink)
    ]
    if not waiting:
        return None
    op = rng.choice(waiting)
    world = state.world
    if isinstance(op, Relink):
        spare = [s for s in _sheets(world) if world.documents[s].linked_participant is None and s != op.sheet]
        if spare and rng.random() < 0.7:
            return Link(DocumentId(rng.choice(spare)), op.to)
        return RemoveParticipant(op.to)
    if isinstance(op, Delete):
        return Unarchive(op.document_id)
    if isinstance(op, Classify):
        stored = stored_class(world, op.document_id, op.key)
        return Classify(op.document_id, op.key, stored, _new_class(rng, world))
    return Delete(op.sheet)


def gen_schedule(rng: random.Random, state: State, length: int) -> tuple[Step, ...]:
    """Interleave up to three in-flight ops, leave some unfinished, emit 0 to 2 RECONCILEs after a
    revocation, and make invalid requests on purpose (stale epochs, past sessions, bad masks and
    audiences, stale classify entries, unavailable locks, replayed command ids). A candidate that
    would raise `OracleUsageError` is never emitted."""
    steps: list[Step] = []
    current = state
    fresh_id = next_op_id(state)
    counter = [0]
    reconciles: dict[OpId, int] = {}
    stuck: set[OpId] = set()
    script: list[OpFactory] = []
    patience = 8
    failures = 0
    while len(steps) < length and failures < 80:
        unfinished = [
            oid
            for oid in sorted(current.in_flight)
            if oid not in stuck and current.in_flight[oid].allowed - {Phase.RECONCILE}
        ]
        due = [oid for oid in sorted(reconciles) if reconciles[oid] > 0 and oid not in stuck]
        lock = rng.random() >= 0.12
        r = rng.random()
        op: Op | None = None
        if unfinished and (len(unfinished) >= 3 or r < 0.5):
            oid = rng.choice(unfinished)
            record = current.in_flight[oid]
            phase = sorted(record.allowed - {Phase.RECONCILE}, key=lambda p: p.value)[0]
            step = Step(oid, record.op, phase, lock)
        elif due and r < 0.6:
            oid = rng.choice(due)
            reconciles[oid] -= 1
            step = Step(oid, current.in_flight[oid].op, Phase.RECONCILE, lock)
        else:
            if not script and rng.random() < 0.2:
                script = _scripts(rng, current, counter)
                patience = 8
            if script:
                op = script[0](current)
                patience -= 1
                if op is not None or patience <= 0:
                    script.pop(0)
                    patience = 8
            elif rng.random() < 0.25:
                op = _interfere(rng, current, counter)
            if op is None:
                op = _gen_op(rng, current, counter)
            if op is None:
                failures += 1
                continue
            step = Step(fresh_id, op, phases(op, current)[0], lock)
        try:
            result = apply(current, step)
        except OracleUsageError:
            failures += 1
            if step.op_id != fresh_id:
                stuck.add(step.op_id)
            continue
        steps.append(step)
        current = result.after
        if step.op_id == fresh_id:
            fresh_id = OpId(fresh_id + 1)
            if type(step.op) in REVOCATIONS and Phase.RECONCILE in current.in_flight[step.op_id].allowed:
                reconciles[step.op_id] = rng.randint(0, 2)
    return tuple(steps)


def gen_eligibility_case(rng: random.Random, state: State) -> tuple[DocumentId, FieldKey, EligAudience]:
    """A document (sometimes unknown), a key (sometimes off the allowlist) and an identity audience
    (sometimes an unknown participant)."""
    world = state.world
    docs = sorted(world.documents)
    doc = rng.choice(docs) if docs and rng.random() < 0.96 else DocumentId("d99")
    known = world.documents.get(doc)
    allow = sorted(world.revealable(doc))
    if known is not None and allow and rng.random() < 0.8:
        key = FieldKey(rng.choice(allow))
    elif known is not None:
        tv = world.type_version(known)
        key = FieldKey(rng.choice(sorted(tv.declared | tv.reserved | {FieldKey("all")})))
    else:
        key = FieldKey(rng.choice(_BAD_KEYS + ("name",)))
    pids = sorted(world.participants)
    if not pids or rng.random() < 0.3:
        return doc, key, TableAudienceId()
    pid = rng.choice(pids) if rng.random() < 0.95 else ParticipantId("p9")
    return doc, key, ParticipantAudienceId(pid)


def gen_requester(rng: random.Random, state: State) -> Requester:
    """The owner, a seat's account (any status), an invitee, a stranger or nobody, with a screen
    grant of any shape (live, revoked, old generation, ended session, unknown) or none."""
    world = state.world
    accounts: list[AccountId | None] = [world.owner, AccountId("a_x"), None]
    for pid in sorted(world.participants):
        seat = world.participants[pid]
        for acct in (seat.account, seat.offered_to):
            if acct is not None:
                accounts.append(acct)
    grants: list[GrantId | None] = [None, None, None, *sorted(state.grants), GrantId("sg99")]
    account = rng.choice(accounts)
    grant = rng.choice(grants) if rng.random() < 0.35 else None
    return Requester(account, grant)


def gen_entitlement_case(rng: random.Random, state: State) -> tuple[Requester, Slot]:
    """A requester and a slot: the table, a seat's own slot, another's, or an unknown participant's."""
    requester = gen_requester(rng, state)
    pids = sorted(state.world.participants)
    if not pids or rng.random() < 0.35:
        return requester, TABLE
    pid = rng.choice(pids) if rng.random() < 0.95 else ParticipantId("p9")
    return requester, participant_slot(pid)


def _describe_state(state: State) -> str:
    w = state.world
    world = {
        "release": w.release,
        "owner": w.owner,
        "participants": w.participants,
        "documents": w.documents,
        "groups": w.groups,
        "classes": w.classes,
        "enforced": w.enforced,
        "automation": w.automation_enabled,
        "authz_revision": w.authz_revision,
    }
    rest = {
        "sessions": state.sessions,
        "live": state.live,
        "slots": state.slots,
        "slot_seq": state.slot_seq,
        "disclosures": state.disclosures,
        "grants": state.grants,
        "history": state.history,
        "commands": state.commands,
    }
    return canonical(world) + "\n" + canonical(rest)


def describe(obj: State | Trace | Step) -> str:
    """A content-free, deterministic rendering for failure messages: ids, keys, enums and numbers."""
    if isinstance(obj, Step):
        return canonical(obj)
    if isinstance(obj, State):
        return _describe_state(obj)
    lines = [_describe_state(obj.initial)]
    for r in obj.results:
        lines.append(canonical((r.step, r.answer, r.events, r.epoch_advanced, r.authz_advanced, r.footprint)))
    lines.append(_describe_state(obj.final))
    return "\n".join(lines)


__all__ = [
    "SCALE_ENV",
    "Bounds",
    "describe",
    "gen_eligibility_case",
    "gen_entitlement_case",
    "gen_requester",
    "gen_schedule",
    "gen_world",
    "scale",
]

"""The oracle's pure rules: eligibility on identities, entitlement on principals, and their helpers.

Each function cites the ADR or threat-model rows it implements. They are written from those records,
never ported from production code (I-2). None of them mutates anything.
"""

from __future__ import annotations

from .model import (
    LIST_KINDS,
    TABLE,
    Actor,
    ClassKind,
    Document,
    DocumentId,
    EligAudience,
    Eligibility,
    EligReason,
    Entitlement,
    EventKind,
    FieldClass,
    FieldKey,
    Group,
    GroupId,
    ParticipantAudienceId,
    ParticipantId,
    Participants,
    PrincipalKind,
    Requester,
    RevealAudience,
    SeatStatus,
    Slot,
    SlotView,
    State,
    StopReason,
    Table,
    TableAudienceId,
    TypeVersion,
    Value,
    VisibleView,
    World,
    participant_slot,
    slot_order,
)


def stored_class(world: World, document_id: DocumentId, key: FieldKey) -> FieldClass:
    """The class a key evaluates as (ED-4, ED-5, ED-24).

    A key off the current type version's allowlist is `gm_only` by construction and its row, if any,
    is an orphan that evaluation ignores. A revealable key with no row is `unclassified`.
    """
    if key not in world.revealable(document_id):
        return FieldClass.gm_only()
    return world.classes.get((document_id, key), FieldClass.unclassified())


def audience_of_slot(slot: Slot) -> EligAudience:
    """A slot's identity audience (ED-10): the table, or `participant:<id>`."""
    if slot.participant is None:
        return TableAudienceId()
    return ParticipantAudienceId(slot.participant)


def eligible_for_audience(world: World, document_id: DocumentId, key: FieldKey, audience: EligAudience) -> Eligibility:
    """ADR section 4 `eligible_for_audience`, on identities, in its order; the first match decides.

    1. A participant audience that is unknown or removed: no, even for a `public` key (W-15).
    2. A key off the revealable allowlist of the document's current type version: no (ED-5, ED-24);
       an unknown or deleted document fails closed the same way (critic item 12).
    3-11. unclassified | gm_only: no; public: yes; the table: no (ED-10, TP-1 keeps the table
       `public`-only, ADR 15.2); campaign: yes; participants[ids]: named; characters[ids]: linked NOW;
       groups[ids]: a member NOW (ED-4).

    It never reads the `enforced` flag: callers gate on it. In Workbench v1 there are no class rows, so
    every revealable key evaluates as `unclassified`.
    """
    if isinstance(audience, ParticipantAudienceId):
        seat = world.participants.get(audience.participant)
        if seat is None or not seat.active:
            return Eligibility(False, EligReason.INACTIVE_PARTICIPANT)
    if key not in world.revealable(document_id):
        return Eligibility(False, EligReason.NOT_REVEALABLE)
    cls = world.classes.get((document_id, key), FieldClass.unclassified())
    if cls.kind is ClassKind.UNCLASSIFIED:
        return Eligibility(False, EligReason.UNCLASSIFIED)
    if cls.kind is ClassKind.GM_ONLY:
        return Eligibility(False, EligReason.GM_ONLY)
    if cls.kind is ClassKind.PUBLIC:
        return Eligibility(True, EligReason.PUBLIC)
    if not isinstance(audience, ParticipantAudienceId):
        return Eligibility(False, EligReason.TABLE_NEEDS_PUBLIC)
    pid = audience.participant
    if cls.kind is ClassKind.CAMPAIGN:
        return Eligibility(True, EligReason.CAMPAIGN)
    if cls.kind is ClassKind.PARTICIPANTS:
        named = pid in cls.ids
        return Eligibility(named, EligReason.NAMED if named else EligReason.NOT_NAMED)
    if cls.kind is ClassKind.CHARACTERS:
        linked = any(_linked_now(world.documents.get(DocumentId(c)), pid) for c in sorted(cls.ids))
        return Eligibility(linked, EligReason.LINKED if linked else EligReason.NOT_LINKED)
    member = any(pid in world.groups.get(GroupId(g), frozenset()) for g in sorted(cls.ids))
    return Eligibility(member, EligReason.MEMBER if member else EligReason.NOT_MEMBER)


def _linked_now(document: Document | None, participant: ParticipantId) -> bool:
    return document is not None and document.linked_participant == participant


def seat_of_account(world: World, account: str) -> ParticipantId | None:
    """The live (awaiting or confirmed) seat an account holds, if any (I-23, SEC-41)."""
    for pid in sorted(world.participants):
        seat = world.participants[pid]
        if seat.account == account and seat.status in (SeatStatus.AWAITING_CONFIRMATION, SeatStatus.CONFIRMED):
            return pid
    return None


def table_principal(state: State, requester: Requester) -> PrincipalKind:
    """Threat model section 15.2 `table_principal`, in order (critic item 2: *open* sessions only).

    1. A live screen grant decides alone (SEC-48): known, unrevoked, of the current session, which is
       open, and of that session's generation. A grant that is not live is ignored.
    2. No account session: unauthenticated (401).
    3. No open session: inactive (SEC-42: expiry is a clock comparison, never `state` alone).
    4. The owner: the table slot (the owner holds no seat).
    5. An accepted, not-removed seat: awaiting confirmation or confirmed (SEC-50(5), D-12).
    6. Anything else (no seat, offered, not accepted, removed): inactive (SEC-46).
    """
    session = state.open_session
    if requester.screen_grant is not None:
        grant = state.grants.get(requester.screen_grant)
        if (
            grant is not None
            and not grant.revoked
            and session is not None
            and grant.session == session.id
            and grant.generation == session.generation
        ):
            return PrincipalKind.SCREEN
    if requester.account is None:
        return PrincipalKind.UNAUTHENTICATED
    if session is None:
        return PrincipalKind.INACTIVE
    world = state.world
    if requester.account == world.owner:
        return PrincipalKind.OWNER
    pid = seat_of_account(world, requester.account)
    if pid is None:
        return PrincipalKind.INACTIVE
    if world.participants[pid].status is SeatStatus.CONFIRMED:
        return PrincipalKind.SEATED_CONFIRMED
    return PrincipalKind.SEATED


def entitled(state: State, requester: Requester, slot: Slot) -> Entitlement:
    """ADR section 4 `entitled` as amended by 15.2, over `table_principal` (SEC-41, SEC-46, 15.6).

    Every principal that is not refused reads the table slot. Only a confirmed seat reads its own
    participant slot; any other participant slot is ABSENT, exactly as an empty one. It never reads a
    class, the `enforced` flag or `authz_revision` (ED-25, RQ-11).
    """
    principal = table_principal(state, requester)
    if principal is PrincipalKind.UNAUTHENTICATED:
        return Entitlement.UNAUTHENTICATED
    if principal is PrincipalKind.INACTIVE:
        return Entitlement.INACTIVE
    if slot.participant is None:
        return Entitlement.ENTITLED
    if principal is PrincipalKind.SEATED_CONFIRMED and requester.account is not None:
        if seat_of_account(state.world, requester.account) == slot.participant:
            return Entitlement.ENTITLED
    return Entitlement.ABSENT


def _slot_view(state: State, slot: Slot) -> SlotView:
    copy = state.slots.get(slot)
    seq = state.slot_seq.get(slot, 0)
    if copy is None:
        return SlotView(None, 0, (), seq)
    return SlotView(copy.document, copy.version, tuple(sorted(copy.mask)), seq)


def visible(state: State, requester: Requester) -> VisibleView:
    """What a table read returns (SEC-41, TABLE-14, 15.6).

    A refused principal gets its refusal. Otherwise the `"table"` entry, and a `"mine"` entry only for a
    confirmed seat whose own slot holds a copy: an empty own slot and a slot the caller is not entitled
    to look identical.
    """
    principal = table_principal(state, requester)
    if principal is PrincipalKind.UNAUTHENTICATED:
        return VisibleView(Entitlement.UNAUTHENTICATED, {})
    if principal is PrincipalKind.INACTIVE:
        return VisibleView(Entitlement.INACTIVE, {})
    entries = {"table": _slot_view(state, TABLE)}
    if principal is PrincipalKind.SEATED_CONFIRMED and requester.account is not None:
        pid = seat_of_account(state.world, requester.account)
        if pid is not None:
            own = participant_slot(pid)
            if own in state.slots:
                entries["mine"] = _slot_view(state, own)
    return VisibleView(None, entries)


def held(state: State, participant_id: ParticipantId) -> bool:
    """`pending_delivery` minus presence (AUD-10 as amended, SEC-50(5)): a copy the seat cannot read yet."""
    seat = state.world.participants.get(participant_id)
    if participant_slot(participant_id) not in state.slots:
        return False
    return seat is None or seat.status is not SeatStatus.CONFIRMED


def asset_readable(state: State, requester: Requester, document_id: DocumentId, key: FieldKey) -> bool:
    """ED-19: an asset follows its field; readable only through a live slot the requester is entitled to."""
    for slot in slot_order(state.slots):
        copy = state.slots[slot]
        if copy.document == document_id and key in copy.mask:
            if entitled(state, requester, slot) is Entitlement.ENTITLED:
                return True
    return False


def expand_audience(world: World, audience: RevealAudience) -> frozenset[Slot] | None:
    """ED-15, ED-8, TP-1, I-7, I-15: the slots a reveal audience names, fixed at Confirm.

    `None` or an empty set means `AUDIENCE_INVALID`: an unknown or removed participant, an unknown group,
    or an expansion to no slot.
    """
    if isinstance(audience, Table):
        return frozenset({TABLE})
    if isinstance(audience, Participants):
        for pid in audience.ids:
            seat = world.participants.get(pid)
            if seat is None or not seat.active:
                return None
        return frozenset(participant_slot(p) for p in audience.ids)
    if isinstance(audience, Group):
        members = world.groups.get(audience.id)
        if members is None:
            return None
        return frozenset(participant_slot(p) for p in members if world.participants[p].active)
    return frozenset(
        participant_slot(pid) for pid, seat in world.participants.items() if seat.status is SeatStatus.CONFIRMED
    )


def expand_all(world: World, document_id: DocumentId, version: int) -> tuple[FieldKey, ...]:
    """ED-8, REVEAL-9: `all` expanded, when used, to the revealable keys PRESENT in that version."""
    doc = world.documents.get(document_id)
    ver = None if doc is None else doc.version(version)
    if ver is None:
        return ()
    allow = world.revealable(document_id)
    return tuple(sorted(k for k in allow if ver.values.get(k) is Value.PRESENT))


def default_draft(state: State, document_id: DocumentId, version: int, slot: Slot) -> tuple[FieldKey, ...]:
    """REVEAL-4 (ADR section 6, ED-14): the reveal sheet's seeded mask; the first match wins.

    1. The live mask, if the document is live in `slot`.
    2. The type's `default_reveal`, if `slot` is the type's own audience: the table for a table-audience
       type, the linked participant's slot for an owner-audience type.
    3. Otherwise empty.
    Intersected with the revealable, PRESENT keys of that version, and sorted.
    """
    world = state.world
    doc = world.documents.get(document_id)
    if doc is None:
        return ()
    copy = state.slots.get(slot)
    seed: frozenset[FieldKey] | tuple[FieldKey, ...]
    if copy is not None and copy.document == document_id:
        seed = copy.mask
    else:
        ftype = world.types[doc.type_id]
        own = TABLE if ftype.audience == "table" else None
        if ftype.audience == "owner" and doc.linked_participant is not None:
            own = participant_slot(doc.linked_participant)
        seed = world.type_version(doc).default_reveal if slot == own else ()
    allowed = set(expand_all(world, document_id, version))
    return tuple(sorted(k for k in seed if k in allowed))


def _rank(cls: FieldClass) -> int:
    if cls.kind in (ClassKind.UNCLASSIFIED, ClassKind.GM_ONLY):
        return 0
    if cls.kind in LIST_KINDS:
        return 1
    if cls.kind is ClassKind.CAMPAIGN:
        return 2
    return 3


def is_widening(old: FieldClass, new: FieldClass) -> bool:
    """I-9: a change is a locked widening iff `old <= new` in the lattice; anything else narrows (ED-12).

    bottom = {unclassified, gm_only} <= participants/characters/groups (by subset, within one kind)
    <= campaign <= public. bottom-to-bottom is a neutral widening; a cross-kind list change narrows.
    """
    old_rank, new_rank = _rank(old), _rank(new)
    if old_rank == 0:
        return True
    if old_rank == 1:
        if new_rank == 1:
            return old.kind is new.kind and old.ids <= new.ids
        return new_rank > 1
    return new_rank >= old_rank


def narrowest_widening(world: World, current: FieldClass, slots: frozenset[Slot] | None) -> FieldClass | None:
    """ED-13(4), M-4, I-10: the one widening that admits the audience, or `None` when there is none.

    The table: `public`, from any class but `gm_only`. Participant slots `P`: `unclassified` gives
    `participants[P]`, and `participants[X]` gives `participants[X | P]`. Anything else has no single
    widening. An invalid or empty expansion has none either.
    """
    if not slots:
        return None
    if current.kind is ClassKind.GM_ONLY:
        return None
    if TABLE in slots:
        return FieldClass.public() if len(slots) == 1 else None
    ids = frozenset(s.participant for s in slots if s.participant is not None)
    if current.kind is ClassKind.UNCLASSIFIED:
        return FieldClass.participants(*sorted(ids))
    if current.kind is ClassKind.PARTICIPANTS:
        return FieldClass.participants(*sorted(current.ids | ids))
    return None


def automation_may_redisplay(state: State, document_id: DocumentId, key: FieldKey, slot: Slot) -> bool:
    """I-16, ED-17 (O-4), 7.3: the necessary condition for a rule's re-display; arming is Phase 5's.

    Enforced, automation enabled, the last ledgered event for `(document, key, slot)` is `session_ended`
    or a mechanical `stopped` (`replaced`, `moved`), a GM `displayed` of that pair exists, and the key
    is eligible for the slot's audience now.
    """
    world = state.world
    if not (world.enforced and world.automation_enabled):
        return False
    pair = [e for e in state.history if e.ledgered and e.document == document_id and e.key == key and e.slot == slot]
    if not pair:
        return False
    last = pair[-1]
    mechanical = last.kind is EventKind.SESSION_ENDED or (
        last.kind is EventKind.STOPPED and last.reason in (StopReason.REPLACED, StopReason.MOVED)
    )
    if not mechanical:
        return False
    if not any(e.kind is EventKind.DISPLAYED and e.actor is Actor.GM for e in pair):
        return False
    return eligible_for_audience(world, document_id, key, audience_of_slot(slot)).allowed


def unclassified_keys(world: World, document_id: DocumentId) -> tuple[FieldKey, ...]:
    """The classification queue (`1ir.2.7`): revealable keys with no row or an `unclassified` row.

    Keys off the allowlist are `gm_only` by construction and never counted (ED-5, TT-43).
    """
    return tuple(
        sorted(
            k
            for k in world.revealable(document_id)
            if world.classes.get((document_id, k), FieldClass.unclassified()).kind is ClassKind.UNCLASSIFIED
        )
    )


def live_exposure(state: State, document_id: DocumentId, key: FieldKey) -> frozenset[Slot]:
    """The slots whose copy's mask holds the key: "The table is seeing this now" (ED-12, TT-51)."""
    return frozenset(s for s, c in state.slots.items() if c.document == document_id and key in c.mask)


def type_evolution_problems(old: TypeVersion, new: TypeVersion) -> tuple[str, ...]:
    """ED-24's registry check between two versions of one type. Messages name keys only.

    A key reserved in `old` but not in `new`; a key both declared and reserved in `new`; a key declared
    in `old` and neither declared nor reserved in `new`.
    """
    problems: list[str] = []
    for k in sorted(old.reserved - new.reserved):
        problems.append(f"reserved key no longer reserved: {k}")
    for k in sorted(new.declared & new.reserved):
        problems.append(f"key both declared and reserved: {k}")
    for k in sorted(old.declared - new.declared - new.reserved):
        problems.append(f"retired key not reserved: {k}")
    return tuple(problems)


__all__ = [
    "asset_readable",
    "audience_of_slot",
    "automation_may_redisplay",
    "default_draft",
    "eligible_for_audience",
    "entitled",
    "expand_all",
    "expand_audience",
    "held",
    "is_widening",
    "live_exposure",
    "narrowest_widening",
    "seat_of_account",
    "stored_class",
    "table_principal",
    "type_evolution_problems",
    "unclassified_keys",
    "visible",
]

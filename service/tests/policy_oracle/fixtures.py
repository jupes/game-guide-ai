"""Fixture types, the canonical world of ADR section 5 (read through 15.11), and named requesters.

The fixture types are the oracle's own (ADR section 5, F-2): they never collide with production's
closed `DocumentTypeId` enum, and `1kg.7.2`'s test registry can mirror them as data (I-21).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from .model import (
    TABLE,
    AccountId,
    ClassKind,
    Copy,
    Disclosure,
    DisclosureId,
    Document,
    DocumentId,
    FieldClass,
    FieldKey,
    FixtureType,
    GrantId,
    GroupId,
    Participant,
    ParticipantId,
    Release,
    Requester,
    ScreenGrant,
    SeatStatus,
    Session,
    SessionId,
    State,
    TypeVersion,
    Value,
    Version,
    World,
    fkeys,
    frozen_map,
)


def _tv(
    version: int,
    revealable: tuple[str, ...],
    hidden: tuple[str, ...] = (),
    reserved: tuple[str, ...] = (),
    default: tuple[str, ...] = (),
) -> TypeVersion:
    return TypeVersion(
        version=version,
        declared=fkeys(*revealable, *hidden),
        revealable=fkeys(*revealable),
        reserved=fkeys(*reserved),
        default_reveal=tuple(FieldKey(k) for k in default),
    )


NPC_V1: Final = _tv(
    1,
    ("name", "portrait", "history", "rumours", "motives", "notes"),
    ("hidden_identity", "tags", "sources"),
    default=("name", "portrait"),
)
NPC_V2: Final = _tv(
    2,
    ("name", "portrait", "history", "motives", "notes", "secret_ally"),
    ("hidden_identity", "tags", "sources"),
    reserved=("rumours",),
    default=("name", "portrait"),
)
STATBLOCK_V1: Final = _tv(1, ("ac", "hp", "attacks"))
SHEET_V1: Final = _tv(1, ("name", "class", "notes"), default=("class", "name", "notes"))
NOTES_V1: Final = _tv(1, ("name", "recap"))

ORACLE_NPC: Final = FixtureType("oracle_npc", "table", (NPC_V1, NPC_V2))
ORACLE_STATBLOCK: Final = FixtureType("oracle_statblock", "table", (STATBLOCK_V1,))
ORACLE_SHEET: Final = FixtureType("oracle_sheet", "owner", (SHEET_V1,))
ORACLE_NOTES: Final = FixtureType("oracle_notes", "table", (NOTES_V1,))

FIXTURE_TYPES: Final = frozen_map({t.id: t for t in (ORACLE_NPC, ORACLE_STATBLOCK, ORACLE_SHEET, ORACLE_NOTES)})

OWNER: Final = AccountId("acct_gm")
ANA_ID: Final = ParticipantId("ana")
BEN_ID: Final = ParticipantId("ben")
CY_ID: Final = ParticipantId("cy")
ONDREY: Final = DocumentId("ondrey")
ONDREY_SB: Final = DocumentId("ondrey_sb")
KIRA: Final = DocumentId("kira")
MOSS: Final = DocumentId("moss")
MARSH: Final = DocumentId("marsh")
NOTES1: Final = DocumentId("notes1")
SCOUTS: Final = GroupId("scouts")
S1: Final = SessionId("s1")
SCREEN1: Final = GrantId("screen1")

GM: Final = Requester(OWNER, None)
ANA: Final = Requester(AccountId("acct_ana"), None)
BEN: Final = Requester(AccountId("acct_ben"), None)
CY: Final = Requester(AccountId("acct_cy"), None)
STRANGER: Final = Requester(AccountId("acct_stranger"), None)
NOBODY: Final = Requester(None, None)
SCREEN: Final = Requester(None, SCREEN1)


def version_of(ftype: TypeVersion, *, absent: tuple[str, ...] = (), sealed: bool = True, number: int = 1) -> Version:
    """A version with every declared key PRESENT except `absent`."""
    values = {k: (Value.ABSENT if k in absent else Value.PRESENT) for k in sorted(ftype.declared)}
    return Version(number, sealed, frozen_map(values))


def _doc(
    doc_id: DocumentId, ftype: FixtureType, *, link: ParticipantId | None = None, absent: tuple[str, ...] = ()
) -> Document:
    return Document(doc_id, ftype.id, 1, (version_of(ftype.version(1), absent=absent),), False, link)


def _classes() -> dict[tuple[DocumentId, FieldKey], FieldClass]:
    rows: dict[tuple[DocumentId, FieldKey], FieldClass] = {
        (ONDREY, FieldKey("name")): FieldClass.public(),
        (ONDREY, FieldKey("portrait")): FieldClass.public(),
        (ONDREY, FieldKey("history")): FieldClass.campaign(),
        (ONDREY, FieldKey("rumours")): FieldClass.participants(ANA_ID),
        (ONDREY, FieldKey("motives")): FieldClass.gm_only(),
        (MARSH, FieldKey("name")): FieldClass.public(),
        (NOTES1, FieldKey("name")): FieldClass.public(),
    }
    for k in ("ac", "hp", "attacks"):
        rows[(ONDREY_SB, FieldKey(k))] = FieldClass.gm_only()
    for sheet in (KIRA, MOSS):
        for k in ("name", "class", "notes"):
            rows[(sheet, FieldKey(k))] = FieldClass(ClassKind.CHARACTERS, frozenset({sheet}))
    return rows


def canonical_world(release: Release, *, enforced: bool = False, automation: bool = False) -> World:
    """ADR section 5's campaign in account form (15.2, 15.11): Ana and Ben confirmed, Cy offered."""
    assistant = release is Release.ASSISTANT
    participants = {
        ANA_ID: Participant(ANA_ID, SeatStatus.CONFIRMED, AccountId("acct_ana")),
        BEN_ID: Participant(BEN_ID, SeatStatus.CONFIRMED, AccountId("acct_ben")),
        CY_ID: Participant(CY_ID, SeatStatus.OFFERED, None, AccountId("acct_cy")),
    }
    documents = {
        ONDREY: _doc(ONDREY, ORACLE_NPC),
        ONDREY_SB: _doc(ONDREY_SB, ORACLE_STATBLOCK),
        KIRA: _doc(KIRA, ORACLE_SHEET, link=ANA_ID),
        MOSS: _doc(MOSS, ORACLE_SHEET, link=CY_ID),
        MARSH: _doc(MARSH, ORACLE_NOTES, absent=("recap",)),
        NOTES1: _doc(NOTES1, ORACLE_NOTES),
    }
    return World(
        release=release,
        owner=OWNER,
        participants=frozen_map(participants),
        documents=frozen_map(documents),
        groups=frozen_map({SCOUTS: frozenset({ANA_ID, BEN_ID})} if assistant else {}),
        classes=frozen_map(_classes() if assistant else {}),
        enforced=enforced,
        automation_enabled=automation,
        types=FIXTURE_TYPES,
        authz_revision=0,
    )


def state_of(world: World, *, live: bool = True) -> State:
    """A state over `world` with session `s1` (live or ended) and screen grant `screen1`, nothing shown."""
    return State(
        world=world,
        sessions=frozen_map({S1: Session(S1, live=live, expired=False, epoch=0, generation=1)}),
        live=S1 if live else None,
        slots=frozen_map({}),
        slot_seq=frozen_map({}),
        disclosures=frozen_map({}),
        grants=frozen_map({SCREEN1: ScreenGrant(SCREEN1, S1, 1, revoked=False)}),
        history=(),
        commands=frozen_map({}),
        in_flight=frozen_map({}),
    )


def canonical_state(release: Release, *, enforced: bool = False, automation: bool = False) -> State:
    """The canonical state (brief section 5.11): session `s1` live at epoch 0, generation 1, empty slots."""
    return state_of(canonical_world(release, enforced=enforced, automation=automation))


def with_world(state: State, world: World) -> State:
    """The same state over another world: the one way the catalogue applies a world delta."""
    return replace(state, world=world)


def ondrey_on_v2_with_v1_copy(release: Release, *, enforced: bool = False) -> State:
    """TT-31(c): the one state built directly. `ondrey` is on type v2 and a live table copy has a mask
    from v1 made only of keys revealable in both versions, so `rumours` and `secret_ally` are absent."""
    base = canonical_state(release, enforced=enforced)
    world = base.world
    ondrey = world.documents[ONDREY]
    v2 = version_of(NPC_V2)
    docs = dict(world.documents)
    docs[ONDREY] = replace(ondrey, type_version=2, versions=(replace(v2, number=1),))
    classes = {k: v for k, v in world.classes.items() if not (k[0] == ONDREY and k[1] == "rumours")}
    world = replace(world, documents=frozen_map(docs), classes=frozen_map(classes))
    did = DisclosureId("k0")
    mask = fkeys("name", "portrait")
    return replace(
        base,
        world=world,
        slots=frozen_map({TABLE: Copy(did, ONDREY, 1, mask)}),
        slot_seq=frozen_map({TABLE: 1}),
        disclosures=frozen_map({did: Disclosure(did, ONDREY, frozenset({TABLE}), None)}),
        next_disclosure=1,
    )


__all__ = [
    "ANA",
    "ANA_ID",
    "BEN",
    "BEN_ID",
    "CY",
    "CY_ID",
    "FIXTURE_TYPES",
    "GM",
    "KIRA",
    "MARSH",
    "MOSS",
    "NOBODY",
    "NOTES1",
    "NOTES_V1",
    "NPC_V1",
    "NPC_V2",
    "ONDREY",
    "ONDREY_SB",
    "ORACLE_NOTES",
    "ORACLE_NPC",
    "ORACLE_SHEET",
    "ORACLE_STATBLOCK",
    "OWNER",
    "S1",
    "SCOUTS",
    "SCREEN",
    "SCREEN1",
    "SHEET_V1",
    "STATBLOCK_V1",
    "STRANGER",
    "canonical_state",
    "canonical_world",
    "ondrey_on_v2_with_v1_copy",
    "state_of",
    "version_of",
    "with_world",
]

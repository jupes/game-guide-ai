"""The policy oracle's case catalogue (bead `1ir.1.12`).

Every truth-table row of the shared eligibility ADR in both columns, every race case, the precedence,
idempotency and entitlement cases, the canonical world, and a check that the catalogue covers every
row the ADR carries today. The oracle itself is `service/tests/policy_oracle/`; see its README.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from service.tests.policy_oracle import (
    ENT_SESSIONS,
    ENTITLEMENT_MATRIX,
    EXTRA_CASES,
    IDEMPOTENCY_CASES,
    NEVER_PRODUCED,
    ORDER_CASES,
    RACE_CASES,
    TABLE,
    TRUTH_TABLE,
    AccountId,
    Case,
    ClassKind,
    Disposition,
    DocumentId,
    EventKind,
    FieldClass,
    FieldKey,
    GroupId,
    OracleUsageError,
    Participant,
    ParticipantId,
    Release,
    SeatStatus,
    SessionId,
    StopReason,
    Value,
    World,
    canonical_state,
    entitled,
    entitlement_requester,
    entitlement_slot,
    entitlement_state,
    frozen_map,
    lattice_pairs,
)

ADR = Path(__file__).resolve().parents[2] / "docs" / "adr" / "shared-eligibility-display-disclosure.md"
_BY_ID = {c.id: c for c in (*TRUTH_TABLE, *RACE_CASES, *ORDER_CASES, *IDEMPOTENCY_CASES, *EXTRA_CASES)}
_DEDICATED = ("UNLINK-EVERY-COPY", "EVERYONE-SEATED", "ASSET", "ENT-OWNER")


def _columns(cases: tuple[Case, ...]) -> list[object]:
    return [pytest.param(case, column, id=f"{case.id}-{column}") for case in cases for column in ("v1", "enforced")]


def _both(case: Case) -> None:
    case.v1()
    case.enforced()


@pytest.mark.parametrize(("case", "column"), _columns(TRUTH_TABLE))
def test_truth_table_case(case: Case, column: str) -> None:
    getattr(case, column)()


@pytest.mark.parametrize("case", RACE_CASES, ids=[c.id for c in RACE_CASES])
def test_race_case(case: Case) -> None:
    _both(case)


@pytest.mark.parametrize("case", ORDER_CASES, ids=[c.id for c in ORDER_CASES])
def test_order_case(case: Case) -> None:
    _both(case)


@pytest.mark.parametrize("case", IDEMPOTENCY_CASES, ids=[c.id for c in IDEMPOTENCY_CASES])
def test_idempotency_case(case: Case) -> None:
    _both(case)


_EXTRA = tuple(c for c in EXTRA_CASES if c.id not in _DEDICATED)


@pytest.mark.parametrize("case", _EXTRA, ids=[c.id for c in _EXTRA])
def test_critic_case(case: Case) -> None:
    _both(case)


def test_entitlement_matrix_15_6() -> None:
    states = {session: entitlement_state(session) for session in ENT_SESSIONS}
    checked = 0
    for principal, session, slot, expected in ENTITLEMENT_MATRIX:
        got = entitled(states[session], entitlement_requester(principal), entitlement_slot(principal, slot))
        assert got is expected, (principal, session, slot, got.name, expected.name)
        checked += 1
    assert checked == len(ENTITLEMENT_MATRIX) == 25 * len(ENT_SESSIONS)


def test_owner_never_holds_a_participant_slot() -> None:
    _both(_BY_ID["ENT-OWNER"])


def test_everyone_seated_expands_to_confirmed_seats() -> None:
    _both(_BY_ID["EVERYONE-SEATED"])


def test_lattice_pairs() -> None:
    lattice_pairs()


def test_unlink_stops_every_copy() -> None:
    _both(_BY_ID["UNLINK-EVERY-COPY"])


def test_asset_follows_its_field() -> None:
    _both(_BY_ID["ASSET"])


@pytest.mark.parametrize("release", list(Release), ids=[r.name for r in Release])
def test_canonical_state_is_pinned(release: Release) -> None:
    assistant = release is Release.ASSISTANT
    state = canonical_state(release, enforced=assistant)
    world = state.world
    assert world.owner == "acct_gm"
    assert {p.id: (p.status, p.account, p.offered_to) for p in world.participants.values()} == {
        "ana": (SeatStatus.CONFIRMED, "acct_ana", None),
        "ben": (SeatStatus.CONFIRMED, "acct_ben", None),
        "cy": (SeatStatus.OFFERED, None, "acct_cy"),
    }
    docs = {d.id: (d.type_id, d.type_version, d.linked_participant, d.archived) for d in world.documents.values()}
    assert docs == {
        "ondrey": ("oracle_npc", 1, None, False),
        "ondrey_sb": ("oracle_statblock", 1, None, False),
        "kira": ("oracle_sheet", 1, "ana", False),
        "moss": ("oracle_sheet", 1, "cy", False),
        "marsh": ("oracle_notes", 1, None, False),
        "notes1": ("oracle_notes", 1, None, False),
    }
    for doc in world.documents.values():
        assert [(v.number, v.sealed) for v in doc.versions] == [(1, True)]
        declared = world.type_version(doc).declared
        absent = {"recap"} if doc.id == "marsh" else set()
        assert {k: v for k, v in doc.latest.values.items()} == {
            k: (Value.ABSENT if k in absent else Value.PRESENT) for k in declared
        }
    expected_classes: dict[tuple[str, str], FieldClass] = {}
    if assistant:
        expected_classes = {
            ("ondrey", "name"): FieldClass.public(),
            ("ondrey", "portrait"): FieldClass.public(),
            ("ondrey", "history"): FieldClass.campaign(),
            ("ondrey", "rumours"): FieldClass.participants("ana"),
            ("ondrey", "motives"): FieldClass.gm_only(),
            ("marsh", "name"): FieldClass.public(),
            ("notes1", "name"): FieldClass.public(),
            **{("ondrey_sb", k): FieldClass.gm_only() for k in ("ac", "hp", "attacks")},
            **{("kira", k): FieldClass.characters("kira") for k in ("name", "class", "notes")},
            **{("moss", k): FieldClass.characters("moss") for k in ("name", "class", "notes")},
        }
    assert dict(world.classes) == expected_classes
    assert dict(world.groups) == ({"scouts": frozenset({"ana", "ben"})} if assistant else {})
    assert (world.enforced, world.automation_enabled, world.authz_revision) == (assistant, False, 0)
    assert {s.id: (s.live, s.expired, s.epoch, s.generation) for s in state.sessions.values()} == {
        "s1": (True, False, 0, 1)
    }
    assert state.live == "s1"
    assert {g.id: (g.session, g.generation, g.revoked) for g in state.grants.values()} == {"screen1": ("s1", 1, False)}
    assert (dict(state.slots), dict(state.disclosures), state.history, dict(state.commands)) == ({}, {}, (), {})


def test_v1_world_refuses_classes_groups_and_flags() -> None:
    base = canonical_state(Release.WORKBENCH_V1).world
    row = {(DocumentId("ondrey"), FieldKey("name")): FieldClass.public()}
    scouts = {GroupId("scouts"): frozenset({ParticipantId("ana")})}
    attempts: list[Callable[[], World]] = [
        lambda: replace(base, classes=frozen_map(row)),
        lambda: replace(base, groups=frozen_map(scouts)),
        lambda: replace(base, enforced=True),
        lambda: replace(base, automation_enabled=True),
    ]
    for attempt in attempts:
        with pytest.raises(OracleUsageError, match="Workbench v1"):
            attempt()
    for account, message in (("acct_ana", "one live seat"), ("acct_gm", "owner holds no seat")):
        seats = dict(base.participants)
        seats[ParticipantId("ben")] = Participant(ParticipantId("ben"), SeatStatus.CONFIRMED, AccountId(account))
        with pytest.raises(OracleUsageError, match=message):
            replace(base, participants=frozen_map(seats))
    for doc_id, link, message in (("moss", "ana", "at most one linked sheet"), ("ondrey", "ben", "owner-audience")):
        docs = dict(base.documents)
        docs[DocumentId(doc_id)] = replace(docs[DocumentId(doc_id)], linked_participant=ParticipantId(link))
        with pytest.raises(OracleUsageError, match=message):
            replace(base, documents=frozen_map(docs))
    assert canonical_state(Release.ASSISTANT, enforced=True, automation=True).world.automation_enabled
    with pytest.raises(OracleUsageError, match="requires enforcement"):
        canonical_state(Release.ASSISTANT, enforced=False, automation=True)


def test_catalogue_covers_every_adr_row() -> None:
    text = ADR.read_text(encoding="utf-8")
    rows = re.findall(r"^\| (TT|RC)-(\d+) \|", text, flags=re.MULTILINE)
    adr_ids = [f"{kind}-{n}" for kind, n in rows]
    assert len(adr_ids) == len(set(adr_ids)) == 71
    catalogued = [c.id for c in (*TRUTH_TABLE, *RACE_CASES)]
    assert len(catalogued) == len(set(catalogued))
    assert set(catalogued) == set(adr_ids)
    for case in (*TRUTH_TABLE, *RACE_CASES):
        if case.disposition in (Disposition.SUPERSEDED, Disposition.ACCOUNT_FORM):
            assert case.supersession, case.id
        if case.disposition is Disposition.SUPERSEDED:
            assert case.successor in _BY_ID, case.id
        if case.disposition in (Disposition.NOT_MODELLABLE, Disposition.FOOTPRINT, Disposition.INVARIANT):
            assert case.owner, case.id


def test_stop_reason_vocabulary_is_ed17() -> None:
    assert [r.value for r in StopReason] == [
        "gm_stop",
        "stop_all",
        "mask_narrowed",
        "replaced",
        "moved",
        "link_rotated",
        "participant_removed",
        "personal_link_reset",
        "character_unlinked",
        "group_member_removed",
        "eligibility_tightened",
        "enforcement_enabled",
        "document_archived",
        "document_deleted",
        "campaign_deleted",
    ]
    assert NEVER_PRODUCED == {StopReason.PERSONAL_LINK_RESET, StopReason.CAMPAIGN_DELETED}
    assert [e.value for e in EventKind] == ["displayed", "updated", "stopped", "session_ended", "retracted", "exported"]
    assert [k.value for k in ClassKind] == [
        "unclassified",
        "gm_only",
        "participants",
        "characters",
        "groups",
        "campaign",
        "public",
    ]
    assert TABLE.participant is None
    assert SessionId("s1") == "s1"

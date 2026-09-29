"""The decision point's own rules (bead 1ir.2.2; brief section 10.1, T-P1 to T-P18).

Hand-built facts, one per rule of ADR section 4 (through 15.2) and threat model 15.2, each asserting
the reason or the kind as well as the answer; the seal; purity; and T-P18's scan, which pins who may
call the deciders and build the facts they read (critic item 1). The oracle comparison is in
`test_policy_oracle_pdp.py`.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Final, cast

import pytest

from service import policy
from service.campaign_store import MissingParent
from service.eligibility_store import GM_ONLY_BY_CONSTRUCTION, Eligibility
from service.eligibility_store import ClassificationSource as Src
from service.eligibility_store import FieldClass as F
from service.policy import EligReason as R
from service.policy import Entitlement as E
from service.policy import SeatFacts, SessionFacts, TableFacts, TableKind, TableRefusal
from service.workbench_contracts import DOC_TYPE_VERSION, DocumentTypeId, revealable_fields

ROOT: Final = Path(__file__).resolve().parents[2]
#: Far in the past, so a decision that read the wall clock would see every session expired.
NOW: Final = datetime(2001, 1, 1, 12, tzinfo=UTC)


def _id(prefix: str, name: str) -> str:
    return prefix + name.ljust(22, "x")


C, C2 = _id("cmp_", "one"), _id("cmp_", "two")
DOC, SHEET, SHEET2 = _id("doc_", "npc"), _id("doc_", "kira"), _id("doc_", "moss")
ANA, BEN, GONE, NOBODY = _id("prt_", "ana"), _id("prt_", "ben"), _id("prt_", "gone"), _id("prt_", "nobody")
SCOUTS, OLD = _id("grp_", "scouts"), _id("grp_", "old")
GRANT, SES, SES2 = _id("tcr_", "screen"), _id("ses_", "live"), _id("ses_", "old")
KEYS: Final = frozenset({"name", "history", "rumours", "notes", "secret", "kin", "band"})


def registry(type_id: str, type_version: int) -> frozenset[str]:
    return KEYS if (type_id, type_version) == ("npc", 1) else frozenset()


def row(field_class: F, *ids: str) -> Eligibility:
    return Eligibility(field_class, ids, Src.GM)


ROWS: Final = {
    "name": row(F.PUBLIC),
    "history": row(F.CAMPAIGN),
    "rumours": row(F.PARTICIPANTS, ANA),
    "secret": row(F.GM_ONLY),
    "kin": row(F.CHARACTERS, SHEET),
    "band": row(F.GROUPS, SCOUTS),
    "hidden": row(F.PUBLIC),  # an orphan: off the allowlist
}  # notes: revealable and unclassified


def facts(rows: dict[str, Eligibility] = ROWS, **changes: object) -> policy.PolicyFacts:
    base = policy.PolicyFacts(
        campaign_id=C,
        documents={DOC: policy.DocumentFacts(DOC, "npc", 1)},
        classes={(DOC, key): value for key, value in rows.items()},
        sheet_links={SHEET: ANA},
        active_participants=frozenset({ANA, BEN}),
        group_members={SCOUTS: frozenset({ANA})},
        revealable=registry,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def verdict(f: policy.PolicyFacts, who: str | None, key: str, document: str = DOC) -> tuple[bool, R]:
    got = policy.eligible_for_audience(f, document, key, who)
    return got.allowed, got.reason


@pytest.mark.parametrize(
    ("who", "key", "want"),
    [
        (GONE, "name", (False, R.INACTIVE_PARTICIPANT)),
        (NOBODY, "name", (False, R.INACTIVE_PARTICIPANT)),
        (ANA, "hidden", (False, R.NOT_REVEALABLE)),
        (ANA, "notes", (False, R.UNCLASSIFIED)),
        (ANA, "secret", (False, R.GM_ONLY)),
        (None, "name", (True, R.PUBLIC)),
        (BEN, "name", (True, R.PUBLIC)),
        (None, "history", (False, R.TABLE_NEEDS_PUBLIC)),
        (BEN, "history", (True, R.CAMPAIGN)),
        (ANA, "rumours", (True, R.NAMED)),
        (BEN, "rumours", (False, R.NOT_NAMED)),
        (ANA, "kin", (True, R.LINKED)),
        (BEN, "kin", (False, R.NOT_LINKED)),
        (ANA, "band", (True, R.MEMBER)),
        (BEN, "band", (False, R.NOT_MEMBER)),
    ],
)
def test_each_rule_of_section_4_in_order(who: str | None, key: str, want: tuple[bool, R]) -> None:
    """T-P1: one row per step of section 4, reason asserted (M-P1 to M-P9)."""
    assert verdict(facts(), who, key) == want
    assert verdict(facts(), who, key, document=SHEET2)[1] in (R.NOT_REVEALABLE, R.INACTIVE_PARTICIPANT)


def test_an_inactive_participant_is_refused_before_a_public_key_and_before_an_unknown_key() -> None:
    """T-P2: step 1 comes first (M-P2), for a removed seat and for an id nobody holds."""
    assert verdict(facts(), GONE, "name") == (False, R.INACTIVE_PARTICIPANT)
    assert verdict(facts(), NOBODY, "hidden") == (False, R.INACTIVE_PARTICIPANT)
    assert verdict(facts(), NOBODY, "name", document=SHEET2) == (False, R.INACTIVE_PARTICIPANT)


def test_an_orphan_row_is_never_read() -> None:
    """T-P3: ED-5, ED-24: a public row off the allowlist, or of a stale type version, is ignored (M-P3)."""
    assert verdict(facts(), None, "hidden") == (False, R.NOT_REVEALABLE)
    stale = facts(documents={DOC: policy.DocumentFacts(DOC, "npc", 2)})
    assert verdict(stale, ANA, "name") == (False, R.NOT_REVEALABLE)


def test_the_table_is_public_only() -> None:
    """T-P4: TP-1 (signed): campaign and every list class, even one naming everyone, are refused
    for the table (M-P4); public is not (M-P5, in T-P1)."""
    everyone = {**ROWS, "rumours": row(F.PARTICIPANTS, ANA, BEN)}
    for key in ("history", "rumours", "kin", "band"):
        assert verdict(facts(everyone), None, key) == (False, R.TABLE_NEEDS_PUBLIC), key


def test_list_ids_that_resolve_to_nothing_admit_nobody() -> None:
    """T-P5: I-10: an unlinked sheet, a sheet the facts do not hold (another campaign's, a deleted
    one), a removed group, a removed member and an unknown participant id admit nobody (M-P6, M-P7)."""
    other = {
        **ROWS,
        "kin": row(F.CHARACTERS, SHEET2),
        "band": row(F.GROUPS, OLD),
        "rumours": row(F.PARTICIPANTS, NOBODY),
    }
    assert verdict(facts(other, sheet_links={SHEET: ANA, SHEET2: None}), ANA, "kin") == (False, R.NOT_LINKED)
    assert verdict(facts(other, sheet_links={}), ANA, "kin") == (False, R.NOT_LINKED)
    assert verdict(facts(other), ANA, "band") == (False, R.NOT_MEMBER)
    assert verdict(facts(group_members={SCOUTS: frozenset()}), ANA, "band") == (False, R.NOT_MEMBER)
    assert verdict(facts(other), ANA, "rumours") == (False, R.NOT_NAMED)
    assert verdict(facts(), SHEET, "kin")[1] is R.INACTIVE_PARTICIPANT  # a list id is never an audience


@pytest.mark.parametrize(
    "raw",
    [
        ("secret_class", None, "gm"),
        ("participants", [], "gm"),
        ("participants", None, "gm"),
        ("participants", [SHEET], "gm"),
        ("participants", [BEN, ANA], "gm"),
        ("participants", [ANA, ANA], "gm"),
        ("participants", ANA, "gm"),
        ("participants", [ANA, 7], "gm"),
        ("public", [ANA], "gm"),
        ("public", None, "default"),
        ("public", None, "suggested"),
        ("unclassified", None, "gm"),
        (None, None, None),
        (5, None, b"gm"),
    ],
)
def test_undecodable_rows_are_gm_only(raw: tuple[object, object, object]) -> None:
    """T-P6: I-11: a row this build cannot read is gm_only by construction, never wider (M-P19)."""
    assert policy.decode_class(*raw) == GM_ONLY_BY_CONSTRUCTION


def test_decodable_rows_decode_exactly() -> None:
    assert policy.decode_class("public", None, "gm") == row(F.PUBLIC)
    assert policy.decode_class("participants", [ANA, BEN], "gm") == row(F.PARTICIPANTS, ANA, BEN)
    assert policy.decode_class("groups", (SCOUTS,), "gm") == row(F.GROUPS, SCOUTS)
    assert policy.decode_class("gm_only", None, "default") == Eligibility(F.GM_ONLY, (), Src.DEFAULT)


def test_ineligible_keys_is_all_or_nothing_sorted_and_distinct() -> None:
    """T-P7: TT-32's shape (M-P17, M-P18); bare strings and an empty audience are refused."""
    f = facts()
    assert policy.ineligible_keys(f, DOC, ["name", "rumours", "history"], [ANA]) == ()
    assert policy.ineligible_keys(f, DOC, ["name", "rumours", "history"], [ANA, BEN]) == ("rumours",)
    assert policy.ineligible_keys(f, DOC, ["rumours", "history", "rumours", "name"], [None]) == ("history", "rumours")
    assert policy.ineligible_keys(f, DOC, [], [ANA]) == ()
    with pytest.raises(ValueError):
        policy.ineligible_keys(f, DOC, ["name"], [])
    with pytest.raises(TypeError):
        policy.ineligible_keys(f, DOC, "name", [ANA])
    with pytest.raises(TypeError):
        policy.ineligible_keys(f, DOC, ["name"], ANA)


def test_ineligible_copies_names_every_slot_holding_one_bad_key() -> None:
    """T-P8: ED-12's scan: each slot is checked against its own audience."""
    copies = {
        None: (DOC, ["name"]),
        ANA: (DOC, ["rumours", "kin"]),
        BEN: (DOC, ["history", "rumours"]),
        GONE: (DOC, ["name"]),
    }
    assert policy.ineligible_copies(facts(), copies) == {BEN, GONE}
    assert policy.ineligible_copies(facts(), {}) == frozenset()


# ------------------------------------------------------------------------------ the table principal


def session(
    sid: str = SES, *, state: str = "live", expires: datetime = NOW + timedelta(hours=1), campaign: str = C
) -> SessionFacts:
    return SessionFacts(sid, campaign, state, expires, 3)


def seat(pid: str = ANA, *, accepted: bool = True, confirmed: bool = True, removed: bool = False) -> SeatFacts:
    return SeatFacts(pid, accepted, confirmed, removed)


def grant(
    *, gid: str = GRANT, generation: int = 3, revoked: bool = False, of: SessionFacts | None = None
) -> policy.GrantFacts:
    return policy.GrantFacts(gid, generation, revoked, of or session())


def table(**changes: object) -> TableFacts:
    base = TableFacts(C, 1, (session(),), (), None, {ANA: frozenset({SHEET})}, {ANA: frozenset({SCOUTS})}, (7, 5))
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def decide(
    f: TableFacts, account: int | None = None, grant_id: str | None = None, now: datetime = NOW
) -> policy.TableOutcome:
    return policy.decide_table(f, account_id=account, grant_id=grant_id, now=now)


def kind(outcome: policy.TableOutcome) -> TableKind | TableRefusal:
    return outcome.kind if isinstance(outcome, policy.TablePrincipal) else outcome


EXPIRED: Final = session(expires=NOW - timedelta(seconds=1))
#: Two state-live rows, one of them expired: an invariant break (I-23, critic item 6).
TWO_LIVE: Final = (session(), session(SES2, expires=EXPIRED.expires_at))
SCREEN, INACTIVE, UNAUTH = TableKind.SCREEN, TableRefusal.INACTIVE, TableRefusal.UNAUTHENTICATED


@pytest.mark.parametrize(
    ("f", "account", "grant_id", "want"),
    [
        (table(grant=grant()), None, GRANT, SCREEN),
        (table(grant=grant(revoked=True)), None, GRANT, UNAUTH),  # M-P13
        (table(grant=grant(generation=2)), None, GRANT, UNAUTH),  # M-P12
        (table(grant=grant(of=session(state="ended"))), None, GRANT, UNAUTH),
        (table(grant=grant(of=EXPIRED)), None, GRANT, UNAUTH),  # M-P15
        (table(grant=grant(of=session(expires=NOW))), None, GRANT, UNAUTH),  # MX6: expiring now is expired
        (table(grant=grant(gid=_id("tcr_", "other"))), None, GRANT, UNAUTH),
        (table(), None, GRANT, UNAUTH),
        (table(grant=grant(of=session(campaign=C2))), None, GRANT, INACTIVE),
        (table(grant=grant(), revisions=None), None, GRANT, INACTIVE),
        (table(grant=grant(), owner_id=None), None, GRANT, INACTIVE),  # MX2: no campaign, even for a live grant
        (table(grant=grant(), live_sessions=TWO_LIVE), None, GRANT, INACTIVE),
        (table(grant=grant(of=session(SES2))), None, GRANT, INACTIVE),  # MX1: not the campaign's live session
        (table(), None, None, UNAUTH),
        (table(owner_id=None), 1, None, INACTIVE),
        (table(revisions=None), 1, None, INACTIVE),
        (table(live_sessions=()), 1, None, INACTIVE),
        (table(live_sessions=(EXPIRED,)), 1, None, INACTIVE),  # M-P15
        (table(live_sessions=(session(expires=NOW),)), 1, None, INACTIVE),  # MX6: expiring now is expired
        (table(live_sessions=TWO_LIVE), 1, None, INACTIVE),  # M-P22
        (table(live_sessions=(session(state="ended"),)), 1, None, INACTIVE),
        (table(live_sessions=(session(campaign=C2),)), 1, None, INACTIVE),
        (table(), 1, None, TableKind.OWNER_VIEWER),
        (table(), 2, None, INACTIVE),
        (table(seats=(seat(accepted=False, confirmed=False),)), 2, None, INACTIVE),
        (table(seats=(seat(removed=True),)), 2, None, INACTIVE),
        (table(seats=(seat(), seat(BEN))), 2, None, INACTIVE),
        (table(seats=(seat(BEN, removed=True), seat(confirmed=False))), 2, None, TableKind.SEATED_AWAITING),
        (table(seats=(seat(),)), 2, None, TableKind.SEATED_CONFIRMED),
    ],
)
def test_decide_table_follows_section_15_2_in_order(
    f: TableFacts, account: int | None, grant_id: str | None, want: TableKind | TableRefusal
) -> None:
    """T-P9: every step of `decide_table`, with each fail-closed branch of I-23 and critic item 6."""
    assert kind(decide(f, account, grant_id)) is want


def test_the_principals_carry_what_they_were_resolved_from() -> None:
    confirmed = decide(table(seats=(seat(),)), 2)
    assert isinstance(confirmed, policy.TablePrincipal)
    assert dataclasses.asdict(confirmed) == dict(
        kind=TableKind.SEATED_CONFIRMED, campaign_id=C, session_id=SES, generation=3, account_id=2, grant_id=None,
        participant_id=ANA, scope=ANA, characters={SHEET}, groups={SCOUTS}, authz_revision=7, projection_revision=5,
    )  # fmt: skip
    owner = decide(table(), 1)
    assert isinstance(owner, policy.TablePrincipal)
    assert (owner.scope, owner.participant_id, owner.characters, owner.groups) == (None, None, frozenset(), frozenset())
    screen = decide(table(grant=grant()), None, GRANT)
    assert isinstance(screen, policy.TablePrincipal) and (screen.account_id, screen.grant_id) == (None, GRANT)
    with pytest.raises(ValueError):
        decide(table(), 1, now=NOW.replace(tzinfo=None))


def test_decide_gm_is_the_owner_or_the_one_missing_parent() -> None:
    """T-P9, critic item 1: no facts, or facts whose owner is another account, are one MissingParent (M-P27)."""
    for gm in (None, policy.GmFacts(owner_id=9, archived=False, authz_revision=1, projection_revision=1)):
        with pytest.raises(MissingParent):
            policy.decide_gm(gm, campaign_id=C, account_id=1)
    got = policy.decide_gm(policy.GmFacts(1, True, 4, 3), campaign_id=C, account_id=1)
    assert (got.campaign_id, got.account_id, got.archived, got.authz_revision, got.projection_revision) == (
        C,
        1,
        True,
        4,
        3,
    )


def test_entitled_matrix() -> None:
    """T-P10: every outcome by {the table, its own slot, another seat's, an unknown id} (M-P10, M-P11, M-P16)."""
    outcomes = {
        "unauth": UNAUTH,
        "inactive": INACTIVE,
        "owner": decide(table(), 1),
        "awaiting": decide(table(seats=(seat(confirmed=False),)), 2),
        "confirmed": decide(table(seats=(seat(),)), 2),
        "screen": decide(table(grant=grant()), None, GRANT),
    }
    want = {
        "unauth": [E.UNAUTHENTICATED] * 4,
        "inactive": [E.INACTIVE] * 4,
        "owner": [E.ENTITLED, E.ABSENT, E.ABSENT, E.ABSENT],
        "awaiting": [E.ENTITLED, E.ABSENT, E.ABSENT, E.ABSENT],
        "confirmed": [E.ENTITLED, E.ENTITLED, E.ABSENT, E.ABSENT],
        "screen": [E.ENTITLED, E.ABSENT, E.ABSENT, E.ABSENT],
    }
    for name, outcome in outcomes.items():
        assert [policy.entitled(outcome, s) for s in (None, ANA, BEN, NOBODY)] == want[name], name


def test_the_registry_seam_is_the_eligibility_stores_allowlist() -> None:
    """T-P11, critic item 9: independent literals (M-P20)."""
    for doc_type in DocumentTypeId:
        v = DOC_TYPE_VERSION[doc_type]
        assert policy.registry_revealable(doc_type.value, v) == frozenset(revealable_fields(doc_type)) != frozenset()
        assert (
            policy.registry_revealable(doc_type.value, v - 1)
            == policy.registry_revealable(doc_type.value, v + 1)
            == frozenset()
        )
    for unknown in ("nonesuch", "oracle_npc", ""):
        assert policy.registry_revealable(unknown, 1) == frozenset()


def test_an_awaiting_seat_is_table_scoped() -> None:
    """T-P12: I-9, SEC-50(5): the seat is named, but has no scope, sheets or groups (M-P23)."""
    got = decide(table(seats=(seat(confirmed=False),)), 2)
    assert isinstance(got, policy.TablePrincipal) and got.kind is TableKind.SEATED_AWAITING
    assert (got.participant_id, got.scope, got.characters, got.groups) == (ANA, None, frozenset(), frozenset())


def test_a_live_grant_decides_alone_and_another_campaigns_grant_is_inactive() -> None:
    """T-P13: I-8, SEC-44(3): the owner's account never upgrades a screen; a foreign live grant is
    inactive even with the owner's account; a dead grant is ignored (M-P14)."""
    assert kind(decide(table(grant=grant()), 1, GRANT)) is SCREEN
    assert kind(decide(table(grant=grant(of=session(campaign=C2))), 1, GRANT)) is INACTIVE
    assert kind(decide(table(grant=grant(revoked=True)), 1, GRANT)) is TableKind.OWNER_VIEWER


def test_principals_are_sealed() -> None:
    """T-P14: I-3 (M-P21): forged, replaced or re-built principals are a TypeError; a copy forges nothing."""
    principal, gm = (
        decide(table(seats=(seat(),)), 2),
        policy.decide_gm(policy.GmFacts(1, False, 0, 0), campaign_id=C, account_id=1),
    )
    assert isinstance(principal, policy.TablePrincipal)
    with pytest.raises(TypeError):
        policy.TablePrincipal(**dataclasses.asdict(principal))
    with pytest.raises(TypeError):
        policy.GmPrincipal(C, 1, False, 0, 0)
    for forged in (lambda: dataclasses.replace(principal, participant_id=BEN), lambda: dataclasses.replace(principal)):
        with pytest.raises(TypeError):
            forged()
    with pytest.raises(TypeError):
        dataclasses.replace(gm, account_id=2)
    assert copy.copy(principal) == principal and copy.copy(gm) == gm


def test_the_deciders_refuse_facts_that_are_not_facts() -> None:
    """T-P14 (H-1): the review's forgery, duck-typed facts handed to the minting path, never reaches the
    seal. Both deciders refuse anything that is not their own facts type."""
    loaded = table(seats=(seat(),))
    forged = SimpleNamespace(**{f.name: getattr(loaded, f.name) for f in dataclasses.fields(TableFacts)})
    with pytest.raises(TypeError):
        decide(cast(TableFacts, forged), 2)
    gm = SimpleNamespace(owner_id=1, archived=False, authz_revision=0, projection_revision=0)
    with pytest.raises(TypeError):
        policy.decide_gm(cast(policy.GmFacts, gm), campaign_id=C, account_id=1)


def _minted_in(tree: ast.Module, names: set[str]) -> dict[str, set[str]]:
    """Each of `names` -> the top-level definitions of `tree` that call it."""
    where: dict[str, set[str]] = {}
    for top in tree.body:
        for node in ast.walk(top):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names:
                where.setdefault(node.func.id, set()).add(getattr(top, "name", "<module>"))
    return where


def test_the_seal_opens_only_inside_the_two_deciders_around_one_constructor() -> None:
    """T-P14 (H-1, L-4): minting is unreachable except through `decide_table` and `decide_gm`. No other
    function of `service/policy.py` opens the seal or builds a principal, so no helper exists that mints
    from arguments the deciders never checked. `_MINTING` is touched only by the seal itself. While the
    seal is open, only the constructor runs, on locals computed before it opened, so nothing the facts
    hold (a crafted `Mapping.get`, a property) ever runs with the seal open."""
    tree = ast.parse((ROOT / "service" / "policy.py").read_text(encoding="utf-8"))
    assert _minted_in(tree, {"_minting", "TablePrincipal", "GmPrincipal", "_sealed"}) == {
        "_minting": {"decide_table", "decide_gm"},
        "TablePrincipal": {"decide_table"},
        "GmPrincipal": {"decide_gm"},
        "_sealed": {"TablePrincipal", "GmPrincipal"},
    }
    touches = {
        getattr(top, "name", "<module>")
        for top in tree.body
        for node in ast.walk(top)
        if isinstance(node, ast.Name) and node.id in {"_MINTING", "_minting"} and isinstance(node.ctx, ast.Load)
    }
    assert touches == {"_minting", "_sealed", "decide_table", "decide_gm"}
    sealed_blocks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With) and any(ast.unparse(item.context_expr) == "_minting()" for item in node.items)
    ]
    assert len(sealed_blocks) == 2
    for block in sealed_blocks:
        assert len(block.items) == 1 and len(block.body) == 1, ast.unparse(block)
        (only,) = block.body
        assert isinstance(only, ast.Return) and isinstance(only.value, ast.Call), ast.unparse(block)
        call = only.value
        assert ast.unparse(call.func) in {"TablePrincipal", "GmPrincipal"} and not call.args, ast.unparse(block)
        assert all(isinstance(k.value, ast.Name) and k.arg for k in call.keywords), ast.unparse(block)


def test_repr_hides_the_account() -> None:
    """T-P15 (M-P24)."""
    principal = decide(table(owner_id=424242), 424242)
    gm = policy.decide_gm(policy.GmFacts(424243, False, 0, 0), campaign_id=C, account_id=424243)
    assert "424242" not in repr(principal) and "424243" not in repr(gm)


def test_decisions_read_no_flag_revision_or_clock_they_were_not_given() -> None:
    """T-P16: ED-25, RQ-11 (M-P25): entitlement is the same whatever the revisions, eligibility is given
    none, and `decide_table` reads the `now` it is given, never the wall clock."""
    slots = (None, ANA, BEN)
    fresh, stale = (
        decide(table(seats=(seat(),), revisions=(5, 5)), 2),
        decide(table(seats=(seat(),), revisions=(9, 2)), 2),
    )
    assert [policy.entitled(fresh, s) for s in slots] == [policy.entitled(stale, s) for s in slots]
    names = {f.name for f in dataclasses.fields(policy.PolicyFacts)}
    assert not {n for n in names if "revision" in n or "enforce" in n or "flag" in n}
    later = datetime(2999, 1, 1, tzinfo=UTC)
    assert kind(decide(table(), 1, now=later)) is INACTIVE
    far = table(live_sessions=(session(expires=later + timedelta(days=1)),))
    assert kind(decide(far, 1, now=later)) is TableKind.OWNER_VIEWER


# --------------------------------------------------------------------------------- structural scans

_POLICY_IMPORTS: Final = {
    "eligibility_store": {
        "Eligibility",
        "FieldClass",
        "ClassificationSource",
        "GM_ONLY_BY_CONSTRUCTION",
        "revealable_keys_of",
    },
    "campaign_store": {"MissingParent", "aware"},
    "campaign_identity": None,
}


def test_policy_is_pure() -> None:
    """T-P17 (M-P26): `service/policy.py` imports only the standard library and the pure names above,
    and calls no `open`, `time.*` or clock."""
    tree = ast.parse((ROOT / "service" / "policy.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in sys.stdlib_module_names for a in node.names), ast.dump(node)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                assert (node.module or "").split(".")[0] in sys.stdlib_module_names | {"__future__"}, node.module
            else:
                assert node.module in _POLICY_IMPORTS, node.module
                allowed = _POLICY_IMPORTS[node.module]
                assert allowed is None or {a.name for a in node.names} <= allowed, node.module
        elif isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            assert callee != "open" and not callee.startswith("time."), callee
            assert callee.split(".")[-1] not in {"now", "utcnow", "today", "time", "monotonic"}, callee


#: T-P18 (critic items 1 and 2). Referencing these outside their modules is a violation.
_REFERENCED_ONLY_IN: Final = {
    "decide_table": {"service/principals.py"},
    "decide_gm": {"service/principals.py"},
    "_minting": {"service/policy.py"},
    "_MINTING": {"service/policy.py"},
}
_FACTS: Final = ("TableFacts", "GmFacts", "SessionFacts", "SeatFacts", "GrantFacts")
#: Calling (constructing) these outside their modules is a violation.
_CALLED_ONLY_IN: Final = {
    **{name: {"service/principals.py"} for name in _FACTS},
    "PolicyFacts": {"service/policy_facts.py", "service/policy.py"},
    "DocumentFacts": {"service/policy_facts.py", "service/policy.py"},
    "TablePrincipal": {"service/policy.py"},
    "GmPrincipal": {"service/policy.py"},
    "decode_session": {"service/app.py"},
}
_PINNED: Final = {**_REFERENCED_ONLY_IN, **_CALLED_ONLY_IN}


_POLICY: Final = "service/policy.py"
#: Reflection that reaches a module's attributes by string, around the name rules below.
_REFLECTION: Final = frozenset({"getattr", "setattr", "delattr", "vars"})


def minting_violations(path: str, source: str) -> list[str]:
    """What a production module at `path` does that only the allowlisted modules may do.

    Outside `service/policy.py`, beyond the pinned names: any `_`-prefixed name of the policy module
    (H-1: the seal, and any helper that builds or decides, present or future), reflection on the
    module or on a name imported from it, an assignment into either, `object.__setattr__`, and any
    `replace(` at all in a module that imports the policy module (M-1: facts a consumer holds are
    widened by a `replace` whatever it imported to read them)."""
    tree, found = ast.parse(source), list[str]()
    local: dict[str, str] = {}
    replacers, copy_modules = set[str](), set[str]()
    policy_modules, from_policy = set[str](), set[str]()  # what names the policy module; names bound from it
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            of_policy = node.module == "service.policy" or (node.level > 0 and node.module == "policy")
            of_package = node.module == "service" or (node.level > 0 and node.module is None)
            for a in node.names:
                local[a.asname or a.name] = a.name
                if node.module in ("dataclasses", "copy") and a.name == "replace":
                    replacers.add(a.asname or a.name)
                if of_package and a.name == "policy":
                    policy_modules.add(a.asname or a.name)
                if of_policy:
                    from_policy.add(a.asname or a.name)
                    if a.name.startswith("_") and path != _POLICY:
                        found.append(f"{path}: {a.name} is private to {_POLICY}")
        elif isinstance(node, ast.Import):
            copy_modules |= {a.asname or a.name for a in node.names if a.name in ("dataclasses", "copy")}
            policy_modules |= {a.asname or a.name for a in node.names if a.name == "service.policy"}
    imported = set(local.values())
    reaches = policy_modules | from_policy
    imports_policy = bool(reaches) and path != _POLICY

    def name_of(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return local.get(node.id, node.id)
        return node.attr if isinstance(node, ast.Attribute) else None

    def into_policy(node: ast.expr) -> bool:
        text = ast.unparse(node)
        return any(text == m or text.startswith(m + ".") for m in reaches)

    for node in ast.walk(tree):
        named = [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else [name_of(node)]
        found += [f"{path}: {n}" for n in named if n in _REFERENCED_ONLY_IN and path not in _REFERENCED_ONLY_IN[n]]
        if isinstance(node, ast.Attribute) and imports_policy and into_policy(node.value):
            if node.attr.startswith("_"):
                found.append(f"{path}: {ast.unparse(node)} is private to {_POLICY}")
            if not isinstance(node.ctx, ast.Load):
                found.append(f"{path}: {ast.unparse(node)} assigned")
        if not isinstance(node, ast.Call):
            continue
        callee = name_of(node.func)
        if callee in _CALLED_ONLY_IN and path not in _CALLED_ONLY_IN[callee]:
            found.append(f"{path}: {callee}(")
        if imports_policy and isinstance(node.func, ast.Name) and node.func.id in _REFLECTION:
            if node.args and into_policy(node.args[0]):
                found.append(f"{path}: {node.func.id}( on {_POLICY}")
        if imports_policy and ast.unparse(node.func) in ("object.__setattr__", "object.__delattr__"):
            found.append(f"{path}: {ast.unparse(node.func)}(")
        via_module = isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) in copy_modules
        direct = isinstance(node.func, ast.Name) and node.func.id in replacers
        if (direct or (callee == "replace" and via_module)) and (
            imports_policy or any(n in imported and path not in _PINNED[n] for n in _PINNED)
        ):
            found.append(f"{path}: replace( of a pinned type")
        # The registry seam is a callable; `FieldRule(revealable=True)` is the registry's own bool flag.
        seam = [k for k in node.keywords if k.arg == "revealable" and not isinstance(k.value, ast.Constant)]
        if path != "service/policy.py" and seam:
            found.append(f"{path}: revealable=")
    return found


def test_minting_is_reachable_only_from_the_loaders() -> None:
    """T-P18: the seal cannot tell a loader's facts from forged ones, so who may decide and who may
    build facts is pinned over every production module (M-P28, M-P29, M-F11), and no production module
    may name anything private to `service/policy.py`, reflect on it, or assign into it (H-1).

    Out of this scan's reach, as of any AST tripwire (review N-1): a dynamic import (`importlib`,
    `__import__`, `sys.modules`); reflection on an object that is not named by an import from the
    policy module (a principal passed in as an argument, a function's `__globals__` or `__closure__`);
    and a `replace(` in a module that never imports the policy module but is handed facts by one that
    does. Review is the control for those."""
    files = [p for p in (ROOT / "service").rglob("*.py") if "tests" not in p.relative_to(ROOT / "service").parts]
    assert len(files) > 50
    violations = [v for p in files for v in minting_violations(p.relative_to(ROOT).as_posix(), p.read_text("utf-8"))]
    assert violations == []


#: The review's H-1 plant: a confirmed seat forged by a private helper of the policy module.
REVIEW_PLANT: Final = (
    "from service import policy as _p\n_p._mint(f, _p.TableKind.SEATED_CONFIRMED, s, (0, 0), 99, participant_id=pid)"
)


@pytest.mark.parametrize(
    "planted",
    [
        "from service.policy import decide_gm as d\nd(None, campaign_id='c', account_id=1)",
        "from service import policy\npolicy.decide_table(f, account_id=1, grant_id=None, now=n)",
        "from .policy import TableFacts\nTableFacts('c', 1, (), (), None, {}, {}, (0, 0))",
        "from .policy import TableFacts as T\nT('c', 1, (), (), None, {}, {}, (0, 0))",
        "from dataclasses import replace\nfrom .policy import GmFacts\nreplace(facts, owner_id=1)",
        "import dataclasses\nfrom .policy import TablePrincipal\ndataclasses.replace(p)",
        "from .policy_facts import PostgresPolicyFacts\nPostgresPolicyFacts().load(u, c, d, revealable=r)",
        "from .session import decode_session\ndecode_session(t, s, 1)",
        "from .policy import _minting",
        "from . import policy\npolicy.PolicyFacts(c, {}, {}, {}, frozenset(), {})",
        "import dataclasses as d\nd.replace(facts, revealable=lambda t, v: frozenset({'secret'}))",
        # H-1: the review's plant, and every other way to reach the seal from outside the module.
        REVIEW_PLANT,
        "from .policy import _mint",
        "import service.policy\nwith service.policy._minting():\n    pass",
        "import service.policy as sp\nsp._MINTING.set(True)",
        "from service import policy\ngetattr(policy, 'decide_gm')(None, campaign_id=c, account_id=1)",
        "from service import policy\nsetattr(policy, '_sealed', lambda kind: None)",
        "from . import policy\npolicy._sealed = lambda kind: None",
        "from service import policy\npolicy.TablePrincipal = dict",
        "from .policy import TablePrincipal\nTablePrincipal.__post_init__ = lambda self: None",
        "from service import policy\nvars(policy)['_MINTING'].set(True)",
        "from service import policy\npolicy.__dict__['_mint']",
        "import copy\nfrom .policy import entitled\nobject.__setattr__(copy.copy(p), 'participant_id', pid)",
        # M-1: a `replace(` in any module that imports the policy module, whatever it imports from it.
        "from dataclasses import replace\nfrom .policy import eligible_for_audience\nreplace(f, active_participants=a)",
        "import dataclasses\nfrom service import policy\ndataclasses.replace(facts, classes={})",
        "import copy as c\nimport service.policy\nc.replace(facts, sheet_links={})",
        "from dataclasses import replace as r\nfrom .policy import eligible_for_audience\nr(f, **{'revealable': fn})",
    ],
)
def test_the_minting_scan_flags_each_planted_violation(planted: str) -> None:
    assert minting_violations("service/workbench_planted_api.py", planted)
    allowed = (
        "from .policy import decide_table, TableFacts\ndecide_table(TableFacts(), account_id=1, grant_id=None, now=n)"
    )
    assert minting_violations("service/principals.py", allowed) == []


def test_a_consumer_of_the_public_decisions_is_not_flagged() -> None:
    """The scan refuses reach into the module, not use of it: public calls and enum reads pass."""
    consumer = (
        "from dataclasses import dataclass\nfrom service import policy\nfrom .policy import TableKind, entitled\n"
        "entitled(p, None)\npolicy.eligible_for_audience(f, d, k, None).allowed\nTableKind.SCREEN.value\n"
        "policy.ineligible_keys(f, d, m, (None,))\n"
    )
    assert minting_violations("service/workbench_consumer.py", consumer) == []


def _private_names_of_policy() -> set[str]:
    tree = ast.parse((ROOT / "service" / "policy.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {n for n in names if n.startswith("_")}


#: Every way a production module can name something in `service.policy`.
_REACH: Final = (
    "from service.policy import {name}",
    "from .policy import {name} as x",
    "from service import policy\npolicy.{name}",
    "from . import policy as p\np.{name}()",
    "import service.policy as sp\nsp.{name}",
    "import service.policy\nservice.policy.{name}",
    "from service import policy\ngetattr(policy, '{name}')",
)


def test_every_private_name_of_policy_is_refused_outside_it() -> None:
    """T-P18 (H-1): not a list of today's private helpers but all of them, now and later. A private name
    of `service/policy.py` referenced from any other production module, by any import form or by
    reflection, is a violation; so is one the module no longer defines (the review's `_mint`)."""
    private = _private_names_of_policy()
    assert {"_minting", "_MINTING", "_sealed"} <= private
    for name in sorted(private | {"_mint"}):
        for form in _REACH:
            planted = form.format(name=name)
            assert minting_violations("service/workbench_planted_api.py", planted), planted


def test_the_reviews_plant_in_table_api_fails_the_scan() -> None:
    """T-P18 (H-1), as the review ran it: a `_mint` call appended to `service/table_api.py` is flagged,
    and the module as committed is not."""
    path = "service/table_api.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert minting_violations(path, source) == []
    assert minting_violations(path, f"{source}\n{REVIEW_PLANT}\n")

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
from typing import Final

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
        (table(grant=grant(gid=_id("tcr_", "other"))), None, GRANT, UNAUTH),
        (table(), None, GRANT, UNAUTH),
        (table(grant=grant(of=session(campaign=C2))), None, GRANT, INACTIVE),
        (table(grant=grant(), revisions=None), None, GRANT, INACTIVE),
        (table(grant=grant(), live_sessions=TWO_LIVE), None, GRANT, INACTIVE),
        (table(), None, None, UNAUTH),
        (table(owner_id=None), 1, None, INACTIVE),
        (table(revisions=None), 1, None, INACTIVE),
        (table(live_sessions=()), 1, None, INACTIVE),
        (table(live_sessions=(EXPIRED,)), 1, None, INACTIVE),  # M-P15
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


def minting_violations(path: str, source: str) -> list[str]:
    """What a production module at `path` does that only the allowlisted modules may do."""
    tree, found = ast.parse(source), list[str]()
    local: dict[str, str] = {}
    replacers, copy_modules = set[str](), set[str]()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                local[a.asname or a.name] = a.name
                if node.module in ("dataclasses", "copy") and a.name == "replace":
                    replacers.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            copy_modules |= {a.asname or a.name for a in node.names if a.name in ("dataclasses", "copy")}
    imported = set(local.values())

    def name_of(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return local.get(node.id, node.id)
        return node.attr if isinstance(node, ast.Attribute) else None

    for node in ast.walk(tree):
        named = [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else [name_of(node)]
        found += [f"{path}: {n}" for n in named if n in _REFERENCED_ONLY_IN and path not in _REFERENCED_ONLY_IN[n]]
        if not isinstance(node, ast.Call):
            continue
        callee = name_of(node.func)
        if callee in _CALLED_ONLY_IN and path not in _CALLED_ONLY_IN[callee]:
            found.append(f"{path}: {callee}(")
        via_module = isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) in copy_modules
        direct = isinstance(node.func, ast.Name) and node.func.id in replacers
        if (direct or (callee == "replace" and via_module)) and any(
            n in imported and path not in _PINNED[n] for n in _PINNED
        ):
            found.append(f"{path}: replace( of a pinned type")
        # The registry seam is a callable; `FieldRule(revealable=True)` is the registry's own bool flag.
        seam = [k for k in node.keywords if k.arg == "revealable" and not isinstance(k.value, ast.Constant)]
        if path != "service/policy.py" and seam:
            found.append(f"{path}: revealable=")
    return found


def test_minting_is_reachable_only_from_the_loaders() -> None:
    """T-P18: the seal cannot tell a loader's facts from forged ones, so who may decide and who may
    build facts is pinned over every production module (M-P28, M-P29, M-F11)."""
    files = [p for p in (ROOT / "service").rglob("*.py") if "tests" not in p.relative_to(ROOT / "service").parts]
    assert len(files) > 50
    violations = [v for p in files for v in minting_violations(p.relative_to(ROOT).as_posix(), p.read_text("utf-8"))]
    assert violations == []


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
    ],
)
def test_the_minting_scan_flags_each_planted_violation(planted: str) -> None:
    assert minting_violations("service/workbench_planted_api.py", planted)
    allowed = (
        "from .policy import decide_table, TableFacts\ndecide_table(TableFacts(), account_id=1, grant_id=None, now=n)"
    )
    assert minting_violations("service/principals.py", allowed) == []

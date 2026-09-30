"""The policy oracle's adapter for the decision point (bead 1ir.2.2, brief section 10.2).

It maps an oracle `World` / `State` / `Requester` onto `service.policy`'s facts by ROW SELECTION only:
it never decides anything. Oracle ids become shaped ids (`prt_` + the id padded with `-` to 22
characters: injective, because oracle ids never hold `-`); an account becomes an int, the world's
owner 1. The fixture registry answers the oracle's own types, which are never production types
(oracle brief F-2). `shadow()` re-computes every call the oracle makes to `eligible_for_audience`,
`table_principal` and `entitled` through the decision point and records every disagreement.
PR-2 and PR-3 import it as `from service.tests import pdp_fixtures`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from service import policy
from service.eligibility_store import ClassificationSource, Eligibility, FieldClass
from service.tests.policy_oracle import (
    FIXTURE_TYPES,
    ClassKind,
    DocumentId,
    EligAudience,
    FieldKey,
    FixtureType,
    ParticipantAudienceId,
    PrincipalKind,
    Requester,
    SeatStatus,
    Session,
    Slot,
    State,
    World,
    describe,
)
from service.tests.policy_oracle import Eligibility as OracleVerdict
from service.tests.policy_oracle import Entitlement as OracleEntitlement
from service.tests.policy_oracle import FieldClass as OracleClass
from service.tests.policy_oracle import cases as oracle_cases
from service.tests.policy_oracle import generate as oracle_generate
from service.tests.policy_oracle import machine as oracle_machine
from service.tests.policy_oracle import rules as oracle_rules

#: The clock every adapted state is read at; an expired session ended a second before it.
T0: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
CAMPAIGN: Final = "cmp_" + "oracle".ljust(22, "-")
OWNER_ACCOUNT: Final = 1
#: The table-principal kinds, in the oracle's vocabulary (`PrincipalKind` names).
KIND_NAMES: Final[Mapping[policy.TableKind | policy.TableRefusal, str]] = {
    policy.TableKind.OWNER_VIEWER: "OWNER",
    policy.TableKind.SEATED_AWAITING: "SEATED",
    policy.TableKind.SEATED_CONFIRMED: "SEATED_CONFIRMED",
    policy.TableKind.SCREEN: "SCREEN",
    policy.TableRefusal.INACTIVE: "INACTIVE",
    policy.TableRefusal.UNAUTHENTICATED: "UNAUTHENTICATED",
}
#: The ops after which the shadow must compare a table principal (critic item 11).
TRANSITIONS: Final = ("End", "Rotate", "Expire", "RevokeScreen", "RemoveParticipant", "ConfirmSeat")
_LIST_PREFIX: Final = {ClassKind.PARTICIPANTS: "prt_", ClassKind.CHARACTERS: "doc_", ClassKind.GROUPS: "grp_"}
_CACHE_MAX: Final = 20_000


def shaped(prefix: str, oracle_id: str) -> str:
    return prefix + oracle_id.ljust(22, "-")


def prt(pid: str) -> str:
    return shaped("prt_", pid)


def doc(did: str) -> str:
    return shaped("doc_", did)


def audience(who: EligAudience) -> policy.SlotId:
    return prt(who.participant) if isinstance(who, ParticipantAudienceId) else None


def slot(which: Slot) -> policy.SlotId:
    return None if which.participant is None else prt(which.participant)


_INTERNED: dict[str, int] = {}


def account(world: World, acct: str | None) -> int | None:
    """The world's owner is 1; any other account string is interned to one int from 2 on."""
    if acct is None:
        return None
    if acct == world.owner:
        return OWNER_ACCOUNT
    return _INTERNED.setdefault(acct, len(_INTERNED) + 2)


def registry(types: Mapping[str, FixtureType] = FIXTURE_TYPES) -> policy.RevealableKeys:
    def revealable(type_id: str, type_version: int) -> frozenset[str]:
        found = types.get(type_id)
        if found is None or not 1 <= type_version <= len(found.versions):
            return frozenset()
        return frozenset(found.version(type_version).revealable)

    return revealable


fixture_revealable: Final = registry()


def eligibility(cls: OracleClass) -> Eligibility | None:
    """An oracle class as the row the database would hold; None for `unclassified` (never stored)."""
    if cls.kind is ClassKind.UNCLASSIFIED:
        return None
    ids = tuple(sorted(shaped(_LIST_PREFIX[cls.kind], i) for i in cls.ids)) if cls.kind in _LIST_PREFIX else ()
    return Eligibility(FieldClass(cls.kind.value), ids, ClassificationSource.GM)


def policy_facts(world: World) -> policy.PolicyFacts:
    classes: dict[tuple[str, str], Eligibility] = {
        (doc(d), k): e for (d, k), c in world.classes.items() if (e := eligibility(c)) is not None
    }
    return policy.PolicyFacts(
        campaign_id=CAMPAIGN,
        documents={
            doc(d.id): policy.DocumentFacts(doc(d.id), d.type_id, d.type_version) for d in world.documents.values()
        },
        classes=classes,
        sheet_links={
            doc(d.id): None if d.linked_participant is None else prt(d.linked_participant)
            for d in world.documents.values()
        },
        active_participants=frozenset(prt(p.id) for p in world.participants.values() if p.active),
        group_members={
            shaped("grp_", g): frozenset(prt(m) for m in members if world.participants[m].active)
            for g, members in world.groups.items()
        },
        revealable=fixture_revealable if world.types is FIXTURE_TYPES else registry(world.types),
    )


def _session(s: Session) -> policy.SessionFacts:
    expires = T0 - timedelta(seconds=1) if s.expired else T0 + timedelta(hours=1)
    return policy.SessionFacts(shaped("ses_", s.id), CAMPAIGN, "live" if s.live else "ended", expires, s.generation)


def table_facts(state: State, requester: Requester) -> policy.TableFacts:
    """Row selection only: the rows a loader's one statement would return for this request."""
    world = state.world
    seats = [p for p in world.participants.values() if requester.account is not None and p.account == requester.account]
    grant = None if requester.screen_grant is None else state.grants.get(requester.screen_grant)
    granted = None
    if grant is not None:
        granted = policy.GrantFacts(
            shaped("tcr_", grant.id), grant.generation, grant.revoked, _session(state.sessions[grant.session])
        )
    confirmed, removed = SeatStatus.CONFIRMED, SeatStatus.REMOVED
    return policy.TableFacts(
        campaign_id=CAMPAIGN,
        owner_id=OWNER_ACCOUNT,
        live_sessions=tuple(_session(s) for _, s in sorted(state.sessions.items()) if s.live),
        # An oracle seat holds its account only once accepted, so every seat of the account is accepted.
        seats=tuple(policy.SeatFacts(prt(p.id), True, p.status is confirmed, p.status is removed) for p in seats),
        grant=granted,
        sheets={
            prt(p.id): frozenset(doc(d.id) for d in world.documents.values() if d.linked_participant == p.id)
            for p in seats
        },
        groups={prt(p.id): frozenset(shaped("grp_", g) for g, ms in world.groups.items() if p.id in ms) for p in seats},
        revisions=(world.authz_revision, world.authz_revision),
    )


def outcome(state: State, requester: Requester) -> policy.TableOutcome:
    grant = None if requester.screen_grant is None else shaped("tcr_", requester.screen_grant)
    acct = account(state.world, requester.account)
    return policy.decide_table(table_facts(state, requester), account_id=acct, grant_id=grant, now=T0)


def kind_name(result: policy.TableOutcome) -> str:
    return KIND_NAMES[result.kind if isinstance(result, policy.TablePrincipal) else result]


@dataclass
class Shadow:
    """What the shadow compared, by reason / kind / entitlement, and every disagreement."""

    reasons: Counter[str] = field(default_factory=Counter)
    kinds: Counter[str] = field(default_factory=Counter)
    entitlements: Counter[str] = field(default_factory=Counter)
    after: Counter[str] = field(default_factory=Counter)
    mismatches: list[str] = field(default_factory=list)
    _facts: dict[int, tuple[World, policy.PolicyFacts]] = field(default_factory=dict)
    _made_by: dict[int, tuple[State, str]] = field(default_factory=dict)

    def facts(self, world: World) -> policy.PolicyFacts:
        """Eligibility facts cached by `id(world)` with a strong reference (a new `State` around the
        same `World` has the same eligibility facts). Table facts are never cached (critic item 11)."""
        hit = self._facts.get(id(world))
        if hit is None:
            if len(self._facts) >= _CACHE_MAX:
                self._facts.clear()
            hit = self._facts[id(world)] = (world, policy_facts(world))
        return hit[1]

    def made(self, state: State, op_name: str) -> None:
        if len(self._made_by) >= _CACHE_MAX:
            self._made_by.clear()
        self._made_by[id(state)] = (state, op_name)

    def disagree(self, what: str, oracle: str, pdp: str, state: State | World) -> None:
        where = describe(state)[:600] if isinstance(state, State) else ""
        self.mismatches.append(f"{what}: oracle {oracle}, decision point {pdp}; {where}")

    def op_before(self, state: State) -> str | None:
        made = self._made_by.get(id(state))
        return None if made is None or made[0] is not state else made[1]


#: Where each rule is bound (R-14). `raising=True`: if the oracle's structure moves, the shadow fails.
_BINDINGS: Final = (
    (oracle_rules, ("eligible_for_audience", "table_principal", "entitled")),
    (oracle_machine, ("eligible_for_audience",)),
    (oracle_generate, ("eligible_for_audience",)),
    (oracle_cases, ("eligible_for_audience", "table_principal", "entitled")),
)
_APPLY: Final = ((oracle_machine, "apply"), (oracle_generate, "apply"), (oracle_cases, "apply_step"))


@contextmanager
def shadow() -> Iterator[Shadow]:
    """Rebind the oracle's three rule functions to wrappers that call the ORIGINAL, compute the
    decision point's answer through this adapter, record any disagreement, and return the oracle's
    answer, so the catalogue and the machine run exactly as before."""
    seen = Shadow()
    elig, principal, entitled = oracle_rules.eligible_for_audience, oracle_rules.table_principal, oracle_rules.entitled
    apply = oracle_machine.apply

    def eligible_w(world: World, document_id: DocumentId, key: FieldKey, who: EligAudience) -> OracleVerdict:
        want = elig(world, document_id, key, who)
        got = policy.eligible_for_audience(seen.facts(world), doc(document_id), key, audience(who))
        seen.reasons[got.reason.name] += 1
        if (got.allowed, got.reason.name) != (want.allowed, want.reason.name):
            seen.disagree("eligible_for_audience", want.reason.name, got.reason.name, world)
        return want

    def principal_w(state: State, requester: Requester) -> PrincipalKind:
        want = principal(state, requester)
        got = kind_name(outcome(state, requester))
        seen.kinds[got] += 1
        if (op := seen.op_before(state)) is not None:
            seen.after[op] += 1
        if got != want.name:
            seen.disagree("table_principal", want.name, got, state)
        return want

    def entitled_w(state: State, requester: Requester, which: Slot) -> OracleEntitlement:
        want = entitled(state, requester, which)
        got = policy.entitled(outcome(state, requester), slot(which))
        seen.entitlements[got.name] += 1
        if got.name != want.name:
            seen.disagree("entitled", want.name, got.name, state)
        return want

    def apply_w(state: State, step: oracle_machine.Step) -> oracle_machine.StepResult:
        result = apply(state, step)
        seen.made(result.after, type(step.op).__name__)
        return result

    wrappers: dict[str, Callable[..., object]] = {
        "eligible_for_audience": eligible_w,
        "table_principal": principal_w,
        "entitled": entitled_w,
    }
    with pytest.MonkeyPatch.context() as patch:
        for module, names in _BINDINGS:
            for name in names:
                patch.setattr(module, name, wrappers[name], raising=True)
        for module, name in _APPLY:
            patch.setattr(module, name, apply_w, raising=True)
        yield seen

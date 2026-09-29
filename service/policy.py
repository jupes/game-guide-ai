"""The policy decision point (agent-forge-harness-1ir.2.2): who may see what.

Written from the shared eligibility ADR (ACCEPTED) section 4, read through its
section 15.2, and from the threat model's section 15.2 `table_principal`. Two
functions, never one (ED-25):

* `eligible_for_audience` is evaluated on **identities** (the table, or a
  participant id, whether or not anyone has signed in as that seat: AUD-10).
  The reveal Confirm, exports and every narrowing scan use it (ED-9, ED-12,
  ED-21); `ineligible_keys` and `ineligible_copies` are those compositions.
* `entitled` is evaluated on the **principal** a request resolved to, at every
  table read. It needs a live session and an active seat, and never reads a
  class, a revision or a flag (ED-25, RQ-11).

**Read in SQL, decide in Python.** Every function here is pure: it reads only
the facts and the clock it is given. The loaders read one snapshot each; this
module decides from it, so it can be compared with the policy oracle at scale.

**Sealed principals.** `TablePrincipal` and `GmPrincipal` are built only while
`decide_table` or `decide_gm` builds them; construction or `dataclasses.replace`
anywhere else is a `TypeError`. Who may call the deciders or build their facts
is pinned by `service/tests/test_policy.py` (T-P18): the seal alone cannot tell
a loader's facts from forged ones.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Final

from .campaign_store import MissingParent, aware
from .eligibility_store import (
    GM_ONLY_BY_CONSTRUCTION,
    ClassificationSource,
    Eligibility,
    FieldClass,
    revealable_keys_of,
)

_log = logging.getLogger(__name__)

#: A slot, and an eligibility audience: a participant id, or None for the table
#: (I-25, `reveal_store.check_targets`'s convention). None is the strictest.
SlotId = str | None
#: A type id and type version -> that version's revealable allowlist.
RevealableKeys = Callable[[str, int], frozenset[str]]
#: Production's registry seam (I-7): the same function `classifiable_keys` uses.
registry_revealable: Final[RevealableKeys] = revealable_keys_of


class EligReason(str, Enum):
    """Why `eligible_for_audience` answered as it did (ADR section 4, in order)."""

    INACTIVE_PARTICIPANT = "inactive_participant"
    NOT_REVEALABLE = "not_revealable"
    UNCLASSIFIED = "unclassified"
    GM_ONLY = "gm_only"
    PUBLIC = "public"
    TABLE_NEEDS_PUBLIC = "table_needs_public"
    CAMPAIGN = "campaign"
    NAMED = "named"
    NOT_NAMED = "not_named"
    LINKED = "linked"
    NOT_LINKED = "not_linked"
    MEMBER = "member"
    NOT_MEMBER = "not_member"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: EligReason


@dataclass(frozen=True)
class DocumentFacts:
    id: str
    type: str
    type_version: int


@dataclass(frozen=True)
class PolicyFacts:
    """One campaign's eligibility facts, as one snapshot read them (I-10).

    `documents`: the requested documents that exist in this campaign.
    `classes`: their stored rows, decoded (`decode_class`), orphans included;
    the allowlist is applied at evaluation (ED-24). `sheet_links`: each document
    a `characters` row names -> its linked participant now. `active_participants`:
    the campaign's seats that are not removed. `group_members`: each live group
    a `groups` row names -> its members whose seats are not removed. An id found
    in none of them resolves to nobody."""

    campaign_id: str
    documents: Mapping[str, DocumentFacts]
    classes: Mapping[tuple[str, str], Eligibility]
    sheet_links: Mapping[str, str | None]
    active_participants: frozenset[str]
    group_members: Mapping[str, frozenset[str]]
    revealable: RevealableKeys = registry_revealable


def decode_class(raw_class: object, raw_ids: object, raw_source: object) -> Eligibility:
    """One stored row's class, or `GM_ONLY_BY_CONSTRUCTION` when this build
    cannot read it (I-11): an unknown class or source, a malformed list, a
    `default` source wider than `gm_only`. Never raises, never logs (the loaders
    count and log), never answers wider than the row."""
    if raw_ids is not None and not isinstance(raw_ids, list | tuple):
        return GM_ONLY_BY_CONSTRUCTION
    try:  # the constructor refuses every shape a row may not hold (non-str ids included)
        return Eligibility(FieldClass(raw_class), tuple(raw_ids or ()), ClassificationSource(raw_source))
    except (ValueError, TypeError):
        return GM_ONLY_BY_CONSTRUCTION


def eligible_for_audience(facts: PolicyFacts, document_id: str, key: str, audience: SlotId) -> Verdict:
    """ADR section 4, on identities; the first match decides. It never reads the
    enforcement flag (callers gate on it) and never raises for well-typed
    arguments: an unknown document, key or participant is a denial."""
    if audience is not None and audience not in facts.active_participants:
        return Verdict(False, EligReason.INACTIVE_PARTICIPANT)  # before anything, even a public key
    document = facts.documents.get(document_id)
    if document is None or key not in facts.revealable(document.type, document.type_version):
        return Verdict(False, EligReason.NOT_REVEALABLE)  # ED-5, ED-24: an orphan row is never read
    stored = facts.classes.get((document_id, key))
    field_class = FieldClass.UNCLASSIFIED if stored is None else stored.field_class
    if field_class is FieldClass.UNCLASSIFIED:
        return Verdict(False, EligReason.UNCLASSIFIED)
    if field_class is FieldClass.GM_ONLY:
        return Verdict(False, EligReason.GM_ONLY)
    if field_class is FieldClass.PUBLIC:
        return Verdict(True, EligReason.PUBLIC)
    if audience is None:
        return Verdict(False, EligReason.TABLE_NEEDS_PUBLIC)  # TP-1: the table is public-only
    if field_class is FieldClass.CAMPAIGN:
        return Verdict(True, EligReason.CAMPAIGN)
    ids = () if stored is None else stored.ids  # a list class is always a stored row
    if field_class is FieldClass.PARTICIPANTS:
        named = audience in ids
        return Verdict(named, EligReason.NAMED if named else EligReason.NOT_NAMED)
    if field_class is FieldClass.CHARACTERS:
        linked = any(facts.sheet_links.get(sheet) == audience for sheet in ids)  # linked NOW (ED-4)
        return Verdict(linked, EligReason.LINKED if linked else EligReason.NOT_LINKED)
    member = any(audience in facts.group_members.get(group, frozenset()) for group in ids)  # a member NOW
    return Verdict(member, EligReason.MEMBER if member else EligReason.NOT_MEMBER)


def ineligible_keys(
    facts: PolicyFacts, document_id: str, mask: Collection[str], audiences: Collection[SlotId]
) -> tuple[str, ...]:
    """The sorted, distinct keys of `mask` that some audience member may not see:
    all or nothing over the audience (ED-9's fourth precondition, ED-15, TT-32;
    ED-21's export passes the table). An empty audience is the Confirm's
    `AUDIENCE_INVALID`, never an answer here."""
    if isinstance(mask, str) or isinstance(audiences, str):
        raise TypeError("a mask and an audience are collections, never a bare str")
    if not audiences:
        raise ValueError("an eligibility check needs at least one audience")

    def refused(key: str) -> bool:
        return any(not eligible_for_audience(facts, document_id, key, who).allowed for who in audiences)

    return tuple(sorted({key for key in mask if refused(key)}))


def ineligible_copies(facts: PolicyFacts, copies: Mapping[SlotId, tuple[str, Collection[str]]]) -> frozenset[SlotId]:
    """The slots whose copy (document, mask) holds a key not eligible for that
    slot's own audience: ED-12's narrowing scan and M-3's enforcement-on scan."""
    return frozenset(
        slot for slot, (document_id, mask) in copies.items() if ineligible_keys(facts, document_id, mask, (slot,))
    )


class TableKind(str, Enum):
    """What a table request may read (threat model 15.2, 15.6). The owner is a
    table viewer on a table route, never the GM (SEC-44(1))."""

    OWNER_VIEWER = "owner_viewer"
    SEATED_AWAITING = "seated_awaiting"
    SEATED_CONFIRMED = "seated_confirmed"
    SCREEN = "screen"


class TableRefusal(str, Enum):
    """SEC-46's one `inactive`, and the 401 of a request with no credentials."""

    INACTIVE = "inactive"
    UNAUTHENTICATED = "unauthenticated"


class Entitlement(str, Enum):
    """`entitled`'s answers. `ABSENT` is exactly what an empty slot gives."""

    ENTITLED = "entitled"
    ABSENT = "absent"
    INACTIVE = "inactive"
    UNAUTHENTICATED = "unauthenticated"


_MINTING: ContextVar[bool] = ContextVar("policy_minting", default=False)


@contextmanager
def _minting() -> Iterator[None]:
    token = _MINTING.set(True)
    try:
        yield
    finally:
        _MINTING.reset(token)


def _sealed(kind: str) -> None:
    if not _MINTING.get():
        raise TypeError(f"a {kind} is built only by the decision point")


@dataclass(frozen=True)
class TablePrincipal:
    """Evidence of standing at the moment it was resolved; it authorises no
    content read by itself. A content read re-asserts the session, generation,
    seat, confirmation and grant in the query that reads (SEC-41, SEC-16).
    `participant_id` identifies a seat for revocation tracking and is never an
    entitlement: only `entitled()` or `scope` is.

    `scope` is the participant id only for a GM-confirmed seat, else None: the
    table audience (SEC-50(5), D-12). The revisions are carried for freshness
    (`1ir.2.5`) and are never evidence here (RQ-11)."""

    kind: TableKind
    campaign_id: str
    session_id: str
    generation: int
    account_id: int | None = field(repr=False)
    grant_id: str | None
    participant_id: str | None
    scope: SlotId
    characters: frozenset[str]
    groups: frozenset[str]
    authz_revision: int
    projection_revision: int

    def __post_init__(self) -> None:
        _sealed("table principal")


TableOutcome = TablePrincipal | TableRefusal


@dataclass(frozen=True)
class GmPrincipal:
    """The campaign's owner on a GM route: ownership was checked in the query
    that loaded the facts and again here (SEC-2). Evidence of standing when
    resolved, never a content authorisation by itself (see `TablePrincipal`)."""

    campaign_id: str
    account_id: int = field(repr=False)
    archived: bool
    authz_revision: int
    projection_revision: int

    def __post_init__(self) -> None:
        _sealed("GM principal")


@dataclass(frozen=True)
class SessionFacts:
    id: str
    campaign_id: str
    state: str
    expires_at: datetime
    generation: int


@dataclass(frozen=True)
class SeatFacts:
    participant_id: str
    accepted: bool
    confirmed: bool
    removed: bool


@dataclass(frozen=True)
class GrantFacts:
    id: str
    generation: int
    revoked: bool
    session: SessionFacts


@dataclass(frozen=True)
class TableFacts:
    """One snapshot of a table request's standing in one campaign: `owner_id`
    (None: no such campaign), its `state = 'live'` sessions, the account's seats
    there in any status, the grant named with its own session (any campaign),
    participant -> linked sheets / live groups, and (authz_revision,
    projection_revision), None without an `authz_state` row."""

    campaign_id: str
    owner_id: int | None
    live_sessions: tuple[SessionFacts, ...]
    seats: tuple[SeatFacts, ...]
    grant: GrantFacts | None
    sheets: Mapping[str, frozenset[str]]
    groups: Mapping[str, frozenset[str]]
    revisions: tuple[int, int] | None


@dataclass(frozen=True)
class GmFacts:
    owner_id: int
    archived: bool
    authz_revision: int
    projection_revision: int


def _invariant_broken(code: str) -> TableRefusal:
    """I-23: a state an index or trigger forbids is denied, with a closed code."""
    _log.error("policy decision point: invariant %s broken; answered inactive", code)
    return TableRefusal.INACTIVE


def _open(session: SessionFacts, now: datetime) -> bool:
    """Live and unexpired: expiry is a clock comparison, never `state` alone (SEC-42)."""
    return session.state == "live" and session.expires_at > now


def decide_table(facts: TableFacts, *, account_id: int | None, grant_id: str | None, now: datetime) -> TableOutcome:
    """Threat model 15.2 `table_principal`, in order (critic item 6)."""
    now = aware(now, "now")
    grant, revisions = facts.grant, facts.revisions
    if (
        grant_id is not None
        and grant is not None
        and grant.id == grant_id
        and not grant.revoked
        and _open(grant.session, now)
        and grant.generation == grant.session.generation
    ):
        # A live grant decides alone; the account is not consulted (SEC-44(3), SEC-48).
        if grant.session.campaign_id != facts.campaign_id or facts.owner_id is None or revisions is None:
            return TableRefusal.INACTIVE
        if len(facts.live_sessions) != 1 or facts.live_sessions[0].id != grant.session.id:
            return _invariant_broken("live_sessions")
        return _mint(facts, TableKind.SCREEN, grant.session, revisions, None, grant_id=grant.id)
    if account_id is None:
        return TableRefusal.UNAUTHENTICATED
    if facts.owner_id is None:
        return TableRefusal.INACTIVE
    if revisions is None:
        return _invariant_broken("authz_state")
    if len(facts.live_sessions) > 1:
        return _invariant_broken("live_sessions")
    if not facts.live_sessions:
        return TableRefusal.INACTIVE
    session = facts.live_sessions[0]
    if session.campaign_id != facts.campaign_id or not _open(session, now):
        return TableRefusal.INACTIVE
    if account_id == facts.owner_id:
        return _mint(facts, TableKind.OWNER_VIEWER, session, revisions, account_id)
    live = [seat for seat in facts.seats if seat.accepted and not seat.removed]
    if len(live) > 1:
        return _invariant_broken("live_seats")
    if not live:
        return TableRefusal.INACTIVE
    kind = TableKind.SEATED_CONFIRMED if live[0].confirmed else TableKind.SEATED_AWAITING
    return _mint(facts, kind, session, revisions, account_id, participant_id=live[0].participant_id)


def _mint(
    facts: TableFacts,
    kind: TableKind,
    session: SessionFacts,
    revisions: tuple[int, int],
    account_id: int | None,
    *,
    grant_id: str | None = None,
    participant_id: str | None = None,
) -> TablePrincipal:
    # Only a GM-confirmed seat has a scope, sheets and groups; an awaiting one reads the table (I-9, SEC-50(5)).
    own = participant_id if kind is TableKind.SEATED_CONFIRMED else None
    with _minting():
        return TablePrincipal(
            kind=kind,
            campaign_id=facts.campaign_id,
            session_id=session.id,
            generation=session.generation,
            account_id=account_id,
            grant_id=grant_id,
            participant_id=participant_id,
            scope=own,
            characters=facts.sheets.get(own, frozenset()) if own is not None else frozenset(),
            groups=facts.groups.get(own, frozenset()) if own is not None else frozenset(),
            authz_revision=revisions[0],
            projection_revision=revisions[1],
        )


def decide_gm(facts: GmFacts | None, *, campaign_id: str, account_id: int) -> GmPrincipal:
    """The owner, or the one `MissingParent` (SEC-2, SEC-3): a missing, foreign
    or unreadable campaign are one answer. The loader's query holds the owner
    predicate too; this compare is the second, independent check."""
    if facts is None or facts.owner_id != account_id:
        raise MissingParent("no campaign of that owner's")
    with _minting():
        return GmPrincipal(
            campaign_id=campaign_id,
            account_id=account_id,
            archived=facts.archived,
            authz_revision=facts.authz_revision,
            projection_revision=facts.projection_revision,
        )


def entitled(outcome: TableOutcome, slot: SlotId) -> Entitlement:
    """ADR section 4 `entitled` through 15.2: every principal reads the table
    slot; only a confirmed seat reads its own; any other slot is ABSENT, exactly
    what an empty table gives (SEC-41, 15.6). Never reads a class, a revision or
    a flag (ED-25)."""
    if outcome is TableRefusal.UNAUTHENTICATED:
        return Entitlement.UNAUTHENTICATED
    if not isinstance(outcome, TablePrincipal):
        return Entitlement.INACTIVE
    if slot is None:
        return Entitlement.ENTITLED
    if outcome.kind is TableKind.SEATED_CONFIRMED and slot == outcome.participant_id:
        return Entitlement.ENTITLED
    return Entitlement.ABSENT

"""Types of the independent policy oracle.

Written from the shared eligibility ADR (`docs/adr/shared-eligibility-display-disclosure.md`,
sections 2 to 5, read through section 15.2) and the Workbench threat model section 15, never from
production code. Standard library only.

No field text exists anywhere in the oracle (I-4): a field value is a `Value`, and every `str` held
in a model object is an id or a field key matching `ID_PATTERN` (ED-2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, NewType

if TYPE_CHECKING:
    from .machine import InFlight


#: ED-2: the flat field key. Every id in the oracle follows the same grammar (I-20).
ID_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")

FieldKey = NewType("FieldKey", str)
ParticipantId = NewType("ParticipantId", str)
DocumentId = NewType("DocumentId", str)
GroupId = NewType("GroupId", str)
SessionId = NewType("SessionId", str)
AccountId = NewType("AccountId", str)
GrantId = NewType("GrantId", str)
CommandId = NewType("CommandId", str)
DisclosureId = NewType("DisclosureId", str)
OpId = NewType("OpId", int)


class OracleUsageError(Exception):
    """A programming error: an unavailable op, a step out of order, a violated precondition.

    Never an answer. The refusal surfaces of other beads (seat offers, screen minting, links) are
    preconditions here, so a generator that emits one has a bug (alignment brief section 5.5).
    """


def check_id(value: str) -> str:
    """Return `value` if it matches the id/key grammar (ED-2, I-20), else raise."""
    if ID_PATTERN.fullmatch(value) is None:
        raise OracleUsageError("an id or key does not match the grammar")
    return value


def fkey(value: str) -> FieldKey:
    """A validated field key (ED-2)."""
    return FieldKey(check_id(value))


def fkeys(*values: str) -> frozenset[FieldKey]:
    """A frozenset of validated field keys."""
    return frozenset(fkey(v) for v in values)


def frozen_map[K, V](items: Mapping[K, V]) -> Mapping[K, V]:
    """A read-only mapping over a dict built fresh, so nothing held in state is mutated in place."""
    return MappingProxyType(dict(items))


class Release(Enum):
    """The two releases a column of the truth table describes (I-6)."""

    WORKBENCH_V1 = "workbench_v1"
    ASSISTANT = "assistant"


class Value(Enum):
    """A field value without its text (I-4). `PRESENT` is ED-9(2)'s "present and non-empty"."""

    PRESENT = "present"
    BLANK = "blank"
    ABSENT = "absent"


class ClassKind(Enum):
    """ED-4's seven classes."""

    UNCLASSIFIED = "unclassified"
    GM_ONLY = "gm_only"
    PARTICIPANTS = "participants"
    CHARACTERS = "characters"
    GROUPS = "groups"
    CAMPAIGN = "campaign"
    PUBLIC = "public"


LIST_KINDS: Final = frozenset({ClassKind.PARTICIPANTS, ClassKind.CHARACTERS, ClassKind.GROUPS})


@dataclass(frozen=True)
class FieldClass:
    """An eligibility class (ED-4). `ids` is non-empty for the three list kinds and empty otherwise."""

    kind: ClassKind
    ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.kind in LIST_KINDS:
            if not self.ids:
                raise OracleUsageError("a list class needs at least one id")
            for i in self.ids:
                check_id(i)
        elif self.ids:
            raise OracleUsageError("only a list class carries ids")

    @classmethod
    def unclassified(cls) -> FieldClass:
        return cls(ClassKind.UNCLASSIFIED)

    @classmethod
    def gm_only(cls) -> FieldClass:
        return cls(ClassKind.GM_ONLY)

    @classmethod
    def participants(cls, *ids: str) -> FieldClass:
        return cls(ClassKind.PARTICIPANTS, frozenset(ids))

    @classmethod
    def characters(cls, *ids: str) -> FieldClass:
        return cls(ClassKind.CHARACTERS, frozenset(ids))

    @classmethod
    def groups(cls, *ids: str) -> FieldClass:
        return cls(ClassKind.GROUPS, frozenset(ids))

    @classmethod
    def campaign(cls) -> FieldClass:
        return cls(ClassKind.CAMPAIGN)

    @classmethod
    def public(cls) -> FieldClass:
        return cls(ClassKind.PUBLIC)


class SeatStatus(Enum):
    """The wire's seat vocabulary (F-9, I-23)."""

    OPEN = "open"
    OFFERED = "offered"
    NOT_ACCEPTED = "not_accepted"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    REMOVED = "removed"


#: I-23: the statuses in which a seat holds an account and is live.
LIVE_SEATS: Final = frozenset({SeatStatus.AWAITING_CONFIRMATION, SeatStatus.CONFIRMED})


@dataclass(frozen=True)
class Participant:
    """A seat (threat model section 15.2, SEC-50). An offer binds to an account only at acceptance.

    `account` is set iff the seat was accepted (awaiting confirmation, confirmed, or removed after
    acceptance); `offered_to` is set only while offered and is never read by `entitled`.
    """

    id: ParticipantId
    status: SeatStatus
    account: AccountId | None
    offered_to: AccountId | None = None

    def __post_init__(self) -> None:
        check_id(self.id)
        if self.account is not None:
            check_id(self.account)
        if self.offered_to is not None:
            check_id(self.offered_to)
        if self.status is SeatStatus.OFFERED:
            if self.offered_to is None or self.account is not None:
                raise OracleUsageError("an offered seat names its invitee and holds no account")
        elif self.offered_to is not None:
            raise OracleUsageError("only an offered seat names an invitee")
        if self.status in LIVE_SEATS and self.account is None:
            raise OracleUsageError("an accepted seat holds its account")
        if self.status in (SeatStatus.OPEN, SeatStatus.NOT_ACCEPTED) and self.account is not None:
            raise OracleUsageError("a seat that was never accepted holds no account")

    @property
    def active(self) -> bool:
        """I-23: active (for eligibility) means not removed."""
        return self.status is not SeatStatus.REMOVED


@dataclass(frozen=True)
class TypeVersion:
    """One version of a fixture type (ED-5, ED-24, REVEAL-4)."""

    version: int
    declared: frozenset[FieldKey]
    revealable: frozenset[FieldKey]
    reserved: frozenset[FieldKey]
    default_reveal: tuple[FieldKey, ...]

    def __post_init__(self) -> None:
        for k in self.declared | self.reserved:
            check_id(k)
        if not self.revealable <= self.declared:
            raise OracleUsageError("revealable keys must be declared")
        if self.reserved & self.declared:
            raise OracleUsageError("a reserved key cannot be declared")
        if not set(self.default_reveal) <= self.revealable:
            raise OracleUsageError("a default reveal key must be revealable")


@dataclass(frozen=True)
class FixtureType:
    """A fixture document type the oracle registers for its own use (ADR section 5, F-2)."""

    id: str
    audience: Literal["table", "owner"]
    versions: tuple[TypeVersion, ...]

    def __post_init__(self) -> None:
        check_id(self.id)
        if [v.version for v in self.versions] != list(range(1, len(self.versions) + 1)):
            raise OracleUsageError("type versions are numbered from 1")

    def version(self, number: int) -> TypeVersion:
        if not 1 <= number <= len(self.versions):
            raise OracleUsageError("unknown type version")
        return self.versions[number - 1]


@dataclass(frozen=True)
class Version:
    """A document version: its number, whether it is sealed, and a text-free value per key."""

    number: int
    sealed: bool
    values: Mapping[FieldKey, Value]

    def __post_init__(self) -> None:
        for k in self.values:
            check_id(k)


@dataclass(frozen=True)
class Document:
    """A document (ED-6: eligibility is per document and key, never per version)."""

    id: DocumentId
    type_id: str
    type_version: int
    versions: tuple[Version, ...]
    archived: bool
    linked_participant: ParticipantId | None

    def __post_init__(self) -> None:
        check_id(self.id)
        if self.id == "all":
            raise OracleUsageError("'all' is the Stop-everything target, never a document id")
        numbers = [v.number for v in self.versions]
        if numbers != list(range(1, len(numbers) + 1)) or not numbers:
            raise OracleUsageError("document versions are numbered from 1")
        if any(not v.sealed for v in self.versions[:-1]):
            raise OracleUsageError("only the latest version may be unsealed")

    def version(self, number: int) -> Version | None:
        if 1 <= number <= len(self.versions):
            return self.versions[number - 1]
        return None

    @property
    def latest(self) -> Version:
        return self.versions[-1]


@dataclass(frozen=True)
class World:
    """The facts a policy reads: seats, documents, links, groups, classes and the switches.

    `__post_init__` refuses what no reachable state holds (brief section 5.2).
    """

    release: Release
    owner: AccountId
    participants: Mapping[ParticipantId, Participant]
    documents: Mapping[DocumentId, Document]
    groups: Mapping[GroupId, frozenset[ParticipantId]]
    classes: Mapping[tuple[DocumentId, FieldKey], FieldClass]
    enforced: bool
    automation_enabled: bool
    types: Mapping[str, FixtureType]
    authz_revision: int

    def __post_init__(self) -> None:
        check_id(self.owner)
        if self.release is Release.WORKBENCH_V1 and (
            self.classes or self.groups or self.enforced or self.automation_enabled
        ):
            raise OracleUsageError("Workbench v1 has no classes, groups or switches (ED-11, M-1)")
        if self.automation_enabled and not self.enforced:
            raise OracleUsageError("display automation requires enforcement (M-2)")
        holders: dict[str, int] = {}
        for pid, p in self.participants.items():
            if pid != p.id:
                raise OracleUsageError("a participant is keyed by its id")
            holder = p.account if p.status in LIVE_SEATS else p.offered_to
            if holder is None:
                continue
            if holder == self.owner:
                raise OracleUsageError("the owner holds no seat (threat model section 15.2)")
            holders[holder] = holders.get(holder, 0) + 1
        if any(n > 1 for n in holders.values()):
            raise OracleUsageError("an account holds at most one live seat or offer per campaign")
        linked: dict[str, int] = {}
        for did, d in self.documents.items():
            if did != d.id:
                raise OracleUsageError("a document is keyed by its id")
            ftype = self.types.get(d.type_id)
            if ftype is None:
                raise OracleUsageError("unknown fixture type")
            tv = ftype.version(d.type_version)
            for v in d.versions:
                if not set(v.values) <= tv.declared:
                    raise OracleUsageError("a version holds only declared keys")
            if d.linked_participant is not None:
                if ftype.audience != "owner":
                    raise OracleUsageError("only an owner-audience document is linked")
                if d.linked_participant not in self.participants:
                    raise OracleUsageError("a link names a known participant")
                linked[d.linked_participant] = linked.get(d.linked_participant, 0) + 1
        if any(n > 1 for n in linked.values()):
            raise OracleUsageError("a participant has at most one linked sheet")
        for gid, members in self.groups.items():
            check_id(gid)
            if not members <= set(self.participants):
                raise OracleUsageError("a group's members are known participants")
        for doc_id, key in self.classes:
            check_id(key)
            if doc_id not in self.documents:
                raise OracleUsageError("a class row names an existing document")

    def type_version(self, document: Document) -> TypeVersion:
        return self.types[document.type_id].version(document.type_version)

    def revealable(self, document_id: DocumentId) -> frozenset[FieldKey]:
        """The revealable allowlist of the document's current type version (ED-5, ED-24)."""
        doc = self.documents.get(document_id)
        if doc is None:
            return frozenset()
        return self.type_version(doc).revealable


class SlotKind(Enum):
    TABLE = "table"
    PARTICIPANT = "participant"


@dataclass(frozen=True)
class Slot:
    """An audience slot of the live table session (ED-10, ED-15)."""

    kind: SlotKind
    participant: ParticipantId | None = None

    def __post_init__(self) -> None:
        if self.kind is SlotKind.TABLE:
            if self.participant is not None:
                raise OracleUsageError("the table slot names no participant")
        elif self.participant is None:
            raise OracleUsageError("a participant slot names its participant")
        else:
            check_id(self.participant)

    def sort_key(self) -> tuple[int, str]:
        """I-19: the table first, then participant ids ascending."""
        return (0, "") if self.participant is None else (1, self.participant)


TABLE: Final[Slot] = Slot(SlotKind.TABLE)


def participant_slot(participant: str) -> Slot:
    """The slot of `participant`."""
    return Slot(SlotKind.PARTICIPANT, ParticipantId(participant))


def slot_order(slots: frozenset[Slot] | set[Slot] | Mapping[Slot, object]) -> list[Slot]:
    """Slots in canonical order (I-19)."""
    return sorted(slots, key=Slot.sort_key)


@dataclass(frozen=True)
class TableAudienceId:
    """The table as an identity audience (ED-10)."""


@dataclass(frozen=True)
class ParticipantAudienceId:
    """`participant:<id>` as an identity audience (ED-10)."""

    participant: ParticipantId


EligAudience = TableAudienceId | ParticipantAudienceId


@dataclass(frozen=True)
class Table:
    """A reveal to the table slot (ED-15)."""


@dataclass(frozen=True)
class Participants:
    """A reveal to one or more participant slots (ED-15, O-3)."""

    ids: frozenset[ParticipantId]

    def __post_init__(self) -> None:
        if not self.ids:
            raise OracleUsageError("a participant audience names at least one participant")
        for i in self.ids:
            check_id(i)


@dataclass(frozen=True)
class Group:
    """A reveal to a named group, expanded to its members at Confirm (ED-8, ED-15). ASSISTANT only."""

    id: GroupId


@dataclass(frozen=True)
class EveryoneSeated:
    """A reveal to every active CONFIRMED seat (TP-1, I-7)."""


RevealAudience = Table | Participants | Group | EveryoneSeated


@dataclass(frozen=True)
class Copy:
    """One copy of a disclosure in one slot: a document, a pinned version and an explicit mask."""

    disclosure: DisclosureId
    document: DocumentId
    version: int
    mask: frozenset[FieldKey]


@dataclass(frozen=True)
class Disclosure:
    """One Confirm's worth of display (ED-15). `slots` are the slots that currently hold its copies."""

    id: DisclosureId
    document: DocumentId
    slots: frozenset[Slot]
    group: GroupId | None


@dataclass(frozen=True)
class Session:
    """A table session. *Open* means live and not expired; *state-live* means live (critic item 2)."""

    id: SessionId
    live: bool
    expired: bool
    epoch: int
    generation: int

    @property
    def open(self) -> bool:
        return self.live and not self.expired


@dataclass(frozen=True)
class ScreenGrant:
    """A view-only table grant of a shared screen (SEC-48, D-13)."""

    id: GrantId
    session: SessionId
    generation: int
    revoked: bool


class ContentKind(Enum):
    """What a slot can hold. `DOCUMENT` only (ADR 7.4, O-5, TT-39)."""

    DOCUMENT = "document"


class EventKind(Enum):
    """ED-17's ledger events, exactly."""

    DISPLAYED = "displayed"
    UPDATED = "updated"
    STOPPED = "stopped"
    SESSION_ENDED = "session_ended"
    RETRACTED = "retracted"
    EXPORTED = "exported"


class StopReason(Enum):
    """ED-17's `stopped` reasons, exactly, in its order.

    `PERSONAL_LINK_RESET` (superseded, ADR section 15.2) and `CAMPAIGN_DELETED` (I-22) are never produced.
    """

    GM_STOP = "gm_stop"
    STOP_ALL = "stop_all"
    MASK_NARROWED = "mask_narrowed"
    REPLACED = "replaced"
    MOVED = "moved"
    LINK_ROTATED = "link_rotated"
    PARTICIPANT_REMOVED = "participant_removed"
    PERSONAL_LINK_RESET = "personal_link_reset"
    CHARACTER_UNLINKED = "character_unlinked"
    GROUP_MEMBER_REMOVED = "group_member_removed"
    ELIGIBILITY_TIGHTENED = "eligibility_tightened"
    ENFORCEMENT_ENABLED = "enforcement_enabled"
    DOCUMENT_ARCHIVED = "document_archived"
    DOCUMENT_DELETED = "document_deleted"
    CAMPAIGN_DELETED = "campaign_deleted"


NEVER_PRODUCED: Final = frozenset({StopReason.PERSONAL_LINK_RESET, StopReason.CAMPAIGN_DELETED})


class SessionEndReason(Enum):
    """ED-17: `session_ended` carries `gm_end` or `expired`."""

    GM_END = "gm_end"
    EXPIRED = "expired"


class Actor(Enum):
    """Who wrote an event. `RULE` is reserved for Phase 5 and never produced."""

    GM = "gm"
    SYSTEM = "system"
    RULE = "rule"


@dataclass(frozen=True)
class FieldEvent:
    """One ledger row per field (ED-17): references and codes, never text (ED-26)."""

    seq: int
    kind: EventKind
    reason: StopReason | SessionEndReason | None
    document: DocumentId
    key: FieldKey
    version: int
    slot: Slot | None
    session: SessionId | None
    disclosure: DisclosureId | None
    actor: Actor
    ledgered: bool


@dataclass(frozen=True)
class Requester:
    """What the server resolves from a request's cookies: an account session and a screen grant."""

    account: AccountId | None = None
    screen_grant: GrantId | None = None


class PrincipalKind(Enum):
    """Threat model section 15.2's `table_principal` answers."""

    OWNER = "owner"
    SEATED = "seated"
    SEATED_CONFIRMED = "seated_confirmed"
    SCREEN = "screen"
    INACTIVE = "inactive"
    UNAUTHENTICATED = "unauthenticated"


class Entitlement(Enum):
    """`entitled`'s answers. `ABSENT` is exactly what an empty slot gives (SEC-41, SEC-46)."""

    ENTITLED = "entitled"
    ABSENT = "absent"
    INACTIVE = "inactive"
    UNAUTHENTICATED = "unauthenticated"


class EligReason(Enum):
    """Why `eligible_for_audience` answered as it did (ADR section 4, in its order)."""

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
class Eligibility:
    allowed: bool
    reason: EligReason


@dataclass(frozen=True)
class SlotView:
    """What a requester reads of one slot: never text, and the slot's sequence (REVEAL-24)."""

    document: DocumentId | None
    version: int
    mask: tuple[FieldKey, ...]
    seq: int


@dataclass(frozen=True)
class VisibleView:
    """`visible()`'s answer: a refusal, or the `"table"` entry and, for a confirmed seat, `"mine"`."""

    refusal: Entitlement | None
    entries: Mapping[str, SlotView]

    def __getitem__(self, name: str) -> SlotView:
        return self.entries[name]

    def __contains__(self, name: object) -> bool:
        return name in self.entries


class RefusalKind(Enum):
    """The oracle's own closed refusal vocabulary (I-18). Wire codes are the consumers' to map."""

    NOT_FOUND = "not_found"
    SESSION_NOT_LIVE = "session_not_live"
    EPOCH_STALE = "epoch_stale"
    CLASS_MOVED = "class_moved"
    AUTOMATION_ENABLED = "automation_enabled"
    ENFORCEMENT_REQUIRED = "enforcement_required"
    DOCUMENT_NOT_ARCHIVED = "document_not_archived"
    LINK_TAKEN = "link_taken"
    DOCUMENT_ARCHIVED = "document_archived"
    VERSION_INVALID = "version_invalid"
    MASK_INVALID = "mask_invalid"
    AUDIENCE_INVALID = "audience_invalid"
    NOT_ELIGIBLE = "not_eligible"
    CLASSIFY_INVALID = "classify_invalid"

    @property
    def status(self) -> int:
        return REFUSAL_STATUS[self]


REFUSAL_STATUS: Final[Mapping[RefusalKind, int]] = MappingProxyType(
    {
        RefusalKind.NOT_FOUND: 404,
        RefusalKind.SESSION_NOT_LIVE: 409,
        RefusalKind.EPOCH_STALE: 409,
        RefusalKind.CLASS_MOVED: 409,
        RefusalKind.AUTOMATION_ENABLED: 409,
        RefusalKind.ENFORCEMENT_REQUIRED: 409,
        RefusalKind.DOCUMENT_NOT_ARCHIVED: 409,
        RefusalKind.LINK_TAKEN: 409,
        RefusalKind.DOCUMENT_ARCHIVED: 422,
        RefusalKind.VERSION_INVALID: 422,
        RefusalKind.MASK_INVALID: 422,
        RefusalKind.AUDIENCE_INVALID: 422,
        RefusalKind.NOT_ELIGIBLE: 422,
        RefusalKind.CLASSIFY_INVALID: 422,
    }
)


@dataclass(frozen=True)
class Ok:
    """The step applied (or was an idempotent no-op)."""


@dataclass(frozen=True)
class Refused:
    """A refusal: its kind and, for a 422, the field keys at fault (never text)."""

    kind: RefusalKind
    keys: tuple[FieldKey, ...] = ()


@dataclass(frozen=True)
class NotAppliedYet:
    """RQ-5, RC-15: a fact-changing narrowing's step 2 could not get the lock. Retryable 503."""


@dataclass(frozen=True)
class RetryLater:
    """A locked widening, Confirm or Export that could not get the lock. Retryable 503."""


@dataclass(frozen=True)
class JobRetry:
    """A reconciliation job that could not get the lock; the job runner retries it."""


@dataclass(frozen=True)
class Replayed:
    """A command id seen before: the first answer, and no second effect (wire *Idempotency*)."""

    first: Answer


Answer = Ok | Refused | NotAppliedYet | RetryLater | JobRetry | Replayed


@dataclass(frozen=True)
class State:
    """Everything a step reads or writes. Immutable; `apply` builds a new one."""

    world: World
    sessions: Mapping[SessionId, Session]
    live: SessionId | None
    slots: Mapping[Slot, Copy]
    slot_seq: Mapping[Slot, int]
    disclosures: Mapping[DisclosureId, Disclosure]
    grants: Mapping[GrantId, ScreenGrant]
    history: tuple[FieldEvent, ...]
    commands: Mapping[tuple[str, CommandId], Answer]
    in_flight: Mapping[OpId, InFlight]
    next_disclosure: int = 0

    @property
    def live_session(self) -> Session | None:
        """The state-live session, expired or not."""
        return None if self.live is None else self.sessions[self.live]

    @property
    def open_session(self) -> Session | None:
        """The live, unexpired session."""
        session = self.live_session
        return session if session is not None and session.open else None


__all__ = [
    "ID_PATTERN",
    "LIST_KINDS",
    "LIVE_SEATS",
    "NEVER_PRODUCED",
    "REFUSAL_STATUS",
    "TABLE",
    "AccountId",
    "Actor",
    "Answer",
    "ClassKind",
    "CommandId",
    "ContentKind",
    "Copy",
    "Disclosure",
    "DisclosureId",
    "Document",
    "DocumentId",
    "EligAudience",
    "EligReason",
    "Eligibility",
    "Entitlement",
    "EventKind",
    "EveryoneSeated",
    "FieldClass",
    "FieldEvent",
    "FieldKey",
    "FixtureType",
    "GrantId",
    "Group",
    "GroupId",
    "JobRetry",
    "NotAppliedYet",
    "Ok",
    "OpId",
    "OracleUsageError",
    "Participant",
    "ParticipantAudienceId",
    "ParticipantId",
    "Participants",
    "PrincipalKind",
    "RefusalKind",
    "Refused",
    "Release",
    "Replayed",
    "Requester",
    "RetryLater",
    "RevealAudience",
    "ScreenGrant",
    "SeatStatus",
    "Session",
    "SessionEndReason",
    "SessionId",
    "Slot",
    "SlotKind",
    "SlotView",
    "State",
    "StopReason",
    "Table",
    "TableAudienceId",
    "TypeVersion",
    "Value",
    "Version",
    "VisibleView",
    "World",
    "check_id",
    "fkey",
    "fkeys",
    "frozen_map",
    "participant_slot",
    "slot_order",
]

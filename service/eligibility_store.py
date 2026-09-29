"""Per-field eligibility and named groups (agent-forge-harness-1ir.2.1).

The shared eligibility / display / disclosure ADR (ACCEPTED) decides **who may
ever be shown a field**; this module stores that decision and nothing else. It
is the store half: a `Protocol`, a PostgreSQL implementation and an in-memory
twin, the shape `service/campaign_store.py` established. Every mutation of it
is composed by `service/eligibility.py`, which holds the campaign lock and
advances the authorisation revision in the same transaction — and is the only
module that may call a write here (a tripwire test says so).

**Default deny, and one representation of "not decided".** A revealable field
without a row is `unclassified` (ED-4, M-5), and resetting a field deletes its
row: `unclassified` is never stored. A key off its type's revealable allowlist
has no row and is `gm_only` **by construction** (ED-5): it is never counted as
unclassified, and a row that names it — an orphan, left by a type that moved —
is ignored (ED-24). A document of a type or type version this build does not
know evaluates every key as `gm_only`, and classifying it is refused.

**Only the GM widens** (ED-7). A row's `source` is `gm` or `default`, and the
database refuses a `default` row wider than `gm_only`. Suggestions are not rows
of this table: every row here is evaluated, so a stored suggestion would be one
missed `WHERE` away from being in force (inferred decision I-3, a departure
from the ADR's `suggested` source that fails closed).

**Principal lists are ids, checked at write time.** A `participants`,
`characters` or `groups` class carries a list of `prt_`, `doc_` or `grp_` ids,
strictly ascending and never empty (ED-8: a list never means "everyone").
Every id must name a live principal of this campaign when the list is written
(`check_principals`, under the exclusive campaign lock). A list id is not a
foreign key, so the resolver (`1ir.2.2`) resolves every id joined by the
campaign and treats anything it cannot resolve as nobody.

**A character is a character-sheet document** (lead ruling R-6). There is no
characters table: "linked to a participant" is 0008's
`documents.linked_participant_id`, one fact read in one place.

**Groups** are a GM's private named sets of seats (O-3). A group's name is
private GM text (SEC-20) under the alias rules, unique among the campaign's
live groups by `alias_key`, hidden from `repr()` and named by no refusal. A
group is marked removed and never deleted, because a disclosure will remember
the group it came from; a membership row is deleted, because nothing
references it. A member is any seat that is not removed (I-18); `members` and
`groups_of` leave removed seats and removed groups out at the source.

**Every write requires the exclusive campaign lock** (`unit.
require_exclusive_campaign_lock`), except `rename_group`, which changes no fact
a display reads and holds the group row `FOR NO KEY UPDATE` instead. Reads
take no lock. Every statement names the campaign (SEC-2), and a missing or
foreign parent is the one `MissingParent` (SEC-3), raised from the statement's
own guard rather than from a caught foreign-key violation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Final, Protocol

import psycopg

from . import campaign_identity as ident
from .campaign_store import CampaignStoreError, MissingParent, Staging, fake, now_or, pg, shared_rows
from .db import (
    FIELD_KEY_PATTERN,
    RESERVED_FIELD_KEYS,
    InMemoryDatabase,
    InMemoryTransaction,
    QueuedProjection,
    UnitOfWork,
)
from .document_store import DocumentRecord, DocumentStore
from .participant_store import Participant, alias_key, check_alias
from .table_session_store import COMMAND_ID
from .workbench_contracts import DOC_TYPE_VERSION, DocumentTypeId, revealable_fields


class FieldClass(str, Enum):
    """The seven classes of the ADR (ED-4). `UNCLASSIFIED` is never stored."""

    UNCLASSIFIED = "unclassified"
    GM_ONLY = "gm_only"
    PARTICIPANTS = "participants"
    CHARACTERS = "characters"
    GROUPS = "groups"
    CAMPAIGN = "campaign"
    PUBLIC = "public"


class ClassificationSource(str, Enum):
    """Who set a stored class. Only `GM` may be wider than `gm_only` (ED-7)."""

    GM = "gm"
    DEFAULT = "default"


#: The classes that carry a principal list, and the prefix each list names.
LIST_CLASSES: Final = frozenset({FieldClass.PARTICIPANTS, FieldClass.CHARACTERS, FieldClass.GROUPS})
LIST_PREFIX: Final = {
    FieldClass.PARTICIPANTS: ident.PARTICIPANT,
    FieldClass.CHARACTERS: ident.DOCUMENT,
    FieldClass.GROUPS: ident.GROUP,
}
#: The most ids one list may name: the participant roster's bound, pinned to
#: `workbench_contracts.PRESENCE_MAX_PARTICIPANTS` and to 0017 by a test.
PRINCIPAL_IDS_MAX: Final = 100
#: The most live groups one campaign may hold (SEC-35).
GROUPS_PER_CAMPAIGN_MAX: Final = 50
#: The largest page `queued_projections` answers.
QUEUE_PAGE_MAX: Final = 500

_FIELD_KEY: Final = re.compile(FIELD_KEY_PATTERN)
#: The type a `characters` list may name (R-6: a character is a sheet).
CHARACTER_SHEET: Final = DocumentTypeId.CHARACTER_SHEET.value


@dataclass(frozen=True)
class Eligibility:
    """One field's class, and its principal list when the class has one.

    Built only in a shape the database would accept: a list class carries 1 to
    `PRINCIPAL_IDS_MAX` ids of its prefix, **sorted strictly ascending** (one
    canonical form, so "has the class moved" is plain equality, ED-13(3)), and
    every other class carries none. `source` is None for exactly two values:
    `UNCLASSIFIED`, which is the absence of a row, and the `gm_only` sentinel
    `GM_ONLY_BY_CONSTRUCTION`, which is never a row either. A non-GM source is
    never wider than `gm_only` (ED-7). Every refusal is a fixed sentence that
    names no id."""

    field_class: FieldClass
    ids: tuple[str, ...] = ()
    source: ClassificationSource | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.field_class, FieldClass):
            raise TypeError("an eligibility's field_class is a FieldClass")
        if not isinstance(self.ids, tuple) or not all(isinstance(value, str) for value in self.ids):
            raise TypeError("an eligibility's ids are a tuple of str")
        if self.source is not None and not isinstance(self.source, ClassificationSource):
            raise TypeError("an eligibility's source is a ClassificationSource or None")
        if self.field_class in LIST_CLASSES:
            _check_list(self.field_class, self.ids)
        elif self.ids:
            raise ValueError("only a participants, characters or groups class carries ids")
        if self.field_class is FieldClass.UNCLASSIFIED:
            if self.source is not None:
                raise ValueError("unclassified is the absence of a classification and has no source")
        elif self.source is None and self.field_class is not FieldClass.GM_ONLY:
            raise ValueError("a stored classification has a source")
        if self.source is ClassificationSource.DEFAULT and self.field_class is not FieldClass.GM_ONLY:
            raise ValueError("only a GM classification is wider than gm_only")

    @property
    def admits_anyone(self) -> bool:
        """Whether this class can let any principal see the field."""
        return self.field_class not in (FieldClass.UNCLASSIFIED, FieldClass.GM_ONLY)


def _check_list(field_class: FieldClass, ids: tuple[str, ...]) -> None:
    if not 1 <= len(ids) <= PRINCIPAL_IDS_MAX:
        raise ValueError(f"a principal list names 1 to {PRINCIPAL_IDS_MAX} ids")
    if any(later <= earlier for earlier, later in zip(ids, ids[1:], strict=False)):
        raise ValueError("a principal list is distinct and sorted ascending")
    prefix = LIST_PREFIX[field_class]
    if not all(ident.is_id(prefix, value) for value in ids):
        raise ValueError("a principal list names identifiers of its class's kind only")


def principals(field_class: FieldClass, ids: list[str] | tuple[str, ...]) -> Eligibility:
    """A GM's list classification, with its ids put into the canonical order."""
    return Eligibility(field_class, tuple(sorted(ids)), ClassificationSource.GM)


#: A revealable field with no row (ED-4, M-5). Never stored.
UNCLASSIFIED: Final = Eligibility(FieldClass.UNCLASSIFIED)
#: A key off the type's revealable allowlist, or of a type or type version this
#: build does not know (ED-5, ED-24, I-20). Not a row, and `put` refuses it.
GM_ONLY_BY_CONSTRUCTION: Final = Eligibility(FieldClass.GM_ONLY)


@dataclass(frozen=True)
class Group:
    """A GM's named group of seats. The name is private text (SEC-20) and is
    hidden from `repr()`, which is what a traceback prints."""

    id: str
    campaign_id: str
    name: str = field(repr=False)
    created_at: datetime
    updated_at: datetime
    removed_at: datetime | None = None

    @property
    def is_live(self) -> bool:
        return self.removed_at is None


class EligibilityStoreError(CampaignStoreError):
    """What the eligibility store refuses. Every message is fixed and names no
    id, key value, name or document text (SEC-20)."""


class NotClassifiable(EligibilityStoreError):
    """The key is not one this document's type lets a GM classify: off the
    revealable allowlist, the reserved word, not a field key at all, or a
    document of a type or type version this build does not know."""

    MESSAGE = "that field cannot be classified"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class InvalidPrincipal(EligibilityStoreError):
    """An id in a principal list names no live principal of this campaign of the
    list's kind: missing, foreign, removed, or a document that is not a
    character sheet. One answer for every reason (SEC-3)."""

    MESSAGE = "a principal list names something this campaign cannot show"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class GroupNameTaken(EligibilityStoreError):
    """Another live group of this campaign already answers to that name."""

    MESSAGE = "that campaign already has a group with that name"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class GroupLimit(EligibilityStoreError):
    """The campaign already holds `GROUPS_PER_CAMPAIGN_MAX` live groups."""

    MESSAGE = f"a campaign holds at most {GROUPS_PER_CAMPAIGN_MAX} groups"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


_GROUP_NAME = "a group name is 1 to 40 visible characters with no control or formatting characters"


def check_group_name(name: str) -> str:
    """A group name under the alias rules (`participant_store.check_alias`: one
    folding rule, not two), refused with a fixed group sentence raised outside
    the handler, so nothing about the name is chained."""
    checked: str | None = None
    try:
        checked = check_alias(name)
    except ValueError:
        checked = None
    if checked is None:
        raise ValueError(_GROUP_NAME)
    return checked


def check_types(**given: object) -> None:
    """Refuse a value of the wrong TYPE before any statement, in both worlds
    (`participant_store.check_argument_types`'s rule). `document` is a
    `DocumentRecord`, `eligibility` an `Eligibility`, `command_id` a str or
    None, `after_id` and `limit` an int; everything else is a str. The message
    names the parameter, never the value."""
    for name, value in given.items():
        if name == "document":
            ok = isinstance(value, DocumentRecord)
        elif name == "eligibility":
            ok = isinstance(value, Eligibility)
        elif name == "command_id":
            ok = value is None or isinstance(value, str)
        elif name in ("after_id", "limit"):
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, str)
        if not ok:
            raise TypeError(f"an eligibility store's {name} has the wrong type")


def _check_command_id(command_id: str | None) -> None:
    if command_id is not None and COMMAND_ID.fullmatch(command_id) is None:
        raise ValueError("a command id is 16 to 64 URL-safe characters")


def _check_page(after_id: int, limit: int) -> None:
    if after_id < 0 or not 1 <= limit <= QUEUE_PAGE_MAX:
        raise ValueError(f"a queue page starts after an id of 0 or more and holds 1 to {QUEUE_PAGE_MAX} items")


def is_field_key(field_key: str) -> bool:
    """A flat field key, never the reserved word (ED-2, ED-8)."""
    return _FIELD_KEY.fullmatch(field_key) is not None and field_key not in RESERVED_FIELD_KEYS


def classifiable_keys(document: DocumentRecord) -> frozenset[str]:
    """The keys of this document a GM may classify: its type's revealable
    allowlist (ED-5) — or nothing, for a type or type version this build does
    not know (I-20)."""
    try:
        doc_type = DocumentTypeId(document.type)
    except ValueError:
        return frozenset()
    if document.type_version != DOC_TYPE_VERSION[doc_type]:
        return frozenset()
    return frozenset(revealable_fields(doc_type))


def _row_eligibility(eligibility_class: str, ids: list[str] | None, source: str) -> Eligibility:
    return Eligibility(FieldClass(eligibility_class), tuple(ids or ()), ClassificationSource(source))


def _storable(document: DocumentRecord, field_key: str, eligibility: Eligibility) -> None:
    """`put`'s checks before any statement: the key is classifiable for this
    document, and the value is something a row may hold."""
    if not is_field_key(field_key) or field_key not in classifiable_keys(document):
        raise NotClassifiable()
    if eligibility.source is None and eligibility.field_class is not FieldClass.UNCLASSIFIED:
        raise ValueError("gm_only by construction is never stored")


class EligibilityStore(Protocol):
    """Field eligibility and groups. Every mutator takes the unit of work first,
    so `service/eligibility.py` composes it with the revision in one
    transaction. Reads take no lock; every write but `rename_group` requires
    the exclusive campaign lock."""

    def eligibility_of(self, unit: UnitOfWork, document: DocumentRecord, field_key: str) -> Eligibility:
        """The field's class, in ED-24's order: `GM_ONLY_BY_CONSTRUCTION` for an
        unknown type or type version, then for a key off the allowlist —
        **before** any row is read, so an orphan row is ignored — then the row,
        else `UNCLASSIFIED`. `document` comes from `DocumentStore.get`, which is
        campaign-scoped (SEC-2)."""
        ...  # pragma: no cover - structural type

    def eligibilities_of(self, unit: UnitOfWork, document: DocumentRecord) -> dict[str, Eligibility]:
        """Exactly the revealable keys, each evaluated as `eligibility_of` does.
        Keys off the allowlist are absent, not unclassified (ED-5)."""
        ...  # pragma: no cover - structural type

    def get_group(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> Group | None:
        """That campaign's group, removed or not, or None."""
        ...  # pragma: no cover - structural type

    def list_groups(self, unit: UnitOfWork, campaign_id: str) -> list[Group]:
        """The campaign's live groups (at most `GROUPS_PER_CAMPAIGN_MAX`), by
        name key then id, in code-point order."""
        ...  # pragma: no cover - structural type

    def members(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> frozenset[str]:
        """The group's seats that are not removed; empty for a removed group."""
        ...  # pragma: no cover - structural type

    def groups_of(self, unit: UnitOfWork, campaign_id: str, participant_id: str) -> frozenset[str]:
        """The live groups a seat that is not removed belongs to."""
        ...  # pragma: no cover - structural type

    def queued_projections(
        self, unit: UnitOfWork, campaign_id: str, *, after_id: int = 0, limit: int
    ) -> list[QueuedProjection]:
        """The campaign's queue after `after_id`, ascending, at most `limit`."""
        ...  # pragma: no cover - structural type

    def group_for_command(self, unit: UnitOfWork, campaign_id: str, command_id: str) -> Group | None:
        """The group this campaign's `command_id` made, removed or not — what a
        replayed create answers, and how the service knows nothing changed."""
        ...  # pragma: no cover - structural type

    def check_principals(self, unit: UnitOfWork, campaign_id: str, eligibility: Eligibility) -> None:
        """`InvalidPrincipal` unless every id of the list names a live principal
        of this campaign of the list's kind. A no-op for a class with no list."""
        ...  # pragma: no cover - structural type

    def put(
        self,
        unit: UnitOfWork,
        document: DocumentRecord,
        field_key: str,
        eligibility: Eligibility,
        *,
        now: datetime | None = None,
    ) -> None:
        """Store the field's class; `UNCLASSIFIED` deletes its row. Checks the
        key before any statement (`NotClassifiable`). Checks no principal and
        advances nothing: both are the service's."""
        ...  # pragma: no cover - structural type

    def create_group(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        name: str,
        command_id: str | None = None,
        now: datetime | None = None,
    ) -> Group:
        """A new live group, or — for a `command_id` this campaign has seen —
        the group that command made, removed or not, whatever name is sent."""
        ...  # pragma: no cover - structural type

    def hold_group(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> Group | None:
        """Hold the group row `FOR NO KEY UPDATE` and hand it back."""
        ...  # pragma: no cover - structural type

    def rename_group(
        self, unit: UnitOfWork, campaign_id: str, group_id: str, *, name: str, now: datetime | None = None
    ) -> Group:
        """Rename a live group; `MissingParent` for a missing, foreign or removed
        one. Takes no campaign lock."""
        ...  # pragma: no cover - structural type

    def remove_group(
        self, unit: UnitOfWork, campaign_id: str, group_id: str, *, now: datetime | None = None
    ) -> bool:
        """Mark the group removed; False if it already was."""
        ...  # pragma: no cover - structural type

    def add_member(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        group_id: str,
        participant_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """True when the seat was added, False when it already was a member;
        `MissingParent` for a missing, foreign or removed group or seat."""
        ...  # pragma: no cover - structural type

    def remove_member(self, unit: UnitOfWork, campaign_id: str, group_id: str, participant_id: str) -> bool:
        """True when a membership row was deleted — a removed seat's included —
        False when the seat was not a member; `MissingParent` for a missing,
        foreign or removed group, or a missing or foreign seat."""
        ...  # pragma: no cover - structural type


# ── PostgreSQL ───────────────────────────────────────────────────────────────

_G_COLUMNS = "id, campaign_id, name, created_at, updated_at, removed_at"
_Q_COLUMNS = "id, campaign_id, document_id, field_key, authz_revision, created_at"
_LIVE_GROUP = (
    "EXISTS (SELECT 1 FROM campaign.groups WHERE id = %s AND campaign_id = %s AND removed_at IS NULL)"
)
#: One count per list kind: every id must be a live principal of the campaign.
_PRINCIPAL_COUNT: Final = {
    FieldClass.PARTICIPANTS: (
        "SELECT count(*) FROM campaign.participants "
        "WHERE campaign_id = %s AND removed_at IS NULL AND id = ANY(%s)"
    ),
    FieldClass.CHARACTERS: (
        "SELECT count(*) FROM campaign.documents "
        f"WHERE campaign_id = %s AND type = '{CHARACTER_SHEET}' AND id = ANY(%s)"
    ),
    FieldClass.GROUPS: (
        "SELECT count(*) FROM campaign.groups WHERE campaign_id = %s AND removed_at IS NULL AND id = ANY(%s)"
    ),
}


def _group(row: tuple) -> Group:
    return Group(row[0], row[1], row[2], row[3], row[4], row[5])


def _queued(row: tuple) -> QueuedProjection:
    return QueuedProjection(int(row[0]), row[1], row[2], row[3], int(row[4]), row[5])


class PostgresEligibilityStore:
    """`campaign.field_eligibility`, `campaign.groups` and
    `campaign.group_members` (migration 0017), and reads of
    `campaign.projection_queue`, which only `advance_authz_revision` writes."""

    # ── Reads ────────────────────────────────────────────────────────────────

    def eligibility_of(self, unit: UnitOfWork, document: DocumentRecord, field_key: str) -> Eligibility:
        check_types(document=document, field_key=field_key)
        if field_key not in classifiable_keys(document):
            return GM_ONLY_BY_CONSTRUCTION
        row = pg(unit).conn.execute(
            "SELECT eligibility_class, principal_ids, classification_source FROM campaign.field_eligibility "
            "WHERE document_id = %s AND field_key = %s AND campaign_id = %s",
            (document.id, field_key, document.campaign_id),
        ).fetchone()
        return UNCLASSIFIED if row is None else _row_eligibility(*row)

    def eligibilities_of(self, unit: UnitOfWork, document: DocumentRecord) -> dict[str, Eligibility]:
        check_types(document=document)
        keys = classifiable_keys(document)
        if not keys:
            return {}
        rows = pg(unit).conn.execute(
            "SELECT field_key, eligibility_class, principal_ids, classification_source "
            "FROM campaign.field_eligibility WHERE document_id = %s AND campaign_id = %s",
            (document.id, document.campaign_id),
        ).fetchall()
        stored = {row[0]: _row_eligibility(row[1], row[2], row[3]) for row in rows if row[0] in keys}
        return {key: stored.get(key, UNCLASSIFIED) for key in sorted(keys)}

    def get_group(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> Group | None:
        check_types(campaign_id=campaign_id, group_id=group_id)
        row = pg(unit).conn.execute(
            f"SELECT {_G_COLUMNS} FROM campaign.groups WHERE id = %s AND campaign_id = %s",
            (group_id, campaign_id),
        ).fetchone()
        return None if row is None else _group(row)

    def list_groups(self, unit: UnitOfWork, campaign_id: str) -> list[Group]:
        check_types(campaign_id=campaign_id)
        rows = pg(unit).conn.execute(
            f'SELECT {_G_COLUMNS} FROM campaign.groups WHERE campaign_id = %s AND removed_at IS NULL '
            f'ORDER BY name_key COLLATE "C", id COLLATE "C" LIMIT %s',
            (campaign_id, GROUPS_PER_CAMPAIGN_MAX),
        ).fetchall()
        return [_group(row) for row in rows]

    def members(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> frozenset[str]:
        check_types(campaign_id=campaign_id, group_id=group_id)
        rows = pg(unit).conn.execute(
            "SELECT m.participant_id FROM campaign.group_members m "
            "JOIN campaign.groups g ON g.id = m.group_id AND g.campaign_id = m.campaign_id "
            "JOIN campaign.participants p ON p.id = m.participant_id AND p.campaign_id = m.campaign_id "
            "WHERE m.campaign_id = %s AND m.group_id = %s AND g.removed_at IS NULL AND p.removed_at IS NULL",
            (campaign_id, group_id),
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def groups_of(self, unit: UnitOfWork, campaign_id: str, participant_id: str) -> frozenset[str]:
        check_types(campaign_id=campaign_id, participant_id=participant_id)
        rows = pg(unit).conn.execute(
            "SELECT m.group_id FROM campaign.group_members m "
            "JOIN campaign.groups g ON g.id = m.group_id AND g.campaign_id = m.campaign_id "
            "JOIN campaign.participants p ON p.id = m.participant_id AND p.campaign_id = m.campaign_id "
            "WHERE m.campaign_id = %s AND m.participant_id = %s AND g.removed_at IS NULL "
            "AND p.removed_at IS NULL",
            (campaign_id, participant_id),
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def queued_projections(
        self, unit: UnitOfWork, campaign_id: str, *, after_id: int = 0, limit: int
    ) -> list[QueuedProjection]:
        check_types(campaign_id=campaign_id, after_id=after_id, limit=limit)
        _check_page(after_id, limit)
        rows = pg(unit).conn.execute(
            f"SELECT {_Q_COLUMNS} FROM campaign.projection_queue "
            f"WHERE campaign_id = %s AND id > %s ORDER BY id LIMIT %s",
            (campaign_id, after_id, limit),
        ).fetchall()
        return [_queued(row) for row in rows]

    def group_for_command(self, unit: UnitOfWork, campaign_id: str, command_id: str) -> Group | None:
        check_types(campaign_id=campaign_id, command_id=command_id)
        _check_command_id(command_id)
        row = pg(unit).conn.execute(
            f"SELECT {_G_COLUMNS} FROM campaign.groups WHERE campaign_id = %s AND created_command_id = %s",
            (campaign_id, command_id),
        ).fetchone()
        return None if row is None else _group(row)

    # ── Writes ───────────────────────────────────────────────────────────────

    def check_principals(self, unit: UnitOfWork, campaign_id: str, eligibility: Eligibility) -> None:
        check_types(campaign_id=campaign_id, eligibility=eligibility)
        unit.require_exclusive_campaign_lock(campaign_id)
        if eligibility.field_class not in LIST_CLASSES:
            return
        found = pg(unit).conn.execute(
            _PRINCIPAL_COUNT[eligibility.field_class], (campaign_id, list(eligibility.ids))
        ).fetchone()
        if found is None or int(found[0]) != len(eligibility.ids):
            raise InvalidPrincipal()

    def put(
        self,
        unit: UnitOfWork,
        document: DocumentRecord,
        field_key: str,
        eligibility: Eligibility,
        *,
        now: datetime | None = None,
    ) -> None:
        check_types(document=document, field_key=field_key, eligibility=eligibility)
        unit.require_exclusive_campaign_lock(document.campaign_id)
        _storable(document, field_key, eligibility)
        conn = pg(unit).conn
        if eligibility.field_class is FieldClass.UNCLASSIFIED:
            there = conn.execute(
                "SELECT 1 FROM campaign.documents WHERE id = %s AND campaign_id = %s",
                (document.id, document.campaign_id),
            ).fetchone()
            if there is None:
                raise MissingParent("no such document in that campaign")
            conn.execute(
                "DELETE FROM campaign.field_eligibility WHERE document_id = %s AND field_key = %s AND campaign_id = %s",
                (document.id, field_key, document.campaign_id),
            )
            return
        moment = now_or(now)
        assert eligibility.source is not None
        written = conn.execute(
            "INSERT INTO campaign.field_eligibility (document_id, field_key, campaign_id, eligibility_class, "
            "principal_ids, classification_source, created_at, updated_at) "
            "SELECT %s, %s, %s, %s, %s, %s, %s, %s "
            "WHERE EXISTS (SELECT 1 FROM campaign.documents WHERE id = %s AND campaign_id = %s) "
            "ON CONFLICT (document_id, field_key) DO UPDATE SET eligibility_class = EXCLUDED.eligibility_class, "
            "principal_ids = EXCLUDED.principal_ids, classification_source = EXCLUDED.classification_source, "
            "updated_at = EXCLUDED.updated_at "
            "WHERE campaign.field_eligibility.campaign_id = EXCLUDED.campaign_id "
            "RETURNING document_id",
            (
                document.id,
                field_key,
                document.campaign_id,
                eligibility.field_class.value,
                list(eligibility.ids) if eligibility.field_class in LIST_CLASSES else None,
                eligibility.source.value,
                moment,
                moment,
                document.id,
                document.campaign_id,
            ),
        ).fetchone()
        if written is None:
            raise MissingParent("no such document in that campaign")

    def create_group(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        name: str,
        command_id: str | None = None,
        now: datetime | None = None,
    ) -> Group:
        check_types(campaign_id=campaign_id, name=name, command_id=command_id)
        _check_command_id(command_id)
        unit.require_exclusive_campaign_lock(campaign_id)
        conn = pg(unit).conn
        if command_id is not None:
            replayed = self.group_for_command(unit, campaign_id, command_id)
            if replayed is not None:
                return replayed
        named = check_group_name(name)
        live = conn.execute(
            "SELECT count(*) FROM campaign.groups WHERE campaign_id = %s AND removed_at IS NULL", (campaign_id,)
        ).fetchone()
        if live is not None and int(live[0]) >= GROUPS_PER_CAMPAIGN_MAX:
            raise GroupLimit()
        moment = now_or(now)
        row: tuple | None = None
        taken = False
        try:
            # A savepoint: the partial unique index decides a name collision, and
            # a UniqueViolation would otherwise abort the caller's transaction.
            with conn.transaction():
                row = conn.execute(
                    f"INSERT INTO campaign.groups (id, campaign_id, name, name_key, created_command_id, "
                    f"created_at, updated_at) SELECT %s, %s, %s, %s, %s, %s, %s "
                    f"WHERE EXISTS (SELECT 1 FROM campaign.campaigns WHERE id = %s) RETURNING {_G_COLUMNS}",
                    (
                        ident.new_id(ident.GROUP),
                        campaign_id,
                        named,
                        alias_key(named),
                        command_id,
                        moment,
                        moment,
                        campaign_id,
                    ),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            # Its DETAIL quotes the name key. Raised below, OUTSIDE this handler,
            # so the driver's error is neither `__cause__` nor `__context__`.
            taken = True
        if taken:
            raise GroupNameTaken()
        if row is None:
            raise MissingParent("no such campaign")
        return _group(row)

    def hold_group(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> Group | None:
        check_types(campaign_id=campaign_id, group_id=group_id)
        transaction = pg(unit)
        transaction.note_row_lock()
        transaction.conn.execute(
            "SELECT set_config('transaction_timeout', %s, true)", (transaction.transaction_bound(None),)
        )
        # The campaign is in the statement that takes the lock (G-11), and the
        # lock is FOR NO KEY UPDATE: a rename changes no key column, so a
        # member insert's FOR KEY SHARE on this row is never blocked (RQ-3).
        row = transaction.conn.execute(
            f"SELECT {_G_COLUMNS} FROM campaign.groups WHERE id = %s AND campaign_id = %s FOR NO KEY UPDATE",
            (group_id, campaign_id),
        ).fetchone()
        return None if row is None else _group(row)

    def rename_group(
        self, unit: UnitOfWork, campaign_id: str, group_id: str, *, name: str, now: datetime | None = None
    ) -> Group:
        check_types(campaign_id=campaign_id, group_id=group_id, name=name)
        named = check_group_name(name)
        held = self.hold_group(unit, campaign_id, group_id)
        if held is None or not held.is_live:
            raise MissingParent("no such group in that campaign")
        if held.name == named:
            return held
        conn = pg(unit).conn
        row: tuple | None = None
        taken = False
        try:
            with conn.transaction():
                row = conn.execute(
                    f"UPDATE campaign.groups SET name = %s, name_key = %s, updated_at = %s "
                    f"WHERE id = %s AND campaign_id = %s RETURNING {_G_COLUMNS}",
                    (named, alias_key(named), now_or(now), group_id, campaign_id),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            taken = True
        if taken:
            raise GroupNameTaken()
        assert row is not None
        return _group(row)

    def remove_group(
        self, unit: UnitOfWork, campaign_id: str, group_id: str, *, now: datetime | None = None
    ) -> bool:
        check_types(campaign_id=campaign_id, group_id=group_id)
        unit.require_exclusive_campaign_lock(campaign_id)
        held = self.hold_group(unit, campaign_id, group_id)
        if held is None:
            raise MissingParent("no such group in that campaign")
        if not held.is_live:
            return False
        moment = now_or(now)
        pg(unit).conn.execute(
            "UPDATE campaign.groups SET removed_at = %s, updated_at = %s WHERE id = %s AND campaign_id = %s",
            (moment, moment, group_id, campaign_id),
        )
        return True

    def _parents(
        self, unit: UnitOfWork, campaign_id: str, group_id: str, participant_id: str, *, live_seat: bool
    ) -> None:
        """`MissingParent` unless the group is live in this campaign and the seat
        is of this campaign (and not removed, when `live_seat`). Read, not
        locked: RQ-3 puts group and seat rows at one level with no order."""
        seat = "AND removed_at IS NULL" if live_seat else ""
        found = pg(unit).conn.execute(
            f"SELECT {_LIVE_GROUP} AND EXISTS (SELECT 1 FROM campaign.participants "
            f"WHERE id = %s AND campaign_id = %s {seat})",
            (group_id, campaign_id, participant_id, campaign_id),
        ).fetchone()
        if found is None or not found[0]:
            raise MissingParent("no such group or seat in that campaign")

    def add_member(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        group_id: str,
        participant_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        check_types(campaign_id=campaign_id, group_id=group_id, participant_id=participant_id)
        unit.require_exclusive_campaign_lock(campaign_id)
        self._parents(unit, campaign_id, group_id, participant_id, live_seat=True)
        added = pg(unit).conn.execute(
            "INSERT INTO campaign.group_members (group_id, participant_id, campaign_id, added_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (group_id, participant_id) DO NOTHING RETURNING group_id",
            (group_id, participant_id, campaign_id, now_or(now)),
        ).fetchone()
        return added is not None

    def remove_member(self, unit: UnitOfWork, campaign_id: str, group_id: str, participant_id: str) -> bool:
        check_types(campaign_id=campaign_id, group_id=group_id, participant_id=participant_id)
        unit.require_exclusive_campaign_lock(campaign_id)
        self._parents(unit, campaign_id, group_id, participant_id, live_seat=False)
        gone = pg(unit).conn.execute(
            "DELETE FROM campaign.group_members WHERE group_id = %s AND participant_id = %s AND campaign_id = %s "
            "RETURNING group_id",
            (group_id, participant_id, campaign_id),
        ).fetchone()
        return gone is not None


# ── The twin ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _EligibilityRow:
    document_id: str
    campaign_id: str
    eligibility: Eligibility
    #: `Staging` offers no removal: a reset replaces the row with this.
    gone: bool = False


@dataclass(frozen=True)
class _GroupRow:
    group: Group
    name_key: str = field(repr=False)
    created_command_id: str | None


@dataclass(frozen=True)
class _MemberRow:
    group_id: str
    participant_id: str
    campaign_id: str
    gone: bool = False


def _key(first: str, second: str) -> str:
    return f"{first}\x1f{second}"


class InMemoryEligibilityStore:
    """The twin. Its tables are the database's (`shared_rows`), and every write
    is staged through `Staging`, which claims the unit as the database's one
    writer first. **It reads documents only through the injected
    `DocumentStore.get`**, which hides deleted documents as PostgreSQL's
    cascade removes their rows: a direct reader of the documents table would
    see a tombstone and accept what the foreign key refuses. Races are proved
    against PostgreSQL (`tests/test_eligibility_db.py`), never here."""

    def __init__(self, db: InMemoryDatabase, *, documents: DocumentStore) -> None:
        self._documents = documents
        # justification: the campaigns table's row type is campaign_store's
        # Campaign, and only its presence is read here.
        self._campaigns: Staging[Any] = shared_rows(db, "campaigns")
        self._participants: Staging[Participant] = shared_rows(db, "participants")
        self._rows: Staging[_EligibilityRow] = shared_rows(db, "field_eligibility")
        self._groups: Staging[_GroupRow] = shared_rows(db, "groups")
        self._members: Staging[_MemberRow] = shared_rows(db, "group_members")

    def _live_document(self, unit: UnitOfWork, campaign_id: str, document_id: str) -> DocumentRecord | None:
        return self._documents.get(unit, campaign_id, document_id)

    def _stored(self, twin: InMemoryTransaction, document: DocumentRecord) -> dict[str, Eligibility]:
        prefix = document.id + "\x1f"
        return {
            key.removeprefix(prefix): row.eligibility
            for key, row in self._rows.visible(twin).items()
            if key.startswith(prefix) and not row.gone and row.campaign_id == document.campaign_id
        }

    # ── Reads ────────────────────────────────────────────────────────────────

    def eligibility_of(self, unit: UnitOfWork, document: DocumentRecord, field_key: str) -> Eligibility:
        check_types(document=document, field_key=field_key)
        if field_key not in classifiable_keys(document):
            return GM_ONLY_BY_CONSTRUCTION
        if self._live_document(unit, document.campaign_id, document.id) is None:
            return UNCLASSIFIED
        return self._stored(fake(unit), document).get(field_key, UNCLASSIFIED)

    def eligibilities_of(self, unit: UnitOfWork, document: DocumentRecord) -> dict[str, Eligibility]:
        check_types(document=document)
        keys = classifiable_keys(document)
        if not keys:
            return {}
        live = self._live_document(unit, document.campaign_id, document.id) is not None
        stored = self._stored(fake(unit), document) if live else {}
        return {key: stored.get(key, UNCLASSIFIED) for key in sorted(keys)}

    def _group_row(self, twin: InMemoryTransaction, campaign_id: str, group_id: str) -> _GroupRow | None:
        found = self._groups.visible(twin).get(group_id)
        return None if found is None or found.group.campaign_id != campaign_id else found

    def get_group(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> Group | None:
        check_types(campaign_id=campaign_id, group_id=group_id)
        found = self._group_row(fake(unit), campaign_id, group_id)
        return None if found is None else found.group

    def _live_groups(self, twin: InMemoryTransaction, campaign_id: str) -> list[_GroupRow]:
        return [
            row
            for row in self._groups.visible(twin).values()
            if row.group.campaign_id == campaign_id and row.group.is_live
        ]

    def list_groups(self, unit: UnitOfWork, campaign_id: str) -> list[Group]:
        check_types(campaign_id=campaign_id)
        rows = sorted(self._live_groups(fake(unit), campaign_id), key=lambda row: (row.name_key, row.group.id))
        return [row.group for row in rows[:GROUPS_PER_CAMPAIGN_MAX]]

    def _live_seat(self, twin: InMemoryTransaction, campaign_id: str, participant_id: str) -> bool:
        seat = self._participants.visible(twin).get(participant_id)
        return seat is not None and seat.campaign_id == campaign_id and seat.is_active

    def _memberships(self, twin: InMemoryTransaction, campaign_id: str) -> list[_MemberRow]:
        """Membership rows of live groups and seats that are not removed."""
        return [
            row
            for row in self._members.visible(twin).values()
            if not row.gone
            and row.campaign_id == campaign_id
            and (group := self._group_row(twin, campaign_id, row.group_id)) is not None
            and group.group.is_live
            and self._live_seat(twin, campaign_id, row.participant_id)
        ]

    def members(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> frozenset[str]:
        check_types(campaign_id=campaign_id, group_id=group_id)
        return frozenset(
            row.participant_id for row in self._memberships(fake(unit), campaign_id) if row.group_id == group_id
        )

    def groups_of(self, unit: UnitOfWork, campaign_id: str, participant_id: str) -> frozenset[str]:
        check_types(campaign_id=campaign_id, participant_id=participant_id)
        return frozenset(
            row.group_id for row in self._memberships(fake(unit), campaign_id) if row.participant_id == participant_id
        )

    def queued_projections(
        self, unit: UnitOfWork, campaign_id: str, *, after_id: int = 0, limit: int
    ) -> list[QueuedProjection]:
        check_types(campaign_id=campaign_id, after_id=after_id, limit=limit)
        _check_page(after_id, limit)
        items = [
            item
            for item in fake(unit).projected_items(campaign_id)
            if item.id > after_id and self._live_document(unit, campaign_id, item.document_id) is not None
        ]
        return items[:limit]

    def group_for_command(self, unit: UnitOfWork, campaign_id: str, command_id: str) -> Group | None:
        check_types(campaign_id=campaign_id, command_id=command_id)
        _check_command_id(command_id)
        for row in self._groups.visible(fake(unit)).values():
            if row.group.campaign_id == campaign_id and row.created_command_id == command_id:
                return row.group
        return None

    # ── Writes ───────────────────────────────────────────────────────────────

    def check_principals(self, unit: UnitOfWork, campaign_id: str, eligibility: Eligibility) -> None:
        check_types(campaign_id=campaign_id, eligibility=eligibility)
        unit.require_exclusive_campaign_lock(campaign_id)
        twin = fake(unit)
        kind = eligibility.field_class
        if kind is FieldClass.PARTICIPANTS:
            valid = all(self._live_seat(twin, campaign_id, value) for value in eligibility.ids)
        elif kind is FieldClass.CHARACTERS:
            valid = all(
                (sheet := self._live_document(unit, campaign_id, value)) is not None and sheet.type == CHARACTER_SHEET
                for value in eligibility.ids
            )
        elif kind is FieldClass.GROUPS:
            valid = all(
                (group := self._group_row(twin, campaign_id, value)) is not None and group.group.is_live
                for value in eligibility.ids
            )
        else:
            valid = True
        if not valid:
            raise InvalidPrincipal()

    def put(
        self,
        unit: UnitOfWork,
        document: DocumentRecord,
        field_key: str,
        eligibility: Eligibility,
        *,
        now: datetime | None = None,
    ) -> None:
        check_types(document=document, field_key=field_key, eligibility=eligibility)
        unit.require_exclusive_campaign_lock(document.campaign_id)
        _storable(document, field_key, eligibility)
        # The composite foreign key's answer: a document of that campaign that
        # is still there, asked of the document store and never of its table.
        if self._live_document(unit, document.campaign_id, document.id) is None:
            raise MissingParent("no such document in that campaign")
        now_or(now)
        row = _EligibilityRow(
            document.id,
            document.campaign_id,
            eligibility,
            gone=eligibility.field_class is FieldClass.UNCLASSIFIED,
        )
        self._rows.replace(fake(unit), _key(document.id, field_key), row)

    def create_group(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        name: str,
        command_id: str | None = None,
        now: datetime | None = None,
    ) -> Group:
        check_types(campaign_id=campaign_id, name=name, command_id=command_id)
        _check_command_id(command_id)
        unit.require_exclusive_campaign_lock(campaign_id)
        twin = fake(unit)
        if command_id is not None:
            replayed = self.group_for_command(unit, campaign_id, command_id)
            if replayed is not None:
                return replayed
        named = check_group_name(name)
        live = self._live_groups(twin, campaign_id)
        if len(live) >= GROUPS_PER_CAMPAIGN_MAX:
            raise GroupLimit()
        if any(row.name_key == alias_key(named) for row in live):
            raise GroupNameTaken()
        if campaign_id not in self._campaigns.visible(twin):
            raise MissingParent("no such campaign")
        moment = now_or(now)
        group = Group(ident.new_id(ident.GROUP), campaign_id, named, moment, moment)
        self._groups.add(twin, group.id, _GroupRow(group, alias_key(named), command_id))
        return group

    def hold_group(self, unit: UnitOfWork, campaign_id: str, group_id: str) -> Group | None:
        check_types(campaign_id=campaign_id, group_id=group_id)
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(None)
        found = self._group_row(twin, campaign_id, group_id)
        return None if found is None else found.group

    def rename_group(
        self, unit: UnitOfWork, campaign_id: str, group_id: str, *, name: str, now: datetime | None = None
    ) -> Group:
        check_types(campaign_id=campaign_id, group_id=group_id, name=name)
        named = check_group_name(name)
        held = self.hold_group(unit, campaign_id, group_id)
        if held is None or not held.is_live:
            raise MissingParent("no such group in that campaign")
        if held.name == named:
            return held
        twin = fake(unit)
        key = alias_key(named)
        if any(row.name_key == key and row.group.id != group_id for row in self._live_groups(twin, campaign_id)):
            raise GroupNameTaken()
        current = self._groups.visible(twin)[group_id]
        renamed = replace(current.group, name=named, updated_at=now_or(now))
        self._groups.replace(twin, group_id, replace(current, group=renamed, name_key=key))
        return renamed

    def remove_group(
        self, unit: UnitOfWork, campaign_id: str, group_id: str, *, now: datetime | None = None
    ) -> bool:
        check_types(campaign_id=campaign_id, group_id=group_id)
        unit.require_exclusive_campaign_lock(campaign_id)
        held = self.hold_group(unit, campaign_id, group_id)
        if held is None:
            raise MissingParent("no such group in that campaign")
        if not held.is_live:
            return False
        twin = fake(unit)
        moment = now_or(now)
        current = self._groups.visible(twin)[group_id]
        removed = replace(held, removed_at=moment, updated_at=moment)
        self._groups.replace(twin, group_id, replace(current, group=removed))
        return True

    def _parents(
        self, twin: InMemoryTransaction, campaign_id: str, group_id: str, participant_id: str, *, live_seat: bool
    ) -> None:
        group = self._group_row(twin, campaign_id, group_id)
        seat = self._participants.visible(twin).get(participant_id)
        seat_ok = seat is not None and seat.campaign_id == campaign_id and (seat.is_active or not live_seat)
        if group is None or not group.group.is_live or not seat_ok:
            raise MissingParent("no such group or seat in that campaign")

    def add_member(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        group_id: str,
        participant_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        check_types(campaign_id=campaign_id, group_id=group_id, participant_id=participant_id)
        unit.require_exclusive_campaign_lock(campaign_id)
        twin = fake(unit)
        self._parents(twin, campaign_id, group_id, participant_id, live_seat=True)
        now_or(now)
        existing = self._members.visible(twin).get(_key(group_id, participant_id))
        if existing is not None and not existing.gone:
            return False
        self._members.replace(twin, _key(group_id, participant_id), _MemberRow(group_id, participant_id, campaign_id))
        return True

    def remove_member(self, unit: UnitOfWork, campaign_id: str, group_id: str, participant_id: str) -> bool:
        check_types(campaign_id=campaign_id, group_id=group_id, participant_id=participant_id)
        unit.require_exclusive_campaign_lock(campaign_id)
        twin = fake(unit)
        self._parents(twin, campaign_id, group_id, participant_id, live_seat=False)
        existing = self._members.visible(twin).get(_key(group_id, participant_id))
        if existing is None or existing.gone:
            return False
        self._members.replace(twin, _key(group_id, participant_id), replace(existing, gone=True))
        return True

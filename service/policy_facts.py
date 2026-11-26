"""Campaign field eligibility from one snapshot in the caller's unit.

Reveal Confirm reads after taking its shared campaign lock. Narrowing scans
can read without that lock. This loader takes no locks and makes no writes.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable
from typing import Final, Protocol

import psycopg

from . import campaign_identity as ident
from .campaign_store import Staging, fake, pg, shared_rows
from .db import InMemoryDatabase, UnitOfWork
from .eligibility_store import (
    GM_ONLY_BY_CONSTRUCTION,
    QUEUE_PAGE_MAX,
    Eligibility,
    FieldClass,
    _EligibilityRow,
    _GroupRow,
    _MemberRow,
)
from .participant_store import Participant
from .policy import DocumentFacts, PolicyFacts, decode_class

FACTS_DOCUMENTS_MAX: Final = QUEUE_PAGE_MAX
_log = logging.getLogger(__name__)


class PolicyUnavailable(psycopg.OperationalError):
    """The database could not answer. Routes return a retryable 503."""

    def __init__(self) -> None:
        super().__init__("Campaign policy is temporarily unavailable")


class PolicyFactsStore(Protocol):
    def load(self, unit: UnitOfWork, campaign_id: str, document_ids: Collection[str]) -> PolicyFacts: ...


class DocumentFactRow(Protocol):
    @property
    def type(self) -> str: ...

    @property
    def type_version(self) -> int: ...

    @property
    def linked_participant_id(self) -> str | None: ...


class PolicyDocumentReader(Protocol):
    def get(self, unit: UnitOfWork, campaign_id: str, document_id: str) -> DocumentFactRow | None: ...


_LOAD_SQL: Final = """
WITH docs AS (
    SELECT id, type, type_version FROM campaign.documents
    WHERE campaign_id = %(campaign)s AND id = ANY(%(documents)s)
), classes AS (
    SELECT e.document_id, e.field_key, e.eligibility_class, e.principal_ids, e.classification_source
    FROM campaign.field_eligibility e JOIN docs d ON d.id = e.document_id
    WHERE e.campaign_id = %(campaign)s
), sheets AS (
    SELECT d.id, d.linked_participant_id FROM campaign.documents d
    WHERE d.campaign_id = %(campaign)s
      AND d.id IN (SELECT unnest(principal_ids) FROM classes WHERE eligibility_class = 'characters')
), seats AS (
    SELECT id FROM campaign.participants WHERE campaign_id = %(campaign)s AND removed_at IS NULL
), live_groups AS (
    SELECT id FROM campaign.groups WHERE campaign_id = %(campaign)s AND removed_at IS NULL
      AND id IN (SELECT unnest(principal_ids) FROM classes WHERE eligibility_class = 'groups')
), members AS (
    SELECT m.group_id, m.participant_id FROM campaign.group_members m
    JOIN live_groups g ON g.id = m.group_id JOIN seats p ON p.id = m.participant_id
    WHERE m.campaign_id = %(campaign)s
)
SELECT
    COALESCE((SELECT json_agg(json_build_array(id, type, type_version)) FROM docs), '[]'),
    COALESCE((SELECT json_agg(json_build_array(
        document_id, field_key, eligibility_class, principal_ids, classification_source
    )) FROM classes), '[]'),
    COALESCE((SELECT json_agg(json_build_array(id, linked_participant_id)) FROM sheets), '[]'),
    COALESCE((SELECT array_agg(id) FROM seats), '{}'),
    COALESCE((SELECT array_agg(id) FROM live_groups), '{}'),
    COALESCE((SELECT json_agg(json_build_array(group_id, participant_id)) FROM members), '[]')
"""

_ClassRow = tuple[str, str, object, object, object]


def _validated(campaign_id: str, document_ids: Collection[str]) -> list[str]:
    if not isinstance(campaign_id, str) or not ident.is_id(ident.CAMPAIGN, campaign_id):
        raise ValueError("campaign policy requires a well-formed campaign id")
    if isinstance(document_ids, str | bytes) or not 1 <= len(document_ids) <= FACTS_DOCUMENTS_MAX:
        raise ValueError(f"campaign policy requires 1 to {FACTS_DOCUMENTS_MAX} document ids")
    ids = list(document_ids)
    if any(not isinstance(document_id, str) or not ident.is_id(ident.DOCUMENT, document_id) for document_id in ids):
        raise ValueError("campaign policy requires well-formed document ids")
    return ids


def _classes(rows: Iterable[_ClassRow]) -> dict[tuple[str, str], Eligibility]:
    decoded = {(doc, key): decode_class(kind, ids, source) for doc, key, kind, ids, source in rows}
    unreadable = sum(value is GM_ONLY_BY_CONSTRUCTION for value in decoded.values())
    if unreadable:
        _log.warning("policy_class_unreadable count=%d", unreadable)
    return decoded


def _members(groups: Iterable[str], rows: Iterable[tuple[str, str]]) -> dict[str, frozenset[str]]:
    gathered: dict[str, set[str]] = {group: set() for group in groups}
    for group, participant in rows:
        gathered[group].add(participant)
    return {group: frozenset(participants) for group, participants in gathered.items()}


class PostgresPolicyFacts:
    def load(self, unit: UnitOfWork, campaign_id: str, document_ids: Collection[str]) -> PolicyFacts:
        ids = _validated(campaign_id, document_ids)
        conn = pg(unit).conn
        try:
            docs, classes, sheets, seats, groups, members = conn.execute(
                _LOAD_SQL, {"campaign": campaign_id, "documents": ids}
            ).fetchone()
        except psycopg.OperationalError as exc:
            _log.warning("policy_database_unavailable sqlstate=%s", exc.sqlstate or "none")
        else:
            return PolicyFacts(
                campaign_id,
                {doc: DocumentFacts(doc, kind, version) for doc, kind, version in docs},
                _classes(classes),
                dict(sheets),
                frozenset(seats),
                _members(groups, members),
            )
        raise PolicyUnavailable()


class InMemoryPolicyFacts:
    """Use the twin's shared campaign rows and its live document reader."""

    def __init__(self, db: InMemoryDatabase, *, documents: PolicyDocumentReader) -> None:
        self._documents = documents
        self._participants: Staging[Participant] = shared_rows(db, "participants")
        self._rows: Staging[_EligibilityRow] = shared_rows(db, "field_eligibility")
        self._groups: Staging[_GroupRow] = shared_rows(db, "groups")
        self._members: Staging[_MemberRow] = shared_rows(db, "group_members")

    def load(self, unit: UnitOfWork, campaign_id: str, document_ids: Collection[str]) -> PolicyFacts:
        ids = _validated(campaign_id, document_ids)
        twin = fake(unit)
        documents = {
            doc: DocumentFacts(doc, row.type, row.type_version)
            for doc in ids
            if (row := self._documents.get(unit, campaign_id, doc)) is not None
        }
        classes = _classes(
            (
                row.document_id, key.split("\x1f", 1)[1],
                row.eligibility.field_class, row.eligibility.ids, row.eligibility.source,
            )
            for key, row in self._rows.visible(twin).items()
            if row.campaign_id == campaign_id
            and row.document_id in documents
            and not row.gone
        )
        sheets = {
            doc: row.linked_participant_id
            for doc in _named(classes.values(), FieldClass.CHARACTERS)
            if (row := self._documents.get(unit, campaign_id, doc)) is not None
        }
        seats = frozenset(
            p.id for p in self._participants.visible(twin).values() if p.campaign_id == campaign_id and p.is_active
        )
        named_groups = _named(classes.values(), FieldClass.GROUPS)
        groups = {
            g.id
            for row in self._groups.visible(twin).values()
            if (g := row.group).campaign_id == campaign_id and g.is_live and g.id in named_groups
        }
        members = _members(
            groups,
            (
                (row.group_id, row.participant_id)
                for row in self._members.visible(twin).values()
                if row.campaign_id == campaign_id
                and not row.gone
                and row.group_id in groups
                and row.participant_id in seats
            ),
        )
        return PolicyFacts(campaign_id, documents, classes, sheets, seats, members)


def _named(classes: Iterable[Eligibility], kind: FieldClass) -> set[str]:
    return {ident for row in classes if row.field_class is kind for ident in row.ids}

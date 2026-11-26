"""Campaign document names, explicitly registered aliases and the GM persona graph."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .campaign_store import MissingParent, pg
from .db import UnitOfWork
from .document_store import name_key
from .eligibility_store import Eligibility, FieldClass, check_postgres_principals
from .policy import SlotId, decode_class, eligible_alias_for_audience
from .workbench_contracts import TEXT_FIELD_MAX_CHARS, check_stored_text


@dataclass(frozen=True)
class Entity:
    document_id: str
    kind: str
    write_revision: int
    canonical_name: str = field(repr=False)


@dataclass(frozen=True)
class EntityAlias:
    id: UUID
    document_id: str
    term: str = field(repr=False)
    pronunciation: str | None = field(repr=False)
    eligibility: Eligibility | None


@dataclass(frozen=True)
class PersonaLink:
    persona_document_id: str
    identity_document_id: str


@dataclass(frozen=True)
class CampaignEntityIndex:
    """One deterministic source snapshot for the GM's campaign lexicon."""

    campaign_id: str
    policy_revision: int
    entities: tuple[Entity, ...]
    aliases: tuple[EntityAlias, ...]
    identities: tuple[PersonaLink, ...] = field(repr=False)

    @property
    def revision(self) -> tuple[str, int, tuple[tuple[str, int], ...]]:
        return self.campaign_id, self.policy_revision, tuple((e.document_id, e.write_revision) for e in self.entities)


@dataclass(frozen=True)
class EligibleAlias:
    """An internal resolver input. Canonical names, hints and identity edges stay with the GM."""

    id: UUID
    document_id: str
    term: str = field(repr=False)


@dataclass(frozen=True)
class _Audience:
    active_participants: frozenset[str]
    sheet_links: dict[str, str | None]
    group_members: dict[str, frozenset[str]]


_GM_INDEX_SQL = """
WITH owned AS (
    SELECT c.id, a.authz_revision FROM campaign.campaigns c
    JOIN campaign.authz_state a ON a.campaign_id = c.id
    WHERE c.id = %(campaign)s AND c.owner_id = %(owner)s
), docs AS (
    SELECT d.id, d.campaign_id, d.type, d.write_revision, d.data ->> 'name' AS name
    FROM campaign.documents d JOIN owned o ON o.id = d.campaign_id
    WHERE d.archived_at IS NULL AND nullif(d.data ->> 'name', '') IS NOT NULL
), aliases AS (
    SELECT a.id, a.document_id, a.alias, a.pronunciation,
           a.eligibility_class, a.principal_ids, a.classification_source
    FROM campaign.entity_aliases a JOIN docs d ON d.id = a.document_id AND d.campaign_id = a.campaign_id
), edges AS (
    SELECT l.persona_document_id, l.identity_document_id FROM campaign.entity_persona_links l
    JOIN docs p ON p.id = l.persona_document_id AND p.campaign_id = l.campaign_id
    JOIN docs i ON i.id = l.identity_document_id AND i.campaign_id = l.campaign_id
)
SELECT authz_revision,
       coalesce((SELECT json_agg(json_build_array(id, type, write_revision, name) ORDER BY id) FROM docs), '[]'),
       coalesce((SELECT json_agg(json_build_array(
           id, document_id, alias, pronunciation, eligibility_class, principal_ids, classification_source
       ) ORDER BY id) FROM aliases), '[]'),
       coalesce((SELECT json_agg(json_build_array(persona_document_id, identity_document_id)
           ORDER BY persona_document_id, identity_document_id) FROM edges), '[]')
FROM owned
"""

_ELIGIBLE_ALIAS_SQL = """
WITH matching AS (
    SELECT a.id, a.document_id, a.alias, a.eligibility_class, a.principal_ids, a.classification_source
    FROM campaign.entity_aliases a JOIN campaign.documents d
        ON d.id = a.document_id AND d.campaign_id = a.campaign_id
    JOIN campaign.campaigns c ON c.id = a.campaign_id AND c.archived_at IS NULL
    WHERE a.campaign_id = %(campaign)s AND d.archived_at IS NULL
      AND (%(key)s::text IS NULL OR a.alias_key = %(key)s)
), seat AS (
    SELECT id FROM campaign.participants
    WHERE campaign_id = %(campaign)s AND id = %(audience)s AND removed_at IS NULL
), sheets AS (
    SELECT d.id, d.linked_participant_id FROM campaign.documents d JOIN seat p ON p.id = d.linked_participant_id
    WHERE d.campaign_id = %(campaign)s
      AND d.id IN (SELECT unnest(principal_ids) FROM matching WHERE eligibility_class = 'characters')
), memberships AS (
    SELECT m.group_id, m.participant_id FROM campaign.group_members m
    JOIN campaign.groups g ON g.id = m.group_id AND g.campaign_id = m.campaign_id AND g.removed_at IS NULL
    JOIN seat p ON p.id = m.participant_id
    WHERE m.campaign_id = %(campaign)s
      AND g.id IN (SELECT unnest(principal_ids) FROM matching WHERE eligibility_class = 'groups')
)
SELECT coalesce((SELECT json_agg(json_build_array(
           id, document_id, alias, eligibility_class, principal_ids, classification_source
       ) ORDER BY id) FROM matching), '[]'),
       coalesce((SELECT array_agg(id) FROM seat), '{}'),
       coalesce((SELECT json_agg(json_build_array(id, linked_participant_id)) FROM sheets), '[]'),
       coalesce((SELECT json_agg(json_build_array(group_id, participant_id)) FROM memberships), '[]')
"""


def _classification(kind: object, ids: object, source: object) -> Eligibility | None:
    return None if kind is None and ids is None and source is None else decode_class(kind, ids, source)


def _term(value: str) -> str:
    cleaned = check_stored_text(value).strip()
    if not 1 <= len(cleaned) <= TEXT_FIELD_MAX_CHARS or len(cleaned.splitlines()) != 1:
        raise ValueError("Resolver names and pronunciation hints are one nonempty line.")
    return cleaned


def _alias_key(term: str) -> str:
    key = name_key(term)
    if not key:
        raise ValueError("A resolver name must have a searchable character.")
    return key


def _lock_owned(unit: UnitOfWork, campaign_id: str, owner_id: int) -> None:
    if pg(unit).conn.execute(
        "SELECT 1 FROM campaign.campaigns WHERE id = %s AND owner_id = %s", (campaign_id, owner_id)
    ).fetchone() is None:
        raise MissingParent()
    unit.lock_campaign(campaign_id, shared=False)


class PostgresEntityIndex:
    """Build lexicon inputs on demand from a current campaign snapshot."""

    def gm_index(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int) -> CampaignEntityIndex | None:
        row = pg(unit).conn.execute(_GM_INDEX_SQL, {"campaign": campaign_id, "owner": owner_id}).fetchone()
        if row is None:
            return None
        revision, docs, aliases, edges = row
        return CampaignEntityIndex(
            campaign_id, revision, tuple(Entity(*doc) for doc in docs),
            tuple(
                EntityAlias(UUID(id_), doc, alias, hint, _classification(kind, ids, source))
                for id_, doc, alias, hint, kind, ids, source in aliases
            ),
            tuple(PersonaLink(*edge) for edge in edges),
        )

    def eligible_aliases(
        self, unit: UnitOfWork, campaign_id: str, audience: SlotId, *, term: str | None = None
    ) -> tuple[EligibleAlias, ...]:
        """Class checks for a campaign audience, in the same snapshot as the alias read."""
        key = None if term is None else _alias_key(_term(term))
        aliases, seats, sheets, memberships = pg(unit).conn.execute(
            _ELIGIBLE_ALIAS_SQL, {"campaign": campaign_id, "audience": audience, "key": key},
        ).fetchone()
        members: dict[str, set[str]] = defaultdict(set)
        for group, participant in memberships:
            members[group].add(participant)
        facts = _Audience(frozenset(seats), dict(sheets), {group: frozenset(ids) for group, ids in members.items()})
        return tuple(
            EligibleAlias(UUID(id_), doc, alias)
            for id_, doc, alias, kind, ids, source in aliases
            if eligible_alias_for_audience(facts, _classification(kind, ids, source), audience).allowed
        )

    def register_alias(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, *,
        owner_id: int, term: str, pronunciation: str | None = None,
    ) -> UUID:
        alias = _term(term)
        key = _alias_key(alias)
        hint = None if pronunciation is None else _term(pronunciation)
        _lock_owned(unit, campaign_id, owner_id)
        conn = pg(unit).conn
        inserted = conn.execute(
            """INSERT INTO campaign.entity_aliases (id, campaign_id, document_id, alias, alias_key, pronunciation)
               SELECT %s, campaign_id, id, %s, %s, %s FROM campaign.documents
               WHERE campaign_id = %s AND id = %s AND archived_at IS NULL
               ON CONFLICT (campaign_id, document_id, alias_key) DO NOTHING RETURNING id""",
            (uuid4(), alias, key, hint, campaign_id, document_id),
        ).fetchone()
        if inserted is not None:
            unit.advance_authz_revision(campaign_id)
            return UUID(str(inserted[0]))
        existing = conn.execute(
            """SELECT a.id FROM campaign.entity_aliases a JOIN campaign.documents d
                   ON d.campaign_id = a.campaign_id AND d.id = a.document_id
               WHERE a.campaign_id = %s AND a.document_id = %s AND a.alias_key = %s AND d.archived_at IS NULL""",
            (campaign_id, document_id, key),
        ).fetchone()
        if existing is None:
            raise MissingParent()
        return UUID(str(existing[0]))

    def classify_alias(
        self, unit: UnitOfWork, campaign_id: str, alias_id: UUID, eligibility: Eligibility, *, owner_id: int
    ) -> bool:
        if eligibility.source is None and eligibility.field_class is not FieldClass.UNCLASSIFIED:
            raise ValueError("A stored alias classification requires a source.")
        kind = None if eligibility.field_class is FieldClass.UNCLASSIFIED else eligibility.field_class.value
        source = None if eligibility.source is None else eligibility.source.value
        principal_ids = list(eligibility.ids) or None
        _lock_owned(unit, campaign_id, owner_id)
        conn = pg(unit).conn
        row = conn.execute(
            """SELECT eligibility_class, principal_ids, classification_source FROM campaign.entity_aliases
               WHERE id = %s AND campaign_id = %s""", (alias_id, campaign_id),
        ).fetchone()
        if row is None:
            raise MissingParent()
        if row == (kind, principal_ids, source):
            return False
        check_postgres_principals(unit, campaign_id, eligibility)
        conn.execute(
            """UPDATE campaign.entity_aliases SET eligibility_class = %s, principal_ids = %s,
                   classification_source = %s WHERE id = %s AND campaign_id = %s""",
            (kind, principal_ids, source, alias_id, campaign_id),
        )
        unit.advance_authz_revision(campaign_id)
        return True

    def link_identity(
        self, unit: UnitOfWork, campaign_id: str, persona_id: str, identity_id: str, *, owner_id: int
    ) -> bool:
        if persona_id == identity_id:
            raise ValueError("An identity edge joins two different documents.")
        _lock_owned(unit, campaign_id, owner_id)
        conn = pg(unit).conn
        if conn.execute(
            "SELECT count(*) FROM campaign.documents WHERE campaign_id = %s AND id = ANY(%s) AND archived_at IS NULL",
            (campaign_id, [persona_id, identity_id]),
        ).fetchone() != (2,):
            raise MissingParent()
        inserted = conn.execute(
            """INSERT INTO campaign.entity_persona_links (campaign_id, persona_document_id, identity_document_id)
               VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1""",
            (campaign_id, persona_id, identity_id),
        ).fetchone()
        if inserted is None:
            return False
        unit.advance_authz_revision(campaign_id)
        return True

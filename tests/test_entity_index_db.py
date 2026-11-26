"""The alias registry and GM identity graph against the campaign schema."""

from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

import pytest
from _pg import needs_db
from test_eligibility_db import World, _campaign, _document, _group, _seat
from test_eligibility_db import dsn as dsn
from test_eligibility_db import world as world

from service.campaign_store import MissingParent
from service.eligibility_store import (
    UNCLASSIFIED,
    ClassificationSource,
    Eligibility,
    FieldClass,
    InvalidPrincipal,
    principals,
)
from service.entity_index import PersonaLink, PostgresEntityIndex
from service.workbench_contracts import Author, DocumentTypeId

pytestmark = pytest.mark.parametrize("world", [pytest.param("postgres", marks=needs_db)], indirect=True)
INDEX = PostgresEntityIndex()
GM = ClassificationSource.GM
PUBLIC = Eligibility(FieldClass.PUBLIC, (), GM)
CAMPAIGN = Eligibility(FieldClass.CAMPAIGN, (), GM)
PRIVATE = Eligibility(FieldClass.GM_ONLY, (), GM)


def _add(world: World, campaign: str, document: str, term: str, *, pronunciation: str | None = None):
    with world.db.transaction() as unit:
        return INDEX.register_alias(
            unit, campaign, document, owner_id=world.owner, term=term, pronunciation=pronunciation,
        )


def _classify(world: World, campaign: str, alias, eligibility: Eligibility) -> bool:
    with world.db.transaction() as unit:
        return INDEX.classify_alias(unit, campaign, alias, eligibility, owner_id=world.owner)


def _index(world: World, campaign: str):
    with world.db.transaction() as unit:
        result = INDEX.gm_index(unit, campaign, owner_id=world.owner)
    assert result is not None
    return result


def _allowed(world: World, campaign: str, audience: str | None, *, term: str | None = None):
    with world.db.transaction() as unit:
        return INDEX.eligible_aliases(unit, campaign, audience, term=term)


def test_index_rebuilds_idempotently_on_alias_and_document_revisions(world: World) -> None:
    campaign = _campaign(world)
    doc = _document(world, campaign, DocumentTypeId.NPC)
    initial = _index(world, campaign)
    assert initial == _index(world, campaign)
    assert initial.entities[0].canonical_name == "Rook" and initial.entities[0].kind == "npc"

    alias = _add(world, campaign, doc, "  The Ｒook  ", pronunciation="rook")
    registered = _index(world, campaign)
    assert registered.policy_revision == initial.policy_revision + 1
    assert _add(world, campaign, doc, "the rook") == alias
    assert _index(world, campaign) == registered
    assert registered.aliases[0].pronunciation == "rook"
    assert not _classify(world, campaign, alias, UNCLASSIFIED)
    assert _allowed(world, campaign, None) == ()
    assert _classify(world, campaign, alias, PUBLIC)
    assert len(_allowed(world, campaign, None, term=" THE  ROOK ")) == 1
    classified = _index(world, campaign)
    assert classified.policy_revision == registered.policy_revision + 1
    assert not _classify(world, campaign, alias, PUBLIC)
    assert _index(world, campaign) == classified

    with world.db.transaction() as unit:
        world.documents.write_fields(
            unit, campaign, doc, fields={"name": "Arden"}, author=Author.GM, base_write_revision=1,
        )
    revised = _index(world, campaign)
    assert revised.policy_revision == classified.policy_revision
    assert revised.revision != classified.revision
    assert revised.entities[0].canonical_name == "Arden"
    assert revised == _index(world, campaign)

    with world.db.transaction() as unit:
        assert world.documents.set_archived(unit, campaign, doc, archived=True)
    assert _allowed(world, campaign, None) == ()
    assert _index(world, campaign).entities == ()


def test_all_alias_classes_follow_current_seat_sheet_and_group_facts(world: World) -> None:
    campaign = _campaign(world)
    target = _document(world, campaign, DocumentTypeId.NPC)
    ana = _seat(world, campaign, "Ana")
    cy = _seat(world, campaign, "Cy")
    removed = _seat(world, campaign, "Removed")
    linked = _document(world, campaign)
    group = _group(world, campaign, members=(ana,))
    with world.db.transaction() as unit:
        assert world.documents.link_character_sheet(unit, campaign, linked, participant_id=ana)
        assert world.participants.remove(unit, campaign, removed)

    for term, eligibility in (
        ("Unknown", UNCLASSIFIED),
        ("Private", PRIVATE),
        ("Public", PUBLIC),
        ("Campaign", CAMPAIGN),
        ("Named", principals(FieldClass.PARTICIPANTS, [ana])),
        ("Character", principals(FieldClass.CHARACTERS, [linked])),
        ("Group", principals(FieldClass.GROUPS, [group])),
    ):
        alias = _add(world, campaign, target, term)
        if eligibility != UNCLASSIFIED:
            assert _classify(world, campaign, alias, eligibility)

    def terms(who):
        return {alias.term for alias in _allowed(world, campaign, who)}

    assert terms(None) == {"Public"}
    assert terms(cy) == {"Public", "Campaign"}
    assert terms(ana) == {"Public", "Campaign", "Named", "Character", "Group"}
    assert terms(removed) == set()
    assert world.mutations().remove_member(campaign, group, ana).changed
    with world.db.transaction() as unit:
        assert world.documents.unlink_character_sheet(unit, campaign, linked)
    assert terms(ana) == {"Public", "Campaign", "Named"}


def test_persona_links_and_identical_campaign_names_stay_isolated(world: World) -> None:
    first, second = _campaign(world), _campaign(world)
    persona = _document(world, first, DocumentTypeId.NPC)
    identity = _document(world, first, DocumentTypeId.NPC)
    duplicate = _document(world, second, DocumentTypeId.NPC)
    a = _add(world, first, persona, "Stranger", pronunciation="private-hint-canary")
    b = _add(world, second, duplicate, "Stranger")
    assert _classify(world, first, a, PUBLIC)
    assert _classify(world, second, b, PUBLIC)
    with world.db.transaction() as unit:
        assert INDEX.link_identity(unit, first, persona, identity, owner_id=world.owner)
    before = _index(world, first)
    with world.db.transaction() as unit:
        assert not INDEX.link_identity(unit, first, persona, identity, owner_id=world.owner)
        assert INDEX.gm_index(unit, first, owner_id=world.owner + 100) is None
    assert _index(world, first) == before
    assert before.identities == (PersonaLink(persona, identity),)
    assert _index(world, second).identities == ()
    (table_first,) = _allowed(world, first, None, term="stranger")
    (table_second,) = _allowed(world, second, None, term="stranger")
    assert asdict(table_first) == {"id": a, "document_id": persona, "term": "Stranger"}
    assert asdict(table_second) == {"id": b, "document_id": duplicate, "term": "Stranger"}
    assert identity not in repr(table_first) and "private-hint-canary" not in repr(table_first)
    assert "private-hint-canary" not in repr(before)

    with pytest.raises(MissingParent), world.db.transaction() as unit:
        INDEX.link_identity(unit, first, persona, duplicate, owner_id=world.owner)
    with pytest.raises(MissingParent), world.db.transaction() as unit:
        INDEX.register_alias(unit, first, duplicate, owner_id=world.owner, term="Foreign")
    with pytest.raises(MissingParent), world.db.transaction() as unit:
        INDEX.register_alias(unit, first, persona, owner_id=world.owner + 100, term="Foreign")


def test_alias_narrowing_is_committed_once_and_rejects_foreign_targets(world: World) -> None:
    campaign, foreign = _campaign(world), _campaign(world)
    target = _document(world, campaign, DocumentTypeId.NPC)
    seat = _seat(world, foreign)
    alias = _add(world, campaign, target, "The Stranger")
    assert _classify(world, campaign, alias, PUBLIC)
    before = _index(world, campaign)
    with pytest.raises(InvalidPrincipal):
        _classify(world, campaign, alias, principals(FieldClass.PARTICIPANTS, [seat]))
    with pytest.raises(MissingParent):
        _classify(world, campaign, uuid4(), PUBLIC)
    assert _index(world, campaign) == before

    with world.db.transaction() as writer:
        assert INDEX.classify_alias(writer, campaign, alias, UNCLASSIFIED, owner_id=world.owner)
        assert _allowed(world, campaign, None)[0].id == alias
        assert INDEX.eligible_aliases(writer, campaign, None) == ()
    assert _allowed(world, campaign, None) == ()
    assert _index(world, campaign).policy_revision == before.policy_revision + 1

    with pytest.raises(ValueError, match="different documents"), world.db.transaction() as unit:
        INDEX.link_identity(unit, campaign, target, target, owner_id=world.owner)
    for malformed in ("", "\x00", "\u200d", "multiple\nlines"):
        with pytest.raises(ValueError):
            _add(world, campaign, target, malformed)

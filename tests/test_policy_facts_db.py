"""Verify that both stores load the policy oracle's facts in one read."""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db
from _policy_worlds import write
from fastapi import HTTPException
from psycopg_pool import PoolTimeout
from test_eligibility_db import World, _campaign, _document, _get, _group, _seat
from test_eligibility_db import dsn as dsn
from test_eligibility_db import world as world

from service import campaign_identity as ident
from service import policy
from service.campaign_store import shared_rows
from service.campaigns_api import guarded
from service.db import PgTransaction
from service.eligibility_store import (
    GM_ONLY_BY_CONSTRUCTION,
    QUEUE_PAGE_MAX,
    UNCLASSIFIED,
    ClassificationSource,
    Eligibility,
    FieldClass,
    _EligibilityRow,
)
from service.policy_facts import FACTS_DOCUMENTS_MAX, InMemoryPolicyFacts, PolicyUnavailable, PostgresPolicyFacts
from service.tests import pdp_fixtures as pdp
from service.tests.policy_oracle import (
    Release,
    eligible_for_audience,
    gen_eligibility_case,
    gen_world,
)


def _loader(world: World) -> InMemoryPolicyFacts | PostgresPolicyFacts:
    if world.kind == "fake":
        return InMemoryPolicyFacts(world.db, documents=world.documents)
    return PostgresPolicyFacts()


def _load(world: World, campaign: str, *documents: str) -> policy.PolicyFacts:
    with world.db.transaction() as unit:
        return _loader(world).load(unit, campaign, documents)


def _plant(world: World, campaign: str, doc: str, key: str, classification: Eligibility) -> None:
    """Plant a permitted storage shape, including orphan and stale target ids."""
    with world.db.transaction() as unit:
        if world.kind == "fake":
            table = shared_rows(world.db, "field_eligibility")
            table.add(unit, f"{doc}\x1f{key}", _EligibilityRow(doc, campaign, classification))
        else:
            assert classification.source is not None
            unit.conn.execute(
                """INSERT INTO campaign.field_eligibility
                       (campaign_id, document_id, field_key, eligibility_class, principal_ids, classification_source)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    campaign, doc, key, classification.field_class.value, list(classification.ids) or None,
                    classification.source.value,
                ),
            )


def _classification(kind: FieldClass, *ids: str) -> Eligibility:
    return Eligibility(kind, tuple(sorted(ids)), ClassificationSource.GM)


def test_loads_only_the_requested_campaign_even_with_foreign_references(world: World) -> None:
    a, b = _campaign(world), _campaign(world)
    a_doc, b_doc = _document(world, a), _document(world, b)
    a_seat, b_seat = _seat(world, a), _seat(world, b)
    a_group, b_group = _group(world, a, members=(a_seat,)), _group(world, b, members=(b_seat,))
    with world.db.transaction() as unit:
        assert world.documents.link_character_sheet(unit, a, a_doc, participant_id=a_seat)
        assert world.documents.link_character_sheet(unit, b, b_doc, participant_id=b_seat)

    groups = _classification(FieldClass.GROUPS, a_group, b_group)
    sheets = _classification(FieldClass.CHARACTERS, a_doc, b_doc)
    for campaign, doc in ((a, a_doc), (b, b_doc)):
        _plant(world, campaign, doc, "name", groups)
        _plant(world, campaign, doc, "class", sheets)
    facts = _load(world, a, a_doc, b_doc)
    assert facts.campaign_id == a
    assert set(facts.documents) == {a_doc}
    assert facts.classes == {(a_doc, "name"): groups, (a_doc, "class"): sheets}
    assert facts.sheet_links == {a_doc: a_seat}
    assert facts.active_participants == {a_seat}
    assert facts.group_members == {a_group: frozenset({a_seat})}
    assert facts.revealable is policy.registry_revealable


def test_excludes_removed_seats_groups_memberships_and_reset_classes(world: World) -> None:
    campaign = _campaign(world)
    doc = _document(world, campaign)
    active, removed, left = (_seat(world, campaign, alias) for alias in ("Rook", "Fox", "Owl"))
    live_group = _group(world, campaign, members=(active, removed, left))
    gone_group = _group(world, campaign, "Gone", members=(active,))
    empty_group = _group(world, campaign, "Empty")
    _plant(world, campaign, doc, "name", _classification(FieldClass.GROUPS, live_group, gone_group, empty_group))
    _plant(world, campaign, doc, "hp", _classification(FieldClass.PUBLIC))
    record = _get(world, campaign, doc)
    with world.db.transaction() as unit:
        unit.lock_campaign(campaign, shared=False)
        assert world.eligibility.remove_member(unit, campaign, live_group, left)
        assert world.eligibility.remove_group(unit, campaign, gone_group)
        assert world.participants.remove(unit, campaign, removed)
        world.eligibility.put(unit, record, "hp", UNCLASSIFIED)
    facts = _load(world, campaign, doc)
    assert facts.active_participants == {active, left}
    assert facts.group_members == {live_group: frozenset({active}), empty_group: frozenset()}
    assert (doc, "hp") not in facts.classes


def test_deleted_documents_are_absent_and_archived_links_survive(world: World) -> None:
    campaign = _campaign(world)
    doc, deleted, archived = (_document(world, campaign) for _ in range(3))
    seat = _seat(world, campaign)
    _plant(world, campaign, doc, "name", _classification(FieldClass.CHARACTERS, deleted, archived))
    _plant(world, campaign, deleted, "name", _classification(FieldClass.PUBLIC))
    with world.db.transaction() as unit:
        assert world.documents.link_character_sheet(unit, campaign, archived, participant_id=seat)
        assert world.documents.set_archived(unit, campaign, archived, archived=True)
        assert world.documents.delete(unit, campaign, deleted)

    facts = _load(world, campaign, doc, deleted, archived, ident.new_id(ident.DOCUMENT))
    assert set(facts.documents) == {doc, archived}
    assert set(facts.classes) == {(doc, "name")}
    assert facts.sheet_links == {archived: seat}


def test_keeps_orphan_classes_for_the_policy_to_decide(world: World) -> None:
    campaign = _campaign(world)
    doc = _document(world, campaign)
    public = _classification(FieldClass.PUBLIC)
    _plant(world, campaign, doc, "old_field", public)
    facts = _load(world, campaign, doc)
    assert facts.classes == {(doc, "old_field"): public}
    assert policy.eligible_for_audience(facts, doc, "old_field", None).reason is policy.EligReason.NOT_REVEALABLE


@pytest.mark.parametrize("world", ["fake"], indirect=True)
def test_twin_counts_unreadable_rows_without_logging_identifiers(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    campaign = _campaign(world)
    doc = _document(world, campaign)
    with world.db.transaction() as unit:
        table = shared_rows(world.db, "field_eligibility")
        for key in ("name", "class"):
            row = _EligibilityRow(doc, campaign, _classification(FieldClass.PUBLIC))
            object.__setattr__(row.eligibility, "source", "unrecognized")
            table.add(unit, f"{doc}\x1f{key}", row)
    facts = _load(world, campaign, doc)
    assert all(facts.classes[(doc, key)] is GM_ONLY_BY_CONSTRUCTION for key in ("name", "class"))
    assert [r.getMessage() for r in caplog.records] == ["policy_class_unreadable count=2"]


class _CountingConnection:
    def __init__(self, conn: Any = None):
        self.conn = conn
        self.statements = 0

    def execute(self, *args: Any):
        self.statements += 1
        return self.conn.execute(*args)


def test_validates_batch_before_touching_the_connection() -> None:
    conn = _CountingConnection()
    unit = PgTransaction(conn)
    loader = PostgresPolicyFacts()
    campaign, doc = ident.new_id(ident.CAMPAIGN), ident.new_id(ident.DOCUMENT)
    for picked_campaign, ids in (
        (campaign, ()),
        (campaign, (doc,) * (FACTS_DOCUMENTS_MAX + 1)),
        (campaign, ("wrong",)),
        (campaign, doc),
        (campaign, (None,)),
        ("bad-campaign", (doc,)),
    ):
        with pytest.raises(ValueError):
            loader.load(unit, picked_campaign, ids)
    assert conn.statements == 0
    assert FACTS_DOCUMENTS_MAX == QUEUE_PAGE_MAX == 500


@pytest.mark.parametrize("world", [pytest.param("postgres", marks=needs_db)], indirect=True)
def test_one_statement_in_the_callers_transaction(world: World) -> None:
    campaign = _campaign(world)
    doc = _document(world, campaign)
    with connect(world.dsn) as conn, conn.transaction():
        proxy = _CountingConnection(conn)
        unit = PgTransaction(proxy)
        unit.lock_campaign(campaign, shared=True)
        before = proxy.statements
        facts = PostgresPolicyFacts().load(unit, campaign, (doc,) * FACTS_DOCUMENTS_MAX)
    assert set(facts.documents) == {doc}
    assert proxy.statements == before + 1


@pytest.mark.parametrize("world", [pytest.param("postgres", marks=needs_db)], indirect=True)
def test_takes_no_lock_on_any_table_it_reads(world: World) -> None:
    campaign = _campaign(world)
    doc, sheet = _document(world, campaign), _document(world, campaign)
    seat = _seat(world, campaign)
    group = _group(world, campaign, members=(seat,))
    _plant(world, campaign, doc, "class", _classification(FieldClass.CHARACTERS, sheet))
    _plant(world, campaign, doc, "name", _classification(FieldClass.GROUPS, group))
    with connect(world.dsn) as locked, connect(world.dsn) as reading, locked.transaction(), reading.transaction():
        for stmt in (
            "SELECT id FROM campaign.documents WHERE campaign_id = %s FOR UPDATE",
            "SELECT document_id FROM campaign.field_eligibility WHERE campaign_id = %s FOR UPDATE",
            "SELECT id FROM campaign.participants WHERE campaign_id = %s FOR UPDATE",
            "SELECT id FROM campaign.groups WHERE campaign_id = %s FOR UPDATE",
            "SELECT group_id FROM campaign.group_members WHERE campaign_id = %s FOR UPDATE",
        ):
            locked.execute(stmt, (campaign,)).fetchall()
        locked.execute(
            """INSERT INTO campaign.field_eligibility
                   (campaign_id, document_id, field_key, eligibility_class, classification_source)
               VALUES (%s, %s, 'old_field', 'public', 'gm')""",
            (campaign, doc),
        )
        reading.execute("SET LOCAL lock_timeout = '200ms'")
        facts = PostgresPolicyFacts().load(PgTransaction(reading), campaign, (doc,))
    assert set(facts.classes) == {(doc, "name"), (doc, "class")}
    assert facts.group_members == {group: {seat}}
    assert facts.sheet_links == {sheet: None}


def _assert_unavailable(work) -> None:
    with pytest.raises(HTTPException) as response:
        guarded(work)
    assert response.value.status_code == 503
    assert response.value.detail["code"] == "backend_unavailable"
    assert response.value.detail["retryable"] is True


@pytest.mark.parametrize(
    "error",
    [psycopg.errors.QueryCanceled, psycopg.OperationalError, PoolTimeout, psycopg.ProgrammingError],
)
def test_driver_failure_is_a_503_and_only_outages_are_retryable(error, caplog: pytest.LogCaptureFixture) -> None:
    class FailingConnection:
        def execute(self, *_args):
            raise error("private driver context")

    campaign, doc = ident.new_id(ident.CAMPAIGN), ident.new_id(ident.DOCUMENT)

    def work():
        return PostgresPolicyFacts().load(PgTransaction(FailingConnection()), campaign, (doc,))

    expected = error if error is psycopg.ProgrammingError else PolicyUnavailable
    with pytest.raises(expected) as failed:
        work()
    if expected is PolicyUnavailable:
        assert failed.value.__context__ is None and failed.value.__cause__ is None
        assert str(failed.value) == "Campaign policy is temporarily unavailable"
    _assert_unavailable(work)
    assert "private driver context" not in caplog.text


@needs_db
def test_closed_connection(dsn: str) -> None:
    conn = connect(dsn)
    conn.close()
    campaign, doc = ident.new_id(ident.CAMPAIGN), ident.new_id(ident.DOCUMENT)
    with pytest.raises(PolicyUnavailable) as failed:
        PostgresPolicyFacts().load(PgTransaction(conn), campaign, (doc,))
    assert failed.value.__context__ is None and failed.value.__cause__ is None


@needs_db
def test_policy_matches_the_independent_oracle_in_postgres(dsn: str) -> None:
    loader = PostgresPolicyFacts()
    compared = 0
    with connect(dsn) as conn:
        for seed in range(10**6, 10**6 + 200):
            release = Release.WORKBENCH_V1 if seed % 3 == 0 else Release.ASSISTANT
            state = gen_world(random.Random(seed), release)
            fixture = write(conn, state)
            ids = [fixture.document(d) for d in state.world.documents]
            facts = loader.load(PgTransaction(conn), fixture.campaign_id, ids or [ident.new_id(ident.DOCUMENT)])
            assert set(facts.documents) == set(ids), f"seed {seed}"
            facts = replace(facts, revealable=pdp.fixture_revealable)
            rng = random.Random(seed + 7)
            for _ in range(25):
                doc, key, audience = gen_eligibility_case(rng, state)
                participant = pdp.audience(audience)
                if participant is not None:
                    participant += fixture.suffix
                expected = eligible_for_audience(state.world, doc, key, audience)
                result = policy.eligible_for_audience(facts, fixture.document(doc), key, participant)
                assert (result.allowed, result.reason.name) == (expected.allowed, expected.reason.name), f"seed {seed}"
                compared += 1
    assert compared >= 5000

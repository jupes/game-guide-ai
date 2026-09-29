"""Persisting a generated document (1kg.5.4, group H), in both worlds.

`persist_generated` runs inside the caller's unit and writes nothing of its
own, so "failures do not create partial docs" is a property of that unit: a
rollback after the write leaves no document and no version, in the twin and
in PostgreSQL alike, and another connection never sees an uncommitted one.
The shared tests run twice through the `world` fixture; its `postgres`
parameter carries `needs_db` and skips without DATABASE_URL, which CI sets
for this file (`.github/workflows/ci.yml`, pinned by
`service/tests/test_ci_workflow.py`).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database

from service import document_generation as dg
from service import migrations as mig
from service.campaign_store import InMemoryCampaignStore, MissingParent, PostgresCampaignStore, fake
from service.db import CampaignLockSettings, Database, InMemoryDatabase, PoolSettings
from service.document_store import InMemoryDocumentStore, PostgresDocumentStore
from service.tests import document_generation_fixtures as fx
from service.workbench_contracts import Author, DocumentTypeId

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
MISSING_CAMPAIGN = "cmp_" + "z" * 22
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("docgen") as target:
        mig.migrate(target)
        yield target


@dataclass
class World:
    kind: str
    db: Any
    campaigns: Any
    documents: Any
    owner: int


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db: Any = InMemoryDatabase()
        yield World("fake", db, InMemoryCampaignStore(db), InMemoryDocumentStore(db), owner=1)
        return
    target = request.getfixturevalue("dsn")
    with connect(target) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    database = Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5), QUICK)
    yield World("postgres", database, PostgresCampaignStore(), PostgresDocumentStore(), owner=int(owner))


def test_the_shared_tests_really_run_against_both_worlds() -> None:
    """A lost `postgres` parameter would make every test below fake-only and green."""
    params = world._fixture_function_marker.params
    assert params is not None
    assert [p if isinstance(p, str) else p.values[0] for p in params] == ["fake", "postgres"]
    postgres = params[1]
    assert not isinstance(postgres, str) and [m.name for m in postgres.marks] == ["skipif"]


def _campaign(world: World) -> str:
    with world.db.transaction() as unit:
        return str(world.campaigns.create(unit, owner_id=world.owner, name="Nocturne").id)


def _generated(doc_type: DocumentTypeId = fx.NPC, cited: bool = False) -> dg.GeneratedDocument:
    corpus = (fx.passage(1),) if cited else ()
    output = fx.envelope(fx.BASE_FIELDS[doc_type], [1] if cited else [])
    return dg.parse_generated(fx.request(doc_type, corpus=corpus), output, finish_reason="stop")


def _counts(world: World) -> tuple[int, int]:
    with world.db.transaction() as unit:
        if world.kind == "postgres":
            documents = unit.conn.execute("SELECT count(*) FROM campaign.documents").fetchone()[0]
            versions = unit.conn.execute("SELECT count(*) FROM campaign.document_versions").fetchone()[0]
            return int(documents), int(versions)
        twin = fake(unit)
        return len(world.documents._documents.visible(twin)), len(world.documents._versions.visible(twin))


def _stored(world: World, document_id: str) -> tuple[str | None, str]:
    """The created command id and version 1's summary, as the world stored them."""
    with world.db.transaction() as unit:
        if world.kind == "postgres":
            row = unit.conn.execute(
                "SELECT d.created_command_id, v.summary FROM campaign.documents d "
                "JOIN campaign.document_versions v ON v.document_id = d.id AND v.number = 1 WHERE d.id = %s",
                (document_id,),
            ).fetchone()
            return row[0], row[1]
        twin = fake(unit)
        stored = world.documents._documents.visible(twin)[document_id]
        versions = [v for v in world.documents._versions.visible(twin).values() if v.version.document_id == document_id]
        return stored.created_command_id, versions[0].version.summary


@pytest.mark.parametrize(("doc_type", "cited"), [(fx.NPC, True), (fx.ENCOUNTER, False), (fx.NOTES, False)])
def test_h1_a_generated_document_is_a_sealed_assistant_version_one(
    world: World, doc_type: DocumentTypeId, cited: bool
) -> None:
    """Kills the `gm` author, a command id passed, and the summary dropped."""
    campaign = _campaign(world)
    generated = _generated(doc_type, cited)
    with world.db.transaction() as unit:
        record = dg.persist_generated(unit, world.documents, campaign, generated, now=NOW)
    assert (record.type, record.type_version, record.data) == (doc_type.value, 1, generated.data)
    assert record.version.author == Author.ASSISTANT.value and record.version.sealed_at is not None
    assert record.version.number == 1 and record.version.changed_fields == tuple(sorted(generated.data))
    summary = dg.version_summary(generated.provenance)
    assert record.version.summary == summary and _stored(world, record.id) == (None, summary)
    assert _counts(world) == (1, 1)
    if world.kind == "postgres":
        with world.db.transaction() as unit:
            touched = [
                unit.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("campaign.reveal_disclosures", "campaign.reveal_slots", "audit.events", "app.jobs")
            ]
        assert touched == [0, 0, 0, 0], "X-2: nothing is revealed, audited or queued"


def test_h2_a_rollback_after_the_persist_leaves_nothing(world: World) -> None:
    """Kills a write outside the caller's unit."""
    campaign = _campaign(world)

    class SettleFailed(Exception):
        pass

    with pytest.raises(SettleFailed), world.db.transaction() as unit:
        dg.persist_generated(unit, world.documents, campaign, _generated(), now=NOW)
        raise SettleFailed
    assert _counts(world) == (0, 0)


def test_h3_a_missing_campaign_writes_nothing_and_leaves_the_transaction_usable(world: World) -> None:
    """Kills a write before the parent check."""
    with world.db.transaction() as unit:
        with pytest.raises(MissingParent):
            dg.persist_generated(unit, world.documents, MISSING_CAMPAIGN, _generated(), now=NOW)
        if world.kind == "postgres":
            assert unit.conn.execute("SELECT 1").fetchone()[0] == 1
    assert _counts(world) == (0, 0)


def test_h4_the_store_never_deduplicates_a_generated_create(world: World) -> None:
    """Kills a hidden command id: exactly-once is the caller's fence."""
    campaign = _campaign(world)
    generated = _generated()
    made = []
    for _ in range(2):
        with world.db.transaction() as unit:
            made.append(dg.persist_generated(unit, world.documents, campaign, generated, now=NOW).id)
    assert len(set(made)) == 2 and _counts(world) == (2, 2)


@pytest.mark.parametrize("summary", ["s" * 201, "a\x00b", "a\u202eb"])
def test_h5_create_refuses_a_bad_summary_before_any_statement(world: World, summary: str) -> None:
    """Kills `check_summary` not called in `_minted`."""
    campaign = _campaign(world)
    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="version summary"):
            world.documents.create(
                unit, campaign, doc_type=fx.NPC, type_version=1, data={"name": "Vashti"},
                author=Author.ASSISTANT, summary=summary, now=NOW,
            )
        if world.kind == "postgres":
            assert unit.conn.execute("SELECT 1").fetchone()[0] == 1
    assert _counts(world) == (0, 0)


def test_h5_create_without_a_summary_still_writes_an_empty_one(world: World) -> None:
    """Kills the route's default changed."""
    campaign = _campaign(world)
    with world.db.transaction() as unit:
        record = world.documents.create(
            unit, campaign, doc_type=fx.NPC, type_version=1, data={"name": "Vashti"}, author=Author.GM, now=NOW
        )
    assert record.version.summary == "" and _stored(world, record.id) == (None, "")
    with world.db.transaction() as unit:
        longest = world.documents.create(
            unit, campaign, doc_type=fx.NPC, type_version=1, data={"name": "Wren"}, author=Author.GM,
            summary="s" * 200, now=NOW,
        )
    assert _stored(world, longest.id)[1] == "s" * 200


@needs_db
def test_h6_an_uncommitted_persist_is_invisible_and_a_rollback_leaves_nothing(dsn: str) -> None:
    """Kills a separate autocommit write (two connections, PostgreSQL only)."""
    database = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5), QUICK)
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    campaigns, documents = PostgresCampaignStore(), PostgresDocumentStore()
    with database.transaction() as unit:
        campaign = campaigns.create(unit, owner_id=int(owner), name="Nocturne").id

    def seen() -> int:
        with connect(dsn) as other:
            return int(other.execute("SELECT count(*) FROM campaign.documents").fetchone()[0])

    class Abandoned(Exception):
        pass

    with pytest.raises(Abandoned), database.transaction() as unit:
        made = dg.persist_generated(unit, documents, campaign, _generated(), now=NOW)
        own = unit.conn.execute("SELECT count(*) FROM campaign.documents WHERE id = %s", (made.id,)).fetchone()[0]
        assert own == 1, "the writer sees its own row"
        assert seen() == 0, "another connection never sees an uncommitted document"
        raise Abandoned
    assert seen() == 0

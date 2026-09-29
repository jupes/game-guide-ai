"""Field eligibility, groups and the revision they advance (1ir.2.1), in both
worlds, and the races that only PostgreSQL can answer.

The shared suite runs twice — once against the in-memory twin, once against a
real PostgreSQL — through the `world` fixture, whose `postgres` parameter
carries `needs_db`, so without a server it skips and the twin still runs. The
two-connection tests below it are PostgreSQL's alone: who waits for whom on the
campaign lock and on the group and seat rows, what a second connection sees
before a commit, and the honest "not applied yet" of a narrowing's step 2.

Every test names the mutation it kills (the brief's section 10).

Requires DATABASE_URL for the PostgreSQL half (CI sets it for this file,
`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`):

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_eligibility_db.py -q
"""

from __future__ import annotations

import secrets
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service.campaign_store import (
    InMemoryCampaignStore,
    MissingParent,
    PostgresCampaignStore,
    shared_rows,
)
from service.db import (
    CampaignLockNotHeld,
    CampaignLockSettings,
    Database,
    InMemoryDatabase,
    PoolSettings,
    ProjectionItem,
)
from service.document_store import DocumentRecord, InMemoryDocumentStore, PostgresDocumentStore
from service.eligibility import (
    Change,
    ChangeOperation,
    ChangeRecord,
    ClassMoved,
    EligibilityMutations,
    EligibilityStores,
    NotAppliedYet,
    add_member_locked,
    classify_locked,
)
from service.eligibility_store import (
    GM_ONLY_BY_CONSTRUCTION,
    GROUPS_PER_CAMPAIGN_MAX,
    UNCLASSIFIED,
    ClassificationSource,
    Eligibility,
    FieldClass,
    GroupLimit,
    GroupNameTaken,
    InMemoryEligibilityStore,
    InvalidPrincipal,
    NotClassifiable,
    PostgresEligibilityStore,
    _EligibilityRow,
    principals,
)
from service.participant_store import InMemoryParticipantStore, PostgresParticipantStore
from service.table_session_store import InMemoryTableSessionStore, PostgresTableSessionStore, no_slots
from service.workbench_contracts import COMMON_FIELDS, DOC_TYPE_FIELDS, Author, DocumentTypeId, revealable_fields

NOW = datetime(2030, 1, 1, tzinfo=UTC)
#: The interleaving tests wait for a THREAD, so the lock wait is four seconds,
#: still under the five-second gate.
PATIENT = CampaignLockSettings(lock_timeout_s=4, transaction_timeout_s=30)
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
PATIENCE = 15
GM = ClassificationSource.GM
PUBLIC = Eligibility(FieldClass.PUBLIC, (), GM)
CAMPAIGN_WIDE = Eligibility(FieldClass.CAMPAIGN, (), GM)
GM_ONLY = Eligibility(FieldClass.GM_ONLY, (), GM)
#: Minimal valid data per type: a statblock needs `ac` and `hp`.
DATA: dict[DocumentTypeId, dict[str, Any]] = {t: {"name": "Rook"} for t in DocumentTypeId} | {
    DocumentTypeId.STATBLOCK: {"name": "Ogre", "ac": 11, "hp": 59}
}


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("elig") as target:
        mig.migrate(target)
        yield target


def _database(dsn: str, settings: CampaignLockSettings = QUICK) -> Database:
    return Database(dsn, PoolSettings(sync_max=6, async_max=0, acquire_timeout_s=5), settings)


@dataclass
class World:
    """The stores over one database, an owner, and what the two extension
    points were called with."""

    kind: str
    db: Any
    campaigns: Any
    participants: Any
    documents: Any
    sessions: Any
    eligibility: Any
    owner: int
    dsn: str | None = None
    #: `(unit, record, revision seen inside the unit)` per recorder call.
    records: list[tuple[Any, ChangeRecord]] = field(default_factory=list)
    #: `(unit, campaign, document, key, campaign locks, revision)` per narrowing.
    narrowed: list[tuple[Any, str, str, str, list[tuple[str, str]], int | None]] = field(default_factory=list)

    def table_namespace(self, unit: Any, campaign_id: str, document_id: str, field_key: str) -> None:
        self.narrowed.append(
            (unit, campaign_id, document_id, field_key, list(unit.campaign_locks),
             self.campaigns.authz_revision(unit, campaign_id))
        )

    def record(self, unit: Any, record: ChangeRecord) -> None:
        self.records.append((unit, record))

    def stores(self) -> EligibilityStores:
        return EligibilityStores(self.eligibility, self.documents, self.sessions, self.table_namespace, self.record)

    def mutations(self, db: Any = None) -> EligibilityMutations:
        return EligibilityMutations(
            self.db if db is None else db,
            eligibility=self.eligibility,
            documents=self.documents,
            sessions=self.sessions,
            table_namespace=self.table_namespace,
            record=self.record,
        )


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db: Any = InMemoryDatabase()
        documents = InMemoryDocumentStore(db)
        yield World(
            "fake",
            db,
            InMemoryCampaignStore(db),
            InMemoryParticipantStore(db),
            documents,
            InMemoryTableSessionStore(db, slot_clear=no_slots),
            InMemoryEligibilityStore(db, documents=documents),
            owner=1,
        )
        return
    target = request.getfixturevalue("dsn")
    with connect(target) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    yield World(
        "postgres",
        _database(target),
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresDocumentStore(),
        PostgresTableSessionStore(slot_clear=no_slots),
        PostgresEligibilityStore(),
        owner=int(owner),
        dsn=target,
    )


# ── Seeding ──────────────────────────────────────────────────────────────────


def _campaign(world: World) -> str:
    with world.db.transaction() as unit:
        return world.campaigns.create(unit, owner_id=world.owner, name="Nocturne").id


def _seat(world: World, campaign_id: str, alias: str = "Rook") -> str:
    with world.db.transaction() as unit:
        return world.participants.add(unit, campaign_id, alias=alias).id


def _remove_seat(world: World, campaign_id: str, seat: str) -> None:
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign_id, seat)


def _document(world: World, campaign_id: str, doc_type: DocumentTypeId = DocumentTypeId.CHARACTER_SHEET) -> str:
    with world.db.transaction() as unit:
        return world.documents.create(
            unit, campaign_id, doc_type=doc_type, type_version=1, data=dict(DATA[doc_type]), author=Author.GM
        ).id


def _get(world: World, campaign_id: str, document_id: str) -> DocumentRecord:
    with world.db.transaction() as unit:
        found = world.documents.get(unit, campaign_id, document_id)
    assert found is not None
    return found


def _revisions(world: World, campaign_id: str) -> tuple[int | None, int | None]:
    with world.db.transaction() as unit:
        return world.campaigns.authz_revision(unit, campaign_id), world.campaigns.projection_revision(unit, campaign_id)


def _queue(world: World, campaign_id: str) -> list[tuple[str, str, int]]:
    with world.db.transaction() as unit:
        items = world.eligibility.queued_projections(unit, campaign_id, limit=500)
    return [(item.document_id, item.field_key, item.authz_revision) for item in items]


def _of(world: World, campaign_id: str, document_id: str, key: str) -> Eligibility:
    record = _get(world, campaign_id, document_id)
    with world.db.transaction() as unit:
        return world.eligibility.eligibility_of(unit, record, key)


@contextmanager
def _locked(world: World, campaign_id: str, *, shared: bool = False) -> Iterator[Any]:
    with world.db.transaction() as unit:
        unit.lock_campaign(campaign_id, shared=shared)
        yield unit


def _group(world: World, campaign_id: str, name: str = "Scouts", members: tuple[str, ...] = ()) -> str:
    mutations = world.mutations()
    group = mutations.create_group(campaign_id, name=name)
    for seat in members:
        mutations.add_member(campaign_id, group.id, seat)
    return group.id


def _live_session(world: World, campaign_id: str) -> str:
    with world.db.transaction() as unit:
        return world.sessions.start(
            unit, campaign_id, owner_id=world.owner, expires_at=datetime.now(UTC) + timedelta(hours=11),
            command_id=secrets.token_urlsafe(16),
        ).id


def _epoch(world: World, campaign_id: str) -> int:
    with world.db.transaction() as unit:
        live = world.sessions.live_session_for_campaign(unit, campaign_id)
    assert live is not None
    return int(live.reveal_epoch)


# ── The helper: rule 3 and the queue against the database (T-C) ─────────────


@pytest.mark.parametrize(
    ("start", "items", "expected"),
    [
        pytest.param((4, 4), 0, (5, 5, 0), id="nothing-pending-moves-with-it"),
        pytest.param((4, 4), 1, (5, 4, 1), id="items-leave-it-behind"),
        pytest.param((5, 3), 0, (6, 3, 0), id="a-lagging-one-never-catches-up"),
    ],
)
def test_rule_three_moves_the_projection_only_when_nothing_was_pending(world, start, items, expected):
    """T-C2 in both worlds: kills M-C2, M-C3 and M-C4."""
    campaign = _campaign(world)
    sheet = _document(world, campaign)
    with _locked(world, campaign) as unit:
        for _ in range(start[0]):
            unit.advance_authz_revision(campaign, project=[ProjectionItem(sheet, "hp")])
    with _locked(world, campaign) as unit:
        # The queue so far says (n, 0); the projector's catch-up is 1ir.2.3's,
        # so the test sets the starting projection the way the projector will.
        if world.kind == "fake":
            unit._stage_projection(campaign, start[1])
        else:
            unit.conn.execute(
                "UPDATE campaign.authz_state SET projection_revision = %s WHERE campaign_id = %s",
                (start[1], campaign),
            )
    before = len(_queue(world, campaign))
    with _locked(world, campaign) as unit:
        project = [ProjectionItem(sheet, "portrait")] * items
        assert unit.advance_authz_revision(campaign, project=project) == expected[0]
    assert _revisions(world, campaign) == expected[:2]
    after = _queue(world, campaign)
    assert len(after) - before == expected[2]
    if expected[2]:
        assert after[-1] == (sheet, "portrait", expected[0])


def test_a_rolled_back_advance_leaves_both_revisions_and_no_queue_item(world):
    """T-C4 in both worlds: kills M-C5."""
    campaign = _campaign(world)
    sheet = _document(world, campaign)
    with pytest.raises(RuntimeError):
        with _locked(world, campaign) as unit:
            unit.advance_authz_revision(campaign, project=[ProjectionItem(sheet, "hp")])
            raise RuntimeError("the caller's next statement failed")
    assert _revisions(world, campaign) == (0, 0)
    assert _queue(world, campaign) == []


@needs_db
def test_the_previous_releases_advance_leaves_work_for_the_projector(dsn):
    """T-C3: the old helper's statement leaves (n+1, n) and an empty queue, and
    the new helper, with no items, does not catch it up. Kills M-C2."""
    world_db = _database(dsn)
    campaigns = PostgresCampaignStore()
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    with world_db.transaction() as unit:
        campaign = campaigns.create(unit, owner_id=int(owner), name="Nocturne").id
    with world_db.transaction() as unit:
        unit.lock_campaign(campaign, shared=False)
        unit.conn.execute(
            "UPDATE campaign.authz_state SET authz_revision = authz_revision + 1 WHERE campaign_id = %s",
            (campaign,),
        )
    with world_db.transaction() as unit:
        assert (campaigns.authz_revision(unit, campaign), campaigns.projection_revision(unit, campaign)) == (1, 0)
        unit.lock_campaign(campaign, shared=False)
        assert unit.advance_authz_revision(campaign) == 2
    with world_db.transaction() as unit:
        assert (campaigns.authz_revision(unit, campaign), campaigns.projection_revision(unit, campaign)) == (2, 0)
        assert unit.conn.execute("SELECT count(*) FROM campaign.projection_queue").fetchone() == (0,)


@needs_db
def test_a_second_reader_sees_neither_revision_nor_queue_item_before_commit(dsn):
    """T-C5 in PostgreSQL, two connections: kills M-C6's server twin."""
    database = _database(dsn)
    campaigns, documents = PostgresCampaignStore(), PostgresDocumentStore()
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    with database.transaction() as unit:
        campaign = campaigns.create(unit, owner_id=int(owner), name="Nocturne").id
        sheet = documents.create(
            unit, campaign, doc_type=DocumentTypeId.CHARACTER_SHEET, type_version=1, data={"name": "Rook"},
            author=Author.GM,
        ).id

    def seen() -> tuple:
        with connect(dsn) as reader:
            return (
                reader.execute(
                    "SELECT authz_revision, projection_revision FROM campaign.authz_state WHERE campaign_id = %s",
                    (campaign,),
                ).fetchone(),
                reader.execute("SELECT count(*) FROM campaign.projection_queue").fetchone()[0],
            )

    with database.transaction() as unit:
        unit.lock_campaign(campaign, shared=False)
        unit.advance_authz_revision(campaign, project=[ProjectionItem(sheet, "hp")])
        assert seen() == ((0, 0), 0)
    assert seen() == ((1, 0), 1)


# ── Evaluation (T-D1 to T-D4) ────────────────────────────────────────────────


@pytest.mark.parametrize("doc_type", list(DocumentTypeId), ids=lambda t: t.value)
def test_a_revealable_field_without_a_row_is_unclassified(world, doc_type):
    """T-D1, the AC: every revealable key of every type, with no row, is
    unclassified. Kills a default of gm_only or public (M-D1)."""
    campaign = _campaign(world)
    record = _get(world, campaign, _document(world, campaign, doc_type))
    keys = revealable_fields(doc_type)
    assert keys, "every type has something revealable"
    with world.db.transaction() as unit:
        for key in keys:
            assert world.eligibility.eligibility_of(unit, record, key) == UNCLASSIFIED, key
        assert world.eligibility.eligibilities_of(unit, record) == dict.fromkeys(sorted(keys), UNCLASSIFIED)


def test_a_field_off_the_allowlist_is_gm_only_by_construction_and_never_counted_unclassified(world):
    """T-D2: kills M-D2 (off-allowlist answers unclassified) and M-D3
    (`eligibilities_of` counts declared-but-unrevealable keys)."""
    campaign = _campaign(world)
    record = _get(world, campaign, _document(world, campaign, DocumentTypeId.NPC))
    with world.db.transaction() as unit:
        for key in ("tags", "true_identity", "nonesuch", "all", "a.b", "Name"):
            assert world.eligibility.eligibility_of(unit, record, key) == GM_ONLY_BY_CONSTRUCTION, key
        listed = world.eligibility.eligibilities_of(unit, record)
    assert set(listed) == set(revealable_fields(DocumentTypeId.NPC))
    assert "tags" not in listed and "true_identity" not in listed


def test_an_orphan_row_is_ignored_by_evaluation(world):
    """T-D3: a row for `tags` (off every allowlist) says public; evaluation
    never reads it. Kills reading the row before the allowlist (M-D4)."""
    campaign = _campaign(world)
    sheet = _document(world, campaign)
    if world.kind == "postgres":
        with connect(world.dsn) as conn:
            conn.execute(
                "INSERT INTO campaign.field_eligibility (document_id, field_key, campaign_id, eligibility_class, "
                "classification_source) VALUES (%s, 'tags', %s, 'public', 'gm')",
                (sheet, campaign),
            )
    else:
        with world.db.transaction() as unit:
            orphan = _EligibilityRow(sheet, campaign, PUBLIC)
            shared_rows(world.db, "field_eligibility").add(unit, f"{sheet}\x1ftags", orphan)
    record = _get(world, campaign, sheet)
    with world.db.transaction() as unit:
        assert world.eligibility.eligibility_of(unit, record, "tags") == GM_ONLY_BY_CONSTRUCTION
        assert "tags" not in world.eligibility.eligibilities_of(unit, record)


def _stale(world: World, campaign_id: str, document_id: str) -> None:
    """Move the stored document to a type version this build does not know."""
    if world.kind == "postgres":
        with connect(world.dsn) as conn:
            conn.execute("UPDATE campaign.documents SET type_version = 2 WHERE id = %s", (document_id,))
        return
    table = shared_rows(world.db, "documents")
    with world.db.transaction() as unit:
        row = table.visible(unit)[document_id]
        table.replace(unit, document_id, replace(row, type_version=2))


def test_an_unknown_type_version_evaluates_gm_only_and_refuses_classification(world):
    """T-D4: kills a type version that is never compared (M-D5)."""
    campaign = _campaign(world)
    sheet = _document(world, campaign)
    world.mutations().classify(campaign, sheet, "hp", expected=UNCLASSIFIED, new=PUBLIC)
    _stale(world, campaign, sheet)
    record = _get(world, campaign, sheet)
    with world.db.transaction() as unit:
        assert world.eligibility.eligibility_of(unit, record, "hp") == GM_ONLY_BY_CONSTRUCTION
        assert world.eligibility.eligibilities_of(unit, record) == {}
    with pytest.raises(NotClassifiable):
        world.mutations().classify(campaign, sheet, "hp", expected=GM_ONLY_BY_CONSTRUCTION, new=GM_ONLY)


# ── Writes (T-D5 to T-D12) ───────────────────────────────────────────────────


@pytest.mark.parametrize("doc_type", list(DocumentTypeId), ids=lambda t: t.value)
def test_put_accepts_exactly_the_revealable_keys_of_each_type(world, doc_type):
    """T-D5: every declared key of every type; revealable keys are stored, the
    rest and the wildcards refused before any statement, and the transaction
    stays usable. Kills M-D6 (declared instead of revealable) and M-D7."""
    campaign = _campaign(world)
    record = _get(world, campaign, _document(world, campaign, doc_type))
    declared = {**COMMON_FIELDS, **DOC_TYPE_FIELDS[doc_type]}
    revealable = set(revealable_fields(doc_type))
    with _locked(world, campaign) as unit:
        for key in [*sorted(declared), "all", "a.b", "*"]:
            if key in revealable:
                world.eligibility.put(unit, record, key, PUBLIC)
            else:
                with pytest.raises(NotClassifiable):
                    world.eligibility.put(unit, record, key, PUBLIC)
        stored = world.eligibility.eligibilities_of(unit, record)
    assert stored == dict.fromkeys(sorted(revealable), PUBLIC)


_WRITES: dict[str, Callable[[World, Any, str, DocumentRecord, str, str], Any]] = {
    "put": lambda w, u, c, d, g, s: w.eligibility.put(u, d, "hp", PUBLIC),
    "check_principals": lambda w, u, c, d, g, s: w.eligibility.check_principals(
        u, c, principals(FieldClass.PARTICIPANTS, [s])
    ),
    "create_group": lambda w, u, c, d, g, s: w.eligibility.create_group(u, c, name="Watch"),
    "remove_group": lambda w, u, c, d, g, s: w.eligibility.remove_group(u, c, g),
    "add_member": lambda w, u, c, d, g, s: w.eligibility.add_member(u, c, g, s),
    "remove_member": lambda w, u, c, d, g, s: w.eligibility.remove_member(u, c, g, s),
}


@pytest.mark.parametrize("write", sorted(_WRITES))
@pytest.mark.parametrize("shared", [None, True], ids=["no-lock", "shared-lock"])
def test_eligibility_and_group_writes_need_the_exclusive_campaign_lock(world, write, shared):
    """T-D6: kills a guard missing on any one write (M-D8)."""
    campaign = _campaign(world)
    seat = _seat(world, campaign)
    record = _get(world, campaign, _document(world, campaign))
    group = _group(world, campaign, members=(seat,))
    with world.db.transaction() as unit:
        if shared:
            unit.lock_campaign(campaign, shared=True)
        with pytest.raises(CampaignLockNotHeld):
            _WRITES[write](world, unit, campaign, record, group, seat)


def test_rename_group_takes_no_campaign_lock(world):
    campaign = _campaign(world)
    group = _group(world, campaign)
    with world.db.transaction() as unit:
        renamed = world.eligibility.rename_group(unit, campaign, group, name="Wardens")
        assert unit.campaign_locks == []
    assert renamed.name == "Wardens"


def test_resetting_to_unclassified_deletes_the_row(world):
    """T-D7: kills a stored 'unclassified' or a row left behind (M-D9)."""
    campaign = _campaign(world)
    sheet = _document(world, campaign)
    record = _get(world, campaign, sheet)
    with _locked(world, campaign) as unit:
        world.eligibility.put(unit, record, "hp", PUBLIC)
    with _locked(world, campaign) as unit:
        world.eligibility.put(unit, record, "hp", UNCLASSIFIED)
    assert _of(world, campaign, sheet, "hp") == UNCLASSIFIED
    if world.kind == "postgres":
        with connect(world.dsn) as conn:
            assert conn.execute("SELECT count(*) FROM campaign.field_eligibility").fetchone() == (0,)
    else:
        rows = shared_rows(world.db, "field_eligibility")
        with world.db.transaction() as unit:
            assert all(row.gone for row in rows.visible(unit).values())


def test_a_principal_list_names_only_live_principals_of_this_campaign(world):
    """T-D8: every failure is the one refusal, whose message names no id.
    Kills M-D10 (removed seat), M-D11 (document type) and M-D12 (foreign)."""
    campaign, other = _campaign(world), _campaign(world)
    seat, gone, foreign_seat = _seat(world, campaign), _seat(world, campaign, "Wren"), _seat(world, other)
    _remove_seat(world, campaign, gone)
    sheet, lore = _document(world, campaign), _document(world, campaign, DocumentTypeId.LORE)
    foreign_sheet = _document(world, other)
    group, dropped, foreign_group = _group(world, campaign), _group(world, campaign, "Old"), _group(world, other)
    world.mutations().remove_group(campaign, dropped)
    bad = [
        (FieldClass.PARTICIPANTS, [seat, gone]),
        (FieldClass.PARTICIPANTS, [seat, foreign_seat]),
        (FieldClass.PARTICIPANTS, ["prt_" + "z" * 22]),
        (FieldClass.CHARACTERS, [foreign_sheet]),
        (FieldClass.CHARACTERS, [lore]),
        (FieldClass.CHARACTERS, ["doc_" + "z" * 22]),
        (FieldClass.GROUPS, [dropped]),
        (FieldClass.GROUPS, [foreign_group]),
    ]
    messages = set()
    with _locked(world, campaign) as unit:
        for field_class, ids in bad:
            with pytest.raises(InvalidPrincipal) as refused:
                world.eligibility.check_principals(unit, campaign, principals(field_class, ids))
            messages.add(str(refused.value))
            assert not any(value in str(refused.value) for value in ids)
        for good in (
            principals(FieldClass.PARTICIPANTS, [seat]),
            principals(FieldClass.CHARACTERS, [sheet]),
            principals(FieldClass.GROUPS, [group]),
            PUBLIC,
        ):
            world.eligibility.check_principals(unit, campaign, good)
    assert messages == {InvalidPrincipal.MESSAGE}


def test_ids_round_trip_sorted(world):
    """T-D9's store half: a list is stored and read back in canonical order."""
    campaign = _campaign(world)
    seats = sorted([_seat(world, campaign, alias) for alias in ("Rook", "Wren", "Ash")], reverse=True)
    sheet = _document(world, campaign)
    listed = principals(FieldClass.PARTICIPANTS, seats)
    assert listed.ids == tuple(sorted(seats))
    world.mutations().classify(campaign, sheet, "hp", expected=UNCLASSIFIED, new=listed)
    assert _of(world, campaign, sheet, "hp") == listed


def test_the_sentinel_is_never_stored_and_the_transaction_stays_usable(world):
    """Critic item 2: `put(..., GM_ONLY_BY_CONSTRUCTION)` is refused before any
    statement, and the same transaction still writes."""
    campaign = _campaign(world)
    sheet = _document(world, campaign)
    record = _get(world, campaign, sheet)
    with _locked(world, campaign) as unit:
        with pytest.raises(ValueError, match="never stored"):
            world.eligibility.put(unit, record, "hp", GM_ONLY_BY_CONSTRUCTION)
        world.eligibility.put(unit, record, "hp", GM_ONLY)
    assert _of(world, campaign, sheet, "hp") == GM_ONLY


def test_members_and_groups_of_exclude_removed_seats_and_removed_groups(world):
    """T-D10 with critic item 8: the membership row outlives a seat's removal
    and is never read for it; a removed group has no members and is nobody's;
    `list_groups` is live groups only, while `get_group` still answers."""
    campaign = _campaign(world)
    rook, wren = _seat(world, campaign), _seat(world, campaign, "Wren")
    scouts = _group(world, campaign, "Scouts", (rook, wren))
    watch = _group(world, campaign, "Watch", (rook,))
    _remove_seat(world, campaign, wren)
    with world.db.transaction() as unit:
        assert world.eligibility.members(unit, campaign, scouts) == {rook}
        assert world.eligibility.groups_of(unit, campaign, wren) == frozenset()
        assert world.eligibility.groups_of(unit, campaign, rook) == {scouts, watch}
    revision = _revisions(world, campaign)[0]
    assert world.mutations().remove_member(campaign, scouts, wren) == Change(True, revision + 1)
    world.mutations().remove_group(campaign, watch)
    with world.db.transaction() as unit:
        assert world.eligibility.members(unit, campaign, watch) == frozenset()
        assert world.eligibility.groups_of(unit, campaign, rook) == {scouts}
        assert [g.id for g in world.eligibility.list_groups(unit, campaign)] == [scouts]
        assert world.eligibility.get_group(unit, campaign, watch).removed_at is not None


def test_queued_projections_page_by_id(world):
    """T-D11: keyset strictly after the id, ascending, bounded. Kills `>=`
    (M-D15)."""
    campaign = _campaign(world)
    sheet = _document(world, campaign)
    with _locked(world, campaign) as unit:
        unit.advance_authz_revision(campaign, project=[ProjectionItem(sheet, key) for key in ("ac", "hp", "speed")])
    with world.db.transaction() as unit:
        first = world.eligibility.queued_projections(unit, campaign, limit=2)
        rest = world.eligibility.queued_projections(unit, campaign, after_id=first[-1].id, limit=500)
        for after_id, limit in ((0, 0), (0, 501), (-1, 1)):
            with pytest.raises(ValueError):
                world.eligibility.queued_projections(unit, campaign, after_id=after_id, limit=limit)
    assert [q.field_key for q in first] == ["ac", "hp"]
    assert [q.field_key for q in rest] == ["speed"]
    assert first[0].id < first[1].id < rest[0].id


def test_an_eligibility_row_for_another_campaigns_document_is_refused(world):
    """T-D12: a record whose campaign is not its document's is `MissingParent`
    in both worlds, as the composite foreign key refuses it (M-D16)."""
    campaign, other = _campaign(world), _campaign(world)
    record = _get(world, campaign, _document(world, campaign))
    forged = replace(record, campaign_id=other)
    with _locked(world, other) as unit:
        for value in (PUBLIC, UNCLASSIFIED):
            with pytest.raises(MissingParent):
                world.eligibility.put(unit, forged, "hp", value)


def test_deleting_a_document_takes_its_eligibility_rows_and_queue_items(world):
    """T-A8 in both worlds (critic item 9(c))."""
    campaign = _campaign(world)
    sheet, kept = _document(world, campaign), _document(world, campaign)
    for document_id in (sheet, kept):
        world.mutations().classify(campaign, document_id, "hp", expected=UNCLASSIFIED, new=PUBLIC)
    record = _get(world, campaign, sheet)
    with world.db.transaction() as unit:
        assert world.documents.delete(unit, campaign, sheet)
    with world.db.transaction() as unit:
        assert world.eligibility.eligibility_of(unit, record, "hp") == UNCLASSIFIED
        assert world.eligibility.eligibilities_of(unit, record)["hp"] == UNCLASSIFIED
    assert [item[0] for item in _queue(world, campaign)] == [kept]
    assert _of(world, campaign, kept, "hp") == PUBLIC


# ── Groups (T-E1 to T-E5) ────────────────────────────────────────────────────


def test_a_group_name_is_unique_among_live_groups_by_the_application_key(world):
    """T-E1: kills a non-partial index (M-E1) and a raw lower() (M-E2); in
    PostgreSQL the transaction survives through the savepoint."""
    campaign = _campaign(world)
    scouts = _group(world, campaign, "Scouts")
    with _locked(world, campaign) as unit:
        for clash in ("SCOUTS", "Sc‍outs", " Scouts "):
            with pytest.raises(GroupNameTaken):
                world.eligibility.create_group(unit, campaign, name=clash)
        watch = world.eligibility.create_group(unit, campaign, name="Watch")
    with world.db.transaction() as unit:
        with pytest.raises(GroupNameTaken):
            world.eligibility.rename_group(unit, campaign, watch.id, name="scouts")
        assert world.eligibility.rename_group(unit, campaign, watch.id, name="WATCH").name == "WATCH"
    world.mutations().remove_group(campaign, scouts)
    assert world.mutations().create_group(campaign, name="SCOUTS").name == "SCOUTS"


def test_create_group_replays_its_command_id(world):
    """T-E2 with critic item 16: a replay answers the original, whatever name
    it sends and even at the cap; a malformed id is refused before any
    statement. Kills M-E3."""
    campaign = _campaign(world)
    command = secrets.token_urlsafe(16)
    mutations = world.mutations()
    first = mutations.create_group(campaign, name="Scouts", command_id=command)
    for n in range(GROUPS_PER_CAMPAIGN_MAX - 1):
        mutations.create_group(campaign, name=f"Group {n}")
    assert mutations.create_group(campaign, name="Different", command_id=command) == first
    with pytest.raises(GroupLimit):
        mutations.create_group(campaign, name="One more")
    with _locked(world, campaign) as unit:
        with pytest.raises(ValueError, match="command id"):
            world.eligibility.create_group(unit, campaign, name="Watch", command_id="short")
        assert world.eligibility.get_group(unit, campaign, first.id) == first


def test_the_fifty_first_live_group_is_refused_and_a_removed_one_does_not_count(world):
    """T-E3: kills a cap that counts removed groups or none (M-E4)."""
    campaign = _campaign(world)
    mutations = world.mutations()
    ids = [mutations.create_group(campaign, name=f"Group {n}").id for n in range(GROUPS_PER_CAMPAIGN_MAX)]
    with pytest.raises(GroupLimit):
        mutations.create_group(campaign, name="Fifty-one")
    mutations.remove_group(campaign, ids[0])
    assert mutations.create_group(campaign, name="Fifty-one").name == "Fifty-one"
    with world.db.transaction() as unit:
        assert len(world.eligibility.list_groups(unit, campaign)) == GROUPS_PER_CAMPAIGN_MAX


CANARY = "CanaryéName"


def _nothing_carries_the_canary(error: BaseException) -> None:
    rendered = "".join(traceback.format_exception(error))
    for text in (str(error), repr(error), str(error.__cause__), str(error.__context__), rendered):
        assert CANARY.casefold() not in text.casefold()


def test_a_group_name_never_reaches_a_repr_or_a_refusal(world):
    """T-E4 with critic item 6: kills `repr=True` on the name and a message that
    interpolates it (M-E5), and a refusal raised inside the driver's handler."""
    campaign = _campaign(world)
    mutations = world.mutations()
    group = mutations.create_group(campaign, name=CANARY)
    assert CANARY not in repr(group)
    other = mutations.create_group(campaign, name="Watch")
    attempts: list[Callable[[], Any]] = [
        lambda: mutations.create_group(campaign, name=CANARY.upper()),
        lambda: mutations.rename_group(campaign, other.id, name=CANARY),
        lambda: mutations.create_group(campaign, name=CANARY + "‮"),
        lambda: mutations.rename_group(campaign, other.id, name=CANARY * 4),
    ]
    for attempt in attempts:
        with pytest.raises((GroupNameTaken, ValueError)) as refused:
            attempt()
        _nothing_carries_the_canary(refused.value)
    with world.db.transaction() as unit:
        assert CANARY not in repr(world.eligibility.list_groups(unit, campaign))


@pytest.mark.parametrize("bad", ["Sc\x00outs", "Sc‮outs", "Sc​outs", "", "x" * 41])
def test_a_group_name_with_control_or_formatting_characters_is_refused(world, bad):
    """T-E5: kills `check_alias` bypassed (M-E6)."""
    campaign = _campaign(world)
    with pytest.raises(ValueError, match="group name"):
        world.mutations().create_group(campaign, name=bad)
    with world.db.transaction() as unit:
        assert world.eligibility.list_groups(unit, campaign) == []


# ── The mutations (T-F) ──────────────────────────────────────────────────────


class Recording:
    """A `TransactionalDatabase` over the world's, recording in order, per
    transaction, every call to the unit's lock and revision helper and every
    call to the eligibility store — then delegating. The unit handed on is the
    real one (the stores check its type), with those two methods wrapped."""

    def __init__(self, world: World, probe: Callable[[str], None] | None = None) -> None:
        self.world = world
        self.probe = probe
        self.units: list[Any] = []
        self.calls: list[tuple[int, str, tuple, dict]] = []

    def index(self, unit: Any) -> int:
        return next(n for n, seen in enumerate(self.units) if seen is unit)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.world.db.transaction() as unit:
            self.units.append(unit)
            number = len(self.units) - 1
            for name in ("lock_campaign", "advance_authz_revision"):
                original = getattr(unit, name)

                def wrapped(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
                    self.calls.append((number, _name, args, kwargs))
                    if _name == "advance_authz_revision" and self.probe is not None:
                        self.probe(args[0])
                    return _original(*args, **kwargs)

                setattr(unit, name, wrapped)
            yield unit

    def store(self) -> Any:
        recording = self

        class _Store:
            def __getattr__(self, name: str) -> Any:
                attribute = getattr(recording.world.eligibility, name)

                def call(unit: Any, *args: Any, **kwargs: Any) -> Any:
                    recording.calls.append((recording.index(unit), f"store.{name}", args, kwargs))
                    return attribute(unit, *args, **kwargs)

                return call

        return _Store()

    def mutations(self) -> EligibilityMutations:
        return EligibilityMutations(
            self,
            eligibility=self.store(),
            documents=self.world.documents,
            sessions=self.world.sessions,
            table_namespace=self.world.table_namespace,
            record=self.world.record,
        )


@dataclass
class Table:
    campaign: str
    seat: str
    sheet: str
    group: str


def _table(world: World) -> Table:
    campaign = _campaign(world)
    seat = _seat(world, campaign)
    sheet = _document(world, campaign)
    group = _group(world, campaign, members=(seat,))
    world.mutations().classify(campaign, sheet, "ac", expected=UNCLASSIFIED, new=PUBLIC)
    world.records.clear()
    world.narrowed.clear()
    return Table(campaign, seat, sheet, group)


#: Each mutation the AC names that this bead implements: (the store write it
#: must make, how many transactions it opens, the call).
MUTATIONS: dict[str, tuple[str, int, Callable[[EligibilityMutations, Table, World], Change]]] = {
    "classify-widen": (
        "store.put", 1,
        lambda m, t, w: m.classify(t.campaign, t.sheet, "hp", expected=UNCLASSIFIED, new=CAMPAIGN_WIDE),
    ),
    "classify-narrow": (
        "store.put", 1,
        lambda m, t, w: m.classify(t.campaign, t.sheet, "ac", expected=PUBLIC, new=GM_ONLY),
    ),
    "classify-reset": (
        "store.put", 1,
        lambda m, t, w: m.classify(t.campaign, t.sheet, "ac", expected=PUBLIC, new=UNCLASSIFIED),
    ),
    "add-member": (
        "store.add_member", 1,
        lambda m, t, w: m.add_member(t.campaign, t.group, _seat(w, t.campaign, "Wren")),
    ),
    "remove-member": ("store.remove_member", 2, lambda m, t, w: m.remove_member(t.campaign, t.group, t.seat)),
    "remove-group": ("store.remove_group", 2, lambda m, t, w: m.remove_group(t.campaign, t.group)),
}


def _probe(world: World, campaign: str, seen: list[tuple[str, int]]) -> Callable[[str], None]:
    """PostgreSQL's half of T-F1, run just before the advance delegates: a
    second connection cannot share-lock the row, and still reads the old
    revision."""

    def probe(campaign_id: str) -> None:
        assert campaign_id == campaign
        assert world.dsn is not None
        with connect(world.dsn) as other:
            other.execute("SET lock_timeout = '100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                other.execute(
                    "SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR SHARE", (campaign_id,)
                )
            seen.append(("before", other.execute(
                "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (campaign_id,)
            ).fetchone()[0]))

    return probe


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_mutation_implemented_here_advances_the_revision_once_in_its_own_locked_transaction(world, name):
    """T-F1, the AC, on the PUBLIC path (critic item 4): a widening opens one
    transaction and a narrowing two, the first of which takes no campaign
    lock; in the mutating transaction the first lock is the exclusive one, and
    the store write and exactly one advance are on that same unit. In
    PostgreSQL a second connection, probing just before the advance, cannot
    share-lock the row and reads the old revision; afterwards it reads +1.
    Kills M-F1 (advance in a second transaction), M-F2 (a shared lock) and
    M-F3 (the advance omitted in any one mutation)."""
    table = _table(world)
    write, transactions, run = MUTATIONS[name]
    before = _revisions(world, table.campaign)[0]
    assert before is not None
    seen: list[tuple[str, int]] = []
    recording = Recording(world, _probe(world, table.campaign, seen) if world.kind == "postgres" else None)
    change = run(recording.mutations(), table, world)
    assert change == Change(True, before + 1)
    assert _revisions(world, table.campaign)[0] == before + 1
    assert len(recording.units) == transactions
    if transactions == 2:
        assert recording.units[0].campaign_locks == []
        assert not any(call[0] == 0 and call[1] == "lock_campaign" for call in recording.calls)
    mutating = transactions - 1
    mine = [call for call in recording.calls if call[0] == mutating]
    assert mine[0][1:] == ("lock_campaign", (table.campaign,), {"shared": False})
    assert [call[1] for call in mine].count("advance_authz_revision") == 1
    assert write in [call[1] for call in mine]
    assert [call[1] for call in mine].index(write) < [call[1] for call in mine].index("advance_authz_revision")
    assert [(world_unit is recording.units[mutating], r.authz_revision) for world_unit, r in world.records] == [
        (True, before + 1)
    ]
    if world.kind == "postgres":
        assert seen == [("before", before)]


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_a_recorder_that_raises_rolls_the_whole_change_back(world, name):
    """Critic item 12: the record is written in the mutating transaction, so a
    failing recorder leaves the fact and the revision where they were."""
    table = _table(world)
    _, _, run = MUTATIONS[name]
    before = _revisions(world, table.campaign)
    ac, members = _of(world, table.campaign, table.sheet, "ac"), None
    with world.db.transaction() as unit:
        members = world.eligibility.members(unit, table.campaign, table.group)

    def refuse(unit: Any, record: ChangeRecord) -> None:
        raise RuntimeError("the audit write failed")

    world.record = refuse  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        run(world.mutations(), table, world)
    assert _revisions(world, table.campaign) == before
    assert _of(world, table.campaign, table.sheet, "ac") == ac
    with world.db.transaction() as unit:
        assert world.eligibility.members(unit, table.campaign, table.group) == members
        assert world.eligibility.get_group(unit, table.campaign, table.group).is_live


def test_a_mutation_that_changes_nothing_advances_nothing(world):
    """T-F2 with critic item 8: the same class, an existing member, a non-member
    and an already-removed group. Kills M-F4."""
    table = _table(world)
    mutations = world.mutations()
    before = _revisions(world, table.campaign)
    assert mutations.classify(table.campaign, table.sheet, "ac", expected=PUBLIC, new=PUBLIC) == Change(False)
    assert mutations.add_member(table.campaign, table.group, table.seat) == Change(False)
    wren = _seat(world, table.campaign, "Wren")
    assert mutations.remove_member(table.campaign, table.group, wren) == Change(False)
    assert _revisions(world, table.campaign) == before
    mutations.remove_group(table.campaign, table.group)
    after = _revisions(world, table.campaign)
    assert mutations.remove_group(table.campaign, table.group) == Change(False)
    assert _revisions(world, table.campaign) == after
    assert [r.operation for _, r in world.records] == [ChangeOperation.GROUP_REMOVED]


def test_create_and_rename_group_advance_nothing_and_rename_takes_no_campaign_lock(world):
    """T-F3: kills M-F5."""
    campaign = _campaign(world)
    before = _revisions(world, campaign)
    recording = Recording(world)
    group = recording.mutations().create_group(campaign, name="Scouts", command_id=secrets.token_urlsafe(16))
    recording.mutations().rename_group(campaign, group.id, name="Wardens")
    recording.mutations().rename_group(campaign, group.id, name="Wardens")
    assert _revisions(world, campaign) == before
    assert [call[1] for call in recording.calls if call[1] == "advance_authz_revision"] == []
    assert recording.calls[0][1:] == ("lock_campaign", (campaign,), {"shared": False})
    assert all(unit.campaign_locks == [] for unit in recording.units[1:])
    assert [r.operation for _, r in world.records] == [ChangeOperation.GROUP_CREATED, ChangeOperation.GROUP_RENAMED]
    assert all(r.authz_revision is None for _, r in world.records)


def test_a_classification_whose_displayed_class_moved_writes_nothing(world):
    """T-F4 with critic items 2 and 3: `ClassMoved`; a non-GM source; and an
    off-allowlist key answers `NotClassifiable` whatever `expected` says. Row,
    revision and queue are unchanged each time. Kills M-F6."""
    table = _table(world)
    mutations = world.mutations()
    before, queued = _revisions(world, table.campaign), _queue(world, table.campaign)
    with pytest.raises(ClassMoved):
        mutations.classify(table.campaign, table.sheet, "ac", expected=UNCLASSIFIED, new=GM_ONLY)
    with pytest.raises(ValueError, match="GM's"):
        mutations.classify(
            table.campaign, table.sheet, "ac", expected=PUBLIC,
            new=Eligibility(FieldClass.GM_ONLY, (), ClassificationSource.DEFAULT),
        )
    for expected in (UNCLASSIFIED, GM_ONLY_BY_CONSTRUCTION):
        with pytest.raises(NotClassifiable):
            mutations.classify(table.campaign, table.sheet, "tags", expected=expected, new=PUBLIC)
    with pytest.raises(NotClassifiable):
        mutations.classify(table.campaign, table.sheet, "all", expected=UNCLASSIFIED, new=PUBLIC)
    with pytest.raises(InvalidPrincipal):
        mutations.classify(
            table.campaign, table.sheet, "ac", expected=UNCLASSIFIED,
            new=principals(FieldClass.PARTICIPANTS, ["prt_" + "z" * 22]),
        )
    assert _of(world, table.campaign, table.sheet, "ac") == PUBLIC
    assert (_revisions(world, table.campaign), _queue(world, table.campaign)) == (before, queued)
    assert world.records == [] and world.narrowed == []


def test_a_classification_that_admits_anyone_queues_the_field_and_leaves_the_projection_behind(world):
    """T-F5, first half: kills a wrong `project` decision (M-F7)."""
    table = _table(world)
    authz, projection = _revisions(world, table.campaign)
    queued = _queue(world, table.campaign)
    groups = principals(FieldClass.GROUPS, [table.group])
    world.mutations().classify(table.campaign, table.sheet, "hp", expected=UNCLASSIFIED, new=groups)
    assert _revisions(world, table.campaign) == (authz + 1, projection)
    assert _queue(world, table.campaign) == [*queued, (table.sheet, "hp", authz + 1)]


def test_a_classification_to_gm_only_or_unclassified_queues_nothing_and_obeys_rule_three(world):
    """T-F5, second half. The table's classification left the projection
    behind, so rule 3 keeps it there; once caught up, it moves with it."""
    table = _table(world)
    mutations = world.mutations()
    authz, projection = _revisions(world, table.campaign)
    queued = _queue(world, table.campaign)
    mutations.classify(table.campaign, table.sheet, "ac", expected=PUBLIC, new=GM_ONLY)
    assert _revisions(world, table.campaign) == (authz + 1, projection)
    with _locked(world, table.campaign) as unit:
        if world.kind == "fake":
            unit._stage_projection(table.campaign, authz + 1)
        else:
            unit.conn.execute(
                "UPDATE campaign.authz_state SET projection_revision = authz_revision WHERE campaign_id = %s",
                (table.campaign,),
            )
    mutations.classify(table.campaign, table.sheet, "ac", expected=GM_ONLY, new=UNCLASSIFIED)
    assert _revisions(world, table.campaign) == (authz + 2, authz + 2)
    assert _queue(world, table.campaign) == queued


@pytest.mark.parametrize(
    ("key", "expected", "new"),
    [
        pytest.param("hp", UNCLASSIFIED, PUBLIC, id="widen"),
        pytest.param("ac", PUBLIC, GM_ONLY, id="narrow"),
        pytest.param("ac", PUBLIC, UNCLASSIFIED, id="reset"),
    ],
)
def test_every_class_change_calls_the_table_namespace_extension_under_the_lock_before_the_advance(
    world, key, expected, new
):
    """T-F6: kills the extension skipped on a widening, or called after the
    advance (M-F8)."""
    table = _table(world)
    before = _revisions(world, table.campaign)[0]
    world.mutations().classify(table.campaign, table.sheet, key, expected=expected, new=new)
    assert [entry[1:] for entry in world.narrowed] == [
        (table.campaign, table.sheet, key, [(table.campaign, "exclusive")], before)
    ]


@pytest.mark.parametrize("operation", ["remove_member", "remove_group"])
def test_removing_a_member_narrows_first_without_the_campaign_lock(world, operation):
    """T-F7: with a live session, step 1 takes no campaign lock and advances the
    reveal epoch; step 2 narrows again and advances the revision. With no live
    session, nothing is narrowed. Kills M-F9 and M-F10."""
    table = _table(world)
    _live_session(world, table.campaign)
    epoch = _epoch(world, table.campaign)
    before = _revisions(world, table.campaign)[0]
    epochs: list[int] = []
    original = world.sessions.narrow

    def spy(unit: Any, campaign_id: str, session_id: str, **kwargs: Any) -> Any:
        epochs.append(len(unit.campaign_locks))
        return original(unit, campaign_id, session_id, **kwargs)

    world.sessions.narrow = spy  # type: ignore[method-assign]
    recording = Recording(world)
    mutations = recording.mutations()
    if operation == "remove_member":
        mutations.remove_member(table.campaign, table.group, table.seat)
    else:
        mutations.remove_group(table.campaign, table.group)
    assert recording.units[0].campaign_locks == []
    assert epochs == [0, 1], "step 1 narrows with no campaign lock; step 2 narrows under it"
    assert _epoch(world, table.campaign) == epoch + 2
    assert _revisions(world, table.campaign)[0] == before + 1


def test_a_narrowing_with_no_live_session_narrows_nothing(world):
    table = _table(world)
    calls: list[Any] = []
    world.sessions.narrow = lambda *args, **kwargs: calls.append(args)  # type: ignore[method-assign]
    world.mutations().remove_member(table.campaign, table.group, table.seat)
    assert calls == []


def test_adding_a_member_displays_nothing_and_narrows_nothing(world):
    """T-F10: kills a narrowing on a widening (M-F14)."""
    table = _table(world)
    _live_session(world, table.campaign)
    epoch = _epoch(world, table.campaign)
    world.mutations().add_member(table.campaign, table.group, _seat(world, table.campaign, "Wren"))
    assert _epoch(world, table.campaign) == epoch


def test_a_campaign_that_is_gone_is_missing_parent(world):
    """T-F11: every mutation, on a campaign that never existed. Kills M-F15."""
    ghost = "cmp_" + "g" * 22
    mutations = world.mutations()
    attempts: list[Callable[[], Any]] = [
        lambda: mutations.classify(ghost, "doc_" + "g" * 22, "hp", expected=UNCLASSIFIED, new=PUBLIC),
        lambda: mutations.create_group(ghost, name="Scouts"),
        lambda: mutations.rename_group(ghost, "grp_" + "g" * 22, name="Scouts"),
        lambda: mutations.add_member(ghost, "grp_" + "g" * 22, "prt_" + "g" * 22),
        lambda: mutations.remove_member(ghost, "grp_" + "g" * 22, "prt_" + "g" * 22),
        lambda: mutations.remove_group(ghost, "grp_" + "g" * 22),
    ]
    for attempt in attempts:
        with pytest.raises(MissingParent):
            attempt()


def test_classifying_a_missing_foreign_or_deleted_document_changes_nothing(world):
    """Critic item 9(d): `MissingParent`, and both revisions and the queue are
    unchanged."""
    table = _table(world)
    other = _campaign(world)
    foreign = _document(world, other)
    deleted = _document(world, table.campaign)
    with world.db.transaction() as unit:
        assert world.documents.delete(unit, table.campaign, deleted)
    before, queued = _revisions(world, table.campaign), _queue(world, table.campaign)
    for document_id in ("doc_" + "z" * 22, foreign, deleted):
        with pytest.raises(MissingParent):
            world.mutations().classify(table.campaign, document_id, "hp", expected=UNCLASSIFIED, new=PUBLIC)
    assert (_revisions(world, table.campaign), _queue(world, table.campaign)) == (before, queued)


def test_a_member_of_a_missing_foreign_or_removed_group_or_seat_is_missing_parent(world):
    """Critic item 8's edges for add and remove."""
    table = _table(world)
    other = _campaign(world)
    foreign_seat, foreign_group = _seat(world, other), _group(world, other)
    gone = _seat(world, table.campaign, "Wren")
    _remove_seat(world, table.campaign, gone)
    mutations = world.mutations()
    for group, seat in ((foreign_group, table.seat), (table.group, foreign_seat), ("grp_" + "z" * 22, table.seat)):
        for attempt in (mutations.add_member, mutations.remove_member):
            with pytest.raises(MissingParent):
                attempt(table.campaign, group, seat)
    with pytest.raises(MissingParent):
        mutations.add_member(table.campaign, table.group, gone)
    assert mutations.remove_member(table.campaign, table.group, gone) == Change(False)


def test_a_removed_seats_membership_can_still_be_removed(world):
    """Critic item 8: remove_member deletes the row of a removed seat (True)."""
    table = _table(world)
    _remove_seat(world, table.campaign, table.seat)
    assert world.mutations().remove_member(table.campaign, table.group, table.seat).changed


def test_the_locked_bodies_compose_into_a_callers_transaction(world):
    """`classify_locked` and `add_member_locked` are public for `1ir.11.1`: in
    a transaction the caller owns, they lock, change and advance there."""
    table = _table(world)
    before = _revisions(world, table.campaign)[0]
    wren = _seat(world, table.campaign, "Wren")
    with world.db.transaction() as unit:
        classify_locked(unit, world.stores(), table.campaign, table.sheet, "hp", expected=UNCLASSIFIED, new=PUBLIC)
        assert unit.campaign_locks == [(table.campaign, "exclusive")]
    with world.db.transaction() as unit:
        add_member_locked(unit, world.stores(), table.campaign, table.group, wren)
    assert _revisions(world, table.campaign)[0] == before + 2


# ── PostgreSQL only: who waits for whom ──────────────────────────────────────


def _someone_waits_on_a_lock(dsn: str) -> bool:
    deadline = time.monotonic() + PATIENCE
    with connect(dsn) as conn:
        while time.monotonic() < deadline:
            blocked = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if blocked:
                return True
            time.sleep(0.05)
    return False


def _in_thread(work: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = work()
        except BaseException as error:  # noqa: BLE001 - the test inspects it
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


@pytest.fixture
def pg_world(dsn: str) -> World:
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    return World(
        "postgres",
        _database(dsn, PATIENT),
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresDocumentStore(),
        PostgresTableSessionStore(slot_clear=no_slots),
        PostgresEligibilityStore(),
        owner=int(owner),
        dsn=dsn,
    )


@needs_db
def test_a_narrowing_that_cannot_get_the_lock_is_answered_not_applied_yet(pg_world):
    """T-F8 with critic item 5: step 1 commits (the epoch moves), step 2 is
    tried once and answers `NotAppliedYet` within 2 x the lock timeout + 1 s;
    the fact and the revision are unchanged and no job exists. Kills M-F11."""
    world = pg_world
    table = _table(world)
    _live_session(world, table.campaign)
    epoch = _epoch(world, table.campaign)
    before = _revisions(world, table.campaign)
    quick = _database(world.dsn, QUICK)
    assert world.dsn is not None
    with connect(world.dsn, autocommit=False) as holder:
        holder.execute("SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR UPDATE", (table.campaign,))
        started = time.monotonic()
        with pytest.raises(NotAppliedYet) as refused:
            world.mutations(quick).remove_member(table.campaign, table.group, table.seat)
        assert time.monotonic() - started < 2 * QUICK.lock_timeout_s + 1
        holder.rollback()
    assert refused.value.__context__ is None and refused.value.__cause__ is None
    assert _epoch(world, table.campaign) == epoch + 1
    assert _revisions(world, table.campaign) == before
    with world.db.transaction() as unit:
        assert world.eligibility.members(unit, table.campaign, table.group) == {table.seat}
        assert unit.conn.execute("SELECT count(*) FROM app.jobs").fetchone() == (0,)


@needs_db
def test_two_classifications_of_one_field_serialise_and_the_second_finds_the_class_moved(pg_world):
    """T-F12: B waits on A's exclusive lock (the server says so), then finds
    the class A wrote. Kills M-F2 in PostgreSQL."""
    world = pg_world
    table = _table(world)
    with world.db.transaction() as unit:
        classify_locked(unit, world.stores(), table.campaign, table.sheet, "hp", expected=UNCLASSIFIED, new=PUBLIC)
        thread, outcome = _in_thread(
            lambda: world.mutations().classify(
                table.campaign, table.sheet, "hp", expected=UNCLASSIFIED, new=GM_ONLY
            )
        )
        assert _someone_waits_on_a_lock(world.dsn)
    thread.join(PATIENCE)
    assert isinstance(outcome.get("error"), ClassMoved)
    assert _of(world, table.campaign, table.sheet, "hp") == PUBLIC


@needs_db
def test_a_member_add_and_a_share_mode_reader_serialise_on_the_campaign_lock(pg_world):
    """T-F13 (RC-11's form): the reader waits, then reads the new revision."""
    world = pg_world
    table = _table(world)
    wren = _seat(world, table.campaign, "Wren")
    before = _revisions(world, table.campaign)[0]

    def read() -> int:
        with world.db.transaction() as unit:
            unit.lock_campaign(table.campaign, shared=True)
            return world.campaigns.authz_revision(unit, table.campaign)

    with world.db.transaction() as unit:
        add_member_locked(unit, world.stores(), table.campaign, table.group, wren)
        thread, outcome = _in_thread(read)
        assert _someone_waits_on_a_lock(world.dsn)
    thread.join(PATIENCE)
    assert outcome.get("value") == before + 1


@needs_db
@pytest.mark.parametrize("held", ["group-rename", "seat-removal"])
def test_a_rename_or_a_seat_removal_in_flight_does_not_block_a_member_add(pg_world, held):
    """Critic item 7: A holds the group row (`hold_group`) or the seat row
    (`ParticipantStore.hold` and `remove`), uncommitted; B's member add under
    the exclusive campaign lock completes with a 200 ms lock timeout. Kills
    `FOR UPDATE` in `hold_group` (RQ-3, RC-14's form)."""
    world = pg_world
    table = _table(world)
    wren = _seat(world, table.campaign, "Wren")
    quick = _database(world.dsn, CampaignLockSettings(lock_timeout_s=0.2, transaction_timeout_s=30))
    with world.db.transaction() as unit:
        if held == "group-rename":
            assert world.eligibility.hold_group(unit, table.campaign, table.group) is not None
        else:
            world.participants.hold(unit, wren, campaign_id=table.campaign)
            world.participants.remove(unit, table.campaign, wren)
        change = world.mutations(quick).add_member(table.campaign, table.group, wren)
        assert change.changed

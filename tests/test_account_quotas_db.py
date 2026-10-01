"""The per-account caps against a real PostgreSQL, and the suite both worlds
run (agent-forge-harness-531x, PR-B).

Two caps live here: the account's campaign count (`CampaignStore.create`'s
`max_per_owner`) and the account's stored document-and-version bytes
(`DocumentStore`'s `quota`). Both refuse before any row changes, both are
account-wide (every campaign the owner has, not just one), and neither has an
override.

**The shared behavioural suite** runs twice, once against the in-memory twin
and once against PostgreSQL (the `world` fixture's pattern copies
`tests/test_document_db.py:86-130` rather than importing it, by that file's
own stated convention).

**What no fake can prove**: that two connections racing at the cap admit
exactly one (`_while_another_transaction_holds` below copies
`tests/test_document_db.py`'s race helper for the same reason — a fake's
transactions are serial and have no race to model), and that a row written
before migration 0021 is still counted correctly from its JSON text.

Run from the repo root:
    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_account_quotas_db.py -q
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service.campaign_store import CampaignCapReached, InMemoryCampaignStore, PostgresCampaignStore
from service.db import (
    AdvisoryLock,
    CampaignLockSettings,
    Database,
    InMemoryDatabase,
    PoolSettings,
)
from service.document_store import (
    SEAL_IDLE_S,
    ByteQuota,
    InMemoryDocumentStore,
    PostgresDocumentStore,
    StorageQuotaReached,
    measured_bytes,
)
from service.workbench_contracts import Author, DocumentTypeId

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
#: Long enough that a lock taken first is always taken first; short enough
#: that a test hangs fast if something is actually wrong.
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
#: For the interleaving tests: a transaction waits for a *thread*, not a
#: database, so RQ-8's five seconds would race the test harness.
PATIENT = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
PATIENCE = 15


def _content(voice: str = "low", **extra: Any) -> dict[str, Any]:
    """An NPC document's `data` — the shape `tests/test_document_db.py`'s
    `AN_NPC` uses — with `voice` as the one field these tests grow and shrink
    to control `measured_bytes` precisely."""
    return {"name": "Vashti", "qualifier": "Harbourmistress", "tags": ["harbour"], "voice": voice, **extra}


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("acctquota") as target:
        mig.migrate(target)
        yield target


def _database(dsn: str, settings: CampaignLockSettings = QUICK) -> Database:
    return Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5), settings)


@dataclass
class World:
    kind: str
    db: Any
    campaigns: Any
    documents: Any
    owner: int
    other_owner: int


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db: Any = InMemoryDatabase()
        yield World("fake", db, InMemoryCampaignStore(db), InMemoryDocumentStore(db), owner=1, other_owner=2)
        return

    target = request.getfixturevalue("dsn")
    with connect(target) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm.a@example.com', 'x') RETURNING id"
        ).fetchone()[0]
        other = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm.b@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    yield World(
        "postgres", _database(target), PostgresCampaignStore(), PostgresDocumentStore(),
        owner=int(owner), other_owner=int(other),
    )


def _campaign(world: World, owner: int, *, max_per_owner: int | None = None, name: str = "Campaign") -> str:
    with world.db.transaction() as unit:
        return world.campaigns.create(unit, owner_id=owner, name=name, now=T0, max_per_owner=max_per_owner).id


def _campaign_count(world: World, owner: int) -> int:
    with world.db.transaction() as unit:
        return len(world.campaigns.list_for_owner(unit, owner))


# ── 3a. The campaign cap ─────────────────────────────────────────────────────


def test_the_campaign_cap_admits_the_last_campaign_and_refuses_the_next(world: World) -> None:
    cap = 3
    for i in range(cap):
        _campaign(world, world.owner, max_per_owner=cap, name=f"C{i}")
    assert _campaign_count(world, world.owner) == cap

    with pytest.raises(CampaignCapReached):
        _campaign(world, world.owner, max_per_owner=cap, name="One Too Many")
    assert _campaign_count(world, world.owner) == cap, "a refused create must write nothing"


def test_archived_campaigns_count_toward_the_campaign_cap(world: World) -> None:
    """An archive-then-create loop must not get around the cap: rows are never
    deleted, and neither is an archived campaign excluded from the count."""
    cap = 2
    first = _campaign(world, world.owner, max_per_owner=cap, name="First")
    with world.db.transaction() as unit:
        assert world.campaigns.set_archived(unit, first, owner_id=world.owner, archived=True) is True
    _campaign(world, world.owner, max_per_owner=cap, name="Second")  # at the cap, but admitted

    with pytest.raises(CampaignCapReached):
        _campaign(world, world.owner, max_per_owner=cap, name="Third")
    assert _campaign_count(world, world.owner) == cap


def test_the_campaign_cap_is_per_owner(world: World) -> None:
    """Owner B at the cap does not stop owner A: A's own count decides."""
    cap = 1
    _campaign(world, world.other_owner, max_per_owner=cap, name="B's only campaign")
    with pytest.raises(CampaignCapReached):
        _campaign(world, world.other_owner, max_per_owner=cap, name="B's second")

    _campaign(world, world.owner, max_per_owner=cap, name="A's only campaign")  # unaffected by B
    assert _campaign_count(world, world.owner) == 1


# ── 3b. The stored-byte cap ──────────────────────────────────────────────────


def _document_id(world: World, campaign_id: str, *, data: dict[str, Any] | None = None, now: datetime = T0) -> str:
    with world.db.transaction() as unit:
        made = world.documents.create(
            unit, campaign_id, doc_type=DocumentTypeId.NPC, type_version=1,
            data=data if data is not None else _content(), author=Author.GM, now=now,
        )
        return made.id


def _stored_bytes(world: World, owner: int) -> int:
    with world.db.transaction() as unit:
        return world.documents.stored_bytes(unit, owner)


def test_stored_bytes_is_measured_bytes_of_every_document_and_version_across_campaigns(world: World) -> None:
    """The corpus: ASCII, an accented letter, an astral character and a quote
    — both worlds must return the same integer, equal to the Python
    expectation. (A text field is refused if it carries a real newline, so
    the corpus stays single-line; `measured_bytes` is exercised on JSON's own
    escaping of the quote regardless.)"""
    corpus = 'plain, café, 𝔸, "quoted"'
    a = _campaign(world, world.owner, name="A")
    b = _campaign(world, world.owner, name="B")
    doc_a = _document_id(world, a, data=_content(voice=corpus))
    _document_id(world, b, data=_content(voice="second"))

    with world.db.transaction() as unit:
        # A patch within the idle window updates the one open version IN
        # PLACE (no second version), so `a` still has exactly one version,
        # now equal to the patch's own content.
        world.documents.write_fields(
            unit, a, doc_a, fields={"voice": corpus + "!"}, author=Author.GM, base_write_revision=None, now=T0,
        )

    expected = (
        measured_bytes(_content(voice=corpus + "!"))  # a's document row
        + measured_bytes(_content(voice=corpus + "!"))  # a's one version, updated in place
        + measured_bytes(_content(voice="second"))  # b's document row
        + measured_bytes(_content(voice="second"))  # b's one version
    )
    assert _stored_bytes(world, world.owner) == expected


def _create_with_quota(world: World, campaign_id: str, owner: int, max_bytes: int, *, data: dict[str, Any]) -> str:
    with world.db.transaction() as unit:
        unit.lock(AdvisoryLock.ACCOUNT_STORAGE, str(owner))
        made = world.documents.create(
            unit, campaign_id, doc_type=DocumentTypeId.NPC, type_version=1, data=data, author=Author.GM, now=T0,
            quota=ByteQuota(owner, max_bytes),
        )
        return made.id


def test_a_create_past_the_byte_cap_is_refused_and_writes_nothing(world: World) -> None:
    campaign_id = _campaign(world, world.owner, name="A")
    data = _content()
    growth = 2 * measured_bytes(data)

    with pytest.raises(StorageQuotaReached):
        _create_with_quota(world, campaign_id, world.owner, growth - 1, data=data)
    assert _stored_bytes(world, world.owner) == 0, "a refused create must write nothing"

    # Exactly at the cap: admitted.
    _create_with_quota(world, campaign_id, world.owner, growth, data=data)
    assert _stored_bytes(world, world.owner) == growth


def test_a_patch_that_grows_past_the_byte_cap_is_refused_and_writes_nothing_open_version(world: World) -> None:
    """Covers the open-version-update branch: a second GM write within the
    idle window updates the one open version in place."""
    campaign_id = _campaign(world, world.owner, name="A")
    doc_id = _document_id(world, campaign_id, data=_content(voice="short"))
    before = _stored_bytes(world, world.owner)
    grown = _content(voice="a much, much longer voice than before")
    old_doc_bytes = measured_bytes(_content(voice="short"))
    new_doc_bytes = measured_bytes(grown)
    growth = (new_doc_bytes - old_doc_bytes) + (new_doc_bytes - old_doc_bytes)  # document + the same open version

    def patch(max_bytes: int) -> None:
        with world.db.transaction() as unit:
            unit.lock(AdvisoryLock.ACCOUNT_STORAGE, str(world.owner))
            world.documents.write_fields(
                unit, campaign_id, doc_id, fields={"voice": grown["voice"]}, author=Author.GM,
                base_write_revision=None, now=T0 + timedelta(seconds=1),
                quota=ByteQuota(world.owner, max_bytes),
            )

    with pytest.raises(StorageQuotaReached):
        patch(before + growth - 1)
    assert _stored_bytes(world, world.owner) == before, "a refused patch must write nothing"

    patch(before + growth)  # exactly at the cap: admitted
    assert _stored_bytes(world, world.owner) == before + growth


def test_a_patch_that_grows_past_the_byte_cap_is_refused_and_writes_nothing_new_version(world: World) -> None:
    """Covers the new-version branch: a write past `SEAL_IDLE_S` seals the
    open version first and appends a new one."""
    campaign_id = _campaign(world, world.owner, name="A")
    doc_id = _document_id(world, campaign_id, data=_content(voice="short"))
    before = _stored_bytes(world, world.owner)
    grown = _content(voice="a much, much longer voice than before, again")
    old_doc_bytes = measured_bytes(_content(voice="short"))
    new_doc_bytes = measured_bytes(grown)
    growth = (new_doc_bytes - old_doc_bytes) + new_doc_bytes  # document, plus a brand-new version
    idle = T0 + timedelta(seconds=SEAL_IDLE_S + 1)

    def patch(max_bytes: int) -> None:
        with world.db.transaction() as unit:
            unit.lock(AdvisoryLock.ACCOUNT_STORAGE, str(world.owner))
            world.documents.write_fields(
                unit, campaign_id, doc_id, fields={"voice": grown["voice"]}, author=Author.GM,
                base_write_revision=None, now=idle, quota=ByteQuota(world.owner, max_bytes),
            )

    with pytest.raises(StorageQuotaReached):
        patch(before + growth - 1)
    assert _stored_bytes(world, world.owner) == before

    patch(before + growth)
    assert _stored_bytes(world, world.owner) == before + growth


def test_a_patch_that_does_not_grow_is_admitted_at_the_byte_cap(world: World) -> None:
    """A write that shrinks or holds storage steady is always admitted, at any
    cap — a GM at the cap can still trim."""
    campaign_id = _campaign(world, world.owner, name="A")
    doc_id = _document_id(world, campaign_id, data=_content(voice="a longer starting voice"))
    current = _stored_bytes(world, world.owner)

    with world.db.transaction() as unit:
        unit.lock(AdvisoryLock.ACCOUNT_STORAGE, str(world.owner))
        world.documents.write_fields(
            unit, campaign_id, doc_id, fields={"voice": "x"}, author=Author.GM, base_write_revision=None,
            now=T0 + timedelta(seconds=1), quota=ByteQuota(world.owner, current - 1),  # BELOW current usage
        )
    assert _stored_bytes(world, world.owner) <= current


def test_a_restore_past_the_byte_cap_is_refused_and_writes_nothing(world: World) -> None:
    campaign_id = _campaign(world, world.owner, name="A")
    big = _content(voice="a" * 200)
    small = _content(voice="a")
    doc_id = _document_id(world, campaign_id, data=big)
    # Seal version 1 with an idle gap, then patch to something small: the
    # current content shrinks, but version 1 (big) is still on record.
    with world.db.transaction() as unit:
        world.documents.write_fields(
            unit, campaign_id, doc_id, fields={"voice": small["voice"]}, author=Author.GM,
            base_write_revision=None, now=T0 + timedelta(seconds=SEAL_IDLE_S + 1),
        )
    before = _stored_bytes(world, world.owner)
    current_bytes = measured_bytes(small)
    big_bytes = measured_bytes(big)
    growth = 2 * big_bytes - current_bytes

    def restore(max_bytes: int) -> None:
        with world.db.transaction() as unit:
            unit.lock(AdvisoryLock.ACCOUNT_STORAGE, str(world.owner))
            world.documents.restore(
                unit, campaign_id, doc_id, version_number=1, now=T0 + timedelta(seconds=2 * SEAL_IDLE_S),
                quota=ByteQuota(world.owner, max_bytes),
            )

    with pytest.raises(StorageQuotaReached):
        restore(before + growth - 1)
    assert _stored_bytes(world, world.owner) == before, "a refused restore must write nothing"

    restore(before + growth)
    assert _stored_bytes(world, world.owner) == before + growth


def test_bytes_in_one_campaign_block_writes_in_another_of_the_same_owner(world: World) -> None:
    """The cap is on the ACCOUNT, not the campaign: filling one campaign's
    documents blocks a create in a sibling campaign of the same owner."""
    a = _campaign(world, world.owner, name="A")
    b = _campaign(world, world.owner, name="B")
    data = _content()
    growth = 2 * measured_bytes(data)
    _create_with_quota(world, a, world.owner, growth, data=data)  # fills the whole budget in A

    with pytest.raises(StorageQuotaReached):
        _create_with_quota(world, b, world.owner, growth, data=data)  # B has none left


def test_archiving_a_document_does_not_shrink_the_byte_total_pr232_r1_h1(world: World) -> None:
    """PR #232 review pr232-531xb-r1, finding H1: an archive-then-write loop
    must not be a way around the byte cap. Fill account A to the byte cap with
    one document, archive it (archiving never takes the account lock and never
    shrinks `stored_bytes`), then prove a 1-byte-or-more quota'd create is
    still refused in the SAME campaign and in a SIBLING campaign of the same
    owner, and that nothing new was written."""
    a = _campaign(world, world.owner, name="A")
    b = _campaign(world, world.owner, name="B")
    data = _content()
    growth = 2 * measured_bytes(data)
    doc_id = _create_with_quota(world, a, world.owner, growth, data=data)  # exactly at the cap
    assert _stored_bytes(world, world.owner) == growth

    with world.db.transaction() as unit:
        changed = world.documents.set_archived(unit, a, doc_id, archived=True, now=T0)
    assert changed is True
    assert _stored_bytes(world, world.owner) == growth, "archiving must not shrink the counted total"

    with pytest.raises(StorageQuotaReached):
        _create_with_quota(world, a, world.owner, growth + 1, data=_content(voice="one more byte, same campaign"))
    with pytest.raises(StorageQuotaReached):
        _create_with_quota(world, b, world.owner, growth + 1, data=_content(voice="one more byte, sibling campaign"))
    assert _stored_bytes(world, world.owner) == growth, "both refused creates must write nothing"


def test_archiving_the_campaign_does_not_shrink_the_byte_total_pr232_r1_h1(world: World) -> None:
    """The other half of H1: archiving the CAMPAIGN itself (not just the
    document) must not exclude its documents from the account's byte total
    either — `stored_bytes` joins through `campaigns.owner_id` with no
    archived filter on either side."""
    a = _campaign(world, world.owner, name="A")
    b = _campaign(world, world.owner, name="B")
    data = _content()
    growth = 2 * measured_bytes(data)
    _create_with_quota(world, a, world.owner, growth, data=data)  # exactly at the cap, in A
    assert _stored_bytes(world, world.owner) == growth

    with world.db.transaction() as unit:
        changed = world.campaigns.set_archived(unit, a, owner_id=world.owner, archived=True)
    assert changed is True
    assert _stored_bytes(world, world.owner) == growth, "archiving the campaign must not shrink the counted total"

    with pytest.raises(StorageQuotaReached):
        _create_with_quota(world, b, world.owner, growth + 1, data=_content(voice="one more byte, sibling campaign"))
    assert _stored_bytes(world, world.owner) == growth, "the refused create must write nothing"


# ── Twin-only: the account-lock guard ────────────────────────────────────────


def test_a_quota_write_without_the_account_lock_is_refused_by_the_twin() -> None:
    """Catches a dropped `unit.lock(AdvisoryLock.ACCOUNT_STORAGE, ...)` that
    no PostgreSQL assertion can prove directly (its half is the race test
    below)."""
    db = InMemoryDatabase()
    campaigns = InMemoryCampaignStore(db)
    documents = InMemoryDocumentStore(db)
    with db.transaction() as unit:
        campaign_id = campaigns.create(unit, owner_id=1, name="A", now=T0).id

    with db.transaction() as unit:
        with pytest.raises(RuntimeError, match="account's storage lock"):
            documents.create(
                unit, campaign_id, doc_type=DocumentTypeId.NPC, type_version=1, data=_content(),
                author=Author.GM, now=T0, quota=ByteQuota(1, 10_000_000),
                # No unit.lock(AdvisoryLock.ACCOUNT_STORAGE, ...) taken first.
            )


# ── PostgreSQL-only: races, with two connections and the server's own account
#    of the wait (copies tests/test_document_db.py's helper; see module note) ─


def _someone_waits_on_a_lock(dsn: str, patience: float = PATIENCE) -> bool:
    deadline = time.monotonic() + patience
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


def _while_another_transaction_holds(
    dsn: str, db: Database, holder_work: Callable[[Any], None], waiter_work: Callable[[Any], Any],
) -> Any:
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []
    produced: list[Any] = []

    def holder() -> None:
        try:
            with db.transaction() as unit:
                holder_work(unit)
                took.set()
                release.wait(PATIENCE)
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    def waiter() -> None:
        try:
            with db.transaction() as unit:
                produced.append(waiter_work(unit))
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            produced.append(exc)

    holding = threading.Thread(target=holder, daemon=True)
    holding.start()
    assert took.wait(PATIENCE), "the holding transaction never did its work"
    if failure:
        raise failure[0]

    waiting = threading.Thread(target=waiter, daemon=True)
    waiting.start()
    assert _someone_waits_on_a_lock(dsn), "nobody was blocked, so this proves nothing"
    release.set()
    holding.join(PATIENCE)
    waiting.join(PATIENCE)
    assert not holding.is_alive() and not waiting.is_alive(), "a transaction never finished"
    if failure:
        raise failure[0]
    if isinstance(produced[0], BaseException):
        raise produced[0]
    return produced[0]


@needs_db
def test_two_racing_campaign_creates_at_the_cap_admit_exactly_one(dsn: str) -> None:
    db = _database(dsn, PATIENT)
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm.race@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    campaigns = PostgresCampaignStore()
    with db.transaction() as unit:
        campaigns.create(unit, owner_id=int(owner), name="the one that already exists", now=T0, max_per_owner=2)

    def holder(unit: Any) -> None:
        campaigns.create(unit, owner_id=int(owner), name="second (holder)", now=T0, max_per_owner=2)

    def waiter(unit: Any) -> Any:
        # A STRING, not the exception itself: `_while_another_transaction_holds`
        # re-raises `produced[0]` whenever it IS a `BaseException` (it cannot
        # tell an expected refusal from a real failure), so `CampaignCapReached`
        # — the expected outcome of this race — must be caught and reported as
        # a plain value instead.
        try:
            campaigns.create(unit, owner_id=int(owner), name="second (waiter)", now=T0, max_per_owner=2)
            return "admitted"
        except CampaignCapReached:
            return "refused"

    outcome = _while_another_transaction_holds(dsn, db, holder, waiter)
    assert outcome == "refused", "exactly one of the two racing creates must succeed"
    with connect(dsn) as conn:
        count = conn.execute("SELECT count(*) FROM campaign.campaigns WHERE owner_id = %s", (owner,)).fetchone()[0]
    assert count == 2


@needs_db
def test_two_racing_document_creates_in_two_campaigns_at_the_byte_cap_admit_exactly_one(dsn: str) -> None:
    """Proves the lock is PER ACCOUNT, not per campaign: two campaigns of the
    same owner still serialise on one lock."""
    db = _database(dsn, PATIENT)
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm.race2@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    campaigns, documents = PostgresCampaignStore(), PostgresDocumentStore()
    with db.transaction() as unit:
        campaign_a = campaigns.create(unit, owner_id=int(owner), name="A", now=T0).id
        campaign_b = campaigns.create(unit, owner_id=int(owner), name="B", now=T0).id
    data = _content()
    max_bytes = 2 * measured_bytes(data)  # room for exactly one document

    def make(campaign_id: str) -> Callable[[Any], Any]:
        def work(unit: Any) -> Any:
            unit.lock(AdvisoryLock.ACCOUNT_STORAGE, str(owner))
            # A string, not the exception — see the campaign race test's
            # comment: `StorageQuotaReached` here is the expected outcome.
            try:
                documents.create(
                    unit, campaign_id, doc_type=DocumentTypeId.NPC, type_version=1, data=data, author=Author.GM,
                    now=T0, quota=ByteQuota(int(owner), max_bytes),
                )
                return "admitted"
            except StorageQuotaReached:
                return "refused"
        return work

    outcome = _while_another_transaction_holds(dsn, db, make(campaign_a), make(campaign_b))
    assert outcome == "refused", "exactly one of the two racing creates must succeed"
    with connect(dsn) as conn:
        count = conn.execute(
            "SELECT count(*) FROM campaign.documents d JOIN campaign.campaigns c ON c.id = d.campaign_id "
            "WHERE c.owner_id = %s", (owner,),
        ).fetchone()[0]
    assert count == 1


@needs_db
def test_stored_bytes_counts_a_row_written_before_0021_by_its_json_text(dsn: str) -> None:
    """A document whose `data_bytes` is NULL — as every row written before
    this migration is — falls back to `octet_length(data::text)`, never to
    zero, which would quietly let an old account's usage be undercounted."""
    db = _database(dsn)
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm.old@example.com', 'x') RETURNING id"
        ).fetchone()[0]
    campaigns, documents = PostgresCampaignStore(), PostgresDocumentStore()
    with db.transaction() as unit:
        campaign_id = campaigns.create(unit, owner_id=int(owner), name="A", now=T0).id
        documents.create(
            unit, campaign_id, doc_type=DocumentTypeId.NPC, type_version=1, data=_content(), author=Author.GM, now=T0,
        )
    with connect(dsn) as conn:
        conn.execute("UPDATE campaign.documents SET data_bytes = NULL")
        conn.execute("UPDATE campaign.document_versions SET data_bytes = NULL")

    with db.transaction() as unit:
        total = documents.stored_bytes(unit, int(owner))
    assert total == 2 * measured_bytes(_content()), "the JSON-text fallback must still measure it correctly"

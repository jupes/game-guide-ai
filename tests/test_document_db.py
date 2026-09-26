"""The document schema against a real PostgreSQL, and the suite both worlds run
(1kg.5.1, slice A).

Two things live here.

**The shared behavioural suite** — one set of tests run twice, once against the
in-memory twin and once against PostgreSQL, so that a rule the two could
disagree about is asserted of both. Those tests carry no mark and run anywhere;
their `postgres` parameter carries `needs_db` and skips without a server.

**What no fake can prove** — that the database itself refuses a second open
version, a second document for one command id and a second sheet for one
participant; that `FOR NO KEY UPDATE` lets a referencing insert through where
`FOR UPDATE` would not (RQ-3, RC-14); and who **blocks** whom when two writes
meet on one document. Every race here is a **two-connection** test with an
explicit interleaving and a `pg_stat_activity` check that the waiter really
waits. A race is never tested by nesting units on the twin, whose transactions
are serial and which therefore has no conflict to model.

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_document_db.py -q
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service import workbench_contracts as wire
from service.campaign_store import (
    InMemoryCampaignStore,
    MissingParent,
    PostgresCampaignStore,
    fake,
)
from service.db import (
    CampaignLockSettings,
    Database,
    InMemoryDatabase,
    PoolSettings,
)
from service.document_store import (
    SEAL_IDLE_S,
    SEARCH_KEY_MAX,
    DocumentRecord,
    FieldConflict,
    InMemoryDocumentStore,
    PostgresDocumentStore,
    StaleTypeVersion,
    UnknownCursor,
    UnknownWriteRevision,
    name_key,
    search_key,
)
from service.participant_store import InMemoryParticipantStore, PostgresParticipantStore
from service.workbench_contracts import Author, DocumentTypeId

DOCUMENT = "doc_" + "a" * 22
CAMPAIGN = "cmp_" + "a" * 22
OTHER_CAMPAIGN = "cmp_" + "b" * 22

#: A second is long enough that a lock taken first is always taken first.
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
#: For the interleaving tests: a transaction there waits for a *thread*, not for
#: a database, so RQ-8's five seconds would be racing the test harness.
PATIENT = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
#: How long a test waits for another thread before calling it a hang.
PATIENCE = 15

#: The SQLSTATE PostgreSQL answers with when a JSON string carries U+0000, which
#: `jsonb` cannot represent. See `test_the_one_known_parity_gap_is_pinned`: this
#: is the ONE behaviour the twin and the database are allowed to disagree about
#: in this bead, and F-9 (`1kg.5.7`) closes it. Pinned by SQLSTATE and exception
#: class and never by the driver's message, which quotes the row (SEC-20).
PG_NUL_IN_JSONB = "22P05"


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("documents") as target:
        mig.migrate(target)
        yield target


def _database(dsn: str, settings: CampaignLockSettings = QUICK) -> Database:
    return Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5), settings)


# ── The shared behavioural suite ─────────────────────────────────────────────


@dataclass
class World:
    """The campaign, participant and document stores over one database."""

    kind: str
    db: Any
    campaigns: Any
    participants: Any
    documents: Any
    owner: int


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db: Any = InMemoryDatabase()
        yield World(
            "fake",
            db,
            InMemoryCampaignStore(db),
            InMemoryParticipantStore(db),
            InMemoryDocumentStore(db),
            owner=1,
        )
        return

    target = request.getfixturevalue("dsn")
    with connect(target) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') "
            "RETURNING id"
        ).fetchone()[0]
    yield World(
        "postgres",
        _database(target),
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresDocumentStore(),
        owner=int(owner),
    )


def test_the_shared_suite_really_runs_against_both_worlds() -> None:
    """A lost `postgres` parameter would turn every test below into a fake-only
    test that still reported green — the exact shape of failure this file
    exists to prevent. Asserted rather than assumed, and it is why the
    parametrisation is read out of the fixture itself."""
    params = world._fixture_function_marker.params
    assert params is not None
    kinds = [p if isinstance(p, str) else p.values[0] for p in params]
    assert kinds == ["fake", "postgres"]
    postgres = params[1]
    assert not isinstance(postgres, str)
    assert [mark.name for mark in postgres.marks] == ["skipif"], (
        "the postgres parameter must carry needs_db, so a machine without a "
        "server SKIPS it rather than silently running the fake twice"
    )


def _a_campaign(world: World, name: str = "Nocturne") -> str:
    with world.db.transaction() as unit:
        return world.campaigns.create(unit, owner_id=world.owner, name=name).id


def _a_participant(world: World, campaign_id: str, alias: str = "Rook") -> str:
    with world.db.transaction() as unit:
        return world.participants.add(unit, campaign_id, alias=alias).id


#: Every document a test builds carries the keys its type declares, and every
#: stat block carries a valid `ac` and `hp` — `1kg.5.7` Stage A makes those two
#: required on writes, and the store's own `check_fields(..., whole=True)` call
#: is where that rule lands.
AN_NPC = {"name": "Vashti", "qualifier": "Harbourmistress", "tags": ["harbour"], "voice": "low"}
A_STATBLOCK = {"name": "Dire Rat", "qualifier": "", "tags": [], "ac": 12, "hp": 7}
#: A character sheet declares neither `voice` nor the stat block's keys, so it
#: needs its own fixture rather than AN_NPC: the store's own
#: `check_fields(..., whole=True)` call refuses a key the type does not declare,
#: which is the check working. `ac` and `hp` stay OPTIONAL for this type (lead
#: ruling 5.7#2) and are given here only because they are valid either way.
A_CHARACTER_SHEET = {"name": "Rook", "qualifier": "Rogue", "tags": ["party"], "ac": 14, "hp": 22}


def _a_document(
    world: World,
    campaign_id: str,
    *,
    data: dict[str, Any] | None = None,
    doc_type: DocumentTypeId = DocumentTypeId.NPC,
    author: Author = Author.GM,
    command_id: str | None = None,
    now: datetime | None = None,
) -> DocumentRecord:
    with world.db.transaction() as unit:
        return world.documents.create(
            unit,
            campaign_id,
            doc_type=doc_type,
            type_version=1,
            data=dict(AN_NPC if data is None else data),
            author=author,
            command_id=command_id,
            now=now,
        )


def _write(world: World, campaign_id: str, document_id: str, **kwargs: Any) -> DocumentRecord:
    with world.db.transaction() as unit:
        return world.documents.write_fields(unit, campaign_id, document_id, **kwargs)


def _versions(world: World, campaign_id: str, document_id: str) -> list[Any]:
    """Every version of that document, oldest first — the whole history, which
    is what the counting assertions below need."""
    with world.db.transaction() as unit:
        page = world.documents.history(
            unit, campaign_id, document_id, before_number=None, limit=wire.HISTORY_PAGE_MAX_ITEMS
        )
    return list(reversed(page))


def _folded_keys(world: World, document_id: str) -> tuple[str, str]:
    """The `name_key` and `search_key` the world actually stored.

    They are columns rather than record fields — `1kg.5.2`'s library page reads
    them, and slice B's `list_documents` is what will — so this is the only way
    to assert that both worlds compute the same ones, which is the point.
    """
    with world.db.transaction() as unit:
        if world.kind == "postgres":
            row = unit.conn.execute(
                "SELECT name_key, search_key FROM campaign.documents WHERE id = %s",
                (document_id,),
            ).fetchone()
            return row[0], row[1]
        stored = world.documents._documents.visible(fake(unit))[document_id]
        return stored.name_key, stored.search_key


# Creating ────────────────────────────────────────────────────────────────────


def test_a_created_document_carries_its_type_version_and_a_first_open_version(
    world: World,
) -> None:
    """The bead's "JSON schema/type version is stored", and CANVAS-34's one open
    working version a GM's autosaves accumulate into."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)

    assert made.type == DocumentTypeId.NPC.value
    assert made.type_version == wire.DOC_TYPE_VERSION[DocumentTypeId.NPC]
    assert made.write_revision == 1
    assert made.field_revisions == {key: 1 for key in AN_NPC}
    assert made.version.number == 1
    assert made.version.author == Author.GM.value
    assert made.version.sealed_at is None, "a GM's first version is the open one"
    assert made.version.changed_fields == tuple(sorted(AN_NPC))
    assert made.version.restored_from is None
    assert made.archived_at is None and made.linked_participant_id is None


def test_an_assistant_create_seals_its_version_as_it_is_written(world: World) -> None:
    """Only a `gm` version is ever open: an open assistant version has no writer
    that would continue it (CANVAS-34)."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign, author=Author.ASSISTANT)

    assert made.version.sealed_at is not None
    assert [v for v in _versions(world, campaign, made.id) if v.sealed_at is None] == []


def test_a_stat_block_carries_its_armour_class_and_hit_points(world: World) -> None:
    """A type other than `npc`, so the store is not shown to work for one shape
    only — and the one `1kg.5.7` Stage A makes strictest."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign, data=A_STATBLOCK, doc_type=DocumentTypeId.STATBLOCK)

    assert made.data["ac"] == 12 and made.data["hp"] == 7
    assert made.type == DocumentTypeId.STATBLOCK.value


def test_creating_in_a_campaign_that_does_not_exist_is_refused(world: World) -> None:
    """One named refusal in both worlds, raised from a `WHERE EXISTS` guard
    rather than from a caught foreign-key violation, so the transaction stays
    usable for whatever the caller had composed around it."""
    with pytest.raises(MissingParent, match="campaign"):
        _a_document(world, OTHER_CAMPAIGN)


def test_a_document_of_another_campaign_is_indistinguishable_from_one_that_is_not_there(
    world: World,
) -> None:
    """SEC-2's "one query, one 404". Ids are not secrets, so the campaign goes
    into the statement that finds the row, never into a comparison after it."""
    mine = _a_campaign(world)
    theirs = _a_campaign(world, name="Someone else's")
    made = _a_document(world, mine)

    with world.db.transaction() as unit:
        assert world.documents.get(unit, theirs, made.id) is None
        assert world.documents.get(unit, theirs, DOCUMENT) is None
        assert world.documents.hold(unit, theirs, made.id) is None
        assert world.documents.seal(unit, theirs, made.id) is None
        assert world.documents.snapshot(unit, theirs, made.id, 1) is None
        assert world.documents.history(unit, theirs, made.id, before_number=None, limit=5) == []
        with pytest.raises(MissingParent):
            world.documents.write_fields(
                unit, theirs, made.id, fields={"voice": "x"},
                author=Author.GM, base_write_revision=None,
            )


def test_a_second_create_with_the_same_command_id_returns_the_document_already_made(
    world: World,
) -> None:
    """A retried request makes no second document. Which requests carry a
    command id, and what a replay answers on the wire, stay `1kg.5.2`'s."""
    campaign = _a_campaign(world)
    first = _a_document(world, campaign, command_id="cmd-1")
    again = _a_document(world, campaign, command_id="cmd-1")

    assert again.id == first.id
    assert again.write_revision == 1, "a replay is not a write"


def test_a_create_without_a_command_id_is_never_deduplicated(world: World) -> None:
    """The parameter is optional so that `1kg.5.5`'s AI edits and `1kg.5.4`'s
    tool creates can mint without inventing one; that must not collapse two
    genuine creates into one."""
    campaign = _a_campaign(world)
    assert _a_document(world, campaign).id != _a_document(world, campaign).id


def test_a_type_version_that_is_not_this_builds_is_refused_on_create(world: World) -> None:
    campaign = _a_campaign(world)
    with pytest.raises(StaleTypeVersion) as refused:
        with world.db.transaction() as unit:
            world.documents.create(
                unit, campaign, doc_type=DocumentTypeId.NPC, type_version=2,
                data=dict(AN_NPC), author=Author.GM,
            )
    assert (refused.value.stored, refused.value.current) == (2, 1)


def test_a_field_the_type_does_not_declare_is_refused(world: World) -> None:
    """The store's own `check_fields` call is the one that cannot be skipped
    (SEC-33). `1kg.5.2` still calls it to produce the right 422 first."""
    campaign = _a_campaign(world)
    with pytest.raises(ValueError, match="do not declare"):
        _a_document(world, campaign, data={"name": "Vashti", "nonesuch": "x"})


# Sealing, and the one open version ───────────────────────────────────────────


def test_a_burst_of_autosaves_stores_no_snapshot_per_pause(world: World) -> None:
    """CANVAS-34: fifty successive GM writes leave exactly ONE version row,
    still number 1, whose content is the document's. A whole-document snapshot
    per autosave pause would put an unbounded history behind every document."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    start = datetime.now(UTC)

    for index in range(50):
        _write(
            world, campaign, made.id,
            fields={"voice": f"take {index}"},
            author=Author.GM,
            base_write_revision=None,
            now=start + timedelta(seconds=index),
        )

    history = _versions(world, campaign, made.id)
    assert len(history) == 1 and history[0].number == 1
    with world.db.transaction() as unit:
        now = world.documents.get(unit, campaign, made.id)
        open_content = world.documents.snapshot(unit, campaign, made.id, 1)
    assert now is not None and open_content is not None
    assert now.write_revision == 51, "every committed write advances the revision"
    assert open_content.data == now.data, "the open version's content is the document's"
    assert open_content.version.sealed_at is None


def test_an_assistant_write_seals_the_gms_open_version_and_leaves_none_open(
    world: World,
) -> None:
    """CANVAS-34: an open version is sealed by another author's write, and an
    assistant's version is sealed as it is written — so afterwards there is no
    open version at all, and the next GM write starts a fresh one."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    _write(world, campaign, made.id, fields={"voice": "gravel"},
           author=Author.ASSISTANT, base_write_revision=None)

    history = _versions(world, campaign, made.id)
    assert [v.number for v in history] == [1, 2]
    assert [v.author for v in history] == [Author.GM.value, Author.ASSISTANT.value]
    assert all(v.sealed_at is not None for v in history), "nothing is left open"

    _write(world, campaign, made.id, fields={"voice": "low again"},
           author=Author.GM, base_write_revision=None)
    reopened = _versions(world, campaign, made.id)
    assert [v.number for v in reopened] == [1, 2, 3]
    assert reopened[-1].sealed_at is None and reopened[-1].author == Author.GM.value


@pytest.mark.parametrize(
    ("idle_s", "expected"), [(SEAL_IDLE_S - 1, 1), (SEAL_IDLE_S, 2)], ids=["just-under", "at"]
)
def test_the_idle_window_seals_and_nothing_else_does(
    world: World, idle_s: int, expected: int
) -> None:
    """CANVAS-34's ten minutes, with an injected clock: no timer, no job and no
    background sealer is required or permitted, so the rule is deterministic
    and this test does not sleep."""
    campaign = _a_campaign(world)
    start = datetime.now(UTC)
    made = _a_document(world, campaign, now=start)

    _write(world, campaign, made.id, fields={"voice": "later"}, author=Author.GM,
           base_write_revision=None, now=start + timedelta(seconds=idle_s))

    history = _versions(world, campaign, made.id)
    assert [v.number for v in history] == list(range(1, expected + 1))
    assert history[-1].sealed_at is None, "the newest GM version is the open one"
    if expected == 2:
        assert history[0].sealed_at == start + timedelta(seconds=idle_s)


def test_sealing_is_idempotent_and_moves_nothing_else(world: World) -> None:
    """**The two `now=` values must differ, and must be given.** Two bare `seal`
    calls in one transaction land inside a single tick of `datetime.now` on most
    machines, so `sealed_at == sealed_at` would hold even for a store that
    re-stamped the already-sealed row — a sealed version mutating, the one thing
    this slice promises cannot happen. With two explicit clocks the equality can
    only hold because the second seal reached nothing."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    first = datetime.now(UTC)
    second = first + timedelta(seconds=89)

    with world.db.transaction() as unit:
        once = world.documents.seal(unit, campaign, made.id, now=first)
        twice = world.documents.seal(unit, campaign, made.id, now=second)
    assert once is not None and twice is not None
    assert once.version.sealed_at == first
    assert twice.version.sealed_at == first, "the second seal did not re-stamp the sealed row"
    assert twice.write_revision == made.write_revision, "sealing is not a content write"
    assert twice.updated_at == made.updated_at


def test_a_sealed_versions_content_never_changes_again(world: World) -> None:
    """"Immutable once sealed" as a behaviour and not only as a statement in the
    module: the writes that follow build new versions and leave the old one
    exactly as it was."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    with world.db.transaction() as unit:
        world.documents.seal(unit, campaign, made.id)
        before = world.documents.snapshot(unit, campaign, made.id, 1)

    _write(world, campaign, made.id, fields={"voice": "gravel"},
           author=Author.GM, base_write_revision=None)
    _write(world, campaign, made.id, fields={"voice": "silk"},
           author=Author.ASSISTANT, base_write_revision=None)

    with world.db.transaction() as unit:
        after = world.documents.snapshot(unit, campaign, made.id, 1)
    assert before is not None and after is not None
    assert after.data == before.data
    assert after.version == before.version, "not its metadata either"


def test_version_numbers_are_consecutive_from_one_and_the_current_is_the_highest(
    world: World,
) -> None:
    """There is no `current_version` pointer column: the current version is
    MAX(number), so it cannot disagree with the rows it names."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    for author in (Author.ASSISTANT, Author.GM, Author.ASSISTANT):
        latest = _write(world, campaign, made.id, fields={"voice": author.value},
                        author=author, base_write_revision=None)

    numbers = [v.number for v in _versions(world, campaign, made.id)]
    assert numbers == [1, 2, 3, 4]
    assert latest.version.number == 4


def test_whenever_a_version_is_open_its_content_is_the_documents(world: World) -> None:
    """Inferred decision 2: the open version's `data` is kept equal to the
    document's, so a reader needs no branch — and after a write that leaves none
    open, the assertion is that there is none."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    for author in (Author.GM, Author.ASSISTANT, Author.GM):
        record = _write(world, campaign, made.id, fields={"voice": author.value},
                        author=author, base_write_revision=None)
        history = _versions(world, campaign, made.id)
        open_versions = [v for v in history if v.sealed_at is None]
        with world.db.transaction() as unit:
            current = world.documents.snapshot(unit, campaign, made.id, record.version.number)
        assert current is not None and current.data == record.data
        if author is Author.ASSISTANT:
            assert open_versions == []
        else:
            assert len(open_versions) == 1
            with world.db.transaction() as unit:
                shown = world.documents.snapshot(
                    unit, campaign, made.id, open_versions[0].number
                )
            assert shown is not None and shown.data == record.data


def test_changed_fields_leaves_out_a_key_set_and_set_back(world: World) -> None:
    """Requirement 4.7: `changed_fields` is the difference against the version
    before, not the key set of the write that produced it. Version 1 is
    compared against `{}`, so the burst is put into version 2 where the
    question has an answer."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    with world.db.transaction() as unit:
        world.documents.seal(unit, campaign, made.id)

    first = _write(world, campaign, made.id, fields={"voice": "gravel"},
                   author=Author.GM, base_write_revision=None)
    assert first.version.number == 2 and first.version.changed_fields == ("voice",)

    final = _write(world, campaign, made.id, fields={"voice": AN_NPC["voice"]},
                   author=Author.GM, base_write_revision=None)

    assert final.version.number == 2, "still the same open version"
    assert final.write_revision == 3, "a non-empty patch always advances the revision"
    assert final.version.changed_fields == (), "and changed_fields simply does not grow"


# Per-field concurrency ───────────────────────────────────────────────────────


def test_a_write_whose_key_moved_past_its_base_is_a_conflict_naming_exactly_that_key(
    world: World,
) -> None:
    """CANVAS-19, per field and against a WRITE revision — a counter separate
    from history version numbers."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    _write(world, campaign, made.id, fields={"voice": "gravel", "tell": "taps"},
           author=Author.GM, base_write_revision=1)

    with pytest.raises(FieldConflict) as refused:
        _write(world, campaign, made.id, fields={"voice": "silk", "qualifier": "Broker"},
               author=Author.GM, base_write_revision=1)

    assert refused.value.fields == ("voice",), "exactly the key that moved, and sorted"
    assert refused.value.write_revision == 2, "the document's current revision"


def test_a_stale_base_touching_only_untouched_fields_is_rebased_by_the_server(
    world: World,
) -> None:
    """CANVAS-19: "a stale write touching untouched fields is rebased by the
    server, which re-validates the merged document against its type schema
    before committing". The merge under the lock IS that rebase; nothing is
    left to the caller, and `1kg.5.2` only decides what the 409 body looks like
    when `stale_fields` IS non-empty."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    _write(world, campaign, made.id, fields={"voice": "gravel"},
           author=Author.GM, base_write_revision=1)

    rebased = _write(world, campaign, made.id, fields={"tell": "taps a ring"},
                     author=Author.GM, base_write_revision=1)

    assert rebased.data["voice"] == "gravel", "the first writer's value survived"
    assert rebased.data["tell"] == "taps a ring"
    assert rebased.field_revisions == {**made.field_revisions, "voice": 2, "tell": 3}


def test_a_write_with_no_base_never_conflicts(world: World) -> None:
    """`base_write_revision=None` means "no base", which is what `create` and a
    restore use (CANVAS-26: a restore is additive and needs no base)."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    _write(world, campaign, made.id, fields={"voice": "gravel"},
           author=Author.GM, base_write_revision=None)
    later = _write(world, campaign, made.id, fields={"voice": "silk"},
                   author=Author.GM, base_write_revision=None)
    assert later.data["voice"] == "silk"


def test_a_base_revision_from_the_future_is_refused_rather_than_ignored(
    world: World,
) -> None:
    """A base ahead of the document is **not** a harmless spelling of `None`.
    `stale_fields` asks which keys moved *after* that base, so from the future
    the answer is always "none" and conflict detection is silently off: the
    caller is told its edit rebased cleanly when nothing was ever compared.
    Refused in both worlds, with the two numbers and no field text.

    A *stale* base stays allowed and is the safe direction — it conflicts on
    every key that has moved, which is the test above."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)

    with pytest.raises(UnknownWriteRevision) as refused:
        _write(world, campaign, made.id, fields={"voice": "gravel"},
               author=Author.GM, base_write_revision=made.write_revision + 1)

    assert refused.value.write_revision == made.write_revision
    assert refused.value.base_write_revision == made.write_revision + 1
    assert _write(world, campaign, made.id, fields={"voice": "gravel"},
                  author=Author.GM,
                  base_write_revision=made.write_revision).write_revision == 2


def test_an_over_long_summary_is_refused_by_the_application_in_both_worlds(
    world: World,
) -> None:
    """`document_versions.summary` carries `CHECK (length(summary) <= 200)`, and
    a CHECK the application does not enforce first comes back from PostgreSQL as
    a `CheckViolation` whose `DETAIL` quotes the failing row — the summary AND
    the document's `data` — which is exactly the leak SEC-20 forbids, while the
    twin would store the row happily. Bounded in Python before the first
    statement, so the refusal is identical in both worlds, names the field and
    never the value, and leaves the transaction usable."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    private = "her wants, at length" * 20

    with pytest.raises(ValueError, match="summary is at most") as refused:
        _write(world, campaign, made.id, fields={"voice": "gravel"},
               author=Author.GM, base_write_revision=None, summary=private)

    assert private not in f"{refused.value!s}{refused.value!r}"
    assert len(_versions(world, campaign, made.id)) == 1, "nothing was written"
    at_the_bound = "x" * wire.TEXT_FIELD_MAX_CHARS
    kept = _write(world, campaign, made.id, fields={"voice": "gravel"},
                  author=Author.GM, base_write_revision=None, summary=at_the_bound)
    assert kept.version.summary == at_the_bound, "the bound itself is accepted"


def test_an_empty_patch_is_a_no_op(world: World) -> None:
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    unchanged = _write(world, campaign, made.id, fields={}, author=Author.GM,
                       base_write_revision=None, now=datetime.now(UTC) + timedelta(days=1))

    assert unchanged.write_revision == made.write_revision
    assert unchanged.updated_at == made.updated_at
    assert len(_versions(world, campaign, made.id)) == 1


def test_a_patch_whose_values_are_all_unchanged_still_advances_the_write_revision(
    world: World,
) -> None:
    """CANVAS-34's "every committed write advances the document's write
    revision", taken literally. CANVAS-25's "a no-op patch creates no version"
    is an AI-edit-lane rule and is `1kg.5.5`'s."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    with world.db.transaction() as unit:
        world.documents.seal(unit, campaign, made.id)
    same = _write(world, campaign, made.id, fields={"voice": AN_NPC["voice"]},
                  author=Author.GM, base_write_revision=1)

    assert same.write_revision == 2
    assert same.field_revisions["voice"] == 2
    assert same.version.changed_fields == ()


def test_a_write_whose_merged_document_fails_validation_is_refused(world: World) -> None:
    """The later writer gets the validation refusal, which `1kg.5.2` turns into
    a 422 — and the document is left exactly as it was."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    with pytest.raises(ValueError, match="name"):
        _write(world, campaign, made.id, fields={"name": "   "},
               author=Author.GM, base_write_revision=None)

    with world.db.transaction() as unit:
        after = world.documents.get(unit, campaign, made.id)
    assert after is not None and after.data["name"] == AN_NPC["name"]
    assert after.write_revision == 1


def test_a_write_to_a_document_stored_at_an_older_type_version_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: the store compares the row's stored `type_version` against
    `DOC_TYPE_VERSION` itself, so a stale type is a named class and not a
    Pydantic error code a caller would have to string-match. The migration step
    that would bring it forward is ED-24's and belongs to `1kg.5.2`."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    monkeypatch.setitem(wire.DOC_TYPE_VERSION, DocumentTypeId.NPC, 2)

    with pytest.raises(StaleTypeVersion) as refused:
        _write(world, campaign, made.id, fields={"voice": "gravel"},
               author=Author.GM, base_write_revision=None)
    assert (refused.value.type, refused.value.stored, refused.value.current) == ("npc", 1, 2)


# History ─────────────────────────────────────────────────────────────────────


def test_history_reads_newest_first_and_pages_by_keyset(world: World) -> None:
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    for index in range(4):
        _write(world, campaign, made.id, fields={"voice": str(index)},
               author=Author.ASSISTANT if index % 2 else Author.GM, base_write_revision=None)
    # Five versions, so the last page is a partial one: a boundary that falls
    # exactly on a page size proves nothing about the page after it.
    _write(world, campaign, made.id, fields={"tell": "taps"},
           author=Author.GM, base_write_revision=None)

    with world.db.transaction() as unit:
        first = world.documents.history(unit, campaign, made.id, before_number=None, limit=2)
        second = world.documents.history(
            unit, campaign, made.id, before_number=first[-1].number, limit=2
        )
        third = world.documents.history(
            unit, campaign, made.id, before_number=second[-1].number, limit=2
        )
    assert [v.number for v in first] == [5, 4]
    assert [v.number for v in second] == [3, 2]
    assert [v.number for v in third] == [1]


def test_a_history_cursor_that_names_no_version_is_refused(world: World) -> None:
    """Never a silent restart at page one, which would loop for ever."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    with world.db.transaction() as unit:
        with pytest.raises(UnknownCursor):
            world.documents.history(unit, campaign, made.id, before_number=99, limit=5)
        with pytest.raises(ValueError, match="1 to"):
            world.documents.history(
                unit, campaign, made.id, before_number=None,
                limit=wire.HISTORY_PAGE_MAX_ITEMS + 1,
            )


def test_a_snapshot_is_one_versions_content_or_nothing(world: World) -> None:
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    _write(world, campaign, made.id, fields={"voice": "gravel"},
           author=Author.ASSISTANT, base_write_revision=None)

    with world.db.transaction() as unit:
        first = world.documents.snapshot(unit, campaign, made.id, 1)
        missing = world.documents.snapshot(unit, campaign, made.id, 99)
    assert first is not None and first.data["voice"] == AN_NPC["voice"]
    assert first.type == DocumentTypeId.NPC.value and first.type_version == 1
    assert missing is None


# The folded keys, and parity ─────────────────────────────────────────────────


def test_both_worlds_compute_the_same_folded_keys_on_every_write(world: World) -> None:
    """The keys are computed by the APPLICATION for the reason `alias_key` is:
    `lower()` answers according to the database's collation provider while the
    twin answers with Python, and the two disagree about a final sigma and a
    dotted capital I, silently."""
    campaign = _a_campaign(world)
    made = _a_document(
        world, campaign,
        data={"name": "Ｖａｓｈｔｉ", "qualifier": "ΑΣ", "tags": ["Straße"], "voice": "low"},
    )
    stored = _folded_keys(world, made.id)
    assert stored == (name_key("Ｖａｓｈｔｉ"), search_key(
        {"name": "Ｖａｓｈｔｉ", "qualifier": "ΑΣ", "tags": ["Straße"]}
    ))
    assert stored[0] == "vashti" and "strasse" in stored[1]

    moved = _write(world, campaign, made.id, fields={"name": "Vashti the Broker"},
                   author=Author.GM, base_write_revision=None)
    assert _folded_keys(world, made.id)[0] == name_key(moved.data["name"])


def test_the_body_of_a_document_never_enters_the_search_key(world: World) -> None:
    """LIB-20 names three keys, and ED-20 keeps an identity link out of every
    index: `npc.true_identity` is a GM-only relation."""
    campaign = _a_campaign(world)
    made = _a_document(
        world, campaign,
        data={"name": "Vashti", "qualifier": "", "tags": [],
              "notes": "keeps the ledger", "true_identity": "the Archivist"},
    )
    _, searchable = _folded_keys(world, made.id)
    assert "ledger" not in searchable and "archivist" not in searchable


def test_a_rolled_back_transaction_leaves_no_trace_in_either_world(world: World) -> None:
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)

    with pytest.raises(RuntimeError, match="deliberate"):
        with world.db.transaction() as unit:
            world.documents.write_fields(
                unit, campaign, made.id, fields={"voice": "gravel"},
                author=Author.GM, base_write_revision=None,
            )
            raise RuntimeError("deliberate")

    with world.db.transaction() as unit:
        after = world.documents.get(unit, campaign, made.id)
    assert after is not None
    assert after.write_revision == 1 and after.data["voice"] == AN_NPC["voice"]


# ── What only the database can say ───────────────────────────────────────────


@needs_db
def test_the_database_refuses_a_second_open_version_for_one_document(dsn: str) -> None:
    """CANVAS-34's "at most one open version" held by the database itself, not
    by the store's intention: two autosaves racing cannot both open one."""
    world = _a_seeded_world(dsn)
    made = _a_document(world, CAMPAIGN)
    with connect(dsn) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO campaign.document_versions "
                "(document_id, number, author, summary, changed_fields, data, "
                "created_at, updated_at) "
                "VALUES (%s, 99, 'gm', '', '[]'::jsonb, '{}'::jsonb, now(), now())",
                (made.id,),
            )


@needs_db
def test_the_database_refuses_a_second_document_for_one_command_id(dsn: str) -> None:
    world = _a_seeded_world(dsn)
    _a_document(world, CAMPAIGN, command_id="cmd-1")
    with connect(dsn) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO campaign.documents (id, campaign_id, type, type_version, data, "
                "write_revision, field_revisions, name_key, search_key, created_command_id) "
                "VALUES (%s, %s, 'npc', 1, '{}'::jsonb, 1, '{}'::jsonb, '', '', 'cmd-1')",
                (DOCUMENT, CAMPAIGN),
            )


@needs_db
def test_the_database_refuses_a_second_sheet_for_one_participant(dsn: str) -> None:
    """One sheet per participant (AUD-13, lead ruling 5.1#2), held by
    `documents_participant_uidx`. The link's two store primitives are slice B's;
    the column and the index ship here with the rest of the DDL."""
    world = _a_seeded_world(dsn)
    seat = _a_participant(world, CAMPAIGN)
    first = _a_document(world, CAMPAIGN, data=dict(A_CHARACTER_SHEET),
                        doc_type=DocumentTypeId.CHARACTER_SHEET)
    second = _a_document(world, CAMPAIGN, data={"name": "Second", "qualifier": "", "tags": []},
                         doc_type=DocumentTypeId.CHARACTER_SHEET)
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE campaign.documents SET linked_participant_id = %s WHERE id = %s",
            (seat, first.id),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "UPDATE campaign.documents SET linked_participant_id = %s WHERE id = %s",
                (seat, second.id),
            )


@needs_db
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("id", "document"),
        ("type_version", 0),
        ("write_revision", 0),
        ("name_key", "n" * 4001),
        ("search_key", "s" * 8001),
        ("type", "t" * 65),
    ],
)
def test_the_database_refuses_a_document_row_the_application_would_never_mint(
    dsn: str, column: str, value: object
) -> None:
    """Every bound the store checks in Python is also the database's, so a path
    that forgot one is refused rather than storing something the twin would
    never hold."""
    row = {
        "id": DOCUMENT, "campaign_id": CAMPAIGN, "type": "npc", "type_version": 1,
        "data": "{}", "write_revision": 1, "field_revisions": "{}",
        "name_key": "", "search_key": "",
    }
    row[column] = value
    _a_seeded_world(dsn)
    with connect(dsn) as conn:
        with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.StringDataRightTruncation)):
            conn.execute(
                "INSERT INTO campaign.documents (id, campaign_id, type, type_version, data, "
                "write_revision, field_revisions, name_key, search_key) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)",
                tuple(row.values()),
            )


@needs_db
@pytest.mark.parametrize(
    ("column", "value"),
    [("number", 0), ("author", "player"), ("summary", "s" * 201), ("restored_from", 2)],
)
def test_the_database_refuses_a_version_row_the_application_would_never_mint(
    dsn: str, column: str, value: object
) -> None:
    """`restored_from` is checked against the version's own number, so the
    database refuses a restore from the future as the wire model does — the one
    invariant slice B's `restore` will lean on."""
    world = _a_seeded_world(dsn)
    made = _a_document(world, CAMPAIGN)
    row = {
        "document_id": made.id, "number": 2, "author": "gm", "summary": "",
        "changed_fields": "[]", "restored_from": None, "data": "{}",
    }
    row[column] = value
    with connect(dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO campaign.document_versions (document_id, number, author, summary, "
                "changed_fields, restored_from, data, created_at, updated_at, sealed_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, now(), now(), now())",
                tuple(row.values()),
            )


@needs_db
def test_deleting_a_campaign_takes_its_documents_and_their_versions(dsn: str) -> None:
    """SEC-36, for this bead's two tables. The wider cascade of every campaign
    table is `tests/test_migrations_db.py`'s and belongs to slice B with the
    rest of requirement 10; this proves the two edges 0008 itself adds."""
    world = _a_seeded_world(dsn)
    made = _a_document(world, CAMPAIGN)
    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM campaign.document_versions").fetchone()[0] == 1
        conn.execute("DELETE FROM campaign.documents WHERE id = %s", (made.id,))
        assert conn.execute("SELECT count(*) FROM campaign.document_versions").fetchone()[0] == 0

    _a_document(world, CAMPAIGN)
    with connect(dsn) as conn:
        for table in ("campaign.documents", "campaign.document_versions"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1, table
        conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (CAMPAIGN,))
        for table in ("campaign.documents", "campaign.document_versions"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


@needs_db
def test_a_participant_deleted_by_raw_sql_clears_the_link_and_keeps_the_document(
    dsn: str,
) -> None:
    """`ON DELETE SET NULL` rather than CASCADE, which would destroy the sheet
    AUD-16 says Remove keeps. It never fires in the application, because
    participants are MARKED removed and never deleted (RQ-3) — it is there so
    the campaign-deletion cascade cannot fail."""
    world = _a_seeded_world(dsn)
    seat = _a_participant(world, CAMPAIGN)
    sheet = _a_document(world, CAMPAIGN, data=dict(A_CHARACTER_SHEET),
                        doc_type=DocumentTypeId.CHARACTER_SHEET)
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE campaign.documents SET linked_participant_id = %s WHERE id = %s",
            (seat, sheet.id),
        )
        conn.execute("DELETE FROM campaign.participants WHERE id = %s", (seat,))
        row = conn.execute(
            "SELECT linked_participant_id FROM campaign.documents WHERE id = %s", (sheet.id,)
        ).fetchone()
    assert row is not None and row[0] is None, "the link went; the document stayed"


@needs_db
def test_the_one_known_parity_gap_is_pinned(dsn: str) -> None:
    """The twin stores a U+0000 in a field value; PostgreSQL refuses it from
    `data JSONB`, because `jsonb` cannot represent it.

    `check_fields` accepts it today: `_well_formed` only tries
    `value.encode("utf-8")`, which U+0000 passes, and `_ListItem` has no
    `_well_formed` at all. **This is the ONE divergence this bead is allowed to
    have, and it is pinned rather than hidden.** F-9 closes it in `1kg.5.7`, and
    both halves of this test change together when it does. A refusal is
    deliberately NOT added to the store here.

    Pinned by exception class and SQLSTATE, never by the driver's message —
    that message's DETAIL quotes the failing row (SEC-20).
    """
    twin = InMemoryDatabase()
    fake_campaign = _a_fake_world(twin)
    stored = _a_document(fake_campaign, _a_campaign(fake_campaign),
                         data={"name": "Vashti\x00", "qualifier": "", "tags": ["x\x00y"]})
    assert stored.data["name"] == "Vashti\x00", "the twin keeps it"

    world = _a_seeded_world(dsn)
    with pytest.raises(psycopg.errors.DataError) as refused:
        _a_document(world, CAMPAIGN,
                    data={"name": "Vashti\x00", "qualifier": "", "tags": ["x\x00y"]})
    assert refused.value.sqlstate == PG_NUL_IN_JSONB


def _a_fake_world(db: InMemoryDatabase) -> World:
    return World(
        "fake", db, InMemoryCampaignStore(db), InMemoryParticipantStore(db),
        InMemoryDocumentStore(db), owner=1,
    )


def _a_seeded_world(dsn: str, settings: CampaignLockSettings = QUICK) -> World:
    """A GM and one campaign at a known id, by raw SQL — the AFTER INSERT
    trigger writes `authz_state`, so this is also how these tests get one."""
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') "
            "RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
            (CAMPAIGN, owner),
        )
    return World(
        "postgres", _database(dsn, settings), PostgresCampaignStore(),
        PostgresParticipantStore(), PostgresDocumentStore(), owner=int(owner),
    )


# ── Races, with two connections and the server's own account of the wait ─────


def _someone_waits_on_a_lock(dsn: str, patience: float = PATIENCE) -> bool:
    """Whether a backend on this database is blocked — the server's own account
    of it, so the test does not merely assume the race went its way."""
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
    dsn: str,
    db: Database,
    holder_work: Callable[[Any], None],
    waiter_work: Callable[[Any], Any],
) -> Any:
    """Run `holder_work` in one transaction, start `waiter_work` in a second,
    wait until the SERVER says it is blocked, then let the first commit and
    return what the second produced.

    A deadlock would show up as `40P01` coming back from one of the two.
    """
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
def test_the_second_of_two_writes_waits_for_the_row_and_then_sees_the_firsts_revision(
    dsn: str,
) -> None:
    """Two autosaves can never both claim the same write revision: the second
    waits for the first's `FOR NO KEY UPDATE` and then reads what it
    committed."""
    world = _a_seeded_world(dsn, PATIENT)
    made = _a_document(world, CAMPAIGN)
    store = world.documents

    def first(unit: Any) -> None:
        store.write_fields(unit, CAMPAIGN, made.id, fields={"voice": "gravel"},
                           author=Author.GM, base_write_revision=1)

    def second(unit: Any) -> DocumentRecord:
        return store.write_fields(unit, CAMPAIGN, made.id, fields={"tell": "taps"},
                                  author=Author.GM, base_write_revision=None)

    produced = _while_another_transaction_holds(dsn, world.db, first, second)
    assert produced.write_revision == 3, "the waiter advanced from the value it waited for"
    assert produced.data["voice"] == "gravel", "and merged over what the holder wrote"
    assert len(_versions(world, CAMPAIGN, made.id)) == 1, "both went into the open version"


@needs_db
def test_two_creates_with_one_command_id_leave_exactly_one_document(dsn: str) -> None:
    """`documents_command_uidx` is what makes idempotent create race-safe: the
    second insert waits on the first's speculative row and then reports the
    document already made, rather than both succeeding."""
    world = _a_seeded_world(dsn, PATIENT)
    store = world.documents

    def make(unit: Any) -> DocumentRecord:
        return store.create(unit, CAMPAIGN, doc_type=DocumentTypeId.NPC, type_version=1,
                            data=dict(AN_NPC), author=Author.GM, command_id="cmd-1")

    first: list[DocumentRecord] = []

    def holder(unit: Any) -> None:
        first.append(make(unit))

    produced = _while_another_transaction_holds(dsn, world.db, holder, make)
    assert produced.id == first[0].id
    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM campaign.documents").fetchone()[0] == 1


@needs_db
@pytest.mark.parametrize("mode", ["FOR NO KEY UPDATE", "FOR UPDATE"])
def test_only_the_stronger_participant_lock_blocks_a_document_that_references_it(
    dsn: str, mode: str
) -> None:
    """RC-14 in this bead's shape. A foreign-key check on the participant takes
    `FOR KEY SHARE`, which conflicts with `FOR UPDATE` alone — so the stronger
    lock would make linking a sheet wait behind an unrelated hold, and two of
    them would deadlock. This is why every explicit row lock in the campaign
    schema is `FOR NO KEY UPDATE` (RQ-3), and why it is proved on a server
    rather than reasoned about."""
    world = _a_seeded_world(dsn, PATIENT)
    seat = _a_participant(world, CAMPAIGN)
    sheet = _a_document(world, CAMPAIGN, data=dict(A_CHARACTER_SHEET),
                        doc_type=DocumentTypeId.CHARACTER_SHEET)
    blocked: list[bool] = []

    def holder(unit: Any) -> None:
        unit.conn.execute(
            f"SELECT id FROM campaign.participants WHERE id = %s {mode}", (seat,)
        ).fetchone()

    def linker(unit: Any) -> bool:
        unit.conn.execute(
            "UPDATE campaign.documents SET linked_participant_id = %s WHERE id = %s",
            (seat, sheet.id),
        )
        return True

    if mode == "FOR NO KEY UPDATE":
        # The reference goes through while the hold is still open, which is the
        # whole point: no `_someone_waits_on_a_lock` here, because nobody waits.
        took, release = threading.Event(), threading.Event()

        def hold_then_wait() -> None:
            with world.db.transaction() as unit:
                holder(unit)
                took.set()
                release.wait(PATIENCE)

        thread = threading.Thread(target=hold_then_wait, daemon=True)
        thread.start()
        assert took.wait(PATIENCE)
        try:
            with world.db.transaction() as unit:
                blocked.append(not linker(unit))
        finally:
            release.set()
            thread.join(PATIENCE)
        assert blocked == [False], "FOR NO KEY UPDATE must not block the reference"
        return

    assert _while_another_transaction_holds(dsn, world.db, holder, linker) is True


# ── Slice B: archive, delete, restore, the library and the sheet link ────────
#
# Appended in sections of its own. Nothing above is edited: the U+0000 parity
# test is `1kg.5.7.2`'s to change, and no test below stores a control character
# through the store (that bead refuses them).


# Archive (LIB-16) ────────────────────────────────────────────────────────────


def test_archiving_reports_whether_a_row_changed_so_an_undo_is_idempotent(
    world: World,
) -> None:
    """The shape of `CampaignStore.set_archived`: the Undo toast and the
    Archived filter's Restore can both be pressed twice. Archiving is not an
    edit, so it moves neither `updated_at` (the Recent order) nor the write
    revision nor the history."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    moment = made.updated_at + timedelta(hours=1)

    with world.db.transaction() as unit:
        assert world.documents.set_archived(unit, campaign, made.id, archived=True, now=moment)
        assert not world.documents.set_archived(
            unit, campaign, made.id, archived=True, now=moment + timedelta(minutes=1)
        )
        archived = world.documents.get(unit, campaign, made.id)
    assert archived is not None
    assert archived.archived_at == moment, "the second archive did not re-stamp the row"
    assert archived.updated_at == made.updated_at, "archiving does not move the Recent order"
    assert archived.write_revision == made.write_revision and archived.version == made.version

    with world.db.transaction() as unit:
        assert world.documents.set_archived(unit, campaign, made.id, archived=False, now=moment)
        assert not world.documents.set_archived(unit, campaign, made.id, archived=False)
        restored = world.documents.get(unit, campaign, made.id)
    assert restored is not None
    assert restored.archived_at is None and restored.updated_at == made.updated_at


def test_archiving_a_missing_or_foreign_document_reports_that_nothing_changed(
    world: World,
) -> None:
    """`False`, the `CampaignStore.set_archived` precedent, and never a refusal
    that would tell another GM's document apart from one that is not there."""
    mine = _a_campaign(world)
    theirs = _a_campaign(world, name="Someone else's")
    made = _a_document(world, mine)

    with world.db.transaction() as unit:
        assert world.documents.set_archived(unit, theirs, made.id, archived=True) is False
        assert world.documents.set_archived(unit, mine, DOCUMENT, archived=True) is False
        after = world.documents.get(unit, mine, made.id)
    assert after is not None and after.archived_at is None


def test_an_archive_flag_that_is_not_a_bool_is_refused(world: World) -> None:
    """`LibraryQuery.archived` is a `StrictBool`; a truthy string must not
    archive in one world and be refused by a driver in the other."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="archived is true or false"):
            world.documents.set_archived(unit, campaign, made.id, archived="yes")


def test_an_archived_document_is_still_writable(world: World) -> None:
    """Lead ruling 5.1#1: LIB-16 keeps an archived document open under a
    Restore banner and never calls it read-only. The route decides."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    with world.db.transaction() as unit:
        world.documents.set_archived(unit, campaign, made.id, archived=True)
    written = _write(world, campaign, made.id, fields={"voice": "gravel"},
                     author=Author.GM, base_write_revision=None)
    assert written.data["voice"] == "gravel" and written.archived_at is not None


# Delete (LIB-18) ─────────────────────────────────────────────────────────────


def test_deleting_a_document_takes_its_whole_history_and_reports_whether_it_did(
    world: World,
) -> None:
    """LIB-18: "This permanently deletes `<title>` and its whole history." There
    is no soft delete; archiving is the reversible state. Afterwards every
    reader answers as it does for a document that never existed — and the
    command id went with the row, so a replay cannot hand the deleted document
    back."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign, command_id="cmd-1")
    _write(world, campaign, made.id, fields={"voice": "gravel"},
           author=Author.ASSISTANT, base_write_revision=None)
    kept = _a_document(world, campaign)

    with world.db.transaction() as unit:
        assert world.documents.delete(unit, campaign, made.id) is True
    with world.db.transaction() as unit:
        assert world.documents.delete(unit, campaign, made.id) is False, "nothing left to delete"
        assert world.documents.get(unit, campaign, made.id) is None
        assert world.documents.hold(unit, campaign, made.id) is None
        assert world.documents.seal(unit, campaign, made.id) is None
        assert world.documents.snapshot(unit, campaign, made.id, 1) is None
        assert world.documents.history(unit, campaign, made.id, before_number=None, limit=5) == []
        assert world.documents.set_archived(unit, campaign, made.id, archived=True) is False
        with pytest.raises(MissingParent):
            world.documents.write_fields(unit, campaign, made.id, fields={"voice": "x"},
                                         author=Author.GM, base_write_revision=None)
        assert world.documents.get(unit, campaign, kept.id) is not None, "only that one went"

    again = _a_document(world, campaign, command_id="cmd-1")
    assert again.id != made.id and again.write_revision == 1


def test_deleting_another_campaigns_document_changes_nothing(world: World) -> None:
    """SEC-2: one statement names the document AND the campaign, so another
    GM's document is as unreachable as one that does not exist."""
    mine = _a_campaign(world)
    theirs = _a_campaign(world, name="Someone else's")
    made = _a_document(world, mine)

    with world.db.transaction() as unit:
        assert world.documents.delete(unit, theirs, made.id) is False
        assert world.documents.delete(unit, mine, DOCUMENT) is False
    assert len(_versions(world, mine, made.id)) == 1


# Restore a VERSION (CANVAS-26, B-5, B-6) — not to be confused with LIB-16's
# un-archive, which is `set_archived(archived=False)` above ─────────────────────


def _restore(world: World, campaign_id: str, document_id: str, **kwargs: Any) -> DocumentRecord:
    with world.db.transaction() as unit:
        return world.documents.restore(unit, campaign_id, document_id, **kwargs)


def _three_versions(world: World, campaign_id: str) -> DocumentRecord:
    """Version 1 is AN_NPC, sealed by an assistant write that is version 2, and
    version 3 is the GM's open working version — so a restore has something to
    seal first and a key (`tell`) the chosen version does not have."""
    made = _a_document(world, campaign_id)
    _write(world, campaign_id, made.id, fields={"voice": "gravel", "tell": "taps"},
           author=Author.ASSISTANT, base_write_revision=None)
    return _write(world, campaign_id, made.id, fields={"name": "Vashti the Broker"},
                  author=Author.GM, base_write_revision=None)


def test_a_restore_appends_a_new_sealed_version_equal_to_the_chosen_one(world: World) -> None:
    """CANVAS-26: "Restore is additive: it appends a new version whose content
    equals the chosen one." It seals the open version first, deletes nothing,
    and advances the write revision and the field revision of every key it
    changed — including a key the chosen version does not have, which is a
    change like any other to a browser holding an older base."""
    campaign = _a_campaign(world)
    before = _three_versions(world, campaign)
    moment = before.updated_at + timedelta(minutes=3)

    restored = _restore(world, campaign, before.id, version_number=1, now=moment)

    assert restored.data == AN_NPC
    assert restored.write_revision == before.write_revision + 1
    revision = restored.write_revision
    assert restored.field_revisions == {
        **before.field_revisions, "name": revision, "voice": revision, "tell": revision,
    }
    assert restored.updated_at == moment
    version = restored.version
    assert (version.number, version.restored_from, version.author) == (4, 1, Author.GM.value)
    assert version.summary == "" and version.sealed_at == moment, "sealed as it is written"
    assert version.changed_fields == ("name", "tell", "voice")

    history = _versions(world, campaign, before.id)
    assert [v.number for v in history] == [1, 2, 3, 4], "nothing is moved or deleted"
    assert history[2].sealed_at == moment, "the open version was sealed first"
    assert all(v.sealed_at is not None for v in history)
    with world.db.transaction() as unit:
        appended = world.documents.snapshot(unit, campaign, before.id, 4)
    assert appended is not None and appended.data == AN_NPC
    assert _folded_keys(world, before.id) == (name_key(AN_NPC["name"]), search_key(AN_NPC))


def test_restoring_content_the_document_already_has_changes_nothing(world: World) -> None:
    """B-5 (the lead's ruling of 2026-09-26, over ruling 5.1#3): a restore whose
    chosen content equals the live content, compared under the row lock, is a
    no-op — no seal, no revision advance, no version — and hands the record back
    unchanged. So Restore pressed twice appends once, and restoring the current
    version appends nothing. `RestoreRequest` says the same: "restoring what the
    document already equals changes nothing"."""
    campaign = _a_campaign(world)
    before = _three_versions(world, campaign)

    assert _restore(world, campaign, before.id, version_number=3) == before, "the current one"
    once = _restore(world, campaign, before.id, version_number=1)
    twice = _restore(world, campaign, before.id, version_number=1,
                     now=once.updated_at + timedelta(hours=1))

    assert twice == once
    assert [v.number for v in _versions(world, campaign, before.id)] == [1, 2, 3, 4]


def test_an_equal_content_restore_leaves_the_open_version_open(world: World) -> None:
    """"No seal" is observable: the GM's working version stays the one their
    next autosave joins."""
    campaign = _a_campaign(world)
    made = _a_document(world, campaign)
    _write(world, campaign, made.id, fields={"voice": "gravel"},
           author=Author.ASSISTANT, base_write_revision=None)
    back = _write(world, campaign, made.id, fields={"voice": AN_NPC["voice"]},
                  author=Author.GM, base_write_revision=None)
    assert back.data == AN_NPC and back.version.sealed_at is None

    unchanged = _restore(world, campaign, made.id, version_number=1)

    assert unchanged == back
    assert unchanged.version.number == 3 and unchanged.version.sealed_at is None


def test_restoring_a_version_that_is_not_there_is_refused_and_writes_nothing(
    world: World,
) -> None:
    """A number that names no version, a document of another campaign, and one
    that does not exist are one refusal (SEC-2) — and the target is read before
    anything is sealed, so a refused restore leaves the open version open.

    The refusals are caught INSIDE a transaction that then commits: the
    refusal is raised in Python, with no failed statement, so the caller's
    transaction stays usable — and a seal taken before the read would commit
    with it, which is what this test would see."""
    mine = _a_campaign(world)
    theirs = _a_campaign(world, name="Someone else's")
    before = _three_versions(world, mine)

    with world.db.transaction() as unit:
        for campaign, document, number in (
            (mine, before.id, 99), (theirs, before.id, 1), (mine, DOCUMENT, 1),
        ):
            with pytest.raises(MissingParent):
                world.documents.restore(unit, campaign, document, version_number=number)

    with world.db.transaction() as unit:
        after = world.documents.get(unit, mine, before.id)
    assert after == before
    assert after.version.sealed_at is None


def test_a_restore_to_a_document_stored_at_an_older_type_version_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed, and first (B-6 step 2): the stale type is refused before the
    chosen version is even read, so a missing number cannot mask it."""
    campaign = _a_campaign(world)
    before = _three_versions(world, campaign)
    monkeypatch.setitem(wire.DOC_TYPE_VERSION, DocumentTypeId.NPC, 2)

    for number in (1, 99):
        with pytest.raises(StaleTypeVersion):
            _restore(world, campaign, before.id, version_number=number)


def test_a_restore_whose_chosen_content_no_longer_validates_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-6 step 4: the chosen content is validated as a whole document, required
    keys enforced, before anything is sealed or stored — a version written under
    an older rule is not a way around the current one."""
    campaign = _a_campaign(world)
    before = _three_versions(world, campaign)
    required = wire.REQUIRED_FIELDS[DocumentTypeId.NPC] | {"tell"}
    monkeypatch.setitem(wire.REQUIRED_FIELDS, DocumentTypeId.NPC, required)

    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="require tell"):
            world.documents.restore(unit, campaign, before.id, version_number=1)

    with world.db.transaction() as unit:
        after = world.documents.get(unit, campaign, before.id)
    assert after == before and after.version.sealed_at is None


# The library (LIB-20 to LIB-23, B-11, B-12, B-13) ────────────────────────────


def _category(category: wire.LibraryCategory) -> list[str]:
    """A library category is a SET of types, read from the registry (LIB-3)."""
    return sorted(
        kind.value for kind, owner in wire.DOC_TYPE_LIBRARY_CATEGORY.items() if owner is category
    )


NPCS = _category(wire.LibraryCategory.NPCS)
DOCUMENTS = _category(wire.LibraryCategory.DOCUMENTS)


def _library(world: World, campaign_id: str, **kwargs: Any) -> list[Any]:
    kwargs.setdefault("types", NPCS)
    kwargs.setdefault("archived", False)
    kwargs.setdefault("limit", wire.LIBRARY_PAGE_MAX_ITEMS)
    with world.db.transaction() as unit:
        return world.documents.list_documents(unit, campaign_id, **kwargs)


def _ids(rows: list[Any]) -> list[str]:
    return [row.id for row in rows]


def _named(world: World, campaign_id: str, name: str, **kwargs: Any) -> DocumentRecord:
    data = {"name": name, "qualifier": "", "tags": []} | kwargs.pop("extra", {})
    return _a_document(world, campaign_id, data=data, **kwargs)


def _recent_order(records: list[DocumentRecord]) -> list[str]:
    """Newest first, the id breaking a tie in code-point order: what
    `ORDER BY updated_at DESC, id COLLATE "C"` means, computed independently."""
    by_id = sorted(records, key=lambda record: record.id)
    return [record.id for record in sorted(by_id, key=lambda r: r.updated_at, reverse=True)]


#: Non-ASCII spelled by code point, so that no test source carries a character
#: an editor might silently normalise.
E_ACUTE_CAPITAL = chr(0xC9)
OMEGA_CAPITAL = chr(0x3A9)
FULLWIDTH_ANA = "".join(chr(code) for code in (0xFF21, 0xFF4E, 0xFF41))
ODYSSEUS_CAPITALS = "".join(chr(code) for code in (0x39F, 0x394, 0x3A5, 0x3A3, 0x3A3, 0x395, 0x3A5, 0x3A3))
ODYSSEUS_FINAL_SIGMA = "".join(
    chr(code) for code in (0x3BF, 0x3B4, 0x3C5, 0x3C3, 0x3C3, 0x3B5, 0x3C5, 0x3C2)
)
DOTTED_CAPITAL_I = chr(0x130)
COMBINING_DOT_ABOVE = chr(0x307)
ZERO_WIDTH_SPACE = chr(0x200B)


def test_the_library_lists_one_category_newest_first_with_the_id_breaking_ties(
    world: World,
) -> None:
    """LIB-22's default sort, and a stable TOTAL order: documents written in the
    same instant are told apart by id, identically in both worlds."""
    campaign = _a_campaign(world)
    other = _a_campaign(world, name="Someone else's")
    start = datetime.now(UTC)
    moments = [start, start + timedelta(seconds=1), start + timedelta(seconds=1),
               start + timedelta(seconds=1), start + timedelta(seconds=2)]
    listed = [_named(world, campaign, f"npc {index}", now=moment)
              for index, moment in enumerate(moments)]
    _a_document(world, campaign, data=A_STATBLOCK, doc_type=DocumentTypeId.STATBLOCK,
                now=start + timedelta(seconds=9))
    _named(world, other, "not mine", now=start + timedelta(seconds=9))

    assert _ids(_library(world, campaign)) == _recent_order(listed)


def test_the_name_sort_is_the_folded_name_in_code_point_order(world: World) -> None:
    """LIB-22's `Name A-Z` over `name_key`, collated "C" in PostgreSQL and
    compared by code point in Python, so the worlds agree by construction. A
    capital E with an acute sorts after `zeta` here, where a linguistic
    collation would put it before: exactly the disagreement "C" rules out."""
    campaign = _a_campaign(world)
    emile, omega = E_ACUTE_CAPITAL + "mile", OMEGA_CAPITAL + "mega"
    names = ["beta", "Alpha", "ALPHA", emile, "zeta", omega]
    made = {name: _named(world, campaign, name) for name in names}
    alphas = sorted([made["Alpha"].id, made["ALPHA"].id])

    shown = _library(world, campaign, sort=wire.LibrarySort.NAME)

    assert _ids(shown) == [
        *alphas, made["beta"].id, made["zeta"].id, made[emile].id, made[omega].id,
    ]


def test_active_or_archived_is_a_filter_and_never_both(world: World) -> None:
    """LIB-22: "Active or Archived everywhere". There is no query for both."""
    campaign = _a_campaign(world)
    kept = _named(world, campaign, "kept")
    shelved = _named(world, campaign, "shelved")
    with world.db.transaction() as unit:
        world.documents.set_archived(unit, campaign, shelved.id, archived=True)

    assert _ids(_library(world, campaign)) == [kept.id]
    archived = _library(world, campaign, archived=True)
    assert _ids(archived) == [shelved.id] and archived[0].archived_at is not None
    assert _ids(_library(world, campaign, archived=True, sort=wire.LibrarySort.NAME)) == [
        shelved.id
    ]


def test_the_documents_category_narrows_to_one_type(world: World) -> None:
    """LIB-3 puts five types in Documents and LIB-22 lets that one category be
    narrowed to a single type; `type` is a filter, never an index key."""
    campaign = _a_campaign(world)
    made = {
        kind: _named(world, campaign, kind.value, doc_type=kind)
        for kind in (DocumentTypeId.HANDOUT, DocumentTypeId.LORE, DocumentTypeId.QUEST_LOG)
    }
    _named(world, campaign, "an npc")

    assert sorted(_ids(_library(world, campaign, types=DOCUMENTS))) == sorted(
        record.id for record in made.values()
    )
    assert _ids(_library(world, campaign, types=[DocumentTypeId.LORE])) == [
        made[DocumentTypeId.LORE].id
    ]


def _every_page(world: World, campaign_id: str, size: int, **kwargs: Any) -> list[list[str]]:
    pages: list[list[str]] = []
    after: str | None = None
    while True:
        page = _ids(_library(world, campaign_id, limit=size, after_id=after, **kwargs))
        if not page:
            return pages
        pages.append(page)
        after = page[-1]


@pytest.mark.parametrize("sort", list(wire.LibrarySort))
def test_keyset_paging_crosses_a_page_boundary_without_a_gap_or_a_repeat(
    world: World, sort: wire.LibrarySort
) -> None:
    """B-12. The anchor is a document id and nothing else (inferred decision
    13: a name in a cursor would carry private text to the client), and its
    sort key is looked up server-side. Three rows tie on the sort key and
    straddle the first page boundary, which is where a keyset predicate that
    forgot the tiebreaker would skip or repeat one."""
    campaign = _a_campaign(world)
    start = datetime.now(UTC)
    tied = start + timedelta(seconds=1)
    specs = [("a", start), ("same", tied), ("same", tied), ("same", tied), ("z", start)]
    made = [_named(world, campaign, name, now=moment) for name, moment in specs]

    whole = _ids(_library(world, campaign, sort=sort))
    pages = _every_page(world, campaign, 2, sort=sort)

    if sort is wire.LibrarySort.RECENT:
        assert whole == _recent_order(made)
    else:
        assert whole == [made[0].id, *sorted(r.id for r in made[1:4]), made[4].id]
    assert [len(page) for page in pages] == [2, 2, 1]
    assert [row for page in pages for row in page] == whole


def test_a_library_cursor_that_names_no_row_of_that_campaign_is_refused(
    world: World,
) -> None:
    """Never a silent restart at page one, which would loop for ever. A
    document of another campaign is refused exactly as a missing one is, and so
    is a deleted one: in the twin, too, where it lingers as a tombstone."""
    campaign = _a_campaign(world)
    other = _a_campaign(world, name="Someone else's")
    _named(world, campaign, "listed")
    theirs = _named(world, other, "theirs")
    gone = _named(world, campaign, "gone")
    with world.db.transaction() as unit:
        world.documents.delete(unit, campaign, gone.id)

    for anchor in (DOCUMENT, theirs.id, gone.id):
        with pytest.raises(UnknownCursor) as refused:
            _library(world, campaign, after_id=anchor)
        assert refused.value.kind == "library"


def test_a_library_row_carries_the_listed_keys_and_no_body(world: World) -> None:
    """Three keys read out of the JSON (`data->>'name'`, `data->>'qualifier'`,
    `data->'tags'`), so a list page never loads a 1.3 MB body (F-10). The row
    is the store's, not the wire's `LibraryItem`: the store validates none of
    it, and a missing qualifier is None rather than a guess."""
    campaign = _a_campaign(world)
    npc = _a_document(world, campaign)
    lore = _a_document(world, campaign, data={"name": "The Drowned Bell"},
                       doc_type=DocumentTypeId.LORE)

    [row] = _library(world, campaign)
    assert (row.id, row.type, row.type_version) == (npc.id, "npc", 1)
    assert (row.name, row.qualifier, row.tags) == ("Vashti", "Harbourmistress", ("harbour",))
    assert (row.archived_at, row.updated_at) == (None, npc.updated_at)
    assert not hasattr(row, "data") and not hasattr(row, "voice")
    [bare] = _library(world, campaign, types=[DocumentTypeId.LORE])
    assert (bare.id, bare.name, bare.qualifier, bare.tags) == (
        lore.id, "The Drowned Bell", None, (),
    )


# Search (LIB-20, requirement 8, B-13) ─────────────────────────────────────────


def _found(world: World, campaign_id: str, term: str, **kwargs: Any) -> list[str]:
    return _ids(_library(world, campaign_id, search=term, **kwargs))


def test_a_search_matches_across_a_fold_only_difference_in_both_worlds(
    world: World,
) -> None:
    """Folded by the application on both sides (the term with the same `_fold`
    as the stored key) and compared case-SENSITIVELY with `strpos`, because
    both sides are already folded. `lower()` would answer by the database's
    collation provider and disagree with Python about a final sigma and a
    dotted capital I, silently."""
    campaign = _a_campaign(world)
    wide = _named(world, campaign, FULLWIDTH_ANA + " the Lamplighter")
    sigma = _named(world, campaign, ODYSSEUS_CAPITALS)
    dotted = _named(world, campaign, DOTTED_CAPITAL_I + "zmir Docks")

    assert _found(world, campaign, "ana") == [wide.id]
    assert _found(world, campaign, ODYSSEUS_FINAL_SIGMA) == [sigma.id]
    assert _found(world, campaign, "i" + COMBINING_DOT_ABOVE + "zmir") == [dotted.id]


def test_a_search_matches_name_qualifier_and_tags_and_never_the_body(world: World) -> None:
    """LIB-20 names three keys. Never the body, never `notes`, and never
    `npc.true_identity`: an identity link is a GM-only relation and never
    enters any index (ED-20)."""
    campaign = _a_campaign(world)
    made = _a_document(
        world, campaign,
        data={"name": "Vashti", "qualifier": "Broker", "tags": ["harbour"],
              "notes": "keeps the ledger", "true_identity": "the Archivist"},
    )

    for term in ("vashti", "broker", "harbour"):
        assert _found(world, campaign, term) == [made.id], term
    for term in ("ledger", "archivist"):
        assert _found(world, campaign, term) == [], term


def test_a_search_term_cannot_match_across_a_field_boundary(world: World) -> None:
    """The separator U+001F is what keeps the tags `alpha` and `beta` from
    reading as `alpha beta`. A term that CONTAINS U+001F cannot reach it: the
    fold turns it into a space before matching, in both worlds — otherwise the
    term would find the two separate tags, whose stored key holds exactly
    `alpha`, U+001F, `beta`. Nothing here stores a control character;
    `1kg.5.7.2` refuses them in field text."""
    campaign = _a_campaign(world)
    apart = _named(world, campaign, "x", extra={"tags": ["alpha", "beta"]})
    together = _named(world, campaign, "y", extra={"tags": ["alpha beta"]})
    assert "alpha" + chr(0x1F) + "beta" in _folded_keys(world, apart.id)[1]

    assert _found(world, campaign, "alpha" + chr(0x1F) + "beta") == [together.id]
    assert _found(world, campaign, "alpha beta") == [together.id]
    assert sorted(_found(world, campaign, "alpha")) == sorted([apart.id, together.id])


def test_a_search_term_is_matched_literally_and_never_as_a_pattern(world: World) -> None:
    """`strpos` is not a pattern, so nothing needs escaping. With `LIKE`, a GM
    searching `50%` would match every document starting `50`, `a_b` would match
    `axb`, and a backslash would escape whatever followed it."""
    campaign = _a_campaign(world)
    backslash = "back" + chr(0x5C) + "slash"
    made = {name: _named(world, campaign, name)
            for name in ("50% off", "5000 crowns", "a_b", "axb", backslash, "backslash")}

    assert _found(world, campaign, "50%") == [made["50% off"].id]
    assert _found(world, campaign, "a_b") == [made["a_b"].id]
    assert _found(world, campaign, "k" + chr(0x5C) + "s") == [made[backslash].id]


def test_a_document_with_the_most_tags_saves_and_is_found_by_its_name(world: World) -> None:
    """`tags` admits 100 items of 2,000 characters, so `search_key` is
    TRUNCATED at `SEARCH_KEY_MAX` rather than refused: refusing would make a
    legal document unsaveable. Truncation loses tags first and never the name.
    The cost is the known limit of lead ruling 5.1#4, pinned here so that it
    cannot change unnoticed: a tag past the bound is not found."""
    campaign = _a_campaign(world)
    tags = ["t" * wire.LIST_ITEM_MAX_CHARS] * (wire.LIST_FIELD_MAX_ITEMS - 1) + ["zeppelin"]
    made = _named(world, campaign, "Vashti", extra={"tags": tags})

    assert len(_folded_keys(world, made.id)[1]) == SEARCH_KEY_MAX
    assert _found(world, campaign, "vashti") == [made.id]
    assert _found(world, campaign, "zeppelin") == [], "the known limit, and only that"


def test_a_search_that_folds_to_nothing_adds_no_clause(world: World) -> None:
    """The caller rejects a search under `SEARCH_MIN_CHARS`; the store treats a
    term with nothing left after folding as no search at all."""
    campaign = _a_campaign(world)
    made = _named(world, campaign, "Vashti")
    for term in ("", "   ", ZERO_WIDTH_SPACE):
        assert _found(world, campaign, term) == [made.id], repr(term)


def test_a_library_query_it_cannot_honour_is_refused_without_quoting_it(
    world: World,
) -> None:
    """Refused before any statement runs, identically in both worlds, naming
    the field and never the value: a search string is private text (SEC-20)."""
    campaign = _a_campaign(world)
    private = "vashtizzle" * 11
    with pytest.raises(ValueError, match="search is at most") as refused:
        _library(world, campaign, search=private)
    assert private not in f"{refused.value!s}{refused.value!r}"
    for bad in ({"types": []}, {"types": ["nonesuch"]}, {"sort": "oldest"},
                {"limit": 0}, {"limit": wire.LIBRARY_PAGE_MAX_ITEMS + 1},
                {"archived": "no"}):
        with pytest.raises(ValueError):
            _library(world, campaign, **bad)

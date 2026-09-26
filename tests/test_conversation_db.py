"""The conversation store, against the twin and against a real PostgreSQL (1kg.2.4).

One behavioural suite, run twice: once on `InMemoryConversationStore` and once
on `PostgresConversationStore`. A rule only one of them keeps is a rule the two
will drift apart on, and the drift shows up in the bead that composes them, not
here. The `postgres` parameter carries `needs_db` and skips without a server;
the `fake` parameter runs anywhere.

Below the shared suite are the tests **no fake can prove**: that a campaign
refused inside the INSERT never comes back as a `ForeignKeyViolation` at COMMIT
(the whole point of validating in the statement, 0006's edge being DEFERRABLE
INITIALLY DEFERRED), that `create` leaves the model-routing columns NULL so the
first `/chat` turn still binds affinity, that a committed row is visible from a
second connection, and who **blocks** whom when two requests race for the same
conversation.

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_conversation_db.py -q
"""

from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import conversations_api
from service import migrations as mig
from service.campaign_store import InMemoryCampaignStore, MissingParent, PostgresCampaignStore
from service.campaign_store import shared_rows as twin_table
from service.conversation_store import (
    Conversation,
    InMemoryConversationStore,
    InvalidCursor,
    PostgresConversationStore,
    new_conversation_id,
)
from service.db import Database, InMemoryDatabase, PoolSettings
from service.history import PostgresMessageStore
from service.model_catalog import CATALOG_REVISION, enabled_profiles

#: A fixed clock, so a test can place rows in a known order without sleeping.
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
#: How long a test waits for another thread before calling it a hang.
PATIENCE = 15


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("conversation") as target:
        mig.migrate(target)
        yield target


def _database(dsn: str) -> Database:
    return Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))


# ── The shared behavioural suite ─────────────────────────────────────────────


@dataclass
class World:
    """A conversation store and a campaign store over one database, and two GMs."""

    kind: str
    db: Any
    campaigns: Any
    conversations: Any
    owner: int
    other_owner: int
    #: Only for the `postgres` world: what a raw-SQL legacy row is inserted through.
    dsn: str | None = None


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db = InMemoryDatabase()
        yield World(
            "fake",
            db,
            InMemoryCampaignStore(db),
            InMemoryConversationStore(db),
            owner=1,
            other_owner=2,
        )
        return

    target = request.getfixturevalue("dsn")
    with connect(target) as conn:
        gms = [
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
                (email,),
            ).fetchone()[0]
            for email in ("gm@example.com", "other@example.com")
        ]
    yield World(
        "postgres",
        _database(target),
        PostgresCampaignStore(),
        PostgresConversationStore(),
        owner=int(gms[0]),
        other_owner=int(gms[1]),
        dsn=target,
    )


def _a_campaign(world: World, *, owner: int | None = None, archived: bool = False) -> str:
    with world.db.transaction() as unit:
        campaign = world.campaigns.create(
            unit, owner_id=world.owner if owner is None else owner, name="Nocturne"
        )
    if archived:
        with world.db.transaction() as unit:
            world.campaigns.set_archived(
                unit,
                campaign.id,
                owner_id=world.owner if owner is None else owner,
                archived=True,
            )
    return str(campaign.id)


def _create(
    world: World,
    *,
    owner: int | None = None,
    campaign_id: str | None = None,
    title: str | None = None,
    started_mode: str | None = "sage",
    now: datetime | None = None,
) -> Conversation:
    with world.db.transaction() as unit:
        return world.conversations.create(
            unit,
            owner_id=world.owner if owner is None else owner,
            campaign_id=campaign_id,
            title=title,
            started_mode=started_mode,
            now=now,
        )


def _a_legacy_conversation(
    world: World, *, owner: int | None = None, now: datetime | None = None
) -> str:
    """A row this bead's store did not create — a client-minted `randomUUID()`,
    no title, no campaign, no `started_mode`. That is what `chat.conversations`
    holds today for every conversation in production, and every one of them must
    keep working on every method here, forever, with no deprecation."""
    owner_id = world.owner if owner is None else owner
    conversation_id = str(uuid.uuid4())
    moment = T0 if now is None else now
    if world.kind == "fake":
        with world.db.transaction() as unit:
            twin_table(world.db, "conversations").add(
                unit,
                conversation_id,
                Conversation(
                    id=conversation_id,
                    owner_id=owner_id,
                    campaign_id=None,
                    title=None,
                    started_mode=None,
                    created_at=moment,
                    updated_at=None,
                    archived_at=None,
                ),
            )
        return conversation_id
    assert world.dsn is not None
    with connect(world.dsn) as conn:
        conn.execute(
            "INSERT INTO chat.conversations (conversation_id, user_id, created_at) "
            "VALUES (%s, %s, %s)",
            (conversation_id, owner_id, moment),
        )
    return conversation_id


def _ids(world: World, **filters: Any) -> list[str]:
    with world.db.transaction() as unit:
        page = world.conversations.list_for_owner(unit, world.owner, **filters)
    return [row.id for row in page.items]


# ── Create ───────────────────────────────────────────────────────────────────


def test_a_created_conversation_is_immediately_in_its_owners_index(world: World) -> None:
    """The bead's first acceptance criterion, in the store's half: a
    server-minted conversation appears in the list at once, with no second
    write and nothing to reconcile."""
    made = _create(world, title="Session one")
    assert _ids(world) == [made.id]


def test_a_created_conversation_is_minted_server_side_and_reads_back_whole(
    world: World,
) -> None:
    made = _create(world, title="Session one", started_mode="gm", now=T0)
    with world.db.transaction() as unit:
        read = world.conversations.get_for_owner(unit, made.id, owner_id=world.owner)
    assert read is not None
    assert read.id == made.id and read.id.startswith("cnv_")
    assert (read.owner_id, read.campaign_id, read.title) == (world.owner, None, "Session one")
    assert (read.started_mode, read.created_at, read.archived_at) == ("gm", T0, None)
    assert read.updated_at is None, "nothing has updated it yet"


def test_a_conversation_can_be_created_already_inside_a_campaign(world: World) -> None:
    campaign = _a_campaign(world)
    made = _create(world, campaign_id=campaign, started_mode="gm")
    with world.db.transaction() as unit:
        assert world.conversations.campaign_for_owner(unit, made.id, owner_id=world.owner) == (
            campaign
        )


def test_two_creates_never_mint_the_same_id(world: World) -> None:
    assert _create(world).id != _create(world).id


@pytest.mark.parametrize("mode", ["sage", "spell", "rules", "gm", None])
def test_every_channel_the_enum_carries_is_a_channel_a_conversation_can_start_in(
    world: World, mode: str | None
) -> None:
    """Both halves of the migration's CHECK: each of the four is accepted, and
    NULL — *not recorded* — is accepted too."""
    assert _create(world, started_mode=mode).started_mode == mode


def test_a_channel_the_enum_does_not_carry_is_refused_before_the_statement(
    world: World,
) -> None:
    with pytest.raises(ValueError, match="channels"), world.db.transaction() as unit:
        world.conversations.create(
            unit,
            owner_id=world.owner,
            campaign_id=None,
            title=None,
            started_mode="oracle",
        )


def test_an_over_long_title_is_refused_without_quoting_the_title(world: World) -> None:
    """SEC-20: the refusal names the field and never the value, so a title
    cannot reach a traceback. Checked before the statement, so it is this
    `ValueError` rather than the driver's error carrying the text."""
    secret = "x" * 201
    with pytest.raises(ValueError) as refusal, world.db.transaction() as unit:
        world.conversations.create(
            unit, owner_id=world.owner, campaign_id=None, title=secret, started_mode="gm"
        )
    assert secret not in str(refusal.value)


# ── The campaign is validated in the statement ───────────────────────────────


@pytest.mark.parametrize("which", ["missing", "foreign", "archived"])
def test_a_campaign_that_is_not_a_live_one_of_the_owners_refuses_the_create(
    world: World, which: str
) -> None:
    campaign = {
        "missing": "cmp_" + "z" * 22,
        "foreign": lambda: _a_campaign(world, owner=world.other_owner),
        "archived": lambda: _a_campaign(world, archived=True),
    }[which]
    named = campaign() if callable(campaign) else campaign

    with pytest.raises(MissingParent), world.db.transaction() as unit:
        world.conversations.create(
            unit, owner_id=world.owner, campaign_id=named, title=None, started_mode="gm"
        )
    assert _ids(world) == [], "a refused create must leave no row behind"


def test_a_refused_campaign_leaves_the_transaction_usable_and_the_commit_clean(
    world: World,
) -> None:
    """A9(a). This is what discriminates in-statement validation from letting
    the deferred foreign key do the refusing: an implementation that passed a
    bad campaign id into a plain INSERT would get no error here at all, and the
    COMMIT below would raise instead — after the refusal was supposed to have
    been reported."""
    foreign = _a_campaign(world, owner=world.other_owner)
    with world.db.transaction() as unit:
        with pytest.raises(MissingParent):
            world.conversations.create(
                unit, owner_id=world.owner, campaign_id=foreign, title=None, started_mode="gm"
            )
        kept = world.conversations.create(
            unit, owner_id=world.owner, campaign_id=None, title="after", started_mode="sage"
        )
    with world.db.transaction() as unit:
        assert world.conversations.get_for_owner(unit, kept.id, owner_id=world.owner) is not None


# ── The index, and every filter on it ────────────────────────────────────────


def test_the_index_is_newest_metadata_first(world: World) -> None:
    old = _create(world, now=T0)
    middle = _create(world, now=T0 + timedelta(hours=1))
    new = _create(world, now=T0 + timedelta(hours=2))
    assert _ids(world) == [new.id, middle.id, old.id]


def test_a_renamed_conversation_rises_to_the_top(world: World) -> None:
    """`COALESCE(updated_at, created_at)`, observable: the index orders by
    metadata changes. (A chat turn does not bump `updated_at` and is not meant
    to — that would be a new statement on /chat's request path.)"""
    old = _create(world, now=T0)
    _create(world, now=T0 + timedelta(hours=2))
    with world.db.transaction() as unit:
        world.conversations.rename(
            unit, old.id, owner_id=world.owner, title="Renamed", now=T0 + timedelta(hours=9)
        )
    assert _ids(world)[0] == old.id


def test_the_index_holds_only_the_callers_own_conversations(world: World) -> None:
    mine = _create(world)
    theirs = _create(world, owner=world.other_owner)
    listed = _ids(world)
    assert listed == [mine.id] and theirs not in listed


def test_the_index_can_be_filtered_to_one_campaign(world: World) -> None:
    campaign = _a_campaign(world)
    inside = _create(world, campaign_id=campaign, started_mode="gm")
    _create(world, started_mode="gm")
    assert _ids(world, campaign_id=campaign) == [inside.id]


def test_filtering_by_a_campaign_the_caller_does_not_own_is_an_empty_page(
    world: World,
) -> None:
    """No 404, no oracle, no second query: the campaign is not resolved
    separately at all, so one that is not the caller's simply matches no rows."""
    theirs = _a_campaign(world, owner=world.other_owner)
    _create(world)
    assert _ids(world, campaign_id=theirs) == []


def test_the_index_can_be_filtered_to_one_channel(world: World) -> None:
    gm = _create(world, started_mode="gm")
    _create(world, started_mode="sage")
    assert _ids(world, started_mode="gm") == [gm.id]


def test_a_conversation_of_unknown_channel_is_in_no_channels_filter(world: World) -> None:
    """Every conversation that exists today has `started_mode` NULL. It belongs
    in no channel's list — it is unknown, not guessed — and it is still in the
    unfiltered index, so nothing is hidden."""
    legacy = _a_legacy_conversation(world)
    assert _ids(world, started_mode="gm") == []
    assert _ids(world) == [legacy]


def test_archived_conversations_are_out_of_the_default_page_and_back_on_request(
    world: World,
) -> None:
    kept = _create(world, now=T0 + timedelta(hours=1))
    gone = _create(world, now=T0)
    with world.db.transaction() as unit:
        assert world.conversations.set_archived(
            unit, gone.id, owner_id=world.owner, archived=True, now=T0 + timedelta(hours=2)
        )
    assert _ids(world) == [kept.id]
    assert set(_ids(world, include_archived=True)) == {kept.id, gone.id}


# ── Paging ───────────────────────────────────────────────────────────────────


def _every_page(world: World, *, limit: int, **filters: Any) -> list[str]:
    """Every id the cursor walks, one page at a time, with a hard stop so a
    cursor that failed to advance is a failure rather than a hang."""
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        with world.db.transaction() as unit:
            page = world.conversations.list_for_owner(
                unit, world.owner, cursor=cursor, limit=limit, **filters
            )
        assert len(page.items) <= limit
        seen.extend(row.id for row in page.items)
        cursor = page.next_cursor
        if cursor is None:
            return seen
    raise AssertionError("the cursor never reached the last page")


def test_paging_walks_every_conversation_once_and_in_the_same_order(world: World) -> None:
    made = [_create(world, now=T0 + timedelta(hours=hour)) for hour in range(5)]
    expected = [row.id for row in reversed(made)]
    assert _ids(world) == expected
    assert _every_page(world, limit=2) == expected


def test_paging_across_a_tie_never_repeats_or_skips_a_row(world: World) -> None:
    """The tie-break is what makes the order TOTAL, and a cursor over a
    non-total order silently repeats or drops rows at a page boundary. Asserted
    against the store's own single-page order rather than a hard-coded one, so
    this claims nothing about which collation decides the tie."""
    for _ in range(5):
        _create(world, now=T0)
    whole = _ids(world)
    assert len(whole) == 5
    assert _every_page(world, limit=1) == whole
    assert _every_page(world, limit=2) == whole


def test_the_last_page_says_so_with_a_null_cursor(world: World) -> None:
    _create(world)
    with world.db.transaction() as unit:
        page = world.conversations.list_for_owner(unit, world.owner, limit=10)
    assert page.next_cursor is None


def test_a_full_page_with_more_behind_it_carries_a_cursor(world: World) -> None:
    _create(world, now=T0)
    _create(world, now=T0 + timedelta(hours=1))
    with world.db.transaction() as unit:
        page = world.conversations.list_for_owner(unit, world.owner, limit=1)
    assert len(page.items) == 1 and page.next_cursor is not None


@pytest.mark.parametrize(("asked", "served"), [(0, 1), (-5, 1), (1000, 100), (None, 100)])
def test_a_limit_outside_the_bounds_is_clamped_and_never_refused(
    world: World, asked: int | None, served: int
) -> None:
    """A page size is a hint, not an assertion about a resource. Proved on the
    small end by serving exactly one row of two."""
    _create(world, now=T0)
    _create(world, now=T0 + timedelta(hours=1))
    with world.db.transaction() as unit:
        page = world.conversations.list_for_owner(unit, world.owner, limit=asked)
    assert len(page.items) == min(2, served)


def test_a_cursor_carries_no_owner_no_filter_and_no_text(world: World) -> None:
    """SEC-20 and the contract's Pagination rule. A cursor is client-held data,
    so the owner comes from the session and never from the cursor, and no search
    text, title or campaign id may be encoded in one."""
    campaign = _a_campaign(world)
    _create(world, campaign_id=campaign, title="The cellar", now=T0)
    newest = _create(world, campaign_id=campaign, title="The duke", now=T0 + timedelta(hours=1))
    with world.db.transaction() as unit:
        page = world.conversations.list_for_owner(
            unit, world.owner, campaign_id=campaign, limit=1
        )
    assert page.next_cursor is not None

    # Structural, not a substring search: the cursor decodes to EXACTLY the two
    # ordering values and nothing else, which is a stronger claim than "no title
    # appears in it" and does not accidentally pass on a single-digit owner id.
    raw = base64.urlsafe_b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4))
    assert json.loads(raw.decode("utf-8")) == [newest.sort_key.isoformat(), newest.id]
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,512}", page.next_cursor), "a cursor is base64url"


def test_a_cursor_this_server_did_not_mint_is_refused(world: World) -> None:
    with pytest.raises(InvalidCursor), world.db.transaction() as unit:
        world.conversations.list_for_owner(unit, world.owner, cursor="not-a-cursor")


# ── Rename, archive, unarchive ───────────────────────────────────────────────


def test_a_rename_changes_the_title_and_the_moment_it_changed(world: World) -> None:
    made = _create(world, title="First", now=T0)
    with world.db.transaction() as unit:
        assert world.conversations.rename(
            unit, made.id, owner_id=world.owner, title="Second", now=T0 + timedelta(hours=1)
        )
        read = world.conversations.get_for_owner(unit, made.id, owner_id=world.owner)
    assert read is not None
    assert (read.title, read.updated_at) == ("Second", T0 + timedelta(hours=1))


def test_archiving_is_reversible_and_destroys_nothing(world: World) -> None:
    """Archive is not deletion. Deletion arrives with `agent-forge-harness-1ka.5`
    and this bead ships none."""
    made = _create(world, title="Kept", now=T0)
    with world.db.transaction() as unit:
        assert world.conversations.set_archived(
            unit, made.id, owner_id=world.owner, archived=True, now=T0 + timedelta(hours=1)
        )
        assert not world.conversations.set_archived(
            unit, made.id, owner_id=world.owner, archived=True
        ), "archiving an archived conversation changes nothing"
        assert world.conversations.set_archived(
            unit, made.id, owner_id=world.owner, archived=False, now=T0 + timedelta(hours=2)
        )
        read = world.conversations.get_for_owner(unit, made.id, owner_id=world.owner)
    assert read is not None
    assert (read.archived_at, read.title) == (None, "Kept")


# ── Linking to a campaign ────────────────────────────────────────────────────


def test_an_uncampaigned_conversation_can_be_linked_to_a_live_campaign(world: World) -> None:
    made = _create(world, started_mode="gm", now=T0)
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        assert world.conversations.link_campaign(
            unit,
            made.id,
            owner_id=world.owner,
            campaign_id=campaign,
            now=T0 + timedelta(hours=1),
        )
        read = world.conversations.get_for_owner(unit, made.id, owner_id=world.owner)
    assert read is not None
    assert (read.campaign_id, read.updated_at) == (campaign, T0 + timedelta(hours=1))


def test_a_conversation_already_in_a_campaign_is_never_moved_to_another(world: World) -> None:
    """Re-pointing a conversation would silently re-scope stored turns that were
    answered under another campaign's authorisation. The caller sees the refusal
    and re-reads to tell it from a missing campaign; there is no second oracle
    here."""
    first, second = _a_campaign(world), _a_campaign(world)
    made = _create(world, campaign_id=first, started_mode="gm")
    with world.db.transaction() as unit:
        assert not world.conversations.link_campaign(
            unit, made.id, owner_id=world.owner, campaign_id=second
        )
        read = world.conversations.get_for_owner(unit, made.id, owner_id=world.owner)
    assert read is not None and read.campaign_id == first


@pytest.mark.parametrize("which", ["missing", "foreign", "archived"])
def test_linking_to_a_campaign_that_is_not_a_live_one_of_the_owners_changes_nothing(
    world: World, which: str
) -> None:
    made = _create(world, started_mode="gm")
    named = {
        "missing": lambda: "cmp_" + "z" * 22,
        "foreign": lambda: _a_campaign(world, owner=world.other_owner),
        "archived": lambda: _a_campaign(world, archived=True),
    }[which]()
    with world.db.transaction() as unit:
        assert not world.conversations.link_campaign(
            unit, made.id, owner_id=world.owner, campaign_id=named
        )
    with world.db.transaction() as unit:
        assert world.conversations.campaign_for_owner(unit, made.id, owner_id=world.owner) is None


def test_a_legacy_conversation_can_be_linked_renamed_and_archived(world: World) -> None:
    """A4, in the store's half: a client-minted UUID is a conversation id like
    any other, on every method, with no deprecation and no warning."""
    legacy = _a_legacy_conversation(world)
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        assert world.conversations.rename(unit, legacy, owner_id=world.owner, title="Old thread")
        assert world.conversations.link_campaign(
            unit, legacy, owner_id=world.owner, campaign_id=campaign
        )
        assert world.conversations.bind_started_mode(
            unit, legacy, owner_id=world.owner, mode="gm"
        ) == "gm"
        assert world.conversations.set_archived(unit, legacy, owner_id=world.owner, archived=True)
        read = world.conversations.get_for_owner(unit, legacy, owner_id=world.owner)
    assert read is not None
    assert (read.title, read.campaign_id, read.started_mode) == ("Old thread", campaign, "gm")


# ── campaign_for_owner — the one thing 1ir.2.4 needs ─────────────────────────


def test_campaign_for_owner_answers_none_for_uncampaigned_missing_and_foreign_alike(
    world: World,
) -> None:
    """The three are deliberately NOT distinguished: one answer, so nothing here
    can be used to tell a conversation that is not yours from one that does not
    exist."""
    uncampaigned = _create(world)
    theirs = _create(world, owner=world.other_owner, campaign_id=_a_campaign(
        world, owner=world.other_owner
    ), started_mode="gm")
    with world.db.transaction() as unit:
        assert world.conversations.campaign_for_owner(
            unit, uncampaigned.id, owner_id=world.owner
        ) is None
        assert world.conversations.campaign_for_owner(
            unit, theirs.id, owner_id=world.owner
        ) is None
        assert world.conversations.campaign_for_owner(
            unit, new_conversation_id(), owner_id=world.owner
        ) is None


# ── Ownership isolation ──────────────────────────────────────────────────────


def test_no_method_touches_a_conversation_that_is_not_the_callers(world: World) -> None:
    """The cross-tenant matrix at the store level: every method names the owner
    in its own statement, so a foreign conversation is refused by every one of
    them and told apart from a missing one by none of them."""
    theirs = _create(world, owner=world.other_owner, title="Theirs", started_mode="gm")
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        assert world.conversations.get_for_owner(unit, theirs.id, owner_id=world.owner) is None
        assert not world.conversations.rename(unit, theirs.id, owner_id=world.owner, title="Mine")
        assert not world.conversations.set_archived(
            unit, theirs.id, owner_id=world.owner, archived=True
        )
        assert not world.conversations.link_campaign(
            unit, theirs.id, owner_id=world.owner, campaign_id=campaign
        )
        assert world.conversations.bind_started_mode(
            unit, theirs.id, owner_id=world.owner, mode="sage"
        ) is None
    with world.db.transaction() as unit:
        read = world.conversations.get_for_owner(
            unit, theirs.id, owner_id=world.other_owner
        )
    assert read is not None
    assert (read.title, read.campaign_id, read.started_mode, read.archived_at) == (
        "Theirs",
        None,
        "gm",
        None,
    )


# ── started_mode: bound once, first writer wins ──────────────────────────────


def test_the_channel_is_bound_by_the_first_writer_and_never_changed(world: World) -> None:
    """A10's server half. The loser is TOLD it lost, by being handed the
    winner's channel rather than its own — a caller that trusted its own value
    would be the thing that silently mixes a conversation."""
    legacy = _a_legacy_conversation(world)
    with world.db.transaction() as unit:
        assert world.conversations.bind_started_mode(
            unit, legacy, owner_id=world.owner, mode="gm"
        ) == "gm"
    with world.db.transaction() as unit:
        assert world.conversations.bind_started_mode(
            unit, legacy, owner_id=world.owner, mode="sage"
        ) == "gm"
        read = world.conversations.get_for_owner(unit, legacy, owner_id=world.owner)
    assert read is not None and read.started_mode == "gm"


def test_binding_a_channel_that_was_never_recorded_reports_no_such_conversation(
    world: World,
) -> None:
    with world.db.transaction() as unit:
        assert world.conversations.bind_started_mode(
            unit, new_conversation_id(), owner_id=world.owner, mode="gm"
        ) is None


# ── Transactions ─────────────────────────────────────────────────────────────


class _Abort(Exception):
    """Raised to roll a transaction back, and never caught by the code under test."""


def test_a_rolled_back_transaction_leaves_no_trace_of_any_write(world: World) -> None:
    kept = _create(world, title="Kept", now=T0)
    with pytest.raises(_Abort), world.db.transaction() as unit:
        world.conversations.create(
            unit, owner_id=world.owner, campaign_id=None, title="Lost", started_mode="gm"
        )
        world.conversations.rename(unit, kept.id, owner_id=world.owner, title="Renamed")
        world.conversations.set_archived(unit, kept.id, owner_id=world.owner, archived=True)
        raise _Abort
    with world.db.transaction() as unit:
        read = world.conversations.get_for_owner(unit, kept.id, owner_id=world.owner)
    assert read is not None
    assert (read.title, read.archived_at) == ("Kept", None)
    assert _ids(world) == [kept.id], "the created row must not have survived the rollback"


def test_a_committed_conversation_is_there_for_the_next_transaction(world: World) -> None:
    made = _create(world, title="Committed")
    with world.db.transaction() as unit:
        assert world.conversations.get_for_owner(unit, made.id, owner_id=world.owner) is not None


# ── What no fake can prove ───────────────────────────────────────────────────


@needs_db
def test_creating_a_conversation_binds_no_model_routing_strategy(dsn: str) -> None:
    """A8, and it is not cosmetic. `claim_conversation_strategy` binds with
    `UPDATE ... WHERE selection_strategy IS NULL`, so a create that pre-set even
    `'auto'` would make the conversation's FIRST /chat turn with a manual model
    preference return 409 instead of binding. "Started model affinity is
    preserved" means exactly this."""
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') "
            "RETURNING id"
        ).fetchone()[0]
    with _database(dsn).transaction() as unit:
        made = PostgresConversationStore().create(
            unit, owner_id=int(owner), campaign_id=None, title=None, started_mode="gm"
        )
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT selection_strategy, manual_alias, catalog_revision "
            "FROM chat.conversations WHERE conversation_id = %s",
            (made.id,),
        ).fetchone()
    assert row == (None, None, None)


@needs_db
@pytest.mark.parametrize("which", ["missing", "foreign", "archived"])
def test_a_refused_campaign_never_raises_a_foreign_key_violation_even_at_commit(
    dsn: str, which: str
) -> None:
    """A9(b). `chat.conversations.campaign_id` is DEFERRABLE INITIALLY DEFERRED,
    so an implementation that passed a bad campaign id into a plain INSERT would
    raise HERE — at the COMMIT, outside every `try`, and after a route had
    already composed its answer. Validating in the statement is what makes this
    test pass, and nothing else does."""
    store = PostgresConversationStore()
    campaigns = PostgresCampaignStore()
    database = _database(dsn)
    with connect(dsn) as conn:
        gms = [
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
                (email,),
            ).fetchone()[0]
            for email in ("gm@example.com", "other@example.com")
        ]
    owner, stranger = int(gms[0]), int(gms[1])

    if which == "missing":
        named = "cmp_" + "z" * 22
    else:
        holder = stranger if which == "foreign" else owner
        with database.transaction() as unit:
            named = campaigns.create(unit, owner_id=holder, name="Theirs").id
        if which == "archived":
            with database.transaction() as unit:
                campaigns.set_archived(unit, named, owner_id=owner, archived=True)

    try:
        with database.transaction() as unit:
            with pytest.raises(MissingParent):
                store.create(
                    unit, owner_id=owner, campaign_id=named, title=None, started_mode="gm"
                )
            # The transaction is still usable, and this row must survive the
            # COMMIT that the deferred constraint would otherwise abort.
            kept = store.create(
                unit, owner_id=owner, campaign_id=None, title=None, started_mode="sage"
            )
    except psycopg.errors.ForeignKeyViolation as violation:  # pragma: no cover - the bug
        raise AssertionError(
            "the deferred foreign key refused at COMMIT, so the campaign was not "
            "validated in the statement"
        ) from violation

    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM chat.conversations WHERE campaign_id IS NOT NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM chat.conversations WHERE conversation_id = %s", (kept.id,)
        ).fetchone()[0] == 1


@needs_db
def test_a_conversation_is_invisible_until_it_commits_and_then_visible_everywhere(
    dsn: str,
) -> None:
    """A2's other half: a server-minted conversation survives a reload and a
    second device, which is a second connection reading what this one wrote."""
    database = _database(dsn)
    with connect(dsn) as conn:
        owner = int(
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') "
                "RETURNING id"
            ).fetchone()[0]
        )
    store = PostgresConversationStore()
    seen_midway: list[int] = []
    with database.transaction() as unit:
        made = store.create(
            unit, owner_id=owner, campaign_id=None, title="Mid-flight", started_mode="gm"
        )
        with connect(dsn) as other:
            seen_midway.append(
                other.execute(
                    "SELECT count(*) FROM chat.conversations WHERE conversation_id = %s",
                    (made.id,),
                ).fetchone()[0]
            )
    assert seen_midway == [0], "an uncommitted conversation was visible to another connection"
    with _database(dsn).transaction() as unit:
        read = store.get_for_owner(unit, made.id, owner_id=owner)
    assert read is not None and read.title == "Mid-flight"


# ── Concurrency: who blocks whom (A14) ───────────────────────────────────────


def _an_owner(dsn: str) -> int:
    with connect(dsn) as conn:
        return int(
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') "
                "RETURNING id"
            ).fetchone()[0]
        )


def _waiting_on_a_row(dsn: str) -> int:
    """How many backends of this database are blocked on a row lock right now.

    Without this a "race" test passes when the second transaction never waited
    at all, which is the failure mode these tests exist to catch.
    """
    with connect(dsn) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
        )


def _until_blocked(dsn: str) -> bool:
    """Wait until another backend is really blocked on a lock, or give up.

    Every race below asserts on this: without it a "race" test passes when the
    second transaction never contended at all, which is the failure mode these
    tests exist to catch.
    """
    for _ in range(PATIENCE * 10):
        if _waiting_on_a_row(dsn):
            return True
        threading.Event().wait(0.1)
    return False


@needs_db
def test_two_requests_racing_to_bind_a_channel_leave_exactly_one_winner(dsn: str) -> None:
    """A14. The loser really waits — `pg_stat_activity` says so — and when the
    winner commits, READ COMMITTED re-evaluates the loser's WHERE, finds
    `started_mode` is no longer NULL, updates nothing and reads the winner back."""
    owner = _an_owner(dsn)
    store = PostgresConversationStore()
    conversation = str(uuid.uuid4())
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s)",
            (conversation, owner),
        )

    loser: list[str | None] = []
    blocked: list[bool] = []

    def second() -> None:
        with _database(dsn).transaction() as unit:
            loser.append(store.bind_started_mode(unit, conversation, owner_id=owner, mode="sage"))

    thread = threading.Thread(target=second, daemon=True)
    with _database(dsn).transaction() as unit:
        winner = store.bind_started_mode(unit, conversation, owner_id=owner, mode="gm")
        thread.start()
        blocked.append(_until_blocked(dsn))
    thread.join(PATIENCE)

    assert blocked == [True], "the second transaction never waited, so this test proves nothing"
    assert not thread.is_alive(), "the second transaction never finished"
    assert winner == "gm"
    assert loser == ["gm"], "the loser must be handed the winner's channel, not its own"


@needs_db
def test_two_requests_racing_to_link_the_same_conversation_leave_exactly_one_link(
    dsn: str,
) -> None:
    owner = _an_owner(dsn)
    store, campaigns, database = (
        PostgresConversationStore(),
        PostgresCampaignStore(),
        _database(dsn),
    )
    with database.transaction() as unit:
        first = campaigns.create(unit, owner_id=owner, name="First").id
    with database.transaction() as unit:
        second_campaign = campaigns.create(unit, owner_id=owner, name="Second").id
    with database.transaction() as unit:
        made = store.create(
            unit, owner_id=owner, campaign_id=None, title=None, started_mode="gm"
        )

    loser: list[bool] = []
    blocked: list[bool] = []

    def contend() -> None:
        with _database(dsn).transaction() as unit:
            loser.append(
                store.link_campaign(
                    unit, made.id, owner_id=owner, campaign_id=second_campaign
                )
            )

    thread = threading.Thread(target=contend, daemon=True)
    with database.transaction() as unit:
        won = store.link_campaign(unit, made.id, owner_id=owner, campaign_id=first)
        thread.start()
        blocked.append(_until_blocked(dsn))
    thread.join(PATIENCE)

    assert blocked == [True], "the second transaction never waited, so this test proves nothing"
    assert won and loser == [False], "exactly one link, and the loser is told it did not link"
    with database.transaction() as unit:
        assert store.campaign_for_owner(unit, made.id, owner_id=owner) == first


@needs_db
def test_a_rename_racing_an_archive_leaves_one_consistent_row(dsn: str) -> None:
    """Both writes touch the same row, so the second waits for the first; it
    then applies on top rather than overwriting it, and the row carries both."""
    owner = _an_owner(dsn)
    store, database = PostgresConversationStore(), _database(dsn)
    with database.transaction() as unit:
        made = store.create(
            unit, owner_id=owner, campaign_id=None, title="Before", started_mode="gm"
        )

    blocked: list[bool] = []

    def archive() -> None:
        with _database(dsn).transaction() as unit:
            store.set_archived(unit, made.id, owner_id=owner, archived=True)

    thread = threading.Thread(target=archive, daemon=True)
    with database.transaction() as unit:
        store.rename(unit, made.id, owner_id=owner, title="After")
        thread.start()
        blocked.append(_until_blocked(dsn))
    thread.join(PATIENCE)

    assert blocked == [True], "the archive never waited, so this test proves nothing"
    with database.transaction() as unit:
        read = store.get_for_owner(unit, made.id, owner_id=owner)
    assert read is not None
    assert read.title == "After" and read.is_archived


# ── What the routes rely on, proved on PostgreSQL (1kg.2.4 A2) ───────────────


@needs_db
def test_a_created_conversation_is_claimed_by_its_creator_and_binds_a_manual_model(dsn: str) -> None:
    """A8's PostgreSQL half (ruling A2-16). `POST /chat` is not modified: a
    conversation the store created already has its ownership row, so the claim
    returns the creator, and the model-routing columns are still NULL, so the
    first turn's manual preference BINDS rather than losing to the create and
    answering 409 (`b8o.2`)."""
    owner = _an_owner(dsn)
    with _database(dsn).transaction() as unit:
        made = PostgresConversationStore().create(
            unit, owner_id=owner, campaign_id=None, title=None, started_mode="gm"
        )
    messages = PostgresMessageStore(dsn)
    alias = enabled_profiles()[0].alias
    assert messages.claim_conversation(made.id, owner) == owner
    assert messages.claim_conversation_strategy(
        made.id, strategy="manual", manual_alias=alias, catalog_revision=CATALOG_REVISION
    ) == ("manual", alias)


@needs_db
def test_a_conversation_chat_claimed_by_a_client_uuid_reads_back_with_nothing_recorded(dsn: str) -> None:
    """A4's PostgreSQL half (ruling A2-16). Every conversation in production was
    made by `claim_conversation` with a client-minted UUID; it reads back through
    both read methods with all five metadata fields NULL, and the wire carries it
    as a valid `Conversation`."""
    owner = _an_owner(dsn)
    legacy = str(uuid.uuid4())
    assert PostgresMessageStore(dsn).claim_conversation(legacy, owner) == owner
    store = PostgresConversationStore()
    with _database(dsn).transaction() as unit:
        read = store.get_for_owner(unit, legacy, owner_id=owner)
        page = store.list_for_owner(unit, owner)
    assert read is not None
    assert [page_row.id for page_row in page.items] == [legacy]
    for row in (read, page.items[0]):
        assert (row.campaign_id, row.title, row.started_mode, row.updated_at, row.archived_at) == (None,) * 5
        wire = conversations_api.to_wire(row)
        assert wire.conversation_id == legacy and wire.started_mode is None


#: Three ids that differ only in `-`, `_` and nothing, which a linguistic
#: collation may order differently from code points. The ORDER BY is the index's
#: and deliberately carries no `COLLATE "C"` (ruling A2-15: residual accepted).
_COLLATION_IDS = ("cnv_a-b", "cnv_ab", "cnv_a_b")


@needs_db
def test_postgresql_pages_ids_that_tie_on_time_exactly_once_in_its_own_order(dsn: str) -> None:
    """Ruling A2-15. The twin orders a tie by code point and PostgreSQL by the
    database's collation, so the two may disagree about the ORDER of these ids;
    what must hold is that PostgreSQL's cursor pages them exactly once, in the
    order its own single page shows, at every page size. The collation facts are
    in the message so a failing run shows what the server was using."""
    owner = _an_owner(dsn)
    with connect(dsn) as conn:
        for conversation_id in _COLLATION_IDS:
            conn.execute(
                "INSERT INTO chat.conversations (conversation_id, user_id, created_at) VALUES (%s, %s, %s)",
                (conversation_id, owner, T0),
            )
        collation = conn.execute(
            "SELECT datcollate FROM pg_database WHERE datname = current_database()"
        ).fetchone()[0]
        dash_first = conn.execute("SELECT 'cnv_a-b' < 'cnv_ab'").fetchone()[0]
    facts = f"datcollate={collation!r}, 'cnv_a-b' < 'cnv_ab' is {dash_first}"
    store, database = PostgresConversationStore(), _database(dsn)
    with database.transaction() as unit:
        whole = [row.id for row in store.list_for_owner(unit, owner).items]
    assert sorted(whole) == sorted(_COLLATION_IDS), facts
    for size in (1, 2):
        walked: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            with database.transaction() as unit:
                page = store.list_for_owner(unit, owner, cursor=cursor, limit=size)
            walked.extend(row.id for row in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert walked == whole, f"limit {size}: {walked} against {whole}; {facts}"

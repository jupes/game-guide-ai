"""The tavern card's facts in both worlds (bead `agent-forge-harness-cfx`).

A shared behavioural suite: every test runs against the in-memory twins and
against PostgreSQL, so a derivation the two could disagree about — what counts
as activity, when a session was played, whose seat counts, what Concluded
touches — is asserted of both. The `postgres` parameter carries `needs_db` and
skips without a server; CI runs this file with DATABASE_URL set.

What is derived and why lives in `service/campaign_summary_store.py`; the
inferred decisions are interactions ADR §19 A-30. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_campaign_summary_db.py -q
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service.campaign_store import InMemoryCampaignStore, PostgresCampaignStore, pg
from service.campaign_summary_store import (
    AVATAR_ICONS,
    AVATAR_TONES,
    DORMANT_AFTER,
    LIVE,
    READY,
    InMemoryCampaignSummaryStore,
    OwnerFacts,
    PostgresCampaignSummaryStore,
    avatar_for,
    badge,
    dormant,
    last_activity,
)
from service.conversation_store import InMemoryConversationStore, PostgresConversationStore
from service.db import CampaignLockSettings, Database, InMemoryDatabase, PoolSettings
from service.document_store import InMemoryDocumentStore, PostgresDocumentStore
from service.history import InMemoryMessageStore
from service.participant_store import InMemoryParticipantStore, PostgresParticipantStore
from service.table_session_store import InMemoryTableSessionStore, PostgresTableSessionStore, no_slots
from service.workbench_contracts import Author, DocumentTypeId

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
AN_NPC = {"name": "Vashti", "qualifier": "Harbourmistress", "tags": ["harbour"], "voice": "low"}
MISSING = "cmp_" + "z" * 22


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("summary") as target:
        mig.migrate(target)
        yield target


@dataclass
class World:
    """Every store a card's facts are derived from, over one database: a GM,
    another GM, and two accounts that own nothing."""

    kind: str
    db: Any
    campaigns: Any
    participants: Any
    sessions: Any
    documents: Any
    conversations: Any
    summaries: Any
    messages: InMemoryMessageStore | None
    owner: int
    other_owner: int
    players: tuple[int, int]


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db: Any = InMemoryDatabase()
        messages = InMemoryMessageStore()
        yield World(
            "fake", db, InMemoryCampaignStore(db), InMemoryParticipantStore(db),
            InMemoryTableSessionStore(db, slot_clear=no_slots), InMemoryDocumentStore(db),
            InMemoryConversationStore(db), InMemoryCampaignSummaryStore(db, messages=messages), messages,
            owner=1, other_owner=2, players=(3, 4),
        )
        return
    target = request.getfixturevalue("dsn")
    with connect(target) as conn:
        ids = [
            int(conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
            ).fetchone()[0])
            for email in ("gm@example.com", "other@example.com", "p1@example.com", "p2@example.com")
        ]
    database = Database(
        target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5),
        CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5),
    )
    yield World(
        "postgres", database, PostgresCampaignStore(), PostgresParticipantStore(),
        PostgresTableSessionStore(slot_clear=no_slots), PostgresDocumentStore(), PostgresConversationStore(),
        PostgresCampaignSummaryStore(), None,
        owner=ids[0], other_owner=ids[1], players=(ids[2], ids[3]),
    )


def test_the_shared_suite_really_runs_against_both_worlds() -> None:
    """A lost `postgres` parameter would make every test below a fake-only test
    that still reports green, so the parametrisation is read, not assumed."""
    params = world._fixture_function_marker.params
    assert params is not None
    kinds = [p if isinstance(p, str) else p.values[0] for p in params]
    assert kinds == ["fake", "postgres"]
    postgres = params[1]
    assert not isinstance(postgres, str)
    assert [mark.name for mark in postgres.marks] == ["skipif"]


# ── Seeding, one helper per fact, the same call in both worlds ───────────────


def _campaign(world: World, *, owner: int | None = None, tone: str | None = None, at: datetime = T0) -> str:
    with world.db.transaction() as unit:
        return world.campaigns.create(
            unit, owner_id=world.owner if owner is None else owner, name="Nocturne", tone=tone, now=at
        ).id


def _session(world: World, campaign: str, *, at: datetime, hours: int = 12) -> str:
    with world.db.transaction() as unit:
        session, _link = world.sessions.start(
            unit, campaign, owner_id=world.owner, expires_at=at + timedelta(hours=hours), now=at
        )
        return str(session.id)


def _end(world: World, campaign: str, session: str, *, at: datetime, expired: bool = False) -> None:
    with world.db.transaction() as unit:
        assert world.sessions.end(unit, campaign, session, expired=expired, now=at) is not None


def _document(world: World, campaign: str, *, at: datetime) -> str:
    with world.db.transaction() as unit:
        return world.documents.create(
            unit, campaign, doc_type=DocumentTypeId.NPC, type_version=1, data=dict(AN_NPC), author=Author.GM, now=at
        ).id


def _thread(world: World, campaign: str | None, *, owner: int | None = None) -> str:
    with world.db.transaction() as unit:
        return world.conversations.create(
            unit, owner_id=world.owner if owner is None else owner, campaign_id=campaign, title=None,
            started_mode="gm", now=T0,
        ).id


def _turn(world: World, conversation: str, *, at: datetime) -> None:
    """A message in that conversation at `at`: through the twin's message
    store, or by SQL — `PostgresMessageStore` opens its own connection and
    stamps `now()`, and a card is a question about when."""
    if world.messages is not None:
        world.messages.append(conversation, "gm", "user", "What does the heir know?")
        world.messages._rows[-1].created_at = at
        return
    with world.db.transaction() as unit:
        pg(unit).conn.execute(
            "INSERT INTO chat.messages (conversation_id, mode, role, content, created_at) "
            "VALUES (%s, 'gm', 'user', 'What does the heir know?', %s)",
            (conversation, at),
        )


def _seat(world: World, campaign: str, alias: str, *, player: int | None = None) -> str:
    """An open seat, or one accepted by `player`."""
    with world.db.transaction() as unit:
        seat = world.participants.add(unit, campaign, alias=alias, now=T0).id
        if player is not None:
            world.participants.offer(unit, campaign, seat, user_id=player)
            world.participants.accept(unit, campaign, seat, user_id=player, now=T0)
        return seat


def _facts(world: World, campaign: str, *, at: datetime, owner: int | None = None) -> OwnerFacts | None:
    with world.db.transaction() as unit:
        return world.summaries.for_owner(unit, world.owner if owner is None else owner, [campaign], now=at).get(
            campaign
        )


def _row(world: World, campaign: str) -> Any:
    with world.db.transaction() as unit:
        return world.campaigns.get(unit, campaign, owner_id=world.owner)


# ── Ownership is in the query ────────────────────────────────────────────────


def test_a_campaign_that_is_not_the_callers_is_absent_and_never_an_error(world: World) -> None:
    mine = _campaign(world)
    theirs = _campaign(world, owner=world.other_owner)
    with world.db.transaction() as unit:
        answered = world.summaries.for_owner(unit, world.owner, [mine, theirs, MISSING], now=T0)
        assert set(answered) == {mine}
        assert world.summaries.for_owner(unit, world.other_owner, [mine], now=T0) == {}
        assert world.summaries.for_owner(unit, world.owner, [], now=T0) == {}


def test_a_new_campaign_has_nothing_yet_and_is_active_from_the_moment_it_is_made(world: World) -> None:
    campaign = _campaign(world)
    facts = _facts(world, campaign, at=T0)
    assert facts == OwnerFacts.nothing_yet(campaign)
    row = _row(world, campaign)
    assert last_activity(row, facts) == T0
    assert badge(row, facts) is None
    assert not dormant(row, facts, T0 + DORMANT_AFTER), "exactly thirty days is still active"
    assert dormant(row, facts, T0 + DORMANT_AFTER + timedelta(seconds=1))


# ── Last activity: a session played, a document edited, a GM turn ────────────


def test_last_activity_is_the_latest_of_a_session_a_document_and_a_gm_turn(world: World) -> None:
    campaign = _campaign(world)
    session = _session(world, campaign, at=T0 + timedelta(days=1))
    _end(world, campaign, session, at=T0 + timedelta(days=1, hours=3))
    _document(world, campaign, at=T0 + timedelta(days=2))
    thread = _thread(world, campaign)
    _turn(world, thread, at=T0 + timedelta(days=4))
    _turn(world, thread, at=T0 + timedelta(days=3))

    facts = _facts(world, campaign, at=T0 + timedelta(days=5))
    assert facts is not None
    assert facts.last_played_at == T0 + timedelta(days=1, hours=3), "an ended session is played when it ends"
    assert facts.last_edited_at == facts.last_prepared_at == T0 + timedelta(days=2)
    assert facts.last_turn_at == T0 + timedelta(days=4), "the latest turn, whatever order the rows arrived in"
    assert last_activity(_row(world, campaign), facts) == T0 + timedelta(days=4)


def test_a_turn_counts_only_in_a_conversation_linked_to_that_campaign(world: World) -> None:
    campaign = _campaign(world)
    elsewhere = _campaign(world)
    _turn(world, _thread(world, None), at=T0 + timedelta(days=9))
    _turn(world, _thread(world, elsewhere), at=T0 + timedelta(days=8))
    facts = _facts(world, campaign, at=T0 + timedelta(days=10))
    assert facts is not None and facts.last_turn_at is None
    positive = _facts(world, elsewhere, at=T0 + timedelta(days=10))
    assert positive is not None and positive.last_turn_at == T0 + timedelta(days=8)


def test_a_live_session_is_live_until_it_expires_and_an_expired_one_was_played_by_its_expiry(world: World) -> None:
    campaign = _campaign(world)
    started = T0 + timedelta(days=1)
    session = _session(world, campaign, at=started, hours=12)

    live = _facts(world, campaign, at=started + timedelta(hours=1))
    assert live is not None and live.live and live.last_played_at == started
    assert badge(_row(world, campaign), live) == LIVE
    overdue = _facts(world, campaign, at=started + timedelta(hours=13))
    assert overdue is not None and not overdue.live, "a live row past its expiry is not a table meeting now"

    # The sweep runs a day late; the table stopped by its expiry.
    _end(world, campaign, session, at=started + timedelta(days=2), expired=True)
    swept = _facts(world, campaign, at=started + timedelta(days=3))
    assert swept is not None and not swept.live
    assert swept.last_played_at == started + timedelta(hours=12)


# ── READY ────────────────────────────────────────────────────────────────────


def test_ready_means_an_unarchived_document_was_edited_after_the_table_last_met(world: World) -> None:
    campaign = _campaign(world)
    _document(world, campaign, at=T0 + timedelta(hours=1))
    facts = _facts(world, campaign, at=T0 + timedelta(hours=2))
    assert facts is not None and badge(_row(world, campaign), facts) == READY, "never met: any prep is waiting"

    session = _session(world, campaign, at=T0 + timedelta(days=1))
    _end(world, campaign, session, at=T0 + timedelta(days=1, hours=4))
    met = _facts(world, campaign, at=T0 + timedelta(days=2))
    assert met is not None and badge(_row(world, campaign), met) is None, "the prep was played"

    later = _document(world, campaign, at=T0 + timedelta(days=3))
    prepared = _facts(world, campaign, at=T0 + timedelta(days=4))
    assert prepared is not None and badge(_row(world, campaign), prepared) == READY

    with world.db.transaction() as unit:
        assert world.documents.set_archived(unit, campaign, later, archived=True, now=T0 + timedelta(days=5))
    shelved = _facts(world, campaign, at=T0 + timedelta(days=6))
    assert shelved is not None and badge(_row(world, campaign), shelved) is None, "archived prep is not waiting"
    assert shelved.last_edited_at == T0 + timedelta(days=3), "archiving is not an edit, and the edit still counts"


def test_a_concluded_or_archived_campaign_is_neither_ready_nor_dormant(world: World) -> None:
    campaign = _campaign(world)
    _document(world, campaign, at=T0)
    months = T0 + timedelta(days=120)
    facts = _facts(world, campaign, at=months)
    assert facts is not None
    row = _row(world, campaign)
    assert badge(row, facts) == READY and dormant(row, facts, months), "the positive control"
    with world.db.transaction() as unit:
        assert world.campaigns.set_concluded(unit, campaign, owner_id=world.owner, concluded=True, now=months)
    concluded = _row(world, campaign)
    assert badge(concluded, facts) is None and not dormant(concluded, facts, months)
    with world.db.transaction() as unit:
        world.campaigns.set_concluded(unit, campaign, owner_id=world.owner, concluded=False, now=months)
        world.campaigns.set_archived(unit, campaign, owner_id=world.owner, archived=True, now=months)
    archived = _row(world, campaign)
    assert badge(archived, facts) is None and not dormant(archived, facts, months)


# ── Seats ────────────────────────────────────────────────────────────────────


def test_the_seat_count_is_the_seats_not_removed_open_and_accepted_alike(world: World) -> None:
    campaign = _campaign(world)
    _seat(world, campaign, "Rook")
    _seat(world, campaign, "Wren", player=world.players[0])
    gone = _seat(world, campaign, "Moth")
    with world.db.transaction() as unit:
        world.participants.remove(unit, campaign, gone, now=T0)
    _seat(world, _campaign(world), "Elsewhere")
    facts = _facts(world, campaign, at=T0)
    assert facts is not None and facts.seat_count == 2


# ── Concluded and the tone line ──────────────────────────────────────────────


def test_concluded_is_the_owners_alone_reversible_and_touches_nothing_else(world: World) -> None:
    campaign = _campaign(world)
    later = T0 + timedelta(days=1)
    with world.db.transaction() as unit:
        revision = world.campaigns.authz_revision(unit, campaign)
        assert not world.campaigns.set_concluded(unit, campaign, owner_id=world.other_owner, concluded=True, now=later)
        assert world.campaigns.set_concluded(unit, campaign, owner_id=world.owner, concluded=True, now=later)
    row = _row(world, campaign)
    assert row.concluded_at == later and row.updated_at == later and row.archived_at is None
    with world.db.transaction() as unit:
        again = T0 + timedelta(days=2)
        assert not world.campaigns.set_concluded(unit, campaign, owner_id=world.owner, concluded=True, now=again)
        assert world.campaigns.authz_revision(unit, campaign) == revision, "not an authorisation fact"
    assert _row(world, campaign).concluded_at == later, "a repeat changes nothing"
    with world.db.transaction() as unit:
        assert world.campaigns.set_concluded(unit, campaign, owner_id=world.owner, concluded=False, now=later)
    assert _row(world, campaign).concluded_at is None


def test_a_tone_line_is_optional_and_set_or_cleared_by_its_owner_alone(world: World) -> None:
    plain = _campaign(world)
    assert _row(world, plain).tone is None
    toned = _campaign(world, tone="Mystery · Low magic")
    assert _row(world, toned).tone == "Mystery · Low magic"
    later = T0 + timedelta(days=1)
    with world.db.transaction() as unit:
        assert world.campaigns.set_tone(unit, toned, owner_id=world.other_owner, tone="Grim", now=later) is None
        same = world.campaigns.set_tone(unit, toned, owner_id=world.owner, tone="Mystery · Low magic", now=later)
        assert same is not None and same.updated_at == T0, "the tone it already has changes nothing"
        cleared = world.campaigns.set_tone(unit, toned, owner_id=world.owner, tone=None, now=later)
        assert cleared is not None and cleared.tone is None and cleared.updated_at == later
        still = world.campaigns.set_tone(unit, plain, owner_id=world.owner, tone=None, now=later)
        assert still is not None and still.updated_at == T0, "clearing no tone changes nothing"
    for refused in ("", "t" * 81, "Mys\u0007tery"):
        with pytest.raises(ValueError, match="tone line"), world.db.transaction() as unit:
            world.campaigns.set_tone(unit, plain, owner_id=world.owner, tone=refused, now=later)


def test_every_campaign_is_dnd5e(world: World) -> None:
    assert _row(world, _campaign(world)).game_system == "dnd5e"


# ── A seated player's card ───────────────────────────────────────────────────


def test_a_seated_card_is_the_tables_and_answers_only_a_live_accepted_seat(world: World) -> None:
    player, stranger = world.players
    campaign = _campaign(world, tone="Grim")
    seat = _seat(world, campaign, "Wren", player=player)
    _seat(world, campaign, "Open")
    started = T0 + timedelta(days=1)
    _session(world, campaign, at=started)
    with world.db.transaction() as unit:
        world.campaigns.set_concluded(unit, campaign, owner_id=world.owner, concluded=True, now=started)
        seen = world.summaries.for_seated(unit, player, [campaign, MISSING], now=started + timedelta(hours=1))
        assert set(seen) == {campaign}
        card = seen[campaign]
        assert (card.tone, card.game_system, card.concluded, card.last_played_at, card.live) == (
            "Grim", "dnd5e", True, started, True,
        )
        assert world.summaries.for_seated(unit, stranger, [campaign], now=started) == {}
        assert world.summaries.for_seated(unit, world.owner, [campaign], now=started) == {}, "the GM holds no seat"

    with world.db.transaction() as unit:
        world.participants.remove(unit, campaign, seat, now=started)
        assert world.summaries.for_seated(unit, player, [campaign], now=started) == {}, "a removed seat"


def test_a_seat_that_is_only_offered_or_in_an_archived_campaign_is_no_card(world: World) -> None:
    player = world.players[0]
    offered = _campaign(world)
    with world.db.transaction() as unit:
        seat = world.participants.add(unit, offered, alias="Wren", now=T0).id
        world.participants.offer(unit, offered, seat, user_id=player)
    shelved = _campaign(world)
    _seat(world, shelved, "Wren", player=player)
    with world.db.transaction() as unit:
        assert world.summaries.for_seated(unit, player, [offered], now=T0) == {}
        assert set(world.summaries.for_seated(unit, player, [shelved], now=T0)) == {shelved}, "positive control"
        world.campaigns.set_archived(unit, shelved, owner_id=world.owner, archived=True, now=T0)
        assert world.summaries.for_seated(unit, player, [shelved], now=T0) == {}


# ── The avatar ───────────────────────────────────────────────────────────────


def test_an_avatar_is_stable_per_campaign_and_drawn_from_the_palette() -> None:
    ids = [f"cmp_{n:022d}" for n in range(64)]
    avatars = [avatar_for(campaign) for campaign in ids]
    assert avatars == [avatar_for(campaign) for campaign in ids], "the same campaign, the same avatar"
    assert {icon for icon, _ in avatars} == set(AVATAR_ICONS), "sixty-four ids reach every icon"
    assert {tone for _, tone in avatars} == set(AVATAR_TONES)
    assert AVATAR_ICONS[:2] == ("sailing", "local_fire_department"), "the spec's own two come first"


# ── What only the database can refuse ────────────────────────────────────────


@needs_db
def test_the_database_refuses_a_tone_or_a_system_the_rules_do_not_allow(dsn: str) -> None:
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0]
        conn.execute("INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'N')", (MISSING, owner))
        row = conn.execute(
            "SELECT tone, game_system, concluded_at FROM campaign.campaigns WHERE id = %s", (MISSING,)
        ).fetchone()
        assert row == (None, "dnd5e", None), "a campaign made without naming them reads as untoned 5e, in play"
        for column, value in (("tone", ""), ("tone", "t" * 81), ("game_system", "pf2e"), ("game_system", None)):
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation)):
                conn.execute(f"UPDATE campaign.campaigns SET {column} = %s WHERE id = %s", (value, MISSING))

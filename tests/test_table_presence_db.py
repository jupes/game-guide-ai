"""Current, page-local listening evidence across both table transports."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from _pg import connect, needs_db
from test_reveal_db import _campaign, _command, _pg_world, _seat, _serve, _session
from test_reveal_db import dsn as dsn
from test_table_stream_reads_db import _ready_ambience

from service import cue_playback, gm_audio_presence, table_presence
from service.campaign_store import pg
from service.campaigns_api import archive_step_one, archive_step_two, restore
from service.db import Database, PoolSettings
from service.gm_stream_reads import GmReads, GmWatch
from service.table_reads import TableReads, TableStreamReads, TableStreamWatch
from service.workbench_contracts import (
    AudioSettingsRequest,
    CuePlayRequest,
    CueStopRequest,
    PresenceEvent,
    TablePresenceClose,
    TablePresenceOpen,
    TablePresenceReport,
)

pytestmark = needs_db


@pytest.fixture
def table(dsn):
    world = _pg_world(dsn, fill=True)
    with connect(dsn) as conn:
        conn.execute("UPDATE auth.users SET role = 'dm' WHERE id = %s", (world.owner,))
    campaign = _campaign(world)
    session = _session(world, campaign)
    yield world, campaign, session
    world.db.close()


def _apply(table, user, grant, body, now):
    world, campaign, _ = table
    with world.db.transaction() as unit:
        return table_presence.apply(pg(unit), campaign, account_id=user, grant_id=grant, body=body, now=now)


def _opening(table, user, grant, now):
    world, campaign, _ = table
    picture, context = TableReads(world.db, world.reveals, world.documents).snapshot_with_context(
        campaign, now=now, account_id=user, grant_id=grant,
    )
    assert picture is not None and context is not None
    assert context not in picture.model_dump_json()
    return TablePresenceOpen(schema_version=1, action="open", page_id=str(uuid4()), context=context)


def _page(table, user, grant, now):
    return _apply(table, user, grant, _opening(table, user, grant, now), now).registration


def _report(table, user, grant, registration, revision, state, now, *handles):
    return _apply(table, user, grant, TablePresenceReport(schema_version=1, action="report", registration=registration,
                  revision=revision, state=state, playing_handles=list(handles)), now)


def _gm(table, now):
    world, campaign, session = table
    with world.db.transaction() as unit:
        return gm_audio_presence.read(pg(unit), campaign, session.id, world.owner, now)


def test_gm_stream_and_fallback_share_seats_screen_counts_and_current_playback_only(table, dsn):
    world, campaign, session = table
    now = datetime.now(UTC)
    player, other = world.players[:2]
    seat = _seat(world, campaign, "accepted", player)
    absent = _seat(world, campaign, "confirmed", other)
    with world.db.transaction() as u:
        grant, _ = world.sessions.mint_screen(u, campaign, session.id, owner_id=world.owner, now=now)
    a, b = (_page(table, player, None, now) for _ in range(2))
    screen, screen_tab = (_page(table, None, grant.id, now) for _ in range(2))
    owner = _page(table, world.owner, None, now)
    _report(table, world.owner, None, owner, 1, "pending", now)
    _report(table, player, None, a, 1, "pending", now)
    _report(table, player, None, b, 1, "muted", now)
    _report(table, None, grant.id, screen, 1, "pending", now)
    _report(table, None, grant.id, screen_tab, 1, "idle", now)
    _, cue = _ready_ambience(dsn, campaign, world.owner)
    with world.db.transaction() as u:
        cue_playback.play(pg(u), campaign, session.id, world.owner, CuePlayRequest(
            schema_version=1, command_id=_command(), cue_id=cue, audio_epoch=0, loop=True), now)
        handle = pg(u).conn.execute("SELECT handle FROM campaign.audio_slots WHERE session_id = %s",
                                    (session.id,)).fetchone()[0]
    _report(table, player, None, a, 2, "listening", now, handle)
    _report(table, None, grant.id, screen_tab, 2, "listening", now, handle)
    snapshot, enabled, epoch = _gm(table, now)
    assert enabled and epoch == 1
    assert {p.participant_id: p.audio for p in snapshot.participants} == {seat: "listening", absent: "absent"}
    assert snapshot.guests.model_dump() == {"connected": 1, "listening": 1, "muted": 0, "pending": 0}

    async def opening():
        db = Database(dsn, PoolSettings(sync_max=0, async_max=1))
        try:
            (gm,) = await GmReads(db).snapshots([GmWatch(campaign, world.owner)], now=now)
            assert next(f for f in gm.frames if isinstance(f, PresenceEvent)) == snapshot
        finally:
            await db.aclose()
    asyncio.run(opening(), loop_factory=asyncio.SelectorEventLoop)
    with world.db.transaction() as u:
        cue_playback.stop(pg(u), campaign, session.id, world.owner, CueStopRequest(
            schema_version=1, command_id=_command(), cue_id=None), now)
    frame, _, _ = _gm(table, now)
    assert next(p for p in frame.participants if p.participant_id == seat).audio == "idle"
    assert frame.guests.connected == 1 and frame.guests.listening == 0
    with world.db.transaction() as u:
        world.sessions.revoke_screen(u, campaign, grant.id, owner_id=world.owner, now=now)
    assert _gm(table, now)[0].guests.connected == 0
    with pytest.raises(table_presence.PresenceInactive):
        _report(table, None, grant.id, screen, 2, "muted", now)


def test_registered_pages_are_bounded_expire_and_cannot_overwrite_another_page_or_generation(table):
    world, campaign, session = table
    now = datetime.now(UTC)
    player, other = world.players[:2]
    _seat(world, campaign, "accepted", player)
    _seat(world, campaign, "accepted", other)
    opening = _opening(table, player, None, now)
    with pytest.raises(table_presence.PresenceInactive):
        _apply(table, other, None, opening, now)
    with pytest.raises(table_presence.PresenceInactive):
        _apply(table, world.players[-1], None, opening, now)
    page = _apply(table, player, None, opening, now).registration
    assert _apply(table, player, None, opening, now).registration == page
    for _ in range(2):
        _page(table, player, None, now)
    with pytest.raises(table_presence.PresenceFull):
        _page(table, player, None, now)
    with pytest.raises(table_presence.PresenceInactive):
        _report(table, other, None, page, 2, "pending", now)
    _report(table, player, None, page, 2, "muted", now)
    _report(table, player, None, page, 1, "pending", now + timedelta(seconds=39))
    with pytest.raises(table_presence.PresenceConflict):
        _report(table, player, None, page, 2, "idle", now)
    assert any(p.audio == "muted" for p in _gm(table, now)[0].participants)
    assert all(p.audio == "absent" for p in _gm(table, now + timedelta(seconds=40))[0].participants)
    expired = now + table_presence.REGISTRATION_TTL
    with pytest.raises(table_presence.PresenceInactive):
        _report(table, player, None, page, 3, "idle", expired)
    reopened = _apply(table, player, None, opening, expired).registration
    assert reopened != page
    with world.db.transaction() as u:
        world.sessions.rotate(u, campaign, session.id, owner_id=world.owner, command_id=_command(), now=expired)
    with pytest.raises(table_presence.PresenceInactive):
        _apply(table, player, None, opening, expired)
    with pytest.raises(table_presence.PresenceInactive):
        _report(table, player, None, reopened, 1, "pending", expired)
    active = _page(table, player, None, expired)
    _apply(table, player, None, TablePresenceClose(schema_version=1, action="close", registration=active), expired)
    with pytest.raises(table_presence.PresenceInactive):
        _report(table, player, None, active, 2, "pending", expired)


def test_switch_is_idempotent_fences_old_widenings_and_never_restarts_a_sound(table, dsn):
    world, campaign, session = table
    now = datetime.now(UTC)
    _, cue = _ready_ambience(dsn, campaign, world.owner)
    original = _opening(table, world.owner, None, now)
    registration = _apply(table, world.owner, None, original, now).registration
    disable = AudioSettingsRequest(schema_version=1, command_id=_command(), enabled=False)
    enable = AudioSettingsRequest(schema_version=1, command_id=_command(), enabled=True, audio_epoch=2)
    with world.db.transaction() as u:
        args = (pg(u), campaign, session.id, world.owner)
        cue_playback.play(*args, CuePlayRequest(
            schema_version=1, command_id=_command(), cue_id=cue, audio_epoch=0, loop=True), now)
        with pytest.raises(cue_playback.NoSuchSession):
            cue_playback.settings(pg(u), campaign, session.id, world.other_owner, disable, now)
        off = cue_playback.settings(*args, disable, now)
        assert not off.enabled and off.audio_epoch == 2
        assert cue_playback.settings(*args, disable, now) == off
        reveal_epoch = pg(u).conn.execute("SELECT reveal_epoch FROM campaign.table_sessions WHERE id = %s",
                                          (session.id,)).fetchone()[0]
        assert reveal_epoch == session.reveal_epoch + 1
        assert all(f.playing is None for f in cue_playback.read_state(*args, now))
        with pytest.raises(cue_playback.AudioDisabled):
            cue_playback.play(*args, CuePlayRequest(
                schema_version=1, command_id=_command(), cue_id=cue, audio_epoch=2, loop=True), now)
        with pytest.raises(cue_playback.StaleAudioEpoch):
            cue_playback.settings(*args, enable.model_copy(update={"audio_epoch": 1}), now)
        on = cue_playback.settings(*args, enable, now)
        assert on.enabled and on.audio_epoch == 3
        assert cue_playback.settings(*args, disable, now) == on
        assert all(f.playing is None for f in cue_playback.read_state(*args, now))
    assert _gm(table, now)[1:] == (True, 3)
    with pytest.raises(table_presence.PresenceInactive):
        _report(table, world.owner, None, registration, 1, "pending", now)
    with pytest.raises(table_presence.PresenceInactive):
        _apply(table, world.owner, None, original, now)
    assert _opening(table, world.owner, None, now).context != original.context


def test_seat_replacement_and_campaign_restore_require_fresh_context(table, dsn):
    world, campaign, session = table
    player = world.players[0]
    old_seat = _seat(world, campaign, "confirmed", player)
    now = datetime.now(UTC)
    old = _opening(table, player, None, now)
    registration = _apply(table, player, None, old, now).registration
    _report(table, player, None, registration, 1, "muted", now)
    with world.db.transaction() as u:
        world.participants.remove(u, campaign, old_seat, now=now)
    new_seat = _seat(world, campaign, "confirmed", player)
    assert [(p.participant_id, p.audio) for p in _gm(table, now)[0].participants] == [(new_seat, "absent")]
    async def check_stream_watches():
        db = Database(dsn, PoolSettings(sync_max=0, async_max=1))
        try:
            old_watch, new_watch = [TableStreamWatch(campaign, session.id, session.link_generation,
                                                     account_id=player, participant_id=seat)
                                    for seat in (old_seat, new_seat)]
            stale, eligible = await TableStreamReads(db).snapshots([old_watch, new_watch], now=now)
            assert stale is None and eligible is not None
        finally:
            await db.aclose()
    asyncio.run(check_stream_watches(), loop_factory=asyncio.SelectorEventLoop)
    with pytest.raises(table_presence.PresenceInactive):
        _apply(table, player, None, old, now)
    with pytest.raises(table_presence.PresenceInactive):
        _report(table, player, None, registration, 2, "pending", now)
    new = _opening(table, player, None, now)
    before_archive = _apply(table, player, None, new, now).registration
    assert new.context != old.context
    served = _serve(world)
    for operation in (archive_step_one, archive_step_two, restore):
        operation(served.db, served.stores, campaign_id=campaign, owner_id=world.owner, now=now)
    with pytest.raises(table_presence.PresenceInactive):
        _apply(table, player, None, new, now)
    with pytest.raises(table_presence.PresenceInactive):
        _report(table, player, None, before_archive, 3, "pending", now)
    assert _opening(table, player, None, now).context != new.context


def test_global_table_and_principal_limits_and_reclamation(table):
    world, campaign, _ = table
    now = datetime.now(UTC)
    other_campaign = _campaign(world, owner=world.other_owner)
    other = (world, other_campaign, _session(world, other_campaign, owner=world.other_owner))
    occupied = []
    for player in world.players[:4]:
        _seat(world, campaign, "accepted", player)
        occupied.extend(_page(table, player, None, now) for _ in range(3))
    with pytest.raises(table_presence.PresenceFull):
        _page(table, world.owner, None, now)
    _seat(world, other_campaign, "accepted", world.players[0])
    with pytest.raises(table_presence.PresenceFull):
        _page(other, world.players[0], None, now)
    for player in world.players[4:7]:
        _seat(world, other_campaign, "accepted", player)
    for _ in range(3):
        _page(other, world.players[4], None, now)
    _page(other, world.players[5], None, now)
    with pytest.raises(table_presence.PresenceFull):
        _page(other, world.players[6], None, now)
    _apply(table, world.players[0], None, TablePresenceClose(
        schema_version=1, action="close", registration=occupied[0]), now)
    assert _page(other, world.players[6], None, now)

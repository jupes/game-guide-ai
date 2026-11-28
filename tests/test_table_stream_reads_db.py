"""The async stream read sees only the slots each live principal may read."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial

import psycopg
import pytest
from _pg import connect, needs_db
from psycopg import sql
from test_migrations_db import _asset_row, _insert_asset
from test_reveal_db import _campaign, _clear, _command, _document, _parts, _pg_world, _seat, _serve, _session, _write
from test_reveal_db import dsn as dsn

from service import campaign_identity as ident
from service import cue_playback
from service.campaign_store import pg
from service.campaigns_api import archive_step_one, archive_step_two, restore
from service.db import Database, PoolSettings
from service.gm_stream_admission import GmAdmission
from service.gm_stream_reads import GmReads
from service.gm_streams import GmStreams
from service.reveal_scope import DocumentCopies, EndReason, TableTarget
from service.table_admission import LEASE_EXPIRY, TABLE_STREAM_LIMIT, StreamLease, TableAdmission, TableFull
from service.table_fanout import TableFanout, listen_table_wakeups
from service.table_reads import TableReads, TableStreamReads, TableStreamWatch
from service.table_stream_events import CAMPAIGN_WAKEUP_CHANNEL, CREDENTIAL_WAKEUP_CHANNEL, SESSION_WAKEUP_CHANNEL
from service.table_streams import TableStreams
from service.workbench_contracts import CuePlayRequest, CueStopRequest, TableAudioEvent, TableRevealSnapshotEvent


def _slots(snapshot):
    assert snapshot is not None
    frame = snapshot.frames[1]
    assert isinstance(frame, TableRevealSnapshotEvent)
    return {slot.slot: slot for slot in frame.slots}


@needs_db
def test_one_batch_matches_the_snapshot_gate_and_private_changes_are_invisible_to_screens(dsn: str) -> None:
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    other_campaign = _campaign(world, owner=world.other_owner)
    session = _session(world, campaign)
    other_session = _session(world, other_campaign, owner=world.other_owner)
    confirmed = _seat(world, campaign, "confirmed", world.players[0])
    _seat(world, campaign, "accepted", world.players[1])
    _seat(world, campaign, "removed", world.players[2])
    _seat(world, other_campaign, "confirmed", world.players[3])
    with world.db.transaction() as unit:
        screen, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)

    def viewer(account: int) -> TableStreamWatch:
        return TableStreamWatch(campaign, session.id, session.link_generation, account_id=account)

    watched_screen = TableStreamWatch(campaign, session.id, session.link_generation, grant_id=screen.id)
    watches = [
        viewer(world.players[2]),
        viewer(world.owner),
        viewer(world.players[0]),
        viewer(world.players[1]),
        watched_screen,
        viewer(world.players[3]),
        replace(watched_screen, campaign_id=other_campaign),
        replace(watched_screen, campaign_id=other_campaign, session_id=other_session.id),
        replace(watched_screen, grant_id=ident.new_id(ident.TABLE_CREDENTIAL)),
        replace(viewer(world.owner), generation=session.link_generation + 1),
        viewer(world.owner),
    ]
    shared = _document(world, campaign)
    private = _document(world, campaign)
    _write(world, campaign, session.id, shared, TableTarget())

    async def scenario() -> None:
        db = Database(dsn, PoolSettings(sync_max=0, async_max=2))
        reader = TableStreamReads(db)
        try:
            before = await reader.snapshots(watches, now=datetime.now(UTC))
            _write(world, campaign, session.id, private, _parts(confirmed), mask=("qualifier",))
            moment = datetime.now(UTC)
            after = await reader.snapshots(watches, now=moment)
        finally:
            await db.aclose()
        assert [after[i] for i in (0, 5, 6, 7, 8, 9)] == [None] * 6
        old = TableReads(world.db, world.reveals, world.documents)
        for index in (1, 2, 3, 4, 10):
            watch = watches[index]
            assert after[index] == old.snapshot(
                campaign, now=moment, account_id=watch.account_id, grant_id=watch.grant_id
            )
        assert before[1] == after[1] == after[10]
        assert before[3] == after[3]
        assert before[4].model_dump_json() == after[4].model_dump_json()
        assert "Harbourmistress" in after[2].model_dump_json()
        assert "Harbourmistress" not in after[1].model_dump_json()
        assert "voice" not in after[2].model_dump_json()
        assert session.id not in after[2].model_dump_json()
        assert confirmed not in after[2].model_dump_json()

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


@needs_db
def test_reads_stop_confirmation_removal_revoke_rotation_and_expiry_from_current_rows(dsn: str) -> None:
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    session = _session(world, campaign)
    seat = _seat(world, campaign, "accepted", world.players[0])
    shared = _document(world, campaign)
    private = _document(world, campaign)
    _write(world, campaign, session.id, shared, TableTarget())
    _write(world, campaign, session.id, private, _parts(seat), mask=("qualifier",))
    with world.db.transaction() as unit:
        screen, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
    confirmed = TableStreamWatch(campaign, session.id, session.link_generation, account_id=world.players[0])
    guest = TableStreamWatch(campaign, session.id, session.link_generation, grant_id=screen.id)
    owner = replace(confirmed, account_id=world.owner)

    async def scenario() -> None:
        db = Database(dsn, PoolSettings(sync_max=0, async_max=1))
        reader = TableStreamReads(db)

        async def read():
            return await reader.snapshots([confirmed, guest, owner], now=datetime.now(UTC))

        try:
            initial = await read()
            assert set(_slots(initial[0])) == {"table"}
            assert _slots(initial[0])["table"].content is not None
            with world.db.transaction() as unit:
                assert world.participants.confirm(unit, campaign, seat)
            after_confirm = await read()
            assert _slots(after_confirm[0])["mine"].content is not None
            assert initial[1] == after_confirm[1]

            _clear(world, session.id, DocumentCopies(shared, EndReason.GM_STOP))
            stopped = await read()
            assert _slots(stopped[0])["table"].content is None
            assert _slots(stopped[0])["table"].seq == _slots(after_confirm[0])["table"].seq + 1
            assert _slots(stopped[0])["mine"] == _slots(after_confirm[0])["mine"]

            with world.db.transaction() as unit:
                assert world.participants.remove(unit, campaign, seat)
                assert world.sessions.revoke_screen(unit, campaign, screen.id, owner_id=world.owner)
            assert (await read())[:2] == [None, None]
            assert (await read())[2] is not None

            with world.db.transaction() as unit:
                rotated = world.sessions.rotate(unit, campaign, session.id, owner_id=world.owner, command_id=_command())
            assert rotated is not None
            fresh = replace(owner, generation=session.link_generation + 1)
            assert (await read())[2] is None
            assert (await reader.snapshots([fresh], now=datetime.now(UTC)))[0] is not None
            assert await reader.snapshots([fresh], now=session.expires_at) == [None]
            with world.db.transaction() as unit:
                assert world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
            assert await reader.snapshots([fresh], now=datetime.now(UTC)) == [None]
        finally:
            await db.aclose()

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


@needs_db
def test_one_async_statement_reads_sealed_values_without_waiting_for_row_locks(dsn: str) -> None:
    world = _pg_world(dsn)
    campaign = _campaign(world)
    session = _session(world, campaign)
    doc = _document(world, campaign)
    shown = _write(world, campaign, session.id, doc, TableTarget())
    owner = TableStreamWatch(campaign, session.id, session.link_generation, account_id=world.owner)

    async def scenario() -> None:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as reader:
            statements = 0

            class CountingConnection:
                async def execute(self, *arguments):
                    nonlocal statements
                    statements += 1
                    return await reader.execute(*arguments)

            class Db:
                @asynccontextmanager
                async def async_connection(self):
                    yield CountingConnection()

            with connect(dsn) as writer, writer.transaction():
                writer.execute("SELECT id FROM campaign.table_sessions WHERE id = %s FOR UPDATE", (session.id,))
                writer.execute(
                    "SELECT id FROM campaign.reveal_disclosures WHERE id = %s FOR UPDATE", (shown.disclosure.id,)
                )
                writer.execute(
                    "UPDATE campaign.document_versions SET data = '{\"name\": \"uncommitted\"}', sealed_at = NULL "
                    "WHERE document_id = %s",
                    (doc,),
                )
                async with reader.transaction():
                    await reader.execute("SET LOCAL lock_timeout = '200ms'")
                    read = await TableStreamReads(Db()).snapshots([owner, owner], now=datetime.now(UTC))
                assert read[0] == read[1]
                assert "Vashti" in read[0].model_dump_json()
                assert "uncommitted" not in read[0].model_dump_json()
                assert statements == 1
            (unsealed,) = await TableStreamReads(Db()).snapshots([owner], now=datetime.now(UTC))
            assert _slots(unsealed)["table"].content is None
            assert statements == 2

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


def test_empty_and_oversized_batches_do_not_acquire_a_connection() -> None:
    campaign, session = ident.new_id(ident.CAMPAIGN), ident.new_id(ident.TABLE_SESSION)
    watch = TableStreamWatch(campaign, session, 1, account_id=1)
    reader = TableStreamReads(None)
    now = datetime.now(UTC)
    assert asyncio.run(reader.snapshots([], now=now)) == []
    with pytest.raises(ValueError, match="too many"):
        asyncio.run(reader.snapshots([watch] * 21, now=now))
    with pytest.raises(ValueError, match="one principal"):
        TableStreamWatch(campaign, session, 1)
    with pytest.raises(ValueError, match="one principal"):
        replace(watch, grant_id=ident.new_id(ident.TABLE_CREDENTIAL))


@needs_db
def test_every_listener_receives_only_opaque_wakeups_after_commit(dsn: str) -> None:
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    doc = _document(world, campaign)
    player = _seat(world, campaign, "accepted", world.players[0])
    now = datetime.now(UTC)
    channels = [SESSION_WAKEUP_CHANNEL, CAMPAIGN_WAKEUP_CHANNEL, CREDENTIAL_WAKEUP_CHANNEL]

    with connect(dsn) as first, connect(dsn) as second:
        listeners = (first, second)
        for listener in listeners:
            for channel in channels:
                listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))

        def received(expected):
            for listener in listeners:
                got = [(n.channel, n.payload) for n in listener.notifies(timeout=2, stop_after=len(expected))]
                assert got == expected

        def quiet():
            for listener in listeners:
                assert list(listener.notifies(timeout=0.1)) == []

        with world.db.transaction() as unit:
            session = world.sessions.start(
                unit, campaign, owner_id=world.owner, expires_at=now + timedelta(hours=2),
                command_id=_command(), now=now,
            )
            quiet()
        received([(SESSION_WAKEUP_CHANNEL, session.id), (CAMPAIGN_WAKEUP_CHANNEL, campaign)])

        params = {
            "campaign_id": campaign,
            "session_id": session.id,
            "document_id": doc,
            "version": 1,
            "targets": TableTarget(),
            "command_id": _command(),
            "mask": ("name",),
            "now": now,
        }
        with pytest.raises(RuntimeError, match="roll back"):
            with world.db.transaction() as unit:
                world.reveals.write(unit, **params)
                quiet()
                raise RuntimeError("roll back")
        quiet()
        with world.db.transaction() as unit:
            world.reveals.write(unit, **params)
        received([(SESSION_WAKEUP_CHANNEL, session.id)])
        scope = DocumentCopies(doc, EndReason.GM_STOP)
        assert _clear(world, session.id, scope)
        received([(SESSION_WAKEUP_CHANNEL, session.id)])
        assert not _clear(world, session.id, scope)
        quiet()

        with world.db.transaction() as unit:
            assert world.participants.confirm(unit, campaign, player)
        received([(CAMPAIGN_WAKEUP_CHANNEL, campaign)])
        with world.db.transaction() as unit:
            assert world.participants.remove(unit, campaign, player)
        received([(CAMPAIGN_WAKEUP_CHANNEL, campaign)])
        with world.db.transaction() as unit:
            screen, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
            assert world.sessions.revoke_screen(unit, campaign, screen.id, owner_id=world.owner)
        received([(CREDENTIAL_WAKEUP_CHANNEL, screen.id)])

        with world.db.transaction() as unit:
            screen, secret = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
            assert world.sessions.leave(unit, ident.digest(secret), now=datetime.now(UTC))
        received([(CREDENTIAL_WAKEUP_CHANNEL, screen.id)])
        with world.db.transaction() as unit:
            assert world.sessions.rotate(unit, campaign, session.id, owner_id=world.owner, command_id=_command())
        received([(SESSION_WAKEUP_CHANNEL, session.id)])
        with world.db.transaction() as unit:
            assert world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
        received([(SESSION_WAKEUP_CHANNEL, session.id)])


def _ready_ambience(dsn, campaign, owner):
    asset = _asset_row("audio", "ready", campaign_id=campaign)
    cue = ident.new_id(ident.CUE)
    with connect(dsn) as conn:
        _insert_asset(conn, asset)
        conn.execute(
            "INSERT INTO campaign.audio_cues (id, campaign_id, asset_id, created_by, command_id, title, kind, "
            "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,'Secret encounter','ambience',now(),now())",
            (cue, campaign, asset["id"], owner, _command()),
        )
    return asset, cue


@needs_db
def test_campaign_archive_clears_player_audio_and_restore_cannot_revive_it(dsn: str) -> None:
    world = _pg_world(dsn, fill=True)
    served = _serve(world)
    campaign = _campaign(world)
    session = _session(world, campaign)
    _, cue = _ready_ambience(dsn, campaign, world.owner)
    reader = TableReads(world.db, world.reveals, world.documents)
    now = datetime.now(UTC)
    with world.db.transaction() as unit:
        screen, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
        cue_playback.play(pg(unit), campaign, session.id, world.owner, CuePlayRequest(
            schema_version=1, command_id=_command(), cue_id=cue, audio_epoch=0, loop=True,
        ), now)

    def ambience():
        picture, context = reader.snapshot_with_context(campaign, grant_id=screen.id, now=now)
        assert picture is not None
        if context is None:
            assert all(not isinstance(f, TableAudioEvent) for f in picture.frames)
            return None
        return next(f for f in picture.frames if isinstance(f, TableAudioEvent) and f.slot == "ambience")

    active = ambience()
    assert active.playing is not None
    for operation in (archive_step_one, archive_step_two, restore):
        operation(served.db, served.stores, campaign_id=campaign, owner_id=world.owner, now=now)
        cleared = ambience()
        if operation is archive_step_two:
            assert cleared is None
        else:
            assert cleared.seq > active.seq and cleared.playing is None


@needs_db
def test_two_instances_deliver_reveals_audio_stops_and_end_without_leaking_a_private_frame(dsn: str) -> None:
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    session = _session(world, campaign)
    seat = _seat(world, campaign, "confirmed", world.players[0])
    shared, private = _document(world, campaign), _document(world, campaign)
    _write(world, campaign, session.id, shared, TableTarget())
    with world.db.transaction() as unit:
        screen, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
    asset, cue = _ready_ambience(dsn, campaign, world.owner)

    async def scenario() -> None:
        db_a, db_b = (Database(dsn, PoolSettings(sync_max=0, async_max=1)) for _ in range(2))
        listen = partial(listen_table_wakeups, dsn)
        # Keep fallback polling beyond the assertion deadline to prove delivery
        # came from notifications, even though the two hubs share no process state.
        a = TableFanout(TableStreamReads(db_a), listen, poll_s=8, heartbeat_s=0.2)
        b = TableFanout(TableStreamReads(db_b), listen, poll_s=8, heartbeat_s=0.2)
        player = a.stream(TableStreamWatch(campaign, session.id, session.link_generation, account_id=world.players[0]))
        guest = b.stream(TableStreamWatch(campaign, session.id, session.link_generation, grant_id=screen.id))

        async def next_event(stream):
            while (frame := await anext(stream)).startswith(b":"):
                pass
            return frame

        async def next_for_both(prefix: bytes):
            frames = await asyncio.wait_for(asyncio.gather(next_event(player), next_event(guest)), 3)
            assert all(frame.startswith(prefix) for frame in frames)
            return frames

        try:
            await next_for_both(b"event: session")
            assert a.listening and b.listening
            first_player, first_guest = await next_for_both(b"event: snapshot")
            assert b'"mine"' in first_player and b'"mine"' not in first_guest
            for slot in ("ambience", "one_shot"):
                player_frame, guest_frame = await next_for_both(b"event: audio")
                assert player_frame == guest_frame
                assert json.loads(player_frame.splitlines()[1][6:]) == {
                    "schema_version": 1, "event": "audio", "slot": slot, "seq": 0, "playing": None,
                }
            await next_for_both(b"event: ready")
            assert await asyncio.wait_for(anext(guest), 1) == b": heartbeat\n\n"
            _write(world, campaign, session.id, private, _parts(seat), mask=("qualifier",))
            changed = await asyncio.wait_for(next_event(player), 1)
            assert changed.startswith(b"event: slot") and b"Harbourmistress" in changed
            assert await asyncio.wait_for(anext(guest), 1) == b": heartbeat\n\n"
            assert _clear(world, session.id, DocumentCopies(shared, EndReason.GM_STOP))
            stopped = await next_for_both(b"event: slot")
            assert stopped[0] == stopped[1] and b'"content":null' in stopped[0]
            started = datetime.now(UTC)
            with world.db.transaction() as unit:
                cue_playback.play(pg(unit), campaign, session.id, world.owner, CuePlayRequest(
                    schema_version=1, command_id=_command(), cue_id=cue, audio_epoch=session.audio_epoch, loop=True,
                ), started)
            player_frame, guest_frame = await next_for_both(b"event: audio")
            assert player_frame == guest_frame
            event = json.loads(player_frame.splitlines()[1][6:])
            assert event["slot"] == "ambience" and event["seq"] == 1
            assert event["playing"]["loop"] and len(event["playing"]["asset"]["handle"]) == 32
            for secret in (cue, asset["id"], campaign, session.id, "Secret encounter", asset["object_key"]):
                assert secret.encode() not in guest_frame
            refresh = TableReads(world.db, world.reveals, world.documents).snapshot(
                campaign, grant_id=screen.id, now=started + timedelta(seconds=23),
            )
            assert refresh is not None
            assert event in [frame.model_dump(mode="json") for frame in refresh.frames]
            with world.db.transaction() as unit:
                cue_playback.stop(pg(unit), campaign, session.id, world.owner, CueStopRequest(
                    schema_version=1, command_id=_command(), cue_id=cue,
                ), datetime.now(UTC))
            player_frame, guest_frame = await next_for_both(b"event: audio")
            assert player_frame == guest_frame
            assert json.loads(player_frame.splitlines()[1][6:]) == {
                "schema_version": 1, "event": "audio", "slot": "ambience", "seq": 2, "playing": None,
            }
            with world.db.transaction() as unit:
                assert world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
            stopped = await next_for_both(b"event: inactive")
            assert stopped[0] == stopped[1]
            with pytest.raises(StopAsyncIteration):
                await anext(player)
            with pytest.raises(StopAsyncIteration):
                await anext(guest)
            assert not a.listening and not b.listening
        finally:
            await player.aclose()
            await guest.aclose()
            await a.close()
            await b.close()
            await db_a.aclose()
            await db_b.aclose()

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


@needs_db
def test_two_gm_instances_share_audio_updates_and_release_revoked_or_expired_streams(dsn: str) -> None:
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    session = _session(world, campaign)
    _, cue = _ready_ambience(dsn, campaign, world.owner)
    with connect(dsn) as conn:
        conn.execute("UPDATE auth.users SET role = 'dm' WHERE id IN (%s, %s)", (world.owner, world.other_owner))

    async def scenario() -> None:
        async with AsyncExitStack() as cleanup:
            hubs = []
            fans = []
            tabs = []
            now = datetime.now(UTC)
            for _ in range(2):
                db = Database(dsn, PoolSettings(sync_max=0, async_max=2))
                cleanup.push_async_callback(db.aclose)
                fan = TableFanout(TableStreamReads(db), partial(listen_table_wakeups, dsn), poll_s=20, heartbeat_s=.2)
                fans.append(fan)
                table = TableStreams(TableAdmission(db), fan)
                cleanup.push_async_callback(table.close)
                gm = GmStreams(GmAdmission(db), GmReads(db), table, poll_s=20, heartbeat_s=.2)
                cleanup.push_async_callback(gm.close)
                hubs.append(gm)
                assert await gm.admission.open(campaign, account_id=world.other_owner, now=now) is None
                assert await gm.snapshot(campaign, account_id=world.players[0], now=now) is None
                lease = await gm.admission.open(campaign, account_id=world.owner, now=now)
                assert lease is not None
                tabs.append(await cleanup.enter_async_context(aclosing(
                    gm.bytes(lease, auth_expires_at=now + timedelta(hours=1)),
                )))

            async def read(tab):
                async with asyncio.timeout(4):
                    while (frame := await anext(tab)).startswith(b":"):
                        pass
                return json.loads(frame.splitlines()[1][6:])

            async def until(tab, matches):
                async with asyncio.timeout(4):
                    while True:
                        frame = await read(tab)
                        if matches(frame):
                            return frame

            opening = await hubs[0].snapshot(campaign, account_id=world.owner, now=now)
            assert opening is not None
            with connect(dsn) as conn:
                assert conn.execute("SELECT count(*) FROM campaign.gm_stream_connections").fetchone() == (2,)
            for tab in tabs:
                frames = [await read(tab) for _ in opening.frames]
                assert frames == [frame.model_dump(mode="json") for frame in opening.frames]
                assert [f["slot"] for f in frames if f["event"] == "audio"] == ["ambience", "one_shot"]
                assert frames[-1]["event"] == "ready"
            async with asyncio.timeout(4):
                while not all(fan.listening for fan in fans):
                    await asyncio.sleep(.02)

            def heard(frame):
                return frame["event"] == "audio" and frame["slot"] == "ambience"

            with world.db.transaction() as unit:
                cue_playback.play(pg(unit), campaign, session.id, world.owner, CuePlayRequest(
                    schema_version=1, command_id=_command(), cue_id=cue, audio_epoch=0, loop=True,
                ), datetime.now(UTC))
            first, second = await asyncio.gather(*(until(tab, heard) for tab in tabs))
            assert first == second and first["playing"]["cue_id"] == cue
            assert first["playing"]["title"] == "Secret encounter" and first["audio_epoch"] == 1
            idle = await gm.snapshot(campaign, account_id=world.owner, now=session.expires_at)
            assert idle is not None and [frame.event for frame in idle.frames] == ["ready"]
            with world.db.transaction() as unit:
                cue_playback.stop(pg(unit), campaign, session.id, world.owner, CueStopRequest(
                    schema_version=1, command_id=_command(), cue_id=None,
                ), datetime.now(UTC))
            first, second = await asyncio.gather(*(until(tab, heard) for tab in tabs))
            assert first == second and first["playing"] is None and first["audio_epoch"] == 2

            with connect(dsn) as conn:
                conn.execute("UPDATE auth.users SET role = 'player' WHERE id = %s", (world.owner,))
                conn.execute("SELECT pg_notify(%s, %s)", (SESSION_WAKEUP_CHANNEL, session.id))
            await asyncio.gather(*(until(tab, lambda f: f["event"] == "reconnect") for tab in tabs))
            for tab in tabs:
                with pytest.raises(StopAsyncIteration):
                    await anext(tab)
            assert await gm.snapshot(campaign, account_id=world.owner, now=now) is None
            with connect(dsn) as conn:
                assert conn.execute("SELECT count(*) FROM campaign.gm_stream_connections").fetchone() == (0,)
                conn.execute("UPDATE auth.users SET role = 'dm' WHERE id = %s", (world.owner,))
            lease = await gm.admission.open(campaign, account_id=world.owner, now=now)
            assert lease is not None
            async with aclosing(gm.bytes(lease, auth_expires_at=now - timedelta(seconds=1))) as expired:
                assert (await read(expired))["event"] == "reconnect"
                with pytest.raises(StopAsyncIteration):
                    await anext(expired)
            with connect(dsn) as conn:
                assert conn.execute("SELECT count(*) FROM campaign.gm_stream_connections").fetchone() == (0,)

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


@needs_db
def test_distributed_admission_caps_different_tables_even_under_concurrent_requests(dsn: str) -> None:
    world = _pg_world(dsn)
    owners = (world.owner, world.other_owner, world.players[0], world.players[1])
    offers = []
    for owner in owners:
        campaign = _campaign(world, owner=owner)
        session = _session(world, campaign, owner=owner)
        offers.append((campaign, owner, None))
        for _ in range(4):
            with world.db.transaction() as unit:
                grant, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=owner)
            offers.append((campaign, None, grant.id))

    async def scenario() -> None:
        dbs = [Database(dsn, PoolSettings(sync_max=0, async_max=3)) for _ in range(2)]
        instances = [TableAdmission(db) for db in dbs]
        try:
            now = datetime.now(UTC)
            replies = await asyncio.gather(*[
                instances[i % 2].open(campaign, account_id=account, grant_id=grant, now=now)
                for i, (campaign, account, grant) in enumerate(offers)
            ], return_exceptions=True)
            assert sum(isinstance(reply, StreamLease) for reply in replies) == TABLE_STREAM_LIMIT == 16
            assert sum(isinstance(reply, TableFull) for reply in replies) == len(offers) - TABLE_STREAM_LIMIT
            with connect(dsn) as conn:
                assert conn.execute("SELECT count(*) FROM campaign.table_stream_connections").fetchone() == (16,)
            accepted = next(i for i, reply in enumerate(replies) if isinstance(reply, StreamLease))
            lease = replies[accepted]
            assert isinstance(lease, StreamLease)
            await instances[1 - accepted % 2].release(lease)
            denied = next(i for i, reply in enumerate(replies) if isinstance(reply, TableFull))
            campaign, account, grant = offers[denied]
            with pytest.raises(TableFull):
                await instances[denied % 2].open(campaign, account_id=account, grant_id=grant, now=now)
            await instances[accepted % 2].release(lease)
            assert await instances[denied % 2].open(campaign, account_id=account, grant_id=grant, now=now) is not None
        finally:
            for db in dbs:
                await db.aclose()

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


@needs_db
def test_admission_rechecks_identity_and_enforces_principal_session_and_lease_limits(dsn: str) -> None:
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    foreign = _campaign(world, owner=world.other_owner)
    session = _session(world, campaign)
    _session(world, foreign, owner=world.other_owner)
    _seat(world, campaign, "accepted", world.players[0])
    _seat(world, campaign, "offered", world.players[1])
    screens = []
    for _ in range(4):
        with world.db.transaction() as unit:
            grant, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
            screens.append(grant.id)

    async def scenario() -> None:
        db = Database(dsn, PoolSettings(sync_max=0, async_max=2))
        admission = TableAdmission(db)
        now = datetime.now(UTC)

        async def admit(*, account=None, grant=None, at=now):
            return await admission.open(campaign, account_id=account, grant_id=grant, now=at)

        try:
            assert await admit(account=world.players[1]) is None
            assert await admit(account=world.other_owner) is None
            assert await admission.open(foreign, account_id=None, grant_id=screens[0], now=now) is None
            owner = await admit(account=world.owner)
            assert owner is not None and owner.watch.session_id == session.id
            accepted = await admit(account=world.players[0])
            assert accepted is not None
            for _ in range(2):
                assert await admit(account=world.players[0]) is not None
            with pytest.raises(TableFull):
                await admit(account=world.players[0])
            for screen_id in (*screens, screens[0], screens[0], screens[1], screens[1]):
                assert await admit(grant=screen_id) is not None
            with pytest.raises(TableFull):
                await admit(grant=screens[2])
            assert await admission.renew(owner, now=now + timedelta(seconds=15))
            fresh = now + LEASE_EXPIRY
            assert not await admission.renew(accepted, now=fresh)
            new = await admit(grant=screens[3], at=fresh)
            assert new is not None and new.id != accepted.id
            await admission.release(accepted)
            assert await admission.renew(new, now=fresh)
            assert await admission.renew(owner, now=fresh)
            assert await admit(account=world.owner, at=session.expires_at) is None
            with world.db.transaction() as unit:
                assert world.sessions.revoke_screen(unit, campaign, screens[3], owner_id=world.owner)
            assert await admit(grant=screens[3], at=fresh) is None
        finally:
            await db.aclose()

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)

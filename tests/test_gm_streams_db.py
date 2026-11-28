"""The private GM stream's projection and its independent distributed permits."""

import asyncio
import json
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from _pg import connect, needs_db
from test_reveal_db import _campaign, _document, _parts, _pg_world, _seat, _serve, _session, _write
from test_reveal_db import dsn as dsn
from test_table_stream_reads_db import _ready_ambience

from service import cue_playback
from service.campaign_store import pg
from service.db import Database, PoolSettings
from service.gm_stream_admission import GmAdmission
from service.gm_stream_reads import GmReads, GmWatch
from service.gm_streams import GmStreams, _updates
from service.reveals import GmReveal
from service.reveals_api import _state_of
from service.table_admission import LEASE_EXPIRY, TableFull
from service.table_fanout import TableFanout, listen_table_wakeups
from service.table_reads import TableStreamReads
from service.table_stream_events import SESSION_WAKEUP_CHANNEL
from service.table_streams import TableStreams
from service.workbench_contracts import CuePlayRequest

pytestmark = needs_db


def _dm(dsn, user):
    with connect(dsn) as conn:
        conn.execute("UPDATE auth.users SET role = 'dm' WHERE id = %s", (user,))


def test_private_snapshot_matches_the_gm_reveal_resource_and_checks_role_on_every_read(dsn):
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    _dm(dsn, world.owner)
    _dm(dsn, world.other_owner)
    served = _serve(world)
    owner, stranger = (GmWatch(campaign, user) for user in (world.owner, world.other_owner))

    async def scenario():
        db = Database(dsn, PoolSettings(sync_max=0, async_max=1))
        reads = GmReads(db)
        try:
            empty, denied = await reads.snapshots([owner, stranger], now=datetime.now(UTC))
            assert denied is None and [row.event for row in empty.frames] == ["ready"]
            session = _session(world, campaign)
            seat = _seat(world, campaign, "confirmed", world.players[0])
            held = _seat(world, campaign, "accepted", world.players[1])
            doc = _document(world, campaign)
            _write(world, campaign, session.id, doc, _parts(seat, held))
            with world.db.transaction() as unit:
                grant, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
            with connect(dsn) as conn:
                conn.execute("UPDATE campaign.documents SET data = data || jsonb_build_object('name','Changed') "
                             "WHERE id = %s", (doc,))
            now = datetime.now(UTC)
            shown, denied = await reads.snapshots([owner, GmWatch(campaign, world.players[0])], now=now)
            assert denied is None
            assert [row.event for row in shown.frames] == ["session", "snapshot", "presence", "audio", "audio", "ready"]
            wire_session = shown.frames[0].session
            assert wire_session.screens[0].screen_id == grant.id and wire_session.session_id == session.id
            picture = served.reveals.picture(campaign, owner_id=world.owner, now=now)
            expected = _state_of(GmReveal(picture, served.reveals.stale_documents(campaign, picture)))
            actual = shown.frames[1].state
            assert actual == expected
            assert all(slot.live.stale_text for slot in actual.slots if slot.live)
            assert {slot.slot.participant_id: slot.live.pending_delivery for slot in actual.slots if slot.live} == {
                seat: False, held: True,
            }
            assert _updates(empty, shown) is None
            changed = shown.model_copy(deep=True)
            changed.frames[3].seq += 2
            assert _updates(shown, changed) is None
            with connect(dsn) as conn:
                conn.execute("UPDATE auth.users SET role = 'player' WHERE id = %s", (world.owner,))
            assert await reads.snapshots([owner], now=now) == [None]
            _dm(dsn, world.owner)
            with connect(dsn) as conn:
                conn.execute("UPDATE campaign.campaigns SET archived_at = now() WHERE id = %s", (campaign,))
            assert await reads.snapshots([owner], now=now) == [None]
        finally:
            await db.aclose()
            world.db.close()

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


def test_four_reserved_permits_across_instances_and_expired_lease_cannot_release_replacement(dsn):
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    _dm(dsn, world.owner)
    other = _campaign(world, owner=world.other_owner)
    _dm(dsn, world.other_owner)
    session = _session(world, campaign)
    # Saturate all sixteen public permits so the reservation is proved in the
    # presence of actual existing rows on exactly the same database.
    now = datetime.now(UTC)
    with connect(dsn) as conn:
        conn.execute("INSERT INTO campaign.table_stream_connections "
                     "(permit,lease_id,instance_id,session_id,account_id,opened_at,last_seen_at) "
                     "SELECT n,gen_random_uuid(),gen_random_uuid(),%s,%s,%s,%s FROM generate_series(1,16) n",
                     (session.id, world.owner, now, now))

    async def scenario():
        db = Database(dsn, PoolSettings(sync_max=0, async_max=2))
        a, b = GmAdmission(db), GmAdmission(db)
        try:
            assert await b.open(campaign, account_id=world.players[0], now=now) is None
            leases = await asyncio.gather(*(a.open(campaign, account_id=world.owner, now=now) for _ in range(3)))
            with pytest.raises(TableFull):
                await b.open(campaign, account_id=world.owner, now=now)
            assert await b.open(other, account_id=world.other_owner, now=now) is not None
            with pytest.raises(TableFull):
                await a.open(other, account_id=world.other_owner, now=now)
            later = now + LEASE_EXPIRY
            fresh = await b.open(campaign, account_id=world.owner, now=later)
            assert fresh is not None
            for lease in leases:
                assert not await a.renew(lease, now=later)
                await a.release(lease)
            assert await b.renew(fresh, now=later)
            await b.release(fresh)
        finally:
            await db.aclose()
            world.db.close()

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)


def test_delayed_opening_subscribes_before_read_and_captures_one_shot_committed_during_first_read(dsn):
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    _dm(dsn, world.owner)
    session = _session(world, campaign)
    asset, cue = _ready_ambience(dsn, campaign, world.owner)
    with connect(dsn) as conn:
        conn.execute("UPDATE campaign.audio_cues SET kind = 'one_shot' WHERE id = %s", (cue,))
        conn.execute("UPDATE campaign.assets SET duration_ms = 3000 WHERE id = %s", (asset["id"],))

    async def scenario():
        db = Database(dsn, PoolSettings(sync_max=0, async_max=2))
        attempting, subscribe, noticed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        read_started, unblock_read = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def listen():
            attempting.set()
            await subscribe.wait()
            async with listen_table_wakeups(dsn) as notices:
                async def observed():
                    async for channel, payload in notices:
                        yield channel, payload
                        if channel == SESSION_WAKEUP_CHANNEL and payload == session.id:
                            noticed.set()
                yield observed()

        class DelayedReads(GmReads):
            async def snapshots(self, watches, *, now):
                pictures = await super().snapshots(watches, now=now)
                if not read_started.is_set():
                    read_started.set()
                    await unblock_read.wait()
                return pictures

        fanout = TableFanout(TableStreamReads(db), listen, poll_s=20)
        streams = GmStreams(GmAdmission(db), DelayedReads(db), TableStreams(None, fanout), poll_s=20, heartbeat_s=.03)
        try:
            now = datetime.now(UTC)
            lease = await streams.admission.open(campaign, account_id=world.owner, now=now)
            async with aclosing(streams.bytes(lease, auth_expires_at=now + timedelta(minutes=1))) as stream:
                assert await asyncio.wait_for(anext(stream), 1) == b": heartbeat\n\n"
                assert attempting.is_set() and not read_started.is_set()
                subscribe.set()
                await asyncio.wait_for(read_started.wait(), 1)
                assert await anext(stream) == b": heartbeat\n\n"
                with world.db.transaction() as unit:
                    cue_playback.play(pg(unit), campaign, session.id, world.owner, CuePlayRequest(
                        schema_version=1, command_id="short-push-at-open", cue_id=cue, audio_epoch=0, loop=False,
                    ), datetime.now(UTC))
                await asyncio.wait_for(noticed.wait(), 1)
                unblock_read.set()
                frames = []
                async with asyncio.timeout(2):
                    while not frames or frames[-1]["event"] != "ready":
                        frame = await anext(stream)
                        if frame.startswith(b"event:"):
                            frames.append(json.loads(frame.split(b"data: ", 1)[1]))
                assert frames[0]["session"]["audio_epoch"] == 1
                assert frames[-2]["slot"] == "one_shot" and frames[-2]["playing"]["cue_id"] == cue
        finally:
            await streams.close()
            await fanout.close()
            await db.aclose()
            world.db.close()

    asyncio.run(scenario(), loop_factory=asyncio.SelectorEventLoop)

"""The async stream read sees only the slots each live principal may read."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest
from _pg import connect, needs_db
from test_reveal_db import _campaign, _clear, _command, _document, _parts, _pg_world, _seat, _session, _write
from test_reveal_db import dsn as dsn

from service import campaign_identity as ident
from service.db import Database, PoolSettings
from service.reveal_scope import DocumentCopies, EndReason, TableTarget
from service.table_reads import TableReads, TableStreamReads, TableStreamWatch
from service.workbench_contracts import TableRevealSnapshotEvent


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

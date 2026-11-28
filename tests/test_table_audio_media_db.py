"""The public byte route reauthorizes a live slot on every Range and during long reads."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from _pg import connect, needs_db
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_reveal_db import _campaign, _command, _pg_world, _seat, _serve, _session
from test_reveal_db import dsn as dsn
from test_table_stream_reads_db import _ready_ambience

import config
from service import app as appmod
from service import cue_playback, media_serving, table_api
from service.auth_store import PostgresAuthStore
from service.campaign_store import pg
from service.db import Database, PoolSettings
from service.media_objects import CHUNK_BYTES, InMemoryObjectStore, MediaSettings
from service.session import SessionData, encode_session
from service.table_admission import TableAdmission
from service.table_audio_media import TableAudioMedia
from service.table_reads import TableReads
from service.table_streams import TableStreams
from service.workbench_api import INACTIVE_DETAIL, install_workbench
from service.workbench_contracts import CuePlayRequest, CueStopRequest

pytestmark = [needs_db, pytest.mark.real_auth]


def _playing(dsn, world, campaign, session, *, loop=True):
    asset, cue = _ready_ambience(dsn, campaign, world.owner)
    start = datetime.now(UTC)
    with world.db.transaction() as unit:
        cue_playback.play(pg(unit), campaign, session.id, world.owner, CuePlayRequest(
            schema_version=1, command_id=_command(), cue_id=cue, audio_epoch=0, loop=loop,
        ), start)
        handle = pg(unit).conn.execute(
            "SELECT handle FROM campaign.audio_slots WHERE session_id = %s AND slot = 'ambience'", (session.id,),
        ).fetchone()[0]
    return asset, cue, handle, start


def _http(world, monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-media-session-secret-over-32-characters")
    lifecycle = _serve(world).lifecycle
    monkeypatch.setitem(appmod.app.dependency_overrides, appmod.get_auth_store, lambda: PostgresAuthStore(db=world.db))
    monkeypatch.setitem(appmod.app.dependency_overrides, appmod.get_table_sessions, lambda: lifecycle)
    monkeypatch.setitem(appmod.app.dependency_overrides, appmod.get_timeline_database, lambda: world.db)
    monkeypatch.setitem(appmod.app.dependency_overrides, appmod.get_table_reads,
                        lambda: TableReads(world.db, world.reveals, world.documents))
    return TestClient(appmod.app, base_url="https://testserver")


def _account(user):
    return f"{config.SESSION_COOKIE_NAME}={encode_session(SessionData(user, 'player'), config.SESSION_SECRET)}"


def _stream_client(dsn, world):
    """Real PostgreSQL admission and HTTP headers, with a finite body for TestClient."""
    db = Database(dsn, PoolSettings(sync_max=0, async_max=2))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await db.aclose()

    class FiniteFanout:
        async def stream(self, watch):
            yield b": heartbeat\n\n"

    stream = TableStreams(TableAdmission(db), FiniteFanout())
    local = FastAPI(lifespan=lifespan)
    install_workbench(local)
    local.include_router(table_api.build_router(
        appmod.require_session, lambda: PostgresAuthStore(db=world.db), appmod._clear_session_cookie,
        lambda: _serve(world).lifecycle, lambda: None, table_streams=lambda: stream, media_enabled=lambda: True,
    ))
    return TestClient(local, base_url="https://testserver")


def test_table_audio_http_range_checks_grants_accounts_and_revocation(dsn, monkeypatch):
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    session = _session(world, campaign)
    seat = _seat(world, campaign, "accepted", world.players[0])
    asset, cue, handle, _ = _playing(dsn, world, campaign, session)
    objects = InMemoryObjectStore()
    content = bytes(i % 251 for i in range(asset["size_bytes"]))
    objects.put_stream(asset["object_key"], [content], max_bytes=len(content))
    audio = TableAudioMedia(world.db, objects)
    with world.db.transaction() as unit:
        grant, secret = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
    other = _campaign(world, owner=world.other_owner)
    foreign = _session(world, other, owner=world.other_owner)
    with world.db.transaction() as unit:
        _, foreign_secret = world.sessions.mint_screen(unit, other, foreign.id, owner_id=world.other_owner)

    client = _http(world, monkeypatch)
    monkeypatch.setitem(appmod.app.dependency_overrides, appmod.get_table_audio_media, lambda: audio)
    path = f"/table/assets/{handle}"

    screen = f"{table_api.SCREEN_COOKIE}={secret}"
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings())
    assert client.get(path, headers={"cookie": screen}).status_code == 404
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"cookie": screen, "sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get(path, headers={"cookie": screen, "sec-fetch-mode": "navigate"}).status_code == 403
    for cookie in (screen, _account(world.owner), _account(world.players[0])):
        reply = client.get(path, headers={"cookie": cookie, "range": "bytes=5-14"})
        assert reply.status_code == 206 and reply.content == content[5:15]
        assert reply.headers["content-range"] == f"bytes 5-14/{len(content)}"
        assert reply.headers["content-type"] == "audio/mpeg" and reply.headers["content-disposition"] == "inline"
        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["cross-origin-resource-policy"] == "same-origin"
        assert "etag" not in reply.headers and reply.headers["x-content-type-options"] == "nosniff"
    assert client.get(path, headers={"cookie": screen}).content == content
    invalid = client.get(path, headers={"cookie": screen, "range": "bytes=10000-"})
    assert invalid.status_code == 416 and invalid.headers["content-range"] == f"bytes */{len(content)}"

    def denied(url, cookie):
        result = client.get(url, headers={"cookie": cookie, "range": "bytes=10000-"})
        assert result.status_code == 404 and result.json() == {"detail": dict(INACTIVE_DETAIL)}
        assert "content-range" not in result.headers
        for hidden in (handle, asset["id"], asset["object_key"], cue, campaign, session.id, "Secret encounter"):
            assert hidden not in result.text + str(result.headers)

    denied(path, _account(world.players[1]))
    denied(path, _account(world.owner) + f"; {table_api.SCREEN_COOKIE}={foreign_secret}")
    denied("/table/assets/" + asset["id"], _account(world.owner))
    with world.db.transaction() as unit:
        world.participants.remove(unit, campaign, seat)
        world.sessions.revoke_screen(unit, campaign, grant.id, owner_id=world.owner)
    denied(path, _account(world.players[0]))
    assert client.get(path, headers={"cookie": screen}).status_code == 401
    with world.db.transaction() as unit:
        cue_playback.stop(pg(unit), campaign, session.id, world.owner, CueStopRequest(
            schema_version=1, command_id=_command(), cue_id=None,
        ), datetime.now(UTC))
    denied(path, _account(world.owner))


def test_presence_http_registration_is_scoped_to_its_live_picture_and_principal(dsn, monkeypatch):
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    session = _session(world, campaign)
    _seat(world, campaign, "confirmed", world.players[0])
    with world.db.transaction() as unit:
        grant, secret = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
    client = _http(world, monkeypatch)
    endpoint = f"/table/presence?campaign_id={campaign}"
    snapshot = f"/table/snapshot?campaign_id={campaign}"
    player = _account(world.players[0])
    screen = f"{table_api.SCREEN_COOKIE}={secret}"

    def post(cookie, action, **props):
        return client.post(endpoint, headers={"cookie": cookie}, json={"schema_version": 1, "action": action, **props})

    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings())
    missing = client.post("/table/no-such-capability", json={})
    dark = post(screen, "open")
    assert (dark.status_code, dark.content) == (missing.status_code, missing.content)
    assert "x-table-audio-context" not in client.get(snapshot, headers={"cookie": screen}).headers
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    assert client.post(endpoint, json={}).status_code == 401
    assert client.post(endpoint, headers={"cookie": screen, "origin": "https://other.test"}, json={}).status_code == 403
    picture = client.get(snapshot, headers={"cookie": screen})
    context = picture.headers["x-table-audio-context"]
    assert len(context) == 64 and context not in picture.text
    player_context = client.get(snapshot, headers={"cookie": player}).headers["x-table-audio-context"]
    assert context != player_context
    with _stream_client(dsn, world) as streams:
        for cookie, expected in [(screen, context), (player, player_context)]:
            opened = streams.get(f"/table/stream?campaign_id={campaign}", headers={"cookie": cookie})
            assert opened.status_code == 200 and opened.headers["x-table-audio-context"] == expected
    page = str(uuid4())
    rejected = post(player, "open", page_id=page, context=context)
    assert rejected.status_code == 404 and rejected.json() == {"detail": dict(INACTIVE_DETAIL)}
    assert post(screen, "open", page_id=page, context="Secret encounter").status_code == 422
    reply = post(screen, "open", page_id=page, context=context)
    assert reply.status_code == 200 and set(reply.json()) == {"schema_version", "registration"}
    assert reply.headers["cache-control"] == "no-store"
    registration = reply.json()["registration"]
    report = dict(registration=registration, revision=1, state="pending", playing_handles=[])
    assert post(screen, "report", **report).status_code == 204
    collision = post(screen, "report", **(report | {"state": "muted"}))
    assert collision.status_code == 409 and collision.json()["detail"]["code"] == "conflict"
    assert post(player, "close", registration=registration).status_code == 404
    assert post(screen, "close", registration=registration).status_code == 204
    assert post(screen, "report", **report).status_code == 404
    for _ in range(3):
        assert post(screen, "open", page_id=str(uuid4()), context=context).status_code == 200
    full = post(screen, "open", page_id=str(uuid4()), context=context)
    assert full.status_code == 503 and full.headers["retry-after"] == "5"
    for result in (rejected, collision, full):
        assert all(value not in result.text for value in (session.id, grant.id, secret, context, registration, page))
    with connect(dsn) as conn:
        conn.execute("UPDATE campaign.table_sessions SET table_audio = false WHERE id = %s", (session.id,))
    assert "x-table-audio-context" not in client.get(snapshot, headers={"cookie": player}).headers
    assert post(screen, "open", page_id=page, context=context).status_code == 404
    with _stream_client(dsn, world) as streams:
        url = f"/table/stream?campaign_id={campaign}"
        assert "x-table-audio-context" not in streams.get(url, headers={"cookie": screen}).headers
        with connect(dsn) as conn:
            conn.execute("UPDATE campaign.table_sessions SET table_audio = true WHERE id = %s", (session.id,))
        rotated = streams.get(url, headers={"cookie": screen}).headers["x-table-audio-context"]
        assert rotated != context
        assert rotated == client.get(snapshot, headers={"cookie": screen}).headers["x-table-audio-context"]
    assert post(screen, "open", page_id=page, context=context).status_code == 404


@pytest.mark.parametrize("change", ["stop", "cookie expiry", "session expiry", "playback ended"])
def test_live_byte_response_stops_after_a_chunk_recheck(dsn, change):
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    session = _session(world, campaign)
    asset, _, handle, start = _playing(dsn, world, campaign, session, loop=change != "playback ended")
    length = 6 * CHUNK_BYTES
    with connect(dsn) as conn:
        conn.execute("UPDATE campaign.assets SET size_bytes = %s WHERE id = %s", (length, asset["id"]))
    objects = InMemoryObjectStore()
    objects.put_stream(asset["object_key"], [b"a" * length], max_bytes=length)
    clock = [start]
    media = TableAudioMedia(world.db, objects, clock=lambda: clock[0])
    deadline = start + timedelta(hours=24) if change != "cookie expiry" else start + timedelta(seconds=2)

    async def run():
        async def read():
            return await media.read(handle, account_id=world.owner, grant_id=None, expires_at=deadline,
                                    range_header="bytes=0-", if_range=None)

        reply = await read()
        assert reply is not None and reply.status_code == 206
        sent = 0

        async def send(message):
            nonlocal sent
            chunk = message.get("body", b"")
            if chunk and sent == 0:
                if change == "stop":
                    with world.db.transaction() as unit:
                        cue_playback.stop(pg(unit), campaign, session.id, world.owner, CueStopRequest(
                            schema_version=1, command_id=_command(), cue_id=None,
                        ), start + timedelta(seconds=1))
                else:
                    clock[0] = {"cookie expiry": deadline, "session expiry": session.expires_at,
                                "playback ended": start + timedelta(milliseconds=asset["duration_ms"])}[change]
            sent += len(chunk)

        async def no_receive():
            raise AssertionError("ASGI 2.4 uses send to detect a dropped client")

        with pytest.raises(media_serving.ReadRevoked):
            await reply({"type": "http", "asgi": {"spec_version": "2.4"}}, no_receive, send)
        assert media_serving.RECHECK_BYTES <= sent < media_serving.RECHECK_BYTES + CHUNK_BYTES < length
        assert await read() is None

    asyncio.run(run())

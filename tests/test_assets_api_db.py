"""The media upload route against a real PostgreSQL (agent-forge-harness-1kg.8.1.2).

AC 7 — **no request holds a database connection while bytes move**
(requirement 3.8, SEC-35). The route's object store is wrapped so that at every
chunk boundary — the body streaming to `tmp/`, the copy to the scratch
directory, the processed object's write — it opens `DB_POOL_MAX` concurrent
transactions on the route's own `Database` and requires every one of them to be
acquired within `DB_POOL_TIMEOUT_S` (`docs/migrations.md` section 5). It cannot
pass while the request holds a gate permit: one of the probes would find the
pool one short and time out. The twin has no permits, so this is PostgreSQL's.

Requires DATABASE_URL (CI sets it for this file, `.github/workflows/ci.yml`,
pinned by `service/tests/test_ci_workflow.py`). Without it every test skips.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from typing import Any, cast

import pytest
from _pg import connect, needs_db, throwaway_database
from fastapi.testclient import TestClient
from PIL import Image

from service import app as appmod
from service import asset_jobs, assets_api
from service import migrations as mig
from service.app import app, get_timeline_database, get_timeline_store, require_session
from service.asset_store import PostgresAssetStore, new_object_key, new_tmp_key
from service.db import Database, PoolSettings
from service.jobs import JobRunner, PostgresJobQueue
from service.media_objects import WAVEFORM_NAME, InMemoryObjectStore, MediaSettings, ObjectPage, ObjectStat
from service.session import SessionData
from service.timeline_store import PostgresTimelineStore
from service.workbench_api import reauth_failed

pytestmark = needs_db

# justification: `cast(Any, ...)` hands the probing double to a Protocol-typed
# parameter, and reads the unit's connection without narrowing its type.

CAMPAIGN = "cmp_" + "u" * 22
#: `docs/migrations.md` section 5: the gate's permits and how long a request
#: waits for one. The route's own database is built with exactly these.
POOL = PoolSettings()


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("assetsapi") as target:
        mig.migrate(target)
        yield target


@pytest.fixture
def owner(dsn: str) -> int:
    with connect(dsn) as conn:
        gm = int(conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0])
        conn.execute("INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
                     (CAMPAIGN, gm))
    return gm


class Probing:
    """An object store that, at every chunk boundary, takes every permit the
    gate has — all at once — and records each one that did not come."""

    def __init__(self, db: Database) -> None:
        self.inner = InMemoryObjectStore()
        self.db = db
        self.probes = 0
        self.failures: list[str] = []

    def _probe(self) -> None:
        self.probes += 1
        together = threading.Barrier(POOL.sync_max)
        failures: list[str] = []

        def hold() -> None:
            try:
                with self.db.transaction() as unit:
                    cast(Any, unit).conn.execute("SELECT 1")
                    together.wait(timeout=POOL.acquire_timeout_s)
            except Exception as exc:  # noqa: BLE001 - every failure is the finding
                failures.append(type(exc).__name__)

        threads = [threading.Thread(target=hold) for _ in range(POOL.sync_max)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=POOL.acquire_timeout_s * 3)
        self.failures += failures

    def put_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> int:
        def probed() -> Iterator[bytes]:
            for chunk in chunks:
                self._probe()
                yield chunk

        return self.inner.put_stream(key, probed(), max_bytes=max_bytes)

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        found = self.inner.get_stream(key, offset=offset, length=length)

        def probed() -> Iterator[bytes]:
            for chunk in found:
                self._probe()
                yield chunk

        return probed()

    def stat_object(self, key: str) -> ObjectStat | None:
        return self.inner.stat_object(key)

    def delete_object(self, key: str) -> None:
        self.inner.delete_object(key)

    def list_objects(self, prefix: str, *, older_than: datetime, start_after: str | None = None,
                     limit: int) -> ObjectPage:
        return self.inner.list_objects(prefix, older_than=older_than, start_after=start_after, limit=limit)

    def reachable(self) -> bool:
        return True


def test_no_connection_is_held_while_an_upload_moves_bytes(
    dsn: str, owner: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (POOL.sync_max, POOL.acquire_timeout_s) == (4, 5)
    db = Database(dsn, PoolSettings(sync_max=POOL.sync_max, async_max=0, acquire_timeout_s=POOL.acquire_timeout_s))
    objects = Probing(db)
    runtime = assets_api.MediaRuntime(cast(Any, objects), PostgresAssetStore(PostgresJobQueue(db)))
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    app.dependency_overrides[get_timeline_database] = lambda: db
    app.dependency_overrides[appmod._media] = lambda: runtime
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=owner, role="dm")
    image = io.BytesIO()
    Image.new("RGB", (64, 48), (1, 2, 3)).save(image, "PNG")
    try:
        client = TestClient(app)
        made = client.post(f"/campaigns/{CAMPAIGN}/assets", json={
            "schema_version": 1, "command_id": "cmd-db-upload-0000001", "campaign_id": CAMPAIGN, "kind": "image",
            "media_type": "image/png", "size_bytes": len(image.getvalue()), "alt": "A test card"})
        assert made.status_code == 201, made.text
        answer = client.put(f"/campaigns/{CAMPAIGN}/assets/{made.json()['asset_id']}/bytes",
                            content=image.getvalue(), headers={"content-type": "image/png"})
        assert answer.status_code == 200, answer.text
        assert answer.json()["state"] == "ready"
    finally:
        for dependency in (get_timeline_database, appmod._media, require_session):
            app.dependency_overrides.pop(dependency, None)
        db.close()
    assert objects.probes >= 3, "the stream in, the copy out and the processed write were each probed"
    assert objects.failures == []


def test_cues_are_private_idempotent_paginated_and_follow_the_asset(
    dsn: str, owner: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from service import campaign_identity as ident
    from service.conversation_store import new_conversation_id

    first, second, long, pending, foreign = (ident.new_id(ident.ASSET) for _ in range(5))
    other = ident.new_id(ident.CAMPAIGN)
    second_campaign = ident.new_id(ident.CAMPAIGN)
    thread, second_thread, legacy_thread, other_campaign_thread = (new_conversation_id() for _ in range(4))
    with connect(dsn) as conn:
        outsider = int(conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('other@example.com', 'x') RETURNING id"
        ).fetchone()[0])
        conn.execute("INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Other')", (other, outsider))
        conn.execute("INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Next')",
                     (second_campaign, owner))
        for conversation, campaign in [(thread, CAMPAIGN), (second_thread, CAMPAIGN),
                                       (legacy_thread, None), (other_campaign_thread, second_campaign)]:
            conn.execute(
                "INSERT INTO chat.conversations (conversation_id, user_id, campaign_id, started_mode) "
                "VALUES (%s,%s,%s,'gm')",
                (conversation, owner, campaign),
            )
        for asset, campaign, duration in [(first, CAMPAIGN, 900), (second, CAMPAIGN, 1500),
                                         (long, CAMPAIGN, 31_000), (foreign, other, 900)]:
            conn.execute(
                "INSERT INTO campaign.assets (id, campaign_id, kind, state, declared_media_type, declared_size_bytes, "
                "media_type, size_bytes, duration_ms, object_key, tmp_key) "
                "VALUES (%s,%s,'audio','ready','audio/mpeg',100,'audio/mpeg',100,%s,%s,%s)",
                (asset, campaign, duration, new_object_key(), new_tmp_key()),
            )
        conn.execute(
            "INSERT INTO campaign.assets (id, campaign_id, kind, state, declared_media_type, declared_size_bytes, "
            "object_key, tmp_key) VALUES (%s,%s,'audio','uploading','audio/mpeg',100,%s,%s)",
            (pending, CAMPAIGN, new_object_key(), new_tmp_key()),
        )
    db = Database(dsn, PoolSettings(async_max=0))
    runtime = assets_api.MediaRuntime(InMemoryObjectStore(), PostgresAssetStore(PostgresJobQueue(db)))
    principal = SessionData(user_id=owner, role="dm")
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    monkeypatch.setitem(app.dependency_overrides, get_timeline_database, lambda: db)
    monkeypatch.setitem(app.dependency_overrides, get_timeline_store, PostgresTimelineStore)
    monkeypatch.setitem(app.dependency_overrides, appmod._media, lambda: runtime)
    monkeypatch.setitem(app.dependency_overrides, require_session, lambda: principal)
    prefix = f"/campaigns/{CAMPAIGN}/cues"
    try:
        with closing(TestClient(app)) as client:
            def post(asset: str, title: str, command: str):
                return client.post(prefix, json={
                    "schema_version": 1, "campaign_id": CAMPAIGN, "asset_id": asset,
                    "command_id": command, "title": title, "kind": "one_shot",
                })

            def listed(**overrides: object):
                return client.post(prefix + "/list", json={
                    "schema_version": 1, "campaign_id": CAMPAIGN, "search": "", "sort": "name",
                    "archived": False, **overrides,
                })

            assert client.get(prefix + "/capability").status_code == 204
            assert client.get(f"/campaigns/{CAMPAIGN}/assets/{pending}/status").json()["state"] == "uploading"
            assert post(foreign, "Private foreign sound", "cmd-cue-foreign-01").status_code == 404
            assert post(pending, "An unfinished sound", "cmd-cue-pending-01").status_code == 409
            assert post(long, "Long sound", "cmd-cue-too-long-01").status_code == 422
            made = post(first, "Storm % at dusk", "cmd-cue-created-01")
            assert made.status_code == 201, made.text
            assert made.headers["cache-control"] == "no-store"
            cue_id = made.json()["cue_id"]
            assert made.json()["duration_ms"] == 900
            assert post(first, "Storm % at dusk", "cmd-cue-created-01").json()["cue_id"] == cue_id

            def attach(conversation: str):
                return client.post(f"{prefix}/{cue_id}/attach", json={
                    "schema_version": 1, "conversation_id": conversation,
                })

            assert attach(legacy_thread).status_code == 404
            assert attach(other_campaign_thread).status_code == 404
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_attach, second_attach = list(executor.map(attach, [thread, thread]))
            assert first_attach.status_code == 200, first_attach.text
            assert first_attach.json() == second_attach.json()
            attached = first_attach.json()
            assert attached["entry_kind"] == "attached_cue" and attached["cue_id"] == cue_id
            assert set(attached) == {"schema_version", "entry_kind", "entry_id", "cue_id", "created_at"}
            assert client.get(f"/conversations/{thread}/timeline").json()["items"] == [attached]
            assert client.get(f"/conversations/{second_thread}/timeline").json()["items"] == []
            assert client.get(f"{prefix}/{cue_id}").json()["title"] == "Storm % at dusk"
            assert post(first, "Duplicate", "cmd-cue-created-02").status_code == 409
            assert post(second, "Bell", "cmd-cue-created-03").status_code == 201
            page = listed(limit=1).json()
            assert [cue["title"] for cue in page["items"]] == ["Bell"]
            assert [cue["title"] for cue in listed(limit=1, cursor=page["next_cursor"]).json()["items"]] == [
                "Storm % at dusk"
            ]
            assert [cue["cue_id"] for cue in listed(search="% at").json()["items"]] == [cue_id]
            rename = client.patch(f"{prefix}/{cue_id}", json={"schema_version": 1, "title": "Rain"})
            assert rename.json()["kind"] == "one_shot"
            assert rename.json()["title"] == "Rain"
            assert client.post(f"{prefix}/{cue_id}/archive").json()["archived"] is True
            assert attach(second_thread).status_code == 404
            assert client.get(f"{prefix}/{cue_id}").json()["title"] == "Rain"
            assert [cue["title"] for cue in listed(archived=True).json()["items"]] == ["Rain"]
            assert client.post(f"{prefix}/{cue_id}/restore").json()["archived"] is False

            principal = SessionData(user_id=outsider, role="dm")
            assert listed().status_code == 404
            assert attach(thread).status_code == 404
            assert client.get(f"{prefix}/{cue_id}").status_code == 404
            assert client.get(f"/conversations/{thread}/timeline").status_code == 404
            assert client.get(f"/campaigns/{CAMPAIGN}/assets/{first}/status").status_code == 404
            assert client.post(f"{prefix}/{cue_id}/archive").status_code == 404
            principal = SessionData(user_id=owner, role="dm")
            with connect(dsn) as conn:
                conn.execute("DELETE FROM campaign.assets WHERE id = %s", (first,))
            assert [cue["title"] for cue in listed().json()["items"]] == ["Bell"]
            assert client.post(f"{prefix}/{cue_id}/archive").status_code == 404
            assert client.get(f"{prefix}/{cue_id}").status_code == 404
            assert client.get(f"/conversations/{thread}/timeline").json()["items"] == [attached]
            monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=False, store="memory"))
            dark = listed()
            missing = client.post(f"{prefix}/missing", json={})
            assert (dark.status_code, dark.content) == (missing.status_code, missing.content)
    finally:
        db.close()


def test_live_cue_controls_and_archive_confirmation(
    dsn: str, owner: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from service import campaign_identity as ident
    from service import ratelimit

    asset, session = ident.new_id(ident.ASSET), ident.new_id(ident.TABLE_SESSION)
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO campaign.assets (id, campaign_id, kind, state, declared_media_type, declared_size_bytes, "
            "media_type, size_bytes, duration_ms, object_key, tmp_key) "
            "VALUES (%s,%s,'audio','ready','audio/mpeg',100,'audio/mpeg',100,30000,%s,%s)",
            (asset, CAMPAIGN, new_object_key(), new_tmp_key()),
        )
        conn.execute(
            "INSERT INTO campaign.table_sessions (id, campaign_id, gm_user_id, state, expires_at) "
            "VALUES (%s,%s,%s,'live',now() + interval '1 hour')", (session, CAMPAIGN, owner),
        )

    with closing(Database(dsn, PoolSettings(async_max=0))) as db, closing(TestClient(app)) as client:
        runtime = assets_api.MediaRuntime(InMemoryObjectStore(), PostgresAssetStore(PostgresJobQueue(db)))
        principal = SessionData(user_id=owner, role="dm")
        monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
        monkeypatch.setitem(app.dependency_overrides, appmod._media, lambda: runtime)
        monkeypatch.setitem(app.dependency_overrides, require_session, lambda: principal)
        monkeypatch.setitem(app.dependency_overrides, get_timeline_database, lambda: db)
        monkeypatch.setattr(ratelimit, "workbench_write_limiter", ratelimit.SlidingWindowLimiter(60, 3600))
        monkeypatch.setattr(ratelimit, "reveal_stop_limiter", ratelimit.SlidingWindowLimiter(60, 3600))
        prefix = f"/campaigns/{CAMPAIGN}"
        audio = f"{prefix}/sessions/{session}/audio"
        made = client.post(f"{prefix}/cues", json={
            "schema_version": 1, "campaign_id": CAMPAIGN, "command_id": "cmd-cue-playback-001",
            "asset_id": asset, "kind": "one_shot", "title": "Bell",
        })
        assert made.status_code == 201, made.text
        cue = made.json()["cue_id"]
        archive = f"{prefix}/cues/{cue}/archive"
        play = {"schema_version": 1, "cue_id": cue, "command_id": "cmd-cue-playback-002", "loop": False,
                "audio_epoch": 0}
        stop = {"schema_version": 1, "cue_id": None, "command_id": "cmd-cue-playback-003"}
        state = client.get(audio)
        assert state.status_code == 200 and state.headers["cache-control"] == "no-store"
        assert [(frame["slot"], frame["playing"]) for frame in state.json()] == [
            ("ambience", None), ("one_shot", None),
        ]

        principal = SessionData(user_id=owner + 1, role="dm")
        refusals = [
            client.get(audio), client.post(f"{audio}/play", json=play), client.post(f"{audio}/stop", json=stop),
        ]
        assert all(r.status_code == 404 and r.content == refusals[0].content for r in refusals)
        principal = SessionData(user_id=owner, role="player")
        assert client.post(f"{audio}/play", json=play).status_code == 403
        principal = SessionData(user_id=owner, role="dm")
        assert client.post(f"{audio}/play", headers={"origin": "https://other.example"}, json=play).status_code == 403
        assert client.post(f"{audio}/play", json={**play, "audio_epoch": 11}).status_code == 409
        assert client.post(f"{audio}/play", json={**play, "loop": True}).status_code == 422
        heard = client.post(f"{audio}/play", json=play)
        assert heard.status_code == 200, heard.text
        assert heard.json()[1]["playing"]["cue_id"] == cue
        assert client.post(f"{audio}/play", json=play).json() == heard.json()
        assert client.post(archive).status_code == 409
        assert client.get(audio).json() == heard.json()
        assert client.post(archive, params={"confirm_stop": "true"}).json()["archived"] is True
        silent = client.get(audio).json()
        assert all(frame["playing"] is None for frame in silent)
        assert silent[1]["seq"] == heard.json()[1]["seq"] + 1

        # SEC-35: exhaust both the ordinary write and the separate reveal Stop budgets.
        monkeypatch.setattr(ratelimit, "workbench_write_limiter", ratelimit.SlidingWindowLimiter(1, 3600))
        client.post(f"{audio}/play", json=play)
        assert client.post(f"{audio}/play", json=play).status_code == 429
        monkeypatch.setattr(ratelimit, "reveal_stop_limiter", ratelimit.SlidingWindowLimiter(1, 3600))
        ratelimit.check_reveal_stop(owner)
        with pytest.raises(ratelimit.RateLimited):
            ratelimit.check_reveal_stop(owner)
        stopped = client.post(f"{audio}/stop", json=stop)
        assert stopped.status_code == 200, stopped.text
        assert client.post(f"{audio}/stop", json=stop).json() == stopped.json()
        monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=False, store="memory"))
        assert client.get(audio).status_code == 404


def test_archived_cue_delete_reauthenticates_and_cleans_up_audio_and_waveform(
    dsn: str, owner: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from service import campaign_identity as ident

    asset, object_key, tmp_key = ident.new_id(ident.ASSET), new_object_key(), new_tmp_key()
    waveform_key = f"{object_key}/{WAVEFORM_NAME}"
    with connect(dsn) as conn:
        outsider = int(conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('other@example.com', 'x') RETURNING id"
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO campaign.assets (id, campaign_id, kind, state, declared_media_type, declared_size_bytes, "
            "media_type, size_bytes, duration_ms, object_key, tmp_key) "
            "VALUES (%s,%s,'audio','ready','audio/mpeg',100,'audio/mpeg',100,1000,%s,%s)",
            (asset, CAMPAIGN, object_key, tmp_key),
        )
        conn.execute(
            "UPDATE campaign.media_usage SET bytes_reserved = 100, asset_count = 1 WHERE campaign_id = %s", (CAMPAIGN,),
        )
    objects = InMemoryObjectStore()
    objects.put_stream(object_key, [b"test audio"], max_bytes=100)
    objects.put_stream(waveform_key, [b'{"schema_version":1,"peaks":[0,1]}'], max_bytes=100)

    db = Database(dsn, PoolSettings(async_max=0))
    queue = PostgresJobQueue(db)
    runtime = assets_api.MediaRuntime(objects, PostgresAssetStore(queue))
    principal = SessionData(user_id=owner, role="dm")
    checking = threading.Event()
    restored = threading.Event()

    def check_password(password: str) -> None:
        if password == "restore race":
            checking.set()
            assert restored.wait(5)
        elif password != "correct password":
            reauth_failed()

    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    monkeypatch.setitem(app.dependency_overrides, get_timeline_database, lambda: db)
    monkeypatch.setitem(app.dependency_overrides, appmod._media, lambda: runtime)
    monkeypatch.setitem(app.dependency_overrides, appmod._job_driver, lambda: None)
    monkeypatch.setitem(app.dependency_overrides, appmod.reauthenticator, lambda: check_password)
    monkeypatch.setitem(app.dependency_overrides, require_session, lambda: principal)
    prefix = f"/campaigns/{CAMPAIGN}"
    try:
        with closing(TestClient(app)) as client:
            created = client.post(f"{prefix}/cues", json={
                "schema_version": 1, "campaign_id": CAMPAIGN, "command_id": "cmd-cue-remove-0001",
                "asset_id": asset, "title": "Private bell", "kind": "one_shot",
            })
            assert created.status_code == 201, created.text
            path = f"{prefix}/cues/{created.json()['cue_id']}"

            def remove(password: str):
                return client.post(f"{path}/delete", json={"schema_version": 1, "password": password})

            assert remove("correct password").status_code == 409
            assert client.post(f"{path}/archive").status_code == 200
            wrong = remove("wrong password")
            assert wrong.status_code == 403 and wrong.json()["detail"]["code"] == "reauth_failed"
            assert client.delete(f"{prefix}/assets/{asset}").status_code == 404
            principal = SessionData(user_id=outsider, role="dm")
            assert remove("correct password").status_code == 404
            principal = SessionData(user_id=owner, role="dm")

            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(remove, "restore race")
                try:
                    assert checking.wait(5)
                    assert client.post(f"{path}/restore").status_code == 200
                finally:
                    restored.set()
                assert pending.result(timeout=5).status_code == 409

            assert client.post(f"{path}/archive").status_code == 200
            removed = remove("correct password")
            assert removed.status_code == 204 and removed.headers["cache-control"] == "no-store"
            assert remove("correct password").status_code == 404
            assert client.post(f"{path}/restore").status_code == 404
            assert client.get(f"{prefix}/assets/{asset}").status_code == 404
            assert client.get(f"{prefix}/assets/{asset}?rendition=waveform").status_code == 404

        with connect(dsn) as conn:
            assert conn.execute("SELECT state FROM campaign.assets WHERE id = %s", (asset,)).fetchone() == ("deleted",)
            assert conn.execute("SELECT id FROM campaign.audio_cues WHERE asset_id = %s", (asset,)).fetchone() is None
            assert conn.execute(
                "SELECT bytes_reserved, asset_count FROM campaign.media_usage WHERE campaign_id = %s", (CAMPAIGN,),
            ).fetchone() == (0, 0)
            assert conn.execute(
                "SELECT detail FROM audit.events WHERE action = 'asset.deleted' AND object_ref = %s", (asset,),
            ).fetchall() == [({"asset_id": asset},)]
            jobs = conn.execute(
                "SELECT id FROM app.jobs WHERE kind = 'asset.delete' AND dedupe_key = %s", (asset,),
            ).fetchall()
            assert len(jobs) == 1
        runner = JobRunner(queue)
        asset_jobs.register_jobs(runner, db=db, queue=queue, objects=objects)
        runner.run_job(int(jobs[0][0]))
        assert objects.stat_object(object_key) is None and objects.stat_object(waveform_key) is None
        with connect(dsn) as conn:
            assert conn.execute("SELECT id FROM campaign.assets WHERE id = %s", (asset,)).fetchone() is None
    finally:
        db.close()

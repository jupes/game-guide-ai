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
from datetime import datetime
from typing import Any, cast

import pytest
from _pg import connect, needs_db, throwaway_database
from fastapi.testclient import TestClient
from PIL import Image

from service import app as appmod
from service import assets_api
from service import migrations as mig
from service.app import app, get_timeline_database, require_session
from service.asset_store import PostgresAssetStore
from service.db import Database, PoolSettings
from service.jobs import PostgresJobQueue
from service.media_objects import InMemoryObjectStore, MediaSettings, ObjectPage, ObjectStat
from service.session import SessionData

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

"""The GM's media reads and deletes against a real PostgreSQL
(agent-forge-harness-1kg.8.1.3).

AC 7, the byte route's run — **no request holds a database connection while
bytes move** (requirement 3.8, SEC-35). The route's object store is wrapped so
that when the stream is opened and at every chunk boundary it takes every permit
the route's own `Database` has — `DB_POOL_MAX` concurrent transactions — and
requires each within `DB_POOL_TIMEOUT_S` (`docs/migrations.md` section 5). It
cannot pass while the request holds a gate permit: one probe would find the
pool one short and time out. The twin has no permits, so this is PostgreSQL's.

AC 23 on PostgreSQL: an asset that is not `ready` is the one 404 on the byte
route. And the delete's one transaction on PostgreSQL: the tombstone, its job
and its `asset.deleted` row commit together.

Requires DATABASE_URL (CI sets it for this file, `.github/workflows/ci.yml`,
pinned by `service/tests/test_ci_workflow.py`). Without it every test skips.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from _pg import connect, needs_db, throwaway_database
from fastapi.testclient import TestClient

from service import app as appmod
from service import assets_api
from service import media_objects as mo
from service import migrations as mig
from service.app import app, get_timeline_database, require_session
from service.asset_store import DELETE_JOB, Measured, PostgresAssetStore
from service.db import Database, PoolSettings
from service.jobs import PostgresJobQueue
from service.media_objects import InMemoryObjectStore, MediaSettings, ObjectPage, ObjectStat, StoreLimiter
from service.session import SessionData

pytestmark = needs_db

# justification: `cast(Any, ...)` hands the probing double to a Protocol-typed
# parameter, and reads the unit's connection without narrowing its type.

CAMPAIGN = "cmp_" + "s" * 22
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
#: `docs/migrations.md` section 5: the gate's permits and how long a request
#: waits for one. The route's own database is built with exactly these.
POOL = PoolSettings()
#: More than one 256 KiB chunk, so there is a boundary between chunks.
IMAGE = bytes(range(256)) * 1500


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("assetserve") as target:
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
    """An object store that, when a stream is opened and at every chunk
    boundary, takes every permit the gate has — all at once — and records each
    one that did not come."""

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
        return self.inner.put_stream(key, chunks, max_bytes=max_bytes)

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        self._probe()
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


class Driver:
    def __init__(self) -> None:
        self.ran: list[int] = []

    def run_job(self, job_id: int) -> None:
        self.ran.append(job_id)


@pytest.fixture
def served(dsn: str, owner: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Database, Probing, Driver]]:
    assert (POOL.sync_max, POOL.acquire_timeout_s) == (4, 5)
    db = Database(dsn, PoolSettings(sync_max=POOL.sync_max, async_max=0, acquire_timeout_s=POOL.acquire_timeout_s))
    objects = Probing(db)
    driver = Driver()
    runtime = assets_api.MediaRuntime(cast(Any, objects), PostgresAssetStore(PostgresJobQueue(db)))
    monkeypatch.setattr(mo, "STORE_LIMITER", StoreLimiter())
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    app.dependency_overrides[get_timeline_database] = lambda: db
    app.dependency_overrides[appmod._media] = lambda: runtime
    app.dependency_overrides[appmod._job_driver] = lambda: cast(Any, driver)
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=owner, role="dm")
    try:
        yield db, objects, driver
    finally:
        for dependency in (get_timeline_database, appmod._media, appmod._job_driver, require_session):
            app.dependency_overrides.pop(dependency, None)
        db.close()


def _asset(db: Database, objects: Probing, owner: int, state: str) -> str:
    """An image in `state`, made through the PostgreSQL store; its bytes in the
    store once it is `ready`."""
    assets = PostgresAssetStore(PostgresJobQueue(db))
    with db.transaction() as unit:
        made = assets.create(unit, CAMPAIGN, owner_id=owner, kind="image", media_type="image/png",
                             size_bytes=len(IMAGE), alt="A test card", now=T0)
    if state == "uploading":
        return made.id
    with db.transaction() as unit:
        assets.start_processing(unit, CAMPAIGN, made.id, owner_id=owner, now=T0)
    if state == "processing":
        return made.id
    if state == "failed":
        with db.transaction() as unit:
            assets.mark_failed(unit, CAMPAIGN, made.id, owner_id=owner, failure="unreadable", now=T0)
        return made.id
    objects.inner.put_stream(made.object_key, iter([IMAGE]), max_bytes=len(IMAGE))
    with db.transaction() as unit:
        assets.mark_ready(unit, CAMPAIGN, made.id, owner_id=owner, measured=Measured("image/png", len(IMAGE), 40, 30),
                          now=T0)
    if state == "deleted":
        with db.transaction() as unit:
            assets.delete(unit, CAMPAIGN, made.id, owner_id=owner, now=T0)
    return made.id


def test_no_connection_is_held_while_the_byte_route_moves_bytes(
    served: tuple[Database, Probing, Driver], owner: int
) -> None:
    db, objects, _ = served
    asset = _asset(db, objects, owner, "ready")
    probes_before = objects.probes
    client = TestClient(app)
    whole = client.get(f"/campaigns/{CAMPAIGN}/assets/{asset}")
    assert (whole.status_code, whole.content == IMAGE) == (200, True)
    ranged = client.get(f"/campaigns/{CAMPAIGN}/assets/{asset}", headers={"range": "bytes=100-299999"})
    assert (ranged.status_code, ranged.content == IMAGE[100:300000]) == (206, True)
    assert objects.probes - probes_before >= 6, "each open and each chunk boundary was probed"
    assert objects.failures == []


@pytest.mark.parametrize("state", ["uploading", "processing", "failed", "deleted"])
def test_nothing_resolves_on_postgresql_unless_it_is_ready(
    served: tuple[Database, Probing, Driver], owner: int, state: str
) -> None:
    db, objects, _ = served
    asset = _asset(db, objects, owner, state)
    missing = TestClient(app).get(f"/campaigns/{CAMPAIGN}/assets/ast_{'q' * 22}")
    answer = TestClient(app).get(f"/campaigns/{CAMPAIGN}/assets/{asset}")
    assert missing.status_code == 404
    assert (answer.status_code, answer.content) == (missing.status_code, missing.content)
    assert objects.probes == 0, "no read ever reached the store"


def test_a_delete_commits_its_tombstone_job_and_audit_row_together(
    served: tuple[Database, Probing, Driver], owner: int, dsn: str
) -> None:
    db, objects, driver = served
    asset = _asset(db, objects, owner, "ready")
    answer = TestClient(app).delete(f"/campaigns/{CAMPAIGN}/assets/{asset}")
    assert answer.status_code == 204
    with connect(dsn) as conn:
        state = conn.execute("SELECT state, alt FROM campaign.assets WHERE id = %s", (asset,)).fetchone()
        jobs = conn.execute("SELECT id, payload->>'asset_id' FROM app.jobs WHERE kind = %s", (DELETE_JOB,)).fetchall()
        audit = conn.execute(
            "SELECT action, object_kind, object_ref, detail FROM audit.events WHERE campaign_id_tombstone = %s",
            (CAMPAIGN,),
        ).fetchall()
    assert state == ("deleted", None)
    assert [row[1] for row in jobs] == [asset]
    assert driver.ran == [jobs[0][0]]
    assert audit == [("asset.deleted", "asset", asset, {"asset_id": asset})]
    again = TestClient(app).delete(f"/campaigns/{CAMPAIGN}/assets/{asset}")
    assert again.status_code == 404

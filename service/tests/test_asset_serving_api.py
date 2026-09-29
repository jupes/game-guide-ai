"""The GM's media reads and deletes (agent-forge-harness-1kg.8.1.3), through the
real app over the in-memory twins and the in-memory object store.

What a client observes: dark by default and indistinguishable from a missing
path; bytes served same-origin, ranged, and unable to become a page (AC 21);
at most six byte responses at once, none of them on the event loop or on the
request pool's tokens (AC 22); nothing resolves unless `ready` (AC 23); the
route rules; the delete, its audit row and its job; no private text on the
serve or the delete leg (AC 25). That no connection is held while bytes move,
and AC 23 on PostgreSQL, are `tests/test_asset_serving_db.py`'s.

Run from the repo root:
    uv run python -m pytest service/tests/test_asset_serving_api.py -q
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anyio.to_thread
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from httpx import Response

from service import app as appmod
from service import asset_serving_api, assets_api
from service import media_objects as mo
from service.app import app, get_timeline_database, require_session
from service.asset_store import DELETE_JOB, InMemoryAssetStore, Measured, asset_rows, visible_rows
from service.audit_log import InMemoryAuditLog
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase
from service.jobs import InMemoryJobQueue
from service.media_objects import InMemoryObjectStore, MediaSettings, ObjectPage, ObjectStat, StoreLimiter
from service.session import SessionData
from service.spa_fallback import install_spa
from service.workbench_api import FORBIDDEN_ORIGIN_DETAIL, FORBIDDEN_ROLE_DETAIL, NOT_FOUND_DETAIL, api_routes

GM_A, GM_B, PLAYER = 1, 2, 3
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
CANARY_ALT = "Qz9ServeAltCanary"
CANARY_TITLE = "Qz9ServeCampaignCanary"
POLICY = "default-src 'none'; sandbox"
#: Bigger than one 256 KiB chunk, so a response is several pulls.
IMAGE = bytes(range(256)) * 1500
AUDIO = b"ID3" + bytes(range(200)) * 50

# justification: `Any` in this file types the test doubles handed to
# Protocol-typed parameters (`cast(Any, ...)`) and a monkeypatched session
# role; no production value is `Any`.


# ── The world ────────────────────────────────────────────────────────────────


class Objects:
    """The in-memory store, recording on which thread each call and each pull
    ran, able to fail one method and to hold every stream after its first
    chunk until the test lets it go."""

    def __init__(self) -> None:
        self.inner = InMemoryObjectStore(lambda: T0)
        self.threads: list[tuple[str, int]] = []
        self.fail: set[str] = set()
        self.gate: threading.Event | None = None

    def put_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> int:
        return self.inner.put_stream(key, chunks, max_bytes=max_bytes)

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        self.threads.append(("get_stream", threading.get_ident()))
        if "get_stream" in self.fail:
            raise mo.ObjectStoreUnavailable()
        found = self.inner.get_stream(key, offset=offset, length=length)

        def pulled() -> Iterator[bytes]:
            for index, chunk in enumerate(found):
                self.threads.append(("pull", threading.get_ident()))
                if index == 1 and self.gate is not None:
                    assert self.gate.wait(timeout=20), "the test never let the stream go"
                yield chunk

        return pulled()

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
    """The job driver the delete hands its job to, after the response."""

    def __init__(self) -> None:
        self.ran: list[int] = []

    def run_job(self, job_id: int) -> None:
        self.ran.append(job_id)


@dataclass
class World:
    db: InMemoryDatabase
    campaigns: InMemoryCampaignStore
    queue: InMemoryJobQueue
    assets: InMemoryAssetStore
    objects: Objects
    audit: InMemoryAuditLog
    driver: Driver
    limiter: StoreLimiter
    runtime: assets_api.MediaRuntime | None = None
    database: InMemoryDatabase | None = None
    commands: Iterator[int] = field(default_factory=itertools.count)

    def campaign(self, owner: int = GM_A, name: str = "Nocturne") -> str:
        with self.db.transaction() as unit:
            return self.campaigns.create(unit, owner_id=owner, name=name, now=T0).id

    def asset(self, campaign: str, *, state: str = "ready", kind: str = "image", data: bytes = IMAGE,
              declared: str = "image/png", owner: int = GM_A, alt: str | None = "A harbour at dusk") -> str:
        """An asset in `state`, its bytes in the store once it is `ready`."""
        with self.db.transaction() as unit:
            made = self.assets.create(
                unit, campaign, owner_id=owner, kind=kind, media_type=declared, size_bytes=len(data),
                alt=alt if kind == "image" else None, command_id=f"cmd-serve-{next(self.commands):010d}", now=T0,
            )
        if state == "uploading":
            return made.id
        with self.db.transaction() as unit:
            self.assets.start_processing(unit, campaign, made.id, owner_id=owner, now=T0)
        if state == "processing":
            return made.id
        if state == "failed":
            with self.db.transaction() as unit:
                self.assets.mark_failed(unit, campaign, made.id, owner_id=owner, failure="unreadable", now=T0)
            return made.id
        self.objects.put_stream(made.object_key, iter([data]), max_bytes=len(data))
        measured = (Measured(declared, len(data), 40, 30) if kind == "image"
                    else Measured("audio/mpeg", len(data), duration_ms=1000))
        with self.db.transaction() as unit:
            self.assets.mark_ready(unit, campaign, made.id, owner_id=owner, measured=measured, now=T0)
        if state == "deleted":
            with self.db.transaction() as unit:
                self.assets.delete(unit, campaign, made.id, owner_id=owner, now=T0)
        return made.id

    def state(self, asset: str) -> str:
        with self.db.transaction() as unit:
            return visible_rows(asset_rows(self.db), cast(Any, unit))[asset].state


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    db = InMemoryDatabase()
    queue = InMemoryJobQueue(db=db)
    limiter = StoreLimiter()
    made = World(db, InMemoryCampaignStore(db), queue, InMemoryAssetStore(db, queue), Objects(),
                 InMemoryAuditLog(), Driver(), limiter)
    made.runtime = assets_api.MediaRuntime(cast(Any, made.objects), made.assets)
    made.database = db
    monkeypatch.setattr(mo, "STORE_LIMITER", limiter)
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    app.dependency_overrides[get_timeline_database] = lambda: made.database
    app.dependency_overrides[appmod._media] = lambda: made.runtime
    app.dependency_overrides[appmod._job_driver] = lambda: cast(Any, made.driver)
    app.dependency_overrides[asset_serving_api.get_audit_log] = lambda: made.audit
    app.dependency_overrides[assets_api.get_clock] = lambda: (lambda: T0)
    _as(GM_A)
    yield made
    for dependency in (get_timeline_database, appmod._media, appmod._job_driver, asset_serving_api.get_audit_log,
                       assets_api.get_clock, require_session):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(world: World) -> TestClient:
    return TestClient(app)


def _as(user_id: int, role: str = "dm") -> None:
    def session(request: Request) -> SessionData:
        return SessionData(user_id=user_id, role=cast(Any, role))

    app.dependency_overrides[require_session] = session


def _signed_out() -> None:
    def refuse() -> SessionData:
        raise HTTPException(status_code=401, detail="account not found")

    app.dependency_overrides[require_session] = refuse


def _path(campaign: str, asset: str) -> str:
    return f"/campaigns/{campaign}/assets/{asset}"


def _shape(response: Response) -> tuple[Any, ...]:
    headers = tuple(sorted((k.lower(), v) for k, v in response.headers.items() if k.lower() != "date"))
    return response.status_code, response.content, headers


# ── Dark by default, and then indistinguishable from nothing ────────────────


@pytest.mark.parametrize("topology", ["no static mount", "root static mount"])
def test_while_off_the_asset_path_answers_exactly_as_a_missing_path_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, topology: str
) -> None:
    """GET, POST, PUT, PATCH, DELETE and HEAD. OPTIONS and TRACE are out of
    scope: on every existing route they reach FastAPI's own 405."""
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings())
    _as(GM_A)
    saved = list(app.router.routes)
    campaign, asset = "cmp_" + "a" * 22, "ast_" + "b" * 22
    try:
        if topology == "root static mount":
            install_spa(app, tmp_path)
        client = TestClient(app)
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            media = client.request(method, _path(campaign, asset))
            missing = client.request(method, f"/campaigns/{campaign}/assetz/{asset}")
            assert _shape(media) == _shape(missing), method
        # Switched on, the same request reaches the route: the test can fail.
        monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
        app.dependency_overrides[appmod._media] = lambda: None
        assert client.get(_path(campaign, asset)).status_code == 503
    finally:
        app.router.routes[:] = saved
        app.dependency_overrides.pop(appmod._media, None)
        app.dependency_overrides.pop(require_session, None)


def test_the_new_routes_are_media_routes_and_no_api_route_begins_with_assets() -> None:
    """RV-1: the built UI loads its bundle from `/assets/<name>-<hash>.js`."""
    served = {(method, path) for path, route in api_routes(app) for method in (route.methods or ())
              if path == asset_serving_api.ASSET_PATH}
    assert served == {("GET", asset_serving_api.ASSET_PATH), ("DELETE", asset_serving_api.ASSET_PATH)}
    assert all(type(route).__name__ == "MediaRoute" for path, route in api_routes(app)
               if path == asset_serving_api.ASSET_PATH)
    assert not [path for path, _ in api_routes(app) if path == "/assets" or path.startswith("/assets/")]


def test_with_media_on_the_bundle_path_still_reaches_the_built_ui(world: World, tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.info('bundle')", encoding="utf-8")
    saved = list(app.router.routes)
    try:
        install_spa(app, tmp_path)
        bundle = TestClient(app).get("/assets/index-abc123.js")
        assert (bundle.status_code, bundle.text) == (200, "console.info('bundle')")
    finally:
        app.router.routes[:] = saved


# ── AC 21: same-origin, ranged, and never a page ─────────────────────────────


def test_a_ready_image_is_served_whole_with_exactly_sec_19s_headers(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = world.asset(campaign)
    answer = client.get(_path(campaign, asset))
    assert answer.status_code == 200
    assert answer.content == IMAGE
    headers = answer.headers
    assert headers["content-type"] == "image/png"
    assert headers["content-length"] == str(len(IMAGE))
    assert headers["accept-ranges"] == "bytes"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers.get_list("content-security-policy") == [POLICY], "the route's policy, and only it, arrives"
    assert headers["content-disposition"] == "inline"
    assert headers["cache-control"] == "no-store"
    assert "etag" not in headers and "last-modified" not in headers
    assert "cross-origin-resource-policy" not in headers, "CORP is undecided (TA-6, ifq)"
    assert not [name for name in headers if name.startswith("access-control-")]


def test_the_type_served_is_the_one_the_server_recorded(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = world.asset(campaign, kind="audio", data=AUDIO, declared="audio/wav")
    answer = client.get(_path(campaign, asset))
    assert (answer.status_code, answer.headers["content-type"]) == (200, "audio/mpeg")
    assert answer.content == AUDIO


def test_a_range_is_206_and_a_range_past_the_end_is_416(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = world.asset(campaign)
    size = len(IMAGE)
    first = client.get(_path(campaign, asset), headers={"range": "bytes=0-99"})
    assert (first.status_code, first.content) == (206, IMAGE[:100])
    assert (first.headers["content-range"], first.headers["content-length"]) == (f"bytes 0-99/{size}", "100")
    assert first.headers.get_list("content-security-policy") == [POLICY]
    # A part carries exactly the whole's policy: nothing may cache it (AC 21).
    assert (first.headers["content-type"], first.headers["cache-control"]) == ("image/png", "no-store")
    assert (first.headers["x-content-type-options"], first.headers["content-disposition"]) == ("nosniff", "inline")
    assert first.headers["accept-ranges"] == "bytes"
    assert "etag" not in first.headers and "last-modified" not in first.headers
    last = client.get(_path(campaign, asset), headers={"range": f"bytes={size - 1}-{size - 1}"})
    assert (last.status_code, last.content) == (206, IMAGE[-1:])
    tail = client.get(_path(campaign, asset), headers={"range": f"bytes={size - 300_000}-"})
    assert (tail.status_code, tail.content) == (206, IMAGE[-300_000:]), "a window across chunks"
    past = client.get(_path(campaign, asset), headers={"range": f"bytes={size}-"})
    assert (past.status_code, past.content) == (416, b"")
    assert past.headers["content-range"] == f"bytes */{size}"
    assert past.headers["cache-control"] == "no-store"
    malformed = client.get(_path(campaign, asset), headers={"range": "bytes=9-0"})
    assert (malformed.status_code, malformed.content) == (200, IMAGE), "an invalid range is ignored"


def test_a_position_too_long_for_int_is_answered_as_its_value_never_500(client: TestClient, world: World) -> None:
    """Review M-1: a position of more than 4,300 digits once raised `ValueError`
    out of the route (Python's `int()` limit), a bare `500` with none of
    SEC-19's headers. Each is now the answer its value earns."""
    campaign = world.campaign()
    asset = world.asset(campaign)
    size = len(IMAGE)
    huge = "9" * 5000
    past = client.get(_path(campaign, asset), headers={"range": f"bytes={huge}-"})
    assert (past.status_code, past.content, past.headers["content-range"]) == (416, b"", f"bytes */{size}")
    clipped = client.get(_path(campaign, asset), headers={"range": f"bytes=0-{huge}"})
    suffix = client.get(_path(campaign, asset), headers={"range": f"bytes=-{huge}"})
    for answer in (clipped, suffix):
        assert (answer.status_code, answer.content) == (206, IMAGE)
        assert answer.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    for answer in (past, clipped, suffix):
        assert answer.headers.get_list("content-security-policy") == [POLICY]
        assert (answer.headers["cache-control"], answer.headers["x-content-type-options"]) == ("no-store", "nosniff")
    assert mo.STORE_LIMITER.borrowed == 0, "no answer keeps a store token"


# ── AC 23: nothing resolves unless the asset is ready ───────────────────────


def test_anything_not_ready_and_anything_not_yours_is_the_one_identical_404(client: TestClient,
                                                                           world: World) -> None:
    mine, theirs = world.campaign(), world.campaign(GM_B)
    ready = world.asset(mine)
    others = world.asset(theirs, owner=GM_B)
    # Not ready: missing to a read. A GM may still delete their own (L-8).
    unready = {state: _path(mine, world.asset(mine, state=state)) for state in ("uploading", "processing", "failed")}
    # Not the caller's, or gone: missing to a read and to a delete alike.
    missing = {
        "deleted": _path(mine, world.asset(mine, state="deleted")),
        "another GM's": _path(theirs, others),
        "mine, under their campaign": _path(theirs, ready),
        "theirs, under my campaign": _path(mine, others),
        "never minted": _path(mine, "ast_" + "z" * 22),
        "a malformed id": _path(mine, "not-an-id"),
        "a malformed campaign": _path("cmp_nope", ready),
    }
    for method in ("GET", "DELETE"):
        reference = _shape(client.request(method, _path(mine, "ast_" + "q" * 22)))
        assert reference[0] == 404 and client.request(method, _path(mine, "ast_" + "q" * 22)).json() == NOT_FOUND
        for name, path in (missing | unready if method == "GET" else missing).items():
            assert _shape(client.request(method, path)) == reference, (name, method)
    assert world.state(others) == "ready", "another GM's asset survived every attempt"
    assert client.get(_path(mine, ready)).status_code == 200, "and the ready one is served"
    assert world.objects.threads[0][0] == "get_stream"
    assert [kind for kind, _ in world.objects.threads].count("get_stream") == 1, "no 404 ever reached the store"


class Counting:
    """The database, counting the transactions a request opens."""

    def __init__(self, inner: InMemoryDatabase) -> None:
        self.inner = inner
        self.opened = 0

    def transaction(self, *args: Any, **kwargs: Any) -> Any:
        self.opened += 1
        return self.inner.transaction(*args, **kwargs)


def test_a_malformed_id_is_the_one_404_before_any_query(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = world.asset(campaign)
    counting = Counting(world.db)
    world.database = cast(Any, counting)
    for path in (_path(campaign, "not-an-id"), _path("cmp_nope", asset), _path(campaign, "cmp_" + "a" * 22)):
        for method in ("GET", "DELETE"):
            assert client.request(method, path).json() == NOT_FOUND, (path, method)
    assert counting.opened == 0, "the shape is judged before any query"
    assert client.get(_path(campaign, asset)).status_code == 200
    assert counting.opened == 1, "and a well-formed one is resolved in one transaction"


def test_the_route_rules_hold(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = world.asset(campaign)
    _as(PLAYER, role="player")
    for method in ("GET", "DELETE"):
        refused = client.request(method, _path(campaign, asset))
        assert (refused.status_code, refused.json()) == (403, {"detail": dict(FORBIDDEN_ROLE_DETAIL)})
    _signed_out()
    for method in ("GET", "DELETE"):
        assert (client.request(method, _path(campaign, asset)).json()) == {"detail": "not signed in"}
    _as(GM_A)
    for headers in ({"origin": "https://evil.example"}, {"origin": "null"}, {"sec-fetch-site": "cross-site"}):
        forged = client.delete(_path(campaign, asset), headers=headers)
        assert (forged.status_code, forged.json()) == (403, {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}), headers
    assert world.state(asset) == "ready", "a forged delete changed nothing"
    assert client.get(_path(campaign, asset), headers={"sec-fetch-site": "same-origin"}).status_code == 200


def test_an_unavailable_database_runtime_or_store_is_503_and_a_vanished_object_is_404(
    client: TestClient, world: World
) -> None:
    campaign = world.campaign()
    asset = world.asset(campaign)
    world.objects.fail.add("get_stream")
    down = client.get(_path(campaign, asset))
    assert (down.status_code, down.json()["detail"]["code"], down.json()["detail"]["retryable"]) == (
        503, "backend_unavailable", True)
    world.objects.fail.clear()
    runtime, world.runtime = world.runtime, None
    assert client.get(_path(campaign, asset)).status_code == 503
    assert client.delete(_path(campaign, asset)).status_code == 503
    world.runtime, world.database = runtime, None
    assert client.get(_path(campaign, asset)).status_code == 503
    world.database = world.db
    # The row says ready, but the bytes have gone: only a deletion takes them.
    with world.db.transaction() as unit:
        key = world.assets.get(unit, campaign, asset, owner_id=GM_A)
    assert key is not None
    world.objects.inner.delete_object(key.object_key)
    assert client.get(_path(campaign, asset)).json() == NOT_FOUND
    assert world.limiter.borrowed == 0


# ── AC 22: bounded per instance, and never on the event loop ─────────────────


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.01)


@asynccontextmanager
async def _no_lifespan(_: object) -> AsyncIterator[None]:
    yield


def test_the_seventh_concurrent_byte_response_is_503_retry_after_while_six_stream(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seven requests on ONE event loop — the client's portal, which is why it
    is entered, with the app's own startup (a database, the environment's
    settings) swapped for nothing, as `test_e2e_app` does."""
    campaign = world.campaign()
    asset = world.asset(campaign)
    world.objects.gate = threading.Event()
    answers: list[Response] = []
    monkeypatch.setattr(app.router, "lifespan_context", _no_lifespan)
    with TestClient(app) as client:
        portal = client.portal
        assert portal is not None, "entered, the client runs every request on one loop"
        loop_thread = portal.call(threading.get_ident)

        async def default_borrowed() -> float:
            return anyio.to_thread.current_default_thread_limiter().borrowed_tokens

        before = portal.call(default_borrowed)
        streams = [threading.Thread(target=lambda: answers.append(client.get(_path(campaign, asset))))
                   for _ in range(mo.STORE_THREADS)]
        try:
            for stream in streams:
                stream.start()
            _wait_for(lambda: world.limiter.borrowed == mo.STORE_THREADS, "six responses in flight")
            _wait_for(lambda: [kind for kind, _ in world.objects.threads].count("pull") >= 2 * mo.STORE_THREADS,
                      "each held on its second chunk")
            assert portal.call(default_borrowed) == before, "six stalled streams hold no request-pool token"
            seventh = client.get(_path(campaign, asset))
            assert seventh.status_code == 503
            assert seventh.headers["retry-after"] == "2"
            assert seventh.json()["detail"]["code"] == "backend_unavailable"
            assert world.limiter.borrowed == mo.STORE_THREADS
        finally:
            world.objects.gate.set()
            for stream in streams:
                stream.join(timeout=20)
    assert [(answer.status_code, answer.content == IMAGE) for answer in answers] == [(200, True)] * mo.STORE_THREADS
    assert world.limiter.borrowed == 0
    assert world.objects.threads and all(ident != loop_thread for _, ident in world.objects.threads), (
        "every store call and every pull ran off the event loop's thread")
    after = TestClient(app).get(_path(campaign, asset))
    assert after.status_code == 200, "the tokens came back"


def test_every_store_call_in_the_serving_path_goes_through_the_chokepoint() -> None:
    """AC 22's structural half: `test_media_objects`' `ast` check covers every
    service module; this names the two slice c added, so it cannot pass by
    skipping them."""
    from service.tests.test_media_objects import _service_store_calls

    calls = _service_store_calls()
    ours = [where for where, _ in calls if where.startswith(("media_serving.py", "asset_serving_api.py"))]
    assert ours, "the serving path's store call was found"
    assert [where for where, through in calls if not through] == []


# ── Delete (requirement 5.7) ─────────────────────────────────────────────────


def test_a_delete_is_204_a_tombstone_an_audit_row_and_a_job_tried_after_the_response(
    client: TestClient, world: World
) -> None:
    campaign = world.campaign()
    asset = world.asset(campaign)
    answer = client.delete(_path(campaign, asset))
    assert (answer.status_code, answer.content) == (204, b"")
    assert world.state(asset) == "deleted"
    jobs = world.queue.claim([DELETE_JOB], limit=10, now=T0)
    assert [job.payload["asset_id"] for job in jobs] == [asset]
    assert world.driver.ran == [jobs[0].id], "the job was handed to the driver after the response"
    with world.db.transaction() as unit:
        rows = world.audit.for_campaign(unit, campaign)
    assert [(row.action, row.object_kind, row.object_ref, dict(row.detail)) for row in rows] == [
        ("asset.deleted", "asset", asset, {"asset_id": asset})]
    assert client.get(_path(campaign, asset)).json() == NOT_FOUND, "a deleted asset is missing"
    again = client.delete(_path(campaign, asset))
    assert (again.status_code, again.json()) == (404, NOT_FOUND)
    assert len(world.driver.ran) == 1


def test_another_gms_delete_changes_nothing(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = world.asset(campaign)
    _as(GM_B)
    assert client.delete(_path(campaign, asset)).json() == NOT_FOUND
    assert world.state(asset) == "ready" and world.driver.ran == []


# ── AC 25: no private text on the serve and delete legs ─────────────────────


def test_no_canary_reaches_a_log_a_header_an_audit_row_or_a_payload(
    client: TestClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    campaign = world.campaign(name=CANARY_TITLE)
    asset = world.asset(campaign, alt=CANARY_ALT)
    answers = [client.get(_path(campaign, asset)), client.get(_path(campaign, asset), headers={"range": "bytes=0-9"}),
               client.get(_path(campaign, asset), headers={"range": "bytes=999999999-"}),
               client.delete(_path(campaign, asset)), client.get(_path(campaign, asset))]
    with world.db.transaction() as unit:
        audit = world.audit.for_campaign(unit, campaign)
    payloads = [dict(job.payload) for job in world.queue.claim([DELETE_JOB], limit=10, now=T0)]
    sinks = [record.getMessage() for record in caplog.records]
    sinks += [str(record.args) for record in caplog.records]
    sinks += [f"{name}: {value}" for answer in answers for name, value in answer.headers.items()]
    sinks += [answer.text for answer in answers if not answer.headers.get("content-type", "").startswith("image/")]
    sinks += [repr(row) + str(dict(row.detail)) for row in audit] + [str(payload) for payload in payloads]
    assert audit and payloads, "each sink was really read"
    for canary in (CANARY_ALT, CANARY_TITLE):
        assert not [sink for sink in sinks if canary in sink], canary

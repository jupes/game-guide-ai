"""The GM's media upload routes (agent-forge-harness-1kg.8.1.2), through the real
app over the in-memory twins and the in-memory object store.

What a client observes: the capability dark by default and indistinguishable
from a missing path (AC 12); the hostile-media corpus (T-9, AC 13); caps while
streaming (AC 14); decoding bombs (AC 15); nothing served as it arrived (AC 16);
no filename anywhere (AC 17); the route rules (T-2, T-7, AC 19); the proxies
(AC 20); no private text (T-12, AC 25). That no connection is held while bytes
move is `tests/test_assets_api_db.py`'s: the twin has no permits.

The audio half needs `ffmpeg`: it skips on a machine without it, and FAILS in
CI, where `.github/workflows/ci.yml` installs it — a skipped security test is
worse than none.

Run from the repo root:
    uv run python -m pytest service/tests/test_assets_api.py -q
"""

from __future__ import annotations

import ast
import asyncio
import io
import itertools
import logging
import os
import re
import shutil
import subprocess
import zlib
from collections.abc import Callable, Iterable, Iterator, MutableMapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from httpx import Response
from PIL import Image, ImageCms

from service import app as appmod
from service import assets_api
from service import media_processing as mp
from service.app import app, get_timeline_database, require_session
from service.asset_store import DELETE_JOB, SWEEP_JOB, AssetRow, InMemoryAssetStore, asset_rows, visible_rows
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase
from service.jobs import InMemoryJobQueue
from service.media_objects import (
    InMemoryObjectStore,
    MediaSettings,
    ObjectPage,
    ObjectStat,
    ObjectStoreUnavailable,
)
from service.session import SessionData
from service.spa_fallback import install_spa
from service.workbench_api import FORBIDDEN_ORIGIN_DETAIL, FORBIDDEN_ROLE_DETAIL, NOT_FOUND_DETAIL, api_routes
from service.workbench_contracts import ASSET_MAX_BYTES, AssetKind

REPO_ROOT = Path(__file__).resolve().parents[2]
GM_A, GM_B, PLAYER = 1, 2, 3
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
CANARY_FILE = "Qz9FilenameCanary"
CANARY_ALT = "Qz9AltCanary"
CANARY_TITLE = "Qz9CampaignCanary"
MISSING_CAMPAIGN = "cmp_" + "z" * 22
MISSING_ASSET = "ast_" + "z" * 22
_COMMANDS = itertools.count()


# justification: `Any` in this file types what the libraries type loosely —
# JSON request bodies, ASGI messages, Pillow's save options, a monkeypatched
# callable's pass-through arguments — and the test doubles handed to a
# Protocol-typed parameter (`cast(Any, ...)`); no production value is `Any`.

# ── Bytes, built in code (never committed binaries) ─────────────────────────


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def forged_png(width: int, height: int) -> bytes:
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", b"\x00garbage" * 8)
            + png_chunk(b"IEND", b""))


def srgb() -> bytes:
    return cast(bytes, ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())


def exif_with_gps() -> Image.Exif:
    exif = Image.Exif()
    exif[0x010E] = CANARY_TITLE  # ImageDescription
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (51.0, 30.0, 12.0)
    return exif


def image_bytes(fmt: str, size: tuple[int, int] = (40, 30), mode: str = "RGBA", **save: Any) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (10, 120, 200, 90) if mode == "RGBA" else (10, 120, 200)).save(out, fmt, **save)
    return out.getvalue()


def png_with_metadata() -> bytes:
    """tEXt, iCCP and eXIf chunks, each carrying private text."""
    raw = image_bytes("PNG", icc_profile=srgb(), exif=exif_with_gps().tobytes())
    at = raw.index(b"IDAT") - 4
    return raw[:at] + png_chunk(b"tEXt", b"Title\x00" + CANARY_TITLE.encode()) + raw[at:]


def jpeg_with_gps_and_thumbnail() -> bytes:
    """EXIF with GPS, an ICC profile, and a second JPEG hidden in an APP3."""
    raw = image_bytes("JPEG", mode="RGB", exif=exif_with_gps().tobytes(), icc_profile=srgb())
    thumbnail = image_bytes("JPEG", (8, 8), mode="RGB")
    payload = b"THUMB\x00" + thumbnail
    return raw[:2] + b"\xff\xe3" + (len(payload) + 2).to_bytes(2, "big") + payload + raw[2:]


def png_chunk_types(data: bytes) -> list[bytes]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    at, kinds = 8, []
    while at < len(data):
        length = int.from_bytes(data[at:at + 4], "big")
        kinds.append(data[at + 4:at + 8])
        at += 12 + length
    assert at == len(data), "no trailer after IEND"
    return kinds


def jpeg_markers(data: bytes) -> list[int]:
    """Every marker before the scan: SOI, then each segment's."""
    assert data.startswith(b"\xff\xd8")
    at, markers = 2, [0xD8]
    while data[at] == 0xFF and data[at + 1] != 0xDA:
        markers.append(data[at + 1])
        at += 2 + int.from_bytes(data[at + 2:at + 4], "big")
    return markers


def webp_chunk_types(data: bytes) -> list[bytes]:
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    at, kinds = 12, []
    while at < len(data):
        size = int.from_bytes(data[at + 4:at + 8], "little")
        kinds.append(data[at:at + 4])
        at += 8 + size + (size & 1)
    return kinds


# ── The world ────────────────────────────────────────────────────────────────


class Objects:
    """The in-memory store, recording what each write was handed and able to
    fail one method on demand."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.inner = InMemoryObjectStore(clock)
        self.handed: dict[str, list[int]] = {}
        self.fail: set[str] = set()

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise ObjectStoreUnavailable()

    def put_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> int:
        self._check("put_stream")
        sizes = self.handed.setdefault(key, [])

        def counted() -> Iterator[bytes]:
            for chunk in chunks:
                sizes.append(len(chunk))
                yield chunk

        return self.inner.put_stream(key, counted(), max_bytes=max_bytes)

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        self._check("get_stream")
        return self.inner.get_stream(key, offset=offset, length=length)

    def stat_object(self, key: str) -> ObjectStat | None:
        return self.inner.stat_object(key)

    def delete_object(self, key: str) -> None:
        self._check("delete_object")
        self.inner.delete_object(key)

    def list_objects(self, prefix: str, *, older_than: datetime, start_after: str | None = None,
                     limit: int) -> ObjectPage:
        return self.inner.list_objects(prefix, older_than=older_than, start_after=start_after, limit=limit)

    def reachable(self) -> bool:
        return True

    def keys(self) -> list[str]:
        return sorted(key for key in (*self.inner._objects,))

    def read(self, key: str) -> bytes:
        return b"".join(self.inner.get_stream(key))


@dataclass
class World:
    db: InMemoryDatabase
    campaigns: InMemoryCampaignStore
    queue: InMemoryJobQueue
    assets: InMemoryAssetStore
    objects: Objects
    now: list[datetime] = field(default_factory=lambda: [T0])

    def campaign(self, owner: int = GM_A, name: str = "Nocturne") -> str:
        with self.db.transaction() as unit:
            return self.campaigns.create(unit, owner_id=owner, name=name, now=self.now[0]).id

    def rows(self) -> dict[str, AssetRow]:
        with self.db.transaction() as unit:
            return visible_rows(asset_rows(self.db), cast(Any, unit))

    def row(self, asset_id: str) -> AssetRow:
        return self.rows()[asset_id]

    def reserved(self, campaign: str) -> tuple[int, int]:
        with self.db.transaction() as unit:
            usage = self.assets.usage(unit, campaign, owner_id=self.owner_of(campaign))
        assert usage is not None
        return usage.bytes_reserved, usage.asset_count

    def owner_of(self, campaign: str) -> int:
        with self.db.transaction() as unit:
            for owner in (GM_A, GM_B):
                if self.campaigns.get(unit, campaign, owner_id=owner) is not None:
                    return owner
        raise AssertionError("no such campaign")

    def tombstone(self, campaign: str, asset: str, owner: int = GM_A) -> None:
        with self.db.transaction() as unit:
            self.assets.delete(unit, campaign, asset, owner_id=owner, now=self.now[0])

    def payloads(self) -> list[dict[str, Any]]:
        jobs = self.queue.claim([SWEEP_JOB, DELETE_JOB], limit=1000, now=T0 + timedelta(days=30))
        return [dict(job.payload) for job in jobs]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    db = InMemoryDatabase()
    queue = InMemoryJobQueue(db=db)
    made = World(db, InMemoryCampaignStore(db), queue, InMemoryAssetStore(db, queue), Objects(lambda: T0))
    runtime = assets_api.MediaRuntime(cast(Any, made.objects), made.assets)
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    app.dependency_overrides[get_timeline_database] = lambda: made.db
    app.dependency_overrides[appmod._media] = lambda: runtime
    app.dependency_overrides[assets_api.get_clock] = lambda: (lambda: made.now[0])
    _as(GM_A)
    yield made
    for dependency in (get_timeline_database, appmod._media, assets_api.get_clock, require_session):
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
        raise HTTPException(status_code=401, detail="authentication required")

    app.dependency_overrides[require_session] = refuse


def _shape(response: Response) -> tuple[Any, ...]:
    varying = {"date", "content-length"}
    headers = tuple(sorted((k.lower(), v) for k, v in response.headers.items() if k.lower() not in varying))
    return response.status_code, response.content, headers


def _command() -> str:
    return f"cmd-asset-{next(_COMMANDS):010d}-x"


def _body(campaign: str, kind: str = "image", media_type: str = "image/png", size: int = 1000,
          alt: str | None = "A harbour at dusk", command: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"schema_version": 1, "command_id": command or _command(), "campaign_id": campaign,
                            "kind": kind, "media_type": media_type, "size_bytes": size}
    if kind == "image":
        body["alt"] = alt
    return body


def _create(client: TestClient, campaign: str, path_campaign: str | None = None, **given: Any) -> Response:
    return client.post(f"/campaigns/{path_campaign or campaign}/assets", json=_body(campaign, **given))


def _bytes_path(campaign: str, asset: str) -> str:
    return f"/campaigns/{campaign}/assets/{asset}/bytes"


def _put(client: TestClient, campaign: str, asset: str, data: bytes, media_type: str,
         headers: dict[str, str] | None = None) -> Response:
    return client.put(_bytes_path(campaign, asset), content=data,
                      headers={"content-type": media_type, **(headers or {})})


def _upload(client: TestClient, campaign: str, data: bytes, media_type: str, declared: str | None = None,
            **given: Any) -> tuple[str, Response]:
    """Create with `declared` (the sent type by default), then send `data`."""
    kind = "audio" if (declared or media_type).startswith("audio/") else "image"
    made = _create(client, campaign, kind=kind, media_type=declared or media_type,
                   size=min(max(len(data), 1), ASSET_MAX_BYTES[AssetKind(kind)]), **given)
    assert made.status_code == 201, made.text
    asset = made.json()["asset_id"]
    return asset, _put(client, campaign, asset, data, declared or media_type)


def _kept_nothing(world: World, asset: str) -> None:
    row = world.row(asset)
    assert world.objects.keys() == [], "neither the tmp/ object nor a processed one is kept"
    assert row.state == "failed" and row.size_bytes is None and row.width is None


def asgi_put(path: str, chunks: list[bytes], headers: dict[str, str]) -> tuple[int, bytes]:
    """The app driven directly, so a test controls every chunk and can make
    `Content-Length` lie — a client library would not."""
    messages: list[dict[str, Any]] = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ] or [{"type": "http.request", "body": b"", "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), *((k.lower().encode(), v.encode()) for k, v in headers.items())],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


# ── AC 12: dark by default, and then indistinguishable from nothing ─────────

DARK_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")


def _dark_pairs() -> list[tuple[str, str]]:
    campaign, asset = "cmp_" + "a" * 22, "ast_" + "b" * 22
    return [(f"/campaigns/{campaign}/assets", f"/campaigns/{campaign}/assetz"),
            (_bytes_path(campaign, asset), f"/campaigns/{campaign}/assetz/{asset}/bytes")]


@pytest.mark.parametrize("topology", ["no static mount", "root static mount"])
def test_while_off_every_media_path_answers_exactly_as_a_missing_path_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, topology: str
) -> None:
    """GET, POST, PUT, PATCH, DELETE and HEAD, with a well-formed upload's
    headers and body. OPTIONS and TRACE are out of scope: on every existing
    route they reach FastAPI's own 405 (the brief's 'ships OFF' section 3)."""
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings())
    saved = list(app.router.routes)
    try:
        if topology == "root static mount":
            install_spa(app, tmp_path)
        client = TestClient(app)
        headers = {"content-type": "image/png"}
        for method, (media, missing) in itertools.product(DARK_METHODS, _dark_pairs()):
            body = image_bytes("PNG") if method in ("POST", "PUT", "PATCH") else None
            answer = client.request(method, media, content=body, headers=headers)
            assert _shape(answer) == _shape(client.request(method, missing, content=body, headers=headers)), (
                method, media)
        # Switched on, the same requests reach the routes: the test can fail.
        monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
        on = client.put(_dark_pairs()[1][0], content=iter([b"x"]), headers=headers)
        assert on.status_code == 411
    finally:
        app.router.routes[:] = saved


def test_the_real_app_starts_dark_and_no_catch_all_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(appmod._state, "media_settings", raising=False)
    assert appmod._media_enabled() is False, "no startup, no capability"
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings())
    assert appmod._media_enabled() is False, "the default is off"
    monkeypatch.setitem(appmod._state, "media_settings", MediaSettings(enabled=True, store="memory"))
    assert appmod._media_enabled() is True
    media = [route for path, route in api_routes(app) if "/assets" in path]
    # Two upload routes, and 1kg.8.1.3's read and delete (`asset_serving_api`).
    assert len(media) == 4 and all(isinstance(route, assets_api.WorkbenchRoute) for route in media)
    assert not [path for path, _ in api_routes(app) if ":path}" in path]
    app.openapi_schema = None
    assert not [path for path in app.openapi()["paths"] if "/assets" in path], "no announcement either"


def test_no_api_route_begins_with_assets() -> None:
    """RV-1: the built UI loads its bundle from /assets/<name>-<hash>.js."""
    assert not [path for path, _ in api_routes(app) if path == "/assets" or path.startswith("/assets/")]


# ── AC 20: the proxies carry the prefix, and the bytes path's posture ───────


def test_the_media_routes_are_visible_to_the_proxy_guard() -> None:
    from tests.test_proxy_contract import _route_prefixes

    assert "campaigns" in _route_prefixes()


def _location(conf: str, opener: str) -> str:
    found = re.search(re.escape(opener) + r"\s*\{(.*?)\n    \}", conf, re.S)
    assert found is not None, opener
    return found.group(1)


def test_nginx_streams_uploads_on_the_bytes_path_only() -> None:
    conf = (REPO_ROOT / "ui" / "nginx.conf").read_text(encoding="utf-8")
    pattern = r"^/campaigns/[^/]+/assets/[^/]+/bytes$"
    upload = _location(conf, f"location ~ {pattern}")
    size = re.search(r"client_max_body_size\s+(\d+)m;", upload)
    assert size is not None and int(size.group(1)) * 1024 * 1024 >= max(ASSET_MAX_BYTES.values())
    assert re.search(r"proxy_request_buffering\s+off;", upload)
    assert re.search(r"proxy_read_timeout\s+180s;", upload)
    assert "add_header" not in upload, "a location with its own add_header loses the server-level policies"
    general = _location(conf, "location /campaigns")
    assert "proxy_request_buffering" not in general
    assert re.fullmatch(pattern[1:-1], _bytes_path("cmp_" + "a" * 22, "ast_" + "b" * 22))
    for other in ("/campaigns/c/assets", "/campaigns/c/documents/d", "/campaigns/c/assets/a/bytes/x"):
        assert not re.fullmatch(pattern[1:-1], other), other


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_answers_an_uploading_asset_and_reserves_its_declared_size(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    made = _create(client, campaign, size=4321, command="cmd-create-000000001")
    assert made.status_code == 201
    asset = made.json()
    assert (asset["state"], asset["media_type"], asset["size_bytes"], asset["alt"]) == (
        "uploading", "image/png", 4321, "A harbour at dusk")
    assert world.reserved(campaign) == (4321, 1)
    replay = _create(client, campaign, size=99, command="cmd-create-000000001")
    assert (replay.status_code, replay.json()["asset_id"]) == (201, asset["asset_id"])
    assert world.reserved(campaign) == (4321, 1), "a replay reserves nothing"


def test_a_full_quota_is_cap_reached_and_writes_nothing(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from service import asset_store

    campaign = world.campaign()
    monkeypatch.setattr(asset_store, "QUOTA_BYTES", 1500)
    assert _create(client, campaign, size=1000).status_code == 201
    refused = _create(client, campaign, size=1000)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "cap_reached"
    assert world.reserved(campaign) == (1000, 1) and len(world.rows()) == 1


def test_the_path_campaign_is_authoritative_and_checked_before_the_body(client: TestClient, world: World) -> None:
    mine, theirs = world.campaign(), world.campaign(GM_B)
    mismatch = _create(client, world.campaign(), path_campaign=mine)
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["field"] == "campaign_id"
    # Not mine: the one 404, never the 422, whatever the body says.
    assert _create(client, mine, path_campaign=theirs).json() == NOT_FOUND
    assert _create(client, theirs, path_campaign=theirs).json() == NOT_FOUND


def test_a_filename_field_is_refused_by_the_contract(client: TestClient, world: World) -> None:
    body = {**_body(world.campaign()), "filename": CANARY_FILE}
    refused = client.post(f"/campaigns/{body['campaign_id']}/assets", json=body)
    assert refused.status_code == 422
    assert CANARY_FILE not in refused.text


# ── Upload: the happy paths, and what is served (AC 16) ─────────────────────


def test_a_png_is_re_encoded_as_png_without_a_single_ancillary_chunk(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    upload = png_with_metadata()
    assert {b"tEXt", b"iCCP", b"eXIf"} <= set(png_chunk_types(upload))
    asset, answer = _upload(client, campaign, upload, "image/png")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["state"], body["width"], body["height"], body["media_type"]) == ("ready", 40, 30, "image/png")
    row = world.row(asset)
    served = world.objects.read(row.object_key)
    assert world.objects.keys() == [row.object_key], "the tmp/ object is gone"
    assert set(png_chunk_types(served)) <= {b"IHDR", b"IDAT", b"IEND"}
    assert CANARY_TITLE.encode() not in served
    with Image.open(io.BytesIO(served)) as out:
        assert (out.format, out.mode, out.size) == ("PNG", "RGBA", (40, 30)), "alpha kept"
    assert body["size_bytes"] == len(served) == row.size_bytes
    assert world.reserved(campaign) == (len(served), 1), "the real size replaced the declared one"


def test_a_jpeg_loses_its_exif_gps_icc_and_hidden_thumbnail(client: TestClient, world: World) -> None:
    upload = jpeg_with_gps_and_thumbnail()
    assert {0xE1, 0xE2, 0xE3} <= set(jpeg_markers(upload))
    asset, answer = _upload(client, world.campaign(), upload, "image/jpeg")
    assert answer.status_code == 200, answer.text
    served = world.objects.read(world.row(asset).object_key)
    assert not {marker for marker in jpeg_markers(served) if 0xE1 <= marker <= 0xEF}, "APP1 to APP15 are gone"
    assert served.count(b"\xff\xd8") == 1 and b"THUMB" not in served and b"Exif" not in served
    assert CANARY_TITLE.encode() not in served
    with Image.open(io.BytesIO(served)) as out:
        assert out.format == "JPEG" and not out.getexif()


def test_a_webp_loses_its_exif_and_icc_chunks(client: TestClient, world: World) -> None:
    upload = image_bytes("WEBP", exif=exif_with_gps().tobytes(), icc_profile=srgb())
    assert {b"EXIF", b"ICCP"} <= set(webp_chunk_types(upload))
    asset, answer = _upload(client, world.campaign(), upload, "image/webp")
    assert answer.status_code == 200, answer.text
    served = world.objects.read(world.row(asset).object_key)
    assert not {b"EXIF", b"ICCP", b"XMP "} & set(webp_chunk_types(served))
    assert CANARY_TITLE.encode() not in served


def test_a_polyglot_trailer_is_not_served(client: TestClient, world: World) -> None:
    upload = image_bytes("PNG") + b"<html><script>alert(document.cookie)</script></html>"
    asset, answer = _upload(client, world.campaign(), upload, "image/png")
    assert answer.status_code == 200
    served = world.objects.read(world.row(asset).object_key)
    assert b"<script" not in served and served.endswith(png_chunk(b"IEND", b""))


def test_processing_runs_through_the_one_gate(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[bool] = []
    real = mp.process

    def watched(*args: Any, **kwargs: Any) -> mp.Processed:
        held.append(mp.GATE.busy())
        return real(*args, **kwargs)

    monkeypatch.setattr(mp, "process", watched)
    assert _upload(client, world.campaign(), image_bytes("PNG"), "image/png")[1].status_code == 200
    assert held == [True] and not mp.GATE.busy()


# ── AC 13: the hostile corpus — status, reason, and nothing kept ────────────

CORPUS: dict[str, tuple[bytes, str, str, int, str]] = {
    # name: (bytes, sent and declared type, the type actually declared at create, status, failure)
    "a PNG declared as JPEG": (image_bytes("PNG"), "image/jpeg", "image/jpeg", 415, "unsupported_type"),
    "an HTML page declared as PNG": (b"<!doctype html><script>x()</script>" * 3, "image/png", "image/png", 415,
                                     "unsupported_type"),
    "an SVG": (b'<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script></svg>', "image/png", "image/png",
               415, "unsupported_type"),
    "a GIF": (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01", "image/png",
              "image/png", 415, "unsupported_type"),
    "a zip archive": (b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + zlib.compress(b"\x00" * 100_000), "image/png",
                      "image/png", 415, "unsupported_type"),
    "a zero-byte body": (b"", "image/png", "image/png", 415, "unsupported_type"),
    "a PNG whose header claims 40 megapixels": (forged_png(8000, 5000), "image/png", "image/png", 422,
                                                "too_many_pixels"),
    "a PNG a side of which is past 8,192": (forged_png(9000, 10), "image/png", "image/png", 422, "too_many_pixels"),
    "a truncated PNG": (image_bytes("PNG")[:60], "image/png", "image/png", 422, "unreadable"),
}


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_the_hostile_corpus_is_refused_and_nothing_is_kept(client: TestClient, world: World, name: str) -> None:
    data, sent, declared, status, failure = CORPUS[name]
    campaign = world.campaign()
    asset, answer = _upload(client, campaign, data, sent, declared)
    assert (answer.status_code, answer.json()["state"], answer.json()["failure"]) == (status, "failed", failure)
    _kept_nothing(world, asset)
    assert world.reserved(campaign) == (0, 0), "a failed asset holds no quota"


def test_a_content_type_other_than_the_declared_one_is_415_and_changes_nothing(
    client: TestClient, world: World
) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()["asset_id"]
    refused = _put(client, campaign, made, image_bytes("PNG"), "image/jpeg")
    assert refused.status_code == 415
    assert refused.json()["detail"]["field"] == "content-type"
    assert world.row(made).state == "uploading" and world.objects.keys() == []


# ── AC 14: caps while streaming, never after buffering ──────────────────────


def test_one_byte_past_the_cap_ends_the_stream_holding_one_chunk_at_most(world: World) -> None:
    client = TestClient(app)
    campaign = world.campaign()
    cap = ASSET_MAX_BYTES[AssetKind.IMAGE]
    asset = _create(client, campaign, size=cap).json()["asset_id"]
    piece = 64 * 1024
    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * cap  # one chunk more than it may be, and then some
    chunks = [body[at:at + piece] for at in range(0, cap + 1, piece)]
    assert sum(map(len, chunks)) > cap
    status, answer = asgi_put(_bytes_path(campaign, asset), chunks,
                              {"content-type": "image/png", "content-length": str(cap)})
    assert status == 413 and b'"too_large"' in answer
    handed = world.objects.handed[world.row(asset).tmp_key]
    assert max(handed) <= piece, "never more than one chunk at a time"
    assert sum(handed) <= cap + piece, "the stream stopped at the first chunk past the cap"
    _kept_nothing(world, asset)


@pytest.mark.parametrize("lie", ["longer than it said", "shorter than it said"])
def test_a_content_length_that_lies_in_either_direction_is_unreadable(world: World, lie: str) -> None:
    client = TestClient(app)
    campaign = world.campaign()
    data = image_bytes("PNG")
    asset = _create(client, campaign, size=len(data)).json()["asset_id"]
    said = len(data) - 10 if lie == "longer than it said" else len(data) + 10
    status, answer = asgi_put(_bytes_path(campaign, asset), [data[:20], data[20:]],
                              {"content-type": "image/png", "content-length": str(said)})
    assert status == 422 and b'"unreadable"' in answer
    if lie == "longer than it said":
        handed = world.objects.handed[world.row(asset).tmp_key]
        assert sum(handed) <= said, "the stream stopped at the first chunk past its declared length"
    _kept_nothing(world, asset)


def test_a_body_without_a_length_is_411(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = _create(client, campaign).json()["asset_id"]
    refused = client.put(_bytes_path(campaign, asset), content=iter([image_bytes("PNG")]),
                         headers={"content-type": "image/png"})
    assert refused.status_code == 411 and refused.json()["detail"]["field"] == "content-length"
    bare = {"no length and no encoding": {}, "a length that is not digits": {"content-length": "+9"}}
    for label, headers in bare.items():
        status, _ = asgi_put(_bytes_path(campaign, asset), [image_bytes("PNG")],
                             {"content-type": "image/png", **headers})
        assert status == 411, label
    assert world.row(asset).state == "uploading"


# ── AC 15: one at a time, and a bounded queue ───────────────────────────────


def test_a_queue_wait_past_its_bound_is_503_retry_after_and_the_asset_waits_again(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mp, "QUEUE_BOUND_S", 0.2)
    campaign = world.campaign()
    asset = _create(client, campaign).json()["asset_id"]
    assert asyncio.run(mp.GATE.acquire(0))
    try:
        answer = _put(client, campaign, asset, image_bytes("PNG"), "image/png")
    finally:
        mp.GATE.release()
    assert answer.status_code == 503 and answer.headers["retry-after"] == str(mp.RETRY_AFTER_S)
    assert answer.json()["detail"]["code"] == "backend_unavailable"
    assert world.row(asset).state == "uploading" and world.objects.keys() == []
    again = _put(client, campaign, asset, image_bytes("PNG"), "image/png")
    assert again.status_code == 200, "the retry is served"


# ── AC 19: every Workbench route rule ───────────────────────────────────────


def _routes(campaign: str, asset: str) -> dict[str, Callable[[TestClient], Response]]:
    return {
        "create": lambda c: c.post(f"/campaigns/{campaign}/assets", json=_body(campaign)),
        "bytes": lambda c: c.put(_bytes_path(campaign, asset), content=image_bytes("PNG"),
                                 headers={"content-type": "image/png"}),
    }


def test_nothing_that_is_not_yours_is_distinguishable_from_nothing(client: TestClient, world: World) -> None:
    mine, other_mine, theirs = world.campaign(), world.campaign(), world.campaign(GM_B)
    _as(GM_B)
    their_asset = _create(client, theirs).json()["asset_id"]
    their_ready = _upload(client, theirs, image_bytes("PNG"), "image/png")[0]
    _as(GM_A)
    my_other = _create(client, other_mine).json()["asset_id"]
    my_deleted = _create(client, mine).json()["asset_id"]
    world.tombstone(mine, my_deleted)
    missing = _routes(mine, MISSING_ASSET)["bytes"](client)
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND)
    baseline = _shape(missing)
    cases = {
        "a never-minted campaign": (MISSING_CAMPAIGN, MISSING_ASSET),
        "another GM's campaign and asset": (theirs, their_asset),
        "another GM's ready asset (a 409 for its owner)": (theirs, their_ready),
        "another GM's asset under my campaign": (mine, their_asset),
        "my other campaign's asset under this one": (mine, my_other),
        "my deleted asset": (mine, my_deleted),
        "a malformed campaign id": ("not-an-id", MISSING_ASSET),
        "a malformed asset id": (mine, "ast_" + "!" * 22),
    }
    for label, (campaign, asset) in cases.items():
        assert _shape(_routes(campaign, asset)["bytes"](client)) == baseline, label
        if campaign != mine:
            assert _shape(_routes(campaign, asset)["create"](client)) == baseline, label
    wrong_type = client.put(_bytes_path(theirs, their_asset), content=image_bytes("PNG"),
                            headers={"content-type": "image/jpeg"})
    assert _shape(wrong_type) == baseline, "a 415 is unreachable across tenants"


def test_a_role_failure_is_one_403_and_a_missing_session_is_the_one_401(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = _create(client, campaign).json()["asset_id"]
    _as(PLAYER, role="player")
    for name, call in _routes(campaign, asset).items():
        answer = call(client)
        assert (answer.status_code, answer.json()) == (403, {"detail": dict(FORBIDDEN_ROLE_DETAIL)}), name
    _signed_out()
    for name, call in _routes(campaign, asset).items():
        answer = call(client)
        assert (answer.status_code, answer.json()) == (401, {"detail": "not signed in"}), name


FORGED: dict[str, dict[str, str]] = {
    "a foreign Origin": {"origin": "https://evil.example"},
    "Origin: null": {"origin": "null"},
    "Sec-Fetch-Site: cross-site": {"sec-fetch-site": "cross-site"},
}


def test_forged_requests_are_refused_before_anything_is_read(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = _create(client, campaign).json()["asset_id"]
    refused = {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}
    for label, headers in FORGED.items():
        create = client.post(f"/campaigns/{campaign}/assets", json=_body(campaign), headers=headers)
        upload = _put(client, campaign, asset, image_bytes("PNG"), "image/png", headers)
        assert (create.status_code, create.json()) == (403, refused), label
        assert (upload.status_code, upload.json()) == (403, refused), label
    form = client.post(f"/campaigns/{campaign}/assets", content=b"a=1",
                       headers={"content-type": "application/x-www-form-urlencoded"})
    json_bytes = _put(client, campaign, asset, b"{}", "application/json")
    png_create = client.post(f"/campaigns/{campaign}/assets", content=image_bytes("PNG"),
                             headers={"content-type": "image/png"})
    for label, answer in {"form create": form, "JSON bytes": json_bytes, "image create": png_create}.items():
        assert (answer.status_code, answer.json()) == (403, refused), label
    assert world.row(asset).state == "uploading" and len(world.rows()) == 1


def test_an_unavailable_store_or_runtime_is_503_backend_unavailable(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    asset = _create(client, campaign).json()["asset_id"]
    world.objects.fail.add("put_stream")
    down = _put(client, campaign, asset, image_bytes("PNG"), "image/png")
    assert (down.status_code, down.json()["detail"]["code"], down.json()["detail"]["retryable"]) == (
        503, "backend_unavailable", True)
    assert world.row(asset).state == "uploading"
    app.dependency_overrides[appmod._media] = lambda: None
    for name, call in _routes(campaign, asset).items():
        answer = call(client)
        assert (answer.status_code, answer.json()["detail"]["code"]) == (503, "backend_unavailable"), name


def test_bytes_for_an_asset_that_is_no_longer_uploading_is_409_for_its_owner(client: TestClient,
                                                                             world: World) -> None:
    campaign = world.campaign()
    asset, first = _upload(client, campaign, image_bytes("PNG"), "image/png")
    assert first.status_code == 200
    again = _put(client, campaign, asset, image_bytes("PNG"), "image/png")
    assert (again.status_code, again.json()["detail"]["code"]) == (409, "conflict")


def test_a_final_transition_that_loses_deletes_what_processing_wrote(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section N: a tombstone won while the file was processed."""
    campaign = world.campaign()
    asset = _create(client, campaign).json()["asset_id"]
    real = mp.process

    def then_deleted(*args: Any, **kwargs: Any) -> mp.Processed:
        made = real(*args, **kwargs)
        world.tombstone(campaign, asset)
        return made

    monkeypatch.setattr(mp, "process", then_deleted)
    answer = _put(client, campaign, asset, image_bytes("PNG"), "image/png")
    assert answer.json() == NOT_FOUND
    assert world.objects.keys() == [], "neither the processed object nor the tmp/ one is left"


# ── AC 17 and AC 25: no filename, no private text ───────────────────────────


def test_a_filename_and_the_private_text_reach_no_sink(
    client: TestClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    upload = png_with_metadata()  # built first: Pillow logs what it writes, at DEBUG
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    campaign = world.campaign(name=CANARY_TITLE)
    made = _create(client, campaign, alt=CANARY_ALT).json()["asset_id"]
    disposition = {"content-disposition": f'attachment; filename="{CANARY_FILE}.png"'}
    answer = _put(client, campaign, made, upload, "image/png", disposition)
    assert answer.status_code == 200
    refused = _put(client, campaign, made, upload, "image/png", disposition)
    assert caplog.records, "the capture is live"
    sinks = {
        "logs": " ".join(f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records),
        "response headers": repr(sorted(answer.headers.items()) + sorted(refused.headers.items())),
        "responses": refused.text,
        "stored rows": repr([asdict(row) for row in world.rows().values()]),
        "object keys": repr(world.objects.keys()),
        "job payloads": repr(world.payloads()),
        "served bytes": repr(world.objects.read(world.row(made).object_key)),
    }
    for sink, text in sinks.items():
        assert CANARY_FILE not in text, sink
        assert CANARY_TITLE not in text, sink
        if sink not in ("stored rows",):
            assert CANARY_ALT not in text, sink
    assert CANARY_FILE not in answer.text
    assert CANARY_ALT not in repr(world.row(made)), "the row's repr hides the alt text"


def test_no_media_function_takes_a_filename() -> None:
    names = ["assets_api.py", "media_processing.py", "media_objects.py", "asset_store.py", "asset_jobs.py"]
    found = []
    for name in names:
        tree = ast.parse((REPO_ROOT / "service" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                found += [f"{name}:{arg.arg}" for arg in arguments if "filename" in arg.arg.lower()]
    assert found == []
    tree = ast.parse((REPO_ROOT / "service" / "assets_api.py").read_text(encoding="utf-8"))
    read = [node.value.lower() for node in ast.walk(tree) if isinstance(node, ast.Constant)
            and isinstance(node.value, str)]
    assert "content-disposition" not in read, "nothing reads the header"
    assert "content-type" in read, "the check above can see a header name"


# ── The audio half: real ffmpeg, the served bytes (AC 13, 14, 16) ────────────


@pytest.fixture(scope="module")
def audio(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        if os.environ.get("CI"):
            pytest.fail("ffmpeg and ffprobe must be installed in CI (.github/workflows/ci.yml)")
        pytest.skip("ffmpeg is not installed here; CI runs these")
    work = tmp_path_factory.mktemp("audio")
    cover = work / "cover.png"
    cover.write_bytes(image_bytes("PNG", (32, 32), mode="RGB"))
    meta = work / "meta.txt"
    meta.write_text(f";FFMETADATA1\ntitle={CANARY_TITLE}\nartist={CANARY_TITLE}\nlyrics={CANARY_TITLE}\n"
                    f"[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle={CANARY_TITLE}\n", encoding="utf-8")
    sine = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    made = {
        "mp3": ["-i", str(cover), "-i", str(meta), "-map", "0:a", "-map", "1:v", "-map_metadata", "2",
                "-map_chapters", "2", "-c:a", "libmp3lame", "-b:a", "64k", "-c:v", "copy",
                "-disposition:v:0", "attached_pic", "-id3v2_version", "3", "-f", "mp3"],
        "ogg": ["-c:a", "libvorbis", "-metadata", f"title={CANARY_TITLE}", "-f", "ogg"],
        "m4a": ["-c:a", "aac", "-metadata", f"title={CANARY_TITLE}", "-f", "ipod"],
        "wav": ["-c:a", "pcm_s16le", "-metadata", f"title={CANARY_TITLE}", "-f", "wav"],
    }
    out: dict[str, bytes] = {}
    for name, args in made.items():
        target = work / f"clip.{name}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", *sine, *args, str(target)], check=True, timeout=120)
        out[name] = target.read_bytes()
    long_wav = ["-f", "lavfi", "-i", "sine=frequency=330:duration=40:sample_rate=44100", "-ac", "2",
                "-c:a", "pcm_s16le", "-f", "wav"]
    too_long = ["-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "601", "-c:a", "libmp3lame", "-b:a", "8k",
                "-f", "mp3"]
    video = ["-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5", "-f", "lavfi", "-i",
             "sine=duration=1", "-c:v", "mpeg4", "-c:a", "aac", "-f", "mp4"]
    for name, args in {"forty seconds": long_wav, "601 seconds": too_long, "video": video}.items():
        target = work / name.replace(" ", "_")
        subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(target)], check=True, timeout=120)
        out[name] = target.read_bytes()
    return out


AUDIO_TYPES = {"mp3": "audio/mpeg", "ogg": "audio/ogg", "m4a": "audio/mp4", "wav": "audio/wav"}


@pytest.mark.parametrize("name", sorted(AUDIO_TYPES))
def test_audio_is_transcoded_to_mp3_with_no_tag_chapter_or_picture(
    client: TestClient, world: World, audio: dict[str, bytes], name: str
) -> None:
    upload = audio[name]
    assert CANARY_TITLE.encode() in upload or name == "ogg", "the fixture really carries the canary"
    asset, answer = _upload(client, world.campaign(), upload, AUDIO_TYPES[name])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["state"], body["media_type"]) == ("ready", "audio/mpeg")
    assert 1900 <= body["duration_ms"] <= 2200
    served = world.objects.read(world.row(asset).object_key)
    assert not served.startswith(b"ID3") and b"TAG" != served[-128:-125], "no ID3v2, no ID3v1"
    for marker in (CANARY_TITLE.encode(), b"APIC", b"CHAP", b"\x89PNG", b"\x03vorbis", b"Lavc", b"TSSE"):
        assert marker not in served, marker
    assert mp.sniff(served[: mp.SNIFF_BYTES]) == "audio/mpeg"


def test_a_forty_second_six_megabyte_clip_is_accepted_the_cue_decides_its_kind(
    client: TestClient, world: World, audio: dict[str, bytes]
) -> None:
    upload = audio["forty seconds"]
    assert len(upload) > 5_000_000
    _, answer = _upload(client, world.campaign(), upload, "audio/wav")
    assert answer.status_code == 200, answer.text
    assert 39_900 <= answer.json()["duration_ms"] <= 40_100


def test_audio_past_ten_minutes_is_too_long_and_video_is_unsupported(
    client: TestClient, world: World, audio: dict[str, bytes]
) -> None:
    campaign = world.campaign()
    asset, answer = _upload(client, campaign, audio["601 seconds"], "audio/mpeg")
    assert (answer.status_code, answer.json()["failure"]) == (422, "too_long")
    _kept_nothing(world, asset)
    asset, answer = _upload(client, campaign, audio["video"], "audio/mp4")
    assert (answer.status_code, answer.json()["failure"]) == (415, "unsupported_type")
    _kept_nothing(world, asset)


# ── AC 11: startup reads the switch, and a store is built only when named ────


def test_startup_refuses_the_capability_with_no_store_naming_no_value() -> None:
    from service.media_objects import MediaSettingsError, startup_settings

    assert startup_settings({}) == MediaSettings()
    assert startup_settings({"WORKBENCH_MEDIA_STORE": "filesystem", "WORKBENCH_MEDIA_DIR": str(REPO_ROOT)}).store
    with pytest.raises(MediaSettingsError) as caught:
        startup_settings({"WORKBENCH_MEDIA_ENABLED": "true"})
    assert str(caught.value) == "WORKBENCH_MEDIA_ENABLED needs WORKBENCH_MEDIA_STORE to name a store"


def test_a_bad_media_setting_stops_the_apps_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from service.media_objects import MediaSettingsError

    monkeypatch.setenv("WORKBENCH_MEDIA_ENABLED", "maybe")
    saved = dict(appmod._state)
    try:
        with pytest.raises(MediaSettingsError), TestClient(app):
            pass
    finally:
        appmod._state.clear()
        appmod._state.update(saved)


MEDIA_KINDS = {"asset.delete", "asset.sweep_stuck", "asset.reconcile_orphans"}


def test_the_stores_build_media_only_when_a_store_is_named_whatever_the_switch(tmp_path: Path) -> None:
    """With none named nothing is built or registered (no bucket, no client, no
    cost); with one named its three kinds register even while the routes are
    off, because a deployment switched off still owes its deletions (MS-3)."""
    saved = dict(appmod._state)
    try:
        for settings, built in ((MediaSettings(), False),
                                (MediaSettings(store="filesystem", media_dir=tmp_path), True)):
            appmod._state.clear()
            appmod._state["media_settings"] = settings
            appmod._build_stores(appmod.Database("postgresql://nobody@127.0.0.1:1/none"))
            kinds = set(appmod._state["jobs"].runner._handlers)
            assert MEDIA_KINDS & kinds == (MEDIA_KINDS if built else set()), settings
            assert ("media" in appmod._state) is built, settings
    finally:
        appmod._state.clear()
        appmod._state.update(saved)


def test_ci_installs_ffmpeg_before_the_suite_runs() -> None:
    """Open question 4, answered (a): the python-tests job installs the tools,
    so the audio tests above run in CI, where their fixture refuses to skip."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  python-tests:\n", 1)[1].split("\n  ui-tests:\n", 1)[0]
    install = job.index("apt-get install -y --no-install-recommends ffmpeg")
    assert install < job.index("python -m pytest -q --cov")

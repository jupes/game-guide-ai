"""An in-process Cloud Storage emulator for the media store's tests
(agent-forge-harness-1kg.8.1.4).

The real `google-cloud-storage` client talks HTTP to this, on 127.0.0.1 and a
port the operating system picks, with anonymous credentials: no bucket, no
account, no key, no network beyond the loopback. It serves the slice of the
JSON API the store uses, the way Cloud Storage answers it:

* object metadata `GET` and `DELETE` (404 when absent);
* media download with a `Range`, `ifGenerationMatch` (412 on a mismatch) and
  the whole object's `x-goog-hash`;
* the listing: `prefix`, an inclusive `startOffset`, `maxResults` per page, and
  a page token, in byte order;
* the resumable upload: an initiating `POST` that answers a session URL, then
  `PUT`s with `Content-Range: bytes a-b/*` (308 and the persisted `Range`) and a
  final `bytes a-b/N` or `bytes */N` that creates the object (200 and its
  metadata, with its `crc32c`). An upload never finalized creates nothing.

Every request is recorded, and `fail` makes the next matching requests answer
an error whose message names the bucket and the object, as Cloud Storage's do,
so that a test can show the store never repeats it.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

import google_crc32c

BUCKET = "media-test-bucket"


@dataclass
class StoredObject:
    data: bytes
    generation: int
    updated: datetime


@dataclass
class UploadSession:
    name: str
    content_type: str | None
    received: bytearray = field(default_factory=bytearray)
    #: The size of each PUT's body, in order.
    puts: list[int] = field(default_factory=list)
    #: Whether a final PUT created the object.
    finished: bool = False
    #: The object metadata the initiating request carried.
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class Failure:
    method: str
    path_part: str
    status: int
    remaining: int


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body_bytes: int


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _crc32c(data: bytes) -> str:
    return base64.b64encode(google_crc32c.value(data).to_bytes(4, "big")).decode("ascii")


class Emulator:
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock
        self.objects: dict[str, StoredObject] = {}
        self.sessions: dict[str, UploadSession] = {}
        self.requests: list[Recorded] = []
        self.failures: list[Failure] = []
        self._generation = 1_000
        self._lock = threading.Lock()
        emulator = self

        class Handler(_Handler):
            owner = emulator

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # What a test may do to the bucket directly.

    def put(self, name: str, data: bytes, *, when: datetime | None = None) -> None:
        with self._lock:
            self._generation += 1
            self.objects[name] = StoredObject(data, self._generation, when or self.clock())

    def age(self, name: str, when: datetime) -> None:
        self.objects[name].updated = when

    def fail(self, method: str, path_part: str, *, status: int = 503, times: int = 1) -> None:
        self.failures.append(Failure(method, path_part, status, times))

    def reset_log(self) -> None:
        self.requests.clear()

    # Serving.

    def _failure_for(self, method: str, path: str) -> int | None:
        for failure in self.failures:
            if failure.remaining > 0 and failure.method == method and failure.path_part in path:
                failure.remaining -= 1
                return failure.status
        return None

    def _metadata(self, name: str, stored: StoredObject) -> dict[str, str]:
        return {
            "kind": "storage#object",
            "id": f"{BUCKET}/{name}/{stored.generation}",
            "name": name,
            "bucket": BUCKET,
            "generation": str(stored.generation),
            "metageneration": "1",
            "contentType": "application/octet-stream",
            "size": str(len(stored.data)),
            "crc32c": _crc32c(stored.data),
            "timeCreated": _rfc3339(stored.updated),
            "updated": _rfc3339(stored.updated),
        }

    def _new_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation


_OBJECT = re.compile(r"^/storage/v1/b/([^/]+)/o/(.+)$")
_LIST = re.compile(r"^/storage/v1/b/([^/]+)/o$")
_DOWNLOAD = re.compile(r"^/download/storage/v1/b/([^/]+)/o/(.+)$")
_UPLOAD = re.compile(r"^/upload/storage/v1/b/([^/]+)/o$")
_RANGE = re.compile(r"^bytes=(\d+)-(\d+)$")
_CONTENT_RANGE = re.compile(r"^bytes (?:(\d+)-(\d+)|\*)/(\d+|\*)$")


class _Handler(BaseHTTPRequestHandler):
    owner: Emulator
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})

    def _error(self, status: int, name: str) -> None:
        # Cloud Storage's own wording: it names the bucket and the object.
        self._json(status, {"error": {"code": status, "message": f"Failure on object: {BUCKET}/{name}"}})

    def _handle(self, method: str) -> None:
        body = self._body()
        split = urlsplit(self.path)
        path, query = split.path, parse_qs(split.query)
        owner = self.owner
        owner.requests.append(
            Recorded(method, path, query, {k.lower(): v for k, v in self.headers.items()}, len(body))
        )
        failed = owner._failure_for(method, unquote(path))
        if failed is not None:
            self._error(failed, unquote(path))
            return
        if method == "PUT" and "upload_id" in query:
            self._upload_put(query["upload_id"][0], body)
        elif method == "POST" and _UPLOAD.match(path):
            self._upload_start(json.loads(body or b"{}"))
        elif method == "GET" and (found := _DOWNLOAD.match(path)):
            self._download(unquote(found.group(2)), query)
        elif method == "GET" and _LIST.match(path):
            self._list(query)
        elif method in ("GET", "DELETE") and (found := _OBJECT.match(path)):
            name = unquote(found.group(2))
            stored = owner.objects.get(name)
            if stored is None:
                self._error(404, name)
            elif method == "GET":
                self._json(200, owner._metadata(name, stored))
            else:
                del owner.objects[name]
                self._send(204)
        else:
            self._error(400, unquote(path))

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _download(self, name: str, query: dict[str, list[str]]) -> None:
        stored = self.owner.objects.get(name)
        if stored is None:
            self._error(404, name)
            return
        wanted = query.get("ifGenerationMatch")
        if wanted and int(wanted[0]) != stored.generation:
            self._error(412, name)
            return
        headers = {
            "x-goog-hash": f"crc32c={_crc32c(stored.data)}",
            "x-goog-generation": str(stored.generation),
            "Content-Type": "application/octet-stream",
        }
        found = _RANGE.match(self.headers.get("Range") or "")
        if found is None:
            self._send(200, stored.data, headers)
            return
        start, end = int(found.group(1)), min(int(found.group(2)), len(stored.data) - 1)
        if start >= len(stored.data):
            self._send(416, b"", {})
            return
        headers["Content-Range"] = f"bytes {start}-{end}/{len(stored.data)}"
        self._send(206, stored.data[start : end + 1], headers)

    def _list(self, query: dict[str, list[str]]) -> None:
        prefix = query.get("prefix", [""])[0]
        offset = query.get("startOffset", [""])[0]
        token = query.get("pageToken", [""])[0]
        page = int(query.get("maxResults", ["1000"])[0])
        names = sorted(
            (name for name in self.owner.objects if name.startswith(prefix) and name >= offset),
            key=lambda n: n.encode("utf-8"),
        )
        if token:
            names = [name for name in names if name.encode("utf-8") > base64.b64decode(token)]
        items = names[:page]
        payload: dict[str, object] = {
            "kind": "storage#objects",
            "items": [self.owner._metadata(name, self.owner.objects[name]) for name in items],
        }
        if len(names) > page:
            payload["nextPageToken"] = base64.b64encode(items[-1].encode("utf-8")).decode("ascii")
        self._json(200, payload)

    def _upload_start(self, metadata: dict[str, object]) -> None:
        name = str(metadata.get("name"))
        session_id = secrets.token_hex(8)
        self.owner.sessions[session_id] = UploadSession(
            name, self.headers.get("x-upload-content-type"), metadata=dict(metadata)
        )
        location = f"{self.owner.endpoint}/upload/storage/v1/b/{BUCKET}/o?uploadType=resumable&upload_id={session_id}"
        self._send(200, b"", {"Location": location})

    def _upload_put(self, session_id: str, body: bytes) -> None:
        session = self.owner.sessions.get(session_id)
        found = _CONTENT_RANGE.match(self.headers.get("Content-Range") or "")
        if session is None or session.finished or found is None:
            self._error(404 if session is None else 400, "upload")
            return
        first, _last, total = found.groups()
        if first is not None and int(first) != len(session.received):
            self._error(400, session.name)
            return
        session.received += body
        session.puts.append(len(body))
        if total == "*" or int(total) != len(session.received):
            headers = {"Range": f"bytes=0-{len(session.received) - 1}"} if session.received else {}
            self._send(308, b"", headers)
            return
        stored = StoredObject(bytes(session.received), self.owner._new_generation(), self.owner.clock())
        self.owner.objects[session.name] = stored
        session.finished = True
        self._json(200, self.owner._metadata(session.name, stored))


def object_path(name: str) -> str:
    """The JSON API path of an object, as the client spells it."""
    return f"/storage/v1/b/{BUCKET}/o/{quote(name, safe='')}"

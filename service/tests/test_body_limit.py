"""The request-body ceiling and the hidden API docs (agent-forge-harness-ust7,
release review S1 and S2).

The body tests drive the real app over raw ASGI with an instrumented receive
channel, so they can count the bytes the app actually took: a refused body must
never be read past its ceiling, whatever the route would have done with it.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import os
import subprocess
import sys
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config
import service.app as app_module
from service.app import app, get_auth_store, get_message_store, get_service
from service.auth_store import InMemoryAuthStore
from service.body_limit import (
    ANONYMOUS_MAX_BODY_BYTES,
    DEFAULT_MAX_BODY_BYTES,
    TOO_LARGE_DETAIL,
    BodyLimitMiddleware,
    ceiling_for,
)
from service.history import InMemoryMessageStore
from service.models import MAX_EMAIL_LENGTH, MAX_INVITE_LENGTH, MAX_PASSWORD_LENGTH, ChatResponse
from service.workbench_contracts import ASSET_MAX_BYTES, CHAT_TEXT_MAX_CHARS

REPO_ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024
#: Divides the default ceiling, so the chunk that crosses it is exactly one past.
CHUNK = 64 * 1024
#: The security review's probe body.
ATTACK = 30 * MIB
REFUSAL = b'{"detail":"request body too large"}'
ATTACHMENT_PATH = "/conversations/c1/attachments"
MEDIA_PATH = "/campaigns/c1/assets/a1/bytes"
LEGACY_JSON_ROUTES = ("/auth/login", "/auth/signup", "/chat", "/metrics/ui")
ANONYMOUS_JSON_ROUTES = ("/auth/login", "/auth/signup", "/metrics/ui")
JSON = {"content-type": "application/json"}
#: The costliest character to send: twelve bytes of JSON once \u-escaped.
ASTRAL = "\U0001d400"
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

_FILLER = b"0" * CHUNK


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_message_store, None)
    app.dependency_overrides.pop(get_service, None)
    app.dependency_overrides.pop(get_auth_store, None)


class Channel:
    """An ASGI receive channel holding a body it hands over CHUNK bytes at a
    time. `pulled` counts every byte the app took from it."""

    def __init__(self, body: bytes | None = None, *, total: int = 0) -> None:
        self.body = body
        self.total = len(body) if body is not None else total
        self.pulled = 0
        self._ended = False
        self.done = asyncio.Event()

    async def receive(self) -> Message:
        if not self._ended:
            size = min(CHUNK, self.total - self.pulled)
            if self.body is not None:
                piece = self.body[self.pulled:self.pulled + size]
            else:
                piece = _FILLER if size == CHUNK else _FILLER[:size]
            self.pulled += size
            self._ended = self.pulled >= self.total
            return {"type": "http.request", "body": piece, "more_body": not self._ended}
        await self.done.wait()
        return {"type": "http.disconnect"}


@dataclass
class Answer:
    status: int
    headers: dict[bytes, bytes]
    body: bytes


def drive(target: ASGIApp, method: str, path: str, channel: Channel, *, declared: bool = True,
          content_type: bytes = b"application/json") -> Answer:
    """One request over raw ASGI: with a Content-Length, or chunked with none."""
    headers = [(b"host", b"testserver"), (b"content-type", content_type)]
    if declared:
        headers.append((b"content-length", str(channel.total).encode()))
    else:
        headers.append((b"transfer-encoding", b"chunked"))
    scope: Scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": headers, "client": ("203.0.113.9", 50000), "server": ("testserver", 80),
    }
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            channel.done.set()

    async def run() -> None:
        await target(scope, channel.receive, send)

    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Answer(start["status"], {k.lower(): v for k, v in start["headers"]}, body)


class Probe:
    """An inner app that reads every byte it is given, then answers 200."""

    def __init__(self) -> None:
        self.seen = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            self.seen += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await PlainTextResponse("read")(scope, receive, send)


# ── S1: the ceiling ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", LEGACY_JSON_ROUTES)
def test_a_declared_oversized_body_is_refused_before_one_byte_is_read(path: str) -> None:
    channel = Channel(total=ATTACK)
    answer = drive(app, "POST", path, channel)
    assert answer.status == 413
    assert answer.body == REFUSAL, "a small fixed answer that echoes nothing"
    assert json.loads(REFUSAL) == {"detail": TOO_LARGE_DETAIL}
    assert channel.pulled == 0
    assert b"content-security-policy" in answer.headers, "the headers middleware still wraps the refusal"


@pytest.mark.parametrize("path", LEGACY_JSON_ROUTES)
def test_a_chunked_body_with_no_length_is_cut_at_the_ceiling(path: str) -> None:
    channel = Channel(total=ATTACK)
    answer = drive(app, "POST", path, channel, declared=False)
    assert answer.status == 413
    assert answer.body == REFUSAL
    # The chunk that crosses the ceiling is the last one taken from the channel.
    assert channel.pulled <= DEFAULT_MAX_BODY_BYTES + CHUNK


@pytest.mark.parametrize("declared", [True, False])
def test_the_app_never_receives_a_byte_past_the_ceiling(declared: bool) -> None:
    probe = Probe()
    answer = drive(BodyLimitMiddleware(probe), "POST", "/chat", Channel(total=ATTACK), declared=declared)
    assert answer.status == 413 and answer.body == REFUSAL
    assert probe.seen <= DEFAULT_MAX_BODY_BYTES


@pytest.mark.parametrize("declared", [True, False])
def test_a_body_at_the_ceiling_is_read_whole(declared: bool) -> None:
    probe = Probe()
    answer = drive(BodyLimitMiddleware(probe), "POST", "/chat", Channel(total=DEFAULT_MAX_BODY_BYTES),
                   declared=declared)
    assert (answer.status, answer.body) == (200, b"read")
    assert probe.seen == DEFAULT_MAX_BODY_BYTES


def _attachment_body(size: int) -> bytes:
    """An attachment upload's JSON, padded to exactly `size` bytes."""
    head, tail = b'{"filename":"max.txt","content_type":"text/plain","data":"', b'"}'
    return head + b"A" * (size - len(head) - len(tail)) + tail


def test_an_attachment_at_its_documented_maximum_is_accepted() -> None:
    app.dependency_overrides[get_message_store] = lambda: InMemoryMessageStore()
    data = base64.b64encode(b"x" * config.ATTACHMENT_MAX_BYTES).decode()
    r = TestClient(app).post(ATTACHMENT_PATH, json={"filename": "max.txt", "content_type": "text/plain", "data": data})
    assert r.status_code == 200, r.text[:200]
    assert len(r.request.content) > DEFAULT_MAX_BODY_BYTES, "it needed the route's own, higher ceiling"


def test_the_attachment_route_takes_its_ceiling_and_refuses_one_byte_over() -> None:
    app.dependency_overrides[get_message_store] = lambda: InMemoryMessageStore()
    ceiling = ceiling_for("POST", ATTACHMENT_PATH, media_enabled=False)
    at = Channel(_attachment_body(ceiling))
    answer = drive(app, "POST", ATTACHMENT_PATH, at)
    assert at.pulled == ceiling
    assert answer.body != REFUSAL, "the route gives its own answer"
    for declared in (True, False):
        over = Channel(_attachment_body(ceiling + 1))
        answer = drive(app, "POST", ATTACHMENT_PATH, over, declared=declared)
        assert (answer.status, answer.body) == (413, REFUSAL), declared
        assert over.pulled == (0 if declared else ceiling + 1)


def test_the_media_upload_route_takes_its_documented_maximum_and_refuses_one_byte_over() -> None:
    largest = max(ASSET_MAX_BYTES.values())
    ceiling = ceiling_for("PUT", MEDIA_PATH, media_enabled=True)
    assert ceiling >= largest
    assert ceiling_for("PUT", MEDIA_PATH, media_enabled=False) == DEFAULT_MAX_BODY_BYTES, "dark: an unknown path"
    probe = Probe()
    guarded = BodyLimitMiddleware(probe, media_enabled=lambda: True)
    answer = drive(guarded, "PUT", MEDIA_PATH, Channel(total=largest), content_type=b"audio/mpeg")
    assert (answer.status, probe.seen) == (200, largest)
    for declared in (True, False):
        answer = drive(guarded, "PUT", MEDIA_PATH, Channel(total=ceiling + 1), declared=declared,
                       content_type=b"audio/mpeg")
        assert (answer.status, answer.body) == (413, REFUSAL), declared


def test_only_the_upload_routes_get_a_higher_ceiling() -> None:
    assert ceiling_for("POST", "/campaigns/c1/documents", media_enabled=False) > DEFAULT_MAX_BODY_BYTES
    assert ceiling_for("PATCH", "/campaigns/c1/documents/d1", media_enabled=False) > DEFAULT_MAX_BODY_BYTES
    for method, path in [("GET", ATTACHMENT_PATH), ("POST", "/campaigns/c1/documents/d1/restore"),
                         ("POST", "/chat"), ("POST", ATTACHMENT_PATH + "/x"),
                         ("PUT", MEDIA_PATH + "/x")]:
        assert ceiling_for(method, path, media_enabled=True) == DEFAULT_MAX_BODY_BYTES, (method, path)


class _Answering:
    def answer(self, prompt, mode="sage", conversation_id=None, attachment_context=None, attachment_label=None):
        return ChatResponse(answer="ok", sources=[], answerable=True, mode=mode, conversation_id=conversation_id)


@pytest.mark.parametrize("prompt", ["What does a beholder see?", "竜" * CHAT_TEXT_MAX_CHARS], ids=["short", "longest"])
def test_a_normal_chat_turn_still_answers(prompt: str) -> None:
    """The longest prompt the route allows, in three-byte characters, is about
    300 KB of JSON: well under the default ceiling."""
    app.dependency_overrides[get_service] = lambda: _Answering()
    app.dependency_overrides[get_message_store] = lambda: InMemoryMessageStore()
    body = json.dumps({"prompt": prompt, "conversation_id": "c1"}, ensure_ascii=False).encode()
    r = TestClient(app).post("/chat", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["answer"] == "ok"


@pytest.mark.real_auth
def test_a_normal_signup_and_login_still_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    store = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: store
    from datetime import UTC, datetime, timedelta
    token = store.create_invite(role="player", expires_at=datetime.now(UTC) + timedelta(days=1)).token
    client = TestClient(app)
    signup = client.post("/auth/signup", json={"email": "ada@example.com", "password": "password123", "invite": token})
    assert signup.status_code == 200, signup.text
    login = TestClient(app).post("/auth/login", json={"email": "ada@example.com", "password": "password123"})
    assert login.status_code == 200 and login.json()["role"] == "player"


# ── Review rework: memory held, not only bytes read (H1, H2, M1) ────────────


def _escaped(value: object) -> bytes:
    """JSON with every non-ASCII character \\u-escaped: the longest a client writes it."""
    return json.dumps(value, ensure_ascii=True).encode()


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("path", ANONYMOUS_JSON_ROUTES)
def test_an_anonymous_json_route_takes_a_small_ceiling(path: str, declared: bool) -> None:
    ceiling = ceiling_for("POST", path, media_enabled=True)
    assert ceiling == ANONYMOUS_MAX_BODY_BYTES == 64 * 1024 < DEFAULT_MAX_BODY_BYTES
    over = Channel(total=ceiling + 1)
    answer = drive(app, "POST", path, over, declared=declared)
    assert (answer.status, answer.body) == (413, REFUSAL)
    assert over.pulled == (0 if declared else ceiling + 1)


@pytest.mark.real_auth
def test_the_largest_valid_sign_in_and_sign_up_bodies_are_under_the_small_ceiling(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    app.dependency_overrides[get_auth_store] = lambda: InMemoryAuthStore()
    email = ASTRAL * (MAX_EMAIL_LENGTH - len("@x.io")) + "@x.io"
    fields = {"email": email, "password": ASTRAL * MAX_PASSWORD_LENGTH}
    login = _escaped(fields)
    signup = _escaped({**fields, "invite": ASTRAL * MAX_INVITE_LENGTH})
    assert len(login) < len(signup) < ANONYMOUS_MAX_BODY_BYTES
    client = TestClient(app)
    # Each route gives its own answer: no such account, no such invite.
    assert client.post("/auth/login", content=login, headers=JSON).status_code == 401
    assert client.post("/auth/signup", content=signup, headers=JSON).status_code == 400


def test_the_largest_valid_ui_metrics_batch_is_under_the_small_ceiling() -> None:
    labels = {"environment": "production", "release": ASTRAL * 64, "mode": "sage",
              "route_template": "/metrics/ui", "browser_family": "chromium"}
    point = {"name": "ui.interaction.chat_round_trip_ms", "kind": "numeric", "unit": "ms",
             "value": 1.7976931348623157e308, "labels": labels}
    body = _escaped({"points": [point] * 50})
    assert len(body) < ANONYMOUS_MAX_BODY_BYTES
    r = TestClient(app).post("/metrics/ui", content=body, headers=JSON)
    assert r.status_code == 202, r.text[:300]


def _empty_objects(size: int, *, closed: bool = True) -> bytes:
    """`[{},{},...]` padded to exactly `size` bytes: about 27 bytes of Python
    objects per byte once parsed. Unclosed, it is not JSON at all."""
    body = (b"[" + b"{}," * ((size - 2) // 3))[:-1] + (b"]" if closed else b"")
    return body + b" " * (size - len(body))


@pytest.mark.parametrize("closed", [True, False], ids=["not-the-schema", "not-json"])
def test_a_refused_body_is_let_go_once_answered(closed: bool) -> None:
    """Review H1. The 422's error held its traceback, the traceback held the
    frame that raised the error, and the frame held the parsed body: a cycle
    only a full garbage collection frees. With the collector off, reference
    counting alone must free every refused body."""
    app.dependency_overrides[get_service] = lambda: _Answering()
    app.dependency_overrides[get_message_store] = lambda: InMemoryMessageStore()
    body = _empty_objects(DEFAULT_MAX_BODY_BYTES, closed=closed)

    async def refuse(times: int) -> list[int]:
        """Raw ASGI on one event loop, keeping only each status: TestClient, or
        a loop per request, would hold bodies of its own with the collector off."""
        statuses: list[int] = []
        for _ in range(times):
            channel = Channel(body)

            async def send(message: Message, channel: Channel = channel) -> None:
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])
                elif not message.get("more_body", False):
                    channel.done.set()

            scope: Scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/chat", "raw_path": b"/chat", "query_string": b"", "root_path": "",
                "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
                "client": ("203.0.113.9", 50000), "server": ("testserver", 80),
            }
            await app(scope, channel.receive, send)
        return statuses

    assert asyncio.run(refuse(1)) == [422]
    gc.collect()
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    gc.disable()
    try:
        before, _ = tracemalloc.get_traced_memory()
        assert asyncio.run(refuse(5)) == [422] * 5
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        gc.enable()
        if started:
            tracemalloc.stop()
    assert held < DEFAULT_MAX_BODY_BYTES, f"{held / MIB:.1f} MiB still held after five refused bodies"


@pytest.mark.real_auth
@pytest.mark.parametrize("declared", [True, False])
def test_an_anonymous_attachment_is_refused_before_its_body_is_read(declared: bool) -> None:
    """Review H2: a declared body model is parsed before any dependency runs, so
    the session check came after up to 94 MB of parsed objects."""
    app.dependency_overrides[get_auth_store] = lambda: InMemoryAuthStore()
    app.dependency_overrides[get_message_store] = lambda: InMemoryMessageStore()
    ceiling = ceiling_for("POST", ATTACHMENT_PATH, media_enabled=False)
    channel = Channel(_attachment_body(ceiling))
    answer = drive(app, "POST", ATTACHMENT_PATH, channel, declared=declared)
    assert answer.status == 401
    assert channel.pulled == 0


MARKER = "private-marker-3f9c"


@pytest.mark.parametrize(("content_type", "body"), [
    ("text/plain", json.dumps({"filename": "a.txt", "content_type": "text/plain", "data": "aGk="})),
    ("application/json", json.dumps({"filename": [MARKER], "content_type": "text/plain", "data": "aGk="})),
    ("application/json", '{"filename": "' + MARKER),
], ids=["not-json-content-type", "not-the-schema", "not-json"])
def test_a_signed_in_attachment_body_is_still_refused_as_before_and_not_echoed(content_type: str, body: str) -> None:
    app.dependency_overrides[get_message_store] = lambda: InMemoryMessageStore()
    r = TestClient(app).post(ATTACHMENT_PATH, content=body, headers={"content-type": content_type})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"][0] == "body"
    assert MARKER not in r.text


@pytest.mark.parametrize("media_on", [False, True])
def test_the_installed_app_asks_the_media_switch_for_the_upload_ceiling(
        monkeypatch: pytest.MonkeyPatch, media_on: bool) -> None:
    """Review M1: the app's own wiring, in both directions. Dark, the media path
    is any unknown path; lit, it takes the upload ceiling (the router, still
    dark here, then answers it without reading)."""
    monkeypatch.setattr(app_module, "_media_enabled", lambda: media_on)
    channel = Channel(total=DEFAULT_MAX_BODY_BYTES + 1)
    answer = drive(app, "PUT", MEDIA_PATH, channel, content_type=b"audio/mpeg")
    if media_on:
        assert answer.status != 413 and answer.body != REFUSAL
    else:
        assert (answer.status, answer.body, channel.pulled) == (413, REFUSAL, 0)


# ── S2: the API docs ────────────────────────────────────────────────────────


def _docs_statuses(**env: str) -> list[int]:
    """The three docs paths' statuses from a fresh import of the app, which is
    the only place the setting is read. A fresh interpreter, because the suite
    itself runs with the docs on (the root conftest.py) for the route census."""
    clean = {k: v for k, v in os.environ.items() if k not in ("RAG_API_DOCS_ENABLED", "K_SERVICE")}
    code = ("from fastapi.testclient import TestClient\n"
            "from service.app import app\n"
            "client = TestClient(app)\n"
            f"print(*[client.get(path).status_code for path in {DOCS_PATHS!r}])\n")
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env={**clean, **env},
                         capture_output=True, text=True, timeout=180, check=True)
    return [int(status) for status in run.stdout.strip().splitlines()[-1].split()]


def test_the_api_docs_are_not_served_by_default() -> None:
    assert _docs_statuses() == [404, 404, 404]


def test_the_api_docs_are_served_when_the_local_setting_is_on() -> None:
    assert _docs_statuses(RAG_API_DOCS_ENABLED="1") == [200, 200, 200]


def test_the_api_docs_stay_off_on_cloud_run_even_with_the_setting_on() -> None:
    assert _docs_statuses(RAG_API_DOCS_ENABLED="1", K_SERVICE="game-guide-ai") == [404, 404, 404]

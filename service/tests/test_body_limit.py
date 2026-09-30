"""The request-body ceiling and the hidden API docs (agent-forge-harness-ust7,
release review S1 and S2).

The body tests drive the real app over raw ASGI with an instrumented receive
channel, so they can count the bytes the app actually took: a refused body must
never be read past its ceiling, whatever the route would have done with it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config
from service.app import app, get_auth_store, get_message_store, get_service
from service.auth_store import InMemoryAuthStore
from service.body_limit import DEFAULT_MAX_BODY_BYTES, TOO_LARGE_DETAIL, BodyLimitMiddleware, ceiling_for
from service.history import InMemoryMessageStore
from service.models import ChatResponse
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
                         ("POST", "/chat"), ("POST", "/auth/login"), ("POST", ATTACHMENT_PATH + "/x"),
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

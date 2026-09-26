"""The Workbench route scaffolding (agent-forge-harness-oe6).

This first part pins the LEGACY answers byte for byte, and it was written and
run against the untouched base before `service/workbench_api.py` existed: the
two application-wide handlers that bead installs must leave every one of these
exactly as it is (status, body and the whole header set). It does NOT prove the
handlers are installed — the handler-identity tests further down do that.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

import config
from service.app import app, get_auth_store, get_service
from service.auth_store import InMemoryAuthStore
from service.security_headers import CONTENT_SECURITY_POLICY
from service.session import SessionData, encode_session

_GOLDEN_SECRET = "golden-bytes-secret-long-enough-for-the-floor"


def _json_headers(body: bytes, *extra: tuple[str, str]) -> list[tuple[str, str]]:
    return sorted([
        ("content-length", str(len(body))),
        ("content-security-policy", CONTENT_SECURITY_POLICY),
        ("content-type", "application/json"),
        *extra,
    ])


def _answer(response: httpx.Response) -> tuple[int, bytes, list[tuple[str, str]]]:
    return response.status_code, response.content, sorted(response.headers.items())


def test_no_spa_route_is_registered_on_the_real_app() -> None:
    """Precondition for the golden bytes below: with a built `ui/dist` the app
    also serves the SPA shell, and an unknown path would no longer be FastAPI's
    JSON 404. This FAILS, never skips — a skip would read as a pass."""
    spa = sorted(
        name for name in (ctx.name for ctx in iter_route_contexts(app.routes))
        if name is not None and (name == "ui" or name.startswith("spa:"))
    )
    assert spa == [], (
        f"the real app serves the SPA ({spa}): a ui/dist directory exists in this "
        "checkout. The legacy golden bytes assume it does not; move ui/dist aside."
    )


@pytest.fixture
def legacy_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryAuthStore]:
    monkeypatch.setattr(config, "SESSION_SECRET", _GOLDEN_SECRET)
    store = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: store
    # Any object: every case below is refused before the service is used.
    app.dependency_overrides[get_service] = lambda: object()
    yield store
    app.dependency_overrides.pop(get_auth_store, None)
    app.dependency_overrides.pop(get_service, None)


def test_legacy_validation_answers_are_byte_identical(legacy_store: InMemoryAuthStore) -> None:
    """FastAPI's default 422 list — `input` echo included, a recorded residual —
    and the timeline route's own hand-validated 422 (1kg.4.2), unchanged."""
    client = TestClient(app)
    missing_prompt = b'{"detail":[{"type":"missing","loc":["body","prompt"],"msg":"Field required","input":{}}]}'
    bad_mode = (
        b'{"detail":[{"type":"enum","loc":["body","mode"],"msg":"Input should be \'sage\', \'spell\', '
        b'\'rules\' or \'gm\'","input":"nope","ctx":{"expected":"\'sage\', \'spell\', \'rules\' or \'gm\'"}}]}'
    )
    malformed = (
        b'{"detail":[{"type":"json_invalid","loc":["body",11],"msg":"JSON decode error","input":{},'
        b'"ctx":{"error":"Expecting value"}}]}'
    )
    login_missing = (
        b'{"detail":[{"type":"missing","loc":["body","password"],"msg":"Field required",'
        b'"input":{"email":"a@example.com"}}]}'
    )
    metrics_bad = (
        b'{"detail":[{"type":"union_tag_not_found","loc":["body","points",0],"msg":"Unable to extract '
        b'tag using discriminator \'kind\'","input":{"name":"x"},"ctx":{"discriminator":"\'kind\'"}}]}'
    )
    timeline_limit = (
        b'{"detail":{"code":"validation_failed","message":"That request isn\'t valid.",'
        b'"retryable":false,"field":"limit"}}'
    )
    cases = [
        ("chat: missing prompt", client.post("/chat", json={}), missing_prompt),
        ("chat: invalid mode", client.post("/chat", json={"prompt": "hi", "mode": "nope"}), bad_mode),
        ("chat: malformed JSON",
         client.post("/chat", content=b'{"prompt": ', headers={"content-type": "application/json"}), malformed),
        ("login: missing field", client.post("/auth/login", json={"email": "a@example.com"}), login_missing),
        ("metrics: bad point", client.post("/metrics/ui", json={"points": [{"name": "x"}]}), metrics_bad),
        ("timeline: limit=0", client.get("/conversations/x/timeline?limit=0"), timeline_limit),
    ]
    assert [label for label, _, _ in cases] == [
        "chat: missing prompt", "chat: invalid mode", "chat: malformed JSON",
        "login: missing field", "metrics: bad point", "timeline: limit=0",
    ]
    for label, response, body in cases:
        assert _answer(response) == (422, body, _json_headers(body)), label


@pytest.mark.real_auth
def test_legacy_refusals_are_byte_identical(legacy_store: InMemoryAuthStore) -> None:
    """`require_session`'s three 401 bodies (F-23's oracle included — R-4 leaves
    legacy routes exactly as they are), an unknown path and a wrong method."""
    client = TestClient(app)
    deleted = encode_session(SessionData(user_id=42, role="dm"), _GOLDEN_SECRET)
    cookie = config.SESSION_COOKIE_NAME
    required = b'{"detail":"authentication required"}'
    invalid = b'{"detail":"invalid or expired session"}'
    gone = b'{"detail":"account no longer exists"}'
    not_found = b'{"detail":"Not Found"}'
    not_allowed = b'{"detail":"Method Not Allowed"}'
    cases = [
        ("messages: no cookie", client.get("/conversations/x/messages"), 401, required, ()),
        ("messages: garbage cookie",
         client.get("/conversations/x/messages", headers={"cookie": f"{cookie}=garbage"}), 401, invalid, ()),
        ("messages: deleted account",
         client.get("/conversations/x/messages", headers={"cookie": f"{cookie}={deleted}"}), 401, gone, ()),
        ("timeline: no cookie", client.get("/conversations/x/timeline"), 401, required, ()),
        ("unknown path", client.get("/no/such/path"), 404, not_found, ()),
        ("wrong method", client.delete("/healthz"), 405, not_allowed, (("allow", "GET"),)),
    ]
    assert [label for label, *_ in cases] == [
        "messages: no cookie", "messages: garbage cookie", "messages: deleted account",
        "timeline: no cookie", "unknown path", "wrong method",
    ]
    for label, response, status, body, extra in cases:
        assert _answer(response) == (status, body, _json_headers(body, *extra)), label

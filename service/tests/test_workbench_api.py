"""The Workbench route scaffolding (agent-forge-harness-oe6).

The first part pins the LEGACY answers byte for byte, and it was written and run
against the untouched base before `service/workbench_api.py` existed: the two
application-wide handlers that bead installs must leave every one of these
exactly as it is (status, body and the whole header set). It does NOT prove the
handlers are installed — the handler-identity tests do that.

The rest proves the Workbench half on PROBE apps. There is no Workbench route on
the real app yet, so every rule is exercised on routers built by the same
factory the route beads will use (`workbench_router(gm_session(require_session))`
on an app prepared by `install_workbench`), over a two-tenant fake store. Those
probe apps authenticate through the REAL `require_session`: `conftest.py`
installs its default session on `service.app.app` only, so what makes a probe
app authenticate is its own `get_auth_store` override plus the monkeypatched
`config.SESSION_SECRET`, not `@pytest.mark.real_auth`. That marker is used only
where a test touches the real app.

Every refusal here is paired with a positive control in the same test, and
every collection a test iterates is asserted by value first: an assertion over
an empty set is a test that cannot fail.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import starlette.exceptions
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

import config
from service import timeline, workbench_api
from service.app import app, get_auth_store, get_service, require_session
from service.auth_store import InMemoryAuthStore
from service.invites import Role
from service.security_headers import CONTENT_SECURITY_POLICY
from service.session import SessionData, encode_session
from service.spa_fallback import SPA_MOUNT_NAME, SPA_ROUTE_PREFIX, install_spa
from service.tests.test_auth_guard import _session_guarded_routes
from service.workbench_api import (
    FORBIDDEN_ORIGIN_DETAIL,
    FORBIDDEN_ROLE_DETAIL,
    NOT_FOUND_DETAIL,
    UNAUTHENTICATED_BODY,
    WorkbenchRoute,
    api_routes,
    gm_session,
    install_workbench,
    not_found,
    workbench_router,
)
from service.workbench_contracts import ErrorBody, ErrorCode, ErrorInfo
from tests.test_spa_routes_parity import _live_api_prefixes

REPO_ROOT = Path(__file__).resolve().parents[2]

_GOLDEN_SECRET = "golden-bytes-secret-long-enough-for-the-floor"


def _is_spa(route: APIRoute) -> bool:
    """The SPA fallback's routes, by NAME (lead ruling Q-13) — never by
    `isinstance`, since one of them and an API route look identical to it."""
    return route.name == SPA_MOUNT_NAME or route.name.startswith(SPA_ROUTE_PREFIX)


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
    spa = sorted(route.name for _, route in api_routes(app) if _is_spa(route))
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


# ── The probe world: two tenants, one player, a router built by the factory ─────

_PROBE_SECRET = "probe-secret-long-enough-for-the-32-char-floor"
_CANARY = "canary-7f3a-gm-private-text"
_OWNER_A, _OWNER_B, _PLAYER, _DELETED = 1, 2, 3, 99
_NOT_SIGNED_IN = b'{"detail":"not signed in"}'
_CONFLICT_DETAIL = ErrorBody(
    detail=ErrorInfo(code=ErrorCode.CONFLICT, message="That changed while you were editing.", retryable=False),
).model_dump(mode="json", exclude_none=True)["detail"]


@dataclass
class _Doc:
    owner_id: int
    title: str
    revision: int = 2
    deleted: bool = False


def _seed_docs() -> dict[str, _Doc]:
    return {
        "doc-a": _Doc(owner_id=_OWNER_A, title="A's notes"),
        "doc-b": _Doc(owner_id=_OWNER_B, title="B's notes"),
        "doc-a-deleted": _Doc(owner_id=_OWNER_A, title="gone", deleted=True),
        "doc-b-deleted": _Doc(owner_id=_OWNER_B, title="gone", deleted=True),
    }


@dataclass
class _Docs:
    """Ownership is part of the lookup, as a real statement's WHERE clause is:
    a row that is missing, deleted or someone else's is simply not found."""

    rows: dict[str, _Doc] = field(default_factory=_seed_docs)

    def owned(self, doc_id: str, user_id: int) -> _Doc | None:
        row = self.rows.get(doc_id)
        if row is None or row.deleted or row.owner_id != user_id:
            return None
        return row


class _Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    base_revision: int


class _Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


def _probe_app(auth: InMemoryAuthStore, docs: _Docs) -> FastAPI:
    probe = FastAPI()
    install_workbench(probe)
    gm = gm_session(require_session)
    router = workbench_router(gm)

    @router.get("/docs/{doc_id}")
    def read_doc(doc_id: str, session: SessionData = Depends(gm)) -> dict[str, str]:
        doc = docs.owned(doc_id, session.user_id)
        if doc is None:
            not_found()
        return {"id": doc_id, "title": doc.title}

    @router.patch("/docs/{doc_id}")
    def patch_doc(doc_id: str, body: _Patch, session: SessionData = Depends(gm)) -> dict[str, object]:
        doc = docs.owned(doc_id, session.user_id)
        if doc is None:
            not_found()
        if body.base_revision != doc.revision:
            raise HTTPException(status_code=409, detail=dict(_CONFLICT_DETAIL))
        doc.title, doc.revision = body.title, doc.revision + 1
        return {"id": doc_id, "revision": doc.revision}

    @router.post("/docs")
    def create_doc(body: _Create, session: SessionData = Depends(gm)) -> dict[str, str]:
        return {"title": body.title}

    @router.delete("/docs/{doc_id}")
    def delete_doc(doc_id: str, session: SessionData = Depends(gm)) -> dict[str, str]:
        doc = docs.owned(doc_id, session.user_id)
        if doc is None:
            not_found()
        doc.deleted = True
        return {"id": doc_id}

    @router.get("/ping")
    def ping() -> dict[str, str]:
        """Declares no session of its own: the router's gate still applies."""
        return {"pong": "yes"}

    probe.include_router(router, prefix="/probe")

    @probe.post("/legacy/docs")
    def legacy_create(body: _Create) -> dict[str, str]:
        return {"title": body.title}

    @probe.get("/legacy/guarded")
    def legacy_guarded(session: SessionData = Depends(require_session)) -> dict[str, int]:
        return {"user_id": session.user_id}

    probe.dependency_overrides[get_auth_store] = lambda: auth
    return probe


def _cookie(user_id: int, *, secret: str = _PROBE_SECRET) -> dict[str, str]:
    token = encode_session(SessionData(user_id=user_id, role="dm"), secret)
    return {"cookie": f"{config.SESSION_COOKIE_NAME}={token}"}


@dataclass
class _World:
    client: TestClient
    docs: _Docs
    auth: InMemoryAuthStore
    probe: FastAPI


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    monkeypatch.setattr(config, "SESSION_SECRET", _PROBE_SECRET)
    auth = InMemoryAuthStore()
    accounts_to_make: list[tuple[str, str, Role]] = [
        ("inv-a", "a@example.com", "dm"), ("inv-b", "b@example.com", "dm"), ("inv-p", "p@example.com", "player")]
    for token, email, role in accounts_to_make:
        auth.seed_invite(token, role=role)
        auth.redeem_invite(token, email, "not-a-real-hash")
    accounts = [auth.get_user_by_id(uid) for uid in (_OWNER_A, _OWNER_B, _PLAYER, _DELETED)]
    assert [(u.id, u.role) if u else None for u in accounts] == [(1, "dm"), (2, "dm"), (3, "player"), None]
    docs = _Docs()
    probe = _probe_app(auth, docs)
    return _World(client=TestClient(probe), docs=docs, auth=auth, probe=probe)


def _stable(response: httpx.Response) -> tuple[int, bytes, tuple[tuple[str, str], ...]]:
    """Status, body and header set, minus the per-response `date` (and
    `content-length`, which is equal whenever the body is)."""
    headers = tuple(sorted((k, v) for k, v in response.headers.items() if k not in {"date", "content-length"}))
    return response.status_code, response.content, headers


def _json_bytes(payload: object) -> bytes:
    """What `JSONResponse` renders for `payload`."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")).encode()


_JSON_ONLY = (("content-type", "application/json"),)

#: (method, path, JSON body) for every probe Workbench route, asserted by value
#: wherever a test iterates it.
_PROBE_ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/probe/docs/doc-a", None),
    ("PATCH", "/probe/docs/doc-a", {"title": "t", "base_revision": 2}),
    ("POST", "/probe/docs", {"title": "t"}),
    ("DELETE", "/probe/docs/doc-a", None),
    ("GET", "/probe/ping", None),
]


def test_the_probe_routes_are_what_the_matrix_iterates(world: _World) -> None:
    assert [(m, p) for m, p, _ in _PROBE_ROUTES] == [
        ("GET", "/probe/docs/doc-a"), ("PATCH", "/probe/docs/doc-a"), ("POST", "/probe/docs"),
        ("DELETE", "/probe/docs/doc-a"), ("GET", "/probe/ping"),
    ]
    served = sorted((m, path) for path, route in api_routes(world.probe) if isinstance(route, WorkbenchRoute)
                    for m in route.methods or ())
    assert served == [("DELETE", "/probe/docs/{doc_id}"), ("GET", "/probe/docs/{doc_id}"), ("GET", "/probe/ping"),
                      ("PATCH", "/probe/docs/{doc_id}"), ("POST", "/probe/docs")]


# ── A1: one 401 body, and the handler is live on the real app ────────────────


def test_every_authentication_failure_on_a_workbench_route_is_one_answer(
    world: _World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-2: no cookie, an undecodable one, one signed with another key, an
    expired one and a deleted account's are the same status, body and headers.
    `require_session` still says three different things; the handler does not
    let a Workbench route repeat them (threat model F-23)."""
    failures = {
        "no cookie": {},
        "garbage cookie": {"cookie": f"{config.SESSION_COOKIE_NAME}=garbage"},
        "another key": _cookie(_OWNER_A, secret="some-other-secret-that-is-long-enough-too"),
        "deleted account": _cookie(_DELETED),
    }
    answers = {
        (method, path, state): _stable(world.client.request(method, path, json=body, headers=headers))
        for method, path, body in _PROBE_ROUTES
        for state, headers in failures.items()
    }
    # Positive control, same routes: a valid cookie is not refused.
    allowed = [world.client.request(m, p, json=b, headers=_cookie(_OWNER_A)).status_code for m, p, b in _PROBE_ROUTES]
    assert allowed == [200, 200, 200, 200, 200]

    monkeypatch.setattr(config, "SESSION_TTL_DAYS", -1)  # every cookie is now past its age
    for method, path, body in _PROBE_ROUTES:
        answers[(method, path, "expired")] = _stable(
            world.client.request(method, path, json=body, headers=_cookie(_OWNER_B)))

    assert len(answers) == 25
    assert set(answers.values()) == {(401, _NOT_SIGNED_IN, _JSON_ONLY)}

    # The same failures on a legacy route on the same app keep all three bodies.
    monkeypatch.setattr(config, "SESSION_TTL_DAYS", 14)
    legacy = [world.client.get("/legacy/guarded", headers=h).content for h in failures.values()]
    assert legacy == [b'{"detail":"authentication required"}', b'{"detail":"invalid or expired session"}',
                      b'{"detail":"invalid or expired session"}', b'{"detail":"account no longer exists"}']


def test_an_auth_outage_on_a_workbench_route_is_not_an_authentication_failure(
    world: _World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 from the auth store or from the signing secret passes through."""
    def unavailable() -> InMemoryAuthStore:
        raise HTTPException(status_code=503, detail="auth backend unavailable")

    world.probe.dependency_overrides[get_auth_store] = unavailable
    down = world.client.get("/probe/docs/doc-a", headers=_cookie(_OWNER_A))
    assert (down.status_code, down.content) == (503, b'{"detail":"auth backend unavailable"}')

    world.probe.dependency_overrides[get_auth_store] = lambda: world.auth
    monkeypatch.setattr(config, "SESSION_SECRET", "")
    unset = world.client.get("/probe/docs/doc-a", headers=_cookie(_OWNER_A))
    assert (unset.status_code, unset.content) == (503, b'{"detail":"auth not configured"}')


@pytest.mark.real_auth
def test_the_real_app_answers_http_exceptions_through_the_workbench_handler() -> None:
    """Nothing else proves the `install_workbench(app)` line in service/app.py
    ran: probe apps install it themselves and the golden bytes are identical
    by design whether or not it did."""
    handlers = app.exception_handlers
    assert handlers[starlette.exceptions.HTTPException] is workbench_api.handle_http_exception


def test_exactly_one_module_registers_exception_handlers() -> None:
    registering = sorted(p.name for p in (REPO_ROOT / "service").glob("*.py")
                         if "exception_handler" in p.read_text(encoding="utf-8"))
    assert registering == ["workbench_api.py"]


def test_the_constants_are_the_contract_envelope_and_read_only() -> None:
    assert dict(UNAUTHENTICATED_BODY) == {"detail": "not signed in"}
    assert dict(NOT_FOUND_DETAIL) == {"code": "not_found", "message": "That isn't available.", "retryable": False}
    assert dict(FORBIDDEN_ROLE_DETAIL) == {
        "code": "forbidden", "message": "This is a Game Master feature.", "retryable": False}
    assert dict(FORBIDDEN_ORIGIN_DETAIL) == {
        "code": "forbidden", "message": "That request didn't come from this application.", "retryable": False}
    for detail in (NOT_FOUND_DETAIL, FORBIDDEN_ROLE_DETAIL, FORBIDDEN_ORIGIN_DETAIL):
        assert ErrorBody.model_validate({"detail": dict(detail)}).detail.code.value == detail["code"]
    with pytest.raises(TypeError):
        NOT_FOUND_DETAIL["code"] = "changed"  # type: ignore[index]


def test_the_role_refusal_says_what_the_timeline_route_says() -> None:
    assert FORBIDDEN_ROLE_DETAIL["message"] == timeline.FORBIDDEN_MESSAGE


# ── A3: one 404 path, non-enumerating; 403 only for a role ───────────────────


def test_missing_foreign_and_deleted_are_one_identical_404(world: _World) -> None:
    """T-2: two owners × every kind of id. Own is 200 (the positive control);
    every other kind is the same 404, from `not_found()`."""
    kinds = {
        _OWNER_A: {"own": "doc-a", "foreign": "doc-b", "missing": "doc-none",
                   "deleted": "doc-a-deleted", "foreign deleted": "doc-b-deleted"},
        _OWNER_B: {"own": "doc-b", "foreign": "doc-a", "missing": "doc-none",
                   "deleted": "doc-b-deleted", "foreign deleted": "doc-a-deleted"},
    }
    owned: dict[tuple[int, str, str], httpx.Response] = {}
    refused: dict[tuple[int, str, str], httpx.Response] = {}
    for owner, ids in kinds.items():
        for kind, doc_id in ids.items():
            for method in ("GET", "PATCH"):
                body = {"title": "x", "base_revision": 2} if method == "PATCH" else None
                r = world.client.request(method, f"/probe/docs/{doc_id}", json=body, headers=_cookie(owner))
                (owned if kind == "own" else refused)[(owner, kind, method)] = r
    assert {key: r.status_code for key, r in owned.items()} == {
        (_OWNER_A, "own", "GET"): 200, (_OWNER_A, "own", "PATCH"): 200,
        (_OWNER_B, "own", "GET"): 200, (_OWNER_B, "own", "PATCH"): 200,
    }
    assert owned[(_OWNER_A, "own", "GET")].json() == {"id": "doc-a", "title": "A's notes"}
    assert len(refused) == 16
    expected = (404, _json_bytes({"detail": dict(NOT_FOUND_DETAIL)}), _JSON_ONLY)
    assert {_stable(r) for r in refused.values()} == {expected}


def test_a_conflict_is_reachable_on_your_own_resource_and_never_across_tenants(world: _World) -> None:
    """A well-formed, stale body: 409 against your own resource, 404 against
    someone else's — so the state check runs only after ownership."""
    stale = {"title": "x", "base_revision": 1}
    own = world.client.patch("/probe/docs/doc-a", json=stale, headers=_cookie(_OWNER_A))
    assert (own.status_code, own.json()) == (409, {"detail": _CONFLICT_DETAIL})

    foreign = world.client.patch("/probe/docs/doc-b", json=stale, headers=_cookie(_OWNER_A))
    missing = world.client.patch("/probe/docs/doc-none", json=stale, headers=_cookie(_OWNER_A))
    assert _stable(foreign) == _stable(missing) == (404, _json_bytes({"detail": dict(NOT_FOUND_DETAIL)}), _JSON_ONLY)
    assert world.docs.rows["doc-b"] == _Doc(owner_id=_OWNER_B, title="B's notes")


def test_a_role_failure_is_one_403_that_names_no_resource(world: _World) -> None:
    """A player gets the same 403 for an id that exists, one that does not and
    a route with no id — and a GM gets through (the positive control)."""
    player = _cookie(_PLAYER)
    refused = [
        world.client.get("/probe/docs/doc-a", headers=player),
        world.client.get("/probe/docs/doc-none", headers=player),
        world.client.post("/probe/docs", json={"title": "t"}, headers=player),
        world.client.get("/probe/ping", headers=player),
    ]
    assert len(refused) == 4
    assert {_stable(r) for r in refused} == {(403, _json_bytes({"detail": dict(FORBIDDEN_ROLE_DETAIL)}), _JSON_ONLY)}
    assert world.client.get("/probe/ping", headers=_cookie(_OWNER_A)).json() == {"pong": "yes"}


def test_the_gm_gate_declares_the_session_so_overrides_reach_through_it(world: _World) -> None:
    """F-7: `gm_session` declares `Depends(require_session)`, so the suite's
    default-session override keeps working on a Workbench route."""
    world.probe.dependency_overrides[require_session] = lambda: SessionData(user_id=_OWNER_A, role="dm")
    assert world.client.get("/probe/docs/doc-a").status_code == 200
    world.probe.dependency_overrides[require_session] = lambda: SessionData(user_id=_OWNER_A, role="player")
    assert world.client.get("/probe/docs/doc-a").status_code == 403


def test_unknown_paths_and_wrong_methods_keep_fastapis_answers(world: _World) -> None:
    unknown = world.client.get("/probe/nothing-here", headers=_cookie(_OWNER_A))
    wrong = world.client.put("/probe/docs/doc-a", json={}, headers=_cookie(_OWNER_A))
    assert (unknown.status_code, unknown.content) == (404, b'{"detail":"Not Found"}')
    assert (wrong.status_code, wrong.content) == (405, b'{"detail":"Method Not Allowed"}')


# ── A4: the order of checks ──────────────────────────────────────────────────


def test_a_foreign_origin_is_refused_before_authentication_and_role(world: _World) -> None:
    """SEC-7's check runs first (an inference: an origin refusal names no
    resource and no account, and it refuses a forged request before a cookie
    is looked up). Positive control: the same request from no browser is 401."""
    foreign = {"origin": "https://evil.example"}
    no_cookie = world.client.post("/probe/docs", json={"title": "t"}, headers=foreign)
    as_player = world.client.post("/probe/docs", json={"title": "t"}, headers={**foreign, **_cookie(_PLAYER)})
    origin_refusal = (403, _json_bytes({"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}), _JSON_ONLY)
    assert _stable(no_cookie) == _stable(as_player) == origin_refusal
    assert world.client.post("/probe/docs", json={"title": "t"}).status_code == 401
    assert world.client.post("/probe/docs", json={"title": "t"}, headers=_cookie(_PLAYER)).status_code == 403


def test_a_body_that_fails_the_model_is_401_without_a_cookie(world: _World) -> None:
    """Dependencies run before body-model errors are raised, so an invalid but
    well-formed body tells a signed-out caller nothing. Signed in, it is 422."""
    bad = {"title": 5, "base_revision": 2}
    assert world.client.patch("/probe/docs/doc-a", json=bad).status_code == 401
    assert world.client.patch("/probe/docs/doc-a", json=bad, headers=_cookie(_OWNER_A)).status_code == 422


def test_malformed_json_is_refused_before_the_origin_check_and_authentication(world: _World) -> None:
    """NOT a defect: FastAPI parses a JSON body before it solves any dependency,
    so malformed JSON under a JSON content type is a 422 ahead of the origin
    check and of authentication. SEC-3 permits exactly this — "body validation
    that depends on nothing but the body may run first, because it reveals
    nothing about any resource": nothing has run, no resource or account is
    named, and the answer is the Workbench body, which echoes nothing."""
    hostile = {"origin": "https://evil.example"}
    answers = [
        world.client.post("/probe/docs", content=b'{"title": ', headers={**hostile, "content-type": kind})
        for kind in ("application/json", "application/merge-patch+json")
    ]
    assert len(answers) == 2
    body = _json_bytes({"detail": {"code": "validation_failed", "message": "That request isn't valid.",
                                   "retryable": False}})
    assert {_stable(r) for r in answers} == {(422, body, _JSON_ONLY)}


def test_a_non_json_content_type_reaches_the_origin_check(world: _World) -> None:
    """Lead ruling Q-15: only malformed JSON is the pre-dependency 422. A form,
    or a body with no content type at all, is refused by the origin check —
    signed in or not. Positive control: the same bytes as JSON are allowed."""
    form = {"content-type": "application/x-www-form-urlencoded"}
    refused = [
        world.client.post("/probe/docs", content=b"title=x", headers=form),
        world.client.post("/probe/docs", content=b"title=x", headers={**form, **_cookie(_OWNER_A)}),
        world.client.post("/probe/docs", content=b'{"title": "x"}'),
        world.client.post("/probe/docs", content=b'{"title": "x"}', headers=_cookie(_OWNER_A)),
    ]
    assert len(refused) == 4
    assert {_stable(r) for r in refused} == {(403, _json_bytes({"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}),
                                               _JSON_ONLY)}
    as_json = world.client.post("/probe/docs", content=b'{"title": "x"}',
                                headers={"content-type": "application/json", **_cookie(_OWNER_A)})
    assert (as_json.status_code, as_json.json()) == (200, {"title": "x"})


# ── A5: SEC-7, both sides ────────────────────────────────────────────────────

#: Each refusal case, and the change that turns it into an allowed request.
_ORIGIN_REFUSALS: list[tuple[str, dict[str, str], bool, dict[str, str]]] = [
    ("foreign Origin", {"origin": "https://evil.example"}, False, {"origin": "http://testserver"}),
    ("Origin: null", {"origin": "null"}, False, {"origin": "http://testserver"}),
    ("Sec-Fetch-Site: cross-site", {"sec-fetch-site": "cross-site"}, False, {"sec-fetch-site": "same-origin"}),
    ("Sec-Fetch-Site: same-site", {"sec-fetch-site": "same-site"}, False, {"sec-fetch-site": "same-origin"}),
    ("Sec-Fetch-Site: none", {"sec-fetch-site": "none"}, False, {"sec-fetch-site": "same-origin"}),
    ("form content type", {"content-type": "application/x-www-form-urlencoded"}, False,
     {"content-type": "application/json"}),
    ("body with no content type", {}, True, {"content-type": "application/json"}),
]


def _origin_request(world: _World, method: str, headers: dict[str, str], strip_type: bool) -> httpx.Response:
    world.docs.rows = _seed_docs()  # each twin succeeds on a fresh store
    body = {"PATCH": {"title": "x", "base_revision": 2}, "POST": {"title": "x"}, "DELETE": {"title": "x"}}[method]
    path = "/probe/docs" if method == "POST" else "/probe/docs/doc-a"
    sent = {"content-type": "application/json", **headers, **_cookie(_OWNER_A)}
    if strip_type:
        del sent["content-type"]
    return world.client.request(method, path, content=json.dumps(body).encode(), headers=sent)


def test_every_origin_refusal_is_one_403_and_each_has_an_allowed_twin(world: _World) -> None:
    """T-7, signed in as the owner so that nothing but the origin check can
    refuse. DELETE carries a body here only to reach the content-type clause."""
    assert [label for label, *_ in _ORIGIN_REFUSALS] == [
        "foreign Origin", "Origin: null", "Sec-Fetch-Site: cross-site", "Sec-Fetch-Site: same-site",
        "Sec-Fetch-Site: none", "form content type", "body with no content type",
    ]
    refused: dict[tuple[str, str], tuple[int, bytes, tuple[tuple[str, str], ...]]] = {}
    twins: dict[tuple[str, str], int] = {}
    for method in ("POST", "PATCH", "DELETE"):
        for label, bad, strip_type, fixed in _ORIGIN_REFUSALS:
            refused[(method, label)] = _stable(_origin_request(world, method, bad, strip_type=strip_type))
            twins[(method, label)] = _origin_request(world, method, fixed, strip_type=False).status_code
        # Neither Origin nor Sec-Fetch-Site: not a browser, allowed.
        twins[(method, "neither header")] = _origin_request(world, method, {}, strip_type=False).status_code
    assert len(refused) == 21
    assert set(refused.values()) == {(403, _json_bytes({"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}), _JSON_ONLY)}
    assert len(twins) == 24
    assert set(twins.values()) == {200}


def test_a_get_is_never_origin_checked(world: _World) -> None:
    hostile = {"origin": "https://evil.example", "sec-fetch-site": "cross-site", **_cookie(_OWNER_A)}
    r = world.client.get("/probe/docs/doc-a", headers=hostile)
    assert (r.status_code, r.json()) == (200, {"id": "doc-a", "title": "A's notes"})


def _bare_request(method: str, headers: list[tuple[str, str]]) -> Request:
    return Request({"type": "http", "method": method, "path": "/", "query_string": b"",
                    "headers": [(name.encode(), value.encode()) for name, value in headers]})


def test_the_origin_rule_at_its_edges() -> None:
    """The comparison and the body clause, one request at a time."""
    check = workbench_api.origin_check()
    cases: dict[str, tuple[str, list[tuple[str, str]]]] = {
        "same host and port": ("POST", [("host", "localhost:5173"), ("origin", "http://localhost:5173")]),
        "Host port differs from Origin port": ("POST", [("host", "localhost:5173"), ("origin", "http://localhost:4173")]),
        "Host names the scheme's default port": ("POST", [("host", "example.test:443"), ("origin", "https://example.test")]),
        "Host in another case": ("POST", [("host", "Example.Test"), ("origin", "https://example.test")]),
        "IPv6 literal": ("POST", [("host", "[::1]:8000"), ("origin", "http://[::1]:8000")]),
        "Origin with a path": ("POST", [("host", "example.test"), ("origin", "https://example.test/x")]),
        "Origin with userinfo": ("POST", [("host", "example.test"), ("origin", "https://u@example.test")]),
        "Origin of another scheme": ("POST", [("host", "example.test"), ("origin", "chrome-extension://example.test")]),
        "Host with userinfo": ("POST", [("host", "u@example.test"), ("origin", "https://example.test")]),
        "no Host at all": ("POST", [("origin", "https://example.test")]),
        "a port that is not a port": ("POST", [("host", "example.test:99999999"), ("origin", "http://example.test")]),
        "chunked JSON": ("PUT", [("transfer-encoding", "chunked"), ("content-type", "application/json")]),
        "chunked, no type": ("PUT", [("transfer-encoding", "chunked")]),
        "unreadable length, no type": ("PATCH", [("content-length", "abc")]),
        "empty body, no type": ("DELETE", [("content-length", "0")]),
        "JSON with a charset": ("POST", [("content-length", "2"), ("content-type", "Application/JSON; charset=utf-8")]),
        "HEAD from anywhere": ("HEAD", [("origin", "https://evil.example"), ("sec-fetch-site", "cross-site")]),
    }
    verdicts: dict[str, str] = {}
    for label, (method, headers) in cases.items():
        try:
            check(_bare_request(method, headers))
            verdicts[label] = "allowed"
        except HTTPException as refused:
            assert (refused.status_code, refused.detail) == (403, dict(FORBIDDEN_ORIGIN_DETAIL))
            verdicts[label] = "refused"
    assert verdicts == {
        "same host and port": "allowed", "Host port differs from Origin port": "refused",
        "Host names the scheme's default port": "allowed", "Host in another case": "allowed",
        "IPv6 literal": "allowed", "Origin with a path": "refused", "Origin with userinfo": "refused",
        "Origin of another scheme": "refused", "Host with userinfo": "refused", "no Host at all": "refused",
        "a port that is not a port": "refused", "chunked JSON": "allowed", "chunked, no type": "refused",
        "unreadable length, no type": "refused", "empty body, no type": "allowed",
        "JSON with a charset": "allowed", "HEAD from anywhere": "allowed",
    }


#: Requirement 5's allow table, transcribed from the files that produce each
#: request on c119dfc: (Host, Origin, Sec-Fetch-Site, where it comes from).
_ALLOW_TABLE: list[tuple[str, str | None, str | None, str]] = [
    # ui/nginx.conf:29,37,45,54,61,72 set `proxy_set_header Host $host`, and
    # $host drops the port; the browser is at ui/e2e/stack.ts:29
    # COMPOSE_STACK_URL = 'http://127.0.0.1:4173', published by
    # docker-compose.e2e.yml:64 "4173:80".
    ("127.0.0.1", "http://127.0.0.1:4173", "same-origin", "E2E through nginx"),
    # ui/vite.config.ts:19-26 uses the string proxy target, which does not set
    # changeOrigin, so the browser's Host (with its port) reaches the service.
    ("localhost:5173", "http://localhost:5173", "same-origin", "Vite dev proxy"),
    # Cloud Run terminates TLS and forwards HTTP (config.py:171-175): the app
    # cannot see its own scheme, and Host carries no port.
    ("svc-xyz.a.run.app", "https://svc-xyz.a.run.app", "same-origin", "Cloud Run"),
    # TestClient: no Origin and no Sec-Fetch-Site — not a browser.
    ("testserver", None, None, "not a browser"),
]


def test_the_allow_table_has_every_front_end() -> None:
    assert [source for *_, source in _ALLOW_TABLE] == [
        "E2E through nginx", "Vite dev proxy", "Cloud Run", "not a browser"]


@pytest.mark.parametrize(("host", "origin", "fetch_site", "source"), _ALLOW_TABLE)
def test_every_front_end_this_repository_ships_is_allowed(
    world: _World, host: str, origin: str | None, fetch_site: str | None, source: str,
) -> None:
    """A real 200, not merely "not 403": a misjudgement here is an outage."""
    headers = {"host": host, **_cookie(_OWNER_A)}
    if origin is not None:
        headers["origin"] = origin
    if fetch_site is not None:
        headers["sec-fetch-site"] = fetch_site
    r = world.client.post("/probe/docs", json={"title": source}, headers=headers)
    assert (r.status_code, r.json()) == (200, {"title": source}), source


def test_no_cors_header_on_a_successful_workbench_answer(world: _World) -> None:
    """Checked on a 200: a refusal carries no CORS header either, so a test of a
    refusal alone would pass for the wrong reason."""
    same_origin = {"origin": "http://testserver", "sec-fetch-site": "same-origin", **_cookie(_OWNER_A)}
    ok = world.client.post("/probe/docs", json={"title": "t"}, headers=same_origin)
    assert ok.status_code == 200
    assert [name for name in ok.headers if name.lower().startswith("access-control-")] == []
    preflight = world.client.options("/probe/docs", headers={
        "origin": "https://evil.example", "access-control-request-method": "POST"})
    assert preflight.status_code == 405
    assert [name for name in preflight.headers if name.lower().startswith("access-control-")] == []


# ── A6: one application-wide validation handler ──────────────────────────────


@pytest.mark.real_auth
def test_the_real_app_answers_validation_errors_through_the_workbench_handler() -> None:
    assert app.exception_handlers[RequestValidationError] is workbench_api.handle_validation_error


def test_a_workbench_validation_failure_echoes_nothing(world: _World) -> None:
    r = world.client.patch("/probe/docs/doc-a", json={"title": [_CANARY], "base_revision": 2},
                           headers=_cookie(_OWNER_A))
    assert r.status_code == 422
    assert r.json() == {"detail": {"code": "validation_failed", "message": "That request isn't valid.",
                                   "retryable": False, "field": "title"}}
    assert _CANARY not in r.text


def test_a_legacy_validation_failure_keeps_fastapis_default(world: _World) -> None:
    """The same handler, a non-Workbench route on the same app: FastAPI's list,
    `input` echo included (a recorded residual, left as it is)."""
    r = world.client.post("/legacy/docs", json={"title": [_CANARY]})
    assert (r.status_code, r.json()) == (422, {"detail": [{
        "type": "string_type", "loc": ["body", "title"], "msg": "Input should be a valid string",
        "input": [_CANARY]}]})


def test_the_validation_log_record_exists_and_is_redacted(world: _World, caplog: pytest.LogCaptureFixture) -> None:
    """In this order: the record exists; its payload equals `redacted_errors`
    by value, with the method and the prefix-joined template; and only then the
    canary is in no record — which would hold vacuously over zero records."""
    caplog.set_level(logging.INFO, logger=workbench_api.__name__)
    world.client.patch("/probe/docs/doc-a", json={"title": [_CANARY], "base_revision": 2},
                       headers=_cookie(_OWNER_A))
    records = [record for record in caplog.records if record.name == workbench_api.__name__]
    assert len(records) == 1
    assert records[0].args == ("PATCH", "/probe/docs/{doc_id}", [
        {"type": "string_type", "loc": ["body", "title"], "msg": "Input should be a valid string"}])
    assert not any(_CANARY in record.getMessage() for record in caplog.records)


# ── A9: the auth matrix walk sees router-mounted, wrapper-guarded routes ─────


def _legacy_walk(target: FastAPI) -> list[tuple[str, str]]:
    """The walk `test_every_session_guarded_route_is_in_the_matrix` used before
    oe6, kept executable so the contrast is shown, not asserted in prose."""
    found: list[tuple[str, str]] = []
    for route in target.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None or not any(d.call is require_session for d in dependant.dependencies):
            continue
        path = getattr(route, "path", "")
        found += [(method, path) for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}]
    return found


def test_the_auth_matrix_walk_sees_a_router_mounted_wrapper_guarded_route() -> None:
    target = FastAPI()
    router = workbench_router(gm_session(require_session))

    @router.get("/things/{thing_id}")
    def read_thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    target.include_router(router, prefix="/probe")

    @target.get("/legacy")
    def legacy(session: SessionData = Depends(require_session)) -> dict[str, int]:
        return {"user_id": session.user_id}

    guarded = sorted(_session_guarded_routes(api_routes(target)))
    assert guarded == [("GET", "/legacy"), ("GET", "/probe/things/{thing_id}")]
    assert _legacy_walk(target) == [("GET", "/legacy")]  # it never saw the router's route


def test_the_spa_parity_walk_sees_a_router_mounted_route() -> None:
    target = FastAPI()
    router = workbench_router(gm_session(require_session))

    @router.get("/lore/{entry_id}")
    def read_entry(entry_id: str) -> dict[str, str]:
        return {"id": entry_id}

    target.include_router(router)
    assert _live_api_prefixes(target) == {"/lore", "/docs", "/openapi.json", "/redoc"}


@pytest.mark.real_auth
def test_the_spa_parity_walk_still_reserves_every_prefix_it_reserved_before() -> None:
    """Moving the walk onto `api_routes()` dropped nothing: FastAPI's own
    documentation routes stay reserved though they are not API routes."""
    assert _live_api_prefixes() == {"/openapi.json", "/docs", "/redoc", "/healthz", "/models", "/chat",
                                    "/metrics", "/conversations", "/auth"}


# ── A10: the route census ────────────────────────────────────────────────────

#: Transcribed from c119dfc, where 1kg.4.2 B's timeline route had merged and
#: 1kg.2.4 A2 and 1kg.2.7 had not. A new route, of either posture, fails the
#: census until its author says which it is.
EXPECTED_LEGACY_ROUTES = {
    ("GET", "/healthz"), ("GET", "/models"), ("POST", "/chat"), ("POST", "/metrics/ui"),
    ("GET", "/conversations/{conversation_id}/messages"),
    ("GET", "/conversations/{conversation_id}/timeline"),
    ("GET", "/conversations/{conversation_id}/attachments"),
    ("POST", "/conversations/{conversation_id}/attachments"),
    ("POST", "/auth/signup"), ("POST", "/auth/login"), ("POST", "/auth/logout"), ("GET", "/auth/me"),
}
#: None yet. The route beads add theirs here; the follow-up bead moves the
#: timeline route from the set above to this one.
EXPECTED_WORKBENCH_ROUTES: set[tuple[str, str]] = set()


def _census(target: FastAPI) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    legacy: set[tuple[str, str]] = set()
    workbench: set[tuple[str, str]] = set()
    for path, route in api_routes(target):
        if _is_spa(route):
            continue
        rows = {(method, path) for method in (route.methods or set()) - {"HEAD", "OPTIONS"}}
        (workbench if isinstance(route, WorkbenchRoute) else legacy).update(rows)
    return legacy, workbench


@pytest.mark.real_auth
def test_the_route_census_is_complete() -> None:
    """Both halves by value. The legacy half is never empty, so this is not
    vacuous; the Workbench half is empty BY ASSERTION, which is a statement,
    not a silence. The split is on the route object (`ctx.original_route`) —
    a route context is never an instance of anything that matters here."""
    legacy, workbench = _census(app)
    assert legacy == EXPECTED_LEGACY_ROUTES
    assert workbench == EXPECTED_WORKBENCH_ROUTES


def test_the_census_filters_the_spa_fallback_by_name(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    target = FastAPI()

    @target.get("/healthz")
    def healthz() -> dict[str, str]:
        return {}

    install_spa(target, tmp_path)
    everything = sorted((m, p) for p, r in api_routes(target) for m in (r.methods or set()) - {"HEAD"})
    assert everything == [("GET", "/"), ("GET", "/healthz"), ("GET", "/profile"), ("GET", "/workspace")]
    assert _census(target) == ({("GET", "/healthz")}, set())


def test_api_routes_reports_router_mounted_routes_with_their_prefix() -> None:
    """The prefix-joined path and the ROUTE object, for a router route too;
    FastAPI's documentation routes are not API routes and are not reported."""
    target = FastAPI()

    @target.get("/legacy")
    def legacy() -> dict[str, str]:
        return {}

    router = workbench_router(gm_session(require_session))

    @router.get("/things/{thing_id}")
    def read_thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    target.include_router(router, prefix="/api")
    rows = [(path, type(route).__name__) for path, route in api_routes(target)]
    assert rows == [("/legacy", "APIRoute"), ("/api/things/{thing_id}", "WorkbenchRoute")]


def test_api_routes_is_loud_when_it_finds_nothing() -> None:
    with pytest.raises(AssertionError, match="found no APIRoute"):
        api_routes(FastAPI())


# ── 10.4: a route module that builds its own 401/403/404 is refused ──────────

#: Built by concatenation so that this file does not itself contain the token,
#: and the repository count below stays meaningful.
_DELIBERATE = "# workbench-api: " + "deliberate-status"
_DELIBERATE_WITH_REASON = re.compile(re.escape(_DELIBERATE) + r"\s*[-:—]*\s*\S")
_OWN_STATUS = re.compile(
    r"status_code\s*=\s*(?:40[134]\b|status\.HTTP_40[134]_)"
    r"|HTTPException\(\s*(?:40[134]\b|status\.HTTP_40[134]_)"
    r"|Response\([^)]*,\s*40[134]\b"
    r"|\bstatus\.HTTP_40[134]_\w+"
)


def _own_refusals(path: Path) -> list[tuple[int, str]]:
    """Lines of `path` that build a 401, 403 or 404 themselves. A line is
    exempt only when it, or the line above it, carries the deliberate-status
    token followed by a reason."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        (number, line.strip())
        for number, line in enumerate(lines, start=1)
        if _OWN_STATUS.search(line)
        and not any(_DELIBERATE_WITH_REASON.search(text) for text in (line, lines[number - 2] if number > 1 else ""))
    ]


def _workbench_route_modules(target: FastAPI) -> set[Path]:
    """The source file of every Workbench route's endpoint. `getsourcefile`
    rather than `endpoint.__module__`: a module name would still have to be
    resolved to a file. Nothing here wraps an endpoint with `functools.wraps`."""
    modules: set[Path] = set()
    for _, route in api_routes(target):
        if isinstance(route, WorkbenchRoute):
            source = inspect.getsourcefile(route.endpoint)
            assert source is not None, f"no source file for {route.endpoint!r}"
            modules.add(Path(source).resolve())
    return modules


_CLEAN_MODULE = '''
from fastapi import Depends

from service.session import SessionData
from service.workbench_api import SessionDependency, not_found, workbench_router


def build_router(gm: SessionDependency):
    router = workbench_router(gm)

    @router.get("/clean/{thing_id}")
    def read_clean(thing_id: str, session: SessionData = Depends(gm)) -> dict[str, str]:
        if thing_id != "t1":
            not_found()
        return {"id": thing_id}

    return router
'''
_DIRTY_MODULE = _CLEAN_MODULE.replace("/clean/", "/dirty/").replace("read_clean", "read_dirty").replace(
    "            not_found()",
    '            raise HTTPException(status_code=404, detail="no such thing")',
).replace("from fastapi import Depends", "from fastapi import Depends, HTTPException")


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_structural_checker_refuses_a_module_that_builds_its_own_status(tmp_path: Path) -> None:
    clean, dirty = tmp_path / "clean.py", tmp_path / "dirty.py"
    clean.write_text(_CLEAN_MODULE, encoding="utf-8")
    dirty.write_text(_DIRTY_MODULE + "\nX = status.HTTP_403_FORBIDDEN\nY = HTTPException(401)\n", encoding="utf-8")
    assert _own_refusals(clean) == []
    assert [text for _, text in _own_refusals(dirty)] == [
        'raise HTTPException(status_code=404, detail="no such thing")',
        "X = status.HTTP_403_FORBIDDEN",
        "Y = HTTPException(401)",
    ]


def test_the_deliberate_status_token_is_the_only_exemption(tmp_path: Path) -> None:
    refusal = 'raise HTTPException(status_code=404)'
    cases = {
        "token and reason, same line": f"{refusal}  {_DELIBERATE}: a dark capability answers like no route",
        "token and reason, line above": f"{_DELIBERATE}: a dark capability answers like no route\n{refusal}",
        "no token": refusal,
        "token without a reason": f"{refusal}  {_DELIBERATE}",
    }
    verdicts: dict[str, int] = {}
    for label, text in cases.items():
        path = tmp_path / f"case_{len(verdicts)}.py"
        path.write_text(text + "\n", encoding="utf-8")
        verdicts[label] = len(_own_refusals(path))
    assert verdicts == {"token and reason, same line": 0, "token and reason, line above": 0,
                        "no token": 1, "token without a reason": 1}


def test_no_module_in_the_repository_uses_the_deliberate_status_token_yet() -> None:
    """Zero today, by value. `1kg.8.1` slices b-d will raise it, each use with
    its reason, for a dark capability's plain 404."""
    tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO_ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
    assert len(tracked) > 100  # the listing itself worked
    uses = [name for name in tracked if _DELIBERATE in (REPO_ROOT / name).read_text(encoding="utf-8")]
    assert uses == []


def test_the_route_module_derivation_finds_real_module_files(world: _World, tmp_path: Path) -> None:
    """The derivation must work the day a route lands, so it is exercised now,
    on two route modules written to real files and loaded from them."""
    clean, dirty = tmp_path / "probe_clean_routes.py", tmp_path / "probe_dirty_routes.py"
    clean.write_text(_CLEAN_MODULE, encoding="utf-8")
    dirty.write_text(_DIRTY_MODULE, encoding="utf-8")
    gm = gm_session(require_session)
    target = world.probe
    target.include_router(_load(clean).build_router(gm))
    target.include_router(_load(dirty).build_router(gm))

    # This file's probe routes are Workbench routes too, so it is derived as well.
    assert _workbench_route_modules(target) == {clean.resolve(), dirty.resolve(), Path(__file__).resolve()}
    assert [text for _, text in _own_refusals(clean)] == []
    assert [text for _, text in _own_refusals(dirty)] == [
        'raise HTTPException(status_code=404, detail="no such thing")']
    # The dirty module really does answer a 404 of its own.
    owner = _cookie(_OWNER_A)
    assert world.client.get("/clean/nope", headers=owner).json() == {"detail": dict(NOT_FOUND_DETAIL)}
    assert world.client.get("/dirty/nope", headers=owner).json() == {"detail": "no such thing"}


@pytest.mark.real_auth
def test_no_workbench_route_on_the_real_app_builds_its_own_status() -> None:
    modules = _workbench_route_modules(app)
    assert modules == set()  # none yet; a route bead adds its module here
    assert [(path.name, _own_refusals(path)) for path in modules if _own_refusals(path)] == []

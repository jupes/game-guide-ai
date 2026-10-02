"""
The posture every Workbench route inherits (agent-forge-harness-oe6): GM routes,
account routes that act for the signed-in account on its own offers and seats
(agent-forge-harness-1kg.2.2), and the table routes (agent-forge-harness-1kg.2.3).

One 401 body (SEC-2), one non-enumerating 404 from one code path (SEC-3), the
origin check (SEC-7) and one application-wide validation handler (SEC-23),
built once so that no route bead has to build its own. The routes on it are the
four conversation routes (`service/conversations_api.py`) and the conversation
timeline (`service/timeline_api.py`, moved here by `agent-forge-harness-oqx`),
the GM's campaigns and seats (`service/campaigns_api.py`), an account's own
offers and seats (`service/seats_api.py`, on `account_router`), the GM's table
session (`service/table_session_api.py`), the table routes
(`service/table_api.py`, on their own router with their own route class), and
the GM's tool invocations (`service/tool_invocations_api.py`, bead 1kg.4.1).

What makes a route a Workbench route
------------------------------------
Its `APIRoute` object is an instance of `WorkbenchRoute`, which it is when it
was declared on a router made by `workbench_router(...)`. Nothing keys on a
path prefix: `/conversations` is served by legacy routes and will be served by
Workbench ones, and at request time `scope["route"].path` does not even carry
the prefix an `include_router(..., prefix=...)` added. The two handlers
`install_workbench` registers branch on that membership (SEC-23 and S-A say
"by path"; route membership is the same rule with a key that can be
implemented). Every other route — every legacy route, an unknown path, a 405
— is answered by FastAPI's own default handler, byte for byte, but for a
validation error, which echoes nothing (`legacy_validation_errors`).

Two factories here. `workbench_router` applies the `dm` gate through
`gm_session`, so it is for GM routes and nothing else. `account_router` is the
same posture without the role: a route that acts for the signed-in account on
its own offers and seats, which a player must reach and so must a GM seated at
another GM's table. Table routes have their own factory in `service/table_api.py`
(`1kg.2.3`), whose route class is a `WorkbenchRoute` that sets
`forwards_cookie_deletion`; it reuses `origin_check` and refuses with
`inactive()` and `cross_site()`, built here beside `not_found()`.

The order of checks, as a client observes it
--------------------------------------------
1. Malformed JSON under a JSON content type (`application/json` or `+json`) on
   a route that declares a body model is a 422 with the Workbench validation
   body BEFORE the origin check and before authentication: FastAPI parses the
   body before it solves any dependency. Nothing has run and no resource is
   named, which SEC-3 permits in as many words ("body validation that depends
   on nothing but the body may run first"). Any other content type reaches the
   origin check, which refuses it. A route that reads its body by hand from a
   raw `Request` has no such case at all.
2. The origin check (`POST`, `PUT`, `PATCH`, `DELETE` only) → 403.
   Inferred, not SEC-3's printed list: an origin refusal names no resource and
   no account, and running it first refuses a forged cross-site request before
   the service looks up an attacker-supplied cookie. Reversal: swap the two
   entries in `workbench_router`'s dependency list.
3. Authentication → the one 401 body, whatever `require_session` said.
4. Role → 403 (`gm_session`), naming no resource.
4b. Write throttle (agent-forge-harness-531x) → 429, the existing
    `throttled_user` shape, before any route's own body is read. A GET, HEAD
    or OPTIONS, and a route marked `reads_by_post` (a search sent by POST,
    X-7), spend nothing from the caller's per-account budget.
5. Ownership, in the statement → `not_found()`: missing, someone else's and
   deleted are one answer from one call.
6. Validation that depends on the resource, then state (409). Both are
   therefore unreachable for a resource the caller does not own.

Wiring a route module
---------------------
A route module cannot import `service.app` (which imports it), so it takes the
application's GM dependency as a parameter::

    # in service/app.py, after require_session is defined:
    WORKBENCH_GM = workbench_api.gm_session(require_session)
    app.include_router(conversations_api.build_router(WORKBENCH_GM))

    # in service/conversations_api.py, which imports nothing from service.app:
    def build_router(gm: SessionDependency) -> APIRouter:
        router = workbench_api.workbench_router(gm)

        @router.get("/conversations")
        def index(session: SessionData = Depends(gm)) -> ConversationPage: ...

        return router

A route refuses with `not_found()` — or, on a table route, `inactive()` and
`cross_site()` — and never builds a 401, 403 or 404 of its own;
`service/tests/test_workbench_api.py` fails a route module that does. The
one exemption is a line carrying the deliberate-status token documented in
`docs/ARCHITECTURE.md`, with its reason on the same comment.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine, Mapping, Sequence
from types import MappingProxyType
from typing import Any, ClassVar, NoReturn
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, params
from fastapi.dependencies.models import Dependant
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from . import ratelimit
from .session import SessionData
from .workbench_contracts import ErrorBody, ErrorCode, ErrorInfo, redacted_errors, validation_error_body

log = logging.getLogger(__name__)

#: The callable a route declares with `Depends(...)` — never a `Depends` value,
#: which `Depends` would not resolve a second time.
type SessionDependency = Callable[..., SessionData]


def _refusal(code: ErrorCode, message: str) -> Mapping[str, object]:
    """The `detail` object of a Workbench refusal, serialised the way the
    contract's examples are, and read-only: callers pass a copy."""
    body = ErrorBody(detail=ErrorInfo(code=code, message=message, retryable=False))
    return MappingProxyType(body.model_dump(mode="json", exclude_none=True)["detail"])


#: The one authentication failure (SEC-2). A string `detail`, outside the error
#: envelope: `ErrorCode` has no 401 member, and the client signs out on the
#: status alone (`ui/src/api.ts`). docs/workbench-wire-contract.md says so.
UNAUTHENTICATED_BODY: Mapping[str, object] = MappingProxyType({"detail": "not signed in"})
#: Missing, someone else's and deleted (SEC-3, CANVAS-31).
NOT_FOUND_DETAIL = _refusal(ErrorCode.NOT_FOUND, "That isn't available.")
#: The `dm` gate (R-3). The same sentence the timeline route answers.
FORBIDDEN_ROLE_DETAIL = _refusal(ErrorCode.FORBIDDEN, "This is a Game Master feature.")
#: SEC-7. The same code as a role refusal; the message tells an operator apart.
FORBIDDEN_ORIGIN_DETAIL = _refusal(ErrorCode.FORBIDDEN, "That request didn't come from this application.")
#: SEC-40: a Remove whose password did not check out. It depends on nothing but
#: the password and names no resource, and it is a 403, never a 401 — the
#: client signs out on any 401 (bead 1kg.2.2, L-12).
REAUTH_FAILED_DETAIL = _refusal(ErrorCode.REAUTH_FAILED, "That password isn't right.")
#: SEC-46's one table answer (TABLE-9): every signed-in caller a table route
#: does not entitle, whatever the reason, gets exactly this 404.
INACTIVE_DETAIL = _refusal(ErrorCode.INACTIVE, "There's no live table here.")
#: SEC-45's Fetch Metadata refusal on a table route. It depends on nothing but
#: those headers; the sentence is SEC-7's, because the cause is the same.
CROSS_SITE_DETAIL = _refusal(ErrorCode.CROSS_SITE, "That request didn't come from this application.")
#: Every Workbench answer's `Cache-Control` (agent-forge-harness-5ir1, release
#: review S8): a campaign's private content must not stay in a shared
#: device's browser cache. A route that sends its own keeps it.
NO_STORE = "no-store"


class WorkbenchRoute(APIRoute):
    """The membership marker: a route is a Workbench route iff its route
    object is one of these. Only `workbench_router`, `account_router` and the
    table router (`service/table_api.py`) should create them.

    `forwards_cookie_deletion` is the one thing a subclass may change (bead
    1kg.2.3, L-22): when it is set, the one 401 carries a `Set-Cookie` of the
    exception that deletes a cookie, and nothing else of it. Only the table
    route class sets it — a table answer deletes a screen-grant cookie that is
    no longer live (SEC-44) — so every GM and account route's 401 stays exactly
    one body, with no header of the exception's.

    Every answer a Workbench route builds — its own, a refusal, a 422 —
    carries `Cache-Control: no-store` unless the route set one (5ir1)."""

    forwards_cookie_deletion: ClassVar[bool] = False

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    headers["cache-control"] = NO_STORE
            await send(message)

        await super().handle(scope, receive, sending)


def not_found() -> NoReturn:
    """The ONE way a Workbench route answers 404 (SEC-3)."""
    raise HTTPException(status_code=404, detail=dict(NOT_FOUND_DETAIL))


def inactive() -> NoReturn:
    """The ONE way a table route answers a caller it does not entitle (SEC-46):
    `404 inactive`, identical for every reason. A cookie deletion the table
    route class adds rides on it like on any other refusal."""
    raise HTTPException(status_code=404, detail=dict(INACTIVE_DETAIL))


def cross_site() -> NoReturn:
    """The ONE way a table route refuses Fetch Metadata (SEC-45): `403
    cross_site`, before any cookie is read or any row touched."""
    raise HTTPException(status_code=403, detail=dict(CROSS_SITE_DETAIL))


def reauth_failed() -> NoReturn:
    """The ONE way a Workbench route refuses a password it asked for again
    (SEC-40): `403 reauth_failed`, not retryable, naming no resource."""
    raise HTTPException(status_code=403, detail=dict(REAUTH_FAILED_DETAIL))


def gm_session(session: SessionDependency) -> SessionDependency:
    """Wrap the application's session dependency in the `dm` requirement (R-3).

    It declares `Depends(session)` rather than calling it, so a test's
    `dependency_overrides[require_session]` still reaches through it. This is
    the single place the role rule lives: when `yje.4.1` replaces roles with
    tiers (TA-3, A-25), this function is what changes.
    """

    def gm(caller: SessionData = Depends(session)) -> SessionData:
        if caller.role != "dm":
            raise HTTPException(status_code=403, detail=dict(FORBIDDEN_ROLE_DETAIL))
        return caller

    return gm


_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


# ── The write throttle (agent-forge-harness-531x) ────────────────────────────
#: The write throttle's fixed sentence (SEC-3: a refusal names no resource).
WRITE_THROTTLED_MESSAGE = "You're saving a lot at once. Wait, then try again."
#: Set on an endpoint function by `reads_by_post`: it is state-changing by HTTP
#: method (a search sent by POST, X-7) but writes nothing, so it spends no
#: write budget. Checked by attribute, never by name, so renaming a route
#: cannot silently exempt it.
_READS_ONLY = "_workbench_reads_only"
#: Set on the throttle dependency itself, so the route-pin test
#: (`test_workbench_write_throttle.py`) can find it in a route's effective
#: dependant tree without depending on the dependency's name or module.
_WRITE_THROTTLE = "_workbench_write_throttle"
#: Set beside `_WRITE_THROTTLE` on a throttle that spends the narrowing budget
#: (ID-8), so a second pin can hold the set of routes on that budget exact.
_NARROWING_THROTTLE = "_workbench_narrowing_throttle"
#: The narrowing throttle's fixed sentence (a Stop, SEC-3: names no resource).
NARROWING_THROTTLED_MESSAGE = "Too many stops at once. Wait, then try again."


def reads_by_post[F: Callable[..., object]](endpoint: F) -> F:
    """Mark a POST route that writes nothing (`documents_api.library`, a
    search sent by POST so its text stays out of URLs, X-7): it must not spend
    a write from the caller's budget, or the GM's read would use up their
    write budget."""
    setattr(endpoint, _READS_ONLY, True)
    return endpoint


def spend_write(user_id: int) -> None:
    """The ONE way a Workbench write throttle answers 429: the existing
    Workbench throttle shape (`campaigns_api.refusal`, SEC's `throttled_user`),
    naming no resource, logged with no body and no path id beyond the
    template."""
    try:
        ratelimit.check_workbench_write(user_id)
        return
    except ratelimit.RateLimited as exc:
        wait = min(exc.retry_after, 86_400)
    log.info("workbench write throttled (user_id=%s, retry_after=%ss)", user_id, wait)
    info = ErrorInfo(code=ErrorCode.THROTTLED_USER, message=WRITE_THROTTLED_MESSAGE, retryable=True, retry_after_s=wait)
    body = ErrorBody(detail=info).model_dump(mode="json", exclude_none=True)
    raise HTTPException(status_code=429, detail=body["detail"], headers={"Retry-After": str(wait)})


def write_throttle(session: SessionDependency) -> Callable[..., None]:
    """The router-level dependency that spends one write from the caller's
    budget on every state-changing request, before any route's own body is
    read. A GET, HEAD or OPTIONS, and a route marked `reads_by_post`, spend
    nothing. Declares `Depends(session)` rather than calling it, exactly as
    `gm_session` does, so the session is resolved once per request and a
    test's `dependency_overrides` still reaches through it."""

    def throttle(request: Request, caller: SessionData = Depends(session)) -> None:
        if request.method not in _STATE_CHANGING:
            return
        route = request.scope.get("route")
        endpoint = getattr(route, "endpoint", None)
        if getattr(endpoint, _READS_ONLY, False):
            return
        spend_write(caller.user_id)

    setattr(throttle, _WRITE_THROTTLE, True)
    return throttle


def spend_narrowing(user_id: int) -> None:
    """A reveal Stop's own budget (1kg.7.2, ID-8). Past the bound it answers the
    same 429 envelope as `spend_write`, with its own sentence, logged with no
    body and no path id."""
    try:
        ratelimit.check_reveal_stop(user_id)
        return
    except ratelimit.RateLimited as exc:
        wait = min(exc.retry_after, 86_400)
    log.info("workbench stop throttled (user_id=%s, retry_after=%ss)", user_id, wait)
    info = ErrorInfo(
        code=ErrorCode.THROTTLED_USER, message=NARROWING_THROTTLED_MESSAGE, retryable=True, retry_after_s=wait
    )
    body = ErrorBody(detail=info).model_dump(mode="json", exclude_none=True)
    raise HTTPException(status_code=429, detail=body["detail"], headers={"Retry-After": str(wait)})


def narrowing_throttle(session: SessionDependency) -> Callable[..., None]:
    """`write_throttle`'s shape, spending `spend_narrowing` instead: the router-level
    dependency of a narrowing (a reveal Stop), so autosaves that emptied the
    shared write budget never refuse it. Marked as a write throttle, so the route
    pin counts the route as throttled, and as a narrowing throttle, so the second
    pin holds the narrowing routes exact."""

    def throttle(request: Request, caller: SessionData = Depends(session)) -> None:
        if request.method not in _STATE_CHANGING:
            return
        spend_narrowing(caller.user_id)

    setattr(throttle, _WRITE_THROTTLE, True)
    setattr(throttle, _NARROWING_THROTTLE, True)
    return throttle


def mark_write_throttle[F: Callable[..., object]](dependency: F) -> F:
    """Mark a dependency outside this module (`table_api.mint_throttle`) as a
    write-throttle check, so the route-pin test finds it in a route's
    effective dependant tree without importing this module's private marker."""
    setattr(dependency, _WRITE_THROTTLE, True)
    return dependency


def is_write_throttle(dependency: object) -> bool:
    """Whether a dependency's `call` is a write-throttle check — `write_throttle`'s
    own, or one `mark_write_throttle` marked."""
    return bool(getattr(dependency, _WRITE_THROTTLE, False))


def is_narrowing_throttle(dependency: object) -> bool:
    """Whether a dependency's `call` is a narrowing throttle (ID-8)."""
    return bool(getattr(dependency, _NARROWING_THROTTLE, False))


def is_reads_only(endpoint: object) -> bool:
    """Whether a route's endpoint was marked `reads_by_post`: state-changing by
    HTTP method, but writes nothing, so it spends no write-throttle budget."""
    return bool(getattr(endpoint, _READS_ONLY, False))


def _is_own_origin(origin: str, host: str | None) -> bool:
    """Whether `Origin` names the host this request was sent to.

    The scheme is not compared and the port only when `Host` carries one: the
    service cannot know its own scheme (Cloud Run forwards HTTP), and nginx
    forwards `Host` without the port the browser used (`proxy_set_header Host
    $host`). A cross-site page can forge neither header. `Sec-Fetch-Site`,
    which does compare scheme and port, is enforced whenever it is sent.
    """
    if host is None:
        return False
    try:
        claimed, served = urlsplit(origin), urlsplit(f"//{host}")
        claimed_port, served_port = claimed.port, served.port
    except ValueError:
        return False
    well_formed = (
        claimed.scheme in _DEFAULT_PORTS
        and claimed.hostname is not None
        and claimed.username is None
        and not (claimed.path or claimed.query or claimed.fragment)
        and served.hostname is not None
        and served.username is None
        and not (served.path or served.query or served.fragment)
    )
    if not well_formed or claimed.hostname != served.hostname:
        return False
    if served_port is None:
        return True
    return (claimed_port or _DEFAULT_PORTS[claimed.scheme]) == served_port


_DECIMAL_LENGTH = re.compile(r"[0-9]+")


def _carries_body(request: Request) -> bool:
    """A length that is anything but ASCII digits (`+0`, `abc`) is unreadable,
    and is treated as a body so that the content-type clause fails closed."""
    if "transfer-encoding" in request.headers:
        return True
    length = request.headers.get("content-length")
    if length is None:
        return False
    return _DECIMAL_LENGTH.fullmatch(length) is None or int(length) != 0


def origin_check(content_types: Sequence[str] = ("application/json",)) -> Callable[[Request], None]:
    """SEC-7, for state-changing methods only. All three clauses must hold:

    1. an `Origin`, if sent, is this application's own (`null` is not);
    2. a `Sec-Fetch-Site`, if sent, is exactly `same-origin`;
    3. a request that carries a body declares one of `content_types`.

    A request with neither header is not a browser and is allowed. There is no
    CORS here, and there must be none: no `Access-Control-*` header is emitted.
    """
    accepted = frozenset(kind.lower() for kind in content_types)

    def check(request: Request) -> None:
        if request.method not in _STATE_CHANGING:
            return
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if (
            (origin is not None and not _is_own_origin(origin, request.headers.get("host")))
            or (fetch_site is not None and fetch_site != "same-origin")
            or (_carries_body(request) and media_type not in accepted)
        ):
            raise HTTPException(status_code=403, detail=dict(FORBIDDEN_ORIGIN_DETAIL))

    return check


def workbench_router(
    gm: SessionDependency,
    *,
    prefix: str = "",
    dependencies: Sequence[params.Depends] = (),
    content_types: Sequence[str] = ("application/json",),
    throttle: Callable[[SessionDependency], Callable[..., None]] = write_throttle,
) -> APIRouter:
    """The router every Workbench GM route is declared on. `throttle` builds the
    router's last dependency; a narrowing (a reveal Stop) passes
    `narrowing_throttle` so it spends its own budget (1kg.7.2).

    Router-level dependencies run in this order, before any route's own:
    `dependencies` (a capability switch that must answer like an unknown path
    belongs here), then the origin check, then `gm`, then the write throttle
    (agent-forge-harness-531x) → 429. A route cannot forget any of them; a
    handler that needs the session declares `Depends(gm)` as well, and FastAPI
    resolves it once per request — the throttle's own `Depends(gm)` is the same
    cached call, so the session is looked up only once.
    """
    return APIRouter(
        prefix=prefix,
        route_class=WorkbenchRoute,
        dependencies=[*dependencies, Depends(origin_check(content_types)), Depends(gm), Depends(throttle(gm))],
    )


def account_router(
    session: SessionDependency,
    *,
    prefix: str = "",
    content_types: Sequence[str] = ("application/json",),
) -> APIRouter:
    """The router a route that acts for the signed-in account is declared on
    (bead 1kg.2.2, L-3): `workbench_router` without the `dm` gate.

    The same route class, so the one 401 body and the Workbench validation
    handler apply, and the same order: the origin check, then `session`, then
    the write throttle (agent-forge-harness-531x) → 429. There is no role
    check — a player must reach these routes, and so must a GM who holds a
    seat at another GM's table. Everything a route here reads is the caller's
    own, found by the account in the statement.
    """
    return APIRouter(
        prefix=prefix,
        route_class=WorkbenchRoute,
        dependencies=[Depends(origin_check(content_types)), Depends(session), Depends(write_throttle(session))],
    )


# justification: `Coroutine`'s send and yield types; FastAPI awaits the dependency and
# uses only its `bytes` result.
def body_reader(max_bytes: int) -> Callable[[Request], Coroutine[Any, Any, bytes]]:
    """A dependency that reads a route's raw body, at most `max_bytes` (1kg.4.1,
    I-23). A longer one is refused as soon as it is known to be longer — by its
    declared length, or by the first chunk that crosses the line — and the rest
    is never read. The refusal is the Workbench `422 validation_failed` naming
    no field, which the one validation handler answers (SEC-23).

    `service/conversations_api.read_body` is the same algorithm at 8 KiB; a
    route whose body can be longer — a 2,000-code-point brief sent as escaped
    JSON is about 24 KiB — takes its own bound from here."""
    if max_bytes < 1:
        raise ValueError("a body bound is at least one byte")

    async def read(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise _too_long()
        received = bytearray()
        async for chunk in request.stream():
            received += chunk
            if len(received) > max_bytes:
                raise _too_long()
        return bytes(received)

    return read


def _too_long() -> RequestValidationError:
    """A body past its bound: `validation_failed`, no field, nothing echoed."""
    return RequestValidationError([{"type": "value_error", "loc": (), "msg": "the body is too long"}])


def api_routes(app: FastAPI) -> list[tuple[str, APIRoute]]:
    """Effective path (prefix-joined) and route object for every API route.

    The one way anything in this repository enumerates what the app serves.
    `app.routes` no longer is the route table: `include_router` appends one
    private object and the included routes are not in it.

    iter_route_contexts yields RouteContext, NOT APIRoute. Attribute access
    proxies the route, so ctx.path / ctx.methods / ctx.dependant all work —
    but isinstance() is the one operation the proxy does not forward, and
    `isinstance(ctx, APIRoute)` is False for EVERY context. Filtering on it
    returns [] and the caller's walk then passes over nothing.
    ctx.original_route is the route object, and the only thing to isinstance.

    The route object's `.dependant` holds the route's own and its router's
    dependencies, but NOT one passed to `include_router(..., dependencies=...)`:
    that lives only on the context. Anything asking "is this route guarded?"
    reads `api_route_dependants()` instead.

    Not `app.openapi()["paths"]`: a route declared with `include_in_schema=False`
    is missing from it, and a guard built on it would go blind the same way.
    """
    rows = [(path, route) for path, route, _ in _api_route_rows(app)]
    assert rows, "api_routes() found no APIRoute — did FastAPI's route model change?"
    return rows


def api_route_dependants(app: FastAPI) -> list[tuple[str, APIRoute, Dependant]]:
    """`api_routes()`, with each route's EFFECTIVE dependant: the one FastAPI
    solves for a request. It is `ctx.dependant`, which adds the dependencies an
    `include_router(..., dependencies=...)` call passed to the route's own and
    its router's; `route.dependant` lacks them, so a guard walk over it would
    pass over a route guarded that way."""
    rows = _api_route_rows(app)
    assert rows, "api_route_dependants() found no APIRoute — did FastAPI's route model change?"
    return rows


def _api_route_rows(app: FastAPI) -> list[tuple[str, APIRoute, Dependant]]:
    rows: list[tuple[str, APIRoute, Dependant]] = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.original_route
        if not isinstance(route, APIRoute) or ctx.path is None:
            continue
        dependant = ctx.dependant
        assert isinstance(dependant, Dependant), f"no effective dependant for {ctx.path}"
        rows.append((ctx.path, route, dependant))
    return rows


def is_workbench_route(request: Request) -> bool:
    """Membership by class. An unknown path has no route in its scope."""
    return isinstance(request.scope.get("route"), WorkbenchRoute)


def _route_template(request: Request) -> str:
    """The prefix-joined template of the route serving `request`, for a log
    line. `scope["route"].path` lacks an `include_router` prefix."""
    route = request.scope.get("route")
    return next((path for path, candidate in api_routes(request.app) if candidate is route), "(unknown)")


def _deleted_cookie(headers: Mapping[str, str] | None) -> str | None:
    """The exception's `Set-Cookie`, if it deletes a cookie (`Max-Age=0`), else
    None. Only the attribute is judged; the cookie's name is the route
    module's, and never spelled here."""
    for name, value in (headers or {}).items():
        if name.lower() != "set-cookie":
            continue
        attributes = [part.strip().lower() for part in value.split(";")[1:]]
        if "max-age=0" in attributes:
            return value
    return None


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    """Every authentication failure on a Workbench route is one body (SEC-2).

    Everything else is FastAPI's own default handler — the exact function it
    installs — so every legacy answer, 404 and 405 is unchanged. No header of
    the exception is copied onto the one 401, with one exception (bead
    1kg.2.3, L-22): on a route whose class sets `forwards_cookie_deletion` — a
    table route — a `Set-Cookie` that deletes a cookie is copied, and nothing
    else, so a screen grant that is no longer live is deleted by the 401 its
    request earned (SEC-44). The body is the same one body either way.
    """
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 401 and is_workbench_route(request):
        route = request.scope.get("route")
        deletion = _deleted_cookie(exc.headers) if getattr(route, "forwards_cookie_deletion", False) else None
        headers = {"set-cookie": deletion} if deletion is not None else None
        return JSONResponse(status_code=401, content=dict(UNAUTHENTICATED_BODY), headers=headers)
    return await http_exception_handler(request, exc)


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    """ONE application-wide validation handler (SEC-23).

    A Workbench route answers `validation_error_body`, which echoes nothing it
    was sent, and logs `redacted_errors` with the method and route template —
    never the exception's text, its raw errors or the URL (SEC-20, SEC-21).
    Every other route keeps FastAPI's list shape, `legacy_validation_errors`:
    its default repeated each error's `input`, a rejected password included
    (agent-forge-harness-fhq9), and a lone surrogate in it was a 500 (5mj).

    Once answered, the error and every error it chains drop their tracebacks
    (agent-forge-harness-ust7, review H1). FastAPI's frame holds the error it
    raised, and the error's traceback holds that frame, so the parsed body in
    it stayed alive until a full garbage collection, which rarely comes: about
    27 MB for each 1 MiB body of empty objects.
    """
    assert isinstance(exc, RequestValidationError)
    try:
        return await _answer_validation_error(request, exc)
    finally:
        link: BaseException | None = exc
        seen: set[int] = set()
        while link is not None and id(link) not in seen:
            seen.add(id(link))
            link.__traceback__ = None
            link = link.__cause__ or link.__context__


# justification: pydantic's error dicts carry values of any type (redacted_errors takes the same).
def legacy_validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A legacy route's 422 list (agent-forge-harness-fhq9): `redacted_errors`,
    each error's type, location and message, never its `input` or `ctx`. One
    Pydantic message also repeats what was sent, a discriminated union's
    unknown tag: it is answered without the tag."""
    out = redacted_errors(errors)
    for error in out:
        if error["type"] == "union_tag_invalid":
            error["msg"] = "Input tag does not match any of the expected tags"
    return out


async def _answer_validation_error(request: Request, exc: RequestValidationError) -> Response:
    if not is_workbench_route(request):
        return JSONResponse(status_code=422, content={"detail": legacy_validation_errors(exc.errors())})
    errors = exc.errors()
    log.info("workbench request refused by validation: %s %s %s",
             request.method, _route_template(request), redacted_errors(errors))
    body = validation_error_body(errors).model_dump(mode="json", exclude_none=True)
    return JSONResponse(status_code=422, content=body)


def install_workbench(app: FastAPI) -> None:
    """Register the two application-wide handlers on `app`.

    Registering a handler for `HTTPException` replaces FastAPI's for every
    route, which is why both handlers delegate to FastAPI's own default for
    anything that is not a Workbench route.
    """
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)

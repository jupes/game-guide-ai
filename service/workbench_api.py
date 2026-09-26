"""
The posture every Workbench GM route inherits (agent-forge-harness-oe6).

One 401 body (SEC-2), one non-enumerating 404 from one code path (SEC-3), the
origin check (SEC-7) and one application-wide validation handler (SEC-23),
built once so that no route bead has to build its own. There are no Workbench
routes yet; this is the seam they attach to.

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
— is answered by FastAPI's own default handler, byte for byte.

GM routes only. `workbench_router` applies the `dm` gate through `gm_session`,
so it is for GM routes and nothing else. Table routes wait on `hgm` (TA-2) and
must reuse `origin_check` in their own factory.

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

A route refuses with `not_found()` and never builds a 401, 403 or 404 of its
own; `service/tests/test_workbench_api.py` fails a route module that does. The
one exemption is a line carrying the deliberate-status token documented in
`docs/ARCHITECTURE.md`, with its reason on the same comment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import NoReturn
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, params
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

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


class WorkbenchRoute(APIRoute):
    """The membership marker: a route is a Workbench route iff its route
    object is one of these. Only `workbench_router` should create them."""


def not_found() -> NoReturn:
    """The ONE way a Workbench route answers 404 (SEC-3)."""
    raise HTTPException(status_code=404, detail=dict(NOT_FOUND_DETAIL))


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


def _carries_body(request: Request) -> bool:
    if "transfer-encoding" in request.headers:
        return True
    length = request.headers.get("content-length")
    if length is None:
        return False
    try:
        return int(length) != 0
    except ValueError:
        return True


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
) -> APIRouter:
    """The router every Workbench GM route is declared on.

    Router-level dependencies run in this order, before any route's own:
    `dependencies` (a capability switch that must answer like an unknown path
    belongs here), then the origin check, then `gm`. A route cannot forget any
    of them; a handler that needs the session declares `Depends(gm)` as well,
    and FastAPI resolves it once per request.
    """
    return APIRouter(
        prefix=prefix,
        route_class=WorkbenchRoute,
        dependencies=[*dependencies, Depends(origin_check(content_types)), Depends(gm)],
    )


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

    Its `.dependant` holds the route's own and its router's dependencies. One
    passed to `include_router(..., dependencies=...)` lives only on the context
    (`ctx.dependant`); nothing here passes one, and `workbench_router` puts its
    guard on the router, where the route object carries it.

    Not `app.openapi()["paths"]`: a route declared with `include_in_schema=False`
    is missing from it, and a guard built on it would go blind the same way.
    """
    rows = [
        (ctx.path, ctx.original_route)
        for ctx in iter_route_contexts(app.routes)
        if isinstance(ctx.original_route, APIRoute) and ctx.path is not None
    ]
    assert rows, "api_routes() found no APIRoute — did FastAPI's route model change?"
    return rows


def is_workbench_route(request: Request) -> bool:
    """Membership by class. An unknown path has no route in its scope."""
    return isinstance(request.scope.get("route"), WorkbenchRoute)


def _route_template(request: Request) -> str:
    """The prefix-joined template of the route serving `request`, for a log
    line. `scope["route"].path` lacks an `include_router` prefix."""
    route = request.scope.get("route")
    return next((path for path, candidate in api_routes(request.app) if candidate is route), "(unknown)")


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    """Every authentication failure on a Workbench route is one body (SEC-2).

    Everything else is FastAPI's own default handler — the exact function it
    installs — so every legacy answer, 404 and 405 is unchanged. No header of
    the exception is copied onto the one 401.
    """
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 401 and is_workbench_route(request):
        return JSONResponse(status_code=401, content=dict(UNAUTHENTICATED_BODY))
    return await http_exception_handler(request, exc)


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    """ONE application-wide validation handler (SEC-23).

    A Workbench route answers `validation_error_body`, which echoes nothing it
    was sent, and logs `redacted_errors` with the method and route template —
    never the exception's text, its raw errors or the URL (SEC-20, SEC-21).
    Every other route keeps FastAPI's default answer, `input` echo included (a
    recorded residual).
    """
    assert isinstance(exc, RequestValidationError)
    if not is_workbench_route(request):
        return await request_validation_exception_handler(request, exc)
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

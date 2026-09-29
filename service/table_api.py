"""The table routes: screen mode over HTTP (bead 1kg.2.3's PR-B,
agent-forge-harness-1kg.2.10; threat model SEC-44 to SEC-49, owner decision D-13).

The first `/table/` routes, on their own router with their own route class,
`TableRoute`: a `WorkbenchRoute`, so the application's one validation handler
(SEC-23), the one 401 body (SEC-2) and the route census and structural checks
cover it — but **no `dm` gate and no tier**: a table route asks for no role
(SEC-41). The application hands `build_router` its `require_session`, its auth
store getter, its `_clear_session_cookie` and the table-session lifecycle; this
module imports nothing from `service.app`, no GM route module and no document,
history, conversation, timeline or asset store (SEC-44(2), T-23 — a test reads
the imports).

- `POST /table/screen`: `ScreenMintAnswer`, and in the same answer the grant's
  cookie, the account cookie deleted and `Clear-Site-Data: "cache", "storage"`
  (SEC-48).
- `POST /table/leave`: `204`, and never a 401: it never reads or ends an
  account session (SEC-49).

**Router-level dependencies are exactly two, in this order.** Fetch Metadata
(SEC-45): a `Sec-Fetch-Site` that is present and not `same-origin`, or
`Sec-Fetch-Mode: navigate`, is `403 cross_site` before any cookie is read or
any row touched; it is logged with a closed label and never budgeted. Then
SEC-7's `origin_check`. **The principal is not a router dependency**: a
router-level session check would 401 a live screen, which holds no account, and
Leave must never 401.

**One principal per request** (SEC-44(3)). `table_principal` reads the
screen-grant cookie first and resolves it with one digest lookup. A **live**
grant decides alone and the account session is not consulted at all. A grant
that is not live is ignored and marked for deletion, and only then is the
account session read — through the application's own `require_session`, called
directly, whose refusal is re-raised as it is. A screen may not mint (DV-2): it
is `inactive`, like every caller who is not the owner of a campaign with a live
session (SEC-46, §15.6).

**A stale grant is deleted by every answer from the principal step on** — the
mint's 401, `inactive`, `409 screen_limit`, a 503, Leave's 204 — by
`TableRoute`'s handler wrapper, which adds the deletion to the response or to
the refusal's headers; the one 401 carries it because this route class sets
`forwards_cookie_deletion` (L-22). The answers made before that step — `403
cross_site`, the origin 403, and a 422, which the application's one validation
handler builds — do not, and the next request deletes it. A successful mint
replaces a stale grant with its own `Set-Cookie`, and never sends a deletion
beside it (one name, one path: two would be order-dependent).

**Every answer of a table route** carries `Cache-Control: no-store` and
`Cross-Origin-Resource-Policy: same-origin` (SEC-17's value for table
responses, TA-6/TA-7), added by `TableRoute` to whatever built the answer, once.
No route-level `Content-Security-Policy` or `Referrer-Policy`: these are JSON
answers that never redirect, and the table page's policy is `1kg.7.4`'s.

The grant is 32 CSPRNG bytes; only its digest is stored (SEC-5). It leaves the
server in exactly one place, the mint's `Set-Cookie` — never a body, a URL, a
log line or a metric label. The cookie's name is spelled here and nowhere else
in the service.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Message, Receive, Scope, Send

import config

from . import campaign_identity as ident
from .auth_store import AuthStore
from .campaign_store import MissingParent, ScreenLimit
from .session import SessionData
from .table_session_store import LiveScreen
from .table_sessions import BackendUnavailable, Inactive, TableSessions
from .workbench_api import WorkbenchRoute, cross_site, inactive, origin_check
from .workbench_contracts import (
    CONTRACT_VERSION,
    ErrorBody,
    ErrorCode,
    ErrorInfo,
    SchemaVersion,
    ScreenMintAnswer,
    ScreenMintRequest,
    TableLeaveRequest,
)

log = logging.getLogger(__name__)

#: The screen grant's cookie (SEC-48): `HttpOnly; SameSite=Strict; Path=/table`,
#: host-only. `Path=/table` keeps it off every GM and account path (SEC-44(4)).
SCREEN_COOKIE = "gga_screen"
SCREEN_COOKIE_PATH = "/table"
#: What a grant looks like on the wire: `secrets.token_urlsafe(32)`. Anything
#: else is not a grant, and is deleted without a lookup.
_GRANT_SHAPE = re.compile(rf"^[A-Za-z0-9_-]{{{ident.SECRET_CHARS}}}$")
#: SEC-48, SEC-49: never `"cookies"` (it would race the grant's own
#: `Set-Cookie`) and never `"*"`.
CLEAR_SITE_DATA = '"cache", "storage"'
#: Every answer of a table route, whoever built it (TA-6, SEC-17, SEC-49).
TABLE_HEADERS = {"cache-control": "no-store", "cross-origin-resource-policy": "same-origin"}
#: Where the request records that its grant cookie must go (SEC-44(3)).
_STALE = "stale_screen_grant"

UNAVAILABLE_MESSAGE = "The table is briefly unavailable. Try again."
SCREEN_LIMIT_MESSAGE = "This table already has as many screens as it can."

type SessionCheck = Callable[[Request, AuthStore], SessionData]

#: `Literal[1]` on the wire, `int` as a constant: spelled once.
WIRE_VERSION = cast("SchemaVersion", CONTRACT_VERSION)


def get_clock() -> datetime:
    """One `now` per request. Tests override it."""
    return datetime.now(UTC)


# ── The route class ──────────────────────────────────────────────────────────


def _deletion() -> str:
    """The grant cookie's deletion, as Starlette writes one: `Max-Age=0` on the
    name and path the grant was set with."""
    carrier = Response()
    carrier.delete_cookie(
        SCREEN_COOKIE,
        path=SCREEN_COOKIE_PATH,
        secure=config.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )
    return carrier.headers["set-cookie"]


def _is_stale(request: Request) -> bool:
    return bool(getattr(request.state, _STALE, False))


def _forget(request: Request) -> None:
    """Mark this request's grant cookie for deletion by its answer."""
    setattr(request.state, _STALE, True)


def _sets_the_grant(response: Response) -> bool:
    prefix = f"{SCREEN_COOKIE}=".encode()
    return any(name == b"set-cookie" and value.startswith(prefix) for name, value in response.raw_headers)


class TableRoute(WorkbenchRoute):
    """A table route: a Workbench route whose 401 may carry a cookie deletion
    (L-22), whose answers delete a stale grant from the principal step on, and
    all of whose answers carry `TABLE_HEADERS`."""

    forwards_cookie_deletion = True

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def app(request: Request) -> Response:
            try:
                response = await handler(request)
            except StarletteHTTPException as exc:
                if _is_stale(request):
                    exc.headers = {**(exc.headers or {}), "set-cookie": _deletion()}
                raise
            if _is_stale(request) and not _sets_the_grant(response):
                response.raw_headers.append((b"set-cookie", _deletion().encode("latin-1")))
            return response

        return app

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in TABLE_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await super().handle(scope, receive, sending)


# ── The two router-level checks ──────────────────────────────────────────────


def fetch_metadata(request: Request) -> None:
    """SEC-45: nothing legitimately reaches a table API from another site, or by
    navigating to it. Judged on the two headers alone, before any cookie is
    read; a request with neither is judged by SEC-7's check, which follows."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        reason = "site"
    elif request.headers.get("sec-fetch-mode") == "navigate":
        reason = "navigate"
    else:
        return
    log.info("table request refused by fetch metadata (%s)", reason)
    cross_site()


# ── Refusals this module builds (none of them a 401, 403 or 404) ─────────────


def _refusal(status: int, code: ErrorCode, message: str, *, retryable: bool) -> StarletteHTTPException:
    body = ErrorBody(detail=ErrorInfo(code=code, message=message, retryable=retryable))
    return StarletteHTTPException(status_code=status, detail=body.model_dump(mode="json", exclude_none=True)["detail"])


def _unavailable() -> StarletteHTTPException:
    return _refusal(503, ErrorCode.BACKEND_UNAVAILABLE, UNAVAILABLE_MESSAGE, retryable=True)


def _guarded[T](work: Callable[[], T]) -> T:
    """One lifecycle call. What the database says is a retryable 503 with its
    type and SQLSTATE logged, never its message (which can quote a row)."""
    failure: StarletteHTTPException | None = None
    try:
        return work()
    except (psycopg.Error, BackendUnavailable) as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        log.warning("table route: database unavailable (%s%s)", type(exc).__name__, f", {sqlstate}" if sqlstate else "")
        failure = _unavailable()
    raise failure


# ── The principal ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GrantCookie:
    """The request's screen-grant cookie: absent, or present and live or not.
    The grant itself is hidden from `repr()`."""

    secret: str | None = field(default=None, repr=False)
    live: LiveScreen | None = None


@dataclass(frozen=True)
class Principal:
    """One principal per request (SEC-44(3)): a live screen, or an account."""

    screen: LiveScreen | None = None
    account: SessionData | None = None


def build_router(
    session: SessionCheck,
    auth_store: Callable[[], AuthStore],
    clear_session_cookie: Callable[[Response], None],
    lifecycle: Callable[[], TableSessions | None],
) -> APIRouter:
    """The table router, given the application's session check (called
    directly, never declared), its auth store getter, its account-cookie
    deletion and the lifecycle getter."""
    router = APIRouter(
        route_class=TableRoute,
        dependencies=[Depends(fetch_metadata), Depends(origin_check())],
    )

    def grant_cookie(
        request: Request,
        sessions: TableSessions | None = Depends(lifecycle),
        now: datetime = Depends(get_clock),
    ) -> GrantCookie:
        """The grant cookie, resolved with one digest lookup (SEC-46). One that
        is not live is marked for deletion; a database that cannot answer
        decides nothing, and deletes nothing."""
        secret = request.cookies.get(SCREEN_COOKIE)
        if secret is None:
            return GrantCookie()
        if _GRANT_SHAPE.fullmatch(secret) is None:
            _forget(request)
            return GrantCookie(secret)
        if sessions is None:
            raise _unavailable()
        live = _guarded(lambda: sessions.resolve_screen(secret, now=now))
        if live is None:
            _forget(request)
        return GrantCookie(secret, live)

    def table_principal(
        request: Request,
        grant: GrantCookie = Depends(grant_cookie),
        store: AuthStore = Depends(auth_store),
    ) -> Principal:
        """A live grant decides alone. Otherwise the account session, by the
        application's own check; its refusal is re-raised as it is, and the route
        class adds a stale grant's deletion to it."""
        if grant.live is not None:
            return Principal(screen=grant.live)
        return Principal(account=session(request, store))

    @router.post("/table/screen", response_model=ScreenMintAnswer)
    def mint(
        body: ScreenMintRequest,
        principal: Principal = Depends(table_principal),
        sessions: TableSessions | None = Depends(lifecycle),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """Make this browser a table screen (SEC-48, D-13). Only the owner of a
        campaign with a live session mints; a screen, a seated account and every
        other account get one `inactive` (§15.6, DV-2). At the bound, `409
        screen_limit`, and the account stays signed in."""
        account = principal.account
        if account is None or not ident.is_id(ident.CAMPAIGN, body.campaign_id):
            inactive()
        if sessions is None:
            raise _unavailable()
        refused: str | None = None
        try:
            minted = _guarded(lambda: sessions.mint_screen(account.user_id, body.campaign_id, now=now))
        except (Inactive, MissingParent):
            refused = "inactive"
        except ScreenLimit:
            refused = "limit"
        if refused == "inactive":
            inactive()
        if refused is not None:
            raise _refusal(409, ErrorCode.SCREEN_LIMIT, SCREEN_LIMIT_MESSAGE, retryable=False)
        answer = ScreenMintAnswer(schema_version=WIRE_VERSION, ends_at=minted.ends_at.astimezone(UTC))
        response = JSONResponse(answer.model_dump(mode="json"))
        lifetime = max(1, int((minted.ends_at - now).total_seconds()))
        response.set_cookie(
            SCREEN_COOKIE,
            minted.secret,
            max_age=lifetime,
            path=SCREEN_COOKIE_PATH,
            secure=config.SESSION_COOKIE_SECURE,
            httponly=True,
            samesite="strict",
        )
        clear_session_cookie(response)
        response.headers["clear-site-data"] = CLEAR_SITE_DATA
        response.headers["cache-control"] = "no-store"
        return response

    @router.post("/table/leave", status_code=204)
    def leave(
        request: Request,
        body: TableLeaveRequest,
        grant: GrantCookie = Depends(grant_cookie),
        sessions: TableSessions | None = Depends(lifecycle),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """Leave (SEC-49): a live grant is revoked and its cookie deleted, with
        `Clear-Site-Data`; a dead one's cookie is deleted; no cookie, nothing.
        Never a 401, and the account session is never read or ended."""
        response = Response(status_code=204)
        secret = grant.secret
        if grant.live is not None and secret is not None and sessions is not None:
            _guarded(lambda: sessions.leave(secret, now=now))
            _forget(request)
            response.headers["clear-site-data"] = CLEAR_SITE_DATA
        return response

    return router


"""Every request body is held to a ceiling before anything reads it
(agent-forge-harness-ust7, release review S1).

FastAPI reads and parses a route's whole body before validation runs, and Cloud
Run hands the app up to 32 MiB of it. Three concurrent 30 MB bodies sent to
`/auth/login` took one instance from 107 MB to 731 MB before any handler or
throttle ran. This middleware wraps every route, so no route can forget it:

- a declared `Content-Length` over the request's ceiling is refused with 413
  before one byte of the body is received;
- every body, declared or chunked, is counted as it arrives. The first chunk
  that crosses the ceiling ends it: the app receives nothing more, and whatever
  it was about to answer is replaced by the same 413.

The ceiling is `DEFAULT_MAX_BODY_BYTES` (`config.REQUEST_BODY_MAX_BYTES`) for
every request except the routes in `ANONYMOUS_CEILINGS` and the uploads in
`UPLOAD_CEILINGS`. The anonymous routes that parse a JSON body take a small
ceiling of their own: FastAPI parses a body before any check runs, and a body of
empty objects costs about 27 times its size once parsed. Each upload route
keeps its own exact cap and its own refusal. The ceiling here sits one default
body above that cap, so the route still gives its own answer to anything short
of an attack. `ui/nginx.conf` declares the same default at server level
(tests/test_body_limit_contract.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import config

from .documents_api import DOCUMENT_BODY_MAX_BYTES
from .workbench_contracts import ASSET_MAX_BYTES

#: Every body that is not an upload's. Every Workbench route caps its own body
#: far lower: 8 KiB, or 32 KiB for a tool invocation.
DEFAULT_MAX_BODY_BYTES: Final = config.REQUEST_BODY_MAX_BYTES

#: The one answer this layer gives, whatever the route. It echoes nothing.
TOO_LARGE_DETAIL: Final = "request body too large"


def _above(cap: int) -> int:
    """An upload's ceiling: the route's own cap plus one default body."""
    return cap + DEFAULT_MAX_BODY_BYTES


def _base64_length(size: int) -> int:
    """How many base64 characters encode `size` bytes."""
    return 4 * -(-size // 3)


@dataclass(frozen=True)
class Ceiling:
    method: str
    path: re.Pattern[str]
    max_bytes: int
    #: The route exists only while the media capability is on. While it is off,
    #: the path gets the default ceiling, as any unknown path does.
    media: bool = False


UPLOAD_CEILINGS: Final = (
    # A legacy attachment: a file of at most ATTACHMENT_MAX_BYTES, sent as base64 JSON.
    Ceiling("POST", re.compile(r"/conversations/[^/]+/attachments"),
            _above(_base64_length(config.ATTACHMENT_MAX_BYTES))),
    # A document create, and a field patch: each carries a whole document, which
    # the route itself caps at DOCUMENT_BODY_MAX_BYTES while it streams.
    Ceiling("POST", re.compile(r"/campaigns/[^/]+/documents"), _above(DOCUMENT_BODY_MAX_BYTES)),
    Ceiling("PATCH", re.compile(r"/campaigns/[^/]+/documents/[^/]+"), _above(DOCUMENT_BODY_MAX_BYTES)),
    # A media upload's bytes. The route caps each kind itself, while streaming.
    Ceiling("PUT", re.compile(r"/campaigns/[^/]+/assets/[^/]+/bytes"), _above(max(ASSET_MAX_BYTES.values())),
            media=True),
)


#: A sign-in, a sign-up or a UI metrics batch, which anyone may send. The
#: largest valid body of each, every non-ASCII character \u-escaped, is under
#: 60 KB (service/tests/test_body_limit.py). The default may not go below this
#: either (config.REQUEST_BODY_MAX_BYTES_FLOOR).
ANONYMOUS_MAX_BODY_BYTES: Final = config.REQUEST_BODY_MAX_BYTES_FLOOR

ANONYMOUS_CEILINGS: Final = tuple(
    Ceiling("POST", re.compile(path), ANONYMOUS_MAX_BODY_BYTES)
    # `/auth/google/start` is a native form POST (an invite, or a link): three short
    # fields, nowhere near this, and anyone may send it (lvs7).
    for path in ("/auth/login", "/auth/signup", "/auth/google/start", "/metrics/ui")
)


def ceiling_for(method: str, path: str, *, media_enabled: bool) -> int:
    for rule in (*ANONYMOUS_CEILINGS, *UPLOAD_CEILINGS):
        if rule.method == method and rule.path.fullmatch(path) and (media_enabled or not rule.media):
            return rule.max_bytes
    return DEFAULT_MAX_BODY_BYTES


class BodyTooLarge(Exception):
    """Raised into the app from `receive` once its body has crossed the ceiling."""


def _declared_length(scope: Scope) -> int | None:
    """The `Content-Length`, when it is ASCII digits. Any other value is left to
    the server and the route, and the body is counted as it arrives."""
    for name, value in scope["headers"]:
        if name == b"content-length" and value.isdigit():
            return int(value)
    return None


async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"detail": TOO_LARGE_DETAIL}, status_code=413)(scope, receive, send)


class BodyLimitMiddleware:
    """A pure ASGI middleware: it never reads a body, it only counts one."""

    def __init__(self, app: ASGIApp, media_enabled: Callable[[], bool] = lambda: False) -> None:
        self.app = app
        self.media_enabled = media_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        ceiling = ceiling_for(scope["method"], scope["path"], media_enabled=self.media_enabled())
        declared = _declared_length(scope)
        if declared is not None and declared > ceiling:
            await _refuse(scope, receive, send)
            return

        received = 0
        exceeded = started = False

        async def counted() -> Message:
            nonlocal received, exceeded
            if exceeded:
                raise BodyTooLarge
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > ceiling:
                    exceeded = True
                    raise BodyTooLarge
            return message

        async def guarded(message: Message) -> None:
            nonlocal started
            if exceeded:
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, guarded)
        except Exception:
            # FastAPI turns a failed body read into a 400, and a dependency's
            # read raises it through: either way it is this refusal.
            if not exceeded:
                raise
        if exceeded and not started:
            await _refuse(scope, receive, send)

"""Answer unexpected request failures without echoing request or exception values."""

from __future__ import annotations

import logging
from collections import deque
from traceback import walk_tb

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .body_limit import BodyTooLarge

_log = logging.getLogger(__name__)


class RouteErrors:
    """Recover before the headers are sent, within the response middleware."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        started = False

        async def sending(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, receive, sending)
        except BodyTooLarge:
            raise
        except Exception as exc:
            if started:
                # The server must terminate an incomplete response. Its log
                # receives a fixed error instead of the underlying exception.
                raise RuntimeError("HTTP response stopped after sending headers") from None
            locations: deque[str] = deque(maxlen=40)
            for frame, line in walk_tb(exc.__traceback__):
                globals_ = frame.f_globals
                module = globals_["__name__"] if frame.f_code.co_filename == globals_.get("__file__") else "<generated>"
                locations.append(f"  {module}, line {line}")
            _log.error("Unexpected route failure\nTraceback (module and line only):\n%s", "\n".join(locations))
            await JSONResponse(
                {"detail": "internal error"}, status_code=500, headers={"Cache-Control": "no-store"},
            )(scope, receive, send)

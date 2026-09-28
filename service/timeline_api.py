"""The conversation timeline route (1kg.4.2) on the Workbench scaffolding.

`GET /conversations/{conversation_id}/timeline`, the conversation as typed
entries, newest first. The read model is `service/timeline.py` and every
statement is `service/timeline_store.py`'s; this module is only the route.
`agent-forge-harness-oqx` moved it here from a hand-built `@app.get` in
`service/app.py`, onto a `workbench_router` (`agent-forge-harness-oe6`). Like
`service/conversations_api.py` it imports nothing from the application, which
hands `build_router` its GM gate and its two dependency getters, so the app's
overrides and its recovery apply unchanged.

**The posture is the scaffolding's.** The router runs authentication, whose
every failure is the one 401 body (SEC-2), then the `dm` role (R-3). Then, here:
the query, which depends on nothing else → the store → the path id → ownership,
in the statement → the page. A `GET` is never origin-checked (SEC-7 covers
writes only).

**One 404, and never a claim.** A conversation that is missing, has no owner
row, belongs to someone else, or has an id outside `OpaqueId` answers the
scaffolding's `not_found()`, from one code path (§8.1, SEC-3). `GET …/messages`
still claims, and keeps its 403: that difference is deliberate.

**Nothing the caller sent is echoed or logged.** `limit` and `cursor` are
declared `str | None`, so FastAPI never validates them, and a refused one is a
`RequestValidationError` naming the parameter and carrying no input: the
application's one validation handler answers it with `validation_error_body`
and logs it redacted (SEC-23). A database failure is logged by exception type
alone, never with its message or the path parameter (SEC-20).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError

from . import timeline
from .db import Database
from .session import SessionData
from .timeline_store import TimelineStore
from .workbench_api import SessionDependency, not_found, workbench_router
from .workbench_contracts import ErrorCode, TimelinePage

log = logging.getLogger(__name__)

PATH = "/conversations/{conversation_id}/timeline"


def _unavailable() -> HTTPException:
    """The route's 503: the timeline's own sentence, retryable. The scaffolding
    has no envelope for it; `exclude_none` keeps the optional keys out."""
    body = timeline.error_body(ErrorCode.BACKEND_UNAVAILABLE, timeline.UNAVAILABLE_MESSAGE, retryable=True)
    return HTTPException(status_code=503, detail=body.detail.model_dump(mode="json", exclude_none=True))


def _invalid_query(field: str) -> RequestValidationError:
    """A 422 naming the parameter at fault, never its value — the same error
    list `timeline.parameter_error_body` reads, so the same bytes."""
    return RequestValidationError([{"type": "value_error", "loc": ("query", field), "msg": "invalid"}])


def build_router(
    gm: SessionDependency,
    store: Callable[[], TimelineStore | None],
    database: Callable[[], Database | None],
) -> APIRouter:
    """The timeline route on a `workbench_router`, given the app's GM gate
    (`gm_session(require_session)`) and its timeline store and database getters."""
    router = workbench_router(gm)

    @router.get(PATH, response_model=TimelinePage)
    def conversation_timeline(
        conversation_id: str,
        user: SessionData = Depends(gm),
        limit: str | None = None,
        cursor: str | None = None,
        timeline_store: TimelineStore | None = Depends(store),
        db: Database | None = Depends(database),
    ) -> TimelinePage:
        """The conversation as typed entries, newest first (1kg.4.2)."""
        try:
            size, page_cursor = timeline.parse_page_query(limit, cursor)
        except timeline.ParameterRefused as refused:
            raise _invalid_query(refused.field) from None
        if timeline_store is None or db is None:
            raise _unavailable()
        try:
            # The path id's shape before any statement runs, and after the 503
            # gate, so malformed and missing never diverge: both 503 without a
            # store, both the one 404 with one (SEC-3).
            timeline.require_readable_id(conversation_id)
            # One transaction covers the ownership check and the read.
            with db.transaction() as unit:
                timeline.authorize(timeline_store, unit, conversation_id, user_id=user.user_id)
                return timeline.read_page(timeline_store, unit, conversation_id, limit=size, cursor=page_cursor)
        except timeline.ConversationNotFound:
            pass  # answered below, outside the `except`, so nothing is chained
        except psycopg.Error as exc:
            # The exception TYPE, never its message, which can carry a statement
            # and so a prompt (SEC-20), and never the path parameter, whose
            # `%0A` would forge a log line.
            log.warning("timeline read failed: %s", type(exc).__name__)
            raise _unavailable() from exc
        not_found()

    return router

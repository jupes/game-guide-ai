"""The GM's tool invocations (bead 1kg.4.1, slice B): three Workbench routes on
a `workbench_router` (`agent-forge-harness-oe6`), wired into `service/app.py`
with one line and importing nothing from it. The rules are
`service/tool_invocations.py`'s; this module reads the request, asks, and
answers.

| Route | Answers |
| --- | --- |
| `POST /campaigns/{campaign_id}/tool-invocations` | `200 ToolInvocation` — new, replayed or retried — or a refusal |
| `GET /campaigns/{campaign_id}/tool-invocations/{invocation_id}` | `200 ToolInvocation`, or the one 404 |
| `POST /campaigns/{campaign_id}/tool-invocations/{invocation_id}/cancel` | `200 ToolInvocation`, or the one 404 |

Once an attempt exists every route answers `200` with the invocation, whatever
it came to (I-3): a provider failure or an expiry is carried on it, so 502, 504
and `attempt_expired` never appear as response statuses here. An HTTP error is
a refusal before any attempt starts — which creates nothing (I-4) — or a `503`.

**The order of checks on the create** (SEC-3; the scaffolding's docstring):
origin (403) → authentication (the one 401) → role (403) → the body, at most
`TOOL_BODY_MAX_BYTES`, parsed by the contract (422, malformed JSON included:
this route reads raw bytes, so there is no pre-dependency 422, C-2) → the body's
campaign against the path's (422) → the database and the message store (503) →
the path's shape (404) → the chat half of the pilot day, read before T1 opens
(503 when it cannot be read) → T1: the GM's in-flight lock, ownership (404),
the stored key by state, then the guards (409, 429).

No handler logs anything a client sent. Every refusal body is `ErrorBody` with
a fixed sentence; the 401, the 403s and the 404 are the scaffolding's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import TypeAdapter, ValidationError

import config

from . import campaign_identity as ident
from . import usage_capture
from .campaign_store import PostgresCampaignStore
from .campaigns_api import invalid, logged_outage, parse_body
from .conversation_store import PostgresConversationStore
from .db import CampaignAuthzMissing, TransactionalDatabase
from .history import MessageStore
from .models import ChatMode
from .providers import ProviderClientFactory
from .session import SessionData
from .timeline_store import PostgresTimelineStore, TimelineStoreError
from .tool_invocation_store import PostgresToolInvocationStore, ToolInvocationStoreError
from .tool_invocations import (
    UNAVAILABLE_MESSAGE,
    Clock,
    ExecutionContext,
    ExecutorFailed,
    InvocationStores,
    NotFound,
    Refused,
    Replay,
    ToolExecutor,
    ToolSettings,
    Unreadable,
    cancel,
    cancellation_probe,
    complete,
    execute,
    parse_settings,
    read,
    submit,
    tool_availability,
)
from .workbench_api import SessionDependency, body_reader, not_found, workbench_router
from .workbench_contracts import ErrorBody, ErrorCode, InvocationId, ToolId, ToolInvocation, ToolInvocationRequest

log = logging.getLogger(__name__)

#: I-23: a 2,000-code-point brief sent as `\u`-escaped JSON is about 24 KiB,
#: beyond `conversations_api.read_body`'s 8 KiB.
TOOL_BODY_MAX_BYTES = 32_768
read_tool_body = body_reader(TOOL_BODY_MAX_BYTES)

#: Parsed once, at import: an unknown id in either variable fails startup (I-7).
_SETTINGS = parse_settings(config.WORKBENCH_ENABLED_TOOLS, config.WORKBENCH_CAPABILITIES)
#: Builds a client on first use only; nothing is built at import.
_FACTORY = ProviderClientFactory()
_INVOCATION_ID: TypeAdapter[str] = TypeAdapter(InvocationId)


def get_invocation_stores() -> InvocationStores:
    """The stores every route uses. Tests override this dependency."""
    return InvocationStores(
        PostgresToolInvocationStore(), PostgresCampaignStore(), PostgresConversationStore(), PostgresTimelineStore(),
    )


def get_tool_executors() -> Mapping[ToolId, ToolExecutor]:
    """The registered executors: none in this bead. `1kg.4.3`, `1kg.4.4` and
    `1kg.8.3` register theirs here; until then every tool is disabled."""
    return MappingProxyType({})


def get_tool_settings() -> ToolSettings:
    return _SETTINGS


def _utc_now() -> datetime:
    return datetime.now(UTC)


def get_clock() -> Clock:
    """The clock, not a moment: the completion reads it again after the
    provider work (I-16). Tests override it rather than sleeping."""
    return _utc_now


def get_provider_factory() -> ProviderClientFactory:
    return _FACTORY


# ── Answers ──────────────────────────────────────────────────────────────────


def _unavailable() -> HTTPException:
    return _refused(Refused(503, ErrorCode.BACKEND_UNAVAILABLE, UNAVAILABLE_MESSAGE, retryable=True))


def _refused(refused: Refused) -> HTTPException:
    """A refusal's `ErrorBody`, and `Retry-After` beside `retry_after_s`."""
    body = ErrorBody(detail=refused.info).model_dump(mode="json", exclude_none=True)
    wait = refused.info.retry_after_s
    headers = None if wait is None else {"Retry-After": str(wait)}
    return HTTPException(status_code=refused.status, detail=body["detail"], headers=headers)


def _answered[T](work: Callable[[], T]) -> T:
    """Run one step and map what it says. Each answer is raised OUTSIDE the
    `except`, so nothing is chained; an outage logs its class and SQLSTATE."""
    failure: HTTPException | None = None
    missing = False
    try:
        return work()
    except (NotFound, CampaignAuthzMissing):
        missing = True
    except Refused as refused:
        failure = _refused(refused)
    except (psycopg.Error, Unreadable, ExecutorFailed, ToolInvocationStoreError, TimelineStoreError) as exc:
        failure = logged_outage(exc, UNAVAILABLE_MESSAGE)
    if missing:
        not_found()
    assert failure is not None
    raise failure


def _chat_turns_today(messages: MessageStore) -> int:
    """The chat half of the pilot day, read before T1 opens. Fails closed."""
    try:
        return messages.calls_today()
    except Exception as exc:
        log.warning("tool route: the day's count is unavailable (%s)", type(exc).__name__)
    raise _unavailable()


def _is_invocation_id(value: str) -> bool:
    try:
        _INVOCATION_ID.validate_python(value)
    except ValidationError:
        return False
    return True


def _shaped(campaign_id: str, invocation_id: str | None = None) -> None:
    """A path id outside its shape is the one 404, before any query (L-18)."""
    if not ident.is_id(ident.CAMPAIGN, campaign_id) or (
        invocation_id is not None and not _is_invocation_id(invocation_id)
    ):
        not_found()


# ── The routes ───────────────────────────────────────────────────────────────


def build_router(
    gm: SessionDependency,
    database: Callable[[], TransactionalDatabase | None],
    messages: Callable[[], MessageStore | None],
) -> APIRouter:
    """The three routes on a `workbench_router`, given the app's GM gate, its
    database getter and its message store getter (the pilot day's chat half)."""
    router = workbench_router(gm)

    @router.post("/campaigns/{campaign_id}/tool-invocations", response_model=ToolInvocation)
    def create(
        campaign_id: str,
        request: Request,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_tool_body),
        stores: InvocationStores = Depends(get_invocation_stores),
        executors: Mapping[ToolId, ToolExecutor] = Depends(get_tool_executors),
        settings: ToolSettings = Depends(get_tool_settings),
        db: TransactionalDatabase | None = Depends(database),
        store: MessageStore | None = Depends(messages),
        clock: Clock = Depends(get_clock),
        factory: ProviderClientFactory = Depends(get_provider_factory),
    ) -> ToolInvocation:
        body = parse_body(ToolInvocationRequest, raw)
        if body.campaign_id != campaign_id:
            raise invalid("campaign_id")
        if db is None or store is None:
            raise _unavailable()
        _shaped(campaign_id)
        chat_turns = _chat_turns_today(store)
        admitted = _answered(lambda: submit(
            db, stores, executors, settings, user, body, now=clock(), chat_turns_today=chat_turns,
        ))
        if isinstance(admitted, Replay):
            return admitted.invocation
        executor = executors[admitted.target.tool_id]
        ctx = ExecutionContext(
            admitted, clock=clock, factory=factory, probe=cancellation_probe(db, stores, admitted, clock),
        )
        # After T1 committed and outside the try, as `/chat` does: it cannot
        # raise, and `end_operation` flushes the ledger rows on every path.
        token = usage_capture.begin_operation(
            mode=ChatMode.gm.value, billed_account_id=user.user_id, actor_kind=usage_capture.ACTOR_ACCOUNT,
            campaign_id=campaign_id, request=request, operation=usage_capture.OPERATION_TOOL_INVOCATION,
            operation_id=admitted.operation_id,
        )
        try:
            outcome = execute(
                executor, ctx, available=lambda tool: tool_availability(tool, settings, executors) is None,
            )
            return _answered(lambda: complete(db, stores, executor, admitted, outcome, ctx, now=clock()))
        finally:
            usage_capture.end_operation(token)

    @router.get("/campaigns/{campaign_id}/tool-invocations/{invocation_id}", response_model=ToolInvocation)
    def status(
        campaign_id: str,
        invocation_id: str,
        user: SessionData = Depends(gm),
        stores: InvocationStores = Depends(get_invocation_stores),
        db: TransactionalDatabase | None = Depends(database),
        clock: Clock = Depends(get_clock),
    ) -> ToolInvocation:
        if db is None:
            raise _unavailable()
        _shaped(campaign_id, invocation_id)
        return _answered(lambda: read(db, stores, user.user_id, campaign_id, invocation_id, now=clock()))

    @router.post("/campaigns/{campaign_id}/tool-invocations/{invocation_id}/cancel", response_model=ToolInvocation)
    def cancel_invocation(
        campaign_id: str,
        invocation_id: str,
        user: SessionData = Depends(gm),
        stores: InvocationStores = Depends(get_invocation_stores),
        db: TransactionalDatabase | None = Depends(database),
        clock: Clock = Depends(get_clock),
    ) -> ToolInvocation:
        """Any body is ignored; the origin check still refuses a non-JSON one."""
        if db is None:
            raise _unavailable()
        _shaped(campaign_id, invocation_id)
        return _answered(lambda: cancel(db, stores, user.user_id, campaign_id, invocation_id, now=clock()))

    return router

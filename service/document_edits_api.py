"""The GM's create, read and cancel routes for AI document edits."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import TypeAdapter, ValidationError

import config

from . import campaign_identity as ident
from . import document_edits, usage_capture
from .campaign_store import CampaignStoreError, PostgresCampaignStore
from .campaigns_api import invalid, logged_outage, parse_body
from .conversation_store import PostgresConversationStore
from .db import CampaignAuthzMissing, TransactionalDatabase, UnitOfWork
from .document_edit_store import DocumentEditStoreError, PostgresDocumentEditStore
from .document_edits import UNAVAILABLE, EditAdmission, EditInvariantBroken, EditStores
from .document_store import PostgresDocumentStore
from .models import ChatMode
from .providers import ProviderClientFactory
from .session import SessionData
from .timeline_store import PostgresTimelineStore, TimelineStoreError
from .tool_invocations import Clock, NotFound, Refused, Unreadable
from .usage_ledger import PostgresUsageLedgerStore, UsageLedgerStore, utc_midnight
from .workbench_api import SessionDependency, body_reader, not_found, workbench_router
from .workbench_contracts import EditInvocation, EditRequest, ErrorBody, ErrorCode, InvocationId
from .workbench_load import PostgresWorkbenchLoad

EDIT_BODY_MAX_BYTES = 294_912
read_edit_body = body_reader(EDIT_BODY_MAX_BYTES)
_FACTORY = ProviderClientFactory()
_KEY: TypeAdapter[str] = TypeAdapter(InvocationId)


def get_edit_stores() -> EditStores:
    return EditStores(
        PostgresDocumentEditStore(), PostgresCampaignStore(), PostgresConversationStore(),
        PostgresDocumentStore(), PostgresTimelineStore(), PostgresWorkbenchLoad(),
    )


def get_edit_settings() -> bool:
    return config.WORKBENCH_AI_EDITS_ENABLED


def get_edit_ledger() -> UsageLedgerStore:
    return PostgresUsageLedgerStore()


def get_clock() -> Clock:
    return lambda: datetime.now(UTC)


def get_provider_factory() -> ProviderClientFactory:
    return _FACTORY


def _http(refused: Refused) -> HTTPException:
    detail = ErrorBody(detail=refused.info).model_dump(mode="json", exclude_none=True)["detail"]
    wait = refused.info.retry_after_s
    return HTTPException(status_code=refused.status, detail=detail,
                         headers=None if wait is None else {"Retry-After": str(wait)})


def _unavailable() -> HTTPException:
    return _http(Refused(503, ErrorCode.BACKEND_UNAVAILABLE, UNAVAILABLE, retryable=True))


def _answered[T](step: Callable[[], T]) -> T:
    failure: HTTPException | None = None
    try:
        return step()
    except (NotFound, CampaignAuthzMissing):
        pass
    except Refused as exc:
        failure = _http(exc)
    except (psycopg.Error, DocumentEditStoreError, CampaignStoreError, TimelineStoreError,
            Unreadable, EditInvariantBroken, ValidationError) as exc:
        failure = logged_outage(exc, UNAVAILABLE)
    if failure is None:
        not_found()
    raise failure


def _shape(campaign_id: str, document_id: str, invocation_id: str | None = None) -> None:
    if not ident.is_id(ident.CAMPAIGN, campaign_id) or not ident.is_id(ident.DOCUMENT, document_id):
        not_found()
    if invocation_id is not None:
        try:
            _KEY.validate_python(invocation_id)
        except ValidationError:
            not_found()


def _chat_turns(ledger: UsageLedgerStore, unit: UnitOfWork, now: datetime) -> int:
    try:
        return ledger.chat_turns_since(unit, since=utc_midnight(now))
    except Exception as exc:
        failure = logged_outage(exc, UNAVAILABLE)
    raise failure


def build_router(
    gm: SessionDependency, database: Callable[[], TransactionalDatabase | None],
) -> APIRouter:
    router = workbench_router(gm)
    base = "/campaigns/{campaign_id}/documents/{document_id}/edits"

    @router.post(base, response_model=EditInvocation)
    def create(
        campaign_id: str, document_id: str, request: Request, user: SessionData = Depends(gm),
        raw: bytes = Depends(read_edit_body), stores: EditStores = Depends(get_edit_stores),
        enabled: bool = Depends(get_edit_settings), db: TransactionalDatabase | None = Depends(database),
        ledger: UsageLedgerStore = Depends(get_edit_ledger), clock: Clock = Depends(get_clock),
        factory: ProviderClientFactory = Depends(get_provider_factory),
    ) -> EditInvocation:
        body = parse_body(EditRequest, raw)
        if body.campaign_id != campaign_id:
            raise invalid("campaign_id")
        if body.document_id != document_id:
            raise invalid("document_id")
        if db is None:
            raise _unavailable()
        _shape(campaign_id, document_id)
        admitted = _answered(lambda: document_edits.submit(
            db, stores, user, body, enabled=enabled, clock=clock,
            chat_turns_today=lambda unit, now: _chat_turns(ledger, unit, now),
        ))
        if not isinstance(admitted, EditAdmission):
            return admitted
        token = usage_capture.begin_operation(
            mode=ChatMode.gm.value, billed_account_id=user.user_id, actor_kind=usage_capture.ACTOR_ACCOUNT,
            campaign_id=campaign_id, request=request, operation=usage_capture.OPERATION_DOCUMENT_EDIT,
            operation_id=admitted.operation_id,
        )
        try:
            outcome = document_edits.execute_edit(db, stores, admitted, factory, clock)
            return _answered(lambda: document_edits.complete(db, stores, admitted, outcome, clock=clock))
        finally:
            usage_capture.end_operation(token)

    def answer(
        campaign_id: str, document_id: str, invocation_id: str, user: SessionData,
        stores: EditStores, db: TransactionalDatabase | None, clock: Clock, *, cancel: bool,
    ) -> EditInvocation:
        if db is None:
            raise _unavailable()
        _shape(campaign_id, document_id, invocation_id)
        return _answered(lambda: document_edits.read(
            db, stores, user.user_id, campaign_id, document_id, invocation_id, clock=clock, cancel=cancel,
        ))

    @router.get(base + "/{invocation_id}", response_model=EditInvocation)
    def status(
        campaign_id: str, document_id: str, invocation_id: str, user: SessionData = Depends(gm),
        stores: EditStores = Depends(get_edit_stores), db: TransactionalDatabase | None = Depends(database),
        clock: Clock = Depends(get_clock),
    ) -> EditInvocation:
        return answer(campaign_id, document_id, invocation_id, user, stores, db, clock, cancel=False)

    @router.post(base + "/{invocation_id}/cancel", response_model=EditInvocation)
    def cancel_invocation(
        campaign_id: str, document_id: str, invocation_id: str, user: SessionData = Depends(gm),
        stores: EditStores = Depends(get_edit_stores), db: TransactionalDatabase | None = Depends(database),
        clock: Clock = Depends(get_clock),
    ) -> EditInvocation:
        return answer(campaign_id, document_id, invocation_id, user, stores, db, clock, cancel=True)

    return router

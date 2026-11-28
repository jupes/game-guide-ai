"""Owner-scoped AI document edit admission, execution and atomic completion."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

import config

from .campaign_store import CampaignStore
from .campaigns_api import ARCHIVED_MESSAGE
from .conversation_store import ConversationStore
from .db import AdvisoryLock, TransactionalDatabase, UnitOfWork
from .document_edit_store import ATTEMPT_MAX, DocumentEditStore, EditNotStored, EditRow, NewEdit
from .document_editing import (
    EditProposal,
    EditRefusal,
    EditRefused,
    EditTarget,
    InvalidEdit,
    NoChange,
    ai_editable_keys,
    check_scope,
    edit_document,
    edit_prose,
    edit_version_summary,
    no_change_prose,
    scope_keys,
)
from .document_generation import attempts_that_fit
from .document_store import (
    ByteQuota,
    DocumentRecord,
    DocumentStore,
    FieldConflict,
    StaleTypeVersion,
    StorageQuotaReached,
    stale_fields,
)
from .document_wire import DocumentUnsupported, stored_json, writable
from .model_catalog import workbench_profile
from .providers import ProviderClientFactory
from .session import SessionData
from .timeline_store import EditTurnStore, EntryNotStored, TimelineStore, new_entry_id
from .tool_invocations import (
    ATTEMPT_EXPIRED_MESSAGE,
    ATTEMPT_TTL_S,
    STORAGE_CAP_MESSAGE,
    Clock,
    InvocationCancelled,
    NotFound,
    OutOfTime,
    Refused,
    Unreadable,
    admit_attempt,
    bounded_client,
    failure_for,
    resolve_tool_model,
)
from .usage_capture import run_config_with_operation
from .workbench_contracts import (
    CONTRACT_VERSION,
    DOC_TYPE_LIBRARY_CATEGORY,
    Author,
    ConflictInfo,
    DocumentLink,
    DocumentTypeId,
    EditChanged,
    EditEntry,
    EditInstruction,
    EditInvocation,
    EditNoChange,
    EditRequest,
    EditScope,
    EditScopeSummary,
    ErrorCode,
    ErrorInfo,
    InvocationStatus,
    SchemaVersion,
    SelectionScope,
    check_fields,
    validation_error_body,
)
from .workbench_load import WorkbenchLoad, bound_transaction
from .workbench_registry import REGISTRY

log = logging.getLogger(__name__)
DISABLED = "Editing with the assistant isn't available yet."
UNAVAILABLE = "The assistant is briefly unavailable. Try again."
UNSUPPORTED = "This document can't be used by this version of Aetheril."
INVALID_OUTPUT = "The assistant couldn't finish that. Try again."
FINAL_PROVIDER = "The assistant couldn't do that one. Change the instruction and try again."
CONFLICT = "The document changed while the assistant was working. Nothing was overwritten."
SELECTION_MOVED = "The selected text has changed. Select it again."

_SCOPE: TypeAdapter[EditScope] = TypeAdapter(EditScope)
_SCOPE_SUMMARY: TypeAdapter[EditScopeSummary] = TypeAdapter(EditScopeSummary)
_INSTRUCTION: TypeAdapter[EditInstruction] = TypeAdapter(EditInstruction)
WIRE_VERSION = cast("SchemaVersion", CONTRACT_VERSION)


class EditTimeline(TimelineStore, EditTurnStore, Protocol):
    """The timeline operations needed for an edit lane."""


@dataclass(frozen=True)
class EditStores:
    edits: DocumentEditStore
    campaigns: CampaignStore
    conversations: ConversationStore
    documents: DocumentStore
    timeline: EditTimeline
    load: WorkbenchLoad


@dataclass(frozen=True)
class EditAdmission:
    row: EditRow = field(repr=False)
    target: EditTarget = field(repr=False)
    operation_id: str
    alias: str = field(repr=False)


class EditInvariantBroken(Exception):
    """The proposed change could not be confined to the admitted scope."""


class _AdmissionLost(Exception):
    """A guarded write lost its admission after the ownership checks."""


class _CompletionExpired(Exception):
    """Roll back a document and timeline write that crossed its attempt's deadline."""


def to_wire(row: EditRow) -> EditInvocation:
    try:
        return EditInvocation.model_validate({
            "schema_version": row.schema_version, "invocation_id": row.invocation_id, "document_id": row.document_id,
            "status": row.status, "attempt": row.attempt, "cancel_requested": row.cancel_requested,
            "created_at": row.created_at, "updated_at": row.updated_at, "result": row.result, "error": row.error,
        })
    except ValidationError:
        raise Unreadable() from None


def _instruction(row: NewEdit) -> EditInstruction:
    if row.instruction_kind == "text":
        return _INSTRUCTION.validate_python({"kind": "text", "text": row.instruction_text})
    return _INSTRUCTION.validate_python({"kind": row.instruction_kind, "action": row.instruction_action})


def _scope(row: NewEdit) -> EditScope:
    info: dict[str, object] = {"kind": row.scope_kind}
    if row.scope_kind != "document":
        info["field"] = row.scope_field
    if row.scope_kind == "selection":
        info.update(start=row.selection_start, end=row.selection_end, text=row.selection_text)
    return _SCOPE.validate_python(info)


def edit_entry(row: NewEdit, invocation: EditInvocation) -> EditEntry:
    try:
        doc_type = DocumentTypeId(row.document_type)
        summary = {"kind": row.scope_kind} if row.scope_kind == "document" else {
            "kind": row.scope_kind, "field": row.scope_field,
        }
        return EditEntry(
            schema_version=WIRE_VERSION, entry_id=row.entry_id, created_at=invocation.created_at,
            entry_kind="edit", scope=_SCOPE_SUMMARY.validate_python(summary), instruction=_instruction(row),
            document=DocumentLink(document_id=row.document_id, type=doc_type, title=row.document_title,
                                  library_category=DOC_TYPE_LIBRARY_CATEGORY[doc_type]),
            invocation=invocation,
        )
    except ValueError:
        raise Unreadable() from None


def _rewrite(unit: UnitOfWork, stores: EditStores, row: EditRow) -> EditInvocation:
    invocation = to_wire(row)
    stores.timeline.replace_edit_invocation(
        unit, row.conversation_id, edit_entry(row, invocation), row.created_at, owner_id=row.owner_id,
    )
    return invocation


def _settle(
    unit: UnitOfWork, stores: EditStores, row: EditRow, *, now: datetime,
    result: EditChanged | EditNoChange | None = None, error: ErrorInfo | None = None, expired: bool = False,
) -> EditRow:
    if error is not None and row.attempt >= ATTEMPT_MAX:
        error = error.model_copy(update={"retryable": False})
    status = "done" if result is not None else "failed" if error is not None else "cancelled"
    updated = stores.edits.settle(
        unit, row.owner_id, row.campaign_id, row.invocation_id, attempt=row.attempt, status=status,
        outcome="expired" if expired else status, result=None if result is None else result.model_dump(mode="json"),
        error=None if error is None else error.model_dump(mode="json", exclude_none=True),
        forget_selection=error is None or not error.retryable, now=now,
    )
    _rewrite(unit, stores, updated)
    return updated


def _row(unit: UnitOfWork, stores: EditStores, owner: int, campaign: str, document: str, key: str) -> EditRow:
    row = stores.edits.get_for_update(unit, owner, campaign, key)
    if row is None or row.document_id != document:
        raise NotFound()
    to_wire(row)
    return row


def _expire(unit: UnitOfWork, stores: EditStores, row: EditRow, now: datetime) -> EditRow:
    if row.status != "working" or row.attempt_deadline > now:
        return row
    error = None if row.cancel_requested else ErrorInfo(
        code=ErrorCode.ATTEMPT_EXPIRED, message=ATTEMPT_EXPIRED_MESSAGE, retryable=True,
    )
    return _settle(unit, stores, row, now=now, error=error, expired=True)


def _invalid(field_name: str | None = None) -> Refused:
    errors = [] if field_name is None else [{"type": "value_error", "loc": (field_name,)}]
    info = validation_error_body(errors).detail
    refusal = Refused(422, info.code, info.message)
    refusal.info = info
    return refusal


def _conflict(revision: int, keys: list[str], message: str) -> ErrorInfo:
    return ErrorInfo(code=ErrorCode.CONFLICT, message=message, retryable=True,
                     conflict=ConflictInfo(write_revision=revision, fields=keys))


def _refuse_conflict(record: DocumentRecord, keys: list[str], scope: EditScope) -> Refused:
    message = SELECTION_MOVED if isinstance(scope, SelectionScope) else "This document changed elsewhere."
    refusal = Refused(409, ErrorCode.CONFLICT, message, retryable=True)
    refusal.info = _conflict(record.write_revision, keys, message)
    return refusal


def _new_edit(request: EditRequest, record: DocumentRecord, data: dict[str, object]) -> NewEdit:
    scope, instruction = request.scope, request.instruction
    return NewEdit(
        request.conversation_id, new_entry_id(), record.id, record.type, str(data["name"]), scope.kind,
        None if scope.kind == "document" else scope.field, scope.start if scope.kind == "selection" else None,
        scope.end if scope.kind == "selection" else None, scope.text if scope.kind == "selection" else None,
        instruction.kind, instruction.text if instruction.kind == "text" else None,
        instruction.action.value if instruction.kind == "action" else None, request.base_write_revision,
    )


def _target(record: DocumentRecord, scope: EditScope, instruction: EditInstruction, base: int) -> EditTarget:
    if base > record.write_revision:
        raise _invalid("base_write_revision")
    try:
        data = stored_json(writable(record))
        kind = DocumentTypeId(record.type)
    except (DocumentUnsupported, StaleTypeVersion):
        raise Refused(409, ErrorCode.DOCUMENT_UNSUPPORTED, UNSUPPORTED) from None
    try:
        check_fields(kind, record.type_version, data, whole=True)
    except ValueError:
        raise _invalid() from None
    target = EditTarget(kind, record.type_version, data, scope, instruction)
    problem = check_scope(target)
    if problem is EditRefusal.SELECTION_MISMATCH and isinstance(scope, SelectionScope):
        raise _refuse_conflict(record, [scope.field], scope)
    if problem is not None and problem is not EditRefusal.TOO_LARGE:
        raise _invalid("scope")
    moved = stale_fields(record, base, scope_keys(kind, scope))
    if moved:
        raise _refuse_conflict(record, moved, scope)
    if problem is EditRefusal.TOO_LARGE:
        raise _invalid("scope")
    return target


def _submit(
    db: TransactionalDatabase, stores: EditStores, session: SessionData, request: EditRequest, *,
    enabled: bool, clock: Clock, chat_turns_today: int | Callable[[UnitOfWork, datetime], int],
) -> EditAdmission | EditInvocation:
    owner, campaign, document, key = session.user_id, request.campaign_id, request.document_id, request.invocation_id
    with db.transaction() as unit:
        stores.load.hold_in_flight_lock(unit, owner)
        parent = stores.campaigns.get(unit, campaign, owner_id=owner)
        if parent is None:
            raise NotFound()
        linked = stores.conversations.campaign_for_owner(unit, request.conversation_id, owner_id=owner)
        if linked != campaign or stores.documents.get(unit, campaign, document) is None:
            raise NotFound()
        existing = stores.edits.get_for_update(unit, owner, campaign, key)
        if existing is not None:
            if existing.document_id != document or existing.conversation_id != request.conversation_id:
                raise NotFound()
            to_wire(existing)
            existing = _expire(unit, stores, existing, clock())
            stored_invocation = to_wire(existing)
            error = stored_invocation.error
            if error is None or not error.retryable or existing.attempt >= ATTEMPT_MAX:
                return stored_invocation
        if not enabled:
            raise Refused(409, ErrorCode.EDIT_DISABLED, DISABLED)
        if parent.archived_at is not None:
            raise Refused(409, ErrorCode.CAMPAIGN_ARCHIVED, ARCHIVED_MESSAGE)
        alias = resolve_tool_model(session)
        if workbench_profile(alias) is None:
            raise Refused(409, ErrorCode.EDIT_DISABLED, DISABLED)
        record = stores.documents.hold(unit, campaign, document)
        if record is None:
            raise NotFound()
        try:
            scope = request.scope if existing is None else _scope(existing)
            instruction = request.instruction if existing is None else _instruction(existing)
        except ValidationError:
            raise Unreadable() from None
        base = request.base_write_revision
        target = _target(record, scope, instruction, base)
        now = clock()
        running = stores.edits.in_flight_for_document(unit, owner, campaign, document, now=now)
        if running:
            raise Refused(409, ErrorCode.CAP_REACHED, "The assistant is already editing this document.",
                          in_flight=running[:1], retryable=True)
        admit_attempt(unit, stores.load, owner, now=now, chat_turns_today=chat_turns_today)
        operation_id, deadline = uuid4().hex, now + timedelta(seconds=ATTEMPT_TTL_S)
        if existing is None:
            new = _new_edit(request, record, dict(target.data))
            working = EditInvocation(
                schema_version=WIRE_VERSION, invocation_id=key, document_id=document,
                status=InvocationStatus.WORKING, attempt=1, cancel_requested=False, created_at=now, updated_at=now,
                result=None, error=None,
            )
            try:
                stores.timeline.append(unit, new.conversation_id, edit_entry(new, working), now, owner_id=owner)
                row = stores.edits.create(
                    unit, owner_id=owner, campaign_id=campaign, invocation_id=key, new=new, operation_id=operation_id,
                    attempt_deadline=deadline, now=now, schema_version=CONTRACT_VERSION,
                )
            except (EntryNotStored, EditNotStored):
                raise _AdmissionLost() from None
        else:
            try:
                row = stores.edits.start_retry(
                    unit, owner_id=owner, campaign_id=campaign, invocation_id=key, attempt=existing.attempt,
                    base_write_revision=base, operation_id=operation_id, attempt_deadline=deadline, now=now,
                )
            except EditNotStored:
                raise _AdmissionLost() from None
            _rewrite(unit, stores, row)
        if stores.documents.seal(unit, campaign, document, now=now) is None:
            raise NotFound()
        return EditAdmission(row, target, operation_id, alias)


def submit(
    db: TransactionalDatabase, stores: EditStores, session: SessionData, request: EditRequest, *,
    enabled: bool, clock: Clock, chat_turns_today: int | Callable[[UnitOfWork, datetime], int],
) -> EditAdmission | EditInvocation:
    try:
        return _submit(db, stores, session, request, enabled=enabled, clock=clock, chat_turns_today=chat_turns_today)
    except _AdmissionLost:
        # The guarded insert/retry refused. Its timeline write has rolled back.
        with db.transaction() as unit:
            parent = stores.campaigns.get(unit, request.campaign_id, owner_id=session.user_id)
            if parent is None:
                raise NotFound() from None
            if parent.is_archived:
                raise Refused(409, ErrorCode.CAMPAIGN_ARCHIVED, ARCHIVED_MESSAGE) from None
            row = _row(unit, stores, session.user_id, request.campaign_id, request.document_id, request.invocation_id)
            if row.conversation_id != request.conversation_id:
                raise NotFound() from None
            return to_wire(_expire(unit, stores, row, clock()))


type EditOutcome = EditProposal | NoChange | ErrorInfo | None


def _document_gone() -> ErrorInfo:
    return ErrorInfo(code=ErrorCode.NOT_FOUND, message="This document isn't available.", retryable=False)


def _selection_was_deleted(working: EditRow) -> bool:
    # Only a permanent delete clears a selection while its edit is still working.
    return working.scope_kind == "selection" and working.selection_text is None


def execute_edit(
    db: TransactionalDatabase, stores: EditStores, admission: EditAdmission,
    factory: ProviderClientFactory, clock: Clock,
) -> EditOutcome:
    row = admission.row

    def remaining() -> float:
        return (row.attempt_deadline - clock()).total_seconds()

    def probe() -> None:
        with db.transaction() as unit:
            active = stores.edits.fence(unit, row.owner_id, row.campaign_id, row.invocation_id,
                                        attempt=row.attempt, now=clock())
        if active is None or active.cancel_requested:
            raise InvocationCancelled()
        if _selection_was_deleted(active):
            raise NotFound()

    try:
        probe()
        attempts = attempts_that_fit(remaining())
        if attempts == 0:
            return failure_for(OutOfTime())
        return edit_document(
            admission.target, client=bounded_client(factory, admission.alias, remaining), alias=admission.alias,
            config=run_config_with_operation({}), max_attempts=attempts, between_attempts=probe,
        )
    except InvocationCancelled:
        return None
    except NotFound:
        return _document_gone()
    except InvalidEdit:
        return ErrorInfo(code=ErrorCode.PROVIDER_FAILED, message=INVALID_OUTPUT, retryable=True)
    except EditRefused as exc:
        if exc.code is EditRefusal.DEADLINE:
            return failure_for(OutOfTime())
        log.warning("edit refused during execution (%s)", exc.code.value)
    except Exception as exc:
        error = failure_for(exc)
        if error.code is ErrorCode.PROVIDER_TIMEOUT:
            return error
        if error.code is ErrorCode.PROVIDER_FAILED:
            return error.model_copy(update={"message": INVALID_OUTPUT if error.retryable else FINAL_PROVIDER})
        log.warning("edit execution failed (%s)", type(exc).__name__)
    return ErrorInfo(code=ErrorCode.BACKEND_UNAVAILABLE, message=UNAVAILABLE, retryable=True)


def _apply(
    unit: UnitOfWork, stores: EditStores, row: EditRow, proposal: EditProposal, now: datetime,
) -> EditChanged | ErrorInfo:
    record = stores.documents.hold(unit, row.campaign_id, row.document_id)
    if record is None:
        return _document_gone()
    try:
        data = writable(record)
        kind = DocumentTypeId(record.type)
    except (DocumentUnsupported, StaleTypeVersion):
        return ErrorInfo(code=ErrorCode.DOCUMENT_UNSUPPORTED, message=UNSUPPORTED, retryable=False)
    scope = _scope(row)
    keys = set(proposal.fields)
    if (not keys or set(proposal.changed) != keys or not keys <= scope_keys(kind, scope)
            or not keys.issubset(ai_editable_keys(kind))):
        raise EditInvariantBroken()
    moved = stale_fields(record, row.base_write_revision, proposal.fields)
    if moved:
        return _conflict(record.write_revision, moved, CONFLICT)
    if isinstance(scope, SelectionScope):
        prior, following = data.get(scope.field), proposal.fields.get(scope.field)
        if not isinstance(prior, str) or not isinstance(following, str):
            raise EditInvariantBroken()
        prefix, suffix = prior[:scope.start], prior[scope.end:]
        if (prior[scope.start:scope.end] != scope.text or not following.startswith(prefix)
                or not following.endswith(suffix) or len(following) <= len(prefix) + len(suffix)):
            raise EditInvariantBroken()
    definition = REGISTRY.document_type(kind.value)
    if definition is None:
        raise EditInvariantBroken()
    labels = [REGISTRY.label_for(definition, key) or key for key in ai_editable_keys(kind) if key in proposal.fields]
    try:
        written = stores.documents.write_fields(
            unit, row.campaign_id, row.document_id, fields=dict(proposal.fields), author=Author.ASSISTANT,
            base_write_revision=row.base_write_revision, summary=edit_version_summary(scope.kind, labels[0]), now=now,
            quota=ByteQuota(row.owner_id, config.WORKBENCH_DOCUMENT_BYTES_PER_ACCOUNT_MAX),
        )
    except FieldConflict as exc:
        return _conflict(exc.write_revision, list(exc.fields), CONFLICT)
    except StorageQuotaReached:
        return ErrorInfo(code=ErrorCode.ACCOUNT_LIMIT_REACHED, message=STORAGE_CAP_MESSAGE, retryable=False)
    except ValueError as exc:
        log.warning("edit merge refused (attempt=%d, class=%s)", row.attempt, type(exc).__name__)
        return ErrorInfo(code=ErrorCode.PROVIDER_FAILED, message=INVALID_OUTPUT, retryable=True)
    return EditChanged(
        outcome="changed", version_number=written.version.number, write_revision=written.write_revision,
        changed_fields=list(written.version.changed_fields), prose=edit_prose(scope.kind, labels), suggestions=[],
    )


def _finish(
    unit: UnitOfWork, stores: EditStores, row: EditRow, outcome: EditOutcome, now: datetime,
) -> EditInvocation:
    if _selection_was_deleted(row):
        outcome = _document_gone()
    elif isinstance(outcome, EditProposal):
        change = _apply(unit, stores, row, outcome, now)
        if isinstance(change, EditChanged):
            return to_wire(_settle(unit, stores, row, result=change, now=now))
        outcome = change
    if isinstance(outcome, NoChange):
        result = EditNoChange(outcome="no_change", prose=no_change_prose(), suggestions=[])
        return to_wire(_settle(unit, stores, row, result=result, now=now))
    error = None if row.cancel_requested else outcome
    return to_wire(_settle(unit, stores, row, error=error, now=now))


def complete(
    db: TransactionalDatabase, stores: EditStores, admission: EditAdmission, outcome: EditOutcome, *, clock: Clock,
) -> EditInvocation:
    target = admission.row
    try:
        with db.transaction() as unit:
            if isinstance(outcome, EditProposal):
                bound_transaction(unit)
                unit.lock(AdvisoryLock.ACCOUNT_STORAGE, str(target.owner_id))
            row = stores.edits.fence(unit, target.owner_id, target.campaign_id, target.invocation_id,
                                    attempt=target.attempt, now=clock())
            if row is None:
                row = _row(unit, stores, target.owner_id, target.campaign_id, target.document_id, target.invocation_id)
                return to_wire(_expire(unit, stores, row, clock()))
            now = clock()
            if row.attempt_deadline <= now:
                return to_wire(_expire(unit, stores, row, now))
            result = _finish(unit, stores, row, outcome, now)
            if row.attempt_deadline <= clock():
                # The document or timeline write waited under its own row lock.
                # Roll back both before settling the expired attempt.
                raise _CompletionExpired()
            return result
    except _CompletionExpired:
        return read(
            db, stores, target.owner_id, target.campaign_id, target.document_id, target.invocation_id, clock=clock,
        )


def read(
    db: TransactionalDatabase, stores: EditStores, owner: int, campaign: str, document: str, key: str, *,
    clock: Clock, cancel: bool = False,
) -> EditInvocation:
    with db.transaction() as unit:
        if stores.campaigns.get(unit, campaign, owner_id=owner) is None:
            raise NotFound()
        row = _row(unit, stores, owner, campaign, document, key)
        now = clock()
        row = _expire(unit, stores, row, now)
        if cancel:
            changed = stores.edits.set_cancel_requested(unit, owner, campaign, key, now=now)
            if changed is not None:
                return _rewrite(unit, stores, changed)
        return to_wire(row)

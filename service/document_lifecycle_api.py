"""A GM's document lifecycle: archive, unarchive and delete (bead 1kg.5.2, its
second pull request; agent-forge-harness-1kg.5.8).

Three Workbench routes on a `workbench_router` (`agent-forge-harness-oe6`),
wired into `service/app.py` and importing nothing from it: the application
hands `build_router` its GM gate, its database getter and its re-authentication
dependency. They sit beside `documents_api`'s eight in a module of their own so
that one sentence holds of each module: **nothing in `documents_api.py` takes
the campaign lock, narrows, advances the authorisation revision or writes an
audit row, and every change here does all four.**

| Route | Answers |
| --- | --- |
| `POST /campaigns/{campaign_id}/documents/{document_id}/archive` | `204` |
| `POST /campaigns/{campaign_id}/documents/{document_id}/unarchive` | `204` |
| `POST /campaigns/{campaign_id}/documents/{document_id}/delete` | `204`; the body is a `DocumentDeleteRequest` |

**Every answer is `204`, never a `Document`** (the brief's I-12): a GM must be
able to archive and delete a document this build cannot render, so nothing
here reads a stored document's content.

**Archive and delete are fact-changing narrowings** (RQ-5; LIB-17 as A-17
amends it; the shared ADR's RC-15), in two transactions:

1. *Step one* NEVER calls `lock_campaign`: the campaign with its owner in the
   statement (SEC-2), the document, and — for an archive of a document that is
   not archived yet, and for every delete — the campaign's live session
   narrowed (its row held, the reveal epoch advanced, and **that document's
   copies** cleared, as `document_archived` or `document_deleted`, stamped with
   the request's clock). It commits before step two asks for the lock, so the
   display is already stopped whatever happens next.
2. *Step two*, still in the request: the exclusive campaign lock FIRST, the
   ownership read again under it (L-18), the document's row held, the fact
   changed, the live session scanned for again — one started between the steps
   is narrowed too — the authorisation revision advanced and the audit row
   written, in RQ-3's order (`authz_state` → document row → session row). A
   delete narrows **before** it deletes: the document's disclosures go with it
   by the foreign key's cascade, and a slot still showing one would refuse the
   delete. If the lock cannot be had in time the answer is a retryable `503`
   whose message says the change is **not applied yet**; the GM's intent is
   never handed to a job.

**The scope is the document's copies** (`reveal_scope.DocumentCopies`; the
`1kg.7.1` brief's ID-5 and T-B12): archiving or deleting one document stops
every copy of it, the table's and each member's, and leaves every other
display up.

**Unarchive is a locked widening** (RQ-4, RQ-10), one transaction: the
ownership read and the document, and for a document that is not archived
nothing more — no lock. Otherwise the exclusive lock, the row, the change, the
advance and the audit row.

**Delete asks for the password** (SEC-40; the brief's I-13) BEFORE any
transaction opens — argon2 never runs while a lock is held — through the
application's re-authentication, which spends the same attempt budget a login
does. It deletes only an archived document (LIB-18): `409
document_not_archived` in step one, before anything narrows, and again under
the lock in step two.

**The order of checks** (SEC-3): origin (403) → authentication (the one 401) →
role (403) → the delete's body (422) → the database (503) → the delete's
password (429, 503 or 403) → the path ids' shapes (the one 404, before any
query) → ownership (the one 404) → the document (the one 404) → state (409).
So `document_not_archived` is reachable only by the document's owner, and a
document of the GM's other campaign under this campaign's path is the same 404
as a missing one.

**Idempotent**: the state a document is already in answers `204` with no
narrowing, no revision advance and no audit row (and, for unarchive, no lock).
A deleted document is a missing one, so a repeated delete is the one 404
(CANVAS-31). No route here consults the campaign's archived state (I-11).

**No private text anywhere.** Refusals are fixed sentences; an audit row names
the document by its id and nothing else; a log line carries an exception's TYPE
and SQLSTATE, never its message. No route here builds a 401, 403 or 404 of its
own.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from . import campaign_identity as ident
from .audit_log import ActorKind, AuditAction, AuditLog, Decision, ObjectKind, PostgresAuditLog
from .campaign_store import CampaignStore, MissingParent, PostgresCampaignStore
from .conversations_api import read_body
from .db import CampaignAuthzMissing, TransactionalDatabase, UnitOfWork
from .document_store import DocumentStore, PostgresDocumentStore
from .documents_api import UNAVAILABLE_MESSAGE, get_clock
from .reveal_scope import DocumentCopies, EndReason
from .reveal_store import PostgresRevealStore
from .reveals import slot_clear_for
from .session import SessionData
from .table_session_store import PostgresTableSessionStore, TableSessionStore
from .workbench_api import SessionDependency, not_found, workbench_router
from .workbench_contracts import DocumentDeleteRequest, ErrorBody, ErrorCode, ErrorInfo

log = logging.getLogger(__name__)

#: Fixed sentences. A refusal never interpolates anything it was sent (X-7).
NOT_APPLIED_MESSAGE = "That change isn't applied yet. Try again."
NOT_ARCHIVED_MESSAGE = "Archive this document before deleting it."

#: The two SQLSTATEs a locked write answers as "busy, try again" (RQ-8):
#: `lock_not_available` and `deadlock_detected`.
_BUSY = (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected)

#: What the re-authentication dependency hands the delete route: a check of one
#: password against the account this request signed in as, which raises the
#: answer or returns.
type PasswordCheck = Callable[[str], None]


# ── What the routes compose ─────────────────────────────────────────────────


@dataclass(frozen=True)
class LifecycleStores:
    """The stores a lifecycle route composes."""

    campaigns: CampaignStore
    documents: DocumentStore
    sessions: TableSessionStore
    audit: AuditLog


def get_lifecycle_stores() -> LifecycleStores:
    """The stores every route here uses. Tests override this dependency. The
    session store's slot clear is the reveal fill (`reveals.slot_clear_for`),
    as every production session store's is, so a narrowing here clears the
    copies its scope names."""
    return LifecycleStores(
        PostgresCampaignStore(),
        PostgresDocumentStore(),
        PostgresTableSessionStore(slot_clear=slot_clear_for(PostgresRevealStore())),
        PostgresAuditLog(),
    )


# ── Refusals ─────────────────────────────────────────────────────────────────
# The 401, the 403s and the 404 are the scaffolding's (`workbench_api`); the
# password's 429, 503 and 403 are the application's re-authentication.


def _refusal(status: int, code: ErrorCode, message: str, *, retryable: bool = False) -> HTTPException:
    info = ErrorInfo(code=code, message=message, retryable=retryable)
    body = ErrorBody(detail=info).model_dump(mode="json", exclude_none=True)
    return HTTPException(status_code=status, detail=body["detail"])


def _unavailable(message: str = UNAVAILABLE_MESSAGE) -> HTTPException:
    return _refusal(503, ErrorCode.BACKEND_UNAVAILABLE, message, retryable=True)


def _not_archived() -> HTTPException:
    return _refusal(409, ErrorCode.DOCUMENT_NOT_ARCHIVED, NOT_ARCHIVED_MESSAGE)


def _parse[M: BaseModel](model: type[M], raw: bytes) -> M:
    """The body as the contract reads it, or the Workbench 422. The error list
    handed on carries no input — here that is the password — and it is raised
    outside the `except`, so it chains nothing."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
    raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors])


def _logged_outage(exc: BaseException, message: str) -> HTTPException:
    """The exception's TYPE and SQLSTATE only: its message can quote a row."""
    sqlstate = getattr(exc, "sqlstate", None)
    log.warning("document route: database unavailable (%s%s)", type(exc).__name__, f", {sqlstate}" if sqlstate else "")
    return _unavailable(message)


def guarded[T](work: Callable[[], T], *, busy_message: str = UNAVAILABLE_MESSAGE) -> T:
    """Run one transaction and map what the database said.

    `CampaignAuthzMissing` — the campaign went between the ownership read and
    the lock — and a `MissingParent` from an unlocked read of a document
    deleted between its two statements are the one 404 (CANVAS-31). A lock
    timeout or a deadlock victim is a retryable 503 carrying `busy_message`:
    step two's is *not applied yet* (RC-15). Any other driver error is the
    generic 503. The transaction has already rolled back by then, and each
    answer is raised OUTSIDE the `except`, so nothing is chained.
    """
    failure: HTTPException | None = None
    gone = False
    try:
        return work()
    except (CampaignAuthzMissing, MissingParent):
        gone = True
    except _BUSY as exc:
        failure = _logged_outage(exc, busy_message)
    except psycopg.Error as exc:
        failure = _logged_outage(exc, UNAVAILABLE_MESSAGE)
    if gone:
        not_found()
    assert failure is not None
    raise failure


def _owned(stores: LifecycleStores, unit: UnitOfWork, campaign_id: str, owner_id: int) -> None:
    """The campaign with its owner in the statement (SEC-2), or the one 404: the
    first store call of every transaction here, and the first after the lock in
    a transaction that takes it first."""
    if stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
        not_found()


def _narrow_live(
    stores: LifecycleStores, unit: UnitOfWork, campaign_id: str, *, clears: DocumentCopies, now: datetime | None
) -> None:
    """Narrow the campaign's live session, if it has one: its row held, its
    reveal epoch advanced, and the copies `clears` names cleared, stamped with
    `now`."""
    live = stores.sessions.live_session_for_campaign(unit, campaign_id)
    if live is not None:
        stores.sessions.narrow(unit, campaign_id, live.id, clears=clears, now=now)


def _audit(
    stores: LifecycleStores,
    unit: UnitOfWork,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    action: AuditAction,
    revision: int,
    now: datetime,
) -> None:
    """SEC-38's row: the owner, the document by its id, the revision the change
    advanced to — and no name, field value, summary or type label."""
    stores.audit.append(
        unit,
        campaign_id=campaign_id,
        actor_kind=ActorKind.GM,
        action=action,
        object_kind=ObjectKind.DOCUMENT,
        decision=Decision.ALLOWED,
        actor_ref=str(owner_id),
        object_ref=document_id,
        authz_revision=revision,
        detail={"document_id": document_id},
        now=now,
    )


# ── The compositions: one transaction each ──────────────────────────────────


def archive_step_one(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime | None = None,
) -> None:
    """Never calls `lock_campaign`, and is never refused on state: ownership,
    the document, and — unless it is archived already — the live session
    narrowed (that document's copies, as `document_archived`, stamped with the
    request's clock). Committed before step two asks for the lock."""
    with db.transaction() as unit:
        _owned(stores, unit, campaign_id, owner_id)
        found = stores.documents.get(unit, campaign_id, document_id)
        if found is None:
            not_found()
        if not found.is_archived:
            _narrow_live(
                stores, unit, campaign_id, clears=DocumentCopies(document_id, EndReason.DOCUMENT_ARCHIVED), now=now
            )


def archive_step_two(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime,
) -> None:
    """The exclusive lock first, ownership again under it, the document's row
    held; an archived document changes nothing. Otherwise archive it, narrow a
    live session again (one started since step one; that document's copies, as
    `document_archived`), advance the revision and record `document.archived`."""
    with db.transaction() as unit:
        unit.lock_campaign(campaign_id, shared=False)
        _owned(stores, unit, campaign_id, owner_id)
        held = stores.documents.hold(unit, campaign_id, document_id)
        if held is None:
            not_found()
        if held.is_archived:
            return
        stores.documents.set_archived(unit, campaign_id, document_id, archived=True, now=now)
        _narrow_live(
            stores, unit, campaign_id, clears=DocumentCopies(document_id, EndReason.DOCUMENT_ARCHIVED), now=now
        )
        revision = unit.advance_authz_revision(campaign_id)
        _audit(stores, unit, campaign_id=campaign_id, document_id=document_id, owner_id=owner_id,
               action=AuditAction.DOCUMENT_ARCHIVED, revision=revision, now=now)


def unarchive_document(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime,
) -> None:
    """A locked widening. Ownership and the document by plain reads, and for a
    document that is not archived nothing more — no lock is taken. Otherwise the
    exclusive lock (the first lock of the transaction: both reads took none),
    the row, the change, the advance and `document.unarchived`."""
    with db.transaction() as unit:
        _owned(stores, unit, campaign_id, owner_id)
        found = stores.documents.get(unit, campaign_id, document_id)
        if found is None:
            not_found()
        if not found.is_archived:
            return
        unit.lock_campaign(campaign_id, shared=False)
        held = stores.documents.hold(unit, campaign_id, document_id)
        if held is None:
            not_found()
        if not held.is_archived:
            return
        stores.documents.set_archived(unit, campaign_id, document_id, archived=False, now=now)
        revision = unit.advance_authz_revision(campaign_id)
        _audit(stores, unit, campaign_id=campaign_id, document_id=document_id, owner_id=owner_id,
               action=AuditAction.DOCUMENT_UNARCHIVED, revision=revision, now=now)


def delete_step_one(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime | None = None,
) -> None:
    """Never calls `lock_campaign`: ownership, the document, `409
    document_not_archived` for one that is not archived — before anything
    narrows — and then the live session narrowed: that document's copies, as
    `document_deleted`, stamped with the request's clock (the shared ADR lists
    delete among the fact-changing narrowings, although an archived document is
    shown nowhere; the brief's I-16)."""
    with db.transaction() as unit:
        _owned(stores, unit, campaign_id, owner_id)
        found = stores.documents.get(unit, campaign_id, document_id)
        if found is None:
            not_found()
        if not found.is_archived:
            raise _not_archived()
        _narrow_live(stores, unit, campaign_id, clears=DocumentCopies(document_id, EndReason.DOCUMENT_DELETED), now=now)


def delete_step_two(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime,
) -> None:
    """The exclusive lock first, ownership again under it, the document's row
    held and found archived still; then a live session narrowed again (that
    document's copies, as `document_deleted`) **before** the document and its
    whole history are deleted — its disclosures go with it by the cascade, and
    a slot still showing one would refuse the delete (the slot -> disclosure
    key has no delete action) — the revision advanced and `document.deleted`
    recorded, the row that outlives the document."""
    with db.transaction() as unit:
        unit.lock_campaign(campaign_id, shared=False)
        _owned(stores, unit, campaign_id, owner_id)
        held = stores.documents.hold(unit, campaign_id, document_id)
        if held is None:
            not_found()
        if not held.is_archived:
            raise _not_archived()
        _narrow_live(stores, unit, campaign_id, clears=DocumentCopies(document_id, EndReason.DOCUMENT_DELETED), now=now)
        stores.documents.delete(unit, campaign_id, document_id)
        revision = unit.advance_authz_revision(campaign_id)
        _audit(stores, unit, campaign_id=campaign_id, document_id=document_id, owner_id=owner_id,
               action=AuditAction.DOCUMENT_DELETED, revision=revision, now=now)


def archive_document(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime,
) -> None:
    """RQ-5's two steps as the route runs them, both at the request's one clock:
    step one, committed, then step two, whose busy answer is *not applied yet*
    (RC-15)."""
    guarded(lambda: archive_step_one(db, stores, campaign_id=campaign_id, document_id=document_id,
                                     owner_id=owner_id, now=now))
    guarded(
        lambda: archive_step_two(db, stores, campaign_id=campaign_id, document_id=document_id, owner_id=owner_id,
                                 now=now),
        busy_message=NOT_APPLIED_MESSAGE,
    )


def delete_document(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime,
) -> None:
    """The same two steps for a delete, once the password has checked out."""
    guarded(lambda: delete_step_one(db, stores, campaign_id=campaign_id, document_id=document_id,
                                    owner_id=owner_id, now=now))
    guarded(
        lambda: delete_step_two(db, stores, campaign_id=campaign_id, document_id=document_id, owner_id=owner_id,
                                now=now),
        busy_message=NOT_APPLIED_MESSAGE,
    )


# ── The router ───────────────────────────────────────────────────────────────


def build_router(
    gm: SessionDependency,
    database: Callable[[], TransactionalDatabase | None],
    reauthenticate: Callable[..., PasswordCheck],
) -> APIRouter:
    """The three routes on a `workbench_router`, given the app's GM gate, its
    database getter and its re-authentication dependency, which the delete
    calls before any transaction opens."""
    router = workbench_router(gm)

    def _database(db: TransactionalDatabase | None) -> TransactionalDatabase:
        if db is None:
            raise _unavailable()
        return db

    def _shaped(campaign_id: str, document_id: str) -> None:
        """A path id outside its prefix's shape is the one 404, before any
        query (L-18)."""
        if not (ident.is_id(ident.CAMPAIGN, campaign_id) and ident.is_id(ident.DOCUMENT, document_id)):
            not_found()

    @router.post("/campaigns/{campaign_id}/documents/{document_id}/archive", status_code=204)
    def archive(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        stores: LifecycleStores = Depends(get_lifecycle_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """LIB-17. No body: one sent is not read."""
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        archive_document(live_db, stores, campaign_id=campaign_id, document_id=document_id, owner_id=user.user_id,
                         now=now)
        return Response(status_code=204)

    @router.post("/campaigns/{campaign_id}/documents/{document_id}/unarchive", status_code=204)
    def unarchive(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        stores: LifecycleStores = Depends(get_lifecycle_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """LIB-16's Undo and the Archived filter's Restore. No body: one sent is
        not read."""
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        guarded(lambda: unarchive_document(live_db, stores, campaign_id=campaign_id, document_id=document_id,
                                           owner_id=user.user_id, now=now))
        return Response(status_code=204)

    @router.post("/campaigns/{campaign_id}/documents/{document_id}/delete", status_code=204)
    def delete(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        stores: LifecycleStores = Depends(get_lifecycle_stores),
        db: TransactionalDatabase | None = Depends(database),
        check_password: PasswordCheck = Depends(reauthenticate),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """LIB-18, behind SEC-40's re-authentication, which runs BEFORE any
        transaction opens. A body-carrying `POST`, as a seat's Remove is: there
        is no `DELETE` method."""
        body = _parse(DocumentDeleteRequest, raw)
        live_db = _database(db)
        check_password(body.password.get_secret_value())
        _shaped(campaign_id, document_id)
        delete_document(live_db, stores, campaign_id=campaign_id, document_id=document_id, owner_id=user.user_id,
                        now=now)
        return Response(status_code=204)

    return router

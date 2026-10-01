"""A GM's document lifecycle: archive, unarchive, delete and the
character-sheet link (bead 1kg.5.2, its second pull request;
agent-forge-harness-1kg.5.8; the link, agent-forge-harness-q156).

Six Workbench routes on a `workbench_router` (`agent-forge-harness-oe6`),
wired into `service/app.py` and importing nothing from it: the application
hands `build_router` its GM gate, its database getter and its re-authentication
dependency. They sit beside `documents_api`'s eight in a module of their own so
that one sentence holds of each module: **nothing in `documents_api.py` takes
the campaign lock, narrows, advances the authorisation revision or writes an
audit row; every **write** here does all four; the two narrowings (archive,
delete, unlink) narrow first; the link read changes nothing.**

| Route | Answers |
| --- | --- |
| `POST /campaigns/{campaign_id}/documents/{document_id}/archive` | `204` |
| `POST /campaigns/{campaign_id}/documents/{document_id}/unarchive` | `204` |
| `POST /campaigns/{campaign_id}/documents/{document_id}/delete` | `204`; the body is a `DocumentDeleteRequest` |
| `POST /campaigns/{campaign_id}/documents/{document_id}/link/{participant_id}` | `204` |
| `POST /campaigns/{campaign_id}/documents/{document_id}/unlink` | `204` |
| `GET /campaigns/{campaign_id}/documents/{document_id}/link` | `200 CharacterSheetLink` |

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

**Character-sheet link** (agent-forge-harness-q156; AUD-15, REVEAL-4, ED-14,
TT-16). A character sheet links to at most one seat, and a seat to at most one
sheet (AUD-13): linking widens what that seat may be shown (REVEAL-4's
seeding, and `eligibility_store`'s `characters` class), and unlinking is a
fact-changing narrowing of that sheet's copies, the table's and every
member's (`reveal_scope.DocumentCopies`), exactly as archive and delete are.

**Link is a locked widening** (RQ-4, RQ-10), one transaction: ownership, the
exclusive campaign lock, then the store's B-4 decision (`document_store.
link_character_sheet`) — a missing or foreign document, a type other than
`character-sheet`, a missing/foreign/removed seat (any other seat, open,
offered or accepted, is linkable), already linked to that seat (a no-op), the
sheet linked to another seat, or the seat already holding another sheet (the
last two `409 link_taken`, never a re-point: unlink first). A real change
advances the revision and writes `participant.linked`, naming the seat and
the document (SEC-38). An archived sheet is linkable, and the campaign's
archived or concluded state is not consulted, as no document route consults
it (I-11).

**Unlink is a fact-changing narrowing** (RQ-5, ED-12, A-17, RC-15), in two
steps like archive and delete: step one, without the lock, narrows the live
session's copies of that sheet (`reveal_scope.EndReason.CHARACTER_UNLINKED`)
and commits; step two, under the exclusive lock, clears the link
(`document_store.unlink_character_sheet`), narrows again for a session
started between the steps, advances the revision and writes
`participant.unlinked`. Unlinking a sheet that is not linked is a `204` no-op
at every state, never refused for type (unlike link, which is owner-only from
`_owned` on). The link survives a seat's removal (AUD-16): a removed seat's
sheet stays linked until an explicit unlink.

**The read** (`GET .../link`) takes no lock, advances nothing and writes no
row (a read never locks, RQ-2): ownership, then the document (the one 404),
then whether its `linked_participant_id` names a seat that is still live.

**Order of checks**, as a client sees it:
- link: origin → the one 401 → role → write throttle → database → the path
  ids' shapes (the one 404, before any query) → ownership (the one 404) → the
  exclusive lock (busy: retryable 503) → the document missing or foreign
  (404) → not a character sheet (422, `loc: ["path", "document_id"]`) → the
  seat missing, foreign or removed (404) → already linked to that seat (204,
  nothing changes) → linked to another seat or the seat already linked (409
  `link_taken`) → the write, the advance and `participant.linked`.
- unlink: origin → 401 → 403 → throttle → database → shapes (404) → step one
  (ownership 404, the document 404, narrowed and committed if it was linked)
  → step two (the lock; busy is the retryable 503 that says *not applied
  yet*; ownership again; the document 404; not linked is a 204 no-op;
  otherwise clear, re-narrow, advance and `participant.unlinked`).
- the read: 401 → 403 → database → shapes (404) → ownership (404) → the
  document (404) → `200`. No origin check and no throttle spend (a GET
  changes nothing).
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
from .document_store import DocumentStore, NotLinkable, PostgresDocumentStore, SheetAlreadyLinked
from .documents_api import UNAVAILABLE_MESSAGE, get_clock
from .reveal_scope import DocumentCopies, EndReason
from .reveal_store import PostgresRevealStore
from .reveals import slot_clear_for
from .session import SessionData
from .table_session_store import PostgresTableSessionStore, TableSessionStore
from .workbench_api import SessionDependency, not_found, workbench_router
from .workbench_contracts import CharacterSheetLink, DocumentDeleteRequest, ErrorBody, ErrorCode, ErrorInfo

log = logging.getLogger(__name__)

#: Fixed sentences. A refusal never interpolates anything it was sent (X-7).
NOT_APPLIED_MESSAGE = "That change isn't applied yet. Try again."
NOT_ARCHIVED_MESSAGE = "Archive this document before deleting it."
LINK_TAKEN_MESSAGE = "That sheet or seat is already linked. Unlink it first."

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


def _link_taken() -> HTTPException:
    return _refusal(409, ErrorCode.LINK_TAKEN, LINK_TAKEN_MESSAGE)


def _not_a_sheet() -> RequestValidationError:
    """The document exists but is not a character sheet: a resource-dependent
    422 on the path (`documents_api._invalid`'s shape), not a new code,
    echoing no input (X-7)."""
    return RequestValidationError([{"type": "value_error", "loc": ("path", "document_id"), "msg": "invalid"}])


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


def _link_audit(
    stores: LifecycleStores,
    unit: UnitOfWork,
    *,
    campaign_id: str,
    document_id: str,
    participant_id: str,
    owner_id: int,
    action: AuditAction,
    revision: int,
    now: datetime,
) -> None:
    """SEC-38's row for a link change: the seat as the object, the document
    named in `detail` beside it — and no alias, title or field text."""
    stores.audit.append(
        unit,
        campaign_id=campaign_id,
        actor_kind=ActorKind.GM,
        action=action,
        object_kind=ObjectKind.PARTICIPANT,
        decision=Decision.ALLOWED,
        actor_ref=str(owner_id),
        object_ref=participant_id,
        authz_revision=revision,
        detail={"participant_id": participant_id, "document_id": document_id},
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


# ── The character-sheet link (agent-forge-harness-q156) ─────────────────────


def link_sheet(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    participant_id: str,
    owner_id: int,
    now: datetime,
) -> None:
    """A locked widening (RQ-4, RQ-10), one transaction: ownership (a plain
    read), the exclusive lock, then the store's B-4 decision. A real change
    advances the revision and records `participant.linked`; a repeat — already
    linked to that seat — takes the lock briefly and changes nothing (no
    advance, no row)."""

    def work() -> None:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            unit.lock_campaign(campaign_id, shared=False)
            refusal: HTTPException | RequestValidationError | None = None
            changed = False
            try:
                changed = stores.documents.link_character_sheet(
                    unit, campaign_id, document_id, participant_id=participant_id
                )
            except NotLinkable:
                refusal = _not_a_sheet()
            except SheetAlreadyLinked:
                refusal = _link_taken()
            if refusal is not None:
                raise refusal  # outside the except: nothing chained; the txn rolls back
            if not changed:
                return
            revision = unit.advance_authz_revision(campaign_id)
            _link_audit(stores, unit, campaign_id=campaign_id, document_id=document_id,
                        participant_id=participant_id, owner_id=owner_id,
                        action=AuditAction.PARTICIPANT_LINKED, revision=revision, now=now)

    guarded(work)  # MissingParent (doc or seat) / CampaignAuthzMissing -> the one 404; busy -> retryable 503


def unlink_step_one(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime | None = None,
) -> None:
    """Never calls `lock_campaign`, and is never refused on state or type:
    ownership, the document (a missing or foreign one is the one 404), and —
    only when it is linked — the live session narrowed (that sheet's copies,
    as `character_unlinked`, stamped with the request's clock). Committed
    before step two asks for the lock."""
    with db.transaction() as unit:
        _owned(stores, unit, campaign_id, owner_id)
        found = stores.documents.get(unit, campaign_id, document_id)
        if found is None:
            not_found()
        if found.linked_participant_id is not None:
            _narrow_live(
                stores, unit, campaign_id, clears=DocumentCopies(document_id, EndReason.CHARACTER_UNLINKED),
                now=now,
            )


def unlink_step_two(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime,
) -> None:
    """The exclusive lock first, ownership again under it, then the store's
    unlink (holds the document row; a missing or foreign document is the one
    404, RQ-3's order: `authz_state` -> document row -> session row). `None`
    (not linked) changes nothing and takes no further step. Otherwise narrow
    again — a session started since step one is narrowed too — advance the
    revision and record `participant.unlinked`, naming the seat it WAS linked
    to."""
    with db.transaction() as unit:
        unit.lock_campaign(campaign_id, shared=False)
        _owned(stores, unit, campaign_id, owner_id)
        participant_id = stores.documents.unlink_character_sheet(unit, campaign_id, document_id)
        if participant_id is None:
            return
        _narrow_live(
            stores, unit, campaign_id, clears=DocumentCopies(document_id, EndReason.CHARACTER_UNLINKED), now=now,
        )
        revision = unit.advance_authz_revision(campaign_id)
        _link_audit(stores, unit, campaign_id=campaign_id, document_id=document_id,
                    participant_id=participant_id, owner_id=owner_id,
                    action=AuditAction.PARTICIPANT_UNLINKED, revision=revision, now=now)


def unlink_sheet(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    now: datetime,
) -> None:
    """RQ-5's two steps: step one, committed, then step two, whose busy answer
    is *not applied yet* (RC-15)."""
    guarded(lambda: unlink_step_one(db, stores, campaign_id=campaign_id, document_id=document_id,
                                    owner_id=owner_id, now=now))
    guarded(
        lambda: unlink_step_two(db, stores, campaign_id=campaign_id, document_id=document_id, owner_id=owner_id,
                                now=now),
        busy_message=NOT_APPLIED_MESSAGE,
    )


def read_link(
    db: TransactionalDatabase,
    stores: LifecycleStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
) -> CharacterSheetLink:
    """No lock, no advance, no row (a read never locks, RQ-2): ownership, the
    document (a missing or foreign one is the one 404), then whether the seat
    it names is still live — read through `sheet_for_participant`, which
    answers `None` for a removed seat (so the link survives removal, AUD-16,
    but `seat_active` does not)."""

    def work() -> CharacterSheetLink:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            found = stores.documents.get(unit, campaign_id, document_id)
            if found is None:
                not_found()
            participant_id = found.linked_participant_id
            seat_active = participant_id is not None and (
                sheet := stores.documents.sheet_for_participant(unit, campaign_id, participant_id)
            ) is not None and sheet.id == document_id
            return CharacterSheetLink(
                schema_version=1, document_id=document_id, participant_id=participant_id,
                seat_active=seat_active,
            )

    return guarded(work)


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

    @router.post("/campaigns/{campaign_id}/documents/{document_id}/link/{participant_id}", status_code=204)
    def link(
        campaign_id: str,
        document_id: str,
        participant_id: str,
        user: SessionData = Depends(gm),
        stores: LifecycleStores = Depends(get_lifecycle_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """AUD-15. No body: one sent is not read."""
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        if not ident.is_id(ident.PARTICIPANT, participant_id):
            not_found()
        link_sheet(live_db, stores, campaign_id=campaign_id, document_id=document_id,
                   participant_id=participant_id, owner_id=user.user_id, now=now)
        return Response(status_code=204)

    @router.post("/campaigns/{campaign_id}/documents/{document_id}/unlink", status_code=204)
    def unlink(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        stores: LifecycleStores = Depends(get_lifecycle_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        """RQ-5's two steps. No body: one sent is not read."""
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        unlink_sheet(live_db, stores, campaign_id=campaign_id, document_id=document_id, owner_id=user.user_id,
                     now=now)
        return Response(status_code=204)

    @router.get("/campaigns/{campaign_id}/documents/{document_id}/link", response_model=CharacterSheetLink)
    def link_of(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        stores: LifecycleStores = Depends(get_lifecycle_stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> CharacterSheetLink:
        """The seat a character sheet is linked to, or null (q156). No lock, no
        advance, no row: a read never locks (RQ-2)."""
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        return read_link(live_db, stores, campaign_id=campaign_id, document_id=document_id, owner_id=user.user_id)

    return router

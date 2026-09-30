"""The GM's campaign documents: the library, create, read, field patch, history,
a version's content, restore and seal (bead 1kg.5.2).

Eight Workbench routes on a `workbench_router` (`agent-forge-harness-oe6`),
wired into `service/app.py` and importing nothing from it: the application
hands `build_router` its GM gate and its database getter. The store is
`service/document_store.py` (`1kg.5.1`); the pure rules between it and the wire
are `service/document_wire.py`.

| Route | Answers |
| --- | --- |
| `POST /campaigns/{campaign_id}/library` | a `LibraryPage` for the `LibraryQuery` in the body |
| `POST /campaigns/{campaign_id}/documents` | `201`, the `Document` — a replay of its `command_id` too |
| `GET /campaigns/{campaign_id}/documents/{document_id}` | the `Document` |
| `PATCH /campaigns/{campaign_id}/documents/{document_id}` | the `Document` after a `FieldPatchRequest` (a no-op too) |
| `GET …/documents/{document_id}/versions?limit&cursor` | a `DocumentHistoryPage`, newest first |
| `GET …/documents/{document_id}/versions/{number}` | a `DocumentVersionSnapshot` |
| `POST …/documents/{document_id}/restore` | the `Document` after a `RestoreRequest` |
| `POST …/documents/{document_id}/seal` | the `Document`, its open version sealed |

**The order of checks** (SEC-3; the 1kg.2.2 brief's L-18): origin (403) →
authentication (the one 401) → role (403) → the body and the query, read as
strings and validated on nothing but themselves (422) → the database (503) →
the path ids' shapes (the one 404, before any query) → the body's
`campaign_id` against the path's (422) → a cursor's own shape (422) →
**ownership, in the statement** (the one 404) → the document (the one 404) →
validation that depends on the document (422) → state (409). A 409, a
resource-dependent 422 and `document_unsupported` are therefore unreachable
for a campaign the caller does not own, and a document of the GM's other
campaign under this campaign's path is the same 404 as a missing one.

**Ownership comes first in every transaction**: its first store call is
`CampaignStore.get(..., owner_id=caller)`, a plain `SELECT` that takes no
lock, and every document statement after it names the path's campaign.

**No campaign lock, no narrowing, no revision advance, no audit row.** Every
route here is a read or a content write: a patch, a restore and a seal change
no fact a display's preconditions read (ED-6, X-2), so none of them waits for
the campaign lock. A content write holds the document's row (`hold`, `FOR NO
KEY UPDATE`) and decides everything under that one lock: the patch's
effective fields, the stale judgment (the store's, on exactly those fields)
and the write, so two autosaves of one document are serialised and a retried
one that already landed is a true no-op.

**A write's `Document` is built inside its transaction**, so a result this
build cannot render rolls the write back and the `409 document_unsupported`
is true. **Every refusal decided from a store exception leaves the
transaction by that exception**, which rolls it back: nothing here catches a
store's exception inside a transaction and lets the block commit.

**The campaign's own archived state is not consulted** (the brief's I-11, lead
ruling 5.1#1): every route works in an archived campaign and on an archived
document. That is deliberate, and differs from `campaign_archived` on seats.

**No private text anywhere.** Search travels in a body; cursors hold ids and
numbers; refusals are fixed sentences; a conflict names keys and the current
write revision, never a value (X-7). A log line carries an exception's TYPE and
SQLSTATE, and a count — never its message, a field value, a name or a search.
Every 422 is raised as a `RequestValidationError` carrying no input, and
answered by the application's one validation handler (SEC-23). No route here
builds a 401, 403 or 404 of its own.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from . import campaign_identity as ident
from .campaign_store import CampaignStore, MissingParent, PostgresCampaignStore
from .conversations_api import read_body
from .db import TransactionalDatabase, UnitOfWork
from .document_store import (
    DocumentStore,
    FieldConflict,
    PostgresDocumentStore,
    StaleTypeVersion,
    UnknownCursor,
    UnknownWriteRevision,
)
from .document_wire import (
    DocumentUnsupported,
    InvalidDocumentCursor,
    category_types,
    decode_history_cursor,
    decode_library_cursor,
    effective_fields,
    stored_json,
    to_document,
    to_history_page,
    to_library_page,
    to_snapshot,
    writable,
)
from .session import SessionData
from .workbench_api import SessionDependency, not_found, workbench_router
from .workbench_contracts import (
    HISTORY_PAGE_MAX_ITEMS,
    VERSION_NUMBER_MAX,
    Author,
    ConflictInfo,
    Document,
    DocumentCreateRequest,
    DocumentHistoryPage,
    DocumentVersionSnapshot,
    ErrorBody,
    ErrorCode,
    ErrorInfo,
    FieldPatchRequest,
    LibraryPage,
    LibraryQuery,
    RestoreRequest,
)

log = logging.getLogger(__name__)

#: Fixed sentences. A refusal never interpolates anything it was sent (X-7).
UNAVAILABLE_MESSAGE = "Documents are briefly unavailable. Try again."
CONFLICT_MESSAGE = "This document changed elsewhere."
UNSUPPORTED_MESSAGE = "This document can't be used by this version of Aetheril."

#: The client's page sizes: CANVAS-27's twenty versions, LIB-23's twenty-five
#: rows. Both stop at the contract's fifty, which is a refusal, never a clamp.
HISTORY_PAGE_DEFAULT = 20
LIBRARY_PAGE_DEFAULT = 25

#: A create or a field patch carries a whole document, or a part of one. The
#: largest valid document is a stat block of about 1.3 million JSON characters
#: (the wire contract's per-type bounds), some 5.2 MB of UTF-8 at four bytes a
#: code point, so the 8 KiB every other Workbench body is held to would refuse
#: a document the contract allows. `ui/nginx.conf` lets the same size through.
DOCUMENT_BODY_MAX_BYTES = 6 * 1024 * 1024

_VERSION_NUMBER = re.compile(r"[1-9][0-9]{0,6}")
_HISTORY_LIMIT = re.compile(r"[1-9][0-9]?")
_CURSOR_TEXT = re.compile(r"[A-Za-z0-9_-]{1,512}")


# ── What the routes compose ─────────────────────────────────────────────────


@dataclass(frozen=True)
class DocumentStores:
    """The stores a document route composes in one transaction."""

    campaigns: CampaignStore
    documents: DocumentStore


def get_document_stores() -> DocumentStores:
    """The stores every route uses. Tests override this dependency."""
    return DocumentStores(PostgresCampaignStore(), PostgresDocumentStore())


def get_clock() -> datetime:
    """One `now` per request. Tests override it rather than sleeping."""
    return datetime.now(UTC)


# ── Refusals ─────────────────────────────────────────────────────────────────
# The 401, the 403s and the 404 are the scaffolding's (`workbench_api`); these
# are the answers it has no envelope for.


def _refusal(
    status: int, code: ErrorCode, message: str, *, retryable: bool = False, conflict: ConflictInfo | None = None
) -> HTTPException:
    info = ErrorInfo(code=code, message=message, retryable=retryable, conflict=conflict)
    body = ErrorBody(detail=info).model_dump(mode="json", exclude_none=True)
    return HTTPException(status_code=status, detail=body["detail"])


def _unavailable() -> HTTPException:
    return _refusal(503, ErrorCode.BACKEND_UNAVAILABLE, UNAVAILABLE_MESSAGE, retryable=True)


def _unsupported() -> HTTPException:
    return _refusal(409, ErrorCode.DOCUMENT_UNSUPPORTED, UNSUPPORTED_MESSAGE)


def _conflict(moved: FieldConflict) -> HTTPException:
    """CANVAS-20's banner: the keys that moved and the revision to rebase on,
    and never their text. Not retryable: the answer is Keep mine or Use latest."""
    conflict = ConflictInfo(write_revision=moved.write_revision, fields=list(moved.fields))
    return _refusal(409, ErrorCode.CONFLICT, CONFLICT_MESSAGE, conflict=conflict)


def _invalid(field: str | None, part: str = "body") -> RequestValidationError:
    """A 422 naming the field at fault — never its value. The application's one
    validation handler answers it (SEC-23)."""
    loc: tuple[str, ...] = (part, field) if field is not None else ()
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": "invalid"}])


def _parse[M: BaseModel](model: type[M], raw: bytes) -> M:
    """The body as the contract reads it, or the Workbench 422. The error list
    handed on carries no input, and it is raised outside the `except`, so it
    chains nothing: a ValidationError's `input` is the request."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
    raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors])


def _logged_outage(exc: BaseException) -> HTTPException:
    """The exception's TYPE and SQLSTATE only: its message can quote a row."""
    sqlstate = getattr(exc, "sqlstate", None)
    log.warning("document route: database unavailable (%s%s)", type(exc).__name__, f", {sqlstate}" if sqlstate else "")
    return _unavailable()


def _guarded[T](
    work: Callable[[], T],
    *,
    missing: str | None = None,
    invalid_content_is_unsupported: bool = False,
    cursor_part: str = "body",
) -> T:
    """Run one route's transaction and map what the store or the database said.

    Every mapped exception has already left the transaction, which rolled it
    back. `MissingParent` is the one 404 unless `missing` names the request
    field it can only mean (a restore's version: the document is held by
    then). A `ValueError` is a validation the store refused — the merged
    document of a patch (422, no field) or, for a restore, the chosen version
    (`document_unsupported`). Any driver error, a lock timeout included, is a
    retryable 503. Each answer is raised OUTSIDE the `except`, so nothing is
    chained.
    """
    failure: Exception | None = None
    gone = False
    try:
        return work()
    except DocumentUnsupported:
        failure = _unsupported()
    except StaleTypeVersion:
        failure = _unsupported()
    except FieldConflict as exc:
        failure = _conflict(exc)
    except UnknownWriteRevision:
        failure = _invalid("base_write_revision")
    except UnknownCursor:
        failure = _invalid("cursor", cursor_part)
    except MissingParent:
        gone = missing is None
        failure = None if gone else _invalid(missing)
    except ValueError:
        failure = _unsupported() if invalid_content_is_unsupported else _invalid(None)
    except psycopg.Error as exc:
        failure = _logged_outage(exc)
    if gone:
        not_found()
    assert failure is not None
    raise failure


# ── Bodies, paths and queries ────────────────────────────────────────────────


async def read_document_body(request: Request) -> bytes:
    """`conversations_api.read_body`'s algorithm with the document cap: a body
    longer than `DOCUMENT_BODY_MAX_BYTES` is refused as soon as it is known to
    be longer — by its declared length, or by the first chunk that crosses the
    line — and the rest is never read."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > DOCUMENT_BODY_MAX_BYTES:
        raise _invalid(None)
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > DOCUMENT_BODY_MAX_BYTES:
            raise _invalid(None)
    return bytes(received)


def _version_number(value: str) -> int | None:
    """A path's version number, or None — which the route answers with the one
    404 before any query. Declared as a string so FastAPI never parses it."""
    if _VERSION_NUMBER.fullmatch(value) is None or int(value) > VERSION_NUMBER_MAX:
        return None
    return int(value)


def history_limit(value: str | None) -> int:
    """`?limit=`: an integer from 1 to 50 by an exact grammar, never FastAPI's
    or Pydantic's lax reading (`1.0`, `1_0`, `+5`, ` 5` and `05` are refused)."""
    if value is None:
        return HISTORY_PAGE_DEFAULT
    if _HISTORY_LIMIT.fullmatch(value) is None or int(value) > HISTORY_PAGE_MAX_ITEMS:
        raise _invalid("limit", "query")
    return int(value)


def _owned(stores: DocumentStores, unit: UnitOfWork, campaign_id: str, owner_id: int) -> None:
    """The first store call of every transaction here: the campaign with its
    owner in the statement (SEC-2), or the one 404."""
    if stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
        not_found()


# ── The compositions: one transaction each ──────────────────────────────────


def query_library(
    db: TransactionalDatabase,
    stores: DocumentStores,
    *,
    campaign_id: str,
    owner_id: int,
    query: LibraryQuery,
    after_id: str | None,
) -> LibraryPage:
    """One page of one category. A row the contract cannot carry is skipped and
    counted in the log; `next_cursor` is decided on the rows the store returned
    before that (the brief's I-10)."""
    types = category_types(query.category, query.type)
    limit = query.limit if query.limit is not None else LIBRARY_PAGE_DEFAULT

    def work() -> LibraryPage:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            rows = stores.documents.list_documents(
                unit, campaign_id, types=types, archived=query.archived, search=query.search, sort=query.sort,
                after_id=after_id, limit=limit,
            )
        page, skipped = to_library_page(campaign_id, query.category, rows, limit)
        if skipped:
            log.warning("document route: %d library row(s) could not be listed", skipped)
        return page

    return _guarded(work)


def create_document(
    db: TransactionalDatabase,
    stores: DocumentStores,
    *,
    campaign_id: str,
    owner_id: int,
    request: DocumentCreateRequest,
    now: datetime,
) -> Document:
    """A new document by the GM, version 1 open. A repeat of its `command_id`
    in this campaign answers the document that key already made, as it is now,
    whatever the repeat's body says."""
    data = stored_json(request.data)

    def work() -> Document:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            made = stores.documents.create(
                unit, campaign_id, doc_type=request.type, type_version=request.type_version, data=data,
                author=Author.GM, command_id=request.command_id, now=now,
            )
            return to_document(made)

    return _guarded(work)


def read_document(
    db: TransactionalDatabase, stores: DocumentStores, *, campaign_id: str, document_id: str, owner_id: int
) -> Document:
    def work() -> Document:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            found = stores.documents.get(unit, campaign_id, document_id)
            if found is None:
                not_found()
            return to_document(found)

    return _guarded(work)


def patch_document(
    db: TransactionalDatabase,
    stores: DocumentStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    request: FieldPatchRequest,
    now: datetime,
) -> Document:
    """CANVAS-10's autosave, under the document's row lock from `hold` on.

    In order: the claimed type is the stored one (422 `type`); the base is not
    ahead of the document (422 `base_write_revision`, before the no-op, so a
    client defect is never hidden); the stored document is one this build can
    write over (409); the **effective** fields — the keys whose value differs
    from what is stored — and nothing else is written or judged stale, so a
    patch that already landed answers the document and writes nothing. The
    store rebases the rest over the current data and re-validates the merged
    whole before the commit (CANVAS-19, SEC-33).
    """

    def work() -> Document:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            record = stores.documents.hold(unit, campaign_id, document_id)
            if record is None:
                not_found()
            if record.type != request.type.value:
                raise _invalid("type")
            if request.base_write_revision > record.write_revision:
                raise _invalid("base_write_revision")
            changes = effective_fields(writable(record), request.fields)
            if not changes:
                return to_document(record)
            written = stores.documents.write_fields(
                unit, campaign_id, document_id, fields=changes, author=Author.GM,
                base_write_revision=request.base_write_revision, now=now,
            )
            return to_document(written)

    return _guarded(work)


def read_history(
    db: TransactionalDatabase,
    stores: DocumentStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    before_number: int | None,
    limit: int,
) -> DocumentHistoryPage:
    def work() -> DocumentHistoryPage:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            if stores.documents.get(unit, campaign_id, document_id) is None:
                not_found()
            versions = stores.documents.history(
                unit, campaign_id, document_id, before_number=before_number, limit=limit
            )
        return to_history_page(document_id, versions)

    return _guarded(work, cursor_part="query")


def read_snapshot(
    db: TransactionalDatabase,
    stores: DocumentStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    number: int,
) -> DocumentVersionSnapshot:
    def work() -> DocumentVersionSnapshot:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            found = stores.documents.snapshot(unit, campaign_id, document_id, number)
            if found is None:
                not_found()
            return to_snapshot(found)

    return _guarded(work)


def restore_document(
    db: TransactionalDatabase,
    stores: DocumentStores,
    *,
    campaign_id: str,
    document_id: str,
    owner_id: int,
    version_number: int,
    now: datetime,
) -> Document:
    """CANVAS-26: additive, and a no-op when the document already equals the
    chosen version. A stored document this build cannot write over is refused
    before `restore` runs: a restore replaces the whole of `data`, so a key
    this build does not declare would otherwise be dropped without a word."""

    def work() -> Document:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            record = stores.documents.hold(unit, campaign_id, document_id)
            if record is None:
                not_found()
            writable(record)
            restored = stores.documents.restore(unit, campaign_id, document_id, version_number=version_number, now=now)
            return to_document(restored)

    return _guarded(work, missing="version_number", invalid_content_is_unsupported=True)


def seal_document(
    db: TransactionalDatabase, stores: DocumentStores, *, campaign_id: str, document_id: str, owner_id: int,
    now: datetime,
) -> Document:
    """CANVAS-34's route events — closing or switching the canvas, opening the
    reveal sheet or an export — seal the open version. Idempotent: a document
    with none is answered as it is."""

    def work() -> Document:
        with db.transaction() as unit:
            _owned(stores, unit, campaign_id, owner_id)
            sealed = stores.documents.seal(unit, campaign_id, document_id, now=now)
            if sealed is None:
                not_found()
            return to_document(sealed)

    return _guarded(work)


# ── The router ───────────────────────────────────────────────────────────────


def build_router(gm: SessionDependency, database: Callable[[], TransactionalDatabase | None]) -> APIRouter:
    """The eight routes on a `workbench_router`, given the app's GM gate and
    its database getter."""
    router = workbench_router(gm)

    def _database(db: TransactionalDatabase | None) -> TransactionalDatabase:
        if db is None:
            raise _unavailable()
        return db

    def _shaped(campaign_id: str, document_id: str | None = None) -> None:
        """A path id outside its prefix's shape is the one 404, before any
        query (L-18)."""
        if not ident.is_id(ident.CAMPAIGN, campaign_id) or (
            document_id is not None and not ident.is_id(ident.DOCUMENT, document_id)
        ):
            not_found()

    @router.post("/campaigns/{campaign_id}/library", response_model=LibraryPage)
    def library(
        campaign_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        stores: DocumentStores = Depends(get_document_stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> LibraryPage:
        """A request BODY even without a search: search text never travels in
        a URL (X-7). A query parameter changes nothing."""
        query = _parse(LibraryQuery, raw)
        live_db = _database(db)
        _shaped(campaign_id)
        if query.campaign_id != campaign_id:
            raise _invalid("campaign_id")
        after_id: str | None = None
        if query.cursor is not None:
            try:
                after_id = decode_library_cursor(query.cursor)
            except InvalidDocumentCursor:
                after_id = None
            if after_id is None:
                raise _invalid("cursor")
        return query_library(live_db, stores, campaign_id=campaign_id, owner_id=user.user_id, query=query,
                             after_id=after_id)

    @router.post("/campaigns/{campaign_id}/documents", response_model=Document, status_code=201)
    def create(
        campaign_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_document_body),
        stores: DocumentStores = Depends(get_document_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Document:
        request = _parse(DocumentCreateRequest, raw)
        live_db = _database(db)
        _shaped(campaign_id)
        if request.campaign_id != campaign_id:
            raise _invalid("campaign_id")
        return create_document(live_db, stores, campaign_id=campaign_id, owner_id=user.user_id, request=request,
                               now=now)

    @router.get("/campaigns/{campaign_id}/documents/{document_id}", response_model=Document)
    def read(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        stores: DocumentStores = Depends(get_document_stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> Document:
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        return read_document(live_db, stores, campaign_id=campaign_id, document_id=document_id,
                             owner_id=user.user_id)

    @router.patch("/campaigns/{campaign_id}/documents/{document_id}", response_model=Document)
    def patch(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_document_body),
        stores: DocumentStores = Depends(get_document_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Document:
        request = _parse(FieldPatchRequest, raw)
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        return patch_document(live_db, stores, campaign_id=campaign_id, document_id=document_id,
                              owner_id=user.user_id, request=request, now=now)

    @router.get("/campaigns/{campaign_id}/documents/{document_id}/versions", response_model=DocumentHistoryPage)
    def history(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        limit: str | None = None,
        cursor: str | None = None,
        stores: DocumentStores = Depends(get_document_stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> DocumentHistoryPage:
        size = history_limit(limit)
        if cursor is not None and _CURSOR_TEXT.fullmatch(cursor) is None:
            raise _invalid("cursor", "query")
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        before: int | None = None
        if cursor is not None:
            try:
                before = decode_history_cursor(cursor, document_id)
            except InvalidDocumentCursor:
                before = None
            if before is None:
                raise _invalid("cursor", "query")
        return read_history(live_db, stores, campaign_id=campaign_id, document_id=document_id,
                            owner_id=user.user_id, before_number=before, limit=size)

    @router.get(
        "/campaigns/{campaign_id}/documents/{document_id}/versions/{number}", response_model=DocumentVersionSnapshot
    )
    def snapshot(
        campaign_id: str,
        document_id: str,
        number: str,
        user: SessionData = Depends(gm),
        stores: DocumentStores = Depends(get_document_stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> DocumentVersionSnapshot:
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        version = _version_number(number)
        if version is None:
            not_found()
        return read_snapshot(live_db, stores, campaign_id=campaign_id, document_id=document_id,
                             owner_id=user.user_id, number=version)

    @router.post("/campaigns/{campaign_id}/documents/{document_id}/restore", response_model=Document)
    def restore(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        stores: DocumentStores = Depends(get_document_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Document:
        request = _parse(RestoreRequest, raw)
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        return restore_document(live_db, stores, campaign_id=campaign_id, document_id=document_id,
                                owner_id=user.user_id, version_number=request.version_number, now=now)

    @router.post("/campaigns/{campaign_id}/documents/{document_id}/seal", response_model=Document)
    def seal(
        campaign_id: str,
        document_id: str,
        user: SessionData = Depends(gm),
        stores: DocumentStores = Depends(get_document_stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Document:
        """No body: one sent is not read."""
        live_db = _database(db)
        _shaped(campaign_id, document_id)
        return seal_document(live_db, stores, campaign_id=campaign_id, document_id=document_id,
                             owner_id=user.user_id, now=now)

    return router

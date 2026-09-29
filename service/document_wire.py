"""From the document store to the wire, and back (bead 1kg.5.2). Pure: no I/O,
no FastAPI, no clock.

`service/documents_api.py` composes the store (`service/document_store.py`,
`1kg.5.1`) into routes; everything those routes decide that does not need a
database is here, so a test can hold each rule alone.

**What is stored is JSON of validated values** (`stored_json`). The request
models run `check_fields`, whose output replaces the values: an `asset` becomes
an `AssetRef` model, an `entry_list` a list of entry models, `14.0` becomes
`14`. `json.dumps` cannot serialise those, and storing the raw request instead
would store `14.0` and whatever sub-keys the client sent. So a route stores
`stored_json(...)` of what the model validated, and nothing else.

**What is emitted went through `read_stored_fields`** (`readable`, the 1kg.5.7
rule): a stored key this build does not declare is dropped from a response and
left in the row. **What is written over must pass the strict check**
(`writable`): a field patch merges over the stored data and the store
re-validates the merged whole, so a stored key or sub-key this build does not
declare would either fail that validation for an unrelated field or be lost.
Either document is `DocumentUnsupported` — the route's `409
document_unsupported`, fail closed (the brief's I-9). No type-version upgrade
walk exists yet; the bead that first bumps a type builds it.

**A repeat of a patch that already landed is a no-op** (`effective_fields`):
only the keys whose canonical value differs from what is stored are written or
judged stale, so a retried autosave neither advances a revision nor manufactures
a conflict for another tab (the contract's *Idempotency* row, CANVAS-19).

**Cursors hold ids and numbers, never text** (X-7, SEC-20). A history cursor is
bound to its document; a library cursor names the last row the store returned
and is resolved by the store by id and campaign. A malformed cursor is
`InvalidDocumentCursor`, whose message repeats nothing it was given.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from . import campaign_identity as ident
from .document_store import DocumentRecord, LibraryRow, VersionRecord, VersionSnapshot
from .workbench_contracts import (
    CONTRACT_VERSION,
    DOC_TYPE_LIBRARY_CATEGORY,
    DOC_TYPE_VERSION,
    VERSION_NUMBER_MAX,
    Document,
    DocumentHistoryPage,
    DocumentTypeId,
    DocumentVersion,
    DocumentVersionSnapshot,
    LibraryCategory,
    LibraryItem,
    LibraryPage,
    SchemaVersion,
    check_fields,
    read_stored_fields,
)

#: `Literal[1]` on the wire, `int` as a constant: spelled once.
WIRE_VERSION = cast("SchemaVersion", CONTRACT_VERSION)

#: Why a stored document is one this build cannot use. Closed, so a reason is
#: safe in a log line.
UnsupportedReason = Literal["type", "type_version", "not_an_object", "unreadable", "undeclared_key"]


class DocumentUnsupported(Exception):
    """A stored document this build cannot read, or cannot write over.

    It names the closed reason and nothing of the document: no key, no value,
    no type label a client wrote (SEC-20).
    """

    def __init__(self, reason: UnsupportedReason) -> None:
        self.reason: UnsupportedReason = reason
        super().__init__(f"this build cannot use that stored document ({reason})")


class InvalidDocumentCursor(ValueError):
    """A cursor that is not one this service issued for this request. The
    message is fixed: a cursor is the client's text."""

    def __init__(self) -> None:
        super().__init__("that cursor isn't one this service issued here")


# ── Values ───────────────────────────────────────────────────────────────────


# justification: a document's field values are bare JSON of whatever shape their
# kind declares — the store and `workbench_contracts.check_fields` spell them
# `dict[str, Any]` for the same reason.
def stored_json(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Validated field values as the JSON the store keeps.

    Per value, so a top-level cleared field stays `null` with its key kept,
    while inside a value an absent optional part is left out: an `AssetRef`
    without a size stores no `width` or `height`.
    """
    return {key: to_jsonable_python(value, exclude_none=True) for key, value in fields.items()}


def effective_fields(stored: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """The patch's keys whose canonical value differs from the stored one, as
    the JSON to store. Canonical is `stored_json` of a validated value on both
    sides, so `{"width": null}` on an asset equals an asset stored without one.
    A key the document does not hold differs from any value."""
    current = stored_json(stored)
    wanted = stored_json(patch)
    return {key: value for key, value in wanted.items() if key not in current or current[key] != value}


# ── Reading a stored document ────────────────────────────────────────────────


def _kind(stored_type: str) -> DocumentTypeId:
    known = stored_type in {member.value for member in DocumentTypeId}
    if not known:
        raise DocumentUnsupported("type")
    return DocumentTypeId(stored_type)


def _stored_content(stored_type: str, type_version: int, data: Any) -> tuple[DocumentTypeId, Mapping[str, Any]]:
    # justification: `data` comes out of a `jsonb` column, which holds any JSON
    # value; this is where it is narrowed.
    kind = _kind(stored_type)
    if type_version != DOC_TYPE_VERSION[kind]:
        raise DocumentUnsupported("type_version")
    if not isinstance(data, Mapping):
        raise DocumentUnsupported("not_an_object")
    return kind, data


def readable(record: DocumentRecord | VersionSnapshot) -> dict[str, Any]:
    """The stored content as a response may carry it: `read_stored_fields`, the
    tolerant read, which drops undeclared keys and sub-keys and does not apply
    `required`. Anything it refuses is `DocumentUnsupported`."""
    kind, data = _stored_content(record.type, record.type_version, record.data)
    try:
        return read_stored_fields(kind, record.type_version, data)
    except ValueError:
        pass
    raise DocumentUnsupported("unreadable")


def writable(record: DocumentRecord) -> dict[str, Any]:
    """The stored content, validated strictly, for a write that merges over it.

    Both must hold: the tolerant read (`readable`), and the strict check of the
    raw stored data as a whole document without `required` — which refuses a
    key, an asset sub-key, an entry sub-key or an ability key this build does
    not declare. The second's output is what a patch is compared against.
    """
    readable(record)
    kind, data = _stored_content(record.type, record.type_version, record.data)
    try:
        return check_fields(kind, record.type_version, dict(data), whole=True, enforce_required=False)
    except ValueError:
        pass
    raise DocumentUnsupported("undeclared_key")


# ── Store records to wire models ─────────────────────────────────────────────


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def _built[M: BaseModel](build: type[M], payload: dict[str, Any]) -> M:
    """A response model, or `DocumentUnsupported` when the stored row cannot be
    carried by it — raised outside the handler, so nothing is chained."""
    try:
        return build.model_validate(payload)
    except ValidationError:
        pass
    raise DocumentUnsupported("unreadable")


def to_version(version: VersionRecord) -> DocumentVersion:
    return _built(
        DocumentVersion,
        {
            "number": version.number,
            "author": version.author,
            "summary": version.summary,
            "created_at": _utc(version.created_at),
            "sealed": version.sealed_at is not None,
            "changed_fields": list(version.changed_fields),
            "restored_from": version.restored_from,
        }
    )


def to_document(record: DocumentRecord) -> Document:
    """The whole document, as every read and content write answers it."""
    data = readable(record)
    return _built(
        Document,
        {
            "schema_version": CONTRACT_VERSION,
            "document_id": record.id,
            "campaign_id": record.campaign_id,
            "type": record.type,
            "type_version": record.type_version,
            "data": data,
            "write_revision": record.write_revision,
            "version": to_version(record.version),
            "archived": record.archived_at is not None,
            "created_at": _utc(record.created_at),
            "updated_at": _utc(record.updated_at),
        },
    )


def to_snapshot(snapshot: VersionSnapshot) -> DocumentVersionSnapshot:
    data = readable(snapshot)
    return _built(
        DocumentVersionSnapshot,
        {
            "schema_version": CONTRACT_VERSION,
            "document_id": snapshot.version.document_id,
            "type": snapshot.type,
            "type_version": snapshot.type_version,
            "version": to_version(snapshot.version),
            "data": data,
        },
    )


def to_history_page(document_id: str, versions: Sequence[VersionRecord]) -> DocumentHistoryPage:
    """Newest first, as the store returned them. Numbers run consecutively from
    1 and are never reused, so a page whose last item is version 1 is the end:
    the walk never ends on an empty page."""
    items = [to_version(version) for version in versions]
    last = versions[-1].number if versions else 1
    return DocumentHistoryPage(
        schema_version=WIRE_VERSION,
        document_id=document_id,
        items=items,
        next_cursor=encode_history_cursor(document_id, last) if last > 1 else None,
    )


def to_library_item(row: LibraryRow) -> LibraryItem:
    """A library row as the list shows it, or `DocumentUnsupported` for a row
    the contract cannot carry (an unknown type, a name that is not a title)."""
    return _built(
        LibraryItem,
        {
            "document_id": row.id,
            "type": row.type,
            "title": row.name,
            "qualifier": "" if row.qualifier is None else row.qualifier,
            "tags": list(row.tags),
            "archived": row.archived_at is not None,
            "updated_at": _utc(row.updated_at),
        },
    )


def to_library_page(
    campaign_id: str, category: LibraryCategory, rows: Sequence[LibraryRow], limit: int
) -> tuple[LibraryPage, int]:
    """The page and how many rows it skipped. `next_cursor` is decided on the
    rows the store returned, before any is skipped, so one damaged row neither
    takes the page down nor ends the walk early."""
    items: list[LibraryItem] = []
    skipped = 0
    for row in rows:
        try:
            items.append(to_library_item(row))
        except DocumentUnsupported:
            skipped += 1
    more = len(rows) == limit and bool(rows)
    page = LibraryPage(
        schema_version=WIRE_VERSION,
        campaign_id=campaign_id,
        category=category,
        items=items,
        next_cursor=encode_library_cursor(rows[-1].id) if more else None,
    )
    return page, skipped


def category_types(category: LibraryCategory, doc_type: DocumentTypeId | None) -> list[DocumentTypeId]:
    """The types one library query lists: its category's set (LIB-3), or the
    one type Documents is narrowed to (LIB-22). `LibraryQuery` has already
    refused a type outside its category."""
    if doc_type is not None:
        return [doc_type]
    return sorted((kind for kind, home in DOC_TYPE_LIBRARY_CATEGORY.items() if home is category), key=lambda k: k.value)


# ── Cursors ──────────────────────────────────────────────────────────────────

_CURSOR_TEXT = re.compile(r"[A-Za-z0-9_-]{1,512}")
_HISTORY_CURSOR = re.compile(r"H1\.(doc_[A-Za-z0-9_-]+)\.([1-9][0-9]{0,6})")
_LIBRARY_CURSOR = re.compile(r"L1\.(doc_[A-Za-z0-9_-]+)")


def _encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("ascii")).rstrip(b"=").decode("ascii")


def _decoded(cursor: str) -> str:
    """The cursor's ASCII text, or `InvalidDocumentCursor`. Only the one
    spelling this module issues is accepted, so two cursors never name one
    position."""
    if _CURSOR_TEXT.fullmatch(cursor) is None:
        raise InvalidDocumentCursor()
    text: str | None = None
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        text = None
    if text is None or _encoded(text) != cursor:
        raise InvalidDocumentCursor()
    return text


def _document_id(value: str) -> str:
    if not ident.is_id(ident.DOCUMENT, value):
        raise InvalidDocumentCursor()
    return value


def encode_history_cursor(document_id: str, number: int) -> str:
    return _encoded(f"H1.{document_id}.{number}")


def decode_history_cursor(cursor: str, document_id: str) -> int:
    """The version number a history cursor names, for the document in the path
    and no other."""
    found = _HISTORY_CURSOR.fullmatch(_decoded(cursor))
    if found is None:
        raise InvalidDocumentCursor()
    number = int(found.group(2))
    if _document_id(found.group(1)) != document_id or number > VERSION_NUMBER_MAX:
        raise InvalidDocumentCursor()
    return number


def encode_library_cursor(document_id: str) -> str:
    return _encoded(f"L1.{document_id}")


def decode_library_cursor(cursor: str) -> str:
    """The document id a library cursor names. Which campaign it is of is the
    store's to decide, after ownership (an unknown anchor is `UnknownCursor`)."""
    found = _LIBRARY_CURSOR.fullmatch(_decoded(cursor))
    if found is None:
        raise InvalidDocumentCursor()
    return _document_id(found.group(1))

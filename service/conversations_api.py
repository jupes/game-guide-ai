"""The conversation routes (1kg.2.4 A2): the owner's index, create, read, and patch.

Four routes on an `APIRouter`, wired into `service/app.py` by two lines and
importing nothing from it (ruling A2-1):

| Route | Answers |
| --- | --- |
| `GET /conversations` | the owner's index, newest metadata first |
| `POST /conversations` | a new conversation, id minted here, owner the session (201) |
| `GET /conversations/{id}` | one conversation's metadata |
| `PATCH /conversations/{id}` | rename, archive, unarchive, link to a campaign, bind the channel |

**The Workbench posture, built by hand** until `agent-forge-harness-oe6`'s
scaffolding moves these routes onto it (R-4, R-5). Every check runs in one order
(ruling A2-3; SEC-3): the origin check on a write (SEC-7) → authentication →
the body or the query, which depend on nothing else → the `dm` role → the store
→ the path id → ownership → validation that depends on the conversation → its
state. A 409, and any 422 that depends on a conversation, is therefore
unreachable for a conversation the caller does not own.

**One 404.** A conversation that is missing, belongs to someone else, has no
owner row, or has an id outside `OpaqueId` — and a campaign that is not a live
one of the caller's — answer the same 404 from one helper, `not_found`. These
routes **never claim** a conversation (§8.1): `GET …/messages` next door still
does, and its 403 is the legacy oracle the threat model's §10 accepts for the
pilot.

**No private text anywhere.** No handler logs a title, a campaign id or a
conversation id, and no refusal repeats what it was sent: validation answers
with `validation_error_body`, which reads only an error's type and location, and
nothing here chains a caller's input into an exception.

Deletion is not here. Archive is reversible and destroys nothing; deletion
follows `agent-forge-harness-1ka.5`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, TypeAdapter, ValidationError

from . import timeline
from .conversation_store import (
    Conversation as StoredConversation,
)
from .conversation_store import (
    ConversationStore,
    InvalidCursor,
    MissingParent,
    PostgresConversationStore,
)
from .db import Database, UnitOfWork
from .models import ChatMode
from .session import SessionData
from .workbench_contracts import (
    CONTRACT_VERSION,
    CONVERSATION_PAGE_MAX_ITEMS,
    Conversation,
    ConversationCreateRequest,
    ConversationPage,
    ConversationPatchRequest,
    Cursor,
    ErrorBody,
    ErrorCode,
    ErrorInfo,
    OpaqueId,
    SchemaVersion,
    validation_error_body,
)

log = logging.getLogger(__name__)

#: The largest body a write takes. A create or a patch is a few hundred bytes;
#: anything past this is refused before it is read to the end (ruling A2-5).
BODY_MAX_BYTES = 8_192
#: `Literal[1]` on the wire, `int` as a constant: spelled once, as the timeline does.
WIRE_VERSION = cast("SchemaVersion", CONTRACT_VERSION)

#: Fixed sentences. A refusal never interpolates anything it was sent (X-7).
FORBIDDEN_ORIGIN_MESSAGE = "That request didn't come from this application."
UNAVAILABLE_MESSAGE = "Conversations are briefly unavailable. Try again."
ALREADY_LINKED_MESSAGE = "That conversation is already in a campaign."

#: The one media type a write's body may have (SEC-7). There are no uploads here.
JSON_MEDIA_TYPE = "application/json"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def get_conversation_store() -> ConversationStore:
    """The store every route uses. Tests override this dependency."""
    return PostgresConversationStore()


# ── Refusals ─────────────────────────────────────────────────────────────────


def _refusal(status: int, body: ErrorBody) -> HTTPException:
    """A Workbench refusal as FastAPI raises one: `detail` holds the object, so
    the wire body is exactly `ErrorBody`, without the keys that are unset."""
    return HTTPException(status_code=status, detail=body.detail.model_dump(mode="json", exclude_none=True))


def not_found() -> HTTPException:
    """THE 404 of these routes, and the only place one is built.

    Missing, foreign, unowned and unreadable conversations, and a campaign that
    is not a live one of the caller's, all come through here, so their answers
    cannot drift apart in a status, a body or a header (SEC-3, T-2).
    """
    return _refusal(404, timeline.error_body(ErrorCode.NOT_FOUND, timeline.NOT_FOUND_MESSAGE, retryable=False))


def _forbidden_role() -> HTTPException:
    return _refusal(403, timeline.error_body(ErrorCode.FORBIDDEN, timeline.FORBIDDEN_MESSAGE, retryable=False))


def _forbidden_origin() -> HTTPException:
    return _refusal(403, timeline.error_body(ErrorCode.FORBIDDEN, FORBIDDEN_ORIGIN_MESSAGE, retryable=False))


def _unavailable() -> HTTPException:
    return _refusal(503, timeline.error_body(ErrorCode.BACKEND_UNAVAILABLE, UNAVAILABLE_MESSAGE, retryable=True))


def _already_linked() -> HTTPException:
    info = ErrorInfo(
        code=ErrorCode.ALREADY_LINKED, message=ALREADY_LINKED_MESSAGE, retryable=False, field="campaign_id"
    )
    return _refusal(409, ErrorBody(detail=info))


def _invalid(field: str | None) -> HTTPException:
    """A 422 naming the field at fault — never its value."""
    loc: tuple[str, ...] = ("body", field) if field is not None else ()
    return _refusal(422, validation_error_body([{"type": "value_error", "loc": loc, "msg": "invalid"}]))


def _invalid_query(field: str) -> HTTPException:
    return _refusal(422, timeline.parameter_error_body(field))


# ── SEC-7: where a write came from ───────────────────────────────────────────


@dataclass(frozen=True)
class _Authority:
    host: str
    port: int | None


def _authority(text: str) -> _Authority | None:
    """`host[:port]` as a `Host` header or an origin's network location carries
    it, or None when it cannot be read — which is a refusal, never a guess."""
    try:
        parts = urlsplit(f"//{text}")
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    return None if not host else _Authority(host.lower(), port)


def _origin_authority(origin: str) -> _Authority | None:
    try:
        parts = urlsplit(origin)
        host, port, scheme = parts.hostname, parts.port, parts.scheme.lower()
    except ValueError:
        return None
    if not host or scheme not in _DEFAULT_PORTS:
        return None
    return _Authority(host.lower(), port if port is not None else _DEFAULT_PORTS[scheme])


def _same_application(origin: str, host_header: str | None) -> bool:
    """The `Origin` names this application: its host is the `Host` header's
    host, case-insensitively, and its port is the `Host` header's port when that
    header carries one. The scheme is never compared: Cloud Run terminates TLS
    and forwards HTTP, so this service cannot know its own scheme
    (`config.py`'s `SESSION_COOKIE_SECURE` note). `null` names no origin."""
    if origin == "null" or host_header is None:
        return False
    claimed, served = _origin_authority(origin), _authority(host_header)
    if claimed is None or served is None or claimed.host != served.host:
        return False
    return served.port is None or claimed.port == served.port


def _carries_a_body(request: Request) -> bool:
    if "transfer-encoding" in request.headers:
        return True
    declared = request.headers.get("content-length")
    if declared is None:
        return False
    # An unreadable length is treated as a body: the check fails closed.
    return not declared.isdigit() or int(declared) > 0


def origin_check(request: Request) -> None:
    """SEC-7, for the two writes. A browser request says where it came from and
    must say this application; a request that says nothing is not a browser's,
    and cross-site forgery is a browser attack. A body, from anyone, is JSON.

    Emits no CORS header, and there is no CORS middleware to add one.
    """
    origin = request.headers.get("origin")
    if origin is not None and not _same_application(origin, request.headers.get("host")):
        raise _forbidden_origin()
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "same-origin":
        raise _forbidden_origin()
    if _carries_a_body(request):
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != JSON_MEDIA_TYPE:
            raise _forbidden_origin()


# ── Bodies and queries: validated here, never by FastAPI (SEC-23) ────────────


async def read_body(request: Request) -> bytes:
    """The raw body, at most `BODY_MAX_BYTES`. A longer one is refused as soon
    as it is known to be longer — by its declared length, or by the first chunk
    that crosses the line — and the rest is never read."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > BODY_MAX_BYTES:
        raise _invalid(None)
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > BODY_MAX_BYTES:
            raise _invalid(None)
    return bytes(received)


def _parse[M: BaseModel](model: type[M], raw: bytes) -> M:
    """The body as the contract reads it, or the Workbench 422. Nothing is
    logged, and the error is not chained: its `input` is the request."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise _refusal(422, validation_error_body(exc.errors())) from None


_LIMIT: TypeAdapter[int] = TypeAdapter(int)
_CURSOR: TypeAdapter[str] = TypeAdapter(Cursor)
_CAMPAIGN: TypeAdapter[str] = TypeAdapter(OpaqueId)
_BOOLEANS = {"true": True, "false": False}


@dataclass(frozen=True)
class IndexQuery:
    """`GET /conversations`'s parameters, read. The owner is not one of them: it
    is the session's, never a parameter and never inside a cursor."""

    limit: int
    cursor: str | None
    include_archived: bool
    started_mode: str | None
    campaign_id: str | None


def parse_index_query(
    limit: str | None,
    cursor: str | None,
    include_archived: str | None,
    started_mode: str | None,
    campaign_id: str | None,
) -> IndexQuery:
    """Every parameter of the index, or the 422 naming the first at fault.

    Each is declared `str | None` on the handler so FastAPI never validates it:
    its default 422 repeats the request (SEC-23). A limit outside 1..100 is a
    refusal here even though the store would clamp it. Parameters this route
    does not know are ignored. There is no title and no search parameter.
    """
    size = CONVERSATION_PAGE_MAX_ITEMS
    if limit is not None:
        try:
            size = _LIMIT.validate_python(limit)
        except ValidationError:
            raise _invalid_query("limit") from None
        if not 1 <= size <= CONVERSATION_PAGE_MAX_ITEMS:
            raise _invalid_query("limit")
    if cursor is not None:
        try:
            _CURSOR.validate_python(cursor)
        except ValidationError:
            raise _invalid_query("cursor") from None
    archived = False
    if include_archived is not None:
        if include_archived not in _BOOLEANS:
            raise _invalid_query("include_archived")
        archived = _BOOLEANS[include_archived]
    mode = None
    if started_mode is not None:
        try:
            mode = ChatMode(started_mode).value
        except ValueError:
            raise _invalid_query("started_mode") from None
    if campaign_id is not None:
        try:
            _CAMPAIGN.validate_python(campaign_id)
        except ValidationError:
            raise _invalid_query("campaign_id") from None
    return IndexQuery(size, cursor, archived, mode, campaign_id)


# ── From the store to the wire ───────────────────────────────────────────────


def _utc(moment: datetime | None) -> datetime | None:
    """The server emits UTC, which the contract writes with `Z`."""
    return None if moment is None else moment.astimezone(UTC)


def to_wire(row: StoredConversation) -> Conversation:
    """A stored conversation as the contract carries it: every key present, what
    the row never recorded `null`, and nothing of the owner or of model routing.
    Raises `ValidationError` for a row the contract cannot carry."""
    return Conversation.model_validate(
        {
            "schema_version": WIRE_VERSION,
            "conversation_id": row.id,
            "campaign_id": row.campaign_id,
            "title": row.title,
            "started_mode": row.started_mode,
            "created_at": _utc(row.created_at),
            "updated_at": _utc(row.updated_at),
            "archived_at": _utc(row.archived_at),
        }
    )


def _page(rows: list[StoredConversation], next_cursor: str | None) -> ConversationPage:
    """The index page. A row the contract cannot carry — an id outside
    `OpaqueId` that `/chat` accepted once — is left out rather than taking the
    page down, and only the COUNT is logged. The cursor is the store's, passed
    through unchanged: a page may be short and still not be the last."""
    items: list[Conversation] = []
    for row in rows:
        try:
            items.append(to_wire(row))
        except ValidationError:
            continue
    if len(items) < len(rows):
        log.warning("conversation index: %d stored rows omitted as unreadable", len(rows) - len(items))
    return ConversationPage(schema_version=WIRE_VERSION, items=items, next_cursor=next_cursor)


def _readable(conversation_id: str) -> None:
    """The path id, against the shape the contract carries it in. Outside it is
    the same 404 as missing — the timeline route's rule."""
    try:
        timeline.require_readable_id(conversation_id)
    except timeline.ConversationNotFound:
        raise not_found() from None


def _require_dm(session: SessionData) -> None:
    # R-3, TA-3: every new Workbench route needs the dm role until yje.4.1
    # replaces this check in one place.
    if session.role != "dm":
        raise _forbidden_role()


def _logged_outage(exc: BaseException) -> HTTPException:
    # The exception TYPE only: its message can carry a statement, and a
    # statement can carry a title (SEC-20).
    log.warning("conversation route: database unavailable (%s)", type(exc).__name__)
    return _unavailable()


# ── PATCH ────────────────────────────────────────────────────────────────────


def _check_against(row: StoredConversation, patch: ConversationPatchRequest) -> None:
    """The 422s that depend on the conversation, after ownership and before any
    state check (SEC-3). A conversation inside a campaign is a GM thread
    (ruling A2-10): linking needs a row started in gm or never recorded, and a
    row that is or is about to be linked binds no channel but gm."""
    links = patch.campaign_id is not None
    if links and row.started_mode not in (None, ChatMode.gm.value):
        raise _invalid("campaign_id")
    linked = row.campaign_id is not None or links
    if patch.started_mode is not None and linked and patch.started_mode is not ChatMode.gm:
        raise _invalid("started_mode")


def apply_patch(
    store: ConversationStore,
    unit: UnitOfWork,
    conversation_id: str,
    *,
    owner_id: int,
    patch: ConversationPatchRequest,
) -> StoredConversation:
    """One PATCH, inside one transaction, in the order of ruling A2-11. Every
    refusal is raised inside the transaction, so it rolls back what came before."""
    row = store.get_for_owner(unit, conversation_id, owner_id=owner_id)
    if row is None:
        raise not_found()
    _check_against(row, patch)
    if patch.campaign_id is not None:
        if row.campaign_id is None:
            if not store.link_campaign(unit, conversation_id, owner_id=owner_id, campaign_id=patch.campaign_id):
                # The campaign is not a live one of the caller's, or another
                # request linked the row first. Told apart by reading again,
                # never by a second oracle.
                again = store.get_for_owner(unit, conversation_id, owner_id=owner_id)
                if again is None or again.campaign_id is None:
                    raise not_found()
                if again.campaign_id != patch.campaign_id:
                    raise _already_linked()
        elif row.campaign_id != patch.campaign_id:
            raise _already_linked()
    if patch.started_mode is not None:
        # First writer wins; a different winner is the answer, not an error.
        if store.bind_started_mode(unit, conversation_id, owner_id=owner_id, mode=patch.started_mode.value) is None:
            raise not_found()
    # A rename to the title it already has changes nothing, not even the order.
    if patch.title is not None and patch.title != row.title:
        if not store.rename(unit, conversation_id, owner_id=owner_id, title=patch.title):
            raise not_found()
    if patch.archived is not None:
        # False means it was already in that state: a repeat is a no-op.
        store.set_archived(unit, conversation_id, owner_id=owner_id, archived=patch.archived)
    final = store.get_for_owner(unit, conversation_id, owner_id=owner_id)
    if final is None:
        raise not_found()
    if final.campaign_id is not None and final.started_mode not in (None, ChatMode.gm.value):
        # Only reachable through a race with another request of the same owner
        # between the checks above and these writes. Refused, so the whole
        # patch rolls back and the invariant holds whichever request lost.
        raise _invalid("campaign_id" if patch.campaign_id is not None else "started_mode")
    return final


# ── The router ───────────────────────────────────────────────────────────────


def build_router(
    session: Callable[..., SessionData],
    database: Callable[[], Database | None],
) -> APIRouter:
    """The four routes, depending on the app's own `require_session` and its
    database getter, so the app's overrides and its recovery apply unchanged."""
    router = APIRouter()

    @router.get("/conversations", response_model=ConversationPage)
    def list_conversations(
        user: SessionData = Depends(session),
        limit: str | None = None,
        cursor: str | None = None,
        include_archived: str | None = None,
        started_mode: str | None = None,
        campaign_id: str | None = None,
        store: ConversationStore = Depends(get_conversation_store),
        db: Database | None = Depends(database),
    ) -> ConversationPage:
        """The owner's index. A campaign filter the caller does not own matches
        nothing, so the page is empty: no 404, no oracle."""
        query = parse_index_query(limit, cursor, include_archived, started_mode, campaign_id)
        _require_dm(user)
        if db is None:
            raise _unavailable()
        try:
            with db.transaction() as unit:
                page = store.list_for_owner(
                    unit,
                    user.user_id,
                    campaign_id=query.campaign_id,
                    started_mode=query.started_mode,
                    include_archived=query.include_archived,
                    cursor=query.cursor,
                    limit=query.limit,
                )
        except InvalidCursor:
            raise _invalid_query("cursor") from None
        except psycopg.Error as exc:
            raise _logged_outage(exc) from exc
        return _page(page.items, page.next_cursor)

    @router.post("/conversations", response_model=Conversation, status_code=201)
    def create_conversation(
        _origin: None = Depends(origin_check),
        user: SessionData = Depends(session),
        raw: bytes = Depends(read_body),
        store: ConversationStore = Depends(get_conversation_store),
        db: Database | None = Depends(database),
    ) -> Conversation:
        """A new conversation: the id minted here, the owner the session. There
        is no claim (§8.1) and no idempotency key (ruling 2.4#5)."""
        request = _parse(ConversationCreateRequest, raw)
        _require_dm(user)
        if db is None:
            raise _unavailable()
        try:
            with db.transaction() as unit:
                made = store.create(
                    unit,
                    owner_id=user.user_id,
                    campaign_id=request.campaign_id,
                    title=request.title,
                    started_mode=request.started_mode.value,
                )
        except MissingParent:
            # "404 by campaign" (§8.1): a missing and a foreign campaign are one answer.
            raise not_found() from None
        except psycopg.Error as exc:
            raise _logged_outage(exc) from exc
        return to_wire(made)

    @router.get("/conversations/{conversation_id}", response_model=Conversation)
    def read_conversation(
        conversation_id: str,
        user: SessionData = Depends(session),
        store: ConversationStore = Depends(get_conversation_store),
        db: Database | None = Depends(database),
    ) -> Conversation:
        """One conversation, archived or not, for its owner — and the one 404 for
        everybody else. Never a claim."""
        _require_dm(user)
        if db is None:
            raise _unavailable()
        _readable(conversation_id)
        try:
            with db.transaction() as unit:
                found = store.get_for_owner(unit, conversation_id, owner_id=user.user_id)
        except psycopg.Error as exc:
            raise _logged_outage(exc) from exc
        if found is None:
            raise not_found()
        return to_wire(found)

    @router.patch("/conversations/{conversation_id}", response_model=Conversation)
    def patch_conversation(
        conversation_id: str,
        _origin: None = Depends(origin_check),
        user: SessionData = Depends(session),
        raw: bytes = Depends(read_body),
        store: ConversationStore = Depends(get_conversation_store),
        db: Database | None = Depends(database),
    ) -> Conversation:
        """Rename, archive, unarchive, link, bind the channel — in one
        transaction, all or nothing."""
        patch = _parse(ConversationPatchRequest, raw)
        _require_dm(user)
        if db is None:
            raise _unavailable()
        _readable(conversation_id)
        try:
            with db.transaction() as unit:
                final = apply_patch(store, unit, conversation_id, owner_id=user.user_id, patch=patch)
        except psycopg.Error as exc:
            raise _logged_outage(exc) from exc
        return to_wire(final)

    return router

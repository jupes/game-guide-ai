"""Workbench wire contract, v1 (1kg.1.2).

The request, response and event shapes the GM Workbench speaks. The decisions
behind them are in ``docs/adr/gm-workbench-interactions.md``; the conventions,
the forward-version rules and what is still to come are in
``docs/workbench-wire-contract.md``.

Three rules shape everything here:

* **The fixtures are the specification.** ``contracts/workbench/v1/*.json`` is
  validated by this module *and* by ``ui/src/gm/contracts.ts``, so the two cannot
  drift apart without a test failing.
* **The server fails closed.** Every model forbids undeclared fields, every enum
  is closed, and no primitive is coerced — a ``brief`` of ``42`` is an error, not
  the string ``"42"``. A client, by contrast, tolerates additive fields from a
  newer server; that asymmetry is recorded per example with ``applies_to``.
* **The registry facts below are not the registry.** They are the minimum the
  validators need (which tool lands as which kind, which may run without a
  brief). ``1kg.3.1`` owns the full catalogue and extends the shared
  ``registry.json`` that pins these.

A flat module rather than a subpackage: ``pyproject.toml`` lists packages
explicitly, so a subpackage would be silently dropped from the built image.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from service.models import (
    ChatMode,
    RoutingInfo,
    Source,
    SpellContent,
    StatBlockContent,
    Suggestion,
    SuggestionsRoutingInfo,
)

CONTRACT_VERSION = 1

#: Decision RAIL-6. Counted in Unicode code points, after trimming — the Zod
#: side counts code points too, so an emoji costs one on both sides.
BRIEF_MAX_CHARS = 2000
#: Decision RAIL-8: a suggestion's prefilled brief.
SUGGESTION_BRIEF_MAX_CHARS = 200
PROSE_MAX_CHARS = 4000
MAX_SUGGESTIONS = 3
#: A ceiling on a stored prompt or answer, so a page has a bounded size. It sits
#: far above anything the pipeline produces; ``agent-forge-harness-764`` owns the
#: request-side bound on ``/chat``, which must not exceed it.
CHAT_TEXT_MAX_CHARS = 100_000
MAX_SOURCES = 50
TIMELINE_PAGE_MAX_ITEMS = 100


# ── Identifiers ──────────────────────────────────────────────────────────────

#: Opaque, URL-fragment-safe and log-safe. Conversation ids are UUIDs today and
#: fit; nothing on the wire may carry meaning in an id.
OpaqueId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")]
#: Client-minted, and the idempotency key of an invocation (RAIL-18), so it has
#: a floor: sixteen characters keeps accidental collisions out of reach.
InvocationId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{16,64}$")]
#: One grammar for every timestamp (CANVAS-27): seconds, at most microseconds,
#: and ``Z`` or a ``+HH:MM`` offset. The Zod side carries the same pattern.
_ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


def _iso_text_or_datetime(value: object) -> object:
    """No coercion. Left alone, Pydantic reads ``1758050000`` as a moment and
    accepts a space for the ``T``; Zod accepts neither."""
    if isinstance(value, datetime) or (isinstance(value, str) and _ISO_TIMESTAMP.fullmatch(value)):
        return value
    raise ValueError("a timestamp is ISO 8601 text with seconds and an offset")


Timestamp = Annotated[AwareDatetime, BeforeValidator(_iso_text_or_datetime)]


def _a_real_integer(value: object) -> object:
    """No coercion. To Python ``True == 1``, so a bare ``Literal[1]`` accepts
    ``true``; Zod does not."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("schema_version is an integer")
    return value


SchemaVersion = Annotated[Literal[1], BeforeValidator(_a_real_integer)]

#: Opaque to clients and base64url, because a cursor may ride in a query string.
#: Search text may not (X-7), which is why a cursor never encodes any.
Cursor = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,512}$")]


# ── Closed vocabularies ──────────────────────────────────────────────────────


class ToolId(str, Enum):
    NPC = "npc"
    MONSTER = "monster"
    LOOT = "loot"
    NAMES = "names"
    RULES = "rules"
    PORTRAIT = "portrait"
    ENCOUNTER = "encounter"
    HOOKS = "hooks"
    RECAP = "recap"
    MAP = "map"


class ResultKind(str, Enum):
    CARD = "card"
    DOCUMENT = "document"
    MEDIA = "media"


class CardKind(str, Enum):
    """Card payloads are a closed union. ``1kg.4.3`` adds loot, names, rules and
    hooks; until then those tools cannot produce a valid card, by design."""

    STAT_BLOCK = "stat_block"


class DocumentTypeId(str, Enum):
    NPC = "npc"
    STATBLOCK = "statblock"
    HANDOUT = "handout"
    SESSION_NOTES = "session-notes"
    QUEST_LOG = "quest-log"
    CHARACTER_SHEET = "character-sheet"
    LORE = "lore"
    ENCOUNTER = "encounter"


class LibraryCategory(str, Enum):
    NPCS = "npcs"
    BESTIARY = "bestiary"
    DOCUMENTS = "documents"
    SESSION_LOG = "session-log"
    CUES = "cues"


class BriefPolicy(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class InvocationStatus(str, Enum):
    """``unknown`` is deliberately absent: it is a client-only state (RAIL-21)."""

    WORKING = "working"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EntryKind(str, Enum):
    """Timeline entry kinds. AI edits and attached cues join with the documents
    and cue families; a new kind after v1 ships is a version bump."""

    CHAT = "chat"
    TOOL = "tool"
    SESSION_DIVIDER = "session_divider"
    OPAQUE = "opaque"


class SessionBoundary(str, Enum):
    START = "start"
    END = "end"


class OpaqueReason(str, Enum):
    """Closed and free of stored text, so a reason is safe as a metric label."""

    NEWER_VERSION = "newer_version"
    UNREADABLE = "unreadable"


class ErrorCode(str, Enum):
    """Closed, lower-case and free of user text, so a code is always safe as a
    metric label (plan invariant 10). Clients treat an unknown code as generic."""

    VALIDATION_FAILED = "validation_failed"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    BRIEF_REQUIRED = "brief_required"
    BRIEF_TOO_LONG = "brief_too_long"
    UNKNOWN_TOOL = "unknown_tool"
    TOOL_DISABLED = "tool_disabled"
    CAMPAIGN_REQUIRED = "campaign_required"
    NOTHING_TO_RECAP = "nothing_to_recap"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    CAP_REACHED = "cap_reached"
    THROTTLED_USER = "throttled_user"
    THROTTLED_DAILY = "throttled_daily"
    PROVIDER_FAILED = "provider_failed"
    PROVIDER_TIMEOUT = "provider_timeout"
    ATTEMPT_EXPIRED = "attempt_expired"
    BACKEND_UNAVAILABLE = "backend_unavailable"


# ── Registry facts the validators need (pinned by registry.json) ─────────────

TOOL_RESULT_KIND: dict[ToolId, ResultKind] = {
    ToolId.NPC: ResultKind.DOCUMENT,
    ToolId.MONSTER: ResultKind.CARD,
    ToolId.LOOT: ResultKind.CARD,
    ToolId.NAMES: ResultKind.CARD,
    ToolId.RULES: ResultKind.CARD,
    ToolId.PORTRAIT: ResultKind.MEDIA,
    ToolId.ENCOUNTER: ResultKind.DOCUMENT,
    ToolId.HOOKS: ResultKind.CARD,
    ToolId.RECAP: ResultKind.DOCUMENT,
    ToolId.MAP: ResultKind.MEDIA,
}

#: Decision RAIL-5: only ``recap`` may run without a brief in v1.
TOOL_BRIEF_POLICY: dict[ToolId, BriefPolicy] = {
    tool: (BriefPolicy.OPTIONAL if tool is ToolId.RECAP else BriefPolicy.REQUIRED) for tool in ToolId
}

TOOL_CREATES_DOC_TYPE: dict[ToolId, DocumentTypeId] = {
    ToolId.NPC: DocumentTypeId.NPC,
    ToolId.ENCOUNTER: DocumentTypeId.ENCOUNTER,
    ToolId.RECAP: DocumentTypeId.SESSION_NOTES,
}

TOOL_CARD_KIND: dict[ToolId, CardKind] = {ToolId.MONSTER: CardKind.STAT_BLOCK}

#: Decision LIB-1 to LIB-6: membership is a registry fact, not a free field.
DOC_TYPE_LIBRARY_CATEGORY: dict[DocumentTypeId, LibraryCategory] = {
    DocumentTypeId.NPC: LibraryCategory.NPCS,
    DocumentTypeId.STATBLOCK: LibraryCategory.BESTIARY,
    DocumentTypeId.HANDOUT: LibraryCategory.DOCUMENTS,
    DocumentTypeId.SESSION_NOTES: LibraryCategory.SESSION_LOG,
    DocumentTypeId.QUEST_LOG: LibraryCategory.DOCUMENTS,
    DocumentTypeId.CHARACTER_SHEET: LibraryCategory.DOCUMENTS,
    DocumentTypeId.LORE: LibraryCategory.DOCUMENTS,
    DocumentTypeId.ENCOUNTER: LibraryCategory.DOCUMENTS,
}


# ── Models ───────────────────────────────────────────────────────────────────


class _Contract(BaseModel):
    """Every Workbench payload forbids undeclared fields: a typo, a smuggled
    document body or a remote URL fails instead of riding along."""

    model_config = ConfigDict(extra="forbid")


class ErrorInfo(_Contract):
    code: ErrorCode
    #: Presentable as-is, and never echoes GM-private text back (X-7).
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    retryable: StrictBool
    #: The request field at fault, for a validation failure.
    field: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)] | None = None
    #: Only for ``throttled_user`` (RAIL-20): the daily cap has no window to wait out.
    retry_after_s: Annotated[StrictInt, Field(ge=0, le=86_400)] | None = None
    #: Only for ``cap_reached`` (X-5): what is holding the cap.
    in_flight: Annotated[list[InvocationId], Field(max_length=8)] | None = None


class ErrorBody(_Contract):
    """The Workbench error envelope. It keeps FastAPI's ``detail`` key so that one
    client reader covers a legacy string, a 422 list and this object."""

    detail: ErrorInfo


class ToolInvocationRequest(_Contract):
    """Decisions RAIL-5 to RAIL-9. There is no free-form ``context``: the server
    assembles context from state it has authorised; a client sends ids only."""

    schema_version: SchemaVersion
    invocation_id: InvocationId
    tool_id: ToolId
    #: Always present; a brief-optional tool sends the empty string.
    brief: StrictStr
    campaign_id: OpaqueId
    conversation_id: OpaqueId
    #: The timeline entry a suggestion was armed from (RAIL-8).
    source_entry_id: OpaqueId | None = None

    @field_validator("brief")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _brief_fits_its_tool(self) -> Self:
        if len(self.brief) > BRIEF_MAX_CHARS:
            raise ValueError(f"brief is longer than {BRIEF_MAX_CHARS} characters")
        if not self.brief and TOOL_BRIEF_POLICY[self.tool_id] is BriefPolicy.REQUIRED:
            raise ValueError(f"{self.tool_id.value} needs a brief")
        return self


class ToolSuggestion(_Contract):
    """Decision RAIL-8: a suggestion arms the composer; it never runs."""

    tool_id: ToolId
    label: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=60)]
    #: A Material Symbols Rounded ligature.
    icon: Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z0-9_]{1,40}$")] | None = None
    brief: (
        Annotated[str, StringConstraints(strict=True, min_length=1, max_length=SUGGESTION_BRIEF_MAX_CHARS)] | None
    ) = None


class DocumentLink(_Contract):
    """What a document result carries: enough to link and title it, and nothing
    of its body (``1kg.4.4``: a response does not embed hidden data)."""

    document_id: OpaqueId
    type: DocumentTypeId
    title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
    library_category: LibraryCategory

    @model_validator(mode="after")
    def _category_belongs_to_type(self) -> Self:
        expected = DOC_TYPE_LIBRARY_CATEGORY[self.type]
        if self.library_category is not expected:
            raise ValueError(f"{self.type.value} documents live in {expected.value}")
        return self


class AssetRef(_Contract):
    """No URL, ever (X-10): a client builds a same-origin URL from the id."""

    asset_id: OpaqueId
    #: Audio is a cue, never a media result.
    media_type: Literal["image"]
    alt: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=300)]
    width: Annotated[StrictInt, Field(ge=1, le=20_000)] | None = None
    height: Annotated[StrictInt, Field(ge=1, le=20_000)] | None = None


class StatBlockCard(_Contract):
    card_kind: Literal["stat_block"]
    #: The existing /chat stat-block contract, reused rather than re-declared.
    stat_block: StatBlockContent


#: A one-member union today. When ``1kg.4.3`` adds card kinds this becomes
#: ``Annotated[A | B, Field(discriminator="card_kind")]``.
CardContent = StatBlockCard


class _ResultBase(_Contract):
    tool_id: ToolId
    #: The assistant's own sentence. Quoted rules text stays inside the card.
    prose: Annotated[str, StringConstraints(strict=True, max_length=PROSE_MAX_CHARS)]
    #: Always present; "none" is the empty list.
    suggestions: Annotated[list[ToolSuggestion], Field(max_length=MAX_SUGGESTIONS)]

    def _require_kind(self, kind: ResultKind) -> None:
        expected = TOOL_RESULT_KIND[self.tool_id]
        if expected is not kind:
            raise ValueError(f"{self.tool_id.value} results land as {expected.value}, not {kind.value}")


class CardResult(_ResultBase):
    result_kind: Literal["card"]
    card: CardContent

    @model_validator(mode="after")
    def _agrees_with_the_registry(self) -> Self:
        self._require_kind(ResultKind.CARD)
        expected = TOOL_CARD_KIND.get(self.tool_id)
        if expected is None or expected.value != self.card.card_kind:
            raise ValueError(f"{self.tool_id.value} has no {self.card.card_kind} card")
        return self


class DocumentResult(_ResultBase):
    result_kind: Literal["document"]
    document: DocumentLink

    @model_validator(mode="after")
    def _agrees_with_the_registry(self) -> Self:
        self._require_kind(ResultKind.DOCUMENT)
        expected = TOOL_CREATES_DOC_TYPE[self.tool_id]
        if self.document.type is not expected:
            raise ValueError(f"{self.tool_id.value} creates {expected.value} documents")
        return self


class MediaResult(_ResultBase):
    result_kind: Literal["media"]
    asset: AssetRef

    @model_validator(mode="after")
    def _agrees_with_the_registry(self) -> Self:
        self._require_kind(ResultKind.MEDIA)
        return self


#: "Size decides placement": the discriminator is the routing rule, and an
#: unknown kind fails closed (X-8).
ToolResult = Annotated[CardResult | DocumentResult | MediaResult, Field(discriminator="result_kind")]


class ToolInvocation(_Contract):
    """The status resource behind a lane (RAIL-15 to RAIL-27)."""

    schema_version: SchemaVersion
    invocation_id: InvocationId
    tool_id: ToolId
    status: InvocationStatus
    #: Counts from one; a retry of a failed attempt increments it (RAIL-18).
    attempt: Annotated[StrictInt, Field(ge=1, le=100)]
    #: With ``status: done`` this is "finished before it could be cancelled" (RAIL-23).
    cancel_requested: StrictBool
    created_at: Timestamp
    updated_at: Timestamp
    result: ToolResult | None
    error: ErrorInfo | None

    @model_validator(mode="after")
    def _status_and_payload_agree(self) -> Self:
        has_result, has_error = self.result is not None, self.error is not None
        wanted = {
            InvocationStatus.WORKING: (False, False),
            InvocationStatus.DONE: (True, False),
            InvocationStatus.FAILED: (False, True),
            InvocationStatus.CANCELLED: (False, False),
        }[self.status]
        if (has_result, has_error) != wanted:
            raise ValueError(f"a {self.status.value} invocation carries the wrong result/error pair")
        if self.result is not None and self.result.tool_id is not self.tool_id:
            raise ValueError("the result belongs to another tool")
        return self


# ── Timeline ─────────────────────────────────────────────────────────────────
#
# One entry per *exchange*: a turn carries its own outcome. A page boundary can
# therefore never separate a prompt from its result (``1kg.4.2``), results sit
# beneath the turn that asked for them however late they finish (RAIL-16), and
# the shape matches what the client already keeps (``useChat``'s ``Exchange``).


class ChatAnswer(_Contract):
    """A complete assistant outcome. The pieces are the existing ``/chat`` models,
    reused rather than re-declared, so the evidence payload ``xiu.5.2`` adds to
    them round-trips here instead of growing a second definition."""

    #: Markdown, rendered without remote subresources (X-10).
    text: Annotated[str, StringConstraints(strict=True, max_length=CHAT_TEXT_MAX_CHARS)]
    #: ``None`` is "not recorded" — a row older than the durable timeline. The
    #: key is required so that absence can never be mistaken for ``True``.
    answerable: StrictBool | None
    #: ``None`` is "not recorded"; the empty list is "recorded, and none".
    sources: Annotated[list[Source], Field(max_length=MAX_SOURCES)] | None
    created_at: Timestamp
    suggestions: Annotated[list[Suggestion], Field(max_length=MAX_SUGGESTIONS)] | None = None
    routing: RoutingInfo | None = None
    suggestions_routing: SuggestionsRoutingInfo | None = None
    spell_content: SpellContent | None = None
    stat_block: StatBlockContent | None = None


class _EntryBase(_Contract):
    #: Per entry, not only per page: entries are stored one by one and may
    #: outlive the server version that wrote them.
    schema_version: SchemaVersion
    entry_id: OpaqueId
    #: When the turn was made. Order is the page's, never re-derived from this.
    created_at: Timestamp


class ChatEntry(_EntryBase):
    """A plain message and its answer, in any mode (RAIL-14)."""

    entry_kind: Literal["chat"]
    mode: ChatMode
    #: ``None`` only for an old answer whose prompt was never recorded.
    prompt: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=CHAT_TEXT_MAX_CHARS)] | None
    #: ``None`` while the turn has no stored answer: it failed, or is still running.
    answer: ChatAnswer | None

    @model_validator(mode="after")
    def _has_something_to_show(self) -> Self:
        if self.prompt is None and self.answer is None:
            raise ValueError("a chat entry needs a prompt or an answer")
        return self


class ToolEntry(_EntryBase):
    """Decision RAIL-9: the stored GM turn is a tool and a brief, never the slash
    string. The tool is ``invocation.tool_id`` — kept in one place so that the
    turn and its lane cannot disagree."""

    entry_kind: Literal["tool"]
    #: Bounded on the way back out too (RAIL-6). The brief *policy* is not
    #: re-judged here: it belongs to the moment a tool runs, and a registry
    #: change must never make an old turn unreadable.
    brief: Annotated[str, StringConstraints(strict=True, max_length=BRIEF_MAX_CHARS)]
    #: The entry whose suggestion armed this turn (RAIL-8).
    source_entry_id: OpaqueId | None = None
    invocation: ToolInvocation


class SessionDividerEntry(_EntryBase):
    """The only thing that separates prep from play. ``/recap`` reads from the
    most recent ``start``; rotating a link moves neither boundary (REVEAL-17)."""

    entry_kind: Literal["session_divider"]
    session_id: OpaqueId
    boundary: SessionBoundary


class OpaqueEntry(_EntryBase):
    """Stands in for a stored entry this server cannot read — written by a newer
    version before a rollback, or damaged. It keeps the entry's place in the
    thread and carries **nothing** of it: a server never forwards bytes it has
    not validated. The stored row itself is left untouched."""

    entry_kind: Literal["opaque"]
    reason: OpaqueReason


AnyEntry = ChatEntry | ToolEntry | SessionDividerEntry | OpaqueEntry
TimelineEntry = Annotated[AnyEntry, Field(discriminator="entry_kind")]


class TimelinePage(_Contract):
    """The pagination envelope's first concrete page. Items run **newest first**
    and ``next_cursor`` leads to older entries."""

    schema_version: SchemaVersion
    conversation_id: OpaqueId
    items: Annotated[list[TimelineEntry], Field(max_length=TIMELINE_PAGE_MAX_ITEMS)]
    #: Required: the end of the list is ``None``, never a missing key.
    next_cursor: Cursor | None


_ENTRY_ADAPTER: TypeAdapter[Any] = TypeAdapter(TimelineEntry)


def _mentions_newer_version(value: object, depth: int = 0) -> bool:
    """Whether any ``schema_version`` inside a stored payload is beyond this
    contract — the entry's own, or an embedded invocation's."""
    if depth > 8:
        return False
    if isinstance(value, dict):
        version = value.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool) and version > CONTRACT_VERSION:
            return True
        return any(_mentions_newer_version(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_mentions_newer_version(item, depth + 1) for item in value)
    return False


def entry_or_opaque(raw: object, *, entry_id: str, created_at: datetime) -> AnyEntry:
    """The forward-version rule for stored entries, as code (``1kg.4.2`` calls it).

    A payload this server can validate is served. Anything else — a newer
    version's after a rollback, an unknown kind, a damaged row — becomes an
    :class:`OpaqueEntry` in the same place. It is never dropped, never
    rewritten, and never forwarded unvalidated.
    """
    try:
        entry: AnyEntry = _ENTRY_ADAPTER.validate_python(raw)
    except ValidationError:
        reason = OpaqueReason.NEWER_VERSION if _mentions_newer_version(raw) else OpaqueReason.UNREADABLE
        return OpaqueEntry(
            schema_version=1, entry_kind="opaque", entry_id=entry_id, created_at=created_at, reason=reason
        )
    return entry


#: Name → validator, in the order ``contracts/workbench/v1/schemas.json`` lists them.
CONTRACT_SCHEMAS: dict[str, TypeAdapter[Any]] = {
    "Timestamp": TypeAdapter(Timestamp),
    "ErrorBody": TypeAdapter(ErrorBody),
    "ToolInvocationRequest": TypeAdapter(ToolInvocationRequest),
    "ToolSuggestion": TypeAdapter(ToolSuggestion),
    "DocumentLink": TypeAdapter(DocumentLink),
    "AssetRef": TypeAdapter(AssetRef),
    "ToolResult": TypeAdapter(ToolResult),
    "ToolInvocation": TypeAdapter(ToolInvocation),
    "TimelineEntry": _ENTRY_ADAPTER,
    "TimelinePage": TypeAdapter(TimelinePage),
}

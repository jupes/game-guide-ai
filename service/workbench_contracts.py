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
  is closed, and nothing is coerced between JSON types — a ``brief`` of ``42`` is
  an error, not the string ``"42"``. (Within JSON's one number type, ``1.0`` *is*
  the integer ``1``, on both sides, because JavaScript cannot tell them apart.)
  A client, by contrast, tolerates additive fields from a newer server; that
  asymmetry is recorded per example with ``applies_to``.
* **The registry facts below are not the registry.** They are the minimum the
  validators need (which tool lands as which kind, which may run without a
  brief). ``1kg.3.1`` owns the full catalogue and extends the shared
  ``registry.json`` that pins these.

A flat module rather than a subpackage: ``pyproject.toml`` lists packages
explicitly, so a subpackage would be silently dropped from the built image.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

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

#: Ceilings per field kind. ``1kg.5.3`` may set tighter caps per type, and
#: ``1kg.5.5`` a cap on the whole document; both are checked before provider
#: work, so a pasted book is not re-sent on every edit (RAIL-6).
TEXT_FIELD_MAX_CHARS = 200
PROSE_FIELD_MAX_CHARS = 20_000
LIST_FIELD_MAX_ITEMS = 100
LIST_ITEM_MAX_CHARS = 2000
MAX_CHANGED_FIELDS = 64
#: Decision CANVAS-27 pages history by 20 and LIB-23 the library by 25; a page
#: may hold up to 50 so a server can choose a larger page without a new contract.
HISTORY_PAGE_MAX_ITEMS = 50
LIBRARY_PAGE_MAX_ITEMS = 50
#: Decision LIB-20.
SEARCH_MIN_CHARS = 2
SEARCH_MAX_CHARS = 100
#: JavaScript's largest safe integer: a write revision must survive ``JSON.parse``.
WRITE_REVISION_MAX = 2**53 - 1
VERSION_NUMBER_MAX = 1_000_000


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


def _an_integer(value: object) -> object:
    """An integer is any JSON number with an integral value: ``1.0`` is ``1``,
    as it is in JavaScript, which cannot tell the two apart. Nothing else is —
    not ``true`` (to Python ``True == 1``, so a bare ``Literal[1]`` would take
    it; Zod does not) and not ``"1"``."""
    if isinstance(value, bool):
        raise ValueError("must be an integer")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if not isinstance(value, int):
        raise ValueError("must be an integer")
    return value


WireInt = Annotated[int, BeforeValidator(_an_integer)]
SchemaVersion = Annotated[Literal[1], BeforeValidator(_an_integer)]


def _well_formed(value: str) -> str:
    """JSON allows the escape of a lone surrogate; UTF-8 does not, so neither
    does anything that stores or answers. Refused here, it is a 422, not a 500."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("must be well-formed Unicode text") from None
    return value


#: A bounded string refuses a lone surrogate by itself (Pydantic decodes it to
#: count). The three fields bounded only after trimming need the refusal added.
WireText = Annotated[StrictStr, AfterValidator(_well_formed)]

#: What ``String.prototype.trim`` removes, by code point: ASCII whitespace, the
#: Unicode space separators, the line and paragraph separators and the byte
#: order mark. ``str.strip`` differs at the edges — it also takes NEL and the
#: ASCII separators, and leaves the mark — so both sides trim exactly this set.
_TRIMMED = "".join(
    chr(code)
    for code in (
        *range(0x09, 0x0E),
        0x20,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)


def trim(value: str) -> str:
    """Trim as the client does, so the two never disagree about emptiness or length."""
    return value.strip(_TRIMMED)

#: Opaque to clients and base64url, because a cursor may ride in a query string.
#: Search text may not (X-7), which is why a cursor never encodes any.
Cursor = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,512}$")]
#: Client-minted idempotency key of a mutation that has no invocation: creating
#: a document today, reveal and audio commands later (AUDIO-24).
CommandId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{16,64}$")]
#: Decision CANVAS-19: a field is a top-level key of a document type's data — one
#: flat, snake_case namespace. It is the unit of concurrency, of change lists, of
#: an edit's field scope and of a reveal mask. There are no paths into a field.
FieldKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,39}$")]
#: Decision CANVAS-34: the concurrency token. It counts committed writes and is
#: never a history version.
WriteRevision = Annotated[WireInt, Field(ge=1, le=WRITE_REVISION_MAX)]
#: For people, and for pinning a reveal to a sealed version (REVEAL-8).
VersionNumber = Annotated[WireInt, Field(ge=1, le=VERSION_NUMBER_MAX)]


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


class FieldKind(str, Enum):
    """What a document field holds. A kind is a registry fact and never appears
    on the wire: values are bare JSON, and the type's definition says what each
    key must be. ``1kg.5.3`` adds kinds (integers, ability scores, entry lists)
    as it defines the types that need them; that is not a version bump, because
    a client strips a key it does not know."""

    TEXT = "text"
    PROSE = "prose"
    TEXT_LIST = "text_list"
    ASSET = "asset"


class Author(str, Enum):
    """Decision AUD-1: one GM per campaign, and players cannot write."""

    GM = "gm"
    ASSISTANT = "assistant"


class EditAction(str, Enum):
    """The ``SelectionBar``'s complete requests (CANVAS-23)."""

    REWRITE = "rewrite"
    SHORTER = "shorter"
    DARKER = "darker"


class LibrarySort(str, Enum):
    """Decision LIB-22. Each has a stable total order, the id breaking ties."""

    RECENT = "recent"
    NAME = "name"


class EntryKind(str, Enum):
    """Timeline entry kinds. The attached-cue entry joins with the cue family;
    a new kind after v1 ships is a version bump."""

    CHAT = "chat"
    TOOL = "tool"
    EDIT = "edit"
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

#: Every document type has these. ``name`` is the document's title everywhere
#: and the one field that cannot be empty (LIB-12).
COMMON_FIELDS: dict[str, FieldKind] = {
    "name": FieldKind.TEXT,
    "qualifier": FieldKind.TEXT,
    "tags": FieldKind.TEXT_LIST,
}

#: A type's own fields. ``npc`` is the worked example, taken from the handoff in
#: snake_case. ``1kg.5.3`` owns all eight: until it declares a type's fields, that
#: type validates with the common fields only, and everything else fails closed —
#: the same posture as card kinds. Nothing here says who may *see* a field; that
#: is ``agent-forge-harness-1ir.1.2``'s decision.
DOC_TYPE_FIELDS: dict[DocumentTypeId, dict[str, FieldKind]] = {
    **{doc_type: {} for doc_type in DocumentTypeId},
    DocumentTypeId.NPC: {
        "portrait": FieldKind.ASSET,
        "voice": FieldKind.TEXT,
        "tell": FieldKind.TEXT,
        "attitude": FieldKind.TEXT,
        "wants": FieldKind.PROSE,
        "leverage": FieldKind.PROSE,
        "if_attacked": FieldKind.PROSE,
        "notes": FieldKind.PROSE,
    },
}

#: The revision of each type's field definitions. A stored document says which
#: one its data conforms to; ``1kg.5.3`` bumps a type's and supplies the adapter.
DOC_TYPE_VERSION: dict[DocumentTypeId, int] = {doc_type: 1 for doc_type in DocumentTypeId}


# ── Models ───────────────────────────────────────────────────────────────────


class _Contract(BaseModel):
    """Every Workbench payload forbids undeclared fields: a typo, a smuggled
    document body or a remote URL fails instead of riding along.

    Its errors hide their input: ``str(exc)`` says what was wrong, never what
    was sent (X-7). ``exc.errors()`` still carries ``input``; a log line takes
    :func:`redacted_errors` instead."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


#: For an adapter of a union or an annotated type, which has no model config of
#: its own to hide the input with.
_HIDE_INPUT = ConfigDict(hide_input_in_errors=True)


class ConflictInfo(_Contract):
    """What moved, for a document write that lost a race (CANVAS-19, CANVAS-20).

    It names the fields and the revision to rebase on, and **never carries their
    text**: an error body is where logs and traces look (X-7). A client reads the
    latest values through the document endpoint, then offers Keep mine / Use latest.
    """

    write_revision: WriteRevision
    fields: Annotated[list[FieldKey], Field(min_length=1, max_length=MAX_CHANGED_FIELDS)]


class ErrorInfo(_Contract):
    code: ErrorCode
    #: Presentable as-is, and never echoes GM-private text back (X-7).
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    retryable: StrictBool
    #: The request field at fault, for a validation failure.
    field: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)] | None = None
    #: Only for ``throttled_user`` (RAIL-20): the daily cap has no window to wait out.
    retry_after_s: Annotated[WireInt, Field(ge=0, le=86_400)] | None = None
    #: Only for ``cap_reached`` (X-5): what is holding the cap.
    in_flight: Annotated[list[InvocationId], Field(max_length=8)] | None = None
    #: Only for ``conflict`` on a document write or an AI edit.
    conflict: ConflictInfo | None = None


class ErrorBody(_Contract):
    """The Workbench error envelope. It keeps FastAPI's ``detail`` key so that one
    client reader covers a legacy string, a 422 list and this object."""

    detail: ErrorInfo


#: Fixed sentences: a validation answer never interpolates anything it was sent.
_VALIDATION_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.VALIDATION_FAILED: "That request isn't valid.",
    ErrorCode.UNSUPPORTED_SCHEMA_VERSION: "This version of Aetheril is out of date. Reload to update.",
    ErrorCode.UNKNOWN_TOOL: "That tool doesn't exist.",
    ErrorCode.BRIEF_REQUIRED: "Describe what you want first.",
    ErrorCode.BRIEF_TOO_LONG: f"A brief can be at most {BRIEF_MAX_CHARS:,} characters.",
}
_SAFE_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
#: The first element of a FastAPI ``loc``: which part of the request it came from.
_REQUEST_PARTS = frozenset({"body", "query", "path", "header", "cookie"})


def _location(error: Mapping[str, Any]) -> list[Any]:
    loc = list(error.get("loc", ()))
    return loc[1:] if loc and loc[0] in _REQUEST_PARTS else loc


def _is_a_version_error(error: Mapping[str, Any]) -> bool:
    """A well-formed version this server does not speak — the contract's, or a
    document type's field definitions'. A missing or malformed version is an
    ordinary validation failure: there is nothing to be out of date about."""
    kind = error.get("type")
    return (kind == "literal_error" and _location(error) == ["schema_version"]) or kind == "unsupported_type_version"


def _validation_code(error: Mapping[str, Any]) -> tuple[ErrorCode, str | None]:
    kind, loc = error.get("type"), _location(error)
    if kind == "brief_required":
        return ErrorCode.BRIEF_REQUIRED, "brief"
    if kind == "brief_too_long":
        return ErrorCode.BRIEF_TOO_LONG, "brief"
    if loc == ["tool_id"] and kind == "enum":
        return ErrorCode.UNKNOWN_TOOL, "tool_id"
    if kind == "unsupported_type_version":
        return ErrorCode.UNSUPPORTED_SCHEMA_VERSION, "type_version"
    if _is_a_version_error(error):
        return ErrorCode.UNSUPPORTED_SCHEMA_VERSION, "schema_version"
    # An undeclared key is the client's text, not a field name of ours: never echoed.
    named = kind != "extra_forbidden" and loc and isinstance(loc[0], str) and _SAFE_FIELD_NAME.fullmatch(loc[0])
    return ErrorCode.VALIDATION_FAILED, loc[0] if named else None


def validation_error_body(errors: Sequence[Mapping[str, Any]]) -> ErrorBody:
    """The Workbench answer to a request that failed validation (a 422).

    Every Workbench route must answer with this rather than FastAPI's default,
    which returns each error's ``input`` — the request itself, GM-private text
    included (X-7). Pass ``exc.errors()`` from a ``RequestValidationError`` or a
    ``ValidationError``; only ``type`` and ``loc`` are read, and nothing of the
    request is echoed.

    A stale client tends to fail in more than one place at once — the version it
    names and a key it sends — and Pydantic lists the errors in field order. A
    version error wins whatever its position, because the answer to it is *reload*.
    """
    code, field = ErrorCode.VALIDATION_FAILED, None
    first = next((error for error in errors if _is_a_version_error(error)), errors[0] if errors else None)
    if first is not None:
        code, field = _validation_code(first)
    return ErrorBody(detail=ErrorInfo(code=code, message=_VALIDATION_MESSAGES[code], retryable=False, field=field))


def redacted_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A validation error list fit for a log line or a trace: ``type``, ``loc``
    and ``msg`` only.

    ``exc.errors()`` carries each error's ``input`` — the request — and
    ``str(exc)`` prints it: always for FastAPI's ``RequestValidationError``, and
    for a Pydantic error unless its model hides it, as every model here does.
    Neither is ever logged. The location of an undeclared key is that key, which
    is the client's text, so it is replaced as well.
    """
    out: list[dict[str, Any]] = []
    for error in errors:
        loc = list(error.get("loc", ()))
        if error.get("type") == "extra_forbidden" and loc:
            loc[-1] = "(undeclared)"
        out.append({"type": error.get("type"), "loc": loc, "msg": error.get("msg")})
    return out


class ToolInvocationRequest(_Contract):
    """Decisions RAIL-5 to RAIL-9. There is no free-form ``context``: the server
    assembles context from state it has authorised; a client sends ids only."""

    schema_version: SchemaVersion
    invocation_id: InvocationId
    tool_id: ToolId
    #: Always present; a brief-optional tool sends the empty string.
    brief: WireText
    campaign_id: OpaqueId
    conversation_id: OpaqueId
    #: The timeline entry a suggestion was armed from (RAIL-8).
    source_entry_id: OpaqueId | None = None

    @field_validator("brief")
    @classmethod
    def _trim(cls, value: str) -> str:
        return trim(value)

    @model_validator(mode="after")
    def _brief_fits_its_tool(self) -> Self:
        # Typed errors, so that ``validation_error_body`` can answer with the
        # specific code without reading a message.
        if len(self.brief) > BRIEF_MAX_CHARS:
            raise PydanticCustomError(
                "brief_too_long", "a brief can be at most {max} characters", {"max": BRIEF_MAX_CHARS}
            )
        if not self.brief and TOOL_BRIEF_POLICY[self.tool_id] is BriefPolicy.REQUIRED:
            raise PydanticCustomError("brief_required", "this tool needs a brief")
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
    width: Annotated[WireInt, Field(ge=1, le=20_000)] | None = None
    height: Annotated[WireInt, Field(ge=1, le=20_000)] | None = None


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


_PAYLOAD_FOR_STATUS: dict[InvocationStatus, tuple[bool, bool]] = {
    InvocationStatus.WORKING: (False, False),
    InvocationStatus.DONE: (True, False),
    InvocationStatus.FAILED: (False, True),
    InvocationStatus.CANCELLED: (False, False),
}


def _require_matching_payload(status: InvocationStatus, result: object, error: object) -> None:
    """Only ``done`` carries a result and only ``failed`` an error — for a tool
    and for an edit alike — so a stale result can never sit under a working lane."""
    if (result is not None, error is not None) != _PAYLOAD_FOR_STATUS[status]:
        raise ValueError(f"a {status.value} invocation carries the wrong result/error pair")


class ToolInvocation(_Contract):
    """The status resource behind a lane (RAIL-15 to RAIL-27)."""

    schema_version: SchemaVersion
    invocation_id: InvocationId
    tool_id: ToolId
    status: InvocationStatus
    #: Counts from one; a retry of a failed attempt increments it (RAIL-18).
    attempt: Annotated[WireInt, Field(ge=1, le=100)]
    #: With ``status: done`` this is "finished before it could be cancelled" (RAIL-23).
    cancel_requested: StrictBool
    created_at: Timestamp
    updated_at: Timestamp
    result: ToolResult | None
    error: ErrorInfo | None

    @model_validator(mode="after")
    def _status_and_payload_agree(self) -> Self:
        _require_matching_payload(self.status, self.result, self.error)
        if self.result is not None and self.result.tool_id is not self.tool_id:
            raise ValueError("the result belongs to another tool")
        return self


# ── Documents ────────────────────────────────────────────────────────────────
#
# A document's ``data`` is flat: one value per field key. Values are bare JSON;
# what each key must hold is the type's definition (``DOC_TYPE_FIELDS``). The
# server rejects a key the type does not declare. A client strips one instead,
# which is what lets a type gain fields without a version bump.

#: CR, LF, and the Unicode line and paragraph separators, by code point so that
#: no invisible character ever sits in this file.
_LINE_BREAKS = tuple(chr(code) for code in (0x0A, 0x0D, 0x2028, 0x2029))


def _one_line(value: str) -> str:
    """A function rather than a pattern, so that a validation message never has
    to quote an invisible separator."""
    if any(mark in value for mark in _LINE_BREAKS):
        raise ValueError("must be a single line")
    return value


_TextValue = Annotated[str, StringConstraints(strict=True, max_length=TEXT_FIELD_MAX_CHARS), AfterValidator(_one_line)]
_ProseValue = Annotated[str, StringConstraints(strict=True, max_length=PROSE_FIELD_MAX_CHARS)]
_ListItem = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=LIST_ITEM_MAX_CHARS)]
_TextListValue = Annotated[list[_ListItem], Field(max_length=LIST_FIELD_MAX_ITEMS)]

#: Text and prose clear to ``""``, a list to ``[]``, and only an asset to ``None``.
_FIELD_VALUE: dict[FieldKind, TypeAdapter[Any]] = {
    FieldKind.TEXT: TypeAdapter(_TextValue, config=_HIDE_INPUT),
    FieldKind.PROSE: TypeAdapter(_ProseValue, config=_HIDE_INPUT),
    FieldKind.TEXT_LIST: TypeAdapter(_TextListValue, config=_HIDE_INPUT),
    FieldKind.ASSET: TypeAdapter(AssetRef | None, config=_HIDE_INPUT),
}


def check_fields(
    doc_type: DocumentTypeId, type_version: int, fields: dict[str, Any], *, whole: bool
) -> dict[str, Any]:
    """Validate field values against a type's definition, failing closed.

    ``whole`` is a complete document, which must have a name; otherwise ``fields``
    is a patch, which may touch any subset. Messages name keys and kinds, never
    values: they can reach a response body (X-7).
    """
    if type_version != DOC_TYPE_VERSION[doc_type]:
        # Typed, so that ``validation_error_body`` answers "reload" (the client
        # is out of date) rather than "malformed".
        raise PydanticCustomError(
            "unsupported_type_version",
            "{type} field definitions are at version {version}",
            {"type": doc_type.value, "version": DOC_TYPE_VERSION[doc_type]},
        )
    declared = {**COMMON_FIELDS, **DOC_TYPE_FIELDS[doc_type]}
    checked: dict[str, Any] = {}
    for key, value in fields.items():
        kind = declared.get(key)
        if kind is None:
            raise ValueError(f"{doc_type.value} documents do not declare that field")
        try:
            checked[key] = _FIELD_VALUE[kind].validate_python(value)
        except ValidationError as err:
            # ``from None``: a chained cause would put the value in the traceback.
            raise ValueError(f"{key} is not a valid {kind.value} field: {err.errors()[0]['msg']}") from None
    name = checked.get("name")
    if (whole and name is None) or (name is not None and not trim(name)):
        raise ValueError("a document has a name, and it cannot be blank")
    return checked


class DocumentVersion(_Contract):
    """One row of a document's history (CANVAS-27). The handoff's ``label`` and
    display ``time`` are not on the wire; a client derives both."""

    number: VersionNumber
    author: Author
    #: One line. May be empty: a burst of GM autosaves needs no summary.
    summary: _TextValue
    created_at: Timestamp
    #: Decision CANVAS-34: the GM's open working version is not sealed, and only
    #: a sealed version may be pinned by a reveal or exported (REVEAL-8).
    sealed: StrictBool
    #: Against the version before it. It survives the gold wash (CANVAS-29).
    changed_fields: Annotated[list[FieldKey], Field(max_length=MAX_CHANGED_FIELDS)]
    #: Decision CANVAS-26: a restore appends a version equal to an earlier one.
    restored_from: VersionNumber | None

    @model_validator(mode="after")
    def _restores_the_past(self) -> Self:
        if self.restored_from is not None and self.restored_from >= self.number:
            raise ValueError("a version can only be restored from an earlier one")
        return self


class _TypedFields(_Contract):
    type: DocumentTypeId
    type_version: Annotated[WireInt, Field(ge=1, le=1000)]


class Document(_TypedFields):
    """The GM-side read of a document, and the answer to every document write.
    It carries its current version only, never its history, and nothing about
    reveal: that is server state with a resource of its own (CANVAS-33)."""

    schema_version: SchemaVersion
    document_id: OpaqueId
    campaign_id: OpaqueId
    data: dict[str, Any]
    write_revision: WriteRevision
    version: DocumentVersion
    #: Decision LIB-16.
    archived: StrictBool
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def _data_fits_its_type(self) -> Self:
        self.data = check_fields(self.type, self.type_version, self.data, whole=True)
        return self


class DocumentVersionSnapshot(_TypedFields):
    """The content of one version: the history preview, and old text beside new
    when a live reveal is updated (REVEAL-7). It is a read of history, so it
    has no write revision — nobody may base a write on the past."""

    schema_version: SchemaVersion
    document_id: OpaqueId
    version: DocumentVersion
    data: dict[str, Any]

    @model_validator(mode="after")
    def _data_fits_its_type(self) -> Self:
        self.data = check_fields(self.type, self.type_version, self.data, whole=True)
        return self


class DocumentHistoryPage(_Contract):
    """Newest first (CANVAS-27)."""

    schema_version: SchemaVersion
    document_id: OpaqueId
    items: Annotated[list[DocumentVersion], Field(max_length=HISTORY_PAGE_MAX_ITEMS)]
    next_cursor: Cursor | None


class FieldPatchRequest(_TypedFields):
    """Decision CANVAS-10: one autosave. The server compares ``base_write_revision``
    with the last change of each field the patch touches (CANVAS-19); the author
    is always the GM and is never the client's to state. Answered with a
    :class:`Document`, or a 409 whose ``conflict`` names what moved."""

    schema_version: SchemaVersion
    base_write_revision: WriteRevision
    fields: Annotated[dict[str, Any], Field(min_length=1, max_length=MAX_CHANGED_FIELDS)]

    @model_validator(mode="after")
    def _fields_fit_the_type(self) -> Self:
        self.fields = check_fields(self.type, self.type_version, self.fields, whole=False)
        return self


class DocumentCreateRequest(_TypedFields):
    """Decision LIB-12: New in a library category. ``command_id`` makes a retry
    open the document already made instead of making a second one."""

    schema_version: SchemaVersion
    command_id: CommandId
    campaign_id: OpaqueId
    data: dict[str, Any]

    @model_validator(mode="after")
    def _data_fits_its_type(self) -> Self:
        self.data = check_fields(self.type, self.type_version, self.data, whole=True)
        return self


class RestoreRequest(_Contract):
    """Decision CANVAS-26. Additive, so it needs no base revision, and naturally
    idempotent: restoring what the document already equals changes nothing."""

    schema_version: SchemaVersion
    version_number: VersionNumber


# ── AI edits ─────────────────────────────────────────────────────────────────


class DocumentScope(_Contract):
    kind: Literal["document"]


class FieldScope(_Contract):
    kind: Literal["field"]
    field: FieldKey


class SelectionScope(_Contract):
    """A span of one field, in **code points**, with the exact text the GM saw.
    The server refuses a span that no longer matches before any provider work,
    and constrains the model to it (``1kg.5.5``)."""

    kind: Literal["selection"]
    field: FieldKey
    start: Annotated[WireInt, Field(ge=0, le=PROSE_FIELD_MAX_CHARS)]
    end: Annotated[WireInt, Field(ge=1, le=PROSE_FIELD_MAX_CHARS)]
    text: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=PROSE_FIELD_MAX_CHARS)]

    @model_validator(mode="after")
    def _span_and_text_agree(self) -> Self:
        if self.end - self.start != len(self.text):
            raise ValueError("the selected text is not as long as its span")
        return self


class SelectionScopeSummary(_Contract):
    """What the thread keeps of a selection: which field, never the text (EXPORT-12)."""

    kind: Literal["selection"]
    field: FieldKey


EditScope = Annotated[DocumentScope | FieldScope | SelectionScope, Field(discriminator="kind")]
EditScopeSummary = Annotated[DocumentScope | FieldScope | SelectionScopeSummary, Field(discriminator="kind")]


class TextInstruction(_Contract):
    kind: Literal["text"]
    #: Decision RAIL-6: an instruction shares the brief's bound, and is stored trimmed.
    text: WireText

    @field_validator("text")
    @classmethod
    def _trimmed_and_bounded(cls, value: str) -> str:
        trimmed = trim(value)
        if not 1 <= len(trimmed) <= BRIEF_MAX_CHARS:
            raise ValueError(f"an instruction is 1 to {BRIEF_MAX_CHARS} characters")
        return trimmed


class ActionInstruction(_Contract):
    kind: Literal["action"]
    action: EditAction


EditInstruction = Annotated[TextInstruction | ActionInstruction, Field(discriminator="kind")]


def _require_action_on_a_selection(scope_kind: str, instruction: TextInstruction | ActionInstruction) -> None:
    """Decision CANVAS-23: this is what stops a one-line fix rewriting the dossier."""
    if isinstance(instruction, ActionInstruction) and scope_kind != "selection":
        raise ValueError("a SelectionBar action is scoped to a selection")


class EditRequest(_Contract):
    """Decisions CANVAS-21 to CANVAS-25. The same lifecycle and cap as a tool
    (X-5). After a conflict, Try again re-sends the same ``invocation_id`` with a
    fresh ``base_write_revision``; the body of a replay is otherwise ignored."""

    schema_version: SchemaVersion
    invocation_id: InvocationId
    campaign_id: OpaqueId
    conversation_id: OpaqueId
    document_id: OpaqueId
    base_write_revision: WriteRevision
    scope: EditScope
    instruction: EditInstruction

    @model_validator(mode="after")
    def _action_needs_a_selection(self) -> Self:
        _require_action_on_a_selection(self.scope.kind, self.instruction)
        return self


class _EditResultBase(_Contract):
    prose: Annotated[str, StringConstraints(strict=True, max_length=PROSE_MAX_CHARS)]
    suggestions: Annotated[list[ToolSuggestion], Field(max_length=MAX_SUGGESTIONS)]


class EditChanged(_EditResultBase):
    outcome: Literal["changed"]
    #: The lane's badge reads ``EDIT · v<n>`` (CANVAS-24).
    version_number: VersionNumber
    write_revision: WriteRevision
    #: What to wash gold (CANVAS-28). Derived from the committed diff (``1kg.5.5``).
    changed_fields: Annotated[list[FieldKey], Field(min_length=1, max_length=MAX_CHANGED_FIELDS)]


class EditNoChange(_EditResultBase):
    """Decision CANVAS-25: no version is created, so there is none to name."""

    outcome: Literal["no_change"]


#: A conflict is a *failed* invocation, not an outcome.
EditResult = Annotated[EditChanged | EditNoChange, Field(discriminator="outcome")]


class EditInvocation(_Contract):
    """The status resource behind an edit lane. It never carries the document,
    which travels through the document endpoints only."""

    schema_version: SchemaVersion
    invocation_id: InvocationId
    document_id: OpaqueId
    status: InvocationStatus
    attempt: Annotated[WireInt, Field(ge=1, le=100)]
    cancel_requested: StrictBool
    created_at: Timestamp
    updated_at: Timestamp
    result: EditResult | None
    error: ErrorInfo | None

    @model_validator(mode="after")
    def _status_and_payload_agree(self) -> Self:
        _require_matching_payload(self.status, self.result, self.error)
        return self


# ── Campaign Library ─────────────────────────────────────────────────────────


def _require_document_category(category: LibraryCategory) -> None:
    if category is LibraryCategory.CUES:
        raise ValueError("cues are not documents; their listing belongs to the cue family")


class LibraryQuery(_Contract):
    """Decisions LIB-20 to LIB-23. A request *body* even without a search: search
    text may never travel in a URL (X-7), and one shape is simpler than two."""

    schema_version: SchemaVersion
    campaign_id: OpaqueId
    category: LibraryCategory
    #: Trimmed; empty for no search, otherwise 2 to 100 characters.
    search: WireText
    sort: LibrarySort
    archived: StrictBool
    #: Only in Documents, the one category that holds more than one type (LIB-22).
    type: DocumentTypeId | None = None
    cursor: Cursor | None = None
    limit: Annotated[WireInt, Field(ge=1, le=LIBRARY_PAGE_MAX_ITEMS)] | None = None

    @field_validator("search")
    @classmethod
    def _trimmed_and_bounded(cls, value: str) -> str:
        trimmed = trim(value)
        if trimmed and not SEARCH_MIN_CHARS <= len(trimmed) <= SEARCH_MAX_CHARS:
            raise ValueError(f"a search is {SEARCH_MIN_CHARS} to {SEARCH_MAX_CHARS} characters")
        return trimmed

    @model_validator(mode="after")
    def _filters_fit_the_category(self) -> Self:
        _require_document_category(self.category)
        if self.type is not None:
            if self.category is not LibraryCategory.DOCUMENTS:
                raise ValueError("only Documents can be filtered by type")
            if DOC_TYPE_LIBRARY_CATEGORY[self.type] is not self.category:
                raise ValueError(f"{self.type.value} documents do not live in Documents")
        return self


class LibraryItem(_Contract):
    """Enough to list, match and open a document, and nothing of its body."""

    document_id: OpaqueId
    type: DocumentTypeId
    title: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=TEXT_FIELD_MAX_CHARS), AfterValidator(_one_line)
    ]
    qualifier: _TextValue
    tags: _TextListValue
    archived: StrictBool
    updated_at: Timestamp


class LibraryPage(_Contract):
    """It echoes the campaign and category it answers, so a response for a
    campaign the GM has left is dropped and a stale row never flashes (LIB-25)."""

    schema_version: SchemaVersion
    campaign_id: OpaqueId
    category: LibraryCategory
    items: Annotated[list[LibraryItem], Field(max_length=LIBRARY_PAGE_MAX_ITEMS)]
    next_cursor: Cursor | None

    @model_validator(mode="after")
    def _rows_belong_to_the_category(self) -> Self:
        _require_document_category(self.category)
        if any(DOC_TYPE_LIBRARY_CATEGORY[item.type] is not self.category for item in self.items):
            raise ValueError(f"a row does not belong in {self.category.value}")
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


class EditEntry(_EntryBase):
    """An AI edit and its outcome. The GM's words are the turn, as a brief is
    for a tool; the thread keeps the scope's kind and field, and never the
    selected text or the document (EXPORT-12)."""

    entry_kind: Literal["edit"]
    #: The title as it was when the edit was asked for.
    document: DocumentLink
    scope: EditScopeSummary
    instruction: EditInstruction
    invocation: EditInvocation

    @model_validator(mode="after")
    def _agrees_with_itself(self) -> Self:
        if self.document.document_id != self.invocation.document_id:
            raise ValueError("the edit touched another document than the one the entry links")
        _require_action_on_a_selection(self.scope.kind, self.instruction)
        return self


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


AnyEntry = ChatEntry | ToolEntry | EditEntry | SessionDividerEntry | OpaqueEntry
TimelineEntry = Annotated[AnyEntry, Field(discriminator="entry_kind")]


class TimelinePage(_Contract):
    """The pagination envelope's first concrete page. Items run **newest first**
    and ``next_cursor`` leads to older entries."""

    schema_version: SchemaVersion
    conversation_id: OpaqueId
    items: Annotated[list[TimelineEntry], Field(max_length=TIMELINE_PAGE_MAX_ITEMS)]
    #: Required: the end of the list is ``None``, never a missing key.
    next_cursor: Cursor | None


_ENTRY_ADAPTER: TypeAdapter[Any] = TypeAdapter(TimelineEntry, config=_HIDE_INPUT)


def _version_named(value: object) -> int | None:
    """A version is an integral JSON number; ``true``, ``"2"`` and ``1.5`` are not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    return value if isinstance(value, int) else None


def _names_a_newer_version(raw: object) -> bool:
    """Whether the payload's own ``schema_version`` — or its embedded
    invocation's, the one other versioned object an entry holds — is beyond
    this contract. A ``schema_version`` anywhere deeper is content, not a
    version, and does not make a damaged row look like the future."""
    if not isinstance(raw, dict):
        return False
    invocation = raw.get("invocation")
    named = [raw.get("schema_version"), invocation.get("schema_version") if isinstance(invocation, dict) else None]
    return any((version := _version_named(value)) is not None and version > CONTRACT_VERSION for value in named)


#: The row's columns, checked before the payload is read.
_ROW_ID: TypeAdapter[str] = TypeAdapter(OpaqueId, config=_HIDE_INPUT)
_ROW_TIME: TypeAdapter[datetime] = TypeAdapter(Timestamp, config=_HIDE_INPUT)


def _placeholder(entry_id: str, created_at: datetime, reason: OpaqueReason) -> OpaqueEntry:
    return OpaqueEntry(schema_version=1, entry_kind="opaque", entry_id=entry_id, created_at=created_at, reason=reason)


def entry_or_opaque(raw: object, *, entry_id: str, created_at: datetime) -> AnyEntry:
    """The forward-version rule for stored entries, as code (``1kg.4.2`` calls it).

    A payload this server can validate is served. Anything else — a newer
    version's after a rollback, an unknown kind, a damaged row — becomes an
    :class:`OpaqueEntry` in the same place. It is never dropped, never
    rewritten, and never forwarded unvalidated.

    The payload is read the way a client reads a response: **undeclared keys
    are ignored**. The versioning table lets a newer server add an optional
    field without a bump, and after a rollback that field must cost the entry
    nothing. What the server *emits* stays strict; that is the adapter's
    default, and the fixtures pin it.

    ``entry_id`` and ``created_at`` are the row's own columns, validated when
    the row was written. They are what the placeholder is built from, so a row
    whose columns are unusable raises before the payload is read — a storage
    fault, not a version gap. The row is also the authority on identity: a
    payload that validates but names another entry's id is served as
    ``unreadable``.
    """
    _ROW_ID.validate_python(entry_id)
    _ROW_TIME.validate_python(created_at)
    try:
        entry: AnyEntry = _ENTRY_ADAPTER.validate_python(raw, extra="ignore")
    except ValidationError:
        reason = OpaqueReason.NEWER_VERSION if _names_a_newer_version(raw) else OpaqueReason.UNREADABLE
        return _placeholder(entry_id, created_at, reason)
    if entry.entry_id != entry_id:
        return _placeholder(entry_id, created_at, OpaqueReason.UNREADABLE)
    return entry


#: Name → validator, in the order ``contracts/workbench/v1/schemas.json`` lists them.
CONTRACT_SCHEMAS: dict[str, TypeAdapter[Any]] = {
    "Timestamp": TypeAdapter(Timestamp, config=_HIDE_INPUT),
    "ErrorBody": TypeAdapter(ErrorBody),
    "ToolInvocationRequest": TypeAdapter(ToolInvocationRequest),
    "ToolSuggestion": TypeAdapter(ToolSuggestion),
    "DocumentLink": TypeAdapter(DocumentLink),
    "AssetRef": TypeAdapter(AssetRef),
    "ToolResult": TypeAdapter(ToolResult, config=_HIDE_INPUT),
    "ToolInvocation": TypeAdapter(ToolInvocation),
    "DocumentVersion": TypeAdapter(DocumentVersion),
    "Document": TypeAdapter(Document),
    "DocumentVersionSnapshot": TypeAdapter(DocumentVersionSnapshot),
    "DocumentHistoryPage": TypeAdapter(DocumentHistoryPage),
    "FieldPatchRequest": TypeAdapter(FieldPatchRequest),
    "DocumentCreateRequest": TypeAdapter(DocumentCreateRequest),
    "RestoreRequest": TypeAdapter(RestoreRequest),
    "EditRequest": TypeAdapter(EditRequest),
    "EditInvocation": TypeAdapter(EditInvocation),
    "LibraryQuery": TypeAdapter(LibraryQuery),
    "LibraryPage": TypeAdapter(LibraryPage),
    "TimelineEntry": _ENTRY_ADAPTER,
    "TimelinePage": TypeAdapter(TimelinePage),
}

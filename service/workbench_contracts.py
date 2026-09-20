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
#: An ``integer`` field holds a count, a score or a budget, never an id and never
#: a revision: a range a person could type, wide enough for an XP budget.
INTEGER_FIELD_MIN = -1_000_000
INTEGER_FIELD_MAX = 1_000_000
#: A 5e ability score. ``0`` is allowed because a creature can lack an ability
#: outright; the ceiling is well past anything the rules produce.
ABILITY_SCORE_MIN = 0
ABILITY_SCORE_MAX = 99
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
    INTEGER = "integer"
    ABILITIES = "abilities"
    ENTRY_LIST = "entry_list"


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

#: A type's own fields, all eight declared (``1kg.5.3``), in the handoff's keys
#: transliterated to snake_case. A key this does not name fails closed, the same
#: posture as card kinds. Nothing *here* says who may see a field: the per-field
#: rule in ``workbench_registry.py`` does, following ED-5, and the mask that acts
#: on it is ``agent-forge-harness-1kg.1.6``'s.
DOC_TYPE_FIELDS: dict[DocumentTypeId, dict[str, FieldKind]] = {
    DocumentTypeId.NPC: {
        "portrait": FieldKind.ASSET,
        "voice": FieldKind.TEXT,
        "tell": FieldKind.TEXT,
        "attitude": FieldKind.TEXT,
        "wants": FieldKind.PROSE,
        "leverage": FieldKind.PROSE,
        "if_attacked": FieldKind.PROSE,
        "notes": FieldKind.PROSE,
        "true_identity": FieldKind.PROSE,
    },
    DocumentTypeId.STATBLOCK: {
        "ac": FieldKind.INTEGER,
        "ac_note": FieldKind.TEXT,
        "hp": FieldKind.INTEGER,
        "hit_dice": FieldKind.TEXT,
        "speed": FieldKind.TEXT,
        "size": FieldKind.TEXT,
        "creature_type": FieldKind.TEXT,
        "alignment": FieldKind.TEXT,
        "abilities": FieldKind.ABILITIES,
        "saving_throws": FieldKind.TEXT,
        "skills": FieldKind.TEXT,
        "damage_immunities": FieldKind.TEXT,
        "condition_immunities": FieldKind.TEXT,
        "senses": FieldKind.TEXT,
        "languages": FieldKind.TEXT,
        "challenge_rating": FieldKind.TEXT,
        "xp": FieldKind.INTEGER,
        "traits": FieldKind.ENTRY_LIST,
        "actions": FieldKind.ENTRY_LIST,
        "bonus_actions": FieldKind.ENTRY_LIST,
        "reactions": FieldKind.ENTRY_LIST,
        "legendary_actions": FieldKind.ENTRY_LIST,
    },
    DocumentTypeId.HANDOUT: {
        "portrait": FieldKind.ASSET,
        "body": FieldKind.PROSE,
    },
    DocumentTypeId.SESSION_NOTES: {
        "session": FieldKind.INTEGER,
        "date": FieldKind.TEXT,
        "present": FieldKind.TEXT_LIST,
        "recap": FieldKind.PROSE,
        "beats": FieldKind.TEXT_LIST,
        "loose_threads": FieldKind.TEXT_LIST,
    },
    DocumentTypeId.QUEST_LOG: {
        "open_threads": FieldKind.ENTRY_LIST,
        "cold_threads": FieldKind.ENTRY_LIST,
        "resolved_threads": FieldKind.ENTRY_LIST,
    },
    DocumentTypeId.CHARACTER_SHEET: {
        "portrait": FieldKind.ASSET,
        "ac": FieldKind.INTEGER,
        "hp": FieldKind.INTEGER,
        "speed": FieldKind.TEXT,
        "abilities": FieldKind.ABILITIES,
        "features": FieldKind.ENTRY_LIST,
        "equipment": FieldKind.TEXT_LIST,
        "notes": FieldKind.PROSE,
    },
    DocumentTypeId.LORE: {
        "region": FieldKind.TEXT,
        "era": FieldKind.TEXT,
        "status": FieldKind.TEXT,
        "summary": FieldKind.PROSE,
        "history": FieldKind.PROSE,
        "rumours": FieldKind.TEXT_LIST,
    },
    DocumentTypeId.ENCOUNTER: {
        "difficulty": FieldKind.TEXT,
        "xp_budget": FieldKind.INTEGER,
        "party_level": FieldKind.INTEGER,
        "setup": FieldKind.PROSE,
        "combatants": FieldKind.ENTRY_LIST,
        "terrain": FieldKind.PROSE,
        "outcome": FieldKind.PROSE,
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
_IntegerValue = Annotated[WireInt, Field(ge=INTEGER_FIELD_MIN, le=INTEGER_FIELD_MAX)]

#: The six 5e ability scores, as a **mapping with a closed key set** rather than a
#: model: ``Abilities`` in ``models.py`` must spell ``int`` as ``int_`` with an
#: alias, and an alias is the kind of asymmetry the differential fuzz exists to
#: find. Any subset may be given; an unknown key is refused on both sides.
AbilityKey = Literal["str", "dex", "con", "int", "wis", "cha"]
#: Registry order, for a client that lays the block out.
ABILITY_KEYS: tuple[str, ...] = ("str", "dex", "con", "int", "wis", "cha")
_AbilityScore = Annotated[WireInt, Field(ge=ABILITY_SCORE_MIN, le=ABILITY_SCORE_MAX)]
_AbilitiesValue = dict[AbilityKey, _AbilityScore | None] | None


class _Entry(_Contract):
    """One named block of a stat block or a quest log — the name is rendered as a
    heading, the text is its body. Plain text on both (X-10)."""

    name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=TEXT_FIELD_MAX_CHARS)]
    text: Annotated[str, StringConstraints(strict=True, max_length=LIST_ITEM_MAX_CHARS)]

    @field_validator("name")
    @classmethod
    def _name_is_one_line(cls, value: str) -> str:
        return _one_line(value)


_EntryListValue = Annotated[list[_Entry], Field(max_length=LIST_FIELD_MAX_ITEMS)]

#: Text and prose clear to ``""``, a list to ``[]``, and an asset, an integer and
#: an ability block to ``None``.
_FIELD_VALUE: dict[FieldKind, TypeAdapter[Any]] = {
    FieldKind.TEXT: TypeAdapter(_TextValue, config=_HIDE_INPUT),
    FieldKind.PROSE: TypeAdapter(_ProseValue, config=_HIDE_INPUT),
    FieldKind.TEXT_LIST: TypeAdapter(_TextListValue, config=_HIDE_INPUT),
    FieldKind.ASSET: TypeAdapter(AssetRef | None, config=_HIDE_INPUT),
    FieldKind.INTEGER: TypeAdapter(_IntegerValue | None, config=_HIDE_INPUT),
    FieldKind.ABILITIES: TypeAdapter(_AbilitiesValue, config=_HIDE_INPUT),
    FieldKind.ENTRY_LIST: TypeAdapter(_EntryListValue, config=_HIDE_INPUT),
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


# ── Media assets and cues ────────────────────────────────────────────────────
#
# Bytes travel in two steps (``docs/adr/gm-workbench-media-and-realtime.md``,
# MS-3 to MS-5): a JSON request creates the asset and reserves its declared size
# against the quota, then one raw request — ``Content-Type`` the declared media
# type, ``Content-Length`` required — carries the bytes. The state machine and
# the caps are the ADR's and the threat model's (SEC-26); the shapes are here.

IMAGE_MAX_BYTES = 10_000_000
AUDIO_MAX_BYTES = 20_000_000
IMAGE_MAX_SIDE = 8_192
IMAGE_MAX_PIXELS = 25_000_000
AMBIENCE_MAX_MS = 600_000
ONE_SHOT_MAX_MS = 30_000
ALT_MAX_CHARS = 300
CUE_TITLE_MAX_CHARS = 200
CUE_PAGE_MAX_ITEMS = 50
PRESENCE_MAX_PARTICIPANTS = 100
#: ``secrets.token_urlsafe(32)``: 32 CSPRNG bytes are 43 base64url characters (SEC-5).
TABLE_SECRET_CHARS = 43


class AssetKind(str, Enum):
    IMAGE = "image"
    AUDIO = "audio"


class AssetState(str, Enum):
    """ADR MS-3. ``deleted`` is a tombstone and is never served."""

    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class AssetFailure(str, Enum):
    """Closed and free of user text, so a reason is safe as a metric label."""

    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    TOO_MANY_PIXELS = "too_many_pixels"
    TOO_LONG = "too_long"
    UNREADABLE = "unreadable"
    TIMED_OUT = "timed_out"
    QUOTA_EXCEEDED = "quota_exceeded"


#: What a kind accepts, judged by magic bytes on upload (SEC-25). Audio is
#: transcoded to ``audio/mpeg`` (ADR MS-6), so a ready audio asset is always that.
MEDIA_TYPES: dict[AssetKind, tuple[str, ...]] = {
    AssetKind.IMAGE: ("image/png", "image/jpeg", "image/webp"),
    AssetKind.AUDIO: ("audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav"),
}
ASSET_MAX_BYTES: dict[AssetKind, int] = {AssetKind.IMAGE: IMAGE_MAX_BYTES, AssetKind.AUDIO: AUDIO_MAX_BYTES}

MediaType = Annotated[str, StringConstraints(strict=True, pattern=r"^(image|audio)/[a-z0-9.+-]{1,32}$")]
AltText = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=ALT_MAX_CHARS), AfterValidator(_one_line)
]
Pixels = Annotated[WireInt, Field(ge=1, le=IMAGE_MAX_SIDE)]
DurationMs = Annotated[WireInt, Field(ge=1, le=AMBIENCE_MAX_MS)]


def _media_type_fits(kind: AssetKind, media_type: str) -> None:
    if media_type not in MEDIA_TYPES[kind]:
        raise ValueError(f"{media_type} is not a type this contract accepts for {kind.value}")


def _alt_fits(kind: AssetKind, alt: str | None) -> None:
    """An image needs alt text (X-10 renders it, and a screen reader hears it);
    an audio asset carries no text at all — its title is the cue's (AUDIO-29)."""
    if (kind is AssetKind.IMAGE) != (alt is not None):
        raise ValueError("an image has alt text and an audio asset has none")


class AssetCreateRequest(_Contract):
    """Step one of an upload: what is coming, so that the quota (SEC-31) and the
    caps (SEC-26) are checked before a byte is accepted (ADR MS-4)."""

    schema_version: SchemaVersion
    command_id: CommandId
    campaign_id: OpaqueId
    kind: AssetKind
    media_type: MediaType
    size_bytes: Annotated[WireInt, Field(ge=1, le=AUDIO_MAX_BYTES)]
    alt: AltText | None = None

    @model_validator(mode="after")
    def _fits_its_kind(self) -> Self:
        _media_type_fits(self.kind, self.media_type)
        _alt_fits(self.kind, self.alt)
        if self.size_bytes > ASSET_MAX_BYTES[self.kind]:
            raise ValueError(f"{self.kind.value} assets are at most {ASSET_MAX_BYTES[self.kind]:,} bytes")
        return self


class Asset(_Contract):
    """The GM-side asset resource. Dimensions and duration exist only once the
    bytes are processed; a failure carries a closed reason and never a message."""

    schema_version: SchemaVersion
    asset_id: OpaqueId
    campaign_id: OpaqueId
    kind: AssetKind
    state: AssetState
    #: Declared until ``ready``, then the type the server recorded (SEC-19).
    media_type: MediaType
    #: Declared until ``ready``, then real.
    size_bytes: Annotated[WireInt, Field(ge=1, le=AUDIO_MAX_BYTES)]
    width: Pixels | None
    height: Pixels | None
    duration_ms: DurationMs | None
    alt: AltText | None
    failure: AssetFailure | None
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def _agrees_with_its_state(self) -> Self:
        _media_type_fits(self.kind, self.media_type)
        _alt_fits(self.kind, self.alt)
        if (self.failure is not None) != (self.state is AssetState.FAILED):
            raise ValueError("only a failed asset carries a failure, and every failed asset does")
        ready = self.state is AssetState.READY
        measured_image = self.width is not None and self.height is not None
        if (self.kind is AssetKind.IMAGE and ready) != measured_image or (self.kind is AssetKind.AUDIO and ready) != (
            self.duration_ms is not None
        ):
            raise ValueError(
                "a ready image has its dimensions and a ready audio asset its duration; nothing else has them"
            )
        if measured_image and (self.width or 0) * (self.height or 0) > IMAGE_MAX_PIXELS:
            raise ValueError(f"an image is at most {IMAGE_MAX_PIXELS:,} pixels")
        if ready and self.kind is AssetKind.AUDIO and self.media_type != "audio/mpeg":
            raise ValueError("processed audio is audio/mpeg")
        return self


class TableAssetRef(_Contract):
    """What a table client is given instead of an asset id (SEC-15): a per-slot
    handle that dies with its slot, the type, and what a player needs to lay it
    out — never a title, a filename or the GM-side id."""

    handle: OpaqueId
    kind: AssetKind
    media_type: MediaType
    width: Pixels | None
    height: Pixels | None
    duration_ms: DurationMs | None

    @model_validator(mode="after")
    def _measured_for_its_kind(self) -> Self:
        _media_type_fits(self.kind, self.media_type)
        image = self.kind is AssetKind.IMAGE
        if image != (self.width is not None and self.height is not None) or image == (self.duration_ms is not None):
            raise ValueError("an image handle carries its dimensions and an audio handle its duration")
        return self


class CueKind(str, Enum):
    """Decision AUDIO-1: one ambience slot, one one-shot slot. A cue's kind is
    immutable after upload (AUDIO-4)."""

    AMBIENCE = "ambience"
    ONE_SHOT = "one_shot"


CUE_MAX_MS: dict[CueKind, int] = {CueKind.AMBIENCE: AMBIENCE_MAX_MS, CueKind.ONE_SHOT: ONE_SHOT_MAX_MS}
CueTitle = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=CUE_TITLE_MAX_CHARS), AfterValidator(_one_line)
]


class Cue(_Contract):
    """A cue record (LIB-26): a title and a kind over a ready audio asset. The
    GM's own resource; a table client never sees one (AUDIO-29)."""

    schema_version: SchemaVersion
    cue_id: OpaqueId
    campaign_id: OpaqueId
    title: CueTitle
    kind: CueKind
    asset_id: OpaqueId
    duration_ms: DurationMs
    archived: StrictBool
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def _fits_its_kind(self) -> Self:
        if self.duration_ms > CUE_MAX_MS[self.kind]:
            raise ValueError(f"a {self.kind.value} cue is at most {CUE_MAX_MS[self.kind]:,} ms")
        return self


class CueCreateRequest(_Contract):
    """Upload takes a title and a kind (LIB-26). The asset must be ready audio of
    a length the kind allows — a state check the route makes (AUDIO-26)."""

    schema_version: SchemaVersion
    command_id: CommandId
    campaign_id: OpaqueId
    asset_id: OpaqueId
    title: CueTitle
    kind: CueKind


class CueRenameRequest(_Contract):
    """The title can be edited; the kind cannot (AUDIO-4), so it is not here."""

    schema_version: SchemaVersion
    title: CueTitle


class CueListQuery(_Contract):
    """The Cues category of the library (LIB-5, LIB-26), with the library's search
    and sort rules; a request body for the same reason as :class:`LibraryQuery`."""

    schema_version: SchemaVersion
    campaign_id: OpaqueId
    search: WireText
    sort: LibrarySort
    archived: StrictBool
    cursor: Cursor | None = None
    limit: Annotated[WireInt, Field(ge=1, le=CUE_PAGE_MAX_ITEMS)] | None = None

    @field_validator("search")
    @classmethod
    def _trimmed_and_bounded(cls, value: str) -> str:
        trimmed = trim(value)
        if trimmed and not SEARCH_MIN_CHARS <= len(trimmed) <= SEARCH_MAX_CHARS:
            raise ValueError(f"a search is {SEARCH_MIN_CHARS} to {SEARCH_MAX_CHARS} characters")
        return trimmed


class CuePage(_Contract):
    schema_version: SchemaVersion
    campaign_id: OpaqueId
    items: Annotated[list[Cue], Field(max_length=CUE_PAGE_MAX_ITEMS)]
    next_cursor: Cursor | None


class AudioSlot(str, Enum):
    AMBIENCE = "ambience"
    ONE_SHOT = "one_shot"


#: Decision AUDIO-8: a push always starts at zero; the field stays for forward
#: compatibility, and today only zero is a valid value.
StartOffsetMs = Annotated[Literal[0], BeforeValidator(_an_integer)]
#: The audio epoch (AUDIO-28): every Stop and every committed push advances it.
AudioEpoch = Annotated[WireInt, Field(ge=0, le=WRITE_REVISION_MAX)]
#: A slot's sequence (AUDIO-15): per slot, monotonic, assigned by the database.
SlotSequence = Annotated[WireInt, Field(ge=0, le=WRITE_REVISION_MAX)]
#: A link generation (SEC-9): counts rotations, and every frame names the one it was produced under.
LinkGeneration = Annotated[WireInt, Field(ge=1, le=WRITE_REVISION_MAX)]


class CuePlayRequest(_Contract):
    """Play to table (AUDIO-8). Idempotent by ``command_id``; a stale epoch is a
    409 (AUDIO-28). ``loop`` on a one-shot is refused against the cue's kind by
    the route (AUDIO-3)."""

    schema_version: SchemaVersion
    command_id: CommandId
    cue_id: OpaqueId
    audio_epoch: AudioEpoch
    loop: StrictBool
    start_offset_ms: StartOffsetMs = 0


class CueStopRequest(_Contract):
    """Decision AUDIO-9: a card's Stop names its cue and clears a slot only if that
    cue still holds it; the strip's Stop is Stop all (``cue_id`` null). A Stop
    carries no epoch and is never queued (X-3)."""

    schema_version: SchemaVersion
    command_id: CommandId
    cue_id: OpaqueId | None


# ── Table sessions ───────────────────────────────────────────────────────────

#: A table token or an enrolment code as it travels — once, in a POST body (SEC-8, SEC-11).
TableSecret = Annotated[str, StringConstraints(strict=True, pattern=rf"^[A-Za-z0-9_-]{{{TABLE_SECRET_CHARS}}}$")]


class TableRole(str, Enum):
    PARTICIPANT = "participant"
    GUEST = "guest"


class JoinStatus(str, Enum):
    """``inactive`` is the one answer for a wrong, ended, expired or rotated token
    (TABLE-9); ``full`` is the device bound (SEC-10)."""

    JOINED = "joined"
    FULL = "full"
    INACTIVE = "inactive"


class TableJoinRequest(_Contract):
    schema_version: SchemaVersion
    token: TableSecret


class TableJoinResponse(_Contract):
    """One shape for every outcome, and the role the device joined with — a
    participant, or a guest with TABLE-13's line — only when it joined."""

    schema_version: SchemaVersion
    status: JoinStatus
    role: TableRole | None

    @model_validator(mode="after")
    def _role_only_when_joined(self) -> Self:
        if (self.role is not None) != (self.status is JoinStatus.JOINED):
            raise ValueError("a role comes with a join, and only with a join")
        return self


class EnrolStatus(str, Enum):
    """``inactive`` covers used, replaced, expired and invalid alike (TABLE-16)."""

    ENROLLED = "enrolled"
    INACTIVE = "inactive"


class EnrolRequest(_Contract):
    schema_version: SchemaVersion
    code: TableSecret


class EnrolResponse(_Contract):
    schema_version: SchemaVersion
    status: EnrolStatus


class SessionState(str, Enum):
    LIVE = "live"
    ENDED = "ended"


class TableSession(_Contract):
    """The GM's view of a session (REVEAL-2, REVEAL-17): its state, its link
    generation, when it ends, whether table audio is on, and how many devices
    hold a credential (SEC-10). The token itself is not here — it travels once."""

    schema_version: SchemaVersion
    session_id: OpaqueId
    campaign_id: OpaqueId
    state: SessionState
    gen: LinkGeneration
    #: Decision AUDIO-24: two GM tabs converge on the epoch the resource carries.
    audio_epoch: AudioEpoch
    started_at: Timestamp
    ends_at: Timestamp
    ended_at: Timestamp | None
    audio: StrictBool
    devices: Annotated[WireInt, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def _times_agree(self) -> Self:
        if (self.ended_at is not None) != (self.state is SessionState.ENDED):
            raise ValueError("an ended session says when, and a live one does not")
        if self.ends_at <= self.started_at:
            raise ValueError("a session ends after it starts")
        return self


class SessionAction(str, Enum):
    START = "start"
    END = "end"
    ROTATE = "rotate"


class TableSessionRequest(_Contract):
    """Start, End and Rotate (REVEAL-17), idempotent by ``command_id``. Rotate may
    also reset every personal link; nothing else may."""

    schema_version: SchemaVersion
    command_id: CommandId
    campaign_id: OpaqueId
    action: SessionAction
    reset_personal_links: StrictBool = False

    @model_validator(mode="after")
    def _reset_only_with_rotate(self) -> Self:
        if self.reset_personal_links and self.action is not SessionAction.ROTATE:
            raise ValueError("personal links are reset with a rotation, not with a start or an end")
        return self


class TableSessionAnswer(_Contract):
    """The answer to a session request. A start or a rotation carries the new
    table token — the one time it is in a body (SEC-8) — and an end carries none."""

    schema_version: SchemaVersion
    session: TableSession
    token: TableSecret | None

    @model_validator(mode="after")
    def _token_only_while_live(self) -> Self:
        if (self.token is not None) != (self.session.state is SessionState.LIVE):
            raise ValueError("a live session answers with its token, and an ended one with none")
        return self


class Capabilities(_Contract):
    """What the deployment has switched on (RAIL-10, AE-58): the answer to the
    lookup a GM client makes once per load. Every switch is off until the owner
    approves a provider (record 3.3). A newer server may add a switch; a client
    strips it, because its registry cannot name a tool for it."""

    schema_version: SchemaVersion
    image_generation: StrictBool
    audio_cues: StrictBool


# ── Realtime events ──────────────────────────────────────────────────────────
#
# Two channels, two unions (ADR RT-1, threat model 8.3): the GM channel carries
# lane status, sessions, audio with titles, and presence with aliases; the table
# channel carries the session, audio by handle, and nothing that names anyone.
# Every frame carries its own ``schema_version``. The heartbeat is an SSE comment,
# not an event. The ``snapshot`` and ``slot`` kinds arrive with the reveal family
# (``agent-forge-harness-1ir.1.2`` first); until v1 is declared complete, adding
# them is not a version bump.


class _EventBase(_Contract):
    schema_version: SchemaVersion


class ToolLaneEvent(_EventBase):
    """A tool lane's status changed (RAIL-15 to RAIL-27)."""

    event: Literal["tool_lane"]
    conversation_id: OpaqueId
    entry_id: OpaqueId
    invocation: ToolInvocation


class EditLaneEvent(_EventBase):
    event: Literal["edit_lane"]
    conversation_id: OpaqueId
    entry_id: OpaqueId
    invocation: EditInvocation


class GmSessionEvent(_EventBase):
    event: Literal["session"]
    session: TableSession


class GmPlaying(_Contract):
    """What a slot holds, as the GM sees it: the cue by id and title (AUDIO-11)."""

    cue_id: OpaqueId
    title: CueTitle
    started_at: Timestamp
    start_offset_ms: StartOffsetMs
    loop: StrictBool
    duration_ms: DurationMs


def _playing_fits_its_slot(slot: AudioSlot, loop: bool, duration_ms: int) -> None:
    """Decision AUDIO-3: a one-shot never loops, and is at most 30 s."""
    if slot is AudioSlot.ONE_SHOT and (loop or duration_ms > ONE_SHOT_MAX_MS):
        raise ValueError("a one-shot never loops and is at most 30 seconds")


class GmAudioEvent(_EventBase):
    event: Literal["audio"]
    session_id: OpaqueId
    gen: LinkGeneration
    audio_epoch: AudioEpoch
    slot: AudioSlot
    seq: SlotSequence
    playing: GmPlaying | None

    @model_validator(mode="after")
    def _fits_its_slot(self) -> Self:
        if self.playing is not None:
            _playing_fits_its_slot(self.slot, self.playing.loop, self.playing.duration_ms)
        return self


class PresenceAudio(str, Enum):
    """Decision AUDIO-21, AUDIO-22: listening means playing and unmuted."""

    LISTENING = "listening"
    MUTED = "muted"
    PENDING = "pending"
    ABSENT = "absent"


class ParticipantPresence(_Contract):
    participant_id: OpaqueId
    #: Decision AUD-11: an alias travels only on the GM's channel.
    alias: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=60), AfterValidator(_one_line)]
    audio: PresenceAudio


class GuestPresence(_Contract):
    """Guests are counted, never named (AUDIO-21)."""

    connected: Annotated[WireInt, Field(ge=0, le=1000)]
    listening: Annotated[WireInt, Field(ge=0, le=1000)]
    muted: Annotated[WireInt, Field(ge=0, le=1000)]
    pending: Annotated[WireInt, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def _adds_up(self) -> Self:
        if self.listening + self.muted + self.pending > self.connected:
            raise ValueError("guest states cannot exceed the guests connected")
        return self


class PresenceEvent(_EventBase):
    event: Literal["presence"]
    session_id: OpaqueId
    gen: LinkGeneration
    participants: Annotated[list[ParticipantPresence], Field(max_length=PRESENCE_MAX_PARTICIPANTS)]
    guests: GuestPresence


class GmAssetEvent(_EventBase):
    """An asset changed state (ADR MS-3): the GM's `Still processing…` ends here."""

    event: Literal["asset"]
    asset: Asset


class GmReadyEvent(_EventBase):
    """The snapshot is complete; what follows is live (ADR RT-4). This is the
    boundary TABLE-7's *fresh snapshot* needs."""

    event: Literal["ready"]


class GmReconnectEvent(_EventBase):
    """The server is closing this stream on purpose (ADR RT-3); reopen with backoff."""

    event: Literal["reconnect"]


GmEvent = Annotated[
    ToolLaneEvent
    | EditLaneEvent
    | GmSessionEvent
    | GmAudioEvent
    | PresenceEvent
    | GmAssetEvent
    | GmReadyEvent
    | GmReconnectEvent,
    Field(discriminator="event"),
]


def _ends_with_ready(frames: Sequence[Any]) -> None:
    """A snapshot ends with ``ready`` and holds no ``reconnect``: ``ready`` is the
    boundary a client trusts nothing before (TABLE-7), and a reconnect belongs to
    a stream, never to a resource."""
    if not frames or frames[-1].event != "ready":
        raise ValueError("a snapshot ends with ready")
    if any(frame.event == "reconnect" for frame in frames):
        raise ValueError("a snapshot never carries a reconnect")


class GmSnapshot(_Contract):
    """The GM channel read as a resource — a stream's opening frames, and the
    polling mode of ADR RT-9 — ending with ``ready``."""

    schema_version: SchemaVersion
    frames: Annotated[list[GmEvent], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        _ends_with_ready(self.frames)
        return self


class TableSessionEvent(_EventBase):
    """A live session as a table client may know it: whether table audio is on
    (AUDIO-19) and this device's own role. The link generation a frame was
    produced under is the server's to check, never the client's to see (SEC-15)."""

    event: Literal["session"]
    audio: StrictBool
    role: TableRole


class TableInactiveEvent(_EventBase):
    """Ended, expired or rotated: one generic event, then the connection closes
    (TABLE-9, SEC-9). It never says which."""

    event: Literal["inactive"]


class TablePlaying(_Contract):
    """What a slot holds, as a table client sees it: a handle, never a title (AUDIO-29)."""

    asset: TableAssetRef
    started_at: Timestamp
    start_offset_ms: StartOffsetMs
    loop: StrictBool
    duration_ms: DurationMs

    @model_validator(mode="after")
    def _is_audio(self) -> Self:
        if self.asset.kind is not AssetKind.AUDIO:
            raise ValueError("a slot plays audio")
        return self


class TableAudioEvent(_EventBase):
    event: Literal["audio"]
    slot: AudioSlot
    seq: SlotSequence
    playing: TablePlaying | None

    @model_validator(mode="after")
    def _fits_its_slot(self) -> Self:
        if self.playing is not None:
            _playing_fits_its_slot(self.slot, self.playing.loop, self.playing.duration_ms)
        return self


class TableReadyEvent(_EventBase):
    event: Literal["ready"]


class TableReconnectEvent(_EventBase):
    event: Literal["reconnect"]


TableEvent = Annotated[
    TableSessionEvent | TableInactiveEvent | TableAudioEvent | TableReadyEvent | TableReconnectEvent,
    Field(discriminator="event"),
]


class TableSnapshot(_Contract):
    """The table channel read as a resource: the session, one audio frame per
    slot, later the reveal slots, then ``ready`` (ADR RT-4, RT-9)."""

    schema_version: SchemaVersion
    frames: Annotated[list[TableEvent], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        _ends_with_ready(self.frames)
        return self


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
    "AssetCreateRequest": TypeAdapter(AssetCreateRequest),
    "Asset": TypeAdapter(Asset),
    "TableAssetRef": TypeAdapter(TableAssetRef),
    "Cue": TypeAdapter(Cue),
    "CueCreateRequest": TypeAdapter(CueCreateRequest),
    "CueRenameRequest": TypeAdapter(CueRenameRequest),
    "CueListQuery": TypeAdapter(CueListQuery),
    "CuePage": TypeAdapter(CuePage),
    "CuePlayRequest": TypeAdapter(CuePlayRequest),
    "CueStopRequest": TypeAdapter(CueStopRequest),
    "TableJoinRequest": TypeAdapter(TableJoinRequest),
    "TableJoinResponse": TypeAdapter(TableJoinResponse),
    "EnrolRequest": TypeAdapter(EnrolRequest),
    "EnrolResponse": TypeAdapter(EnrolResponse),
    "TableSession": TypeAdapter(TableSession),
    "TableSessionRequest": TypeAdapter(TableSessionRequest),
    "TableSessionAnswer": TypeAdapter(TableSessionAnswer),
    "Capabilities": TypeAdapter(Capabilities),
    "GmEvent": TypeAdapter(GmEvent, config=_HIDE_INPUT),
    "TableEvent": TypeAdapter(TableEvent, config=_HIDE_INPUT),
    "GmSnapshot": TypeAdapter(GmSnapshot),
    "TableSnapshot": TypeAdapter(TableSnapshot),
}

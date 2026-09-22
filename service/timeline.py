"""The conversation timeline's read model (1kg.4.2).

A conversation is read as a list of **exchanges**, newest first: a turn carries
its own outcome, so a page boundary can never separate a prompt from its result
(`docs/workbench-wire-contract.md`, *The timeline family*).

Three pieces, all of them free of SQL — every statement lives in
`service/timeline_store.py`:

* **The legacy adapter** (requirement 6). A pure function over a run of
  `chat.messages` rows. A `user` row opens an exchange and the next `assistant`
  row completes it; an exchange is therefore one or two rows, never more. What
  a legacy row cannot know it says with `null`: `answerable` and `sources` are
  **never** `true` and never `[]` for an adapted row, because the client used
  to invent both and the contract's *not recorded* is the honest answer.
* **The merge and the cursor** (requirement 7). Each source contributes a
  bounded window under the cursor predicate; the two are merged in Python by
  one total ordering key and the page is the first `limit` of them. The cursor
  carries **each source's position independently**, because an item read but
  not taken from one source must be re-read on the next page.
* **The route's read-only authorization** (requirement 9). `owner_of`, and
  nothing else. It deliberately does **not** reuse `_authorize_conversation`:
  that helper answers 403 and, on a read, *claims* an unowned conversation that
  has content, and the threat model's §8.1 row for this route forbids both
  (`| Conversation index and metadata; the timeline | yes | 404 (never a claim,
  SEC-2) | — | 401 |`). `GET …/messages` keeps its 403 and its claim, untouched
  (R-4, R-5).

**This slice serves legacy rows only.** Slice A adds `chat.timeline_entries`
and a second source; the ordering key and the cursor are already the merged
form, so it adds a source and never a second format.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from .timeline_store import LegacyMessageRow, TimelineStore
from .workbench_contracts import (
    CONTRACT_VERSION,
    TIMELINE_PAGE_MAX_ITEMS,
    AnyEntry,
    Cursor,
    ErrorBody,
    ErrorCode,
    ErrorInfo,
    OpaqueId,
    SchemaVersion,
    TimelinePage,
    entry_or_opaque,
    validation_error_body,
)

#: The contract's page maximum is also the default: a client that asks for
#: nothing gets as much of the conversation as one page may hold.
DEFAULT_PAGE_LIMIT = TIMELINE_PAGE_MAX_ITEMS
#: Bumped only if the encoding below changes shape. A cursor naming any other
#: version is refused rather than guessed at.
CURSOR_VERSION = 1
#: The wire field is `Literal[1]` while `CONTRACT_VERSION` types as a plain
#: `int`, so the version is still spelled once and the cast is the only bridge.
#: Nothing rests on the cast being right: the page is validated on the way out
#: by `TimelinePage` itself, and a `CONTRACT_VERSION` the field does not admit
#: fails there — which is what `test_every_page_the_route_emits_validates…`
#: exercises.
PAGE_SCHEMA_VERSION = cast("SchemaVersion", CONTRACT_VERSION)

#: Fixed sentences. A refusal never interpolates anything it was sent (X-7).
NOT_FOUND_MESSAGE = "That conversation isn't available."
FORBIDDEN_MESSAGE = "This is a Game Master feature."
UNAVAILABLE_MESSAGE = "The conversation timeline is briefly unavailable. Try again."


# ── Refusals ─────────────────────────────────────────────────────────────────


class TimelineRefusal(Exception):
    """What the read model refuses. Each subclass maps to one status."""


class ConversationNotFound(TimelineRefusal, LookupError):
    """Missing, unowned, or another user's — one refusal for all three, raised
    from one code path, so the route cannot become an enumeration oracle
    (SEC-3). It is a `LookupError` too, because that is what "no such row"
    means in the standard exceptions."""


class ParameterRefused(TimelineRefusal):
    """A query parameter this route cannot read. It names the field and never
    the value (SEC-23, R-12)."""

    def __init__(self, field: str) -> None:
        super().__init__(f"the {field} parameter is not valid")
        self.field = field


class CursorUnreadable(ValueError):
    """A cursor this server did not mint, or one from another version. Raised
    below `ParameterRefused`, which is what a route answers with."""


def parameter_error_body(field: str) -> ErrorBody:
    """The 422 body for a malformed `limit` or `cursor`.

    Built through `validation_error_body`, which reads only an error's type and
    location, uses fixed sentences and echoes nothing of the request — the
    error list handed to it therefore carries no `input` at all.
    """
    return validation_error_body([{"type": "value_error", "loc": ("query", field), "msg": "invalid"}])


def error_body(code: ErrorCode, message: str, *, retryable: bool) -> ErrorBody:
    return ErrorBody(detail=ErrorInfo(code=code, message=message, retryable=retryable))


# ── Requirement 6: adapting a run of legacy message rows ─────────────────────


@dataclass(frozen=True)
class Exchange:
    """One or two `chat.messages` rows: a prompt, its answer, or one alone."""

    prompt: LegacyMessageRow | None
    answer: LegacyMessageRow | None

    @property
    def anchor(self) -> LegacyMessageRow:
        """The **oldest** row of the exchange — the `user` row when it has one.

        It decides the entry's id and its `created_at`, which is what lets the
        cursor resume exactly at an exchange boundary rather than inside one.
        """
        anchor = self.prompt if self.prompt is not None else self.answer
        if anchor is None:  # pragma: no cover - `group_exchanges` never builds one
            raise ValueError("an exchange holds at least one row")
        return anchor


def group_exchanges(rows: Sequence[LegacyMessageRow]) -> list[Exchange]:
    """An **ascending** run of rows, grouped into exchanges, ascending.

    A `user` row opens an exchange; the next `assistant` row completes and
    closes it. Two `user` rows in a row therefore close the first with no
    answer, and an `assistant` row with no open exchange becomes an entry with
    no prompt — rendered, never dropped, which is the loss `useChat` has today.
    """
    exchanges: list[Exchange] = []
    open_prompt: LegacyMessageRow | None = None
    for row in rows:
        if row.role == "user":
            if open_prompt is not None:
                exchanges.append(Exchange(prompt=open_prompt, answer=None))
            open_prompt = row
            continue
        if open_prompt is not None:
            exchanges.append(Exchange(prompt=open_prompt, answer=row))
            open_prompt = None
        else:
            exchanges.append(Exchange(prompt=None, answer=row))
    if open_prompt is not None:
        exchanges.append(Exchange(prompt=open_prompt, answer=None))
    return exchanges


@dataclass(frozen=True)
class LegacyWindow:
    """A window of rows turned into exchanges none of which can be half of one."""

    exchanges: list[Exchange]
    #: Whether a possibly-partial oldest group was dropped — which means there
    #: is more below, whatever the page took.
    partial_dropped: bool


def complete_exchanges(
    rows: Sequence[LegacyMessageRow], *, window_was_full: bool
) -> LegacyWindow:
    """A **newest-first** window of rows as complete exchanges, ascending.

    Reading newest-first, the only ambiguous group is the oldest: if its oldest
    row is an `assistant` row, its `user` row may lie below the window, and
    rendering it as an orphan answer would invent a turn that never happened.
    It is dropped — but only when the window came back **full**, because a short
    window read everything there was and a leading `assistant` row is then a
    genuine orphan that must be kept.

    Separated from `read_page` so the rule is observable on its own. It is
    otherwise invisible, and a first pass at this bead proved it: breaking the
    drop changed no page, because `read_page` over-reads by `2 * limit + 3`
    rows, a full window of which makes at least `limit + 2` groups, so the
    dropped group is never one the page would have taken. That margin is an
    accident of the over-read, and this function must not depend on it.
    """
    exchanges = group_exchanges(list(reversed(rows)))
    if window_was_full and exchanges and exchanges[0].prompt is None:
        return LegacyWindow(exchanges[1:], partial_dropped=True)
    return LegacyWindow(exchanges, partial_dropped=False)


def adapt(exchange: Exchange) -> AnyEntry:
    """One exchange as a wire entry — or an `opaque` one in its place.

    Read through `entry_or_opaque`, the same rule a stored row is read by, so an
    exchange whose columns cannot make a valid `ChatEntry` keeps its place in
    the thread and never takes the page down. The anchor's own columns are the
    authority on identity and on time.
    """
    anchor = exchange.anchor
    answer: dict[str, Any] | None = None
    if exchange.answer is not None:
        answer = {
            "text": exchange.answer.content,
            # Never `True`, never `[]`: a legacy row records neither, and the
            # contract says *not recorded* with `null` (bead design comment 2).
            "answerable": None,
            "sources": None,
            "created_at": exchange.answer.created_at,
        }
        if exchange.answer.suggestions:
            answer["suggestions"] = exchange.answer.suggestions
    payload = {
        "schema_version": CONTRACT_VERSION,
        "entry_kind": "chat",
        "entry_id": str(anchor.id),
        "created_at": anchor.created_at,
        "mode": anchor.mode,
        "prompt": None if exchange.prompt is None else exchange.prompt.content,
        "answer": answer,
    }
    return entry_or_opaque(payload, entry_id=str(anchor.id), created_at=anchor.created_at)


# ── Requirement 7: the ordering key, the merge and the cursor ────────────────

#: A stored entry outranks an adapted exchange at the same instant. The two can
#: never describe the same turn — an adapted exchange is suppressed once an
#: entry covers its rows (slice A) — so the rank only has to be *stable*.
ADAPTED_RANK = 0
STORED_RANK = 1


@dataclass(frozen=True)
class Candidate:
    """One entry, with everything the total order and the cursor need."""

    entry: Any  # justification: an `AnyEntry` in the route; tests order plain markers.
    created_at: datetime
    source_rank: int
    tiebreak: int


def order_key(candidate: Candidate) -> tuple[datetime, int, int]:
    """The total order, stated once. Descending, it is the page's order: no two
    candidates compare equal, so the order can never depend on the page size."""
    return (candidate.created_at, candidate.source_rank, candidate.tiebreak)


def merge(*sources: Iterable[Candidate], limit: int) -> list[Candidate]:
    """The newest `limit` candidates across every source, newest first."""
    everything = [c for source in sources for c in source]
    everything.sort(key=order_key, reverse=True)
    return everything[:limit]


@dataclass(frozen=True)
class Position:
    """Where one source was left off: the ordering key's time and tiebreak."""

    created_at: datetime
    tiebreak: int


@dataclass(frozen=True)
class TimelineCursor:
    """Both sources' positions, carried independently.

    A source from which nothing was taken keeps its previous position, and one
    whose oldest candidate was read but not taken must re-read it — neither is
    expressible with a single position.
    """

    entries: Position | None
    messages: Position | None


def _position_json(position: Position | None) -> list[Any] | None:
    return None if position is None else [position.created_at.isoformat(), position.tiebreak]


def encode_cursor(cursor: TimelineCursor) -> str:
    """base64url, **unpadded**: `Cursor`'s `^[A-Za-z0-9_-]{1,512}$` refuses `=`."""
    raw = json.dumps(
        {"v": CURSOR_VERSION, "e": _position_json(cursor.entries), "m": _position_json(cursor.messages)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _position_from(value: object) -> Position | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2:
        raise CursorUnreadable("a cursor position is a time and a tiebreak")
    moment, tiebreak = value
    if not isinstance(moment, str) or isinstance(tiebreak, bool) or not isinstance(tiebreak, int):
        raise CursorUnreadable("a cursor position is a time and a tiebreak")
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError as exc:
        raise CursorUnreadable("a cursor position carries an ISO-8601 instant") from exc
    if parsed.tzinfo is None:
        raise CursorUnreadable("a cursor position carries an aware instant")
    return Position(created_at=parsed, tiebreak=tiebreak)


def decode_cursor(text: str) -> TimelineCursor:
    """The inverse of `encode_cursor`, refusing anything it did not mint.

    Nothing here is guessed at: a cursor of another version, or a damaged one,
    is a refusal rather than a guess, so a client can never be served a page
    that silently skips or repeats entries.

    What this does **not** claim is authenticity. Cursors are unsigned, so a
    structurally valid one a caller minted themselves is honoured — it names a
    position, and every window is scoped to `conversation_id` on top of an
    ownership check, so the most such a caller can do is re-read or skip part of
    a conversation they already own. Signing was never asked for, and a cursor
    carries ids and times only (X-7).
    """
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        decoded = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise CursorUnreadable("that cursor is not one this server minted") from exc
    if not isinstance(decoded, dict) or decoded.get("v") != CURSOR_VERSION:
        raise CursorUnreadable("that cursor is not one this server minted")
    return TimelineCursor(entries=_position_from(decoded.get("e")), messages=_position_from(decoded.get("m")))


# ── Requirement 9: the two query parameters ──────────────────────────────────

_LIMIT = TypeAdapter(int)
_CURSOR = TypeAdapter(Cursor)


def parse_page_query(limit: str | None, cursor: str | None) -> tuple[int, TimelineCursor | None]:
    """The route's `limit` and `cursor`, validated by this route and no one else.

    Both parameters are declared `str | None` on the handler so that FastAPI
    never validates them: its default 422 body repeats the request's own input
    (SEC-23, R-12), and this route answers with `validation_error_body` instead.
    A `limit` outside the bound is a **refusal**, never a silent clamp.
    """
    size = DEFAULT_PAGE_LIMIT
    if limit is not None:
        try:
            size = _LIMIT.validate_python(limit)
        except ValidationError:
            # `from None`, deliberately. `_LIMIT` and `_CURSOR` are bare
            # `TypeAdapter`s and do not carry `_Contract`'s
            # `hide_input_in_errors=True`, so their `ValidationError` prints
            # `input_value=...`; a forged cursor position reaches
            # `datetime.fromisoformat`, whose `ValueError` quotes the string.
            # Chaining either would leave the caller's raw parameter one
            # `__cause__` hop from any `exc_info=True` log line (SEC-20,
            # SEC-23, R-12). Clearing the cause also suppresses `__context__`,
            # so the formatted traceback holds the refusal and nothing else.
            raise ParameterRefused("limit") from None
        if not 1 <= size <= TIMELINE_PAGE_MAX_ITEMS:
            raise ParameterRefused("limit")
    if cursor is None:
        return size, None
    try:
        return size, decode_cursor(_CURSOR.validate_python(cursor))
    except (ValidationError, CursorUnreadable):
        raise ParameterRefused("cursor") from None


# ── Requirement 9: authorization, and the page ───────────────────────────────


#: The path id, against the shape the *contract* carries it in. `OpaqueId` is
#: `^[A-Za-z0-9_-]{1,64}$`; `ChatRequest.conversation_id` is a bare `str`, so
#: `/chat` can and did mint ids outside it.
_CONVERSATION_ID = TypeAdapter(OpaqueId)


def require_readable_id(conversation_id: str) -> None:
    """The id this route can answer with, or the one refusal it already has.

    An id outside `OpaqueId` is one `TimelinePage.conversation_id` cannot
    carry, so building the page raises a `ValidationError` — inside the
    transaction, where it is neither `ConversationNotFound` nor a database
    error, and a caller's own conversation answers **500**. Refused up front
    instead, and refused as `ConversationNotFound`: malformed, missing and
    foreign must stay one response from one code path (SEC-3), and a second
    refusal shape here would be a second oracle.

    Acceptable for real users: the shipped UI mints UUIDs, which fit the shape.
    A legacy conversation whose id does not is not readable through this route
    and stays readable through the unchanged `GET …/messages`.
    """
    try:
        _CONVERSATION_ID.validate_python(conversation_id)
    except ValidationError:
        # `from None`, for `parse_page_query`'s reason: a bare `TypeAdapter`'s
        # `ValidationError` prints `input_value=...`, and chaining it would put
        # the raw path parameter one `__cause__` hop from any traceback
        # (SEC-20, SEC-23, R-12).
        raise ConversationNotFound("the conversation id is not readable here") from None


def authorize(
    # justification: the unit is passed straight through to the store, which
    # types it as `UnitOfWork`. Naming that type here would make this module
    # import `service.db`, and requirement 10 keeps the read model free of the
    # database layer — it holds no SQL and touches no connection.
    store: TimelineStore, unit: Any, conversation_id: str, *, user_id: int,
) -> None:
    """The caller owns this conversation, or one refusal for every other case.

    **Never a claim.** A conversation with no ownership row — 0001 declares
    `messages_conversation_fkey` `NOT VALID`, so rows written before the table
    existed may have none — answers 404 here until some other route claims it.
    `GET …/messages` still claims such a conversation on first read; this route
    may not (§8.1), and that difference is deliberate.
    """
    owner = store.owner_of(unit, conversation_id)
    if owner is None or owner != user_id:
        raise ConversationNotFound(conversation_id)


def read_page(
    # justification: as in `authorize` above — the unit is opaque here and is
    # only ever handed back to the store, which types it as `UnitOfWork`.
    store: TimelineStore, unit: Any, conversation_id: str, *,
    limit: int, cursor: TimelineCursor | None,
) -> TimelinePage:
    """One page of this conversation, newest first.

    Each source contributes a bounded window under the cursor predicate, so a
    page is a bounded index read and never a scan. **The legacy window is a
    window of exchanges, not of rows**: an exchange is at most two rows, and
    reading newest-first the only ambiguous group is the oldest — if its oldest
    row is an `assistant` row, its `user` row may lie below the window. So the
    read over-reads by `2 * limit + 3` rows and discards that group when the
    window came back full; everything above it is unambiguous.
    """
    before = None if cursor is None or cursor.messages is None else (
        cursor.messages.created_at, cursor.messages.tiebreak
    )
    limit_rows = 2 * limit + 3
    rows = store.legacy_window(unit, conversation_id, before=before, limit_rows=limit_rows)
    window = complete_exchanges(rows, window_was_full=len(rows) == limit_rows)
    adapted = [
        Candidate(
            entry=adapt(exchange), created_at=exchange.anchor.created_at,
            source_rank=ADAPTED_RANK, tiebreak=exchange.anchor.id,
        )
        for exchange in window.exchanges
    ]
    taken = merge(adapted, limit=limit)
    # Null ONLY when nothing was dropped as a partial exchange and every
    # candidate read was taken.
    #
    # The first clause cannot fire on its own today, and saying so is worth more
    # than a line that looks load-bearing and is not: a dropped group means the
    # window was full, a full window of `2 * limit + 3` rows makes at least
    # `limit + 2` groups, and `len(adapted) <= limit` is then already false. It
    # is kept because it states the rule the cursor actually depends on, and it
    # is what stays correct if the over-read above is ever narrowed.
    exhausted = not window.partial_dropped and len(adapted) <= limit
    next_cursor = None if exhausted else encode_cursor(
        TimelineCursor(
            entries=None if cursor is None else cursor.entries,
            messages=Position(created_at=taken[-1].created_at, tiebreak=taken[-1].tiebreak),
        )
    )
    return TimelinePage(
        schema_version=PAGE_SCHEMA_VERSION,
        conversation_id=conversation_id,
        items=[c.entry for c in taken],
        next_cursor=next_cursor,
    )

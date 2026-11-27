"""Read a bounded, owner-authorized window of a GM thread for `/recap`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal

from .db import UnitOfWork
from .document_generation import (
    GENERATION_CONTEXT_MAX_CHARS,
    THREAD_MAX_TURNS,
    GenerationRequest,
    ThreadTurn,
    context_size,
)
from .timeline import TimelineCursor, authorize, decode_cursor, read_page
from .timeline_store import TimelineStore
from .workbench_contracts import (
    TIMELINE_PAGE_MAX_ITEMS,
    AnyEntry,
    ChatEntry,
    DocumentResult,
    InvocationStatus,
    SessionBoundary,
    SessionDividerEntry,
    ToolEntry,
)
from .workbench_registry import REGISTRY

RECAP_TURN_MAX_CHARS: Final = 4_000
RECAP_READ_MAX_PAGES: Final = 4


class RecapBoundary(str, Enum):
    SESSION_SO_FAR = "session_so_far"
    LAST_SESSION = "last_session"
    WHOLE_THREAD = "whole_thread"
    RECENT_TURNS = "recent_turns"


@dataclass(frozen=True)
class RecapWindow:
    turns: tuple[ThreadTurn, ...] = field(repr=False)
    boundary: RecapBoundary


def _turn(speaker: Literal["gm", "assistant"], text: str | None) -> tuple[ThreadTurn, ...]:
    if text is None or not (text := text.strip()):
        return ()
    if len(text) > RECAP_TURN_MAX_CHARS:
        text = text[:RECAP_TURN_MAX_CHARS] + "…"
    return (ThreadTurn(speaker, text),)


def entry_turns(entry: AnyEntry) -> tuple[ThreadTurn, ...]:
    """Convert only chat and completed tools, oldest turn first within an entry."""
    if isinstance(entry, ChatEntry):
        return _turn("gm", entry.prompt) + _turn("assistant", None if entry.answer is None else entry.answer.text)
    if not isinstance(entry, ToolEntry) or entry.invocation.status is not InvocationStatus.DONE:
        return ()
    result = entry.invocation.result
    tool = REGISTRY.tool(entry.invocation.tool_id.value)
    if result is None or tool is None:
        return ()
    prompt = f"{tool.label}: {entry.brief}" if entry.brief.strip() else tool.label
    prose = result.prose.strip()
    if isinstance(result, DocumentResult):
        saved = f"Saved to the campaign: {result.document.title}"
        prose = f"{prose}\n{saved}" if prose else saved
    return _turn("gm", prompt) + _turn("assistant", prose)


def read_recap_window(
    store: TimelineStore,
    unit: UnitOfWork,
    conversation_id: str,
    *,
    owner_id: int,
    max_turns: int = THREAD_MAX_TURNS,
    max_pages: int = RECAP_READ_MAX_PAGES,
) -> RecapWindow:
    """Walk newest first and return the retained turns in playback order.

    Before the first divider, keep scanning for an End even at the turn cap.
    That divider can still invalidate everything collected as post-session prep.
    The page cap bounds both the lookup and the admission precheck.
    """
    authorize(store, unit, conversation_id, user_id=owner_id)
    turns: list[ThreadTurn] = []
    cursor: TimelineCursor | None = None
    ended = False
    overflow = False

    def finish(boundary: RecapBoundary) -> RecapWindow:
        return RecapWindow(tuple(reversed(turns)), RecapBoundary.RECENT_TURNS if overflow else boundary)

    for _ in range(max_pages):
        page = read_page(store, unit, conversation_id, limit=TIMELINE_PAGE_MAX_ITEMS, cursor=cursor)
        for entry in page.items:
            if isinstance(entry, SessionDividerEntry):
                if ended:
                    return finish(RecapBoundary.LAST_SESSION)
                if entry.boundary is SessionBoundary.START:
                    return finish(RecapBoundary.SESSION_SO_FAR)
                ended = True
                overflow = False
                turns.clear()
            elif not overflow:
                incoming = entry_turns(entry)
                if len(turns) + len(incoming) > max_turns:
                    overflow = True
                    if ended:
                        return finish(RecapBoundary.LAST_SESSION)
                else:
                    turns.extend(reversed(incoming))
        if page.next_cursor is None:
            return finish(RecapBoundary.LAST_SESSION if ended else RecapBoundary.WHOLE_THREAD)
        cursor = decode_cursor(page.next_cursor)
    return finish(RecapBoundary.RECENT_TURNS)


def has_recap_turns(store: TimelineStore, unit: UnitOfWork, conversation_id: str, *, owner_id: int) -> bool:
    """Use the same boundary for admission and for the later context read."""
    return bool(read_recap_window(store, unit, conversation_id, owner_id=owner_id).turns)


def fit_request(
    window: RecapWindow, build: Callable[[tuple[ThreadTurn, ...]], GenerationRequest],
) -> tuple[RecapWindow, GenerationRequest]:
    request = build(window.turns)
    while window.turns and context_size(request) > GENERATION_CONTEXT_MAX_CHARS:
        window = RecapWindow(window.turns[1:], RecapBoundary.RECENT_TURNS)
        request = build(window.turns)
    return window, request

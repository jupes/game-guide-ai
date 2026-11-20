"""The recap boundary through the actual timeline read model and its twin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from service.campaign_store import InMemoryCampaignStore
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase, UnitOfWork
from service.document_generation import GENERATION_CONTEXT_MAX_CHARS, GenerationRequest, ThreadTurn, context_size
from service.history import InMemoryMessageStore
from service.recap_window import (
    RECAP_TURN_MAX_CHARS,
    RecapBoundary,
    RecapWindow,
    entry_turns,
    fit_request,
    has_recap_turns,
    read_recap_window,
)
from service.session_divider_store import InMemorySessionDividerStore
from service.session_dividers import divider_entry
from service.timeline import ConversationNotFound, read_page
from service.timeline_store import InMemoryTimelineStore, TimelineStore, new_entry_id
from service.workbench_contracts import ChatAnswer, ChatEntry, ChatMode, DocumentTypeId, SessionBoundary, ToolEntry

T0 = datetime(2026, 9, 29, 12, tzinfo=UTC)


def append_chat(
    store: TimelineStore,
    unit: UnitOfWork,
    conversation_id: str,
    owner: int,
    at: datetime,
    prompt: str,
    answer: str | None = None,
) -> None:
    entry = ChatEntry(
        schema_version=1, entry_id=new_entry_id(), created_at=at, entry_kind="chat", mode=ChatMode.gm,
        prompt=prompt, answer=None if answer is None else ChatAnswer(
            text=answer, answerable=True, sources=[], created_at=at,
        ),
    )
    store.append(unit, conversation_id, entry, at, owner_id=owner)


class Thread:
    def __init__(self) -> None:
        self.db = InMemoryDatabase()
        self.messages = InMemoryMessageStore()
        self.store = InMemoryTimelineStore(self.db, messages=self.messages)
        self.dividers = InMemorySessionDividerStore(self.db)
        with self.db.transaction() as unit:
            self.campaign = InMemoryCampaignStore(self.db).create(unit, owner_id=1, name="Lantern Road").id
            self.id = InMemoryConversationStore(self.db).create(
                unit, owner_id=1, campaign_id=self.campaign, title=None, started_mode=None,
            ).id
        self.messages.claim_conversation(self.id, 1)

    def seed(self, rows: list[str], unit: UnitOfWork) -> None:
        for index, row in enumerate(rows):
            at = T0 + timedelta(seconds=index)
            if row.startswith(("START:", "END:")):
                kind, session = row.split(":", 1)
                entry = divider_entry(
                    entry_id=new_entry_id(), session_id=session, boundary=SessionBoundary(kind.lower()), at=at,
                )
                assert self.dividers.append_divider(unit, self.id, entry, owner_id=1, campaign_id=self.campaign)
            else:
                append_chat(self.store, unit, self.id, 1, at, row)


@pytest.mark.parametrize(("rows", "boundary", "expected"), [
    (["one", "two"], RecapBoundary.WHOLE_THREAD, ["one", "two"]),
    (["prep", "START:ses_1", "one", "two"], RecapBoundary.SESSION_SO_FAR, ["one", "two"]),
    (["prep", "START:ses_1", "play", "END:ses_1", "later"], RecapBoundary.LAST_SESSION, ["play"]),
    (["START:ses_1", "first", "END:ses_1", "START:ses_2", "second"], RecapBoundary.SESSION_SO_FAR, ["second"]),
    (["play", "END:ses_1", "later"], RecapBoundary.LAST_SESSION, ["play"]),
    (["older", "END:ses_1", "play", "END:ses_2", "later"], RecapBoundary.LAST_SESSION, ["play"]),
    (["START:ses_1", "END:ses_1", "later"], RecapBoundary.LAST_SESSION, []),
    (["START:ses_1"], RecapBoundary.SESSION_SO_FAR, []),
])
def test_window_and_precheck_agree(rows: list[str], boundary: RecapBoundary, expected: list[str]) -> None:
    thread = Thread()
    with thread.db.transaction() as unit:
        thread.seed(rows, unit)
        window = read_recap_window(thread.store, unit, thread.id, owner_id=1)
        assert window.boundary is boundary
        assert [turn.text for turn in window.turns] == expected
        assert has_recap_turns(thread.store, unit, thread.id, owner_id=1) is bool(expected)


def test_legacy_exchanges_are_read_and_another_owner_never_reads_pages() -> None:
    thread = Thread()
    thread.messages.append(thread.id, "gm", "user", "LEGACY_QUESTION")
    thread.messages.append(thread.id, "gm", "assistant", "LEGACY_ANSWER")
    with thread.db.transaction() as unit:
        window = read_recap_window(thread.store, unit, thread.id, owner_id=1)
        assert window.turns == (ThreadTurn("gm", "LEGACY_QUESTION"), ThreadTurn("assistant", "LEGACY_ANSWER"))
        with patch("service.recap_window.read_page", wraps=read_page) as pages:
            with pytest.raises(ConversationNotFound):
                read_recap_window(thread.store, unit, thread.id, owner_id=2)
            with pytest.raises(ConversationNotFound):
                has_recap_turns(thread.store, unit, "cnv_missing", owner_id=1)
            pages.assert_not_called()


def test_turn_cap_keeps_whole_entries_and_still_finds_an_end_behind_prep() -> None:
    thread = Thread()
    with thread.db.transaction() as unit:
        append_chat(thread.store, unit, thread.id, 1, T0, "older question", "older answer")
        append_chat(thread.store, unit, thread.id, 1, T0 + timedelta(seconds=1), "newest")
        exact = read_recap_window(thread.store, unit, thread.id, owner_id=1, max_turns=3)
        capped = read_recap_window(thread.store, unit, thread.id, owner_id=1, max_turns=2)
        assert (exact.boundary, len(exact.turns)) == (RecapBoundary.WHOLE_THREAD, 3)
        assert capped == RecapWindow((ThreadTurn("gm", "newest"),), RecapBoundary.RECENT_TURNS)

    thread = Thread()
    with thread.db.transaction() as unit:
        thread.seed(["START:ses_recent", "inside", "END:ses_recent", "prep1", "prep2", "prep3"], unit)
        finished = read_recap_window(thread.store, unit, thread.id, owner_id=1, max_turns=2)
        assert finished == RecapWindow((ThreadTurn("gm", "inside"),), RecapBoundary.LAST_SESSION)


def test_default_turn_and_page_limits_are_bounded() -> None:
    thread = Thread()
    with thread.db.transaction() as unit:
        thread.seed([f"question {i}" for i in range(201)], unit)
        window = read_recap_window(thread.store, unit, thread.id, owner_id=1)
        assert window.boundary is RecapBoundary.RECENT_TURNS
        assert len(window.turns) == 200
        assert window.turns[0].text == "question 1"
    thread = Thread()
    with thread.db.transaction() as unit:
        thread.seed([f"question {i}" for i in range(401)], unit)
        with patch("service.recap_window.read_page", wraps=read_page) as pages:
            window = read_recap_window(thread.store, unit, thread.id, owner_id=1)
        assert pages.call_count == 4
        assert window.boundary is RecapBoundary.RECENT_TURNS
        assert len(window.turns) == 200


def test_one_turn_is_clipped_and_request_fit_drops_oldest_first() -> None:
    entry = ChatEntry(
        schema_version=1, entry_id=new_entry_id(), created_at=T0, entry_kind="chat", mode=ChatMode.gm,
        prompt="  ", answer=ChatAnswer(text="a" * 5000, answerable=None, sources=None, created_at=T0),
    )
    assert entry_turns(entry) == (ThreadTurn("assistant", "a" * RECAP_TURN_MAX_CHARS + "…"),)

    original = tuple(ThreadTurn("gm", str(i) + "\n" * 3998 + ".") for i in range(9))

    def build(turns: tuple[ThreadTurn, ...]) -> GenerationRequest:
        return GenerationRequest(doc_type=DocumentTypeId.SESSION_NOTES, brief="b" * 2000, thread=turns)

    fitted, request = fit_request(RecapWindow(original, RecapBoundary.WHOLE_THREAD), build)
    assert 0 < len(request.thread) < len(original)
    assert request.thread == fitted.turns == original[-len(fitted.turns):]
    assert fitted.boundary is RecapBoundary.RECENT_TURNS
    assert context_size(request) <= GENERATION_CONTEXT_MAX_CHARS
    assert context_size(build(original[-len(fitted.turns) - 1:])) > GENERATION_CONTEXT_MAX_CHARS


def test_only_completed_tool_turns_supply_their_brief_and_result() -> None:
    result = {
        "result_kind": "document", "tool_id": "npc", "prose": "The ferryman remembers the group.", "suggestions": [],
        "document": {"document_id": "doc_recap", "type": "npc", "title": "The ferryman", "library_category": "npcs"},
    }
    error = {"code": "backend_unavailable", "message": "Try again.", "retryable": True}
    for status, value, failure in [("done", result, None), ("working", None, None), ("cancelled", None, None),
                                   ("failed", None, error)]:
        entry = ToolEntry.model_validate({
            "schema_version": 1, "entry_id": new_entry_id(), "created_at": T0, "entry_kind": "tool", "brief": "A guide",
            "invocation": {
                "schema_version": 1, "invocation_id": "inv_recap_tool_0001", "tool_id": "npc", "status": status,
                "attempt": 2, "cancel_requested": False, "created_at": T0, "updated_at": T0,
                "result": value, "error": failure,
            },
        })
        assert entry_turns(entry) == ((
            ThreadTurn("gm", "NPC: A guide"),
            ThreadTurn("assistant", "The ferryman remembers the group.\nSaved to the campaign: The ferryman"),
        ) if status == "done" else ())

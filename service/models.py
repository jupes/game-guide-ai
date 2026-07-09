"""Pydantic request/response models for the D&D agent service."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ChatMode(str, Enum):
    sage  = "sage"
    spell = "spell"
    rules = "rules"
    gm    = "gm"


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"


class SuggestionStyle(str, Enum):
    practical = "practical"
    roleplay = "roleplay"
    wacky = "wacky"


class Suggestion(BaseModel):
    """One LLM-invented spell-usage idea (spell mode only)."""
    style: SuggestionStyle
    text: str


class StoredMessage(BaseModel):
    """One persisted chat turn, as returned by GET /conversations/{id}/messages."""
    id: int
    role: MessageRole
    content: str
    mode: ChatMode
    created_at: datetime
    # Assistant turns from spell mode carry their suggestions (CP-C).
    suggestions: list[Suggestion] | None = None


class MessagesResponse(BaseModel):
    conversation_id: str
    messages: list[StoredMessage]


# Response-contract constant (not a Pydantic model, but part of the contract):
# the canonical refusal text that ChatResponse.answer carries on the refuse
# path. Lives here beside ChatResponse so both the graph and the service layer
# can import it without a service.rag <-> service.graph cycle; service.rag
# re-exports it for existing importers (ingestion/eval_answers.py, tests).
REFUSAL = "I couldn't find that in the D&D 5e sources I have."


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Natural-language D&D question")
    mode: ChatMode = Field(ChatMode.sage, description="Chat mode (sage|spell|rules|gm)")
    conversation_id: str | None = Field(None, description="Carried through; persistence is stubbed")


class Source(BaseModel):
    book: str
    chapter: str | None = None
    section: str | None = None
    entity: str | None = None
    page: int | None = None
    snippet: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]
    answerable: bool
    mode: ChatMode = ChatMode.sage
    conversation_id: str | None = None
    # Spell mode only: exactly three usage ideas (practical/roleplay/wacky);
    # None everywhere else — and in spell mode when suggestion generation
    # failed (the answer must never fail because the garnish did).
    suggestions: list[Suggestion] | None = None

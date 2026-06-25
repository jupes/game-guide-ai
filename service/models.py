"""Pydantic request/response models for the D&D agent service."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ChatMode(str, Enum):
    sage  = "sage"
    spell = "spell"
    rules = "rules"
    gm    = "gm"


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

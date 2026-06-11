"""Pydantic request/response models for the D&D agent service."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Natural-language D&D question")


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

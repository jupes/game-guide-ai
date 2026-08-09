"""Pydantic request/response models for the D&D agent service."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class SpellComponents(BaseModel):
    """V/S/M component flags; `m` carries the material-component text."""
    model_config = ConfigDict(extra="forbid")
    v: bool | None = None
    s: bool | None = None
    m: str | None = None


class SpellContent(BaseModel):
    """A spell entry extracted from the spell-mode prose answer (z7fl.1).
    `name` and `description` are core-required: a reply missing either is not
    a usable card and must degrade to prose rather than replace it."""
    name: str
    description: str
    level: int | None = None
    school: str | None = None
    casting_time: str | None = None
    range: str | None = None
    duration: str | None = None
    components: SpellComponents | None = None
    higher_levels: str | None = None
    classes: list[str] | None = None
    concentration: bool | None = None
    ritual: bool | None = None


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
    # Spell mode only: structured spell content extracted from `answer`, best-
    # effort (degrades to None on any parse/LLM failure, same posture as
    # suggestions — see service/generate.py:parse_spell_content).
    spell_content: SpellContent | None = None


# ── File attachments (swe1.6) ────────────────────────────────────────────────


class Attachment(BaseModel):
    """UI-facing metadata for one uploaded attachment (extracted text stays server-side)."""
    id: int
    filename: str
    content_type: str
    chars: int  # length of the extracted text
    created_at: datetime


class AttachmentUploadRequest(BaseModel):
    """base64 upload body (no multipart dep). `data` is the base64 file content."""
    filename: str = Field(..., min_length=1)
    content_type: str = Field("", description="Client-reported MIME type")
    data: str = Field(..., min_length=1, description="base64-encoded file content")


class AttachmentResponse(BaseModel):
    conversation_id: str
    attachment: Attachment


class AttachmentsResponse(BaseModel):
    conversation_id: str
    attachments: list[Attachment]


# ── Auth (x5bz.2) ────────────────────────────────────────────────────────────


def _validate_email(v: str) -> str:
    """Light structural check — the invite link is the real trust anchor, so we
    don't verify deliverability, just reject the obviously-not-an-email. Avoids a
    hard `email-validator` dependency for the pilot."""
    v = v.strip()
    if "@" not in v or v.startswith("@") or v.endswith("@") or " " in v:
        raise ValueError("must be a valid email address")
    return v


# Upper bounds on unauthenticated input. argon2 hashes whatever password it is
# given, so an unbounded field is free work for an attacker; these are far above
# any real credential and are rejected by validation before any hashing happens.
MAX_EMAIL_LENGTH = 254        # RFC 5321 practical maximum
MAX_PASSWORD_LENGTH = 1024
MAX_INVITE_LENGTH = 256       # token_urlsafe(32) is 43 chars


class SignupRequest(BaseModel):
    """Create an account by redeeming a one-time invite (POST /auth/signup)."""
    email: str = Field(..., min_length=3, max_length=MAX_EMAIL_LENGTH,
                       description="Account email (login id)")
    password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_LENGTH,
                          description="Account password (min 8 chars)")
    invite: str = Field(..., min_length=1, max_length=MAX_INVITE_LENGTH,
                        description="One-time invite token from the link")

    _email = field_validator("email")(_validate_email)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=MAX_EMAIL_LENGTH)
    password: str = Field(..., min_length=1, max_length=MAX_PASSWORD_LENGTH)

    _email = field_validator("email")(_validate_email)


class AuthUser(BaseModel):
    """The authenticated identity returned by signup / login / GET /auth/me."""
    email: str
    role: str

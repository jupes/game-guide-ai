"""Pydantic request/response models for the D&D agent service."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

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


class Abilities(BaseModel):
    """The six 5e ability scores. Raw scores only — modifiers are derived by
    the widget, never passed in (matches the aetheril-design-system
    StatBlockCard contract). `extra="forbid"` so a misspelled ability name
    (e.g. "strength") surfaces as a validation failure instead of silently
    validating as an empty ability set."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    str: int | None = None
    dex: int | None = None
    con: int | None = None
    int_: int | None = Field(None, alias="int")
    wis: int | None = None
    cha: int | None = None


class StatBlockEntry(BaseModel):
    """One named block (a trait, action, bonus action, reaction, or legendary
    action) — the entry name is rendered italic, the text is its description."""
    name: str
    text: str


class StatBlockContent(BaseModel):
    """A 5e NPC/monster stat block extracted from a GM/Sage prose answer
    (z7fl.1 Checkpoint B). `name`, `ac`, `hp` are core-required: without
    armor class or hit points this isn't recognizably a stat block, and must
    degrade to prose rather than replace it."""
    name: str
    ac: int
    hp: int
    size: str | None = None
    type: str | None = None
    alignment: str | None = None
    ac_note: str | None = None
    hit_dice: str | None = None
    speed: str | None = None
    abilities: Abilities | None = None
    saving_throws: str | None = None
    skills: str | None = None
    damage_immunities: str | None = None
    condition_immunities: str | None = None
    senses: str | None = None
    languages: str | None = None
    cr: str | int | None = None
    xp: int | None = None
    traits: list[StatBlockEntry] | None = None
    actions: list[StatBlockEntry] | None = None
    bonus_actions: list[StatBlockEntry] | None = None
    reactions: list[StatBlockEntry] | None = None
    legendary_actions: list[StatBlockEntry] | None = None


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
    # b8o.2: "auto" or a specific enabled catalog alias (e.g. "gpt-4o-mini").
    # Defaults to "auto" so existing callers that omit it keep working.
    # Validated against the catalog and atomically bound to the conversation
    # in the /chat handler, not here — Pydantic has no catalog access.
    model_preference: str = Field("auto", description="'auto' or a specific enabled model alias")


class Source(BaseModel):
    book: str
    chapter: str | None = None
    section: str | None = None
    entity: str | None = None
    page: int | None = None
    snippet: str


class RoutingInfo(BaseModel):
    """Honest model/fallback disclosure for one provider call (b8o.2 D3).
    All fields are bounded enums/aliases — never an endpoint, key state, or
    internal error. `task_class`/`reason` stay None until Checkpoint 4's
    classifier exists; `auto` resolves to the static baseline until then."""
    requested: str
    effective: str
    provider: str
    strategy: Literal["auto", "manual"]
    task_class: str | None = None
    reason: str | None = None
    fallback_from: str | None = None


class SuggestionsRoutingInfo(BaseModel):
    """Same disclosure shape as RoutingInfo, for spell mode's second
    (suggestions) call (D3) — present only in spell mode, deliberately
    narrower (no `requested`/`strategy`: suggestions always route to the
    economy subroute regardless of the answer's routing)."""
    effective: str
    provider: str
    reason: str | None = None
    fallback_from: str | None = None


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
    # b8o.2: which model answered. None only when routing can't be resolved
    # (never expected on a successful response — present for forward safety).
    routing: RoutingInfo | None = None
    # Spell mode only; null when suggestion generation failed (D3).
    suggestions_routing: SuggestionsRoutingInfo | None = None
    # Spell mode only: structured spell content extracted from `answer`, best-
    # effort (degrades to None on any parse/LLM failure, same posture as
    # suggestions — see service/generate.py:parse_spell_content).
    spell_content: SpellContent | None = None
    # GM/Sage only, and only when the answer looks like it presents a
    # creature (cost-gated heuristic — see service/generate.py:
    # _looks_like_statblock): structured NPC/monster stat block extracted
    # from `answer`, same best-effort/degrade posture as spell_content.
    stat_block: StatBlockContent | None = None


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

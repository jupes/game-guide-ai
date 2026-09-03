"""
Context assembly + grounded answer generation (gpt-4o-mini).

`build_context` and `build_sources` are pure (no network) and operate on a
`RetrievalResult` using **full** chunk text (the 120-char preview is too short to
ground an answer — see the plan review). `generate_answer` calls the LLM and
accepts an injected client for tests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import openai
from langchain_core.messages import HumanMessage, SystemMessage

# Env-overridable tuning knobs live in the single top-level config module.
# DEFAULT_MODEL is re-exported here for `from .generate import DEFAULT_MODEL`.
from config import ATTACHMENT_MAX_CHARS, CONTEXT_TOP_N, DEFAULT_MODEL, SNIPPET_MAX, TEMPERATURE
from ingestion.retrieval import RetrievalResult

from .attachments import cap_text
from .models import Source, Suggestion, SuggestionStyle


# Minimal structural type for the injected chat model (ziw.2 / CP2). We call a
# LangChain-style `client.invoke(messages)` and read `.content` off the result, so
# a `langchain_openai.ChatOpenAI` (or a test fake) satisfies it without depending on
# the concrete class. Response typed loosely (`Any`): we only read `.content`.
class LLMClient(Protocol):
    def invoke(
        self, input: Any, config: Any = None, **kwargs: Any
    ) -> Any: ...  # pragma: no cover - structural type


# ---------------------------------------------------------------------------
# GenerationResult / usage / attempt observer (agent-forge-harness-b8o.1,
# Checkpoint 1 slice 4 — TDD row 11). The provider boundary must return more
# than answer text; unknown usage fields stay None rather than becoming zero
# (a provider that doesn't report reasoning tokens is not the same as a
# provider that used zero). generate_answer/generate_suggestions still return
# their existing plain types unchanged — this is an additive capability the
# later routing/observability checkpoints build on, not a behavior change.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GenerationUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    text: str
    usage: GenerationUsage
    provider_request_id: str | None
    finish_reason: str | None


class AttemptObserver(Protocol):
    """Receives one record per actual provider call attempt, including
    failures. Checkpoint 5 wires a real (Langfuse-backed) observer; this
    checkpoint ships the seam + a no-op default so nothing breaks before then."""
    def record(
        self, *, alias: str, result: GenerationResult | None, error: BaseException | None,
    ) -> None: ...  # pragma: no cover - structural type


class NullAttemptObserver:
    """Default observer: does nothing. Safe when no one is watching."""
    def record(
        self, *, alias: str, result: GenerationResult | None, error: BaseException | None,
    ) -> None:
        pass


def _usage_from_message(message: Any) -> GenerationUsage:
    meta = getattr(message, "usage_metadata", None) or {}
    input_details = meta.get("input_token_details") or {}
    output_details = meta.get("output_token_details") or {}
    return GenerationUsage(
        input_tokens=meta.get("input_tokens"),
        cached_input_tokens=input_details.get("cache_read"),
        output_tokens=meta.get("output_tokens"),
        reasoning_tokens=output_details.get("reasoning"),
    )


# Bounded service-owned retry (agent-forge-harness-b8o.1, Checkpoint 1 step 5).
# Provider clients are constructed with max_retries=0 (ProviderClientFactory) —
# ChatOpenAI retries by default, so disabling that AND landing this retry ship
# together, or the baseline temporarily loses resilience. Narrow on purpose:
# only the transient network/rate-limit/5xx exceptions the SDK itself used to
# retry on. The full normalized error-category table (authentication/quota/
# rate_limit/timeout/content_filter/invalid_request/upstream_unavailable/
# unknown, D4) is Checkpoint 2's job — this is not that taxonomy.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,  # covers APITimeoutError (its subclass)
    openai.RateLimitError,
    openai.InternalServerError,
)
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 0.5


def generate_result(
    messages: list[Any], *, alias: str, client: LLMClient,
    config: Any | None = None, observer: AttemptObserver | None = None,
    max_attempts: int = _MAX_ATTEMPTS, sleep: Any = time.sleep,
) -> GenerationResult:
    """Invoke `client`, retrying transient failures up to `max_attempts` times,
    and return everything the provider boundary reports, not just the answer
    text. Records one attempt with `observer` per actual call — success or
    failure — including every retried attempt, so every attempt, latency, and
    charge stays attributable. The final failure re-raises unchanged after
    being recorded. `alias` identifies which catalog entry made the call;
    Checkpoint 1 has no per-request routing yet, so callers pass the
    service's current model alias."""
    obs = observer or NullAttemptObserver()
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.invoke(messages, config=config)
        except BaseException as exc:
            obs.record(alias=alias, result=None, error=exc)
            if isinstance(exc, _RETRYABLE_EXCEPTIONS) and attempt < max_attempts:
                sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise
        content = resp.content
        text = content.strip() if isinstance(content, str) else str(content).strip()
        result = GenerationResult(
            text=text,
            usage=_usage_from_message(resp),
            provider_request_id=getattr(resp, "id", None),
            finish_reason=(getattr(resp, "response_metadata", None) or {}).get("finish_reason"),
        )
        obs.record(alias=alias, result=result, error=None)
        return result
    raise AssertionError("unreachable: the loop above always returns or raises")  # pragma: no cover

# Shared grounding instruction appended to every grounded-mode system prompt.
# The numbered sources include any uploaded attachment, which generate_node
# injects as its own numbered source (swe1.6 / 08il) — call it out so the model
# treats the file as answerable material instead of giving its canned
# "I can't see attachments" refusal.
_GROUNDING_SUFFIX = (
    "Answer the user's question using ONLY the numbered sources below. The "
    "numbered sources include any file the user uploaded, which appears as its "
    "own numbered source labeled 'Attachment' — treat it as readable material "
    "you can see and quote. "
    "Cite the sources you use inline as [1], [2], etc. "
    "If the sources do not contain the answer, say you don't have that in your "
    "sources — do not use outside knowledge."
)

# Formatting directive for statted creatures (synn). Appended to the personas
# that produce or reproduce creatures so a generated character/monster/NPC comes
# back in the canonical 5e stat-block layout instead of free-form prose.
_STATBLOCK_SUFFIX = (
    " When you present a character, monster, or NPC — anything with game "
    "statistics — format it as a classic D&D 5e stat block in this order: the "
    "creature's name; a line of 'Size type, alignment'; Armor Class; Hit "
    "Points (with Hit Dice); Speed; the six ability scores as a "
    "STR / DEX / CON / INT / WIS / CHA row, each with its score and modifier; "
    "then, where they apply, Saving Throws, Skills, Damage and Condition "
    "immunities, Senses, Languages, and Challenge (with XP); followed by any "
    "Traits, then Actions, Bonus Actions, Reactions, and Legendary Actions."
)

PERSONA_PROMPTS: dict[str, str] = {
    "sage": (
        "You are the Sage — an all-knowing D&D 5th Edition assistant. "
        + _GROUNDING_SUFFIX
        + _STATBLOCK_SUFFIX
    ),
    "spell": (
        "You are a Spell Archivist for D&D 5e. Reproduce the spell's rules "
        "text and description faithfully from the numbered sources: quote the "
        "casting time, range, components, duration, and effect text as "
        "written, including at-higher-levels text when present. Do not "
        "paraphrase, summarize, or embellish the rules text. "
        + _GROUNDING_SUFFIX
    ),
    "rules": (
        "You are a Rules Arbiter for D&D 5e, answering strictly by the rules "
        "as written (RAW). Quote the exact rules text from the sources; note "
        "errata or official clarifications when the sources contain them. Do "
        "not offer interpretations, rulings-at-the-table advice, house rules, "
        "or homebrew — if the written rules do not settle the question, say "
        "so plainly. "
        + _GROUNDING_SUFFIX
    ),
    "gm": (
        "You are the GM Oracle — a creative Dungeon Master assistant. "
        "Ground your answers in the D&D 5e sources provided, but you may "
        "extrapolate, invent, and create (monsters, NPCs, plot hooks) "
        "inspired by those sources. When inventing, say so explicitly. "
        "Cite sources where you draw from them; note invented content."
        + _STATBLOCK_SUFFIX
    ),
}

# Grounding template: sources block + question, sent as the user message.
GROUNDED_TEMPLATE = "Sources:\n{context}\n\nQuestion: {question}\n\nAnswer:"


def context_texts(result: RetrievalResult, top_n: int = CONTEXT_TOP_N) -> list[str]:
    """The full chunk texts generation builds its context from, in chunk order.
    Single source of truth shared with `build_context`, so eval consumers (the
    Ragas `contexts`) score exactly what the LLM saw — never display snippets."""
    return [result.text_for(c).strip() for c in result.chunks[:top_n]]


def build_context(result: RetrievalResult, top_n: int = CONTEXT_TOP_N) -> str:
    """Numbered source blocks with FULL chunk text, for the LLM context."""
    blocks: list[str] = []
    # strict=False is the intent, not an oversight: context_texts() returns only
    # the first `top_n` chunks' text, so the zip truncates result.chunks to the
    # context window on purpose.
    for i, (c, text) in enumerate(
        zip(result.chunks, context_texts(result, top_n), strict=False), start=1,
    ):
        label = c.entity_name or c.section or c.chapter or c.content_type
        blocks.append(f"[{i}] ({label}): {text}")
    return "\n\n".join(blocks)


def assemble_context(
    result: RetrievalResult, *,
    attachment_context: str | None, attachment_label: str | None,
    top_n: int = CONTEXT_TOP_N,
) -> str:
    """The exact string sent to the LLM as "Sources:" — corpus chunks via
    `build_context`, plus an uploaded attachment (if any) appended as its own
    numbered source continuing the [1..N] sequence (swe1.6 / 08il). Extracted
    (D2, agent-forge-harness-b8o.1) from `generate_node`'s inline block so both
    the graph and the eval capture harness build the identical string the LLM
    actually saw — see `service/tests/test_generate_context_assembly.py` for
    the characterization tests proving this is behavior-preserving.

    The suggestions path deliberately does NOT call this — it uses its own
    narrower `build_context()` without the attachment block (see
    `service/graph.py::suggest_node`); do not unify the two."""
    context = build_context(result, top_n=top_n)
    if not attachment_context:
        return context
    capped = cap_text(attachment_context, ATTACHMENT_MAX_CHARS)
    label = attachment_label or "your attachment"
    n = len(context_texts(result, top_n)) + 1
    attachment_block = f"[{n}] (Attachment — {label}): {capped}"
    return f"{context}\n\n{attachment_block}" if context else attachment_block


def build_sources(result: RetrievalResult, top_n: int = CONTEXT_TOP_N) -> list[Source]:
    """One Source per contributing chunk, deduped by (entity/section), snippet
    truncated for display."""
    seen: set[str] = set()
    sources: list[Source] = []
    for c in result.chunks[:top_n]:
        key = c.entity_name or c.section or c.chunk_id
        if key in seen:
            continue
        seen.add(key)
        full = result.text_for(c).strip().replace("\n", " ")
        snippet = full[:SNIPPET_MAX] + ("…" if len(full) > SNIPPET_MAX else "")
        sources.append(Source(
            book=result.book_for(c) or "D&D 5e",
            chapter=c.chapter, section=c.section, entity=c.entity_name,
            page=c.page_start, snippet=snippet,
        ))
    return sources


# Spell-usage suggestions (channel-chats CP-C). One extra LLM call in spell
# mode; the graph node degrades to no suggestions on any failure.
SUGGESTIONS_SYSTEM = (
    "You are a creative D&D 5e assistant. Given a spell's rules text, propose "
    "exactly three ways a character might use the spell: one practical "
    "(tactically effective), one roleplay (social or story flavor), and one "
    "wacky (unexpected, rule-bending fun). Respond with ONLY a JSON array of "
    'three objects, e.g. [{"style": "practical", "text": "..."}, '
    '{"style": "roleplay", "text": "..."}, {"style": "wacky", "text": "..."}]. '
    "No prose outside the JSON."
)

SUGGESTIONS_TEMPLATE = "Spell sources:\n{context}\n\nSpell question: {question}"

_CANONICAL_STYLES = (
    SuggestionStyle.practical, SuggestionStyle.roleplay, SuggestionStyle.wacky,
)


def parse_suggestions(text: str) -> list[Suggestion]:
    """Parse the suggestions JSON, tolerating a markdown code fence. Requires
    exactly one suggestion per canonical style; returns them in canonical
    order. Raises ValueError on anything else."""
    import json

    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[len("json"):]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"suggestions are not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("suggestions JSON must be an array")
    by_style: dict[SuggestionStyle, Suggestion] = {}
    for item in data:
        s = Suggestion.model_validate(item)
        by_style[s.style] = s
    if set(by_style) != set(_CANONICAL_STYLES):
        raise ValueError(
            f"need exactly one suggestion per style, got {sorted(s.value for s in by_style)}"
        )
    return [by_style[style] for style in _CANONICAL_STYLES]


def generate_suggestions(
    question: str, context: str, *,
    model: str = DEFAULT_MODEL, client: LLMClient | None = None,
    config: Any | None = None, observer: AttemptObserver | None = None,
) -> list[Suggestion]:
    """One structured LLM call for the three spell-usage ideas. Raises on any
    LLM or parse failure — the caller (graph suggest node) degrades to None."""
    if client is None:  # pragma: no cover - live path mirrors generate_answer
        from langchain_openai import ChatOpenAI

        client = ChatOpenAI(model=model, temperature=TEMPERATURE)
    result = generate_result(
        [
            SystemMessage(content=SUGGESTIONS_SYSTEM),
            HumanMessage(content=SUGGESTIONS_TEMPLATE.format(context=context, question=question)),
        ],
        alias=model, client=client, config=config, observer=observer,
    )
    return parse_suggestions(result.text)


def generate_answer(
    question: str, context: str, *, mode: str = "sage",
    model: str = DEFAULT_MODEL, client: LLMClient | None = None,
    config: Any | None = None, observer: AttemptObserver | None = None,
) -> str:
    """Call gpt-4o-mini with a per-mode system prompt + grounded user message.

    `mode` selects the persona from PERSONA_PROMPTS (defaults to 'sage').
    `client` is injectable for tests. `config` is the LangChain RunnableConfig
    (Langfuse callbacks); forwarded to the model so the LLM call is traced (CP3).
    """
    # Defensive: callers reach here only past the grounding gate (non-empty
    # context). An empty context or question is a programming error, not input.
    if not context.strip() or not question.strip():
        raise ValueError("generate_answer requires non-empty question and context")
    if client is None:
        # langchain-openai ChatOpenAI — the wrapper that lets Langfuse (CP3)
        # capture tokens/cost natively. Imported lazily so tests stay offline.
        from langchain_openai import ChatOpenAI

        client = ChatOpenAI(model=model, temperature=TEMPERATURE)
    system = PERSONA_PROMPTS.get(mode, PERSONA_PROMPTS["sage"])
    user_content = GROUNDED_TEMPLATE.format(context=context, question=question)
    result = generate_result(
        [SystemMessage(content=system), HumanMessage(content=user_content)],
        alias=model, client=client, config=config, observer=observer,
    )
    return result.text

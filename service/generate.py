"""
Context assembly + grounded answer generation (gpt-4o-mini).

`build_context` and `build_sources` are pure (no network) and operate on a
`RetrievalResult` using **full** chunk text (the 120-char preview is too short to
ground an answer — see the plan review). `generate_answer` calls the LLM and
accepts an injected client for tests.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from ingestion.retrieval import RetrievalResult

from .models import Source

SNIPPET_MAX = 240
DEFAULT_MODEL = "gpt-4o-mini"


# Minimal structural type for the injected LLM client. Captures only the call we
# make — `client.chat.completions.create(...)` — so an OpenAI client (or a test
# fake) satisfies it without mirroring the full SDK surface. The response is typed
# loosely (`Any`) on purpose: we only read `.choices[0].message.content`.
class _Completions(Protocol):
    def create(
        self, *, model: str, messages: list[dict[str, str]], temperature: float
    ) -> Any: ...  # pragma: no cover - structural type


class _Chat(Protocol):
    @property
    def completions(self) -> _Completions: ...  # pragma: no cover - structural type


class LLMClient(Protocol):
    @property
    def chat(self) -> _Chat: ...  # pragma: no cover - structural type

# Legacy constant kept for backward compatibility with any direct importers.
GROUNDED_PROMPT = (
    "You are a Dungeons & Dragons 5th Edition rules assistant. "
    "Answer the user's question using ONLY the numbered sources below. "
    "Cite the sources you use inline as [1], [2], etc. "
    "If the sources do not contain the answer, say you don't have that in your "
    "sources — do not use outside knowledge.\n\n"
    "Sources:\n{context}\n\nQuestion: {question}\n\nAnswer:"
)

# Shared grounding instruction appended to every grounded-mode system prompt.
_GROUNDING_SUFFIX = (
    "Answer the user's question using ONLY the numbered sources below. "
    "Cite the sources you use inline as [1], [2], etc. "
    "If the sources do not contain the answer, say you don't have that in your "
    "sources — do not use outside knowledge."
)

PERSONA_PROMPTS: dict[str, str] = {
    "sage": (
        "You are the Sage — an all-knowing D&D 5th Edition assistant. "
        + _GROUNDING_SUFFIX
    ),
    "spell": (
        "You are a Spell Archivist specializing in D&D 5e spells and cantrips. "
        "Be precise about components, ranges, durations, and upcasting. "
        + _GROUNDING_SUFFIX
    ),
    "rules": (
        "You are a Rules Arbiter for D&D 5e. Cite rules text exactly; "
        "be clear when a rule has errata or is disputed. "
        + _GROUNDING_SUFFIX
    ),
    "gm": (
        "You are the GM Oracle — a creative Dungeon Master assistant. "
        "Ground your answers in the D&D 5e sources provided, but you may "
        "extrapolate, invent, and create (monsters, NPCs, plot hooks) "
        "inspired by those sources. When inventing, say so explicitly. "
        "Cite sources where you draw from them; note invented content."
    ),
}

# Grounding template: sources block + question, sent as the user message.
GROUNDED_TEMPLATE = "Sources:\n{context}\n\nQuestion: {question}\n\nAnswer:"


def build_context(result: RetrievalResult, top_n: int = 5) -> str:
    """Numbered source blocks with FULL chunk text, for the LLM context."""
    blocks: list[str] = []
    for i, c in enumerate(result.chunks[:top_n], start=1):
        text = result.text_for(c).strip()
        label = c.entity_name or c.section or c.chapter or c.content_type
        blocks.append(f"[{i}] ({label}): {text}")
    return "\n\n".join(blocks)


def build_sources(result: RetrievalResult, top_n: int = 5) -> list[Source]:
    """One Source per contributing chunk, deduped by (entity/section), snippet
    truncated for display."""
    seen: set[str] = set()
    sources: list[Source] = []
    for c in result.chunks[:top_n]:
        key = (c.entity_name or c.section or c.chunk_id).lower()
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


def generate_answer(
    question: str, context: str, *, mode: str = "sage",
    model: str = DEFAULT_MODEL, client: LLMClient | None = None,
) -> str:
    """Call gpt-4o-mini with a per-mode system prompt + grounded user message.

    `mode` selects the persona from PERSONA_PROMPTS (defaults to 'sage').
    `client` is injectable for tests.
    """
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    system = PERSONA_PROMPTS.get(mode, PERSONA_PROMPTS["sage"])
    user_content = GROUNDED_TEMPLATE.format(context=context, question=question)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

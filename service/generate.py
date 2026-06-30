"""
Context assembly + grounded answer generation (gpt-4o-mini).

`build_context` and `build_sources` are pure (no network) and operate on a
`RetrievalResult` using **full** chunk text (the 120-char preview is too short to
ground an answer — see the plan review). `generate_answer` calls the LLM and
accepts an injected client for tests.
"""

from __future__ import annotations

from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from ingestion.retrieval import RetrievalResult

# Env-overridable tuning knobs live in the single top-level config module.
# DEFAULT_MODEL is re-exported here for `from .generate import DEFAULT_MODEL`.
from config import CONTEXT_TOP_N, DEFAULT_MODEL, SNIPPET_MAX, TEMPERATURE

from .models import Source


# Minimal structural type for the injected chat model (ziw.2 / CP2). We call a
# LangChain-style `client.invoke(messages)` and read `.content` off the result, so
# a `langchain_openai.ChatOpenAI` (or a test fake) satisfies it without depending on
# the concrete class. Response typed loosely (`Any`): we only read `.content`.
class LLMClient(Protocol):
    def invoke(
        self, input: Any, config: Any = None, **kwargs: Any
    ) -> Any: ...  # pragma: no cover - structural type

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


def build_context(result: RetrievalResult, top_n: int = CONTEXT_TOP_N) -> str:
    """Numbered source blocks with FULL chunk text, for the LLM context."""
    blocks: list[str] = []
    for i, c in enumerate(result.chunks[:top_n], start=1):
        text = result.text_for(c).strip()
        label = c.entity_name or c.section or c.chapter or c.content_type
        blocks.append(f"[{i}] ({label}): {text}")
    return "\n\n".join(blocks)


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


def generate_answer(
    question: str, context: str, *, mode: str = "sage",
    model: str = DEFAULT_MODEL, client: LLMClient | None = None,
) -> str:
    """Call gpt-4o-mini with a per-mode system prompt + grounded user message.

    `mode` selects the persona from PERSONA_PROMPTS (defaults to 'sage').
    `client` is injectable for tests.
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
    resp = client.invoke(
        [SystemMessage(content=system), HumanMessage(content=user_content)],
    )
    content = resp.content
    return content.strip() if isinstance(content, str) else str(content).strip()

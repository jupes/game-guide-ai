"""
Context assembly + grounded answer generation (gpt-4o-mini).

`build_context` and `build_sources` are pure (no network) and operate on a
`RetrievalResult` using **full** chunk text (the 120-char preview is too short to
ground an answer — see the plan review). `generate_answer` calls the LLM and
accepts an injected client for tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))
from retrieval import RetrievalResult  # noqa: E402

from .models import Source

SNIPPET_MAX = 240
DEFAULT_MODEL = "gpt-4o-mini"

GROUNDED_PROMPT = (
    "You are a Dungeons & Dragons 5th Edition rules assistant. "
    "Answer the user's question using ONLY the numbered sources below. "
    "Cite the sources you use inline as [1], [2], etc. "
    "If the sources do not contain the answer, say you don't have that in your "
    "sources — do not use outside knowledge.\n\n"
    "Sources:\n{context}\n\nQuestion: {question}\n\nAnswer:"
)


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
    question: str, context: str, *, model: str = DEFAULT_MODEL, client=None,
) -> str:
    """Call gpt-4o-mini with the grounded prompt. `client` injectable for tests."""
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    prompt = GROUNDED_PROMPT.format(context=context, question=question)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

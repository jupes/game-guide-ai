"""
RagService — orchestrates retrieve → answerability gate → generate → cite.

Stateless per call. The retriever (vocab loaded once) and optional reranker are
injected so the FastAPI app can build them at startup and tests can mock them.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))
from retrieval import RagRetriever, RetrievalResult  # noqa: E402

from .generate import DEFAULT_MODEL, build_context, build_sources, generate_answer
from .models import ChatMode, ChatResponse

REFUSAL = "I couldn't find that in the D&D 5e sources I have."
CONTEXT_TOP_N = 5

# ---------------------------------------------------------------------------
# Mode → retrieval scope mapping
# ---------------------------------------------------------------------------

_SPELL_BOOKS: frozenset[str] = frozenset({
    "phb-5e", "xge-5e", "tce-5e", "eepc-5e",
    "scag-5e", "tortle-5e", "eberron-5e", "ravnica-5e",
})

_RULES_CTYPES: frozenset[str] = frozenset({
    "rule", "class_feature", "condition", "race_feature", "background", "feat",
})

_GM_FORCED_CTYPES: frozenset[str] = frozenset({
    "monster", "dm_guidance", "magic_item",
})


def _scope_for_mode(
    mode: str,
    query_ctypes: set[str],
) -> tuple[set[str] | None, set[str] | None]:
    """Pure function: map (mode, query-derived ctypes) → (effective_ctypes, allowed_books).

    Returns:
        effective_ctypes: set passed to the retriever's content_types filter,
                          or None for unscoped (all content types).
        allowed_books:    set passed to the retriever's book_slugs filter,
                          or None for unscoped (all books).

    Modes:
        sage  — unscoped; query-derived ctypes + no book restriction.
        spell — forces content_types={"spell"}, restricts to spell-bearing books.
        rules — forces content_types=rules allowlist (merges with query-derived,
                 but strips out non-rules types).
        gm    — merges forced creative ctypes with query-derived; no book restriction.
    """
    if mode == "spell":
        return {"spell"}, set(_SPELL_BOOKS)

    if mode == "rules":
        # Intersection of query-derived and rules allowlist; fall back to full allowlist.
        intersection = query_ctypes & _RULES_CTYPES
        effective = intersection if intersection else set(_RULES_CTYPES)
        return effective, None

    if mode == "gm":
        # Union of query-derived ctypes with the GM forced set.
        effective = query_ctypes | set(_GM_FORCED_CTYPES)
        return effective, None

    # sage (and any unrecognised mode) — pass through query-derived, no book limit.
    return query_ctypes or None, None


# ---------------------------------------------------------------------------
# Secondary retriever seam (stubbed now; drop-in for a future world corpus)
# ---------------------------------------------------------------------------

@dataclass
class SecondaryResult:
    """Minimal result type returned by a secondary retriever."""
    chunks: list = field(default_factory=list)
    full_texts: dict = field(default_factory=dict)
    book_by_id: dict = field(default_factory=dict)
    answerable: bool = False


class SecondaryRetriever(Protocol):
    """Protocol for a secondary (world/campaign) corpus retriever."""
    def retrieve(self, prompt: str, k: int = 5) -> SecondaryResult: ...  # pragma: no cover


class StubSecondaryRetriever:
    """No-op stub — always returns empty results.

    Replace with a real SecondaryRetriever when the world corpus is built.
    """

    def retrieve(self, prompt: str, k: int = 5) -> SecondaryResult:
        return SecondaryResult()


# ---------------------------------------------------------------------------
# RagService
# ---------------------------------------------------------------------------

class RagService:
    def __init__(
        self, retriever=None, *, reranker=None, dsn: str | None = None,
        model: str = DEFAULT_MODEL, llm_client=None,
        secondary_retriever=None,
    ):
        self.retriever = retriever or RagRetriever(dsn)
        self.reranker = reranker
        self.model = model
        self.llm_client = llm_client  # injected OpenAI-like client (tests)
        self.secondary = secondary_retriever or StubSecondaryRetriever()

    def _merge_results(
        self, primary: RetrievalResult, secondary: SecondaryResult,
    ) -> RetrievalResult:
        """Merge primary + secondary chunks; primary chunks take precedence.

        Deduplication by chunk_id. When secondary is empty (stub), primary is
        returned unchanged.
        """
        if not secondary.chunks:
            return primary

        primary_ids = {c.chunk_id for c in primary.chunks}
        extra_chunks = [c for c in secondary.chunks if c.chunk_id not in primary_ids]
        merged_chunks = primary.chunks + extra_chunks
        # Primary wins on key collision
        merged_texts = {**secondary.full_texts, **primary.full_texts}
        merged_books = {**secondary.book_by_id, **primary.book_by_id}
        return RetrievalResult(
            chunks=merged_chunks,
            full_texts=merged_texts,
            top1_distance=primary.top1_distance,
            answerable=primary.answerable or secondary.answerable,
            book_by_id=merged_books,
            matched_classes=primary.matched_classes,
            matched_entities=primary.matched_entities,
            matched_content_types=primary.matched_content_types,
        )

    def answer(
        self, prompt: str, mode: str = "sage", conversation_id: str | None = None,
    ) -> ChatResponse:
        result = self.retriever.retrieve(prompt, reranker=self.reranker, mode=mode)

        # Second-source merge (GM mode only; stub is a no-op).
        if mode == "gm":
            secondary = self.secondary.retrieve(prompt)
            result = self._merge_results(result, secondary)

        # Grounding gate: strict for sage/spell/rules; relaxed for gm.
        if mode == "gm":
            # GM: proceed when any chunks exist; answerable=False is allowed
            # (marks creative/partly-inventive output for the client).
            if not result.chunks:
                return ChatResponse(
                    answer=REFUSAL, sources=[], answerable=False,
                    mode=ChatMode(mode), conversation_id=conversation_id,
                )
        else:
            # sage / spell / rules: strict koz gate.
            if not result.answerable or not result.chunks:
                return ChatResponse(
                    answer=REFUSAL, sources=[], answerable=False,
                    mode=ChatMode(mode), conversation_id=conversation_id,
                )

        context = build_context(result, top_n=CONTEXT_TOP_N)
        answer = generate_answer(
            prompt, context, mode=mode, model=self.model, client=self.llm_client,
        )
        sources = build_sources(result, top_n=CONTEXT_TOP_N)
        return ChatResponse(
            answer=answer, sources=sources, answerable=result.answerable,
            mode=ChatMode(mode), conversation_id=conversation_id,
        )

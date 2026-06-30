"""
RagService — orchestrates retrieve → answerability gate → generate → cite.

Stateless per call. The retriever (vocab loaded once) and optional reranker are
injected so the FastAPI app can build them at startup and tests can mock them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ingestion.retrieval import RagRetriever, RetrievalResult, RetrievedChunk

from .generate import DEFAULT_MODEL, LLMClient
from .models import ChatMode, ChatResponse

REFUSAL = "I couldn't find that in the D&D 5e sources I have."

# Mode → retrieval scope mapping lives in the canonical leaf module
# `ingestion/scope.py` (`scope_for_mode`); the retriever applies it. The service
# does not scope directly, so it no longer carries its own copy.

# ---------------------------------------------------------------------------
# Secondary retriever seam (stubbed now; drop-in for a future world corpus)
# ---------------------------------------------------------------------------

@dataclass
class SecondaryResult:
    """Minimal result type returned by a secondary retriever."""
    chunks: list[RetrievedChunk] = field(default_factory=list)
    full_texts: dict[str, str] = field(default_factory=dict)
    book_by_id: dict[str, str] = field(default_factory=dict)
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
        model: str = DEFAULT_MODEL, llm_client: LLMClient | None = None,
        secondary_retriever=None,
    ):
        self.retriever = retriever or RagRetriever(dsn)
        self.reranker = reranker
        self.model = model
        self.llm_client: LLMClient | None = llm_client  # injected OpenAI-like client (tests)
        self.secondary = secondary_retriever or StubSecondaryRetriever()
        self._graph: Any = None  # compiled LangGraph pipeline (lazy; see _compiled_graph)

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
        # Validate the mode up front so an invalid value fails fast with a clear
        # error instead of silently scoping-as-sage and then raising at response
        # build (the API layer already 422s real users via the ChatMode enum).
        try:
            mode_enum = ChatMode(mode)
        except ValueError:
            raise ValueError(f"unknown mode: {mode!r}") from None

        # Empty/whitespace prompt → refuse without spending retrieval or an LLM
        # call (the API enforces min_length=1; this guards direct callers).
        if not prompt.strip():
            return ChatResponse(
                answer=REFUSAL, sources=[], answerable=False,
                mode=mode_enum, conversation_id=conversation_id,
            )

        # The retrieve -> grounding gate -> generate|refuse core now runs as a
        # LangGraph graph (ziw.2 / Phase 1). Behavior is identical to the prior
        # imperative flow; the graph orchestrates the same building blocks.
        final = self._compiled_graph().invoke({"prompt": prompt, "mode": mode})
        return ChatResponse(
            answer=final["answer"], sources=final["sources"],
            answerable=final["answerable"],
            mode=mode_enum, conversation_id=conversation_id,
        )

    def _compiled_graph(self):
        """Lazily build + cache the pipeline graph (langgraph imported on first use
        so constructing a RagService stays cheap)."""
        if self._graph is None:
            from .graph import build_rag_graph

            self._graph = build_rag_graph(self)
        return self._graph

"""
RagService — orchestrates retrieve → answerability gate → generate → cite.

Stateless per call. The retriever (vocab loaded once) and optional reranker are
injected so the FastAPI app can build them at startup and tests can mock them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))
from retrieval import RagRetriever  # noqa: E402

from .generate import DEFAULT_MODEL, build_context, build_sources, generate_answer
from .models import ChatResponse

REFUSAL = "I couldn't find that in the D&D 5e sources I have."
CONTEXT_TOP_N = 5


class RagService:
    def __init__(
        self, retriever=None, *, reranker=None, dsn: str | None = None,
        model: str = DEFAULT_MODEL, llm_client=None,
    ):
        self.retriever = retriever or RagRetriever(dsn)
        self.reranker = reranker
        self.model = model
        self.llm_client = llm_client  # injected OpenAI-like client (tests)

    def answer(self, prompt: str) -> ChatResponse:
        result = self.retriever.retrieve(prompt, reranker=self.reranker)

        # koz gate: refuse out-of-corpus prompts without calling the LLM.
        if not result.answerable or not result.chunks:
            return ChatResponse(answer=REFUSAL, sources=[], answerable=False)

        context = build_context(result, top_n=CONTEXT_TOP_N)
        answer = generate_answer(prompt, context, model=self.model, client=self.llm_client)
        sources = build_sources(result, top_n=CONTEXT_TOP_N)
        return ChatResponse(answer=answer, sources=sources, answerable=True)

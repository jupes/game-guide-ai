"""
LangGraph orchestration of the RAG pipeline (ziw.2 / Phase 1 — foundational migration).

Models RagService.answer's core pipeline as a graph:

    START -> retrieve --(grounding gate)--> generate -> END
                              \--> refuse --> END

Behavior parity with the prior imperative implementation. `RagService.answer` now
delegates here. Pre-flight checks that must run BEFORE retrieval — invalid-mode
(raises ValueError) and empty-prompt refusal — stay in the service wrapper, so this
graph models only the retrieve->gate->generate|refuse core.

CP1 of the migration: the graph orchestrates the EXISTING building blocks
(`retriever.retrieve`, `build_context`, `generate_answer`, `build_sources`). The LLM
still flows through `generate_answer` (raw OpenAI SDK); CP2 swaps that node to
`langchain-openai` `ChatOpenAI`. The grounding gate is a conditional edge here; CP3
may promote it to its own node for finer trace granularity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from config import CONTEXT_TOP_N

from .generate import build_context, build_sources, generate_answer
from .models import Source

if TYPE_CHECKING:
    from .rag import RagService


class GraphState(TypedDict, total=False):
    """State threaded through the graph (last-value-wins per key)."""
    prompt: str
    mode: str
    result: Any  # ingestion.retrieval.RetrievalResult (kept loose to avoid a hard import here)
    answer: str
    sources: list[Source]
    answerable: bool


def build_rag_graph(svc: RagService) -> Any:
    """Compile the retrieve -> gate -> (generate|refuse) graph, closing over the
    service's injected dependencies (retriever, reranker, model, llm_client,
    secondary). Returns a compiled LangGraph runnable; invoke with
    `{"prompt": ..., "mode": ...}` and read `answer` / `sources` / `answerable`."""
    from langgraph.graph import END, START, StateGraph

    def retrieve_node(state: GraphState) -> GraphState:
        result = svc.retriever.retrieve(
            state["prompt"], reranker=svc.reranker, mode=state["mode"],
        )
        # Second-source merge (GM mode only; stub is a no-op).
        if state["mode"] == "gm":
            secondary = svc.secondary.retrieve(state["prompt"])
            result = svc._merge_results(result, secondary)
        return {"result": result}

    def gate_route(state: GraphState) -> Literal["generate", "refuse"]:
        result = state["result"]
        if state["mode"] == "gm":
            # GM: proceed when any chunks exist; answerable=False is allowed
            # (marks creative/partly-inventive output for the client).
            return "generate" if result.chunks else "refuse"
        # sage / spell / rules: strict koz gate — need answerable AND chunks.
        return "generate" if (result.answerable and result.chunks) else "refuse"

    def generate_node(state: GraphState) -> GraphState:
        result = state["result"]
        context = build_context(result, top_n=CONTEXT_TOP_N)
        answer = generate_answer(
            state["prompt"], context, mode=state["mode"],
            model=svc.model, client=svc.llm_client,
        )
        sources = build_sources(result, top_n=CONTEXT_TOP_N)
        return {"answer": answer, "sources": sources, "answerable": result.answerable}

    def refuse_node(state: GraphState) -> GraphState:
        # Deferred import avoids a circular import (rag imports this module).
        from .rag import REFUSAL

        return {"answer": REFUSAL, "sources": [], "answerable": False}

    g = StateGraph(GraphState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("generate", generate_node)
    g.add_node("refuse", refuse_node)

    g.add_edge(START, "retrieve")
    g.add_conditional_edges(
        "retrieve", gate_route, {"generate": "generate", "refuse": "refuse"},
    )
    g.add_edge("generate", END)
    g.add_edge("refuse", END)
    return g.compile()

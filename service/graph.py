"""
LangGraph orchestration of the RAG pipeline (ziw.2 foundation; 1em unification).

Models the FULL request pipeline as a graph — pre-flight checks included, so
`RagService.answer` is just invoke + response mapping:

    START -> preflight --(empty prompt)--------------------> refuse -> END
                | (valid)
                v
             retrieve -> gate --(refuse)--> refuse -> END
                            \\--(generate)--> generate -> END

`preflight` validates the mode (raises ValueError for unknown modes, matching
the old service-layer contract) and routes empty/whitespace prompts straight to
`refuse` without spending retrieval or an LLM call. `gate` is the grounding
gate as a first-class node (CP-B): it records the route decision in state so
the gate itself shows up as a traced span, with the conditional edge reading
that decision. The LLM flows through `generate_answer` (`langchain-openai`
`ChatOpenAI` or an injected fake); Langfuse tracing attaches via the run config
passed to `invoke` (env-gated, off by default — see tracing.py).

1em.3 (CP-C) explodes `retrieve` into embed/extract_hints/scope/search/
fetch_texts/rerank nodes and splits `cite` out of `generate`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from config import CONTEXT_TOP_N

from ingestion.retrieval import RetrievalResult

from .generate import build_context, build_sources, generate_answer
from .models import ChatMode, REFUSAL, Source

if TYPE_CHECKING:
    from .rag import RagService


class GraphState(TypedDict, total=False):
    """State threaded through the graph (last-value-wins per key)."""
    prompt: str
    mode: str
    result: RetrievalResult
    route: Literal["generate", "refuse"]   # the gate node's decision
    answer: str
    sources: list[Source]
    answerable: bool


def build_rag_graph(svc: RagService) -> Any:
    """Compile the preflight -> retrieve -> gate -> (generate|refuse) graph,
    closing over the service's injected dependencies (retriever, reranker,
    model, llm_client, secondary). Returns a compiled LangGraph runnable;
    invoke with `{"prompt": ..., "mode": ...}` and read `answer` / `sources` /
    `answerable`."""
    from langgraph.graph import END, START, StateGraph

    def preflight_node(state: GraphState) -> GraphState:
        # Unknown mode fails fast with a clear error (direct callers only; the
        # API layer already 422s real users via the ChatMode enum).
        try:
            ChatMode(state["mode"])
        except ValueError:
            raise ValueError(f"unknown mode: {state['mode']!r}") from None
        return {}

    def preflight_route(state: GraphState) -> Literal["retrieve", "refuse"]:
        # Empty/whitespace prompt refuses without retrieval or an LLM call.
        return "retrieve" if state["prompt"].strip() else "refuse"

    def retrieve_node(state: GraphState) -> GraphState:
        result = svc.retriever.retrieve(
            state["prompt"], reranker=svc.reranker, mode=state["mode"],
        )
        # Second-source merge (GM mode only; stub is a no-op).
        if state["mode"] == "gm":
            secondary = svc.secondary.retrieve(state["prompt"])
            result = svc._merge_results(result, secondary)
        return {"result": result}

    def gate_node(state: GraphState) -> GraphState:
        result = state["result"]
        if state["mode"] == "gm":
            # GM: proceed when any chunks exist; answerable=False is allowed
            # (marks creative/partly-inventive output for the client).
            route: Literal["generate", "refuse"] = (
                "generate" if result.chunks else "refuse"
            )
        else:
            # sage / spell / rules: strict koz gate — need answerable AND chunks.
            route = "generate" if (result.answerable and result.chunks) else "refuse"
        return {"route": route}

    def gate_route(state: GraphState) -> Literal["generate", "refuse"]:
        return state["route"]

    def generate_node(state: GraphState, config: Any = None) -> GraphState:
        # LangGraph injects the run `config` (Langfuse callbacks) as the 2nd arg;
        # forward it to the LLM call so the generation emits a token/cost span.
        result = state["result"]
        context = build_context(result, top_n=CONTEXT_TOP_N)
        answer = generate_answer(
            state["prompt"], context, mode=state["mode"],
            model=svc.model, client=svc.llm_client, config=config,
        )
        sources = build_sources(result, top_n=CONTEXT_TOP_N)
        return {"answer": answer, "sources": sources, "answerable": result.answerable}

    def refuse_node(state: GraphState) -> GraphState:
        return {"answer": REFUSAL, "sources": [], "answerable": False}

    g = StateGraph(GraphState)
    g.add_node("preflight", preflight_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("gate", gate_node)
    g.add_node("generate", generate_node)
    g.add_node("refuse", refuse_node)

    g.add_edge(START, "preflight")
    g.add_conditional_edges(
        "preflight", preflight_route, {"retrieve": "retrieve", "refuse": "refuse"},
    )
    g.add_edge("retrieve", "gate")
    g.add_conditional_edges(
        "gate", gate_route, {"generate": "generate", "refuse": "refuse"},
    )
    g.add_edge("generate", END)
    g.add_edge("refuse", END)
    return g.compile()

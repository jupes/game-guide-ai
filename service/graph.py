"""
LangGraph orchestration of the RAG pipeline (ziw.2 foundation; 1em unification).

Models the FULL request pipeline as a graph — pre-flight checks and every
retrieval stage included, so `RagService.answer` is just invoke + response
mapping and each stage gets its own trace span:

    START -> preflight --(empty prompt)---------------------------> refuse -> END
                | (valid)
                v
             embed -> extract_hints -> scope -> search -> fetch_texts
                                                              |
                                    (reranker ∧ prose query)? rerank
                                                              v
                                                            merge   (GM second source)
                                                              v
                                          gate --(refuse)--> refuse -> END
                                             \\--(generate)--> generate -> cite -> END

`preflight` validates the mode (raises ValueError for unknown modes, matching
the old service-layer contract) and routes empty/whitespace prompts straight to
`refuse` without spending retrieval or an LLM call. The retrieval stages call
`RagRetriever`'s granular stage methods (1em.3); `rerank` runs only when a
reranker is configured AND the query's content types are prose-like
(`should_rerank`, the bo4 gate). `merge` folds in the GM secondary corpus
(stubbed; no-op for other modes — 1em.4 makes it a parallel branch). `gate` is
the grounding gate as a first-class node; `cite` builds the Sources list apart
from `generate` so answer generation and citation assembly trace separately.
The LLM flows through `generate_answer` (`langchain-openai` `ChatOpenAI` or an
injected fake); Langfuse tracing attaches via the run config passed to `invoke`
(env-gated, off by default — see tracing.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from config import CONTEXT_TOP_N, TOP_K

from ingestion.rerank import should_rerank
from ingestion.retrieval import RetrievalResult, RetrievedChunk, assemble_result
from ingestion.scope import scope_for_mode

from .generate import build_context, build_sources, generate_answer
from .models import ChatMode, REFUSAL, Source

if TYPE_CHECKING:
    from .rag import RagService


class GraphState(TypedDict, total=False):
    """State threaded through the graph (last-value-wins per key)."""
    prompt: str
    mode: str
    # retrieval stages
    emb: list[float]
    classes: set[str]
    entities: set[str]
    ctypes: set[str]                      # query-derived (drives the rerank gate)
    effective_ctypes: set[str] | None     # post-scope filters (None = unscoped)
    allowed_books: set[str] | None
    chunks: list[RetrievedChunk]
    result: RetrievalResult
    # gate + output
    route: Literal["generate", "refuse"]  # the gate node's decision
    answer: str
    sources: list[Source]
    answerable: bool


def build_rag_graph(svc: RagService) -> Any:
    """Compile the full pipeline graph, closing over the service's injected
    dependencies (retriever, reranker, model, llm_client, secondary). Returns a
    compiled LangGraph runnable; invoke with `{"prompt": ..., "mode": ...}` and
    read `answer` / `sources` / `answerable`."""
    from langgraph.graph import END, START, StateGraph

    def preflight_node(state: GraphState) -> GraphState:
        # Unknown mode fails fast with a clear error (direct callers only; the
        # API layer already 422s real users via the ChatMode enum).
        try:
            ChatMode(state["mode"])
        except ValueError:
            raise ValueError(f"unknown mode: {state['mode']!r}") from None
        return {}

    def preflight_route(state: GraphState) -> Literal["embed", "refuse"]:
        # Empty/whitespace prompt refuses without retrieval or an LLM call.
        return "embed" if state["prompt"].strip() else "refuse"

    def embed_node(state: GraphState) -> GraphState:
        return {"emb": svc.retriever.embed(state["prompt"])}

    def extract_hints_node(state: GraphState) -> GraphState:
        classes, entities, ctypes = svc.retriever.analyze(state["prompt"])
        return {"classes": classes, "entities": entities, "ctypes": ctypes}

    def scope_node(state: GraphState) -> GraphState:
        effective_ctypes, allowed_books = scope_for_mode(state["mode"], state["ctypes"])
        return {"effective_ctypes": effective_ctypes, "allowed_books": allowed_books}

    def search_node(state: GraphState) -> GraphState:
        chunks = svc.retriever.search(
            state["emb"], state["prompt"], TOP_K,
            state["classes"], state["entities"],
            state["effective_ctypes"], state["allowed_books"],
        )
        return {"chunks": chunks}

    def fetch_texts_node(state: GraphState) -> GraphState:
        full, book_by_id = svc.retriever.fetch(state["chunks"])
        result = assemble_result(
            state["chunks"], full, book_by_id,
            state["classes"], state["entities"], state["ctypes"],
        )
        return {"result": result}

    def rerank_route(state: GraphState) -> Literal["rerank", "merge"]:
        # bo4 gate: rerank only prose-like queries, and only when configured.
        if (
            svc.reranker is not None
            and state["result"].chunks
            and should_rerank(state["ctypes"])
        ):
            return "rerank"
        return "merge"

    def rerank_node(state: GraphState) -> GraphState:
        # top1/answerable stay pre-rerank (parity with the composed retrieve()).
        result = state["result"]
        texts = [result.text_for(c) for c in result.chunks]
        order = svc.reranker.rerank(state["prompt"], texts)
        result.chunks = [result.chunks[i] for i in order]
        return {"result": result}

    def merge_node(state: GraphState) -> GraphState:
        # Second-source merge (GM mode only; stub is a no-op). Runs post-rerank,
        # matching the pre-split pipeline order.
        result = state["result"]
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
        return {"answer": answer, "answerable": result.answerable}

    def cite_node(state: GraphState) -> GraphState:
        return {"sources": build_sources(state["result"], top_n=CONTEXT_TOP_N)}

    def refuse_node(state: GraphState) -> GraphState:
        return {"answer": REFUSAL, "sources": [], "answerable": False}

    g = StateGraph(GraphState)
    g.add_node("preflight", preflight_node)
    g.add_node("embed", embed_node)
    g.add_node("extract_hints", extract_hints_node)
    g.add_node("scope", scope_node)
    g.add_node("search", search_node)
    g.add_node("fetch_texts", fetch_texts_node)
    g.add_node("rerank", rerank_node)
    g.add_node("merge", merge_node)
    g.add_node("gate", gate_node)
    g.add_node("generate", generate_node)
    g.add_node("cite", cite_node)
    g.add_node("refuse", refuse_node)

    g.add_edge(START, "preflight")
    g.add_conditional_edges(
        "preflight", preflight_route, {"embed": "embed", "refuse": "refuse"},
    )
    g.add_edge("embed", "extract_hints")
    g.add_edge("extract_hints", "scope")
    g.add_edge("scope", "search")
    g.add_edge("search", "fetch_texts")
    g.add_conditional_edges(
        "fetch_texts", rerank_route, {"rerank": "rerank", "merge": "merge"},
    )
    g.add_edge("rerank", "merge")
    g.add_edge("merge", "gate")
    g.add_conditional_edges(
        "gate", gate_route, {"generate": "generate", "refuse": "refuse"},
    )
    g.add_edge("generate", "cite")
    g.add_edge("cite", END)
    g.add_edge("refuse", END)
    return g.compile()

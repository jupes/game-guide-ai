"""
LangGraph orchestration of the RAG pipeline (ziw.2 foundation; 1em unification).

Models the FULL request pipeline as a graph — pre-flight checks and every
retrieval stage included, so `RagService.answer` is just invoke + response
mapping and each stage gets its own trace span:

    START -> preflight --(empty prompt)---------------------------> refuse -> END
                | (valid)
                v
             embed -> extract_hints -> scope -> search -> fetch_texts
                                          |                   |
                                (gm) secondary      (reranker ∧ prose)? rerank
                                          :                   v
                                          :.....(by state)> merge
                                                              v
                                          gate --(refuse)--> refuse -> END
                                             \\--(generate)--> generate -> cite -> END

`preflight` validates the mode (raises ValueError for unknown modes, matching
the old service-layer contract) and routes empty/whitespace prompts straight to
`refuse` without spending retrieval or an LLM call. The retrieval stages call
`RagRetriever`'s granular stage methods (1em.3); `rerank` runs only when a
reranker is configured AND the query's content types are prose-like
(`should_rerank`, the bo4 gate). In GM mode, `scope` FANS OUT to the secondary
(world/campaign) corpus in parallel with the primary search branch (1em.4);
the branches join BY STATE at `merge` (primary chunks first, deduped — the
stub secondary is a no-op). `gate` is the grounding gate as a first-class
node; `cite` builds the Sources list apart from `generate` so answer
generation and citation assembly trace separately. The LLM flows through
`generate_answer` (`langchain-openai` `ChatOpenAI` or an injected fake);
Langfuse tracing attaches via the run config passed to `invoke` (env-gated,
off by default — see tracing.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from config import ATTACHMENT_MAX_CHARS, CONTEXT_TOP_N, SNIPPET_MAX, TOP_K

from ingestion.rerank import should_rerank
from ingestion.retrieval import RetrievalResult, RetrievedChunk, assemble_result
from ingestion.scope import scope_for_mode

import logging

from .attachments import cap_text
from .generate import (
    build_context,
    build_sources,
    context_texts,
    generate_answer,
    generate_suggestions,
)
from .models import ChatMode, REFUSAL, Source, Suggestion

log = logging.getLogger(__name__)

# Runtime import (not TYPE_CHECKING): LangGraph resolves GraphState's
# annotations when building the state schema. No cycle — service.rag imports
# this module lazily (inside _compiled_graph), never at module load.
from .rag import SecondaryResult

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
    secondary_result: SecondaryResult     # GM parallel branch output (gm only)
    # File attachments (swe1.6) — a conversation's uploaded-file text, injected
    # as a sibling context source. Present ⇒ the gate relaxes (an off-corpus
    # "ask about my file" question must still generate, not refuse).
    attachment_context: str | None
    attachment_label: str | None
    # gate + output
    route: Literal["generate", "refuse"]  # the gate node's decision
    answer: str
    sources: list[Source]
    answerable: bool
    suggestions: list[Suggestion] | None   # spell mode only; None on failure


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

    def scope_route(state: GraphState) -> list[str]:
        # GM fans out to the secondary (world/campaign) corpus IN PARALLEL with
        # the primary search branch; other modes run the primary branch only.
        if state["mode"] == "gm":
            return ["search", "secondary"]
        return ["search"]

    def secondary_node(state: GraphState) -> GraphState:
        return {"secondary_result": svc.secondary.retrieve(state["prompt"])}

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
        # Join point of the parallel branches. The secondary branch (one hop,
        # gm only) finishes in an earlier superstep than the primary chain and
        # joins by state: its secondary_result is visible here. Merge keeps
        # primary chunks first and dedupes by chunk_id; the stub secondary is
        # a no-op. Runs post-rerank, matching the pre-split order.
        result = state["result"]
        secondary = state.get("secondary_result")
        if secondary is not None:
            result = svc._merge_results(result, secondary)
        return {"result": result}

    def gate_node(state: GraphState) -> GraphState:
        result = state["result"]
        if state.get("attachment_context"):
            # swe1.6: an attachment can ground the answer even when the D&D
            # corpus can't — e.g. "what does my homebrew doc say?" is entirely
            # off-corpus. Route to generate regardless of corpus answerability;
            # generate_node (not here) sets the final `answerable` flag.
            route: Literal["generate", "refuse"] = "generate"
        elif state["mode"] == "gm":
            # GM: proceed when any chunks exist; answerable=False is allowed
            # (marks creative/partly-inventive output for the client).
            route = "generate" if result.chunks else "refuse"
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
        attachment_context = state.get("attachment_context")
        if attachment_context:
            # Capped HERE (not upstream) so the limit is observable through
            # RagService.answer regardless of who set attachment_context.
            capped = cap_text(attachment_context, ATTACHMENT_MAX_CHARS)
            label = state.get("attachment_label") or "your attachment"
            # Present the attachment as a first-class NUMBERED source continuing
            # the [1..N] sequence (08il). An unnumbered block was invisible to
            # the "answer using ONLY the numbered sources" grounding rule, so the
            # model refused ("I can't see attachments") even with the file in hand.
            n = len(context_texts(result, CONTEXT_TOP_N)) + 1
            attachment_block = f"[{n}] (Attachment — {label}): {capped}"
            context = f"{context}\n\n{attachment_block}" if context else attachment_block
        answer = generate_answer(
            state["prompt"], context, mode=state["mode"],
            model=svc.model, client=svc.llm_client, config=config,
        )
        # An attachment can ground an answer the corpus alone couldn't — treat
        # the response as answerable even when corpus retrieval wasn't.
        answerable = result.answerable or bool(attachment_context)
        return {"answer": answer, "answerable": answerable}

    def generate_route(state: GraphState) -> Literal["suggest", "cite"]:
        # Spell mode detours through the suggestions node; everyone else cites.
        return "suggest" if state["mode"] == "spell" else "cite"

    def suggest_node(state: GraphState, config: Any = None) -> GraphState:
        # Best-effort garnish: any LLM/parse failure degrades to no suggestions
        # rather than failing an answer that already generated.
        context = build_context(state["result"], top_n=CONTEXT_TOP_N)
        try:
            suggestions = generate_suggestions(
                state["prompt"], context,
                model=svc.model, client=svc.llm_client, config=config,
            )
        except Exception:
            log.warning("spell suggestions failed; answering without them", exc_info=True)
            return {"suggestions": None}
        return {"suggestions": suggestions}

    def cite_node(state: GraphState) -> GraphState:
        sources = build_sources(state["result"], top_n=CONTEXT_TOP_N)
        attachment_context = state.get("attachment_context")
        if attachment_context:
            label = state.get("attachment_label") or "your attachment"
            snippet = cap_text(attachment_context, SNIPPET_MAX)
            sources.append(Source(
                book=label, section="Attachment", snippet=snippet,
            ))
        return {"sources": sources}

    def refuse_node(state: GraphState) -> GraphState:
        return {"answer": REFUSAL, "sources": [], "answerable": False}

    g = StateGraph(GraphState)
    g.add_node("preflight", preflight_node)
    g.add_node("embed", embed_node)
    g.add_node("extract_hints", extract_hints_node)
    g.add_node("scope", scope_node)
    g.add_node("search", search_node)
    g.add_node("fetch_texts", fetch_texts_node)
    g.add_node("secondary", secondary_node)
    g.add_node("rerank", rerank_node)
    g.add_node("merge", merge_node)
    g.add_node("gate", gate_node)
    g.add_node("generate", generate_node)
    g.add_node("suggest", suggest_node)
    g.add_node("cite", cite_node)
    g.add_node("refuse", refuse_node)

    g.add_edge(START, "preflight")
    g.add_conditional_edges(
        "preflight", preflight_route, {"embed": "embed", "refuse": "refuse"},
    )
    g.add_edge("embed", "extract_hints")
    g.add_edge("extract_hints", "scope")
    g.add_conditional_edges(
        "scope", scope_route, {"search": "search", "secondary": "secondary"},
    )
    g.add_edge("search", "fetch_texts")
    g.add_conditional_edges(
        "fetch_texts", rerank_route, {"rerank": "rerank", "merge": "merge"},
    )
    g.add_edge("rerank", "merge")
    # NOTE deliberate: no secondary -> merge edge. The branches join BY STATE —
    # `secondary` runs in the same superstep as `search` (true fan-out) and
    # ends after writing secondary_result; `merge` on the primary chain reads
    # it. An explicit edge would double-trigger merge on the shorter branch
    # (langgraph <0.4 has no defer= to barrier unequal-length branches).
    g.add_edge("merge", "gate")
    g.add_conditional_edges(
        "gate", gate_route, {"generate": "generate", "refuse": "refuse"},
    )
    g.add_conditional_edges(
        "generate", generate_route, {"suggest": "suggest", "cite": "cite"},
    )
    g.add_edge("suggest", "cite")
    g.add_edge("cite", END)
    g.add_edge("refuse", END)
    return g.compile()

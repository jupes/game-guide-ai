#!/usr/bin/env python
"""
Phase 0 spike (epic ziw / ziw.1): prove that the rag-chat pipeline modeled as a
**LangGraph graph** emits **node-level traces** to **Langfuse** via the LangChain
callback handler — the integration the foundational migration (ziw.2) will rely on.

This is a SPIKE, not the migration. It mirrors the real pipeline's *seams*
(`RagService.answer`: retrieve -> grounding_gate -> generate) as graph nodes, but
**stubs retrieval** so it runs without a populated pgvector DB. The LLM node uses
`langchain-openai`'s `ChatOpenAI` (the model wrapper the migration will adopt) so
tokens/cost/latency surface natively in the trace.

What it demonstrates / decides:
  - LangGraph graph executes retrieve -> gate -> (generate | refuse).
  - Langfuse `CallbackHandler` captures a span per node + the LLM call, tagged with
    `model`, `service_version` (git SHA), and `mode`.
  - => Langfuse is a viable backend for the epic (decision recorded in
       docs/observability/phase0-langfuse-decision.md).

Run modes:
  # Headless wiring check — no network, no Langfuse, no API key. Proves the graph runs.
  python spikes/langgraph_langfuse_spike.py --dry

  # Real run — emits a trace to Langfuse. Requires:
  #   pip install -r spikes/requirements-spike.txt
  #   OPENAI_API_KEY + LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY (+ LANGFUSE_HOST for self-host)
  python spikes/langgraph_langfuse_spike.py --mode sage --prompt "What is a beholder?"

SDK assumptions (current as of 2026-06): langgraph >=0.2, langchain-openai >=0.2,
langfuse >=3 (LangChain integration at `langfuse.langchain.CallbackHandler`).
"""

from __future__ import annotations

import argparse
import os
import subprocess
from typing import Any, Callable, Literal, TypedDict

# REFUSAL mirrors service/generate.py's grounded-refusal contract (kept local so the
# spike has no import-time dependency on the service package).
REFUSAL = "I don't have enough grounded material to answer that."

# Minimal per-mode system prompts — the real persona prompts live in
# service/generate.py:PERSONA_PROMPTS; the spike only needs to vary by mode.
SYSTEM_BY_MODE: dict[str, str] = {
    "sage": "You are a precise D&D 5e sage. Answer ONLY from the provided context.",
    "gm": "You are a creative D&D 5e game master. Prefer the context; you may embellish.",
}
GROUNDED_TEMPLATE = "Context:\n{context}\n\nQuestion: {question}\n\nGrounded answer:"


class GraphState(TypedDict, total=False):
    """State threaded through the graph — mirrors the fields the real pipeline carries."""
    prompt: str
    mode: str
    chunks: list[dict[str, Any]]
    top1_distance: float
    answerable: bool
    context: str
    answer: str


def _git_sha() -> str:
    """service_version tag = short git SHA (best-effort; 'unknown' off a checkout)."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return os.environ.get("SERVICE_VERSION", "unknown")


# ── Nodes (mirror the real seams) ─────────────────────────────────────────────


def retrieve_node(state: GraphState) -> GraphState:
    """STUB of the real pgvector kNN retrieve. Returns fixed chunks so the spike
    runs with no DB. The real migration node calls ingestion/retrieval.py."""
    chunks = [
        {
            "text": "A beholder is a Large aberration: a floating orb dominated by a "
            "central eye and ten eyestalks, each firing a different magical ray.",
            "distance": 0.21,
            "book": "Monster Manual",
        },
        {
            "text": "Beholders are xenophobic and believe themselves the pinnacle of "
            "creation; no two agree on what a 'perfect' beholder looks like.",
            "distance": 0.34,
            "book": "Monster Manual",
        },
    ]
    return {
        "chunks": chunks,
        "top1_distance": chunks[0]["distance"],
        "answerable": chunks[0]["distance"] <= 0.50,  # KOZ_ANSWERABLE_DISTANCE
    }


def gate_node(state: GraphState) -> GraphState:
    """Grounding gate — pass-through; routing decided by `gate_route`. Mirrors the
    strict koz gate in service/rag.py (sage/spell/rules)."""
    return {}


def gate_route(state: GraphState) -> Literal["generate", "refuse"]:
    answerable = bool(state.get("answerable")) and bool(state.get("chunks"))
    # GM mode is allowed through on any chunks (creative); others need answerable.
    if state.get("mode") == "gm":
        return "generate" if state.get("chunks") else "refuse"
    return "generate" if answerable else "refuse"


def refuse_node(state: GraphState) -> GraphState:
    return {"answer": REFUSAL, "answerable": False}


def make_generate_node(llm_call: Callable[[str, str], str]) -> Callable[[GraphState], GraphState]:
    """Generate node factory — `llm_call(system, user) -> answer` is injected so the
    node is testable with a fake (preserving the real pipeline's injectable seam)."""

    def generate_node(state: GraphState) -> GraphState:
        context = "\n\n".join(c["text"] for c in state.get("chunks", []))
        system = SYSTEM_BY_MODE.get(state.get("mode", "sage"), SYSTEM_BY_MODE["sage"])
        user = GROUNDED_TEMPLATE.format(context=context, question=state["prompt"])
        return {"context": context, "answer": llm_call(system, user)}

    return generate_node


# ── Graph assembly ────────────────────────────────────────────────────────────


def build_graph(llm_call: Callable[[str, str], str]) -> Any:
    """Compile the retrieve -> gate -> (generate|refuse) -> END graph."""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(GraphState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("gate", gate_node)
    g.add_node("generate", make_generate_node(llm_call))
    g.add_node("refuse", refuse_node)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "gate")
    g.add_conditional_edges("gate", gate_route, {"generate": "generate", "refuse": "refuse"})
    g.add_edge("generate", END)
    g.add_edge("refuse", END)
    return g.compile()


# ── LLM call implementations ──────────────────────────────────────────────────


def fake_llm_call(system: str, user: str) -> str:
    """Deterministic stand-in for --dry runs (no network)."""
    return "[dry-run answer] A beholder is a floating aberration with ten eyestalks."


def make_real_llm_call(model: str) -> Callable[[str, str], str]:
    """Real ChatOpenAI call — the wrapper the migration (ziw.2) adopts so Langfuse
    captures tokens/cost natively."""
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model=model, temperature=0.2)

    def _call(system: str, user: str) -> str:
        resp = llm.invoke([("system", system), ("human", user)])
        return str(resp.content)

    return _call


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="LangGraph + Langfuse Phase 0 spike")
    parser.add_argument("--prompt", default="What is a beholder?")
    parser.add_argument("--mode", default="sage", choices=sorted(SYSTEM_BY_MODE))
    parser.add_argument("--model", default=os.environ.get("RAG_DEFAULT_MODEL", "gpt-4o-mini"))
    parser.add_argument("--dry", action="store_true", help="No network/Langfuse; prove graph wiring")
    args = parser.parse_args()

    sha = _git_sha()
    initial: GraphState = {"prompt": args.prompt, "mode": args.mode}

    if args.dry:
        graph = build_graph(fake_llm_call)
        result = graph.invoke(initial)
        print(f"[dry] model={args.model} service_version={sha} mode={args.mode}")
        print(f"[dry] answerable={result.get('answerable')} answer={result.get('answer')!r}")
        print("[dry] graph wiring OK -- retrieve -> gate -> generate/refuse executed.")
        return

    # Real run: attach the Langfuse callback so each node + the LLM call is traced,
    # tagged with model + service_version (git SHA) + mode.
    from langfuse.langchain import CallbackHandler

    handler = CallbackHandler()  # reads LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST from env
    graph = build_graph(make_real_llm_call(args.model))
    config = {
        "callbacks": [handler],
        "metadata": {
            "langfuse_tags": ["phase0-spike", f"mode:{args.mode}"],
            "model": args.model,
            "service_version": sha,
            "mode": args.mode,
        },
    }
    result = graph.invoke(initial, config=config)
    print(f"model={args.model} service_version={sha} mode={args.mode}")
    print(f"answerable={result.get('answerable')}")
    print(f"answer={result.get('answer')}")
    print("Trace sent to Langfuse — open the dashboard to inspect per-node spans.")


if __name__ == "__main__":
    main()

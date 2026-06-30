"""
Env-gated Langfuse tracing for the RAG graph (ziw.2 / CP3).

Tracing is **off by default** — `build_trace_config` returns an empty config unless
`RAG_TRACING` is truthy, so the test suite and CI never emit traces or require live
Langfuse keys. When enabled, it attaches a Langfuse LangChain `CallbackHandler` plus
per-request metadata (`model`, `service_version`, `mode`) to the graph run; the
generate node forwards the same config to `ChatOpenAI` so the LLM call emits a
token/cost span. Credentials are read from the environment (`LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`) by the handler itself.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def tracing_enabled() -> bool:
    """True when RAG_TRACING is set to a truthy value (off by default)."""
    return os.environ.get("RAG_TRACING", "").strip().lower() in _TRUTHY


def service_version() -> str:
    """Version tag for traces: SERVICE_VERSION env if set (e.g. baked into the
    image), else the short git SHA, else 'unknown'."""
    env = os.environ.get("SERVICE_VERSION")
    if env:
        return env
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def trace_metadata(*, model: str, mode: str, version: str | None = None) -> dict[str, Any]:
    """Per-request trace metadata: lets the Langfuse dashboard filter by model +
    service_version + mode (the epic's core comparison axes)."""
    version = version or service_version()
    return {
        "langfuse_tags": ["rag-chat", f"mode:{mode}"],
        "model": model,
        "service_version": version,
        "mode": mode,
    }


def build_trace_config(*, model: str, mode: str, version: str | None = None) -> dict[str, Any]:
    """A LangChain RunnableConfig with Langfuse callbacks + metadata when tracing is
    enabled; otherwise `{}` (the default — no callbacks, so graph.invoke is untraced
    and offline). Degrades to metadata-only if langfuse is unavailable rather than
    breaking a request."""
    if not tracing_enabled():
        return {}
    config: dict[str, Any] = {"metadata": trace_metadata(model=model, mode=mode, version=version)}
    try:
        from langfuse.langchain import CallbackHandler

        config["callbacks"] = [CallbackHandler()]
    except Exception:
        # langfuse missing/misconfigured: keep serving, just without live traces —
        # but log it, so an enabled-but-silent trace gap is diagnosable (not lost).
        logger.warning(
            "RAG_TRACING is on but Langfuse is unavailable; serving without traces.",
            exc_info=True,
        )
    return config

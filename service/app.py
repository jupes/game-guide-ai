"""
FastAPI app for the D&D 5e agent service.

Stateless `POST /chat`: prompt → retrieve+gate → grounded answer + citations.
The `RagService` (vocabulary loaded once) is built at startup and supplied via a
dependency so tests can override it without a DB or LLM.

Run:
    uv run --with fastapi --with uvicorn --with openai --with "psycopg[binary]" \
        uvicorn service.app:app --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

import config

from .models import ChatRequest, ChatResponse
from .rag import RagService

log = logging.getLogger(__name__)

# Upstream error classes, mapped to distinct HTTP statuses so a failed LLM call
# (502) is distinguishable from an unavailable retrieval backend (503) and from
# an actual bug in our code (500). Guarded imports keep the app importable even
# if a dependency is missing in a stripped-down test env; an empty tuple in an
# `except` clause simply never matches and falls through to the 500 handler.
try:
    import openai

    _LLM_ERRORS: tuple[type[BaseException], ...] = (openai.APIError,)
except Exception:  # pragma: no cover - openai always present in service image
    _LLM_ERRORS = ()

try:
    import psycopg

    _DB_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except Exception:  # pragma: no cover - psycopg always present in service image
    _DB_ERRORS = ()

_state: dict[str, RagService] = {}


def build_reranker(enabled: bool | None = None) -> Any | None:
    """The gated cross-encoder reranker for the live service, or None.

    Off unless RAG_RERANK is truthy (see config.py). When enabled but the
    `[rerank]` extra isn't installed, degrade to no reranker with a warning
    instead of failing startup — the same posture as tracing.py's missing
    Langfuse. The model itself still lazy-loads on first reranked query.
    """
    if enabled is None:
        enabled = config.RAG_RERANK
    if not enabled:
        return None
    if find_spec("sentence_transformers") is None:
        log.warning(
            "RAG_RERANK is on but sentence-transformers is not installed "
            "(pip install '.[rerank]'); serving without a reranker."
        )
        return None
    from ingestion.rerank import CrossEncoderReranker

    return CrossEncoderReranker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the service once (loads corpus vocabulary). Guarded so the app can
    # still start for endpoint tests that override the dependency without a DB.
    try:
        _state["rag"] = RagService(reranker=build_reranker())
    except Exception:  # pragma: no cover - depends on live DB
        log.warning(
            "startup: RagService unavailable; /chat will 503 until ready", exc_info=True
        )
    yield
    _state.clear()


app = FastAPI(title="D&D 5e RAG — Agent Service", version="1.0", lifespan=lifespan)


def get_service() -> RagService:
    svc = _state.get("rag")
    if svc is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return svc


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "ready": str("rag" in _state)}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, svc: RagService = Depends(get_service)) -> ChatResponse:
    try:
        return svc.answer(req.prompt, mode=req.mode.value, conversation_id=req.conversation_id)
    except _LLM_ERRORS as exc:
        # LLM provider failed (timeout, rate limit, API error) — upstream, retryable.
        log.warning(
            "LLM upstream error on /chat (mode=%s, conversation_id=%s): %s: %s",
            req.mode.value, req.conversation_id, type(exc).__name__, exc,
        )
        raise HTTPException(status_code=502, detail="LLM upstream error") from exc
    except _DB_ERRORS as exc:
        # Retrieval backend (Postgres/pgvector) unavailable — upstream, retryable.
        log.warning(
            "retrieval backend error on /chat (mode=%s, conversation_id=%s): %s: %s",
            req.mode.value, req.conversation_id, type(exc).__name__, exc,
        )
        raise HTTPException(status_code=503, detail="retrieval backend unavailable") from exc
    except Exception:
        # Anything else is a bug in our code — log the full traceback, return 500.
        log.exception("internal error on /chat (mode=%s)", req.mode.value)
        raise HTTPException(status_code=500, detail="internal error") from None


# Mount the pre-built UI at "/" — after route decorators so API routes always win.
# Only active when `cd ui && bun run build` has been run (ui/dist/ must exist).
_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
if _UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_UI_DIST, html=True), name="ui")

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

import base64
import binascii
import logging
from contextlib import asynccontextmanager
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

import config

from ingestion.retrieval import EmbeddingUnavailableError

from .attachments import UnsupportedAttachmentError, extract_text
from .history import MessageStore, PostgresMessageStore, StoredAttachment
from .models import (
    Attachment,
    AttachmentResponse,
    AttachmentsResponse,
    AttachmentUploadRequest,
    ChatRequest,
    ChatResponse,
    MessagesResponse,
)
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

_state: dict[str, Any] = {}


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
    # Message history store — best-effort: chat answers work without it.
    # ensure_schema() is the migration path for volumes that predate chat.*.
    try:
        store = PostgresMessageStore()
        store.ensure_schema()
        _state["store"] = store
    except Exception:  # pragma: no cover - depends on live DB
        log.warning(
            "startup: message store unavailable; history is disabled", exc_info=True
        )
    yield
    _state.clear()


app = FastAPI(title="D&D 5e RAG — Agent Service", version="1.0", lifespan=lifespan)


def get_service() -> RagService:
    svc = _state.get("rag")
    if svc is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return svc


def get_message_store() -> MessageStore | None:
    # None is a valid state (history disabled) — /chat degrades gracefully;
    # only the history endpoint itself hard-fails without a store.
    return _state.get("store")


def _persist_turn(
    store: MessageStore | None, conversation_id: str | None,
    mode: str, role: str, content: str,
    suggestions: list[dict[str, Any]] | None = None,
) -> None:
    """Best-effort history write: a failure is logged, never raised — a chat
    answer must not fail because persistence did (deliberately outside the
    _DB_ERRORS → 503 taxonomy, which is reserved for retrieval)."""
    if store is None or conversation_id is None:
        return
    try:
        store.append(conversation_id, mode, role, content, suggestions=suggestions)
    except Exception:
        log.warning(
            "history write failed (mode=%s, conversation_id=%s, role=%s)",
            mode, conversation_id, role, exc_info=True,
        )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "ready": str("rag" in _state)}


@app.post("/chat", response_model=ChatResponse)
def chat(
    req: ChatRequest,
    svc: RagService = Depends(get_service),
    store: MessageStore | None = Depends(get_message_store),
) -> ChatResponse:
    try:
        resp = svc.answer(req.prompt, mode=req.mode.value, conversation_id=req.conversation_id)
        _persist_turn(store, req.conversation_id, req.mode.value, "user", req.prompt)
        _persist_turn(
            store, req.conversation_id, req.mode.value, "assistant", resp.answer,
            suggestions=(
                [s.model_dump(mode="json") for s in resp.suggestions]
                if resp.suggestions else None
            ),
        )
        return resp
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
    except EmbeddingUnavailableError as exc:
        # Embedding can't run (missing OPENAI_API_KEY) — service-side
        # unavailability, not a crash (1em.3; previously sys.exit killed the worker).
        log.warning(
            "embedding unavailable on /chat (mode=%s, conversation_id=%s): %s",
            req.mode.value, req.conversation_id, exc,
        )
        raise HTTPException(status_code=503, detail="embedding backend unavailable") from exc
    except Exception:
        # Anything else is a bug in our code — log the full traceback, return 500.
        log.exception("internal error on /chat (mode=%s)", req.mode.value)
        raise HTTPException(status_code=500, detail="internal error") from None


@app.get("/conversations/{conversation_id}/messages", response_model=MessagesResponse)
def conversation_messages(
    conversation_id: str,
    limit: int | None = None,
    store: MessageStore | None = Depends(get_message_store),
) -> MessagesResponse:
    if store is None:
        raise HTTPException(status_code=503, detail="message history unavailable")
    # config.HISTORY_LIMIT read at request time (not import) so env/test
    # overrides of the knob take effect; client may ask for fewer, never more.
    cap = config.HISTORY_LIMIT
    effective = cap if limit is None else max(1, min(limit, cap))
    try:
        messages = store.recent(conversation_id, effective)
    except _DB_ERRORS as exc:
        log.warning(
            "history read failed (conversation_id=%s): %s: %s",
            conversation_id, type(exc).__name__, exc,
        )
        raise HTTPException(status_code=503, detail="message history unavailable") from exc
    return MessagesResponse(conversation_id=conversation_id, messages=messages)


def _to_attachment(sa: StoredAttachment) -> Attachment:
    """Map a stored attachment to UI-facing metadata (extracted text omitted)."""
    return Attachment(
        id=sa.id, filename=sa.filename, content_type=sa.content_type,
        chars=len(sa.extracted_text), created_at=sa.created_at,
    )


@app.post("/conversations/{conversation_id}/attachments", response_model=AttachmentResponse)
def upload_attachment(
    conversation_id: str,
    req: AttachmentUploadRequest,
    store: MessageStore | None = Depends(get_message_store),
) -> AttachmentResponse:
    if store is None:
        raise HTTPException(status_code=503, detail="attachments unavailable")
    try:
        data = base64.b64decode(req.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="attachment data is not valid base64") from exc
    if len(data) > config.ATTACHMENT_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"attachment exceeds the {config.ATTACHMENT_MAX_BYTES}-byte limit",
        )
    try:
        text = extract_text(data, req.filename)
    except UnsupportedAttachmentError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    try:
        stored = store.append_attachment(conversation_id, req.filename, req.content_type, text)
    except _DB_ERRORS as exc:
        log.warning("attachment write failed (conversation_id=%s): %s", conversation_id, exc)
        raise HTTPException(status_code=503, detail="attachment storage unavailable") from exc
    return AttachmentResponse(conversation_id=conversation_id, attachment=_to_attachment(stored))


@app.get("/conversations/{conversation_id}/attachments", response_model=AttachmentsResponse)
def conversation_attachments(
    conversation_id: str,
    store: MessageStore | None = Depends(get_message_store),
) -> AttachmentsResponse:
    if store is None:
        raise HTTPException(status_code=503, detail="attachments unavailable")
    try:
        stored = store.attachments_for(conversation_id)
    except _DB_ERRORS as exc:
        log.warning("attachment read failed (conversation_id=%s): %s", conversation_id, exc)
        raise HTTPException(status_code=503, detail="attachments unavailable") from exc
    return AttachmentsResponse(
        conversation_id=conversation_id,
        attachments=[_to_attachment(a) for a in stored],
    )


# Mount the pre-built UI at "/" — after route decorators so API routes always win.
# Only active when `cd ui && bun run build` has been run (ui/dist/ must exist).
_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
if _UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_UI_DIST, html=True), name="ui")

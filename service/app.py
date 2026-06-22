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

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from .models import ChatRequest, ChatResponse
from .rag import RagService

_state: dict[str, RagService] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the service once (loads corpus vocabulary). Guarded so the app can
    # still start for endpoint tests that override the dependency without a DB.
    try:
        _state["rag"] = RagService()
    except Exception as exc:  # pragma: no cover - depends on live DB
        print(f"startup: RagService unavailable ({exc!r}); /chat will 503 until ready")
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
    except Exception as exc:  # retrieval/generation failure
        raise HTTPException(status_code=503, detail=f"upstream error: {exc}") from exc


# Mount the pre-built UI at "/" — after route decorators so API routes always win.
# Only active when `cd ui && bun run build` has been run (ui/dist/ must exist).
_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
if _UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_UI_DIST, html=True), name="ui")

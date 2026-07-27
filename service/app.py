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
import time
from contextlib import asynccontextmanager
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.staticfiles import StaticFiles

import config

from ingestion.retrieval import EmbeddingUnavailableError

from .attachments import UnsupportedAttachmentError, extract_text
from .auth_store import AuthStore, EmailTaken, PostgresAuthStore
from .hashing import DUMMY_PASSWORD_HASH, hash_password, verify_password
from .history import MessageStore, PostgresMessageStore, StoredAttachment
from .invites import InviteError
from .models import (
    Attachment,
    AttachmentResponse,
    AttachmentsResponse,
    AttachmentUploadRequest,
    AuthUser,
    ChatRequest,
    ChatResponse,
    LoginRequest,
    MessagesResponse,
    SignupRequest,
)
from .session import SessionData, decode_session, encode_session
from .metrics import (
    BooleanMetricPoint,
    CategoricalMetricPoint,
    MetricBatch,
    MetricLabels,
    MetricsSink,
    NoopMetricsSink,
    NumericMetricPoint,
    build_metrics_sink,
    record_safely,
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
    app.state.metrics_sink = build_metrics_sink()
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
    # Auth store — invite-gated accounts (x5bz.2). Same best-effort startup +
    # ensure_schema() migration path as the message store.
    try:
        auth = PostgresAuthStore()
        auth.ensure_schema()
        _state["auth"] = auth
    except Exception:  # pragma: no cover - depends on live DB
        log.warning(
            "startup: auth store unavailable; auth endpoints will 503", exc_info=True
        )
    yield
    _state.clear()
    del app.state.metrics_sink


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


def get_metrics_sink(request: Request) -> MetricsSink:
    return getattr(request.app.state, "metrics_sink", NoopMetricsSink())


# ── Auth (x5bz.2) ─────────────────────────────────────────────────────────────

def get_auth_store() -> AuthStore:
    store = _state.get("auth")
    if store is None:
        raise HTTPException(status_code=503, detail="auth backend unavailable")
    return store


def _session_secret() -> str:
    """Fail CLOSED if the signing secret is unset — never sign or verify with an
    empty key. In Cloud Run it comes from the `session-secret` Secret Manager
    entry; locally from SESSION_SECRET in .env."""
    secret = config.SESSION_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="auth not configured")
    return secret


def require_session(
    request: Request, store: AuthStore = Depends(get_auth_store),
) -> SessionData:
    """Dependency guarding the data endpoints: a valid session cookie or 401.

    The cookie is authentic-but-stale by nature (it is signed once and lives for
    days), so the account is **re-read from the store on every request**: a
    deleted account stops working immediately instead of at cookie expiry, and
    the role used for authorization is the CURRENT one — demoting a DM takes
    effect at once rather than requiring a secret rotation to log everyone out.
    """
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    session = decode_session(token, _session_secret(), config.SESSION_TTL_DAYS * 86400)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    try:
        user = store.get_user_by_id(session.user_id)
    except Exception as exc:  # fail closed — never authorize on a failed lookup
        log.warning("session user lookup failed (user_id=%s)", session.user_id, exc_info=True)
        raise HTTPException(status_code=503, detail="auth backend unavailable") from exc
    if user is None:
        raise HTTPException(status_code=401, detail="account no longer exists")
    return SessionData(user_id=user.id, role=user.role)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=token,
        max_age=config.SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite=config.SESSION_COOKIE_SAMESITE,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=config.SESSION_COOKIE_NAME,
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite=config.SESSION_COOKIE_SAMESITE,
        path="/",
    )


def _chat_error_category(status_code: int) -> str:
    if status_code == 422:
        return "validation"
    if status_code in {502, 503}:
        return "dependency"
    if status_code >= 500:
        return "handler"
    return "unknown"


@app.middleware("http")
async def capture_chat_metrics(request: Request, call_next):
    if request.url.path != "/chat":
        return await call_next(request)

    started_at = time.perf_counter()
    response = await call_next(request)
    sink = get_metrics_sink(request)
    labels = MetricLabels(route_template="/chat")
    record_safely(
        sink,
        NumericMetricPoint(
            name="service.chat.duration_ms",
            kind="numeric",
            unit="ms",
            value=(time.perf_counter() - started_at) * 1000,
            labels=labels,
        ),
    )
    is_error = response.status_code >= 400
    record_safely(
        sink,
        BooleanMetricPoint(
            name="service.chat.error",
            kind="boolean",
            unit="boolean",
            value=is_error,
            labels=labels,
        ),
    )
    if is_error:
        record_safely(
            sink,
            CategoricalMetricPoint(
                name="service.chat.error_category",
                kind="categorical",
                unit="category",
                value=_chat_error_category(response.status_code),
                labels=labels,
            ),
        )
    return response


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


def _fetch_attachment_context(
    store: MessageStore | None, conversation_id: str | None,
) -> tuple[str | None, str | None]:
    """Best-effort: join a conversation's stored attachment texts for the RAG
    prompt. Mirrors `_persist_turn` — a fetch failure must never fail the chat
    answer (deliberately swallowed, not the _DB_ERRORS → 503 taxonomy)."""
    if store is None or conversation_id is None:
        return None, None
    try:
        attachments = store.attachments_for(conversation_id)
    except Exception:
        log.warning(
            "attachment fetch failed (conversation_id=%s)", conversation_id, exc_info=True,
        )
        return None, None
    if not attachments:
        return None, None
    context = "\n\n".join(a.extracted_text for a in attachments)
    label = ", ".join(a.filename for a in attachments)
    return context, label


def _authorize_conversation(
    store: MessageStore | None, conversation_id: str | None, user_id: int, *, claim: bool,
) -> None:
    """Authorize this user for `conversation_id`, or raise (x5bz.2 D).

    **Fails CLOSED.** A lookup error raises 503 rather than continuing: reads and
    ownership checks run on separate connections, so a transient failure (or a
    role that can't see `chat.conversations`) must never be followed by a
    successful cross-user read.

    `claim=True` (writes) uses the store's **atomic** claim, which returns the
    winning owner — that single call is both the check and the claim, closing the
    TOCTOU window where two users racing a fresh id could both pass a separate
    pre-check. Anyone who is not the winner is rejected.
    """
    if store is None or conversation_id is None:
        return
    try:
        owner = (
            store.claim_conversation(conversation_id, user_id)
            if claim
            else store.owner_of(conversation_id)
        )
    except Exception as exc:
        log.warning(
            "conversation authorization failed (conversation_id=%s)",
            conversation_id, exc_info=True,
        )
        raise HTTPException(
            status_code=503, detail="authorization backend unavailable"
        ) from exc
    if owner is not None and owner != user_id:
        raise HTTPException(status_code=403, detail="not your conversation")


@app.get("/healthz")
def healthz() -> dict[str, str | bool]:
    return {"status": "ok", "ready": "rag" in _state}


@app.post("/chat", response_model=ChatResponse)
def chat(
    req: ChatRequest,
    svc: RagService = Depends(get_service),
    store: MessageStore | None = Depends(get_message_store),
    metrics: MetricsSink = Depends(get_metrics_sink),
    session: SessionData = Depends(require_session),
) -> ChatResponse:
    # Server-side role gate: the GM channel is DM-only, enforced from the session
    # role (not the UI toggle). Raised before the try so it isn't masked as a 500.
    if req.mode.value == "gm" and session.role != "dm":
        raise HTTPException(status_code=403, detail="the GM channel requires the DM role")
    # Ownership: one atomic claim-or-reject (403 if it's someone else's).
    # Before the try for the same reason (403, not 500).
    _authorize_conversation(store, req.conversation_id, session.user_id, claim=True)
    try:
        attachment_context, attachment_label = _fetch_attachment_context(
            store, req.conversation_id,
        )
        resp = svc.answer(
            req.prompt, mode=req.mode.value, conversation_id=req.conversation_id,
            attachment_context=attachment_context, attachment_label=attachment_label,
        )
        record_safely(
            metrics,
            BooleanMetricPoint(
                name="service.chat.gate.answerable",
                kind="boolean",
                unit="boolean",
                value=resp.answerable,
                labels=MetricLabels(mode=req.mode.value, route_template="/chat"),
            ),
        )
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


@app.post("/metrics/ui", status_code=status.HTTP_202_ACCEPTED)
def record_ui_metrics(
    batch: MetricBatch,
    sink: MetricsSink = Depends(get_metrics_sink),
) -> dict[str, int]:
    if any(not point.name.startswith("ui.") for point in batch.points):
        raise HTTPException(status_code=422, detail="only UI metrics are accepted")
    for point in batch.points:
        record_safely(sink, point)
    return {"accepted": len(batch.points)}


@app.get("/conversations/{conversation_id}/messages", response_model=MessagesResponse)
def conversation_messages(
    conversation_id: str,
    limit: int | None = None,
    store: MessageStore | None = Depends(get_message_store),
    session: SessionData = Depends(require_session),
) -> MessagesResponse:
    if store is None:
        raise HTTPException(status_code=503, detail="message history unavailable")
    _authorize_conversation(store, conversation_id, session.user_id, claim=False)
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
    session: SessionData = Depends(require_session),
) -> AttachmentResponse:
    if store is None:
        raise HTTPException(status_code=503, detail="attachments unavailable")
    _authorize_conversation(store, conversation_id, session.user_id, claim=True)
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
    session: SessionData = Depends(require_session),
) -> AttachmentsResponse:
    if store is None:
        raise HTTPException(status_code=503, detail="attachments unavailable")
    _authorize_conversation(store, conversation_id, session.user_id, claim=False)
    try:
        stored = store.attachments_for(conversation_id)
    except _DB_ERRORS as exc:
        log.warning("attachment read failed (conversation_id=%s): %s", conversation_id, exc)
        raise HTTPException(status_code=503, detail="attachments unavailable") from exc
    return AttachmentsResponse(
        conversation_id=conversation_id,
        attachments=[_to_attachment(a) for a in stored],
    )


@app.post("/auth/signup", response_model=AuthUser)
def signup(
    req: SignupRequest,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
) -> AuthUser:
    """Create an account by redeeming a one-time invite, then start a session.
    The invite carries the role; the token is consumed atomically."""
    secret = _session_secret()
    # Cheap pre-check BEFORE the (deliberately expensive) argon2 hash, so an
    # unauthenticated caller without a usable invite can't burn CPU/memory at
    # will. The atomic re-check inside redeem_invite is still what actually
    # guarantees single use — this only short-circuits the obvious rejects.
    try:
        invite = store.get_invite(req.invite)
    except Exception as exc:
        log.warning("invite lookup failed", exc_info=True)
        raise HTTPException(status_code=503, detail="auth backend unavailable") from exc
    if invite is None:
        raise HTTPException(status_code=400, detail="Unknown invite link.")
    try:
        invite.check_redeemable()
    except InviteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        user = store.redeem_invite(req.invite, req.email, hash_password(req.password))
    except EmailTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InviteError as exc:
        # Unknown / used / expired / revoked — the message says which.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_session_cookie(
        response, encode_session(SessionData(user_id=user.id, role=user.role), secret),
    )
    return AuthUser(email=user.email, role=user.role)


@app.post("/auth/login", response_model=AuthUser)
def login(
    req: LoginRequest,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
) -> AuthUser:
    secret = _session_secret()
    user = store.get_user_by_email(req.email)
    stored_hash = store.password_hash_for(req.email)
    # Always run one argon2 verification, even for an unknown email — otherwise
    # the response time itself reveals whether an account exists, and the generic
    # message below buys nothing. DUMMY_PASSWORD_HASH never matches.
    ok = verify_password(stored_hash or DUMMY_PASSWORD_HASH, req.password)
    if user is None or stored_hash is None or not ok:
        # One generic message — never reveal whether the email is registered.
        raise HTTPException(status_code=401, detail="invalid email or password")
    _set_session_cookie(
        response, encode_session(SessionData(user_id=user.id, role=user.role), secret),
    )
    return AuthUser(email=user.email, role=user.role)


@app.post("/auth/logout")
def logout(response: Response) -> dict[str, bool]:
    _clear_session_cookie(response)
    return {"ok": True}


@app.get("/auth/me", response_model=AuthUser)
def me(
    session: SessionData = Depends(require_session),
    store: AuthStore = Depends(get_auth_store),
) -> AuthUser:
    user = store.get_user_by_id(session.user_id)
    if user is None:
        # Session points at a since-deleted account — treat as unauthenticated.
        raise HTTPException(status_code=401, detail="account not found")
    return AuthUser(email=user.email, role=user.role)


# Mount the pre-built UI at "/" — after route decorators so API routes always win.
# Only active when `cd ui && bun run build` has been run (ui/dist/ must exist).
_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
if _UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_UI_DIST, html=True), name="ui")

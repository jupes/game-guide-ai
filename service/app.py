"""
FastAPI app for the D&D 5e agent service.

Stateless `POST /chat`: prompt → retrieve+gate → grounded answer + citations.
The `RagService` (vocabulary loaded once) is built at startup and supplied via a
dependency so tests can override it without a DB or LLM.

Run:
    uv run --with fastapi --with uvicorn --with openai --with "psycopg[binary,pool]" \
        uvicorn service.app:app --port 8000
"""

from __future__ import annotations

import base64
import binascii
import email.message
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from importlib.util import find_spec
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

import config
from ingestion.retrieval import EmbeddingUnavailableError

from . import (
    asset_jobs,
    assets_api,
    body_limit,
    campaigns_api,
    conversations_api,
    document_lifecycle_api,
    documents_api,
    gcp_logging,
    groups_api,
    job_driver,
    media_objects,
    reconciliation,
    seats_api,
    table_api,
    table_session_api,
    timeline_api,
    tool_invocations_api,
    usage_capture,
)
from .attachments import UnsupportedAttachmentError, extract_text
from .auth_store import AuthStore, EmailTaken, PostgresAuthStore, User
from .db import Database, PoolSettings
from .evidence import RetrievalStageError
from .hashing import (
    DUMMY_PASSWORD_HASH,
    HashingCapacityError,
    hash_password,
    verify_password,
)
from .history import MessageStore, PostgresMessageStore, StoredAttachment
from .invites import InviteError
from .jobs import JobRunner, PostgresJobQueue
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
from .migrations import MigrationError, Mode, migrate
from .model_catalog import (
    AUTO_PUBLIC_ENTRY,
    CATALOG,
    CATALOG_REVISION,
    DEFAULT_ALIAS,
    PRE_D9_CATALOG_REVISION,
    PUBLIC_MODELS,
    ModelProfile,
    enabled_profiles,
    get_profile,
    get_profile_by_public_id,
    public_model_entry,
    public_model_id,
)
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
    RoutingInfo,
    SignupRequest,
    SuggestionsRoutingInfo,
)
from .provider_deadline import begin_turn, end_turn
from .rag import RagService
from .ratelimit import (
    RateLimited,
    check_auth_attempt,
    check_chat_request,
    client_source,
)
from .security_headers import (
    CONTENT_SECURITY_POLICY,
    CROSS_ORIGIN_OPENER_POLICY,
    PERMISSIONS_POLICY,
    REFERRER_POLICY,
    X_CONTENT_TYPE_OPTIONS,
)
from .session import SessionData, decode_session, encode_session
from .spa_fallback import install_spa
from .table_sessions import TableSessions
from .timeline_store import PostgresTimelineStore, TimelineStore, new_entry_id
from .workbench_api import gm_session, install_workbench, reauth_failed
from .workbench_contracts import CHAT_TEXT_MAX_CHARS, CONTRACT_VERSION, ErrorCode, check_plain_text

log = logging.getLogger(__name__)

# Upstream error classes, mapped to distinct HTTP statuses so a failed LLM call
# (502) is distinguishable from an unavailable retrieval backend (503) and from
# an actual bug in our code (500). Guarded imports keep the app importable even
# if a dependency is missing in a stripped-down test env; an empty tuple in an
# `except` clause simply never matches and falls through to the 500 handler.
try:
    import openai

    # OpenAIError, not just APIError: ContentFilterFinishReasonError (raised
    # by the LangChain wrapper on a content-filtered finish_reason) inherits
    # OpenAIError directly, not APIError -- narrower scope would let it fall
    # through to the generic 500 handler instead of the D4 mapping below.
    _LLM_ERRORS: tuple[type[BaseException], ...] = (openai.OpenAIError,)
except Exception:  # pragma: no cover - openai always present in service image
    _LLM_ERRORS = ()

try:
    import psycopg

    _DB_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except Exception:  # pragma: no cover - psycopg always present in service image
    _DB_ERRORS = ()


# Normalized LLM error category -> (HTTP status, retryable). See the plan's
# "Error and status contract" (D4, agent-forge-harness-b8o.2 Checkpoint 2).
# conversation_strategy_mismatch (409) and budget/daily-cap (429) are handled
# directly at their own raise sites (claim_conversation_strategy's caller,
# _enforce_daily_cap/_throttle_chat) -- not through this table, since neither
# originates as an openai SDK exception.
ERROR_STATUS: dict[str, tuple[int, bool]] = {
    "rate_limit": (429, True),
    "content_filter": (422, False),
    "invalid_request": (422, False),
    "authentication": (502, False),
    "quota": (502, False),
    "timeout": (502, True),
    "upstream_unavailable": (502, True),
    "unknown": (502, True),
}

# Human-readable message per category, exposed alongside the machine-readable
# category/retryable pair in the 4xx/5xx response body — the UI switches on
# `category`, not this string (which may still be shown to a tester).
_ERROR_DETAIL: dict[str, str] = {
    "rate_limit": "the model provider is rate-limiting requests; retry shortly",
    "content_filter": "the request was refused by the model provider's content policy",
    "invalid_request": "the request was rejected as invalid",
    "authentication": "the model provider rejected our credentials",
    "quota": "the model provider account has no remaining quota",
    "timeout": "the model provider timed out",
    "upstream_unavailable": "the model provider is temporarily unavailable",
    "unknown": "an unexpected upstream error occurred",
}


def _retrieval_stage_error(exc: RetrievalStageError) -> HTTPException:
    """The /chat answer to a typed retrieval-stage fault (agent-forge-harness-xiu.2.3).

    A request the provider refused as invalid (an over-long prompt the
    embeddings API rejects, say) keeps the D4 `invalid_request` 422: retrying
    cannot help, so a 503 "try again" would be false. Any other embed fault is
    the embedding backend's 503, and any other stage the retrieval backend's.
    Never the cause's `Retry-After`: the 429 belonged to a provider call the
    client did not make."""
    if exc.outcome == "rejected":
        status_code, retryable = ERROR_STATUS["invalid_request"]
        return HTTPException(status_code=status_code, detail={
            "category": "invalid_request", "retryable": retryable, "message": _ERROR_DETAIL["invalid_request"],
        })
    if exc.stage == "embed":
        return HTTPException(status_code=503, detail="embedding backend unavailable")
    return HTTPException(status_code=503, detail="retrieval backend unavailable")


def normalize_llm_error(exc: BaseException) -> str:
    """Map an openai SDK exception (or anything else that reaches the /chat
    handler's LLM-error branch) to one of ERROR_STATUS's bounded categories.
    Order matters: APITimeoutError is a subclass of APIConnectionError, so it
    must be checked first or every timeout would classify as the broader
    upstream_unavailable category instead of the more specific, retryable
    timeout one (same status/retryable pair today, but a real distinction —
    see the plan's rationale for keeping them separate categories)."""
    if isinstance(exc, openai.RateLimitError):
        return "rate_limit"
    if isinstance(exc, openai.ContentFilterFinishReasonError):
        return "content_filter"
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return "invalid_request"
    if isinstance(exc, openai.AuthenticationError):
        return "authentication"
    if isinstance(exc, openai.PermissionDeniedError):
        return "quota"
    if isinstance(exc, openai.APITimeoutError):
        return "timeout"
    if isinstance(exc, (openai.APIConnectionError, openai.InternalServerError)):
        return "upstream_unavailable"
    return "unknown"

_state: dict[str, Any] = {}

# The cost ledger's one way in (yje.5.1.2): a turn's rows go to whatever writer
# this registry holds when the turn ends (`_build_stores` puts it there, the
# lifespan teardown clears it), or nowhere. Registered once, here.
usage_capture.set_ledger_provider(lambda: _state.get("ledger"))


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


#: One refused connection at a cold start must not decide the instance's whole
#: life: a Cloud SQL blip or a full server (53300) is over in seconds. Three tries
#: cost at most ~35 s, well inside the startup window.
STARTUP_CONNECT_ATTEMPTS = 3
STARTUP_CONNECT_PAUSE_S = 2.0
_pause = time.sleep


def prepare_database() -> Database:
    """The schema first (1kg.1.5): ordered migrations, before anything is served.

    Two kinds of failure, deliberately treated differently. A `MigrationError`
    is a verdict — drift, a migration that failed, a broken package — and so is
    a bad setting. Retrying cannot change either, so both may stop startup:
    on Cloud Run that fails the new revision and keeps traffic on the old one.
    An unreachable database is an outage: after a few tries it degrades exactly
    as it always has — history off, auth endpoints 503, `/healthz` answering —
    and `_state["migrations"]` says `unavailable`.

    The `Database` is returned either way. It connects to nothing until it is
    used, and retrieval must stay inside the connection budget even on an
    instance that started during an outage.
    """
    db = Database(settings=PoolSettings.from_env())
    mode = Mode(os.environ.get("MIGRATIONS_MODE") or Mode.APPLY.value)
    for attempt in range(1, STARTUP_CONNECT_ATTEMPTS + 1):
        try:
            _state["migrations"] = migrate(mode=mode).state
            return db
        except MigrationError:
            raise
        except _AUTH_BACKEND_ERRORS as exc:  # psycopg's hierarchy and socket errors; defined below
            # The class and SQLSTATE only: the driver's text names hosts and users,
            # and libpq quotes whatever it could not parse (SEC-21).
            sqlstate = getattr(exc, "sqlstate", None)
            log.warning(
                "startup: database unreachable, attempt %d of %d (%s%s)",
                attempt,
                STARTUP_CONNECT_ATTEMPTS,
                type(exc).__name__,
                f", SQLSTATE {sqlstate}" if sqlstate else "",
            )
        if attempt < STARTUP_CONNECT_ATTEMPTS:
            _pause(STARTUP_CONNECT_PAUSE_S)
    _state["migrations"] = "unavailable"
    log.warning("startup: database unavailable; history is disabled and auth endpoints will 503")
    return db


#: A degraded instance looks for its database again this often and no more: the
#: look is a connection attempt on a request's own thread, so it is rationed,
#: short, and made by one request at a time.
RECOVERY_INTERVAL_S = 15.0
RECOVERY_CONNECT_TIMEOUT_S = 3
_recovery_lock = threading.Lock()
_clock = time.monotonic


def _build_stores(db: Database) -> None:
    """Message history (best-effort: chat answers work without it) and the auth
    store — invite-gated accounts (x5bz.2). Both go through the one bounded gate.
    Only ever called once the schema has been checked."""
    _state["store"] = PostgresMessageStore(db=db)
    _state["auth"] = PostgresAuthStore(db=db)
    _state["timeline"] = PostgresTimelineStore()
    # The provider-attempt cost ledger (yje.5.1.2). A store like the others, so
    # it lives and dies with this registry; `usage_capture` finds it through the
    # provider registered below `_state`, because `chat()` does not change.
    from .usage_ledger import LedgerWriter, PostgresUsageLedgerStore

    _state["ledger"] = LedgerWriter(PostgresUsageLedgerStore(), db)
    # The job outbox's drivers (1kg.2.7), and the kinds this build registers,
    # each retried until it succeeds (registering one turns the request hook on
    # for every signed-in request): `campaign.reconcile`, which every revocation
    # leaves behind (1kg.2.2, RQ-5), its slot step the reveal fill (1kg.7.1:
    # `reveals.make_reconcile_slots`, which clears a dead session's slots and a
    # removed seat's copy); `table_session.expire`
    # (1kg.2.3), the delayed job a Start enqueues at `expires_at`; and
    # `timeline.session_divider` (1kg.3.5), which a Start and an ending leave
    # behind to write the session's dividers. End, Rotate and expiry enqueue their
    # reconciliation through 1kg.2.2's helper, and the lifecycle its divider jobs
    # through 1kg.3.5's enqueuer, never naming either kind here.
    from .audit_log import PostgresAuditLog
    from .campaign_store import PostgresCampaignStore
    from .reveal_store import PostgresRevealStore
    from .reveals import make_reconcile_slots, slot_clear_for
    from .session_divider_store import PostgresSessionDividerStore
    from .session_dividers import DIVIDER_KIND, SessionDividers, enqueuer
    from .table_session_store import PostgresTableSessionStore
    from .table_sessions import EXPIRE_KIND, TableSessions

    queue = PostgresJobQueue(db)
    runner = JobRunner(queue, single_flight=job_driver.JOB_LOCK)
    # Every session store here clears what its narrowing invalidates (RQ-7).
    reveal_rows = PostgresRevealStore()
    sessions = PostgresTableSessionStore(slot_clear=slot_clear_for(reveal_rows))
    runner.register(
        reconciliation.RECONCILE_KIND,
        reconciliation.handler(
            db, slots=make_reconcile_slots(sessions, reveal_rows, clock=lambda: datetime.now(UTC))
        ),
    )
    _state["job_queue"] = queue
    table_sessions = TableSessions(
        db,
        campaigns=PostgresCampaignStore(),
        sessions=sessions,
        audit=PostgresAuditLog(),
        jobs=queue,
        reconcile=lambda unit, campaign_id: reconciliation.enqueue_reconciliation(unit, queue, campaign_id),
        dividers=enqueuer(queue),
    )
    _state["table_sessions"] = table_sessions
    runner.register(EXPIRE_KIND, table_sessions.expire_handler())
    runner.register(
        DIVIDER_KIND, SessionDividers(db, sessions=sessions, store=PostgresSessionDividerStore()).handler()
    )
    # Media (1kg.8.1.2): an object store only when the settings name one, and
    # then its three job kinds whatever the capability switch says — a
    # deployment switched off still owes the deletions it enqueued (MS-3). With
    # none named nothing is built or registered: no bucket, no client, no cost.
    objects = media_objects.build_object_store(_state.get("media_settings", media_objects.MediaSettings()))
    if objects is not None:
        from .asset_store import PostgresAssetStore

        asset_jobs.register_jobs(runner, db=db, queue=queue, objects=objects)
        _state["media"] = assets_api.MediaRuntime(objects, PostgresAssetStore(queue))
    _state["jobs"] = job_driver.JobDriver(runner, healthy=_schema_understood)


def _schema_understood() -> bool:
    """A degraded instance runs no job: `unavailable` never checked the schema,
    `failed` refused it. `ahead` is an older build mid-rollout, which runs the
    kinds it knows."""
    return _state.get("migrations") in ("current", "ahead")


def _build_rag(db: Database) -> None:
    # Build the service once (loads corpus vocabulary). Guarded so the app can
    # still start for endpoint tests that override the dependency without a DB.
    try:
        _state["rag"] = RagService(reranker=build_reranker(), connect=db.connection)
    except Exception:  # pragma: no cover - depends on live DB
        log.warning(
            "startup: RagService unavailable; /chat will 503 until ready", exc_info=True
        )


def recover_database() -> None:
    """An instance that started while the database was away gets it back without
    a restart (1kg.9.8).

    Until now nothing ever looked again: every login answered 503 until Cloud Run
    recycled the instance, which it does not do while the instance keeps receiving
    traffic. The dependencies below call this when they find nothing to hand out.
    A healthy instance pays one dictionary lookup; a degraded one makes at most one
    short attempt every RECOVERY_INTERVAL_S, by one request at a time — the others
    answer 503 at once, as before, instead of queueing behind it.

    The schema is checked (or applied, per MIGRATIONS_MODE) before any store
    exists, exactly as at startup. A verdict ends the looking: it is logged as
    an error, `/healthz` says `failed`, and the instance stays as it was — it
    cannot be stopped from here the way a starting one can, but it must not
    serve a schema it does not understand, and it must not loop."""
    if _state.get("migrations") != "unavailable":
        return
    db = _state.get("db")
    if db is None:
        return
    if _clock() < _state.get("recover_after", 0.0) or not _recovery_lock.acquire(blocking=False):
        return
    try:
        _state["recover_after"] = _clock() + RECOVERY_INTERVAL_S
        mode = Mode(os.environ.get("MIGRATIONS_MODE") or Mode.APPLY.value)
        try:
            report = migrate(mode=mode, connect_timeout_s=RECOVERY_CONNECT_TIMEOUT_S)
        except MigrationError as exc:
            _state["migrations"] = "failed"
            log.error("recovery: the database is back but its schema is refused; not retrying (%s)", exc)
            return
        except _AUTH_BACKEND_ERRORS as exc:
            log.warning("recovery: database still unreachable (%s)", type(exc).__name__)
            return
        _build_stores(db)
        if "rag" not in _state:
            _build_rag(db)
        _state["migrations"] = report.state  # last: this is what every reader keys off
        log.info("recovery: database reachable again; stores built")
    finally:
        _recovery_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A media setting that cannot be used stops startup, like a migration
    # verdict (1kg.8.1.2): retrying cannot change it. Off by default (Q-5).
    _state["media_settings"] = media_objects.startup_settings()
    app.state.metrics_sink = build_metrics_sink()
    db = prepare_database()
    _state["db"] = db
    _build_rag(db)
    # With no database at startup no store exists yet — the schema was never
    # checked, so nothing may write to it. `recover_database` looks again later.
    if _state["migrations"] != "unavailable":
        _build_stores(db)
    yield
    _state.clear()
    await db.aclose()
    del app.state.metrics_sink


# /docs, /redoc and /openapi.json exist only for a local run that asks for them
# (agent-forge-harness-ust7, release review S2). Tests read `app.openapi()`,
# which needs none of the three.
app = FastAPI(
    title="D&D 5e RAG — Agent Service",
    version="1.0",
    lifespan=lifespan,
    docs_url="/docs" if config.API_DOCS_ENABLED else None,
    redoc_url="/redoc" if config.API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if config.API_DOCS_ENABLED else None,
)
install_workbench(app)


def get_service() -> RagService:
    if "rag" not in _state:
        recover_database()
    svc = _state.get("rag")
    if svc is None:
        raise HTTPException(status_code=503, detail="service not ready")
    return svc


def get_message_store() -> MessageStore | None:
    # None is a valid state (history disabled) — /chat degrades gracefully;
    # only the history endpoint itself hard-fails without a store.
    if "store" not in _state:
        recover_database()
    return _state.get("store")


def get_timeline_store() -> TimelineStore | None:
    # Same posture as `get_message_store`: None is a valid state. Without one
    # the timeline route answers 503 and no other path is affected.
    if "timeline" not in _state:
        recover_database()
    return _state.get("timeline")


def get_timeline_database() -> Database | None:
    # The database only when a store exists, so a degraded instance whose schema
    # was never checked cannot be read through.
    if "timeline" not in _state:
        recover_database()
    return _state.get("db") if "timeline" in _state else None


def get_metrics_sink(request: Request) -> MetricsSink:
    return getattr(request.app.state, "metrics_sink", NoopMetricsSink())


# ── Auth (x5bz.2) ─────────────────────────────────────────────────────────────

def get_auth_store() -> AuthStore:
    if "auth" not in _state:
        recover_database()
    store = _state.get("auth")
    if store is None:
        raise HTTPException(status_code=503, detail="auth backend unavailable")
    return store


#: What "the auth backend is unavailable" actually looks like: psycopg's error
#: hierarchy (connection lost, Cloud SQL failover, connections exhausted, a
#: statement timeout) plus socket-level failures reaching it at all.
#:
#: Deliberately NOT `Exception`. A bare catch would relabel a TypeError or an
#: IndexError inside a store implementation as a retryable 503, contradicting
#: the documented taxonomy (`502` upstream · `503` unavailable · `500` bug) and
#: hiding the classification operators alert on — while the client dutifully
#: retried a request that can never succeed. Bugs stay 500s.
_AUTH_BACKEND_ERRORS: tuple[type[BaseException], ...] = (*_DB_ERRORS, OSError)


def _auth_lookup[T](what: str, call: Callable[[], T]) -> T:
    """Run an auth-store call, converting a backend *outage* into 503.

    `auth backend unavailable → 503` is the contract the rest of the auth path
    already speaks: `get_auth_store` 503s when the store never came up at all,
    and startup logs that intent explicitly. But a store that comes up and *then*
    breaks raised straight out of the endpoint as a 500. That is the wrong thing
    to tell a client: 500 means "this request is broken, retrying won't help", so
    the UI surfaced a transient outage as a permanent error, and an operator
    reading status codes saw an application bug rather than a sick dependency.

    Only the errors in `_AUTH_BACKEND_ERRORS` are translated. Domain outcomes
    (a taken email, a spent invite) and genuine bugs both pass through
    untouched, to be answered by the caller and by the 500 handler respectively.
    """
    try:
        return call()
    except _AUTH_BACKEND_ERRORS as exc:
        log.warning("auth store unavailable (%s)", what, exc_info=True)
        raise HTTPException(status_code=503, detail="auth backend unavailable") from exc


def _routing_store[T](conversation_id: str, call: Callable[[], T]) -> T:
    """Run a conversation-routing (strategy binding) store call, failing CLOSED
    on a backend outage with the ownership lookup's handled 503 (xiu.2.3, N-6).

    The binding lives on the same `chat.conversations` row as ownership. An
    outage here used to escape `chat()` before its `try:` as Starlette's raw
    500, with no JSON body, no security headers and no chat metrics. Only
    `_AUTH_BACKEND_ERRORS` are translated: the block's own 409 and 422 pass
    through, and a bug stays a 500. The log line names the class, never the
    message, and says which store it was, apart from the ownership lookup's."""
    try:
        return call()
    except _AUTH_BACKEND_ERRORS as exc:
        log.warning(
            "conversation routing store unavailable (conversation_id=%s, error=%s)",
            conversation_id, type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="authorization backend unavailable") from exc


# Any secret shipped in an example/template is public by definition — copying it
# unchanged would let anyone forge a session (including a DM one), so these are
# rejected as if unset. Compared case-insensitively.
_PLACEHOLDER_SESSION_SECRETS = frozenset({
    "replace-me-with-a-random-string",
    "replace-me",
    "changeme",
    "change-me",
    "secret",
    "your-secret-here",
})

# Short enough to be brute-forced offline against a signed cookie. `openssl rand
# -base64 48` gives 64 chars, so a real secret clears this comfortably.
MIN_SESSION_SECRET_LENGTH = 32


def _session_secret() -> str:
    """Fail CLOSED unless the signing secret is real: never sign or verify with
    an empty, placeholder, or trivially short key. In Cloud Run it comes from the
    `session-secret` Secret Manager entry; locally from SESSION_SECRET in .env."""
    secret = config.SESSION_SECRET
    # Normalize FIRST, then judge. Checking emptiness on the raw value while
    # measuring its length let a whitespace-only secret through: 32 spaces is
    # truthy and 32 chars long, but trivially guessable — enough to forge any
    # session, including a DM one.
    normalized = secret.strip() if secret else ""
    if not normalized:
        log.error(
            "SESSION_SECRET is unset or whitespace-only — auth is disabled "
            "(see docs/deploy-gcp.md)"
        )
        raise HTTPException(status_code=503, detail="auth not configured")
    if normalized.lower() in _PLACEHOLDER_SESSION_SECRETS:
        log.error(
            "SESSION_SECRET is a known placeholder value — refusing to sign sessions "
            "with a publicly known key. Generate one: openssl rand -base64 48"
        )
        raise HTTPException(status_code=503, detail="auth not configured")
    if len(normalized) < MIN_SESSION_SECRET_LENGTH:
        log.error(
            "SESSION_SECRET is too short (%d meaningful chars; need >= %d) — refusing "
            "to sign sessions with a weak key. Generate one: openssl rand -base64 48",
            len(normalized), MIN_SESSION_SECRET_LENGTH,
        )
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
    # Fails CLOSED — never authorize on a failed lookup (`_auth_lookup` 503s).
    user = _auth_lookup(f"session user lookup (user_id={session.user_id})",
                        lambda: store.get_user_by_id(session.user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="account no longer exists")
    # Stash the account this request has already re-read. /auth/me answers from
    # it instead of repeating the identical query — a second round trip that was
    # also a second chance to fail, and did so as an unguarded 500.
    request.state.auth_user = user
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


#: Set on every 429 the auth limiter raises, and on no other response. It exists
#: so an external check can tell an application throttle from a Cloud Run
#: platform 429 (no instance available), which is indistinguishable by status.
AUTH_THROTTLE_HEADER = "X-Auth-Throttled"


def _throttle_auth(request: Request, account: str) -> None:
    """Apply the auth attempt budget, or 429. Must run BEFORE any argon2 work —
    the whole point is to cap how much hashing an anonymous caller can trigger."""
    try:
        check_auth_attempt(request, account)
    except RateLimited as exc:
        # The DERIVED source key is logged, not just the fact of throttling: it
        # is the only way to confirm from outside that AUTH_TRUSTED_PROXY_HOPS
        # matches the real topology — it must equal the address Google observed
        # (docs/deploy-gcp.md §9). That comparison needs Cloud Run's *request*
        # log, which is a separate entry, so on Cloud Run this goes out as a
        # structured record carrying the trace that joins the two. No new
        # exposure: Cloud Run already logs the peer address per request.
        source = client_source(request)
        if not gcp_logging.emit(
            "WARNING", "auth attempt throttled", request,
            source=source, retry_after=exc.retry_after,
        ):
            log.warning(
                "auth attempt throttled (source=%s, retry_after=%ss)",
                source, exc.retry_after,
            )
        raise HTTPException(
            status_code=429,
            detail="Too many attempts — please wait and try again.",
            # AUTH_THROTTLE_HEADER marks this 429 as OURS. Cloud Run returns 429
            # of its own when no instance is available, and a status code alone
            # cannot tell the two apart — so a verifier that accepted any 429 as
            # "the limiter fired" could certify a broken proxy configuration on
            # the strength of a transient platform response. Only this header
            # means the application's budget was actually enforced.
            # scripts/verify_auth_throttle.py requires it before reporting PASS.
            headers={
                "Retry-After": str(exc.retry_after),
                AUTH_THROTTLE_HEADER: "1",
            },
        ) from exc


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


# The job hook (1kg.2.7, RT-15): declared between the two middleware functions,
# so it wraps `capture_chat_metrics` (job time never enters the chat duration)
# and `set_security_headers` stays the outermost, as its docstring requires.
app.add_middleware(job_driver.JobHookMiddleware, driver=lambda: _state.get("jobs"))

# The body ceiling (agent-forge-harness-ust7, release review S1): declared after
# the job hook, so it wraps every route and every other middleware except
# `set_security_headers`, which stays the outermost and puts its headers on the
# 413 too. The lambda defers the name, which is defined further down.
app.add_middleware(body_limit.BodyLimitMiddleware, media_enabled=lambda: _media_enabled())


@app.middleware("http")
async def set_security_headers(request: Request, call_next):
    """Send the security headers this app owns on every response it produces
    (va8, and agent-forge-harness-y58 for the four added after it).

    A separate middleware rather than two lines inside `capture_chat_metrics`:
    that one returns early for every path that is not `/chat`, so folding the
    header into it would leave the SPA document, `/healthz`, `/auth/*` and every
    404 with no policy at all — and its metric contract is pinned by
    `service/tests/test_metrics.py`.

    Declared last, so it is the OUTERMOST user middleware (Starlette inserts
    each one at position 0) and `setdefault` therefore gets the last word. That
    ordering is not what puts the header on the production SPA, though: ANY user
    middleware wraps the router, and the router is what holds the `StaticFiles`
    mount at the bottom of this file.

    `setdefault`, not assignment, for every header here: a route may answer
    with a stricter policy of its own — SEC-19 requires `default-src 'none';
    sandbox` on asset responses — and must not have to unpick this middleware
    to keep it.

    Known and accepted: a 500 raised by an UNHANDLED exception is produced by
    Starlette's `ServerErrorMiddleware`, which sits outside all user middleware,
    so it carries no policy. Handled responses — including `HTTPException`, 401,
    404 and 422 — do.
    """
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("X-Content-Type-Options", X_CONTENT_TYPE_OPTIONS)
    response.headers.setdefault("Referrer-Policy", REFERRER_POLICY)
    response.headers.setdefault("Cross-Origin-Opener-Policy", CROSS_ORIGIN_OPENER_POLICY)
    response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
    return response


def _persist_turn(
    store: MessageStore | None, conversation_id: str | None,
    mode: str, role: str, content: str,
    suggestions: list[dict[str, Any]] | None = None,
) -> int | None:
    """Best-effort history write: a failure is logged, never raised — a chat
    answer must not fail because persistence did (deliberately outside the
    _DB_ERRORS → 503 taxonomy, which is reserved for retrieval).

    Answers the new row's id, or `None` when nothing was written: the turn's
    timeline entry links the rows it carries (1kg.4.2, ruling R-1)."""
    if store is None or conversation_id is None:
        return None
    try:
        return store.append(conversation_id, mode, role, content, suggestions=suggestions)
    except Exception:
        log.warning(
            "history write failed (mode=%s, conversation_id=%s, role=%s)",
            mode, conversation_id, role, exc_info=True,
        )
        return None


#: What `/chat` answered, under the names `ChatAnswer` keeps it by. `text` is
#: `resp.answer` and `created_at` is minted: `ChatResponse` has no time.
_ANSWER_FIELDS = {
    "answerable", "sources", "suggestions", "routing", "suggestions_routing", "spell_content", "stat_block",
}


def _record_timeline_entry(
    timeline: TimelineStore | None, tdb: Database | None, *,
    conversation_id: str, owner_id: int, req: ChatRequest, resp: ChatResponse,
    user_message_id: int | None, assistant_message_id: int | None,
) -> None:
    """Best-effort: the answered turn as one typed timeline entry (1kg.4.2).

    The posture of `_persist_turn`, and for the same reason: an answer must
    never fail because a record of it did. The call sits inside `chat()`'s
    `try:`, so this catch-all is load-bearing — without it a failure here
    would be answered as a 500. It runs only once `svc.answer` has returned
    and both message rows are written, on one short transaction of its own,
    and makes no provider call. A turn it cannot write — the contract bounds
    what `/chat`'s own models do not — is served by the legacy adapter from its
    rows instead.

    The log line is content-free: the exception's TYPE, never its message,
    which can quote a statement and with it the prompt or the answer (X-7).
    """
    if timeline is None or tdb is None:
        return
    try:
        created_at = datetime.now(UTC)
        answer = resp.model_dump(mode="json", include=_ANSWER_FIELDS)
        entry = {
            "schema_version": CONTRACT_VERSION, "entry_kind": "chat", "entry_id": new_entry_id(),
            "created_at": created_at, "mode": req.mode.value, "prompt": req.prompt,
            "answer": {**answer, "text": resp.answer, "created_at": created_at},
        }
        with tdb.transaction() as unit:
            timeline.append(
                unit, conversation_id, entry, created_at, owner_id=owner_id,
                user_message_id=user_message_id, assistant_message_id=assistant_message_id,
            )
    except Exception as exc:
        log.warning(
            "timeline entry write failed (mode=%s, conversation_id=%s): %s",
            req.mode.value, conversation_id, type(exc).__name__,
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


class ConversationAccess(Enum):
    """Outcome of `_authorize_conversation` — see there for the rules."""

    OWNED = "owned"
    #: Unowned AND empty. The caller is authorized, but MUST NOT go on to read
    #: the conversation: it was only observed to be empty, and another user can
    #: claim and write it in the gap before that read. Serve an empty result
    #: built from nothing instead.
    EMPTY = "empty"


def _conversation_lookup(conversation_id: str):
    """Run an ownership/content lookup, converting any failure into a 503.

    **Fails CLOSED.** Reads and ownership checks run on separate connections, so
    a transient failure (or a role that can't see `chat.conversations`) must
    never be followed by a successful cross-user read.
    """
    def run(fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            log.warning(
                "conversation authorization failed (conversation_id=%s)",
                conversation_id, exc_info=True,
            )
            raise HTTPException(
                status_code=503, detail="authorization backend unavailable"
            ) from exc
    return run


def _reject_foreign_conversation(
    store: MessageStore | None, conversation_id: str, user_id: int,
) -> None:
    """403 if `conversation_id` demonstrably belongs to someone else — without
    claiming anything.

    This is the cheap precheck that lets a write endpoint refuse an obviously
    foreign conversation before doing expensive work, while leaving the actual
    claim for the moment just before persistence. It is deliberately NOT an
    authorization decision on its own: passing it means "not known to be someone
    else's", and only the atomic claim that follows settles ownership.
    """
    if store is None:
        return
    run = _conversation_lookup(conversation_id)
    owner = run(store.owner_of, conversation_id)
    if owner is not None and owner != user_id:
        raise HTTPException(status_code=403, detail="not your conversation")


def _authorize_conversation(
    store: MessageStore | None, conversation_id: str | None, user_id: int,
    *, creating: bool,
) -> ConversationAccess:
    """Authorize this user for `conversation_id`, or raise (x5bz.2 D).

    `creating=True` (writes) uses the store's **atomic** claim, which returns the
    winning owner — one call that is both the check and the claim. That closes
    the TOCTOU window where two users racing a fresh id could both pass a
    separate pre-check.

    `creating=False` (reads) claims only a conversation that **already has
    content**. That closes the ownership table's cold-start hole — conversations
    predating the table have no owner row, and "no owner ⇒ allow" would leave all
    of that history readable by any authenticated caller — while refusing to mint
    a row for an id with nothing in it, so a GET loop over random ids can't fill
    `chat.conversations`.

    An unowned, empty conversation returns `EMPTY` rather than authorizing a
    read. "Empty" is a past-tense observation on its own connection: between the
    probe and the endpoint's read, another user can claim the id and write to it,
    and the read would then serve their content. The caller must answer from
    `EMPTY` directly instead of reading.

    Either way, the first authenticated user to touch a conversation with content
    takes ownership (ids are unguessable `crypto.randomUUID()` values held only
    by their owner), and everyone else is rejected from then on.
    """
    if store is None or conversation_id is None:
        return ConversationAccess.OWNED
    run = _conversation_lookup(conversation_id)
    if creating:
        owner = run(store.claim_conversation, conversation_id, user_id)
    else:
        owner = run(store.owner_of, conversation_id)
        if owner is None:
            if not run(store.has_content, conversation_id):
                return ConversationAccess.EMPTY
            owner = run(store.claim_conversation, conversation_id, user_id)
    if owner is not None and owner != user_id:
        raise HTTPException(status_code=403, detail="not your conversation")
    return ConversationAccess.OWNED


@app.get("/healthz")
def healthz() -> dict[str, str | bool]:
    # `migrations` (1kg.1.5) is a field of its own: `status` and `ready` are what
    # both Compose health checks assert, and neither changes meaning. It says
    # `current`, `ahead` (an older build on a newer schema, mid-rollout),
    # `unavailable` (no database yet; the instance keeps looking), `failed` (the
    # database came back with a schema this build refuses) or `unchecked` (a
    # process that never ran the startup path, like the E2E stub) — and never a
    # version.
    return {
        "status": "ok",
        "ready": "rag" in _state,
        "migrations": str(_state.get("migrations", "unchecked")),
    }


@app.get("/models")
def get_models() -> dict[str, object]:
    """Server-owned model catalog (agent-forge-harness-b8o.1, Checkpoint 1),
    as the client may know it: public ids and tier labels only (D-9, au3).
    Never an alias, model or provider name, secret name, base URL, or the
    exact provider model/snapshot string — see model_catalog.PUBLIC_MODELS."""
    return {
        "default": "auto",
        "models": [dict(AUTO_PUBLIC_ENTRY), *(public_model_entry(p) for p in enabled_profiles())],
    }


#: Marks a 429 as the CHAT limiter's, the way AUTH_THROTTLE_HEADER does for auth.
#: Cloud Run emits its own 429 when no instance is available, so the status code
#: alone proves nothing. The value also says WHICH control fired, because "slow
#: down" and "the day's budget is gone" need different words in the UI.
CHAT_THROTTLE_HEADER = "X-Chat-Throttled"


def _refuse_unstorable_prompt(prompt: str) -> None:
    """Bead 5mj: the prompt is stored text, so it takes the one rule
    (`check_plain_text`). It is persisted to PostgreSQL `text` and `jsonb`, which
    refuse U+0000, so an unrefused NUL was a provider call paid for and then a
    write that failed; a bidi override is stored text that reads differently
    from how it is stored. Raised as a validation error that carries no `input`,
    so the application's one handler (`workbench_api.handle_validation_error`)
    answers FastAPI's default 422 list with nothing of the prompt in it: the
    field, never the value."""
    try:
        check_plain_text(prompt)
    except ValueError as refused:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body", "prompt"), "msg": f"Value error, {refused}"}]
        ) from None


def _throttle_chat(request: Request, user_id: int) -> None:
    """Spend one chat request from this tester's budget, or 429."""
    try:
        check_chat_request(user_id)
    except RateLimited as exc:
        # Same reasoning as _throttle_auth: log the DERIVED source so the
        # trusted-hop configuration can be confirmed from outside.
        source = client_source(request)
        if not gcp_logging.emit(
            "WARNING", "chat request throttled", request,
            source=source, user_id=user_id, retry_after=exc.retry_after,
        ):
            log.warning(
                "chat request throttled (user_id=%s, source=%s, retry_after=%ss)",
                user_id, source, exc.retry_after,
            )
        raise HTTPException(
            status_code=429,
            detail="You're asking faster than the tavern can pour. Try again shortly.",
            headers={
                "Retry-After": str(exc.retry_after),
                CHAT_THROTTLE_HEADER: "user",
            },
        ) from exc


def _enforce_daily_cap(store: MessageStore | None) -> None:
    """Refuse once the pilot has spent its question budget for the day (x5bz.3.3).

    Counted from chat.messages rather than a counter, so it is exact across
    instances and survives the scale-to-zero that would reset an in-process one.

    **Fails closed**, in the style of `_conversation_lookup`: a count that cannot
    be read becomes a 503, never an allowed request. The alternative — letting the
    call through when the database hiccups — makes the ceiling optional at
    precisely the moment nobody is watching.

    `store is None` means no database is configured at all (local dev). There is
    nothing to count and nothing to bill against a shared key, so there is no cap.
    """
    if store is None:
        return
    try:
        spent = store.calls_today()
    except Exception as exc:
        log.warning("daily cap check failed", exc_info=True)
        raise HTTPException(
            status_code=503, detail="usage backend unavailable"
        ) from exc
    if spent < config.CHAT_DAILY_CAP:
        return
    log.warning("daily chat cap reached (%s/%s)", spent, config.CHAT_DAILY_CAP)
    raise HTTPException(
        status_code=429,
        detail="The tavern is closed for today — the daily question limit is spent.",
        headers={CHAT_THROTTLE_HEADER: "daily"},
    )


def _pre_d9_binding(
    store: MessageStore | None, conversation_id: str, alias: str,
) -> ModelProfile | None:
    """The enabled profile this conversation was bound to by naming `alias`
    itself, before D-9 (a6o), or None. Such a client keeps sending the alias it
    bound by; honouring it tells the caller nothing they did not tell the
    server. A binding made since D-9 never counts, or a caller could bind one
    through a public id and then test aliases against it."""
    if store is None:
        return None
    if store.conversation_binding(conversation_id) != ("manual", alias, PRE_D9_CATALOG_REVISION):
        return None
    return get_profile(alias)


def _require_json(request: Request) -> None:
    """FastAPI's strict content type, for a body a route reads by hand: a body
    that is not JSON is refused as FastAPI refuses it, echoing nothing."""
    kind = email.message.Message()
    kind["content-type"] = request.headers.get("content-type", "")
    subtype = kind.get_content_subtype()
    if kind.get_content_maintype() != "application" or not (subtype == "json" or subtype.endswith("+json")):
        raise RequestValidationError([{"type": "model_attributes_type", "loc": ("body",),
                                       "msg": "Input should be a valid dictionary or object to extract fields from"}])


async def _chat_request(request: Request, _session: SessionData = Depends(require_session)) -> ChatRequest:
    """The chat body, read only once the caller is signed in
    (agent-forge-harness-dl7x, PR #211 review M1), as `_attachment_upload`
    reads its own: a declared body model is parsed before any dependency runs,
    so an anonymous caller could make the app parse a default body, about 27 MB
    of objects, only to be refused. Parsed by the standard library, as FastAPI
    parses it, so a lone surrogate still reaches the stored-text rule (5mj)."""
    raw = await request.body()
    if not raw:
        raise RequestValidationError([{"type": "missing", "loc": ("body",), "msg": "Field required"}])
    _require_json(request)
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # A JSONDecodeError, bytes that are no Unicode text, or nesting past the
        # recursion limit, which was a 500 (PR #217 second review M1).
        # justification: RequestValidationError takes pydantic's untyped error dicts.
        errors: list[Any] = [{"type": "json_invalid", "loc": ("body", getattr(exc, "pos", 0)),
                              "msg": "JSON decode error"}]
    else:
        if parsed is None:  # FastAPI took a JSON null as no body at all (PR #217 second review L1).
            raise RequestValidationError([{"type": "missing", "loc": ("body",), "msg": "Field required"}])
        try:
            # from_attributes, as FastAPI validates a declared body: JSON that is
            # no object gets model_attributes_type, as it did (PR #217 review M1).
            return ChatRequest.model_validate(parsed, from_attributes=True)
        except ValidationError as exc:
            errors = [{**error, "loc": ("body", *error["loc"])}
                      for error in exc.errors(include_url=False, include_context=False, include_input=False)]
    raise RequestValidationError(errors)


# justification: FastAPI's openapi_extra is an untyped JSON dict.
def _documented_body(model: type[BaseModel]) -> dict[str, Any]:
    """A body read by hand, documented as FastAPI documents a declared one. Its
    nested models are referred to as components, which each must already be:
    ChatRequest's one, ChatMode, is, through ChatResponse."""
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schema.pop("$defs", None)
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


@app.post("/chat", response_model=ChatResponse, openapi_extra=_documented_body(ChatRequest))
def chat(
    req: Annotated[ChatRequest, Depends(_chat_request)],
    request: Request,
    svc: RagService = Depends(get_service),
    store: MessageStore | None = Depends(get_message_store),
    metrics: MetricsSink = Depends(get_metrics_sink),
    session: SessionData = Depends(require_session),
    timeline: TimelineStore | None = Depends(get_timeline_store),
    tdb: Database | None = Depends(get_timeline_database),
) -> ChatResponse:
    # Stored-text rule (5mj): a prompt that cannot be stored is refused before
    # anything is spent on it — the budget below included.
    _refuse_unstorable_prompt(req.prompt)
    # Cost guard (x5bz.3): spend one of this tester's chat budget before any
    # work happens. Before the try for the same reason as the gates below — a
    # 429 raised inside it would be caught by the `except Exception` and
    # reported as an internal error.
    _throttle_chat(request, session.user_id)
    # ...then the pilot-wide ceiling. Second because it costs a database read and
    # the per-tester budget above does not: a caller in a loop is already refused
    # before this runs. No role exemption — the account most likely to run up a
    # bill by accident is the one being used to test.
    _enforce_daily_cap(store)
    # Prompt length gate (agent-forge-harness-764): reuse the Workbench's own
    # request-side ceiling rather than a `Field(max_length=...)` on
    # ChatRequest.prompt, whose rejection would go through FastAPI's default
    # RequestValidationError handler and echo the whole oversized prompt back
    # in the 422 body (R-12, docs/adr/gm-workbench-threat-model.md). A plain
    # HTTPException here is handled ordinarily -- no echo -- exactly like the
    # model_preference 422 below. `detail` stays a static string; the prompt
    # itself must never appear in it. Raised before the try so it isn't masked
    # as a 500, same as the gates around it.
    if len(req.prompt) > CHAT_TEXT_MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"prompt exceeds the {CHAT_TEXT_MAX_CHARS}-character limit",
        )
    # Server-side role gate: the GM channel is DM-only, enforced from the session
    # role (not the UI toggle). Raised before the try so it isn't masked as a 500.
    if req.mode.value == "gm" and session.role != "dm":
        raise HTTPException(status_code=403, detail="the GM channel requires the DM role")
    # A client may omit conversation_id. It has been optional since the field was
    # a pass-through stub, and every consumer since — persistence, attachments,
    # ownership — independently chose to SKIP on None rather than reject it. That
    # made `None` mean "opt out of every server-side control", which the daily cap
    # (x5bz.3.3) would have inherited: a turn nobody persists is a turn nobody can
    # count. Minting here gives None one honest meaning instead — start a new
    # conversation — and puts these requests through the same ownership claim and
    # the same persistence as any other.
    conversation_id = req.conversation_id or str(uuid4())
    # Ownership: one atomic claim-or-reject (403 if it's someone else's).
    # Before the try for the same reason (403, not 500).
    _authorize_conversation(store, conversation_id, session.user_id, creating=True)

    # Model routing (b8o.2): validate the request, then atomically bind this
    # conversation's strategy before any provider call (D6). D1's "null
    # conversation_id" branch from the plan doesn't apply here — conversation_id
    # is always minted above before this point (a design decision made after
    # the plan was written), so every request already has a real key to bind
    # against; there's no stateless-single-turn path left to special-case.
    # Before the try for the same reason as ownership (409/422, not 500).
    #
    # Retired-binding recovery (agent-forge-harness-j9w): a conversation
    # already bound to a manual pick the catalog no longer serves (disabled or
    # removed since) gets ONE defined outcome — answered by that pick's own
    # configured successor (`fallback_alias`) if THAT is itself enabled, else
    # by auto — instead of a 422/409 on every further turn. Keyed on the
    # EXISTING binding, never on what this request happens to send, because
    # the client that bound it (D6's conversation affinity) never resends
    # anything else: posting the retired id is the model-preference 422
    # below, and posting anything different is the mismatch 409 below — both
    # forever, with no recovery, unless the server heals it here.
    #
    # The stored binding moves only once the client has provably SEEN the
    # heal: on the turn whose model_preference already names the successor
    # (`routing.requested` of a healed turn). A healed turn whose provider
    # call fails, or whose response never reaches the client, leaves the
    # binding where the client still believes it is, so the next turn heals
    # again instead of refusing a retired id the client was never told about
    # (pr156 M-1). A client that ignores `routing` altogether keeps being
    # answered by the successor on every turn.
    existing_binding = (
        _routing_store(conversation_id, lambda: store.conversation_binding(conversation_id))
        if store is not None else None
    )
    strategy: Literal["auto", "manual"]
    manual_alias: str | None
    requested: str
    retired_public_id: str | None = None
    retired_alias = (
        existing_binding[1]
        if existing_binding is not None and existing_binding[0] == "manual"
        and existing_binding[1] is not None
        # A pre-D-9 (v1) binding to a never-served alias is _pre_d9_binding's
        # own refusal below (test_legacy_model_preference.py's "any other
        # pre-D-9 binding" row) — never this healing, which is only for a
        # D-9-era manual pick that WAS served and has since been retired.
        and existing_binding[2] != PRE_D9_CATALOG_REVISION
        and get_profile(existing_binding[1]) is None
        else None
    )
    if retired_alias is not None:
        assert store is not None  # existing_binding only comes from a real store
        # CATALOG.get, never CATALOG[...]: an alias removed from the catalog
        # outright (not just disabled) has no row left to read a successor
        # from, and heals to auto like one with no successor (pr156 H-2).
        retired = CATALOG.get(retired_alias)
        successor_alias = retired.fallback_alias if retired is not None else None
        if successor_alias is not None and get_profile(successor_alias) is not None:
            strategy, manual_alias = "manual", successor_alias
        else:
            strategy, manual_alias = "auto", None
        # The binding this conversation moves to: what a client adopts as its
        # preference from here on. Never `effective`, which for 'auto' is the
        # model that answered and would be refused as a mismatch (pr156 H-1).
        requested = public_model_id(manual_alias) if manual_alias is not None else "auto"
        if req.model_preference == requested:
            # The client already names the successor: it has seen a healed
            # turn, so the stored binding follows it and this turn is ordinary.
            _routing_store(conversation_id, lambda: store.rebind_conversation_strategy(
                conversation_id, strategy=strategy, manual_alias=manual_alias,
                catalog_revision=CATALOG_REVISION,
            ))
        else:
            # A healed turn. `fallback_from` is the retired pick's public id;
            # PUBLIC_MODELS.get, not public_model_id, since a removed alias
            # has lost that row too (H-2) and would raise KeyError. With no id
            # left to name, it carries this request's own preference back, so
            # the client still learns to stop sending it; the server never
            # names an alias either way (D-9).
            retired_public = PUBLIC_MODELS.get(retired_alias)
            retired_public_id = (
                retired_public.id if retired_public is not None else req.model_preference
            )
    else:
        # D-9 (au3): the client names a model by its PUBLIC id, never the alias; a
        # real alias sent here is as unknown as any other string (no oracle), save
        # on a conversation bound by that alias before D-9 (a6o, _pre_d9_binding).
        requested = req.model_preference
        requested_profile = None if requested == "auto" else get_profile_by_public_id(requested)
        if requested != "auto" and requested_profile is None:
            requested_profile = _routing_store(
                conversation_id, lambda: _pre_d9_binding(store, conversation_id, requested),
            )
            if requested_profile is None:
                raise HTTPException(
                    status_code=422, detail=f"unknown or disabled model: {requested!r}",
                )
            requested = public_model_id(requested_profile.alias)
        strategy = "auto" if requested == "auto" else "manual"
        manual_alias = None if requested_profile is None else requested_profile.alias
        if store is not None:
            bound_strategy, bound_alias = _routing_store(conversation_id, lambda: store.claim_conversation_strategy(
                conversation_id, strategy=strategy, manual_alias=manual_alias,
                catalog_revision=CATALOG_REVISION,
            ))
            if (bound_strategy, bound_alias) != (strategy, manual_alias):
                raise HTTPException(
                    status_code=409,
                    detail="this conversation is bound to a different model preference; "
                           "start a new conversation to change it",
                )
    # Effective model resolution: Checkpoint 4 (b8o.4) adds the real per-turn
    # Auto classifier; until then 'auto' resolves to the catalog default and a
    # manual alias resolves to itself (already validated enabled above).
    if strategy == "manual":
        assert manual_alias is not None  # invariant: set exactly when strategy == "manual"
        effective_alias = manual_alias
    else:
        effective_alias = DEFAULT_ALIAS
    assert get_profile(effective_alias) is not None  # validated above; DEFAULT_ALIAS is always enabled
    # The alias and provider stay server-side (logs, traces and usage records
    # take them from generate.py); the client is told the public id only.
    # `fallback_from` is set ONLY on a healed turn above — the one signal a
    # client needs to stop sending the retired pick and adopt `requested`
    # (never an alias the server names, D-9).
    routing = RoutingInfo(
        requested=requested, effective=public_model_id(effective_alias), strategy=strategy,
        fallback_from=retired_public_id,
    )

    # yje.5.1.1: one usage-capture operation per turn, created AFTER every gate
    # above (a turn a gate refuses makes no provider call and must record
    # nothing) and before any provider call below. It is outside the try on
    # purpose — `begin_operation` cannot raise, by construction rather than by
    # hope — and torn down in the finally at the end of this chain.
    op_token = usage_capture.begin_operation(
        mode=req.mode.value, billed_account_id=session.user_id,
        actor_kind=usage_capture.ACTOR_ACCOUNT, campaign_id=None, request=request,
    )
    # 0u02: the turn's one provider budget, which every call below draws down,
    # so the turn answers before Cloud Run's request timeout cuts it off.
    # Beside the operation for the same reasons, and ended in the same finally.
    turn_token = begin_turn()
    try:
        attachment_context, attachment_label = _fetch_attachment_context(
            store, conversation_id,
        )
        resp = svc.answer(
            req.prompt, mode=req.mode.value, conversation_id=conversation_id,
            attachment_context=attachment_context, attachment_label=attachment_label,
        )
        resp.routing = routing
        if req.mode.value == "spell" and resp.suggestions is not None:
            # Suggestions always route to the economy subroute regardless of
            # the answer's routing (D3) — one enabled profile today, so
            # "economy subroute" is the same baseline; Checkpoint 4 gives
            # this its own real resolution once more tiers exist.
            resp.suggestions_routing = SuggestionsRoutingInfo(
                effective=public_model_id(DEFAULT_ALIAS),
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
        user_message_id = _persist_turn(store, conversation_id, req.mode.value, "user", req.prompt)
        assistant_message_id = _persist_turn(
            store, conversation_id, req.mode.value, "assistant", resp.answer,
            suggestions=(
                [s.model_dump(mode="json") for s in resp.suggestions]
                if resp.suggestions else None
            ),
        )
        _record_timeline_entry(
            timeline, tdb, conversation_id=conversation_id, owner_id=session.user_id, req=req, resp=resp,
            user_message_id=user_message_id, assistant_message_id=assistant_message_id,
        )
        return resp
    except RetrievalStageError as exc:
        # xiu.2.3: a typed retrieval-stage fault, never a generation error. One
        # content-free line per lost stage the turn carries.
        for attempt in exc.attempts or (exc.attempt(),):
            log.warning(
                "retrieval stage failed on /chat (mode=%s, conversation_id=%s, source=%s, stage=%s, "
                "outcome=%s, error=%s)",
                req.mode.value, conversation_id, attempt.source_kind, attempt.stage, attempt.outcome,
                attempt.error_class,
            )
        raise _retrieval_stage_error(exc) from exc
    except _LLM_ERRORS as exc:
        # D4: normalize to a bounded category, then look up its status/
        # retryable pair — replaces the old blanket "LLM error -> 502".
        category = normalize_llm_error(exc)
        status_code, retryable = ERROR_STATUS[category]
        # The class, never the message: a provider error can echo the prompt (N-1).
        log.warning(
            "LLM error on /chat (mode=%s, conversation_id=%s, category=%s, error=%s)",
            req.mode.value, conversation_id, category, type(exc).__name__,
        )
        headers: dict[str, str] = {}
        if category == "rate_limit":
            response = getattr(exc, "response", None)
            retry_after = response.headers.get("retry-after") if response is not None else None
            if retry_after:
                headers["Retry-After"] = retry_after
        raise HTTPException(
            status_code=status_code,
            detail={
                "category": category, "retryable": retryable,
                "message": _ERROR_DETAIL[category],
            },
            headers=headers or None,
        ) from exc
    except _DB_ERRORS as exc:
        # Retrieval backend (Postgres/pgvector) unavailable — upstream, retryable.
        log.warning(
            "retrieval backend error on /chat (mode=%s, conversation_id=%s, error=%s)",
            req.mode.value, conversation_id, type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="retrieval backend unavailable") from exc
    except EmbeddingUnavailableError as exc:
        # Embedding can't run (missing OPENAI_API_KEY) — service-side
        # unavailability, not a crash (1em.3; previously sys.exit killed the worker).
        log.warning(
            "embedding unavailable on /chat (mode=%s, conversation_id=%s, error=%s)",
            req.mode.value, conversation_id, type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="embedding backend unavailable") from exc
    except Exception:
        # Anything else is a bug in our code — log the full traceback, return 500.
        log.exception("internal error on /chat (mode=%s)", req.mode.value)
        raise HTTPException(status_code=500, detail="internal error") from None
    finally:
        end_turn(turn_token)
        usage_capture.end_operation(op_token)


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
    access = _authorize_conversation(store, conversation_id, session.user_id, creating=False)
    if access is ConversationAccess.EMPTY:
        # Unowned and empty: answer from the authorization result, do NOT read.
        # A second read could land after another user claimed and wrote the id.
        return MessagesResponse(conversation_id=conversation_id, messages=[])
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


async def _attachment_upload(
    request: Request, _session: SessionData = Depends(require_session),
) -> AttachmentUploadRequest:
    """The upload body, read only once the caller is signed in
    (agent-forge-harness-ust7, review H2). FastAPI reads and parses a declared
    body model before any dependency runs, so an anonymous caller could make the
    app parse a body up to the attachment ceiling, about 94 MB of objects each,
    only to be refused. Read raw here, the body-limit middleware caps it; a body
    that is not JSON is refused as FastAPI's strict content type refused it, and
    no 422 repeats what it was sent."""
    raw = await request.body()
    _require_json(request)
    try:
        return AttachmentUploadRequest.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
    raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors])


@app.post(
    "/conversations/{conversation_id}/attachments", response_model=AttachmentResponse,
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/json": {"schema": AttachmentUploadRequest.model_json_schema()}}}},
)
def upload_attachment(
    conversation_id: str,
    req: AttachmentUploadRequest = Depends(_attachment_upload),
    store: MessageStore | None = Depends(get_message_store),
    session: SessionData = Depends(require_session),
) -> AttachmentResponse:
    if store is None:
        raise HTTPException(status_code=503, detail="attachments unavailable")
    # Ownership is settled in two steps here, deliberately:
    #   1. a precheck that only REJECTS a conversation already owned by someone
    #      else, so a foreign id still 403s without any decoding work; then
    #   2. validation; then
    #   3. the atomic claim, immediately before persisting.
    # Claiming up front instead would let a stream of rejected uploads (bad
    # base64, oversized, unsupported type) mint an ownership row per request —
    # unbounded writes to chat.conversations for input that never gets stored.
    _reject_foreign_conversation(store, conversation_id, session.user_id)
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
    # Payload is good — now take ownership atomically. The claim (not the
    # precheck above) is the authorization decision: it settles the race between
    # two users reaching a fresh id at once.
    _authorize_conversation(store, conversation_id, session.user_id, creating=True)
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
    access = _authorize_conversation(store, conversation_id, session.user_id, creating=False)
    if access is ConversationAccess.EMPTY:
        # See conversation_messages: answer from the authorization result rather
        # than re-reading an id that another user may have claimed since.
        return AttachmentsResponse(conversation_id=conversation_id, attachments=[])
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
    request: Request,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
) -> AuthUser:
    """Create an account by redeeming a one-time invite, then start a session.
    The invite carries the role; the token is consumed atomically."""
    secret = _session_secret()
    _throttle_auth(request, req.email)
    # Cheap pre-check BEFORE the (deliberately expensive) argon2 hash, so an
    # unauthenticated caller without a usable invite can't burn CPU/memory at
    # will. The atomic re-check inside redeem_invite is still what actually
    # guarantees single use — this only short-circuits the obvious rejects.
    invite = _auth_lookup("invite lookup", lambda: store.get_invite(req.invite))
    if invite is None:
        raise HTTPException(status_code=400, detail="Unknown invite link.")
    try:
        invite.check_redeemable()
    except InviteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        # Hashing runs OUTSIDE _auth_lookup: it is CPU work in this process, not
        # a call to the backend, so its failures are not backend unavailability.
        password_hash = hash_password(req.password)
        # ...and the store call is wrapped, so an outage mid-redemption is a 503
        # rather than a 500. The domain errors below pass straight through it.
        user = _auth_lookup(
            "redeem invite",
            lambda: store.redeem_invite(req.invite, req.email, password_hash),
        )
    except HashingCapacityError as exc:
        # Overloaded, not rejected — shed load with a retryable status.
        log.warning("signup shed: %s", exc)
        raise HTTPException(status_code=503, detail="busy, please retry") from exc
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
    request: Request,
    response: Response,
    store: AuthStore = Depends(get_auth_store),
) -> AuthUser:
    secret = _session_secret()
    # Before the lookup AND before hashing: this is the brute-force ceiling and
    # what keeps waiting-for-a-hash-slot requests from starving the instance.
    _throttle_auth(request, req.email)
    # Identity and hash together, in one guarded query. A store outage here must
    # not become a 500: this is the endpoint testers hit when the pilot looks
    # broken, and "retry later" is the truthful answer.
    creds = _auth_lookup("credentials lookup", lambda: store.get_credentials(req.email))
    stored_hash = creds[1] if creds is not None else None
    # Always run one argon2 verification, even for an unknown email — otherwise
    # the response time itself reveals whether an account exists, and the generic
    # message below buys nothing. DUMMY_PASSWORD_HASH never matches.
    try:
        ok = verify_password(stored_hash or DUMMY_PASSWORD_HASH, req.password)
    except HashingCapacityError as exc:
        # Overloaded, not wrong credentials — must NOT be reported as a 401.
        log.warning("login shed: %s", exc)
        raise HTTPException(status_code=503, detail="busy, please retry") from exc
    if creds is None or not ok:
        # One generic message — never reveal whether the email is registered.
        raise HTTPException(status_code=401, detail="invalid email or password")
    user = creds[0]
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
    request: Request,
    session: SessionData = Depends(require_session),
    store: AuthStore = Depends(get_auth_store),
) -> AuthUser:
    """Who the session cookie belongs to.

    `require_session` has already re-read the account this request (that re-read
    is what makes a deleted account stop working immediately), so normally there
    is nothing left to fetch. The lookup below is for a caller that supplies its
    own session dependency and therefore never populated the stash — it is
    guarded, because an unguarded repeat of this query is exactly how a store
    outage used to leave here as a 500.
    """
    user: User | None = getattr(request.state, "auth_user", None)
    if user is None:
        user = _auth_lookup("account lookup",
                            lambda: store.get_user_by_id(session.user_id))
    if user is None:
        # Session points at a since-deleted account — treat as unauthenticated.
        raise HTTPException(status_code=401, detail="account not found")
    return AuthUser(email=user.email, role=user.role)


def _job_driver() -> job_driver.JobDriver | None:
    # A scheduler call may be the only traffic a degraded instance gets, so it
    # looks for the database like the other getters (it runs in the thread pool).
    if "jobs" not in _state:
        recover_database()
    return _state.get("jobs")


def _job_queue() -> PostgresJobQueue | None:
    """The outbox a revocation enqueues its reconciliation in (1kg.2.2)."""
    if "job_queue" not in _state:
        recover_database()
    return _state.get("job_queue")


def _media() -> assets_api.MediaRuntime | None:
    """The media runtime, once a store exists (1kg.8.1.2); None otherwise."""
    if "jobs" not in _state:
        recover_database()
    return _state.get("media")


def _media_enabled() -> bool:
    """The capability switch the media routes consult on every match: off
    unless startup read it on (Q-5), so a test app with no lifespan is dark."""
    settings = _state.get("media_settings")
    return isinstance(settings, media_objects.MediaSettings) and settings.enabled


def get_table_sessions() -> TableSessions | None:
    """The live table session's lifecycle (1kg.2.3), built with the stores; None
    on a degraded instance, which the table-session routes answer with a 503."""
    if "table_sessions" not in _state:
        recover_database()
    return _state.get("table_sessions")


def start_gate(caller: SessionData) -> None:
    """The one check point for starting a live table (1kg.2.3, L-19; owner
    decision D-3: running a live table is Paid, joining one is Free).

    Start calls it once, before any database access, and nothing else calls it:
    End, Rotate, the status read, a screen revoke, the screen mint and Leave are
    never gated on a tier — a narrowing is never gated on payment, and a session
    in progress runs to its end. Today it admits every account the `dm` gate
    admits. `ubw` and `yje.4.1` replace its body and give its refusal a shape."""
    return None


#: The Workbench envelope of the auth throttle's 429 and of a hashing outage,
#: for the Workbench routes that check a password (a seat's Remove and a
#: document's delete, SEC-40).
REAUTH_THROTTLED_MESSAGE = "Too many attempts. Wait, then try again."
REAUTH_BUSY_MESSAGE = "That can't be checked right now. Try again."


def reauthenticator(request: Request, store: AuthStore = Depends(get_auth_store)) -> Callable[[str], None]:
    """The re-authentication of a seat's Remove and a document's delete
    (SEC-40), as a dependency: it hands the route a `check(password)` bound to
    this request's account and auth store. The store is the one
    `require_session` already resolved for this request, so declaring it here
    adds no lookup and no outage path of its own."""

    def check(password: str) -> None:
        reauthenticate(request, store, password)

    return check


def reauthenticate(request: Request, store: AuthStore, password: str) -> None:
    """SEC-40: the password of the account this request signed in as, checked
    again before a Remove or a document delete — BEFORE any transaction
    opens, because argon2 never runs while a lock is held (bead 1kg.2.2, L-12).

    In order: the auth attempt budget (`_throttle_auth`), whose legacy 429 is
    answered in the Workbench envelope with both of its headers; the
    credentials, whose outage is a 503; exactly one argon2 verification, against
    `DUMMY_PASSWORD_HASH` when there are no credentials, as login does, whose
    capacity refusal is a 503; and a wrong password is `reauth_failed()` — a
    403 that names nothing, never the 401 the client would sign out on. The
    password is never logged, echoed or chained into an exception."""
    user = getattr(request.state, "auth_user", None)
    if not isinstance(user, User):
        reauth_failed()
    throttled: HTTPException | None = None
    try:
        _throttle_auth(request, user.email)
    except HTTPException as exc:
        throttled = exc
    if throttled is not None:
        headers = dict(throttled.headers or {})
        wait = headers.get("Retry-After", "")
        raise campaigns_api.refusal(
            429,
            ErrorCode.THROTTLED_USER,
            REAUTH_THROTTLED_MESSAGE,
            retryable=True,
            retry_after_s=int(wait) if wait.isdigit() else None,
            headers=headers,
        )
    outage = False
    try:
        creds = _auth_lookup("credentials lookup", lambda: store.get_credentials(user.email))
    except HTTPException:
        outage, creds = True, None
    if outage:
        raise campaigns_api.unavailable(REAUTH_BUSY_MESSAGE)
    stored_hash = creds[1] if creds is not None else None
    busy = False
    try:
        matches = verify_password(stored_hash or DUMMY_PASSWORD_HASH, password)
    except HashingCapacityError:
        busy, matches = True, False
    if busy:
        raise campaigns_api.unavailable(REAUTH_BUSY_MESSAGE)
    if creds is None or not matches or creds[0].id != user.id:
        reauth_failed()


def get_group_stores() -> groups_api.GroupStores:
    """The group routes' stores (agent-forge-harness-btb). The campaigns, the
    sessions and the ledger are `campaigns_api.get_campaign_stores()`'s own, so
    whatever slot clear that module gives its sessions reaches the group routes
    too; only this module may build a concrete eligibility store (T-G2). The
    table-namespace narrowing is passed by name until `1ir.2.3` builds that
    namespace. Tests override this dependency."""
    from .document_store import PostgresDocumentStore
    from .eligibility import no_table_namespace
    from .eligibility_store import PostgresEligibilityStore

    shared = campaigns_api.get_campaign_stores()
    return groups_api.GroupStores(
        campaigns=shared.campaigns,
        eligibility=PostgresEligibilityStore(),
        documents=PostgresDocumentStore(),
        sessions=shared.sessions,
        audit=shared.audit,
        table_namespace=no_table_namespace,
    )


#: The GM gate every Workbench router is built with (agent-forge-harness-oe6).
WORKBENCH_GM = gm_session(require_session)
app.include_router(conversations_api.build_router(WORKBENCH_GM, get_timeline_database))
app.include_router(timeline_api.build_router(WORKBENCH_GM, get_timeline_store, get_timeline_database))
app.include_router(
    campaigns_api.build_router(WORKBENCH_GM, get_timeline_database, reauthenticator, _job_queue, _job_driver)
)
app.include_router(seats_api.build_router(require_session, get_timeline_database))
app.include_router(documents_api.build_router(WORKBENCH_GM, get_timeline_database))
app.include_router(document_lifecycle_api.build_router(WORKBENCH_GM, get_timeline_database, reauthenticator))
app.include_router(assets_api.build_router(WORKBENCH_GM, get_timeline_database, _media, _media_enabled))
app.include_router(table_session_api.build_router(WORKBENCH_GM, get_table_sessions, _job_driver, start_gate))
app.include_router(table_api.build_router(require_session, get_auth_store, _clear_session_cookie, get_table_sessions))
app.include_router(tool_invocations_api.build_router(WORKBENCH_GM, get_timeline_database, get_message_store))
app.include_router(groups_api.build_router(WORKBENCH_GM, get_timeline_database, get_group_stores))


app.include_router(job_driver.build_router(_job_driver))
# Mount the pre-built UI last, as an ALLOWLIST fallback, not a catch-all
# (agent-forge-harness-y40) -- see service/spa_fallback.py for what each path
# answers and why the order matters. Only active when `cd ui && bun run build`
# has been run (ui/dist/ must exist).
_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
install_spa(app, _UI_DIST)

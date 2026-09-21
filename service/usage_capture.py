"""
One privacy-bounded structured log record per provider attempt
(agent-forge-harness-yje.5.1.1, "yje.5.1 slice a — measure now").

Production measures no model cost at all: the graph passes no AttemptObserver,
so NullAttemptObserver discards every attempt, embedding calls record nothing,
and RAG_TRACING/Langfuse are unset in production. Every cost figure in the
billing plan is therefore a list-price estimate. This module makes one real
number possible: it writes one structured record per request actually sent to a
provider — purpose, the alias actually called, provider, tokens, status, retry
index, latency — from which `scripts/usage_cost_report.py` computes cost by
mode per day and per account per month. See docs/runbooks/usage-capture.md.

Deliberately NOT here (slice b, agent-forge-harness-yje.5.1.2): any table,
migration, store or price table. The sink is Cloud Logging and nothing else.

THREE RULES THIS MODULE EXISTS TO ENFORCE

1. *No content, ever.* A record's key set is closed (`EXPECTED_KEYS`) and every
   value is an id, an enum member, a count or a duration. No prompt, answer,
   suggestion, spell or stat-block text, no attachment name, no email, no
   exception message — `error_class` is `type(exc).__name__` and nothing more.
   `billed_account_id` is an internal integer account id, which is allowed in
   an access-controlled structured log (service/app.py:659 already logs one)
   and must never become a metric label (docs/observability/metrics-standard.md).

2. *A logging failure never fails, slows or degrades a turn.* Every public
   entry point here catches Exception, logs one bounded warning and returns a
   harmless value — mirroring `service/metrics.record_safely`. This matters
   most in two places callers cannot see: `generate_result` records inside
   `except BaseException` before re-raising (an observer that raised there
   would turn a 429 into a 500), and `suggest_node`/`structure_node` wrap their
   calls in `except Exception` and degrade to None (an observer that raised
   there would silently drop a spell card and still return 200).

3. *Nothing is recorded outside a live turn.* Capture happens only when an
   operation context exists, which only `/chat` creates. Eval scripts, the E2E
   app and unit tests call the same functions and stay silent.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from ingestion import retrieval
from ingestion.retrieval import EMBED_MODEL

from . import gcp_logging
from .generate import AttemptObserver, GenerationResult, NullAttemptObserver
from .model_catalog import CATALOG

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The contract: constants the runbook query and slice b both read
# ---------------------------------------------------------------------------

#: The filter key. Cloud Logging queries filter on `jsonPayload.event`, never on
#: message text — filtering on text is how "read the log and compare" has
#: silently proved nothing before (see service/gcp_logging.py's docstring).
EVENT = "provider_attempt"
RECORD_VERSION = 1
RECORD_MESSAGE = "provider attempt"
RECORD_SEVERITY = "INFO"

OPERATION_CHAT_TURN = "chat_turn"

PURPOSE_EMBEDDING = "embedding"
PURPOSE_ANSWER = "answer"
PURPOSE_SUGGESTIONS = "suggestions"
PURPOSE_SPELL_STRUCTURING = "spell_structuring"
PURPOSE_STATBLOCK_STRUCTURING = "statblock_structuring"
PURPOSES = frozenset({
    PURPOSE_EMBEDDING, PURPOSE_ANSWER, PURPOSE_SUGGESTIONS,
    PURPOSE_SPELL_STRUCTURING, PURPOSE_STATBLOCK_STRUCTURING,
})

ACTOR_ACCOUNT = "account"
ACTOR_KINDS = frozenset({ACTOR_ACCOUNT, "participant", "guest", "system"})

STATUS_OK = "ok"
STATUS_ERROR = "error"

#: The embedding model is not in the generation catalog; its provider is known.
_EMBEDDING_PROVIDER = "openai"

#: The single `configurable` key the operation travels under inside the graph.
#: It holds the Operation OBJECT, never a bare string or int: langchain_core
#: copies primitive `configurable` values into tracing metadata, which would
#: put ids somewhere this module does not control.
CONFIG_KEY = "usage_operation"

#: A record has exactly these keys, in every branch, always. This is what makes
#: it impossible for a future field to smuggle content into the log. Asserted on
#: the BUILDER's dict: `gcp_logging.emit` then adds `severity` and `message`
#: unconditionally and the trace key only when the request carries a valid one.
EXPECTED_KEYS = frozenset({
    "event", "record_version", "operation_id", "operation", "purpose", "mode",
    "alias", "provider", "retry_index", "status", "error_class", "finish_reason",
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
    "latency_ms", "provider_request_id", "billed_account_id", "actor_kind",
    "campaign_id",
})


# ---------------------------------------------------------------------------
# The operation context — one per /chat turn
# ---------------------------------------------------------------------------

class _NoHeaders:
    """Stand-in for a request when there is none. `gcp_logging.emit` reads
    `.headers` to find the trace id; an empty mapping simply means no trace."""

    headers: Mapping[str, str] = {}


@dataclass(frozen=True)
class Operation:
    """What every attempt of one logical turn shares. Carries the live request
    because `gcp_logging.emit` needs `.headers` for the trace join, and the
    capture points are five frames below the endpoint that has it."""

    operation_id: str
    operation: str
    mode: str
    billed_account_id: int
    actor_kind: str
    campaign_id: str | None
    # justification: FastAPI's Request, or any object exposing `.headers`. The
    # emitter reads nothing else off it and typing it as Request would force
    # every test to build one.
    request: Any


_CURRENT: ContextVar[Operation | None] = ContextVar("usage_capture_operation", default=None)


def _warn(stage: str, exc: BaseException) -> None:
    """One bounded warning. The exception CLASS only — never its message, which
    can contain the prompt, the answer or a key."""
    log.warning("usage capture failed (stage=%s, error=%s)", stage, type(exc).__name__)


def begin_operation(
    *, mode: str, billed_account_id: int, actor_kind: str = ACTOR_ACCOUNT,
    campaign_id: str | None = None, request: Any = None,
) -> Token[Operation | None] | None:
    """Start one turn's capture context. Called from `chat()` OUTSIDE its try,
    so it must not be able to raise — that is the mechanism, not a hope."""
    try:
        operation = Operation(
            operation_id=uuid.uuid4().hex,
            operation=OPERATION_CHAT_TURN,
            mode=mode,
            billed_account_id=billed_account_id,
            actor_kind=actor_kind,
            campaign_id=campaign_id,
            request=request if request is not None else _NoHeaders(),
        )
        return _CURRENT.set(operation)
    except Exception as exc:
        _warn("begin_operation", exc)
        return None


def end_operation(token: Token[Operation | None] | None) -> None:
    """Tear the context down. Tolerates a None token (begin_operation failed)
    and a token from another context (it is still better to leave the variable
    alone than to fail a request that has already been answered)."""
    if token is None:
        return
    try:
        _CURRENT.reset(token)
    except Exception as exc:
        _warn("end_operation", exc)


def current_operation() -> Operation | None:
    try:
        return _CURRENT.get()
    except Exception as exc:  # pragma: no cover - ContextVar.get does not raise
        _warn("current_operation", exc)
        return None


def run_config_with_operation(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Merge the current operation into a LangChain run config under one key.

    Returns `config` untouched when there is no operation — so a service with
    tracing off and no turn in flight still invokes the graph with `config=None`
    exactly as it does today (service/tests/test_tracing.py pins that).
    """
    try:
        operation = current_operation()
        if operation is None:
            return config
        merged: dict[str, Any] = dict(config or {})
        configurable: dict[str, Any] = dict(merged.get("configurable") or {})
        configurable[CONFIG_KEY] = operation
        merged["configurable"] = configurable
        return merged
    except Exception as exc:
        _warn("run_config_with_operation", exc)
        return config


def operation_from_config(config: Any) -> Operation | None:
    """Read the operation back out of a node's run config. Never raises: a
    malformed or hostile config yields None and capture simply does not happen."""
    try:
        if config is None:
            return None
        configurable = config.get("configurable") or {}
        candidate = configurable.get(CONFIG_KEY)
        return candidate if isinstance(candidate, Operation) else None
    except Exception as exc:
        _warn("operation_from_config", exc)
        return None


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def _provider_for(alias: str) -> str | None:
    """The provider that was actually called, or None. Never a guess."""
    if alias == EMBED_MODEL:
        return _EMBEDDING_PROVIDER
    profile = CATALOG.get(alias)
    return profile.provider if profile is not None else None


def build_record(
    *, operation: Operation, purpose: str, alias: str, retry_index: int,
    status: str, error_class: str | None, finish_reason: str | None,
    input_tokens: int | None, cached_input_tokens: int | None,
    output_tokens: int | None, reasoning_tokens: int | None,
    latency_ms: float, provider_request_id: str | None,
) -> dict[str, Any]:
    """The one place a record is shaped. Every key of `EXPECTED_KEYS` is written
    here literally, in every branch, so no branch can add or drop one.

    Unknown token counts stay None. A provider that did not report output tokens
    is not a provider that used zero, and the report must be able to tell the
    difference (service/tests/test_generation_result.py pins the same contract
    one layer down).
    """
    return {
        "event": EVENT,
        "record_version": RECORD_VERSION,
        "operation_id": operation.operation_id,
        "operation": operation.operation,
        "purpose": purpose,
        "mode": operation.mode,
        "alias": alias,
        "provider": _provider_for(alias),
        "retry_index": retry_index,
        "status": status,
        "error_class": error_class,
        "finish_reason": finish_reason,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "latency_ms": latency_ms,
        "provider_request_id": provider_request_id,
        "billed_account_id": operation.billed_account_id,
        "actor_kind": operation.actor_kind,
        "campaign_id": operation.campaign_id,
    }


def _emit_record(operation: Operation, fields: dict[str, Any]) -> None:
    """One synchronous in-process write. No network call, no retry, no sleep,
    no lock, no buffer — emission is on the request path."""
    if not gcp_logging.emit(RECORD_SEVERITY, RECORD_MESSAGE, operation.request, **fields):
        # Off Cloud Run `emit` writes nothing and returns False; keep local runs
        # and tests readable with one ordinary log line carrying the same fields.
        log.info("usage capture record: %s", json.dumps(fields, sort_keys=True, default=str))


# ---------------------------------------------------------------------------
# The per-call-site recorder
# ---------------------------------------------------------------------------

class AttemptRecorder:
    """One recorder per call site (one `generate_*` call, or one `embed_query`).

    It counts its own records to produce `retry_index`, and learns each
    attempt's start through `attempt_started` — which `generate_result` calls
    immediately before `client.invoke`, and therefore AFTER the backoff sleep of
    the previous attempt. That is what keeps the backoff out of `latency_ms`.
    The clock is injected so a test can move it and watch the assertion fail.

    Satisfies `service.generate.AttemptObserver` (`record`) and its optional
    companion `AttemptStartObserver` (`attempt_started`); `record_embedding` is
    the embedding seam's equivalent of `record`.
    """

    def __init__(
        self, operation: Operation, *, purpose: str, alias: str,
        now: Callable[[], float] = time.perf_counter,
        emit: Callable[[Operation, dict[str, Any]], None] | None = None,
    ):
        self._operation = operation
        self._purpose = purpose
        self._alias = alias
        self._now = now
        self._emit = emit
        self._retry_index = 0
        self._started: float | None = None

    # -- the attempt-start hook (requirement 3a) ----------------------------
    def attempt_started(self) -> None:
        try:
            self._started = self._now()
        except Exception as exc:
            _warn("attempt_started", exc)
            self._started = None

    # -- generation attempts ------------------------------------------------
    def record(
        self, *, alias: str, result: GenerationResult | None, error: BaseException | None,
    ) -> None:
        try:
            usage = result.usage if result is not None else None
            self._write(
                alias=alias,
                status=STATUS_OK if error is None else STATUS_ERROR,
                error_class=type(error).__name__ if error is not None else None,
                finish_reason=result.finish_reason if result is not None else None,
                input_tokens=usage.input_tokens if usage is not None else None,
                cached_input_tokens=usage.cached_input_tokens if usage is not None else None,
                output_tokens=usage.output_tokens if usage is not None else None,
                reasoning_tokens=usage.reasoning_tokens if usage is not None else None,
                provider_request_id=result.provider_request_id if result is not None else None,
            )
        except Exception as exc:
            _warn("record", exc)

    # -- embedding attempts -------------------------------------------------
    def record_embedding(
        self, *, input_tokens: int | None, error: BaseException | None,
    ) -> None:
        try:
            self._write(
                alias=self._alias,
                status=STATUS_OK if error is None else STATUS_ERROR,
                error_class=type(error).__name__ if error is not None else None,
                finish_reason=None,
                input_tokens=input_tokens,
                cached_input_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
                provider_request_id=None,
            )
        except Exception as exc:
            _warn("record_embedding", exc)

    def _write(
        self, *, alias: str, status: str, error_class: str | None,
        finish_reason: str | None, input_tokens: int | None,
        cached_input_tokens: int | None, output_tokens: int | None,
        reasoning_tokens: int | None, provider_request_id: str | None,
    ) -> None:
        started = self._started
        latency_ms = 0.0
        if started is not None:
            latency_ms = (self._now() - started) * 1000.0
        self._started = None
        fields = build_record(
            operation=self._operation, purpose=self._purpose, alias=alias,
            retry_index=self._retry_index, status=status, error_class=error_class,
            finish_reason=finish_reason, input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens, output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens, latency_ms=latency_ms,
            provider_request_id=provider_request_id,
        )
        self._retry_index += 1
        emitter = self._emit if self._emit is not None else _emit_record
        emitter(self._operation, fields)


# ---------------------------------------------------------------------------
# What the graph nodes call. None of these can raise.
# ---------------------------------------------------------------------------

def observer_for(config: Any, *, purpose: str, alias: str) -> AttemptObserver:
    """An observer bound to this call site, or a no-op one when there is no
    turn in flight. Built BEFORE the caller's `try:` so that a failure here
    could never be swallowed into a 200 with missing content — and it cannot
    fail anyway, which is what this except exists to guarantee."""
    try:
        operation = operation_from_config(config)
        if operation is None:
            return NullAttemptObserver()
        return AttemptRecorder(operation, purpose=purpose, alias=alias)
    except Exception as exc:
        _warn("observer_for", exc)
        return NullAttemptObserver()


def begin_embedding_scope(config: Any) -> Any:
    """Install the embedding sink for the duration of one `embed` call.

    The sink is scoped rather than passed because neither `RagRetriever.embed`
    nor its test fakes can carry a new argument, and a per-instance attribute
    would be a race: the service and its compiled graph are process-wide
    singletons and /chat is served concurrently from a thread pool.
    """
    try:
        operation = operation_from_config(config)
        if operation is None:
            return None
        recorder = AttemptRecorder(operation, purpose=PURPOSE_EMBEDDING, alias=EMBED_MODEL)
        return retrieval.set_embedding_sink(recorder)
    except Exception as exc:
        _warn("begin_embedding_scope", exc)
        return None


def end_embedding_scope(token: Any) -> None:
    if token is None:
        return
    try:
        retrieval.reset_embedding_sink(token)
    except Exception as exc:
        _warn("end_embedding_scope", exc)

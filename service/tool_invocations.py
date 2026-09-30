"""GM tool invocations: the service (agent-forge-harness-1kg.4.1, slice B).

A GM asks for a tool with a brief; the answer, and every later status read, is
a `ToolInvocation` (docs/workbench-wire-contract.md). This module holds the
rules and no SQL: the rows are `service/tool_invocation_store.py`'s, the turn
in the conversation is `service/timeline_store.py`'s, and the three routes are
`service/tool_invocations_api.py`'s.

Three steps, so that every interleaving can be tested without threads:

1. `submit` — **T1**, one short transaction. The GM's in-flight advisory lock
   first, then ownership (every lookup runs, one decision: the one 404), then
   an existing key is answered by state from the stored row alone (C-1), then
   the guards in I-8's order, then the writes: the timeline entry, the
   invocation, the attempt row with a fresh `operation_id`. Nothing of the
   provider has run when it commits (SEC-34).
2. `execute` — the executor's `run`, with **no unit of work open**; the route
   wraps it in the ledger's operation (`usage_capture.begin_operation`).
3. `complete` — **T2**. The fence (`working`, this attempt, before its
   deadline) is taken first; only through it does an outcome land, and only
   then does the executor's `finish` run, in the same transaction (X-6).

**The cap and the day are shared with AI edits** (`1kg.5.5`, I-3). The X-5
count and the pilot day are read through `stores.load`, a
`service/workbench_load.py` reader that counts tool invocations and AI document
edits alike, so neither route can run past the other's work.

`read` and `cancel` are one transaction each. Every path that meets a `working`
row past its deadline settles it there (I-13, lazy expiry): `cancelled` when a
cancel was asked for, else `failed` with `attempt_expired`.

**Production runs nothing.** A tool runs only when the deployment names it
(`WORKBENCH_ENABLED_TOOLS`), its capability is switched on, and an executor is
registered for it — and this bead registers none (I-7). Availability is decided
in one function, `tool_availability`, which 1kg.9.6 replaces (C-12).

**The model is chosen here and never shown** (D-8, D-9, SEC-39):
`resolve_tool_model` is the one place a tier mapping (bead iov) changes, and
`ExecutionContext.client()` re-checks the admission's alias against the
Workbench provider allowlist before any client is built (C-8). No message of
this module names a model, a provider, a brief, a result or an invocation id,
and no log line carries anything but an operation id, a tool id, a code, an
attempt number and an exception's class (I-24).

An executor's `run` reports a refused answer with one of two markers, which
`failure_for` maps before any provider error: `OutputRefused` (the provider
answered and the answer cannot become a result: `provider_failed`, retryable;
1kg.4.4 I-3) and `NotInSources` (the rules corpus does not ground the brief:
`not_in_sources`, final; 1kg.4.3 I-7). Neither carries a message of its own.
`ExecutionContext.read` runs one lock-free read in a transaction of its own,
closed before it returns, so `run` never holds a connection across a provider
call; `ExecutionContext.now` is the route's clock (I-4).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NoReturn, Protocol

import openai
from pydantic import BaseModel, TypeAdapter, ValidationError

import config

from . import ratelimit, usage_capture
from .campaign_store import Campaign, CampaignStore
from .campaigns_api import ARCHIVED_MESSAGE
from .conversation_store import ConversationStore
from .db import TransactionalDatabase, UnitOfWork
from .generate import LLMClient
from .model_catalog import DEFAULT_ALIAS, workbench_profile
from .models import REFUSAL
from .providers import ProviderClientFactory
from .session import SessionData
from .timeline_store import EntryNotStored, TimelineStore, ToolTurnStore, new_entry_id
from .tool_invocation_store import ATTEMPT_MAX, InvocationNotStored, InvocationRow, ToolInvocationStore
from .workbench_contracts import (
    CONTRACT_VERSION,
    CardResult,
    DocumentResult,
    ErrorCode,
    ErrorInfo,
    MediaResult,
    ToolId,
    ToolInvocation,
    ToolInvocationRequest,
    ToolResult,
    redacted_errors,
)
from .workbench_load import WorkbenchLoad
from .workbench_registry import REGISTRY

log = logging.getLogger(__name__)

# ── The numbers ──────────────────────────────────────────────────────────────

#: X-5: a GM runs at most this many tool invocations at once, across every
#: campaign and every instance. `/chat` turns are not counted.
IN_FLIGHT_CAP: Final = 2
#: How many of the caller's in-flight ids a `cap_reached` lists (the wire's bound).
IN_FLIGHT_LISTED: Final = 8
#: An attempt's deadline runs from its start (C-7: stricter than RAIL-27's "no
#: progress for 150 s"). Under Cloud Run's 300 s request timeout, over the
#: client's 120 s wait.
ATTEMPT_TTL_S: Final = 150
#: One provider call's own bound, at most the attempt's (C-7).
PROVIDER_CALL_MAX_S: Final = 120.0
#: A call that would have less than this left is refused before it starts, as a
#: timeout: it could not finish, and it would still be billed.
PROVIDER_CALL_MIN_S: Final = 5.0
#: The bound on context an executor assembles (the ac comment: "the context
#: bound applies to context the server assembles; the client sends none").
CONTEXT_MAX_CHARS: Final = 24_000
#: The only codes an executor's precheck may answer.
PRECHECK_CODES: Final = frozenset({ErrorCode.NOTHING_TO_RECAP})

# ── Fixed sentences (X-7, D-9): nothing a client sent, no model, no provider ─

UNAVAILABLE_MESSAGE: Final = "GM tools are briefly unavailable. Try again."
TOOL_DISABLED_MESSAGE: Final = "That tool isn't available yet."
NOTHING_TO_RECAP_MESSAGE: Final = "Nothing to recap yet."
CAP_REACHED_MESSAGE: Final = "Two tools are already running."
#: The cap's sentence when an AI edit holds a slot (`1kg.5.5`, C-15): "two
#: tools" would be false. Placeholder copy the design lane (`cub`) may reword.
EDIT_CAP_REACHED_MESSAGE: Final = "Two assistant tasks are already running."
THROTTLED_DAILY_MESSAGE: Final = "The pilot's daily limit is spent. It resets overnight."
THROTTLED_USER_MESSAGE: Final = "That's a lot at once. Try again shortly."
PROVIDER_TIMEOUT_MESSAGE: Final = "The model took too long to answer."
PROVIDER_FAILED_MESSAGE: Final = "The assistant couldn't finish that. Try again."
PROVIDER_FINAL_MESSAGE: Final = "The assistant couldn't do that one. Edit the brief and try again."
ATTEMPT_EXPIRED_MESSAGE: Final = "That took too long and was stopped. Try again."
#: The product's one refusal sentence, `/chat`'s Rules mode's own (1kg.4.3 I-7).
NOT_IN_SOURCES_MESSAGE: Final = REFUSAL
_PRECHECK_MESSAGES: Final = {ErrorCode.NOTHING_TO_RECAP: NOTHING_TO_RECAP_MESSAGE}

#: `/chat`'s provider-error categories (`service.app.normalize_llm_error`), in
#: its order: `APITimeoutError` subclasses `APIConnectionError`, so it comes
#: first. This module cannot import `service.app` (which imports it through the
#: route module); a test asserts the two classifications agree (I4).
_CATEGORIES: Final[tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str], ...]] = (
    (openai.RateLimitError, "rate_limit"),
    (openai.ContentFilterFinishReasonError, "content_filter"),
    ((openai.BadRequestError, openai.UnprocessableEntityError), "invalid_request"),
    (openai.AuthenticationError, "authentication"),
    (openai.PermissionDeniedError, "quota"),
    (openai.APITimeoutError, "timeout"),
    ((openai.APIConnectionError, openai.InternalServerError), "upstream_unavailable"),
)
#: C-9: a stored provider failure is final only when `/chat` answers its
#: category 422 — the request itself cannot succeed. Every other category,
#: `rate_limit` included, is retryable (RAIL-18).
FINAL_CATEGORIES: Final = frozenset({"content_filter", "invalid_request"})

# ── Deployment switches (I-7) ────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolSettings:
    """Which tools the deployment names and which capabilities it switched on.
    Both empty by default: production runs nothing."""

    enabled_tools: frozenset[ToolId] = frozenset()
    capabilities: frozenset[str] = frozenset()


def _names(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_settings(enabled_tools: str, capabilities: str) -> ToolSettings:
    """The two variables, or a `ValueError` naming the VARIABLE at fault — a
    revision that fails to start leaves traffic on the one before it. Empty and
    whitespace mean none."""
    tools, switched = _names(enabled_tools), _names(capabilities)
    if any(name not in {tool.value for tool in ToolId} for name in tools):
        raise ValueError("WORKBENCH_ENABLED_TOOLS names a tool the registry does not have")
    if any(REGISTRY.capability(name) is None for name in switched):
        raise ValueError("WORKBENCH_CAPABILITIES names a capability the registry does not have")
    return ToolSettings(frozenset(ToolId(name) for name in tools), frozenset(switched))


# ── The executor seam ────────────────────────────────────────────────────────


class InvocationCancelled(Exception):
    """The attempt was cancelled, or is no longer this attempt's to finish
    (expired or superseded): `ExecutionContext.check_cancelled` raised it."""


class ProviderNotAllowed(Exception):
    """A client for an alias off the Workbench provider allowlist (SEC-39),
    refused before any client is built."""


class OutOfTime(Exception):
    """A provider call refused before it started: the attempt's deadline leaves
    less than `PROVIDER_CALL_MIN_S`. Stored as `provider_timeout` (C-7)."""


class OutputRefused(Exception):
    """The provider answered, and the answer cannot become a result (1kg.4.4 I-3;
    the 1kg.5.4 Critic's C-7). Stored as `provider_failed`, retryable — the answer
    `judge_result`'s refusal gets. Raised by an executor's `run` only; it carries
    no message of its own."""


class NotInSources(Exception):
    """The corpus does not ground the request (1kg.4.3 I-7). Stored as
    `not_in_sources`, final: the GM edits the brief (RAIL-19). Raised by an
    executor's `run` only; it carries no message of its own."""


class ContextReader(Protocol):
    """Runs one read in a transaction of its own (1kg.4.4 I-4)."""

    def __call__[T](self, read: Callable[[UnitOfWork], T], /) -> T: ...  # pragma: no cover - structural type


def context_reader(db: TransactionalDatabase) -> ContextReader:
    """What `ExecutionContext.read` uses: one transaction per read, closed
    before the answer is handed back. Lock-free reads only; nothing is held
    across the provider call that follows."""

    def read_once[T](read: Callable[[UnitOfWork], T], /) -> T:
        with db.transaction() as unit:
            return read(unit)

    return read_once


@dataclass(frozen=True)
class InvocationTarget:
    """What an executor's precheck and run see. For a retry it is the STORED
    request, whatever the repeat carried (C-1)."""

    owner_id: int
    campaign_id: str
    conversation_id: str
    tool_id: ToolId
    brief: str = field(repr=False)
    source_entry_id: str | None


@dataclass(frozen=True)
class Admission:
    """T1 started an attempt: everything `execute` and `complete` need."""

    target: InvocationTarget
    invocation_id: str = field(repr=False)
    entry_id: str
    created_at: datetime
    attempt: int
    operation_id: str
    deadline: datetime
    model_alias: str = field(repr=False)


@dataclass(frozen=True)
class Replay:
    """T1 started nothing: the stored invocation answers the repeat."""

    invocation: ToolInvocation


type Clock = Callable[[], datetime]
#: What a produced result may be: the contract's own model, or its JSON shape.
#: justification: that shape is bare JSON, validated by `judge_result`.
type Produced = CardResult | DocumentResult | MediaResult | Mapping[str, Any]


class ToolExecutor(Protocol):
    """One tool's work. `1kg.4.3`, `1kg.4.4` and `1kg.8.3` add implementations;
    this bead registers none."""

    @property
    def tool_id(self) -> ToolId: ...  # pragma: no cover - structural type

    def precheck(self, unit: UnitOfWork, target: InvocationTarget) -> ErrorCode | None:
        """Inside T1, before any row is written, after the GM's in-flight lock:
        lock-free reads only — T1 can never take a campaign lock (C-3). May
        answer only a code in `PRECHECK_CODES`; `None` admits."""
        ...  # pragma: no cover - structural type

    def run(self, ctx: ExecutionContext) -> Produced:
        """The provider work. NO unit of work is open. Every provider client
        comes from `ctx.client()`; call `ctx.check_cancelled()` between provider
        calls. May raise `InvocationCancelled`. May raise `OutputRefused` or
        `NotInSources`; any other exception is an outage."""
        ...  # pragma: no cover - structural type

    def finish(self, unit: UnitOfWork, ctx: ExecutionContext, result: ToolResult) -> Produced:
        """Inside the fenced completion transaction; no network. `1kg.4.4`
        creates its document here, so it exists exactly once whatever retries."""
        ...  # pragma: no cover - structural type


class _BoundedClient:
    """Every provider call an executor makes, bounded by its attempt (C-7): a
    request timeout of `min(PROVIDER_CALL_MAX_S, remaining)`, and no call at
    all with less than `PROVIDER_CALL_MIN_S` left."""

    def __init__(self, inner: LLMClient, remaining_s: Callable[[], float]) -> None:
        self._inner = inner
        self._remaining_s = remaining_s

    # justification: `LLMClient.invoke`'s own signature, forwarded unchanged.
    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        left = self._remaining_s()
        if left < PROVIDER_CALL_MIN_S:
            raise OutOfTime()
        return self._inner.invoke(input, config=config, **{**kwargs, "timeout": min(PROVIDER_CALL_MAX_S, left)})


def bounded_client(factory: ProviderClientFactory, alias: str, remaining_s: Callable[[], float]) -> LLMClient:
    """The ONE builder of a Workbench provider client (SEC-39, `1kg.5.5` I-20):
    `alias` re-checked against the Workbench allowlist before anything is
    built, and every call bounded by `remaining_s` (C-7).

    **Call it only with an admitted attempt's alias and deadline, inside the
    `usage_capture` operation that attempt began** (C-24). A client built any
    other way reaches a provider with no admission, no attempt row, no X-5
    count and no ledger operation. `service/tests/test_bounded_client_callers.py`
    pins every call site."""
    if workbench_profile(alias) is None:
        raise ProviderNotAllowed()
    return _BoundedClient(factory.client_for(alias), remaining_s)


class ExecutionContext:
    """What `run` and `finish` are handed."""

    def __init__(
        self, admission: Admission, *, clock: Clock, factory: ProviderClientFactory,
        probe: Callable[[], None], reader: ContextReader | None = None,
    ) -> None:
        self.target = admission.target
        self.attempt = admission.attempt
        self.operation_id = admission.operation_id
        self.deadline = admission.deadline
        self.model_alias = admission.model_alias
        self.context_max_chars = CONTEXT_MAX_CHARS
        self._clock = clock
        self._factory = factory
        self._probe = probe
        self._reader = reader

    def __repr__(self) -> str:
        return f"ExecutionContext(tool={self.target.tool_id.value}, attempt={self.attempt})"

    def client(self) -> LLMClient:
        """The one path from an executor to a provider: the admission's model,
        re-checked against the Workbench allowlist before anything is built."""
        return bounded_client(self._factory, self.model_alias, self.remaining_s)

    # justification: LangChain's `RunnableConfig` is a JSON-shaped dict of mixed values.
    def run_config(self) -> dict[str, Any]:
        """The ledger's operation, and nothing else: no tracing callback and no
        trace metadata ever rides on a Workbench model call (SEC-24)."""
        return usage_capture.run_config_with_operation({}) or {}

    def check_cancelled(self) -> None:
        """One short read. Raises `InvocationCancelled` when a cancel was asked
        for, or when the row is no longer this attempt's `working` before its
        deadline — an expired or superseded attempt stops spending (C-7)."""
        self._probe()

    def remaining_s(self) -> float:
        return (self.deadline - self._clock()).total_seconds()

    def read[T](self, read: Callable[[UnitOfWork], T]) -> T:
        """`read(unit)` in one short transaction of its own, closed before this
        returns (RQ-8). A context built without a reader is a server defect:
        `RuntimeError`, stored as `backend_unavailable`."""
        if self._reader is None:
            raise RuntimeError("this context has no reader")
        return self._reader(read)

    def now(self) -> datetime:
        """The route's clock: the service's one clock."""
        return self._clock()


def cancellation_probe(
    db: TransactionalDatabase, stores: InvocationStores, admission: Admission, clock: Clock,
) -> Callable[[], None]:
    """`check_cancelled`'s read: the fence, in a transaction of its own that
    holds nothing across the provider call."""
    target = admission.target

    def probe() -> None:
        with db.transaction() as unit:
            row = stores.invocations.fence(
                unit, target.owner_id, target.campaign_id, admission.invocation_id,
                attempt=admission.attempt, now=clock(),
            )
        if row is None or row.cancel_requested:
            raise InvocationCancelled()

    return probe


# ── What the service composes ────────────────────────────────────────────────


class ToolTimeline(TimelineStore, ToolTurnStore, Protocol):
    """The timeline as a tool invocation uses it: `append` for the turn, and
    `has_entry` / `replace_tool_invocation` beside it."""


@dataclass(frozen=True)
class InvocationStores:
    invocations: ToolInvocationStore
    campaigns: CampaignStore
    conversations: ConversationStore
    timeline: ToolTimeline
    #: The X-5 count and the pilot day, across tools AND AI edits (I-3). A
    #: required field: a defaulted one would be a count this route could skip.
    load: WorkbenchLoad


# ── Refusals the routes turn into answers ────────────────────────────────────


class NotFound(Exception):
    """Missing, someone else's, deleted, or not in the campaign named: the one
    `not_found()` (SEC-3)."""


class Unreadable(Exception):
    """A stored invocation this build cannot read (I-25): a later schema
    version, or a payload that no longer validates. A 503; never overwritten."""


class ExecutorFailed(Exception):
    """An executor broke its contract: `finish` raised or answered a result
    that does not validate, or `precheck` answered a code it may not. The
    transaction rolls back and the answer is a 503; a row left `working`
    expires."""


class Refused(Exception):
    """A refusal before any attempt starts (I-4): nothing is created."""

    def __init__(
        self, status: int, code: ErrorCode, message: str, *, retryable: bool = False,
        retry_after_s: int | None = None, in_flight: list[str] | None = None,
    ) -> None:
        super().__init__(code.value)
        self.status = status
        self.info = ErrorInfo(
            code=code, message=message, retryable=retryable, retry_after_s=retry_after_s, in_flight=in_flight,
        )


class _ReplayInstead(Exception):
    """A write found the key taken: roll T1 back and answer the stored row."""

    def __init__(self, invocation: ToolInvocation) -> None:
        super().__init__("replay")
        self.invocation = invocation


# ── Availability and the model ───────────────────────────────────────────────


def tool_availability(
    tool_id: ToolId, settings: ToolSettings, executors: Mapping[ToolId, ToolExecutor],
) -> str | None:
    """`None` when the tool may run; otherwise the fixed sentence of its
    `409 tool_disabled`. The ONE place availability is decided (C-12)."""
    known = REGISTRY.availability({name: True for name in settings.capabilities}).get(tool_id)
    if known is None:
        return TOOL_DISABLED_MESSAGE
    if not known.enabled:
        return known.reason or TOOL_DISABLED_MESSAGE
    if tool_id not in settings.enabled_tools or tool_id not in executors:
        return TOOL_DISABLED_MESSAGE
    return None


def resolve_tool_model(session: SessionData) -> str:
    """The alias every attempt of this GM's uses. The single place bead `iov`
    replaces with the tier mapping (D-8); nothing the client sent reaches it —
    a request cannot carry a model (`extra="forbid"`). Today every GM gets the
    default, because no tier exists yet."""
    return DEFAULT_ALIAS


# ── The wire ─────────────────────────────────────────────────────────────────


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def to_wire(row: InvocationRow) -> ToolInvocation:
    """The ONE builder of an invocation's answer, which the routes and the
    timeline entry both use, so a status read and the entry cannot disagree."""
    if row.schema_version != CONTRACT_VERSION:
        raise Unreadable()
    try:
        return ToolInvocation.model_validate({
            "schema_version": CONTRACT_VERSION, "invocation_id": row.invocation_id, "tool_id": row.tool_id,
            "status": row.status, "attempt": row.attempt, "cancel_requested": row.cancel_requested,
            "created_at": _utc(row.created_at), "updated_at": _utc(row.updated_at),
            "result": row.result, "error": row.error,
        })
    except ValidationError:
        pass
    raise Unreadable()


# justification: the entry is the JSON the timeline stores; `to_wire` typed it first.
def tool_entry(row: InvocationRow) -> dict[str, Any]:
    """The timeline entry that carries `row`, around `to_wire(row)`."""
    return {
        "schema_version": CONTRACT_VERSION, "entry_kind": "tool", "entry_id": row.entry_id,
        "created_at": _utc(row.created_at), "brief": row.brief, "source_entry_id": row.source_entry_id,
        # By alias (I-28): the stored JSON is what a sender forwards, so a stat
        # block's ability is `int`, the wire's spelling, never `int_`.
        "invocation": to_wire(row).model_dump(mode="json", by_alias=True),
    }


# justification: the error as the JSON column stores it; `ErrorInfo` typed it first.
def _stored(info: ErrorInfo) -> dict[str, Any]:
    return info.model_dump(mode="json", exclude_none=True)


# ── Outcomes ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    """What `run` came to: a judged result, a stored error, or a cancel."""

    result: CardResult | DocumentResult | MediaResult | None = field(default=None, repr=False)
    error: ErrorInfo | None = None
    cancelled: bool = False


def provider_category(exc: BaseException) -> str:
    """`/chat`'s category for an exception, `unknown` for anything unnamed."""
    for kinds, category in _CATEGORIES:
        if isinstance(exc, kinds):
            return category
    return "unknown"


def failure_for(exc: BaseException) -> ErrorInfo:
    """The stored error of a failed attempt (I-22, C-9). A provider timeout —
    or a call refused for want of time — is `provider_timeout`; any other
    provider error is `provider_failed`, final only for a 422 category. An
    executor's `OutputRefused` is `provider_failed`, retryable, and its
    `NotInSources` is `not_in_sources`, final. A database or embedding outage,
    and anything unexpected, is `backend_unavailable`. All retryable except the
    final provider failures and `not_in_sources`."""
    if isinstance(exc, OutOfTime):
        return ErrorInfo(code=ErrorCode.PROVIDER_TIMEOUT, message=PROVIDER_TIMEOUT_MESSAGE, retryable=True)
    # The markers come before the provider branch: an executor's own verdict on
    # an answer it received wins over any class the exception also has.
    if isinstance(exc, OutputRefused):
        return ErrorInfo(code=ErrorCode.PROVIDER_FAILED, message=PROVIDER_FAILED_MESSAGE, retryable=True)
    if isinstance(exc, NotInSources):
        return ErrorInfo(code=ErrorCode.NOT_IN_SOURCES, message=NOT_IN_SOURCES_MESSAGE, retryable=False)
    if isinstance(exc, openai.OpenAIError):
        category = provider_category(exc)
        if category == "timeout":
            return ErrorInfo(code=ErrorCode.PROVIDER_TIMEOUT, message=PROVIDER_TIMEOUT_MESSAGE, retryable=True)
        final = category in FINAL_CATEGORIES
        return ErrorInfo(
            code=ErrorCode.PROVIDER_FAILED, message=PROVIDER_FINAL_MESSAGE if final else PROVIDER_FAILED_MESSAGE,
            retryable=not final,
        )
    return ErrorInfo(code=ErrorCode.BACKEND_UNAVAILABLE, message=UNAVAILABLE_MESSAGE, retryable=True)


_RESULT: TypeAdapter[CardResult | DocumentResult | MediaResult] = TypeAdapter(ToolResult)


def _suggests_an_available_tool(suggestion: object, available: Callable[[ToolId], bool]) -> bool:
    name = suggestion.get("tool_id") if isinstance(suggestion, Mapping) else None
    return name in {tool.value for tool in ToolId} and available(ToolId(name))


def judge_result(
    produced: object, tool_id: ToolId, available: Callable[[ToolId], bool],
) -> CardResult | DocumentResult | MediaResult | None:
    """A produced result, re-validated through the contract, or `None` (I-21).
    Suggestions naming a tool that may not run now are dropped first (RAIL-8,
    SEC-32). A refusal logs the error locations only, never a value."""
    raw = produced.model_dump(mode="json") if isinstance(produced, BaseModel) else produced
    if isinstance(raw, Mapping) and isinstance(raw.get("suggestions"), list):
        raw = {**raw, "suggestions": [s for s in raw["suggestions"] if _suggests_an_available_tool(s, available)]}
    try:
        result = _RESULT.validate_python(raw)
    except ValidationError as exc:
        where = [error["loc"] for error in redacted_errors(exc.errors())]
        log.warning("tool result refused (tool=%s, at=%s)", tool_id.value, where)
        return None
    if result.tool_id is not tool_id:
        log.warning("tool result refused (tool=%s, at=%s)", tool_id.value, [["tool_id"]])
        return None
    return result


def execute(
    executor: ToolExecutor, ctx: ExecutionContext, *, available: Callable[[ToolId], bool],
) -> Outcome:
    """Run the attempt. No unit of work is open here, and nothing raises out:
    every failure becomes the outcome it is stored as."""
    try:
        produced = executor.run(ctx)
    except InvocationCancelled:
        return Outcome(cancelled=True)
    except Exception as exc:
        info = failure_for(exc)
        log.warning(
            "tool attempt failed (operation_id=%s, tool=%s, attempt=%d, code=%s, error=%s)",
            ctx.operation_id, ctx.target.tool_id.value, ctx.attempt, info.code.value, type(exc).__name__,
        )
        return Outcome(error=info)
    judged = judge_result(produced, ctx.target.tool_id, available)
    if judged is None:
        return Outcome(error=ErrorInfo(
            code=ErrorCode.PROVIDER_FAILED, message=PROVIDER_FAILED_MESSAGE, retryable=True,
        ))
    return Outcome(result=judged)


# ── Transitions every path shares ────────────────────────────────────────────


def _readable(row: InvocationRow) -> InvocationRow:
    to_wire(row)
    return row


def _rewrite_entry(unit: UnitOfWork, stores: InvocationStores, row: InvocationRow) -> None:
    stores.timeline.replace_tool_invocation(
        unit, row.conversation_id, tool_entry(row), row.created_at, owner_id=row.owner_id,
    )


# justification: `result` and `error` are JSON column values, typed before they get here.
def _settle(
    unit: UnitOfWork, stores: InvocationStores, row: InvocationRow, *, status: str, outcome: str,
    result: Mapping[str, Any] | None = None, error: Mapping[str, Any] | None = None, now: datetime,
) -> InvocationRow:
    """A terminal state, its attempt row and its entry, in one transaction."""
    settled = stores.invocations.settle(
        unit, row.owner_id, row.campaign_id, row.invocation_id, attempt=row.attempt, status=status,
        outcome=outcome, result=result, error=error, now=now,
    )
    _rewrite_entry(unit, stores, settled)
    return settled


def _expire_if_due(unit: UnitOfWork, stores: InvocationStores, row: InvocationRow, now: datetime) -> InvocationRow:
    """I-13: a `working` row past its deadline ends here — `cancelled` if a
    cancel was asked for, else `failed attempt_expired`, retryable below the
    last attempt. The attempt row says `expired` either way."""
    if row.status != "working" or row.attempt_deadline > now:
        return row
    if row.cancel_requested:
        return _settle(unit, stores, row, status="cancelled", outcome="expired", now=now)
    expired = ErrorInfo(
        code=ErrorCode.ATTEMPT_EXPIRED, message=ATTEMPT_EXPIRED_MESSAGE, retryable=row.attempt < ATTEMPT_MAX,
    )
    return _settle(unit, stores, row, status="failed", outcome="expired", error=_stored(expired), now=now)


def _retryable(row: InvocationRow) -> bool:
    return (
        row.status == "failed" and isinstance(row.error, Mapping) and row.error.get("retryable") is True
        and row.attempt < ATTEMPT_MAX
    )


def _target_of(row: InvocationRow) -> InvocationTarget:
    return InvocationTarget(
        row.owner_id, row.campaign_id, row.conversation_id, ToolId(row.tool_id), row.brief, row.source_entry_id,
    )


# ── T1: submit ───────────────────────────────────────────────────────────────


def admit_attempt(
    unit: UnitOfWork, load: WorkbenchLoad, owner_id: int, *, now: datetime, chat_turns_today: int,
) -> None:
    """The admission seam (§5.8): the X-5 cap, the pilot day, the per-user
    window, in that order — for a tool and an AI edit alike (`1kg.5.5`, I-3),
    both counted through `load`. `yje.5.2`'s reservation and `0o2`'s limit
    states plug in here. The window is last because it is the only guard that
    spends: no request refused by another guard costs a token (I-8). The cap's
    sentence says what holds it (C-15): "two tools" only when both slots are
    tools."""
    running = load.in_flight(unit, owner_id, now=now)
    if len(running) >= IN_FLIGHT_CAP:
        message = CAP_REACHED_MESSAGE if all(kind == "tool" for _, kind in running) else EDIT_CAP_REACHED_MESSAGE
        raise Refused(409, ErrorCode.CAP_REACHED, message, retryable=True,
                      in_flight=[invocation_id for invocation_id, _ in running][:IN_FLIGHT_LISTED])
    midnight = _utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if chat_turns_today + load.attempts_since(unit, midnight) >= config.CHAT_DAILY_CAP:
        raise Refused(429, ErrorCode.THROTTLED_DAILY, THROTTLED_DAILY_MESSAGE)
    wait: int | None = None
    try:
        ratelimit.check_chat_request(owner_id)
    except ratelimit.RateLimited as exc:
        wait = exc.retry_after
    if wait is not None:
        raise Refused(429, ErrorCode.THROTTLED_USER, THROTTLED_USER_MESSAGE, retryable=True, retry_after_s=wait)


def _guards(
    unit: UnitOfWork, stores: InvocationStores, executors: Mapping[ToolId, ToolExecutor],
    settings: ToolSettings, session: SessionData, campaign: Campaign, target: InvocationTarget, *,
    now: datetime, chat_turns_today: int,
) -> str:
    """I-8's order, after ownership: archived, enabled, the model, the
    executor's precheck, then the admission seam. Answers the model alias."""
    if campaign.is_archived:
        raise Refused(409, ErrorCode.CAMPAIGN_ARCHIVED, ARCHIVED_MESSAGE)
    reason = tool_availability(target.tool_id, settings, executors)
    if reason is not None:
        raise Refused(409, ErrorCode.TOOL_DISABLED, reason)
    alias = resolve_tool_model(session)
    if workbench_profile(alias) is None:
        log.warning("tool refused: its model is off the Workbench allowlist (tool=%s)", target.tool_id.value)
        raise Refused(409, ErrorCode.TOOL_DISABLED, TOOL_DISABLED_MESSAGE)
    code = executors[target.tool_id].precheck(unit, target)
    if code is not None:
        if code not in PRECHECK_CODES:
            raise ExecutorFailed()
        raise Refused(409, code, _PRECHECK_MESSAGES[code])
    admit_attempt(unit, stores.load, target.owner_id, now=now, chat_turns_today=chat_turns_today)
    return alias


def _refused_create(
    unit: UnitOfWork, stores: InvocationStores, target: InvocationTarget, invocation_id: str,
) -> NoReturn:
    """C-4: the creating statement wrote nothing. A row under the key means the
    replay path; otherwise the campaign, read again with its owner: archived is
    the 409, anything else the one 404. Raised inside T1, so the entry appended
    before the statement rolls back with it."""
    found = stores.invocations.get_for_update(unit, target.owner_id, target.campaign_id, invocation_id)
    if found is not None:
        raise _ReplayInstead(to_wire(found))
    _refused_by_campaign(unit, stores, target)


def _refused_by_campaign(unit: UnitOfWork, stores: InvocationStores, target: InvocationTarget) -> NoReturn:
    campaign = stores.campaigns.get(unit, target.campaign_id, owner_id=target.owner_id)
    if campaign is not None and campaign.is_archived:
        raise Refused(409, ErrorCode.CAMPAIGN_ARCHIVED, ARCHIVED_MESSAGE)
    raise NotFound()


def _create(
    unit: UnitOfWork, stores: InvocationStores, target: InvocationTarget, invocation_id: str, *,
    operation_id: str, deadline: datetime, now: datetime,
) -> InvocationRow:
    planned = InvocationRow(
        owner_id=target.owner_id, campaign_id=target.campaign_id, invocation_id=invocation_id,
        conversation_id=target.conversation_id, entry_id=new_entry_id(), tool_id=target.tool_id.value,
        brief=target.brief, source_entry_id=target.source_entry_id, status="working", attempt=1,
        attempt_deadline=deadline, cancel_requested=False, result=None, error=None,
        schema_version=CONTRACT_VERSION, created_at=now, updated_at=now,
    )
    try:
        stores.timeline.append(unit, target.conversation_id, tool_entry(planned), now, owner_id=target.owner_id)
        return stores.invocations.create(
            unit, owner_id=target.owner_id, campaign_id=target.campaign_id, invocation_id=invocation_id,
            conversation_id=target.conversation_id, entry_id=planned.entry_id, tool_id=planned.tool_id,
            brief=target.brief, source_entry_id=target.source_entry_id, operation_id=operation_id,
            attempt_deadline=deadline, now=now, schema_version=CONTRACT_VERSION,
        )
    except (InvocationNotStored, EntryNotStored):
        pass
    _refused_create(unit, stores, target, invocation_id)


def _retry(
    unit: UnitOfWork, stores: InvocationStores, existing: InvocationRow, *,
    operation_id: str, deadline: datetime, now: datetime,
) -> InvocationRow:
    try:
        row = stores.invocations.start_retry(
            unit, owner_id=existing.owner_id, campaign_id=existing.campaign_id,
            invocation_id=existing.invocation_id, attempt=existing.attempt, operation_id=operation_id,
            attempt_deadline=deadline, now=now,
        )
    except InvocationNotStored:
        _refused_by_campaign(unit, stores, _target_of(existing))
    _rewrite_entry(unit, stores, row)
    return row


def _admit(
    unit: UnitOfWork, stores: InvocationStores, executors: Mapping[ToolId, ToolExecutor],
    settings: ToolSettings, session: SessionData, request: ToolInvocationRequest, *,
    now: datetime, chat_turns_today: int,
) -> Admission | Replay:
    owner = session.user_id
    stores.invocations.hold_in_flight_lock(unit, owner)
    # I-18: every lookup runs, then one decision — any miss is the one 404.
    campaign = stores.campaigns.get(unit, request.campaign_id, owner_id=owner)
    linked = stores.conversations.campaign_for_owner(unit, request.conversation_id, owner_id=owner)
    source_known = request.source_entry_id is None or stores.timeline.has_entry(
        unit, request.conversation_id, request.source_entry_id,
    )
    if campaign is None or linked != request.campaign_id or not source_known:
        raise NotFound()
    existing = stores.invocations.get_for_update(unit, owner, request.campaign_id, request.invocation_id)
    if existing is not None:
        existing = _expire_if_due(unit, stores, _readable(existing), now)
        if not _retryable(existing):
            return Replay(to_wire(existing))
        target = _target_of(existing)
    else:
        target = InvocationTarget(
            owner, request.campaign_id, request.conversation_id, request.tool_id, request.brief,
            request.source_entry_id,
        )
    alias = _guards(unit, stores, executors, settings, session, campaign, target, now=now,
                    chat_turns_today=chat_turns_today)
    operation_id = uuid.uuid4().hex
    deadline = now + timedelta(seconds=ATTEMPT_TTL_S)
    if existing is None:
        row = _create(unit, stores, target, request.invocation_id, operation_id=operation_id, deadline=deadline,
                      now=now)
    else:
        row = _retry(unit, stores, existing, operation_id=operation_id, deadline=deadline, now=now)
    return Admission(target, row.invocation_id, row.entry_id, row.created_at, row.attempt, operation_id,
                     deadline, alias)


def submit(
    db: TransactionalDatabase, stores: InvocationStores, executors: Mapping[ToolId, ToolExecutor],
    settings: ToolSettings, session: SessionData, request: ToolInvocationRequest, *,
    now: datetime, chat_turns_today: int,
) -> Admission | Replay:
    """T1. `chat_turns_today` is read by the caller BEFORE this opens, so one
    request never holds two connections (§5.3 step 7)."""
    try:
        with db.transaction() as unit:
            return _admit(unit, stores, executors, settings, session, request, now=now,
                          chat_turns_today=chat_turns_today)
    except _ReplayInstead as replay:
        return Replay(replay.invocation)


# ── T2: complete ─────────────────────────────────────────────────────────────


# justification: the answer is a validated result's JSON dump, the `result` column's value.
def _finished(
    unit: UnitOfWork, executor: ToolExecutor, ctx: ExecutionContext,
    result: CardResult | DocumentResult | MediaResult,
) -> dict[str, Any]:
    try:
        finished = executor.finish(unit, ctx, result)
    except Exception as exc:
        log.warning("tool finish failed (operation_id=%s, tool=%s, error=%s)",
                    ctx.operation_id, ctx.target.tool_id.value, type(exc).__name__)
        raise ExecutorFailed() from None
    judged = judge_result(finished, ctx.target.tool_id, lambda _tool: True)
    if judged is None:
        raise ExecutorFailed()
    return judged.model_dump(mode="json", by_alias=True)


def complete(
    db: TransactionalDatabase, stores: InvocationStores, executor: ToolExecutor, admission: Admission,
    outcome: Outcome, ctx: ExecutionContext, *, now: datetime,
) -> ToolInvocation:
    """T2. The fence first (I-14, C-6): an outcome lands only on a row that is
    still `working`, still on this attempt, and before its deadline — a done, a
    failed and a cancelled outcome alike. After a failed fence the answer is the
    row as T2 found it, past-deadline transition applied; a row that is gone
    (its conversation or account deleted) is the one 404."""
    target = admission.target
    with db.transaction() as unit:
        row = stores.invocations.fence(
            unit, target.owner_id, target.campaign_id, admission.invocation_id, attempt=admission.attempt, now=now,
        )
        if row is None:
            current = stores.invocations.get_for_update(
                unit, target.owner_id, target.campaign_id, admission.invocation_id,
            )
            if current is None:
                raise NotFound()
            return to_wire(_expire_if_due(unit, stores, _readable(current), now))
        if outcome.result is not None:
            # RAIL-23: a result that arrives after a cancel is kept, flag and all.
            stored = _finished(unit, executor, ctx, outcome.result)
            settled = _settle(unit, stores, row, status="done", outcome="done", result=stored, now=now)
        elif outcome.cancelled or row.cancel_requested:
            settled = _settle(unit, stores, row, status="cancelled", outcome="cancelled", now=now)
        else:
            error = outcome.error or failure_for(RuntimeError())
            if row.attempt >= ATTEMPT_MAX:
                error = error.model_copy(update={"retryable": False})
            settled = _settle(unit, stores, row, status="failed", outcome="failed", error=_stored(error), now=now)
        return to_wire(settled)


# ── Status reads and cancel ──────────────────────────────────────────────────


def read(
    db: TransactionalDatabase, stores: InvocationStores, owner_id: int, campaign_id: str, invocation_id: str, *,
    now: datetime,
) -> ToolInvocation:
    """The GET: the caller's invocation, lazily expired, or the one 404."""
    with db.transaction() as unit:
        row = stores.invocations.get_for_update(unit, owner_id, campaign_id, invocation_id)
        if row is None:
            raise NotFound()
        return to_wire(_expire_if_due(unit, stores, _readable(row), now))


def cancel(
    db: TransactionalDatabase, stores: InvocationStores, owner_id: int, campaign_id: str, invocation_id: str, *,
    now: datetime,
) -> ToolInvocation:
    """I-15: a flag, never refused for state. On `working` it asks the attempt
    to stop; on `done` it means "finished before it could be cancelled", so a
    cancel and a completion end the same in either order. On `failed` or
    `cancelled` nothing changes. Never throttled, never counted."""
    with db.transaction() as unit:
        row = stores.invocations.get_for_update(unit, owner_id, campaign_id, invocation_id)
        if row is None:
            raise NotFound()
        row = _expire_if_due(unit, stores, _readable(row), now)
        if row.status in {"working", "done"} and not row.cancel_requested:
            flagged = stores.invocations.set_cancel_requested(unit, owner_id, campaign_id, invocation_id, now=now)
            if flagged is not None:
                _rewrite_entry(unit, stores, flagged)
                row = flagged
        return to_wire(row)

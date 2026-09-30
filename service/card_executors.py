"""The five card tools (agent-forge-harness-1kg.4.3): monster, loot, names,
rules and hooks. Each is a `tool_invocations.ToolExecutor`, so everything
around it — admission, the guards, the ledger's operation, the fence,
retries, cancel and expiry — is `1kg.4.1`'s, and the generation itself is
this bead's own `card_generation`. **No SQL, no client construction and no
route** lives here.

* `run` reads nothing and writes nothing (I-19): a card carries no aggregate,
  so exactly-once is trivial, and `precheck`/`finish` are no-ops. It generates
  with the one allowlisted client `ctx.client()` hands it (SEC-39), the
  ledger's config and nothing else (SEC-24), and as many attempts as fit.
* The rules tool additionally retrieves through a registered `RagLike`
  (I-20): the graph and `RagService.answer` are never called (R-10), so no
  trace attaches and the allowlist is never bypassed.
* A malformed answer is `card_generation.InvalidCardOutput`, converted here
  to `OutputRefused` — the executor's job, so the library stays free of
  `tool_invocations` for that one translation (§6.4). A rules corpus miss is
  this module's own `NotInSources`, raised directly (there is nothing to
  translate: the code and the finality are already right).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol

import config as settings
from ingestion.retrieval import _EMBED_RETRY_BACKOFF_S, EMBED_MAX_ATTEMPTS, TOP_K

from . import usage_capture
from .card_generation import (
    CARD_TOOLS,
    GENERATION_CONTEXT_MAX_CHARS,
    PURPOSE,
    RULES_PASSAGES,
    CardRequest,
    GeneratedCard,
    InvalidCardOutput,
    compose_result,
    context_size,
    generate_card,
)
from .db import UnitOfWork
from .document_generation import attempts_that_fit, passages_from_retrieval
from .tool_invocations import (
    PROVIDER_CALL_MIN_S,
    ExecutionContext,
    InvocationTarget,
    NotInSources,
    OutOfTime,
    OutputRefused,
    Produced,
    ToolExecutor,
)
from .workbench_contracts import ErrorCode, ToolId

log = logging.getLogger(__name__)

#: The rules gate's worst-case embedding time (mirrors `RagRetriever.embed`'s
#: own retry loop: up to `EMBED_MAX_ATTEMPTS` request+connect timeouts, plus
#: the backoff between them). A test pins this to the retriever's own private
#: backoff constant, so the two can never drift silently.
EMBED_WORST_S: Final = EMBED_MAX_ATTEMPTS * (settings.EMBED_REQUEST_TIMEOUT_S + settings.EMBED_CONNECT_TIMEOUT_S) + (
    _EMBED_RETRY_BACKOFF_S * EMBED_MAX_ATTEMPTS * (EMBED_MAX_ATTEMPTS - 1) / 2
)


class RetrieverLike(Protocol):
    def retrieve(self, prompt: str, k: int, reranker: Any, mode: str) -> Any: ...  # pragma: no cover - structural


class RagLike(Protocol):
    """What the rules tool needs of the live `RagService` (I-20): only its
    retriever and its reranker, never its factory, model or graph."""

    retriever: RetrieverLike
    reranker: Any  # justification: an optional local cross-encoder, passed through untouched


class RetrievalUnavailable(RuntimeError):
    """No `RagService` is registered yet: stored as `backend_unavailable`,
    retryable. Detected here in `run`, because `precheck` may answer only
    `nothing_to_recap` (1kg.4.1's `PRECHECK_CODES`)."""


_provider: Callable[[], RagLike | None] | None = None


def set_rag_provider(provider: Callable[[], RagLike | None] | None) -> None:
    """Registered once, in the app module beside `set_ledger_provider`
    (R-12): a module that needs something the app builds registers a
    provider, so route modules keep importing nothing from it."""
    global _provider
    _provider = provider


class CardExecutor:
    """One card tool whose only context is the GM's own brief (I-11): the
    monster, and the three other creative tools. `RulesExecutor` extends it
    with the corpus it alone needs."""

    def __init__(self, tool: ToolId) -> None:
        self._tool = tool

    def __repr__(self) -> str:
        return f"CardExecutor(tool={self._tool.value})"

    @property
    def tool_id(self) -> ToolId:
        return self._tool

    def precheck(self, unit: UnitOfWork, target: InvocationTarget) -> ErrorCode | None:
        """Nothing to check before admission: the brief is required, and T1
        already checked the campaign with its owner. Reads nothing (I-19)."""
        return None

    def run(self, ctx: ExecutionContext) -> Produced:
        return self._run(ctx, CardRequest(self._tool, ctx.target.brief))

    def finish(self, unit: UnitOfWork, ctx: ExecutionContext, result: Produced) -> Produced:
        """Writes nothing (I-19): there is no aggregate to create, so this
        attempt's result is already everything the invocation stores."""
        return result

    # ── Shared by every card tool ───────────────────────────────────────────

    def _run(self, ctx: ExecutionContext, request: CardRequest) -> Produced:
        attempts = attempts_that_fit(ctx.remaining_s())
        if attempts == 0:
            raise OutOfTime()
        generated = self._generate(ctx, request, attempts)
        return compose_result(generated)

    def _generate(self, ctx: ExecutionContext, request: CardRequest, attempts: int) -> GeneratedCard:
        """`InvalidCardOutput` is translated OUTSIDE the `except`, so the
        raised `OutputRefused` chains neither a cause nor a context — nothing
        a later message could leak through (document_tools.py's own pattern
        for the same translation)."""
        refused_code: str | None = None
        try:
            return generate_card(
                request, client=ctx.client(), alias=ctx.model_alias, config=ctx.run_config(),
                max_attempts=attempts, between_attempts=ctx.check_cancelled,
            )
        except InvalidCardOutput as exc:
            refused_code = exc.code.value
        log.warning("card tool output refused (operation_id=%s, tool=%s, code=%s)",
                    ctx.operation_id, self._tool.value, refused_code)
        raise OutputRefused()


class RulesExecutor(CardExecutor):
    """The rules tool: corpus-grounded, through the registered `RagLike`
    (I-6). Never calls `RagService.answer` or the graph (R-10)."""

    def __init__(self) -> None:
        super().__init__(ToolId.RULES)

    def run(self, ctx: ExecutionContext) -> Produced:
        rag = self._rag()
        if ctx.remaining_s() < EMBED_WORST_S + PROVIDER_CALL_MIN_S:
            raise OutOfTime()
        result = self._retrieve(ctx, rag)
        if not (result.answerable and result.chunks):
            usage_capture.record_structuring_outcome(
                ctx.run_config(), purpose=PURPOSE, outcome=usage_capture.OUTCOME_SKIPPED_BY_GATE,
            )
            raise NotInSources()
        ctx.check_cancelled()
        passages = self._bounded_passages(ctx, passages_from_retrieval(result, top_n=RULES_PASSAGES))
        return self._run(ctx, CardRequest(ToolId.RULES, ctx.target.brief, passages))

    @staticmethod
    def _rag() -> RagLike:
        rag = _provider() if _provider is not None else None
        if rag is None:
            raise RetrievalUnavailable()
        return rag

    @staticmethod
    def _retrieve(ctx: ExecutionContext, rag: RagLike) -> Any:
        """Inside the embedding scope, so every embed attempt is a ledger row
        under the tool operation (SEC-34)."""
        token = usage_capture.begin_embedding_scope(ctx.run_config())
        try:
            return rag.retriever.retrieve(ctx.target.brief, TOP_K, rag.reranker, "rules")
        finally:
            usage_capture.end_embedding_scope(token)

    @staticmethod
    def _bounded_passages(ctx: ExecutionContext, passages: tuple[Any, ...]) -> tuple[Any, ...]:
        """Drop the lowest-ranked passage while the serialized context would
        exceed the bound. Stops at 1: a single oversize passage is left for
        the provider call itself to refuse."""
        while len(passages) > 1:
            request = CardRequest(ToolId.RULES, ctx.target.brief, passages)
            if context_size(request) <= GENERATION_CONTEXT_MAX_CHARS:
                break
            passages = passages[:-1]
        return passages


def card_executors() -> Mapping[ToolId, ToolExecutor]:
    """Every card tool this module runs, keyed by tool — exactly
    `card_generation.CARD_TOOLS` (I-24: registered is not enabled).
    Stateless: building this connects nothing."""
    return {tool: RulesExecutor() if tool is ToolId.RULES else CardExecutor(tool) for tool in CARD_TOOLS}

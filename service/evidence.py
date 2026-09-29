"""
Typed retrieval-stage faults and the evidence-attempt record (agent-forge-harness-xiu.2.3).

A fault in a retrieval stage (embed, vector_search, fetch, rerank, secondary)
is raised as a `RetrievalStageError`, or, for the reranker, recorded as an
`EvidenceAttempt` while the vector order is kept. Either one carries the
stage, a bounded outcome and the cause's class name, never the cause's text:
the objects are content-free by construction (SEC-20 / X-7). A generation
(provider) error is never typed here; it keeps the D4 taxonomy.

Both objects are INTERNAL. Neither is serialized, persisted, sent on the wire
or used as a metric label. The public projection (the plan's `EvidenceAttempt`
and `evidence_mode`) belongs to agent-forge-harness-xiu.1.2. The field names
follow the plan's vocabulary (`source_kind`, with `local` for the primary
corpus); `rejected`, a request the provider refuses as invalid, is an outcome
the plan's list does not have and is kept apart because a retry cannot help it.

The degradable sets are deliberately narrow: a bug (TypeError, KeyError, a bare
RuntimeError) is never typed and stays a 500. An authorization-bound retriever
(agent-forge-harness-1ir.2.4's CampaignRetriever) must signal a denial with a
class OUTSIDE `SECONDARY_FAULTS`, never an OSError/PermissionError relabelled as
an outage; a database-grant or row-security denial
(`psycopg.errors.InsufficientPrivilege`) is in `SECONDARY_NEVER_TYPED` and
always fails the turn untyped.

`SHARED_INFRA_FAULTS` follows the bead's topology note: the connection gate
(one per instance, shared by every store) and the provider credential (shared
with generation) are not stage-local, so a fault in them is typed but must
never degrade to another evidence tier.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import openai
import psycopg
import psycopg.errors
import psycopg_pool

from ingestion.retrieval import EmbeddingUnavailableError

RetrievalStage = Literal["embed", "vector_search", "fetch", "rerank", "secondary"]
EvidenceSource = Literal["local", "secondary"]  # xiu.2.5 adds attachment/campaign; xiu.3 adds web
FaultOutcome = Literal["timeout", "unavailable", "rejected", "error"]

# The order a failure is reported in when several stages were lost: fixed,
# never arrival order (the GM fan-out runs search and secondary together).
STAGE_ORDER: tuple[RetrievalStage, ...] = ("embed", "vector_search", "fetch", "secondary", "rerank")
EVIDENCE_LOSING_STAGES: frozenset[RetrievalStage] = frozenset({"embed", "vector_search", "fetch", "secondary"})

EMBED_FAULTS: tuple[type[BaseException], ...] = (EmbeddingUnavailableError, openai.OpenAIError)
CORPUS_DB_FAULTS: tuple[type[BaseException], ...] = (psycopg.Error,)
SECONDARY_FAULTS: tuple[type[BaseException], ...] = (psycopg.Error, openai.OpenAIError, EmbeddingUnavailableError)
# rerank: any Exception (ranking only; it never loses evidence)

SECONDARY_NEVER_TYPED: tuple[type[BaseException], ...] = (psycopg.errors.InsufficientPrivilege,)

SHARED_INFRA_FAULTS: tuple[type[BaseException], ...] = (
    psycopg_pool.PoolTimeout,
    psycopg_pool.PoolClosed,
    EmbeddingUnavailableError,
    openai.AuthenticationError,
)


class RerankOrderInvalid(ValueError):
    """The reranker returned something other than a permutation of the texts."""


# Checked in this order: APITimeoutError subclasses APIConnectionError, and
# PoolTimeout and QueryCanceled subclass psycopg.OperationalError.
_TIMEOUT: tuple[type[BaseException], ...] = (
    openai.APITimeoutError, psycopg_pool.PoolTimeout, psycopg.errors.QueryCanceled, TimeoutError,
)
_REJECTED: tuple[type[BaseException], ...] = (openai.BadRequestError, openai.UnprocessableEntityError)
_UNAVAILABLE: tuple[type[BaseException], ...] = (
    EmbeddingUnavailableError, openai.APIConnectionError, openai.InternalServerError, openai.RateLimitError,
    openai.AuthenticationError, openai.PermissionDeniedError, psycopg.OperationalError,
)


def classify_fault(exc: BaseException) -> FaultOutcome:
    if isinstance(exc, _TIMEOUT):
        return "timeout"
    if isinstance(exc, _REJECTED):
        return "rejected"
    if isinstance(exc, _UNAVAILABLE):
        return "unavailable"
    return "error"


@dataclass(frozen=True)
class EvidenceAttempt:
    """One lost or degraded retrieval stage of a turn. Faults only: success
    outcomes (hit / weak / empty) belong to agent-forge-harness-xiu.2.2."""

    source_kind: EvidenceSource
    stage: RetrievalStage
    outcome: FaultOutcome
    error_class: str  # type(cause).__name__, never str(cause)

    @property
    def loses_evidence(self) -> bool:
        return self.stage in EVIDENCE_LOSING_STAGES


def _source_kind(stage: RetrievalStage) -> EvidenceSource:
    return "secondary" if stage == "secondary" else "local"


def attempt_for_fault(stage: RetrievalStage, exc: BaseException) -> EvidenceAttempt:
    return EvidenceAttempt(_source_kind(stage), stage, classify_fault(exc), type(exc).__name__)


class RetrievalStageError(Exception):
    """A retrieval stage failed and the turn cannot continue. Its message and
    `args` carry the stage, the outcome and the cause's class only. `attempts`
    is every attempt of the turn when the gate raises it (empty when a stage
    raises it directly); it stays out of the message and `args`."""

    def __init__(
        self, stage: RetrievalStage, outcome: FaultOutcome, error_class: str,
        attempts: tuple[EvidenceAttempt, ...] = (),
    ) -> None:
        super().__init__(f"retrieval stage {stage} failed: {outcome} ({error_class})")
        self.stage: RetrievalStage = stage
        self.outcome: FaultOutcome = outcome
        self.error_class = error_class
        self.attempts = attempts

    @classmethod
    def from_fault(cls, stage: RetrievalStage, exc: BaseException) -> RetrievalStageError:
        return cls.from_attempt(attempt_for_fault(stage, exc))

    @classmethod
    def from_attempt(
        cls, attempt: EvidenceAttempt, attempts: tuple[EvidenceAttempt, ...] = (),
    ) -> RetrievalStageError:
        return cls(attempt.stage, attempt.outcome, attempt.error_class, attempts)

    def attempt(self) -> EvidenceAttempt:
        return EvidenceAttempt(_source_kind(self.stage), self.stage, self.outcome, self.error_class)


def first_lost_evidence(attempts: Iterable[EvidenceAttempt]) -> EvidenceAttempt | None:
    """The evidence-losing attempt earliest in STAGE_ORDER, or None."""
    lost = [a for a in attempts if a.loses_evidence]
    return min(lost, key=lambda a: STAGE_ORDER.index(a.stage)) if lost else None

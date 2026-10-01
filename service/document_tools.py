"""The GM tools whose result is a new document (agent-forge-harness-1kg.4.4).

`npc` and `encounter` today; `recap` follows in the bead's second PR. Each is a
`tool_invocations.ToolExecutor`, so everything around it — admission, the
guards, the ledger's operation, the fence, retries, cancel and expiry — is
1kg.4.1's, and the generation itself is 1kg.5.4's `document_generation`.
**No SQL, no client construction, no route** lives here.

How one attempt goes, and why:

* `run` reads the campaign's name and tone through `ctx.read`, one short
  transaction closed before anything is spent (RQ-8), keyed by the fenced
  admission's owner (SEC-2). It checks for a cancel once, then generates with
  the one allowlisted client `ctx.client()` hands it (SEC-39), the ledger's
  config and nothing else (SEC-24), and as many attempts as fit. Every text
  from the GM travels in `document_generation`'s nonce-fenced data block
  (SEC-32). It writes nothing, and returns a placeholder result that the
  contract accepts; the generated document is carried to `finish`, keyed by
  the context itself.
* `finish` runs inside the fenced T2 that settles the invocation: it writes the
  document there — sealed version 1 by `assistant`, with the admission's
  campaign and the route's clock — and returns the real result, which is what
  gets stored. The document therefore exists before any link to it can render
  (X-6), exactly once however the attempt was retried, fenced out, expired or
  cancelled (RAIL-23, RAIL-27), and a T2 that rolls back takes it with it.
* The result carries a link — id, type, title, category — and server-composed
  closed sentences; nothing else of the document leaves (SEC-20). It suggests
  nothing: no model output starts work (SEC-32, SEC-33). Creation takes no
  campaign lock and writes no slot, disclosure, eligibility or audit row: every
  field starts `unclassified` and nothing is revealed (M-5, X-2).
* A model answer that cannot become a document is `OutputRefused`, stored as
  `provider_failed` (I-3); a request that would not fit the attempt's time is
  `OutOfTime`. Nothing here names a model, a provider or a tier (D-9).

The caller obligations `document_generation` states are discharged as follows:
authorize first (T1 did, and every read here is keyed by the admission's
owner); pass the allowlisted client and its alias (`ctx.client()`,
`ctx.model_alias`); persist inside the unit that settles the invocation, with
the admission's `campaign_id` (`finish`).
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

import config

from .campaign_store import CampaignStore, PostgresCampaignStore
from .conversation_store import ConversationStore, PostgresConversationStore
from .db import UnitOfWork
from .document_generation import (
    GENERATION_SPECS,
    CampaignFacts,
    GeneratedDocument,
    GenerationRefusal,
    GenerationRefused,
    GenerationRequest,
    GenerationSpec,
    InvalidGeneration,
    InvalidOutput,
    Provenance,
    attempts_that_fit,
    disclosure_prose,
    generate_document,
    persist_generated,
)
from .document_store import DocumentStore, PostgresDocumentStore
from .timeline_store import PostgresTimelineStore, TimelineStore
from .tool_invocations import (
    ExecutionContext,
    InvocationTarget,
    OutOfTime,
    OutputRefused,
    ToolExecutor,
)
from .workbench_contracts import (
    DOC_TYPE_LIBRARY_CATEGORY,
    TOOL_CREATES_DOC_TYPE,
    DocumentLink,
    DocumentResult,
    ErrorCode,
    ToolId,
    ToolResult,
)

log = logging.getLogger(__name__)

#: What `run` answers before the document exists. It satisfies `OpaqueId`, so
#: the contract accepts the placeholder; `finish` never lets it be stored.
PENDING_DOCUMENT_ID: Final = "doc_pending"
PENDING_TITLE: Final = "Pending"

#: PLACEHOLDER COPY, the design lane's (`cub`) to reword. The result's first
#: sentence, which a screen reader hears after "<label> finished": it states
#: the outcome on its own and names no model, provider or tier (D-9).
LEAD_SENTENCES: Mapping[ToolId, str] = MappingProxyType({
    ToolId.NPC: "Here's your NPC, saved to the campaign.",
    ToolId.ENCOUNTER: "Here's your encounter, saved to the campaign.",
})


@dataclass(frozen=True)
class DocumentToolStores:
    """The stores a document tool reads through `ctx.read` and writes in
    `finish`. Stateless: building them connects nothing."""

    campaigns: CampaignStore
    conversations: ConversationStore
    timeline: TimelineStore
    documents: DocumentStore

    @classmethod
    def postgres(cls) -> DocumentToolStores:
        return cls(PostgresCampaignStore(), PostgresConversationStore(), PostgresTimelineStore(),
                   PostgresDocumentStore())


@dataclass(frozen=True)
class _Carried:
    """What `run` hands `finish`: the validated document, never logged."""

    generated: GeneratedDocument = field(repr=False)


class _ContextGone(RuntimeError):
    """The admitted campaign is no longer the owner's to read between T1 and
    `run`: stored as `backend_unavailable`, and nothing is spent."""


def result_prose(tool_id: ToolId, provenance: Provenance) -> str:
    """The result's prose: the tool's lead sentence, then the closed disclosure
    of what the document rests on. Never model text (I-10)."""
    return f"{LEAD_SENTENCES[tool_id]}\n\n{disclosure_prose(provenance)}"


class DocumentToolExecutor:
    """`tool_invocations.ToolExecutor` for one document-creating tool."""

    def __init__(self, tool_id: ToolId, stores: DocumentToolStores) -> None:
        """`ValueError` unless the tool creates a document of a type with a
        generation spec for this very tool (X-8: never a default spec), and
        this module composes its result sentence."""
        doc_type = TOOL_CREATES_DOC_TYPE.get(tool_id)
        spec = None if doc_type is None else GENERATION_SPECS.get(doc_type)
        if spec is None or spec.tool is not tool_id or tool_id not in LEAD_SENTENCES:
            raise ValueError("that tool does not create a document here")
        self._tool_id = tool_id
        self._spec: GenerationSpec = spec
        self._stores = stores
        self._lock = threading.Lock()
        self._carried: weakref.WeakKeyDictionary[ExecutionContext, _Carried] = weakref.WeakKeyDictionary()

    def __repr__(self) -> str:
        return f"DocumentToolExecutor(tool={self._tool_id.value})"

    @property
    def tool_id(self) -> ToolId:
        return self._tool_id

    def precheck(self, unit: UnitOfWork, target: InvocationTarget) -> ErrorCode | None:
        """`account_limit_reached` at the account's stored-byte cap
        (agent-forge-harness-531x, PR-B), before any provider spend — T2's
        `persist_generated` write carries no quota of its own (its locks are
        not ours to reorder), so the account is soft-checked here instead.
        The overshoot this admits is bounded by `IN_FLIGHT_CAP` (2) times one
        generated document, about 12 KB or less. Otherwise nothing: the brief
        is required, and T1 has already checked the campaign and the thread
        with their owner."""
        stored = self._stores.documents.stored_bytes(unit, target.owner_id)
        if stored >= config.WORKBENCH_DOCUMENT_BYTES_PER_ACCOUNT_MAX:
            return ErrorCode.ACCOUNT_LIMIT_REACHED
        return None

    def run(self, ctx: ExecutionContext) -> DocumentResult:
        """Read, check for a cancel, generate, carry. Writes nothing."""
        campaign = ctx.read(lambda unit: self._campaign_facts(unit, ctx.target))
        request = GenerationRequest(
            doc_type=self._spec.doc_type, brief=ctx.target.brief, campaign=campaign, thread=(), preset={},
        )
        ctx.check_cancelled()
        generated = self._generated(ctx, request)
        with self._lock:
            self._carried[ctx] = _Carried(generated)
        return DocumentResult(
            tool_id=self._tool_id, result_kind="document", prose="", suggestions=[],
            document=DocumentLink(
                document_id=PENDING_DOCUMENT_ID, type=self._spec.doc_type, title=PENDING_TITLE,
                library_category=DOC_TYPE_LIBRARY_CATEGORY[self._spec.doc_type],
            ),
        )

    def finish(self, unit: UnitOfWork, ctx: ExecutionContext, result: ToolResult) -> DocumentResult:
        """Inside the fenced T2: write the carried document and answer its link.
        Anything but this executor's own placeholder, or a context that never
        ran, is a contract breach (`LookupError`), so T2 rolls back."""
        if not (
            isinstance(result, DocumentResult) and result.tool_id is self._tool_id
            and result.document.document_id == PENDING_DOCUMENT_ID
        ):
            raise LookupError("not this tool's pending result")
        with self._lock:
            carried = self._carried.pop(ctx, None)
        if carried is None:
            raise LookupError("nothing was generated for this attempt")
        generated = carried.generated
        record = persist_generated(unit, self._stores.documents, ctx.target.campaign_id, generated, now=ctx.now())
        finished = DocumentResult(
            tool_id=self._tool_id, result_kind="document",
            prose=result_prose(self._tool_id, generated.provenance), suggestions=[],
            document=DocumentLink(
                document_id=record.id, type=self._spec.doc_type, title=generated.data["name"],
                library_category=DOC_TYPE_LIBRARY_CATEGORY[self._spec.doc_type],
            ),
        )
        log.info(
            "document tool finished (operation_id=%s, tool=%s, document_id=%s, basis=%s, dropped_keys=%d, "
            "dropped_citations=%d)", ctx.operation_id, self._tool_id.value, record.id,
            generated.provenance.basis.value, generated.dropped_keys, generated.provenance.dropped_citations,
        )
        return finished

    # ── Steps ────────────────────────────────────────────────────────────────

    def _campaign_facts(self, unit: UnitOfWork, target: InvocationTarget) -> CampaignFacts:
        campaign = self._stores.campaigns.get(unit, target.campaign_id, owner_id=target.owner_id)
        if campaign is None:
            raise _ContextGone()
        return CampaignFacts(name=campaign.name, tone=campaign.tone)

    def _generated(self, ctx: ExecutionContext, request: GenerationRequest) -> GeneratedDocument:
        """`generate_document`, its two expected refusals translated for the
        failure table. Each translation is raised OUTSIDE the `except`, so it
        chains nothing a later message could leak through."""
        invalid: InvalidOutput | None = None
        try:
            return generate_document(
                request, client=ctx.client(), alias=ctx.model_alias, config=ctx.run_config(),
                max_attempts=attempts_that_fit(ctx.remaining_s()), between_attempts=ctx.check_cancelled,
            )
        except InvalidGeneration as exc:
            invalid = exc.code
        except GenerationRefused as exc:
            if exc.code is not GenerationRefusal.DEADLINE:
                log.warning("document tool refused before the provider (operation_id=%s, tool=%s, code=%s)",
                            ctx.operation_id, self._tool_id.value, exc.code.value)
                raise
        if invalid is None:
            raise OutOfTime()
        log.warning("document tool output refused (operation_id=%s, tool=%s, code=%s)",
                    ctx.operation_id, self._tool_id.value, invalid.value)
        raise OutputRefused()


def document_executors(stores: DocumentToolStores) -> dict[ToolId, ToolExecutor]:
    """Every document tool this module runs, keyed by tool."""
    return {tool: DocumentToolExecutor(tool, stores) for tool in LEAD_SENTENCES}

"""The document tools' executor (agent-forge-harness-1kg.4.4, PR 1 of 2):
group N of the alignment brief, on the in-memory twins with a scripted
provider behind the real `ProviderClientFactory` and `_BoundedClient`.

What the route adds — the fence, retries, cancel, the ledger, the answer — is
`test_document_tools_api.py`'s; what only PostgreSQL can prove is
`tests/test_document_tools_db.py`'s. Each test names the mutant it kills.

Run from the repo root:
    uv run python -m pytest service/tests/test_document_tools.py -q
"""

from __future__ import annotations

import ast
import gc
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from service import document_generation as dg
from service import document_tools, generate, tool_invocations
from service.campaign_store import InMemoryCampaignStore, shared_rows
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase, InMemoryTransaction
from service.document_store import InMemoryDocumentStore
from service.document_tools import (
    LEAD_SENTENCES,
    PENDING_DOCUMENT_ID,
    DocumentToolExecutor,
    DocumentToolStores,
    document_executors,
    result_prose,
)
from service.history import InMemoryMessageStore
from service.model_catalog import CATALOG, DEFAULT_ALIAS, PUBLIC_MODELS
from service.models import Source
from service.providers import ProviderClientFactory
from service.tests import document_generation_fixtures as fx
from service.timeline_store import InMemoryTimelineStore
from service.tool_invocations import (
    ATTEMPT_TTL_S,
    Admission,
    ExecutionContext,
    InvocationCancelled,
    InvocationTarget,
    OutOfTime,
    OutputRefused,
    context_reader,
    failure_for,
    judge_result,
)
from service.workbench_contracts import (
    DOC_TYPE_LIBRARY_CATEGORY,
    PROSE_MAX_CHARS,
    TOOL_CREATES_DOC_TYPE,
    DocumentResult,
    ErrorCode,
    ToolId,
)

GM_A, GM_B = 1, 2
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TOOLS = (ToolId.NPC, ToolId.ENCOUNTER)
BRIEFS = {ToolId.NPC: "A nervous harbourmistress", ToolId.ENCOUNTER: "Smugglers in a sea cave"}
FIELDS = {ToolId.NPC: fx.BASE_FIELDS[fx.NPC], ToolId.ENCOUNTER: fx.BASE_FIELDS[fx.ENCOUNTER]}
CANARY = fx.CANARY
SECOND_ALIAS = "second-allowlisted-alias"


class ScriptedLLM:
    """A provider client answering from a script of texts and exceptions."""

    def __init__(self, *script: str | BaseException, finish_reason: str | None = None) -> None:
        self.script = list(script)
        self.finish_reason = finish_reason
        self.calls: list[dict[str, Any]] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append({"input": input, "config": config, **kwargs})
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        metadata = {"finish_reason": self.finish_reason} if self.finish_reason else {}
        return AIMessage(content=item, response_metadata=metadata,
                         usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8})


@dataclass
class Twin:
    db: InMemoryDatabase
    stores: DocumentToolStores
    llm: ScriptedLLM
    factory: ProviderClientFactory

    def campaign(self, owner: int = GM_A, name: str = "Nocturne", tone: str | None = "Grim and salty") -> str:
        with self.db.transaction() as unit:
            return self.stores.campaigns.create(unit, owner_id=owner, name=name, tone=tone, now=T0).id

    def ctx(self, tool: ToolId, campaign: str, *, owner: int = GM_A, brief: str | None = None,
            left_s: float = ATTEMPT_TTL_S, probe: Callable[[], None] = lambda: None,
            alias: str = DEFAULT_ALIAS, read: bool = True, clock: Callable[[], datetime] | None = None,
            ) -> ExecutionContext:
        target = InvocationTarget(owner, campaign, "cnv_" + "a" * 22, tool,
                                  BRIEFS[tool] if brief is None else brief, None)
        admission = Admission(target, "inv_unit_00000000001", "ent_" + "a" * 22, T0, 1, "0" * 32,
                              T0 + timedelta(seconds=left_s), alias)
        return ExecutionContext(admission, clock=clock or (lambda: T0), factory=self.factory, probe=probe,
                                reader=context_reader(self.db) if read else None)

    def tables(self) -> dict[str, dict[Any, Any]]:
        with self.db.transaction() as unit:
            return {name: dict(shared_rows(self.db, name).visible(unit)) for name in sorted(self.db.tables)}

    def documents(self) -> dict[Any, Any]:
        return self.tables().get("documents", {})

    def finish(self, executor: DocumentToolExecutor, ctx: ExecutionContext, result: Any) -> DocumentResult:
        with self.db.transaction() as unit:
            return executor.finish(unit, ctx, result)


def a_twin(*script: str | BaseException, finish_reason: str | None = None) -> Twin:
    db = InMemoryDatabase()
    messages = InMemoryMessageStore()
    stores = DocumentToolStores(InMemoryCampaignStore(db), InMemoryConversationStore(db),
                                InMemoryTimelineStore(db, messages=messages), InMemoryDocumentStore(db))
    llm = ScriptedLLM(*script, finish_reason=finish_reason)
    return Twin(db, stores, llm, ProviderClientFactory(client_builders={DEFAULT_ALIAS: llm}))


def good(tool: ToolId, **changes: Any) -> str:
    return fx.envelope({**FIELDS[tool], **changes})


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generate, "_RETRY_BACKOFF_SECONDS", 0.0)


@dataclass
class Spy:
    calls: list[dict[str, Any]]


@pytest.fixture
def generation(monkeypatch: pytest.MonkeyPatch) -> Iterator[Spy]:
    """`document_tools.generate_document`, recorded and called through."""
    spy = Spy([])
    real = dg.generate_document

    def recorded(request: dg.GenerationRequest, **kwargs: Any) -> dg.GeneratedDocument:
        spy.calls.append({"request": request, **kwargs})
        return real(request, **kwargs)

    monkeypatch.setattr(document_tools, "generate_document", recorded)
    yield spy


# ── N-1, N-2: what reaches the generator ─────────────────────────────────────


@pytest.mark.parametrize("tool", TOOLS)
def test_n1_the_request_is_the_brief_and_that_owners_campaign_facts_and_nothing_else(
        tool: ToolId, generation: Spy) -> None:
    """Kills: the wrong spec; the brief from elsewhere; campaign facts read
    without the owner; a thread, corpus or preset leaking in."""
    twin = a_twin(good(tool))
    twin.campaign(GM_B, name="Nocturne", tone="Sunny and kind")
    mine = twin.campaign(GM_A, name="Nocturne", tone="Grim and salty")
    asked: list[tuple[str, int]] = []
    real_get = twin.stores.campaigns.get

    def get(unit: Any, campaign_id: str, *, owner_id: int) -> Any:
        asked.append((campaign_id, owner_id))
        return real_get(unit, campaign_id, owner_id=owner_id)

    twin.stores.campaigns.get = get  # type: ignore[method-assign]
    DocumentToolExecutor(tool, twin.stores).run(twin.ctx(tool, mine))
    [call] = generation.calls
    request = call["request"]
    assert request.doc_type is TOOL_CREATES_DOC_TYPE[tool]
    assert request.brief == BRIEFS[tool]
    assert request.campaign == dg.CampaignFacts(name="Nocturne", tone="Grim and salty")
    assert (request.thread, request.corpus, dict(request.preset), request.source_result) == ((), (), {}, None)
    assert asked == [(mine, GM_A)]


def test_n2_the_client_alias_config_attempts_and_cancel_hook_are_the_contexts(
        generation: Spy, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: `max_attempts=3` hard-coded; `DEFAULT_ALIAS` hard-coded;
    `between_attempts` omitted; a client built another way."""
    second = ScriptedLLM(good(ToolId.NPC))
    monkeypatch.setattr(tool_invocations, "workbench_profile",
                        lambda alias: CATALOG[DEFAULT_ALIAS] if alias in (DEFAULT_ALIAS, SECOND_ALIAS) else None)
    twin = a_twin(good(ToolId.NPC))
    twin.factory = ProviderClientFactory(client_builders={DEFAULT_ALIAS: twin.llm, SECOND_ALIAS: second})
    ctx = twin.ctx(ToolId.NPC, twin.campaign(), alias=SECOND_ALIAS)
    handed: list[Any] = []
    real_client = ctx.client

    def client() -> Any:
        handed.append(real_client())
        return handed[-1]

    hook = ctx.check_cancelled
    ctx.client = client  # type: ignore[method-assign]
    ctx.run_config = lambda: {"configurable": {"marker": 1}}  # type: ignore[method-assign]
    DocumentToolExecutor(ToolId.NPC, twin.stores).run(ctx)
    [call] = generation.calls
    assert call["client"] is handed[0]
    assert call["alias"] == SECOND_ALIAS
    assert call["config"] == {"configurable": {"marker": 1}}
    assert call["max_attempts"] == 2 == dg.attempts_that_fit(ATTEMPT_TTL_S)
    assert call["between_attempts"] == hook
    assert (len(second.calls), len(twin.llm.calls)) == (1, 0)


def test_n2_with_no_time_for_an_attempt_nothing_is_called_and_it_is_a_timeout(generation: Spy) -> None:
    twin = a_twin(good(ToolId.NPC))
    ctx = twin.ctx(ToolId.NPC, twin.campaign(), left_s=64)
    with pytest.raises(OutOfTime) as raised:
        DocumentToolExecutor(ToolId.NPC, twin.stores).run(ctx)
    assert generation.calls[0]["max_attempts"] == 0
    assert twin.llm.calls == []
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)
    assert failure_for(raised.value).code is ErrorCode.PROVIDER_TIMEOUT


# ── N-3: a cancel before the call spends nothing ─────────────────────────────


def test_n3_a_cancel_seen_after_the_read_stops_before_any_provider_call() -> None:
    """Kills: the pre-call `check_cancelled` omitted, or moved before the read."""
    twin = a_twin(good(ToolId.NPC))
    order: list[str] = []
    campaign = twin.campaign()
    real_get = twin.stores.campaigns.get

    def get(unit: Any, campaign_id: str, *, owner_id: int) -> Any:
        order.append("read")
        return real_get(unit, campaign_id, owner_id=owner_id)

    def probe() -> None:
        order.append("probe")
        raise InvocationCancelled()

    twin.stores.campaigns.get = get  # type: ignore[method-assign]
    with pytest.raises(InvocationCancelled):
        DocumentToolExecutor(ToolId.NPC, twin.stores).run(twin.ctx(ToolId.NPC, campaign, probe=probe))
    assert order == ["read", "probe"]
    assert twin.llm.calls == []


# ── N-4, N-5: the failure table ──────────────────────────────────────────────

_REACHABLE = [case for case in fx.INVALID_CASES if case.doc_type in (fx.NPC, fx.ENCOUNTER)]


@pytest.mark.parametrize("case", _REACHABLE, ids=lambda c: f"{c.doc_type.value}-{c.code.value}")
def test_n4_every_invalid_output_is_output_refused_unchained_and_logged_by_code(
        case: fx.InvalidCase, caplog: pytest.LogCaptureFixture) -> None:
    """Kills: re-raising `InvalidGeneration` (→ backend); chaining the cause."""
    tool = ToolId.NPC if case.doc_type is fx.NPC else ToolId.ENCOUNTER
    twin = a_twin(case.output, finish_reason=case.finish_reason)
    caplog.set_level(logging.DEBUG, logger="service.document_tools")
    with pytest.raises(OutputRefused) as raised:
        DocumentToolExecutor(tool, twin.stores).run(twin.ctx(tool, twin.campaign()))
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)
    assert failure_for(raised.value).code is ErrorCode.PROVIDER_FAILED
    [logged] = [r.getMessage() for r in caplog.records if r.name == "service.document_tools"]
    assert f"code={case.code.value}" in logged
    assert twin.documents() == {}


def test_n5_a_refusal_other_than_the_deadline_propagates_as_itself_and_is_an_outage(
        caplog: pytest.LogCaptureFixture) -> None:
    """Kills: every refusal mapped to `OutputRefused`. An empty brief on a
    brief-required tool is `invalid_context`, a server defect by the time it
    reaches `run` (T1's contract refuses it first)."""
    twin = a_twin(good(ToolId.NPC))
    caplog.set_level(logging.WARNING, logger="service.document_tools")
    with pytest.raises(dg.GenerationRefused) as raised:
        DocumentToolExecutor(ToolId.NPC, twin.stores).run(twin.ctx(ToolId.NPC, twin.campaign(), brief=""))
    assert raised.value.code is dg.GenerationRefusal.INVALID_CONTEXT
    assert failure_for(raised.value).code is ErrorCode.BACKEND_UNAVAILABLE
    assert any("code=invalid_context" in r.getMessage() for r in caplog.records)
    assert twin.llm.calls == []


def test_n5_a_campaign_gone_between_admission_and_run_is_an_outage_that_spends_nothing() -> None:
    twin = a_twin(good(ToolId.NPC))
    theirs = twin.campaign(GM_B)
    with pytest.raises(RuntimeError) as raised:
        DocumentToolExecutor(ToolId.NPC, twin.stores).run(twin.ctx(ToolId.NPC, theirs, owner=GM_A))
    assert failure_for(raised.value).code is ErrorCode.BACKEND_UNAVAILABLE
    assert twin.llm.calls == []


# ── N-6, N-8: the placeholder, then the real result and its one document ─────


@pytest.mark.parametrize("tool", TOOLS)
def test_n6_run_answers_a_valid_placeholder_and_finish_answers_the_stored_documents_link(tool: ToolId) -> None:
    """Kills: `finish` passing `result` through (the sentinel stored); the
    title from the brief; suggestions from anywhere."""
    twin = a_twin(good(tool, name="Maren of the Reach"))
    ctx = twin.ctx(tool, twin.campaign())
    executor = DocumentToolExecutor(tool, twin.stores)
    placeholder = judge_result(executor.run(ctx), tool, lambda _tool: True)
    assert isinstance(placeholder, DocumentResult)
    assert placeholder.document.document_id == PENDING_DOCUMENT_ID
    finished = twin.finish(executor, ctx, placeholder)
    [stored_id] = twin.documents()
    assert finished.document.document_id != PENDING_DOCUMENT_ID
    assert finished.document.document_id.startswith("doc_") and str(stored_id).endswith(finished.document.document_id)
    doc_type = TOOL_CREATES_DOC_TYPE[tool]
    assert finished.document.model_dump(mode="json") == {
        "document_id": finished.document.document_id, "type": doc_type.value, "title": "Maren of the Reach",
        "library_category": DOC_TYPE_LIBRARY_CATEGORY[doc_type].value,
    }
    assert finished.suggestions == [] and finished.prose.startswith(LEAD_SENTENCES[tool])
    assert judge_result(finished, tool, lambda _tool: True) == finished


def test_n8_finish_persists_with_the_admissions_campaign_and_the_routes_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: the campaign from elsewhere; `now=datetime.now`."""
    twin = a_twin(good(ToolId.NPC))
    later = T0 + timedelta(seconds=7)
    campaign = twin.campaign()
    ctx = twin.ctx(ToolId.NPC, campaign, clock=lambda: later)
    seen: list[tuple[str, datetime]] = []
    real = dg.persist_generated

    def persist(unit: Any, store: Any, campaign_id: str, generated: Any, *, now: datetime) -> Any:
        seen.append((campaign_id, now))
        return real(unit, store, campaign_id, generated, now=now)

    monkeypatch.setattr(document_tools, "persist_generated", persist)
    executor = DocumentToolExecutor(ToolId.NPC, twin.stores)
    finished = twin.finish(executor, ctx, executor.run(ctx))
    assert seen == [(campaign, later)]
    with twin.db.transaction() as unit:
        record = twin.stores.documents.get(unit, campaign, finished.document.document_id)
    assert record is not None
    assert (record.version.number, record.version.is_sealed, record.version.author) == (1, True, "assistant")
    assert record.version.summary == dg.version_summary(dg.Provenance(dg.GenerationBasis.INVENTED, (), 0))
    assert record.created_at == later
    [row] = twin.documents().values()
    assert row.created_command_id is None


# ── N-7: the prose ───────────────────────────────────────────────────────────


def _mixed(count: int, label_chars: int) -> dg.Provenance:
    sources = tuple(Source(book=f"{n}" + "b" * label_chars, chapter="c", section="s", page=n, snippet="x")
                    for n in range(count))
    return dg.Provenance(dg.GenerationBasis.MIXED, sources, 0)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("provenance", [
    dg.Provenance(dg.GenerationBasis.INVENTED, (), 0), dg.Provenance(dg.GenerationBasis.THREAD, (), 0),
    _mixed(1, 10), _mixed(8, 1_000),
], ids=["invented", "thread", "mixed-1", "mixed-8x1000"])
def test_n7_the_prose_is_the_lead_sentence_and_the_disclosure_within_the_bound(
        tool: ToolId, provenance: dg.Provenance) -> None:
    """Kills: the bound unchecked; a disclosure dropped."""
    prose = result_prose(tool, provenance)
    assert len(prose) <= PROSE_MAX_CHARS
    assert prose.startswith(LEAD_SENTENCES[tool] + "\n\n")
    assert prose.endswith(dg.disclosure_prose(provenance))
    assert len(LEAD_SENTENCES[tool]) < 120 and "#" not in prose.split("\n\n")[0]


# ── N-9: the carry is the context's ──────────────────────────────────────────


def test_n9_the_carry_is_keyed_by_context_emptied_by_finish_and_dies_with_its_context() -> None:
    """Kills: a per-executor "last generated" slot; a strong dict that leaks."""
    twin = a_twin(good(ToolId.NPC, name="Alpha"), good(ToolId.NPC, name="Beta"), good(ToolId.NPC, name="Gamma"))
    campaign = twin.campaign()
    executor = DocumentToolExecutor(ToolId.NPC, twin.stores)
    a, b = twin.ctx(ToolId.NPC, campaign), twin.ctx(ToolId.NPC, campaign)
    placeholder_a, placeholder_b = executor.run(a), executor.run(b)
    assert twin.finish(executor, a, placeholder_a).document.title == "Alpha"
    assert twin.finish(executor, b, placeholder_b).document.title == "Beta"
    assert len(executor._carried) == 0
    with pytest.raises(LookupError):
        twin.finish(executor, a, placeholder_a)
    with pytest.raises(LookupError):
        twin.finish(executor, twin.ctx(ToolId.NPC, campaign), placeholder_a)
    dropped = twin.ctx(ToolId.NPC, campaign)
    executor.run(dropped)
    assert len(executor._carried) == 1
    del dropped
    gc.collect()
    assert len(executor._carried) == 0
    assert len(twin.documents()) == 2


def test_n9_finish_refuses_anything_but_its_own_placeholder() -> None:
    twin = a_twin(good(ToolId.NPC))
    campaign = twin.campaign()
    executor = DocumentToolExecutor(ToolId.NPC, twin.stores)
    ctx = twin.ctx(ToolId.NPC, campaign)
    placeholder = executor.run(ctx)
    real_link = placeholder.model_copy(update={"document": placeholder.document.model_copy(
        update={"document_id": "doc_" + "r" * 22})})
    for wrong in (real_link, placeholder.model_dump(mode="json")):
        with pytest.raises(LookupError):
            twin.finish(executor, ctx, wrong)
    assert twin.documents() == {}
    assert twin.finish(executor, ctx, placeholder).document.document_id != PENDING_DOCUMENT_ID


# ── N-10, N-11: run writes nothing; finish writes one document, unlocked ─────


class _Recording:
    """A store that records every method called on it, and refuses writes when told."""

    def __init__(self, inner: Any, *, refuse: frozenset[str] = frozenset()) -> None:
        self._inner, self._refuse = inner, refuse
        self.called: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            self.called.append(name)
            if name in self._refuse:
                raise AssertionError(f"{name} called")
            return attr(*args, **kwargs)

        return call


def test_n10_run_is_read_only_and_finish_writes_only_through_document_create() -> None:
    twin = a_twin(good(ToolId.NPC))
    campaign = twin.campaign()
    documents = _Recording(twin.stores.documents, refuse=frozenset({"create", "patch", "restore", "seal"}))
    campaigns = _Recording(twin.stores.campaigns, refuse=frozenset({"create", "rename", "set_archived"}))
    stores = DocumentToolStores(campaigns, twin.stores.conversations, twin.stores.timeline, documents)  # type: ignore[arg-type]
    executor = DocumentToolExecutor(ToolId.NPC, stores)
    ctx = twin.ctx(ToolId.NPC, campaign)
    before = twin.tables()
    placeholder = executor.run(ctx)
    assert twin.tables() == before
    assert (campaigns.called, documents.called) == (["get"], [])
    documents._refuse = frozenset()
    twin.finish(executor, ctx, placeholder)
    assert (campaigns.called, documents.called) == (["get"], ["create"])
    changed = {name for name, rows in twin.tables().items() if rows != before.get(name)}
    assert changed == {"documents", "document_versions"}


def test_n11_finish_takes_no_campaign_lock_and_no_row_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    locks: list[str] = []
    real_campaign, real_row = InMemoryTransaction.lock_campaign, InMemoryTransaction.note_row_lock

    def lock_campaign(self: InMemoryTransaction, *args: Any, **kwargs: Any) -> None:
        locks.append("campaign")
        real_campaign(self, *args, **kwargs)

    def note_row_lock(self: InMemoryTransaction) -> None:
        locks.append("row")
        real_row(self)

    monkeypatch.setattr(InMemoryTransaction, "lock_campaign", lock_campaign)
    monkeypatch.setattr(InMemoryTransaction, "note_row_lock", note_row_lock)
    twin = a_twin(good(ToolId.NPC))
    campaign = twin.campaign()
    with twin.db.transaction() as unit:  # the positive control: the spies are installed
        unit.lock_campaign(campaign, shared=True)
        unit.note_row_lock()
    assert locks == ["campaign", "row"]
    locks.clear()
    executor = DocumentToolExecutor(ToolId.NPC, twin.stores)
    ctx = twin.ctx(ToolId.NPC, campaign)
    twin.finish(executor, ctx, executor.run(ctx))
    assert locks == []
    assert len(twin.documents()) == 1


# ── N-12: nothing private, no model ──────────────────────────────────────────


def test_n12_no_value_log_or_repr_carries_private_text_or_a_model(caplog: pytest.LogCaptureFixture) -> None:
    twin = a_twin(good(ToolId.NPC, voice=f"voice {CANARY}", wants=f"wants {CANARY}"))
    caplog.set_level(logging.DEBUG)
    campaign = twin.campaign(name=f"Camp {CANARY}", tone=f"tone {CANARY}")
    executor = DocumentToolExecutor(ToolId.NPC, twin.stores)
    ctx = twin.ctx(ToolId.NPC, campaign, brief=f"brief {CANARY}")
    placeholder = executor.run(ctx)
    carried = repr(executor._carried.get(ctx))
    finished = twin.finish(executor, ctx, placeholder)
    words = {CANARY, *CATALOG, *(p.display_name for p in CATALOG.values()),
             *(p.label for p in PUBLIC_MODELS.values()), "traveller", "adventurer", "loremaster", "openai"}
    seen = [placeholder.model_dump_json(), finished.model_dump_json(), repr(executor), carried,
            repr(twin.stores), repr(ctx), *(record.getMessage() for record in caplog.records)]
    assert any("document tool finished" in text for text in seen), "the positive control: the log was captured"
    assert [(word, text) for text in seen for word in words if word.casefold() in text.casefold()] == []


# ── N-13, N-14: which tools, and what the module may import ──────────────────


@pytest.mark.parametrize("tool", [t for t in ToolId if t not in TOOLS])
def test_n13_every_other_tool_is_refused_at_construction(tool: ToolId) -> None:
    """Kills: a default spec (X-8). `recap` is refused until its window lands
    (PR 2 of 2)."""
    twin = a_twin()
    with pytest.raises(ValueError):
        DocumentToolExecutor(tool, twin.stores)


def test_n13_the_module_registers_exactly_npc_and_encounter() -> None:
    twin = a_twin()
    made = document_executors(twin.stores)
    assert set(made) == set(TOOLS)
    assert all(executor.tool_id is tool for tool, executor in made.items())
    assert set(LEAD_SENTENCES) == set(TOOLS)


ALLOWED_SERVICE_MODULES = frozenset({
    "campaign_store", "conversation_store", "db", "document_generation", "document_store", "timeline",
    "timeline_store", "tool_invocations", "workbench_contracts", "workbench_registry", "recap_window",
})


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.update([base] if node.module else [f"{base}{alias.name}" for alias in node.names])
    return found


def test_n14_the_module_imports_only_the_standard_library_and_the_allowed_service_modules() -> None:
    """Kills: a second provider path (SEC-39)."""
    import sys

    imported = _imports(Path(document_tools.__file__))
    service = {name.lstrip(".").split(".")[0] for name in imported if name.startswith(".")}
    outside = {name.split(".")[0] for name in imported if not name.startswith(".")}
    assert service <= ALLOWED_SERVICE_MODULES, service - ALLOWED_SERVICE_MODULES
    assert outside - {"__future__"} <= sys.stdlib_module_names, outside - set(sys.stdlib_module_names)
    assert "tool_invocations" in service and "logging" in outside, "the positive control: the walk found both kinds"

"""The card tools' wire contract and seam (bead 1kg.4.3, PR-A).

The card shapes themselves are pinned by ``contracts/workbench/v1/*.json``, which
``test_workbench_contracts.py`` and ``ui/src/gm/contracts.test.ts`` both read.
This file pins what a fixture cannot: that the two languages hold the same
numbers and the same marker pattern, that the strict cited source keeps
``Source``'s keys, that the seam's two markers land through the real service,
that a stored result is written by alias (I-28), and the ``card_generation``
usage purpose.

Run from the repo root:
    uv run python -m pytest service/tests/test_card_contract.py -q
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from service import document_generation, ratelimit, tool_invocations, usage_capture, usage_ledger
from service import workbench_contracts as wc
from service.campaign_store import InMemoryCampaignStore, shared_rows
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase
from service.generate import AttemptStartObserver
from service.history import InMemoryMessageStore
from service.model_catalog import DEFAULT_ALIAS
from service.models import Source
from service.providers import ProviderClientFactory
from service.session import SessionData
from service.timeline_store import InMemoryTimelineStore
from service.tool_invocation_store import ATTEMPT_MAX, InMemoryToolInvocationStore, InvocationRow
from service.tool_invocations import (
    ExecutionContext,
    InvocationStores,
    InvocationTarget,
    OutputRefused,
    Replay,
    ToolSettings,
    cancellation_probe,
    complete,
    execute,
    submit,
)
from service.workbench_contracts import ResultKind, ToolId, ToolInvocation, ToolInvocationRequest

CONTRACTS_TS = Path(__file__).resolve().parents[2] / "ui" / "src" / "gm" / "contracts.ts"
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

_CARD_CONSTANTS = (
    "CARD_TITLE_MAX_CHARS", "LOOT_MAX_ITEMS", "LOOT_VALUE_MAX_CHARS", "LOOT_NOTE_MAX_CHARS", "LOOT_QUANTITY_MAX",
    "NAMES_MAX_ENTRIES", "NAME_MAX_CHARS", "NAME_NOTE_MAX_CHARS", "HOOKS_MAX_ENTRIES", "HOOK_TITLE_MAX_CHARS",
    "HOOK_TEXT_MAX_CHARS", "RULES_ANSWER_MAX_CHARS", "RULES_MAX_CITATIONS",
)


def _ts_source() -> str:
    return CONTRACTS_TS.read_text(encoding="utf-8")


# ── A. The contract, where a fixture cannot reach ────────────────────────────


@pytest.mark.parametrize("name", _CARD_CONSTANTS)
def test_a1_each_card_bound_is_the_same_number_in_both_languages(name: str) -> None:
    """The boundary fixtures pin every bound's behaviour; this pins the named
    constants a later edit would change, so neither side moves alone."""
    found = re.search(rf"^export const {name} = ([A-Z_]+|[0-9_]+)$", _ts_source(), re.MULTILINE)
    assert found is not None, name
    written = found.group(1)
    value = getattr(wc, written) if written[0].isalpha() else int(written.replace("_", ""))
    assert value == getattr(wc, name)


def test_a5_the_marker_pattern_is_one_literal_on_both_sides_and_ascii_digits_only() -> None:
    """Kills a marker rule that drifts between the languages, and a Python ``\\d``
    that reads a non-ASCII digit as a marker JavaScript would not see."""
    assert f"export const RULES_MARKER = /{wc.RULES_MARKER.pattern}/g" in _ts_source()
    assert wc.RULES_MARKER.flags & re.ASCII
    assert wc.RULES_MARKER.findall("[1] [12] [123] [1d6] [DC 15] [٣]") == ["1", "12"]


def test_a5_a_cited_source_keeps_the_chat_sources_keys_and_reads_strictly() -> None:
    """``CitedSource`` mirrors ``service.models.Source`` (the strict reading the
    client's ``SourceSchema`` already applies); a new key on one must reach the other."""
    assert list(wc.CitedSource.model_fields) == list(Source.model_fields)
    retrieved = Source(book="synthetic-5e", chapter="Conditions", page=12, snippet="A held creature…")
    assert wc.CitedSource.model_validate(retrieved.model_dump()).page == 12
    with pytest.raises(ValueError):
        wc.CitedSource.model_validate({**retrieved.model_dump(), "page": "12"})


def test_a5_a_citation_names_at_most_the_passages_a_generation_is_handed() -> None:
    assert wc.RULES_MAX_CITATIONS == document_generation.CORPUS_MAX_PASSAGES


def test_a7_every_card_tool_names_its_card_kind_and_nothing_else_does() -> None:
    card_tools = {tool for tool, kind in wc.TOOL_RESULT_KIND.items() if kind is ResultKind.CARD}
    assert set(wc.TOOL_CARD_KIND) == card_tools
    assert sorted(kind.value for kind in wc.TOOL_CARD_KIND.values()) == sorted(kind.value for kind in wc.CardKind)
    assert wc.ErrorCode("not_in_sources") is wc.ErrorCode.NOT_IN_SOURCES


# ── C-5, C-6: the seam through the twin ──────────────────────────────────────


class _Card:
    """An executor that answers what the test says, reads nothing and writes nothing."""

    def __init__(self, tool_id: ToolId, behaviour: Callable[[ExecutionContext], Any]) -> None:
        self.tool_id = tool_id
        self.behaviour = behaviour

    def precheck(self, unit: Any, target: InvocationTarget) -> wc.ErrorCode | None:
        return None

    def run(self, ctx: ExecutionContext) -> Any:
        return self.behaviour(ctx)

    def finish(self, unit: Any, ctx: ExecutionContext, result: Any) -> Any:
        return result


class _Twin:
    """The in-memory twins, driven through `submit`, `execute` and `complete` —
    what the create route runs, without the route."""

    def __init__(self, executor: _Card) -> None:
        self.db = InMemoryDatabase()
        messages = InMemoryMessageStore()
        self.stores = InvocationStores(
            InMemoryToolInvocationStore(self.db), InMemoryCampaignStore(self.db),
            InMemoryConversationStore(self.db), InMemoryTimelineStore(self.db, messages=messages),
        )
        self.executor = executor
        with self.db.transaction() as unit:
            self.campaign = self.stores.campaigns.create(unit, owner_id=1, name="Nocturne", now=T0).id
            self.conversation = self.stores.conversations.create(
                unit, owner_id=1, campaign_id=self.campaign, title=None, started_mode=None,
            ).id
        messages.claim_conversation(self.conversation, 1)

    def post(self, now: datetime) -> ToolInvocation:
        request = ToolInvocationRequest.model_validate({
            "schema_version": 1, "invocation_id": "inv_card_0000000000001", "tool_id": self.executor.tool_id.value,
            "brief": "a drowned thing", "campaign_id": self.campaign, "conversation_id": self.conversation,
        })
        executors = {self.executor.tool_id: self.executor}
        settings = ToolSettings(frozenset({self.executor.tool_id}))
        admitted = submit(self.db, self.stores, executors, settings, SessionData(user_id=1, role="dm"), request,
                          now=now, chat_turns_today=0)
        if isinstance(admitted, Replay):
            return admitted.invocation

        def clock() -> datetime:
            return now

        ctx = ExecutionContext(admitted, clock=clock, factory=ProviderClientFactory(client_builders={}),
                               probe=cancellation_probe(self.db, self.stores, admitted, clock))
        outcome = execute(self.executor, ctx, available=lambda tool: True)
        return complete(self.db, self.stores, self.executor, admitted, outcome, ctx, now=now)

    def stored(self) -> tuple[InvocationRow, dict[str, Any]]:
        """The one invocation row as stored (its JSON parsed on read, as JSONB
        is), and its timeline entry's payload."""
        with self.db.transaction() as unit:
            invocations: list[Any] = list(dict(shared_rows(self.db, "tool_invocations").visible(unit)).values())
            entries = self.stores.timeline.entry_window(unit, self.conversation, before=None, limit=50)
        assert len(invocations) == 1 and len(entries) == 1
        return invocations[0].read(), cast(dict[str, Any], entries[0].payload)


def _raise(exc: BaseException) -> Callable[[ExecutionContext], Any]:
    def run(ctx: ExecutionContext) -> Any:
        raise exc

    return run


def test_c5_a_refused_answer_on_the_last_attempt_is_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills the attempt ceiling bypassed for the new marker path: every attempt
    below the last stays retryable, the last one is final, and nothing runs after."""
    monkeypatch.setattr(ratelimit, "check_chat_request", lambda user_id: None)
    runs: list[int] = []

    def refuse(ctx: ExecutionContext) -> Any:
        runs.append(ctx.attempt)
        raise OutputRefused()

    twin = _Twin(_Card(ToolId.LOOT, refuse))
    for attempt in range(1, ATTEMPT_MAX + 1):
        answer = twin.post(T0 + timedelta(seconds=attempt))
        assert answer.error is not None and answer.error.code is wc.ErrorCode.PROVIDER_FAILED
        assert (answer.attempt, answer.error.retryable) == (attempt, attempt < ATTEMPT_MAX)
    assert twin.post(T0 + timedelta(seconds=ATTEMPT_MAX + 1)) == answer
    assert runs == list(range(1, ATTEMPT_MAX + 1))


_MONSTER = {
    "result_kind": "card", "tool_id": "monster", "prose": "Invented for your campaign.", "suggestions": [],
    "card": {"card_kind": "stat_block", "stat_block": {
        "name": "Tidewarden Drowned", "ac": 16, "hp": 82,
        "abilities": {"str": 18, "dex": 12, "con": 17, "int": 9, "wis": 14, "cha": 8},
    }},
}


@pytest.mark.parametrize("produced", [
    pytest.param(lambda: _MONSTER, id="as-json"),
    pytest.param(lambda: wc.CardResult.model_validate(_MONSTER), id="as-the-model"),
])
def test_c6_a_stored_card_is_written_by_alias(monkeypatch: pytest.MonkeyPatch, produced: Callable[[], Any]) -> None:
    """I-28. Kills `by_alias` dropped at either call site: the stored result and
    the timeline entry's embedded invocation both spell the ability `int`, as the
    client's strict `AbilitiesSchema` requires, and both still read back."""
    monkeypatch.setattr(ratelimit, "check_chat_request", lambda user_id: None)
    twin = _Twin(_Card(ToolId.MONSTER, lambda ctx: produced()))
    answer = twin.post(T0)
    assert answer.status.value == "done"
    row, entry = twin.stored()

    assert row.result is not None
    stored_abilities = row.result["card"]["stat_block"]["abilities"]
    entry_abilities = entry["invocation"]["result"]["card"]["stat_block"]["abilities"]
    for abilities in (stored_abilities, entry_abilities):
        assert abilities["int"] == 9 and "int_" not in abilities
    assert entry["invocation"]["result"] == row.result

    assert ToolInvocation.model_validate(entry["invocation"]) == answer
    assert wc.CardResult.model_validate(row.result).card.stat_block.abilities.int_ == 9  # type: ignore[union-attr]


def test_c6_the_entry_builder_embeds_the_invocation_by_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills `by_alias` dropped from `tool_entry` alone: the timeline store
    re-serializes what it is handed, so this reads the builder's own answer, the
    shape any other sender of an entry would forward."""
    monkeypatch.setattr(ratelimit, "check_chat_request", lambda user_id: None)
    twin = _Twin(_Card(ToolId.MONSTER, lambda ctx: _MONSTER))
    twin.post(T0)
    row, _ = twin.stored()
    built = tool_invocations.tool_entry(row)["invocation"]["result"]["card"]["stat_block"]["abilities"]
    assert built["int"] == 9 and "int_" not in built


def test_c6_a_corpus_miss_is_stored_final_with_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second marker through the service: `failed`, `not_in_sources`, final,
    and a repeat replays rather than running again."""
    monkeypatch.setattr(ratelimit, "check_chat_request", lambda user_id: None)
    runs: list[int] = []

    def miss(ctx: ExecutionContext) -> Any:
        runs.append(ctx.attempt)
        raise tool_invocations.NotInSources()

    twin = _Twin(_Card(ToolId.RULES, miss))
    answer = twin.post(T0)
    assert answer.error is not None
    assert (answer.status.value, answer.error.code, answer.error.retryable) == (
        "failed", wc.ErrorCode.NOT_IN_SOURCES, False)
    assert twin.post(T0 + timedelta(seconds=1)) == answer and runs == [1]
    row, entry = twin.stored()
    assert row.error is not None and row.error["code"] == "not_in_sources"
    assert {k: v for k, v in entry["invocation"]["error"].items() if v is not None} == row.error


# ── D-1: the usage purpose ───────────────────────────────────────────────────


def test_d1_card_generation_is_an_attempt_purpose_and_a_structuring_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills only one of the two purpose sets extended: an attempt with the
    purpose reaches the ledger through `check_attempt`, and an outcome with it is
    emitted rather than refused."""
    purpose = usage_capture.PURPOSE_CARD_GENERATION
    assert purpose == "card_generation"
    rows: list[Any] = []
    outcomes: list[dict[str, Any]] = []

    class Sink:
        def write(self, batch: Any) -> int:
            rows.extend(batch)
            return len(batch)

    monkeypatch.setattr(usage_capture, "_ledger_provider", lambda: Sink())
    monkeypatch.setattr(usage_capture, "_emit_outcome_record", lambda operation, fields: outcomes.append(fields))
    token = usage_capture.begin_operation(mode="gm", billed_account_id=1, request=SimpleNamespace(headers={}),
                                          operation=usage_capture.OPERATION_TOOL_INVOCATION)
    config = usage_capture.run_config_with_operation(None)
    try:
        observer = usage_capture.observer_for(config, purpose=purpose, alias=DEFAULT_ALIAS)
        assert isinstance(observer, AttemptStartObserver)
        observer.attempt_started()
        observer.record(alias=DEFAULT_ALIAS, result=None, error=RuntimeError("the provider failed"))
        usage_capture.record_structuring_outcome(config, purpose=purpose, outcome=usage_capture.OUTCOME_PRODUCED)
    finally:
        usage_capture.end_operation(token)
    assert [usage_ledger.check_attempt(row).purpose for row in rows] == [purpose]
    assert [(o["purpose"], o["outcome"]) for o in outcomes] == [(purpose, "produced")]

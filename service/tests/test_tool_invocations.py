"""The GM tool invocation service's own rules (bead 1kg.4.1, slice B), without
the routes: the deployment switches (I-7), availability (C-12), the failure
table against `/chat`'s (I-22, C-9), the attempt's time bounds (C-7), the model
allowlist on the executor's one path to a provider (C-8, SEC-39), and the one
builder of the wire shape (I-25).

Run from the repo root:
    uv run python -m pytest service/tests/test_tool_invocations.py -q
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import openai
import pytest

from service import tool_invocations
from service.app import ERROR_STATUS, normalize_llm_error
from service.model_catalog import CATALOG, DEFAULT_ALIAS, workbench_profile
from service.providers import ProviderClientFactory
from service.tool_invocation_store import ATTEMPT_MAX, InvocationRow
from service.tool_invocations import (
    ATTEMPT_TTL_S,
    FINAL_CATEGORIES,
    PROVIDER_CALL_MAX_S,
    PROVIDER_CALL_MIN_S,
    Admission,
    ExecutionContext,
    InvocationTarget,
    OutOfTime,
    ProviderNotAllowed,
    ToolSettings,
    Unreadable,
    failure_for,
    judge_result,
    parse_settings,
    provider_category,
    to_wire,
    tool_availability,
)
from service.workbench_contracts import ErrorCode, ToolId

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_REQUEST = httpx.Request("POST", "https://provider.invalid")


def _status(code: int) -> httpx.Response:
    return httpx.Response(code, request=_REQUEST)


#: One instance of every class `service.app.normalize_llm_error` names.
PROVIDER_ERRORS: list[BaseException] = [
    openai.RateLimitError("x", response=_status(429), body=None),
    openai.ContentFilterFinishReasonError(),
    openai.BadRequestError("x", response=_status(400), body=None),
    openai.UnprocessableEntityError("x", response=_status(422), body=None),
    openai.AuthenticationError("x", response=_status(401), body=None),
    openai.PermissionDeniedError("x", response=_status(403), body=None),
    openai.APITimeoutError(request=_REQUEST),
    openai.APIConnectionError(request=_REQUEST),
    openai.InternalServerError("x", response=_status(500), body=None),
    openai.NotFoundError("x", response=_status(404), body=None),
]


# ── I3: the deployment switches ──────────────────────────────────────────────


@pytest.mark.parametrize(("tools", "capabilities"), [("", ""), ("  ", " , "), (" , ,", "")])
def test_empty_and_whitespace_switches_mean_nothing_runs(tools: str, capabilities: str) -> None:
    assert parse_settings(tools, capabilities) == ToolSettings()


def test_switches_are_comma_separated_registry_ids() -> None:
    assert parse_settings(" npc , loot", "image_generation") == ToolSettings(
        frozenset({ToolId.NPC, ToolId.LOOT}), frozenset({"image_generation"}))


@pytest.mark.parametrize(("tools", "capabilities", "variable"), [
    ("npc,npcs", "", "WORKBENCH_ENABLED_TOOLS"), ("NPC", "", "WORKBENCH_ENABLED_TOOLS"),
    ("", "images", "WORKBENCH_CAPABILITIES"),
])
def test_an_unknown_id_in_either_switch_fails_at_parse(tools: str, capabilities: str, variable: str) -> None:
    with pytest.raises(ValueError, match=variable) as refused:
        parse_settings(tools, capabilities)
    assert "npcs" not in str(refused.value) and "images" not in str(refused.value)


def test_the_route_module_parses_the_real_switches_at_import() -> None:
    from service import tool_invocations_api
    assert tool_invocations_api.get_tool_settings() == ToolSettings()


# ── C-12: availability is one function ───────────────────────────────────────


class _Executor:
    def __init__(self, tool_id: ToolId) -> None:
        self.tool_id = tool_id


def test_a_tool_runs_only_when_named_switched_on_and_registered() -> None:
    every: dict[ToolId, Any] = {tool: _Executor(tool) for tool in ToolId}
    named = ToolSettings(frozenset(ToolId))
    assert tool_availability(ToolId.NPC, named, every) is None
    assert tool_availability(ToolId.NPC, ToolSettings(), every) == "That tool isn't available yet."
    assert tool_availability(ToolId.NPC, named, {}) == "That tool isn't available yet."
    assert tool_availability(ToolId.MAP, named, every) == "Image generation isn't set up yet."
    switched = ToolSettings(frozenset(ToolId), frozenset({"image_generation"}))
    assert tool_availability(ToolId.MAP, switched, every) is None


# ── I4, C-9: the failure table agrees with /chat's ───────────────────────────


@pytest.mark.parametrize("error", PROVIDER_ERRORS, ids=lambda e: type(e).__name__)
def test_every_provider_error_is_classified_as_chat_classifies_it(error: BaseException) -> None:
    """M-I1 (a timeout checked after the connection error), M-I2 (content_filter
    retryable)."""
    category = normalize_llm_error(error)
    assert provider_category(error) == category
    stored = failure_for(error)
    if category == "timeout":
        assert (stored.code, stored.retryable) == (ErrorCode.PROVIDER_TIMEOUT, True)
        return
    final = ERROR_STATUS[category][0] == 422
    assert (stored.code, stored.retryable) == (ErrorCode.PROVIDER_FAILED, not final)
    assert stored.message == (tool_invocations.PROVIDER_FINAL_MESSAGE if final
                              else tool_invocations.PROVIDER_FAILED_MESSAGE)


def test_the_final_categories_are_exactly_chats_422s() -> None:
    assert frozenset(c for c, (status, _) in ERROR_STATUS.items() if status == 422) == FINAL_CATEGORIES


@pytest.mark.parametrize("error", [RuntimeError("x"), OSError("x"), ValueError("x")])
def test_anything_unexpected_is_a_retryable_backend_outage(error: BaseException) -> None:
    stored = failure_for(error)
    assert (stored.code, stored.retryable) == (ErrorCode.BACKEND_UNAVAILABLE, True)


def test_a_database_or_embedding_outage_is_a_retryable_backend_outage() -> None:
    import psycopg

    from ingestion.retrieval import EmbeddingUnavailableError

    for error in (psycopg.OperationalError("x"), EmbeddingUnavailableError("x")):
        assert failure_for(error).code is ErrorCode.BACKEND_UNAVAILABLE


# ── E6, C-7: the attempt's time ──────────────────────────────────────────────


def test_the_attempt_bounds_are_pinned() -> None:
    assert 120 < ATTEMPT_TTL_S < 300
    assert 0 < PROVIDER_CALL_MIN_S < PROVIDER_CALL_MAX_S <= ATTEMPT_TTL_S
    assert ATTEMPT_MAX == 100


class _Recorder:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> str:
        self.timeouts.append(kwargs["timeout"])
        return "ok"


class _Factory(ProviderClientFactory):
    def __init__(self, client: Any) -> None:
        super().__init__(client_builders={DEFAULT_ALIAS: client})
        self.asked: list[str] = []

    def client_for(self, alias: str) -> Any:
        self.asked.append(alias)
        return super().client_for(alias)


def _admission(alias: str = DEFAULT_ALIAS) -> Admission:
    target = InvocationTarget(1, "cmp_" + "a" * 22, "cnv_" + "a" * 22, ToolId.NPC, "a brief", None)
    return Admission(target, "inv_unit_00000000001", "ent_x", T0, 1, "0" * 32, T0 + timedelta(seconds=ATTEMPT_TTL_S),
                     alias)


def test_each_provider_call_is_bounded_by_what_is_left_of_the_attempt() -> None:
    now = [T0]
    recorder = _Recorder()
    ctx = ExecutionContext(_admission(), clock=lambda: now[0], factory=_Factory(recorder), probe=lambda: None)
    ctx.client().invoke("a prompt")
    now[0] = T0 + timedelta(seconds=ATTEMPT_TTL_S - 40)
    ctx.client().invoke("a prompt", config={"configurable": {}})
    assert recorder.timeouts == [PROVIDER_CALL_MAX_S, 40.0]


def test_a_call_with_no_time_left_never_reaches_the_provider() -> None:
    now = [T0 + timedelta(seconds=ATTEMPT_TTL_S - PROVIDER_CALL_MIN_S + 1)]
    recorder = _Recorder()
    ctx = ExecutionContext(_admission(), clock=lambda: now[0], factory=_Factory(recorder), probe=lambda: None)
    with pytest.raises(OutOfTime):
        ctx.client().invoke("a prompt")
    assert recorder.timeouts == []
    assert failure_for(OutOfTime()).code is ErrorCode.PROVIDER_TIMEOUT


def test_an_alias_off_the_allowlist_is_refused_before_any_client_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """C-8, M-G2."""
    monkeypatch.setitem(CATALOG, "deepseek-v4-flash", replace(CATALOG["deepseek-v4-flash"], enabled=True))
    assert workbench_profile("deepseek-v4-flash") is None
    assert workbench_profile(DEFAULT_ALIAS) is CATALOG[DEFAULT_ALIAS]
    factory = _Factory(_Recorder())
    ctx = ExecutionContext(_admission("deepseek-v4-flash"), clock=lambda: T0, factory=factory, probe=lambda: None)
    with pytest.raises(ProviderNotAllowed):
        ctx.client()
    assert factory.asked == []
    assert "deepseek" not in repr(ctx)


def test_the_context_names_its_bound_and_hides_the_brief() -> None:
    ctx = ExecutionContext(_admission(), clock=lambda: T0, factory=_Factory(_Recorder()), probe=lambda: None)
    assert ctx.context_max_chars == tool_invocations.CONTEXT_MAX_CHARS
    assert "a brief" not in repr(ctx) and "a brief" not in repr(ctx.target)
    assert "inv_unit" not in repr(_admission())


# ── I-25, I-21: the wire shape and the judged result ─────────────────────────


def _row(**changes: Any) -> InvocationRow:
    row = InvocationRow(
        owner_id=1, campaign_id="cmp_" + "a" * 22, invocation_id="inv_unit_00000000001",
        conversation_id="cnv_" + "a" * 22, entry_id="ent_" + "a" * 22, tool_id="npc", brief="a brief",
        source_entry_id=None, status="working", attempt=1, attempt_deadline=T0, cancel_requested=False,
        result=None, error=None, schema_version=1, created_at=T0, updated_at=T0,
    )
    return replace(row, **changes)


@pytest.mark.parametrize("changes", [
    {"schema_version": 2},
    {"status": "done", "result": {"tool_id": "npc", "result_kind": "document"}},
    {"status": "failed", "error": {"code": "a_code_from_later", "message": "x", "retryable": True}},
    {"tool_id": "sketch"},
])
def test_a_row_this_build_cannot_read_is_unreadable(changes: dict[str, Any]) -> None:
    with pytest.raises(Unreadable):
        to_wire(_row(**changes))


def test_the_wire_shape_is_in_utc() -> None:
    wire = to_wire(_row(created_at=T0.astimezone(tool_invocations.UTC)))
    assert wire.created_at.utcoffset() == timedelta(0)


def test_a_judged_result_keeps_only_suggestions_that_may_run() -> None:
    produced = {
        "tool_id": "npc", "result_kind": "document", "prose": "x",
        "suggestions": [{"tool_id": "loot", "label": "L"}, {"tool_id": "npc", "label": "N"}, "junk"],
        "document": {"document_id": "doc_" + "a" * 22, "type": "npc", "title": "T", "library_category": "npcs"},
    }
    judged = judge_result(produced, ToolId.NPC, lambda tool: tool is ToolId.NPC)
    assert judged is not None and [s.tool_id for s in judged.suggestions] == [ToolId.NPC]
    assert judge_result(produced, ToolId.ENCOUNTER, lambda tool: True) is None


# ── 1kg.4.4 I-3, I-4: what an executor may lean on ───────────────────────────


def test_s1_an_output_the_executor_refused_is_a_retryable_provider_failure() -> None:
    """Kills: the branch removed (→ backend); `retryable=False`; the final
    message. A mutant mapping every `ValueError` there turns the pinned
    `test_anything_unexpected_is_a_retryable_backend_outage` red."""
    stored = tool_invocations.failure_for(tool_invocations.OutputRefused())
    assert (stored.code, stored.message, stored.retryable) == (
        ErrorCode.PROVIDER_FAILED, tool_invocations.PROVIDER_FAILED_MESSAGE, True)


def test_s2_output_refused_from_run_is_stored_as_provider_failed_and_logged_by_class(
        caplog: pytest.LogCaptureFixture) -> None:
    """Kills: `execute` treating `OutputRefused` as a cancel."""

    class _Refusing(_Executor):
        def run(self, ctx: ExecutionContext) -> Any:
            raise tool_invocations.OutputRefused()

    ctx = ExecutionContext(_admission(), clock=lambda: T0, factory=_Factory(_Recorder()), probe=lambda: None)
    caplog.set_level("DEBUG", logger="service.tool_invocations")
    refusing: Any = _Refusing(ToolId.NPC)
    outcome = tool_invocations.execute(refusing, ctx, available=lambda tool: True)
    assert (outcome.cancelled, outcome.result) == (False, None)
    assert outcome.error is not None and (outcome.error.code, outcome.error.retryable) == (
        ErrorCode.PROVIDER_FAILED, True)
    [logged] = [record.getMessage() for record in caplog.records]
    assert "tool=npc" in logged and "error=OutputRefused" in logged and "a brief" not in logged


class _SpyDatabase:
    """A database that records each transaction's enter and exit."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.units: list[object] = []

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        unit = object()
        self.units.append(unit)
        self.events.append("enter")
        try:
            yield unit
        finally:
            self.events.append("exit")


def test_s3_a_context_read_runs_in_its_own_transaction_closed_before_it_returns() -> None:
    """Kills: reusing a shared unit; returning before the exit; swallowing the
    exception."""
    db = _SpyDatabase()
    spied: Any = db
    ctx = ExecutionContext(_admission(), clock=lambda: T0, factory=_Factory(_Recorder()), probe=lambda: None,
                           reader=tool_invocations.context_reader(spied))
    got: list[object] = []

    def read(unit: object) -> str:
        got.append(unit)
        db.events.append("read")
        return "marker"

    assert ctx.read(read) == "marker"
    assert db.events == ["enter", "read", "exit"]
    assert ctx.read(read) == "marker"
    assert got == db.units and got[0] is not got[1]

    def broken(unit: object) -> str:
        raise LookupError("x")

    with pytest.raises(LookupError):
        ctx.read(broken)
    assert db.events[-2:] == ["enter", "exit"] and len(db.units) == 3


def test_s4_a_context_without_a_reader_refuses_to_read_and_calls_nothing() -> None:
    called: list[object] = []
    ctx = ExecutionContext(_admission(), clock=lambda: T0, factory=_Factory(_Recorder()), probe=lambda: None)
    with pytest.raises(RuntimeError):
        ctx.read(called.append)
    assert called == []
    assert failure_for(RuntimeError()).code is ErrorCode.BACKEND_UNAVAILABLE


def test_s5_the_contexts_now_is_the_routes_clock_and_moves_with_it() -> None:
    now = [T0]
    ctx = ExecutionContext(_admission(), clock=lambda: now[0], factory=_Factory(_Recorder()), probe=lambda: None)
    assert ctx.now() == T0
    now[0] = T0 + timedelta(seconds=41)
    assert ctx.now() == T0 + timedelta(seconds=41)


def test_s7_the_generation_bound_fits_inside_the_executor_context_bound() -> None:
    """1kg.5.4's skipped B-5: the two bounds cannot drift apart."""
    from service import document_generation

    assert document_generation.GENERATION_CONTEXT_MAX_CHARS <= tool_invocations.CONTEXT_MAX_CHARS
    ctx = ExecutionContext(_admission(), clock=lambda: T0, factory=_Factory(_Recorder()), probe=lambda: None)
    assert ctx.context_max_chars == tool_invocations.CONTEXT_MAX_CHARS

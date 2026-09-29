"""The canary harness's sinks: log, stdio and warning capture, the recording fakes, and import hygiene
(agent-forge-harness-1ir.1.10). Each test proves a sink sees what real code hands it, through the
seams the product already has.

These tests emit canaries on purpose, so a suite-wide ``sweep_for_canaries`` excludes this module.

Run from repo root:
    uv run --frozen --no-sync python -m pytest service/tests/test_canary_sinks.py -q
"""

from __future__ import annotations

import ast
import asyncio
import io
import logging
import sys
import threading
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import openai
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from starlette.requests import Request
from typing_extensions import TypedDict

from ingestion.retrieval import embed_query
from service import gcp_logging, tracing
from service.app import get_metrics_sink
from service.generate import generate_result
from service.metrics import (
    LangfuseMetricsSink,
    MetricLabels,
    NumericMetricPoint,
    build_metrics_sink,
    record_safely,
)
from service.providers import ProviderClientFactory
from service.tests.canary import (
    Audience,
    Canary,
    CanaryLeak,
    CanarySet,
    CanaryWorld,
    Finding,
    FindingCategory,
    LeakCapture,
    RecordingLangfuseClient,
    RecordingLLM,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _leaks(raised: pytest.ExceptionInfo[CanaryLeak]) -> list[tuple[str, str | None, str | None]]:
    """(sink, facet, canary label) of every finding, which must all be LEAKs."""
    findings = raised.value.findings
    assert all(f.category is FindingCategory.LEAK for f in findings), [f.line() for f in findings]
    return [(f.sink, f.facet, f.canary.label if f.canary is not None else None) for f in findings]


def _categories(findings: tuple[Finding, ...]) -> list[tuple[str, str, str | None]]:
    return [(f.category.value, f.sink, f.facet) for f in findings]


# ── Logs ─────────────────────────────────────────────────────────────────────


class _HiddenError(Exception):
    """Its ``str`` hides the text; its ``repr`` shows it."""

    def __init__(self, text: str) -> None:
        super().__init__("hidden")
        self.text = text

    def __str__(self) -> str:
        return "hidden"

    def __repr__(self) -> str:
        return f"_HiddenError({self.text!r})"


@dataclass
class _LogCase:
    labels: tuple[str, ...]
    facet: str
    act: Callable[[dict[str, Canary]], None]
    before: Callable[[], Callable[[], None]] | None = None


def _no_propagate() -> Callable[[], None]:
    logger = logging.getLogger("canary_sinks.no_propagate")
    logger.propagate = False

    def undo() -> None:
        logger.propagate = True
    return undo


def _root_at_warning() -> Callable[[], None]:
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.WARNING)
    return lambda: root.setLevel(level)


def _httpx_at_its_default() -> Callable[[], None]:
    """Importing langfuse sets the ``httpx`` logger to WARNING and gives it a console handler of its
    own, for the rest of the process. The harness honours a level set on a logger (as production
    does), so this case restores httpx's defaults for its duration: what it proves is that a
    third-party logger reaches the capture, whichever test imported langfuse first."""
    logger = logging.getLogger("httpx")
    level, handlers = logger.level, list(logger.handlers)
    logger.setLevel(logging.NOTSET)
    for handler in handlers:
        logger.removeHandler(handler)

    def undo() -> None:
        logger.setLevel(level)
        for handler in handlers:
            logger.addHandler(handler)
    return undo


def _chained(c: dict[str, Canary]) -> None:
    try:
        try:
            raise KeyError(c["context"].value)
        except KeyError:
            error = RuntimeError("provider failed")
            error.add_note(c["note"].value)
            raise error from ValueError(c["cause"].value)
    except RuntimeError:
        logging.getLogger("canary_sinks.chain").error("call failed", exc_info=True)


def _from_thread(c: dict[str, Canary]) -> None:
    worker = threading.Thread(target=lambda: logging.getLogger("canary_sinks.thread").info("%s", c["x"].value))
    worker.start()
    worker.join()


def _cleared_list(c: dict[str, Canary]) -> None:
    items = [c["x"].value]
    logging.getLogger("canary_sinks.lazy").info("items: %s", items)
    items.clear()


_LOG_CASES: dict[str, _LogCase] = {
    "httpx": _LogCase(("x",), "record", lambda c: logging.getLogger("httpx").info("HTTP Request: %s", c["x"].value),
                      before=_httpx_at_its_default),
    "openai": _LogCase(("x",), "record", lambda c: logging.getLogger("openai._base_client").debug(
        "Request options: %s", {"json": c["x"].value})),
    "no-propagate": _LogCase(("x",), "record", lambda c: logging.getLogger("canary_sinks.no_propagate").info(
        c["x"].value), before=_no_propagate),
    "debug-under-warning-root": _LogCase(("x",), "record", lambda c: logging.getLogger("canary_sinks.debug").debug(
        c["x"].value), before=_root_at_warning),
    "cause-context-note": _LogCase(("cause", "context", "note"), "exception", _chained),
    "percent-r": _LogCase(("x",), "record", lambda c: logging.getLogger("canary_sinks.r").warning(
        "%r", _HiddenError(c["x"].value))),
    "hidden-exc-info": _LogCase(("x",), "exception", lambda c: logging.getLogger("canary_sinks.hidden").warning(
        "failed", exc_info=_HiddenError(c["x"].value))),
    "extra": _LogCase(("x",), "record", lambda c: logging.getLogger("canary_sinks.extra").info(
        "saved", extra={"field": c["x"].value})),
    "non-string-msg": _LogCase(("x",), "record", lambda c: logging.getLogger("canary_sinks.obj").info(
        {"aside": c["x"].value})),
    "thread": _LogCase(("x",), "record", _from_thread),
    "cleared-list": _LogCase(("x",), "record", _cleared_list),
}


@pytest.mark.parametrize("case", list(_LOG_CASES), ids=list(_LOG_CASES))
def test_logs_capture_every_logger_level_thread_and_part(canary_world: CanaryWorld, case: str) -> None:
    """H-L1."""
    spec = _LOG_CASES[case]
    canaries = {label: canary_world.mint(label) for label in spec.labels}
    undo = spec.before() if spec.before is not None else None
    try:
        with LeakCapture(canary_world) as capture:
            spec.act(canaries)
            with pytest.raises(CanaryLeak) as raised:
                capture.assert_clean()
    finally:
        if undo is not None:
            undo()
    assert sorted(_leaks(raised)) == sorted(("logs", spec.facet, label) for label in spec.labels)


def test_a_stack_info_record_is_captured(canary_world: CanaryWorld) -> None:
    """H-L1's structural pin: ``stack_info`` reaches the ``stack`` facet (no canary involved)."""
    with LeakCapture(canary_world) as capture:
        logging.getLogger("canary_sinks.stack").info("where am I", stack_info=True)
        capture.assert_clean()
    stacks = [c for c in capture.captures("logs") if c.facet == "stack"]
    assert len(stacks) == 1 and "test_a_stack_info_record_is_captured" in str(stacks[0].data)


# ── Standard output and warnings ─────────────────────────────────────────────


def _write_stdout(canary_world: CanaryWorld) -> None:
    emitted, split = canary_world.mint("usage-field"), canary_world.mint("split")
    assert gcp_logging.emit("INFO", "m", SimpleNamespace(headers={}), field=emitted.value) is True
    sys.stdout.write(f"partial {split.token[:9]}")
    sys.stdout.write(f"{split.token[9:]} rest\n")



def test_stdout_captures_the_cloud_run_json_line(
    leak_capture: LeakCapture, canary_world: CanaryWorld, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-L2 through the fixture, set up in pytest's setup phase: pytest swaps sys.stdout between
    phases, so only a capture wired to capteesys still sees the call phase's output (C-2)."""
    monkeypatch.setenv("K_SERVICE", "x")
    _write_stdout(canary_world)
    with pytest.raises(CanaryLeak) as raised:
        leak_capture.assert_clean()
    assert sorted(_leaks(raised)) == [("stdio", "stdout", "split"), ("stdio", "stdout", "usage-field")]


def test_stdout_captures_the_cloud_run_json_line_inside_a_context(
    canary_world: CanaryWorld, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-L2 through ``with LeakCapture(...)`` inside one test phase, where the streams are teed."""
    monkeypatch.setenv("K_SERVICE", "x")
    with LeakCapture(canary_world) as capture:
        _write_stdout(canary_world)
        with pytest.raises(CanaryLeak) as raised:
            capture.assert_clean()
    assert sorted(_leaks(raised)) == [("stdio", "stdout", "split"), ("stdio", "stdout", "usage-field")]


def test_warnings_are_captured_then_reissued(canary_world: CanaryWorld) -> None:
    """H-L3."""
    canary = canary_world.mint("warned")
    with warnings.catch_warnings(record=True) as outer:
        warnings.simplefilter("always")
        with LeakCapture(canary_world) as capture:
            warnings.warn(f"deprecated option {canary.value}", UserWarning, stacklevel=1)
            with pytest.raises(CanaryLeak) as raised:
                capture.assert_clean()
    assert _leaks(raised) == [("warnings", "warning", "warned")]
    assert [str(w.message) for w in outer] == [f"deprecated option {canary.value}"]


def _logging_state() -> tuple[object, ...]:
    root = logging.getLogger()
    quiet = logging.getLogger("canary_sinks.restore")
    return (list(root.handlers), root.level, logging.root.manager.disable, list(quiet.handlers), sys.stdout,
            sys.stderr, list(warnings.filters))


def test_exit_restores_logging_stdio_and_warnings(canary_world: CanaryWorld) -> None:
    """H-L4, for one capture and for two nested ones."""
    quiet = logging.getLogger("canary_sinks.restore")
    quiet.propagate = False
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.ERROR)
    try:
        before = _logging_state()
        with LeakCapture(canary_world) as capture:
            assert _logging_state() != before
            capture.assert_clean()
        assert _logging_state() == before
        with LeakCapture(canary_world) as outer:
            with LeakCapture(canary_world) as inner:
                inner.assert_clean()
            outer.assert_clean()
        assert _logging_state() == before
    finally:
        root.setLevel(level)
        quiet.propagate = True


def _disable(capture: LeakCapture) -> None:
    logging.disable(logging.CRITICAL)


def _remove_handler(capture: LeakCapture) -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if type(h).__name__ == "_RecordHandler"]:
        root.removeHandler(handler)


_SILENCERS: dict[str, tuple[Callable[[LeakCapture], None], list[tuple[str, str, str | None]]]] = {
    "logging-disabled": (_disable, [("probe_lost", "logs", None)]),
    "handler-removed": (_remove_handler, [("probe_lost", "logs", None)]),
}


@pytest.mark.parametrize("case", [*_SILENCERS, "stdout-replaced", "catch-warnings"])
def test_a_silenced_log_capture_is_reported(canary_world: CanaryWorld, case: str) -> None:
    """H-L5 (with C-2's stdout case and C-11's warnings case)."""
    with LeakCapture(canary_world) as capture:
        if case in _SILENCERS:
            silence, expected = _SILENCERS[case]
            silence(capture)
            with pytest.raises(CanaryLeak) as raised:
                capture.assert_clean()
        elif case == "stdout-replaced":
            expected = [("probe_lost", "stdio", "stdout")]
            saved, sys.stdout = sys.stdout, io.StringIO()
            try:
                with pytest.raises(CanaryLeak) as raised:
                    capture.assert_clean()
            finally:
                sys.stdout = saved
        else:
            expected = [("probe_lost", "warnings", None)]
            with warnings.catch_warnings(record=True):
                with pytest.raises(CanaryLeak) as raised:
                    capture.assert_clean()
    assert _categories(raised.value.findings) == expected
    assert logging.root.manager.disable == logging.NOTSET


# ── Metrics and tracing ──────────────────────────────────────────────────────


def _point(release: str) -> NumericMetricPoint:
    return NumericMetricPoint(name="service.chat.duration_ms", kind="numeric", unit="ms", value=12.0,
                              labels=MetricLabels(release=release))


def test_metrics_capture_labels_and_values(canary_world: CanaryWorld) -> None:
    """H-M1: the one free-text label a canary token fits is caught."""
    canary = canary_world.mint("release", max_chars=20)
    with LeakCapture(canary_world) as capture:
        sink = capture.metrics()
        record_safely(sink, _point(canary.token))
        with pytest.raises(CanaryLeak) as raised:
            capture.assert_clean()
    assert _leaks(raised) == [("metrics", "point", "release")]
    assert [p.labels.release for p in sink.points] == [canary.token]


def test_install_metrics_sets_and_restores_app_state(canary_world: CanaryWorld) -> None:
    """H-M2 (with C-12's metrics probe)."""
    app = FastAPI()
    request = Request({"type": "http", "app": app})
    with LeakCapture(canary_world) as capture:
        sink = capture.install_metrics(app)
        assert get_metrics_sink(request) is sink
        capture.assert_clean()
    assert not hasattr(app.state, "metrics_sink")

    previous = object()
    app.state.metrics_sink = previous
    with LeakCapture(canary_world) as capture:
        capture.install_metrics(app)
        capture.assert_clean()
    assert app.state.metrics_sink is previous

    with LeakCapture(canary_world) as capture:
        capture.install_metrics(app)
        app.state.metrics_sink = object()
        with pytest.raises(CanaryLeak) as raised:
            capture.assert_clean()
        del app.state.metrics_sink
    assert _categories(raised.value.findings) == [("probe_lost", "metrics", None)]
    assert not hasattr(app.state, "metrics_sink")


class _State(TypedDict):
    note: str


def test_tracing_reaches_service_tracing_and_records_graph_inputs(leak_capture: LeakCapture,
                                                                  canary_world: CanaryWorld) -> None:
    """H-T1."""
    handler = leak_capture.tracing()
    config = tracing.build_trace_config(model="m", mode="gm")
    assert config["callbacks"] == [handler]
    graph = StateGraph(_State)
    graph.add_node("first", lambda state: {"note": state["note"] + " first"})
    graph.add_node("second", lambda state: {"note": state["note"] + " second"})
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    canary = canary_world.mint("graph-input")
    graph.compile().invoke({"note": canary.value}, config=cast("RunnableConfig", config))
    with pytest.raises(CanaryLeak) as raised:
        leak_capture.assert_clean()
    facets = {facet for sink, facet, label in _leaks(raised) if sink == "trace" and label == "graph-input"}
    assert {"on_chain_start", "on_chain_end"} <= facets


def test_tracing_also_captures_the_langfuse_client(leak_capture: LeakCapture, canary_world: CanaryWorld) -> None:
    """H-T2: no real Langfuse object is built."""
    leak_capture.tracing()
    sink = build_metrics_sink(enabled=True)
    assert isinstance(sink, LangfuseMetricsSink)
    assert type(sink._client) is RecordingLangfuseClient
    canary = canary_world.mint("release", max_chars=20)
    sink.record(_point(canary.token))
    with pytest.raises(CanaryLeak) as raised:
        leak_capture.assert_clean()
    assert {facet for _, facet, _ in _leaks(raised)} == {"start_observation", "create_score"}


def test_tracing_records_the_config_metadata_handed_to_callbacks(leak_capture: LeakCapture,
                                                                 canary_world: CanaryWorld) -> None:
    """H-T1: a canary only in the trace metadata (here ``service_version``) still reaches the recorder."""
    canary = canary_world.mint("version", max_chars=20)
    leak_capture.tracing()
    config = tracing.build_trace_config(model="m", mode="gm", version=canary.token)
    graph = StateGraph(_State)
    graph.add_node("only", lambda state: {"note": state["note"] + " seen"})
    graph.add_edge(START, "only")
    graph.add_edge("only", END)
    graph.compile().invoke({"note": "plain"}, config=cast("RunnableConfig", config))
    with pytest.raises(CanaryLeak) as raised:
        leak_capture.assert_clean()
    facets = {facet for sink, facet, label in _leaks(raised) if sink == "trace" and label == "version"}
    assert "on_chain_start" in facets


def test_tracing_records_every_call_on_a_langfuse_observation(leak_capture: LeakCapture,
                                                              canary_world: CanaryWorld) -> None:
    """H-T2: a method the harness does not name (``update``) is recorded under its own facet."""
    import langfuse

    canary = canary_world.mint("output")
    leak_capture.tracing()
    langfuse.get_client().start_observation(name="span").update(output=canary.value)
    with pytest.raises(CanaryLeak) as raised:
        leak_capture.assert_clean()
    assert _leaks(raised) == [("trace", "observation.update", "output")]


# ── The LLM and embeddings ───────────────────────────────────────────────────


def test_the_llm_records_every_message_and_its_config(leak_capture: LeakCapture, canary_world: CanaryWorld) -> None:
    """H-LLM1 (with C-15: the whole config)."""
    system, metadata, configurable = (canary_world.mint(label) for label in ("system", "metadata", "configurable"))
    llm = leak_capture.llm("player-llm", audience=Audience.PLAYER)
    assert ProviderClientFactory(client_builders={"gpt-4o-mini": llm}).client_for("gpt-4o-mini") is llm
    llm.invoke([SystemMessage(content=f"rules: {system.value}"), HumanMessage(content="hello")])
    llm.invoke("hello", config={"metadata": {"k": metadata.value}, "tags": ["t"]})
    llm.invoke("hello", config={"configurable": {"thread": configurable.value}})
    with pytest.raises(CanaryLeak) as raised:
        leak_capture.assert_clean()
    assert sorted(_leaks(raised)) == [
        ("player-llm", "config", "configurable"),
        ("player-llm", "config", "metadata"),
        ("player-llm", "messages", "system"),
    ]


def _tool_turn(args: dict[str, str]) -> list[BaseMessage]:
    return [HumanMessage(content="look it up"),
            AIMessage(content="", tool_calls=[{"name": "lookup", "args": args, "id": "call-1"}])]


def _invalid_tool_turn(args: str) -> list[BaseMessage]:
    return [HumanMessage(content="look it up"), AIMessage(
        content="", invalid_tool_calls=[{"name": "lookup", "args": args, "id": "call-1", "error": "bad json"}])]


#: Each case puts the canary in exactly one place a provider adapter would send it (H-LLM1).
_LLM_FACETS: dict[str, tuple[str, Callable[[RecordingLLM, Canary], object]]] = {
    "list-content": ("messages", lambda llm, c: llm.invoke(
        [HumanMessage(content=[{"type": "text", "text": c.value}])])),
    "tool-call-args": ("messages", lambda llm, c: llm.invoke(_tool_turn({"q": c.value}))),
    "invalid-tool-call-args": ("messages", lambda llm, c: llm.invoke(_invalid_tool_turn(f'{{"q": "{c.token}"'))),
    "tool-call-id": ("messages", lambda llm, c: llm.invoke(
        [*_tool_turn({"q": "plain"}), ToolMessage(content="found", tool_call_id=c.token)])),
    "dict-input": ("messages", lambda llm, c: llm.invoke({"question": c.value})),
    "stop-kwarg": ("kwargs", lambda llm, c: llm.invoke("hello", stop=[c.value])),
    "ainvoke-kwarg": ("kwargs", lambda llm, c: asyncio.run(llm.ainvoke("hello", stop=[c.value]))),
}


@pytest.mark.parametrize("case", list(_LLM_FACETS), ids=list(_LLM_FACETS))
def test_the_llm_records_every_part_of_its_input(
    leak_capture: LeakCapture, canary_world: CanaryWorld, case: str,
) -> None:
    """H-LLM1: list content, tool calls (valid or not), tool call ids, a dict input and call kwargs."""
    canary = canary_world.mint("payload")
    facet, act = _LLM_FACETS[case]
    llm = leak_capture.llm("player-llm", audience=Audience.PLAYER)
    act(llm, canary)
    with pytest.raises(CanaryLeak) as raised:
        leak_capture.assert_clean()
    assert _leaks(raised) == [("player-llm", facet, "payload")]


def _rate_limited() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)


def _messages_with(capture: LeakCapture, label: str, canary: Canary) -> int:
    return sum(1 for c in capture.captures(label) if c.facet == "messages" and canary.token in str(c.data))


def test_the_llm_records_failed_attempts_before_raising(leak_capture: LeakCapture, canary_world: CanaryWorld) -> None:
    """H-LLM2 (with C-16's async cancellation)."""
    prompt = canary_world.mint("prompt")
    retried = leak_capture.llm("retried", audience=Audience.GM, replies=[_rate_limited(), _rate_limited(), "ok"])
    result = generate_result([HumanMessage(content=prompt.value)], alias="a", client=retried, sleep=lambda s: None)
    assert result.text == "ok" and _messages_with(leak_capture, "retried", prompt) == 3

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    failing = leak_capture.llm("failing", audience=Audience.GM,
                               replies=[openai.APIConnectionError(request=request)] * 3)
    with pytest.raises(openai.APIConnectionError):
        generate_result([HumanMessage(content=prompt.value)], alias="a", client=failing, sleep=lambda s: None)
    assert _messages_with(leak_capture, "failing", prompt) == 3

    cancelled = leak_capture.llm("cancelled", audience=Audience.GM, replies=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled.ainvoke([HumanMessage(content=prompt.value)]))
    assert _messages_with(leak_capture, "cancelled", prompt) == 1
    leak_capture.assert_clean()


def test_embeddings_record_input_through_embed_query(leak_capture: LeakCapture, canary_world: CanaryWorld) -> None:
    """H-E1 (with C-16's scripted error)."""
    query, failed = canary_world.mint("query"), canary_world.mint("failed-query")
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    fake = leak_capture.embeddings("embed", audience=Audience.PLAYER, dimensions=5,
                                   errors=[None, openai.APIConnectionError(request=request)])
    assert embed_query(query.value, client=fake) == [0.1] * 5
    with pytest.raises(openai.APIConnectionError):
        embed_query(failed.value, client=fake)
    with pytest.raises(CanaryLeak) as raised:
        leak_capture.assert_clean()
    assert sorted(_leaks(raised)) == [("embed", "input", "failed-query"), ("embed", "input", "query")]


# ── The remaining sinks ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _AuditRow:
    actor: str
    note: str


def _http(capture: LeakCapture, *, content: bytes = b"{}", headers: list[tuple[str, str]] | None = None,
          url: str = "https://example.test/x") -> None:
    response = httpx.Response(200, content=content, headers=headers or [], request=httpx.Request("GET", url))
    capture.http("sink", response, audience=Audience.PLAYER)


_SINK_CASES: dict[str, tuple[str, bool, Callable[[LeakCapture, Canary], object]]] = {
    "stt-glossary": ("glossary", False, lambda cap, c: cap.stt("sink", audience=Audience.PLAYER).transcribe(
        b"\x00\x01", glossary=["Strahd", c.value])),
    "stt-audio": ("audio", False, lambda cap, c: cap.stt("sink", audience=Audience.PLAYER).transcribe(
        b"RIFF\x00" + c.value.encode())),
    "stt-options": ("options", False, lambda cap, c: cap.stt("sink", audience=Audience.PLAYER).transcribe(
        b"\x00\x01", prompt=c.value)),
    "embeddings-kwargs": ("kwargs", False, lambda cap, c: cap.embeddings("sink", audience=Audience.PLAYER).create(
        model="m", input="plain", user=c.value)),
    "cache-key": ("key", True, lambda cap, c: cap.cache("sink", audience=Audience.PLAYER).get(f"card:{c.token}")),
    "cache-value": ("value", False, lambda cap, c: cap.cache("sink", audience=Audience.PLAYER).set(
        "card:1", {"body": c.value})),
    "channel-frame": ("frame", False, lambda cap, c: cap.channel("sink", audience=Audience.PLAYER).publish(
        c.value.encode(), topic="slot-table")),
    "channel-topic": ("topic", True, lambda cap, c: cap.channel("sink", audience=Audience.PLAYER).publish(
        b"{}", topic=f"slot-{c.token}")),
    "channel-recipient": ("recipient", True, lambda cap, c: cap.channel("sink", audience=Audience.PLAYER).publish(
        b"{}", topic="slot-table", recipient=f"player-{c.token}")),
    "http-body": ("body", False, lambda cap, c: _http(cap, content=c.value.encode())),
    "http-content-disposition": ("header:content-disposition", False, lambda cap, c: _http(
        cap, headers=[("Content-Disposition", f'attachment; filename="{c.token}.pdf"')])),
    "http-second-set-cookie": ("header:set-cookie", True, lambda cap, c: _http(
        cap, headers=[("Set-Cookie", "a=1"), ("Set-Cookie", f"b={c.token}")])),
    "http-url": ("url", True, lambda cap, c: _http(cap, url=f"https://example.test/x?q={c.token}")),
}


@pytest.mark.parametrize("case", [*_SINK_CASES, "rows"])
def test_each_remaining_sink_records_what_it_is_given(canary_world: CanaryWorld, case: str) -> None:
    """H-X1: found under the right facet; with the canary visible, only key-like facets still leak."""
    canary = canary_world.mint("payload")
    with LeakCapture(canary_world) as capture:
        if case == "rows":
            capture.rows("audit", [_AuditRow(actor="gm", note=canary.value)])
            with pytest.raises(CanaryLeak) as raised:
                capture.assert_clean()
            assert _leaks(raised) == [("audit", "row", "payload")]
            return
        facet, key_like, act = _SINK_CASES[case]
        act(capture, canary)
        with pytest.raises(CanaryLeak) as raised:
            capture.assert_clean()
        assert _leaks(raised) == [("sink", facet, "payload")]
        if key_like:
            with pytest.raises(CanaryLeak) as raised:
                capture.assert_clean(visible={"sink": CanarySet([canary])})
            assert _leaks(raised) == [("sink", facet, "payload")]
        else:
            capture.assert_clean(visible={"sink": CanarySet([canary])})


# ── Import hygiene ───────────────────────────────────────────────────────────


def _modules(root: Path, *, skip: tuple[str, ...] = ()) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if any(relative.startswith(prefix) for prefix in skip) or "/.venv/" in f"/{relative}":
            continue
        yield path


def _package_of(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    return ".".join(parts[:-1]) if parts[-1] != "__init__" else ".".join(parts[:-1])


def _imported(path: Path) -> list[str]:
    """Every module name a file imports, relative imports resolved, plus importlib and __import__ strings."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    package = _package_of(path)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1] if package else []
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if called in ("import_module", "__import__") and isinstance(node.args[0].value, str):
                names.append(node.args[0].value)
    return names


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def test_no_production_module_imports_the_harness() -> None:
    """H-B1 (C-5): the production image contains service/tests/**, so this is the only control."""
    product = list(_modules(REPO_ROOT / "service", skip=("service/tests/",)))
    assert len(product) > 30
    offenders = [p.name for p in product if any(_under(n, "service.tests") for n in _imported(p))]
    assert offenders == [], offenders

    elsewhere = [
        *_modules(REPO_ROOT / "ingestion", skip=("ingestion/tests/",)),
        *_modules(REPO_ROOT / "contracts"),
        *_modules(REPO_ROOT / "scripts", skip=("scripts/gcp/tests/",)),
        *(p for p in sorted(REPO_ROOT.glob("*.py")) if p.name != "conftest.py"),
    ]
    offenders = [p.name for p in elsewhere if any(_under(n, "service.tests.canary") for n in _imported(p))]
    assert offenders == [], offenders

    conftest = ast.parse((REPO_ROOT / "conftest.py").read_text(encoding="utf-8"))
    mentions = [n for n in ast.walk(conftest) if isinstance(n, ast.Constant) and "canary" in str(n.value)]
    assert len(mentions) == 1 and mentions[0].value == "service.tests.canary.plugin"
    assignment = conftest.body[-1]
    assert isinstance(assignment, ast.Assign) and ast.unparse(assignment) == \
        "pytest_plugins = ['service.tests.canary.plugin']"

    testpaths = ("service/tests", "ingestion/tests", "tests", "scripts/gcp/tests")
    bare = [p.name for root in testpaths for p in _modules(REPO_ROOT / root)
            if any(_under(n, "canary") or _under(n, "canary_demo_flow") for n in _imported(p))]
    assert bare == [], "import the harness only as service.tests.canary"

    harness = list(_modules(REPO_ROOT / "service" / "tests" / "canary"))
    assert len(harness) == 4
    forbidden = ("service.workbench_registry", "service.policy_oracle", "policy_oracle")
    offenders = [p.name for p in harness if any(_under(n, f) for n in _imported(p) for f in forbidden)]
    assert offenders == [], offenders

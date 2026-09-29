"""The canary harness's stateful half: ``LogCapture``, the recording fakes, and ``LeakCapture``.

Every fake records at the moment it is called, before it returns or raises, and records the raw
``str`` or ``bytes`` it was handed, serialised then (never a parsed object, never later). The STT,
cache and channel fakes are **provisional shapes**: their product interfaces do not exist yet, and the
bead that builds each one (``1ir.4.3``, ``1ir.2.5``, ``1ir.11.2``/``1kg.7.2``) adapts its fake,
keeping these recording semantics (ID-10).

Contributor documentation: ``docs/canary-leak-harness.md``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import secrets
import sys
import threading
import traceback
import warnings
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Any, TextIO, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, MessageLikeRepresentation, convert_to_messages

from service.tests.canary.core import (
    Audience,
    CanaryLeak,
    CanarySet,
    CanaryWorld,
    Capture,
    CaptureNotAsserted,
    Finding,
    FindingCategory,
    HarnessMisuse,
    SinkKind,
    SinkSpec,
    _check_campaign,
    evaluate,
    validate_arguments,
)

if TYPE_CHECKING:
    import httpx
    import pytest
    from fastapi import FastAPI

    from service.metrics import MetricPoint

# ── Serialisation ────────────────────────────────────────────────────────────


def _safe_repr(value: object) -> str:
    try:
        return repr(value)
    except Exception as exc:  # a hostile __repr__ must not break a capture
        return f"<repr failed: {type(exc).__name__}>"


def _default(value: object) -> object:
    """``json.dumps``'s fallback: dataclasses and pydantic models (messages included) as their
    fields, bytes as latin-1, sets as lists, everything else as ``repr``."""
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("latin-1")
    if isinstance(value, set | frozenset):
        return sorted(_safe_repr(item) for item in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return dataclasses.asdict(value)
        except Exception:
            return _safe_repr(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump) and not isinstance(value, type):
        try:
            return model_dump()
        except Exception:
            return _safe_repr(value)
    return _safe_repr(value)


def _dump(value: object, *, sort_keys: bool = False) -> str:
    for sort in (sort_keys, False):
        try:
            return json.dumps(value, default=_default, sort_keys=sort, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError):
            continue
    return _safe_repr(value)


def _text(data: str | bytes) -> str:
    return data.decode("latin-1") if isinstance(data, bytes) else data


# ── LogCapture: logs, stdout/stderr and warnings ─────────────────────────────

_PROBE_LOGGER = "service.tests.canary.probe"
_PROBE_SHAPE = re.compile(r"probe-[0-9a-f]{16}")
_LOG_STACK: list[LogCapture] = []
#: A LogRecord's own attributes; anything else on a record came from ``extra=``.
_RECORD_FIELDS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def _nonce() -> str:
    """``probe-`` and 16 hex characters: not canary-shaped, and too short for a hex needle."""
    return f"probe-{secrets.token_hex(8)}"


def _exception_text(exc: BaseException) -> str:
    """The whole chain: ``format_exception`` (causes, contexts, notes) plus ``repr`` of every link."""
    try:
        parts = ["".join(traceback.format_exception(exc))]
    except Exception as err:
        parts = [f"<format_exception failed: {_safe_repr(err)}>"]
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(f"repr: {_safe_repr(current)}")
        notes = getattr(current, "__notes__", None)
        if notes:
            parts.append(f"notes: {_safe_repr(notes)}")
        pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return "\n".join(parts)


def _format_record(record: logging.LogRecord) -> tuple[str, str | None, str | None]:
    """Everything a record carries, formatted now, while its arguments are what they were when logged."""
    try:
        message = record.getMessage()
    except Exception as exc:
        message = f"<getMessage failed: {_safe_repr(exc)}>"
    lines = [
        f"logger={record.name} level={record.levelname}",
        f"msg={_safe_repr(record.msg)}",
        f"args={_safe_repr(record.args)}",
        f"message={message}",
    ]
    extras = {key: value for key, value in vars(record).items() if key not in _RECORD_FIELDS}
    if extras:
        lines.append(f"extra={_dump(extras, sort_keys=True)}")
    exception: str | None = None
    if record.exc_info and record.exc_info[1] is not None:
        exception = _exception_text(record.exc_info[1])
    elif record.exc_text:
        exception = record.exc_text
    return "\n".join(lines), exception, record.stack_info


class _RecordHandler(logging.Handler):
    def __init__(self, owner: LogCapture) -> None:
        super().__init__(logging.NOTSET)
        self._owner = owner

    def emit(self, record: logging.LogRecord) -> None:
        self._owner._on_record(record)


class _Tee:
    """Stands in for ``sys.stdout``/``sys.stderr``: every write is recorded, then passed through."""

    def __init__(self, target: TextIO, sink: Callable[[str], None]) -> None:
        self._target = target
        self._sink = sink

    def write(self, text: str) -> int:
        self._sink(text if isinstance(text, str) else _safe_repr(text))
        return self._target.write(text)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._target.flush()

    def isatty(self) -> bool:
        return self._target.isatty()

    def fileno(self) -> int:
        return self._target.fileno()

    @property
    def encoding(self) -> str:
        return self._target.encoding

    def __getattr__(self, name: str) -> object:
        return getattr(self._target, name)


class LogCapture:
    """Captures every log record at every level, stdout and stderr, and every warning, while active.

    A handler goes on the root logger and on every logger that exists with ``propagate=False``; the
    root level drops to DEBUG and ``logging.disable`` is lifted. Records are formatted when emitted.
    Without ``stdio_source`` the standard streams are teed; with one (the ``leak_capture`` fixture
    passes pytest's ``capteesys``) the streams are left alone and read from the source instead,
    because pytest swaps ``sys.stdout`` at every phase boundary (C-2). Exit restores exactly what
    entry changed, in LIFO order, then re-issues the captured warnings. Captures belong to the
    TELEMETRY sinks ``logs``, ``stdio`` and ``warnings``.
    """

    def __init__(self, *, stdio_source: Callable[[], tuple[str, str]] | None = None) -> None:
        self._source = stdio_source
        self._lock = threading.Lock()
        self._records: list[Capture] = []
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._log_probes: set[str] = set()
        self._warning_probes: set[str] = set()
        self._warning_log: list[warnings.WarningMessage] = []
        self._install_lost: list[Finding] = []
        self._state = "new"
        self._handler = _RecordHandler(self)
        self._attached: list[logging.Logger] = []
        self._saved_level = logging.NOTSET
        self._saved_disable = logging.NOTSET
        self._saved_streams: tuple[TextIO, TextIO] | None = None
        self._warnings: warnings.catch_warnings[list[warnings.WarningMessage]] | None = None

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> LogCapture:
        if self._state != "new":
            raise HarnessMisuse("a LogCapture is entered once")
        root = logging.getLogger()
        self._saved_level = root.level
        self._saved_disable = logging.root.manager.disable
        root.addHandler(self._handler)
        self._attached = [
            logger for logger in list(logging.root.manager.loggerDict.values())
            if isinstance(logger, logging.Logger) and not logger.propagate
        ]
        for logger in self._attached:
            logger.addHandler(self._handler)
        root.setLevel(logging.DEBUG)
        logging.disable(logging.NOTSET)
        if self._source is None:
            self._saved_streams = (sys.stdout, sys.stderr)
            sys.stdout = cast(TextIO, _Tee(sys.stdout, self._stdout.append))
            sys.stderr = cast(TextIO, _Tee(sys.stderr, self._stderr.append))
        else:
            self._pull()
        self._warnings = warnings.catch_warnings(record=True)
        self._warning_log = self._warnings.__enter__()
        warnings.simplefilter("always")
        _LOG_STACK.append(self)
        self._state = "active"
        self._install_lost = self._probe_logs_and_warnings("install")
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> None:
        self._exit()

    def _is_innermost(self) -> bool:
        return bool(_LOG_STACK) and _LOG_STACK[-1] is self

    def _exit(self) -> None:
        if self._state != "active":
            raise HarnessMisuse("a LogCapture exits once, after it was entered")
        if not self._is_innermost():
            raise HarnessMisuse("captures exit in LIFO order")
        self._pull()
        _LOG_STACK.pop()
        self._state = "closed"
        if self._warnings is not None:
            self._warnings.__exit__(None, None, None)
        if self._saved_streams is not None:
            sys.stdout, sys.stderr = self._saved_streams
        logging.disable(self._saved_disable)
        root = logging.getLogger()
        root.setLevel(self._saved_level)
        for logger in self._attached:
            logger.removeHandler(self._handler)
        root.removeHandler(self._handler)
        for message in list(self._warning_log):
            if str(message.message) not in self._warning_probes:
                warnings.warn_explicit(message.message, message.category, message.filename, message.lineno,
                                       source=message.source)

    # -- recording ------------------------------------------------------------

    def _on_record(self, record: logging.LogRecord) -> None:
        try:
            if record.name == _PROBE_LOGGER and isinstance(record.msg, str) and _PROBE_SHAPE.fullmatch(record.msg):
                with self._lock:
                    self._log_probes.add(record.msg)
                return
            parts = _format_record(record)
        except Exception as exc:  # a record the harness cannot format is still evidence
            parts = (f"<the harness could not format a record from {record.name}: {_safe_repr(exc)}>", None, None)
        with self._lock:
            for facet, text in zip(("record", "exception", "stack"), parts, strict=True):
                if text is not None:
                    self._records.append(
                        Capture("logs", SinkKind.LOG, Audience.TELEMETRY, len(self._records), facet, text)
                    )

    def _pull(self) -> None:
        if self._source is None or self._state == "closed":
            return
        out, err = self._source()
        with self._lock:
            self._stdout.append(out)
            self._stderr.append(err)

    def _stdio(self) -> tuple[str, str]:
        with self._lock:
            return "".join(self._stdout), "".join(self._stderr)

    def captures(self) -> tuple[Capture, ...]:
        """Every log record, the accumulated stdout and stderr, and every warning, captured so far."""
        self._pull()
        with self._lock:
            records = list(self._records)
        out, err = self._stdio()
        result = [*records]
        if out:
            result.append(Capture("stdio", SinkKind.STDIO, Audience.TELEMETRY, 0, "stdout", out))
        if err:
            result.append(Capture("stdio", SinkKind.STDIO, Audience.TELEMETRY, 1, "stderr", err))
        shown = [m for m in list(self._warning_log) if str(m.message) not in self._warning_probes]
        for number, message in enumerate(shown):
            text = (
                f"{message.category.__name__}: {message.message}\n{_safe_repr(message.message)}\n"
                f"{message.filename}:{message.lineno}"
            )
            result.append(Capture("warnings", SinkKind.WARNING, Audience.TELEMETRY, number, "warning", text))
        return tuple(result)

    # -- probes ---------------------------------------------------------------

    def _probe_logs_and_warnings(self, when: str) -> list[Finding]:
        lost: list[Finding] = []
        nonce = _nonce()
        logging.getLogger(_PROBE_LOGGER).debug(nonce)
        with self._lock:
            arrived = nonce in self._log_probes
        if not arrived:
            lost.append(Finding(
                FindingCategory.PROBE_LOST, "logs", Audience.TELEMETRY, SinkKind.LOG,
                detail=f"the probe emitted at {when} was not captured (a handler was removed, a level raised, "
                       "or logging disabled)",
            ))
        nonce = _nonce()
        self._warning_probes.add(nonce)
        try:
            warnings.warn(nonce, UserWarning, stacklevel=1)
        except Warning:
            pass
        if not any(str(message.message) == nonce for message in list(self._warning_log)):
            lost.append(Finding(
                FindingCategory.PROBE_LOST, "warnings", Audience.TELEMETRY, SinkKind.WARNING,
                detail=f"the warning probe sent at {when} was not captured (a catch_warnings or a filter "
                       "replaced the harness's)",
            ))
        return lost

    def _probe_stdio(self) -> list[Finding]:
        lost: list[Finding] = []
        nonces = {"stdout": _nonce(), "stderr": _nonce()}
        for stream, facet in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
            try:
                stream.write(nonces[facet] + "\n")
                stream.flush()
            except Exception:
                continue
        self._pull()
        for facet, text in zip(("stdout", "stderr"), self._stdio(), strict=True):
            if nonces[facet] not in text:
                lost.append(Finding(
                    FindingCategory.PROBE_LOST, "stdio", Audience.TELEMETRY, SinkKind.STDIO, facet=facet,
                    detail=f"the {facet} probe written at assert time was not captured (sys.{facet} was "
                           "replaced, or the capture is not wired to pytest's)",
                ))
        return lost

    def check_probes(self) -> list[Finding]:
        """Send the log, warning and stdio probes and report each that did not arrive (plus install-time losses)."""
        if self._state != "active":
            raise HarnessMisuse("probes are checked while the capture is active")
        lost, self._install_lost = self._install_lost, []
        return [*lost, *self._probe_logs_and_warnings("assert time"), *self._probe_stdio()]


# ── The recording fakes ──────────────────────────────────────────────────────


class _SinkWriter:
    __slots__ = ("_capture", "label")

    def __init__(self, capture: LeakCapture, label: str) -> None:
        self._capture = capture
        self.label = label

    def record(self, facet: str, data: str | bytes) -> None:
        self._capture._append(self.label, facet, data)


def _as_messages(value: object) -> list[BaseMessage] | None:
    if isinstance(value, str):
        return [HumanMessage(content=value)]
    if isinstance(value, BaseMessage):
        return [value]
    to_messages = getattr(value, "to_messages", None)
    try:
        if callable(to_messages):
            return list(to_messages())
        if isinstance(value, Sequence):
            return convert_to_messages(cast("Sequence[MessageLikeRepresentation]", value))
    except Exception:
        return None
    return None


def _message_record(message: BaseMessage) -> dict[str, object]:
    content = message.content
    record: dict[str, object] = {
        "role": message.type,
        "content": content if isinstance(content, str) else _dump(content),
    }
    for extra in ("name", "additional_kwargs", "tool_calls"):
        value = getattr(message, extra, None)
        if value:
            record[extra] = value
    return record


def _config_record(config: object) -> str:
    """Every RunnableConfig key; callbacks reduced to their class names (C-15)."""
    if isinstance(config, Mapping):
        rendered: dict[str, object] = {}
        for key, value in config.items():
            if key == "callbacks" and isinstance(value, list | tuple):
                rendered[str(key)] = [type(callback).__name__ for callback in value]
            elif key == "callbacks" and value is not None:
                rendered[str(key)] = type(value).__name__
            else:
                rendered[str(key)] = value
        return _dump(rendered)
    return _dump(config)


class RecordingLLM:
    """``service.generate.LLMClient``: records ``messages``, ``config`` (key-like) and ``kwargs``, then replies.

    A scripted exception is raised after recording; a callable reply is given the messages; when the
    script runs out the last reply repeats."""

    def __init__(
        self,
        writer: _SinkWriter,
        replies: Sequence[str | BaseException] | Callable[[Sequence[BaseMessage]], str],
    ) -> None:
        self._writer = writer
        self._reply_fn: Callable[[Sequence[BaseMessage]], str] | None = None
        self._script: tuple[str | BaseException, ...] = ()
        if isinstance(replies, str):
            raise HarnessMisuse("replies is a sequence of replies or a callable, not one string")
        if callable(replies):
            self._reply_fn = replies
        else:
            self._script = tuple(replies)
            if not self._script:
                raise HarnessMisuse("replies needs at least one reply")
        self.calls = 0

    def _record(self, value: object, config: object, kwargs: Mapping[str, object]) -> list[BaseMessage]:
        messages = _as_messages(value)
        self.calls += 1
        if messages is None:
            self._writer.record("messages", _dump({"unparsed_input": _safe_repr(value)}))
        else:
            self._writer.record("messages", _dump([_message_record(message) for message in messages]))
        self._writer.record("config", _config_record(config))
        self._writer.record("kwargs", _safe_repr(dict(kwargs)))
        return messages or []

    def _reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        if self._reply_fn is not None:
            return AIMessage(content=self._reply_fn(messages))
        item = self._script[min(self.calls, len(self._script)) - 1]
        if isinstance(item, BaseException):
            raise item
        return AIMessage(content=item)

    def invoke(self, input: object, config: object = None, **kwargs: object) -> AIMessage:
        return self._reply(self._record(input, config, kwargs))

    async def ainvoke(self, input: object, config: object = None, **kwargs: object) -> AIMessage:
        messages = self._record(input, config, kwargs)
        await asyncio.sleep(0)
        return self._reply(messages)


class RecordingEmbeddings:
    """The OpenAI client shape ``embed_query`` uses: ``.embeddings.create(*, model, input)``.

    Records every input string under ``input`` and the model under ``model``; each call takes the next
    item of ``errors`` (an exception is raised after recording, ``None`` succeeds)."""

    def __init__(self, writer: _SinkWriter, *, dimensions: int, errors: Sequence[BaseException | None]) -> None:
        if dimensions < 1:
            raise HarnessMisuse("dimensions is at least 1")
        self._writer = writer
        self._dimensions = dimensions
        self._errors = tuple(errors)
        self.calls = 0

    @property
    def embeddings(self) -> RecordingEmbeddings:
        return self

    def create(self, *, model: str, input: object, **kwargs: object) -> SimpleNamespace:
        items: list[object] = list(input) if isinstance(input, list | tuple) else [input]
        for item in items:
            self._writer.record("input", item if isinstance(item, str | bytes) else _dump(item))
        self._writer.record("model", str(model))
        if kwargs:
            self._writer.record("kwargs", _safe_repr(kwargs))
        self.calls += 1
        error = self._errors[self.calls - 1] if self.calls <= len(self._errors) else None
        if error is not None:
            raise error
        used = len(items)
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1] * self._dimensions) for _ in items],
            usage=SimpleNamespace(prompt_tokens=used, total_tokens=used),
        )


class RecordingSTT:
    """Provisional (``1ir.4.3`` owns the real interface): ``transcribe(audio, *, glossary=(), **options)``."""

    def __init__(self, writer: _SinkWriter, transcripts: Sequence[str | BaseException]) -> None:
        if isinstance(transcripts, str) or not transcripts:
            raise HarnessMisuse("transcripts is a non-empty sequence")
        self._writer = writer
        self._script = tuple(transcripts)
        self.calls = 0

    def transcribe(self, audio: bytes, *, glossary: Sequence[str] = (), **options: object) -> str:
        self._writer.record("audio", bytes(audio))
        self._writer.record("glossary", _dump(list(glossary)))
        self._writer.record("options", _safe_repr(options))
        self.calls += 1
        item = self._script[min(self.calls, len(self._script)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item


class RecordingCache:
    """Provisional (``1ir.2.5`` owns the real interface): a dict-backed ``get``/``set``/``delete``.

    Every key is recorded under the key-like facet ``key``; every stored value under ``value``."""

    def __init__(self, writer: _SinkWriter) -> None:
        self._writer = writer
        self._data: dict[str, object] = {}

    def get(self, key: str) -> object | None:
        self._writer.record("key", key)
        return self._data.get(key)

    def set(self, key: str, value: object, *, ttl_s: float | None = None) -> None:
        self._writer.record("key", key)
        self._writer.record("value", _dump(value))
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._writer.record("key", key)
        self._data.pop(key, None)


class RecordingChannel:
    """Provisional (``1ir.11.2`` and ``1kg.7.2`` own the real interface): ``publish(frame, *, topic, recipient)``.

    Frames are recorded as the bytes that would be sent, never parsed. Use one sink per recipient."""

    def __init__(self, writer: _SinkWriter) -> None:
        self._writer = writer
        self.frames: list[bytes | str] = []

    def publish(self, frame: bytes | str, *, topic: str, recipient: str | None = None) -> None:
        self._writer.record("frame", frame if isinstance(frame, str | bytes) else _safe_repr(frame))
        self._writer.record("topic", topic)
        if recipient is not None:
            self._writer.record("recipient", recipient)
        self.frames.append(frame)


class RecordingMetricsSink:
    """``service.metrics.MetricsSink``: records each point's JSON under ``point`` and keeps the objects."""

    def __init__(self, writer: _SinkWriter) -> None:
        self._writer = writer
        self.points: list[MetricPoint] = []

    def record(self, point: MetricPoint) -> None:
        self._writer.record("point", point.model_dump_json())
        self.points.append(point)


#: LangChain's callback signatures differ per event and are LangChain's to change; one recorder
#: takes them all, and ``*args: Any, **kwargs: Any`` is the only override mypy accepts for each.
_CallbackArg = Any  # justification: a catch-all override of every BaseCallbackHandler event signature


class RecordingTraceHandler(BaseCallbackHandler):
    """Records every callback event, facet named after the callback, as JSON of every argument.

    It sees everything a Langfuse handler would be handed (a superset of what Langfuse exports after
    masking), so it models SEC-24's *absent* option only. It never raises."""

    def __init__(self, writer: _SinkWriter) -> None:
        super().__init__()
        self._writer = writer

    def _event(self, name: str, args: tuple[object, ...], kwargs: Mapping[str, object]) -> None:
        try:
            self._writer.record(name, _dump({"args": list(args), **{str(k): v for k, v in kwargs.items()}}))
        except Exception as exc:
            self._writer.record(name, f"<unserialisable {name}: {_safe_repr(exc)}>")

    def on_chain_start(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_chain_start", args, kwargs)

    def on_chain_end(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_chain_end", args, kwargs)

    def on_chain_error(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_chain_error", args, kwargs)

    def on_llm_start(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_llm_start", args, kwargs)

    def on_chat_model_start(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_chat_model_start", args, kwargs)

    def on_llm_new_token(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_llm_new_token", args, kwargs)

    def on_stream_event(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_stream_event", args, kwargs)

    def on_llm_end(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_llm_end", args, kwargs)

    def on_llm_error(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_llm_error", args, kwargs)

    def on_tool_start(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_tool_start", args, kwargs)

    def on_tool_end(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_tool_end", args, kwargs)

    def on_tool_error(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_tool_error", args, kwargs)

    def on_agent_action(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_agent_action", args, kwargs)

    def on_agent_finish(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_agent_finish", args, kwargs)

    def on_retriever_start(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_retriever_start", args, kwargs)

    def on_retriever_end(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_retriever_end", args, kwargs)

    def on_retriever_error(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_retriever_error", args, kwargs)

    def on_text(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_text", args, kwargs)

    def on_retry(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_retry", args, kwargs)

    def on_custom_event(self, *args: _CallbackArg, **kwargs: _CallbackArg) -> None:
        self._event("on_custom_event", args, kwargs)


def _call_record(args: tuple[object, ...], kwargs: Mapping[str, object]) -> str:
    return _dump({"args": list(args), "kwargs": dict(kwargs)})


class _Observation:
    """What ``start_observation`` returns: an id, a trace id, and methods that record their calls."""

    def __init__(self, writer: _SinkWriter, number: int) -> None:
        self._writer = writer
        self.id = f"observation-{number}"
        self.trace_id = f"trace-{number}"

    def end(self, *args: object, **kwargs: object) -> None:
        self._writer.record("observation.end", _call_record(args, kwargs))

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: object, **kwargs: object) -> None:
            self._writer.record(f"observation.{name}", _call_record(args, kwargs))

        return call


class RecordingLangfuseClient:
    """What ``langfuse.get_client()`` users call: every method call is one capture, facet = method name."""

    def __init__(self, writer: _SinkWriter) -> None:
        self._writer = writer
        self._observations = 0

    def _observe(self, name: str, args: tuple[object, ...], kwargs: Mapping[str, object]) -> _Observation:
        self._writer.record(name, _call_record(args, kwargs))
        self._observations += 1
        return _Observation(self._writer, self._observations)

    def start_observation(self, *args: object, **kwargs: object) -> _Observation:
        return self._observe("start_observation", args, kwargs)

    def create_score(self, *args: object, **kwargs: object) -> None:
        self._writer.record("create_score", _call_record(args, kwargs))

    def flush(self, *args: object, **kwargs: object) -> None:
        self._writer.record("flush", _call_record(args, kwargs))

    def __getattr__(self, name: str) -> Callable[..., _Observation]:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: object, **kwargs: object) -> _Observation:
            return self._observe(name, args, kwargs)

        return call


# ── LeakCapture ──────────────────────────────────────────────────────────────

_RESERVED: dict[str, SinkKind] = {"logs": SinkKind.LOG, "stdio": SinkKind.STDIO, "warnings": SinkKind.WARNING}
_LEAK_STACK: list[LeakCapture] = []
#: The longest needle (a 64-character hex digest) less one: how far before a stdio watermark the
#: late scan starts, so a needle straddling it is still found (C-7; the brief's 19 covers tokens only).
_LATE_OVERLAP = 63


@dataclasses.dataclass(frozen=True)
class _MetricsInstall:
    app: FastAPI
    sink: RecordingMetricsSink
    label: str
    had_previous: bool
    previous: object


class LeakCapture:
    """Owns the sinks of one test, runs the policy, and raises ``CanaryLeak`` with every finding.

    Use the ``leak_capture`` fixture (request it first), or ``with LeakCapture(world) as capture:``
    inside one test phase. ``assert_clean`` may be called more than once; captures made after the
    last call are still scanned at exit (C-7)."""

    def __init__(
        self,
        world: CanaryWorld,
        *,
        monkeypatch: pytest.MonkeyPatch | None = None,
        stdio_source: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        if not isinstance(world, CanaryWorld):
            raise HarnessMisuse("a LeakCapture needs a CanaryWorld")
        self._world = world
        self._monkeypatch = monkeypatch
        self._logs = LogCapture(stdio_source=stdio_source)
        self._lock = threading.Lock()
        self._sinks: dict[str, SinkSpec] = {
            label: SinkSpec(label, kind, Audience.TELEMETRY) for label, kind in _RESERVED.items()
        }
        self._reusable: set[str] = set()
        self._captures: list[Capture] = []
        self._counts: dict[str, int] = {}
        self._state = "new"
        self._asserted = False
        self._installs: list[_MetricsInstall] = []
        self._tracing: RecordingTraceHandler | None = None
        self._marks: dict[str, int] | None = None
        self._stdio_marks: dict[str, int] = {}
        self._last_visible: dict[str, CanarySet] = {}
        self._last_expect_empty: frozenset[str] = frozenset()

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> LeakCapture:
        if self._state != "new":
            raise HarnessMisuse("a LeakCapture is entered once")
        self._logs.__enter__()
        _LEAK_STACK.append(self)
        self._state = "active"
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> None:
        self._exit(call_failed=exc_type is not None)

    def _exit(self, *, call_failed: bool) -> None:
        """Undo metrics, then the log capture (which re-issues warnings), then raise if the test left
        the capture unasserted or something leaked after its last ``assert_clean`` (C-11)."""
        if self._state == "new":
            raise HarnessMisuse("a LeakCapture exits after it was entered")
        if self._state == "closed":
            raise HarnessMisuse("a LeakCapture exits once")
        if not _LEAK_STACK or _LEAK_STACK[-1] is not self or not self._logs._is_innermost():
            raise HarnessMisuse("captures exit in LIFO order")
        late: list[Finding] = []
        try:
            if not call_failed:
                late = self._late_findings()
        finally:
            _LEAK_STACK.pop()
            self._state = "closed"
            self._undo_metrics()
            self._logs._exit()
        if call_failed:
            return
        if not self._asserted:
            raise CaptureNotAsserted(
                "the canary capture was used but assert_clean never ran, so nothing it recorded was checked"
            )
        if late:
            raise CanaryLeak(late, late=True)

    def _require_active(self, what: str) -> None:
        if self._state == "new":
            raise HarnessMisuse(f"{what} before the capture was entered")
        if self._state == "closed":
            raise HarnessMisuse(f"{what} after the capture exited")

    # -- sinks ----------------------------------------------------------------

    def _declare(
        self, label: str, kind: SinkKind, audience: Audience, campaign: str | None, *, reusable: bool, what: str,
    ) -> _SinkWriter:
        self._require_active(what)
        if not isinstance(label, str) or not label:
            raise HarnessMisuse("a sink label is a non-empty string")
        if label in _RESERVED:
            raise HarnessMisuse(f"the label {label!r} is reserved for the log capture")
        if not isinstance(audience, Audience):
            raise HarnessMisuse("audience must be an Audience")
        if campaign is not None:
            _check_campaign(campaign)
        spec = SinkSpec(label, kind, audience, campaign)
        existing = self._sinks.get(label)
        if existing is not None:
            if reusable and label in self._reusable and existing == spec:
                return _SinkWriter(self, label)
            raise HarnessMisuse(f"the sink label {label!r} is already declared in this capture")
        self._sinks[label] = spec
        if reusable:
            self._reusable.add(label)
        return _SinkWriter(self, label)

    def _append(self, label: str, facet: str, data: str | bytes) -> None:
        with self._lock:
            spec = self._sinks[label]
            index = self._counts.get(label, 0)
            self._counts[label] = index + 1
            self._captures.append(Capture(label, spec.kind, spec.audience, index, facet, data))

    def llm(
        self,
        label: str,
        *,
        audience: Audience,
        campaign: str | None = None,
        replies: Sequence[str | BaseException] | Callable[[Sequence[BaseMessage]], str] = ("ok",),
    ) -> RecordingLLM:
        return RecordingLLM(self._declare(label, SinkKind.LLM, audience, campaign, reusable=False, what="llm()"),
                            replies)

    def embeddings(
        self,
        label: str,
        *,
        audience: Audience,
        campaign: str | None = None,
        dimensions: int = 3,
        errors: Sequence[BaseException | None] = (),
    ) -> RecordingEmbeddings:
        writer = self._declare(label, SinkKind.EMBEDDING, audience, campaign, reusable=False, what="embeddings()")
        return RecordingEmbeddings(writer, dimensions=dimensions, errors=errors)

    def stt(
        self,
        label: str,
        *,
        audience: Audience,
        campaign: str | None = None,
        transcripts: Sequence[str | BaseException] = ("",),
    ) -> RecordingSTT:
        return RecordingSTT(self._declare(label, SinkKind.STT, audience, campaign, reusable=False, what="stt()"),
                            transcripts)

    def cache(self, label: str, *, audience: Audience, campaign: str | None = None) -> RecordingCache:
        return RecordingCache(self._declare(label, SinkKind.CACHE, audience, campaign, reusable=False, what="cache()"))

    def channel(self, label: str, *, audience: Audience, campaign: str | None = None) -> RecordingChannel:
        return RecordingChannel(
            self._declare(label, SinkKind.CHANNEL, audience, campaign, reusable=False, what="channel()")
        )

    def metrics(self, label: str = "metrics") -> RecordingMetricsSink:
        """A TELEMETRY metrics sink; wire it yourself, or use ``install_metrics``."""
        return RecordingMetricsSink(
            self._declare(label, SinkKind.METRIC, Audience.TELEMETRY, None, reusable=False, what="metrics()")
        )

    def install_metrics(self, app: FastAPI, label: str = "metrics") -> RecordingMetricsSink:
        """Set ``app.state.metrics_sink``; exit restores the previous value, or deletes the attribute."""
        self._require_active("install_metrics()")
        if any(install.app is app for install in self._installs):
            raise HarnessMisuse("install_metrics was already called for this app in this capture")
        sink = self.metrics(label)
        had_previous = hasattr(app.state, "metrics_sink")
        previous = getattr(app.state, "metrics_sink", None)
        app.state.metrics_sink = sink
        self._installs.append(_MetricsInstall(app, sink, label, had_previous, previous))
        return sink

    def _undo_metrics(self) -> None:
        for install in reversed(self._installs):
            if not hasattr(install.app.state, "metrics_sink"):
                continue
            if install.had_previous:
                install.app.state.metrics_sink = install.previous
            else:
                del install.app.state.metrics_sink

    def _metrics_probe(self) -> list[Finding]:
        return [
            Finding(
                FindingCategory.PROBE_LOST, install.label, Audience.TELEMETRY, SinkKind.METRIC,
                detail="app.state.metrics_sink is no longer the recording sink (a lifespan or a test replaced it)",
            )
            for install in self._installs
            if getattr(install.app.state, "metrics_sink", None) is not install.sink
        ]

    def tracing(self, label: str = "trace") -> RecordingTraceHandler:
        """Turn tracing on and route it into recorders: ``RAG_TRACING=1``, ``langfuse.langchain.CallbackHandler``
        and ``langfuse.get_client`` (third-party names only, through monkeypatch; ID-9)."""
        self._require_active("tracing()")
        if self._tracing is not None:
            raise HarnessMisuse("tracing() is installed once per capture")
        if self._monkeypatch is None:
            raise HarnessMisuse("tracing() patches through monkeypatch; pass monkeypatch= or use leak_capture")
        writer = self._declare(label, SinkKind.TRACE, Audience.TELEMETRY, None, reusable=False, what="tracing()")
        handler = RecordingTraceHandler(writer)
        client = RecordingLangfuseClient(writer)
        self._monkeypatch.setenv("RAG_TRACING", "1")
        self._monkeypatch.setattr("langfuse.langchain.CallbackHandler", lambda *args, **kwargs: handler)
        self._monkeypatch.setattr("langfuse.get_client", lambda *args, **kwargs: client)
        self._tracing = handler
        return handler

    def http(
        self, label: str, response: httpx.Response, *, audience: Audience, campaign: str | None = None,
    ) -> None:
        """The raw body bytes, every header (duplicates included), and the request URL (key-like)."""
        writer = self._declare(label, SinkKind.HTTP, audience, campaign, reusable=True, what="http()")
        writer.record("body", bytes(response.content))
        for name, value in response.headers.multi_items():
            writer.record(f"header:{name.lower()}", value)
        try:
            request = response.request
        except RuntimeError:
            return
        writer.record("url", str(request.url))

    def rows(
        self, label: str, rows: Iterable[object], *, audience: Audience = Audience.TELEMETRY, facet: str = "row",
    ) -> None:
        """One capture per row: JSON, dataclasses as their fields, sorted keys (audit rows, outbox payloads)."""
        writer = self._declare(label, SinkKind.ROWS, audience, None, reusable=True, what="rows()")
        for row in rows:
            writer.record(facet, _dump(row, sort_keys=True))

    def record(
        self,
        label: str,
        *,
        kind: SinkKind,
        audience: Audience,
        facet: str,
        data: str | bytes,
        campaign: str | None = None,
    ) -> None:
        """The generic escape hatch: one capture of exactly ``data`` (for example SQL parameters)."""
        if not isinstance(data, str | bytes):
            raise HarnessMisuse("record() takes str or bytes; serialise it first")
        self._declare(label, kind, audience, campaign, reusable=True, what="record()").record(facet, data)

    def captures(self, label: str | None = None) -> tuple[Capture, ...]:
        if label is not None and label not in self._sinks:
            raise HarnessMisuse(f"{label!r} is not a sink of this capture")
        return tuple(capture for capture in self._all_captures() if label is None or capture.sink == label)

    def _all_captures(self) -> list[Capture]:
        with self._lock:
            own = list(self._captures)
        return [*own, *self._logs.captures()]

    # -- checking -------------------------------------------------------------

    def assert_clean(
        self,
        *,
        visible: Mapping[str, CanarySet] | None = None,
        must_see: Mapping[str, CanarySet] | None = None,
        expect_empty: Collection[str] = (),
    ) -> None:
        """Probe, scan everything captured so far, and raise ``CanaryLeak`` with every finding."""
        self._require_active("assert_clean()")
        visible_map = dict(visible or {})
        must_map = dict(must_see or {})
        sinks = dict(self._sinks)
        validate_arguments(sinks, self._world, visible=visible_map, must_see=must_map, expect_empty=expect_empty)
        self._asserted = True
        lost = [*self._logs.check_probes(), *self._metrics_probe()]
        captures = self._all_captures()
        findings = evaluate(
            captures, sinks, self._world, visible=visible_map, must_see=must_map, expect_empty=expect_empty,
        )
        marks: dict[str, int] = {}
        for capture in captures:
            if capture.sink == "stdio":
                self._stdio_marks[capture.facet] = len(capture.data)
            else:
                marks[capture.sink] = marks.get(capture.sink, 0) + 1
        self._marks = marks
        self._last_visible = visible_map
        self._last_expect_empty = frozenset(expect_empty)
        if findings or lost:
            raise CanaryLeak([*findings, *lost])

    def _late_findings(self) -> list[Finding]:
        """What arrived after the last ``assert_clean``, under that call's ``visible`` and ``expect_empty``."""
        if not self._asserted or self._marks is None:
            return []
        late: list[Capture] = []
        floors: dict[int, int] = {}
        for capture in self._all_captures():
            if capture.sink == "stdio":
                text = _text(capture.data)
                mark = self._stdio_marks.get(capture.facet, 0)
                if len(text) <= mark:
                    continue
                start = max(0, mark - _LATE_OVERLAP)
                floors[len(late)] = mark - start
                late.append(dataclasses.replace(capture, data=text[start:]))
            elif capture.index >= self._marks.get(capture.sink, 0):
                late.append(capture)
        return evaluate(
            late, dict(self._sinks), self._world, visible=self._last_visible,
            expect_empty=self._last_expect_empty, check_presence=False, min_end=floors,
        )

"""The job driver's contract, pinned where `test_job_driver.py` leaves it open
(agent-forge-harness-1kg.2.7.2, checkpoint B of 1kg.2.7).

A separate file because other beads edit `test_job_driver.py`; nothing here
imports its helpers, so neither file's edits can break the other.

- **Nothing existing changed.** The routes declared before the job runner
  arrived, the middleware stack and `capture_chat_metrics` are pinned exactly as
  they were at `a9ebf63`, the integration head before 1kg.2.7 merged. This is a
  characterization pin, not a must-stay-green suite: a later, reviewed bead that
  deliberately changes a pinned declaration updates its row in the same commit
  and says why.
- **"At once" has a bound.** Every competing driver finishes within
  `BOUND_S` while a job is blocked, and a spy lock proves it asked without
  waiting: a bounded wait would also finish in time.
- **A failed claim after a commit gives the lock back**; only a schema this
  build understands runs jobs; the lock is injected, never imported; and a
  degraded instance gets its driver back on recovery.

Nothing here dials a database: the queue is the in-memory twin, `migrate` is a
stub, and the recovered stores are never asked for a connection.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import logging
import textwrap
import threading
import tokenize
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout
from starlette.middleware.base import BaseHTTPMiddleware

import service.app as appmod
from service import job_driver, jobs
from service.body_limit import BodyLimitMiddleware
from service.db import Database
from service.job_driver import (
    JOB_LOCK,
    SCHEDULER_PATH,
    SCHEDULER_SECRET_ENV,
    SCHEDULER_SECRET_HEADER,
    JobDriver,
    JobHookMiddleware,
    SchedulerRoute,
    build_router,
)
from service.jobs import LEASE_SECONDS, InMemoryJobQueue, Job, JobContext, JobHandler, JobRunner, JobRunResult
from service.migrations import Report
from service.workbench_api import api_route_dependants, install_workbench

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
SECRET = "scheduler-secret-long-enough-to-be-accepted-0123456789"
KIND = "asset.delete"
#: How long a competing driver may take while a job is blocked. It answers in
#: milliseconds; the margin is for a starved CPU, not for a wait.
BOUND_S = 5.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _CountingQueue(InMemoryJobQueue):
    """The twin, counting every claim, including one that finds nothing."""

    claims: int = 0
    raises: Exception | None = None

    def claim(
        self,
        kinds: Collection[str],
        *,
        limit: int = 1,
        job_id: int | None = None,
        lease_seconds: int = LEASE_SECONDS,
        now: datetime | None = None,
    ) -> list[Job]:
        self.claims += 1
        if self.raises is not None:
            raise self.raises
        return super().claim(kinds, limit=limit, job_id=job_id, lease_seconds=lease_seconds, now=now)


class _SpyLock:
    """One real lock, recording each `acquire` as `(blocking, timeout, got)`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[tuple[bool, float, bool]] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        got = self._lock.acquire(blocking, timeout)
        self.calls.append((blocking, timeout, got))
        return got

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def as_lock(self) -> threading.Lock:
        # justification: typeshed's `threading.Lock` is the @final
        # `_thread.lock`, so a spy cannot subclass it. The runner and the driver
        # call only acquire, release and locked, which this delegates.
        return cast("threading.Lock", self)


def _enqueue(queue: InMemoryJobQueue, n: int = 1) -> list[int]:
    ids = []
    for i in range(n):
        with queue.db.transaction() as unit:
            ids.append(queue.enqueue(unit, KIND, {"asset_id": f"asset-{i}"}, now=T0))
    return ids


def _driver(
    queue: InMemoryJobQueue,
    handler: Callable[[Job, JobContext], None] | None = None,
    *,
    clock: _Clock | None = None,
    lock: threading.Lock | None = None,
    healthy: Callable[[], bool] = lambda: True,
) -> JobDriver:
    """A runner and a driver sharing ONE injected lock, as `_build_stores` wires them."""
    clock = clock if clock is not None else _Clock()
    lock = lock if lock is not None else threading.Lock()
    handlers = {KIND: JobHandler(handler or (lambda job, context: None))}
    runner = JobRunner(queue, handlers, clock=lambda: T0, monotonic=clock, single_flight=lock)
    return JobDriver(runner, lock=lock, clock=clock, healthy=healthy)


def _probe(driver: JobDriver) -> FastAPI:
    """The hook around one signed-in route: `auth_user` is what `require_session` leaves."""
    probe = FastAPI()
    probe.add_middleware(JobHookMiddleware, driver=lambda: driver)

    @probe.get("/things")
    def things(request: Request) -> dict[str, bool]:
        request.state.auth_user = object()
        return {"ok": True}

    return probe


def _scheduler_app(driver: JobDriver) -> FastAPI:
    """Assembled as `service/app.py` assembles it: the Workbench handlers, then the scheduler router."""
    target = FastAPI()
    install_workbench(target)
    target.include_router(build_router(lambda: driver))
    return target


# ── Nothing that existed before the job runner changed (G1) ─────────────────

#: `(class, endpoint, response model, status code, in schema, dependencies)` of
#: every route declared before 1kg.2.7, derived on `a9ebf63`. Dependencies are
#: the effective ones, in order, for the legacy routes only: the Workbench
#: routes' router-level posture is pinned by `test_workbench_api.py`.
_Row = tuple[str, str, str, int | None, bool, tuple[str, ...] | None]
_PRE_BEAD: dict[tuple[str, str], _Row] = {
    ("GET", "/healthz"): ("APIRoute", "healthz", "dict", None, True, ()),
    ("GET", "/models"): ("APIRoute", "get_models", "dict", None, True, ()),
    # The body is read by a dependency, after the session check (agent-forge-harness-dl7x).
    # agent-forge-harness-u2uj added `get_usage_day`: the daily caps now read the
    # usage ledger instead of `get_message_store.calls_today()`.
    ("POST", "/chat"): ("APIRoute", "chat", "ChatResponse", None, True, (
        "_chat_request", "get_service", "get_message_store", "get_metrics_sink", "require_session",
        "get_timeline_store", "get_timeline_database", "get_usage_day")),
    ("POST", "/metrics/ui"): ("APIRoute", "record_ui_metrics", "dict", 202, True, ("get_metrics_sink",)),
    ("GET", "/conversations/{conversation_id}/messages"): (
        "APIRoute", "conversation_messages", "MessagesResponse", None, True, ("get_message_store", "require_session")),
    # The body is read by a dependency, after the session check (agent-forge-harness-ust7, review H2).
    ("POST", "/conversations/{conversation_id}/attachments"): (
        "APIRoute", "upload_attachment", "AttachmentResponse", None, True,
        ("_attachment_upload", "get_message_store", "require_session")),
    ("GET", "/conversations/{conversation_id}/attachments"): (
        "APIRoute", "conversation_attachments", "AttachmentsResponse", None, True,
        ("get_message_store", "require_session")),
    ("POST", "/auth/signup"): ("APIRoute", "signup", "AuthUser", None, True, ("get_auth_store",)),
    ("POST", "/auth/login"): ("APIRoute", "login", "AuthUser", None, True, ("get_auth_store",)),
    ("POST", "/auth/logout"): ("APIRoute", "logout", "dict", None, True, ()),
    ("GET", "/auth/me"): ("APIRoute", "me", "AuthUser", None, True, ("require_session", "get_auth_store")),
    ("GET", "/conversations"): ("WorkbenchRoute", "list_conversations", "ConversationPage", None, True, None),
    ("POST", "/conversations"): ("WorkbenchRoute", "create_conversation", "Conversation", 201, True, None),
    ("GET", "/conversations/{conversation_id}"): (
        "WorkbenchRoute", "read_conversation", "Conversation", None, True, None),
    ("PATCH", "/conversations/{conversation_id}"): (
        "WorkbenchRoute", "patch_conversation", "Conversation", None, True, None),
    ("GET", "/conversations/{conversation_id}/timeline"): (
        "WorkbenchRoute", "conversation_timeline", "TimelinePage", None, True, None),
}

#: `_fingerprint(capture_chat_metrics)`, the same on `a9ebf63` and on the base
#: this pin was written against.
CAPTURE_CHAT_METRICS_FINGERPRINT = "423040e0ace406f8ed3522c684d61af01701e21fa3b7bc4f1adaa56e10d15709"

_LAYOUT = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
           tokenize.ENCODING, tokenize.ENDMARKER}


def _fingerprint(function: Callable[..., object]) -> str:
    """SHA-256 of the function's tokens, blind to comments and layout. Tokens,
    not `ast.dump`, whose output changed in Python 3.13."""
    source = textwrap.dedent(inspect.getsource(function))
    words = [t.string for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type not in _LAYOUT]
    return hashlib.sha256("\x1f".join(words).encode()).hexdigest()


def _name(thing: object) -> str:
    return getattr(thing, "__name__", repr(thing))


def test_every_declaration_that_existed_before_the_job_runner_is_unchanged() -> None:
    declared: dict[tuple[str, str], list[_Row]] = {}
    schedulers: list[tuple[str, set[str], bool]] = []
    for path, route, dependant in api_route_dependants(appmod.app):
        methods = set(route.methods or ()) - {"HEAD", "OPTIONS"}
        if isinstance(route, SchedulerRoute):
            schedulers.append((path, methods, route.include_in_schema))
        for method in methods:
            key = (method, path)
            row = _PRE_BEAD.get(key)
            if row is None:
                continue
            # Exactly its own method: a method added to a pinned declaration is a change.
            assert methods == {method}, f"{key} now also answers {sorted(methods - {method})}"
            deps = tuple(_name(d.call) for d in dependant.dependencies) if row[5] is not None else None
            declared.setdefault(key, []).append((
                type(route).__name__, _name(route.endpoint), _name(route.response_model),
                route.status_code, route.include_in_schema, deps))

    for key, row in _PRE_BEAD.items():
        assert declared.get(key) == [row], f"{key} changed or is declared more than once"
    assert schedulers == [(SCHEDULER_PATH, {"POST"}, False)], "the scheduler route class is the job route's alone"

    stack = [(m.cls, m.kwargs.get("dispatch")) for m in appmod.app.user_middleware]
    expected = [
        (BaseHTTPMiddleware, appmod.set_security_headers),
        (BodyLimitMiddleware, None),  # agent-forge-harness-ust7
        (JobHookMiddleware, None),
        (BaseHTTPMiddleware, appmod.capture_chat_metrics),
    ]
    assert len(stack) == len(expected), stack
    for (cls, dispatch), (want_cls, want_dispatch) in zip(stack, expected, strict=True):
        assert cls is want_cls and dispatch is want_dispatch, stack

    assert _fingerprint(appmod.capture_chat_metrics) == CAPTURE_CHAT_METRICS_FINGERPRINT


# ── While a job runs, every other driver returns at once (G2) ────────────────


#: What each competitor answers while it steps aside. Smoke, not evidence: `run_job`
#: always returns None and the hook never changes a status.
_SMOKE: dict[str, object] = {"hook": 200, "scheduler": (200, {"ran": 0, "failed": 0, "remaining": True}),
                             "after_response": None, "after_commit": None}


@pytest.mark.parametrize("competitor", sorted(_SMOKE))
def test_every_other_driver_returns_within_a_bound_while_a_job_is_blocked(
    competitor: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    caplog.set_level(logging.WARNING, logger="service.db")
    started, release = threading.Event(), threading.Event()
    ran: list[int] = []
    queue = _CountingQueue()
    first, second = _enqueue(queue, 2)

    def handler(job: Job, context: JobContext) -> None:
        if job.id != first:
            ran.append(job.id)
            return
        started.set()
        assert release.wait(10)

    spy, clock = _SpyLock(), _Clock()
    driver = _driver(queue, handler, clock=clock, lock=spy.as_lock())
    hook_client, scheduler_client = TestClient(_probe(driver)), TestClient(_scheduler_app(driver))

    def compete() -> object:
        if competitor == "hook":
            return hook_client.get("/things").status_code
        if competitor == "scheduler":
            answer = scheduler_client.post(SCHEDULER_PATH, headers={SCHEDULER_SECRET_HEADER: SECRET})
            return answer.status_code, answer.json()
        if competitor == "after_response":
            driver.run_job(second)
        else:
            with queue.db.transaction() as unit:
                driver.runner.run_after_commit(unit, second)
        return None

    outcome: list[object] = []

    def attempt() -> None:
        try:
            outcome.append(compete())
        except Exception as exc:  # recorded here, asserted in the main thread
            outcome.append(exc)

    worker = threading.Thread(target=driver.run_hook, daemon=True)
    helper = threading.Thread(target=attempt, daemon=True)
    worker.start()
    try:
        assert started.wait(10), "the first job never started"
        claims, asked = queue.claims, len(spy.calls)
        clock.now += job_driver.HOOK_INTERVAL_S  # the hook's turn is due
        helper.start()
        helper.join(BOUND_S)
        assert not helper.is_alive(), f"the {competitor} driver waited for the job in flight"
        assert (queue.claims, ran) == (claims, []), "something queued behind, or ran beside, the job in flight"
        assert spy.calls[asked:] == [(False, -1, False)], "asked for the lock other than once, without waiting"
        assert outcome == [_SMOKE[competitor]], "smoke only: the spy above is the evidence"
        assert not [r for r in caplog.records if r.name == "service.db" and "after-commit" in r.getMessage()]
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert [row[0] for row in queue.snapshot()] == [second], "the first job ran; the second waits for later"
    assert spy.acquire(blocking=False), "the job lock is free again"
    spy.release()


# ── A claim that fails after a commit gives the lock back (G6) ───────────────


@pytest.mark.parametrize("failure", [PoolTimeout, psycopg.OperationalError])
def test_a_failed_claim_after_the_commit_gives_the_lock_back(
    failure: type[Exception], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    queue = _CountingQueue()
    (job_id,) = _enqueue(queue)
    lock = threading.Lock()
    runner = JobRunner(queue, {KIND: JobHandler(lambda job, context: None)}, clock=lambda: T0, single_flight=lock)
    queue.raises = failure("connection to 10.0.0.1 refused for secret-user")

    with queue.db.transaction() as unit:  # enqueues nothing, so it claims no twin writer
        runner.run_after_commit(unit, job_id)

    assert queue.claims == 1, "the after-commit attempt reached the claim"
    lines = [r.getMessage() for r in caplog.records if r.name == "service.db"]
    assert len(lines) == 1 and failure.__name__ in lines[0], lines
    assert "secret" not in caplog.text and "10.0.0.1" not in caplog.text
    assert queue.snapshot() == [(job_id, KIND, 0, None, False)], "the row stays the retry record"
    assert lock.acquire(blocking=False), "a failed claim leaked the job lock"
    lock.release()


# ── Only a schema this build understands runs jobs (G3) ─────────────────────


@pytest.mark.parametrize(
    ("migrations", "understood"),
    [("current", True), ("ahead", True), ("unavailable", False), ("failed", False), (None, False)],
    ids=["current", "ahead", "unavailable", "failed", "absent"],
)
def test_jobs_run_only_on_a_schema_this_build_understands(
    migrations: str | None, understood: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if migrations is None:
        monkeypatch.delitem(appmod._state, "migrations", raising=False)
    else:
        monkeypatch.setitem(appmod._state, "migrations", migrations)
    assert appmod._schema_understood() is understood

    queue = _CountingQueue()
    _enqueue(queue, 2)
    driver = _driver(queue, healthy=appmod._schema_understood)
    hook, batch = driver.run_hook(), driver.run_batch()
    if understood:
        assert isinstance(hook, JobRunResult) and isinstance(batch, JobRunResult)
        assert queue.claims >= 2 and queue.snapshot() == [], "the positive control: this setup does claim"
    else:
        assert (hook, batch, queue.claims) == (None, None, 0)


# ── The lock is injected, never imported (G4) ────────────────────────────────


def _driver_imports(tree: ast.AST) -> list[str]:
    """Every import of `job_driver`, and every dynamic import, in any form or scope."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if "job_driver" in a.name or a.name.split(".")[0] == "importlib"]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if ("job_driver" in module or module.split(".")[0] == "importlib"
                    or any("job_driver" in a.name for a in node.names)):
                found.append(f"from {'.' * node.level}{module} import {', '.join(a.name for a in node.names)}")
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if called in ("__import__", "import_module"):
                found.append(f"{called}(...)")
    return found


def test_the_queue_module_is_handed_the_job_lock_and_never_imports_it() -> None:
    for form in ("from .job_driver import JOB_LOCK", "from . import job_driver",
                 "import importlib\nimportlib.import_module('.job_driver', __package__).JOB_LOCK"):
        assert _driver_imports(ast.parse(form)), f"the guard itself misses: {form!r}"

    source = Path(cast(str, jobs.__file__)).read_text(encoding="utf-8")
    assert _driver_imports(ast.parse(source)) == []
    assert inspect.signature(JobRunner).parameters["single_flight"].default is None
    assert JobRunner(InMemoryJobQueue())._single_flight is None, "a runner built without a lock has none"


# ── A degraded instance has no driver until its database returns (G5) ───────

CURRENT = Report(applied=("0004_next.sql",), current=4, ahead=())
OUTAGE = psycopg.OperationalError('connection to server at "10.1.2.3" failed: the database system is starting up')


@pytest.fixture
def degraded(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """The state `lifespan` leaves behind when the database was away at startup
    (as in `test_startup_recovery.py`)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("MIGRATIONS_MODE", raising=False)
    now = [1000.0]
    monkeypatch.setattr(appmod, "_clock", lambda: now[0])
    monkeypatch.setattr(appmod, "RagService", lambda **kwargs: object())
    saved = dict(appmod._state)
    appmod._state.clear()
    appmod._state.update({"db": Database(), "migrations": "unavailable"})
    yield now
    appmod._state.clear()
    appmod._state.update(saved)


def _migrate(monkeypatch: pytest.MonkeyPatch, *outcomes: Report | BaseException) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def migrate(dsn: str | None = None, **kwargs: object) -> Report:
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(appmod, "migrate", migrate)
    return calls


def test_a_degraded_instance_has_no_job_driver_until_its_database_returns(
    degraded: list[float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    calls = _migrate(monkeypatch, OUTAGE, CURRENT)

    assert appmod._job_driver() is None
    client = TestClient(appmod.app)  # never `with`: the lifespan would re-prepare and then clear `_state`
    answer = client.post(SCHEDULER_PATH, headers={SCHEDULER_SECRET_HEADER: SECRET})
    assert (answer.status_code, answer.json()) == (503, {"detail": "jobs unavailable"})
    assert len(calls) == 1, "the scheduler's call is rationed like any other look"

    degraded[0] += appmod.RECOVERY_INTERVAL_S
    driver = appmod._job_driver()  # nothing below may claim: it would dial the unreachable DSN
    assert len(calls) == 2
    assert isinstance(driver, JobDriver)
    assert driver.lock is JOB_LOCK and driver.runner._single_flight is JOB_LOCK
    assert driver.healthy() and appmod._state["migrations"] == "current"
    hooked = appmod.app.user_middleware[2].kwargs["driver"]  # [1] is the body limit (ust7)
    assert callable(hooked) and hooked() is appmod._job_driver(), "the hook sees the recovered driver too"

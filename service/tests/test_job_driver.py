"""The drivers of the job outbox (1kg.2.7, RT-15): the request hook, the
authenticated `POST /internal/jobs`, and a job started after the response.

Everything runs against the in-memory twin with injected clocks. The ASGI calls
that must observe *when* a response is complete are driven by hand, because
`TestClient` returns only once the whole application call has finished.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout
from starlette.types import Message

import config
import service.app as appmod
from service import job_driver
from service.app import app, get_auth_store
from service.auth_store import InMemoryAuthStore
from service.job_driver import (
    JOB_LOCK,
    SCHEDULER_PATH,
    SCHEDULER_SECRET_ENV,
    SCHEDULER_SECRET_HEADER,
    SCHEDULER_SECRET_MIN_LENGTH,
    JobDriver,
    JobHookMiddleware,
    build_router,
    run_after_response,
)
from service.jobs import InMemoryJobQueue, Job, JobContext, JobHandler, JobRunner
from service.spa_fallback import install_spa
from service.workbench_api import api_routes, install_workbench

#: `require_session` must really run: what it leaves on a request is what the
#: hook keys off, and the suite's default session override would bypass it.
pytestmark = pytest.mark.real_auth

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
SECRET = "scheduler-secret-long-enough-to-be-accepted-0123456789"
KIND = "asset.delete"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _CountingQueue(InMemoryJobQueue):
    """The twin, counting the claims — "without touching the queue" is a count
    of zero."""

    claims: int = 0
    raises: BaseException | None = None

    def claim(self, *args: Any, **kwargs: Any) -> list[Job]:
        self.claims += 1
        if self.raises is not None:
            raise self.raises
        return super().claim(*args, **kwargs)


def _enqueue(queue: InMemoryJobQueue, n: int = 1, kind: str = KIND) -> list[int]:
    ids = []
    for i in range(n):
        with queue.db.transaction() as unit:
            ids.append(queue.enqueue(unit, kind, {"asset_id": f"secret-asset-{i}"}, now=T0))
    return ids


def _driver(
    queue: InMemoryJobQueue,
    handler: Callable[[Job, JobContext], None] | None = None,
    *,
    clock: _Clock | None = None,
    lock: threading.Lock | None = None,
    **kwargs: Any,
) -> JobDriver:
    clock = clock or _Clock()
    lock = lock or threading.Lock()
    handlers = {KIND: JobHandler(handler or (lambda job, context: None))}
    runner = JobRunner(queue, handlers, clock=lambda: T0, monotonic=clock, single_flight=lock)
    return JobDriver(runner, lock=lock, clock=clock, **kwargs)


def _probe(driver: Callable[[], JobDriver | None]) -> FastAPI:
    """An app with the hook and one route per kind of path it must tell apart.
    `auth_user` is what `require_session` leaves on a signed-in request."""
    probe = FastAPI()
    probe.add_middleware(JobHookMiddleware, driver=driver)

    def signed_in(request: Request) -> dict[str, bool]:
        request.state.auth_user = object()
        return {"ok": True}

    def anonymous() -> dict[str, bool]:
        return {"ok": True}

    for path in ("/things", "/healthz", "/internal/probe", "/table/assets/x", "/tables"):
        probe.add_api_route(path, signed_in, methods=["GET"])
    probe.add_api_route("/open", anonymous, methods=["GET"])
    return probe


# ── Driving an ASGI call by hand ─────────────────────────────────────────────


def _scope(method: str, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"", "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80), "headers": [(b"host", b"testserver"), *(headers or [])],
    }


async def _receive_nothing() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _complete(messages: list[dict[str, Any]]) -> bool:
    """Whether the server already holds the whole response: a start and a
    final body chunk."""
    starts = [m for m in messages if m["type"] == "http.response.start"]
    bodies = [m for m in messages if m["type"] == "http.response.body"]
    return bool(starts) and bool(bodies) and not bodies[-1].get("more_body", False)


def _held_until_released(target: FastAPI, scope: dict[str, Any], started: threading.Event,
                         release: threading.Event) -> tuple[bool, bool, list[dict[str, Any]]]:
    """Start the call, wait until the job has started, and report whether the
    response was already complete and the call still running at that moment."""

    async def scenario() -> tuple[bool, bool, list[dict[str, Any]]]:
        messages: list[dict[str, Any]] = []

        async def send(message: Message) -> None:
            messages.append(dict(message))

        call = asyncio.create_task(target(scope, _receive_nothing, send))
        assert await asyncio.to_thread(started.wait, 10), "the job never started"
        complete, running = _complete(messages), not call.done()
        release.set()
        await asyncio.wait_for(call, 10)
        return complete, running, messages

    return asyncio.run(scenario())


# ── The request hook ─────────────────────────────────────────────────────────


def test_the_hook_runs_one_due_job_after_a_signed_in_request_and_then_waits_its_turn():
    queue = _CountingQueue()
    ran: list[int] = []
    clock = _Clock()
    driver = _driver(queue, lambda job, context: ran.append(job.id), clock=clock)
    first, second, third = _enqueue(queue, 3)
    client = TestClient(_probe(lambda: driver))

    assert client.get("/things").json() == {"ok": True}
    assert ran == [first], "one job, not the whole backlog"
    assert client.get("/things").status_code == 200
    assert ran == [first] and queue.claims == 1, "within the interval nothing is even claimed"

    clock.now += job_driver.HOOK_INTERVAL_S
    client.get("/things")
    assert ran == [first, second]


@pytest.mark.parametrize("path", ["/healthz", "/internal/probe", "/table/assets/x", "/open"])
def test_the_hook_never_runs_for_probes_the_scheduler_route_table_routes_or_anonymous_callers(path):
    queue = _CountingQueue()
    driver = _driver(queue)
    _enqueue(queue)
    assert TestClient(_probe(lambda: driver)).get(path).status_code == 200
    assert queue.claims == 0


def test_the_table_exclusion_is_a_path_segment_not_a_prefix_of_letters():
    """`/tables` is not a table route: excluding it would be a false negative,
    and the positive control that the exclusion list is not simply "all"."""
    queue = _CountingQueue()
    driver = _driver(queue)
    _enqueue(queue)
    TestClient(_probe(lambda: driver)).get("/tables")
    assert queue.claims == 1


@pytest.mark.parametrize(
    "state",
    ["no driver", "degraded", "no handler registered"],
)
def test_the_hook_is_off_when_unconfigured_degraded_or_idle(state):
    queue = _CountingQueue()
    _enqueue(queue)
    driver: JobDriver | None = None
    if state == "degraded":
        driver = _driver(queue, healthy=lambda: False)
    elif state == "no handler registered":
        clock = _Clock()  # the runner's too, or a deadline mismatch would stop it instead
        driver = JobDriver(JobRunner(queue, {}, clock=lambda: T0, monotonic=clock), lock=threading.Lock(), clock=clock)
    assert TestClient(_probe(lambda: driver)).get("/things").json() == {"ok": True}
    assert queue.claims == 0 and len(queue.snapshot()) == 1


def _raising_handler(job: Job, context: JobContext) -> None:
    raise RuntimeError("gs://bucket/secret-object-name")


@pytest.mark.parametrize(
    "failure",
    ["handler raises", "outage", "busy gate", "unknown error", "driver lookup raises"],
)
def test_nothing_that_goes_wrong_in_job_work_changes_the_response(failure, caplog):
    caplog.set_level(logging.DEBUG)
    baseline = TestClient(_probe(lambda: None)).get("/things")

    queue = _CountingQueue()
    _enqueue(queue)
    driver = _driver(queue, _raising_handler)
    if failure == "outage":
        queue.raises = psycopg.OperationalError("could not connect to server at 10.0.0.1 as secret-user")
    elif failure == "busy gate":
        queue.raises = PoolTimeout("no database connection came free in 5 s")
    elif failure == "unknown error":
        queue.raises = ValueError("secret value in a bug")

    def lookup() -> JobDriver | None:
        if failure == "driver lookup raises":
            raise KeyError("secret")
        return driver

    answer = TestClient(_probe(lookup)).get("/things")
    assert (answer.status_code, answer.content, dict(answer.headers)) == (
        baseline.status_code, baseline.content, dict(baseline.headers))
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "secret" not in logged and "10.0.0.1" not in logged, logged


def test_an_outage_backs_the_hook_off_and_logs_only_class_and_sqlstate(caplog):
    caplog.set_level(logging.INFO)
    queue = _CountingQueue()
    _enqueue(queue)
    clock = _Clock()
    driver = _driver(queue, clock=clock)

    class _Refused(psycopg.OperationalError):
        sqlstate = "53300"

    queue.raises = _Refused("too many connections for role secret-user")
    client = TestClient(_probe(lambda: driver))
    client.get("/things")
    assert queue.claims == 1
    assert any("_Refused" in r.getMessage() and "53300" in r.getMessage() for r in caplog.records)
    assert "secret-user" not in caplog.text

    clock.now += job_driver.HOOK_INTERVAL_S
    client.get("/things")
    assert queue.claims == 1, "backed off beyond the ordinary interval"
    clock.now += job_driver.OUTAGE_BACKOFF_S
    queue.raises = None
    client.get("/things")
    assert queue.claims == 2 and queue.snapshot() == []


def test_a_busy_gate_is_skipped_without_backing_off(caplog):
    caplog.set_level(logging.INFO)
    queue = _CountingQueue()
    _enqueue(queue)
    clock = _Clock()
    driver = _driver(queue, clock=clock)
    queue.raises = PoolTimeout("the pool's own words, which name the gate")
    client = TestClient(_probe(lambda: driver))
    client.get("/things")
    queue.raises = None
    clock.now += job_driver.HOOK_INTERVAL_S
    client.get("/things")
    assert queue.claims == 2 and queue.snapshot() == [], "the next turn was the ordinary one"
    assert "own words" not in caplog.text


def test_the_client_holds_the_complete_response_before_the_hooks_job_is_released():
    """Through the REAL application's middleware stack — the security headers,
    the hook and chat metrics as they are assembled in `service/app.py` — on a
    signed-in request."""
    started, release = threading.Event(), threading.Event()
    threads: list[int] = []

    def handler(job: Job, context: JobContext) -> None:
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(10)

    queue = _CountingQueue()
    _enqueue(queue)
    driver = _driver(queue, handler)
    cookie = _signed_in_cookie()
    with _installed(driver):
        complete, running, messages = _held_until_released(
            app, _scope("GET", "/auth/me", [(b"cookie", cookie.encode())]), started, release)
    assert complete, "the job started before the client had the whole response"
    assert running, "the call was still busy with the job, so the job really came after"
    assert messages[0]["status"] == 200
    assert threads and threads[0] != threading.get_ident(), "job work ran on the event loop's thread"
    assert queue.snapshot() == []


# ── One job attempt at a time ────────────────────────────────────────────────


def test_while_a_job_runs_every_other_driver_returns_at_once_without_touching_the_queue(monkeypatch):
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    started, release = threading.Event(), threading.Event()

    def blocking(job: Job, context: JobContext) -> None:
        started.set()
        assert release.wait(10)

    queue = _CountingQueue()
    first, second = _enqueue(queue, 2)
    clock = _Clock()
    lock = threading.Lock()
    driver = _driver(queue, blocking, clock=clock, lock=lock)
    worker = threading.Thread(target=driver.run_hook)
    worker.start()
    try:
        assert started.wait(10)
        claims = queue.claims

        clock.now += job_driver.HOOK_INTERVAL_S
        TestClient(_probe(lambda: driver)).get("/things")  # a second hook attempt

        scheduler = _scheduler_app(lambda: driver)
        answer = TestClient(scheduler).post("/internal/jobs", headers={SCHEDULER_SECRET_HEADER: SECRET})
        assert (answer.status_code, answer.json()) == (200, {"ran": 0, "failed": 0, "remaining": True})

        driver.run_job(second)  # a post-response attempt
        with queue.db.transaction() as unit:  # and one after a commit
            driver.runner.run_after_commit(unit, second)

        assert queue.claims == claims, "something queued behind, or ran beside, the job in flight"
    finally:
        release.set()
        worker.join(10)
    assert [row[0] for row in queue.snapshot()] == [second], "the others left the job for later"


# ── POST /internal/jobs ──────────────────────────────────────────────────────


def _scheduler_app(driver: Callable[[], JobDriver | None], dist: Path | None = None) -> FastAPI:
    """Assembled the way `service/app.py` assembles the real one: the Workbench
    handlers, a legacy route, the scheduler router, the SPA fallback last."""
    target = FastAPI()
    install_workbench(target)

    @target.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    target.include_router(build_router(driver))
    if dist is not None:
        install_spa(target, dist)
    return target


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return tmp_path


def _scheduler_call(driver: JobDriver | None) -> Any:
    """One credentialed call, as Cloud Scheduler makes it."""
    return TestClient(_scheduler_app(lambda: driver)).post(SCHEDULER_PATH, headers={SCHEDULER_SECRET_HEADER: SECRET})


def _answer(
    client: TestClient, method: str, path: str, headers: dict[str, str] | list[tuple[str, str]],
) -> tuple[int, dict[str, str], bytes]:
    r = client.request(method, path, headers=headers, follow_redirects=False)
    return r.status_code, {k: v for k, v in r.headers.items() if k.lower() != "date"}, r.content


_CREDENTIALS = {
    "no header": (SECRET, {}),
    "wrong secret": (SECRET, {SCHEDULER_SECRET_HEADER: "x" * len(SECRET)}),
    "right secret, route unconfigured": (None, {SCHEDULER_SECRET_HEADER: SECRET}),
    "configured secret too short": ("short", {SCHEDULER_SECRET_HEADER: "short"}),
    # agent-forge-harness-ttda M1: one character short of SCHEDULER_SECRET_MIN_LENGTH,
    # presented correctly (not merely "wrong") — mutant A1 (`< SCHEDULER_SECRET_MIN_LENGTH`
    # -> `< 6`) treats this as configured and lets it through; the 5-character case above
    # is too short to tell the two bounds apart.
    "configured secret 31 chars, one short of the minimum": (
        "x" * (SCHEDULER_SECRET_MIN_LENGTH - 1), {SCHEDULER_SECRET_HEADER: "x" * (SCHEDULER_SECRET_MIN_LENGTH - 1)},
    ),
}


@pytest.mark.parametrize("topology", ["no static mount", "root static mount"])
@pytest.mark.parametrize("method", ["POST", "GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "FROBNICATE"])
@pytest.mark.parametrize("credential", sorted(_CREDENTIALS))
def test_without_the_credential_the_route_is_exactly_a_path_that_does_not_exist(
    monkeypatch, dist, topology, method, credential,
):
    configured, headers = _CREDENTIALS[credential]
    if configured is None:
        monkeypatch.delenv(SCHEDULER_SECRET_ENV, raising=False)
    else:
        monkeypatch.setenv(SCHEDULER_SECRET_ENV, configured)
    queue = _CountingQueue()
    _enqueue(queue)
    client = TestClient(_scheduler_app(lambda: _driver(queue), dist if topology == "root static mount" else None))

    for suffix in ("", "/"):
        route = _answer(client, method, f"/internal/jobs{suffix}", headers)
        assert route == _answer(client, method, f"/internal/nope{suffix}", headers)
        assert route == _answer(client, method, f"/nope{suffix}", headers)
    assert queue.claims == 0


#: pr158-i L1 / mutant U2: the header presented twice, either copy correct.
#: Both must still leave the route unmatched — `_credentialed` requires
#: exactly one presented value, so a second value is never authoritative,
#: whether it repeats the secret or gets it wrong (never a bypass either way).
_DUPLICATED_SECRET = {
    "both copies correct": [(SCHEDULER_SECRET_HEADER, SECRET), (SCHEDULER_SECRET_HEADER, SECRET)],
    "second copy wrong": [(SCHEDULER_SECRET_HEADER, SECRET), (SCHEDULER_SECRET_HEADER, "x" * len(SECRET))],
}


@pytest.mark.parametrize("topology", ["no static mount", "root static mount"])
@pytest.mark.parametrize("pair", sorted(_DUPLICATED_SECRET))
def test_a_duplicated_secret_header_is_refused_even_when_both_copies_match(monkeypatch, dist, topology, pair):
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    queue = _CountingQueue()
    _enqueue(queue)
    client = TestClient(_scheduler_app(lambda: _driver(queue), dist if topology == "root static mount" else None))
    headers = _DUPLICATED_SECRET[pair]

    for suffix in ("", "/"):
        route = _answer(client, "POST", f"/internal/jobs{suffix}", headers)
        assert route == _answer(client, "POST", f"/internal/nope{suffix}", headers)
        assert route == _answer(client, "POST", f"/nope{suffix}", headers)
    assert queue.claims == 0


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "FROBNICATE"])
def test_with_the_credential_any_other_method_is_still_a_missing_path(monkeypatch, dist, method):
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    headers = {SCHEDULER_SECRET_HEADER: SECRET}
    for mount in (None, dist):
        client = TestClient(_scheduler_app(lambda: None, mount))
        assert _answer(client, method, "/internal/jobs", headers) == _answer(client, method, "/nope", headers)


def test_a_32_character_secret_is_exactly_long_enough_to_be_accepted(monkeypatch):
    """agent-forge-harness-ttda M1, the boundary's other side: paired with the 31-character
    case in `_CREDENTIALS`, this pins `SCHEDULER_SECRET_MIN_LENGTH` exactly — a mutant that
    moved the boundary either up or down fails one case or the other."""
    secret = "x" * SCHEDULER_SECRET_MIN_LENGTH
    assert len(secret) == 32
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, secret)
    queue = _CountingQueue()
    _enqueue(queue)
    client = TestClient(_scheduler_app(lambda: _driver(queue)))
    response = client.post("/internal/jobs", headers={SCHEDULER_SECRET_HEADER: secret})
    assert (response.status_code, response.json()) == (200, {"ran": 1, "failed": 0, "remaining": False})


@pytest.mark.parametrize("topology", ["no static mount", "root static mount"])
def test_the_real_app_hides_the_route_the_same_way(monkeypatch, dist, topology):
    """The service app itself, not an assembly of its parts. No `ui/dist` in a
    checkout (`test_no_spa_route_is_registered_on_the_real_app`), so the mounted
    topology is installed for the duration, as production's image has it."""
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    saved = list(app.router.routes)
    try:
        if topology == "root static mount":
            install_spa(app, dist)
        client = TestClient(app)
        for method in ("POST", "GET", "PUT", "DELETE"):
            for headers in ({}, {SCHEDULER_SECRET_HEADER: "x" * len(SECRET)}):
                assert _answer(client, method, "/internal/jobs", headers) == _answer(client, method, "/nope", headers)
    finally:
        app.router.routes[:] = saved


def test_todays_answers_are_pinned_and_there_is_no_catch_all(dist):
    """What the route must not change, in both topologies — before and after
    this bead. A `POST /{path:path}` fallback would turn the 405 into a 404."""
    saved = list(app.router.routes)
    try:
        client = TestClient(app)
        assert client.post("/healthz").status_code == 405
        assert client.post("/nope").status_code == 404
        install_spa(app, dist)
        assert client.post("/healthz").status_code == 405
        assert client.post("/nope").status_code == 405
    finally:
        app.router.routes[:] = saved
    assert not [path for path, _ in api_routes(app) if ":path}" in path]


def test_the_route_is_not_in_the_published_schema():
    assert not [path for path in app.openapi()["paths"] if path.startswith("/internal")]
    assert ("/internal/jobs", "SchedulerRoute") in [(p, type(r).__name__) for p, r in api_routes(app)]


def test_a_credentialed_call_runs_due_jobs_up_to_the_count_and_says_only_how_many(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    queue = _CountingQueue()
    _enqueue(queue, 3)
    driver = _driver(queue, _raising_handler, batch_limit=2)
    client = TestClient(_scheduler_app(lambda: driver))
    headers = {SCHEDULER_SECRET_HEADER: SECRET}

    first = client.post("/internal/jobs", headers=headers)
    assert (first.status_code, first.json()) == (200, {"ran": 2, "failed": 2, "remaining": True})
    # The two failures back off; the third job is still due, and then nothing is.
    assert client.post("/internal/jobs", headers=headers).json() == {"ran": 1, "failed": 1, "remaining": False}
    assert "secret" not in first.text
    assert "secret" not in caplog.text and "gs://" not in caplog.text


def test_an_empty_queue_says_nothing_remains(monkeypatch):
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    queue = _CountingQueue()
    ran: list[int] = []
    driver = _driver(queue, lambda job, context: ran.append(job.id))
    _enqueue(queue, 2)
    answer = _scheduler_call(driver)
    assert answer.json() == {"ran": 2, "failed": 0, "remaining": False} and len(ran) == 2


def test_the_budget_stops_the_batch_starting_jobs_and_never_interrupts_one(monkeypatch):
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    clock = _Clock()
    finished: list[int] = []

    def slow(job: Job, context: JobContext) -> None:
        clock.now += job_driver.BATCH_BUDGET_S + 1  # overruns the whole budget
        finished.append(job.id)

    queue = _CountingQueue()
    _enqueue(queue, 3)
    driver = _driver(queue, slow, clock=clock)
    answer = _scheduler_call(driver)
    assert answer.json() == {"ran": 1, "failed": 0, "remaining": True}
    assert len(finished) == 1, "the running job finished; no other was started"


@pytest.mark.parametrize("state", ["no driver", "degraded", "outage"])
def test_a_scheduler_call_the_instance_cannot_serve_is_a_content_free_503(monkeypatch, state, caplog):
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    queue = _CountingQueue()
    _enqueue(queue)
    driver: JobDriver | None = None
    if state == "degraded":
        driver = _driver(queue, healthy=lambda: False)
    elif state == "outage":
        driver = _driver(queue)
        queue.raises = psycopg.OperationalError("connection to 10.0.0.1 refused for secret-user")
    answer = _scheduler_call(driver)
    assert (answer.status_code, answer.json()) == (503, {"detail": "jobs unavailable"})
    assert "secret-user" not in caplog.text


def test_the_secret_is_compared_in_constant_time(monkeypatch):
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    compared: list[tuple[bytes, bytes]] = []
    real = job_driver.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(job_driver.hmac, "compare_digest", spy)
    client = TestClient(_scheduler_app(lambda: None))
    client.post("/internal/jobs", headers={SCHEDULER_SECRET_HEADER: "wrong"})
    assert compared == [(b"wrong", SECRET.encode())]


def test_the_job_callable_of_the_route_runs_off_the_event_loop(monkeypatch):
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    threads: list[int] = []
    queue = _CountingQueue()
    _enqueue(queue)
    driver = _driver(queue, lambda job, context: threads.append(threading.get_ident()))
    target = _scheduler_app(lambda: driver)

    async def call() -> None:
        async def send(message: Message) -> None:
            return None

        scope = _scope("POST", "/internal/jobs", [(SCHEDULER_SECRET_HEADER.encode(), SECRET.encode())])
        await target(scope, _receive_nothing, send)

    asyncio.run(call())  # the loop runs on this thread
    assert threads and threads[0] != threading.get_ident()


# ── A job started after the response ─────────────────────────────────────────


def _after_response_app(driver: JobDriver, job_id: int) -> FastAPI:
    target = FastAPI()

    @target.post("/revoke")
    def revoke(tasks: BackgroundTasks) -> dict[str, bool]:
        run_after_response(tasks, driver, job_id)
        return {"revoked": True}

    return target


def test_a_job_started_after_the_response_never_holds_the_answer():
    started, release = threading.Event(), threading.Event()
    threads: list[int] = []

    def reconcile(job: Job, context: JobContext) -> None:
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(10)

    queue = _CountingQueue()
    (job_id,) = _enqueue(queue)
    driver = _driver(queue, reconcile)
    complete, running, messages = _held_until_released(
        _after_response_app(driver, job_id), _scope("POST", "/revoke"), started, release)
    assert complete and running
    assert threads[0] != threading.get_ident()
    assert queue.snapshot() == []


def test_a_job_started_after_the_response_leaves_its_row_while_other_job_work_runs():
    queue = _CountingQueue()
    (job_id,) = _enqueue(queue)
    lock = threading.Lock()
    driver = _driver(queue, lock=lock)
    assert lock.acquire(blocking=False)
    try:
        answer = TestClient(_after_response_app(driver, job_id)).post("/revoke")
    finally:
        lock.release()
    assert answer.json() == {"revoked": True}
    assert queue.claims == 0 and [row[0] for row in queue.snapshot()] == [job_id]


def test_nothing_is_scheduled_after_the_response_without_a_driver():
    tasks = BackgroundTasks()
    run_after_response(tasks, None, 1)
    assert tasks.tasks == []


# ── Wiring in service/app.py ─────────────────────────────────────────────────


def test_the_hook_is_outside_chat_metrics_and_inside_the_security_headers():
    """Job time can never enter `service.chat.duration_ms`, and
    `set_security_headers` stays the outermost user middleware, as its own
    docstring says."""
    stack = [(m.cls.__name__, getattr(m.kwargs.get("dispatch"), "__name__", None)) for m in app.user_middleware]
    assert stack == [
        ("BaseHTTPMiddleware", "set_security_headers"),
        ("JobHookMiddleware", None),
        ("BaseHTTPMiddleware", "capture_chat_metrics"),
    ]


def test_the_stores_bring_a_driver_that_shares_the_one_job_lock():
    saved = dict(appmod._state)
    try:
        appmod._build_stores(appmod.Database("postgresql://nobody@127.0.0.1:1/none"))
        driver = appmod._state["jobs"]
        assert isinstance(driver, JobDriver)
        assert driver.lock is JOB_LOCK and driver.runner._single_flight is JOB_LOCK
        handlers = driver.runner._handlers
        assert set(handlers) == {"campaign.reconcile", "table_session.expire", "timeline.session_divider"}, (
            "the kinds this build registers, by value"
        )
        assert handlers["table_session.expire"].max_attempts is None, "an expiry is retried until it runs"
        assert handlers["timeline.session_divider"].max_attempts is None, "a divider is retried until it is written"
        assert all(handler.max_attempts is None for handler in handlers.values()), (
            "each kind is retried until it succeeds"
        )
        appmod._state["migrations"] = "current"
        assert driver.healthy()
        appmod._state["migrations"] = "failed"
        assert not driver.healthy()
    finally:
        appmod._state.clear()
        appmod._state.update(saved)


# ── Helpers that reach into the real application ─────────────────────────────


def _signed_in_cookie() -> str:
    """A real session cookie, minted by a real signup against an in-memory
    auth store: `require_session` itself must run, because what it leaves on
    the request is what makes the request eligible."""
    store = InMemoryAuthStore()
    invite = store.create_invite(role="dm", expires_at=datetime.now(UTC) + timedelta(days=1)).token
    app.dependency_overrides[get_auth_store] = lambda: store
    client = TestClient(app)
    r = client.post("/auth/signup", json={"email": "gm@example.com", "password": "password123", "invite": invite})
    assert r.status_code == 200, r.text
    return f"{config.SESSION_COOKIE_NAME}={client.cookies[config.SESSION_COOKIE_NAME]}"


@contextmanager
def _installed(driver: JobDriver) -> Iterator[None]:
    """`_state["jobs"]` for the duration; the auth store override goes with it."""
    appmod._state["jobs"] = driver
    try:
        yield
    finally:
        appmod._state.pop("jobs", None)
        app.dependency_overrides.pop(get_auth_store, None)


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SESSION_SECRET", "job-driver-test-secret-at-least-32-characters")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)

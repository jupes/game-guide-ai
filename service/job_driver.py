"""
What makes the job outbox run (1kg.2.7, RT-15).

Cloud Run gives an instance CPU only while a request is in flight, so nothing
here is a background thread. `service/jobs.py`'s `JobRunner` is driven from the
three places RT-15 names, and from nowhere else:

1. **After the commit** — `JobRunner.run_after_commit`, inside the request that
   enqueued the job, or **after the response** — `run_after_response`, a
   Starlette background task, for work whose answer must not wait for it (a
   revocation's acknowledgement never waits on its reconciliation, RQ-5).
2. **The request hook** — `JobHookMiddleware`. After a signed-in request has
   been answered, at most one due job, at most once every `HOOK_INTERVAL_S` per
   instance.
3. **`POST /internal/jobs`** — what Cloud Scheduler calls, so that a quiet
   service still runs its jobs. Up to `BATCH_LIMIT` jobs within a cooperative
   `BATCH_BUDGET_S`.

The first two are opportunistic: Cloud Run may take the CPU away once the final
body is sent. Reliability comes from the durable row and the scheduler.

**One attempt at a time per instance.** `JOB_LOCK` is taken without waiting by
every driver, before any connection is asked for; if job work is already in
flight, the attempt is simply not made. Job work therefore holds at most one of
the gate's connections, ever — table and chat traffic keep the rest (SEC-35) —
and there is never anything queued behind it to accumulate.

**Never on the event loop, never in the response.** Every driver runs the
synchronous runner in the thread pool, after the response is complete. A job
failure, an outage, a busy gate or a verdict is caught here: it never changes
or delays an answer and never raises into a request. What is logged is the
job's kind and id, an exception's class and a SQLSTATE — never a payload, a
message or a driver's text (SEC-20, SEC-21).

**The bound is soft.** A handler gets a `JobContext` deadline and must bound its
own I/O; the queue's own transactions carry server-side timeouts. Neither is a
client wall clock: see `docs/migrations.md` section 4 and `1kg.2.8`.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from collections.abc import Callable

import psycopg
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.routing import APIRoute
from psycopg_pool import PoolTimeout
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .jobs import JobRunner, JobRunResult

log = logging.getLogger(__name__)

#: The instance's one job lock. Module-level, like `service/app.py`'s
#: `_recovery_lock`, because the guarantee is per process. `JobRunner` is handed
#: it rather than importing it (`jobs.py` never imports this module).
JOB_LOCK = threading.Lock()

#: The hook attempts a job at most this often per instance (*suggested*).
HOOK_INTERVAL_S = 5.0
#: After an outage the hook waits this long before it asks the database again.
OUTAGE_BACKOFF_S = 30.0
#: The advisory deadline a handler is given for one job.
JOB_BUDGET_S = 30.0
#: `/internal/jobs`: at most this many jobs per call, and no new job started once
#: this much time has gone — a tenth of Cloud Run's 300 s request timeout.
BATCH_LIMIT = 20
BATCH_BUDGET_S = 30.0

#: Where the scheduler's credential comes from and how it is presented. A
#: shared secret is the INTERIM credential: the deployment does not yet have the
#: OIDC audience and service account a Cloud Scheduler token would be verified
#: against (`1kg.9.5`, `docs/deploy-gcp.md`).
SCHEDULER_SECRET_ENV = "JOB_SCHEDULER_SECRET"
SCHEDULER_SECRET_HEADER = "x-job-scheduler-secret"
#: Shorter than this and the secret is refused — the route stays missing.
SCHEDULER_SECRET_MIN_LENGTH = 32
SCHEDULER_PATH = "/internal/jobs"

#: Paths the hook never runs for: the platform's probe, the scheduler's own
#: route, and table (player) traffic, which may never pay for job work (SEC-35).
#: Whole path segments: `/tables` is not `/table`.
HOOK_EXCLUDED_PREFIXES = ("/healthz", "/internal", "/table")

#: What an attempt that found job work already in flight — or no free
#: connection — reports: nothing ran, and something may well be due.
BUSY = JobRunResult(ran=0, failed=0, remaining=True)

#: An unreachable or overloaded database, as the rest of the service defines it.
_OUTAGE: tuple[type[BaseException], ...] = (psycopg.OperationalError, OSError)


class JobDriver:
    """The runner, the lock, and the hook's cadence, for one instance."""

    def __init__(
        self,
        runner: JobRunner,
        *,
        lock: threading.Lock = JOB_LOCK,
        healthy: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.monotonic,
        hook_interval_s: float = HOOK_INTERVAL_S,
        outage_backoff_s: float = OUTAGE_BACKOFF_S,
        job_budget_s: float = JOB_BUDGET_S,
        batch_limit: int = BATCH_LIMIT,
        batch_budget_s: float = BATCH_BUDGET_S,
    ) -> None:
        self.runner = runner
        self.lock = lock
        #: False on a degraded instance: one whose schema is not known to be
        #: one this build understands. It runs nothing.
        self.healthy = healthy
        #: Must be the runner's own monotonic clock: deadlines are compared there.
        self._clock = clock
        self._hook_interval_s = hook_interval_s
        self._outage_backoff_s = outage_backoff_s
        self._job_budget_s = job_budget_s
        self._batch_limit = batch_limit
        self._batch_budget_s = batch_budget_s
        self._hook_not_before = 0.0

    def enabled(self) -> bool:
        """Whether the hook has anything to do at all. With no kind registered
        it never asks the database — not one query per chat request."""
        return self.healthy() and self.runner.has_handlers()

    def hook_slot(self) -> bool:
        """Take the hook's turn, if it is due. Called on the event loop, so two
        requests can never both take the same turn."""
        now = self._clock()
        if now < self._hook_not_before:
            return False
        self._hook_not_before = now + self._hook_interval_s
        return True

    def run_hook(self) -> JobRunResult | None:
        """At most one due job. Runs in a worker thread; never raises."""
        return self._attempt(lambda deadline: self.runner.run_due(1, deadline), self._job_budget_s)

    def run_batch(self) -> JobRunResult | None:
        """Due jobs up to the count and the cooperative budget: no job is started
        once the budget is spent, and none is interrupted. `None` when the
        instance cannot run jobs now — degraded, or its database is away."""
        return self._attempt(
            lambda deadline: self.runner.run_due(self._batch_limit, deadline), self._batch_budget_s
        )

    def run_job(self, job_id: int) -> None:
        """One named job — the attempt `run_after_response` schedules. Its row
        stays the retry record whatever happens here."""
        self._attempt(lambda deadline: self.runner.run_job(job_id, deadline), self._job_budget_s)

    def _attempt(self, work: Callable[[float], JobRunResult], budget_s: float) -> JobRunResult | None:
        if not self.healthy():
            return None
        if not self.lock.acquire(blocking=False):
            return BUSY
        try:
            return work(self._clock() + budget_s)
        except PoolTimeout:
            # Every connection is serving a request: job work never queues for
            # one longer than any request would, and then it steps aside.
            log.info("jobs: no database connection came free; attempt skipped")
            return BUSY
        except _OUTAGE as exc:
            self._hook_not_before = max(self._hook_not_before, self._clock() + self._outage_backoff_s)
            log.warning("jobs: database unavailable (%s); backing off", _describe(exc))
            return None
        except Exception as exc:
            log.error("jobs: the attempt failed (%s)", _describe(exc))
            return None
        finally:
            self.lock.release()


def _describe(exc: BaseException) -> str:
    """The class and, for a database error, the SQLSTATE. Never the message:
    the driver's text names hosts and users."""
    sqlstate = getattr(exc, "sqlstate", None)
    return f"{type(exc).__name__}, SQLSTATE {sqlstate}" if sqlstate else type(exc).__name__


# ── The request hook ─────────────────────────────────────────────────────────


def _excluded(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in HOOK_EXCLUDED_PREFIXES)


def _signed_in(scope: Scope) -> bool:
    """`require_session` leaves the account it re-read on the request state. A
    request that never passed it — a probe, a login, a table credential — has
    none, and so never pays for job work."""
    state = scope.get("state")
    return isinstance(state, dict) and state.get("auth_user") is not None


class JobHookMiddleware:
    """RT-15's hook, as a pure ASGI middleware of its own.

    It forwards every message untouched and only watches for the final body.
    Once the response is complete — and only for a signed-in request outside
    `HOOK_EXCLUDED_PREFIXES` — it takes the hook's turn and runs one due job in
    the thread pool. Declared outside `capture_chat_metrics`, so job time never
    enters `service.chat.duration_ms`.
    """

    def __init__(self, app: ASGIApp, driver: Callable[[], JobDriver | None]) -> None:
        self.app = app
        self._driver = driver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _excluded(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        answered = False

        async def watch(message: Message) -> None:
            nonlocal answered
            await send(message)
            final_body = message["type"] == "http.response.body" and not message.get("more_body", False)
            if final_body or message["type"] == "http.response.pathsend":
                answered = True

        await self.app(scope, receive, watch)
        if answered and _signed_in(scope):
            await self._after_response()

    async def _after_response(self) -> None:
        try:
            driver = self._driver()
            if driver is None or not driver.enabled() or not driver.hook_slot():
                return
            await run_in_threadpool(driver.run_hook)
        except Exception as exc:
            log.error("jobs: the request hook failed (%s)", _describe(exc))


# ── After the response ───────────────────────────────────────────────────────


def run_after_response(tasks: BackgroundTasks, driver: JobDriver | None, job_id: int) -> None:
    """Try `job_id` once the response has been sent, in the thread pool.

    Use this, not `JobRunner.run_after_commit`, whenever the answer must not
    wait for the job. Like every driver it is an opportunity, not a promise: it
    does nothing while other job work is in flight, and Cloud Run may withhold
    the CPU after the final body. The row is the retry record either way."""
    if driver is not None:
        tasks.add_task(driver.run_job, job_id)


# ── POST /internal/jobs ──────────────────────────────────────────────────────


def scheduler_secret() -> bytes | None:
    """The configured credential, or None when it is unset or too short to
    trust — in which case the route does not exist for anyone."""
    configured = os.environ.get(SCHEDULER_SECRET_ENV, "").strip()
    if len(configured) < SCHEDULER_SECRET_MIN_LENGTH:
        return None
    return configured.encode()


def _credentialed(scope: Scope) -> bool:
    expected = scheduler_secret()
    if expected is None:
        return False
    presented = [value for name, value in scope.get("headers", []) if name == SCHEDULER_SECRET_HEADER.encode()]
    return len(presented) == 1 and hmac.compare_digest(presented[0], expected)


class SchedulerRoute(APIRoute):
    """A route that only exists for its caller.

    It matches nothing but a POST that carries the scheduler's credential. Any
    other request — no credential, a wrong one, an unset or refused secret, any
    other method, known or not — does not match it at all, so the router goes on
    exactly as it would for a path that does not exist: the framework's 404, or
    the root static mount's answer where the built UI is mounted (SEC-3). Not a
    look-alike of that answer, but the same code path, in every topology.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        if match is Match.NONE or scope.get("method") != "POST" or not _credentialed(scope):
            return Match.NONE, {}
        return match, child_scope


class JobsRun(BaseModel):
    """Counts only (SEC-20). `remaining` is conservative: true whenever the
    call stopped before it saw an empty queue."""

    ran: int
    failed: int
    remaining: bool


def build_router(driver: Callable[[], JobDriver | None]) -> APIRouter:
    """`POST /internal/jobs`. Include it before the SPA fallback, as every API
    route is. Never proxied by a public front end: Cloud Scheduler calls the
    service directly (`tests/test_proxy_contract.py`)."""
    router = APIRouter(route_class=SchedulerRoute)

    @router.post(SCHEDULER_PATH, response_model=JobsRun, include_in_schema=False)
    def run_jobs() -> JobsRun:
        # A synchronous endpoint: the framework runs it in the thread pool.
        current = driver()
        result = current.run_batch() if current is not None else None
        if result is None:
            raise HTTPException(status_code=503, detail="jobs unavailable")
        return JobsRun(ran=result.ran, failed=result.failed, remaining=result.remaining)

    return router

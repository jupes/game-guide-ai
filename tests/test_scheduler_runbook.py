"""
The job scheduler's runbook against the code it describes
(agent-forge-harness-1kg.2.7.3).

`docs/deploy-gcp.md` §12 is what an operator follows to create the Cloud
Scheduler job that calls `POST /internal/jobs`, and to read what it did. It
quotes the route's path, header, secret and bounds, the order of the bootstrap,
what each answer means, and the lines the job path writes to the log. Each
quotation is checked here against its source — the answers against the real
application — so that a change on either side fails a build instead of misleading
someone during an incident.

Run from repo root:
    uv run python -m pytest tests/test_scheduler_runbook.py -q
"""

from __future__ import annotations

import ast
import json
import logging
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from uvicorn.config import LOGGING_CONFIG

import service.app as appmod
from service.job_driver import (
    BATCH_BUDGET_S,
    BATCH_LIMIT,
    OUTAGE_BACKOFF_S,
    SCHEDULER_PATH,
    SCHEDULER_SECRET_ENV,
    SCHEDULER_SECRET_HEADER,
    SCHEDULER_SECRET_MIN_LENGTH,
    JobDriver,
)
from service.jobs import LEASE_SECONDS, RETRY_CAP_SECONDS, InMemoryJobQueue, JobHandler, JobRunner, retry_delay
from service.spa_fallback import SPA_MOUNT_NAME, SPA_ROUTE_PREFIX, install_spa

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = REPO_ROOT / "docs" / "deploy-gcp.md"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
DOCKERFILE_CLOUD = REPO_ROOT / "Dockerfile.cloud"
JOB_MODULES = (REPO_ROOT / "service" / "job_driver.py", REPO_ROOT / "service" / "jobs.py")

SECRET = "scheduler-runbook-secret-long-enough-0123456789"
KIND = "runbook.probe"


def _section() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("## 12. The job scheduler")
    return text[start : text.index("\n## 13.", start)]


def _prose() -> str:
    """The section with its line breaks folded, for a phrase that wraps."""
    return " ".join(_section().split())


def _create_command() -> str:
    """Step 4's `gcloud scheduler jobs create http`, its continuation lines joined."""
    lines = _section().splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("gcloud scheduler jobs create http "))
    parts = []
    for line in lines[first:]:
        parts.append(line.rstrip().removesuffix("\\").strip())
        if not line.rstrip().endswith("\\"):
            break
    return " ".join(parts)


def _flag(command: str, name: str) -> str:
    found = re.findall(rf'--{re.escape(name)}=("[^"]*"|\S+)', command)
    assert len(found) == 1, f"--{name} appears {len(found)} times in: {command}"
    return str(found[0]).strip('"')


def _table_rows(first_cell: str) -> dict[str, str]:
    """The rows of the §12 table whose header row starts `| <first_cell> |`,
    keyed by their first cell with its backticks removed."""
    lines = _section().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"| {first_cell} |"))
    rows: dict[str, str] = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows[line.split("|")[1].strip().strip("`")] = line
    return rows


# ── How Cloud Scheduler calls the route ──────────────────────────────────────


def test_the_job_calls_the_route_the_way_the_route_answers() -> None:
    command = _create_command()
    assert _flag(command, "http-method") == "POST", "the route matches nothing but a POST"
    assert _flag(command, "uri") == "$SERVICE_URL" + SCHEDULER_PATH
    name, _, value = _flag(command, "headers").partition("=")
    assert name.lower() == SCHEDULER_SECRET_HEADER, "headers are matched case-insensitively, never by spelling"
    assert "gcloud secrets versions access latest --secret=job-scheduler-secret" in value
    assert _flag(command, "oidc-token-audience") == "$SERVICE_URL", "Cloud Run IAM checks the audience"
    assert _flag(command, "oidc-service-account-email").startswith("job-scheduler@")
    assert f"{SCHEDULER_SECRET_ENV}=job-scheduler-secret:latest" in _section(), "the name the service reads"


def test_the_attempt_deadline_outlasts_the_batch_and_fits_the_request_timeout() -> None:
    deadline = re.fullmatch(r"(\d+)s", _flag(_create_command(), "attempt-deadline"))
    timeout = re.search(r"--timeout (\d+)", DEPLOY_SH.read_text(encoding="utf-8"))
    assert deadline is not None and timeout is not None
    assert BATCH_BUDGET_S < int(deadline.group(1)) <= int(timeout.group(1))
    assert f"Cloud Run's {timeout.group(1)} s request timeout" in _prose()


def test_the_bootstrap_runs_in_the_order_its_steps_depend_on() -> None:
    """The API before anything that uses it; the secret and its reader before
    the deploy that passes it; the scheduler's account and its invoker grant
    before the job that sends its token."""
    section = _section()
    steps = [
        "gcloud services enable cloudscheduler.googleapis.com",
        "gcloud secrets create job-scheduler-secret",
        "gcloud secrets add-iam-policy-binding job-scheduler-secret",
        f"{SCHEDULER_SECRET_ENV}=job-scheduler-secret:latest",
        "gcloud iam service-accounts create job-scheduler",
        "--role=roles/run.invoker",
        "gcloud scheduler jobs create http",
    ]
    positions = [section.index(step) for step in steps]
    assert positions == sorted(positions), "a bootstrap step comes before what it needs"
    assert "INTERIM" in _table_rows("Credential")["the shared secret `JOB_SCHEDULER_SECRET` (step 1)"]
    assert "**The header is interim.**" in _prose()


def test_the_runbook_quotes_the_routes_own_bounds() -> None:
    section = _prose()
    assert f"up to {BATCH_LIMIT} due jobs per call, starting none after {BATCH_BUDGET_S:g} s" in section
    assert f"the route's {BATCH_BUDGET_S:g} s budget" in section
    assert f"under {SCHEDULER_SECRET_MIN_LENGTH} characters" in section
    assert f"Fewer than {SCHEDULER_SECRET_MIN_LENGTH} characters and the service refuses it" in section
    assert f"The hook waits {OUTAGE_BACKOFF_S:g} s before it asks again" in section
    assert f"A lease lasts {LEASE_SECONDS} s" in section
    steps = ", ".join(f"{retry_delay(n).total_seconds():g} s" for n in (1, 2, 3))
    assert RETRY_CAP_SECONDS == 3600 and f"retried after {steps} … capped at an hour" in section


# ── What each answer means ───────────────────────────────────────────────────


@pytest.fixture
def real_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The service app itself, with the secret configured; its state and its
    route table restored afterwards. No lifespan: nothing dials a database."""
    monkeypatch.setenv(SCHEDULER_SECRET_ENV, SECRET)
    state, routes = dict(appmod._state), list(appmod.app.router.routes)
    try:
        yield
    finally:
        appmod._state.clear()
        appmod._state.update(state)
        appmod.app.router.routes[:] = routes


def _install_driver(*, healthy: bool) -> None:
    queue = InMemoryJobQueue()
    with queue.db.transaction() as unit:
        queue.enqueue(unit, KIND, {"probe_id": "p-1"})
    lock = threading.Lock()
    runner = JobRunner(queue, {KIND: JobHandler(lambda job, context: None)}, single_flight=lock)
    appmod._state["jobs"] = JobDriver(runner, lock=lock, healthy=lambda: healthy)


def _post(headers: dict[str, str]) -> tuple[int, bytes]:
    answer = TestClient(appmod.app).post(SCHEDULER_PATH, headers=headers)
    return answer.status_code, answer.content


@pytest.mark.usefixtures("real_app")
def test_each_answer_the_runbook_explains_is_what_the_real_app_answers(tmp_path: Path) -> None:
    rows = _table_rows("Status")
    credentialed = {SCHEDULER_SECRET_HEADER: SECRET}

    _install_driver(healthy=True)
    status, body = _post(credentialed)
    ran = json.loads(body)
    assert status == 200 and ran == {"ran": 1, "failed": 0, "remaining": False}
    assert set(re.findall(r'"(\w+)":', rows["200"])) == set(ran), "the counts the 200 row quotes, and no more"

    _install_driver(healthy=False)
    status, body = _post(credentialed)
    assert status == 503 and json.dumps(json.loads(body)) in rows["503"]

    # Refused: a healthy instance with a due job, so a route that answered at
    # all would say 200 — it must say what a missing path says instead, in an
    # image without the built UI and in one that serves it from `/`.
    _install_driver(healthy=True)
    appmod.app.router.routes[:] = [
        route
        for route in appmod.app.router.routes
        if not (getattr(route, "name", "") == SPA_MOUNT_NAME or getattr(route, "name", "").startswith(SPA_ROUTE_PREFIX))
    ]
    refused: list[tuple[int, bytes]] = []
    for serves_the_ui in (False, True):
        if serves_the_ui:
            (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
            install_spa(appmod.app, tmp_path)
        missing = TestClient(appmod.app).post("/nope")
        refused.append(_post({}))
        assert refused[-1] == (missing.status_code, missing.content)
    (without_ui, _), (with_ui, _) = refused
    assert f"a `{without_ui}` in an image without the UI" in rows[str(with_ui)]

    # The image the runbook is about serves the built UI from `/`.
    dockerfile = DOCKERFILE_CLOUD.read_text(encoding="utf-8")
    assert re.search(r"^COPY --from=\S+ /ui/dist ui/dist$", dockerfile, re.M)
    assert appmod._UI_DIST == REPO_ROOT / "ui" / "dist"


# ── What the job path writes to the log ──────────────────────────────────────


def _log_calls() -> list[tuple[str, str, list[str]]]:
    """(level, format, other string constants) of every `log.<level>("jobs: ...")`
    in the job modules."""
    calls = []
    for module in JOB_MODULES:
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "log"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith("jobs:")
            ):
                continue
            level = {"exception": "ERROR"}.get(node.func.attr, node.func.attr.upper())
            extras = [
                c.value
                for arg in node.args[1:]
                for c in ast.walk(arg)
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and len(c.value) >= 3
            ]
            calls.append((level, node.args[0].value, extras))
    return calls


def test_every_line_the_job_path_logs_is_in_the_runbook_at_its_level() -> None:
    calls = _log_calls()
    assert len(calls) >= 5, "found fewer log calls than the runbook lists — did the job modules move?"
    rows = list(_table_rows("Line").values())
    for level, fmt, extras in calls:
        pattern = ".*?".join(re.escape(fragment) for fragment in re.split(r"%[sd]", fmt) if fragment)
        found = [row for row in rows if re.search(pattern, row)]
        assert found, f"the runbook does not list the line {fmt!r}"
        assert all(f"| {level}" in row for row in found), f"{fmt!r} is logged at {level}"
        reaches_the_log = logging.getLevelName(level) >= logging.WARNING
        assert all(("never in the log" in row) != reaches_the_log for row in found), fmt
        for extra in extras:
            assert any(extra in row and f"| {level}" in row for row in rows), f"{extra!r} of {fmt!r}"


def test_only_warning_and_above_reach_the_images_log() -> None:
    """What the INFO row's "never in the log" rests on: the image starts uvicorn
    with its default logging, which configures only uvicorn's own loggers, and
    no module the image ships configures any — so the application's records meet
    no handler and fall to `logging.lastResort`, which passes WARNING and above."""
    command = next(line for line in DOCKERFILE_CLOUD.read_text(encoding="utf-8").splitlines() if line.startswith("CMD"))
    assert "uvicorn service.app:app" in command and "--log-config" not in command
    assert "root" not in LOGGING_CONFIG
    assert all(name.startswith("uvicorn") for name in LOGGING_CONFIG["loggers"])
    assert logging.lastResort is not None and logging.lastResort.level == logging.WARNING
    configures = {"basicConfig", "dictConfig", "fileConfig", "addHandler"}
    shipped = [
        REPO_ROOT / "config.py",
        *(REPO_ROOT / "service").rglob("*.py"),
        *(REPO_ROOT / "ingestion").rglob("*.py"),
    ]
    for module in sorted(path for path in shipped if "tests" not in path.relative_to(REPO_ROOT).parts):
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
        }
        assert not called & configures, f"{module.name} configures logging; the runbook's log table no longer holds"

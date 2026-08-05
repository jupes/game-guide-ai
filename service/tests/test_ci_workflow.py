"""Repository-level contract for PR E2E gating and deploy safety."""

import re
from pathlib import Path

WORKFLOW = Path(".github/workflows/ci.yml")

#: Tests that only mean something against a real database. Each must be reachable
#: from CI with DATABASE_URL set, or it silently reverts to a permanent skip.
DB_BACKED_TESTS = [
    "service/tests/test_invite_atomic.py",
    "tests/test_schema.py",
]


def _python_job() -> str:
    return WORKFLOW.read_text(encoding="utf-8").split("\n  python-tests:\n", 1)[1].split(
        "\n  ui-tests:\n", 1
    )[0]


def test_ci_runs_e2e_on_pull_requests_and_never_deploys_them():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "\n  pull_request:\n" in workflow
    assert "\n  ui-e2e:\n" in workflow
    e2e_job = workflow.split("\n  ui-e2e:\n", 1)[1].split("\n  deploy:\n", 1)[0]
    assert "bun run test:e2e" in e2e_job
    assert "actions/upload-artifact@v4" in e2e_job
    assert "ui/e2e-results" in e2e_job

    deploy_job = workflow.split("\n  deploy:\n", 1)[1]
    assert "ui-e2e" in deploy_job.split("\n    if:", 1)[0]
    assert "github.event_name != 'pull_request'" in deploy_job


# ── The database-backed tests must actually RUN in CI ────────────────────────
#
# The failure these prevent is invisible: a skipped test reports the same green
# tick as a passing one, so the atomic invite guarantee could go unverified on
# every run with nothing to show for it.


def test_ci_provides_a_postgres_service_for_the_python_job():
    job = _python_job()
    assert re.search(r"^\s{4}services:$", job, re.M), (
        "python-tests must declare a `services:` block — without a database the "
        "integration tests skip, and a skip looks exactly like a pass"
    )
    assert re.search(r"image:\s*postgres:", job), "the service must be a postgres image"
    assert "--health-cmd" in job, (
        "the postgres service needs a health check, or the test step races the "
        "database's startup and fails for a reason that has nothing to do with the code"
    )


def test_ci_runs_the_database_backed_tests_with_a_dsn():
    job = _python_job()
    assert "DATABASE_URL:" in job, (
        "CI must set DATABASE_URL for the integration step; without it "
        f"{DB_BACKED_TESTS} skip themselves and verify nothing"
    )
    for test in DB_BACKED_TESTS:
        assert test in job, f"CI must invoke {test} (it is skip-only without a DSN)"


def test_the_dsn_is_scoped_to_the_integration_step_not_the_whole_job():
    """A job-wide DATABASE_URL would change the app's startup path in every
    unrelated test (the lifespan builds a real auth store when it can connect),
    so the variable belongs to the one step that wants it."""
    job = _python_job()
    dsn_index = job.index("DATABASE_URL:")
    # The `env:` that owns it must sit inside a step, i.e. after the job's
    # `steps:` key — not in a job-level `env:` block above it.
    assert "\n    steps:" in job and job.index("\n    steps:") < dsn_index, (
        "DATABASE_URL must be set on a step, not on the whole python-tests job"
    )


def test_the_integration_step_does_not_swallow_its_own_failure():
    job = _python_job()
    step = job.split("Integration tests against real PostgreSQL", 1)[1]
    assert "continue-on-error" not in step, (
        "the integration step must be able to fail the job — allowing it to "
        "continue would make the job green whatever the database did"
    )

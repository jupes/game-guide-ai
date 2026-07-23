"""Repository-level contract for PR E2E gating and deploy safety."""

from pathlib import Path


def test_ci_runs_e2e_on_pull_requests_and_never_deploys_them():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "\n  pull_request:\n" in workflow
    assert "\n  ui-e2e:\n" in workflow
    e2e_job = workflow.split("\n  ui-e2e:\n", 1)[1].split("\n  deploy:\n", 1)[0]
    assert "bun run test:e2e" in e2e_job
    assert "actions/upload-artifact@v4" in e2e_job
    assert "ui/e2e-results" in e2e_job

    deploy_job = workflow.split("\n  deploy:\n", 1)[1]
    assert "ui-e2e" in deploy_job.split("\n    if:", 1)[0]
    assert "github.event_name != 'pull_request'" in deploy_job

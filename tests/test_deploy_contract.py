"""
Deploy-contract guards (x5bz.1 — GCP pilot hosting).

These tests inspect the *deploy artifacts* the CI `deploy` job and operators run,
so their risky invariants cannot silently regress:

- `Dockerfile.cloud` — the single-container UI+API image (Checkpoint A).
- `scripts/deploy.sh` — the Cloud Run deploy entrypoint (Checkpoint B).

The licensing lock is the headline invariant: the pilot serves a *closed* group,
so the deploy must never request public ingress (`--allow-unauthenticated`). A
guard here is cheaper than discovering a public D&D-rules app after the fact.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_deploy_contract.py -q
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE_CLOUD = REPO_ROOT / "Dockerfile.cloud"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"


def _read(path: Path) -> str:
    assert path.exists(), f"{path.relative_to(REPO_ROOT)} does not exist"
    return path.read_text(encoding="utf-8")


# ── Checkpoint A: Dockerfile.cloud ────────────────────────────────────────────


def test_cloud_image_builds_ui_and_copies_dist_without_rerank() -> None:
    """Dockerfile.cloud builds the UI in a bun stage, copies ui/dist into the
    runtime image, and keeps the heavy rerank extra opt-in (test #4)."""
    text = _read(DOCKERFILE_CLOUD)

    # A dedicated bun build stage (reused pattern from ui/Dockerfile).
    assert re.search(r"(?im)^\s*FROM\s+oven/bun\S*\s+AS\s+\w+", text), (
        "Dockerfile.cloud must build the UI in a named `FROM oven/bun ... AS <stage>` stage"
    )
    assert "bun run build" in text, "the bun stage must run `bun run build`"

    # The built UI lands where the FastAPI app serves it (/app/ui/dist).
    copies_dist = [
        line
        for line in text.splitlines()
        if line.lstrip().upper().startswith("COPY --FROM=") and "ui/dist" in line
    ]
    assert copies_dist, "a `COPY --from=<stage> .../dist ui/dist` line must stage the built UI"

    # The reranker (torch, heavy) must stay opt-in — never a default cloud-image layer.
    if "[rerank]" in text:
        assert "INSTALL_RERANK" in text, (
            "the rerank extra must be gated behind INSTALL_RERANK (opt-in), not installed by default"
        )


# ── Checkpoint B: scripts/deploy.sh ───────────────────────────────────────────


def test_deploy_never_requests_public_ingress() -> None:
    """The licensing lock: deploy.sh locks ingress and can never open it (test #1)."""
    text = _read(DEPLOY_SH)

    assert text.startswith("#!"), "deploy.sh must be a runnable script (shebang)"
    assert "--no-allow-unauthenticated" in text, (
        "deploy.sh must deploy Cloud Run with --no-allow-unauthenticated (closed pilot)"
    )
    # The public-ingress flag must never appear. `--no-allow-unauthenticated` does
    # NOT contain the substring `--allow-unauthenticated`, so this is a clean check.
    assert "--allow-unauthenticated" not in text, (
        "deploy.sh must NEVER request public ingress (licensing lock — see x5bz.5)"
    )


def test_deploy_attaches_cloudsql_and_injects_secrets_by_reference() -> None:
    """Cloud SQL by socket; OPENAI_API_KEY + DATABASE_URL by Secret Manager
    reference, never inlined values (test #2)."""
    text = _read(DEPLOY_SH)

    assert "--add-cloudsql-instances" in text, "deploy.sh must attach Cloud SQL by socket"
    assert "--set-secrets" in text, "secrets must be injected by reference via --set-secrets"
    assert "OPENAI_API_KEY=" in text and "DATABASE_URL=" in text, (
        "both OPENAI_API_KEY and DATABASE_URL must be wired (as secret references)"
    )
    # No inlined secret material.
    assert "sk-" not in text, "deploy.sh must not inline an OpenAI key literal"
    assert not re.search(r"--set-env-vars[^\n]*OPENAI_API_KEY=", text), (
        "OPENAI_API_KEY must come from --set-secrets, not an inlined --set-env-vars value"
    )


def test_deploy_dry_run_prints_commands_without_executing() -> None:
    """`deploy.sh --dry-run` prints the full gcloud plan and runs nothing — a safe,
    inspectable preview that needs neither gcloud nor docker (test #3)."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable to exercise deploy.sh --dry-run (runs in CI)")

    result = subprocess.run(
        [bash, str(DEPLOY_SH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"--dry-run exited {result.returncode}: {result.stderr}"
    out = result.stdout
    assert "gcloud run deploy" in out, "dry-run must print the gcloud run deploy command"
    assert "--no-allow-unauthenticated" in out, "the printed plan must carry the ingress lock"

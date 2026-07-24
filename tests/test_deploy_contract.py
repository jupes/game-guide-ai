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
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE_CLOUD = REPO_ROOT / "Dockerfile.cloud"


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

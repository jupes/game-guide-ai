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

import os
import re
import subprocess
from pathlib import Path

import pytest
from _bash import bash_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE_CLOUD = REPO_ROOT / "Dockerfile.cloud"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
UV_LOCK = REPO_ROOT / "uv.lock"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _read(path: Path) -> str:
    assert path.exists(), f"{path.relative_to(REPO_ROOT)} does not exist"
    return path.read_text(encoding="utf-8")


# See tests/_bash.py for why `shutil.which("bash")` is not enough on Windows.
_bash_or_skip = bash_or_skip


# ── A script with a shebang must be executable ───────────────────────────────


def test_every_shebanged_script_is_recorded_executable():
    """Runbooks invoke these directly (`scripts/bootstrap-db.sh <dsn>`), so the
    bit has to survive a clone. Checked against the git index rather than the
    working tree: the index is what a clone receives, and on Windows
    `core.fileMode` is off, so the working-tree bit carries no information.
    """
    indexed = subprocess.run(
        ["git", "ls-files", "-s", "scripts/"],
        capture_output=True, text=True, timeout=30, cwd=REPO_ROOT, check=True,
    ).stdout.splitlines()
    wrong = []
    for line in indexed:
        meta, path = line.split("\t", 1)
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="ignore")
        if text.startswith("#!") and meta.split()[0] != "100755":
            wrong.append(path)
    assert not wrong, (
        f"{wrong} declare a shebang but are recorded non-executable. "
        f"Fix with `git update-index --chmod=+x <path>`."
    )


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


# ── Reproducible dependency resolution ───────────────────────────────────────


def test_the_lockfile_is_committed_and_reachable_by_the_build() -> None:
    """Without a committed lock, CI and the production image re-resolve every
    transitive dependency on each build — so the artifact that passed review is
    not necessarily the artifact that ships. For code that hashes passwords and
    signs sessions, "probably the same packages" is not good enough.

    Three ways this silently regresses: the file gets deleted, `.gitignore`
    starts ignoring it again (it did until this change), or `.dockerignore`
    keeps it out of the build context so the image falls back to a fresh
    resolve.
    """
    assert UV_LOCK.exists(), "uv.lock must be committed, not generated per build"

    ignored = subprocess.run(
        ["git", "check-ignore", "uv.lock"],
        capture_output=True, text=True, timeout=30, cwd=REPO_ROOT,
    )
    assert ignored.returncode != 0, "uv.lock must not be gitignored"

    if DOCKERIGNORE.exists():
        patterns = {
            line.strip() for line in _read(DOCKERIGNORE).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        assert not patterns & {"uv.lock", "*.lock", "uv.*"}, (
            ".dockerignore must not exclude uv.lock — the image build reads it"
        )


def test_the_cloud_image_installs_from_the_lock_not_a_fresh_resolve() -> None:
    text = _read(DOCKERFILE_CLOUD)

    assert "uv.lock" in text, "Dockerfile.cloud must COPY uv.lock into the build"
    assert "--frozen" in text, (
        "the export must be --frozen so a lock that has drifted from "
        "pyproject.toml fails the build instead of being silently re-resolved"
    )
    assert "--require-hashes" in text, (
        "install with --require-hashes: the lock pins versions, the hashes pin "
        "the actual artifacts"
    )
    # A bare `pip install .` (or '.[extra]') resolves dependencies afresh and
    # would quietly undo all of the above. Only the --no-deps form is allowed.
    for match in re.finditer(r"pip install[^\n\\]*", text):
        command = match.group(0)
        if re.search(r"\s'?\.(\[|\s|'|$)", command):
            assert "--no-deps" in command, (
                f"project install must be --no-deps (deps come from the lock): {command!r}"
            )


# ── Checkpoint B: scripts/deploy.sh ───────────────────────────────────────────


def test_deploy_never_requests_public_ingress() -> None:
    """The licensing lock: deploy.sh can never open ingress (test #1)."""
    text = _read(DEPLOY_SH)

    assert text.startswith("#!"), "deploy.sh must be a runnable script (shebang)"
    # The public-ingress flag must never appear in EXECUTABLE code. Comments may
    # name it (they explain why it is absent). `--no-allow-unauthenticated` does
    # NOT contain the substring `--allow-unauthenticated`, so this is a clean check.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--allow-unauthenticated" not in code, (
        "deploy.sh must NEVER request public ingress (licensing lock — see x5bz.5); "
        "opening it is a separate deliberate command, see docs/deploy-gcp.md §9"
    )


def test_deploy_does_not_hardcode_the_iam_mode() -> None:
    """It must not *close* ingress unconditionally either.

    `--no-allow-unauthenticated` on every deploy meant that once x5bz.1.6 opened
    the service, the next routine CI push — or the incident-response redeploy in
    docs/deploy-gcp.md §10, which runs during an incident — silently revoked every
    tester's access, handing them a Cloud Run IAM 403 at the edge with no sign-in
    page to explain it. The IAM mode has to be an input, and its default must
    leave the live policy alone.
    """
    text = _read(DEPLOY_SH)

    assert "ACCESS" in text, "deploy.sh must expose the IAM mode as an input (ACCESS)"
    assert 'ACCESS="${ACCESS:-preserve}"' in text, (
        "the default IAM mode must be `preserve` — a deploy must not change who "
        "may invoke the service unless explicitly asked to"
    )
    # The lock flag may still appear, but only inside the resolution logic — never
    # in the gcloud invocation itself, where it would apply to every deploy.
    deploy_call = text.split("gcloud run deploy", 1)[1]
    assert "--no-allow-unauthenticated" not in deploy_call, (
        "the gcloud run deploy call must take the IAM flags from the resolved "
        "ACCESS mode, not hardcode --no-allow-unauthenticated"
    )


@pytest.mark.parametrize(
    ("access", "expect_lock_flag"),
    [(None, False), ("preserve", False), ("locked", True)],
)
def test_dry_run_iam_flags_follow_the_access_mode(access, expect_lock_flag) -> None:
    """Default/preserve emits no IAM flag (policy untouched); locked emits one."""
    bash = _bash_or_skip()

    env = {**os.environ}
    env.pop("ACCESS", None)
    if access is not None:
        env["ACCESS"] = access

    result = subprocess.run(
        [bash, str(DEPLOY_SH), "--dry-run"],
        capture_output=True, text=True, timeout=30, cwd=REPO_ROOT, env=env,
    )
    assert result.returncode == 0, f"--dry-run exited {result.returncode}: {result.stderr}"
    plan = result.stdout.split("gcloud run deploy", 1)[1]
    assert ("--no-allow-unauthenticated" in plan) is expect_lock_flag, (
        f"ACCESS={access!r} should {'' if expect_lock_flag else 'not '}emit the lock flag:\n{plan}"
    )
    assert "--allow-unauthenticated" not in plan.replace("--no-allow-unauthenticated", "")


def test_deploy_rejects_an_unknown_access_mode() -> None:
    """Notably `ACCESS=public`: opening ingress must not be reachable from here."""
    bash = _bash_or_skip()

    result = subprocess.run(
        [bash, str(DEPLOY_SH), "--dry-run"],
        capture_output=True, text=True, timeout=30, cwd=REPO_ROOT,
        env={**os.environ, "ACCESS": "public"},
    )
    assert result.returncode != 0, "an unknown ACCESS mode must fail, not be ignored"


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


def test_deploy_sets_memory_and_concurrency_explicitly() -> None:
    """Cloud Run's defaults (512 MiB / 80 concurrent) are not survivable here:
    /auth/login runs a 64 MiB argon2 hash on every attempt, so a handful of
    simultaneous logins would push the instance past its memory limit and get it
    killed. Both limits must be stated, not inherited."""
    text = _read(DEPLOY_SH)

    assert "--memory" in text, (
        "deploy.sh must set --memory explicitly (argon2 is memory-hard; the "
        "512 MiB default leaves no headroom)"
    )
    assert "--concurrency" in text, (
        "deploy.sh must set --concurrency explicitly (the default of 80 allows "
        "far more simultaneous hashes than the instance can hold)"
    )


def test_deploy_wires_the_session_secret() -> None:
    """The auth session-signing key (x5bz.2) must reach the service as a Secret
    Manager reference. Without it the service fails closed — every auth endpoint
    503s and no tester can log in — so a deploy that drops it is a broken deploy."""
    text = _read(DEPLOY_SH)

    assert "SESSION_SECRET=" in text, (
        "deploy.sh must inject SESSION_SECRET (auth session signing key, x5bz.2)"
    )
    assert re.search(r"--set-secrets[^\n]*SESSION_SECRET=", text), (
        "SESSION_SECRET must come from --set-secrets (a Secret Manager reference), "
        "never an inlined value"
    )
    assert not re.search(r"--set-env-vars[^\n]*SESSION_SECRET=", text), (
        "SESSION_SECRET must never be passed as a plaintext env var"
    )


def test_deploy_dry_run_prints_commands_without_executing() -> None:
    """`deploy.sh --dry-run` prints the full gcloud plan and runs nothing — a safe,
    inspectable preview that needs neither gcloud nor docker (test #3)."""
    bash = _bash_or_skip()

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
    assert "access=" in out, "the plan must state which IAM mode it resolved to"

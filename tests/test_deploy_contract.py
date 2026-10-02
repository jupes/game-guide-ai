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


def test_the_connection_bounds_fit_the_database_at_full_scale_and_during_a_rollout() -> None:
    """`db-f1-micro` accepts 22 application connections. Every instance may have
    its gate, its realtime pool and one listener open at once (service/db.py,
    1kg.1.5), so the defaults and `--max-instances` are one number in two files:
    raise either without the other and logins start failing under load, which no
    unit test would show.

    `--max-instances` is per REVISION, and a rollout overlaps two of them. The
    gate is what is in use today, so it must fit that overlap too; the realtime
    pool and the listener must be added to that sum by the beads that turn them on."""
    from service.db import (
        MIGRATION_SESSIONS,
        RESERVED_FOR_OPERATORS,
        SERVER_CONNECTION_LIMIT,
        PoolSettings,
    )

    found = re.search(r"--max-instances\s+(\d+)", _read(DEPLOY_SH))
    assert found is not None, "deploy.sh must state --max-instances: the pool budget depends on it"
    instances = int(found.group(1))

    needed = PoolSettings().per_instance * instances + RESERVED_FOR_OPERATORS
    assert needed <= SERVER_CONNECTION_LIMIT, (
        f"{instances} instances x {PoolSettings().per_instance} connections + "
        f"{RESERVED_FOR_OPERATORS} for the operator = {needed}, but the database accepts "
        f"{SERVER_CONNECTION_LIMIT}"
    )

    overlapping_revisions = 2
    during_a_rollout = (
        PoolSettings().sync_max * instances * overlapping_revisions + MIGRATION_SESSIONS + RESERVED_FOR_OPERATORS
    )
    assert during_a_rollout <= SERVER_CONNECTION_LIMIT, (
        f"a rollout needs {during_a_rollout} connections for routes alone, but the database accepts "
        f"{SERVER_CONNECTION_LIMIT}: lower DB_POOL_MAX's default or --max-instances"
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


# ── Sign in with Google (lvs7) ───────────────────────────────────────────────

GOOGLE_ID = "123456789012-testclient.apps.googleusercontent.com"
GOOGLE_REDIRECT = "https://game-guide-ai-example.us-central1.run.app/auth/google/callback"


def _dry_run(**env: str) -> subprocess.CompletedProcess[str]:
    """`deploy.sh --dry-run` with a clean Google environment plus `env`."""
    bash = _bash_or_skip()
    base = {k: v for k, v in os.environ.items() if not k.startswith("GOOGLE_OAUTH")}
    return subprocess.run(
        [bash, str(DEPLOY_SH), "--dry-run"],
        capture_output=True, text=True, timeout=30, cwd=REPO_ROOT, env={**base, **env},
    )


def _flag(plan: str, name: str) -> str:
    found = re.search(rf"{re.escape(name)} (\S+)", plan)
    assert found, f"the plan prints no {name}"
    return found.group(1)


def test_google_is_off_by_default_and_the_plan_carries_nothing_for_it() -> None:
    result = _dry_run()
    assert result.returncode == 0, result.stderr
    assert "google=off" in result.stdout
    assert "GOOGLE_OAUTH" not in _flag(result.stdout, "--set-secrets")
    assert "GOOGLE_OAUTH" not in _flag(result.stdout, "--set-env-vars")


def test_google_on_carries_the_secret_by_reference_and_the_id_and_uri_as_env() -> None:
    """The secret is a --set-secrets reference, never a value and never an env var;
    both flags REPLACE the service's whole set on every deploy, so they live HERE."""
    result = _dry_run(GOOGLE_OAUTH_CLIENT_ID=GOOGLE_ID, GOOGLE_OAUTH_REDIRECT_URI=GOOGLE_REDIRECT)
    assert result.returncode == 0, result.stderr
    secrets = _flag(result.stdout, "--set-secrets")
    env = _flag(result.stdout, "--set-env-vars")
    assert "google=on" in result.stdout
    assert secrets.endswith(",GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest")
    assert "SESSION_SECRET=session-secret:latest" in secrets, "the existing references are kept"
    assert env.endswith(f",GOOGLE_OAUTH_CLIENT_ID={GOOGLE_ID},GOOGLE_OAUTH_REDIRECT_URI={GOOGLE_REDIRECT}")
    assert "AUTH_TRUSTED_PROXY_HOPS=1" in env
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in env


def test_the_secret_name_is_configurable_and_still_a_name() -> None:
    result = _dry_run(
        GOOGLE_OAUTH_CLIENT_ID=GOOGLE_ID, GOOGLE_OAUTH_REDIRECT_URI=GOOGLE_REDIRECT,
        GOOGLE_OAUTH_CLIENT_SECRET_SECRET="my-google-secret",
    )
    assert result.returncode == 0, result.stderr
    assert "GOOGLE_OAUTH_CLIENT_SECRET=my-google-secret:latest" in _flag(result.stdout, "--set-secrets")


@pytest.mark.parametrize(
    ("env", "named"),
    [
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": "not-a-google-id", "GOOGLE_OAUTH_REDIRECT_URI": GOOGLE_REDIRECT},
            "GOOGLE_OAUTH_CLIENT_ID", id="bad-client-id",
        ),
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": "a,b.apps.googleusercontent.com", "GOOGLE_OAUTH_REDIRECT_URI": GOOGLE_REDIRECT},
            "GOOGLE_OAUTH_CLIENT_ID", id="comma-in-client-id",
        ),
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": GOOGLE_ID, "GOOGLE_OAUTH_REDIRECT_URI": ""},
            "GOOGLE_OAUTH_REDIRECT_URI", id="id-without-uri",
        ),
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": "", "GOOGLE_OAUTH_REDIRECT_URI": GOOGLE_REDIRECT},
            "GOOGLE_OAUTH_CLIENT_ID", id="uri-without-id",
        ),
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": GOOGLE_ID, "GOOGLE_OAUTH_REDIRECT_URI": "http://x.run.app/auth/google/callback"},
            "GOOGLE_OAUTH_REDIRECT_URI", id="http-uri",
        ),
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": GOOGLE_ID, "GOOGLE_OAUTH_REDIRECT_URI": "https://x.run.app/auth/google/other"},
            "GOOGLE_OAUTH_REDIRECT_URI", id="wrong-path",
        ),
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": GOOGLE_ID, "GOOGLE_OAUTH_REDIRECT_URI": "https://x.run.app,y/auth/google/callback"},
            "GOOGLE_OAUTH_REDIRECT_URI", id="comma-in-uri",
        ),
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": GOOGLE_ID, "GOOGLE_OAUTH_REDIRECT_URI": GOOGLE_REDIRECT + " "},
            "GOOGLE_OAUTH_REDIRECT_URI", id="space-in-uri",
        ),
        pytest.param(
            {"GOOGLE_OAUTH_CLIENT_ID": GOOGLE_ID, "GOOGLE_OAUTH_REDIRECT_URI": "https://x.run.app=/auth/google/callback"},
            "GOOGLE_OAUTH_REDIRECT_URI", id="equals-in-uri",
        ),
    ],
)
def test_a_malformed_google_value_fails_the_deploy_naming_the_variable(env: dict[str, str], named: str) -> None:
    result = _dry_run(**env)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert named in result.stderr
    assert "gcloud run deploy" not in result.stdout, "nothing is planned, let alone run"


@pytest.mark.parametrize(
    "name",
    ["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cret", "1starts-with-digit", "has space", "has,comma", "a=b", "x" * 256],
)
def test_a_secret_name_that_could_be_a_secret_is_refused_and_never_printed(name: str) -> None:
    """The repository is public, so Actions logs are public and `run` prints whole
    commands: a client secret pasted into the NAME variable would be printed."""
    result = _dry_run(
        GOOGLE_OAUTH_CLIENT_ID=GOOGLE_ID, GOOGLE_OAUTH_REDIRECT_URI=GOOGLE_REDIRECT,
        GOOGLE_OAUTH_CLIENT_SECRET_SECRET=name,
    )
    assert result.returncode == 2
    assert "GOOGLE_OAUTH_CLIENT_SECRET_SECRET" in result.stderr
    assert name not in result.stdout and name not in result.stderr


def test_the_client_secret_itself_in_the_deploy_environment_is_refused_and_never_printed() -> None:
    leaked = "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-value"
    result = _dry_run(
        GOOGLE_OAUTH_CLIENT_ID=GOOGLE_ID, GOOGLE_OAUTH_REDIRECT_URI=GOOGLE_REDIRECT,
        GOOGLE_OAUTH_CLIENT_SECRET=leaked,
    )
    assert result.returncode == 2
    assert "GOOGLE_OAUTH_CLIENT_SECRET" in result.stderr
    assert leaked not in result.stdout and leaked not in result.stderr
    # Even with the feature off: the value has no business being in this environment.
    assert _dry_run(GOOGLE_OAUTH_CLIENT_SECRET=leaked).returncode == 2


def test_the_google_suffixes_are_conditional_and_the_old_contract_lines_are_intact() -> None:
    """Guards the TEXT too: an unconditional suffix would be invisible to an
    off-run test only if it were empty, and a literal secret reference in the flag
    line would turn every deploy of an unconfigured service red."""
    text = _read(DEPLOY_SH)
    lines = text.splitlines()
    secrets_line = next(line for line in lines if "--set-secrets" in line and "SESSION_SECRET=" in line)
    env_line = next(line for line in lines if "--set-env-vars" in line and "AUTH_TRUSTED_PROXY_HOPS" in line)
    assert "${GOOGLE_SECRET_REF}" in secrets_line and "GOOGLE_OAUTH_CLIENT_SECRET" not in secrets_line
    assert "${GOOGLE_ENV}" in env_line and "GOOGLE_OAUTH" not in env_line
    assert not re.search(r"--set-env-vars[^\n]*GOOGLE_OAUTH_CLIENT_SECRET=", text)
    assert not re.search(r"--set-env-vars[^\n]*(OPENAI_API_KEY|SESSION_SECRET)=", text)


# ── GM tools (po56) ──────────────────────────────────────────────────────────
#
# WORKBENCH_ENABLED_TOOLS is a repository VARIABLE the deploy job passes in. It has
# to ride in --set-env-vars like the Google values do: the flag REPLACES the
# service's whole env on every deploy, so a value set by hand is wiped by the next
# push. It holds a COMMA list, and gcloud splits --set-env-vars on commas, so a
# non-empty list switches the flag to gcloud's `^;^` custom-delimiter form.


def _tools_dry_run(**env: str) -> subprocess.CompletedProcess[str]:
    """`deploy.sh --dry-run` with no tool or capability variables except `env`."""
    bash = _bash_or_skip()
    base = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GOOGLE_OAUTH") and not k.startswith("WORKBENCH_")
    }
    return subprocess.run(
        [bash, str(DEPLOY_SH), "--dry-run"],
        capture_output=True, text=True, timeout=30, cwd=REPO_ROOT, env={**base, **env},
    )


def _env_vars(plan: str) -> dict[str, str]:
    """The `--set-env-vars` flag parsed the way gcloud parses it: an optional
    `^D^` prefix names the delimiter, else it is a comma."""
    raw = _flag(plan, "--set-env-vars")
    delimiter = ","
    custom = re.match(r"\^(.+?)\^", raw)
    if custom:
        delimiter, raw = custom.group(1), raw[custom.end():]
    pairs: dict[str, str] = {}
    for item in raw.split(delimiter):
        key, sep, value = item.partition("=")
        assert sep, f"{item!r} is not KEY=VALUE: gcloud would refuse this flag"
        assert key not in pairs, f"{key} is set twice"
        pairs[key] = value
    return pairs


def _registry_tool_ids() -> list[str]:
    from service.workbench_contracts import ToolId
    from service.workbench_registry import REGISTRY

    ids = [tool.value for tool in ToolId]
    assert sorted(ids) == sorted(tool.id.value for tool in REGISTRY.tools), "the enum and the registry agree"
    return ids


@pytest.mark.parametrize("value", [None, "", "   ", " , ,"], ids=["unset", "empty", "blank", "only-separators"])
def test_tools_are_off_unless_ids_are_named(value: str | None) -> None:
    result = _tools_dry_run() if value is None else _tools_dry_run(WORKBENCH_ENABLED_TOOLS=value)
    assert result.returncode == 0, result.stderr
    assert "tools=off" in result.stdout
    assert "WORKBENCH" not in result.stdout
    env = _flag(result.stdout, "--set-env-vars")
    assert not env.startswith("^"), "the plain comma form is kept when no list needs the custom delimiter"
    assert _env_vars(result.stdout) == {"AUTH_TRUSTED_PROXY_HOPS": "1", "GCP_PROJECT": "game-guide-ai-cloud"}


def test_named_tools_reach_the_service_in_set_env_vars() -> None:
    result = _tools_dry_run(WORKBENCH_ENABLED_TOOLS="npc,loot")
    assert result.returncode == 0, result.stderr
    assert "tools=on (npc,loot)" in result.stdout
    env = _env_vars(result.stdout)
    assert env["WORKBENCH_ENABLED_TOOLS"] == "npc,loot"
    assert env["AUTH_TRUSTED_PROXY_HOPS"] == "1", "the existing variables are kept"
    assert env["GCP_PROJECT"] == "game-guide-ai-cloud"


def test_a_single_tool_works_and_the_list_may_hold_every_registry_id() -> None:
    one = _tools_dry_run(WORKBENCH_ENABLED_TOOLS="npc")
    assert one.returncode == 0, one.stderr
    assert _env_vars(one.stdout)["WORKBENCH_ENABLED_TOOLS"] == "npc"
    ids = _registry_tool_ids()
    every = _tools_dry_run(WORKBENCH_ENABLED_TOOLS=",".join(ids))
    assert every.returncode == 0, every.stderr
    assert _env_vars(every.stdout)["WORKBENCH_ENABLED_TOOLS"] == ",".join(ids)


def test_the_list_is_normalised_before_it_reaches_the_service() -> None:
    """Spaces, empty entries and a repeated id are tidied; order is kept."""
    result = _tools_dry_run(WORKBENCH_ENABLED_TOOLS=" npc , loot,,npc ,names ")
    assert result.returncode == 0, result.stderr
    assert _env_vars(result.stdout)["WORKBENCH_ENABLED_TOOLS"] == "npc,loot,names"


def test_tools_and_google_together_keep_every_value_whole() -> None:
    """The custom delimiter must not leave a Google value split on a comma."""
    result = _tools_dry_run(
        WORKBENCH_ENABLED_TOOLS="npc,encounter",
        GOOGLE_OAUTH_CLIENT_ID=GOOGLE_ID, GOOGLE_OAUTH_REDIRECT_URI=GOOGLE_REDIRECT,
    )
    assert result.returncode == 0, result.stderr
    assert _env_vars(result.stdout) == {
        "AUTH_TRUSTED_PROXY_HOPS": "1",
        "GCP_PROJECT": "game-guide-ai-cloud",
        "GOOGLE_OAUTH_CLIENT_ID": GOOGLE_ID,
        "GOOGLE_OAUTH_REDIRECT_URI": GOOGLE_REDIRECT,
        "WORKBENCH_ENABLED_TOOLS": "npc,encounter",
    }
    assert "GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-client-secret:latest" in _flag(result.stdout, "--set-secrets")


@pytest.mark.parametrize(
    "value",
    [
        "nope",
        "npc,nope",
        "NPC",
        "image_generation",
        "npc;loot",
        "npc loot",
        "monster loot",
        "npc monster loot",
        "loot names",
        "monster\nloot",
        "npc\tloot",
        "npc,loot,^;^X=1",
        "npc\nloot",
        "../npc",
        "npc=1",
    ],
)
def test_an_unknown_tool_id_fails_the_deploy_before_anything_runs(value: str) -> None:
    result = _tools_dry_run(WORKBENCH_ENABLED_TOOLS=value)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "WORKBENCH_ENABLED_TOOLS" in result.stderr
    assert result.stdout == "", "not even the plan is printed, let alone a docker or gcloud command"
    assert value.strip() not in result.stderr, "a bad value is never echoed: Actions logs are public"


def test_a_refused_id_lists_the_valid_ones_so_the_fix_is_obvious() -> None:
    result = _tools_dry_run(WORKBENCH_ENABLED_TOOLS="nope")
    for tool in _registry_tool_ids():
        assert tool in result.stderr


def test_a_refusal_runs_no_command_on_a_real_deploy_either(tmp_path: Path) -> None:
    """Without --dry-run the script would call docker and gcloud. Put stubs that
    record themselves first on PATH and show neither ran."""
    bash = _bash_or_skip()
    log = tmp_path / "calls.log"
    for name in ("docker", "gcloud"):
        stub = tmp_path / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log.as_posix()}"\n', encoding="utf-8")
        stub.chmod(0o755)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE_OAUTH", "WORKBENCH_"))},
        "PATH": f"{tmp_path.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "WORKBENCH_ENABLED_TOOLS": "nope",
    }
    result = subprocess.run(
        [bash, str(DEPLOY_SH), "game-guide-ai", "abc1234"],
        capture_output=True, text=True, timeout=30, cwd=REPO_ROOT, env=env,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not log.exists(), log.read_text(encoding="utf-8") if log.exists() else ""


def test_capabilities_are_never_forwarded_so_image_generation_stays_off() -> None:
    """Portrait and map are paid (D-3). The script has no WORKBENCH_CAPABILITIES
    input at all, so a stray value in the deploy environment cannot switch them on."""
    result = _tools_dry_run(
        WORKBENCH_ENABLED_TOOLS="npc,portrait,map", WORKBENCH_CAPABILITIES="image_generation",
    )
    assert result.returncode == 0, result.stderr
    assert "image_generation" not in result.stdout
    assert "WORKBENCH_CAPABILITIES" not in result.stdout
    assert "WORKBENCH_CAPABILITIES" not in re.sub(r"#.*", "", _read(DEPLOY_SH))


def test_the_tool_id_list_in_deploy_sh_is_the_registrys() -> None:
    """deploy.sh cannot import Python, so it carries the ids as a word list. This
    is what keeps that list from drifting from `ToolId`: a new tool fails here
    until deploy.sh knows it, and a retired one cannot be deployed."""
    declared = re.search(r'^KNOWN_TOOL_IDS="([^"]*)"', _read(DEPLOY_SH), re.MULTILINE)
    assert declared, "deploy.sh must declare KNOWN_TOOL_IDS"
    assert sorted(declared.group(1).split()) == sorted(_registry_tool_ids())


def test_the_tools_value_is_only_ever_a_checked_variable_in_the_flag_line() -> None:
    text = _read(DEPLOY_SH)
    env_line = next(
        line for line in text.splitlines() if "--set-env-vars" in line and "AUTH_TRUSTED_PROXY_HOPS" in line
    )
    assert "${TOOLS_ENV}" in env_line and "WORKBENCH" not in env_line
    assert "${GOOGLE_ENV}" in env_line, "the Google suffix is still on the same line"
    assert 'ENABLED_TOOLS="${WORKBENCH_ENABLED_TOOLS:-}"' in text


# The runbook lists the ids that work today, derived from the code, so a new
# executor fails this test until the owner is told.


def _executor_tool_ids() -> list[str]:
    from service import card_generation, document_tools

    return sorted(
        {tool.value for tool in document_tools.LEAD_SENTENCES} | {tool.value for tool in card_generation.CARD_TOOLS}
    )


def _runbook_tools_section() -> str:
    text = _read(REPO_ROOT / "docs" / "deploy-gcp.md")
    found = re.search(r"^## 15\. GM tools.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert found, "docs/deploy-gcp.md needs a '## 15. GM tools' section"
    return found.group(0)


def test_the_runbook_lists_exactly_the_ids_that_have_executors() -> None:
    section = _runbook_tools_section()
    listed = re.search(r"^Working ids: (.+)$", section, re.MULTILINE)
    assert listed, "the section needs one 'Working ids: ...' line"
    assert sorted(re.findall(r"`([a-z]+)`", listed.group(1))) == _executor_tool_ids()


def test_the_runbook_names_the_variable_and_says_image_generation_stays_off() -> None:
    section = _runbook_tools_section()
    assert "WORKBENCH_ENABLED_TOOLS" in section
    assert "gh variable set WORKBENCH_ENABLED_TOOLS" in section
    assert "image_generation" in section and "off" in section
    for inert in ("recap", "portrait", "map"):
        assert f"`{inert}`" in section, f"{inert} has no executor today; the runbook says so"

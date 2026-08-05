"""
`scripts/bootstrap-db.sh` — the one-shot schema bootstrap for a managed database.

It is run by hand against a fresh production instance, and the files depend on
each other: `05-auth-schema.sql` adds the ownership foreign key onto a table
`04-chat-schema.sql` creates. Two defaults conspire to hide a failure — psql
continues past a SQL error unless told otherwise, and a shell block ending in
`echo` exits 0 whatever it printed. So the invariants worth testing are: apply
in order, stop at the first failure, and exit nonzero when you do.

A fake `psql` on PATH stands in for the database, which keeps this a test of the
script's control flow rather than of PostgreSQL.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_bootstrap_db.py -q
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from _bash import bash_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bootstrap-db.sh"
DSN = "postgresql://user:pw@localhost:5432/db"

#: In dependency order. 04/05 come from the canonical package location.
EXPECTED_ORDER = [
    "01-extensions.sql", "02-schema.sql", "03-hybrid-search.sql",
    "04-chat-schema.sql", "05-auth-schema.sql",
]


def _fake_psql(tmp_path: Path, fail_on: str = "") -> Path:
    """A `psql` that logs the file it was given and optionally fails on one."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "psql"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do :; done\n'
        'echo "${!#}" >> "$PSQL_LOG"\n'
        f'case "${{!#}}" in *{fail_on}) [ -n "{fail_on}" ] && exit 1 ;; esac\n'
        "exit 0\n",
        encoding="utf-8", newline="\n",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def _run(tmp_path: Path, *args: str, fail_on: str = "") -> tuple[int, list[str], str]:
    bash = bash_or_skip()
    bindir = _fake_psql(tmp_path, fail_on)
    log = tmp_path / "psql.log"
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "PSQL_LOG": str(log),
        "HOME": str(tmp_path),
    }
    result = subprocess.run(
        [bash, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT, env=env,
    )
    applied = [
        Path(line.strip()).name
        for line in (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])
    ]
    return result.returncode, applied, result.stdout + result.stderr


def test_applies_every_schema_file_in_dependency_order(tmp_path):
    code, applied, output = _run(tmp_path, DSN)
    assert code == 0, output
    assert applied == EXPECTED_ORDER


@pytest.mark.skipif(os.name == "nt", reason="no executable bit on Windows checkouts")
def test_runs_the_way_the_runbook_invokes_it(tmp_path):
    """`docs/deploy-gcp.md` says `scripts/bootstrap-db.sh <dsn>` — no `bash`
    prefix — so the script has to carry its own executable bit and shebang.

    Every other test here launches it as `bash <script>`, which works whether or
    not it is executable. That is precisely how a non-executable script reached
    a runbook: the suite was green and the documented command was not runnable.
    """
    bindir = _fake_psql(tmp_path)
    log = tmp_path / "psql.log"
    result = subprocess.run(
        [str(SCRIPT), DSN],  # NOT [bash, script] — the point of this test
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "PSQL_LOG": str(log), "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, (
        f"the documented invocation failed: {result.returncode}\n{result.stderr}"
    )
    applied = [Path(p.strip()).name for p in log.read_text(encoding="utf-8").splitlines()]
    assert applied == EXPECTED_ORDER


def test_stops_at_the_first_failure_and_exits_nonzero(tmp_path):
    """03 fails: 04 and 05 must never run, and the exit status must say so —
    otherwise automation reads a half-built database as ready."""
    code, applied, output = _run(tmp_path, DSN, fail_on="03-hybrid-search.sql")
    assert code == 1, f"a failed bootstrap must exit nonzero:\n{output}"
    assert applied == EXPECTED_ORDER[:3]
    assert "BOOTSTRAP FAILED" in output


def test_requires_a_dsn(tmp_path):
    code, _, _ = _run(tmp_path)
    assert code == 2


@pytest.mark.parametrize("name", EXPECTED_ORDER)
def test_every_file_it_names_actually_exists(name):
    """The script resolves paths relative to the repo root, so a moved schema
    file must fail here rather than at 2am against production."""
    text = SCRIPT.read_text(encoding="utf-8")
    rel = next(line.strip().strip('"') for line in text.splitlines() if name in line)
    assert (REPO_ROOT / rel).is_file(), f"{rel} does not exist"


def test_the_runbook_invokes_the_script_rather_than_inlining_a_loop():
    runbook = (REPO_ROOT / "docs" / "deploy-gcp.md").read_text(encoding="utf-8")
    assert "scripts/bootstrap-db.sh" in runbook
    assert "BOOTSTRAP_OK" not in runbook, "the loop is back in the docs; call the script"

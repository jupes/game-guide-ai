"""
Contract for the Cloud SQL bootstrap loop in `docs/deploy-gcp.md`.

That loop is run once, by hand, against a fresh production database, and the
schema files depend on each other — `05-auth-schema.sql` adds the ownership
foreign key onto a table `04-chat-schema.sql` creates. Two defaults conspire
against noticing a failure:

- `psql` continues after a SQL error unless told otherwise, and
- a shell block whose last command is `echo` **exits 0 no matter what it
  printed** — so a run that stopped halfway still reports success to whatever
  called it.

The snippet is extracted from the documentation itself rather than copied here.
A test against a copy would keep passing while the runbook people actually
follow drifted away from it.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_bootstrap_snippet.py -q
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from _bash import bash_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = REPO_ROOT / "docs" / "deploy-gcp.md"
SCHEMA_FILES = [
    "01-extensions.sql", "02-schema.sql", "03-hybrid-search.sql",
    "04-chat-schema.sql", "05-auth-schema.sql",
]


def _extract_loop() -> str:
    """The bootstrap loop as documented: `BOOTSTRAP_OK=1` through its closing `fi`."""
    lines = RUNBOOK.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "BOOTSTRAP_OK=1"]
    assert len(starts) == 1, f"expected exactly one bootstrap loop, found {len(starts)}"
    start = starts[0]
    ends = [i for i, line in enumerate(lines[start:], start) if line.strip() == "fi"]
    assert ends, "the bootstrap loop has no closing `fi`"
    snippet = "\n".join(lines[start:ends[0] + 1])
    assert "ON_ERROR_STOP=1" in snippet, "psql must be told to stop on SQL errors"
    return snippet


@pytest.fixture
def workspace(tmp_path: Path):
    """A tree the snippet can run in, with a stub psql that fails on demand."""
    (tmp_path / "vector-db" / "init").mkdir(parents=True)
    for name in SCHEMA_FILES:
        (tmp_path / "vector-db" / "init" / name).write_text(f"-- {name}\n", encoding="utf-8")
    (tmp_path / "bin").mkdir()

    def build(fail_on: str | None) -> Path:
        stub = tmp_path / "bin" / "psql"
        stub.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                # Record every invocation, and fail for one specific file.
                for arg in "$@"; do
                  case "$arg" in
                    */*.sql)
                      echo "${{arg##*/}}" >> "{tmp_path.as_posix()}/applied.txt"
                      case "${{arg##*/}}" in
                        {fail_on or "__never__"})
                          echo "ERROR: relation does not exist" >&2
                          exit 1
                          ;;
                      esac
                      ;;
                  esac
                done
                exit 0
                """
            ),
            encoding="utf-8",
        )
        stub.chmod(0o755)
        return tmp_path

    return build


def _run(bash: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [bash, "-c", f'PROXY="postgres://stub"\n{_extract_loop()}'],
        capture_output=True, text=True, timeout=60, cwd=root,
        env={"PATH": f"{(root / 'bin').as_posix()}:/usr/bin:/bin"},
    )


def _applied(root: Path) -> list[str]:
    log = root / "applied.txt"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


@pytest.fixture(scope="module")
def bash():
    return bash_or_skip()


def test_a_clean_bootstrap_applies_every_file_and_succeeds(bash, workspace) -> None:
    root = workspace(fail_on=None)
    result = _run(bash, root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _applied(root) == SCHEMA_FILES
    assert "complete" in result.stdout


def test_a_failure_stops_before_the_dependent_files(bash, workspace) -> None:
    """`05` must not run against a database where `04` never created its table."""
    root = workspace(fail_on="03-hybrid-search.sql")
    result = _run(bash, root)
    assert _applied(root) == SCHEMA_FILES[:3], "the loop continued past the failure"
    assert "04-chat-schema.sql" not in result.stdout.replace("==> ", "!")
    assert "BOOTSTRAP FAILED at 03-hybrid-search.sql" in result.stderr


def test_a_failure_exits_nonzero(bash, workspace) -> None:
    """The reported hole: the block printed INCOMPLETE and still returned 0,
    because its last command was a successful `echo`. Automation reading the
    exit status would treat a half-bootstrapped database as ready."""
    root = workspace(fail_on="03-hybrid-search.sql")
    result = _run(bash, root)
    assert result.returncode != 0, (
        "a failed bootstrap must report failure through its exit status, not "
        "only in prose:\n" + result.stdout + result.stderr
    )
    assert "INCOMPLETE" in result.stderr, "the diagnostic must survive too"


def test_a_failure_on_the_last_file_also_exits_nonzero(bash, workspace) -> None:
    """Nothing depends on 05, so the loop ends normally — the status must still
    say the database is not fully bootstrapped."""
    root = workspace(fail_on="05-auth-schema.sql")
    result = _run(bash, root)
    assert result.returncode != 0, result.stdout + result.stderr
    assert _applied(root) == SCHEMA_FILES

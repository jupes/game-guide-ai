from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci.pr_job_scope import classify_changes, scope_at_merge


@pytest.mark.parametrize(("paths", "scope"), [
    ((b"docs/ci.md", b"docs/notes with spaces.md"), "docs"),
    ((b"ui/src/App.tsx", b"ui/e2e/story.spec.ts"), "ui"),
    ((b"service/app.py", b"tests/test_config.py", b"ingestion/index.py", b"config.py"), "python"),
    ((b"docs/ci.md", b"ui/src/App.tsx"), "full"),
    ((b"ui/src/App.tsx", b"service/app.py"), "full"),
    ((b"old/file",), "full"),
    ((), "full"),
])
def test_scope_for_each_supported_group(paths: tuple[bytes, ...], scope: str) -> None:
    assert classify_changes(paths) == scope


@pytest.mark.parametrize("shared", [
    b".github/workflows/ci.yml", b"pyproject.toml", b"uv.lock", b"ui/package.json", b"ui/bun.lock",
    b"Dockerfile", b"ui/Dockerfile.dev", b"contracts/workbench/schema.json",
])
def test_shared_inputs_keep_every_check(shared: bytes) -> None:
    assert classify_changes((b"ui/src/App.tsx", shared)) == "full"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=CI scope test", "-c", "user.email=ci@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )


@pytest.mark.parametrize("rename_python_file", [False, True])
def test_scope_reads_the_merge_against_its_first_parent(tmp_path: Path, rename_python_file: bool) -> None:
    _git(tmp_path, "init", "-b", "trunk")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/note.md").write_text("Original\n", encoding="utf-8")
    (tmp_path / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Base")
    assert scope_at_merge(tmp_path) == "full", "a checkout without a test merge cannot safely skip"
    _git(tmp_path, "switch", "-c", "proposal")
    if rename_python_file:
        _git(tmp_path, "mv", "config.py", "docs/config-example.md")
    else:
        (tmp_path / "docs/note.md").write_text("Changed\n", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "Proposal")
    _git(tmp_path, "switch", "trunk")
    (tmp_path / "pyproject.toml").write_text("# Base moved\n", encoding="utf-8")
    _git(tmp_path, "add", "pyproject.toml")
    _git(tmp_path, "commit", "-m", "Update the base branch")
    _git(tmp_path, "merge", "--no-ff", "-m", "Test merge", "proposal")
    assert scope_at_merge(tmp_path) == ("full" if rename_python_file else "docs")

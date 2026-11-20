"""Classify a GitHub pull request by the changes in its test merge commit."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

SHARED_FILES = {b"pyproject.toml", b"uv.lock", b"package.json", b"bun.lock", b"bun.lockb"}
PYTHON_DIRECTORIES = (b"service/", b"tests/", b"ingestion/")


def classify_changes(paths: Sequence[bytes]) -> str:
    if not paths:
        return "full"
    for path in paths:
        name = path.rsplit(b"/", 1)[-1]
        if (
            path.startswith((b".github/", b"contracts/"))
            or name in SHARED_FILES
            or name == b"Dockerfile"
            or name.startswith(b"Dockerfile.")
            or name.endswith(b".Dockerfile")
        ):
            return "full"
    if all(path.startswith(b"docs/") for path in paths):
        return "docs"
    if all(path.startswith(b"ui/") for path in paths):
        return "ui"
    if all(path == b"config.py" or path.startswith(PYTHON_DIRECTORIES) for path in paths):
        return "python"
    return "full"


def scope_at_merge(checkout: Path) -> str:
    try:
        parents = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
            check=True, capture_output=True, cwd=checkout,
        ).stdout.split()
        if len(parents) != 3:
            return "full"
        # The first parent is the base used to prepare the test merge. Count
        # both names of a rename so moving a file out of a job's scope runs it.
        changed = subprocess.run(
            ["git", "diff", "--name-only", "-z", "--no-renames", "HEAD^1", "HEAD", "--"],
            check=True, capture_output=True, cwd=checkout,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "full"
    return classify_changes(changed.rstrip(b"\0").split(b"\0") if changed else ())


if __name__ == "__main__":
    print(f"scope={scope_at_merge(Path.cwd())}")

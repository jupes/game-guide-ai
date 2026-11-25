"""Count additions in a proposed GitHub merge, excluding test files."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

WARN_LINES = 5_000
FAIL_LINES = 15_000
RELEASE_LABEL = "release-train"
TEST_PATHS = (
    "**/tests/**",
    "ui/e2e/**",
    "ui/integration/**",
    "ui/src/**/*.test.*",
    "ui/src/**/*.spec.*",
)


def added_lines(checkout: Path) -> int:
    diff = subprocess.run(
        [
            "git", "diff", "--numstat", "-z", "--find-renames",
            "HEAD^1", "HEAD", "--", ".", *(f":(exclude,glob){path}" for path in TEST_PATHS),
        ],
        cwd=checkout, capture_output=True, check=True,
    ).stdout
    records = iter(diff.split(b"\0"))
    total = 0
    for record in records:
        if not record:
            continue
        added, _, path = record.split(b"\t", 2)
        if not path:
            # With -z a rename carries two more NUL-terminated paths.
            next(records)
            next(records)
        if added != b"-":
            total += int(added)
    return total


def report_size(lines: int, *, release_train: bool) -> int:
    message = f"This pull request adds {lines:,} non-test lines."
    if lines > FAIL_LINES and not release_train:
        print(f"::error::{message} Split the change or use the {RELEASE_LABEL} label for a release train.")
        return 1
    if lines > WARN_LINES:
        suffix = " The release-train exception applies." if lines > FAIL_LINES else ""
        print(f"::warning::{message}{suffix}")
    else:
        print(message)
    return 0


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    labels = {label["name"] for label in event["pull_request"]["labels"]}
    raise SystemExit(report_size(added_lines(Path.cwd()), release_train=RELEASE_LABEL in labels))

"""Review labels belong in Beads and PR reviews. Check new production lines."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

REVIEW_TAG = re.compile(r"\b(?:critic item \d+|PR #\d+ review|release review S\d+)\b", re.IGNORECASE)
BEAD_ID = re.compile(r"\bagent-forge-harness-[a-z0-9]+(?:\.\d+)*\b", re.IGNORECASE)
COMMENT = re.compile(r"(?:^|\s)(?:#|//|/\*|\*)\s*(.*)$")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _git(checkout: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=checkout, capture_output=True, check=True,
    ).stdout


def _is_production(path: str) -> bool:
    if path == "config.py":
        return True
    if path.startswith("service/"):
        return path.endswith(".py") and not path.startswith("service/tests/")
    return (
        path.startswith("ui/src/")
        and path.endswith((".ts", ".tsx", ".js", ".jsx", ".css"))
        and re.search(r"\.(test|spec|stories)\.", path) is None
        and not path.endswith("/test-setup.ts")
    )


def _added_line_numbers(patch: str) -> Iterator[tuple[int, str]]:
    number = None
    for line in patch.splitlines():
        hunk = HUNK.match(line)
        if hunk:
            number = int(hunk[1])
        elif number is not None and line.startswith("+"):
            yield number, line[1:]
            number += 1
        elif number is not None and line.startswith(" "):
            number += 1


def additions(checkout: Path) -> Iterator[tuple[str, int, str]]:
    records = iter(_git(
        checkout, "diff", "--name-status", "--diff-filter=AMR", "--find-renames", "-z", "HEAD^1", "HEAD",
    ).split(b"\0"))
    while status := next(records, b""):
        previous = os.fsdecode(next(records))
        path = os.fsdecode(next(records)) if status.startswith(b"R") else previous
        if not _is_production(path):
            continue
        lines: Iterator[tuple[int, str]]
        if status == b"A":
            source = _git(checkout, "show", f"HEAD:{path}").decode("utf-8")
            lines = enumerate(source.splitlines(), start=1)
        else:
            # Compare the two blobs so a rename does not count old text as new.
            patch = _git(
                checkout, "diff", "--no-ext-diff", "--no-color", "-U0", f"HEAD^1:{previous}", f"HEAD:{path}",
            ).decode("utf-8")
            lines = _added_line_numbers(patch)
        for number, line in lines:
            yield path, number, line


def check(checkout: Path) -> int:
    failed = False
    for path, number, source in additions(checkout):
        location = path.replace("%", "%25").replace(",", "%2C").replace(":", "%3A")
        location = location.replace("\r", "%0D").replace("\n", "%0A")
        if REVIEW_TAG.search(source):
            print(f"::error file={location},line={number}::Replace the review tag with the relevant design rule.")
            failed = True
        comment = COMMENT.search(source.lstrip())
        if comment and BEAD_ID.search(comment[1]):
            print(f"::warning file={location},line={number}::Consider citing the ADR rule instead of a Beads ID.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(check(Path.cwd()))

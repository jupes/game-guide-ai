"""
Finding a bash that can actually run.

`shutil.which("bash")` only proves something is on PATH. On Windows it finds
`C:\\Windows\\System32\\bash.exe` — the WSL launcher, present on every modern
install — which exits non-zero when no distribution is registered. Tests that
trust it then *fail* instead of skipping, on machines with a perfectly good Git
Bash one PATH entry away. So: prefer Git Bash on Windows, and prove whatever we
picked starts before handing it a script.

Shared by the shell-script contract suites (`test_deploy_contract.py`,
`test_throttle_verifier.py`).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@functools.lru_cache(maxsize=1)
def working_bash() -> str | None:
    """Path to a bash that responds to `--version`, or None."""
    candidates: list[str] = []
    if os.name == "nt":
        git = shutil.which("git")
        if git:  # <git>/cmd/git.exe -> <git>/bin/bash.exe
            candidates.append(str(Path(git).resolve().parent.parent / "bin" / "bash.exe"))
        candidates += [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ]
    on_path = shutil.which("bash")
    if on_path:
        candidates.append(on_path)

    for candidate in candidates:
        if not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, "--version"], capture_output=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def bash_or_skip() -> str:
    bash = working_bash()
    if bash is None:
        pytest.skip("no working bash to exercise the shell scripts (CI always has one)")
    return bash

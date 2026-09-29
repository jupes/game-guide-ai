"""pytest wiring for the canary leak-test harness: the ``canary_world`` and ``leak_capture`` fixtures.

Registered once, in the repository's root ``conftest.py``, so every test directory can use them.
Every pytest run loads this module, so it imports only pytest and the standard library at import
time; the harness itself is imported inside the fixtures. No autouse fixture, marker or option: a
test that does not ask for the harness is untouched by it.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from service.tests.canary.core import CanaryWorld
    from service.tests.canary.sinks import LeakCapture

#: Whether the test's call phase passed; read by ``leak_capture`` at teardown.
_CALL_PASSED = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None],
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Store the call phase's outcome, and do nothing else."""
    report = yield
    if report.when == "call":
        item.stash[_CALL_PASSED] = report.passed
    return report


@pytest.fixture
def canary_world(request: pytest.FixtureRequest) -> CanaryWorld:
    """A fresh world seeded by the test's node id, so a failure reproduces with the same tokens."""
    from service.tests.canary.core import CanaryWorld

    return CanaryWorld(seed=request.node.nodeid)


@pytest.fixture
def leak_capture(
    canary_world: CanaryWorld,
    monkeypatch: pytest.MonkeyPatch,
    capteesys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
) -> Generator[LeakCapture]:
    """An entered ``LeakCapture``. Request it first, so it also captures what later fixtures log.

    Standard output is read through ``capteesys`` because pytest swaps ``sys.stdout`` between test
    phases (C-2), so this fixture cannot be combined with ``capsys``, ``capfd`` or a test's own
    ``capteesys.readouterr()``. At teardown it raises ``CaptureNotAsserted`` when the call phase
    passed without ``assert_clean`` having run, and ``CanaryLeak`` for anything captured after the
    last ``assert_clean``."""
    from service.tests.canary.sinks import LeakCapture

    def read_stdio() -> tuple[str, str]:
        captured = capteesys.readouterr()
        return captured.out, captured.err

    capture = LeakCapture(canary_world, monkeypatch=monkeypatch, stdio_source=read_stdio)
    capture.__enter__()
    yield capture
    capture._exit(call_failed=not request.node.stash.get(_CALL_PASSED, False))

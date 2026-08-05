"""
Shared test wiring for the service suite.

x5bz.2 guarded /chat + /conversations/* behind `require_session`. The pre-auth
tests (which predate auth and exercise chat/history/attachment behavior, not
auth) shouldn't each have to mint a session — so by default every test runs as
an authenticated user via a dependency override. Tests that verify auth ITSELF
(the 401/403 guard, signup/login) opt out with `@pytest.mark.real_auth` to hit
the real `require_session`.
"""

from __future__ import annotations

import pytest

from service import ratelimit
from service.app import app, require_session
from service.session import SessionData


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_auth: exercise the real require_session guard (auth tests opt out of the default session)",
    )


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Give every test its own attempt budget.

    The limiters are process-global module state, and `TestClient` has no real
    peer address, so every test in the run shares one source key. Without this,
    signups/logins accumulate across unrelated tests until some later test —
    whichever one happens to cross the threshold — starts getting 429s and fails
    for reasons that have nothing to do with what it is testing. Tests that
    exercise throttling build their own budget inside the test.
    """
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


@pytest.fixture(autouse=True)
def _default_session(request: pytest.FixtureRequest):
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=1, role="dm")
    yield
    app.dependency_overrides.pop(require_session, None)

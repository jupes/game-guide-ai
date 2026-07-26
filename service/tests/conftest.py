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

from service.app import app, require_session
from service.session import SessionData


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_auth: exercise the real require_session guard (auth tests opt out of the default session)",
    )


@pytest.fixture(autouse=True)
def _default_session(request: pytest.FixtureRequest):
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=1, role="dm")
    yield
    app.dependency_overrides.pop(require_session, None)

"""Rate limiting on /chat (x5bz.3 Checkpoint A).

Every /chat call spends the operator's OpenAI key, and the $10 GCP budget
kill-switch does not cover OpenAI — it detaches billing on the Cloud project,
while tokens are billed by a separate vendor. With public ingress open, the
application-side limit is the only bound on what a loop can spend.

This is the per-tester half: a sliding window keyed on the SESSION user id, so
one tester cannot spend everyone's budget. The key is the authenticated
identity rather than the client IP because testers are invited and known —
keying on IP would lump a household behind one NAT into a single budget while
handing anyone on a fresh address a new one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import config
from service import ratelimit
from service.app import app, get_auth_store, get_service
from service.auth_store import InMemoryAuthStore
from service.models import ChatMode, ChatResponse

pytestmark = pytest.mark.real_auth


class _FakeService:
    """Answers without an LLM — this suite is about the guard, not retrieval."""

    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(answer="ok", sources=[], answerable=True,
                            mode=ChatMode(mode), conversation_id=conversation_id)


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    ratelimit.reset_all()
    s = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: s
    app.dependency_overrides[get_service] = lambda: _FakeService()
    yield s
    app.dependency_overrides.clear()
    ratelimit.reset_all()


def _client_as(store, role: str = "player", email: str | None = None) -> TestClient:
    """A signed-in TestClient. Each call is a distinct account, so the per-user
    budgets under test are genuinely separate identities."""
    token = store.create_invite(
        role=role, expires_at=datetime.now(UTC) + timedelta(days=1),
    ).token
    client = TestClient(app)
    client.post("/auth/signup", json={
        "email": email or f"{role}@example.com",
        "password": "password123",
        "invite": token,
    })
    return client


def _ask(client: TestClient) -> int:
    return client.post("/chat", json={"prompt": "what does fireball do"}).status_code


# ── Behavior 1 (tracer): the budget is enforced ──────────────────────────────


def test_a_tester_past_their_hourly_budget_is_refused(store) -> None:
    """The tracer: an authenticated request reaches the guard with a session in
    hand, and the guard's refusal surfaces as 429 rather than being swallowed
    into a 500 by the endpoint's own error taxonomy."""
    client = _client_as(store)
    budget = config.CHAT_RATE_LIMIT_PER_USER

    allowed = [_ask(client) for _ in range(budget)]
    assert allowed == [200] * budget, "the budget must be spendable in full"
    assert _ask(client) == 429, "the request past the budget must be refused"


def _spend_budget(client: TestClient):
    """Exhaust the budget and return the first refused response."""
    for _ in range(config.CHAT_RATE_LIMIT_PER_USER):
        _ask(client)
    return client.post("/chat", json={"prompt": "one too many"})


# ── Behavior 2: the refusal is legible, and identifiably ours ────────────────


def test_a_throttled_chat_says_who_threw_it_and_when_to_return(store) -> None:
    """Cloud Run returns a 429 of its own when no instance is available, so the
    status code cannot prove our limiter fired. The marker says it did, and its
    value says which control — the UI needs different words for "slow down" and
    "the day's budget is gone"."""
    from service.app import CHAT_THROTTLE_HEADER

    refused = _spend_budget(_client_as(store))
    assert refused.status_code == 429
    assert refused.headers[CHAT_THROTTLE_HEADER] == "user"
    assert int(refused.headers["retry-after"]) > 0


def test_an_ordinary_answer_carries_no_throttle_marker(store) -> None:
    from service.app import CHAT_THROTTLE_HEADER

    ok = _client_as(store).post("/chat", json={"prompt": "what does fireball do"})
    assert ok.status_code == 200
    assert CHAT_THROTTLE_HEADER.lower() not in {k.lower() for k in ok.headers}


def test_retry_after_reflects_the_chat_window_not_the_auth_window(store) -> None:
    """Regression: `_build` used to hardcode AUTH_RATE_LIMIT_WINDOW_S. A chat
    limiter constructed through it would have silently enforced 20 requests per
    5 MINUTES while the config said an hour — twelve times tighter, with nothing
    failing to say so. Retry-After is the observable tell."""
    refused = _spend_budget(_client_as(store))
    retry_after = int(refused.headers["retry-after"])
    assert retry_after > config.AUTH_RATE_LIMIT_WINDOW_S, (
        f"Retry-After of {retry_after}s fits the auth window "
        f"({config.AUTH_RATE_LIMIT_WINDOW_S}s), not the chat window "
        f"({config.CHAT_RATE_LIMIT_WINDOW_S}s) — the wrong window is in use"
    )


# ── Behavior 3: budgets belong to people, not to the service ─────────────────


def test_one_tester_burning_their_budget_does_not_silence_another(store) -> None:
    """The failure this rules out is a shared key: with public ingress open, a
    single enthusiastic tester must not be able to close the pilot for everyone
    else. That is the daily cap's job, deliberately, and not this limiter's."""
    heavy = _client_as(store, email="heavy@example.com")
    quiet = _client_as(store, email="quiet@example.com")

    assert _spend_budget(heavy).status_code == 429
    assert _ask(quiet) == 200, "a second tester's budget must be untouched"


# ── Behavior 10: misconfiguration fails at startup, not at request time ──────


def test_a_nonsense_chat_budget_refuses_to_build_and_names_the_env_var() -> None:
    """Startup contract with no public surface of its own: a limiter that cannot
    limit must stop the container rather than quietly admitting everything. The
    message has to name the variable, or the operator is left bisecting env."""
    with pytest.raises(ValueError, match="CHAT_RATE_LIMIT_PER_USER"):
        ratelimit._build(
            0, config.CHAT_RATE_LIMIT_WINDOW_S,
            "CHAT_RATE_LIMIT_PER_USER", "CHAT_RATE_LIMIT_WINDOW_S",
        )

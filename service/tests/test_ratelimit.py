"""Auth rate limiting (x5bz.2) — brute force + request-slot starvation.

The hashing semaphore bounds memory but not *attempts*: without a limit,
/auth/login accepts unlimited guesses and unlimited concurrent callers, each of
which can hold a request slot while waiting for a hashing slot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from service import ratelimit
from service.app import app, get_auth_store
from service.auth_store import InMemoryAuthStore
from service.ratelimit import RateLimited, SlidingWindowLimiter

pytestmark = pytest.mark.real_auth


# ── The limiter itself ───────────────────────────────────────────────────────


def test_allows_up_to_the_limit_then_raises() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)
    for _ in range(3):
        limiter.check("k", now=100.0)
    with pytest.raises(RateLimited):
        limiter.check("k", now=100.0)


def test_window_slides_so_a_throttled_caller_recovers() -> None:
    """Decay, not lockout — a legitimate user recovers without an admin."""
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    limiter.check("k", now=0.0)
    limiter.check("k", now=1.0)
    with pytest.raises(RateLimited):
        limiter.check("k", now=2.0)
    limiter.check("k", now=61.5)  # the first two have aged out


def test_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("a", now=0.0)
    limiter.check("b", now=0.0)  # unaffected by a's budget


def test_retry_after_is_a_positive_whole_number() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("k", now=0.0)
    with pytest.raises(RateLimited) as exc:
        limiter.check("k", now=10.0)
    assert isinstance(exc.value.retry_after, int)
    assert 0 < exc.value.retry_after <= 61


def test_key_table_is_capped_so_the_limiter_is_not_itself_a_memory_leak() -> None:
    """An attacker rotating emails/IPs must not be able to grow the limiter into
    the memory problem it exists to prevent."""
    limiter = SlidingWindowLimiter(limit=5, window_seconds=60, max_keys=10)
    for i in range(500):
        limiter.check(f"key-{i}", now=float(i))
    assert len(limiter._hits) <= 10  # noqa: SLF001


# ── Enforcement on the endpoints ─────────────────────────────────────────────


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    ratelimit.reset_all()
    s = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: s
    yield s
    app.dependency_overrides.clear()
    ratelimit.reset_all()


def _login(client: TestClient, email: str = "a@example.com", ip: str = "203.0.113.1"):
    return client.post(
        "/auth/login",
        json={"email": email, "password": "password123"},
        headers={"x-forwarded-for": ip},
    )


def test_repeated_failed_logins_are_throttled_with_retry_after(store) -> None:
    client = TestClient(app)
    statuses = [_login(client).status_code for _ in range(config.AUTH_RATE_LIMIT_PER_ACCOUNT + 3)]
    assert 401 in statuses, "the first attempts should be ordinary failures"
    assert 429 in statuses, "sustained guessing must be throttled, not just slow"

    throttled = _login(client)
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) > 0


def test_throttling_is_per_account_not_global(store) -> None:
    """One account being ground down must not lock everyone else out."""
    client = TestClient(app)
    for _ in range(config.AUTH_RATE_LIMIT_PER_ACCOUNT + 2):
        _login(client, email="victim@example.com")
    assert _login(client, email="victim@example.com").status_code == 429
    # A different account, same source, still within the (looser) source budget.
    assert _login(client, email="someone-else@example.com").status_code == 401


def test_one_source_spraying_many_accounts_is_throttled(store) -> None:
    """Rotating the email defeats the account limiter; the source limiter is
    what still protects the instance's request slots."""
    client = TestClient(app)
    statuses = [
        _login(client, email=f"user{i}@example.com").status_code
        for i in range(config.AUTH_RATE_LIMIT_PER_SOURCE + 5)
    ]
    assert statuses[-1] == 429


def test_signup_is_throttled_too(store) -> None:
    """Signup hashes as well, so it needs the same ceiling."""
    client = TestClient(app)
    body = {"email": "new@example.com", "password": "password123", "invite": "nope"}
    statuses = [
        client.post("/auth/signup", json=body, headers={"x-forwarded-for": "198.51.100.7"}).status_code
        for _ in range(config.AUTH_RATE_LIMIT_PER_ACCOUNT + 3)
    ]
    assert 429 in statuses


def test_throttle_runs_before_any_hashing(store, monkeypatch) -> None:
    """The point of the limit is to cap how much argon2 work an anonymous caller
    can trigger — so it must be checked before the hash, not after."""
    from service import app as app_module

    hashes: list[str] = []
    monkeypatch.setattr(
        app_module, "verify_password",
        lambda stored, pw: hashes.append(pw) or False,
    )
    client = TestClient(app)
    for _ in range(config.AUTH_RATE_LIMIT_PER_ACCOUNT):
        _login(client)
    before = len(hashes)
    assert _login(client).status_code == 429
    assert len(hashes) == before, "a throttled attempt must not reach the hasher"


def test_a_successful_login_still_works_within_budget(store) -> None:
    invite = store.create_invite(
        role="player", expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ).token
    client = TestClient(app)
    client.post(
        "/auth/signup",
        json={"email": "ada@example.com", "password": "password123", "invite": invite},
    )
    r = client.post("/auth/login", json={"email": "ada@example.com", "password": "password123"})
    assert r.status_code == 200

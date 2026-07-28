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


# ── Configuration must fail loudly, never silently disable the limit ─────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0, "window_seconds": 60},      # can never be satisfied
        {"limit": -1, "window_seconds": 60},
        {"limit": 5, "window_seconds": 0},       # every hit expires instantly
        {"limit": 5, "window_seconds": -30},
        {"limit": 5, "window_seconds": 60, "max_keys": 0},
        # nan fails every comparison, so `<= 0` waves it through; inf passes too.
        # Both reach the retry_after arithmetic and blow up there, turning
        # /auth/login into a 500 on the second attempt.
        {"limit": 5, "window_seconds": float("nan")},
        {"limit": 5, "window_seconds": float("inf")},
        {"limit": 5, "window_seconds": float("-inf")},
    ],
)
def test_invalid_configuration_is_rejected_at_construction(kwargs) -> None:
    """These values are env-derived. A zero/negative window would silently turn
    throttling OFF while every request still looks fine — the worst failure mode
    for a security control — so the limiter refuses to exist instead."""
    with pytest.raises(ValueError):
        SlidingWindowLimiter(**kwargs)


# ── Source identity: the header is caller-writable ───────────────────────────


class _Req:
    """Minimal stand-in for a Starlette Request (headers + peer)."""

    def __init__(self, xff: str | None = None, peer: str = "10.0.0.1"):
        self.headers = {} if xff is None else {"x-forwarded-for": xff}
        self.client = type("C", (), {"host": peer})()


def test_untrusted_deployment_ignores_the_header_entirely(monkeypatch) -> None:
    """With no trusted proxy, X-Forwarded-For is just text the caller typed."""
    monkeypatch.setattr(config, "AUTH_TRUSTED_PROXY_HOPS", 0)
    assert ratelimit.client_source(_Req("1.2.3.4", peer="10.0.0.1")) == "10.0.0.1"


def test_only_the_trusted_hop_is_read_not_the_caller_supplied_prefix(monkeypatch) -> None:
    """Google PRESERVES what the client sent and APPENDS its own observation, so
    the leftmost entry is attacker-controlled. Read from the right instead."""
    monkeypatch.setattr(config, "AUTH_TRUSTED_PROXY_HOPS", 1)
    req = _Req("9.9.9.9, 8.8.8.8, 203.0.113.7")  # first two are spoofed
    assert ratelimit.client_source(req) == "203.0.113.7"


def test_two_hops_reads_past_the_load_balancer(monkeypatch) -> None:
    monkeypatch.setattr(config, "AUTH_TRUSTED_PROXY_HOPS", 2)
    req = _Req("9.9.9.9, 203.0.113.7, 130.211.0.1")  # client, then the LB
    assert ratelimit.client_source(req) == "203.0.113.7"


def test_a_chain_shorter_than_the_trusted_hops_falls_back_to_the_peer(monkeypatch) -> None:
    """Fewer entries than our proxies append ⇒ our proxies did not write this."""
    monkeypatch.setattr(config, "AUTH_TRUSTED_PROXY_HOPS", 2)
    assert ratelimit.client_source(_Req("1.2.3.4", peer="10.0.0.1")) == "10.0.0.1"


@pytest.mark.parametrize(
    "value", ["not-an-ip", "", "   ", "x" * 200, "999.999.999.999", "<script>"],
)
def test_a_non_ip_in_the_trusted_position_falls_back_to_the_peer(monkeypatch, value) -> None:
    """Junk must not become a rate-limit key — that is free budget per variant."""
    monkeypatch.setattr(config, "AUTH_TRUSTED_PROXY_HOPS", 1)
    assert ratelimit.client_source(_Req(value, peer="10.0.0.1")) == "10.0.0.1"


def test_equivalent_spellings_collapse_to_one_key(monkeypatch) -> None:
    """`::1`, its expanded form and a bracketed host:port are one caller; three
    keys would be three budgets."""
    monkeypatch.setattr(config, "AUTH_TRUSTED_PROXY_HOPS", 1)
    keys = {
        ratelimit.client_source(_Req(v))
        for v in ("::1", "0:0:0:0:0:0:0:1", "[::1]:443")
    }
    assert keys == {"::1"}
    assert ratelimit.client_source(_Req("203.0.113.7:51234")) == "203.0.113.7"


def test_rotating_a_spoofed_header_does_not_buy_extra_budget(monkeypatch, store) -> None:
    """The reported attack: rotate X-Forwarded-For *and* the email, and every
    request reaches the dummy argon2 hash. With the header untrusted, the source
    budget still binds."""
    monkeypatch.setattr(config, "AUTH_TRUSTED_PROXY_HOPS", 0)
    client = TestClient(app)
    statuses = [
        client.post(
            "/auth/login",
            json={"email": f"nobody{i}@example.com", "password": "password123"},
            headers={"x-forwarded-for": f"198.51.100.{i}"},  # rotating, spoofed
        ).status_code
        for i in range(config.AUTH_RATE_LIMIT_PER_SOURCE + 5)
    ]
    assert 429 in statuses, "a spoofed source must not mint a fresh budget"


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


def test_a_throttled_response_is_marked_as_ours(store) -> None:
    """Cloud Run returns 429 itself when no instance is available, so a status
    code alone cannot prove OUR limiter fired — and the deployed verifier would
    otherwise certify a broken proxy config on a platform hiccup. Every throttle
    response carries the marker; nothing else does."""
    from service.app import AUTH_THROTTLE_HEADER

    client = TestClient(app)
    ordinary = _login(client)
    assert ordinary.status_code == 401
    assert AUTH_THROTTLE_HEADER.lower() not in {k.lower() for k in ordinary.headers}

    for _ in range(config.AUTH_RATE_LIMIT_PER_ACCOUNT):
        _login(client)
    throttled = _login(client)
    assert throttled.status_code == 429
    assert throttled.headers[AUTH_THROTTLE_HEADER] == "1"
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

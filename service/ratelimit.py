"""
Rate limiting for the unauthenticated auth endpoints (x5bz.2).

The hashing semaphore bounds *memory*, but it does not bound *attempts*: without
this, `/auth/login` accepts unlimited guesses (brute force) and unlimited
concurrent callers, each of which can sit holding a request slot while it waits
for a hashing slot — modest sustained traffic starves the instance long before
any password is cracked. OWASP's Authentication Cheat Sheet calls for limiting
failed attempts; this is that limit, applied *before* any argon2 work happens.

Two keys are checked per attempt, and both must pass:

- **account** (the submitted email) — stops one account being ground down, no
  matter how many sources the attempts come from.
- **source** (client IP) — stops one source spraying many accounts, and is what
  actually protects the request slots.

Deliberate limitations, stated rather than implied:

- **Per-instance, in memory.** Cloud Run may run up to `--max-instances`, so the
  effective ceiling is that multiple. For a closed pilot with 2 instances that is
  fine; a shared store (Redis/DB) is the answer if this ever needs to be exact.
- **Not a lockout.** Counters decay with the window, so a throttled legitimate
  user recovers on their own instead of needing an admin.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

import config


class RateLimited(Exception):
    """Too many attempts for this key. `retry_after` is whole seconds."""

    def __init__(self, retry_after: int):
        super().__init__(f"rate limited; retry in {retry_after}s")
        self.retry_after = retry_after


class SlidingWindowLimiter:
    """Fixed attempt budget over a sliding time window, per key.

    The key table is itself capped (`max_keys`, LRU-evicted): an attacker
    supplying a fresh email or spoofed source per request must not be able to
    grow the limiter into the memory problem it exists to prevent.
    """

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 10_000):
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str, *, now: float | None = None) -> None:
        """Record an attempt for `key`, or raise RateLimited without recording."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            self._hits.move_to_end(key)

            cutoff = now - self._window
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self._limit:
                # Free again once the oldest hit leaves the window.
                retry_after = max(1, int(hits[0] + self._window - now) + 1)
                raise RateLimited(retry_after)

            hits.append(now)
            self._evict_locked()

    def _evict_locked(self) -> None:
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)  # oldest-touched key

    def reset(self) -> None:
        """Drop all state (tests; also the natural 'forget everything' hook)."""
        with self._lock:
            self._hits.clear()


# One limiter per dimension. Sizing: the account limit is the brute-force
# ceiling; the source limit is deliberately looser (a household or office can
# share an egress IP) but still far below what it takes to starve request slots.
account_limiter = SlidingWindowLimiter(
    limit=config.AUTH_RATE_LIMIT_PER_ACCOUNT,
    window_seconds=config.AUTH_RATE_LIMIT_WINDOW_S,
)
source_limiter = SlidingWindowLimiter(
    limit=config.AUTH_RATE_LIMIT_PER_SOURCE,
    window_seconds=config.AUTH_RATE_LIMIT_WINDOW_S,
)


def reset_all() -> None:
    account_limiter.reset()
    source_limiter.reset()


def client_source(request) -> str:
    """Best-effort caller identity for throttling.

    Cloud Run terminates the connection, so `request.client.host` is the proxy;
    the real caller is the FIRST entry of X-Forwarded-For. It is spoofable, which
    is why the account limiter exists alongside it — an attacker can rotate this
    value but cannot rotate the account they are attacking.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def check_auth_attempt(request, account: str) -> None:
    """Throttle one auth attempt, or raise RateLimited. Call BEFORE hashing."""
    source_limiter.check(client_source(request))
    account_limiter.check(account.strip().lower())

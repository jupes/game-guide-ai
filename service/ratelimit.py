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
- **source** (client IP, read only from a *trusted* proxy hop — see
  `client_source`) — stops one source spraying many accounts, and is what
  actually protects the request slots.

Deliberate limitations, stated rather than implied:

- **Per-instance, in memory.** Cloud Run may run up to `--max-instances`, so the
  effective ceiling is that multiple. For a closed pilot with 2 instances that is
  fine; a shared store (Redis/DB) is the answer if this ever needs to be exact.
- **Not a lockout.** Counters decay with the window, so a throttled legitimate
  user recovers on their own instead of needing an admin.
"""

from __future__ import annotations

import ipaddress
import math
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
        # These are env-derived, so a typo must fail loudly at startup rather
        # than silently disabling the control: a window <= 0 expires every hit
        # immediately (no throttling at all) and a limit < 1 can never be
        # satisfied. Refusing to construct is the fail-closed choice — the
        # container will not start with a rate limiter that does not limit.
        if limit < 1:
            raise ValueError(f"rate-limit `limit` must be >= 1, got {limit!r}")
        # `isfinite` first: nan fails EVERY comparison, so `<= 0` waves it
        # through, and inf passes too. Both survive to the retry_after
        # arithmetic, where int() of a nan/inf raises — turning /auth/login into
        # a 500 on the second attempt. A comparison alone is not a range check.
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError(
                f"rate-limit `window_seconds` must be a finite number > 0, "
                f"got {window_seconds!r}"
            )
        if max_keys < 1:
            raise ValueError(f"rate-limit `max_keys` must be >= 1, got {max_keys!r}")
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


def _build(limit: int, window_seconds: float, env_name: str, window_env: str) -> SlidingWindowLimiter:
    """Construct a limiter, naming the offending env vars if the values are bad.

    The window is a parameter rather than read from `config` here: this builds
    limiters for two different controls (auth attempts, chat requests) whose
    windows differ by an order of magnitude, and a hardcoded one would silently
    give a caller the other control's window.
    """
    try:
        return SlidingWindowLimiter(limit=limit, window_seconds=window_seconds)
    except ValueError as exc:
        raise ValueError(
            f"invalid rate-limit configuration ({env_name} / {window_env}): {exc}"
        ) from exc


# One limiter per dimension. Sizing: the account limit is the brute-force
# ceiling; the source limit is deliberately looser (a household or office can
# share an egress IP) but still far below what it takes to starve request slots.
account_limiter = _build(
    config.AUTH_RATE_LIMIT_PER_ACCOUNT, config.AUTH_RATE_LIMIT_WINDOW_S,
    "AUTH_RATE_LIMIT_PER_ACCOUNT", "AUTH_RATE_LIMIT_WINDOW_S",
)
source_limiter = _build(
    config.AUTH_RATE_LIMIT_PER_SOURCE, config.AUTH_RATE_LIMIT_WINDOW_S,
    "AUTH_RATE_LIMIT_PER_SOURCE", "AUTH_RATE_LIMIT_WINDOW_S",
)

# The chat budget (x5bz.3) is a cost control, not an abuse control: the caller is
# already authenticated and invited, so one limiter keyed on identity is the
# whole story. Same class, a much longer window.
chat_user_limiter = _build(
    config.CHAT_RATE_LIMIT_PER_USER, config.CHAT_RATE_LIMIT_WINDOW_S,
    "CHAT_RATE_LIMIT_PER_USER", "CHAT_RATE_LIMIT_WINDOW_S",
)

if config.AUTH_TRUSTED_PROXY_HOPS < 0:
    raise ValueError(
        f"AUTH_TRUSTED_PROXY_HOPS must be >= 0, got {config.AUTH_TRUSTED_PROXY_HOPS!r}"
    )


def reset_all() -> None:
    account_limiter.reset()
    source_limiter.reset()
    chat_user_limiter.reset()


# An IPv6 address in text form is at most 45 characters; anything longer is not
# an address, and accepting it would let a caller inflate the key table.
_MAX_SOURCE_LEN = 64


def _parse_ip(raw: str) -> str | None:
    """Normalize one X-Forwarded-For / peer entry to a canonical IP, or None.

    Normalizing (rather than using the raw text) matters: `::1`, `0:0:0:0:0:0:0:1`
    and `[::1]:443` are the same caller, and treating them as three keys would
    hand an attacker three budgets.
    """
    candidate = raw.strip()
    if not candidate or len(candidate) > _MAX_SOURCE_LEN:
        return None
    if candidate.startswith("["):  # [2001:db8::1]:443
        candidate = candidate[1:].partition("]")[0]
    elif candidate.count(":") == 1:  # 203.0.113.7:443 (a bare IPv6 has >1 colon)
        candidate = candidate.rpartition(":")[0]
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return None


def client_source(request) -> str:
    """Caller identity for throttling, taken only from a **trusted** hop.

    `X-Forwarded-For` is caller-writable: Google's front end *preserves* whatever
    the client sent and *appends* its own observation, and the docs are explicit
    that the client-supplied part is unverified. Reading the leftmost entry —
    the intuitive "original client" — therefore reads attacker-controlled text,
    and an attacker who rotates it gets a fresh budget every request, which is
    exactly the throttle this module exists to apply.

    So the header is parsed from the RIGHT instead, and only when we know how
    many entries our own infrastructure appended:

    - `AUTH_TRUSTED_PROXY_HOPS = 0` (default) — trust nothing in the header; key
      on the peer address. Correct for direct/local serving.
    - `= 1` — one trusted hop appended the caller's address (Cloud Run's default
      run.app front end). Set by `scripts/deploy.sh`.
    - `= 2` — an external HTTPS load balancer in front of Cloud Run.

    Entries left of the trusted hops are ignored no matter what they contain, and
    the chosen value must parse as an IP address or we fall back to the peer.
    Ingress must also be restricted to that infrastructure (see
    `docs/deploy-gcp.md`), or a caller could reach the service directly and
    become the "trusted" hop themselves.
    """
    hops = config.AUTH_TRUSTED_PROXY_HOPS
    if hops > 0:
        chain = [p for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        # Fewer entries than trusted hops ⇒ our proxies did not write this
        # header, so none of it is trustworthy.
        if len(chain) >= hops:
            source = _parse_ip(chain[-hops])
            if source is not None:
                return source
    client = getattr(request, "client", None)
    return _parse_ip(getattr(client, "host", "") or "") or "unknown"


def check_auth_attempt(request, account: str) -> None:
    """Throttle one auth attempt, or raise RateLimited. Call BEFORE hashing."""
    source_limiter.check(client_source(request))
    account_limiter.check(account.strip().lower())


def check_chat_request(user_id: int) -> None:
    """Spend one of this tester's chat budget, or raise RateLimited.

    Only the authenticated user id is keyed — unlike the auth endpoints, there is
    no second source dimension. An authenticated caller cannot rotate their
    identity the way an anonymous one rotates addresses, so the IP adds no
    protection here and would only split one person's budget across networks.
    """
    chat_user_limiter.check(str(user_id))

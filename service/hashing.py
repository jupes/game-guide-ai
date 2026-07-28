"""
Password hashing (x5bz.2 Checkpoint A).

argon2id via argon2-cffi. Two functions the auth store and endpoints call:
`hash_password` at signup, `verify_password` at login. The plaintext is never
logged and never stored — only the returned hash goes to `auth.users`.

**Memory is the operational risk here, not CPU.** argon2 is deliberately
memory-hard: each hash/verify holds `ARGON2_MEMORY_KIB` for its duration, and
`/auth/login` runs one on EVERY attempt (including unknown emails — see
DUMMY_PASSWORD_HASH). Unbounded concurrency therefore turns a public endpoint
into an OOM lever: Cloud Run kills an instance that exceeds its memory limit,
so enough simultaneous logins would take the service down without any
credential ever being guessed.

Two things bound that, and they must be kept consistent with each other:

1. `_HASH_SLOTS` — a process-wide semaphore capping how many hashes run at once,
   so peak hashing memory is `ARGON2_MEMORY_KIB * MAX_CONCURRENT_HASHES`
   regardless of how many requests arrive.
2. Cloud Run `--memory` / `--concurrency`, set explicitly in `scripts/deploy.sh`
   (the platform defaults — 512 MiB, 80 concurrent — are not survivable here).

Requests beyond the cap wait rather than fail; the wait is bounded by
`HASH_ACQUIRE_TIMEOUT_S`, after which the caller gets a 503 instead of piling up.
"""

from __future__ import annotations

import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

import config


class HashingCapacityError(Exception):
    """No hashing slot became available in time — shed load rather than queue."""


# Explicit parameters, not library defaults: the cost of a login has to be a
# deliberate, reviewable number because it is multiplied by concurrency above.
# 64 MiB / t=3 / p=4 matches argon2-cffi's defaults and RFC 9106's guidance for
# an interactive login; changing them does NOT invalidate existing hashes
# (parameters are embedded in each hash string, so old ones still verify).
ARGON2_TIME_COST = config.ARGON2_TIME_COST
ARGON2_MEMORY_KIB = config.ARGON2_MEMORY_KIB
ARGON2_PARALLELISM = config.ARGON2_PARALLELISM

MAX_CONCURRENT_HASHES = config.MAX_CONCURRENT_HASHES
HASH_ACQUIRE_TIMEOUT_S = config.HASH_ACQUIRE_TIMEOUT_S

_hasher = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_KIB,
    parallelism=ARGON2_PARALLELISM,
)

# Endpoints run in a threadpool (sync defs), so a threading semaphore is the
# right primitive. BoundedSemaphore turns a release/acquire mismatch into a
# loud error instead of silently growing the cap.
_HASH_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_HASHES)


class _slot:
    """Hold a hashing slot, or raise HashingCapacityError."""

    def __enter__(self) -> None:
        if not _HASH_SLOTS.acquire(timeout=HASH_ACQUIRE_TIMEOUT_S):
            raise HashingCapacityError(
                f"no argon2 slot within {HASH_ACQUIRE_TIMEOUT_S}s "
                f"(cap {MAX_CONCURRENT_HASHES})"
            )

    def __exit__(self, *exc_info: object) -> None:
        _HASH_SLOTS.release()


def hash_password(password: str) -> str:
    """Return an argon2id hash string (embeds algorithm, params, and salt)."""
    with _slot():
        return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True iff `password` matches `password_hash`. Never raises on a bad
    password — a mismatch (or a malformed stored hash) is a plain False.
    Capacity exhaustion DOES raise (HashingCapacityError): that is a 503, not a
    failed login, and must not be reported as "wrong password"."""
    with _slot():
        try:
            return _hasher.verify(password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False


# A real argon2 hash of a value nobody can supply, used to equalize login timing
# for unknown emails (verifying against it costs the same as a real check but can
# never succeed). Computed once at import, not per request.
DUMMY_PASSWORD_HASH: str = _hasher.hash("gga-nonexistent-account-sentinel")

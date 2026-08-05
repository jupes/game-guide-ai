"""Behavior #1 — argon2 password hashing (x5bz.2 Checkpoint A)."""

from __future__ import annotations

import pytest

from service.hashing import hash_password, verify_password


def test_hash_is_not_the_plaintext_and_is_argon2() -> None:
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert h.startswith("$argon2")  # argon2id encoded hash


def test_correct_password_verifies() -> None:
    h = hash_password("s3kr3t-pw")
    assert verify_password(h, "s3kr3t-pw") is True


def test_wrong_password_fails_without_raising() -> None:
    h = hash_password("s3kr3t-pw")
    assert verify_password(h, "not-the-password") is False


def test_malformed_hash_is_false_not_error() -> None:
    assert verify_password("not-a-real-hash", "anything") is False


def test_same_password_hashes_differ_by_salt() -> None:
    assert hash_password("dup") != hash_password("dup")


# ── Capacity: argon2 is memory-hard, so concurrency must be bounded ──────────


def test_concurrent_hashing_never_exceeds_the_cap(monkeypatch) -> None:
    """/auth/login hashes on EVERY attempt, so unbounded concurrency would let a
    public endpoint exhaust the instance's memory (Cloud Run kills it). Peak
    memory is ARGON2_MEMORY_KIB * MAX_CONCURRENT_HASHES, which only holds if the
    semaphore actually caps in-flight work.

    The real hasher is swapped for a slow fake: this exercises the semaphore
    wrapper (the thing under test) without paying 64 MiB per thread.
    """
    import threading
    import time

    from service import hashing

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    class _CountingHasher:
        def hash(self, password: str) -> str:
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                time.sleep(0.02)  # long enough for contention to be real
                return f"fake-hash:{password}"
            finally:
                with lock:
                    in_flight -= 1

    monkeypatch.setattr(hashing, "_hasher", _CountingHasher())

    threads = [
        threading.Thread(target=lambda: hashing.hash_password("concurrent-pw"))
        for _ in range(hashing.MAX_CONCURRENT_HASHES + 6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert peak > 0, "the fake hasher was never exercised"
    assert peak <= hashing.MAX_CONCURRENT_HASHES, (
        f"{peak} concurrent hashes exceeded the cap of {hashing.MAX_CONCURRENT_HASHES}"
    )


def test_capacity_exhaustion_raises_rather_than_queueing_forever(monkeypatch) -> None:
    """When no slot frees up, the request is shed (503 upstream) instead of
    piling up until the whole request times out."""
    import threading

    from service import hashing

    monkeypatch.setattr(hashing, "HASH_ACQUIRE_TIMEOUT_S", 0.05)
    blocked = threading.Semaphore(0)
    # Occupy every slot.
    for _ in range(hashing.MAX_CONCURRENT_HASHES):
        assert hashing._HASH_SLOTS.acquire(timeout=1)  # noqa: SLF001
    try:
        with pytest.raises(hashing.HashingCapacityError):
            hash_password("no-slot-available")
    finally:
        for _ in range(hashing.MAX_CONCURRENT_HASHES):
            hashing._HASH_SLOTS.release()  # noqa: SLF001
        del blocked

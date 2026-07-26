"""Behavior #1 — argon2 password hashing (x5bz.2 Checkpoint A)."""

from __future__ import annotations

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

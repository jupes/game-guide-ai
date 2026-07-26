"""
Password hashing (x5bz.2 Checkpoint A).

argon2id via argon2-cffi with library defaults (a sensible memory/time cost for
an interactive login). Two pure functions the auth store and endpoints call:
`hash_password` at signup, `verify_password` at login. The plaintext is never
logged and never stored — only the returned hash goes to `auth.users`.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an argon2id hash string (embeds algorithm, params, and salt)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True iff `password` matches `password_hash`. Never raises on a bad
    password — a mismatch (or a malformed stored hash) is a plain False."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False

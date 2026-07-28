"""Behavior #2 — signed session cookie encode/decode (x5bz.2 Checkpoint B)."""

from __future__ import annotations

from service.session import SessionData, decode_session, encode_session

SECRET = "test-secret-please-rotate-at-least-32-chars"
TTL = 14 * 24 * 3600


def test_round_trip_preserves_user_and_role() -> None:
    token = encode_session(SessionData(user_id=42, role="dm"), SECRET)
    out = decode_session(token, SECRET, max_age_seconds=TTL)
    assert out == SessionData(user_id=42, role="dm")


def test_tampered_token_is_rejected() -> None:
    token = encode_session(SessionData(user_id=1, role="player"), SECRET)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert decode_session(tampered, SECRET, max_age_seconds=TTL) is None


def test_wrong_secret_is_rejected() -> None:
    token = encode_session(SessionData(user_id=1, role="player"), SECRET)
    assert decode_session(token, "a-different-secret", max_age_seconds=TTL) is None


def test_expired_token_is_rejected() -> None:
    token = encode_session(SessionData(user_id=1, role="player"), SECRET)
    # A negative max_age expires even a freshly-signed token, deterministically.
    assert decode_session(token, SECRET, max_age_seconds=-1) is None


def test_garbage_token_is_none_not_error() -> None:
    assert decode_session("not-a-token", SECRET, max_age_seconds=TTL) is None

"""Behavior #2 — signed session cookie encode/decode (x5bz.2 Checkpoint B)."""

from __future__ import annotations

import base64

from service.session import SessionData, decode_session, encode_session

SECRET = "test-secret-please-rotate-at-least-32-chars"
TTL = 14 * 24 * 3600


def test_round_trip_preserves_user_and_role() -> None:
    token = encode_session(SessionData(user_id=42, role="dm"), SECRET)
    out = decode_session(token, SECRET, max_age_seconds=TTL)
    assert out == SessionData(user_id=42, role="dm")


def test_tampered_payload_is_rejected() -> None:
    """Flip a character in the PAYLOAD segment — deterministically changes the
    signed content, so the signature can no longer match."""
    token = encode_session(SessionData(user_id=1, role="player"), SECRET)
    payload, _, rest = token.partition(".")
    flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
    assert decode_session(f"{flipped}.{rest}", SECRET, max_age_seconds=TTL) is None


def test_tampered_signature_is_rejected() -> None:
    """Flip a byte of the DECODED signature and re-encode.

    Mutating the last base64 character is not a reliable tamper: the final
    character can carry unused padding bits, so a different string can decode to
    the same signature bytes and still verify. Round-tripping through the raw
    bytes changes the signature itself, every time.
    """
    token = encode_session(SessionData(user_id=1, role="player"), SECRET)
    head, _, signature = token.rpartition(".")
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    flipped = bytes([raw[0] ^ 0xFF]) + raw[1:]
    tampered = base64.urlsafe_b64encode(flipped).rstrip(b"=").decode()
    assert tampered != signature
    assert decode_session(f"{head}.{tampered}", SECRET, max_age_seconds=TTL) is None


def test_wrong_secret_is_rejected() -> None:
    token = encode_session(SessionData(user_id=1, role="player"), SECRET)
    assert decode_session(token, "a-different-secret", max_age_seconds=TTL) is None


def test_expired_token_is_rejected() -> None:
    token = encode_session(SessionData(user_id=1, role="player"), SECRET)
    # A negative max_age expires even a freshly-signed token, deterministically.
    assert decode_session(token, SECRET, max_age_seconds=-1) is None


def test_garbage_token_is_none_not_error() -> None:
    assert decode_session("not-a-token", SECRET, max_age_seconds=TTL) is None

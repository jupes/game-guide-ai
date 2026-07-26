"""
Signed session cookie (x5bz.2 Checkpoint B).

A stateless session: the user's id + role are signed (not encrypted — nothing
secret is inside) with the server `SESSION_SECRET` via itsdangerous and carried
in an httpOnly cookie. No session table; logout clears the cookie, and rotating
the secret invalidates every session at once. Encoding/decoding are pure so they
test without HTTP; the endpoints that set/read the cookie are Checkpoint C.
"""

from __future__ import annotations

from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer

from .invites import Role

# Namespacing salt — lets one secret sign different token kinds unambiguously and
# lets us rev the token format later without secret rotation.
_SESSION_SALT = "gga-session-v1"


@dataclass
class SessionData:
    user_id: int
    role: Role


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=_SESSION_SALT)


def encode_session(data: SessionData, secret: str) -> str:
    """Sign (user_id, role) into a url-safe, timestamped token."""
    return _serializer(secret).dumps({"uid": data.user_id, "role": data.role})


def decode_session(token: str, secret: str, max_age_seconds: int) -> SessionData | None:
    """Return the SessionData iff the token is authentic and within
    `max_age_seconds`. Any failure — tampered, expired, wrong secret, malformed
    payload — is a plain None (the caller treats that as "no session" → 401).
    `SignatureExpired` is a subclass of `BadSignature`, so one except covers both."""
    try:
        payload = _serializer(secret).loads(token, max_age=max_age_seconds)
    except BadSignature:
        return None
    try:
        return SessionData(user_id=int(payload["uid"]), role=payload["role"])
    except (KeyError, TypeError, ValueError):
        return None

"""
Invite domain (x5bz.2 Checkpoint A).

An invite is the trust anchor for signup: a single-use, expiring token that
carries the role the new account gets. Token generation and the redeemability
rules live here (pure, no I/O); atomic consumption + persistence live in
`auth_store.py`. The signup/login HTTP flow is Checkpoint C.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

# Roles are server-authoritative (the GM channel is enforced from this, not the
# UI toggle). Kept as a Literal so a bad role is a typecheck error at call sites.
Role = Literal["player", "dm"]
ROLES: tuple[Role, ...] = ("player", "dm")

# 32 bytes -> a 43-char url-safe token (~256 bits). Ample entropy for an
# access-granting anchor; url-safe so it drops cleanly into `/#invite=<token>`
# (the FRAGMENT — never sent to the server, so it stays out of request logs).
INVITE_TOKEN_BYTES = 32


def new_invite_token() -> str:
    return secrets.token_urlsafe(INVITE_TOKEN_BYTES)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


# ── Errors (each maps to a distinct, user-facing signup failure) ──────────────

class InviteError(Exception):
    """Base for a non-redeemable invite. Subtypes let the API pick the message."""


class InviteNotFound(InviteError):
    pass


class InviteAlreadyUsed(InviteError):
    pass


class InviteExpired(InviteError):
    pass


class InviteRevoked(InviteError):
    pass


@dataclass
class Invite:
    token: str
    role: Role
    expires_at: datetime
    used_at: datetime | None = None
    used_by: int | None = None
    revoked_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def check_redeemable(self, now: datetime | None = None) -> None:
        """Raise the specific InviteError if this invite cannot be redeemed.

        Order matters: revoked and used are terminal states worth reporting
        distinctly before the (also-terminal) expiry check.
        """
        if self.is_revoked:
            raise InviteRevoked("This invite has been revoked.")
        if self.is_used:
            raise InviteAlreadyUsed("This invite link has already been used.")
        if self.expires_at <= _now(now):
            raise InviteExpired("This invite link has expired.")

"""Behavior #3 — invite lifecycle over the in-memory store (x5bz.2 Checkpoint A).

Single-use, expiry, revocation, role-carrying, and case-folded email uniqueness —
all the logical rules, no DB. The concurrency guarantee is test_invite_atomic.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from service.auth_store import EmailTaken, InMemoryAuthStore
from service.invites import (
    InviteAlreadyUsed,
    InviteExpired,
    InviteNotFound,
    InviteRevoked,
    Role,
    new_invite_token,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=7)


def _store_with_invite(role: Role = "player", expires_at: datetime = LATER):
    store = InMemoryAuthStore()
    invite = store.create_invite(role=role, expires_at=expires_at)
    return store, invite


def test_new_token_is_long_and_urlsafe() -> None:
    token = new_invite_token()
    assert len(token) >= 40
    assert all(c.isalnum() or c in "-_" for c in token)


def test_redeem_creates_user_with_the_invite_role() -> None:
    store, invite = _store_with_invite(role="dm")
    user = store.redeem_invite(invite.token, "Ada@example.com", "hash", now=NOW)
    assert user.role == "dm"
    assert store.get_user_by_email("ada@example.com") is not None  # case-folded lookup


def test_second_redemption_of_same_invite_fails() -> None:
    store, invite = _store_with_invite()
    store.redeem_invite(invite.token, "first@example.com", "hash", now=NOW)
    with pytest.raises(InviteAlreadyUsed):
        store.redeem_invite(invite.token, "second@example.com", "hash", now=NOW)


def test_expired_invite_cannot_be_redeemed() -> None:
    store, invite = _store_with_invite(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(InviteExpired):
        store.redeem_invite(invite.token, "late@example.com", "hash", now=NOW)


def test_revoked_invite_cannot_be_redeemed() -> None:
    store, invite = _store_with_invite()
    assert store.revoke_invite(invite.token) is True
    with pytest.raises(InviteRevoked):
        store.redeem_invite(invite.token, "revoked@example.com", "hash", now=NOW)


def test_revoke_is_rejected_after_use() -> None:
    store, invite = _store_with_invite()
    store.redeem_invite(invite.token, "used@example.com", "hash", now=NOW)
    assert store.revoke_invite(invite.token) is False


def test_unknown_token_raises_not_found() -> None:
    store = InMemoryAuthStore()
    with pytest.raises(InviteNotFound):
        store.redeem_invite("nope", "x@example.com", "hash", now=NOW)


def test_duplicate_email_is_rejected_case_folded() -> None:
    store = InMemoryAuthStore()
    i1 = store.create_invite(role="player", expires_at=LATER)
    i2 = store.create_invite(role="player", expires_at=LATER)
    store.redeem_invite(i1.token, "dup@example.com", "hash", now=NOW)
    with pytest.raises(EmailTaken):
        store.redeem_invite(i2.token, "DUP@example.com", "hash", now=NOW)


def test_list_invites_returns_created() -> None:
    store, invite = _store_with_invite()
    tokens = [i.token for i in store.list_invites()]
    assert tokens == [invite.token]

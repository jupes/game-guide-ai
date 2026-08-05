"""Admin invite CLI command functions (x5bz.2 Checkpoint B).

The `cmd_*`/format helpers are tested against the in-memory store — no DB. `main()`
(argparse + PostgresAuthStore wiring) is exercised end-to-end in the gated
integration path, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from service.admin_invites import (
    build_signup_link,
    cmd_create,
    cmd_list,
    cmd_revoke,
    format_invites,
)
from service.auth_store import InMemoryAuthStore


def test_build_signup_link_puts_the_token_in_the_fragment() -> None:
    link = build_signup_link("https://svc.run.app/", "abc123")
    assert link == "https://svc.run.app/#invite=abc123"  # trailing slash normalized
    # A query string would be captured by Cloud Run's request logs
    # (httpRequest.requestUrl keeps the query), exposing a live invite.
    assert "?invite=" not in link


def test_create_persists_invite_with_role_and_returns_link() -> None:
    store = InMemoryAuthStore()
    link = cmd_create(store, role="dm", ttl_days=14, base_url="https://svc.run.app")
    invites = store.list_invites()
    assert len(invites) == 1
    assert invites[0].role == "dm"
    assert invites[0].token in link
    assert link.startswith("https://svc.run.app/#invite=")


def test_list_shows_status_open_used_revoked() -> None:
    store = InMemoryAuthStore()
    now = datetime.now(UTC)
    later = now + timedelta(days=7)
    open_i = store.create_invite(role="player", expires_at=later)
    used_i = store.create_invite(role="player", expires_at=later)
    revoked_i = store.create_invite(role="player", expires_at=later)
    store.redeem_invite(used_i.token, "u@example.com", "hash")
    store.revoke_invite(revoked_i.token)

    out = cmd_list(store)
    assert "open" in out and "used" in out and "revoked" in out
    # FULL tokens: `revoke` needs a whole one, so a truncated listing would be
    # unusable for the operator reading it.
    for inv in (open_i, used_i, revoked_i):
        assert inv.token in out


def test_format_invites_empty() -> None:
    assert format_invites([]) == "(no invites)"


def test_create_rejects_non_positive_ttl() -> None:
    """A zero/negative TTL would mint an already-expired invite while printing a
    normal-looking link — the operator only finds out when signup fails."""
    import pytest

    store = InMemoryAuthStore()
    for bad in (0, -1):
        with pytest.raises(ValueError, match="ttl-days"):
            cmd_create(store, role="player", ttl_days=bad, base_url="https://svc")
    assert store.list_invites() == [], "no invite should be created for a bad TTL"


def test_revoke_returns_true_then_false() -> None:
    store = InMemoryAuthStore()
    inv = store.create_invite(role="player", expires_at=datetime.now(UTC) + timedelta(days=1))
    assert cmd_revoke(store, inv.token) is True
    assert cmd_revoke(store, inv.token) is False  # already revoked

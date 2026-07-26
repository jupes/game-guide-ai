"""Behavior #8 — atomic single-use under concurrency (x5bz.2 Checkpoint A).

Gated integration test: needs a reachable Postgres (DATABASE_URL). It proves the
guarantee the in-memory test can't — two concurrent redemptions of ONE invite,
exactly one wins — which relies on real row-lock semantics. Skips when no DB.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from service.auth_store import PostgresAuthStore, User
from service.invites import InviteError


def _db_available() -> bool:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return False
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="no reachable Postgres (set DATABASE_URL to run the atomic-invite integration test)",
)


def test_concurrent_double_redeem_exactly_one_wins() -> None:
    store = PostgresAuthStore()
    store.ensure_schema()
    invite = store.create_invite(role="player", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    marker = uuid.uuid4().hex
    emails = [f"race-{marker}-a@example.com", f"race-{marker}-b@example.com"]

    try:
        def redeem(email: str):
            try:
                return ("ok", store.redeem_invite(invite.token, email, "hash"))
            except InviteError as exc:
                return ("err", type(exc).__name__)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(redeem, emails))

        outcomes = sorted(kind for kind, _ in results)
        assert outcomes == ["err", "ok"], f"expected exactly one winner, got {results}"

        winner = next(payload for kind, payload in results if kind == "ok")
        assert isinstance(winner, User)
        # DB agrees: the invite is consumed exactly once, by the winner.
        stored = store.get_invite(invite.token)
        assert stored is not None and stored.is_used
        assert stored.used_by == winner.id
    finally:
        _cleanup(store, invite.token, emails)


def _cleanup(store: PostgresAuthStore, token: str, emails: list[str]) -> None:
    import psycopg

    with psycopg.connect(store._dsn) as conn:  # noqa: SLF001 - test teardown
        conn.execute("DELETE FROM auth.invites WHERE token = %s", (token,))
        conn.execute("DELETE FROM auth.users WHERE email = ANY(%s)", (emails,))

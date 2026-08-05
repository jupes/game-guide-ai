"""Atomic single-use invite redemption, against real PostgreSQL.

The guarantee: two concurrent redemptions of one invite produce exactly one
account. It rests on row-lock semantics no fake reproduces — a check-then-update
would let both callers observe an unused invite and both succeed — so it can
only be tested here. The redeemability rules themselves (expired, revoked,
already used, unknown token) are covered against the fake in test_invites.py.

Skipping rules: no DATABASE_URL means nobody asked for a database, so skip. A
DATABASE_URL that does not work is a broken environment, so FAIL — silently
skipping would leave the guarantee unverified exactly when something is wrong.
CI always sets it; tests/test_ci_workflow.py guards that wiring.

Diagnostics deliberately name no DSN and quote no driver message: this runs in
CI, whose logs are public, and a connection string is a credential.

Run:
    DATABASE_URL=postgresql://... uv run python -m pytest service/tests/test_invite_atomic.py
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from service.auth_store import PostgresAuthStore, User
from service.invites import InviteError

DSN = os.environ.get("DATABASE_URL") or None

needs_db = pytest.mark.skipif(
    DSN is None,
    reason="no DATABASE_URL — set one to run the atomic-invite integration test "
           "(CI always sets it; see .github/workflows/ci.yml)",
)

#: Bounded, so an unreachable host reports "the database is down" rather than
#: blocking until the CI job's own limit kills it and reports "the build hung".
CONNECT_TIMEOUT_SECONDS = 10

#: Waiting for the contender to show as lock-blocked, then to unblock. Generous:
#: a slow runner must not turn a correct implementation into a flaky failure.
LOCK_WAIT_TIMEOUT_SECONDS = 15


def _fail(message: str) -> None:
    """Fail with this message and NOTHING else.

    `pytrace=False` is the security-relevant part, and it is why the reporting
    below hand-rolls its own message instead of letting the exception surface:
    pytest renders frame locals, and psycopg's connect frames hold the full
    conninfo — password included — so an ordinary traceback publishes the
    database credentials into a public CI log.
    """
    pytest.fail(message, pytrace=False)


@pytest.fixture(scope="module")
def store() -> PostgresAuthStore:
    assert DSN is not None  # guaranteed by needs_db
    import psycopg

    failure: str | None = None
    try:
        with psycopg.connect(DSN, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        failure = (
            f"DATABASE_URL is configured but unreachable after "
            f"{CONNECT_TIMEOUT_SECONDS}s ({type(exc).__name__}); the atomic-invite "
            f"guarantee went unverified"
        )
    if failure:  # outside the except block, so nothing is chained onto it
        _fail(failure)

    s = PostgresAuthStore(DSN)
    try:
        s.ensure_schema()
    except Exception as exc:
        failure = f"connected, but the auth schema would not apply ({type(exc).__name__})"
    if failure:
        _fail(failure)
    return s


def _lock_waiters(conn) -> int:
    """Backends currently BLOCKED on a lock. Asking PostgreSQL is the difference
    between "the second redemption is waiting" and "it has not started yet"."""
    return conn.execute(
        "SELECT count(*) FROM pg_stat_activity "
        " WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "   AND state = 'active' AND wait_event_type = 'Lock'"
    ).fetchone()[0]


def _cleanup(store: PostgresAuthStore, token: str, emails: list[str]) -> None:
    import psycopg

    with psycopg.connect(store._dsn) as conn:  # noqa: SLF001 - test teardown
        conn.execute("DELETE FROM auth.invites WHERE token = %s", (token,))
        conn.execute("DELETE FROM auth.users WHERE email = ANY(%s)", (emails,))


@needs_db
def test_a_second_redemption_blocks_on_the_row_lock_and_then_loses(
    store: PostgresAuthStore,
) -> None:
    """Overlap is forced, not hoped for.

    Handing two calls to a thread pool does not guarantee the transactions ever
    coexist — the first can commit before the second starts, and sequential
    redemption of a one-shot invite fails the second time even in a
    check-then-update implementation. So: hold the first transaction open, start
    the second, and require PostgreSQL to report it blocked on a lock before
    letting the first commit.
    """
    import psycopg

    assert DSN is not None
    invite = store.create_invite(role="player", expires_at=datetime.now(UTC) + timedelta(days=1))
    marker = uuid.uuid4().hex
    winner_email = f"race-{marker}-a@example.com"
    loser_email = f"race-{marker}-b@example.com"
    outcome: dict[str, object] = {}

    def contend() -> None:
        try:
            outcome["ok"] = store.redeem_invite(invite.token, loser_email, "hash")
        except InviteError as exc:
            outcome["rejected"] = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            outcome["unexpected"] = f"{type(exc).__name__}: {exc}"

    holder = psycopg.connect(DSN)
    observer = psycopg.connect(DSN, autocommit=True)
    try:
        # Production code path: take the row lock, and do NOT commit.
        assert store._consume_invite_locked(holder, invite.token) == "player"  # noqa: SLF001

        contender = threading.Thread(target=contend, daemon=True)
        contender.start()

        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline and _lock_waiters(observer) == 0:
            time.sleep(0.05)

        assert _lock_waiters(observer) >= 1, (
            "the second redemption never blocked while the first held the invite "
            "row — the two never overlapped, so single use here is not evidence "
            "of atomicity"
        )
        assert not outcome, f"the second redemption completed mid-transaction: {outcome}"

        holder.commit()
        contender.join(timeout=LOCK_WAIT_TIMEOUT_SECONDS)
        assert not contender.is_alive(), "the second redemption never unblocked"

        assert "unexpected" not in outcome, outcome
        assert "ok" not in outcome, "BOTH redemptions succeeded — the invite was used twice"
        assert "rejected" in outcome, f"expected an InviteError, got {outcome}"

        stored = store.get_invite(invite.token)
        assert stored is not None and stored.is_used
    finally:
        holder.close()
        observer.close()
        _cleanup(store, invite.token, [winner_email, loser_email])


@needs_db
def test_redeeming_creates_the_account_and_marks_the_invite_spent(
    store: PostgresAuthStore,
) -> None:
    """The happy path through real SQL: the account exists with the invite's
    role, the invite records who spent it, and `get_credentials` returns the
    identity and hash together (one query, so column order is load-bearing)."""
    invite = store.create_invite(role="dm", expires_at=datetime.now(UTC) + timedelta(days=1))
    marker = uuid.uuid4().hex
    email = f"redeem-{marker}@example.com"

    try:
        created = store.redeem_invite(invite.token, email, "hash-" + marker)
        assert isinstance(created, User)
        assert created.role == "dm", "the invite's role must carry to the account"

        spent = store.get_invite(invite.token)
        assert spent is not None and spent.is_used
        assert spent.used_by == created.id

        creds = store.get_credentials(email.upper())  # lookup is case-folded
        assert creds is not None
        user, password_hash = creds
        assert (user.id, user.email, user.role) == (created.id, email, "dm")
        assert password_hash == "hash-" + marker
        assert store.get_credentials(f"absent-{marker}@example.com") is None
    finally:
        _cleanup(store, invite.token, [email])

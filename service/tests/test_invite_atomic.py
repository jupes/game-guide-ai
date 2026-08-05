"""Behavior #8 — atomic single-use under concurrency (x5bz.2 Checkpoint A).

Integration test against a real Postgres. It proves the guarantee the in-memory
test cannot — two concurrent redemptions of ONE invite, exactly one wins — which
rests on row-lock semantics no fake reproduces.

**Skipping rules (PR #43 review).** This used to be the only test of the
single-use guarantee AND it was skipped on every CI run, because CI provided no
`DATABASE_URL`. Worse, the availability probe collapsed two very different
situations into the same green skip: "nobody asked for a database" and "a
database was configured and it is broken". The second is a failure — silently
skipping it means the guarantee is unverified precisely when something is wrong.

So:

  * no `DATABASE_URL` at all → skip (the local unit-test loop stays dependency-free)
  * `DATABASE_URL` set but unreachable → **fail**, loudly, naming the DSN host

CI sets `DATABASE_URL` against a `postgres` service container, so the skip branch
does not apply there. `tests/test_ci_workflow.py` guards that wiring — without it
a workflow edit could quietly restore the always-skipped state this fixed.

Run locally:
    DATABASE_URL=postgresql://... uv run python -m pytest service/tests/test_invite_atomic.py
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from service.auth_store import PostgresAuthStore, User
from service.invites import InviteError

#: Read once at import: whether a database was *requested*, which is a different
#: question from whether it works.
DSN = os.environ.get("DATABASE_URL") or None

pytestmark = pytest.mark.skipif(
    DSN is None,
    reason="no DATABASE_URL — set one to run the atomic-invite integration test "
           "(CI always sets it; see .github/workflows/ci.yml)",
)


def _redacted(dsn: str) -> str:
    """The DSN's host/database, never its password — this goes into a failure
    message, and failure messages end up in public CI logs."""
    tail = dsn.rsplit("@", 1)[-1]
    return tail or "<unparseable DSN>"


#: Seconds to wait for the first connection. Bounded on purpose: an unreachable
#: host with no timeout leaves psycopg blocking until the CI job's own limit
#: kills it, which reports as "the build hung" rather than "the database is
#: down" — and a 15-minute wait for a wrong DSN is its own kind of broken.
CONNECT_TIMEOUT_SECONDS = 10


@pytest.fixture(scope="module")
def store() -> PostgresAuthStore:
    """A connected store, or a hard failure.

    Deliberately NOT a skip: reaching here means a DSN was configured, so an
    unreachable database is a broken environment, not an absent one.
    """
    assert DSN is not None  # guaranteed by the module-level skipif
    import psycopg

    try:
        with psycopg.connect(DSN, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.fail(
            f"DATABASE_URL is set ({_redacted(DSN)}) but the database is unreachable "
            f"after {CONNECT_TIMEOUT_SECONDS}s, so the atomic-invite guarantee went "
            f"unverified: {type(exc).__name__}: {exc}"
        )

    s = PostgresAuthStore(DSN)
    try:
        s.ensure_schema()
    except Exception as exc:
        pytest.fail(
            f"connected to {_redacted(DSN)} but the auth schema could not be applied: "
            f"{type(exc).__name__}: {exc}"
        )
    return s


def test_concurrent_double_redeem_exactly_one_wins(store: PostgresAuthStore) -> None:
    invite = store.create_invite(role="player", expires_at=datetime.now(UTC) + timedelta(days=1))
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


def test_credentials_round_trip_through_real_postgres(store: PostgresAuthStore) -> None:
    """`get_credentials` replaced two separate queries with one that returns the
    identity and the hash together (PR #43 review). The in-memory fake can agree
    with a wrong SQL column order forever — only a real database checks the query.
    """
    invite = store.create_invite(role="dm", expires_at=datetime.now(UTC) + timedelta(days=1))
    marker = uuid.uuid4().hex
    email = f"creds-{marker}@example.com"

    try:
        created = store.redeem_invite(invite.token, email, "hash-for-" + marker)
        creds = store.get_credentials(email.upper())  # lookup is case-folded
        assert creds is not None
        user, password_hash = creds
        assert (user.id, user.email, user.role) == (created.id, email, "dm")
        assert password_hash == "hash-for-" + marker
        assert store.get_credentials(f"absent-{marker}@example.com") is None
    finally:
        _cleanup(store, invite.token, [email])


def _cleanup(store: PostgresAuthStore, token: str, emails: list[str]) -> None:
    import psycopg

    with psycopg.connect(store._dsn) as conn:  # noqa: SLF001 - test teardown
        conn.execute("DELETE FROM auth.invites WHERE token = %s", (token,))
        conn.execute("DELETE FROM auth.users WHERE email = ANY(%s)", (emails,))

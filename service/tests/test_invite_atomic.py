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
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from service.auth_store import PostgresAuthStore, User
from service.invites import InviteError

#: Read once at import: whether a database was *requested*, which is a different
#: question from whether it works.
DSN = os.environ.get("DATABASE_URL") or None

#: Applied per-test rather than module-wide, so the redaction unit tests below
#: — which need no database and guard a credential leak — always run.
needs_db = pytest.mark.skipif(
    DSN is None,
    reason="no DATABASE_URL — set one to run the atomic-invite integration test "
           "(CI always sets it; see .github/workflows/ci.yml)",
)

#: Keys safe to echo into a failure message. An ALLOWLIST, so a key nobody
#: thought about cannot leak by default.
SAFE_CONNINFO_KEYS = ("host", "port", "dbname")

#: libpq's secret-bearing keys, struck out of any text before it is printed.
SECRET_CONNINFO_KEYS = ("password", "sslpassword", "passfile")


def _conninfo(dsn: str) -> dict[str, str]:
    """Parse either DSN form psycopg accepts — a `postgresql://` URI *or* libpq
    keyword conninfo (`host=db user=alice password=...`)."""
    from psycopg.conninfo import conninfo_to_dict

    try:
        return {k: str(v) for k, v in conninfo_to_dict(dsn).items() if v is not None}
    except Exception:
        return {}


def _describe(dsn: str) -> str:
    """Where the database is, assembled from SAFE_CONNINFO_KEYS.

    Built from a parse, never by slicing the DSN. The previous version returned
    everything after the last `@`, which for keyword conninfo — a form psycopg
    accepts and which contains no `@` at all — meant printing the password
    verbatim into a public CI log. Deriving the string from named keys means a
    field nobody enumerated cannot escape this function.
    """
    parsed = _conninfo(dsn)
    shown = [f"{key}={parsed[key]}" for key in SAFE_CONNINFO_KEYS if parsed.get(key)]
    return " ".join(shown) or "<unparseable DSN>"


def _scrub(text: str, dsn: str) -> str:
    """Strike the DSN's actual secret values out of arbitrary text.

    Belt and braces for text we did not compose — a driver's exception message
    is not ours to make promises about.
    """
    parsed = _conninfo(dsn)
    for key in SECRET_CONNINFO_KEYS:
        secret = parsed.get(key)
        if secret:
            text = text.replace(secret, "***")
    return text


#: Seconds to wait for the first connection. Bounded on purpose: an unreachable
#: host with no timeout leaves psycopg blocking until the CI job's own limit
#: kills it, which reports as "the build hung" rather than "the database is
#: down" — and a 15-minute wait for a wrong DSN is its own kind of broken.
CONNECT_TIMEOUT_SECONDS = 10

#: How long to wait for the contending transaction to show up as lock-blocked,
#: and afterwards for it to unblock. Generous: a slow CI runner must not turn a
#: correct implementation into a flaky failure.
LOCK_WAIT_TIMEOUT_SECONDS = 15


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
        pytest.fail(_scrub(
            f"DATABASE_URL is set ({_describe(DSN)}) but the database is unreachable "
            f"after {CONNECT_TIMEOUT_SECONDS}s, so the atomic-invite guarantee went "
            f"unverified: {type(exc).__name__}: {exc}", DSN))

    s = PostgresAuthStore(DSN)
    try:
        s.ensure_schema()
    except Exception as exc:
        pytest.fail(_scrub(
            f"connected to {_describe(DSN)} but the auth schema could not be applied: "
            f"{type(exc).__name__}: {exc}", DSN))
    return s


# ── Credential redaction (no database needed) ────────────────────────────────


@pytest.mark.parametrize("dsn", [
    "postgresql://alice:TOPSECRET@db.example.com:5432/app",
    "postgres://alice:TOPSECRET@db.example.com/app",
    # libpq keyword conninfo. psycopg accepts it, it contains no '@' at all, and
    # the previous implementation printed the whole thing.
    "host=db.example.com dbname=app user=alice password=TOPSECRET",
    "dbname=app password=TOPSECRET",
    "postgresql://alice:TOPSECRET@db.example.com/app?sslmode=require",
])
def test_the_dsn_description_never_contains_the_password(dsn):
    """These strings go into `pytest.fail`, and CI logs are public."""
    described = _describe(dsn)
    assert "TOPSECRET" not in described, f"password leaked into {described!r}"
    assert "password" not in described.lower()


def test_the_dsn_description_still_says_where_the_database_is():
    """Redaction that redacts everything is useless — the point of the message
    is to tell an operator which database could not be reached."""
    described = _describe("host=db.example.com port=5432 dbname=app user=alice password=x")
    assert "db.example.com" in described
    assert "app" in described


def test_a_garbage_dsn_does_not_leak_and_does_not_crash():
    described = _describe("this is not a dsn TOPSECRET")
    assert "TOPSECRET" not in described


def test_scrub_removes_the_password_from_text_we_did_not_compose():
    """A driver's exception message is not ours to make promises about."""
    dsn = "host=db dbname=app user=alice password=TOPSECRET"
    scrubbed = _scrub(f"could not connect using {dsn}", dsn)
    assert "TOPSECRET" not in scrubbed
    assert "***" in scrubbed


# ── The single-use guarantee ─────────────────────────────────────────────────


def _lock_waiters(conn) -> int:
    """Backends currently BLOCKED on a lock in this database.

    Asking PostgreSQL is the difference between "the second redemption is
    waiting for the first to finish" and "the second redemption merely hasn't
    been scheduled yet" — which is the whole point of the test below.
    """
    return conn.execute(
        "SELECT count(*) FROM pg_stat_activity "
        " WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "   AND state = 'active' AND wait_event_type = 'Lock'"
    ).fetchone()[0]


@needs_db
def test_a_second_redemption_blocks_on_the_row_lock_and_then_loses(
    store: PostgresAuthStore,
) -> None:
    """The load-bearing proof, with the overlap forced rather than hoped for.

    Handing two calls to a thread pool does not guarantee the transactions ever
    coexist: the first can commit before the second begins, and *sequential*
    redemption of a one-shot invite fails the second time even in a naive
    check-then-update implementation. A green test like that proves single use,
    not atomicity.

    So this holds the first transaction OPEN, starts the second, and requires
    PostgreSQL to report it as blocked on a lock before letting the first
    commit. An implementation whose read takes no row lock does not block, and
    fails here — which is exactly the discrimination that was missing.
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
        role = store._consume_invite_locked(holder, invite.token)  # noqa: SLF001
        assert role == "player"

        contender = threading.Thread(target=contend, daemon=True)
        contender.start()

        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline and _lock_waiters(observer) == 0:
            time.sleep(0.05)

        assert _lock_waiters(observer) >= 1, (
            "the second redemption never blocked on a lock while the first "
            "transaction held the invite row — the two never actually overlapped, "
            "so single use here is not evidence of atomicity"
        )
        assert not outcome, (
            f"the second redemption completed while the first still held the row: {outcome}"
        )

        # Let the first win. The second must now find used_at already set.
        holder.commit()
        contender.join(timeout=LOCK_WAIT_TIMEOUT_SECONDS)
        assert not contender.is_alive(), "the second redemption never unblocked"

        assert "unexpected" not in outcome, outcome
        assert "ok" not in outcome, "BOTH redemptions succeeded — the invite was used twice"
        assert "rejected" in outcome, f"expected an InviteError, got {outcome}"

        # And the database agrees the invite is spent exactly once.
        stored = store.get_invite(invite.token)
        assert stored is not None and stored.is_used
    finally:
        holder.close()
        observer.close()
        _cleanup(store, invite.token, [winner_email, loser_email])


@needs_db
def test_concurrent_double_redeem_exactly_one_wins(store: PostgresAuthStore) -> None:
    """The weaker companion to the test above: two real threads, no
    synchronisation. It cannot prove the transactions overlapped, so it is not
    the atomicity proof — it covers the ordinary path where two testers click
    the same link and exactly one gets an account."""
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


@needs_db
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

"""The Google identity rules of the auth store (lvs7), one contract, two stores.

The in-memory twin and `PostgresAuthStore` must answer every case here the same
way, including the order in which they notice what is wrong (invite, then
identity, then email), because the twin is what the fast suites and the
deterministic E2E stack run on and Postgres is what ships. The Postgres half
needs a database; the twin half always runs.

Skipping rules are test_invite_atomic.py's: no DATABASE_URL means nobody asked
for a database, so skip; one that does not work is a broken environment, so FAIL.
Diagnostics name no DSN and quote no driver message: CI logs are public.
"""

from __future__ import annotations

import ast
import os
import re
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service.auth_store import (
    AccountGone,
    AuthStore,
    EmailTaken,
    GoogleAlreadyLinked,
    GoogleIdentityTaken,
    IdentityRefused,
    InMemoryAuthStore,
    PostgresAuthStore,
)
from service.invites import InviteAlreadyUsed, InviteExpired, InviteNotFound, InviteRevoked
from service.migrations import migrate

DSN = os.environ.get("DATABASE_URL") or None

needs_database = pytest.mark.skipif(
    DSN is None, reason="no DATABASE_URL (CI always sets it; see .github/workflows/ci.yml)",
)

CONNECT_TIMEOUT_SECONDS = 10
REPO_ROOT = Path(__file__).resolve().parents[2]


def _fail(message: str) -> None:
    pytest.fail(message, pytrace=False)


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    """A throwaway database of this module's own, migrated to this build's schema."""
    assert DSN is not None
    import psycopg

    name = f"gga_lvs7_{uuid.uuid4().hex[:12]}"
    target = re.sub(r"/[^/?]+(\?|$)", f"/{name}\\1", DSN)
    failure: str | None = None
    try:
        with psycopg.connect(DSN, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS) as admin:
            admin.execute(f'CREATE DATABASE "{name}"')
    except Exception as exc:
        failure = f"DATABASE_URL is set but a database could not be created ({type(exc).__name__})"
    if failure:
        _fail(failure)
    try:
        try:
            migrate(target)
        except Exception as exc:
            failure = f"the schema would not apply ({type(exc).__name__})"
        if failure:
            _fail(failure)
        yield target
    finally:
        with psycopg.connect(DSN, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class Harness:
    """One store plus the few things a test must read that the Protocol does not offer."""

    def __init__(self, kind: str, store: InMemoryAuthStore | PostgresAuthStore) -> None:
        self.kind = kind
        self.store: AuthStore = store
        self._raw = store

    # -- fixtures of the world --------------------------------------------------

    def invite(self, role: str = "player", *, ttl: timedelta = timedelta(days=1)) -> str:
        return self.store.create_invite(role, datetime.now(UTC) + ttl).token  # type: ignore[arg-type]

    def password_user(self, email: str):
        return self.store.redeem_invite(self.invite(), email, "argon2-hash-of-a-password")

    # -- observation ------------------------------------------------------------

    @staticmethod
    def _count(cursor) -> int:
        row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def events(self, user_id: int | None) -> list[tuple[str, str, str | None]]:
        if isinstance(self._raw, InMemoryAuthStore):
            return [(a, d, r) for uid, a, d, r in self._raw.identity_events if uid == user_id]
        import psycopg

        with psycopg.connect(self._raw._dsn) as conn:  # noqa: SLF001 - test observation
            rows = conn.execute(
                "SELECT action, decision, reason_code FROM auth.identity_events "
                "WHERE user_id IS NOT DISTINCT FROM %s ORDER BY id", (user_id,),
            ).fetchall()
        return [tuple(r) for r in rows]  # type: ignore[misc]

    def all_event_count(self) -> int:
        if isinstance(self._raw, InMemoryAuthStore):
            return len(self._raw.identity_events)
        import psycopg

        with psycopg.connect(self._raw._dsn) as conn:  # noqa: SLF001
            return self._count(conn.execute("SELECT count(*) FROM auth.identity_events"))

    def identity_rows(self, subject: str) -> int:
        if isinstance(self._raw, InMemoryAuthStore):
            return 1 if subject in self._raw._identities else 0
        import psycopg

        with psycopg.connect(self._raw._dsn) as conn:  # noqa: SLF001
            return self._count(
                conn.execute("SELECT count(*) FROM auth.identities WHERE subject = %s", (subject,)),
            )

    def identities_of(self, user_id: int) -> int:
        if isinstance(self._raw, InMemoryAuthStore):
            return sum(1 for owner, _ in self._raw._identities.values() if owner == user_id)
        import psycopg

        with psycopg.connect(self._raw._dsn) as conn:  # noqa: SLF001
            return self._count(
                conn.execute("SELECT count(*) FROM auth.identities WHERE user_id = %s", (user_id,)),
            )


@pytest.fixture(params=["memory", pytest.param("postgres", marks=needs_database)])
def h(request: pytest.FixtureRequest) -> Harness:
    if request.param == "memory":
        return Harness("memory", InMemoryAuthStore())
    return Harness("postgres", PostgresAuthStore(request.getfixturevalue("database_url")))


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _email() -> str:
    return f"{_unique('user')}@example.com"


# ── Sign up with an invite ───────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["dm", "player"])
def test_redeeming_with_google_makes_a_passwordless_account_with_the_invites_role(h, role):
    token, email, sub = h.invite(role), _email(), _unique("sub")
    user = h.store.redeem_invite_with_google(token, email, sub, "unusable-argon2-hash")
    assert user.role == role and user.email == email
    assert h.store.get_user_by_id(user.id) == user
    assert h.store.google_link_status(user.id).has_password is False
    assert h.store.google_link_status(user.id).linked is True
    assert h.store.get_invite(token).is_used and h.store.get_invite(token).used_by == user.id
    assert h.events(user.id) == [("sign_up", "allowed", None)]
    credentials = h.store.get_credentials(email)
    assert credentials is not None and credentials[1] == "unusable-argon2-hash", "a real hash string is stored"
    assert h.store.sign_in_with_google(sub) == user


def test_a_password_account_has_a_password_and_no_google(h):
    user = h.password_user(_email())
    status = h.store.google_link_status(user.id)
    assert (status.linked, status.email, status.has_password) == (False, None, True)


def test_an_account_made_by_the_old_redeem_has_a_password(h):
    """The previous build's path must keep producing password accounts."""
    user = h.password_user(_email())
    assert h.store.google_link_status(user.id).has_password is True


@pytest.mark.parametrize("dead", ["unknown", "used", "expired", "revoked"])
def test_a_dead_invite_is_refused_by_kind_and_nothing_is_created(h, dead):
    ttl = timedelta(days=-1) if dead == "expired" else timedelta(days=1)
    token, email, sub = h.invite(ttl=ttl), _email(), _unique("sub")
    expected: type[Exception] = {
        "unknown": InviteNotFound, "used": InviteAlreadyUsed, "expired": InviteExpired, "revoked": InviteRevoked,
    }[dead]
    if dead == "unknown":
        token = "no-such-invite-token"
    elif dead == "used":
        h.store.redeem_invite(token, _email(), "hash")
    elif dead == "revoked":
        h.store.revoke_invite(token)
    with pytest.raises(expected):
        h.store.redeem_invite_with_google(token, email, sub, "hash")
    assert h.store.get_user_by_email(email) is None and h.identity_rows(sub) == 0


def test_the_precedence_is_invite_then_identity_then_email(h):
    taken_email, taken_sub = _email(), _unique("taken")
    h.store.redeem_invite_with_google(h.invite(), taken_email, taken_sub, "hash")

    # A dead invite wins over an identity and an email that are both taken.
    dead = h.invite()
    h.store.revoke_invite(dead)
    with pytest.raises(InviteRevoked):
        h.store.redeem_invite_with_google(dead, taken_email, taken_sub, "hash")

    # A taken identity wins over a taken email.
    live = h.invite()
    with pytest.raises(GoogleIdentityTaken):
        h.store.redeem_invite_with_google(live, taken_email, taken_sub, "hash")
    assert not h.store.get_invite(live).is_used, "a refusal leaves the invite unspent"

    # A taken email alone.
    with pytest.raises(EmailTaken):
        h.store.redeem_invite_with_google(live, taken_email.upper(), _unique("fresh"), "hash")
    assert not h.store.get_invite(live).is_used

    # And the invite still works for an address and a Google account nobody has.
    fresh = h.store.redeem_invite_with_google(live, _email(), _unique("fresh"), "hash")
    assert h.store.get_invite(live).used_by == fresh.id


def test_a_refused_redemption_writes_no_account_identity_or_event(h):
    email, sub = _email(), _unique("sub")
    h.password_user(email)
    token = h.invite()
    before = h.all_event_count()
    with pytest.raises(EmailTaken):
        h.store.redeem_invite_with_google(token, email, sub, "hash")
    assert h.identity_rows(sub) == 0 and h.all_event_count() == before
    assert not h.store.get_invite(token).is_used


@pytest.mark.parametrize(
    ("email", "sub"),
    [("a@", ""), ("a@b.c", "s" * 256), ("ab", "s"), ("e" * 250 + "@x.co", "s")],
)
def test_values_the_database_would_refuse_are_refused_before_any_sql(h, email, sub):
    token = h.invite()
    with pytest.raises(IdentityRefused):
        h.store.redeem_invite_with_google(token, email, sub, "hash")
    assert not h.store.get_invite(token).is_used


def test_validation_happens_before_a_connection_is_opened(monkeypatch):
    """The Postgres store checks everything the CHECKs check in Python first, so a
    bad value is never in a statement a driver error could quote. No database needed."""

    def no_connection(*_a, **_k):
        raise AssertionError("a connection was opened for a value that was never going to be accepted")

    monkeypatch.setattr(PostgresAuthStore, "_connect", no_connection)
    store = PostgresAuthStore("postgresql://unused.invalid/none")
    with pytest.raises(IdentityRefused):
        store.redeem_invite_with_google("t", "x@y.co", "", "hash")
    with pytest.raises(IdentityRefused):
        store.link_google(1, "s" * 256, "x@y.co")
    with pytest.raises(IdentityRefused):
        store.link_google(1, "s", "ab")
    with pytest.raises(IdentityRefused):
        store.record_identity_event(1, "sign_in", "refused", None)
    assert store.sign_in_with_google("") is None and store.sign_in_with_google("s" * 256) is None


# ── Sign in ──────────────────────────────────────────────────────────────────


def test_sign_in_finds_the_account_by_subject_and_records_it(h):
    sub = _unique("sub")
    user = h.store.redeem_invite_with_google(h.invite(), _email(), sub, "hash")
    assert h.store.sign_in_with_google(sub) == user
    assert h.events(user.id) == [("sign_up", "allowed", None), ("sign_in", "allowed", None)]


def test_an_unknown_or_unusable_subject_signs_in_nobody_and_writes_nothing(h):
    before = h.all_event_count()
    for subject in (_unique("nobody"), "", "s" * 256):
        assert h.store.sign_in_with_google(subject) is None
    assert h.all_event_count() == before


def test_sign_in_never_matches_by_email(h):
    email = _email()
    h.password_user(email)
    assert h.store.sign_in_with_google(email) is None
    assert h.store.sign_in_with_google(email.upper()) is None


# ── Link ─────────────────────────────────────────────────────────────────────


def test_link_adds_one_identity_once(h):
    user = h.password_user(_email())
    sub, email = _unique("sub"), "google.address@example.com"
    assert h.store.link_google(user.id, sub, email) is True
    assert h.store.link_google(user.id, sub, email) is False, "the same link again is idempotent"
    status = h.store.google_link_status(user.id)
    assert (status.linked, status.email, status.has_password) == (True, email, True)
    assert h.events(user.id) == [("link", "allowed", None)], "only a new row writes an event"
    assert h.store.sign_in_with_google(sub) == user


def test_link_refuses_another_accounts_google_and_a_second_google(h):
    a, b = h.password_user(_email()), h.password_user(_email())
    sub = _unique("sub")
    h.store.link_google(a.id, sub, "a.google@example.com")
    with pytest.raises(GoogleIdentityTaken):
        h.store.link_google(b.id, sub, "b.google@example.com")
    with pytest.raises(GoogleAlreadyLinked):
        h.store.link_google(a.id, _unique("other"), "other@example.com")
    assert h.identities_of(a.id) == 1 and h.identities_of(b.id) == 0


def test_link_precedence_is_identity_then_already_linked(h):
    a, b = h.password_user(_email()), h.password_user(_email())
    sub_a, sub_b = _unique("a"), _unique("b")
    h.store.link_google(a.id, sub_a, "a@example.com")
    h.store.link_google(b.id, sub_b, "b@example.com")
    # b already has a Google AND sub_a is taken: the taken identity is what is reported.
    with pytest.raises(GoogleIdentityTaken):
        h.store.link_google(b.id, sub_a, "b@example.com")


def test_link_to_an_account_that_no_longer_exists_is_account_gone(h):
    with pytest.raises(AccountGone):
        h.store.link_google(2_000_000_000, _unique("sub"), "ghost@example.com")


def test_two_accounts_cannot_link_the_same_google_concurrently(h):
    a, b = h.password_user(_email()), h.password_user(_email())
    sub = _unique("sub")
    outcomes: list[object] = []
    gate = threading.Barrier(2)

    def attempt(user_id: int) -> None:
        gate.wait()
        try:
            outcomes.append(h.store.link_google(user_id, sub, f"g{user_id}@example.com"))
        except GoogleIdentityTaken as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt, args=(u.id,)) for u in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(type(o).__name__ for o in outcomes) == ["GoogleIdentityTaken", "bool"]
    assert h.identity_rows(sub) == 1


def test_two_different_googles_for_one_account_concurrently_leave_one_row(h):
    user = h.password_user(_email())
    outcomes: list[object] = []
    gate = threading.Barrier(2)

    def attempt(sub: str) -> None:
        gate.wait()
        try:
            outcomes.append(h.store.link_google(user.id, sub, f"{sub}@example.com"))
        except GoogleAlreadyLinked as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt, args=(_unique("s"),)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(type(o).__name__ for o in outcomes) == ["GoogleAlreadyLinked", "bool"]
    assert h.identities_of(user.id) == 1, "a concurrent double link is a refusal, never a 500 or two rows"


# ── Concurrency of redemption ────────────────────────────────────────────────


def test_two_threads_redeeming_one_invite_make_exactly_one_account(h):
    token = h.invite()
    outcomes: list[object] = []
    gate = threading.Barrier(2)

    def attempt(n: int) -> None:
        gate.wait()
        try:
            outcomes.append(h.store.redeem_invite_with_google(token, _email(), _unique(f"s{n}"), "hash"))
        except (InviteAlreadyUsed, InviteNotFound) as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    kinds = sorted(type(o).__name__ for o in outcomes)
    assert kinds == ["InviteAlreadyUsed", "User"], kinds


def test_the_twin_holds_its_lock_across_the_check_and_the_write(monkeypatch):
    """A fast pair of threads can win a check-then-write race by luck; this one cannot.
    The twin's email lookup, which runs between its checks and its writes, is made slow, so
    without ONE lock held across both, two redeemers would each see an unspent invite."""
    import time

    twin = InMemoryAuthStore()
    token = twin.create_invite("player", datetime.now(UTC) + timedelta(days=1)).token
    real = InMemoryAuthStore.get_user_by_email

    def slow(self, email):
        time.sleep(0.05)
        return real(self, email)

    monkeypatch.setattr(InMemoryAuthStore, "get_user_by_email", slow)
    outcomes: list[object] = []
    gate = threading.Barrier(2)

    def attempt(n: int) -> None:
        gate.wait()
        try:
            outcomes.append(twin.redeem_invite_with_google(token, f"n{n}@example.com", f"sub-{n}", "hash"))
        except InviteAlreadyUsed as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(type(o).__name__ for o in outcomes) == ["InviteAlreadyUsed", "User"]
    assert len(twin._users) == 1


def test_two_invites_with_one_google_subject_make_one_account_and_leave_the_other_invite_unspent(h):
    first, second = h.invite(), h.invite()
    sub = _unique("shared")
    outcomes: list[object] = []
    gate = threading.Barrier(2)

    def attempt(token: str) -> None:
        gate.wait()
        try:
            outcomes.append(h.store.redeem_invite_with_google(token, _email(), sub, "hash"))
        except GoogleIdentityTaken as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt, args=(t,)) for t in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(type(o).__name__ for o in outcomes) == ["GoogleIdentityTaken", "User"]
    assert h.identity_rows(sub) == 1
    spent = [h.store.get_invite(t).is_used for t in (first, second)]
    assert sorted(spent) == [False, True], "the invite that lost the race is still usable"


# ── identity events ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "decision", "reason"),
    [
        ("sign_in", "refused", "no_account"),
        ("sign_up", "refused", "email_in_use"),
        ("link", "refused", "signin_required"),
        ("sign_in", "allowed", None),
    ],
)
def test_record_identity_event_accepts_what_the_table_accepts(h, action, decision, reason):
    h.store.record_identity_event(None, action, decision, reason)


@pytest.mark.parametrize(
    ("action", "decision", "reason"),
    [
        ("sign_in", "refused", None),  # a refusal without a reason
        ("sign_in", "allowed", "no_account"),  # an allow with a reason
        ("sign_out", "allowed", None),
        ("sign_in", "maybe", "x"),
        ("sign_in", "refused", ""),
        ("sign_in", "refused", "r" * 41),
    ],
)
def test_record_identity_event_refuses_what_the_table_refuses(h, action, decision, reason):
    with pytest.raises(IdentityRefused):
        h.store.record_identity_event(None, action, decision, reason)


def test_an_event_refusal_names_the_field_and_never_the_value(h):
    secret_reason = "sensitive-" + "x" * 40
    with pytest.raises(IdentityRefused) as caught:
        h.store.record_identity_event(None, "sign_in", "refused", secret_reason)
    assert "sensitive" not in str(caught.value)
    with pytest.raises(IdentityRefused) as caught:
        h.store.link_google(1, "s" * 256, "leaky-address@example.com")
    assert "leaky-address" not in str(caught.value)


def test_the_identity_events_writer_has_no_update_delete_or_truncate():
    """`auth.identity_events` is append-only: no module may change or remove a row.
    Every SQL string constant that mentions it, anywhere under service/, is read."""
    forbidden = re.compile(r"\b(UPDATE|DELETE|TRUNCATE)\b", re.IGNORECASE)
    offenders: list[str] = []
    scanned = 0
    for path in sorted((REPO_ROOT / "service").rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "identity_events" in node.value:
                scanned += 1
                if forbidden.search(node.value):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert scanned >= 3, "the scan found the store's identity_events statements, or it is not looking"
    assert not offenders, f"identity_events must be append-only: {offenders}"

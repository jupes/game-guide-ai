"""
Auth store — users + invites persistence.

Mirrors `history.py`: an `AuthStore` Protocol the app talks to, with a Postgres
impl (`auth` schema in the same instance as the corpus) and an in-memory fake
with identical semantics for pure tests. The schema comes from the ordered
migrations (`service/migrations.py`), which the app runs once at startup;
`ensure_schema()` only checks that they have been applied.

The single load-bearing invariant here is **atomic invite consumption**: a
concurrent second redemption of one invite must fail. The Postgres impl enforces
it with a guarded `UPDATE ... WHERE used_at IS NULL ... RETURNING` (the row lock
serializes the two writers); the in-memory impl replicates the logical checks.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol

from .db import Database, default_dsn
from .invites import (
    Invite,
    InviteError,
    InviteNotFound,
    Role,
    new_invite_token,
)
from .migrations import Mode, migrate


class EmailTaken(Exception):
    """Signup email already belongs to an account (case-folded)."""


class GoogleIdentityTaken(Exception):
    """This Google identity (its `sub`) is already linked to an account."""


class GoogleAlreadyLinked(Exception):
    """This account already has a (different) Google identity."""


class AccountGone(Exception):
    """The account an identity was to be linked to no longer exists."""


class IdentityRefused(ValueError):
    """A value the `auth.identities` / `auth.identity_events` CHECKs would refuse.

    Raised BEFORE any SQL runs (lvs7, Critic C-8): a CheckViolation's DETAIL
    quotes the failing row, and `_auth_lookup` logs psycopg errors with their
    traceback, so a bad value caught by the database would write an email or a
    provider subject into the log. The message names the field, never the value."""


# What the CHECK constraints of 0023_google_identities.sql allow. The in-memory
# twin and the Postgres store validate through the same functions, so they refuse
# exactly what the database refuses.
IDENTITY_PROVIDER: Final = "google"
IDENTITY_SUBJECT_MAX: Final = 255
IDENTITY_EMAIL_MIN: Final = 3
IDENTITY_EMAIL_MAX: Final = 254
IDENTITY_REASON_MAX: Final = 40
IdentityAction = Literal["sign_in", "sign_up", "link"]
IdentityDecision = Literal["allowed", "refused"]
IDENTITY_ACTIONS: Final[tuple[str, ...]] = ("sign_in", "sign_up", "link")
IDENTITY_DECISIONS: Final[tuple[str, ...]] = ("allowed", "refused")


def validate_identity_subject(subject: str) -> str:
    if not isinstance(subject, str) or not 1 <= len(subject) <= IDENTITY_SUBJECT_MAX:
        raise IdentityRefused("identity subject must be 1 to 255 characters")
    return subject


def validate_identity_email(email: str) -> str:
    if not isinstance(email, str) or not IDENTITY_EMAIL_MIN <= len(email) <= IDENTITY_EMAIL_MAX:
        raise IdentityRefused("identity email must be 3 to 254 characters")
    return email


def validate_identity_event(action: str, decision: str, reason_code: str | None) -> None:
    if action not in IDENTITY_ACTIONS:
        raise IdentityRefused("identity event action is not a known action")
    if decision not in IDENTITY_DECISIONS:
        raise IdentityRefused("identity event decision is not a known decision")
    if (decision == "allowed") != (reason_code is None):
        raise IdentityRefused("an allowed identity event has no reason code and a refused one has one")
    if reason_code is not None and not 1 <= len(reason_code) <= IDENTITY_REASON_MAX:
        raise IdentityRefused("identity event reason code must be 1 to 40 characters")


@dataclass(frozen=True)
class GoogleLinkStatus:
    """What Profile shows about Google: whether an identity is linked, the
    address Google asserted when it was, and whether the account also has a
    password somebody chose."""

    linked: bool
    email: str | None
    has_password: bool


@dataclass
class User:
    """A registered account. The password hash is intentionally NOT a field —
    it never travels with the identity; the store fetches it only for login."""

    id: int
    email: str
    role: Role
    created_at: datetime


def verified_address(user: User) -> str | None:
    """The address this account has PROVED it holds, or None — and on this build
    it is None for every account (bead 1kg.2.2, L-8; owner question OQ-1).

    An offer of a seat is shown to, and may be accepted by, only a Verified
    account whose verified address is the one the offer names (SEC-50(4), the
    identity ADR's IDA-2). Nothing can be Verified yet: `auth.users` has no
    `email_verified_at`, and IDT-15 enters every existing account as Unverified.
    `User.email` is only the login id — signup checks its shape, never that the
    account can read mail sent to it — so answering with it would let whoever
    registered someone else's address read and accept that person's offers.

    So this fails closed, in one place. `agent-forge-harness-yje.2.1` adds the
    column and makes this return the address exactly when the account is
    Verified. There is no flag, setting or environment variable that turns it
    on; tests override the dependency that reads it, and production has none.
    """
    del user
    return None


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


class AuthStore(Protocol):
    """What the app + admin CLI need from the auth backend."""

    def ensure_schema(self) -> None: ...  # pragma: no cover - structural type

    def create_invite(
        self, role: Role, expires_at: datetime,
    ) -> Invite: ...  # pragma: no cover - structural type

    def get_invite(self, token: str) -> Invite | None:
        ...  # pragma: no cover - structural type

    def list_invites(self) -> list[Invite]:
        ...  # pragma: no cover - structural type

    def revoke_invite(self, token: str) -> bool:
        ...  # pragma: no cover - structural type

    def get_user_by_email(self, email: str) -> User | None:
        ...  # pragma: no cover - structural type

    def get_user_by_id(self, user_id: int) -> User | None:
        ...  # pragma: no cover - structural type

    def get_credentials(self, email: str) -> tuple[User, str] | None:
        ...  # pragma: no cover - structural type

    def redeem_invite(
        self, token: str, email: str, password_hash: str,
    ) -> User: ...  # pragma: no cover - structural type

    # Sign in with Google (lvs7).
    def sign_in_with_google(self, subject: str) -> User | None:
        ...  # pragma: no cover - structural type

    def redeem_invite_with_google(
        self, token: str, email: str, subject: str, password_hash: str,
    ) -> User: ...  # pragma: no cover - structural type

    def link_google(self, user_id: int, subject: str, email: str) -> bool:
        ...  # pragma: no cover - structural type

    def google_link_status(self, user_id: int) -> GoogleLinkStatus:
        ...  # pragma: no cover - structural type

    def record_identity_event(
        self, user_id: int | None, action: str, decision: str, reason_code: str | None = None,
    ) -> None: ...  # pragma: no cover - structural type


@dataclass
class InMemoryAuthStore:
    """Fake with the real store's semantics (single-threaded tests/dev)."""

    _users: list[User] = field(default_factory=list)
    _hashes: dict[int, str] = field(default_factory=dict)
    _invites: dict[str, Invite] = field(default_factory=dict)
    # Sign in with Google (lvs7). `_lock` makes the check-then-mutate of the new
    # methods atomic: routes run in a threadpool, and the deterministic E2E stack
    # serves this twin for real (Critic C-12).
    # `init=False`: the class itself is used as a FastAPI dependency in tests
    # (`dependency_overrides[get_auth_store] = InMemoryAuthStore`), and FastAPI
    # reads every `__init__` parameter as a request field; a lock is not one.
    _passwordless: set[int] = field(default_factory=set, init=False)
    _identities: dict[str, tuple[int, str]] = field(default_factory=dict, init=False)
    identity_events: list[tuple[int | None, str, str, str | None]] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def ensure_schema(self) -> None:
        return None

    def create_invite(self, role: Role, expires_at: datetime) -> Invite:
        invite = Invite(
            token=new_invite_token(), role=role, expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        self._invites[invite.token] = invite
        return invite

    def seed_invite(
        self, token: str, role: Role = "player", expires_at: datetime | None = None,
    ) -> Invite:
        """Insert an invite with a KNOWN token — test/dev only (the real
        `create_invite` mints a random one, which a browser E2E can't guess)."""
        invite = Invite(
            token=token, role=role,
            expires_at=expires_at or (datetime.now(UTC) + timedelta(days=365)),
            created_at=datetime.now(UTC),
        )
        self._invites[token] = invite
        return invite

    def get_invite(self, token: str) -> Invite | None:
        return self._invites.get(token)

    def list_invites(self) -> list[Invite]:
        return sorted(
            self._invites.values(),
            key=lambda i: i.created_at or datetime.min.replace(tzinfo=UTC),
        )

    def revoke_invite(self, token: str) -> bool:
        invite = self._invites.get(token)
        if invite is None or invite.is_revoked or invite.is_used:
            return False
        invite.revoked_at = datetime.now(UTC)
        return True

    def get_user_by_email(self, email: str) -> User | None:
        folded = email.lower()
        return next((u for u in self._users if u.email.lower() == folded), None)

    def get_user_by_id(self, user_id: int) -> User | None:
        return next((u for u in self._users if u.id == user_id), None)

    def get_credentials(self, email: str) -> tuple[User, str] | None:
        user = self.get_user_by_email(email)
        if user is None:
            return None
        stored = self._hashes.get(user.id)
        return (user, stored) if stored is not None else None

    def redeem_invite(
        self, token: str, email: str, password_hash: str, now: datetime | None = None,
    ) -> User:
        invite = self._invites.get(token)
        if invite is None:
            raise InviteNotFound("Unknown invite link.")
        invite.check_redeemable(now)
        if self.get_user_by_email(email) is not None:
            raise EmailTaken("An account already exists for this email.")
        user = User(
            id=len(self._users) + 1, email=email, role=invite.role, created_at=_now(now),
        )
        self._users.append(user)
        self._hashes[user.id] = password_hash
        invite.used_at = _now(now)
        invite.used_by = user.id
        return user

    # ── Sign in with Google (lvs7) ──────────────────────────────────────────
    # Every method holds `_lock` across its checks AND its writes, and performs
    # every check before the first write, in the precedence the Postgres store
    # gets from its constraints: invite, then identity, then email.

    def sign_in_with_google(self, subject: str) -> User | None:
        """The account `subject` is linked to, or None. A hit records its
        `sign_in` / `allowed` event in the same step, as the Postgres store does
        in the same transaction."""
        with self._lock:
            if not 1 <= len(subject) <= IDENTITY_SUBJECT_MAX:
                return None
            hit = self._identities.get(subject)
            user = self.get_user_by_id(hit[0]) if hit is not None else None
            if user is not None:
                self.identity_events.append((user.id, "sign_in", "allowed", None))
            return user

    def redeem_invite_with_google(
        self, token: str, email: str, subject: str, password_hash: str, now: datetime | None = None,
    ) -> User:
        validate_identity_subject(subject)
        validate_identity_email(email)
        with self._lock:
            invite = self._invites.get(token)
            if invite is None:
                raise InviteNotFound("Unknown invite link.")
            invite.check_redeemable(now)
            if subject in self._identities:
                raise GoogleIdentityTaken("That Google account is already linked.")
            if self.get_user_by_email(email) is not None:
                raise EmailTaken("An account already exists for this email.")
            user = User(
                id=len(self._users) + 1, email=email, role=invite.role, created_at=_now(now),
            )
            self._users.append(user)
            self._hashes[user.id] = password_hash
            self._passwordless.add(user.id)
            self._identities[subject] = (user.id, email)
            invite.used_at = _now(now)
            invite.used_by = user.id
            self.identity_events.append((user.id, "sign_up", "allowed", None))
            return user

    def link_google(self, user_id: int, subject: str, email: str) -> bool:
        validate_identity_subject(subject)
        validate_identity_email(email)
        with self._lock:
            if self.get_user_by_id(user_id) is None:
                raise AccountGone("The account no longer exists.")
            existing = self._identities.get(subject)
            if existing is not None:
                if existing[0] == user_id:
                    return False
                raise GoogleIdentityTaken("That Google account is already linked.")
            if any(owner == user_id for owner, _ in self._identities.values()):
                raise GoogleAlreadyLinked("This account already has a Google account.")
            self._identities[subject] = (user_id, email)
            self.identity_events.append((user_id, "link", "allowed", None))
            return True

    def google_link_status(self, user_id: int) -> GoogleLinkStatus:
        with self._lock:
            linked = next((mail for owner, mail in self._identities.values() if owner == user_id), None)
            return GoogleLinkStatus(
                linked=linked is not None, email=linked,
                has_password=user_id not in self._passwordless,
            )

    def record_identity_event(
        self, user_id: int | None, action: str, decision: str, reason_code: str | None = None,
    ) -> None:
        validate_identity_event(action, decision, reason_code)
        with self._lock:
            self.identity_events.append((user_id, action, decision, reason_code))


class PostgresAuthStore:
    """`auth.users` / `auth.invites` in the corpus Postgres. One short-lived
    connection per operation: through the service's bounded gate when it is
    given one (`db`, 1kg.1.5), opened and closed on the spot otherwise."""

    def __init__(self, dsn: str | None = None, *, db: Database | None = None):
        self._given_dsn = dsn
        self._dsn = dsn or default_dsn()
        self._db = db

    def _connect(self):
        if self._db is not None:
            return self._db.connection()
        import psycopg

        return psycopg.connect(self._dsn)

    def ensure_schema(self) -> None:
        """Check — never change — that the database is at this build's schema.

        An operator's checkout is not the deployed image: applying whatever
        migrations it happens to hold, as a side effect of listing invites,
        would put unreviewed DDL into production. Only the service's startup
        and an explicit `python -m service.migrations migrate` change a schema;
        this raises `MigrationsPending` and says so. With no DSN of its own
        the runner chooses one, preferring the schema owner's."""
        migrate(self._given_dsn, mode=Mode.VERIFY)

    def create_invite(self, role: Role, expires_at: datetime) -> Invite:
        token = new_invite_token()
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO auth.invites (token, role, expires_at) VALUES (%s, %s, %s) "
                "RETURNING created_at",
                (token, role, expires_at),
            ).fetchone()
        return Invite(token=token, role=role, expires_at=expires_at, created_at=row[0])

    def get_invite(self, token: str) -> Invite | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token, role, expires_at, used_at, used_by, revoked_at, created_at "
                "FROM auth.invites WHERE token = %s",
                (token,),
            ).fetchone()
        return _row_to_invite(row) if row is not None else None

    def list_invites(self) -> list[Invite]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT token, role, expires_at, used_at, used_by, revoked_at, created_at "
                "FROM auth.invites ORDER BY created_at, token"
            ).fetchall()
        return [_row_to_invite(r) for r in rows]

    def revoke_invite(self, token: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE auth.invites SET revoked_at = now() "
                "WHERE token = %s AND used_at IS NULL AND revoked_at IS NULL "
                "RETURNING token",
                (token,),
            ).fetchone()
        return row is not None

    def get_user_by_email(self, email: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, role, created_at FROM auth.users WHERE lower(email) = lower(%s)",
                (email,),
            ).fetchone()
        return User(id=row[0], email=row[1], role=row[2], created_at=row[3]) if row else None

    def get_user_by_id(self, user_id: int) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, role, created_at FROM auth.users WHERE id = %s",
                (user_id,),
            ).fetchone()
        return User(id=row[0], email=row[1], role=row[2], created_at=row[3]) if row else None

    def get_credentials(self, email: str) -> tuple[User, str] | None:
        """Identity + password hash in ONE query. Login needs both, and fetching
        them separately meant two round trips (on the endpoint an anonymous
        caller can hammer) and two independent chances for the store to fail."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, email, role, created_at, password_hash "
                "FROM auth.users WHERE lower(email) = lower(%s)",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return User(id=row[0], email=row[1], role=row[2], created_at=row[3]), row[4]

    def redeem_invite(self, token: str, email: str, password_hash: str) -> User:
        from psycopg import errors as pg_errors

        try:
            with self._connect() as conn:
                role = self._consume_invite_locked(conn, token)
                user_row = conn.execute(
                    "INSERT INTO auth.users (email, password_hash, role) VALUES (%s, %s, %s) "
                    "RETURNING id, created_at",
                    (email, password_hash, role),
                ).fetchone()
                user_id, created_at = user_row
                conn.execute(
                    "UPDATE auth.invites SET used_by = %s WHERE token = %s",
                    (user_id, token),
                )
                return User(id=user_id, email=email, role=role, created_at=created_at)
        except pg_errors.UniqueViolation as exc:
            # The whole transaction (incl. the used_at flip) rolled back, so the
            # invite stays unused — the tester can retry with a different email.
            raise EmailTaken("An account already exists for this email.") from exc

    # ── Sign in with Google (lvs7) ──────────────────────────────────────────
    # Validation happens in Python BEFORE any SQL, and unique violations are
    # classified by constraint name, never by message: a driver error's DETAIL
    # quotes the failing row, which holds an email and a provider subject.

    def sign_in_with_google(self, subject: str) -> User | None:
        """The account `subject` is linked to, or None. A hit writes its
        `sign_in` / `allowed` event in the same transaction as the lookup."""
        if not 1 <= len(subject) <= IDENTITY_SUBJECT_MAX:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.role, u.created_at FROM auth.identities i "
                "JOIN auth.users u ON u.id = i.user_id "
                "WHERE i.provider = 'google' AND i.subject = %s",
                (subject,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "INSERT INTO auth.identity_events (user_id, provider, action, decision) "
                "VALUES (%s, 'google', 'sign_in', 'allowed')",
                (row[0],),
            )
        return User(id=row[0], email=row[1], role=row[2], created_at=row[3])

    def redeem_invite_with_google(
        self, token: str, email: str, subject: str, password_hash: str,
    ) -> User:
        """One transaction: consume the invite, create the account with the
        invite's role and no password of its own, link the identity, write the
        event. Any failure rolls all of it back, so the invite stays unspent."""
        from psycopg import errors as pg_errors

        validate_identity_subject(subject)
        validate_identity_email(email)
        try:
            with self._connect() as conn:
                role = self._consume_invite_locked(conn, token)
                # Noticed BEFORE the email, as the in-memory twin does: a taken
                # Google account is reported as that, whatever else is taken. The
                # identity's primary key below is still what decides a race.
                if conn.execute(
                    "SELECT 1 FROM auth.identities WHERE provider = 'google' AND subject = %s", (subject,),
                ).fetchone() is not None:
                    raise GoogleIdentityTaken("That Google account is already linked.")
                user_row = conn.execute(
                    "INSERT INTO auth.users (email, password_hash, role, has_password) "
                    "VALUES (%s, %s, %s, false) RETURNING id, created_at",
                    (email, password_hash, role),
                ).fetchone()
                user_id, created_at = user_row
                conn.execute(
                    "INSERT INTO auth.identities (provider, subject, user_id, email_at_link) "
                    "VALUES ('google', %s, %s, %s)",
                    (subject, user_id, email),
                )
                conn.execute(
                    "UPDATE auth.invites SET used_by = %s WHERE token = %s",
                    (user_id, token),
                )
                conn.execute(
                    "INSERT INTO auth.identity_events (user_id, provider, action, decision) "
                    "VALUES (%s, 'google', 'sign_up', 'allowed')",
                    (user_id,),
                )
                return User(id=user_id, email=email, role=role, created_at=created_at)
        except pg_errors.UniqueViolation as exc:
            constraint = exc.diag.constraint_name
            if constraint == "users_email_lower_uidx":
                raise EmailTaken("An account already exists for this email.") from None
            if constraint == "identities_pkey":
                raise GoogleIdentityTaken("That Google account is already linked.") from None
            raise

    def link_google(self, user_id: int, subject: str, email: str) -> bool:
        """Link `subject` to `user_id`. True: a new row. False: it was already
        this account's. Raises `GoogleIdentityTaken` (another account's),
        `GoogleAlreadyLinked` (this account has a different one) or
        `AccountGone` (the account vanished mid-flow)."""
        from psycopg import errors as pg_errors

        validate_identity_subject(subject)
        validate_identity_email(email)
        try:
            with self._connect() as conn:
                inserted = conn.execute(
                    "INSERT INTO auth.identities (provider, subject, user_id, email_at_link) "
                    "VALUES ('google', %s, %s, %s) "
                    "ON CONFLICT ON CONSTRAINT identities_pkey DO NOTHING RETURNING user_id",
                    (subject, user_id, email),
                ).fetchone()
                if inserted is not None:
                    conn.execute(
                        "INSERT INTO auth.identity_events (user_id, provider, action, decision) "
                        "VALUES (%s, 'google', 'link', 'allowed')",
                        (user_id,),
                    )
                    return True
                owner = conn.execute(
                    "SELECT user_id FROM auth.identities WHERE provider = 'google' AND subject = %s",
                    (subject,),
                ).fetchone()
        except pg_errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "identities_user_provider_key":
                raise GoogleAlreadyLinked("This account already has a Google account.") from None
            raise
        except pg_errors.ForeignKeyViolation:
            raise AccountGone("The account no longer exists.") from None
        if owner is not None and owner[0] == user_id:
            return False
        raise GoogleIdentityTaken("That Google account is already linked.")

    def google_link_status(self, user_id: int) -> GoogleLinkStatus:
        with self._connect() as conn:
            linked = conn.execute(
                "SELECT email_at_link FROM auth.identities WHERE provider = 'google' AND user_id = %s",
                (user_id,),
            ).fetchone()
            account = conn.execute(
                "SELECT has_password FROM auth.users WHERE id = %s", (user_id,),
            ).fetchone()
        return GoogleLinkStatus(
            linked=linked is not None,
            email=linked[0] if linked is not None else None,
            has_password=True if account is None else bool(account[0]),
        )

    def record_identity_event(
        self, user_id: int | None, action: str, decision: str, reason_code: str | None = None,
    ) -> None:
        validate_identity_event(action, decision, reason_code)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO auth.identity_events (user_id, provider, action, decision, reason_code) "
                "VALUES (%s, 'google', %s, %s, %s)",
                (user_id, action, decision, reason_code),
            )

    def _consume_invite_locked(self, conn, token: str) -> Role:
        """Atomically flip an invite to used and return its role, or raise the
        specific InviteError. The guarded UPDATE is the single-use guarantee:
        a concurrent second caller blocks on the row lock, then finds used_at
        already set and matches zero rows."""
        row = conn.execute(
            "UPDATE auth.invites SET used_at = now() "
            "WHERE token = %s AND used_at IS NULL AND revoked_at IS NULL AND expires_at > now() "
            "RETURNING role",
            (token,),
        ).fetchone()
        if row is not None:
            return row[0]
        current = conn.execute(
            "SELECT token, role, expires_at, used_at, used_by, revoked_at, created_at "
            "FROM auth.invites WHERE token = %s",
            (token,),
        ).fetchone()
        if current is None:
            raise InviteNotFound("Unknown invite link.")
        # Re-derive the exact reason (used / revoked / expired) for a clear message.
        _row_to_invite(current).check_redeemable()
        raise InviteError("Invite could not be redeemed.")  # pragma: no cover - defensive


def _row_to_invite(row) -> Invite:
    return Invite(
        token=row[0], role=row[1], expires_at=row[2], used_at=row[3],
        used_by=row[4], revoked_at=row[5], created_at=row[6],
    )

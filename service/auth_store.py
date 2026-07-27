"""
Auth store (x5bz.2 Checkpoint A) — users + invites persistence.

Mirrors `history.py`: a `AuthStore` Protocol the app talks to, with a Postgres
impl (`auth` schema in the same instance as the corpus) and an in-memory fake
with identical semantics for pure tests. `ensure_schema()` runs idempotent DDL
at startup — the migration path for volumes predating the auth schema (kept in
sync with vector-db/init/05-auth-schema.sql).

The single load-bearing invariant here is **atomic invite consumption**: a
concurrent second redemption of one invite must fail. The Postgres impl enforces
it with a guarded `UPDATE ... WHERE used_at IS NULL ... RETURNING` (the row lock
serializes the two writers); the in-memory impl replicates the logical checks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .invites import (
    Invite,
    InviteError,
    InviteNotFound,
    Role,
    new_invite_token,
)

# Idempotent DDL — kept in sync with vector-db/init/05-auth-schema.sql.
AUTH_SCHEMA_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
  id            BIGSERIAL PRIMARY KEY,
  email         TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'player' CHECK (role IN ('player', 'dm')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uidx
  ON auth.users (lower(email));

CREATE TABLE IF NOT EXISTS auth.invites (
  token       TEXT PRIMARY KEY,
  role        TEXT NOT NULL DEFAULT 'player' CHECK (role IN ('player', 'dm')),
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ,
  used_by     BIGINT REFERENCES auth.users (id),
  revoked_at  TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS invites_created_idx ON auth.invites (created_at);
"""


class EmailTaken(Exception):
    """Signup email already belongs to an account (case-folded)."""


@dataclass
class User:
    """A registered account. The password hash is intentionally NOT a field —
    it never travels with the identity; the store fetches it only for login."""

    id: int
    email: str
    role: Role
    created_at: datetime


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


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

    def password_hash_for(self, email: str) -> str | None:
        ...  # pragma: no cover - structural type

    def redeem_invite(
        self, token: str, email: str, password_hash: str,
    ) -> User: ...  # pragma: no cover - structural type


@dataclass
class InMemoryAuthStore:
    """Fake with the real store's semantics (single-threaded tests/dev)."""

    _users: list[User] = field(default_factory=list)
    _hashes: dict[int, str] = field(default_factory=dict)
    _invites: dict[str, Invite] = field(default_factory=dict)

    def ensure_schema(self) -> None:
        return None

    def create_invite(self, role: Role, expires_at: datetime) -> Invite:
        invite = Invite(
            token=new_invite_token(), role=role, expires_at=expires_at,
            created_at=datetime.now(timezone.utc),
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
            expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(days=365)),
            created_at=datetime.now(timezone.utc),
        )
        self._invites[token] = invite
        return invite

    def get_invite(self, token: str) -> Invite | None:
        return self._invites.get(token)

    def list_invites(self) -> list[Invite]:
        return sorted(
            self._invites.values(),
            key=lambda i: i.created_at or datetime.min.replace(tzinfo=timezone.utc),
        )

    def revoke_invite(self, token: str) -> bool:
        invite = self._invites.get(token)
        if invite is None or invite.is_revoked or invite.is_used:
            return False
        invite.revoked_at = datetime.now(timezone.utc)
        return True

    def get_user_by_email(self, email: str) -> User | None:
        folded = email.lower()
        return next((u for u in self._users if u.email.lower() == folded), None)

    def get_user_by_id(self, user_id: int) -> User | None:
        return next((u for u in self._users if u.id == user_id), None)

    def password_hash_for(self, email: str) -> str | None:
        user = self.get_user_by_email(email)
        return self._hashes.get(user.id) if user is not None else None

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


class PostgresAuthStore:
    """`auth.users` / `auth.invites` in the corpus Postgres. One connection per
    operation (chat/auth traffic is single-user scale)."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or os.environ.get(
            "DATABASE_URL", "postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai"
        )

    def _connect(self):
        import psycopg

        return psycopg.connect(self._dsn)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(AUTH_SCHEMA_DDL)

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

    def password_hash_for(self, email: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash FROM auth.users WHERE lower(email) = lower(%s)",
                (email,),
            ).fetchone()
        return row[0] if row is not None else None

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

"""
Database access (1kg.1.5): bounded pools, one transaction boundary, and the
in-memory twin unit tests use.

**Why pools.** Every store used to open a connection per operation, which cannot
be bounded: twenty concurrent requests on each of two instances is forty
connections against a `db-f1-micro` that accepts twenty-two. `Database` holds a
synchronous pool for routes and an asynchronous one for the realtime path, so a
reconnect storm's snapshots never take request threads. Both are sized so that
`PoolSettings.per_instance` times the deployed instance count, plus the
operator's own sessions, fits the server (`tests/test_deploy_contract.py`).

Both pools keep **no idle minimum**. Cloud Run gives an instance CPU only while
a request is in flight, so a pool's housekeeping cannot be relied on to shed
connections from an idle instance; starting from zero and checking every
connection as it is handed out (a frozen instance's sockets die quietly) is what
fits that platform.

**The transaction boundary.** `Database.transaction()` yields a unit of work.
Whatever a repository writes through it — an aggregate, its outbox job
(`service/jobs.py`), a wake-up notification — commits or rolls back together.
`on_commit` callbacks run only after the commit, and after the connection has
gone back to the pool, so a callback may use the database itself.

**The fake.** `InMemoryDatabase` gives unit tests the same boundary: changes
register their own undo, a failed block takes them back in reverse, and
notifications and callbacks are released on commit only. Every store keeps the
repository's pattern — a `Protocol`, an in-memory implementation, a Postgres one.

`DB_POOL_MAX=0` turns pooling off and restores a connection per operation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: Local development only (docker-compose.yml's default credentials).
DEFAULT_DSN = "postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai"

CONNECT_TIMEOUT_S = 10

#: A pool nobody has borrowed from for this long is checked before it lends
#: again. Cloud Run freezes an instance between requests and the sockets it was
#: holding die quietly; met one at a time during checkout, each dead connection
#: costs a growing backoff, and four of them cost the request. Swept up front,
#: they cost one new connection.
IDLE_SUSPECT_S = 30.0

#: What `db-f1-micro` accepts from non-superusers (docs/deploy-gcp.md §3).
SERVER_CONNECTION_LIMIT = 22
#: Kept free for the operator: a proxy session and `python -m service.admin_invites`.
RESERVED_FOR_OPERATORS = 2

#: A channel is a Postgres identifier; a payload is an id, never content (SEC-20).
_CHANNEL = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
NOTIFY_PAYLOAD_MAX_CHARS = 200


def default_dsn() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DSN


class AdvisoryLock(IntEnum):
    """The first key of every two-key advisory lock the service takes, so two
    features can never contend for one lock by accident. Add a member; never
    reuse a number."""

    MIGRATIONS = 1
    #: Per table session, around the connection-bound checks (RT-8, 1kg.1.4).
    TABLE_SESSION = 2


def advisory_key(key: str) -> int:
    """A stable signed 32-bit key for a string id. A collision only makes two
    unrelated holders wait for each other; it can never let two related ones in."""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big", signed=True)


def _int_setting(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}")
    return value


@dataclass(frozen=True)
class PoolSettings:
    """Bounded on purpose: a typo must not become six hundred connections."""

    #: `DB_POOL_MAX`. Zero means no pool — a connection per operation, as before.
    sync_max: int = 6
    #: `DB_ASYNC_POOL_MAX`. Opened on first use, so it costs nothing until then.
    async_max: int = 3
    #: `DB_POOL_TIMEOUT_S`: how long a request waits for a connection before it
    #: fails as "backend unavailable". Short, so an outage does not stack threads.
    acquire_timeout_s: int = 5
    max_idle_s: int = 60
    max_lifetime_s: int = 1800

    #: One dedicated LISTEN connection per instance, outside both pools (RT-5).
    LISTENERS = 1

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PoolSettings:
        source = os.environ if env is None else env
        return cls(
            sync_max=_int_setting(source, "DB_POOL_MAX", cls.sync_max, 0, 10),
            async_max=_int_setting(source, "DB_ASYNC_POOL_MAX", cls.async_max, 0, 5),
            acquire_timeout_s=_int_setting(source, "DB_POOL_TIMEOUT_S", cls.acquire_timeout_s, 1, 60),
        )

    @property
    def per_instance(self) -> int:
        """The most connections one instance can hold open at once."""
        return self.sync_max + self.async_max + self.LISTENERS


# ── The unit of work ─────────────────────────────────────────────────────────


class UnitOfWork(Protocol):
    """What a repository may ask of the transaction it is writing in."""

    def on_commit(self, callback: Callable[[], None]) -> None:
        """Run `callback` once the transaction has committed — never otherwise."""
        ...  # pragma: no cover - structural type

    def notify(self, channel: str, payload: str = "") -> None:
        """Wake listeners up when (and only if) the transaction commits. A
        wake-up, never the record: the payload is an id, and readers fetch state."""
        ...  # pragma: no cover - structural type

    def lock(self, lock_class: AdvisoryLock, key: str) -> None:
        """Serialise with every other transaction holding `(lock_class, key)`,
        until this one ends."""
        ...  # pragma: no cover - structural type


def _check_notification(channel: str, payload: str) -> None:
    if _CHANNEL.fullmatch(channel) is None:
        raise ValueError("a notification channel is a lowercase identifier")
    if len(payload) > NOTIFY_PAYLOAD_MAX_CHARS:
        raise ValueError("a notification carries an id, not content")


def _run_after_commit(callbacks: list[Callable[[], None]]) -> None:
    """The transaction is already committed: a callback that fails is logged and
    the rest still run. Raising would tell the caller its write was lost."""
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:
            log.warning("db: an after-commit callback failed (%s)", type(exc).__name__)


class PgTransaction:
    """A unit of work on one pooled connection, inside one transaction."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._after_commit: list[Callable[[], None]] = []

    def on_commit(self, callback: Callable[[], None]) -> None:
        self._after_commit.append(callback)

    def notify(self, channel: str, payload: str = "") -> None:
        _check_notification(channel, payload)
        # pg_notify() rather than NOTIFY: it takes parameters. Postgres holds the
        # notification until commit and drops it on rollback.
        self.conn.execute("SELECT pg_notify(%s, %s)", (channel, payload))

    def lock(self, lock_class: AdvisoryLock, key: str) -> None:
        self.conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (int(lock_class), advisory_key(key)))


class TransactionalDatabase(Protocol):
    def transaction(self) -> AbstractContextManager[UnitOfWork]: ...  # pragma: no cover - structural type


class Database:
    """The service's Postgres: two bounded pools and the transaction boundary."""

    def __init__(self, dsn: str | None = None, settings: PoolSettings | None = None) -> None:
        self._dsn = dsn or default_dsn()
        self.settings = settings if settings is not None else PoolSettings()
        self._pool: Any = None
        self._async_pool: Any = None
        self._last_borrowed = {"sync": time.monotonic(), "async": time.monotonic()}
        self._clock_lock = threading.Lock()
        if self.settings.sync_max > 0:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                self._dsn,
                kwargs=self._connect_kwargs("sync"),
                min_size=0,
                max_size=self.settings.sync_max,
                open=False,
                check=ConnectionPool.check_connection,
                timeout=self.settings.acquire_timeout_s,
                max_idle=self.settings.max_idle_s,
                max_lifetime=self.settings.max_lifetime_s,
                name="sync",
            )
        if self.settings.async_max > 0:
            from psycopg_pool import AsyncConnectionPool

            self._async_pool = AsyncConnectionPool(
                self._dsn,
                kwargs=self._connect_kwargs("async"),
                min_size=0,
                max_size=self.settings.async_max,
                open=False,
                check=AsyncConnectionPool.check_connection,
                timeout=self.settings.acquire_timeout_s,
                max_idle=self.settings.max_idle_s,
                max_lifetime=self.settings.max_lifetime_s,
                name="async",
            )

    @staticmethod
    def _connect_kwargs(role: str) -> dict[str, Any]:
        # application_name: `pg_stat_activity` then says who holds each connection.
        return {"connect_timeout": CONNECT_TIMEOUT_S, "application_name": f"game-guide-ai:{role}"}

    def open(self) -> None:
        """Start the synchronous pool. With no idle minimum this connects to
        nothing, so it cannot fail because the database happens to be down."""
        if self._pool is not None:
            self._pool.open(wait=False)

    def close(self) -> None:
        """Close the synchronous pool and every connection it holds. Idempotent."""
        if self._pool is not None:
            self._pool.close()

    async def aclose(self) -> None:
        """Close both pools — the shutdown path of an async application."""
        if self._async_pool is not None:
            await self._async_pool.close()
        self.close()

    def _was_idle(self, pool: str) -> bool:
        now = time.monotonic()
        with self._clock_lock:
            idle = now - self._last_borrowed[pool]
            self._last_borrowed[pool] = now
        return idle > IDLE_SUSPECT_S

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """One connection for one short operation: committed on a clean exit,
        rolled back on an exception, then returned (or closed, without a pool)."""
        if self._pool is None:
            import psycopg

            with psycopg.connect(self._dsn, **self._connect_kwargs("direct")) as conn:
                yield conn
            return
        if self._was_idle("sync"):
            self._pool.check()
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[PgTransaction]:
        with self.connection() as conn:
            unit = PgTransaction(conn)
            with conn.transaction():
                yield unit
        _run_after_commit(unit._after_commit)

    @asynccontextmanager
    async def async_connection(self) -> AsyncIterator[Any]:
        """A connection from the realtime pool, which opens on first use."""
        if self._async_pool is None:
            raise RuntimeError("the async pool is disabled (DB_ASYNC_POOL_MAX=0)")
        if self._async_pool.closed:
            await self._async_pool.open(wait=False)
        elif self._was_idle("async"):
            await self._async_pool.check()
        async with self._async_pool.connection() as conn:
            yield conn


# ── The in-memory twin ───────────────────────────────────────────────────────


class InMemoryTransaction:
    """The unit of work of `InMemoryDatabase`. A fake store mutates its state at
    once and registers how to take the change back."""

    def __init__(self) -> None:
        self._after_commit: list[Callable[[], None]] = []
        self._undo: list[Callable[[], None]] = []
        self.notifications: list[tuple[str, str]] = []
        self.locks: list[tuple[AdvisoryLock, str]] = []

    def on_commit(self, callback: Callable[[], None]) -> None:
        self._after_commit.append(callback)

    def on_rollback(self, undo: Callable[[], None]) -> None:
        self._undo.append(undo)

    def notify(self, channel: str, payload: str = "") -> None:
        _check_notification(channel, payload)
        self.notifications.append((channel, payload))

    def lock(self, lock_class: AdvisoryLock, key: str) -> None:
        # Recorded for assertions. The database-wide lock below already makes
        # every in-memory transaction serial, which is the strongest reading.
        self.locks.append((lock_class, key))


class InMemoryDatabase:
    """Transactions for fakes: serial, all-or-nothing, with commit-time release
    of notifications and callbacks — the observable contract of `Database`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: Every notification of every committed transaction, in order.
        self.notifications: list[tuple[str, str]] = []

    @contextmanager
    def transaction(self) -> Iterator[InMemoryTransaction]:
        unit = InMemoryTransaction()
        with self._lock:
            try:
                yield unit
            except BaseException:
                for undo in reversed(unit._undo):
                    undo()
                raise
            self.notifications.extend(unit.notifications)
        _run_after_commit(unit._after_commit)

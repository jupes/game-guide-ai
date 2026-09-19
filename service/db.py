"""
Database access (1kg.1.5): a bounded gate for routes, a small pool for the
realtime path, one transaction boundary, and the in-memory twin unit tests use.

**Why a bound.** Every store used to open a connection per operation with
nothing limiting how many: twenty concurrent requests on each of two instances
is forty connections against a `db-f1-micro` that accepts twenty-two.

**Why routes keep no connections.** Cloud Run gives an instance CPU only while a
request is in flight, so a keep-alive pool cannot shed its idle connections from
an instance that has gone quiet — and during a rollout the previous revision's
instances go quiet all at once, still holding theirs. The synchronous side is
therefore a **gate**, not a cache: at most `DB_POOL_MAX` connections open at the
same moment per instance, each opened for one operation and closed after it,
which is what the stores always did. An idle or frozen instance holds nothing,
no connection is ever stale, and the only thing that changed for a request is
that it may wait its turn. `tests/test_deploy_contract.py` checks the arithmetic
against `scripts/deploy.sh`, including a rollout's overlap of two revisions.

**The realtime path** is different: a stream is a request, so its instance has
CPU for as long as it matters, and state reads are frequent. It gets a small
`AsyncConnectionPool`, opened on first use, checked on checkout, and marked with
`idle_session_timeout` so that the *server* reaps whatever a frozen instance left
behind. A dedicated LISTEN connection (RT-5) is counted in the budget and owned
by the realtime beads.

**The transaction boundary.** `Database.transaction()` yields a unit of work.
Whatever a repository writes through it — an aggregate, its outbox job
(`service/jobs.py`), a wake-up notification — commits or rolls back together.
`on_commit` callbacks run only after the commit, and after the connection has
been given back, so a callback may use the database itself.

**The fake.** `InMemoryDatabase` gives unit tests the same boundary: changes
register their own undo, a failed block takes them back in reverse, and what a
transaction publishes — rows other readers may see, notifications, callbacks —
is released on commit only. Every store keeps the repository's pattern: a
`Protocol`, an in-memory implementation, a Postgres one.

`DB_POOL_MAX=0` removes the gate: a connection per operation, unbounded, as before.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout

log = logging.getLogger(__name__)

#: Local development only (docker-compose.yml's default credentials).
DEFAULT_DSN = "postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai"

CONNECT_TIMEOUT_S = 10

#: What `db-f1-micro` accepts from non-superusers (docs/deploy-gcp.md §3).
SERVER_CONNECTION_LIMIT = 22
#: Kept free for the operator: a proxy session and `python -m service.admin_invites`.
RESERVED_FOR_OPERATORS = 2
#: One per starting instance, for as long as its migration check takes.
MIGRATION_SESSIONS = 1

#: A channel is a Postgres identifier; a payload is an id, never content (SEC-20).
_CHANNEL = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
NOTIFY_PAYLOAD_MAX_CHARS = 200


def default_dsn() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DSN


def check_dsn(name: str, dsn: str) -> str:
    """Refuse a connection string that does not parse — without repeating it.

    psycopg quotes the whole string, password included, when it cannot parse
    one, and a pool would log that text on every retry (SEC-21). A bad setting is
    a verdict, so it is raised here, once, by the variable's name only.
    """
    try:
        psycopg.conninfo.conninfo_to_dict(dsn)
    except psycopg.Error:
        parsed = False
    else:
        parsed = True
    if not parsed:  # outside the except block: the driver's message is not chained
        raise ValueError(f"{name} is not a valid PostgreSQL connection string")
    return dsn


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

    #: `DB_POOL_MAX`: connections routes may have open at once. Zero removes the
    #: gate. Four, not more: a rollout overlaps two revisions of two instances,
    #: and 4 x 4 plus a migration session and the operator's two is 19 of 22.
    sync_max: int = 4
    #: `DB_ASYNC_POOL_MAX`. Opened on first use, so it costs nothing until then.
    async_max: int = 3
    #: `DB_POOL_TIMEOUT_S`: how long a request waits for its turn before it
    #: fails as "backend unavailable". Short, so an outage does not stack threads.
    acquire_timeout_s: int = 5
    #: The realtime pool sheds a connection it has not used for this long ...
    max_idle_s: int = 60
    max_lifetime_s: int = 1800
    #: ... and the server ends the session of one that could not (a frozen instance).
    idle_session_timeout_s: int = 120

    #: One dedicated LISTEN connection per instance, outside both (RT-5).
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
        """The most connections one instance can have open at once."""
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
    """A unit of work on one connection, inside one transaction."""

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
    """The service's Postgres: a gate for routes, a pool for the realtime path,
    and the transaction boundary."""

    def __init__(self, dsn: str | None = None, settings: PoolSettings | None = None) -> None:
        self._dsn = check_dsn("DATABASE_URL", dsn or default_dsn())
        self.settings = settings if settings is not None else PoolSettings()
        self._closed = False
        self._gate = threading.BoundedSemaphore(self.settings.sync_max) if self.settings.sync_max > 0 else None
        self._async_pool: Any = None
        if self.settings.async_max > 0:
            self._async_pool = AsyncConnectionPool(
                self._dsn,
                kwargs={
                    **self._connect_kwargs("async"),
                    # The server ends what a frozen instance cannot: see the module docstring.
                    "options": f"-c idle_session_timeout={self.settings.idle_session_timeout_s}s",
                },
                min_size=0,
                max_size=self.settings.async_max,
                open=False,
                check=AsyncConnectionPool.check_connection,
                timeout=self.settings.acquire_timeout_s,
                max_idle=self.settings.max_idle_s,
                max_lifetime=self.settings.max_lifetime_s,
                name="async",
            )
            # The pool logs the driver's own text for every failed connect, at
            # WARNING, with retries. Borrowers get the exception; logs do not need it.
            logging.getLogger("psycopg.pool").setLevel(logging.ERROR)

    @staticmethod
    def _connect_kwargs(role: str) -> dict[str, Any]:
        # application_name: `pg_stat_activity` then says who holds each connection.
        return {"connect_timeout": CONNECT_TIMEOUT_S, "application_name": f"game-guide-ai:{role}"}

    def close(self) -> None:
        """Refuse new borrowers. Nothing is held between operations, so there is
        nothing else to close on the synchronous side. Idempotent."""
        self._closed = True

    async def aclose(self) -> None:
        """Close the realtime pool and the gate — an async application's shutdown."""
        if self._async_pool is not None:
            await self._async_pool.close()
        self.close()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """One connection for one short operation: opened when its turn comes,
        committed on a clean exit, rolled back on an exception, then closed."""
        if self._closed:
            raise PoolClosed("the database is closed")
        gate = self._gate
        if gate is not None and not gate.acquire(timeout=self.settings.acquire_timeout_s):
            # An OperationalError, like the driver's own: routes already answer 503.
            raise PoolTimeout(f"no database connection came free in {self.settings.acquire_timeout_s} s")
        try:
            with psycopg.connect(self._dsn, **self._connect_kwargs("sync")) as conn:
                yield conn
        finally:
            if gate is not None:
                gate.release()

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
        if self._async_pool.closed and not self._closed:
            await self._async_pool.open(wait=False)
        async with self._async_pool.connection() as conn:
            yield conn


# ── The in-memory twin ───────────────────────────────────────────────────────


class InMemoryTransaction:
    """The unit of work of `InMemoryDatabase`. A fake store either changes its
    state at once and registers how to take the change back (`on_rollback`), or
    stages what other readers must not see yet and registers how to make it
    visible (`on_publish`)."""

    def __init__(self) -> None:
        self._after_commit: list[Callable[[], None]] = []
        self._publish: list[Callable[[], None]] = []
        self._undo: list[Callable[[], None]] = []
        self.notifications: list[tuple[str, str]] = []
        self.locks: list[tuple[AdvisoryLock, str]] = []

    def on_commit(self, callback: Callable[[], None]) -> None:
        self._after_commit.append(callback)

    def on_publish(self, publish: Callable[[], None]) -> None:
        """Runs as part of the commit, before any `on_commit` callback: the
        moment a row becomes visible outside its transaction."""
        self._publish.append(publish)

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
    of published rows, notifications and callbacks — the observable contract of
    `Database`."""

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
            for publish in unit._publish:
                publish()
            self.notifications.extend(unit.notifications)
        _run_after_commit(unit._after_commit)

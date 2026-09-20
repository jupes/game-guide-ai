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


class CampaignLockRefused(Exception):
    """What `lock_campaign` and `advance_authz_revision` refuse to do (1kg.2.1,
    RQ-2). Each subclass is a distinct refusal so that a route can map one to a
    404 and let the others be the 500 they are."""


class CampaignAuthzMissing(CampaignLockRefused):
    """No `campaign.authz_state` row for that campaign, so its authorisation
    cannot be held still — fail closed (RQ-2(c)). A route maps this to its
    generic 404, which is also what a campaign that never existed gets; a
    background job makes it a no-op."""


class CampaignLockNotHeld(CampaignLockRefused):
    """The authorisation revision was written by a transaction that does not hold
    that campaign's lock exclusively — the one rule that keeps a revision from
    advancing while another reader believes it is stable (RQ-2)."""


class CampaignLockOrder(CampaignLockRefused):
    """The campaign lock was not this transaction's first lock, a second campaign
    was asked for, or a shared holder tried to upgrade in place. All three are
    deadlock shapes rather than authorisation failures, and all three are a
    programming error caught here instead of at three in the morning."""


#: How the two modes are recorded and named. `share` is FOR SHARE, `exclusive`
#: is FOR UPDATE on the `authz_state` row.
SHARE = "share"
EXCLUSIVE = "exclusive"


class _CampaignLockOrder:
    """RQ-2's lock order, obeyed by both units of work.

    The campaign's authorisation row is the **first** lock a transaction takes,
    it is the **only** campaign that transaction locks, and a shared holder never
    upgrades to exclusive in place — two holders of `FOR SHARE` both asking for
    `FOR UPDATE` deadlock, so a caller that will write takes the exclusive lock
    at the start.

    The rules live here, once, so that the twin and PostgreSQL cannot disagree
    about which calls are **refused**. They still disagree about who **blocks**
    whom: conflict is the database's, and `tests/test_campaign_db.py` proves it
    there (RQ-2(a)).

    It also holds `transaction_bound`, so that the one bound RQ-8 asks for is
    checked in both units of work and against one source for its default —
    whether the caller reached it through `lock_campaign` or through a store's
    row hold.
    """

    def __init__(self, campaign_lock: CampaignLockSettings | None = None) -> None:
        #: `(campaign_id, mode)` per call, in order — what a test asserts on.
        self.campaign_locks: list[tuple[str, str]] = []
        #: Every bound this transaction has asked for, in order, in the same
        #: spirit: a store that holds a row bounds the transaction first, and
        #: this is how a test says so in either world. Only the FIRST of them is
        #: ever armed — see `transaction_bound`.
        self.transaction_bounds: list[str] = []
        self._locked_anything_else = False
        #: One source for the two bounds. A store that holds a row reads it from
        #: here rather than keeping a second settings object of its own.
        self.campaign_lock = campaign_lock if campaign_lock is not None else CampaignLockSettings()

    def transaction_bound(self, transaction_timeout_s: float | None) -> str:
        """How long this transaction may live, as PostgreSQL spells a duration.

        `None` is the configured default. A caller's own value is **checked**
        rather than passed through, because PostgreSQL spells "no timeout" as
        `0` and its unit here is the millisecond — so `0`, a negative, a NaN or
        a value that rounds down to 0 ms would each switch RQ-8's bound *off*
        through the very parameter that exists to raise it. The ceiling is the
        other half: nothing legitimate holds a campaign's authorisation row for
        ten minutes, and a bound nobody can reach is not a bound.
        """
        if transaction_timeout_s is None:
            bound = self.campaign_lock.transaction_timeout
        else:
            # A bool is an int to Python, so it passes a range check and renders
            # as `'Trues'` — a duration the server refuses in the middle of the
            # transaction the bound was meant to protect.
            if isinstance(transaction_timeout_s, bool) or not (
                TRANSACTION_BOUND_MIN_S <= transaction_timeout_s <= TRANSACTION_BOUND_MAX_S
            ):
                raise ValueError(
                    f"a transaction bound is from {TRANSACTION_BOUND_MIN_S} to "
                    f"{TRANSACTION_BOUND_MAX_S} seconds"
                )
            bound = f"{transaction_timeout_s}s"
        self.transaction_bounds.append(bound)
        return bound

    def note_row_lock(self) -> None:
        """A store that takes an explicit row lock says so here, so that taking
        one before the campaign lock is refused rather than merely discouraged."""
        self._locked_anything_else = True

    def _check_campaign_lock(self, campaign_id: str, *, shared: bool) -> str:
        """The mode to take, or a refusal. Nothing is recorded: the lock is not
        held until the statement that takes it has succeeded."""
        mode = SHARE if shared else EXCLUSIVE
        if not self.campaign_locks:
            if self._locked_anything_else:
                raise CampaignLockOrder("the campaign lock is the first lock a transaction takes")
            return mode
        if any(held != campaign_id for held, _ in self.campaign_locks):
            raise CampaignLockOrder("a transaction locks one campaign, never two")
        if mode == EXCLUSIVE and not self._holds_exclusively(campaign_id):
            raise CampaignLockOrder("a shared campaign lock is never upgraded in place")
        return mode

    def _note_campaign_lock(self, campaign_id: str, mode: str) -> None:
        self.campaign_locks.append((campaign_id, mode))

    def _holds_exclusively(self, campaign_id: str) -> bool:
        return (campaign_id, EXCLUSIVE) in self.campaign_locks

    def _require_exclusive(self, campaign_id: str) -> None:
        if not self._holds_exclusively(campaign_id):
            raise CampaignLockNotHeld(
                "the authorisation revision advances only in a transaction that holds "
                "that campaign's lock exclusively"
            )


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


def _float_setting(
    env: Mapping[str, str], name: str, default: float, low: float, high: float
) -> float:
    """The same, for a duration that may be a fraction of a second. A NaN parses
    and then fails the comparison, which is the answer it deserves."""
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        raise ValueError(f"{name} must be a number from {low} to {high}")
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


#: The floor and the ceiling on a `transaction_timeout_s` a caller passes.
#: PostgreSQL measures this GUC in milliseconds, so anything below one of them
#: is rounded to zero, which is how the server spells "no timeout at all".
TRANSACTION_BOUND_MIN_S = 0.001
TRANSACTION_BOUND_MAX_S = 600

#: The floor and the ceiling on how long a caller may wait for a campaign lock.
#: Below 50 ms nothing contended could ever be acquired; above 4 s the wait
#: outlasts the documented gate's own default.
LOCK_TIMEOUT_MIN_S = 0.05
LOCK_TIMEOUT_MAX_S = 4
#: RQ-8's suggested wait, and the most the derived default will ever be.
LOCK_TIMEOUT_SUGGESTED_S = 2.0


def default_lock_timeout_s(acquire_timeout_s: int) -> float:
    """The campaign lock timeout for a caller that named none: RQ-8's two
    seconds, or half the gate when the gate is short.

    Derived rather than fixed because the two bounds are not independent — a
    request waiting for the campaign lock is holding one of the gate's
    connections — and because `DB_POOL_TIMEOUT_S` is documented as 1 to 60 and
    is the one of the two an operator actually sets. A fixed two seconds made
    `DB_POOL_TIMEOUT_S=1` and `=2` refuse to start the service, with no
    environment variable that could have fixed it (G-4). Half, so that the wait
    is strictly below the gate for every value in that range.
    """
    return min(LOCK_TIMEOUT_SUGGESTED_S, acquire_timeout_s / 2)


@dataclass(frozen=True)
class CampaignLockSettings:
    """How long a transaction may wait for the campaign lock, and how long it may
    then live (1kg.2.1, RQ-8 of the shared eligibility decision).

    **Why both are bounded.** No network or model call is made while the campaign's
    authorisation row is held, so a holder that lives for minutes is a bug rather
    than a slow caller — and every display of that campaign waits behind it.

    **Why the lock timeout must be under the gate's.** A request blocked on the
    lock is holding one of its instance's `DB_POOL_MAX` connections, so a lock wait
    that outlasts `DB_POOL_TIMEOUT_S` turns one contended campaign into a 503 for
    unrelated traffic. It is held **both ways**: a timeout nobody set is derived
    from the gate (`default_lock_timeout_s`) and is below it by construction; one
    somebody set is checked against whatever the gate is *set* to, and refused by
    name at construction. That is why `DB_POOL_TIMEOUT_S=1`, documented as valid,
    still starts the service.

    **Why `transaction_timeout` and not `statement_timeout`.** RQ-8 wants a bound
    on the transaction; `statement_timeout` bounds one statement, so a transaction
    made of many short statements escapes it. `transaction_timeout` is PostgreSQL
    17, which is what CI runs and what the deployment targets.
    """

    #: `CAMPAIGN_LOCK_TIMEOUT_S`, RQ-8's *suggested* 2 s — and a fraction of a
    #: second is legal, because it is the only thing a gate of 1 s leaves room
    #: for. A caller that names none gets `default_lock_timeout_s` of its gate.
    lock_timeout_s: float = LOCK_TIMEOUT_SUGGESTED_S
    #: `CAMPAIGN_TRANSACTION_TIMEOUT_S`, RQ-8's *suggested* 5 s. A long caller — a
    #: campaign deletion, an enforcement scan, a type migration — passes its own
    #: bound to `lock_campaign` instead of raising this for everyone.
    transaction_timeout_s: int = 5

    def __post_init__(self) -> None:
        """The wait is a duration, wherever the object was built — from the
        environment, by a route, or by a test. A bool is not one (it is an `int`
        to Python and would render as 1000 ms), zero is how PostgreSQL spells
        "wait for ever", and a NaN fails the comparison."""
        if isinstance(self.lock_timeout_s, bool) or not (
            LOCK_TIMEOUT_MIN_S <= self.lock_timeout_s <= LOCK_TIMEOUT_MAX_S
        ):
            raise ValueError(
                f"a campaign lock timeout is from {LOCK_TIMEOUT_MIN_S} to "
                f"{LOCK_TIMEOUT_MAX_S} seconds"
            )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        pool: PoolSettings | None = None,
    ) -> CampaignLockSettings:
        source = os.environ if env is None else env
        gate = pool if pool is not None else PoolSettings.from_env(source)
        chosen = source.get("CAMPAIGN_LOCK_TIMEOUT_S")
        settings = cls(
            lock_timeout_s=_float_setting(
                source,
                "CAMPAIGN_LOCK_TIMEOUT_S",
                default_lock_timeout_s(gate.acquire_timeout_s),
                LOCK_TIMEOUT_MIN_S,
                LOCK_TIMEOUT_MAX_S,
            ),
            transaction_timeout_s=_int_setting(
                source, "CAMPAIGN_TRANSACTION_TIMEOUT_S", cls.transaction_timeout_s, 1, 60
            ),
        )
        # Only a value somebody chose can be refused: the derived one is below
        # the gate by construction, and refusing it would refuse a gate the
        # operator's documentation calls valid (G-4).
        if chosen and settings.lock_timeout_s >= gate.acquire_timeout_s:
            raise ValueError(
                "CAMPAIGN_LOCK_TIMEOUT_S must be below DB_POOL_TIMEOUT_S "
                f"(currently {gate.acquire_timeout_s}): a request waiting for the campaign "
                "lock holds one of the gate's connections"
            )
        return settings

    @property
    def lock_timeout(self) -> str:
        """As PostgreSQL spells a duration in `set_config`: whole milliseconds,
        because that is this GUC's own unit and `'0.5s'` is not a duration the
        server will parse. `__post_init__`'s floor keeps the rounding away from
        zero, which the server reads as "wait for ever"."""
        return f"{round(self.lock_timeout_s * 1000)}ms"

    @property
    def transaction_timeout(self) -> str:
        return f"{self.transaction_timeout_s}s"


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

    def note_row_lock(self) -> None:
        """A store about to take an explicit row lock says so first.

        On the Protocol, not merely on the implementations, because it is how
        `lock_campaign` can be "the first lock a transaction takes" (RQ-2): a
        store that takes a row lock without saying so leaves that rule
        unenforceable, and a third unit of work that did not implement this
        would silently opt out of it.
        """
        ...  # pragma: no cover - structural type

    def lock_campaign(
        self,
        campaign_id: str,
        *,
        shared: bool,
        transaction_timeout_s: float | None = None,
    ) -> None:
        """Hold a campaign's authorisation still for the rest of this transaction
        (RQ-2): `FOR SHARE` to read under it, `FOR UPDATE` to change it.

        It is the first lock the transaction takes and the only campaign it takes
        one on. It bounds both the wait and the transaction, raises
        `CampaignAuthzMissing` if the campaign has no authorisation row, and
        relies on READ COMMITTED, which `Database.transaction()` sets explicitly.
        """
        ...  # pragma: no cover - structural type

    def advance_authz_revision(self, campaign_id: str) -> int:
        """Increment and return the campaign's authorisation revision — the only
        code that writes it. Refused unless this transaction holds that
        campaign's lock exclusively."""
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


class PgTransaction(_CampaignLockOrder):
    """A unit of work on one connection, inside one transaction."""

    def __init__(self, conn: Any, *, campaign_lock: CampaignLockSettings | None = None) -> None:
        # justification: psycopg's Connection is generic over its row factory and
        # ships partial stubs; the repository's existing pattern for it is `Any`.
        super().__init__(campaign_lock)
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
        self.note_row_lock()
        self.conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (int(lock_class), advisory_key(key)))

    def lock_campaign(
        self,
        campaign_id: str,
        *,
        shared: bool,
        transaction_timeout_s: float | None = None,
    ) -> None:
        mode = self._check_campaign_lock(campaign_id, shared=shared)
        bound = self.transaction_bound(transaction_timeout_s)
        # Both bounds are local to this transaction (set_config's third argument)
        # and are set BEFORE the lock is taken, so a wait ends as a lock timeout
        # rather than holding one of the gate's connections until the client
        # gives up. transaction_timeout is PostgreSQL 17: RQ-8 bounds the
        # transaction, and a statement bound does not (many short statements).
        self.conn.execute(
            "SELECT set_config('lock_timeout', %s, true), set_config('transaction_timeout', %s, true)",
            (self.campaign_lock.lock_timeout, bound),
        )
        held = self.conn.execute(
            "SELECT campaign_id FROM campaign.authz_state WHERE campaign_id = %s "
            + ("FOR SHARE" if shared else "FOR UPDATE"),
            (campaign_id,),
        ).fetchone()
        if held is None:
            raise CampaignAuthzMissing("that campaign has no authorisation row")
        self._note_campaign_lock(campaign_id, mode)

    def advance_authz_revision(self, campaign_id: str) -> int:
        self._require_exclusive(campaign_id)
        advanced = self.conn.execute(
            "UPDATE campaign.authz_state SET authz_revision = authz_revision + 1 "
            "WHERE campaign_id = %s RETURNING authz_revision",
            (campaign_id,),
        ).fetchone()
        if advanced is None:
            raise CampaignAuthzMissing("that campaign has no authorisation row")
        return int(advanced[0])


class TransactionalDatabase(Protocol):
    def transaction(self) -> AbstractContextManager[UnitOfWork]: ...  # pragma: no cover - structural type


class Database:
    """The service's Postgres: a gate for routes, a pool for the realtime path,
    and the transaction boundary."""

    def __init__(
        self,
        dsn: str | None = None,
        settings: PoolSettings | None = None,
        campaign_lock: CampaignLockSettings | None = None,
    ) -> None:
        self._dsn = check_dsn("DATABASE_URL", dsn or default_dsn())
        self.settings = settings if settings is not None else PoolSettings()
        # Defaults, like `settings` above: the caller that first takes a campaign
        # lock on a route passes `CampaignLockSettings.from_env(pool=...)` here,
        # the way `service/app.py` already passes `PoolSettings.from_env()`.
        # Nothing in this bead takes one — the routes are 1kg.2.2's and 1kg.2.3's.
        self.campaign_lock = (
            campaign_lock
            if campaign_lock is not None
            else CampaignLockSettings(
                lock_timeout_s=default_lock_timeout_s(self.settings.acquire_timeout_s)
            )
        )
        # ... which is exactly why the check lives here and not only in
        # `from_env`: this object has both settings, and a `Database` built in
        # code — a test, a script, a route that passes its own `PoolSettings` —
        # would otherwise take a lock timeout unrelated to the gate it was given,
        # and the invariant would depend on the call site. For a lock timeout
        # this constructor derived the check can never fire; for one the caller
        # passed it is the whole guarantee.
        if self.campaign_lock.lock_timeout_s >= self.settings.acquire_timeout_s:
            raise ValueError(
                "the campaign lock timeout must be below the gate's acquire timeout "
                f"(currently {self.settings.acquire_timeout_s} s): a request waiting for "
                "the campaign lock holds one of the gate's connections"
            )
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
            # RQ-2's reasoning about what a lock holds still is READ COMMITTED's,
            # so the level is stated here rather than left to a server, database
            # or role default that an operator could change without knowing. Set
            # before the first statement: psycopg begins the transaction lazily,
            # and the level may not be changed once one is open.
            conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
            unit = PgTransaction(conn, campaign_lock=self.campaign_lock)
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


class InMemoryTransaction(_CampaignLockOrder):
    """The unit of work of `InMemoryDatabase`. A fake store either changes its
    state at once and registers how to take the change back (`on_rollback`), or
    stages what other readers must not see yet and registers how to make it
    visible (`on_publish`)."""

    def __init__(
        self,
        authz_state: dict[str, int] | None = None,
        *,
        campaign_lock: CampaignLockSettings | None = None,
    ) -> None:
        super().__init__(campaign_lock)
        # The COMMITTED `campaign.authz_state` table, as the twin holds it:
        # campaign id to authorisation revision. A transaction made without one
        # locks no campaign, which fails closed rather than silently succeeding.
        self._authz_state = {} if authz_state is None else authz_state
        # Rows this transaction has created and nobody else may see yet — the
        # AFTER INSERT trigger's output before the insert that caused it commits.
        self._authz_staged: dict[str, int] = {}
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
        self.note_row_lock()
        self.locks.append((lock_class, key))

    def _stage_authz(self, campaign_id: str, revision: int) -> None:
        """Hold a campaign's revision for this transaction alone, and publish it
        on commit at whatever value it has reached by then.

        Every change to `authz_state` goes through here — the row's creation and
        every advance of it — because a second reader's unlocked `SELECT` in
        PostgreSQL reads the committed value under READ COMMITTED, so the twin
        must not show it a number no other transaction could see. A rollback
        needs no undo: the staged dictionary dies with the transaction, and the
        committed one was never touched.
        """
        if campaign_id not in self._authz_staged:
            self.on_publish(
                lambda: self._authz_state.__setitem__(campaign_id, self._authz_staged[campaign_id])
            )
        self._authz_staged[campaign_id] = revision

    def create_authz_state(self, campaign_id: str) -> None:
        """What PostgreSQL's AFTER INSERT trigger does, for the twin (RQ-1).

        The row exists for **this** transaction at once — the trigger fires
        inside the insert's own transaction — and for every other reader only
        when this one commits. A fake campaign store calls it from `create`; it
        is the twin's only way to make one, which is why the case a raw SQL
        insert covers is proved against the database instead.
        """
        self._stage_authz(campaign_id, 0)

    def authz_revision(self, campaign_id: str) -> int | None:
        """The revision this transaction can see: committed, plus its own."""
        if campaign_id in self._authz_staged:
            return self._authz_staged[campaign_id]
        return self._authz_state.get(campaign_id)

    def lock_campaign(
        self,
        campaign_id: str,
        *,
        shared: bool,
        transaction_timeout_s: float | None = None,
    ) -> None:
        """The order rules, the fail-closed refusal and the bound's validity,
        and nothing more: every in-memory transaction is already serial, so the
        twin has no conflict to model and makes no claim about one (RQ-2(a)).
        The bound itself is PostgreSQL's; what is checked here is that the value
        the caller passed would be a bound at all, because a test that can only
        run against the twin must still catch a caller that switches it off."""
        mode = self._check_campaign_lock(campaign_id, shared=shared)
        self.transaction_bound(transaction_timeout_s)
        if self.authz_revision(campaign_id) is None:
            raise CampaignAuthzMissing("that campaign has no authorisation row")
        self._note_campaign_lock(campaign_id, mode)

    def advance_authz_revision(self, campaign_id: str) -> int:
        self._require_exclusive(campaign_id)
        current = self.authz_revision(campaign_id)
        if current is None:
            raise CampaignAuthzMissing("that campaign has no authorisation row")
        self._stage_authz(campaign_id, current + 1)
        return current + 1


class InMemoryDatabase:
    """Transactions for fakes: serial, all-or-nothing, with commit-time release
    of published rows, notifications and callbacks — the observable contract of
    `Database`."""

    def __init__(self, campaign_lock: CampaignLockSettings | None = None) -> None:
        self._lock = threading.RLock()
        self.campaign_lock = campaign_lock
        #: Every notification of every committed transaction, in order.
        self.notifications: list[tuple[str, str]] = []
        #: `campaign.authz_state` as committed, which is what a transaction
        #: other than the writer's can see. A fake campaign store's `create`
        #: stages a campaign at revision 0 — standing in for the AFTER INSERT
        #: trigger PostgreSQL has — and the commit publishes it here.
        self.authz_state: dict[str, int] = {}
        #: The fakes' tables, by name, reached through
        #: `service/campaign_store.shared_rows`. One state for every twin on this
        #: database, so that a fake refuses a row whose parent is not there —
        #: which is the thing a foreign key does and a per-store dictionary
        #: cannot. justification: the value is a `Staging[T]` of a row type this
        #: module must not import (the stores import `db`, never the reverse).
        self.tables: dict[str, Any] = {}

    @contextmanager
    def transaction(self) -> Iterator[InMemoryTransaction]:
        unit = InMemoryTransaction(self.authz_state, campaign_lock=self.campaign_lock)
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

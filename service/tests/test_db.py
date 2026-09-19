"""Bounded pools and the transaction boundary (1kg.1.5) — without a database.

The settings and their bounds, the in-memory twin's all-or-nothing contract, and
the Postgres unit of work against a scripted connection. What needs a server —
that the pool really refuses a connection too many, replaces a dead one, and
closes cleanly — is in `tests/test_db_postgres.py`, which CI runs.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager

import psycopg
import pytest
from psycopg_pool import PoolClosed

from service import db as dbmod
from service.db import (
    AdvisoryLock,
    Database,
    InMemoryDatabase,
    PoolSettings,
    advisory_key,
)

# ── Settings ─────────────────────────────────────────────────────────────────


def test_the_defaults_are_the_reviewed_numbers():
    """Six for routes, three for the realtime path, one listener (F-3, RT-5)."""
    settings = PoolSettings.from_env({})
    assert (settings.sync_max, settings.async_max, settings.per_instance) == (6, 3, 10)
    assert settings.acquire_timeout_s == 5


def test_settings_come_from_the_environment():
    settings = PoolSettings.from_env({"DB_POOL_MAX": "4", "DB_ASYNC_POOL_MAX": "0", "DB_POOL_TIMEOUT_S": "9"})
    assert (settings.sync_max, settings.async_max, settings.acquire_timeout_s) == (4, 0, 9)
    assert settings.per_instance == 5
    assert PoolSettings.from_env({"DB_POOL_MAX": ""}).sync_max == 6


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_POOL_MAX", "600"),
        ("DB_POOL_MAX", "-1"),
        ("DB_POOL_MAX", "six"),
        ("DB_ASYNC_POOL_MAX", "6"),
        ("DB_POOL_TIMEOUT_S", "0"),
        ("DB_POOL_TIMEOUT_S", "2.5"),
    ],
)
def test_a_setting_out_of_bounds_is_refused_by_name(name, value):
    """A typo must not become six hundred connections against a server that
    accepts twenty-two."""
    with pytest.raises(ValueError, match=f"{name} must be a whole number from"):
        PoolSettings.from_env({name: value})


def test_an_advisory_key_is_a_stable_signed_32_bit_number():
    assert advisory_key("session-1") == advisory_key("session-1")
    assert advisory_key("session-1") != advisory_key("session-2")
    for key in ("", "a", "session-1", "ü" * 50):
        assert -(2**31) <= advisory_key(key) < 2**31


def test_the_default_dsn_treats_an_empty_variable_as_unset(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    assert dbmod.default_dsn() == dbmod.DEFAULT_DSN
    monkeypatch.setenv("DATABASE_URL", "postgresql://elsewhere/app")
    assert dbmod.default_dsn() == "postgresql://elsewhere/app"


# ── The in-memory twin ───────────────────────────────────────────────────────


def test_a_committed_block_keeps_its_changes_and_then_runs_its_callbacks():
    db = InMemoryDatabase()
    state: list[str] = []
    with db.transaction() as unit:
        state.append("row")
        unit.on_rollback(state.pop)
        unit.on_commit(lambda: state.append("after-commit"))
        unit.notify("table_session", "s-1")
        assert state == ["row"], "a callback must not run inside the transaction"
        assert db.notifications == [], "a notification is released by the commit"
    assert state == ["row", "after-commit"]
    assert db.notifications == [("table_session", "s-1")]


def test_a_failed_block_takes_everything_back_in_reverse_order():
    db = InMemoryDatabase()
    state: list[str] = []
    undone: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        with db.transaction() as unit:
            for name in ("aggregate", "job"):
                state.append(name)
                unit.on_rollback(lambda name=name: (state.remove(name), undone.append(name)))
            unit.on_commit(lambda: state.append("after-commit"))
            unit.notify("table_session", "s-1")
            raise RuntimeError("boom")
    assert state == [] and undone == ["job", "aggregate"]
    assert db.notifications == []


def test_a_callback_that_fails_cannot_undo_a_commit(caplog):
    db = InMemoryDatabase()
    ran: list[str] = []

    def broken() -> None:
        raise LookupError("a private brief must not be logged")

    with caplog.at_level(logging.WARNING, logger="service.db"):
        with db.transaction() as unit:
            unit.on_commit(broken)
            unit.on_commit(lambda: ran.append("second"))
    assert ran == ["second"]
    assert "after-commit callback failed (LookupError)" in caplog.text
    assert "private brief" not in caplog.text


@pytest.mark.parametrize("channel", ["", "Table", "table session", "1table", "t" * 64, "table;drop"])
def test_a_channel_is_a_lowercase_identifier(channel):
    with pytest.raises(ValueError, match="lowercase identifier"):
        with InMemoryDatabase().transaction() as unit:
            unit.notify(channel)


def test_a_notification_carries_an_id_not_content():
    with pytest.raises(ValueError, match="an id, not content"):
        with InMemoryDatabase().transaction() as unit:
            unit.notify("table_session", "x" * 201)


def test_in_memory_transactions_are_serial():
    """The strongest reading of any lock a repository asks for."""
    db = InMemoryDatabase()
    order: list[str] = []
    inside = threading.Event()
    release = threading.Event()

    def first() -> None:
        with db.transaction() as unit:
            unit.lock(AdvisoryLock.TABLE_SESSION, "s-1")
            assert unit.locks == [(AdvisoryLock.TABLE_SESSION, "s-1")]
            order.append("first-in")
            inside.set()
            release.wait(5)
            order.append("first-out")

    def second() -> None:
        inside.wait(5)
        with db.transaction():
            order.append("second-in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    inside.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)
    assert order == ["first-in", "first-out", "second-in"]


# ── The Postgres unit of work, against a scripted connection ─────────────────


class _Connection:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def __enter__(self):
        self.log.append("connect")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.log.append("rollback+close" if exc_type else "commit+close")
        return False

    @contextmanager
    def transaction(self):
        self.log.append("begin")
        try:
            yield
        except BaseException:
            self.log.append("rollback")
            raise
        self.log.append("commit")

    def execute(self, sql: str, params=None):
        self.log.append(f"{sql} {params}")


@pytest.fixture
def unpooled(monkeypatch):
    """`DB_POOL_MAX=0`: a connection per operation, as before 1kg.1.5."""
    log: list[str] = []
    seen: dict[str, object] = {}

    def connect(dsn, **kwargs):
        seen["dsn"], seen["kwargs"] = dsn, kwargs
        return _Connection(log)

    monkeypatch.setattr(psycopg, "connect", connect)
    return Database("postgresql://test/db", PoolSettings(sync_max=0, async_max=0)), log, seen


def test_without_a_pool_every_operation_opens_and_closes_its_own_connection(unpooled):
    db, log, seen = unpooled
    with db.connection() as conn:
        conn.execute("SELECT 1")
    assert log == ["connect", "SELECT 1 None", "commit+close"]
    assert seen["dsn"] == "postgresql://test/db"
    assert seen["kwargs"] == {"connect_timeout": 10, "application_name": "game-guide-ai:direct"}
    db.open()
    db.close()  # nothing to open, nothing to close, no error


def test_the_unit_of_work_commits_then_releases_then_calls_back(unpooled):
    db, log, _ = unpooled
    with db.transaction() as unit:
        unit.lock(AdvisoryLock.TABLE_SESSION, "s-1")
        unit.notify("table_session", "s-1")
        unit.on_commit(lambda: log.append("after-commit"))
    assert log == [
        "connect",
        "begin",
        f"SELECT pg_advisory_xact_lock(%s, %s) (2, {advisory_key('s-1')})",
        "SELECT pg_notify(%s, %s) ('table_session', 's-1')",
        "commit",
        "commit+close",
        "after-commit",  # after the connection is back: a callback may need one itself
    ]


def test_a_failed_unit_of_work_rolls_back_and_never_calls_back(unpooled):
    db, log, _ = unpooled
    with pytest.raises(RuntimeError):
        with db.transaction() as unit:
            unit.on_commit(lambda: log.append("after-commit"))
            raise RuntimeError("boom")
    assert log == ["connect", "begin", "rollback", "rollback+close"]


def test_the_postgres_unit_of_work_checks_notifications_too(unpooled):
    db, log, _ = unpooled
    with pytest.raises(ValueError, match="lowercase identifier"):
        with db.transaction() as unit:
            unit.notify("Table Session")
    assert not any("pg_notify" in line for line in log)


# ── The pools themselves (no server: neither keeps an idle minimum) ──────────


def test_the_pools_are_bounded_and_start_empty():
    db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=4, async_max=2))
    assert (db._pool.min_size, db._pool.max_size) == (0, 4)
    assert (db._async_pool.min_size, db._async_pool.max_size) == (0, 2)
    assert db._pool.timeout == 5 and db._pool.max_idle == 60
    assert db._pool.closed and db._async_pool.closed


def test_opening_connects_to_nothing_and_closing_is_clean_and_idempotent():
    """With no idle minimum, `open()` cannot fail because the database is down —
    startup does not depend on it — and `close()` leaves a pool that refuses."""
    db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=2, async_max=0))
    db.open()
    assert not db._pool.closed and db._pool.get_stats().get("pool_size", 0) == 0
    db.close()
    db.close()
    assert db._pool.closed
    with pytest.raises(PoolClosed):
        with db.connection():
            pass  # pragma: no cover


def test_the_async_pool_opens_on_first_use_and_closes_with_the_rest():
    async def scenario() -> tuple[bool, bool]:
        db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=1, async_max=1))
        db.open()
        assert db._async_pool.closed, "nothing realtime has asked for it yet"
        await db._async_pool.open(wait=False)
        opened = not db._async_pool.closed
        await db.aclose()
        return opened, db._async_pool.closed and db._pool.closed

    assert asyncio.run(scenario()) == (True, True)


def test_a_disabled_async_pool_says_so():
    async def scenario() -> None:
        db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=1, async_max=0))
        async with db.async_connection():
            pass  # pragma: no cover
        await db.aclose()  # pragma: no cover

    with pytest.raises(RuntimeError, match="DB_ASYNC_POOL_MAX=0"):
        asyncio.run(scenario())


# ── A pool that sat idle is swept before it lends again ──────────────────────


class _SweepablePool:
    closed = False

    def __init__(self) -> None:
        self.sweeps = 0

    def check(self) -> None:
        self.sweeps += 1

    @contextmanager
    def connection(self):
        yield "conn"


def test_an_idle_pool_is_swept_once_and_a_busy_one_never():
    """Cloud Run freezes an instance between requests and its sockets die quietly.
    Met one at a time at checkout, each costs a growing backoff; swept first,
    they cost one new connection."""
    db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=2, async_max=0))
    pool = db._pool = _SweepablePool()
    with db.connection():
        pass
    assert pool.sweeps == 0, "borrowed from a moment ago: nothing to suspect"

    db._last_borrowed["sync"] -= dbmod.IDLE_SUSPECT_S + 1
    with db.connection():
        pass
    with db.connection():
        pass
    assert pool.sweeps == 1


def test_the_async_pool_is_swept_the_same_way():
    class _AsyncPool:
        closed = False
        sweeps = 0

        async def check(self) -> None:
            type(self).sweeps += 1

        def connection(self):
            class _Borrowed:
                async def __aenter__(self):
                    return "conn"

                async def __aexit__(self, *exc):
                    return False

            return _Borrowed()

        async def close(self) -> None:
            return None

    async def scenario() -> int:
        db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=0, async_max=1))
        db._async_pool = _AsyncPool()
        async with db.async_connection():
            pass
        db._last_borrowed["async"] -= dbmod.IDLE_SUSPECT_S + 1
        async with db.async_connection():
            pass
        async with db.async_connection():
            pass
        await db.aclose()
        return _AsyncPool.sweeps

    assert asyncio.run(scenario()) == 1

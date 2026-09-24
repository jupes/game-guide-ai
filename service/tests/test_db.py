"""Bounded pools and the transaction boundary (1kg.1.5) — without a database.

The settings and their bounds, the in-memory twin's all-or-nothing contract, the
gate, and the Postgres unit of work against a scripted connection. What needs a
server — that the gate really refuses a connection too many, that nothing is held
between operations, that the realtime pool closes cleanly — is in
`tests/test_db_postgres.py`, which CI runs.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager

import psycopg
import pytest
from psycopg_pool import PoolClosed, PoolTimeout

from service import db as dbmod
from service.db import (
    AdvisoryLock,
    Database,
    InMemoryDatabase,
    PoolSettings,
    advisory_key,
)

# ── Settings ─────────────────────────────────────────────────────────────────


def test_the_defaults_fit_a_rollout():
    """Four for routes — two revisions of two instances overlap during a rollout —
    three for the realtime path, one listener (F-3, RT-5)."""
    settings = PoolSettings.from_env({})
    assert (settings.sync_max, settings.async_max, settings.per_instance) == (4, 3, 8)
    assert (settings.acquire_timeout_s, settings.max_idle_s, settings.idle_session_timeout_s) == (5, 60, 120)


def test_settings_come_from_the_environment():
    settings = PoolSettings.from_env({"DB_POOL_MAX": "4", "DB_ASYNC_POOL_MAX": "0", "DB_POOL_TIMEOUT_S": "9"})
    assert (settings.sync_max, settings.async_max, settings.acquire_timeout_s) == (4, 0, 9)
    assert settings.per_instance == 5
    assert PoolSettings.from_env({"DB_POOL_MAX": ""}).sync_max == 4


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
def scripted(monkeypatch):
    """psycopg.connect replaced by a recorder: what the gate does around it."""
    log: list[str] = []
    seen: dict[str, object] = {}

    def connect(dsn, **kwargs):
        seen["dsn"], seen["kwargs"] = dsn, kwargs
        return _Connection(log)

    monkeypatch.setattr(psycopg, "connect", connect)
    return log, seen


def test_every_operation_opens_and_closes_a_connection_of_its_own(scripted):
    """Nothing is kept between operations, so an idle or frozen instance holds
    nothing and no connection is ever stale."""
    log, seen = scripted
    db = Database("postgresql://test/db", PoolSettings(sync_max=2, async_max=0))
    with db.connection() as conn:
        conn.execute("SELECT 1")
    assert log == ["connect", "SELECT 1 None", "commit+close"]
    assert seen["dsn"] == "postgresql://test/db"
    assert seen["kwargs"] == {"connect_timeout": 10, "application_name": "game-guide-ai:sync"}


def test_the_gate_admits_only_its_number_and_frees_a_place_on_the_way_out(scripted):
    db = Database("postgresql://test/db", PoolSettings(sync_max=2, async_max=0, acquire_timeout_s=1))
    with db.connection(), db.connection():
        with pytest.raises(PoolTimeout, match="no database connection came free in 1 s"):
            with db.connection():
                pass  # pragma: no cover
    with db.connection():
        pass  # both places are free again


def test_a_failed_operation_gives_its_place_back(scripted):
    db = Database("postgresql://test/db", PoolSettings(sync_max=1, async_max=0, acquire_timeout_s=1))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            with db.connection():
                raise RuntimeError("boom")
    with db.connection():
        pass


def test_a_connection_that_cannot_be_made_gives_its_place_back(monkeypatch):
    def refuse(dsn, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)
    db = Database("postgresql://test/db", PoolSettings(sync_max=1, async_max=0, acquire_timeout_s=1))
    for _ in range(3):
        with pytest.raises(psycopg.OperationalError, match="connection refused"):
            with db.connection():
                pass  # pragma: no cover


def test_a_gate_timeout_is_the_kind_of_error_routes_already_answer_503_for():
    assert issubclass(PoolTimeout, psycopg.OperationalError)
    assert issubclass(PoolClosed, psycopg.OperationalError)


def test_without_a_gate_connections_are_unbounded_as_before(scripted):
    """`DB_POOL_MAX=0`: the behaviour before 1kg.1.5, kept as a switch."""
    db = Database("postgresql://test/db", PoolSettings(sync_max=0, async_max=0))
    with db.connection(), db.connection(), db.connection(), db.connection(), db.connection():
        pass


def test_a_closed_database_refuses_and_closing_twice_is_fine(scripted):
    db = Database("postgresql://test/db", PoolSettings(sync_max=1, async_max=0))
    db.close()
    db.close()
    with pytest.raises(PoolClosed):
        with db.connection():
            pass  # pragma: no cover


def test_the_unit_of_work_commits_then_releases_then_calls_back(scripted):
    log, _ = scripted
    db = Database("postgresql://test/db", PoolSettings(sync_max=1, async_max=0, acquire_timeout_s=1))

    def after_commit() -> None:
        log.append("after-commit")
        with db.connection():  # the only place is free again: a callback may use the database
            pass

    with db.transaction() as unit:
        unit.lock(AdvisoryLock.TABLE_SESSION, "s-1")
        unit.notify("table_session", "s-1")
        unit.on_commit(after_commit)
    assert log == [
        "connect",
        "begin",
        f"SELECT pg_advisory_xact_lock(%s, %s) (2, {advisory_key('s-1')})",
        "SELECT pg_notify(%s, %s) ('table_session', 's-1')",
        "commit",
        "commit+close",
        "after-commit",
        "connect",
        "commit+close",
    ]


def test_a_failed_unit_of_work_rolls_back_and_never_calls_back(scripted):
    log, _ = scripted
    db = Database("postgresql://test/db", PoolSettings(sync_max=1, async_max=0))
    with pytest.raises(RuntimeError):
        with db.transaction() as unit:
            unit.on_commit(lambda: log.append("after-commit"))
            raise RuntimeError("boom")
    assert log == ["connect", "begin", "rollback", "rollback+close"]


def test_the_postgres_unit_of_work_checks_notifications_too(scripted):
    log, _ = scripted
    db = Database("postgresql://test/db", PoolSettings(sync_max=1, async_max=0))
    with pytest.raises(ValueError, match="lowercase identifier"):
        with db.transaction() as unit:
            unit.notify("Table Session")
    assert not any("pg_notify" in line for line in log)


# ── A connection string that does not parse is refused without being repeated ─


@pytest.mark.parametrize(
    "dsn",
    [" postgresql://postgres:S3cretPW@/app?host=/cloudsql/p:r:i", '"postgresql://u:S3cretPW@h/app"'],
)
def test_a_malformed_dsn_is_a_verdict_that_never_quotes_the_dsn(dsn):
    """psycopg quotes the whole string, password included, when it cannot parse
    one — and a pool would log that on every retry (SEC-21)."""
    with pytest.raises(ValueError) as caught:
        Database(dsn)
    assert str(caught.value) == "DATABASE_URL is not a valid PostgreSQL connection string"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert dbmod.check_dsn("X", "postgresql://u:pw@h/app") == "postgresql://u:pw@h/app"


# ── The realtime pool (no server: it keeps no idle minimum) ──────────────────


def test_the_realtime_pool_is_bounded_starts_empty_and_asks_the_server_to_reap_it():
    db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=4, async_max=2))
    pool = db._async_pool
    assert (pool.min_size, pool.max_size, pool.timeout, pool.max_idle) == (0, 2, 5, 60)
    assert pool.closed, "nothing realtime has asked for it yet"
    assert pool.kwargs["options"] == "-c idle_session_timeout=120s", (
        "a frozen instance cannot shed its own connections; the server must"
    )
    assert pool.kwargs["application_name"] == "game-guide-ai:async"


class _StubAsyncPool:
    def __init__(self) -> None:
        self.closed = True
        self.opened = 0

    async def open(self, wait: bool = False) -> None:
        self.opened += 1
        self.closed = False

    def connection(self):
        class _Borrowed:
            async def __aenter__(self):
                return "conn"

            async def __aexit__(self, *exc):
                return False

        return _Borrowed()

    async def close(self) -> None:
        self.closed = True


def test_the_realtime_pool_opens_on_first_use_and_closes_with_the_rest():
    async def scenario() -> tuple[int, bool, bool]:
        db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=1, async_max=1))
        pool = db._async_pool = _StubAsyncPool()
        async with db.async_connection() as conn:
            assert conn == "conn"
        async with db.async_connection():
            pass
        await db.aclose()
        return pool.opened, pool.closed, db._closed

    assert asyncio.run(scenario()) == (1, True, True)


def test_a_real_realtime_pool_opens_without_a_server_and_closes():
    async def scenario() -> tuple[bool, bool]:
        db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=1, async_max=1))
        await db._async_pool.open(wait=False)
        opened = not db._async_pool.closed
        await db.aclose()
        return opened, db._async_pool.closed

    assert asyncio.run(scenario()) == (True, True)


def test_a_disabled_async_pool_says_so():
    async def scenario() -> None:
        db = Database("postgresql://nobody@127.0.0.1:1/none", PoolSettings(sync_max=1, async_max=0))
        async with db.async_connection():
            pass  # pragma: no cover

    with pytest.raises(RuntimeError, match="DB_ASYNC_POOL_MAX=0"):
        asyncio.run(scenario())

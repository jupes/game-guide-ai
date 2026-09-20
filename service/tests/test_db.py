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
    CampaignAuthzMissing,
    CampaignLockNotHeld,
    CampaignLockOrder,
    CampaignLockSettings,
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


# ── The campaign lock's bounds (1kg.2.1, RQ-8) ───────────────────────────────


def test_the_campaign_lock_defaults_are_rq8s_suggested_numbers():
    """RQ-8 suggests two seconds to wait for the lock and five as a bound on the
    whole transaction. Both are settings because a campaign deletion or an
    enforcement scan legitimately needs longer than a display does."""
    settings = CampaignLockSettings.from_env({})
    assert (settings.lock_timeout_s, settings.transaction_timeout_s) == (2, 5)


def test_the_campaign_lock_settings_come_from_the_environment():
    settings = CampaignLockSettings.from_env(
        {"CAMPAIGN_LOCK_TIMEOUT_S": "3", "CAMPAIGN_TRANSACTION_TIMEOUT_S": "30"}
    )
    assert (settings.lock_timeout_s, settings.transaction_timeout_s) == (3, 30)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CAMPAIGN_LOCK_TIMEOUT_S", "0"),
        ("CAMPAIGN_LOCK_TIMEOUT_S", "5"),
        ("CAMPAIGN_LOCK_TIMEOUT_S", "two"),
        ("CAMPAIGN_TRANSACTION_TIMEOUT_S", "0"),
        ("CAMPAIGN_TRANSACTION_TIMEOUT_S", "61"),
        ("CAMPAIGN_TRANSACTION_TIMEOUT_S", "2.5"),
    ],
)
def test_a_campaign_lock_setting_out_of_bounds_is_refused_by_name(name, value):
    with pytest.raises(ValueError, match=f"{name} must be a whole number from"):
        CampaignLockSettings.from_env({name: value})


def test_the_lock_timeout_must_be_below_the_gates_acquire_timeout():
    """RQ-8: "the lock timeout always below the gate's acquire timeout". A request
    waiting for the campaign lock is holding one of the gate's four connections,
    so a lock wait that outlasts the gate's own timeout turns one slow campaign
    into a 503 for unrelated traffic.

    Checked against whatever `DB_POOL_TIMEOUT_S` is set to — not against its
    default of five — because the gate's bound moves anywhere in 1..60.
    """
    # Default gate (5 s): 4 is allowed, and nothing above it can be reached
    # anyway because CAMPAIGN_LOCK_TIMEOUT_S is itself bounded at 4.
    assert CampaignLockSettings.from_env({"CAMPAIGN_LOCK_TIMEOUT_S": "4"}).lock_timeout_s == 4

    # A narrowed gate makes the same value illegal.
    with pytest.raises(ValueError, match="CAMPAIGN_LOCK_TIMEOUT_S must be below DB_POOL_TIMEOUT_S"):
        CampaignLockSettings.from_env({"CAMPAIGN_LOCK_TIMEOUT_S": "4", "DB_POOL_TIMEOUT_S": "4"})
    with pytest.raises(ValueError, match="CAMPAIGN_LOCK_TIMEOUT_S must be below DB_POOL_TIMEOUT_S"):
        CampaignLockSettings.from_env({"DB_POOL_TIMEOUT_S": "2"})


def test_a_gate_of_one_second_refuses_every_valid_lock_timeout():
    """The bounds' own consequence, asserted so it is a documented refusal rather
    than a surprise: CAMPAIGN_LOCK_TIMEOUT_S is bounded at 1..4, so a gate of one
    second leaves no legal value. It fails closed, at startup, by name."""
    with pytest.raises(ValueError, match="CAMPAIGN_LOCK_TIMEOUT_S must be below DB_POOL_TIMEOUT_S"):
        CampaignLockSettings.from_env({"CAMPAIGN_LOCK_TIMEOUT_S": "1", "DB_POOL_TIMEOUT_S": "1"})


def test_the_caller_may_pass_its_own_pool_settings():
    """`Database` already holds a `PoolSettings`; the check reads that object
    rather than re-reading the environment, so a programmatically-built
    `Database` is checked against its own gate."""
    settings = CampaignLockSettings.from_env(
        {"CAMPAIGN_LOCK_TIMEOUT_S": "3"}, pool=PoolSettings(acquire_timeout_s=10)
    )
    assert settings.lock_timeout_s == 3

    with pytest.raises(ValueError, match="must be below DB_POOL_TIMEOUT_S"):
        CampaignLockSettings.from_env(
            {"CAMPAIGN_LOCK_TIMEOUT_S": "3"}, pool=PoolSettings(acquire_timeout_s=3)
        )


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


# ── The campaign lock and the authorisation revision (1kg.2.1, RQ-2) ─────────
#
# What the twin can and cannot show. It records which campaign a transaction
# locked and in which mode, refuses a second campaign, refuses a revision written
# without the exclusive lock, and takes an increment back on rollback. It makes
# no claim about who blocks whom: conflict is the database's, and
# `tests/test_campaign_db.py` proves it there (RQ-2(a)).


def _campaign(db: InMemoryDatabase, campaign_id: str = "cmp_one", revision: int = 0) -> str:
    """The `authz_state` row PostgreSQL's AFTER INSERT trigger writes; here, by hand."""
    db.authz_state[campaign_id] = revision
    return campaign_id


def test_the_twin_records_which_campaign_a_transaction_locked_and_in_which_mode():
    db = InMemoryDatabase()
    campaign = _campaign(db)
    with db.transaction() as unit:
        unit.lock_campaign(campaign, shared=True)
        assert unit.campaign_locks == [(campaign, "share")]
    with db.transaction() as unit:
        unit.lock_campaign(campaign, shared=False)
        assert unit.campaign_locks == [(campaign, "exclusive")]


def test_locking_a_campaign_that_has_no_authorisation_row_fails_closed():
    """RQ-2(c). A caller maps this to the generic 404; a job makes it a no-op."""
    with pytest.raises(CampaignAuthzMissing, match="authorisation"):
        with InMemoryDatabase().transaction() as unit:
            unit.lock_campaign("cmp_gone", shared=True)


def test_one_transaction_may_not_lock_two_different_campaigns():
    db = InMemoryDatabase()
    first, second = _campaign(db, "cmp_one"), _campaign(db, "cmp_two")
    with pytest.raises(CampaignLockOrder, match="one campaign"):
        with db.transaction() as unit:
            unit.lock_campaign(first, shared=False)
            unit.lock_campaign(second, shared=False)


def test_the_campaign_lock_comes_before_any_other_lock_the_transaction_takes():
    db = InMemoryDatabase()
    campaign = _campaign(db)
    with pytest.raises(CampaignLockOrder, match="first lock"):
        with db.transaction() as unit:
            unit.lock(AdvisoryLock.TABLE_SESSION, "ses_one")
            unit.lock_campaign(campaign, shared=True)


def test_a_shared_campaign_lock_is_never_upgraded_in_place():
    """Two holders of FOR SHARE both asking for FOR UPDATE deadlock. A caller
    that will write takes the exclusive lock at the start."""
    db = InMemoryDatabase()
    campaign = _campaign(db)
    with pytest.raises(CampaignLockOrder, match="upgrade"):
        with db.transaction() as unit:
            unit.lock_campaign(campaign, shared=True)
            unit.lock_campaign(campaign, shared=False)


def test_re_locking_the_same_campaign_in_the_same_mode_is_allowed():
    db = InMemoryDatabase()
    campaign = _campaign(db)
    with db.transaction() as unit:
        unit.lock_campaign(campaign, shared=False)
        unit.lock_campaign(campaign, shared=False)
        assert unit.campaign_locks == [(campaign, "exclusive"), (campaign, "exclusive")]


@pytest.mark.parametrize(
    "take",
    [
        pytest.param(lambda unit, campaign: None, id="no-lock"),
        pytest.param(lambda unit, campaign: unit.lock_campaign(campaign, shared=True), id="shared"),
    ],
)
def test_advancing_the_revision_without_the_exclusive_lock_is_refused(take):
    db = InMemoryDatabase()
    campaign = _campaign(db)
    with pytest.raises(CampaignLockNotHeld, match="exclusively"):
        with db.transaction() as unit:
            take(unit, campaign)
            unit.advance_authz_revision(campaign)
    assert db.authz_state[campaign] == 0


def test_advancing_another_campaigns_revision_under_this_ones_lock_is_refused():
    db = InMemoryDatabase()
    held, other = _campaign(db, "cmp_one"), _campaign(db, "cmp_two")
    with pytest.raises(CampaignLockNotHeld, match="exclusively"):
        with db.transaction() as unit:
            unit.lock_campaign(held, shared=False)
            unit.advance_authz_revision(other)
    assert db.authz_state[other] == 0


def test_the_revision_advances_under_the_exclusive_lock_and_a_rollback_takes_it_back():
    db = InMemoryDatabase()
    campaign = _campaign(db)
    with db.transaction() as unit:
        assert unit.lock_campaign(campaign, shared=False) is None
        assert unit.advance_authz_revision(campaign) == 1
        assert unit.advance_authz_revision(campaign) == 2
    assert db.authz_state[campaign] == 2

    with pytest.raises(RuntimeError, match="boom"):
        with db.transaction() as unit:
            unit.lock_campaign(campaign, shared=False)
            assert unit.advance_authz_revision(campaign) == 3
            raise RuntimeError("boom")
    assert db.authz_state[campaign] == 2, "a rolled-back increment is taken back"


# ── The Postgres unit of work, against a scripted connection ─────────────────


class _Result:
    """What psycopg's `execute` hands back: one row, or none."""

    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def fetchone(self) -> tuple | None:
        return self._row


class _Connection:
    def __init__(self, log: list[str], rows: list[tuple[str, tuple]] | None = None) -> None:
        self.log = log
        #: `(fragment of the statement, the row it returns)`, primed by a test.
        self.rows = [] if rows is None else rows

    def __setattr__(self, name: str, value: object) -> None:
        if name == "isolation_level":
            self.log.append(f"isolation_level={value.name}")  # type: ignore[attr-defined]
        object.__setattr__(self, name, value)

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
        for fragment, row in self.rows:
            if fragment in sql:
                return _Result(row)
        return _Result(None)


@pytest.fixture
def scripted(monkeypatch):
    """psycopg.connect replaced by a recorder: what the gate does around it."""
    log: list[str] = []
    rows: list[tuple[str, tuple]] = []
    seen: dict[str, object] = {"rows": rows}

    def connect(dsn, **kwargs):
        seen["dsn"], seen["kwargs"] = dsn, kwargs
        seen["conn"] = connection = _Connection(log, rows)
        return connection

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
        "isolation_level=READ_COMMITTED",
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
    assert log == ["connect", "isolation_level=READ_COMMITTED", "begin", "rollback", "rollback+close"]


def test_the_postgres_unit_of_work_checks_notifications_too(scripted):
    log, _ = scripted
    db = Database("postgresql://test/db", PoolSettings(sync_max=1, async_max=0))
    with pytest.raises(ValueError, match="lowercase identifier"):
        with db.transaction() as unit:
            unit.notify("Table Session")
    assert not any("pg_notify" in line for line in log)


# ── The campaign lock, as PostgreSQL is asked for it ─────────────────────────
#
# The statements and their order, against the scripted connection. Whether the
# lock then *conflicts* — who waits for whom, which timeout fires, what the
# server reports its isolation level to be — is `tests/test_campaign_db.py`'s,
# and runs only in CI against a real server.

_SET_BOUNDS = "SELECT set_config('lock_timeout', %s, true), set_config('transaction_timeout', %s, true)"
_AUTHZ_ROW = ("FROM campaign.authz_state", ("cmp_one",))


def _scripted_database() -> Database:
    return Database("postgresql://test/db", PoolSettings(sync_max=1, async_max=0))


def test_a_transaction_sets_read_committed_on_its_connection_before_any_statement(scripted):
    """The mechanism, not the server's answer: RQ-2 reasons about what a lock
    holds still under READ COMMITTED, so the level is stated rather than
    inherited from a server, database or role default."""
    log, _ = scripted
    with _scripted_database().transaction():
        pass
    assert log == ["connect", "isolation_level=READ_COMMITTED", "begin", "commit", "commit+close"]


@pytest.mark.parametrize(
    ("shared", "strength"), [(True, "FOR SHARE"), (False, "FOR UPDATE")]
)
def test_the_campaign_lock_bounds_the_wait_and_the_transaction_before_it_locks(scripted, shared, strength):
    log, seen = scripted
    seen["rows"].append(_AUTHZ_ROW)
    with _scripted_database().transaction() as unit:
        unit.lock_campaign("cmp_one", shared=shared)
        assert log[-2:] == [
            f"{_SET_BOUNDS} ('2s', '5s')",
            f"SELECT campaign_id FROM campaign.authz_state WHERE campaign_id = %s {strength} ('cmp_one',)",
        ]
        assert unit.campaign_locks == [("cmp_one", "share" if shared else "exclusive")]


def test_a_caller_may_raise_the_transaction_bound_for_its_own_longer_work(scripted):
    """A campaign deletion or an enforcement scan needs more than RQ-8's five
    seconds; raising it for everyone instead would be the wrong trade."""
    log, seen = scripted
    seen["rows"].append(_AUTHZ_ROW)
    with _scripted_database().transaction() as unit:
        unit.lock_campaign("cmp_one", shared=False, transaction_timeout_s=30)
    assert f"{_SET_BOUNDS} ('2s', '30s')" in log


def test_locking_a_campaign_postgres_has_no_authorisation_row_for_fails_closed(scripted):
    """Nothing is primed, so the SELECT finds no row (RQ-2(c))."""
    _, seen = scripted
    held: list[list[tuple[str, str]]] = []
    with pytest.raises(CampaignAuthzMissing, match="authorisation"):
        with _scripted_database().transaction() as unit:
            held.append(unit.campaign_locks)
            unit.lock_campaign("cmp_gone", shared=True)
    assert held == [[]], "a lock that was refused is not recorded as held"


def test_the_revision_advance_is_one_guarded_statement_under_the_exclusive_lock(scripted):
    log, seen = scripted
    seen["rows"].extend([_AUTHZ_ROW, ("UPDATE campaign.authz_state", (7,))])
    with _scripted_database().transaction() as unit:
        with pytest.raises(CampaignLockNotHeld, match="exclusively"):
            unit.advance_authz_revision("cmp_one")
        assert not any("UPDATE campaign.authz_state" in line for line in log)

        unit.lock_campaign("cmp_one", shared=False)
        assert unit.advance_authz_revision("cmp_one") == 7
    assert log[-3] == (
        "UPDATE campaign.authz_state SET authz_revision = authz_revision + 1 "
        "WHERE campaign_id = %s RETURNING authz_revision ('cmp_one',)"
    )


def test_a_revision_advance_that_changes_no_row_fails_closed(scripted):
    """The lock succeeded and the UPDATE then matched nothing: the row went away
    under a transaction that believed it held it. Never a silent zero."""
    _, seen = scripted
    seen["rows"].append(_AUTHZ_ROW)
    with pytest.raises(CampaignAuthzMissing, match="authorisation"):
        with _scripted_database().transaction() as unit:
            unit.lock_campaign("cmp_one", shared=False)
            unit.advance_authz_revision("cmp_one")


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

"""An instance that started during a database outage recovers without a restart (1kg.9.8).

Before this, nothing ever looked again: `prepare_database()` gave up after its
tries, no store was built, and every login answered 503 until Cloud Run recycled
the instance — which it does not do while the instance keeps receiving traffic.

Nothing here dials a database. `migrate` is a stub, as in
`test_startup_migrations.py`, and time is a list the test moves by hand.
"""

from __future__ import annotations

import logging

import psycopg
import pytest
from fastapi import HTTPException

from service import app as appmod
from service import auth_store, history
from service.db import Database
from service.migrations import MigrationDriftError, Mode, Report

CURRENT = Report(applied=("0004_next.sql",), current=4, ahead=())
OUTAGE = psycopg.OperationalError('connection to server at "10.1.2.3" failed: the database system is starting up')


class _Rag:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def degraded(monkeypatch):
    """The state `lifespan` leaves behind when the database was away at startup."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("MIGRATIONS_MODE", raising=False)
    now = [1000.0]
    monkeypatch.setattr(appmod, "_clock", lambda: now[0])
    monkeypatch.setattr(appmod, "RagService", _Rag)
    saved = dict(appmod._state)
    appmod._state.clear()
    appmod._state.update({"db": Database(), "migrations": "unavailable"})
    yield now
    appmod._state.clear()
    appmod._state.update(saved)


def _migrate(monkeypatch, *outcomes):
    calls: list[dict[str, object]] = []

    def migrate(dsn=None, **kwargs):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(appmod, "migrate", migrate)
    return calls


def test_the_request_that_finds_the_database_back_is_served(degraded, monkeypatch):
    calls = _migrate(monkeypatch, CURRENT)
    store = appmod.get_auth_store()  # would have been a 503

    assert isinstance(store, auth_store.PostgresAuthStore)
    assert isinstance(appmod.get_message_store(), history.PostgresMessageStore)
    assert store._db is appmod._state["db"], "through the same bounded gate as everything else"
    assert isinstance(appmod.get_service(), _Rag) and appmod.get_service().kwargs["connect"] == store._db.connection
    assert appmod.healthz()["migrations"] == "current"
    assert calls == [{"mode": Mode.APPLY, "connect_timeout_s": 3}], "one short attempt, schema first"

    appmod.get_auth_store()
    appmod.get_message_store()
    assert len(calls) == 1, "a healthy instance never looks again"


def test_the_schema_is_checked_in_the_mode_the_deployment_runs_in(degraded, monkeypatch):
    monkeypatch.setenv("MIGRATIONS_MODE", "verify")
    calls = _migrate(monkeypatch, CURRENT)
    appmod.recover_database()
    assert calls[0]["mode"] is Mode.VERIFY


def test_a_database_that_is_still_away_costs_one_short_look_every_fifteen_seconds(degraded, monkeypatch, caplog):
    now = degraded
    calls = _migrate(monkeypatch, OUTAGE)
    with caplog.at_level(logging.WARNING, logger="service.app"):
        for _ in range(5):
            with pytest.raises(HTTPException) as refused:
                appmod.get_auth_store()
            assert refused.value.status_code == 503
            assert appmod.get_message_store() is None
    assert len(calls) == 1, "every other request answers at once, as before"
    assert "still unreachable (OperationalError)" in caplog.text and "10.1.2.3" not in caplog.text

    now[0] += appmod.RECOVERY_INTERVAL_S - 0.1
    appmod.recover_database()
    assert len(calls) == 1
    now[0] += 0.2
    appmod.recover_database()
    assert len(calls) == 2
    assert appmod.healthz()["migrations"] == "unavailable"


def test_it_recovers_on_a_later_look(degraded, monkeypatch):
    now = degraded
    calls = _migrate(monkeypatch, OUTAGE, OUTAGE, CURRENT)
    for _ in range(3):
        appmod.recover_database()
        now[0] += appmod.RECOVERY_INTERVAL_S + 1
    assert len(calls) == 3 and appmod.healthz()["migrations"] == "current"
    assert "auth" in appmod._state and "store" in appmod._state


def test_only_one_request_looks_at_a_time(degraded, monkeypatch):
    calls = _migrate(monkeypatch, CURRENT)
    assert appmod._recovery_lock.acquire(blocking=False)
    try:
        appmod.recover_database()  # somebody else is looking: answer at once, do not queue
        assert calls == [] and "auth" not in appmod._state
    finally:
        appmod._recovery_lock.release()
    appmod.recover_database()
    assert len(calls) == 1
    assert appmod._recovery_lock.acquire(blocking=False), "and the lock is given back"
    appmod._recovery_lock.release()


def test_a_verdict_ends_the_looking_loudly(degraded, monkeypatch, caplog):
    """A starting instance with a verdict never takes traffic. A running one cannot
    be stopped from here — but it must not serve a schema it does not understand,
    and it must not retry a question whose answer cannot change."""
    now = degraded
    calls = _migrate(monkeypatch, MigrationDriftError("0002_auth_schema.sql changed after it was applied"))
    with caplog.at_level(logging.ERROR, logger="service.app"):
        with pytest.raises(HTTPException):
            appmod.get_auth_store()
    assert appmod.healthz()["migrations"] == "failed"
    assert "schema is refused; not retrying (0002_auth_schema.sql changed after it was applied)" in caplog.text
    assert "auth" not in appmod._state and "store" not in appmod._state

    now[0] += 10 * appmod.RECOVERY_INTERVAL_S
    with pytest.raises(HTTPException):
        appmod.get_auth_store()
    assert len(calls) == 1
    assert appmod._recovery_lock.acquire(blocking=False), "the lock is given back on every path"
    appmod._recovery_lock.release()


def test_an_instance_that_started_healthy_and_a_process_that_never_started_do_not_look(degraded, monkeypatch):
    calls = _migrate(monkeypatch, CURRENT)
    appmod._state["migrations"] = "current"
    appmod.recover_database()
    appmod._state.clear()  # the E2E stub: no startup path ever ran, there is no database to find
    appmod.recover_database()
    assert appmod.get_message_store() is None
    assert calls == []


def test_retrieval_that_already_came_up_is_left_alone(degraded, monkeypatch):
    _migrate(monkeypatch, CURRENT)
    already = object()
    appmod._state["rag"] = already
    appmod.recover_database()
    assert appmod._state["rag"] is already

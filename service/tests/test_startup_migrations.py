"""Startup runs the migrations before anything is served (1kg.1.5).

A verdict stops the process; an outage degrades it exactly as before; and what
is built afterwards — the stores, retrieval — borrows from one bounded pool.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from ingestion import retrieval
from service import app as appmod
from service import auth_store, history
from service.db import Database
from service.migrations import MigrationDriftError, Mode, Report

CURRENT = Report(applied=(), current=3, ahead=())


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    for name in ("MIGRATIONS_MODE", "DB_POOL_MAX", "DB_ASYNC_POOL_MAX", "DB_POOL_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    saved = dict(appmod._state)
    appmod._state.clear()
    yield
    appmod._state.clear()
    appmod._state.update(saved)


def _migrate_returning(monkeypatch, outcome):
    calls: list[Mode] = []

    def migrate(dsn=None, *, mode=Mode.APPLY, **kwargs):
        calls.append(mode)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(appmod, "migrate", migrate)
    return calls


def test_a_migrated_database_gets_one_bounded_pool(monkeypatch):
    calls = _migrate_returning(monkeypatch, CURRENT)
    monkeypatch.setenv("DB_POOL_MAX", "4")
    db = appmod.prepare_database()
    try:
        assert calls == [Mode.APPLY]
        assert isinstance(db, Database) and db.settings.sync_max == 4
        assert not db._pool.closed
        assert appmod._state["migrations"] == "current"
    finally:
        assert db is not None
        db.close()


def test_an_older_build_on_a_newer_schema_says_ahead(monkeypatch):
    _migrate_returning(monkeypatch, Report(applied=(), current=4, ahead=(4,)))
    db = appmod.prepare_database()
    assert db is not None
    db.close()
    assert appmod._state["migrations"] == "ahead"


@pytest.mark.parametrize("outage", [psycopg.OperationalError("connection refused"), OSError("no route to host")])
def test_an_unreachable_database_degrades_as_it_always_has(monkeypatch, caplog, outage):
    _migrate_returning(monkeypatch, outage)
    with caplog.at_level(logging.WARNING, logger="service.app"):
        assert appmod.prepare_database() is None
    assert appmod._state["migrations"] == "unavailable"
    assert "database unavailable" in caplog.text


def test_drift_stops_startup(monkeypatch):
    """On Cloud Run a revision that cannot start never takes traffic, which is
    the point: the previous revision keeps serving the schema it understands."""
    _migrate_returning(monkeypatch, MigrationDriftError("0002_auth_schema.sql changed after it was applied"))
    with pytest.raises(MigrationDriftError):
        appmod.prepare_database()
    with pytest.raises(MigrationDriftError):
        with TestClient(appmod.app):
            pass  # pragma: no cover


def test_verify_mode_is_a_setting_and_a_bad_setting_is_a_verdict(monkeypatch):
    calls = _migrate_returning(monkeypatch, CURRENT)
    monkeypatch.setenv("MIGRATIONS_MODE", "verify")
    db = appmod.prepare_database()
    assert db is not None
    db.close()
    assert calls == [Mode.VERIFY]

    monkeypatch.setenv("MIGRATIONS_MODE", "sometimes")
    with pytest.raises(ValueError):
        appmod.prepare_database()

    monkeypatch.setenv("MIGRATIONS_MODE", "apply")
    monkeypatch.setenv("DB_POOL_MAX", "600")
    with pytest.raises(ValueError, match="DB_POOL_MAX"):
        appmod.prepare_database()
    assert calls == [Mode.VERIFY], "a bad pool setting is refused before the database is touched"


class _RecordingRagService:
    seen: dict[str, object] = {}

    def __init__(self, **kwargs):
        type(self).seen = kwargs


def test_startup_builds_everything_on_the_pool_and_shutdown_closes_it(monkeypatch):
    _migrate_returning(monkeypatch, CURRENT)
    monkeypatch.setattr(appmod, "RagService", _RecordingRagService)
    with TestClient(appmod.app) as client:
        store, auth = appmod._state["store"], appmod._state["auth"]
        assert isinstance(store, history.PostgresMessageStore)
        assert isinstance(auth, auth_store.PostgresAuthStore)
        db = store._db
        assert db is not None and auth._db is db, "one pool, not one per store"
        assert _RecordingRagService.seen["connect"] == db.connection, "retrieval borrows from it too"
        assert not db._pool.closed
        assert client.get("/healthz").json() == {"status": "ok", "ready": True, "migrations": "current"}
    assert db._pool.closed and db._async_pool.closed


def test_with_no_database_at_startup_nothing_is_built(monkeypatch):
    _migrate_returning(monkeypatch, psycopg.OperationalError("connection refused"))
    monkeypatch.setattr(appmod, "RagService", _RecordingRagService)
    with TestClient(appmod.app) as client:
        assert "store" not in appmod._state and "auth" not in appmod._state
        assert _RecordingRagService.seen["connect"] is None
        assert client.get("/healthz").json()["migrations"] == "unavailable"


def test_a_process_that_never_ran_startup_says_unchecked():
    assert TestClient(appmod.app).get("/healthz").json()["migrations"] == "unchecked"


# ── The stores and retrieval borrow, or connect for themselves ───────────────


class _FakePool:
    def __init__(self) -> None:
        self.borrowed = 0

    @contextmanager
    def connection(self):
        self.borrowed += 1
        yield "pooled-connection"


@pytest.mark.parametrize("store_class", [history.PostgresMessageStore, auth_store.PostgresAuthStore])
def test_a_store_borrows_from_the_pool_it_is_given(monkeypatch, store_class):
    pool = _FakePool()
    with store_class(db=pool)._connect() as conn:  # type: ignore[arg-type]
        assert conn == "pooled-connection"
    assert pool.borrowed == 1

    monkeypatch.setattr(psycopg, "connect", lambda dsn: f"direct:{dsn}")
    assert store_class("postgresql://cli/db")._connect() == "direct:postgresql://cli/db"


@pytest.mark.parametrize(
    ("module", "store_class"),
    [(history, history.PostgresMessageStore), (auth_store, auth_store.PostgresAuthStore)],
)
def test_ensure_schema_is_the_migration_runner(monkeypatch, module, store_class):
    """The admin CLI and the integration tests call it; there is one mechanism."""
    seen: list[str | None] = []
    monkeypatch.setattr(module, "migrate", lambda dsn=None: seen.append(dsn))
    store_class("postgresql://cli/db").ensure_schema()
    assert seen == ["postgresql://cli/db"]


def test_retrieval_uses_the_connection_factory_it_is_lent(monkeypatch):
    pool = _FakePool()
    seen: list[object] = []
    monkeypatch.setattr(retrieval, "load_vocabulary", lambda conn: (seen.append(conn), set(), set(), {}, {})[1:])
    monkeypatch.setattr(retrieval, "retrieve_top_k", lambda conn, *args, **kwargs: seen.append(conn) or [])
    monkeypatch.setattr(retrieval, "fetch_chunk_details", lambda conn, ids: seen.append(conn) or {})

    retriever = retrieval.RagRetriever(connect=pool.connection)
    retriever.search([0.0], "basilisk", 3, set(), set(), None, None)
    retriever.fetch([])

    assert seen == ["pooled-connection"] * 3 and pool.borrowed == 3


@pytest.mark.parametrize("store_class", [history.PostgresMessageStore, auth_store.PostgresAuthStore])
def test_a_store_without_a_dsn_of_its_own_lets_the_runner_choose(monkeypatch, store_class):
    """The runner prefers the schema owner's DSN; a store must not override that
    with the runtime's just because it resolved one for its own queries."""
    seen: list[str | None] = []
    module = history if store_class is history.PostgresMessageStore else auth_store
    monkeypatch.setattr(module, "migrate", lambda dsn=None: seen.append(dsn))
    store_class().ensure_schema()
    assert seen == [None]

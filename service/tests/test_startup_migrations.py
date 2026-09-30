"""Startup runs the migrations before anything is served (1kg.1.5).

A verdict stops the process; an outage degrades it exactly as before; and what
is built afterwards — the stores, retrieval — goes through one bounded gate.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from ingestion import retrieval
from service import admin_invites, auth_store, history
from service import app as appmod
from service import migrations as mig
from service.db import Database
from service.migrations import MigrationDriftError, MigrationPackageError, MigrationsPending, Mode, Report

CURRENT = Report(applied=(), current=3, ahead=())


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    for name in ("MIGRATIONS_MODE", "MIGRATIONS_DATABASE_URL", "DB_POOL_MAX", "DB_ASYNC_POOL_MAX", "DB_POOL_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(appmod, "_pause", lambda seconds: None)
    saved = dict(appmod._state)
    appmod._state.clear()
    yield
    appmod._state.clear()
    appmod._state.update(saved)


def _migrate_returning(monkeypatch, *outcomes):
    """`migrate` answers with each outcome in turn, then keeps giving the last."""
    calls: list[Mode] = []

    def migrate(dsn=None, *, mode=Mode.APPLY, **kwargs):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(mode)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(appmod, "migrate", migrate)
    return calls


def test_a_migrated_database_gets_one_bounded_gate(monkeypatch):
    calls = _migrate_returning(monkeypatch, CURRENT)
    monkeypatch.setenv("DB_POOL_MAX", "3")
    db = appmod.prepare_database()
    assert calls == [Mode.APPLY]
    assert isinstance(db, Database) and db.settings.sync_max == 3
    assert appmod._state["migrations"] == "current"


def test_an_older_build_on_a_newer_schema_says_ahead(monkeypatch):
    _migrate_returning(monkeypatch, Report(applied=(), current=4, ahead=(4,)))
    appmod.prepare_database()
    assert appmod._state["migrations"] == "ahead"


OUTAGE = psycopg.OperationalError(
    'connection to server at "10.1.2.3", port 5432 failed: FATAL: password authentication failed for user "rag"'
)


@pytest.mark.parametrize("outage", [OUTAGE, OSError("no route to host 10.1.2.3")])
def test_an_unreachable_database_degrades_as_it_always_has_after_a_few_tries(monkeypatch, caplog, outage):
    calls = _migrate_returning(monkeypatch, outage)
    pauses: list[float] = []
    monkeypatch.setattr(appmod, "_pause", pauses.append)
    with caplog.at_level(logging.WARNING, logger="service.app"):
        db = appmod.prepare_database()
    assert isinstance(db, Database), "retrieval must stay inside the connection budget even now"
    assert appmod._state["migrations"] == "unavailable"
    assert len(calls) == appmod.STARTUP_CONNECT_ATTEMPTS == 3 and pauses == [2.0, 2.0]
    assert "database unavailable" in caplog.text and f"({type(outage).__name__})" in caplog.text
    assert "10.1.2.3" not in caplog.text and "rag" not in caplog.text, "the driver's text stays out of the log"


def test_a_blip_at_cold_start_does_not_decide_the_instances_whole_life(monkeypatch):
    calls = _migrate_returning(monkeypatch, OUTAGE, CURRENT)
    appmod.prepare_database()
    assert len(calls) == 2 and appmod._state["migrations"] == "current"


def test_drift_stops_startup_at_once(monkeypatch):
    """On Cloud Run a revision that cannot start never takes traffic, which is
    the point: the previous revision keeps serving the schema it understands."""
    calls = _migrate_returning(monkeypatch, MigrationDriftError("0002_auth_schema.sql changed after it was applied"))
    with pytest.raises(MigrationDriftError):
        appmod.prepare_database()
    assert len(calls) == 1, "a verdict is not retried"
    with pytest.raises(MigrationDriftError):
        with TestClient(appmod.app):
            pass  # pragma: no cover


def test_an_image_without_its_migrations_does_not_come_up_degraded(monkeypatch, tmp_path):
    """The one packaging failure this design exists to catch. As an `OSError` it
    would read as "the database is unreachable": the revision would start, take
    all the traffic, and answer every login with a 503."""
    monkeypatch.setattr(mig, "packaged_root", lambda: Path(tmp_path / "not-shipped"))
    with pytest.raises(MigrationPackageError, match="missing or unreadable"):
        appmod.prepare_database()


def test_verify_mode_is_a_setting_and_a_bad_setting_is_a_verdict(monkeypatch):
    calls = _migrate_returning(monkeypatch, CURRENT)
    monkeypatch.setenv("MIGRATIONS_MODE", "verify")
    appmod.prepare_database()
    assert calls == [Mode.VERIFY]

    monkeypatch.setenv("MIGRATIONS_MODE", "sometimes")
    with pytest.raises(ValueError):
        appmod.prepare_database()

    monkeypatch.setenv("MIGRATIONS_MODE", "apply")
    monkeypatch.setenv("DB_POOL_MAX", "600")
    with pytest.raises(ValueError, match="DB_POOL_MAX"):
        appmod.prepare_database()

    monkeypatch.delenv("DB_POOL_MAX")
    monkeypatch.setenv("DATABASE_URL", " postgresql://postgres:S3cretPW@/app")
    with pytest.raises(ValueError) as caught:
        appmod.prepare_database()
    assert "S3cretPW" not in str(caught.value)
    assert calls == [Mode.VERIFY], "a bad setting is refused before the database is touched"


class _RecordingRagService:
    seen: dict[str, object] = {}

    def __init__(self, **kwargs):
        type(self).seen = kwargs


def test_startup_builds_everything_on_the_gate_and_shutdown_closes_it(monkeypatch):
    _migrate_returning(monkeypatch, CURRENT)
    monkeypatch.setattr(appmod, "RagService", _RecordingRagService)
    with TestClient(appmod.app) as client:
        store, auth = appmod._state["store"], appmod._state["auth"]
        assert isinstance(store, history.PostgresMessageStore)
        assert isinstance(auth, auth_store.PostgresAuthStore)
        db = store._db
        assert db is not None and auth._db is db, "one gate, not one per store"
        assert _RecordingRagService.seen["connect"] == db.connection, "retrieval goes through it too"
        assert not db._closed
        assert client.get("/healthz").json() == {"status": "ok", "ready": True, "migrations": "current"}
    assert db._closed and db._async_pool.closed


def test_with_no_database_at_startup_no_store_is_built_but_retrieval_stays_bounded(monkeypatch):
    _migrate_returning(monkeypatch, OUTAGE)
    monkeypatch.setattr(appmod, "RagService", _RecordingRagService)
    with TestClient(appmod.app) as client:
        assert "store" not in appmod._state and "auth" not in appmod._state, "the schema was never checked"
        connect = _RecordingRagService.seen["connect"]
        assert getattr(connect, "__self__", None).__class__ is Database
        assert client.get("/healthz").json()["migrations"] == "unavailable"


def test_a_process_that_never_ran_startup_says_unchecked():
    assert TestClient(appmod.app).get("/healthz").json()["migrations"] == "unchecked"


# ── The stores and retrieval borrow, or connect for themselves ───────────────


class _FakeGate:
    def __init__(self) -> None:
        self.borrowed = 0

    @contextmanager
    def connection(self):
        self.borrowed += 1
        yield "gated-connection"


@pytest.mark.parametrize("store_class", [history.PostgresMessageStore, auth_store.PostgresAuthStore])
def test_a_store_goes_through_the_gate_it_is_given(monkeypatch, store_class):
    gate = _FakeGate()
    with store_class(db=gate)._connect() as conn:  # type: ignore[arg-type]
        assert conn == "gated-connection"
    assert gate.borrowed == 1

    monkeypatch.setattr(psycopg, "connect", lambda dsn: f"direct:{dsn}")
    assert store_class("postgresql://cli/db")._connect() == "direct:postgresql://cli/db"


@pytest.mark.parametrize(
    ("module", "store_class"),
    [(history, history.PostgresMessageStore), (auth_store, auth_store.PostgresAuthStore)],
)
def test_ensure_schema_checks_and_never_changes(monkeypatch, module, store_class):
    """An operator's checkout is not the deployed image. Applying whatever
    migrations it holds as a side effect of listing invites would put unreviewed
    DDL into production — so the stores only ever verify."""
    seen: list[tuple[str | None, Mode]] = []
    monkeypatch.setattr(module, "migrate", lambda dsn=None, *, mode: seen.append((dsn, mode)))
    store_class("postgresql://cli/db").ensure_schema()
    store_class().ensure_schema()
    assert seen == [("postgresql://cli/db", Mode.VERIFY), (None, Mode.VERIFY)], (
        "with no DSN of its own a store lets the runner choose; it prefers the schema owner's"
    )


def test_the_admin_cli_refuses_to_run_against_a_schema_it_would_have_to_change(monkeypatch, capsys):
    def pending(self) -> None:
        raise MigrationsPending("this build needs 0004_next.sql — run `python -m service.migrations migrate`")

    monkeypatch.setattr(auth_store.PostgresAuthStore, "ensure_schema", pending)
    assert admin_invites.main(["list"]) == 2
    out = capsys.readouterr().out
    assert "error: this build needs 0004_next.sql" in out and "service.migrations migrate" in out


def test_retrieval_uses_the_connection_factory_it_is_lent(monkeypatch):
    gate = _FakeGate()
    seen: list[object] = []
    monkeypatch.setattr(retrieval, "load_vocabulary", lambda conn: (seen.append(conn), set(), set(), {}, {})[1:])
    monkeypatch.setattr(retrieval, "retrieve_top_k", lambda conn, *args, **kwargs: seen.append(conn) or [])
    monkeypatch.setattr(retrieval, "fetch_chunk_details", lambda conn, ids: seen.append(conn) or {})

    retriever = retrieval.RagRetriever(connect=gate.connection)
    retriever.search([0.0], "basilisk", 3, set(), set(), None, None)
    retriever.fetch([])

    assert seen == ["gated-connection"] * 3 and gate.borrowed == 3

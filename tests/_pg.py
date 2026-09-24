"""Throwaway PostgreSQL databases for the tests that need a real server.

`DATABASE_URL` names a server the tests may create databases on; CI always sets
it (`.github/workflows/ci.yml`, guarded by `service/tests/test_ci_workflow.py`).
Without it these tests skip — and with it, an unreachable server fails them,
because a skip looks exactly like a pass.

Each test gets a database of its own, so advisory locks, ledgers and pools never
meet across tests, and `DROP DATABASE ... WITH (FORCE)` cleans up whatever a
failed test left connected.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

DSN = os.environ.get("DATABASE_URL") or None

needs_db = pytest.mark.skipif(DSN is None, reason="no DATABASE_URL (CI always sets it)")


def connect(dsn: str, *, autocommit: bool = True):
    import psycopg

    return psycopg.connect(dsn, autocommit=autocommit, connect_timeout=10)


def target_dsn(dsn: str, dbname: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", f"/{dbname}\\1", dsn)


@contextmanager
def throwaway_database(prefix: str) -> Iterator[str]:
    """Yields the DSN of a new, empty database and drops it afterwards."""
    assert DSN is not None
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    with connect(DSN) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield target_dsn(DSN, name)
    finally:
        with connect(DSN) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

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
from pathlib import Path

import pytest

DSN = os.environ.get("DATABASE_URL") or None

#: The corpus schema, in the order docker-compose.yml mounts it into vector-db.
#: 01-extensions.sql creates pgvector, so the server must have that extension
#: available, which is why CI runs compose's image.
CORPUS_SCHEMA = [
    Path(__file__).resolve().parent.parent / "vector-db" / "init" / name
    for name in ("01-extensions.sql", "02-schema.sql", "03-hybrid-search.sql", "03a-corpus-provenance.sql")
]

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


@contextmanager
def corpus_database(prefix: str) -> Iterator[str]:
    """Yields the DSN of a new database holding the corpus schema and no rows.

    The server DATABASE_URL names has no corpus in CI (it is deliberately not in
    git), so a test that reads `dnd.chunks` builds one instead of assuming it.
    """
    with throwaway_database(prefix) as dsn:
        with connect(dsn) as conn:
            for path in CORPUS_SCHEMA:
                conn.execute(path.read_text(encoding="utf-8"))
        yield dsn

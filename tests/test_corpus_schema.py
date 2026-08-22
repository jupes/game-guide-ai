"""
Behavioural tests for the dnd.chunks provenance migration (dnd-corpus-wikidot-expansion).

vector-db/init/03a-corpus-provenance.sql is the only vector-db/init/ file with a
manual second application path: unlike service/sql/04-*.sql and 05-*.sql (which
`ensure_schema()` re-applies at every service startup), nothing re-applies
vector-db/init/ files to a database that already has data. It must therefore be
provably idempotent, since it will be run by hand against live databases.

Requires DATABASE_URL (same needs_db pattern as tests/test_schema.py).

Run from repo root:
    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_corpus_schema.py -q
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_DIR = REPO_ROOT / "vector-db" / "init"
INIT_FILES = ["01-extensions.sql", "02-schema.sql", "03-hybrid-search.sql", "03a-corpus-provenance.sql"]
DSN = os.environ.get("DATABASE_URL") or None

needs_db = pytest.mark.skipif(DSN is None, reason="no DATABASE_URL (CI always sets it)")


def _connect(dsn: str, autocommit: bool = True):
    import psycopg

    return psycopg.connect(dsn, autocommit=autocommit)


def _target_dsn(dsn: str, dbname: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", f"/{dbname}\\1", dsn)


@pytest.fixture(scope="module")
def db():
    """A throwaway database with the corpus schema applied, as a fresh install would."""
    assert DSN is not None
    name = f"corpus_{uuid.uuid4().hex[:12]}"
    with _connect(DSN) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with _connect(_target_dsn(DSN, name)) as conn:
            for filename in INIT_FILES:
                conn.execute((INIT_DIR / filename).read_text(encoding="utf-8"))
            yield conn
    finally:
        with _connect(DSN) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _columns(conn) -> dict[str, tuple[str, str, str | None]]:
    """column_name -> (is_nullable, data_type, column_default)."""
    rows = conn.execute(
        "SELECT column_name, is_nullable, data_type, column_default FROM information_schema.columns "
        " WHERE table_schema = 'dnd' AND table_name = 'chunks'"
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


MINIMAL_CHUNK_SQL = """
INSERT INTO dnd.chunks (chunk_id, book_slug, source_file, content_type, text, embedding)
VALUES (%s, %s, %s, 'rule', 'placeholder text', %s)
"""


def _insert_minimal(conn, chunk_id: str, book_slug: str = "wikidot-5e", source_file: str = "https://example.test/page"):
    conn.execute(MINIMAL_CHUNK_SQL, (chunk_id, book_slug, source_file, "[" + ",".join(["0"] * 1536) + "]"))


@needs_db
def test_provenance_columns_exist_with_expected_shape(db):
    cols = _columns(db)
    assert "source_type" in cols and cols["source_type"][:2] == ("NO", "text")
    assert cols["source_type"][2] == "'pdf'::text", "source_type must default to 'pdf' for pre-existing/legacy rows"
    assert "source_url" in cols and cols["source_url"][0] == "YES"
    assert "license" in cols and cols["license"][0] == "YES"


@needs_db
def test_page_fields_are_now_nullable(db):
    cols = _columns(db)
    assert cols["page_start"][0] == "YES", "page_start must be nullable for wiki chunks with no page numbers"
    assert cols["page_end"][0] == "YES", "page_end must be nullable for wiki chunks with no page numbers"


@needs_db
def test_omitting_source_type_defaults_to_pdf(db):
    """A legacy INSERT (the shape every pre-existing PDF chunk used) still works
    and lands as 'pdf' — existing rows are not silently reclassified or broken."""
    chunk_id = f"legacy-{uuid.uuid4().hex[:12]}"
    _insert_minimal(db, chunk_id, book_slug="phb-5e")
    row = db.execute("SELECT source_type, source_url, license, page_start FROM dnd.chunks WHERE chunk_id = %s",
                      (chunk_id,)).fetchone()
    assert row == ("pdf", None, None, None)


@needs_db
def test_source_type_check_constraint_rejects_unknown_values(db):
    chunk_id = f"bad-{uuid.uuid4().hex[:12]}"
    with pytest.raises(Exception, match="dnd_chunks_source_type_check"):
        db.execute(
            "INSERT INTO dnd.chunks (chunk_id, book_slug, source_file, content_type, text, embedding, source_type) "
            "VALUES (%s, 'x', 'x', 'rule', 'x', %s, 'bogus')",
            (chunk_id, "[" + ",".join(["0"] * 1536) + "]"),
        )


@needs_db
def test_wiki_chunk_carries_its_provenance(db):
    chunk_id = f"wiki-{uuid.uuid4().hex[:12]}"
    db.execute(
        "INSERT INTO dnd.chunks (chunk_id, book_slug, source_file, content_type, text, embedding, "
        "source_type, source_url, license) "
        "VALUES (%s, 'wikidot-5e', %s, 'spell', 'x', %s, 'wiki', %s, 'CC BY-SA 3.0')",
        (chunk_id, "https://dnd5e.wikidot.com/spell:fireball",
         "[" + ",".join(["0"] * 1536) + "]", "https://dnd5e.wikidot.com/spell:fireball"),
    )
    row = db.execute("SELECT source_type, source_url, license, page_start, page_end FROM dnd.chunks "
                      "WHERE chunk_id = %s", (chunk_id,)).fetchone()
    assert row == ("wiki", "https://dnd5e.wikidot.com/spell:fireball", "CC BY-SA 3.0", None, None)


@needs_db
def test_reapplying_the_migration_is_idempotent(db):
    """This file has a manual second-application path (no ensure_schema()
    equivalent for vector-db/init/) — it must tolerate being run more than once
    against a database that already has the columns/constraint."""
    migration = (INIT_DIR / "03a-corpus-provenance.sql").read_text(encoding="utf-8")
    for _ in range(3):
        db.execute(migration)  # must not raise

    constraint_count = db.execute(
        "SELECT count(*) FROM pg_constraint WHERE conname = 'dnd_chunks_source_type_check'"
    ).fetchone()[0]
    assert constraint_count == 1, "re-applying must not duplicate the constraint"

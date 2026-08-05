"""
Schema-parity guard: the startup DDL and the compose init SQL must agree.

Each schema exists twice on purpose. `vector-db/init/*.sql` runs only on FIRST
container init (fresh volume); `ensure_schema()` in the stores runs the same DDL
at every startup, which is the migration path for volumes that already exist.
The two are kept in sync by hand, and the failure mode when they drift is
invisible: whichever environment you didn't test picks up a schema the other one
doesn't have.

That is not hypothetical for the constraints below — they are the database-level
backstop for account deletion (a request already in flight when an account is
deleted must not be able to recreate its rows), so a fresh volume silently
missing them would mean the protection exists only where it was tested.

**Two layers (PR #43 review).** This used to compare CREATE/ALTER *target names*
and then check that a handful of strings appeared somewhere in the file. That is
weaker than it looks: a foreign key naming the wrong column — the exact defect
found in review round 9 — has the right name, the right target and all the right
substrings, so it passed. Now:

  1. **Offline (always runs).** The two copies must be identical once comments
     and whitespace are removed. They already were; asserting it turns "the same
     objects are mentioned" into "the same DDL, character for character".
  2. **Against real PostgreSQL (runs when DATABASE_URL is set; CI sets it).**
     Apply each copy to its own throwaway database and compare what PostgreSQL
     actually built — `pg_constraint` (including the referenced columns and the
     delete action), indexes, and column types. This is the layer that cannot be
     fooled by any amount of clever text: it compares outcomes, not source.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_schema_parity.py -q
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_DIR = REPO_ROOT / "vector-db" / "init"


def _ddl_constant(module_rel: str, name: str) -> str:
    """Read a triple-quoted DDL constant WITHOUT importing the module (these
    tests must run without the service's dependencies installed)."""
    source = (REPO_ROOT / module_rel).read_text(encoding="utf-8")
    match = re.search(rf'^{name}\s*=\s*"""(.*?)"""', source, re.S | re.M)
    assert match, f"{name} not found in {module_rel}"
    return match.group(1)


def _normalize(sql: str) -> str:
    """Strip comments and collapse whitespace — the two copies are allowed to
    differ in commentary and formatting, never in what they create."""
    no_comments = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"\s+", " ", no_comments).strip().lower()


def _statements(sql: str) -> list[str]:
    """Split normalized DDL into statements, keeping `DO $$ ... $$` blocks whole
    (they contain semicolons of their own)."""
    out: list[str] = []
    for chunk in re.split(r"(do \$\$.*?\$\$)", sql, flags=re.S):
        if chunk.startswith("do $$"):
            out.append(chunk.strip())
        else:
            out.extend(s.strip() for s in chunk.split(";") if s.strip())
    return out


PAIRS = [
    ("service/history.py", "CHAT_SCHEMA_DDL", "04-chat-schema.sql"),
    ("service/auth_store.py", "AUTH_SCHEMA_DDL", "05-auth-schema.sql"),
]

# Statements whose absence silently removes a security property, so they are
# asserted by name rather than left to a whole-file comparison.
REQUIRED = {
    "04-chat-schema.sql": [
        "messages_conversation_fkey",
        "attachments_conversation_fkey",
        "on delete cascade",
    ],
    "05-auth-schema.sql": [
        "conversations_user_fkey",
        "invites_used_by_fkey",
        "on delete set null",
    ],
}


# ── Layer 1: the two copies are the same DDL ─────────────────────────────────


@pytest.mark.parametrize(("module_rel", "const", "sql_name"), PAIRS)
def test_startup_ddl_and_init_sql_are_the_same_ddl(module_rel, const, sql_name):
    """Full equality, not just matching object names.

    Comparing names let a constraint keep its name while changing what it
    constrains. If a legitimate divergence between the two copies is ever
    needed, this test is the place to record it deliberately — the point is that
    nobody can introduce one by accident.
    """
    startup = _normalize(_ddl_constant(module_rel, const))
    init = _normalize((INIT_DIR / sql_name).read_text(encoding="utf-8"))
    if startup == init:
        return

    # Same failure, reported per statement so the diff is readable.
    startup_stmts, init_stmts = _statements(startup), _statements(init)
    only_startup = [s for s in startup_stmts if s not in init_stmts]
    only_init = [s for s in init_stmts if s not in startup_stmts]
    pytest.fail(
        f"{module_rel}:{const} and vector-db/init/{sql_name} have drifted.\n"
        + "".join(f"\n  only at startup: {s[:400]}" for s in only_startup)
        + "".join(f"\n  only in init sql: {s[:400]}" for s in only_init)
        + ("\n  (statements match; the copies differ only in ordering)"
           if not only_startup and not only_init else "")
    )


@pytest.mark.parametrize(("module_rel", "const", "sql_name"), PAIRS)
def test_security_constraints_are_present_in_both_copies(module_rel, const, sql_name):
    startup = _normalize(_ddl_constant(module_rel, const))
    init = _normalize((INIT_DIR / sql_name).read_text(encoding="utf-8"))
    for needle in REQUIRED[sql_name]:
        assert needle in startup, f"{const} lost `{needle}`"
        assert needle in init, f"vector-db/init/{sql_name} lost `{needle}`"


def test_ownership_constraints_are_added_not_valid():
    """`NOT VALID` is load-bearing: conversations predating the ownership table
    have messages with no parent row, so a validating constraint would refuse to
    be created against real data (or, worse, only fail on the production
    volume). New writes are still enforced."""
    chat = _normalize(_ddl_constant("service/history.py", "CHAT_SCHEMA_DDL"))
    auth = _normalize(_ddl_constant("service/auth_store.py", "AUTH_SCHEMA_DDL"))
    for name, sql in (("chat", chat), ("auth", auth)):
        for constraint in re.findall(r"add constraint (\w+_fkey)[^;]*", sql):
            if constraint == "invites_used_by_fkey":
                continue  # auth.invites is new with auth; no legacy rows to spare
            clause = re.search(rf"add constraint {constraint}(.*?);", sql, re.S)
            assert clause and "not valid" in clause.group(1), (
                f"{name}: {constraint} must be added NOT VALID"
            )


# ── Layer 2: what PostgreSQL actually builds ─────────────────────────────────

DSN = os.environ.get("DATABASE_URL") or None

pg_required = pytest.mark.skipif(
    DSN is None,
    reason="no DATABASE_URL — set one to compare the two schemas as PostgreSQL "
           "builds them (CI always sets it; see .github/workflows/ci.yml)",
)

#: Every property that matters about a foreign key, read back from the catalogue
#: rather than from the text that created it. `confdeltype` is the delete action;
#: the two `unnest` joins resolve conkey/confkey to real column NAMES, which is
#: what makes a wrong-column FK visible here.
CONSTRAINT_QUERY = """
SELECT n.nspname || '.' || rel.relname AS table_name,
       c.conname,
       c.contype,
       c.confdeltype,
       c.confupdtype,
       c.convalidated,
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
       ) AS columns,
       CASE WHEN c.confrelid = 0 THEN NULL
            ELSE (SELECT fn.nspname || '.' || fr.relname
                    FROM pg_class fr JOIN pg_namespace fn ON fn.oid = fr.relnamespace
                   WHERE fr.oid = c.confrelid)
       END AS referenced_table,
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
       ) AS referenced_columns,
       pg_get_constraintdef(c.oid) AS definition
  FROM pg_constraint c
  JOIN pg_class rel ON rel.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = rel.relnamespace
 WHERE n.nspname = ANY(%s)
 ORDER BY 1, 2
"""

#: Column positions in CONSTRAINT_QUERY. Named, because reading a wide row by
#: bare index is how the first draft of this test indexed past the end of it.
C_TABLE, C_NAME, C_TYPE, C_DELETE_ACTION = 0, 1, 2, 3
C_COLUMNS, C_REF_TABLE, C_REF_COLUMNS, C_DEFINITION = 6, 7, 8, 9

INDEX_QUERY = """
SELECT schemaname || '.' || tablename, indexname, indexdef
  FROM pg_indexes WHERE schemaname = ANY(%s) ORDER BY 1, 2
"""

COLUMN_QUERY = """
SELECT table_schema || '.' || table_name, column_name, data_type,
       is_nullable, column_default
  FROM information_schema.columns
 WHERE table_schema = ANY(%s) ORDER BY 1, 2
"""

SCHEMAS = ["chat", "auth"]


def _apply(dsn: str, dbname: str, scripts: list[str]) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    target = re.sub(r"/[^/?]+(\?|$)", f"/{dbname}\\1", dsn)
    with psycopg.connect(target, autocommit=True) as conn:
        for sql in scripts:
            conn.execute(sql)


def _catalogue(dsn: str, dbname: str) -> dict[str, list[tuple]]:
    import psycopg

    target = re.sub(r"/[^/?]+(\?|$)", f"/{dbname}\\1", dsn)
    with psycopg.connect(target) as conn:
        return {
            "constraints": [tuple(r) for r in conn.execute(CONSTRAINT_QUERY, (SCHEMAS,)).fetchall()],
            "indexes": [tuple(r) for r in conn.execute(INDEX_QUERY, (SCHEMAS,)).fetchall()],
            "columns": [tuple(r) for r in conn.execute(COLUMN_QUERY, (SCHEMAS,)).fetchall()],
        }


def _drop(dsn: str, dbname: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


@pytest.fixture(scope="module")
def built_schemas():
    """Build both copies in throwaway databases and hand back their catalogues.

    Order matters and mirrors production: chat before auth, because the auth DDL
    adds the ownership FK to `chat.conversations` and skips it when that table
    does not exist yet.
    """
    assert DSN is not None
    stamp = uuid.uuid4().hex[:12]
    init_db, startup_db = f"parity_init_{stamp}", f"parity_startup_{stamp}"
    try:
        _apply(DSN, init_db, [
            (INIT_DIR / "04-chat-schema.sql").read_text(encoding="utf-8"),
            (INIT_DIR / "05-auth-schema.sql").read_text(encoding="utf-8"),
        ])
        _apply(DSN, startup_db, [
            _ddl_constant("service/history.py", "CHAT_SCHEMA_DDL"),
            _ddl_constant("service/auth_store.py", "AUTH_SCHEMA_DDL"),
        ])
        yield _catalogue(DSN, init_db), _catalogue(DSN, startup_db)
    finally:
        _drop(DSN, init_db)
        _drop(DSN, startup_db)


@pg_required
@pytest.mark.parametrize("aspect", ["constraints", "indexes", "columns"])
def test_both_copies_build_the_same_schema(built_schemas, aspect):
    """The check no text comparison can make: PostgreSQL applied both copies and
    reports identical catalogues — same constraints on the same columns, with
    the same delete actions and referenced columns."""
    init, startup = built_schemas
    assert init[aspect] == startup[aspect], (
        f"the init SQL and the startup DDL build different {aspect}:\n"
        f"  only from init sql: {[r for r in init[aspect] if r not in startup[aspect]]}\n"
        f"  only from startup:  {[r for r in startup[aspect] if r not in init[aspect]]}"
    )


@pg_required
def test_the_built_foreign_keys_have_the_expected_shape(built_schemas):
    """An anchor on the real behaviour, so 'both copies are equally wrong' still
    fails. `confdeltype` is the delete action PostgreSQL will enforce: 'c' =
    CASCADE, 'n' = SET NULL. These are the constraints that make deleting a
    compromised account actually remove its content."""
    init, _ = built_schemas
    by_name = {r[C_NAME]: r for r in init["constraints"] if r[C_TYPE] == "f"}

    # name -> (table, columns, referenced table, referenced columns, delete action)
    # `chat.conversations` is keyed by conversation_id (TEXT), `auth.users` by id.
    expected = {
        "messages_conversation_fkey": (
            "chat.messages", ["conversation_id"],
            "chat.conversations", ["conversation_id"], "c"),
        "attachments_conversation_fkey": (
            "chat.attachments", ["conversation_id"],
            "chat.conversations", ["conversation_id"], "c"),
        "conversations_user_fkey": (
            "chat.conversations", ["user_id"], "auth.users", ["id"], "c"),
        "invites_used_by_fkey": (
            "auth.invites", ["used_by"], "auth.users", ["id"], "n"),
    }
    missing = sorted(set(expected) - set(by_name))
    assert not missing, f"foreign keys never created: {missing}"

    for name, shape in expected.items():
        row = by_name[name]
        actual = (
            row[C_TABLE], list(row[C_COLUMNS]), row[C_REF_TABLE],
            list(row[C_REF_COLUMNS]), row[C_DELETE_ACTION],
        )
        assert actual == shape, (
            f"{name} does not constrain what it is supposed to.\n"
            f"  expected: {shape}\n"
            f"  actual:   {actual}\n"
            f"  definition: {row[C_DEFINITION]}"
        )

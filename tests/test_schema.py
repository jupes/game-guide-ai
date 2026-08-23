"""
The canonical schema files enforce what the application relies on.

`service/sql/*.sql` is the single definition of the `chat` and `auth` schemas.
Both the fresh-database path (compose init dir, `scripts/bootstrap-db.sh`) and
the existing-database path (`ensure_schema()` at every service startup) apply
these same files, so there is no second copy to drift.

The tests below therefore check *behaviour against a real PostgreSQL* rather
than comparing text: what tables and columns exist, which columns each foreign
key actually constrains, and what deleting a row really does. Constraint
definitions are only interesting for what the database does with them.

Requires DATABASE_URL for the behavioural half; CI always sets it. The packaging
guards above it run everywhere.

Run from repo root:
    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_schema.py -q
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = REPO_ROOT / "service" / "sql"
DSN = os.environ.get("DATABASE_URL") or None

needs_db = pytest.mark.skipif(DSN is None, reason="no DATABASE_URL (CI always sets it)")


# ── The canonical files reach the places that apply them ─────────────────────


def test_the_schema_files_load_through_the_package():
    """`ensure_schema()` reads them via importlib, so an image that ships the
    code without the SQL cannot migrate a database."""
    from service.schema import ALL_SCHEMAS, load

    for name in ALL_SCHEMAS:
        assert "CREATE" in load(name).upper(), f"{name} loaded empty"


def test_packaging_ships_the_sql():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"\[tool\.setuptools\.package-data\]", text), (
        "service/sql/*.sql must be declared as package-data or `pip install .` "
        "silently produces an image that cannot apply its own schema"
    )
    assert re.search(r'service\s*=\s*\[[^\]]*sql/\*\.sql', text)


def test_compose_mounts_the_canonical_files():
    """Not a second copy under vector-db/init/ — that duplication is what this
    layout removes."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name in ("04-chat-schema.sql", "05-auth-schema.sql"):
        assert f"./service/sql/{name}:" in compose, f"compose must mount {name}"
        assert not (REPO_ROOT / "vector-db" / "init" / name).exists(), (
            f"vector-db/init/{name} is back — the schema has one definition"
        )


def test_initdb_files_are_mounted_individually_not_nested_in_a_directory_mount():
    """`docker compose up -d vector-db` reliably fails ("read-only file system")
    if 01-03 are mounted as one ./vector-db/init:/docker-entrypoint-initdb.d
    directory mount while 04/05 are separately mounted as single files nested
    *under* that same target — the OCI runtime has to create a mountpoint for
    each nested file mount by writing into its parent, which a read-only
    directory mount blocks. A writable directory mount "fixes" the error but
    silently recreates a second copy of 04/05 on the host at container start
    (Docker materializes the mountpoint stub through the live bind), which is
    exactly the duplication test_compose_mounts_the_canonical_files forbids.
    The only fix that has neither failure mode: mount every init file
    individually, so there is no directory-level mount to nest under."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/docker-entrypoint-initdb.d:" not in compose and ":/docker-entrypoint-initdb.d\n" not in compose, (
        "no bare directory mount at /docker-entrypoint-initdb.d — mount each "
        "init file individually (see the comment above the volumes: block)"
    )
    for name in ("01-extensions.sql", "02-schema.sql", "03-hybrid-search.sql"):
        assert f"./vector-db/init/{name}:/docker-entrypoint-initdb.d/{name}:ro" in compose, (
            f"{name} must be mounted individually, read-only"
        )


def test_the_stores_do_not_embed_their_own_ddl():
    for module in ("service/history.py", "service/auth_store.py"):
        text = (REPO_ROOT / module).read_text(encoding="utf-8")
        assert "CREATE TABLE" not in text.upper(), (
            f"{module} embeds DDL again; the .sql file is the definition"
        )


# ── What the database actually does ──────────────────────────────────────────


def _connect(dsn: str, autocommit: bool = True):
    import psycopg

    return psycopg.connect(dsn, autocommit=autocommit)


def _target_dsn(dsn: str, dbname: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", f"/{dbname}\\1", dsn)


@pytest.fixture(scope="module")
def db():
    """A throwaway database with the canonical schema applied, as a fresh
    install would."""
    from service.schema import ALL_SCHEMAS, load

    assert DSN is not None
    name = f"schema_{uuid.uuid4().hex[:12]}"
    with _connect(DSN) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with _connect(_target_dsn(DSN, name)) as conn:
            for sql in ALL_SCHEMAS:
                conn.execute(load(sql))
            yield conn
    finally:
        with _connect(DSN) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _columns(conn, table: str) -> dict[str, str]:
    schema, name = table.split(".")
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        " WHERE table_schema = %s AND table_name = %s",
        (schema, name),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


REQUIRED_COLUMNS = {
    "auth.users": ["id", "email", "password_hash", "role", "created_at"],
    "auth.invites": ["token", "role", "expires_at", "used_at", "used_by", "revoked_at"],
    "chat.conversations": [
        "conversation_id", "user_id", "created_at",
        "selection_strategy", "manual_alias", "catalog_revision",
    ],
    "chat.messages": ["id", "conversation_id", "mode", "role", "content", "suggestions"],
    "chat.attachments": ["id", "conversation_id", "filename", "content_type", "extracted_text"],
}


@needs_db
@pytest.mark.parametrize(("table", "expected"), REQUIRED_COLUMNS.items())
def test_required_tables_and_columns_exist(db, table, expected):
    actual = _columns(db, table)
    assert actual, f"{table} was not created"
    missing = [c for c in expected if c not in actual]
    assert not missing, f"{table} is missing {missing}"


#: name -> (table, columns, referenced table, referenced columns, delete action)
#: 'c' = ON DELETE CASCADE, 'n' = ON DELETE SET NULL.
FOREIGN_KEYS = {
    "messages_conversation_fkey": (
        "chat.messages", ["conversation_id"], "chat.conversations", ["conversation_id"], "c"),
    "attachments_conversation_fkey": (
        "chat.attachments", ["conversation_id"], "chat.conversations", ["conversation_id"], "c"),
    "conversations_user_fkey": (
        "chat.conversations", ["user_id"], "auth.users", ["id"], "c"),
    "invites_used_by_fkey": (
        "auth.invites", ["used_by"], "auth.users", ["id"], "n"),
}

FK_QUERY = """
SELECT n.nspname || '.' || rel.relname,
       c.confdeltype,
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum),
       (SELECT fn.nspname || '.' || fr.relname FROM pg_class fr
          JOIN pg_namespace fn ON fn.oid = fr.relnamespace WHERE fr.oid = c.confrelid),
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum),
       c.convalidated
  FROM pg_constraint c
  JOIN pg_class rel ON rel.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = rel.relnamespace
 WHERE c.conname = %s AND c.contype = 'f'
"""


@needs_db
@pytest.mark.parametrize(("name", "shape"), FOREIGN_KEYS.items())
def test_foreign_keys_constrain_the_right_columns(db, name, shape):
    """A same-named key on the wrong column type-checks and creates cleanly —
    and leaves the column it was meant to protect wide open."""
    row = db.execute(FK_QUERY, (name,)).fetchone()
    assert row is not None, f"{name} was never created"
    table, delete_action, cols, ref_table, ref_cols, _ = row
    assert (table, list(cols), ref_table, list(ref_cols), delete_action) == shape


@needs_db
def test_deleting_an_account_takes_its_content_with_it(db):
    """The cascade chain auth.users -> chat.conversations -> messages/attachments.

    This is the operational guarantee behind account revocation: one DELETE
    removes the account and everything it owns, with no orphaned rows an
    in-flight request could still write into.
    """
    conv = f"conv-{uuid.uuid4().hex[:8]}"
    user_id = db.execute(
        "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
        (f"cascade-{uuid.uuid4().hex[:8]}@example.com",),
    ).fetchone()[0]
    db.execute("INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s)",
               (conv, user_id))
    db.execute("INSERT INTO chat.messages (conversation_id, mode, role, content) "
               "VALUES (%s, 'sage', 'user', 'hi')", (conv,))
    db.execute("INSERT INTO chat.attachments (conversation_id, filename, extracted_text) "
               "VALUES (%s, 'a.txt', 'text')", (conv,))

    db.execute("DELETE FROM auth.users WHERE id = %s", (user_id,))

    for table in ("chat.conversations", "chat.messages", "chat.attachments"):
        left = db.execute(
            f"SELECT count(*) FROM {table} WHERE conversation_id = %s", (conv,)  # noqa: S608
        ).fetchone()[0]
        assert left == 0, f"{table} still holds rows for a deleted account"


@needs_db
def test_deleting_an_account_keeps_the_invite_as_a_spent_token(db):
    """SET NULL, not CASCADE. The invite row is the audit trail that its token
    was spent — deleting the account must not hand the token back."""
    token = f"tok-{uuid.uuid4().hex}"
    user_id = db.execute(
        "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
        (f"setnull-{uuid.uuid4().hex[:8]}@example.com",),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO auth.invites (token, expires_at, used_at, used_by) "
        "VALUES (%s, now() + interval '1 day', now(), %s)", (token, user_id))

    db.execute("DELETE FROM auth.users WHERE id = %s", (user_id,))

    row = db.execute("SELECT used_at, used_by FROM auth.invites WHERE token = %s",
                     (token,)).fetchone()
    assert row is not None, "the invite was deleted — its token now looks unspent"
    assert row[0] is not None, "the invite must stay marked used"
    assert row[1] is None, "used_by must be NULL once the account is gone"


@needs_db
def test_reapplying_over_legacy_rows_succeeds_and_leaves_them_alone(db):
    """NOT VALID is what makes the startup migration safe.

    Conversations predating the ownership table have messages with no parent
    row. A validating constraint would refuse to be created against that data —
    on the production volume only — so every startup would fail after deploy.
    """
    from service.schema import CHAT_SCHEMA, load

    orphan = f"orphan-{uuid.uuid4().hex[:8]}"
    db.execute("ALTER TABLE chat.messages DROP CONSTRAINT messages_conversation_fkey")
    db.execute("INSERT INTO chat.messages (conversation_id, mode, role, content) "
               "VALUES (%s, 'sage', 'user', 'legacy')", (orphan,))

    db.execute(load(CHAT_SCHEMA))  # the startup migration path

    row = db.execute(FK_QUERY, ("messages_conversation_fkey",)).fetchone()
    assert row is not None, "the migration did not restore the constraint"
    assert row[5] is False, "the constraint must be NOT VALID, or legacy rows block startup"
    assert db.execute("SELECT count(*) FROM chat.messages WHERE conversation_id = %s",
                      (orphan,)).fetchone()[0] == 1, "legacy rows must survive"


@needs_db
def test_reapplying_is_a_no_op(db):
    """Every cold start re-applies this against a live database. Re-creating a
    constraint would take an ACCESS EXCLUSIVE lock each time; identical OIDs
    prove nothing was dropped and re-added."""
    from service.schema import ALL_SCHEMAS, load

    def oids():
        return dict(db.execute(
            "SELECT conname, oid FROM pg_constraint WHERE conname = ANY(%s)",
            (list(FOREIGN_KEYS),),
        ).fetchall())

    before = oids()
    for _ in range(3):
        for sql in ALL_SCHEMAS:
            db.execute(load(sql))
    assert oids() == before, "re-applying the schema rebuilt constraints"


# ── The daily cost ceiling counts the same rows in both stores (x5bz.3.3) ────


@needs_db
def test_calls_today_counts_the_same_rows_as_the_in_memory_fake(db):
    """The cap (`x5bz.3.3`) reads this number to decide whether the pilot has
    spent its budget, and every unit test upstream trusts the in-memory fake to
    behave like the real store. Two ways they could silently diverge:

    - **Role.** Each turn writes a user row AND an assistant row. Counting both
      would halve the configured cap with nothing failing.
    - **Timezone.** A bare `date_trunc('day', now())` truncates in the server's
      TimeZone, which this schema never sets — so the day would roll over at
      whatever hour the instance is configured for, and this test would pass or
      fail on where it ran rather than on the query.

    Asserted as a DELTA, not an absolute: `calls_today()` counts the whole
    database and the `db` fixture is module-scoped, so an absolute count would
    depend on which other tests ran first.
    """
    from datetime import UTC, datetime, timedelta

    from service.history import InMemoryMessageStore, PostgresMessageStore, _Row

    # Built from DSN, not db.info.dsn — psycopg strips the password from the
    # latter, so the store would fail to authenticate.
    assert DSN is not None
    current = db.execute("SELECT current_database()").fetchone()[0]
    real = PostgresMessageStore(dsn=_target_dsn(DSN, current))
    before = real.calls_today()

    now = datetime.now(UTC)
    rows = [
        ("today-user-1", "user", now),
        ("today-assistant", "assistant", now),
        ("today-user-2", "user", now - timedelta(minutes=5)),
        ("yesterday-user", "user", now - timedelta(days=1)),
    ]

    # The owning rows come first. messages_conversation_fkey is NOT VALID, which
    # exempts rows that predate it — it still enforces every new INSERT, so
    # writing a message under an unknown conversation fails outright.
    conv = f"cap-{uuid.uuid4().hex[:8]}"
    user_id = db.execute(
        "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
        (f"cap-{uuid.uuid4().hex[:8]}@example.com",),
    ).fetchone()[0]
    db.execute("INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s)",
               (conv, user_id))
    for content, role, created in rows:
        db.execute(
            "INSERT INTO chat.messages (conversation_id, mode, role, content, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (conv, "sage", role, content, created),
        )

    fake = InMemoryMessageStore()
    fake._rows.extend([
        _Row(id=i, conversation_id=conv, mode="sage", role=role,
             content=content, suggestions=None, created_at=created)
        for i, (content, role, created) in enumerate(rows, start=1)
    ])

    assert real.calls_today() - before == fake.calls_today() == 2, (
        "both stores must count today's USER rows only — two of these four"
    )


# ── Conversation strategy binding (b8o.2, D1/D6) — behaviour, not just shape ──


@needs_db
def test_strategy_check_constraint_rejects_an_invalid_value(db):
    conv = f"strategy-invalid-{uuid.uuid4().hex[:8]}"
    user_id = db.execute(
        "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
        (f"strategy-invalid-{uuid.uuid4().hex[:8]}@example.com",),
    ).fetchone()[0]
    db.execute("INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s)",
               (conv, user_id))
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE chat.conversations SET selection_strategy = 'not-a-real-strategy' "
            "WHERE conversation_id = %s", (conv,),
        )
    db.rollback()


@needs_db
def test_claim_conversation_strategy_wins_the_race_against_a_real_database(db):
    """The one behavior an in-memory fake can't meaningfully prove: two
    concurrent UPDATEs racing against a real database, gated by `WHERE
    selection_strategy IS NULL`, must leave exactly one winner — not a
    torn/overwritten state. Simulated as two sequential calls (the SQL gate
    itself is what makes true concurrency safe; this proves the gate exists
    and behaves as specified for the caller-visible contract)."""
    from service.history import PostgresMessageStore

    current = db.execute("SELECT current_database()").fetchone()[0]
    real = PostgresMessageStore(dsn=_target_dsn(DSN, current))

    conv = f"strategy-race-{uuid.uuid4().hex[:8]}"
    user_id = db.execute(
        "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
        (f"strategy-race-{uuid.uuid4().hex[:8]}@example.com",),
    ).fetchone()[0]
    db.execute("INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s)",
               (conv, user_id))

    first = real.claim_conversation_strategy(
        conv, strategy="auto", manual_alias=None, catalog_revision="r1",
    )
    second = real.claim_conversation_strategy(
        conv, strategy="manual", manual_alias="gpt-4o-mini", catalog_revision="r1",
    )
    assert first == ("auto", None)
    assert second == ("auto", None), "the second (losing) call must return the FIRST call's binding"

    row = db.execute(
        "SELECT selection_strategy, manual_alias FROM chat.conversations WHERE conversation_id = %s",
        (conv,),
    ).fetchone()
    assert (row[0], row[1]) == ("auto", None), "the database must hold the winner, not the loser"


@needs_db
def test_claim_conversation_strategy_without_an_ownership_row_raises(db):
    from service.history import PostgresMessageStore

    current = db.execute("SELECT current_database()").fetchone()[0]
    real = PostgresMessageStore(dsn=_target_dsn(DSN, current))

    with pytest.raises(LookupError):
        real.claim_conversation_strategy(
            f"never-owned-{uuid.uuid4().hex[:8]}", strategy="auto",
            manual_alias=None, catalog_revision="r1",
        )

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

Run from repo root:
    uv run --with pytest python -m pytest tests/test_schema_parity.py -q
"""

from __future__ import annotations

import re
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


@pytest.mark.parametrize(("module_rel", "const", "sql_name"), PAIRS)
def test_startup_ddl_and_init_sql_declare_the_same_objects(module_rel, const, sql_name):
    """Every CREATE/ALTER target in one copy must appear in the other."""
    startup = _normalize(_ddl_constant(module_rel, const))
    init = _normalize((INIT_DIR / sql_name).read_text(encoding="utf-8"))

    pattern = re.compile(
        r"(create (?:schema|table|(?:unique )?index)(?: if not exists)?|alter table)\s+([\w.\"]+)"
    )
    startup_objects = set(pattern.findall(startup))
    init_objects = set(pattern.findall(init))

    assert startup_objects == init_objects, (
        f"{module_rel}:{const} and vector-db/init/{sql_name} have drifted.\n"
        f"  only at startup: {sorted(startup_objects - init_objects)}\n"
        f"  only in init sql: {sorted(init_objects - startup_objects)}"
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

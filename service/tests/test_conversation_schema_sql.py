"""What the conversation SQL and the conversation Python promise each other (1kg.2.4).

A sibling of `test_campaign_schema_sql.py`, which this bead does not edit: that
file pins the *identifier* registry against released `0004`, and a conversation
id deliberately never joins that registry (a `cnv_` id carries no `CHECK`, so it
has nothing to agree with there).

What is written twice here is the **channel vocabulary**. `service.models.ChatMode`
names four modes in Python and the migration's `IN` list names them again in SQL.
One rule spelled twice drifts, so this file reads the migration and requires the
two to be the same list, in both directions: a mode added to the enum that the
CHECK would refuse is a row the application can never write, and a mode left in
the CHECK that the enum has retired is a value nothing can ever produce.

The index is pinned here too, by **name and predicate**, because
`ConversationStore.list_for_owner`'s ordering and default filter are written to
match it exactly; an index quietly renamed or widened turns the index's whole
point — the default query — into a sequential scan without failing anything.

These are text assertions on a file, so they need no database and run anywhere.
What the database itself does with them is `tests/test_migrations_db.py`'s.
"""

from __future__ import annotations

import re
from pathlib import Path

from service.models import ChatMode

#: The migration's number appears in Python exactly ONCE, here (ruling R-7).
#: A parallel bead takes the same number on its own base and the lead renumbers
#: whichever pull request merges second; this constant is the only line that has
#: to move with it.
MIGRATION_FILENAME = "0007_conversation_started_mode.sql"

MIGRATIONS = Path(__file__).resolve().parents[1] / "sql" / "migrations"
STARTED_MODE_SQL = (MIGRATIONS / MIGRATION_FILENAME).read_text(encoding="utf-8")


def _statements(sql: str) -> str:
    """`sql` with its comment lines removed — what the server actually reads.

    Every assertion below is about a statement, and the header comment of this
    migration quotes several of the same words it asserts on.
    """
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


# ── The channel vocabulary, spelled once ─────────────────────────────────────


def test_the_started_mode_check_lists_exactly_the_modes_the_enum_carries():
    """Both directions at once, and in the enum's own order: the `IN` list is
    generated from `service.models.ChatMode`, never typed by hand."""
    found = re.search(r"started_mode IN \(([^)]*)\)", _statements(STARTED_MODE_SQL))
    assert found is not None, f"{MIGRATION_FILENAME} no longer constrains started_mode"
    in_sql = [value.strip().strip("'") for value in found.group(1).split(",")]
    assert in_sql == [mode.value for mode in ChatMode]


def test_started_mode_is_nullable_and_the_check_says_so():
    """NULL is the honest value for every conversation that exists today — the
    channel it was started in is unknown, not absent (the contract's *Not
    recorded* rule). A CHECK that forgot the NULL arm would refuse every legacy
    row the moment anything touched it."""
    assert "started_mode IS NULL OR started_mode IN (" in _statements(STARTED_MODE_SQL)
    assert "started_mode TEXT NOT NULL" not in _statements(STARTED_MODE_SQL)


def test_the_migration_adds_exactly_one_column():
    """Requirement 1: `started_mode` is the only column this bead adds. 0006
    already gave `campaign_id`, `title`, `updated_at` and `archived_at`, and a
    second column here would be scope that belongs to another bead."""
    assert set(re.findall(r"ADD COLUMN (\w+)", STARTED_MODE_SQL)) == {"started_mode"}


# ── The index the default query is written against ───────────────────────────


def test_the_owner_recent_index_exists_by_name_with_the_agreed_ordering():
    """`list_for_owner` orders by `COALESCE(updated_at, created_at) DESC,
    conversation_id DESC` — a total order, so a cursor over it is stable. The
    index carries the same three keys in the same direction or it does not serve
    that query at all."""
    # Greedy to the last `)` on the line, because the key list itself contains a
    # parenthesised call: a lazy match stops inside `COALESCE(`.
    found = re.search(
        r"CREATE INDEX conversations_owner_recent_idx\s+ON chat\.conversations \((.*)\)\s*$",
        _statements(STARTED_MODE_SQL),
        re.MULTILINE,
    )
    assert found is not None, "conversations_owner_recent_idx is gone or was renamed"
    keys = " ".join(found.group(1).split())
    assert keys == "user_id, COALESCE(updated_at, created_at) DESC, conversation_id DESC"


def test_the_owner_recent_index_is_partial_on_the_default_filter():
    """It is partial *because* `archived_at IS NULL` is the default query.
    `include_archived=true` is the rare case and is allowed to sort."""
    index = _statements(STARTED_MODE_SQL).split("CREATE INDEX conversations_owner_recent_idx", 1)[1]
    assert "WHERE archived_at IS NULL" in index.split(";", 1)[0]


# ── What the migration must not do ───────────────────────────────────────────


def test_the_migration_opens_no_transaction_of_its_own():
    """`docs/migrations.md` section 3: the runner owns the transaction. A file
    that commits by itself never gets a ledger row."""
    statements = _statements(STARTED_MODE_SQL).upper()
    assert "BEGIN" not in statements and "COMMIT" not in statements


def test_no_migration_constrains_the_shape_of_a_conversation_id():
    """Requirement 2, and the compatibility rule under it: a client-minted
    `crypto.randomUUID()` is a valid conversation id, on every route, forever. A
    prefix CHECK on `conversation_id` — anywhere in the history — would make the
    ids already in production illegal, which is the one thing this bead may not
    do while claiming to preserve today's behaviour."""
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        text = _statements(migration.read_text(encoding="utf-8"))
        assert not re.search(r"conversation_id[^,;]*CHECK", text), (
            f"{migration.name} constrains the shape of a conversation id"
        )

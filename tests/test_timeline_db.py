"""The conversation timeline against a real PostgreSQL, and its twin (1kg.4.2).

The **shared behavioural suite**: one set of tests run twice, once against the
in-memory twin and once against PostgreSQL, so that a rule the two could
disagree about is asserted of both. Those tests carry no mark and run anywhere;
their `postgres` parameter carries `needs_db` and skips without a server.

Both worlds seed through `MessageStore` — `claim_conversation` and `append` —
so the seeding itself is parity rather than two different ways of making the
same rows.

What no fake can prove, and what the `needs_db` tests below are therefore for:
that a `chat.messages` row whose `mode` no `ChatMode` knows really exists (the
column carries no CHECK, and `StoredMessage` could never hold one), and that
rows written straight into a migrated database — the shape of every
conversation that predates this build — read back as exchanges saying
*not recorded*.

This slice reads and never writes: it adds no migration and issues no statement
against `chat.timeline_entries`, which slice A creates.

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_timeline_db.py -q
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service import timeline, timeline_store
from service.db import Database, InMemoryDatabase, PoolSettings
from service.history import InMemoryMessageStore, PostgresMessageStore
from service.timeline_store import (
    LEGACY_FAKE_WINDOW_ROWS,
    InMemoryTimelineStore,
    PostgresTimelineStore,
)

CONVERSATION = "conv-timeline-suite"
FOREIGN = "conv-of-another-gm"
UNCLAIMED = "conv-with-no-ownership-row"


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("timeline") as target:
        mig.migrate(target)
        yield target


@dataclass
class World:
    """A message store and a timeline store over one database, and two GMs."""

    kind: str
    db: Any
    messages: Any
    timeline: Any
    owner: int
    other_owner: int


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db: Any = InMemoryDatabase()
        messages = InMemoryMessageStore()
        yield World("fake", db, messages, InMemoryTimelineStore(db, messages=messages), 1, 2)
        return

    target = request.getfixturevalue("dsn")
    with connect(target) as conn:
        gms = [
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
                (email,),
            ).fetchone()[0]
            for email in ("gm@example.com", "other@example.com")
        ]
    database = Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))
    yield World(
        "postgres", database, PostgresMessageStore(db=database), PostgresTimelineStore(),
        int(gms[0]), int(gms[1]),
    )


def own_it(world: World, conversation_id: str = CONVERSATION, *, owner: int | None = None) -> None:
    world.messages.claim_conversation(conversation_id, world.owner if owner is None else owner)


def say_it(
    world: World, role: str, content: str, *,
    conversation_id: str = CONVERSATION, mode: str = "sage", suggestions: Any = None,
) -> None:
    world.messages.append(conversation_id, mode, role, content, suggestions=suggestions)


def a_short_conversation(world: World, exchanges: int = 4, conversation_id: str = CONVERSATION) -> None:
    own_it(world, conversation_id)
    for i in range(exchanges):
        say_it(world, "user", f"question {i}", conversation_id=conversation_id)
        say_it(world, "assistant", f"answer {i}", conversation_id=conversation_id)


# ── Ownership: one answer for missing, unowned and foreign ───────────────────


def test_owner_of_finds_the_gm_who_owns_the_conversation(world: World) -> None:
    own_it(world)
    with world.db.transaction() as unit:
        assert world.timeline.owner_of(unit, CONVERSATION) == world.owner


def test_owner_of_answers_none_for_a_conversation_that_was_never_claimed(world: World) -> None:
    """0001 declares `messages_conversation_fkey` NOT VALID, so a conversation
    can hold rows and have no ownership row at all. `None` is not "not yours",
    and the route answers 404 to both."""
    say_it(world, "user", "written before the ownership table existed", conversation_id=UNCLAIMED)
    with world.db.transaction() as unit:
        assert world.timeline.owner_of(unit, UNCLAIMED) is None


def test_owner_of_answers_none_for_a_conversation_that_does_not_exist(world: World) -> None:
    with world.db.transaction() as unit:
        assert world.timeline.owner_of(unit, "no-such-conversation") is None


def test_the_authorization_helper_refuses_all_three_with_the_same_named_error(world: World) -> None:
    """SEC-3, from one code path: missing, unowned and another user's are
    indistinguishable to the caller, in both worlds."""
    own_it(world, FOREIGN, owner=world.other_owner)
    say_it(world, "user", "not yours", conversation_id=FOREIGN)
    say_it(world, "user", "unowned", conversation_id=UNCLAIMED)
    with world.db.transaction() as unit:
        for conversation_id in ("no-such-conversation", UNCLAIMED, FOREIGN):
            with pytest.raises(timeline.ConversationNotFound):
                timeline.authorize(world.timeline, unit, conversation_id, user_id=world.owner)


def test_a_refused_read_leaves_the_transaction_usable_and_it_still_commits(world: World) -> None:
    """Finding G-9. The refusal must come from a guard, not from a failed
    statement: a transaction that has failed one is unusable for the rest of the
    request, and a deferred foreign key would surface at COMMIT anyway."""
    a_short_conversation(world)
    own_it(world, FOREIGN, owner=world.other_owner)
    say_it(world, "user", "not yours", conversation_id=FOREIGN)
    with world.db.transaction() as unit:
        with pytest.raises(timeline.ConversationNotFound):
            timeline.authorize(world.timeline, unit, FOREIGN, user_id=world.owner)
        # Same transaction, straight afterwards: still usable.
        assert world.timeline.owner_of(unit, CONVERSATION) == world.owner
        page = timeline.read_page(world.timeline, unit, CONVERSATION, limit=10, cursor=None)
    assert len(page.items) == 4


# ── The legacy window ────────────────────────────────────────────────────────


def test_the_legacy_window_comes_back_newest_first_and_never_longer_than_asked(
    world: World,
) -> None:
    a_short_conversation(world, exchanges=5)
    with world.db.transaction() as unit:
        rows = world.timeline.legacy_window(unit, CONVERSATION, before=None, limit_rows=4)
    assert len(rows) == 4
    assert [r.content for r in rows] == ["answer 4", "question 4", "answer 3", "question 3"]
    assert [r.id for r in rows] == sorted((r.id for r in rows), reverse=True)


def test_the_legacy_window_stops_strictly_below_the_position_it_is_given(world: World) -> None:
    a_short_conversation(world, exchanges=5)
    with world.db.transaction() as unit:
        everything = world.timeline.legacy_window(unit, CONVERSATION, before=None, limit_rows=50)
        pivot = everything[3]
        below = world.timeline.legacy_window(
            unit, CONVERSATION, before=(pivot.created_at, pivot.id), limit_rows=50
        )
    assert [r.id for r in below] == [r.id for r in everything[4:]]
    assert pivot.id not in {r.id for r in below}


def test_the_legacy_window_never_reaches_into_another_conversation(world: World) -> None:
    """C10: ownership is in the query. Every statement names the conversation."""
    a_short_conversation(world, exchanges=2)
    own_it(world, FOREIGN, owner=world.other_owner)
    for i in range(5):
        say_it(world, "user", f"someone else's {i}", conversation_id=FOREIGN)
    with world.db.transaction() as unit:
        rows = world.timeline.legacy_window(unit, CONVERSATION, before=None, limit_rows=100)
    assert len(rows) == 4
    assert all("someone else" not in r.content for r in rows)


def test_a_conversation_with_nothing_in_it_yields_an_empty_window(world: World) -> None:
    own_it(world)
    with world.db.transaction() as unit:
        assert world.timeline.legacy_window(unit, CONVERSATION, before=None, limit_rows=10) == []


def test_the_fakes_row_bound_is_far_above_anything_the_suite_seeds(world: World) -> None:
    """`LEGACY_FAKE_WINDOW_ROWS` stands in for a table PostgreSQL scans with an
    index. A conversation near it would make the twin and the real store
    disagree about what a page holds."""
    a_short_conversation(world, exchanges=30)
    with world.db.transaction() as unit:
        rows = world.timeline.legacy_window(unit, CONVERSATION, before=None, limit_rows=LEGACY_FAKE_WINDOW_ROWS)
    assert len(rows) == 60
    assert len(rows) * 10 < LEGACY_FAKE_WINDOW_ROWS


# ── The page, and the cursor walk ────────────────────────────────────────────


def _walk_the_store(world: World, limit: int) -> list[str]:
    """Every entry id of the conversation, newest first, at this page size."""
    seen: list[str] = []
    cursor: timeline.TimelineCursor | None = None
    for _ in range(500):
        with world.db.transaction() as unit:
            page = timeline.read_page(world.timeline, unit, CONVERSATION, limit=limit, cursor=cursor)
        assert len(page.items) <= limit
        seen.extend(entry.entry_id for entry in page.items)
        if page.next_cursor is None:
            return seen
        cursor = timeline.decode_cursor(page.next_cursor)
    raise AssertionError("the cursor walk did not terminate")


def test_the_whole_conversation_reads_back_the_same_at_every_page_size(world: World) -> None:
    """C3 and C8 together: the walk agrees with the unpaged read, and the two
    worlds agree with each other, because this is one test run twice."""
    own_it(world)
    say_it(world, "assistant", "an orphan before anything")
    for i in range(14):
        say_it(world, "user", f"question {i}")
        if i % 4 == 3:
            say_it(world, "user", f"asked again {i}")
        if i % 5 != 4:
            say_it(world, "assistant", f"answer {i}")

    with world.db.transaction() as unit:
        unpaged = timeline.read_page(world.timeline, unit, CONVERSATION, limit=100, cursor=None)
    assert unpaged.next_cursor is None
    expected = [entry.entry_id for entry in unpaged.items]
    assert len(expected) > 14

    for size in (1, 2, 3, 5, 7, 100):
        walked = _walk_the_store(world, size)
        assert walked == expected, f"page size {size} changed the list in the {world.kind} world"
        assert len(walked) == len(set(walked))


def test_a_page_boundary_inside_a_run_of_exchanges_keeps_every_prompt_with_its_answer(
    world: World,
) -> None:
    a_short_conversation(world, exchanges=6)
    with world.db.transaction() as unit:
        page = timeline.read_page(world.timeline, unit, CONVERSATION, limit=3, cursor=None)
    assert page.next_cursor is not None
    for entry in page.items:
        assert entry.entry_kind == "chat"
        assert entry.prompt is not None and entry.answer is not None


def test_an_adapted_answer_records_neither_answerable_nor_sources(world: World) -> None:
    a_short_conversation(world, exchanges=1)
    with world.db.transaction() as unit:
        page = timeline.read_page(world.timeline, unit, CONVERSATION, limit=10, cursor=None)
    answer = page.items[0].answer
    assert answer is not None
    assert answer.answerable is None and answer.sources is None


def test_the_stores_read_nothing_and_change_nothing_when_a_unit_rolls_back(world: World) -> None:
    """This slice writes nothing at all, and that is the assertion: a
    transaction that raises leaves both stores exactly as they were."""
    a_short_conversation(world, exchanges=2)
    with pytest.raises(RuntimeError):
        with world.db.transaction() as unit:
            timeline.read_page(world.timeline, unit, CONVERSATION, limit=10, cursor=None)
            raise RuntimeError("rolled back on purpose")
    with world.db.transaction() as unit:
        after = timeline.read_page(world.timeline, unit, CONVERSATION, limit=10, cursor=None)
        assert world.timeline.owner_of(unit, CONVERSATION) == world.owner
    assert [entry.entry_id for entry in after.items] != []


# ── What only a real database can show ───────────────────────────────────────


def _raw_rows(dsn: str, conversation_id: str, owner: int, rows: list[tuple[str, str, str]]) -> None:
    """Legacy `chat.messages` rows written straight into a migrated database —
    the shape of every conversation that predates this build."""
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s)",
            (conversation_id, owner),
        )
        for mode, role, content in rows:
            conn.execute(
                "INSERT INTO chat.messages (conversation_id, mode, role, content) "
                "VALUES (%s, %s, %s, %s)",
                (conversation_id, mode, role, content),
            )


@needs_db
def test_rows_written_by_raw_sql_read_back_as_exchanges_that_record_nothing(dsn: str) -> None:
    """C4. No backfill, no migration of old rows: the read model adapts them."""
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('legacy@example.com', 'x') "
            "RETURNING id"
        ).fetchone()[0]
    _raw_rows(dsn, CONVERSATION, int(owner), [
        ("sage", "user", "an old question"),
        ("sage", "assistant", "an old answer"),
        ("gm", "assistant", "an orphan answer"),
    ])
    database = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))
    with database.transaction() as unit:
        page = timeline.read_page(PostgresTimelineStore(), unit, CONVERSATION, limit=10, cursor=None)
    assert [e.prompt for e in page.items] == [None, "an old question"]
    assert [e.answer.text for e in page.items] == ["an orphan answer", "an old answer"]
    assert all(e.answer.answerable is None and e.answer.sources is None for e in page.items)
    assert page.next_cursor is None


@needs_db
def test_a_stored_mode_no_contract_knows_costs_one_entry_and_not_the_page(dsn: str) -> None:
    """`chat.messages.mode` carries no CHECK (0001), so this row is real — and
    it is why the store returns the columns as stored rather than coercing them
    into `StoredMessage`, which could never hold it."""
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('modes@example.com', 'x') "
            "RETURNING id"
        ).fetchone()[0]
    _raw_rows(dsn, CONVERSATION, int(owner), [
        ("sage", "user", "before"),
        ("sage", "assistant", "before"),
        ("oracle", "user", "a mode from the future"),
        ("sage", "user", ""),
        ("sage", "user", "after"),
        ("sage", "assistant", "after"),
    ])
    database = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))
    with database.transaction() as unit:
        page = timeline.read_page(PostgresTimelineStore(), unit, CONVERSATION, limit=10, cursor=None)
    assert [e.entry_kind for e in page.items] == ["chat", "opaque", "opaque", "chat"]
    for placeholder in page.items[1:3]:
        assert placeholder.reason.value == "unreadable"
        assert set(placeholder.model_dump()) == {
            "schema_version", "entry_kind", "entry_id", "created_at", "reason"
        }
    assert page.items[0].prompt == "after" and page.items[3].prompt == "before"

    # The stored rows are left exactly as they were: never repaired, never dropped.
    with connect(dsn) as conn:
        modes = conn.execute(
            "SELECT mode FROM chat.messages WHERE conversation_id = %s ORDER BY id", (CONVERSATION,)
        ).fetchall()
    assert [m[0] for m in modes] == ["sage", "sage", "oracle", "sage", "sage", "sage"]


@needs_db
def test_deleting_the_conversation_takes_its_messages_with_it(dsn: str) -> None:
    """C12, as far as this slice reaches: the entry table is slice A's, and the
    cascade this slice depends on is 0001's, unchanged."""
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('cascade@example.com', 'x') "
            "RETURNING id"
        ).fetchone()[0]
    _raw_rows(dsn, CONVERSATION, int(owner), [("sage", "user", "q"), ("sage", "assistant", "a")])
    with connect(dsn) as conn:
        conn.execute("DELETE FROM chat.conversations WHERE conversation_id = %s", (CONVERSATION,))
        left = conn.execute(
            "SELECT count(*) FROM chat.messages WHERE conversation_id = %s", (CONVERSATION,)
        ).fetchone()[0]
    assert int(left) == 0


# ── Ownership is in the query, statically ────────────────────────────────────

_NAMES_THE_CONVERSATION = re.compile(r"\bconversation_id\s*=\s*%s")


def _statements(module: Any) -> list[str]:
    """Every statement the module hands to `.execute(...)`, read out of its own
    syntax tree rather than grepped for.

    A grep over string literals matches this file's prose as readily as its SQL
    — the first version of this test failed on its own docstring — so the scan
    walks call sites instead, resolving the module-level string constants the
    statements are assembled from. A statement it cannot read is a failure, not
    a skip: a test that quietly stops looking is the thing it exists to prevent.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    constants: dict[str, str] = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def flatten(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        if isinstance(node, ast.JoinedStr):
            parts = [
                flatten(part.value if isinstance(part, ast.FormattedValue) else part)
                for part in node.values
            ]
            return None if any(part is None for part in parts) else "".join(parts)  # type: ignore[arg-type]
        return None

    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            text = flatten(node.args[0])
            assert text is not None, f"a statement this test could not read, at line {node.lineno}"
            found.append(text)
    return found


def test_every_statement_the_timeline_store_issues_names_the_conversation() -> None:
    """C10(c). A static read of the module's own call sites, paired with the
    behavioural cross-user tests above — which are what actually prove it."""
    statements = _statements(timeline_store)
    assert len(statements) >= 3, "no SQL found — this test would pass vacuously"
    for statement in statements:
        before_returning = re.split(r"\bRETURNING\b", statement)[0]
        assert _NAMES_THE_CONVERSATION.search(before_returning), (
            f"a statement that does not name the conversation: {statement!r}"
        )


def test_this_slice_issues_no_statement_against_the_entry_table() -> None:
    """Slice B adds no migration and `chat.timeline_entries` does not exist yet;
    a statement naming it would fail against every migrated database."""
    for statement in _statements(timeline_store):
        assert "timeline_entries" not in statement


def test_the_read_model_holds_no_sql_at_all() -> None:
    """Requirement 10: every statement lives in `service/timeline_store.py`."""
    assert _statements(timeline) == []

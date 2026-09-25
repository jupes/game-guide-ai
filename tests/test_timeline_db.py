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
column carries no CHECK, and `StoredMessage` could never hold one); that rows
written straight into a migrated database — the shape of every conversation
that predates this build — read back as exchanges saying *not recorded*; that a
payload written by raw SQL, bypassing every check `append` makes, is served
through `entry_or_opaque` and left exactly as it was; and what the database's
own deletion edges do to entry rows.

Slice B wrote the read half against legacy rows alone. Slice A adds
`chat.timeline_entries`, `append`, `entry_window` and
`covered_message_ids`, and the merge of the two sources.

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_timeline_db.py -q
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database
from pydantic import TypeAdapter

from service import migrations as mig
from service import timeline, timeline_store
from service.db import Database, InMemoryDatabase, PoolSettings
from service.history import InMemoryMessageStore, PostgresMessageStore
from service.timeline_store import (
    LEGACY_FAKE_WINDOW_ROWS,
    InMemoryTimelineStore,
    PostgresTimelineStore,
)
from service.workbench_contracts import (
    CONTRACT_VERSION,
    ChatEntry,
    EntryKind,
    OpaqueId,
    OpaqueReason,
    entry_or_opaque,
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
    #: The PostgreSQL world's DSN, for reading a row the way no store does.
    dsn: str | None = None


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
        int(gms[0]), int(gms[1]), target,
    )


def own_it(world: World, conversation_id: str = CONVERSATION, *, owner: int | None = None) -> None:
    world.messages.claim_conversation(conversation_id, world.owner if owner is None else owner)


def say_it(
    world: World, role: str, content: str, *,
    conversation_id: str = CONVERSATION, mode: str = "sage", suggestions: Any = None,
) -> int:
    message_id = world.messages.append(conversation_id, mode, role, content, suggestions=suggestions)
    assert isinstance(message_id, int), "MessageStore.append answers the new row's id"
    return message_id


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


def test_owner_of_answers_none_for_a_conversation_with_no_ownership_row(world: World) -> None:
    """`None` is not "not yours", and the route answers 404 to both.

    Seeded with no message rows on purpose, and CI is why. `NOT VALID` on
    `messages_conversation_fkey` exempts the rows that were already there — it
    still **enforces every new insert** — so a fresh database refuses a message
    whose conversation row is missing, while `InMemoryMessageStore` (a plain
    dict and a plain list, and not this bead's to change) accepts one. The
    legacy shape those two disagree about is real, and it is proved against
    PostgreSQL by `test_a_conversation_that_predates_the_ownership_table…`
    below, where it can be created the way it actually arose.
    """
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


def test_a_unit_that_rolls_back_leaves_no_entry_and_frees_the_rows_it_linked(world: World) -> None:
    """`on_rollback` in the twin, a rolled-back transaction in PostgreSQL: the
    same observable answer. The page is what it was before the unit began, and
    the message rows the discarded entry linked can be linked again."""
    a_short_conversation(world, exchanges=2)
    asked, answered = say_it(world, "user", "asked again"), say_it(world, "assistant", "answered again")
    with world.db.transaction() as unit:
        before = timeline.read_page(world.timeline, unit, CONVERSATION, limit=10, cursor=None)
    entry, moment = _chat_entry("asked again", "answered again")
    with pytest.raises(RuntimeError), world.db.transaction() as unit:
        world.timeline.append(unit, CONVERSATION, entry, moment, owner_id=world.owner,
                              user_message_id=asked, assistant_message_id=answered)
        assert len(world.timeline.entry_window(unit, CONVERSATION, before=None, limit=10)) == 1
        raise RuntimeError("rolled back on purpose")
    with world.db.transaction() as unit:
        after = timeline.read_page(world.timeline, unit, CONVERSATION, limit=10, cursor=None)
        assert world.timeline.entry_window(unit, CONVERSATION, before=None, limit=10) == []
        assert world.timeline.covered_message_ids(unit, CONVERSATION, [asked, answered]) == set()
    assert after == before
    _store(world, *_chat_entry("asked again", "answered again"), user=asked, assistant=answered)


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


def _a_legacy_orphan(dsn: str, conversation_id: str, rows: list[tuple[str, str, str]]) -> None:
    """Message rows whose conversation row does not exist — the cold-start hole
    0001 left behind, reproduced the only way it can be.

    `messages_conversation_fkey` is `NOT VALID`, which exempts the rows that
    were already there and enforces every new one, so these rows can only be
    made the way the real ones were: while the constraint is not on the table.
    It goes back exactly as 0001 declares it, `NOT VALID` included, so what the
    test then reads is a database in the state a real one is in.

    The restore is in a `finally`. `connect()` is `autocommit=True`, so a
    failure between the two `ALTER`s would otherwise leave the constraint off
    for the rest of the test. It cannot bleed past the test — `throwaway_database`
    gives every one its own uuid-named database and drops it — but a reader
    should not have to establish that to trust the helper.
    """
    with connect(dsn) as conn:
        conn.execute("ALTER TABLE chat.messages DROP CONSTRAINT messages_conversation_fkey")
        try:
            for mode, role, content in rows:
                conn.execute(
                    "INSERT INTO chat.messages (conversation_id, mode, role, content) "
                    "VALUES (%s, %s, %s, %s)",
                    (conversation_id, mode, role, content),
                )
        finally:
            conn.execute(
                "ALTER TABLE chat.messages ADD CONSTRAINT messages_conversation_fkey "
                "FOREIGN KEY (conversation_id) REFERENCES chat.conversations (conversation_id) "
                "ON DELETE CASCADE NOT VALID"
            )


@needs_db
def test_a_conversation_that_predates_the_ownership_table_is_refused_not_claimed(dsn: str) -> None:
    """The case §8.1 decided: this route answers 404 for such a conversation
    until some other route claims it, and it never claims one itself.

    The rows are still there and still readable — the read model is not what is
    withholding them — so `GET …/messages`, which does claim, keeps working
    exactly as it did. That contrast is the whole point of the rule.
    """
    _a_legacy_orphan(dsn, UNCLAIMED, [("sage", "user", "asked before ownership existed"),
                                      ("sage", "assistant", "answered before ownership existed")])
    database = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))
    store = PostgresTimelineStore()
    with database.transaction() as unit:
        assert store.owner_of(unit, UNCLAIMED) is None
        with pytest.raises(timeline.ConversationNotFound):
            timeline.authorize(store, unit, UNCLAIMED, user_id=1)
        # The transaction is still usable: a guard refused, not a statement.
        rows = store.legacy_window(unit, UNCLAIMED, before=None, limit_rows=10)
    assert [r.content for r in rows] == ["answered before ownership existed",
                                         "asked before ownership existed"]

    # And nothing was minted on the way through (SEC-3: no claim, no enumeration).
    with connect(dsn) as conn:
        claimed = conn.execute(
            "SELECT count(*) FROM chat.conversations WHERE conversation_id = %s", (UNCLAIMED,)
        ).fetchone()[0]
    assert int(claimed) == 0


@needs_db
def test_a_fresh_database_refuses_a_message_whose_conversation_row_is_missing(dsn: str) -> None:
    """Why the shared suite cannot seed the case above, stated as a test rather
    than as a comment: `NOT VALID` exempts old rows and enforces new ones, so
    the hole cannot be reopened by an ordinary write."""
    import psycopg

    with connect(dsn) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO chat.messages (conversation_id, mode, role, content) "
            "VALUES (%s, 'sage', 'user', 'x')",
            ("a-conversation-nobody-created",),
        )


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


@needs_db
def test_two_rows_on_one_instant_still_come_back_in_a_total_order(dsn: str) -> None:
    """M-3. `_LEGACY_ORDER` ends `, id DESC`, and nothing proved it.

    Every other PostgreSQL test here writes its rows through separate
    statements, so `now()` differs on each and `ORDER BY created_at DESC`
    alone already orders them — drop the tiebreak and they all stay green.
    The twin's equivalent dies only because `InMemoryMessageStore.append`
    stamps `datetime.now(UTC)` in a tight loop and really does tie. A real
    conversation ties for the same reason, and an order PostgreSQL is free to
    choose is an order that can differ between two reads of the same page:
    an entry repeated on one side of a cursor and missing on the other.

    So: two rows, one explicit instant, and the window must still be a total
    order. Both halves are asserted — the raw window the SQL returns, and the
    page the read model builds on top of it.
    """
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('tied@example.com', 'x') "
            "RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s)",
            (CONVERSATION, int(owner)),
        )
        tied = conn.execute("SELECT now()").fetchone()[0]
        ids = [
            conn.execute(
                "INSERT INTO chat.messages (conversation_id, mode, role, content, created_at) "
                "VALUES (%s, 'sage', %s, %s, %s) RETURNING id",
                (CONVERSATION, role, content, tied),
            ).fetchone()[0]
            for role, content in (("user", "asked"), ("assistant", "answered"))
        ]
        stamps = conn.execute(
            "SELECT count(DISTINCT created_at) FROM chat.messages WHERE conversation_id = %s",
            (CONVERSATION,),
        ).fetchone()[0]
    assert int(stamps) == 1, "the two rows must share one instant or this test proves nothing"

    database = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))
    store = PostgresTimelineStore()
    with database.transaction() as unit:
        window = store.legacy_window(unit, CONVERSATION, before=None, limit_rows=10)
        bounded = store.legacy_window(unit, CONVERSATION, before=None, limit_rows=1)
        page = timeline.read_page(store, unit, CONVERSATION, limit=10, cursor=None)
    assert [row.id for row in window] == [int(ids[1]), int(ids[0])], (
        "newest first, and on a tie the higher id first — the ordering key is total"
    )
    assert [row.role for row in window] == ["assistant", "user"]
    # A bound of one must take the newest of the tied pair, not an arbitrary one.
    assert [row.id for row in bounded] == [int(ids[1])]
    # And the pair still adapts to one exchange rather than two half ones.
    assert [entry.prompt for entry in page.items] == ["asked"]
    assert [entry.answer.text for entry in page.items] == ["answered"]


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


def test_the_store_writes_its_own_table_and_only_reads_everyone_elses() -> None:
    """The boundary with `1kg.2.4`, as a property of the statements. The one
    write is an `INSERT` into `chat.timeline_entries`; `chat.conversations` and
    `chat.messages` are only ever read, and `1kg.2.4`'s columns are never named."""
    statements = _statements(timeline_store)
    writes = [s for s in statements if re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|TRUNCATE)\b", s, re.I)]
    assert [" ".join(s.split()[:3]) for s in writes] == ["INSERT INTO chat.timeline_entries"]
    for statement in statements:
        for column in ("campaign_id", "title", "updated_at", "archived_at", "started_mode"):
            assert not re.search(rf"\b{column}\b", statement), f"names 1kg.2.4's {column}: {statement!r}"
        assert "FOR UPDATE" not in statement and "FOR SHARE" not in statement, "the timeline takes no lock"


def test_the_read_model_holds_no_sql_at_all() -> None:
    """Requirement 10: every statement lives in `service/timeline_store.py`."""
    assert _statements(timeline) == []


# ── Slice A: the migration, pinned against the Python that writes it ─────────

#: The migration's number appears in Python exactly ONCE, here (ruling R-7).
#: The lead renumbers at merge if a parallel bead takes it first, and this is
#: the only line that moves with it.
MIGRATION_FILENAME = "0010_timeline_entries.sql"
MIGRATIONS = Path(__file__).resolve().parents[1] / "service" / "sql" / "migrations"

#: The entry kinds `chat.timeline_entries.entry_kind`'s CHECK admits, AS OF THIS
#: MIGRATION — deliberately not read off the live `EntryKind`. A released
#: migration is never edited, and the wire contract already says a kind is
#: coming (the attached cue, with `1kg.1.2`). Compared with the live enum, this
#: file would go red the day another bead adds that member: a failure here
#: caused by a change there. So the CHECK must equal THIS list exactly, and this
#: list must be a SUBSET of the live enum — a kind renamed or removed still goes
#: red, an added one does not, and the added one needs its own migration.
KINDS_AS_OF_THIS_MIGRATION = ("chat", "tool", "edit", "session_divider", "opaque")


def _migration_sql() -> str:
    """The migration without its comment lines — what the server reads. The
    header quotes several of the words the assertions below look for."""
    text = (MIGRATIONS / MIGRATION_FILENAME).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


def test_the_entry_kind_check_is_exactly_the_kinds_of_this_migration() -> None:
    found = re.search(r"entry_kind IN \(([^)]*)\)", _migration_sql())
    assert found is not None, f"{MIGRATION_FILENAME} no longer constrains entry_kind"
    assert tuple(v.strip().strip("'") for v in found.group(1).split(",")) == KINDS_AS_OF_THIS_MIGRATION


def test_every_kind_of_this_migration_is_still_one_the_contract_knows() -> None:
    assert set(KINDS_AS_OF_THIS_MIGRATION) <= {kind.value for kind in EntryKind}


def test_the_entry_id_check_is_generated_by_the_module_that_mints_the_ids() -> None:
    """SEC-4's shape is spelled once, in Python, and the SQL is held to it."""
    expected = timeline_store.entry_id_check_regex()
    assert f"CHECK (entry_id ~ '{expected}')" in _migration_sql()
    assert expected.startswith(f"^{timeline_store.ENTRY_PREFIX}")


def test_one_legacy_row_is_covered_by_at_most_one_entry_and_both_links_cascade() -> None:
    sql = " ".join(_migration_sql().split())
    for column in ("user_message_id", "assistant_message_id"):
        assert f"{column} BIGINT REFERENCES chat.messages (id) ON DELETE CASCADE" in sql
        assert re.search(
            rf"CREATE UNIQUE INDEX \w+ ON chat\.timeline_entries \({column}\) WHERE {column} IS NOT NULL", sql
        ), f"{column} is not unique among the entries that set it"
    assert (
        "conversation_id TEXT NOT NULL REFERENCES chat.conversations (conversation_id) ON DELETE CASCADE"
        in sql
    )
    assert "NOT VALID" not in sql, "the table is new: there is no legacy row to spare the check"
    assert re.search(
        r"CREATE INDEX \w+ ON chat\.timeline_entries \(conversation_id, created_at DESC, seq DESC\)", sql
    ), "the page read is not served by an index on its own ordering key"


def test_the_migration_carries_no_transaction_control_and_the_runner_knows_it() -> None:
    assert not re.search(r"^\s*(begin|start\s+transaction|commit|rollback|end)\b", _migration_sql(), re.I | re.M)
    assert MIGRATION_FILENAME in {m.filename for m in mig.discover()}, (
        "not pinned in manifest.txt: run `python -m service.migrations manifest`"
    )


# ── Slice A: minting an entry id (requirement 2) ─────────────────────────────


def test_two_hundred_minted_ids_are_distinct_well_formed_and_not_sequential() -> None:
    minted = [timeline_store.new_entry_id() for _ in range(200)]
    assert len(set(minted)) == 200
    shape = re.compile(timeline_store.entry_id_check_regex())
    opaque = TypeAdapter(OpaqueId)
    for entry_id in minted:
        assert shape.fullmatch(entry_id)
        assert opaque.validate_python(entry_id) == entry_id
    bodies = [entry_id.removeprefix(timeline_store.ENTRY_PREFIX) for entry_id in minted]
    assert bodies != sorted(bodies) and bodies != sorted(bodies, reverse=True)
    assert max(len(_common_prefix(a, b)) for a, b in zip(bodies, bodies[1:], strict=False)) < 8


def _common_prefix(a: str, b: str) -> str:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


def test_a_minted_id_can_never_pass_for_a_legacy_one() -> None:
    """A legacy entry id is a decimal `chat.messages.id`; a minted one is
    prefixed. Neither shape admits the other, so the two can never collide."""
    shape = re.compile(timeline_store.entry_id_check_regex())
    assert not shape.fullmatch("42")
    assert not timeline_store.new_entry_id().isdecimal()


# ── Slice A: appending typed entries (requirements 3 and 4) ──────────────────

CANARY = "Zx9CanaryQ7"
#: An owner id as it would be smuggled into a payload; digits nothing else here
#: uses, so a refusal that repeated it would be caught.
SMUGGLED_OWNER_ID = 424242


def _chat_entry(
    prompt: str | None = "a question", answer_text: str = "an answer", *,
    moment: datetime | None = None, entry_id: str | None = None,
) -> tuple[dict[str, Any], datetime]:
    """A chat entry as `/chat` builds one: a minted id, one instant for the
    entry and its answer, and a complete outcome."""
    at = moment or datetime.now(UTC)
    return {
        "schema_version": CONTRACT_VERSION, "entry_kind": "chat",
        "entry_id": entry_id or timeline_store.new_entry_id(), "created_at": at,
        "mode": "sage", "prompt": prompt,
        "answer": {
            "text": answer_text, "answerable": True,
            "sources": [{"book": "mm-5e", "snippet": f"the snippet behind {answer_text}"}],
            "created_at": at,
        },
    }, at


def _edit_entry() -> tuple[dict[str, Any], datetime]:
    """A valid `edit` entry, taken from the wire contract's own fixtures (read,
    never edited) and re-minted, so the two items below that only an edit entry
    can carry are refused for what they are rather than for being malformed."""
    fixtures = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts" / "workbench" / "v1" / "TimelineEntry.json")
        .read_text(encoding="utf-8")
    )["valid"]
    edits = [f["value"] for f in fixtures if f["value"].get("entry_kind") == "edit"]
    example = next(e for e in edits if e["scope"]["kind"] == "selection")
    at = datetime.now(UTC)
    return {**example, "entry_id": timeline_store.new_entry_id(), "created_at": at}, at


def _append_in(world: World, unit: Any, entry: Any, moment: datetime, *,
               conversation_id: str = CONVERSATION, owner: int | None = None,
               user: int | None = None, assistant: int | None = None) -> str:
    entry_id: str = world.timeline.append(
        unit, conversation_id, entry, moment, owner_id=world.owner if owner is None else owner,
        user_message_id=user, assistant_message_id=assistant,
    )
    return entry_id


def _store(world: World, entry: Any, moment: datetime, **links: Any) -> str:
    with world.db.transaction() as unit:
        return _append_in(world, unit, entry, moment, **links)


def _a_turn(world: World, n: int, *, entry: bool = True, answered: bool = True,
            moment: datetime | None = None) -> str | None:
    """One `/chat` turn as the handler writes it — the user row, the assistant
    row, then one entry linking both — or one of its partial shapes."""
    asked = say_it(world, "user", f"question {n}")
    reply = say_it(world, "assistant", f"answer {n}") if answered else None
    if not entry:
        return None
    return _store(world, *_chat_entry(f"question {n}", f"answer {n}", moment=moment), user=asked, assistant=reply)


def _entries(world: World, conversation_id: str = CONVERSATION) -> list[Any]:
    with world.db.transaction() as unit:
        rows: list[Any] = world.timeline.entry_window(unit, conversation_id, before=None, limit=100)
    return rows


def _read_all(world: World, conversation_id: str = CONVERSATION) -> Any:
    with world.db.transaction() as unit:
        return timeline.read_page(world.timeline, unit, conversation_id, limit=100, cursor=None)


def test_append_on_the_message_store_answers_the_new_rows_id(world: World) -> None:
    """R-1: the one change to `service/history.py`, a return value. Proved in
    both worlds, because the PostgreSQL half is a `RETURNING id`."""
    own_it(world)
    asked, answered = say_it(world, "user", "one"), say_it(world, "assistant", "two")
    assert answered > asked
    with world.db.transaction() as unit:
        rows = world.timeline.legacy_window(unit, CONVERSATION, before=None, limit_rows=10)
    assert [(r.id, r.content) for r in rows] == [(answered, "two"), (asked, "one")]


def test_an_appended_entry_reads_back_as_exactly_the_entry_it_was(world: World) -> None:
    """C5 and C6: stored at `CONTRACT_VERSION`, and served back through
    `entry_or_opaque` as the real entry — never a placeholder."""
    own_it(world)
    entry, moment = _chat_entry()
    assert _store(world, entry, moment) == entry["entry_id"]
    [row] = _entries(world)
    assert (row.entry_id, row.created_at) == (entry["entry_id"], moment)
    assert row.payload["schema_version"] == CONTRACT_VERSION
    served = entry_or_opaque(row.payload, entry_id=row.entry_id, created_at=row.created_at)
    assert isinstance(served, ChatEntry)
    assert served == ChatEntry.model_validate(entry)


@pytest.mark.parametrize("skew", ["one microsecond later", "no offset at all"])
def test_an_entry_whose_time_is_not_the_rows_is_refused_before_any_statement(world: World, skew: str) -> None:
    """C6. The row is the authority on time; a payload that disagrees would be
    stored as a lie about when the turn happened."""
    own_it(world)
    entry, moment = _chat_entry()
    row_time = moment + timedelta(microseconds=1) if skew.startswith("one") else moment.replace(tzinfo=None)
    with world.db.transaction() as unit:
        with pytest.raises(timeline_store.EntryMismatch):
            world.timeline.append(unit, CONVERSATION, entry, row_time, owner_id=world.owner)
        # Refused before any statement ran: the same unit is still usable.
        _append_in(world, unit, *_chat_entry("still usable"))
    assert [r.payload["prompt"] for r in _entries(world)] == ["still usable"]


def test_an_entry_whose_id_no_row_can_carry_is_refused_before_any_statement(world: World) -> None:
    """C6. The id column is written from the payload's own `entry_id`, so the
    two cannot disagree; what is refused is an id this module did not mint — a
    legacy decimal one here — which the column's CHECK would refuse with a
    failed statement, leaving the caller's transaction unusable."""
    own_it(world)
    with world.db.transaction() as unit:
        with pytest.raises(timeline_store.EntryMismatch):
            world.timeline.append(unit, CONVERSATION, *_chat_entry(entry_id="42"), owner_id=world.owner)
        _append_in(world, unit, *_chat_entry("still usable"))
    assert [r.payload["prompt"] for r in _entries(world)] == ["still usable"]


def test_a_missing_an_unclaimed_and_a_foreign_conversation_are_refused_alike(world: World) -> None:
    """C8 and C10(a): the same named refusal in both worlds, from a guard in
    the statement — so the transaction is still usable and commits (G-9)."""
    own_it(world)
    own_it(world, FOREIGN, owner=world.other_owner)
    told = set()
    with world.db.transaction() as unit:
        for conversation_id in ("no-such-conversation", UNCLAIMED, FOREIGN):
            with pytest.raises(timeline_store.EntryNotStored) as refused:
                world.timeline.append(unit, conversation_id, *_chat_entry(), owner_id=world.owner)
            told.add((type(refused.value), str(refused.value)))
        _append_in(world, unit, *_chat_entry("mine"))
    assert len(told) == 1, "three refusals, one answer"
    assert _entries(world, FOREIGN) == [] and _entries(world, UNCLAIMED) == []
    assert [r.payload["prompt"] for r in _entries(world)] == ["mine"]


def test_a_message_row_that_is_not_this_conversations_is_never_linked(world: World) -> None:
    """A linked message row is a parent too, and G-9 applies to it: the guard
    is in the statement, never a foreign-key violation caught afterwards."""
    own_it(world)
    own_it(world, FOREIGN, owner=world.other_owner)
    theirs = say_it(world, "user", "not yours", conversation_id=FOREIGN)
    with world.db.transaction() as unit:
        for link in ({"user": theirs}, {"assistant": theirs}, {"user": 10**12}):
            with pytest.raises(timeline_store.EntryNotStored):
                _append_in(world, unit, *_chat_entry(), **link)
        _append_in(world, unit, *_chat_entry("still usable"))
    assert [r.payload["prompt"] for r in _entries(world)] == ["still usable"]


def test_one_legacy_row_is_carried_by_at_most_one_entry_and_an_entry_is_stored_once(world: World) -> None:
    own_it(world)
    asked = say_it(world, "user", "q")
    first, moment = _chat_entry("q")
    _store(world, first, moment, user=asked)
    with world.db.transaction() as unit:
        with pytest.raises(timeline_store.EntryNotStored):
            _append_in(world, unit, *_chat_entry("q, again"), user=asked)
        with pytest.raises(timeline_store.EntryNotStored):
            _append_in(world, unit, first, moment)
        _append_in(world, unit, *_chat_entry("still usable"))
    assert [r.payload["prompt"] for r in _entries(world)] == ["still usable", "q"]


def test_the_entry_window_is_newest_first_bounded_and_strictly_below_its_position(world: World) -> None:
    own_it(world)
    base = datetime.now(UTC)
    ids = [_store(world, *_chat_entry(f"q{i}", moment=base + timedelta(seconds=i))) for i in range(5)]
    with world.db.transaction() as unit:
        top = world.timeline.entry_window(unit, CONVERSATION, before=None, limit=3)
        below = world.timeline.entry_window(unit, CONVERSATION, before=(top[1].created_at, top[1].seq), limit=10)
    assert [r.entry_id for r in top] == ids[::-1][:3]
    assert [r.entry_id for r in below] == ids[::-1][2:]


def test_two_entries_on_one_instant_still_come_back_in_a_total_order(world: World) -> None:
    """`seq` is the tiebreak: without it the two worlds — or two reads of one
    page — could disagree about which of a tied pair comes first."""
    own_it(world)
    moment = datetime.now(UTC)
    first = _store(world, *_chat_entry("first", moment=moment))
    second = _store(world, *_chat_entry("second", moment=moment))
    with world.db.transaction() as unit:
        both = world.timeline.entry_window(unit, CONVERSATION, before=None, limit=10)
        one = world.timeline.entry_window(unit, CONVERSATION, before=None, limit=1)
        rest = world.timeline.entry_window(unit, CONVERSATION, before=(one[0].created_at, one[0].seq), limit=10)
    assert [r.entry_id for r in both] == [second, first]
    assert [r.entry_id for r in one] == [second] and [r.entry_id for r in rest] == [first]


def test_no_read_reaches_into_another_users_conversation(world: World) -> None:
    """C10(a), for the three reads: another user's rows are an empty answer."""
    own_it(world)
    own_it(world, FOREIGN, owner=world.other_owner)
    theirs = [say_it(world, "user", "their q", conversation_id=FOREIGN),
              say_it(world, "assistant", "their a", conversation_id=FOREIGN)]
    _store(world, *_chat_entry("their q"), conversation_id=FOREIGN, owner=world.other_owner,
           user=theirs[0], assistant=theirs[1])
    with world.db.transaction() as unit:
        assert world.timeline.entry_window(unit, CONVERSATION, before=None, limit=10) == []
        assert world.timeline.legacy_window(unit, CONVERSATION, before=None, limit_rows=10) == []
        assert world.timeline.covered_message_ids(unit, CONVERSATION, theirs) == set()
        assert world.timeline.covered_message_ids(unit, FOREIGN, theirs) == set(theirs)


def test_covered_message_ids_names_exactly_the_rows_an_entry_links(world: World) -> None:
    own_it(world)
    linked = [say_it(world, "user", "q1"), say_it(world, "assistant", "a1")]
    loose = [say_it(world, "user", "q2"), say_it(world, "assistant", "a2")]
    alone = say_it(world, "user", "q3")
    _store(world, *_chat_entry("q1"), user=linked[0], assistant=linked[1])
    _store(world, *_chat_entry("q3"), user=alone)
    with world.db.transaction() as unit:
        assert world.timeline.covered_message_ids(unit, CONVERSATION, [*linked, *loose, alone]) == {*linked, alone}
        assert world.timeline.covered_message_ids(unit, CONVERSATION, []) == set()


def test_an_entry_a_unit_has_not_committed_is_invisible_to_a_second_reader(world: World) -> None:
    """C8, commit-time visibility: `Staging` in the twin, READ COMMITTED in
    PostgreSQL. The second unit only reads."""
    own_it(world)
    asked = say_it(world, "user", "q")
    with world.db.transaction() as writer:
        entry_id = _append_in(world, writer, *_chat_entry("q"), user=asked)
        mine = world.timeline.entry_window(writer, CONVERSATION, before=None, limit=9)
        assert [r.entry_id for r in mine] == [entry_id], "its own write, yes"
        with world.db.transaction() as reader:
            assert world.timeline.entry_window(reader, CONVERSATION, before=None, limit=9) == []
            assert world.timeline.covered_message_ids(reader, CONVERSATION, [asked]) == set()
    assert [r.entry_id for r in _entries(world)] == [entry_id]


# ── Slice A: what may never be stored (requirement 4, C7) ────────────────────

_Build = Callable[[], tuple[dict[str, Any], datetime]]
_Smuggle = Callable[[dict[str, Any]], None]

#: One item per line of requirement 4's list. Each is refused by the contract's
#: own `extra="forbid"`, and this pins that as behaviour.
NEVER_STORED: dict[str, tuple[_Build, _Smuggle]] = {
    "a document body": (_edit_entry, lambda e: e["document"].__setitem__("body", CANARY)),
    "an edit's selected text": (_edit_entry, lambda e: e["scope"].__setitem__("selected_text", CANARY)),
    "the prompt the server assembled": (
        _chat_entry, lambda e: e["answer"].__setitem__("assembled_prompt", f"SYSTEM: {CANARY}")),
    "attachment text": (
        _chat_entry, lambda e: e.__setitem__("attachments", [{"filename": "n.txt", "extracted_text": CANARY}])),
    "a provider payload": (
        _chat_entry, lambda e: e["answer"].__setitem__("provider_response", {"choices": [{"text": CANARY}]})),
    "the owner's user id": (_chat_entry, lambda e: e.__setitem__("user_id", SMUGGLED_OWNER_ID)),
}


@pytest.mark.parametrize("what", sorted(NEVER_STORED))
def test_what_an_entry_may_never_carry_is_refused_by_where_and_never_by_what(world: World, what: str) -> None:
    own_it(world)
    build, smuggle = NEVER_STORED[what]
    entry, moment = build()
    smuggle(entry)
    value = str(SMUGGLED_OWNER_ID) if what == "the owner's user id" else CANARY
    with world.db.transaction() as unit, pytest.raises(timeline_store.EntryInvalid) as refused:
        world.timeline.append(unit, CONVERSATION, entry, moment, owner_id=world.owner)
    told = f"{refused.value} {refused.value!r} {refused.value.fields}"
    assert not [value[i:i + 4] for i in range(len(value) - 3) if value[i:i + 4] in told]
    assert "(undeclared)" in refused.value.fields[0]
    assert refused.value.__cause__ is None and refused.value.__suppress_context__, "the input rides the chain"
    assert _entries(world) == []


def _raw_entry_rows(world: World) -> str:
    """Every stored entry row, every column, as text — read the way no store
    reads it, so nothing a store leaves out can hide."""
    if world.dsn is None:
        return json.dumps([vars(r) for r in world.timeline._rows._rows.values()], default=str)
    with connect(world.dsn) as conn:
        return json.dumps([r[0] for r in conn.execute("SELECT row_to_json(t) FROM chat.timeline_entries t")])


def test_no_stored_row_holds_a_digest_of_any_text_it_was_given(world: World) -> None:
    """ED-26: no value derived from field text outlives the text — no hash,
    digest, fingerprint or checksum of a prompt, an answer or a snippet."""
    own_it(world)
    entry, moment = _chat_entry(f"prompt {CANARY}", f"answer {CANARY}")
    _store(world, entry, moment)
    raw = _raw_entry_rows(world)
    assert CANARY in raw, "the row is read, or this test proves nothing"
    texts = [entry["prompt"], entry["answer"]["text"], entry["answer"]["sources"][0]["snippet"]]
    for text in texts:
        for algorithm in ("md5", "sha1", "sha224", "sha256", "sha384", "sha512", "blake2b", "blake2s", "sha3_256"):
            digest = hashlib.new(algorithm, text.encode("utf-8")).digest()
            for shown in (digest.hex(), base64.b64encode(digest).decode(), base64.urlsafe_b64encode(digest).decode()):
                assert shown.rstrip("=") not in raw, f"a {algorithm} digest of stored text"
    assert not re.search(r"(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])", raw), "something hash-shaped is stored"


# ── Slice A: the merged page (requirements 6 to 8, C3, C8, C11) ──────────────


def test_a_turn_with_an_entry_is_read_once_as_the_entry_and_never_as_its_rows(world: World) -> None:
    own_it(world)
    _a_turn(world, 0, entry=False)
    stored = _a_turn(world, 1)
    _a_turn(world, 2, entry=False)
    page = _read_all(world)
    # By prompt, not by position: three turns written in a tight loop can share
    # a clock tick, and the order of a tie is the ordering key's business —
    # which the walks below pin — not this test's.
    by_prompt = {e.prompt: e for e in page.items}
    assert len(page.items) == len(by_prompt) == 3 and page.next_cursor is None
    assert by_prompt["question 1"].entry_id == stored
    assert by_prompt["question 1"].answer.answerable is True and by_prompt["question 1"].answer.sources
    for adapted in (by_prompt["question 0"], by_prompt["question 2"]):
        assert adapted.entry_id.isdecimal()
        assert adapted.answer.answerable is None and adapted.answer.sources is None


def test_each_of_the_four_partial_writes_reads_back_as_exactly_one_exchange(world: World) -> None:
    """Requirement 8's table, at the store: what succeeded, and what the page
    shows for it. (The same four through `POST /chat` are in
    `service/tests/test_timeline_write.py`.)"""
    own_it(world)
    complete = _a_turn(world, 0)
    _a_turn(world, 1, entry=False)
    user_only_with_entry = _a_turn(world, 2, answered=False)
    _a_turn(world, 3, answered=False, entry=False)
    items = _read_all(world).items
    by_prompt = {e.prompt: e for e in items}
    assert len(items) == len(by_prompt) == 4, "one exchange per turn, whatever succeeded"
    first, second, third, fourth = (by_prompt[f"question {n}"] for n in range(4))
    assert first.entry_id == complete and first.answer.answerable is True
    assert second.entry_id.isdecimal() and second.answer.text == "answer 1"
    assert second.answer.answerable is None and second.answer.sources is None
    assert third.entry_id == user_only_with_entry and third.answer.text == "answer 2"
    assert third.answer.answerable is True, "the entry carries the outcome; its orphan user row is suppressed"
    assert fourth.entry_id.isdecimal() and fourth.answer is None


def test_a_mixed_conversation_reads_back_the_same_at_every_page_size(world: World) -> None:
    """C3 and C8 together, over both sources: stored entries, adapted pairs, an
    orphan answer, two prompts in a row, and both partial writes — with a page
    boundary on every size from 1 to 10. The walk equals the unpaged read, no
    entry appears twice, none is missing, and no turn renders twice."""
    own_it(world)
    say_it(world, "assistant", "an orphan before anything")
    for i in range(26):
        shape = i % 6
        if shape == 4:
            say_it(world, "user", f"asked and abandoned {i}")
        _a_turn(world, i, entry=shape in (0, 2, 4, 5), answered=shape not in (2, 3))
        if shape == 5:
            say_it(world, "assistant", f"an orphan answer {i}")
    unpaged = _read_all(world)
    assert unpaged.next_cursor is None
    expected = [e.entry_id for e in unpaged.items]
    assert len(expected) == len(set(expected)) > 30
    prompts = [e.prompt for e in unpaged.items if e.prompt is not None]
    assert len(prompts) == len(set(prompts)), "a turn rendered twice"
    assert sum(not e.entry_id.isdecimal() for e in unpaged.items) == len(_entries(world))
    for size in (*range(1, 11), 100):
        assert _walk_the_store(world, size) == expected, f"size {size} changed the list in the {world.kind} world"


def test_a_long_run_of_stored_turns_never_hides_the_legacy_turns_beneath_it(world: World) -> None:
    """The legacy position moves past covered rows. Anchored only at the oldest
    exchange TAKEN — requirement 7's first reading — a window full of covered
    rows never moves, and once the stored entries run out the legacy turns
    below it are lost, or looped over for ever."""
    own_it(world)
    for i in range(3):
        _a_turn(world, i, entry=False)
    for i in range(3, 11):
        _a_turn(world, i)
    expected = [e.entry_id for e in _read_all(world).items]
    assert len(expected) == 11
    for size in (1, 2, 3):
        assert _walk_the_store(world, size) == expected


def test_a_stored_entry_older_than_a_legacy_turn_below_the_window_waits_for_it(world: World) -> None:
    """The floor. An entry's time is the caller's, and it can be older than the
    rows it links — a clock that lags, or two turns in flight at once. Here
    eight stored turns are dated before three legacy turns, so a full legacy
    window of their covered rows sits above legacy exchanges that are NEWER
    than every stored entry. Nothing older than a full window's floor may be
    taken, or a stored entry is served ahead of an exchange newer than it."""
    own_it(world)
    long_ago = datetime(2001, 1, 1, tzinfo=UTC)
    for i in range(3):
        _a_turn(world, i, entry=False)
    for i in range(3, 11):
        _a_turn(world, i, moment=long_ago + timedelta(minutes=i))
    expected = [e.entry_id for e in _read_all(world).items]
    assert [entry_id.isdecimal() for entry_id in expected] == [True] * 3 + [False] * 8
    for size in (1, 2, 3):
        assert _walk_the_store(world, size) == expected


def test_a_location_that_is_not_a_declared_name_ends_the_path_it_is_named_by() -> None:
    """The refusal names WHERE from each error's location. A location element
    that is not a declared-name shape — a key the caller chose — is never
    repeated: the path stops at it."""
    where = timeline_store._where
    assert where({"loc": ("chat", "answer", "sources", 0, "snippet")}) == "chat.answer.sources.0.snippet"
    assert where({"loc": ("chat", "answer", CANARY, "text")}) == "chat.answer.?"
    assert where({"loc": ()}) == "(entry)"


# ── Slice A: what only a real database can show ──────────────────────────────


def _pg_stores(dsn: str) -> tuple[Database, PostgresMessageStore, PostgresTimelineStore]:
    database = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))
    return database, PostgresMessageStore(db=database), PostgresTimelineStore()


def _a_user(dsn: str, email: str) -> int:
    with connect(dsn) as conn:
        return int(conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
        ).fetchone()[0])


def _a_stored_turn(dsn: str, conversation_id: str, owner: int, *, moment: datetime | None = None) -> str:
    database, messages, store = _pg_stores(dsn)
    messages.claim_conversation(conversation_id, owner)
    asked = messages.append(conversation_id, "sage", "user", "q")
    answered = messages.append(conversation_id, "sage", "assistant", "a")
    with database.transaction() as unit:
        return store.append(unit, conversation_id, *_chat_entry("q", "a", moment=moment), owner_id=owner,
                            user_message_id=asked, assistant_message_id=answered)


def _pg_count(dsn: str, table: str, conversation_id: str | None = None) -> int:
    with connect(dsn) as conn:
        if conversation_id is None:
            return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        return int(conn.execute(
            f"SELECT count(*) FROM {table} WHERE conversation_id = %s", (conversation_id,)
        ).fetchone()[0])


def _every_entry_row(dsn: str) -> list[str]:
    """Every column of every entry row, as the database renders it."""
    with connect(dsn) as conn:
        return [r[0] for r in conn.execute("SELECT row_to_json(t)::text FROM chat.timeline_entries t ORDER BY seq")]


@needs_db
def test_deleting_a_conversation_or_its_owner_takes_every_entry_with_it(dsn: str) -> None:
    """C12. The entry's edge to its conversation is `ON DELETE CASCADE`, as
    `chat.messages`'s is, and deleting a user reaches it through 0002's chain.
    Another user's entries are untouched by either.

    Each conversation also holds an entry that links no message row. A linked
    entry would go with its message rows even if the conversation edge did not
    cascade, and the test would then prove nothing about that edge.
    """
    mine, theirs = _a_user(dsn, "mine@example.com"), _a_user(dsn, "theirs@example.com")
    database, _, store = _pg_stores(dsn)
    for conversation_id, owner in ((CONVERSATION, mine), (FOREIGN, theirs)):
        _a_stored_turn(dsn, conversation_id, owner)
        with database.transaction() as unit:
            store.append(unit, conversation_id, *_chat_entry("linked to nothing"), owner_id=owner)
    assert _pg_count(dsn, "chat.timeline_entries") == 4
    with connect(dsn) as conn:
        conn.execute("DELETE FROM chat.conversations WHERE conversation_id = %s", (CONVERSATION,))
    assert _pg_count(dsn, "chat.timeline_entries", CONVERSATION) == 0
    assert _pg_count(dsn, "chat.messages", CONVERSATION) == 0
    assert _pg_count(dsn, "chat.timeline_entries", FOREIGN) == 2, "another conversation's entries went too"
    with connect(dsn) as conn:
        conn.execute("DELETE FROM auth.users WHERE id = %s", (theirs,))
    assert [_pg_count(dsn, t) for t in ("chat.timeline_entries", "chat.messages", "chat.conversations")] == [0, 0, 0]


@needs_db
def test_a_campaign_a_conversation_still_names_cannot_be_deleted_and_its_entries_stay(dsn: str) -> None:
    """C12's third row. 0006 made the conversation's campaign edge deferred and
    gave it no action — fail-closed until `1kg.2.6` decides the order — so the
    DELETE is refused at COMMIT and every entry row is exactly as it was.
    Whether a caller is ever shown that refusal is `1kg.2.6`'s, not this."""
    import psycopg

    owner = _a_user(dsn, "gm@example.com")
    campaign = "cmp_" + "A" * 22
    with connect(dsn) as conn:
        conn.execute("INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
                      (campaign, owner))
        conn.execute("INSERT INTO chat.conversations (conversation_id, user_id, campaign_id) VALUES (%s, %s, %s)",
                     (CONVERSATION, owner, campaign))
    _a_stored_turn(dsn, CONVERSATION, owner)
    before = _every_entry_row(dsn)
    assert len(before) == 1
    with connect(dsn, autocommit=False) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (campaign,))
            conn.commit()
        conn.rollback()
    assert _every_entry_row(dsn) == before
    assert _pg_count(dsn, "campaign.campaigns") == 1


@needs_db
def test_a_payload_written_past_append_is_served_by_the_rule_and_never_touched(dsn: str) -> None:
    """C5 against the real column. Four payloads written by raw SQL, past every
    check `append` makes: a newer build's (as after a rollback), a damaged one,
    one naming another entry's id, and one carrying a key no contract declares.
    Each is served as the forward-version rule says and keeps its place, the
    entries around them render, and not one row is changed by being read."""
    owner = _a_user(dsn, "c5@example.com")
    base = datetime.now(UTC)
    _a_stored_turn(dsn, CONVERSATION, owner, moment=base)

    def an_entry(entry_id: str, at: datetime) -> dict[str, Any]:
        return _chat_entry("q", "a", moment=at, entry_id=entry_id)[0]

    shapes: list[Callable[[str, datetime], dict[str, Any]]] = [
        lambda eid, at: {**an_entry(eid, at), "schema_version": CONTRACT_VERSION + 1},
        lambda eid, at: {"entry_kind": "chat", "schema_version": CONTRACT_VERSION},
        lambda eid, at: an_entry(timeline_store.new_entry_id(), at),
        lambda eid, at: {**an_entry(eid, at), "added_by_a_newer_build": True},
    ]
    with connect(dsn) as conn:
        for n, shape in enumerate(shapes, start=1):
            entry_id, at = timeline_store.new_entry_id(), base + timedelta(seconds=n)
            conn.execute(
                "INSERT INTO chat.timeline_entries "
                "(entry_id, conversation_id, entry_kind, schema_version, created_at, payload) "
                "VALUES (%s, %s, 'chat', %s, %s, %s::jsonb)",
                (entry_id, CONVERSATION, CONTRACT_VERSION, at,
                 json.dumps(shape(entry_id, at), default=lambda v: v.isoformat())),
            )
    _a_stored_turn(dsn, CONVERSATION, owner, moment=base + timedelta(seconds=10))
    before = _every_entry_row(dsn)
    database, _, store = _pg_stores(dsn)
    with database.transaction() as unit:
        items = timeline.read_page(store, unit, CONVERSATION, limit=10, cursor=None).items
    assert [(e.entry_kind, getattr(e, "reason", None)) for e in items] == [
        ("chat", None), ("chat", None), ("opaque", OpaqueReason.UNREADABLE),
        ("opaque", OpaqueReason.UNREADABLE), ("opaque", OpaqueReason.NEWER_VERSION), ("chat", None),
    ]
    assert _every_entry_row(dsn) == before

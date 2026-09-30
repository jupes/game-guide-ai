"""The timeline's `edit` entry replace, in both worlds (agent-forge-harness-1kg.5.5,
PR-1: group T of the brief's test plan).

`replace_edit_invocation` and `replace_tool_invocation` share the module's ONE
update, whose kind and identity key come from the validated entry's class
(C-28.3). `tests/test_timeline_db.py` still pins that there is exactly one
update and every tool-replace rule, unedited (T-4); this file adds the edit
half and the cross-kind refusals.

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_timeline_edit_entries_db.py -q
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database
from test_document_edit_store_db import (
    INV,
    RETRYABLE,
    T0,
    Table,
    World,
    a_table,
    edit_entry,
    fake_world,
    pg_world,
)

from service import migrations as mig
from service.campaign_store import shared_rows
from service.db import Database, PoolSettings
from service.timeline_store import EntryMismatch, EntryNotStored, TwinEntryRow, new_entry_id
from service.workbench_contracts import CONTRACT_VERSION


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("edit_entries") as target:
        mig.migrate(target)
        yield target


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        yield fake_world()
        return
    target = request.getfixturevalue("dsn")
    yield pg_world(target, Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5)))


def stored(world: World, table: Table, entry: dict[str, Any]) -> str:
    with world.db.transaction() as unit:
        entry_id: str = world.timeline.append(unit, table.conversation, entry, entry["created_at"],
                                              owner_id=table.owner)
    return entry_id


def payloads(world: World, table: Table) -> list[tuple[str, Any, int]]:
    with world.db.transaction() as unit:
        rows = world.timeline.entry_window(unit, table.conversation, before=None, limit=10)
    return [(row.entry_id, row.payload, row.seq) for row in rows]


def failed(entry: dict[str, Any], **invocation: Any) -> dict[str, Any]:
    """The same turn, its edit now failed and retryable three seconds later."""
    moved = {**entry["invocation"], "status": "failed", "error": RETRYABLE,
             "updated_at": T0 + timedelta(seconds=3), **invocation}
    return {**entry, "invocation": moved}


def tool_entry(entry_id: str) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_VERSION, "entry_kind": "tool", "entry_id": entry_id, "created_at": T0,
        "brief": "a brief", "source_entry_id": None,
        "invocation": {"schema_version": CONTRACT_VERSION, "invocation_id": INV, "tool_id": "npc",
                       "status": "working", "attempt": 1, "cancel_requested": False, "created_at": T0,
                       "updated_at": T0, "result": None, "error": None},
    }


def test_an_edit_entry_is_replaced_in_place_and_reads_back_as_its_new_invocation(world: World) -> None:
    """T-1."""
    table = a_table(world)
    entry = edit_entry(INV, new_entry_id(), T0, table.document)
    entry_id = stored(world, table, entry)
    [(_, before, seq)] = payloads(world, table)
    with world.db.transaction() as unit:
        assert world.timeline.replace_edit_invocation(
            unit, table.conversation, failed(entry), T0, owner_id=table.owner) == entry_id
    [(same_id, after, same_seq)] = payloads(world, table)
    assert (same_id, same_seq) == (entry_id, seq)
    assert (after["invocation"]["status"], after["invocation"]["error"]["code"]) == ("failed", RETRYABLE["code"])
    assert (after["document"], after["scope"], after["instruction"]) == (
        before["document"], before["scope"], before["instruction"])


def test_only_an_edit_entry_is_replaced_as_an_edit_and_the_refusal_comes_before_any_statement(world: World) -> None:
    """T-2: a chat or tool payload is `EntryMismatch` before any statement, and
    the tool replace cannot rewrite an edit entry."""
    table = a_table(world)
    entry = edit_entry(INV, new_entry_id(), T0, table.document)
    stored(world, table, entry)
    chat = {"schema_version": CONTRACT_VERSION, "entry_kind": "chat", "entry_id": entry["entry_id"],
            "created_at": T0, "mode": "gm", "prompt": "a question", "answer": None}
    with world.db.transaction() as unit:
        for wrong in (chat, tool_entry(entry["entry_id"])):
            with pytest.raises(EntryMismatch):
                world.timeline.replace_edit_invocation(unit, table.conversation, wrong, T0, owner_id=table.owner)
        with pytest.raises(EntryMismatch):
            world.timeline.replace_tool_invocation(unit, table.conversation, failed(entry), T0, owner_id=table.owner)
        assert world.timeline.has_entry(unit, table.conversation, entry["entry_id"]), "the transaction runs on"
    assert [p["invocation"]["status"] for _, p, _ in payloads(world, table)] == ["working"]


def test_the_kind_column_decides_which_replace_may_touch_a_row(world: World) -> None:
    """T-2: a stored tool turn is never rewritten by the edit replace, and a
    stored edit turn never by the tool replace, whatever the new payload says."""
    table = a_table(world)
    tool_id = stored(world, table, tool_entry(new_entry_id()))
    edit = edit_entry(INV, new_entry_id(), T0, table.document)
    stored(world, table, edit)
    with world.db.transaction() as unit, pytest.raises(EntryNotStored):
        world.timeline.replace_edit_invocation(
            unit, table.conversation, failed({**edit, "entry_id": tool_id}), T0, owner_id=table.owner)
    kept = {entry_id: p["entry_kind"] for entry_id, p, _ in payloads(world, table)}
    assert kept == {tool_id: "tool", edit["entry_id"]: "edit"}
    assert [p["invocation"]["status"] for _, p, _ in payloads(world, table)] == ["working", "working"]


@pytest.mark.parametrize("change", ["invocation", "document", "created_at", "entry_id", "owner", "conversation"])
def test_an_edit_replace_that_is_not_the_same_turn_is_refused_by_the_guard(world: World, change: str) -> None:
    """T-3: the same id, instant, invocation and document, in a conversation of
    the caller's — every miss is the one refusal, and zero rows."""
    table = a_table(world)
    elsewhere = a_table(world, campaign=table.campaign)
    theirs = a_table(world, world.other)
    entry = edit_entry(INV, new_entry_id(), T0, table.document)
    stored(world, table, entry)
    moment = T0 + timedelta(seconds=1) if change == "created_at" else T0
    moved = {**entry, "created_at": moment}
    if change == "invocation":
        moved = failed(moved, invocation_id="edit_another_000001")
    elif change == "document":
        moved = {**failed(moved, document_id=elsewhere.document),
                 "document": {**entry["document"], "document_id": elsewhere.document}}
    else:
        moved = failed(moved, created_at=moment)
    if change == "entry_id":
        moved["entry_id"] = new_entry_id()
    with world.db.transaction() as unit:
        with pytest.raises(EntryNotStored):
            world.timeline.replace_edit_invocation(
                unit, theirs.conversation if change == "conversation" else table.conversation, moved, moment,
                owner_id=world.other if change == "owner" else table.owner,
            )
        assert world.timeline.has_entry(unit, table.conversation, entry["entry_id"]), "the transaction runs on"
    assert [p["invocation"]["status"] for _, p, _ in payloads(world, table)] == ["working"]


def test_an_edit_replace_in_a_unit_that_rolls_back_leaves_the_turn_as_it_was(world: World) -> None:
    table = a_table(world)
    entry = edit_entry(INV, new_entry_id(), T0, table.document)
    stored(world, table, entry)
    with pytest.raises(RuntimeError), world.db.transaction() as unit:
        world.timeline.replace_edit_invocation(unit, table.conversation, failed(entry), T0, owner_id=table.owner)
        raise RuntimeError("the rest of the transaction failed")
    assert [p["invocation"]["status"] for _, p, _ in payloads(world, table)] == ["working"]


def test_a_row_of_another_kind_is_never_replaced_as_an_edit_whatever_its_payload_says(world: World) -> None:
    """Past `append`: a row whose kind column is `tool` stays as it is even when
    its payload — written by raw SQL, or straight into the twin's rows — is the
    very edit a replace carries. The kind column is the authority."""
    table = a_table(world)
    entry = edit_entry(INV, new_entry_id(), T0, table.document)
    payload = json.dumps(entry, default=lambda d: d.isoformat())
    if world.dsn is not None:
        with connect(world.dsn) as conn:
            conn.execute(
                "INSERT INTO chat.timeline_entries (entry_id, conversation_id, entry_kind, schema_version, "
                "created_at, payload) VALUES (%s, %s, 'tool', 1, %s, %s::jsonb)",
                (entry["entry_id"], table.conversation, T0, payload),
            )
    else:
        with world.db.transaction() as unit:
            shared_rows(world.db, "timeline_entries").add(unit, entry["entry_id"], TwinEntryRow(
                table.conversation, "tool", 1, entry["entry_id"], T0, 1, payload, None, None,
            ))
    with pytest.raises(EntryNotStored), world.db.transaction() as unit:
        world.timeline.replace_edit_invocation(unit, table.conversation, failed(entry), T0, owner_id=table.owner)
    [(entry_id, kept, _)] = payloads(world, table)
    assert (entry_id, kept["invocation"]["status"]) == (entry["entry_id"], "working")

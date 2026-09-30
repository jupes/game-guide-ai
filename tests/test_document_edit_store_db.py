"""The AI document edit aggregate against a real PostgreSQL, and its twin
(agent-forge-harness-1kg.5.5, PR-1: group S of the brief's test plan).

The **shared behavioural suite** runs every rule twice, once on the in-memory
twin and once on PostgreSQL, so a rule the two could disagree about is asserted
of both. Both worlds seed alike: campaigns through `CampaignStore`,
conversations through `ConversationStore` **and**
`MessageStore.claim_conversation`, documents through `DocumentStore.create`,
and `edit` entries through `TimelineStore.append`.

What only PostgreSQL can prove — the deletion edges — is `needs_db`. Without
DATABASE_URL those tests skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_document_edit_store_db.py -q
"""

from __future__ import annotations

import ast
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import document_edit_store
from service import migrations as mig
from service.campaign_store import InMemoryCampaignStore, PostgresCampaignStore
from service.conversation_store import InMemoryConversationStore, PostgresConversationStore
from service.db import Database, InMemoryDatabase, PoolSettings
from service.document_edit_store import (
    EditInvalid,
    EditNotStored,
    EditRow,
    InMemoryDocumentEditStore,
    NewEdit,
    PostgresDocumentEditStore,
)
from service.document_store import InMemoryDocumentStore, PostgresDocumentStore
from service.history import InMemoryMessageStore, PostgresMessageStore
from service.timeline_store import InMemoryTimelineStore, PostgresTimelineStore, new_entry_id
from service.workbench_contracts import (
    CONTRACT_VERSION,
    DOC_TYPE_LIBRARY_CATEGORY,
    Author,
    DocumentTypeId,
)

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
TTL = timedelta(seconds=150)
INV = "edit_suite_000000001"
OTHER_INV = "edit_suite_000000002"
MISSING_CAMPAIGN = "cmp_" + "z" * 22
MISSING_DOCUMENT = "doc_" + "z" * 22
CANARY = "Qv8CanaryEdit"
AN_NPC = {"name": "Vashti", "qualifier": "Harbourmistress", "wants": "The harbour, and the 🎲 in it."}
A_STATBLOCK = {"name": "Dire Rat", "qualifier": "", "ac": 12, "hp": 7}
RETRYABLE = {"code": "provider_failed", "message": "The assistant couldn't finish that. Try again.",
             "retryable": True}
FINAL = {"code": "provider_failed", "message": "The assistant couldn't do that one.", "retryable": False}
CHANGED = {"outcome": "changed", "prose": "Edited Wants.", "suggestions": [], "version_number": 2,
           "write_revision": 2, "changed_fields": ["wants"]}


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("edits") as target:
        mig.migrate(target)
        yield target


def users(dsn: str, *emails: str) -> list[int]:
    with connect(dsn) as conn:
        return [
            int(conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
            ).fetchone()[0])
            for email in emails
        ]


@dataclass
class World:
    kind: str
    db: Any
    campaigns: Any
    conversations: Any
    messages: Any
    timeline: Any
    documents: Any
    store: Any
    owner: int
    other: int
    dsn: str | None = None


def pg_world(target: str, database: Database) -> World:
    owner, other = users(target, "gm@example.com", "other@example.com")
    return World(
        "postgres", database, PostgresCampaignStore(), PostgresConversationStore(),
        PostgresMessageStore(db=database), PostgresTimelineStore(), PostgresDocumentStore(),
        PostgresDocumentEditStore(), owner, other, target,
    )


def fake_world() -> World:
    db = InMemoryDatabase()
    messages = InMemoryMessageStore()
    return World(
        "fake", db, InMemoryCampaignStore(db), InMemoryConversationStore(db), messages,
        InMemoryTimelineStore(db, messages=messages), InMemoryDocumentStore(db), InMemoryDocumentEditStore(db),
        1, 2,
    )


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        yield fake_world()
        return
    target = request.getfixturevalue("dsn")
    yield pg_world(target, Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5)))


@dataclass(frozen=True)
class Table:
    owner: int
    campaign: str
    conversation: str
    document: str


def a_table(world: World, owner: int | None = None, *, campaign: str | None = None,
            doc_type: DocumentTypeId = DocumentTypeId.NPC) -> Table:
    """A campaign of `owner`'s (or `campaign`), a conversation of theirs in it and a document of it."""
    gm = world.owner if owner is None else owner
    with world.db.transaction() as unit:
        if campaign is None:
            campaign = world.campaigns.create(unit, owner_id=gm, name="Nocturne").id
        conversation = world.conversations.create(
            unit, owner_id=gm, campaign_id=campaign, title=None, started_mode=None,
        )
        document = world.documents.create(
            unit, campaign, doc_type=doc_type, type_version=1,
            data=dict(AN_NPC if doc_type is DocumentTypeId.NPC else A_STATBLOCK), author=Author.GM,
        )
    world.messages.claim_conversation(conversation.id, gm)
    return Table(gm, campaign, conversation.id, document.id)


def edit_entry(invocation_id: str, entry_id: str, at: datetime, document_id: str, *,
               title: str = "Vashti", status: str = "working", error: dict[str, Any] | None = None,
               result: dict[str, Any] | None = None, updated: datetime | None = None,
               doc_type: str = "npc") -> dict[str, Any]:
    """An `edit` entry carrying its invocation, as the edit service writes one."""
    return {
        "schema_version": CONTRACT_VERSION, "entry_kind": "edit", "entry_id": entry_id, "created_at": at,
        "document": {"document_id": document_id, "type": doc_type, "title": title,
                     "library_category": DOC_TYPE_LIBRARY_CATEGORY[DocumentTypeId(doc_type)].value},
        "scope": {"kind": "selection", "field": "wants"},
        "instruction": {"kind": "action", "action": "shorter"},
        "invocation": {
            "schema_version": CONTRACT_VERSION, "invocation_id": invocation_id, "document_id": document_id,
            "status": status, "attempt": 1, "cancel_requested": False, "created_at": at,
            "updated_at": updated or at, "result": result, "error": error,
        },
    }


def entry_in(world: World, unit: Any, table: Table, invocation_id: str = INV, *, at: datetime = T0,
             kind: str = "edit", conversation: str | None = None, owner: int | None = None) -> str:
    entry_id = new_entry_id()
    if kind == "edit":
        entry = edit_entry(invocation_id, entry_id, at, table.document)
    elif kind == "tool":
        entry = {
            "schema_version": CONTRACT_VERSION, "entry_kind": "tool", "entry_id": entry_id, "created_at": at,
            "brief": "a brief", "source_entry_id": None,
            "invocation": {"schema_version": CONTRACT_VERSION, "invocation_id": invocation_id, "tool_id": "npc",
                           "status": "working", "attempt": 1, "cancel_requested": False, "created_at": at,
                           "updated_at": at, "result": None, "error": None},
        }
    else:
        entry = {"schema_version": CONTRACT_VERSION, "entry_kind": "chat", "entry_id": entry_id, "created_at": at,
                 "mode": "gm", "prompt": "a question", "answer": None}
    world.timeline.append(unit, conversation or table.conversation, entry, at,
                          owner_id=table.owner if owner is None else owner)
    return entry_id


def new_edit(table: Table, entry_id: str, **changes: Any) -> NewEdit:
    """A selection edit of `wants` ("The harbour"), by action, unless `changes` say otherwise."""
    made = NewEdit(
        conversation_id=table.conversation, entry_id=entry_id, document_id=table.document, document_type="npc",
        document_title="Vashti", scope_kind="selection", scope_field="wants", selection_start=0,
        selection_end=11, selection_text="The harbour", instruction_kind="action", instruction_text=None,
        instruction_action="shorter", base_write_revision=1,
    )
    return replace(made, **changes)


def create(world: World, unit: Any, table: Table, entry_id: str, invocation_id: str = INV, *,
           owner: int | None = None, campaign: str | None = None, now: datetime = T0,
           operation_id: str | None = None, deadline: datetime | None = None, schema_version: int = CONTRACT_VERSION,
           **changes: Any) -> EditRow:
    row: EditRow = world.store.create(
        unit, owner_id=table.owner if owner is None else owner,
        campaign_id=table.campaign if campaign is None else campaign, invocation_id=invocation_id,
        new=new_edit(table, entry_id, **changes), operation_id=operation_id or uuid.uuid4().hex,
        attempt_deadline=now + TTL if deadline is None else deadline, now=now, schema_version=schema_version,
    )
    return row


def admit(world: World, table: Table, invocation_id: str = INV, *, now: datetime = T0, **changes: Any) -> EditRow:
    """What the admitting transaction writes: the carrying entry, then the row."""
    with world.db.transaction() as unit:
        return create(world, unit, table, entry_in(world, unit, table, invocation_id, at=now), invocation_id,
                      now=now, **changes)


def held(world: World, table: Table, invocation_id: str = INV) -> EditRow | None:
    with world.db.transaction() as unit:
        row: EditRow | None = world.store.get_for_update(unit, table.owner, table.campaign, invocation_id)
    return row


def attempts(world: World, table: Table, invocation_id: str = INV) -> list[Any]:
    with world.db.transaction() as unit:
        rows: list[Any] = world.store.attempts(unit, table.owner, table.campaign, invocation_id)
    return rows


def settle(world: World, table: Table, *, status: str, outcome: str | None = None, attempt: int = 1,
           result: dict[str, Any] | None = None, error: dict[str, Any] | None = None, forget: bool = False,
           now: datetime = T0 + timedelta(seconds=5), invocation_id: str = INV) -> EditRow:
    with world.db.transaction() as unit:
        row: EditRow = world.store.settle(
            unit, table.owner, table.campaign, invocation_id, attempt=attempt, status=status,
            outcome=outcome or status, result=result, error=error, forget_selection=forget, now=now,
        )
    return row


def retry(world: World, table: Table, *, attempt: int = 1, base: int = 2,
          now: datetime = T0 + timedelta(seconds=10), invocation_id: str = INV) -> EditRow:
    with world.db.transaction() as unit:
        row: EditRow = world.store.start_retry(
            unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=invocation_id,
            attempt=attempt, base_write_revision=base, operation_id=uuid.uuid4().hex,
            attempt_deadline=now + TTL, now=now,
        )
    return row


def archive(world: World, table: Table) -> None:
    with world.db.transaction() as unit:
        assert world.campaigns.set_archived(unit, table.campaign, owner_id=table.owner, archived=True)


# ── A new edit ───────────────────────────────────────────────────────────────


def test_a_new_edit_is_working_at_attempt_one_with_its_request_link_and_admission_record(world: World) -> None:
    table = a_table(world)
    operation = uuid.uuid4().hex
    with world.db.transaction() as unit:
        entry_id = entry_in(world, unit, table)
        created = create(world, unit, table, entry_id, operation_id=operation)
    assert held(world, table) == created
    assert (created.status, created.attempt, created.cancel_requested) == ("working", 1, False)
    assert (created.result, created.error, created.schema_version) == (None, None, CONTRACT_VERSION)
    assert (created.attempt_deadline, created.created_at, created.updated_at) == (T0 + TTL, T0, T0)
    assert (created.entry_id, created.conversation_id, created.document_id) == (
        entry_id, table.conversation, table.document)
    assert (created.document_type, created.document_title) == ("npc", "Vashti")
    assert (created.scope_kind, created.scope_field, created.selection_start, created.selection_end,
            created.selection_text) == ("selection", "wants", 0, 11, "The harbour")
    assert (created.instruction_kind, created.instruction_text, created.instruction_action,
            created.base_write_revision) == ("action", None, "shorter", 1)
    [record] = attempts(world, table)
    assert (record.attempt, record.operation_id, record.started_at, record.deadline_at) == (1, operation, T0, T0 + TTL)
    assert (record.ended_at, record.outcome) == (None, None)


@pytest.mark.parametrize("scope", ["document", "field"])
def test_a_document_or_field_edit_by_text_holds_no_span(world: World, scope: str) -> None:
    table = a_table(world)
    created = admit(
        world, table, scope_kind=scope, scope_field=None if scope == "document" else "wants",
        selection_start=None, selection_end=None, selection_text=None, instruction_kind="text",
        instruction_text="Make her warier.", instruction_action=None,
    )
    assert (created.scope_kind, created.selection_start, created.selection_end, created.selection_text) == (
        scope, None, None, None)
    assert (created.instruction_text, created.instruction_action) == ("Make her warier.", None)


REFUSED_PARENTS = [
    "foreign_campaign", "missing_campaign", "archived_campaign", "foreign_conversation",
    "another_campaigns_conversation", "entry_of_another_conversation", "chat_entry_as_carrier",
    "tool_entry_as_carrier", "another_campaigns_document", "missing_document", "another_type",
    "deleted_document", "taken_key", "taken_entry",
]


@pytest.mark.parametrize("case", REFUSED_PARENTS)
def test_the_creating_statement_refuses_every_parent_that_is_not_the_callers(world: World, case: str) -> None:
    """S-1: every guard is in the statement, so each miss is zero rows — the same
    named refusal in both worlds, raised without a failed statement, and the
    PostgreSQL transaction is still usable afterwards."""
    mine = a_table(world)
    theirs = a_table(world, world.other)
    second = a_table(world)
    if case == "archived_campaign":
        archive(world, mine)
    if case == "deleted_document":
        with world.db.transaction() as unit:
            assert world.documents.delete(unit, mine.campaign, mine.document)
    if case in {"taken_key", "taken_entry"}:
        admit(world, mine, OTHER_INV if case == "taken_entry" else INV)
    with world.db.transaction() as unit:
        kwargs: dict[str, Any] = {}
        carrier = entry_in(world, unit, mine)
        if case == "foreign_campaign":
            kwargs = {"owner": world.other}
        elif case == "missing_campaign":
            kwargs = {"campaign": MISSING_CAMPAIGN}
        elif case == "foreign_conversation":
            carrier = entry_in(world, unit, theirs)
            kwargs = {"conversation_id": theirs.conversation}
        elif case == "another_campaigns_conversation":
            carrier = entry_in(world, unit, second)
            kwargs = {"conversation_id": second.conversation}
        elif case == "entry_of_another_conversation":
            carrier = entry_in(world, unit, second)
        elif case in {"chat_entry_as_carrier", "tool_entry_as_carrier"}:
            carrier = entry_in(world, unit, mine, kind=case.split("_")[0])
        elif case == "another_campaigns_document":
            kwargs = {"document_id": second.document}
        elif case == "missing_document":
            kwargs = {"document_id": MISSING_DOCUMENT}
        elif case == "another_type":
            kwargs = {"document_type": "statblock"}
        elif case == "taken_entry":
            with world.db.transaction() as peek:
                carrier = world.store.get_for_update(peek, mine.owner, mine.campaign, OTHER_INV).entry_id
        with pytest.raises(EditNotStored):
            create(world, unit, mine, carrier, **kwargs)
        if case == "taken_key":
            assert world.store.get_for_update(unit, mine.owner, mine.campaign, INV) is not None
        else:
            assert world.store.get_for_update(unit, mine.owner, mine.campaign, INV) is None, "still usable"
    expected = 1 if case in {"taken_key", "taken_entry"} else 0
    for owner, campaign in ((mine.owner, mine.campaign), (world.other, mine.campaign),
                            (mine.owner, MISSING_CAMPAIGN), (world.other, theirs.campaign)):
        with world.db.transaction() as unit:
            found = [world.store.get_for_update(unit, owner, campaign, key) for key in (INV, OTHER_INV)]
            assert len([row for row in found if row is not None]) == (expected if campaign == mine.campaign
                                                                       and owner == mine.owner else 0)


def test_a_refused_create_takes_the_entry_appended_before_it_with_it(world: World) -> None:
    table = a_table(world)
    archive(world, table)
    with pytest.raises(EditNotStored), world.db.transaction() as unit:
        create(world, unit, table, entry_in(world, unit, table))
    with world.db.transaction() as unit:
        assert world.timeline.entry_window(unit, table.conversation, before=None, limit=10) == []
    assert (held(world, table), attempts(world, table)) == (None, [])


def test_the_key_is_the_gm_the_campaign_and_the_id(world: World) -> None:
    """S-2: the same key under another GM or another campaign is another row."""
    mine = a_table(world)
    second = a_table(world)
    theirs = a_table(world, world.other)
    first = admit(world, mine)
    in_second = admit(world, second)
    in_theirs = admit(world, theirs)
    assert (held(world, mine), held(world, second), held(world, theirs)) == (first, in_second, in_theirs)
    with world.db.transaction() as unit:
        assert world.store.get_for_update(unit, world.other, mine.campaign, INV) is None
        assert world.store.get_for_update(unit, mine.owner, theirs.campaign, INV) is None


def test_a_unit_that_rolls_back_leaves_no_edit_and_no_attempt(world: World) -> None:
    table = a_table(world)
    with pytest.raises(RuntimeError), world.db.transaction() as unit:
        create(world, unit, table, entry_in(world, unit, table))
        raise RuntimeError("the rest of the transaction failed")
    assert (held(world, table), attempts(world, table)) == (None, [])


def test_the_edit_outlives_its_document(world: World) -> None:
    """I-25: no foreign key to the document, so its row survives to say so."""
    table = a_table(world)
    created = admit(world, table)
    with world.db.transaction() as unit:
        assert world.documents.delete(unit, table.campaign, table.document)
    assert held(world, table) == created


# ── Values the table would refuse (S-9) ──────────────────────────────────────

REFUSED_VALUES: dict[str, dict[str, Any]] = {
    "invocation_id": {"invocation_id": "too_short"},
    "document_id": {"document_id": "cmp_" + "a" * 22},
    "document_type": {"document_type": "dossier"},
    "document_title": {"document_title": ""},
    "scope_kind": {"scope_kind": "paragraph"},
    "scope_field": {"scope_field": "Wants"},
    "selection": {"scope_kind": "field", "instruction_kind": "text", "instruction_text": "x",
                  "instruction_action": None},
    "selection_start": {"selection_start": True},
    "selection_end": {"selection_end": 0},
    "selection_text": {"selection_text": "The harbou"},
    "instruction_kind": {"instruction_kind": "voice"},
    "instruction_text": {"instruction_kind": "text", "instruction_text": "\U0001f409" * 2001,
                         "instruction_action": None},
    "instruction_action": {"instruction_action": "funnier"},
    "base_write_revision": {"base_write_revision": 2**53},
    "attempt_deadline": {"deadline": T0},
    "operation_id": {"operation_id": "Z" * 32},
    "schema_version": {"schema_version": 0},
}


@pytest.mark.parametrize("what", sorted(REFUSED_VALUES))
def test_a_value_the_table_would_refuse_is_refused_by_name_before_any_statement(world: World, what: str) -> None:
    """S-9: refused before any statement, so the PostgreSQL transaction runs on
    (a failed statement would abort it and its DETAIL would carry the value)."""
    table = a_table(world)
    with world.db.transaction() as unit:
        entry_id = entry_in(world, unit, table)
        with pytest.raises(EditInvalid) as refused:
            create(world, unit, table, entry_id, **REFUSED_VALUES[what])
        assert refused.value.what == what
        if world.kind == "postgres":
            assert unit.conn.execute("SELECT 1").fetchone() == (1,)
        assert world.store.get_for_update(unit, table.owner, table.campaign, INV) is None


@pytest.mark.parametrize("changes", [
    {"instruction_kind": "action", "instruction_text": "x"},
    {"scope_kind": "field", "selection_start": None, "selection_end": None, "selection_text": None},
    {"instruction_kind": "text", "instruction_text": "x", "instruction_action": "shorter"},
    {"document_title": "\x00"},
    {"scope_kind": "document", "scope_field": "wants", "selection_start": None, "selection_end": None,
     "selection_text": None, "instruction_kind": "text", "instruction_text": "x", "instruction_action": None},
])
def test_a_request_the_consistency_checks_would_refuse_is_refused_before_any_statement(
    world: World, changes: dict[str, Any],
) -> None:
    table = a_table(world)
    with world.db.transaction() as unit:
        with pytest.raises(EditInvalid):
            create(world, unit, table, entry_in(world, unit, table), **changes)
        assert world.store.get_for_update(unit, table.owner, table.campaign, INV) is None


def test_the_widest_values_the_contract_allows_are_stored_whole(world: World) -> None:
    table = a_table(world)
    text = "\U0001f409" * 20_000
    created = admit(world, table, selection_start=0, selection_end=20_000, selection_text=text,
                    document_title="\U0001f409" * 200, base_write_revision=2**53 - 1)
    assert (created.selection_text, created.document_title, created.base_write_revision) == (
        text, "\U0001f409" * 200, 2**53 - 1)
    assert held(world, table) == created


@pytest.mark.parametrize("bad", ["status", "outcome", "result", "error", "forget_selection"])
def test_a_settlement_the_table_would_refuse_is_refused_before_any_statement(world: World, bad: str) -> None:
    table = a_table(world)
    admit(world, table)
    args: dict[str, Any] = {"status": "done", "outcome": "done", "result": CHANGED, "error": None,
                            "forget_selection": True}
    args.update({"status": {"status": "working"}, "outcome": {"outcome": "failed"}, "result": {"result": None},
                 "error": {"error": FINAL}, "forget_selection": {"forget_selection": 1}}[bad])
    with world.db.transaction() as unit:
        with pytest.raises(EditInvalid) as refused:
            world.store.settle(unit, table.owner, table.campaign, INV, attempt=1, now=T0, **args)
        assert refused.value.what == bad
    assert held(world, table).status == "working"


def test_no_repr_carries_the_instruction_the_selection_the_title_the_result_or_the_invocation_id(
    world: World,
) -> None:
    """S-8, C-11: the title is private text too."""
    table = a_table(world)
    secret = "edit_" + CANARY + "_01"
    with world.db.transaction() as unit:
        new = new_edit(table, entry_in(world, unit, table, secret), document_title=f"The {CANARY}",
                       selection_text=CANARY[:11])
        world.store.create(unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=secret, new=new,
                           operation_id=uuid.uuid4().hex, attempt_deadline=T0 + TTL, now=T0,
                           schema_version=CONTRACT_VERSION)
    row = settle(world, table, status="done", result={**CHANGED, "prose": CANARY}, invocation_id=secret)
    [record] = attempts(world, table, secret)
    texted = replace(row, instruction_kind="text", instruction_text=CANARY, instruction_action=None)
    for shown in (repr(new), repr(row), repr(record), repr(texted)):
        assert CANARY not in shown and CANARY[:11] not in shown
    assert "owner_id=" in repr(row), "a positive control: the repr is not simply empty"


# ── One edit per document, the fence, the settlement ─────────────────────────


def test_in_flight_for_document_lists_only_this_documents_working_rows_before_their_deadline(world: World) -> None:
    """S-3, I-5: from any conversation of the campaign, oldest first."""
    table = a_table(world)
    other_doc = a_table(world, campaign=table.campaign)
    same_doc_elsewhere = replace(other_doc, document=table.document)
    admit(world, table, "edit_suite_later_00001", now=T0 + timedelta(seconds=2))
    admit(world, same_doc_elsewhere, "edit_suite_first_00001", now=T0)
    admit(world, other_doc, "edit_suite_other_00001", now=T0)
    admit(world, table, "edit_suite_expired_001", now=T0 - TTL)
    admit(world, table, "edit_suite_settled_001", now=T0)
    settle(world, table, status="failed", error=RETRYABLE, invocation_id="edit_suite_settled_001")
    with world.db.transaction() as unit:
        listed = world.store.in_flight_for_document(unit, table.owner, table.campaign, table.document, now=T0)
        assert listed == ["edit_suite_first_00001", "edit_suite_later_00001"]
        assert world.store.in_flight_for_document(unit, world.other, table.campaign, table.document, now=T0) == []
        assert world.store.in_flight_for_document(
            unit, table.owner, table.campaign, table.document, now=T0 + TTL + timedelta(seconds=2)) == []


def test_the_fence_holds_only_its_own_attempt_while_working_and_before_its_deadline(world: World) -> None:
    """S-4."""
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        assert world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0) is not None
        assert world.store.fence(unit, table.owner, table.campaign, INV, attempt=2, now=T0) is None
        assert world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0 + TTL) is None
        assert world.store.fence(unit, world.other, table.campaign, INV, attempt=1, now=T0) is None
    settle(world, table, status="cancelled")
    with world.db.transaction() as unit:
        assert world.store.fence(unit, table.owner, table.campaign, INV, attempt=1, now=T0) is None


def test_settle_moves_a_working_row_once_and_ends_its_attempt(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    done = settle(world, table, status="done", result=CHANGED)
    assert (done.status, done.result, done.error, done.updated_at) == ("done", CHANGED, None, T0 + timedelta(seconds=5))
    assert [(a.outcome, a.ended_at) for a in attempts(world, table)] == [("done", T0 + timedelta(seconds=5))]
    with pytest.raises(EditNotStored):
        settle(world, table, status="failed", error=FINAL)
    assert held(world, table) == done


def test_settle_refuses_a_row_on_another_attempt_or_of_another_gm(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    with pytest.raises(EditNotStored):
        settle(world, table, status="cancelled", attempt=2)
    with world.db.transaction() as unit, pytest.raises(EditNotStored):
        world.store.settle(unit, world.other, table.campaign, INV, attempt=1, status="cancelled",
                           outcome="cancelled", result=None, error=None, forget_selection=True, now=T0)
    assert held(world, table).selection_text == "The harbour"


@pytest.mark.parametrize("forget", [True, False])
def test_the_selected_text_is_forgotten_by_the_settling_statement_only_when_asked(world: World, forget: bool) -> None:
    """S-5, I-18: in the same statement, and never otherwise."""
    table = a_table(world)
    admit(world, table)
    settled = settle(world, table, status="failed", error=RETRYABLE, forget=forget)
    expected = None if forget else "The harbour"
    assert settled.selection_text == expected
    assert held(world, table).selection_text == expected
    assert (settled.selection_start, settled.selection_end) == (0, 11), "the span itself is kept"


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_an_expiry_that_ends_the_edit_can_forget_the_selection(world: World, status: str) -> None:
    """S-5b: the lazy expiry's settle (outcome `expired`) at attempt 100, or to
    `cancelled`, is a final state and forgets the text like any other."""
    table = a_table(world)
    admit(world, table)
    error = {**RETRYABLE, "code": "attempt_expired", "retryable": False} if status == "failed" else None
    ended = settle(world, table, status=status, outcome="expired", error=error, forget=True)
    assert (ended.status, ended.selection_text) == (status, None)
    assert [a.outcome for a in attempts(world, table)] == ["expired"]


def test_the_cancel_flag_is_raised_on_working_and_done_only_and_only_once(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        flagged = world.store.set_cancel_requested(unit, table.owner, table.campaign, INV, now=T0)
        assert flagged is not None and flagged.cancel_requested
        assert world.store.set_cancel_requested(unit, table.owner, table.campaign, INV, now=T0) is None
        assert world.store.set_cancel_requested(unit, world.other, table.campaign, INV, now=T0) is None
    done = settle(world, table, status="done", result=CHANGED)
    assert (done.status, done.cancel_requested) == ("done", True), "a completion keeps the flag"
    failed_table = a_table(world)
    admit(world, failed_table)
    settle(world, failed_table, status="failed", error=FINAL)
    with world.db.transaction() as unit:
        assert world.store.set_cancel_requested(unit, failed_table.owner, failed_table.campaign, INV, now=T0) is None


# ── Retries (S-6) ────────────────────────────────────────────────────────────


def test_a_retry_starts_the_next_attempt_on_the_new_base_and_clears_the_row(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    with world.db.transaction() as unit:
        world.store.set_cancel_requested(unit, table.owner, table.campaign, INV, now=T0)
    settle(world, table, status="failed", error=RETRYABLE)
    again = retry(world, table, base=7)
    assert (again.status, again.attempt, again.base_write_revision) == ("working", 2, 7)
    assert (again.error, again.result, again.cancel_requested) == (None, None, False)
    assert again.selection_text == "The harbour", "a retry must re-verify the span, so it keeps it"
    assert again.attempt_deadline == T0 + timedelta(seconds=10) + TTL
    assert [(a.attempt, a.outcome) for a in attempts(world, table)] == [(1, "failed"), (2, None)]


RETRY_REFUSALS = ["working", "done", "cancelled", "final", "another_attempt", "archived", "forgotten_selection",
                  "another_gm"]


@pytest.mark.parametrize("case", RETRY_REFUSALS)
def test_a_retry_of_anything_but_a_failed_retryable_row_in_a_live_campaign_is_refused(world: World, case: str) -> None:
    table = a_table(world)
    admit(world, table)
    if case == "done":
        settle(world, table, status="done", result=CHANGED)
    elif case == "cancelled":
        settle(world, table, status="cancelled")
    elif case == "final":
        settle(world, table, status="failed", error=FINAL)
    elif case != "working":
        settle(world, table, status="failed", error=RETRYABLE, forget=case == "forgotten_selection")
    if case == "archived":
        archive(world, table)
    before = held(world, table)
    with world.db.transaction() as unit, pytest.raises(EditNotStored):
        world.store.start_retry(
            unit, owner_id=world.other if case == "another_gm" else table.owner, campaign_id=table.campaign,
            invocation_id=INV, attempt=2 if case == "another_attempt" else 1, base_write_revision=2,
            operation_id=uuid.uuid4().hex, attempt_deadline=T0 + TTL + TTL, now=T0 + TTL,
        )
    assert held(world, table) == before
    assert len(attempts(world, table)) == 1


def test_the_hundredth_attempt_is_the_last(world: World) -> None:
    table = a_table(world)
    admit(world, table)
    for attempt in range(1, 100):
        settle(world, table, status="failed", error=RETRYABLE, attempt=attempt)
        retry(world, table, attempt=attempt, base=attempt + 1)
    settle(world, table, status="failed", error=RETRYABLE, attempt=100)
    with pytest.raises(EditNotStored):
        retry(world, table, attempt=100)
    assert (held(world, table).attempt, len(attempts(world, table))) == (100, 100)


@pytest.mark.parametrize("what", ["attempt", "base_write_revision", "operation_id", "attempt_deadline"])
def test_a_retry_the_table_would_refuse_is_refused_by_name_before_any_statement(world: World, what: str) -> None:
    table = a_table(world)
    admit(world, table)
    settle(world, table, status="failed", error=RETRYABLE)
    args: dict[str, Any] = {"attempt": 1, "base_write_revision": 2, "operation_id": uuid.uuid4().hex,
                            "attempt_deadline": T0 + TTL + TTL}
    args[what] = {"attempt": True, "base_write_revision": 0, "operation_id": "x",
                  "attempt_deadline": T0 + TTL}[what]
    with world.db.transaction() as unit:
        with pytest.raises(EditInvalid) as refused:
            world.store.start_retry(unit, owner_id=table.owner, campaign_id=table.campaign, invocation_id=INV,
                                    now=T0 + TTL, **args)
        assert refused.value.what == what
        assert world.store.get_for_update(unit, table.owner, table.campaign, INV).status == "failed"


# ── Ownership is in the query, statically (S-7) ─────────────────────────────

NAMES_THE_OWNER = re.compile(r"\bowner_id\s*=\s*%s")


def statements(module: Any) -> dict[str, str]:
    """Every statement the module hands to `.execute(...)`, by the constant it is
    written in, read from the module's syntax tree. A call whose statement is
    not a named constant is a failure: it could not be checked by name."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = getattr(module, node.targets[0].id, None)
            if isinstance(value, str):
                constants[node.targets[0].id] = value
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            first = node.args[0]
            assert isinstance(first, ast.Name) and first.id in constants, f"an unnamed statement at line {node.lineno}"
            found[first.id] = constants[first.id]
    return found


def test_every_statement_names_the_owner() -> None:
    """SEC-2 with no exemption: the bound is `workbench_load`'s statement."""
    found = statements(document_edit_store)
    assert len(found) >= 10, "no SQL found — this test would pass vacuously"
    for name, statement in found.items():
        before_returning = re.split(r"\bRETURNING\b", statement)[0]
        assert NAMES_THE_OWNER.search(before_returning), f"{name} does not name the owner"


def test_the_store_writes_only_its_own_tables_and_holds_rows_the_rq3_way() -> None:
    found = statements(document_edit_store)
    writes = 0
    for name, statement in found.items():
        write = re.match(r"\s*(INSERT INTO|UPDATE|DELETE FROM)\s+(\S+)", statement)
        if write:
            writes += 1
            assert write.group(2) in {"campaign.document_edits", "campaign.document_edit_attempts"}, name
            assert write.group(1) != "DELETE FROM", f"{name}: the aggregate has no delete"
        assert not re.search(r"\bFOR\s+(UPDATE|SHARE|KEY SHARE)\b", statement), f"{name}: RQ-3 says FOR NO KEY UPDATE"
        assert "now()" not in statement.lower(), f"{name}: the clock is the caller's"
        for column in re.findall(r"\bv\.(\w+)", statement):
            assert column in {"conversation_id", "user_id", "campaign_id"}, f"{name} reads v.{column}"
        for column in re.findall(r"\bd\.(\w+)", statement):
            assert column in {"id", "campaign_id", "type"}, f"{name} reads d.{column}"
    assert writes == 6


# ── PostgreSQL only: the deletion edges (S-10) ───────────────────────────────


def count(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> int:
    with connect(dsn) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def rows_of(dsn: str) -> tuple[int, int]:
    return (count(dsn, "SELECT count(*) FROM campaign.document_edits"),
            count(dsn, "SELECT count(*) FROM campaign.document_edit_attempts"))


def _database(dsn: str) -> Database:
    return Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))


@needs_db
def test_deleting_a_conversation_takes_its_edits_and_their_attempts(dsn: str) -> None:
    world = pg_world(dsn, _database(dsn))
    table = a_table(world)
    kept = a_table(world)
    admit(world, table)
    admit(world, kept)
    with connect(dsn) as conn:
        conn.execute("DELETE FROM chat.conversations WHERE conversation_id = %s", (table.conversation,))
    assert rows_of(dsn) == (1, 1)
    assert held(world, kept) is not None


@needs_db
def test_deleting_the_gms_account_cascades_the_whole_chain_and_the_deferred_check_passes(dsn: str) -> None:
    """S-10(c), C-6.1."""
    world = pg_world(dsn, _database(dsn))
    admit(world, a_table(world))
    admit(world, a_table(world, world.other), OTHER_INV)
    with connect(dsn) as conn:
        conn.execute("DELETE FROM auth.users WHERE id = %s", (world.owner,))
    assert rows_of(dsn) == (1, 1)
    assert count(dsn, "SELECT count(*) FROM campaign.document_edits WHERE owner_id = %s", (world.owner,)) == 0


@needs_db
def test_a_campaign_an_edit_still_references_cannot_be_deleted(dsn: str) -> None:
    """Refused at COMMIT with every row untouched, even once the conversation's
    own edge is out of the way."""
    world = pg_world(dsn, _database(dsn))
    table = a_table(world)
    admit(world, table)
    with connect(dsn) as conn:
        conn.execute("UPDATE chat.conversations SET campaign_id = NULL WHERE conversation_id = %s",
                     (table.conversation,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (table.campaign,))
    assert rows_of(dsn) == (1, 1)

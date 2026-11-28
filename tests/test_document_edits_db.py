"""PostgreSQL checks for the AI edit admission lock and completion fence."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Barrier, Event

import pytest
from _pg import connect, needs_db, throwaway_database
from test_tool_invocation_db import PATIENT, _someone_waits_on_a_lock

import config
from service import document_edits, migrations, ratelimit, tool_invocations, tool_invocations_api
from service.campaign_store import PostgresCampaignStore
from service.conversation_store import PostgresConversationStore
from service.db import AdvisoryLock, Database, PoolSettings
from service.document_edit_store import PostgresDocumentEditStore
from service.document_editing import EditProposal
from service.document_store import PostgresDocumentStore
from service.session import SessionData
from service.timeline_store import PostgresTimelineStore
from service.workbench_contracts import (
    Author,
    DocumentTypeId,
    EditInvocation,
    EditRequest,
    ErrorCode,
    ToolId,
    ToolInvocationRequest,
)
from service.workbench_load import PostgresWorkbenchLoad

pytestmark = needs_db
NOW = datetime(2026, 11, 19, tzinfo=UTC)


@dataclass(frozen=True)
class World:
    db: Database
    stores: document_edits.EditStores
    user: SessionData
    request: EditRequest
    dsn: str = field(repr=False)

    def submit(self, request: EditRequest | None = None) -> document_edits.EditAdmission | EditInvocation:
        return document_edits.submit(
            self.db, self.stores, self.user, request or self.request, enabled=True,
            clock=lambda: NOW, chat_turns_today=0,
        )

    def submit_tool(self, invocation_id: str) -> tool_invocations.Admission | tool_invocations.Replay:
        request = ToolInvocationRequest.model_validate({
            "schema_version": 1, "invocation_id": invocation_id, "tool_id": "npc", "brief": "A smith",
            "campaign_id": self.request.campaign_id, "conversation_id": self.request.conversation_id,
        })
        return tool_invocations.submit(
            self.db, tool_invocations_api.get_invocation_stores(), tool_invocations_api.get_tool_executors(),
            tool_invocations.ToolSettings(frozenset({ToolId.NPC})), self.user, request, now=NOW, chat_turns_today=0,
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    monkeypatch.setattr(ratelimit, "check_chat_request", lambda owner: None)
    with throwaway_database("edits") as dsn:
        migrations.migrate(dsn)
        with connect(dsn) as conn:
            owner = int(conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id",
            ).fetchone()[0])
        db = Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=10), PATIENT)
        stores = document_edits.EditStores(
            PostgresDocumentEditStore(), PostgresCampaignStore(), PostgresConversationStore(),
            PostgresDocumentStore(), PostgresTimelineStore(), PostgresWorkbenchLoad(),
        )
        with db.transaction() as unit:
            campaign = stores.campaigns.create(unit, owner_id=owner, name="Lantern Road", now=NOW).id
            conversation = stores.conversations.create(unit, owner_id=owner, campaign_id=campaign,
                                                       title=None, started_mode=None).id
            document = stores.documents.create(
                unit, campaign, doc_type=DocumentTypeId.NPC, type_version=1, author=Author.GM,
                data={"name": "Maren", "wants": "An honest bargain", "voice": "Quiet"}, now=NOW,
            ).id
        request = EditRequest.model_validate({
            "schema_version": 1, "invocation_id": "inv_db_edits_000000001", "campaign_id": campaign,
            "conversation_id": conversation, "document_id": document, "base_write_revision": 1,
            "scope": {"kind": "field", "field": "wants"},
            "instruction": {"kind": "text", "text": "Shorten the goal"},
        })
        try:
            yield World(db, stores, SessionData(user_id=owner, role="dm"), request, dsn)
        finally:
            db.close()


def test_parallel_submissions_share_one_attempt_and_its_working_status(world: World) -> None:
    ready = Barrier(2, timeout=10)

    def submit() -> document_edits.EditAdmission | EditInvocation:
        ready.wait()
        return world.submit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        second = pool.submit(submit)
        outcomes = [first.result(timeout=20), second.result(timeout=20)]
    assert sum(isinstance(value, document_edits.EditAdmission) for value in outcomes) == 1
    [replay] = [value for value in outcomes if isinstance(value, EditInvocation)]
    assert (replay.status.value, replay.attempt) == ("working", 1)
    request = world.request
    with world.db.transaction() as unit:
        attempts = world.stores.edits.attempts(unit, world.user.user_id, request.campaign_id, request.invocation_id)
    assert len(attempts) == 1


def _race_at_admission(
    world: World, monkeypatch: pytest.MonkeyPatch, first: Callable[[], object], second: Callable[[], object],
) -> tool_invocations.Refused:
    """Hold the winner after its cap checks and let PostgreSQL confirm the contender is blocked."""
    inside, release = Event(), Event()
    spent: list[int] = []

    def spend(owner: int) -> None:
        spent.append(owner)
        inside.set()
        assert release.wait(timeout=15)

    monkeypatch.setattr(ratelimit, "check_chat_request", spend)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(first)
        try:
            assert inside.wait(timeout=15)
            contender = pool.submit(second)
            assert _someone_waits_on_a_lock(world.dsn)
        finally:
            release.set()
        assert isinstance(winner.result(timeout=15), tool_invocations.Admission | document_edits.EditAdmission)
        with pytest.raises(tool_invocations.Refused) as rejected:
            contender.result(timeout=15)
    assert spent == [world.user.user_id]
    assert (rejected.value.status, rejected.value.info.code, rejected.value.info.retryable) == (
        409, ErrorCode.CAP_REACHED, True,
    )
    return rejected.value


def _assert_admitted(world: World, expected: dict[str, str]) -> None:
    with world.db.transaction() as unit:
        assert dict(world.stores.load.in_flight(unit, world.user.user_id, now=NOW)) == expected
        assert world.stores.load.attempts_since(unit, NOW) == len(expected)
        entries = world.stores.timeline.entry_window(unit, world.request.conversation_id, before=None, limit=10)
        assert sorted((e.payload["invocation"]["invocation_id"], e.payload["entry_kind"]) for e in entries) == sorted(
            expected.items(),
        )


def test_distinct_simultaneous_edit_ids_for_one_document_admit_only_one(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = world.request
    second = first.model_copy(update={"invocation_id": "inv_db_competing_edit_02"})
    refusal = _race_at_admission(world, monkeypatch, world.submit, partial(world.submit, second))
    assert refusal.info.in_flight == [first.invocation_id]
    _assert_admitted(world, {first.invocation_id: "edit"})


@pytest.mark.parametrize("first", ["tool", "edit"])
def test_a_tool_and_an_edit_competing_for_the_last_slot(
    world: World, monkeypatch: pytest.MonkeyPatch, first: str,
) -> None:
    held, tool, edit = "inv_db_occupied_slot_01", "inv_db_competing_tool_02", world.request.invocation_id
    assert isinstance(world.submit_tool(held), tool_invocations.Admission)
    submit_tool = partial(world.submit_tool, tool)
    winner, contender = (submit_tool, world.submit) if first == "tool" else (world.submit, submit_tool)
    refusal = _race_at_admission(world, monkeypatch, winner, contender)
    expected = {held: "tool", (tool if first == "tool" else edit): first}
    assert sorted(refusal.info.in_flight or []) == sorted(expected)
    _assert_admitted(world, expected)


@pytest.mark.parametrize("changed_key", ["voice", "wants"])
def test_a_concurrent_gm_save_is_preserved_at_completion(world: World, changed_key: str) -> None:
    admitted = world.submit()
    assert isinstance(admitted, document_edits.EditAdmission)
    request = world.request
    with world.db.transaction() as unit:
        world.stores.documents.write_fields(
            unit, request.campaign_id, request.document_id, fields={changed_key: "The GM's newer text"},
            author=Author.GM, base_write_revision=1, now=NOW + timedelta(seconds=1),
        )
    result = document_edits.complete(
        world.db, world.stores, admitted, EditProposal({"wants": "The assistant's suggestion"}, ("wants",)),
        clock=lambda: NOW + timedelta(seconds=2),
    )
    with world.db.transaction() as unit:
        document = world.stores.documents.get(unit, request.campaign_id, request.document_id)
    assert document is not None
    assert document.data[changed_key] == "The GM's newer text"
    if changed_key == "wants":
        assert result.status.value == "failed"
        assert result.error is not None and result.error.code.value == "conflict"
        assert document.write_revision == 2
    else:
        assert result.status.value == "done"
        assert document.data["wants"] == "The assistant's suggestion"
        assert document.write_revision == 3 and document.version.is_sealed


def test_edit_exceeding_the_account_byte_quota_is_settled_without_a_version_or_paid_retry(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted = world.submit()
    assert isinstance(admitted, document_edits.EditAdmission)
    request = world.request
    owner = world.user.user_id
    with world.db.transaction() as unit:
        before = world.stores.documents.get(unit, request.campaign_id, request.document_id)
        used = world.stores.documents.stored_bytes(unit, owner)
    assert before is not None and before.version.is_sealed and used > 0
    # Storage is full when the provider returns its proposal. The version write
    # and the invocation result must agree even though admission succeeded.
    monkeypatch.setattr(config, "WORKBENCH_DOCUMENT_BYTES_PER_ACCOUNT_MAX", used)
    result = document_edits.complete(
        world.db, world.stores, admitted, EditProposal({"wants": "A larger, more detailed bargain"}, ("wants",)),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert result.status.value == "failed" and result.error is not None
    assert (result.error.code, result.error.retryable) == (ErrorCode.ACCOUNT_LIMIT_REACHED, False)
    assert world.submit() == result
    with world.db.transaction() as unit:
        assert world.stores.documents.get(unit, request.campaign_id, request.document_id) == before
        assert world.stores.documents.stored_bytes(unit, owner) == used
        assert world.stores.load.attempts_since(unit, NOW) == 1
        [entry] = world.stores.timeline.entry_window(unit, request.conversation_id, before=None, limit=10)
        assert EditInvocation.model_validate(entry.payload["invocation"]) == result


def test_proposal_waiting_for_the_account_storage_lock_cannot_write_past_its_deadline(world: World) -> None:
    admitted = world.submit()
    assert isinstance(admitted, document_edits.EditAdmission)
    request = world.request
    elapsed = Event()

    def clock() -> datetime:
        return admitted.row.attempt_deadline if elapsed.is_set() else NOW + timedelta(seconds=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with world.db.transaction() as holder:
            holder.lock(AdvisoryLock.ACCOUNT_STORAGE, str(world.user.user_id))
            before = world.stores.documents.get(holder, request.campaign_id, request.document_id)
            assert before is not None
            pending = pool.submit(document_edits.complete, world.db, world.stores, admitted,
                                  EditProposal({"wants": "A late change"}, ("wants",)), clock=clock)
            assert _someone_waits_on_a_lock(world.dsn)
            elapsed.set()
        result = pending.result(timeout=15)
    assert result.status.value == "failed" and result.error is not None
    assert result.error.code is ErrorCode.ATTEMPT_EXPIRED
    with world.db.transaction() as unit:
        assert world.stores.documents.get(unit, request.campaign_id, request.document_id) == before
        [attempt] = world.stores.edits.attempts(unit, world.user.user_id, request.campaign_id, request.invocation_id)
        assert attempt.outcome == "expired"
        [entry] = world.stores.timeline.entry_window(unit, request.conversation_id, before=None, limit=10)
        assert EditInvocation.model_validate(entry.payload["invocation"]) == result


@pytest.mark.parametrize("cancel", [False, True], ids=["status", "cancel"])
def test_status_and_cancel_judge_expiry_after_acquiring_the_invocation_row(world: World, cancel: bool) -> None:
    admitted = world.submit()
    assert isinstance(admitted, document_edits.EditAdmission)
    request = world.request
    identity = (world.user.user_id, request.campaign_id, request.invocation_id)
    elapsed = Event()

    def clock() -> datetime:
        return admitted.row.attempt_deadline if elapsed.is_set() else NOW + timedelta(seconds=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with world.db.transaction() as holder:
            held = world.stores.edits.get_for_update(holder, *identity)
            assert held is not None and held.status == "working" and not held.cancel_requested
            pending = pool.submit(
                document_edits.read, world.db, world.stores, world.user.user_id, request.campaign_id,
                request.document_id, request.invocation_id, clock=clock, cancel=cancel,
            )
            assert _someone_waits_on_a_lock(world.dsn)
            elapsed.set()
        result = pending.result(timeout=15)

    assert result.status.value == "failed" and result.error is not None and not result.cancel_requested
    assert result.error.code is ErrorCode.ATTEMPT_EXPIRED
    with world.db.transaction() as unit:
        stored = world.stores.edits.get_for_update(unit, *identity)
        assert stored is not None and document_edits.to_wire(stored) == result
        [attempt] = world.stores.edits.attempts(unit, *identity)
        assert (attempt.outcome, attempt.ended_at) == ("expired", admitted.row.attempt_deadline)
        [entry] = world.stores.timeline.entry_window(unit, request.conversation_id, before=None, limit=10)
        assert EditInvocation.model_validate(entry.payload["invocation"]) == result

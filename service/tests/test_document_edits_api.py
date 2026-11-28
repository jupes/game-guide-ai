"""AI document edits against the real router, with in-memory stores and a fake provider."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from service import document_edits, document_edits_api, ratelimit, usage_capture
from service.app import app, get_timeline_database, require_session
from service.campaign_store import InMemoryCampaignStore
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase, UnitOfWork
from service.document_edit_store import EditNotStored, InMemoryDocumentEditStore
from service.document_editing import EditProposal
from service.document_store import DocumentRecord, InMemoryDocumentStore
from service.history import InMemoryMessageStore
from service.model_catalog import DEFAULT_ALIAS
from service.providers import ProviderClientFactory
from service.session import SessionData
from service.tests import document_editing_fixtures as fx
from service.timeline import read_page
from service.timeline_store import InMemoryTimelineStore
from service.workbench_api import FORBIDDEN_ORIGIN_DETAIL
from service.workbench_contracts import Author, EditEntry
from service.workbench_load import InMemoryWorkbenchLoad

NOW = datetime(2026, 11, 19, tzinfo=UTC)
KEY = "inv_edits_0000000001"
INITIAL = "🎲 A secret offer is hidden nearby."


class Client:
    def __init__(self) -> None:
        self.response = fx.fields_envelope({"wants": "Find the missing key."})
        self.calls: list[Any] = []
        self.inside_call: Callable[[], None] = lambda: None

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append(input)
        self.inside_call()
        return AIMessage(
            content=self.response, usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        )


class Ledger:
    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.count_reads: list[datetime] = []
        self.chat_turns = 0

    def chat_turns_since(self, unit: UnitOfWork, *, since: datetime) -> int:
        self.count_reads.append(since)
        return self.chat_turns

    def write(self, rows: Any) -> int:
        self.rows.extend(rows)
        return len(rows)


class World:
    def __init__(self) -> None:
        self.db = InMemoryDatabase()
        self.messages = InMemoryMessageStore()
        self.stores = document_edits.EditStores(
            InMemoryDocumentEditStore(self.db), InMemoryCampaignStore(self.db), InMemoryConversationStore(self.db),
            InMemoryDocumentStore(self.db), InMemoryTimelineStore(self.db, messages=self.messages),
            InMemoryWorkbenchLoad(self.db),
        )
        self.provider = Client()
        self.ledger = Ledger()
        self.web = TestClient(app)
        self.who = 1
        self.enabled = True
        self.now = NOW
        self.spent: list[int] = []
        with self.db.transaction() as unit:
            self.campaign = self.stores.campaigns.create(unit, owner_id=1, name="Lantern Road").id
            self.conversation = self.stores.conversations.create(
                unit, owner_id=1, campaign_id=self.campaign, title=None, started_mode=None,
            ).id
        self.messages.claim_conversation(self.conversation, 1)
        self.document = self.create_doc()
        self.path = f"/campaigns/{self.campaign}/documents/{self.document}/edits"

    def create_doc(self) -> str:
        with self.db.transaction() as unit:
            return self.stores.documents.create(
                unit, self.campaign, doc_type=fx.NPC, type_version=1, author=Author.GM,
                data={"name": "Maren", "wants": INITIAL, "voice": "Soft"}, now=self.now,
            ).id

    def record(self) -> DocumentRecord:
        with self.db.transaction() as unit:
            record = self.stores.documents.get(unit, self.campaign, self.document)
        assert record is not None
        return record

    def patch(self, **fields: str) -> None:
        with self.db.transaction() as unit:
            self.stores.documents.write_fields(
                unit, self.campaign, self.document, fields=fields, author=Author.GM, base_write_revision=None,
                now=self.now,
            )

    def body(self, **changes: Any) -> dict[str, Any]:
        return {
            "schema_version": 1, "campaign_id": self.campaign, "conversation_id": self.conversation,
            "document_id": self.document, "invocation_id": KEY, "base_write_revision": 1,
            "scope": {"kind": "field", "field": "wants"}, "instruction": {"kind": "text", "text": "Shorten the goal"},
            **changes,
        }

    def selected(self) -> dict[str, Any]:
        return {"kind": "selection", "field": "wants", "start": 4, "end": 16, "text": INITIAL[4:16]}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    w = World()
    factory = ProviderClientFactory(client_builders={DEFAULT_ALIAS: w.provider})
    original = app.dependency_overrides.copy()
    app.dependency_overrides.update({
        require_session: lambda: SessionData(user_id=w.who, role="dm"),
        get_timeline_database: lambda: w.db,
        document_edits_api.get_edit_ledger: lambda: w.ledger,
        document_edits_api.get_edit_stores: lambda: w.stores,
        document_edits_api.get_edit_settings: lambda: w.enabled,
        document_edits_api.get_clock: lambda: lambda: w.now,
        document_edits_api.get_provider_factory: lambda: factory,
    })
    monkeypatch.setattr(ratelimit, "check_chat_request", w.spent.append)
    monkeypatch.setattr(usage_capture, "_ledger_provider", lambda: w.ledger)
    try:
        yield w
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


def test_edit_refuses_non_owner_before_the_switch_and_uses_the_one_404(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world.enabled = False
    disabled = world.web.post(world.path, json=world.body())
    assert (disabled.status_code, disabled.json()["detail"]["code"]) == (409, "edit_disabled")
    world.who = 2

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a foreign campaign must be refused before loading its private document or conversation")

    monkeypatch.setattr(world.stores.documents, "get", unexpected)
    monkeypatch.setattr(world.stores.conversations, "campaign_for_owner", unexpected)
    refused = world.web.post(world.path, json=world.body())
    status = world.web.get(f"{world.path}/{KEY}")
    cancelled = world.web.post(f"{world.path}/{KEY}/cancel")
    assert refused.status_code == status.status_code == cancelled.status_code == 404
    assert refused.content == status.content == cancelled.content
    assert world.spent == world.provider.calls == world.ledger.count_reads == []


def test_edit_routes_require_the_gm_role_and_same_origin_for_writes(world: World) -> None:
    origin = {"Origin": "https://other.invalid"}
    for denied in (
        world.web.post(world.path, json=world.body(), headers=origin),
        world.web.post(f"{world.path}/{KEY}/cancel", headers=origin),
    ):
        assert denied.status_code == 403 and denied.json()["detail"] == dict(FORBIDDEN_ORIGIN_DETAIL)
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=world.who, role="player")
    responses = [
        world.web.post(world.path, json=world.body()), world.web.get(f"{world.path}/{KEY}"),
        world.web.post(f"{world.path}/{KEY}/cancel"),
    ]
    for response in responses:
        assert response.status_code == 403 and response.json()["detail"]["code"] == "forbidden"
        assert response.headers["Cache-Control"] == "no-store"
    assert world.spent == world.provider.calls == world.ledger.count_reads == []


def test_selection_mismatch_is_free_and_leaves_the_gm_version_open(world: World) -> None:
    scope = {**world.selected(), "text": "x" * (world.selected()["end"] - world.selected()["start"])}
    refusal = world.web.post(world.path, json=world.body(scope=scope))
    assert refusal.status_code == 409
    assert refusal.json()["detail"]["conflict"] == {"write_revision": 1, "fields": ["wants"]}
    assert "x" * 12 not in refusal.text
    assert world.spent == world.provider.calls == world.ledger.count_reads == []
    assert world.record().version.is_sealed is False


def test_selection_writes_only_the_span_and_replays_the_committed_diff(world: World) -> None:
    selection = world.selected()
    world.provider.response = fx.replacement_envelope("a lantern")
    body = world.body(scope=selection, instruction={"kind": "action", "action": "rewrite"})
    response = world.web.post(world.path, json=body)
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["status"] == "done", done
    assert done["result"]["changed_fields"] == ["wants"]
    assert done["result"]["version_number"] == done["result"]["write_revision"] == 2
    record = world.record()
    assert record.data["wants"] == INITIAL[:selection["start"]] + "a lantern" + INITIAL[selection["end"]:]
    assert record.version.author == "assistant" and record.version.is_sealed
    assert record.data["voice"] == "Soft"
    assert world.web.post(world.path, json=body).json() == done
    assert world.web.get(f"{world.path}/{KEY}").json() == done
    assert world.spent == [1] and len(world.provider.calls) == 1
    with world.db.transaction() as unit:
        row = world.stores.edits.get_for_update(unit, 1, world.campaign, KEY)
        [attempt] = world.stores.edits.attempts(unit, 1, world.campaign, KEY)
        page = read_page(world.stores.timeline, unit, world.conversation, limit=5, cursor=None)
    assert row is not None and row.selection_text is None
    [entry] = page.items
    assert isinstance(entry, EditEntry)
    assert entry.scope.model_dump() == {"kind": "selection", "field": "wants"}
    assert entry.invocation.model_dump(mode="json") == done
    assert [(event.operation, event.purpose, event.operation_id) for event in world.ledger.rows] == [
        ("document_edit", "document_edit", attempt.operation_id),
    ]


def test_same_field_concurrent_change_is_conflict_and_retry_keeps_original_scope(world: World) -> None:
    world.provider.inside_call = lambda: world.patch(wants="The GM's new goal.")
    failed = world.web.post(world.path, json=world.body()).json()
    assert (failed["status"], failed["error"]["code"]) == ("failed", "conflict")
    assert failed["error"]["conflict"]["fields"] == ["wants"]
    assert world.record().data["wants"] == "The GM's new goal."
    world.provider.inside_call = lambda: world.patch(voice="Firm")
    retry = world.web.post(world.path, json=world.body(
        base_write_revision=world.record().write_revision, scope={"kind": "field", "field": "voice"},
        instruction={"kind": "text", "text": "Ignored revision instruction"},
    )).json()
    assert (retry["status"], retry["attempt"], retry["result"]["changed_fields"]) == ("done", 2, ["wants"])
    assert world.record().data["voice"] == "Firm"
    assert world.record().data["wants"] == "Find the missing key."
    assert "Ignored revision instruction" not in world.provider.calls[-1][1].content


def test_invalid_provider_patch_is_retryable_and_no_change_creates_no_revision(world: World) -> None:
    world.provider.response = fx.fields_envelope({"voice": "Disallowed outside the field scope"})
    failed = world.web.post(world.path, json=world.body()).json()
    assert failed["status"] == "failed"
    assert (failed["error"]["code"], failed["error"]["retryable"]) == ("provider_failed", True)
    assert world.record().write_revision == 1
    world.provider.response = fx.fields_envelope({"wants": INITIAL})
    done = world.web.post(world.path, json=world.body()).json()
    assert (done["status"], done["attempt"], done["result"]["outcome"]) == ("done", 2, "no_change")
    assert world.record().write_revision == 1
    assert world.record().version.is_sealed


def test_cancel_during_provider_preserves_finished_change_and_expiry_drops_late_result(world: World) -> None:
    world.provider.inside_call = lambda: world.web.post(f"{world.path}/{KEY}/cancel")
    done = world.web.post(world.path, json=world.body()).json()
    assert (done["status"], done["cancel_requested"]) == ("done", True)

    world.provider.inside_call = lambda: setattr(world, "now", world.now + timedelta(minutes=5))
    world.provider.response = fx.fields_envelope({"wants": "Too late"})
    expired = world.web.post(world.path, json=world.body(
        invocation_id="inv_edits_0000000002", base_write_revision=2,
    )).json()
    assert (expired["status"], expired["error"]["code"]) == ("failed", "attempt_expired")
    assert world.record().write_revision == 2
    assert world.record().data["wants"] == "Find the missing key."


def test_expiry_during_timeline_write_rolls_back_the_document_and_publishes_the_expired_attempt(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace_entry = world.stores.timeline.replace_edit_invocation

    def delayed(unit: UnitOfWork, conversation: str, entry: EditEntry, at: datetime, *, owner_id: int) -> str:
        result = replace_entry(unit, conversation, entry, at, owner_id=owner_id)
        if entry.invocation.status == "done":
            world.now += timedelta(minutes=5)
        return result

    monkeypatch.setattr(world.stores.timeline, "replace_edit_invocation", delayed)
    response = world.web.post(world.path, json=world.body())
    assert response.status_code == 200
    expired = response.json()
    assert (expired["status"], expired["error"]["code"]) == ("failed", "attempt_expired")
    assert world.web.get(f"{world.path}/{KEY}").json() == expired
    document = world.record()
    assert document.write_revision == document.version.number == 1 and document.data["wants"] == INITIAL
    with world.db.transaction() as unit:
        [entry] = read_page(world.stores.timeline, unit, world.conversation, limit=5, cursor=None).items
    assert isinstance(entry, EditEntry) and entry.invocation.model_dump(mode="json") == expired


@pytest.mark.parametrize(("fields", "changed"), [
    ({"voice": "Outside scope"}, ("voice",)), ({"wants": "A"}, ("wants",)),
])
def test_write_fence_refuses_a_tampered_scope_or_selection(
    world: World, monkeypatch: pytest.MonkeyPatch, fields: dict[str, str], changed: tuple[str, ...],
) -> None:
    monkeypatch.setattr(document_edits, "execute_edit", lambda *args: EditProposal(fields, changed))
    response = world.web.post(world.path, json=world.body(scope=world.selected()))
    assert response.status_code == 503
    assert world.record().write_revision == 1
    status = world.web.get(f"{world.path}/{KEY}")
    assert status.json()["status"] == "working"


def test_a_key_from_another_document_is_the_same_404_on_all_edit_routes(world: World) -> None:
    assert world.web.post(world.path, json=world.body()).status_code == 200
    other = world.create_doc()
    path = f"/campaigns/{world.campaign}/documents/{other}/edits"
    post = world.web.post(path, json=world.body(document_id=other))
    status = world.web.get(f"{path}/{KEY}")
    cancelled = world.web.post(f"{path}/{KEY}/cancel")
    assert post.status_code == status.status_code == cancelled.status_code == 404
    assert post.content == status.content == cancelled.content
    assert len(world.provider.calls) == 1


def test_an_edit_cannot_be_retried_from_another_owned_conversation(world: World) -> None:
    world.provider.response = fx.fields_envelope({"voice": "Outside the requested field."})
    failed = world.web.post(world.path, json=world.body()).json()
    assert failed["status"] == "failed" and failed["error"]["retryable"]
    with world.db.transaction() as unit:
        other = world.stores.conversations.create(
            unit, owner_id=world.who, campaign_id=world.campaign, title=None, started_mode=None,
        ).id
    denied = world.web.post(world.path, json=world.body(conversation_id=other))
    assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
    assert world.ledger.count_reads == [NOW] and len(world.provider.calls) == 1
    assert world.web.get(f"{world.path}/{KEY}").json() == failed


def test_a_second_edit_of_the_same_document_is_refused_without_spending(world: World) -> None:
    def try_second() -> None:
        response = world.web.post(world.path, json=world.body(invocation_id="inv_edits_0000000002"))
        assert response.status_code == 409
        assert response.json()["detail"]["in_flight"] == [KEY]

    world.provider.inside_call = try_second
    result = world.web.post(world.path, json=world.body())
    assert result.json()["status"] == "done"
    assert world.spent == [1] and world.ledger.count_reads == [NOW]


def test_admission_consults_usage_in_its_transaction_and_a_replay_survives_a_ledger_outage(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    units: list[UnitOfWork] = []
    lookup = world.stores.load.attempts_since

    def attempts(unit: UnitOfWork, since: datetime) -> int:
        units.append(unit)
        return lookup(unit, since)

    def count(unit: UnitOfWork, *, since: datetime) -> int:
        assert since == NOW
        units.append(unit)
        return 0

    def broken(unit: UnitOfWork, *, since: datetime) -> int:
        raise RuntimeError("private ledger failure message")

    monkeypatch.setattr(world.stores.load, "attempts_since", attempts)
    monkeypatch.setattr(world.ledger, "chat_turns_since", count)
    done = world.web.post(world.path, json=world.body()).json()
    assert done["status"] == "done"
    assert len(units) == 2 and units[0] is units[1]
    monkeypatch.setattr(world.ledger, "chat_turns_since", broken)
    assert world.web.post(world.path, json=world.body()).json() == done
    fresh = "inv_edits_0000000002"
    new = world.web.post(world.path, json=world.body(invocation_id=fresh, base_write_revision=2))
    assert new.status_code == 503
    assert new.json()["detail"]["code"] == "backend_unavailable" and new.json()["detail"]["retryable"]
    assert "private ledger failure message" not in new.text + caplog.text
    assert world.web.get(f"{world.path}/{fresh}").status_code == 404
    assert world.spent == [1] and len(world.provider.calls) == 1


def test_edit_throttle_carries_the_retry_delay_and_creates_no_attempt(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def limited(owner: int) -> None:
        raise ratelimit.RateLimited(17)

    monkeypatch.setattr(ratelimit, "check_chat_request", limited)
    denied = world.web.post(world.path, json=world.body())
    assert denied.status_code == 429 and denied.headers["retry-after"] == "17"
    assert denied.json()["detail"]["code"] == "throttled_user" and denied.json()["detail"]["retry_after_s"] == 17
    assert world.web.get(f"{world.path}/{KEY}").status_code == 404
    assert world.provider.calls == []


def test_selection_conflict_settles_before_rechecking_its_old_span(world: World) -> None:
    world.provider.response = fx.replacement_envelope("a lantern")
    world.provider.inside_call = lambda: world.patch(wants="A new sentence from the GM.")
    body = world.body(scope=world.selected(), instruction={"kind": "action", "action": "rewrite"})
    response = world.web.post(world.path, json=body)
    assert response.status_code == 200
    result = response.json()
    assert (result["status"], result["error"]["code"]) == ("failed", "conflict")
    assert result["error"]["conflict"] == {"write_revision": 2, "fields": ["wants"]}
    assert world.record().data["wants"] == "A new sentence from the GM."


def test_matching_prefix_and_suffix_cannot_overlap_after_a_selection_edit(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world.patch(wants="aaaaa")
    monkeypatch.setattr(document_edits, "execute_edit", lambda *args: EditProposal({"wants": "aa"}, ("wants",)))
    body = world.body(
        base_write_revision=2, scope={"kind": "selection", "field": "wants", "start": 2, "end": 3, "text": "a"},
    )
    assert world.web.post(world.path, json=body).status_code == 503
    assert world.record().data["wants"] == "aaaaa"


@pytest.mark.parametrize(("changes", "field"), [
    ({"base_write_revision": 2}, "base_write_revision"),
    ({"scope": {"kind": "field", "field": "tags"}}, "scope"),
])
def test_invalid_base_and_scope_are_refused_before_admission(world: World, changes: dict[str, Any], field: str) -> None:
    response = world.web.post(world.path, json=world.body(**changes))
    assert (response.status_code, response.json()["detail"]["field"]) == (422, field)
    assert world.spent == world.provider.calls == []


def test_oversized_body_is_refused_without_echoing_the_request(world: World) -> None:
    response = world.web.post(world.path, content=b"x" * (document_edits_api.EDIT_BODY_MAX_BYTES + 1),
                              headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert "field" not in response.json()["detail"]
    assert world.spent == world.provider.calls == []


def test_a_guarded_admission_loss_leaves_no_orphan_timeline_entry(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lost(*args: Any, **kwargs: Any) -> None:
        raise EditNotStored()

    monkeypatch.setattr(world.stores.edits, "create", lost)
    assert world.web.post(world.path, json=world.body()).status_code == 404
    with world.db.transaction() as unit:
        page = read_page(world.stores.timeline, unit, world.conversation, limit=5, cursor=None)
    assert page.items == []
    assert world.provider.calls == [] and not world.record().version.is_sealed


def test_unexpected_engine_error_stays_retryable_and_logs_no_private_text(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def failure(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private provider exception")

    monkeypatch.setattr(document_edits, "edit_document", failure)
    response = world.web.post(world.path, json=world.body())
    assert response.status_code == 200
    invocation = response.json()
    error = invocation["error"]
    assert invocation["status"] == "failed"
    assert (error["code"], error["message"], error["retryable"]) == (
        "backend_unavailable", document_edits.UNAVAILABLE, True,
    )
    assert "private provider exception" not in response.text + caplog.text
    assert world.record().write_revision == 1


def test_merge_validation_failure_is_provider_failed_without_a_conflict(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_merge(*args: Any, **kwargs: Any) -> None:
        raise ValueError("invalid merged document")

    monkeypatch.setattr(world.stores.documents, "write_fields", invalid_merge)
    response = world.web.post(world.path, json=world.body())
    error = response.json()["error"]
    assert response.status_code == 200
    assert (error["code"], error["retryable"], error["conflict"]) == ("provider_failed", True, None)
    assert world.record().write_revision == 1


def test_missing_database_reports_the_edit_outage(world: World) -> None:
    app.dependency_overrides[get_timeline_database] = lambda: None
    response = world.web.post(world.path, json=world.body())
    assert response.status_code == 503
    assert response.json()["detail"]["message"] == document_edits.UNAVAILABLE
    status = world.web.get(f"{world.path}/{KEY}")
    assert status.status_code == 503 and status.content == response.content

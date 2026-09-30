"""The character-sheet link routes (agent-forge-harness-q156), through the real
app over the in-memory twins.

Self-contained (not added to `test_documents_api.py`, which is already 1,605
lines): its own `_CountingDatabase`, `InMemoryCampaignStore`,
`InMemoryDocumentStore`, `InMemoryParticipantStore` (to add, offer, accept and
remove seats), `InMemoryTableSessionStore(slot_clear=no_slots)` and
`InMemoryAuditLog`, wired through `document_lifecycle_api.get_lifecycle_stores`.

Who waits for whom is `tests/test_character_link_api_db.py`'s; this file holds
what a client observes: the answers and their order (SEC-3), the B-4 decision
order, the one 404 across tenants, and that no private text leaves (T-12).
Every test names the mutant it kills.

Run from the repo root:
    uv run python -m pytest service/tests/test_character_link_api.py -q
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from httpx import Response

from service import document_lifecycle_api, documents_api
from service.app import app, get_timeline_database, require_session
from service.audit_log import AuditAction, AuditEvent, InMemoryAuditLog, ObjectKind
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase, InMemoryTransaction
from service.document_store import DocumentRecord, InMemoryDocumentStore
from service.participant_store import InMemoryParticipantStore
from service.session import SessionData
from service.table_session_store import InMemoryTableSessionStore, TableSession, no_slots
from service.workbench_api import FORBIDDEN_ORIGIN_DETAIL, FORBIDDEN_ROLE_DETAIL, NOT_FOUND_DETAIL
from service.workbench_contracts import Author, CharacterSheetLink, DocumentTypeId

GM_A, GM_B, PLAYER = 1, 2, 3
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
UNAUTHENTICATED = {"detail": "not signed in"}
CANARY = "Zx9Canary"
MISSING_DOC = "doc_" + "z" * 22
_COMMANDS = itertools.count()

MINIMAL: dict[str, dict[str, Any]] = {"npc": {"name": "Mira"}, "character-sheet": {"name": "Wren"}}


def _command() -> str:
    return f"cmd-link-{next(_COMMANDS):012d}-abc"


# ── The world ────────────────────────────────────────────────────────────────


class _CountingDatabase(InMemoryDatabase):
    """The twin, keeping every unit of work it opened — so a test can say that
    a refusal opened none, and that no stranger's unit ever took a lock."""

    def __init__(self) -> None:
        super().__init__()
        self.units: list[InMemoryTransaction] = []

    @contextmanager
    def transaction(self) -> Iterator[InMemoryTransaction]:
        with super().transaction() as unit:
            self.units.append(unit)
            yield unit


# justification: a store wrapper stands in for a Protocol it does not name;
# each wrapper forwards to the real twin and records or overrides one call.
class _Traced:
    """A store that records every call as (unit, "kind.method", owner_id, the
    campaign locks the unit held at that moment), and lets a test replace one
    method."""

    def __init__(self, inner: Any, kind: str, calls: list[tuple[int, str, Any, tuple[Any, ...]]],
                 overrides: dict[str, Callable[..., Any]] | None = None) -> None:
        self._inner, self._kind, self._calls = inner, kind, calls
        self._overrides = overrides or {}

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            unit = args[0] if args else None
            held = tuple(getattr(unit, "campaign_locks", ()))
            self._calls.append((id(unit), f"{self._kind}.{name}", kwargs.get("owner_id"), held))
            if name in self._overrides:
                return self._overrides[name](target, *args, **kwargs)
            return target(*args, **kwargs)

        return call


@dataclass
class World:
    db: _CountingDatabase
    stores: document_lifecycle_api.LifecycleStores
    participants: InMemoryParticipantStore
    now: list[datetime] = field(default_factory=lambda: [T0])
    calls: list[tuple[int, str, Any, tuple[Any, ...]]] = field(default_factory=list)

    def campaign(self, owner: int = GM_A, name: str = "Nocturne") -> str:
        with self.db.transaction() as unit:
            return self.stores.campaigns.create(unit, owner_id=owner, name=name, now=self.now[0]).id

    def document(self, campaign_id: str, doc_type: str = "character-sheet",
                 data: dict[str, Any] | None = None) -> str:
        with self.db.transaction() as unit:
            return self.stores.documents.create(
                unit, campaign_id, doc_type=DocumentTypeId(doc_type), type_version=1,
                data=data if data is not None else dict(MINIMAL[doc_type]), author=Author.GM, now=self.now[0],
            ).id

    def record(self, campaign_id: str, document_id: str) -> DocumentRecord:
        with self.db.transaction() as unit:
            found = self.stores.documents.get(unit, campaign_id, document_id)
        assert found is not None
        return found

    def seat(self, campaign_id: str, *, alias: str = "Wren") -> str:
        with self.db.transaction() as unit:
            return self.participants.add(unit, campaign_id, alias=alias, now=self.now[0]).id

    def offer(self, campaign_id: str, participant_id: str, *, user_id: int) -> None:
        with self.db.transaction() as unit:
            self.participants.offer(unit, campaign_id, participant_id, user_id=user_id)

    def accept(self, campaign_id: str, participant_id: str, *, user_id: int) -> None:
        with self.db.transaction() as unit:
            self.participants.accept(unit, campaign_id, participant_id, user_id=user_id, now=self.now[0])

    def remove_seat(self, campaign_id: str, participant_id: str) -> None:
        with self.db.transaction() as unit:
            self.participants.remove(unit, campaign_id, participant_id, now=self.now[0])

    def revision(self, campaign_id: str) -> int | None:
        with self.db.transaction() as unit:
            return self.stores.campaigns.authz_revision(unit, campaign_id)

    def ledger(self, campaign_id: str) -> list[AuditEvent]:
        with self.db.transaction() as unit:
            return self.stores.audit.for_campaign(unit, campaign_id)

    def session(self, campaign_id: str, owner: int = GM_A) -> TableSession:
        with self.db.transaction() as unit:
            return self.stores.sessions.start(unit, campaign_id, owner_id=owner,
                                              expires_at=self.now[0] + timedelta(hours=12), command_id=_command(),
                                              now=self.now[0])

    def epoch(self, session_id: str) -> int:
        with self.db.transaction() as unit:
            found = self.stores.sessions.get(unit, session_id)
        assert found is not None
        return found.reveal_epoch

    def spy(self, **overrides: dict[str, Callable[..., Any]]) -> None:
        inner = self.stores
        self.stores = document_lifecycle_api.LifecycleStores(*(
            cast(Any, _Traced(getattr(inner, kind), kind, self.calls, overrides.get(kind)))
            for kind in ("campaigns", "documents", "sessions", "audit")
        ))


@pytest.fixture
def world() -> Iterator[World]:
    db = _CountingDatabase()
    documents = InMemoryDocumentStore(db)
    made = World(
        db,
        document_lifecycle_api.LifecycleStores(
            InMemoryCampaignStore(db), documents, InMemoryTableSessionStore(db, slot_clear=no_slots),
            InMemoryAuditLog(),
        ),
        InMemoryParticipantStore(db),
    )
    app.dependency_overrides[document_lifecycle_api.get_lifecycle_stores] = lambda: made.stores
    app.dependency_overrides[get_timeline_database] = lambda: made.db
    app.dependency_overrides[documents_api.get_clock] = lambda: made.now[0]
    yield made
    for dependency in (document_lifecycle_api.get_lifecycle_stores, get_timeline_database, documents_api.get_clock,
                       require_session):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(world: World) -> TestClient:
    _as(GM_A)
    return TestClient(app)


def _as(user_id: int, role: str = "dm") -> None:
    def session(request: Request) -> SessionData:
        return SessionData(user_id=user_id, role=cast(Any, role))

    app.dependency_overrides[require_session] = session


def _signed_out() -> None:
    def refuse() -> SessionData:
        raise HTTPException(status_code=401, detail="authentication required")

    app.dependency_overrides[require_session] = refuse


# ── URLs and requests ────────────────────────────────────────────────────────


def _link_url(campaign: str, document: str, participant: str) -> str:
    return f"/campaigns/{campaign}/documents/{document}/link/{participant}"


def _unlink_url(campaign: str, document: str) -> str:
    return f"/campaigns/{campaign}/documents/{document}/unlink"


def _read_url(campaign: str, document: str) -> str:
    return f"/campaigns/{campaign}/documents/{document}/link"


def _link(client: TestClient, campaign: str, document: str, participant: str, **kwargs: Any) -> Response:
    return client.post(_link_url(campaign, document, participant), **kwargs)


def _unlink(client: TestClient, campaign: str, document: str, **kwargs: Any) -> Response:
    return client.post(_unlink_url(campaign, document), **kwargs)


def _read(client: TestClient, campaign: str, document: str) -> Response:
    return client.get(_read_url(campaign, document))


def _unchanged(world: World, campaign: str, before: tuple[int | None, int]) -> None:
    assert (world.revision(campaign), len(world.ledger(campaign))) == before


# ── A-1: the posture every route inherits ───────────────────────────────────


def test_the_link_routes_answer_the_scaffoldings_401_403_and_503(client: TestClient, world: World) -> None:
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    routes: list[tuple[str, str]] = [
        ("POST", _link_url(campaign, sheet, seat)), ("POST", _unlink_url(campaign, sheet)),
        ("GET", _read_url(campaign, sheet)),
    ]
    _as(PLAYER, "player")
    for method, path in routes:
        answer = client.request(method, path)
        assert (answer.status_code, answer.json()) == (403, {"detail": dict(FORBIDDEN_ROLE_DETAIL)}), path
    _as(GM_A)
    for method, path in routes:
        if method == "GET":
            continue  # a GET carries no origin check
        refused = client.request(method, path, headers={"origin": "https://evil.example"})
        assert (refused.status_code, refused.json()) == (403, {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}), path
    _signed_out()
    for method, path in routes:
        assert client.request(method, path).json() == UNAUTHENTICATED, path
    _as(GM_A)
    app.dependency_overrides[get_timeline_database] = lambda: None
    for method, path in routes:
        answer = client.request(method, path)
        assert (answer.status_code, answer.json()["detail"]["code"]) == (503, "backend_unavailable"), path
        assert answer.json()["detail"]["retryable"] is True
    app.dependency_overrides[get_timeline_database] = lambda: world.db
    assert world.record(campaign, sheet).linked_participant_id is None and world.ledger(campaign) == []


# ── T1, T2: link lands and locks in the right order ─────────────────────────


def test_link_lands_advances_and_audits(client: TestClient, world: World) -> None:
    """T1. Kills: advance dropped; audit dropped; PARTICIPANT_UNLINKED used;
    object_kind=DOCUMENT; detail missing document_id."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    before = world.revision(campaign)
    assert before is not None
    answer = _link(client, campaign, sheet, seat)
    assert (answer.status_code, answer.content) == (204, b"")
    assert world.record(campaign, sheet).linked_participant_id == seat
    rows = world.ledger(campaign)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AuditAction.PARTICIPANT_LINKED
    assert row.object_kind == ObjectKind.PARTICIPANT
    assert row.object_ref == seat
    assert dict(row.detail) == {"participant_id": seat, "document_id": sheet}
    assert row.authz_revision == before + 1 == world.revision(campaign)
    assert row.actor_ref == str(GM_A)


def test_link_locks_after_ownership_before_the_store(client: TestClient, world: World) -> None:
    """T2. Kills: lock removed; shared=True; ownership read after the lock;
    lock taken after the store call."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    world.spy()
    assert _link(client, campaign, sheet, seat).status_code == 204
    owned = next(c for c in world.calls if c[1] == "campaigns.get")
    linked = next(c for c in world.calls if c[1] == "documents.link_character_sheet")
    assert owned[3] == (), "ownership is read before any lock"
    assert linked[3] == ((campaign, "exclusive"),), "the store is called under the exclusive lock"
    assert world.calls.index(owned) < world.calls.index(linked)


# ── T3: B-4's decision order through the route ──────────────────────────────


def test_link_b4_missing_or_foreign_document(client: TestClient, world: World) -> None:
    """T3(a, b). Kills: a route-side existence check that skips the lock, or
    one that leaks a distinction between missing and foreign."""
    campaign = world.campaign()
    other = world.campaign()
    seat = world.seat(campaign)
    before = (world.revision(campaign), len(world.ledger(campaign)))
    missing = _link(client, campaign, MISSING_DOC, seat)
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND)
    foreign_sheet = world.document(other)
    foreign = _link(client, campaign, foreign_sheet, seat)
    assert (foreign.status_code, foreign.json()) == (404, NOT_FOUND)
    _unchanged(world, campaign, before)


def test_link_b4_not_a_character_sheet(client: TestClient, world: World) -> None:
    """T3(c). Kills: NotLinkable mapped to 409 instead of 422; the field
    reported is anything but document_id."""
    campaign = world.campaign()
    npc = world.document(campaign, "npc")
    seat = world.seat(campaign)
    before = (world.revision(campaign), len(world.ledger(campaign)))
    answer = _link(client, campaign, npc, seat)
    assert answer.status_code == 422
    detail = answer.json()["detail"]
    assert (detail["code"], detail["field"]) == ("validation_failed", "document_id")
    _unchanged(world, campaign, before)


def test_link_b4_type_is_checked_before_the_seat(client: TestClient, world: World) -> None:
    """T3(c'). Kills: the seat checked before the document's type."""
    campaign = world.campaign()
    npc = world.document(campaign, "npc")
    missing_seat = "prt_" + "z" * 22
    answer = _link(client, campaign, npc, missing_seat)
    assert answer.status_code == 422, "type wins over a missing seat"


def test_link_b4_missing_foreign_or_removed_seat(client: TestClient, world: World) -> None:
    """T3(d). Kills: SeatUnavailable-style leniency for a removed seat; a seat
    of another campaign accepted under this one."""
    campaign = world.campaign()
    other = world.campaign()
    sheet = world.document(campaign)
    missing_seat = "prt_" + "z" * 22
    foreign_seat = world.seat(other)
    removed_seat = world.seat(campaign)
    world.remove_seat(campaign, removed_seat)
    before = (world.revision(campaign), len(world.ledger(campaign)))
    for seat in (missing_seat, foreign_seat, removed_seat):
        answer = _link(client, campaign, sheet, seat)
        assert (answer.status_code, answer.json()) == (404, NOT_FOUND), seat
    _unchanged(world, campaign, before)


def test_link_b4_seat_is_checked_before_linked_elsewhere(client: TestClient, world: World) -> None:
    """T3(d'). A sheet already linked elsewhere, aimed at a REMOVED seat:
    still the one 404, not 409 — the seat check comes first. Kills: an
    order that reports link_taken before the seat is shown to exist."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    elsewhere = world.seat(campaign, alias="Elsewhere")
    assert _link(client, campaign, sheet, elsewhere).status_code == 204
    removed = world.seat(campaign, alias="Removed")
    world.remove_seat(campaign, removed)
    answer = _link(client, campaign, sheet, removed)
    assert (answer.status_code, answer.json()) == (404, NOT_FOUND)


def test_link_repeat_to_the_same_seat_is_an_idempotent_noop(client: TestClient, world: World) -> None:
    """T3(e) / T14: a repeat lands 204 and changes nothing — no advance, no
    second row. Kills: an unconditional advance; a second audit row."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    assert _link(client, campaign, sheet, seat).status_code == 204
    before = (world.revision(campaign), len(world.ledger(campaign)))
    again = _link(client, campaign, sheet, seat)
    assert again.status_code == 204
    _unchanged(world, campaign, before)


def test_link_to_a_seat_holding_another_sheet_is_link_taken(client: TestClient, world: World) -> None:
    """T3(f): the sheet is linked elsewhere already — never a re-point.
    Kills: SheetAlreadyLinked mapped to 404 or 500."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat_a = world.seat(campaign, alias="A")
    seat_b = world.seat(campaign, alias="B")
    assert _link(client, campaign, sheet, seat_a).status_code == 204
    before = (world.revision(campaign), len(world.ledger(campaign)))
    answer = _link(client, campaign, sheet, seat_b)
    assert answer.status_code == 409
    detail = answer.json()["detail"]
    assert (detail["code"], detail["message"]) == ("link_taken", document_lifecycle_api.LINK_TAKEN_MESSAGE)
    _unchanged(world, campaign, before)


def test_link_to_a_seat_that_already_has_a_sheet_is_link_taken(client: TestClient, world: World) -> None:
    """T3(g): the seat already holds another sheet. Kills: the partial-unique
    path mapped to a 500 or a silent overwrite."""
    campaign = world.campaign()
    seat = world.seat(campaign)
    sheet_a = world.document(campaign)
    sheet_b = world.document(campaign)
    assert _link(client, campaign, sheet_a, seat).status_code == 204
    before = (world.revision(campaign), len(world.ledger(campaign)))
    answer = _link(client, campaign, sheet_b, seat)
    assert (answer.status_code, answer.json()["detail"]["code"]) == (409, "link_taken")
    _unchanged(world, campaign, before)
    assert world.record(campaign, sheet_b).linked_participant_id is None


def test_link_open_offered_and_accepted_seats_are_all_linkable(client: TestClient, world: World) -> None:
    """T3(h), B-2: every seat state but removed is linkable."""
    campaign = world.campaign()
    open_seat = world.seat(campaign, alias="Open")
    offered_seat = world.seat(campaign, alias="Offered")
    world.offer(campaign, offered_seat, user_id=PLAYER)
    accepted_seat = world.seat(campaign, alias="Accepted")
    world.offer(campaign, accepted_seat, user_id=PLAYER + 1)
    world.accept(campaign, accepted_seat, user_id=PLAYER + 1)
    for seat in (open_seat, offered_seat, accepted_seat):
        sheet = world.document(campaign)
        answer = _link(client, campaign, sheet, seat)
        assert answer.status_code == 204, seat
        assert world.record(campaign, sheet).linked_participant_id == seat


# ── T4: the one 404 ──────────────────────────────────────────────────────────


def test_link_the_one_404_across_tenants_and_malformed_ids(client: TestClient, world: World) -> None:
    """T4. Kills: the lock taken before ownership; the participant shape check
    dropped."""
    campaign = world.campaign(owner=GM_A)
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    _as(GM_B)
    cross = _link(client, campaign, sheet, seat)
    assert (cross.status_code, cross.json()) == (404, NOT_FOUND)
    assert all(not unit.campaign_locks for unit in world.db.units), "a stranger never reaches the lock"
    _as(GM_A)
    opened = len(world.db.units)
    for bad_campaign, bad_doc, bad_prt in (
        ("cmp_short", sheet, seat), (campaign, "not-a-document", seat), (campaign, sheet, "not-a-participant"),
    ):
        answer = _link(client, bad_campaign, bad_doc, bad_prt)
        assert (answer.status_code, answer.json()) == (404, NOT_FOUND), (bad_campaign, bad_doc, bad_prt)
    assert len(world.db.units) == opened, "a malformed id never opens a transaction"


# ── T5 to T9: unlink ─────────────────────────────────────────────────────────


def test_unlink_lands_in_two_steps_and_audits(client: TestClient, world: World) -> None:
    """T5. Kills: step one dropped; step two's re-narrow dropped; the reason
    used elsewhere; advance dropped; audit naming None."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    assert _link(client, campaign, sheet, seat).status_code == 204
    session = world.session(campaign)
    before_epoch = world.epoch(session.id)
    before_rev = world.revision(campaign)
    assert before_rev is not None
    world.spy()
    answer = _unlink(client, campaign, sheet)
    assert (answer.status_code, answer.content) == (204, b"")
    assert world.record(campaign, sheet).linked_participant_id is None
    narrows = [c for c in world.calls if c[1] == "sessions.narrow"]
    assert len(narrows) == 2, "step one and step two each narrow the live session"
    assert narrows[0][3] == (), "step one never holds the campaign lock"
    assert narrows[1][3] == ((campaign, "exclusive"),), "step two narrows under the lock"
    assert world.epoch(session.id) == before_epoch + 2
    assert world.revision(campaign) == before_rev + 1
    rows = world.ledger(campaign)
    assert len(rows) == 2, "the link row, then the unlink row"
    unlink_row = rows[-1]
    assert unlink_row.action == AuditAction.PARTICIPANT_UNLINKED
    assert unlink_row.object_kind == ObjectKind.PARTICIPANT
    assert unlink_row.object_ref == seat, "names the seat it WAS linked to"
    assert dict(unlink_row.detail) == {"participant_id": seat, "document_id": sheet}


def test_unlink_of_an_unlinked_sheet_is_a_noop_for_any_type(client: TestClient, world: World) -> None:
    """T6. Kills: unconditional narrowing; audit on None; a type check in
    unlink."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    npc = world.document(campaign, "npc")
    session = world.session(campaign)
    for doc in (sheet, npc):
        before_epoch = world.epoch(session.id)
        before = (world.revision(campaign), len(world.ledger(campaign)))
        answer = _unlink(client, campaign, doc)
        assert (answer.status_code, answer.content) == (204, b""), doc
        _unchanged(world, campaign, before)
        assert world.epoch(session.id) == before_epoch, "nothing narrowed"


def test_unlink_survives_the_seats_removal(client: TestClient, world: World) -> None:
    """T7, AUD-16: the link outlives Remove; unlink still names the removed
    seat. Kills: unlink implemented through sheet_for_participant, or a
    refusal for a removed seat."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    assert _link(client, campaign, sheet, seat).status_code == 204
    world.remove_seat(campaign, seat)
    assert world.record(campaign, sheet).linked_participant_id == seat, "the link survives removal"
    answer = _unlink(client, campaign, sheet)
    assert answer.status_code == 204
    assert world.record(campaign, sheet).linked_participant_id is None
    row = world.ledger(campaign)[-1]
    assert (row.action, row.object_ref) == (AuditAction.PARTICIPANT_UNLINKED, seat)


def test_unlink_404s_across_tenants_before_any_narrowing(client: TestClient, world: World) -> None:
    """T8. Kills: step one narrowing before the document is shown to exist."""
    campaign = world.campaign(owner=GM_A)
    other = world.campaign()
    sheet = world.document(campaign)
    session = world.session(campaign)
    before_epoch = world.epoch(session.id)
    foreign_sheet = world.document(other)
    assert (_unlink(client, campaign, MISSING_DOC).status_code,
            _unlink(client, campaign, MISSING_DOC).json()) == (404, NOT_FOUND)
    assert (_unlink(client, campaign, foreign_sheet).status_code,
            _unlink(client, campaign, foreign_sheet).json()) == (404, NOT_FOUND)
    _as(GM_B)
    assert _unlink(client, campaign, sheet).json() == NOT_FOUND
    _as(GM_A)
    assert world.epoch(session.id) == before_epoch


def test_unlink_busy_in_step_two_is_not_applied_yet(client: TestClient, world: World) -> None:
    """T9. Kills: busy_message omitted from step two's guard."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    assert _link(client, campaign, sheet, seat).status_code == 204

    def busy(_real: Callable[..., Any], *_args: Any, **_kwargs: Any) -> Any:
        raise psycopg.errors.LockNotAvailable()

    world.spy(documents={"unlink_character_sheet": busy})
    answer = _unlink(client, campaign, sheet)
    detail = answer.json()["detail"]
    assert (answer.status_code, detail["code"], detail["message"], detail["retryable"]) == (
        503, "backend_unavailable", document_lifecycle_api.NOT_APPLIED_MESSAGE, True)
    assert world.record(campaign, sheet).linked_participant_id == seat, "the link is untouched"


def test_link_busy_is_the_generic_retryable_503(client: TestClient, world: World) -> None:
    """T10. Kills: an unmapped driver error surfacing as a 500."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)

    def busy(_real: Callable[..., Any], *_args: Any, **_kwargs: Any) -> Any:
        raise psycopg.errors.LockNotAvailable()

    world.spy(documents={"link_character_sheet": busy})
    before = (world.revision(campaign), len(world.ledger(campaign)))
    answer = _link(client, campaign, sheet, seat)
    detail = answer.json()["detail"]
    assert (answer.status_code, detail["code"], detail["retryable"]) == (503, "backend_unavailable", True)
    assert detail["message"] == documents_api.UNAVAILABLE_MESSAGE
    _unchanged(world, campaign, before)


# ── T11: the read ────────────────────────────────────────────────────────────


def test_read_link(client: TestClient, world: World) -> None:
    """T11. Kills: seat_active computed as `participant is not None`; reading
    through sheet_for_participant alone (loses the removed-seat link); a lock
    or an audit row added to the read."""
    campaign = world.campaign(owner=GM_A)
    other = world.campaign()
    sheet = world.document(campaign)
    before = (world.revision(campaign), len(world.ledger(campaign)))

    unlinked = _read(client, campaign, sheet)
    assert unlinked.status_code == 200
    body = unlinked.json()
    assert body == {"schema_version": 1, "document_id": sheet, "participant_id": None, "seat_active": False}
    CharacterSheetLink.model_validate(body)

    seat = world.seat(campaign)
    assert _link(client, campaign, sheet, seat).status_code == 204
    live = _read(client, campaign, sheet)
    assert live.json() == {"schema_version": 1, "document_id": sheet, "participant_id": seat, "seat_active": True}
    CharacterSheetLink.model_validate(live.json())

    world.remove_seat(campaign, seat)
    removed = _read(client, campaign, sheet)
    assert removed.json() == {"schema_version": 1, "document_id": sheet, "participant_id": seat,
                              "seat_active": False}
    CharacterSheetLink.model_validate(removed.json())

    foreign_sheet = world.document(other)
    opened = len(world.db.units)
    settled = (world.revision(campaign), len(world.ledger(campaign)))
    assert (_read(client, campaign, MISSING_DOC).status_code, _read(client, campaign, foreign_sheet).status_code) \
        == (404, 404)
    _as(GM_B)
    assert _read(client, campaign, sheet).json() == NOT_FOUND
    _as(GM_A)
    assert _read(client, campaign, sheet).status_code == 200
    assert all(not unit.campaign_locks for unit in world.db.units[opened:]), "the read never locks"
    _unchanged(world, campaign, settled)
    assert before != settled, "the link and the removal really did change the campaign"


# ── T12: no private text ─────────────────────────────────────────────────────


def test_no_private_text_leaks(client: TestClient, world: World, caplog: pytest.LogCaptureFixture) -> None:
    """T12. Kills: a refusal interpolating an alias or an id; a log line
    carrying an exception's message."""
    campaign = world.campaign()
    sheet_a = world.document(campaign)
    sheet_b = world.document(campaign)
    seat = world.seat(campaign, alias=CANARY)
    with caplog.at_level(logging.DEBUG):
        landed = _link(client, campaign, sheet_a, seat)
        taken = _link(client, campaign, sheet_b, seat)
        unlinked = _unlink(client, campaign, sheet_a)
    for answer in (landed, taken, unlinked):
        assert CANARY not in answer.text
    for record in caplog.records:
        assert CANARY not in record.getMessage()
    for row in world.ledger(campaign):
        assert CANARY not in str(dict(row.detail))
        assert row.object_ref is None or CANARY not in row.object_ref


# ── T13: origin ──────────────────────────────────────────────────────────────


def test_link_and_unlink_are_origin_checked(client: TestClient, world: World) -> None:
    """T13. Kills: a route declared on a router that skips the origin check."""
    campaign = world.campaign()
    sheet = world.document(campaign)
    seat = world.seat(campaign)
    opened = len(world.db.units)
    for answer in (
        client.post(_link_url(campaign, sheet, seat), headers={"origin": "https://evil.example"}),
        client.post(_unlink_url(campaign, sheet), headers={"origin": "https://evil.example"}),
    ):
        assert (answer.status_code, answer.json()) == (403, {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)})
    assert len(world.db.units) == opened, "an origin refusal opens no transaction"

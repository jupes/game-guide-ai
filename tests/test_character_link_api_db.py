"""The character-sheet link compositions (agent-forge-harness-q156) in both
worlds, and the waits only PostgreSQL can answer.

The shared test runs one scripted transcript through the `world` fixture,
whose `postgres` parameter carries `needs_db`, and compares it with one
literal (the AC's "twin == PostgreSQL"), covering B-4's decision order,
idempotent repeats, a relink, and a seat's removal surviving on the link
(AUD-16). The two-connection tests below prove who waits on the campaign lock
through `pg_stat_activity`, never through timing alone, mirroring
`tests/test_documents_api_db.py` (archive/delete) and
`tests/test_groups_api_db.py` (the harness this file borrows).

Requires DATABASE_URL for the PostgreSQL half (CI sets it for this file,
`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`):

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_character_link_api_db.py -q
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError

from service import migrations as mig
from service.audit_log import InMemoryAuditLog, PostgresAuditLog
from service.campaign_store import InMemoryCampaignStore, PostgresCampaignStore
from service.db import CampaignLockSettings, Database, InMemoryDatabase, PoolSettings
from service.document_lifecycle_api import (
    NOT_APPLIED_MESSAGE,
    LifecycleStores,
    link_sheet,
    read_link,
    unlink_sheet,
    unlink_step_one,
    unlink_step_two,
)
from service.document_store import InMemoryDocumentStore, PostgresDocumentStore
from service.participant_store import InMemoryParticipantStore, PostgresParticipantStore
from service.table_session_store import InMemoryTableSessionStore, PostgresTableSessionStore, no_slots
from service.workbench_contracts import Author, CharacterSheetLink, DocumentTypeId

NOW = datetime(2030, 1, 1, tzinfo=UTC)
PATIENT = CampaignLockSettings(lock_timeout_s=4, transaction_timeout_s=30)
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
PATIENCE = 15
MISSING_DOC = "doc_" + "z" * 22
MISSING_PARTICIPANT = "prt_" + "z" * 22


def _database(dsn: str, settings: CampaignLockSettings = QUICK) -> Database:
    return Database(dsn, PoolSettings(sync_max=6, async_max=0, acquire_timeout_s=5), settings)


@dataclass
class World:
    kind: str
    # justification: the twin world holds an InMemoryDatabase and the postgres world a
    # Database; the fixtures only share their duck-typed transaction surface.
    db: Any
    stores: LifecycleStores
    # justification: the in-memory and PostgreSQL participant stores share no
    # declared base type; both worlds call the same methods on it.
    participants: Any
    owner: int
    stranger: int
    player: int
    dsn: str | None = None


def _pg_world(dsn: str, settings: CampaignLockSettings = QUICK) -> World:
    with connect(dsn) as conn:
        owner, stranger, player = (
            int(conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
            ).fetchone()[0])
            for email in ("gm.a@example.com", "gm.b@example.com", "wren@example.com")
        )
    stores = LifecycleStores(
        PostgresCampaignStore(), PostgresDocumentStore(), PostgresTableSessionStore(slot_clear=no_slots),
        PostgresAuditLog(),
    )
    return World("postgres", _database(dsn, settings), stores, PostgresParticipantStore(), owner, stranger, player,
                dsn)


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("q156") as target:
        mig.migrate(target)
        yield target


@pytest.fixture(params=["twin", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> World:
    if request.param == "postgres":
        return _pg_world(request.getfixturevalue("dsn"))
    db = InMemoryDatabase()
    documents = InMemoryDocumentStore(db)
    stores = LifecycleStores(
        InMemoryCampaignStore(db), documents, InMemoryTableSessionStore(db, slot_clear=no_slots), InMemoryAuditLog(),
    )
    return World("twin", db, stores, InMemoryParticipantStore(db), owner=1, stranger=2, player=3)


# ── Seeding and reading ──────────────────────────────────────────────────────


def _campaign(world: World, owner: int) -> str:
    with world.db.transaction() as unit:
        return world.stores.campaigns.create(unit, owner_id=owner, name="Nocturne", now=NOW).id


def _document(world: World, campaign: str, doc_type: str) -> str:
    with world.db.transaction() as unit:
        return world.stores.documents.create(
            unit, campaign, doc_type=DocumentTypeId(doc_type), type_version=1,
            data={"name": "Wren"} if doc_type == "character-sheet" else {"name": "Mira"}, author=Author.GM, now=NOW,
        ).id


def _seat(world: World, campaign: str, alias: str) -> str:
    with world.db.transaction() as unit:
        return world.participants.add(unit, campaign, alias=alias, now=NOW).id


def _offer(world: World, campaign: str, seat: str, *, user_id: int) -> None:
    with world.db.transaction() as unit:
        world.participants.offer(unit, campaign, seat, user_id=user_id)


def _accept(world: World, campaign: str, seat: str, *, user_id: int) -> None:
    with world.db.transaction() as unit:
        world.participants.accept(unit, campaign, seat, user_id=user_id, now=NOW)


def _remove(world: World, campaign: str, seat: str) -> None:
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, seat, now=NOW)


def _revision(world: World, campaign: str) -> int:
    with world.db.transaction() as unit:
        found = world.stores.campaigns.authz_revision(unit, campaign)
    assert found is not None
    return found


def _linked(world: World, campaign: str, document: str) -> str | None:
    with world.db.transaction() as unit:
        found = world.stores.documents.get(unit, campaign, document)
    assert found is not None
    return found.linked_participant_id


def _live_session(world: World, campaign: str) -> None:
    with world.db.transaction() as unit:
        world.stores.sessions.start(
            unit, campaign, owner_id=world.owner, expires_at=datetime.now(UTC) + timedelta(hours=11),
            command_id="cmd_" + "a" * 16,
        )


def _epoch(world: World, campaign: str) -> int:
    with world.db.transaction() as unit:
        live = world.stores.sessions.live_session_for_campaign(unit, campaign)
    assert live is not None
    return int(live.reveal_epoch)


# ── D1: one transcript, both worlds ─────────────────────────────────────────

#: What the script below must produce in BOTH worlds: each step's answer or
#: refusal, the ledger, and the revision it moved. Ids are replaced by labels
#: in the order they were made.
EXPECTED_TRANSCRIPT: list[Any] = [
    ("missing document", (404, "not_found")),
    ("foreign document", (404, "not_found")),
    ("not a character sheet", (422, ("path", "document_id"))),
    ("missing seat", (404, "not_found")),
    ("foreign seat", (404, "not_found")),
    ("removed seat", (404, "not_found")),
    ("link to an open seat", "ok"),
    ("repeat the same link", "ok"),
    ("another sheet to the same seat", (409, "link_taken")),
    ("this sheet to another seat", (409, "link_taken")),
    ("read while linked", ("seat_b", True)),
    ("unlink", "ok"),
    ("repeat the unlink", "ok"),
    ("read after unlink", (None, False)),
    ("link to an offered seat", "ok"),
    ("unlink to free it", "ok"),
    ("link to an accepted seat", "ok"),
    ("read before removal", ("seat_accepted", True)),
    ("seat removed, link survives (AUD-16)", ("seat_accepted", False)),
    ("unlink after removal", "ok"),
    ("ledger", [
        ("participant.linked", "seat_b", {"participant_id": "seat_b", "document_id": "sheet"}, 1),
        ("participant.unlinked", "seat_b", {"participant_id": "seat_b", "document_id": "sheet"}, 2),
        ("participant.linked", "seat_offered", {"participant_id": "seat_offered", "document_id": "sheet"}, 3),
        ("participant.unlinked", "seat_offered", {"participant_id": "seat_offered", "document_id": "sheet"}, 4),
        ("participant.linked", "seat_accepted", {"participant_id": "seat_accepted", "document_id": "sheet"}, 5),
        ("participant.unlinked", "seat_accepted", {"participant_id": "seat_accepted", "document_id": "sheet"}, 6),
    ]),
    ("revision moved", 6),
]


def _script(world: World) -> list[Any]:
    campaign = _campaign(world, world.owner)
    theirs = _campaign(world, world.stranger)
    sheet = _document(world, campaign, "character-sheet")
    sheet2 = _document(world, campaign, "character-sheet")
    npc = _document(world, campaign, "npc")
    foreign_sheet = _document(world, theirs, "character-sheet")
    seat_theirs = _seat(world, theirs, "Foreigner")
    seat_a = _seat(world, campaign, "A")
    seat_b = _seat(world, campaign, "B")
    seat_removed = _seat(world, campaign, "Removed")
    _remove(world, campaign, seat_removed)
    seat_offered = _seat(world, campaign, "Offered")
    _offer(world, campaign, seat_offered, user_id=world.player)
    seat_accepted = _seat(world, campaign, "Accepted")
    _offer(world, campaign, seat_accepted, user_id=world.stranger)
    _accept(world, campaign, seat_accepted, user_id=world.stranger)
    labels = {
        sheet: "sheet", sheet2: "sheet2", npc: "npc", foreign_sheet: "foreign_sheet", seat_theirs: "seat_theirs",
        seat_a: "seat_a", seat_b: "seat_b", seat_removed: "seat_removed", seat_offered: "seat_offered",
        seat_accepted: "seat_accepted",
    }
    start = _revision(world, campaign)
    transcript: list[Any] = []

    def step(name: str, work: Callable[[], Any]) -> None:
        try:
            answer = work()
        except HTTPException as refused:
            transcript.append((name, (refused.status_code, refused.detail["code"])))
            return
        except RequestValidationError as refused:
            [error] = refused.errors()
            transcript.append((name, (422, tuple(error["loc"]))))
            return
        if isinstance(answer, CharacterSheetLink):
            transcript.append((name, (labels.get(answer.participant_id, answer.participant_id),
                                      answer.seat_active)))
        else:
            transcript.append((name, "ok"))

    def _link(document: str, participant: str) -> None:
        link_sheet(world.db, world.stores, campaign_id=campaign, document_id=document, participant_id=participant,
                  owner_id=world.owner, now=NOW)

    def _unlink(document: str) -> None:
        unlink_sheet(world.db, world.stores, campaign_id=campaign, document_id=document, owner_id=world.owner,
                     now=NOW)

    def _read(document: str) -> CharacterSheetLink:
        return read_link(world.db, world.stores, campaign_id=campaign, document_id=document, owner_id=world.owner)

    step("missing document", lambda: _link(MISSING_DOC, seat_a))
    step("foreign document", lambda: _link(foreign_sheet, seat_a))
    step("not a character sheet", lambda: _link(npc, seat_a))
    step("missing seat", lambda: _link(sheet, MISSING_PARTICIPANT))
    step("foreign seat", lambda: _link(sheet, seat_theirs))
    step("removed seat", lambda: _link(sheet, seat_removed))
    step("link to an open seat", lambda: _link(sheet, seat_b))
    step("repeat the same link", lambda: _link(sheet, seat_b))
    step("another sheet to the same seat", lambda: _link(sheet2, seat_b))
    step("this sheet to another seat", lambda: _link(sheet, seat_a))
    step("read while linked", lambda: _read(sheet))
    step("unlink", lambda: _unlink(sheet))
    step("repeat the unlink", lambda: _unlink(sheet))
    step("read after unlink", lambda: _read(sheet))
    step("link to an offered seat", lambda: _link(sheet, seat_offered))
    step("unlink to free it", lambda: _unlink(sheet))
    step("link to an accepted seat", lambda: _link(sheet, seat_accepted))
    step("read before removal", lambda: _read(sheet))
    _remove(world, campaign, seat_accepted)
    step("seat removed, link survives (AUD-16)", lambda: _read(sheet))
    step("unlink after removal", lambda: _unlink(sheet))

    with world.db.transaction() as unit:
        events = world.stores.audit.for_campaign(unit, campaign)
    transcript.append(("ledger", [
        (e.action, labels.get(e.object_ref or "", e.object_ref), {k: labels.get(str(v), str(v))
                                                                   for k, v in e.detail.items()},
         None if e.authz_revision is None else e.authz_revision - start)
        for e in events
    ]))
    transcript.append(("revision moved", _revision(world, campaign) - start))
    return transcript


def test_the_compositions_answer_the_same_transcript_in_both_worlds(world: World) -> None:
    """D1 (the AC's twin == PostgreSQL): B-4's whole order, link, read, a
    repeat, unlink, a repeat, a relink and a seat's removal, compared to one
    literal. Kills any divergence — notably "another sheet to the same seat",
    where only PostgreSQL's partial unique index and savepoint decide it."""
    assert _script(world) == EXPECTED_TRANSCRIPT


# ── PostgreSQL only: who waits for whom ─────────────────────────────────────


def _waiting(dsn: str, waiters: int = 1, patience: float = PATIENCE) -> bool:
    deadline = time.monotonic() + patience
    with connect(dsn) as conn:
        while time.monotonic() < deadline:
            blocked = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if blocked >= waiters:
                return True
            time.sleep(0.05)
    return False


def _in_thread(work: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = work()
        except BaseException as error:  # noqa: BLE001 - the test inspects it
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _hold(dsn: str, campaign: str, mode: str) -> Any:
    holder = connect(dsn, autocommit=False)
    holder.execute(f"SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR {mode}", (campaign,))
    return holder


@needs_db
def test_link_waits_on_the_campaign_lock(dsn: str) -> None:
    """D2: a share-mode holder (a reveal) makes link wait — the server says
    so — and on release it lands; with the lock held past its timeout it
    answers the generic retryable 503 and links nothing. Kills: link without
    `lock_campaign`."""
    world = _pg_world(dsn, PATIENT)
    campaign = _campaign(world, world.owner)
    sheet = _document(world, campaign, "character-sheet")
    seat = _seat(world, campaign, "Wren")
    holder = _hold(dsn, campaign, "SHARE")
    thread, outcome = _in_thread(lambda: link_sheet(
        world.db, world.stores, campaign_id=campaign, document_id=sheet, participant_id=seat,
        owner_id=world.owner, now=NOW,
    ))
    assert _waiting(dsn), "nobody was blocked, so this proves nothing"
    holder.rollback()
    holder.close()
    thread.join(PATIENCE)
    assert outcome == {"value": None}
    assert _linked(world, campaign, sheet) == seat

    stuck = replace(world, db=_database(dsn, QUICK))
    other_sheet = _document(world, campaign, "character-sheet")
    holder = _hold(dsn, campaign, "SHARE")
    try:
        with pytest.raises(HTTPException) as refused:
            link_sheet(stuck.db, stuck.stores, campaign_id=campaign, document_id=other_sheet, participant_id=seat,
                      owner_id=world.owner, now=NOW)
    finally:
        holder.rollback()
        holder.close()
    assert refused.value.status_code == 503
    assert _linked(world, campaign, other_sheet) is None


@needs_db
def test_unlink_under_a_held_lock_has_stopped_the_display_and_is_not_applied_yet(dsn: str) -> None:
    """D3: step one commits — the sheet's live copy is stopped and the epoch
    moves — before step two even asks for the lock; step two, unable to have
    it, answers "not applied yet" and the link is untouched; the retry clears
    it. Kills: step one placed inside the locked transaction; the wrong busy
    message."""
    world = _pg_world(dsn, QUICK)
    campaign = _campaign(world, world.owner)
    sheet = _document(world, campaign, "character-sheet")
    seat = _seat(world, campaign, "Wren")
    link_sheet(world.db, world.stores, campaign_id=campaign, document_id=sheet, participant_id=seat,
              owner_id=world.owner, now=NOW)
    _live_session(world, campaign)
    epoch = _epoch(world, campaign)
    holder = _hold(dsn, campaign, "UPDATE")
    try:
        with pytest.raises(HTTPException) as refused:
            unlink_sheet(world.db, world.stores, campaign_id=campaign, document_id=sheet, owner_id=world.owner,
                        now=NOW)
    finally:
        holder.rollback()
        holder.close()
    assert (refused.value.status_code, refused.value.detail["message"]) == (503, NOT_APPLIED_MESSAGE)
    assert _epoch(world, campaign) == epoch + 1, "step one committed before step two ever asked for the lock"
    assert _linked(world, campaign, sheet) == seat, "the link is untouched"
    unlink_sheet(world.db, world.stores, campaign_id=campaign, document_id=sheet, owner_id=world.owner, now=NOW)
    assert _linked(world, campaign, sheet) is None


@needs_db
def test_two_links_racing_for_one_seat_leave_exactly_one(dsn: str) -> None:
    """D4: two sheets, one seat, released together. The campaign's exclusive
    lock serialises the two transactions; exactly one lands and one answers
    `409 link_taken`, and the seat ends with exactly one sheet. Kills:
    `SheetAlreadyLinked` from the partial-unique-index path mapped to a 500,
    or the index silently overwritten."""
    world = _pg_world(dsn, PATIENT)
    campaign = _campaign(world, world.owner)
    seat = _seat(world, campaign, "Wren")
    sheet_a = _document(world, campaign, "character-sheet")
    sheet_b = _document(world, campaign, "character-sheet")
    a = replace(world, db=_database(dsn, PATIENT))
    b = replace(world, db=_database(dsn, PATIENT))
    barrier = threading.Barrier(2, timeout=PATIENCE)

    def attempt(w: World, document: str) -> str | None:
        barrier.wait()
        try:
            link_sheet(w.db, w.stores, campaign_id=campaign, document_id=document, participant_id=seat,
                      owner_id=world.owner, now=NOW)
        except HTTPException as refused:
            return refused.detail["code"]
        return None

    thread_a, outcome_a = _in_thread(lambda: attempt(a, sheet_a))
    thread_b, outcome_b = _in_thread(lambda: attempt(b, sheet_b))
    thread_a.join(PATIENCE)
    thread_b.join(PATIENCE)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    codes = {outcome_a.get("value"), outcome_b.get("value")}
    assert codes == {None, "link_taken"}, "one lands, one is refused, never both or neither"
    linked = {_linked(world, campaign, sheet_a), _linked(world, campaign, sheet_b)}
    assert linked == {seat, None}


@needs_db
def test_step_two_narrows_a_session_started_after_step_one(dsn: str) -> None:
    """D5: a table that goes live between the two steps is narrowed in step
    two too (mirrors `tests/test_documents_api_db.py`'s equivalent). Kills:
    the re-narrow in step two dropped."""
    world = _pg_world(dsn)
    campaign = _campaign(world, world.owner)
    sheet = _document(world, campaign, "character-sheet")
    seat = _seat(world, campaign, "Wren")
    link_sheet(world.db, world.stores, campaign_id=campaign, document_id=sheet, participant_id=seat,
              owner_id=world.owner, now=NOW)
    unlink_step_one(world.db, world.stores, campaign_id=campaign, document_id=sheet, owner_id=world.owner, now=NOW)
    _live_session(world, campaign)
    session_epoch = _epoch(world, campaign)
    unlink_step_two(world.db, world.stores, campaign_id=campaign, document_id=sheet, owner_id=world.owner, now=NOW)
    assert _epoch(world, campaign) == session_epoch + 1, "the session that started after step one is narrowed too"


def _counts(dsn: str) -> tuple[int, int]:
    with connect(dsn) as conn:
        events = conn.execute(
            "SELECT count(*) FROM audit.events WHERE action LIKE 'participant.%'"
        ).fetchone()[0]
        linked = conn.execute(
            "SELECT count(*) FROM campaign.documents WHERE linked_participant_id IS NOT NULL"
        ).fetchone()[0]
        return int(events), int(linked)


@needs_db
def test_an_audit_failure_rolls_back_the_link_and_the_advance(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7: the lock, the store change, the revision advance and the audit row
    share one transaction (SEC-38 audit completeness) — an audit failure
    rolls back the whole unit, for both link and unlink step two. Kills: the
    audit row written in a transaction of its own, after the locked unit."""
    world = _pg_world(dsn)
    campaign = _campaign(world, world.owner)
    sheet = _document(world, campaign, "character-sheet")
    seat = _seat(world, campaign, "Wren")
    start = _revision(world, campaign)

    # justification: stands in for PostgresAuditLog.append's full signature, which this
    # stub never uses — it only ever raises.
    def _broken_append(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("audit sink is down")

    monkeypatch.setattr(PostgresAuditLog, "append", _broken_append)
    with pytest.raises(RuntimeError):
        link_sheet(world.db, world.stores, campaign_id=campaign, document_id=sheet, participant_id=seat,
                  owner_id=world.owner, now=NOW)
    assert _linked(world, campaign, sheet) is None
    assert _revision(world, campaign) == start
    assert _counts(dsn) == (0, 0)

    monkeypatch.undo()
    link_sheet(world.db, world.stores, campaign_id=campaign, document_id=sheet, participant_id=seat,
              owner_id=world.owner, now=NOW)
    assert _linked(world, campaign, sheet) == seat
    assert _counts(dsn) == (1, 1)
    assert _revision(world, campaign) == start + 1

    monkeypatch.setattr(PostgresAuditLog, "append", _broken_append)
    with pytest.raises(RuntimeError):
        unlink_sheet(world.db, world.stores, campaign_id=campaign, document_id=sheet, owner_id=world.owner, now=NOW)
    assert _linked(world, campaign, sheet) == seat, "the link is kept"
    assert _revision(world, campaign) == start + 1
    assert _counts(dsn) == (1, 1)


@needs_db
def test_the_ledger_row_in_postgresql_carries_ids_only(dsn: str) -> None:
    """D6: `object_kind='participant'`, `detail` keys exactly
    `{participant_id, document_id}`, and `detail` carries ids only (a
    private-looking alias never appears in it). Kills: detail widening; a
    fourth column added to the row."""
    world = _pg_world(dsn)
    campaign = _campaign(world, world.owner)
    sheet = _document(world, campaign, "character-sheet")
    seat = _seat(world, campaign, "Zx9Canary")
    link_sheet(world.db, world.stores, campaign_id=campaign, document_id=sheet, participant_id=seat,
              owner_id=world.owner, now=NOW)
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT object_kind, detail, reason_code FROM audit.events WHERE action = 'participant.linked'"
        ).fetchone()
    assert row is not None
    object_kind, detail, reason_code = row
    assert object_kind == "participant"
    assert set(detail.keys()) == {"participant_id", "document_id"}
    assert reason_code is None
    assert "Zx9Canary" not in str(detail)

"""Disclosures and slots: the shared suite, and what only PostgreSQL can prove
(agent-forge-harness-1kg.7.1).

The **shared suite** runs every test twice, against the in-memory twin and
against PostgreSQL, so a rule the two could disagree about is asserted of both;
after each one the invariant auditor (I-1 to I-5) must find nothing. The
`postgres` parameter carries `needs_db` and skips without a server.

The **PostgreSQL-only** tests prove the schema: the partial unique index, the
composite keys, the delete action that is deliberately absent, the mask CHECK,
and who waits for whom. Every "waits" claim is the server's own account
(`pg_stat_activity`), and every "does not wait" claim runs under a short
`lock_timeout` beside a positive control that does wait.

The **service suite** (PR-B) runs `service/reveals.py` in both worlds, with
every session store built with the production fill, and proves RQ-7 for every
narrowing already shipped; the **races** after it are PostgreSQL's alone.

Requires DATABASE_URL (CI sets it for this file, `.github/workflows/ci.yml`,
pinned by `service/tests/test_ci_workflow.py`). From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_reveal_db.py -q
"""

from __future__ import annotations

import logging
import random
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import document_lifecycle_api
from service import migrations as mig
from service.audit_log import AuditAction, InMemoryAuditLog, PostgresAuditLog
from service.campaign_store import InMemoryCampaignStore, MissingParent, PostgresCampaignStore, shared_rows
from service.campaign_summary_store import InMemoryCampaignSummaryStore, PostgresCampaignSummaryStore
from service.campaigns_api import (
    CampaignStores,
    archive,
    archive_step_one,
    archive_step_two,
    confirm_seat,
    remove_seat,
)
from service.db import CampaignLockOrder, CampaignLockSettings, Database, InMemoryDatabase, PoolSettings
from service.document_store import InMemoryDocumentStore, PostgresDocumentStore
from service.eligibility import EligibilityMutations, EligibilityStores, narrow_step_one, remove_member_step_two
from service.eligibility_store import InMemoryEligibilityStore, PostgresEligibilityStore
from service.history import InMemoryMessageStore
from service.jobs import InMemoryJobQueue, PostgresJobQueue
from service.participant_store import InMemoryParticipantStore, PostgresParticipantStore
from service.policy import EligReason, PolicyFacts, eligible_for_audience
from service.reconciliation import enqueue_reconciliation, reconcile
from service.reveal_scope import (
    DocumentCopies,
    EndReason,
    EveryoneSeated,
    EverySlot,
    NoSlot,
    ParticipantsAudience,
    ParticipantSlots,
    ParticipantTargets,
    TableAudience,
    TableTarget,
)
from service.reveal_store import (
    EMPTY_SLOT,
    AudienceRefused,
    CopyChange,
    Disclosure,
    DocumentNotDisplayable,
    InMemoryRevealStore,
    MaskRefused,
    PostgresRevealStore,
    RevealBusy,
    RevealConflict,
    RevealNotFound,
    RevealPicture,
    SlotRow,
    StaleSlots,
    VersionNotDisplayable,
    Written,
    audit_reveal_invariants,
)
from service.reveals import (
    DEADLOCK_ATTEMPTS,
    DisplayCommand,
    Displayed,
    Reveals,
    StopAll,
    StopDocument,
    make_reconcile_slots,
    slot_clear_for,
)
from service.seat_offer_store import InMemorySeatOfferStore, PostgresSeatOfferStore
from service.table_session_store import (
    InMemoryTableSessionStore,
    PostgresTableSessionStore,
    TableSession,
    no_slots,
)
from service.table_sessions import TableSessions
from service.workbench_contracts import Author, DocumentTypeId

QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
#: How long the gate lets a caller wait for a connection (RC-9 compares it with the lock bound).
GATE_ACQUIRE_S = 5
#: How long a test waits for another thread before calling it a hang.
PATIENCE = 15
AN_NPC = {"name": "Vashti", "qualifier": "Harbourmistress", "tags": ["harbour"], "voice": "low"}


def _command() -> str:
    return secrets.token_urlsafe(16)


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("reveal") as target:
        mig.migrate(target)
        yield target


def _database(target: str) -> Database:
    return Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=GATE_ACQUIRE_S), QUICK)


@dataclass
class World:
    """Every store a display touches, over one database, with two GMs and eight
    accounts that own nothing."""

    kind: str
    db: Any
    campaigns: Any
    participants: Any
    sessions: Any
    documents: Any
    reveals: Any
    owner: int
    other_owner: int
    players: tuple[int, ...]
    dsn: str | None = None
    #: False only for the test that corrupts rows on purpose.
    audited: bool = True

    def audit(self) -> list[str]:
        with self.db.transaction() as unit:
            return audit_reveal_invariants(unit, self.reveals if self.kind == "fake" else None)


def _pg_world(target: str, *, fill: bool = False) -> World:
    """The PostgreSQL world; `fill` builds the session store with the reveal
    fill, as production does (the service suite), rather than `no_slots`."""
    with connect(target) as conn:
        ids = [
            int(
                conn.execute(
                    "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
                    (f"account{n}@example.com",),
                ).fetchone()[0]
            )
            for n in range(10)
        ]
    rows = PostgresRevealStore()
    return World(
        "postgres",
        _database(target),
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresTableSessionStore(slot_clear=slot_clear_for(rows) if fill else no_slots),
        PostgresDocumentStore(),
        rows,
        owner=ids[0],
        other_owner=ids[1],
        players=tuple(ids[2:]),
        dsn=target,
    )


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db = InMemoryDatabase()
        made = World(
            "fake",
            db,
            InMemoryCampaignStore(db),
            InMemoryParticipantStore(db),
            InMemoryTableSessionStore(db, slot_clear=no_slots),
            InMemoryDocumentStore(db),
            InMemoryRevealStore(db),
            owner=1,
            other_owner=2,
            players=tuple(range(3, 11)),
        )
    else:
        made = _pg_world(request.getfixturevalue("dsn"))
    yield made
    if made.audited:
        assert made.audit() == [], "the invariant auditor found a broken invariant after the test"


@pytest.fixture
def pgw(dsn: str) -> World:
    return _pg_world(dsn)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _campaign(w: World, *, owner: int | None = None) -> str:
    with w.db.transaction() as unit:
        return w.campaigns.create(unit, owner_id=w.owner if owner is None else owner, name="Nocturne").id


def _seat(w: World, campaign: str, state: str, player: int) -> str:
    """A seat in `state`: open, offered, accepted, confirmed or removed (a
    confirmed seat, then removed)."""
    with w.db.transaction() as unit:
        seat = w.participants.add(unit, campaign, alias=f"Seat {player}").id
    steps: list[tuple[str, Callable[[Any], object]]] = [
        ("offered", lambda unit: w.participants.offer(unit, campaign, seat, user_id=player)),
        ("accepted", lambda unit: w.participants.accept(unit, campaign, seat, user_id=player)),
        ("confirmed", lambda unit: w.participants.confirm(unit, campaign, seat)),
        ("removed", lambda unit: w.participants.remove(unit, campaign, seat)),
    ]
    for reached, step in steps:
        if state == "open":
            break
        with w.db.transaction() as unit:
            step(unit)
        if reached == state:
            break
    return seat


def _session(
    w: World, campaign: str, *, owner: int | None = None, hours: int = 12, started_ago_h: int = 0
) -> TableSession:
    started = datetime.now(UTC) - timedelta(hours=started_ago_h)
    with w.db.transaction() as unit:
        return w.sessions.start(
            unit,
            campaign,
            owner_id=w.owner if owner is None else owner,
            expires_at=started + timedelta(hours=hours),
            command_id=_command(),
            now=started,
        )


def _document(w: World, campaign: str) -> str:
    """A document with one sealed version, number 1."""
    with w.db.transaction() as unit:
        made = w.documents.create(
            unit, campaign, doc_type=DocumentTypeId.NPC, type_version=1, data=dict(AN_NPC), author=Author.GM
        )
    with w.db.transaction() as unit:
        w.documents.seal(unit, campaign, made.id)
    return str(made.id)


def _parts(*ids: str) -> ParticipantTargets:
    return ParticipantTargets(frozenset(ids))


def _write(
    w: World,
    campaign: str,
    session: str,
    document: str,
    targets: Any,
    *,
    mask: tuple[str, ...] = ("name",),
    version: int = 1,
    command: str | None = None,
    now: datetime | None = None,
) -> Written:
    with w.db.transaction() as unit:
        return w.reveals.write(
            unit,
            campaign_id=campaign,
            session_id=session,
            document_id=document,
            version=version,
            mask=mask,
            targets=targets,
            command_id=command or _command(),
            now=now or datetime.now(UTC),
        )


def _clear(w: World, session: str, scope: Any) -> tuple[CopyChange, ...]:
    with w.db.transaction() as unit:
        return w.reveals.clear(unit, session, scope, now=datetime.now(UTC))


def _slots(w: World, session: str) -> dict[str | None, SlotRow]:
    with w.db.transaction() as unit:
        return {s.participant_id: s for s in w.reveals._slots(unit, session)}


def _shown(w: World, session: str) -> dict[str | None, str | None]:
    return {pid: s.disclosure_id for pid, s in _slots(w, session).items()}


def _seqs(w: World, session: str) -> dict[str | None, int]:
    return {pid: s.seq for pid, s in _slots(w, session).items()}


def _by_command(w: World, session: str, command: str) -> Disclosure:
    with w.db.transaction() as unit:
        found = w.reveals.by_command(unit, session, command)
    assert found is not None
    return found


def _live(w: World, session: str) -> list[Disclosure]:
    with w.db.transaction() as unit:
        return list(w.reveals.live_disclosures(unit, session))


def _stage(w: World, seats: int = 3) -> tuple[str, TableSession, list[str]]:
    """A campaign, its live session, and `seats` open seats."""
    campaign = _campaign(w)
    people = [_seat(w, campaign, "open", w.players[n]) for n in range(seats)]
    return campaign, _session(w, campaign), people


# ── The shared suite: what a write does ──────────────────────────────────────


def test_a_write_fills_one_slot_per_recipient_with_one_disclosure(world: World) -> None:
    """T-A13. Copies of one Confirm share one disclosure, and the mask is stored
    sorted whatever order it came in."""
    campaign, session, (a, b, c) = _stage(world)
    document = _document(world, campaign)
    command = _command()
    written = _write(world, campaign, session.id, document, _parts(a, b, c), mask=("voice", "name"), command=command)

    assert written.kind == "displayed" and written.taken == () and written.reconciled == ()
    assert written.disclosure.mask == ("name", "voice")
    assert written.disclosure.audience_kind == "participant" and written.disclosure.version == 1
    assert _by_command(world, session.id, command) == written.disclosure
    assert _shown(world, session.id) == {a: written.disclosure.id, b: written.disclosure.id, c: written.disclosure.id}
    assert _seqs(world, session.id) == {a: 1, b: 1, c: 1}
    assert _live(world, session.id) == [written.disclosure]

    table = _write(world, campaign, session.id, _document(world, campaign), TableTarget())
    assert table.disclosure.audience_kind == "table"
    assert _shown(world, session.id)[None] == table.disclosure.id


@pytest.mark.parametrize(
    "targets",
    [
        ParticipantTargets(frozenset()),
        ParticipantTargets(frozenset({"Rook"})),
        ParticipantTargets(frozenset({"cmp_" + "a" * 22})),
        pytest.param("table", id="not-a-target"),
    ],
)
def test_a_write_is_the_table_or_participants_never_both(world: World, targets: Any) -> None:
    """T-A14. Mixed, empty or malformed targets are one refusal, and nothing is
    written."""
    campaign, session, _ = _stage(world, seats=0)
    document = _document(world, campaign)
    with pytest.raises(AudienceRefused):
        _write(world, campaign, session.id, document, targets)
    assert _slots(world, session.id) == {} and _live(world, session.id) == []


def test_a_new_audience_ends_the_previous_disclosure_entirely(world: World) -> None:
    """T-A15 (Move). Revealing the document to a new audience ends its previous
    disclosure — `moved`, never `replaced` — and clears every copy outside the
    new set, while a slot in both sets is re-pointed."""
    campaign, session, (a, b, c) = _stage(world)
    document = _document(world, campaign)
    first_command = _command()
    first = _write(world, campaign, session.id, document, _parts(a, b), command=first_command)
    second = _write(world, campaign, session.id, document, _parts(b, c))

    assert second.kind == "displayed"
    assert second.taken == (CopyChange(first.disclosure, tuple(sorted((a, b))), True, EndReason.MOVED),)
    ended = _by_command(world, session.id, first_command)
    assert ended.ended_reason == EndReason.MOVED and not ended.is_live
    assert _shown(world, session.id) == {a: None, b: second.disclosure.id, c: second.disclosure.id}

    third = _write(world, campaign, session.id, document, TableTarget())
    assert [c.reason for c in third.taken] == [EndReason.MOVED]
    assert _shown(world, session.id) == {a: None, b: None, c: None, None: third.disclosure.id}
    assert _live(world, session.id) == [third.disclosure]


@pytest.mark.parametrize("change", ["shrink", "grow"])
def test_a_smaller_or_larger_audience_is_a_move_not_an_update(world: World, change: str) -> None:
    """T-A15 (Move), the subset and superset cases: only the SAME set is an
    Update (ID-6). Shrinking (a, b) to (a) or growing (a) to (a, b) is a Move:
    `displayed`, the old disclosure ended `moved`, and every one of its old
    copies listed, so each is audited as stopped (SEC-38, ED-18)."""
    campaign, session, (a, b, _) = _stage(world)
    document = _document(world, campaign)
    before, after = ((a, b), (a,)) if change == "shrink" else ((a,), (a, b))
    first_command = _command()
    first = _write(world, campaign, session.id, document, _parts(*before), command=first_command)
    second = _write(world, campaign, session.id, document, _parts(*after))

    assert second.kind == "displayed" and second.reconciled == ()
    assert second.taken == (CopyChange(first.disclosure, tuple(sorted(before)), True, EndReason.MOVED),)
    assert _by_command(world, session.id, first_command).ended_reason == EndReason.MOVED
    assert _shown(world, session.id) == {pid: second.disclosure.id if pid in after else None for pid in (a, b)}
    assert _live(world, session.id) == [second.disclosure]


def test_the_same_audience_is_an_update_with_a_new_disclosure_and_the_same_slots(world: World) -> None:
    """T-A16 (Update, E-3). A new disclosure takes the same slots; the old one
    ends `updated` and is not a copy taken by a GM action."""
    campaign, session, (a, b, _) = _stage(world)
    document = _document(world, campaign)
    first_command = _command()
    first = _write(world, campaign, session.id, document, _parts(a, b), command=first_command)
    second = _write(world, campaign, session.id, document, _parts(b, a), mask=("name", "voice"))

    assert second.kind == "updated" and second.taken == () and second.reconciled == ()
    assert second.disclosure.id != first.disclosure.id
    assert first.disclosure.mask == ("name",) and second.disclosure.mask == ("name", "voice")
    assert _by_command(world, session.id, first_command).ended_reason == EndReason.UPDATED
    assert _shown(world, session.id) == {a: second.disclosure.id, b: second.disclosure.id}


def test_a_replaced_disclosure_ends_only_with_its_last_copy(world: World) -> None:
    """T-A17 (Replace). A slot holds one live projection: another document takes
    it, and the disclosure it held stays live while any copy remains."""
    campaign, session, (a, b, _) = _stage(world)
    one, two, three = (_document(world, campaign) for _ in range(3))
    one_command = _command()
    first = _write(world, campaign, session.id, one, _parts(a, b), command=one_command)

    second = _write(world, campaign, session.id, two, _parts(a))
    assert second.taken == (CopyChange(first.disclosure, (a,), False, EndReason.REPLACED),)
    assert {d.id for d in _live(world, session.id)} == {first.disclosure.id, second.disclosure.id}
    assert _shown(world, session.id) == {a: second.disclosure.id, b: first.disclosure.id}

    third = _write(world, campaign, session.id, three, _parts(b))
    assert third.taken == (CopyChange(first.disclosure, (b,), True, EndReason.REPLACED),)
    assert _by_command(world, session.id, one_command).ended_reason == EndReason.REPLACED


def test_a_slots_sequence_moves_exactly_when_its_content_changes(world: World) -> None:
    """T-A18 and the critic's point 4: a target slot is re-pointed (+1), never
    cleared and then pointed (+2) — on an Update, a Move that keeps a recipient
    and a Replace alike — and an untouched slot never moves."""
    campaign, session, (a, b, c) = _stage(world)
    one, two = _document(world, campaign), _document(world, campaign)
    _write(world, campaign, session.id, one, _parts(a, b))
    assert _seqs(world, session.id) == {a: 1, b: 1}
    _write(world, campaign, session.id, one, _parts(a, b), mask=("voice",))
    assert _seqs(world, session.id) == {a: 2, b: 2}, "an update re-points each slot once"
    _write(world, campaign, session.id, one, _parts(b, c))
    assert _seqs(world, session.id) == {a: 3, b: 3, c: 1}, "a move clears a once and re-points b once"
    _write(world, campaign, session.id, two, _parts(c))
    assert _seqs(world, session.id) == {a: 3, b: 3, c: 2}, "a replace re-points c once and leaves a and b"
    _clear(world, session.id, EverySlot(EndReason.NARROWED))
    assert _seqs(world, session.id) == {a: 3, b: 4, c: 3}, "a clear moves only the slots it empties"


def test_each_clear_scope_clears_exactly_what_it_names(world: World) -> None:
    """T-A19. A member's removal stops that member's copy and nothing else; a
    Stop of a document stops every copy of it; `NoSlot` clears nothing."""
    campaign, session, (a, b, c) = _stage(world)
    one, two, three = (_document(world, campaign) for _ in range(3))
    table = _write(world, campaign, session.id, one, TableTarget()).disclosure
    pair = _write(world, campaign, session.id, two, _parts(a, b)).disclosure
    solo = _write(world, campaign, session.id, three, _parts(c)).disclosure
    before = _seqs(world, session.id)

    assert _clear(world, session.id, NoSlot()) == ()
    assert _seqs(world, session.id) == before

    removed = _clear(world, session.id, ParticipantSlots(frozenset({a}), EndReason.PARTICIPANT_REMOVED))
    assert removed == (CopyChange(pair, (a,), False, EndReason.PARTICIPANT_REMOVED),)
    assert _shown(world, session.id) == {None: table.id, a: None, b: pair.id, c: solo.id}

    stopped = _clear(world, session.id, DocumentCopies(two, EndReason.GM_STOP))
    assert stopped == (CopyChange(pair, (b,), True, EndReason.GM_STOP),)
    assert _shown(world, session.id) == {None: table.id, a: None, b: None, c: solo.id}

    everything = _clear(world, session.id, EverySlot(EndReason.NARROWED))
    assert everything == tuple(
        sorted(
            (CopyChange(table, (), True, EndReason.NARROWED), CopyChange(solo, (c,), True, EndReason.NARROWED)),
            key=lambda change: change.disclosure.id,
        )
    )
    assert _live(world, session.id) == []
    assert set(_shown(world, session.id).values()) == {None}


def test_a_second_identical_clear_changes_nothing(world: World) -> None:
    """T-A20. Every clear is idempotent: it moves no sequence on an empty slot."""
    campaign, session, (a, b, _) = _stage(world)
    _write(world, campaign, session.id, _document(world, campaign), _parts(a, b))
    for scope in (
        ParticipantSlots(frozenset({a}), EndReason.PARTICIPANT_REMOVED),
        EverySlot(EndReason.GM_END),
    ):
        assert _clear(world, session.id, scope) != ()
        after = _seqs(world, session.id)
        assert _clear(world, session.id, scope) == ()
        assert _seqs(world, session.id) == after


def test_a_scope_carries_only_a_reason_its_narrowing_may_use(world: World) -> None:
    """A Remove cannot record `gm_stop`, nor a Stop `reconciled`, nor a Confirm's
    own reasons be used by a narrowing."""
    campaign, session, _ = _stage(world, seats=0)
    for scope in (
        ParticipantSlots(frozenset(), EndReason.GM_STOP),
        DocumentCopies("doc_" + "a" * 22, EndReason.RECONCILED),
        EverySlot(EndReason.REPLACED),
        EverySlot(EndReason.UPDATED),
    ):
        with pytest.raises(ValueError, match="reason its narrowing may use"):
            _clear(world, session.id, scope)


def test_every_writer_holds_the_session_row(world: World) -> None:
    """T-A21 (both worlds): a write and a clear each note a row lock, so the
    campaign lock can no longer be taken after them in that transaction. The
    PostgreSQL half — that the lock is really the session row's — is
    `test_a_write_and_a_clear_wait_for_the_session_row`."""
    campaign, session, (a, _, _) = _stage(world)
    document = _document(world, campaign)
    with world.db.transaction() as unit:
        world.reveals.write(
            unit,
            campaign_id=campaign,
            session_id=session.id,
            document_id=document,
            version=1,
            mask=("name",),
            targets=_parts(a),
            command_id=_command(),
            now=datetime.now(UTC),
        )
        with pytest.raises(CampaignLockOrder):
            unit.lock_campaign(campaign, shared=True)
    with world.db.transaction() as unit:
        world.reveals.clear(unit, session.id, EverySlot(EndReason.NARROWED), now=datetime.now(UTC))
        with pytest.raises(CampaignLockOrder):
            unit.lock_campaign(campaign, shared=True)


def test_a_disclosure_cannot_name_another_campaigns_document_or_session_or_a_missing_version(world: World) -> None:
    """T-A6 (both worlds): the store's own answer is `MissingParent`, as a
    foreign key's would be. The schema's half is
    `test_the_composite_keys_refuse_a_cross_campaign_row`."""
    campaign, session, (a,) = _stage(world, seats=1)
    other = _campaign(world, owner=world.other_owner)
    foreign_seat = _seat(world, other, "open", world.players[5])
    foreign_document = _document(world, other)
    other_session = _session(world, other, owner=world.other_owner)
    document = _document(world, campaign)
    for kwargs in (
        {"document": foreign_document},
        {"version": 2},
        {"session": other_session.id},
        {"targets": _parts(a, foreign_seat)},
    ):
        with pytest.raises(MissingParent):
            _write(
                world,
                campaign,
                kwargs.get("session", session.id),
                kwargs.get("document", document),
                kwargs.get("targets", _parts(a)),
                version=kwargs.get("version", 1),
            )
    assert _live(world, session.id) == [] and _live(world, other_session.id) == []


def test_a_command_id_is_used_once_per_session(world: World) -> None:
    """The replay key `(session_id, command_id)`: the store refuses a second
    write with one command id rather than letting the index answer with a
    driver error. Replay itself is the service's, before the write."""
    campaign, session, (a,) = _stage(world, seats=1)
    command = _command()
    _write(world, campaign, session.id, _document(world, campaign), _parts(a), command=command)
    with pytest.raises(RevealConflict):
        _write(world, campaign, session.id, _document(world, campaign), TableTarget(), command=command)


def test_a_stale_display_in_a_dead_session_is_ended_as_reconciled(world: World) -> None:
    """ID-15 at the store: a live disclosure left in a session that is no
    longer live is ended `reconciled` by the next write of that document, in
    `Written.reconciled` and never in `taken`."""
    campaign = _campaign(world)
    document = _document(world, campaign)
    dead = _session_left_behind(world, campaign)
    stale = _write(world, campaign, dead.id, document, TableTarget()).disclosure
    session = _session(world, campaign)

    written = _write(world, campaign, session.id, document, TableTarget())
    assert written.kind == "displayed" and written.taken == ()
    assert written.reconciled == (CopyChange(stale, (), True, EndReason.RECONCILED),)
    assert _shown(world, dead.id) == {None: None}
    assert _live(world, dead.id) == []


def _session_left_behind(w: World, campaign: str) -> TableSession:
    """A session that is `live` but past its expiry — the only dead session a
    GM can hold beside a new one once it has been finalised."""
    overdue = _session(w, campaign, hours=1, started_ago_h=2)
    with w.db.transaction() as unit:
        w.sessions.expire(unit, overdue.id, campaign, now=datetime.now(UTC))
    return overdue


def test_a_live_display_in_another_live_session_is_a_conflict(world: World) -> None:
    """The critic's point 14: `write` ends a display in another session only
    when that session is dead. A live one is a defect — one live session per GM
    forbids it — refused with nothing ended. The store does not judge its own
    session's liveness (the service does, under the row), so a write aimed at an
    ended session while the display sits in the live one reaches that guard."""
    campaign = _campaign(world)
    document = _document(world, campaign)
    ended = _ended_session(world, campaign)
    live = _session(world, campaign)
    shown = _write(world, campaign, live.id, document, TableTarget()).disclosure
    with pytest.raises(RevealConflict):
        _write(world, campaign, ended.id, document, TableTarget())
    assert _live(world, live.id) == [shown] and _live(world, ended.id) == []
    assert _slots(world, ended.id) == {}


def _ended_session(w: World, campaign: str) -> TableSession:
    """A session of the campaign that the owner started and ended — called
    before the live one starts, since a GM has one live session at a time."""
    session = _session(w, campaign)
    with w.db.transaction() as unit:
        w.sessions.end(unit, campaign, session.id, owner_id=w.owner)
    return session


# ── The shared suite: what each principal reads ──────────────────────────────


def test_the_picture_lists_the_table_slot_always_and_every_live_copy(world: World) -> None:
    """T-A22. The table entry comes first even with no row; a live copy is
    listed whatever its seat's state (`held` unless confirmed); the empty slot of
    a removed seat is omitted and an active seat's empty slot is not."""
    campaign = _campaign(world)
    confirmed = _seat(world, campaign, "confirmed", world.players[0])
    accepted = _seat(world, campaign, "accepted", world.players[1])
    gone = _seat(world, campaign, "confirmed", world.players[2])
    gone_empty = _seat(world, campaign, "confirmed", world.players[3])
    idle = _seat(world, campaign, "open", world.players[4])
    session = _session(world, campaign)
    document = _document(world, campaign)
    shown = _write(world, campaign, session.id, document, _parts(confirmed, accepted, gone, gone_empty, idle))
    _clear(world, session.id, ParticipantSlots(frozenset({gone_empty, idle}), EndReason.PARTICIPANT_REMOVED))
    for seat in (gone, gone_empty):
        with world.db.transaction() as unit:
            world.participants.remove(unit, campaign, seat)

    with world.db.transaction() as unit:
        picture = world.reveals.picture(unit, campaign, owner_id=world.owner, now=datetime.now(UTC))
    assert picture is not None
    assert (picture.session_id, picture.generation, picture.reveal_epoch) == (session.id, 1, 0)
    first = picture.entries[0]
    assert (first.audience_kind, first.participant_id, first.seq, first.live) == ("table", None, 0, None)
    listed = {e.participant_id: e for e in picture.entries[1:]}
    assert [e.participant_id for e in picture.entries[1:]] == sorted(listed)
    assert set(listed) == {confirmed, accepted, gone, idle}, "the removed seat's empty slot is omitted"
    assert listed[idle].live is None
    held = {pid: e.live.held for pid, e in listed.items() if e.live is not None}
    assert held == {confirmed: False, accepted: True, gone: True}
    live = listed[confirmed].live
    assert live is not None
    assert (live.disclosure_id, live.document_id, live.document_type, live.version, live.mask) == (
        shown.disclosure.id, document, "npc", 1, ("name",)
    )


def test_the_picture_and_the_views_see_nothing_of_a_session_past_expiry(world: World) -> None:
    """T-A23. A row still `live` past `expires_at` is dead to every read that
    decides what a principal sees, judged by the clock the caller passes — and
    the same reads see it at a clock before its expiry (the positive control
    that a `now()` in SQL would fail)."""
    campaign = _campaign(world)
    _seat(world, campaign, "confirmed", world.players[0])
    session = _session(world, campaign, hours=1, started_ago_h=2)
    before = session.started_at + timedelta(minutes=30)
    _write(world, campaign, session.id, _document(world, campaign), TableTarget(), now=before)
    with world.db.transaction() as unit:
        grant, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner, now=before)

    def reads(at: datetime) -> list[Any]:
        with world.db.transaction() as unit:
            return [
                world.reveals.picture(unit, campaign, owner_id=world.owner, now=at),
                world.reveals.view_for_account(unit, campaign, user_id=world.owner, now=at),
                world.reveals.view_for_account(unit, campaign, user_id=world.players[0], now=at),
                world.reveals.view_for_screen(unit, campaign, grant.id, now=at),
            ]

    assert None not in reads(before), "live at a clock before its expiry"
    assert reads(datetime.now(UTC)) == [None, None, None, None]


def test_who_sees_which_slot(world: World) -> None:
    """T-A24, the matrix, with the critic's row for a screen grant read under
    another campaign. The own slot needs a confirmed seat in the same query; the
    owner and a screen see the table slot only."""
    campaign = _campaign(world)
    elsewhere = _campaign(world, owner=world.other_owner)
    p = world.players
    confirmed = _seat(world, campaign, "confirmed", p[0])
    bare = _seat(world, campaign, "confirmed", p[1])
    accepted = _seat(world, campaign, "accepted", p[2])
    _seat(world, campaign, "offered", p[3])
    _seat(world, campaign, "removed", p[4])
    _seat(world, elsewhere, "confirmed", p[5])
    session = _session(world, campaign)
    _session(world, elsewhere, owner=world.other_owner)
    table_doc, own_doc, held_doc = (_document(world, campaign) for _ in range(3))
    _write(world, campaign, session.id, table_doc, TableTarget())
    mine = _write(world, campaign, session.id, own_doc, _parts(confirmed)).disclosure
    _write(world, campaign, session.id, held_doc, _parts(accepted))
    now = datetime.now(UTC)
    with world.db.transaction() as unit:
        live_grant, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner, now=now)
        old_grant, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner, now=now)
        revoked_grant, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner, now=now)
        world.sessions.revoke_screen(unit, campaign, revoked_grant.id, owner_id=world.owner, now=now)

    def account(user: int, where: str = campaign) -> Any:
        with world.db.transaction() as unit:
            return world.reveals.view_for_account(unit, where, user_id=user, now=datetime.now(UTC))

    def screen(grant: str, where: str = campaign) -> Any:
        with world.db.transaction() as unit:
            return world.reveals.view_for_screen(unit, where, grant, now=datetime.now(UTC))

    def picture() -> Any:
        with world.db.transaction() as unit:
            return world.reveals.picture(unit, campaign, owner_id=world.owner, now=datetime.now(UTC))

    owner = account(world.owner)
    assert (owner.is_owner, owner.own_slot, owner.mine, owner.participant_id) == (True, False, None, None)
    assert owner.table.document_id == table_doc and owner.table.document_type == "npc"
    seated = account(p[0])
    assert (seated.is_owner, seated.own_slot, seated.participant_id) == (False, True, confirmed)
    assert seated.table.document_id == table_doc
    assert seated.mine is not None and seated.mine.document_id == own_doc and seated.mine.mask == mine.mask
    empty = account(p[1])
    assert (empty.own_slot, empty.mine, empty.participant_id) == (True, EMPTY_SLOT, bare)
    waiting = account(p[2])
    assert (waiting.own_slot, waiting.mine, waiting.participant_id) == (False, None, accepted), (
        "an accepted seat the owner has not confirmed reads the table slot only; its held copy is absent"
    )
    assert waiting.table.document_id == table_doc
    on_screen = screen(live_grant.id)
    assert (on_screen.is_owner, on_screen.own_slot, on_screen.mine, on_screen.participant_id) == (
        False, False, None, None
    )
    assert on_screen.table.document_id == table_doc

    _retire_generation(world, session.id)
    with world.db.transaction() as unit:
        fresh, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner, now=datetime.now(UTC))
    assert screen(fresh.id) is not None, "the positive control: a grant of the current generation reads"
    nobody = [
        account(p[3]),  # an offered seat
        account(p[4]),  # a removed seat
        account(p[6]),  # an account with no seat
        account(world.other_owner),  # the GM of another campaign
        account(p[5]),  # a seat in another campaign
        screen(old_grant.id),  # a grant of an old generation
        screen(revoked_grant.id),  # a revoked grant
        screen(fresh.id, elsewhere),  # a live grant, read under another campaign
    ]
    assert nobody == [None] * len(nobody)
    assert picture() is not None, "the positive control: the owner's picture of the live session"
    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
    assert screen(fresh.id) is None, "a grant of an ended session"
    assert account(p[0]) is None and account(world.owner) is None
    assert picture() is None, "an ended session has no picture before its expires_at (I-11's state half)"


def test_a_private_copy_never_reads_as_the_table_slot(world: World) -> None:
    """T-A24, the table slot is the slot with no participant (TP-1, SEC-48):
    a confirmed seat's private copy, written before the session has a table
    slot row and still there once one is written after it, never reads as the
    table for the owner, another seat or a screen, whatever order the rows
    were written in."""
    campaign = _campaign(world)
    p = world.players
    holder = _seat(world, campaign, "confirmed", p[0])
    _seat(world, campaign, "confirmed", p[1])
    session = _session(world, campaign)
    private_doc, table_doc = _document(world, campaign), _document(world, campaign)
    _write(world, campaign, session.id, private_doc, _parts(holder))
    with world.db.transaction() as unit:
        grant, _ = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner, now=datetime.now(UTC))

    def views() -> dict[str, Any]:
        now = datetime.now(UTC)
        with world.db.transaction() as unit:
            return {
                "owner": world.reveals.view_for_account(unit, campaign, user_id=world.owner, now=now),
                "holder": world.reveals.view_for_account(unit, campaign, user_id=p[0], now=now),
                "other seat": world.reveals.view_for_account(unit, campaign, user_id=p[1], now=now),
                "screen": world.reveals.view_for_screen(unit, campaign, grant.id, now=now),
            }

    assert set(_slots(world, session.id)) == {holder}, "no table slot row yet"
    before = views()
    assert {who: view.table for who, view in before.items()} == dict.fromkeys(before, EMPTY_SLOT)
    assert before["holder"].mine is not None and before["holder"].mine.document_id == private_doc
    assert before["other seat"].mine == EMPTY_SLOT

    _write(world, campaign, session.id, table_doc, TableTarget())
    after = views()
    assert {who: view.table.document_id for who, view in after.items()} == dict.fromkeys(after, table_doc)
    assert after["holder"].mine is not None and after["holder"].mine.document_id == private_doc
    assert after["other seat"].mine == EMPTY_SLOT


def _retire_generation(w: World, session: str) -> None:
    """Move the session's generation on and leave its grants unrevoked — the
    state a mint that lost a race with a Rotate can commit."""
    if w.kind == "fake":
        rows = shared_rows(w.db, "table_sessions")
        with w.db.transaction() as unit:
            found = rows.visible(unit)[session]
            rows.replace(unit, session, replace(found, link_generation=found.link_generation + 1))
    else:
        assert w.dsn is not None
        with connect(w.dsn) as conn:
            conn.execute(
                "UPDATE campaign.table_sessions SET link_generation = link_generation + 1 WHERE id = %s", (session,)
            )


def test_every_not_entitled_view_is_the_same_none(world: World) -> None:
    """T-A25 (SEC-46): no session, another GM, a stranger, a missing campaign,
    and a grant id that names nothing all answer the one `None`."""
    campaign = _campaign(world)
    _seat(world, campaign, "offered", world.players[0])
    now = datetime.now(UTC)
    with world.db.transaction() as unit:
        before = [
            world.reveals.view_for_account(unit, campaign, user_id=world.owner, now=now),
            world.reveals.picture(unit, campaign, owner_id=world.owner, now=now),
        ]
    _session(world, campaign)
    with world.db.transaction() as unit:
        answers = before + [
            world.reveals.view_for_account(unit, campaign, user_id=world.players[0], now=now),
            world.reveals.view_for_account(unit, campaign, user_id=world.players[7], now=now),
            world.reveals.view_for_account(unit, "cmp_" + "z" * 22, user_id=world.owner, now=now),
            world.reveals.view_for_screen(unit, campaign, "tcr_" + "z" * 22, now=now),
            world.reveals.picture(unit, campaign, owner_id=world.other_owner, now=now),
        ]
    assert answers == [None] * len(answers)


def test_the_stale_slots_are_a_dead_sessions_copies_and_a_live_sessions_removed_seats(world: World) -> None:
    """What the reconciliation will clear: every live copy of a dead session,
    and in a live session only the copies of removed seats."""
    campaign = _campaign(world)
    kept, removed = (_seat(world, campaign, "confirmed", world.players[n]) for n in range(2))
    dead = _session_left_behind(world, campaign)
    _write(world, campaign, dead.id, _document(world, campaign), TableTarget())
    session = _session(world, campaign)
    _write(world, campaign, session.id, _document(world, campaign), _parts(kept, removed))
    with world.db.transaction() as unit:
        world.participants.remove(unit, campaign, removed)
    with world.db.transaction() as unit:
        stale = world.reveals.stale_slots(unit, campaign, now=datetime.now(UTC))
    expected = {
        dead.id: EverySlot(EndReason.RECONCILED),
        session.id: ParticipantSlots(frozenset({removed}), EndReason.RECONCILED),
    }
    assert [s.session_id for s in stale] == sorted(expected)
    assert {s.session_id: s.scope for s in stale} == expected


def test_a_session_still_live_past_its_expiry_is_stale(world: World) -> None:
    """ID-19, the clock's half: a session whose row still says `live` after its
    `expires_at` is dead to the reconciliation, so every live copy it holds is
    stale, judged by the clock the caller passes. The same read at a clock
    before its expiry finds nothing (the positive control)."""
    campaign = _campaign(world)
    seat = _seat(world, campaign, "confirmed", world.players[0])
    overdue = _session(world, campaign, hours=1, started_ago_h=2)
    before = overdue.started_at + timedelta(minutes=30)
    _write(world, campaign, overdue.id, _document(world, campaign), TableTarget(), now=before)
    _write(world, campaign, overdue.id, _document(world, campaign), _parts(seat), now=before)

    def stale(at: datetime) -> list[StaleSlots]:
        with world.db.transaction() as unit:
            return world.reveals.stale_slots(unit, campaign, now=at)

    assert stale(before) == [], "live, with no removed seat, at a clock before its expiry"
    assert stale(datetime.now(UTC)) == [StaleSlots(overdue.id, EverySlot(EndReason.RECONCILED))]


def test_everyone_seated_is_the_confirmed_active_seats_only(world: World) -> None:
    """What *Everyone seated* expands to, and what an audience may name: the
    active seats (AUD-10) — an open seat included — never a removed one."""
    campaign = _campaign(world)
    seats = {state: _seat(world, campaign, state, world.players[n]) for n, state in enumerate(
        ("open", "offered", "accepted", "confirmed", "removed")
    )}
    other = _campaign(world, owner=world.other_owner)
    foreign = _seat(world, other, "confirmed", world.players[6])
    with world.db.transaction() as unit:
        assert world.reveals.confirmed_seats(unit, campaign) == frozenset({seats["confirmed"]})
        assert world.reveals.active_participants(unit, campaign, [*seats.values(), foreign]) == frozenset(
            seats[s] for s in ("open", "offered", "accepted", "confirmed")
        )


def test_the_invariant_auditor_finds_each_broken_invariant(world: World) -> None:
    """T-A26. Each of I-1 to I-5, broken on purpose, is reported."""
    world.audited = False
    campaign, session, (a, b, _) = _stage(world)
    one, two = _document(world, campaign), _document(world, campaign)
    table = _write(world, campaign, session.id, one, TableTarget()).disclosure
    pair = _write(world, campaign, session.id, two, _parts(a, b)).disclosure
    assert world.audit() == []
    slots = _slots(world, session.id)

    if world.kind == "fake":
        disclosures, slot_rows = world.reveals.disclosures, world.reveals.slot_rows
        with world.db.transaction() as unit:
            disclosures.replace(unit, pair.id, replace(pair, ended_at=datetime.now(UTC), ended_reason=EndReason.MOVED))
            slot_rows.replace(unit, slots[None].id, replace(slots[None], disclosure_id=None))
            slot_rows.add(unit, "rsl_dup", replace(slots[a], id="rsl_dup", disclosure_id=None))
            disclosures.add(unit, "dsc_two", replace(table, id="dsc_two"))
            slot_rows.replace(unit, slots[b].id, replace(slots[b], disclosure_id="dsc_two"))
            disclosures.add(unit, "dsc_far", replace(table, id="dsc_far", document_id="doc_x", session_id="ses_x"))
            slot_rows.add(unit, "rsl_far", replace(slots[None], id="rsl_far", participant_id="prt_x",
                                                   disclosure_id="dsc_far"))
        expected = {"I-1", "I-2", "I-3", "I-4", "I-5"}
    else:
        assert world.dsn is not None
        with connect(world.dsn) as conn:
            conn.execute("UPDATE campaign.reveal_disclosures SET ended_at = now(), ended_reason = 'moved' "
                         "WHERE id = %s", (pair.id,))
            conn.execute("UPDATE campaign.reveal_slots SET disclosure_id = NULL WHERE id = %s", (slots[None].id,))
            conn.execute("DROP INDEX campaign.reveal_disclosures_one_live_per_document_uidx")
            conn.execute(
                "INSERT INTO campaign.reveal_disclosures (id, campaign_id, session_id, document_id, version_number, "
                "mask, audience_kind, command_id, created_at) VALUES (%s, %s, %s, %s, 1, ARRAY['name'], 'table', "
                "%s, now())",
                ("dsc_" + "t" * 22, campaign, session.id, one, _command()),
            )
        expected = {"I-2", "I-5"}
    found = world.audit()
    assert {line.split(":")[0] for line in found} == expected
    assert any("no copy" in line for line in found) and any("ended disclosure" in line for line in found)


def test_no_reveal_record_or_refusal_shows_a_command_id_or_text(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """T-A28. The command id is the client's: no record's `repr()` shows it, a
    refusal repeats no key that is really a sentence, and nothing is logged."""
    caplog.set_level(logging.DEBUG)
    campaign, session, (a,) = _stage(world, seats=1)
    command = "Rook-saw-the-card-" + secrets.token_urlsafe(8)
    written = _write(world, campaign, session.id, _document(world, campaign), _parts(a), command=command)
    moved = _write(world, campaign, session.id, written.disclosure.document_id, TableTarget())
    for record in (written, written.disclosure, moved, *moved.taken):
        assert command not in repr(record) and command not in str(record)
    with pytest.raises(MaskRefused) as refused:
        _write(world, campaign, session.id, _document(world, campaign), TableTarget(),
               mask=("The Hooded Stranger", "all", "name", "name"))
    assert refused.value.keys == ("all", "name")
    assert "Hooded" not in str(refused.value) and "Hooded" not in repr(refused.value)
    assert not [r for r in caplog.records if command in r.getMessage()]


# ── PostgreSQL only: the schema ──────────────────────────────────────────────


def _rows(w: World, seats: int = 2) -> tuple[str, TableSession, list[str], str]:
    campaign, session, people = _stage(w, seats)
    return campaign, session, people, _document(w, campaign)


def _insert_disclosure(conn: Any, campaign: str, session: str, document: str, *, audience: str = "table",
                       mask: Any = ("name",), version: int = 1, ended: bool = False) -> str:
    made = "dsc_" + secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO campaign.reveal_disclosures (id, campaign_id, session_id, document_id, version_number, mask, "
        "audience_kind, command_id, created_at, ended_at, ended_reason) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)",
        (made, campaign, session, document, version, list(mask), audience, _command(),
         datetime.now(UTC) if ended else None, "gm_stop" if ended else None),
    )
    return made


@needs_db
def test_a_second_live_disclosure_of_one_document_is_refused_and_an_ended_one_is_not(pgw: World) -> None:
    """T-A2. One live disclosure per document across the campaign's sessions,
    and an ended one blocks nothing."""
    campaign = _campaign(pgw)
    other = _ended_session(pgw, campaign).id
    session = _session(pgw, campaign)
    document = _document(pgw, campaign)
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        _insert_disclosure(conn, campaign, session.id, document, ended=True)
        _insert_disclosure(conn, campaign, session.id, document)
        for where in (session.id, other):
            with pytest.raises(psycopg.errors.UniqueViolation):
                _insert_disclosure(conn, campaign, where, document)


@needs_db
def test_a_table_slot_cannot_show_a_participant_disclosure_or_the_reverse(pgw: World) -> None:
    """T-A3 (I-3): the audience kind is part of the slot -> disclosure key."""
    campaign, session, (a, _), document = _rows(pgw)
    table = _write(pgw, campaign, session.id, document, TableTarget()).disclosure
    pair = _write(pgw, campaign, session.id, _document(pgw, campaign), _parts(a)).disclosure
    slots = _slots(pgw, session.id)
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        for slot, shown in ((slots[None], pair), (slots[a], table)):
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute("UPDATE campaign.reveal_slots SET disclosure_id = %s WHERE id = %s", (shown.id, slot.id))


@needs_db
def test_a_slot_cannot_show_a_disclosure_of_another_session(pgw: World) -> None:
    """T-A4 (I-4): the session is part of the slot -> disclosure key."""
    campaign = _campaign(pgw)
    other = _ended_session(pgw, campaign).id
    session = _session(pgw, campaign)
    document = _document(pgw, campaign)
    shown = _write(pgw, campaign, session.id, document, TableTarget()).disclosure
    _write(pgw, campaign, other, _document(pgw, campaign), TableTarget())
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "UPDATE campaign.reveal_slots SET disclosure_id = %s WHERE session_id = %s", (shown.id, other)
            )


@needs_db
def test_the_composite_keys_refuse_a_cross_campaign_row(pgw: World) -> None:
    """T-A6 (the schema's half): a disclosure of another campaign's document or
    session, of a version that does not exist, and a slot for another
    campaign's participant are each refused by a foreign key."""
    campaign, session, _, document = _rows(pgw)
    other = _campaign(pgw, owner=pgw.other_owner)
    foreign_document = _document(pgw, other)
    foreign_session = _session(pgw, other, owner=pgw.other_owner)
    foreign_seat = _seat(pgw, other, "open", pgw.players[7])
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        for kwargs in (
            {"document": foreign_document},
            {"session": foreign_session.id},
            {"version": 7},
        ):
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                _insert_disclosure(conn, campaign, kwargs.get("session", session.id),
                                   kwargs.get("document", document), version=kwargs.get("version", 1))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO campaign.reveal_slots (id, campaign_id, session_id, audience_kind, participant_id, "
                "updated_at) VALUES (%s, %s, %s, 'participant', %s, now())",
                ("rsl_" + "f" * 22, campaign, session.id, foreign_seat),
            )


@needs_db
def test_a_document_with_a_live_copy_cannot_be_deleted_until_its_copies_are_cleared(pgw: World) -> None:
    """T-A7. The slot -> disclosure key has no delete action: deleting a
    document (or its version) that is still shown fails, which makes the bead
    that deletes documents narrow first; after the clear it passes, and the
    ended disclosure goes with it. A session's deletion, which takes the slots
    in the same statement, passes."""
    campaign, session, _, document = _rows(pgw)
    _write(pgw, campaign, session.id, document, TableTarget())
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM campaign.documents WHERE id = %s", (document,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM campaign.document_versions WHERE document_id = %s", (document,))
    _clear(pgw, session.id, DocumentCopies(document, EndReason.DOCUMENT_DELETED))
    with connect(pgw.dsn) as conn:
        conn.execute("DELETE FROM campaign.documents WHERE id = %s", (document,))
        assert conn.execute("SELECT count(*) FROM campaign.reveal_disclosures").fetchone()[0] == 0
        second = _document(pgw, campaign)
        _write(pgw, campaign, session.id, second, TableTarget())
        conn.execute("DELETE FROM campaign.table_sessions WHERE id = %s", (session.id,))
        assert conn.execute("SELECT count(*) FROM campaign.reveal_slots").fetchone()[0] == 0


@needs_db
def test_deleting_an_account_takes_every_live_copy_of_its_campaigns(pgw: World) -> None:
    """T-A11 with live copies (SEC-36): an account's deletion cascades through
    its campaigns, sessions, documents and seats, and takes live disclosures and
    the slots that show them in the same statement — the slot -> disclosure key's
    NO ACTION is checked after every cascade has run. The table slot is reached
    only through its session."""
    campaign, session, (a, _), document = _rows(pgw)
    _write(pgw, campaign, session.id, document, TableTarget())
    _write(pgw, campaign, session.id, _document(pgw, campaign), _parts(a))
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        for table in ("campaign.reveal_disclosures", "campaign.reveal_slots"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 2, table
        conn.execute("DELETE FROM auth.users WHERE id = %s", (pgw.owner,))
        for table in ("campaign.reveal_disclosures", "campaign.reveal_slots", "campaign.table_sessions"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


@needs_db
@pytest.mark.parametrize(
    "mask",
    [["all"], ["name", None], ["Bad Key"], [], ["a" * 41], [f"k{n}" for n in range(65)], ["name,voice"]],
    ids=["all", "null", "bad-key", "empty", "long-key", "65-keys", "comma"],
)
def test_the_mask_check_refuses_all_null_a_bad_key_and_an_empty_mask(pgw: World, mask: list[Any]) -> None:
    """T-A10: each clause of the mask CHECK, with the positive control that a
    well-formed mask of 64 keys is accepted."""
    campaign, session, _, document = _rows(pgw, seats=0)
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_disclosure(conn, campaign, session.id, document, mask=mask)
        _insert_disclosure(conn, campaign, session.id, document, mask=[f"k{n}" for n in range(64)])


@needs_db
def test_end_reasons_on_the_server_equal_the_enum(pgw: World) -> None:
    """T-A5 (the server's half): the CHECK in force holds exactly `EndReason`."""
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'reveal_disclosures_ended_reason_check'"
        ).fetchone()[0]
    for member in EndReason:
        assert f"'{member.value}'::text" in definition
    assert definition.count("::text") == len(EndReason)


@needs_db
def test_no_visibility_column_on_documents_or_versions(pgw: World) -> None:
    """T-A12 (ED-6): nothing about visibility sits on a document or a version."""
    assert pgw.dsn is not None
    with connect(pgw.dsn) as conn:
        columns = [
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'campaign' "
                "AND table_name IN ('documents', 'document_versions')"
            ).fetchall()
        ]
    assert columns, "the scan found the two tables"
    words = ("reveal", "mask", "visib", "audience", "disclos", "slot", "shown")
    assert not [c for c in columns if any(w in c for w in words)]


# ── PostgreSQL only: who waits for whom ──────────────────────────────────────


def _someone_waits_on_a_lock(dsn: str) -> bool:
    deadline = time.monotonic() + PATIENCE
    with connect(dsn) as conn:
        while time.monotonic() < deadline:
            blocked = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if blocked:
                return True
            time.sleep(0.05)
    return False


@contextmanager
def _holding(dsn: str, statement: str, params: tuple[Any, ...]) -> Iterator[None]:
    """A second connection that runs `statement` and keeps its transaction open
    until the block ends, then rolls back."""
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with connect(dsn, autocommit=False) as conn:
                conn.execute(statement, params)
                took.set()
                release.wait(PATIENCE)
                conn.rollback()
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE), "the row was never locked"
    if failure:
        raise failure[0]
    try:
        yield
    finally:
        release.set()
        thread.join(PATIENCE)


def _in_background(work: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(work())
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _without_waiting(w: World, work: Callable[[Any], object]) -> object:
    """Run `work` in a transaction whose every lock wait is cut at two seconds:
    a wait is then a `LockNotAvailable`, never a pass."""
    with w.db.transaction() as unit:
        unit.conn.execute("SET LOCAL lock_timeout = '2s'")
        return work(unit)


@needs_db
@pytest.mark.parametrize("call", ["write", "clear"])
def test_a_write_and_a_clear_wait_for_the_session_row(pgw: World, call: str) -> None:
    """T-A21 (the server's half): with another connection holding the session
    row `FOR NO KEY UPDATE`, a write and a clear each wait — the server says
    so — and complete once it lets go."""
    campaign, session, (a, _), document = _rows(pgw)
    if call == "clear":
        _write(pgw, campaign, session.id, document, _parts(a))
    assert pgw.dsn is not None
    with _holding(pgw.dsn, "SELECT 1 FROM campaign.table_sessions WHERE id = %s FOR NO KEY UPDATE", (session.id,)):
        work: Callable[[], object] = (
            (lambda: _write(pgw, campaign, session.id, document, _parts(a)))
            if call == "write"
            else (lambda: _clear(pgw, session.id, EverySlot(EndReason.NARROWED)))
        )
        thread, outcome = _in_background(work)
        assert _someone_waits_on_a_lock(pgw.dsn), f"the {call} did not wait for the session row"
    thread.join(PATIENCE)
    assert outcome and not isinstance(outcome[0], BaseException), outcome


@needs_db
def test_ending_a_disclosure_and_repointing_a_slot_never_wait_for_a_foreign_key_check(pgw: World) -> None:
    """T-A8 (RQ-3). A foreign-key check takes `FOR KEY SHARE` on the row it
    references. With one held on the disclosure and on the slot, a clear that
    ends the one and re-points the other does not wait — and the positive
    control, a real `FOR UPDATE` on the disclosure, does make it wait."""
    campaign, session, (a, _), document = _rows(pgw)
    shown = _write(pgw, campaign, session.id, document, _parts(a)).disclosure
    slot = _slots(pgw, session.id)[a]
    assert pgw.dsn is not None
    with _holding(
        pgw.dsn,
        "SELECT 1 FROM campaign.reveal_disclosures d, campaign.reveal_slots r "
        "WHERE d.id = %s AND r.id = %s FOR KEY SHARE",
        (shown.id, slot.id),
    ):
        _without_waiting(pgw, lambda unit: pgw.reveals.clear(unit, session.id, EverySlot(EndReason.NARROWED),
                                                             now=datetime.now(UTC)))
    assert _live(pgw, session.id) == []

    again = _write(pgw, campaign, session.id, document, _parts(a)).disclosure
    with _holding(pgw.dsn, "SELECT 1 FROM campaign.reveal_disclosures WHERE id = %s FOR UPDATE", (again.id,)):
        thread, outcome = _in_background(lambda: _clear(pgw, session.id, EverySlot(EndReason.NARROWED)))
        assert _someone_waits_on_a_lock(pgw.dsn), "the positive control: FOR UPDATE does make the clear wait"
    thread.join(PATIENCE)
    assert outcome and not isinstance(outcome[0], BaseException)


@needs_db
def test_the_new_unique_keys_leave_rotate_and_document_writes_non_blocking(pgw: World) -> None:
    """T-A9. The two keys this migration adds cover `id`, which nothing
    updates: a Rotate holding its session row, and a document write holding its
    document row, never make an insert that references either wait — and a real
    `FOR UPDATE` on the session does (the positive control)."""
    campaign, session, _, document = _rows(pgw, seats=0)
    assert pgw.dsn is not None
    rotating = threading.Event()
    release = threading.Event()

    def rotate_and_hold() -> None:
        with pgw.db.transaction() as unit:
            pgw.sessions.rotate(unit, campaign, session.id, owner_id=pgw.owner, command_id=_command())
            pgw.documents.write_fields(
                unit, campaign, document, fields={"voice": "hoarse"}, author=Author.GM, base_write_revision=None
            )
            rotating.set()
            release.wait(PATIENCE)

    holder = threading.Thread(target=rotate_and_hold, daemon=True)
    holder.start()
    try:
        assert rotating.wait(PATIENCE)
        _without_waiting(pgw, lambda unit: _referencing_insert(unit.conn, campaign, session.id, document))
    finally:
        release.set()
        holder.join(PATIENCE)

    with _holding(pgw.dsn, "SELECT 1 FROM campaign.table_sessions WHERE id = %s FOR UPDATE", (session.id,)):
        thread, outcome = _in_background(lambda: _referencing_insert_committed(pgw.dsn, campaign, session.id, document))
        assert _someone_waits_on_a_lock(pgw.dsn), "the positive control: FOR UPDATE on the session does block"
    thread.join(PATIENCE)
    assert outcome and not isinstance(outcome[0], BaseException), outcome


def _referencing_insert(conn: Any, campaign: str, session: str, document: str) -> None:
    """An insert whose foreign-key checks take `FOR KEY SHARE` on the session,
    the document and its version."""
    conn.execute(
        "INSERT INTO campaign.reveal_disclosures (id, campaign_id, session_id, document_id, version_number, "
        "mask, audience_kind, command_id, created_at) "
        "VALUES (%s, %s, %s, %s, 1, ARRAY['name'], 'table', %s, now())",
        ("dsc_" + secrets.token_urlsafe(16), campaign, session, document, _command()),
    )


def _referencing_insert_committed(dsn: str | None, campaign: str, session: str, document: str) -> None:
    assert dsn is not None
    with connect(dsn, autocommit=False) as conn:
        conn.execute("UPDATE campaign.reveal_disclosures SET ended_at = now(), ended_reason = 'gm_stop' "
                     "WHERE document_id = %s AND ended_at IS NULL", (document,))
        _referencing_insert(conn, campaign, session, document)
        conn.rollback()


# ══ PR-B: the reveal service, the fills, and RQ-7 ═══════════════════════════
#
# The service suite runs in both worlds, with every session store built with
# the fill (`reveals.slot_clear_for`), as production builds them. The races
# after it are PostgreSQL's alone: two connections, explicit interleaving, and
# the server's own account of who waits (`pg_stat_activity`).


class _Recorded:
    """The world's database, remembering every unit of work it hands out, so a
    test can read what each of them locked — in either world."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.units: list[Any] = []

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._db.transaction() as unit:
            self.units.append(unit)
            yield unit

    def locks(self, since: int = 0) -> list[tuple[str, str]]:
        return [lock for unit in self.units[since:] for lock in unit.campaign_locks]


@dataclass
class Served:
    """A world whose session store clears what it narrows, and the services
    over it: the reveal service, the session lifecycle, and the campaign
    stores the seat and archive functions compose."""

    w: World
    db: _Recorded
    reveals: Reveals
    audit: Any
    jobs: Any
    stores: CampaignStores
    lifecycle: TableSessions

    def over(self, db: Any, *, attempts: int = 1) -> Reveals:
        """The same stores over another database — the races' second
        connection, or a paused one. One attempt: a deadlock a retry would
        hide is seen (critic 10)."""
        return _reveals_over(self.w, self.audit, db, attempts)

    def lifecycle_over(self, db: Any) -> TableSessions:
        return _lifecycle_over(self.w, self.audit, self.jobs, db)


def _reveals_over(w: World, audit: Any, db: Any, attempts: int) -> Reveals:
    return Reveals(
        db,
        campaigns=w.campaigns,
        sessions=w.sessions,
        reveals=w.reveals,
        documents=w.documents,
        audit=audit,
        attempts=attempts,
    )


def _lifecycle_over(w: World, audit: Any, jobs: Any, db: Any) -> TableSessions:
    return TableSessions(
        db,
        campaigns=w.campaigns,
        sessions=w.sessions,
        audit=audit,
        jobs=jobs,
        reconcile=lambda unit, campaign_id: enqueue_reconciliation(unit, jobs, campaign_id),
    )


def _serve(w: World) -> Served:
    recorded = _Recorded(w.db)
    if w.kind == "fake":
        audit: Any = InMemoryAuditLog()
        jobs: Any = InMemoryJobQueue(db=w.db)
        offers: Any = InMemorySeatOfferStore(w.db)
        summaries: Any = InMemoryCampaignSummaryStore(w.db, messages=InMemoryMessageStore())
    else:
        audit, jobs = PostgresAuditLog(), PostgresJobQueue(w.db)
        offers, summaries = PostgresSeatOfferStore(), PostgresCampaignSummaryStore()
    return Served(
        w,
        recorded,
        _reveals_over(w, audit, recorded, DEADLOCK_ATTEMPTS),
        audit,
        jobs,
        CampaignStores(w.campaigns, w.participants, w.sessions, offers, audit, summaries),
        _lifecycle_over(w, audit, jobs, recorded),
    )


def _fake_served() -> Served:
    db = InMemoryDatabase()
    rows = InMemoryRevealStore(db)
    return _serve(
        World(
            "fake",
            db,
            InMemoryCampaignStore(db),
            InMemoryParticipantStore(db),
            InMemoryTableSessionStore(db, slot_clear=slot_clear_for(rows)),
            InMemoryDocumentStore(db),
            rows,
            owner=1,
            other_owner=2,
            players=tuple(range(3, 11)),
        )
    )


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def served(request: pytest.FixtureRequest) -> Iterator[Served]:
    made = _fake_served() if request.param == "fake" else _serve(_pg_world(request.getfixturevalue("dsn"), fill=True))
    yield made
    assert made.w.audit() == [], "the invariant auditor found a broken invariant after the test"


@pytest.fixture
def pgs(dsn: str) -> Iterator[Served]:
    made = _serve(_pg_world(dsn, fill=True))
    yield made
    assert made.w.audit() == []


# ── Service helpers ──────────────────────────────────────────────────────────


REVEAL_ACTIONS = {AuditAction.REVEAL_DISPLAYED, AuditAction.REVEAL_UPDATED, AuditAction.REVEAL_STOPPED}
A_STATBLOCK = {
    "name": "Ogre",
    "ac": 11,
    "hp": 59,
    "abilities": {},
    "xp": None,
    "traits": [{"name": "Brute", "text": "  "}],
}
A_LORE = {"name": "The Drowned Bell", "rumours": ["it rings at low tide", "   "]}


def _now() -> datetime:
    return datetime.now(UTC)


def _epoch(w: World, session: str) -> int:
    with w.db.transaction() as unit:
        found = w.sessions.get(unit, session)
    assert found is not None
    return int(found.reveal_epoch)


def _command_for(
    campaign: str,
    session: str,
    document: str,
    audience: Any,
    epoch: int,
    *,
    mask: tuple[str, ...] = ("name",),
    version: int = 1,
    command: str | None = None,
) -> DisplayCommand:
    return DisplayCommand(campaign, session, command or _command(), epoch, document, version, mask, audience)


def _confirm(
    s: Served,
    campaign: str,
    session: str,
    document: str,
    audience: Any = None,
    *,
    mask: tuple[str, ...] = ("name",),
    version: int = 1,
    command: str | None = None,
    epoch: int | None = None,
    owner: int | None = None,
    reveals: Reveals | None = None,
) -> Displayed:
    composed = _command_for(
        campaign,
        session,
        document,
        TableAudience() if audience is None else audience,
        _epoch(s.w, session) if epoch is None else epoch,
        mask=mask,
        version=version,
        command=command,
    )
    return (reveals or s.reveals).display(composed, owner_id=s.w.owner if owner is None else owner, now=_now())


def _stop(
    s: Served,
    campaign: str,
    scope: Any,
    *,
    command: str | None = None,
    owner: int | None = None,
    reveals: Reveals | None = None,
) -> RevealPicture | None:
    return (reveals or s.reveals).stop(
        campaign,
        owner_id=s.w.owner if owner is None else owner,
        scope=scope,
        command_id=command or _command(),
        now=_now(),
    )


def _reveal_rows(s: Served, campaign: str) -> list[Any]:
    with s.w.db.transaction() as unit:
        return [e for e in s.audit.for_campaign(unit, campaign) if e.action in REVEAL_ACTIONS]


def _revision(w: World, campaign: str) -> int:
    with w.db.transaction() as unit:
        if w.kind == "fake":
            return int(unit.authz_revision(campaign))
        row = unit.conn.execute(
            "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (campaign,)
        ).fetchone()
        return int(row[0])


def _queued(s: Served) -> int:
    if s.w.kind == "fake":
        return len(s.jobs._rows)
    with s.w.db.transaction() as unit:
        return int(unit.conn.execute("SELECT count(*) FROM app.jobs").fetchone()[0])


def _state(s: Served, campaign: str, session: str) -> tuple[Any, ...]:
    """Everything a refused Confirm must leave as it found."""
    return (
        _shown(s.w, session),
        _seqs(s.w, session),
        [d.id for d in _live(s.w, session)],
        _epoch(s.w, session),
        len(_reveal_rows(s, campaign)),
        _revision(s.w, campaign),
    )


def _doc_of(w: World, campaign: str, doc_type: DocumentTypeId, data: dict[str, Any]) -> str:
    with w.db.transaction() as unit:
        made = w.documents.create(unit, campaign, doc_type=doc_type, type_version=1, data=dict(data), author=Author.GM)
    with w.db.transaction() as unit:
        w.documents.seal(unit, campaign, made.id)
    return str(made.id)


def _refusal(caught: pytest.ExceptionInfo[BaseException]) -> tuple[Any, ...]:
    return type(caught.value), str(caught.value), caught.value.args


def _entry(picture: RevealPicture, participant: str | None) -> Any:
    [found] = [e for e in picture.entries if e.participant_id == participant]
    return found


def _empty_sessions(w: World) -> Any:
    """A session store whose narrowings clear nothing — how a test leaves a
    display behind in a session that has ended."""
    if w.kind == "fake":
        return InMemoryTableSessionStore(w.db, slot_clear=no_slots)
    return PostgresTableSessionStore(slot_clear=no_slots)


def _left_live_in_a_dead_session(w: World, campaign: str, document: str) -> tuple[TableSession, str]:
    """A session that displayed `document` and then ended without clearing it —
    what an older build, or a narrowing before this bead, could leave."""
    dead = _session(w, campaign)
    command = _command()
    _write(w, campaign, dead.id, document, TableTarget(), command=command)
    with w.db.transaction() as unit:
        _empty_sessions(w).end(unit, campaign, dead.id, owner_id=w.owner)
    return dead, command


# ── T-B1 to T-B9: a Confirm's order and its refusals ─────────────────────────


def test_a_strangers_confirm_is_the_one_404_and_takes_no_lock(served: Served) -> None:
    """T-B1 (SEC-2, SEC-3). Another GM naming my session, me naming another
    GM's session, a session of another of my campaigns, a document of another
    campaign and a missing document: one refusal, byte-identical, and no unit
    of work took the campaign lock or wrote anything."""
    s, w = served, served.w
    elsewhere = _campaign(w)
    old = _ended_session(w, elsewhere)
    campaign, session, _ = _stage(w, seats=0)
    document = _document(w, campaign)
    theirs = _campaign(w, owner=w.other_owner)
    their_session = _session(w, theirs, owner=w.other_owner)
    their_document = _document(w, theirs)
    before, marks = _state(s, campaign, session.id), len(s.db.units)

    cases: list[dict[str, Any]] = [
        {"owner": w.other_owner},
        {"campaign": theirs, "session": their_session.id, "document": their_document},
        {"session": old.id},
        {"document": their_document},
        {"document": "doc_" + "z" * 22},
    ]
    answers = set()
    for case in cases:
        with pytest.raises(RevealNotFound) as caught:
            _confirm(
                s,
                case.get("campaign", campaign),
                case.get("session", session.id),
                case.get("document", document),
                owner=case.get("owner"),
                epoch=0,
            )
        answers.add(_refusal(caught))
    assert len(answers) == 1, "one answer for every stranger"
    assert len(s.db.units) - marks == len(cases) and s.db.locks(marks) == [], "no lock before ownership"
    assert _state(s, campaign, session.id) == before


def test_a_replayed_confirm_writes_nothing_even_after_its_own_epoch_advance_and_a_later_stop(
    served: Served,
) -> None:
    """T-B2 (ID-10, critic 5). A retried Confirm whose first attempt committed
    answers the current picture and writes nothing — though its own write moved
    the epoch it carries, and though a Stop has since cleared what it showed.
    Once the session has ended there is no picture: the replay is a conflict."""
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    document = _document(w, campaign)
    command, epoch = _command(), _epoch(w, session.id)
    first = _confirm(s, campaign, session.id, document, command=command, epoch=epoch)
    assert not first.replayed and _entry(first.picture, None).live.document_id == document

    after, marks = _state(s, campaign, session.id), len(s.db.units)
    again = _confirm(s, campaign, session.id, document, command=command, epoch=epoch)
    assert again == Displayed(first.picture, True)
    assert _state(s, campaign, session.id) == after and s.db.locks(marks) == []

    _stop(s, campaign, StopAll())
    stopped = _state(s, campaign, session.id)
    later = _confirm(s, campaign, session.id, document, command=command, epoch=epoch)
    assert later.replayed and _entry(later.picture, None).live is None
    assert later.picture.reveal_epoch == epoch + 2, "the current picture, not a stored one"
    assert _state(s, campaign, session.id) == stopped

    with w.db.transaction() as unit:
        w.sessions.end(unit, campaign, session.id, owner_id=w.owner)
    ended, marks = _state(s, campaign, session.id), len(s.db.units)
    with pytest.raises(RevealConflict):
        _confirm(s, campaign, session.id, document, command=command, epoch=epoch)
    assert _state(s, campaign, session.id) == ended and s.db.locks(marks) == []


def test_a_stale_epoch_or_a_dead_session_is_a_conflict_before_the_campaign_lock(served: Served) -> None:
    """T-B3 (REVEAL-5, RQ-11). An ended session, a session still `live` past
    its expiry, and an epoch the table has moved past are each refused before
    any lock is asked for; the Confirm that is current takes the share lock."""
    s, w = served, served.w
    campaign = _campaign(w)
    document = _document(w, campaign)
    ended = _ended_session(w, campaign)
    overdue = _session(w, campaign, hours=1, started_ago_h=2)
    for dead in (ended, overdue):
        marks = len(s.db.units)
        with pytest.raises(RevealConflict):
            _confirm(s, campaign, dead.id, document)
        assert s.db.locks(marks) == [], "the courtesy check comes before the lock"
    with w.db.transaction() as unit:
        w.sessions.expire(unit, overdue.id, campaign, now=_now())
    session = _session(w, campaign)
    before, marks = _state(s, campaign, session.id), len(s.db.units)
    with pytest.raises(RevealConflict):
        _confirm(s, campaign, session.id, document, epoch=_epoch(w, session.id) + 1)
    assert s.db.locks(marks) == [] and _state(s, campaign, session.id) == before

    marks = len(s.db.units)
    _confirm(s, campaign, session.id, document)
    assert s.db.locks(marks) == [(campaign, "share")], "positive control: a current Confirm does lock"


def test_a_mask_holds_only_revealable_present_non_empty_keys(served: Served) -> None:
    """T-B4 (REVEAL-9, REVEAL-10, ED-5, critic 6). Each mask is refused with
    exactly the sorted keys at fault, and nothing is written: a key the type
    does not allow (`tags`, `npc.true_identity`, `all`), a key named twice, a
    key absent from the pinned version, a text blank after trimming, and the
    contract's per-kind emptiness — a list with a blank item, an entry with a
    blank text, an empty abilities block and a null integer."""
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    npc = _doc_of(w, campaign, DocumentTypeId.NPC, {**AN_NPC, "attitude": " \t "})
    lore = _doc_of(w, campaign, DocumentTypeId.LORE, A_LORE)
    stat = _doc_of(w, campaign, DocumentTypeId.STATBLOCK, A_STATBLOCK)
    cases = [
        (npc, ("tags",), ("tags",)),
        (npc, ("name", "true_identity"), ("true_identity",)),
        (npc, ("tell",), ("tell",)),
        (npc, ("attitude",), ("attitude",)),
        (npc, ("all",), ("all",)),
        (npc, ("name", "name"), ("name",)),
        (npc, ("voice", "tags", "tell"), ("tags", "tell")),
        (lore, ("rumours",), ("rumours",)),
        (stat, ("name", "traits"), ("traits",)),
        (stat, ("abilities",), ("abilities",)),
        (stat, ("xp", "ac"), ("xp",)),
    ]
    before = _state(s, campaign, session.id)
    for document, mask, at_fault in cases:
        with pytest.raises(MaskRefused) as caught:
            _confirm(s, campaign, session.id, document, mask=mask)
        assert caught.value.keys == at_fault, mask
    assert _state(s, campaign, session.id) == before

    shown = _confirm(s, campaign, session.id, npc, mask=("voice", "name"))
    assert _entry(shown.picture, None).live.mask == ("name", "voice"), "positive control"


def test_a_masks_presence_is_read_from_the_pinned_version_never_the_head(served: Served) -> None:
    """T-B4, the pinned version (REVEAL-9, ED-9; fg7k M1). Presence and
    emptiness are judged on the version the Confirm pins: version 1 sealed with
    a blank `attitude` stays refused while an open version 2 fills it, and
    version 1 sealed with it filled is displayable while an open version 2
    blanks it — otherwise a pinned version could be shown that the projection
    cannot read."""
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    filled_later = _doc_of(w, campaign, DocumentTypeId.NPC, {**AN_NPC, "attitude": " \t "})
    blanked_later = _doc_of(w, campaign, DocumentTypeId.NPC, {**AN_NPC, "attitude": "friendly"})
    for document, head in ((filled_later, "friendly"), (blanked_later, " \t ")):
        with w.db.transaction() as unit:
            w.documents.write_fields(
                unit, campaign, document, fields={"attitude": head}, author=Author.GM, base_write_revision=None
            )
        with w.db.transaction() as unit:
            record = w.documents.get(unit, campaign, document)
        assert record is not None and record.data["attitude"] == head, "the head is an open version 2"

    before = _state(s, campaign, session.id)
    with pytest.raises(MaskRefused) as caught:
        _confirm(s, campaign, session.id, filled_later, mask=("attitude",), version=1)
    assert caught.value.keys == ("attitude",)
    assert _state(s, campaign, session.id) == before

    live = _entry(_confirm(s, campaign, session.id, blanked_later, mask=("attitude",), version=1).picture, None).live
    assert live is not None and (live.document_id, live.version, live.mask) == (blanked_later, 1, ("attitude",))


def test_an_open_or_missing_version_and_an_archived_document_are_refused(served: Served) -> None:
    """T-B5 (ED-9). Only a sealed version of a document that is not archived is
    displayable; the refusal names the version or the document."""
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    with w.db.transaction() as unit:
        open_one = str(
            w.documents.create(
                unit, campaign, doc_type=DocumentTypeId.NPC, type_version=1, data=dict(AN_NPC), author=Author.GM
            ).id
        )
    archived = _document(w, campaign)
    with w.db.transaction() as unit:
        w.documents.set_archived(unit, campaign, archived, archived=True)
    before = _state(s, campaign, session.id)
    for version in (1, 9, 0):
        with pytest.raises(VersionNotDisplayable):
            _confirm(s, campaign, session.id, open_one, version=version)
    with pytest.raises(DocumentNotDisplayable):
        _confirm(s, campaign, session.id, archived)
    assert _state(s, campaign, session.id) == before

    with w.db.transaction() as unit:
        w.documents.seal(unit, campaign, open_one)
    assert _confirm(s, campaign, session.id, open_one).picture.entries[0].live.version == 1, "positive control"


def test_a_removed_foreign_or_unknown_participant_is_one_refusal(served: Served) -> None:
    """T-B6 (RD-4, critic 17, O-2). A removed seat, another campaign's seat, an
    id that names nobody, and a list holding any of them: one refusal, nothing
    written. An open seat is an audience — its copy is held — and any type may
    go to a participant."""
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    open_seat = _seat(w, campaign, "open", w.players[0])
    removed = _seat(w, campaign, "removed", w.players[1])
    foreign = _seat(w, _campaign(w, owner=w.other_owner), "open", w.players[2])
    unknown = "prt_" + "z" * 22
    document = _document(w, campaign)
    before = _state(s, campaign, session.id)
    answers = set()
    for named in ({removed}, {foreign}, {unknown}, {open_seat, removed}):
        with pytest.raises(AudienceRefused) as caught:
            _confirm(s, campaign, session.id, document, ParticipantsAudience(frozenset(named)))
        answers.add(_refusal(caught))
    assert len(answers) == 1 and _state(s, campaign, session.id) == before

    shown = _confirm(s, campaign, session.id, document, ParticipantsAudience(frozenset({open_seat})))
    assert _entry(shown.picture, open_seat).live.held is True


def test_everyone_seated_expands_to_confirmed_active_seats_only(served: Served) -> None:
    """T-B7 (TA-5, ID-12). *Everyone seated* is the accepted, GM-confirmed and
    not removed seats at the moment of the Confirm, expanded by the server:
    none is a refusal; a seat confirmed later does not join the display."""
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    document = _document(w, campaign)
    with pytest.raises(AudienceRefused):
        _confirm(s, campaign, session.id, document, EveryoneSeated())
    accepted = _seat(w, campaign, "accepted", w.players[2])
    for state, player in (("offered", w.players[3]), ("open", w.players[4]), ("removed", w.players[5])):
        _seat(w, campaign, state, player)
    with pytest.raises(AudienceRefused):
        _confirm(s, campaign, session.id, document, EveryoneSeated())
    assert _slots(w, session.id) == {}

    a = _seat(w, campaign, "confirmed", w.players[0])
    b = _seat(w, campaign, "confirmed", w.players[1])
    _confirm(s, campaign, session.id, document, EveryoneSeated())
    [live] = _live(w, session.id)
    assert _shown(w, session.id) == {a: live.id, b: live.id}
    assert _reveal_rows(s, campaign)[-1].detail["participant_ids"] == sorted([a, b])

    with w.db.transaction() as unit:
        w.participants.confirm(unit, campaign, accepted)
    assert set(_shown(w, session.id)) == {a, b}, "a seat confirmed later does not join a display already made"


def test_a_confirm_advances_the_epoch_once_and_never_authz_revision(served: Served) -> None:
    """T-B8 (RQ-10, I-10; fg7k M2). Three recipients, one Confirm: the epoch
    moves by one, the audio epoch and the authorisation revision not at all,
    and no job is enqueued."""
    s, w = served, served.w
    campaign, session, (a, b, c) = _stage(w)
    document = _document(w, campaign)
    epoch, revision, queued = _epoch(w, session.id), _revision(w, campaign), _queued(s)
    audio = _session_row(w, session.id).audio_epoch
    _confirm(s, campaign, session.id, document, ParticipantsAudience(frozenset({a, b, c})))
    assert (_epoch(w, session.id), _revision(w, campaign), _queued(s)) == (epoch + 1, revision, queued)
    assert _seqs(w, session.id) == {a: 1, b: 1, c: 1}
    assert _session_row(w, session.id).audio_epoch == audio, "a Confirm never narrows table audio"


def test_a_copy_to_an_unconfirmed_seat_is_held_and_delivered_by_the_confirmation_alone(served: Served) -> None:
    """T-B9 (SEC-41, SEC-50(5), D-12, ID-11). A copy for an accepted seat the GM
    has not confirmed is written and held: the GM sees it waiting, the player's
    table read does not return it. Confirming the seat delivers it on the next
    read, with no reveal write in between."""
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    player = w.players[0]
    seat = _seat(w, campaign, "accepted", player)
    document = _document(w, campaign)
    shown = _confirm(s, campaign, session.id, document, ParticipantsAudience(frozenset({seat})))
    assert _entry(shown.picture, seat).live.held is True
    with w.db.transaction() as unit:
        view = w.reveals.view_for_account(unit, campaign, user_id=player, now=_now())
    assert view is not None and (view.own_slot, view.mine) == (False, None)

    written = (_seqs(w, session.id), [d.id for d in _live(w, session.id)], len(_reveal_rows(s, campaign)))
    confirm_seat(s.db, s.stores, campaign_id=campaign, participant_id=seat, owner_id=w.owner, now=_now())
    with w.db.transaction() as unit:
        view = w.reveals.view_for_account(unit, campaign, user_id=player, now=_now())
    assert view is not None and view.own_slot and view.mine is not None and view.mine.document_id == document
    assert _entry(s.reveals.picture(campaign, owner_id=w.owner, now=_now()), seat).live.held is False
    assert (_seqs(w, session.id), [d.id for d in _live(w, session.id)], len(_reveal_rows(s, campaign))) == written


# ── T-B10: a Stop ────────────────────────────────────────────────────────────


def test_a_stop_clears_every_copy_advances_the_epoch_on_an_empty_session_and_equals_narrow(served: Served) -> None:
    """T-B10 (ID-13, REVEAL-22, X-3). A Stop of one document takes every copy
    of it and a Stop-all every slot, leaving exactly the state `narrow` with
    the same scope leaves on an identical table. A Stop on a session showing
    nothing still advances the epoch and writes its row; a campaign with no
    live session answers None and writes nothing; an archived campaign's Stop
    is not refused. No Stop moves the audio epoch (fg7k M2)."""
    s, w = served, served.w

    def build(owner: int, players: tuple[int, ...]) -> tuple[str, TableSession, list[str], dict[str, str]]:
        campaign = _campaign(w, owner=owner)
        seats = [_seat(w, campaign, "open", p) for p in players]
        session = _session(w, campaign, owner=owner)
        x, y, z = (_document(w, campaign) for _ in range(3))
        commands = {x: _command(), y: _command(), z: _command()}
        _write(w, campaign, session.id, x, _parts(*seats[:2]), command=commands[x])
        _write(w, campaign, session.id, y, TableTarget(), command=commands[y])
        _write(w, campaign, session.id, z, _parts(seats[2]), command=commands[z])
        return campaign, session, seats, commands

    def picture_of(built: tuple[str, TableSession, list[str], dict[str, str]]) -> tuple[Any, ...]:
        _, session, seats, commands = built
        docs = list(commands)
        live = {d.id: docs.index(d.document_id) for d in _live(w, session.id)}
        slots = {
            (None if pid is None else seats.index(pid)): (live.get(row.disclosure_id or ""), row.seq)
            for pid, row in _slots(w, session.id).items()
        }
        ended = [_by_command(w, session.id, commands[d]).ended_reason for d in docs]
        return slots, ended, _epoch(w, session.id)

    stopped = build(w.owner, w.players[:3])
    narrowed = build(w.other_owner, w.players[3:6])
    assert picture_of(stopped) == picture_of(narrowed)

    x_stopped, x_narrowed = list(stopped[3])[0], list(narrowed[3])[0]
    audio = _session_row(w, stopped[1].id).audio_epoch
    _stop(s, stopped[0], StopDocument(x_stopped))
    with w.db.transaction() as unit:
        w.sessions.narrow(
            unit, narrowed[0], narrowed[1].id, clears=DocumentCopies(x_narrowed, EndReason.GM_STOP), now=_now()
        )
    assert picture_of(stopped) == picture_of(narrowed)
    assert picture_of(stopped)[1] == [EndReason.GM_STOP, None, None], "every copy of the one document"

    _stop(s, stopped[0], StopAll())
    with w.db.transaction() as unit:
        w.sessions.narrow(unit, narrowed[0], narrowed[1].id, clears=EverySlot(EndReason.STOP_ALL), now=_now())
    assert picture_of(stopped) == picture_of(narrowed)
    assert picture_of(stopped)[1] == [EndReason.GM_STOP, EndReason.STOP_ALL, EndReason.STOP_ALL]

    campaign, session = stopped[0], stopped[1]
    epoch, rows = _epoch(w, session.id), len(_reveal_rows(s, campaign))
    answer = _stop(s, campaign, StopAll())
    assert answer is not None and answer.reveal_epoch == epoch + 1, "an empty session's epoch still advances"
    assert len(_reveal_rows(s, campaign)) == rows + 1
    assert _session_row(w, session.id).audio_epoch == audio, "three Stops, and table audio never narrowed"

    archive(s.db, s.stores, campaign_id=campaign, owner_id=w.owner, now=_now())
    epoch = _epoch(w, session.id)
    assert _stop(s, campaign, StopAll()) is not None and _epoch(w, session.id) == epoch + 1, "never refused"

    quiet = _campaign(w)
    assert _stop(s, quiet, StopAll()) is None and _reveal_rows(s, quiet) == []


# ── T-B11: RQ-7, every shipped narrowing clears what it invalidates ──────────


NARROWINGS = [
    "end",
    "expiry_by_the_job",
    "expiry_found_by_end",
    "expiry_finalised_by_start",
    "rotate",
    "remove",
    "archive_step_one",
    "archive_step_two",
    "narrow_by_default",
]


@pytest.mark.parametrize("narrowing", NARROWINGS)
def test_every_shipped_narrowing_clears_exactly_what_it_invalidates(served: Served, narrowing: str) -> None:
    """T-B11 (RQ-7, RD-3, A-20, critic 9). With the table showing one document
    and two members holding copies of another, each narrowing already shipped
    clears — through the production fill — exactly the displays it makes
    invalid, with its reason and its request's clock: End, expiry (by each of
    its three paths), Rotate, both archive steps and a scope-less `narrow`
    clear every slot; Remove clears that member's copy alone. The epoch moves
    by one, no reveal audit row is written, and a revocation takes no campaign
    lock."""
    s, w = served, served.w
    overdue = narrowing.startswith("expiry")
    campaign = _campaign(w)
    a, b = (_seat(w, campaign, "open", w.players[n]) for n in range(2))
    session = _session(w, campaign, hours=1, started_ago_h=2) if overdue else _session(w, campaign)
    x, y = _document(w, campaign), _document(w, campaign)
    cx, cy = _command(), _command()
    _write(w, campaign, session.id, x, TableTarget(), command=cx)
    _write(w, campaign, session.id, y, _parts(a, b), command=cy)
    epoch, marks, now = _epoch(w, session.id), len(s.db.units), _now()

    reasons = {
        "end": EndReason.GM_END,
        "rotate": EndReason.LINK_ROTATED,
        "archive_step_one": EndReason.CAMPAIGN_ARCHIVED,
        "archive_step_two": EndReason.CAMPAIGN_ARCHIVED,
        "narrow_by_default": EndReason.NARROWED,
    }
    if narrowing == "end" or narrowing == "expiry_found_by_end":
        s.lifecycle.end(w.owner, campaign, session.id, now=now)
    elif narrowing == "expiry_by_the_job":
        s.lifecycle.expire(campaign, session.id, now=now)
    elif narrowing == "expiry_finalised_by_start":
        s.lifecycle.start(w.owner, campaign, command_id=_command(), now=now)
    elif narrowing == "rotate":
        s.lifecycle.rotate(w.owner, campaign, session.id, command_id=_command(), now=now)
    elif narrowing == "remove":
        remove_seat(s.db, s.stores, s.jobs, campaign_id=campaign, participant_id=a, owner_id=w.owner, now=now)
    elif narrowing == "archive_step_one":
        archive_step_one(s.db, s.stores, campaign_id=campaign, owner_id=w.owner, now=now)
    elif narrowing == "archive_step_two":
        archive_step_two(s.db, s.stores, campaign_id=campaign, owner_id=w.owner, now=now)
    else:
        with s.db.transaction() as unit:
            w.sessions.narrow(unit, campaign, session.id)

    table, copies = _by_command(w, session.id, cx), _by_command(w, session.id, cy)
    if narrowing == "remove":
        assert _shown(w, session.id) == {None: table.id, a: None, b: copies.id}, "that member's copy, and only it"
        assert _seqs(w, session.id) == {None: 1, a: 2, b: 1}
        assert table.is_live and copies.is_live, "the disclosure stays live while any copy remains"
    else:
        reason = EndReason.EXPIRED if overdue else reasons[narrowing]
        assert _shown(w, session.id) == {None: None, a: None, b: None}
        assert _seqs(w, session.id) == {None: 2, a: 2, b: 2}
        assert (table.ended_reason, copies.ended_reason) == (reason, reason)
        if narrowing != "narrow_by_default":
            assert table.ended_at == copies.ended_at == now, "the request's clock, not a second reading"
    assert _epoch(w, session.id) == epoch + 1
    assert _reveal_rows(s, campaign) == [], "a narrowing writes its own row, never a reveal row"
    if narrowing == "expiry_finalised_by_start":
        assert s.db.units[marks].campaign_locks == [], "Start's finalising transaction takes no campaign lock"
    elif narrowing != "archive_step_two":
        assert s.db.locks(marks) == [], "a revocation never asks for the campaign lock"


def _group_narrowings(s: Served) -> tuple[EligibilityMutations, EligibilityStores]:
    """`1ir.2.1`'s mutations over the served world's filled session store. The
    change recorder and the table namespace do nothing here: the subject is
    the reveal slots."""
    w = s.w
    eligibility: Any = (
        InMemoryEligibilityStore(w.db, documents=w.documents) if w.kind == "fake" else PostgresEligibilityStore()
    )

    def nothing(*_: Any) -> None:
        return None

    mutations = EligibilityMutations(
        s.db,
        eligibility=eligibility,
        documents=w.documents,
        sessions=w.sessions,
        table_namespace=nothing,
        record=nothing,
    )
    return mutations, EligibilityStores(eligibility, w.documents, w.sessions, nothing, nothing)


@pytest.mark.parametrize("narrowing", ["remove_member", "remove_group", "step_one_alone", "step_two_alone"])
def test_a_group_narrowing_clears_every_slot_in_each_step_at_the_requests_clock(
    served: Served, narrowing: str
) -> None:
    """T-B11 for `1ir.2.1`'s narrowings (RQ-7, RQ-5; the lead ruling on
    `1kg.7.1`). A disclosure does not record the group it went to yet, so
    removing a member or a whole group clears every slot, as `narrowed`,
    stamped with the one clock the request read: step 1 takes no campaign lock
    and step 2 the exclusive one, each advancing the epoch once, and no reveal
    audit row is written. Each step clears on its own: step 1 before step 2
    has run, and step 2 for the session a start between the steps leaves
    showing."""
    s, w = served, served.w
    mutations, stores = _group_narrowings(s)
    campaign = _campaign(w)
    a, b = (_seat(w, campaign, "open", w.players[n]) for n in range(2))
    group = mutations.create_group(campaign, name="Scouts")
    assert all(mutations.add_member(campaign, group.id, seat).changed for seat in (a, b))
    session = _session(w, campaign)
    x, y = _document(w, campaign), _document(w, campaign)
    cx, cy = _command(), _command()
    _write(w, campaign, session.id, x, TableTarget(), command=cx)
    _write(w, campaign, session.id, y, _parts(a, b), command=cy)
    epoch, marks = _epoch(w, session.id), len(s.db.units)
    #: A moment no reading of the clock during the test can be.
    now = _now() + timedelta(minutes=5)

    if narrowing == "remove_member":
        assert mutations.remove_member(campaign, group.id, a, now=now).changed
    elif narrowing == "remove_group":
        assert mutations.remove_group(campaign, group.id, now=now).changed
    elif narrowing == "step_one_alone":
        with s.db.transaction() as unit:
            narrow_step_one(unit, stores, campaign, now=now)
    else:
        with s.db.transaction() as unit:
            assert remove_member_step_two(unit, stores, campaign, group.id, a, now=now).changed

    table, copies = _by_command(w, session.id, cx), _by_command(w, session.id, cy)
    assert _shown(w, session.id) == {None: None, a: None, b: None}, "every slot: no display outlives the change"
    assert _seqs(w, session.id) == {None: 2, a: 2, b: 2}
    assert (table.ended_reason, copies.ended_reason) == (EndReason.NARROWED, EndReason.NARROWED)
    assert table.ended_at == copies.ended_at == now, "the request's clock, not a second reading"
    assert _reveal_rows(s, campaign) == [], "a narrowing writes its own row, never a reveal row"
    locks = [unit.campaign_locks for unit in s.db.units[marks:]]
    if narrowing == "step_one_alone":
        assert (_epoch(w, session.id), locks) == (epoch + 1, [[]]), "step 1 never asks for the campaign lock"
    elif narrowing == "step_two_alone":
        assert (_epoch(w, session.id), locks) == (epoch + 1, [[(campaign, "exclusive")]])
    else:
        assert _epoch(w, session.id) == epoch + 2, "step 1 and step 2 each advance it once"
        assert locks == [[], [(campaign, "exclusive")]], "step 1 unlocked, then step 2 under the exclusive lock"


DOCUMENT_NARROWINGS = [
    "archive",
    "archive_step_one_alone",
    "archive_step_two_alone",
    "delete",
    "delete_step_one_alone",
    "delete_step_two_alone",
]


def _document_lifecycle_stores(s: Served) -> document_lifecycle_api.LifecycleStores:
    """The stores `1kg.5.2`'s routes compose: in PostgreSQL the production
    dependency itself, fill and all; in the twin the served world's, whose
    session store has the same fill."""
    w = s.w
    if w.kind == "postgres":
        return document_lifecycle_api.get_lifecycle_stores()
    return document_lifecycle_api.LifecycleStores(w.campaigns, w.documents, w.sessions, s.audit)


@pytest.mark.parametrize("narrowing", DOCUMENT_NARROWINGS)
def test_a_document_archive_or_delete_clears_that_documents_copies_at_the_requests_clock(
    served: Served, narrowing: str
) -> None:
    """T-B11 for `1kg.5.2`'s narrowings (RQ-7; the brief's ID-5 and T-B12, and
    its critic 8 for a bead that lands first). Archiving or deleting a document
    clears every copy of that document, as `document_archived` or
    `document_deleted`, stamped with the one clock the request read, and leaves
    every other display up: step 1 takes no campaign lock and step 2 the
    exclusive one, each advancing the epoch once, and no reveal audit row is
    written. A delete narrows **before** it deletes, so a document still shown
    (archived under its display by the store's primitive, as nothing else can
    leave one) is deleted rather than refused by the slot -> disclosure key,
    and in PostgreSQL its ended disclosure then goes with it by the cascade.
    Each step clears on its own: step 1 before step 2 has run, and step 2 for
    the session a start between the steps leaves showing."""
    s, w = served, served.w
    stores = _document_lifecycle_stores(s)
    campaign = _campaign(w)
    a, b = (_seat(w, campaign, "open", w.players[n]) for n in range(2))
    session = _session(w, campaign)
    x, y = _document(w, campaign), _document(w, campaign)
    cx, cy = _command(), _command()
    _write(w, campaign, session.id, x, _parts(a, b), command=cx)
    _write(w, campaign, session.id, y, TableTarget(), command=cy)
    deleting = narrowing.startswith("delete")
    if deleting:
        with w.db.transaction() as unit:
            assert w.documents.set_archived(unit, campaign, x, archived=True, now=_now())
    epoch, marks = _epoch(w, session.id), len(s.db.units)
    #: A moment no reading of the clock during the test can be.
    now = _now() + timedelta(minutes=5)

    if narrowing == "archive":
        document_lifecycle_api.archive_document(
            s.db, stores, campaign_id=campaign, document_id=x, owner_id=w.owner, now=now
        )
    elif narrowing == "archive_step_one_alone":
        document_lifecycle_api.archive_step_one(
            s.db, stores, campaign_id=campaign, document_id=x, owner_id=w.owner, now=now
        )
    elif narrowing == "archive_step_two_alone":
        document_lifecycle_api.archive_step_two(
            s.db, stores, campaign_id=campaign, document_id=x, owner_id=w.owner, now=now
        )
    elif narrowing == "delete":
        document_lifecycle_api.delete_document(
            s.db, stores, campaign_id=campaign, document_id=x, owner_id=w.owner, now=now
        )
    elif narrowing == "delete_step_one_alone":
        document_lifecycle_api.delete_step_one(
            s.db, stores, campaign_id=campaign, document_id=x, owner_id=w.owner, now=now
        )
    else:
        document_lifecycle_api.delete_step_two(
            s.db, stores, campaign_id=campaign, document_id=x, owner_id=w.owner, now=now
        )

    table = _by_command(w, session.id, cy)
    assert _shown(w, session.id) == {None: table.id, a: None, b: None}, "that document's copies, and only them"
    assert _seqs(w, session.id) == {None: 1, a: 2, b: 2}
    assert {p: _slots(w, session.id)[p].updated_at for p in (a, b)} == {a: now, b: now}, "cleared at the one clock"
    assert [d.id for d in _live(w, session.id)] == [table.id], "another document's display stays up"
    gone = narrowing in ("delete", "delete_step_two_alone")
    with w.db.transaction() as unit:
        ended = w.reveals.by_command(unit, session.id, cx)
        kept = w.documents.get(unit, campaign, x)
    assert (kept is None) == gone, "a delete deletes; nothing else does"
    if gone and w.kind == "postgres":
        assert ended is None, "the ended disclosure went with the document, by the cascade"
    else:
        reason = EndReason.DOCUMENT_DELETED if deleting else EndReason.DOCUMENT_ARCHIVED
        assert ended is not None and (ended.ended_reason, ended.ended_at) == (reason, now), "the request's clock"
    assert _reveal_rows(s, campaign) == [], "a narrowing writes its own row, never a reveal row"
    locks = [unit.campaign_locks for unit in s.db.units[marks:]]
    if narrowing.endswith("step_one_alone"):
        assert (_epoch(w, session.id), locks) == (epoch + 1, [[]]), "step 1 never asks for the campaign lock"
    elif narrowing.endswith("step_two_alone"):
        assert (_epoch(w, session.id), locks) == (epoch + 1, [[(campaign, "exclusive")]])
    else:
        assert _epoch(w, session.id) == epoch + 2, "step 1 and step 2 each advance it once"
        assert locks == [[], [(campaign, "exclusive")]], "step 1 unlocked, then step 2 under the exclusive lock"


def test_the_scopes_later_beads_will_use(served: Served) -> None:
    """T-B12 (ID-5). Document archive, delete and unlink will each narrow with
    `DocumentCopies` — every copy of that document, table or member, and
    nothing else; switching table audio off will narrow with `NoSlot`, which
    advances the epoch (and the audio epoch when asked) and clears nothing."""
    w = served.w
    campaign, session, (a, b) = _stage(w, seats=2)
    x, y = _document(w, campaign), _document(w, campaign)
    cy = _command()
    _write(w, campaign, session.id, y, _parts(a, b), command=cy)

    def narrow(scope: Any, *, audio: bool = False) -> TableSession:
        with w.db.transaction() as unit:
            narrowed = w.sessions.narrow(unit, campaign, session.id, clears=scope, audio=audio, now=_now())
        assert narrowed is not None
        return narrowed

    for reason in (EndReason.DOCUMENT_ARCHIVED, EndReason.DOCUMENT_DELETED, EndReason.CHARACTER_UNLINKED):
        command = _command()
        _write(w, campaign, session.id, x, TableTarget(), command=command)
        seqs = _seqs(w, session.id)
        narrow(DocumentCopies(x, reason))
        assert _by_command(w, session.id, command).ended_reason == reason
        assert _seqs(w, session.id) == {**seqs, None: seqs[None] + 1}, "the other document's copies untouched"
        assert _by_command(w, session.id, cy).is_live

    narrow(DocumentCopies(y, EndReason.DOCUMENT_ARCHIVED))
    assert _shown(w, session.id) == {None: None, a: None, b: None}
    assert _by_command(w, session.id, cy).ended_reason == EndReason.DOCUMENT_ARCHIVED

    _write(w, campaign, session.id, x, TableTarget())
    seqs, shown, before = _seqs(w, session.id), _shown(w, session.id), _session_row(w, session.id)
    after = narrow(NoSlot(), audio=True)
    assert (_seqs(w, session.id), _shown(w, session.id)) == (seqs, shown), "NoSlot clears no reveal slot"
    assert (after.reveal_epoch, after.audio_epoch) == (before.reveal_epoch + 1, before.audio_epoch + 1)


def _session_row(w: World, session: str) -> TableSession:
    with w.db.transaction() as unit:
        found = w.sessions.get(unit, session)
    assert found is not None
    return found


# ── T-B13: the audit rows ────────────────────────────────────────────────────


def test_the_confirm_audit_rows(served: Served) -> None:
    """T-B13 (ED-18(a), SEC-38, RD-1, critic 3 and 12). Exactly the declared
    fields, by value: a display; an update is one row; a move is a stop of every
    old copy then a display; a replacement names the copies it took; a Stop-all
    of three disclosures is three rows of one command and one epoch; a Stop that
    took nothing is one row naming its document (or none for a Stop-all); and an
    End or a Remove writes no reveal row."""
    s, w = served, served.w
    campaign, session, (a, b, c) = _stage(w)
    x, y, z = (_document(w, campaign) for _ in range(3))
    owner, sid = str(w.owner), session.id

    def detail(command: str, document: str | None, epoch: int, *, audience: str | None = "table",
               mask: list[str] | None = None, pids: list[str] | None = None, version: int | None = 1) -> dict[str, Any]:
        return {
            "session_id": sid,
            "command_id": command,
            "document_id": document,
            "version": version,
            "mask": ["name"] if mask is None and audience is not None else mask,
            "audience": audience,
            "participant_ids": pids,
            "reveal_epoch": epoch,
        }

    def rows_since(n: int) -> list[tuple[Any, ...]]:
        return [
            (str(e.action), e.reason_code, dict(e.detail))
            for e in _reveal_rows(s, campaign)[n:]
        ]

    cx = _command()
    _confirm(s, campaign, sid, x, mask=("voice", "name"), command=cx)
    [row] = _reveal_rows(s, campaign)
    assert (row.actor_kind, row.actor_ref, row.object_kind, row.object_ref, row.decision, row.authz_revision) == (
        "gm",
        owner,
        "table_session",
        sid,
        "allowed",
        None,
    )
    assert rows_since(0) == [("reveal.displayed", None, detail(cx, x, 1, mask=["name", "voice"]))]

    cu = _command()
    _confirm(s, campaign, sid, x, command=cu)
    assert rows_since(1) == [("reveal.updated", None, detail(cu, x, 2))], "an update is exactly one row"

    cy = _command()
    _confirm(s, campaign, sid, y, ParticipantsAudience(frozenset({a, b})), command=cy)
    assert rows_since(2) == [
        ("reveal.displayed", None, detail(cy, y, 3, audience="participant", pids=sorted([a, b])))
    ]

    cm = _command()
    _confirm(s, campaign, sid, y, ParticipantsAudience(frozenset({b, c})), command=cm)
    assert rows_since(3) == [
        ("reveal.stopped", "moved", detail(cm, y, 4, audience="participant", pids=sorted([a, b]))),
        ("reveal.displayed", None, detail(cm, y, 4, audience="participant", pids=sorted([b, c]))),
    ], "a move stops every old copy, those kept included, then displays"

    cr = _command()
    _confirm(s, campaign, sid, z, ParticipantsAudience(frozenset({c})), command=cr)
    assert rows_since(5) == [
        ("reveal.stopped", "replaced", detail(cr, y, 5, audience="participant", pids=[c])),
        ("reveal.displayed", None, detail(cr, z, 5, audience="participant", pids=[c])),
    ]

    cs = _command()
    _stop(s, campaign, StopAll(), command=cs)
    stops = rows_since(7)
    assert len(stops) == 3, "one row per disclosure a Stop-all took copies from"
    assert {(action, reason, d["command_id"], d["reveal_epoch"]) for action, reason, d in stops} == {
        ("reveal.stopped", "stop_all", cs, 6)
    }
    assert sorted(d["document_id"] for _, _, d in stops) == sorted([x, y, z])

    cn, ca = _command(), _command()
    _stop(s, campaign, StopDocument(x), command=cn)
    _stop(s, campaign, StopAll(), command=ca)
    assert rows_since(10) == [
        ("reveal.stopped", "gm_stop", detail(cn, x, 7, audience=None, version=None)),
        ("reveal.stopped", "stop_all", detail(ca, None, 8, audience=None, version=None)),
    ]

    _confirm(s, campaign, sid, x, ParticipantsAudience(frozenset({a})))
    remove_seat(s.db, s.stores, s.jobs, campaign_id=campaign, participant_id=a, owner_id=w.owner, now=_now())
    _confirm(s, campaign, sid, y)
    s.lifecycle.end(w.owner, campaign, sid, now=_now())
    assert [action for action, _, _ in rows_since(12)] == ["reveal.displayed", "reveal.displayed"], (
        "neither the Remove nor the End wrote a reveal row"
    )


# ── T-B15, T-B17, T-B19, T-B20 ───────────────────────────────────────────────


def test_the_reconciliation_clears_only_stale_copies(served: Served) -> None:
    """T-B15 (ID-19, RC-6). A live display left in a dead session and a copy
    left for a seat removed without a narrowing are cleared as `reconciled`; a
    valid copy is untouched; the revision advances once and each session
    narrowed advances its epoch once. A campaign that has gone is a no-op."""
    s, w = served, served.w
    campaign = _campaign(w)
    a, b = (_seat(w, campaign, "open", w.players[n]) for n in range(2))
    z, x, y = (_document(w, campaign) for _ in range(3))
    dead, cz = _left_live_in_a_dead_session(w, campaign, z)
    live = _session(w, campaign)
    cx, cy = _command(), _command()
    _write(w, campaign, live.id, x, _parts(a, b), command=cx)
    _write(w, campaign, live.id, y, TableTarget(), command=cy)
    with w.db.transaction() as unit:
        w.participants.remove(unit, campaign, a)
    revision, dead_epoch, live_epoch = _revision(w, campaign), _epoch(w, dead.id), _epoch(w, live.id)
    now = _now()
    fill = make_reconcile_slots(w.sessions, w.reveals, clock=lambda: now)

    assert reconcile(s.db, campaign, slots=fill) is True
    assert _shown(w, dead.id) == {None: None}
    assert _by_command(w, dead.id, cz).ended_reason == EndReason.RECONCILED
    assert _shown(w, live.id) == {None: _by_command(w, live.id, cy).id, a: None, b: _by_command(w, live.id, cx).id}
    assert _by_command(w, live.id, cx).is_live and _by_command(w, live.id, cy).is_live
    assert (_revision(w, campaign), _epoch(w, dead.id), _epoch(w, live.id)) == (
        revision + 1,
        dead_epoch + 1,
        live_epoch + 1,
    )

    assert reconcile(s.db, campaign, slots=fill) is True
    assert (_epoch(w, dead.id), _epoch(w, live.id)) == (dead_epoch + 1, live_epoch + 1), "nothing stale is left"
    assert reconcile(s.db, "cmp_" + "z" * 22, slots=fill) is False


def test_a_stale_live_disclosure_in_a_dead_session_is_ended_by_the_next_confirm(served: Served) -> None:
    """T-B17 (ID-15, critic 3). The Confirm ends the display left behind as
    `reconciled`, rather than tripping the one-live index, and is audited as
    one `reveal.displayed` — a reconciliation is not a GM's stop."""
    s, w = served, served.w
    campaign = _campaign(w)
    document = _document(w, campaign)
    dead, left = _left_live_in_a_dead_session(w, campaign, document)
    session = _session(w, campaign)
    _confirm(s, campaign, session.id, document)
    assert _by_command(w, dead.id, left).ended_reason == EndReason.RECONCILED
    assert _shown(w, dead.id) == {None: None}
    assert [str(e.action) for e in _reveal_rows(s, campaign)] == ["reveal.displayed"]


def test_a_confirm_after_the_campaign_is_archived_is_a_conflict(served: Served) -> None:
    """T-B19 (critic 1, RQ-4, RC-2). Archive narrows the live session and does
    not end it, so a Confirm carrying the fresh epoch passes every earlier
    step; under the row it finds the campaign archived and is refused, having
    written nothing."""
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    document = _document(w, campaign)
    archive(s.db, s.stores, campaign_id=campaign, owner_id=w.owner, now=_now())
    before = _state(s, campaign, session.id)
    with pytest.raises(RevealConflict):
        _confirm(s, campaign, session.id, document)
    assert _state(s, campaign, session.id) == before


def test_document_writes_display_nothing(served: Served) -> None:
    """T-B20 (X-2, REVEAL-4, critic 11). Creating, editing, sealing and
    restoring a document while a session is live write no reveal row and move
    no epoch: only a GM's Confirm displays anything."""
    w = served.w
    campaign, session, _ = _stage(w, seats=0)
    epoch = _epoch(w, session.id)
    with w.db.transaction() as unit:
        made = w.documents.create(
            unit, campaign, doc_type=DocumentTypeId.NPC, type_version=1, data=dict(AN_NPC), author=Author.GM
        )
    with w.db.transaction() as unit:
        w.documents.write_fields(
            unit, campaign, made.id, fields={"voice": "hoarse"}, author=Author.GM, base_write_revision=None
        )
    with w.db.transaction() as unit:
        w.documents.seal(unit, campaign, made.id)
    with w.db.transaction() as unit:
        w.documents.restore(unit, campaign, made.id, version_number=1)
    assert (_slots(w, session.id), _live(w, session.id), _epoch(w, session.id)) == ({}, [], epoch)


# ══ PR-B: PostgreSQL races ═══════════════════════════════════════════════════

#: A database whose waits outlast the races below: the campaign lock's wait is
#: bounded at the maximum `CampaignLockSettings` allows, and a transaction at
#: thirty seconds.
PATIENT_LOCKS = CampaignLockSettings(lock_timeout_s=4, transaction_timeout_s=30)


def _patient(target: str | None) -> Database:
    assert target is not None
    return Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=10), PATIENT_LOCKS)


class _Paused:
    """A database whose transactions do their work and then hold everything they
    locked until released — the first of two racers, so that the second can be
    shown waiting on it (or not)."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.holding = threading.Event()
        self.release = threading.Event()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._db.transaction() as unit:
            yield unit
            self.holding.set()
            assert self.release.wait(PATIENCE), "the paused transaction was never released"


def _waits_on(dsn: str, relation: str) -> bool:
    """Whether a backend of this database is waiting on a lock in a statement
    that names `relation` — the server's own account of **which** lock."""
    deadline = time.monotonic() + PATIENCE
    with connect(dsn) as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND query LIKE %s",
                (f"%{relation}%",),
            ).fetchone()[0]
            if waiting:
                return True
            time.sleep(0.05)
    return False


def _race(
    dsn: str | None,
    paused: _Paused,
    first: Callable[[], object],
    second: Callable[[], object],
    *,
    on: str = "campaign.table_sessions",
) -> tuple[object, object]:
    """`first` over the paused database, then `second` while it holds: the
    server must show `second` waiting, in a statement naming `on` (the session
    row unless said otherwise); then `first` commits and both finish."""
    assert dsn is not None
    one, first_out = _in_background(first)
    assert paused.holding.wait(PATIENCE), first_out
    two, second_out = _in_background(second)
    try:
        assert _waits_on(dsn, on), f"the second racer never waited on {on}"
    finally:
        paused.release.set()
        one.join(PATIENCE)
        two.join(PATIENCE)
    assert first_out and second_out, "a racer did not finish"
    return first_out[0], second_out[0]


def _alongside(paused: _Paused, first: Callable[[], object], second: Callable[[], object]) -> tuple[object, object]:
    """`first` holds; `second` must finish while it still holds."""
    one, first_out = _in_background(first)
    assert paused.holding.wait(PATIENCE), first_out
    try:
        two, second_out = _in_background(second)
        two.join(8)
        assert not two.is_alive() and second_out, "the second racer waited for the first"
    finally:
        paused.release.set()
        one.join(PATIENCE)
    assert first_out
    return first_out[0], second_out[0]


def _deadlocks(dsn: str | None) -> int:
    assert dsn is not None
    with connect(dsn) as conn:
        conn.execute("SELECT pg_stat_force_next_flush()")
        return int(
            conn.execute("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()").fetchone()[0]
        )


def _waiters(dsn: str | None) -> int:
    assert dsn is not None
    with connect(dsn) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
        )


@needs_db
def test_two_confirms_at_one_epoch_make_one_display(pgs: Served) -> None:
    """T-C1 (RC-4). Two Confirms composed at one epoch, to different audiences:
    the second waits on the session row, then finds the epoch moved and is a
    conflict. One disclosure, one `reveal.displayed`, the epoch +1."""
    s, w = pgs, pgs.w
    campaign, session, (a,) = _stage(w, seats=1)
    x, y = _document(w, campaign), _document(w, campaign)
    epoch, deadlocks = _epoch(w, session.id), _deadlocks(w.dsn)
    paused = _Paused(w.db)
    first, second = s.over(paused), s.over(_patient(w.dsn))
    one, two = _race(
        w.dsn,
        paused,
        lambda: _confirm(s, campaign, session.id, x, epoch=epoch, reveals=first),
        lambda: _confirm(s, campaign, session.id, y, ParticipantsAudience(frozenset({a})), epoch=epoch, reveals=second),
    )
    assert isinstance(one, Displayed) and isinstance(two, RevealConflict)
    assert [d.document_id for d in _live(w, session.id)] == [x] and _epoch(w, session.id) == epoch + 1
    assert [str(e.action) for e in _reveal_rows(s, campaign)] == ["reveal.displayed"]
    assert _deadlocks(w.dsn) == deadlocks


@needs_db
@pytest.mark.parametrize("order", ["stop_first", "confirm_first"])
def test_a_confirm_and_a_stop_resolve_in_either_order(pgs: Served, order: str) -> None:
    """T-C2 (RC-1, REVEAL-22). A Stop that commits first makes the waiting
    Confirm a conflict, because it advanced the epoch; a Confirm that commits
    first is cleared by the Stop that waited for it."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x, y = _document(w, campaign), _document(w, campaign)
    _confirm(s, campaign, session.id, x)
    epoch = _epoch(w, session.id)
    paused = _Paused(w.db)
    held, waiting = s.over(paused), s.over(_patient(w.dsn))
    if order == "stop_first":
        one, two = _race(
            w.dsn,
            paused,
            lambda: _stop(s, campaign, StopAll(), reveals=held),
            lambda: _confirm(s, campaign, session.id, y, epoch=epoch, reveals=waiting),
        )
        assert isinstance(one, RevealPicture) and isinstance(two, RevealConflict)
        assert _live(w, session.id) == [] and _epoch(w, session.id) == epoch + 1
    else:
        command = _command()
        one, two = _race(
            w.dsn,
            paused,
            lambda: _confirm(s, campaign, session.id, y, epoch=epoch, command=command, reveals=held),
            lambda: _stop(s, campaign, StopAll(), reveals=waiting),
        )
        assert isinstance(one, Displayed) and isinstance(two, RevealPicture)
        assert _by_command(w, session.id, command).ended_reason == EndReason.STOP_ALL
        assert _live(w, session.id) == [] and _epoch(w, session.id) == epoch + 2


@needs_db
def test_two_confirms_with_one_command_id_write_once(pgs: Served) -> None:
    """T-C3 (ID-10, the *Idempotency* row). The second of two Confirms with one
    command id waits, then finds the first's disclosure under the row and
    replays it — never a conflict, never an integrity error."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x = _document(w, campaign)
    epoch, command = _epoch(w, session.id), _command()
    paused = _Paused(w.db)
    one, two = _race(
        w.dsn,
        paused,
        lambda: _confirm(s, campaign, session.id, x, epoch=epoch, command=command, reveals=s.over(paused)),
        lambda: _confirm(s, campaign, session.id, x, epoch=epoch, command=command, reveals=s.over(_patient(w.dsn))),
    )
    assert isinstance(one, Displayed) and not one.replayed
    assert isinstance(two, Displayed) and two.replayed
    assert len(_live(w, session.id)) == 1 and len(_reveal_rows(s, campaign)) == 1


@needs_db
@pytest.mark.parametrize("narrowing", ["end", "rotate"])
@pytest.mark.parametrize("order", ["narrowing_first", "confirm_first"])
def test_a_confirm_and_an_end_or_a_rotate_resolve_in_either_order(pgs: Served, narrowing: str, order: str) -> None:
    """T-C4 (RC-5). An End or a Rotate that commits first makes the waiting
    Confirm a conflict; a Confirm that commits first is cleared by the End
    (`gm_end`) or the Rotate (`link_rotated`) that waited for it."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x = _document(w, campaign)
    epoch, command = _epoch(w, session.id), _command()
    paused = _Paused(w.db)

    def narrow(db: Any) -> Callable[[], object]:
        lifecycle = s.lifecycle_over(db)
        if narrowing == "end":
            return lambda: lifecycle.end(w.owner, campaign, session.id)
        return lambda: lifecycle.rotate(w.owner, campaign, session.id, command_id=_command())

    if order == "narrowing_first":
        _, two = _race(
            w.dsn,
            paused,
            narrow(paused),
            lambda: _confirm(s, campaign, session.id, x, epoch=epoch, reveals=s.over(_patient(w.dsn))),
        )
        assert isinstance(two, RevealConflict) and _slots(w, session.id) == {}
    else:
        one, _ = _race(
            w.dsn,
            paused,
            lambda: _confirm(s, campaign, session.id, x, epoch=epoch, command=command, reveals=s.over(paused)),
            narrow(_patient(w.dsn)),
        )
        assert isinstance(one, Displayed)
        reason = EndReason.GM_END if narrowing == "end" else EndReason.LINK_ROTATED
        assert _by_command(w, session.id, command).ended_reason == reason
        assert _shown(w, session.id) == {None: None}


@needs_db
@pytest.mark.parametrize("order", ["remove_first", "confirm_first"])
def test_a_confirm_to_a_member_and_their_removal_never_deadlock(pgs: Served, order: str) -> None:
    """T-C5 (RC-14, critic 10). With no retry to hide one, a Confirm to Ana and
    Ben and Ana's removal never deadlock (the server's own count agrees). A
    Remove first makes the Confirm a conflict; a Confirm first loses Ana's copy
    to the Remove and keeps Ben's."""
    s, w = pgs, pgs.w
    campaign, session, (ana, ben) = _stage(w, seats=2)
    x = _document(w, campaign)
    epoch, command, deadlocks = _epoch(w, session.id), _command(), _deadlocks(w.dsn)
    paused = _Paused(w.db)
    both = ParticipantsAudience(frozenset({ana, ben}))

    def remove(db: Any) -> Callable[[], object]:
        return lambda: remove_seat(
            db, s.stores, s.jobs, campaign_id=campaign, participant_id=ana, owner_id=w.owner, now=_now()
        )

    if order == "remove_first":
        _, two = _race(
            w.dsn,
            paused,
            remove(paused),
            lambda: _confirm(s, campaign, session.id, x, both, epoch=epoch, reveals=s.over(_patient(w.dsn))),
        )
        assert isinstance(two, RevealConflict) and _live(w, session.id) == []
    else:
        one, two = _race(
            w.dsn,
            paused,
            lambda: _confirm(s, campaign, session.id, x, both, epoch=epoch, command=command, reveals=s.over(paused)),
            remove(_patient(w.dsn)),
        )
        assert isinstance(one, Displayed) and not isinstance(two, BaseException)
        shown = _by_command(w, session.id, command)
        assert _shown(w, session.id) == {ana: None, ben: shown.id} and shown.is_live
    assert _deadlocks(w.dsn) == deadlocks


@needs_db
def test_a_stop_never_waits_for_the_campaign_lock(pgs: Served) -> None:
    """T-C6 (RQ-6, X-3). While another transaction holds the campaign lock
    exclusively, a Stop completes; a Confirm does not (positive control)."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x, y = _document(w, campaign), _document(w, campaign)
    _confirm(s, campaign, session.id, x)
    assert w.dsn is not None
    with _holding(w.dsn, "SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR UPDATE", (campaign,)):
        started = time.monotonic()
        assert _stop(s, campaign, StopAll()) is not None
        assert time.monotonic() - started < 1.0 and _live(w, session.id) == []
        with pytest.raises(RevealBusy):
            _confirm(s, campaign, session.id, y)


@needs_db
def test_a_confirm_waits_for_the_reconciliation_and_is_busy_when_it_waits_too_long(pgs: Served) -> None:
    """T-C7 (RQ-4). A Confirm waits for a reconciliation holding the campaign
    lock exclusively, then proceeds; with the short bound it is `RevealBusy`
    and writes nothing."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x = _document(w, campaign)
    paused = _Paused(w.db)
    fill = make_reconcile_slots(w.sessions, w.reveals, clock=_now)
    _, two = _race(
        w.dsn,
        paused,
        lambda: reconcile(paused, campaign, slots=fill),
        lambda: _confirm(s, campaign, session.id, x, reveals=s.over(_patient(w.dsn))),
        on="campaign.authz_state",
    )
    assert isinstance(two, Displayed)

    y = _document(w, campaign)
    before = _state(s, campaign, session.id)
    assert w.dsn is not None
    with _holding(w.dsn, "SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR UPDATE", (campaign,)):
        with pytest.raises(RevealBusy):
            _confirm(s, campaign, session.id, y)
    assert _state(s, campaign, session.id) == before


@needs_db
def test_a_stranger_waits_for_nothing(pgs: Served) -> None:
    """T-C8 (SEC-2, the lock-before-ownership pitfall). While a third
    connection holds the campaign lock exclusively and the session row, a
    stranger's Confirm and Stop are each the one not-found answer at once, and
    nobody waits; the owner's Confirm does wait (positive control)."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x = _document(w, campaign)
    assert w.dsn is not None
    both = (
        "WITH a AS (SELECT campaign_id FROM campaign.authz_state WHERE campaign_id = %s FOR UPDATE), "
        "t AS (SELECT id FROM campaign.table_sessions WHERE id = %s FOR NO KEY UPDATE) "
        "SELECT (SELECT count(*) FROM a), (SELECT count(*) FROM t)"
    )
    with _holding(w.dsn, both, (campaign, session.id)):
        for stranger in (
            lambda: _confirm(s, campaign, session.id, x, owner=w.other_owner),
            lambda: _stop(s, campaign, StopAll(), owner=w.other_owner),
        ):
            started = time.monotonic()
            with pytest.raises(RevealNotFound):
                stranger()
            assert time.monotonic() - started < 1.0 and _waiters(w.dsn) == 0
        owner, answer = _in_background(lambda: _confirm(s, campaign, session.id, x, reveals=s.over(_patient(w.dsn))))
        assert _someone_waits_on_a_lock(w.dsn), "positive control: the owner's Confirm waits"
    owner.join(PATIENCE)
    assert answer and isinstance(answer[0], Displayed)


@needs_db
def test_a_confirm_and_a_document_write_never_wait_for_each_other(pgs: Served) -> None:
    """T-C9 (RQ-3). A Confirm pinning the sealed version while a write updates
    and seals the next: the Confirm's key-share checks and the write's
    no-key-update locks do not conflict, in either order, and the Confirm pins
    the version it named. Positive control: a real `FOR UPDATE` on the document
    row does make a Confirm wait."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x = _document(w, campaign)

    def write(db: Any) -> Callable[[], object]:
        def run() -> object:
            with db.transaction() as unit:
                w.documents.write_fields(
                    unit,
                    campaign,
                    x,
                    fields={"voice": secrets.token_hex(4)},
                    author=Author.GM,
                    base_write_revision=None,
                )
                return w.documents.seal(unit, campaign, x)

        return run

    paused = _Paused(w.db)
    _, shown = _alongside(paused, write(paused), lambda: _confirm(s, campaign, session.id, x, version=1))
    assert isinstance(shown, Displayed) and _entry(shown.picture, None).live.version == 1

    paused = _Paused(w.db)
    confirmed, written = _alongside(
        paused, lambda: _confirm(s, campaign, session.id, x, version=1, reveals=s.over(paused)), write(w.db)
    )
    assert isinstance(confirmed, Displayed) and not isinstance(written, BaseException)

    assert w.dsn is not None
    with _holding(w.dsn, "SELECT 1 FROM campaign.documents WHERE id = %s FOR UPDATE", (x,)):
        waiter, answer = _in_background(lambda: _confirm(s, campaign, session.id, x, reveals=s.over(_patient(w.dsn))))
        assert _someone_waits_on_a_lock(w.dsn)
    waiter.join(PATIENCE)
    assert answer and isinstance(answer[0], Displayed)


@needs_db
def test_a_confirm_and_a_screen_mint_never_wait_for_each_other(pgs: Served) -> None:
    """T-C10 (RQ-3). A mint's foreign-key check takes key-share on the session
    row, which a Confirm's no-key-update hold does not conflict with, in either
    order. Positive control: a `FOR UPDATE` on the session row makes a mint
    wait."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x, y = _document(w, campaign), _document(w, campaign)

    def mint(db: Any) -> Callable[[], object]:
        def run() -> object:
            with db.transaction() as unit:
                return w.sessions.mint_screen(unit, campaign, session.id, owner_id=w.owner)

        return run

    paused = _Paused(w.db)
    _, shown = _alongside(paused, mint(paused), lambda: _confirm(s, campaign, session.id, x))
    assert isinstance(shown, Displayed)

    paused = _Paused(w.db)
    confirmed, minted = _alongside(
        paused, lambda: _confirm(s, campaign, session.id, y, reveals=s.over(paused)), mint(w.db)
    )
    assert isinstance(confirmed, Displayed) and isinstance(minted, tuple)

    assert w.dsn is not None
    with _holding(w.dsn, "SELECT 1 FROM campaign.table_sessions WHERE id = %s FOR UPDATE", (session.id,)):
        waiter, answer = _in_background(mint(_patient(w.dsn)))
        assert _someone_waits_on_a_lock(w.dsn)
    waiter.join(PATIENCE)
    assert answer and isinstance(answer[0], tuple)


@needs_db
@pytest.mark.parametrize("pair", ["stop_stop", "stop_remove", "remove_stop"])
def test_two_narrowings_both_succeed_and_each_advances_the_epoch_once(pgs: Served, pair: str) -> None:
    """T-C11. Two Stops, or a Stop and a Remove, in either order: both succeed,
    no deadlock (the server's count agrees), the epoch moves by exactly two,
    and the second finds nothing left to clear."""
    s, w = pgs, pgs.w
    campaign, session, (a, b) = _stage(w, seats=2)
    x = _document(w, campaign)
    _confirm(s, campaign, session.id, x, ParticipantsAudience(frozenset({a, b})))
    epoch, rows, deadlocks = _epoch(w, session.id), len(_reveal_rows(s, campaign)), _deadlocks(w.dsn)
    paused, patient = _Paused(w.db), _patient(w.dsn)

    def stop(db: Any) -> Callable[[], object]:
        return lambda: _stop(s, campaign, StopAll(), reveals=s.over(db))

    def remove(db: Any) -> Callable[[], object]:
        return lambda: remove_seat(
            db, s.stores, s.jobs, campaign_id=campaign, participant_id=a, owner_id=w.owner, now=_now()
        )

    first, second = {"stop_stop": (stop, stop), "stop_remove": (stop, remove), "remove_stop": (remove, stop)}[pair]
    one, two = _race(w.dsn, paused, first(paused), second(patient))
    assert not isinstance(one, BaseException) and not isinstance(two, BaseException)
    assert _epoch(w, session.id) == epoch + 2 and _live(w, session.id) == []
    added = [dict(e.detail) for e in _reveal_rows(s, campaign)[rows:]]
    if pair == "stop_stop":
        assert [d["document_id"] for d in added] == [x, None], "the second Stop found nothing left to clear"
    elif pair == "stop_remove":
        assert [d["participant_ids"] for d in added] == [sorted([a, b])]
    else:
        assert [d["participant_ids"] for d in added] == [[b]], "the Stop took only what the Remove left"
    assert _deadlocks(w.dsn) == deadlocks


@needs_db
def test_an_archive_waits_for_a_confirm_and_then_clears_it(pgs: Served) -> None:
    """T-C12 (critic 1, RC-2's campaign half). (a) A Confirm holding the share
    lock makes archive step 2 wait; the Confirm commits, and step 2's scan
    clears its copy as `campaign_archived`. (b) A Confirm composed before the
    archive and sent after it is a conflict."""
    s, w = pgs, pgs.w
    campaign, session, _ = _stage(w, seats=0)
    x, y = _document(w, campaign), _document(w, campaign)
    command = _command()
    paused = _Paused(w.db)
    patient_stores = s.stores
    one, two = _race(
        w.dsn,
        paused,
        lambda: _confirm(s, campaign, session.id, x, command=command, reveals=s.over(paused)),
        lambda: archive_step_two(
            _patient(w.dsn), patient_stores, campaign_id=campaign, owner_id=w.owner, now=_now()
        ),
        on="campaign.authz_state",
    )
    assert isinstance(one, Displayed) and not isinstance(two, BaseException)
    assert _by_command(w, session.id, command).ended_reason == EndReason.CAMPAIGN_ARCHIVED

    s.lifecycle.end(w.owner, campaign, session.id)
    campaign, session, _ = _stage(w, seats=0)
    y = _document(w, campaign)
    composed = _epoch(w, session.id)
    archive(s.db, s.stores, campaign_id=campaign, owner_id=w.owner, now=_now())
    with pytest.raises(RevealConflict):
        _confirm(s, campaign, session.id, y, epoch=composed)



# ── 1kg.7.2: the GM's read, in both worlds ───────────────────────────────────


def _edit(w: World, campaign: str, document: str, fields: dict[str, Any]) -> None:
    with w.db.transaction() as unit:
        w.documents.write_fields(
            unit, campaign, document, fields=fields, author=Author.GM, base_write_revision=None
        )


def test_d1_the_read_reports_stale_text_by_comparing_the_masked_text(served: Served) -> None:
    """ID-5, REVEAL-8, in both worlds (the route's A10 at the service level).
    `stale` is the set of live documents whose masked keys differ between the
    pinned version and the document's current data, sealed head or not; an
    unmasked change, a revert and a missing campaign are not stale."""
    # kills: reading the version table instead of the current data; comparing every key
    s, w = served, served.w
    campaign, session, _ = _stage(w, seats=0)
    document = _document(w, campaign)
    now = _now()
    with pytest.raises(RevealNotFound):
        s.reveals.view(campaign, owner_id=w.other_owner, now=now)
    shown = _confirm(s, campaign, session.id, document, mask=("name",))
    assert s.reveals.stale_documents(campaign, shown.picture) == frozenset()

    def stale() -> frozenset[str]:
        view = s.reveals.view(campaign, owner_id=w.owner, now=_now())
        assert view is not None and view.picture.session_id == session.id
        return view.stale

    assert stale() == frozenset()
    _edit(w, campaign, document, {"voice": "an unmasked change"})
    assert stale() == frozenset(), "REVEAL-8: only the masked keys are the table's text"
    _edit(w, campaign, document, {"name": "Someone else"})
    assert stale() == frozenset({document})
    assert s.reveals.stale_documents(campaign, shown.picture) == frozenset({document})
    with w.db.transaction() as unit:
        w.documents.seal(unit, campaign, document)
    assert stale() == frozenset({document}), "stale whether or not the head version is sealed"
    _edit(w, campaign, document, {"name": AN_NPC["name"]})
    assert stale() == frozenset(), "reverting the text clears it"
    assert s.reveals.stale_documents(_campaign(w), shown.picture) == frozenset(), "not found is not stale"

    s.lifecycle.end(w.owner, campaign, session.id)
    assert s.reveals.view(campaign, owner_id=w.owner, now=_now()) is None, "no live session, no picture"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_s3_the_policy_decision_point_agrees_on_who_is_an_active_participant(served: Served, seed: int) -> None:
    """ID-14, Critic C-8. The decision point is an oracle here and not a
    production import: for a generated mix of seats the Confirm names one at a
    time, a participant audience is accepted exactly when
    `eligible_for_audience` does not answer `inactive_participant`. The facts
    come from the generator's own labels (which seats it removed, which it put
    in another campaign), never from a store read, so the test cannot agree with
    the code by asking it."""
    # kills: accepting a removed seat in `active_participants`
    s, w = served, served.w
    rng = random.Random(seed)
    campaign, session, _ = _stage(w, seats=0)
    document = _document(w, campaign)
    elsewhere = _campaign(w)
    extra = rng.choices(["open", "confirmed", "removed"], k=1)
    states = ["open", "offered", "accepted", "confirmed", "removed", *extra]
    rng.shuffle(states)
    labelled = {_seat(w, campaign, state, w.players[n]): state for n, state in enumerate(states)}
    labelled[_seat(w, campaign, "confirmed", w.players[6])] = "confirmed"
    labelled[_seat(w, elsewhere, "confirmed", w.players[7])] = "foreign"
    labelled["prt_" + "u" * 22] = "unknown"
    labelled["nope"] = "unknown"

    facts = PolicyFacts(
        campaign_id=campaign,
        documents={},
        classes={},
        sheet_links={},
        active_participants=frozenset(
            pid for pid, label in labelled.items() if label not in {"removed", "foreign", "unknown"}
        ),
        group_members={},
    )
    assert {label for label in labelled.values()} >= {"removed", "foreign", "unknown", "confirmed", "open"}
    for participant, label in labelled.items():
        try:
            _confirm(s, campaign, session.id, document, ParticipantsAudience(frozenset({participant})))
            accepted = True
        except AudienceRefused:
            accepted = False
        verdict = eligible_for_audience(facts, document, "name", participant)
        assert accepted == (verdict.reason is not EligReason.INACTIVE_PARTICIPANT), label


# ── 1kg.7.2: RC-9, a Stop is answered while every gate slot waits ────────────


@needs_db
def test_rc9_a_stop_is_answered_while_four_confirms_wait_on_the_campaign_lock(pgs: Served) -> None:
    """RC-9 (RQ-6(b), X-3). The gate holds four connections and every one is a
    Confirm waiting for the campaign lock, which a connection outside the gate
    holds exclusively. Each Confirm ends `RevealBusy` at the lock bound, and the
    Stop that needed a slot gets one *by that bound* and then completes, because
    it never asks for the campaign lock: it returns a picture inside the lock
    bound plus a margin, and well inside the gate's own wait."""
    # kills: a Stop that takes the shared campaign lock, so it waits behind the holder past the bound
    s, w = pgs, pgs.w
    assert QUICK.lock_timeout_s + 1.5 < GATE_ACQUIRE_S, "the bound is not vacuous: the gate would time out later"
    campaign, session, _ = _stage(w, seats=0)
    documents = [_document(w, campaign) for _ in range(5)]
    _confirm(s, campaign, session.id, documents[0])
    epoch = _epoch(w, session.id)
    assert w.dsn is not None
    held = "SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR UPDATE"
    with _holding(w.dsn, held, (campaign,)):
        confirms = [
            _in_background(lambda d=document: _confirm(s, campaign, session.id, d, epoch=epoch))
            for document in documents[1:]
        ]
        deadline = time.monotonic() + PATIENCE
        while _waiters(w.dsn) < len(confirms) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _waiters(w.dsn) == len(confirms), "all four gate slots wait on the campaign lock"
        started = time.monotonic()
        picture = _stop(s, campaign, StopAll())
        elapsed = time.monotonic() - started
    for thread, outcome in confirms:
        thread.join(PATIENCE)
        assert len(outcome) == 1 and isinstance(outcome[0], RevealBusy), outcome
    assert picture is not None
    assert elapsed < QUICK.lock_timeout_s + 1.5 and elapsed < GATE_ACQUIRE_S
    assert elapsed > QUICK.lock_timeout_s * 0.5, "the Stop's slot was one a Confirm freed by its RevealBusy"
    assert _live(w, session.id) == [] and _epoch(w, session.id) == epoch + 1
    assert _shown(w, session.id) == {None: None}, "the table shows nothing, and the Stop's one epoch advance is all"

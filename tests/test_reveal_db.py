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

Requires DATABASE_URL (CI sets it for this file, `.github/workflows/ci.yml`,
pinned by `service/tests/test_ci_workflow.py`). From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_reveal_db.py -q
"""

from __future__ import annotations

import logging
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

from service import migrations as mig
from service.campaign_store import InMemoryCampaignStore, MissingParent, PostgresCampaignStore, shared_rows
from service.db import CampaignLockOrder, CampaignLockSettings, Database, InMemoryDatabase, PoolSettings
from service.document_store import InMemoryDocumentStore, PostgresDocumentStore
from service.participant_store import InMemoryParticipantStore, PostgresParticipantStore
from service.reveal_scope import (
    DocumentCopies,
    EndReason,
    EverySlot,
    NoSlot,
    ParticipantSlots,
    ParticipantTargets,
    TableTarget,
)
from service.reveal_store import (
    EMPTY_SLOT,
    AudienceRefused,
    CopyChange,
    Disclosure,
    InMemoryRevealStore,
    MaskRefused,
    PostgresRevealStore,
    RevealConflict,
    SlotRow,
    StaleSlots,
    Written,
    audit_reveal_invariants,
)
from service.table_session_store import (
    InMemoryTableSessionStore,
    PostgresTableSessionStore,
    TableSession,
    no_slots,
)
from service.workbench_contracts import Author, DocumentTypeId

QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
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
    return Database(target, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5), QUICK)


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


def _pg_world(target: str) -> World:
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
    return World(
        "postgres",
        _database(target),
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresTableSessionStore(slot_clear=no_slots),
        PostgresDocumentStore(),
        PostgresRevealStore(),
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

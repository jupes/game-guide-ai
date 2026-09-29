"""Seats, offers and the campaign lock against a real PostgreSQL (bead 1kg.2.2).

What no twin can prove: who waits for whom. Every test here runs two
transactions on two connections, interleaves them explicitly — one holds a
lock, the other is started, and the SERVER is asked (`pg_stat_activity`)
whether it is really blocked before the first lets go — so nothing depends on
timing. `wait_event_type = 'Lock'` covers advisory waits too.

The shared behaviour, run in both worlds, is `tests/test_campaign_db.py`'s.

Requires DATABASE_URL (CI sets it for this file, `.github/workflows/ci.yml`,
pinned by `service/tests/test_ci_workflow.py`). Without it every test skips.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database
from fastapi import HTTPException

from service import migrations as mig
from service.audit_log import PostgresAuditLog
from service.campaign_store import PostgresCampaignStore, SeatUnavailable
from service.campaigns_api import (
    NOT_APPLIED_MESSAGE,
    SEAT_CAP,
    CampaignStores,
    add_seat,
    archive,
    archive_step_one,
    archive_step_two,
    guarded,
    offer_seat,
    remove_seat,
)
from service.db import AdvisoryLock, CampaignLockSettings, Database, PoolSettings, advisory_key
from service.jobs import JobRunner, PostgresJobQueue
from service.participant_store import PostgresParticipantStore
from service.reconciliation import RECONCILE_KIND, handler, reconcile_slots
from service.seat_offer_store import THROTTLE_LIMIT, PostgresSeatOfferStore
from service.seats_api import accept_offer, decline_offer
from service.table_session_store import PostgresTableSessionStore, no_slots

pytestmark = needs_db

CAMPAIGN = "cmp_" + "s" * 22
OTHER_CAMPAIGN = "cmp_" + "t" * 22
#: The interleaving tests wait for a THREAD, not for the database, so RQ-8's
#: five seconds would race the harness; the lock wait stays one second.
PATIENT = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
PATIENCE = 15
NOW = datetime.now(UTC)


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("seats") as target:
        mig.migrate(target)
        yield target


def _account(dsn: str, email: str) -> int:
    with connect(dsn) as conn:
        return int(
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
            ).fetchone()[0]
        )


def _campaign(dsn: str, owner: int, campaign: str = CAMPAIGN) -> str:
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (campaign, owner)
        )
    return campaign


@pytest.fixture
def owner(dsn: str) -> int:
    gm = _account(dsn, "gm@example.com")
    _campaign(dsn, gm)
    return gm


def _database(dsn: str) -> Database:
    return Database(dsn, PoolSettings(sync_max=6, async_max=0, acquire_timeout_s=5), PATIENT)


def _stores() -> CampaignStores:
    return CampaignStores(
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresTableSessionStore(slot_clear=no_slots),
        PostgresSeatOfferStore(),
        PostgresAuditLog(),
    )


def _someone_waits_on_a_lock(dsn: str, waiters: int = 1) -> bool:
    """Whether at least `waiters` backends on this database are blocked on a
    lock — the server's own account, advisory locks included."""
    deadline = time.monotonic() + PATIENCE
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


@contextmanager
def _holding(dsn: str, statement: str, params: tuple) -> Iterator[None]:
    """A second connection that runs `statement` and keeps its transaction open
    until the block ends, then commits."""
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with connect(dsn, autocommit=False) as conn:
                conn.execute(statement, params)
                took.set()
                release.wait(PATIENCE)
                conn.commit()
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE), "the holder never took its lock"
    if failure:
        raise failure[0]
    try:
        yield
    finally:
        release.set()
        thread.join(PATIENCE)
    if failure:
        raise failure[0]


def _holding_the_campaign(dsn: str, campaign: str = CAMPAIGN) -> Any:
    return _holding(dsn, "SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR UPDATE", (campaign,))


def _started(work: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    produced: list[object] = []

    def run() -> None:
        try:
            produced.append(work())
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            produced.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, produced


def _finished(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(PATIENCE)
        assert not thread.is_alive(), "a transaction never finished"


_ALIASES = itertools.count()


def _seats(db: Database, stores: CampaignStores, count: int, campaign: str = CAMPAIGN) -> list[str]:
    """`count` open seats, each under an alias no other seat of this test has."""
    with db.transaction() as unit:
        return [stores.participants.add(unit, campaign, alias=f"Seat {next(_ALIASES)}").id for _ in range(count)]


def _count(dsn: str, sql: str, params: tuple = ()) -> int:
    with connect(dsn) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def _offer(db: Database, stores: CampaignStores, owner: int, seat: str, address: str, campaign: str = CAMPAIGN) -> bool:
    return offer_seat(
        db, stores, campaign_id=campaign, participant_id=seat, owner_id=owner, owner_email="gm@example.com",
        address=address, now=NOW,
    )


def _offer_id(dsn: str, seat: str) -> str:
    with connect(dsn) as conn:
        return str(conn.execute("SELECT id FROM campaign.seat_offers WHERE participant_id = %s", (seat,)).fetchone()[0])


# ── RC-11: the cap is counted under the lock ─────────────────────────────────


def _add(db: Database, stores: CampaignStores, owner: int, alias: str) -> object:
    return add_seat(db, stores, campaign_id=CAMPAIGN, owner_id=owner, alias=alias, now=NOW)


def test_two_seat_adds_at_one_under_the_cap_seat_exactly_one(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    _seats(db, stores, SEAT_CAP - 1)
    with _holding_the_campaign(dsn):
        first, one = _started(lambda: _add(db, stores, owner, "Wren"))
        second, two = _started(lambda: _add(db, stores, owner, "Finch"))
        assert _someone_waits_on_a_lock(dsn, 2), "both adds must wait behind the lock"
    _finished(first, second)
    outcomes = one + two
    refused = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 409, outcomes
    assert refused[0].detail["code"] == "seat_cap_reached"  # type: ignore[index]
    assert _count(dsn, "SELECT count(*) FROM campaign.participants WHERE removed_at IS NULL") == SEAT_CAP


# ── An address holds one live offer per campaign ─────────────────────────────


def test_two_offers_of_one_address_to_two_seats_write_one_and_both_answer_alike(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    a, b = _seats(db, stores, 2)
    with _holding_the_campaign(dsn):
        first, one = _started(lambda: _offer(db, stores, owner, a, "Wren@example.com"))
        second, two = _started(lambda: _offer(db, stores, owner, b, "wren@EXAMPLE.com"))
        assert _someone_waits_on_a_lock(dsn, 2)
    _finished(first, second)
    assert sorted(one + two) == [False, True], "one writes, the other is a repeat: no exception either way"
    assert _count(dsn, "SELECT count(*) FROM campaign.seat_offers") == 1


# ── The throttle holds across campaigns: the advisory lock ───────────────────


def test_the_throttle_across_two_campaigns_of_one_owner_writes_exactly_the_thirtieth(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    _campaign(dsn, owner, OTHER_CAMPAIGN)
    earlier = _seats(db, stores, THROTTLE_LIMIT - 1)
    for n, seat in enumerate(earlier):
        assert _offer(db, stores, owner, seat, f"p{n}@example.com")
    (mine,) = _seats(db, stores, 1)
    (theirs,) = _seats(db, stores, 1, OTHER_CAMPAIGN)
    key = (int(AdvisoryLock.SEAT_OFFERS), advisory_key(str(owner)))
    with _holding(dsn, "SELECT pg_advisory_xact_lock(%s, %s)", key):
        first, one = _started(lambda: _offer(db, stores, owner, mine, "last@example.com"))
        second, two = _started(lambda: _offer(db, stores, owner, theirs, "other@example.com", OTHER_CAMPAIGN))
        assert _someone_waits_on_a_lock(dsn, 2), "both offers wait on the owner's advisory lock"
    _finished(first, second)
    outcomes = one + two
    throttled = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(throttled) == 1 and throttled[0].status_code == 429, outcomes
    assert True in outcomes
    assert _count(dsn, "SELECT count(*) FROM campaign.seat_offers WHERE offered_by = %s", (owner,)) == THROTTLE_LIMIT


# ── Accept against Remove, decline, and a second accept ─────────────────────


def _an_offered_seat(dsn: str, db: Database, stores: CampaignStores, owner: int, address: str) -> tuple[str, str]:
    (seat,) = _seats(db, stores, 1)
    assert _offer(db, stores, owner, seat, address)
    return seat, _offer_id(dsn, seat)


def _accept(db: Database, stores: CampaignStores, offer: str, player: int, address: str) -> object:
    return accept_offer(db, stores, offer_id=offer, user_id=player, address=address, now=NOW)


@contextmanager
def _holding_a_transaction(db: Database, work: Callable[[Any], None]) -> Iterator[None]:
    """`work` in a transaction of this service's own, kept open until the block
    ends, then committed."""
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with db.transaction() as unit:
                work(unit)
                took.set()
                release.wait(PATIENCE)
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE)
    if failure:
        raise failure[0]
    try:
        yield
    finally:
        release.set()
        thread.join(PATIENCE)
    if failure:
        raise failure[0]


def test_an_accept_behind_a_remove_is_the_one_404_and_the_seat_stays_removed(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    player = _account(dsn, "wren@example.com")
    seat, offer = _an_offered_seat(dsn, db, stores, owner, "wren@example.com")

    def removing(unit: Any) -> None:
        assert stores.participants.hold(unit, seat, campaign_id=CAMPAIGN) is not None
        stores.participants.remove(unit, CAMPAIGN, seat, now=NOW)
        stores.offers.withdraw_open(unit, CAMPAIGN, seat, now=NOW)

    with _holding_a_transaction(db, removing):
        waiter, produced = _started(lambda: _accept(db, stores, offer, player, "wren@example.com"))
        assert _someone_waits_on_a_lock(dsn)
    _finished(waiter)
    assert isinstance(produced[0], HTTPException) and produced[0].status_code == 404, produced
    assert _count(dsn, "SELECT count(*) FROM campaign.participants WHERE id = %s AND removed_at IS NOT NULL "
                       "AND user_id IS NULL", (seat,)) == 1


def test_a_remove_behind_an_accept_wins_and_the_offer_stays_closed(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    player = _account(dsn, "wren@example.com")
    seat, offer = _an_offered_seat(dsn, db, stores, owner, "wren@example.com")

    def accepting(unit: Any) -> None:
        unit.lock_campaign(CAMPAIGN, shared=False)
        stores.participants.offer(unit, CAMPAIGN, seat, user_id=player)
        stores.participants.accept(unit, CAMPAIGN, seat, user_id=player, now=NOW)
        stores.offers.close(unit, offer, outcome="accepted", now=NOW)

    queue = PostgresJobQueue(db)
    with _holding_a_transaction(db, accepting):
        waiter, produced = _started(
            lambda: remove_seat(db, stores, queue, campaign_id=CAMPAIGN, participant_id=seat, owner_id=owner, now=NOW)
        )
        assert _someone_waits_on_a_lock(dsn)
    _finished(waiter)
    assert isinstance(produced[0], int), produced
    removed = "SELECT count(*) FROM campaign.participants WHERE id = %s AND removed_at IS NOT NULL"
    assert _count(dsn, removed, (seat,)) == 1
    assert _count(dsn, "SELECT count(*) FROM campaign.seat_offers WHERE outcome IS NULL") == 0


def test_an_accept_and_a_decline_of_one_offer_have_exactly_one_winner(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    player = _account(dsn, "wren@example.com")
    seat, offer = _an_offered_seat(dsn, db, stores, owner, "wren@example.com")

    def accepting(unit: Any) -> None:
        unit.lock_campaign(CAMPAIGN, shared=False)
        assert stores.participants.hold(unit, seat, campaign_id=CAMPAIGN) is not None
        stores.participants.offer(unit, CAMPAIGN, seat, user_id=player)
        stores.participants.accept(unit, CAMPAIGN, seat, user_id=player, now=NOW)
        stores.offers.close(unit, offer, outcome="accepted", now=NOW)

    with _holding_a_transaction(db, accepting):
        waiter, produced = _started(
            lambda: decline_offer(
                db, stores, offer_id=offer, user_id=player, address="wren@example.com", block=True, now=NOW
            )
        )
        assert _someone_waits_on_a_lock(dsn)
    _finished(waiter)
    assert isinstance(produced[0], HTTPException) and produced[0].status_code == 404, produced
    assert _count(dsn, "SELECT count(*) FROM campaign.seat_offers WHERE outcome = 'accepted'") == 1
    assert _count(dsn, "SELECT count(*) FROM campaign.seat_blocks") == 0, "the loser changed nothing"


def test_two_accepts_by_one_account_in_one_campaign_seat_it_once(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    player = _account(dsn, "wren@example.com")
    _, first_offer = _an_offered_seat(dsn, db, stores, owner, "wren@example.com")
    (second_seat,) = _seats(db, stores, 1)
    with db.transaction() as unit:
        stores.offers.create(unit, CAMPAIGN, second_seat, offered_by=owner, address="wren@example.com", now=NOW)
    second_offer = _offer_id(dsn, second_seat)
    with _holding_the_campaign(dsn):
        first, one = _started(lambda: _accept(db, stores, first_offer, player, "wren@example.com"))
        second, two = _started(lambda: _accept(db, stores, second_offer, player, "wren@example.com"))
        assert _someone_waits_on_a_lock(dsn, 2)
    _finished(first, second)
    refused = [o for o in one + two if isinstance(o, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 404, one + two
    assert _count(dsn, "SELECT count(*) FROM campaign.participants WHERE user_id = %s", (player,)) == 1


# ── Remove never waits for the campaign lock; its job does ───────────────────


def test_remove_commits_under_a_held_campaign_lock_and_its_job_retries_until_it_frees(
    dsn: str, owner: int
) -> None:
    db, stores = _database(dsn), _stores()
    player = _account(dsn, "wren@example.com")
    seat, offer = _an_offered_seat(dsn, db, stores, owner, "wren@example.com")
    assert _accept(db, stores, offer, player, "wren@example.com") is not None
    queue = PostgresJobQueue(db)
    # The runner's clock runs ahead of the job's `run_after`, so each attempt
    # below can claim it without waiting out the retry backoff.
    clock = [datetime.now(UTC) + timedelta(minutes=1)]
    runner = JobRunner(queue, {RECONCILE_KIND: handler(db, slots=reconcile_slots)}, clock=lambda: clock[0])
    revision = _count(dsn, "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,))
    with _holding_the_campaign(dsn):
        job_id = remove_seat(db, stores, queue, campaign_id=CAMPAIGN, participant_id=seat, owner_id=owner, now=NOW)
        assert job_id is not None, "Remove committed while another transaction held the campaign lock"
        attempt = runner.run_job(job_id)
        assert (attempt.ran, attempt.failed) == (1, 1), "the reconciliation waited, timed out and will retry"
    clock[0] = clock[0] + timedelta(hours=1)
    retried = runner.run_job(job_id)
    assert (retried.ran, retried.failed) == (1, 0)
    assert _count(dsn, "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,)) == (
        revision + 1
    )
    assert _count(dsn, "SELECT count(*) FROM app.jobs") == 0


def test_the_reconciliation_of_a_deleted_campaign_completes_as_a_no_op(dsn: str, owner: int) -> None:
    db = _database(dsn)
    queue = PostgresJobQueue(db)
    with db.transaction() as unit:
        job_id = queue.enqueue(unit, RECONCILE_KIND, {"campaign_id": CAMPAIGN})
    with connect(dsn) as conn:
        conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (CAMPAIGN,))
    result = JobRunner(queue, {RECONCILE_KIND: handler(db, slots=reconcile_slots)}).run_job(job_id)
    assert (result.ran, result.failed) == (1, 0)
    assert _count(dsn, "SELECT count(*) FROM app.jobs") == 0


# ── RC-15: archive's step 1 commits even when step 2 cannot get the lock ─────


def _live_session(db: Database, owner: int) -> Any:
    """`TableSessionStore.start` through one place: 1kg.2.3 changes what it
    returns (the session alone, rather than the session and a link token)."""
    with db.transaction() as unit:
        started = PostgresTableSessionStore(slot_clear=no_slots).start(
            unit, CAMPAIGN, owner_id=owner, expires_at=datetime.now(UTC) + timedelta(hours=12)
        )
    return started[0] if isinstance(started, tuple) else started


def test_archive_under_a_held_lock_answers_not_applied_yet_after_narrowing(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    session = _live_session(db, owner)
    epoch = "SELECT reveal_epoch FROM campaign.table_sessions WHERE id = %s"
    with _holding_the_campaign(dsn):
        archive_step_one(db, stores, campaign_id=CAMPAIGN, owner_id=owner)
        with pytest.raises(HTTPException) as refused:
            guarded(
                lambda: archive_step_two(db, stores, campaign_id=CAMPAIGN, owner_id=owner, now=NOW),
                busy_message=NOT_APPLIED_MESSAGE,
            )
        assert refused.value.status_code == 503
        assert refused.value.detail == {  # type: ignore[comparison-overlap]
            "code": "backend_unavailable", "message": NOT_APPLIED_MESSAGE, "retryable": True,
        }
        assert _count(dsn, "SELECT count(*) FROM campaign.campaigns WHERE archived_at IS NULL") == 1
        assert _count(dsn, epoch, (session.id,)) == session.reveal_epoch + 1, "step 1 committed"
    archive(db, stores, campaign_id=CAMPAIGN, owner_id=owner, now=NOW)
    assert _count(dsn, "SELECT count(*) FROM campaign.campaigns WHERE archived_at IS NOT NULL") == 1
    assert _count(dsn, "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,)) == 1
    assert _count(dsn, "SELECT count(*) FROM audit.events WHERE action = 'campaign.archived'") == 1


# ── The composite foreign keys ───────────────────────────────────────────────


def test_the_database_refuses_an_offer_of_another_campaigns_seat_or_by_another_offerer(
    dsn: str, owner: int
) -> None:
    db, stores = _database(dsn), _stores()
    stranger = _account(dsn, "other@example.com")
    _campaign(dsn, stranger, OTHER_CAMPAIGN)
    (theirs,) = _seats(db, stores, 1, OTHER_CAMPAIGN)
    (mine,) = _seats(db, stores, 1)
    insert = (
        "INSERT INTO campaign.seat_offers (id, campaign_id, participant_id, offered_by, address, address_key, "
        "expires_at) VALUES (%s, %s, %s, %s, 'a@b.c', 'a@b.c', now() + interval '1 day')"
    )
    for campaign, seat, by in ((CAMPAIGN, theirs, owner), (CAMPAIGN, mine, stranger)):
        with connect(dsn) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(insert, ("sof_" + "f" * 22, campaign, seat, by))
    with db.transaction() as unit:
        with pytest.raises(SeatUnavailable) as refused:
            stores.offers.create(unit, CAMPAIGN, theirs, offered_by=owner, address="wren@example.com")
        assert refused.value.__context__ is None and refused.value.__cause__ is None
        stores.participants.add(unit, CAMPAIGN, alias="Still usable")


def test_deleting_either_account_of_a_block_removes_it(dsn: str, owner: int) -> None:
    blocker = _account(dsn, "wren@example.com")
    other = _account(dsn, "finch@example.com")
    with connect(dsn) as conn:
        conn.execute("INSERT INTO campaign.seat_blocks (blocker_user_id, blocked_owner_id) VALUES (%s, %s)",
                     (blocker, owner))
        conn.execute("INSERT INTO campaign.seat_blocks (blocker_user_id, blocked_owner_id) VALUES (%s, %s)",
                     (other, owner))
        conn.execute("DELETE FROM auth.users WHERE id = %s", (blocker,))
        assert conn.execute("SELECT count(*) FROM campaign.seat_blocks").fetchone()[0] == 1
        conn.execute("DELETE FROM auth.users WHERE id = %s", (owner,))
        assert conn.execute("SELECT count(*) FROM campaign.seat_blocks").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO campaign.seat_blocks (blocker_user_id, blocked_owner_id) VALUES (%s, %s)",
                         (other, other))


# ── SEC-41's `own_slot`, as the own-slot resolver will read it ──────────────

#: SEC-41's example, verbatim from docs/adr/gm-workbench-threat-model.md; only
#: the named parameters are respelled for psycopg below.
SEC_41_QUERY = (
    "SELECT s.id, (c.owner_id = :u) AS is_owner, p.id AS participant_id, "
    "(p.confirmed_at IS NOT NULL) AS own_slot FROM campaign.table_sessions s "
    "JOIN campaign.campaigns c ON c.id = s.campaign_id "
    "LEFT JOIN campaign.participants p ON p.campaign_id = s.campaign_id AND p.user_id = :u "
    "AND p.accepted_at IS NOT NULL AND p.removed_at IS NULL "
    "WHERE s.campaign_id = :c AND s.state = 'live' AND s.expires_at > now() "
    "AND (c.owner_id = :u OR p.id IS NOT NULL)"
)


def _principal(dsn: str, user: int) -> list[tuple[bool, str | None, bool]]:
    with connect(dsn) as conn:
        rows = conn.execute(
            SEC_41_QUERY.replace(":u", "%(u)s").replace(":c", "%(c)s"), {"u": user, "c": CAMPAIGN}
        ).fetchall()
    return [(bool(row[1]), row[2], bool(row[3])) for row in rows]


def test_own_slot_is_true_for_a_confirmed_seat_alone(dsn: str, owner: int) -> None:
    """L-11: the column's meaning, pinned for 1kg.7.1, 1kg.7.2 and 1kg.7.5."""
    db, stores = _database(dsn), _stores()
    _live_session(db, owner)
    people = {name: _account(dsn, f"{name}@example.com") for name in ("confirmed", "awaiting", "invited",
                                                                       "removed", "elsewhere")}

    def seated(name: str, campaign: str = CAMPAIGN, *, confirm: bool = True) -> str:
        (seat,) = _seats(db, stores, 1, campaign)
        with db.transaction() as unit:
            stores.participants.offer(unit, campaign, seat, user_id=people[name])
            stores.participants.accept(unit, campaign, seat, user_id=people[name])
            if confirm:
                stores.participants.confirm(unit, campaign, seat)
        return seat

    confirmed = seated("confirmed")
    seated("awaiting", confirm=False)
    (open_seat,) = _seats(db, stores, 1)
    assert _offer(db, stores, owner, open_seat, "invited@example.com")
    removed = seated("removed")
    with db.transaction() as unit:
        stores.participants.remove(unit, CAMPAIGN, removed)
    _campaign(dsn, owner, OTHER_CAMPAIGN)
    seated("elsewhere", OTHER_CAMPAIGN)

    assert _principal(dsn, owner) == [(True, None, False)], "the owner is admitted with no participant row"
    assert _principal(dsn, people["confirmed"]) == [(False, confirmed, True)]
    assert [row[2] for row in _principal(dsn, people["awaiting"])] == [False], "the table slot only"
    for name in ("invited", "removed", "elsewhere"):
        assert _principal(dsn, people[name]) == [], name

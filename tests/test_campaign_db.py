"""The campaign schema against a real PostgreSQL (1kg.2.1).

What no fake can prove, and what this file is therefore for: who **blocks** whom
on a campaign's authorisation row and on a participant row, that the bounds RQ-8
asks for really fire, that `FOR NO KEY UPDATE` lets a referencing insert through
where `FOR UPDATE` would not (RQ-3), and that every transaction is READ
COMMITTED whatever the server has been told to default to (RQ-2(d)).

It also holds the **shared behavioural suite**: one set of tests run twice, once
against the in-memory twin and once against PostgreSQL, so that a rule the two
could disagree about is asserted of both. Those tests carry no mark and run
anywhere; their `postgres` parameter carries `needs_db` and skips without a
server.

`service/tests/test_db.py` owns the other half — the lock-order rules, the
refusals and the statements — which the twin can show and which runs on any
machine. Neither file claims the other's half, with one exception kept
here: fma's scripted `offer` test
(`test_a_driver_refusal_inside_offer_becomes_the_one_refusal_with_nothing_attached`)
is the one statement-level test in this file.

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_campaign_db.py -q
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import time
import traceback
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import count
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database
from fastapi import HTTPException

from service import campaign_identity
from service import migrations as mig
from service.audit_log import (
    ActorKind,
    AuditAction,
    Decision,
    InMemoryAuditLog,
    PostgresAuditLog,
)
from service.campaign_store import (
    AliasTaken,
    Campaign,
    InMemoryCampaignStore,
    InvalidCursor,
    LiveSessionExists,
    MissingParent,
    NotDue,
    NotLive,
    PostgresCampaignStore,
    ScreenLimit,
    SeatNotAccepted,
    SeatUnavailable,
    StartReplayed,
    check_name,
    encode_cursor,
)
from service.campaign_summary_store import InMemoryCampaignSummaryStore, PostgresCampaignSummaryStore
from service.campaigns_api import CampaignStores, archive, archive_step_one, archive_step_two, restore
from service.db import (
    CampaignAuthzMissing,
    CampaignLockOrder,
    CampaignLockSettings,
    Database,
    InMemoryDatabase,
    PgTransaction,
    PoolSettings,
    TwinWouldBlock,
)
from service.history import InMemoryMessageStore
from service.jobs import InMemoryJobQueue, Job, JobContext, JobRunner, PostgresJobQueue
from service.participant_store import (
    InMemoryParticipantStore,
    Participant,
    PostgresParticipantStore,
    alias_key,
    check_alias,
)
from service.reconciliation import (
    RECONCILE_KIND,
    enqueue_reconciliation,
    handler,
    reconcile,
    reconcile_slots,
)
from service.seat_offer_store import (
    ACCEPTED,
    DECLINED,
    EXPIRED,
    OFFER_LIFETIME,
    THROTTLE_WINDOW,
    WITHDRAWN,
    InMemorySeatOfferStore,
    PostgresSeatOfferStore,
    SeatOffer,
    ThrottleCount,
    address_key,
    check_address,
    seat_status,
    throttle_wait_s,
)
from service.table_session_store import (
    SCREENS_PER_SESSION,
    InMemoryTableSessionStore,
    PostgresTableSessionStore,
    ScreenGrant,
    TableSession,
    no_slots,
)
from service.table_sessions import (
    EXPIRE_KIND,
    FANOUT_BOUND,
    SESSION_LIFETIME,
    BackendUnavailable,
    Binding,
    FanOutBound,
    Inactive,
    MintedScreen,
    TableSessions,
    may_write,
)
from service.workbench_contracts import REFUSED_TEXT_CODE_POINTS, SeatStatus

CAMPAIGN = "cmp_" + "a" * 22
OTHER_CAMPAIGN = "cmp_" + "b" * 22
PARTICIPANT = "prt_" + "a" * 22
SESSION = "ses_" + "a" * 22
#: `1kg.2.2`'s reconciliation kind, imported from its module (never a second
#: literal): what End, Rotate and expiry enqueue.
RECONCILE = RECONCILE_KIND

#: A second is long enough that a lock taken first is always taken first, and
#: short enough that a test which should time out does not hold the job up.
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
#: For the interleaving tests: a transaction there waits for a *thread*, not for
#: a database, so RQ-8's five seconds would be racing the test harness.
PATIENT = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
#: How long a test waits for another thread before calling it a hang.
PATIENCE = 15


def _command() -> str:
    """A fresh client command id, in the wire contract's `CommandId` shape."""
    return secrets.token_urlsafe(16)


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("campaign") as target:
        mig.migrate(target)
        yield target


def _seed(dsn: str, *, email: str = "gm@example.com", campaign: str = CAMPAIGN) -> int:
    """A GM and one of their campaigns, by raw SQL — the AFTER INSERT trigger
    writes `authz_state`, so this is also how the tests below get one."""
    with connect(dsn) as conn:
        owner = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
            (campaign, owner),
        )
    return int(owner)


@pytest.fixture
def owner(dsn: str) -> int:
    return _seed(dsn)


def _database(dsn: str, settings: CampaignLockSettings = QUICK) -> Database:
    return Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5), settings)


@contextmanager
def _a_transaction_holding(
    db: Database,
    *,
    shared: bool,
    campaign: str = CAMPAIGN,
    then: Callable[[object], None] | None = None,
) -> Iterator[threading.Event]:
    """Another thread's transaction, holding `campaign`'s lock until the block
    this wraps ends. It yields the event that releases it, so a test can let the
    holder commit and then watch what the waiter sees.

    The holder's failure is re-raised here rather than swallowed: a test that
    would otherwise pass because nothing ever held the lock is not a test.
    """
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with db.transaction() as unit:
                unit.lock_campaign(campaign, shared=shared)
                if then is not None:
                    then(unit)
                took.set()
                release.wait(PATIENCE)
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE), "the holding transaction never took the lock"
    if failure:
        raise failure[0]
    try:
        yield release
    finally:
        release.set()
        thread.join(PATIENCE)
        assert not thread.is_alive(), "the holding transaction never finished"
    if failure:
        raise failure[0]


# ── Behaviour 28 — READ COMMITTED is stated, not inherited ───────────────────


@needs_db
def test_every_transaction_is_read_committed_whatever_the_server_defaults_to(dsn: str) -> None:
    """RQ-2's reasoning about what a lock holds still is READ COMMITTED's. An
    operator who sets the database or the role to SERIALIZABLE would otherwise
    change that reasoning silently — and under SERIALIZABLE the same code would
    start failing with serialization errors instead of waiting."""
    with connect(dsn) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        # ALTER DATABASE is a utility statement: it takes no bound parameter, so
        # the level is a literal. The database name comes from
        # `current_database()` and the level is this file's own constant, so
        # nothing a caller supplies is interpolated here.
        conn.execute(
            f'ALTER DATABASE "{database}" SET default_transaction_isolation = \'serializable\''
        )

    with connect(dsn) as fresh:
        assert fresh.execute("SHOW transaction_isolation").fetchone()[0] == "serializable", (
            "the server default did not change, so this test proves nothing"
        )

    with _database(dsn).transaction() as unit:
        assert unit.conn.execute("SHOW transaction_isolation").fetchone()[0] == "read committed"


# ── Behaviour 27 — the lock matrix on the real row ───────────────────────────


@needs_db
def test_two_shared_holders_of_one_campaign_coexist(dsn: str, owner: int) -> None:
    """Every authorisation check takes the shared lock, so if two of them
    blocked each other the Workbench would serialise on the common case."""
    db = _database(dsn)
    with _a_transaction_holding(db, shared=True):
        with db.transaction() as unit:
            unit.lock_campaign(CAMPAIGN, shared=True)
            assert unit.campaign_locks == [(CAMPAIGN, "share")]


@pytest.mark.parametrize(
    ("held_shared", "wanted_shared"),
    [
        pytest.param(True, False, id="share-blocks-exclusive"),
        pytest.param(False, True, id="exclusive-blocks-share"),
        pytest.param(False, False, id="exclusive-blocks-exclusive"),
    ],
)
@needs_db
def test_a_conflicting_campaign_lock_waits_and_then_times_out(
    dsn: str, owner: int, held_shared: bool, wanted_shared: bool
) -> None:
    """Behaviours 27 and 29 in one: the conflict matrix, and `lock_timeout`
    ending the wait as a lock timeout rather than holding one of the gate's
    connections until the client gives up."""
    db = _database(dsn)
    with _a_transaction_holding(db, shared=held_shared):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with db.transaction() as unit:
                unit.lock_campaign(CAMPAIGN, shared=wanted_shared)


@needs_db
def test_a_waiter_reads_the_revision_the_holder_committed(dsn: str, owner: int) -> None:
    """The point of the lock: a waiter is not merely delayed, it goes on to read
    what the holder decided — never the value it would have read before."""
    db = _database(dsn, CampaignLockSettings(lock_timeout_s=4, transaction_timeout_s=10))
    with _a_transaction_holding(
        db, shared=False, then=lambda unit: unit.advance_authz_revision(CAMPAIGN)
    ) as release:
        seen: list[int] = []
        waiting = threading.Thread(
            target=lambda: seen.append(_revision_under_the_lock(db)), daemon=True
        )
        waiting.start()
        assert _someone_waits_on_a_lock(dsn), "nobody was blocked, so this proves nothing"
        release.set()
        waiting.join(PATIENCE)
    assert seen == [1], "a waiter must see the holder's committed revision"


def _someone_waits_on_a_lock(dsn: str, patience: float = PATIENCE) -> bool:
    """Whether a backend on this database is blocked on a lock — the server's own
    account of it, so the test does not merely assume the race went its way."""
    deadline = time.monotonic() + patience
    with connect(dsn) as conn:
        while time.monotonic() < deadline:
            blocked = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if blocked:
                return True
            time.sleep(0.05)
    return False


def _revision_under_the_lock(db: Database) -> int:
    with db.transaction() as unit:
        unit.lock_campaign(CAMPAIGN, shared=True)
        return int(
            unit.conn.execute(
                "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,)
            ).fetchone()[0]
        )


@needs_db
def test_locking_a_campaign_with_no_authorisation_row_raises_the_named_error(dsn: str) -> None:
    """RQ-2(c), fail closed. The campaign may never have existed or may have been
    deleted under the caller; both are the same answer, and neither is a silent
    success."""
    with pytest.raises(CampaignAuthzMissing, match="authorisation"):
        with _database(dsn).transaction() as unit:
            unit.lock_campaign(CAMPAIGN, shared=True)


@needs_db
def test_the_revision_advances_only_under_the_exclusive_lock_and_is_durable(dsn: str, owner: int) -> None:
    db = _database(dsn)
    with db.transaction() as unit:
        unit.lock_campaign(CAMPAIGN, shared=False)
        assert unit.advance_authz_revision(CAMPAIGN) == 1
    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,)
        ).fetchone()[0] == 1

    with pytest.raises(RuntimeError, match="boom"):
        with db.transaction() as unit:
            unit.lock_campaign(CAMPAIGN, shared=False)
            unit.advance_authz_revision(CAMPAIGN)
            raise RuntimeError("boom")
    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,)
        ).fetchone()[0] == 1, "a rolled-back advance is not kept"


# ── Behaviour 29 — the bounds RQ-8 asks for ──────────────────────────────────


@needs_db
def test_the_transaction_bound_arms_inside_the_transaction_and_is_local_to_it(dsn: str, owner: int) -> None:
    """`transaction_timeout` is PostgreSQL 17 and is set with `set_config(...,
    true)` from inside the transaction that has already begun, so the question
    worth asking is whether it is in force there at all — and whether it is gone
    afterwards, rather than leaking onto the next user of the connection."""
    db = _database(dsn, CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=9))
    with db.transaction() as unit:
        unit.lock_campaign(CAMPAIGN, shared=True)
        assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "9s"
        assert unit.conn.execute("SHOW lock_timeout").fetchone()[0] == "1s"

    with connect(dsn) as conn:
        assert conn.execute("SHOW transaction_timeout").fetchone()[0] == "0", (
            "the bound is this transaction's, never the server's"
        )


@needs_db
def test_the_default_lock_timeout_is_two_seconds_on_the_gate_operators_run(dsn: str, owner: int) -> None:
    """G-4. The lock timeout is derived from the gate now — half of it, capped at
    RQ-8's suggested two seconds — so what an operator running the documented
    default gate of 5 s gets must still be exactly those two seconds. It reaches
    the server in milliseconds, which is that GUC's unit; the server is what
    says whether `2000ms` is the same duration as before."""
    db = Database(dsn, PoolSettings(sync_max=2, async_max=0))
    assert db.campaign_lock.lock_timeout == "2000ms"
    with db.transaction() as unit:
        unit.lock_campaign(CAMPAIGN, shared=True)
        assert unit.conn.execute("SHOW lock_timeout").fetchone()[0] == "2s"


@needs_db
def test_a_caller_may_raise_the_transaction_bound_for_its_own_longer_work(dsn: str, owner: int) -> None:
    with _database(dsn).transaction() as unit:
        unit.lock_campaign(CAMPAIGN, shared=False, transaction_timeout_s=42)
        assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "42s"


@needs_db
def test_the_first_transaction_bound_a_transaction_sets_is_the_one_that_fires(
    dsn: str, owner: int
) -> None:
    """G-10, characterised against the server rather than read off its source.

    Two primitives in one transaction each set `transaction_timeout`, and the
    question is which one the timer obeys. PostgreSQL 17's assign hook arms it
    only when one is not already running, so the expectation is that the FIRST
    value wins and a later one changes only what `SHOW` reports — which is why
    `transaction_bound`'s docstring tells a caller that needs longer to pass its
    bound to the first primitive it calls.

    The transaction here is cut short at one second while `SHOW` says thirty. It
    owns a connection it can lose: what PostgreSQL 17 does at
    `transaction_timeout` is end the session.
    """
    db = _database(dsn)
    participants, sessions = PostgresParticipantStore(), PostgresTableSessionStore(slot_clear=no_slots)
    with db.transaction() as unit:
        seat = participants.add(unit, CAMPAIGN, alias="Rook")
        session = sessions.start(
            unit,
            CAMPAIGN,
            owner_id=owner,
            expires_at=datetime.now(UTC) + timedelta(hours=12),
            command_id=_command(),
        )

    with pytest.raises(psycopg.Error):
        with db.transaction() as unit:
            participants.hold(unit, seat.id, campaign_id=CAMPAIGN, transaction_timeout_s=1)
            sessions.narrow(unit, CAMPAIGN, session.id, transaction_timeout_s=30)
            assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "30s", (
                "the later value is what the server REPORTS"
            )
            assert unit.transaction_bounds == ["1s", "30s"]
            unit.conn.execute("SELECT pg_sleep(2)")

    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT reveal_epoch FROM campaign.table_sessions WHERE id = %s", (session.id,)
        ).fetchone()[0] == 0, "the transaction was cut short at one second, and kept nothing"


@needs_db
def test_a_longer_bound_set_first_is_not_cut_short_by_a_shorter_one_set_after_it(
    dsn: str, owner: int
) -> None:
    """thl AC1, the mirror of the test above, and ADR RQ-8(a)'s deciding case.

    The test above sets the bounds ascending (1 s, then 30 s) and the
    transaction dies at one second. That falsifies "the last value set wins"
    and nothing else: "the first value wins" and "the shortest value wins"
    predict the same death. This one sets them DESCENDING — 30 s first, then
    1 s — where the two part: first-wins predicts the transaction survives a
    two-second sleep, shortest-wins (and last-wins) predict it dies at one
    second. The two tests together tell the three rules apart; neither alone
    does.

    **The control.** The verdict here is the absence of an exception, so it
    rests on three things that each show a presence:
    `test_the_first_transaction_bound_a_transaction_sets_is_the_one_that_fires`
    is the observation that a `transaction_timeout` set inside a transaction
    does arm and does end the session on this server; the `SELECT 1` on the
    same connection straight after the sleep is the survival observed inside
    the block rather than inferred from a clean exit; and the elapsed time is
    asserted, so a `pg_sleep` that did not run cannot pass as survival. Both
    bounds are asserted to have reached the transaction before the sleep, and
    the write is read back on a fresh connection afterwards.
    """
    db = _database(dsn)
    participants = PostgresParticipantStore()
    with db.transaction() as unit:
        seat = participants.add(unit, CAMPAIGN, alias="Rook")

    with db.transaction() as unit:
        unit.lock_campaign(CAMPAIGN, shared=False, transaction_timeout_s=30)
        participants.hold(unit, seat.id, campaign_id=CAMPAIGN, transaction_timeout_s=1)
        assert unit.transaction_bounds == ["30s", "1s"]
        assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "1s", (
            "the later value is what the server REPORTS"
        )
        assert unit.advance_authz_revision(CAMPAIGN) == 1
        started = time.monotonic()
        unit.conn.execute("SELECT pg_sleep(2)")
        assert unit.conn.execute("SELECT 1").fetchone()[0] == 1, "the session outlived the one-second bound"
        assert time.monotonic() - started >= 2, "the sleep really ran past the shorter bound"

    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,)
        ).fetchone()[0] == 1, "the transaction committed its write"


@needs_db
def test_a_participant_only_transaction_is_bounded_like_every_other_holder(dsn: str, owner: int) -> None:
    """RQ-6 rests on "every holder is bounded by `transaction_timeout`", and a
    participant row can be held without the campaign lock ever being taken.
    Before `hold` bounded it, that was the one transaction in the schema with no
    bound at all."""
    db = _database(dsn, CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=9))
    participants = PostgresParticipantStore()
    with db.transaction() as unit:
        seat = participants.add(unit, CAMPAIGN, alias="Rook")
    with db.transaction() as unit:
        assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "0", "not yet"
        participants.hold(unit, seat.id, campaign_id=CAMPAIGN)
        assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "9s"


@needs_db
def test_a_transaction_that_outlives_its_bound_is_ended_rather_than_left_holding(
    dsn: str, owner: int
) -> None:
    """The bound is not decoration: a holder that stops making progress must let
    go. What PostgreSQL 17 does at `transaction_timeout` is end the session, so
    what this asserts is that the transaction fails and its write is not kept —
    not which exception class the driver raises for it."""
    db = _database(dsn, CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=1))
    with pytest.raises(psycopg.Error):
        with db.transaction() as unit:
            unit.lock_campaign(CAMPAIGN, shared=False)
            unit.advance_authz_revision(CAMPAIGN)
            unit.conn.execute("SELECT pg_sleep(4)")

    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,)
        ).fetchone()[0] == 0, "a transaction cut short by its bound keeps nothing"


# ── Behaviour 32 — why every store row lock is FOR NO KEY UPDATE (RQ-3) ──────


def _a_participant_row(dsn: str) -> None:
    """By raw SQL, with a known id — this test locks the row directly rather
    than through a store. Named apart from the suite's `_a_participant` below:
    two helpers of the same name in one module means the later one wins, and
    nothing but a real run says so."""
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO campaign.participants (id, campaign_id, alias, alias_key) "
            "VALUES (%s, %s, 'Rook', 'rook')",
            (PARTICIPANT, CAMPAIGN),
        )


@contextmanager
def _holding_a_row(dsn: str, statement: str, params: tuple) -> Iterator[None]:
    """A second connection that runs `statement` and then keeps its transaction
    open, so a test can watch what the statement's locks do to somebody else."""
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
    if failure:
        raise failure[0]


def _insert_a_sheet_referencing_the_participant(dsn: str) -> None:
    """A character sheet linked to the seat (0008). The foreign-key check this
    makes takes FOR KEY SHARE on the participant. It was an enrolment code until
    0009 dropped that table; the sheet is the reference to a seat that remains."""
    with connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL lock_timeout = '2s'")
        conn.execute(
            "INSERT INTO campaign.documents (id, campaign_id, type, type_version, data, "
            "write_revision, field_revisions, name_key, search_key, linked_participant_id) "
            "VALUES (%s, %s, 'character_sheet', 1, '{}', 1, '{}', '', '', %s)",
            ("doc_" + "a" * 22, CAMPAIGN, PARTICIPANT),
        )
        conn.commit()


@needs_db
def test_a_participant_locked_for_no_key_update_does_not_block_a_row_that_references_it(
    dsn: str, owner: int
) -> None:
    """RQ-3's reason, shown once. A display writing a participant's slot holds
    that participant's row; a join inserts a row that references it. With
    `FOR UPDATE` the second waits for the first — which is how a Stop ends up
    waiting behind a join, and how two of them deadlock. `FOR NO KEY UPDATE`
    does not conflict with the `FOR KEY SHARE` a foreign-key check takes, so
    every explicit row lock a store takes is that one."""
    _a_participant_row(dsn)
    held = "SELECT id FROM campaign.participants WHERE id = %s FOR NO KEY UPDATE"
    with _holding_a_row(dsn, held, (PARTICIPANT,)):
        _insert_a_sheet_referencing_the_participant(dsn)

    with connect(dsn) as conn:
        conn.execute("DELETE FROM campaign.documents")

    stronger = "SELECT id FROM campaign.participants WHERE id = %s FOR UPDATE"
    with _holding_a_row(dsn, stronger, (PARTICIPANT,)):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _insert_a_sheet_referencing_the_participant(dsn)


def _a_session_row(dsn: str, owner: int) -> None:
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO campaign.table_sessions "
            "(id, campaign_id, gm_user_id, state, expires_at) "
            "VALUES (%s, %s, %s, 'live', now() + interval '12 hours')",
            (SESSION, CAMPAIGN, owner),
        )


def _insert_a_screen_grant_referencing_the_session(dsn: str) -> None:
    with connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL lock_timeout = '2s'")
        conn.execute(
            "INSERT INTO campaign.table_credentials "
            "(id, session_id, link_generation, credential_digest) VALUES (%s, %s, 1, %s)",
            ("tcr_" + "a" * 22, SESSION, "2" * 64),
        )
        conn.commit()


@needs_db
def test_rotating_does_not_block_a_screen_grant_that_references_the_session(
    dsn: str, owner: int
) -> None:
    """Why `rotate_command_id` has no index at all, and the start index is
    PARTIAL (0016, RQ-3).

    PostgreSQL treats the columns of a non-partial unique index as key columns,
    so an index over `rotate_command_id` would turn Rotate's `UPDATE ... SET
    link_generation = ..., rotate_command_id = ...` into a `FOR UPDATE` on the
    session row — which conflicts with the `FOR KEY SHARE` every screen-grant
    insert's foreign-key check takes, and a mint would start waiting behind a
    Rotate. So it is asserted here rather than reasoned about: Rotate's own
    UPDATE lets the referencing insert through, and a real `FOR UPDATE` does not.
    """
    _a_session_row(dsn, owner)
    rotate = (
        "UPDATE campaign.table_sessions "
        "SET link_generation = link_generation + 1, rotate_command_id = %s WHERE id = %s"
    )
    with _holding_a_row(dsn, rotate, (_command(), SESSION)):
        _insert_a_screen_grant_referencing_the_session(dsn)

    with connect(dsn) as conn:
        conn.execute("DELETE FROM campaign.table_credentials")

    stronger = "SELECT id FROM campaign.table_sessions WHERE id = %s FOR UPDATE"
    with _holding_a_row(dsn, stronger, (SESSION,)):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _insert_a_screen_grant_referencing_the_session(dsn)


@needs_db
def test_an_ordinary_campaign_update_does_not_block_a_row_that_references_it(
    dsn: str, owner: int
) -> None:
    """The other half of the same question, asked of the new `UNIQUE (id,
    owner_id)` on `campaigns`. It exists so that `table_sessions` can carry a
    composite foreign key to it, and it makes `owner_id` a key column — so the
    thing to check is that updating a column that is NOT in it (here
    `updated_at`, and the same holds for `name` and `archived_at`) still takes
    only `FOR NO KEY UPDATE` and lets a participant insert through."""
    rename = "UPDATE campaign.campaigns SET updated_at = now() WHERE id = %s"
    with _holding_a_row(dsn, rename, (CAMPAIGN,)):
        with connect(dsn, autocommit=False) as conn:
            conn.execute("SET LOCAL lock_timeout = '2s'")
            conn.execute(
                "INSERT INTO campaign.participants (id, campaign_id, alias, alias_key) "
                "VALUES (%s, %s, 'Rook', 'rook')",
                (PARTICIPANT, CAMPAIGN),
            )
            conn.commit()


@needs_db
def test_the_database_refuses_a_session_whose_gm_is_not_the_campaigns_owner(dsn: str, owner: int) -> None:
    """AUD-1 held by the database rather than by a route remembering to compare
    two values: `(campaign_id, gm_user_id)` references `campaigns (id,
    owner_id)`, so a session for somebody else's campaign is a foreign-key
    violation however it was composed."""
    with connect(dsn) as conn:
        stranger = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('other@example.com', 'x') "
            "RETURNING id"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO campaign.table_sessions "
                "(id, campaign_id, gm_user_id, state, expires_at) "
                "VALUES (%s, %s, %s, 'live', now() + interval '12 hours')",
                (SESSION, CAMPAIGN, stranger),
            )


# ── The shared behavioural suite ─────────────────────────────────────────────
#
# One set of behaviours, run twice: against the in-memory twin, and against
# PostgreSQL. A rule that only one of them keeps is a rule the two will drift
# apart on, and the drift shows up in the bead that composes them, not here.
# The `postgres` parameter carries `needs_db`, so without a server it skips and
# the `fake` parameter still runs.


@dataclass
class World:
    """The three stores and the audit log over one database, and two GMs."""

    kind: str
    db: Any
    campaigns: Any
    participants: Any
    sessions: Any
    audit: Any
    owner: int
    other_owner: int
    #: Three accounts that own nothing: the people a GM offers seats to.
    players: tuple[int, int, int]
    #: Every `(unit, session_id)` the slot-clearing extension point was called with.
    slot_clears: list[tuple[Any, str]]
    #: The job outbox over the same database (1kg.2.3): the expiry job and the
    #: reconciliations the lifecycle enqueues.
    jobs: Any = None


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    clears: list[tuple[Any, str]] = []

    def record(unit: Any, session_id: str) -> None:
        clears.append((unit, session_id))

    if request.param == "fake":
        db: Any = InMemoryDatabase()
        yield World(
            "fake",
            db,
            InMemoryCampaignStore(db),
            InMemoryParticipantStore(db),
            InMemoryTableSessionStore(db, slot_clear=record),
            InMemoryAuditLog(),
            owner=1,
            other_owner=2,
            players=(3, 4, 5),
            slot_clears=clears,
            jobs=InMemoryJobQueue(db=db),
        )
        return

    target = request.getfixturevalue("dsn")
    with connect(target) as conn:
        gms = [
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
                (email,),
            ).fetchone()[0]
            for email in (
                "gm@example.com",
                "other@example.com",
                "p1@example.com",
                "p2@example.com",
                "p3@example.com",
            )
        ]
    database = _database(target)
    yield World(
        "postgres",
        database,
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresTableSessionStore(slot_clear=record),
        PostgresAuditLog(),
        owner=int(gms[0]),
        other_owner=int(gms[1]),
        players=(int(gms[2]), int(gms[3]), int(gms[4])),
        slot_clears=clears,
        jobs=PostgresJobQueue(database),
    )


def _a_campaign(world: World, *, owner: int | None = None, name: str = "Nocturne") -> str:
    with world.db.transaction() as unit:
        return world.campaigns.create(
            unit, owner_id=world.owner if owner is None else owner, name=name
        ).id


def _a_participant(world: World, campaign_id: str, alias: str = "Rook") -> str:
    with world.db.transaction() as unit:
        return world.participants.add(unit, campaign_id, alias=alias).id


def _a_session(
    world: World,
    campaign_id: str,
    *,
    hours: int = 12,
    owner: int | None = None,
    started_ago_h: int = 0,
):
    """A session that started `started_ago_h` ago and lives `hours` from then —
    two numbers rather than one because `expires_at > started_at` is a CHECK,
    so an overdue session is one that STARTED long enough ago. Each is started
    by a command of its own, and no secret comes back (1kg.2.3)."""
    started = datetime.now(UTC) - timedelta(hours=started_ago_h)
    with world.db.transaction() as unit:
        return world.sessions.start(
            unit,
            campaign_id,
            owner_id=world.owner if owner is None else owner,
            expires_at=started + timedelta(hours=hours),
            command_id=_command(),
            now=started,
        )


# Behaviour 9 — the tracer.


def test_creating_a_campaign_creates_its_authorisation_row_at_revision_zero(world: World) -> None:
    """The one path a campaign can come into being by, in both worlds: in
    PostgreSQL the AFTER INSERT trigger writes the row; in the twin the fake's
    `create` does, which is the only way it has of making a campaign. The case
    the twin cannot have — a raw SQL insert — is proved in
    `tests/test_migrations_db.py`."""
    with world.db.transaction() as unit:
        campaign = world.campaigns.create(unit, owner_id=world.owner, name="Nocturne")
        assert campaign.owner_id == world.owner
        assert campaign.archived_at is None
        assert world.campaigns.authz_revision(unit, campaign.id) == 0
        assert world.campaigns.get(unit, campaign.id, owner_id=world.owner) == campaign


def test_a_campaign_of_another_owner_is_indistinguishable_from_one_that_is_not_there(
    world: World,
) -> None:
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        assert world.campaigns.get(unit, campaign, owner_id=world.other_owner) is None
        assert world.campaigns.list_for_owner(unit, world.other_owner) == []
        assert not world.campaigns.set_archived(
            unit, campaign, owner_id=world.other_owner, archived=True
        )


# Behaviours 10 and 11 — aliases, and removal that never deletes.


def test_an_alias_is_unique_within_its_campaign_and_case_does_not_help(world: World) -> None:
    campaign, elsewhere = _a_campaign(world), _a_campaign(world, name="Other")
    _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        with pytest.raises(AliasTaken):
            world.participants.add(unit, campaign, alias="ROOK")
    # Another campaign is another table: the same alias is free there.
    with world.db.transaction() as unit:
        assert world.participants.add(unit, elsewhere, alias="Rook").campaign_id == elsewhere


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("Ana", "Ana ", id="a-trailing-space"),
        pytest.param("Ana", " Ana", id="a-leading-space"),
        pytest.param("Wren the Unseen", "Wren  the  Unseen", id="a-doubled-space"),
        pytest.param(
            # Written with an explicit code point: a combining acute in the
            # source is invisible, and this file must not be the place a
            # reviewer has to notice one.
            unicodedata.normalize("NFC", "Zoe" + chr(0x301)),
            unicodedata.normalize("NFD", "Zoe" + chr(0x301)),
            id="composed-and-decomposed",
        ),
        pytest.param("Straße", "STRASSE", id="a-sharp-s-and-its-expansion"),
        pytest.param("ΑΣ", "ας", id="a-final-sigma"),
        # The one pair only NFKC separates: casefold alone leaves these two
        # apart, so without the compatibility normalisation `alias_key` is
        # doing nothing here and every other pair in this list still passes.
        pytest.param("Ａｎａ", "Ana", id="full-width-and-ascii"),
        # ysj: invisible characters OUTSIDE category C, which `check_alias`
        # accepts, so only `alias_key` can make these collide — and the two
        # joiners the lead ruling of 2026-09-21 allows in a stored alias.
        pytest.param("Ana", "Ana" + chr(0x034F), id="a-combining-grapheme-joiner"),
        pytest.param("Ana", "Ana" + chr(0xFE0F), id="a-variation-selector-16"),
        pytest.param("Ana", "Ana" + chr(0x3164), id="a-hangul-filler"),
        pytest.param("Ana", "Ana" + chr(0x2800), id="a-braille-blank"),
        pytest.param("Ana", "An" + chr(0x200D) + "a", id="a-zero-width-joiner"),
        pytest.param("Ana", "An" + chr(0x200C) + "a", id="a-zero-width-non-joiner"),
        # A grapheme joiner between a letter and its accent stops NFC and NFKC
        # composing them, so a key that only dropped it AFTER normalising would
        # hold a decomposed accent where the other holds a composed one.
        pytest.param(
            unicodedata.normalize("NFC", "Zoe" + chr(0x301)),
            "Zoe" + chr(0x034F) + chr(0x301),
            id="a-joiner-inside-an-accent",
        ),
    ],
)
def test_two_aliases_a_gm_could_not_tell_apart_cannot_both_be_seated(
    world: World, first: str, second: str
) -> None:
    """F-12. Through CASE the partial unique index always refused them; through
    whitespace and normalisation it did not, in either world — and `lower()` and
    `str.lower()` disagreed about a final sigma, so the two worlds could not
    even agree on which pairs collided. `alias_key` is computed by the
    application (NFKC then casefold), so the answer is the same everywhere."""
    campaign = _a_campaign(world)
    _a_participant(world, campaign, first)
    with world.db.transaction() as unit:
        with pytest.raises(AliasTaken):
            world.participants.add(unit, campaign, alias=second)


def test_an_alias_whose_key_folds_past_the_columns_bound_is_refused_in_both_worlds(
    world: World,
) -> None:
    """G-1. NFKC expands, and `alias_key` is what the column holds: one
    `U+FDFA` is a single character a GM can type and eighteen in the key, so a
    legal twelve-character alias folds to 216 — past the 200
    `0004_campaign_schema.sql` allows. The twin seated it; PostgreSQL refused
    the INSERT with a check violation whose DETAIL quotes the failing row, alias
    included, and left the transaction aborted. The bound belongs in
    `check_alias`, which already owns the alias's own bound."""
    campaign = _a_campaign(world)
    overflowing = "ﷺ" * 12
    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="folds to at most") as refused:
            world.participants.add(unit, campaign, alias=overflowing)
        assert overflowing not in str(refused.value), "a refusal never repeats private text"
        assert world.participants.list_for_campaign(unit, campaign) == [], "nothing was seated"
        # The refusal is raised before any statement, so this transaction is
        # still usable — which is exactly what a driver's error would not leave.
        assert world.participants.add(unit, campaign, alias="Rook").alias == "Rook"


#: Written by code point: an invisible character in this file is one a reviewer
#: would have to notice, and the whole point of these names is that it is kept.
_NAMES_THAT_NEED_A_JOINER = {
    # "Alireza" as Persian writes it: ZWNJ keeps the two halves unjoined.
    "a-persian-name-with-a-non-joiner": "".join(
        chr(code) for code in (0x0639, 0x0644, 0x06CC, 0x200C, 0x0631, 0x0636, 0x0627)
    ),
    # "Wren" and the woman-mage emoji: a ZWJ sequence ending in VS16.
    "an-emoji-zwj-sequence": "Wren " + "".join(chr(code) for code in (0x1F9D9, 0x200D, 0x2640, 0xFE0F)),
}


@pytest.mark.parametrize("alias", list(_NAMES_THAT_NEED_A_JOINER.values()), ids=list(_NAMES_THAT_NEED_A_JOINER))
def test_a_name_that_needs_a_joiner_is_seated_as_written(world: World, alias: str) -> None:
    """ysj, and the lead ruling of 2026-09-21: U+200C and U+200D are
    orthographically required in real names and in emoji sequences, so they are
    kept in the STORED alias — in both worlds — and only folded out of the key."""
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        assert world.participants.add(unit, campaign, alias=alias).alias == alias
    with world.db.transaction() as unit:
        [seated] = world.participants.list_for_campaign(unit, campaign)
        assert seated.alias == alias, "the joiner is kept in what is stored"


@pytest.mark.parametrize(
    "invisible",
    [chr(0x3164), chr(0x2800), chr(0x034F), chr(0x200D), chr(0x200C) + chr(0xFE0F)],
    ids=["a-hangul-filler", "a-braille-blank", "a-grapheme-joiner", "a-joiner", "a-non-joiner-and-a-selector"],
)
def test_an_alias_with_nothing_visible_in_it_is_refused_in_both_worlds(world: World, invisible: str) -> None:
    """Folding the invisible characters out of `alias_key` can leave it empty,
    and `0004_campaign_schema.sql` bounds the key at 1 to 200: PostgreSQL would
    refuse the INSERT with a check violation whose DETAIL quotes the row, alias
    included, while the twin seated it. An alias a GM cannot see is not one."""
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="at least one visible character") as refused:
            world.participants.add(unit, campaign, alias=invisible)
        assert invisible not in str(refused.value), "a refusal never repeats private text"
        assert world.participants.list_for_campaign(unit, campaign) == [], "nothing was seated"
        assert world.participants.add(unit, campaign, alias="Rook").alias == "Rook"


def test_an_alias_is_stored_normalised_and_a_blank_one_is_not_an_alias(world: World) -> None:
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        seated = world.participants.add(unit, campaign, alias="  Wren   the Unseen  ")
        assert seated.alias == "Wren the Unseen", "trimmed, and inner runs collapsed"
        for blank in ("", "   ", "\t\n"):
            with pytest.raises(ValueError, match="1 to 40 characters"):
                world.participants.add(unit, campaign, alias=blank)


def test_a_removed_participant_frees_their_alias_and_keeps_their_row(world: World) -> None:
    """RQ-3/W-3: marked removed, never deleted — a row another transaction may
    be referencing must not vanish under it, and an audit row that names the
    removal must still resolve."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, seat)
        assert not world.participants.remove(unit, campaign, seat), "removing twice is not an error"

        gone = world.participants.get(unit, seat)
        assert gone is not None and gone.removed_at is not None
        assert world.participants.list_for_campaign(unit, campaign) == []
        assert [p.id for p in world.participants.list_for_campaign(
            unit, campaign, include_removed=True
        )] == [seat]

        again = world.participants.add(unit, campaign, alias="rook")
        assert again.id != seat, "a removed alias is free again, on a new seat"


def test_a_participant_of_another_campaign_cannot_be_changed_through_this_one(world: World) -> None:
    """`docs/migrations.md` section 4: ownership belongs in the query. Ids are
    not secrets (SEC-4), so a route in 1kg.2.2 that forgot one comparison would
    otherwise let GM A remove GM B's participant, or offer B's seat, by id."""
    mine, theirs = _a_campaign(world), _a_campaign(world, owner=world.other_owner, name="Theirs")
    seat = _a_participant(world, theirs, "Rook")
    nobody = "prt_" + "z" * 22
    player = world.players[0]
    with world.db.transaction() as unit:
        assert not world.participants.remove(unit, mine, seat)
        with pytest.raises(SeatUnavailable):
            world.participants.offer(unit, mine, seat, user_id=player)

        # The same answers a participant that is not there gets, which is the
        # point: the seat of another campaign must be indistinguishable from one
        # that does not exist, now that the mutators hold the row themselves.
        assert not world.participants.remove(unit, mine, nobody)
        with pytest.raises(SeatUnavailable):
            world.participants.offer(unit, mine, nobody, user_id=player)
    with world.db.transaction() as unit:
        still = world.participants.get(unit, seat)
        assert still is not None and still.is_active and still.user_id is None


# Seats — an account offered a seat, and accepting it (agent-forge-harness-fma).
#
# D-1 and D-4: every player holds an account and there are no guests, so a
# participant is an account's seat at a campaign. A seat is open (an alias the
# GM seated while preparing), offered (to one account), accepted (by that
# account) or removed. Only offer-then-accept is built; claiming an open seat by
# any other proof would be the retired enrolment code under a new name.


def _offer(world: World, campaign_id: str, seat: str, player: int) -> None:
    with world.db.transaction() as unit:
        world.participants.offer(unit, campaign_id, seat, user_id=player)


def _seat(world: World, campaign_id: str, alias: str, player: int, *, now: datetime | None = None) -> str:
    """A seat offered to `player` and accepted by them."""
    seat = _a_participant(world, campaign_id, alias)
    _offer(world, campaign_id, seat, player)
    with world.db.transaction() as unit:
        assert world.participants.accept(unit, campaign_id, seat, user_id=player, now=now) is True
    return seat


def test_a_seat_is_open_then_offered_then_accepted_and_only_then_is_it_a_seat(world: World) -> None:
    """L-1 and L-10. An offered seat is not a seat yet: `seat_for` answers only
    for one the account has accepted, so nothing a route authorises by it can
    be reached by an account that was merely offered one."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    player, bystander = world.players[0], world.players[1]
    with world.db.transaction() as unit:
        opened = world.participants.get(unit, seat)
        assert opened is not None
        assert (opened.user_id, opened.accepted_at, opened.is_accepted) == (None, None, False)

        offered = world.participants.offer(unit, campaign, seat, user_id=player)
        assert (offered.id, offered.user_id, offered.accepted_at) == (seat, player, None)
        assert not offered.is_accepted and offered.is_active
        assert world.participants.seat_for(unit, campaign, player) is None, "offered is not seated"
        assert world.participants.seats_for_user(unit, player) == []

    with world.db.transaction() as unit:
        assert world.participants.accept(unit, campaign, seat, user_id=player) is True

    with world.db.transaction() as unit:
        mine = world.participants.seat_for(unit, campaign, player)
        assert mine is not None and mine.id == seat and mine.user_id == player
        assert mine.is_accepted and mine.accepted_at is not None
        assert [p.id for p in world.participants.seats_for_user(unit, player)] == [seat]
        assert world.participants.seat_for(unit, campaign, bystander) is None
        assert world.participants.seats_for_user(unit, bystander) == []
        assert [p.id for p in world.participants.list_for_campaign(unit, campaign)] == [seat]


def test_every_refusal_of_an_offer_or_an_accept_is_the_same_refusal(world: World) -> None:
    """L-4 and L-5, and SEC-3. Every way an offer or an acceptance can be
    refused raises `SeatUnavailable` with one message, byte for byte — so a
    seat that does not exist, one in another GM's campaign, one that is taken
    and one that is removed cannot be told apart by the caller — and none of
    them changes anything.

    The cases include the three the evaluator names: no path accepts a seat not
    offered to that account, re-opens an accepted seat, or seats the campaign's
    owner. And a stranger's acceptance of a seat somebody else has accepted is
    refused like the rest rather than answered False — only the account that
    accepted it may hear "already done", or a stranger learns the seat is
    taken."""
    campaign = _a_campaign(world)
    elsewhere = _a_campaign(world, owner=world.other_owner, name="Theirs")
    first, second, third = world.players
    nobody = "prt_" + "z" * 22

    open_seat = _a_participant(world, campaign, "Open")
    spare = _a_participant(world, campaign, "Spare")
    left = _a_participant(world, campaign, "Left")
    _offer(world, campaign, left, third)
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, left)
    gone = _a_participant(world, campaign, "Gone")
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, gone)
    offered = _a_participant(world, campaign, "Offered")
    _offer(world, campaign, offered, first)
    taken = _seat(world, campaign, "Taken", second)
    theirs_open = _a_participant(world, elsewhere, "Stranger")
    theirs_offered = _a_participant(world, elsewhere, "Guest")
    _offer(world, elsewhere, theirs_offered, first)

    p = world.participants
    cases: list[tuple[str, Callable[[Any], object]]] = [
        ("accept by an account it was not offered to",
         lambda u: p.accept(u, campaign, offered, user_id=second)),
        ("accept of an open seat nobody was offered",
         lambda u: p.accept(u, campaign, open_seat, user_id=first)),
        ("accept of a removed seat by the account it was offered to",
         lambda u: p.accept(u, campaign, left, user_id=third)),
        ("accept of another campaign's seat",
         lambda u: p.accept(u, campaign, theirs_offered, user_id=first)),
        ("accept of a seat that does not exist",
         lambda u: p.accept(u, campaign, nobody, user_id=first)),
        ("accept of a seat another account has accepted",
         lambda u: p.accept(u, campaign, taken, user_id=first)),
        ("offer of a removed seat", lambda u: p.offer(u, campaign, gone, user_id=first)),
        ("offer of a seat already offered", lambda u: p.offer(u, campaign, offered, user_id=third)),
        ("offer of an accepted seat", lambda u: p.offer(u, campaign, taken, user_id=third)),
        ("offer to the campaign's owner", lambda u: p.offer(u, campaign, open_seat, user_id=world.owner)),
        ("offer to an account seated there", lambda u: p.offer(u, campaign, spare, user_id=second)),
        ("offer to an account offered a seat there", lambda u: p.offer(u, campaign, spare, user_id=first)),
        ("offer of another campaign's seat", lambda u: p.offer(u, campaign, theirs_open, user_id=third)),
        ("offer of a seat that does not exist", lambda u: p.offer(u, campaign, nobody, user_id=third)),
    ]
    messages: dict[str, str] = {}
    for name, attempt in cases:
        with world.db.transaction() as unit:
            with pytest.raises(SeatUnavailable) as refused:
                attempt(unit)
        messages[name] = str(refused.value)
    assert len(messages) == len(cases) == 14
    assert len(set(messages.values())) == 1, messages
    assert next(iter(messages.values())), "one message, and not an empty one"

    with world.db.transaction() as unit:
        states = {
            seat: (found.user_id, found.accepted_at is not None, found.is_active)
            for seat in (open_seat, spare, left, gone, offered, taken, theirs_open, theirs_offered)
            if (found := p.get(unit, seat)) is not None
        }
    assert states == {
        open_seat: (None, False, True),
        spare: (None, False, True),
        left: (third, False, False),
        gone: (None, False, False),
        offered: (first, False, True),
        taken: (second, True, True),
        theirs_open: (None, False, True),
        theirs_offered: (first, False, True),
    }, "a refusal changes nothing"


def test_an_accounts_seat_answers_for_its_own_campaign_and_no_other(world: World) -> None:
    """`seat_for` is what a table route will authorise by, so it must name the
    campaign as well as the account: a player seated at A is nobody at B, even
    though B has seats of its own — one of them accepted by somebody else, and
    one merely offered to this player."""
    player, other = world.players[0], world.players[1]
    table_a = _a_campaign(world, name="A")
    table_b = _a_campaign(world, owner=world.other_owner, name="B")
    mine = _seat(world, table_a, "Rook", player)
    _seat(world, table_b, "Wren", other)
    _offer(world, table_b, _a_participant(world, table_b, "Rook"), player)
    with world.db.transaction() as unit:
        assert world.participants.seat_for(unit, table_b, player) is None
        found = world.participants.seat_for(unit, table_a, player)
        assert found is not None and found.id == mine and found.campaign_id == table_a
        assert world.participants.seat_for(unit, table_a, other) is None


def test_accepting_a_seat_twice_is_not_an_error_and_changes_nothing(world: World) -> None:
    """L-5, the way `remove` twice is not an error: the account already has what
    it asked for, and the moment it first accepted is kept."""
    campaign = _a_campaign(world)
    player = world.players[0]
    first_time = datetime.now(UTC) - timedelta(hours=1)
    seat = _seat(world, campaign, "Rook", player, now=first_time)
    with world.db.transaction() as unit:
        assert world.participants.accept(unit, campaign, seat, user_id=player) is False
    with world.db.transaction() as unit:
        kept = world.participants.get(unit, seat)
        assert kept is not None and kept.accepted_at == first_time and kept.user_id == player


def test_a_removed_seat_is_nobodys_and_its_account_may_be_seated_again(world: World) -> None:
    """Removal marks the seat — the account and the moment it accepted stay on
    the row, for the audit rows and the character-sheet link that name it — and
    it is then nobody's seat: `seat_for` and `seats_for_user` stop answering,
    accepting it again is refused rather than reported as already done, and the
    partial unique index lets the same account be offered a NEW seat there."""
    campaign = _a_campaign(world)
    player = world.players[0]
    seat = _seat(world, campaign, "Rook", player)
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, seat)

    with world.db.transaction() as unit:
        assert world.participants.seat_for(unit, campaign, player) is None
        assert world.participants.seats_for_user(unit, player) == []
        gone = world.participants.get(unit, seat)
        assert gone is not None and not gone.is_active and not gone.is_accepted
        assert gone.user_id == player and gone.accepted_at is not None, "marked, never cleared"
        with pytest.raises(SeatUnavailable):
            world.participants.accept(unit, campaign, seat, user_id=player)

    again = _seat(world, campaign, "Rook", player)
    assert again != seat
    with world.db.transaction() as unit:
        found = world.participants.seat_for(unit, campaign, player)
        assert found is not None and found.id == again


def test_an_accounts_seats_are_listed_most_recently_accepted_first(world: World) -> None:
    """The "enter the tavern" list: accepted, live seats only, newest acceptance
    first, across every campaign and every GM."""
    player = world.players[0]
    oldest, newest = _a_campaign(world, name="Oldest"), _a_campaign(world, name="Newest")
    between = _a_campaign(world, owner=world.other_owner, name="Between")
    offered_only, removed = _a_campaign(world, name="Offered"), _a_campaign(world, name="Removed")
    now = datetime.now(UTC)
    first = _seat(world, oldest, "Rook", player, now=now - timedelta(hours=3))
    third = _seat(world, newest, "Rook", player, now=now - timedelta(hours=1))
    second = _seat(world, between, "Rook", player, now=now - timedelta(hours=2))
    _offer(world, offered_only, _a_participant(world, offered_only, "Rook"), player)
    dropped = _seat(world, removed, "Rook", player, now=now)
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, removed, dropped)

    with world.db.transaction() as unit:
        listed = world.participants.seats_for_user(unit, player)
    assert [p.id for p in listed] == [third, second, first]
    assert all(p.user_id == player and p.is_accepted for p in listed)


def test_a_seat_accepted_by_no_account_cannot_exist_in_the_twin() -> None:
    """L-1's CHECK, kept by the record itself so that no twin path can build a
    row PostgreSQL would refuse. The database half is
    `test_the_database_refuses_a_seat_accepted_by_no_account`."""
    moment = datetime.now(UTC)
    with pytest.raises(ValueError, match="accepted by an account"):
        Participant("prt_x", "cmp_x", "Rook", moment, accepted_at=moment)
    assert Participant("prt_x", "cmp_x", "Rook", moment, user_id=3, accepted_at=moment).is_accepted


PRIVATE_ALIAS = "Wren the Unseen"
PRIVATE_EMAIL = "wren.hidden@example.com"
PRIVATE_USER_ID = 987654321


def _a_private_account(world: World) -> int:
    """An account whose id would stand out in any text it leaked into."""
    if world.kind == "postgres":
        with world.db.transaction() as unit:
            unit.conn.execute(
                "INSERT INTO auth.users (id, email, password_hash) VALUES (%s, %s, 'x')",
                (PRIVATE_USER_ID, PRIVATE_EMAIL),
            )
    return PRIVATE_USER_ID


def test_no_seat_refusal_names_the_alias_the_account_or_the_seat(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """L-5 and AC-9 (SEC-20, SEC-3). Every refusal path, fed a private-looking
    alias and account, says nothing a log line may not carry: no alias, email,
    user id, participant id or campaign id in the message, in the whole printed
    traceback — which is where a chained driver error would put its DETAIL
    line, quoting the key values — or in anything logged meanwhile."""
    private = _a_private_account(world)
    campaign = _a_campaign(world, name="The Nocturne of Vex")
    seat = _a_participant(world, campaign, PRIVATE_ALIAS)
    spare = _a_participant(world, campaign, "Spare")
    _offer(world, campaign, seat, private)
    p = world.participants
    attempts: list[Callable[[Any], object]] = [
        lambda u: p.offer(u, campaign, seat, user_id=world.players[0]),
        lambda u: p.offer(u, campaign, spare, user_id=private),
        lambda u: p.accept(u, campaign, seat, user_id=world.players[0]),
        lambda u: p.accept(u, campaign, spare, user_id=private),
        lambda u: p.offer(u, campaign, spare, user_id=world.owner),
    ]
    spoken: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for attempt in attempts:
            with world.db.transaction() as unit:
                with pytest.raises(SeatUnavailable) as refused:
                    attempt(unit)
            spoken.append("".join(traceback.format_exception(refused.value)))
            # `from None` only hides a chained error from the printed traceback:
            # the driver's error, DETAIL and all, would still be on
            # `__context__` for anything that walks it. There must be none.
            assert refused.value.__context__ is None and refused.value.__cause__ is None
    assert len(spoken) == len(attempts)
    text = "\n".join([*spoken, caplog.text])
    for kind, value in (
        ("alias", PRIVATE_ALIAS),
        ("email", PRIVATE_EMAIL),
        ("user id", str(PRIVATE_USER_ID)),
        ("participant id", seat),
        ("participant id", spare),
        ("campaign id", campaign),
        ("campaign name", "The Nocturne of Vex"),
    ):
        assert value not in text, f"a {kind} reached a message"


#: The four methods that change a participant's own row, each called the way a
#: caller who has composed nothing else would call it. Every one of them takes
#: the seat's row lock itself (G-6), naming the campaign there, so the
#: participant-first order RQ-3 asks for is a property of the store and not of
#: every caller. `accept` needs a seat offered to `player` first, and `confirm`
#: one offered to `player` and accepted; the tests that use this dict arrange
#: that before they call it.
PARTICIPANT_MUTATORS: dict[str, Callable[[Any, Any, str, str, int], object]] = {
    "remove": lambda store, unit, campaign, seat, player: store.remove(unit, campaign, seat),
    "offer": lambda store, unit, campaign, seat, player: store.offer(
        unit, campaign, seat, user_id=player
    ),
    "accept": lambda store, unit, campaign, seat, player: store.accept(
        unit, campaign, seat, user_id=player
    ),
    "confirm": lambda store, unit, campaign, seat, player: store.confirm(unit, campaign, seat),
}


#: The state of the seat a mutator is called on (thl AC2): one it may change,
#: one removed in an earlier transaction, and one of another GM's campaign named
#: with this campaign's id.
SEAT_STATES = ("active", "removed", "foreign")


@pytest.mark.parametrize("state", SEAT_STATES)
@pytest.mark.parametrize("mutator", sorted(PARTICIPANT_MUTATORS))
def test_every_participant_mutator_takes_the_seats_row_and_bounds_its_transaction(
    world: World, mutator: str, state: str
) -> None:
    """G-6, and the residue of F-15: every mutator holds the seat's row itself,
    so the order RQ-3 rests on is one no caller can forget, and a bare call is
    bounded like every other holder (RQ-8). A second `FOR NO KEY UPDATE` on a row
    this transaction already holds is free, so a caller's own hold can stay.

    Both halves are read off the unit of work, which keeps them assertable in
    both worlds: the bound the mutator asked for, and the refusal that proves it
    declared a row lock (`lock_campaign` is the first lock a transaction takes,
    RQ-2, so a declared row lock makes a later one illegal). On an active seat
    the call must also have succeeded, so the bound cannot come from a
    refusal's path alone.

    **The seat-state axis (thl AC2).** On a seat that is removed, or that
    belongs to another GM's campaign and is named with this campaign's id,
    every mutator refuses — `remove` answers False, `offer`, `accept` and
    `confirm` raise `SeatUnavailable` — so nothing after the refusal can have bounded the
    transaction or declared a row lock: only the mutator's own leading `hold`
    can. A mutator that answers from an unlocked read before it holds (mutant
    M-R) leaves `transaction_bounds` empty on those cells and goes red here,
    while the active cell stays green. The two unit-of-work assertions sit
    after the refusal, at the call's own indentation — inside the
    `pytest.raises` block they would never run.

    The removed cell removes the seat in an earlier transaction — for `accept`
    after offering it to `player`, so the removal is the only reason left to
    refuse. The foreign cell seats `Rook` in the other GM's campaign (offered
    to `player` there, for `accept`) and calls with this campaign's id, so the
    refusal has one cause: the seat is not in this campaign.
    """
    campaign = _a_campaign(world)
    home = _a_campaign(world, owner=world.other_owner, name="Theirs") if state == "foreign" else campaign
    seat = _a_participant(world, home, "Rook")
    player = world.players[0]
    if mutator in ("accept", "confirm"):
        _offer(world, home, seat, player)
    if mutator == "confirm":
        with world.db.transaction() as unit:
            assert world.participants.accept(unit, home, seat, user_id=player) is True
    if state == "removed":
        with world.db.transaction() as unit:
            assert world.participants.remove(unit, home, seat) is True
    call = PARTICIPANT_MUTATORS[mutator]
    with world.db.transaction() as unit:
        if state == "active":
            outcome = call(world.participants, unit, campaign, seat, player)
            assert outcome is True or isinstance(outcome, Participant), outcome
        elif mutator == "remove":
            assert call(world.participants, unit, campaign, seat, player) is False
        else:
            with pytest.raises(SeatUnavailable):
                call(world.participants, unit, campaign, seat, player)
        assert unit.transaction_bounds[:1] == ["5s"], "bounded before anything else (RQ-8)"
        with pytest.raises(CampaignLockOrder):
            unit.lock_campaign(campaign, shared=True)


def test_a_hold_names_its_campaign_and_answers_for_no_other_ones_seat(world: World) -> None:
    """G-11 and L-7. Ids are not secrets (SEC-4), so an unscoped hold let GM A
    take `FOR NO KEY UPDATE` on GM B's participant row and be handed B's alias.
    The one caller that could not name a campaign — the unauthenticated
    enrolment route — is retired with the enrolment code, so the campaign is now
    required on every hold: another campaign's seat is None, the same answer a
    participant that does not exist gets, and a hold that names none is a
    programming error rather than a wider lock."""
    mine, theirs = _a_campaign(world), _a_campaign(world, owner=world.other_owner, name="Theirs")
    seat = _a_participant(world, theirs, "Rook")
    with world.db.transaction() as unit:
        assert world.participants.hold(unit, seat, campaign_id=mine) is None
        assert world.participants.hold(unit, "prt_" + "z" * 22, campaign_id=mine) is None
        assert world.participants.hold(unit, seat, campaign_id=theirs) is not None

    with world.db.transaction() as unit:
        with pytest.raises(TypeError, match="campaign_id"):
            world.participants.hold(unit, seat)


def test_a_hold_obeys_the_lock_order_and_cannot_be_left_unbounded(world: World) -> None:
    """G-5. RQ-2 and RQ-8 through the participant store, in both worlds. The
    campaign lock is the first lock a transaction takes, so a transaction that
    has held a seat may not go on to take one — `hold` declares its row lock,
    and the refusal is the unit of work's rather than a deadlock later. And the
    bound cannot be switched off through the parameter that exists to raise it:
    `0` is how PostgreSQL spells "no timeout at all", and a value below a
    millisecond rounds to it."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        assert world.participants.hold(unit, seat, campaign_id=campaign) is not None
        with pytest.raises(CampaignLockOrder, match="first lock"):
            unit.lock_campaign(campaign, shared=True)

    with world.db.transaction() as unit:
        for switched_off in (0, -1, 1e-05):
            with pytest.raises(ValueError, match="a transaction bound is from"):
                world.participants.hold(
                    unit, seat, campaign_id=campaign, transaction_timeout_s=switched_off
                )


def test_holding_a_participant_hands_back_the_row_so_the_caller_can_read_it(world: World) -> None:
    """F-1: the lock the ADR says "the caller holds" (RQ-5) is a primitive
    rather than something private to the mutators. It returns the row because
    the caller must test the seat's state UNDER the lock, which is how `offer`
    and `accept` themselves decide."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        held = world.participants.hold(unit, seat, campaign_id=campaign)
        assert held is not None
        assert (held.id, held.campaign_id, held.is_active) == (seat, campaign, True)
        assert world.participants.hold(unit, "prt_" + "z" * 22, campaign_id=campaign) is None


# z9v item 3 — a value of the wrong TYPE is refused in both worlds, by name,
# before any statement. PostgreSQL would answer `operator does not exist` and
# abort the caller's whole transaction; the twin used to answer "not found".

#: (method, the parameter given the wrong type, the wrong value): every
#: identifier and account parameter of every public method, plus `None` (no
#: parameter is optional any more) and a bool (Python counts one as an int).
WRONG_TYPES: list[tuple[str, str, object]] = [
    ("add", "campaign_id", 987654321),
    ("add", "alias", 987654321),
    ("add", "campaign_id", None),
    ("get", "participant_id", 987654321),
    ("get", "participant_id", None),
    ("list_for_campaign", "campaign_id", 987654321),
    ("hold", "participant_id", 987654321),
    ("hold", "campaign_id", 987654321),
    ("remove", "campaign_id", 987654321),
    ("remove", "participant_id", 987654321),
    ("offer", "campaign_id", 987654321),
    ("offer", "participant_id", 987654321),
    ("offer", "user_id", "987654321"),
    ("accept", "campaign_id", 987654321),
    ("accept", "participant_id", 987654321),
    ("accept", "user_id", "987654321"),
    ("seat_for", "campaign_id", 987654321),
    ("seat_for", "user_id", "987654321"),
    ("seats_for_user", "user_id", "987654321"),
    ("seats_for_user", "user_id", True),
    ("confirm", "campaign_id", 987654321),
    ("confirm", "participant_id", 987654321),
    ("confirm", "participant_id", None),
    ("count_live", "campaign_id", 987654321),
    ("page_for_campaign", "campaign_id", 987654321),
    ("seat_page_for_user", "user_id", "987654321"),
    ("seat_page_for_user", "user_id", True),
]

_STORE_CALLS: dict[str, Callable[[Any, Any, dict[str, Any]], object]] = {
    "add": lambda store, unit, a: store.add(unit, a["campaign_id"], alias=a["alias"]),
    "get": lambda store, unit, a: store.get(unit, a["participant_id"]),
    "list_for_campaign": lambda store, unit, a: store.list_for_campaign(unit, a["campaign_id"]),
    "hold": lambda store, unit, a: store.hold(unit, a["participant_id"], campaign_id=a["campaign_id"]),
    "remove": lambda store, unit, a: store.remove(unit, a["campaign_id"], a["participant_id"]),
    "offer": lambda store, unit, a: store.offer(
        unit, a["campaign_id"], a["participant_id"], user_id=a["user_id"]
    ),
    "accept": lambda store, unit, a: store.accept(
        unit, a["campaign_id"], a["participant_id"], user_id=a["user_id"]
    ),
    "seat_for": lambda store, unit, a: store.seat_for(unit, a["campaign_id"], a["user_id"]),
    "seats_for_user": lambda store, unit, a: store.seats_for_user(unit, a["user_id"]),
    "confirm": lambda store, unit, a: store.confirm(unit, a["campaign_id"], a["participant_id"]),
    "count_live": lambda store, unit, a: store.count_live(unit, a["campaign_id"]),
    "page_for_campaign": lambda store, unit, a: store.page_for_campaign(unit, a["campaign_id"]),
    "seat_page_for_user": lambda store, unit, a: store.seat_page_for_user(unit, a["user_id"]),
}


@pytest.mark.parametrize(
    ("method", "parameter", "wrong"),
    WRONG_TYPES,
    ids=[f"{m}-{p}-{type(w).__name__}" for m, p, w in WRONG_TYPES],
)
def test_a_value_of_the_wrong_type_is_refused_by_name_and_the_transaction_goes_on(
    world: World, method: str, parameter: str, wrong: object
) -> None:
    """z9v item 3. Every other argument is valid, so the refusal has one cause.
    It is the built-in `TypeError`, it names the parameter and never the value,
    and — the half only the `postgres` parameter can show, in CI — it arrives
    without a failed statement: the same transaction goes on to seat `Kestrel`
    and commits. Without the check PostgreSQL raises `operator does not exist`
    and the write after it fails on an aborted transaction."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    arguments: dict[str, Any] = {
        "campaign_id": campaign,
        "participant_id": seat,
        "alias": "Wren",
        "user_id": world.players[0],
    }
    arguments[parameter] = wrong
    with world.db.transaction() as unit:
        with pytest.raises(TypeError, match=f"{parameter} is an? (str|int)") as refused:
            _STORE_CALLS[method](world.participants, unit, arguments)
        assert "987654321" not in str(refused.value), "a refusal names the parameter, never the value"
        world.participants.add(unit, campaign, alias="Kestrel")

    with world.db.transaction() as unit:
        seated = sorted(p.alias for p in world.participants.list_for_campaign(unit, campaign))
        assert seated == ["Kestrel", "Rook"]


# Behaviour 14 — one live session per GM, across campaigns.


def test_a_second_live_session_for_the_same_gm_is_refused_across_campaigns(world: World) -> None:
    """REVEAL-2. Not one per campaign: a GM is one person at one table, and two
    live sessions would mean two displays claiming the same authority. The twin
    enforces it too, so it cannot drift away from the partial unique index."""
    here, elsewhere = _a_campaign(world), _a_campaign(world, name="Other")
    _a_session(world, here)
    with world.db.transaction() as unit:
        with pytest.raises(LiveSessionExists):
            world.sessions.start(
                unit,
                elsewhere,
                command_id=_command(), owner_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
    # Another GM is unaffected — in their own campaign, which is the only place
    # they can start one at all.
    theirs = _a_campaign(world, owner=world.other_owner, name="Theirs")
    assert _a_session(world, theirs, owner=world.other_owner).gm_user_id == world.other_owner


def test_a_session_cannot_be_started_in_a_campaign_that_is_not_the_gms(world: World) -> None:
    """F-4, AUD-1. The GM is the owner and `start` reads that out of the
    campaigns table in the same statement, so a foreign campaign, an archived
    one and one that never existed are one answer — and the free `gm_user_id`
    parameter, which the database never verified, is gone."""
    here = _a_campaign(world)
    later = datetime.now(UTC) + timedelta(hours=12)
    with world.db.transaction() as unit:
        with pytest.raises(MissingParent):
            world.sessions.start(unit, here, command_id=_command(), owner_id=world.other_owner, expires_at=later)
        with pytest.raises(MissingParent):
            world.sessions.start(unit, "cmp_" + "z" * 22, command_id=_command(), owner_id=world.owner, expires_at=later)

    with world.db.transaction() as unit:
        world.campaigns.set_archived(unit, here, owner_id=world.owner, archived=True)
    with world.db.transaction() as unit:
        with pytest.raises(MissingParent):
            world.sessions.start(unit, here, command_id=_command(), owner_id=world.owner, expires_at=later)


def test_a_session_that_expires_before_it_starts_is_refused(world: World) -> None:
    """The bound 0004 carries, so the caller gets the store's refusal rather
    than an integrity error naming a constraint."""
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="expires after it starts"):
            world.sessions.start(
                unit,
                campaign,
                command_id=_command(), owner_id=world.owner,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )


def test_a_start_command_opens_one_session_per_campaign_in_both_worlds(world: World) -> None:
    """0016's partial unique index over `(campaign_id, start_command_id)`, kept
    by the twin too. A caller that holds the campaign lock reads the replay
    first; one that did not gets this named refusal, not a unique violation.
    The same command in another campaign is another command, and a command id
    not of the contract's shape is refused before any statement."""
    campaign, elsewhere = _a_campaign(world), _a_campaign(world, name="Elsewhere")
    command = _command()
    later = datetime.now(UTC) + timedelta(hours=12)
    with world.db.transaction() as unit:
        first = world.sessions.start(
            unit, campaign, owner_id=world.owner, expires_at=later, command_id=command
        )
        assert first.start_command_id == command
        assert world.sessions.by_start_command(unit, campaign, command) == first
        assert world.sessions.by_start_command(unit, elsewhere, command) is None
        world.sessions.end(unit, campaign, first.id, owner_id=world.owner)
    with world.db.transaction() as unit:
        with pytest.raises(StartReplayed):
            world.sessions.start(
                unit, campaign, owner_id=world.owner, expires_at=later, command_id=command
            )
    with world.db.transaction() as unit:
        assert world.sessions.start(
            unit, elsewhere, owner_id=world.owner, expires_at=later, command_id=command
        ).campaign_id == elsewhere
    with world.db.transaction() as unit:
        for malformed in ("short", "x" * 65, "has space in it!!", 7):
            with pytest.raises(ValueError, match="16 to 64"):
                world.sessions.start(
                    unit, campaign, owner_id=world.owner, expires_at=later, command_id=malformed
                )


def test_a_gm_may_start_again_once_their_session_has_ended(world: World) -> None:
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
    assert _a_session(world, campaign).id != session.id


# Behaviour 15 — the narrow primitive.


def test_narrow_advances_the_reveal_epoch_and_calls_its_extension_point_once(world: World) -> None:
    """RQ-7. The audio epoch moves only when the caller asks, because muting the
    table and narrowing what it can see are different decisions."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    assert (session.reveal_epoch, session.audio_epoch) == (0, 0)

    with world.db.transaction() as unit:
        quiet = world.sessions.narrow(unit, campaign, session.id)
        assert quiet is not None
        assert (quiet.reveal_epoch, quiet.audio_epoch) == (1, 0)
        assert world.slot_clears == [(unit, session.id)], "exactly once, with (unit, session_id)"

        loud = world.sessions.narrow(unit, campaign, session.id, audio=True)
        assert loud is not None
        assert (loud.reveal_epoch, loud.audio_epoch) == (2, 1)
        assert world.slot_clears == [(unit, session.id), (unit, session.id)]

    with world.db.transaction() as unit:
        assert world.sessions.narrow(unit, campaign, "ses_" + "z" * 22) is None
        assert world.sessions.narrow(unit, "cmp_" + "z" * 22, session.id) is None, "not that campaign's"


# Behaviours 16 and 17 — End and Rotate, each closing an admission generation.


def _a_screen(
    world: World, campaign_id: str, session_id: str, *, now: datetime | None = None
) -> tuple[ScreenGrant, str]:
    with world.db.transaction() as unit:
        return world.sessions.mint_screen(
            unit, campaign_id, session_id, owner_id=world.owner, now=now
        )


def _digest(secret: str) -> str:
    return sha256(secret.encode("utf-8")).hexdigest()


def test_ending_a_session_revokes_its_screens_and_advances_both_epochs(world: World) -> None:
    """SEC-42: End revokes the session, its admission generation and every
    screen grant of it, in the transaction that advances both epochs — the
    grant would otherwise be a live door into a table that is over."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    screen, _ = _a_screen(world, campaign, session.id)

    with world.db.transaction() as unit:
        closing = world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
        assert closing is not None and closing.outcome == "ended"
        assert closing.screens_revoked == 1
        ended = closing.session
        assert ended.state == "ended" and ended.ended_at is not None
        assert (ended.reveal_epoch, ended.audio_epoch) == (1, 1)
        held = {g.id: g for g in world.sessions.screens(unit, session.id)}
        assert held[screen.id].revoked_at is not None

    with world.db.transaction() as unit:
        again = world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
        assert again is not None and again.outcome is None, "ending twice writes nothing"
        assert (again.session.reveal_epoch, again.session.audio_epoch) == (1, 1)
        assert again.session.ended_at == ended.ended_at


#: The secret whose digest `_a_stray_grant_of_the_retired_generation` stores.
STRAY_SECRET = "a grant of the retired generation"


def _a_stray_grant_of_the_retired_generation(world: World, unit: Any, session_id: str) -> str:
    """The row a mint racing a Rotate leaves behind: unrevoked, and of the
    generation the Rotate has just retired.

    Written by hand, in each world's own way, because no store method makes one
    — `mint_screen` reads the session's *current* generation — which is exactly
    why an ending that revoked only the current generation would look right."""
    stray = "tcr_" + "s" * 22
    digest = _digest(STRAY_SECRET)
    if world.kind == "fake":
        world.db.tables["table_credentials"].add(
            unit, stray, ScreenGrant(stray, session_id, 1, digest, datetime.now(UTC))
        )
    else:
        unit.conn.execute(
            "INSERT INTO campaign.table_credentials "
            "(id, session_id, link_generation, credential_digest) VALUES (%s, %s, 1, %s)",
            (stray, session_id, digest),
        )
    return stray


def test_ending_a_session_revokes_every_generation_it_ever_had(world: World) -> None:
    """A mint that commits just after a Rotate holds an unrevoked grant of the
    generation the Rotate retired — it reads the generation without waiting for
    the Rotate, deliberately (RQ-3). The table is over, so the ending's own
    statement leaves nothing of the session unrevoked behind it, whatever its
    generation. It cannot promise more, which is why a reader checks the state,
    the expiry, the generation and the revocation together (L-10)."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    first, _ = _a_screen(world, campaign, session.id)
    with world.db.transaction() as unit:
        world.sessions.rotate(
            unit, campaign, session.id, owner_id=world.owner, command_id=_command()
        )
    second, _ = _a_screen(world, campaign, session.id)
    assert (first.link_generation, second.link_generation) == (1, 2)

    with world.db.transaction() as unit:
        stray = _a_stray_grant_of_the_retired_generation(world, unit, session.id)

    with world.db.transaction() as unit:
        closing = world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
        assert closing is not None and closing.screens_revoked == 2
        held = {g.id: g for g in world.sessions.screens(unit, session.id)}
        assert held[stray].link_generation == 1 and held[second.id].link_generation == 2, (
            "one retired generation and one current, or this test proves nothing"
        )
        assert [g.id for g in held.values() if g.revoked_at is None] == []


def test_rotating_retires_the_generation_and_every_screen_and_leaves_the_session_live(
    world: World,
) -> None:
    """SEC-42 and REVEAL-17: rotating is not restarting, so the session keeps its
    id, its start and its expiry; the generation moves on, every screen of it is
    revoked, and the command that did it is recorded for its retry."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    old, _ = _a_screen(world, campaign, session.id)
    command = _command()

    with world.db.transaction() as unit:
        closing = world.sessions.rotate(
            unit, campaign, session.id, owner_id=world.owner, command_id=command
        )
        assert closing is not None and closing.outcome == "rotated"
        assert closing.screens_revoked == 1
        after = closing.session
        assert after.id == session.id and after.is_live
        assert after.started_at == session.started_at
        assert after.expires_at == session.expires_at
        assert after.link_generation == 2 and after.rotate_command_id == command
        assert (after.reveal_epoch, after.audio_epoch) == (1, 1)

    fresh, _ = _a_screen(world, campaign, session.id)
    assert fresh.link_generation == 2
    with world.db.transaction() as unit:
        held = {g.id: g for g in world.sessions.screens(unit, session.id)}
        assert held[old.id].revoked_at is not None
        assert held[fresh.id].revoked_at is None


def test_a_rotate_repeated_with_its_command_writes_nothing_and_a_new_one_rotates_again(
    world: World,
) -> None:
    """The contract's Idempotency row: a retried Rotate never closes the table's
    streams twice. A new command always rotates a live session — narrowing always
    wins (X-3) — and so does an older command retried after a newer one, which is
    a harmless narrowing."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    first, second = _command(), _command()

    def rotate(command: str) -> Any:
        with world.db.transaction() as unit:
            return world.sessions.rotate(
                unit, campaign, session.id, owner_id=world.owner, command_id=command
            )

    assert rotate(first).session.link_generation == 2
    again = rotate(first)
    assert again.outcome is None, "the same command answers the session as it stands"
    assert again.session.link_generation == 2
    assert (again.session.reveal_epoch, again.session.audio_epoch) == (1, 1)
    assert rotate(second).session.link_generation == 3
    older = rotate(first)
    assert older.outcome == "rotated" and older.session.link_generation == 4


def test_rotating_a_session_that_is_no_longer_live_answers_it_as_it_stands(world: World) -> None:
    """X-3: a narrowing is never refused for state. The old store refused this
    with `NotLive`; a Rotate of an ended table has nothing left to retire."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
    with world.db.transaction() as unit:
        closing = world.sessions.rotate(
            unit, campaign, session.id, owner_id=world.owner, command_id=_command()
        )
        assert closing is not None and closing.outcome is None
        assert closing.session.state == "ended" and closing.session.link_generation == 1


def test_end_and_rotate_find_nothing_of_another_gm_or_another_campaign(world: World) -> None:
    """The owner and the campaign are in the statement that locks the row (L-5),
    so a stranger's End and a session named through the wrong campaign are one
    answer — None — and neither changes the session."""
    campaign = _a_campaign(world)
    elsewhere = _a_campaign(world, name="Elsewhere")
    session = _a_session(world, campaign)
    with world.db.transaction() as unit:
        for owner, through in ((world.other_owner, campaign), (world.owner, elsewhere)):
            assert world.sessions.end(unit, through, session.id, owner_id=owner) is None
            assert world.sessions.rotate(
                unit, through, session.id, owner_id=owner, command_id=_command()
            ) is None
    with world.db.transaction() as unit:
        untouched = world.sessions.get(unit, session.id)
        assert untouched is not None and untouched.is_live
        assert (untouched.reveal_epoch, untouched.link_generation) == (0, 1)


def test_expiry_finalises_a_due_session_at_its_expiry_and_refuses_one_not_yet_due(
    world: World,
) -> None:
    """A session found past `expires_at` is finalised as `expired` with
    `ended_at = expires_at`, the moment it stopped serving. The system's path
    names no owner, so it refuses a session that is not due: it can never be an
    End that skipped the owner check. An End that finds its session due records
    the same thing."""
    campaign = _a_campaign(world)
    due = _a_session(world, campaign, hours=11, started_ago_h=12)
    with world.db.transaction() as unit:
        stray = _a_stray_grant_of_the_retired_generation(world, unit, due.id)
    with world.db.transaction() as unit:
        closing = world.sessions.expire(unit, due.id, campaign)
        assert closing is not None and closing.outcome == "expired"
        assert closing.session.state == "expired"
        assert closing.session.ended_at == due.expires_at
        assert closing.screens_revoked == 1
        held = {g.id: g for g in world.sessions.screens(unit, due.id)}
        assert held[stray].revoked_at is not None
    with world.db.transaction() as unit:
        again = world.sessions.expire(unit, due.id, campaign)
        assert again is not None and again.outcome is None

    by_end = _a_session(world, campaign, hours=11, started_ago_h=12)
    with world.db.transaction() as unit:
        closing = world.sessions.end(unit, campaign, by_end.id, owner_id=world.owner)
        assert closing is not None and closing.outcome == "expired"
        assert closing.session.ended_at == by_end.expires_at

    live = _a_session(world, campaign)
    with world.db.transaction() as unit:
        with pytest.raises(NotDue):
            world.sessions.expire(unit, live.id, campaign)
    with world.db.transaction() as unit:
        assert world.sessions.get(unit, live.id).is_live


def test_a_session_that_is_not_live_admits_no_screen(world: World) -> None:
    """A mint re-checks the owner, the state and the expiry in its own
    statement: an ended session and a row still `live` past its expiry both
    refuse it, and neither leaves a grant behind."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
    with world.db.transaction() as unit:
        with pytest.raises(NotLive):
            world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
        assert world.sessions.screens(unit, session.id) == []

    due = _a_session(world, campaign, hours=11, started_ago_h=12)
    with world.db.transaction() as unit:
        with pytest.raises(NotLive):
            world.sessions.mint_screen(unit, campaign, due.id, owner_id=world.owner)
        with pytest.raises(MissingParent):
            world.sessions.mint_screen(unit, campaign, due.id, owner_id=world.other_owner)
        assert world.sessions.screens(unit, due.id) == []


def test_a_grant_is_found_by_its_digest_and_a_revoked_one_like_an_unknown_one(world: World) -> None:
    """SEC-46: one digest lookup, and one answer for every grant that is not
    live — an unknown digest and a revoked grant are the same None."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    screen, secret = _a_screen(world, campaign, session.id)
    moment = datetime.now(UTC)
    with world.db.transaction() as unit:
        found = world.sessions.resolve_screen(unit, _digest(secret), now=moment)
        assert found is not None
        assert (found.grant_id, found.session_id, found.campaign_id, found.generation) == (
            screen.id, session.id, campaign, 1
        )
        assert world.sessions.resolve_screen(unit, "f" * 64, now=moment) is None
        assert screen.credential_digest == _digest(secret), "only the digest is at rest"

    with world.db.transaction() as unit:
        assert world.sessions.revoke_screen(
            unit, campaign, screen.id, owner_id=world.owner
        ) == (session.id, True)
    with world.db.transaction() as unit:
        assert world.sessions.resolve_screen(unit, _digest(secret), now=moment) is None


def test_the_screen_bound_is_the_twins_and_the_databases_alike(world: World) -> None:
    """SEC-48's bound, enforced by the store in both worlds under the session's
    advisory lock. Revoking one frees a place."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    screens = [_a_screen(world, campaign, session.id)[0] for _ in range(SCREENS_PER_SESSION)]
    with world.db.transaction() as unit:
        with pytest.raises(ScreenLimit):
            world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
        if world.kind == "fake":
            assert unit.locks[-1][1] == session.id, "the session's advisory lock"
    with world.db.transaction() as unit:
        world.sessions.revoke_screen(unit, campaign, screens[0].id, owner_id=world.owner)
    assert _a_screen(world, campaign, session.id)[0].link_generation == 1


def test_a_screen_revoke_is_the_owners_and_a_repeat_changes_nothing(world: World) -> None:
    """L-11: one owner-scoped statement. Another GM's grant, a grant named
    through another campaign and a grant that does not exist are None; the
    owner's repeat is not an error and reports that it revoked nothing."""
    campaign = _a_campaign(world)
    elsewhere = _a_campaign(world, name="Elsewhere")
    session = _a_session(world, campaign)
    screen, _ = _a_screen(world, campaign, session.id)
    later = datetime.now(UTC) + timedelta(seconds=1)
    with world.db.transaction() as unit:
        assert world.sessions.revoke_screen(
            unit, campaign, screen.id, owner_id=world.other_owner
        ) is None
        assert world.sessions.revoke_screen(
            unit, elsewhere, screen.id, owner_id=world.owner
        ) is None
        assert world.sessions.revoke_screen(
            unit, campaign, "tcr_" + "z" * 22, owner_id=world.owner
        ) is None
    with world.db.transaction() as unit:
        assert world.sessions.revoke_screen(
            unit, campaign, screen.id, owner_id=world.owner
        ) == (session.id, True)
    with world.db.transaction() as unit:
        assert world.sessions.revoke_screen(
            unit, campaign, screen.id, owner_id=world.owner, now=later
        ) == (session.id, False)


# Behaviour 18 — the stale sessions a start has to clear out of its own way.


def test_expired_live_sessions_for_gm_returns_that_gms_overdue_live_ones_only(
    world: World,
) -> None:
    """RQ-7: a GM whose last session timed out unnoticed must be able to start
    again, so the start ends the stale one first rather than meeting the index."""
    campaign = _a_campaign(world)
    theirs = _a_campaign(world, owner=world.other_owner, name="Theirs")
    stale = _a_session(world, campaign, hours=11, started_ago_h=12)
    other = _a_session(world, theirs, hours=11, started_ago_h=12, owner=world.other_owner)

    with world.db.transaction() as unit:
        assert [s.id for s in world.sessions.expired_live_sessions_for_gm(unit, world.owner)] == [
            stale.id
        ], "another GM's stale session is not this GM's to end"
        assert [s.id for s in world.sessions.expired_live_sessions_for_gm(
            unit, world.other_owner
        )] == [other.id]

        world.sessions.expire(unit, stale.id, campaign)
        assert world.sessions.expired_live_sessions_for_gm(unit, world.owner) == [], (
            "an ended session is not still overdue"
        )

    fresh = _a_session(world, campaign, hours=12)
    with world.db.transaction() as unit:
        assert world.sessions.expired_live_sessions_for_gm(unit, world.owner) == [], (
            "a session that is live and in date is not overdue"
        )
        assert world.sessions.get(unit, fresh.id) is not None


# Behaviours 19 and 20 — what a transaction owes its readers.


def test_a_rolled_back_unit_of_work_leaves_no_row_in_any_of_the_three_stores(
    world: World,
) -> None:
    """The contract that lets `1kg.2.2` and `1kg.2.3` compose all three stores in
    one transaction: every write registers its undo, so a block that fails
    part-way leaves nothing behind for the next caller to trip over."""
    campaign = _a_campaign(world)
    with pytest.raises(RuntimeError, match="boom"):
        with world.db.transaction() as unit:
            doomed = world.campaigns.create(unit, owner_id=world.owner, name="Doomed")
            seat = world.participants.add(unit, campaign, alias="Wren")
            world.participants.offer(unit, campaign, seat.id, user_id=world.players[0])
            assert world.participants.accept(unit, campaign, seat.id, user_id=world.players[0])
            session = world.sessions.start(
                unit,
                campaign,
                command_id=_command(), owner_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
            world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
            world.audit.append(
                unit,
                campaign_id=campaign,
                actor_kind=ActorKind.GM,
                action=AuditAction.PARTICIPANT_ADDED,
                object_kind="participant",
                decision=Decision.ALLOWED,
                object_ref=seat.id,
            )
            raise RuntimeError("boom")

    with world.db.transaction() as unit:
        assert world.campaigns.get(unit, doomed.id, owner_id=world.owner) is None
        assert world.campaigns.authz_revision(unit, doomed.id) is None
        assert world.participants.get(unit, seat.id) is None
        assert world.participants.seats_for_user(unit, world.players[0]) == []
        assert world.sessions.get(unit, session.id) is None
        assert world.sessions.screens(unit, session.id) == []
        assert world.audit.for_campaign(unit, campaign) == []


def test_a_row_a_unit_has_not_committed_is_invisible_to_a_second_reader(world: World) -> None:
    """`on_publish` in the twin, and an uncommitted transaction in PostgreSQL:
    the same observable answer. A reader that could see a half-written aggregate
    would see a campaign whose participants do not exist yet."""
    campaign = _a_campaign(world)
    with world.db.transaction() as writer:
        seat = world.participants.add(writer, campaign, alias="Wren")
        assert world.participants.get(writer, seat.id) is not None, "its own writes, yes"
        with world.db.transaction() as reader:
            assert world.participants.get(reader, seat.id) is None
            assert world.participants.list_for_campaign(reader, campaign) == []

    with world.db.transaction() as reader:
        assert world.participants.get(reader, seat.id) is not None
        assert [p.id for p in world.participants.list_for_campaign(reader, campaign)] == [seat.id]


def test_a_change_to_a_committed_row_is_invisible_until_it_commits_too(world: World) -> None:
    """F-5. The first version of the twin staged INSERTs and wrote UPDATEs in
    place, on the grounds that its transactions are serial — but a second
    transaction opened inside the first is exactly how a visibility test is
    written, and that reader saw every uncommitted change. PostgreSQL shows it
    the committed row; so does the twin now."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    session = _a_session(world, campaign)

    with world.db.transaction() as writer:
        assert world.participants.remove(writer, campaign, seat)
        assert world.sessions.end(writer, campaign, session.id, owner_id=world.owner) is not None
        assert world.campaigns.set_archived(writer, campaign, owner_id=world.owner, archived=True)

        with world.db.transaction() as reader:
            still = world.participants.get(reader, seat)
            assert still is not None and still.is_active, "a removal nobody has committed"
            running = world.sessions.get(reader, session.id)
            assert running is not None and running.is_live, "an ending nobody has committed"
            assert not world.campaigns.get(reader, campaign, owner_id=world.owner).is_archived

    with world.db.transaction() as reader:
        assert not world.participants.get(reader, seat).is_active
        assert not world.sessions.get(reader, session.id).is_live
        assert world.campaigns.get(reader, campaign, owner_id=world.owner).is_archived


def test_a_rolled_back_change_leaves_no_state_a_unique_index_would_forbid(world: World) -> None:
    """F-5's second half, and the one that made the twin actively misleading: a
    unit that ended a session and started another, then rolled back, left TWO
    live sessions for one GM — a state `table_sessions_one_live_per_gm_uidx`
    cannot hold — and the same shape left two active participants answering to
    one alias."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    seat = _a_participant(world, campaign, "Rook")

    with pytest.raises(RuntimeError, match="boom"):
        with world.db.transaction() as unit:
            world.sessions.end(unit, campaign, session.id, owner_id=world.owner)
            world.sessions.start(
                unit,
                campaign,
                command_id=_command(), owner_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
            world.participants.remove(unit, campaign, seat)
            world.participants.add(unit, campaign, alias="Rook")
            raise RuntimeError("boom")

    with world.db.transaction() as unit:
        live = [
            s
            for s in (world.sessions.get(unit, session.id),)
            if s is not None and s.is_live
        ]
        assert [s.id for s in live] == [session.id], "the ending was taken back, and only that"
        seated = world.participants.list_for_campaign(unit, campaign)
        assert [p.id for p in seated] == [seat], "one active seat answering to 'Rook', not two"
        # And the invariants still hold, which is the point: a second start is
        # refused, and the alias is still taken.
        with pytest.raises(LiveSessionExists):
            world.sessions.start(
                unit,
                campaign,
                command_id=_command(), owner_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
        with pytest.raises(AliasTaken):
            world.participants.add(unit, campaign, alias="rook")


def test_an_uncommitted_campaign_cannot_be_read_or_locked_by_a_second_reader(
    world: World,
) -> None:
    """The authorisation row is written by a trigger rather than by the store,
    so it is the one row that could plausibly escape its transaction — and a
    campaign another reader can already LOCK is a campaign it can already
    authorise against. The writer sees its own; nobody else sees anything."""
    with world.db.transaction() as writer:
        campaign = world.campaigns.create(writer, owner_id=world.owner, name="Unfinished")
        assert world.campaigns.authz_revision(writer, campaign.id) == 0, "its own, yes"

        with world.db.transaction() as reader:
            assert world.campaigns.get(reader, campaign.id, owner_id=world.owner) is None
            assert world.campaigns.authz_revision(reader, campaign.id) is None
            with pytest.raises(CampaignAuthzMissing):
                reader.lock_campaign(campaign.id, shared=True)

        writer.lock_campaign(campaign.id, shared=False)
        assert writer.advance_authz_revision(campaign.id) == 1

    with world.db.transaction() as reader:
        assert world.campaigns.get(reader, campaign.id, owner_id=world.owner) is not None
        assert world.campaigns.authz_revision(reader, campaign.id) == 1, (
            "the revision the writer reached is the one that commits"
        )
        reader.lock_campaign(campaign.id, shared=True)


def test_an_advance_a_unit_has_not_committed_is_invisible_to_a_second_reader(world: World) -> None:
    """The other half of the same rule, on an already-committed campaign. A
    reader that is not holding the lock does an unlocked SELECT, which under
    READ COMMITTED gives it the committed revision — so a second reader must
    never see a number the writer has not committed, and must see it the moment
    the writer does."""
    campaign = _a_campaign(world)
    with world.db.transaction() as writer:
        writer.lock_campaign(campaign, shared=False)
        assert writer.advance_authz_revision(campaign) == 1
        assert world.campaigns.authz_revision(writer, campaign) == 1, "its own, yes"

        with world.db.transaction() as reader:
            assert world.campaigns.authz_revision(reader, campaign) == 0

    with world.db.transaction() as reader:
        assert world.campaigns.authz_revision(reader, campaign) == 1


def test_a_rolled_back_advance_leaves_the_revision_where_it_was(world: World) -> None:
    campaign = _a_campaign(world)
    with pytest.raises(RuntimeError, match="boom"):
        with world.db.transaction() as unit:
            unit.lock_campaign(campaign, shared=False)
            assert unit.advance_authz_revision(campaign) == 1
            assert unit.advance_authz_revision(campaign) == 2
            raise RuntimeError("boom")
    with world.db.transaction() as reader:
        assert world.campaigns.authz_revision(reader, campaign) == 0


def test_a_campaign_a_rolled_back_unit_created_leaves_no_authorisation_row(world: World) -> None:
    world_campaign: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        with world.db.transaction() as unit:
            world_campaign.append(world.campaigns.create(unit, owner_id=world.owner, name="Doomed").id)
            unit.lock_campaign(world_campaign[0], shared=False)
            unit.advance_authz_revision(world_campaign[0])
            raise RuntimeError("boom")

    with world.db.transaction() as unit:
        assert world.campaigns.authz_revision(unit, world_campaign[0]) is None
        with pytest.raises(CampaignAuthzMissing):
            unit.lock_campaign(world_campaign[0], shared=True)


def test_a_campaign_that_is_archived_and_restored_ends_up_where_it_started(world: World) -> None:
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        assert world.campaigns.set_archived(unit, campaign, owner_id=world.owner, archived=True)
        assert not world.campaigns.set_archived(unit, campaign, owner_id=world.owner, archived=True)
        assert world.campaigns.get(unit, campaign, owner_id=world.owner).is_archived
        assert world.campaigns.set_archived(unit, campaign, owner_id=world.owner, archived=False)
        assert not world.campaigns.get(unit, campaign, owner_id=world.owner).is_archived


# Behaviour 20b — what the fakes used to accept and a foreign key never would.


def test_a_row_whose_parent_does_not_exist_is_refused_in_both_worlds(world: World) -> None:
    """F-6. The twins each kept their own rows and never looked at `db`, so a
    participant could be added to a campaign that does not exist, and a join
    credential minted for a session that does not exist — every unit test
    on the fakes green, and the first real request a 500 carrying the driver's
    text. They share one state now, and both worlds answer with the same named
    refusal rather than an integrity error."""
    nowhere = "cmp_" + "z" * 22
    with world.db.transaction() as unit:
        with pytest.raises(MissingParent):
            world.participants.add(unit, nowhere, alias="Rook")
        with pytest.raises(MissingParent):
            world.sessions.start(
                unit,
                nowhere,
                command_id=_command(), owner_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
        with pytest.raises(MissingParent):
            world.sessions.mint_screen(unit, nowhere, "ses_" + "z" * 22, owner_id=world.owner)


@pytest.mark.parametrize("field", ["now", "expires_at"])
def test_a_naive_timestamp_is_refused_in_both_worlds(world: World, field: str) -> None:
    """F-6's third disagreement. PostgreSQL reinterprets a naive value in the
    session's time zone against a TIMESTAMPTZ column; the twin keeps it and
    raises `TypeError` the first time something compares it. Neither is an
    answer, so it is refused before either can happen."""
    campaign = _a_campaign(world)
    naive = datetime(2026, 9, 20, 12, 0, 0)  # noqa: DTZ001 - the point of the test
    when = {field: naive} if field == "now" else {}
    expires = naive if field == "expires_at" else datetime.now(UTC) + timedelta(hours=12)
    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="timezone-aware"):
            world.sessions.start(
                unit, campaign, command_id=_command(), owner_id=world.owner, expires_at=expires, **when
            )


# Behaviour 22b — the ledger, in both worlds.


def test_a_recorded_decision_reads_back_with_its_detail(world: World) -> None:
    """F-8: `PostgresAuditLog` was executed by no test anywhere, so the writer
    the whole epic is told to audit through had never run against a database.
    It is in the shared suite now, which means CI runs it."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        recorded = world.audit.append(
            unit,
            campaign_id=campaign,
            actor_kind=ActorKind.GM,
            action=AuditAction.PARTICIPANT_REMOVED,
            object_kind="participant",
            decision=Decision.ALLOWED,
            actor_ref=str(world.owner),
            object_ref=seat,
            reason_code="gm_removed",
            authz_revision=3,
            detail={"participant_id": seat},
        )

    with world.db.transaction() as unit:
        [kept] = world.audit.for_campaign(unit, campaign)
        assert kept.action == "participant.removed" and kept.decision == "allowed"
        assert kept.campaign_id_tombstone == campaign and kept.object_ref == seat
        assert kept.actor_ref == str(world.owner) and kept.authz_revision == 3
        assert kept.reason_code == "gm_removed"
        assert kept.detail == {"participant_id": seat}
        assert kept.id == recorded.id
        assert world.audit.for_campaign(unit, "cmp_" + "z" * 22) == []


def test_the_ledger_refuses_a_row_that_could_carry_content_in_both_worlds(world: World) -> None:
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="no such detail key"):
            world.audit.append(
                unit,
                campaign_id=campaign,
                actor_kind=ActorKind.GM,
                action=AuditAction.PARTICIPANT_ADDED,
                object_kind="participant",
                decision=Decision.ALLOWED,
                detail={"alias": "Rook"},
            )
        assert world.audit.for_campaign(unit, campaign) == [], "nothing was recorded"


# Behaviour 21 — what a record says when something prints it.


def test_no_store_record_shows_a_digest_or_an_alias_when_it_is_printed() -> None:
    """A traceback is a log line, and `repr()` is what a traceback prints. A
    digest is the lookup key for a live credential (SEC-5) and an alias is
    private text (SEC-20), so neither belongs in one — nor does the account a
    seat belongs to, whose user id is personal data (L-5)."""
    moment = datetime.now(UTC)
    digest = "f" * 64
    records = [
        Campaign("cmp_x", 1, "Nocturne", moment, moment),
        Participant("prt_x", "cmp_x", "Rook", moment, user_id=987654321, accepted_at=moment),
        TableSession("ses_x", "cmp_x", 1, "live", moment, moment, 1),
        ScreenGrant("tcr_x", "ses_x", 1, digest, moment),
    ]
    for record in records:
        printed = repr(record)
        assert digest not in printed, f"{type(record).__name__} shows a digest"
        assert "Rook" not in printed, f"{type(record).__name__} shows an alias"
        assert "Nocturne" not in printed, f"{type(record).__name__} shows a campaign name"
        assert "987654321" not in printed, f"{type(record).__name__} shows an account"
        assert type(record).__name__ in printed, "a record still says what it is"


# ── ixa.1 — the twin refuses what only a race can answer ─────────────────────
#
# On the twin a single-threaded test can interleave two transactions only by
# nesting one inside the other, and until ixa.1 the inner one then committed
# states PostgreSQL forbids, because nothing made it wait. Each test below is
# one of those probes. It now raises `TwinWouldBlock` at the inner unit's first
# write, or at its conflicting campaign lock, and it names the PostgreSQL test
# that shows what the server does instead — or says plainly that none exists
# yet. Twin-only: the PostgreSQL half of a race is a two-connection test.


def _twin() -> World:
    db = InMemoryDatabase()
    return World(
        "fake",
        db,
        InMemoryCampaignStore(db),
        InMemoryParticipantStore(db),
        InMemoryTableSessionStore(db, slot_clear=no_slots),
        InMemoryAuditLog(),
        owner=1,
        other_owner=2,
        players=(3, 4, 5),
        slot_clears=[],
        jobs=InMemoryJobQueue(db=db),
    )


def test_the_twin_refuses_a_second_session_start_while_the_first_is_uncommitted() -> None:
    """Probe N1. The twin used to commit both: two live sessions for one GM. In
    PostgreSQL `table_sessions_one_live_per_gm_uidx` makes the second start
    wait for the first and then refuses it —
    `test_two_racing_session_starts_for_one_gm_leave_exactly_one_winner`."""
    world = _twin()
    campaign = _a_campaign(world)
    expires = datetime.now(UTC) + timedelta(hours=12)
    with world.db.transaction() as outer:
        first = world.sessions.start(outer, campaign, command_id=_command(), owner_id=world.owner, expires_at=expires)
        with world.db.transaction() as inner:
            with pytest.raises(TwinWouldBlock):
                world.sessions.start(inner, campaign, command_id=_command(), owner_id=world.owner, expires_at=expires)

    with world.db.transaction() as unit:
        assert world.sessions.get(unit, first.id) is not None
        with pytest.raises(LiveSessionExists):
            world.sessions.start(unit, campaign, command_id=_command(), owner_id=world.owner, expires_at=expires)


def test_the_twin_refuses_a_second_seat_on_an_alias_while_the_first_is_uncommitted() -> None:
    """Probe N2. The twin used to commit both: two active seats answering to
    one alias. In PostgreSQL `participants_alias_uidx` makes the second insert
    wait for the first and then skips it. **No PostgreSQL test of that race
    exists yet** — the index is the evidence, and the follow-up is named in
    the pull request."""
    world = _twin()
    campaign = _a_campaign(world)
    with world.db.transaction() as outer:
        world.participants.add(outer, campaign, alias="Rook")
        with world.db.transaction() as inner:
            with pytest.raises(TwinWouldBlock):
                world.participants.add(inner, campaign, alias="rook")

    with world.db.transaction() as unit:
        assert [p.alias for p in world.participants.list_for_campaign(unit, campaign)] == ["Rook"]


def test_the_twin_refuses_a_remove_nested_inside_an_uncommitted_accept() -> None:
    """Probe N3'. The twin used to lose the GM's Remove: the inner unit
    removed the committed row, and the outer then published its own accepted
    copy over it. In PostgreSQL the Remove waits for the seat's row and then
    marks the seat just accepted —
    `test_a_remove_waiting_behind_an_accept_wins_and_the_seat_reads_removed`."""
    world = _twin()
    campaign = _a_campaign(world)
    player = world.players[0]
    seat = _a_participant(world, campaign, "Rook")
    _offer(world, campaign, seat, player)
    with world.db.transaction() as outer:
        assert world.participants.accept(outer, campaign, seat, user_id=player) is True
        with world.db.transaction() as inner:
            with pytest.raises(TwinWouldBlock):
                world.participants.remove(inner, campaign, seat)

    with world.db.transaction() as unit:
        held = world.participants.get(unit, seat)
        assert held is not None and held.is_accepted, "only the outer unit's decision committed"


def test_the_twin_refuses_an_accept_nested_inside_an_uncommitted_remove() -> None:
    """Probe N3'', the other order. The twin used to let the inner unit accept
    a seat the outer had removed, and the outer then published the removal over
    it. In PostgreSQL the acceptance waits and is refused —
    `test_an_accept_waiting_behind_a_remove_is_refused_and_the_seat_stays_removed`."""
    world = _twin()
    campaign = _a_campaign(world)
    player = world.players[0]
    seat = _a_participant(world, campaign, "Rook")
    _offer(world, campaign, seat, player)
    with world.db.transaction() as outer:
        assert world.participants.remove(outer, campaign, seat) is True
        with world.db.transaction() as inner:
            with pytest.raises(TwinWouldBlock):
                world.participants.accept(inner, campaign, seat, user_id=player)

    with world.db.transaction() as unit:
        held = world.participants.get(unit, seat)
        assert held is not None and not held.is_active and held.accepted_at is None


def test_the_twin_refuses_a_narrow_nested_inside_an_uncommitted_narrow() -> None:
    """Probe N4. The twin used to commit both and lose one advance:
    `reveal_epoch` 1, not 2. In PostgreSQL the second narrowing waits for the
    session's row. **No PostgreSQL test of that wait exists yet** — the row
    lock `narrow` takes is the evidence, and the follow-up is named in the pull
    request."""
    world = _twin()
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    with world.db.transaction() as outer:
        world.sessions.narrow(outer, campaign, session.id)
        with world.db.transaction() as inner:
            with pytest.raises(TwinWouldBlock):
                world.sessions.narrow(inner, campaign, session.id)

    with world.db.transaction() as unit:
        narrowed = world.sessions.get(unit, session.id)
        assert narrowed is not None and narrowed.reveal_epoch == 1


def test_the_twin_refuses_an_offer_nested_inside_an_uncommitted_offer() -> None:
    """Probe N5. The twin used to commit both offers of one open seat, the
    later one winning. In PostgreSQL the second waits for the row, is handed the
    seat just offered and is refused —
    `test_two_offers_of_one_open_seat_leave_exactly_one_account_in_it`."""
    world = _twin()
    campaign = _a_campaign(world)
    first, second = world.players[0], world.players[1]
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as outer:
        world.participants.offer(outer, campaign, seat, user_id=first)
        with world.db.transaction() as inner:
            with pytest.raises(TwinWouldBlock):
                world.participants.offer(inner, campaign, seat, user_id=second)

    with world.db.transaction() as unit:
        held = world.participants.get(unit, seat)
        assert held is not None and held.user_id == first


def test_the_twin_refuses_a_second_exclusive_holder_of_one_campaign() -> None:
    """Probe P9. The twin used to let two nested exclusive holders both
    advance the revision and commit 1, not 2. In PostgreSQL the second
    `FOR UPDATE` waits and times out —
    `test_a_conflicting_campaign_lock_waits_and_then_times_out[exclusive-blocks-exclusive]`.
    The refusal is at the LOCK, before any write: the conflict is judged on the
    holder's recorded lock, not on whether it has written."""
    world = _twin()
    campaign = _a_campaign(world)
    with world.db.transaction() as outer:
        outer.lock_campaign(campaign, shared=False)
        assert outer.advance_authz_revision(campaign) == 1
        with world.db.transaction() as inner:
            with pytest.raises(TwinWouldBlock):
                inner.lock_campaign(campaign, shared=False)

    with world.db.transaction() as unit:
        assert world.campaigns.authz_revision(unit, campaign) == 1


# Behaviours 30 and 31 — two callers, one winner. No fake can show this.


def _race(work: Callable[[], object], runners: int = 2) -> list[object]:
    """Run `work` in `runners` threads that start together, and return what each
    one produced — an exception counting as its own result."""
    ready = threading.Barrier(runners, timeout=PATIENCE)
    results: list[object] = []
    guard = threading.Lock()

    def run() -> None:
        try:
            ready.wait()
            outcome = work()
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcome = exc
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=run, daemon=True) for _ in range(runners)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(PATIENCE)
        assert not thread.is_alive(), "a racing transaction never finished"
    return results


@needs_db
def test_two_racing_session_starts_for_one_gm_leave_exactly_one_winner(dsn: str) -> None:
    """REVEAL-2's concurrency half. The partial unique index decides it; the
    loser gets the same refusal a sequential second start gets."""
    owner = _seed(dsn)
    db = _database(dsn)
    sessions = PostgresTableSessionStore(slot_clear=no_slots)
    expires = datetime.now(UTC) + timedelta(hours=12)

    def start() -> object:
        with db.transaction() as unit:
            return sessions.start(unit, CAMPAIGN, command_id=_command(), owner_id=owner, expires_at=expires).id

    outcomes = _race(start)
    started = [o for o in outcomes if isinstance(o, str)]
    refused = [o for o in outcomes if isinstance(o, LiveSessionExists)]
    assert len(started) == 1 and len(refused) == 1, outcomes

    with connect(dsn) as conn:
        live = conn.execute(
            "SELECT count(*) FROM campaign.table_sessions WHERE gm_user_id = %s AND state = 'live'",
            (owner,),
        ).fetchone()[0]
    assert live == 1


# ── Seats on a real server: an accept or an offer against a remove ───────────
#
# RQ-3/RQ-5: every mutator takes the participant row first, so two of them on
# one seat serialise on it and the second decides on the row the first
# committed. The interleaving is explicit — one transaction holds the row, the
# other is started and the server is asked whether it is really blocked — so
# nothing here depends on timing.


def _while_another_transaction_holds_the_seat(
    dsn: str,
    db: Database,
    participant_id: str,
    holder_work: Callable[[Any], None],
    waiter_work: Callable[[Any], Any],
) -> Any:
    """Run `holder_work` in a transaction holding the participant row, start
    `waiter_work` in a second one, wait until the SERVER says it is blocked,
    then let the first commit and return what the second produced.

    A deadlock would show up as `40P01` coming back from one of the two; a
    lock-order inversion is what would cause one, and every path here takes the
    participant row first.
    """
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []
    produced: list[Any] = []

    def holder() -> None:
        try:
            with db.transaction() as unit:
                held = PostgresParticipantStore().hold(unit, participant_id, campaign_id=CAMPAIGN)
                assert held is not None
                holder_work(unit)
                took.set()
                release.wait(PATIENCE)
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    def waiter() -> None:
        try:
            with db.transaction() as unit:
                produced.append(waiter_work(unit))
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            produced.append(exc)

    holding = threading.Thread(target=holder, daemon=True)
    holding.start()
    assert took.wait(PATIENCE), "the holding transaction never took the row"
    if failure:
        raise failure[0]

    waiting = threading.Thread(target=waiter, daemon=True)
    waiting.start()
    assert _someone_waits_on_a_lock(dsn), "nobody was blocked, so this proves nothing"
    release.set()
    holding.join(PATIENCE)
    waiting.join(PATIENCE)
    assert not holding.is_alive() and not waiting.is_alive(), "a transaction never finished"
    if failure:
        raise failure[0]
    if isinstance(produced[0], BaseException):
        raise produced[0]
    return produced[0]


def _accounts(dsn: str, count: int = 2) -> list[int]:
    """Accounts that own nothing: the people a GM offers seats to."""
    with connect(dsn) as conn:
        return [
            int(
                conn.execute(
                    "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
                    (f"player{n}@example.com",),
                ).fetchone()[0]
            )
            for n in range(count)
        ]


def _a_seat(
    db: Database,
    participants: PostgresParticipantStore,
    *,
    offered_to: int | None = None,
    accepted_by: int | None = None,
) -> str:
    with db.transaction() as unit:
        seat = participants.add(unit, CAMPAIGN, alias="Rook")
        if offered_to is not None:
            participants.offer(unit, CAMPAIGN, seat.id, user_id=offered_to)
        if accepted_by is not None:
            participants.offer(unit, CAMPAIGN, seat.id, user_id=accepted_by)
            participants.accept(unit, CAMPAIGN, seat.id, user_id=accepted_by)
    return seat.id


@needs_db
def test_an_accept_waiting_behind_a_remove_is_refused_and_the_seat_stays_removed(
    dsn: str, owner: int
) -> None:
    """AC-5, remove first. The acceptance waits for the seat's row; when it gets
    it the row it is handed is the removed one — READ COMMITTED re-reads a row
    a lock wait was for — so it is refused, and nobody is seated."""
    db, participants = _database(dsn, PATIENT), PostgresParticipantStore()
    (player,) = _accounts(dsn, 1)
    seat = _a_seat(db, participants, offered_to=player)

    def remove(unit: Any) -> None:
        assert participants.remove(unit, CAMPAIGN, seat)

    def accept(unit: Any) -> bool:
        return participants.accept(unit, CAMPAIGN, seat, user_id=player)

    with pytest.raises(SeatUnavailable):
        _while_another_transaction_holds_the_seat(dsn, db, seat, remove, accept)

    with db.transaction() as unit:
        gone = participants.get(unit, seat)
        assert gone is not None and not gone.is_active and gone.accepted_at is None
        assert participants.seat_for(unit, CAMPAIGN, player) is None


@needs_db
def test_a_remove_waiting_behind_an_accept_wins_and_the_seat_reads_removed(
    dsn: str, owner: int
) -> None:
    """AC-5, accept first. The removal waits for the seat, then marks the seat
    the account has just accepted: a GM's Remove is never lost to a player who
    accepted a moment earlier."""
    db, participants = _database(dsn, PATIENT), PostgresParticipantStore()
    (player,) = _accounts(dsn, 1)
    seat = _a_seat(db, participants, offered_to=player)

    def accept(unit: Any) -> None:
        assert participants.accept(unit, CAMPAIGN, seat, user_id=player) is True

    def remove(unit: Any) -> bool:
        return participants.remove(unit, CAMPAIGN, seat)

    assert _while_another_transaction_holds_the_seat(dsn, db, seat, accept, remove) is True

    with db.transaction() as unit:
        gone = participants.get(unit, seat)
        assert gone is not None and not gone.is_active
        assert gone.user_id == player and gone.accepted_at is not None, "marked, never cleared"
        assert participants.seat_for(unit, CAMPAIGN, player) is None
        assert participants.seats_for_user(unit, player) == []


@needs_db
def test_two_offers_of_one_open_seat_leave_exactly_one_account_in_it(dsn: str, owner: int) -> None:
    """AC-5. Two GM requests offering one open seat to two accounts: the second
    waits for the row, is handed the seat the first has just offered, and is
    refused — so exactly one account holds the offer."""
    db, participants = _database(dsn, PATIENT), PostgresParticipantStore()
    first, second = _accounts(dsn, 2)
    seat = _a_seat(db, participants)

    def offer_first(unit: Any) -> None:
        participants.offer(unit, CAMPAIGN, seat, user_id=first)

    def offer_second(unit: Any) -> object:
        return participants.offer(unit, CAMPAIGN, seat, user_id=second)

    with pytest.raises(SeatUnavailable):
        _while_another_transaction_holds_the_seat(dsn, db, seat, offer_first, offer_second)

    with db.transaction() as unit:
        held = participants.get(unit, seat)
        assert held is not None and held.user_id == first and held.accepted_at is None


@pytest.mark.parametrize("mutator", sorted(PARTICIPANT_MUTATORS))
@needs_db
def test_a_bare_participant_mutator_waits_for_the_seat_another_transaction_holds(
    dsn: str, owner: int, mutator: str
) -> None:
    """G-6 on the server rather than in the unit of work's bookkeeping: called
    with nothing composed around it, each of these really is behind the
    participant row. `pg_stat_activity` is asked whether the second transaction
    is blocked before the first is released, so nothing here depends on
    timing."""
    db, participants = _database(dsn, PATIENT), PostgresParticipantStore()
    (player,) = _accounts(dsn, 1)
    seat = _a_seat(
        db,
        participants,
        offered_to=player if mutator == "accept" else None,
        accepted_by=player if mutator == "confirm" else None,
    )

    def nothing_else(unit: Any) -> None:
        """The harness's own `hold` is the whole of the holder's work."""

    def mutate(unit: Any) -> object:
        return PARTICIPANT_MUTATORS[mutator](participants, unit, CAMPAIGN, seat, player)

    outcome = _while_another_transaction_holds_the_seat(dsn, db, seat, nothing_else, mutate)
    assert outcome is True or isinstance(outcome, Participant), outcome


@needs_db
def test_a_hold_naming_the_wrong_campaign_locks_nothing(dsn: str, owner: int) -> None:
    """L-7 and G-11 on the server. The campaign is in the statement that takes
    the lock, so a row the WHERE does not match is never locked at all: a second
    connection asking for it with NOWAIT gets it at once. The control is the
    same probe against a hold that names the right campaign, which must fail —
    otherwise the probe could not tell a locked row from a free one."""
    _seed(dsn, email="other@example.com", campaign=OTHER_CAMPAIGN)
    db, participants = _database(dsn), PostgresParticipantStore()
    seat = _a_seat(db, participants)
    probe = "SELECT id FROM campaign.participants WHERE id = %s FOR NO KEY UPDATE NOWAIT"

    with db.transaction() as unit:
        assert participants.hold(unit, seat, campaign_id=OTHER_CAMPAIGN) is None
        with connect(dsn) as other:
            assert other.execute(probe, (seat,)).fetchone() == (seat,), "nothing was locked"

    with db.transaction() as unit:
        assert participants.hold(unit, seat, campaign_id=CAMPAIGN) is not None
        with connect(dsn) as other:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                other.execute(probe, (seat,))


@needs_db
def test_deleting_an_account_that_holds_a_seat_is_refused(dsn: str, owner: int) -> None:
    """L-2. `ON DELETE NO ACTION`: CASCADE would delete a seat the disclosures,
    the character-sheet link and the audit rows reference, and SET NULL would
    re-open an accepted seat to whoever next accepted it. So the database
    refuses to delete an account that holds a seat — live or removed — until
    the account-deletion work (`agent-forge-harness-zkc`) handles seats. An
    account that holds none is deleted as before, which is the control."""
    db, participants = _database(dsn), PostgresParticipantStore()
    seated, removed, free = _accounts(dsn, 3)
    live_seat = _a_seat(db, participants, offered_to=seated)
    with db.transaction() as unit:
        assert participants.accept(unit, CAMPAIGN, live_seat, user_id=seated)
        dropped = participants.add(unit, CAMPAIGN, alias="Wren")
        participants.offer(unit, CAMPAIGN, dropped.id, user_id=removed)
        assert participants.remove(unit, CAMPAIGN, dropped.id)

    with connect(dsn) as conn:
        for account in (seated, removed):
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute("DELETE FROM auth.users WHERE id = %s", (account,))
        conn.execute("DELETE FROM auth.users WHERE id = %s", (free,))
        left = {row[0] for row in conn.execute("SELECT id FROM auth.users").fetchall()}
    assert {seated, removed} <= left and free not in left


@needs_db
def test_a_refused_offer_leaves_the_callers_transaction_usable(dsn: str, owner: int) -> None:
    """The one-live-seat index refuses the second offer with a UniqueViolation,
    which would abort the caller's whole transaction — every write it had
    composed, and every one after — had `offer` not run its statement in a
    savepoint. So: refuse, then write again in the same unit, commit, and find
    the write."""
    db, participants = _database(dsn), PostgresParticipantStore()
    (player,) = _accounts(dsn, 1)
    first = _a_seat(db, participants, offered_to=player)
    with db.transaction() as unit:
        second = participants.add(unit, CAMPAIGN, alias="Wren")
        with pytest.raises(SeatUnavailable):
            participants.offer(unit, CAMPAIGN, second.id, user_id=player)
        later = participants.add(unit, CAMPAIGN, alias="Fern")

    with db.transaction() as unit:
        kept = participants.get(unit, later.id)
        assert kept is not None and kept.is_active, "the write after the refusal was kept"
        refused = participants.get(unit, second.id)
        assert refused is not None and refused.user_id is None
        held = participants.get(unit, first)
        assert held is not None and held.user_id == player


@needs_db
def test_offering_a_seat_to_an_account_that_does_not_exist_is_the_same_refusal(
    dsn: str, owner: int
) -> None:
    """The statement asks whether the account exists rather than letting the
    foreign key refuse it: a `ForeignKeyViolation` would abort the caller's
    whole transaction and arrive carrying the driver's DETAIL, which quotes the
    user id. The twin has no accounts table, so this half is PostgreSQL's."""
    db, participants = _database(dsn), PostgresParticipantStore()
    seat = _a_seat(db, participants)
    with db.transaction() as unit:
        with pytest.raises(SeatUnavailable):
            participants.offer(unit, CAMPAIGN, seat, user_id=987654321)
        still = participants.get(unit, seat)
        assert still is not None and still.user_id is None, "and the transaction is still usable"


@needs_db
def test_the_database_refuses_a_seat_accepted_by_no_account(dsn: str, owner: int) -> None:
    """L-1's CHECK, on the server. The twin's half is
    `test_a_seat_accepted_by_no_account_cannot_exist_in_the_twin`."""
    _a_participant_row(dsn)
    with connect(dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE campaign.participants SET accepted_at = now() WHERE id = %s", (PARTICIPANT,)
            )
        (player,) = _accounts(dsn, 1)
        conn.execute(
            "UPDATE campaign.participants SET user_id = %s, accepted_at = now() WHERE id = %s",
            (player, PARTICIPANT),
        )


# The guards and bounds, which belong to neither world ────────────────────────


@pytest.mark.parametrize(
    ("store", "method", "args"),
    [
        pytest.param(PostgresCampaignStore(), "list_for_owner", (1,), id="campaigns"),
        pytest.param(PostgresParticipantStore(), "get", ("prt_x",), id="participants"),
        pytest.param(
            PostgresTableSessionStore(slot_clear=no_slots), "get", ("ses_x",), id="sessions"
        ),
        pytest.param(PostgresAuditLog(), "for_campaign", ("cmp_x",), id="audit"),
    ],
)
def test_a_postgres_store_refuses_the_twins_unit_of_work(store: Any, method: str, args: tuple) -> None:
    """Without the guard a Postgres store handed the twin's unit would reach for
    a connection that is not there — or, worse, quietly do nothing and report
    success to a caller that believes its aggregate is saved."""
    with InMemoryDatabase().transaction() as unit:
        with pytest.raises(TypeError, match="PostgreSQL transaction"):
            getattr(store, method)(unit, *args)


class _Rows:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def fetchone(self) -> tuple | None:
        return self._row


class _ScriptedConnection:
    """Just enough of a connection for `PostgresParticipantStore.offer`: it
    answers `hold`'s two statements with an open seat, then fails the offer's
    UPDATE — inside the savepoint — with the driver error it was given. What a
    real server does between the EXISTS and the foreign-key check cannot be
    interleaved from a test, so the error is scripted rather than raced."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.savepoints = 0
        self.inside_savepoint = False

    def execute(self, statement: str, params: tuple = ()) -> _Rows:
        if statement.lstrip().startswith("UPDATE"):
            assert self.inside_savepoint, "the offer's statement runs inside its savepoint"
            raise self.error
        if "set_config" in statement:
            return _Rows(("5s",))
        return _Rows(("prt_x", "cmp_x", "Rook", datetime.now(UTC), None, None, None))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.savepoints += 1
        self.inside_savepoint = True
        try:
            yield
        finally:
            self.inside_savepoint = False


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            psycopg.errors.UniqueViolation(
                'duplicate key value violates unique constraint "participants_one_live_seat_per_account_uidx"'
                "\nDETAIL:  Key (campaign_id, user_id)=(cmp_x, 987654321) already exists."
            ),
            id="one-live-seat-index",
        ),
        pytest.param(
            psycopg.errors.ForeignKeyViolation(
                'insert or update on table "participants" violates foreign key constraint '
                '"participants_user_id_fkey"\nDETAIL:  Key (user_id)=(987654321) is not present.'
            ),
            id="account-deleted-mid-offer",
        ),
    ],
)
def test_a_driver_refusal_inside_offer_becomes_the_one_refusal_with_nothing_attached(
    error: Exception,
) -> None:
    """M-1 and N-1. The index's UniqueViolation, and the ForeignKeyViolation an
    account deleted between the offer's EXISTS and its foreign-key check would
    raise, both quote the account in their DETAIL. Each becomes the one
    `SeatUnavailable`, raised OUTSIDE the handler so that neither `__cause__`
    nor `__context__` carries the driver's error — `from None` would only have
    hidden it from a printed traceback."""
    connection = _ScriptedConnection(error)
    account = PRIVATE_USER_ID
    with pytest.raises(SeatUnavailable) as refused:
        PostgresParticipantStore().offer(PgTransaction(conn=connection), "cmp_x", "prt_x", user_id=account)
    assert refused.value.__context__ is None and refused.value.__cause__ is None
    assert str(refused.value) == SeatUnavailable.MESSAGE
    assert connection.savepoints == 1
    assert str(PRIVATE_USER_ID) not in "".join(traceback.format_exception(refused.value))


def test_an_in_memory_store_refuses_a_postgres_unit_of_work() -> None:
    db = InMemoryDatabase()
    unit = PgTransaction(conn=None)
    with pytest.raises(TypeError, match="in-memory transaction"):
        InMemoryCampaignStore(db).list_for_owner(unit, 1)


@pytest.mark.parametrize("name", ["", "n" * 121])
def test_a_campaign_name_outside_the_column_bound_is_refused_before_the_statement(name: str) -> None:
    """The same bound `0004_campaign_schema.sql` carries, so the caller gets this
    refusal rather than an integrity error naming a constraint."""
    with pytest.raises(ValueError, match="1 to 120 characters"):
        check_name(name)


@pytest.mark.parametrize("alias", ["", "a" * 41, "   "])
def test_an_alias_outside_the_column_bound_is_refused_without_being_quoted(alias: str) -> None:
    with pytest.raises(ValueError, match="1 to 40 characters") as refusal:
        check_alias(alias)
    assert alias not in str(refusal.value) or not alias.strip(), (
        "a refusal never repeats private text"
    )


def test_an_alias_with_a_control_character_is_refused() -> None:
    """Invisible, survives no round trip intact, and the way two aliases are made
    to look identical to the one person who sees them (AUD-11)."""
    for control in (chr(0), chr(7), chr(0x200B), chr(0x2060)):
        with pytest.raises(ValueError, match="control or formatting"):
            check_alias(f"Ro{control}ok")


def test_an_alias_follows_the_one_rule_for_stored_text() -> None:
    """ysj and 5mj, by the lead ruling of 2026-09-21: one opinion about stored
    text, `check_plain_text`'s, and the alias agrees with it. Everything that
    refuses is refused here too — on the alias as SENT, because `str.split()`
    would otherwise quietly turn a vertical tab, a file separator or NEL into a
    space — and the two joiners real names need are accepted and kept."""
    for code in sorted(REFUSED_TEXT_CODE_POINTS) + [0xD800]:
        with pytest.raises(ValueError, match="an alias carries no control or formatting") as refused:
            check_alias(f"Wren{chr(code)}hold")
        assert "Wren" not in str(refused.value), "a refusal never repeats private text"
    for joiner in (0x200C, 0x200D):
        assert check_alias(f"Wren{chr(joiner)}hold") == f"Wren{chr(joiner)}hold"


def test_the_alias_key_is_the_comparison_both_worlds_make() -> None:
    """Computed by the application on purpose: `lower()` in the index would
    answer according to the database's collation provider, and `str.lower()`
    according to Python, and the two disagree."""
    assert alias_key("ROOK") == alias_key("rook") == "rook"
    assert alias_key("Straße") == alias_key("STRASSE") == "strasse"
    assert alias_key("ΑΣ") == alias_key("ας")
    assert alias_key("Ａｎａ") == alias_key("Ana") == "ana", "NFKC, not only casefold"


def test_the_slot_clearing_extension_point_is_empty_in_this_bead() -> None:
    """`1kg.7.1` fills it. Until then a narrowing has nothing to clear, and this
    says so in one place rather than by omission."""
    with InMemoryDatabase().transaction() as unit:
        assert no_slots(unit, "ses_x") is None


# ══ 1kg.2.2: campaigns, seats, offers and the GM's confirmation ═════════════
#
# The shared suite for bead 1kg.2.2, in both worlds. The races — two
# connections, explicit interleaving, `pg_stat_activity` — are
# `tests/test_seats_db.py`'s.

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _offer_store(world: World) -> Any:
    return InMemorySeatOfferStore(world.db) if world.kind == "fake" else PostgresSeatOfferStore()


def _job_queue(world: World) -> Any:
    return InMemoryJobQueue(world.db) if world.kind == "fake" else PostgresJobQueue(world.db)


def _summary_store(world: World) -> Any:
    if world.kind == "fake":
        return InMemoryCampaignSummaryStore(world.db, messages=InMemoryMessageStore())
    return PostgresCampaignSummaryStore()


def _stores(world: World) -> CampaignStores:
    return CampaignStores(
        world.campaigns, world.participants, world.sessions, _offer_store(world), world.audit, _summary_store(world)
    )


class _Recording:
    """The world's database, recording every unit of work it opens, so a test
    can read what each one locked — in either world."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self.units: list[Any] = []

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.db.transaction() as unit:
            self.units.append(unit)
            yield unit


def _revision(world: World, campaign_id: str) -> int | None:
    with world.db.transaction() as unit:
        return world.campaigns.authz_revision(unit, campaign_id)


def _ledger(world: World, campaign_id: str) -> list[str]:
    with world.db.transaction() as unit:
        return [event.action for event in world.audit.for_campaign(unit, campaign_id)]


def _jobs_enqueued(world: World, queue: Any) -> list[tuple[str, dict[str, Any], str | None]]:
    """(kind, payload, dedupe_key) of every committed job, oldest first."""
    if world.kind == "fake":
        return [
            (row.job.kind, dict(row.job.payload), row.dedupe_key)
            for row in sorted(queue._rows.values(), key=lambda row: row.job.id)
        ]
    with world.db.transaction() as unit:
        rows = unit.conn.execute("SELECT kind, payload, dedupe_key FROM app.jobs ORDER BY id").fetchall()
    return [(kind, dict(payload), key) for kind, payload, key in rows]


def _held_offer(world: World, campaign_id: str, alias: str, address: str, *, now: datetime = T0) -> tuple[str, str]:
    """An open seat and an offer of it to `address`, made by the owner."""
    seat = _a_participant(world, campaign_id, alias)
    offers = _offer_store(world)
    with world.db.transaction() as unit:
        made = offers.create(unit, campaign_id, seat, offered_by=world.owner, address=address, now=now)
    return seat, made.id


# ── Campaigns: rename, the page, the name rule ──────────────────────────────


def test_a_rename_changes_the_name_and_a_rename_to_the_same_name_changes_nothing(world: World) -> None:
    campaign = _a_campaign(world, name="Nocturne")
    later = datetime.now(UTC) + timedelta(minutes=5)
    with world.db.transaction() as unit:
        renamed = world.campaigns.rename(unit, campaign, owner_id=world.owner, name="Aubade", now=later)
    assert renamed is not None and renamed.name == "Aubade" and renamed.updated_at == later
    with world.db.transaction() as unit:
        again = world.campaigns.rename(
            unit, campaign, owner_id=world.owner, name="Aubade", now=later + timedelta(hours=1)
        )
    assert again is not None and again.updated_at == later, "a rename to the same name changes nothing"
    with world.db.transaction() as unit:
        assert world.campaigns.rename(unit, campaign, owner_id=world.other_owner, name="Mine") is None
        stored = world.campaigns.get(unit, campaign, owner_id=world.owner)
    assert stored is not None and stored.name == "Aubade"


def test_a_campaign_name_with_a_control_character_is_refused_before_the_statement(world: World) -> None:
    campaign = _a_campaign(world)
    for code in (0x00, 0x1B, 0x202E, 0xFEFF):
        with world.db.transaction() as unit:
            with pytest.raises(ValueError, match="no control or formatting") as refused:
                world.campaigns.rename(unit, campaign, owner_id=world.owner, name=f"Noc{chr(code)}turne")
            assert "Noc" not in str(refused.value)
            with pytest.raises(ValueError, match="no control or formatting"):
                world.campaigns.create(unit, owner_id=world.owner, name=f"Noc{chr(code)}turne")
    with pytest.raises(ValueError, match="no control or formatting"):
        check_name("Noc" + chr(0xD800))


def _made_at(world: World, name: str, moment: datetime, *, owner: int | None = None) -> str:
    with world.db.transaction() as unit:
        return world.campaigns.create(
            unit, owner_id=world.owner if owner is None else owner, name=name, now=moment
        ).id


def _walk_campaigns(world: World, *, limit: int, include_archived: bool = False) -> list[list[str]]:
    pages: list[list[str]] = []
    cursor: str | None = None
    for _ in range(20):
        with world.db.transaction() as unit:
            page = world.campaigns.page_for_owner(
                unit, world.owner, include_archived=include_archived, cursor=cursor, limit=limit
            )
        pages.append([c.id for c in page.items])
        cursor = page.next_cursor
        if cursor is None:
            return pages
    raise AssertionError("the walk did not end")


def test_the_campaign_page_is_newest_first_ties_by_id_and_walks_every_row_once(world: World) -> None:
    oldest = _made_at(world, "One", T0)
    tied = sorted([_made_at(world, "Two", T0 + timedelta(hours=1)), _made_at(world, "Three", T0 + timedelta(hours=1))])
    newest = _made_at(world, "Four", T0 + timedelta(hours=2))
    _made_at(world, "Theirs", T0 + timedelta(hours=3), owner=world.other_owner)
    expected = [newest, tied[1], tied[0], oldest]
    assert sum(_walk_campaigns(world, limit=50), []) == expected
    pages = _walk_campaigns(world, limit=3)
    assert pages == [expected[:3], expected[3:]], "a full page carries a cursor; the last does not"
    assert sum(_walk_campaigns(world, limit=1), []) == expected
    assert _walk_campaigns(world, limit=4) == [expected], "exactly a page is the last page"


def test_the_campaign_page_leaves_archived_ones_out_unless_asked(world: World) -> None:
    kept = _made_at(world, "Kept", T0)
    shelved = _made_at(world, "Shelved", T0 + timedelta(hours=1))
    with world.db.transaction() as unit:
        assert world.campaigns.set_archived(unit, shelved, owner_id=world.owner, archived=True)
    assert _walk_campaigns(world, limit=50) == [[kept]]
    assert _walk_campaigns(world, limit=50, include_archived=True) == [[shelved, kept]]


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64-json",
        encode_cursor(T0, "prt_" + "a" * 22),
        encode_cursor(T0, "cmp_" + "a" * 21 + chr(0x07)),
        encode_cursor(T0.replace(tzinfo=None), "cmp_" + "a" * 22),
    ],
    ids=["garbage", "another-kinds-id", "a-control-character", "a-naive-moment"],
)
def test_a_cursor_this_server_did_not_make_is_the_one_refusal(world: World, cursor: str) -> None:
    """L-19 and `kky`: an id outside its prefix's shape — a control character
    in it included — is `InvalidCursor` before any statement, never a 500."""
    with world.db.transaction() as unit:
        with pytest.raises(InvalidCursor) as refused:
            world.campaigns.page_for_owner(unit, world.owner, cursor=cursor)
        assert refused.value.__context__ is None and refused.value.__cause__ is None
        assert str(refused.value) == InvalidCursor.MESSAGE


# ── Archive: RQ-5's two steps; restore: a locked widening ───────────────────


def test_archive_step_one_narrows_a_live_session_without_the_campaign_lock(world: World) -> None:
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    recording = _Recording(world.db)
    archive_step_one(recording, _stores(world), campaign_id=campaign, owner_id=world.owner)
    assert [unit.campaign_locks for unit in recording.units] == [[]], "step 1 never asks for the lock"
    with world.db.transaction() as unit:
        narrowed = world.sessions.get(unit, session.id)
        still = world.campaigns.get(unit, campaign, owner_id=world.owner)
    assert narrowed is not None and narrowed.reveal_epoch == session.reveal_epoch + 1
    assert still is not None and not still.is_archived, "step 1 changes no fact"
    assert _revision(world, campaign) == 0 and _ledger(world, campaign) == []


def test_archive_step_two_narrows_a_session_step_one_did_not_see_then_archives_once(world: World) -> None:
    campaign = _a_campaign(world)
    stores = _stores(world)
    archive_step_one(world.db, stores, campaign_id=campaign, owner_id=world.owner)
    session = _a_session(world, campaign)  # started between the steps
    recording = _Recording(world.db)
    archived = archive_step_two(recording, stores, campaign_id=campaign, owner_id=world.owner, now=T0)
    assert recording.units[0].campaign_locks == [(campaign, "exclusive")]
    assert archived.is_archived and archived.archived_at == T0
    with world.db.transaction() as unit:
        narrowed = world.sessions.get(unit, session.id)
    assert narrowed is not None and narrowed.reveal_epoch == session.reveal_epoch + 1
    assert narrowed.is_live, "archive narrows; ending the session is 1kg.2.6's"
    assert _revision(world, campaign) == 1
    assert _ledger(world, campaign) == ["campaign.archived"]


def test_archiving_an_archived_campaign_and_restoring_a_live_one_change_nothing(world: World) -> None:
    campaign = _a_campaign(world)
    stores = _stores(world)
    archive(world.db, stores, campaign_id=campaign, owner_id=world.owner, now=T0)
    session = _a_session(world, _a_campaign(world, name="Other"))  # a live session elsewhere
    archive(world.db, stores, campaign_id=campaign, owner_id=world.owner, now=T0 + timedelta(hours=1))
    assert _revision(world, campaign) == 1 and _ledger(world, campaign) == ["campaign.archived"]
    recording = _Recording(world.db)
    restored = restore(recording, stores, campaign_id=campaign, owner_id=world.owner, now=T0)
    assert not restored.is_archived and _revision(world, campaign) == 2
    recording = _Recording(world.db)
    again = restore(recording, stores, campaign_id=campaign, owner_id=world.owner, now=T0)
    assert not again.is_archived
    assert [unit.campaign_locks for unit in recording.units] == [[]], "restoring a live campaign takes no lock"
    assert _ledger(world, campaign) == ["campaign.archived", "campaign.restored"]
    with world.db.transaction() as unit:
        untouched = world.sessions.get(unit, session.id)
    assert untouched is not None and untouched.reveal_epoch == session.reveal_epoch


def test_another_gms_campaign_is_not_found_by_archive_or_restore(world: World) -> None:
    theirs = _a_campaign(world, owner=world.other_owner, name="Theirs")
    stores = _stores(world)
    for call in (
        lambda: archive(world.db, stores, campaign_id=theirs, owner_id=world.owner, now=T0),
        lambda: restore(world.db, stores, campaign_id=theirs, owner_id=world.owner, now=T0),
    ):
        with pytest.raises(HTTPException) as refused:
            call()
        assert refused.value.status_code == 404
    assert _revision(world, theirs) == 0 and _ledger(world, theirs) == []


# ── The GM's confirmation ────────────────────────────────────────────────────


def test_confirm_is_true_once_then_false_and_sets_confirmed_at(world: World) -> None:
    campaign = _a_campaign(world)
    seat = _seat(world, campaign, "Rook", world.players[0])
    with world.db.transaction() as unit:
        assert world.participants.confirm(unit, campaign, seat, now=T0) is True
    for _ in range(2):
        with world.db.transaction() as unit:
            assert world.participants.confirm(unit, campaign, seat, now=T0 + timedelta(days=1)) is False
    with world.db.transaction() as unit:
        held = world.participants.get(unit, seat)
        assert world.participants.seat_for(unit, campaign, world.players[0]) is not None
    assert held is not None and held.confirmed_at == T0 and held.is_confirmed


def test_confirm_refuses_every_seat_it_cannot_confirm_identically_in_both_worlds(world: World) -> None:
    campaign = _a_campaign(world)
    theirs = _a_campaign(world, owner=world.other_owner, name="Theirs")
    foreign = _seat(world, theirs, "Rook", world.players[0])
    removed = _seat(world, campaign, "Wren", world.players[1])
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, removed)
    open_seat = _a_participant(world, campaign, "Kestrel")
    offered = _a_participant(world, campaign, "Finch")
    unavailable = [("prt_" + "z" * 22, campaign), (foreign, campaign), (removed, campaign)]
    for seat, where in unavailable:
        with world.db.transaction() as unit:
            with pytest.raises(SeatUnavailable) as refused:
                world.participants.confirm(unit, where, seat)
            assert str(refused.value) == SeatUnavailable.MESSAGE
    with world.db.transaction() as unit:
        with pytest.raises(SeatNotAccepted) as refused:
            world.participants.confirm(unit, campaign, open_seat)
        assert str(refused.value) == SeatNotAccepted.MESSAGE
    with world.db.transaction() as unit:
        world.participants.offer(unit, campaign, offered, user_id=world.players[2])
        with pytest.raises(SeatNotAccepted):
            world.participants.confirm(unit, campaign, offered)


def test_a_seat_confirmed_before_it_was_accepted_cannot_exist_in_the_twin() -> None:
    with pytest.raises(ValueError, match="confirmed after it is accepted"):
        Participant("prt_x", "cmp_x", "Rook", T0, user_id=3, confirmed_at=T0)


def test_a_removed_confirmed_seat_is_no_longer_confirmed(world: World) -> None:
    campaign = _a_campaign(world)
    seat = _seat(world, campaign, "Rook", world.players[0])
    with world.db.transaction() as unit:
        world.participants.confirm(unit, campaign, seat)
        world.participants.remove(unit, campaign, seat)
        held = world.participants.get(unit, seat)
    assert held is not None and held.confirmed_at is not None and not held.is_confirmed


# ── Offers ──────────────────────────────────────────────────────────────────


def test_an_offer_is_created_open_for_fourteen_days_and_names_no_account(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `repr(made)` also prints the offer's three random ids (secrets.token_urlsafe),
    # and "Wren" or "example" could turn up in one by chance. Rather than take the
    # ids out of the rendered repr before checking it -- which would also hide a
    # store that slipped address text into the one id it mints itself, `made.id`,
    # since only campaign_id and participant_id are pinned by the equality assert
    # below -- mint digit-only ids for this test. A digit body can never match
    # either probe, so the whole, unstripped repr can be checked directly.
    ids = count()
    monkeypatch.setattr(campaign_identity.secrets, "token_urlsafe", lambda _n: f"{next(ids):022d}")
    campaign = _a_campaign(world)
    seat, offer_id = _held_offer(world, campaign, "Rook", "Wren@Example.com")
    with world.db.transaction() as unit:
        made = _offer_store(world).get(unit, offer_id)
    assert made is not None
    assert (made.campaign_id, made.participant_id, made.offered_by) == (campaign, seat, world.owner)
    assert (made.address, made.address_key) == ("Wren@Example.com", "wren@example.com")
    assert made.expires_at - made.created_at == OFFER_LIFETIME and made.outcome is None
    # The offerer, the address and its key are hidden from `repr()` (SeatOffer's
    # docstring: a repr reaches a log line, and ids also go into URLs). Confirm
    # the dataclass marks all three unrepr'd -- a mutant that flips one back to
    # `repr=True` would otherwise survive, since a hidden account id never shows
    # up in these three probe strings -- and that the probes are absent too.
    assert {f.name for f in fields(made) if not f.repr} >= {"offered_by", "address", "address_key"}
    rendered = repr(made)
    assert "Wren" not in rendered and "example" not in rendered
    assert f"offered_by={world.owner}" not in rendered


def test_a_seat_holds_one_open_offer_and_the_refusal_names_nothing(world: World) -> None:
    campaign = _a_campaign(world)
    seat, _ = _held_offer(world, campaign, "Rook", "wren@example.com")
    with world.db.transaction() as unit:
        with pytest.raises(SeatUnavailable) as refused:
            _offer_store(world).create(unit, campaign, seat, offered_by=world.owner, address="finch@example.com")
        assert refused.value.__context__ is None and "finch" not in str(refused.value)


def test_an_offer_names_a_seat_of_its_own_campaign_made_by_its_owner(world: World) -> None:
    campaign = _a_campaign(world)
    theirs = _a_campaign(world, owner=world.other_owner, name="Theirs")
    their_seat = _a_participant(world, theirs, "Rook")
    mine = _a_participant(world, campaign, "Wren")
    offers = _offer_store(world)
    for where, seat, by in ((campaign, their_seat, world.owner), (campaign, mine, world.other_owner)):
        with world.db.transaction() as unit:
            with pytest.raises(SeatUnavailable):
                offers.create(unit, where, seat, offered_by=by, address="finch@example.com")


def test_a_stale_offer_is_marked_expired_lazily_under_its_own_seat(world: World) -> None:
    campaign = _a_campaign(world)
    seat, offer_id = _held_offer(world, campaign, "Rook", "wren@example.com")
    other, other_id = _held_offer(world, campaign, "Finch", "finch@example.com")
    offers = _offer_store(world)
    later = T0 + OFFER_LIFETIME + timedelta(seconds=1)
    with world.db.transaction() as unit:
        assert offers.expire_stale(unit, campaign, seat, now=T0 + timedelta(days=1)) is False
        assert offers.expire_stale(unit, campaign, seat, now=later) is True
        assert offers.expire_stale(unit, campaign, seat, now=later) is False
        marked, untouched = offers.get(unit, offer_id), offers.get(unit, other_id)
    assert marked is not None and (marked.outcome, marked.answered_at) == (EXPIRED, marked.expires_at)
    assert untouched is not None and untouched.outcome is None, "only this seat's offer is marked"
    with world.db.transaction() as unit:
        offers.create(unit, campaign, seat, offered_by=world.owner, address="wren@example.com", now=later)
    del other


def test_a_repeat_is_a_live_offer_on_any_seat_or_an_accepted_seat_of_that_key(world: World) -> None:
    campaign = _a_campaign(world)
    offers = _offer_store(world)
    _held_offer(world, campaign, "Rook", "Wren@Example.com")
    with world.db.transaction() as unit:
        assert offers.is_repeat(unit, campaign, "wren@example.com", now=T0)
        assert offers.is_repeat(unit, campaign, address_key("WREN@EXAMPLE.COM"), now=T0), "ASCII case folds"
        assert not offers.is_repeat(unit, campaign, "finch@example.com", now=T0)
        assert not offers.is_repeat(unit, campaign, "wren@example.com", now=T0 + OFFER_LIFETIME), "expired"
    other = _a_campaign(world, name="Other")
    with world.db.transaction() as unit:
        assert not offers.is_repeat(unit, other, "wren@example.com", now=T0), "per campaign"
    seat, offer_id = _held_offer(world, campaign, "Finch", "finch@example.com")
    with world.db.transaction() as unit:
        world.participants.offer(unit, campaign, seat, user_id=world.players[0])
        world.participants.accept(unit, campaign, seat, user_id=world.players[0])
        offers.close(unit, offer_id, outcome=ACCEPTED, now=T0)
    with world.db.transaction() as unit:
        assert offers.is_repeat(unit, campaign, "finch@example.com", now=T0 + OFFER_LIFETIME * 2)
        world.participants.remove(unit, campaign, seat)
    with world.db.transaction() as unit:
        assert not offers.is_repeat(unit, campaign, "finch@example.com", now=T0), "a removed seat repeats nothing"


def test_the_throttle_counts_every_offer_in_the_window_whatever_became_of_it(world: World) -> None:
    campaign = _a_campaign(world)
    offers = _offer_store(world)
    _, first = _held_offer(world, campaign, "Rook", "one@example.com", now=T0)
    _, _second = _held_offer(world, campaign, "Wren", "two@example.com", now=T0 + timedelta(hours=1))
    with world.db.transaction() as unit:
        offers.close(unit, first, outcome=DECLINED, now=T0 + timedelta(minutes=5))
    with world.db.transaction() as unit:
        counted = offers.count_recent(unit, world.owner, now=T0 + timedelta(hours=2))
        assert (counted.count, counted.oldest) == (2, T0)
        assert offers.count_recent(unit, world.owner, now=T0 + THROTTLE_WINDOW + timedelta(minutes=1)).count == 1
        assert offers.count_recent(unit, world.other_owner, now=T0).count == 0
    left = THROTTLE_WINDOW - timedelta(hours=2)
    assert throttle_wait_s(counted, T0 + timedelta(hours=2)) == int(left.total_seconds())
    assert throttle_wait_s(ThrottleCount(30, T0), T0 + THROTTLE_WINDOW - timedelta(milliseconds=1)) == 1


def _invitee(world: World, key: str, user: int, *, now: datetime = T0) -> list[str]:
    with world.db.transaction() as unit:
        page = _offer_store(world).invitee_page(unit, key, user, now=now)
    return [found.offer.id for found in page.items]


def test_the_invitee_sees_an_offer_only_through_all_seven_predicates(world: World) -> None:
    """L-9, each predicate shown excluding a row it alone excludes."""
    player = world.players[0]
    key = "wren@example.com"
    offers = _offer_store(world)
    campaign = _a_campaign(world)
    _, shown = _held_offer(world, campaign, "Rook", "Wren@Example.com")
    assert _invitee(world, key, player) == [shown]
    assert _invitee(world, "finch@example.com", player) == [], "1. another key"
    assert _invitee(world, key, player, now=T0 + OFFER_LIFETIME) == [], "2. expired"
    removed_campaign = _a_campaign(world, name="Removed")
    removed_seat, _ = _held_offer(world, removed_campaign, "Rook", key)
    with world.db.transaction() as unit:
        world.participants.remove(unit, removed_campaign, removed_seat)
    archived = _a_campaign(world, name="Archived")
    _held_offer(world, archived, "Rook", key)
    with world.db.transaction() as unit:
        world.campaigns.set_archived(unit, archived, owner_id=world.owner, archived=True)
    assert _invitee(world, key, player) == [shown], "3. a removed seat; 4. an archived campaign"
    assert _invitee(world, key, world.owner) == [], "5. the caller's own campaign"
    with world.db.transaction() as unit:
        offers.block(unit, blocker_user_id=world.players[1], blocked_owner_id=world.owner)
    assert _invitee(world, key, world.players[1]) == [], "6. an owner the caller blocked"
    seated = _seat(world, campaign, "Finch", player)
    assert _invitee(world, key, player) == [], "7. a campaign where the caller already has a seat"
    del seated
    with world.db.transaction() as unit:
        assert offers.close(unit, shown, outcome=DECLINED, now=T0)
    assert _invitee(world, key, world.players[2]) == [], "an answered offer is not open"


def test_the_invitee_page_is_newest_first_and_walks_once(world: World) -> None:
    key = "wren@example.com"
    made = []
    for n in range(3):
        campaign = _a_campaign(world, name=f"C{n}")
        made.append(_held_offer(world, campaign, f"Rook{n}", key, now=T0 + timedelta(hours=n))[1])
    offers = _offer_store(world)
    seen: list[str] = []
    seen_names: list[str] = []
    seen_aliases: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        with world.db.transaction() as unit:
            page = offers.invitee_page(unit, key, world.players[0], now=T0 + timedelta(days=1), cursor=cursor, limit=2)
        seen += [found.offer.id for found in page.items]
        seen_names += [found.campaign_name for found in page.items]
        seen_aliases += [found.alias for found in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == list(reversed(made))
    # C1, C2: each item's campaign name and seat alias travel with ITS row, not
    # the page's last row (mutant M10: pg's invitee_page giving every item
    # rows[-1]'s campaign name) or a shared alias (a wrong-seat-alias mutant:
    # swapping in another item's alias would pass if every seat answered to
    # the same name).
    assert seen_names == ["C2", "C1", "C0"]
    assert seen_aliases == ["Rook2", "Rook1", "Rook0"]
    with world.db.transaction() as unit:
        found = offers.find_for_invitee(unit, made[0], key, world.players[0], now=T0)
    assert found is not None and (found.campaign_name, found.alias) == ("C0", "Rook0")
    # `repr(found)` also holds SeatOffer's random ids (secrets.token_urlsafe), so
    # a short name like "C0" can appear there by chance. Check two things that
    # do not depend on those ids: the dataclass marks both fields hidden, and
    # what `repr(found)` prints outside the nested offer names neither of them
    # (a hand-written `__repr__` would pass the first check but not this one).
    assert {f.name for f in fields(found) if not f.repr} >= {"campaign_name", "alias"}
    rendered = repr(found).replace(repr(found.offer), "")
    assert "C0" not in rendered and "Rook" not in rendered


def test_decline_block_and_the_repeat_readers(world: World) -> None:
    campaign = _a_campaign(world)
    offers = _offer_store(world)
    seat, offer_id = _held_offer(world, campaign, "Rook", "wren@example.com")
    with world.db.transaction() as unit:
        assert offers.close(unit, offer_id, outcome=DECLINED, now=T0) is True
        assert offers.close(unit, offer_id, outcome=DECLINED, now=T0) is False
        assert offers.block(unit, blocker_user_id=world.players[0], blocked_owner_id=world.owner) is True
        assert offers.block(unit, blocker_user_id=world.players[0], blocked_owner_id=world.owner) is False
        assert offers.is_blocked(unit, world.players[0], world.owner)
        assert not offers.is_blocked(unit, world.owner, world.players[0])
        assert offers.repeat_decline(unit, offer_id, "wren@example.com") is not None
        assert offers.repeat_decline(unit, offer_id, "finch@example.com") is None
        assert offers.repeat_accept(unit, offer_id, "wren@example.com", world.players[0]) is None
    del seat


def test_remove_withdraws_the_open_offer_and_an_expired_one_reads_expired(world: World) -> None:
    campaign = _a_campaign(world)
    offers = _offer_store(world)
    seat, live = _held_offer(world, campaign, "Rook", "wren@example.com")
    stale_seat, stale = _held_offer(world, campaign, "Finch", "finch@example.com")
    moment = T0 + timedelta(days=1)
    with world.db.transaction() as unit:
        assert offers.withdraw_open(unit, campaign, seat, now=moment) is True
        assert offers.withdraw_open(unit, campaign, seat, now=moment) is False
        assert offers.withdraw_open(unit, campaign, stale_seat, now=T0 + OFFER_LIFETIME * 2) is True
        withdrawn, expired = offers.get(unit, live), offers.get(unit, stale)
    assert withdrawn is not None and (withdrawn.outcome, withdrawn.answered_at) == (WITHDRAWN, moment)
    assert expired is not None and (expired.outcome, expired.answered_at) == (EXPIRED, expired.expires_at)


# ── The status function ──────────────────────────────────────────────────────


def _an_offer(outcome: str | None = None, *, at: datetime = T0) -> SeatOffer:
    return SeatOffer(
        "sof_x", "cmp_x", "prt_x", 1, "a@b.c", "a@b.c", at, at + OFFER_LIFETIME,
        outcome, None if outcome is None else at,
    )


@pytest.mark.parametrize(
    ("seat", "latest", "status"),
    [
        (Participant("prt_x", "cmp_x", "Rook", T0), None, SeatStatus.OPEN),
        (Participant("prt_x", "cmp_x", "Rook", T0), _an_offer(), SeatStatus.OFFERED),
        (Participant("prt_x", "cmp_x", "Rook", T0), _an_offer(DECLINED), SeatStatus.NOT_ACCEPTED),
        (Participant("prt_x", "cmp_x", "Rook", T0), _an_offer(EXPIRED), SeatStatus.NOT_ACCEPTED),
        (Participant("prt_x", "cmp_x", "Rook", T0), _an_offer(at=T0 - OFFER_LIFETIME * 2), SeatStatus.NOT_ACCEPTED),
        (Participant("prt_x", "cmp_x", "Rook", T0, user_id=3, accepted_at=T0), _an_offer(ACCEPTED),
         SeatStatus.AWAITING_CONFIRMATION),
        (Participant("prt_x", "cmp_x", "Rook", T0, user_id=3, accepted_at=T0, confirmed_at=T0), _an_offer(ACCEPTED),
         SeatStatus.CONFIRMED),
        (Participant("prt_x", "cmp_x", "Rook", T0, removed_at=T0), _an_offer(WITHDRAWN), SeatStatus.REMOVED),
    ],
    ids=["open", "offered", "declined", "expired-marked", "expired-unmarked", "awaiting", "confirmed", "removed"],
)
def test_the_status_function_derives_each_of_the_six_statuses(
    seat: Participant, latest: SeatOffer | None, status: SeatStatus
) -> None:
    assert seat_status(seat, latest, T0 + timedelta(minutes=1)) is status


# ── The player's seats ───────────────────────────────────────────────────────


def test_the_players_seat_page_is_newest_acceptance_first_and_leaves_archived_out(world: World) -> None:
    player = world.players[0]
    campaigns = [_a_campaign(world, name=f"C{n}") for n in range(3)]
    seats = [_seat(world, c, "Rook", player, now=T0 + timedelta(hours=n)) for n, c in enumerate(campaigns)]
    with world.db.transaction() as unit:
        world.campaigns.set_archived(unit, campaigns[1], owner_id=world.owner, archived=True)
        world.participants.confirm(unit, campaigns[0], seats[0])
    walked: list[tuple[str, str, bool]] = []
    cursor: str | None = None
    for _ in range(5):
        with world.db.transaction() as unit:
            page = world.participants.seat_page_for_user(unit, player, cursor=cursor, limit=1)
        walked += [(h.seat.campaign_id, h.campaign_name, h.seat.is_confirmed) for h in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert walked == [(campaigns[2], "C2", False), (campaigns[0], "C0", True)]
    with world.db.transaction() as unit:
        assert world.participants.seat_page_for_user(unit, world.players[1]).items == []


def _cursor_payload(cursor: str) -> list[Any]:
    """What a page cursor carries, decoded here by hand rather than by the
    store's own reader, which would accept only what it expects."""
    return list(json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))))


def test_the_players_seat_cursor_is_the_acceptance_and_the_campaign_never_a_seat_id(world: World) -> None:
    """L-9 and SEC-43 (R145-M2): no participant id, ever, not even the caller's
    own. The cursor is keyed on the acceptance time and the campaign id, which
    the page already shows; a live seat is unique per account and campaign, so
    the pair orders the list totally. Four acceptances at one instant are
    walked one row at a time: ties by campaign id compared by code point, none
    skipped and none repeated."""
    player = world.players[0]
    campaigns = [_a_campaign(world, name=f"Tie{n}") for n in range(4)]
    seats = [_seat(world, c, "Rook", player, now=T0) for c in campaigns]
    walked: list[str] = []
    cursors: list[str] = []
    cursor: str | None = None
    for _ in range(6):
        with world.db.transaction() as unit:
            page = world.participants.seat_page_for_user(unit, player, cursor=cursor, limit=1)
        walked += [h.seat.campaign_id for h in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
        cursors.append(cursor)
    payloads = [_cursor_payload(c) for c in cursors]
    assert [v for p in payloads for v in p if str(v).startswith("prt_")] == [], "a cursor decodes to a participant id"
    assert [s for s in seats for p in payloads if s in json.dumps(p)] == []
    assert [(datetime.fromisoformat(at), campaign) for at, campaign in payloads] == [(T0, c) for c in walked[:3]]
    assert walked == sorted(campaigns), "ties by campaign id, by code point, each exactly once"


def test_the_seat_page_is_oldest_first_and_counts_the_live_seats(world: World) -> None:
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        made = [
            world.participants.add(unit, campaign, alias=f"Seat{n}", now=T0 + timedelta(seconds=n)).id
            for n in range(3)
        ]
    with world.db.transaction() as unit:
        world.participants.remove(unit, campaign, made[1])
    with world.db.transaction() as unit:
        assert world.participants.count_live(unit, campaign) == 2
        first = world.participants.page_for_campaign(unit, campaign, limit=1)
        second = world.participants.page_for_campaign(unit, campaign, cursor=first.next_cursor, limit=1)
        everyone = world.participants.page_for_campaign(unit, campaign, include_removed=True)
    assert [p.id for p in first.items + second.items] == [made[0], made[2]] and second.next_cursor is None
    assert [p.id for p in everyone.items] == made


# ── campaign.reconcile ───────────────────────────────────────────────────────


def test_reconcile_advances_the_revision_under_the_exclusive_lock_and_calls_the_slot_step(world: World) -> None:
    campaign = _a_campaign(world)
    seen: list[tuple[list[tuple[str, str]], str]] = []

    def slots(unit: Any, campaign_id: str) -> None:
        seen.append((list(unit.campaign_locks), campaign_id))

    assert reconcile(world.db, campaign, slots=slots) is True
    assert seen == [([(campaign, "exclusive")], campaign)]
    assert _revision(world, campaign) == 1
    assert reconcile_slots(None, campaign) is None  # type: ignore[arg-type]


def test_reconcile_completes_as_a_no_op_on_a_campaign_that_is_gone(world: World) -> None:
    calls: list[str] = []
    gone = "cmp_" + "g" * 22
    assert reconcile(world.db, gone, slots=lambda unit, cid: calls.append(cid)) is False
    assert calls == []
    job = Job(1, RECONCILE_KIND, {"campaign_id": gone}, 1, T0)
    handler(world.db, slots=lambda unit, cid: calls.append(cid)).run(job, JobContext())
    handler(world.db, slots=lambda unit, cid: calls.append(cid)).run(
        Job(2, RECONCILE_KIND, {"campaign_id": "not an id"}, 1, T0), JobContext()
    )
    assert calls == []
    assert handler(world.db, slots=reconcile_slots).max_attempts is None


def test_every_enqueue_of_a_reconciliation_is_its_own_job_with_no_dedupe_key(world: World) -> None:
    campaign = _a_campaign(world)
    queue = _job_queue(world)
    with world.db.transaction() as unit:
        first = enqueue_reconciliation(unit, queue, campaign)
        second = enqueue_reconciliation(unit, queue, campaign)
    assert first != second
    assert _jobs_enqueued(world, queue) == [
        (RECONCILE_KIND, {"campaign_id": campaign}, None),
        (RECONCILE_KIND, {"campaign_id": campaign}, None),
    ]


# ── Every refusal is fixed and names nothing ─────────────────────────────────


def test_every_new_refusal_message_is_fixed_and_carries_no_identifier() -> None:
    for refusal in (SeatNotAccepted(), InvalidCursor(), SeatUnavailable()):
        assert str(refusal) == type(refusal).MESSAGE
        assert not any(prefix in str(refusal) for prefix in ("cmp_", "prt_", "sof_", "@"))
    with pytest.raises(TypeError):
        SeatNotAccepted("prt_leak")  # type: ignore[call-arg]
    with pytest.raises(ValueError) as refused:
        check_address("Wren Hidden@example.com")
    assert "Wren" not in str(refused.value)


# ── 1kg.2.3 — the live table session's lifecycle, in both worlds ─────────────
#
# `service/table_sessions.py` composes the stores, the ledger and the outbox.
# What it writes, and in which order, is asserted of the twin and of PostgreSQL
# alike; who waits for whom is the two-connection tests' further down.


def _reconciliation(jobs: Any) -> Callable[[Any, str], int]:
    """How the lifecycle enqueues `campaign.reconcile` — `1kg.2.2`'s helper, the
    one `service/app.py` wires in, and never a second kind."""
    return lambda unit, campaign_id: enqueue_reconciliation(unit, jobs, campaign_id)


def _lifecycle(
    world: World, *, db: Any = None, clock: Callable[[], datetime] | None = None
) -> TableSessions:
    return TableSessions(
        world.db if db is None else db,
        campaigns=world.campaigns,
        sessions=world.sessions,
        audit=world.audit,
        jobs=world.jobs,
        reconcile=_reconciliation(world.jobs),
        clock=clock,
    )


class _Recorded:
    """A database that remembers every unit of work it hands out, so a test can
    ask what each of them locked."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.units: list[Any] = []

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._db.transaction() as unit:
            self.units.append(unit)
            yield unit


def _queued(world: World) -> list[tuple[str, dict, str | None, datetime]]:
    """Every job in the outbox, oldest first: kind, payload, dedupe key and
    when it may run."""
    if world.kind == "fake":
        rows = sorted(world.jobs._rows.values(), key=lambda row: row.job.id)
        return [(r.job.kind, dict(r.job.payload), r.dedupe_key, r.run_after) for r in rows]
    with world.db.transaction() as unit:
        found = unit.conn.execute(
            "SELECT kind, payload, dedupe_key, run_after FROM app.jobs ORDER BY id"
        ).fetchall()
    return [(kind, dict(payload), key, after) for kind, payload, key, after in found]


def _reconciles(world: World) -> list[tuple[dict, str | None]]:
    return [(payload, key) for kind, payload, key, _ in _queued(world) if kind == RECONCILE]


def _ledger_rows(world: World, campaign: str) -> list[Any]:
    with world.db.transaction() as unit:
        return world.audit.for_campaign(unit, campaign)


def _written(world: World, campaign: str) -> tuple[Any, ...]:
    """Everything a no-op must leave as it was: the outbox, the revision and the
    campaign's ledger."""
    return (_queued(world), _revision(world, campaign), [e.id for e in _ledger_rows(world, campaign)])


def test_start_opens_one_session_advances_the_revision_and_schedules_its_expiry(
    world: World,
) -> None:
    """L-4: a locked widening. One session, `authz_revision` advanced once,
    `session.started` recorded with the revision it advanced to, and the
    expiry job enqueued at `expires_at`, last. No reconciliation: nothing was
    narrowed."""
    campaign = _a_campaign(world)
    moment = datetime.now(UTC)
    before = _revision(world, campaign)
    started = _lifecycle(world).start(world.owner, campaign, command_id=_command(), now=moment)
    session = started.session
    assert session.live_at(moment) and started.reconcile_jobs == ()
    assert session.expires_at == moment + SESSION_LIFETIME
    assert _revision(world, campaign) == before + 1
    [row] = _ledger_rows(world, campaign)
    assert (row.action, row.actor_kind, row.actor_ref) == (
        "session.started", "gm", str(world.owner)
    )
    assert row.authz_revision == before + 1 and row.object_ref == session.id
    assert _queued(world) == [
        (EXPIRE_KIND, {"session_id": session.id, "campaign_id": campaign}, None, session.expires_at)
    ]


def test_a_replayed_start_answers_its_session_even_after_it_ended_and_writes_nothing(
    world: World,
) -> None:
    """The contract's Idempotency row: a retried Start opens the session already
    started rather than a second one — whatever has happened to it since."""
    campaign = _a_campaign(world)
    service, command = _lifecycle(world), _command()
    first = service.start(world.owner, campaign, command_id=command).session
    written = _written(world, campaign)
    assert service.start(world.owner, campaign, command_id=command).session.id == first.id
    assert _written(world, campaign) == written

    service.end(world.owner, campaign, first.id)
    written = _written(world, campaign)
    again = service.start(world.owner, campaign, command_id=command)
    assert again.session.id == first.id and again.session.state == "ended"
    assert _written(world, campaign) == written, "a replay after the end opens nothing"


def test_a_new_start_while_live_answers_the_live_session(world: World) -> None:
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    live = service.start(world.owner, campaign, command_id=_command()).session
    written = _written(world, campaign)
    assert service.start(world.owner, campaign, command_id=_command()).session.id == live.id
    assert _written(world, campaign) == written


def test_start_while_live_in_another_campaign_is_refused_and_ends_nothing(world: World) -> None:
    """DV-6: Start never ends another table on its own; the client sends an End
    and then a Start."""
    here, elsewhere = _a_campaign(world), _a_campaign(world, name="Elsewhere")
    service = _lifecycle(world)
    live = service.start(world.owner, here, command_id=_command()).session
    with pytest.raises(LiveSessionExists):
        service.start(world.owner, elsewhere, command_id=_command())
    with world.db.transaction() as unit:
        assert world.sessions.get(unit, live.id).is_live


def test_start_is_refused_for_a_campaign_that_is_not_the_callers(world: World) -> None:
    """One not-found answer for a foreign campaign, a missing one and an
    archived one — decided before any lock."""
    campaign = _a_campaign(world)
    recorded = _Recorded(world.db)
    service = _lifecycle(world, db=recorded)
    for owner, target in ((world.other_owner, campaign), (world.owner, "cmp_" + "z" * 22)):
        with pytest.raises(MissingParent):
            service.start(owner, target, command_id=_command())
    with world.db.transaction() as unit:
        world.campaigns.set_archived(unit, campaign, owner_id=world.owner, archived=True)
    with pytest.raises(MissingParent):
        service.start(world.owner, campaign, command_id=_command())
    assert _queued(world) == []
    assert recorded.units and all(unit.campaign_locks == [] for unit in recorded.units), (
        "the campaign lock is never asked for before ownership is shown"
    )


def test_start_first_finalises_the_gms_expired_session_as_an_expiry(world: World) -> None:
    """RQ-7 and L-5: a GM whose last session timed out unnoticed can start
    again. The stale one is finalised first — `expired`, `ended_at =
    expires_at`, its grants revoked, one reconciliation — in a transaction of
    its own, by the system."""
    stale_home, campaign = _a_campaign(world, name="Stale"), _a_campaign(world)
    stale = _a_session(world, stale_home, hours=11, started_ago_h=12)
    with world.db.transaction() as unit:
        stray = _a_stray_grant_of_the_retired_generation(world, unit, stale.id)

    started = _lifecycle(world).start(world.owner, campaign, command_id=_command())
    assert started.session.is_live and len(started.reconcile_jobs) == 1
    with world.db.transaction() as unit:
        finalised = world.sessions.get(unit, stale.id)
        assert finalised.state == "expired" and finalised.ended_at == stale.expires_at
        assert all(g.revoked_at is not None for g in world.sessions.screens(unit, stale.id))
        assert [g.id for g in world.sessions.screens(unit, stale.id)] == [stray]
    [expiry] = _ledger_rows(world, stale_home)
    assert (expiry.action, expiry.actor_kind, expiry.actor_ref) == ("session.expired", "system", None)
    assert expiry.detail["screens_revoked"] == 1
    assert _reconciles(world) == [({"campaign_id": stale_home}, None)]


def test_end_revokes_everything_and_enqueues_its_reconciliation_last_without_the_campaign_lock(
    world: World,
) -> None:
    """L-5 and RQ-5's first step. Every grant of every generation revoked, both
    epochs advanced, the slots cleared once, `session.ended` recorded — and only
    then, last, one `campaign.reconcile` `{campaign_id}` with no dedupe key. None
    of it asks for the campaign lock, in any unit of work End opens."""
    campaign = _a_campaign(world)
    session = _a_session(world, campaign)
    _a_screen(world, campaign, session.id)
    with world.db.transaction() as unit:
        world.sessions.rotate(unit, campaign, session.id, owner_id=world.owner, command_id=_command())
    _a_screen(world, campaign, session.id)
    with world.db.transaction() as unit:
        _a_stray_grant_of_the_retired_generation(world, unit, session.id)
    world.slot_clears.clear()

    seen_when_enqueued: list[tuple[int, list[str], str, bool]] = []
    enqueue = _reconciliation(world.jobs)

    def reconcile(unit: Any, campaign_id: str) -> int:
        seen_when_enqueued.append(
            (
                len(world.slot_clears),
                [e.action for e in world.audit.for_campaign(unit, campaign_id)],
                world.sessions.get(unit, session.id).state,
                all(g.revoked_at is not None for g in world.sessions.screens(unit, session.id)),
            )
        )
        return enqueue(unit, campaign_id)

    recorded = _Recorded(world.db)
    service = TableSessions(
        recorded,
        campaigns=world.campaigns,
        sessions=world.sessions,
        audit=world.audit,
        jobs=world.jobs,
        reconcile=reconcile,
    )
    ended = service.end(world.owner, campaign, session.id)

    assert ended.session.state == "ended" and len(ended.reconcile_jobs) == 1
    assert (ended.session.reveal_epoch, ended.session.audio_epoch) == (2, 2)
    assert [session_id for _, session_id in world.slot_clears] == [session.id], "once"
    assert seen_when_enqueued == [(1, ["session.ended"], "ended", True)], (
        "the slots, the grants, the state and the audit row all precede the enqueue"
    )
    assert _reconciles(world) == [({"campaign_id": campaign}, None)]
    assert recorded.units and all(unit.campaign_locks == [] for unit in recorded.units)
    [row] = _ledger_rows(world, campaign)
    assert row.detail == {
        "session_id": session.id, "generation": 2, "screens_revoked": 2
    }


def test_ending_an_ended_session_writes_nothing(world: World) -> None:
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    service.end(world.owner, campaign, session.id)
    written = _written(world, campaign)
    again = service.end(world.owner, campaign, session.id)
    assert again.session.state == "ended" and again.reconcile_jobs == ()
    assert _written(world, campaign) == written


def test_rotate_retires_the_generation_and_every_screen_and_leaves_the_session_live(
    world: World,
) -> None:
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    minted = service.mint_screen(world.owner, campaign)
    rotated = service.rotate(world.owner, campaign, session.id, command_id=_command())
    after = rotated.session
    assert after.live_at(datetime.now(UTC)) and after.link_generation == 2
    assert (after.reveal_epoch, after.audio_epoch) == (1, 1)
    assert len(rotated.reconcile_jobs) == 1
    with world.db.transaction() as unit:
        assert [g.revoked_at is not None for g in world.sessions.screens(unit, session.id)] == [True]
    assert service.resolve_screen(minted.secret) is None
    assert _ledger_rows(world, campaign)[-1].action == "session.rotated"


def test_a_repeated_rotate_writes_nothing_and_a_new_command_rotates_again(world: World) -> None:
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    command = _command()
    assert service.rotate(world.owner, campaign, session.id, command_id=command).session.link_generation == 2
    written = _written(world, campaign)
    again = service.rotate(world.owner, campaign, session.id, command_id=command)
    assert again.session.link_generation == 2 and again.reconcile_jobs == ()
    assert _written(world, campaign) == written
    assert service.rotate(world.owner, campaign, session.id, command_id=_command()).session.link_generation == 3


def test_a_rotate_of_an_ended_session_answers_it_as_it_stands(world: World) -> None:
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    service.end(world.owner, campaign, session.id)
    written = _written(world, campaign)
    rotated = service.rotate(world.owner, campaign, session.id, command_id=_command())
    assert rotated.session.state == "ended" and rotated.session.link_generation == 1
    assert _written(world, campaign) == written


def test_end_and_rotate_of_another_gms_session_or_through_another_campaign_are_not_found(
    world: World,
) -> None:
    campaign, elsewhere = _a_campaign(world), _a_campaign(world, name="Elsewhere")
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    written = _written(world, campaign)
    for owner, through in ((world.other_owner, campaign), (world.owner, elsewhere)):
        with pytest.raises(MissingParent):
            service.end(owner, through, session.id)
        with pytest.raises(MissingParent):
            service.rotate(owner, through, session.id, command_id=_command())
    assert _written(world, campaign) == written


def test_the_fan_out_bound_refuses_start_and_rotate_never_end_and_never_a_stranger(
    world: World,
) -> None:
    """L-8 and DV-1. Ten Starts, Ends and Rotates of one campaign in a trailing
    minute refuse the next Start or Rotate; End is counted and never refused; a
    stranger's Rotate is the not-found answer whether or not the campaign is at
    the bound — ownership is decided before anything is counted."""
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    t0 = datetime.now(UTC)
    at = [t0 + timedelta(seconds=n) for n in range(80)]
    session = service.start(world.owner, campaign, command_id=_command(), now=at[0]).session
    for n in range(1, FANOUT_BOUND):
        service.rotate(world.owner, campaign, session.id, command_id=_command(), now=at[n])
    with pytest.raises(FanOutBound) as bound:
        service.rotate(world.owner, campaign, session.id, command_id=_command(), now=at[10])
    assert bound.value.retry_after_s == 60
    with pytest.raises(MissingParent):
        service.rotate(world.other_owner, campaign, session.id, command_id=_command(), now=at[10])
    with pytest.raises(MissingParent):
        service.start(world.other_owner, campaign, command_id=_command(), now=at[10])

    ended = service.end(world.owner, campaign, session.id, now=at[11])
    assert ended.session.state == "ended", "End is never refused by the bound"
    with pytest.raises(FanOutBound):
        service.start(world.owner, campaign, command_id=_command(), now=at[12])
    assert service.start(world.owner, campaign, command_id=_command(), now=at[75]).session.is_live


def test_the_expiry_job_finalises_a_due_session_once_and_an_early_run_waits_for_it(
    world: World,
) -> None:
    """L-6. The job is RQ-5's trigger for expiry's reconciliation, not the
    revocation: it finalises a due session once, does nothing the second time,
    and, run early, enqueues a fresh job at `expires_at` and completes."""
    campaign = _a_campaign(world)
    t0 = datetime.now(UTC)
    session = _lifecycle(world).start(world.owner, campaign, command_id=_command(), now=t0).session
    job = Job(1, EXPIRE_KIND, {"session_id": session.id, "campaign_id": campaign}, 1, t0)

    early = _lifecycle(world, clock=lambda: t0 + timedelta(hours=1)).expire_handler()
    assert early.max_attempts is None
    early.run(job, JobContext())
    with world.db.transaction() as unit:
        assert world.sessions.get(unit, session.id).is_live
    expiries = [row for row in _queued(world) if row[0] == EXPIRE_KIND]
    assert [row[3] for row in expiries] == [session.expires_at, session.expires_at], (
        "the early run enqueued a fresh job at the expiry"
    )

    due = _lifecycle(world, clock=lambda: session.expires_at + timedelta(seconds=1))
    due.expire_handler().run(job, JobContext())
    with world.db.transaction() as unit:
        finalised = world.sessions.get(unit, session.id)
        assert finalised.state == "expired" and finalised.ended_at == session.expires_at
    written = _written(world, campaign)
    due.expire_handler().run(job, JobContext())
    assert _written(world, campaign) == written, "a second run changes nothing"
    assert _reconciles(world) == [({"campaign_id": campaign}, None)]
    assert [e.action for e in _ledger_rows(world, campaign)] == ["session.started", "session.expired"]


def test_every_revocation_enqueues_its_own_reconciliation_with_no_dedupe_key(world: World) -> None:
    """RQ-12: two Ends of two sessions make two rows — a dedupe key would let a
    job that had already read the state absorb the second."""
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    for _ in range(2):
        session = service.start(world.owner, campaign, command_id=_command()).session
        service.end(world.owner, campaign, session.id)
    assert _reconciles(world) == [({"campaign_id": campaign}, None)] * 2


# L-10 and L-12 — what "live" means, for a grant and for a stream's binding.

_WAYS_A_SCREEN_ENDS = ("end", "rotate", "expiry", "revoke", "leave")


@pytest.mark.parametrize("way", _WAYS_A_SCREEN_ENDS)
def test_a_screen_and_its_binding_stop_being_live_each_way_a_screen_ends(
    world: World, way: str
) -> None:
    """The resolver and the predicate agree after each of End, Rotate (the old
    generation), expiry with no End (a row still `live` past `expires_at`), a
    per-screen revoke and Leave. The last two touch one screen: the table stays
    for everyone else."""
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    t0 = datetime.now(UTC)
    session = service.start(world.owner, campaign, command_id=_command(), now=t0).session
    minted = service.mint_screen(world.owner, campaign, now=t0)
    screen = Binding(session.id, session.link_generation, minted.grant.id)
    account = Binding(session.id, session.link_generation)
    read = service.liveness([session.id])
    assert may_write(screen, read, t0) and may_write(account, read, t0)
    assert service.resolve_screen(minted.secret, now=t0) is not None

    later = t0 + timedelta(seconds=1)
    if way == "end":
        service.end(world.owner, campaign, session.id, now=later)
    elif way == "rotate":
        service.rotate(world.owner, campaign, session.id, command_id=_command(), now=later)
    elif way == "expiry":
        later = session.expires_at + timedelta(seconds=1)
    elif way == "revoke":
        service.revoke_screen(world.owner, campaign, minted.grant.id, now=later)
    else:
        assert service.leave(minted.secret, now=later) is True
        assert service.leave(minted.secret, now=later) is False, "a second Leave finds nothing live"

    read = service.liveness([session.id])
    assert service.resolve_screen(minted.secret, now=later) is None
    assert not may_write(screen, read, later)
    assert may_write(account, read, later) is (way in ("revoke", "leave"))


def test_an_end_then_a_start_at_the_same_generation_fails_the_old_binding(world: World) -> None:
    """Every session starts at generation 1, so the generation alone is not a
    binding (SEC-42): the session id is part of it."""
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    first = service.start(world.owner, campaign, command_id=_command()).session
    old = Binding(first.id, first.link_generation)
    service.end(world.owner, campaign, first.id)
    second = service.start(world.owner, campaign, command_id=_command()).session
    assert second.link_generation == first.link_generation == 1
    read = service.liveness([first.id, second.id])
    now = datetime.now(UTC)
    assert not may_write(old, read, now)
    assert may_write(Binding(second.id, 1), read, now)
    assert not may_write(Binding("ses_" + "z" * 22, 1), read, now), "an unknown session"


def test_the_screen_resolver_answers_a_grant_and_never_a_person(world: World) -> None:
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    service.start(world.owner, campaign, command_id=_command())
    minted = service.mint_screen(world.owner, campaign)
    found = service.resolve_screen(minted.secret)
    assert found is not None
    assert set(vars(found)) == {"grant_id", "session_id", "campaign_id", "generation"}
    assert service.resolve_screen(secrets.token_urlsafe(32)) is None


def test_only_the_owner_of_a_live_session_mints_and_the_bound_is_reached_through_the_service(
    world: World,
) -> None:
    """L-9: the owner's unlocked read decides first — another account, a
    campaign with no live session and a session past its expiry are one
    `Inactive`, and none of them takes the session's advisory lock."""
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    with pytest.raises(Inactive):
        service.mint_screen(world.owner, campaign)
    t0 = datetime.now(UTC)
    session = service.start(world.owner, campaign, command_id=_command(), now=t0).session
    recorded = _Recorded(world.db)
    stranger = _lifecycle(world, db=recorded)
    with pytest.raises(Inactive):
        stranger.mint_screen(world.other_owner, campaign, now=t0)
    with pytest.raises(Inactive):
        stranger.mint_screen(world.owner, campaign, now=session.expires_at)
    if world.kind == "fake":
        assert all(unit.locks == [] for unit in recorded.units), "no advisory lock was taken"

    for _ in range(SCREENS_PER_SESSION):
        service.mint_screen(world.owner, campaign, now=t0)
    with pytest.raises(ScreenLimit):
        service.mint_screen(world.owner, campaign, now=t0)
    minted = [e for e in _ledger_rows(world, campaign) if e.action == "screen.minted"]
    assert len(minted) == SCREENS_PER_SESSION
    assert {(e.actor_kind, e.actor_ref, e.object_kind) for e in minted} == {
        ("gm", str(world.owner), "table_screen")
    }


def test_a_screen_revoke_and_a_leave_are_recorded_once_and_touch_nothing_else(world: World) -> None:
    """L-11: neither advances an epoch, clears a slot or enqueues a
    reconciliation; each is idempotent and recorded only when it revoked."""
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    session = service.start(world.owner, campaign, command_id=_command()).session
    by_gm, leaving = service.mint_screen(world.owner, campaign), service.mint_screen(world.owner, campaign)
    queued = _queued(world)
    t1 = datetime.now(UTC) + timedelta(seconds=1)
    service.revoke_screen(world.owner, campaign, by_gm.grant.id, now=t1)
    service.revoke_screen(world.owner, campaign, by_gm.grant.id, now=t1 + timedelta(seconds=1))
    with pytest.raises(MissingParent):
        service.revoke_screen(world.other_owner, campaign, leaving.grant.id)
    t2 = t1 + timedelta(seconds=2)
    assert service.leave(leaving.secret, now=t2) is True
    assert service.leave(leaving.secret, now=t2) is False
    assert service.leave(secrets.token_urlsafe(32), now=t2) is False
    revoked = [e for e in _ledger_rows(world, campaign) if e.action == "screen.revoked"]
    assert [(e.reason_code, e.actor_kind, e.actor_ref, e.object_ref) for e in revoked] == [
        ("gm_revoked", "gm", str(world.owner), by_gm.grant.id),
        ("left", "screen", leaving.grant.id, leaving.grant.id),
    ]
    assert _queued(world) == queued
    with world.db.transaction() as unit:
        after = world.sessions.get(unit, session.id)
        assert (after.reveal_epoch, after.audio_epoch, after.link_generation) == (0, 0, 1)


def _screens_revoked(world: World, campaign: str) -> list[Any]:
    return [e for e in _ledger_rows(world, campaign) if e.action == "screen.revoked"]


def test_a_stray_grant_of_a_retired_generation_is_live_to_no_reader(world: World) -> None:
    """L-9, L-10 (review M-2): the grant a mint loses to a Rotate is unrevoked
    and of the generation the Rotate retired. Validity is the reader's test, so
    every reader refuses it: the resolver, the GM's status read, the per-frame
    read, the screen bound, and Leave, which revokes only a live grant. A screen
    of the current generation is the positive control for each."""
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    t0 = datetime.now(UTC)
    session = service.start(world.owner, campaign, command_id=_command(), now=t0).session
    service.rotate(world.owner, campaign, session.id, command_id=_command(), now=t0)
    with world.db.transaction() as unit:
        stray = _a_stray_grant_of_the_retired_generation(world, unit, session.id)
    later = t0 + timedelta(seconds=1)
    current = service.mint_screen(world.owner, campaign, now=later)
    with world.db.transaction() as unit:
        held = {g.id: g for g in world.sessions.screens(unit, session.id)}
    assert (held[stray].revoked_at, held[stray].link_generation) == (None, 1), "unrevoked and retired"
    assert current.grant.link_generation == 2

    assert service.resolve_screen(STRAY_SECRET, now=later) is None
    assert service.resolve_screen(current.secret, now=later) is not None
    status = service.status(world.owner, campaign, now=later)
    assert status is not None and [g.id for g in status.screens] == [current.grant.id]
    assert service.liveness([session.id])[session.id].live_screens == frozenset({current.grant.id})

    for _ in range(SCREENS_PER_SESSION - 1):
        service.mint_screen(world.owner, campaign, now=later)
    with pytest.raises(ScreenLimit):
        service.mint_screen(world.owner, campaign, now=later)

    assert service.leave(STRAY_SECRET, now=later) is False
    assert _screens_revoked(world, campaign) == [], "a Leave of a dead grant records nothing"
    with world.db.transaction() as unit:
        assert {g.id: g for g in world.sessions.screens(unit, session.id)}[stray].revoked_at is None


def test_a_leave_after_the_session_expired_revokes_and_records_nothing(world: World) -> None:
    """L-17, SEC-49 (review M-3): once its session is past `expires_at` — with
    nothing having ended it — a grant is dead, so its Leave changes nothing and
    the route answers by deleting the cookie alone. Just before the expiry the
    same Leave revokes it, which is the positive control."""
    campaign = _a_campaign(world)
    service = _lifecycle(world)
    t0 = datetime.now(UTC)
    session = service.start(world.owner, campaign, command_id=_command(), now=t0).session
    minted = service.mint_screen(world.owner, campaign, now=t0)

    assert service.leave(minted.secret, now=session.expires_at + timedelta(seconds=1)) is False
    assert _screens_revoked(world, campaign) == []
    with world.db.transaction() as unit:
        assert world.sessions.get(unit, session.id).state == "live", "expiry alone, no End"
        assert {g.id: g for g in world.sessions.screens(unit, session.id)}[minted.grant.id].revoked_at is None

    assert service.leave(minted.secret, now=session.expires_at - timedelta(seconds=1)) is True
    assert [e.object_ref for e in _screens_revoked(world, campaign)] == [minted.grant.id]


# ── 1kg.2.3 on a real server: who waits for whom ─────────────────────────────
#
# Each race below is explicit: one transaction is held open at a known point,
# the other is started, and the server is asked whether it is really blocked —
# or, where the claim is "does not wait", the other finished while the first
# still held what it held.

#: Long enough that a lock taken first is always waited for inside a race.
RACY = CampaignLockSettings(lock_timeout_s=4, transaction_timeout_s=30)


def _pg_lifecycle(db: Any) -> TableSessions:
    jobs = PostgresJobQueue(db)
    return TableSessions(
        db,
        campaigns=PostgresCampaignStore(),
        sessions=PostgresTableSessionStore(slot_clear=no_slots),
        audit=PostgresAuditLog(),
        jobs=jobs,
        reconcile=_reconciliation(jobs),
    )


class _HeldOpen:
    """A database whose transactions, once their work is done, stay open until
    released — the caller's locks held at a known point."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.took, self.release = threading.Event(), threading.Event()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._db.transaction() as unit:
            yield unit
            self.took.set()
            self.release.wait(PATIENCE)


@contextmanager
def _in_a_thread(work: Callable[[], object], ready: threading.Event | None = None) -> Iterator[list[object]]:
    """Run `work` in a thread; the list it yields gets its outcome — an
    exception counting as one. If `ready` is given, wait for it first."""
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(work())
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcome.append(exc)
            if ready is not None:
                ready.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    if ready is not None:
        assert ready.wait(PATIENCE), "the held transaction never got there"
        if outcome and isinstance(outcome[0], BaseException):
            raise outcome[0]
    try:
        yield outcome
    finally:
        thread.join(PATIENCE)
        assert not thread.is_alive(), "a racing transaction never finished"


def _scalar(dsn: str, statement: str, params: tuple = ()) -> Any:
    with connect(dsn) as conn:
        return conn.execute(statement, params).fetchone()[0]


def _a_stranger(dsn: str) -> int:
    return int(
        _scalar(
            dsn,
            "INSERT INTO auth.users (email, password_hash) VALUES ('stranger@example.com', 'x') "
            "RETURNING id",
        )
    )


@needs_db
@pytest.mark.parametrize("holders", ["exclusive", "two-shared"])
def test_end_and_rotate_complete_while_the_campaign_lock_is_held(
    dsn: str, owner: int, holders: str
) -> None:
    """RC-8: a narrowing never waits for the campaign lock. End and Rotate each
    complete while another transaction holds it exclusively, and while two
    shared holders hold it — well inside the time the holders keep it."""
    db = _database(dsn, PATIENT)
    service = _pg_lifecycle(db)
    session = service.start(owner, CAMPAIGN, command_id=_command()).session
    with _a_transaction_holding(db, shared=holders != "exclusive"):
        with (
            _a_transaction_holding(db, shared=True)
            if holders == "two-shared"
            else nullcontext(threading.Event())
        ):
            began = time.monotonic()
            rotated = service.rotate(owner, CAMPAIGN, session.id, command_id=_command())
            ended = service.end(owner, CAMPAIGN, session.id)
            assert time.monotonic() - began < PATIENCE / 3, "they waited for the holders"
    assert rotated.session.link_generation == 2 and ended.session.state == "ended"
    assert len(rotated.reconcile_jobs) == len(ended.reconcile_jobs) == 1


def _authz_revision(dsn: str) -> int:
    return int(
        _scalar(dsn, "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,))
    )


@needs_db
@pytest.mark.parametrize("revocation", ["end", "rotate"])
@pytest.mark.parametrize("holders", ["exclusive", "two-shared"])
def test_a_revocations_reconciliation_waits_for_the_campaign_lock_and_runs_once_it_frees(
    dsn: str, owner: int, holders: str, revocation: str
) -> None:
    """RC-8's other half, run by 1kg.2.2's own `campaign.reconcile` handler. End
    or Rotate commits while the campaign lock is held (exclusively, or by two
    share holders); the one job it left then waits for that lock — the server
    shows it waiting — times out, and is kept for a retry with the revision
    untouched. Once the holders commit, the retry advances the revision exactly
    once and the job is gone. The holders commit as their blocks end."""
    db = _database(dsn, PATIENT)
    service = _pg_lifecycle(db)
    session = service.start(owner, CAMPAIGN, command_id=_command()).session
    # The runner's clock runs ahead of `run_after`, so each attempt below can
    # claim the job without waiting out the retry backoff (1kg.2.2's pattern).
    clock = [datetime.now(UTC) + timedelta(minutes=1)]
    runner = JobRunner(
        PostgresJobQueue(db), {RECONCILE_KIND: handler(db, slots=reconcile_slots)}, clock=lambda: clock[0]
    )
    with _a_transaction_holding(db, shared=holders != "exclusive"):
        with (
            _a_transaction_holding(db, shared=True)
            if holders == "two-shared"
            else nullcontext()
        ):
            if revocation == "end":
                outcome = service.end(owner, CAMPAIGN, session.id)
            else:
                outcome = service.rotate(owner, CAMPAIGN, session.id, command_id=_command())
            [job_id] = outcome.reconcile_jobs
            revision = _authz_revision(dsn)
            with _in_a_thread(lambda: runner.run_job(job_id)) as attempts:
                assert _someone_waits_on_a_lock(dsn), "the reconciliation never waited for the holders"
            [attempt] = attempts
            assert not isinstance(attempt, BaseException), attempt
            assert (attempt.ran, attempt.failed) == (1, 1), "it timed out waiting and is kept for a retry"
            assert _authz_revision(dsn) == revision, "nothing advanced while the holders held the lock"
            assert _scalar(dsn, "SELECT attempts FROM app.jobs WHERE id = %s", (job_id,)) == 1
    clock[0] = clock[0] + timedelta(hours=1)
    retried = runner.run_job(job_id)
    assert (retried.ran, retried.failed) == (1, 0), "the retry ran once the holders had committed"
    assert _authz_revision(dsn) == revision + 1, "the revision advanced exactly once"
    assert _scalar(dsn, "SELECT count(*) FROM app.jobs WHERE id = %s", (job_id,)) == 0
    assert _scalar(dsn, "SELECT count(*) FROM app.jobs WHERE kind = %s", (RECONCILE,)) == 0


@needs_db
def test_the_apps_own_expiry_handler_leaves_one_reconciliation(
    dsn: str, owner: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RQ-5, review M-4: the suite above wires its own `reconcile` lambda, so
    nothing there would notice `service/app.py`'s going missing. This drives
    the composition production runs — `_build_stores` on a real database, and
    the driver it leaves — through one due `table_session.expire`, and asks for
    exactly one `campaign.reconcile` `{campaign_id}` behind it — and, since
    1kg.3.5 wired session dividers into the same composition, exactly one `end`
    divider job after it, carrying the session's id and nothing else."""
    from service import app as appmod
    from service.session_dividers import DIVIDER_KIND

    monkeypatch.setattr(appmod, "_state", {"migrations": "current"})
    db = _database(dsn, PATIENT)
    appmod._build_stores(db)
    started_at = datetime.now(UTC) - SESSION_LIFETIME - timedelta(minutes=1)
    session = _pg_lifecycle(db).start(owner, CAMPAIGN, command_id=_command(), now=started_at).session
    assert _scalar(dsn, "SELECT count(*) FROM app.jobs WHERE kind = %s", (RECONCILE,)) == 0

    ran = appmod._state["jobs"].run_hook()
    assert ran is not None and (ran.ran, ran.failed) == (1, 0), ran
    assert _scalar(dsn, "SELECT state FROM campaign.table_sessions WHERE id = %s", (session.id,)) == "expired"
    with connect(dsn) as conn:
        queued = conn.execute(
            "SELECT kind, payload, dedupe_key FROM app.jobs WHERE kind <> %s ORDER BY id", (EXPIRE_KIND,)
        ).fetchall()
    assert queued == [
        (RECONCILE, {"campaign_id": CAMPAIGN}, None),
        (DIVIDER_KIND, {"session_id": session.id, "boundary": "end"}, None),
    ]


@needs_db
def test_two_racing_starts_in_one_campaign_answer_one_session(dsn: str, owner: int) -> None:
    """The second waits for the campaign lock and then finds the first's live
    session: one session, one `session.started`, the revision advanced once."""
    service = _pg_lifecycle(_database(dsn, RACY))
    outcomes = _race(lambda: service.start(owner, CAMPAIGN, command_id=_command()).session.id)
    assert len(outcomes) == 2 and all(isinstance(o, str) for o in outcomes), outcomes
    assert len(set(outcomes)) == 1
    assert _scalar(dsn, "SELECT count(*) FROM campaign.table_sessions") == 1
    assert _scalar(
        dsn, "SELECT count(*) FROM audit.events WHERE action = 'session.started'"
    ) == 1
    assert _scalar(
        dsn, "SELECT authz_revision FROM campaign.authz_state WHERE campaign_id = %s", (CAMPAIGN,)
    ) == 1


@needs_db
def test_two_racing_starts_in_two_campaigns_of_one_gm_leave_one_live_session(
    dsn: str, owner: int
) -> None:
    """Two campaign locks, no conflict between them: the one-live-per-GM index
    decides, and the loser gets `LiveSessionExists`."""
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Other')",
            (OTHER_CAMPAIGN, owner),
        )
    service = _pg_lifecycle(_database(dsn, RACY))
    targets = iter([CAMPAIGN, OTHER_CAMPAIGN])
    guard = threading.Lock()

    def start() -> object:
        with guard:
            target = next(targets)
        return service.start(owner, target, command_id=_command()).session.id

    outcomes = _race(start)
    assert sorted(type(o).__name__ for o in outcomes) == ["LiveSessionExists", "str"], outcomes
    assert _scalar(
        dsn, "SELECT count(*) FROM campaign.table_sessions WHERE state = 'live'"
    ) == 1


@needs_db
def test_two_racing_ends_make_one_ending(dsn: str, owner: int) -> None:
    """They serialise on the session row: one state change, one audit row, one
    reconciliation; the other answers the ended session."""
    service = _pg_lifecycle(_database(dsn, RACY))
    session = service.start(owner, CAMPAIGN, command_id=_command()).session
    outcomes = _race(lambda: service.end(owner, CAMPAIGN, session.id))
    assert all(o.session.state == "ended" for o in outcomes), outcomes
    assert sorted(len(o.reconcile_jobs) for o in outcomes) == [0, 1]
    assert _scalar(dsn, "SELECT count(*) FROM audit.events WHERE action = 'session.ended'") == 1
    assert _scalar(dsn, "SELECT count(*) FROM app.jobs WHERE kind = %s", (RECONCILE,)) == 1


@needs_db
def test_two_racing_rotates_with_one_command_make_one_rotation(dsn: str, owner: int) -> None:
    service = _pg_lifecycle(_database(dsn, RACY))
    session = service.start(owner, CAMPAIGN, command_id=_command()).session
    command = _command()
    outcomes = _race(lambda: service.rotate(owner, CAMPAIGN, session.id, command_id=command))
    assert [o.session.link_generation for o in outcomes] == [2, 2], outcomes
    assert _scalar(dsn, "SELECT count(*) FROM audit.events WHERE action = 'session.rotated'") == 1
    assert _scalar(
        dsn, "SELECT link_generation FROM campaign.table_sessions WHERE id = %s", (session.id,)
    ) == 2


@needs_db
def test_two_mints_racing_for_the_last_screen_leave_one_winner_and_the_other_waited(
    dsn: str, owner: int
) -> None:
    """SEC-48's bound under the session's advisory lock: the first mint for
    the last place holds the lock until it commits, the second really waits for
    it (the server says so), then counts four and is refused."""
    db = _database(dsn, RACY)
    service = _pg_lifecycle(db)
    service.start(owner, CAMPAIGN, command_id=_command())
    for _ in range(SCREENS_PER_SESSION - 1):
        service.mint_screen(owner, CAMPAIGN)
    held = _HeldOpen(db)
    with _in_a_thread(lambda: _pg_lifecycle(held).mint_screen(owner, CAMPAIGN), held.took) as first:
        with _in_a_thread(lambda: service.mint_screen(owner, CAMPAIGN)) as second:
            try:
                assert _someone_waits_on_a_lock(dsn), "the second mint never waited"
            finally:
                held.release.set()
    assert isinstance(first[0], MintedScreen), first
    assert isinstance(second[0], ScreenLimit), second
    assert _scalar(dsn, "SELECT count(*) FROM campaign.table_credentials") == SCREENS_PER_SESSION


@needs_db
def test_a_mint_racing_a_rotate_never_waits_and_its_grant_is_dead_on_arrival(
    dsn: str, owner: int
) -> None:
    """L-9: the mint's insert takes `FOR KEY SHARE` on the session row, which a
    Rotate's `FOR NO KEY UPDATE` does not block — so the mint completes while
    the Rotate still holds the row. It read the old generation, so the grant it
    commits after the Rotate's revoke resolves as not live: fail-closed."""
    db = _database(dsn, RACY)
    service = _pg_lifecycle(db)
    session = service.start(owner, CAMPAIGN, command_id=_command()).session
    service.mint_screen(owner, CAMPAIGN)
    held = _HeldOpen(db)
    rotate = _pg_lifecycle(held)
    with _in_a_thread(
        lambda: rotate.rotate(owner, CAMPAIGN, session.id, command_id=_command()), held.took
    ) as rotated:
        began = time.monotonic()
        minted = service.mint_screen(owner, CAMPAIGN)
        assert time.monotonic() - began < PATIENCE / 3, "the mint waited for the Rotate"
        assert not rotated, "the Rotate was still holding the row"
        held.release.set()
    assert rotated[0].session.link_generation == 2
    assert minted.grant.link_generation == 1
    assert service.resolve_screen(minted.secret) is None, "a grant of a retired generation"


@needs_db
def test_a_multi_row_revoke_waits_for_single_row_revokes_and_never_deadlocks(
    dsn: str, owner: int
) -> None:
    """L-5: End holds the session row and locks the grants in ascending id; a
    per-screen revoke and a Leave each hold one grant and take nothing after it.
    With the highest grant held by the revoke and another by the Leave, End
    waits (the server says so), both commit, and End completes with no
    deadlock: every grant revoked, one audit row per decision."""
    db = _database(dsn, RACY)
    service = _pg_lifecycle(db)
    session = service.start(owner, CAMPAIGN, command_id=_command()).session
    minted = sorted(
        (service.mint_screen(owner, CAMPAIGN) for _ in range(3)), key=lambda m: m.grant.id
    )
    lowest, highest = minted[0], minted[-1]
    by_gm, leaving = _HeldOpen(db), _HeldOpen(db)
    with _in_a_thread(
        lambda: _pg_lifecycle(by_gm).revoke_screen(owner, CAMPAIGN, highest.grant.id), by_gm.took
    ) as revoked:
        with _in_a_thread(lambda: _pg_lifecycle(leaving).leave(lowest.secret), leaving.took) as left:
            with _in_a_thread(lambda: service.end(owner, CAMPAIGN, session.id)) as ended:
                try:
                    assert _someone_waits_on_a_lock(dsn), "End never waited for a held grant"
                finally:
                    leaving.release.set()
                    by_gm.release.set()
    assert revoked == [None] and left == [True], (revoked, left)
    assert not isinstance(ended[0], BaseException), ended
    assert ended[0].session.state == "ended"
    assert _scalar(
        dsn,
        "SELECT count(*) FROM campaign.table_credentials WHERE session_id = %s "
        "AND revoked_at IS NULL",
        (session.id,),
    ) == 0
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT action, reason_code FROM audit.events "
            "WHERE action IN ('session.ended', 'screen.revoked') ORDER BY action, reason_code"
        ).fetchall()
    assert rows == [("screen.revoked", "gm_revoked"), ("screen.revoked", "left"), ("session.ended", None)]


@contextmanager
def _a_store_transaction_holding(db: Any, work: Callable[[Any], object]) -> Iterator[object]:
    """`work` inside a transaction in another thread, kept open — and then
    rolled back — so a test can ask what it locked."""
    took, release = threading.Event(), threading.Event()
    result: list[object] = []

    class _Done(Exception):
        pass

    def hold() -> None:
        try:
            with db.transaction() as unit:
                result.append(work(unit))
                took.set()
                release.wait(PATIENCE)
                raise _Done
        except _Done:
            pass
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            result.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE), "the transaction never got there"
    if result and isinstance(result[0], BaseException):
        raise result[0]
    try:
        yield result[0]
    finally:
        release.set()
        thread.join(PATIENCE)


def _lock_the_session_row(dsn: str, session_id: str) -> None:
    with connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL lock_timeout = '2s'")
        conn.execute(
            "SELECT id FROM campaign.table_sessions WHERE id = %s FOR NO KEY UPDATE", (session_id,)
        )
        conn.rollback()


@needs_db
def test_a_strangers_end_on_another_gms_session_locks_nothing(dsn: str, owner: int) -> None:
    """L-5: the owner is in the locking statement, so a stranger naming another
    GM's session holds nothing a second connection has to wait for. The owner's
    own End is the positive control: it does hold the row."""
    stranger = _a_stranger(dsn)
    db = _database(dsn, RACY)
    session = _pg_lifecycle(db).start(owner, CAMPAIGN, command_id=_command()).session
    store = PostgresTableSessionStore(slot_clear=no_slots)

    def theirs(unit: Any) -> object:
        return store.end(unit, CAMPAIGN, session.id, owner_id=stranger)

    with _a_store_transaction_holding(db, theirs) as found:
        assert found is None
        _lock_the_session_row(dsn, session.id)

    def mine(unit: Any) -> object:
        return store.end(unit, CAMPAIGN, session.id, owner_id=owner)

    with _a_store_transaction_holding(db, mine) as found:
        assert found is not None
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _lock_the_session_row(dsn, session.id)


@needs_db
def test_a_strangers_start_never_asks_for_another_gms_campaign_lock(dsn: str, owner: int) -> None:
    """L-4: ownership is read unlocked before the campaign lock is asked for,
    so a stranger's Start is the not-found answer at once while another
    transaction holds that campaign exclusively. The owner's Start is the
    positive control: it waits (the server says so), times out, and is a
    retryable `BackendUnavailable` that created nothing."""
    stranger = _a_stranger(dsn)
    db = _database(dsn, QUICK)
    service = _pg_lifecycle(db)
    with _a_transaction_holding(db, shared=False):
        began = time.monotonic()
        with pytest.raises(MissingParent):
            service.start(stranger, CAMPAIGN, command_id=_command())
        assert time.monotonic() - began < QUICK.lock_timeout_s, "the stranger waited"
        with _in_a_thread(lambda: service.start(owner, CAMPAIGN, command_id=_command())) as owners:
            assert _someone_waits_on_a_lock(dsn), "the owner's Start never waited"
    assert isinstance(owners[0], BackendUnavailable), owners
    assert _scalar(dsn, "SELECT count(*) FROM campaign.table_sessions") == 0
    assert _scalar(dsn, "SELECT count(*) FROM app.jobs") == 0

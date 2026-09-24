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
machine. Neither file claims the other's half.

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_campaign_db.py -q
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

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
    LiveSessionExists,
    MissingParent,
    NotLive,
    PostgresCampaignStore,
    SeatUnavailable,
    check_name,
)
from service.db import (
    CampaignAuthzMissing,
    CampaignLockOrder,
    CampaignLockSettings,
    Database,
    InMemoryDatabase,
    PgTransaction,
    PoolSettings,
)
from service.participant_store import (
    InMemoryParticipantStore,
    Participant,
    PostgresParticipantStore,
    alias_key,
    check_alias,
)
from service.table_session_store import (
    InMemoryTableSessionStore,
    PostgresTableSessionStore,
    TableCredential,
    TableSession,
    no_slots,
)

CAMPAIGN = "cmp_" + "a" * 22
OTHER_CAMPAIGN = "cmp_" + "b" * 22
PARTICIPANT = "prt_" + "a" * 22
SESSION = "ses_" + "a" * 22

#: A second is long enough that a lock taken first is always taken first, and
#: short enough that a test which should time out does not hold the job up.
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
#: For the interleaving tests: a transaction there waits for a *thread*, not for
#: a database, so RQ-8's five seconds would be racing the test harness.
PATIENT = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
#: How long a test waits for another thread before calling it a hang.
PATIENCE = 15


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
        session, _ = sessions.start(
            unit, CAMPAIGN, owner_id=owner, expires_at=datetime.now(UTC) + timedelta(hours=12)
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
            "(id, campaign_id, gm_user_id, state, expires_at, link_digest) "
            "VALUES (%s, %s, %s, 'live', now() + interval '12 hours', %s)",
            (SESSION, CAMPAIGN, owner, "a" * 64),
        )


def _insert_a_credential_referencing_the_session(dsn: str) -> None:
    with connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL lock_timeout = '2s'")
        conn.execute(
            "INSERT INTO campaign.table_credentials "
            "(id, session_id, link_generation, credential_digest) VALUES (%s, %s, 1, %s)",
            ("tcr_" + "a" * 22, SESSION, "2" * 64),
        )
        conn.commit()


@needs_db
def test_rotating_the_link_does_not_block_a_join_that_references_the_session(
    dsn: str, owner: int
) -> None:
    """Why `table_sessions_link_digest_uidx` must stay PARTIAL.

    PostgreSQL treats the columns of a non-partial, non-expression unique index
    as key columns, so a full unique index on `link_digest` would turn Rotate's
    `UPDATE ... SET link_digest` into a `FOR UPDATE` on the session row — which
    conflicts with the `FOR KEY SHARE` every join's foreign-key check takes, and
    a join would start waiting behind a Rotate. That is exactly what RQ-3
    forbids, so it is asserted here rather than reasoned about: the UPDATE lets
    the referencing insert through, and a real `FOR UPDATE` does not.
    """
    _a_session_row(dsn, owner)
    rotate = "UPDATE campaign.table_sessions SET link_digest = %s WHERE id = %s"
    with _holding_a_row(dsn, rotate, ("b" * 64, SESSION)):
        _insert_a_credential_referencing_the_session(dsn)

    with connect(dsn) as conn:
        conn.execute("DELETE FROM campaign.table_credentials")

    stronger = "SELECT id FROM campaign.table_sessions WHERE id = %s FOR UPDATE"
    with _holding_a_row(dsn, stronger, (SESSION,)):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _insert_a_credential_referencing_the_session(dsn)


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
    yield World(
        "postgres",
        _database(target),
        PostgresCampaignStore(),
        PostgresParticipantStore(),
        PostgresTableSessionStore(slot_clear=record),
        PostgresAuditLog(),
        owner=int(gms[0]),
        other_owner=int(gms[1]),
        players=(int(gms[2]), int(gms[3]), int(gms[4])),
        slot_clears=clears,
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
    so an overdue session is one that STARTED long enough ago."""
    started = datetime.now(UTC) - timedelta(hours=started_ago_h)
    with world.db.transaction() as unit:
        return world.sessions.start(
            unit,
            campaign_id,
            owner_id=world.owner if owner is None else owner,
            expires_at=started + timedelta(hours=hours),
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


#: The three methods that change a participant's own row, each called the way a
#: caller who has composed nothing else would call it. Every one of them takes
#: the seat's row lock itself (G-6), naming the campaign there, so the
#: participant-first order RQ-3 asks for is a property of the store and not of
#: every caller. `accept` needs a seat offered to `player` first; the tests that
#: use this dict arrange that before they call it.
PARTICIPANT_MUTATORS: dict[str, Callable[[Any, Any, str, str, int], object]] = {
    "remove": lambda store, unit, campaign, seat, player: store.remove(unit, campaign, seat),
    "offer": lambda store, unit, campaign, seat, player: store.offer(
        unit, campaign, seat, user_id=player
    ),
    "accept": lambda store, unit, campaign, seat, player: store.accept(
        unit, campaign, seat, user_id=player
    ),
}


@pytest.mark.parametrize("mutator", sorted(PARTICIPANT_MUTATORS))
def test_every_participant_mutator_takes_the_seats_row_and_bounds_its_transaction(
    world: World, mutator: str
) -> None:
    """G-6, and the residue of F-15: every mutator holds the seat's row itself,
    so the order RQ-3 rests on is one no caller can forget, and a bare call is
    bounded like every other holder (RQ-8). A second `FOR NO KEY UPDATE` on a row
    this transaction already holds is free, so a caller's own hold can stay.

    Both halves are read off the unit of work, which keeps them assertable in
    both worlds: the bound the mutator asked for, and the refusal that proves it
    declared a row lock (`lock_campaign` is the first lock a transaction takes,
    RQ-2, so a declared row lock makes a later one illegal). The call must also
    have succeeded, so the bound cannot come from a refusal's path alone.
    """
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    player = world.players[0]
    if mutator == "accept":
        _offer(world, campaign, seat, player)
    with world.db.transaction() as unit:
        outcome = PARTICIPANT_MUTATORS[mutator](world.participants, unit, campaign, seat, player)
        assert outcome is True or isinstance(outcome, Participant), outcome
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
                owner_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
    # Another GM is unaffected — in their own campaign, which is the only place
    # they can start one at all.
    theirs = _a_campaign(world, owner=world.other_owner, name="Theirs")
    assert _a_session(world, theirs, owner=world.other_owner)[0].gm_user_id == world.other_owner


def test_a_session_cannot_be_started_in_a_campaign_that_is_not_the_gms(world: World) -> None:
    """F-4, AUD-1. The GM is the owner and `start` reads that out of the
    campaigns table in the same statement, so a foreign campaign, an archived
    one and one that never existed are one answer — and the free `gm_user_id`
    parameter, which the database never verified, is gone."""
    here = _a_campaign(world)
    later = datetime.now(UTC) + timedelta(hours=12)
    with world.db.transaction() as unit:
        with pytest.raises(MissingParent):
            world.sessions.start(unit, here, owner_id=world.other_owner, expires_at=later)
        with pytest.raises(MissingParent):
            world.sessions.start(unit, "cmp_" + "z" * 22, owner_id=world.owner, expires_at=later)

    with world.db.transaction() as unit:
        world.campaigns.set_archived(unit, here, owner_id=world.owner, archived=True)
    with world.db.transaction() as unit:
        with pytest.raises(MissingParent):
            world.sessions.start(unit, here, owner_id=world.owner, expires_at=later)


def test_a_session_that_expires_before_it_starts_is_refused(world: World) -> None:
    """The bound 0004 carries, so the caller gets the store's refusal rather
    than an integrity error naming a constraint."""
    campaign = _a_campaign(world)
    with world.db.transaction() as unit:
        with pytest.raises(ValueError, match="expires after it starts"):
            world.sessions.start(
                unit,
                campaign,
                owner_id=world.owner,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )


def test_a_gm_may_start_again_once_their_session_has_ended(world: World) -> None:
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id)
    assert _a_session(world, campaign)[0].id != session.id


# Behaviour 15 — the narrow primitive.


def test_narrow_advances_the_reveal_epoch_and_calls_its_extension_point_once(world: World) -> None:
    """RQ-7. The audio epoch moves only when the caller asks, because muting the
    table and narrowing what it can see are different decisions."""
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
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


# Behaviours 16 and 17 — End and Rotate, each closing a generation.


def test_ending_a_session_revokes_its_generation_and_advances_both_epochs(world: World) -> None:
    """SEC-9: 'Either one revokes every table credential of the old generation in
    the same transaction that advances the reveal and audio epochs.' End is the
    'either' that is easy to forget, because the session is going away anyway —
    but a credential that outlives the epoch retiring it is a live door."""
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        joined, _ = world.sessions.issue_credential(unit, campaign, session.id)

    with world.db.transaction() as unit:
        ended = world.sessions.end(unit, campaign, session.id)
        assert ended is not None
        assert ended.state == "ended" and ended.ended_at is not None
        assert (ended.reveal_epoch, ended.audio_epoch) == (1, 1)
        assert ended.link_digest is None, "the retired link opens nothing"
        held = {c.id: c for c in world.sessions.credentials(unit, session.id)}
        assert held[joined.id].revoked_at is not None

    with world.db.transaction() as unit:
        again = world.sessions.end(unit, campaign, session.id)
        assert again is not None
        assert (again.reveal_epoch, again.audio_epoch) == (1, 1), "ending twice changes nothing"
        assert again.ended_at == ended.ended_at


def test_an_ending_can_be_recorded_as_an_expiry_rather_than_the_gms_decision(world: World) -> None:
    """F-7. 0004's CHECK has `expired`, the audit vocabulary has
    `session.expired`, and ED-17 distinguishes `session_ended(gm_end |
    expired)` — but nothing could write it, so `expired_live_sessions_for_gm`
    found the rows and ending them mislabelled every one."""
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign, hours=11, started_ago_h=12)
    with world.db.transaction() as unit:
        [overdue] = world.sessions.expired_live_sessions_for_gm(unit, world.owner)
        ended = world.sessions.end(unit, campaign, overdue.id, expired=True)
        assert ended is not None
        assert ended.state == "expired" and ended.ended_at is not None
        assert ended.link_digest is None
    with world.db.transaction() as unit:
        assert world.sessions.get(unit, session.id).state == "expired"


def _a_stray_credential_of_the_retired_generation(world: World, unit: Any, session_id: str) -> str:
    """The row a join racing a Rotate leaves behind: unrevoked, and belonging to
    the generation the Rotate has just retired.

    It has to be written by hand, in each world's own way, because no store
    method will make one — `issue_credential` reads the session's *current*
    generation, so a credential issued after the Rotate belongs to the new one
    and an ending that revoked only the current generation would revoke it
    anyway. That is precisely why the earlier version of this test could not
    fail (G-5).
    """
    stray = "tcr_" + "s" * 22
    digest = sha256(b"a credential of the retired generation").hexdigest()
    if world.kind == "fake":
        world.db.tables["table_credentials"].add(
            unit, stray, TableCredential(stray, session_id, 1, digest, datetime.now(UTC))
        )
    else:
        unit.conn.execute(
            "INSERT INTO campaign.table_credentials "
            "(id, session_id, link_generation, credential_digest) VALUES (%s, %s, 1, %s)",
            (stray, session_id, digest),
        )
    return stray


def test_ending_a_session_revokes_every_generation_it_ever_had(world: World) -> None:
    """A join that commits just after a Rotate holds an unrevoked credential of
    the generation the Rotate retired — `issue_credential` reads the generation
    without a lock, deliberately, so that a join never makes a Stop wait. The
    table is over, so the ending's own statement leaves nothing of the session
    unrevoked behind it, whatever generation it belongs to. It cannot promise
    more: a join that took its snapshot before the End committed is not blocked
    by it and may commit one afterwards, which is why a reader checks the state,
    the generation and the revocation together (ED-25, SEC-9)."""
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        first, _ = world.sessions.issue_credential(unit, campaign, session.id)
        world.sessions.rotate_link(unit, campaign, session.id)
        second, _ = world.sessions.issue_credential(unit, campaign, session.id)
        assert (first.link_generation, second.link_generation) == (1, 2)

    with world.db.transaction() as unit:
        stray = _a_stray_credential_of_the_retired_generation(world, unit, session.id)

    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id)
        held = {c.id: c for c in world.sessions.credentials(unit, session.id)}
        assert held[stray].link_generation == 1 and held[second.id].link_generation == 2, (
            "one retired generation and one current, or this test proves nothing"
        )
        assert [c.id for c in held.values() if c.is_active] == []
        assert held[stray].revoked_at is not None and held[second.id].revoked_at is not None


def test_rotating_the_link_retires_the_old_generation_and_leaves_the_session_live(
    world: World,
) -> None:
    """SEC-9 again, and REVEAL-17: rotating is not restarting, so the session
    keeps its id, its start and its expiry."""
    campaign = _a_campaign(world)
    session, first_link = _a_session(world, campaign)
    with world.db.transaction() as unit:
        old_credential, _ = world.sessions.issue_credential(unit, campaign, session.id)

    with world.db.transaction() as unit:
        rotated = world.sessions.rotate_link(unit, campaign, session.id)
        assert rotated is not None
        session_after, new_link = rotated
        assert session_after.id == session.id and session_after.is_live
        assert session_after.started_at == session.started_at
        assert session_after.expires_at == session.expires_at
        assert session_after.link_generation == 2
        assert new_link != first_link
        assert session_after.link_digest == sha256(new_link.encode("utf-8")).hexdigest()
        assert (session_after.reveal_epoch, session_after.audio_epoch) == (1, 1)

        # A device joining now belongs to the new generation and survives, while
        # every credential of the superseded one is revoked. Keyed by id, never
        # by position: two rows written in one call can share a timestamp.
        fresh, _ = world.sessions.issue_credential(unit, campaign, session.id)
        assert fresh.link_generation == 2
        held = {c.id: c for c in world.sessions.credentials(unit, session.id)}
        assert held[old_credential.id].revoked_at is not None
        assert held[fresh.id].revoked_at is None


def test_a_session_that_is_no_longer_live_cannot_have_its_link_rotated(world: World) -> None:
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id)
    with world.db.transaction() as unit:
        with pytest.raises(NotLive):
            world.sessions.rotate_link(unit, campaign, session.id)


def test_a_session_that_is_no_longer_live_admits_no_device(world: World) -> None:
    """F-7. Without `AND state = 'live'` in the statement, a credential issued
    for an ended session came back active — and a reader that checked only
    `revoked_at` would have honoured it."""
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id)
    with world.db.transaction() as unit:
        with pytest.raises(NotLive):
            world.sessions.issue_credential(unit, campaign, session.id)
        assert world.sessions.credentials(unit, session.id) == []


# Behaviour 14b — the digest lookups every unauthenticated join needs.


def test_a_table_link_and_a_join_credential_are_found_by_their_digest(world: World) -> None:
    """The alignment's "digest lookups are exact matches on a unique index".
    `link_digest` had neither a unique index nor a reader; `table_credentials`
    had the index and no reader."""
    campaign = _a_campaign(world)
    session, link = _a_session(world, campaign)
    link_digest = sha256(link.encode("utf-8")).hexdigest()
    with world.db.transaction() as unit:
        found = world.sessions.find_by_link_digest(unit, link_digest)
        assert found is not None and found.id == session.id
        assert world.sessions.find_by_link_digest(unit, "f" * 64) is None

        joined, secret = world.sessions.issue_credential(unit, campaign, session.id)
        credential = world.sessions.find_credential(
            unit, sha256(secret.encode("utf-8")).hexdigest()
        )
        assert credential is not None and credential.id == joined.id

    with world.db.transaction() as unit:
        rotated = world.sessions.rotate_link(unit, campaign, session.id)
        assert rotated is not None
        assert world.sessions.find_by_link_digest(unit, link_digest) is None, "a retired link"
        assert world.sessions.find_by_link_digest(
            unit, sha256(rotated[1].encode("utf-8")).hexdigest()
        ) is not None
        revoked = world.sessions.find_credential(
            unit, sha256(secret.encode("utf-8")).hexdigest()
        )
        assert revoked is not None and not revoked.is_active, (
            "a revoked credential is refused for being revoked, not mistaken for an unknown device"
        )


# Behaviour 18 — the stale sessions a start has to clear out of its own way.


def test_expired_live_sessions_for_gm_returns_that_gms_overdue_live_ones_only(
    world: World,
) -> None:
    """RQ-7: a GM whose last session timed out unnoticed must be able to start
    again, so the start ends the stale one first rather than meeting the index."""
    campaign = _a_campaign(world)
    theirs = _a_campaign(world, owner=world.other_owner, name="Theirs")
    stale, _ = _a_session(world, campaign, hours=11, started_ago_h=12)
    other, _ = _a_session(world, theirs, hours=11, started_ago_h=12, owner=world.other_owner)

    with world.db.transaction() as unit:
        assert [s.id for s in world.sessions.expired_live_sessions_for_gm(unit, world.owner)] == [
            stale.id
        ], "another GM's stale session is not this GM's to end"
        assert [s.id for s in world.sessions.expired_live_sessions_for_gm(
            unit, world.other_owner
        )] == [other.id]

        world.sessions.end(unit, campaign, stale.id, expired=True)
        assert world.sessions.expired_live_sessions_for_gm(unit, world.owner) == [], (
            "an ended session is not still overdue"
        )

    fresh, _ = _a_session(world, campaign, hours=12)
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
            session, _ = world.sessions.start(
                unit,
                campaign,
                owner_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
            world.sessions.issue_credential(unit, campaign, session.id)
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
        assert world.sessions.credentials(unit, session.id) == []
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
    session, _ = _a_session(world, campaign)

    with world.db.transaction() as writer:
        assert world.participants.remove(writer, campaign, seat)
        assert world.sessions.end(writer, campaign, session.id) is not None
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
    session, _ = _a_session(world, campaign)
    seat = _a_participant(world, campaign, "Rook")

    with pytest.raises(RuntimeError, match="boom"):
        with world.db.transaction() as unit:
            world.sessions.end(unit, campaign, session.id)
            world.sessions.start(
                unit,
                campaign,
                owner_id=world.owner,
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
                owner_id=world.owner,
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
                owner_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
        with pytest.raises(MissingParent):
            world.sessions.issue_credential(unit, nowhere, "ses_" + "z" * 22)


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
            world.sessions.start(unit, campaign, owner_id=world.owner, expires_at=expires, **when)


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
            detail={"participant_id": seat, "codes_revoked": 1, "devices_revoked": 0},
        )

    with world.db.transaction() as unit:
        [kept] = world.audit.for_campaign(unit, campaign)
        assert kept.action == "participant.removed" and kept.decision == "allowed"
        assert kept.campaign_id_tombstone == campaign and kept.object_ref == seat
        assert kept.actor_ref == str(world.owner) and kept.authz_revision == 3
        assert kept.reason_code == "gm_removed"
        assert kept.detail == {"participant_id": seat, "codes_revoked": 1, "devices_revoked": 0}
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
        TableSession("ses_x", "cmp_x", 1, "live", moment, moment, 1, digest),
        TableCredential("tcr_x", "ses_x", 1, digest, moment),
    ]
    for record in records:
        printed = repr(record)
        assert digest not in printed, f"{type(record).__name__} shows a digest"
        assert "Rook" not in printed, f"{type(record).__name__} shows an alias"
        assert "Nocturne" not in printed, f"{type(record).__name__} shows a campaign name"
        assert "987654321" not in printed, f"{type(record).__name__} shows an account"
        assert type(record).__name__ in printed, "a record still says what it is"


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
            return sessions.start(unit, CAMPAIGN, owner_id=owner, expires_at=expires)[0].id

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
    db: Database, participants: PostgresParticipantStore, *, offered_to: int | None = None
) -> str:
    with db.transaction() as unit:
        seat = participants.add(unit, CAMPAIGN, alias="Rook")
        if offered_to is not None:
            participants.offer(unit, CAMPAIGN, seat.id, user_id=offered_to)
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
    seat = _a_seat(db, participants, offered_to=player if mutator == "accept" else None)

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
@pytest.mark.skip(reason="DELIBERATELY BROKEN COMMIT: offer raises inside its handler")
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

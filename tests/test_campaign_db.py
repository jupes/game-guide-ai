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

import threading
import time
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
    ParticipantRemoved,
    PostgresCampaignStore,
    check_name,
)
from service.db import (
    CampaignAuthzMissing,
    CampaignLockSettings,
    Database,
    InMemoryDatabase,
    PgTransaction,
    PoolSettings,
)
from service.participant_store import (
    DeviceCredential,
    EnrolmentCode,
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
def test_a_caller_may_raise_the_transaction_bound_for_its_own_longer_work(dsn: str, owner: int) -> None:
    with _database(dsn).transaction() as unit:
        unit.lock_campaign(CAMPAIGN, shared=False, transaction_timeout_s=42)
        assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "42s"


@needs_db
def test_a_participant_only_transaction_is_bounded_like_every_other_holder(dsn: str, owner: int) -> None:
    """RQ-6 rests on "every holder is bounded by `transaction_timeout`", and an
    enrolment — the unauthenticated route — holds a participant row without ever
    taking the campaign lock. Before this, that was the one transaction in the
    schema with no bound at all."""
    db = _database(dsn, CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=9))
    participants = PostgresParticipantStore()
    with db.transaction() as unit:
        seat = participants.add(unit, CAMPAIGN, alias="Rook")
    with db.transaction() as unit:
        assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "0", "not yet"
        participants.hold(unit, seat.id)
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


def _insert_a_code_referencing_the_participant(dsn: str) -> None:
    """The foreign-key check this makes takes FOR KEY SHARE on the participant."""
    with connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL lock_timeout = '2s'")
        conn.execute(
            "INSERT INTO campaign.enrolment_codes (id, participant_id, code_digest, expires_at) "
            "VALUES (%s, %s, %s, now() + interval '7 days')",
            ("enc_" + "a" * 22, PARTICIPANT, "0" * 64),
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
        _insert_a_code_referencing_the_participant(dsn)

    with connect(dsn) as conn:
        conn.execute("DELETE FROM campaign.enrolment_codes")

    stronger = "SELECT id FROM campaign.participants WHERE id = %s FOR UPDATE"
    with _holding_a_row(dsn, stronger, (PARTICIPANT,)):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _insert_a_code_referencing_the_participant(dsn)


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
            for email in ("gm@example.com", "other@example.com")
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


def _a_code(world: World, campaign_id: str, seat: str) -> str:
    with world.db.transaction() as unit:
        _, secret = world.participants.issue_code(unit, campaign_id, seat)
    return sha256(secret.encode("utf-8")).hexdigest()


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
    otherwise let GM A remove, reset or re-enrol GM B's participant by id."""
    mine, theirs = _a_campaign(world), _a_campaign(world, owner=world.other_owner, name="Theirs")
    seat = _a_participant(world, theirs, "Rook")
    with world.db.transaction() as unit:
        assert not world.participants.remove(unit, mine, seat)
        assert world.participants.revoke_codes(unit, mine, seat) == 0
        assert world.participants.revoke_device_credentials(unit, mine, seat) == 0
        with pytest.raises(MissingParent):
            world.participants.issue_code(unit, mine, seat)
        with pytest.raises(MissingParent):
            world.participants.issue_device_credential(unit, mine, seat)
        assert not world.participants.consume_code(unit, mine, seat, "0" * 64)
    with world.db.transaction() as unit:
        still = world.participants.get(unit, seat)
        assert still is not None and still.is_active


# Behaviours 12 and 13 — codes and devices.


def test_a_code_is_single_use_and_only_its_own_participant_can_spend_it(world: World) -> None:
    campaign = _a_campaign(world)
    seat, other = _a_participant(world, campaign, "Rook"), _a_participant(world, campaign, "Wren")
    with world.db.transaction() as unit:
        code, secret = world.participants.issue_code(unit, campaign, seat)
        digest = sha256(secret.encode("utf-8")).hexdigest()
        assert code.code_digest == digest, "the store keeps the digest, never the code"

        assert world.participants.find_code(unit, digest) is not None
        assert not world.participants.consume_code(unit, campaign, other, digest), "not that seat's"
        assert world.participants.consume_code(unit, campaign, seat, digest)
        assert not world.participants.consume_code(unit, campaign, seat, digest), "single use"


def test_an_expired_code_is_refused_and_its_replacement_revokes_the_dead_row(world: World) -> None:
    """AUD-4's seven days, and the consequence of the live-code index being
    unable to read the clock: without the revocation, issuing a replacement for
    an expired code would collide with it."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    long_ago = datetime.now(UTC) - timedelta(days=8)
    with world.db.transaction() as unit:
        stale, secret = world.participants.issue_code(unit, campaign, seat, now=long_ago)
        assert stale.expires_at == long_ago + timedelta(days=7)
        digest = sha256(secret.encode("utf-8")).hexdigest()
        assert not world.participants.consume_code(unit, campaign, seat, digest), "expired"

        fresh, _ = world.participants.issue_code(unit, campaign, seat)
        codes = {c.id: c for c in world.participants.codes(unit, seat)}
        assert codes[stale.id].revoked_at is not None, "the dead row is revoked first"
        assert codes[fresh.id].revoked_at is None
        live = [c for c in codes.values() if c.consumed_at is None and c.revoked_at is None]
        assert [c.id for c in live] == [fresh.id], "at most one live code per participant"


def test_a_participant_has_at_most_one_unrevoked_device(world: World) -> None:
    """AUD-5. Resetting a personal link is revoking the old credential and
    issuing a new one, so the old browser stops working at that moment."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        first, first_secret = world.participants.issue_device_credential(unit, campaign, seat)
        second, _ = world.participants.issue_device_credential(unit, campaign, seat)
        held = {d.id: d for d in world.participants.device_credentials(unit, seat)}
        assert held[first.id].revoked_at is not None
        assert held[second.id].revoked_at is None
        assert [d.id for d in held.values() if d.is_active] == [second.id]
        # The revoked credential is still findable by digest: a request carrying
        # it is refused for being revoked, not mistaken for an unknown device.
        found = world.participants.find_device_credential(
            unit, sha256(first_secret.encode("utf-8")).hexdigest()
        )
        assert found is not None and not found.is_active


# Behaviour 11b — Remove and Reset, which need primitives that only revoke.


def test_removing_a_participant_revokes_their_codes_and_their_device(world: World) -> None:
    """RQ-5: a revocation makes its change true in step 1 — "the credential and
    the codes revoked, the participant marked removed". AUD-16 says the same of
    Remove. Nobody ever wants a removed participant with a live credential, so
    `remove` does all three in one call rather than leaving two of them to a
    caller that might forget."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        world.participants.issue_code(unit, campaign, seat)
        world.participants.issue_device_credential(unit, campaign, seat)

    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, seat)
        assert [c for c in world.participants.codes(unit, seat) if c.revoked_at is None] == []
        assert [
            d for d in world.participants.device_credentials(unit, seat) if d.is_active
        ] == []


def test_a_reset_can_revoke_the_device_without_minting_another_one(world: World) -> None:
    """AUD-5's Reset revokes the device and issues a CODE, so it cannot go
    through `issue_device_credential`, which revokes-and-issues. Before these
    primitives, Remove and Reset could not be built at all."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        world.participants.issue_device_credential(unit, campaign, seat)

    with world.db.transaction() as unit:
        held = world.participants.hold(unit, seat)
        assert held is not None and held.is_active and held.campaign_id == campaign
        assert world.participants.revoke_device_credentials(unit, campaign, seat) == 1
        assert world.participants.revoke_device_credentials(unit, campaign, seat) == 0, "idempotent"
        assert [
            d for d in world.participants.device_credentials(unit, seat) if d.is_active
        ] == []
        fresh, _ = world.participants.issue_code(unit, campaign, seat)
        assert fresh.revoked_at is None


def test_revoking_the_codes_mints_nothing_and_says_how_many_it_revoked(world: World) -> None:
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        world.participants.issue_code(unit, campaign, seat)
        assert world.participants.revoke_codes(unit, campaign, seat) == 1
        assert world.participants.revoke_codes(unit, campaign, seat) == 0
        assert world.participants.codes(unit, seat) != [], "revoked, never deleted"
        assert [c for c in world.participants.codes(unit, seat) if c.revoked_at is None] == []


def test_holding_a_participant_hands_back_the_row_so_the_caller_can_read_it(world: World) -> None:
    """F-1: the lock the ADR says "the caller holds" (RQ-5, RC-13) is a
    primitive rather than something private to two methods. It returns the row
    because the caller must test `is_active` UNDER the lock — that is the whole
    of RC-13 — and because the enrolment route learns the campaign from it."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        held = world.participants.hold(unit, seat)
        assert held is not None
        assert (held.id, held.campaign_id, held.is_active) == (seat, campaign, True)
        assert world.participants.hold(unit, "prt_" + "z" * 22) is None


def test_nothing_is_minted_against_a_removed_seat_and_its_old_code_is_spent_by_nobody(
    world: World,
) -> None:
    """RC-13's other half. `consume_code` answers False rather than raising: it
    is the unauthenticated route, RQ-5 asks it to fail generically when no row
    changed, and a caller must not be able to tell a removed seat from a spent
    code. The two that return a record cannot answer False, so they name it."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    digest = _a_code(world, campaign, seat)

    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, seat)

    with world.db.transaction() as unit:
        assert not world.participants.consume_code(unit, campaign, seat, digest)
        with pytest.raises(ParticipantRemoved):
            world.participants.issue_device_credential(unit, campaign, seat)
        with pytest.raises(ParticipantRemoved):
            world.participants.issue_code(unit, campaign, seat)


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


def test_ending_a_session_revokes_every_generation_it_ever_had(world: World) -> None:
    """A join that commits just after a Rotate holds an unrevoked credential of
    the generation the Rotate retired — `issue_credential` reads the generation
    without a lock, deliberately, so that a join never makes a Stop wait. The
    table is over, so an ending leaves nothing with `revoked_at IS NULL` behind
    it, whatever generation it belongs to."""
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        first, _ = world.sessions.issue_credential(unit, campaign, session.id)
        world.sessions.rotate_link(unit, campaign, session.id)
        second, _ = world.sessions.issue_credential(unit, campaign, session.id)
        assert (first.link_generation, second.link_generation) == (1, 2)

    # Put the first generation's credential back to unrevoked, which is the
    # state the race leaves it in: made in generation 1, committed after the
    # Rotate that retired it.
    with world.db.transaction() as unit:
        stray, _ = world.sessions.issue_credential(unit, campaign, session.id)

    with world.db.transaction() as unit:
        world.sessions.end(unit, campaign, session.id)
        held = {c.id: c for c in world.sessions.credentials(unit, session.id)}
        assert [c.id for c in held.values() if c.is_active] == []
        assert held[stray.id].revoked_at is not None and held[second.id].revoked_at is not None


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
            world.participants.issue_code(unit, campaign, seat.id)
            world.participants.issue_device_credential(unit, campaign, seat.id)
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
        assert world.participants.codes(unit, seat.id) == []
        assert world.participants.device_credentials(unit, seat.id) == []
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
    participant could be added to a campaign that does not exist, and a code or
    a credential minted for a participant that does not exist — every unit test
    on the fakes green, and the first real request a 500 carrying the driver's
    text. They share one state now, and both worlds answer with the same named
    refusal rather than an integrity error."""
    nowhere = "cmp_" + "z" * 22
    nobody = "prt_" + "z" * 22
    with world.db.transaction() as unit:
        with pytest.raises(MissingParent):
            world.participants.add(unit, nowhere, alias="Rook")
        with pytest.raises(MissingParent):
            world.participants.issue_code(unit, nowhere, nobody)
        with pytest.raises(MissingParent):
            world.participants.issue_device_credential(unit, nowhere, nobody)
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
    private text (SEC-20), so neither belongs in one."""
    moment = datetime.now(UTC)
    digest = "f" * 64
    records = [
        Campaign("cmp_x", 1, "Nocturne", moment, moment),
        Participant("prt_x", "cmp_x", "Rook", moment),
        EnrolmentCode("enc_x", "prt_x", digest, moment, moment),
        DeviceCredential("dev_x", "prt_x", digest, moment),
        TableSession("ses_x", "cmp_x", 1, "live", moment, moment, 1, digest),
        TableCredential("tcr_x", "ses_x", 1, digest, moment),
    ]
    for record in records:
        printed = repr(record)
        assert digest not in printed, f"{type(record).__name__} shows a digest"
        assert "Rook" not in printed, f"{type(record).__name__} shows an alias"
        assert "Nocturne" not in printed, f"{type(record).__name__} shows a campaign name"
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
def test_two_racing_consumptions_of_one_code_leave_exactly_one_winner(dsn: str) -> None:
    """RC-13. The guard is the statement itself: under READ COMMITTED the second
    UPDATE re-reads the row the first committed and matches nothing."""
    _seed(dsn)
    db = _database(dsn)
    participants = PostgresParticipantStore()
    with db.transaction() as unit:
        seat = participants.add(unit, CAMPAIGN, alias="Rook")
        _, secret = participants.issue_code(unit, CAMPAIGN, seat.id)
    digest = sha256(secret.encode("utf-8")).hexdigest()

    def consume() -> bool:
        with db.transaction() as unit:
            return participants.consume_code(unit, CAMPAIGN, seat.id, digest)

    assert sorted(_race(consume), key=str) == [False, True]


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


# ── RC-13 on a real server: a Reset or a Remove against an enrolment ─────────
#
# "in either order ... once the Reset or the Remove has committed, no credential
# made from an older code is valid: all three lock the participant row, and the
# code is consumed by a conditional write under it". The interleaving is
# explicit — one transaction holds the row, the other is started and the server
# is asked whether it is really blocked — so nothing here depends on timing.


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
                assert PostgresParticipantStore().hold(unit, participant_id) is not None
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


@contextmanager
def _a_seat_with_a_code(dsn: str) -> Iterator[tuple[Database, PostgresParticipantStore, str, str]]:
    db = _database(dsn, PATIENT)
    participants = PostgresParticipantStore()
    with db.transaction() as unit:
        seat = participants.add(unit, CAMPAIGN, alias="Rook")
        _, secret = participants.issue_code(unit, CAMPAIGN, seat.id)
    yield db, participants, seat.id, sha256(secret.encode("utf-8")).hexdigest()


@needs_db
def test_a_reset_waiting_behind_an_enrolment_finishes_and_kills_the_older_code(
    dsn: str, owner: int
) -> None:
    """RC-13, enrolment first. The enrolment holds the seat, spends the code and
    binds a device; the Reset waits for the seat — it never deadlocks, because
    both took the participant row before any code row — and when it commits the
    device is revoked and the spent code is spent for good."""
    with _a_seat_with_a_code(dsn) as (db, participants, seat, digest):
        def enrol(unit: Any) -> None:
            assert participants.consume_code(unit, CAMPAIGN, seat, digest)
            participants.issue_device_credential(unit, CAMPAIGN, seat)

        def reset(unit: Any) -> int:
            assert participants.hold(unit, seat) is not None
            revoked = participants.revoke_device_credentials(unit, CAMPAIGN, seat)
            participants.issue_code(unit, CAMPAIGN, seat)
            return revoked

        revoked = _while_another_transaction_holds_the_seat(dsn, db, seat, enrol, reset)
        assert revoked == 1, "the Reset revoked the device the enrolment had just bound"

    with db.transaction() as unit:
        assert not participants.consume_code(unit, CAMPAIGN, seat, digest), "spent for good"
        assert [d for d in participants.device_credentials(unit, seat) if d.is_active] == []


@needs_db
def test_an_enrolment_waiting_behind_a_reset_makes_no_credential_from_the_older_code(
    dsn: str, owner: int
) -> None:
    """RC-13, Reset first — the direction that matters. Once the Reset has
    committed, the code the player is holding is revoked, so the conditional
    write matches nothing and no device is ever bound."""
    with _a_seat_with_a_code(dsn) as (db, participants, seat, digest):
        def reset(unit: Any) -> None:
            participants.revoke_device_credentials(unit, CAMPAIGN, seat)
            participants.issue_code(unit, CAMPAIGN, seat)

        def enrol(unit: Any) -> bool:
            assert participants.hold(unit, seat) is not None
            return participants.consume_code(unit, CAMPAIGN, seat, digest)

        assert _while_another_transaction_holds_the_seat(dsn, db, seat, reset, enrol) is False

    with db.transaction() as unit:
        assert participants.device_credentials(unit, seat) == [], "no device was ever bound"


@needs_db
def test_a_remove_waiting_behind_an_enrolment_still_revokes_what_it_bound(
    dsn: str, owner: int
) -> None:
    """AUD-16 and RC-13 together, Remove second. The removal waits for the seat,
    then revokes the credential the enrolment bound a moment earlier — which is
    the whole reason `remove` revokes rather than only marking."""
    with _a_seat_with_a_code(dsn) as (db, participants, seat, digest):
        def enrol(unit: Any) -> None:
            assert participants.consume_code(unit, CAMPAIGN, seat, digest)
            participants.issue_device_credential(unit, CAMPAIGN, seat)

        def remove(unit: Any) -> bool:
            assert participants.hold(unit, seat) is not None
            return participants.remove(unit, CAMPAIGN, seat)

        assert _while_another_transaction_holds_the_seat(dsn, db, seat, enrol, remove) is True

    with db.transaction() as unit:
        gone = participants.get(unit, seat)
        assert gone is not None and not gone.is_active
        assert [d for d in participants.device_credentials(unit, seat) if d.is_active] == []


@needs_db
def test_an_enrolment_waiting_behind_a_remove_binds_nothing(dsn: str, owner: int) -> None:
    """Remove first. The seat is gone by the time the enrolment gets the row, so
    the conditional write matches nothing — the statement itself names the
    participant's status, so there is no window between the check and the write
    — and minting a credential is refused outright."""
    with _a_seat_with_a_code(dsn) as (db, participants, seat, digest):
        def remove(unit: Any) -> None:
            assert participants.remove(unit, CAMPAIGN, seat)

        def enrol(unit: Any) -> bool:
            held = participants.hold(unit, seat)
            assert held is not None and not held.is_active, "read under the lock (RC-13)"
            return participants.consume_code(unit, CAMPAIGN, seat, digest)

        assert _while_another_transaction_holds_the_seat(dsn, db, seat, remove, enrol) is False

    with db.transaction() as unit:
        with pytest.raises(ParticipantRemoved):
            participants.issue_device_credential(unit, CAMPAIGN, seat)


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


def test_the_slot_clearing_extension_point_is_empty_in_this_bead() -> None:
    """`1kg.7.1` fills it. Until then a narrowing has nothing to clear, and this
    says so in one place rather than by omission."""
    with InMemoryDatabase().transaction() as unit:
        assert no_slots(unit, "ses_x") is None

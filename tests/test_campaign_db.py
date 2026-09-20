"""The campaign schema against a real PostgreSQL (1kg.2.1).

What no fake can prove, and what this file is therefore for: who **blocks** whom
on a campaign's authorisation row, that the bounds RQ-8 asks for really fire,
that `FOR NO KEY UPDATE` lets a referencing insert through where `FOR UPDATE`
would not (RQ-3), and that every transaction is READ COMMITTED whatever the
server has been told to default to (RQ-2(d)).

`service/tests/test_db.py` owns the other half — the lock-order rules, the
refusals and the statements — which the in-memory twin can show and which runs
on any machine. Neither file claims the other's half.

Requires DATABASE_URL, which CI sets for this file (`.github/workflows/ci.yml`,
pinned by `service/tests/test_ci_workflow.py`). Without it every test here skips,
and a skip is reported as a skip. Run from the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_campaign_db.py -q
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service.db import (
    CampaignAuthzMissing,
    CampaignLockSettings,
    Database,
    PoolSettings,
)

pytestmark = needs_db

CAMPAIGN = "cmp_" + "a" * 22
OTHER_CAMPAIGN = "cmp_" + "b" * 22
PARTICIPANT = "prt_" + "a" * 22

#: A second is long enough that a lock taken first is always taken first, and
#: short enough that a test which should time out does not hold the job up.
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
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


def test_every_transaction_is_read_committed_whatever_the_server_defaults_to(dsn: str) -> None:
    """RQ-2's reasoning about what a lock holds still is READ COMMITTED's. An
    operator who sets the database or the role to SERIALIZABLE would otherwise
    change that reasoning silently — and under SERIALIZABLE the same code would
    start failing with serialization errors instead of waiting."""
    with connect(dsn) as conn:
        database = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(f'ALTER DATABASE "{database}" SET default_transaction_isolation = %s', ("serializable",))

    with connect(dsn) as fresh:
        assert fresh.execute("SHOW transaction_isolation").fetchone()[0] == "serializable", (
            "the server default did not change, so this test proves nothing"
        )

    with _database(dsn).transaction() as unit:
        assert unit.conn.execute("SHOW transaction_isolation").fetchone()[0] == "read committed"


# ── Behaviour 27 — the lock matrix on the real row ───────────────────────────


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


def test_locking_a_campaign_with_no_authorisation_row_raises_the_named_error(dsn: str) -> None:
    """RQ-2(c), fail closed. The campaign may never have existed or may have been
    deleted under the caller; both are the same answer, and neither is a silent
    success."""
    with pytest.raises(CampaignAuthzMissing, match="authorisation"):
        with _database(dsn).transaction() as unit:
            unit.lock_campaign(CAMPAIGN, shared=True)


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


def test_a_caller_may_raise_the_transaction_bound_for_its_own_longer_work(dsn: str, owner: int) -> None:
    with _database(dsn).transaction() as unit:
        unit.lock_campaign(CAMPAIGN, shared=False, transaction_timeout_s=42)
        assert unit.conn.execute("SHOW transaction_timeout").fetchone()[0] == "42s"


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


def _a_participant(dsn: str) -> None:
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO campaign.participants (id, campaign_id, alias) VALUES (%s, %s, 'Rook')",
            (PARTICIPANT, CAMPAIGN),
        )


@contextmanager
def _holding_the_participant_row(dsn: str, strength: str) -> Iterator[None]:
    took, release = threading.Event(), threading.Event()

    def hold() -> None:
        with connect(dsn, autocommit=False) as conn:
            conn.execute(
                f"SELECT id FROM campaign.participants WHERE id = %s {strength}", (PARTICIPANT,)
            )
            took.set()
            release.wait(PATIENCE)
            conn.rollback()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE), "the participant row was never locked"
    try:
        yield
    finally:
        release.set()
        thread.join(PATIENCE)


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


def test_a_participant_locked_for_no_key_update_does_not_block_a_row_that_references_it(
    dsn: str, owner: int
) -> None:
    """RQ-3's reason, shown once. A display writing a participant's slot holds
    that participant's row; a join inserts a row that references it. With
    `FOR UPDATE` the second waits for the first — which is how a Stop ends up
    waiting behind a join, and how two of them deadlock. `FOR NO KEY UPDATE`
    does not conflict with the `FOR KEY SHARE` a foreign-key check takes, so
    every explicit row lock a store takes is that one."""
    _a_participant(dsn)
    with _holding_the_participant_row(dsn, "FOR NO KEY UPDATE"):
        _insert_a_code_referencing_the_participant(dsn)

    with connect(dsn) as conn:
        conn.execute("DELETE FROM campaign.enrolment_codes")

    with _holding_the_participant_row(dsn, "FOR UPDATE"):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _insert_a_code_referencing_the_participant(dsn)

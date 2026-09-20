"""The campaign schema against a real PostgreSQL (1kg.2.1).

What no fake can prove, and what this file is therefore for: who **blocks** whom
on a campaign's authorisation row, that the bounds RQ-8 asks for really fire,
that `FOR NO KEY UPDATE` lets a referencing insert through where `FOR UPDATE`
would not (RQ-3), and that every transaction is READ COMMITTED whatever the
server has been told to default to (RQ-2(d)).

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
from service.campaign_store import (
    AliasTaken,
    Campaign,
    InMemoryCampaignStore,
    LiveSessionExists,
    NotLive,
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
    with _holding_the_participant_row(dsn, "FOR NO KEY UPDATE"):
        _insert_a_code_referencing_the_participant(dsn)

    with connect(dsn) as conn:
        conn.execute("DELETE FROM campaign.enrolment_codes")

    with _holding_the_participant_row(dsn, "FOR UPDATE"):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            _insert_a_code_referencing_the_participant(dsn)


# ── The shared behavioural suite ─────────────────────────────────────────────
#
# One set of behaviours, run twice: against the in-memory twin, and against
# PostgreSQL. A rule that only one of them keeps is a rule the two will drift
# apart on, and the drift shows up in the bead that composes them, not here.
# The `postgres` parameter carries `needs_db`, so without a server it skips and
# the `fake` parameter still runs.


@dataclass
class World:
    """The three stores over one database, and two GMs to own things."""

    kind: str
    db: Any
    campaigns: Any
    participants: Any
    sessions: Any
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
        PostgresTableSessionStore(slot_clear=record, settings=QUICK),
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


def _a_session(world: World, campaign_id: str, *, hours: int = 12, owner: int | None = None):
    with world.db.transaction() as unit:
        return world.sessions.start(
            unit,
            campaign_id,
            gm_user_id=world.owner if owner is None else owner,
            expires_at=datetime.now(UTC) + timedelta(hours=hours),
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


def test_a_removed_participant_frees_their_alias_and_keeps_their_row(world: World) -> None:
    """RQ-3/W-3: marked removed, never deleted — a row another transaction may
    be referencing must not vanish under it, and an audit row that names the
    removal must still resolve."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, seat)
        assert not world.participants.remove(unit, seat), "removing twice is not an error"

        gone = world.participants.get(unit, seat)
        assert gone is not None and gone.removed_at is not None
        assert world.participants.list_for_campaign(unit, campaign) == []
        assert [p.id for p in world.participants.list_for_campaign(
            unit, campaign, include_removed=True
        )] == [seat]

        again = world.participants.add(unit, campaign, alias="rook")
        assert again.id != seat, "a removed alias is free again, on a new seat"


# Behaviours 12 and 13 — codes and devices.


def test_a_code_is_single_use_and_only_its_own_participant_can_spend_it(world: World) -> None:
    campaign = _a_campaign(world)
    seat, other = _a_participant(world, campaign, "Rook"), _a_participant(world, campaign, "Wren")
    with world.db.transaction() as unit:
        code, secret = world.participants.issue_code(unit, seat)
        digest = sha256(secret.encode("utf-8")).hexdigest()
        assert code.code_digest == digest, "the store keeps the digest, never the code"

        assert world.participants.find_code(unit, digest) is not None
        assert not world.participants.consume_code(unit, other, digest), "not that seat's code"
        assert world.participants.consume_code(unit, seat, digest)
        assert not world.participants.consume_code(unit, seat, digest), "single use"


def test_an_expired_code_is_refused_and_its_replacement_revokes_the_dead_row(world: World) -> None:
    """AUD-4's seven days, and the consequence of the live-code index being
    unable to read the clock: without the revocation, issuing a replacement for
    an expired code would collide with it."""
    campaign = _a_campaign(world)
    seat = _a_participant(world, campaign, "Rook")
    long_ago = datetime.now(UTC) - timedelta(days=8)
    with world.db.transaction() as unit:
        stale, secret = world.participants.issue_code(unit, seat, now=long_ago)
        assert stale.expires_at == long_ago + timedelta(days=7)
        digest = sha256(secret.encode("utf-8")).hexdigest()
        assert not world.participants.consume_code(unit, seat, digest), "expired"

        fresh, _ = world.participants.issue_code(unit, seat)
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
        first, first_secret = world.participants.issue_device_credential(unit, seat)
        second, _ = world.participants.issue_device_credential(unit, seat)
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
                gm_user_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
    # Another GM is unaffected, and so is this one once the first has ended.
    _a_session(world, elsewhere, owner=world.other_owner)


def test_a_gm_may_start_again_once_their_session_has_ended(world: World) -> None:
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        world.sessions.end(unit, session.id)
    assert _a_session(world, campaign)[0].id != session.id


# Behaviour 15 — the narrow primitive.


def test_narrow_advances_the_reveal_epoch_and_calls_its_extension_point_once(world: World) -> None:
    """RQ-7. The audio epoch moves only when the caller asks, because muting the
    table and narrowing what it can see are different decisions."""
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    assert (session.reveal_epoch, session.audio_epoch) == (0, 0)

    with world.db.transaction() as unit:
        quiet = world.sessions.narrow(unit, session.id)
        assert quiet is not None
        assert (quiet.reveal_epoch, quiet.audio_epoch) == (1, 0)
        assert world.slot_clears == [(unit, session.id)], "exactly once, with (unit, session_id)"

        loud = world.sessions.narrow(unit, session.id, audio=True)
        assert loud is not None
        assert (loud.reveal_epoch, loud.audio_epoch) == (2, 1)
        assert world.slot_clears == [(unit, session.id), (unit, session.id)]

    with world.db.transaction() as unit:
        assert world.sessions.narrow(unit, "ses_" + "z" * 22) is None


# Behaviours 16 and 17 — End and Rotate, each closing a generation.


def test_ending_a_session_revokes_its_generation_and_advances_both_epochs(world: World) -> None:
    """SEC-9: 'Either one revokes every table credential of the old generation in
    the same transaction that advances the reveal and audio epochs.' End is the
    'either' that is easy to forget, because the session is going away anyway —
    but a credential that outlives the epoch retiring it is a live door."""
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        joined, _ = world.sessions.issue_credential(unit, session.id)

    with world.db.transaction() as unit:
        ended = world.sessions.end(unit, session.id)
        assert ended is not None
        assert ended.state == "ended" and ended.ended_at is not None
        assert (ended.reveal_epoch, ended.audio_epoch) == (1, 1)
        assert ended.link_digest is None, "the retired link opens nothing"
        held = {c.id: c for c in world.sessions.credentials(unit, session.id)}
        assert held[joined.id].revoked_at is not None

    with world.db.transaction() as unit:
        again = world.sessions.end(unit, session.id)
        assert again is not None
        assert (again.reveal_epoch, again.audio_epoch) == (1, 1), "ending twice changes nothing"
        assert again.ended_at == ended.ended_at


def test_rotating_the_link_retires_the_old_generation_and_leaves_the_session_live(
    world: World,
) -> None:
    """SEC-9 again, and REVEAL-17: rotating is not restarting, so the session
    keeps its id, its start and its expiry."""
    campaign = _a_campaign(world)
    session, first_link = _a_session(world, campaign)
    with world.db.transaction() as unit:
        old_credential, _ = world.sessions.issue_credential(unit, session.id)

    with world.db.transaction() as unit:
        rotated = world.sessions.rotate_link(unit, session.id)
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
        fresh, _ = world.sessions.issue_credential(unit, session.id)
        assert fresh.link_generation == 2
        held = {c.id: c for c in world.sessions.credentials(unit, session.id)}
        assert held[old_credential.id].revoked_at is not None
        assert held[fresh.id].revoked_at is None


def test_a_session_that_is_no_longer_live_cannot_have_its_link_rotated(world: World) -> None:
    campaign = _a_campaign(world)
    session, _ = _a_session(world, campaign)
    with world.db.transaction() as unit:
        world.sessions.end(unit, session.id)
    with world.db.transaction() as unit:
        with pytest.raises(NotLive):
            world.sessions.rotate_link(unit, session.id)


# Behaviour 18 — the stale sessions a start has to clear out of its own way.


def test_expired_live_sessions_for_gm_returns_that_gms_overdue_live_ones_only(
    world: World,
) -> None:
    """RQ-7: a GM whose last session timed out unnoticed must be able to start
    again, so the start ends the stale one first rather than meeting the index."""
    campaign, other_campaign = _a_campaign(world), _a_campaign(world, name="Other")
    stale, _ = _a_session(world, campaign, hours=-1)
    theirs, _ = _a_session(world, other_campaign, hours=-1, owner=world.other_owner)

    with world.db.transaction() as unit:
        assert [s.id for s in world.sessions.expired_live_sessions_for_gm(unit, world.owner)] == [
            stale.id
        ], "another GM's stale session is not this GM's to end"
        assert [s.id for s in world.sessions.expired_live_sessions_for_gm(
            unit, world.other_owner
        )] == [theirs.id]

        world.sessions.end(unit, stale.id)
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
            world.participants.issue_code(unit, seat.id)
            world.participants.issue_device_credential(unit, seat.id)
            session, _ = world.sessions.start(
                unit,
                campaign,
                gm_user_id=world.owner,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
            world.sessions.issue_credential(unit, session.id)
            raise RuntimeError("boom")

    with world.db.transaction() as unit:
        assert world.campaigns.get(unit, doomed.id, owner_id=world.owner) is None
        assert world.campaigns.authz_revision(unit, doomed.id) is None
        assert world.participants.get(unit, seat.id) is None
        assert world.participants.codes(unit, seat.id) == []
        assert world.participants.device_credentials(unit, seat.id) == []
        assert world.sessions.get(unit, session.id) is None
        assert world.sessions.credentials(unit, session.id) == []


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
        _, secret = participants.issue_code(unit, seat.id)
    digest = sha256(secret.encode("utf-8")).hexdigest()

    def consume() -> bool:
        with db.transaction() as unit:
            return participants.consume_code(unit, seat.id, digest)

    assert sorted(_race(consume), key=str) == [False, True]


@needs_db
def test_two_racing_session_starts_for_one_gm_leave_exactly_one_winner(dsn: str) -> None:
    """REVEAL-2's concurrency half. The partial unique index decides it; the
    loser gets the same refusal a sequential second start gets."""
    owner = _seed(dsn)
    db = _database(dsn)
    sessions = PostgresTableSessionStore(settings=QUICK)
    expires = datetime.now(UTC) + timedelta(hours=12)

    def start() -> object:
        with db.transaction() as unit:
            return sessions.start(unit, CAMPAIGN, gm_user_id=owner, expires_at=expires)[0].id

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


# The guards and bounds, which belong to neither world ────────────────────────


@pytest.mark.parametrize(
    ("store", "method", "args"),
    [
        pytest.param(PostgresCampaignStore(), "list_for_owner", (1,), id="campaigns"),
        pytest.param(PostgresParticipantStore(), "get", ("prt_x",), id="participants"),
        pytest.param(PostgresTableSessionStore(), "get", ("ses_x",), id="sessions"),
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


@pytest.mark.parametrize("alias", ["", "a" * 41])
def test_an_alias_outside_the_column_bound_is_refused_without_being_quoted(alias: str) -> None:
    with pytest.raises(ValueError, match="1 to 40 characters") as refusal:
        check_alias(alias)
    assert alias not in str(refusal.value) or alias == "", "a refusal never repeats private text"


def test_the_slot_clearing_extension_point_is_empty_in_this_bead() -> None:
    """`1kg.7.1` fills it. Until then a narrowing has nothing to clear, and this
    says so in one place rather than by omission."""
    with InMemoryDatabase().transaction() as unit:
        assert no_slots(unit, "ses_x") is None

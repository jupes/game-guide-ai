"""The live table session's lifecycle on the in-memory twin (1kg.2.3).

What the twin can show on its own: that a deadlock's victim is retried in a
fresh transaction and that three of them are the database being unavailable —
with nothing half-written either way (L-5, AC-A9); that no path through the
lifecycle puts a secret, a digest or a private name into a log line, an
exception, a `repr()` or an audit row (SEC-20, SEC-21, AC-A8); and the pure
per-frame predicate (L-12).

Everything the two worlds must agree on is in `tests/test_campaign_db.py`'s
shared suite, and who waits for whom is that file's two-connection tests.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import psycopg
import pytest

from service.audit_log import InMemoryAuditLog
from service.campaign_store import InMemoryCampaignStore, MissingParent
from service.db import InMemoryDatabase
from service.jobs import InMemoryJobQueue, Job, JobContext
from service.reconciliation import RECONCILE_KIND, enqueue_reconciliation
from service.table_session_store import InMemoryTableSessionStore, Liveness, no_slots
from service.table_sessions import (
    EXPIRE_KIND,
    BackendUnavailable,
    Binding,
    FanOutBound,
    Inactive,
    TableSessions,
    may_write,
)

OWNER, STRANGER = 1, 2
#: A campaign name that must never be spoken: SEC-20's positive rule is that a
#: log line carries opaque ids, codes, sizes and durations, and a name is none.
PRIVATE_NAME = "The Nocturne of Vex"


class _Twin:
    """The stores, the ledger and the outbox over one in-memory database."""

    def __init__(self) -> None:
        self.db = InMemoryDatabase()
        self.campaigns = InMemoryCampaignStore(self.db)
        self.sessions = InMemoryTableSessionStore(self.db, slot_clear=no_slots)
        self.audit = InMemoryAuditLog()
        self.jobs = InMemoryJobQueue(db=self.db)

    def lifecycle(self, db: Any = None, clock: Callable[[], datetime] | None = None) -> TableSessions:
        return TableSessions(
            self.db if db is None else db,
            campaigns=self.campaigns,
            sessions=self.sessions,
            audit=self.audit,
            jobs=self.jobs,
            reconcile=lambda unit, campaign_id: enqueue_reconciliation(unit, self.jobs, campaign_id),
            clock=clock,
        )

    def campaign(self, name: str = "Nocturne") -> str:
        with self.db.transaction() as unit:
            return self.campaigns.create(unit, owner_id=OWNER, name=name).id

    def ledger(self, campaign: str) -> list[Any]:
        with self.db.transaction() as unit:
            return self.audit.for_campaign(unit, campaign)

    def queued(self) -> list[str]:
        return [row.job.kind for row in sorted(self.jobs._rows.values(), key=lambda r: r.job.id)]


class _Deadlocking:
    """The twin's database, whose next `failures` transactions are chosen as a
    deadlock's victim once their work is done — and so rolled back, as
    PostgreSQL rolls a victim back."""

    def __init__(self, db: InMemoryDatabase, failures: int) -> None:
        self._db = db
        self.failures = failures
        self.opened = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._db.transaction() as unit:
            self.opened += 1
            yield unit
            if self.failures:
                self.failures -= 1
                raise psycopg.errors.DeadlockDetected("deadlock detected")


def _command() -> str:
    return secrets.token_urlsafe(16)


def _a_due_session(twin: _Twin, campaign: str) -> str:
    """A session still `live` twelve hours after it started, eleven after it
    expired — the row the expiry job exists for."""
    started = datetime.now(UTC) - timedelta(hours=12)
    with twin.db.transaction() as unit:
        return twin.sessions.start(
            unit,
            campaign,
            owner_id=OWNER,
            expires_at=started + timedelta(hours=11),
            command_id=_command(),
            now=started,
        ).id


_REVOCATIONS = ("end", "rotate", "expire")


def _revoke(service: TableSessions, how: str, campaign: str, session_id: str) -> Any:
    if how == "end":
        return service.end(OWNER, campaign, session_id)
    if how == "rotate":
        return service.rotate(OWNER, campaign, session_id, command_id=_command())
    return service.expire(campaign, session_id)


def _a_session_for(twin: _Twin, how: str, campaign: str) -> str:
    if how == "expire":
        return _a_due_session(twin, campaign)
    return twin.lifecycle().start(OWNER, campaign, command_id=_command()).session.id


@pytest.mark.parametrize("how", _REVOCATIONS)
def test_a_deadlock_victim_is_retried_in_a_fresh_transaction_and_succeeds(how: str) -> None:
    """RQ-3: End, Rotate and expiry are retried by the server when they lose a
    deadlock — each attempt a fresh transaction, the lost one rolled back, so
    the one that commits writes one audit row and one reconciliation."""
    twin = _Twin()
    campaign = twin.campaign()
    session_id = _a_session_for(twin, how, campaign)
    before = len(twin.ledger(campaign))
    flaky = _Deadlocking(twin.db, failures=1)
    outcome = _revoke(twin.lifecycle(db=flaky), how, campaign, session_id)
    assert flaky.opened == 2, "the victim's work was done again in a transaction of its own"
    assert len(outcome.reconcile_jobs) == 1
    assert len(twin.ledger(campaign)) == before + 1, "one decision recorded, not two"
    assert twin.queued().count(RECONCILE_KIND) == 1


@pytest.mark.parametrize("how", _REVOCATIONS)
def test_three_deadlocks_are_the_database_unavailable_and_nothing_is_half_written(how: str) -> None:
    """After the third lost deadlock the answer is `BackendUnavailable` — the
    database, retryable, and never a refusal — and none of the three attempts
    left anything behind: the session is as it was."""
    twin = _Twin()
    campaign = twin.campaign()
    session_id = _a_session_for(twin, how, campaign)
    ledger, queued = len(twin.ledger(campaign)), twin.queued()
    flaky = _Deadlocking(twin.db, failures=3)
    with pytest.raises(BackendUnavailable):
        _revoke(twin.lifecycle(db=flaky), how, campaign, session_id)
    assert flaky.opened == 3
    with twin.db.transaction() as unit:
        untouched = twin.sessions.get(unit, session_id)
        assert untouched is not None and untouched.is_live and untouched.link_generation == 1
        assert (untouched.reveal_epoch, untouched.audio_epoch) == (0, 0)
    assert len(twin.ledger(campaign)) == ledger and twin.queued() == queued


def test_a_refusal_other_than_a_deadlock_is_not_retried() -> None:
    twin = _Twin()
    campaign = twin.campaign()
    session_id = twin.lifecycle().start(OWNER, campaign, command_id=_command()).session.id
    flaky = _Deadlocking(twin.db, failures=0)
    with pytest.raises(MissingParent):
        twin.lifecycle(db=flaky).end(STRANGER, campaign, session_id)
    assert flaky.opened == 1


def test_no_path_speaks_a_secret_a_digest_or_a_campaign_name(caplog: pytest.LogCaptureFixture) -> None:
    """AC-A8, the whole lifecycle at once: mint, resolve, revoke, Leave, End,
    Rotate and expiry, the refusals, a deadlock's retry and the expiry job. A
    log line, an exception's message, a `repr()` and an audit row are all
    places a value goes on being read, so none of them may hold one."""
    twin = _Twin()
    campaign = twin.campaign(PRIVATE_NAME)
    said: list[str] = []
    minted: list[Any] = []

    def refused(work: Callable[[], object]) -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - every refusal's message is under test
            said.append(f"{type(exc).__name__}: {exc!r} {exc}")
        else:
            raise AssertionError("expected a refusal")

    with caplog.at_level(logging.DEBUG):
        service = twin.lifecycle()
        started = service.start(OWNER, campaign, command_id=_command())
        session = started.session
        for _ in range(3):
            minted.append(service.mint_screen(OWNER, campaign))
        said += [repr(m) for m in minted] + [repr(m.grant) for m in minted] + [repr(started)]
        said.append(repr(service.resolve_screen(minted[0].secret)))
        service.revoke_screen(OWNER, campaign, minted[0].grant.id)
        assert service.leave(minted[1].secret)
        said.append(repr(service.liveness([session.id])))
        said.append(repr(service.status(OWNER, campaign)))
        refused(lambda: service.mint_screen(STRANGER, campaign))
        refused(lambda: service.end(STRANGER, campaign, session.id))
        refused(lambda: service.start(STRANGER, campaign, command_id=_command()))
        said.append(repr(twin.lifecycle(db=_Deadlocking(twin.db, 1)).rotate(
            OWNER, campaign, session.id, command_id=_command()
        )))
        said.append(repr(service.end(OWNER, campaign, session.id)))
        refused(lambda: service.mint_screen(OWNER, campaign))
        refused(lambda: twin.lifecycle(db=_Deadlocking(twin.db, 3)).end(OWNER, campaign, session.id))

        due = _a_due_session(twin, campaign)
        job = Job(1, EXPIRE_KIND, {"session_id": due, "campaign_id": campaign}, 1, datetime.now(UTC))
        service.expire_handler().run(job, JobContext())

        bounded = twin.lifecycle()
        at = datetime.now(UTC) + timedelta(days=1)
        again = bounded.start(OWNER, campaign, command_id=_command(), now=at).session
        for n in range(1, 10):
            bounded.rotate(OWNER, campaign, again.id, command_id=_command(), now=at + timedelta(seconds=n))
        refused(lambda: bounded.rotate(
            OWNER, campaign, again.id, command_id=_command(), now=at + timedelta(seconds=10)
        ))

    rows = twin.ledger(campaign)
    said += [repr(row) for row in rows] + [f"{row.actor_ref} {row.object_ref} {row.detail}" for row in rows]
    spoken = "\n".join([*said, caplog.text])
    assert "lost a deadlock" in caplog.text, "the retry was logged, so the scan below read it"
    assert PRIVATE_NAME not in spoken, "a campaign name reached a message"
    for screen in minted:
        assert screen.secret not in spoken, "a screen grant reached a message"
        assert screen.grant.credential_digest not in spoken, "a grant's digest reached a message"
        assert sha256(screen.secret.encode("utf-8")).hexdigest() not in spoken
    assert "email" not in spoken.lower()
    assert {e.action for e in rows} >= {
        "session.started", "screen.minted", "screen.revoked", "session.rotated",
        "session.ended", "session.expired",
    }


def test_the_refusals_carry_fixed_messages_that_name_nothing() -> None:
    assert str(Inactive()) == "no live table session for that caller"
    assert str(BackendUnavailable()) == "the table session could not be changed just now"
    bound = FanOutBound(60)
    assert bound.retry_after_s == 60 and "60" not in str(bound)


# ── L-12: the per-frame predicate, pure ─────────────────────────────────────


def _read(
    state: str = "live", *, expires_in: int = 60, generation: int = 1, screens: tuple[str, ...] = ()
) -> dict[str, Liveness]:
    now = datetime.now(UTC)
    return {
        "ses_a": Liveness("ses_a", state, now + timedelta(seconds=expires_in), generation, frozenset(screens))
    }


def test_a_frame_is_written_only_to_a_binding_that_is_still_live() -> None:
    now = datetime.now(UTC)
    account, screen = Binding("ses_a", 1), Binding("ses_a", 1, "tcr_a")
    assert may_write(account, _read(), now)
    assert may_write(screen, _read(screens=("tcr_a",)), now)
    assert not may_write(screen, _read(screens=("tcr_b",)), now), "its grant is not among the live ones"
    assert not may_write(account, _read(generation=2), now), "a retired generation"
    assert not may_write(account, _read("ended"), now)
    assert not may_write(account, _read(expires_in=-1), now), "a row still live past its expiry is dead"
    assert not may_write(Binding("ses_b", 1), _read(), now), "a session the read does not have"


def test_the_predicate_takes_its_clock_and_refuses_a_naive_one() -> None:
    naive = datetime(2026, 9, 28, 12, 0, 0)  # noqa: DTZ001 - the point of the test
    with pytest.raises(ValueError, match="timezone-aware"):
        may_write(Binding("ses_a", 1), _read(), naive)

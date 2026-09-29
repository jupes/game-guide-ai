"""The GM's table-session routes (bead 1kg.2.3's PR-B, agent-forge-harness-1kg.2.10),
through the real app over the in-memory twin.

What a client observes: the order of checks (L-15), the one 404 across tenants
and for every ownership failure (T-2, SEC-3) — the fan-out bound included, which
a stranger never reaches (L-8) — the Paid check point's one call site (L-19),
Start's replay and End's and Rotate's idempotence (the contract's Idempotency
row), the reconciliation handed to `run_after_response` and attempted only after
the answer's last byte (RQ-5), and what the GM is shown (L-14).

Everything two connections decide is `tests/test_campaign_db.py`'s, and the
lifecycle's own rules are `service/tests/test_table_sessions.py`'s and that
file's shared suite.
"""

from __future__ import annotations

import ast
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from httpx import Response

from service import app as appmod
from service import table_session_api
from service.app import app, require_session
from service.audit_log import InMemoryAuditLog
from service.auth_store import User
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase, InMemoryTransaction
from service.invites import Role
from service.job_driver import JobDriver
from service.jobs import InMemoryJobQueue
from service.reconciliation import RECONCILE_KIND, enqueue_reconciliation
from service.session import SessionData
from service.table_session_store import InMemoryTableSessionStore, no_slots
from service.table_sessions import EXPIRE_KIND, FANOUT_BOUND, TableSessions
from service.workbench_api import NOT_FOUND_DETAIL, gm_session, install_workbench
from service.workbench_contracts import TableSessionAnswer

GM_A, GM_B, PLAYER = 1, 2, 3
T0 = datetime(2026, 9, 1, 19, 0, tzinfo=UTC)
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
UNAUTHENTICATED = {"detail": "not signed in"}
MISSING_CAMPAIGN = "cmp_" + "z" * 22
MISSING_SESSION = "ses_" + "z" * 22
MISSING_SCREEN = "tcr_" + "z" * 22
SOURCE = Path(table_session_api.__file__)


class _RecordingDriver:
    """Stands in for the job driver: records what the route hands to
    `run_after_response`, and — when given a log — when it was attempted."""

    def __init__(self, log: list[Any] | None = None) -> None:
        self.ran: list[int] = []
        self._log = log

    def run_job(self, job_id: int) -> None:
        self.ran.append(job_id)
        if self._log is not None:
            self._log.append(("job", job_id))


class _Counting:
    """The twin's database, counting the transactions it opens: a refusal that
    must come before any database access opens none."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db
        self.opened = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self.opened += 1
        with self._db.transaction() as unit:
            yield unit


@dataclass
class _World:
    db: InMemoryDatabase
    counting: _Counting
    campaigns: InMemoryCampaignStore
    sessions: InMemoryTableSessionStore
    audit: InMemoryAuditLog
    jobs: InMemoryJobQueue
    driver: _RecordingDriver
    now: list[datetime] = field(default_factory=lambda: [T0])

    def lifecycle(self, db: Any = None) -> TableSessions:
        return TableSessions(
            self.counting if db is None else db,
            campaigns=self.campaigns,
            sessions=self.sessions,
            audit=self.audit,
            jobs=self.jobs,
            reconcile=lambda unit, campaign_id: enqueue_reconciliation(unit, self.jobs, campaign_id),
            clock=lambda: self.now[0],
        )

    def campaign(self, owner: int = GM_A, name: str = "Nocturne", *, archived: bool = False) -> str:
        with self.db.transaction() as unit:
            made = self.campaigns.create(unit, owner_id=owner, name=name, now=self.now[0]).id
            if archived:
                self.campaigns.set_archived(unit, made, owner_id=owner, archived=True, now=self.now[0])
        return made

    def ledger(self, campaign: str) -> list[str]:
        with self.db.transaction() as unit:
            return [event.action for event in self.audit.for_campaign(unit, campaign)]

    def queued(self) -> list[tuple[int, str]]:
        return [(row.job.id, row.job.kind) for row in sorted(self.jobs._rows.values(), key=lambda r: r.job.id)]

    def stored(self, session_id: str) -> Any:
        with self.db.transaction() as unit:
            return self.sessions.get(unit, session_id)


@pytest.fixture
def world() -> Iterator[_World]:
    db = InMemoryDatabase()
    made = _World(
        db,
        _Counting(db),
        InMemoryCampaignStore(db),
        InMemoryTableSessionStore(db, slot_clear=no_slots),
        InMemoryAuditLog(),
        InMemoryJobQueue(db=db),
        _RecordingDriver(),
    )
    lifecycle = made.lifecycle()
    app.dependency_overrides[appmod.get_table_sessions] = lambda: lifecycle
    app.dependency_overrides[appmod._job_driver] = lambda: made.driver
    app.dependency_overrides[table_session_api.get_clock] = lambda: made.now[0]
    yield made
    for dependency in (appmod.get_table_sessions, appmod._job_driver, table_session_api.get_clock):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(world: _World) -> TestClient:
    _as(GM_A)
    return TestClient(app)


def _as(user_id: int, role: Role = "dm") -> None:
    def session(request: Request) -> SessionData:
        request.state.auth_user = User(id=user_id, email=f"u{user_id}@example.com", role=role, created_at=T0)
        return SessionData(user_id=user_id, role=role)

    app.dependency_overrides[require_session] = session


def _signed_out() -> None:
    def refuse() -> SessionData:
        raise HTTPException(status_code=401, detail="authentication required")

    app.dependency_overrides[require_session] = refuse


def _shape(response: Response) -> tuple[Any, ...]:
    varying = {"date", "content-length", "server", "x-request-id"}
    return (
        response.status_code,
        response.content,
        tuple(sorted((k.lower(), v) for k, v in response.headers.items() if k.lower() not in varying)),
    )


def _command() -> str:
    return secrets.token_urlsafe(16)


def _path(campaign: str) -> str:
    return f"/campaigns/{campaign}/table-session"


def _start(client: TestClient, campaign: str, command: str | None = None) -> Response:
    body = {"schema_version": 1, "command_id": command or _command(), "action": "start"}
    return client.post(_path(campaign), json=body)


def _end(client: TestClient, campaign: str, session: str) -> Response:
    body = {"schema_version": 1, "command_id": _command(), "action": "end", "session_id": session}
    return client.post(_path(campaign), json=body)


def _rotate(client: TestClient, campaign: str, session: str, command: str | None = None) -> Response:
    body = {"schema_version": 1, "command_id": command or _command(), "action": "rotate", "session_id": session}
    return client.post(_path(campaign), json=body)


def _session(response: Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    session = TableSessionAnswer.model_validate(response.json()).session
    assert session is not None
    return session.model_dump(mode="json")


def _mint(world: _World, campaign: str) -> str:
    """A screen of the live session, minted through the lifecycle (the route is
    `test_table_api.py`'s)."""
    return world.lifecycle().mint_screen(GM_A, campaign, now=world.now[0]).grant.id


# ── Start, End, Rotate and the status read ───────────────────────────────────


def test_a_start_opens_one_session_and_its_replay_answers_it_writing_nothing(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    command = _command()
    first = _session(_start(client, campaign, command))
    assert (first["state"], first["gen"], first["screens"], first["ended_at"]) == ("live", 1, [], None)
    assert first["ends_at"] == (T0 + timedelta(hours=12)).isoformat().replace("+00:00", "Z")
    assert world.ledger(campaign) == ["session.started"]
    assert [kind for _, kind in world.queued()] == [EXPIRE_KIND]
    assert set(_start(client, campaign, command).json()) == {"schema_version", "session"}

    replayed = _session(_start(client, campaign, command))
    assert replayed == first
    assert world.ledger(campaign) == ["session.started"], "a replayed Start writes nothing"
    again = _session(_start(client, campaign))
    assert again["session_id"] == first["session_id"], "a new Start while live answers the live session"
    assert world.ledger(campaign) == ["session.started"]


def test_the_status_read_answers_null_then_the_session_and_writes_nothing(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    assert client.get(_path(campaign)).json() == {"schema_version": 1, "session": None}
    started = _session(_start(client, campaign))
    assert _session(client.get(_path(campaign))) == started

    world.now[0] = T0 + timedelta(hours=13)
    overdue = _session(client.get(_path(campaign)))
    assert (overdue["state"], overdue["ended_at"]) == ("ended", started["ends_at"]), (
        "a row still live past its expiry is dead, and reads ended at the moment it stopped serving"
    )
    assert world.stored(started["session_id"]).state == "live", "the read wrote nothing"
    assert world.ledger(campaign) == ["session.started"]


def test_an_end_is_an_answer_as_it_stands_on_repeat_and_hands_its_reconciliation_on(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    session = _session(_start(client, campaign))["session_id"]
    world.now[0] = T0 + timedelta(minutes=5)
    ended = _session(_end(client, campaign, session))
    assert (ended["state"], ended["ended_at"], ended["screens"]) == ("ended", "2026-09-01T19:05:00Z", [])
    reconcile = [job for job, kind in world.queued() if kind == RECONCILE_KIND]
    assert len(reconcile) == 1 and world.driver.ran == reconcile

    assert _session(_end(client, campaign, session)) == ended
    assert world.ledger(campaign) == ["session.started", "session.ended"], "a repeated End writes nothing"
    assert world.driver.ran == reconcile, "and hands nothing on"


def test_a_rotate_replays_its_command_and_a_new_command_rotates_again(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    session = _session(_start(client, campaign))["session_id"]
    command = _command()
    rotated = _session(_rotate(client, campaign, session, command))
    assert (rotated["state"], rotated["gen"]) == ("live", 2)
    assert _session(_rotate(client, campaign, session, command)) == rotated
    assert world.ledger(campaign) == ["session.started", "session.rotated"]
    assert _session(_rotate(client, campaign, session))["gen"] == 3
    assert len(world.driver.ran) == 2, "each rotation hands its reconciliation on; the replay none"


def test_a_start_while_live_in_another_campaign_is_live_elsewhere(client: TestClient, world: _World) -> None:
    first, second = world.campaign(), world.campaign(name="Aubade")
    _session(_start(client, first))
    refused = _start(client, second)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "live_elsewhere"
    assert refused.json()["detail"]["retryable"] is False
    assert world.ledger(second) == [], "Start never ends a table on its own"


def test_a_live_answer_lists_its_screens_by_id_and_time_only(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    _session(_start(client, campaign))
    minted = world.lifecycle().mint_screen(GM_A, campaign, now=world.now[0])
    read = client.get(_path(campaign))
    screens = _session(read)["screens"]
    assert screens == [{"screen_id": minted.grant.id, "created_at": "2026-09-01T19:00:00Z", "last_seen_at": None}]
    assert minted.secret not in read.text and minted.grant.credential_digest not in read.text
    replayed = _session(_start(client, campaign))
    assert replayed["screens"] == screens, "the answer to a Start that finds the table live lists its screens"


# ── The screen revoke ────────────────────────────────────────────────────────


def test_a_screen_revoke_is_204_on_repeat_and_one_audit_row(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    _session(_start(client, campaign))
    screen = _mint(world, campaign)
    path = f"{_path(campaign)}/screens/{screen}"
    assert client.delete(path).status_code == 204
    world.now[0] = T0 + timedelta(seconds=1)
    assert client.delete(path).status_code == 204
    assert world.ledger(campaign) == ["session.started", "screen.minted", "screen.revoked"]
    assert _session(client.get(_path(campaign)))["screens"] == []
    assert [kind for _, kind in world.queued()] == [EXPIRE_KIND], "a revoke advances nothing and enqueues nothing"


# ── T-2: the one 404, and the bound is never an oracle ───────────────────────


def test_every_ownership_failure_is_one_identical_404(client: TestClient, world: _World) -> None:
    mine, other_mine, archived = world.campaign(), world.campaign(name="Aubade"), world.campaign(archived=True)
    theirs = world.campaign(GM_B, "Theirs")
    _as(GM_B)
    their_session = _session(_start(client, theirs))["session_id"]
    their_screen = world.lifecycle().mint_screen(GM_B, theirs, now=world.now[0]).grant.id
    _as(GM_A)
    my_session = _session(_start(client, mine))["session_id"]
    ended_elsewhere = _session(_end(client, mine, my_session))["session_id"]
    _session(_start(client, other_mine))

    refusals = {
        "GET another GM's campaign": client.get(_path(theirs)),
        "GET a missing campaign": client.get(_path(MISSING_CAMPAIGN)),
        "GET a malformed campaign id": client.get(_path("not-an-id")),
        "Start in another GM's campaign": _start(client, theirs),
        "Start in a missing campaign": _start(client, MISSING_CAMPAIGN),
        "Start in an archived campaign": _start(client, archived),
        "End another GM's session": _end(client, theirs, their_session),
        "End another GM's session through my campaign": _end(client, mine, their_session),
        "End a session of another of my campaigns": _end(client, other_mine, ended_elsewhere),
        "End a missing session": _end(client, mine, MISSING_SESSION),
        "Rotate another GM's session": _rotate(client, theirs, their_session),
        "Rotate a session of another of my campaigns": _rotate(client, other_mine, ended_elsewhere),
        "revoke another GM's screen": client.delete(f"{_path(theirs)}/screens/{their_screen}"),
        "revoke another GM's screen through my campaign": client.delete(f"{_path(mine)}/screens/{their_screen}"),
        "revoke a missing screen": client.delete(f"{_path(mine)}/screens/{MISSING_SCREEN}"),
    }
    shapes = {name: _shape(answer) for name, answer in refusals.items()}
    assert {shape[0] for shape in shapes.values()} == {404}, shapes
    assert len(set(shapes.values())) == 1, "every ownership failure is one answer, byte for byte"
    assert refusals["GET a missing campaign"].json() == NOT_FOUND
    assert world.ledger(theirs) == ["session.started", "screen.minted"], "a stranger wrote nothing"
    assert world.ledger(archived) == []


def test_a_stranger_never_reaches_the_bound_the_owner_does(client: TestClient, world: _World) -> None:
    """L-8, AC-B2: the owner fills SEC-35's per-campaign bound; the owner's next
    Rotate is the 429 (the positive control), while a stranger's Rotate and
    Start against that campaign are the missing campaign's 404, byte for byte.
    End is never refused."""
    campaign = world.campaign()
    session = _session(_start(client, campaign))["session_id"]
    for _ in range(FANOUT_BOUND - 1):
        _session(_rotate(client, campaign, session))
    assert len(world.ledger(campaign)) == FANOUT_BOUND

    throttled = _rotate(client, campaign, session)
    assert throttled.status_code == 429
    assert throttled.json()["detail"] == {
        "code": "throttled_user",
        "message": table_session_api.THROTTLED_MESSAGE,
        "retryable": True,
        "retry_after_s": 60,
    }
    _as(GM_B)
    stranger = {"Rotate": _rotate(client, campaign, session), "Start": _start(client, campaign)}
    missing = _shape(_start(client, MISSING_CAMPAIGN))
    assert {name: _shape(answer) for name, answer in stranger.items()} == {"Rotate": missing, "Start": missing}
    _as(GM_A)
    assert _session(_end(client, campaign, session))["state"] == "ended", "End is never refused by the bound"


# ── The order of checks (L-15) ───────────────────────────────────────────────


def test_the_order_of_checks_as_a_client_observes_it(client: TestClient, world: _World) -> None:
    campaign = world.campaign(GM_B, "Theirs")
    foreign = {"origin": "https://evil.example"}
    malformed = {"content-type": "application/json"}
    start = {"schema_version": 1, "command_id": _command(), "action": "start"}

    _signed_out()
    assert client.post(_path(campaign), json=start, headers=foreign).status_code == 403, "origin before auth"
    for method, path in (("GET", _path(campaign)), ("POST", _path(campaign)),
                         ("DELETE", f"{_path(campaign)}/screens/{MISSING_SCREEN}")):
        answer = client.request(method, path, json=start if method == "POST" else None)
        assert (answer.status_code, answer.json()) == (401, UNAUTHENTICATED), (method, path)
    _as(PLAYER, role="player")
    role = client.post(_path(campaign), json=start)
    assert (role.status_code, role.json()["detail"]["code"]) == (403, "forbidden"), "the dm gate before the body"
    assert client.get(_path(campaign)).status_code == 403
    _as(GM_A)
    bad = client.post(_path(campaign), content=b"{not json", headers=malformed)
    assert (bad.status_code, bad.json()["detail"]["code"]) == (422, "validation_failed")
    model = client.post(_path(campaign), json={**start, "session_id": MISSING_SESSION})
    assert model.status_code == 422, "a body that fails the model before ownership"
    assert _start(client, campaign).json() == NOT_FOUND, "ownership, in the statement"


def test_a_lock_timeout_on_start_is_a_retryable_503_that_creates_nothing(
    client: TestClient, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = world.campaign()

    def times_out(self: InMemoryTransaction, campaign_id: str, **_: Any) -> None:
        raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    monkeypatch.setattr(InMemoryTransaction, "lock_campaign", times_out)
    answer = _start(client, campaign)
    assert answer.status_code == 503
    assert answer.json()["detail"] == {
        "code": "backend_unavailable",
        "message": table_session_api.BUSY_MESSAGE,
        "retryable": True,
    }
    monkeypatch.undo()
    assert world.ledger(campaign) == [] and world.queued() == []
    with world.db.transaction() as unit:
        assert world.sessions.latest_for_campaign(unit, campaign) is None


def test_no_database_is_a_503(client: TestClient, world: _World) -> None:
    app.dependency_overrides[appmod.get_table_sessions] = lambda: None
    campaign = world.campaign()
    for answer in (client.get(_path(campaign)), _start(client, campaign),
                   client.delete(f"{_path(campaign)}/screens/{MISSING_SCREEN}")):
        assert (answer.status_code, answer.json()["detail"]["code"]) == (503, "backend_unavailable")


# ── The Paid check point (L-19) ──────────────────────────────────────────────


def _probe(world: _World, gate: Callable[[SessionData], None], driver: _RecordingDriver | None = None) -> TestClient:
    """The routes on an app of their own, with a gate the test chooses."""
    probe = FastAPI()
    install_workbench(probe)
    lifecycle = world.lifecycle()
    probe.include_router(
        table_session_api.build_router(
            gm_session(require_session), lambda: lifecycle, lambda: cast(JobDriver, driver or world.driver), gate
        )
    )
    probe.dependency_overrides.update(app.dependency_overrides)
    probe.dependency_overrides[table_session_api.get_clock] = lambda: world.now[0]
    return TestClient(probe)


def test_the_paid_check_point_is_called_by_start_alone(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    calls: list[int] = []
    probe = _probe(world, lambda caller: calls.append(caller.user_id))
    session = _session(_start(probe, campaign))["session_id"]
    assert calls == [GM_A]
    screen = _mint(world, campaign)
    probe.get(_path(campaign))
    _session(_rotate(probe, campaign, session))
    probe.delete(f"{_path(campaign)}/screens/{screen}")
    _session(_end(probe, campaign, session))
    assert calls == [GM_A], "End, Rotate, the status read and a screen revoke are never gated on a tier"


def test_a_refusing_check_point_leaves_the_database_untouched(client: TestClient, world: _World) -> None:
    campaign = world.campaign()

    def refuse(caller: SessionData) -> None:
        raise HTTPException(status_code=402, detail="a paid feature")

    before = world.counting.opened
    answer = _start(_probe(world, refuse), campaign)
    assert answer.status_code == 402
    assert world.counting.opened == before, "the gate runs before any database access"
    assert world.ledger(campaign) == [] and world.queued() == []


def test_the_check_point_has_exactly_one_call_site() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "start_gate"
    ]
    assert len(calls) == 1
    app_source = Path(appmod.__file__).read_text(encoding="utf-8")
    assert app_source.count("start_gate(") == 1, "defined in service/app.py, and called there by nobody"


# ── The reconciliation never delays the answer (RQ-5, AC-B2) ─────────────────


class _SendRecorder:
    """An ASGI wrapper that records each body message the app sends."""

    def __init__(self, inner: Any, log: list[Any]) -> None:
        self._inner = inner
        self._log = log

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def recording(message: Any) -> None:
            if message["type"] == "http.response.body":
                self._log.append(("body", bool(message.get("more_body", False))))
            await send(message)

        await self._inner(scope, receive, recording)


@pytest.mark.parametrize("action", ["end", "rotate"])
def test_the_reconciliation_is_attempted_only_after_the_answers_last_byte(world: _World, action: str) -> None:
    log: list[Any] = []
    driver = _RecordingDriver(log)
    _as(GM_A)
    campaign = world.campaign()
    probe_client = _probe(world, lambda caller: None, driver)
    session = _session(_start(probe_client, campaign))["session_id"]
    recorded = TestClient(_SendRecorder(probe_client.app, log))
    answer = (_end if action == "end" else _rotate)(recorded, campaign, session)
    assert answer.status_code == 200
    assert log == [("body", False), ("job", driver.ran[0])], log
    assert source_uses_run_after_response_only()


def source_uses_run_after_response_only() -> bool:
    text = SOURCE.read_text(encoding="utf-8")
    return "run_after_response(" in text and "run_after_commit" not in text

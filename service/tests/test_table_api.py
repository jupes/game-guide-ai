"""The table routes: screen mode over HTTP (bead 1kg.2.3's PR-B,
agent-forge-harness-1kg.2.10), through the real app over the in-memory twin,
signed in for real.

The mint calls the application's `require_session` directly — a live screen
grant decides without it (SEC-44) — so a `dependency_overrides` entry cannot
reach it: every account here holds a real signed cookie, read by the real
check against an in-memory auth store.

What a client observes: who may mint and the one `inactive` for everyone else
(SEC-46, §15.6, DV-2); the mint's answer, which signs the account out and sets
the grant in the same response (SEC-48, T-22's server half); one principal per
request (SEC-44); which answers delete a grant that is no longer live and which
cannot (L-17); Leave, which is never a 401 (SEC-49); Fetch Metadata before any
cookie or row (SEC-45, T-19); the headers every table answer carries (L-20);
the secret's one way out (T-4); and the import boundary (T-23).
"""

from __future__ import annotations

import ast
import logging
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response

import config
from service import app as appmod
from service import ratelimit, security_headers, table_api, table_session_api
from service.app import app, get_auth_store
from service.audit_log import InMemoryAuditLog
from service.auth_store import InMemoryAuthStore, User
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase
from service.document_store import InMemoryDocumentStore
from service.invites import Role
from service.jobs import InMemoryJobQueue
from service.participant_store import InMemoryParticipantStore
from service.ratelimit import SlidingWindowLimiter
from service.reconciliation import enqueue_reconciliation
from service.reveal_scope import ParticipantsAudience, TableAudience
from service.reveal_store import InMemoryRevealStore
from service.reveals import DisplayCommand, Reveals, slot_clear_for
from service.session import SessionData, encode_session
from service.table_reads import TableReads
from service.table_session_store import SCREENS_PER_SESSION, InMemoryTableSessionStore, no_slots
from service.table_sessions import TableSessions
from service.workbench_api import CROSS_SITE_DETAIL, FORBIDDEN_ORIGIN_DETAIL, INACTIVE_DETAIL, api_routes
from service.workbench_contracts import Author, DocumentTypeId, TableSnapshot

pytestmark = pytest.mark.real_auth

GM_A, GM_B, PLAYER, BYSTANDER = 1, 2, 3, 4
SECRET = "table-api-test-secret-long-enough-for-the-floor"
T0 = datetime(2026, 9, 1, 19, 0, tzinfo=UTC)
INACTIVE = {"detail": dict(INACTIVE_DETAIL)}
UNAUTHENTICATED = {"detail": "not signed in"}
MINT, LEAVE, SNAPSHOT = "/table/screen", "/table/leave", "/table/snapshot"
LEAVE_BODY = {"schema_version": 1}
SOURCE = Path(table_api.__file__)
SERVICE = SOURCE.parent


class _Counting:
    """The twin's database, counting the transactions it opens."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db
        self.opened = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self.opened += 1
        with self._db.transaction() as unit:
            yield unit


class _CountingAuth(InMemoryAuthStore):
    """The auth store, counting account reads: `require_session` makes one."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get_user_by_id(self, user_id: int) -> User | None:
        self.reads += 1
        return super().get_user_by_id(user_id)


@dataclass
class _World:
    db: InMemoryDatabase
    counting: _Counting
    campaigns: InMemoryCampaignStore
    sessions: InMemoryTableSessionStore
    audit: InMemoryAuditLog
    jobs: InMemoryJobQueue
    auth: _CountingAuth
    lifecycle: TableSessions
    now: list[datetime] = field(default_factory=lambda: [T0])

    def campaign(self, owner: int = GM_A, name: str = "Nocturne") -> str:
        with self.db.transaction() as unit:
            return self.campaigns.create(unit, owner_id=owner, name=name, now=self.now[0]).id

    def start(self, campaign: str, owner: int = GM_A) -> str:
        return self.lifecycle.start(owner, campaign, command_id=secrets.token_urlsafe(16), now=self.now[0]).session.id

    def live(self, owner: int = GM_A, name: str = "Nocturne") -> tuple[str, str]:
        campaign = self.campaign(owner, name)
        return campaign, self.start(campaign, owner)

    def grant(self, campaign: str, owner: int = GM_A) -> str:
        return self.lifecycle.mint_screen(owner, campaign, now=self.now[0]).secret

    def ledger(self, campaign: str) -> list[tuple[str, str | None]]:
        with self.db.transaction() as unit:
            return [(event.action, event.reason_code) for event in self.audit.for_campaign(unit, campaign)]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    monkeypatch.setattr(config, "SESSION_SECRET", SECRET)
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", True)
    db = InMemoryDatabase()
    counting = _Counting(db)
    campaigns = InMemoryCampaignStore(db)
    sessions = InMemoryTableSessionStore(db, slot_clear=no_slots)
    audit = InMemoryAuditLog()
    jobs = InMemoryJobQueue(db=db)
    auth = _CountingAuth()
    roles: tuple[tuple[int, Role], ...] = ((GM_A, "dm"), (GM_B, "dm"), (PLAYER, "player"), (BYSTANDER, "dm"))
    for user_id, role in roles:
        auth._users.append(User(id=user_id, email=f"u{user_id}@example.com", role=role, created_at=T0))
    now = [T0]
    lifecycle = TableSessions(
        counting,
        campaigns=campaigns,
        sessions=sessions,
        audit=audit,
        jobs=jobs,
        reconcile=lambda unit, campaign_id: enqueue_reconciliation(unit, jobs, campaign_id),
        clock=lambda: now[0],
    )
    made = _World(db, counting, campaigns, sessions, audit, jobs, auth, lifecycle, now)
    app.dependency_overrides[get_auth_store] = lambda: made.auth
    app.dependency_overrides[appmod.get_table_sessions] = lambda: made.lifecycle
    app.dependency_overrides[table_api.get_clock] = lambda: made.now[0]
    app.dependency_overrides[table_session_api.get_clock] = lambda: made.now[0]
    yield made
    for dependency in (get_auth_store, appmod.get_table_sessions, table_api.get_clock, table_session_api.get_clock):
        app.dependency_overrides.pop(dependency, None)


def _account(user_id: int) -> str:
    role: Role = "player" if user_id == PLAYER else "dm"
    return encode_session(SessionData(user_id=user_id, role=role), SECRET)


def _post(
    path: str,
    body: Any = None,
    *,
    account: int | str | None = None,
    grant: str | None = None,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> Response:
    """One request from a browser holding exactly these cookies. A fresh
    client each time, so no cookie jar carries anything between requests."""
    cookies = []
    if account is not None:
        value = account if isinstance(account, str) else _account(account)
        cookies.append(f"{config.SESSION_COOKIE_NAME}={value}")
    if grant is not None:
        cookies.append(f"{table_api.SCREEN_COOKIE}={grant}")
    sent = dict(headers or {})
    if cookies:
        sent["cookie"] = "; ".join(cookies)
    client = TestClient(app)
    if content is not None:
        return client.post(path, content=content, headers={"content-type": "application/json", **sent})
    return client.post(path, json=body, headers=sent)


def _mint(campaign: str, **kwargs: Any) -> Response:
    return _post(MINT, {"schema_version": 1, "campaign_id": campaign}, **kwargs)


def _cookies(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _named(response: Response, name: str) -> list[str]:
    return [cookie for cookie in _cookies(response) if cookie.startswith(f"{name}=")]


def _attributes(cookie: str) -> set[str]:
    return {part.strip().lower() for part in cookie.split(";")[1:]}


def _deletes_the_grant(response: Response) -> bool:
    found = _named(response, table_api.SCREEN_COOKIE)
    return len(found) == 1 and {"max-age=0", "path=/table"} <= _attributes(found[0])


def _shape(response: Response) -> tuple[Any, ...]:
    varying = {"date", "content-length", "server", "x-request-id"}
    return (
        response.status_code,
        response.content,
        tuple(sorted((k.lower(), v) for k, v in response.headers.items() if k.lower() not in varying)),
    )


def _stale(world: _World, campaign: str) -> str:
    """A grant that is no longer live: minted, then left."""
    secret = world.grant(campaign)
    assert world.lifecycle.leave(secret, now=world.now[0])
    return secret


# ── The mint (SEC-48, D-13; T-22's server half) ─────────────────────────────


@pytest.mark.parametrize("secure", [True, False])
def test_the_owner_mints_and_the_same_answer_signs_the_account_out(
    world: _World, monkeypatch: pytest.MonkeyPatch, secure: bool
) -> None:
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", secure)
    campaign, _ = world.live()
    world.now[0] = T0 + timedelta(hours=1)
    answer = _mint(campaign, account=GM_A)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"schema_version": 1, "ends_at": "2026-09-02T07:00:00Z"}

    grants = _named(answer, table_api.SCREEN_COOKIE)
    assert len(grants) == 1
    secret = grants[0].split(";")[0].removeprefix(f"{table_api.SCREEN_COOKIE}=")
    expected = {"httponly", "max-age=39600", "path=/table", "samesite=strict"} | ({"secure"} if secure else set())
    assert _attributes(grants[0]) == expected, "host-only: no Domain; Max-Age is the session's remaining time"
    signed_out = _named(answer, config.SESSION_COOKIE_NAME)
    assert len(signed_out) == 1 and {"max-age=0", "path=/"} <= _attributes(signed_out[0])
    assert len(_cookies(answer)) == 2
    assert answer.headers.get_list("clear-site-data") == ['"cache", "storage"']
    assert answer.headers.get_list("cache-control") == ["no-store"]

    live = world.lifecycle.resolve_screen(secret, now=world.now[0])
    assert live is not None and live.campaign_id == campaign
    assert secret not in answer.text
    assert world.ledger(campaign)[-1] == ("screen.minted", None)


def test_only_the_owner_of_a_live_session_mints_and_everyone_else_is_one_inactive(world: _World) -> None:
    ended = world.campaign(name="Ended")
    world.lifecycle.end(GM_A, ended, world.start(ended), now=world.now[0])
    campaign, _ = world.live()
    theirs, _ = world.live(GM_B, "Theirs")
    with world.db.transaction() as unit:
        seats = InMemoryParticipantStore(world.db)
        seat = seats.add(unit, campaign, alias="Rook", now=world.now[0]).id
        seats.offer(unit, campaign, seat, user_id=PLAYER)
        seats.accept(unit, campaign, seat, user_id=PLAYER, now=world.now[0])
        seats.confirm(unit, campaign, seat, now=world.now[0])

    refusals = {
        "a seated, confirmed player": _mint(campaign, account=PLAYER),
        "an account with no campaign": _mint(campaign, account=BYSTANDER),
        "the GM of another live campaign": _mint(campaign, account=GM_B),
        "the owner, of a campaign whose session ended": _mint(ended, account=GM_A),
        "the owner, naming a missing campaign": _mint("cmp_" + "z" * 22, account=GM_A),
        "the owner, naming another GM's live campaign": _mint(theirs, account=GM_A),
        "the owner, naming an id of another kind": _mint("doc_" + "z" * 22, account=GM_A),
    }
    shapes = {name: _shape(answer) for name, answer in refusals.items()}
    assert len(set(shapes.values())) == 1, shapes
    one = next(iter(refusals.values()))
    assert (one.status_code, one.json()) == (404, INACTIVE)
    assert _cookies(one) == [], "nobody is signed out by a refusal"
    assert [event for event, _ in world.ledger(campaign)] == ["session.started"]

    nobody = _mint(campaign)
    assert (nobody.status_code, nobody.json()) == (401, UNAUTHENTICATED)
    assert _cookies(nobody) == []


def test_a_live_screen_is_judged_alone_and_cannot_mint(world: _World) -> None:
    """SEC-44(3): a live grant decides, and the account session in the same
    browser is not consulted — not even read. A screen asking to mint is
    `inactive` (DV-2), and with no account cookie it is still not a 401."""
    campaign, _ = world.live()
    live = world.grant(campaign)
    reads = world.auth.reads
    alone = _mint(campaign, grant=live)
    beside_the_owner = _mint(campaign, grant=live, account=GM_A)
    assert _shape(alone) == _shape(beside_the_owner)
    assert (alone.status_code, alone.json()) == (404, INACTIVE)
    assert world.auth.reads == reads, "require_session was not called"
    assert _cookies(beside_the_owner) == [], "a live grant is not deleted, and the account is not signed out"


def test_at_the_bound_the_mint_is_screen_limit_and_the_account_stays_signed_in(world: _World) -> None:
    campaign, _ = world.live()
    stale = _stale(world, campaign)
    for _ in range(SCREENS_PER_SESSION):
        world.grant(campaign)
    refused = _mint(campaign, account=GM_A)
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "code": "screen_limit",
        "message": table_api.SCREEN_LIMIT_MESSAGE,
        "retryable": False,
    }
    assert _cookies(refused) == [], "the account cookie is kept"
    with_stale = _mint(campaign, account=GM_A, grant=stale)
    assert with_stale.status_code == 409
    assert _deletes_the_grant(with_stale) and len(_cookies(with_stale)) == 1


def test_a_mint_that_replaces_a_stale_grant_sends_one_grant_cookie(world: _World) -> None:
    campaign, _ = world.live()
    stale = _stale(world, campaign)
    answer = _mint(campaign, account=GM_A, grant=stale)
    assert answer.status_code == 200
    grants = _named(answer, table_api.SCREEN_COOKIE)
    assert len(grants) == 1 and "max-age=0" not in _attributes(grants[0]), "the new grant, and no deletion beside it"


# ── Which answers delete a grant that is no longer live (SEC-44, L-17) ───────


def _stale_kinds(world: _World, campaign: str, session: str) -> dict[str, str]:
    left = _stale(world, campaign)
    rotated_out = world.grant(campaign)
    world.lifecycle.rotate(GM_A, campaign, session, command_id=secrets.token_urlsafe(16), now=world.now[0])
    return {
        "left": left,
        "of a retired generation": rotated_out,
        "unknown": secrets.token_urlsafe(32),
        "not a grant at all": "garbage",
    }


def test_every_answer_from_the_principal_step_on_deletes_a_stale_grant(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign, session = world.live()
    for kind, stale in _stale_kinds(world, campaign, session).items():
        answers = {
            "the mint's 401": _mint(campaign, grant=stale),
            "inactive": _mint(campaign, grant=stale, account=BYSTANDER),
            "Leave's 204": _post(LEAVE, LEAVE_BODY, grant=stale),
        }
        assert answers["the mint's 401"].json() == UNAUTHENTICATED, kind
        assert answers["inactive"].json() == INACTIVE, kind
        assert answers["Leave's 204"].status_code == 204, kind
        assert "clear-site-data" not in answers["Leave's 204"].headers, (kind, "only a live grant's Leave clears")
        for name, answer in answers.items():
            assert _deletes_the_grant(answer), (kind, name)
            assert _named(answer, config.SESSION_COOKIE_NAME) == [], (kind, name, "no account is signed out")

    stale = secrets.token_urlsafe(32)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("the server closed the connection")

    monkeypatch.setattr(world.lifecycle, "mint_screen", unavailable)
    outage = _mint(campaign, grant=stale, account=GM_A)
    assert (outage.status_code, outage.json()["detail"]["code"]) == (503, "backend_unavailable")
    assert _deletes_the_grant(outage), "a 503 after the principal step deletes it too"


def test_the_answers_before_the_principal_step_do_not_delete_it(world: _World) -> None:
    """Pinned so the boundary cannot move silently: Fetch Metadata and the
    origin check run before any cookie is read, and a 422 is built by the
    application's one validation handler — for a malformed body and for one
    that fails the model alike. The next request deletes the grant."""
    campaign, _ = world.live()
    stale = secrets.token_urlsafe(32)
    answers = {
        "cross_site": _mint(campaign, grant=stale, account=GM_A, headers={"sec-fetch-site": "cross-site"}),
        "the origin 403": _mint(campaign, grant=stale, account=GM_A, headers={"origin": "https://evil.example"}),
        "a malformed body": _post(MINT, grant=stale, account=GM_A, content=b"{nope"),
        "a body that fails the model": _post(MINT, {"schema_version": 1}, grant=stale, account=GM_A),
        "Leave with a body that fails the model": _post(LEAVE, {"schema_version": 1, "x": 1}, grant=stale),
    }
    assert {name: answer.status_code for name, answer in answers.items()} == {
        "cross_site": 403, "the origin 403": 403, "a malformed body": 422, "a body that fails the model": 422,
        "Leave with a body that fails the model": 422,
    }
    assert {name: _cookies(answer) for name, answer in answers.items()} == {name: [] for name in answers}


@pytest.mark.parametrize("revocation", ["end", "rotate", "expiry", "a per-screen revoke", "leave"])
def test_each_revocation_leaves_the_grant_dead(world: _World, revocation: str) -> None:
    """A live grant's Leave answers `Clear-Site-Data`; a dead one's does not —
    which is how a client sees, through the route, that the grant died."""
    campaign, session = world.live()
    secret = world.grant(campaign)
    if revocation == "end":
        world.lifecycle.end(GM_A, campaign, session, now=world.now[0])
    elif revocation == "rotate":
        world.lifecycle.rotate(GM_A, campaign, session, command_id=secrets.token_urlsafe(16), now=world.now[0])
    elif revocation == "expiry":
        world.now[0] = T0 + timedelta(hours=12, seconds=1)
        world.lifecycle.expire(campaign, session, now=world.now[0])
    elif revocation == "a per-screen revoke":
        screen = world.lifecycle.resolve_screen(secret, now=world.now[0])
        assert screen is not None
        world.lifecycle.revoke_screen(GM_A, campaign, screen.grant_id, now=world.now[0])
    else:
        first = _post(LEAVE, LEAVE_BODY, grant=secret)
        assert first.headers.get_list("clear-site-data") == ['"cache", "storage"']
        assert _deletes_the_grant(first)
    dead = _post(LEAVE, LEAVE_BODY, grant=secret)
    assert dead.status_code == 204
    assert "clear-site-data" not in dead.headers
    assert _deletes_the_grant(dead)
    assert world.lifecycle.resolve_screen(secret, now=world.now[0]) is None


# ── Leave (SEC-49) ───────────────────────────────────────────────────────────


def test_leave_is_never_a_401_and_never_signs_an_account_out(world: _World) -> None:
    campaign, _ = world.live()
    for cookies in ({}, {"account": GM_A}, {"account": "not-a-session"}):
        answer = _post(LEAVE, LEAVE_BODY, **cookies)
        assert (answer.status_code, answer.content, _cookies(answer)) == (204, b"", []), cookies
        assert "clear-site-data" not in answer.headers

    live = world.grant(campaign)
    left = _post(LEAVE, LEAVE_BODY, grant=live, account=GM_A)
    assert left.status_code == 204
    assert left.headers.get_list("clear-site-data") == ['"cache", "storage"']
    assert _deletes_the_grant(left) and _named(left, config.SESSION_COOKIE_NAME) == []
    assert world.ledger(campaign)[-1] == ("screen.revoked", "left")
    assert world.lifecycle.resolve_screen(live, now=world.now[0]) is None


# ── The digest lookup's source budget (release review S6, 6621) ──────────────

SOURCE_A, SOURCE_B = {"x-forwarded-for": "203.0.113.7"}, {"x-forwarded-for": "203.0.113.8"}


@pytest.fixture
def budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source budget of two in place of the auth one, keyed on the address
    one trusted hop appended, so two sources can be told apart."""
    monkeypatch.setattr(ratelimit, "source_limiter", ratelimit.SlidingWindowLimiter(limit=2, window_seconds=60))
    monkeypatch.setattr(config, "AUTH_TRUSTED_PROXY_HOPS", 1)


def test_past_the_source_budget_a_grant_shaped_cookie_is_429_and_nothing_is_looked_up(
    world: _World, budget: None
) -> None:
    campaign, _ = world.live()
    stale, live = secrets.token_urlsafe(32), world.grant(campaign)
    within = [_post(LEAVE, LEAVE_BODY, grant=stale, headers=SOURCE_A),
              _mint(campaign, grant=stale, account=BYSTANDER, headers=SOURCE_A)]
    assert [answer.status_code for answer in within] == [204, 404]
    assert (within[0].content, within[1].json()) == (b"", INACTIVE)
    assert all(_deletes_the_grant(answer) for answer in within), "under the budget, the answers are unchanged"

    opened, reads = world.counting.opened, world.auth.reads
    refused = {
        "leave": _post(LEAVE, LEAVE_BODY, grant=live, headers=SOURCE_A),
        "mint": _mint(campaign, grant=live, account=GM_A, headers=SOURCE_A),
    }
    assert (world.counting.opened, world.auth.reads) == (opened, reads), "no row read, no account read"
    for name, answer in refused.items():
        detail = answer.json()["detail"]
        assert (answer.status_code, detail["code"], detail["retryable"]) == (429, "throttled_user", True), name
        assert answer.headers.get_list("retry-after") == [str(detail["retry_after_s"])], name
        assert _cookies(answer) == [], (name, "a throttled request deletes nothing and signs nothing out")
        assert answer.headers.get_list("cache-control") == ["no-store"], name

    other = _post(LEAVE, LEAVE_BODY, grant=live, headers=SOURCE_B)
    assert other.headers.get_list("clear-site-data") == ['"cache", "storage"'], "another source has its own budget"


def test_no_cookie_and_a_cookie_that_is_not_a_grant_spend_nothing(world: _World, budget: None) -> None:
    for grant in (None, "garbage", None, "garbage"):
        assert _post(LEAVE, LEAVE_BODY, grant=grant, headers=SOURCE_A).status_code == 204, grant
    stale = secrets.token_urlsafe(32)
    answers = [_post(LEAVE, LEAVE_BODY, grant=stale, headers=SOURCE_A).status_code for _ in range(3)]
    assert answers == [204, 204, 429], "the whole budget of two was still there"


# ── Fetch Metadata and the origin check (SEC-45, SEC-7; T-19) ────────────────


@pytest.mark.parametrize("path", [MINT, LEAVE])
@pytest.mark.parametrize(
    "metadata",
    [
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
        {"sec-fetch-site": "none"},
        {"sec-fetch-site": "same-origin", "sec-fetch-mode": "navigate"},
        {"sec-fetch-mode": "navigate"},
    ],
)
def test_fetch_metadata_is_refused_before_any_cookie_or_row(
    world: _World, path: str, metadata: dict[str, str]
) -> None:
    campaign, _ = world.live()
    live = world.grant(campaign)
    opened, reads = world.counting.opened, world.auth.reads
    body = {"schema_version": 1, "campaign_id": campaign} if path == MINT else LEAVE_BODY
    answer = _post(path, body, grant=live, account=GM_A, headers=metadata)
    assert (answer.status_code, answer.json()) == (403, {"detail": dict(CROSS_SITE_DETAIL)})
    assert (world.counting.opened, world.auth.reads) == (opened, reads), "no row read, no account read"
    assert world.lifecycle.resolve_screen(live, now=world.now[0]) is not None


def test_without_fetch_metadata_the_origin_check_judges(world: _World) -> None:
    campaign, _ = world.live()
    foreign = _mint(campaign, account=GM_A, headers={"origin": "https://evil.example"})
    assert (foreign.status_code, foreign.json()) == (403, {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)})
    own = {"origin": "http://testserver", "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}
    assert _mint(campaign, account=GM_A, headers=own).status_code == 200
    assert _post(LEAVE, LEAVE_BODY).status_code == 204, "no browser headers at all: not a browser, allowed"


# ── Headers every table answer carries (L-20, TA-6) ──────────────────────────


def test_every_table_answer_carries_no_store_and_corp_once_and_each_app_header_once(world: _World) -> None:
    campaign, _ = world.live()
    stale = secrets.token_urlsafe(32)
    answers = {
        "mint 200": _mint(campaign, account=GM_A),
        "mint 401": _mint(campaign, grant=stale),
        "mint inactive": _mint(campaign, account=BYSTANDER),
        "mint 422": _post(MINT, {"schema_version": 1}, account=GM_A),
        "cross_site": _mint(campaign, account=GM_A, headers={"sec-fetch-site": "cross-site"}),
        "origin 403": _mint(campaign, account=GM_A, headers={"origin": "https://evil.example"}),
        "leave 204": _post(LEAVE, LEAVE_BODY, grant=stale),
    }
    app_headers = {
        "content-security-policy": security_headers.CONTENT_SECURITY_POLICY,
        "x-content-type-options": security_headers.X_CONTENT_TYPE_OPTIONS,
        "referrer-policy": security_headers.REFERRER_POLICY,
        "cross-origin-opener-policy": security_headers.CROSS_ORIGIN_OPENER_POLICY,
        "permissions-policy": security_headers.PERMISSIONS_POLICY,
    }
    for name, answer in answers.items():
        assert answer.headers.get_list("cache-control") == ["no-store"], name
        assert answer.headers.get_list("cross-origin-resource-policy") == ["same-origin"], name
        for header, value in app_headers.items():
            assert answer.headers.get_list(header) == [value], (name, header)


def test_a_gm_route_answer_carries_no_table_header(world: _World) -> None:
    """The route class adds them, so they stay on table routes: CORP for the
    non-table pages is undecided (`ifq`)."""
    answer = TestClient(app).get("/campaigns")
    assert answer.status_code == 401
    assert "cross-origin-resource-policy" not in answer.headers


# ── The grant reaches no GM or account route (SEC-44(4)) ─────────────────────


def test_a_grant_alone_is_no_account_on_a_gm_or_account_route(world: _World) -> None:
    campaign, _ = world.live()
    live = world.grant(campaign)
    client = TestClient(app)
    cookie = {"cookie": f"{table_api.SCREEN_COOKIE}={live}"}
    assert client.get("/campaigns", headers=cookie).status_code == 401
    assert client.get(f"/campaigns/{campaign}/table-session", headers=cookie).status_code == 401
    assert client.get("/auth/me", headers=cookie).status_code == 401


# ── The secret's one way out (T-4, SEC-5, SEC-20) ────────────────────────────


def test_the_grant_leaves_in_its_cookie_and_nowhere_else(world: _World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    campaign, _ = world.live()
    minted = _mint(campaign, account=GM_A)
    secret = _named(minted, table_api.SCREEN_COOKIE)[0].split(";")[0].split("=", 1)[1]
    digest = sha256(secret.encode()).hexdigest()
    gm = TestClient(app).get(
        f"/campaigns/{campaign}/table-session",
        headers={"cookie": f"{config.SESSION_COOKIE_NAME}={_account(GM_A)}"},
    )
    assert gm.status_code == 200 and len(gm.json()["session"]["screens"]) == 1
    left = _post(LEAVE, LEAVE_BODY, grant=secret)
    for text in (minted.text, gm.text, left.text, caplog.text):
        assert secret not in text and digest not in text
    with world.db.transaction() as unit:
        rows = world.audit.for_campaign(unit, campaign)
    assert all(secret not in repr(row.detail) and digest not in repr(row.detail) for row in rows)


def test_the_minted_secret_is_mentioned_once_in_the_set_cookie_call() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    mentions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "secret"
        and isinstance(node.value, ast.Name) and node.value.id == "minted"
    ]
    assert len(mentions) == 1
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "set_cookie"
    ]
    assert len(calls) == 1 and mentions[0] in calls[0].args


def test_the_cookie_name_is_spelled_in_the_table_module_alone() -> None:
    spelled = sorted(
        path.name for path in SERVICE.glob("*.py") if table_api.SCREEN_COOKIE in path.read_text(encoding="utf-8")
    )
    assert spelled == ["table_api.py"]
    readers = sorted(
        path.name for path in SERVICE.glob("*.py")
        if path.name != "table_api.py" and "SCREEN_COOKIE" in path.read_text(encoding="utf-8")
    )
    assert readers == []


# ── The import boundary (SEC-44(2), T-23) ────────────────────────────────────

#: What a table route module may never import: a GM route module, or a store
#: that holds GM-private content. Content reaches a table only through the
#: projection builder and the slot resolver (SEC-14, SEC-16).
FORBIDDEN_IMPORTS = frozenset({
    "conversations_api", "timeline_api", "table_session_api", "campaigns_api", "documents_api", "seats_api",
    "document_store", "document_wire", "history", "conversation_store", "timeline_store", "timeline",
    "asset_store", "asset_jobs", "media_objects", "attachments", "app",
    # 1kg.7.2 PR-2, T-23, C-6(c): stored text reaches a table route through
    # `table_reads` alone, so the route module names no reveal service or store
    # and no projection builder either.
    "reveals", "reveal_store", "table_projection",
    # T-23, SEC-44 (1kg.4.3 I-27): a table route acts with table authority
    # only, so it may never reach the Workbench tool modules either.
    "tool_invocations", "tool_invocations_api", "document_generation", "document_tools",
    "card_generation", "card_executors",
})


def _imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.rsplit(".", 1)[-1])
            names.update(alias.name for alias in node.names)
    return names


def test_the_table_module_imports_no_gm_route_module_and_no_private_store() -> None:
    imported = _imported_modules(SOURCE.read_text(encoding="utf-8"))
    assert imported & FORBIDDEN_IMPORTS == set()
    assert {"table_sessions", "workbench_api", "table_reads"} <= imported, "the check reads the imports it should"


@pytest.mark.parametrize(
    "added",
    [
        "from .conversations_api import read_body",
        "from . import timeline_api",
        "import service.document_store",
        "from .reveals import Reveals",
        "from . import reveal_store",
    ],
)
def test_the_import_boundary_fails_when_an_import_is_added(added: str) -> None:
    source = SOURCE.read_text(encoding="utf-8").replace("import config\n", f"import config\n{added}\n", 1)
    assert _imported_modules(source) & FORBIDDEN_IMPORTS != set()


# ── The proxies (L-21) ───────────────────────────────────────────────────────

NGINX = SERVICE.parent / "ui" / "nginx.conf"


def test_nginx_names_each_table_route_and_forwards_host_and_source() -> None:
    """`tests/test_proxy_contract.py` asks only for a location that begins with
    `/table`. The three routes are named exactly — a bare `/table` would also
    catch the table page (`1kg.7.4`) and `/table-sessions` — and each forwards
    `Host` (the origin check compares `Origin` with it) and `X-Real-IP`."""
    conf = NGINX.read_text(encoding="utf-8")
    table_paths = sorted({path for path, _ in api_routes(app) if path.startswith("/table/")})
    assert table_paths == [LEAVE, MINT, SNAPSHOT]
    blocks = dict(re.findall(r"location\s+(/table\S*)\s*\{([^}]*)\}", conf))
    assert sorted(blocks) == table_paths, "each table route by its own name, and no bare /table"
    for path, block in blocks.items():
        assert "proxy_set_header   Host $host;" in block, path
        assert "proxy_set_header   X-Real-IP $remote_addr;" in block, path
        assert "proxy_pass         http://service:8000;" in block, path


# ── The snapshot read (1kg.7.2 PR-2; ID-15, SEC-46, SEC-47, T-1, T-23) ───────

SNAPSHOT_AT = "/table/snapshot?campaign_id="
CANARY = {
    "name": "Canary-Name-9f1c",
    "qualifier": "Canary-Qualifier-3a7d",
    "voice": "Canary-Voice-55b0",
    "tags": ["Canary-Tag-c21e"],
}
CONFIRMED, AWAITING = PLAYER, 5


@dataclass
class _Reading:
    world: _World
    campaign: str
    session: str
    ids: set[str]
    grant: str
    grant_id: str


def _get(
    path: str, *, account: int | None = None, grant: str | None = None, headers: dict[str, str] | None = None
) -> Response:
    """One GET from a browser holding exactly these cookies."""
    cookies = []
    if account is not None:
        cookies.append(f"{config.SESSION_COOKIE_NAME}={_account(account)}")
    if grant is not None:
        cookies.append(f"{table_api.SCREEN_COOKIE}={grant}")
    sent = {**(headers or {}), **({"cookie": "; ".join(cookies)} if cookies else {})}
    return TestClient(app).get(path, headers=sent)


@pytest.fixture
def reading(world: _World) -> Iterator[_Reading]:
    """A live table whose table slot shows an NPC's name and whose confirmed seat
    holds a private copy of its voice, an awaiting seat holding a held one."""
    world.auth._users.append(User(id=AWAITING, email="u5@example.com", role="dm", created_at=T0))
    rows = InMemoryRevealStore(world.db)
    documents = InMemoryDocumentStore(world.db)
    seats = InMemoryParticipantStore(world.db)
    served = Reveals(
        world.db,
        campaigns=world.campaigns,
        sessions=InMemoryTableSessionStore(world.db, slot_clear=slot_clear_for(rows)),
        reveals=rows,
        documents=documents,
        audit=world.audit,
    )
    campaign, session = world.live()
    seat_ids = []
    for player, confirm in ((CONFIRMED, True), (AWAITING, False)):
        with world.db.transaction() as unit:
            seat = seats.add(unit, campaign, alias=f"Seat {player}", now=T0).id
            seats.offer(unit, campaign, seat, user_id=player)
            seats.accept(unit, campaign, seat, user_id=player, now=T0)
            if confirm:
                seats.confirm(unit, campaign, seat, now=T0)
        seat_ids.append(str(seat))

    def new_document() -> str:
        with world.db.transaction() as unit:
            made = documents.create(
                unit, campaign, doc_type=DocumentTypeId.NPC, type_version=1, data=dict(CANARY), author=Author.GM
            )
        with world.db.transaction() as unit:
            documents.seal(unit, campaign, made.id)
        return str(made.id)

    shown, private, held = new_document(), new_document(), new_document()
    for n, (document, audience, mask) in enumerate(
        [
            (shown, TableAudience(), ("name",)),
            (private, ParticipantsAudience(frozenset({seat_ids[0]})), ("voice",)),
            (held, ParticipantsAudience(frozenset({seat_ids[1]})), ("voice",)),
        ]
    ):
        with world.db.transaction() as unit:
            found = world.sessions.get(unit, session)
        assert found is not None
        command = DisplayCommand(
            campaign, session, f"cmd_reading_{n:010d}", found.reveal_epoch, document, 1, mask, audience
        )
        served.display(command, owner_id=GM_A, now=T0)
    reads = TableReads(world.counting, rows, documents)
    app.dependency_overrides[appmod.get_table_reads] = lambda: reads
    secret = world.grant(campaign)
    live = world.lifecycle.resolve_screen(secret, now=T0)
    assert live is not None
    ids = {campaign, session, shown, private, held, *seat_ids, secret, live.grant_id}
    yield _Reading(world, campaign, session, ids, secret, live.grant_id)
    app.dependency_overrides.pop(appmod.get_table_reads, None)


def test_every_viewers_bytes_hold_the_masked_text_and_no_other_field_and_no_id(reading: _Reading) -> None:
    """T-1 on the raw response bytes and headers, and T-23's owner row: the
    owner's account on a table route reads the table slot only, as a guest.
    Kills: a projection built from the whole document, a private copy sent to
    anyone but its seat, an id in a body or a header, a missing header."""
    at = SNAPSHOT_AT + reading.campaign
    viewers = {
        "owner": _get(at, account=GM_A),
        "confirmed seat": _get(at, account=CONFIRMED),
        "awaiting seat": _get(at, account=AWAITING),
        "screen": _get(at, grant=reading.grant),
    }
    for name, answer in viewers.items():
        assert answer.status_code == 200, (name, answer.text)
        assert answer.headers["cache-control"] == "no-store"
        assert answer.headers["cross-origin-resource-policy"] == "same-origin"
        assert answer.headers["content-type"].startswith("application/json")
        TableSnapshot.model_validate_json(answer.content)
        raw = answer.content.decode()
        assert CANARY["name"] in raw, name
        assert "Canary-Qualifier" not in raw and "Canary-Tag" not in raw, name
        assert ("Canary-Voice" in raw) == (name == "confirmed seat"), name
        assert not any(i in raw or i in str(answer.headers) for i in reading.ids), name
        for key in ("session_id", "document_id", "participant_id", "disclosure_id", "epoch"):
            assert key not in raw, (name, key)
    assert '"role":"participant"' in viewers["confirmed seat"].text.replace(" ", "")
    assert '"role":"guest"' in viewers["owner"].text.replace(" ", "")


def test_everyone_not_entitled_gets_the_one_inactive(reading: _Reading) -> None:
    """SEC-46: a stranger, another GM, no live session, a missing campaign, an id
    of another kind, a missing or empty query: one body, one set of headers, and
    no cookie deleted or set. Kills: a distinct answer for any of them."""
    world = reading.world
    ended = world.campaign(GM_B, "Ended")
    world.lifecycle.end(GM_B, ended, world.start(ended, GM_B), now=T0)
    refusals = {
        "a stranger": _get(SNAPSHOT_AT + reading.campaign, account=BYSTANDER),
        "another GM": _get(SNAPSHOT_AT + reading.campaign, account=GM_B),
        "no live session": _get(SNAPSHOT_AT + ended, account=GM_B),
        "a missing campaign": _get(SNAPSHOT_AT + "cmp_" + "z" * 22, account=GM_A),
        "another kind of id": _get(SNAPSHOT_AT + "doc_" + "z" * 22, account=GM_A),
        "no query": _get("/table/snapshot", account=GM_A),
        "an empty query": _get(SNAPSHOT_AT, account=GM_A),
    }
    shapes = {name: _shape(answer) for name, answer in refusals.items()}
    assert len(set(shapes.values())) == 1, shapes
    one = refusals["a stranger"]
    assert (one.status_code, one.json()) == (404, INACTIVE) and _cookies(one) == []
    signed_out = _get(SNAPSHOT_AT + reading.campaign)
    assert (signed_out.status_code, signed_out.json()) == (401, UNAUTHENTICATED)


def test_the_order_of_checks_opens_no_transaction_before_the_id_shape(reading: _Reading) -> None:
    """Fetch Metadata, then the principal, then the id shape, then the budget:
    a cross-site read is the 403 and a malformed id the 404, and neither opens a
    transaction. Kills: the id shape checked after the read."""
    world = reading.world
    before = world.counting.opened
    cross = _get(SNAPSHOT_AT + reading.campaign, account=GM_A, headers={"sec-fetch-site": "cross-site"})
    assert (cross.status_code, cross.json()["detail"]["code"]) == (403, "cross_site")
    assert _get(SNAPSHOT_AT + "not-an-id", account=GM_A).status_code == 404
    assert _get(SNAPSHOT_AT + "cmp_" + "a" * 21 + "!", account=GM_A).status_code == 404
    assert world.counting.opened == before
    assert _get(SNAPSHOT_AT + reading.campaign, account=GM_A).status_code == 200
    assert world.counting.opened == before + 1, "one transaction per entitled read"


def test_the_read_budget_is_keyed_by_the_principal_and_never_by_the_source(
    reading: _Reading, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-47(1). Two accounts and a screen behind one source spend their own
    budgets: the keys are `account:<id>` and `screen:<grant>`, and past its bound
    one principal is the 429 while the others still read. Kills: keying by
    `client_source`; one shared budget."""
    seen: list[str] = []
    real = ratelimit.check_table_read

    def spy(key: str) -> None:
        seen.append(key)
        real(key)

    monkeypatch.setattr(ratelimit, "check_table_read", spy)
    monkeypatch.setattr(ratelimit, "table_read_limiter", SlidingWindowLimiter(2, 3600))
    at = SNAPSHOT_AT + reading.campaign
    assert [_get(at, account=GM_A).status_code for _ in range(2)] == [200, 200]
    spent = reading.world.counting.opened
    refused = _get(at, account=GM_A)
    assert refused.status_code == 429 and refused.json()["detail"]["code"] == "throttled_user"
    assert int(refused.headers["retry-after"]) >= 1
    assert reading.world.counting.opened == spent, "a throttled read opens no transaction"
    assert _get(at, account=CONFIRMED).status_code == 200
    assert _get(at, grant=reading.grant).status_code == 200
    assert seen == [f"account:{GM_A}"] * 3 + [f"account:{CONFIRMED}", f"screen:{reading.grant_id}"]


def test_a_refusal_is_counted_against_the_source_and_never_an_entitled_read(
    reading: _Reading, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-47(2): only a request that failed spends the source's refusal budget,
    after the lookup; past it the next would-be refusal is a 429, and an entitled
    principal on the same source still reads. Kills: counting before the lookup."""
    monkeypatch.setattr(ratelimit, "source_limiter", SlidingWindowLimiter(2, 3600))
    at = SNAPSHOT_AT + reading.campaign
    assert [_get(at, account=GM_A).status_code for _ in range(4)] == [200] * 4
    assert [_get(at, account=BYSTANDER).status_code for _ in range(3)] == [404, 404, 429]
    assert _get(at, account=GM_A).status_code == 200


def test_a_degraded_instance_and_a_database_error_are_a_retryable_503_that_names_no_text(
    reading: _Reading, caplog: pytest.LogCaptureFixture
) -> None:
    """Kills: a 500; a log line that carries the driver's message."""
    at = SNAPSHOT_AT + reading.campaign
    app.dependency_overrides[appmod.get_table_reads] = lambda: None
    degraded = _get(at, account=GM_A)
    assert (degraded.status_code, degraded.json()["detail"]["code"]) == (503, "backend_unavailable")

    class Failing:
        def snapshot(self, *args: Any, **kwargs: Any) -> Any:
            raise psycopg.OperationalError("the row says Canary-Name-9f1c")

    app.dependency_overrides[appmod.get_table_reads] = lambda: Failing()
    with caplog.at_level(logging.DEBUG):
        failed = _get(at, account=GM_A)
    assert (failed.status_code, failed.json()["detail"]["retryable"]) == (503, True)
    assert "OperationalError" in caplog.text and "Canary" not in caplog.text + failed.text

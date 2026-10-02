"""The Workbench write throttle (agent-forge-harness-531x, PR-A).

Workbench writes (campaigns, documents, versions, timeline, groups, tool
results) had no throttle at all: one script could fill the Cloud SQL disk
(auto-increase never shrinks). This is the write-COUNT half of the fix — a
per-account sliding-window budget on every state-changing Workbench route,
answering the existing `throttled_user` 429 shape. The storage-BYTE caps are a
separate change (PR-B).

Two kinds of test:

- **Structural** (no HTTP): the pin walks every `WorkbenchRoute` on the real
  app and proves every state-changing one carries the throttle, except the two
  named exemptions — so a new route that forgets it fails CI, and a stale
  exemption (one that turns out to write after all) fails it too.
- **Behavioural** (`TestClient`, over the twin): a small monkeypatched limiter
  proves the budget is spent, shared, refused and reported correctly.

Run from the repo root:
    uv run python -m pytest service/tests/test_workbench_write_throttle.py -q
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response

import config
from service import campaigns_api, documents_api, ratelimit
from service.app import app, get_auth_store, get_reveals, get_timeline_database, require_session
from service.audit_log import InMemoryAuditLog
from service.auth_store import InMemoryAuthStore, User
from service.campaign_store import InMemoryCampaignStore
from service.campaign_summary_store import InMemoryCampaignSummaryStore
from service.db import InMemoryDatabase
from service.document_store import InMemoryDocumentStore
from service.hashing import hash_password
from service.history import InMemoryMessageStore
from service.participant_store import InMemoryParticipantStore
from service.ratelimit import RateLimited, SlidingWindowLimiter
from service.reveals import Reveals
from service.seat_offer_store import InMemorySeatOfferStore
from service.session import SessionData, encode_session
from service.table_session_store import InMemoryTableSessionStore, no_slots
from service.workbench_api import (
    NARROWING_THROTTLED_MESSAGE,
    WRITE_THROTTLED_MESSAGE,
    WorkbenchRoute,
    api_route_dependants,
    is_narrowing_throttle,
    is_reads_only,
    is_write_throttle,
)
from service.workbench_contracts import ErrorCode

GM_A, GM_B = 1, 2
PASSWORD = {GM_A: "correct horse battery", GM_B: "another good passphrase"}
EMAIL = {GM_A: "gm.a@example.com", GM_B: "gm.b@example.com"}
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

#: Every state-changing Workbench route the pin's walk finds unthrottled must
#: be exactly this set (agent-forge-harness-531x alignment plan, section 1).
EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/campaigns/{campaign_id}/library"): "reads only (X-7): a search sent by POST",
    ("POST", "/table/leave"): "screen principal, revoke only, never a new row (SEC-49)",
}
_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ── Structural: the route pin ────────────────────────────────────────────────


def _depends_on_throttle(dependant: Dependant) -> bool:
    """Whether the write throttle is anywhere in `dependant`'s tree: directly,
    or through a wrapper (`table_api.mint_throttle`), exactly as
    `test_auth_guard._depends_on` walks for `require_session`."""
    return any(is_write_throttle(d.call) or _depends_on_throttle(d) for d in dependant.dependencies)


def _unthrottled_mutations(
    rows: list[tuple[str, APIRoute, Dependant]],
) -> list[tuple[str, str]]:
    """(method, path) of every state-changing `WorkbenchRoute` that does NOT
    both carry the throttle and spend from it (i.e. is not `reads_by_post`)."""
    missing: list[tuple[str, str]] = []
    for path, route, dependant in rows:
        if not isinstance(route, WorkbenchRoute):
            continue
        for method in (route.methods or set()) & _STATE_CHANGING_METHODS:
            throttled = _depends_on_throttle(dependant) and not is_reads_only(
                getattr(route, "endpoint", None)
            )
            if not throttled:
                missing.append((method, path))
    return missing


def test_every_state_changing_workbench_route_carries_the_write_throttle() -> None:
    """The pin. Every state-changing `WorkbenchRoute` on the real app spends
    from the write throttle, except the two named exemptions — and the
    exemption set is exact, so a stale one (a route that turns out to write
    after all) fails this too."""
    rows = api_route_dependants(app)
    assert rows, "api_route_dependants() is empty; this walk would pass over nothing"
    mutating = [
        (method, path)
        for path, route, _ in rows
        if isinstance(route, WorkbenchRoute)
        for method in (route.methods or set()) & _STATE_CHANGING_METHODS
    ]
    assert mutating, "found no state-changing WorkbenchRoute; the walk is not seeing the app"
    assert set(_unthrottled_mutations(rows)) == set(EXEMPT)


def test_the_throttle_pin_reports_a_route_built_without_it() -> None:
    """Positive control: a bare `WorkbenchRoute` router with no throttle is
    reported unthrottled by the same walk the pin uses — proving the pin can
    actually fail, not just pass by construction."""
    probe = FastAPI()
    router = APIRouter(route_class=WorkbenchRoute)

    @router.post("/probe/{thing_id}")
    def create_thing() -> dict[str, bool]:
        return {"ok": True}

    probe.include_router(router)
    rows = api_route_dependants(probe)
    assert ("POST", "/probe/{thing_id}") in _unthrottled_mutations(rows)


def test_the_throttle_runs_after_origin_and_role_and_before_any_body_read() -> None:
    """Order pin: wherever the throttle is present, `origin_check` precedes it
    (SEC-7 before 531x), and the route declares no pydantic body model — the
    one thing that would let FastAPI parse a body before any dependency runs
    at all (the documented `/table/screen` exception aside, which carries no
    body-model route in R/A)."""
    #: `/table/screen` is the ONE documented exception (alignment plan §2,
    #: "Order a client observes"): its body is a declared pydantic model, so
    #: FastAPI decodes it before any dependency runs at all, throttle
    #: included. Every other throttled route reads its body by hand (or not
    #: at all), so the throttle is genuinely first there.
    BODY_MODEL_EXEMPT = {"/table/screen"}
    checked = 0
    for path, route, dependant in api_route_dependants(app):
        if not isinstance(route, WorkbenchRoute) or not _depends_on_throttle(dependant):
            continue
        calls = [d.call for d in dependant.dependencies]
        throttle_idx = next(i for i, c in enumerate(calls) if is_write_throttle(c))
        origin_idx = next(
            (i for i, c in enumerate(calls) if getattr(c, "__qualname__", "").startswith("origin_check")),
            None,
        )
        assert origin_idx is not None, f"{path} throttles with no origin_check in its dependant"
        assert origin_idx < throttle_idx, f"{path}: origin_check must precede the write throttle"
        if path not in BODY_MODEL_EXEMPT:
            assert dependant.body_params == [], (
                f"{path} declares a pydantic body model — FastAPI would decode it "
                f"before the throttle runs, so a throttled caller's body would "
                f"still be parsed"
            )
        checked += 1
    assert checked > 10, "too few routes checked; the walk is not seeing the real app"


# ── Behavioural: TestClient over the twin ────────────────────────────────────


@dataclass
class _World:
    db: InMemoryDatabase
    stores: campaigns_api.CampaignStores
    auth: InMemoryAuthStore
    now: list[datetime] = field(default_factory=lambda: [T0])

    def campaign(self, owner: int = GM_A, name: str = "Nocturne") -> str:
        with self.db.transaction() as unit:
            return self.stores.campaigns.create(unit, owner_id=owner, name=name, now=self.now[0]).id

    def campaign_count(self, owner: int = GM_A) -> int:
        with self.db.transaction() as unit:
            return len(self.stores.campaigns.list_for_owner(unit, owner))


@pytest.fixture
def world() -> Iterator[_World]:
    db = InMemoryDatabase()
    stores = campaigns_api.CampaignStores(
        InMemoryCampaignStore(db),
        InMemoryParticipantStore(db),
        InMemoryTableSessionStore(db, slot_clear=no_slots),
        InMemorySeatOfferStore(db),
        InMemoryAuditLog(),
        InMemoryCampaignSummaryStore(db, messages=InMemoryMessageStore()),
    )
    auth = InMemoryAuthStore()
    for user_id in (GM_A, GM_B):
        auth._users.append(User(id=user_id, email=EMAIL[user_id], role="dm", created_at=T0))
        auth._hashes[user_id] = hash_password(PASSWORD[user_id])
    made = _World(db, stores, auth)
    # documents_api composes its own CampaignStore instance, but it reads and
    # writes the SAME InMemoryDatabase tables, so a campaign created through
    # campaigns_api's store is visible to it (needed for the library route,
    # which is a documents_api route).
    doc_stores = documents_api.DocumentStores(InMemoryCampaignStore(db), InMemoryDocumentStore(db))
    app.dependency_overrides[campaigns_api.get_campaign_stores] = lambda: made.stores
    app.dependency_overrides[documents_api.get_document_stores] = lambda: doc_stores
    app.dependency_overrides[get_timeline_database] = lambda: made.db
    app.dependency_overrides[campaigns_api.get_clock] = lambda: made.now[0]
    app.dependency_overrides[documents_api.get_clock] = lambda: made.now[0]
    app.dependency_overrides[get_auth_store] = lambda: made.auth
    yield made
    for dependency in (
        campaigns_api.get_campaign_stores,
        documents_api.get_document_stores,
        get_timeline_database,
        campaigns_api.get_clock,
        documents_api.get_clock,
        get_auth_store,
    ):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(world: _World) -> TestClient:
    _as(GM_A)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _small_budget(monkeypatch: pytest.MonkeyPatch) -> SlidingWindowLimiter:
    """A 3-write budget, swapped in for every behavioural test here (the
    plan's pattern): small enough to exhaust in a handful of calls, and
    monkeypatch reverts it — `ratelimit.reset_all()`'s autouse fixture in
    `conftest.py` still clears whichever limiter is live around each test."""
    limiter = SlidingWindowLimiter(3, 3600)
    monkeypatch.setattr(ratelimit, "workbench_write_limiter", limiter)
    return limiter


def _as(user_id: int, role: str = "dm") -> None:
    def session(request: Request) -> SessionData:
        return SessionData(user_id=user_id, role=role)  # type: ignore[arg-type]

    app.dependency_overrides[require_session] = session


def _signed_out() -> None:
    def refuse() -> SessionData:
        raise HTTPException(status_code=401, detail="authentication required")

    app.dependency_overrides[require_session] = refuse


def _create(client: TestClient, name: str = "Nocturne") -> Response:
    return client.post("/campaigns", json={"schema_version": 1, "name": name})


#: The budget `_small_budget` installs for every behavioural test below.
BUDGET = 3


def test_the_limit_admits_exactly_n_writes_and_refuses_the_next(client: TestClient) -> None:
    allowed = [_create(client, f"Campaign {i}").status_code for i in range(BUDGET)]
    assert allowed == [201] * BUDGET, "the whole budget must be spendable"
    assert _create(client, "One Too Many").status_code == 429


def test_a_throttled_write_answers_the_workbench_429_shape(client: TestClient) -> None:
    for i in range(BUDGET):
        _create(client, f"Campaign {i}")
    refused = _create(client, "One Too Many")

    assert refused.status_code == 429
    body = refused.json()
    assert body["detail"]["code"] == ErrorCode.THROTTLED_USER.value
    assert body["detail"]["message"] == WRITE_THROTTLED_MESSAGE
    assert body["detail"]["retryable"] is True
    retry_after_s = body["detail"]["retry_after_s"]
    assert 1 <= retry_after_s <= 3601
    assert refused.headers["retry-after"] == str(retry_after_s)


def test_a_throttled_write_changes_no_row(client: TestClient, world: _World) -> None:
    for i in range(BUDGET):
        _create(client, f"Campaign {i}")
    before = world.campaign_count(GM_A)

    refused = _create(client, "One Too Many")

    assert refused.status_code == 429
    assert world.campaign_count(GM_A) == before, "a throttled request must write nothing"


def test_a_throttled_write_is_refused_before_its_body_is_read(client: TestClient) -> None:
    """A body long enough to trip `read_body`'s 8 KiB bound (422) still gets a
    429 once the budget is spent — proving the throttle runs before FastAPI's
    own body reader, not after it."""
    oversized = {"schema_version": 1, "name": "x" * 9_000}
    # Under budget: the oversized body IS read, and rejected on its own terms —
    # this spends one write too, the throttle runs regardless of the outcome.
    assert client.post("/campaigns", json=oversized).status_code == 422
    for i in range(BUDGET - 1):
        assert _create(client, f"Campaign {i}").status_code == 201
    # The budget (3) is now fully spent: 1 (oversized) + (BUDGET - 1) creates.

    refused = client.post("/campaigns", json=oversized)
    assert refused.status_code == 429, "the body must never be read once the budget is spent"


def test_the_budget_is_per_account_not_per_source(client: TestClient) -> None:
    """Two accounts from the same TestClient source each get their own budget."""
    for i in range(BUDGET):
        assert _create(client, f"A {i}").status_code == 201
    assert _create(client, "A one too many").status_code == 429

    _as(GM_B)
    other = TestClient(app)
    for i in range(BUDGET):
        assert other.post("/campaigns", json={"schema_version": 1, "name": f"B {i}"}).status_code == 201
    assert other.post("/campaigns", json={"schema_version": 1, "name": "B one too many"}).status_code == 429


def test_one_budget_spans_campaigns_and_routes(client: TestClient, world: _World) -> None:
    """The throttle key is the account, not a resource: creating two campaigns
    and patching each once shares one three-write budget, across two different
    routes on the same router."""
    a = _create(client, "A").json()["campaign_id"]  # 1 spent
    b = _create(client, "B").json()["campaign_id"]  # 2 spent
    assert client.patch(
        f"/campaigns/{a}", json={"schema_version": 1, "name": "A2"}
    ).status_code == 200  # 3 spent — the budget (BUDGET=3) is now exhausted

    refused = client.patch(f"/campaigns/{b}", json={"schema_version": 1, "name": "B2"})
    assert refused.status_code == 429, "the budget must be shared across campaigns and across routes"


def test_a_get_and_the_library_search_spend_nothing(client: TestClient) -> None:
    campaign = _create(client, "A").json()["campaign_id"]  # 1 spent
    for _ in range(5):
        assert client.get("/campaigns").status_code == 200
    library = {
        "schema_version": 1, "campaign_id": campaign, "category": "npcs", "search": "",
        "sort": "recent", "archived": False,
    }
    for _ in range(5):
        assert client.post(f"/campaigns/{campaign}/library", json=library).status_code == 200
    # BUDGET - 1 writes remain: all still spendable, then refused.
    for i in range(BUDGET - 1):
        assert _create(client, f"remaining {i}").status_code == 201
    assert _create(client, "one too many").status_code == 429


def test_an_unauthenticated_write_spends_nothing(world: _World) -> None:
    _signed_out()
    anon = TestClient(app)
    assert anon.post("/campaigns", json={"schema_version": 1, "name": "x"}).status_code == 401

    _as(GM_A)
    client = TestClient(app)
    for i in range(BUDGET):
        assert _create(client, f"Campaign {i}").status_code == 201
    assert _create(client, "One Too Many").status_code == 429, "the budget must be untouched by the 401"


def test_a_cross_origin_write_spends_nothing(client: TestClient) -> None:
    foreign = client.post(
        "/campaigns", json={"schema_version": 1, "name": "x"},
        headers={"origin": "https://evil.example"},
    )
    assert foreign.status_code == 403

    for i in range(BUDGET):
        assert _create(client, f"Campaign {i}").status_code == 201
    assert _create(client, "One Too Many").status_code == 429, "the budget must be untouched by the 403"


def test_a_player_seat_accept_is_throttled_on_the_account_router(client: TestClient) -> None:
    """`account_router` (seats) carries the same throttle. The throttle runs
    before ownership, so a nonexistent offer still proves it: 404 while under
    budget, 429 once it is spent — never a 404 past the budget."""
    for _ in range(BUDGET):
        r = client.post("/seats/offers/sof_aaaaaaaaaaaaaaaaaaaaaa/accept")
        assert r.status_code == 404
    refused = client.post("/seats/offers/sof_aaaaaaaaaaaaaaaaaaaaaa/accept")
    assert refused.status_code == 429
    assert refused.json()["detail"]["code"] == ErrorCode.THROTTLED_USER.value


def test_a_retry_after_past_a_day_is_clamped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The window is 172,800s (2 days); `spend_write` must still clamp
    `retry_after_s` to at most 86,400, the contract's documented ceiling."""
    monkeypatch.setattr(ratelimit, "workbench_write_limiter", SlidingWindowLimiter(1, 172_800))
    assert _create(client, "A").status_code == 201
    refused = _create(client, "B")
    assert refused.status_code == 429
    assert refused.json()["detail"]["retry_after_s"] <= 86_400
    assert int(refused.headers["retry-after"]) <= 86_400


def test_mint_throttle_spends_only_for_an_account_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit-level: `table_api`'s `mint_throttle` spends the write budget for
    an account principal and spends nothing for a screen principal (a screen
    never mints — it is answered `inactive` — so it must never be charged)."""
    from service.auth_store import AuthStore
    from service.table_api import Principal, build_router
    from service.table_session_store import LiveScreen

    def never_called(request: Request, store: AuthStore) -> SessionData:
        raise AssertionError("a live screen decides, or the test passes an account directly")

    router = build_router(
        session=never_called,
        auth_store=lambda: cast(AuthStore, None),
        clear_session_cookie=lambda response: None,
        lifecycle=lambda: None,
    )
    mint_route = next(route for route in router.routes if isinstance(route, APIRoute) and route.path == "/table/screen")
    mint_throttle = next(d.call for d in mint_route.dependant.dependencies if is_write_throttle(d.call))
    assert mint_throttle is not None

    monkeypatch.setattr(ratelimit, "workbench_write_limiter", SlidingWindowLimiter(1, 3600))

    account_principal = Principal(account=SessionData(user_id=GM_A, role="dm"))  # type: ignore[arg-type]
    mint_throttle(account_principal)  # spends the one write
    with pytest.raises(RateLimited):
        ratelimit.check_workbench_write(GM_A)  # the budget is now empty

    screen = LiveScreen(grant_id="scr_a", session_id="tbs_a", campaign_id="cmp_aaaaaaaaaaaaaaaaaaaaaa", generation=1)
    screen_principal = Principal(screen=screen)
    mint_throttle(screen_principal)  # must spend nothing — GM_A's budget stays exactly empty, no error either
    ratelimit.check_workbench_write(GM_B)  # a different, untouched account still has its own budget


def test_a_screen_mint_is_throttled_on_the_account(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """N2 (PR-A review pr221-531x-r1): the mint throttle's wiring was pinned
    (the route pin) and its spend behaviour was unit-tested directly
    (`test_mint_throttle_spends_only_for_an_account_principal`), but no test
    hit the real `POST /table/screen` route over HTTP. This spends the
    account's whole budget on ordinary campaign writes, then proves the mint
    route itself answers 429 once it is gone — the throttle fires as a router
    dependency before the handler runs, so no live table session is needed to
    observe it.

    `table_api`'s session check is called directly, never through
    `Depends(require_session)` (its own docstring), so `_as`'s dependency
    override does not reach it here — a real signed cookie is required, as
    `service/tests/test_table_api.py`'s own fixture mints one."""
    secret = "table-throttle-test-secret-long-enough-for-the-floor"
    monkeypatch.setattr(config, "SESSION_SECRET", secret)
    token = encode_session(SessionData(user_id=GM_A, role="dm"), secret)  # type: ignore[arg-type]

    for i in range(BUDGET):
        assert _create(client, f"Campaign {i}").status_code == 201

    client.cookies.set(config.SESSION_COOKIE_NAME, token)
    refused = client.post("/table/screen", json={"schema_version": 1, "campaign_id": "cmp_" + "a" * 22})

    assert refused.status_code == 429
    assert refused.json()["detail"]["code"] == ErrorCode.THROTTLED_USER.value


# ── Config: a bad limit fails at startup ─────────────────────────────────────


def test_a_bad_workbench_write_limit_fails_startup() -> None:
    with pytest.raises(ValueError, match="WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT"):
        ratelimit._build(
            0, config.WORKBENCH_WRITE_RATE_LIMIT_WINDOW_S,
            "WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT", "WORKBENCH_WRITE_RATE_LIMIT_WINDOW_S",
        )


def test_a_bad_workbench_write_window_fails_startup() -> None:
    with pytest.raises(ValueError, match="WORKBENCH_WRITE_RATE_LIMIT_WINDOW_S"):
        ratelimit._build(
            config.WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT, 0.0,
            "WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT", "WORKBENCH_WRITE_RATE_LIMIT_WINDOW_S",
        )


def test_workbench_write_throttle_defaults_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """M2 (PR-A review pr221-531x-r1): the two tests above prove only that an
    invalid limit or window refuses at startup — neither proves what an ABSENT
    one reads as. With no env set, `_int("WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT",
    600)` -> `10**12` and `_float("..._WINDOW_S", 3600.0)` -> `0.001` both
    survived mutation: either ships a throttle that is, in effect, switched
    off. Reload (`tests/test_config.py`'s pattern) and pin the numbers
    themselves."""
    monkeypatch.delenv("WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT", raising=False)
    monkeypatch.delenv("WORKBENCH_WRITE_RATE_LIMIT_WINDOW_S", raising=False)
    cfg = importlib.reload(config)
    assert cfg.WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT == 600
    assert cfg.WORKBENCH_WRITE_RATE_LIMIT_WINDOW_S == 3600.0


def test_reset_all_refills_the_real_workbench_write_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """L2 (PR-A review pr221-531x-r1): every throttle test above monkeypatches
    a fresh limiter, so none of them proves test isolation for a file that
    spends on the real 600/h shape. This file's own `_small_budget` autouse
    fixture already swaps in a 3-write limiter for every test — including
    this one — so a limiter is rebuilt here with the account's documented
    real numbers (`ratelimit._build`, the exact call `ratelimit.py` makes at
    import) and installed in `workbench_write_limiter`'s place, which
    `check_workbench_write` and `reset_all` both resolve by that module-level
    name at call time. Spend one, `reset_all()`, and the full budget must be
    available again — mutant M17 (dropping `workbench_write_limiter.reset()`
    from `reset_all`) fails this."""
    limit = config.WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT
    monkeypatch.setattr(
        ratelimit, "workbench_write_limiter",
        ratelimit._build(
            limit, config.WORKBENCH_WRITE_RATE_LIMIT_WINDOW_S,
            "WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT", "WORKBENCH_WRITE_RATE_LIMIT_WINDOW_S",
        ),
    )
    user_id = 9_004_231  # an id no other test in this file uses
    ratelimit.check_workbench_write(user_id)
    ratelimit.reset_all()
    for _ in range(limit):
        ratelimit.check_workbench_write(user_id)


# ── The reveal Stop's own budget (agent-forge-harness-1kg.7.2, ID-8) ─────────

REVEAL_STOP_ROUTE = ("POST", "/campaigns/{campaign_id}/reveals/stop")
_A_CAMPAIGN = "cmp_" + "a" * 22


class _NothingLive:
    """A reveal service with no live session: a Stop is nothing, never refused."""

    def stop(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.fixture
def stopping(world: _World) -> Iterator[None]:
    app.dependency_overrides[get_reveals] = lambda: cast(Reveals, _NothingLive())
    yield
    app.dependency_overrides.pop(get_reveals, None)


def _a_stop(client: TestClient) -> Response:
    body = {"schema_version": 1, "command_id": "cmd_aaaaaaaaaaaaaaaa", "scope": "all"}
    return client.post(f"/campaigns/{_A_CAMPAIGN}/reveals/stop", json=body)


def _a_confirm(client: TestClient) -> Response:
    body = {
        "schema_version": 1,
        "command_id": "cmd_aaaaaaaaaaaaaaaa",
        "document_id": "doc_" + "a" * 22,
        "session_id": "ses_" + "a" * 22,
        "reveal_epoch": 0,
        "version": 1,
        "mask": ["name"],
        "audience": {"kind": "table"},
    }
    return client.post(f"/campaigns/{_A_CAMPAIGN}/reveals", json=body)


def _depends_on_narrowing(dependant: Dependant) -> bool:
    return any(is_narrowing_throttle(d.call) or _depends_on_narrowing(d) for d in dependant.dependencies)


def test_w1_a_stop_is_answered_when_the_shared_write_budget_is_spent(client: TestClient, stopping: None) -> None:
    # kills: building the Stop router with the default throttle
    for i in range(BUDGET):
        assert _create(client, f"Campaign {i}").status_code == 201
    assert _create(client, "One Too Many").status_code == 429
    assert _a_confirm(client).status_code == 429, "a Confirm spends the shared budget"
    stopped = _a_stop(client)
    assert stopped.status_code == 200 and stopped.json() == {"schema_version": 1, "state": None}


def test_w1_the_narrowing_budget_is_its_own_and_answers_the_throttle_shape(
    client: TestClient, stopping: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: spending the shared budget for a Stop; a 429 without Retry-After
    monkeypatch.setattr(ratelimit, "reveal_stop_limiter", SlidingWindowLimiter(2, 3600))
    assert [_a_stop(client).status_code for _ in range(2)] == [200, 200]
    refused = _a_stop(client)
    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert detail["code"] == ErrorCode.THROTTLED_USER.value and detail["retryable"] is True
    assert detail["message"] == NARROWING_THROTTLED_MESSAGE and detail["retry_after_s"] == 3600
    assert refused.headers["retry-after"] == "3600"
    for i in range(BUDGET):
        assert _create(client, f"Campaign {i}").status_code == 201, "the shared budget was never spent by a Stop"


def test_w1_a_cross_origin_stop_spends_nothing(
    client: TestClient, stopping: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "reveal_stop_limiter", SlidingWindowLimiter(1, 3600))
    body = {"schema_version": 1, "command_id": "cmd_aaaaaaaaaaaaaaaa", "scope": "all"}
    foreign = client.post(
        f"/campaigns/{_A_CAMPAIGN}/reveals/stop", json=body, headers={"origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    assert _a_stop(client).status_code == 200, "the 403 came before the budget"


def test_w1_exactly_the_stop_route_carries_the_narrowing_marker() -> None:
    """The second pin (Critic C-5): no later write can move onto the narrowing
    budget unnoticed. Confirm and every other route stay off it."""
    # kills: dropping the marker; putting Confirm on the narrowing router
    on_it = {
        (method, path)
        for path, route, dependant in api_route_dependants(app)
        if isinstance(route, WorkbenchRoute) and _depends_on_narrowing(dependant)
        for method in (route.methods or set())
    }
    assert on_it == {REVEAL_STOP_ROUTE}
    every = {path for path, route, _ in api_route_dependants(app) if isinstance(route, WorkbenchRoute)}
    assert "/campaigns/{campaign_id}/reveals" in every and "/campaigns/{campaign_id}/reveals/stop" in every


def test_w1_the_narrowing_throttle_still_counts_as_a_write_throttle() -> None:
    # kills: dropping _WRITE_THROTTLE from narrowing_throttle (the pin would call the Stop unthrottled)
    rows = api_route_dependants(app)
    assert REVEAL_STOP_ROUTE not in set(_unthrottled_mutations(rows))


_REPO = Path(__file__).resolve().parents[2]


def _import_ratelimit(**env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("REVEAL_STOP_")}
    return subprocess.run(
        [sys.executable, "-c", "import service.ratelimit"],
        cwd=_REPO,
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REVEAL_STOP_RATE_LIMIT_PER_ACCOUNT", "0"),
        ("REVEAL_STOP_RATE_LIMIT_WINDOW_S", "0.0"),
        ("REVEAL_STOP_RATE_LIMIT_WINDOW_S", "nan"),
    ],
)
def test_w1_a_bad_stop_limit_fails_startup(name: str, value: str) -> None:
    # kills: building the limiter without _build, so a bad value is accepted
    assert _import_ratelimit().returncode == 0, "the control: the defaults start"
    refused = _import_ratelimit(**{name: value})
    assert refused.returncode != 0 and name in refused.stderr


def test_w1_the_stop_budget_defaults_are_pinned_and_reset_all_refills_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVEAL_STOP_RATE_LIMIT_PER_ACCOUNT", raising=False)
    monkeypatch.delenv("REVEAL_STOP_RATE_LIMIT_WINDOW_S", raising=False)
    cfg = importlib.reload(config)
    assert (cfg.REVEAL_STOP_RATE_LIMIT_PER_ACCOUNT, cfg.REVEAL_STOP_RATE_LIMIT_WINDOW_S) == (100, 600.0)
    hourly = cfg.REVEAL_STOP_RATE_LIMIT_PER_ACCOUNT * 3600 / cfg.REVEAL_STOP_RATE_LIMIT_WINDOW_S
    assert hourly <= cfg.WORKBENCH_WRITE_RATE_LIMIT_PER_ACCOUNT, "no higher than the shared write budget's ceiling"

    monkeypatch.setattr(ratelimit, "reveal_stop_limiter", SlidingWindowLimiter(1, 3600))
    ratelimit.check_reveal_stop(9_004_232)
    with pytest.raises(RateLimited):
        ratelimit.check_reveal_stop(9_004_232)
    ratelimit.reset_all()
    ratelimit.check_reveal_stop(9_004_232)

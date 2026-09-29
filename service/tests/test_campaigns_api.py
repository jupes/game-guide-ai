"""The GM's campaign and seat routes (bead 1kg.2.2), through the real app over
the in-memory twins.

Everything a lock or an index decides between two connections is
`tests/test_seats_db.py`'s; the shared store behaviour in both worlds is
`tests/test_campaign_db.py`'s. This file holds what a client observes: the
answers, their order (SEC-3, L-18), the one 404 across tenants (T-2), the
offer's single answer (T-24's GM half), the confirmation and Remove behind the
password (SEC-40).

Run from the repo root:
    uv run python -m pytest service/tests/test_campaigns_api.py -q
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from httpx import Response

from service import app as appmod
from service import campaigns_api
from service.app import app, get_auth_store, get_timeline_database, require_session
from service.audit_log import InMemoryAuditLog
from service.auth_store import InMemoryAuthStore, User
from service.campaign_store import InMemoryCampaignStore
from service.campaign_summary_store import InMemoryCampaignSummaryStore, avatar_for
from service.db import InMemoryDatabase
from service.hashing import HashingCapacityError, hash_password
from service.history import InMemoryMessageStore
from service.invites import Role
from service.jobs import InMemoryJobQueue
from service.participant_store import InMemoryParticipantStore
from service.ratelimit import RateLimited
from service.seat_offer_store import OFFER_LIFETIME, THROTTLE_LIMIT, InMemorySeatOfferStore
from service.session import SessionData
from service.table_session_store import InMemoryTableSessionStore, no_slots
from service.workbench_api import NOT_FOUND_DETAIL, REAUTH_FAILED_DETAIL
from service.workbench_contracts import Campaign, ErrorBody, Seat, SeatPage, SeatRemoveRequest

GM_A, GM_B, PLAYER = 1, 2, 3
PASSWORD = {GM_A: "correct horse battery", GM_B: "another good passphrase"}
EMAIL = {GM_A: "gm.a@example.com", GM_B: "gm.b@example.com", PLAYER: "wren@example.com"}
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
UNAUTHENTICATED = {"detail": "not signed in"}
CANARY = "Zx9Canary"
MISSING_CAMPAIGN = "cmp_" + "z" * 22
MISSING_SEAT = "prt_" + "z" * 22


class _RecordingDriver:
    """Stands in for the job driver: records the job the route hands to
    `run_after_response`, and runs nothing."""

    def __init__(self) -> None:
        self.ran: list[int] = []

    def run_job(self, job_id: int) -> None:
        self.ran.append(job_id)


@dataclass
class _World:
    db: InMemoryDatabase
    stores: campaigns_api.CampaignStores
    jobs: InMemoryJobQueue
    auth: InMemoryAuthStore
    driver: _RecordingDriver
    now: list[datetime] = field(default_factory=lambda: [T0])

    def campaign(self, owner: int = GM_A, name: str = "Nocturne") -> str:
        with self.db.transaction() as unit:
            return self.stores.campaigns.create(unit, owner_id=owner, name=name, now=self.now[0]).id

    def seat(self, campaign: str, alias: str = "Rook") -> str:
        with self.db.transaction() as unit:
            return self.stores.participants.add(unit, campaign, alias=alias, now=self.now[0]).id

    def accepted(self, campaign: str, alias: str, player: int, address: str) -> str:
        seat = self.seat(campaign, alias)
        with self.db.transaction() as unit:
            offer = self.stores.offers.create(
                unit, campaign, seat, offered_by=self._owner(unit, campaign), address=address, now=self.now[0]
            )
            self.stores.participants.offer(unit, campaign, seat, user_id=player)
            self.stores.participants.accept(unit, campaign, seat, user_id=player, now=self.now[0])
            self.stores.offers.close(unit, offer.id, outcome="accepted", now=self.now[0])
        return seat

    def _owner(self, unit: Any, campaign: str) -> int:
        for owner in (GM_A, GM_B):
            if self.stores.campaigns.get(unit, campaign, owner_id=owner) is not None:
                return owner
        raise AssertionError("no such campaign")

    def ledger(self, campaign: str) -> list[str]:
        with self.db.transaction() as unit:
            return [e.action for e in self.stores.audit.for_campaign(unit, campaign)]

    def revision(self, campaign: str) -> int | None:
        with self.db.transaction() as unit:
            return self.stores.campaigns.authz_revision(unit, campaign)

    def offers(self) -> list[Any]:
        table = self.db.tables.get("seat_offers")
        if table is None:
            return []
        with self.db.transaction() as unit:
            return list(table.visible(unit).values())


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
    made = _World(db, stores, InMemoryJobQueue(db), auth, _RecordingDriver())
    app.dependency_overrides[campaigns_api.get_campaign_stores] = lambda: made.stores
    app.dependency_overrides[get_timeline_database] = lambda: made.db
    app.dependency_overrides[appmod._job_queue] = lambda: made.jobs
    app.dependency_overrides[appmod._job_driver] = lambda: made.driver
    app.dependency_overrides[campaigns_api.get_clock] = lambda: made.now[0]
    app.dependency_overrides[get_auth_store] = lambda: made.auth
    yield made
    for dependency in (
        campaigns_api.get_campaign_stores,
        get_timeline_database,
        appmod._job_queue,
        appmod._job_driver,
        campaigns_api.get_clock,
        get_auth_store,
    ):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(world: _World) -> TestClient:
    _as(GM_A)
    return TestClient(app)


def _as(user_id: int, role: Role = "dm") -> None:
    """Every later request is this account's, re-read as `require_session`
    re-reads it: the `User` stashed on the request, as the app does."""

    def session(request: Request) -> SessionData:
        request.state.auth_user = User(id=user_id, email=EMAIL.get(user_id, "x@example.com"), role=role,
                                       created_at=T0)
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


def _offer(client: TestClient, campaign: str, seat: str, email: str) -> Response:
    return client.post(f"/campaigns/{campaign}/participants/{seat}/offer", json={"schema_version": 1, "email": email})


def _remove(client: TestClient, campaign: str, seat: str, password: str = PASSWORD[GM_A]) -> Response:
    return client.post(
        f"/campaigns/{campaign}/participants/{seat}/remove", json={"schema_version": 1, "password": password}
    )


def _seats(client: TestClient, campaign: str, **params: str) -> list[dict[str, Any]]:
    answer = client.get(f"/campaigns/{campaign}/participants", params=params)
    assert answer.status_code == 200, answer.text
    SeatPage.model_validate(answer.json())
    return list(answer.json()["items"])


# ── Campaigns ────────────────────────────────────────────────────────────────


def test_a_created_campaign_is_the_callers_and_duplicate_names_make_two(client: TestClient, world: _World) -> None:
    first = client.post("/campaigns", json={"schema_version": 1, "name": "  Nocturne  "})
    world.now[0] = T0 + timedelta(minutes=1)
    second = client.post("/campaigns", json={"schema_version": 1, "name": "Nocturne"})
    assert (first.status_code, second.status_code) == (201, 201)
    body = Campaign.model_validate(first.json())
    assert body.name == "Nocturne" and body.archived_at is None
    assert first.json()["campaign_id"] != second.json()["campaign_id"], "a retried create makes a second"
    listed = client.get("/campaigns").json()
    assert [c["campaign_id"] for c in listed["items"]] == [second.json()["campaign_id"], first.json()["campaign_id"]]
    assert client.get(f"/campaigns/{body.campaign_id}").json() == first.json()
    assert set(first.json()) == {
        "schema_version", "campaign_id", "name", "created_at", "updated_at", "archived_at",
        # bead cfx: the tavern card's facts, and still no owner.
        "concluded_at", "tone", "game_system", "avatar_icon", "avatar_tone", "badge", "seat_count",
        "last_activity_at", "last_played_at", "dormant",
    }


def test_a_card_carries_its_facts_on_every_answer(client: TestClient, world: _World) -> None:
    """Bead cfx: the create, the read, the list and a patch all answer the
    tavern card's facts, derived by the one rule each."""
    created = client.post("/campaigns", json={"schema_version": 1, "name": "Crown", "tone": "  Mystery · Low magic "})
    assert created.status_code == 201, created.text
    card = created.json()
    campaign = card["campaign_id"]
    assert card["tone"] == "Mystery · Low magic", "trimmed as a name is"
    assert (card["game_system"], card["badge"], card["seat_count"], card["last_played_at"], card["dormant"]) == (
        "dnd5e", None, 0, None, False,
    )
    assert card["last_activity_at"] == card["created_at"] and card["concluded_at"] is None
    assert (card["avatar_icon"], card["avatar_tone"]) == avatar_for(campaign)
    assert client.get(f"/campaigns/{campaign}").json() == card

    world.seat(campaign, "Rook")
    world.accepted(campaign, "Wren", PLAYER, "wren@example.com")
    world.now[0] = T0 + timedelta(hours=1)
    with world.db.transaction() as unit:
        world.stores.sessions.start(
            unit, campaign, owner_id=GM_A, expires_at=world.now[0] + timedelta(hours=12), now=world.now[0]
        )
    live = client.get(f"/campaigns/{campaign}").json()
    assert (live["badge"], live["seat_count"], live["last_played_at"]) == ("live", 2, "2026-09-01T13:00:00Z")
    assert live["last_activity_at"] == "2026-09-01T13:00:00Z"
    assert client.get("/campaigns").json()["items"] == [live], "the list answers the same card"

    cleared = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "tone": None})
    assert cleared.status_code == 200 and cleared.json()["tone"] is None
    assert cleared.json()["badge"] == "live", "a patch answers the facts too"
    retoned = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "tone": "Grim", "name": "Crowns"})
    assert (retoned.json()["tone"], retoned.json()["name"]) == ("Grim", "Crowns")


def test_a_tone_line_rides_an_archive_and_a_restore_in_their_own_transaction(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    archived = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": True, "tone": "Grim"})
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None and archived.json()["tone"] == "Grim"
    restored = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": False, "tone": None})
    assert restored.json()["archived_at"] is None and restored.json()["tone"] is None
    refused = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "tone": "t" * 81})
    assert refused.status_code == 422 and "t" * 81 not in refused.text


def test_conclude_and_reopen_are_idempotent_audited_once_each_and_not_an_authorisation_fact(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    seat = world.seat(campaign)
    revision = world.revision(campaign)
    world.now[0] = T0 + timedelta(days=1)
    concluded = client.post(f"/campaigns/{campaign}/conclude")
    assert concluded.status_code == 200, concluded.text
    body = Campaign.model_validate(concluded.json())
    assert body.concluded_at == world.now[0] and body.archived_at is None
    world.now[0] = T0 + timedelta(days=2)
    assert client.post(f"/campaigns/{campaign}/conclude").json() == concluded.json(), "a repeat changes nothing"
    assert [c["campaign_id"] for c in client.get("/campaigns").json()["items"]] == [campaign], "not archived"
    assert [s["participant_id"] for s in _seats(client, campaign)] == [seat], "the table keeps its seats"

    reopened = client.post(f"/campaigns/{campaign}/reopen")
    assert reopened.status_code == 200 and reopened.json()["concluded_at"] is None
    assert client.post(f"/campaigns/{campaign}/reopen").json() == reopened.json()
    assert world.ledger(campaign) == ["campaign.concluded", "campaign.reopened"], "one row per change"
    assert world.revision(campaign) == revision, "concluded narrows and widens nothing"


def test_a_campaign_untouched_for_thirty_days_is_dormant_until_it_is_concluded(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    world.now[0] = T0 + timedelta(days=30)
    assert client.get(f"/campaigns/{campaign}").json()["dormant"] is False, "thirty days is still active"
    world.now[0] = T0 + timedelta(days=31)
    assert client.get(f"/campaigns/{campaign}").json()["dormant"] is True
    concluded = client.post(f"/campaigns/{campaign}/conclude").json()
    assert (concluded["dormant"], concluded["badge"]) == (False, None)


def test_rename_archive_and_restore_through_patch(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    renamed = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "name": "Aubade"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Aubade"
    assert world.ledger(campaign) == [] and world.revision(campaign) == 0, "a rename is not an authorisation fact"
    archived = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": True, "name": "Coda"})
    assert archived.json()["archived_at"] is not None and archived.json()["name"] == "Coda"
    again = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": True})
    assert again.status_code == 200 and again.json()["archived_at"] == archived.json()["archived_at"]
    restored = client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": False})
    assert restored.json()["archived_at"] is None
    assert world.ledger(campaign) == ["campaign.archived", "campaign.restored"] and world.revision(campaign) == 2
    assert client.get("/campaigns").json()["items"][0]["campaign_id"] == campaign
    assert client.patch(f"/campaigns/{campaign}", json={"schema_version": 1}).status_code == 422


def test_the_campaign_list_pages_and_leaves_archived_ones_out_unless_asked(client: TestClient, world: _World) -> None:
    made = []
    for n in range(3):
        world.now[0] = T0 + timedelta(hours=n)
        made.append(world.campaign(name=f"C{n}"))
    client.patch(f"/campaigns/{made[1]}", json={"schema_version": 1, "archived": True})
    first = client.get("/campaigns", params={"limit": "1"}).json()
    second = client.get("/campaigns", params={"limit": "1", "cursor": first["next_cursor"]}).json()
    assert [c["campaign_id"] for c in first["items"] + second["items"]] == [made[2], made[0]]
    assert second["next_cursor"] is None
    everything = client.get("/campaigns", params={"include_archived": "true"}).json()["items"]
    assert [c["campaign_id"] for c in everything] == list(reversed(made))
    for bad in ({"limit": "0"}, {"limit": "51"}, {"limit": "x"}, {"cursor": "bad cursor"},
                {"cursor": "WyJ4Il0"}, {"include_archived": "yes"}):
        refused = client.get("/campaigns", params=bad)
        assert refused.status_code == 422, bad
        assert refused.json()["detail"]["field"] in {"limit", "cursor", "include_archived"}


# ── Seats ────────────────────────────────────────────────────────────────────


def test_a_seat_is_added_open_and_every_add_refusal_is_its_own(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    added = client.post(f"/campaigns/{campaign}/participants", json={"schema_version": 1, "alias": " Rook "})
    assert added.status_code == 201
    seat = Seat.model_validate(added.json())
    assert (seat.alias, seat.status.value, seat.address) == ("Rook", "open", None)
    assert world.ledger(campaign) == ["participant.added"] and world.revision(campaign) == 1
    taken = client.post(f"/campaigns/{campaign}/participants", json={"schema_version": 1, "alias": "rook"})
    assert (taken.status_code, taken.json()["detail"]["code"]) == (409, "alias_taken")
    hidden = client.post(f"/campaigns/{campaign}/participants", json={"schema_version": 1, "alias": "Ro​ok"})
    assert hidden.status_code == 422 and hidden.json()["detail"]["field"] == "alias"
    for n in range(campaigns_api.SEAT_CAP - 1):
        world.seat(campaign, f"Seat {n}")
    full = client.post(f"/campaigns/{campaign}/participants", json={"schema_version": 1, "alias": "Late"})
    assert (full.status_code, full.json()["detail"]["code"]) == (409, "seat_cap_reached")
    client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": True})
    archived = client.post(f"/campaigns/{campaign}/participants", json={"schema_version": 1, "alias": "Later"})
    assert (archived.status_code, archived.json()["detail"]["code"]) == (409, "campaign_archived")


def test_the_gm_sees_each_status_and_the_address_as_typed(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    open_seat = world.seat(campaign, "Open")
    offered = world.seat(campaign, "Offered")
    assert _offer(client, campaign, offered, " Finch@Example.com ").status_code == 204
    accepted = world.accepted(campaign, "Accepted", PLAYER, "Wren@Example.com")
    by_id = {s["participant_id"]: s for s in _seats(client, campaign)}
    assert (by_id[open_seat]["status"], by_id[open_seat]["address"]) == ("open", None)
    assert (by_id[offered]["status"], by_id[offered]["address"]) == ("offered", "Finch@Example.com")
    assert (by_id[accepted]["status"], by_id[accepted]["address"]) == ("awaiting_confirmation", "Wren@Example.com")
    world.now[0] = T0 + OFFER_LIFETIME
    assert {s["participant_id"]: s["status"] for s in _seats(client, campaign)}[offered] == "not_accepted"
    assert not any("user_id" in s for s in _seats(client, campaign))


# ── The offer's single answer (T-24, the GM half) ────────────────────────────


def test_every_non_error_offer_is_one_byte_identical_204(client: TestClient, world: _World) -> None:
    """An address nobody holds, one an (Unverified) account holds, a repeat to
    the same seat and to another, an address whose seat was accepted, one whose
    holder blocked the owner, and a repeat while the owner is at the throttle."""
    campaign = world.campaign()
    seats = [world.seat(campaign, f"Seat {n}") for n in range(6)]
    world.accepted(campaign, "Held", PLAYER, "held@example.com")
    with world.db.transaction() as unit:
        world.stores.offers.block(unit, blocker_user_id=PLAYER, blocked_owner_id=GM_A)
    answers = [
        _offer(client, campaign, seats[0], "nobody@example.com"),
        _offer(client, campaign, seats[1], EMAIL[PLAYER]),
        _offer(client, campaign, seats[0], "nobody@example.com"),
        _offer(client, campaign, seats[2], "NOBODY@example.com"),
        _offer(client, campaign, seats[3], "held@example.com"),
        _offer(client, campaign, seats[4], "blocker@example.com"),
    ]
    other = world.campaign(name="Busy")
    for n in range(THROTTLE_LIMIT - 4):
        assert _offer(client, other, world.seat(other, f"Busy {n}"), f"p{n}@example.com").status_code == 204
    throttled = _offer(client, campaign, seats[5], "fresh@example.com")
    assert throttled.status_code == 429
    answers.append(_offer(client, campaign, seats[1], EMAIL[PLAYER]))
    shapes = {_shape(answer) for answer in answers}
    assert len(shapes) == 1 and next(iter(shapes))[:2] == (204, b"")


def test_the_offer_path_never_asks_the_auth_store(client: TestClient, world: _World) -> None:
    """The account store is a spy that fails on any call after the session
    (SEC-50(2)): the answer cannot depend on whether an account holds the
    address when nothing about accounts is asked."""

    class _Spy:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the offer path asked the auth store for {name}")

    app.dependency_overrides[get_auth_store] = lambda: _Spy()
    campaign = world.campaign()
    for address in ("nobody@example.com", EMAIL[PLAYER], EMAIL[GM_B]):
        assert _offer(client, campaign, world.seat(campaign, address[:4]), address).status_code == 204


def test_the_owners_own_address_is_refused_naming_email(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    refused = _offer(client, campaign, world.seat(campaign), " GM.A@example.COM ")
    assert refused.status_code == 422 and refused.json()["detail"]["field"] == "email"
    assert CANARY not in refused.text and "gm.a" not in refused.text


def test_the_offer_steps_answer_in_l6s_order(client: TestClient, world: _World) -> None:
    """Each refusal wins over every later one: not found over the owner's own
    address, the owner's own address over archived, archived over not-open,
    not-open over the throttle."""
    theirs = world.campaign(owner=GM_B, name="Theirs")
    assert _offer(client, theirs, world.seat(theirs), EMAIL[GM_A]).json() == NOT_FOUND
    campaign = world.campaign()
    held = world.accepted(campaign, "Held", PLAYER, "held@example.com")
    live = world.seat(campaign, "Live")
    assert _offer(client, campaign, live, "live@example.com").status_code == 204
    not_open = _offer(client, campaign, held, "other@example.com")
    assert (not_open.status_code, not_open.json()["detail"]["code"]) == (409, "seat_not_open")
    assert _offer(client, campaign, live, "other@example.com").json()["detail"]["code"] == "seat_not_open"
    other = world.campaign(name="Busy")
    for n in range(THROTTLE_LIMIT - 1):
        _offer(client, other, world.seat(other, f"Busy {n}"), f"p{n}@example.com")
    assert _offer(client, campaign, held, "other@example.com").json()["detail"]["code"] == "seat_not_open"
    client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": True})
    archived = _offer(client, campaign, held, "other@example.com")
    assert archived.json()["detail"]["code"] == "campaign_archived"
    assert _offer(client, campaign, held, EMAIL[GM_A]).status_code == 422


def test_the_throttle_holds_across_app_instances_and_names_its_wait(world: _World) -> None:
    _as(GM_A)
    first, second = TestClient(app), TestClient(app)
    campaign = world.campaign()
    for n in range(THROTTLE_LIMIT):
        client = first if n % 2 else second
        assert _offer(client, campaign, world.seat(campaign, f"S{n}"), f"p{n}@example.com").status_code == 204
    world.now[0] = T0 + timedelta(hours=1)
    refused = _offer(TestClient(app), campaign, world.seat(campaign, "Late"), "late@example.com")
    assert refused.status_code == 429
    body = ErrorBody.model_validate(refused.json())
    assert body.detail.code.value == "throttled_user" and body.detail.retryable
    assert body.detail.retry_after_s == 23 * 3600 and refused.headers["retry-after"] == str(23 * 3600)
    assert len([o for o in world.offers() if o.offered_by == GM_A]) == THROTTLE_LIMIT
    world.now[0] = T0 + timedelta(days=1, seconds=1)
    assert _offer(first, campaign, world.seat(campaign, "Later"), "later@example.com").status_code == 204


# ── The GM's confirmation (D-12) ─────────────────────────────────────────────


def test_only_the_owner_confirms_an_accepted_seat_and_it_is_audited_once(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    seat = world.accepted(campaign, "Rook", PLAYER, "Wren@example.com")
    open_seat = world.seat(campaign, "Open")
    url = f"/campaigns/{campaign}/participants/{seat}/confirm"
    _as(GM_B)
    assert client.post(url).json() == NOT_FOUND
    _as(PLAYER, "player")
    assert client.post(url).status_code == 403
    _as(GM_A)
    before = world.revision(campaign)
    confirmed = client.post(url)
    assert confirmed.status_code == 200
    assert (confirmed.json()["status"], confirmed.json()["address"]) == ("confirmed", "Wren@example.com")
    assert world.revision(campaign) == (before or 0) + 1
    assert client.post(url).status_code == 200
    assert world.ledger(campaign).count("seat.confirmed") == 1
    refused = client.post(f"/campaigns/{campaign}/participants/{open_seat}/confirm")
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "seat_not_accepted")
    _remove(client, campaign, seat)
    assert client.post(url).json() == NOT_FOUND


# ── Remove, behind the password (SEC-40, RQ-5) ───────────────────────────────


def test_a_wrong_password_is_reauth_failed_and_nothing_is_opened(
    client: TestClient, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = world.campaign()
    seat = world.seat(campaign)
    opened: list[object] = []
    real = world.db.transaction

    def counting() -> Any:
        opened.append(1)
        return real()

    monkeypatch.setattr(world.db, "transaction", counting)
    refused = _remove(client, campaign, seat, password="wrong " + CANARY)
    assert refused.status_code == 403 and refused.json() == {"detail": dict(REAUTH_FAILED_DETAIL)}
    assert opened == [], "no transaction opens before the password checks out"
    assert CANARY not in refused.text


def test_the_auth_throttle_and_a_hashing_outage_answer_in_the_workbench_envelope(
    client: TestClient, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = world.campaign()
    seat = world.seat(campaign)

    def throttle(request: object, account: str) -> None:
        raise RateLimited(42)

    monkeypatch.setattr(appmod, "check_auth_attempt", throttle)
    throttled = _remove(client, campaign, seat)
    body = ErrorBody.model_validate(throttled.json())
    assert throttled.status_code == 429 and body.detail.code.value == "throttled_user"
    assert body.detail.retry_after_s == 42 and throttled.headers["x-auth-throttled"] == "1"
    monkeypatch.undo()

    def busy(stored: str, password: str) -> bool:
        raise HashingCapacityError("busy")

    monkeypatch.setattr(appmod, "verify_password", busy)
    shed = _remove(client, campaign, seat)
    assert shed.status_code == 503 and shed.json()["detail"]["code"] == "backend_unavailable"
    monkeypatch.undo()

    class _Down:
        def get_credentials(self, email: str) -> Any:
            raise psycopg.OperationalError("down " + CANARY)

    app.dependency_overrides[get_auth_store] = lambda: _Down()
    outage = _remove(client, campaign, seat)
    assert outage.status_code == 503 and CANARY not in outage.text
    with world.db.transaction() as unit:
        assert world.stores.participants.get(unit, seat).is_active  # type: ignore[union-attr]


def test_remove_narrows_withdraws_audits_and_leaves_one_reconciliation(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    seat = world.seat(campaign)
    assert _offer(client, campaign, seat, "wren@example.com").status_code == 204
    with world.db.transaction() as unit:
        session, _ = world.stores.sessions.start(unit, campaign, owner_id=GM_A, expires_at=T0 + timedelta(hours=12),
                                                 now=T0)
    removed = _remove(client, campaign, seat)
    assert (removed.status_code, removed.content) == (204, b"")
    with world.db.transaction() as unit:
        assert not world.stores.participants.get(unit, seat).is_active  # type: ignore[union-attr]
        narrowed = world.stores.sessions.get(unit, session.id)
    assert narrowed is not None and narrowed.reveal_epoch == session.reveal_epoch + 1
    assert [o.outcome for o in world.offers()] == ["withdrawn"]
    assert world.ledger(campaign)[-1] == "participant.removed"
    rows = sorted(world.jobs._rows.values(), key=lambda row: row.job.id)
    assert [(r.job.kind, dict(r.job.payload), r.dedupe_key) for r in rows] == [
        ("campaign.reconcile", {"campaign_id": campaign}, None)
    ]
    assert world.driver.ran == [rows[0].job.id], "handed to run_after_response, never run in the request"
    assert world.revision(campaign) == 0, "Remove never takes the lock; the job advances the revision"
    again = _remove(client, campaign, seat)
    assert again.status_code == 204
    assert world.ledger(campaign).count("participant.removed") == 1 and len(world.jobs._rows) == 1


def test_remove_is_allowed_on_an_archived_campaign(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    seat = world.seat(campaign)
    client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": True})
    assert _remove(client, campaign, seat).status_code == 204


# ── T-2: the one 404, across tenants ─────────────────────────────────────────


def _every_gm_route(campaign: str, seat: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    base = f"/campaigns/{campaign}"
    return [
        ("GET", base, None),
        ("PATCH", base, {"schema_version": 1, "archived": True}),
        ("PATCH", base, {"schema_version": 1, "name": "Mine"}),
        ("PATCH", base, {"schema_version": 1, "archived": False}),
        ("PATCH", base, {"schema_version": 1, "tone": "Grim"}),
        ("POST", f"{base}/conclude", None),
        ("POST", f"{base}/reopen", None),
        ("GET", f"{base}/participants", None),
        ("POST", f"{base}/participants", {"schema_version": 1, "alias": "Rook"}),
        ("POST", f"{base}/participants/{seat}/offer", {"schema_version": 1, "email": "x@example.com"}),
        ("POST", f"{base}/participants/{seat}/confirm", None),
        ("POST", f"{base}/participants/{seat}/remove", {"schema_version": 1, "password": PASSWORD[GM_B]}),
    ]


def test_every_gm_route_answers_a_stranger_with_the_one_identical_404(client: TestClient, world: _World) -> None:
    """Another GM's campaign (archived, at the seat cap, at the throttle — so
    that no 409 or 429 can leak), a missing one and a malformed one: one
    answer, byte for byte, on every route."""
    campaign = world.campaign()
    seat = world.accepted(campaign, "Rook", PLAYER, "wren@example.com")
    for n in range(campaigns_api.SEAT_CAP - 1):
        world.seat(campaign, f"Seat {n}")
    for n in range(THROTTLE_LIMIT):
        other = world.campaign(name=f"Busy {n}")
        _offer(client, other, world.seat(other), f"p{n}@example.com")
    client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "archived": True})
    before = (world.ledger(campaign), world.revision(campaign))
    _as(GM_B)
    shapes = set()
    for target_campaign, target_seat in (
        (campaign, seat),
        (MISSING_CAMPAIGN, MISSING_SEAT),
        ("cmp_short", "prt_x"),
        (campaign, MISSING_SEAT),
    ):
        for method, path, body in _every_gm_route(target_campaign, target_seat):
            answer = client.request(method, path, json=body) if body is not None else client.request(method, path)
            assert answer.json() == NOT_FOUND, (method, path, answer.text)
            shapes.add(_shape(answer))
    assert len(shapes) == 1
    assert (world.ledger(campaign), world.revision(campaign)) == before


# ── The posture every route inherits ─────────────────────────────────────────


def test_every_gm_route_answers_the_one_401_a_player_403_and_a_foreign_origin_403(
    client: TestClient, world: _World
) -> None:
    campaign, seat = world.campaign(), MISSING_SEAT
    routes = [("GET", "/campaigns", None), ("POST", "/campaigns", {"schema_version": 1, "name": "N"}),
              *_every_gm_route(campaign, seat)]
    _signed_out()
    for method, path, body in routes:
        assert client.request(method, path, json=body).json() == UNAUTHENTICATED, (method, path)
    _as(PLAYER, "player")
    for method, path, body in routes:
        refused = client.request(method, path, json=body)
        assert (refused.status_code, refused.json()["detail"]["code"]) == (403, "forbidden"), (method, path)
    _as(GM_A)
    for method, path, body in routes:
        if method == "GET":
            continue
        foreign = client.request(method, path, json=body, headers={"origin": "https://evil.example"})
        assert foreign.status_code == 403 and "application" in foreign.json()["detail"]["message"], (method, path)


def test_malformed_json_is_the_workbench_422_that_echoes_nothing(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    for path in ("/campaigns", f"/campaigns/{campaign}/participants"):
        broken = b'{"name": "' + CANARY.encode() + b'"'
        refused = client.post(path, content=broken, headers={"content-type": "application/json"})
        assert refused.status_code == 422 and CANARY not in refused.text
    refused = client.post(f"/campaigns/{campaign}/participants/{MISSING_SEAT}/offer",
                          json={"schema_version": 1, "email": CANARY, "user_id": 7})
    assert refused.status_code == 422 and CANARY not in refused.text


def test_a_database_outage_is_a_503_on_every_route(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    app.dependency_overrides[get_timeline_database] = lambda: None
    for method, path, body in [("GET", "/campaigns", None), ("POST", "/campaigns", {"schema_version": 1, "name": "N"}),
                               *_every_gm_route(campaign, MISSING_SEAT)]:
        answer = client.request(method, path, json=body)
        assert answer.status_code == 503 and answer.json()["detail"]["retryable"] is True, (method, path)


def test_a_driver_error_is_a_503_that_logs_its_type_only(
    client: TestClient, world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    class _Broken:
        def page_for_owner(self, *args: Any, **kwargs: Any) -> Any:
            raise psycopg.OperationalError("statement quoting " + CANARY)

    app.dependency_overrides[campaigns_api.get_campaign_stores] = lambda: campaigns_api.CampaignStores(
        _Broken(), world.stores.participants, world.stores.sessions, world.stores.offers, world.stores.audit,  # type: ignore[arg-type]
        world.stores.summaries,
    )
    with caplog.at_level(logging.DEBUG):
        answer = client.get("/campaigns")
    assert answer.status_code == 503 and CANARY not in answer.text
    assert CANARY not in caplog.text and "OperationalError" in caplog.text


# ── The OpenAPI document exposes no secrets ──────────────────────────────────


def test_the_openapi_models_of_this_family_expose_no_secret(client: TestClient) -> None:
    document = app.openapi()
    family = {path: spec for path, spec in document["paths"].items() if path.startswith(("/campaigns", "/seats"))}
    # 1kg.2.2's ten, and bead cfx's conclude and reopen.
    assert len(family) == 12
    schemas = document["components"]["schemas"]
    answered = {"Campaign", "CampaignPage", "Seat", "SeatPage", "SeatOffer", "SeatOfferPage", "PlayerSeat",
                "PlayerSeatPage"}
    assert answered <= set(schemas), "every answer of the family is a documented model"
    fields = {name: set(schemas[name].get("properties", {})) for name in answered}
    every = set().union(*fields.values())
    assert not every & {"password", "user_id", "owner_id", "gm_email", "offered_by"}
    for account_side in ("SeatOffer", "SeatOfferPage", "PlayerSeat", "PlayerSeatPage"):
        assert not fields[account_side] & {"address", "email", "participant_id"}, account_side
    # bead cfx: a seated card says nothing about other players and nothing of
    # the GM's private prep, which the owner's card does.
    private = {"seat_count", "badge", "last_activity_at", "dormant", "concluded_at"}
    assert private <= fields["Campaign"]
    assert not fields["PlayerSeat"] & private
    remove = SeatRemoveRequest.model_json_schema()["properties"]["password"]
    assert remove.get("writeOnly") is True


# ── Private text never escapes (AC10) ────────────────────────────────────────


def test_no_private_value_reaches_an_exception_a_log_an_audit_row_or_a_job(
    client: TestClient, world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    alias, name, address, password = "Aa" + CANARY, "Nn" + CANARY, "ee" + CANARY + "@example.com", "pp" + CANARY
    campaign = world.campaign(name=name)
    seat = world.seat(campaign, alias)
    with caplog.at_level(logging.DEBUG):
        answers = [
            client.post("/campaigns", json={"schema_version": 1, "name": name + "\u0000"}),
            client.patch(f"/campaigns/{campaign}", json={"schema_version": 1, "name": name, "user_id": 7}),
            client.post(f"/campaigns/{campaign}/participants", json={"schema_version": 1, "alias": alias}),
            _offer(client, campaign, seat, address),
            _offer(client, campaign, seat, address + " x"),
            _offer(client, campaign, MISSING_SEAT, address),
            _remove(client, campaign, seat, password=password),
            _remove(client, MISSING_CAMPAIGN, seat, password=password),
            client.post(f"/campaigns/{campaign}/participants/{seat}/confirm"),
            client.get("/campaigns", params={"cursor": CANARY}),
        ]
        _as(GM_B)
        answers += [_offer(client, campaign, seat, address), _remove(client, campaign, seat, password=password)]
        _as(PLAYER, "player")
        answers.append(client.get("/seats/offers"))
    for answer in answers:
        for secret in (alias, name, address, password):
            assert secret not in answer.text
    for record in caplog.records:
        rendered = record.getMessage() + str(record.exc_info or "") + str(record.__dict__)
        for secret in (alias, name, address, password):
            assert secret not in rendered
    with world.db.transaction() as unit:
        events = world.stores.audit.for_campaign(unit, campaign)
    for event in events:
        assert CANARY not in str(event) + str(dict(event.detail))
    for row in world.jobs._rows.values():
        assert CANARY not in str(dict(row.job.payload))

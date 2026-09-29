"""An account's own offers and seats (bead 1kg.2.2): `/seats`, on `account_router`.

The invitee half of T-24: offers are shown to, and accepted by, only a Verified
holder of the offered address (SEC-50(4)), and on this build nobody is Verified
(L-8), so the real app fails closed — which the first tests pin with no
override at all. The rest override the verified-address dependency, as
`agent-forge-harness-yje.2.1` will make production answer, and walk accept,
decline and block through the real routes over the in-memory twins.

Run from the repo root:
    uv run python -m pytest service/tests/test_seats_api.py -q
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response

import config
from service import seats_api
from service.app import app, get_auth_store, get_timeline_database, require_session
from service.auth_store import User, verified_address
from service.invites import Role
from service.session import SessionData, encode_session
from service.tests.test_campaigns_api import (
    EMAIL,
    GM_A,
    GM_B,
    NOT_FOUND,
    PLAYER,
    T0,
    UNAUTHENTICATED,
    _as,
    _offer,
    _remove,
    _seats,
    _shape,
    _signed_out,
    _World,
    world,
)
from service.workbench_contracts import PlayerSeat, PlayerSeatPage, SeatOfferPage

__all__ = ["world"]  # the fixture, imported so that this module's tests get it

WREN = "Wren@Example.com"
OFFER = "sof_" + "z" * 22


@pytest.fixture
def client(world: _World) -> Iterator[TestClient]:
    _as(GM_A)
    yield TestClient(app)
    app.dependency_overrides.pop(seats_api.verified_address_of, None)


def _verified(address: str | None) -> None:
    app.dependency_overrides[seats_api.verified_address_of] = lambda: address


def _offers(client: TestClient) -> list[dict[str, Any]]:
    answer = client.get("/seats/offers")
    assert answer.status_code == 200, answer.text
    SeatOfferPage.model_validate(answer.json())
    return list(answer.json()["items"])


def _accept(client: TestClient, offer_id: str) -> Response:
    return client.post(f"/seats/offers/{offer_id}/accept")


def _decline(client: TestClient, offer_id: str, *, block: bool = False) -> Response:
    return client.post(f"/seats/offers/{offer_id}/decline", json={"schema_version": 1, "block": block})


def _offered(client: TestClient, world: _World, address: str = WREN, *, owner: int = GM_A, name: str = "Nocturne",
             alias: str = "Rook") -> tuple[str, str]:
    campaign = world.campaign(owner=owner, name=name)
    seat = world.seat(campaign, alias)
    _as(owner)
    assert _offer(client, campaign, seat, address).status_code == 204
    return campaign, seat


def _offer_id(world: _World, seat: str) -> str:
    return next(o.id for o in world.offers() if o.participant_id == seat)


# ── The seam fails closed on the real app (L-8) ──────────────────────────────


def test_the_verified_address_is_none_for_every_account_on_this_build() -> None:
    for role in ("dm", "player"):
        assert verified_address(User(id=9, email=EMAIL[PLAYER], role=role, created_at=T0)) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["dm", "player"])
def test_the_real_app_with_no_override_and_no_database_fails_closed(role: Role) -> None:
    app.dependency_overrides[get_timeline_database] = lambda: None
    try:
        _as(PLAYER, role)
        client = TestClient(app)
        listed = client.get("/seats/offers")
        assert (listed.status_code, listed.json()) == (200, {"schema_version": 1, "items": [], "next_cursor": None})
        assert _accept(client, OFFER).json() == NOT_FOUND
        assert _decline(client, OFFER, block=True).json() == NOT_FOUND
        assert client.get("/seats").status_code == 503, "the player's own seats do not consult the seam"
    finally:
        app.dependency_overrides.pop(get_timeline_database, None)


def test_an_unverified_holder_of_the_address_sees_nothing_until_it_is_verified(
    client: TestClient, world: _World
) -> None:
    _, seat = _offered(client, world)
    offer = _offer_id(world, seat)
    _as(PLAYER, "player")
    assert _offers(client) == [] and _accept(client, offer).json() == NOT_FOUND
    _verified(WREN.lower())
    assert [o["offer_id"] for o in _offers(client)] == [offer]


# ── Accept ───────────────────────────────────────────────────────────────────


def test_accepting_binds_the_seat_to_the_caller_unconfirmed_and_a_repeat_changes_nothing(
    client: TestClient, world: _World
) -> None:
    campaign, seat = _offered(client, world)
    offer = _offer_id(world, seat)
    _as(PLAYER, "player")
    _verified("wren@example.com")
    listed = _offers(client)
    assert listed == [{"schema_version": 1, "offer_id": offer, "campaign_name": "Nocturne", "alias": "Rook",
                       "offered_at": "2026-09-01T12:00:00Z", "expires_at": "2026-09-15T12:00:00Z"}]
    before = world.revision(campaign)
    accepted = _accept(client, offer)
    assert accepted.status_code == 200
    body = PlayerSeat.model_validate(accepted.json())
    assert (body.campaign_id, body.alias, body.confirmed) == (campaign, "Rook", False)
    assert "participant_id" not in accepted.json()
    assert world.revision(campaign) == (before or 0) + 1
    assert world.ledger(campaign)[-1] == "seat.accepted"
    again = _accept(client, offer)
    assert (again.status_code, again.json()) == (200, accepted.json())
    assert world.ledger(campaign).count("seat.accepted") == 1
    seats = client.get("/seats").json()
    PlayerSeatPage.model_validate(seats)
    assert [s["campaign_id"] for s in seats["items"]] == [campaign] and seats["items"][0]["confirmed"] is False
    assert _offers(client) == []
    _as(GM_A)
    gm_view = {s["participant_id"]: s for s in _seats(client, campaign)}[seat]
    assert (gm_view["status"], gm_view["address"]) == ("awaiting_confirmation", WREN)
    assert client.post(f"/campaigns/{campaign}/participants/{seat}/confirm").status_code == 200
    _as(PLAYER, "player")
    assert client.get("/seats").json()["items"][0]["confirmed"] is True


# ── Decline and block ────────────────────────────────────────────────────────


def test_decline_shows_the_gm_not_accepted_and_a_block_drops_that_gms_later_offers(
    client: TestClient, world: _World
) -> None:
    campaign, seat = _offered(client, world)
    offer = _offer_id(world, seat)
    _as(PLAYER, "player")
    _verified(WREN)
    declined = _decline(client, offer, block=True)
    assert (declined.status_code, declined.content) == (204, b"")
    assert _shape(_decline(client, offer)) == _shape(declined), "a repeat decline is the same 204"
    assert world.ledger(campaign)[-1] == "seat.declined"
    _as(GM_A)
    assert {s["participant_id"]: s["status"] for s in _seats(client, campaign)}[seat] == "not_accepted"
    later = world.seat(campaign, "Later")
    assert _offer(client, campaign, later, WREN).status_code == 204
    assert {s["participant_id"]: s["status"] for s in _seats(client, campaign)}[later] == "offered"
    _as(PLAYER, "player")
    assert _offers(client) == [], "a blocked owner's offers are listed to nobody"
    assert _accept(client, _offer_id(world, later)).json() == NOT_FOUND
    world.now[0] = T0 + timedelta(days=15)
    _as(GM_A)
    assert {s["participant_id"]: s["status"] for s in _seats(client, campaign)}[later] == "not_accepted"
    _, elsewhere = _offered(client, world, owner=GM_B, name="Theirs")
    _as(PLAYER, "player")
    assert [o["offer_id"] for o in _offers(client)] == [_offer_id(world, elsewhere)], "another GM is not blocked"


def test_a_repeat_decline_applies_a_block_it_now_asks_for(client: TestClient, world: _World) -> None:
    _, seat = _offered(client, world)
    offer = _offer_id(world, seat)
    _as(PLAYER, "player")
    _verified(WREN)
    assert _decline(client, offer).status_code == 204
    assert _decline(client, offer, block=True).status_code == 204
    with world.db.transaction() as unit:
        assert world.stores.offers.is_blocked(unit, PLAYER, GM_A)


# ── Every refusal on the account routes is one identical 404 ─────────────────


def test_every_account_refusal_is_one_identical_404(client: TestClient, world: _World) -> None:
    """Missing, someone else's, expired, withdrawn, archived, blocked, already
    seated, and the caller being the campaign's owner.

    Only the expired offer has expired (R145-M1): it is made a week before the
    others, and the clock stops a week short of theirs running out. So each
    invitee predicate — archived, the owner, blocked, already seated — refuses
    an offer that is still live, and dropping any one of them fails here."""
    refused: dict[str, str] = {"missing": OFFER}
    _, expiring = _offered(client, world, name="Expired")
    refused["expired"] = _offer_id(world, expiring)
    world.now[0] = T0 + timedelta(days=7)
    _, theirs = _offered(client, world, "finch@example.com", name="Someone else's")
    refused["someone else's"] = _offer_id(world, theirs)
    withdrawn_campaign, withdrawn = _offered(client, world, name="Withdrawn")
    refused["withdrawn"] = _offer_id(world, withdrawn)
    assert _remove(client, withdrawn_campaign, withdrawn).status_code == 204
    archived_campaign, archived = _offered(client, world, name="Archived")
    refused["archived"] = _offer_id(world, archived)
    archiving = client.patch(f"/campaigns/{archived_campaign}", json={"schema_version": 1, "archived": True})
    assert archiving.status_code == 200, archiving.text
    _, blocked = _offered(client, world, owner=GM_B, name="Blocked")
    refused["blocked"] = _offer_id(world, blocked)
    with world.db.transaction() as unit:
        world.stores.offers.block(unit, blocker_user_id=PLAYER, blocked_owner_id=GM_B)
    seated_campaign = world.campaign(name="Seated")
    world.accepted(seated_campaign, "Mine", PLAYER, WREN)
    second = world.seat(seated_campaign, "Second")
    _as(GM_A)
    assert _offer(client, seated_campaign, second, "wren.alt@example.com").status_code == 204
    refused["already seated"] = _offer_id(world, second)
    own_campaign, own = _offered(client, world, "gm.alt@example.com", name="Own")
    refused["the owner"] = _offer_id(world, own)
    world.now[0] = T0 + timedelta(days=14)
    _, fresh = _offered(client, world, name="Fresh")
    offers = {o.id: o for o in world.offers()}
    live = {reason for reason, offer in refused.items() if offer in offers and offers[offer].is_live(world.now[0])}
    assert live == {"someone else's", "archived", "blocked", "already seated", "the owner"}, "a predicate refuses these"
    lapsed = offers[refused["expired"]]
    assert (lapsed.outcome, lapsed.expires_at <= world.now[0]) == (None, True), "only time refuses the expired one"
    assert offers[refused["withdrawn"]].outcome == "withdrawn"
    shapes = set()
    for reason, offer in refused.items():
        address = {"already seated": "wren.alt@example.com", "the owner": "gm.alt@example.com"}.get(reason, WREN)
        _as(GM_A if reason == "the owner" else PLAYER, "dm" if reason == "the owner" else "player")
        _verified(address)
        for answer in (_accept(client, offer), _decline(client, offer, block=True)):
            assert answer.json() == NOT_FOUND, reason
            shapes.add(_shape(answer))
    assert len(shapes) == 1
    _as(PLAYER, "player")
    _verified(WREN)
    assert [o["offer_id"] for o in _offers(client)] == [_offer_id(world, fresh)]
    del own_campaign


# ── The player's seat list ───────────────────────────────────────────────────


def test_the_seat_list_cursor_decodes_to_no_participant_id(client: TestClient, world: _World) -> None:
    """L-9 and SEC-43 (R145-M2): `next_cursor` on `GET /seats` names no
    participant, not even the caller's own. It carries the acceptance time and
    the campaign id, which the page already shows, and it walks on."""
    first, second = world.campaign(name="First"), world.campaign(name="Second")
    seats = [world.accepted(first, "Rook", PLAYER, WREN)]
    world.now[0] = T0 + timedelta(minutes=1)
    seats.append(world.accepted(second, "Wren", PLAYER, WREN))
    _as(PLAYER, "player")
    page = client.get("/seats", params={"limit": "1"})
    assert page.status_code == 200, page.text
    assert [s["campaign_id"] for s in page.json()["items"]] == [second]
    cursor = page.json()["next_cursor"]
    assert isinstance(cursor, str)
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert [v for v in decoded if str(v).startswith("prt_")] == [], "the cursor decodes to a participant id"
    assert [s for s in seats if s in json.dumps(decoded)] == []
    assert decoded == [(T0 + timedelta(minutes=1)).isoformat(), second]
    rest = client.get("/seats", params={"limit": "1", "cursor": cursor}).json()
    assert ([s["campaign_id"] for s in rest["items"]], rest["next_cursor"]) == ([first], None)


# ── The account router's posture ─────────────────────────────────────────────


def test_a_player_reaches_seats_and_is_refused_on_campaigns(client: TestClient, world: _World) -> None:
    _as(PLAYER, "player")
    assert client.get("/seats").status_code == 200
    refused = client.get("/campaigns")
    assert (refused.status_code, refused.json()["detail"]["code"]) == (403, "forbidden")


def test_every_authentication_failure_on_seats_is_the_one_401(client: TestClient) -> None:
    _signed_out()
    for method, path, body in (
        ("GET", "/seats", None),
        ("GET", "/seats/offers", None),
        ("POST", f"/seats/offers/{OFFER}/accept", None),
        ("POST", f"/seats/offers/{OFFER}/decline", {"schema_version": 1, "block": False}),
    ):
        assert client.request(method, path, json=body).json() == UNAUTHENTICATED, path


def test_a_foreign_origin_and_a_bad_body_are_refused_on_the_account_writes(client: TestClient) -> None:
    _as(PLAYER, "player")
    _verified(WREN)
    foreign = client.post(f"/seats/offers/{OFFER}/accept", headers={"origin": "https://evil.example"})
    assert foreign.status_code == 403
    bad = client.post(f"/seats/offers/{OFFER}/decline", json={"schema_version": 1, "block": "yes", "user_id": 7})
    assert bad.status_code == 422
    assert client.get("/seats", params={"limit": "0"}).status_code == 422


@pytest.mark.real_auth
def test_an_auth_store_outage_on_seats_is_still_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "a-test-secret-that-is-long-enough-to-sign-with"
    monkeypatch.setattr(config, "SESSION_SECRET", secret)

    class _Down:
        def get_user_by_id(self, user_id: int) -> Any:
            raise psycopg.OperationalError("down")

    app.dependency_overrides[get_auth_store] = lambda: _Down()
    app.dependency_overrides.pop(require_session, None)
    try:
        client = TestClient(app)
        client.cookies.set(config.SESSION_COOKIE_NAME, encode_session(SessionData(user_id=3, role="player"), secret))
        assert client.get("/seats").status_code == 503
    finally:
        app.dependency_overrides.pop(get_auth_store, None)

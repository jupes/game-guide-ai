"""The conversation routes (1kg.2.4 A2): `GET|POST /conversations`,
`GET|PATCH /conversations/{id}`.

Everything runs through the app's test client against the in-memory twins, which
share one set of tables — so a campaign the twin does not know is refused here
exactly as the statement refuses it in PostgreSQL. What only PostgreSQL can
prove (affinity binding after a create, a legacy row read back, the collation
of the tie-break) is `tests/test_conversation_db.py`'s.

Run from the repo root:
    uv run python -m pytest service/tests/test_conversations_api.py -q
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from httpx import Response
from starlette.requests import Request

from service import conversations_api
from service.app import app, get_timeline_database, require_session
from service.campaign_store import InMemoryCampaignStore, Staging
from service.campaign_store import shared_rows as twin_table
from service.conversation_store import Conversation as StoredConversation
from service.conversation_store import InMemoryConversationStore
from service.db import InMemoryDatabase
from service.invites import Role
from service.session import SessionData
from service.tests.test_auth_guard import PROTECTED_ROUTES
from service.workbench_contracts import Conversation, ConversationPage, ErrorBody

OWNER = 1
STRANGER = 2
CANARY = "Zx9CanaryQ7"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

WIRE_KEYS = {
    "schema_version",
    "conversation_id",
    "campaign_id",
    "title",
    "started_mode",
    "created_at",
    "updated_at",
    "archived_at",
}
NOT_FOUND = {"detail": {"code": "not_found", "message": "That conversation isn't available.", "retryable": False}}
FORBIDDEN_ROLE = {"detail": {"code": "forbidden", "message": "This is a Game Master feature.", "retryable": False}}
FORBIDDEN_ORIGIN = {
    "detail": {"code": "forbidden", "message": "That request didn't come from this application.", "retryable": False}
}
UNAVAILABLE = {
    "detail": {
        "code": "backend_unavailable",
        "message": "Conversations are briefly unavailable. Try again.",
        "retryable": True,
    }
}
ALREADY_LINKED = {
    "detail": {
        "code": "already_linked",
        "message": "That conversation is already in a campaign.",
        "retryable": False,
        "field": "campaign_id",
    }
}


@dataclass
class _World:
    db: InMemoryDatabase
    campaigns: InMemoryCampaignStore
    conversations: InMemoryConversationStore

    def campaign(self, owner: int = OWNER, *, archived: bool = False) -> str:
        with self.db.transaction() as unit:
            made = self.campaigns.create(unit, owner_id=owner, name="Nocturne")
        if archived:
            with self.db.transaction() as unit:
                self.campaigns.set_archived(unit, made.id, owner_id=owner, archived=True)
        return str(made.id)

    def conversation(
        self,
        owner: int = OWNER,
        *,
        campaign_id: str | None = None,
        title: str | None = None,
        started_mode: str | None = "sage",
        now: datetime | None = None,
    ) -> StoredConversation:
        with self.db.transaction() as unit:
            return self.conversations.create(
                unit, owner_id=owner, campaign_id=campaign_id, title=title, started_mode=started_mode, now=now
            )

    def legacy(self, conversation_id: str, owner: int = OWNER, *, now: datetime = T0) -> str:
        """A row this bead's store did not create: a client-minted id, nothing
        recorded — what every conversation in production is today."""
        rows: Staging[StoredConversation] = twin_table(self.db, "conversations")
        with self.db.transaction() as unit:
            rows.add(
                unit,
                conversation_id,
                StoredConversation(
                    id=conversation_id,
                    owner_id=owner,
                    campaign_id=None,
                    title=None,
                    started_mode=None,
                    created_at=now,
                    updated_at=None,
                    archived_at=None,
                ),
            )
        return conversation_id

    def stored(self, conversation_id: str) -> StoredConversation | None:
        with self.db.transaction() as unit:
            return twin_table(self.db, "conversations").visible(unit).get(conversation_id)

    def row_count(self) -> int:
        with self.db.transaction() as unit:
            return len(twin_table(self.db, "conversations").visible(unit))


@pytest.fixture
def world() -> _World:
    db = InMemoryDatabase()
    return _World(db, InMemoryCampaignStore(db), InMemoryConversationStore(db))


@pytest.fixture
def client(world: _World) -> Iterator[TestClient]:
    # A plain client, never the context manager: entering it runs the lifespan,
    # which dials a database that is not there.
    app.dependency_overrides[conversations_api.get_conversation_store] = lambda: world.conversations
    app.dependency_overrides[get_timeline_database] = lambda: world.db
    yield TestClient(app)
    app.dependency_overrides.pop(conversations_api.get_conversation_store, None)
    app.dependency_overrides.pop(get_timeline_database, None)


def _as(user_id: int, role: Role = "dm") -> None:
    """Every later request in the test is this account's."""
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=user_id, role=role)


def _signed_out() -> None:
    def refuse() -> SessionData:
        raise HTTPException(status_code=401, detail="authentication required")

    app.dependency_overrides[require_session] = refuse


def _create(client: TestClient, **body: Any) -> Response:
    return client.post("/conversations", json={"schema_version": 1, "started_mode": "sage", **body})


def _patch(client: TestClient, conversation_id: str, **body: Any) -> Response:
    return client.patch(f"/conversations/{quote(conversation_id, safe='')}", json={"schema_version": 1, **body})


def _read(client: TestClient, conversation_id: str) -> Response:
    return client.get(f"/conversations/{quote(conversation_id, safe='')}")


def _ids(response: Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["conversation_id"] for item in response.json()["items"]]


_WALK_BOUND = 200


def _walk(client: TestClient, **params: str) -> list[str]:
    """Every id the index pages through, bounded so a stuck cursor fails."""
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(_WALK_BOUND):
        page = client.get("/conversations", params=params | ({"cursor": cursor} if cursor else {}))
        assert page.status_code == 200, page.text
        body = page.json()
        assert "next_cursor" in body
        seen.extend(item["conversation_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("the cursor walk did not terminate")


def _shape(response: Response) -> tuple[Any, ...]:
    varying = {"date", "content-length", "server"}
    return (
        response.status_code,
        response.text,
        tuple(sorted((k.lower(), v) for k, v in response.headers.items() if k.lower() not in varying)),
    )


# ── A2: a created conversation is in its owner's index at once ───────────────


def test_a_created_conversation_is_in_its_owners_index_at_once(client: TestClient) -> None:
    made = _create(client, started_mode="gm", title="Session zero")
    assert made.status_code == 201, made.text
    body = made.json()
    Conversation.model_validate(body)
    assert _ids(client.get("/conversations")) == [body["conversation_id"]]
    again = _read(client, body["conversation_id"])
    assert again.status_code == 200
    assert again.json() == body


def test_a_create_answers_every_key_and_a_server_minted_id(client: TestClient) -> None:
    body = _create(client).json()
    assert set(body) == WIRE_KEYS, "every key present, and nothing of the owner or of model routing"
    assert body["conversation_id"].startswith("cnv_") and len(body["conversation_id"]) == 26
    assert body["started_mode"] == "sage"
    assert body["campaign_id"] is None and body["title"] is None
    assert body["updated_at"] is None and body["archived_at"] is None
    assert body["created_at"].endswith("Z"), "the server emits UTC with Z"


def test_a_conversation_can_be_created_inside_the_callers_campaign(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    body = _create(client, started_mode="gm", campaign_id=campaign).json()
    assert body["campaign_id"] == campaign
    assert _ids(client.get("/conversations", params={"campaign_id": campaign})) == [body["conversation_id"]]


@pytest.mark.parametrize("which", ["missing", "foreign", "archived"])
def test_a_campaign_that_is_not_a_live_one_of_the_callers_is_the_one_404(
    client: TestClient, world: _World, which: str
) -> None:
    """§8.1 row 307, "404 by campaign": a missing and a foreign campaign are one
    answer, and nothing is written."""
    campaign = {
        "missing": "cmp_" + "z" * 22,
        "foreign": world.campaign(STRANGER),
        "archived": world.campaign(archived=True),
    }[which]
    answer = _create(client, started_mode="gm", campaign_id=campaign)
    assert (answer.status_code, answer.json()) == (404, NOT_FOUND)
    assert world.row_count() == 0


def test_a_title_is_stored_trimmed(client: TestClient, world: _World) -> None:
    body = _create(client, title="  Harbour at night \t").json()
    assert body["title"] == "Harbour at night"
    stored = world.stored(body["conversation_id"])
    assert stored is not None and stored.title == "Harbour at night"


def test_a_retried_create_makes_a_second_conversation(client: TestClient) -> None:
    """Ruling 2.4#5: no idempotency key. The consequence is stated, not hidden:
    a retry is a second conversation, which archive recovers."""
    first, second = _create(client).json(), _create(client).json()
    assert first["conversation_id"] != second["conversation_id"]
    assert len(_ids(client.get("/conversations"))) == 2


def test_a_campaign_conversation_must_be_started_in_gm(client: TestClient, world: _World) -> None:
    answer = _create(client, started_mode="sage", campaign_id=world.campaign())
    assert answer.status_code == 422
    assert answer.json()["detail"]["field"] == "campaign_id"
    assert world.row_count() == 0


# ── A2-3: the order of the checks ────────────────────────────────────────────


def test_the_writes_check_origin_then_session_then_body_then_role_then_store(
    client: TestClient, world: _World
) -> None:
    foreign = {"origin": "https://evil.example", "sec-fetch-site": "cross-site"}
    mine = world.conversation().id
    writes = [
        lambda body, **kw: client.post("/conversations", json=body, **kw),
        lambda body, **kw: client.patch(f"/conversations/{mine}", json=body, **kw),
    ]
    good = [{"schema_version": 1, "started_mode": "sage"}, {"schema_version": 1, "archived": True}]
    bad = {"schema_version": 1, "started_mode": "combat", "archived": "yes"}
    for write, body in zip(writes, good, strict=True):
        _signed_out()
        assert write(body, headers=foreign).json() == FORBIDDEN_ORIGIN, "1. SEC-7 before authentication"
        assert write(bad).status_code == 401, "2. authentication before the body"
        _as(OWNER, "player")
        assert write(bad).status_code == 422, "3. the body before the role"
        assert write(body).json() == FORBIDDEN_ROLE, "4. the role before the store"
        app.dependency_overrides[get_timeline_database] = lambda: None
        assert write(body).json() == FORBIDDEN_ROLE, "4. the role before the store"
        _as(OWNER)
        assert write(body).json() == UNAVAILABLE, "5. the store before the path id and ownership"
        app.dependency_overrides[get_timeline_database] = lambda: world.db
    assert world.stored(mine) is not None and world.stored(mine).archived_at is None  # type: ignore[union-attr]
    assert world.row_count() == 1


def test_the_reads_check_session_then_query_then_role_then_store(client: TestClient) -> None:
    _signed_out()
    assert client.get("/conversations", params={"limit": "0"}).status_code == 401
    assert _read(client, "not an id").status_code == 401
    _as(OWNER, "player")
    assert client.get("/conversations", params={"limit": "0"}).status_code == 422
    assert client.get("/conversations").json() == FORBIDDEN_ROLE
    assert _read(client, "not an id").json() == FORBIDDEN_ROLE
    _as(OWNER)
    app.dependency_overrides[get_timeline_database] = lambda: None
    assert client.get("/conversations").json() == UNAVAILABLE
    assert _read(client, "not an id").json() == UNAVAILABLE, "the store before the path id"


def test_the_path_id_is_checked_after_the_store_and_answers_the_one_404(client: TestClient) -> None:
    for conversation_id in ("not an id", "x" * 65, "a.b"):
        assert (_read(client, conversation_id).json(), _read(client, conversation_id).status_code) == (NOT_FOUND, 404)
        answer = _patch(client, conversation_id, archived=True)
        assert (answer.status_code, answer.json()) == (404, NOT_FOUND)


# ── A5: never a claim ────────────────────────────────────────────────────────


def test_an_unknown_id_is_404_and_no_route_writes_a_row_for_it(client: TestClient, world: _World) -> None:
    unknown = "3f9a1b2c-7d4e-4f5a-9b8c-1d2e3f4a5b6c"
    before = world.row_count()
    assert _read(client, unknown).status_code == 404
    assert _patch(client, unknown, title="Mine now").status_code == 404
    assert _patch(client, unknown, started_mode="gm").status_code == 404
    assert world.row_count() == before, "a Workbench route never claims (SEC-2, §8.1)"
    assert world.stored(unknown) is None


# ── A6: no enumeration ───────────────────────────────────────────────────────


def _refused_ids(world: _World, *, viewer: int, other: int) -> dict[str, str]:
    """Every kind of id a viewer is refused, the other owner's included."""
    return {
        "missing": "cnv_" + "q" * 22,
        "foreign": world.conversation(other).id,
        "foreign legacy": world.legacy(f"3f9a1b2c-7d4e-4f5a-9b8c-{other:012d}", other),
        "foreign archived": _archived(world, other),
        "outside the grammar": "not an id",
        "too long": "c" * 65,
    }


def _archived(world: _World, owner: int) -> str:
    made = world.conversation(owner)
    with world.db.transaction() as unit:
        world.conversations.set_archived(unit, made.id, owner_id=owner, archived=True)
    return made.id


@pytest.mark.parametrize(("viewer", "other"), [(OWNER, STRANGER), (STRANGER, OWNER)])
def test_every_refused_id_gets_one_identical_404_on_every_id_route(
    client: TestClient, world: _World, viewer: int, other: int
) -> None:
    """T-2's matrix: two owners, every route that takes an id, every kind of id
    a caller is refused. Identical status, body and headers, except the three
    that vary per response. There is no per-request id header in this app."""
    refused = _refused_ids(world, viewer=viewer, other=other)
    _as(viewer)
    answers = []
    for conversation_id in refused.values():
        answers.append(_read(client, conversation_id))
        answers.append(_patch(client, conversation_id, archived=True))
        answers.append(_patch(client, conversation_id, campaign_id=world.campaign(viewer)))
    assert {a.status_code for a in answers} == {404}
    assert len({_shape(a) for a in answers}) == 1, "the 404s differ somewhere"
    assert answers[0].json() == NOT_FOUND


def test_the_404_is_built_in_one_place(client: TestClient, world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Why the matrix above cannot drift, and why the timing of the refusals is
    comparable: every one of them, the campaign's included, is `not_found()`."""
    refused = _refused_ids(world, viewer=OWNER, other=STRANGER)
    monkeypatch.setattr(conversations_api, "not_found", lambda: HTTPException(status_code=418, detail="marker"))
    for conversation_id in refused.values():
        assert _read(client, conversation_id).status_code == 418
        assert _patch(client, conversation_id, archived=True).status_code == 418
    mine = world.conversation(started_mode="gm").id
    assert _patch(client, mine, campaign_id=world.campaign(STRANGER)).status_code == 418
    assert _create(client, started_mode="gm", campaign_id="cmp_" + "z" * 22).status_code == 418


def test_a_would_be_409_aimed_at_a_foreign_conversation_is_404(client: TestClient, world: _World) -> None:
    """SEC-3: a 409 is unreachable for a conversation the caller does not own.
    The same well-formed body is a 409 against the caller's own."""
    theirs = world.conversation(STRANGER, campaign_id=world.campaign(STRANGER), started_mode="gm").id
    mine = world.conversation(OWNER, campaign_id=world.campaign(OWNER), started_mode="gm").id
    another = world.campaign(OWNER)
    conflict = _patch(client, mine, campaign_id=another)
    assert (conflict.status_code, conflict.json()) == (409, ALREADY_LINKED)
    answer = _patch(client, theirs, campaign_id=another)
    assert (answer.status_code, answer.json()) == (404, NOT_FOUND)


def test_a_422_that_depends_on_the_conversation_is_404_for_a_foreign_one(client: TestClient, world: _World) -> None:
    theirs = world.conversation(STRANGER, started_mode="sage").id
    mine = world.conversation(OWNER, started_mode="sage").id
    campaign = world.campaign(OWNER)
    assert _patch(client, mine, campaign_id=campaign).json()["detail"]["field"] == "campaign_id"
    assert _patch(client, theirs, campaign_id=campaign).json() == NOT_FOUND


def test_a_malformed_body_is_the_same_422_for_an_owned_and_a_foreign_conversation(
    client: TestClient, world: _World
) -> None:
    """Body validation depends on nothing but the body, so it may run first —
    and a 422 is therefore never evidence that a conversation exists."""
    mine, theirs = world.conversation(OWNER).id, world.conversation(STRANGER).id
    for body in ({}, {"title": None}, {"archived": "yes"}, {"title": "a\tb"}):
        answers = [_patch(client, target, **body) for target in (mine, theirs, "cnv_" + "q" * 22)]
        assert {a.status_code for a in answers} == {422}
        assert len({_shape(a) for a in answers}) == 1


# ── A7: 403 only for a role failure ──────────────────────────────────────────


def test_a_player_gets_the_same_403_on_every_route_and_it_names_nothing(client: TestClient, world: _World) -> None:
    """R-3: every new Workbench route needs the dm role. A player's 403 is the
    same whether the conversation exists, is theirs, or is anyone's, and a
    player's request writes nothing."""
    mine = world.conversation(OWNER, title=CANARY).id
    _as(OWNER, "player")
    answers = [
        client.get("/conversations"),
        _create(client, title=CANARY),
        _read(client, mine),
        _read(client, "cnv_" + "q" * 22),
        _patch(client, mine, title="Renamed"),
        _patch(client, "cnv_" + "q" * 22, title="Renamed"),
    ]
    assert {a.status_code for a in answers} == {403}
    assert len({_shape(a) for a in answers}) == 1
    assert answers[0].json() == FORBIDDEN_ROLE
    assert all(CANARY not in a.text and mine not in a.text for a in answers)
    assert world.row_count() == 1
    stored = world.stored(mine)
    assert stored is not None and stored.title == CANARY


# ── Reading one conversation ─────────────────────────────────────────────────


def test_a_client_minted_uuid_conversation_reads_with_nothing_recorded(client: TestClient, world: _World) -> None:
    """A4's route half: every id that works today keeps working, with no
    deprecation. The legacy row answers with every metadata key null."""
    legacy = world.legacy("0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d")
    answer = _read(client, legacy)
    assert answer.status_code == 200
    body = answer.json()
    Conversation.model_validate(body)
    assert body["conversation_id"] == legacy
    assert [body[k] for k in ("campaign_id", "title", "started_mode", "updated_at", "archived_at")] == [None] * 5
    assert _ids(client.get("/conversations")) == [legacy]


def test_an_archived_conversation_is_still_its_owners_to_read_and_patch(client: TestClient, world: _World) -> None:
    archived = _archived(world, OWNER)
    assert _read(client, archived).json()["archived_at"] is not None
    assert _patch(client, archived, title="Kept").json()["title"] == "Kept"
    assert _patch(client, archived, archived=False).json()["archived_at"] is None


# ── The index ────────────────────────────────────────────────────────────────


def test_the_index_is_the_callers_own_newest_first_and_every_filter_narrows_it(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    plain = world.conversation(started_mode="sage", now=T0).id
    gm = world.conversation(started_mode="gm", campaign_id=campaign, now=T0 + timedelta(hours=1)).id
    archived = _archived(world, OWNER)
    world.conversation(STRANGER)
    assert _ids(client.get("/conversations")) == [gm, plain]
    assert _ids(client.get("/conversations", params={"include_archived": "true"}))[0] == archived
    assert _ids(client.get("/conversations", params={"include_archived": "false"})) == [gm, plain]
    assert _ids(client.get("/conversations", params={"started_mode": "sage"})) == [plain]
    assert _ids(client.get("/conversations", params={"campaign_id": campaign})) == [gm]


@pytest.mark.parametrize("campaign", ["missing", "foreign"])
def test_a_campaign_filter_the_caller_does_not_own_is_an_empty_page(
    client: TestClient, world: _World, campaign: str
) -> None:
    theirs = world.campaign(STRANGER)
    world.conversation(STRANGER, campaign_id=theirs, started_mode="gm")
    world.conversation(OWNER)
    named = theirs if campaign == "foreign" else "cmp_" + "z" * 22
    answer = client.get("/conversations", params={"campaign_id": named})
    assert answer.status_code == 200
    assert answer.json() == {"schema_version": 1, "items": [], "next_cursor": None}


def test_the_index_pages_every_conversation_once(client: TestClient, world: _World) -> None:
    made = [world.conversation(now=T0 + timedelta(minutes=m)).id for m in range(5)]
    assert _walk(client, limit="2") == list(reversed(made))
    assert _walk(client, limit="1") == list(reversed(made))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("limit", CANARY),
        ("limit", "0"),
        ("limit", "101"),
        ("limit", "1.5"),
        ("limit", ""),
        ("cursor", ""),
        ("cursor", "a" * 513),
        ("cursor", CANARY + "/+="),
        ("include_archived", "TRUE"),
        ("include_archived", CANARY),
        ("started_mode", CANARY),
        ("campaign_id", CANARY + " "),
        ("campaign_id", "c" * 65),
    ],
)
def test_a_query_parameter_it_cannot_read_is_a_422_naming_the_field_and_never_the_value(
    client: TestClient, name: str, value: str
) -> None:
    answer = client.get("/conversations", params={name: value})
    assert answer.status_code == 422
    assert answer.json() == {
        "detail": {
            "code": "validation_failed",
            "message": "That request isn't valid.",
            "retryable": False,
            "field": name,
        }
    }
    assert CANARY not in answer.text


def test_a_cursor_of_the_right_shape_that_this_server_did_not_mint_is_a_422(client: TestClient) -> None:
    forged = base64.urlsafe_b64encode(CANARY.encode()).decode().rstrip("=")
    answer = client.get("/conversations", params={"cursor": forged})
    assert (answer.status_code, answer.json()["detail"]["field"]) == (422, "cursor")
    assert CANARY not in answer.text


def test_a_cursor_the_store_would_read_is_still_refused_past_the_contracts_bound(
    client: TestClient, world: _World
) -> None:
    """Carried from PR #85's review: the store decodes a well-formed cursor of any
    length, so the route is what enforces `Cursor`'s 1 to 512 characters."""
    world.conversation()
    raw = f'["{T0.isoformat()}","{"c" * 600}"]'.encode()
    forged = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert len(forged) > 512
    with world.db.transaction() as unit:
        world.conversations.list_for_owner(unit, OWNER, cursor=forged)  # the store reads it
    answer = client.get("/conversations", params={"cursor": forged})
    assert (answer.status_code, answer.json()["detail"]["field"]) == (422, "cursor")


def test_a_parameter_it_does_not_know_is_ignored_and_there_is_no_title_or_search_parameter(
    client: TestClient, world: _World
) -> None:
    """SEC-20: a title or a search never travels in a URL. The index has no such
    parameter, and one sent anyway changes nothing."""
    made = world.conversation(title=CANARY).id
    route = next(
        ctx.original_route
        for ctx in iter_route_contexts(app.routes)
        if isinstance(ctx.original_route, APIRoute)
        and ctx.path == "/conversations"
        and "GET" in (ctx.methods or set())
    )
    assert {param.name for param in route.dependant.query_params} == {
        "limit",
        "cursor",
        "include_archived",
        "started_mode",
        "campaign_id",
    }
    assert _ids(client.get("/conversations", params={"title": "nothing", "search": "nothing"})) == [made]


def test_a_cursor_carries_no_title(client: TestClient, world: _World) -> None:
    for minute in range(3):
        world.conversation(title=f"{CANARY} {minute}", now=T0 + timedelta(minutes=minute))
    cursor = client.get("/conversations", params={"limit": "1"}).json()["next_cursor"]
    assert cursor is not None
    decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    assert CANARY not in decoded


def test_a_stored_row_the_wire_cannot_carry_is_left_out_and_the_walk_still_ends(
    client: TestClient, world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    """Ruling A2-17. `/chat` accepted any string as an id once; a row whose id is
    outside `OpaqueId` cannot be carried, so it is omitted — the page is still
    200, the cursor is the store's, and only the count is logged."""
    readable = [world.conversation(now=T0 + timedelta(minutes=m)).id for m in range(3)]
    unreadable = world.legacy(f"legacy id {CANARY}", now=T0 + timedelta(seconds=90))
    with caplog.at_level(logging.WARNING, logger="service.conversations_api"):
        page = client.get("/conversations")
        ours = [r for r in caplog.records if r.name == "service.conversations_api"]
    assert page.status_code == 200
    assert _ids(page) == list(reversed(readable))
    assert [r.getMessage() for r in ours] == ["conversation index: 1 stored rows omitted as unreadable"]
    assert all(unreadable not in r.getMessage() and CANARY not in r.getMessage() for r in ours)
    # One row per page, so the walk also meets a page whose only row is left
    # out: an empty page with a cursor, which is not the end of the list.
    assert _walk(client, limit="1") == list(reversed(readable))


def test_every_page_validates_as_the_contracts_page(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    world.conversation(campaign_id=campaign, started_mode="gm", title="Session zero")
    world.legacy("0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d")
    page = client.get("/conversations", params={"include_archived": "true"}).json()
    ConversationPage.model_validate(page)
    assert set(page) == {"schema_version", "items", "next_cursor"}, "no filter is echoed"


# ── PATCH ────────────────────────────────────────────────────────────────────


def test_a_rename_is_stored_trimmed_and_a_repeat_changes_nothing(client: TestClient, world: _World) -> None:
    mine = world.conversation(now=T0).id
    first = _patch(client, mine, title="  The harbour job ")
    assert first.status_code == 200
    assert first.json()["title"] == "The harbour job"
    assert first.json()["updated_at"] is not None
    assert _patch(client, mine, title="The harbour job").json() == first.json(), "a repeat is a no-op"


def test_archive_and_unarchive_are_reversible_and_a_repeat_changes_nothing(client: TestClient, world: _World) -> None:
    mine = world.conversation().id
    archived = _patch(client, mine, archived=True).json()
    assert archived["archived_at"] is not None
    assert _ids(client.get("/conversations")) == []
    assert _patch(client, mine, archived=True).json() == archived
    restored = _patch(client, mine, archived=False).json()
    assert restored["archived_at"] is None
    assert _patch(client, mine, archived=False).json() == restored
    assert _ids(client.get("/conversations")) == [mine]


def test_an_uncampaigned_conversation_links_once_and_a_repeat_is_a_no_op(client: TestClient, world: _World) -> None:
    mine = world.conversation(started_mode="gm").id
    campaign = world.campaign()
    linked = _patch(client, mine, campaign_id=campaign)
    assert (linked.status_code, linked.json()["campaign_id"]) == (200, campaign)
    assert _patch(client, mine, campaign_id=campaign).json() == linked.json()
    moved = _patch(client, mine, campaign_id=world.campaign())
    assert (moved.status_code, moved.json()) == (409, ALREADY_LINKED)
    stored = world.stored(mine)
    assert stored is not None and stored.campaign_id == campaign


def test_a_legacy_conversation_of_unknown_channel_can_be_linked(client: TestClient, world: _World) -> None:
    legacy = world.legacy("0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d")
    campaign = world.campaign()
    assert _patch(client, legacy, campaign_id=campaign).json()["campaign_id"] == campaign


@pytest.mark.parametrize("which", ["missing", "foreign", "archived"])
def test_linking_to_a_campaign_that_is_not_a_live_one_of_the_callers_is_the_one_404(
    client: TestClient, world: _World, which: str
) -> None:
    mine = world.conversation(started_mode="gm").id
    campaign = {
        "missing": "cmp_" + "z" * 22,
        "foreign": world.campaign(STRANGER),
        "archived": world.campaign(archived=True),
    }[which]
    answer = _patch(client, mine, campaign_id=campaign)
    assert (answer.status_code, answer.json()) == (404, NOT_FOUND)
    stored = world.stored(mine)
    assert stored is not None and stored.campaign_id is None


def test_a_conversation_started_outside_gm_cannot_be_linked(client: TestClient, world: _World) -> None:
    mine = world.conversation(started_mode="rules").id
    answer = _patch(client, mine, campaign_id=world.campaign())
    assert (answer.status_code, answer.json()["detail"]["field"]) == (422, "campaign_id")


def test_the_channel_is_bound_once_and_a_later_different_one_is_answered_not_refused(
    client: TestClient, world: _World
) -> None:
    legacy = world.legacy("0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d")
    assert _patch(client, legacy, started_mode="spell").json()["started_mode"] == "spell"
    second = _patch(client, legacy, started_mode="rules")
    assert (second.status_code, second.json()["started_mode"]) == (200, "spell")


def test_a_linked_conversation_binds_no_channel_but_gm(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    linked = world.legacy("0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d")
    assert _patch(client, linked, campaign_id=campaign).status_code == 200
    answer = _patch(client, linked, started_mode="sage")
    assert (answer.status_code, answer.json()["detail"]["field"]) == (422, "started_mode")
    assert _patch(client, linked, started_mode="gm").json()["started_mode"] == "gm"
    fresh = world.conversation(started_mode=None).id
    both = _patch(client, fresh, campaign_id=campaign, started_mode="sage")
    assert (both.status_code, both.json()["detail"]["field"]) == (422, "started_mode")
    stored = world.stored(fresh)
    assert stored is not None and stored.campaign_id is None, "the refusal rolled the link back"


def test_several_changes_land_together(client: TestClient, world: _World) -> None:
    legacy = world.legacy("0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d")
    campaign = world.campaign()
    body = _patch(
        client, legacy, campaign_id=campaign, started_mode="gm", title="Session one", archived=True
    ).json()
    assert (body["campaign_id"], body["started_mode"], body["title"]) == (campaign, "gm", "Session one")
    assert body["archived_at"] is not None


class _RacingStore(InMemoryConversationStore):
    """The twin, with another request of the same owner slipping in between the
    route's read and its link — the race only PostgreSQL could otherwise stage."""

    def __init__(self, db: InMemoryDatabase, *, link_first: str | None, bind_first: str | None) -> None:
        super().__init__(db)
        self._link_first, self._bind_first = link_first, bind_first

    def link_campaign(self, unit, conversation_id, *, owner_id, campaign_id, now=None):  # type: ignore[no-untyped-def]
        if self._link_first is not None:
            super().link_campaign(unit, conversation_id, owner_id=owner_id, campaign_id=self._link_first)
            return False
        if self._bind_first is not None:
            super().bind_started_mode(unit, conversation_id, owner_id=owner_id, mode=self._bind_first)
        return super().link_campaign(unit, conversation_id, owner_id=owner_id, campaign_id=campaign_id)


@pytest.mark.parametrize(("winner", "status"), [("same", 200), ("other", 409)])
def test_losing_the_link_race_rereads_and_answers_from_what_won(
    client: TestClient, world: _World, winner: str, status: int
) -> None:
    mine = world.conversation(started_mode="gm").id
    asked, other = world.campaign(), world.campaign()
    racing = _RacingStore(world.db, link_first=asked if winner == "same" else other, bind_first=None)
    app.dependency_overrides[conversations_api.get_conversation_store] = lambda: racing
    answer = _patch(client, mine, campaign_id=asked)
    assert answer.status_code == status
    if status == 409:
        assert answer.json() == ALREADY_LINKED


def test_a_race_that_would_leave_a_linked_conversation_outside_gm_rolls_the_patch_back(
    client: TestClient, world: _World
) -> None:
    """The backstop: the checks passed on the row as read, but another request
    bound `sage` before the link landed. The re-read sees it and the whole patch
    is refused and rolled back, so the invariant holds whichever request lost."""
    mine = world.conversation(started_mode=None).id
    campaign = world.campaign()
    racing = _RacingStore(world.db, link_first=None, bind_first="sage")
    app.dependency_overrides[conversations_api.get_conversation_store] = lambda: racing
    answer = _patch(client, mine, campaign_id=campaign, title="Renamed")
    assert (answer.status_code, answer.json()["detail"]["field"]) == (422, "campaign_id")
    stored = world.stored(mine)
    assert stored is not None
    assert (stored.campaign_id, stored.started_mode, stored.title) == (None, None, None)


class _Vanishing(InMemoryConversationStore):
    """The twin, with the row gone after the route's first read — an account
    deleted mid-request, whose cascade takes its conversations with it."""

    def __init__(self, db: InMemoryDatabase, gone_at: str) -> None:
        super().__init__(db)
        self._gone_at = gone_at
        self._reads = 0

    def get_for_owner(self, unit, conversation_id, *, owner_id):  # type: ignore[no-untyped-def]
        self._reads += 1
        if self._gone_at == "final read" and self._reads > 1:
            return None
        return super().get_for_owner(unit, conversation_id, owner_id=owner_id)

    def bind_started_mode(self, unit, conversation_id, *, owner_id, mode):  # type: ignore[no-untyped-def]
        return None if self._gone_at == "bind" else super().bind_started_mode(
            unit, conversation_id, owner_id=owner_id, mode=mode
        )

    def rename(self, unit, conversation_id, *, owner_id, title, now=None):  # type: ignore[no-untyped-def]
        return False if self._gone_at == "rename" else super().rename(
            unit, conversation_id, owner_id=owner_id, title=title, now=now
        )


@pytest.mark.parametrize("gone_at", ["bind", "rename", "final read"])
def test_a_conversation_gone_mid_patch_is_the_one_404(client: TestClient, world: _World, gone_at: str) -> None:
    mine = world.conversation(started_mode=None).id
    app.dependency_overrides[conversations_api.get_conversation_store] = lambda: _Vanishing(world.db, gone_at)
    answer = _patch(client, mine, started_mode="gm", title="Renamed", archived=True)
    assert (answer.status_code, answer.json()) == (404, NOT_FOUND)
    stored = world.stored(mine)
    assert stored is not None and (stored.title, stored.archived_at) == (None, None), "rolled back"


# ── SEC-7 ────────────────────────────────────────────────────────────────────

#: Re-transcribed from the base on 2026-09-26. Each triple must be a real 201.
ALLOWED = [
    # ui/nginx.conf: `proxy_set_header Host $host` in every location (it drops the
    # port); the browser is at ui/e2e/stack.ts:29 COMPOSE_STACK_URL
    # 'http://127.0.0.1:4173', published by docker-compose.e2e.yml:64 "4173:80".
    ("127.0.0.1", "http://127.0.0.1:4173", "same-origin"),
    # ui/vite.config.ts: the string proxy form, no changeOrigin, so the
    # browser's Host reaches the service with its port.
    ("localhost:5173", "http://localhost:5173", "same-origin"),
    # Cloud Run: TLS ends at the edge and the app sees http (config.py's
    # SESSION_COOKIE_SECURE note), so the scheme is not compared.
    ("svc-xyz.a.run.app", "https://svc-xyz.a.run.app", "same-origin"),
    # TestClient: not a browser, so neither header.
    ("testserver", None, None),
]


def _headers(host: str, origin: str | None, fetch_site: str | None) -> dict[str, str]:
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    if fetch_site is not None:
        headers["sec-fetch-site"] = fetch_site
    return headers


@pytest.mark.parametrize(("host", "origin", "fetch_site"), ALLOWED)
def test_a_write_from_this_application_is_allowed(
    client: TestClient, world: _World, host: str, origin: str | None, fetch_site: str | None
) -> None:
    headers = _headers(host, origin, fetch_site)
    made = client.post("/conversations", json={"schema_version": 1, "started_mode": "gm"}, headers=headers)
    assert made.status_code == 201, made.text
    patched = client.patch(
        f"/conversations/{made.json()['conversation_id']}", json={"schema_version": 1, "title": "Ok"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    for answer in (made, patched):
        assert not [k for k in answer.headers if k.lower().startswith("access-control-")], "no CORS header"


REFUSED = [
    ("a foreign origin", {"host": "127.0.0.1", "origin": "https://evil.example"}, None),
    ("a foreign origin on the same port", {"host": "127.0.0.1:4173", "origin": "http://evil.example:4173"}, None),
    ("the right host on another port", {"host": "localhost:5173", "origin": "http://localhost:4173"}, None),
    ("a null origin", {"host": "127.0.0.1", "origin": "null"}, None),
    ("an origin that is not a URL", {"host": "127.0.0.1", "origin": "127.0.0.1"}, None),
    ("an origin with a broken port", {"host": "localhost:5173", "origin": "http://localhost:99999"}, None),
    ("an origin naming another host than Host", {"origin": "http://127.0.0.1"}, None),
    ("a cross-site fetch", {"sec-fetch-site": "cross-site"}, None),
    ("a same-site fetch", {"sec-fetch-site": "same-site"}, None),
    ("a fetch the user typed", {"sec-fetch-site": "none"}, None),
    ("a text/plain body", {"content-type": "text/plain"}, '{"schema_version":1,"started_mode":"sage"}'),
    (
        "a text/plain body from this application",
        {"host": "127.0.0.1", "origin": "http://127.0.0.1:4173", "sec-fetch-site": "same-origin",
         "content-type": "text/plain"},
        '{"schema_version":1,"started_mode":"sage"}',
    ),
    ("a form body", {"content-type": "application/x-www-form-urlencoded"}, "schema_version=1&started_mode=sage"),
    ("a body with no type", {}, '{"schema_version":1,"started_mode":"sage"}'),
]


@pytest.mark.parametrize(("name", "headers", "raw"), REFUSED, ids=[r[0] for r in REFUSED])
def test_a_forged_write_is_refused_before_anything_else(
    client: TestClient, world: _World, name: str, headers: dict[str, str], raw: str | None
) -> None:
    """T-7. Refused as `forbidden` with its own sentence, before the session is
    looked at, and nothing is written."""
    mine = world.conversation().id
    if raw is None:
        # Only the headers are wrong: the body is the contract's, and typed so.
        sent = {"content-type": "application/json", **headers}
        bodies = ('{"schema_version":1,"started_mode":"sage"}', '{"schema_version":1,"title":"x"}')
    else:
        sent, bodies = headers, (raw, raw)
    answers = [
        client.post("/conversations", content=bodies[0], headers=sent),
        client.patch(f"/conversations/{mine}", content=bodies[1], headers=sent),
    ]
    for answer in answers:
        assert (answer.status_code, answer.json()) == (403, FORBIDDEN_ORIGIN), name
    assert world.row_count() == 1
    stored = world.stored(mine)
    assert stored is not None and stored.title is None


def test_a_request_with_no_host_header_cannot_name_this_application() -> None:
    """The one refusal a test client cannot send: it always adds a Host."""
    scope = {"type": "http", "method": "POST", "headers": [(b"origin", b"http://127.0.0.1")]}
    with pytest.raises(HTTPException) as refused:
        conversations_api.origin_check(Request(scope))
    assert refused.value.status_code == 403


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        # Neither browser header, and no body at all: not a browser, nothing to type.
        ([], True),
        # A Host this service cannot read names no application, so an Origin fails against it.
        ([(b"host", b"localhost:99999"), (b"origin", b"http://localhost:99999")], False),
        # A streamed body is a body: it must be typed JSON like any other.
        ([(b"transfer-encoding", b"chunked"), (b"content-type", b"text/plain")], False),
        ([(b"transfer-encoding", b"chunked"), (b"content-type", b"application/json; charset=utf-8")], True),
        # A length that cannot be read is treated as a body, so the check fails closed.
        ([(b"content-length", b"x"), (b"content-type", b"text/plain")], False),
        ([(b"content-length", b"0"), (b"content-type", b"text/plain")], True),
    ],
)
def test_what_counts_as_a_body_and_as_this_application(headers: list[tuple[bytes, bytes]], allowed: bool) -> None:
    request = Request({"type": "http", "method": "POST", "headers": headers})
    if allowed:
        conversations_api.origin_check(request)
        return
    with pytest.raises(HTTPException) as refused:
        conversations_api.origin_check(request)
    assert refused.value.status_code == 403


def test_a_read_is_not_origin_checked(client: TestClient, world: _World) -> None:
    mine = world.conversation().id
    foreign = {"origin": "https://evil.example", "sec-fetch-site": "cross-site"}
    assert client.get("/conversations", headers=foreign).status_code == 200
    assert client.get(f"/conversations/{mine}", headers=foreign).status_code == 200


# ── Bodies ───────────────────────────────────────────────────────────────────


def test_a_body_over_the_cap_is_refused_and_one_at_the_cap_is_read(client: TestClient, world: _World) -> None:
    cap = conversations_api.BODY_MAX_BYTES
    body = '{"schema_version":1,"started_mode":"sage"}'
    at_cap = body + " " * (cap - len(body))
    json_type = {"content-type": "application/json"}
    assert client.post("/conversations", content=at_cap, headers=json_type).status_code == 201
    over = client.post("/conversations", content=at_cap + " ", headers=json_type)
    assert (over.status_code, over.json()) == (
        422,
        {"detail": {"code": "validation_failed", "message": "That request isn't valid.", "retryable": False}},
    )
    assert world.row_count() == 1


def _streamed(chunks: list[bytes], pulled: list[int]) -> Request:
    async def receive() -> dict[str, Any]:
        index = len(pulled)
        pulled.append(index)
        return {"type": "http.request", "body": chunks[index], "more_body": index + 1 < len(chunks)}

    scope = {"type": "http", "method": "POST", "headers": [(b"transfer-encoding", b"chunked")]}
    return Request(scope, receive)


def test_a_streamed_body_is_refused_at_the_chunk_that_crosses_the_cap_and_the_rest_is_never_read() -> None:
    pulled: list[int] = []
    chunks = [b"x" * 4096, b"x" * 4096, b"x" * 2, b"x" * 4096, b"x" * 4096]
    with pytest.raises(HTTPException) as refused:
        asyncio.run(conversations_api.read_body(_streamed(chunks, pulled)))
    assert refused.value.status_code == 422
    assert pulled == [0, 1, 2], "read past the chunk that crossed the cap"


def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_read() -> None:
    pulled: list[int] = []
    request = _streamed([b"{}"], pulled)
    request.scope["headers"] = [(b"content-length", str(conversations_api.BODY_MAX_BYTES + 1).encode())]
    with pytest.raises(HTTPException) as refused:
        asyncio.run(conversations_api.read_body(request))
    assert refused.value.status_code == 422
    assert pulled == []


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"schema_version":1,"started_mode":"sage"'])
def test_a_body_that_is_not_the_contracts_json_is_a_422_not_a_500(client: TestClient, raw: str) -> None:
    answer = client.post("/conversations", content=raw, headers={"content-type": "application/json"})
    assert answer.status_code == 422
    ErrorBody.model_validate(answer.json())


# ── 503, and no private text in a log or an answer (A12) ─────────────────────


class _Down(InMemoryConversationStore):
    def __init__(self, db: InMemoryDatabase) -> None:
        super().__init__(db)

    def _fail(self, *args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError(f"server closed the connection: {CANARY}")

    create = get_for_owner = list_for_owner = _fail  # type: ignore[assignment]


def test_a_database_failure_is_a_503_logged_by_type_alone(
    client: TestClient, world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    app.dependency_overrides[conversations_api.get_conversation_store] = lambda: _Down(world.db)
    with caplog.at_level(logging.DEBUG, logger="service"):
        answers = [
            client.get("/conversations"),
            _create(client, title=CANARY),
            _read(client, "cnv_" + "q" * 22),
            _patch(client, "cnv_" + "q" * 22, title=CANARY),
        ]
    assert [(a.status_code, a.json()) for a in answers] == [(503, UNAVAILABLE)] * 4
    ours = [r for r in caplog.records if r.name.startswith("service")]
    assert [r.getMessage() for r in ours] == ["conversation route: database unavailable (OperationalError)"] * 4
    assert all(r.exc_info is None for r in ours)


def test_no_title_campaign_id_or_conversation_id_reaches_a_log_line(
    client: TestClient, world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    """Every path of every route, the refusals included, with the canary in
    each value a caller controls. The two paths that DO log — an outage and an
    omitted row — are driven too, so the assertion is not over zero records."""
    campaign = world.campaign()
    with caplog.at_level(logging.DEBUG, logger="service"):
        made = _create(client, started_mode="gm", campaign_id=campaign, title=CANARY).json()
        _patch(client, made["conversation_id"], title=f"{CANARY} again")
        client.get("/conversations", params={"campaign_id": campaign})
        _read(client, made["conversation_id"])
        assert _create(client, title=CANARY + chr(0x0A) + "x").status_code == 422
        _create(client, started_mode=CANARY)
        _patch(client, f"cnv_{CANARY}", title=CANARY)
        client.get("/conversations", params={"cursor": CANARY, "started_mode": CANARY})
        world.legacy(f"legacy {CANARY}")
        client.get("/conversations")
        app.dependency_overrides[conversations_api.get_conversation_store] = lambda: _Down(world.db)
        _create(client, title=CANARY)
    ours = [r for r in caplog.records if r.name.startswith("service")]
    assert len(ours) == 2, [r.getMessage() for r in ours]
    for record in ours:
        text = record.getMessage() + " ".join(str(arg) for arg in (record.args or ()))
        assert CANARY not in text and campaign not in text and made["conversation_id"] not in text
        assert record.exc_info is None


@pytest.mark.parametrize(
    "request_",
    [
        ("post", "/conversations", {"schema_version": 1, "started_mode": "sage", "title": CANARY * 30}),
        ("post", "/conversations", {"schema_version": 1, "started_mode": CANARY}),
        ("post", "/conversations", {"schema_version": 1, "started_mode": "sage", CANARY: 1}),
        ("post", "/conversations", {"schema_version": 1, "started_mode": "sage", "campaign_id": CANARY + "!"}),
        ("patch", f"/conversations/cnv_{CANARY}", {"schema_version": 1, "title": CANARY + chr(0x202E)}),
        ("patch", f"/conversations/cnv_{CANARY}", {"schema_version": 1, "archived": CANARY}),
        ("patch", f"/conversations/{CANARY}.x", {"schema_version": 1, "title": "ok"}),
        ("get", f"/conversations/{CANARY}.x", None),
    ],
)
def test_a_refusal_is_from_the_closed_code_set_and_repeats_nothing_it_was_sent(
    client: TestClient, request_: tuple[str, str, dict[str, Any] | None]
) -> None:
    method, path, body = request_
    answer = client.request(method.upper(), path, json=body)
    assert answer.status_code in {404, 422}
    ErrorBody.model_validate(answer.json())
    assert CANARY not in answer.text


# ── Wiring (A2-1, A2-2) ──────────────────────────────────────────────────────

CONVERSATION_ROUTES = {
    ("GET", "/conversations"),
    ("POST", "/conversations"),
    ("GET", "/conversations/{conversation_id}"),
    ("PATCH", "/conversations/{conversation_id}"),
}


def _conversation_routes() -> list[tuple[str, str, APIRoute]]:
    rows = [
        (method, str(ctx.path), ctx.original_route)
        for ctx in iter_route_contexts(app.routes)
        if isinstance(ctx.original_route, APIRoute)
        for method in (ctx.methods or set())
    ]
    assert rows, "the routing table read as empty: the walk below would pass for nothing"
    return [row for row in rows if row[2].endpoint.__module__ == conversations_api.__name__]


def test_every_conversation_route_is_session_guarded_and_in_the_auth_matrix() -> None:
    """`test_auth_guard.py`'s own completeness walk reads `app.routes`, where an
    included router is one opaque entry, so it cannot see these. This walk can."""
    found = _conversation_routes()
    assert {(method, path) for method, path, _ in found} == CONVERSATION_ROUTES
    listed = {(method, template) for method, template, _, _ in PROTECTED_ROUTES}
    for method, path, route in found:
        assert any(d.call is require_session for d in route.dependant.dependencies), (method, path)
        assert (method, path) in listed, f"{method} {path} is missing from PROTECTED_ROUTES"


def test_the_routes_module_imports_nothing_from_the_app() -> None:
    text = Path(conversations_api.__file__ or "").read_text(encoding="utf-8")
    assert "from .app" not in text and "service.app" not in text


def test_the_wire_record_carries_no_owner_and_no_routing_and_is_utc(world: _World) -> None:
    made = world.conversation(owner=STRANGER, title="x")
    shifted = StoredConversation(
        id=made.id,
        owner_id=STRANGER,
        campaign_id=None,
        title="x",
        started_mode="gm",
        created_at=datetime(2026, 3, 1, 16, 0, tzinfo=timezone(timedelta(hours=2))),
        updated_at=None,
        archived_at=None,
    )
    wire = conversations_api.to_wire(shifted).model_dump(mode="json")
    assert set(wire) == WIRE_KEYS
    assert wire["created_at"] == "2026-03-01T14:00:00Z"
    assert str(STRANGER) not in {str(v) for v in wire.values()}

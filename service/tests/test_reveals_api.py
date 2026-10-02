"""The GM's reveal routes (agent-forge-harness-1kg.7.2, PR-1), through the real
app over the in-memory twin.

What a client observes: the order of checks (SEC-3), the one 404 for every
stranger on every route, the refusal mapping of the 7.1 brief (409 for a table
that moved on, 422 naming a field and, for a mask, the keys at fault, 503
retryable), `pending_delivery` and `stale_text`, headers, and a canary proof that
no field text reaches a body, a log line or an audit row. What two connections
decide is `tests/test_reveal_db.py`'s; the rules of a Confirm, a Stop and the
picture are `service/reveals.py`'s and are proved there.

Every test names the mutation it kills (`# kills:`).
"""

from __future__ import annotations

import ast
import json
import logging
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from httpx import Response

from service import app as appmod
from service import ratelimit, reveals_api
from service import reveals as reveals_mod
from service.app import app, require_session
from service.auth_store import User
from service.document_store import InMemoryDocumentStore
from service.invites import Role
from service.participant_store import InMemoryParticipantStore
from service.ratelimit import SlidingWindowLimiter
from service.reveal_scope import ParticipantsAudience, TableAudience
from service.reveal_store import InMemoryRevealStore, LiveCopy, PictureEntry, RevealBusy, RevealPicture
from service.reveals import DisplayCommand, Reveals
from service.session import SessionData
from service.tests.test_reveals import Twin, _twin
from service.workbench_api import NOT_FOUND_DETAIL
from service.workbench_contracts import Author, DocumentTypeId, RevealAnswer

GM_A, GM_B, PLAYER, GM_THIRD = 1, 2, 3, 9
T0 = datetime(2026, 9, 1, 19, 0, tzinfo=UTC)
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
UNAUTHENTICATED = {"detail": "not signed in"}
MISSING_CAMPAIGN = "cmp_" + "z" * 22
SOURCE = Path(reveals_api.__file__)
EVIL = {"origin": "https://evil.example"}
AN_ASSET = {"asset_id": "ast_" + "a" * 22, "media_type": "image", "alt": "A hooded woman"}


class _Counting:
    """The twin's database, counting the transactions it opens: a refusal that
    must come before any database access opens none."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.opened = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self.opened += 1
        with self._db.transaction() as unit:
            yield unit


class Rig:
    """A twin with a live session and one sealed NPC, the reveal service over it,
    and the helpers a route test needs."""

    def __init__(
        self,
        rows_type: type[InMemoryRevealStore] = InMemoryRevealStore,
        service_type: type[Reveals] = Reveals,
    ) -> None:
        self.twin: Twin
        self.twin, self.rows = _twin(rows_type)
        self.counting = _Counting(self.twin.db)
        self.participants = InMemoryParticipantStore(self.twin.db)
        self.service: Reveals = service_type(
            self.counting,  # type: ignore[arg-type]
            campaigns=self.twin.campaigns,
            sessions=self.twin.sessions,
            reveals=self.rows,
            documents=self.twin.documents,
            audit=self.twin.audit,
        )

    @property
    def campaign(self) -> str:
        return self.twin.campaign

    @property
    def session(self) -> str:
        return str(self.twin.session.id)

    @property
    def document(self) -> str:
        return self.twin.document

    # ── Staging ──────────────────────────────────────────────────────────────

    def new_document(
        self, data: dict[str, Any], *, campaign: str | None = None, doc_type: DocumentTypeId = DocumentTypeId.NPC
    ) -> str:
        where = campaign or self.campaign
        with self.twin.db.transaction() as unit:
            made = self.twin.documents.create(
                unit, where, doc_type=doc_type, type_version=1, data=data, author=Author.GM
            )
        with self.twin.db.transaction() as unit:
            self.twin.documents.seal(unit, where, made.id)
        return str(made.id)

    def write(self, document: str, fields: dict[str, Any], *, campaign: str | None = None) -> None:
        with self.twin.db.transaction() as unit:
            self.twin.documents.write_fields(
                unit, campaign or self.campaign, document, fields=fields, author=Author.GM, base_write_revision=None
            )

    def seat(self, state: str, player: int, *, campaign: str | None = None) -> str:
        """A seat in `state`: open, offered, accepted, confirmed or removed."""
        where = campaign or self.campaign
        with self.twin.db.transaction() as unit:
            seat = self.participants.add(unit, where, alias=f"Seat {player}").id
        steps = [
            ("offered", lambda unit: self.participants.offer(unit, where, seat, user_id=player)),
            ("accepted", lambda unit: self.participants.accept(unit, where, seat, user_id=player)),
            ("confirmed", lambda unit: self.participants.confirm(unit, where, seat)),
            ("removed", lambda unit: self.participants.remove(unit, where, seat)),
        ]
        for reached, step in steps:
            if state == "open":
                break
            with self.twin.db.transaction() as unit:
                step(unit)
            if reached == state:
                break
        return str(seat)

    def confirm_seat(self, seat: str) -> None:
        with self.twin.db.transaction() as unit:
            self.participants.confirm(unit, self.campaign, seat)

    def stranger(self, owner: int = GM_B) -> tuple[str, str, str]:
        """Another GM's campaign with a live session and a sealed document."""
        with self.twin.db.transaction() as unit:
            campaign = self.twin.campaigns.create(unit, owner_id=owner, name="Theirs").id
        with self.twin.db.transaction() as unit:
            session = self.twin.sessions.start(
                unit,
                campaign,
                owner_id=owner,
                expires_at=datetime.now(UTC).replace(year=2099),
                command_id=secrets.token_urlsafe(16),
                now=datetime.now(UTC),
            )
        return campaign, str(session.id), self.new_document({"name": "Zed"}, campaign=campaign)

    def actions(self, campaign: str | None = None) -> list[str]:
        with self.twin.db.transaction() as unit:
            return [str(e.action) for e in self.twin.audit.for_campaign(unit, campaign or self.campaign)]

    # ── Requests ─────────────────────────────────────────────────────────────

    def body(
        self,
        *,
        document: str | None = None,
        mask: list[str] | None = None,
        audience: dict[str, Any] | None = None,
        epoch: int | None = None,
        version: int = 1,
        command: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command_id": command or secrets.token_urlsafe(16),
            "document_id": document or self.document,
            "session_id": session or self.session,
            "reveal_epoch": self.twin.epoch() if epoch is None else epoch,
            "version": version,
            "mask": ["name"] if mask is None else mask,
            "audience": audience or {"kind": "table"},
        }


@pytest.fixture
def rig() -> Iterator[Rig]:
    made = Rig()
    _use(made)
    yield made
    for dependency in (appmod.get_reveals, require_session):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(rig: Rig) -> TestClient:
    _as(GM_A)
    return TestClient(app)


def _use(made: Rig) -> None:
    app.dependency_overrides[appmod.get_reveals] = lambda: made.service


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


def _path(campaign: str, tail: str = "") -> str:
    return f"/campaigns/{campaign}/reveals{tail}"


def _confirm(client: TestClient, campaign: str, body: dict[str, Any], **kwargs: Any) -> Response:
    return client.post(_path(campaign), json=body, **kwargs)


def _stop(client: TestClient, campaign: str, *, document: str | None = None, command: str | None = None) -> Response:
    body: dict[str, Any] = {
        "schema_version": 1,
        "command_id": command or secrets.token_urlsafe(16),
        "scope": "all" if document is None else "document",
    }
    if document is not None:
        body["document_id"] = document
    return client.post(_path(campaign, "/stop"), json=body)


def _answer(response: Response) -> RevealAnswer:
    assert response.status_code == 200, response.text
    return RevealAnswer.model_validate(response.json())


def _slots(response: Response) -> dict[str | None, dict[str, Any]]:
    """The answer's slots by participant id (None is the table)."""
    state = response.json()["state"]
    return {entry["slot"].get("participant_id"): entry for entry in state["slots"]}


def _detail(response: Response) -> dict[str, Any]:
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    return detail


# ── A1: the order of checks ──────────────────────────────────────────────────


def test_a1_the_order_of_checks(rig: Rig, client: TestClient) -> None:
    # kills: moving the id-shape check after the service call; swapping the service's 404 and 409
    path = _path(rig.campaign)
    body = rig.body()

    _signed_out()
    assert _confirm(client, rig.campaign, body, headers=EVIL).status_code == 403, "origin before authentication"
    assert client.post(_path(rig.campaign, "/stop"), json={}, headers=EVIL).status_code == 403
    assert _confirm(client, rig.campaign, body).status_code == 401
    assert client.get(path).json() == UNAUTHENTICATED
    assert _stop(client, rig.campaign).status_code == 401

    _as(PLAYER, "player")
    assert _confirm(client, rig.campaign, body).status_code == 403
    assert _stop(client, rig.campaign).status_code == 403
    assert client.get(path).status_code == 403

    _as(GM_A)
    wrong = client.post("/campaigns/nope/reveals", json={"schema_version": 1})
    assert wrong.status_code == 422, "a malformed body is a 422 before the id-shape 404"
    broken = client.post("/campaigns/nope/reveals", content=b"{nope", headers={"content-type": "application/json"})
    assert broken.status_code == 422
    assert _confirm(client, "nope", body).status_code == 404
    assert _confirm(client, rig.campaign, rig.body(session="ses_nope")).status_code == 404

    _as(GM_B)
    assert _confirm(client, rig.campaign, rig.body(epoch=99)).status_code == 404, "a stranger never reaches a 409"
    assert _confirm(client, rig.campaign, rig.body(mask=["tags"])).status_code == 404, "nor a resource-dependent 422"
    assert _stop(client, rig.campaign).status_code == 404

    _as(GM_A)
    rig.counting.opened = 0
    assert client.get(_path("nope")).status_code == 404
    assert _confirm(client, "nope", body).status_code == 404
    assert _confirm(client, rig.campaign, rig.body(session="nope")).status_code == 404
    assert _confirm(client, rig.campaign, rig.body(document="nope")).status_code == 404
    nowhere = {"schema_version": 1, "command_id": "c" * 16, "scope": "all"}
    assert client.post(_path("nope", "/stop"), json=nowhere).status_code == 404
    assert _stop(client, rig.campaign, document="nope").status_code == 404
    assert rig.counting.opened == 0, "a 404 for an id shape opened no transaction"
    assert _confirm(client, rig.campaign, body).status_code == 200
    assert rig.counting.opened >= 1, "the control: a real request does open one"


# ── A2: one 404 ──────────────────────────────────────────────────────────────


def _a_session_of_another_campaign(rig: Rig, other: str) -> str:
    """One live table per GM: the owner's other campaign gets a session that then
    ends, and the first campaign gets a fresh live one."""
    sessions, db = rig.twin.sessions, rig.twin.db

    def start(campaign: str) -> Any:
        with db.transaction() as unit:
            return sessions.start(
                unit,
                campaign,
                owner_id=GM_A,
                expires_at=datetime.now(UTC).replace(year=2099),
                command_id=secrets.token_urlsafe(16),
                now=datetime.now(UTC),
            )

    with db.transaction() as unit:
        sessions.end(unit, rig.campaign, rig.session, owner_id=GM_A)
    elsewhere = start(other)
    with db.transaction() as unit:
        sessions.end(unit, other, elsewhere.id, owner_id=GM_A)
    rig.twin.session = start(rig.campaign)
    return str(elsewhere.id)


def test_a2_the_one_404_is_byte_identical_for_every_stranger(rig: Rig, client: TestClient) -> None:
    # kills: a distinct 404 for a foreign document
    their_campaign, their_session, their_document = rig.stranger()
    with rig.twin.db.transaction() as unit:
        mine2 = rig.twin.campaigns.create(unit, owner_id=GM_A, name="Aubade").id
    session2 = _a_session_of_another_campaign(rig, mine2)
    confirms = [
        _confirm(client, MISSING_CAMPAIGN, rig.body()),
        _confirm(client, their_campaign, rig.body(document=their_document, session=their_session)),
        _confirm(client, rig.campaign, rig.body(session=their_session)),
        _confirm(client, rig.campaign, rig.body(session=session2)),
        _confirm(client, rig.campaign, rig.body(document=their_document)),
    ]
    stops = [
        _stop(client, MISSING_CAMPAIGN),
        _stop(client, their_campaign),
        _stop(client, rig.campaign, document=their_document),
    ]
    reads = [client.get(_path(MISSING_CAMPAIGN)), client.get(_path(their_campaign))]
    for group in (confirms, stops, reads):
        assert {_shape(r) for r in group} == {_shape(group[0])}
        assert group[0].status_code == 404 and group[0].json() == NOT_FOUND
    assert len({_shape(r) for r in [*confirms, *stops, *reads]}) == 1, "across the three routes too"


# ── A3: Confirm to the table, and its replay ─────────────────────────────────


def test_a3_confirm_to_the_table_and_its_replay_write_nothing_twice(rig: Rig, client: TestClient) -> None:
    # kills: dropping the replay path; mapping the state from a stale picture
    before = rig.twin.epoch()
    command = secrets.token_urlsafe(16)
    first = _confirm(client, rig.campaign, rig.body(command=command))
    answer = _answer(first)
    assert answer.state is not None and answer.state.reveal_epoch == before + 1
    assert [e.slot.kind for e in answer.state.slots] == ["table"]
    live = answer.state.slots[0].live
    assert live is not None and live.mask == ["name"] and live.stale_text is False and live.pending_delivery is False
    assert rig.actions() == ["reveal.displayed"]

    replay = _confirm(client, rig.campaign, rig.body(command=command, epoch=before))
    assert replay.status_code == 200 and replay.json() == first.json()
    assert rig.actions() == ["reveal.displayed"], "a replay writes nothing"
    assert rig.twin.epoch() == before + 1


# ── A4: stale epochs ─────────────────────────────────────────────────────────


def test_a4_a_stale_epoch_is_a_409_and_a_stop_hands_over_the_epoch(rig: Rig, client: TestClient) -> None:
    # kills: answering 200 on RevealConflict; omitting the epoch from the Stop answer
    stale = _confirm(client, rig.campaign, rig.body(epoch=rig.twin.epoch() + 5))
    assert stale.status_code == 409
    assert _detail(stale)["code"] == "conflict" and _detail(stale)["retryable"] is False
    assert _detail(stale)["message"] == reveals_api.CONFLICT_MESSAGE

    old = rig.twin.epoch()
    stopped = _stop(client, rig.campaign)
    epoch = _answer(stopped).state
    assert epoch is not None and epoch.reveal_epoch == old + 1, "RC-1: a Stop advances the epoch when nothing is live"
    assert _confirm(client, rig.campaign, rig.body(epoch=old)).status_code == 409
    assert _confirm(client, rig.campaign, rig.body(epoch=epoch.reveal_epoch)).status_code == 200


# ── A5: mask refusals ────────────────────────────────────────────────────────

CANARY_IDENTITY = "Canary identity the queen of the drowned bell"


def _a_refusable_npc(rig: Rig) -> str:
    return rig.new_document(
        {
            "name": "Vashti",
            "qualifier": "   ",
            "voice": "low",
            "tags": ["harbour"],
            "true_identity": CANARY_IDENTITY,
            "portrait": dict(AN_ASSET),
        }
    )


@pytest.mark.parametrize(
    ("mask", "keys"),
    [
        (["tags"], ["tags"]),
        (["true_identity"], ["true_identity"]),
        (["qualifier"], ["qualifier"]),
        (["portrait"], ["portrait"]),
        (
            ["name", "tags", "true_identity", "qualifier", "portrait"],
            ["portrait", "qualifier", "tags", "true_identity"],
        ),
    ],
)
def test_a5_a_mask_the_document_cannot_honour_is_a_422_naming_the_keys(
    rig: Rig, client: TestClient, mask: list[str], keys: list[str]
) -> None:
    # kills: str(exc) in the message; dropping the keys; flipping TABLE_ASSETS_SERVED
    document = _a_refusable_npc(rig)
    refused = _confirm(client, rig.campaign, rig.body(document=document, mask=mask))
    assert refused.status_code == 422
    detail = _detail(refused)
    assert (detail["code"], detail["field"], detail["keys"], detail["retryable"]) == (
        "validation_failed",
        "mask",
        keys,
        False,
    )
    assert detail["message"] == reveals_api.INVALID_MESSAGE
    assert CANARY_IDENTITY not in refused.text and "Vashti" not in refused.text and "harbour" not in refused.text
    assert rig.actions() == [], "a refused Confirm writes nothing"


def test_a5_the_constant_is_what_refuses_an_asset(
    rig: Rig, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _a_refusable_npc(rig)
    assert _confirm(client, rig.campaign, rig.body(document=document, mask=["portrait"])).status_code == 422
    monkeypatch.setattr(reveals_mod, "TABLE_ASSETS_SERVED", True)
    assert _confirm(client, rig.campaign, rig.body(document=document, mask=["name", "portrait"])).status_code == 200


@pytest.mark.parametrize("mask", [["all"], ["name", "name"], ["Not A Key"], [], ["*"]])
def test_a5_a_mask_the_body_refuses_names_no_keys_and_ignores_ownership(
    rig: Rig, client: TestClient, mask: list[str]
) -> None:
    # kills: a 404 that depends on the mask for a stranger (Critic C-4)
    mine = _confirm(client, rig.campaign, rig.body(mask=mask))
    assert mine.status_code == 422
    assert _detail(mine) == {
        "code": "validation_failed",
        "message": "That request isn't valid.",
        "retryable": False,
        "field": "mask",
    }
    _as(GM_B)
    stranger = _confirm(client, rig.campaign, rig.body(mask=mask))
    assert _shape(stranger) == _shape(mine), "the body's 422 comes before ownership"


# ── A6, A7: audiences ────────────────────────────────────────────────────────


def _audiences(rig: Rig) -> list[dict[str, Any]]:
    removed = rig.seat("removed", 3)
    with rig.twin.db.transaction() as unit:
        elsewhere = rig.twin.campaigns.create(unit, owner_id=GM_A, name="Aubade").id
    foreign = rig.seat("confirmed", 4, campaign=elsewhere)
    unknown = "prt_" + "q" * 22
    return [
        {"kind": "participants", "participant_ids": [removed]},
        {"kind": "participants", "participant_ids": [foreign]},
        {"kind": "participants", "participant_ids": [unknown]},
        {"kind": "participants", "participant_ids": ["nope"]},
        {"kind": "everyone_seated"},
    ]


def test_a6_every_audience_refusal_is_one_422_and_a_stranger_gets_the_404(rig: Rig, client: TestClient) -> None:
    # kills: a 404 for a malformed participant id; shape-checking participants at the route;
    # skipping active_participants
    audiences = _audiences(rig)
    refused = [_confirm(client, rig.campaign, rig.body(audience=a)) for a in audiences]
    assert {r.status_code for r in refused} == {422}
    assert {_shape(r) for r in refused} == {_shape(refused[0])}
    assert _detail(refused[0])["field"] == "audience" and "keys" not in _detail(refused[0])
    assert rig.actions() == []

    their_campaign, their_session, their_document = rig.stranger(GM_THIRD)
    _as(GM_B)
    for audience in audiences:
        mine = _confirm(client, rig.campaign, rig.body(audience=audience))
        theirs = _confirm(
            client, their_campaign, rig.body(audience=audience, document=their_document, session=their_session)
        )
        for stranger in (mine, theirs):
            assert stranger.status_code == 404 and stranger.json() == NOT_FOUND, audience


def test_a7_everyone_seated_expands_to_confirmed_seats_only(rig: Rig, client: TestClient) -> None:
    # kills: expanding to active_participants
    rig.seat("open", 3)
    rig.seat("offered", 4)
    rig.seat("accepted", 5)
    confirmed = {rig.seat("confirmed", 6), rig.seat("confirmed", 7)}
    answer = _answer(_confirm(client, rig.campaign, rig.body(audience={"kind": "everyone_seated"}))).state
    assert answer is not None
    named = {getattr(entry.slot, "participant_id", None) for entry in answer.slots}
    assert named == {None, *confirmed}, "the table slot, and one slot per confirmed seat"
    copies = [entry.live for entry in answer.slots if entry.live is not None]
    assert len(copies) == 2 and len({c.disclosure_id for c in copies}) == 1


# ── A8: documents and versions ───────────────────────────────────────────────


def test_a8_an_archived_document_and_an_unsealed_or_unknown_version_are_422s(rig: Rig, client: TestClient) -> None:
    # kills: accepting unsealed versions
    archived = rig.new_document({"name": "Old"})
    with rig.twin.db.transaction() as unit:
        rig.twin.documents.set_archived(unit, rig.campaign, archived, archived=True)
    gone = _confirm(client, rig.campaign, rig.body(document=archived))
    assert gone.status_code == 422 and _detail(gone)["field"] == "document_id"

    rig.write(rig.document, {"name": "Newer"})
    open_version = _confirm(client, rig.campaign, rig.body(version=2))
    assert open_version.status_code == 422 and _detail(open_version)["field"] == "version"
    unknown = _confirm(client, rig.campaign, rig.body(version=999))
    assert unknown.status_code == 422 and _detail(unknown)["field"] == "version"
    assert _confirm(client, rig.campaign, rig.body(version=1)).status_code == 200


# ── A9, A10: pending_delivery and stale_text ─────────────────────────────────


def test_a9_pending_delivery_is_held_and_a_confirmation_releases_it(rig: Rig, client: TestClient) -> None:
    # kills: pending_delivery=False always
    seats = {state: rig.seat(state, 3 + n) for n, state in enumerate(("open", "offered", "accepted", "confirmed"))}
    named = {"kind": "participants", "participant_ids": list(seats.values())}
    _answer(_confirm(client, rig.campaign, rig.body(audience=named)))
    other = rig.new_document({"name": "Table copy"})
    _answer(_confirm(client, rig.campaign, rig.body(document=other)))

    slots = _slots(client.get(_path(rig.campaign)))
    held = {state: slots[seat]["live"]["pending_delivery"] for state, seat in seats.items()}
    assert held == {"open": True, "offered": True, "accepted": True, "confirmed": False}
    assert slots[None]["live"]["pending_delivery"] is False

    rig.confirm_seat(seats["accepted"])
    slots = _slots(client.get(_path(rig.campaign)))
    assert slots[seats["accepted"]]["live"]["pending_delivery"] is False
    assert slots[seats["open"]]["live"]["pending_delivery"] is True


def test_a10_stale_text_compares_the_masked_text_not_version_numbers(rig: Rig, client: TestClient) -> None:
    # kills: comparing version numbers; comparing every key instead of the masked ones
    _answer(_confirm(client, rig.campaign, rig.body(mask=["name"])))

    def stale() -> bool:
        state = client.get(_path(rig.campaign)).json()["state"]
        live = state["slots"][0]["live"]
        assert isinstance(live["stale_text"], bool)
        return bool(live["stale_text"])

    assert stale() is False
    rig.write(rig.document, {"qualifier": "Unmasked change"})
    assert stale() is False, "REVEAL-8: an unmasked key is not the table's text"
    rig.write(rig.document, {"name": "Someone else"})
    assert stale() is True
    rig.write(rig.document, {"name": "Vashti"})
    assert stale() is False, "reverting the text clears it"
    rig.write(rig.document, {"name": "Another"})
    rig.write(rig.document, {"name": "Another again"})
    assert stale() is True, "two autosaves raise one flag"


# ── A11, A12: the read and Stop ──────────────────────────────────────────────


def test_a11_the_read_is_null_without_a_session_and_lists_the_empty_table_slot_with_one(
    rig: Rig, client: TestClient
) -> None:
    # kills: {"state": null} for a foreign campaign; omitting the empty table slot
    assert client.get(_path(rig.campaign)).json() == {
        "schema_version": 1,
        "state": {
            "session_id": rig.session,
            "gen": 1,
            "reveal_epoch": 0,
            "slots": [{"slot": {"kind": "table"}, "seq": 0, "live": None}],
        },
    }
    with rig.twin.db.transaction() as unit:
        no_session = rig.twin.campaigns.create(unit, owner_id=GM_A, name="Quiet").id
    assert client.get(_path(no_session)).json() == {"schema_version": 1, "state": None}
    their_campaign, _, _ = rig.stranger()
    assert client.get(_path(their_campaign)).status_code == 404


def test_a12_stop_clears_what_it_names_and_never_refuses_for_state(rig: Rig, client: TestClient) -> None:
    # kills: refusing a Stop on an archived campaign; writing an audit row with no session
    seats = [rig.seat("confirmed", 3 + n) for n in range(3)]
    named = {"kind": "participants", "participant_ids": seats}
    _answer(_confirm(client, rig.campaign, rig.body(audience=named)))
    other = rig.new_document({"name": "Table copy"})
    _answer(_confirm(client, rig.campaign, rig.body(document=other)))

    after_document = _stop(client, rig.campaign, document=rig.document)
    assert [e["live"] for e in _slots(after_document).values() if e["live"] is not None][0]["document_id"] == other
    assert all(_slots(after_document)[seat]["live"] is None for seat in seats), "every copy of the disclosure"

    after_all = _answer(_stop(client, rig.campaign))
    assert after_all.state is not None and all(e.live is None for e in after_all.state.slots)

    _answer(_confirm(client, rig.campaign, rig.body(document=other)))
    with rig.twin.db.transaction() as unit:
        rig.twin.campaigns.set_archived(unit, rig.campaign, owner_id=GM_A, archived=True)
    cleared = _answer(_stop(client, rig.campaign))
    assert cleared.state is not None and all(e.live is None for e in cleared.state.slots), (
        "an archived campaign is still stopped"
    )

    with rig.twin.db.transaction() as unit:
        quiet = rig.twin.campaigns.create(unit, owner_id=GM_A, name="Quiet").id
    before = rig.actions(quiet)
    assert _stop(client, quiet).json() == {"schema_version": 1, "state": None}
    assert rig.actions(quiet) == before == [], "no session: nothing written"


@pytest.mark.parametrize(
    "extra",
    [{"scope": "document"}, {"scope": "all", "reveal_epoch": 3}, {"scope": "everything"}, {}],
)
def test_a12_a_stop_body_that_is_not_one_is_the_one_422_and_echoes_nothing(
    rig: Rig, client: TestClient, extra: dict[str, Any]
) -> None:
    # kills: parse_body on a union (Critic C-3); echoing the input
    command = secrets.token_urlsafe(16)
    refused = client.post(_path(rig.campaign, "/stop"), json={"schema_version": 1, "command_id": command, **extra})
    assert refused.status_code == 422
    assert _detail(refused)["code"] == "validation_failed"
    assert command not in refused.text and "reveal_epoch" not in refused.text and "everything" not in refused.text


# ── A13, A14: outages and pictures that cannot be expressed ──────────────────


class _Raising:
    """A reveal service that fails every call with `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def view(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error

    display = stop = stale_documents = view


def _serving(rig: Rig, service: Any) -> None:
    rig.service = cast(Reveals, service)


@pytest.mark.parametrize("which", ["read", "confirm", "stop"])
def test_a13_a_busy_service_is_a_retryable_503(rig: Rig, client: TestClient, which: str) -> None:
    # kills: letting RevealBusy propagate (500)
    _serving(rig, _Raising(RevealBusy()))
    response = {
        "read": lambda: client.get(_path(rig.campaign)),
        "confirm": lambda: _confirm(client, rig.campaign, rig.body()),
        "stop": lambda: _stop(client, rig.campaign),
    }[which]()
    assert response.status_code == 503
    assert _detail(response) == {
        "code": "backend_unavailable",
        "message": reveals_api.BUSY_MESSAGE,
        "retryable": True,
    }


def test_a13_a_driver_error_is_a_503_with_no_cause_and_a_content_free_log(
    rig: Rig, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # kills: logging str(exc); chaining the driver's exception onto the answer
    secret = "connection to the row with the Canary identity failed"
    _serving(rig, _Raising(psycopg.OperationalError(secret)))
    with caplog.at_level(logging.DEBUG):
        response = _confirm(client, rig.campaign, rig.body())
    assert response.status_code == 503 and _detail(response)["retryable"] is True
    assert secret not in response.text
    assert "OperationalError" in caplog.text and "Canary" not in caplog.text

    def outage() -> None:
        raise psycopg.OperationalError(secret)

    with pytest.raises(HTTPException) as caught:
        reveals_api.reveal_call(outage)
    assert caught.value.status_code == 503
    assert caught.value.__cause__ is None and caught.value.__context__ is None


class _Unexpressible(InMemoryRevealStore):
    """A store whose picture names a document type the contract does not know."""

    def picture(self, unit: Any, campaign_id: str, *, owner_id: int, now: datetime) -> RevealPicture | None:
        found = super().picture(unit, campaign_id, owner_id=owner_id, now=now)
        if found is None:
            return None
        bad = LiveCopy("dsc_" + "k" * 22, "doc_" + "k" * 22, "wizard", 1, ("name",))
        return RevealPicture(
            found.session_id,
            found.campaign_id,
            found.generation,
            found.reveal_epoch,
            (*found.entries, PictureEntry("participant", "prt_" + "k" * 22, 1, bad)),
        )


def test_a14_a_picture_that_cannot_be_expressed_is_a_503_with_a_content_free_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # kills: mapping an unknown type to npc
    made = Rig(_Unexpressible)
    _use(made)
    _as(GM_A)
    try:
        with caplog.at_level(logging.DEBUG):
            response = TestClient(app).get(_path(made.campaign))
    finally:
        app.dependency_overrides.pop(appmod.get_reveals, None)
        app.dependency_overrides.pop(require_session, None)
    assert response.status_code == 503 and _detail(response)["retryable"] is True
    assert "could not be expressed (ValidationError)" in caplog.text
    assert "wizard" not in caplog.text and "wizard" not in response.text


# ── A15: headers ─────────────────────────────────────────────────────────────


def test_a15_every_answer_carries_the_security_headers(rig: Rig, client: TestClient) -> None:
    # kills: building the router with a plain APIRouter
    answers: list[Response] = [
        client.get(_path(rig.campaign)),
        _confirm(client, rig.campaign, rig.body()),
        _confirm(client, rig.campaign, rig.body(epoch=99)),
        _confirm(client, rig.campaign, rig.body(mask=["tags"])),
        _confirm(client, rig.campaign, {"schema_version": 1}),
        client.get(_path(MISSING_CAMPAIGN)),
        _stop(client, rig.campaign),
        _confirm(client, rig.campaign, rig.body(), headers=EVIL),
    ]
    _as(PLAYER, "player")
    answers.append(client.get(_path(rig.campaign)))
    _signed_out()
    answers.append(client.get(_path(rig.campaign)))
    statuses = {r.status_code for r in answers}
    assert statuses == {200, 401, 403, 404, 409, 422}
    for response in answers:
        headers = {k.lower(): v for k, v in response.headers.items()}
        assert headers["cache-control"] == "no-store", response.status_code
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"


# ── A16: the canary ──────────────────────────────────────────────────────────

CANARY_FIELDS: dict[str, Any] = {
    "name": "Canary name zq1",
    "qualifier": "Canary qualifier zq2",
    "voice": "Canary voice zq3",
    "tell": "Canary tell zq4",
    "attitude": "Canary attitude zq5",
    "wants": "Canary wants zq6",
    "leverage": "Canary leverage zq7",
    "if_attacked": "Canary if attacked zq8",
    "notes": "Canary notes zq9",
    "true_identity": "Canary identity zq10",
    "tags": ["canary tag zq11"],
}
_ID_SHAPED = re.compile(r"^[A-Za-z0-9_-]+$")


def test_a16_no_field_text_reaches_a_body_a_log_line_or_an_audit_row(
    rig: Rig, client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kills: adding field values to the RevealLive mapping; logging the request body or the command id
    document = rig.new_document(dict(CANARY_FIELDS))
    seat = rig.seat("confirmed", 3)
    command = "cmd_Zk3m91Vq0001xxxx"
    answers: list[Response] = []
    with caplog.at_level(logging.DEBUG):
        answers.append(client.get(_path(rig.campaign)))
        answers.append(
            _confirm(client, rig.campaign, rig.body(document=document, mask=["name", "voice", "tell"], command=command))
        )
        answers.append(
            _confirm(
                client,
                rig.campaign,
                rig.body(
                    document=document,
                    mask=["name", "notes"],
                    command="cmd_Zk3m91Vq0002xxxx",
                    audience={"kind": "participants", "participant_ids": [seat]},
                ),
            )
        )
        refusable = rig.body(document=document, mask=["tags", "true_identity", "wants"])
        answers.append(_confirm(client, rig.campaign, refusable))
        answers.append(_confirm(client, rig.campaign, rig.body(document=document, epoch=99)))
        answers.append(_confirm(client, rig.campaign, rig.body(document=document, mask=["all"])))
        malformed = {"kind": "participants", "participant_ids": ["nope"]}
        answers.append(_confirm(client, rig.campaign, rig.body(document=document, audience=malformed)))
        answers.append(client.get(_path(rig.campaign)))
        monkeypatch.setattr(ratelimit, "reveal_stop_limiter", SlidingWindowLimiter(1, 3600))
        answers.append(_stop(client, rig.campaign, document=document, command="cmd_Zk3m91Vq0003xxxx"))
        answers.append(_stop(client, rig.campaign, command="cmd_Zk3m91Vq0004xxxx"))
        _serving(rig, _Raising(psycopg.OperationalError("canary " + CANARY_FIELDS["name"])))
        answers.append(_confirm(client, rig.campaign, rig.body(document=document)))
    assert [r.status_code for r in answers] == [200, 200, 200, 422, 409, 422, 422, 200, 200, 429, 503]

    canaries = [value for value in CANARY_FIELDS.values() if isinstance(value, str)] + CANARY_FIELDS["tags"]
    raw = b"".join(r.content for r in answers).decode("utf-8")
    for canary in canaries:
        assert canary not in raw
    assert "Canary" not in raw and "canary" not in raw, "not even a part of a canary"
    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    for canary in canaries:
        assert canary not in logged
    for command_id in ("cmd_Zk3m91Vq0001xxxx", "cmd_Zk3m91Vq0003xxxx"):
        assert command_id not in logged and command_id not in raw

    with rig.twin.db.transaction() as unit:
        events = rig.twin.audit.for_campaign(unit, rig.campaign)
    assert {e.action for e in events} == {"reveal.displayed", "reveal.stopped"}
    for event in events:
        assert "anary" not in json.dumps(dict(event.detail))
        for key, value in event.detail.items():
            for part in value if isinstance(value, list) else [value]:
                if isinstance(part, str):
                    assert _ID_SHAPED.fullmatch(part), f"{key} holds text-shaped data"


# ── A17: the import boundary ─────────────────────────────────────────────────


def _forbidden_imports(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            tail = module.rsplit(".", 1)[-1]
            if tail in {"policy", "app"} and (node.level > 0 or module.startswith("service.")):
                found.append(module)
            if tail == "reveals" and any(alias.name.startswith("_") for alias in node.names):
                found.append(f"{module}._private")
        elif isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name in {"service.policy", "service.app"}]
    return found


def test_a17_the_routes_import_neither_the_policy_point_nor_the_app() -> None:
    # kills: importing service.policy (T-P18 pins it to service/principals.py)
    assert _forbidden_imports(SOURCE.read_text(encoding="utf-8")) == []
    for source in (
        "from .policy import decide_table\n",
        "from service.policy import decide_gm\n",
        "from .app import app\n",
        "from .reveals import _detail\n",
        "import service.policy\n",
    ):
        assert _forbidden_imports(source), f"the scan must flag {source!r}"
    assert _forbidden_imports("from .reveals import GmReveal, Reveals\n") == []


# ── A18: a failure after the commit never double-acts ────────────────────────


class _StaleFails(Reveals):
    """A reveal service whose stale read fails once, after the work committed."""

    fail_next = False

    def stale_documents(self, campaign_id: str, picture: RevealPicture) -> frozenset[str]:
        if self.fail_next:
            self.fail_next = False
            raise psycopg.OperationalError("the read after the commit failed")
        return super().stale_documents(campaign_id, picture)


def test_a18_a_failed_read_after_a_confirm_or_a_stop_commits_is_a_503_and_its_retry_is_safe() -> None:
    # kills: treating the post-commit read as part of the transaction; a retry that writes twice
    made = Rig(service_type=_StaleFails)
    service = cast(_StaleFails, made.service)
    _use(made)
    _as(GM_A)
    try:
        client = TestClient(app)
        command = secrets.token_urlsafe(16)
        body = made.body(command=command)
        service.fail_next = True
        failed = _confirm(client, made.campaign, body)
        assert failed.status_code == 503 and _detail(failed)["retryable"] is True
        epoch = made.twin.epoch()
        assert made.actions() == ["reveal.displayed"], "the Confirm committed"

        retry = _confirm(client, made.campaign, body)
        assert retry.status_code == 200
        assert made.actions() == ["reveal.displayed"], "no second audit row"
        assert made.twin.epoch() == epoch, "no second epoch"
        assert len(made.twin.live()) == 1

        stop_command = secrets.token_urlsafe(16)
        service.fail_next = True
        assert _stop(client, made.campaign, command=stop_command).status_code == 503
        assert made.actions() == ["reveal.displayed", "reveal.stopped"]
        epoch_after_stop = made.twin.epoch()
        assert epoch_after_stop == epoch + 1

        assert _stop(client, made.campaign, command=stop_command).status_code == 200
        assert made.actions() == ["reveal.displayed", "reveal.stopped", "reveal.stopped"], (
            "the documented cost of an idempotent Stop retry: one more audit row"
        )
        assert made.twin.epoch() == epoch_after_stop + 1, "and one more epoch"
    finally:
        app.dependency_overrides.pop(appmod.get_reveals, None)
        app.dependency_overrides.pop(require_session, None)


# ── C-11: the read's database work is bounded ────────────────────────────────


class _SpyDocuments(InMemoryDocumentStore):
    def __init__(self, db: Any) -> None:
        super().__init__(db)
        self.calls: list[str] = []

    def get(self, unit: Any, campaign_id: str, document_id: str) -> Any:
        self.calls.append("get")
        return super().get(unit, campaign_id, document_id)

    def snapshot(self, unit: Any, campaign_id: str, document_id: str, version_number: int) -> Any:
        self.calls.append("snapshot")
        return super().snapshot(unit, campaign_id, document_id, version_number)


def test_c11_the_read_costs_two_document_reads_per_document_never_per_slot(rig: Rig) -> None:
    # kills: reading per slot instead of per distinct document
    seats = [rig.seat("confirmed", 3 + n) for n in range(3)]
    other = rig.new_document({"name": "Table copy"})
    command = DisplayCommand(
        rig.campaign, rig.session, secrets.token_urlsafe(16), 0, rig.document, 1, ("name",),
        ParticipantsAudience(frozenset(seats)),
    )
    rig.service.display(command, owner_id=GM_A, now=datetime.now(UTC))
    rig.service.display(
        DisplayCommand(rig.campaign, rig.session, secrets.token_urlsafe(16), 1, other, 1, ("name",), TableAudience()),
        owner_id=GM_A,
        now=datetime.now(UTC),
    )
    spy = _SpyDocuments(rig.twin.db)
    service = Reveals(
        rig.twin.db,
        campaigns=rig.twin.campaigns,
        sessions=rig.twin.sessions,
        reveals=rig.rows,
        documents=spy,
        audit=rig.twin.audit,
    )
    view = service.view(rig.campaign, owner_id=GM_A, now=datetime.now(UTC))
    assert view is not None and len(view.picture.entries) == 4, "four slots, two documents"
    assert spy.calls.count("get") == 2 and spy.calls.count("snapshot") == 2

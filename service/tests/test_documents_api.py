"""The GM's document routes (bead 1kg.5.2), through the real app over the
in-memory twins.

Who waits for whom — two autosaves of one document, a create racing its own
retry, a content write under a held campaign lock — is
`tests/test_documents_api_db.py`'s. This file holds what a client observes:
the answers and their order (SEC-3, L-18), the one 404 across tenants (T-2),
the patch's effective fields and per-field conflict (CANVAS-19), additive
restore (CANVAS-26), the library's order and paging, and that no private text
leaves (T-12).

Run from the repo root:
    uv run python -m pytest service/tests/test_documents_api.py -q
"""

from __future__ import annotations

import ast
import asyncio
import functools
import itertools
import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import ValidationError

import config
from service import app as appmod
from service import document_lifecycle_api, documents_api
from service.app import app, get_auth_store, get_timeline_database, require_session
from service.audit_log import AuditEvent, InMemoryAuditLog
from service.auth_store import InMemoryAuthStore, User
from service.campaign_store import InMemoryCampaignStore, MissingParent, shared_rows
from service.db import InMemoryDatabase, InMemoryTransaction
from service.document_store import (
    SEAL_IDLE_S,
    DocumentRecord,
    InMemoryDocumentStore,
    StaleTypeVersion,
    UnknownWriteRevision,
    _version_key,
)
from service.document_wire import encode_history_cursor, encode_library_cursor
from service.hashing import HashingCapacityError, hash_password
from service.ratelimit import RateLimited
from service.session import SessionData
from service.table_session_store import InMemoryTableSessionStore, TableSession, no_slots
from service.workbench_api import (
    FORBIDDEN_ORIGIN_DETAIL,
    FORBIDDEN_ROLE_DETAIL,
    NOT_FOUND_DETAIL,
    REAUTH_FAILED_DETAIL,
)
from service.workbench_contracts import (
    INTEGER_FIELD_MIN,
    Author,
    Document,
    DocumentDeleteRequest,
    DocumentHistoryPage,
    DocumentTypeId,
    DocumentVersionSnapshot,
    ErrorBody,
    LibraryPage,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GM_A, GM_B, PLAYER = 1, 2, 3
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
UNAUTHENTICATED = {"detail": "not signed in"}
CANARY = "Zq7Canary"
MISSING_CAMPAIGN = "cmp_" + "z" * 22
MISSING_DOC = "doc_" + "z" * 22
_COMMANDS = itertools.count()

#: The type each library category lists, with the data a create of it needs.
MINIMAL: dict[str, dict[str, Any]] = {
    "npc": {"name": "Mira"},
    "statblock": {"name": "Ogre", "ac": 11, "hp": 59},
    "handout": {"name": "A letter"},
    "session-notes": {"name": "Session 1"},
    "quest-log": {"name": "Threads"},
    "character-sheet": {"name": "Wren"},
    "lore": {"name": "The Drowned City"},
    "encounter": {"name": "Ambush"},
}
CATEGORY = {"npc": "npcs", "statblock": "bestiary", "session-notes": "session-log"}


# ── The world ────────────────────────────────────────────────────────────────


class _CountingDatabase(InMemoryDatabase):
    """The twin, keeping every unit of work it opened — so a test can say that
    a refusal opened none, and that no unit took a campaign lock."""

    def __init__(self) -> None:
        super().__init__()
        self.units: list[InMemoryTransaction] = []

    @contextmanager
    def transaction(self) -> Iterator[InMemoryTransaction]:
        with super().transaction() as unit:
            self.units.append(unit)
            yield unit


# justification: a store wrapper stands in for a Protocol it does not name;
# each wrapper forwards to the real twin and records or overrides one call.
class _Wrapped:
    """A store that records every call as (unit, "kind.method", owner_id) and
    lets a test replace one method."""

    def __init__(self, inner: Any, kind: str, calls: list[tuple[int, str, Any]],
                 overrides: dict[str, Callable[..., Any]] | None = None) -> None:
        self._inner, self._kind, self._calls = inner, kind, calls
        self._overrides = overrides or {}

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self._calls.append((id(args[0]) if args else 0, f"{self._kind}.{name}", kwargs.get("owner_id")))
            if name in self._overrides:
                return self._overrides[name](target, *args, **kwargs)
            return target(*args, **kwargs)

        return call


@dataclass
class _World:
    db: _CountingDatabase
    stores: documents_api.DocumentStores
    now: list[datetime] = field(default_factory=lambda: [T0])
    calls: list[tuple[int, str, Any]] = field(default_factory=list)

    def campaign(self, owner: int = GM_A, name: str = "Nocturne") -> str:
        with self.db.transaction() as unit:
            return self.stores.campaigns.create(unit, owner_id=owner, name=name, now=self.now[0]).id

    def document(self, campaign: str, doc_type: str = "npc", data: dict[str, Any] | None = None) -> str:
        with self.db.transaction() as unit:
            return self.stores.documents.create(
                unit, campaign, doc_type=DocumentTypeId(doc_type), type_version=1,
                data=data if data is not None else dict(MINIMAL[doc_type]), author=Author.GM, now=self.now[0],
            ).id

    def record(self, campaign: str, document: str) -> DocumentRecord:
        with self.db.transaction() as unit:
            found = self.stores.documents.get(unit, campaign, document)
        assert found is not None
        return found

    def grow(self, campaign: str, document: str, versions: int, key: str = "voice") -> None:
        """Give the document `versions` versions in all, each write ten
        minutes after the last, so each seals the one before."""
        with self.db.transaction() as unit:
            for step in range(1, versions):
                self.now[0] = T0 + timedelta(seconds=SEAL_IDLE_S * step)
                self.stores.documents.write_fields(
                    unit, campaign, document, fields={key: f"take {step}"}, author=Author.GM,
                    base_write_revision=None, now=self.now[0],
                )

    def inject(self, document: str, **changes: Any) -> None:
        """Damage a stored row the way a later release or a defect would."""
        table: Any = shared_rows(self.db, "documents")
        with self.db.transaction() as unit:
            table.replace(unit, document, replace(table.visible(unit)[document], **changes))

    def revision(self, campaign: str) -> int | None:
        with self.db.transaction() as unit:
            return self.stores.campaigns.authz_revision(unit, campaign)

    def damage_version(self, document: str, number: int, data: dict[str, Any]) -> None:
        """Damage a stored version's content, as `inject` damages a document."""
        table: Any = shared_rows(self.db, "document_versions")
        key = _version_key(document, number)
        with self.db.transaction() as unit:
            table.replace(unit, key, replace(table.visible(unit)[key], data=data))

    def spy(self, **overrides: dict[str, Callable[..., Any]]) -> None:
        inner = self.stores
        self.stores = cast(Any, documents_api.DocumentStores(
            cast(Any, _Wrapped(inner.campaigns, "campaigns", self.calls, overrides.get("campaigns"))),
            cast(Any, _Wrapped(inner.documents, "documents", self.calls, overrides.get("documents"))),
        ))


@pytest.fixture
def world() -> Iterator[_World]:
    db = _CountingDatabase()
    made = _World(db, documents_api.DocumentStores(InMemoryCampaignStore(db), InMemoryDocumentStore(db)))
    app.dependency_overrides[documents_api.get_document_stores] = lambda: made.stores
    app.dependency_overrides[get_timeline_database] = lambda: made.db
    app.dependency_overrides[documents_api.get_clock] = lambda: made.now[0]
    yield made
    for dependency in (documents_api.get_document_stores, get_timeline_database, documents_api.get_clock,
                       require_session):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(world: _World) -> TestClient:
    _as(GM_A)
    return TestClient(app)


def _as(user_id: int, role: str = "dm") -> None:
    def session(request: Request) -> SessionData:
        return SessionData(user_id=user_id, role=cast(Any, role))

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
    return f"cmd-{next(_COMMANDS):012d}-abcdef"


def _doc(campaign: str, document: str, rest: str = "") -> str:
    return f"/campaigns/{campaign}/documents/{document}{rest}"


def _create(client: TestClient, campaign: str, doc_type: str = "npc", data: dict[str, Any] | None = None, *,
            command_id: str | None = None, body_campaign: str | None = None) -> Response:
    return client.post(f"/campaigns/{campaign}/documents", json={
        "schema_version": 1, "command_id": command_id or _command(), "campaign_id": body_campaign or campaign,
        "type": doc_type, "type_version": 1, "data": data if data is not None else dict(MINIMAL[doc_type]),
    })


def _patch(client: TestClient, campaign: str, document: str, base: int, fields: dict[str, Any],
           doc_type: str = "npc") -> Response:
    return client.patch(_doc(campaign, document), json={
        "schema_version": 1, "type": doc_type, "type_version": 1, "base_write_revision": base, "fields": fields,
    })


def _restore(client: TestClient, campaign: str, document: str, number: int) -> Response:
    return client.post(_doc(campaign, document, "/restore"), json={"schema_version": 1, "version_number": number})


def _library(client: TestClient, campaign: str, category: str = "npcs", **extra: Any) -> Response:
    body = {"schema_version": 1, "campaign_id": campaign, "category": category, "search": "", "sort": "recent",
            "archived": False, **extra}
    return client.post(f"/campaigns/{campaign}/library", json=body)


def _titles(response: Response) -> list[str]:
    assert response.status_code == 200, response.text
    LibraryPage.model_validate(response.json())
    return [item["title"] for item in response.json()["items"]]


def _walk(client: TestClient, campaign: str, limit: int, **extra: Any) -> list[str]:
    ids: list[str] = []
    cursor: str | None = None
    for _ in range(100):
        answer = _library(client, campaign, limit=limit, **({"cursor": cursor} if cursor else {}), **extra)
        assert answer.status_code == 200, answer.text
        ids += [item["document_id"] for item in answer.json()["items"]]
        cursor = answer.json()["next_cursor"]
        if cursor is None:
            return ids
    raise AssertionError("the walk never ended")


def _versions(client: TestClient, campaign: str, document: str) -> list[dict[str, Any]]:
    answer = client.get(_doc(campaign, document, "/versions"), params={"limit": "50"})
    assert answer.status_code == 200, answer.text
    return list(answer.json()["items"])


def _routes(campaign: str, document: str, *, base: int = 1, number: str = "1") -> list[tuple[str, str, Any]]:
    """Every route, each with a body valid for it."""
    return [
        ("POST", f"/campaigns/{campaign}/library",
         {"schema_version": 1, "campaign_id": campaign, "category": "npcs", "search": "", "sort": "recent",
          "archived": False}),
        ("POST", f"/campaigns/{campaign}/documents",
         {"schema_version": 1, "command_id": "cmd-matrix-0000000001", "campaign_id": campaign, "type": "npc",
          "type_version": 1, "data": {"name": "Mira"}}),
        ("GET", _doc(campaign, document), None),
        ("PATCH", _doc(campaign, document),
         {"schema_version": 1, "type": "npc", "type_version": 1, "base_write_revision": base,
          "fields": {"voice": "low"}}),
        ("GET", _doc(campaign, document, "/versions"), None),
        ("GET", _doc(campaign, document, f"/versions/{number}"), None),
        ("POST", _doc(campaign, document, "/restore"), {"schema_version": 1, "version_number": 1}),
        ("POST", _doc(campaign, document, "/seal"), None),
    ]


def _send(client: TestClient, method: str, path: str, body: Any, **kwargs: Any) -> Response:
    return client.request(method, path, json=body, **kwargs) if body is not None else client.request(
        method, path, **kwargs)


# ── A-1: the posture every route inherits ───────────────────────────────────


def test_every_route_answers_the_scaffoldings_401_403_and_503(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    routes = _routes(campaign, document)
    for method, path, body in routes:
        assert _send(client, method, path, body).status_code in {200, 201}, (method, path)
    _as(PLAYER, "player")
    for method, path, body in routes:
        answer = _send(client, method, path, body)
        assert (answer.status_code, answer.json()) == (403, {"detail": dict(FORBIDDEN_ROLE_DETAIL)}), path
    _as(GM_A)
    for method, path, body in routes:
        if method in {"POST", "PATCH"}:
            refused = _send(client, method, path, body, headers={"origin": "https://evil.example"})
            assert (refused.status_code, refused.json()) == (403, {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}), path
    _signed_out()
    for method, path, body in routes:
        assert _send(client, method, path, body).json() == UNAUTHENTICATED, path
    _as(GM_A)
    app.dependency_overrides[get_timeline_database] = lambda: None
    for method, path, body in routes:
        answer = _send(client, method, path, body)
        assert (answer.status_code, answer.json()["detail"]["code"]) == (503, "backend_unavailable"), path
        assert answer.json()["detail"]["retryable"] is True


def test_malformed_json_is_the_workbench_422_and_echoes_nothing(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    for method, path, body in _routes(campaign, document):
        if body is None:
            continue
        answer = client.request(method, path, content=b'{"' + CANARY.encode() + b'": ',
                                headers={"content-type": "application/json"})
        assert answer.status_code == 422, path
        assert answer.json()["detail"]["code"] == "validation_failed"
        assert CANARY not in answer.text


# ── A-2 to A-4: create ───────────────────────────────────────────────────────


@pytest.mark.parametrize("doc_type", list(MINIMAL))
def test_every_type_creates_reads_back_and_lists(client: TestClient, world: _World, doc_type: str) -> None:
    campaign = world.campaign()
    data = {**MINIMAL[doc_type], "qualifier": "of the Reach", "tags": ["river", "old"]}
    made = _create(client, campaign, doc_type, data)
    assert made.status_code == 201, made.text
    document = Document.model_validate(made.json())
    assert (document.version.number, document.version.sealed, document.version.author.value) == (1, False, "gm")
    assert document.write_revision == 1 and document.type.value == doc_type
    read = client.get(_doc(campaign, document.document_id))
    assert read.status_code == 200 and read.json() == made.json()
    assert read.json()["data"] == data
    listed = _library(client, campaign, CATEGORY.get(doc_type, "documents")).json()["items"]
    assert [(i["document_id"], i["title"], i["qualifier"], i["tags"]) for i in listed] == [
        (document.document_id, data["name"], "of the Reach", ["river", "old"])
    ]


def test_a_create_is_idempotent_by_its_command_id_in_its_campaign(client: TestClient, world: _World) -> None:
    campaign, other = world.campaign(), world.campaign(name="Second")
    key = _command()
    first = _create(client, campaign, data={"name": "Mira"}, command_id=key)
    again = _create(client, campaign, data={"name": "Someone else", "voice": "x"}, command_id=key)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json(), "the first document, as it is now"
    assert [i["document_id"] for i in _library(client, campaign).json()["items"]] == [first.json()["document_id"]]
    elsewhere = _create(client, other, command_id=key)
    assert elsewhere.status_code == 201 and elsewhere.json()["document_id"] != first.json()["document_id"]


def test_create_refusals_fail_before_any_transaction(client: TestClient, world: _World) -> None:
    campaign, other = world.campaign(), world.campaign(name="Second")
    opened = len(world.db.units)
    no_hp = _create(client, campaign, "statblock", {"name": "Ogre", "ac": 11})
    assert (no_hp.status_code, no_hp.json()["detail"]["code"]) == (422, "validation_failed")
    wrong = _create(client, campaign, body_campaign=other)
    assert (wrong.status_code, wrong.json()["detail"]["field"]) == (422, "campaign_id")
    undeclared = _create(client, campaign, data={"name": "Mira", "secret_ally": "x"})
    assert undeclared.status_code == 422 and "secret_ally" not in undeclared.text
    newer = client.post(f"/campaigns/{campaign}/documents", json={
        "schema_version": 1, "command_id": _command(), "campaign_id": campaign, "type": "npc", "type_version": 2,
        "data": {"name": "Mira"}})
    assert (newer.status_code, newer.json()["detail"]["code"], newer.json()["detail"]["field"]) == (
        422, "unsupported_schema_version", "type_version")
    assert len(world.db.units) == opened, "no refusal opened a transaction"


# ── A-5: read ────────────────────────────────────────────────────────────────


def test_a_read_is_the_one_404_for_anything_not_the_callers_here(client: TestClient, world: _World) -> None:
    mine, second, theirs = world.campaign(), world.campaign(name="Second"), world.campaign(GM_B)
    document, elsewhere, foreign = world.document(mine), world.document(second), world.document(theirs)
    reference = _shape(client.get(_doc(mine, MISSING_DOC)))
    assert reference[0] == 404 and json.loads(reference[1]) == NOT_FOUND
    for path in (_doc(theirs, foreign), _doc(mine, foreign), _doc(mine, elsewhere), _doc("cmp_bad", document),
                 _doc(mine, "doc_bad"), _doc(MISSING_CAMPAIGN, document)):
        assert _shape(client.get(path)) == reference, path
    assert client.get(_doc(mine, document)).status_code == 200


def test_a_read_drops_an_undeclared_stored_key_and_refuses_a_newer_type_version(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    world.inject(document, data={"name": "Mira", "secret_ally": CANARY})
    read = client.get(_doc(campaign, document))
    assert read.status_code == 200 and read.json()["data"] == {"name": "Mira"} and CANARY not in read.text
    world.inject(document, type_version=2)
    refused = client.get(_doc(campaign, document))
    assert (refused.status_code, refused.json()["detail"]) == (409, {
        "code": "document_unsupported", "message": documents_api.UNSUPPORTED_MESSAGE, "retryable": False})


# ── A-6 to A-11: the field patch ─────────────────────────────────────────────


def test_a_burst_of_autosaves_is_one_version_and_an_idle_gap_opens_the_next(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()
    base = made["write_revision"]
    for step in range(1, 51):
        world.now[0] = T0 + timedelta(seconds=step)
        answer = _patch(client, campaign, made["document_id"], base, {"voice": f"take {step}"})
        assert answer.status_code == 200, answer.text
        base = answer.json()["write_revision"]
    assert base == made["write_revision"] + 50
    history = _versions(client, campaign, made["document_id"])
    assert [(v["number"], v["author"], v["sealed"]) for v in history] == [(1, "gm", False)]
    world.now[0] += timedelta(seconds=SEAL_IDLE_S)
    later = _patch(client, campaign, made["document_id"], base, {"voice": "after a pause"})
    assert later.json()["version"]["number"] == 2
    assert [v["sealed"] for v in _versions(client, campaign, made["document_id"])] == [False, True]


def test_two_clients_on_one_base_touching_different_keys_both_land(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()
    base, document = made["write_revision"], made["document_id"]
    first = _patch(client, campaign, document, base, {"voice": "low"})
    second = _patch(client, campaign, document, base, {"tell": "hums"})
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["write_revision"] == base + 2
    assert {k: second.json()["data"][k] for k in ("voice", "tell")} == {"voice": "low", "tell": "hums"}


def test_a_stale_touched_key_is_a_conflict_naming_keys_only_and_writes_nothing(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()
    document, base = made["document_id"], made["write_revision"]
    assert _patch(client, campaign, document, base, {"voice": "low"}).status_code == 200
    before = world.record(campaign, document)
    refused = _patch(client, campaign, document, base, {"voice": CANARY, "tell": CANARY})
    assert refused.status_code == 409
    assert refused.json() == {"detail": {
        "code": "conflict", "message": documents_api.CONFLICT_MESSAGE, "retryable": False,
        "conflict": {"write_revision": before.write_revision, "fields": ["voice"]}}}
    assert CANARY not in refused.text
    after = world.record(campaign, document)
    assert (after.write_revision, after.data, after.version, after.field_revisions) == (
        before.write_revision, before.data, before.version, before.field_revisions)


def test_a_repeat_of_a_patch_that_landed_is_a_true_no_op(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()
    document, base = made["document_id"], made["write_revision"]
    landed = _patch(client, campaign, document, base, {"voice": "low", "tags": ["a"]})
    before = world.record(campaign, document)
    world.now[0] += timedelta(seconds=30)
    again = _patch(client, campaign, document, base, {"voice": "low", "tags": ["a"]})
    assert again.status_code == 200 and again.json() == landed.json()
    after = world.record(campaign, document)
    assert (after.write_revision, after.updated_at, after.version, after.field_revisions) == (
        before.write_revision, before.updated_at, before.version, before.field_revisions)


def test_a_partly_equal_patch_writes_and_judges_only_what_differs(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()
    document, base = made["document_id"], made["write_revision"]
    moved = _patch(client, campaign, document, base, {"voice": "low"}).json()["write_revision"]
    mixed = _patch(client, campaign, document, base, {"voice": "low", "tell": "hums"})
    assert mixed.status_code == 200, mixed.text
    revisions = world.record(campaign, document).field_revisions
    assert revisions["voice"] == moved, "the equal key is neither written nor judged stale"
    assert revisions["tell"] == mixed.json()["write_revision"] == moved + 1


def test_the_patchs_refusals_come_in_order_and_write_nothing(client: TestClient, world: _World) -> None:
    mine, theirs = world.campaign(), world.campaign(GM_B)
    document, foreign = world.document(mine), world.document(theirs)
    assert _patch(client, theirs, foreign, 1, {"region": "x"}, "lore").json() == NOT_FOUND
    assert _patch(client, mine, document, 1, {"region": "x"}, "lore").json()["detail"]["field"] == "type"
    ahead = _patch(client, mine, document, 2, {"name": "Mira"})
    assert (ahead.status_code, ahead.json()["detail"]["field"]) == (422, "base_write_revision")
    opened = len(world.db.units)
    block = world.document(mine, "statblock")
    cleared = _patch(client, mine, block, 1, {"hp": None}, "statblock")
    assert cleared.status_code == 422 and len(world.db.units) == opened + 1, "refused before any transaction"
    world.inject(block, data={"name": "Ogre", "ac": 11})
    renamed = _patch(client, mine, block, 1, {"name": "Big Ogre"}, "statblock")
    assert (renamed.status_code, renamed.json()["detail"].get("field")) == (422, None)
    assert world.record(mine, block).data == {"name": "Ogre", "ac": 11}
    for damaged in ({"name": "Mira", "secret_ally": "x"},
                    {"name": "Mira", "portrait": {"asset_id": "ast_" + "a" * 22, "media_type": "image", "alt": "a",
                                                  "focal": 3}}):
        world.inject(document, data=damaged)
        refused = _patch(client, mine, document, 1, {"voice": "low"})
        assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "document_unsupported")
        assert world.record(mine, document).data == damaged and world.record(mine, document).write_revision == 1


def test_an_archived_document_and_an_archived_campaign_take_every_write(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()
    document = made["document_id"]
    with world.db.transaction() as unit:
        assert world.stores.documents.set_archived(unit, campaign, document, archived=True, now=T0)
        assert world.stores.campaigns.set_archived(unit, campaign, owner_id=GM_A, archived=True, now=T0)
    patched = _patch(client, campaign, document, made["write_revision"], {"voice": "low"})
    assert patched.status_code == 200 and patched.json()["archived"] is True
    assert client.post(_doc(campaign, document, "/seal")).status_code == 200
    assert _restore(client, campaign, document, 1).status_code == 200
    assert _create(client, campaign).status_code == 201
    assert _library(client, campaign, archived=True).json()["items"][0]["document_id"] == document


# ── A-13 and A-14: history and a version's content ───────────────────────────


def test_history_pages_newest_first_without_gap_or_repeat(client: TestClient, world: _World) -> None:
    mine, theirs = world.campaign(), world.campaign(GM_B)
    document, foreign = world.document(mine), world.document(theirs)
    world.grow(mine, document, 45)
    pages: list[list[int]] = []
    cursor: str | None = None
    while True:
        answer = client.get(_doc(mine, document, "/versions"), params={"cursor": cursor} if cursor else {})
        assert answer.status_code == 200, answer.text
        DocumentHistoryPage.model_validate(answer.json())
        pages.append([v["number"] for v in answer.json()["items"]])
        cursor = answer.json()["next_cursor"]
        if cursor is None:
            break
    assert [len(page) for page in pages] == [20, 20, 5]
    assert [n for page in pages for n in page] == list(range(45, 0, -1))
    for limit in ("0", "51", "abc", "1.0", "1_0", "+5", " 5", "05", ""):
        refused = client.get(_doc(mine, document, "/versions"), params={"limit": limit})
        assert (refused.status_code, refused.json()["detail"]["field"]) == (422, "limit"), repr(limit)
    for bad in ("%%", encode_history_cursor(MISSING_DOC, 3), encode_library_cursor(document), "QUJD",
                encode_history_cursor(document, 9999)):
        refused = client.get(_doc(mine, document, "/versions"), params={"cursor": bad})
        assert (refused.status_code, refused.json()["detail"]["field"]) == (422, "cursor"), bad
    assert client.get(_doc(theirs, foreign, "/versions"),
                      params={"cursor": encode_history_cursor(foreign, 1)}).json() == NOT_FOUND


def test_a_snapshot_is_that_versions_content(client: TestClient, world: _World) -> None:
    mine, theirs = world.campaign(), world.campaign(GM_B)
    document, foreign = world.document(mine), world.document(theirs)
    world.grow(mine, document, 3)
    snap = client.get(_doc(mine, document, "/versions/2"))
    assert snap.status_code == 200
    body = DocumentVersionSnapshot.model_validate(snap.json())
    assert body.data == {"name": "Mira", "voice": "take 1"} and body.version.number == 2
    assert client.get(_doc(mine, document)).json()["data"]["voice"] == "take 2"
    opened = len(world.db.units)
    for number in ("0", "01", "1e3", "-1", "1000001", "x"):
        assert client.get(_doc(mine, document, f"/versions/{number}")).json() == NOT_FOUND, number
    assert len(world.db.units) == opened, "a malformed number is refused before any query"
    assert client.get(_doc(mine, document, "/versions/9")).json() == NOT_FOUND
    assert client.get(_doc(theirs, foreign, "/versions/1")).json() == NOT_FOUND


# ── A-15 and A-16: restore and seal ─────────────────────────────────────────


def test_restore_appends_and_moves_nothing(client: TestClient, world: _World) -> None:
    mine, theirs = world.campaign(), world.campaign(GM_B)
    document, foreign = world.document(mine), world.document(theirs)
    world.grow(mine, document, 3)
    before = [client.get(_doc(mine, document, f"/versions/{n}")).content for n in (1, 2, 3)]
    base = client.get(_doc(mine, document)).json()["write_revision"]
    restored = _restore(client, mine, document, 1)
    assert restored.status_code == 200, restored.text
    assert (restored.json()["version"]["number"], restored.json()["version"]["restored_from"]) == (4, 1)
    assert restored.json()["data"] == {"name": "Mira"} and restored.json()["write_revision"] == base + 1
    after = [client.get(_doc(mine, document, f"/versions/{n}")) for n in (1, 2, 3)]
    assert [answer.content for answer in after[:2]] == before[:2], "sealed versions never change"
    was_open = json.loads(before[2])
    assert after[2].json() == {**was_open, "version": {**was_open["version"], "sealed": True}}, (
        "the open version is sealed, its content untouched")
    assert [v["sealed"] for v in _versions(client, mine, document)] == [True, True, True, True]
    again = _restore(client, mine, document, 1)
    assert again.status_code == 200 and again.json() == restored.json(), "restoring what it equals is a no-op"
    missing = _restore(client, mine, document, 99)
    assert (missing.status_code, missing.json()["detail"]["field"]) == (422, "version_number")
    assert _restore(client, theirs, foreign, 1).json() == NOT_FOUND
    stale = _patch(client, mine, document, base, {"voice": "mine"})
    assert stale.status_code == 409 and stale.json()["detail"]["conflict"]["fields"] == ["voice"]


def test_restore_refuses_a_document_it_would_lose_a_key_of(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    world.grow(campaign, document, 2)
    damaged = {"name": "Mira", "voice": "take 1", "secret_ally": "x"}
    world.inject(document, data=damaged)
    refused = _restore(client, campaign, document, 1)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "document_unsupported")
    assert world.record(campaign, document).data == damaged
    assert len(_versions(client, campaign, document)) == 2


def test_restoring_a_version_this_build_cannot_validate_is_document_unsupported(
    client: TestClient, world: _World
) -> None:
    """Bead ssr (PR #173 review M-1). The chosen version fails the whole-document
    validation: that is the stored content's defect, never the request's, so the
    answer is `409 document_unsupported` (the brief's R7) and not a patch's 422
    — and nothing is appended or changed."""
    campaign = world.campaign()
    document = world.document(campaign)
    world.grow(campaign, document, 2)
    world.damage_version(document, 1, {"name": "Mira", "secret_ally": CANARY})
    before = world.record(campaign, document)
    refused = _restore(client, campaign, document, 1)
    assert (refused.status_code, refused.json()["detail"]) == (409, {
        "code": "document_unsupported", "message": documents_api.UNSUPPORTED_MESSAGE, "retryable": False})
    assert CANARY not in refused.text
    after = world.record(campaign, document)
    assert (after.data, after.write_revision, after.version, after.updated_at) == (
        before.data, before.write_revision, before.version, before.updated_at)
    assert len(_versions(client, campaign, document)) == 2, "no version appended"


def test_seal_closes_the_open_version_once(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()
    document = made["document_id"]
    sealed = client.post(_doc(campaign, document, "/seal"), json={"ignored": True})
    assert sealed.status_code == 200 and sealed.json()["version"]["sealed"] is True
    assert sealed.json()["write_revision"] == made["write_revision"]
    assert client.post(_doc(campaign, document, "/seal")).json() == sealed.json()
    world.now[0] += timedelta(seconds=5)
    after = _patch(client, campaign, document, made["write_revision"], {"voice": "low"})
    assert after.json()["version"] == {**after.json()["version"], "number": 2, "sealed": False}


# ── A-17: the library ────────────────────────────────────────────────────────


def test_the_library_filters_sorts_searches_and_pages_deterministically(
    client: TestClient, world: _World
) -> None:
    campaign = world.campaign()
    for name in ("delta", "Alpha", "charlie", "bravo", "echo"):
        world.document(campaign, data={"name": name})  # all at T0: every Recent row ties
    mira = world.document(campaign, data={"name": "Mira", "qualifier": "the Ferrywoman", "tags": ["River"],
                                         "notes": "hidden-note", "true_identity": "secret-self"})
    lore = world.document(campaign, "lore")
    world.document(campaign, "handout")
    archived = world.document(campaign, data={"name": "Gone"})
    with world.db.transaction() as unit:
        world.stores.documents.set_archived(unit, campaign, archived, archived=True, now=T0)
    everything = _walk(client, campaign, 25)
    assert len(everything) == 6 and archived not in everything
    for limit in (1, 2, 3):
        assert _walk(client, campaign, limit) == everything, limit
    assert _walk(client, campaign, 2, sort="name") == _walk(client, campaign, 25, sort="name")
    assert _titles(_library(client, campaign, sort="name")) == ["Alpha", "bravo", "charlie", "delta", "echo", "Mira"]
    assert [i["document_id"] for i in _library(client, campaign, archived=True).json()["items"]] == [archived]
    assert {i["type"] for i in _library(client, campaign, "documents").json()["items"]} == {"lore", "handout"}
    assert [i["document_id"] for i in _library(client, campaign, "documents", type="lore").json()["items"]] == [lore]
    for term in ("FERRY", "river", "mira"):
        assert _titles(_library(client, campaign, search=term)) == ["Mira"], term
    for term in ("hidden-note", "secret-self"):
        assert _titles(_library(client, campaign, search=term)) == [], term
    unfiltered = client.post(f"/campaigns/{campaign}/library", params={"search": "mira"}, json={
        "schema_version": 1, "campaign_id": campaign, "category": "npcs", "search": "", "sort": "recent",
        "archived": False})
    assert len(unfiltered.json()["items"]) == 6, "a query parameter changes nothing"
    page = _library(client, campaign).json()
    assert (page["campaign_id"], page["category"]) == (campaign, "npcs")
    assert mira in everything


def test_a_row_the_contract_cannot_carry_is_skipped_counted_and_paged_past(
    client: TestClient, world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    campaign = world.campaign()
    first, middle, last = (world.document(campaign, data={"name": n}) for n in ("a", "b", "c"))
    world.inject(middle, data={"name": ""})
    with caplog.at_level(logging.WARNING, logger="service.documents_api"):
        pages = [_library(client, campaign, sort="name", limit=1)]
        while pages[-1].json()["next_cursor"]:
            pages.append(_library(client, campaign, sort="name", limit=1, cursor=pages[-1].json()["next_cursor"]))
    assert [[i["document_id"] for i in p.json()["items"]] for p in pages] == [[first], [], [last], []]
    assert "1 library row(s) could not be listed" in caplog.text


def test_a_library_query_without_a_limit_answers_twenty_five_rows(client: TestClient, world: _World) -> None:
    """Bead ssr (PR #173 review M-2): LIB-23's twenty-five, the wire contract's
    "default 25" — the page a client gets when it sends no `limit`."""
    campaign = world.campaign()
    for number in range(26):
        world.document(campaign, data={"name": f"Npc {number:02d}"})
    page = _library(client, campaign, sort="name")
    assert page.status_code == 200, page.text
    assert [item["title"] for item in page.json()["items"]] == [f"Npc {number:02d}" for number in range(25)]
    assert page.json()["next_cursor"] is not None
    rest = _library(client, campaign, sort="name", cursor=page.json()["next_cursor"]).json()
    assert ([item["title"] for item in rest["items"]], rest["next_cursor"]) == (["Npc 25"], None)


def test_a_library_cursor_of_another_campaign_or_a_deleted_anchor_is_a_422(
    client: TestClient, world: _World
) -> None:
    campaign, other = world.campaign(), world.campaign(name="Second")
    anchor = world.document(campaign, data={"name": "a"})
    world.document(campaign, data={"name": "b"})
    world.document(other, data={"name": "z"})
    world.document(other, data={"name": "y"})
    elsewhere = _library(client, other, limit=1).json()["next_cursor"]
    refused = _library(client, campaign, cursor=elsewhere)
    assert (refused.status_code, refused.json()["detail"]["field"]) == (422, "cursor")
    cursor = _library(client, campaign, sort="name", limit=1).json()["next_cursor"]
    assert cursor == encode_library_cursor(anchor)
    with world.db.transaction() as unit:
        assert world.stores.documents.delete(unit, campaign, anchor)
    gone = _library(client, campaign, sort="name", limit=1, cursor=cursor)
    assert (gone.status_code, gone.json()["detail"]["field"]) == (422, "cursor")
    for bad in ("%%", encode_history_cursor(anchor, 2)):
        assert _library(client, campaign, cursor=bad).status_code == 422


# ── A-18: the one 404 across tenants, and ownership first ────────────────────


def test_every_route_answers_the_one_404_for_whatever_is_not_the_callers(
    client: TestClient, world: _World
) -> None:
    mine, second, theirs = world.campaign(), world.campaign(name="Second"), world.campaign(GM_B)
    elsewhere, foreign = world.document(second), world.document(theirs)
    reference = _shape(client.get(_doc(mine, MISSING_DOC)))
    cases = [(theirs, foreign), (mine, foreign), (mine, elsewhere), (mine, MISSING_DOC), ("cmp_bad", "doc_bad"),
             (MISSING_CAMPAIGN, foreign), (mine, "doc_bad")]
    for campaign, document in cases:
        for method, path, body in _routes(campaign, document):
            if path.endswith(("/library", "/documents")) and campaign == mine:
                continue
            assert _shape(_send(client, method, path, body)) == reference, (method, path)
        for provoking in ({"type": "lore", "fields": {"region": "x"}}, {"base_write_revision": 99},
                          {"base_write_revision": 1, "fields": {"voice": "x"}}):
            body = {"schema_version": 1, "type": "npc", "type_version": 1, "base_write_revision": 1,
                    "fields": {"name": "x"}, **provoking}
            assert _shape(client.patch(_doc(campaign, document), json=body)) == reference, (campaign, provoking)
        assert _shape(_restore(client, campaign, document, 99)) == reference
    assert _shape(client.get(_doc(mine, world.document(mine), "/versions/9"))) == reference


def test_the_first_store_call_of_every_transaction_is_the_ownership_read(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    world.grow(campaign, document, 2)
    world.spy()
    for method, path, body in _routes(campaign, document, base=2):
        assert _send(client, method, path, body).status_code in {200, 201}, path
    firsts: dict[int, tuple[str, Any]] = {}
    for unit, call, owner in world.calls:
        firsts.setdefault(unit, (call, owner))
    assert len(firsts) == 8
    assert set(firsts.values()) == {("campaigns.get", GM_A)}


# ── A-19 and A-20: body caps ─────────────────────────────────────────────────


def _largest_statblock() -> dict[str, Any]:
    """Every text at its bound in four-byte code points (the wire contract's
    largest valid document)."""
    wide = chr(0x1F600)
    text, item = wide * 200, wide * 2000
    entries = [{"name": text, "text": item} for _ in range(100)]
    data: dict[str, Any] = {"name": text, "qualifier": text, "tags": [item] * 100, "ac": 1_000_000,
                            "hp": 1_000_000, "xp": INTEGER_FIELD_MIN,
                            "abilities": {k: 99 for k in ("str", "dex", "con", "int", "wis", "cha")}}
    for key in ("ac_note", "hit_dice", "speed", "size", "creature_type", "alignment", "saving_throws", "skills",
                "damage_immunities", "condition_immunities", "senses", "languages", "challenge_rating"):
        data[key] = text
    for key in ("traits", "actions", "bonus_actions", "reactions", "legendary_actions"):
        data[key] = entries
    return data


def test_the_largest_valid_document_fits_and_larger_bodies_do_not(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    payload = {"schema_version": 1, "command_id": _command(), "campaign_id": campaign, "type": "statblock",
               "type_version": 1, "data": _largest_statblock()}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert 5_000_000 < len(body) < documents_api.DOCUMENT_BODY_MAX_BYTES
    made = client.post(f"/campaigns/{campaign}/documents", content=body, headers={"content-type": "application/json"})
    assert made.status_code == 201, made.text[:200]
    too_big = b'{"schema_version": 1, "pad": "' + b"x" * documents_api.DOCUMENT_BODY_MAX_BYTES + b'"}'
    for method, path in (("POST", f"/campaigns/{campaign}/documents"),
                         ("PATCH", _doc(campaign, made.json()["document_id"]))):
        refused = client.request(method, path, content=too_big, headers={"content-type": "application/json"})
        assert (refused.status_code, refused.json()["detail"].get("field")) == (422, None), path
    small_cap = b'{"schema_version": 1, "version_number": 1, "pad": "' + b"x" * 9000 + b'"}'
    refused = client.post(_doc(campaign, made.json()["document_id"], "/restore"), content=small_cap,
                          headers={"content-type": "application/json"})
    assert refused.status_code == 422


def test_a_document_body_over_the_cap_is_refused_before_it_is_read_to_the_end() -> None:
    chunk = b"x" * (4 * 1024 * 1024)
    sent: list[int] = []

    async def receive() -> dict[str, Any]:
        sent.append(1)
        return {"type": "http.request", "body": chunk, "more_body": len(sent) < 3}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(RequestValidationError):
        asyncio.run(documents_api.read_document_body(request))
    assert len(sent) == 2, "the third chunk is never read"


def test_nginx_lets_a_document_body_through_to_the_service() -> None:
    conf = (REPO_ROOT / "ui" / "nginx.conf").read_text(encoding="utf-8")
    block = re.search(r"location /campaigns \{(.*?)\}", conf, re.S)
    assert block is not None
    size = re.search(r"client_max_body_size\s+(\d+)([kKmM]?);", block.group(1))
    assert size is not None, "the /campaigns location raises nginx's 1 MB default"
    scale = {"": 1, "k": 1024, "m": 1024 * 1024}[size.group(2).lower()]
    assert int(size.group(1)) * scale >= documents_api.DOCUMENT_BODY_MAX_BYTES


# ── A-21 and A-22: no private text leaves ────────────────────────────────────


def test_no_private_text_reaches_a_refusal_or_a_log_line(
    client: TestClient, world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    campaign = world.campaign()
    made = _create(client, campaign, data={"name": CANARY, "voice": CANARY, "notes": CANARY, "tags": [CANARY]})
    assert made.status_code == 201
    document, base = made.json()["document_id"], made.json()["write_revision"]
    assert _patch(client, campaign, document, base, {"voice": "moved"}).status_code == 200
    world.now[0] += timedelta(seconds=1)
    refusals = [
        _create(client, campaign, "statblock", {"name": CANARY, "ac": 1, "hp": CANARY}),
        _create(client, campaign, data={"name": CANARY, CANARY.lower(): CANARY}),
        _patch(client, campaign, document, base, {"voice": CANARY}),
        _patch(client, campaign, document, base + 5, {"tell": CANARY}),
        _patch(client, campaign, document, base, {"region": CANARY}, "lore"),
        _library(client, campaign, search=CANARY * 12),
        _library(client, campaign, search=f"{CANARY} nothing"),
        _library(client, campaign, cursor=CANARY),
        client.get(_doc(campaign, document, "/versions"), params={"cursor": CANARY}),
    ]
    assert [r.status_code for r in refusals] == [422, 422, 409, 422, 422, 422, 200, 422, 422]
    def outage_on_read(real: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError(f"server said {CANARY}")

    world.spy(documents={"get": outage_on_read})
    outage = client.get(_doc(campaign, document))
    assert outage.status_code == 503
    for answer in [*refusals, outage]:
        assert CANARY.lower() not in answer.text.lower()
    ours = [r for r in caplog.records if r.name.startswith("service")]
    assert ours, "the refusals were logged"
    assert all(CANARY.lower() not in r.getMessage().lower() for r in ours)
    assert "OperationalError" in caplog.text


def test_the_openapi_of_the_document_routes_names_no_owner() -> None:
    paths = {path: item for path, item in app.openapi()["paths"].items() if "/documents" in path or "/library" in path}
    assert len(paths) == 10
    text = json.dumps(paths)
    assert "owner_id" not in text and "password" not in text


# ── A-23 to A-25: no campaign lock; a store's refusal rolls back ─────────────


def test_no_document_route_takes_the_campaign_lock_or_moves_the_revision(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    world.grow(campaign, document, 2)
    revision = world.revision(campaign)
    opened = len(world.db.units)
    for method, path, body in _routes(campaign, document, base=2):
        assert _send(client, method, path, body).status_code in {200, 201}, path
    units = world.db.units[opened:]
    assert len(units) == 8
    assert [unit.campaign_locks for unit in units] == [[]] * 8
    assert world.revision(campaign) == revision


def test_no_composition_calls_a_lock_a_narrowing_or_the_audit_log() -> None:
    tree = ast.parse((REPO_ROOT / "service" / "documents_api.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "write_fields" in called, "the walk sees attribute calls"
    assert called.isdisjoint({"lock_campaign", "advance_authz_revision", "narrow", "append", "set_archived",
                              "delete"})


def test_a_store_refusal_after_a_staged_write_rolls_the_whole_write_back(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    made = _create(client, campaign).json()
    document = made["document_id"]
    before = world.record(campaign, document)

    def write_then_refuse(real: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        raise ValueError("a document keeps at most 1000000 versions")

    world.spy(documents={"write_fields": write_then_refuse})
    refused = _patch(client, campaign, document, made["write_revision"], {"voice": "low"})
    assert (refused.status_code, refused.json()["detail"].get("field")) == (422, None)
    after = world.record(campaign, document)
    assert (after.data, after.write_revision, after.version, after.updated_at) == (
        before.data, before.write_revision, before.version, before.updated_at)


def test_a_document_deleted_between_two_reads_is_the_one_404(client: TestClient, world: _World) -> None:
    campaign = world.campaign()
    document = world.document(campaign)

    def gone(real: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        raise MissingParent("that document has no version")

    reference = _shape(client.get(_doc(campaign, MISSING_DOC)))
    world.spy(documents={"get": gone})
    for path in (_doc(campaign, document), _doc(campaign, document, "/versions")):
        assert _shape(client.get(path)) == reference, path


def test_a_library_query_for_another_campaign_than_its_path_is_refused_before_any_query(
    client: TestClient, world: _World
) -> None:
    campaign, other = world.campaign(), world.campaign(name="Second")
    opened = len(world.db.units)
    refused = client.post(f"/campaigns/{campaign}/library", json={
        "schema_version": 1, "campaign_id": other, "category": "npcs", "search": "", "sort": "recent",
        "archived": False})
    assert (refused.status_code, refused.json()["detail"]["field"]) == (422, "campaign_id")
    assert len(world.db.units) == opened


@pytest.mark.parametrize(
    ("refusal", "status", "code", "field"),
    [
        (StaleTypeVersion("npc", 2, 1), 409, "document_unsupported", None),
        (UnknownWriteRevision(9, 1), 422, "validation_failed", "base_write_revision"),
    ],
)
def test_the_stores_named_refusals_are_answered_and_roll_back(
    client: TestClient, world: _World, refusal: Exception, status: int, code: str, field: str | None
) -> None:
    """The route checks both first, so the store's own refusal is a backstop:
    answered as the route would answer it, never a 500."""
    campaign = world.campaign()
    made = _create(client, campaign).json()

    def refuse(real: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        raise refusal

    world.spy(documents={"write_fields": refuse})
    answer = _patch(client, campaign, made["document_id"], made["write_revision"], {"voice": "low"})
    assert (answer.status_code, answer.json()["detail"]["code"], answer.json()["detail"].get("field")) == (
        status, code, field)
    assert world.record(campaign, made["document_id"]).write_revision == made["write_revision"]


# ── PR-B: archive, unarchive and delete (R9 to R11; bead 1kg.5.8) ───────────
# Who waits for the campaign lock, and RC-15's "not applied yet" under a held
# one, are `tests/test_documents_api_db.py`'s. What a client observes is here.

PASSWORD = {GM_A: "correct horse battery", GM_B: "another good passphrase"}
EMAIL = {GM_A: "gm.a@example.com", GM_B: "gm.b@example.com", PLAYER: "wren@example.com"}


@functools.cache
def _hash(user_id: int) -> str:
    return hash_password(PASSWORD[user_id])


# justification: as `_Wrapped`, a stand-in for a Protocol it does not name.
class _Traced:
    """A lifecycle store that records every call as (unit, "kind.method",
    owner_id, the campaign locks the unit held at that moment), and lets a test
    replace one method."""

    def __init__(self, inner: Any, kind: str, calls: list[tuple[int, str, Any, tuple[Any, ...]]],
                 overrides: dict[str, Callable[..., Any]] | None = None) -> None:
        self._inner, self._kind, self._calls = inner, kind, calls
        self._overrides = overrides or {}

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            unit = args[0] if args else None
            held = tuple(getattr(unit, "campaign_locks", ()))
            self._calls.append((id(unit), f"{self._kind}.{name}", kwargs.get("owner_id"), held))
            if name in self._overrides:
                return self._overrides[name](target, *args, **kwargs)
            return target(*args, **kwargs)

        return call


@dataclass
class _Life:
    world: _World
    stores: document_lifecycle_api.LifecycleStores
    auth: InMemoryAuthStore
    calls: list[tuple[int, str, Any, tuple[Any, ...]]] = field(default_factory=list)

    def session(self, campaign: str, owner: int = GM_A) -> TableSession:
        with self.world.db.transaction() as unit:
            return self.stores.sessions.start(unit, campaign, owner_id=owner, expires_at=T0 + timedelta(hours=12),
                                              command_id=_command(), now=T0)

    def epoch(self, session_id: str) -> int:
        with self.world.db.transaction() as unit:
            found = self.stores.sessions.get(unit, session_id)
        assert found is not None
        return found.reveal_epoch

    def ledger(self, campaign: str) -> list[AuditEvent]:
        with self.world.db.transaction() as unit:
            return self.stores.audit.for_campaign(unit, campaign)

    def archived(self, campaign: str, document: str) -> bool:
        return self.world.record(campaign, document).is_archived

    def archive(self, campaign: str, document: str) -> None:
        """Archive through the store, as another tab's archive left it."""
        with self.world.db.transaction() as unit:
            assert self.world.stores.documents.set_archived(unit, campaign, document, archived=True, now=T0)

    def spy(self, **overrides: dict[str, Callable[..., Any]]) -> None:
        inner = self.stores
        self.stores = document_lifecycle_api.LifecycleStores(*(
            cast(Any, _Traced(getattr(inner, kind), kind, self.calls, overrides.get(kind)))
            for kind in ("campaigns", "documents", "sessions", "audit")
        ))


def _account(user_id: int, role: str = "dm") -> None:
    """`_as`, with the account stashed on the request as `require_session`
    stashes it — what the delete's password check reads."""

    def session(request: Request) -> SessionData:
        request.state.auth_user = User(id=user_id, email=EMAIL.get(user_id, "x@example.com"), role=cast(Any, role),
                                       created_at=T0)
        return SessionData(user_id=user_id, role=cast(Any, role))

    app.dependency_overrides[require_session] = session


@pytest.fixture
def life(world: _World) -> Iterator[_Life]:
    auth = InMemoryAuthStore()
    for user_id in (GM_A, GM_B):
        auth._users.append(User(id=user_id, email=EMAIL[user_id], role="dm", created_at=T0))
        auth._hashes[user_id] = _hash(user_id)
    made = _Life(world, document_lifecycle_api.LifecycleStores(
        world.stores.campaigns, world.stores.documents, InMemoryTableSessionStore(world.db, slot_clear=no_slots),
        InMemoryAuditLog(),
    ), auth)
    app.dependency_overrides[document_lifecycle_api.get_lifecycle_stores] = lambda: made.stores
    app.dependency_overrides[get_auth_store] = lambda: made.auth
    _account(GM_A)
    yield made
    for dependency in (document_lifecycle_api.get_lifecycle_stores, get_auth_store):
        app.dependency_overrides.pop(dependency, None)


def _archive(client: TestClient, campaign: str, document: str) -> Response:
    return client.post(_doc(campaign, document, "/archive"))


def _unarchive(client: TestClient, campaign: str, document: str) -> Response:
    return client.post(_doc(campaign, document, "/unarchive"))


def _delete(client: TestClient, campaign: str, document: str, password: str = PASSWORD[GM_A]) -> Response:
    return client.post(_doc(campaign, document, "/delete"), json={"schema_version": 1, "password": password})


def _lifecycle(campaign: str, document: str) -> list[tuple[str, str, Any]]:
    """R9 to R11, each with a body valid for it."""
    return [
        ("POST", _doc(campaign, document, "/archive"), None),
        ("POST", _doc(campaign, document, "/unarchive"), None),
        ("POST", _doc(campaign, document, "/delete"), {"schema_version": 1, "password": PASSWORD[GM_A]}),
    ]


def _trail(rows: list[AuditEvent]) -> list[tuple[Any, ...]]:
    return [(r.action, r.object_ref, dict(r.detail), r.authz_revision) for r in rows]


# A-1, for R9 to R11


def test_the_lifecycle_routes_answer_the_scaffoldings_401_403_and_503(
    client: TestClient, world: _World, life: _Life
) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    life.archive(campaign, document)
    routes = _lifecycle(campaign, document)
    _account(PLAYER, "player")
    for method, path, body in routes:
        answer = _send(client, method, path, body)
        assert (answer.status_code, answer.json()) == (403, {"detail": dict(FORBIDDEN_ROLE_DETAIL)}), path
    _account(GM_A)
    for method, path, body in routes:
        refused = _send(client, method, path, body, headers={"origin": "https://evil.example"})
        assert (refused.status_code, refused.json()) == (403, {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}), path
    _signed_out()
    for method, path, body in routes:
        assert _send(client, method, path, body).json() == UNAUTHENTICATED, path
    _account(GM_A)
    app.dependency_overrides[get_timeline_database] = lambda: None
    for method, path, body in routes:
        answer = _send(client, method, path, body)
        assert (answer.status_code, answer.json()["detail"]["code"]) == (503, "backend_unavailable"), path
        assert answer.json()["detail"]["retryable"] is True
    app.dependency_overrides[get_timeline_database] = lambda: world.db
    assert life.archived(campaign, document) and life.ledger(campaign) == [], "no refusal changed anything"


def test_a_delete_body_is_read_first_and_echoes_nothing(client: TestClient, world: _World, life: _Life) -> None:
    """B-6 through the route: the body before the database and before the
    password, and a 422 that repeats none of it."""
    campaign = world.campaign()
    document = world.document(campaign)
    life.archive(campaign, document)
    opened = len(world.db.units)
    app.dependency_overrides[get_timeline_database] = lambda: None
    for body in ({"schema_version": 1, "password": CANARY, "document_id": document}, {"schema_version": 1},
                 {"schema_version": 1, "password": ""}, {"schema_version": 1, "password": 1234}):
        refused = client.post(_doc(campaign, document, "/delete"), json=body)
        assert (refused.status_code, refused.json()["detail"]["code"]) == (422, "validation_failed"), body
        assert CANARY not in refused.text and document not in refused.text
    malformed = client.post(_doc(campaign, document, "/delete"), headers={"content-type": "application/json"},
                            content=b'{"schema_version": 1, "password": "' + CANARY.encode() + b'", ')
    assert malformed.status_code == 422 and CANARY not in malformed.text
    app.dependency_overrides[get_timeline_database] = lambda: world.db
    assert len(world.db.units) == opened and life.archived(campaign, document)


def test_the_delete_request_is_write_only_and_hidden_from_repr() -> None:
    """B-6: `SeatRemoveRequest`'s field, with its own model and fixture file."""
    request = DocumentDeleteRequest.model_validate({"schema_version": 1, "password": CANARY})
    assert CANARY not in repr(request) and CANARY not in str(request)
    assert request.password.get_secret_value() == CANARY
    assert DocumentDeleteRequest.model_json_schema()["properties"]["password"]["writeOnly"] is True
    with pytest.raises(ValidationError) as refused:
        DocumentDeleteRequest.model_validate({"schema_version": 1, "password": CANARY, "extra": CANARY})
    assert CANARY not in str(refused.value)
    fixture = json.loads((REPO_ROOT / "contracts" / "workbench" / "v1" / "DocumentDeleteRequest.json").read_text(
        encoding="utf-8"))
    assert fixture["schema"] == "DocumentDeleteRequest" and fixture["valid"] and fixture["invalid"]


# B-1 and B-2: archive and unarchive


def test_archive_narrows_without_the_lock_then_archives_once_under_it(
    client: TestClient, world: _World, life: _Life
) -> None:
    campaign = world.campaign()
    document, other = world.document(campaign), world.document(campaign)
    session = life.session(campaign)
    opened = len(world.db.units)
    answer = _archive(client, campaign, document)
    assert (answer.status_code, answer.content) == (204, b"")
    step_one, step_two = world.db.units[opened:]
    assert step_one.campaign_locks == [], "step one never takes the campaign lock"
    assert step_two.campaign_locks == [(campaign, "exclusive")], "step two takes it, exclusively, as its first lock"
    assert life.archived(campaign, document)
    assert world.revision(campaign) == 1
    assert life.epoch(session.id) == session.reveal_epoch + 2, "narrowed in step one, and again under the lock"
    assert _trail(life.ledger(campaign)) == [("document.archived", document, {"document_id": document}, 1)]

    again = _archive(client, campaign, document)
    assert again.status_code == 204
    assert (world.revision(campaign), len(life.ledger(campaign))) == (1, 1), "no second row, no advance"
    assert life.epoch(session.id) == session.reveal_epoch + 2, "an archived document narrows nothing"

    document_lifecycle_api.archive_step_one(world.db, life.stores, campaign_id=campaign, document_id=other,
                                            owner_id=GM_A)
    assert life.epoch(session.id) == session.reveal_epoch + 3, "step one alone has stopped the display"
    assert not life.archived(campaign, other) and world.revision(campaign) == 1


def test_unarchive_takes_the_lock_only_for_an_archived_document(
    client: TestClient, world: _World, life: _Life
) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    session = life.session(campaign)
    opened = len(world.db.units)
    assert _unarchive(client, campaign, document).status_code == 204
    (only,) = world.db.units[opened:]
    assert only.campaign_locks == [], "a document that is not archived takes no lock"
    assert (world.revision(campaign), life.ledger(campaign)) == (0, [])
    life.archive(campaign, document)
    opened = len(world.db.units)
    answer = _unarchive(client, campaign, document)
    assert (answer.status_code, answer.content) == (204, b"")
    (only,) = world.db.units[opened:]
    assert only.campaign_locks == [(campaign, "exclusive")]
    assert not life.archived(campaign, document)
    assert world.revision(campaign) == 1
    assert _trail(life.ledger(campaign)) == [("document.unarchived", document, {"document_id": document}, 1)]
    assert life.epoch(session.id) == session.reveal_epoch, "a widening narrows nothing"


# B-3: delete, behind the password and the Archived filter


def test_delete_asks_for_the_password_first_and_takes_only_an_archived_document(
    client: TestClient, world: _World, life: _Life
) -> None:
    mine, theirs = world.campaign(), world.campaign(GM_B)
    document, foreign = world.document(mine), world.document(theirs)
    world.grow(mine, document, 3)
    session = life.session(mine)

    opened = len(world.db.units)
    wrong = _delete(client, mine, document, password="wrong " + CANARY)
    assert (wrong.status_code, wrong.json()) == (403, {"detail": dict(REAUTH_FAILED_DETAIL)})
    assert CANARY not in wrong.text
    assert len(world.db.units) == opened, "no transaction opens before the password checks out"

    live = _delete(client, mine, document)
    assert (live.status_code, live.json()) == (409, {"detail": {
        "code": "document_not_archived", "message": document_lifecycle_api.NOT_ARCHIVED_MESSAGE,
        "retryable": False}})
    assert life.epoch(session.id) == session.reveal_epoch, "refused before anything narrows"
    assert (world.revision(mine), life.ledger(mine)) == (0, [])
    assert world.record(mine, document).version.number == 3

    life.archive(mine, document)
    deleted = _delete(client, mine, document)
    assert (deleted.status_code, deleted.content) == (204, b"")
    with world.db.transaction() as unit:
        assert world.stores.documents.get(unit, mine, document) is None
    for path in (_doc(mine, document), _doc(mine, document, "/versions"), _doc(mine, document, "/versions/1")):
        assert client.get(path).json() == NOT_FOUND, path
    assert world.revision(mine) == 1
    assert _trail(life.ledger(mine)) == [("document.deleted", document, {"document_id": document}, 1)]
    assert life.epoch(session.id) == session.reveal_epoch + 2, "narrowed in each step"

    assert _delete(client, mine, document).json() == NOT_FOUND, "a deleted document is a missing one"
    life.archive(theirs, foreign)
    assert _delete(client, theirs, foreign).json() == NOT_FOUND, "the caller's right password, another GM's document"
    assert world.record(theirs, foreign).is_archived


def test_step_two_decides_again_under_the_lock(client: TestClient, world: _World, life: _Life) -> None:
    """A document unarchived by another tab after delete's step one is refused
    under the lock (I-14), and one deleted after archive's step one is the one
    404: step two never acts on what step one read."""
    campaign = world.campaign()
    kept, gone = world.document(campaign), world.document(campaign)
    life.archive(campaign, kept)
    document_lifecycle_api.delete_step_one(world.db, life.stores, campaign_id=campaign, document_id=kept,
                                           owner_id=GM_A)
    assert _unarchive(client, campaign, kept).status_code == 204
    with pytest.raises(HTTPException) as refused:
        document_lifecycle_api.delete_step_two(world.db, life.stores, campaign_id=campaign, document_id=kept,
                                               owner_id=GM_A, now=T0)
    assert (refused.value.status_code, cast(Any, refused.value.detail)["code"]) == (409, "document_not_archived")
    assert not life.archived(campaign, kept)
    assert ([r.action for r in life.ledger(campaign)], world.revision(campaign)) == (["document.unarchived"], 1)

    document_lifecycle_api.archive_step_one(world.db, life.stores, campaign_id=campaign, document_id=gone,
                                            owner_id=GM_A)
    with world.db.transaction() as unit:
        assert world.stores.documents.delete(unit, campaign, gone)
    with pytest.raises(HTTPException) as missing:
        document_lifecycle_api.archive_step_two(world.db, life.stores, campaign_id=campaign, document_id=gone,
                                                owner_id=GM_A, now=T0)
    assert missing.value.status_code == 404
    assert world.revision(campaign) == 1


def test_the_password_check_answers_the_throttle_and_a_hashing_outage_in_the_envelope(
    client: TestClient, world: _World, life: _Life, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = world.campaign()
    document = world.document(campaign)
    life.archive(campaign, document)
    opened = len(world.db.units)

    def throttle(request: object, account: str) -> None:
        raise RateLimited(42)

    monkeypatch.setattr(appmod, "check_auth_attempt", throttle)
    throttled = _delete(client, campaign, document)
    body = ErrorBody.model_validate(throttled.json())
    assert throttled.status_code == 429 and body.detail.code.value == "throttled_user" and body.detail.retryable
    assert body.detail.retry_after_s == 42 and throttled.headers["retry-after"] == "42"
    assert throttled.headers["x-auth-throttled"] == "1"
    monkeypatch.undo()

    def busy(stored: str, password: str) -> bool:
        raise HashingCapacityError("busy")

    monkeypatch.setattr(appmod, "verify_password", busy)
    shed = _delete(client, campaign, document)
    assert (shed.status_code, shed.json()["detail"]["code"]) == (503, "backend_unavailable")
    monkeypatch.undo()
    assert len(world.db.units) == opened and life.archived(campaign, document), "nothing opened, nothing deleted"


def test_the_eleventh_delete_in_a_window_is_throttled_like_a_login_and_deletes_nothing(
    client: TestClient, world: _World, life: _Life
) -> None:
    """Critic item 14: the password spends the login budget —
    `AUTH_RATE_LIMIT_PER_ACCOUNT` (10) a `AUTH_RATE_LIMIT_WINDOW_S` (300 s),
    right or wrong — so the eleventh delete in five minutes is a `429`, before
    any transaction. `1kg.6.4`'s delete dialog is told so."""
    campaign = world.campaign()
    budget = config.AUTH_RATE_LIMIT_PER_ACCOUNT
    documents = [world.document(campaign, data={"name": f"Doc {n}"}) for n in range(budget + 1)]
    for document in documents:
        life.archive(campaign, document)
    for document in documents[:budget]:
        assert _delete(client, campaign, document).status_code == 204
    opened = len(world.db.units)
    last = _delete(client, campaign, documents[-1])
    assert last.status_code == 429, "the eleventh check in the window is refused"
    assert last.json()["detail"]["code"] == "throttled_user"
    assert len(world.db.units) == opened and life.archived(campaign, documents[-1])


# B-4 and B-5


def test_a_stored_document_this_build_cannot_render_is_still_archived_and_deleted(
    client: TestClient, world: _World, life: _Life
) -> None:
    """I-12: the lifecycle answers 204 and never builds a `Document`."""
    campaign = world.campaign()
    for damage in ({"data": {"name": "Mira", "secret_ally": CANARY}}, {"type_version": 2}):
        document = world.document(campaign)
        world.inject(document, **damage)
        unwritable = _patch(client, campaign, document, 1, {"voice": "low"})
        assert unwritable.json()["detail"]["code"] == "document_unsupported", damage
        for answer in (_archive(client, campaign, document), _unarchive(client, campaign, document),
                       _archive(client, campaign, document), _delete(client, campaign, document)):
            assert (answer.status_code, answer.content) == (204, b""), damage
        assert client.get(_doc(campaign, document)).json() == NOT_FOUND


def test_every_lifecycle_audit_row_names_the_document_by_id_and_nothing_else(
    client: TestClient, world: _World, life: _Life
) -> None:
    campaign = world.campaign()
    document = world.document(campaign, data={"name": CANARY, "qualifier": CANARY, "voice": CANARY,
                                              "tags": [CANARY]})
    for answer in (_archive(client, campaign, document), _unarchive(client, campaign, document),
                   _archive(client, campaign, document), _delete(client, campaign, document)):
        assert answer.status_code == 204
    rows = life.ledger(campaign)
    assert [(r.action, r.authz_revision) for r in rows] == [
        ("document.archived", 1), ("document.unarchived", 2), ("document.archived", 3), ("document.deleted", 4)]
    for row in rows:
        assert (row.campaign_id_tombstone, row.actor_kind, row.actor_ref, row.object_kind, row.object_ref,
                row.decision, row.reason_code) == (campaign, "gm", str(GM_A), "document", document, "allowed", None)
        assert dict(row.detail) == {"document_id": document}
        assert CANARY.lower() not in (repr(row) + json.dumps(dict(row.detail))).lower()


# A-18, for R9 to R11: the one 404, and ownership first


def test_every_lifecycle_route_answers_the_one_404_for_whatever_is_not_the_callers(
    client: TestClient, world: _World, life: _Life, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each foreign document is tried first as it is — not archived, which is
    what provokes the owner's `409 document_not_archived` — and then archived.
    The attempt budget is the password tests'; this matrix sends more deletes
    than one window allows, so it is set aside here."""
    monkeypatch.setattr(appmod, "check_auth_attempt", lambda request, account: None)
    mine, second, theirs = world.campaign(), world.campaign(name="Second"), world.campaign(GM_B)
    elsewhere, foreign = world.document(second), world.document(theirs)
    reference = _shape(client.get(_doc(mine, MISSING_DOC)))
    cases = [(theirs, foreign), (mine, foreign), (mine, elsewhere), (mine, MISSING_DOC), ("cmp_bad", "doc_bad"),
             (MISSING_CAMPAIGN, foreign), (mine, "doc_bad")]
    for archived in (False, True):
        for campaign, document in cases:
            for method, path, body in _lifecycle(campaign, document):
                assert _shape(_send(client, method, path, body)) == reference, (archived, method, path)
        if not archived:
            life.archive(theirs, foreign)
            life.archive(second, elsewhere)
    assert world.record(theirs, foreign).is_archived and life.ledger(theirs) == [] and world.revision(theirs) == 0
    assert world.record(second, elsewhere).is_archived and life.ledger(second) == []


def test_every_lifecycle_transaction_reads_ownership_first_and_under_the_lock_in_step_two(
    client: TestClient, world: _World, life: _Life
) -> None:
    """M-A18b and critic item 16: the first store call of every transaction is
    the ownership read with the caller in it; in step two it is made with the
    exclusive lock already held, so step two never trusts step one's read."""
    campaign = world.campaign()
    kept, idle, gone = world.document(campaign), world.document(campaign), world.document(campaign)
    life.session(campaign)
    life.spy()
    for answer in (_archive(client, campaign, kept), _unarchive(client, campaign, kept),
                   _unarchive(client, campaign, idle), _archive(client, campaign, gone),
                   _delete(client, campaign, gone)):
        assert answer.status_code == 204
    firsts: dict[int, tuple[str, Any, tuple[Any, ...]]] = {}
    for unit, call, owner, held in life.calls:
        firsts.setdefault(unit, (call, owner, held))
    assert len(firsts) == 8
    assert {(call, owner) for call, owner, _ in firsts.values()} == {("campaigns.get", GM_A)}
    assert [held for _, _, held in firsts.values() if held] == [((campaign, "exclusive"),)] * 3, (
        "archive's and delete's step two read ownership under the lock")


def test_a_document_or_campaign_gone_between_reads_is_the_one_404(
    client: TestClient, world: _World, life: _Life
) -> None:
    """Critic item 5 for step one's unlocked read, and a campaign whose
    authorisation row is gone by step two's lock: both the one 404, never a 500."""
    campaign = world.campaign()
    document = world.document(campaign)
    life.archive(campaign, document)
    reference = _shape(client.get(_doc(campaign, MISSING_DOC)))
    world.db.authz_state.pop(campaign)
    for method, path, body in _lifecycle(campaign, document):
        assert _shape(_send(client, method, path, body)) == reference, path
    assert life.archived(campaign, document)

    def gone(real: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        raise MissingParent("that document has no version")

    life.spy(documents={"get": gone})
    for method, path, body in _lifecycle(campaign, document):
        assert _shape(_send(client, method, path, body)) == reference, path


# A-21 and A-22, for R9 to R11


def test_no_private_text_reaches_a_lifecycle_refusal_or_a_log_line(
    client: TestClient, world: _World, life: _Life, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    campaign = world.campaign()
    document = world.document(campaign, data={"name": CANARY, "voice": CANARY, "notes": CANARY})
    refusals = [
        _delete(client, campaign, document, password=CANARY),
        _delete(client, campaign, document),
        client.post(_doc(campaign, document, "/delete"), json={"schema_version": 1, "password": CANARY,
                                                               CANARY: CANARY}),
        client.post(_doc(campaign, document, "/delete"), headers={"content-type": "application/json"},
                    content=b'{"password": "' + CANARY.encode() + b'", '),
        _delete(client, campaign, MISSING_DOC, password=CANARY),
    ]
    assert [r.status_code for r in refusals] == [403, 409, 422, 422, 403]

    def outage(real: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError(f"server said {CANARY}")

    life.archive(campaign, document)
    life.spy(documents={"get": outage})
    outages = [_archive(client, campaign, document), _unarchive(client, campaign, document),
               _delete(client, campaign, document)]
    assert [r.status_code for r in outages] == [503, 503, 503]
    for answer in [*refusals, *outages]:
        assert CANARY.lower() not in answer.text.lower()
    ours = [r for r in caplog.records if r.name.startswith("service")]
    assert any(r.name == "service.document_lifecycle_api" for r in ours), "the outages were logged"
    assert all(CANARY.lower() not in r.getMessage().lower() for r in ours)
    assert "OperationalError" in caplog.text
    assert life.ledger(campaign) == []


def test_the_openapi_of_the_lifecycle_routes_names_no_password_and_no_owner() -> None:
    paths = {path: item for path, item in app.openapi()["paths"].items()
             if path.endswith(("/archive", "/unarchive", "/delete")) and "/documents/" in path}
    assert sorted(paths) == [
        "/campaigns/{campaign_id}/documents/{document_id}/archive",
        "/campaigns/{campaign_id}/documents/{document_id}/delete",
        "/campaigns/{campaign_id}/documents/{document_id}/unarchive",
    ]
    for item in paths.values():
        assert set(item) == {"post"} and "204" in item["post"]["responses"]
    text = json.dumps(paths)
    assert "owner_id" not in text and "password" not in text

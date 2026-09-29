"""The GM's named-group routes (agent-forge-harness-btb, PR-2), through the real
app over the in-memory twins.

What a client observes: the answers and their order (SEC-3), the one 404
across tenants, the revision each call site advances or leaves alone, the
ledger row each change writes in its own transaction, and that a group's name
reaches no log, audit row, error body or header. Who waits for whom on the
campaign lock is `tests/test_groups_api_db.py`'s, against PostgreSQL. Every
test names the mutant it kills (the brief's section 9.2, with the adopted
critic's items 2, 3 and 7).

Run from the repo root:
    uv run python -m pytest service/tests/test_groups_api.py -q
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
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import Response

from service import app as appmod
from service import campaigns_api, groups_api
from service.app import app, get_timeline_database, require_session
from service.audit_log import AuditAction, AuditEvent, InMemoryAuditLog
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase, InMemoryTransaction
from service.document_store import InMemoryDocumentStore
from service.eligibility import BackendUnavailable, ChangeOperation, ChangeRecord, NotAppliedYet, no_table_namespace
from service.eligibility_store import GROUPS_PER_CAMPAIGN_MAX, InMemoryEligibilityStore
from service.invites import Role
from service.participant_store import InMemoryParticipantStore
from service.session import SessionData
from service.table_session_store import InMemoryTableSessionStore, no_slots
from service.workbench_api import FORBIDDEN_ORIGIN_DETAIL, FORBIDDEN_ROLE_DETAIL, NOT_FOUND_DETAIL

GM_A, GM_B = 1, 2
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOT_FOUND = {"detail": dict(NOT_FOUND_DETAIL)}
CANARY = "Zx9Canary"
REPO_ROOT = Path(__file__).resolve().parents[2]


class _SpyDatabase(InMemoryDatabase):
    """The twin, keeping every unit it opened, so a test can read the campaign
    locks each one took."""

    def __init__(self) -> None:
        super().__init__()
        self.units: list[InMemoryTransaction] = []

    @contextmanager
    def transaction(self) -> Iterator[InMemoryTransaction]:
        with super().transaction() as unit:
            self.units.append(unit)
            yield unit


@dataclass
class _World:
    db: _SpyDatabase
    stores: groups_api.GroupStores
    participants: InMemoryParticipantStore
    audit: InMemoryAuditLog
    now: list[datetime] = field(default_factory=lambda: [T0])

    def campaign(self, owner: int = GM_A) -> str:
        with self.db.transaction() as unit:
            return self.stores.campaigns.create(unit, owner_id=owner, name="Nocturne", now=self.now[0]).id

    def seat(self, campaign: str, alias: str = "Rook") -> str:
        with self.db.transaction() as unit:
            return self.participants.add(unit, campaign, alias=alias, now=self.now[0]).id

    def remove_seat(self, campaign: str, seat: str) -> None:
        with self.db.transaction() as unit:
            assert self.participants.remove(unit, campaign, seat, now=self.now[0])

    def archive(self, campaign: str, owner: int = GM_A) -> None:
        with self.db.transaction() as unit:
            unit.lock_campaign(campaign, shared=False)
            assert self.stores.campaigns.set_archived(unit, campaign, owner_id=owner, archived=True, now=self.now[0])

    def live_session(self, campaign: str, owner: int = GM_A) -> None:
        with self.db.transaction() as unit:
            self.stores.sessions.start(
                unit, campaign, owner_id=owner, expires_at=self.now[0] + timedelta(hours=11),
                command_id=secrets.token_urlsafe(16), now=self.now[0],
            )

    def epoch(self, campaign: str) -> int:
        with self.db.transaction() as unit:
            live = self.stores.sessions.live_session_for_campaign(unit, campaign)
        assert live is not None
        return int(live.reveal_epoch)

    def revision(self, campaign: str) -> int | None:
        with self.db.transaction() as unit:
            return self.stores.campaigns.authz_revision(unit, campaign)

    def ledger(self, campaign: str) -> list[AuditEvent]:
        with self.db.transaction() as unit:
            return self.audit.for_campaign(unit, campaign)

    def groups(self, campaign: str) -> list[tuple[str, str, frozenset[str]]]:
        with self.db.transaction() as unit:
            return [
                (group.id, group.name, self.stores.eligibility.members(unit, campaign, group.id))
                for group in self.stores.eligibility.list_groups(unit, campaign)
            ]

    def state(self, campaign: str) -> tuple[Any, ...]:
        """Everything a refused request must leave as it was."""
        return self.revision(campaign), [(e.action, e.object_ref) for e in self.ledger(campaign)], self.groups(campaign)


@pytest.fixture
def world() -> Iterator[_World]:
    db = _SpyDatabase()
    documents = InMemoryDocumentStore(db)
    audit = InMemoryAuditLog()
    stores = groups_api.GroupStores(
        campaigns=InMemoryCampaignStore(db),
        eligibility=InMemoryEligibilityStore(db, documents=documents),
        documents=documents,
        sessions=InMemoryTableSessionStore(db, slot_clear=no_slots),
        audit=audit,
        table_namespace=no_table_namespace,
    )
    made = _World(db, stores, InMemoryParticipantStore(db), audit)
    app.dependency_overrides[appmod.get_group_stores] = lambda: made.stores
    app.dependency_overrides[get_timeline_database] = lambda: made.db
    app.dependency_overrides[campaigns_api.get_clock] = lambda: made.now[0]
    yield made
    for dependency in (appmod.get_group_stores, get_timeline_database, campaigns_api.get_clock, require_session):
        app.dependency_overrides.pop(dependency, None)


@pytest.fixture
def client(world: _World) -> TestClient:
    _as(GM_A)
    return TestClient(app)


def _as(user_id: int, role: Role = "dm") -> None:
    app.dependency_overrides[require_session] = lambda: SessionData(user_id=user_id, role=role)


def _signed_out() -> None:
    def refuse() -> SessionData:
        raise HTTPException(status_code=401, detail="authentication required")

    app.dependency_overrides[require_session] = refuse


def _key() -> str:
    return secrets.token_urlsafe(16)


def _create(client: TestClient, campaign: str, name: str = "Scouts", key: str | None = None) -> Response:
    return client.post(
        f"/campaigns/{campaign}/groups", json={"schema_version": 1, "command_id": key or _key(), "name": name}
    )


def _group(client: TestClient, campaign: str, name: str = "Scouts") -> str:
    made = _create(client, campaign, name)
    assert made.status_code == 201, made.text
    return str(made.json()["group_id"])


def _rename(client: TestClient, campaign: str, group: str, name: str) -> Response:
    return client.patch(f"/campaigns/{campaign}/groups/{group}", json={"schema_version": 1, "name": name})


def _remove(client: TestClient, campaign: str, group: str) -> Response:
    return client.post(f"/campaigns/{campaign}/groups/{group}/remove")


def _add(client: TestClient, campaign: str, group: str, seat: str) -> Response:
    return client.post(f"/campaigns/{campaign}/groups/{group}/members/{seat}")


def _drop(client: TestClient, campaign: str, group: str, seat: str) -> Response:
    return client.post(f"/campaigns/{campaign}/groups/{group}/members/{seat}/remove")


def _routes(campaign: str, group: str, seat: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """All six, with a body that is valid wherever one is read."""
    base = f"/campaigns/{campaign}/groups"
    return [
        ("GET", base, None),
        ("POST", base, {"schema_version": 1, "command_id": _key(), "name": "Wardens"}),
        ("PATCH", f"{base}/{group}", {"schema_version": 1, "name": "Wardens"}),
        ("POST", f"{base}/{group}/remove", None),
        ("POST", f"{base}/{group}/members/{seat}", None),
        ("POST", f"{base}/{group}/members/{seat}/remove", None),
    ]


def _send(client: TestClient, method: str, path: str, body: dict[str, Any] | None, **headers: str) -> Response:
    return client.request(method, path, json=body, headers=headers or None)


# ── T-S2, T-S19: the scaffolding on every route ──────────────────────────────


@pytest.mark.parametrize("header", [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}])
def test_a_cross_site_write_is_refused_before_anything_changes(client, world, header):
    """T-S2: kills M-S20 (the router built on a plain `APIRouter`)."""
    campaign = world.campaign()
    group, seat = _group(client, campaign), world.seat(campaign)
    assert _add(client, campaign, group, seat).status_code == 204
    before = world.state(campaign)
    for method, path, body in _routes(campaign, group, seat)[1:]:
        refused = client.request(method, path, json=body, headers=header)
        assert (refused.status_code, refused.json()) == (403, {"detail": dict(FORBIDDEN_ORIGIN_DETAIL)}), path
    assert world.state(campaign) == before


def test_no_session_no_role_and_no_database_are_answered_on_every_route(client, world):
    """T-S19: the one 401 body, the role's 403 and the degraded 503."""
    campaign = world.campaign()
    group, seat = _group(client, campaign), world.seat(campaign)
    routes = _routes(campaign, group, seat)
    _signed_out()
    for method, path, body in routes:
        refused = _send(client, method, path, body)
        assert (refused.status_code, refused.json()) == (401, {"detail": "not signed in"}), path
    _as(GM_A, "player")
    for method, path, body in routes:
        refused = _send(client, method, path, body)
        assert (refused.status_code, refused.json()) == (403, {"detail": dict(FORBIDDEN_ROLE_DETAIL)}), path
    _as(GM_A)
    app.dependency_overrides[get_timeline_database] = lambda: None
    for method, path, body in routes:
        refused = _send(client, method, path, body)
        detail = refused.json()["detail"]
        assert (refused.status_code, detail["code"], detail["retryable"]) == (503, "backend_unavailable", True), path
        assert detail["message"] == groups_api.GROUPS_UNAVAILABLE_MESSAGE


# ── T-S3, T-S4, T-S5: the one 404 and the order ──────────────────────────────


def test_another_gm_gets_the_one_404_on_every_route_and_changes_nothing(client, world):
    """T-S3: kills M-S1 (the owner pre-read dropped) and M-S2 (the pre-read
    reads the campaign without `owner_id`)."""
    campaign = world.campaign()
    world.live_session(campaign)
    group, seat, other = _group(client, campaign), world.seat(campaign), world.seat(campaign, "Wren")
    assert _add(client, campaign, group, seat).status_code == 204
    before, epoch = world.state(campaign), world.epoch(campaign)
    _as(GM_B)
    routes = [*_routes(campaign, group, seat), ("POST", f"/campaigns/{campaign}/groups/{group}/members/{other}", None)]
    for method, path, body in routes:
        refused = _send(client, method, path, body)
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND), path
    assert world.state(campaign) == before and world.epoch(campaign) == epoch


def test_missing_foreign_and_removed_are_the_same_404(client, world):
    """T-S3's other cases, with the critic's item 3: another campaign's seat is
    a 404 to add, and a `204` that narrows nothing to remove."""
    mine, theirs = world.campaign(), world.campaign(GM_B)
    world.live_session(mine)
    group, seat = _group(client, mine), world.seat(mine)
    _as(GM_B)
    foreign_group, foreign_seat = _group(client, theirs, "Theirs"), world.seat(theirs, "Mira")
    _as(GM_A)
    missing = "cmp_" + "z" * 22
    assert _create(client, missing).json() == NOT_FOUND
    assert client.get(f"/campaigns/{missing}/groups").json() == NOT_FOUND
    for refused in (
        _rename(client, mine, foreign_group, "Mine now"),
        _add(client, mine, foreign_group, seat),
        _drop(client, mine, foreign_group, seat),
        _remove(client, mine, foreign_group),
        _add(client, mine, group, foreign_seat),
    ):
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    epoch = world.epoch(mine)
    assert _drop(client, mine, group, foreign_seat).status_code == 204
    assert world.epoch(mine) == epoch
    assert _remove(client, mine, group).status_code == 204
    for refused in (_rename(client, mine, group, "Again"), _add(client, mine, group, seat),
                    _drop(client, mine, group, seat)):
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND)


class _NoTransactions:
    def transaction(self) -> Any:
        raise AssertionError("a malformed id opened a transaction")


@pytest.mark.parametrize("bad", ["campaign", "group", "participant"])
def test_a_malformed_id_is_the_one_404_before_any_transaction(client, world, bad):
    """T-S4: kills M-S14 (a `readable(...)` check dropped)."""
    ids = {"campaign": "cmp_" + "a" * 22, "group": "grp_" + "b" * 22, "participant": "prt_" + "c" * 22}
    ids[bad] = {"campaign": "cmp_short", "group": "prt_" + "b" * 22, "participant": "grp_" + "c" * 22}[bad]
    app.dependency_overrides[get_timeline_database] = lambda: _NoTransactions()
    for method, path, body in _routes(ids["campaign"], ids["group"], ids["participant"]):
        if bad == "group" and path.endswith("/groups"):
            continue
        if bad == "participant" and "/members/" not in path:
            continue
        refused = _send(client, method, path, body)
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND), path


@pytest.mark.parametrize("bad", ["Sc\x07outs", "Sc‮outs", "Sc​outs", "   ", "x" * 41])
def test_an_invalid_name_is_a_422_naming_name_before_ownership(client, world, bad):
    """T-S5: kills M-S15 (the alias rules checked after the ownership read):
    a zero-width space passes the contract and only `check_group_name` refuses
    it, so B sending it to A's campaign must still be a 422, never a 404."""
    mine = world.campaign()
    group = _group(client, mine)
    _as(GM_B)
    own = world.campaign(GM_B)
    own_group = _group(client, own)
    for campaign, target in ((own, own_group), (mine, group)):
        for refused in (_create(client, campaign, bad), _rename(client, campaign, target, bad)):
            assert refused.status_code == 422, (campaign, repr(bad))
            assert refused.json()["detail"]["field"] == "name"
            assert bad.strip() == "" or bad not in refused.text
    assert _create(client, mine, "Wardens").json() == NOT_FOUND
    assert _rename(client, mine, group, "Wardens").json() == NOT_FOUND


# ── T-S6, T-S7: the name and the cap ─────────────────────────────────────────


def test_a_taken_name_is_group_name_taken_on_create_and_rename(client, world):
    """T-S6: kills M-S11 (mapped to `conflict`). The body carries no name."""
    campaign = world.campaign()
    _group(client, campaign, "Scouts")
    other = _group(client, campaign, "Wardens")
    for refused in (_create(client, campaign, "scouts"), _rename(client, campaign, other, "SCOUTS")):
        detail = refused.json()["detail"]
        assert (refused.status_code, detail["code"], detail["message"]) == (
            409, "group_name_taken", groups_api.GROUP_NAME_TAKEN_MESSAGE
        )
        assert "scouts" not in refused.text.lower()
    assert [name for _, name, _ in world.groups(campaign)] == ["Scouts", "Wardens"]


def test_the_fifty_first_group_is_group_cap_reached_and_a_replay_still_answers(client, world):
    """T-S7: kills M-S12 (`GroupLimit` answered as a 422)."""
    campaign = world.campaign()
    first_key = _key()
    first = _create(client, campaign, "Group 0", first_key).json()
    for number in range(1, GROUPS_PER_CAMPAIGN_MAX):
        assert _create(client, campaign, f"Group {number}").status_code == 201
    refused = _create(client, campaign, "One too many")
    detail = refused.json()["detail"]
    assert (refused.status_code, detail["code"], detail["message"]) == (
        409, "group_cap_reached", groups_api.GROUP_CAP_MESSAGE
    )
    replay = _create(client, campaign, "Another name", first_key)
    assert (replay.status_code, replay.json()) == (201, first)


# ── T-S8 to T-S12: each call site, and the revision it advances or not ────────


def test_create_answers_an_empty_group_advances_nothing_and_a_replay_writes_nothing(client, world):
    """T-S8: kills M-S17 (the route passes `command_id=None` or mints its own)."""
    campaign = world.campaign()
    revision, key = world.revision(campaign), _key()
    made = _create(client, campaign, "Scouts", key)
    body = made.json()
    assert made.status_code == 201 and body["member_ids"] == [] and body["name"] == "Scouts"
    assert body["group_id"].startswith("grp_") and set(body) == {
        "schema_version", "group_id", "name", "member_ids", "created_at", "updated_at"
    }
    assert world.revision(campaign) == revision
    replay = _create(client, campaign, "Wardens", key)
    assert (replay.status_code, replay.json()) == (201, body)
    rows = world.ledger(campaign)
    assert [(e.action, e.object_ref, e.authz_revision) for e in rows] == [("group.created", body["group_id"], None)]


def test_a_replay_of_a_removed_group_is_the_one_404(client, world):
    """T-S8b: kills M-S18 (the replayed removed group answered as `201`)."""
    campaign, key = world.campaign(), _key()
    group = _create(client, campaign, "Scouts", key).json()["group_id"]
    assert _remove(client, campaign, group).status_code == 204
    replay = _create(client, campaign, "Scouts", key)
    assert (replay.status_code, replay.json()) == (404, NOT_FOUND)


def test_rename_answers_advances_nothing_takes_no_campaign_lock_and_a_no_op_records_nothing(client, world):
    """T-S8c: kills M-S19 (the pre-read or the route takes the campaign lock)."""
    campaign = world.campaign()
    group, seat = _group(client, campaign), world.seat(campaign)
    assert _add(client, campaign, group, seat).status_code == 204
    revision, rows = world.revision(campaign), len(world.ledger(campaign))
    world.db.units.clear()
    renamed = _rename(client, campaign, group, "Wardens")
    assert renamed.status_code == 200 and renamed.json()["name"] == "Wardens"
    assert renamed.json()["member_ids"] == [seat]
    assert world.db.units and all(unit.campaign_locks == [] for unit in world.db.units)
    again = _rename(client, campaign, group, "Wardens")
    assert again.status_code == 200 and again.json()["name"] == "Wardens"
    assert world.revision(campaign) == revision
    assert [e.action for e in world.ledger(campaign)[rows:]] == ["group.renamed"]


def test_remove_group_advances_once_narrows_twice_frees_the_name_and_a_repeat_does_nothing(client, world):
    """T-S9 with the critic's item 2: kills M-S5 (the shortcut taken for a live
    group) and M-S6 (the shortcut dropped, so a repeat narrows)."""
    campaign = world.campaign()
    world.live_session(campaign)
    group = _group(client, campaign)
    revision, epoch = world.revision(campaign), world.epoch(campaign)
    assert _remove(client, campaign, group).status_code == 204
    assert (world.revision(campaign), world.epoch(campaign)) == (revision + 1, epoch + 2)
    row = world.ledger(campaign)[-1]
    assert (row.action, row.object_ref, row.authz_revision) == ("group.removed", group, revision + 1)
    assert world.groups(campaign) == []
    rows = len(world.ledger(campaign))
    repeat = _remove(client, campaign, group)
    assert (repeat.status_code, repeat.content) == (204, b"")
    assert (world.revision(campaign), world.epoch(campaign), len(world.ledger(campaign))) == (
        revision + 1, epoch + 2, rows
    )
    assert _create(client, campaign, "Scouts").status_code == 201


def test_add_member_advances_once_displays_nothing_and_a_repeat_changes_nothing(client, world):
    """T-S10 and T-S11's first half: kills M-S3 (the service not called) and
    M-S22 (the add path narrows)."""
    campaign = world.campaign()
    world.live_session(campaign)
    group, seat = _group(client, campaign), world.seat(campaign)
    revision, epoch = world.revision(campaign), world.epoch(campaign)
    assert _add(client, campaign, group, seat).status_code == 204
    assert (world.revision(campaign), world.epoch(campaign)) == (revision + 1, epoch)
    row = world.ledger(campaign)[-1]
    assert (row.action, row.authz_revision, dict(row.detail)) == (
        "group.member_added", revision + 1, {"group_id": group, "participant_id": seat}
    )
    rows = len(world.ledger(campaign))
    assert _add(client, campaign, group, seat).status_code == 204
    assert (world.revision(campaign), len(world.ledger(campaign))) == (revision + 1, rows)
    assert world.groups(campaign) == [(group, "Scouts", frozenset({seat}))]
    gone = world.seat(campaign, "Wren")
    world.remove_seat(campaign, gone)
    assert _add(client, campaign, group, gone).json() == NOT_FOUND


def test_remove_member_advances_once_narrows_twice_and_a_non_member_changes_nothing(client, world):
    """T-S11's second half and T-S12 with the critic's items 2 and 3: kills M-S4
    (the service not called) and M-S27 (the non-member shortcut dropped)."""
    campaign = world.campaign()
    world.live_session(campaign)
    group, seat, other = _group(client, campaign), world.seat(campaign), world.seat(campaign, "Wren")
    assert _add(client, campaign, group, seat).status_code == 204
    revision, epoch = world.revision(campaign), world.epoch(campaign)
    assert _drop(client, campaign, group, seat).status_code == 204
    assert (world.revision(campaign), world.epoch(campaign)) == (revision + 1, epoch + 2)
    row = world.ledger(campaign)[-1]
    assert (row.action, row.authz_revision, dict(row.detail)) == (
        "group.member_removed", revision + 1, {"group_id": group, "participant_id": seat}
    )
    rows = len(world.ledger(campaign))
    for stranger in (seat, other, "prt_" + "q" * 22):
        assert _drop(client, campaign, group, stranger).status_code == 204
    assert (world.revision(campaign), world.epoch(campaign), len(world.ledger(campaign))) == (
        revision + 1, epoch + 2, rows
    )


class _Refusing:
    """Stands in for `EligibilityMutations`: every call refuses."""

    def __init__(self, refusal: type[Exception]) -> None:
        self.refusal = refusal

    def __getattr__(self, name: str) -> Any:
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise self.refusal()

        return refuse


@pytest.mark.parametrize(
    ("refusal", "message"),
    [(NotAppliedYet, campaigns_api.NOT_APPLIED_MESSAGE), (BackendUnavailable, groups_api.GROUPS_UNAVAILABLE_MESSAGE)],
)
def test_a_service_refusal_is_a_retryable_503_with_its_own_sentence(client, world, monkeypatch, refusal, message):
    """T-S13: kills M-S13 (`NotAppliedYet` non-retryable, or the busy sentence)."""
    campaign = world.campaign()
    group, seat = _group(client, campaign), world.seat(campaign)
    assert _add(client, campaign, group, seat).status_code == 204
    monkeypatch.setattr(groups_api, "mutations_for", lambda *args, **kwargs: _Refusing(refusal))
    for refused in (_create(client, campaign, "Wardens"), _rename(client, campaign, group, "Wardens"),
                    _remove(client, campaign, group), _add(client, campaign, group, seat),
                    _drop(client, campaign, group, seat)):
        detail = refused.json()["detail"]
        assert (refused.status_code, detail["code"], detail["retryable"], detail["message"]) == (
            503, "backend_unavailable", True, message
        )


def test_an_archived_campaign_still_narrows_and_adds(client, world):
    """T-S14: kills M-S21 (an archived campaign refused)."""
    campaign = world.campaign()
    group, doomed, seat = _group(client, campaign), _group(client, campaign, "Doomed"), world.seat(campaign)
    assert _add(client, campaign, group, seat).status_code == 204
    world.archive(campaign)
    revision = world.revision(campaign)
    assert _drop(client, campaign, group, seat).status_code == 204
    assert _remove(client, campaign, doomed).status_code == 204
    assert world.revision(campaign) == revision + 2
    assert _add(client, campaign, group, seat).status_code == 204


# ── T-S15, T-S16: the ledger ─────────────────────────────────────────────────


def test_the_ledger_of_a_scripted_run_is_exact(client, world):
    """T-S15: kills M-S7 (nothing written), M-S8 (`authz_revision=None`) and
    M-S9 (two actions swapped)."""
    campaign = world.campaign()
    group = _group(client, campaign)
    seat = world.seat(campaign)
    start = world.revision(campaign)
    assert start is not None
    assert _rename(client, campaign, group, "Wardens").status_code == 200
    assert _add(client, campaign, group, seat).status_code == 204
    assert _drop(client, campaign, group, seat).status_code == 204
    assert _remove(client, campaign, group).status_code == 204
    rows = [
        (e.action, e.object_kind, e.object_ref, dict(e.detail), e.authz_revision, e.actor_kind, e.actor_ref)
        for e in world.ledger(campaign)
    ]
    member = {"group_id": group, "participant_id": seat}
    assert rows == [
        ("group.created", "group", group, {"group_id": group}, None, "gm", str(GM_A)),
        ("group.renamed", "group", group, {"group_id": group}, None, "gm", str(GM_A)),
        ("group.member_added", "group", group, member, start + 1, "gm", str(GM_A)),
        ("group.member_removed", "group", group, member, start + 2, "gm", str(GM_A)),
        ("group.removed", "group", group, {"group_id": group}, start + 3, "gm", str(GM_A)),
    ]
    assert world.revision(campaign) == start + 3


def test_the_recorder_fails_closed_and_covers_exactly_the_five_group_operations(client, world):
    """T-S16: kills M-S10 (an unknown operation skipped silently)."""
    assert set(groups_api.GROUP_AUDIT) == {op for op in ChangeOperation if op.value.startswith("group.")}
    assert all(op.value == action.value for op, action in groups_api.GROUP_AUDIT.items())
    assert set(groups_api.GROUP_AUDIT.values()) == {a for a in AuditAction if a.value.startswith("group.")}
    campaign = world.campaign()
    group, seat = _group(client, campaign), world.seat(campaign)
    record = groups_api.audit_recorder(world.audit, campaign_id=campaign, owner_id=GM_A, now=T0)
    for bad in (
        ChangeRecord(ChangeOperation.FIELD_CLASSIFIED, campaign, 1, document_id="doc_" + "d" * 22, field_key="hp"),
        ChangeRecord(ChangeOperation.GROUP_REMOVED, "cmp_" + "o" * 22, 1, group_id=group),
        ChangeRecord(ChangeOperation.MEMBER_ADDED, campaign, 1, group_id=group),
    ):
        with world.db.transaction() as unit, pytest.raises(ValueError, match="not one the group routes record"):
            record(unit, bad)
    before = world.state(campaign)
    stranger = groups_api.mutations_for(world.db, world.stores, campaign_id="cmp_" + "o" * 22, owner_id=GM_A, now=T0)
    with pytest.raises(ValueError):
        stranger.add_member(campaign, group, seat)
    assert world.state(campaign) == before


# ── T-S17, T-S18, T-S21 ──────────────────────────────────────────────────────


def test_a_group_name_reaches_no_log_audit_row_error_body_or_header(client, world, caplog):
    """T-S17: kills M-S16 (the name logged)."""
    caplog.set_level(logging.DEBUG)
    campaign = world.campaign()
    group = _group(client, campaign, CANARY)
    seen: list[Response] = [
        _create(client, campaign, CANARY.lower()),
        _create(client, campaign, CANARY + "​"),
        _rename(client, campaign, _group(client, campaign, "Other"), CANARY.upper()),
    ]
    _as(GM_B)
    seen += [_rename(client, campaign, group, CANARY + "2"), _create(client, campaign, CANARY + "3")]
    for answer in seen:
        assert answer.status_code >= 400
        assert CANARY.lower() not in answer.text.lower()
        assert all(CANARY.lower() not in value.lower() for value in answer.headers.values())
    with pytest.raises(HTTPException) as raised:
        groups_api.group_create(
            world.db, world.stores, campaign_id=campaign, owner_id=GM_A, name=CANARY, command_id=_key(), now=T0
        )
    assert CANARY not in repr(raised.value) and raised.value.__context__ is None
    for event in world.ledger(campaign):
        assert CANARY.lower() not in repr((event, dict(event.detail))).lower()
    for record in caplog.records:
        assert CANARY.lower() not in (record.getMessage() + repr(record.args)).lower()


def test_the_list_is_every_live_group_by_fold_then_id_with_its_live_seats(client, world):
    """T-S18: kills M-S23 (removed groups listed) and M-S24 (members unsorted,
    or a removed seat listed)."""
    campaign, empty = world.campaign(), world.campaign()
    listed = client.get(f"/campaigns/{empty}/groups")
    assert (listed.status_code, listed.json()) == (200, {"schema_version": 1, "items": [], "next_cursor": None})
    seats = sorted(world.seat(campaign, alias) for alias in ("Rook", "Wren", "Mira"))
    wardens, scouts, gone = _group(client, campaign, "wardens"), _group(client, campaign, "Scouts"), _group(
        client, campaign, "Archers"
    )
    for seat in reversed(seats):
        assert _add(client, campaign, scouts, seat).status_code == 204
    world.remove_seat(campaign, seats[1])
    assert _remove(client, campaign, gone).status_code == 204
    page = client.get(f"/campaigns/{campaign}/groups").json()
    assert page["next_cursor"] is None
    assert [(item["group_id"], item["name"], item["member_ids"]) for item in page["items"]] == [
        (scouts, "Scouts", [seats[0], seats[2]]),
        (wardens, "wardens", []),
    ]


_UPDATE_CAMPAIGNS = re.compile(
    r"\bUPDATE\s+campaign\.campaigns\b[^;]*?\bSET\b(.*?)(?:\bWHERE\b|\bRETURNING\b|\bFROM\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)


def owner_writes(source: str, *, sql: bool = False) -> list[int]:
    """Where a statement's SET clause assigns `campaign.campaigns.owner_id`:
    every string a Python module holds (adjacent literals and f-strings are one
    node, so a statement split across lines is read whole), or a migration."""
    texts = [(1, source)] if sql else list(_strings(source))
    return sorted(
        {
            line
            for line, text in texts
            if any(re.search(r"\bowner_id\s*=", clause) for clause in _UPDATE_CAMPAIGNS.findall(text))
        }
    )


def _strings(source: str) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )


PLANTED_OWNER_WRITE = """
A = "UPDATE campaign.campaigns SET name = %s WHERE id = %s AND owner_id = %s"
B = (
    "UPDATE campaign.campaigns "
    f"SET updated_at = now(), owner_id = {NEW} WHERE id = %s"
)
"""


def test_nothing_ever_updates_a_campaigns_owner():
    """T-S21, the critic's item 7: the ownership pre-read (I-4) is safe only
    while `owner_id` is never updated (D-5). Proven on planted source first
    (kills M-S28, a scan that passes vacuously), then run over `service/` and
    the migrations. An ownership transfer must revisit I-4."""
    assert owner_writes(PLANTED_OWNER_WRITE) == [4]
    assert owner_writes("UPDATE campaign.campaigns SET owner_id = 7;", sql=True) == [1]
    modules = [p for p in (REPO_ROOT / "service").rglob("*.py") if "tests" not in p.relative_to(REPO_ROOT).parts]
    migrations = sorted((REPO_ROOT / "service" / "sql" / "migrations").glob("*.sql"))
    assert len(modules) > 50 and len(migrations) >= 19
    found = [(p.name, owner_writes(p.read_text(encoding="utf-8"))) for p in modules]
    found += [(p.name, owner_writes(p.read_text(encoding="utf-8"), sql=True)) for p in migrations]
    assert [hit for hit in found if hit[1]] == []

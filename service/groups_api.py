"""The GM's named groups of seats (agent-forge-harness-btb, owner decision O-3).

Six Workbench routes on a `workbench_router` (`agent-forge-harness-oe6`),
wired into `service/app.py` and importing nothing from it: the application
hands `build_router` its GM gate, its database getter and its stores getter.
Every mutation is `service/eligibility.py`'s (`1ir.2.1`); this module adds
ownership (SEC-2), the answers, and the audit row its recorder writes.

| Route | Answers |
| --- | --- |
| `GET /campaigns/{campaign_id}/groups` | a `GroupPage`: every live group, one page |
| `POST /campaigns/{campaign_id}/groups` | `201`, the `Group` — a replay of its `command_id` too |
| `PATCH /campaigns/{campaign_id}/groups/{group_id}` | the renamed `Group` (the same name too) |
| `POST …/groups/{group_id}/remove` | `204`; a group already removed too |
| `POST …/groups/{group_id}/members/{participant_id}` | `204`; a member already too |
| `POST …/groups/{group_id}/members/{participant_id}/remove` | `204`; a seat that is not a member too |

**The order of checks** (SEC-3, `campaigns_api`'s L-18): origin (403) →
authentication (the one 401) → role (403) → the body, then the name's alias
rules (422) → the database (503) → the path ids' shapes (the one 404, before
any transaction) → **ownership and the group, in one transaction that takes
no lock** (the one 404) → the one service call → its refusals (409, 503).

**Ownership comes before the lock.** The pre-read is `CampaignStore.get` with
the caller in the statement, committed before the service asks for the
campaign lock: a stranger never reaches the lock, so it can neither stall
another GM's reveals nor learn from a lock timeout that a campaign exists.
The owner of a campaign is never updated (D-5), so ownership shown a moment
earlier still holds; a campaign deleted in between is the service's
`MissingParent`, which is the same 404 (inferred decision I-4).

**Two shortcuts, both race-free** (I-11, and the critic's item 3). Removing a
group already removed, or a seat that is not in the group, answers `204` from
the pre-read: no step 1, no audit row and no advance. Otherwise a repeated
toggle would narrow the live session for nothing.

**Locks and revisions are the helper table's** (`docs/ARCHITECTURE.md`).
Create takes the campaign lock and advances nothing; rename takes no campaign
lock and advances nothing; add member, remove member and remove group advance
`authz_revision`, and the two removals are RQ-5's two steps. The campaign's
archived and concluded states are never consulted: a narrowing is never
refused for state (X-3).

**No private text anywhere.** A group's name travels in a body, never a URL;
no refusal repeats it, and every audit row carries ids only (SEC-20, SEC-38).
Failures are logged by `guarded` with an exception's TYPE and SQLSTATE only,
and every refusal is raised outside the handler that caught its cause.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Response

from . import campaign_identity as ident
from .audit_log import ActorKind, AuditAction, AuditLog, Decision, DetailValue, ObjectKind
from .campaign_store import CampaignStore, MissingParent
from .campaigns_api import (
    NOT_APPLIED_MESSAGE,
    WIRE_VERSION,
    conflict,
    get_clock,
    guarded,
    invalid,
    parse_body,
    readable,
    unavailable,
)
from .conversations_api import read_body
from .db import TransactionalDatabase, UnitOfWork
from .document_store import DocumentStore
from .eligibility import (
    BackendUnavailable,
    ChangeOperation,
    ChangeRecord,
    ChangeRecorder,
    EligibilityMutations,
    NotAppliedYet,
    TableNamespaceNarrowing,
)
from .eligibility_store import GROUPS_PER_CAMPAIGN_MAX, EligibilityStore, GroupLimit, GroupNameTaken, check_group_name
from .eligibility_store import Group as StoredGroup
from .session import SessionData
from .table_session_store import TableSessionStore
from .workbench_api import SessionDependency, not_found, workbench_router
from .workbench_contracts import ErrorCode, Group, GroupCreateRequest, GroupPage, GroupPatchRequest

#: Fixed sentences. None interpolates anything a caller sent (X-7);
#: `NOT_APPLIED_MESSAGE` is `campaigns_api`'s, never spelled twice.
GROUPS_UNAVAILABLE_MESSAGE = "Groups are briefly unavailable. Try again."
GROUP_NAME_TAKEN_MESSAGE = "Another group already has that name."
GROUP_CAP_MESSAGE = f"A campaign holds at most {GROUPS_PER_CAMPAIGN_MAX} groups."


@dataclass(frozen=True)
class GroupStores:
    """What a group route composes. `eligibility` is the Protocol and is only
    READ here: every write is `EligibilityMutations`'s (T-G2)."""

    campaigns: CampaignStore
    eligibility: EligibilityStore
    documents: DocumentStore
    sessions: TableSessionStore
    audit: AuditLog
    table_namespace: TableNamespaceNarrowing


#: The five group operations and the ledger's name for each: exactly these,
#: and `field.classified` is not one of them (`1ir.2.7` records its own).
GROUP_AUDIT: Final[Mapping[ChangeOperation, AuditAction]] = MappingProxyType(
    {
        ChangeOperation.GROUP_CREATED: AuditAction.GROUP_CREATED,
        ChangeOperation.GROUP_RENAMED: AuditAction.GROUP_RENAMED,
        ChangeOperation.GROUP_REMOVED: AuditAction.GROUP_REMOVED,
        ChangeOperation.MEMBER_ADDED: AuditAction.GROUP_MEMBER_ADDED,
        ChangeOperation.MEMBER_REMOVED: AuditAction.GROUP_MEMBER_REMOVED,
    }
)
_MEMBER_OPERATIONS: Final = frozenset({ChangeOperation.MEMBER_ADDED, ChangeOperation.MEMBER_REMOVED})


def audit_recorder(audit: AuditLog, *, campaign_id: str, owner_id: int, now: datetime) -> ChangeRecorder:
    """The service's `ChangeRecorder`: one content-free row per change, in the
    unit that made it, so the row commits with the change or not at all
    (SEC-38). It raises — and the change rolls back — for an operation outside
    `GROUP_AUDIT`, for a record of another campaign, or for a record missing
    an id its action needs: a change that cannot be audited is not made."""

    def record(unit: UnitOfWork, change: ChangeRecord) -> None:
        action = GROUP_AUDIT.get(change.operation)
        member = change.operation in _MEMBER_OPERATIONS
        if (
            action is None
            or change.campaign_id != campaign_id
            or change.group_id is None
            or (member and change.participant_id is None)
        ):
            raise ValueError("that change is not one the group routes record")
        detail: dict[str, DetailValue] = {"group_id": change.group_id}
        if member:
            detail["participant_id"] = change.participant_id
        audit.append(
            unit,
            campaign_id=campaign_id,
            actor_kind=ActorKind.GM,
            action=action,
            object_kind=ObjectKind.GROUP,
            decision=Decision.ALLOWED,
            actor_ref=str(owner_id),
            object_ref=change.group_id,
            authz_revision=change.authz_revision,
            detail=detail,
            now=now,
        )

    return record


def mutations_for(
    db: TransactionalDatabase, stores: GroupStores, *, campaign_id: str, owner_id: int, now: datetime
) -> EligibilityMutations:
    """The service for one request, its recorder bound to this caller and
    this campaign. Both extension points are passed by name (I-19)."""
    return EligibilityMutations(
        db,
        eligibility=stores.eligibility,
        documents=stores.documents,
        sessions=stores.sessions,
        table_namespace=stores.table_namespace,
        record=audit_recorder(stores.audit, campaign_id=campaign_id, owner_id=owner_id, now=now),
    )


# ── From the store to the wire ───────────────────────────────────────────────


def group_to_wire(group: StoredGroup, members: frozenset[str]) -> Group:
    """A live group as its GM sees it: its seats that are not removed,
    ascending by code point. No campaign id and no removed state."""
    return Group.model_validate(
        {
            "schema_version": WIRE_VERSION,
            "group_id": group.id,
            "name": group.name,
            "member_ids": sorted(members),
            "created_at": group.created_at.astimezone(UTC),
            "updated_at": group.updated_at.astimezone(UTC),
        }
    )


def checked_name(name: str) -> str:
    """The alias rules (`check_group_name`), which depend on nothing but the
    body, or the 422 naming `name` — raised outside the handler, so the
    refusal chains nothing and names no value."""
    try:
        named = check_group_name(name)
    except ValueError:
        named = ""
    if not named:
        raise invalid("name")
    return named


# ── The compositions ─────────────────────────────────────────────────────────


def _refused[T](work: Callable[[], T]) -> T:
    """Run one service call and map what it refuses (I-22). Each answer is
    raised after the handler has closed, so nothing is chained."""
    answer: HTTPException | None = None
    missing = False
    try:
        return guarded(work, busy_message=GROUPS_UNAVAILABLE_MESSAGE)
    except MissingParent:
        missing = True
    except GroupNameTaken:
        answer = conflict(ErrorCode.GROUP_NAME_TAKEN, GROUP_NAME_TAKEN_MESSAGE)
    except GroupLimit:
        answer = conflict(ErrorCode.GROUP_CAP_REACHED, GROUP_CAP_MESSAGE)
    except NotAppliedYet:
        answer = unavailable(NOT_APPLIED_MESSAGE)
    except BackendUnavailable:
        answer = unavailable(GROUPS_UNAVAILABLE_MESSAGE)
    if missing:
        not_found()
    assert answer is not None
    raise answer


def _owned(db: TransactionalDatabase, stores: GroupStores, *, campaign_id: str, owner_id: int) -> None:
    """The pre-read of a route with no group in its path: the campaign, with
    its owner in the statement, in a transaction that takes no lock."""

    def work() -> bool:
        with db.transaction() as unit:
            return stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is not None

    if not guarded(work, busy_message=GROUPS_UNAVAILABLE_MESSAGE):
        not_found()


def _owned_group(
    db: TransactionalDatabase,
    stores: GroupStores,
    *,
    campaign_id: str,
    group_id: str,
    owner_id: int,
) -> tuple[StoredGroup, frozenset[str]]:
    """The pre-read of a group-scoped route, in one transaction that takes no
    lock: the campaign with its owner in the statement, then that campaign's
    group, removed or not, and its members when it is live. Missing, foreign
    and another GM's are the one 404."""

    def work() -> tuple[StoredGroup, frozenset[str]] | None:
        with db.transaction() as unit:
            if stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
                return None
            group = stores.eligibility.get_group(unit, campaign_id, group_id)
            if group is None:
                return None
            seats = stores.eligibility.members(unit, campaign_id, group_id) if group.is_live else frozenset()
            return group, seats

    found = guarded(work, busy_message=GROUPS_UNAVAILABLE_MESSAGE)
    if found is None:
        not_found()
    return found


def _answer(db: TransactionalDatabase, stores: GroupStores, group: StoredGroup) -> Group:
    """A written group on the wire, its members read in a transaction of their
    own after the write committed: at least as new as the write."""
    if not group.is_live:
        not_found()

    def work() -> frozenset[str]:
        with db.transaction() as unit:
            return stores.eligibility.members(unit, group.campaign_id, group.id)

    return group_to_wire(group, guarded(work, busy_message=GROUPS_UNAVAILABLE_MESSAGE))


def group_list(db: TransactionalDatabase, stores: GroupStores, *, campaign_id: str, owner_id: int) -> GroupPage:
    """The live groups, by the name's fold then id, each with its members, all
    read in the transaction that read ownership. One page (I-9); group by
    group, so a membership changed meanwhile may show torn (I-10)."""

    def work() -> list[Group] | None:
        with db.transaction() as unit:
            if stores.campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
                return None
            return [
                group_to_wire(group, stores.eligibility.members(unit, campaign_id, group.id))
                for group in stores.eligibility.list_groups(unit, campaign_id)
            ]

    items = guarded(work, busy_message=GROUPS_UNAVAILABLE_MESSAGE)
    if items is None:
        not_found()
    return GroupPage(schema_version=WIRE_VERSION, items=items, next_cursor=None)


def group_create(
    db: TransactionalDatabase,
    stores: GroupStores,
    *,
    campaign_id: str,
    owner_id: int,
    name: str,
    command_id: str,
    now: datetime,
) -> Group:
    """A new empty group, or the one `command_id` already made, whatever name
    the replay sends. A replay whose group was since removed is the one 404
    (I-8): `Group` has no removed state to show it with."""
    _owned(db, stores, campaign_id=campaign_id, owner_id=owner_id)
    mutations = mutations_for(db, stores, campaign_id=campaign_id, owner_id=owner_id, now=now)
    made = _refused(lambda: mutations.create_group(campaign_id, name=name, command_id=command_id, now=now))
    return _answer(db, stores, made)


def group_rename(
    db: TransactionalDatabase,
    stores: GroupStores,
    *,
    campaign_id: str,
    group_id: str,
    owner_id: int,
    name: str,
    now: datetime,
) -> Group:
    """Rename a live group, with no campaign lock and no advance. The same
    name again changes nothing, records nothing and answers the group."""
    group, _ = _owned_group(db, stores, campaign_id=campaign_id, group_id=group_id, owner_id=owner_id)
    if not group.is_live:
        not_found()
    mutations = mutations_for(db, stores, campaign_id=campaign_id, owner_id=owner_id, now=now)
    renamed = _refused(lambda: mutations.rename_group(campaign_id, group_id, name=name, now=now))
    return _answer(db, stores, renamed)


def group_remove(
    db: TransactionalDatabase,
    stores: GroupStores,
    *,
    campaign_id: str,
    group_id: str,
    owner_id: int,
    now: datetime,
) -> bool:
    """Remove a group for good: RQ-5's two steps. True when something changed.
    A group already removed changes nothing and runs no step 1 (I-11)."""
    group, _ = _owned_group(db, stores, campaign_id=campaign_id, group_id=group_id, owner_id=owner_id)
    if not group.is_live:
        return False
    mutations = mutations_for(db, stores, campaign_id=campaign_id, owner_id=owner_id, now=now)
    return _refused(lambda: mutations.remove_group(campaign_id, group_id, now=now)).changed


def member_add(
    db: TransactionalDatabase,
    stores: GroupStores,
    *,
    campaign_id: str,
    group_id: str,
    participant_id: str,
    owner_id: int,
    now: datetime,
) -> bool:
    """Add a seat that is not removed to a live group: a locked widening that
    displays nothing (ED-13). A missing, foreign or removed seat is the store
    statement's own `MissingParent`, the one 404."""
    group, _ = _owned_group(db, stores, campaign_id=campaign_id, group_id=group_id, owner_id=owner_id)
    if not group.is_live:
        not_found()
    mutations = mutations_for(db, stores, campaign_id=campaign_id, owner_id=owner_id, now=now)
    return _refused(lambda: mutations.add_member(campaign_id, group_id, participant_id, now=now)).changed


def member_remove(
    db: TransactionalDatabase,
    stores: GroupStores,
    *,
    campaign_id: str,
    group_id: str,
    participant_id: str,
    owner_id: int,
    now: datetime,
) -> bool:
    """Take a seat out of a live group: RQ-5's two steps. A seat that is not a
    member — missing, foreign and removed alike — changes nothing and runs no
    step 1 (the critic's item 3): a membership added after the pre-read is
    ordered after this removal."""
    group, members = _owned_group(db, stores, campaign_id=campaign_id, group_id=group_id, owner_id=owner_id)
    if not group.is_live:
        not_found()
    if participant_id not in members:
        return False
    mutations = mutations_for(db, stores, campaign_id=campaign_id, owner_id=owner_id, now=now)
    return _refused(lambda: mutations.remove_member(campaign_id, group_id, participant_id)).changed


# ── The router ───────────────────────────────────────────────────────────────


def build_router(
    gm: SessionDependency,
    database: Callable[[], TransactionalDatabase | None],
    stores: Callable[[], GroupStores],
) -> APIRouter:
    """The six routes on a `workbench_router`, given the app's GM gate, its
    database getter and its stores getter."""
    router = workbench_router(gm)

    def _database(db: TransactionalDatabase | None) -> TransactionalDatabase:
        if db is None:
            raise unavailable(GROUPS_UNAVAILABLE_MESSAGE)
        return db

    def _readable(*shapes: tuple[str, str]) -> None:
        """Every path id against its prefix's shape: outside it is the same 404
        as missing, before any transaction opens."""
        if not all(readable(prefix, value) for prefix, value in shapes):
            not_found()

    @router.get("/campaigns/{campaign_id}/groups", response_model=GroupPage)
    def read_groups(
        campaign_id: str,
        user: SessionData = Depends(gm),
        chosen: GroupStores = Depends(stores),
        db: TransactionalDatabase | None = Depends(database),
    ) -> GroupPage:
        live_db = _database(db)
        _readable((ident.CAMPAIGN, campaign_id))
        return group_list(live_db, chosen, campaign_id=campaign_id, owner_id=user.user_id)

    @router.post("/campaigns/{campaign_id}/groups", response_model=Group, status_code=201)
    def post_group(
        campaign_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        chosen: GroupStores = Depends(stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Group:
        request = parse_body(GroupCreateRequest, raw)
        name = checked_name(request.name)
        live_db = _database(db)
        _readable((ident.CAMPAIGN, campaign_id))
        return group_create(
            live_db, chosen, campaign_id=campaign_id, owner_id=user.user_id, name=name,
            command_id=request.command_id, now=now,
        )

    @router.patch("/campaigns/{campaign_id}/groups/{group_id}", response_model=Group)
    def patch_group(
        campaign_id: str,
        group_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        chosen: GroupStores = Depends(stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Group:
        request = parse_body(GroupPatchRequest, raw)
        name = checked_name(request.name)
        live_db = _database(db)
        _readable((ident.CAMPAIGN, campaign_id), (ident.GROUP, group_id))
        return group_rename(
            live_db, chosen, campaign_id=campaign_id, group_id=group_id, owner_id=user.user_id, name=name, now=now
        )

    @router.post("/campaigns/{campaign_id}/groups/{group_id}/remove", status_code=204)
    def post_group_remove(
        campaign_id: str,
        group_id: str,
        user: SessionData = Depends(gm),
        chosen: GroupStores = Depends(stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        live_db = _database(db)
        _readable((ident.CAMPAIGN, campaign_id), (ident.GROUP, group_id))
        group_remove(live_db, chosen, campaign_id=campaign_id, group_id=group_id, owner_id=user.user_id, now=now)
        return Response(status_code=204)

    @router.post("/campaigns/{campaign_id}/groups/{group_id}/members/{participant_id}", status_code=204)
    def post_member(
        campaign_id: str,
        group_id: str,
        participant_id: str,
        user: SessionData = Depends(gm),
        chosen: GroupStores = Depends(stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        live_db = _database(db)
        _readable((ident.CAMPAIGN, campaign_id), (ident.GROUP, group_id), (ident.PARTICIPANT, participant_id))
        member_add(
            live_db, chosen, campaign_id=campaign_id, group_id=group_id, participant_id=participant_id,
            owner_id=user.user_id, now=now,
        )
        return Response(status_code=204)

    @router.post("/campaigns/{campaign_id}/groups/{group_id}/members/{participant_id}/remove", status_code=204)
    def post_member_remove(
        campaign_id: str,
        group_id: str,
        participant_id: str,
        user: SessionData = Depends(gm),
        chosen: GroupStores = Depends(stores),
        db: TransactionalDatabase | None = Depends(database),
        now: datetime = Depends(get_clock),
    ) -> Response:
        live_db = _database(db)
        _readable((ident.CAMPAIGN, campaign_id), (ident.GROUP, group_id), (ident.PARTICIPANT, participant_id))
        member_remove(
            live_db, chosen, campaign_id=campaign_id, group_id=group_id, participant_id=participant_id,
            owner_id=user.user_id, now=now,
        )
        return Response(status_code=204)

    return router

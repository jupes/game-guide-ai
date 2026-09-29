"""The eligibility and group mutations, and the one revision they advance
(agent-forge-harness-1ir.2.1).

Every mutation implemented here changes who may ever be shown a field — a
classification, a group's membership, a group's existence — so every one that
changes something takes the campaign's lock **exclusively, first**, changes the
fact, and advances `authz_revision` **in the same transaction** through the one
helper, `UnitOfWork.advance_authz_revision` (RQ-2, RQ-4, RQ-10). This module is
the only caller of the eligibility store's writes (a tripwire test says so),
and the store refuses every write but a rename without that lock.

**Three shapes** (ADR section 4, and `docs/ARCHITECTURE.md`'s helper contract):

- **A locked widening or a classification** — `classify`, `add_member` — is one
  transaction. A classification that admits anyone queues the field for the
  projector (`project=`); one to `gm_only` or back to unclassified obeys rule 3.
  Every class change first removes the field's table-namespace rows through
  `TableNamespaceNarrowing` (inferred decision I-9), which is empty until
  `1ir.2.3` builds that namespace.
- **A fact-changing narrowing** — `remove_member`, `remove_group` — is RQ-5's two
  steps. Step 1 is its own transaction, never takes the campaign lock and is
  never refused on state: it narrows the campaign's live session, if there is
  one, so that no display outlives the change. Step 2 is in the request: the
  exclusive lock, the fact, the session narrowed again (one started between the
  steps), and the advance. If step 2 cannot have the lock it answers
  `NotAppliedYet` — honestly, and never replayed from a job.
- **Neither** — `create_group` (an empty group widens nothing) and
  `rename_group` (a name is not a fact a display reads) — advances nothing, and
  a rename takes no campaign lock at all.

**Nothing here displays, stops a display for a class change, audits, enqueues
a job or reaches a route** (I-10, I-17). The route bead composes this with
ownership (SEC-2), which is why every method takes an already-authorised
`campaign_id`, and records its audit row through `ChangeRecorder`, which is
called once, inside the mutating transaction, only when something changed.

**Retries** (I-21 as the critic amended it, RQ-3, RQ-8): a lock timeout is
never retried inside the request, because three waits of the derived two
seconds outlast the gate's five; a deadlock victim is retried up to
`DEADLOCK_ATTEMPTS` times, each in a fresh transaction. Only the SQLSTATE is
logged, and every refusal is raised outside the handler, so no driver text is
chained (SEC-20).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TypeVar

import psycopg

from .campaign_store import MissingParent
from .db import CampaignAuthzMissing, ProjectionItem, TransactionalDatabase, UnitOfWork
from .document_store import DocumentStore
from .eligibility_store import (
    ClassificationSource,
    Eligibility,
    EligibilityStore,
    FieldClass,
    Group,
    NotClassifiable,
    check_group_name,
    check_types,
    classifiable_keys,
    is_field_key,
)
from .table_session_store import TableSessionStore

log = logging.getLogger(__name__)

#: How many times a mutation is tried when it is chosen as a deadlock's victim
#: (RQ-3). The number is `service/table_sessions.py`'s.
DEADLOCK_ATTEMPTS = 3

_T = TypeVar("_T")

#: Required extension point (I-9): given the unit — which holds the exclusive
#: campaign lock — the campaign, the document and the key, remove that field's
#: table-namespace rows. Passed by name wherever the service is built, never
#: defaulted, like `slot_clear`.
TableNamespaceNarrowing = Callable[[UnitOfWork, str, str, str], None]


def no_table_namespace(unit: UnitOfWork, campaign_id: str, document_id: str, field_key: str) -> None:
    """The narrowing until `1ir.2.3` builds the table namespace: there are no
    rows to remove yet. Said here, once, rather than by a default."""


class ChangeOperation(str, Enum):
    """What changed, as a code (critic item 12). Never text."""

    FIELD_CLASSIFIED = "field.classified"
    GROUP_CREATED = "group.created"
    GROUP_RENAMED = "group.renamed"
    GROUP_REMOVED = "group.removed"
    MEMBER_ADDED = "group.member_added"
    MEMBER_REMOVED = "group.member_removed"


@dataclass(frozen=True)
class ChangeRecord:
    """One change, in ids and codes only — never a group name, a field value
    or a principal list (SEC-20, ED-26) — so the route bead can write its audit
    row from it in the same transaction as the revision (SEC-38)."""

    operation: ChangeOperation
    campaign_id: str
    authz_revision: int | None = None
    document_id: str | None = None
    field_key: str | None = None
    group_id: str | None = None
    participant_id: str | None = None
    old_class: FieldClass | None = None
    new_class: FieldClass | None = None


#: Required extension point (critic item 12): called once per change, on the
#: unit that made it, after the advance (after the write for create and
#: rename). A recorder that raises rolls the whole change back.
ChangeRecorder = Callable[[UnitOfWork, ChangeRecord], None]


def no_record(unit: UnitOfWork, record: ChangeRecord) -> None:
    """The recorder until the route bead writes audit rows: nothing is kept."""


class EligibilityRefusal(Exception):
    """What the mutations refuse, beside the store's own refusals. Every
    message is fixed and names nothing a caller supplied."""


class ClassMoved(EligibilityRefusal):
    """The field's stored class is not the one the caller displayed (ED-13(3)):
    someone changed it since. Nothing was written."""

    def __init__(self) -> None:
        super().__init__("that field's classification changed since it was shown")


class BackendUnavailable(EligibilityRefusal):
    """The database, not a decision: the campaign lock timed out or a deadlock
    was lost, and the server's own retries are spent. Retryable (RQ-4, RQ-8)."""

    def __init__(self) -> None:
        super().__init__("that change could not be made just now")


class NotAppliedYet(EligibilityRefusal):
    """Step 2 of a narrowing could not have the campaign lock in time. Step 1
    has already stopped the displays; the fact is unchanged. Retryable (RQ-5,
    RC-15)."""

    def __init__(self) -> None:
        super().__init__("that change was not applied yet; try again")


@dataclass(frozen=True)
class Change:
    """Whether the mutation changed anything, and the revision its advance
    returned — None when nothing changed and nothing advanced."""

    changed: bool
    authz_revision: int | None = None


@dataclass(frozen=True)
class EligibilityStores:
    """What the locked bodies compose: the stores, and the two required
    extension points, none of them defaulted."""

    eligibility: EligibilityStore
    documents: DocumentStore
    sessions: TableSessionStore
    table_namespace: TableNamespaceNarrowing
    record: ChangeRecorder


# ── The locked bodies ────────────────────────────────────────────────────────
#
# Public so that `1ir.11.1` can compose a classification into a Confirm's own
# transaction, and so the PostgreSQL tests can hold one open. Each takes the
# campaign lock itself as its unit's first lock; the store guard refuses a
# write without it.


def classify_locked(
    unit: UnitOfWork,
    stores: EligibilityStores,
    campaign_id: str,
    document_id: str,
    field_key: str,
    *,
    expected: Eligibility,
    new: Eligibility,
    now: datetime | None = None,
) -> Change:
    """A classification, compare-and-set, in SEC-3's order: 404, then 422, then
    409. `expected` is the `Eligibility` the caller read and displayed; the
    comparison is dataclass equality over class, ids and source."""
    unit.lock_campaign(campaign_id, shared=False)
    document = stores.documents.get(unit, campaign_id, document_id)
    if document is None:
        raise MissingParent("no such document in that campaign")
    if field_key not in classifiable_keys(document):
        raise NotClassifiable()
    if new.field_class is not FieldClass.UNCLASSIFIED and new.source is not ClassificationSource.GM:
        raise ValueError("a classification is the GM's")
    stores.eligibility.check_principals(unit, campaign_id, new)
    stored = stores.eligibility.eligibility_of(unit, document, field_key)
    if stored != expected:
        raise ClassMoved()
    if stored == new:
        return Change(False)
    stores.eligibility.put(unit, document, field_key, new, now=now)
    stores.table_namespace(unit, campaign_id, document_id, field_key)
    project = (ProjectionItem(document_id, field_key),) if new.admits_anyone else ()
    revision = unit.advance_authz_revision(campaign_id, project=project)
    stores.record(
        unit,
        ChangeRecord(
            ChangeOperation.FIELD_CLASSIFIED,
            campaign_id,
            revision,
            document_id=document_id,
            field_key=field_key,
            old_class=stored.field_class,
            new_class=new.field_class,
        ),
    )
    return Change(True, revision)


def add_member_locked(
    unit: UnitOfWork,
    stores: EligibilityStores,
    campaign_id: str,
    group_id: str,
    participant_id: str,
    *,
    now: datetime | None = None,
) -> Change:
    """A locked widening: displays nothing and narrows nothing (ED-13, O-3 —
    an added member sees nothing until the GM displays again)."""
    unit.lock_campaign(campaign_id, shared=False)
    if not stores.eligibility.add_member(unit, campaign_id, group_id, participant_id, now=now):
        return Change(False)
    revision = unit.advance_authz_revision(campaign_id)
    stores.record(
        unit,
        ChangeRecord(
            ChangeOperation.MEMBER_ADDED, campaign_id, revision, group_id=group_id, participant_id=participant_id
        ),
    )
    return Change(True, revision)


def narrow_step_one(unit: UnitOfWork, stores: EligibilityStores, campaign_id: str) -> None:
    """Step 1 of every narrowing here: never the campaign lock, never refused on
    state. Narrow the campaign's live session, if it has one."""
    live = stores.sessions.live_session_for_campaign(unit, campaign_id)
    if live is not None:
        stores.sessions.narrow(unit, campaign_id, live.id)


#: The names the brief gives step 1 of each narrowing: one body serves both.
remove_member_step_one = narrow_step_one
remove_group_step_one = narrow_step_one


def _narrow_again_and_advance(unit: UnitOfWork, stores: EligibilityStores, campaign_id: str) -> int:
    live = stores.sessions.live_session_for_campaign(unit, campaign_id)
    if live is not None:
        stores.sessions.narrow(unit, campaign_id, live.id)
    return unit.advance_authz_revision(campaign_id)


def remove_member_step_two(
    unit: UnitOfWork, stores: EligibilityStores, campaign_id: str, group_id: str, participant_id: str
) -> Change:
    """Step 2, in the request: lock, change the fact, narrow again, advance."""
    unit.lock_campaign(campaign_id, shared=False)
    if not stores.eligibility.remove_member(unit, campaign_id, group_id, participant_id):
        return Change(False)
    revision = _narrow_again_and_advance(unit, stores, campaign_id)
    stores.record(
        unit,
        ChangeRecord(
            ChangeOperation.MEMBER_REMOVED, campaign_id, revision, group_id=group_id, participant_id=participant_id
        ),
    )
    return Change(True, revision)


def remove_group_step_two(
    unit: UnitOfWork, stores: EligibilityStores, campaign_id: str, group_id: str, *, now: datetime | None = None
) -> Change:
    """Step 2 of removing a group: as `remove_member_step_two`."""
    unit.lock_campaign(campaign_id, shared=False)
    if not stores.eligibility.remove_group(unit, campaign_id, group_id, now=now):
        return Change(False)
    revision = _narrow_again_and_advance(unit, stores, campaign_id)
    stores.record(unit, ChangeRecord(ChangeOperation.GROUP_REMOVED, campaign_id, revision, group_id=group_id))
    return Change(True, revision)


def _create_group_locked(
    unit: UnitOfWork,
    stores: EligibilityStores,
    campaign_id: str,
    name: str,
    command_id: str | None,
    now: datetime | None,
) -> Group:
    unit.lock_campaign(campaign_id, shared=False)
    if command_id is not None:
        replayed = stores.eligibility.group_for_command(unit, campaign_id, command_id)
        if replayed is not None:
            return replayed
    group = stores.eligibility.create_group(unit, campaign_id, name=name, command_id=command_id, now=now)
    stores.record(unit, ChangeRecord(ChangeOperation.GROUP_CREATED, campaign_id, group_id=group.id))
    return group


def _rename_group_unlocked(
    unit: UnitOfWork, stores: EligibilityStores, campaign_id: str, group_id: str, name: str, now: datetime | None
) -> Group:
    held = stores.eligibility.hold_group(unit, campaign_id, group_id)
    if held is None or not held.is_live:
        raise MissingParent("no such group in that campaign")
    if held.name == check_group_name(name):
        return held
    renamed = stores.eligibility.rename_group(unit, campaign_id, group_id, name=name, now=now)
    stores.record(unit, ChangeRecord(ChangeOperation.GROUP_RENAMED, campaign_id, group_id=group_id))
    return renamed


# ── The service ──────────────────────────────────────────────────────────────


class EligibilityMutations:
    """The mutations, each in its own transaction(s) over one database."""

    def __init__(
        self,
        db: TransactionalDatabase,
        *,
        eligibility: EligibilityStore,
        documents: DocumentStore,
        sessions: TableSessionStore,
        table_namespace: TableNamespaceNarrowing,
        record: ChangeRecorder,
    ) -> None:
        self._db = db
        self._stores = EligibilityStores(eligibility, documents, sessions, table_namespace, record)

    def _attempt(
        self, operation: str, work: Callable[[UnitOfWork], _T], busy: type[EligibilityRefusal]
    ) -> _T:
        """`work` in a fresh transaction per attempt. A deadlock victim is
        retried up to `DEADLOCK_ATTEMPTS` times; a lock timeout is answered at
        once. Either, spent, is `busy`; a campaign with no authorisation row is
        `MissingParent`. Every refusal is raised after the handler has closed."""
        missing = False
        for attempt in range(1, DEADLOCK_ATTEMPTS + 1):
            try:
                with self._db.transaction() as unit:
                    return work(unit)
            except psycopg.errors.DeadlockDetected as exc:
                log.warning(
                    "eligibility: %s lost a deadlock (attempt %d of %d, %s)",
                    operation,
                    attempt,
                    DEADLOCK_ATTEMPTS,
                    exc.sqlstate,
                )
            except psycopg.errors.LockNotAvailable as exc:
                log.warning("eligibility: %s could not have the lock (%s)", operation, exc.sqlstate)
                break
            except CampaignAuthzMissing:
                missing = True
                break
        if missing:
            raise MissingParent("no such campaign")
        raise busy()

    def classify(
        self,
        campaign_id: str,
        document_id: str,
        field_key: str,
        *,
        expected: Eligibility,
        new: Eligibility,
        now: datetime | None = None,
    ) -> Change:
        """Classify one field, compare-and-set against the class the caller
        displayed (`ClassMoved` if it moved). Checked before any statement: the
        argument types, and the key's shape (`NotClassifiable`)."""
        check_types(
            campaign_id=campaign_id, document_id=document_id, field_key=field_key, eligibility=expected
        )
        check_types(eligibility=new)
        if not is_field_key(field_key):
            raise NotClassifiable()
        return self._attempt(
            "classify",
            lambda unit: classify_locked(
                unit, self._stores, campaign_id, document_id, field_key, expected=expected, new=new, now=now
            ),
            BackendUnavailable,
        )

    def create_group(
        self, campaign_id: str, *, name: str, command_id: str | None = None, now: datetime | None = None
    ) -> Group:
        """A new group, or the one this `command_id` already made. Advances
        nothing: an empty group widens nothing."""
        check_types(campaign_id=campaign_id, name=name, command_id=command_id)
        return self._attempt(
            "create_group",
            lambda unit: _create_group_locked(unit, self._stores, campaign_id, name, command_id, now),
            BackendUnavailable,
        )

    def rename_group(self, campaign_id: str, group_id: str, *, name: str, now: datetime | None = None) -> Group:
        """Rename a live group. No campaign lock, no revision (RQ-10 treats a
        participant's rename the same way): the group row is held instead."""
        check_types(campaign_id=campaign_id, group_id=group_id, name=name)
        check_group_name(name)
        return self._attempt(
            "rename_group",
            lambda unit: _rename_group_unlocked(unit, self._stores, campaign_id, group_id, name, now),
            BackendUnavailable,
        )

    def add_member(
        self, campaign_id: str, group_id: str, participant_id: str, *, now: datetime | None = None
    ) -> Change:
        """Add a seat to a group: a locked widening."""
        check_types(campaign_id=campaign_id, group_id=group_id, participant_id=participant_id)
        return self._attempt(
            "add_member",
            lambda unit: add_member_locked(unit, self._stores, campaign_id, group_id, participant_id, now=now),
            BackendUnavailable,
        )

    def _narrowing(self, operation: str, step_two: Callable[[UnitOfWork], Change], campaign_id: str) -> Change:
        """RQ-5's two steps. Step 2 runs only once step 1 has committed."""
        self._attempt(
            f"{operation} (step 1)",
            lambda unit: narrow_step_one(unit, self._stores, campaign_id),
            BackendUnavailable,
        )
        return self._attempt(f"{operation} (step 2)", step_two, NotAppliedYet)

    def remove_member(self, campaign_id: str, group_id: str, participant_id: str) -> Change:
        """Remove a seat from a group: a fact-changing narrowing."""
        check_types(campaign_id=campaign_id, group_id=group_id, participant_id=participant_id)
        return self._narrowing(
            "remove_member",
            lambda unit: remove_member_step_two(unit, self._stores, campaign_id, group_id, participant_id),
            campaign_id,
        )

    def remove_group(self, campaign_id: str, group_id: str, *, now: datetime | None = None) -> Change:
        """Mark a group removed: a fact-changing narrowing. A removed group is
        never restored; a restore would be a locked widening."""
        check_types(campaign_id=campaign_id, group_id=group_id)
        return self._narrowing(
            "remove_group",
            lambda unit: remove_group_step_two(unit, self._stores, campaign_id, group_id, now=now),
            campaign_id,
        )


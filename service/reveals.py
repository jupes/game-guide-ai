"""The reveal service: Confirm, Stop and the GM's picture, as transactions, and
the fills of the two slot extension points (agent-forge-harness-1kg.7.1).

It composes the stores — `reveal_store` (disclosures and slots),
`table_session_store` (the session row and its epochs), `campaign_store`,
`document_store` and `audit_log` — and holds no HTTP: `1kg.7.2` builds the
routes, the projection and the headers on top of it, and maps each refusal as
the brief's section 10.6 says (one 404 for every stranger; 409 for the table
having moved on; 422 naming a field; 503 retryable).

**A Confirm (`display`) is a locked widening, in this order** (ED-9, SEC-3,
RQ-4), one transaction per attempt:

1. ownership, unlocked — the session is that owner's in that campaign and the
   document is that campaign's, else `RevealNotFound`, one answer for both.
   **Nothing is locked before this**, so a stranger can never stall another
   GM's table behind a lock;
2. replay, unlocked — a command id this session has already used answers the
   current picture and writes nothing;
3. courtesy — a session that is not live, or an epoch the GM did not compose
   against, is `RevealConflict` before any lock is taken;
4. the campaign lock, **shared** — a widening waits for a fact change, never the
   reverse; a lock that cannot be had in time is `RevealBusy`;
5. validation, reads only — the document not archived, the pinned version
   sealed, every masked key revealable, present and non-empty in that version
   (`check_mask`), and the audience: the table, active participants of the
   campaign, or *Everyone seated* expanded here to the accepted and
   GM-confirmed seats (TA-5);
6. the session row, held with the owner in the locking statement;
7. replay again, under the row;
8. state again, under the row — live, the same epoch, and a campaign that is
   not archived (an archive that committed first wins);
9. the write, and the reveal epoch advanced **once**, whatever the number of
   slots;
10. the audit rows: one `reveal.displayed` or `reveal.updated`, and one
    `reveal.stopped` per disclosure a move or a replacement took copies from;
11. the picture, read in the same transaction.

It never advances `authz_revision`, never enqueues a job and never notifies.

**A Stop is a narrowing**: owner-scoped, **never the campaign lock and never a
`lock_timeout` of its own**, never refused for state (X-3). It holds the
campaign's live session row, clears the document's copies (`gm_stop`) or every
slot (`stop_all`), advances the reveal epoch **even when nothing was live**,
writes one `reveal.stopped` row per disclosure it took copies from (or one row
when it took none), and commits; only then does it read the picture, in a
transaction of its own, so nothing after the clear can undo it. It is `narrow`'s
three steps (hold, clear, advance), not a call of `narrow`, because it must
audit what it cleared; it leaves the state `narrow` with the same scope would.

**Why no deadlock cycle is possible.** Every writer of a session's reveal rows
holds that session's row first (I-6), so all of them serialise on it and the
order among slot and disclosure rows cannot cycle. Nothing that holds a slot or
a disclosure row then asks for `authz_state` or a participant row, and a Confirm
holds `authz_state` in share mode and never a participant or a document row.
Every transaction holds **at most one** session row — Stop, End, expiry, Rotate,
Remove, both archive steps and Start's finalisation alike, because a GM has at
most one row in state `live` — with two exceptions. A Confirm that finds the
document's live display in another session (ID-15) holds that second row only
when that session is no longer live; and the reconciliation narrows several
sessions, in ascending id, but holds the campaign lock **exclusively**, so no
Confirm of that campaign is inside it and no other writer of those rows can hold
the campaign lock at all.

A deadlock victim is retried in a fresh transaction, up to `attempts` in all,
then `RevealBusy`; a replayed Confirm makes that retry safe. The races are
proved against PostgreSQL in `tests/test_reveal_db.py`.

Nothing here logs, raises or records text: ids, version numbers, mask keys and
codes only (I-9). A log line names the operation and a count.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, TypeVar

import psycopg
from pydantic import ValidationError

from .audit_log import ActorKind, AuditAction, AuditLog, Decision, DetailValue, ObjectKind
from .campaign_store import CampaignStore, aware
from .db import CampaignAuthzMissing, TransactionalDatabase, UnitOfWork
from .document_store import DocumentStore
from .reconciliation import SlotReconcile
from .reveal_scope import (
    TABLE,
    Audience,
    DocumentCopies,
    EndReason,
    EveryoneSeated,
    EverySlot,
    ParticipantsAudience,
    ParticipantTargets,
    SlotScope,
    TableAudience,
    TableTarget,
    Targets,
)
from .reveal_store import (
    FIELD_KEY,
    AudienceRefused,
    CopyChange,
    Disclosure,
    DocumentNotDisplayable,
    MaskRefused,
    RevealBusy,
    RevealConflict,
    RevealNotFound,
    RevealPicture,
    RevealStore,
    VersionNotDisplayable,
)
from .table_session_store import SlotClear, TableSession, TableSessionStore, check_command_id
from .workbench_contracts import (
    _PROJECTION_VALUE,
    MASK_MAX_KEYS,
    PRESENCE_MAX_PARTICIPANTS,
    RESERVED_MASK_KEYS,
    DocumentTypeId,
    FieldKind,
    revealable_fields,
)

log = logging.getLogger(__name__)

#: How many times a Confirm or a Stop is tried when it loses a deadlock (ID-17).
DEADLOCK_ATTEMPTS: Final = 3

#: A wait that ran out: the campaign lock's `lock_timeout` or the transaction's
#: bound (RQ-4, RQ-8). Busy, never a refusal.
_BUSY: Final = (psycopg.errors.LockNotAvailable, psycopg.errors.TransactionTimeout)

_T = TypeVar("_T")


# ── The commands ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DisplayCommand:
    """One Confirm, as `1kg.7.2` builds it from the wire's `RevealRequest`. The
    command id is the client's, so it is hidden from `repr()`."""

    campaign_id: str
    session_id: str
    command_id: str = field(repr=False)
    reveal_epoch: int
    document_id: str
    version: int
    mask: tuple[str, ...]
    audience: Audience


@dataclass(frozen=True)
class Displayed:
    """What a Confirm answers: the GM's picture after it, and whether it was a
    replay of a command this session had already carried out."""

    picture: RevealPicture
    replayed: bool


@dataclass(frozen=True)
class StopDocument:
    """Stop every copy of one document."""

    document_id: str


@dataclass(frozen=True)
class StopAll:
    """Stop everything the session shows."""


# ── Pure checks ──────────────────────────────────────────────────────────────


def check_mask(doc_type: DocumentTypeId, data: Mapping[str, Any], mask: Sequence[str]) -> tuple[str, ...]:
    """The sorted keys at fault in `mask`, `()` when there are none. Pure.

    A key is at fault when it is not one of `revealable_fields(doc_type)` (which
    also rules out `all`, `tags` and every withheld key such as
    `npc.true_identity`), when it is named twice, or when the pinned version's
    value for it is absent or not **present and non-empty** by the contract's
    own per-kind rule (`workbench_contracts._PROJECTION_VALUE`): text non-blank
    after trimming, a list with at least one item and no blank one, an abilities
    block with at least one score, an entry with both a name and a text, an
    integer that is a number. An asset is present when its value is not null;
    the per-slot handle is `1kg.8.1.3`'s. **Never reads a registry default.**

    A key that is not field-key shaped is at fault too, and `MaskRefused` drops
    it from what it repeats, so a key that is really a sentence is never echoed.
    """
    revealable = revealable_fields(doc_type)
    at_fault: set[str] = set()
    seen: set[str] = set()
    for key in mask:
        if key in seen:
            at_fault.add(key)
            continue
        seen.add(key)
        kind = revealable.get(key)
        if kind is None or key in RESERVED_MASK_KEYS or FIELD_KEY.fullmatch(key) is None or key not in data:
            at_fault.add(key)
            continue
        if not _present(kind, data[key]):
            at_fault.add(key)
    return tuple(sorted(at_fault))


def _present(kind: FieldKind, value: Any) -> bool:
    if kind is FieldKind.ASSET:
        return value is not None
    shape = _PROJECTION_VALUE.get(kind)
    if shape is None:
        return False
    try:
        shape.validate_python(value)
    except ValidationError:
        return False
    return True


def _mask_shape(mask: object) -> tuple[str, ...]:
    """1 to `MASK_MAX_KEYS` strings, else the one mask refusal naming no key."""
    if (
        not isinstance(mask, (tuple, list))
        or not 1 <= len(mask) <= MASK_MAX_KEYS
        or not all(isinstance(key, str) for key in mask)
    ):
        raise MaskRefused()
    return tuple(mask)


# ── The fills ────────────────────────────────────────────────────────────────


def slot_clear_for(reveals: RevealStore) -> SlotClear:
    """`narrow`'s slot-clearing extension point, filled: clear what the scope
    names, in the narrowing's transaction and under its held session row, with
    its clock. What was cleared is recorded on the disclosures (`ended_reason`);
    a narrowing writes its own audit row, never a reveal row."""

    def clear(unit: UnitOfWork, session_id: str, scope: SlotScope, now: datetime) -> None:
        reveals.clear(unit, session_id, scope, now=now)

    return clear


def make_reconcile_slots(
    sessions: TableSessionStore, reveals: RevealStore, *, clock: Callable[[], datetime]
) -> SlotReconcile:
    """`campaign.reconcile`'s slot step, filled (ID-19). It runs holding the
    campaign lock exclusively, before the revision advances. In one read it
    finds every slot holding live content whose session is not live at the
    clock, or whose participant is removed; then, per session in ascending id,
    it narrows a dead session's every slot and a live session's removed seats,
    each as `reconciled`. A slot still valid is left alone."""

    def reconcile_slots(unit: UnitOfWork, campaign_id: str) -> None:
        moment = aware(clock(), "a clock")
        for stale in reveals.stale_slots(unit, campaign_id, now=moment):
            sessions.narrow(unit, campaign_id, stale.session_id, clears=stale.scope, now=moment)

    return reconcile_slots


# ── The service ──────────────────────────────────────────────────────────────


class Reveals:
    """Confirm, Stop and the GM's picture, over one database and the stores that
    share it. `attempts` is how many times a deadlock victim is tried; the race
    tests pass 1, so that a deadlock a retry would hide is seen."""

    def __init__(
        self,
        db: TransactionalDatabase,
        *,
        campaigns: CampaignStore,
        sessions: TableSessionStore,
        reveals: RevealStore,
        documents: DocumentStore,
        audit: AuditLog,
        attempts: int = DEADLOCK_ATTEMPTS,
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("a reveal is tried at least once")
        self._db = db
        self._campaigns = campaigns
        self._sessions = sessions
        self._reveals = reveals
        self._documents = documents
        self._audit = audit
        self._attempts = attempts

    # ── Confirm ──────────────────────────────────────────────────────────────

    def display(self, command: DisplayCommand, *, owner_id: int, now: datetime) -> Displayed:
        """A Confirm, in the module docstring's order. Refusals: `RevealNotFound`,
        `RevealConflict`, `MaskRefused`, `AudienceRefused`,
        `DocumentNotDisplayable`, `VersionNotDisplayable` and `RevealBusy`."""
        moment = aware(now, "a clock")
        check_command_id(command.command_id)
        return self._retrying("display", lambda unit: self._display(unit, command, owner_id, moment))

    def _display(self, unit: UnitOfWork, command: DisplayCommand, owner_id: int, moment: datetime) -> Displayed:
        campaign_id = command.campaign_id
        # 1. Ownership, unlocked: both reads, then one answer.
        session = self._sessions.find_owned(unit, campaign_id, command.session_id, owner_id=owner_id)
        document = self._documents.get(unit, campaign_id, command.document_id)
        if session is None or document is None:
            raise RevealNotFound()
        # 2. Replay, unlocked.
        if self._reveals.by_command(unit, session.id, command.command_id) is not None:
            return self._replay(unit, campaign_id, owner_id, moment)
        # 3. Courtesy: the table moved on, so the lock is not worth asking for.
        if not session.live_at(moment) or session.reveal_epoch != command.reveal_epoch:
            raise RevealConflict()
        # 4. The campaign lock, shared.
        try:
            unit.lock_campaign(campaign_id, shared=True)
        except CampaignAuthzMissing:
            raise RevealNotFound() from None
        # 5. Validation: reads only, no row lock.
        mask, targets = self._validated(unit, command)
        # 1kg.7.2 builds the projection here, from the sealed version and the
        # mask, still before the session row is held (shared ADR section 4).
        # 6. The session row, owner in the locking statement.
        held = self._sessions.hold(unit, campaign_id, session.id, owner_id=owner_id)
        if held is None:
            raise RevealNotFound()
        # 7. Replay again, under the row.
        if self._reveals.by_command(unit, held.id, command.command_id) is not None:
            return self._replay(unit, campaign_id, owner_id, moment)
        # 8. State again, under the row.
        if not held.live_at(moment) or held.reveal_epoch != command.reveal_epoch:
            raise RevealConflict()
        campaign = self._campaigns.get(unit, campaign_id, owner_id=owner_id)
        if campaign is None:
            raise RevealNotFound()
        if campaign.is_archived:
            raise RevealConflict()
        # 9. The write, and the epoch once.
        written = self._reveals.write(
            unit,
            campaign_id=campaign_id,
            session_id=held.id,
            document_id=command.document_id,
            version=command.version,
            mask=mask,
            targets=targets,
            command_id=command.command_id,
            now=moment,
        )
        after = self._sessions.advance_reveal_epoch(unit, held)
        # 10. The audit rows: what was taken first, then what was shown.
        for change in written.taken:
            self._record_stop(unit, after, owner_id, command.command_id, change, moment)
        shown = written.disclosure
        self._record(
            unit,
            after,
            owner_id,
            AuditAction.REVEAL_UPDATED if written.kind == "updated" else AuditAction.REVEAL_DISPLAYED,
            _detail(after, command.command_id, shown, _targets_of(targets)),
            None,
            moment,
        )
        # 11. The picture, in the same transaction.
        picture = self._reveals.picture(unit, campaign_id, owner_id=owner_id, now=moment)
        if picture is None:
            raise RevealConflict()
        return Displayed(picture, False)

    def _validated(self, unit: UnitOfWork, command: DisplayCommand) -> tuple[tuple[str, ...], Targets]:
        """Step 5. Each failure refuses the whole Confirm, and nothing is
        written."""
        document = self._documents.get(unit, command.campaign_id, command.document_id)
        if document is None:
            raise RevealNotFound()
        if document.is_archived:
            raise DocumentNotDisplayable()
        version = command.version
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise VersionNotDisplayable()
        snapshot = self._documents.snapshot(unit, command.campaign_id, command.document_id, version)
        if snapshot is None or not snapshot.version.is_sealed:
            raise VersionNotDisplayable()
        try:
            doc_type = DocumentTypeId(snapshot.type)
        except ValueError:
            raise DocumentNotDisplayable() from None
        mask = _mask_shape(command.mask)
        at_fault = check_mask(doc_type, snapshot.data, mask)
        if at_fault:
            raise MaskRefused(at_fault)
        return mask, self._targets(unit, command.campaign_id, command.audience)

    def _targets(self, unit: UnitOfWork, campaign_id: str, audience: Audience) -> Targets:
        """The audience as slots to fill. No type ties an audience (O-2, ED-14):
        what is checked is that every named participant is an active seat of
        this campaign (RD-4), one answer for removed, foreign and unknown."""
        if isinstance(audience, TableAudience):
            return TableTarget()
        if isinstance(audience, ParticipantsAudience):
            named = audience.participant_ids
            if (
                not isinstance(named, frozenset)
                or not 1 <= len(named) <= PRESENCE_MAX_PARTICIPANTS
                or not all(isinstance(pid, str) for pid in named)
            ):
                raise AudienceRefused()
            if self._reveals.active_participants(unit, campaign_id, named) != named:
                raise AudienceRefused()
            return ParticipantTargets(named)
        if isinstance(audience, EveryoneSeated):
            seated = self._reveals.confirmed_seats(unit, campaign_id)
            if not seated:
                raise AudienceRefused()
            return ParticipantTargets(seated)
        raise AudienceRefused()

    def _replay(self, unit: UnitOfWork, campaign_id: str, owner_id: int, moment: datetime) -> Displayed:
        """A command this session already carried out: the **current** picture,
        and nothing written. A session that has since stopped being live has no
        picture, so the replay is a conflict."""
        picture = self._reveals.picture(unit, campaign_id, owner_id=owner_id, now=moment)
        if picture is None:
            raise RevealConflict()
        return Displayed(picture, True)

    # ── Stop ─────────────────────────────────────────────────────────────────

    def stop(
        self,
        campaign_id: str,
        *,
        owner_id: int,
        scope: StopDocument | StopAll,
        command_id: str,
        now: datetime,
    ) -> RevealPicture | None:
        """A Stop, in the module docstring's order. None when the campaign has no
        live session: there is nothing to stop and no epoch to advance, and
        nothing is written. `RevealNotFound` for a campaign that is not the
        owner's or a document that is not the campaign's; an archived campaign
        is **not** refused. `RevealBusy` when the Stop committed and the picture
        could not be read after it — a retry is idempotent."""
        moment = aware(now, "a clock")
        check_command_id(command_id)
        if not isinstance(scope, (StopDocument, StopAll)):
            raise TypeError("a Stop names one document or every slot")
        stopped = self._retrying(
            "stop", lambda unit: self._stop(unit, campaign_id, owner_id, scope, command_id, moment)
        )
        if not stopped:
            return None
        try:
            with self._db.transaction() as unit:
                return self._reveals.picture(unit, campaign_id, owner_id=owner_id, now=moment)
        except (RevealBusy, psycopg.Error):
            log.warning("reveals: a stop committed and its picture could not be read")
            raise RevealBusy() from None

    def _stop(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        owner_id: int,
        scope: StopDocument | StopAll,
        command_id: str,
        moment: datetime,
    ) -> bool:
        if self._campaigns.get(unit, campaign_id, owner_id=owner_id) is None:
            raise RevealNotFound()
        if isinstance(scope, StopDocument) and self._documents.get(unit, campaign_id, scope.document_id) is None:
            raise RevealNotFound()
        live = self._sessions.live_session_for_campaign(unit, campaign_id)
        if live is None:
            return False
        held = self._sessions.hold(unit, campaign_id, live.id, owner_id=owner_id)
        if held is None:
            return False
        clears = (
            DocumentCopies(scope.document_id, EndReason.GM_STOP)
            if isinstance(scope, StopDocument)
            else EverySlot(EndReason.STOP_ALL)
        )
        changes = self._reveals.clear(unit, held.id, clears, now=moment)
        after = self._sessions.advance_reveal_epoch(unit, held)
        for change in changes:
            self._record_stop(unit, after, owner_id, command_id, change, moment)
        if not changes:
            named = scope.document_id if isinstance(scope, StopDocument) else None
            reason = EndReason.GM_STOP if isinstance(scope, StopDocument) else EndReason.STOP_ALL
            self._record(
                unit,
                after,
                owner_id,
                AuditAction.REVEAL_STOPPED,
                _nothing_stopped(after, command_id, named),
                reason.value,
                moment,
            )
        return True

    # ── The GM's picture ─────────────────────────────────────────────────────

    def picture(self, campaign_id: str, *, owner_id: int, now: datetime) -> RevealPicture | None:
        """The GM's picture of the campaign's live session, or None. One read,
        no lock."""
        moment = aware(now, "a clock")
        with self._db.transaction() as unit:
            return self._reveals.picture(unit, campaign_id, owner_id=owner_id, now=moment)

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def _retrying(self, operation: str, work: Callable[[UnitOfWork], _T]) -> _T:
        """`work` in a fresh transaction per attempt, tried again when it is a
        deadlock's victim; a wait that ran out is busy at once."""
        for attempt in range(1, self._attempts + 1):
            try:
                with self._db.transaction() as unit:
                    return work(unit)
            except psycopg.errors.DeadlockDetected:
                log.warning("reveals: %s lost a deadlock (attempt %d of %d)", operation, attempt, self._attempts)
            except _BUSY:
                log.warning("reveals: %s timed out waiting for a lock", operation)
                raise RevealBusy() from None
        raise RevealBusy()

    def _record(
        self,
        unit: UnitOfWork,
        after: TableSession,
        owner_id: int,
        action: AuditAction,
        detail: Mapping[str, DetailValue],
        reason_code: str | None,
        moment: datetime,
    ) -> None:
        self._audit.append(
            unit,
            campaign_id=after.campaign_id,
            actor_kind=ActorKind.GM,
            action=action,
            object_kind=ObjectKind.TABLE_SESSION,
            decision=Decision.ALLOWED,
            actor_ref=str(owner_id),
            object_ref=after.id,
            reason_code=reason_code,
            authz_revision=None,
            detail=detail,
            now=moment,
        )

    def _record_stop(
        self,
        unit: UnitOfWork,
        after: TableSession,
        owner_id: int,
        command_id: str,
        change: CopyChange,
        moment: datetime,
    ) -> None:
        self._record(
            unit,
            after,
            owner_id,
            AuditAction.REVEAL_STOPPED,
            _detail(after, command_id, change.disclosure, change.participant_ids),
            change.reason.value,
            moment,
        )


def _targets_of(targets: Targets) -> tuple[str, ...]:
    if isinstance(targets, ParticipantTargets):
        return tuple(sorted(targets.participant_ids))
    return ()


def _detail(
    after: TableSession, command_id: str, disclosure: Disclosure, participant_ids: Sequence[str]
) -> dict[str, DetailValue]:
    """ED-18(a)'s fields, exactly: no disclosure id, no recipient's name, no text."""
    return {
        "session_id": after.id,
        "command_id": command_id,
        "document_id": disclosure.document_id,
        "version": disclosure.version,
        "mask": list(disclosure.mask),
        "audience": disclosure.audience_kind,
        "participant_ids": None if disclosure.audience_kind == TABLE else sorted(participant_ids),
        "reveal_epoch": after.reveal_epoch,
    }


def _nothing_stopped(after: TableSession, command_id: str, document_id: str | None) -> dict[str, DetailValue]:
    """A Stop that took no copy: the document it named (None for a Stop-all),
    and the epoch it still advanced."""
    return {
        "session_id": after.id,
        "command_id": command_id,
        "document_id": document_id,
        "version": None,
        "mask": None,
        "audience": None,
        "participant_ids": None,
        "reveal_epoch": after.reveal_epoch,
    }

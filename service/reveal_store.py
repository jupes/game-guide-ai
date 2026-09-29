"""What the table may see: disclosures and slots (agent-forge-harness-1kg.7.1).

A **disclosure** is one Confirm's worth of display — a document, the version it
pins, the mask of field keys and the kind of audience — and a **slot row** is
one audience slot of one table session: the table slot, or one participant's
slot, holding a pointer to the disclosure it shows or to nothing
(`0018_reveal_disclosures.sql`; owner decision O-3; ED-15 as rewritten; A-16,
A-19, A-20). The two together are the explicit server state the table is built
from. Nothing about visibility is stored on a document or a version (ED-6): the
pin is reached slot -> disclosure -> (document, version).

**Invariants.** The auditor below checks I-1 to I-5 in either world; the schema,
the writers and the tests hold the rest.

* I-1 A slot shows at most one live content (one pointer column).
* I-2 A document has at most one live disclosure (a partial unique index; the
  twin refuses too).
* I-3 A disclosure is the table slot, or participant slots, never both.
* I-4 Every copy is in its disclosure's session and campaign (composite keys).
* I-5 No slot points at an ended disclosure, and every live disclosure has at
  least one copy.
* I-6 **Every write of a reveal row holds that session's row** in its
  transaction. Every writer here takes it itself first — `FOR NO KEY UPDATE`,
  re-entrant, free when the caller already holds it — and none ever calls
  `lock_campaign`. So every writer of one session's reveal rows is serialised on
  the session row, and the order among slot rows cannot deadlock.
* I-7 Every explicit lock is `FOR NO KEY UPDATE`, and no UPDATE changes a key
  column: the store updates only `reveal_slots (seq, disclosure_id, updated_at)`
  and `reveal_disclosures (ended_at, ended_reason)`.
* I-8 No column about visibility exists on `documents` or `document_versions`.
* I-9 No reveal row, record `repr()` or refusal carries text: ids, version
  numbers, mask keys and codes only. The client-minted command id is hidden
  from every `repr()`.
* I-10 A slot's `seq` rises by exactly 1 when, and only when, that slot's
  content changes. A Confirm **re-points** a target slot (+1), never clears it
  and then points it (+2), whatever it held before.
* I-11 Every read that decides what a principal sees — `picture`,
  `view_for_account`, `view_for_screen` — gates on `state = 'live' AND
  expires_at > :now`, with the application's clock; `state` alone is never the
  test. `by_command`, `live_for_document`, `live_disclosures` and `stale_slots`
  read dead sessions **by design** (replay, a stale display in a dead session,
  the reconciliation) and carry no such gate.
* I-12 An own slot is read only for an accepted, confirmed, not-removed seat, in
  the same query that finds the session (SEC-41, SEC-50(5), D-12).
* I-13 Ended disclosures are read by `by_command` only: never to seed a mask
  (REVEAL-4) and never as a ledger (ED-18; `1ir.2.8` never backfills from them).
* I-14 Nothing here reads a registry default mask (X-2, REVEAL-4).

**What a write does** (`write`), under the session row, in this order: end the
document's live disclosure — `updated` when the target slots are exactly its
slots, `moved` otherwise (every copy outside the new set is cleared), or
`reconciled` when it is in another session that is no longer live, whose row is
then held second; create the target slot rows that do not exist yet; take each
target slot's copy of **another** document, ending that disclosure (`replaced`)
only when it was its last copy; insert the new disclosure with the sorted mask;
and point every target slot at it, `seq + 1`. The old disclosure is ended before
the new one is inserted, so the one-live index never fires on a legal Confirm.
**It does not validate** revealability, presence, sealing or the session's
liveness: those are the service's, decided before the row is held (ED-9).

**Held copies are computed, never stored** (SEC-50(5), D-12, A-27). A copy in the
slot of a seat that is open, offered, or accepted and not yet confirmed is
written like any copy. It is never returned by `view_for_account`, because the
own slot is joined only for a confirmed seat in that same query; `picture` marks
it `held`. Confirming the seat delivers it from the next read, with no write
here.

The twin reads the shared rows of campaigns, participants, sessions, screen
grants, documents and versions (`campaign_store.shared_rows`) and keeps, in
Python, every relation the service relies on: one live disclosure per document,
the composite keys, a participant of the campaign, an existing version and the
uniqueness of `(session_id, command_id)`. It raises `MissingParent` where
PostgreSQL would raise a foreign-key violation. Races and `ON DELETE` behaviour
are proved against PostgreSQL only (`tests/test_reveal_db.py`).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol

from . import campaign_identity as ident
from .campaign_store import (
    Campaign,
    CampaignStoreError,
    MissingParent,
    Staging,
    aware,
    fake,
    pg,
    shared_rows,
)
from .db import InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork
from .participant_store import Participant
from .reveal_scope import (
    DOCUMENT_SCOPE_REASONS,
    PARTICIPANT,
    PARTICIPANT_SCOPE_REASONS,
    TABLE,
    DocumentCopies,
    EndReason,
    EverySlot,
    NoSlot,
    ParticipantSlots,
    ParticipantTargets,
    SlotScope,
    TableTarget,
    Targets,
)
from .table_session_store import _LIVE_GRANT, LIVE, ScreenGrant, TableSession, check_command_id
from .workbench_contracts import (
    MASK_MAX_KEYS,
    PRESENCE_MAX_PARTICIPANTS,
    RESERVED_MASK_KEYS,
    REVEAL_MAX_SLOTS,
)

#: ED-2's flat field key, as `0018`'s mask CHECK spells it.
FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

#: The reasons `EverySlot` may carry: the narrowings that clear everything.
EVERY_SLOT_REASONS = frozenset(
    {
        EndReason.STOP_ALL,
        EndReason.GM_END,
        EndReason.EXPIRED,
        EndReason.LINK_ROTATED,
        EndReason.CAMPAIGN_ARCHIVED,
        EndReason.RECONCILED,
        EndReason.NARROWED,
    }
)


# ── Refusals ─────────────────────────────────────────────────────────────────
#
# Each has a fixed message with no identifier and no text, and takes nothing
# except `MaskRefused`'s field keys. How `1kg.7.2` answers each one is the
# brief's section 10.6; no route is built here.


class RevealNotFound(CampaignStoreError):
    """No such session or document for that owner — one answer for every case
    (SEC-2, SEC-3)."""

    def __init__(self) -> None:
        super().__init__("no such table session or document")


class RevealConflict(CampaignStoreError):
    """The table moved on since the command was composed: a stale epoch, a
    session that is not live, a live display of the document in another live
    session, or a command id this session has already used."""

    def __init__(self) -> None:
        super().__init__("the table has changed since that command was composed")


class MaskRefused(CampaignStoreError):
    """A mask that is not 1 to 64 distinct revealable field keys. `keys` holds
    the sorted keys at fault that are field-key shaped — never anything else, so
    a key that is really a sentence is refused without being repeated."""

    def __init__(self, keys: Iterable[str] = ()) -> None:
        self.keys: tuple[str, ...] = tuple(
            sorted({key for key in keys if isinstance(key, str) and FIELD_KEY.fullmatch(key)})
        )
        super().__init__("a mask lists distinct revealable field keys")


class AudienceRefused(CampaignStoreError):
    """The table alone, or 1 to 100 active participants of the campaign — one
    answer for a removed, foreign or unknown participant and an empty audience."""

    def __init__(self) -> None:
        super().__init__("that audience cannot be shown this")


class DocumentNotDisplayable(CampaignStoreError):
    """The document is archived."""

    def __init__(self) -> None:
        super().__init__("that document cannot be displayed")


class VersionNotDisplayable(CampaignStoreError):
    """The version is missing or still open (not sealed)."""

    def __init__(self) -> None:
        super().__init__("that version cannot be displayed")


class RevealBusy(CampaignStoreError):
    """Retry later: a lock wait ran out, a deadlock persisted, or a picture would
    exceed `REVEAL_MAX_SLOTS` (fail closed rather than truncate)."""

    def __init__(self) -> None:
        super().__init__("the table is busy; try again")


# ── Records ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Disclosure:
    """One Confirm's worth of display. `mask` is sorted. The command id is the
    client's, so it is hidden from `repr()`."""

    id: str
    campaign_id: str
    session_id: str
    document_id: str
    version: int
    mask: tuple[str, ...]
    audience_kind: str
    created_at: datetime
    command_id: str = field(repr=False)
    ended_at: datetime | None = None
    ended_reason: EndReason | None = None

    @property
    def is_live(self) -> bool:
        return self.ended_at is None


@dataclass(frozen=True)
class SlotRow:
    """One audience slot of one session: the table slot (`participant_id` None)
    or one participant's. `seq` rises by one each time its content changes."""

    id: str
    campaign_id: str
    session_id: str
    participant_id: str | None
    seq: int
    disclosure_id: str | None
    updated_at: datetime

    @property
    def audience_kind(self) -> str:
        return TABLE if self.participant_id is None else PARTICIPANT


@dataclass(frozen=True)
class CopyChange:
    """Copies a write or a clear took from one disclosure: the disclosure as it
    was, the participants whose copies were taken (sorted; `()` for the table),
    whether that ended it, and why."""

    disclosure: Disclosure
    participant_ids: tuple[str, ...]
    ended: bool
    reason: EndReason


@dataclass(frozen=True)
class Written:
    """What a write did. `taken` holds only `moved` and `replaced` changes, in
    ascending disclosure id — the ones a GM's Confirm is audited for. An update's
    own previous disclosure is not in it, and a stale display ended in a dead
    session is in `reconciled`, which is a reconciliation, not a GM action."""

    disclosure: Disclosure
    kind: Literal["displayed", "updated"]
    taken: tuple[CopyChange, ...] = ()
    reconciled: tuple[CopyChange, ...] = ()


@dataclass(frozen=True)
class LiveCopy:
    """What a slot shows, for the GM. `held` is true for a participant slot whose
    seat is not accepted, confirmed and not removed: written, never delivered."""

    disclosure_id: str
    document_id: str
    document_type: str
    version: int
    mask: tuple[str, ...]
    held: bool = False


@dataclass(frozen=True)
class PictureEntry:
    audience_kind: str
    participant_id: str | None
    seq: int
    live: LiveCopy | None


@dataclass(frozen=True)
class RevealPicture:
    """The GM's picture of one live session: the table slot first, always
    (sequence 0 and empty when it has no row), then participant slots by
    ascending participant id — every slot holding live content whatever its
    participant's state, and the empty slots of participants not removed."""

    session_id: str
    campaign_id: str
    generation: int
    reveal_epoch: int
    entries: tuple[PictureEntry, ...]


@dataclass(frozen=True)
class SlotContent:
    """One slot as a table principal is entitled to it. Empty when nothing is
    shown. **`slot_id`, `document_id` and `version` never reach a table client**:
    `1kg.7.2`'s projection builder reads them and sends only the contract's
    `TableProjection` (`content_kind`, `type`, `fields`)."""

    slot_id: str | None
    seq: int
    document_id: str | None = None
    document_type: str | None = None
    version: int | None = None
    mask: tuple[str, ...] = ()


EMPTY_SLOT = SlotContent(None, 0)


@dataclass(frozen=True)
class TableView:
    """What one table principal may see of one live session: the table slot
    always, and its own slot iff `own_slot` (a confirmed seat).

    **Five fields never reach a table client**: `participant_id` here, and
    `slot_id`, `document_id` and `version` in each `SlotContent`, and the
    disclosure id, which this record does not carry at all. `1kg.7.2` strips
    them and builds the projection with its one builder."""

    session_id: str
    campaign_id: str
    generation: int
    is_owner: bool
    participant_id: str | None
    own_slot: bool
    table: SlotContent
    mine: SlotContent | None = None


@dataclass(frozen=True)
class StaleSlots:
    """A session whose slots the reconciliation must clear (reason
    `reconciled`): every slot of a session no longer live, or the slots of
    removed participants in a live one."""

    session_id: str
    scope: EverySlot | ParticipantSlots


# ── Pure checks ──────────────────────────────────────────────────────────────


def check_stored_mask(mask: Sequence[str]) -> tuple[str, ...]:
    """The mask as it is stored: 1 to `MASK_MAX_KEYS` distinct field keys, none
    of them `all`, sorted. The order a caller gives carries no meaning, so it is
    normalised, not refused. Whether each key is revealable and holds a value is
    the service's check, not this one."""
    if isinstance(mask, str) or not 1 <= len(mask) <= MASK_MAX_KEYS:
        raise MaskRefused()
    at_fault = {key for key in mask if not isinstance(key, str) or FIELD_KEY.fullmatch(key) is None}
    at_fault |= {key for key in mask if key in RESERVED_MASK_KEYS}
    seen: set[str] = set()
    for key in mask:
        if key in seen:
            at_fault.add(key)
        seen.add(key)
    if at_fault:
        raise MaskRefused(key for key in at_fault if isinstance(key, str))
    return tuple(sorted(mask))


def check_targets(targets: Targets) -> frozenset[str | None]:
    """The slots a write fills, as participant ids (`None` is the table slot):
    the table alone, or 1 to 100 well-formed participant ids — never both, never
    none."""
    if isinstance(targets, TableTarget):
        return frozenset({None})
    if (
        isinstance(targets, ParticipantTargets)
        and isinstance(targets.participant_ids, frozenset)
        and 1 <= len(targets.participant_ids) <= PRESENCE_MAX_PARTICIPANTS
        and all(
            isinstance(pid, str) and ident.is_id(ident.PARTICIPANT, pid) for pid in targets.participant_ids
        )
    ):
        return frozenset(targets.participant_ids)
    raise AudienceRefused()


def check_scope(scope: SlotScope) -> SlotScope:
    """A scope carries a reason its narrowing may use: a Remove cannot record
    `gm_stop`, nor a Stop `reconciled`."""
    if isinstance(scope, NoSlot):
        return scope
    if isinstance(scope, EverySlot) and scope.reason in EVERY_SLOT_REASONS:
        return scope
    if isinstance(scope, ParticipantSlots) and scope.reason in PARTICIPANT_SCOPE_REASONS:
        return scope
    if isinstance(scope, DocumentCopies) and scope.reason in DOCUMENT_SCOPE_REASONS:
        return scope
    raise ValueError("a slot scope carries a reason its narrowing may use")


def _pids(slots: Iterable[SlotRow]) -> tuple[str, ...]:
    return tuple(sorted(s.participant_id for s in slots if s.participant_id is not None))


def _slot_order(participant_id: str | None) -> tuple[bool, str]:
    """The table slot first, then participants by id (code-point order, which is
    PostgreSQL's `COLLATE "C"`)."""
    return (participant_id is not None, participant_id or "")


@dataclass(frozen=True)
class _Session:
    """The facts of a session row a write needs, read under its lock."""

    id: str
    campaign_id: str
    state: str
    expires_at: datetime

    def live_at(self, now: datetime) -> bool:
        return self.state == LIVE and self.expires_at > now


# ── The store ────────────────────────────────────────────────────────────────


class RevealStore(Protocol):
    """Disclosures and slots. Every writer holds its session row first (I-6) and
    never calls `lock_campaign`; every read is one statement."""

    def by_command(self, unit: UnitOfWork, session_id: str, command_id: str) -> Disclosure | None:
        """The replay lookup, and **the only read of ended rows** (I-13)."""
        ...  # pragma: no cover - structural type

    def live_for_document(self, unit: UnitOfWork, campaign_id: str, document_id: str) -> Disclosure | None:
        """The document's one live disclosure, in any session of the campaign."""
        ...  # pragma: no cover - structural type

    def live_disclosures(self, unit: UnitOfWork, session_id: str) -> list[Disclosure]:
        """That session's live disclosures, in ascending id."""
        ...  # pragma: no cover - structural type

    def active_participants(self, unit: UnitOfWork, campaign_id: str, ids: Collection[str]) -> frozenset[str]:
        """The subset of `ids` that are this campaign's participants and not
        removed — open, offered, accepted and confirmed seats alike (AUD-10,
        SEC-50(5)): a copy for a seat not yet confirmed is held, not refused."""
        ...  # pragma: no cover - structural type

    def confirmed_seats(self, unit: UnitOfWork, campaign_id: str) -> frozenset[str]:
        """Participants whose seats are accepted, GM-confirmed and not removed:
        what *Everyone seated* expands to (TA-5). `yje.2.1`'s Verified conjunct
        joins this query."""
        ...  # pragma: no cover - structural type

    def write(
        self,
        unit: UnitOfWork,
        *,
        campaign_id: str,
        session_id: str,
        document_id: str,
        version: int,
        mask: Sequence[str],
        targets: Targets,
        command_id: str,
        now: datetime,
    ) -> Written:
        """Display a document to the targets, in the module docstring's order,
        under the session row. `MissingParent` for a session, document version
        or participant that is not the campaign's; `AudienceRefused` for mixed
        or empty targets; `MaskRefused` for a mask that is not distinct field
        keys; `RevealConflict` for a used command id or a live display of the
        document in another live session."""
        ...  # pragma: no cover - structural type

    def clear(self, unit: UnitOfWork, session_id: str, scope: SlotScope, *, now: datetime) -> tuple[CopyChange, ...]:
        """Under the session row, point every slot the scope selects that holds
        content at nothing, `seq + 1`, and end with the scope's reason each
        disclosure left with no copy. `NoSlot` does nothing. Idempotent: a
        second identical clear returns `()` and changes nothing."""
        ...  # pragma: no cover - structural type

    def picture(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, now: datetime) -> RevealPicture | None:
        """The GM's picture, in one statement. None unless the campaign is the
        owner's and has a session live at `now`."""
        ...  # pragma: no cover - structural type

    def view_for_account(
        self, unit: UnitOfWork, campaign_id: str, *, user_id: int, now: datetime
    ) -> TableView | None:
        """SEC-41's query: the campaign's session live at `now`, for its owner
        (table slot only) or an accepted, not-removed seat (table slot, and its
        own slot only when confirmed). One `None` for every case that is not
        entitled (SEC-46). `yje.2.1`'s Verified conjuncts join the owner and
        the seat here."""
        ...  # pragma: no cover - structural type

    def view_for_screen(
        self, unit: UnitOfWork, campaign_id: str, grant_id: str, *, now: datetime
    ) -> TableView | None:
        """A live screen grant of this campaign's session, by
        `table_session_store._LIVE_GRANT`: the table slot only (SEC-48). None
        for a grant that is not live, or of another campaign."""
        ...  # pragma: no cover - structural type

    def stale_slots(self, unit: UnitOfWork, campaign_id: str, *, now: datetime) -> list[StaleSlots]:
        """Every session of the campaign with a slot the reconciliation must
        clear, in ascending session id: a dead session's live copies, and a
        live session's copies for removed seats."""
        ...  # pragma: no cover - structural type


class _Writes(ABC):
    """`write` and `clear`, once for both worlds, over each world's primitives —
    so the twin cannot decide differently from PostgreSQL what a Confirm or a
    narrowing does, only whether its rows are stored."""

    @abstractmethod
    def _hold(self, unit: UnitOfWork, session_id: str) -> _Session | None: ...

    @abstractmethod
    def _slots(self, unit: UnitOfWork, session_id: str) -> list[SlotRow]: ...

    @abstractmethod
    def _live(self, unit: UnitOfWork, ids: Collection[str]) -> dict[str, Disclosure]: ...

    @abstractmethod
    def _members(self, unit: UnitOfWork, campaign_id: str, ids: Collection[str]) -> frozenset[str]: ...

    @abstractmethod
    def _pinnable(self, unit: UnitOfWork, campaign_id: str, document_id: str, version: int) -> bool: ...

    @abstractmethod
    def _make_slots(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        participant_ids: Sequence[str | None],
        moment: datetime,
    ) -> None: ...

    @abstractmethod
    def _point(self, unit: UnitOfWork, slot_ids: Sequence[str], disclosure_id: str | None, moment: datetime) -> None:
        ...

    @abstractmethod
    def _end(self, unit: UnitOfWork, disclosure_ids: Sequence[str], reason: EndReason, moment: datetime) -> None: ...

    @abstractmethod
    def _insert(self, unit: UnitOfWork, disclosure: Disclosure) -> None: ...

    @abstractmethod
    def by_command(self, unit: UnitOfWork, session_id: str, command_id: str) -> Disclosure | None: ...

    @abstractmethod
    def live_for_document(self, unit: UnitOfWork, campaign_id: str, document_id: str) -> Disclosure | None: ...

    def _take_every_copy(self, unit: UnitOfWork, stale: Disclosure, reason: EndReason, moment: datetime) -> CopyChange:
        copies = [s for s in self._slots(unit, stale.session_id) if s.disclosure_id == stale.id]
        if copies:
            self._point(unit, sorted(s.id for s in copies), None, moment)
        self._end(unit, [stale.id], reason, moment)
        return CopyChange(stale, _pids(copies), True, reason)

    def write(
        self,
        unit: UnitOfWork,
        *,
        campaign_id: str,
        session_id: str,
        document_id: str,
        version: int,
        mask: Sequence[str],
        targets: Targets,
        command_id: str,
        now: datetime,
    ) -> Written:
        moment = aware(now, "a clock")
        stored = check_stored_mask(mask)
        wanted = check_targets(targets)
        check_command_id(command_id)
        held = self._hold(unit, session_id)
        if held is None or held.campaign_id != campaign_id:
            raise MissingParent("no such table session in that campaign")
        if self.by_command(unit, session_id, command_id) is not None:
            raise RevealConflict()
        if not self._pinnable(unit, campaign_id, document_id, version):
            raise MissingParent("no such document version in that campaign")
        named = frozenset(pid for pid in wanted if pid is not None)
        if named and self._members(unit, campaign_id, named) != named:
            raise MissingParent("no such participant in that campaign")

        kind: Literal["displayed", "updated"] = "displayed"
        taken: list[CopyChange] = []
        reconciled: tuple[CopyChange, ...] = ()
        previous = self.live_for_document(unit, campaign_id, document_id)
        if previous is not None and previous.session_id != session_id:
            # ID-15: a display left in another session. One live session per GM
            # makes that session dead; if it is not, this is a defect, and the
            # Confirm is refused rather than ending a live display.
            other = self._hold(unit, previous.session_id)
            if other is not None and other.live_at(moment):
                raise RevealConflict()
            reconciled = (self._take_every_copy(unit, previous, EndReason.RECONCILED, moment),)
            previous = None

        slots = {s.participant_id: s for s in self._slots(unit, session_id)}
        if previous is not None:
            copies = [s for s in slots.values() if s.disclosure_id == previous.id]
            outside = sorted(s.id for s in copies if s.participant_id not in wanted)
            if outside:
                self._point(unit, outside, None, moment)
            same = frozenset(s.participant_id for s in copies) == wanted
            self._end(unit, [previous.id], EndReason.UPDATED if same else EndReason.MOVED, moment)
            if same:
                kind = "updated"
            else:
                taken.append(CopyChange(previous, _pids(copies), True, EndReason.MOVED))

        missing = sorted((pid for pid in wanted if pid not in slots), key=_slot_order)
        if missing:
            self._make_slots(unit, campaign_id, session_id, missing, moment)
            slots = {s.participant_id: s for s in self._slots(unit, session_id)}
        targeted = [slots[pid] for pid in sorted(wanted, key=_slot_order)]
        before = None if previous is None else previous.id
        others = self._live(
            unit, {s.disclosure_id for s in targeted if s.disclosure_id is not None and s.disclosure_id != before}
        )
        for other_id in sorted(others):
            theirs = [s for s in targeted if s.disclosure_id == other_id]
            last = not any(s.disclosure_id == other_id and s.participant_id not in wanted for s in slots.values())
            if last:
                self._end(unit, [other_id], EndReason.REPLACED, moment)
            taken.append(CopyChange(others[other_id], _pids(theirs), last, EndReason.REPLACED))

        fresh = Disclosure(
            id=ident.new_id(ident.DISCLOSURE),
            campaign_id=campaign_id,
            session_id=session_id,
            document_id=document_id,
            version=version,
            mask=stored,
            audience_kind=TABLE if None in wanted else PARTICIPANT,
            created_at=moment,
            command_id=command_id,
        )
        self._insert(unit, fresh)
        self._point(unit, sorted(s.id for s in targeted), fresh.id, moment)
        return Written(fresh, kind, tuple(sorted(taken, key=lambda c: c.disclosure.id)), reconciled)

    def clear(self, unit: UnitOfWork, session_id: str, scope: SlotScope, *, now: datetime) -> tuple[CopyChange, ...]:
        check_scope(scope)
        if isinstance(scope, NoSlot):
            return ()
        moment = aware(now, "a clock")
        if self._hold(unit, session_id) is None:
            return ()
        slots = self._slots(unit, session_id)
        pointed = [s for s in slots if s.disclosure_id is not None]
        live = self._live(unit, {s.disclosure_id for s in pointed if s.disclosure_id is not None})
        if isinstance(scope, EverySlot):
            chosen = pointed
        elif isinstance(scope, ParticipantSlots):
            chosen = [s for s in pointed if s.participant_id in scope.participant_ids]
        else:
            chosen = [
                s
                for s in pointed
                if s.disclosure_id in live and live[s.disclosure_id].document_id == scope.document_id
            ]
        if not chosen:
            return ()
        self._point(unit, sorted(s.id for s in chosen), None, moment)
        chosen_ids = {s.id for s in chosen}
        changes: list[CopyChange] = []
        for disclosure_id in sorted({s.disclosure_id for s in chosen if s.disclosure_id is not None} & set(live)):
            theirs = [s for s in chosen if s.disclosure_id == disclosure_id]
            last = not any(s.disclosure_id == disclosure_id and s.id not in chosen_ids for s in slots)
            if last:
                self._end(unit, [disclosure_id], scope.reason, moment)
            changes.append(CopyChange(live[disclosure_id], _pids(theirs), last, scope.reason))
        return tuple(changes)


# ── PostgreSQL ───────────────────────────────────────────────────────────────

_D_COLUMNS = (
    "id, campaign_id, session_id, document_id, version_number, mask, audience_kind, "
    "created_at, command_id, ended_at, ended_reason"
)
_R_COLUMNS = "id, campaign_id, session_id, participant_id, seq, disclosure_id, updated_at"


def _disclosure(row: tuple[Any, ...]) -> Disclosure:
    return Disclosure(
        row[0], row[1], row[2], row[3], int(row[4]), tuple(row[5]), row[6], row[7], row[8], row[9],
        None if row[10] is None else EndReason(row[10]),
    )


def _slot(row: tuple[Any, ...]) -> SlotRow:
    return SlotRow(row[0], row[1], row[2], row[3], int(row[4]), row[5], row[6])


def _content(row: Sequence[Any]) -> SlotContent:
    """`(slot id, seq, document id, type, version, mask)` from a view's joins."""
    if row[0] is None:
        return EMPTY_SLOT
    if row[2] is None:
        return SlotContent(row[0], int(row[1]))
    return SlotContent(row[0], int(row[1]), row[2], row[3], int(row[4]), tuple(row[5]))


class PostgresRevealStore(_Writes):
    """`campaign.reveal_disclosures` and `campaign.reveal_slots` (0018)."""

    def _hold(self, unit: UnitOfWork, session_id: str) -> _Session | None:
        transaction = pg(unit)
        transaction.note_row_lock()
        row = transaction.conn.execute(
            "SELECT id, campaign_id, state, expires_at FROM campaign.table_sessions "
            "WHERE id = %s FOR NO KEY UPDATE",
            (session_id,),
        ).fetchone()
        return None if row is None else _Session(row[0], row[1], row[2], row[3])

    def _slots(self, unit: UnitOfWork, session_id: str) -> list[SlotRow]:
        rows = pg(unit).conn.execute(
            f'SELECT {_R_COLUMNS} FROM campaign.reveal_slots WHERE session_id = %s ORDER BY id COLLATE "C"',
            (session_id,),
        ).fetchall()
        return [_slot(row) for row in rows]

    def _live(self, unit: UnitOfWork, ids: Collection[str]) -> dict[str, Disclosure]:
        if not ids:
            return {}
        rows = pg(unit).conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.reveal_disclosures WHERE id = ANY(%s) AND ended_at IS NULL",
            (sorted(ids),),
        ).fetchall()
        return {row[0]: _disclosure(row) for row in rows}

    def _members(self, unit: UnitOfWork, campaign_id: str, ids: Collection[str]) -> frozenset[str]:
        rows = pg(unit).conn.execute(
            "SELECT id FROM campaign.participants WHERE campaign_id = %s AND id = ANY(%s)",
            (campaign_id, sorted(ids)),
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def _pinnable(self, unit: UnitOfWork, campaign_id: str, document_id: str, version: int) -> bool:
        row = pg(unit).conn.execute(
            "SELECT 1 FROM campaign.document_versions v JOIN campaign.documents d ON d.id = v.document_id "
            "WHERE d.id = %s AND d.campaign_id = %s AND v.number = %s",
            (document_id, campaign_id, version),
        ).fetchone()
        return row is not None

    def _make_slots(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        participant_ids: Sequence[str | None],
        moment: datetime,
    ) -> None:
        kinds = [TABLE if pid is None else PARTICIPANT for pid in participant_ids]
        ids = [ident.new_id(ident.REVEAL_SLOT) for _ in participant_ids]
        pg(unit).conn.execute(
            "INSERT INTO campaign.reveal_slots "
            "(id, campaign_id, session_id, audience_kind, participant_id, seq, disclosure_id, updated_at) "
            "SELECT u.id, %s, %s, u.kind, u.pid, 0, NULL, %s "
            "FROM unnest(%s::text[], %s::text[], %s::text[]) AS u(id, kind, pid) "
            "ON CONFLICT (session_id, participant_id) DO NOTHING",
            (campaign_id, session_id, moment, ids, kinds, list(participant_ids)),
        )

    def _point(self, unit: UnitOfWork, slot_ids: Sequence[str], disclosure_id: str | None, moment: datetime) -> None:
        pg(unit).conn.execute(
            "UPDATE campaign.reveal_slots SET disclosure_id = %s, seq = seq + 1, updated_at = %s "
            "WHERE id = ANY(%s)",
            (disclosure_id, moment, list(slot_ids)),
        )

    def _end(self, unit: UnitOfWork, disclosure_ids: Sequence[str], reason: EndReason, moment: datetime) -> None:
        pg(unit).conn.execute(
            "UPDATE campaign.reveal_disclosures SET ended_at = %s, ended_reason = %s "
            "WHERE id = ANY(%s) AND ended_at IS NULL",
            (moment, reason.value, list(disclosure_ids)),
        )

    def _insert(self, unit: UnitOfWork, disclosure: Disclosure) -> None:
        pg(unit).conn.execute(
            "INSERT INTO campaign.reveal_disclosures (id, campaign_id, session_id, document_id, "
            "version_number, mask, audience_kind, command_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                disclosure.id,
                disclosure.campaign_id,
                disclosure.session_id,
                disclosure.document_id,
                disclosure.version,
                list(disclosure.mask),
                disclosure.audience_kind,
                disclosure.command_id,
                disclosure.created_at,
            ),
        )

    def by_command(self, unit: UnitOfWork, session_id: str, command_id: str) -> Disclosure | None:
        row = pg(unit).conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.reveal_disclosures WHERE session_id = %s AND command_id = %s",
            (session_id, command_id),
        ).fetchone()
        return None if row is None else _disclosure(row)

    def live_for_document(self, unit: UnitOfWork, campaign_id: str, document_id: str) -> Disclosure | None:
        row = pg(unit).conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.reveal_disclosures "
            f"WHERE campaign_id = %s AND document_id = %s AND ended_at IS NULL",
            (campaign_id, document_id),
        ).fetchone()
        return None if row is None else _disclosure(row)

    def live_disclosures(self, unit: UnitOfWork, session_id: str) -> list[Disclosure]:
        rows = pg(unit).conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.reveal_disclosures "
            f'WHERE session_id = %s AND ended_at IS NULL ORDER BY id COLLATE "C"',
            (session_id,),
        ).fetchall()
        return [_disclosure(row) for row in rows]

    def active_participants(self, unit: UnitOfWork, campaign_id: str, ids: Collection[str]) -> frozenset[str]:
        rows = pg(unit).conn.execute(
            "SELECT id FROM campaign.participants WHERE campaign_id = %s AND id = ANY(%s) AND removed_at IS NULL",
            (campaign_id, sorted(ids)),
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def confirmed_seats(self, unit: UnitOfWork, campaign_id: str) -> frozenset[str]:
        rows = pg(unit).conn.execute(
            "SELECT id FROM campaign.participants WHERE campaign_id = %s AND removed_at IS NULL "
            "AND accepted_at IS NOT NULL AND confirmed_at IS NOT NULL",
            (campaign_id,),
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def picture(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, now: datetime) -> RevealPicture | None:
        rows = pg(unit).conn.execute(
            "SELECT s.id, s.campaign_id, s.link_generation, s.reveal_epoch, "
            "e.slot_id, e.participant_id, e.seq, e.disclosure_id, e.document_id, e.type, "
            "e.version_number, e.mask, e.held "
            "FROM campaign.table_sessions s JOIN campaign.campaigns c ON c.id = s.campaign_id "
            "LEFT JOIN LATERAL ("
            " SELECT r.id AS slot_id, r.participant_id, r.seq, d.id AS disclosure_id, d.document_id, doc.type,"
            " d.version_number, d.mask,"
            " (r.participant_id IS NOT NULL AND NOT (p.accepted_at IS NOT NULL"
            " AND p.confirmed_at IS NOT NULL AND p.removed_at IS NULL)) AS held"
            " FROM campaign.reveal_slots r"
            " LEFT JOIN campaign.reveal_disclosures d ON d.id = r.disclosure_id AND d.ended_at IS NULL"
            " LEFT JOIN campaign.documents doc ON doc.id = d.document_id"
            " LEFT JOIN campaign.participants p ON p.id = r.participant_id"
            " WHERE r.session_id = s.id"
            " AND (r.participant_id IS NULL OR d.id IS NOT NULL OR p.removed_at IS NULL)"
            ' ORDER BY r.participant_id COLLATE "C" NULLS FIRST LIMIT %s'
            ") e ON true "
            "WHERE s.campaign_id = %s AND c.owner_id = %s AND s.state = 'live' AND s.expires_at > %s",
            (REVEAL_MAX_SLOTS + 1, campaign_id, owner_id, aware(now, "a clock")),
        ).fetchall()
        if not rows:
            return None
        first = rows[0]
        entries: list[PictureEntry] = []
        for row in rows:
            if row[4] is None:
                continue
            live = (
                None
                if row[7] is None
                else LiveCopy(row[7], row[8], row[9], int(row[10]), tuple(row[11]), bool(row[12]))
            )
            entries.append(PictureEntry(TABLE if row[5] is None else PARTICIPANT, row[5], int(row[6]), live))
        return _picture(first[0], first[1], int(first[2]), int(first[3]), entries)

    def view_for_account(
        self, unit: UnitOfWork, campaign_id: str, *, user_id: int, now: datetime
    ) -> TableView | None:
        row = pg(unit).conn.execute(
            "SELECT s.id, s.campaign_id, s.link_generation, (c.owner_id = %(u)s) AS is_owner, p.id, "
            "(c.owner_id <> %(u)s AND p.confirmed_at IS NOT NULL) AS own_slot, "
            "t.id, t.seq, td.document_id, tdoc.type, td.version_number, td.mask, "
            "m.id, m.seq, md.document_id, mdoc.type, md.version_number, md.mask "
            "FROM campaign.table_sessions s "
            "JOIN campaign.campaigns c ON c.id = s.campaign_id "
            "LEFT JOIN campaign.participants p ON p.campaign_id = s.campaign_id AND p.user_id = %(u)s "
            "AND p.accepted_at IS NOT NULL AND p.removed_at IS NULL "
            "LEFT JOIN campaign.reveal_slots t ON t.session_id = s.id AND t.participant_id IS NULL "
            "LEFT JOIN campaign.reveal_disclosures td ON td.id = t.disclosure_id AND td.ended_at IS NULL "
            "LEFT JOIN campaign.documents tdoc ON tdoc.id = td.document_id "
            "LEFT JOIN campaign.reveal_slots m ON m.session_id = s.id AND m.participant_id = p.id "
            "AND p.confirmed_at IS NOT NULL AND c.owner_id <> %(u)s "
            "LEFT JOIN campaign.reveal_disclosures md ON md.id = m.disclosure_id AND md.ended_at IS NULL "
            "LEFT JOIN campaign.documents mdoc ON mdoc.id = md.document_id "
            "WHERE s.campaign_id = %(c)s AND s.state = 'live' AND s.expires_at > %(now)s "
            "AND (c.owner_id = %(u)s OR p.id IS NOT NULL)",
            {"u": user_id, "c": campaign_id, "now": aware(now, "a clock")},
        ).fetchone()
        if row is None:
            return None
        own = bool(row[5])
        return TableView(
            session_id=row[0],
            campaign_id=row[1],
            generation=int(row[2]),
            is_owner=bool(row[3]),
            participant_id=row[4],
            own_slot=own,
            table=_content(row[6:12]),
            mine=_content(row[12:18]) if own else None,
        )

    def view_for_screen(
        self, unit: UnitOfWork, campaign_id: str, grant_id: str, *, now: datetime
    ) -> TableView | None:
        row = pg(unit).conn.execute(
            f"SELECT s.id, s.campaign_id, s.link_generation, "
            f"t.id, t.seq, td.document_id, tdoc.type, td.version_number, td.mask "
            f"FROM campaign.table_credentials g JOIN campaign.table_sessions s ON s.id = g.session_id "
            f"LEFT JOIN campaign.reveal_slots t ON t.session_id = s.id AND t.participant_id IS NULL "
            f"LEFT JOIN campaign.reveal_disclosures td ON td.id = t.disclosure_id AND td.ended_at IS NULL "
            f"LEFT JOIN campaign.documents tdoc ON tdoc.id = td.document_id "
            f"WHERE g.id = %s AND s.campaign_id = %s AND {_LIVE_GRANT}",
            (grant_id, campaign_id, aware(now, "a clock")),
        ).fetchone()
        if row is None:
            return None
        return TableView(row[0], row[1], int(row[2]), False, None, False, _content(row[3:9]))

    def stale_slots(self, unit: UnitOfWork, campaign_id: str, *, now: datetime) -> list[StaleSlots]:
        moment = aware(now, "a clock")
        rows = pg(unit).conn.execute(
            "SELECT r.session_id, (s.state = 'live' AND s.expires_at > %s) AS live, r.participant_id "
            "FROM campaign.reveal_slots r "
            "JOIN campaign.reveal_disclosures d ON d.id = r.disclosure_id AND d.ended_at IS NULL "
            "JOIN campaign.table_sessions s ON s.id = r.session_id "
            "LEFT JOIN campaign.participants p ON p.id = r.participant_id "
            "WHERE r.campaign_id = %s "
            "AND (NOT (s.state = 'live' AND s.expires_at > %s) OR p.removed_at IS NOT NULL) "
            'ORDER BY r.session_id COLLATE "C", r.participant_id COLLATE "C"',
            (moment, campaign_id, moment),
        ).fetchall()
        return _stale([(row[0], bool(row[1]), row[2]) for row in rows])


def _picture(
    session_id: str, campaign_id: str, generation: int, epoch: int, entries: list[PictureEntry]
) -> RevealPicture:
    """The table entry first, always; raises rather than truncates past
    `REVEAL_MAX_SLOTS`."""
    ordered = sorted(entries, key=lambda e: _slot_order(e.participant_id))
    if not ordered or ordered[0].participant_id is not None:
        ordered.insert(0, PictureEntry(TABLE, None, 0, None))
    if len(ordered) > REVEAL_MAX_SLOTS:
        raise RevealBusy()
    return RevealPicture(session_id, campaign_id, generation, epoch, tuple(ordered))


def _stale(rows: Sequence[tuple[str, bool, str | None]]) -> list[StaleSlots]:
    """`(session id, session live, participant id)` rows, grouped per session in
    ascending id: every slot of a dead session, the removed seats of a live one."""
    found: list[StaleSlots] = []
    for session_id in sorted({row[0] for row in rows}):
        mine = [row for row in rows if row[0] == session_id]
        if not mine[0][1]:
            found.append(StaleSlots(session_id, EverySlot(EndReason.RECONCILED)))
        else:
            removed = frozenset(row[2] for row in mine if row[2] is not None)
            found.append(StaleSlots(session_id, ParticipantSlots(removed, EndReason.RECONCILED)))
    return found


# ── The twin ─────────────────────────────────────────────────────────────────


class InMemoryRevealStore(_Writes):
    """The twin, over the shared rows. It supports one open writer and models no
    lock wait: every race is proved against PostgreSQL."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._participants: Staging[Participant] = shared_rows(db, "participants")
        self._sessions: Staging[TableSession] = shared_rows(db, "table_sessions")
        self._grants: Staging[ScreenGrant] = shared_rows(db, "table_credentials")
        # justification: the document twin's row types are private to
        # `document_store`; this reads `campaign_id`, `type` and `gone` only.
        self._documents: Staging[Any] = shared_rows(db, "documents")
        # justification: as above; only a version's key is read.
        self._versions: Staging[Any] = shared_rows(db, "document_versions")
        self.disclosures: Staging[Disclosure] = shared_rows(db, "reveal_disclosures")
        self.slot_rows: Staging[SlotRow] = shared_rows(db, "reveal_slots")

    def _hold(self, unit: UnitOfWork, session_id: str) -> _Session | None:
        twin = fake(unit)
        twin.note_row_lock()
        found = self._sessions.visible(twin).get(session_id)
        return None if found is None else _Session(found.id, found.campaign_id, found.state, found.expires_at)

    def _slots(self, unit: UnitOfWork, session_id: str) -> list[SlotRow]:
        mine = [s for s in self.slot_rows.visible(fake(unit)).values() if s.session_id == session_id]
        return sorted(mine, key=lambda s: s.id)

    def _live(self, unit: UnitOfWork, ids: Collection[str]) -> dict[str, Disclosure]:
        visible = self.disclosures.visible(fake(unit))
        return {i: visible[i] for i in ids if i in visible and visible[i].is_live}

    def _members(self, unit: UnitOfWork, campaign_id: str, ids: Collection[str]) -> frozenset[str]:
        visible = self._participants.visible(fake(unit))
        return frozenset(i for i in ids if i in visible and visible[i].campaign_id == campaign_id)

    def _document(self, twin: InMemoryTransaction, campaign_id: str, document_id: str) -> Any:
        found = self._documents.visible(twin).get(document_id)
        if found is None or getattr(found, "gone", False) or found.campaign_id != campaign_id:
            return None
        return found

    def _pinnable(self, unit: UnitOfWork, campaign_id: str, document_id: str, version: int) -> bool:
        twin = fake(unit)
        if self._document(twin, campaign_id, document_id) is None:
            return False
        return f"{document_id}#{version}" in self._versions.visible(twin)

    def _make_slots(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        session_id: str,
        participant_ids: Sequence[str | None],
        moment: datetime,
    ) -> None:
        twin = fake(unit)
        session = self._sessions.visible(twin).get(session_id)
        if session is None or session.campaign_id != campaign_id:
            raise MissingParent("no such table session in that campaign")
        named = [pid for pid in participant_ids if pid is not None]
        if self._members(unit, campaign_id, named) != frozenset(named):
            raise MissingParent("no such participant in that campaign")
        existing = {s.participant_id for s in self._slots(unit, session_id)}
        for pid in participant_ids:
            if pid in existing:
                continue
            row = SlotRow(ident.new_id(ident.REVEAL_SLOT), campaign_id, session_id, pid, 0, None, moment)
            self.slot_rows.add(twin, row.id, row)

    def _point(self, unit: UnitOfWork, slot_ids: Sequence[str], disclosure_id: str | None, moment: datetime) -> None:
        twin = fake(unit)
        slots = self.slot_rows.visible(twin)
        shown = None if disclosure_id is None else self.disclosures.visible(twin).get(disclosure_id)
        for slot_id in slot_ids:
            slot = slots[slot_id]
            if disclosure_id is not None and (
                shown is None or shown.session_id != slot.session_id or shown.audience_kind != slot.audience_kind
            ):
                # The composite key (disclosure_id, session_id, audience_kind).
                raise MissingParent("no such disclosure for that slot")
            self.slot_rows.replace(
                twin, slot_id, replace(slot, disclosure_id=disclosure_id, seq=slot.seq + 1, updated_at=moment)
            )

    def _end(self, unit: UnitOfWork, disclosure_ids: Sequence[str], reason: EndReason, moment: datetime) -> None:
        twin = fake(unit)
        visible = self.disclosures.visible(twin)
        for disclosure_id in disclosure_ids:
            found = visible.get(disclosure_id)
            if found is not None and found.is_live:
                self.disclosures.replace(twin, disclosure_id, replace(found, ended_at=moment, ended_reason=reason))

    def _insert(self, unit: UnitOfWork, disclosure: Disclosure) -> None:
        twin = fake(unit)
        session = self._sessions.visible(twin).get(disclosure.session_id)
        if session is None or session.campaign_id != disclosure.campaign_id:
            raise MissingParent("no such table session in that campaign")
        if not self._pinnable(unit, disclosure.campaign_id, disclosure.document_id, disclosure.version):
            raise MissingParent("no such document version in that campaign")
        for other in self.disclosures.visible(twin).values():
            # The one-live index, and the command index.
            if (other.document_id == disclosure.document_id and other.is_live) or (
                other.session_id == disclosure.session_id and other.command_id == disclosure.command_id
            ):
                raise RevealConflict()
        self.disclosures.add(twin, disclosure.id, disclosure)

    def by_command(self, unit: UnitOfWork, session_id: str, command_id: str) -> Disclosure | None:
        for found in self.disclosures.visible(fake(unit)).values():
            if found.session_id == session_id and found.command_id == command_id:
                return found
        return None

    def live_for_document(self, unit: UnitOfWork, campaign_id: str, document_id: str) -> Disclosure | None:
        for found in self.disclosures.visible(fake(unit)).values():
            if found.campaign_id == campaign_id and found.document_id == document_id and found.is_live:
                return found
        return None

    def live_disclosures(self, unit: UnitOfWork, session_id: str) -> list[Disclosure]:
        live = [d for d in self.disclosures.visible(fake(unit)).values() if d.session_id == session_id and d.is_live]
        return sorted(live, key=lambda d: d.id)

    def active_participants(self, unit: UnitOfWork, campaign_id: str, ids: Collection[str]) -> frozenset[str]:
        visible = self._participants.visible(fake(unit))
        return frozenset(
            i for i in ids if i in visible and visible[i].campaign_id == campaign_id and visible[i].is_active
        )

    def confirmed_seats(self, unit: UnitOfWork, campaign_id: str) -> frozenset[str]:
        return frozenset(
            p.id
            for p in self._participants.visible(fake(unit)).values()
            if p.campaign_id == campaign_id and p.is_confirmed
        )

    def _live_session(self, twin: InMemoryTransaction, campaign_id: str, moment: datetime) -> TableSession | None:
        for session in self._sessions.visible(twin).values():
            if session.campaign_id == campaign_id and session.live_at(moment):
                return session
        return None

    def _shown(self, twin: InMemoryTransaction, slot: SlotRow | None) -> SlotContent:
        if slot is None:
            return EMPTY_SLOT
        shown = None if slot.disclosure_id is None else self.disclosures.visible(twin).get(slot.disclosure_id)
        if shown is None or not shown.is_live:
            return SlotContent(slot.id, slot.seq)
        doc = self._documents.visible(twin).get(shown.document_id)
        return SlotContent(
            slot.id, slot.seq, shown.document_id, None if doc is None else doc.type, shown.version, shown.mask
        )

    def picture(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int, now: datetime) -> RevealPicture | None:
        twin = fake(unit)
        moment = aware(now, "a clock")
        campaign = self._campaigns.visible(twin).get(campaign_id)
        if campaign is None or campaign.owner_id != owner_id:
            return None
        session = self._live_session(twin, campaign_id, moment)
        if session is None:
            return None
        participants = self._participants.visible(twin)
        entries: list[PictureEntry] = []
        for slot in self._slots(unit, session.id):
            content = self._shown(twin, slot)
            seat = None if slot.participant_id is None else participants.get(slot.participant_id)
            if content.document_id is None and seat is not None and not seat.is_active:
                # The empty slot of a removed seat is omitted, as PostgreSQL's
                # lateral filter omits it.
                continue
            live = None
            if content.document_id is not None and slot.disclosure_id is not None:
                held = slot.participant_id is not None and (seat is None or not seat.is_confirmed)
                live = LiveCopy(
                    slot.disclosure_id,
                    content.document_id,
                    content.document_type or "",
                    content.version or 0,
                    content.mask,
                    held,
                )
            entries.append(PictureEntry(slot.audience_kind, slot.participant_id, slot.seq, live))
        return _picture(session.id, campaign_id, session.link_generation, session.reveal_epoch, entries)

    def view_for_account(
        self, unit: UnitOfWork, campaign_id: str, *, user_id: int, now: datetime
    ) -> TableView | None:
        twin = fake(unit)
        moment = aware(now, "a clock")
        campaign = self._campaigns.visible(twin).get(campaign_id)
        session = self._live_session(twin, campaign_id, moment)
        if campaign is None or session is None:
            return None
        is_owner = campaign.owner_id == user_id
        seats = [
            p
            for p in self._participants.visible(twin).values()
            if p.campaign_id == campaign_id and p.user_id == user_id and p.is_accepted
        ]
        seat = seats[0] if seats else None
        if not is_owner and seat is None:
            return None
        own = not is_owner and seat is not None and seat.confirmed_at is not None
        slots = {s.participant_id: s for s in self._slots(unit, session.id)}
        return TableView(
            session_id=session.id,
            campaign_id=campaign_id,
            generation=session.link_generation,
            is_owner=is_owner,
            participant_id=None if seat is None else seat.id,
            own_slot=own,
            table=self._shown(twin, slots.get(None)),
            mine=self._shown(twin, slots.get(seat.id)) if own and seat is not None else None,
        )

    def view_for_screen(
        self, unit: UnitOfWork, campaign_id: str, grant_id: str, *, now: datetime
    ) -> TableView | None:
        twin = fake(unit)
        moment = aware(now, "a clock")
        grant = self._grants.visible(twin).get(grant_id)
        session = None if grant is None else self._sessions.visible(twin).get(grant.session_id)
        if (
            grant is None
            or session is None
            or grant.revoked_at is not None
            or session.campaign_id != campaign_id
            or not session.live_at(moment)
            or grant.link_generation != session.link_generation
        ):
            return None
        table = next((s for s in self._slots(unit, session.id) if s.participant_id is None), None)
        return TableView(
            session.id, campaign_id, session.link_generation, False, None, False, self._shown(twin, table)
        )

    def stale_slots(self, unit: UnitOfWork, campaign_id: str, *, now: datetime) -> list[StaleSlots]:
        twin = fake(unit)
        moment = aware(now, "a clock")
        sessions = self._sessions.visible(twin)
        participants = self._participants.visible(twin)
        live = self.disclosures.visible(twin)
        rows: list[tuple[str, bool, str | None]] = []
        for slot in self.slot_rows.visible(twin).values():
            if slot.campaign_id != campaign_id or slot.disclosure_id is None:
                continue
            if slot.disclosure_id not in live or not live[slot.disclosure_id].is_live:
                continue
            session = sessions[slot.session_id]
            seat = None if slot.participant_id is None else participants.get(slot.participant_id)
            alive = session.live_at(moment)
            if not alive or (seat is not None and not seat.is_active):
                rows.append((slot.session_id, alive, slot.participant_id))
        return _stale(rows)


# ── The invariant auditor (tests only) ───────────────────────────────────────


def _breaches(disclosures: Sequence[Disclosure], slots: Sequence[SlotRow]) -> list[str]:
    """I-1 to I-5 over every row. The messages name the invariant and a count,
    never an identifier."""
    found: list[str] = []
    keys = [(s.session_id, s.participant_id) for s in slots]
    if len(keys) != len(set(keys)):
        found.append("I-1: a session has two rows for one slot")
    live = {d.id: d for d in disclosures if d.is_live}
    by_document = [d.document_id for d in live.values()]
    if len(by_document) != len(set(by_document)):
        found.append("I-2: a document has more than one live disclosure")
    every = {d.id: d for d in disclosures}
    shown = [(s, every.get(s.disclosure_id)) for s in slots if s.disclosure_id is not None]
    if any(d is not None and d.audience_kind != s.audience_kind for s, d in shown):
        found.append("I-3: a slot shows a disclosure of the other audience kind")
    if any(d is None or d.session_id != s.session_id or d.campaign_id != s.campaign_id for s, d in shown):
        found.append("I-4: a copy is outside its disclosure's session or campaign")
    if any(d is not None and not d.is_live for s, d in shown):
        found.append("I-5: a slot points at an ended disclosure")
    copied = {s.disclosure_id for s, _ in shown}
    if any(d_id not in copied for d_id in live):
        found.append("I-5: a live disclosure has no copy")
    return found


def audit_reveal_invariants(unit: UnitOfWork, twin: InMemoryRevealStore | None = None) -> list[str]:
    """Which of I-1 to I-5 the stored rows break — `[]` when none. **Used by
    tests only**, after every test of the shared suite: it reads every row,
    ended ones included, which no production path may do (I-13). The twin's
    rows live on its store, so the twin world passes the store."""
    if isinstance(unit, PgTransaction):
        conn = unit.conn
        disclosures = [_disclosure(r) for r in conn.execute(f"SELECT {_D_COLUMNS} FROM campaign.reveal_disclosures")]
        slots = [_slot(r) for r in conn.execute(f"SELECT {_R_COLUMNS} FROM campaign.reveal_slots")]
        return _breaches(disclosures, slots)
    if twin is None:
        raise TypeError("the twin's auditor reads the twin store's rows")
    view = fake(unit)
    return _breaches(list(twin.disclosures.visible(view).values()), list(twin.slot_rows.visible(view).values()))

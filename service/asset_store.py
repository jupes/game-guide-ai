"""GM-side media assets: the rows, their states, the quota, and deletion
(agent-forge-harness-1kg.8.1.1, slice a of the media bead).

An asset row (migration `*_media_assets.sql`) names its campaign, holds what was
declared and what the server measured, and holds two object KEYS — never bytes.
This module is the only writer of `campaign.assets` and `campaign.media_usage`
for a GM: a `Protocol`, a PostgreSQL store and an in-memory twin, the shape
`service/campaign_store.py` established, and one behavioural suite runs over both
(`tests/test_asset_db.py`). **It imports no object store and no job handler**,
so no transaction here can hold a connection while bytes move (requirement
3.8): the handlers in `service/asset_jobs.py` move bytes with no connection open.

**The five states** (media ADR MS-3). `uploading` -> `processing` -> `ready` or
`failed`; `processing` may go back to `uploading` (MS-6's return after a queue
timeout); and any of the four -> `deleted`, a storage-only tombstone the wire
contract never carries. The machine is the store's and it is total: a mutator
holds its row first, then decides on the row it holds. A row that is missing,
another owner's, another campaign's or a tombstone is `MissingParent`, with one
fixed message; `IllegalTransition` is raised only for the caller's own live
asset in a state the change is not legal from — ownership has already passed by
then, which is SEC-3's order. Every entry into `uploading` or `processing`
enqueues that state's deadline sweep (`asset.sweep_stuck`, no dedupe key,
RQ-12).

**The quota** (SEC-31, MS-4). `media_usage` holds, per campaign, the sum of the
live rows' reservations — the declared size while uploading or processing, the
real size once ready, nothing once failed or deleted — and how many rows are
uploading, processing or ready. Every write to it is one conditional UPDATE or
an exact subtraction, never a read-then-write. The quota's correctness depends
on READ COMMITTED, which `Database.transaction()` sets: the loser of two racing
reservations re-evaluates the WHERE against the winner's new row version and
changes nothing, rather than raising a serialisation error. The quota numbers
are *suggested* (MS-4; the owner's O-F).

**The lock order**, which every path here keeps and `1kg.2.6` must fit into:

0. `create` only: the campaign row, `FOR KEY SHARE`, in the statement that
   checks ownership — so a create that loses to the campaign's deletion waits,
   then finds nothing, and answers `MissingParent` rather than raising a
   foreign-key violation;
1. the asset row or rows — explicitly `FOR NO KEY UPDATE` when a method holds
   its row before deciding, or implicitly by its UPDATE or DELETE;
2. then `media_usage`, by its UPDATE;
3. then the outbox enqueue, always last (RQ-3). An enqueue with a dedupe key
   waits for another transaction's uncommitted job of the same kind and key, so
   an enqueue taken before a row lock can wait on a transaction that waits for
   that row: a deadlock.

Before its first lock a path calls `note_row_lock()` and bounds the transaction
(RQ-8), `participant_store.hold`'s pattern, so a later `lock_campaign` in the
same unit is refused (RQ-2). Nothing here takes `campaign.authz_state` or
advances `authz_revision`: an asset has no eligibility of its own (ED-19). No
statement here takes `FOR UPDATE`; `1kg.2.6` alone may rule on the one it needs.

**Ownership is in the query, through two named fragments** (L-6). Every GM
statement names the campaign and, through `GM_CAMPAIGNS`, its owner — the
fragment where `yje.2.1` adds the identity ADR's section 8.1 conjunct (the owner
is Verified). The campaign primitive alone uses `OWNER_CAMPAIGNS`, which never
gains that conjunct: once `yje.2.1` lands a Deleted GM's campaigns are
unavailable, and deletion scoped by the GM fragment would match nothing while
the foreign key refused `zkc`'s erasure for ever. `service/asset_jobs.py` holds
the system statements, which name an asset by its globally unique id; no GM
method can reach them. **The table side never uses this store** (SEC-44(2)): a
table read finds its asset in its own `table_principal` query (SEC-16, SEC-41).

**Deletion.** A GM's delete writes the tombstone — clearing the alt text and
every measured value by the database's own rule — releases the reservation and
enqueues `asset.delete` carrying `{asset_id, object_key, tmp_key}` with the
asset id as its dedupe key. It returns the job id; the route (slice c) passes it
to `job_driver.run_after_response`, because an answer never waits on bytes. The
handler deletes the objects, then purges the row. No payload carries a campaign
id, so no outbox row maps an owner to a key (MS-2).

**The campaign primitive** (SEC-36, for `1kg.2.6`; its shape is provisional and
`1kg.2.6` may re-cut it). One owner-scoped `DELETE ... RETURNING` removes every
row of the campaign; the usage is reduced by exactly what it removed, never
zeroed; then one `asset.delete` per removed row that was not already a
tombstone is enqueued, last. It runs in the caller's transaction, before the
campaign row goes: the campaign's foreign key is `ON DELETE NO ACTION`, so no
other path can delete a campaign that still has an asset row. Creates must be
stopped before it runs — that is `1kg.2.6`'s, which may lock the campaign row
`FOR UPDATE` after `lock_campaign(exclusive)` to wait for in-flight creates and
turn new ones into `MissingParent`.

**Private text.** `alt` is private (SEC-20): hidden from `repr()`, never in a
log line, an exception, a payload, an audit row or a key. The store writes no
audit row — slice c's delete route records `asset.deleted` — and logs nothing.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from . import campaign_identity as ident
from .campaign_store import Campaign, CampaignStoreError, MissingParent, Staging, fake, now_or, pg, shared_rows
from .db import InMemoryDatabase, InMemoryTransaction, PgTransaction, UnitOfWork
from .jobs import JobQueue
from .workbench_contracts import (
    ALT_MAX_CHARS,
    AMBIENCE_MAX_MS,
    ASSET_MAX_BYTES,
    IMAGE_MAX_PIXELS,
    IMAGE_MAX_SIDE,
    MEDIA_TYPES,
    AltText,
    Asset,
    AssetFailure,
    AssetKind,
    AssetState,
    CommandId,
)

# ── Numbers and names ────────────────────────────────────────────────────────

#: Per campaign, *suggested* (MS-4, SEC-31; the owner's O-F): media is Paid (D-3)
#: and whether the quota depends on the tier is an open question. Constants, so
#: an answer changes no schema.
QUOTA_BYTES = 500_000_000
QUOTA_ASSETS = 500
#: How long a row may sit in a state before its sweep fails it `timed_out`
#: (*suggested*, MS-3).
UPLOADING_BOUND = timedelta(hours=1)
PROCESSING_BOUND = timedelta(minutes=10)

#: The storage-only tombstone. Never a member of the wire's `AssetState`.
DELETED = "deleted"
STORAGE_STATES = (*(state.value for state in AssetState), DELETED)
#: What processed audio always is (the wire contract's `MEDIA_TYPES` note, MS-6).
PROCESSED_AUDIO_TYPE = "audio/mpeg"

#: The two job kinds this store enqueues. The third, the orphan reconciliation,
#: is enqueued by `service/asset_jobs.enqueue_reconcile` alone.
DELETE_JOB = "asset.delete"
SWEEP_JOB = "asset.sweep_stuck"

#: 16 CSPRNG bytes per key (MS-2, SEC-28).
KEY_BYTES = 16
#: The key shapes `service/media_objects.py` checks before any I/O and the
#: migration's CHECKs spell; `service/tests/test_media_objects.py` holds the
#: three spellings to one another. Spelled here too because this module
#: imports no object store (requirement 3.8).
OBJECT_KEY_PATTERN = r"^assets/[0-9a-f]{32}$"
TMP_KEY_PATTERN = r"^tmp/[0-9a-f]{32}$"
_OBJECT_KEY = re.compile(OBJECT_KEY_PATTERN)
_TMP_KEY = re.compile(TMP_KEY_PATTERN)

_ALT = TypeAdapter(AltText)
_COMMAND = TypeAdapter(CommandId)

#: The one message for anything that is not the caller's (SEC-3).
MISSING = "no such asset or campaign for that owner"


def new_object_key() -> str:
    """`assets/<32 hex>`: 16 bytes from the operating system's CSPRNG and
    nothing else — no asset, campaign, name or time reaches it (MS-2)."""
    return "assets/" + secrets.token_hex(KEY_BYTES)


def new_tmp_key() -> str:
    """`tmp/<32 hex>`: drawn separately, so it says nothing about the object key."""
    return "tmp/" + secrets.token_hex(KEY_BYTES)


# ── The two ownership fragments (L-6) ────────────────────────────────────────

#: THE GM FRAGMENT. Every GM statement names its campaign AND this set: the
#: campaigns the caller owns. `yje.2.1` adds the identity ADR's section 8.1
#: conjunct HERE — a campaign is available to its owner only while the owner is
#: Verified — so a GM whose campaign is unavailable neither reads nor changes its
#: assets. `service/tests/test_asset_db.py` checks every statement by the NAME it
#: interpolates, because the two fragments are the same text until then.
GM_CAMPAIGNS = "SELECT id FROM campaign.campaigns WHERE owner_id = %(owner_id)s"

#: THE OWNER-ONLY FRAGMENT, used by the campaign primitive and by nothing else.
#: It must NEVER gain an availability conjunct: once `yje.2.1` makes a Deleted or
#: Suspended owner's campaigns unavailable, a primitive scoped by the GM fragment
#: would match nothing, and the foreign key's NO ACTION would refuse `zkc`'s
#: erasure of a Deleted GM for ever. `zkc` knows the account id, so owner-only
#: scoping serves erasure too.
OWNER_CAMPAIGNS = "SELECT id FROM campaign.campaigns WHERE owner_id = %(owner_id)s"


# ── Refusals ─────────────────────────────────────────────────────────────────


class QuotaExceeded(CampaignStoreError):
    """The campaign's media quota cannot take this asset (SEC-31). A route maps
    it to `ErrorCode.CAP_REACHED` — never to `AssetFailure.QUOTA_EXCEEDED`, which
    is a row's reason, not a wire code. Nothing was written."""

    MESSAGE = "the campaign's media quota cannot take that asset"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class IllegalTransition(CampaignStoreError):
    """The caller's own live asset is in a state the change is not legal from.
    Only ever raised after ownership has passed (SEC-3's order)."""

    MESSAGE = "that asset cannot make that change from the state it is in"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class AssetRowRefused(ValueError):
    """The twin's CHECK: a row the migration's constraints would refuse. It
    names the rule, never a value."""


def _missing() -> MissingParent:
    return MissingParent(MISSING)


# ── The row, and what a GM is given ──────────────────────────────────────────


@dataclass(frozen=True)
class AssetRow:
    """One row of `campaign.assets`, as both worlds hold it — tombstones
    included, which is why `state` is the storage vocabulary."""

    id: str
    campaign_id: str
    kind: AssetKind
    state: str
    declared_media_type: str
    declared_size_bytes: int
    media_type: str | None
    size_bytes: int | None
    width: int | None
    height: int | None
    duration_ms: int | None
    alt: str | None = field(repr=False)
    failure: AssetFailure | None
    object_key: str = field(repr=False)
    tmp_key: str = field(repr=False)
    created_command_id: str | None
    created_at: datetime
    updated_at: datetime
    state_changed_at: datetime

    @property
    def reservation(self) -> int:
        """What this row holds of its campaign's quota (L-4)."""
        if self.state in (AssetState.UPLOADING.value, AssetState.PROCESSING.value):
            return self.declared_size_bytes
        if self.state == AssetState.READY.value:
            return self.size_bytes or 0
        return 0

    @property
    def counted(self) -> bool:
        return self.state in (AssetState.UPLOADING.value, AssetState.PROCESSING.value, AssetState.READY.value)


@dataclass(frozen=True)
class AssetRecord:
    """A live asset, as a GM may see it. `alt` is private text and the keys are
    a map to the bytes, so all three are hidden from `repr()`: a traceback is a
    log line (SEC-20, MS-2)."""

    id: str
    campaign_id: str
    kind: AssetKind
    state: AssetState
    declared_media_type: str
    declared_size_bytes: int
    media_type: str | None
    size_bytes: int | None
    width: int | None
    height: int | None
    duration_ms: int | None
    alt: str | None = field(repr=False)
    failure: AssetFailure | None
    object_key: str = field(repr=False)
    tmp_key: str = field(repr=False)
    created_command_id: str | None
    created_at: datetime
    updated_at: datetime
    state_changed_at: datetime

    def to_wire(self) -> Asset:
        """The wire `Asset`: the declared type and size until `ready`, then the
        recorded ones (SEC-19). The schema's CHECKs make every live row convert;
        a row that did not would be a bug, and pydantic's message would quote
        the alt text, so the refusal is this module's own sentence."""
        fields = {
            "schema_version": 1,
            "asset_id": self.id,
            "campaign_id": self.campaign_id,
            "kind": self.kind,
            "state": self.state,
            "media_type": self.media_type if self.media_type is not None else self.declared_media_type,
            "size_bytes": self.size_bytes if self.size_bytes is not None else self.declared_size_bytes,
            "width": self.width,
            "height": self.height,
            "duration_ms": self.duration_ms,
            "alt": self.alt,
            "failure": self.failure,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        wire: Asset | None
        try:
            wire = Asset.model_validate(fields)
        except ValidationError:
            wire = None
        if wire is None:
            raise ValueError("an asset row did not convert to the wire Asset")
        return wire


@dataclass(frozen=True)
class ReadyObject:
    """What serving a `ready` asset needs, and nothing more (SEC-27, MS-3)."""

    object_key: str = field(repr=False)
    media_type: str
    size_bytes: int


@dataclass(frozen=True)
class MediaUsage:
    bytes_reserved: int
    asset_count: int


def record_of(row: AssetRow) -> AssetRecord:
    """A live row as a GM sees it. A tombstone has no record."""
    if row.state == DELETED:
        raise ValueError("a tombstone is never a record")
    return AssetRecord(
        id=row.id,
        campaign_id=row.campaign_id,
        kind=row.kind,
        state=AssetState(row.state),
        declared_media_type=row.declared_media_type,
        declared_size_bytes=row.declared_size_bytes,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        width=row.width,
        height=row.height,
        duration_ms=row.duration_ms,
        alt=row.alt,
        failure=row.failure,
        object_key=row.object_key,
        tmp_key=row.tmp_key,
        created_command_id=row.created_command_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        state_changed_at=row.state_changed_at,
    )


def delete_payload(row: AssetRow) -> dict[str, str | int | bool | None]:
    """`asset.delete`'s payload: the asset and its two keys, and no campaign id —
    so no outbox row holds a campaign next to a key (MS-2)."""
    return {"asset_id": row.id, "object_key": row.object_key, "tmp_key": row.tmp_key}


def usage_of(rows: Iterable[AssetRow], campaign_id: str) -> MediaUsage:
    """L-4's invariant, computed from the rows."""
    mine = [row for row in rows if row.campaign_id == campaign_id]
    return MediaUsage(sum(row.reservation for row in mine), sum(1 for row in mine if row.counted))


# ── What the migration's CHECKs refuse, for the twin ─────────────────────────


def check_row(row: AssetRow) -> AssetRow:
    """Every invariant the migration's CHECKs hold, held here for the twin
    (L-16), so that the twin refuses what PostgreSQL refuses."""
    problem = _row_problem(row)
    if problem is not None:
        raise AssetRowRefused(f"an asset row breaks the schema's rule on {problem}")
    return row


def _row_problem(row: AssetRow) -> str | None:
    image = row.kind is AssetKind.IMAGE
    ready = row.state == AssetState.READY.value
    checks: tuple[tuple[str, bool], ...] = (
        ("id", isinstance(row.id, str) and ident.is_id(ident.ASSET, row.id)),
        ("campaign_id", isinstance(row.campaign_id, str) and ident.is_id(ident.CAMPAIGN, row.campaign_id)),
        ("kind", isinstance(row.kind, AssetKind)),
        ("state", row.state in STORAGE_STATES),
        ("failure", (row.failure is not None) == (row.state == AssetState.FAILED.value)),
        ("declared_media_type", row.declared_media_type in MEDIA_TYPES.get(row.kind, ())),
        ("declared_size_bytes", 1 <= row.declared_size_bytes <= ASSET_MAX_BYTES.get(row.kind, 0)),
        ("media_type", (row.media_type is not None) == ready),
        ("size_bytes", (row.size_bytes is not None) == ready),
        ("width", (row.width is not None) == (image and ready)),
        ("height", (row.height is not None) == (image and ready)),
        ("duration_ms", (row.duration_ms is not None) == ((not image) and ready)),
        ("size_bytes", row.size_bytes is None or 1 <= row.size_bytes <= ASSET_MAX_BYTES.get(row.kind, 0)),
        ("width", row.width is None or 1 <= row.width <= IMAGE_MAX_SIDE),
        ("height", row.height is None or 1 <= row.height <= IMAGE_MAX_SIDE),
        ("width", row.width is None or row.height is None or row.width * row.height <= IMAGE_MAX_PIXELS),
        ("duration_ms", row.duration_ms is None or 1 <= row.duration_ms <= AMBIENCE_MAX_MS),
        (
            "media_type",
            not ready
            or (image and row.media_type == row.declared_media_type)
            or ((not image) and row.media_type == PROCESSED_AUDIO_TYPE),
        ),
        ("alt", (row.alt is None) if row.state == DELETED else ((row.alt is not None) == image)),
        ("alt", row.alt is None or 1 <= len(row.alt) <= ALT_MAX_CHARS),
        ("object_key", _OBJECT_KEY.fullmatch(row.object_key) is not None),
        ("tmp_key", _TMP_KEY.fullmatch(row.tmp_key) is not None),
        ("created_command_id", row.created_command_id is None or _valid(_COMMAND, row.created_command_id)),
    )
    for name, holds in checks:
        if not holds:
            return name
    return None


# justification: the adapter is one of two differently annotated types (AltText,
# CommandId), and this only asks whether a value validates, never for the result.
def _valid(adapter: TypeAdapter[Any], value: object) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


# ── Arguments, checked before any statement ──────────────────────────────────


def _check_types(**given: object) -> None:
    """Refuse a value of the wrong TYPE, in both worlds, before any statement
    (`participant_store.check_argument_types`' convention, z9v): the twin must
    not accept what PostgreSQL would refuse by aborting the caller's whole
    transaction. Names the parameter and the type; never the value."""
    for name, value in given.items():
        if name in ("owner_id", "size_bytes", "width", "height", "duration_ms"):
            optional = name in ("width", "height", "duration_ms")
            if optional and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"an asset store's {name} is an int")
        elif name in ("alt", "command_id"):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"an asset store's {name} is a str or None")
        elif not isinstance(value, str):
            raise TypeError(f"an asset store's {name} is a str")


def _kind(kind: AssetKind | str) -> AssetKind:
    checked: AssetKind | None
    try:
        checked = AssetKind(kind)
    except ValueError:
        checked = None
    if checked is None:
        raise ValueError("an asset's kind is not one the contract knows")
    return checked


def _failure(failure: AssetFailure | str) -> AssetFailure:
    checked: AssetFailure | None
    try:
        checked = AssetFailure(failure)
    except ValueError:
        checked = None
    if checked is None:
        raise ValueError("an asset's failure is not one the contract knows")
    return checked


@dataclass(frozen=True)
class _Declared:
    kind: AssetKind
    media_type: str
    size_bytes: int
    alt: str | None = field(repr=False)
    command_id: str | None


def _declared(
    kind: AssetKind | str, media_type: str, size_bytes: int, alt: str | None, command_id: str | None
) -> _Declared:
    """What a create declares, checked against the contract's own rules — body
    validation that depends on no resource, so it may run first (SEC-3). A
    refusal names the field, never the value: pydantic's message quotes its
    input, so it is never what reaches the caller."""
    checked_kind = _kind(kind)
    if media_type not in MEDIA_TYPES[checked_kind]:
        raise ValueError("an asset's declared media type is not one its kind accepts")
    if not 1 <= size_bytes <= ASSET_MAX_BYTES[checked_kind]:
        raise ValueError("an asset's declared size is outside what its kind allows")
    if alt is not None and not _valid(_ALT, alt):
        raise ValueError(f"an asset's alt text is 1 to {ALT_MAX_CHARS} characters of one line of plain text")
    if (checked_kind is AssetKind.IMAGE) != (alt is not None):
        raise ValueError("an image has alt text and an audio asset has none")
    if command_id is not None and not _valid(_COMMAND, command_id):
        raise ValueError("an asset's command id is not a command id")
    return _Declared(checked_kind, media_type, size_bytes, alt, command_id)


@dataclass(frozen=True)
class Measured:
    """What processing measured: the recorded type and size, and dimensions for
    an image or a duration for audio (the wire `Asset` rule)."""

    media_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


def _check_measured(row: AssetRow, measured: Measured) -> Measured:
    """Checked against the row's kind, so only after the row is held."""
    if not isinstance(measured, Measured):
        raise TypeError("an asset store's measured is a Measured")
    _check_types(
        media_type=measured.media_type,
        size_bytes=measured.size_bytes,
        width=measured.width,
        height=measured.height,
        duration_ms=measured.duration_ms,
    )
    image = row.kind is AssetKind.IMAGE
    expected_type = row.declared_media_type if image else PROCESSED_AUDIO_TYPE
    if measured.media_type != expected_type:
        raise ValueError("a processed asset's media type is not the one its kind records")
    if not 1 <= measured.size_bytes <= ASSET_MAX_BYTES[row.kind]:
        raise ValueError("a processed asset's size is outside what its kind allows")
    if image:
        if measured.width is None or measured.height is None or measured.duration_ms is not None:
            raise ValueError("a processed image has its dimensions and no duration")
        if not (1 <= measured.width <= IMAGE_MAX_SIDE and 1 <= measured.height <= IMAGE_MAX_SIDE):
            raise ValueError("a processed image's side is outside what the contract allows")
        if measured.width * measured.height > IMAGE_MAX_PIXELS:
            raise ValueError("a processed image has more pixels than the contract allows")
    else:
        if measured.duration_ms is None or measured.width is not None or measured.height is not None:
            raise ValueError("processed audio has its duration and no dimensions")
        if not 1 <= measured.duration_ms <= AMBIENCE_MAX_MS:
            raise ValueError("processed audio is longer than the contract allows")
    return measured


# ── The protocol ─────────────────────────────────────────────────────────────


class AssetStore(Protocol):
    """Every mutator takes the unit of work first, so `1kg.2.6`, `1kg.5.6` and
    `1kg.8.2` can compose it into one transaction."""

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        kind: AssetKind | str,
        media_type: str,
        size_bytes: int,
        alt: str | None = None,
        command_id: str | None = None,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        """An `uploading` row with two freshly minted keys, its reservation, and
        its uploading-deadline sweep. A replayed `command_id` returns the first
        asset and reserves nothing, even when the quota is now full; one whose
        first asset is a tombstone, and a campaign that is missing, another
        owner's or archived, are `MissingParent`; a full quota is
        `QuotaExceeded`, having written nothing."""
        ...  # pragma: no cover - structural type

    def get(self, unit: UnitOfWork, campaign_id: str, asset_id: str, *, owner_id: int) -> AssetRecord | None:
        """The GM's status: any state but `deleted`."""
        ...  # pragma: no cover - structural type

    def resolve_ready(
        self, unit: UnitOfWork, campaign_id: str, asset_id: str, *, owner_id: int
    ) -> ReadyObject | None:
        """What serving needs, for a `ready` asset only (SEC-27, MS-3)."""
        ...  # pragma: no cover - structural type

    def usage(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int) -> MediaUsage | None:
        ...  # pragma: no cover - structural type

    def hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord | None:
        """The caller's live asset, locked `FOR NO KEY UPDATE` for the rest of
        the transaction, after bounding it; None for anything else."""
        ...  # pragma: no cover - structural type

    def start_processing(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        ...  # pragma: no cover - structural type

    def return_to_uploading(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        ...  # pragma: no cover - structural type

    def mark_ready(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        measured: Measured,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        """`processing` -> `ready`, replacing the declared reservation with the
        real size. If the real size no longer fits the quota, the row becomes
        `failed` with reason `quota_exceeded` instead, and the returned record
        says so."""
        ...  # pragma: no cover - structural type

    def mark_failed(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        failure: AssetFailure | str,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        ...  # pragma: no cover - structural type

    def delete(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> int:
        """The tombstone and its `asset.delete` job; returns the job id."""
        ...  # pragma: no cover - structural type

    def delete_campaign_assets(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> list[int]:
        """The campaign primitive (SEC-36): see the module docstring."""
        ...  # pragma: no cover - structural type


# ── The decisions both worlds share ──────────────────────────────────────────

_UPLOADING = AssetState.UPLOADING.value
_PROCESSING = AssetState.PROCESSING.value
_READY = AssetState.READY.value
_FAILED = AssetState.FAILED.value


def _deadline_of(state: str) -> timedelta:
    return UPLOADING_BOUND if state == _UPLOADING else PROCESSING_BOUND


# justification: the keywords are AssetRow fields of differing types, forwarded
# to dataclasses.replace; every written row then passes the schema's rules
# (check_row in the twin, the CHECKs in PostgreSQL).
def _entered(row: AssetRow, state: str, moment: datetime, **changes: Any) -> AssetRow:
    return replace(row, state=state, updated_at=moment, state_changed_at=moment, **changes)


def _tombstone(row: AssetRow, moment: datetime) -> AssetRow:
    """No alt text, no failure, no measured value: the tombstone purges the
    asset's private text at once (L-3(f))."""
    return _entered(
        row, DELETED, moment,
        alt=None, failure=None, media_type=None, size_bytes=None, width=None, height=None, duration_ms=None,
    )


class _Transitions:
    """The state machine, over four primitives each world implements: hold the
    row, write it, try a conditional reservation, release one. The order of
    those calls is L-5's, and it is the same in both worlds."""

    _jobs: JobQueue

    def _held(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        owner_id: int,
        legal_from: tuple[str, ...],
        transaction_timeout_s: float | None = None,
    ) -> AssetRow:
        row = self._hold_row(unit, campaign_id, asset_id, owner_id, transaction_timeout_s)
        if row is None:
            raise _missing()
        if row.state not in legal_from:
            raise IllegalTransition()
        return row

    def _seed_sweep(self, unit: UnitOfWork, row: AssetRow) -> int:
        """An entry into `uploading` or `processing` seeds that state's own
        deadline sweep. No dedupe key (RQ-12): two are harmless."""
        return self._jobs.enqueue(
            unit,
            SWEEP_JOB,
            {"asset_id": row.id},
            run_after=row.state_changed_at + _deadline_of(row.state),
            now=row.state_changed_at,
        )

    def start_processing(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        moment = now_or(now)
        row = self._held(unit, campaign_id, asset_id, owner_id, (_UPLOADING,), transaction_timeout_s)
        written = self._write_row(unit, _entered(row, _PROCESSING, moment), owner_id)
        self._seed_sweep(unit, written)
        return record_of(written)

    def return_to_uploading(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        moment = now_or(now)
        row = self._held(unit, campaign_id, asset_id, owner_id, (_PROCESSING,), transaction_timeout_s)
        written = self._write_row(unit, _entered(row, _UPLOADING, moment), owner_id)
        self._seed_sweep(unit, written)
        return record_of(written)

    def mark_ready(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        measured: Measured,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        moment = now_or(now)
        row = self._held(unit, campaign_id, asset_id, owner_id, (_PROCESSING,), transaction_timeout_s)
        real = _check_measured(row, measured)
        if self._try_reserve(unit, row.campaign_id, owner_id, real.size_bytes - row.declared_size_bytes, 0):
            ready = _entered(
                row, _READY, moment,
                media_type=real.media_type, size_bytes=real.size_bytes,
                width=real.width, height=real.height, duration_ms=real.duration_ms,
            )
            return record_of(self._write_row(unit, ready, owner_id))
        # The real size no longer fits: failed, carrying no measured value, and
        # its processed object is the processing sweep's to delete (L-10(a)).
        failed = self._write_row(
            unit, _entered(row, _FAILED, moment, failure=AssetFailure.QUOTA_EXCEEDED), owner_id
        )
        self._release(unit, row.campaign_id, owner_id, row.reservation, 1)
        return record_of(failed)

    def mark_failed(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        failure: AssetFailure | str,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        reason = _failure(failure)
        moment = now_or(now)
        row = self._held(unit, campaign_id, asset_id, owner_id, (_UPLOADING, _PROCESSING), transaction_timeout_s)
        failed = self._write_row(unit, _entered(row, _FAILED, moment, failure=reason), owner_id)
        self._release(unit, row.campaign_id, owner_id, row.reservation, 1)
        return record_of(failed)

    def delete(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> int:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        moment = now_or(now)
        row = self._held(
            unit, campaign_id, asset_id, owner_id, (_UPLOADING, _PROCESSING, _READY, _FAILED), transaction_timeout_s
        )
        self._write_row(unit, _tombstone(row, moment), owner_id)
        if row.counted:
            self._release(unit, row.campaign_id, owner_id, row.reservation, 1)
        return self._jobs.enqueue(unit, DELETE_JOB, delete_payload(row), dedupe_key=row.id, now=moment)

    def hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        asset_id: str,
        *,
        owner_id: int,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord | None:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        row = self._hold_row(unit, campaign_id, asset_id, owner_id, transaction_timeout_s)
        return None if row is None else record_of(row)

    # The four primitives each world provides.

    def _hold_row(
        self, unit: UnitOfWork, campaign_id: str, asset_id: str, owner_id: int, transaction_timeout_s: float | None
    ) -> AssetRow | None:
        raise NotImplementedError  # pragma: no cover

    def _write_row(self, unit: UnitOfWork, row: AssetRow, owner_id: int) -> AssetRow:
        raise NotImplementedError  # pragma: no cover

    def _try_reserve(
        self, unit: UnitOfWork, campaign_id: str, owner_id: int, delta_bytes: int, delta_count: int
    ) -> bool:
        raise NotImplementedError  # pragma: no cover

    def _release(self, unit: UnitOfWork, campaign_id: str, owner_id: int, freed_bytes: int, freed_count: int) -> None:
        raise NotImplementedError  # pragma: no cover


def _freed(rows: Iterable[AssetRow]) -> tuple[int, int]:
    removed = list(rows)
    return sum(row.reservation for row in removed), sum(1 for row in removed if row.counted)


# ── PostgreSQL ───────────────────────────────────────────────────────────────

_COLUMNS = (
    "id, campaign_id, kind, state, declared_media_type, declared_size_bytes, media_type, size_bytes, "
    "width, height, duration_ms, alt, failure, object_key, tmp_key, created_command_id, "
    "created_at, updated_at, state_changed_at"
)


def row_from_tuple(found: tuple) -> AssetRow:
    return AssetRow(
        id=found[0],
        campaign_id=found[1],
        kind=AssetKind(found[2]),
        state=found[3],
        declared_media_type=found[4],
        declared_size_bytes=int(found[5]),
        media_type=found[6],
        size_bytes=None if found[7] is None else int(found[7]),
        width=found[8],
        height=found[9],
        duration_ms=found[10],
        alt=found[11],
        failure=None if found[12] is None else AssetFailure(found[12]),
        object_key=found[13],
        tmp_key=found[14],
        created_command_id=found[15],
        created_at=found[16],
        updated_at=found[17],
        state_changed_at=found[18],
    )


def bound_before_locking(transaction: PgTransaction, transaction_timeout_s: float | None) -> None:
    """`participant_store.hold`'s pattern: say a row lock is coming, so a later
    `lock_campaign` in this unit is refused (RQ-2), and bound the transaction
    first (RQ-8)."""
    transaction.note_row_lock()
    transaction.conn.execute(
        "SELECT set_config('transaction_timeout', %s, true)",
        (transaction.transaction_bound(transaction_timeout_s),),
    )


class PostgresAssetStore(_Transitions):
    """`campaign.assets` and `campaign.media_usage`."""

    def __init__(self, jobs: JobQueue) -> None:
        self._jobs = jobs

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        kind: AssetKind | str,
        media_type: str,
        size_bytes: int,
        alt: str | None = None,
        command_id: str | None = None,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        _check_types(
            campaign_id=campaign_id, owner_id=owner_id, media_type=media_type, size_bytes=size_bytes,
            alt=alt, command_id=command_id,
        )
        declared = _declared(kind, media_type, size_bytes, alt, command_id)
        moment = now_or(now)
        transaction = pg(unit)
        bound_before_locking(transaction, transaction_timeout_s)
        conn = transaction.conn
        params = {
            "id": ident.new_id(ident.ASSET),
            "campaign_id": campaign_id,
            "owner_id": owner_id,
            "kind": declared.kind.value,
            "media_type": declared.media_type,
            "size_bytes": declared.size_bytes,
            "alt": declared.alt,
            "object_key": new_object_key(),
            "tmp_key": new_tmp_key(),
            "command_id": declared.command_id,
            "now": moment,
            "quota_bytes": QUOTA_BYTES,
            "quota_assets": QUOTA_ASSETS,
        }
        created: AssetRow | None = None
        # A savepoint (participant_store.offer's pattern): a quota refusal rolls
        # back to it, taking the new row with it, and leaves the caller's
        # transaction usable. No case in this method raises a database error.
        with conn.transaction():
            # L-5 step 0: the campaign row, FOR KEY SHARE, in the statement that
            # checks ownership. The new row's foreign key takes this lock a
            # moment later anyway; taken first, a create that loses to the
            # campaign's deletion waits, finds nothing, and answers MissingParent.
            guard = conn.execute(
                f"SELECT c.id FROM campaign.campaigns c "
                f"WHERE c.id = %(campaign_id)s AND c.archived_at IS NULL AND c.id IN ({GM_CAMPAIGNS}) "
                f"FOR KEY SHARE OF c",
                params,
            ).fetchone()
            found = None
            if guard is not None:
                found = conn.execute(
                    f"INSERT INTO campaign.assets (id, campaign_id, kind, state, declared_media_type, "
                    f"declared_size_bytes, alt, object_key, tmp_key, created_command_id, created_at, "
                    f"updated_at, state_changed_at) "
                    f"SELECT %(id)s, c.id, %(kind)s, 'uploading', %(media_type)s, %(size_bytes)s, %(alt)s, "
                    f"%(object_key)s, %(tmp_key)s, %(command_id)s, %(now)s, %(now)s, %(now)s "
                    f"FROM campaign.campaigns c "
                    f"WHERE c.id = %(campaign_id)s AND c.archived_at IS NULL AND c.id IN ({GM_CAMPAIGNS}) "
                    f"ON CONFLICT (campaign_id, created_command_id) WHERE created_command_id IS NOT NULL "
                    f"DO NOTHING RETURNING {_COLUMNS}",
                    params,
                ).fetchone()
            if found is None:
                # Replay first, before anything is reserved: a replayed command
                # returns its first asset even when the quota is now full.
                return self._replay_or_refuse(conn, params)
            created = row_from_tuple(found)
            # One conditional UPDATE (SEC-31). The loser of two racing creates
            # gets no row back ONLY because PostgreSQL re-evaluates this WHERE
            # against the winner's committed row version under READ COMMITTED,
            # which Database.transaction() sets; under REPEATABLE READ it would
            # raise a serialisation error instead. The insert above has already
            # shown, in this transaction, that the campaign is the caller's, so
            # no row changed can only mean over quota.
            reserved = conn.execute(
                f"UPDATE campaign.media_usage "
                f"SET bytes_reserved = bytes_reserved + %(size_bytes)s, asset_count = asset_count + 1 "
                f"WHERE campaign_id = %(campaign_id)s AND campaign_id IN ({GM_CAMPAIGNS}) "
                f"AND bytes_reserved + %(size_bytes)s <= %(quota_bytes)s "
                f"AND asset_count + 1 <= %(quota_assets)s RETURNING campaign_id",
                params,
            ).fetchone()
            if reserved is None:
                raise QuotaExceeded()
        self._seed_sweep(unit, created)
        return record_of(created)

    # justification: psycopg's Connection is generic over its row factory, and
    # this reads plain tuples through it (service/db.py's own note).
    def _replay_or_refuse(self, conn: Any, params: Mapping[str, object]) -> AssetRecord:
        """Why the insert made nothing (document_store._replay_or_refuse's
        shape), asked through the GM fragment, so it can only ever describe the
        caller's own rows. A live asset made by this command is returned as it
        is; a tombstone, or nothing, is MissingParent."""
        if params["command_id"] is not None:
            made = conn.execute(
                f"SELECT {_COLUMNS} FROM campaign.assets "
                f"WHERE campaign_id = %(campaign_id)s AND created_command_id = %(command_id)s "
                f"AND campaign_id IN ({GM_CAMPAIGNS})",
                params,
            ).fetchone()
            if made is not None and made[3] != DELETED:
                return record_of(row_from_tuple(made))
        raise _missing()

    def get(self, unit: UnitOfWork, campaign_id: str, asset_id: str, *, owner_id: int) -> AssetRecord | None:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        found = pg(unit).conn.execute(
            f"SELECT {_COLUMNS} FROM campaign.assets "
            f"WHERE id = %(asset_id)s AND campaign_id = %(campaign_id)s AND campaign_id IN ({GM_CAMPAIGNS}) "
            f"AND state <> 'deleted'",
            {"asset_id": asset_id, "campaign_id": campaign_id, "owner_id": owner_id},
        ).fetchone()
        return None if found is None else record_of(row_from_tuple(found))

    def resolve_ready(
        self, unit: UnitOfWork, campaign_id: str, asset_id: str, *, owner_id: int
    ) -> ReadyObject | None:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        found = pg(unit).conn.execute(
            f"SELECT object_key, media_type, size_bytes FROM campaign.assets "
            f"WHERE id = %(asset_id)s AND campaign_id = %(campaign_id)s AND campaign_id IN ({GM_CAMPAIGNS}) "
            f"AND state = 'ready'",
            {"asset_id": asset_id, "campaign_id": campaign_id, "owner_id": owner_id},
        ).fetchone()
        return None if found is None else ReadyObject(found[0], found[1], int(found[2]))

    def usage(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int) -> MediaUsage | None:
        _check_types(campaign_id=campaign_id, owner_id=owner_id)
        found = pg(unit).conn.execute(
            f"SELECT bytes_reserved, asset_count FROM campaign.media_usage "
            f"WHERE campaign_id = %(campaign_id)s AND campaign_id IN ({GM_CAMPAIGNS})",
            {"campaign_id": campaign_id, "owner_id": owner_id},
        ).fetchone()
        return None if found is None else MediaUsage(int(found[0]), int(found[1]))

    def _hold_row(
        self, unit: UnitOfWork, campaign_id: str, asset_id: str, owner_id: int, transaction_timeout_s: float | None
    ) -> AssetRow | None:
        transaction = pg(unit)
        bound_before_locking(transaction, transaction_timeout_s)
        # The campaign and its owner go into the statement that takes the lock:
        # a row the WHERE does not match is not locked at all. A tombstone is
        # never held, so it is missing to every mutator.
        found = transaction.conn.execute(
            f"SELECT {_COLUMNS} FROM campaign.assets "
            f"WHERE id = %(asset_id)s AND campaign_id = %(campaign_id)s AND campaign_id IN ({GM_CAMPAIGNS}) "
            f"AND state <> 'deleted' FOR NO KEY UPDATE",
            {"asset_id": asset_id, "campaign_id": campaign_id, "owner_id": owner_id},
        ).fetchone()
        return None if found is None else row_from_tuple(found)

    def _write_row(self, unit: UnitOfWork, row: AssetRow, owner_id: int) -> AssetRow:
        """The row is already held, so this UPDATE takes no new lock. It never
        assigns a key: they are key columns (RQ-3), and immutable."""
        written = pg(unit).conn.execute(
            f"UPDATE campaign.assets SET state = %(state)s, failure = %(failure)s, "
            f"media_type = %(media_type)s, size_bytes = %(size_bytes)s, width = %(width)s, "
            f"height = %(height)s, duration_ms = %(duration_ms)s, alt = %(alt)s, "
            f"updated_at = %(updated_at)s, state_changed_at = %(state_changed_at)s "
            f"WHERE id = %(asset_id)s AND campaign_id = %(campaign_id)s AND campaign_id IN ({GM_CAMPAIGNS}) "
            f"RETURNING {_COLUMNS}",
            {
                "state": row.state,
                "failure": None if row.failure is None else row.failure.value,
                "media_type": row.media_type,
                "size_bytes": row.size_bytes,
                "width": row.width,
                "height": row.height,
                "duration_ms": row.duration_ms,
                "alt": row.alt,
                "updated_at": row.updated_at,
                "state_changed_at": row.state_changed_at,
                "asset_id": row.id,
                "campaign_id": row.campaign_id,
                "owner_id": owner_id,
            },
        ).fetchone()
        if written is None:  # pragma: no cover - the row is held, so it cannot vanish
            raise _missing()
        return row_from_tuple(written)

    def _try_reserve(
        self, unit: UnitOfWork, campaign_id: str, owner_id: int, delta_bytes: int, delta_count: int
    ) -> bool:
        # Conditional, and correct only under READ COMMITTED (see create).
        changed = pg(unit).conn.execute(
            f"UPDATE campaign.media_usage "
            f"SET bytes_reserved = bytes_reserved + %(delta_bytes)s, asset_count = asset_count + %(delta_count)s "
            f"WHERE campaign_id = %(campaign_id)s AND campaign_id IN ({GM_CAMPAIGNS}) "
            f"AND bytes_reserved + %(delta_bytes)s <= %(quota_bytes)s "
            f"AND asset_count + %(delta_count)s <= %(quota_assets)s RETURNING campaign_id",
            {
                "delta_bytes": delta_bytes,
                "delta_count": delta_count,
                "campaign_id": campaign_id,
                "owner_id": owner_id,
                "quota_bytes": QUOTA_BYTES,
                "quota_assets": QUOTA_ASSETS,
            },
        ).fetchone()
        return changed is not None

    def _release(self, unit: UnitOfWork, campaign_id: str, owner_id: int, freed_bytes: int, freed_count: int) -> None:
        pg(unit).conn.execute(
            f"UPDATE campaign.media_usage "
            f"SET bytes_reserved = bytes_reserved - %(freed_bytes)s, asset_count = asset_count - %(freed_count)s "
            f"WHERE campaign_id = %(campaign_id)s AND campaign_id IN ({GM_CAMPAIGNS})",
            {"freed_bytes": freed_bytes, "freed_count": freed_count, "campaign_id": campaign_id, "owner_id": owner_id},
        )

    def delete_campaign_assets(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> list[int]:
        _check_types(campaign_id=campaign_id, owner_id=owner_id)
        moment = now_or(now)
        transaction = pg(unit)
        bound_before_locking(transaction, transaction_timeout_s)
        conn = transaction.conn
        params = {"campaign_id": campaign_id, "owner_id": owner_id}
        # One statement, and what it RETURNS is the truth about what it removed:
        # a create committing between an earlier SELECT and this DELETE would be
        # removed with no job. A row whose GM delete committed while this waited
        # comes back as its new version, the tombstone, which has its job.
        removed = [
            row_from_tuple(found)
            for found in conn.execute(
                f"DELETE FROM campaign.assets "
                f"WHERE campaign_id = %(campaign_id)s AND campaign_id IN ({OWNER_CAMPAIGNS}) "
                f"RETURNING {_COLUMNS}",
                params,
            ).fetchall()
        ]
        freed_bytes, freed_count = _freed(removed)
        if freed_bytes or freed_count:
            # Subtract exactly what was removed; never zero. A create that
            # commits after the DELETE's snapshot keeps its row and its
            # reservation, and a zero would break the invariant under it.
            conn.execute(
                f"UPDATE campaign.media_usage "
                f"SET bytes_reserved = bytes_reserved - %(freed_bytes)s, "
                f"asset_count = asset_count - %(freed_count)s "
                f"WHERE campaign_id = %(campaign_id)s AND campaign_id IN ({OWNER_CAMPAIGNS})",
                {**params, "freed_bytes": freed_bytes, "freed_count": freed_count},
            )
        return [
            self._jobs.enqueue(unit, DELETE_JOB, delete_payload(row), dedupe_key=row.id, now=moment)
            for row in removed
            if row.state != DELETED
        ]


# ── The in-memory twin ───────────────────────────────────────────────────────


def asset_rows(db: InMemoryDatabase) -> Staging[AssetRow | None]:
    """The twin's `campaign.assets`. A purged row is staged as None — `Staging`
    has no delete — published on commit and dropped on rollback; every reader
    skips it."""
    return shared_rows(db, "assets")


def visible_rows(rows: Staging[AssetRow | None], unit: InMemoryTransaction) -> dict[str, AssetRow]:
    return {key: row for key, row in rows.visible(unit).items() if row is not None}


def stage_row(rows: Staging[AssetRow | None], unit: InMemoryTransaction, row: AssetRow, *, new: bool) -> AssetRow:
    """Every twin write to `campaign.assets` comes here: the CHECKs, then the
    three unique indexes, then the staging area. Refused before anything is
    staged, so a refusal leaves the twin untouched."""
    check_row(row)
    others = [other for key, other in visible_rows(rows, unit).items() if key != row.id]
    if new and row.id in visible_rows(rows, unit):
        raise AssetRowRefused("an asset row breaks the schema's rule on id")
    if any(row.object_key in (o.object_key, o.tmp_key) or row.tmp_key in (o.object_key, o.tmp_key) for o in others):
        raise AssetRowRefused("an asset row breaks the schema's rule on its keys")
    if row.created_command_id is not None and any(
        o.campaign_id == row.campaign_id and o.created_command_id == row.created_command_id for o in others
    ):
        raise AssetRowRefused("an asset row breaks the schema's rule on created_command_id")
    if new:
        rows.add(unit, row.id, row)
    else:
        rows.replace(unit, row.id, row)
    return row


class InMemoryAssetStore(_Transitions):
    """The twin (L-16). It reads the shared `campaigns` table for the owner and
    the archive state, derives the usage from its rows (so it needs no usage
    table), stages every write with commit-time visibility, and enforces the
    migration's CHECKs, its three unique indexes and the usage invariant itself.
    It decides as PostgreSQL does; who blocks whom is PostgreSQL's to prove."""

    def __init__(self, db: InMemoryDatabase, jobs: JobQueue) -> None:
        self._rows: Staging[AssetRow | None] = asset_rows(db)
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._jobs = jobs

    def _owned(self, twin: InMemoryTransaction, campaign_id: str, owner_id: int) -> Campaign | None:
        """The GM fragment, for the twin: the campaign exists and the caller
        owns it. `yje.2.1`'s conjunct goes here too."""
        campaign = self._campaigns.visible(twin).get(campaign_id)
        return campaign if campaign is not None and campaign.owner_id == owner_id else None

    def _mine(self, twin: InMemoryTransaction, campaign_id: str, owner_id: int) -> list[AssetRow]:
        if self._owned(twin, campaign_id, owner_id) is None:
            return []
        return [row for row in visible_rows(self._rows, twin).values() if row.campaign_id == campaign_id]

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        kind: AssetKind | str,
        media_type: str,
        size_bytes: int,
        alt: str | None = None,
        command_id: str | None = None,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> AssetRecord:
        _check_types(
            campaign_id=campaign_id, owner_id=owner_id, media_type=media_type, size_bytes=size_bytes,
            alt=alt, command_id=command_id,
        )
        declared = _declared(kind, media_type, size_bytes, alt, command_id)
        moment = now_or(now)
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(transaction_timeout_s)
        campaign = self._owned(twin, campaign_id, owner_id)
        earlier = None
        if declared.command_id is not None:
            earlier = next(
                (
                    row for row in visible_rows(self._rows, twin).values()
                    if row.campaign_id == campaign_id and row.created_command_id == declared.command_id
                ),
                None,
            )
        if campaign is None or campaign.is_archived or earlier is not None:
            # The insert would make nothing: ask why, through ownership.
            if campaign is not None and earlier is not None and earlier.state != DELETED:
                return record_of(earlier)
            raise _missing()
        # One open writer, so the twin may check before it stages; it gives the
        # same answers in the same cases as the insert-then-reserve order.
        usage = usage_of(visible_rows(self._rows, twin).values(), campaign_id)
        if usage.bytes_reserved + declared.size_bytes > QUOTA_BYTES or usage.asset_count + 1 > QUOTA_ASSETS:
            raise QuotaExceeded()
        created = stage_row(
            self._rows,
            twin,
            AssetRow(
                id=ident.new_id(ident.ASSET),
                campaign_id=campaign_id,
                kind=declared.kind,
                state=_UPLOADING,
                declared_media_type=declared.media_type,
                declared_size_bytes=declared.size_bytes,
                media_type=None,
                size_bytes=None,
                width=None,
                height=None,
                duration_ms=None,
                alt=declared.alt,
                failure=None,
                object_key=new_object_key(),
                tmp_key=new_tmp_key(),
                created_command_id=declared.command_id,
                created_at=moment,
                updated_at=moment,
                state_changed_at=moment,
            ),
            new=True,
        )
        self._seed_sweep(unit, created)
        return record_of(created)

    def _find(self, twin: InMemoryTransaction, campaign_id: str, asset_id: str, owner_id: int) -> AssetRow | None:
        found = visible_rows(self._rows, twin).get(asset_id)
        if found is None or found.campaign_id != campaign_id or self._owned(twin, campaign_id, owner_id) is None:
            return None
        return found

    def get(self, unit: UnitOfWork, campaign_id: str, asset_id: str, *, owner_id: int) -> AssetRecord | None:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        found = self._find(fake(unit), campaign_id, asset_id, owner_id)
        return None if found is None or found.state == DELETED else record_of(found)

    def resolve_ready(
        self, unit: UnitOfWork, campaign_id: str, asset_id: str, *, owner_id: int
    ) -> ReadyObject | None:
        _check_types(campaign_id=campaign_id, asset_id=asset_id, owner_id=owner_id)
        found = self._find(fake(unit), campaign_id, asset_id, owner_id)
        if found is None or found.state != _READY or found.media_type is None or found.size_bytes is None:
            return None
        return ReadyObject(found.object_key, found.media_type, found.size_bytes)

    def usage(self, unit: UnitOfWork, campaign_id: str, *, owner_id: int) -> MediaUsage | None:
        _check_types(campaign_id=campaign_id, owner_id=owner_id)
        twin = fake(unit)
        if self._owned(twin, campaign_id, owner_id) is None:
            return None
        return usage_of(visible_rows(self._rows, twin).values(), campaign_id)

    def _hold_row(
        self, unit: UnitOfWork, campaign_id: str, asset_id: str, owner_id: int, transaction_timeout_s: float | None
    ) -> AssetRow | None:
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(transaction_timeout_s)
        found = self._find(twin, campaign_id, asset_id, owner_id)
        return None if found is None or found.state == DELETED else found

    def _write_row(self, unit: UnitOfWork, row: AssetRow, owner_id: int) -> AssetRow:
        return stage_row(self._rows, fake(unit), row, new=False)

    def _try_reserve(
        self, unit: UnitOfWork, campaign_id: str, owner_id: int, delta_bytes: int, delta_count: int
    ) -> bool:
        usage = usage_of(visible_rows(self._rows, fake(unit)).values(), campaign_id)
        return (
            usage.bytes_reserved + delta_bytes <= QUOTA_BYTES and usage.asset_count + delta_count <= QUOTA_ASSETS
        )

    def _release(self, unit: UnitOfWork, campaign_id: str, owner_id: int, freed_bytes: int, freed_count: int) -> None:
        """The twin's usage is derived from its rows, so the row's own write
        already released it."""

    def delete_campaign_assets(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        owner_id: int,
        now: datetime | None = None,
        transaction_timeout_s: float | None = None,
    ) -> list[int]:
        _check_types(campaign_id=campaign_id, owner_id=owner_id)
        moment = now_or(now)
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(transaction_timeout_s)
        removed = self._mine(twin, campaign_id, owner_id)
        for row in removed:
            self._rows.replace(twin, row.id, None)
        return [
            self._jobs.enqueue(unit, DELETE_JOB, delete_payload(row), dedupe_key=row.id, now=moment)
            for row in removed
            if row.state != DELETED
        ]

"""The slot resolver: what one table principal may read of a live session, as a
`TableSnapshot` (agent-forge-harness-1kg.7.2 PR-2; ID-15, ADR RT-4, RT-9).

`TableReads.snapshot` is the **only** way `table_api` reaches stored document
text (T-23): the route imports this module and never the document store.

**One transaction, reads only, no lock.**

1. `view_for_account` or `view_for_screen` finds the live session and what the
   principal is entitled to: the table slot, and its own slot iff the seat is
   confirmed (SEC-41, I-12). That query is the whole of the entitlement: this
   module consults no eligibility rule (ED-25, C-15), and it expands no
   audience (ED-13: an *Everyone seated* Confirm named its seats when it was
   written, so a seat confirmed later reads only what was written to it by name).
2. For each shown slot, **only** `documents.snapshot(campaign, document,
   slot.version)`. Never `get`, `hold` or `list_documents`: the text a table
   reads is the pinned version, and only a sealed one whose type is the one the
   disclosure recorded (C-6b). Anything else is an empty slot.
3. `table_projection.build` makes the projection from the mask.
4. The frames: `session` (audio, and the role: `participant` iff the own slot is
   entitled, else `guest`, ID-15), `snapshot` (`table`, and `mine` iff entitled)
   followed by the authoritative audio slots and `ready`.

`TableSnapshot` validates the frames. When it refuses them, the same frames are
emitted with **every** `content` empty, and the log line names the exception
type only (fail closed, F-6): a table shown nothing is a table that is wrong in
the safe direction.

`None` means *not entitled*, for every reason alike: no live session, a seat
that is not accepted or is removed, an account that is nobody's, a grant that is
dead or another campaign's. The route answers it with the one `inactive` (SEC-46).

Streams use `TableStreamReads` to recheck a batch's standing, session
generation and authorized pinned values in the same PostgreSQL statement. A
rotated watch gets a content-free signal only if its original account standing
still entitles it to the same live session.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from itertools import groupby
from operator import itemgetter
from typing import Final, cast

from pydantic import ValidationError

from .campaign_store import aware
from .db import Database, TransactionalDatabase, UnitOfWork
from .document_store import DocumentStore
from .reveal_store import EMPTY_SLOT, RevealStore, SlotContent, TableView
from .table_audio_projection import AUDIO_SLOT_ROWS, opening_audio, table_audio_frames
from .table_presence import context_for
from .table_projection import build
from .workbench_contracts import (
    CONTRACT_VERSION,
    AudioSlot,
    SchemaVersion,
    TableAudioEvent,
    TableProjection,
    TableReadyEvent,
    TableRevealSnapshotEvent,
    TableRole,
    TableSessionEvent,
    TableSlot,
    TableSlotName,
    TableSnapshot,
)

log = logging.getLogger(__name__)

#: `Literal[1]` on the wire, `int` as a constant: spelled once.
WIRE_VERSION = cast("SchemaVersion", CONTRACT_VERSION)


class TableReads:
    """The table's read side, over one database and the two stores it reads."""

    def __init__(self, db: TransactionalDatabase, reveals: RevealStore, documents: DocumentStore) -> None:
        self._db = db
        self._reveals = reveals
        self._documents = documents

    def snapshot(
        self, campaign_id: str, *, now: datetime, account_id: int | None = None, grant_id: str | None = None
    ) -> TableSnapshot | None:
        return self.snapshot_with_context(campaign_id, now=now, account_id=account_id, grant_id=grant_id)[0]

    def snapshot_with_context(
        self, campaign_id: str, *, now: datetime, account_id: int | None = None, grant_id: str | None = None
    ) -> tuple[TableSnapshot | None, str | None]:
        """The principal's snapshot of the campaign's live session, or `None`.
        Exactly one of `account_id` (a signed-in account) and `grant_id` (a live
        screen's grant) names the principal."""
        if (account_id is None) == (grant_id is None):
            raise ValueError("a snapshot is read for one principal")
        moment = aware(now, "a clock")
        with self._db.transaction() as unit:
            if grant_id is not None:
                view = self._reveals.view_for_screen(unit, campaign_id, grant_id, now=moment)
            else:
                assert account_id is not None
                view = self._reveals.view_for_account(unit, campaign_id, user_id=account_id, now=moment)
            if view is None:
                return None, None
            slots = [self._slot(unit, view, TableSlotName.TABLE, view.table)]
            if view.own_slot:
                slots.append(self._slot(unit, view, TableSlotName.MINE, view.mine or EMPTY_SLOT))
        context = context_for(
            campaign_id, view.session_id, view.generation, account_id=account_id, grant_id=grant_id,
            audio_generation=view.audio_generation, participant_id=None if view.is_owner else view.participant_id,
        )
        return _frames(view.own_slot, view.audio, slots, [opening_audio(f, moment) for f in view.audio_frames]), (
            context if view.audio else None
        )

    def _slot(self, unit: UnitOfWork, view: TableView, name: TableSlotName, held: SlotContent) -> TableSlot:
        return TableSlot(slot=name, seq=held.seq, content=self._content(unit, view.campaign_id, held))

    def _content(self, unit: UnitOfWork, campaign_id: str, held: SlotContent) -> TableProjection | None:
        if held.document_id is None or held.version is None:
            return None
        pinned = self._documents.snapshot(unit, campaign_id, held.document_id, held.version)
        if pinned is None or not pinned.version.is_sealed or pinned.type != held.document_type:
            return None
        return build(pinned.type, pinned.data, held.mask)


TABLE_STREAM_BATCH_MAX: Final = 20


class TableStreamSignal(Enum):
    ROTATED = auto()


@dataclass(frozen=True)
class TableStreamWatch:
    """The server-derived standing to recheck for one admitted stream."""

    campaign_id: str
    session_id: str
    generation: int
    account_id: int | None = field(default=None, repr=False)
    grant_id: str | None = field(default=None, repr=False)
    participant_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.account_id is None) == (self.grant_id is None):
            raise ValueError("a stream is watched for one principal")


_STREAM_READ_SQL: Final = f"""
WITH watched AS (
    SELECT * FROM unnest(%(campaigns)s::text[], %(sessions)s::text[], %(generations)s::int[],
                         %(accounts)s::bigint[], %(grants)s::text[], %(seats)s::text[])
    WITH ORDINALITY AS w(campaign_id, session_id, generation, account_id, grant_id, participant_id, ordinal)
), standing AS (
    SELECT w.ordinal, s.id, s.campaign_id, s.link_generation > w.generation AS rotated,
           (s.table_audio AND c.archived_at IS NULL) AS table_audio,
           p.id AS participant_id, p.confirmed_at IS NOT NULL AS own_slot
    FROM watched w
    JOIN campaign.table_sessions s ON s.id = w.session_id AND s.campaign_id = w.campaign_id
        AND s.state = 'live' AND s.expires_at > %(now)s AND s.link_generation >= w.generation
    JOIN campaign.campaigns c ON c.id = s.campaign_id
    LEFT JOIN campaign.participants p ON p.id = w.participant_id AND p.campaign_id = s.campaign_id
        AND p.user_id = w.account_id AND c.owner_id <> w.account_id
        AND p.accepted_at IS NOT NULL AND p.removed_at IS NULL
    LEFT JOIN campaign.table_credentials g ON g.id = w.grant_id AND g.session_id = s.id
        AND g.link_generation = w.generation AND g.link_generation = s.link_generation AND g.revoked_at IS NULL
    WHERE (c.owner_id = w.account_id AND w.participant_id IS NULL) OR p.id IS NOT NULL OR g.id IS NOT NULL
), eligible AS (
    SELECT s.*, {AUDIO_SLOT_ROWS} AS audio_slots FROM standing s WHERE NOT s.rotated
)
SELECT e.ordinal, audience.ordering, e.own_slot, e.table_audio, audience.region, COALESCE(r.seq, 0), d.type, f.mask,
       (SELECT jsonb_object_agg(key, v.data -> key) FROM unnest(f.mask) AS k(key) WHERE v.data ? key), e.audio_slots
FROM eligible e
CROSS JOIN LATERAL (
    SELECT 'table' AS region, NULL::text AS participant_id, 0 AS ordering
    UNION ALL
    SELECT 'mine', e.participant_id, 1 WHERE e.own_slot
) audience
LEFT JOIN campaign.reveal_slots r ON r.session_id = e.id AND r.campaign_id = e.campaign_id
    AND r.participant_id IS NOT DISTINCT FROM audience.participant_id
LEFT JOIN campaign.reveal_disclosures f ON f.id = r.disclosure_id AND f.campaign_id = e.campaign_id
    AND f.session_id = e.id AND f.ended_at IS NULL
LEFT JOIN campaign.documents d ON d.id = f.document_id AND d.campaign_id = e.campaign_id
LEFT JOIN campaign.document_versions v ON v.document_id = d.id AND v.number = f.version_number
    AND v.sealed_at IS NOT NULL
UNION ALL
SELECT ordinal, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL FROM standing WHERE rotated
ORDER BY ordinal, ordering
"""


class TableStreamReads:
    """Read current stream snapshots without taking a lock or a sync connection."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def snapshots(
        self, watches: Sequence[TableStreamWatch], *, now: datetime
    ) -> list[TableSnapshot | TableStreamSignal | None]:
        """Results align with the watches. Denied watches are empty. Rotated accounts reopen."""
        if not watches:
            return []
        if len(watches) > TABLE_STREAM_BATCH_MAX:
            raise ValueError("too many table streams to read in one batch")
        values = {
            "campaigns": [watch.campaign_id for watch in watches],
            "sessions": [watch.session_id for watch in watches],
            "generations": [watch.generation for watch in watches],
            "accounts": [watch.account_id for watch in watches],
            "grants": [watch.grant_id for watch in watches],
            "seats": [watch.participant_id for watch in watches],
            "now": aware(now, "a clock"),
        }
        async with self._db.async_connection() as connection:
            rows = await (await connection.execute(_STREAM_READ_SQL, values)).fetchall()
        snapshots: list[TableSnapshot | TableStreamSignal | None] = [None] * len(watches)
        for ordinal, group in groupby(rows, itemgetter(0)):
            entries = list(group)
            _, ordering, own_slot, audio, *_, audio_rows = entries[0]
            if ordering is None:
                snapshots[ordinal - 1] = TableStreamSignal.ROTATED
                continue
            slots = [
                TableSlot(
                    slot=TableSlotName(region), seq=seq,
                    content=None if doc_type is None or data is None else build(doc_type, data, mask),
                )
                for _, _, _, _, region, seq, doc_type, mask, data, _ in entries
            ]
            snapshots[ordinal - 1] = _frames(own_slot, audio, slots, table_audio_frames(audio_rows, now))
        return snapshots


def _frames(
    own_slot: bool, audio: bool, slots: list[TableSlot], audio_frames: Sequence[TableAudioEvent] = (),
) -> TableSnapshot:
    role = TableRole.PARTICIPANT if own_slot else TableRole.GUEST
    by_slot = {frame.slot: frame for frame in audio_frames}
    audio_picture = [
        by_slot[slot] if slot in by_slot else TableAudioEvent(
            schema_version=WIRE_VERSION, event="audio", slot=slot, seq=0, playing=None,
        )
        for slot in AudioSlot
    ] if audio else []

    def assemble(shown: list[TableSlot]) -> TableSnapshot:
        return TableSnapshot.model_validate(
            {
                "schema_version": WIRE_VERSION,
                "frames": [
                    TableSessionEvent(schema_version=WIRE_VERSION, event="session", audio=audio, role=role),
                    TableRevealSnapshotEvent(schema_version=WIRE_VERSION, event="snapshot", slots=shown),
                    *audio_picture,
                    TableReadyEvent(schema_version=WIRE_VERSION, event="ready"),
                ],
            }
        )

    try:
        return assemble(slots)
    except ValidationError as exc:
        log.error("table snapshot refused, every slot emptied (%s)", type(exc).__name__)
        return assemble([TableSlot(slot=slot.slot, seq=slot.seq, content=None) for slot in slots])

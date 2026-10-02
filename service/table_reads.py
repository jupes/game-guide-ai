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
   and `ready`. There are no audio frames until cue slots exist (1kg.8.6).

`TableSnapshot` validates the frames. When it refuses them, the same frames are
emitted with **every** `content` empty, and the log line names the exception
type only (fail closed, F-6): a table shown nothing is a table that is wrong in
the safe direction.

`None` means *not entitled*, for every reason alike: no live session, a seat
that is not accepted or is removed, an account that is nobody's, a grant that is
dead or another campaign's. The route answers it with the one `inactive` (SEC-46).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from .campaign_store import aware
from .db import TransactionalDatabase, UnitOfWork
from .document_store import DocumentStore
from .reveal_store import EMPTY_SLOT, RevealStore, SlotContent, TableView
from .table_projection import build
from .workbench_contracts import (
    CONTRACT_VERSION,
    SchemaVersion,
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
                return None
            slots = [self._slot(unit, view, TableSlotName.TABLE, view.table)]
            if view.own_slot:
                slots.append(self._slot(unit, view, TableSlotName.MINE, view.mine or EMPTY_SLOT))
        return _frames(view, slots)

    def _slot(self, unit: UnitOfWork, view: TableView, name: TableSlotName, held: SlotContent) -> TableSlot:
        return TableSlot(slot=name, seq=held.seq, content=self._content(unit, view.campaign_id, held))

    def _content(self, unit: UnitOfWork, campaign_id: str, held: SlotContent) -> TableProjection | None:
        if held.document_id is None or held.version is None:
            return None
        pinned = self._documents.snapshot(unit, campaign_id, held.document_id, held.version)
        if pinned is None or not pinned.version.is_sealed or pinned.type != held.document_type:
            return None
        return build(pinned.type, pinned.data, held.mask)


def _frames(view: TableView, slots: list[TableSlot]) -> TableSnapshot:
    role = TableRole.PARTICIPANT if view.own_slot else TableRole.GUEST

    def assemble(shown: list[TableSlot]) -> TableSnapshot:
        return TableSnapshot.model_validate(
            {
                "schema_version": WIRE_VERSION,
                "frames": [
                    TableSessionEvent(schema_version=WIRE_VERSION, event="session", audio=view.audio, role=role),
                    TableRevealSnapshotEvent(schema_version=WIRE_VERSION, event="snapshot", slots=shown),
                    TableReadyEvent(schema_version=WIRE_VERSION, event="ready"),
                ],
            }
        )

    try:
        return assemble(slots)
    except ValidationError as exc:
        log.error("table snapshot refused, every slot emptied (%s)", type(exc).__name__)
        return assemble([TableSlot(slot=slot.slot, seq=slot.seq, content=None) for slot in slots])

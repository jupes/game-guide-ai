"""The reveal vocabulary: why a display ended, what a narrowing clears, and
whom a Confirm names (agent-forge-harness-1kg.7.1).

A leaf module. It imports no store, so `table_session_store` (whose `narrow`
learns a scope in the next change of this bead) and `reveal_store` can both
import it without a cycle.

**`EndReason`** is the closed set a disclosure's `ended_reason` holds, the SQL
CHECK of `0018_reveal_disclosures.sql` spelt again and pinned equal by
`tests/test_reveal_db.py`. It follows ED-17's spelling: a Rotate ends a display
as `link_rotated`. A member is only ever written as `ended_reason`; it is a code,
never text.

**A scope says what a narrowing invalidates**, so each one clears exactly the
displays it makes invalid and nothing else (RQ-7, read as a scoped clear):

* `EverySlot` — End, expiry, Rotate, a campaign's archive, a Stop-all, and the
  reconciliation of a dead session;
* `ParticipantSlots` — a member's removal stops that member's copy (A-20), and
  the reconciliation of removed seats in a live session;
* `DocumentCopies` — a Stop of one document, and a document's archive, deletion
  or unlink;
* `NoSlot` — a narrowing that clears no reveal slot (table audio switched off).

`NARROWED`, every slot, is the fail-closed default: a caller that forgets to
name a scope over-clears, and never leaves content up. `1ir.2.1`'s group
narrowings (a member's or a group's removal) name it on purpose: a disclosure
does not record the group it went to yet, so which copies a group change
invalidates is not known until `1ir.2.x`.

**An audience is what a Confirm names; a target is what a write fills.**
`EveryoneSeated` is expanded by the server, under the campaign's share lock and
in the Confirm's own transaction, to the participants whose seats are accepted
and GM-confirmed — never from a list a client sends (TA-5, ID-12). A target is
the table slot alone, or one or more participant slots, never both (I-3): TP-1
is signed *do not widen*, so there is no party slot and no new slot kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class EndReason(StrEnum):
    """Why a disclosure ended — `0018_reveal_disclosures.sql`'s CHECK, in the
    same order. Content-free by construction: every member is a code."""

    GM_STOP = "gm_stop"
    STOP_ALL = "stop_all"
    REPLACED = "replaced"
    MOVED = "moved"
    UPDATED = "updated"
    GM_END = "gm_end"
    EXPIRED = "expired"
    LINK_ROTATED = "link_rotated"
    PARTICIPANT_REMOVED = "participant_removed"
    CHARACTER_UNLINKED = "character_unlinked"
    DOCUMENT_ARCHIVED = "document_archived"
    DOCUMENT_DELETED = "document_deleted"
    CAMPAIGN_ARCHIVED = "campaign_archived"
    RECONCILED = "reconciled"
    NARROWED = "narrowed"


#: `audience_kind`, in both tables.
TABLE: Final = "table"
PARTICIPANT: Final = "participant"


@dataclass(frozen=True)
class EverySlot:
    """Every slot of the session that holds live content."""

    reason: EndReason


@dataclass(frozen=True)
class ParticipantSlots:
    """Those participants' slots of the session, and nothing else: the table
    slot and every other member's copy stay."""

    participant_ids: frozenset[str]
    reason: EndReason


@dataclass(frozen=True)
class DocumentCopies:
    """Every copy of that document's disclosure in the session."""

    document_id: str
    reason: EndReason


@dataclass(frozen=True)
class NoSlot:
    """A narrowing that clears no reveal slot."""


SlotScope = EverySlot | ParticipantSlots | DocumentCopies | NoSlot

#: `narrow`'s fail-closed default: every slot.
NARROWED: Final = EverySlot(EndReason.NARROWED)

#: Which reasons each narrowing that names a partial scope may use. A test pins
#: the pairs, so that a Remove cannot record `gm_stop` or a Stop `reconciled`.
PARTICIPANT_SCOPE_REASONS: Final = frozenset(
    {EndReason.PARTICIPANT_REMOVED, EndReason.RECONCILED}
)
DOCUMENT_SCOPE_REASONS: Final = frozenset(
    {
        EndReason.GM_STOP,
        EndReason.CHARACTER_UNLINKED,
        EndReason.DOCUMENT_ARCHIVED,
        EndReason.DOCUMENT_DELETED,
    }
)


@dataclass(frozen=True)
class TableAudience:
    """The table slot: what every seat, the owner's player view and every live
    screen of the session see."""


@dataclass(frozen=True)
class ParticipantsAudience:
    """Named participants of the campaign, 1 to 100 distinct ids."""

    participant_ids: frozenset[str]


@dataclass(frozen=True)
class EveryoneSeated:
    """Every participant whose seat is accepted and GM-confirmed at the moment
    of the Confirm, expanded by the server (TA-5). A seat confirmed later does
    not join a display already made."""


Audience = TableAudience | ParticipantsAudience | EveryoneSeated


@dataclass(frozen=True)
class TableTarget:
    """A write that fills the table slot."""


@dataclass(frozen=True)
class ParticipantTargets:
    """A write that fills these participants' slots; never empty."""

    participant_ids: frozenset[str]


Targets = TableTarget | ParticipantTargets

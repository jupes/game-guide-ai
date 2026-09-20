"""The append-only audit ledger (1kg.2.1).

SEC-38 names the decisions the Workbench has to be able to account for
afterwards. This module is the only way to write one, and there is deliberately
**no way to change or remove one**: no update, no delete, no truncate, in this
module or in any SQL it runs. A ledger that can be edited answers no question
worth asking, and `service/tests/test_audit_log.py` asserts the absence rather
than trusting it.

**The action set is closed** (`AuditAction`). Adding a member is a code change a
reviewer sees, not a string a caller invents, so the ledger cannot quietly grow
a vocabulary nobody agreed to. Reveal's three actions and the export ones are
not here: ED-18(a) makes the table shared, and those belong to `1kg.7.1` and
`1kg.5.2`, which add their own members without a migration. Nor is there a
writer in this bead for the sixteen that are here — their callers are
`1kg.2.2`'s and `1kg.2.3`'s routes. The reason is ownership, not use.

**A row carries identifiers, never content** (SEC-20, ED-26) — and no hash of
any content either: ED-26 is explicit that no value derived from field text may
outlive the text, and a digest of a brief outlives it while still answering
"was it this one?" to anyone holding a guess.

**What actually enforces that, and what it cannot do.** `0005_audit_events.sql`
bounds lengths and two closed vocabularies; it types `actor_ref`, `object_ref`
and `campaign_id_tombstone` as free `TEXT` with no CHECK at all. So the writer
is the enforcement, and it is a **shape** rule: every string a caller supplies
must be an identifier — letters, digits and `_ . : -` — with no space and no
other punctuation. `as_identifier` applies it to every one of them, each against
its own column's bound: each `detail` value and each `detail` key,
`actor_ref`, `object_ref`, `campaign_id_tombstone` (64, `OpaqueId`'s ceiling),
`object_kind` (40) and `reason_code` (60). A row is only as content-free as its
least-checked field, so no string field is exempt.

A shape rule refuses a sentence, a brief, a filename and any multi-word name.
It **cannot** tell a one-word alias from an identifier: `{"alias": "Rook"}` is a
well-formed identifier and passes. What keeps an alias out of a row is that no
caller puts one there — the callers are `1kg.2.2`'s and `1kg.2.3`'s routes, and
they pass minted ids — and what this rule guarantees is that the failure cannot
be a silent one of degree: nothing that reads as text gets in. A caller that
must record which seat something happened to passes `object_ref`, the
participant's minted id.

This is deliberately stricter than
`service/jobs.check_payload`, which admits any short string: a job payload is
read by this service and deleted, while an audit row is retained past the
deletion of everything it describes.

**Rows outlive their campaign.** `campaign_id_tombstone` has no foreign key, so
deleting a campaign — which SEC-36 makes take everything else with it — leaves
the ledger legible and attributable (ED-26, ED-18(a)).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from .campaign_store import fake, now_or, pg
from .db import InMemoryTransaction, UnitOfWork

DetailValue = str | int | bool | None

#: Identifiers, field keys, numbers and booleans — requirement 6's list, and
#: nothing that could be a sentence. 64 characters is `OpaqueId`'s ceiling in
#: `docs/workbench-wire-contract.md`, so a minted id fits and free text does not.
DETAIL_MAX_KEYS = 20
DETAIL_KEY_MAX_CHARS = 40
DETAIL_VALUE_MAX_CHARS = 64

REASON_CODE_MAX_CHARS = 60

#: What a string in an audit row may look like. No space, so no prose; no
#: punctuation beyond what an identifier, a dotted action or a field key needs.
#: Applied to EVERY string a caller supplies — values, keys, references, the
#: object kind and the reason code — because a row is only as content-free as
#: its least-checked field.
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]+$")


def as_identifier(what: str, value: str, limit: int) -> str:
    """`value` if it is an identifier of at most `limit` characters, else a
    refusal that names the FIELD and never the value."""
    if not value or len(value) > limit or IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{what} is an identifier of at most {limit} characters, never text")
    return value


class AuditAction(str, Enum):
    """Every decision this bead's schema knows how to record (SEC-38).

    Closed on purpose: a caller cannot invent one, so the ledger's vocabulary is
    something a reviewer agreed to. A later bead adds its own members here — the
    column is TEXT, so no migration is needed for that.
    """

    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    SESSION_EXPIRED = "session.expired"
    SESSION_ROTATED = "session.rotated"
    PARTICIPANT_ADDED = "participant.added"
    PARTICIPANT_REMOVED = "participant.removed"
    PARTICIPANT_LINKED = "participant.linked"
    PARTICIPANT_UNLINKED = "participant.unlinked"
    CODE_ISSUED = "code.issued"
    CODE_CONSUMED = "code.consumed"
    DEVICE_REPLACED = "device.replaced"
    DEVICE_RESET = "device.reset"
    CAMPAIGN_ARCHIVED = "campaign.archived"
    CAMPAIGN_RESTORED = "campaign.restored"
    CAMPAIGN_DELETED = "campaign.deleted"
    JOIN_BURST_REFUSED = "join.burst_refused"


class ActorKind(str, Enum):
    """Who acted, as `0005_audit_events.sql`'s CHECK has it."""

    GM = "gm"
    PARTICIPANT = "participant"
    GUEST = "guest"
    SYSTEM = "system"


class Decision(str, Enum):
    ALLOWED = "allowed"
    REFUSED = "refused"


def check_ref(name: str, value: str | None) -> str | None:
    """A reference in an audit row is an identifier, or nothing.

    The refusal names the FIELD and never the value: the value is exactly the
    thing that must not reach a log line, and a validator that quoted it back
    would be the leak it exists to prevent.
    """
    if value is None:
        return None
    return as_identifier(f"an audit row's {name}", value, DETAIL_VALUE_MAX_CHARS)


def check_detail(detail: Mapping[str, DetailValue] | None) -> dict[str, DetailValue]:
    """Identifiers, field keys, numbers and booleans — nothing else.

    A string value must look like an identifier, not merely be short: an alias
    has a space in it, and so does every sentence, so neither can be smuggled in
    as a 60-character "code". A refusal names the offending KEY and never its
    value.
    """
    checked = dict(detail or {})
    if len(checked) > DETAIL_MAX_KEYS:
        raise ValueError(f"an audit detail has at most {DETAIL_MAX_KEYS} keys")
    for key, value in checked.items():
        if not isinstance(key, str):
            raise ValueError("an audit detail key is a string")
        # The KEY too: a field key is an identifier, and a caller that put a
        # sentence there would have written content into the row just the same.
        as_identifier("an audit detail key", key, DETAIL_KEY_MAX_CHARS)
        if not isinstance(value, str | int | bool | type(None)):
            raise ValueError(f"audit detail '{key}' must be a string, a whole number, a boolean or null")
        if isinstance(value, str):
            as_identifier(f"audit detail '{key}'", value, DETAIL_VALUE_MAX_CHARS)
    return checked


def check_reason_code(reason_code: str | None) -> str | None:
    """A code, not a sentence: `not_eligible`, never "Rook may not see Wren's
    passive perception" — which is why the length bound is not enough on its own."""
    if reason_code is None:
        return None
    return as_identifier("a reason code", reason_code, REASON_CODE_MAX_CHARS)


@dataclass(frozen=True)
class AuditEvent:
    """One recorded decision. `detail` is hidden from `repr()` — it is validated
    to hold identifiers only, but a record that prints a caller's dictionary is
    one mistake away from printing whatever a caller put there."""

    id: int
    campaign_id_tombstone: str
    actor_kind: str
    action: str
    object_kind: str
    decision: str
    created_at: datetime
    actor_ref: str | None = None
    object_ref: str | None = None
    reason_code: str | None = None
    authz_revision: int | None = None
    detail: Mapping[str, DetailValue] = field(repr=False, default_factory=dict)


class AuditLog(Protocol):
    """One writer, and the reads an operator or a later bead needs. There is no
    update and no delete: see the module docstring."""

    def append(
        self,
        unit: UnitOfWork,
        *,
        campaign_id: str,
        actor_kind: ActorKind,
        action: AuditAction,
        object_kind: str,
        decision: Decision,
        actor_ref: str | None = None,
        object_ref: str | None = None,
        reason_code: str | None = None,
        authz_revision: int | None = None,
        detail: Mapping[str, DetailValue] | None = None,
        now: datetime | None = None,
    ) -> AuditEvent:
        """Record a decision inside `unit`'s transaction, so that it commits with
        whatever it describes and rolls back with it too."""
        ...  # pragma: no cover - structural type

    def for_campaign(self, unit: UnitOfWork, campaign_id: str) -> list[AuditEvent]:
        """That campaign's ledger, oldest first — including rows whose campaign
        has since been deleted, which is the point of the tombstone."""
        ...  # pragma: no cover - structural type


OBJECT_KIND_MAX_CHARS = 40


def _check_object_kind(object_kind: str) -> str:
    """`participant`, `table_session`, `enrolment_code` — a kind, not a name."""
    return as_identifier("an object kind", object_kind, OBJECT_KIND_MAX_CHARS)


_COLUMNS = (
    "id, campaign_id_tombstone, actor_kind, action, object_kind, decision, created_at, "
    "actor_ref, object_ref, reason_code, authz_revision, detail"
)


def _event(row: tuple) -> AuditEvent:
    return AuditEvent(
        int(row[0]), row[1], row[2], row[3], row[4], row[5], row[6],
        row[7], row[8], row[9], None if row[10] is None else int(row[10]), row[11],
    )


class PostgresAuditLog:
    """`audit.events` (migration 0005). One INSERT and one SELECT — the module
    runs no other statement against this table, which is what "append-only"
    means here."""

    def append(
        self,
        unit: UnitOfWork,
        *,
        campaign_id: str,
        actor_kind: ActorKind,
        action: AuditAction,
        object_kind: str,
        decision: Decision,
        actor_ref: str | None = None,
        object_ref: str | None = None,
        reason_code: str | None = None,
        authz_revision: int | None = None,
        detail: Mapping[str, DetailValue] | None = None,
        now: datetime | None = None,
    ) -> AuditEvent:
        row = pg(unit).conn.execute(
            f"INSERT INTO audit.events (campaign_id_tombstone, actor_kind, action, object_kind, "
            f"decision, actor_ref, object_ref, reason_code, authz_revision, detail, created_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) RETURNING {_COLUMNS}",
            (
                check_ref("campaign id", campaign_id),
                ActorKind(actor_kind).value,
                AuditAction(action).value,
                _check_object_kind(object_kind),
                Decision(decision).value,
                check_ref("actor reference", actor_ref),
                check_ref("object reference", object_ref),
                check_reason_code(reason_code),
                authz_revision,
                json.dumps(check_detail(detail)),
                now_or(now),
            ),
        ).fetchone()
        return _event(row)

    def for_campaign(self, unit: UnitOfWork, campaign_id: str) -> list[AuditEvent]:
        rows = pg(unit).conn.execute(
            f"SELECT {_COLUMNS} FROM audit.events "
            f"WHERE campaign_id_tombstone = %s ORDER BY created_at, id",
            (campaign_id,),
        ).fetchall()
        return [_event(row) for row in rows]


class InMemoryAuditLog:
    """The twin, with the same validation and the same commit-time visibility:
    a decision recorded by a transaction that rolls back was never made."""

    def __init__(self) -> None:
        self._rows: list[AuditEvent] = []
        #: Rows this transaction has written and nobody else may see yet.
        self._staged: dict[int, list[AuditEvent]] = {}
        self._next_id = 1

    def _mine(self, unit: InMemoryTransaction) -> list[AuditEvent]:
        key = id(unit)
        if key not in self._staged:
            self._staged[key] = []

            def publish() -> None:
                self._rows.extend(self._staged.pop(key, []))

            def discard() -> None:
                self._staged.pop(key, None)

            unit.on_publish(publish)
            unit.on_rollback(discard)
        return self._staged[key]

    def append(
        self,
        unit: UnitOfWork,
        *,
        campaign_id: str,
        actor_kind: ActorKind,
        action: AuditAction,
        object_kind: str,
        decision: Decision,
        actor_ref: str | None = None,
        object_ref: str | None = None,
        reason_code: str | None = None,
        authz_revision: int | None = None,
        detail: Mapping[str, DetailValue] | None = None,
        now: datetime | None = None,
    ) -> AuditEvent:
        twin = fake(unit)
        event = AuditEvent(
            id=self._next_id,
            campaign_id_tombstone=check_ref("campaign id", campaign_id) or "",
            actor_kind=ActorKind(actor_kind).value,
            action=AuditAction(action).value,
            object_kind=_check_object_kind(object_kind),
            decision=Decision(decision).value,
            created_at=now_or(now),
            actor_ref=check_ref("actor reference", actor_ref),
            object_ref=check_ref("object reference", object_ref),
            reason_code=check_reason_code(reason_code),
            authz_revision=authz_revision,
            detail=check_detail(detail),
        )
        self._next_id += 1
        self._mine(twin).append(event)
        return event

    def for_campaign(self, unit: UnitOfWork, campaign_id: str) -> list[AuditEvent]:
        twin = fake(unit)
        visible = [*self._rows, *self._staged.get(id(twin), [])]
        return sorted(
            (e for e in visible if e.campaign_id_tombstone == campaign_id),
            key=lambda e: (e.created_at, e.id),
        )

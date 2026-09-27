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
writer in this bead for the fourteen that are here — their callers are
`1kg.2.2`'s and `1kg.2.3`'s routes. The reason is ownership, not use.

**A row carries identifiers, never content** (SEC-20, ED-26) — and no hash of
any content either: ED-26 is explicit that no value derived from field text may
outlive the text, and a digest of a brief outlives it while still answering
"was it this one?" to anyone holding a guess.

**`detail` is closed and per action**, as ED-18(a) words it: "a closed,
per-action `detail` of ids, codes and keys". `ACTION_DETAIL` says, for each
action, exactly which keys a row of that action may carry and what each one is —
a minted id of a named prefix, one of a closed set of codes, a Workbench field
key (ED-2), a bounded list of them (which is how `1kg.7.1` records a reveal's
mask), a whole number or a boolean. A key the registry does not list is refused,
so an alias, a title, a filename or a sentence cannot be recorded whatever it is
called: `alias` is a key of no action and `"Rook"` is a value of no kind.

**What is closed, and what is only bounded.** Two kinds are open by
construction, and a caller has to know which:

* a `MintedId` accepts **any well-formed body** behind its prefix, because the
  tombstone has no foreign key (ED-26) and nothing here can ask whether that row
  exists. `prt_The-Hooded-Stranger-is-Ondrey` is a well-formed body. What the
  kind does close is the *prefix* and the *length*, so the id of another kind of
  thing, or a name that is not id-shaped at all, is still refused.
* `Shape.FIELD_KEY(S)` is a **shape**, not a vocabulary: the keys a document may
  have belong to its type, and this module does not know the types. A caller
  that records a reveal's mask must check the keys against the document type's
  declared keys before it gets here — the bound on the list is a bound, not a
  closure.

Everything else is a closed set: the action, the actor kind, the object kind,
the decision, a `OneOf`'s codes, and the reason codes each action declares. No
kind holds the client-minted command id ED-18(a) wants on a reveal row, and none
is added here: how that row records it is `1kg.7.1`'s decision.

The same closure applies to the columns beside `detail`: `campaign_id_tombstone`
is a minted `cmp_` identifier, `actor_ref` and `object_ref` are minted
identifiers or the GM's numeric user id, `object_kind` is one of `ObjectKind`,
`reason_code` is one of the codes its own action declares, and `authz_revision`
is a whole number that is never negative — the bound `0005_audit_events.sql`
also carries, so the twin and the database agree. Every refusal names the
**field** and never the value: a validator that quoted the offending value back
would be the leak it exists to prevent, which is also why the enum conversions
are wrapped rather than left to raise Python's own `ValueError` with the value
in it.

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

from . import campaign_identity as ident
from .campaign_store import fake, now_or, pg
from .db import InMemoryTransaction, UnitOfWork

DetailValue = str | int | bool | list[str] | None

#: ED-2's flat field key: the unit of eligibility, of a reveal mask, of
#: field-level concurrency and of a changed-field list. There are no nested
#: paths, so this is the whole vocabulary of keys the Workbench has.
FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
#: `auth.users.id`, as text. The GM is the one actor that is a row in another
#: schema rather than a minted identifier (AUD-1).
USER_ID = re.compile(r"^[1-9][0-9]{0,18}$")

#: A mask is at most the document type's fields; forty is far past any of them
#: and still a bound, which an unbounded list in a retained row would not be.
DETAIL_MAX_FIELD_KEYS = 40
#: What a whole number in a row may be: a count, a generation or a revision —
#: never negative, and never wider than the bigint a column would hold it in.
WHOLE_NUMBER_MAX = 2**63 - 1
#: No action's registry entry may grow past this without a second look.
DETAIL_MAX_KEYS = 20


class AuditAction(str, Enum):
    """Every decision this bead's schema knows how to record (SEC-38).

    Closed on purpose: a caller cannot invent one, so the ledger's vocabulary is
    something a reviewer agreed to. A later bead adds its own members here — the
    column is TEXT, so no migration is needed for that — together with the
    `ACTION_DETAIL` entry that says what such a row may carry.
    """

    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    SESSION_EXPIRED = "session.expired"
    SESSION_ROTATED = "session.rotated"
    PARTICIPANT_ADDED = "participant.added"
    PARTICIPANT_REMOVED = "participant.removed"
    PARTICIPANT_LINKED = "participant.linked"
    PARTICIPANT_UNLINKED = "participant.unlinked"
    #: The GM offered a seat to an account (actor `gm`). Bead `fma`: the
    #: enrolment code and the device credential are retired (D-1, D-4), and
    #: their four actions with them.
    SEAT_OFFERED = "seat.offered"
    #: The account accepted the seat offered to it (actor `participant`). There
    #: is no `seat.removed`: `participant.removed` records a seat's removal.
    SEAT_ACCEPTED = "seat.accepted"
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


class ObjectKind(str, Enum):
    """What the decision was **about** — one of the three things the fourteen
    actions act on, and nothing else.

    Closed for the same reason `AuditAction` is, and for one more: a lower-case
    key is a *shape*, so `rook` and `the_hooded_stranger_is_ondrey` both passed
    the rule this replaced, in a row that outlives its campaign by design
    (ED-26). The column stays TEXT — a later bead adds a member here in the same
    change that adds its action, without a migration.
    """

    CAMPAIGN = "campaign"
    TABLE_SESSION = "table_session"
    PARTICIPANT = "participant"


class Decision(str, Enum):
    ALLOWED = "allowed"
    REFUSED = "refused"


# ── What a detail value may be ───────────────────────────────────────────────


@dataclass(frozen=True)
class MintedId:
    """A minted identifier of one prefix (SEC-4). Not "an identifier-shaped
    string": the prefix and the body length are both checked, so the id of a
    different kind of thing is refused too."""

    prefix: str


@dataclass(frozen=True)
class OneOf:
    """One of a closed set of codes agreed in this file. The set is the
    vocabulary, so a refusal may name it: it contains nothing a caller wrote."""

    codes: tuple[str, ...]


class Shape(Enum):
    """The kinds that need no parameter."""

    FIELD_KEY = "a Workbench field key"
    FIELD_KEYS = "a list of Workbench field keys"
    WHOLE_NUMBER = "a whole number"
    FLAG = "a boolean"


Kind = MintedId | OneOf | Shape


def accepts(kind: Kind, value: DetailValue) -> bool:
    """Whether `value` is of `kind`. Pure, and public because a later bead adds
    its own `ACTION_DETAIL` entry and has to be able to test it — `1kg.7.1`'s
    reveal rows carry `Shape.FIELD_KEYS`, the mask ED-18(a) asks for, and this
    function already answers for it. What that bead still has to decide is what
    the rest of a reveal row holds: **no kind here fits the command id** ED-18(a)
    puts on one, because the wire contract has the client mint it and no prefix
    of this schema's registry is its. And `Shape.FIELD_KEYS` checks the shape of
    a mask's keys, never that they are keys of the document type in question —
    that comparison is the recording caller's, and there is nowhere in an audit
    row to do it.

    `None` is accepted for every kind: it is how a declared key says "this row
    has no such value", and a JSON null carries nothing.
    """
    if value is None:
        return True
    if isinstance(kind, MintedId):
        return isinstance(value, str) and ident.is_id(kind.prefix, value)
    if isinstance(kind, OneOf):
        return isinstance(value, str) and value in kind.codes
    if kind is Shape.FIELD_KEY:
        return isinstance(value, str) and FIELD_KEY.fullmatch(value) is not None
    if kind is Shape.FIELD_KEYS:
        return (
            isinstance(value, list)
            and len(value) <= DETAIL_MAX_FIELD_KEYS
            and all(isinstance(key, str) and FIELD_KEY.fullmatch(key) for key in value)
        )
    if kind is Shape.WHOLE_NUMBER:
        # bool is an int in Python and is not a number in an audit row; a count,
        # a generation and a revision are never negative; and the widest column
        # that could hold one is a bigint, so an integer of any size is not one.
        return (
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= WHOLE_NUMBER_MAX
        )
    return isinstance(value, bool)


def _describes(kind: Kind) -> str:
    """How a refusal names the kind. Every branch is this file's own vocabulary,
    so none of it can repeat what a caller supplied."""
    if isinstance(kind, MintedId):
        return f"a minted {kind.prefix} identifier"
    if isinstance(kind, OneOf):
        return f"one of {', '.join(kind.codes)}"
    return kind.value


# ── The registry: what a row of each action may carry ────────────────────────

_CAMPAIGN = MintedId(ident.CAMPAIGN)
_PARTICIPANT = MintedId(ident.PARTICIPANT)
_SESSION = MintedId(ident.TABLE_SESSION)
#: The character sheet a link or unlink row names (SEC-38). An id, never a
#: title: a document's name is field text, and nothing derived from field text
#: may outlive it in a ledger that survives the campaign (ED-26).
_DOCUMENT = MintedId(ident.DOCUMENT)

#: SEC-10 bounds a generation two ways — 24 credentials, and 60 joins in ten
#: minutes. Which one a refused join hit is a closed code, not a sentence.
JOIN_BOUND = OneOf(("credentials_per_generation", "joins_per_window"))

_SESSION_CLOSED: dict[str, Kind] = {
    "session_id": _SESSION,
    "generation": Shape.WHOLE_NUMBER,
    "credentials_revoked": Shape.WHOLE_NUMBER,
}

#: The closed, per-action `detail` of ED-18(a). A bead that adds an action adds
#: its entry here in the same change, and `test_audit_log.py` requires the two
#: to stay in step — an action with no entry can record nothing at all.
ACTION_DETAIL: dict[AuditAction, dict[str, Kind]] = {
    AuditAction.SESSION_STARTED: {"session_id": _SESSION, "generation": Shape.WHOLE_NUMBER},
    AuditAction.SESSION_ENDED: _SESSION_CLOSED,
    AuditAction.SESSION_EXPIRED: _SESSION_CLOSED,
    AuditAction.SESSION_ROTATED: {**_SESSION_CLOSED, "personal_links_reset": Shape.FLAG},
    AuditAction.PARTICIPANT_ADDED: {"participant_id": _PARTICIPANT},
    AuditAction.PARTICIPANT_REMOVED: {
        "participant_id": _PARTICIPANT,
        "codes_revoked": Shape.WHOLE_NUMBER,
        "devices_revoked": Shape.WHOLE_NUMBER,
    },
    AuditAction.PARTICIPANT_LINKED: {"participant_id": _PARTICIPANT, "document_id": _DOCUMENT},
    AuditAction.PARTICIPANT_UNLINKED: {"participant_id": _PARTICIPANT, "document_id": _DOCUMENT},
    # The seat, and never the account: "who was seated" is answered by the
    # participant row, and a user id is personal data the ledger does not need.
    AuditAction.SEAT_OFFERED: {"participant_id": _PARTICIPANT},
    AuditAction.SEAT_ACCEPTED: {"participant_id": _PARTICIPANT},
    AuditAction.CAMPAIGN_ARCHIVED: {"campaign_id": _CAMPAIGN},
    AuditAction.CAMPAIGN_RESTORED: {"campaign_id": _CAMPAIGN},
    AuditAction.CAMPAIGN_DELETED: {
        "campaign_id": _CAMPAIGN,
        "participants": Shape.WHOLE_NUMBER,
        "sessions": Shape.WHOLE_NUMBER,
    },
    AuditAction.JOIN_BURST_REFUSED: {
        "session_id": _SESSION,
        "generation": Shape.WHOLE_NUMBER,
        "bound": JOIN_BOUND,
    },
}

#: The closed set of reason codes **per action**, beside `ACTION_DETAIL` and
#: keyed by the same closed set. `reason_code` used to be shape-checked like a
#: field key, which made every lower-case word a reason — `rook` among them.
#: An action with no reason to record has the empty set, which is an answer
#: rather than an omission, and `None` is legal for every action: most rows are
#: an allowed decision that needs no explaining. A bead that adds a refusal adds
#: its code here in the change a reviewer reads, as it does for the detail.
ACTION_REASONS: dict[AuditAction, frozenset[str]] = {
    AuditAction.PARTICIPANT_REMOVED: frozenset({"gm_removed"}),
    AuditAction.JOIN_BURST_REFUSED: frozenset(JOIN_BOUND.codes),
    **{
        action: frozenset()
        for action in AuditAction
        if action not in (AuditAction.PARTICIPANT_REMOVED, AuditAction.JOIN_BURST_REFUSED)
    },
}


# ── The checks every row goes through ────────────────────────────────────────


def check_action(action: AuditAction | str) -> AuditAction:
    """The action, as a member. Wrapped so that the refusal does not repeat the
    value: `AuditAction('the GM called Ana')` raises Python's own message, which
    quotes it, and that message is the one thing in this module that must never
    reach a log line."""
    try:
        return AuditAction(action)
    except ValueError:
        raise ValueError("an audit row's action is not one the ledger knows") from None


def check_actor_kind(actor_kind: ActorKind | str) -> str:
    try:
        return ActorKind(actor_kind).value
    except ValueError:
        raise ValueError("an audit row's actor kind is not one the ledger knows") from None


def check_decision(decision: Decision | str) -> str:
    try:
        return Decision(decision).value
    except ValueError:
        raise ValueError("an audit row's decision is not one the ledger knows") from None


def check_campaign_id(campaign_id: str) -> str:
    """The tombstone is a minted campaign identifier and nothing else. The
    column has no foreign key (ED-26), so this is the only thing standing
    between a row that names a campaign and a row that names `Rook`."""
    if not isinstance(campaign_id, str) or not ident.is_id(ident.CAMPAIGN, campaign_id):
        raise ValueError("an audit row's campaign id is a minted campaign identifier, never text")
    return campaign_id


def check_ref(name: str, value: str | None) -> str | None:
    """A reference in an audit row is a minted identifier or the GM's numeric
    user id, or nothing.

    The refusal names the FIELD and never the value: the value is exactly the
    thing that must not reach a log line, and a validator that quoted it back
    would be the leak it exists to prevent.
    """
    if value is None:
        return None
    minted = isinstance(value, str) and any(ident.is_id(p, value) for p in ident.PREFIXES)
    if minted or (isinstance(value, str) and USER_ID.fullmatch(value) is not None):
        return value
    raise ValueError(f"an audit row's {name} is a minted identifier or a user id, never text")


def check_field_key(what: str, value: str) -> str:
    """ED-2's flat key: `passive_perception`, `motives` — a key of a Workbench
    field, never a name and never a sentence.

    A **shape**, which is all a field key can be: the set of keys belongs to a
    document type, and this module does not know the types. That is why neither
    the object kind nor the reason code goes through it any more — for those two
    a closed vocabulary exists, and a shape would admit `rook`.
    """
    if not isinstance(value, str) or FIELD_KEY.fullmatch(value) is None:
        raise ValueError(f"{what} is a lowercase key of at most 40 characters, never text")
    return value


def check_object_kind(object_kind: ObjectKind | str) -> str:
    """What the row is about, as a member. Wrapped like `check_action` so that
    the refusal does not repeat the value — `ObjectKind('the_hooded_stranger')`
    raises Python's own message, which quotes it."""
    try:
        return ObjectKind(object_kind).value
    except ValueError:
        raise ValueError("an audit row's object kind is one the ledger knows, never text") from None


def check_reason_code(action: AuditAction, reason_code: str | None) -> str | None:
    """One of the codes **this action** declares, or nothing.

    Per action rather than one list, for the reason ED-18(a) gives for `detail`:
    the vocabulary that makes sense of a refused join is not the one that makes
    sense of a removed seat, and a single list would grow until it was a shape
    again. The refusal may name the set — it is this file's own — but never the
    value it was given.
    """
    if reason_code is None:
        return None
    declared = ACTION_REASONS[action]
    if not isinstance(reason_code, str) or reason_code not in declared:
        carries = ", ".join(sorted(declared)) or "no reason code at all"
        raise ValueError(f"an audit row for {action.value} carries {carries}, never text")
    return reason_code


def check_authz_revision(authz_revision: int | None) -> int | None:
    """The bound `0005_audit_events.sql` carries, applied in both worlds so that
    the twin cannot accept a row the database would refuse."""
    if authz_revision is None:
        return None
    if isinstance(authz_revision, bool) or not isinstance(authz_revision, int):
        raise ValueError("an audit row's authorisation revision is a whole number")
    if authz_revision < 0:
        raise ValueError("an audit row's authorisation revision is never negative")
    return authz_revision


def check_detail(
    action: AuditAction, detail: Mapping[str, DetailValue] | None
) -> dict[str, DetailValue]:
    """The closed, per-action detail of ED-18(a).

    A key the action's registry entry does not list is refused, and the refusal
    does **not** repeat it: a key carries content just as well as a value does
    (`{"Rook saw the wrong card": True}`), so it is named only by the vocabulary
    it was measured against, which is this file's own.
    """
    schema = ACTION_DETAIL[action]
    checked = dict(detail or {})
    for key, value in checked.items():
        kind = schema.get(key) if isinstance(key, str) else None
        if kind is None:
            allowed = ", ".join(sorted(schema)) or "nothing"
            raise ValueError(
                f"an audit row for {action.value} carries no such detail key; it carries {allowed}"
            )
        if not accepts(kind, value):
            raise ValueError(f"audit detail '{key}' is {_describes(kind)}, never text")
    return checked


@dataclass(frozen=True)
class AuditEvent:
    """One recorded decision. `detail` is hidden from `repr()` — it is validated
    to hold identifiers, codes and keys only, but a record that prints a
    caller's dictionary is one mistake away from printing whatever a caller put
    there."""

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
        object_kind: ObjectKind | str,
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


@dataclass(frozen=True)
class _Row:
    """Every column of a row, checked. Built once and used by both writers, so
    the twin cannot validate less than the database does."""

    campaign_id: str
    actor_kind: str
    action: str
    object_kind: str
    decision: str
    actor_ref: str | None
    object_ref: str | None
    reason_code: str | None
    authz_revision: int | None
    detail: dict[str, DetailValue]
    created_at: datetime


def _checked(
    *,
    campaign_id: str,
    actor_kind: ActorKind | str,
    action: AuditAction | str,
    object_kind: ObjectKind | str,
    decision: Decision | str,
    actor_ref: str | None,
    object_ref: str | None,
    reason_code: str | None,
    authz_revision: int | None,
    detail: Mapping[str, DetailValue] | None,
    now: datetime | None,
) -> _Row:
    chosen = check_action(action)
    return _Row(
        campaign_id=check_campaign_id(campaign_id),
        actor_kind=check_actor_kind(actor_kind),
        action=chosen.value,
        object_kind=check_object_kind(object_kind),
        decision=check_decision(decision),
        actor_ref=check_ref("actor reference", actor_ref),
        object_ref=check_ref("object reference", object_ref),
        reason_code=check_reason_code(chosen, reason_code),
        authz_revision=check_authz_revision(authz_revision),
        detail=check_detail(chosen, detail),
        created_at=now_or(now),
    )


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
        object_kind: ObjectKind | str,
        decision: Decision,
        actor_ref: str | None = None,
        object_ref: str | None = None,
        reason_code: str | None = None,
        authz_revision: int | None = None,
        detail: Mapping[str, DetailValue] | None = None,
        now: datetime | None = None,
    ) -> AuditEvent:
        checked = _checked(
            campaign_id=campaign_id,
            actor_kind=actor_kind,
            action=action,
            object_kind=object_kind,
            decision=decision,
            actor_ref=actor_ref,
            object_ref=object_ref,
            reason_code=reason_code,
            authz_revision=authz_revision,
            detail=detail,
            now=now,
        )
        row = pg(unit).conn.execute(
            f"INSERT INTO audit.events (campaign_id_tombstone, actor_kind, action, object_kind, "
            f"decision, actor_ref, object_ref, reason_code, authz_revision, detail, created_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) RETURNING {_COLUMNS}",
            (
                checked.campaign_id,
                checked.actor_kind,
                checked.action,
                checked.object_kind,
                checked.decision,
                checked.actor_ref,
                checked.object_ref,
                checked.reason_code,
                checked.authz_revision,
                json.dumps(checked.detail),
                checked.created_at,
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
        object_kind: ObjectKind | str,
        decision: Decision,
        actor_ref: str | None = None,
        object_ref: str | None = None,
        reason_code: str | None = None,
        authz_revision: int | None = None,
        detail: Mapping[str, DetailValue] | None = None,
        now: datetime | None = None,
    ) -> AuditEvent:
        twin = fake(unit)
        checked = _checked(
            campaign_id=campaign_id,
            actor_kind=actor_kind,
            action=action,
            object_kind=object_kind,
            decision=decision,
            actor_ref=actor_ref,
            object_ref=object_ref,
            reason_code=reason_code,
            authz_revision=authz_revision,
            detail=detail,
            now=now,
        )
        event = AuditEvent(
            id=self._next_id,
            campaign_id_tombstone=checked.campaign_id,
            actor_kind=checked.actor_kind,
            action=checked.action,
            object_kind=checked.object_kind,
            decision=checked.decision,
            created_at=checked.created_at,
            actor_ref=checked.actor_ref,
            object_ref=checked.object_ref,
            reason_code=checked.reason_code,
            authz_revision=checked.authz_revision,
            detail=checked.detail,
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

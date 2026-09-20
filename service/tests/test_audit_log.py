"""The append-only audit ledger (1kg.2.1) — without a database.

Three things are asserted here, and they are the three that make the ledger
worth keeping: that its vocabulary is closed, that a row cannot carry content,
and that the module offers no way to change or remove what it has written.

That the rows survive the deletion of their campaign is the database's answer,
and is in `tests/test_migrations_db.py`.
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import UTC, datetime

import pytest

from service import audit_log
from service.audit_log import (
    ActorKind,
    AuditAction,
    AuditEvent,
    Decision,
    InMemoryAuditLog,
    check_detail,
    check_reason_code,
)
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase
from service.participant_store import InMemoryParticipantStore
from service.table_session_store import InMemoryTableSessionStore

CAMPAIGN = "cmp_" + "a" * 22
PARTICIPANT = "prt_" + "a" * 22


def _record(log: InMemoryAuditLog, unit: object, **changes: object) -> AuditEvent:
    fields: dict[str, object] = {
        "campaign_id": CAMPAIGN,
        "actor_kind": ActorKind.GM,
        "action": AuditAction.PARTICIPANT_ADDED,
        "object_kind": "participant",
        "decision": Decision.ALLOWED,
        "object_ref": PARTICIPANT,
    }
    fields.update(changes)
    return log.append(unit, **fields)  # type: ignore[arg-type]


# ── Behaviour 22 — a closed vocabulary and a shape with no room for content ──


def test_a_decision_is_recorded_with_its_actor_object_and_outcome():
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        event = _record(log, unit, authz_revision=4, detail={"code_id": "enc_x", "seat": 2})
        assert event.action == "participant.added"
        assert event.actor_kind == "gm" and event.decision == "allowed"
        assert event.object_ref == PARTICIPANT and event.authz_revision == 4
        assert event.detail == {"code_id": "enc_x", "seat": 2}
        assert log.for_campaign(unit, CAMPAIGN) == [event], "its own transaction sees it"
    with db.transaction() as unit:
        assert log.for_campaign(unit, CAMPAIGN) == [event]


def test_a_decision_a_rolled_back_transaction_made_was_never_made():
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with pytest.raises(RuntimeError, match="boom"):
        with db.transaction() as unit:
            _record(log, unit)
            raise RuntimeError("boom")
    with db.transaction() as unit:
        assert log.for_campaign(unit, CAMPAIGN) == []


def test_the_action_set_is_closed_and_a_caller_cannot_invent_one():
    """A string the ledger has never heard of is a vocabulary nobody agreed to."""
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        with pytest.raises(ValueError, match="not a valid AuditAction"):
            _record(log, unit, action="participant.vanished")
        with pytest.raises(ValueError, match="not a valid ActorKind"):
            _record(log, unit, actor_kind="robot")
        with pytest.raises(ValueError, match="not a valid Decision"):
            _record(log, unit, decision="maybe")


def test_the_sixteen_actions_sec38_names_are_the_ones_that_ship():
    """Reveal's three and the export ones are not here: ED-18(a) makes the table
    shared, and they belong to the beads that will write them (1kg.7.1, 1kg.5.2),
    which add their own members without a migration."""
    assert {a.value for a in AuditAction} == {
        "session.started", "session.ended", "session.expired", "session.rotated",
        "participant.added", "participant.removed", "participant.linked",
        "participant.unlinked", "code.issued", "code.consumed", "device.replaced",
        "device.reset", "campaign.archived", "campaign.restored", "campaign.deleted",
        "join.burst_refused",
    }
    assert not [a for a in AuditAction if a.value.startswith(("reveal.", "export."))]


@pytest.mark.parametrize(
    ("detail", "refusal"),
    [
        pytest.param({"alias": "Wren the Unseen"}, "identifier, never text", id="an-alias"),
        pytest.param({"title": "The Nocturne of Vex"}, "identifier, never text", id="a-title"),
        pytest.param(
            {"why": "the GM stopped the reveal because Rook saw the wrong card"},
            "identifier, never text",
            id="a-sentence",
        ),
        pytest.param({"file": "session notes.pdf"}, "identifier, never text", id="a-filename"),
        pytest.param({"brief": "x" * 65}, "identifier, never text", id="too-long"),
        pytest.param({"slots": {"one": 1}}, "must be a string", id="nested"),
        pytest.param({"seats": [1, 2]}, "must be a string", id="a-list"),
        pytest.param({"at": datetime.now(UTC)}, "must be a string", id="not-a-scalar"),
        pytest.param({"": "x"}, "a short string", id="no-key"),
        pytest.param({"k" * 41: "x"}, "a short string", id="a-key-that-is-a-sentence"),
        pytest.param(dict.fromkeys((f"k{n}" for n in range(21)), 1), "at most 20 keys", id="too-many"),
    ],
)
def test_a_detail_that_could_carry_content_is_refused(detail, refusal):
    """Not merely short — an identifier. An alias has a space in it, and so does
    every sentence, so neither can be smuggled in as a sixty-character "code"."""
    with pytest.raises(ValueError, match=refusal):
        check_detail(detail)


def test_a_refusal_never_repeats_the_value_it_refused():
    """A validator that quoted the offending value back would be the leak it
    exists to prevent."""
    with pytest.raises(ValueError) as refused:
        check_detail({"alias": "Wren the Unseen"})
    assert "Wren" not in str(refused.value)
    assert "alias" in str(refused.value), "it still says which key was wrong"


def test_a_detail_of_identifiers_keys_numbers_and_booleans_is_accepted():
    accepted = {
        "code_id": "enc_dEfG-hIjK_lMnOpQrStU",
        "field": "passive_perception",
        "action": "session.rotated",
        "generation": 2,
        "was_expired": True,
        "reason": None,
    }
    assert check_detail(accepted) == accepted
    assert check_detail(None) == {}


@pytest.mark.parametrize("field", ["actor_ref", "object_ref", "campaign_id"])
def test_a_reference_in_a_row_is_an_identifier_too(field):
    """`0005_audit_events.sql` types actor_ref, object_ref and
    campaign_id_tombstone as free TEXT, so the writer is the only thing
    stopping a caller passing an alias where an id belongs."""
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        with pytest.raises(ValueError, match="identifier, never text"):
            _record(log, unit, **{field: "Wren the Unseen"})
        assert log.for_campaign(unit, CAMPAIGN) == [], "nothing was recorded"


@pytest.mark.parametrize("reason", ["", "r" * 61])
def test_a_reason_is_a_code_not_a_sentence(reason):
    with pytest.raises(ValueError, match="1 to 60 characters"):
        check_reason_code(reason)


# ── Behaviour 23 — there is no way to change or remove a recorded decision ───


def test_the_audit_module_offers_no_update_and_no_delete_path():
    """Asserted rather than trusted. A ledger that can be edited answers no
    question worth asking, so the absence is a property of the module, checked
    the way a reviewer would check it: every public name, and every statement
    the module can run."""
    public = [
        name
        for name, value in vars(audit_log).items()
        if not name.startswith("_") and (inspect.isfunction(value) or inspect.isclass(value))
    ]
    assert not [n for n in public if re.search(r"update|delete|remove|purge|edit", n, re.I)], public

    for writer in (audit_log.PostgresAuditLog, audit_log.InMemoryAuditLog):
        methods = [n for n in dir(writer) if not n.startswith("_")]
        assert sorted(methods) == ["append", "for_campaign"], (writer.__name__, methods)

    source = inspect.getsource(audit_log)
    statements = re.findall(r"\b(INSERT|SELECT|UPDATE|DELETE|TRUNCATE|DROP)\b\s", source)
    assert set(statements) == {"INSERT", "SELECT"}, statements


# ── Behaviour 24 — nothing private reaches a log line or an exception ────────


#: The two kinds of text this bead's records can hold. A CONVERSATION title is
#: the third thing the acceptance criteria name, and it is deliberately absent:
#: migration 0006 adds the column, `service/history.py` is untouched, and no
#: record defined here carries one — so there is none to leak yet, and
#: `1kg.2.4` inherits this sweep when it reads the column.
PRIVATE = {
    "alias": "Wren the Unseen",
    "campaign name": "The Nocturne of Vex",
}


def test_no_alias_title_or_secret_reaches_a_log_line_or_an_exception(caplog):
    """The whole store and audit surface at once. An exception message is a log
    line as soon as anything catches it, and a traceback prints every `repr()`
    on the way out."""
    db = InMemoryDatabase()
    campaigns = InMemoryCampaignStore(db)
    participants = InMemoryParticipantStore(db)
    sessions = InMemoryTableSessionStore(db)
    log = InMemoryAuditLog()
    said: list[str] = []

    with caplog.at_level(logging.DEBUG):
        with db.transaction() as unit:
            campaign = campaigns.create(unit, owner_id=1, name=PRIVATE["campaign name"])
            seat = participants.add(unit, campaign.id, alias=PRIVATE["alias"])
            code, secret = participants.issue_code(unit, seat.id)
            credential, device_secret = participants.issue_device_credential(unit, seat.id)
            session, link = sessions.start(
                unit, campaign.id, gm_user_id=1, expires_at=datetime.now(UTC)
            )
            joined, join_secret = sessions.issue_credential(unit, session.id)
            said += [repr(r) for r in (campaign, seat, code, credential, session, joined)]

            with pytest.raises(ValueError) as refused:
                participants.add(unit, campaign.id, alias="a" * 41)
            said.append(str(refused.value))
            with pytest.raises(Exception) as taken:
                participants.add(unit, campaign.id, alias=PRIVATE["alias"])
            said.append(str(taken.value))

            _record(log, unit, campaign_id=campaign.id, object_ref=seat.id)
            with pytest.raises(ValueError) as long_detail:
                check_detail({"alias": PRIVATE["alias"] * 20})
            said.append(str(long_detail.value))

    spoken = "\n".join([*said, caplog.text])
    for kind, value in PRIVATE.items():
        assert value not in spoken, f"a {kind} reached a message"
    for kind, value in (
        ("enrolment code", secret),
        ("device credential", device_secret),
        ("table link", link),
        ("join credential", join_secret),
    ):
        assert value not in spoken, f"a {kind} reached a message"
    for kind, digest in (
        ("code", code.code_digest),
        ("device", credential.credential_digest),
        ("link", session.link_digest or ""),
        ("join", joined.credential_digest),
    ):
        assert digest not in spoken, f"a {kind} digest reached a message"

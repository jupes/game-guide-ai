"""The append-only audit ledger (1kg.2.1) — without a database.

Four things are asserted here, and they are the four that make the ledger worth
keeping: that its vocabulary is closed, that a row cannot carry content, that
what the Python side refuses is what `0005_audit_events.sql` refuses, and that
the module offers no way to change or remove what it has written.

That the rows survive the deletion of their campaign is the database's answer,
and is in `tests/test_migrations_db.py`. That `PostgresAuditLog` writes them at
all is `tests/test_campaign_db.py`'s shared suite, which runs the same
behaviours against both worlds.
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service import audit_log
from service import campaign_identity as ident
from service.audit_log import (
    ACTION_DETAIL,
    ACTION_REASONS,
    DETAIL_MAX_FIELD_KEYS,
    DETAIL_MAX_KEYS,
    FIELD_KEY,
    ActorKind,
    AuditAction,
    AuditEvent,
    Decision,
    InMemoryAuditLog,
    MintedId,
    ObjectKind,
    OneOf,
    Shape,
    accepts,
    check_detail,
    check_reason_code,
)
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase
from service.participant_store import InMemoryParticipantStore
from service.table_session_store import InMemoryTableSessionStore, no_slots

CAMPAIGN = "cmp_" + "a" * 22
PARTICIPANT = "prt_" + "a" * 22

AUDIT_SQL = (
    Path(__file__).resolve().parents[1] / "sql" / "migrations" / "0005_audit_events.sql"
).read_text(encoding="utf-8")


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
        event = _record(
            log, unit, actor_ref="42", authz_revision=4, detail={"participant_id": PARTICIPANT}
        )
        assert event.action == "participant.added"
        assert event.actor_kind == "gm" and event.decision == "allowed"
        assert event.object_ref == PARTICIPANT and event.authz_revision == 4
        assert event.actor_ref == "42", "the GM is a row in auth.users, not a minted id"
        assert event.detail == {"participant_id": PARTICIPANT}
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
    """A string the ledger has never heard of is a vocabulary nobody agreed to —
    and the refusal must not repeat it, because the invented value is exactly
    the place a caller would have put an alias."""
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        for field, value in (
            ("action", "participant.vanished"),
            ("actor_kind", "the GM called Ana"),
            ("decision", "Rook may not see that"),
        ):
            with pytest.raises(ValueError, match="not one the ledger knows") as refused:
                _record(log, unit, **{field: value})
            assert value not in str(refused.value), "a refusal never repeats what it refused"


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


# ── The closed, per-action detail of ED-18(a) ────────────────────────────────


def test_every_action_says_what_a_row_of_it_may_carry():
    """An action with no registry entry could record anything or nothing, and
    nobody would notice which. Every entry is bounded, and every key of every
    entry is itself a field key (ED-2), so the registry cannot become the place
    free text gets in."""
    assert set(ACTION_DETAIL) == set(AuditAction)
    for action, schema in ACTION_DETAIL.items():
        assert len(schema) <= DETAIL_MAX_KEYS, action
        for key in schema:
            assert FIELD_KEY.fullmatch(key), (action, key)


@pytest.mark.parametrize(
    ("detail", "refusal"),
    [
        # There is no kind that admits a free string, so the canonical secret of
        # ED-20 has nowhere to go — not under a plausible key, not under any.
        pytest.param({"note": "The_Hooded_Stranger_is_Ondrey"}, "no such detail key", id="a-note"),
        pytest.param({"alias": "Rook"}, "no such detail key", id="an-alias"),
        pytest.param({"title": "Dragonfall"}, "no such detail key", id="a-title"),
        pytest.param({"why": "Rook saw the wrong card"}, "no such detail key", id="a-sentence"),
        pytest.param({"file": "session notes.pdf"}, "no such detail key", id="a-filename"),
        pytest.param({"Rook saw the wrong card": True}, "no such detail key", id="a-key-of-text"),
        pytest.param({"": "x"}, "no such detail key", id="no-key"),
        # A declared key, with something that is not of its kind.
        pytest.param({"participant_id": "Rook"}, "minted prt_ identifier", id="a-name-for-an-id"),
        pytest.param({"participant_id": CAMPAIGN}, "minted prt_ identifier", id="the-wrong-kind"),
        pytest.param({"participant_id": 7}, "minted prt_ identifier", id="a-number-for-an-id"),
    ],
)
def test_a_detail_that_could_carry_content_is_refused(detail, refusal):
    """The rule is no longer "does this look like an identifier?" — which could
    not tell a one-word alias from one — but "is this key declared for this
    action, and is the value of its kind?". `alias` is a key of no action, and
    `Rook` is a value of no kind."""
    with pytest.raises(ValueError, match=refusal):
        check_detail(AuditAction.PARTICIPANT_ADDED, detail)


def test_a_refusal_never_repeats_the_value_or_the_key_it_refused():
    """A validator that quoted the offending value back would be the leak it
    exists to prevent — and a KEY carries content just as well as a value does,
    which is why an unknown key is named only by the vocabulary it was measured
    against."""
    with pytest.raises(ValueError) as refused:
        check_detail(AuditAction.PARTICIPANT_ADDED, {"Wren the Unseen": True})
    assert "Wren" not in str(refused.value)
    assert "participant_id" in str(refused.value), "it still says what the action does carry"

    with pytest.raises(ValueError) as wrong_kind:
        check_detail(AuditAction.PARTICIPANT_ADDED, {"participant_id": "Wren the Unseen"})
    assert "Wren" not in str(wrong_kind.value)
    assert "participant_id" in str(wrong_kind.value), "it still says which key was wrong"


def test_a_detail_of_ids_codes_keys_numbers_and_booleans_is_accepted():
    session = "ses_" + "b" * 22
    assert check_detail(
        AuditAction.SESSION_ROTATED,
        {
            "session_id": session,
            "generation": 2,
            "credentials_revoked": 3,
            "personal_links_reset": True,
        },
    ) == {
        "session_id": session,
        "generation": 2,
        "credentials_revoked": 3,
        "personal_links_reset": True,
    }
    assert check_detail(AuditAction.JOIN_BURST_REFUSED, {"bound": "joins_per_window"}) == {
        "bound": "joins_per_window"
    }
    assert check_detail(AuditAction.CAMPAIGN_ARCHIVED, None) == {}
    assert check_detail(AuditAction.PARTICIPANT_ADDED, {"participant_id": None}) == {
        "participant_id": None
    }, "a declared key may say that this row has no such value"


def test_a_closed_code_is_one_of_the_agreed_set_and_nothing_else():
    with pytest.raises(ValueError, match="one of credentials_per_generation"):
        check_detail(AuditAction.JOIN_BURST_REFUSED, {"bound": "too_many"})


@pytest.mark.parametrize(
    ("kind", "value", "ok"),
    [
        (Shape.FIELD_KEYS, ["motives", "hidden_identity"], True),
        (Shape.FIELD_KEYS, [], True),
        (Shape.FIELD_KEYS, ["Hidden Identity"], False),
        (Shape.FIELD_KEYS, ["k"] * (DETAIL_MAX_FIELD_KEYS + 1), False),
        (Shape.FIELD_KEYS, "motives", False),
        (Shape.FIELD_KEY, "passive_perception", True),
        (Shape.FIELD_KEY, "The Hooded Stranger", False),
        (Shape.WHOLE_NUMBER, 2, True),
        (Shape.WHOLE_NUMBER, 0, True),
        (Shape.WHOLE_NUMBER, True, False),
        # G-3: a count, a generation and a revision are all non-negative, and
        # the column that would hold one is a bigint.
        (Shape.WHOLE_NUMBER, -1, False),
        (Shape.WHOLE_NUMBER, 2**63 - 1, True),
        (Shape.WHOLE_NUMBER, 2**63, False),
        (Shape.WHOLE_NUMBER, 10**40, False),
        (Shape.FLAG, True, True),
        (Shape.FLAG, 1, False),
        (MintedId("prt_"), PARTICIPANT, True),
        (OneOf(("a", "b")), "b", True),
        (OneOf(("a", "b")), "c", False),
    ],
)
def test_the_kinds_a_later_bead_will_need_are_here_and_bounded(kind, value, ok):
    """ED-18(a) requires `1kg.7.1`'s reveal rows to carry MASK KEYS, so a list of
    field keys has to be expressible before the action that uses it exists —
    otherwise the first dependant would have to widen the validator it was told
    to rely on. `accepts` is public for exactly that: `1kg.7.1` adds
    `reveal.displayed` with `{"fields": Shape.FIELD_KEYS, ...}` and tests its own
    entry, changing nothing here."""
    assert accepts(kind, value) is ok


# ── The columns beside `detail` are closed too ───────────────────────────────


@pytest.mark.parametrize(
    "field", ["actor_ref", "object_ref", "campaign_id", "object_kind", "reason_code"]
)
@pytest.mark.parametrize(
    "content",
    [
        "Wren the Unseen",
        "The Nocturne of Vex",
        "session notes.pdf",
        "Rook saw the wrong card",
        "Rook",
        # G-3: the same two in the shape `object_kind` and `reason_code` used to
        # admit. A lower-case key is a key, so the shape rule could not tell
        # ED-20's canonical secret from a vocabulary word — and an audit row
        # outlives the campaign it describes by design (ED-26).
        "the_hooded_stranger_is_ondrey",
        "rook",
    ],
)
def test_no_string_field_of_a_row_accepts_content(field, content):
    """Every string a caller can put in a row, against every kind of content the
    acceptance criteria name — `Rook` included, which the previous shape rule
    let through. `0005_audit_events.sql` bounds lengths and nothing else, so the
    writer is the only thing between an alias and the ledger, and it has to
    cover the least-checked field rather than the obvious one."""
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        with pytest.raises(ValueError, match="never text") as refused:
            _record(log, unit, **{field: content})
        assert content not in str(refused.value)
        assert log.for_campaign(unit, CAMPAIGN) == [], "nothing was recorded"


def test_a_reference_is_a_minted_id_or_the_gms_user_id():
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        assert _record(log, unit, actor_ref="1", object_ref=PARTICIPANT).actor_ref == "1"
        assert _record(log, unit, actor_ref=CAMPAIGN).actor_ref == CAMPAIGN
        for refused in ("0", "-1", "prt_short", "1234567890123456789012"):
            with pytest.raises(ValueError, match="never text"):
                _record(log, unit, actor_ref=refused)


def test_the_campaign_a_row_names_is_a_minted_campaign_id():
    """The tombstone has no foreign key (ED-26), so this is the only thing
    between a row that names a campaign and a row that names a person."""
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        for refused in ("Rook", PARTICIPANT, "cmp_short"):
            with pytest.raises(ValueError, match="minted campaign identifier"):
                _record(log, unit, campaign_id=refused)


def test_the_object_kind_is_one_of_the_ledgers_own_kinds():
    """G-3. A lower-case key was a shape, not a vocabulary: `rook` passed it, and
    so did ED-20's secret spelled with underscores. The kinds are a closed enum
    now — exactly the things the registered actions are about — and the member
    is what is written, so the column stays TEXT and a later bead adds a kind
    the way it adds an action."""
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        assert _record(log, unit, object_kind=ObjectKind.TABLE_SESSION).object_kind == "table_session"
        assert _record(log, unit, object_kind="table_session").object_kind == "table_session"
        for refused in ("the session Rook joined", "rook", "session", ""):
            with pytest.raises(ValueError, match="never text"):
                _record(log, unit, object_kind=refused)


def test_every_kind_the_ledger_knows_is_a_thing_the_schema_mints_an_id_for():
    """The other direction, so the enum cannot quietly become somewhere to put a
    word: every member names one of the things `campaign_identity` mints an
    identifier for. `table_credential` is not one of them because no registered
    action is about a join credential — the bead that records one adds the
    member together with its action, as `AuditAction`'s docstring says."""
    minted = {audit_log.ObjectKind(kind) for kind in ("campaign", "participant", "table_session")}
    assert minted <= set(ObjectKind)
    assert {kind.value for kind in ObjectKind} == {
        "campaign", "table_session", "participant", "enrolment_code", "device_credential",
    }
    assert len(ObjectKind) < len(ident.PREFIXES), "table_credential has no action yet"


@pytest.mark.parametrize("reason", ["", "r" * 41, "not eligible", "Rook may not see that", "rook"])
def test_a_reason_is_one_of_the_codes_its_own_action_declares(reason):
    """G-3. `check_field_key` made a reason code a shape too, so any lower-case
    word was a reason. The set is per action, beside `ACTION_DETAIL`: a bead that
    needs a new reason declares it in the same change a reviewer reads."""
    with pytest.raises(ValueError, match="never text"):
        check_reason_code(AuditAction.PARTICIPANT_REMOVED, reason)
    assert check_reason_code(AuditAction.PARTICIPANT_REMOVED, "gm_removed") == "gm_removed"
    assert check_reason_code(AuditAction.PARTICIPANT_REMOVED, None) is None
    assert check_reason_code(AuditAction.PARTICIPANT_ADDED, None) is None, "and None stays legal"
    with pytest.raises(ValueError, match="never text"):
        # A code of another action is not a code of this one.
        check_reason_code(AuditAction.PARTICIPANT_ADDED, "gm_removed")


def test_every_action_says_what_its_rows_may_carry_in_both_registries():
    """An action with no entry can record nothing at all, and one with no reason
    entry would fall over on the first refusal a route recorded. Both registries
    are keyed by the same closed set, so this is the test that keeps them in step
    with it."""
    assert set(ACTION_DETAIL) == set(AuditAction)
    assert set(ACTION_REASONS) == set(AuditAction)
    assert ACTION_REASONS[AuditAction.PARTICIPANT_ADDED] == frozenset(), "an empty set is an answer"


@pytest.mark.parametrize("revision", [-1, -5, 1.5, True])
def test_an_authorisation_revision_the_database_would_refuse_is_refused_here(revision):
    """`0005_audit_events.sql` carries `authz_revision >= 0`, so the twin must
    too — otherwise a test on the fakes passes and the first real row is a
    CheckViolation."""
    log, db = InMemoryAuditLog(), InMemoryDatabase()
    with db.transaction() as unit:
        with pytest.raises(ValueError, match="authorisation revision"):
            _record(log, unit, authz_revision=revision)


def test_the_python_vocabularies_are_the_ones_the_migration_checks():
    """Two lists of the same strings, in two files: drift would surface as a
    production CheckViolation on a row nobody could re-send."""
    for column, members in (("actor_kind", ActorKind), ("decision", Decision)):
        found = re.search(rf"{column}\s+TEXT NOT NULL CHECK \({column} IN \(([^)]*)\)\)", AUDIT_SQL)
        assert found is not None, f"0005 no longer constrains {column} with an IN list"
        in_sql = {value.strip().strip("'") for value in found.group(1).split(",")}
        assert in_sql == {member.value for member in members}, column

    # `action` is deliberately NOT an IN list in the database: a later bead adds
    # a member without a migration (ED-18a). The closed set is Python's.
    assert "action IN (" not in AUDIT_SQL
    assert re.search(r"action\s+TEXT NOT NULL CHECK \(action <> ''", AUDIT_SQL) is not None


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
    sessions = InMemoryTableSessionStore(db, slot_clear=no_slots)
    log = InMemoryAuditLog()
    said: list[str] = []

    with caplog.at_level(logging.DEBUG):
        with db.transaction() as unit:
            campaign = campaigns.create(unit, owner_id=1, name=PRIVATE["campaign name"])
            seat = participants.add(unit, campaign.id, alias=PRIVATE["alias"])
            code, secret = participants.issue_code(unit, campaign.id, seat.id)
            credential, device_secret = participants.issue_device_credential(
                unit, campaign.id, seat.id
            )
            session, link = sessions.start(
                unit,
                campaign.id,
                owner_id=1,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
            joined, join_secret = sessions.issue_credential(unit, campaign.id, session.id)
            said += [repr(r) for r in (campaign, seat, code, credential, session, joined)]

            with pytest.raises(ValueError) as refused:
                participants.add(unit, campaign.id, alias="a" * 41)
            said.append(str(refused.value))
            with pytest.raises(Exception) as taken:
                participants.add(unit, campaign.id, alias=PRIVATE["alias"])
            said.append(str(taken.value))

            _record(log, unit, campaign_id=campaign.id, object_ref=seat.id)
            with pytest.raises(ValueError) as long_detail:
                check_detail(AuditAction.PARTICIPANT_ADDED, {"alias": PRIVATE["alias"] * 20})
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

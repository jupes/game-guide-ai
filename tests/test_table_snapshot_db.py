"""The table's snapshot read, in both worlds (agent-forge-harness-1kg.7.2 PR-2).

`TableReads` over the in-memory twin and over PostgreSQL, through the shared
suite's `served` fixture: who reads which slot (T-6), that nothing but the masked
text of an entitled slot reaches the bytes (T-1), that one seat's private reveal
leaves another reader's bytes exactly as they were (T-8), that a later widening
displays nothing (C-7, ED-13), and a differential of the store's views against
the policy decision point, whose facts are built here from the generator's own
labels and never from a store read (ID-14, C-8).

Requires DATABASE_URL for the `postgres` parameter (CI sets it for this file,
`.github/workflows/ci.yml`). From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_table_snapshot_db.py -q
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _pg import connect

from service.campaign_store import shared_rows
from service.policy import (
    Entitlement,
    GrantFacts,
    SeatFacts,
    SessionFacts,
    TableFacts,
    TableKind,
    TablePrincipal,
    decide_table,
    entitled,
)
from service.reveal_scope import EveryoneSeated, ParticipantsAudience
from service.reveals import StopDocument
from service.table_reads import TableReads
from service.workbench_contracts import DocumentTypeId, TableSnapshot
from tests import test_reveal_db as reveal_db
from tests.test_reveal_db import Served, _campaign, _confirm, _doc_of, _document, _seat, _session, _stop

# The shared suite's two fixtures, under their own names, so pytest finds them here.
served = reveal_db.served
dsn = reveal_db.dsn

CANARIES = {
    "name": "Canary-Name-9f1c",
    "qualifier": "Canary-Qualifier-3a7d",
    "voice": "Canary-Voice-55b0",
    "tags": ["Canary-Tag-c21e"],
}


def _to(*ids: str) -> ParticipantsAudience:
    return ParticipantsAudience(frozenset(ids))


def _reads(s: Served) -> TableReads:
    return TableReads(s.w.db, s.w.reveals, s.w.documents)


def _read(s: Served, campaign: str, *, account: int | None = None, grant: str | None = None) -> TableSnapshot | None:
    return _reads(s).snapshot(campaign, now=datetime.now(UTC), account_id=account, grant_id=grant)


def _bytes(snapshot: TableSnapshot | None) -> bytes:
    return b"null" if snapshot is None else snapshot.model_dump_json().encode()


def _slots(snapshot: TableSnapshot) -> dict[str, Any]:
    """slot name -> the dumped content (or None), from the one snapshot frame."""
    [frame] = [f for f in snapshot.frames if f.event == "snapshot"]
    return {
        slot.slot.value: None if slot.content is None else slot.content.model_dump(mode="json") for slot in frame.slots
    }


def _role(snapshot: TableSnapshot) -> str:
    [frame] = [f for f in snapshot.frames if f.event == "session"]
    return frame.role.value


def _grant(s: Served, campaign: str, session: str, at: datetime | None = None, *, owner: int | None = None) -> str:
    with s.w.db.transaction() as unit:
        made, _ = s.w.sessions.mint_screen(
            unit, campaign, session, owner_id=s.w.owner if owner is None else owner, now=at or datetime.now(UTC)
        )
    return str(made.id)


def _stage(s: Served) -> tuple[str, str, list[str]]:
    """A campaign and its live session, with confirmed seats A and B."""
    campaign = _campaign(s.w)
    seats = [_seat(s.w, campaign, "confirmed", s.w.players[n]) for n in range(2)]
    return campaign, _session(s.w, campaign).id, seats


def _fields(content: Any) -> dict[str, Any]:
    return {field["key"]: field["value"] for field in content["fields"]}


def test_a_screen_reads_the_table_slot_and_a_confirmed_seat_reads_its_own_as_well(served: Served) -> None:
    """T-6 for the content: the owner and a screen are guests of the table slot;
    a confirmed seat is a participant with `mine`; every slot shows its masked
    fields and nothing else. Kills: `own_slot` ignored (a guest gets `mine`)."""
    campaign, session, (a, b) = _stage(served)
    doc = _doc_of(served.w, campaign, DocumentTypeId.NPC, CANARIES)
    _confirm(served, campaign, session, doc, mask=("name",))
    private = _doc_of(served.w, campaign, DocumentTypeId.NPC, {**CANARIES, "name": "Canary-Name-private"})
    _confirm(served, campaign, session, private, _to(a), mask=("voice",))

    owner = _read(served, campaign, account=served.w.owner)
    assert owner is not None and _role(owner) == "guest"
    shown = {"content_kind": "document", "type": "npc", "fields": [{"key": "name", "value": CANARIES["name"]}]}
    assert _slots(owner) == {"table": shown}
    screen = _read(served, campaign, grant=_grant(served, campaign, session))
    assert screen is not None and _role(screen) == "guest" and set(_slots(screen)) == {"table"}
    seat = _read(served, campaign, account=served.w.players[0])
    assert seat is not None and _role(seat) == "participant"
    assert _fields(_slots(seat)["table"]) == {"name": CANARIES["name"]}
    assert _fields(_slots(seat)["mine"]) == {"voice": CANARIES["voice"]}
    empty = _read(served, campaign, account=served.w.players[1])
    assert empty is not None and _role(empty) == "participant" and _slots(empty)["mine"] is None


def test_the_bytes_of_every_viewer_hold_the_masked_text_and_no_id_and_no_other_field(served: Served) -> None:
    """T-1 at the resolver: the owner, a confirmed seat, an awaiting seat and a
    screen. Every other field's canary, and every id the rows hold, is absent
    from the serialised snapshot. Kills: a projection built from the whole
    document; an id or a disclosure riding in a frame."""
    campaign, session, (a, _b) = _stage(served)
    awaiting = _seat(served.w, campaign, "accepted", served.w.players[2])
    doc = _doc_of(served.w, campaign, DocumentTypeId.NPC, CANARIES)
    shown = _confirm(served, campaign, session, doc, mask=("name",)).picture
    mine = _confirm(
        served, campaign, session, _doc_of(served.w, campaign, DocumentTypeId.NPC, CANARIES), _to(a), mask=("voice",)
    ).picture
    grant = _grant(served, campaign, session)
    disclosures = {e.live.disclosure_id for p in (shown, mine) for e in p.entries if e.live}
    ids = {campaign, session, doc, a, awaiting, grant, *disclosures}
    viewers = {
        "owner": _read(served, campaign, account=served.w.owner),
        "confirmed": _read(served, campaign, account=served.w.players[0]),
        "awaiting": _read(served, campaign, account=served.w.players[2]),
        "screen": _read(served, campaign, grant=grant),
    }
    for name, snapshot in viewers.items():
        assert snapshot is not None, name
        raw = snapshot.model_dump_json()
        assert CANARIES["name"] in raw, name
        assert "Canary-Qualifier" not in raw and "Canary-Tag" not in raw, name
        assert not any(i in raw for i in ids), name
        assert ("Canary-Voice" in raw) == (name == "confirmed"), name


def test_an_awaiting_seat_reads_the_table_slot_only_and_a_removed_or_offered_one_reads_nothing(served: Served) -> None:
    """T-6, the rows between: an accepted seat the GM has not confirmed is a
    guest; an offered seat, a removed seat, a seat of another campaign, an
    account with no seat and the GM of another campaign are all `None`. Kills:
    joining the own slot for `accepted` rather than `confirmed`."""
    w = served.w
    campaign, session, _ = _stage(served)
    elsewhere = _campaign(w, owner=w.other_owner)
    _seat(w, elsewhere, "confirmed", w.players[5])
    _confirm(served, campaign, session, _document(w, campaign))
    held = _doc_of(w, campaign, DocumentTypeId.NPC, CANARIES)
    waiting = _seat(w, campaign, "accepted", w.players[2])
    _confirm(served, campaign, session, held, _to(waiting), mask=("voice",))
    _seat(w, campaign, "offered", w.players[3])
    _seat(w, campaign, "removed", w.players[4])
    guest = _read(served, campaign, account=w.players[2])
    assert guest is not None and _role(guest) == "guest" and set(_slots(guest)) == {"table"}
    assert "Canary-Voice" not in guest.model_dump_json(), "a held copy is not delivered before the GM confirms the seat"
    for nobody in (w.players[3], w.players[4], w.players[5], w.players[6], w.other_owner):
        assert _read(served, campaign, account=nobody) is None, nobody


def test_a_grant_of_another_campaign_a_revoked_one_and_an_ended_session_read_nothing(served: Served) -> None:
    """T-6, the screen rows. Kills: a grant read under another campaign's id."""
    w = served.w
    campaign, session, _ = _stage(served)
    other = _campaign(w, owner=w.other_owner)
    _session(w, other, owner=w.other_owner)
    live = _grant(served, campaign, session)
    assert _read(served, other, grant=live) is None, "a grant of this campaign, read under another campaign's id"
    revoked = _grant(served, campaign, session)
    with w.db.transaction() as unit:
        w.sessions.revoke_screen(unit, campaign, revoked, owner_id=w.owner, now=datetime.now(UTC))
    assert _read(served, campaign, grant=revoked) is None
    assert _read(served, campaign, grant=live) is not None, "the positive control"
    with w.db.transaction() as unit:
        w.sessions.end(unit, campaign, session, owner_id=w.owner)
    assert _read(served, campaign, grant=live) is None
    assert _read(served, campaign, account=w.owner) is None


def test_one_seats_private_reveal_never_changes_another_readers_bytes(served: Served) -> None:
    """T-8, the snapshot half: a screen's bytes, and the other confirmed seat's,
    are identical before A's private reveal, after it, after its update, after
    its Stop, and after A is removed. Kills: a sequence, a count or a slot that
    leaks A's activity to B."""
    w = served.w
    campaign, session, (a, _b) = _stage(served)
    _confirm(served, campaign, session, _document(w, campaign))
    grant = _grant(served, campaign, session)

    def watchers() -> tuple[bytes, bytes]:
        return _bytes(_read(served, campaign, grant=grant)), _bytes(_read(served, campaign, account=w.players[1]))

    before = watchers()
    secret = _doc_of(w, campaign, DocumentTypeId.NPC, CANARIES)
    _confirm(served, campaign, session, secret, _to(a), mask=("voice",))
    assert watchers() == before, "after A's private reveal"
    _confirm(served, campaign, session, secret, _to(a), mask=("voice", "qualifier"))
    assert watchers() == before, "after its update"
    _stop(served, campaign, StopDocument(secret))
    assert watchers() == before, "after its Stop"
    _confirm(served, campaign, session, secret, _to(a), mask=("voice",))
    with w.db.transaction() as unit:
        w.participants.remove(unit, campaign, a)
    assert watchers() == before, "after A's removal"


def test_a_widening_after_an_everyone_seated_confirm_displays_nothing_and_a_named_copy_is_delivered_on_confirmation(
    served: Served,
) -> None:
    """C-7 (ED-13, SEC-50(5), D-12). Everyone seated names the seats confirmed
    at Confirm. A seat confirmed afterwards reads nothing of it. A copy written
    to an unconfirmed seat by name is delivered on the first read after the
    confirmation, to that seat only. Kills: expanding the audience at read time
    (an Everyone copy for a seat accepted but not yet confirmed)."""
    w = served.w
    campaign, session, (a, _b) = _stage(served)
    bare = _seat(w, campaign, "accepted", w.players[3])
    late = _seat(w, campaign, "accepted", w.players[2])
    everyone = _doc_of(w, campaign, DocumentTypeId.NPC, {**CANARIES, "voice": "Canary-Everyone"})
    _confirm(served, campaign, session, everyone, EveryoneSeated(), mask=("voice",))
    named = _doc_of(w, campaign, DocumentTypeId.NPC, {**CANARIES, "voice": "Canary-Named"})
    _confirm(served, campaign, session, named, _to(late), mask=("voice",))
    for waiting in (w.players[2], w.players[3]):
        assert "Canary-" not in (_read(served, campaign, account=waiting) or _bad()).model_dump_json()
    with w.db.transaction() as unit:
        w.participants.confirm(unit, campaign, late)
        w.participants.confirm(unit, campaign, bare)
    after = _read(served, campaign, account=w.players[2]) or _bad()
    assert _role(after) == "participant"
    assert _fields(_slots(after)["mine"]) == {"voice": "Canary-Named"}, "the named copy, and not the Everyone copy"
    assert "Canary-Everyone" not in after.model_dump_json()
    widened = _read(served, campaign, account=w.players[3]) or _bad()
    assert _role(widened) == "participant" and _slots(widened)["mine"] is None, "a later widening displays nothing"
    assert "Canary-" not in widened.model_dump_json()
    first = _read(served, campaign, account=w.players[0]) or _bad()
    assert _fields(_slots(first)["mine"]) == {"voice": "Canary-Everyone"}
    assert "Canary-Named" not in first.model_dump_json()


def _bad() -> Any:
    raise AssertionError("that reader should have been entitled")


def test_a_session_with_audio_off_says_so(served: Served) -> None:
    """`TableView.audio` is the session's flag in both worlds. Kills: a constant."""
    w = served.w
    campaign, session, _ = _stage(served)

    def audio() -> bool:
        snapshot = _read(served, campaign, account=w.owner) or _bad()
        [frame] = [f for f in snapshot.frames if f.event == "session"]
        return bool(frame.audio)

    assert audio() is True
    if w.kind == "fake":
        rows = shared_rows(w.db, "table_sessions")
        with w.db.transaction() as unit:
            rows.replace(unit, session, replace(rows.visible(unit)[session], table_audio=False))
    else:
        assert w.dsn is not None
        with connect(w.dsn) as conn:
            conn.execute("UPDATE campaign.table_sessions SET table_audio = false WHERE id = %s", (session,))
    assert audio() is False


# ── The differential against the policy decision point (ID-14, C-8) ──────────


def _facts(
    campaign: str, gm: int, session: SessionFacts | None, seats: tuple[SeatFacts, ...], grant: GrantFacts | None
) -> TableFacts:
    """Built from the generator's own labels, never from a store read."""
    return TableFacts(campaign, gm, (session,) if session is not None else (), seats, grant, {}, {}, (0, 0))


SEAT_STATES = {
    "open": None,
    "offered": (False, False, False),
    "accepted": (True, False, False),
    "confirmed": (True, True, False),
    "removed": (True, True, True),
}


@pytest.mark.parametrize("session_state", ["live", "ended", "expired"])
def test_the_views_agree_with_the_policy_decision_point(served: Served, session_state: str) -> None:
    """`view_for_account`, `view_for_screen` and `TableReads` against
    `decide_table` plus `entitled`, over every principal the labels name and
    every state of the session. Kills: a store view that is wider or narrower
    than the decision point (a removed seat that reads, an expired session that
    serves, a grant of another campaign that resolves)."""
    w = served.w
    campaign = _campaign(w)
    other = _campaign(w, owner=w.other_owner)
    now = datetime.now(UTC)
    ago = 2 if session_state == "expired" else 0
    minted = now - timedelta(hours=ago) + timedelta(minutes=30 if ago else 0)
    session = _session(w, campaign, hours=1 if ago else 12, started_ago_h=ago)
    other_session = _session(w, other, owner=w.other_owner)
    seats: dict[str, tuple[int, str, tuple[SeatFacts, ...]]] = {}
    for n, (label, flags) in enumerate(SEAT_STATES.items()):
        player = w.players[n]
        seat = _seat(w, campaign, label, player)
        facts = () if flags is None else (SeatFacts(seat, *flags),)
        seats[label] = (player, seat, facts)
    foreign = (w.players[5], "prt_foreign", ())
    _seat(w, other, "confirmed", foreign[0])
    live_grant = _grant(served, campaign, session.id, minted)
    revoked_grant = _grant(served, campaign, session.id, minted)
    with w.db.transaction() as unit:
        w.sessions.revoke_screen(unit, campaign, revoked_grant, owner_id=w.owner, now=minted)
    foreign_grant = _grant(served, other, other_session.id, owner=w.other_owner)
    ended = session_state == "ended"
    if ended:
        with w.db.transaction() as unit:
            w.sessions.end(unit, campaign, session.id, owner_id=w.owner)
    row = SessionFacts(
        session.id, campaign, "ended" if ended else "live", session.expires_at, session.link_generation
    )
    row_facts = None if ended else row
    elsewhere = SessionFacts(other_session.id, other, "live", now + timedelta(hours=12), other_session.link_generation)

    def grant_facts(grant_id: str, session_row: SessionFacts, revoked: bool) -> GrantFacts:
        return GrantFacts(grant_id, session_row.generation, revoked, session_row)

    accounts: list[tuple[str, int, tuple[SeatFacts, ...]]] = [("owner", w.owner, ()), ("other gm", w.other_owner, ())]
    accounts += [(label, p, f) for label, (p, _s, f) in seats.items()]
    accounts += [("foreign seat", foreign[0], ()), ("stranger", w.players[6], ())]
    cases: list[tuple[str, int | None, str | None, tuple[SeatFacts, ...], GrantFacts | None]] = [
        (label, account, None, f, None) for label, account, f in accounts
    ]
    cases += [
        ("live screen", None, live_grant, (), grant_facts(live_grant, row, False)),
        ("revoked screen", None, revoked_grant, (), grant_facts(revoked_grant, row, True)),
        ("another campaign's screen", None, foreign_grant, (), grant_facts(foreign_grant, elsewhere, False)),
    ]
    matched = 0
    for label, account, grant, seat_facts, grant_row in cases:
        facts = _facts(campaign, w.owner, row_facts, seat_facts, grant_row)
        outcome = decide_table(facts, account_id=account, grant_id=grant, now=now)
        with w.db.transaction() as unit:
            view = (
                w.reveals.view_for_screen(unit, campaign, grant, now=now)
                if grant is not None
                else w.reveals.view_for_account(unit, campaign, user_id=account, now=now)
            )
        snapshot = _read(served, campaign, account=account, grant=grant)
        if not isinstance(outcome, TablePrincipal):
            assert view is None and snapshot is None, (label, session_state)
            continue
        matched += 1
        assert view is not None and snapshot is not None, (label, session_state)
        assert view.is_owner is (outcome.kind is TableKind.OWNER_VIEWER), label
        own = outcome.participant_id is not None and entitled(outcome, outcome.participant_id) is Entitlement.ENTITLED
        assert view.own_slot is own, label
        assert entitled(outcome, None) is Entitlement.ENTITLED
        assert set(_slots(snapshot)) == ({"table", "mine"} if own else {"table"}), label
        assert _role(snapshot) == ("participant" if own else "guest"), label
    # The owner, a confirmed seat, an accepted one and a live screen.
    assert matched == (4 if session_state == "live" else 0), "the matrix reached the principals it names"

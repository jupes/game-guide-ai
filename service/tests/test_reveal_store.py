"""The reveal store's pure checks and structural rules (agent-forge-harness-1kg.7.1).

Everything here runs without a database. The behaviour both worlds share is
`tests/test_reveal_db.py`'s; this file holds what reading the source or calling
a pure function can prove: the mask and target checks, the scope's reason
pairs, what a record's `repr()` shows, and two source scans — ended disclosures
are read by the replay lookup only (I-13), and no reveal module reads a default
mask (I-14). Each scan names the targets it expects to find, so an empty or
mis-rooted scan fails rather than passing.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest

from service import campaign_identity as ident
from service import reveal_scope, reveal_store
from service.reveal_scope import (
    NARROWED,
    DocumentCopies,
    EndReason,
    EverySlot,
    NoSlot,
    ParticipantSlots,
    ParticipantTargets,
    TableTarget,
)
from service.reveal_store import (
    AudienceRefused,
    CopyChange,
    Disclosure,
    DocumentNotDisplayable,
    MaskRefused,
    PictureEntry,
    RevealBusy,
    RevealConflict,
    RevealNotFound,
    VersionNotDisplayable,
    Written,
    check_scope,
    check_stored_mask,
    check_targets,
)
from service.workbench_contracts import REVEAL_MAX_SLOTS

SERVICE = Path(__file__).resolve().parents[1]
COMMAND = "Rook-saw-the-card-0123456789"


def _disclosure(**overrides: object) -> Disclosure:
    base: dict[str, object] = {
        "id": ident.new_id(ident.DISCLOSURE),
        "campaign_id": ident.new_id(ident.CAMPAIGN),
        "session_id": ident.new_id(ident.TABLE_SESSION),
        "document_id": ident.new_id(ident.DOCUMENT),
        "version": 1,
        "mask": ("name",),
        "audience_kind": "table",
        "created_at": datetime.now(UTC),
        "command_id": COMMAND,
    }
    base.update(overrides)
    return Disclosure(**base)  # type: ignore[arg-type]


# ── The mask, the targets and the scopes ─────────────────────────────────────


def test_a_stored_mask_is_sorted_and_distinct_field_keys() -> None:
    assert check_stored_mask(["voice", "name", "tags"]) == ("name", "tags", "voice")
    assert check_stored_mask(("name",)) == ("name",)
    assert check_stored_mask([f"k{n:02d}" for n in range(64)])[0] == "k00"


@pytest.mark.parametrize(
    ("mask", "keys"),
    [
        ([], ()),
        ([f"k{n}" for n in range(65)], ()),
        ("name", ()),
        (["all"], ("all",)),
        (["name", "name"], ("name",)),
        (["name", "The Hooded Stranger"], ()),
        (["voice", "all", "Bad", "voice"], ("all", "voice")),
        (["name,voice"], ()),
    ],
)
def test_a_mask_that_is_not_distinct_field_keys_is_refused_naming_only_well_formed_keys(
    mask: object, keys: tuple[str, ...]
) -> None:
    """A key that is really a sentence is refused without being repeated; the
    refusal's `keys` hold only field-key-shaped keys, sorted."""
    with pytest.raises(MaskRefused) as refused:
        check_stored_mask(mask)  # type: ignore[arg-type]
    assert refused.value.keys == keys
    assert "Hooded" not in str(refused.value) and "Bad" not in repr(refused.value)


def test_targets_are_the_table_alone_or_well_formed_participants() -> None:
    ana, ben = ident.new_id(ident.PARTICIPANT), ident.new_id(ident.PARTICIPANT)
    assert check_targets(TableTarget()) == frozenset({None})
    assert check_targets(ParticipantTargets(frozenset({ana, ben}))) == frozenset({ana, ben})
    too_many = frozenset(ident.new_id(ident.PARTICIPANT) for _ in range(101))
    for refused in (
        ParticipantTargets(frozenset()),
        ParticipantTargets(too_many),
        ParticipantTargets(frozenset({ident.new_id(ident.DOCUMENT)})),
        ParticipantTargets(frozenset({"Ana"})),
        ParticipantTargets({ana}),  # type: ignore[arg-type]
        reveal_scope.TableAudience(),
        None,
    ):
        with pytest.raises(AudienceRefused):
            check_targets(refused)  # type: ignore[arg-type]


def test_each_narrowing_names_only_the_reasons_it_may_use() -> None:
    """The pairs: a Remove records `participant_removed`, the reconciliation
    `reconciled`, a Stop `gm_stop` or `stop_all`; a Confirm's own reasons
    (`replaced`, `moved`, `updated`) are never a narrowing's."""
    remove = ParticipantSlots(frozenset(), EndReason.PARTICIPANT_REMOVED)
    allowed: reveal_scope.SlotScope
    for allowed in (
        remove,
        ParticipantSlots(frozenset(), EndReason.RECONCILED),
        DocumentCopies("doc", EndReason.GM_STOP),
        EverySlot(EndReason.STOP_ALL),
        EverySlot(EndReason.GM_END),
        EverySlot(EndReason.EXPIRED),
        EverySlot(EndReason.LINK_ROTATED),
        EverySlot(EndReason.CAMPAIGN_ARCHIVED),
        EverySlot(EndReason.RECONCILED),
        NARROWED,
        NoSlot(),
        DocumentCopies("doc", EndReason.DOCUMENT_ARCHIVED),
        DocumentCopies("doc", EndReason.DOCUMENT_DELETED),
        DocumentCopies("doc", EndReason.CHARACTER_UNLINKED),
    ):
        assert check_scope(allowed) is allowed
    refusals: list[reveal_scope.SlotScope] = [
        ParticipantSlots(frozenset(), EndReason.GM_STOP),
        DocumentCopies("doc", EndReason.RECONCILED),
        EverySlot(EndReason.GM_STOP),
        EverySlot(EndReason.PARTICIPANT_REMOVED),
        *(EverySlot(reason) for reason in (EndReason.REPLACED, EndReason.MOVED, EndReason.UPDATED)),
    ]
    for refused in refusals:
        with pytest.raises(ValueError, match="reason its narrowing may use"):
            check_scope(refused)
    assert NARROWED == EverySlot(EndReason.NARROWED), "narrow's fail-closed default is every slot"


def test_end_reasons_are_ed17s_codes() -> None:
    assert len(EndReason) == 15
    assert EndReason.LINK_ROTATED.value == "link_rotated"
    assert all(member.value.islower() and " " not in member.value for member in EndReason)


# ── What a record and a refusal show ─────────────────────────────────────────


def test_no_record_shows_the_command_id_and_no_refusal_carries_an_identifier() -> None:
    """T-A28 (the pure half): `repr()` and `str()` of every record that holds a
    command id hide it, and each refusal's message is a fixed sentence."""
    shown = _disclosure()
    change = CopyChange(shown, (), True, EndReason.MOVED)
    for record in (shown, change, Written(shown, "displayed", (change,))):
        assert COMMAND not in repr(record) and COMMAND not in str(record)
    for refusal in (
        RevealNotFound(),
        RevealConflict(),
        MaskRefused(),
        AudienceRefused(),
        DocumentNotDisplayable(),
        VersionNotDisplayable(),
        RevealBusy(),
    ):
        text = str(refusal)
        assert text and not any(prefix in text for prefix in ident.PREFIXES), type(refusal).__name__
    for refusal_type in (RevealNotFound, RevealConflict, AudienceRefused, RevealBusy):
        assert list(inspect.signature(refusal_type).parameters) == [], "constructed with nothing"


def test_a_disclosure_is_live_until_it_ends() -> None:
    assert _disclosure().is_live
    assert not _disclosure(ended_at=datetime.now(UTC), ended_reason=EndReason.GM_STOP).is_live


def test_the_picture_refuses_rather_than_truncates_past_reveal_max_slots() -> None:
    """ID-16 (critic 13): the table entry and `REVEAL_MAX_SLOTS - 1` participant
    entries are listed whole, and one participant more is `RevealBusy`, never a
    picture cut short (PostgreSQL's LIMIT reads one row past the bound so that
    this guard can see it)."""
    seats = sorted(ident.new_id(ident.PARTICIPANT) for _ in range(REVEAL_MAX_SLOTS))
    entries = [PictureEntry("participant", pid, 1, None) for pid in seats]
    session, campaign = ident.new_id(ident.TABLE_SESSION), ident.new_id(ident.CAMPAIGN)

    whole = reveal_store._picture(session, campaign, 1, 0, entries[:-1])
    assert len(whole.entries) == REVEAL_MAX_SLOTS
    assert whole.entries[0] == PictureEntry("table", None, 0, None)
    assert [e.participant_id for e in whole.entries[1:]] == seats[:-1]
    with pytest.raises(RevealBusy):
        reveal_store._picture(session, campaign, 1, 0, entries)


# ── Source scans ─────────────────────────────────────────────────────────────


def _statements_by_method() -> dict[str, list[str]]:
    """Every SQL statement `PostgresRevealStore` runs, by the method that runs
    it: the literal text of each `.execute(...)`'s first argument."""
    tree = ast.parse((SERVICE / "reveal_store.py").read_text(encoding="utf-8"))
    [store] = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PostgresRevealStore"]
    found: dict[str, list[str]] = {}
    for method in store.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        for call in ast.walk(method):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "execute"
                and call.args
            ):
                text = "".join(
                    node.value
                    for node in ast.walk(call.args[0])
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                )
                found.setdefault(method.name, []).append(text)
    return found


def test_ended_disclosures_are_read_only_by_the_replay_lookup() -> None:
    """T-A27 (I-13). Every statement of the PostgreSQL store that reads or
    writes `reveal_disclosures` — except the INSERT of a new one — carries
    `ended_at IS NULL`, save `by_command`, the replay lookup, which is the one
    read of ended rows. The scan must find the named methods, or it proves
    nothing."""
    statements = _statements_by_method()
    touching = {
        name: [sql for sql in sqls if "reveal_disclosures" in sql and not sql.lstrip().startswith("INSERT")]
        for name, sqls in statements.items()
    }
    touching = {name: sqls for name, sqls in touching.items() if sqls}
    assert set(touching) == {
        "_live",
        "_end",
        "by_command",
        "live_for_document",
        "live_disclosures",
        "picture",
        "view_for_account",
        "view_for_screen",
        "stale_slots",
    }, "the scan found the methods it names"
    for name, sqls in touching.items():
        for sql in sqls:
            if name == "by_command":
                assert "ended_at" not in sql, "replay answers an ended disclosure too"
                continue
            assert "ended_at IS NULL" in sql, f"{name} reads ended disclosures"
    assert "ended_at" not in "".join(statements["_insert"]), "the insert writes a live row"


def test_no_reveal_module_reads_a_default_mask() -> None:
    """I-14 (X-2, REVEAL-4): no code path in the reveal modules reads the
    registry's default reveal. The positive control shows the needle is real:
    the registry itself carries it."""
    needles = ("default_reveal", "defaultReveal", "default_reveal_for")
    scanned = {name: (SERVICE / name).read_text(encoding="utf-8") for name in ("reveal_scope.py", "reveal_store.py")}
    assert all(len(text) > 1000 for text in scanned.values()), "the scan read both modules"
    for name, text in scanned.items():
        assert not [needle for needle in needles if needle in text], name
    assert "default_reveal_for" in (SERVICE / "workbench_registry.py").read_text(encoding="utf-8")


def test_every_explicit_lock_in_the_store_is_for_no_key_update() -> None:
    """I-7: the store's one explicit lock is the session row's, `FOR NO KEY
    UPDATE`, and no other mode appears in any statement it runs."""
    statements = [sql for sqls in _statements_by_method().values() for sql in sqls]
    locking = [sql for sql in statements if " FOR " in sql and "FOR NO KEY UPDATE" in sql]
    assert len(locking) == 1 and "campaign.table_sessions" in locking[0]
    for sql in statements:
        for mode in ("FOR UPDATE", "FOR SHARE", "FOR KEY SHARE"):
            assert mode not in sql.replace("FOR NO KEY UPDATE", ""), sql
    updates = [sql for sql in statements if sql.lstrip().startswith("UPDATE")]
    assert len(updates) == 2
    assert any("SET disclosure_id = %s, seq = seq + 1, updated_at = %s" in sql for sql in updates)
    assert any("SET ended_at = %s, ended_reason = %s" in sql for sql in updates)


def test_the_store_never_takes_the_campaign_lock_and_writes_no_audit_row() -> None:
    source = inspect.getsource(reveal_store)
    assert "lock_campaign" not in source.replace("`lock_campaign`", "")
    assert "audit_log" not in source and "AuditAction" not in source, "the service audits, never the store"

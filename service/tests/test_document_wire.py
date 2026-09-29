"""The pure rules between the document store and the wire (bead 1kg.5.2):
what is stored, what is emitted, what a patch effectively changes, the cursors.

Run from the repo root:
    uv run python -m pytest service/tests/test_document_wire.py -q
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from service import document_wire as wire
from service.document_store import DocumentRecord, LibraryRow, VersionRecord, VersionSnapshot
from service.workbench_contracts import (
    VERSION_NUMBER_MAX,
    AssetRef,
    DocumentTypeId,
    LibraryCategory,
    check_fields,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
DOC = "doc_" + "a" * 22
OTHER_DOC = "doc_" + "b" * 22
CAMPAIGN = "cmp_" + "a" * 22
ASSET = {"asset_id": "ast_" + "a" * 22, "media_type": "image", "alt": "A portrait"}


def _validated(doc_type: DocumentTypeId, fields: dict[str, Any]) -> dict[str, Any]:
    return check_fields(doc_type, 1, fields, whole=False, enforce_required=False)


def _version(number: int = 1, *, sealed: bool = False, restored_from: int | None = None) -> VersionRecord:
    return VersionRecord(
        document_id=DOC, number=number, author="gm", summary="", changed_fields=("name",),
        restored_from=restored_from, sealed_at=T0 if sealed else None, created_at=T0, updated_at=T0,
    )


def _record(data: Any, *, doc_type: str = "npc", type_version: int = 1, archived: bool = False) -> DocumentRecord:
    return DocumentRecord(
        id=DOC, campaign_id=CAMPAIGN, type=doc_type, type_version=type_version, data=data, write_revision=3,
        field_revisions={"name": 1}, archived_at=T0 if archived else None, linked_participant_id=None,
        created_at=T0, updated_at=T0, version=_version(),
    )


# ── W-1: what is stored ──────────────────────────────────────────────────────


def test_stored_json_is_plain_json_of_the_validated_values() -> None:
    npc = wire.stored_json(_validated(DocumentTypeId.NPC, {"portrait": dict(ASSET), "voice": "low"}))
    assert npc == {"portrait": ASSET, "voice": "low"}, "an asset without a size stores no width or height"
    block = wire.stored_json(
        _validated(
            DocumentTypeId.STATBLOCK,
            {"hp": 14.0, "ac": None, "abilities": None, "traits": [{"name": "Keen", "text": "Sharp."}]},
        )
    )
    assert block == {"hp": 14, "ac": None, "abilities": None, "traits": [{"name": "Keen", "text": "Sharp."}]}
    assert type(block["hp"]) is int
    cleared = wire.stored_json(_validated(DocumentTypeId.NPC, {"portrait": None}))
    assert cleared == {"portrait": None}, "a cleared field keeps its key and stores null"


# ── W-2: what a patch effectively changes ────────────────────────────────────

_SEVEN_KINDS: list[tuple[DocumentTypeId, str, Any, Any]] = [
    (DocumentTypeId.NPC, "voice", "low", "high"),  # text
    (DocumentTypeId.NPC, "wants", "gold", "power"),  # prose
    (DocumentTypeId.NPC, "tags", ["a", "b"], ["b", "a"]),  # text_list
    (DocumentTypeId.NPC, "portrait", ASSET, {**ASSET, "alt": "Another"}),  # asset
    (DocumentTypeId.STATBLOCK, "hp", 14, 15),  # integer
    (DocumentTypeId.STATBLOCK, "abilities", {"str": 10, "dex": 12}, {"str": 10}),  # abilities
    (DocumentTypeId.STATBLOCK, "traits", [{"name": "Keen", "text": "x"}], [{"name": "Keen", "text": "y"}]),
]


@pytest.mark.parametrize(("doc_type", "key", "same", "different"), _SEVEN_KINDS)
def test_effective_fields_drop_what_is_already_stored_for_every_kind(
    doc_type: DocumentTypeId, key: str, same: Any, different: Any
) -> None:
    stored = _validated(doc_type, {key: same})
    assert wire.effective_fields(stored, _validated(doc_type, {key: same})) == {}
    changed = wire.effective_fields(stored, _validated(doc_type, {key: different}))
    assert changed == wire.stored_json(_validated(doc_type, {key: different}))
    assert wire.effective_fields({}, _validated(doc_type, {key: same})) == wire.stored_json(
        _validated(doc_type, {key: same})
    ), "a key the document does not hold differs from any value"


def test_an_explicit_null_size_equals_an_asset_stored_without_one() -> None:
    stored = _validated(DocumentTypeId.NPC, {"portrait": dict(ASSET)})
    sent = _validated(DocumentTypeId.NPC, {"portrait": {**ASSET, "width": None, "height": None}})
    assert isinstance(sent["portrait"], AssetRef)
    assert wire.effective_fields(stored, sent) == {}


def test_a_partly_equal_patch_keeps_only_the_keys_that_differ() -> None:
    stored = _validated(DocumentTypeId.NPC, {"name": "Mira", "voice": "low"})
    sent = _validated(DocumentTypeId.NPC, {"name": "Mira", "voice": "high", "tell": "hums"})
    assert wire.effective_fields(stored, sent) == {"voice": "high", "tell": "hums"}


# ── W-3: the cursors ─────────────────────────────────────────────────────────


def _b64(text: str | bytes) -> str:
    raw = text.encode("latin-1") if isinstance(text, str) else text
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def test_both_cursors_round_trip() -> None:
    history = wire.encode_history_cursor(DOC, 17)
    assert wire.decode_history_cursor(history, DOC) == 17
    assert wire.decode_history_cursor(wire.encode_history_cursor(DOC, VERSION_NUMBER_MAX), DOC) == VERSION_NUMBER_MAX
    library = wire.encode_library_cursor(DOC)
    assert wire.decode_library_cursor(library) == DOC
    for cursor in (history, library):
        assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert "=" not in cursor


_BAD_HISTORY = [
    "not base64!",
    "",
    "a",  # a length base64 cannot have
    _b64(b"H1.doc_\xff\xfe.3"),  # not ASCII
    _b64(f"X1.{DOC}.3"),  # the wrong tag
    _b64(f"H1.{DOC}.0"),
    _b64(f"H1.{DOC}.03"),
    _b64(f"H1.{DOC}.{VERSION_NUMBER_MAX + 1}"),
    _b64("H1.doc_short.3"),  # an id outside doc_'s shape the grammar lets through
    _b64("H1.doc_" + "a" * 11 + chr(0) + "a" * 10 + ".3"),
    _b64(f"H1.{OTHER_DOC}.3"),  # another document's cursor
    _b64(f"L1.{DOC}"),  # a library cursor given to history
    _b64(f"H1.{DOC}.3") + "A",  # not the one spelling this module issues
]


@pytest.mark.parametrize("cursor", _BAD_HISTORY)
def test_a_history_cursor_refuses_anything_it_did_not_issue_for_this_document(cursor: str) -> None:
    with pytest.raises(wire.InvalidDocumentCursor) as refused:
        wire.decode_history_cursor(cursor, DOC)
    assert str(refused.value) == "that cursor isn't one this service issued here", "a fixed message"
    assert isinstance(refused.value, ValueError)


@pytest.mark.parametrize(
    "cursor", ["%%", _b64(f"H1.{DOC}.3"), _b64("L1.doc_short"), _b64(f"L1.{DOC}.3"), _b64(b"L1.doc_\xff")]
)
def test_a_library_cursor_refuses_anything_it_did_not_issue(cursor: str) -> None:
    with pytest.raises(wire.InvalidDocumentCursor):
        wire.decode_library_cursor(cursor)


# ── W-4: a category's types ──────────────────────────────────────────────────


def test_each_category_lists_its_own_types() -> None:
    assert wire.category_types(LibraryCategory.NPCS, None) == [DocumentTypeId.NPC]
    assert wire.category_types(LibraryCategory.BESTIARY, None) == [DocumentTypeId.STATBLOCK]
    assert wire.category_types(LibraryCategory.SESSION_LOG, None) == [DocumentTypeId.SESSION_NOTES]
    assert set(wire.category_types(LibraryCategory.DOCUMENTS, None)) == {
        DocumentTypeId.HANDOUT, DocumentTypeId.QUEST_LOG, DocumentTypeId.CHARACTER_SHEET,
        DocumentTypeId.LORE, DocumentTypeId.ENCOUNTER,
    }
    assert wire.category_types(LibraryCategory.DOCUMENTS, DocumentTypeId.LORE) == [DocumentTypeId.LORE]


# ── W-5 and W-6: reading, and writing over, a stored document ────────────────


def test_a_stored_document_becomes_the_wire_document() -> None:
    record = _record({"name": "Mira", "voice": "low", "secret_ally": "undeclared"}, archived=True)
    document = wire.to_document(record)
    assert document.data == {"name": "Mira", "voice": "low"}, "an undeclared stored key is dropped"
    assert (document.archived, document.version.sealed, document.write_revision) == (True, False, 3)
    assert wire.to_document(replace(record, version=_version(sealed=True))).version.sealed is True
    assert wire.to_document(replace(record, archived_at=None)).archived is False
    moment = datetime(2026, 9, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert wire.to_document(replace(record, updated_at=moment)).updated_at == T0
    assert wire.to_document(replace(record, updated_at=moment)).updated_at.tzinfo == UTC


def test_a_snapshot_and_a_library_row_become_their_wire_models() -> None:
    snap = VersionSnapshot(_version(2, sealed=True, restored_from=1), "statblock", 1, {"name": "Ogre", "hp": 59})
    wired = wire.to_snapshot(snap)
    assert (wired.version.number, wired.version.restored_from, wired.data) == (2, 1, {"name": "Ogre", "hp": 59})
    row = LibraryRow(id=DOC, type="npc", type_version=1, name="Mira", qualifier=None, tags=("a",),
                     archived_at=None, updated_at=T0)
    item = wire.to_library_item(row)
    assert (item.title, item.qualifier, item.tags, item.archived) == ("Mira", "", ["a"], False)


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (_record({"name": "X"}, doc_type="spellbook"), "type"),
        (_record({"name": "X"}, type_version=2), "type_version"),
        (_record("x"), "not_an_object"),
        (_record({"voice": "no name"}), "unreadable"),
    ],
)
def test_a_stored_document_this_build_cannot_read_is_unsupported(record: DocumentRecord, reason: str) -> None:
    with pytest.raises(wire.DocumentUnsupported) as refused:
        wire.to_document(record)
    assert refused.value.reason == reason
    with pytest.raises(wire.DocumentUnsupported):
        wire.writable(record)


@pytest.mark.parametrize(
    "data",
    [
        {"name": "Mira", "secret_ally": "x"},  # a top-level key
        {"name": "Mira", "portrait": {**ASSET, "focal": 3}},  # an asset sub-key
    ],
)
def test_writable_refuses_what_the_tolerant_read_accepts(data: dict[str, Any]) -> None:
    record = _record(data)
    assert wire.readable(record)["name"] == "Mira"
    with pytest.raises(wire.DocumentUnsupported) as refused:
        wire.writable(record)
    assert refused.value.reason == "undeclared_key"


@pytest.mark.parametrize(
    "data",
    [
        {"name": "Ogre", "traits": [{"name": "Keen", "text": "x", "icon": "eye"}]},  # an entry sub-key
        {"name": "Ogre", "abilities": {"str": 10, "luck": 3}},  # an ability key
    ],
)
def test_writable_refuses_an_undeclared_sub_key_of_a_stat_block(data: dict[str, Any]) -> None:
    record = _record(data, doc_type="statblock")
    assert wire.readable(record)["name"] == "Ogre"
    with pytest.raises(wire.DocumentUnsupported) as refused:
        wire.writable(record)
    assert refused.value.reason == "undeclared_key"


def test_writable_does_not_apply_required_and_returns_validated_values() -> None:
    stored = wire.writable(_record({"name": "Ogre", "portrait": dict(ASSET)}, doc_type="npc"))
    assert isinstance(stored["portrait"], AssetRef)
    assert wire.writable(_record({"name": "Ogre"}, doc_type="statblock")) == {"name": "Ogre"}, (
        "a stat block a defect left without hp is still writable; the merged whole is the store's to judge"
    )


# ── W-7: the end of history ──────────────────────────────────────────────────


def _versions(newest: int, oldest: int) -> list[VersionRecord]:
    return [_version(number, sealed=True) for number in range(newest, oldest - 1, -1)]


def test_history_ends_exactly_at_version_one() -> None:
    assert wire.to_history_page(DOC, _versions(20, 1)).next_cursor is None, "20 versions at 20 a page"
    first = wire.to_history_page(DOC, _versions(21, 2))
    assert first.next_cursor is not None
    assert wire.decode_history_cursor(first.next_cursor, DOC) == 2
    assert wire.to_history_page(DOC, _versions(20, 1)[:0]).next_cursor is None
    second = wire.to_history_page(DOC, _versions(20, 1))
    assert [item.number for item in second.items][-1] == 1 and second.next_cursor is None


def test_a_library_page_decides_next_cursor_before_skipping_a_row() -> None:
    good = LibraryRow(id=DOC, type="npc", type_version=1, name="Mira", qualifier="", tags=(),
                      archived_at=None, updated_at=T0)
    bad = replace(good, id=OTHER_DOC, name="")
    page, skipped = wire.to_library_page(CAMPAIGN, LibraryCategory.NPCS, [good, bad], 2)
    assert skipped == 1 and [item.document_id for item in page.items] == [DOC]
    assert page.next_cursor is not None and wire.decode_library_cursor(page.next_cursor) == OTHER_DOC
    short, _ = wire.to_library_page(CAMPAIGN, LibraryCategory.NPCS, [good], 2)
    assert short.next_cursor is None
    assert (page.campaign_id, page.category) == (CAMPAIGN, LibraryCategory.NPCS)

"""The projection builder and the slot resolver, over the twin alone
(agent-forge-harness-1kg.7.2 PR-2).

What both worlds share is `tests/test_table_snapshot_db.py`'s. What is here is
what only a twin can stage cheaply: a builder that refuses, a store that hands
back a version that is not sealed, a document store that must never be asked for
anything but a pinned snapshot (C-6b), and an emitter whose frames do not
validate (F-6). Every test names the mutation it kills (`# kills:`).
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from service import reveals as reveals_mod
from service import table_reads
from service.document_store import InMemoryDocumentStore, VersionSnapshot
from service.reveal_scope import TableAudience
from service.reveals import DisplayCommand, check_mask
from service.table_projection import build
from service.table_reads import TableReads
from service.tests.test_reveals_api import Rig
from service.workbench_contracts import DocumentTypeId, FieldKind, TableSnapshot, revealable_fields

SERVICE = Path(table_reads.__file__).parent
A_CANARY = "Canary-Value-7e21"
A_STATBLOCK = {
    "name": "Ogre",
    "ac": 11,
    "hp": 59,
    "abilities": {"str": 19, "dex": 8},
    "traits": [{"name": "Brute", "text": "Hits hard"}],
    "senses": A_CANARY,
}


@pytest.fixture
def rig() -> Iterator[Rig]:
    yield Rig()


def _show(rig: Rig, document: str, mask: tuple[str, ...] = ("name",)) -> None:
    rig.service.display(
        DisplayCommand(
            rig.campaign, rig.session, "cmd_" + "a" * 16, rig.twin.epoch(), document, 1, mask, TableAudience()
        ),
        owner_id=1,
        now=datetime.now(UTC),
    )


def _slots(snapshot: TableSnapshot | None) -> dict[str, Any]:
    assert snapshot is not None
    [frame] = [f for f in snapshot.frames if f.event == "snapshot"]
    return {slot.slot.value: slot.content for slot in frame.slots}


def _owner_reads(rig: Rig, documents: Any = None) -> TableSnapshot | None:
    reads = TableReads(rig.twin.db, rig.rows, documents or rig.twin.documents)
    return reads.snapshot(rig.campaign, now=datetime.now(UTC), account_id=1)


# ── The builder ──────────────────────────────────────────────────────────────


def test_the_builder_copies_only_the_masked_keys_in_mask_order() -> None:
    """kills: building from the whole document; sorting the mask."""
    made = build("statblock", A_STATBLOCK, ("traits", "ac", "abilities", "name"))
    assert made is not None
    assert [field.key for field in made.fields] == ["traits", "ac", "abilities", "name"]
    assert A_CANARY not in made.model_dump_json()


@pytest.mark.parametrize(
    ("doc_type", "data", "mask"),
    [
        ("scroll", {"name": "x"}, ("name",)),
        ("npc", {"name": "Vashti"}, ("name", "voice")),
        ("npc", {"name": "   "}, ("name",)),
        ("statblock", {"traits": [{"name": "Brute", "text": " "}]}, ("traits",)),
        ("npc", {"name": "Vashti", "true_identity": A_CANARY}, ("true_identity",)),
        ("npc", {"portrait": {"asset_id": "ast_" + "a" * 22, "media_type": "image", "alt": "A woman"}}, ("portrait",)),
    ],
)
def test_the_builder_refuses_what_a_table_may_not_be_shown_and_logs_a_closed_label(
    doc_type: str, data: dict[str, Any], mask: tuple[str, ...], caplog: pytest.LogCaptureFixture
) -> None:
    """kills: skipping a missing key; showing a withheld key or a GM asset; a log
    line that names a key, a value or an id."""
    with caplog.at_level(logging.DEBUG):
        assert build(doc_type, data, mask) is None
    [record] = caplog.records
    closed = {"table projection refused (type=unknown)", f"table projection refused (type={doc_type})"}
    assert record.getMessage() in closed
    assert A_CANARY not in caplog.text and "Vashti" not in caplog.text and "ast_" not in caplog.text


_VALUES: list[Any] = [
    "Vashti", "  ", "", "two\nlines", "x" * 5000, ["a"], [], ["a", " "], 3, 0, True, "3", None,
    {"str": 10}, {}, {"str": "10"}, {"zzz": 1}, {"str": 10, "int": 99999},
    [{"name": "Brute", "text": "Hits hard"}], [{"name": "Brute", "text": " "}], [{"name": "B"}],
    [{"name": "B", "text": "t", "extra": 1}], {"asset_id": "ast_" + "a" * 22, "media_type": "image", "alt": "A"},
]  # fmt: skip


def test_whatever_a_confirm_admits_the_builder_can_build() -> None:
    """C-6(a), ID-16: the projection is built at read from the pinned sealed text,
    so it must express everything `check_mask` let a Confirm pin. Over every type,
    every revealable key and a spread of stored values, while no table asset route
    exists. Kills: loosening a projection shape without loosening
    `_PROJECTION_VALUE` (a key `check_mask` admits that `build` then refuses)."""
    assert reveals_mod.TABLE_ASSETS_SERVED is False
    admitted = {kind: 0 for kind in FieldKind}
    for doc_type in DocumentTypeId:
        for key, kind in revealable_fields(doc_type).items():
            for value in _VALUES:
                data = {key: value}
                if check_mask(doc_type, data, (key,)) != ():
                    continue
                admitted[kind] += 1
                assert build(doc_type.value, data, (key,)) is not None, (doc_type, key, kind)
    assert [kind for kind, count in admitted.items() if count == 0 and kind is not FieldKind.ASSET] == [], (
        "every non-asset kind was admitted at least once, so the property is not vacuous"
    )
    assert admitted[FieldKind.ASSET] == 0, "no asset is admitted while no handle route exists"


# ── The resolver ─────────────────────────────────────────────────────────────


def test_an_empty_slot_has_no_content_and_a_shown_one_has_only_its_masked_field(rig: Rig) -> None:
    """kills: reading content for an empty slot."""
    empty = _owner_reads(rig)
    assert _slots(empty) == {"table": None}
    _show(rig, rig.document)
    shown = _slots(_owner_reads(rig))["table"]
    assert [(f.key, f.value) for f in shown.fields] == [("name", "Vashti")]


def test_one_principal_is_named(rig: Rig) -> None:
    """kills: reading as nobody, or as two principals at once."""
    reads = TableReads(rig.twin.db, rig.rows, rig.twin.documents)
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="one principal"):
        reads.snapshot(rig.campaign, now=now)
    with pytest.raises(ValueError, match="one principal"):
        reads.snapshot(rig.campaign, now=now, account_id=1, grant_id="tcr_" + "a" * 22)


class _Pinned(InMemoryDocumentStore):
    """A document store that answers a snapshot as it is told and refuses every
    other read: the resolver may ask for nothing else (C-6b)."""

    def __init__(self, inner: InMemoryDocumentStore, change: Any = None) -> None:
        self._inner = inner
        self._change = change
        self.asked: list[str] = []

    def snapshot(self, unit: Any, campaign_id: str, document_id: str, version_number: int) -> VersionSnapshot | None:
        self.asked.append("snapshot")
        found = self._inner.snapshot(unit, campaign_id, document_id, version_number)
        return found if self._change is None or found is None else self._change(found)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a table read asked for a document's live state")

    def hold(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a table read took a document hold")

    def list_documents(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a table read listed documents")


def test_the_resolver_reads_text_only_through_the_pinned_snapshot(rig: Rig) -> None:
    """C-6(b). Kills: `DocumentStore.get`, `hold` or `list_documents` anywhere in
    the read path (the fake store raises on each)."""
    _show(rig, rig.document)
    pinned = _Pinned(rig.twin.documents)
    shown = _slots(_owner_reads(rig, pinned))["table"]
    assert shown is not None and pinned.asked == ["snapshot"]


@pytest.mark.parametrize(
    "change",
    [
        lambda snap: replace(snap, version=replace(snap.version, sealed_at=None)),
        lambda snap: replace(snap, type="statblock"),
    ],
    ids=["an unsealed version", "a type the disclosure did not record"],
)
def test_a_version_that_is_not_sealed_or_not_of_the_recorded_type_shows_nothing(rig: Rig, change: Any) -> None:
    """C-6(b). Kills: dropping the sealed check or the type check."""
    _show(rig, rig.document)
    assert _slots(_owner_reads(rig, _Pinned(rig.twin.documents, change))) == {"table": None}


def test_a_snapshot_that_does_not_validate_is_emitted_with_every_slot_empty(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F-6, fail closed. The frames are refused whenever one carries content, and
    the same frames are emitted empty, with a log line that names the exception's
    type and no text. Kills: emitting the refused frames; logging `str(exc)`."""
    _show(rig, rig.document)
    real = TableSnapshot.model_validate

    class Refusing:
        @staticmethod
        def model_validate(value: Any) -> TableSnapshot:
            if any(getattr(f, "event", "") == "snapshot" and any(s.content for s in f.slots) for f in value["frames"]):
                return TableSnapshot.model_validate({"schema_version": 2, "frames": []})
            return real(value)

    monkeypatch.setattr(table_reads, "TableSnapshot", Refusing)
    with caplog.at_level(logging.DEBUG):
        emptied = _owner_reads(rig)
    assert _slots(emptied) == {"table": None}
    assert "ValidationError" in caplog.text and "Vashti" not in caplog.text and "schema_version" not in caplog.text


# ── What the source must say ─────────────────────────────────────────────────

_FORBIDDEN_READS = {"get", "hold", "list_documents"}


def _document_reads(source: str) -> list[str]:
    return [
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and node.attr in _FORBIDDEN_READS
        and "documents" in ast.unparse(node.value)
    ]


@pytest.mark.parametrize("module", ["table_reads.py", "table_projection.py"])
def test_the_read_path_names_no_document_read_but_the_snapshot(module: str) -> None:
    """C-6(b), the structural half, with a positive control. Kills: a later
    `self._documents.get(...)`."""
    source = (SERVICE / module).read_text(encoding="utf-8")
    assert _document_reads(source) == []
    assert _document_reads(source + "\nself._documents.get(unit, c, d)\n") == ["get"]
    assert "snapshot" in (SERVICE / "table_reads.py").read_text(encoding="utf-8")


def test_only_the_resolver_among_the_table_modules_imports_the_document_store() -> None:
    """T-23, C-6(c): `table_reads` is the one table module that reaches stored
    documents, and the projection module imports no store at all."""
    importers = sorted(
        path.name
        for path in SERVICE.glob("table_*.py")
        if "document_store" in path.read_text(encoding="utf-8") and not path.name.startswith("table_reads")
    )
    assert importers == []
    assert "document_store" in (SERVICE / "table_reads.py").read_text(encoding="utf-8")
    assert "_store" not in (SERVICE / "table_projection.py").read_text(encoding="utf-8")

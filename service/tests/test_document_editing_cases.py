"""The AI document-edit eval cases (1kg.5.5, group Q).

`ingestion/eval_data/document_edit/cases.jsonl` is synthetic input for a live
qualification run (`1kg.4.6` owns the runner). Here its shape, coverage and
licence are checked, and every case is built into messages offline.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from service import document_editing as de
from service.tests import document_editing_fixtures as fx
from service.workbench_contracts import (
    DOC_TYPE_VERSION,
    ActionInstruction,
    DocumentScope,
    DocumentTypeId,
    FieldScope,
    SelectionScope,
    TextInstruction,
    check_fields,
)

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "ingestion" / "eval_data" / "document_edit" / "cases.jsonl"
LICENCE = "Original to this repository (synthetic); contains no licensed book text"
SCOPE_KINDS = ("document", "field", "selection")
SHINGLE_WORDS = 12
I13_MARKERS = de.REMOTE_REFERENCE_MARKERS


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CaseDocumentScope(_Strict):
    kind: Literal["document"]


class CaseFieldScope(_Strict):
    kind: Literal["field"]
    field: str


class CaseSelectionScope(_Strict):
    kind: Literal["selection"]
    field: str
    start: int
    end: int
    text: str


class CaseTextInstruction(_Strict):
    kind: Literal["text"]
    text: str


class Expect(_Strict):
    changed_subset_of: list[str]
    must_mention: list[str]
    forbid_substrings: list[str]
    forbid_facts: list[str]
    unchanged_keys: list[str]


class EvalCase(_Strict):
    id: str = Field(pattern=r"^de-(document|field|selection)-\d{3}$")
    doc_type: str
    scope: CaseDocumentScope | CaseFieldScope | CaseSelectionScope = Field(discriminator="kind")
    instruction: CaseTextInstruction
    data: dict[str, Any]
    category: Literal["representative", "edge", "injection"]
    injection_family: str | None
    source: Literal["synthetic"]
    licence: Literal["Original to this repository (synthetic); contains no licensed book text"]
    expect: Expect

    @model_validator(mode="after")
    def _consistent(self) -> EvalCase:
        assert self.id.startswith(f"de-{self.scope.kind}-")
        assert (self.category == "injection") == (self.injection_family is not None)
        assert self.injection_family is None or self.injection_family in fx.EDIT_INJECTION_FAMILIES
        doc_type = DocumentTypeId(self.doc_type)
        checked = check_fields(doc_type, DOC_TYPE_VERSION[doc_type], dict(self.data), whole=True)
        assert checked is not None
        scope_keys = _scope_keys(doc_type, self.scope)
        assert self.expect.changed_subset_of
        assert set(self.expect.changed_subset_of) <= scope_keys
        assert set(I13_MARKERS) <= set(self.expect.forbid_substrings)
        if self.category == "representative":
            assert self.expect.must_mention
        if isinstance(self.scope, CaseSelectionScope):
            value = self.data.get(self.scope.field)
            assert isinstance(value, str)
            assert self.scope.end - self.scope.start == len(self.scope.text)
            assert value[self.scope.start : self.scope.end] == self.scope.text
        return self


def _scope_keys(doc_type: DocumentTypeId, scope: CaseDocumentScope | CaseFieldScope | CaseSelectionScope) -> set[str]:
    if scope.kind == "document":
        return set(de.ai_editable_keys(doc_type))
    return {scope.field}


@cache
def cases() -> tuple[EvalCase, ...]:
    lines = CASES.read_text(encoding="utf-8").splitlines()
    return tuple(EvalCase.model_validate_json(line) for line in lines if line.strip())


def as_target(case: EvalCase) -> de.EditTarget:
    doc_type = DocumentTypeId(case.doc_type)
    instruction: TextInstruction | ActionInstruction = TextInstruction(kind="text", text=case.instruction.text)
    scope: DocumentScope | FieldScope | SelectionScope
    if isinstance(case.scope, CaseDocumentScope):
        scope = DocumentScope(kind="document")
    elif isinstance(case.scope, CaseFieldScope):
        scope = FieldScope(kind="field", field=case.scope.field)
    else:
        scope = SelectionScope(
            kind="selection", field=case.scope.field, start=case.scope.start, end=case.scope.end, text=case.scope.text
        )
    return de.EditTarget(
        doc_type=doc_type, type_version=DOC_TYPE_VERSION[doc_type], data=dict(case.data), scope=scope,
        instruction=instruction,
    )


def test_q1_every_line_is_a_well_formed_case() -> None:
    """Kills malformed data: ids unique, every field typed and consistent."""
    loaded = cases()
    assert len(loaded) >= 18
    assert len({c.id for c in loaded}) == len(loaded)
    with pytest.raises(ValueError):
        EvalCase.model_validate({**loaded[0].model_dump(), "category": "injection"})
    with pytest.raises(ValueError):
        EvalCase.model_validate({**loaded[0].model_dump(), "surprise": 1})
    # A patched scope out of the changed_subset_of bound is caught too.
    bad = loaded[0].model_dump()
    bad["expect"]["changed_subset_of"] = ["not_a_real_key_at_all"]
    with pytest.raises(ValueError):
        EvalCase.model_validate(bad)


def test_q2_every_scope_kind_and_every_family_is_covered() -> None:
    """Kills coverage loss."""
    by_scope = Counter((c.scope.kind, c.category) for c in cases())
    for kind in SCOPE_KINDS:
        total = sum(count for (k, _), count in by_scope.items() if k == kind)
        injections = sum(count for (k, cat), count in by_scope.items() if k == kind and cat == "injection")
        assert total >= 6, kind
        assert injections >= 2, kind
    assert {c.injection_family for c in cases()} - {None} == set(fx.EDIT_INJECTION_FAMILIES)
    types = {c.doc_type for c in cases()}
    assert {"npc", "session-notes", "lore", "handout"} <= types
    statblock_scopes = {c.scope.kind for c in cases() if c.doc_type == "statblock"}
    assert {"document", "field"} <= statblock_scopes
    assert all(c.expect.must_mention for c in cases() if c.category == "representative")


def _shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i : i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def _case_text(case: EvalCase) -> str:
    parts = [case.instruction.text, *(str(v) for v in case.data.values())]
    return "\n".join(parts)


def test_q3_the_set_is_synthetic_and_shares_no_run_with_any_book() -> None:
    """Kills licensed text in the set; a copied run is caught (positive control)."""
    assert all(c.source == "synthetic" and c.licence == LICENCE for c in cases())
    books = sorted((ROOT / "ingestion").glob("chunks*.jsonl"))
    assert len(books) >= 14 and "chunks-mm-5e.jsonl" in {b.name for b in books}
    ours: dict[str, str] = {}
    for case in cases():
        for shingle in _shingles(_case_text(case)):
            ours.setdefault(shingle, case.id)
    control_words: list[str] = []
    hits: list[tuple[str, str]] = []
    words_read = 0
    for path in books:
        with path.open(encoding="utf-8") as lines:
            for line in lines:
                text = json.loads(line)["text"]
                words_read += len(text.split())
                if not control_words and path.name == "chunks-mm-5e.jsonl" and len(text.split()) >= SHINGLE_WORDS:
                    control_words = re.findall(r"[a-z0-9]+", text.lower())[:SHINGLE_WORDS]
                    ours.setdefault(" ".join(control_words), "control")
                hits += [(ours[s], path.name) for s in _shingles(text.replace("\n", " ")) & ours.keys()]
    assert words_read > 0 and len(control_words) == SHINGLE_WORDS
    assert {case for case, _ in hits} == {"control"}, hits


@pytest.mark.parametrize("case", cases(), ids=[c.id for c in cases()])
def test_q4_every_case_builds_offline_within_the_payload_bound(case: EvalCase) -> None:
    """Kills cases that cannot run, and an injection payload that leaks outside
    the fenced data block."""
    target = as_target(case)
    assert de.check_scope(target) is None, case.id
    system_message, human_message = de.build_edit_messages(target, new_nonce=lambda: "9" * 24)
    system = str(system_message.content)
    human = str(human_message.content)
    opening, closing = de.data_tags("9" * 24)
    assert human.startswith(opening) and human.count(opening) == 1 and human.count(closing) == 1
    body = human.partition(opening)[2].partition(closing)[0]
    payload = json.loads(body)
    assert payload["instruction"] == case.instruction.text
    if case.category == "injection":
        assert case.instruction.text not in system
        # The hostile text reached the model only inside the fenced data block,
        # never the system prompt: `payload["instruction"]` above already proves
        # it round-trips through the block's JSON exactly (quotes escaped and all).

"""The document-generation eval cases (1kg.5.4, group L).

`ingestion/eval_data/document_generation/cases.jsonl` is synthetic input for a
live qualification run (`1kg.4.6` owns the runner). Here its shape, coverage
and licence are checked, and every case is built into messages offline.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from service import document_generation as dg
from service.models import Source
from service.tests import document_generation_fixtures as fx
from service.workbench_contracts import DocumentTypeId
from service.workbench_registry import REGISTRY

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "ingestion" / "eval_data" / "document_generation" / "cases.jsonl"
LICENCE = "Original to this repository (synthetic); contains no licensed book text"
TYPES = tuple(t.value for t in dg.GENERATION_SPECS)
SHINGLE_WORDS = 12
I16_MARKERS = ("://", "![", "](", "<img", "<a ", "<iframe", "<script", "href=", "src=")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CaseCampaign(_Strict):
    name: str
    tone: str | None


class CaseTurn(_Strict):
    speaker: Literal["gm", "assistant"]
    text: str


class CasePassage(_Strict):
    text: str
    source: Source


class CaseRequest(_Strict):
    brief: str
    campaign: CaseCampaign | None
    thread: list[CaseTurn]
    source_result: str | None
    corpus: list[CasePassage]
    preset: dict[str, object]


class Expect(_Strict):
    basis: list[Literal["invented", "mixed", "thread"]] = Field(min_length=1)
    must_fill: list[str]
    forbid_substrings: list[str]
    max_cited: int = Field(ge=0)
    must_mention: list[str]
    forbid_facts: list[str]


class EvalCase(_Strict):
    id: str = Field(pattern=r"^dg-(npc|encounter|session-notes)-\d{3}$")
    doc_type: str
    category: Literal["representative", "edge", "injection"]
    injection_family: str | None
    source: Literal["synthetic"]
    licence: Literal["Original to this repository (synthetic); contains no licensed book text"]
    request: CaseRequest
    expect: Expect

    @model_validator(mode="after")
    def _consistent(self) -> EvalCase:
        assert self.doc_type in TYPES and self.id.startswith(f"dg-{self.doc_type}-")
        assert (self.category == "injection") == (self.injection_family is not None)
        assert self.injection_family is None or self.injection_family in fx.INJECTION_FAMILIES
        spec = dg.spec_for(self.doc_type)
        assert set(self.expect.must_fill) >= spec.must_fill
        assert set(self.expect.forbid_substrings) >= set(I16_MARKERS)
        assert self.expect.max_cited <= len(self.request.corpus)
        return self


@cache
def cases() -> tuple[EvalCase, ...]:
    lines = CASES.read_text(encoding="utf-8").splitlines()
    return tuple(EvalCase.model_validate_json(line) for line in lines if line.strip())


def as_request(case: EvalCase) -> dg.GenerationRequest:
    r = case.request
    return dg.GenerationRequest(
        doc_type=DocumentTypeId(case.doc_type),
        brief=r.brief,
        campaign=None if r.campaign is None else dg.CampaignFacts(r.campaign.name, r.campaign.tone),
        thread=tuple(dg.ThreadTurn(t.speaker, t.text) for t in r.thread),
        source_result=r.source_result,
        corpus=tuple(dg.CorpusPassage(p.text, p.source) for p in r.corpus),
        preset=dict(r.preset),
    )


def test_l1_every_line_is_a_well_formed_case() -> None:
    """Kills malformed data: ids unique, every field typed and consistent."""
    loaded = cases()
    assert len(loaded) >= 18
    assert len({c.id for c in loaded}) == len(loaded)
    with pytest.raises(ValueError):
        EvalCase.model_validate({**loaded[0].model_dump(), "category": "injection"})
    with pytest.raises(ValueError):
        EvalCase.model_validate({**loaded[0].model_dump(), "surprise": 1})


def test_l2_every_type_and_every_family_is_covered() -> None:
    """Kills coverage loss (C-12's four fixture kinds included)."""
    by_type = Counter((c.doc_type, c.category) for c in cases())
    for doc_type in TYPES:
        assert by_type[(doc_type, "representative")] >= 3
        assert by_type[(doc_type, "edge")] >= 1
        assert by_type[(doc_type, "injection")] >= 2
        briefs = [len(c.request.brief) for c in cases() if c.doc_type == doc_type and c.category == "edge"]
        assert min(briefs) <= 10 and max(briefs) == 2000, "a minimal brief and one at the bound"
        creative = dg.spec_for(doc_type).basis_kind == "creative"
        typed = [c for c in cases() if c.doc_type == doc_type]
        assert all(c.expect.forbid_facts for c in typed) if not creative else any(c.expect.forbid_facts for c in typed)
    assert {c.injection_family for c in cases()} - {None} == set(fx.INJECTION_FAMILIES)
    assert all(c.expect.must_mention for c in cases() if c.category == "representative")


def _shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i:i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def _case_text(case: EvalCase) -> str:
    r = case.request
    parts = [r.brief, r.source_result or "", *(t.text for t in r.thread), *(p.text for p in r.corpus)]
    return "\n".join(parts + ([r.campaign.name, r.campaign.tone or ""] if r.campaign else []))


def test_l3_the_set_is_synthetic_and_shares_no_run_with_any_book() -> None:
    """Kills licensed text in the set; a copied run is caught (positive control)."""
    assert all(c.source == "synthetic" and c.licence == LICENCE for c in cases())
    assert {p.source.book for c in cases() for p in c.request.corpus} == {fx.SYNTHETIC_BOOK}
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
def test_l4_every_case_builds_offline_with_its_untrusted_text_inside_the_block(case: EvalCase) -> None:
    """Kills cases that cannot run."""
    request = as_request(case)
    assert dg.context_size(request) <= dg.GENERATION_CONTEXT_MAX_CHARS
    system_message, human_message = dg.build_messages(request, new_nonce=lambda: "9" * 24)
    system, human = str(system_message.content), str(human_message.content)
    opening, closing = dg.data_tags("9" * 24)
    head, body, tail, closing_line = human.split("\n")
    assert (head, tail) == (opening, closing) and json.loads(body)["brief"] == request.brief.strip()
    doc = REGISTRY.document_type(case.doc_type)
    assert doc is not None and closing_line == f"Write the {doc.label} the brief describes."
    if case.category == "injection":
        planted = [text for text in _case_text(case).split("\n") if text]
        assert not [text for text in planted if text[:30] in system]


def test_l5_every_generatable_type_has_fixtures_and_six_cases() -> None:
    """Kills an untested generatable type: a spec added without them fails here."""
    counts = Counter(c.doc_type for c in cases())
    for doc_type in dg.GENERATION_SPECS:
        assert counts[doc_type.value] >= 6
        assert any(v.doc_type is doc_type for v in fx.VALID_CASES)
        assert len([v for v in fx.VALID_CASES if v.doc_type is doc_type]) >= 3
        assert any(i.doc_type is doc_type for i in fx.INVALID_CASES)

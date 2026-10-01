"""The card-tools eval cases (agent-forge-harness-1kg.4.3, group Q).

`ingestion/eval_data/card_tools/cases.jsonl` is synthetic input for a live
qualification run (`1kg.4.6` owns the runner). Here its shape, coverage and
licence are checked, and every case is built into messages offline.
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

from service import card_generation as cg
from service.models import Source
from service.tests import card_generation_fixtures as fx
from service.workbench_contracts import ToolId

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "ingestion" / "eval_data" / "card_tools" / "cases.jsonl"
LICENCE = "Original to this repository (synthetic); contains no licensed book text"
TOOLS = tuple(t.value for t in fx.CARD_TOOLS)
SHINGLE_WORDS = 12
INJECTION_FAMILIES = frozenset(fx.INJECTION_FAMILIES)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CasePassage(_Strict):
    text: str
    source: Source


class CaseRequest(_Strict):
    brief: str
    passages: list[CasePassage]


class Expect(_Strict):
    forbid_substrings: list[str]
    must_mention: list[str]
    forbid_facts: list[str]
    must_cite: bool | None
    expect: Literal["not_in_sources"] | None


class EvalCase(_Strict):
    id: str = Field(pattern=r"^ct-(monster|loot|names|rules|hooks)-\d{3}$")
    tool: str
    category: Literal["representative", "edge", "injection"]
    injection_family: str | None
    source: Literal["synthetic"]
    licence: Literal["Original to this repository (synthetic); contains no licensed book text"]
    request: CaseRequest
    expect: Expect

    @model_validator(mode="after")
    def _consistent(self) -> EvalCase:
        assert self.tool in TOOLS and self.id.startswith(f"ct-{self.tool}-")
        assert (self.category == "injection") == (self.injection_family is not None)
        assert self.injection_family is None or self.injection_family in INJECTION_FAMILIES
        assert not self.request.passages or self.tool == "rules", "only rules takes passages"
        assert self.expect.must_cite is None or self.tool == "rules"
        assert self.expect.expect is None or self.tool == "rules"
        assert set(self.expect.forbid_substrings) >= set(cg.REMOTE_REFERENCE_MARKERS)
        return self


@cache
def cases() -> tuple[EvalCase, ...]:
    lines = CASES.read_text(encoding="utf-8").splitlines()
    return tuple(EvalCase.model_validate_json(line) for line in lines if line.strip())


def as_request(case: EvalCase) -> cg.CardRequest:
    passages = tuple(cg.CorpusPassage(p.text, p.source) for p in case.request.passages)
    return cg.CardRequest(tool=ToolId(case.tool), brief=case.request.brief, passages=passages)


def test_q1_every_line_is_a_well_formed_case() -> None:
    """Kills malformed data: ids unique, every field typed and consistent."""
    loaded = cases()
    assert len(loaded) >= 30
    assert len({c.id for c in loaded}) == len(loaded)
    with pytest.raises(ValueError):
        EvalCase.model_validate({**loaded[0].model_dump(), "category": "injection"})
    with pytest.raises(ValueError):
        EvalCase.model_validate({**loaded[0].model_dump(), "surprise": 1})


def test_q2_every_tool_and_every_family_is_covered() -> None:
    """Kills coverage loss."""
    by_tool = Counter((c.tool, c.category) for c in cases())
    for tool in TOOLS:
        assert by_tool[(tool, "representative")] >= 3
        assert by_tool[(tool, "edge")] >= 1
        assert by_tool[(tool, "injection")] >= 2
        briefs = [len(c.request.brief) for c in cases() if c.tool == tool and c.category == "edge"]
        assert min(briefs) <= 10 and max(briefs) == 2000, "a minimal brief and one at the bound"
    assert {c.injection_family for c in cases()} - {None} == INJECTION_FAMILIES
    assert any(c.expect.must_cite for c in cases() if c.tool == "rules")
    assert any(c.expect.expect == "not_in_sources" for c in cases() if c.tool == "rules")
    assert all(c.expect.must_mention for c in cases() if c.category == "representative" and c.tool != "rules")


def _shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i:i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def _case_text(case: EvalCase) -> str:
    r = case.request
    return "\n".join([r.brief, *(p.text for p in r.passages)])


def test_q3_the_set_is_synthetic_and_shares_no_run_with_any_book() -> None:
    """Kills licensed text in the set; a copied run is caught (positive control)."""
    assert all(c.source == "synthetic" and c.licence == LICENCE for c in cases())
    assert {p.source.book for c in cases() for p in c.request.passages} == {fx.SYNTHETIC_BOOK}
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
def test_q4_every_case_builds_offline_with_its_untrusted_text_inside_the_block(case: EvalCase) -> None:
    """Kills cases that cannot run. A `not_in_sources` case has no passages by
    design: the real gate that gives it that outcome runs before generation,
    inside `RulesExecutor` (I-7), so there is nothing here for `build_messages`
    itself to accept or refuse."""
    if case.expect.expect == "not_in_sources":
        assert case.request.passages == []
        return
    request = as_request(case)
    assert cg.context_size(request) <= cg.GENERATION_CONTEXT_MAX_CHARS
    system_message, human_message = cg.build_messages(request, new_nonce=lambda: "9" * 24)
    system, human = str(system_message.content), str(human_message.content)
    opening, closing = cg.data_tags("9" * 24)
    assert human.startswith(opening) and human.rstrip().endswith(
        cg._CLOSING_LINE.format(label=cg._ROLE_LABEL[request.tool]),
    )
    assert opening in human and closing in human
    if case.category == "injection":
        assert case.request.brief not in system

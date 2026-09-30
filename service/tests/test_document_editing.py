"""`service/document_editing.py` (1kg.5.5): group P, the engine, pure.

No database: every client is a fake. Each test names the mutant it exists to
kill (§10 of the alignment brief). Group Q (the eval cases) lives in
`test_document_editing_cases.py`.
"""

from __future__ import annotations

import ast
import json
import logging
import traceback
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from service import document_editing as de
from service import document_generation as dg
from service import generate, model_catalog, usage_capture
from service.tests import document_editing_fixtures as fx
from service.workbench_contracts import (
    DocumentTypeId,
    EditAction,
    FieldKind,
    SelectionScope,
)

Refusal = de.EditRefusal
Invalid = de.InvalidEditOutput
ALL_TYPES = tuple(DocumentTypeId)


# ── Harness ──────────────────────────────────────────────────────────────────


class FakeClient:
    """Answers from a script of texts and exceptions; records every call."""

    def __init__(self, *script: str | BaseException, finish_reason: str | None = None) -> None:
        self.script = list(script)
        self.finish_reason = finish_reason
        self.calls: list[tuple[Any, Any, dict[str, Any]]] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append((input, config, kwargs))
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        metadata = {"finish_reason": self.finish_reason} if self.finish_reason else {}
        return AIMessage(content=item, response_metadata=metadata)


class ObeyingClient:
    """A hostile-but-obedient model: it does exactly what the instruction
    (wherever it was smuggled in) asks, inside the correct envelope shape."""

    def __init__(self, builder: Any) -> None:
        self._builder = builder
        self.calls: list[Any] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append(input)
        return AIMessage(content=self._builder())


@dataclass
class Capture:
    config: Any
    attempts: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]
    _token: Any = None
    _ended: bool = False

    def end(self) -> None:
        if not self._ended:
            self._ended = True
            usage_capture.end_operation(self._token)


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[Capture]:
    attempts: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    emit_record, emit_outcome = usage_capture._emit_record, usage_capture._emit_outcome_record

    def record(operation: Any, fields: dict[str, Any]) -> None:
        attempts.append(dict(fields))
        emit_record(operation, fields)

    def outcome(operation: Any, fields: dict[str, Any]) -> None:
        outcomes.append(dict(fields))
        emit_outcome(operation, fields)

    monkeypatch.setattr(usage_capture, "_emit_record", record)
    monkeypatch.setattr(usage_capture, "_emit_outcome_record", outcome)
    monkeypatch.setattr(usage_capture, "_ledger_provider", lambda: None)
    token = usage_capture.begin_operation(mode="gm", billed_account_id=1, request=SimpleNamespace(headers={}))
    captured = Capture(usage_capture.run_config_with_operation(None), attempts, outcomes, token)
    yield captured
    captured.end()


def _edit(target: de.EditTarget, client: Any, config: Any = None, attempts: int = 1, **kwargs: Any) -> Any:
    return de.edit_document(target, client=client, alias="gpt-4o-mini", config=config, max_attempts=attempts, **kwargs)


def _refused(code: Refusal, call: Any) -> de.EditRefused:
    with pytest.raises(de.EditRefused) as caught:
        call()
    assert caught.value.code is code
    return caught.value


def _invalid(code: Invalid, call: Any) -> de.InvalidEdit:
    with pytest.raises(de.InvalidEdit) as caught:
        call()
    assert caught.value.code is code
    return caught.value


def _text(message: Any) -> str:
    content = message.content
    assert isinstance(content, str)
    return content


def _fields(target: de.EditTarget, patch: dict[str, Any], finish_reason: str | None = None) -> Any:
    return de.parse_edit(target, json.dumps({"fields": patch}), finish_reason=finish_reason)


def _replacement(target: de.EditTarget, text: str, finish_reason: str | None = None) -> Any:
    return de.parse_edit(target, json.dumps({"replacement": text}), finish_reason=finish_reason)


# ── P-1: ai_editable_keys ────────────────────────────────────────────────────


def test_p1_editable_keys_exclude_assets_and_tags_for_every_type() -> None:
    """Kills `tags` or an asset key made editable."""
    for doc_type in ALL_TYPES:
        declared = {**dg._declared(doc_type)}
        editable = de.ai_editable_keys(doc_type)
        assert "tags" not in editable
        assert all(declared[key] is not FieldKind.ASSET for key in editable)
        # Declaration order is preserved.
        assert editable == tuple(k for k in declared if k in editable)
        # Every non-asset, non-tags key IS editable.
        assert set(editable) == {k for k, kind in declared.items() if kind is not FieldKind.ASSET and k != "tags"}


# ── P-2: check_scope ─────────────────────────────────────────────────────────


def test_p2_field_scope_refuses_undeclared_tags_and_assets() -> None:
    for field in ("bogus_field", "tags", "portrait"):
        t = fx.target(fx.NPC, fx.field_scope(field))
        assert de.check_scope(t) is Refusal.FIELD_NOT_EDITABLE


@pytest.mark.parametrize("field", ["skills", "tags", "actions", "hp", "abilities"])
def test_p2_selection_refuses_non_prose_fields(field: str) -> None:
    """Kills the prose check being dropped for text, text_list, entry_list,
    integer and abilities fields."""
    row = fx.data_for(fx.STATBLOCK)
    value = row[field]
    text = str(value)[:1] or "x"
    t = fx.target(fx.STATBLOCK, fx.selection_scope(field, 0, len(text), text), data=row)
    code = de.check_scope(t)
    assert code in (Refusal.SELECTION_NOT_PROSE, Refusal.FIELD_NOT_EDITABLE, Refusal.SELECTION_MISMATCH)
    if field != "tags":
        assert code is Refusal.SELECTION_NOT_PROSE
    else:
        assert code is Refusal.FIELD_NOT_EDITABLE


def test_p2_selection_mismatch_on_span_past_the_value_or_wrong_text() -> None:
    row = fx.data_for(fx.NPC)
    value = row["wants"]
    past_end = fx.selection_scope("wants", 0, len(value) + 5, value + "xxxxx")
    assert de.check_scope(fx.target(fx.NPC, past_end, data=row)) is Refusal.SELECTION_MISMATCH
    wrong_text = fx.selection_scope("wants", 0, 4, "ZZZZ")
    assert de.check_scope(fx.target(fx.NPC, wrong_text, data=row)) is Refusal.SELECTION_MISMATCH
    missing_value = fx.selection_scope("wants", 0, 1, "Z")
    row2 = {**row, "wants": None}
    assert de.check_scope(fx.target(fx.NPC, missing_value, data=row2)) is Refusal.SELECTION_MISMATCH


def test_p2_selection_matches_in_code_points_across_an_emoji_and_cjk() -> None:
    """Kills UTF-16 or byte indexing: an emoji and a CJK character are each one
    Python code point, so a span counted any other way would misalign."""
    value = "before \U0001f3b2 龍 after"  # a die emoji (astral) and a CJK character
    row = fx.data_for(fx.NPC, wants=value)
    start, end = value.index("\U0001f3b2"), value.index("\U0001f3b2") + 1
    scope = fx.selection_scope("wants", start, end, value[start:end])
    t = fx.target(fx.NPC, scope, data=row)
    assert de.check_scope(t) is None
    start2, end2 = value.index("龍"), value.index("龍") + 1
    scope2 = fx.selection_scope("wants", start2, end2, value[start2:end2])
    t2 = fx.target(fx.NPC, scope2, data=row)
    assert de.check_scope(t2) is None


def test_p2_a_carriage_return_before_the_span_is_one_code_point() -> None:
    """Critic C-17: offsets count the stored value exactly, CRs included — a
    client that silently normalised CRLF to LF before counting would misalign
    every span after one."""
    value = "line one\r\nline two selected here"
    row = fx.data_for(fx.NPC, wants=value)
    start, end = value.index("selected"), value.index("selected") + len("selected")
    t = fx.target(fx.NPC, fx.selection_scope("wants", start, end, value[start:end]), data=row)
    assert de.check_scope(t) is None


def test_p2_a_blank_selection_is_refused_before_any_work() -> None:
    """Critic C-7: a selection whose trimmed text is empty is refused up
    front, so the splice never has to double up on it (I-12)."""
    row = fx.data_for(fx.NPC, wants="before   after")
    t = fx.target(fx.NPC, fx.selection_scope("wants", 6, 9, "   "), data=row)
    assert de.check_scope(t) is Refusal.SELECTION_BLANK


# ── P-3 / P-3b: the payload bound ────────────────────────────────────────────


def test_p3_field_scope_shrinks_context_before_refusing() -> None:
    small = fx.data_for(fx.NPC)
    fits = fx.target(fx.NPC, fx.field_scope("wants"), data=small)
    assert de.check_scope(fits) is None

    over = fx.data_for(fx.NPC, notes="n" * 24_500)
    shrunk = fx.target(fx.NPC, fx.field_scope("wants"), data=over)
    assert de.check_scope(shrunk) is None, "should shrink to name+qualifier and fit"
    body = _text(de.build_edit_messages(shrunk)[1])
    assert "n" * 100 not in body, "the huge context field must not have been sent"

    still_over = fx.data_for(fx.NPC, wants="w" * 24_500)
    refused = fx.target(fx.NPC, fx.field_scope("wants"), data=still_over)
    assert de.check_scope(refused) is Refusal.TOO_LARGE


def test_p3_document_scope_over_the_bound_refuses_with_no_shrinking() -> None:
    over = fx.data_for(fx.NPC, notes="n" * 30_000)
    t = fx.target(fx.NPC, fx.document_scope(), data=over)
    assert de.check_scope(t) is Refusal.TOO_LARGE
    _refused(Refusal.TOO_LARGE, lambda: de.build_edit_messages(t))


def test_p3_selection_scope_payload_bound_fires_on_a_huge_field_around_a_small_selection() -> None:
    """The echo bound (C-3) covers only the selected span; the `before` and
    `after` slices of a very long field are not context that shrinks, so the
    overall payload bound is the one that has to catch this."""
    huge = "w" * 24_500
    start, end = 12_000, 12_050
    row = fx.data_for(fx.NPC, wants=huge)
    t = fx.target(fx.NPC, fx.selection_scope("wants", start, end, huge[start:end]), data=row)
    assert de._echo_size(t) <= de.EDIT_ECHO_MAX_CHARS, "the selection itself is small"
    assert de.check_scope(t) is Refusal.TOO_LARGE


def test_p3b_the_edit_bound_equals_the_tool_paths_bound() -> None:
    from service import tool_invocations

    assert de.EDIT_CONTEXT_MAX_CHARS == tool_invocations.CONTEXT_MAX_CHARS


def test_p3c_the_echo_bound_is_the_selected_text_itself() -> None:
    """Critic C-3: at selection scope, only the span the model must echo is
    bounded — the ``json.dumps`` quoting the field's other context does not
    count against it."""
    fits = "w" * de.EDIT_ECHO_MAX_CHARS
    row = fx.data_for(fx.NPC, wants=fits)
    t = fx.target(fx.NPC, fx.selection_scope("wants", 0, len(fits), fits), data=row)
    assert de.check_scope(t) is None

    over = "w" * (de.EDIT_ECHO_MAX_CHARS + 1)
    row2 = fx.data_for(fx.NPC, wants=over)
    t2 = fx.target(fx.NPC, fx.selection_scope("wants", 0, len(over), over), data=row2)
    assert de.check_scope(t2) is Refusal.TOO_LARGE


def test_p3c_field_scope_echo_bound_is_the_fields_own_value_not_the_context() -> None:
    """The other AI-editable keys sit in ``context`` only; they must not count
    toward the field's own echo cost."""
    fits_value = "w" * (de.EDIT_ECHO_MAX_CHARS - 2)  # json.dumps adds the two quote characters
    row = fx.data_for(fx.NPC, wants=fits_value)
    t = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    assert de.check_scope(t) is None

    over_value = "w" * (de.EDIT_ECHO_MAX_CHARS - 1)
    row2 = fx.data_for(fx.NPC, wants=over_value)
    t2 = fx.target(fx.NPC, fx.field_scope("wants"), data=row2)
    assert de.check_scope(t2) is Refusal.TOO_LARGE


def test_p3c_document_scope_echo_bound_sums_every_ai_editable_key_present() -> None:
    small = fx.data_for(fx.NPC, wants="w" * 100, notes="n" * 100)
    assert de.check_scope(fx.target(fx.NPC, fx.document_scope(), data=small)) is None

    big = fx.data_for(fx.NPC, wants="w" * 6_000, notes="n" * 6_000)
    assert de.check_scope(fx.target(fx.NPC, fx.document_scope(), data=big)) is Refusal.TOO_LARGE


def test_p3c_a_long_prose_field_is_too_large_whole_but_a_short_selection_inside_it_builds() -> None:
    """The residual C-3 names: a 20,000-code-point prose field cannot be
    AI-edited whole, but a short selection inside it still can."""
    row = fx.data_for(fx.NPC, wants="w" * 20_000)
    whole_field = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    assert de.check_scope(whole_field) is Refusal.TOO_LARGE
    small_selection = fx.target(fx.NPC, fx.selection_scope("wants", 0, 2_000, row["wants"][:2_000]), data=row)
    assert de.check_scope(small_selection) is None


def test_p3c_the_echo_bound_refuses_with_zero_invokes(capture: Capture) -> None:
    row = fx.data_for(fx.NPC, wants="w" * (de.EDIT_ECHO_MAX_CHARS + 1))
    t = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    client = FakeClient(json.dumps({"fields": {"wants": "x"}}))
    _refused(Refusal.TOO_LARGE, lambda: _edit(t, client, capture.config))
    assert client.calls == [] and capture.attempts == [] and capture.outcomes == []


# ── P-4: the data block ──────────────────────────────────────────────────────


def test_p4_the_instruction_and_document_are_only_inside_the_data_block() -> None:
    row = fx.data_for(fx.NPC, notes=f"has {fx.CANARY} in it")
    t = fx.target(fx.NPC, fx.document_scope(), fx.text_instruction(f"do {fx.CANARY} please"), data=row)
    system, human = de.build_edit_messages(t)
    assert fx.CANARY not in _text(system)
    human_text = _text(human)
    opening, closing = de.data_tags(_the_nonce(human_text))
    before, _, rest = human_text.partition(opening)
    body, _, after = rest.partition(closing)
    assert fx.CANARY not in before and fx.CANARY not in after
    assert fx.CANARY in body
    assert body.count(opening) == 0 and human_text.count(opening) == 1 and human_text.count(closing) == 1
    payload = json.loads(body)
    assert payload["instruction"] == f"do {fx.CANARY} please"
    assert payload["document"]["notes"] == row["notes"]


def _the_nonce(human_content: str) -> str:
    first_line = human_content.splitlines()[0]
    return first_line[len('<data id="') : -len('">')]


def test_p4_tags_and_asset_values_never_appear_in_either_message() -> None:
    row = fx.data_for(fx.NPC, tags=[fx.CANARY], portrait={"asset_id": fx.CANARY, "kind": "image"})
    t = fx.target(fx.NPC, fx.document_scope(), data=row)
    system, human = de.build_edit_messages(t)
    assert fx.CANARY not in _text(system) and fx.CANARY not in _text(human)


def test_p4_action_sentences_are_fixed_module_constants() -> None:
    for action in EditAction:
        scope = fx.selection_scope("wants", 0, 3, fx.data_for(fx.NPC)["wants"][:3])
        t = fx.target(fx.NPC, scope, fx.action_instruction(action))
        system, _ = de.build_edit_messages(t)
        assert de.EDIT_ACTION_SENTENCES[action] in _text(system)


def test_p4_three_colliding_nonces_refuse_with_zero_invokes() -> None:
    t = fx.target(fx.NPC, fx.document_scope())
    calls = []

    def colliding() -> str:
        calls.append(1)
        return "AAAA"

    body = de._sized_body(t)
    # Force every draw to collide by making the draw always return something
    # that IS inside the body: reuse a substring of the body itself.
    literal = body[:4] if body[:4] else "AAAA"

    def always_present() -> str:
        calls.append(1)
        return literal

    _refused(Refusal.TOO_LARGE, lambda: de.build_edit_messages(t, new_nonce=always_present))
    assert len(calls) == de.NONCE_DRAWS


# ── P-5: envelopes ────────────────────────────────────────────────────────────


def test_p5_selection_envelope_must_be_exactly_replacement() -> None:
    t = fx.selection_target(fx.NPC, "wants")
    _invalid(Invalid.BAD_ENVELOPE, lambda: de.parse_edit(t, json.dumps({"fields": {"wants": "x"}}), finish_reason=None))


def test_p5_field_envelope_rejects_extra_top_level_keys() -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    extras: list[dict[str, Any]] = [
        {"prose": "x"}, {"suggestions": []}, {"cited": []}, {"type": "npc"}, {"write_revision": 1},
    ]
    for extra in extras:
        out = {"fields": {"wants": "y"}, **extra}
        _invalid(Invalid.BAD_ENVELOPE, lambda out=out: de.parse_edit(t, json.dumps(out), finish_reason=None))


def test_p5_duplicate_keys_and_non_finite_numbers_are_not_json() -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    _invalid(Invalid.NOT_JSON, lambda: de.parse_edit(t, '{"fields": {"wants": "a", "wants": "b"}}', finish_reason=None))
    _invalid(Invalid.NOT_JSON, lambda: de.parse_edit(t, '{"fields": {"wants": NaN}}', finish_reason=None))
    _invalid(Invalid.NOT_JSON, lambda: de.parse_edit(t, '{"fields": {"wants": Infinity}}', finish_reason=None))
    _invalid(Invalid.NOT_JSON, lambda: de.parse_edit(t, "[" * 20_000, finish_reason=None))
    _invalid(Invalid.NOT_JSON, lambda: de.parse_edit(t, "5" * 5_000, finish_reason=None))


def test_p5_depth_over_the_bound_is_bad_envelope() -> None:
    t = fx.target(fx.STATBLOCK, fx.field_scope("traits"))
    deep: Any = "x"
    for _ in range(dg.MAX_OUTPUT_DEPTH + 2):
        deep = [deep]
    _invalid(Invalid.BAD_ENVELOPE, lambda: _fields(t, {"traits": deep}))


def test_p5_truncated_and_oversize_are_refused_before_json_loads() -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    _invalid(Invalid.TRUNCATED, lambda: _fields(t, {"wants": "x"}, finish_reason="length"))
    _invalid(Invalid.OVERSIZE, lambda: de.parse_edit(t, "x" * (de.EDIT_OUTPUT_MAX_CHARS + 1), finish_reason=None))


# ── P-6: confinement ──────────────────────────────────────────────────────────


def test_p6_field_scope_refuses_an_echoed_unchanged_out_of_scope_key() -> None:
    row = fx.data_for(fx.NPC)
    t = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    out = {"fields": {"wants": "changed", "voice": row["voice"]}}
    _invalid(Invalid.OUT_OF_SCOPE, lambda: de.parse_edit(t, json.dumps(out), finish_reason=None))


def test_p6_document_scope_tags_out_of_scope_portrait_asset_undeclared() -> None:
    t = fx.target(fx.NPC, fx.document_scope())
    _invalid(Invalid.OUT_OF_SCOPE, lambda: _fields(t, {"tags": ["x"]}))
    _invalid(Invalid.ASSET_REFERENCE, lambda: _fields(t, {"portrait": None}))
    error = _invalid(Invalid.UNDECLARED_FIELD, lambda: _fields(t, {fx.CANARY: "x"}))
    assert fx.CANARY not in str(error)


def test_p6_undeclared_beats_asset_beats_out_of_scope_in_that_order() -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    mixed = {"fields": {"bogus": "x", "portrait": None, "voice": "y"}}
    _invalid(Invalid.UNDECLARED_FIELD, lambda: de.parse_edit(t, json.dumps(mixed), finish_reason=None))
    asset_and_scope = {"fields": {"portrait": None, "voice": "y"}}
    _invalid(Invalid.ASSET_REFERENCE, lambda: de.parse_edit(t, json.dumps(asset_and_scope), finish_reason=None))


# ── P-7: the splice ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "selected,replacement",
    [
        ("To clear her debt to the smugglers quietly, before the town finds out.", "REPLACED"),
        ("clear her", '{"json": "looking"}'),
        ("clear\nher debt", "one\ntwo"),
        ("  padded  ", "middle"),
        ("her", "  extra spaces around  "),
    ],
)
def test_p7_the_splice_never_touches_text_outside_the_span(selected: str, replacement: str) -> None:
    row = fx.data_for(fx.NPC)
    value = row["wants"]
    start = value.index(selected) if selected in value else 0
    end = start + len(selected)
    t = fx.target(fx.NPC, fx.selection_scope("wants", start, end, value[start:end]), data=row)
    out = de.parse_edit(t, json.dumps({"replacement": replacement}), finish_reason=None)
    if isinstance(out, de.NoChange):
        return
    new_value = out.fields["wants"]
    assert new_value[:start] == value[:start]
    assert new_value[len(new_value) - (len(value) - end) :] == value[end:]


def test_p7_leading_spaces_and_a_trailing_newline_in_the_selection_survive() -> None:
    """Critic C-12's residual sweep case: a selection with its own leading
    whitespace and a trailing newline round-trips exactly outside the span,
    with only the trimmed replacement standing in for the non-whitespace
    middle."""
    value = "before[  padded text\n]after"
    start, end = value.index("[") + 1, value.index("]")
    selected = value[start:end]
    assert selected == "  padded text\n"
    t = fx.target(fx.NPC, fx.selection_scope("wants", start, end, selected), data=fx.data_for(fx.NPC, wants=value))
    out = _replacement(t, "new text")
    assert isinstance(out, de.EditProposal)
    new_value = out.fields["wants"]
    assert new_value == "before[  new text\n]after"
    assert new_value[:start] == value[:start]
    assert new_value[len(new_value) - (len(value) - end) :] == value[end:]


def test_p7_whitespace_only_replacement_is_empty_replacement() -> None:
    t = fx.selection_target(fx.NPC, "wants")
    _invalid(Invalid.EMPTY_REPLACEMENT, lambda: _replacement(t, "  \n "))


def test_p7_selections_own_whitespace_is_preserved() -> None:
    row = fx.data_for(fx.NPC, wants="Keep  this  spaced  out.")
    value = row["wants"]
    start, end = 4, 6  # "  " between Keep and this
    t = fx.target(fx.NPC, fx.selection_scope("wants", start, end, value[start:end]), data=row)
    out = _replacement(t, "X")
    # The selection ("  ") is pure trim-set whitespace, so it has no non-whitespace
    # anchor to split lead from trail; by construction it is kept whole, before
    # the replacement — the text OUTSIDE the span (`Keep` and `this...`) is what
    # AC 1 actually requires to survive untouched, and it does.
    assert out.fields["wants"] == "Keep  Xthis  spaced  out."
    assert out.fields["wants"].startswith(value[:start])
    assert out.fields["wants"].endswith(value[end:])


# ── P-8: no-ops ────────────────────────────────────────────────────────────────


def test_p8_output_equal_to_base_is_no_change() -> None:
    row = fx.data_for(fx.NPC)
    t = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    assert isinstance(_fields(t, {"wants": row["wants"]}), de.NoChange)


def test_p8_a_float_equal_to_the_stored_int_is_no_change() -> None:
    row = fx.data_for(fx.ENCOUNTER)
    t = fx.target(fx.ENCOUNTER, fx.field_scope("party_level"), data=row)
    out = de.parse_edit(t, json.dumps({"fields": {"party_level": 3.0}}), finish_reason=None)
    assert isinstance(out, de.NoChange), "a raw != comparison would see 3.0 != 3"


def test_p8_empty_fields_object_is_no_change() -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    assert isinstance(de.parse_edit(t, json.dumps({"fields": {}}), finish_reason=None), de.NoChange)


def test_p8_clearing_an_absent_optional_field_is_no_change() -> None:
    row = fx.data_for(fx.NPC, tell="")
    t = fx.target(fx.NPC, fx.field_scope("tell"), data=row)
    out = de.parse_edit(t, json.dumps({"fields": {"tell": ""}}), finish_reason=None)
    assert isinstance(out, de.NoChange), "absent-then-empty must not count as a change"


def test_p8_a_trailing_newline_on_the_base_does_not_make_a_trimmed_echo_a_change() -> None:
    """Critic C-12: the base is never normalised on its own, so an echo the
    model happened to trim (`_normalized` trims every prose output) must be
    compared against a normalised base too — a raw compare would see a
    spurious change, a spurious version, and an advanced `field_revisions`."""
    row = fx.data_for(fx.NPC, wants="He lies.\n")
    t = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    assert isinstance(_fields(t, {"wants": "He lies."}), de.NoChange)


def test_p8_a_padded_text_list_item_echoed_untrimmed_is_no_change() -> None:
    row = fx.data_for(fx.SESSION_NOTES, beats=["  Keeper missing  "])
    t = fx.target(fx.SESSION_NOTES, fx.field_scope("beats"), data=row)
    assert isinstance(_fields(t, {"beats": ["  Keeper missing  "]}), de.NoChange)


def test_p8_a_padded_entry_name_echoed_untrimmed_is_no_change() -> None:
    row = fx.data_for(fx.STATBLOCK, traits=[{"name": "  Pack Tactics  ", "text": "x"}])
    t = fx.target(fx.STATBLOCK, fx.field_scope("traits"), data=row)
    out = _fields(t, {"traits": [{"name": "  Pack Tactics  ", "text": "x"}]})
    assert isinstance(out, de.NoChange)


def test_p8_a_null_tell_cleared_to_empty_string_is_no_change() -> None:
    row = fx.data_for(fx.NPC, tell=None)
    t = fx.target(fx.NPC, fx.field_scope("tell"), data=row)
    assert isinstance(_fields(t, {"tell": ""}), de.NoChange)


def test_p8_an_empty_tell_echoed_as_whitespace_is_no_change() -> None:
    row = fx.data_for(fx.NPC, tell="")
    t = fx.target(fx.NPC, fx.field_scope("tell"), data=row)
    assert isinstance(_fields(t, {"tell": "   "}), de.NoChange)


def test_p8_an_empty_list_echoed_as_empty_is_no_change() -> None:
    row = fx.data_for(fx.STATBLOCK, reactions=[])
    t = fx.target(fx.STATBLOCK, fx.field_scope("reactions"), data=row)
    assert isinstance(_fields(t, {"reactions": []}), de.NoChange)


def test_p8_an_unchanged_entry_list_echo_is_no_change() -> None:
    """Kills a raw `!=` comparison: `check_fields` returns entry-list values as
    validated objects, not the plain dicts `target.data` holds, so a naive
    compare would see every unchanged entry-list echo as a change."""
    row = fx.data_for(fx.STATBLOCK)
    t = fx.target(fx.STATBLOCK, fx.field_scope("traits"), data=row)
    out = _fields(t, {"traits": row["traits"]})
    assert isinstance(out, de.NoChange)


# ── P-9: the diff ──────────────────────────────────────────────────────────────


def test_p9_changed_fields_is_the_diff_not_the_outputs_keys() -> None:
    row = fx.data_for(fx.NPC)
    t = fx.target(fx.NPC, fx.document_scope(), data=row)
    out = {"fields": {"wants": row["wants"], "voice": "A brand new voice."}}
    result = de.parse_edit(t, json.dumps(out), finish_reason=None)
    assert isinstance(result, de.EditProposal)
    assert result.changed == ("voice",)
    assert set(result.fields) == {"voice"}


# ── P-10: remote references ───────────────────────────────────────────────────


def test_p10_a_new_link_is_refused_a_kept_one_passes_a_doubled_one_is_refused() -> None:
    row = fx.data_for(fx.NPC, wants="Visit https://ok.test for details.")
    t = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    _invalid(Invalid.REMOTE_REFERENCE, lambda: _fields(t, {"wants": row["wants"] + " and https://new.test"}))
    kept = _fields(t, {"wants": "Please see https://ok.test now."})
    assert isinstance(kept, de.EditProposal)
    _invalid(Invalid.REMOTE_REFERENCE, lambda: _fields(t, {"wants": "https://ok.test and https://ok.test twice"}))


def test_p10_markers_are_checked_inside_lists_and_entries() -> None:
    row = fx.data_for(fx.STATBLOCK)
    t = fx.target(fx.STATBLOCK, fx.field_scope("traits"), data=row)
    hostile = [{"name": "Lure", "text": "![x](https://evil.test)"}]
    _invalid(Invalid.REMOTE_REFERENCE, lambda: _fields(t, {"traits": hostile}))


# ── P-11: the merged check ────────────────────────────────────────────────────


def test_p11_clearing_name_or_ac_or_a_bad_party_level_is_invalid_fields() -> None:
    t = fx.target(fx.NPC, fx.field_scope("name"))
    _invalid(Invalid.INVALID_FIELDS, lambda: _fields(t, {"name": ""}))
    t2 = fx.target(fx.STATBLOCK, fx.field_scope("ac"))
    _invalid(Invalid.INVALID_FIELDS, lambda: _fields(t2, {"ac": None}))
    t3 = fx.target(fx.ENCOUNTER, fx.field_scope("party_level"))
    _invalid(Invalid.INVALID_FIELDS, lambda: _fields(t3, {"party_level": 0}))


# ── P-12: observation ─────────────────────────────────────────────────────────


def test_p12_success_no_change_and_parse_failure_each_record_one_outcome(capture: Capture) -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    client = FakeClient(json.dumps({"fields": {"wants": "A brand new want."}}))
    _edit(t, client, capture.config)
    assert [a["purpose"] for a in capture.attempts] == [de.PURPOSE]
    assert [o["outcome"] for o in capture.outcomes] == [usage_capture.OUTCOME_PRODUCED]

    capture.attempts.clear()
    capture.outcomes.clear()
    row = fx.data_for(fx.NPC)
    t2 = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    client2 = FakeClient(json.dumps({"fields": {"wants": row["wants"]}}))
    _edit(t2, client2, capture.config)
    assert [o["outcome"] for o in capture.outcomes] == [usage_capture.OUTCOME_PRODUCED]

    capture.outcomes.clear()
    client3 = FakeClient("not json")
    with pytest.raises(de.InvalidEdit):
        _edit(t, client3, capture.config)
    assert [o["outcome"] for o in capture.outcomes] == [usage_capture.OUTCOME_PARSE_FAILURE]


def test_p12_a_provider_auth_error_gives_none_and_is_reraised(capture: Capture) -> None:
    import httpx
    import openai

    response = httpx.Response(401, request=httpx.Request("POST", "https://provider.invalid"))
    error = openai.AuthenticationError("denied", response=response, body=None)
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    client = FakeClient(error)
    with pytest.raises(openai.AuthenticationError):
        _edit(t, client, capture.config)
    assert [o["outcome"] for o in capture.outcomes] == [usage_capture.OUTCOME_NONE]


def test_p12_refusals_and_a_zero_deadline_record_nothing_with_a_positive_control(capture: Capture) -> None:
    t = fx.target(fx.NPC, fx.field_scope("portrait"))
    client = FakeClient(json.dumps({"fields": {}}))
    with pytest.raises(de.EditRefused):
        _edit(t, client, capture.config)
    assert capture.attempts == [] and capture.outcomes == []
    assert client.calls == []

    t2 = fx.target(fx.NPC, fx.field_scope("wants"))
    with pytest.raises(de.EditRefused):
        _edit(t2, client, capture.config, attempts=0)
    assert capture.attempts == [] and capture.outcomes == []

    # Positive control: a real attempt on the SAME config does record.
    client3 = FakeClient(json.dumps({"fields": {"wants": "changed"}}))
    _edit(t2, client3, capture.config)
    assert capture.attempts and capture.outcomes


# ── P-13: SEC-24, no inherited callbacks ──────────────────────────────────────


class _Recorder(BaseCallbackHandler):
    def __init__(self) -> None:
        self.fired = False

    def on_llm_start(self, *a: Any, **k: Any) -> None:
        self.fired = True


def test_p13_the_forwarded_config_carries_no_inherited_callbacks(capture: Capture) -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    client = FakeClient(json.dumps({"fields": {"wants": "changed"}}))
    hostile = {**capture.config, "callbacks": [_Recorder()], "tags": ["t"], "metadata": {"m": 1}, "run_name": "r"}
    _edit(t, client, hostile)
    operation = capture.config["configurable"][usage_capture.CONFIG_KEY]
    assert client.calls[0][1] == {"callbacks": [], "configurable": {usage_capture.CONFIG_KEY: operation}}

    bare = FakeClient(json.dumps({"fields": {"wants": "changed"}}))
    _edit(t, bare, {"callbacks": [_Recorder()]})
    assert bare.calls[0][1] == {"callbacks": []}


def test_p13_a_fake_chat_model_never_fires_an_outer_recorder() -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    model = FakeListChatModel(responses=[json.dumps({"fields": {"wants": "changed"}})])
    recorder = _Recorder()
    traced: RunnableLambda[int, object] = RunnableLambda(lambda _: _edit(t, model))
    traced.invoke(0, config={"callbacks": [recorder]})
    assert not recorder.fired


# ── P-14: the output bound ────────────────────────────────────────────────────


def test_p14_every_call_carries_the_edit_max_tokens_and_other_kwargs_pass_through() -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    client = FakeClient(json.dumps({"fields": {"wants": "changed"}}))
    _edit(t, client)
    assert client.calls[0][2] == {"max_tokens": de.EDIT_MAX_OUTPUT_TOKENS}


def test_p14_a_client_with_no_kwargs_is_unbounded_with_zero_calls() -> None:
    class NoKwargsClient:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, input: Any, config: Any = None) -> AIMessage:
            self.calls += 1
            return AIMessage(content="{}")

    client = NoKwargsClient()
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    _refused(Refusal.UNBOUNDED_CLIENT, lambda: _edit(t, client))
    assert client.calls == 0


def test_p14_document_generations_default_bound_is_untouched() -> None:
    """Kills a behaviour change to `_OutputBounded`'s default (1kg.5.4 I-20)."""
    calls: list[dict[str, Any]] = []

    class Inner:
        def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "ok"

    dg._OutputBounded(Inner()).invoke("x")
    assert calls[-1]["max_tokens"] == dg.GENERATION_MAX_OUTPUT_TOKENS
    dg._OutputBounded(Inner(), max_tokens=999).invoke("x")
    assert calls[-1]["max_tokens"] == 999


# ── P-15: between_attempts ────────────────────────────────────────────────────


def test_p15_a_hook_that_raises_after_a_transient_error_gives_one_billable_attempt(
    capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    import openai

    monkeypatch.setattr(generate, "_RETRY_BACKOFF_SECONDS", 0.0)
    sentinel = RuntimeError("cancelled")
    transient = openai.APIConnectionError(request=httpx.Request("POST", "https://provider.invalid"))
    client = FakeClient(transient, json.dumps({"fields": {"wants": "changed"}}))
    calls = {"n": 0}

    def hook() -> None:
        calls["n"] += 1
        raise sentinel

    t = fx.target(fx.NPC, fx.field_scope("wants"))
    with pytest.raises(RuntimeError) as caught:
        _edit(t, client, capture.config, attempts=3, between_attempts=hook)
    assert caught.value is sentinel
    assert len(client.calls) == 1, "the hook must run before the SECOND attempt, not the first"
    assert calls["n"] == 1
    assert [o["outcome"] for o in capture.outcomes] == [usage_capture.OUTCOME_NONE]


# ── P-16: the canary sweep ─────────────────────────────────────────────────────


def test_p16_a_canary_never_reaches_a_log_record_or_an_exception(
    capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    row = fx.data_for(fx.NPC, notes=f"secret {fx.CANARY} here")
    t = fx.target(fx.NPC, fx.document_scope(), fx.text_instruction(f"do {fx.CANARY}"), data=row)

    for refusal_code in Refusal:
        assert fx.CANARY not in str(de.EditRefused(refusal_code))
    for invalid_code in Invalid:
        assert fx.CANARY not in str(de.InvalidEdit(invalid_code))

    client = FakeClient("not json with " + fx.CANARY)
    try:
        _edit(t, client, capture.config)
    except de.InvalidEdit as exc:
        assert fx.CANARY not in str(exc) and fx.CANARY not in repr(exc)
        assert fx.CANARY not in "".join(traceback.format_exception(exc))
    for record in caplog.records:
        assert fx.CANARY not in record.getMessage()
    for attempt in capture.attempts + capture.outcomes:
        assert fx.CANARY not in json.dumps(attempt, default=str)
    assert fx.CANARY not in repr(t)


# ── P-17: D-9, no model or tier named ─────────────────────────────────────────


def _model_words() -> set[str]:
    words = {tier for tier in typing.get_args(model_catalog.PublicTier)}
    for profile in model_catalog.CATALOG.values():
        words |= {profile.alias, profile.display_name, profile.api_model, profile.provider}
    for public in model_catalog.PUBLIC_MODELS.values():
        words |= {public.id, public.label}
    return {word.casefold() for word in words if word}


def test_p17_no_model_provider_or_tier_is_ever_named() -> None:
    words = _model_words()
    texts = [str(de.EditRefused(code)) for code in Refusal] + [str(de.InvalidEdit(code)) for code in Invalid]
    texts += [de.no_change_prose()]
    for scope_kind in ("document", "field", "selection"):
        texts.append(de.edit_prose(scope_kind, ["Wants"]))
        texts.append(de.edit_version_summary(scope_kind, "Wants"))
    for text in texts:
        assert not [word for word in words if word and word in text.casefold()], text


# ── P-18: lane prose and version summaries ────────────────────────────────────


def test_p18_prose_and_summary_are_bounded_and_escape_labels() -> None:
    hostile_label = "Wants ](evil) *bold*"
    for scope_kind in ("document", "field", "selection"):
        prose = de.edit_prose(scope_kind, [hostile_label, "Voice"])
        summary = de.edit_version_summary(scope_kind, hostile_label)
        assert len(prose) <= 4_000 and len(summary) <= 200
        assert "](evil)" not in prose and "](evil)" not in summary
        assert "\\*bold\\*" in prose or "bold" in prose


def test_p18_summaries_pass_document_stores_own_check_summary() -> None:
    from service.document_store import check_summary

    for scope_kind, label in (("document", "x"), ("field", "Wants"), ("selection", "Wants")):
        summary = de.edit_version_summary(scope_kind, label)
        assert check_summary(summary) == summary


# ── P-19: structural ───────────────────────────────────────────────────────────


_ALLOWED_IMPORTS = {
    "__future__", "json", "collections.abc", "dataclasses", "enum", "types", "typing",
    "langchain_core.messages",
    "config", "service.generate", "service.usage_capture", "service.workbench_contracts",
    "service.workbench_registry", "service.document_wire", "service.document_generation",
    "service",
}


def _resolved_module(node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    return "service" if not node.module else f"service.{node.module}"


def test_p19_the_import_allow_list_and_no_sql_string() -> None:
    import service.document_editing as module

    text = open(module.__file__, encoding="utf-8").read()
    source = ast.parse(text)
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in _ALLOWED_IMPORTS, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert _resolved_module(node) in _ALLOWED_IMPORTS, (node.module, node.level)
    forbidden = ("service.providers", "service.rag", "service.graph", "service.app", "service.tracing",
                 "service.tool_invocations", "openai", "langchain_openai", "langfuse")
    for name in forbidden:
        assert name not in text
    sql_shapes = ("SELECT * FROM", "INSERT INTO", "UPDATE CAMPAIGN", "DELETE FROM", "FOR UPDATE", "FOR NO KEY UPDATE")
    assert not any(shape in text.upper() for shape in sql_shapes)


def test_p19_every_reused_helper_is_the_same_object_as_document_generations() -> None:
    reused = (
        "_strict_json", "_unfenced", "_walk", "_inert", "_new_nonce", "_BetweenAttempts",
        "_forwarded_config", "_takes_keyword_arguments", "_normalized", "_declared",
        "_KIND_LIMITS", "_MARKDOWN_ESCAPABLE", "data_tags", "field_catalog",
        "attempts_that_fit", "REMOTE_REFERENCE_MARKERS", "MAX_OUTPUT_DEPTH", "NONCE_DRAWS",
        "IDENTITY_RULE", "PLAIN_TEXT_RULE",
    )
    for name in reused:
        assert getattr(de, name) is getattr(dg, name), name
    assert de._OutputBounded is dg._OutputBounded


# ── P-20: the T-10 injection corpus ────────────────────────────────────────────


@pytest.mark.parametrize("family", sorted(fx.EDIT_INJECTION_FAMILIES))
@pytest.mark.parametrize("placement", ["instruction", "in_scope_value", "context_value", "selected_text"])
def test_p20_an_obeying_model_never_lands_outside_its_scope(family: str, placement: str) -> None:
    payload = fx.EDIT_INJECTION_FAMILIES[family]
    row = fx.data_for(fx.NPC)

    if placement == "selected_text":
        value = row["wants"]
        scope = fx.selection_scope("wants", 0, len(value), value)
        target = fx.target(fx.NPC, scope, fx.text_instruction(payload), data=row)
    elif placement == "instruction":
        target = fx.target(fx.NPC, fx.field_scope("wants"), fx.text_instruction(payload), data=row)
    elif placement == "in_scope_value":
        row = {**row, "wants": payload}
        target = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    else:  # context_value: a field OUTSIDE the edit's scope
        row = {**row, "notes": payload}
        target = fx.target(fx.NPC, fx.field_scope("wants"), data=row)

    messages = de.build_edit_messages(target)
    nonce = _the_nonce(_text(messages[1]))

    if isinstance(target.scope, SelectionScope):
        model_output = json.dumps({"replacement": payload.replace("{nonce}", nonce)})
    else:
        model_output = json.dumps({"fields": {"wants": payload.replace("{nonce}", nonce), "tags": ["owned"]}})

    try:
        out = de.parse_edit(target, model_output, finish_reason=None)
    except de.InvalidEdit as exc:
        assert exc.code in Invalid
        return
    if isinstance(out, de.NoChange):
        return
    assert set(out.fields) <= de.scope_keys(target.doc_type, target.scope)
    de._check_remote_references(dict(out.fields), target.data)


def test_p20_revision_forge_and_suggestion_injection_are_bad_envelope() -> None:
    """Critic C-26: an extra top-level key must be refused, never dropped and
    silently accepted — the defect a lax pass/fail on the sweep above could
    hide (the sweep's own `fields`/`replacement` envelopes never carry a
    second top-level key, so it cannot exercise this)."""
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    revision_forge = json.dumps({"fields": {"wants": "changed"}, "base_write_revision": 999_999})
    _invalid(Invalid.BAD_ENVELOPE, lambda: de.parse_edit(t, revision_forge, finish_reason=None))
    suggestion_injection = json.dumps({"fields": {"wants": "changed"}, "suggestions": [{"a": 1}]})
    _invalid(Invalid.BAD_ENVELOPE, lambda: de.parse_edit(t, suggestion_injection, finish_reason=None))


def test_p20_scope_escape_is_refused_by_the_key_it_widens_to() -> None:
    """Critic C-26: `tags` is never AI-editable, so a scope-escape payload
    that widens the write to include it is `out_of_scope` at both document and
    field scope, pinned exactly rather than "refused or safe"."""
    field_target = fx.target(fx.NPC, fx.field_scope("voice"))
    escaped = json.dumps({"fields": {"voice": "changed", "name": "PWNED", "tags": ["owned"]}})
    _invalid(Invalid.OUT_OF_SCOPE, lambda: de.parse_edit(field_target, escaped, finish_reason=None))

    doc_target = fx.target(fx.NPC, fx.document_scope())
    escaped_doc = json.dumps({"fields": {"voice": "changed", "tags": ["owned"]}})
    _invalid(Invalid.OUT_OF_SCOPE, lambda: de.parse_edit(doc_target, escaped_doc, finish_reason=None))


def test_p20_span_escape_cannot_reach_past_its_own_selection() -> None:
    """Critic C-26: the server enforces confinement structurally — a model
    that claims it will "ignore the selection boundaries" can still only
    replace the span, because the wire never lets a selection edit send
    anything but a replacement. Pinned as a normal successful splice, not a
    refusal, since the escape attempt has no channel to act through."""
    row = fx.data_for(fx.NPC)
    value = row["wants"]
    t = fx.target(fx.NPC, fx.selection_scope("wants", 0, 10, value[:10]), data=row)
    out = _replacement(t, fx.EDIT_INJECTION_FAMILIES["span_escape"])
    assert isinstance(out, de.EditProposal)
    assert set(out.fields) == {"wants"}
    new_value = out.fields["wants"]
    assert new_value.startswith(value[:0]) and new_value.endswith(value[10:])


def test_p20_a_new_remote_reference_is_refused_exactly() -> None:
    row = fx.data_for(fx.NPC, wants="Plain text with no links.")
    t = fx.target(fx.NPC, fx.field_scope("wants"), data=row)
    hostile = json.dumps({"fields": {"wants": fx.EDIT_INJECTION_FAMILIES["exfiltration_link"]}})
    _invalid(Invalid.REMOTE_REFERENCE, lambda: de.parse_edit(t, hostile, finish_reason=None))


# ── P-21: 1kg.5.4's suite is unaffected ───────────────────────────────────────


def test_p21_document_generations_own_suite_still_imports_and_the_module_is_unmoved() -> None:
    """A real regression here is caught by running `test_document_generation.py`
    itself (unedited, per §13.2); this just pins the one additive change."""
    import inspect

    sig = inspect.signature(dg._OutputBounded.__init__)
    assert list(sig.parameters)[1:] == ["inner", "max_tokens"]
    assert sig.parameters["max_tokens"].default == dg.GENERATION_MAX_OUTPUT_TOKENS


# ── Structural edges: no dedicated P-row, still load-bearing ─────────────────


def test_edit_document_rejects_an_out_of_range_max_attempts() -> None:
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    client = FakeClient(json.dumps({"fields": {"wants": "changed"}}))
    with pytest.raises(ValueError, match="max_attempts is 1 to 3"):
        _edit(t, client, attempts=4)
    with pytest.raises(ValueError, match="max_attempts is 1 to 3"):
        _edit(t, client, attempts=-1)


def test_inert_deep_walks_lists_and_leaves_non_strings_alone() -> None:
    row = fx.data_for(fx.STATBLOCK)
    t = fx.target(fx.STATBLOCK, fx.document_scope(), data=row)
    _, human = de.build_edit_messages(t)
    body = _text(human)
    nonce = _the_nonce(body)
    opening, closing = de.data_tags(nonce)
    payload = json.loads(body.partition(opening)[2].partition(closing)[0])
    # `abilities` (a dict of ints) and `traits` (a list of dicts) both round-trip
    # through the payload: the int leaves are untouched, and the list is walked.
    assert payload["document"]["abilities"] == row["abilities"]
    assert payload["document"]["traits"] == row["traits"]


def test_merged_document_check_defensively_refuses_an_impossible_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """I-8: defensive, and unreachable while every field rule is per-field —
    proven here by making only the WHOLE-document check lie about a merge it
    would otherwise accept, never the per-field check the patch already passed."""
    t = fx.target(fx.NPC, fx.field_scope("wants"))
    real_check_fields = de.check_fields

    def flaky(doc_type: Any, version: Any, fields: Any, *, whole: bool, enforce_required: bool = True) -> Any:
        if whole:
            raise ValueError("boom")
        return real_check_fields(doc_type, version, fields, whole=whole, enforce_required=enforce_required)

    monkeypatch.setattr(de, "check_fields", flaky)
    with pytest.raises(de.InvalidEdit) as caught:
        _fields(t, {"wants": "a brand new want"})
    assert caught.value.code is Invalid.INVALID_FIELDS

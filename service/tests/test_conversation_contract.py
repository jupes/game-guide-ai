"""The conversation family of the Workbench wire contract (1kg.2.4 A2).

The shared fixtures under `contracts/workbench/v1/Conversation*.json` are the
specification and `test_workbench_contracts.py` runs them; this file holds what
a fixture cannot say: that the family's numbers are the store's, that the title
rule refuses exactly REFUSED_TEXT_CODE_POINTS — the one shared stored-text rule
(lead ruling 2026-09-21, agent-forge-harness-644; originally ruling A2-9's own,
narrower REFUSED_IN_A_TITLE, retired here) — that a refusal names the field and
never the value, and that a patch is emitted as sent.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from service import conversation_store as store
from service import workbench_contracts as wc

CANARY = "Zx9CanaryQ7"


def test_the_familys_bounds_are_the_stores() -> None:
    assert wc.CONVERSATION_PAGE_MAX_ITEMS == store.LIMIT_MAX
    assert wc.CONVERSATION_TITLE_MAX_CHARS == store.TITLE_MAX_CHARS


def test_the_title_rule_keeps_no_second_opinion() -> None:
    """agent-forge-harness-644: a title used to refuse its own hand-picked
    REFUSED_IN_A_TITLE list — narrower than the lead ruling of 2026-09-21's
    check_plain_text/REFUSED_TEXT_CODE_POINTS, which every other piece of
    stored text refuses by. There must not be three opinions (1kg.2.4,
    1kg.5.7.2, and this one): the list is retired, not merely unused."""
    assert not hasattr(wc, "REFUSED_IN_A_TITLE")


def _create(title: Any) -> wc.ConversationCreateRequest:
    return wc.ConversationCreateRequest.model_validate({"schema_version": 1, "started_mode": "sage", "title": title})


@pytest.mark.parametrize("code", sorted(wc.REFUSED_TEXT_CODE_POINTS))
def test_every_refused_code_point_is_refused_inside_a_title(code: int) -> None:
    with pytest.raises(ValidationError):
        _create(f"Harbour{chr(code)}job")


@pytest.mark.parametrize("code", [0x09, 0x20, 0x7E, 0xA0, 0x202F, 0x2065, 0x206A, 0x200C, 0x200D, 0xFE0F, 0x1F3B2])
def test_the_code_points_either_side_of_each_range_are_kept(code: int) -> None:
    assert _create(f"Harbour{chr(code)}job").title == f"Harbour{chr(code)}job"


@pytest.mark.parametrize("code", [0x0A, 0x0D, 0x2028, 0x2029])
def test_a_title_stays_one_line_a_dimension_check_plain_text_does_not_cover(code: int) -> None:
    """check_plain_text carves tab/LF/CR out of its refused set on purpose — a
    multi-line prose field needs them (see its own docstring) — so a title
    needs its OWN one-line rule alongside it, the same composition
    `plainOneLine` uses in contracts.ts for a cue's title and the like. This is
    not REFUSED_TEXT_CODE_POINTS' job and must not grow into a second list."""
    with pytest.raises(ValidationError):
        _create(f"Harbour{chr(code)}job")


#: Where a title's refusal is swept: every code point to U+3000, each refused
#: code point and its two neighbours (U+FEFF sits above U+3000), and astral
#: samples. Lone surrogates are left out: well-formedness is its own rule, and
#: no UTF-8 request body can carry one.
_TITLE_SWEEP = sorted(
    (
        set(range(0x0000, 0x3001))
        | {code + step for code in wc.REFUSED_TEXT_CODE_POINTS for step in (-1, 0, 1) if code + step >= 0}
        | {0x1F3B2, 0xE0001, 0xE007F, 0xF0000, 0x10FFFD, 0x10FFFF}
    )
    - set(range(0xD800, 0xE000))
)
#: The one-line rule's own code points (LF, CR, LS, PS) -- spelled out here so
#: this pin is independent of the module's private tuple.
_A_TITLE_LINE_BREAK = frozenset({0x0A, 0x0D, 0x2028, 0x2029})


def test_a_title_refuses_a_code_point_if_and_only_if_the_shared_rule_or_the_one_line_rule_does() -> None:
    """agent-forge-harness-644 review M1: pinning only that the shared set IS
    refused lets a title-only refusal grow back unseen -- the second opinion
    644 retired. Both directions, exhaustively over the sweep."""
    wrong: list[str] = []
    for code in _TITLE_SWEEP:
        expected = code in wc.REFUSED_TEXT_CODE_POINTS or code in _A_TITLE_LINE_BREAK
        try:
            _create(f"Harbour{chr(code)}job")
            refused = False
        except ValidationError:
            refused = True
        if refused != expected:
            wrong.append(f"U+{code:04X} {'refused' if refused else 'kept'}")
    assert wrong == []


def test_a_title_edge_is_trimmed_before_it_is_checked_as_the_client_does() -> None:
    """Review N1: the BOM is in the trimmed set, so at an edge it is trimmed
    away, never refused; a bidirectional mark is not, so at an edge it stays
    and is refused. contracts.test.ts pins the same two cases."""
    bom, lrm = chr(0xFEFF), chr(0x200E)
    assert _create(f"{bom}Harbour{bom}").title == "Harbour"
    for edged in (f"{lrm}Harbour", f"Harbour{lrm}"):
        with pytest.raises(ValidationError):
            _create(edged)


def test_a_title_is_trimmed_as_the_client_trims_and_stored_trimmed() -> None:
    assert _create(" \t Harbour " + chr(0x3000)).title == "Harbour"
    assert wc.ConversationPatchRequest.model_validate({"schema_version": 1, "title": "  Job "}).title == "Job"
    # NEL is not what `String.prototype.trim` removes, so it stays — and is a C1 control.
    with pytest.raises(ValidationError):
        _create("Harbour" + chr(0x85))


@pytest.mark.parametrize("title", ["a" * 200, " " + "a" * 200 + " "])
def test_two_hundred_characters_after_trimming_is_the_bound(title: str) -> None:
    assert len(_create(title).title or "") == 200
    with pytest.raises(ValidationError):
        _create(title.strip() + "a")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"schema_version": 1, "started_mode": "sage", "campaign_id": "cmp_4b1d9e7a"}, "campaign_id"),
        ({"schema_version": 1}, "started_mode"),
        ({"schema_version": 1, "started_mode": "sage", "title": " "}, "title"),
        ({"schema_version": 2, "started_mode": "sage"}, "schema_version"),
    ],
)
def test_a_refused_create_names_its_field(body: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as refused:
        wc.ConversationCreateRequest.model_validate(body)
    assert wc.validation_error_body(refused.value.errors()).detail.field == field


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"schema_version": 1}, None),
        ({"schema_version": 1, "campaign_id": None}, "campaign_id"),
        ({"schema_version": 1, "title": None}, "title"),
        ({"schema_version": 1, "archived": None}, "archived"),
        ({"schema_version": 1, "started_mode": None}, "started_mode"),
        ({"schema_version": 1, "archived": 1}, "archived"),
    ],
)
def test_a_refused_patch_names_its_field_or_none(body: dict[str, Any], field: str | None) -> None:
    with pytest.raises(ValidationError) as refused:
        wc.ConversationPatchRequest.model_validate(body)
    assert wc.validation_error_body(refused.value.errors()).detail.field == field


@pytest.mark.parametrize(
    "sent",
    [
        {"schema_version": 1, "title": "Job"},
        {"schema_version": 1, "archived": False},
        {"schema_version": 1, "campaign_id": "cmp_4b1d9e7a", "started_mode": "gm"},
    ],
)
def test_a_patch_is_emitted_exactly_as_it_was_sent(sent: dict[str, Any]) -> None:
    """The differential fuzz's emission check found it: written back with a
    `null` for every key left out, a patch is one this contract refuses."""
    emitted = wc.ConversationPatchRequest.model_validate(sent).model_dump(mode="json")
    assert emitted == sent
    wc.ConversationPatchRequest.model_validate(emitted)


@pytest.mark.parametrize(
    "title", [CANARY + chr(0x0A) + "x", CANARY * 20, CANARY + chr(0x202E), " ", 42]
)
def test_a_refused_title_is_never_quoted(title: Any) -> None:
    with pytest.raises(ValidationError) as refused:
        _create(title)
    assert CANARY not in str(refused.value)
    body = wc.validation_error_body(refused.value.errors()).model_dump_json()
    assert CANARY not in body
    assert CANARY not in str(wc.redacted_errors(refused.value.errors()))


def test_a_response_title_is_read_as_stored() -> None:
    """Tolerant: a stored title is bounded but not re-trimmed or re-judged."""
    row = {
        "schema_version": 1,
        "conversation_id": "cnv_" + "a" * 22,
        "campaign_id": None,
        "title": " Harbour ",
        "started_mode": None,
        "created_at": "2026-09-16T19:20:11Z",
        "updated_at": None,
        "archived_at": None,
    }
    assert wc.Conversation.model_validate(row).title == " Harbour "


def test_already_linked_is_a_new_code_and_conflict_keeps_its_meaning() -> None:
    assert wc.ErrorCode.ALREADY_LINKED.value == "already_linked"
    assert wc.ErrorCode.CONFLICT.value == "conflict"

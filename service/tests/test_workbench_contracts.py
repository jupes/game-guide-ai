"""Workbench wire contract (1kg.1.2).

The JSON files under ``contracts/workbench/v1`` are the specification. This
suite and ``ui/src/gm/contracts.test.ts`` read the *same* files, so the Pydantic
models and the Zod schemas cannot drift apart without one of the two failing.

An example may carry ``applies_to``: ``["server"]`` or ``["client"]``. That is how
the one deliberate asymmetry is written down — the server is strict about what
it emits (closed error codes, no undeclared fields), while a client must tolerate
additive fields from a newer server.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from service import workbench_contracts as wc
from service.models import ChatResponse, MessagesResponse

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "workbench" / "v1"
SIDE = "server"

_REPEAT = re.compile(r"^@repeat:(.):(\d+)$")

#: Legacy models ride the same harness as a guard (see the fixtures' notes).
SCHEMAS: dict[str, TypeAdapter[Any]] = {
    **wc.CONTRACT_SCHEMAS,
    "LegacyChatResponse": TypeAdapter(ChatResponse),
    "LegacyMessagesResponse": TypeAdapter(MessagesResponse),
}


def expand(value: Any) -> Any:
    """Expand the fixtures' two directives.

    ``"@repeat:a:2000"`` is 2,000 ``a``s, and
    ``{"@repeat_value": x, "@count": 101}`` is a list of 101 ``x``s. Boundary
    cases need long strings and long lists, and a 2,001-character literal or a
    101-item array in a JSON file is unreadable and easy to get wrong by one.
    """
    if isinstance(value, str):
        match = _REPEAT.match(value)
        return match.group(1) * int(match.group(2)) if match else value
    if isinstance(value, list):
        return [expand(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"@repeat_value", "@count"}:
            return [expand(value["@repeat_value"]) for _ in range(value["@count"])]
        return {key: expand(item) for key, item in value.items()}
    return value


def _fixture_files() -> list[Path]:
    skip = {"schemas.json", "registry.json"}
    return sorted(p for p in FIXTURES.rglob("*.json") if p.name not in skip)


def _examples(kind: str) -> list[Any]:
    out = []
    for path in _fixture_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        for example in doc[kind]:
            if SIDE in example.get("applies_to", ["server", "client"]):
                out.append(pytest.param(doc["schema"], example["value"], id=f"{doc['schema']}::{example['name']}"))
    return out


@pytest.mark.parametrize(("schema", "value"), _examples("valid"))
def test_valid_examples_validate(schema: str, value: Any) -> None:
    SCHEMAS[schema].validate_python(expand(value))


@pytest.mark.parametrize(("schema", "value"), _examples("invalid"))
def test_invalid_examples_fail_closed(schema: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        SCHEMAS[schema].validate_python(expand(value))


def test_every_listed_schema_is_implemented_and_exercised() -> None:
    """Neither language may quietly lack a schema, or a schema lack examples."""
    listed = json.loads((FIXTURES / "schemas.json").read_text(encoding="utf-8"))
    assert listed["contract_version"] == wc.CONTRACT_VERSION
    assert set(listed["schemas"]) == set(SCHEMAS)

    seen: dict[str, dict[str, int]] = {}
    for path in _fixture_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert path.stem == doc["schema"], f"{path.name} must be named after its schema"
        seen[doc["schema"]] = {"valid": len(doc["valid"]), "invalid": len(doc["invalid"])}
    assert set(seen) == set(listed["schemas"])
    assert all(counts["valid"] >= 1 and counts["invalid"] >= 1 for counts in seen.values()), seen


def test_registry_constants_match_the_shared_registry() -> None:
    """The facts the validators lean on come from one file (1kg.3.1 extends it)."""
    registry = json.loads((FIXTURES / "registry.json").read_text(encoding="utf-8"))
    assert registry["contract_version"] == wc.CONTRACT_VERSION
    assert [kind.value for kind in wc.ResultKind] == registry["result_kinds"]
    assert [kind.value for kind in wc.CardKind] == registry["card_kinds"]
    assert [category.value for category in wc.LibraryCategory] == registry["library_categories"]

    assert [tool.value for tool in wc.ToolId] == [tool["id"] for tool in registry["tools"]]
    for tool in registry["tools"]:
        tool_id = wc.ToolId(tool["id"])
        assert wc.TOOL_RESULT_KIND[tool_id].value == tool["result_kind"]
        assert wc.TOOL_BRIEF_POLICY[tool_id].value == tool["brief"]
        created = wc.TOOL_CREATES_DOC_TYPE.get(tool_id)
        assert (created.value if created else None) == tool["creates_doc_type"]
        card = wc.TOOL_CARD_KIND.get(tool_id)
        assert (card.value if card else None) == tool["card_kind"]

    assert [doc_type.value for doc_type in wc.DocumentTypeId] == [d["id"] for d in registry["document_types"]]
    for doc_type in registry["document_types"]:
        assert wc.DOC_TYPE_LIBRARY_CATEGORY[wc.DocumentTypeId(doc_type["id"])].value == doc_type["library_category"]


def test_error_codes_are_safe_metric_labels() -> None:
    """Plan invariant 10: a code may become a metric label, so it is bounded and
    can never carry user text."""
    assert len(wc.ErrorCode) <= 40
    assert all(re.fullmatch(r"[a-z][a-z_]{1,39}", code.value) for code in wc.ErrorCode)


def test_the_brief_is_stored_trimmed() -> None:
    request = wc.ToolInvocationRequest.model_validate(
        {
            "schema_version": 1,
            "invocation_id": "inv_9f2c4e1a7b3d4c5e",
            "tool_id": "monster",
            "brief": "  CR 5, drowned \n",
            "campaign_id": "cmp_4b1d9e7a",
            "conversation_id": "0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d",
        }
    )
    assert request.brief == "CR 5, drowned"
    assert request.source_entry_id is None


def test_a_response_serialises_in_snake_case_with_iso_timestamps() -> None:
    """What the API emits is exactly what the fixtures show (CANVAS-27)."""
    fixture = json.loads((FIXTURES / "ToolInvocation.json").read_text(encoding="utf-8"))
    done = next(e["value"] for e in fixture["valid"] if e["name"] == "done carries its result")
    dumped = wc.ToolInvocation.model_validate(done).model_dump(mode="json")
    assert dumped["created_at"] == "2026-09-16T19:31:02Z"
    assert dumped["result"]["document"]["library_category"] == "npcs"
    assert set(dumped) == set(done)


def test_an_unknown_result_kind_names_the_discriminator() -> None:
    """X-8: the failure is about the kind, not a confusing cascade of field errors."""
    with pytest.raises(ValidationError) as caught:
        wc.CONTRACT_SCHEMAS["ToolResult"].validate_python({"result_kind": "table", "tool_id": "loot"})
    assert "result_kind" in str(caught.value)


# ── Timeline ─────────────────────────────────────────────────────────────────

_ROW_TIME = datetime(2026, 9, 16, 20, 10, tzinfo=UTC)


def _valid_entry(name: str) -> dict[str, Any]:
    fixture = json.loads((FIXTURES / "TimelineEntry.json").read_text(encoding="utf-8"))
    return next(e["value"] for e in fixture["valid"] if e["name"] == name)


def test_the_entry_kind_vocabulary_is_the_union() -> None:
    """``EntryKind`` is what ``1kg.4.2`` stores by; it may not drift from the models."""
    tags = {get_args(member.model_fields["entry_kind"].annotation)[0] for member in get_args(wc.AnyEntry)}
    assert tags == {kind.value for kind in wc.EntryKind}


def test_a_readable_stored_entry_is_served_as_itself() -> None:
    raw = _valid_entry("a tool turn is a tool and a brief, never the slash string (RAIL-9)")
    entry = wc.entry_or_opaque(raw, entry_id="ent_77aa12bc", created_at=_ROW_TIME)
    assert isinstance(entry, wc.ToolEntry)
    assert entry.invocation.tool_id is wc.ToolId.NPC


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"schema_version": 2, "entry_kind": "player_turn"}, "newer_version"),
        # An embedded invocation can be newer than the entry around it.
        ({"invocation": {"schema_version": 3, "status": "paused"}}, "newer_version"),
        # By the versioning rule a new kind bumps the version, so an unknown kind
        # at version 1 is damage, not the future.
        ({"entry_kind": "player_turn"}, "unreadable"),
        ({"brief": 42}, "unreadable"),
        # ``True`` is an int in Python; it is not a version.
        ({"schema_version": True}, "unreadable"),
    ],
)
def test_an_unreadable_stored_entry_becomes_a_placeholder(change: dict[str, Any], reason: str) -> None:
    """The forward-version rule: never dropped, never forwarded unvalidated."""
    raw = {**_valid_entry("a tool turn is a tool and a brief, never the slash string (RAIL-9)"), **change}
    entry = wc.entry_or_opaque(raw, entry_id="ent_77aa12bc", created_at=_ROW_TIME)
    assert isinstance(entry, wc.OpaqueEntry)
    assert entry.reason.value == reason
    # It keeps the row's place and identity, and nothing of its content.
    assert entry.model_dump(mode="json") == {
        "schema_version": 1,
        "entry_kind": "opaque",
        "entry_id": "ent_77aa12bc",
        "created_at": "2026-09-16T20:10:00Z",
        "reason": reason,
    }


def test_junk_in_storage_never_raises() -> None:
    deep: Any = {"schema_version": 9}
    for _ in range(40):
        deep = {"nested": [deep]}
    for junk in [None, 7, "x", [], {}, deep]:
        entry = wc.entry_or_opaque(junk, entry_id="398", created_at=_ROW_TIME)
        assert isinstance(entry, wc.OpaqueEntry)
        # Too deep to look for a version in: damage, which is the safe reading.
        assert entry.reason is wc.OpaqueReason.UNREADABLE


def test_a_page_serialises_exactly_as_the_fixtures_show() -> None:
    """Nulls that mean "not recorded" survive the trip; nothing is invented."""
    fixture = json.loads((FIXTURES / "TimelinePage.json").read_text(encoding="utf-8"))
    last = next(e["value"] for e in fixture["valid"] if e["name"] == "the last page says so with a null cursor")
    dumped = wc.TimelinePage.model_validate(last).model_dump(mode="json", by_alias=True)
    assert dumped["next_cursor"] is None
    answer = dumped["items"][0]["answer"]
    assert answer["answerable"] is None
    assert answer["sources"] is None
    assert answer["created_at"] == "2026-08-02T18:05:09Z"


def test_a_stat_block_inside_an_entry_keeps_its_wire_alias() -> None:
    """``Abilities.int_`` is ``int`` on the wire; the timeline must not change that."""
    raw = _valid_entry("a plain GM-mode message is an ordinary chat exchange (RAIL-14), here with a stat block")
    raw["answer"]["stat_block"]["abilities"] = {"str": 18, "int": 6}
    entry = wc.ChatEntry.model_validate(raw)
    dumped = entry.model_dump(mode="json", by_alias=True)
    assert dumped["answer"]["stat_block"]["abilities"]["int"] == 6

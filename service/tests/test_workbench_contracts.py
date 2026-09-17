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
    assert [kind.value for kind in wc.FieldKind] == registry["field_kinds"]
    assert {key: kind.value for key, kind in wc.COMMON_FIELDS.items()} == registry["common_fields"]
    for doc_type in registry["document_types"]:
        type_id = wc.DocumentTypeId(doc_type["id"])
        assert wc.DOC_TYPE_LIBRARY_CATEGORY[type_id].value == doc_type["library_category"]
        assert wc.DOC_TYPE_VERSION[type_id] == doc_type["type_version"]
        assert {key: kind.value for key, kind in wc.DOC_TYPE_FIELDS[type_id].items()} == doc_type["fields"]
        # A type's own field may not shadow a common one, and every key is a field key.
        assert not set(doc_type["fields"]) & set(registry["common_fields"])
        assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key) for key in doc_type["fields"])


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


def test_an_edit_entry_is_read_back_like_any_other() -> None:
    raw = _valid_entry("an edit that lost a race says so, and nothing was overwritten (CANVAS-25)")
    entry = wc.entry_or_opaque(raw, entry_id="ent_ed170003", created_at=_ROW_TIME)
    assert isinstance(entry, wc.EditEntry)
    assert entry.invocation.error is not None and entry.invocation.error.conflict is not None
    assert entry.invocation.error.conflict.fields == ["wants"]


# ── Documents ────────────────────────────────────────────────────────────────

_SECRET = "She is the drowned saint."


def _document(**data: Any) -> dict[str, Any]:
    fixture = json.loads((FIXTURES / "Document.json").read_text(encoding="utf-8"))
    base = next(e["value"] for e in fixture["valid"] if e["name"].startswith("a document made by hand"))
    return {**base, "data": {"name": "Sister Ondrey Vashe", **data}}


@pytest.mark.parametrize(
    "data",
    [
        {_SECRET: "a stray key"},  # an undeclared key that is itself private text
        {"wants": [_SECRET]},  # the wrong shape for a prose field
        {"voice": _SECRET + "\n" + _SECRET},  # a line break in a text field
        {"tags": [_SECRET, ""]},  # a bad item beside a private one
        {"portrait": {"asset_id": "ast_1", "media_type": "image", "alt": _SECRET, "url": "https://x.test/p.png"}},
    ],
)
def test_a_rejected_field_never_echoes_gm_private_text(data: dict[str, Any]) -> None:
    """X-7: a validation message can reach a response body and a log line, so it
    names keys and kinds and never quotes a value — or a key it did not expect."""
    with pytest.raises(ValidationError) as caught:
        wc.Document.model_validate(_document(**data))
    messages = " ".join(error["msg"] for error in caught.value.errors())
    assert "drowned saint" not in messages


def test_check_fields_is_usable_on_its_own() -> None:
    """``1kg.5.2`` and ``1kg.5.5`` validate a merged document before committing it."""
    checked = wc.check_fields(wc.DocumentTypeId.NPC, 1, {"name": "A guard", "tags": ["gate"]}, whole=True)
    assert checked == {"name": "A guard", "tags": ["gate"]}
    # A patch may touch any subset, but still cannot blank the name.
    assert wc.check_fields(wc.DocumentTypeId.NPC, 1, {"notes": ""}, whole=False) == {"notes": ""}
    with pytest.raises(ValueError, match="cannot be blank"):
        wc.check_fields(wc.DocumentTypeId.NPC, 1, {"name": " \t "}, whole=False)
    with pytest.raises(ValueError, match="version 1"):
        wc.check_fields(wc.DocumentTypeId.NPC, 2, {"name": "A guard"}, whole=True)


def test_every_type_validates_with_the_common_fields_alone() -> None:
    """Until ``1kg.5.3`` declares a type's own fields, the common ones are all it has."""
    for doc_type in wc.DocumentTypeId:
        wc.check_fields(doc_type, 1, {"name": "x", "qualifier": "", "tags": []}, whole=True)
        if not wc.DOC_TYPE_FIELDS[doc_type]:
            with pytest.raises(ValueError, match="do not declare"):
                wc.check_fields(doc_type, 1, {"name": "x", "body": "text"}, whole=True)


def test_an_instruction_and_a_search_are_stored_trimmed() -> None:
    instruction = wc.TextInstruction.model_validate({"kind": "text", "text": "  sharper, and shorter \n"})
    assert instruction.text == "sharper, and shorter"
    query = wc.LibraryQuery.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "cmp_1",
            "category": "npcs",
            "search": "  tide ",
            "sort": "name",
            "archived": False,
        }
    )
    assert query.search == "tide"
    assert (query.type, query.cursor, query.limit) == (None, None, None)


def test_a_document_serialises_exactly_as_the_fixtures_show() -> None:
    fixture = json.loads((FIXTURES / "Document.json").read_text(encoding="utf-8"))
    dossier = next(e["value"] for e in fixture["valid"] if e["name"] == "an NPC dossier after an assistant edit")
    dumped = wc.Document.model_validate(dossier).model_dump(mode="json", by_alias=True)
    # A portrait's optional size is emitted as null; everything else is unchanged.
    dumped["data"]["portrait"] = {k: v for k, v in dumped["data"]["portrait"].items() if v is not None}
    assert dumped == dossier


# ── Validation failures, as the Workbench envelope ───────────────────────────

_REQUEST = {
    "schema_version": 1,
    "invocation_id": "inv_9f2c4e1a7b3d4c5e",
    "tool_id": "npc",
    "brief": "a guard",
    "campaign_id": "cmp_4b1d9e7a",
    "conversation_id": "0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d",
}


@pytest.mark.parametrize(
    ("change", "code", "field"),
    [
        ({"brief": "  "}, "brief_required", "brief"),
        ({"brief": "a" * 2001}, "brief_too_long", "brief"),
        ({"tool_id": "npcs"}, "unknown_tool", "tool_id"),
        ({"schema_version": 2}, "unsupported_schema_version", "schema_version"),
        # Malformed is not "unsupported": there is no version to be out of date.
        ({"schema_version": True}, "validation_failed", "schema_version"),
        ({"campaign_id": "cmp 4b1d/9e7a"}, "validation_failed", "campaign_id"),
        # An undeclared key is the client's text, so it is never named back.
        ({_SECRET: "x"}, "validation_failed", None),
    ],
)
def test_a_validation_failure_becomes_the_workbench_envelope(
    change: dict[str, Any], code: str, field: str | None
) -> None:
    with pytest.raises(ValidationError) as caught:
        wc.ToolInvocationRequest.model_validate({**_REQUEST, **change})
    body = wc.validation_error_body(caught.value.errors())
    assert (body.detail.code.value, body.detail.field, body.detail.retryable) == (code, field, False)
    assert "drowned saint" not in body.model_dump_json()
    # What it produces is itself a valid envelope.
    SCHEMAS["ErrorBody"].validate_python(body.model_dump(mode="json", exclude_none=True))


def test_an_empty_error_list_is_still_an_answer() -> None:
    assert wc.validation_error_body([]).detail.code is wc.ErrorCode.VALIDATION_FAILED


def test_a_route_that_uses_it_echoes_nothing_where_the_default_echoes_everything() -> None:
    """X-7, shown end to end. FastAPI's default 422 returns each error's ``input``
    — the request itself. A Workbench route installs this handler instead."""
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    def build(*, safe: bool) -> TestClient:
        app = FastAPI()

        @app.post("/documents/doc_1/fields")
        def patch(body: wc.FieldPatchRequest) -> dict[str, str]:
            return {}

        if safe:

            @app.exception_handler(RequestValidationError)
            async def answer(_request: Request, exc: RequestValidationError) -> JSONResponse:
                body = wc.validation_error_body(exc.errors())
                return JSONResponse(status_code=422, content=body.model_dump(mode="json", exclude_none=True))

        return TestClient(app)

    patch_body = {
        "schema_version": 1,
        "type": "npc",
        "type_version": 1,
        "base_write_revision": 3,
        "fields": {"wants": [_SECRET]},
    }

    default = build(safe=False).post("/documents/doc_1/fields", json=patch_body)
    assert default.status_code == 422 and _SECRET in default.text  # the hazard is real

    safe = build(safe=True).post("/documents/doc_1/fields", json=patch_body)
    assert safe.status_code == 422 and _SECRET not in safe.text
    assert safe.json() == {
        "detail": {"code": "validation_failed", "message": "That request isn't valid.", "retryable": False}
    }


def test_a_write_revision_survives_javascript() -> None:
    """The token must round-trip through ``JSON.parse``, so it stops at 2**53 - 1."""
    assert wc.WRITE_REVISION_MAX == 9_007_199_254_740_991
    top = TypeAdapter(wc.WriteRevision)
    assert top.validate_python(wc.WRITE_REVISION_MAX) == wc.WRITE_REVISION_MAX
    with pytest.raises(ValidationError):
        top.validate_python(wc.WRITE_REVISION_MAX + 1)


def test_a_stat_block_inside_an_entry_keeps_its_wire_alias() -> None:
    """``Abilities.int_`` is ``int`` on the wire; the timeline must not change that."""
    raw = _valid_entry("a plain GM-mode message is an ordinary chat exchange (RAIL-14), here with a stat block")
    raw["answer"]["stat_block"]["abilities"] = {"str": 18, "int": 6}
    entry = wc.ChatEntry.model_validate(raw)
    dumped = entry.model_dump(mode="json", by_alias=True)
    assert dumped["answer"]["stat_block"]["abilities"]["int"] == 6

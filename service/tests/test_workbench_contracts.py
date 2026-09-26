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

import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import PydanticCustomError

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


def test_a_request_shape_is_tagged_as_one() -> None:
    """``direction`` decides how much the differential fuzz generates for a shape:
    a ``request`` gets the stray-key and ``__proto__`` mutations a client→server
    body must refuse, a ``response`` does not. The fuzz fails closed on a tag it
    does not recognise, but a *recognised and wrong* tag — ``response`` by
    copy-paste on a new ``*Request`` — silently drops that coverage while the
    job still prints ``0 disagreements in the contract's own shapes``. So the tag
    is pinned against the name rather than trusted.

    One-way on purpose: ``RevealAudience`` is a request body that is not named
    like one (it travels as ``RevealRequest.audience``), so a ``request`` tag on
    a shape with another name is legal.
    """
    mistagged = {}
    for path in _fixture_files():
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc["schema"].endswith(("Request", "Query")) and doc.get("direction") != "request":
            mistagged[doc["schema"]] = doc.get("direction")
    assert not mistagged, f"named like a request body, tagged otherwise: {mistagged}"


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
    assert [kind.value for kind in wc.AssetKind] == registry["asset_kinds"]
    assert {kind.value: list(types) for kind, types in wc.MEDIA_TYPES.items()} == registry["media_types"]
    assert [kind.value for kind in wc.CueKind] == registry["cue_kinds"]
    assert [slot.value for slot in wc.AudioSlot] == registry["audio_slots"]
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


def test_the_revealable_set_is_the_registry_s_allowlist_for_every_type() -> None:
    """Decisions REVEAL-10, ED-5, ED-20: **one** answer to *may this field reach a
    player*, and it is an allowlist.

    ``1kg.5.3``'s per-field ``revealable`` rule is the source; this module holds a
    copy only because the registry imports it and cannot be imported back. Pinning
    the copy to ``registry.json`` for **every** type is what stops the two from
    ever giving different answers — the failure that would otherwise be silent is
    a field marked ``revealable: false`` on a type nobody wrote an assertion for.
    """
    registry = json.loads((FIXTURES / "registry.json").read_text(encoding="utf-8"))

    def allowed(rules: dict[str, Any]) -> set[str]:
        return {key for key, rule in rules.items() if rule["revealable"]}

    assert set(wc.REVEALABLE_COMMON_FIELDS) == allowed(registry["common_field_rules"])
    assert {t.value for t in wc.REVEALABLE_FIELDS} == {doc["id"] for doc in registry["document_types"]}
    for doc_type in registry["document_types"]:
        type_id = wc.DocumentTypeId(doc_type["id"])
        assert set(wc.REVEALABLE_FIELDS[type_id]) == allowed(doc_type["field_rules"]), doc_type["id"]
        # And the derived set — what a mask and a projection are actually checked
        # against — never names a key the registry withholds, on any type.
        withheld = set(doc_type["field_rules"]) - allowed(doc_type["field_rules"])
        withheld |= set(registry["common_field_rules"]) - allowed(registry["common_field_rules"])
        assert not withheld & set(wc.revealable_fields(type_id)), doc_type["id"]

    # ED-20's worked case, spelled out: the link between a face and what wears it.
    assert "true_identity" in wc.DOC_TYPE_FIELDS[wc.DocumentTypeId.NPC]
    assert "true_identity" not in wc.revealable_fields(wc.DocumentTypeId.NPC)
    # …and tags never are, on any type (SEC-15).
    assert all("tags" not in wc.revealable_fields(doc_type) for doc_type in wc.DocumentTypeId)


def test_every_revealable_kind_has_a_shape_a_table_can_be_shown() -> None:
    """A field kind a type declares and the registry marks revealable must have a
    projection shape, or REVEAL-10's *each stat cell* has nowhere to land. The
    TypeScript twin gets this from an exhaustive ``switch``; Python needs the
    assertion, because a missing entry only shows up as a refused reveal."""
    reachable = {
        kind for doc_type in wc.DocumentTypeId for kind in wc.revealable_fields(doc_type).values()
    }
    assert reachable <= set(wc._PROJECTION_VALUE)
    # Every kind the registry declares is reachable today, so the two sets agree.
    assert reachable == set(wc.FieldKind)


def test_a_mask_key_is_never_a_wildcard() -> None:
    """Decisions REVEAL-9, ED-8. ``all`` matches the field-key pattern, so it is
    refused by name; ``*`` and ``%`` never matched it in the first place."""
    keys = TypeAdapter(wc.MaskKey)
    assert keys.validate_python("notes") == "notes"
    for wildcard in ("all", "*", "**", "%", "ALL"):
        with pytest.raises(ValidationError):
            keys.validate_python(wildcard)
    # A registry that declared a field called `all` would make a mask unspeakable.
    assert all("all" not in wc.revealable_fields(doc_type) for doc_type in wc.DocumentTypeId)


def test_a_field_kind_with_no_table_shape_refuses_rather_than_raising() -> None:
    """When ``1kg.5.3`` adds a field kind, a projection of it must be **refused**
    until this family gives it a shape — not raise a ``KeyError`` out of
    validation and answer 500. TypeScript gets this from an exhaustive switch;
    Python needs the lookup to miss safely.
    """
    kinds = {kind: wc._PROJECTION_VALUE[kind] for kind in wc._PROJECTION_VALUE}
    try:
        del wc._PROJECTION_VALUE[wc.FieldKind.TEXT]
        with pytest.raises(ValidationError) as caught:
            wc.TableProjection.model_validate(
                {
                    "content_kind": "document",
                    "type": "npc",
                    "fields": [{"key": "name", "value": "Sister Ondrey Vashe"}],
                }
            )
        assert "no shape a table can be shown" in str(caught.value)
    finally:
        wc._PROJECTION_VALUE.update(kinds)


def test_no_request_a_table_client_sends_names_a_participant() -> None:
    """Decision threat model §8.2: the table client is not a restricted view of
    the GM API, it is a separate, smaller API with its own principal. A guest
    asking beyond the table slot gets what an empty table gives, never a refusal
    — which is only possible if there is no shape in which it can *ask*.
    """
    def names_a_participant(schema: dict[str, Any]) -> bool:
        """The whole schema, not its top-level field names: a shape that nests an
        audience carries the id one level down and would read as clean.

        The *word* is not the test — ``TableRole`` is the enum ``participant |
        guest`` and belongs on the table channel (TABLE-13). The **identifier**
        is: the field, or the shape that holds it.

        What this cannot catch, and no textual guard can: an id under another
        name. It is a tripwire against the shape drifting, not a proof; the
        fixtures are what pin each frame's actual content.
        """
        text = json.dumps(schema)
        named = ("participant_id", "participant_ids", "ParticipantSlotRef", "ParticipantsAudience")
        return any(word in text for word in named)

    # The list is read from the fixtures, not written here: a fixture file marks
    # itself ``"channel": "table"`` and ``"direction": "request"``, so a table-side
    # request someone adds later joins this test by existing. A hard-coded pair
    # would go on passing while the new shape carried an id.
    table_requests = sorted(
        doc["schema"]
        for doc in (json.loads(path.read_text(encoding="utf-8")) for path in FIXTURES.glob("*.json"))
        if doc.get("channel") == "table" and doc.get("direction") == "request"
    )
    assert table_requests, "no fixture file declares itself a table-side request"
    for name in table_requests:
        assert not names_a_participant(wc.CONTRACT_SCHEMAS[name].json_schema(ref_template="{model}")), name

    # The frames a table client receives name their slot ``table`` or ``mine``,
    # never a slot reference, so no id travels that way either (SEC-15).
    for member in get_args(get_args(wc.TableEvent)[0]):
        assert not names_a_participant(TypeAdapter(member).json_schema(ref_template="{model}")), member.__name__


def test_content_kind_has_exactly_one_member_in_v1() -> None:
    """Decision ADR §7.4, and the alignment's sixth acceptance row. What reserving
    the discriminator buys is that a v1 table client meets a future member as its
    neutral placeholder; adding one **is** a version bump. The cardinality is the
    claim, so it is the assertion — read from the schema itself, not from a list
    beside it, so the enum and the literal cannot drift apart."""
    assert [kind.value for kind in wc.ContentKind] == ["document"]
    projection = wc.TableProjection.model_json_schema()
    assert projection["properties"]["content_kind"]["const"] == "document"


def test_no_shape_in_v1_declares_an_eligibility_field() -> None:
    """Decision ED-11: Workbench v1 ships mask-only. Eligibility binds reveal from
    the assistant's enforcement release (``1ir.11.1``), and the refusal it needs
    is an additive error code — so nothing here carries a class or a revision,
    and nothing about eligibility ever reaches a table client (ED-25, REVEAL-24).
    """
    forbidden = ("eligibility", "classification", "authz_revision", "projection_revision")
    for name, adapter in wc.CONTRACT_SCHEMAS.items():
        schema = json.dumps(adapter.json_schema(ref_template="{model}"))
        assert not any(f'"{word}"' in schema for word in forbidden), name


def test_every_field_kinds_bounds_are_the_shared_registrys() -> None:
    """Each kind's ceiling is one number, not two.

    A bound kept as two independent constants can drift: each suite goes on
    testing against its own, and the differential fuzz never reaches the values
    in between. ``registry.json`` holds the number, and the boundary examples in
    ``Document.json`` exercise it on both sides.
    """
    registry = json.loads((FIXTURES / "registry.json").read_text(encoding="utf-8"))
    assert registry["field_bounds"] == {
        "text_field_max_chars": wc.TEXT_FIELD_MAX_CHARS,
        "prose_field_max_chars": wc.PROSE_FIELD_MAX_CHARS,
        "list_field_max_items": wc.LIST_FIELD_MAX_ITEMS,
        "list_item_max_chars": wc.LIST_ITEM_MAX_CHARS,
        "integer_field_min": wc.INTEGER_FIELD_MIN,
        "integer_field_max": wc.INTEGER_FIELD_MAX,
        "ability_score_min": wc.ABILITY_SCORE_MIN,
        "ability_score_max": wc.ABILITY_SCORE_MAX,
    }


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


def _entry_starting(prefix: str) -> dict[str, Any]:
    fixture = json.loads((FIXTURES / "TimelineEntry.json").read_text(encoding="utf-8"))
    return next(e["value"] for e in fixture["valid"] if e["name"].startswith(prefix))


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
        # A version is an integral JSON number, however it is spelled — ``2.0`` in
        # the text is a float here and the number 2 in JavaScript — and nothing else:
        # ``True`` is an int in Python, but it is not a version.
        ({"schema_version": 2.0, "entry_kind": "player_turn"}, "newer_version"),
        ({"schema_version": "2", "entry_kind": "player_turn"}, "unreadable"),
        ({"schema_version": 1.5}, "unreadable"),
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
        # A version anywhere but at the top is content: damage, the safe reading.
        assert entry.reason is wc.OpaqueReason.UNREADABLE


def test_a_field_a_newer_server_added_costs_a_stored_entry_nothing_after_a_rollback() -> None:
    """The versioning table allows an optional field without a bump. A stored
    entry is therefore read the way a client reads a response — undeclared keys
    ignored — while what the server emits stays strict."""
    chat = _entry_starting("a chat exchange with its complete outcome")
    newer = {**chat, "answer": {**chat["answer"], "evidence": {"grounded": True}}, "pinned": True}
    entry = wc.entry_or_opaque(newer, entry_id=chat["entry_id"], created_at=_ROW_TIME)
    assert isinstance(entry, wc.ChatEntry)
    dumped = entry.model_dump(mode="json")
    assert "pinned" not in dumped and "evidence" not in dumped["answer"]
    with pytest.raises(ValidationError):
        wc.CONTRACT_SCHEMAS["TimelineEntry"].validate_python(newer)


def test_a_version_deeper_in_the_payload_is_content_not_a_version() -> None:
    chat = _entry_starting("a chat exchange with its complete outcome")
    stray = {**chat, "answer": {**chat["answer"], "schema_version": 9}}
    # Ignored on the way in, like any undeclared key: the entry is served.
    assert isinstance(wc.entry_or_opaque(stray, entry_id=chat["entry_id"], created_at=_ROW_TIME), wc.ChatEntry)
    # And when the row is damaged as well, that is damage, not the future.
    entry = wc.entry_or_opaque({**stray, "mode": 42}, entry_id=chat["entry_id"], created_at=_ROW_TIME)
    assert isinstance(entry, wc.OpaqueEntry) and entry.reason is wc.OpaqueReason.UNREADABLE


def test_the_row_is_the_authority_on_identity() -> None:
    raw = _entry_starting("a session starts")
    entry = wc.entry_or_opaque(raw, entry_id="ent_another", created_at=_ROW_TIME)
    assert isinstance(entry, wc.OpaqueEntry) and entry.reason is wc.OpaqueReason.UNREADABLE
    assert entry.entry_id == "ent_another"


def test_unusable_row_columns_raise_rather_than_serve() -> None:
    """The columns are what a placeholder is built from; without them there is
    nothing to serve in the row's place, and that is a storage fault."""
    raw = _entry_starting("a session starts")
    with pytest.raises(ValidationError):
        wc.entry_or_opaque(raw, entry_id="ent/../398", created_at=_ROW_TIME)
    with pytest.raises(ValidationError):
        wc.entry_or_opaque(raw, entry_id=raw["entry_id"], created_at=_ROW_TIME.replace(tzinfo=None))


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


@pytest.mark.parametrize("doc_type", list(wc.DocumentTypeId), ids=lambda t: t.value)
def test_every_type_takes_the_common_fields(doc_type: wc.DocumentTypeId) -> None:
    """``name``, ``qualifier`` and ``tags`` belong to every type — and on seven of
    the eight they are enough for a whole document.

    ``statblock`` is the exception and is asserted as one rather than skipped:
    LIB-12 says a stat block without its AC and HP is not valid, so a write of the
    common fields alone must be **refused** there. Skipping the type would make
    this parametrised test quietly stop covering the type it matters most for.
    """
    common = {"name": "x", "qualifier": "", "tags": []}
    if doc_type is wc.DocumentTypeId.STATBLOCK:
        with pytest.raises(ValueError, match="require ac"):
            wc.check_fields(doc_type, 1, dict(common), whole=True)
        assert wc.check_fields(doc_type, 1, {**common, "ac": 12, "hp": 33}, whole=True) == {
            **common,
            "ac": 12,
            "hp": 33,
        }
        return
    assert wc.check_fields(doc_type, 1, dict(common), whole=True) == common


@pytest.mark.parametrize("doc_type", list(wc.DocumentTypeId), ids=lambda t: t.value)
def test_every_type_fails_closed_on_a_key_it_does_not_declare(doc_type: wc.DocumentTypeId) -> None:
    """The posture, proved per type rather than for the types that happened to
    have no fields. ``smuggled_key`` is declared by none of the eight."""
    with pytest.raises(ValueError, match="do not declare"):
        wc.check_fields(doc_type, 1, {"name": "x", "smuggled_key": "text"}, whole=True)


@pytest.mark.parametrize("doc_type", list(wc.DocumentTypeId), ids=lambda t: t.value)
def test_the_answer_to_an_undeclared_key_carries_no_trace_of_it(doc_type: wc.DocumentTypeId) -> None:
    """X-7: the body a route returns names the key's *type*, never the key or
    the text under it — a GM's private note must not come back in a 422.

    Asserted on ``validation_error_body``, which is what a route answers with,
    and not on ``redacted_errors``: that is for a log line, its redaction branch
    only fires for ``extra_forbidden``, and an undeclared document field is a
    ``value_error`` with an empty location, so a test written against it would
    pass while proving nothing.
    """
    secret = "Drown the harbourmaster."
    document = {
        "schema_version": 1,
        "document_id": "doc_9k2f7a1c",
        "campaign_id": "cmp_4b1d9e7a",
        "type": doc_type.value,
        "type_version": 1,
        "data": {"name": "A document", "secret_plan": secret},
        "write_revision": 1,
        "version": {
            "number": 1,
            "author": "gm",
            "summary": "",
            "created_at": "2026-09-16T20:00:00Z",
            "sealed": False,
            "changed_fields": ["name"],
            "restored_from": None,
        },
        "archived": False,
        "created_at": "2026-09-16T20:00:00Z",
        "updated_at": "2026-09-16T20:00:00Z",
    }
    with pytest.raises(ValidationError) as caught:
        wc.Document.model_validate(document)
    errors = caught.value.errors()

    answered = wc.validation_error_body(errors).model_dump_json()
    assert secret not in answered and "secret_plan" not in answered

    logged = json.dumps(wc.redacted_errors(errors))
    assert secret not in logged and "secret_plan" not in logged

    # And the reason both helpers exist: the raw list still carries the request.
    # This is the assertion that would catch someone logging ``exc.errors()``.
    assert secret in json.dumps(errors, default=str)


_STATBLOCK = wc.DocumentTypeId.STATBLOCK


def _statblock(**fields: Any) -> dict[str, Any]:
    """A whole stat block, so it carries the three LIB-12 requires. A caller that
    is exercising ``ac`` or ``hp`` overrides the value here."""
    return wc.check_fields(_STATBLOCK, 1, {"name": "Ondrey", "ac": 16, "hp": 104, **fields}, whole=True)


@pytest.mark.parametrize(
    ("value", "stored"),
    [(7, 7), (7.0, 7), (0, 0), (wc.INTEGER_FIELD_MAX, wc.INTEGER_FIELD_MAX)],
)
def test_an_integer_field_takes_a_json_integer(value: Any, stored: Any) -> None:
    """``1.0`` is ``1`` because JavaScript cannot tell the two apart."""
    assert _statblock(ac=value)["ac"] == stored


@pytest.mark.parametrize("value", [-1, wc.INTEGER_FIELD_MIN, None])
def test_the_one_unbounded_integer_field_still_reaches_the_kinds_own_range(value: Any) -> None:
    """``statblock.xp`` is the one declared ``integer`` field with no per-use
    bounds, which is what keeps ``INTEGER_FIELD_MIN`` reachable through a declared
    field — and so keeps the shared floor fixtures honest. ``null`` clears it."""
    assert _statblock(xp=value)["xp"] == value


@pytest.mark.parametrize(
    "value", [True, False, "7", "", 1.5, [], {}, wc.INTEGER_FIELD_MAX + 1, wc.INTEGER_FIELD_MIN - 1]
)
def test_an_integer_field_refuses_anything_else(value: Any) -> None:
    with pytest.raises(ValueError, match="not a valid integer field"):
        _statblock(ac=value)


@pytest.mark.parametrize(
    "value",
    [{}, {"str": 10}, {"str": 10, "dex": 12, "con": 14, "int": 8, "wis": 13, "cha": 16}, None],
)
def test_an_abilities_field_takes_any_subset_of_the_six_scores(value: Any) -> None:
    """CANVAS-19: the ability-score block is **one** field. ``null`` clears it."""
    assert _statblock(abilities=value)["abilities"] == value


@pytest.mark.parametrize(
    "value",
    [
        {"strength": 10},
        {"str": 10, "luck": 3},
        {"str": "10"},
        {"str": True},
        {"str": wc.ABILITY_SCORE_MAX + 1},
        {"str": wc.ABILITY_SCORE_MIN - 1},
        {"str": None},
        [],
        "10",
    ],
)
def test_an_abilities_field_refuses_an_unknown_key_or_an_impossible_score(value: Any) -> None:
    with pytest.raises(ValueError, match="not a valid abilities field"):
        _statblock(abilities=value)


def test_there_is_one_spelling_of_no_score() -> None:
    """Requirement 7e / AC 17: a score that is not known is a key left **out**;
    ``{"str": null}`` would be a second spelling of the same fact, against the
    fixtures' own principle of one way to say empty per kind. Omitting a key and
    ``{}`` stay valid, and the whole block still clears to ``null`` — on a write
    and on a tolerant stored read alike."""
    with pytest.raises(ValueError, match="not a valid abilities field"):
        _statblock(abilities={"str": 14, "dex": None})
    with pytest.raises(ValueError, match="not a valid abilities field"):
        wc.read_stored_fields(_STATBLOCK, 1, {"name": "Ondrey", "abilities": {"dex": None}})
    assert _statblock(abilities={"str": 14})["abilities"] == {"str": 14}
    assert _statblock(abilities={})["abilities"] == {}
    assert _statblock(abilities=None)["abilities"] is None


def _entries(checked: dict[str, Any]) -> list[dict[str, str]]:
    """``check_fields`` returns a model for a structured kind, as it already does
    for ``asset``; what matters on the wire is what it serialises to."""
    return [entry.model_dump() for entry in checked["traits"]]


def test_an_entry_list_field_takes_named_entries() -> None:
    entries = [{"name": "Amphibious", "text": "She breathes water."}, {"name": "Silent", "text": ""}]
    assert _entries(_statblock(traits=entries)) == entries
    assert _statblock(traits=[])["traits"] == []


@pytest.mark.parametrize(
    "value",
    [
        [{"name": "Amphibious"}],
        [{"text": "no name"}],
        [{"name": "Amphibious", "text": "x", "damage": "1d6"}],
        [{"name": "", "text": "x"}],
        # Requirement 5 (F-8): blank by the contract's own trim, as a document's name.
        [{"name": chr(9), "text": "x"}],
        [{"name": "   ", "text": "x"}],
        [{"name": "two\nlines", "text": "x"}],
        [{"name": "x", "text": "y"}] * (wc.LIST_FIELD_MAX_ITEMS + 1),
        ["Amphibious"],
        {},
        None,
    ],
)
def test_an_entry_list_field_refuses_a_malformed_entry(value: Any) -> None:
    with pytest.raises(ValueError, match="not a valid entry_list field"):
        _statblock(traits=value)


def test_the_new_kinds_are_bounded_at_their_edges() -> None:
    """The caps are the contract's, and both languages read them from it."""
    assert _statblock(traits=[{"name": "n", "text": "t"}] * wc.LIST_FIELD_MAX_ITEMS)["traits"]
    long_name = "a" * wc.TEXT_FIELD_MAX_CHARS
    assert _entries(_statblock(traits=[{"name": long_name, "text": "t"}]))[0]["name"] == long_name
    with pytest.raises(ValueError, match="not a valid entry_list field"):
        _statblock(traits=[{"name": "a" * (wc.TEXT_FIELD_MAX_CHARS + 1), "text": "t"}])
    with pytest.raises(ValueError, match="not a valid entry_list field"):
        _statblock(traits=[{"name": "n", "text": "t" * (wc.LIST_ITEM_MAX_CHARS + 1)}])


# ── Required fields (LIB-12) and per-use integer bounds ──────────────────────


def _registry_rules() -> dict[str, dict[str, dict[str, Any]]]:
    """Every field rule ``registry.json`` declares, per type, commons folded in —
    which is how the contract module spells the two facts below."""
    registry = json.loads((FIXTURES / "registry.json").read_text(encoding="utf-8"))
    return {
        doc_type["id"]: {**registry["common_field_rules"], **doc_type["field_rules"]}
        for doc_type in registry["document_types"]
    }


def test_the_required_fields_are_the_registrys() -> None:
    """The fact lives in three places — ``registry.json``, this module and
    ``ui/src/gm/registry.ts`` — and is pinned across them, because two independent
    definitions of one safety fact is exactly the defect that shipped once before.

    Asserted for **every** type, not for the one that has required fields: a type
    nobody wrote an assertion for is where a drift would sit unseen.
    """
    rules = _registry_rules()
    assert {doc_type.value for doc_type in wc.REQUIRED_FIELDS} == set(rules)
    for type_id, type_rules in rules.items():
        expected = {key for key, rule in type_rules.items() if rule["required"]}
        assert set(wc.REQUIRED_FIELDS[wc.DocumentTypeId(type_id)]) == expected, type_id
    # …and what that comes to, spelled out, so the shape is legible here too.
    assert wc.REQUIRED_FIELDS[_STATBLOCK] == frozenset({"name", "ac", "hp"})
    assert all(
        wc.REQUIRED_FIELDS[doc_type] == frozenset({"name"})
        for doc_type in wc.DocumentTypeId
        if doc_type is not _STATBLOCK
    )


def test_the_integer_field_bounds_are_the_registrys() -> None:
    """The same pinning, for the second fact this bead moves into the validator."""
    rules = _registry_rules()
    assert {doc_type.value for doc_type in wc.INTEGER_FIELD_BOUNDS} == set(rules)
    for type_id, type_rules in rules.items():
        expected = {
            key: tuple(rule["bounds"]) for key, rule in type_rules.items() if rule["bounds"] is not None
        }
        assert wc.INTEGER_FIELD_BOUNDS[wc.DocumentTypeId(type_id)] == expected, type_id
    # Only an integer field may carry them, and the registry's validator is what
    # enforces that — here it is checked against the kinds this module declares.
    for doc_type, bounds in wc.INTEGER_FIELD_BOUNDS.items():
        for key in bounds:
            assert wc.DOC_TYPE_FIELDS[doc_type][key] is wc.FieldKind.INTEGER, f"{doc_type.value}.{key}"


@pytest.mark.parametrize(
    ("doc_type", "key", "lowest"),
    [
        (wc.DocumentTypeId.STATBLOCK, "ac", 0),
        (wc.DocumentTypeId.STATBLOCK, "hp", 0),
        (wc.DocumentTypeId.CHARACTER_SHEET, "ac", 0),
        (wc.DocumentTypeId.CHARACTER_SHEET, "hp", 0),
        (wc.DocumentTypeId.SESSION_NOTES, "session", 1),
        (wc.DocumentTypeId.ENCOUNTER, "party_level", 1),
        (wc.DocumentTypeId.ENCOUNTER, "xp_budget", 0),
    ],
)
def test_a_bounded_integer_field_stops_at_its_own_floor(
    doc_type: wc.DocumentTypeId, key: str, lowest: int
) -> None:
    """One case per bounded field, so no field is bounded in the data and
    unchecked in the validator. The ceiling is the kind's, and the two floor
    fixtures in ``Document.json`` pin the kind's own."""
    base: dict[str, Any] = {"name": "A document"}
    if doc_type is _STATBLOCK:
        base |= {"ac": 16, "hp": 104}
    assert wc.check_fields(doc_type, 1, {**base, key: lowest}, whole=True)[key] == lowest
    assert wc.check_fields(doc_type, 1, {**base, key: wc.INTEGER_FIELD_MAX}, whole=True)[key] == wc.INTEGER_FIELD_MAX
    # …and a cleared value is still a cleared value, unless the field is required.
    if key not in wc.REQUIRED_FIELDS[doc_type]:
        assert wc.check_fields(doc_type, 1, {**base, key: None}, whole=True)[key] is None
    with pytest.raises(ValueError, match="not a valid integer field"):
        wc.check_fields(doc_type, 1, {**base, key: lowest - 1}, whole=True)


def test_a_bounds_refusal_names_the_field_and_the_type_and_quotes_no_value() -> None:
    """X-7: a message can reach a response body, so it says which key, which type
    and which range — never the number that was sent."""
    with pytest.raises(ValueError) as caught:
        wc.check_fields(_STATBLOCK, 1, {"name": "Ondrey", "ac": -424242, "hp": 104}, whole=True)
    assert "424242" not in str(caught.value)
    assert "ac" in str(caught.value) and "statblock" in str(caught.value)


@pytest.mark.parametrize(
    ("key", "empty"),
    [("name", ""), ("name", "  \t "), ("ac", None), ("hp", None)],
)
def test_a_create_refuses_a_required_field_that_is_absent_or_empty(key: str, empty: Any) -> None:
    """Decision LIB-12: *required* means **present and not empty**, so a cleared
    cell is the same defect as an absent key. ``name`` keeps its own older
    message, because its dedicated check runs first and is unchanged."""
    whole = {"name": "Ondrey", "ac": 16, "hp": 104}
    absent = {other: value for other, value in whole.items() if other != key}
    with pytest.raises(ValueError):
        wc.check_fields(_STATBLOCK, 1, absent, whole=True)
    with pytest.raises(ValueError):
        wc.check_fields(_STATBLOCK, 1, {**whole, key: empty}, whole=True)


def test_every_empty_kind_counts_as_empty_for_a_required_field() -> None:
    """*Empty* is defined per kind, once, and the same on both sides. The three
    shapes are exercised through a type that could declare each: a required
    ``text``/``prose`` is empty when the contract's own trim empties it, a
    required list when it has no items, and a required asset, integer or ability
    block when it is ``null``.
    """
    assert wc._is_empty(wc.FieldKind.TEXT, " \t ") and not wc._is_empty(wc.FieldKind.TEXT, "x")
    assert wc._is_empty(wc.FieldKind.PROSE, " ") and not wc._is_empty(wc.FieldKind.PROSE, "x")
    assert wc._is_empty(wc.FieldKind.TEXT_LIST, []) and not wc._is_empty(wc.FieldKind.TEXT_LIST, ["x"])
    assert wc._is_empty(wc.FieldKind.ENTRY_LIST, []) and not wc._is_empty(wc.FieldKind.ENTRY_LIST, [object()])
    for kind in (wc.FieldKind.ASSET, wc.FieldKind.INTEGER, wc.FieldKind.ABILITIES):
        assert wc._is_empty(kind, None) and not wc._is_empty(kind, 0 if kind is wc.FieldKind.INTEGER else {})
    # And the rule as a whole document sees it: 0 is a real armour class, and null
    # is not — the one place where "empty" and "falsy" must not be confused.
    assert wc.check_fields(_STATBLOCK, 1, {"name": "Ondrey", "ac": 0, "hp": 0}, whole=True)["ac"] == 0


@pytest.mark.parametrize("key", ["ac", "hp"])
def test_a_patch_that_clears_a_required_field_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match=f"require {key}"):
        wc.check_fields(_STATBLOCK, 1, {key: None}, whole=False)


def test_a_patch_that_does_not_mention_a_required_field_still_works() -> None:
    """A patch touches any subset (CANVAS-10). Only a key it actually **sets** is
    checked, so an autosave of one cell is never refused for a field it left alone."""
    assert wc.check_fields(_STATBLOCK, 1, {"speed": "20 ft."}, whole=False) == {"speed": "20 ft."}
    assert wc.check_fields(_STATBLOCK, 1, {"ac": 12}, whole=False) == {"ac": 12}


def test_a_required_field_refusal_never_quotes_a_value() -> None:
    """X-7, checked over every other field's text in the same payload: the message
    a route answers with, the string form of the error, and the log line.

    ``redacted_errors`` is belt and braces only, and is asserted as such: its
    redaction branch fires for ``extra_forbidden`` alone, and a missing required
    field is a ``value_error`` with an empty location — so a test written against
    it would pass while proving nothing. The assertion that would really catch a
    leak is the one on ``validation_error_body`` and on ``str(ValidationError)``.
    """
    payload = {
        "schema_version": 1,
        "command_id": "cmd_4d1c2b3a9f8e7d6c",
        "campaign_id": "cmp_4b1d9e7a",
        "type": "statblock",
        "type_version": 1,
        "data": {
            "name": "Ondrey",
            "ac": 12,
            "ac_note": _SECRET,
            "languages": "Aquan, and the drowned saint's own",
            "traits": [{"name": "Salt-bound", "text": _SECRET}],
        },
    }
    with pytest.raises(ValidationError) as caught:
        wc.DocumentCreateRequest.model_validate(payload)
    errors = caught.value.errors()
    messages = " ".join(error["msg"] for error in errors)
    assert "require hp" in messages
    for text in (messages, str(caught.value), wc.validation_error_body(errors).model_dump_json()):
        assert "drowned saint" not in text
        assert "Aquan" not in text
    assert "drowned saint" not in json.dumps(wc.redacted_errors(errors))


def test_a_response_and_a_history_snapshot_stay_tolerant_of_a_missing_required_field() -> None:
    """Lead ruling 5.7#1. LIB-12's own words are *"is not valid; nothing is
    **stored** until they are given"*, so the rule binds a **write**. A response
    that refused a stat block whose ``hp`` a data defect or a manual database edit
    lost would show the GM the *"made by a newer version of Aetheril"* placeholder
    for their own document — the precise hazard ``read_stored_fields`` exists to
    remove. Strict writes, strict emission of what the server composes, tolerant
    reads of what is already stored.
    """
    fixture = json.loads((FIXTURES / "Document.json").read_text(encoding="utf-8"))
    whole = next(
        e["value"] for e in fixture["valid"] if e["name"] == "a stat block that uses every field the type declares"
    )
    without_hp = {**whole, "data": {key: v for key, v in whole["data"].items() if key != "hp"}}
    assert "hp" not in wc.Document.model_validate(without_hp).data

    snapshot = {
        "schema_version": 1,
        "document_id": without_hp["document_id"],
        "type": "statblock",
        "type_version": 1,
        "version": without_hp["version"],
        "data": without_hp["data"],
    }
    assert "hp" not in wc.DocumentVersionSnapshot.model_validate(snapshot).data

    # …and the same document is refused the moment it is WRITTEN.
    with pytest.raises(ValidationError, match="require hp"):
        wc.DocumentCreateRequest.model_validate(
            {
                "schema_version": 1,
                "command_id": "cmd_4d1c2b3a9f8e7d6c",
                "campaign_id": without_hp["campaign_id"],
                "type": "statblock",
                "type_version": 1,
                "data": without_hp["data"],
            }
        )


def test_no_valid_fixture_clears_a_required_field_and_every_structured_kind_is_shown_cleared() -> None:
    """Both halves of the sweep, over every fixture file rather than the three
    this bead edited, so a fixture added later joins the check by existing.

    The first half is what stops the required rule being undermined by an example
    that says a cleared required field is fine. The second is what stops it being
    *satisfied* by deleting the examples that show a kind cleared: every structured
    kind must still appear cleared in some valid whole document.
    """
    whole_document_schemas = {"Document", "DocumentCreateRequest", "DocumentVersionSnapshot"}
    structured = {
        wc.FieldKind.ASSET,
        wc.FieldKind.INTEGER,
        wc.FieldKind.ABILITIES,
        wc.FieldKind.TEXT_LIST,
        wc.FieldKind.ENTRY_LIST,
    }
    seen_cleared: set[wc.FieldKind] = set()
    checked = 0
    for path in _fixture_files():
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if fixture["schema"] not in whole_document_schemas:
            continue
        for example in fixture["valid"]:
            raw = expand(example["value"])
            doc_type = wc.DocumentTypeId(raw["type"])
            declared = {**wc.COMMON_FIELDS, **wc.DOC_TYPE_FIELDS[doc_type]}
            data = raw["data"]
            checked += 1
            for key in wc.REQUIRED_FIELDS[doc_type]:
                assert key in data, f"{path.name}: {example['name']} omits the required {key}"
                assert not wc._is_empty(declared[key], data[key]), (
                    f"{path.name}: {example['name']} clears the required {key}"
                )
            for key, value in data.items():
                # A client-only example may carry a key the type does not declare —
                # that is the tolerance it exists to show, and it is not a kind.
                kind = declared.get(key)
                if kind in structured and wc._is_empty(kind, value):
                    seen_cleared.add(kind)
    assert checked >= 20, "the sweep found suspiciously few valid whole documents"
    assert seen_cleared == structured, f"no valid example shows these cleared: {structured - seen_cleared}"


def test_the_contract_document_states_the_rules_this_bead_adds() -> None:
    """``docs/workbench-wire-contract.md`` is where a reader learns what the server
    refuses to store, so a rule that lives only in a docstring is a rule the next
    bead re-derives — or gets wrong. Asserted as literal strings rather than by
    shape, in the same spirit as ``test_the_wire_contracts_tables_are_the_registry``:
    a prose edit that drops one of these is telling you the document no longer says
    what the code does.
    """
    text = (FIXTURES.parents[2] / "docs" / "workbench-wire-contract.md").read_text(encoding="utf-8")
    # F-3: a tolerant stored read exists, and is named, and what the server puts
    # on the wire is still strict.
    assert "read_stored_fields" in text
    assert "what the server stores and emits stays strict" in text
    # Requirement 3: the rule that makes requirement 1 and the tolerant read safe.
    assert "making a declared field required" in text
    # F-10: the number ``1kg.5.5`` needs for its whole-document cap (RAIL-6).
    assert "1,316,510" in text
    # …and the per-field bounds have a home of their own.
    assert "#### Per-field integer bounds" in text


# ── A tolerant read of a stored document (F-3) ───────────────────────────────

_WITH_ABILITIES = [wc.DocumentTypeId.STATBLOCK, wc.DocumentTypeId.CHARACTER_SHEET]
_WITH_ENTRIES = [
    wc.DocumentTypeId.STATBLOCK,
    wc.DocumentTypeId.QUEST_LOG,
    wc.DocumentTypeId.CHARACTER_SHEET,
    wc.DocumentTypeId.ENCOUNTER,
]


def _first_key_of_kind(doc_type: wc.DocumentTypeId, kind: wc.FieldKind) -> str | None:
    return next((key for key, k in wc.DOC_TYPE_FIELDS[doc_type].items() if k is kind), None)


def test_the_types_that_can_prove_each_tolerant_read_clause_are_the_ones_listed() -> None:
    """The three parametrised tests below are only honest if their type lists are
    the types that actually declare the kind. Asserted against ``DOC_TYPE_FIELDS``
    so a list cannot silently shrink — or grow stale when a type gains a kind."""
    assert [d for d in wc.DocumentTypeId if _first_key_of_kind(d, wc.FieldKind.ABILITIES)] == _WITH_ABILITIES
    assert [d for d in wc.DocumentTypeId if _first_key_of_kind(d, wc.FieldKind.ENTRY_LIST)] == _WITH_ENTRIES


@pytest.mark.parametrize("doc_type", list(wc.DocumentTypeId), ids=lambda t: t.value)
def test_a_stored_document_drops_an_undeclared_top_level_key_and_keeps_the_rest(
    doc_type: wc.DocumentTypeId,
) -> None:
    """Release N+1 adds a field and a GM uses it; a rollback to N must leave the
    dossier readable, because *adding a field is not a version bump*. Proved on
    all eight, the way the strict posture already is."""
    stored: dict[str, Any] = {"name": "A document", "tags": ["tide"], "secret_ally": "The harbourmaster"}
    if doc_type is _STATBLOCK:
        stored |= {"ac": 16, "hp": 104}
    read = wc.read_stored_fields(doc_type, 1, stored)
    assert "secret_ally" not in read
    assert read["name"] == "A document" and read["tags"] == ["tide"]
    # …and the very same key is still refused in what the server emits.
    with pytest.raises(ValueError, match="do not declare"):
        wc.check_fields(doc_type, 1, dict(stored), whole=True)


@pytest.mark.parametrize("doc_type", _WITH_ABILITIES, ids=lambda t: t.value)
def test_a_stored_document_drops_an_undeclared_ability_key(doc_type: wc.DocumentTypeId) -> None:
    key = _first_key_of_kind(doc_type, wc.FieldKind.ABILITIES)
    assert key is not None
    stored: dict[str, Any] = {"name": "A document", key: {"str": 10, "luck": 3}}
    if doc_type is _STATBLOCK:
        stored |= {"ac": 16, "hp": 104}
    assert wc.read_stored_fields(doc_type, 1, stored)[key] == {"str": 10}


@pytest.mark.parametrize("doc_type", _WITH_ENTRIES, ids=lambda t: t.value)
def test_a_stored_document_drops_an_undeclared_entry_sub_key(doc_type: wc.DocumentTypeId) -> None:
    key = _first_key_of_kind(doc_type, wc.FieldKind.ENTRY_LIST)
    assert key is not None
    stored: dict[str, Any] = {
        "name": "A document",
        key: [{"name": "Amphibious", "text": "It breathes water.", "damage": "1d6"}],
    }
    if doc_type is _STATBLOCK:
        stored |= {"ac": 16, "hp": 104}
    entries = [entry.model_dump() for entry in wc.read_stored_fields(doc_type, 1, stored)[key]]
    assert entries == [{"name": "Amphibious", "text": "It breathes water."}]


def test_a_stored_document_drops_an_undeclared_asset_sub_key() -> None:
    """The client's non-strict read already strips an unknown asset sub-key, so a
    server read that kept one would make the two tolerant reads disagree — and the
    fuzz would not see it, because neither side is wrong about a *declared* key."""
    stored = {
        "name": "Ondrey",
        "portrait": {"asset_id": "ast_77c1d0e2", "media_type": "image", "alt": "A portrait", "blurhash": "LKO2"},
    }
    read = wc.read_stored_fields(wc.DocumentTypeId.NPC, 1, stored)
    assert read["portrait"].model_dump() == {
        "asset_id": "ast_77c1d0e2",
        "media_type": "image",
        "alt": "A portrait",
        "width": None,
        "height": None,
    }


def test_a_tolerant_read_leaves_the_stored_row_untouched() -> None:
    """Pure: it returns a new dict, so nothing a caller holds is rewritten — the
    row must render again after a roll-forward."""
    stored = {
        "name": "Ondrey",
        "secret_ally": "The harbourmaster",
        "portrait": {"asset_id": "ast_1", "media_type": "image", "alt": "A portrait", "blurhash": "LKO2"},
    }
    before = copy.deepcopy(stored)
    wc.read_stored_fields(wc.DocumentTypeId.NPC, 1, stored)
    assert stored == before


def test_a_tolerant_read_still_refuses_an_invalid_declared_value() -> None:
    """Tolerance is scoped to keys the type does not declare. A declared key whose
    value is wrong is damage, and damage is not read."""
    with pytest.raises(ValueError, match="not a valid text field"):
        wc.read_stored_fields(wc.DocumentTypeId.NPC, 1, {"name": "Ondrey", "voice": 42})
    with pytest.raises(ValueError, match="not a valid integer field"):
        wc.read_stored_fields(_STATBLOCK, 1, {"name": "Ondrey", "ac": -1, "hp": 104})
    with pytest.raises(ValueError, match="cannot be blank"):
        wc.read_stored_fields(wc.DocumentTypeId.NPC, 1, {"name": "  "})
    with pytest.raises(ValueError, match="cannot be blank"):
        wc.read_stored_fields(wc.DocumentTypeId.NPC, 1, {})


def test_a_tolerant_read_does_not_apply_the_required_rule() -> None:
    """Lead ruling 5.7#1 again, at the function the rollback story is about: the
    tolerance here is scoped to the changes that are **not** version bumps — a new
    field, a new kind. Making a field required **is** a bump, so a document written
    before the rule is at an older ``type_version`` and the adapter walk has it."""
    read = wc.read_stored_fields(_STATBLOCK, 1, {"name": "Ondrey", "ac": 16})
    assert read == {"name": "Ondrey", "ac": 16}


def test_a_tolerant_read_refuses_a_type_version_it_does_not_know() -> None:
    """The same typed error ``check_fields`` raises, so a route answers *reload*
    rather than *malformed*. Inventing an opaque document is ``1kg.5.1``'s."""
    with pytest.raises(PydanticCustomError) as caught:
        wc.read_stored_fields(wc.DocumentTypeId.NPC, 2, {"name": "Ondrey"})
    assert caught.value.type == "unsupported_type_version"


def test_nothing_on_the_wire_calls_the_tolerant_read() -> None:
    """*What the server stores and emits stays strict.* The four wire models go on
    calling ``check_fields``; ``read_stored_fields`` is for ``1kg.5.1`` and
    ``1kg.5.2`` to call when they read a stored row, and for nothing else.

    Read from the module's source rather than asserted by behaviour, because the
    claim is about which function is *called* — a behavioural test would pass the
    day someone wired it in and widened a fixture to match.
    """
    source = Path(wc.__file__).read_text(encoding="utf-8")
    body = source[source.index("class Document(") :]
    assert "read_stored_fields" not in body, "a wire model now calls the tolerant read"
    assert body.count("check_fields(self.type, self.type_version") == 4


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


def test_a_stale_client_is_told_to_reload_whatever_pydantic_lists_first() -> None:
    """A v2 client fails on its version and on what v2 added, and Pydantic lists
    the errors in the model's field order. The answer must not depend on it."""
    stale: dict[str, Any] = {"schema_version": 2, "type": "faction", "type_version": 1, "base_write_revision": 3}
    stale["fields"] = {"name": "x"}
    with pytest.raises(ValidationError) as caught:
        wc.FieldPatchRequest.model_validate(stale)  # here ``type`` is listed before ``schema_version``
    body = wc.validation_error_body(caught.value.errors())
    assert (body.detail.code, body.detail.field) == (wc.ErrorCode.UNSUPPORTED_SCHEMA_VERSION, "schema_version")

    # A client built against older field definitions is out of date in the same way.
    with pytest.raises(ValidationError) as caught:
        wc.FieldPatchRequest.model_validate({**stale, "schema_version": 1, "type": "npc", "type_version": 2})
    body = wc.validation_error_body(caught.value.errors())
    assert (body.detail.code, body.detail.field) == (wc.ErrorCode.UNSUPPORTED_SCHEMA_VERSION, "type_version")


@pytest.mark.parametrize(
    ("loc", "field"),
    [
        (("body", "brief"), "brief"),
        (("query", "limit"), "limit"),
        (("path", "document_id"), "document_id"),
        (("header", "x_request_id"), "x_request_id"),
        (("cookie", "gga_session"), "gga_session"),
        # Only the first element says which part of the request FastAPI read.
        (("body", "body"), "body"),
        ((), None),
    ],
)
def test_the_request_part_is_stripped_from_the_location(loc: tuple[str, ...], field: str | None) -> None:
    body = wc.validation_error_body([{"type": "missing", "loc": loc, "msg": "Field required"}])
    assert (body.detail.code, body.detail.field) == (wc.ErrorCode.VALIDATION_FAILED, field)


@pytest.mark.parametrize(
    ("version", "code"),
    [
        (1.0, None),
        (2.0, "unsupported_schema_version"),
        (1.5, "validation_failed"),
        (True, "validation_failed"),
        ("1", "validation_failed"),
    ],
)
def test_a_version_is_an_integral_json_number(version: Any, code: str | None) -> None:
    """``1.0`` in JSON text reaches Python as a float and JavaScript as the number
    1. The two must agree, so an integral value is an integer on both sides."""
    if code is None:
        request = wc.ToolInvocationRequest.model_validate({**_REQUEST, "schema_version": version})
        assert request.schema_version == 1 and isinstance(request.schema_version, int)
        assert request.model_dump_json().startswith('{"schema_version":1,')
        return
    with pytest.raises(ValidationError) as caught:
        wc.ToolInvocationRequest.model_validate({**_REQUEST, "schema_version": version})
    assert wc.validation_error_body(caught.value.errors()).detail.code.value == code


def test_an_integral_float_is_an_integer_and_is_emitted_as_one() -> None:
    top = TypeAdapter(wc.WriteRevision)
    assert top.validate_python(14.0) == 14
    assert top.dump_json(top.validate_python(14.0)) == b"14"
    for bad in [14.5, float("inf"), float("nan"), True, "14"]:
        with pytest.raises(ValidationError):
            top.validate_python(bad)


_LONE_SURROGATE = "x" + chr(0xD83C)
_QUERY = {"schema_version": 1, "campaign_id": "cmp_1", "category": "npcs", "sort": "name", "archived": False}


@pytest.mark.parametrize(
    ("model", "value"),
    [
        (wc.ToolInvocationRequest, {**_REQUEST, "brief": _LONE_SURROGATE}),
        (wc.TextInstruction, {"kind": "text", "text": _LONE_SURROGATE}),
        (wc.LibraryQuery, {**_QUERY, "search": _LONE_SURROGATE}),
        # A bounded string refuses one by itself; this pins that it keeps doing so.
        (wc.SelectionScope, {"kind": "selection", "field": "notes", "start": 0, "end": 2, "text": _LONE_SURROGATE}),
    ],
)
def test_a_lone_surrogate_is_a_422_not_a_500(model: type[BaseModel], value: dict[str, Any]) -> None:
    """JSON allows the escape; UTF-8 does not. Accepted, it would fail on the way
    into the database, or on the way out — this is what a JSONResponse does."""
    with pytest.raises(UnicodeEncodeError):
        json.dumps({"text": _LONE_SURROGATE}, ensure_ascii=False).encode("utf-8")
    with pytest.raises(ValidationError) as caught:
        model.model_validate(value)
    assert wc.validation_error_body(caught.value.errors()).detail.code is wc.ErrorCode.VALIDATION_FAILED


def test_trimming_is_what_javascript_trims() -> None:
    """Both sides trim one explicit set, so neither can find a brief empty that
    the other finds two characters long."""
    bom, nel, ideographic_space = chr(0xFEFF), chr(0x85), chr(0x3000)
    assert wc.trim(f"{bom} CR 5{ideographic_space}\t\n") == "CR 5"
    # NEL is not in JavaScript's set, so it stays — on both sides.
    assert wc.trim(f"{nel}CR 5{nel}") == f"{nel}CR 5{nel}"
    # The disagreements ``str.strip`` would have introduced.
    assert nel.strip() == "" and bom.strip() == bom and wc.trim(bom) == ""

    assert wc.ToolInvocationRequest.model_validate({**_REQUEST, "tool_id": "recap", "brief": bom}).brief == ""
    with pytest.raises(ValidationError) as caught:
        wc.ToolInvocationRequest.model_validate({**_REQUEST, "brief": bom})
    assert wc.validation_error_body(caught.value.errors()).detail.code is wc.ErrorCode.BRIEF_REQUIRED
    # A name that only the trim empties is blank. A byte order mark alone never
    # gets that far: stored text refuses it as a character (requirement 6).
    with pytest.raises(ValueError, match="cannot be blank"):
        wc.check_fields(wc.DocumentTypeId.NPC, 1, {"name": ideographic_space}, whole=True)
    with pytest.raises(ValueError, match="must not contain a byte order mark"):
        wc.check_fields(wc.DocumentTypeId.NPC, 1, {"name": bom}, whole=True)


def test_nothing_of_a_rejected_request_reaches_a_traceback() -> None:
    """X-7 for logs: ``str(exc)``, ``repr(exc)`` and a formatted traceback name
    what was wrong and never what was sent — for a value-level error, for a field
    error re-raised from ``check_fields`` (its cause is cut), and for an undeclared
    key among the fields."""
    import traceback

    cases: list[tuple[type[BaseModel], dict[str, Any]]] = [
        (wc.ToolInvocationRequest, {**_REQUEST, "brief": [_SECRET]}),
        (wc.Document, _document(wants=[_SECRET])),
        (
            wc.FieldPatchRequest,
            {"schema_version": 1, "type": "npc", "type_version": 1, "base_write_revision": 3, "fields": {_SECRET: "x"}},
        ),
    ]
    for model, value in cases:
        with pytest.raises(ValidationError) as caught:
            model.model_validate(value)
        exc = caught.value
        rendered = "\n".join([str(exc), repr(exc), *traceback.format_exception(exc)])
        assert "drowned saint" not in rendered, model.__name__
        assert "drowned saint" not in json.dumps(exc.errors(include_input=False), default=str)


def test_redacted_errors_are_fit_for_a_log_line() -> None:
    with pytest.raises(ValidationError) as caught:
        wc.ToolInvocationRequest.model_validate({**_REQUEST, "brief": [_SECRET], _SECRET: "x"})
    raw = caught.value.errors()
    # The hazard: ``errors()`` carries the input, and an undeclared key is its own location.
    assert all("drowned saint" in json.dumps(error, default=str) for error in raw)
    redacted = wc.redacted_errors(raw)
    assert "drowned saint" not in json.dumps(redacted)
    assert {error["type"] for error in redacted} == {"string_type", "extra_forbidden"}
    assert all(set(error) == {"type", "loc", "msg"} for error in redacted)
    assert [error["loc"] for error in redacted if error["type"] == "extra_forbidden"] == [["(undeclared)"]]


def test_a_route_that_uses_it_echoes_nothing_where_the_default_echoes_everything() -> None:
    """X-7, shown end to end. FastAPI's default 422 returns each error's ``input``
    — the request itself. A Workbench route installs this handler instead."""
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    logged: list[tuple[str, list[dict[str, Any]]]] = []

    def build(*, safe: bool) -> TestClient:
        app = FastAPI()

        @app.post("/documents/doc_1/fields")
        def patch(body: wc.FieldPatchRequest) -> dict[str, str]:
            return {}

        if safe:

            @app.exception_handler(RequestValidationError)
            async def answer(_request: Request, exc: RequestValidationError) -> JSONResponse:
                logged.append((str(exc), wc.redacted_errors(exc.errors())))
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
    # FastAPI's exception prints the request; a handler logs the redacted list, never ``str(exc)``.
    [(printed, redacted)] = logged
    assert _SECRET in printed and _SECRET not in json.dumps(redacted)


def test_a_write_revision_survives_javascript() -> None:
    """The token must round-trip through ``JSON.parse``, so it stops at 2**53 - 1."""
    assert wc.WRITE_REVISION_MAX == 9_007_199_254_740_991
    top = TypeAdapter(wc.WriteRevision)
    assert top.validate_python(wc.WRITE_REVISION_MAX) == wc.WRITE_REVISION_MAX
    with pytest.raises(ValidationError):
        top.validate_python(wc.WRITE_REVISION_MAX + 1)


# ── Media, sessions and realtime ─────────────────────────────────────────────


def test_the_event_kind_vocabularies_are_the_unions() -> None:
    """What ``1kg.7.5`` and ``1kg.8.6`` emit is pinned to the models, per channel."""
    gm = {"tool_lane", "edit_lane", "session", "audio", "slot", "snapshot", "presence", "asset", "ready", "reconnect"}
    table = {"session", "inactive", "audio", "slot", "snapshot", "ready", "reconnect"}
    for union, kinds in ((wc.GmEvent, gm), (wc.TableEvent, table)):
        tags = {get_args(member.model_fields["event"].annotation)[0] for member in get_args(get_args(union)[0])}
        assert tags == kinds


def test_a_table_secret_is_what_the_server_mints() -> None:
    """SEC-5: 32 bytes from the CSPRNG, as ``secrets.token_urlsafe`` spells them."""
    import secrets

    adapter = TypeAdapter(wc.TableSecret)
    for _ in range(20):
        adapter.validate_python(secrets.token_urlsafe(32))
    for wrong in (secrets.token_urlsafe(31), secrets.token_urlsafe(33), secrets.token_hex(32)):
        with pytest.raises(ValidationError):
            adapter.validate_python(wrong)


def test_a_session_answer_carries_the_token_once_and_the_session_never_does() -> None:
    fixture = json.loads((FIXTURES / "TableSessionAnswer.json").read_text(encoding="utf-8"))
    started = next(e["value"] for e in fixture["valid"] if e["name"].startswith("started"))
    answer = wc.TableSessionAnswer.model_validate(started)
    dumped = answer.model_dump(mode="json")
    assert dumped["token"] == started["token"]
    assert "token" not in dumped["session"]
    with pytest.raises(ValidationError):
        wc.TableSession.model_validate({**started["session"], "token": started["token"]})


def test_an_asset_is_measured_only_once_it_is_ready() -> None:
    fixture = json.loads((FIXTURES / "Asset.json").read_text(encoding="utf-8"))
    ready = next(e["value"] for e in fixture["valid"] if e["name"] == "a ready portrait")
    asset = wc.Asset.model_validate(ready)
    assert (asset.width, asset.height, asset.duration_ms) == (1024, 1280, None)
    with pytest.raises(ValidationError, match="dimensions"):
        wc.Asset.model_validate({**ready, "state": "processing"})
    with pytest.raises(ValidationError, match="pixels"):
        wc.Asset.model_validate({**ready, "width": 5001, "height": 5000})


def test_a_one_shot_never_loops_on_either_channel() -> None:
    gm = json.loads((FIXTURES / "GmEvent.json").read_text(encoding="utf-8"))
    table = json.loads((FIXTURES / "TableEvent.json").read_text(encoding="utf-8"))
    gm_playing = next(e["value"] for e in gm["valid"] if e["name"].startswith("ambience is playing"))
    table_playing = next(e["value"] for e in table["valid"] if e["name"].startswith("ambience is playing"))
    for schema, frame in (("GmEvent", gm_playing), ("TableEvent", table_playing)):
        short = {**frame["playing"], "loop": False, "duration_ms": 12_000}
        wc.CONTRACT_SCHEMAS[schema].validate_python({**frame, "slot": "one_shot", "playing": short})
        with pytest.raises(ValidationError, match="one-shot"):
            wc.CONTRACT_SCHEMAS[schema].validate_python({**frame, "slot": "one_shot"})


def test_a_stat_block_inside_an_entry_keeps_its_wire_alias() -> None:
    """``Abilities.int_`` is ``int`` on the wire; the timeline must not change that."""
    raw = _valid_entry("a plain GM-mode message is an ordinary chat exchange (RAIL-14), here with a stat block")
    raw["answer"]["stat_block"]["abilities"] = {"str": 18, "int": 6}
    entry = wc.ChatEntry.model_validate(raw)
    dumped = entry.model_dump(mode="json", by_alias=True)
    assert dumped["answer"]["stat_block"]["abilities"]["int"] == 6


# ── Plain text: what stored text refuses (1kg.5.7.2, requirement 6) ──────────

#: The lead ruling of 2026-09-21, as literal inclusive ranges, built with ``chr``
#: so that no invisible character sits in this file. ``contracts.test.ts`` pins
#: ``REFUSED_TEXT_CODE_POINTS`` to the same list, range for range.
_REFUSED_RANGES = [
    (0x0000, 0x0008),
    (0x000B, 0x000C),
    (0x000E, 0x001F),
    (0x007F, 0x009F),
    (0x061C, 0x061C),
    (0x200E, 0x200F),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
]
_REFUSED = [chr(code) for low, high in _REFUSED_RANGES for code in range(low, high + 1)]
#: What stays allowed: a tab, real names' joiners and an emoji's presentation.
_ALLOWED = {
    "an emoji": chr(0x1F3B2),
    "an accented letter": chr(0xE9),
    "a tab": chr(0x09),
    "a zero width non-joiner": chr(0x200C),
    "a zero width joiner": chr(0x200D),
    "a variation selector-16": chr(0xFE0F),
}


def _npc(**fields: Any) -> dict[str, Any]:
    return wc.check_fields(wc.DocumentTypeId.NPC, 1, {"name": "Sister Ondrey", **fields}, whole=True)


def _quest(entry: dict[str, str]) -> dict[str, Any]:
    data = {"name": "The Mill Road", "open_threads": [entry]}
    return wc.check_fields(wc.DocumentTypeId.QUEST_LOG, 1, data, whole=True)


_DOCUMENT_TEXT_SITES = {
    "text": lambda s: _npc(voice=f"Low{s}and slow"),
    "prose": lambda s: _npc(notes=f"Keeps{s}the ledger"),
    "a text_list item": lambda s: _npc(tags=["abbey", f"mill{s}road"]),
    "an entry name": lambda s: _quest({"name": f"Find{s}the ledger", "text": "Under the mill."}),
    "an entry text": lambda s: _quest({"name": "Find the ledger", "text": f"Under{s}the mill."}),
}


def test_the_refused_code_points_are_the_rulings_table() -> None:
    """One table, both languages: the vitest twin pins the same literal ranges."""
    assert frozenset(ord(char) for char in _REFUSED) == wc.REFUSED_TEXT_CODE_POINTS
    assert len(wc.REFUSED_TEXT_CODE_POINTS) == 75
    assert not {ord(char) for char in _ALLOWED.values()} & wc.REFUSED_TEXT_CODE_POINTS
    for char in _REFUSED:
        with pytest.raises(ValueError, match="must not contain"):
            wc.check_plain_text(f"a{char}b")
    for char in _ALLOWED.values():
        assert wc.check_plain_text(f"a{char}b") == f"a{char}b"


@pytest.mark.parametrize("site", list(_DOCUMENT_TEXT_SITES))
def test_every_document_text_kind_refuses_the_whole_table(site: str) -> None:
    """Requirement 6 / AC 15: text, prose, each list item, an entry's name and its
    text. PostgreSQL's ``text`` and ``jsonb`` refuse U+0000, so without this a NUL
    is a 500 at write time rather than a 422 naming the field."""
    write = _DOCUMENT_TEXT_SITES[site]
    for char in _REFUSED:
        with pytest.raises(ValueError, match="must not contain"):
            write(char)
    for char in _ALLOWED.values():
        write(char)


def test_a_line_break_stays_allowed_where_it_already_was() -> None:
    """Line feed and carriage return are not in the table: prose and an entry's
    text keep their paragraphs, and a one-line value still refuses them, as before."""
    assert _npc(notes="One.\nTwo.\r\nThree.")["notes"] == "One.\nTwo.\r\nThree."
    assert _quest({"name": "Find the ledger", "text": "Under\nthe mill."})["open_threads"][0].text == "Under\nthe mill."
    with pytest.raises(ValueError, match="single line"):
        _npc(voice="Low\nand slow")


@pytest.mark.parametrize(
    ("char", "what"),
    [
        (chr(0x00), "a control character"),
        (chr(0x202E), "a bidirectional control character"),
        (chr(0xFEFF), "a byte order mark"),
    ],
    ids=["NUL", "RLO", "BOM"],
)
def test_a_plain_text_refusal_names_the_field_and_the_class_and_never_the_value(char: str, what: str) -> None:
    """X-7: the refusal names the field and the class, never the value. The
    validation message is where the field and the class are named; the 422 body
    is the generic envelope every ``check_fields`` refusal already answers with,
    and it must carry no trace of the value either."""
    patch = _fixture_value("FieldPatchRequest", "one field, committed on blur")
    patch["fields"] = {"voice": f"Vashti{char}whispers", "tell": "Hums a hymn off key"}
    with pytest.raises(ValidationError) as caught:
        wc.FieldPatchRequest.model_validate(patch)
    message = str(caught.value)
    assert "voice is not a valid text field" in message and what in message
    body = json.dumps(wc.validation_error_body(caught.value.errors()).model_dump(mode="json"))
    assert wc.validation_error_body(caught.value.errors()).detail.code is wc.ErrorCode.VALIDATION_FAILED
    for text in (message, body, json.dumps(wc.redacted_errors(caught.value.errors()))):
        for secret in ("Vashti", "whispers", "Hums a hymn"):
            assert secret not in text


@pytest.mark.parametrize("shape", ["_PresentText", "_PresentProse", "_PresentListItem"])
def test_a_projection_refuses_what_a_document_refuses(shape: str) -> None:
    """Requirement 6: a value a document refuses can never ride in a projection
    instead. ``_PresentEntry`` is a name and a text of these shapes."""
    adapter: TypeAdapter[str] = TypeAdapter(getattr(wc, shape))
    for char in _REFUSED:
        with pytest.raises(ValidationError, match="must not contain"):
            adapter.validate_python(f"Sister{char}Ondrey")
    for char in _ALLOWED.values():
        assert adapter.validate_python(f"Sister{char}Ondrey") == f"Sister{char}Ondrey"


def test_the_refusal_stops_at_the_document_kinds_and_the_projection() -> None:
    """AC 15 and ruling 3: the scope boundary is pinned rather than assumed. A
    version's summary, a brief, chat text and a cue title are bead ``5mj``'s, and
    each still accepts a NUL until it lands."""
    nul = chr(0)
    version = _fixture_value("DocumentVersion", "an assistant pass")
    version["summary"] = f"Wants{nul}the signet"
    assert wc.DocumentVersion.model_validate(version).summary == f"Wants{nul}the signet"
    assert wc.ToolInvocationRequest.model_validate({**_REQUEST, "brief": f"a{nul}guard"}).brief == f"a{nul}guard"
    chat = _valid_entry("a chat exchange with its complete outcome")
    chat["answer"]["text"] = f"A basilisk{nul} petrifies."
    wc.CONTRACT_SCHEMAS["TimelineEntry"].validate_python(chat)
    assert wc.CueRenameRequest.model_validate({"schema_version": 1, "title": f"Rain{nul}"}).title == f"Rain{nul}"


def _fixture_value(schema: str, name: str) -> dict[str, Any]:
    fixture = json.loads((FIXTURES / f"{schema}.json").read_text(encoding="utf-8"))
    value: dict[str, Any] = copy.deepcopy(next(e["value"] for e in fixture["valid"] if e["name"] == name))
    return value


# ── M-2: a stored value that is not an object (1kg.5.7.2) ────────────────────


@pytest.mark.parametrize("data", ["x", [], None, 7], ids=["str", "list", "None", "int"])
def test_a_stored_value_that_is_not_an_object_is_a_typed_refusal(data: Any) -> None:
    """M-2: ``data`` comes out of ``jsonb``, which holds any JSON value. Before,
    each of these was an untyped ``AttributeError`` from ``.items()``; now it is
    the typed refusal, like every other one on this path."""
    with pytest.raises(PydanticCustomError) as caught:
        wc.read_stored_fields(wc.DocumentTypeId.NPC, 1, data)
    assert caught.value.type == "stored_data_not_an_object"
    assert str(caught.value) == "stored npc document data is not a JSON object"


def test_a_stored_value_of_an_unknown_version_is_refused_for_its_version_first() -> None:
    with pytest.raises(PydanticCustomError) as caught:
        wc.read_stored_fields(wc.DocumentTypeId.NPC, 2, "x")  # type: ignore[arg-type]
    assert caught.value.type == "unsupported_type_version"

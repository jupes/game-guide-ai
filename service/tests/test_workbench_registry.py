"""The canonical GM tool and document-type registry (1kg.3.1).

``contracts/workbench/v1/registry.json`` is the one fixture; this suite and
``ui/src/gm/registry.test.ts`` both compare their language's copy against it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from service import workbench_registry as reg
from service.workbench_contracts import ToolId

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "workbench" / "v1"
REGISTRY_JSON = json.loads((FIXTURES / "registry.json").read_text(encoding="utf-8"))


def _rule(rule: reg.FieldRule) -> dict[str, object]:
    return {
        "label": rule.label,
        "editable": rule.editable,
        "revealable": rule.revealable,
        "warning": rule.warning,
    }


def test_the_registry_is_the_shared_file() -> None:
    """Every fact, in the file's order (registry order is menu order, SLASH-9)."""
    assert [
        {
            "id": tool.id.value,
            "result_kind": tool.result_kind.value,
            "brief": tool.brief.value,
            "creates_doc_type": tool.creates_doc_type.value if tool.creates_doc_type else None,
            "card_kind": tool.card_kind.value if tool.card_kind else None,
            "command": tool.command,
            "aliases": list(tool.aliases),
            "label": tool.label,
            "icon": tool.icon,
            "blurb": tool.blurb,
            "working_label": tool.working_label,
            "capability": tool.capability,
        }
        for tool in reg.REGISTRY.tools
    ] == REGISTRY_JSON["tools"]
    assert [
        {
            "id": doc.id.value,
            "library_category": doc.library_category.value,
            "type_version": doc.type_version,
            "fields": {key: kind.value for key, kind in doc.fields.items()},
            "field_rules": {key: _rule(rule) for key, rule in doc.field_rules.items()},
            "label": doc.label,
            "icon": doc.icon,
            "renderer": doc.renderer,
            "printable": doc.printable,
            "cites_corpus": doc.cites_corpus,
            "audience": doc.audience,
            "accent": doc.accent,
            "reveal_groups": [
                {"id": group.id, "label": group.label, "keys": list(group.keys)} for group in doc.reveal_groups
            ],
            "default_reveal": {audience: list(keys) for audience, keys in doc.default_reveal.items()},
            "reserved_keys": list(doc.reserved_keys),
        }
        for doc in reg.REGISTRY.document_types
    ] == REGISTRY_JSON["document_types"]
    assert [
        {"id": cap.id, "label": cap.label, "disabled_reason": cap.disabled_reason} for cap in reg.REGISTRY.capabilities
    ] == REGISTRY_JSON["capabilities"]
    assert [pin.value for pin in reg.REGISTRY.default_pinned] == REGISTRY_JSON["default_pinned"]
    assert reg.REGISTRY.rail_limit == REGISTRY_JSON["rail_limit"] == reg.RAIL_LIMIT
    assert list(reg.REGISTRY.renderers) == REGISTRY_JSON["renderers"]
    assert {key: _rule(rule) for key, rule in reg.REGISTRY.common_field_rules.items()} == REGISTRY_JSON[
        "common_field_rules"
    ]
    # The two vocabularies the new per-type flags are checked against. This test
    # names its top-level facts one by one, so anything unnamed has no parity check.
    assert list(reg.REGISTRY.accents) == REGISTRY_JSON["accents"]
    assert list(reg.REGISTRY.audiences) == REGISTRY_JSON["audiences"]


def test_the_capability_switches_are_the_wire_shape() -> None:
    """The lookup answers exactly the switches the registry names (RAIL-10)."""
    from service.workbench_contracts import Capabilities

    assert set(Capabilities.model_fields) - {"schema_version"} == {cap.id for cap in reg.REGISTRY.capabilities}
    assert {cap.id for cap in reg.REGISTRY.capabilities} == {cap["id"] for cap in REGISTRY_JSON["capabilities"]}


def test_unknown_is_never_npc() -> None:
    """X-8: the handoff's ``documentType(id)`` fell back to the NPC config."""
    assert reg.REGISTRY.document_type("faction") is None
    assert reg.REGISTRY.tool("npcs") is None
    npc = reg.REGISTRY.tool("npc")
    assert npc is not None and npc.label == "NPC"


def test_a_tool_answers_to_its_command_and_its_aliases() -> None:
    """SLASH-2 on the client; here the registry pins the data the parser uses."""
    hooks = reg.REGISTRY.tool("hooks")
    assert hooks is not None and hooks.commands == ("/hook", "/hooks")


_LOOKUP_FAILED = "Couldn't check which tools are available"


@pytest.mark.parametrize(
    ("enabled", "portrait", "npc"),
    [
        (None, (False, _LOOKUP_FAILED), (False, _LOOKUP_FAILED)),
        ({}, (False, "Image generation isn't set up yet."), (True, None)),
        ({"image_generation": False}, (False, "Image generation isn't set up yet."), (True, None)),
        ({"image_generation": True}, (True, None), (True, None)),
    ],
)
def test_availability_says_why_a_tool_is_disabled(
    enabled: dict[str, bool] | None, portrait: tuple[bool, str | None], npc: tuple[bool, str | None]
) -> None:
    """RAIL-10 and AE-58: disabled tools stay visible, with a reason."""
    availability = reg.REGISTRY.availability(enabled)
    assert set(availability) == set(ToolId)
    assert (availability[ToolId.PORTRAIT].enabled, availability[ToolId.PORTRAIT].reason) == portrait
    assert (availability[ToolId.MAP].enabled, availability[ToolId.MAP].reason) == portrait
    assert (availability[ToolId.NPC].enabled, availability[ToolId.NPC].reason) == npc


def _broken(**changes: object) -> reg.Registry:
    return replace(reg.REGISTRY, **changes)  # type: ignore[arg-type]


def _with_tool(tool: reg.Tool) -> tuple[reg.Tool, ...]:
    return tuple(tool if existing.id is tool.id else existing for existing in reg.REGISTRY.tools)


def _tool(tool_id: str) -> reg.Tool:
    found = reg.REGISTRY.tool(tool_id)
    assert found is not None
    return found


def _with_first_type(**changes: object) -> tuple[reg.DocumentType, ...]:
    first, *rest = reg.REGISTRY.document_types
    return (replace(first, **changes), *rest)  # type: ignore[arg-type]


_NPC_RULES = dict(reg.REGISTRY.document_types[0].field_rules)


def _with_npc_rules(**changes: reg.FieldRule) -> tuple[reg.DocumentType, ...]:
    return _with_first_type(field_rules={**_NPC_RULES, **changes})


def _character_sheet(**changes: object) -> tuple[reg.DocumentType, ...]:
    return tuple(
        replace(doc, **changes) if doc.id is reg.DocumentTypeId.CHARACTER_SHEET else doc  # type: ignore[arg-type]
        for doc in reg.REGISTRY.document_types
    )


@pytest.mark.parametrize(
    ("registry", "problem"),
    [
        (_broken(tools=_with_tool(replace(_tool("loot"), aliases=("/NPC",)))), "collides with npc"),
        (_broken(tools=_with_tool(replace(_tool("loot"), aliases=("loot",)))), "not a well-formed command"),
        (_broken(tools=_with_tool(replace(_tool("loot"), capability="teleportation"))), "unknown capability"),
        (_broken(tools=_with_tool(replace(_tool("loot"), icon="Diamond!"))), "not a ligature name"),
        (_broken(tools=_with_tool(replace(_tool("loot"), blurb="Treasure &amp; hoards"))), "HTML entity"),
        (_broken(tools=reg.REGISTRY.tools[:-1]), "every tool of the contract"),
        (_broken(tools=(*reg.REGISTRY.tools, _tool("loot"))), "duplicate tool ids"),
        (
            _broken(tools=_with_tool(replace(_tool("loot"), creates_doc_type=reg.DocumentTypeId.NPC))),
            "only a document result",
        ),
        (_broken(tools=_with_tool(replace(_tool("npc"), card_kind=reg.CardKind.STAT_BLOCK))), "only a card result"),
        (_broken(document_types=reg.REGISTRY.document_types[1:]), "creates an unknown document type"),
        (_broken(document_types=_with_first_type(icon="Person!")), "not a ligature name"),
        (_broken(default_pinned=tuple(ToolId)[:6]), "at most 5"),
        (_broken(default_pinned=(ToolId.NPC, ToolId.NPC)), "each once"),
        (_broken(default_pinned=(ToolId.PORTRAIT,)), "off by default"),
        (_broken(document_types=_with_first_type(renderer="table")), "unknown renderer"),
        (_broken(document_types=_with_npc_rules(xp_budget=reg.FieldRule("XP budget"))), "every declared field"),
        (_broken(document_types=_with_npc_rules(notes=reg.FieldRule("Terrain &amp; hazards"))), "HTML entity"),
        # 1kg.5.3: a label is for a person, and the validator now says so.
        (_broken(document_types=_with_npc_rules(notes=reg.FieldRule(""))), "is empty"),
        (_broken(document_types=_with_npc_rules(notes=reg.FieldRule("x" * 61))), "longer than 60"),
        (_broken(document_types=_with_npc_rules(notes=reg.FieldRule("loose_threads"))), "snake_case"),
        (_broken(document_types=_with_npc_rules(notes=reg.FieldRule("ifAttacked"))), "camelCase"),
        # ED-2 / CANVAS-19: a declared key is flat, snake_case and bounded.
        (_broken(document_types=_with_first_type(reserved_keys=("motives.hidden",))), "not a flat field key"),
        (_broken(document_types=_with_first_type(reserved_keys=("notes",))), "cannot be declared as a field again"),
        # REVEAL-11: a warning is about revealing, so an unrevealable key cannot carry one.
        (
            _broken(document_types=_with_npc_rules(notes=reg.FieldRule("Notes", revealable=False, warning="Careful"))),
            "can never be revealed",
        ),
        (
            _broken(
                document_types=_with_first_type(
                    reveal_groups=(reg.RevealGroup("secrets", "Secrets", ("name", "tags")),)
                )
            ),
            "can never be revealed",
        ),
        (
            _broken(
                document_types=_with_first_type(reveal_groups=(reg.RevealGroup("solo", "Solo", ("name",)),))
            ),
            "fewer than two keys",
        ),
        (
            _broken(
                document_types=_with_first_type(
                    reveal_groups=(
                        reg.RevealGroup("a", "Voice one", ("name", "voice")),
                        reg.RevealGroup("b", "Voice two", ("voice", "tell")),
                    )
                )
            ),
            "more than one reveal group",
        ),
        # REVEAL-4 and AUD-12: a default seeds the type's own audience, never another's.
        (_broken(document_types=_character_sheet(default_reveal={"table": ()})), "the type's audience is 'owner'"),
        (_broken(document_types=_with_first_type(default_reveal={"table": ("tags",)})), "can never be revealed"),
        (_broken(document_types=_with_first_type(default_reveal={"table": ("nonesuch",)})), "undeclared field"),
        (_broken(document_types=_with_first_type(audience="everyone")), "unknown audience"),
        (_broken(document_types=_with_first_type(accent="neon")), "unknown accent"),
    ],
)
def test_a_registry_that_breaks_a_rule_cannot_be_built(registry: reg.Registry, problem: str) -> None:
    """The bead's AC, as one validator: duplicates and collisions (SLASH-2), unknown
    kinds, types, capabilities and renderers, unsafe defaults, too many pins (RAIL-11)."""
    with pytest.raises(reg.RegistryError, match=problem):
        reg.validate(registry)


def _doc(type_id: str) -> reg.DocumentType:
    found = reg.REGISTRY.document_type(type_id)
    assert found is not None
    return found


def test_every_per_type_flag_is_read_by_a_selector() -> None:
    """The bead's AC: a flag nothing reads is removed. A type entry's keys split
    in two, and both halves are pinned, so neither can grow quietly."""
    keys = {key for doc in REGISTRY_JSON["document_types"] for key in doc}
    assert keys == reg.TYPE_STRUCTURE_KEYS | set(reg.TYPE_FLAG_SELECTORS)
    assert not reg.TYPE_STRUCTURE_KEYS & set(reg.TYPE_FLAG_SELECTORS)
    for flag, selectors in reg.TYPE_FLAG_SELECTORS.items():
        assert flag in keys, f"{flag} is read by a selector but is not a registry flag"
        for name in selectors:
            assert callable(getattr(reg.REGISTRY, name)), f"{flag}: {name} is not a selector"


def test_each_selector_answers_the_flag_it_names() -> None:
    """Not just callable — reading the fact the registry actually holds."""
    assert reg.REGISTRY.renderer_for(_doc("statblock")) == "stat_block_card"
    assert reg.REGISTRY.is_printable(_doc("handout")) and not reg.REGISTRY.is_printable(_doc("npc"))
    assert reg.REGISTRY.cites_corpus(_doc("lore")) and not reg.REGISTRY.cites_corpus(_doc("npc"))
    assert reg.REGISTRY.accent_token(_doc("lore")) == "arcane"
    assert reg.REGISTRY.accent_token(_doc("npc")) is None


def test_only_the_character_sheet_offers_an_audience_picker() -> None:
    """Decision AUD-9: every other type is table-only in v1."""
    owners = [doc.id.value for doc in reg.REGISTRY.document_types if reg.REGISTRY.shows_audience_picker(doc)]
    assert owners == ["character-sheet"]
    assert reg.REGISTRY.audience_of(_doc("character-sheet")) == "owner"


def test_a_default_reveal_answers_empty_for_an_audience_its_type_does_not_name() -> None:
    """Decision REVEAL-4: the table default of an owner-audience type is *empty*,
    not absent — a caller never gets ``None`` and never an error (AUD-12)."""
    assert reg.REGISTRY.default_reveal_for(_doc("character-sheet"), "table") == ()
    assert reg.REGISTRY.default_reveal_for(_doc("npc"), "owner") == ()
    assert reg.REGISTRY.default_reveal_for(_doc("npc"), "table") == ("portrait", "name", "voice")


def test_the_ability_row_is_derived_from_the_fields() -> None:
    """The handoff's ``hasAbilityRow`` was read by nothing and could disagree
    with the fields; a lookup cannot."""
    assert reg.REGISTRY.ability_row_key(_doc("statblock")) == "abilities"
    assert reg.REGISTRY.ability_row_key(_doc("npc")) is None


def test_a_type_cannot_declare_two_ability_blocks() -> None:
    two = {**dict(_doc("statblock").fields), "stats": reg.FieldKind.ABILITIES}
    rules = {**dict(_doc("statblock").field_rules), "stats": reg.FieldRule("Stats")}
    broken = _broken(
        document_types=tuple(
            replace(doc, fields=two, field_rules=rules) if doc.id.value == "statblock" else doc
            for doc in reg.REGISTRY.document_types
        )
    )
    with pytest.raises(reg.RegistryError, match="at most one ability block"):
        reg.validate(broken)


def test_tags_are_never_revealable_and_the_allowlist_is_the_classifiable_list() -> None:
    """REVEAL-10 and ED-5: everything off the allowlist is ``gm_only`` by
    construction, and ``tags`` is off it on every type."""
    for doc in reg.REGISTRY.document_types:
        allowlist = reg.REGISTRY.revealable_keys(doc)
        assert "tags" not in allowlist
        assert set(allowlist) <= set(REGISTRY_JSON["common_fields"]) | set(doc.fields)


def test_an_identity_link_is_never_revealable() -> None:
    """Decision ED-20: "X *is* Y" is a GM-only relation, so it is a field off
    the allowlist and cannot be widened by any action."""
    npc = _doc("npc")
    for key in ("true_identity",):
        rule = reg.REGISTRY.rule_for(npc, key)
        if rule is not None:  # declared by the checkpoint that adds it
            assert not rule.revealable, f"{key} must never be revealable"


def test_a_reveal_group_is_found_by_any_of_its_keys() -> None:
    """Decision REVEAL-11, replacing the handoff's hard-coded row."""
    npc = _doc("npc")
    group = reg.REGISTRY.reveal_group_for(npc, "voice")
    assert group is not None and group.id == "name_and_voice" and group.label == "Name & voice"
    assert reg.REGISTRY.reveal_group_for(npc, "name") is group
    assert reg.REGISTRY.reveal_group_for(npc, "notes") is None


def test_the_warning_copy_comes_from_the_registry_not_from_a_hard_coded_key_list() -> None:
    """REVEAL-11: the handoff hard-coded ``wants || leverage || outcome`` and one
    string; the copy is per field and in the type's own words."""
    npc = _doc("npc")
    assert reg.REGISTRY.warning_for(npc, "wants") == "Would spoil the lie"
    assert reg.REGISTRY.warning_for(npc, "leverage") == "Would spoil the lie"
    assert reg.REGISTRY.warning_for(npc, "notes") is None


def test_the_labels_are_derived_from_the_rules() -> None:
    """One place holds a label, so ``fieldLabel`` and the rules cannot drift."""
    for doc in reg.REGISTRY.document_types:
        assert dict(doc.field_labels) == {key: rule.label for key, rule in doc.field_rules.items()}
    assert dict(reg.REGISTRY.common_field_labels) == {"name": "Name", "qualifier": "Qualifier", "tags": "Tags"}


def test_a_rule_is_never_invented_for_a_key_the_type_does_not_declare() -> None:
    """X-8 again: unknown is never a default, because a default visibility is a
    decision made by accident."""
    assert reg.REGISTRY.rule_for(_doc("npc"), "nonesuch") is None
    assert reg.REGISTRY.rule_for(_doc("npc"), "constructor") is None
    assert reg.REGISTRY.rule_for(_doc("npc"), "tags") is not None


def test_the_reserved_key_lists_are_pinned() -> None:
    """Decision ED-24: nothing is retired yet, and this test is what makes
    adding to a list — or quietly removing an entry — a deliberate act."""
    assert {doc.id.value: list(doc.reserved_keys) for doc in reg.REGISTRY.document_types} == {
        doc.id.value: [] for doc in reg.REGISTRY.document_types
    }


def test_every_problem_is_reported_at_once() -> None:
    loot = replace(_tool("loot"), aliases=("/NPC",), icon="!")
    broken = _broken(tools=_with_tool(loot), default_pinned=tuple(ToolId)[:6])
    with pytest.raises(reg.RegistryError) as caught:
        reg.validate(broken)
    assert str(caught.value).count(";") >= 2

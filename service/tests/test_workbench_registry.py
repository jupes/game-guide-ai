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
        "required": rule.required,
        "revealable": rule.revealable,
        "warning": rule.warning,
        "bounds": list(rule.bounds) if rule.bounds is not None else None,
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
_COMMON_RULES = dict(reg.REGISTRY.common_field_rules)


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
        # The branches the first round of review found untested.
        (_broken(document_types=_with_first_type(reserved_keys=("gone", "gone"))), "listed twice"),
        (
            _broken(
                document_types=_with_first_type(
                    reveal_groups=(reg.RevealGroup("Name Voice", "Name & voice", ("name", "voice")),)
                )
            ),
            "is not a flat key",
        ),
        (
            _broken(
                document_types=_with_first_type(
                    reveal_groups=(reg.RevealGroup("ghosts", "Ghosts", ("name", "nonesuch")),)
                )
            ),
            "undeclared field",
        ),
        (
            _broken(
                document_types=_with_first_type(
                    reveal_groups=(
                        reg.RevealGroup("same", "One", ("name", "voice")),
                        reg.RevealGroup("same", "Two", ("tell", "attitude")),
                    )
                )
            ),
            "share an id",
        ),
        (
            _broken(document_types=_with_first_type(default_reveal={"table": ("name", "name")})),
            "names a key twice",
        ),
        (_broken(common_field_rules={**_COMMON_RULES, "nonesuch": reg.FieldRule("Nonesuch")}), "not a common field"),
        (_broken(common_field_rules={"name": reg.FieldRule("Name")}), "every common field has a rule"),
        (_broken(common_field_rules={**_COMMON_RULES, "tags": reg.FieldRule("tagList")}), "camelCase"),
        # REVEAL-10 / ED-5, on both routes into the allowlist: the common rule
        # and a type's own. The second was the hole the first review's fix left.
        (
            _broken(common_field_rules={**_COMMON_RULES, "tags": reg.FieldRule("Tags", revealable=True)}),
            "never revealable",
        ),
        (_broken(document_types=_with_npc_rules(tags=reg.FieldRule("Tags"))), "never revealable"),
        (_broken(document_types=_with_npc_rules(name=reg.FieldRule("Name"))), "cannot redeclare it"),
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


def test_only_the_character_sheet_seeds_an_owner_default() -> None:
    """Records ED-14 and A-19 (owner decision O-2), which amend AUD-9: the flag
    says whose default reveal a type **seeds**, and nothing about who may receive
    one — any type may be revealed to a participant, and every type but the sheet
    seeds that participant empty."""
    owners = [doc.id.value for doc in reg.REGISTRY.document_types if reg.REGISTRY.seeds_owner_default(doc)]
    assert owners == ["character-sheet"]
    assert reg.REGISTRY.audience_of(_doc("character-sheet")) == "owner"
    assert not reg.REGISTRY.seeds_owner_default(_doc("npc"))


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
    the allowlist and cannot be widened by any action.

    Unguarded on purpose: the key must **exist** for this to prove anything, and
    a version of this test that skipped when it did not would pass with the
    field deleted.
    """
    npc = _doc("npc")
    assert "true_identity" in npc.fields, "ED-20 needs a home for the link, and this is it"
    assert not reg.REGISTRY.is_revealable(npc, "true_identity")
    assert "true_identity" not in reg.REGISTRY.revealable_keys(npc)


def test_every_key_of_a_field_rule_is_read_by_a_selector() -> None:
    """The same rule as ``TYPE_FLAG_SELECTORS``, one level down. Without this,
    ``editable`` was declared on every field and read by nothing."""
    keys = {key for doc in REGISTRY_JSON["document_types"] for rule in doc["field_rules"].values() for key in rule}
    keys |= {key for rule in REGISTRY_JSON["common_field_rules"].values() for key in rule}
    assert keys == set(reg.FIELD_FLAG_SELECTORS)
    for flag, selectors in reg.FIELD_FLAG_SELECTORS.items():
        for name in selectors:
            assert callable(getattr(reg.REGISTRY, name)), f"{flag}: {name} is not a selector"


def test_each_field_selector_answers_the_flag_it_names() -> None:
    npc = _doc("npc")
    statblock = _doc("statblock")
    assert reg.REGISTRY.label_for(npc, "if_attacked") == "If the party attacks"
    assert reg.REGISTRY.is_editable(npc, "wants")
    assert reg.REGISTRY.is_revealable(npc, "wants")
    assert not reg.REGISTRY.is_revealable(npc, "tags")
    assert reg.REGISTRY.warning_for(npc, "wants") == "Would spoil the lie"
    # LIB-12, as the flag rather than as an invention of the New dialog.
    assert reg.REGISTRY.is_required(statblock, "ac")
    assert reg.REGISTRY.is_required(statblock, "hp")
    assert reg.REGISTRY.is_required(statblock, "name"), "the common rule answers through the type"
    assert not reg.REGISTRY.is_required(npc, "voice")
    # Per-use integer bounds, and the one integer field deliberately without them.
    assert reg.REGISTRY.bounds_for(statblock, "ac") == (0, 1_000_000)
    assert reg.REGISTRY.bounds_for(_doc("session-notes"), "session") == (1, 1_000_000)
    assert reg.REGISTRY.bounds_for(statblock, "xp") is None
    assert reg.REGISTRY.bounds_for(npc, "voice") is None
    # X-8 again: an undeclared key is not editable, not revealable, not required,
    # has no bounds and has no label.
    assert not reg.REGISTRY.is_editable(npc, "nonesuch")
    assert not reg.REGISTRY.is_revealable(npc, "nonesuch")
    assert not reg.REGISTRY.is_required(npc, "nonesuch")
    assert reg.REGISTRY.bounds_for(npc, "nonesuch") is None
    assert reg.REGISTRY.label_for(npc, "nonesuch") is None


def test_only_three_rules_in_the_whole_registry_are_required() -> None:
    """Decision LIB-12 names a stat block's name, AC and HP and nothing else, and
    ruling 5.7#2 keeps the character sheet's own ``ac`` and ``hp`` optional. Pinned
    as a list rather than derived, so widening it is a deliberate act — a field
    that becomes required is a ``type_version`` bump for every stored document."""
    required = {
        doc.id.value: sorted(key for key in (*REGISTRY_JSON["common_fields"], *doc.fields)
                             if reg.REGISTRY.is_required(doc, key))
        for doc in reg.REGISTRY.document_types
    }
    assert required == {
        "npc": ["name"],
        "statblock": ["ac", "hp", "name"],
        "handout": ["name"],
        "session-notes": ["name"],
        "quest-log": ["name"],
        "character-sheet": ["name"],
        "lore": ["name"],
        "encounter": ["name"],
    }


def test_only_an_integer_field_may_carry_bounds_and_only_inside_its_kinds_range() -> None:
    """Three rules, each with its own negative case, because ``bounds`` is the
    first field-rule key whose value a validator has to make sense of rather than
    merely read."""
    statblock = _doc("statblock")
    rules = dict(statblock.field_rules)

    def broken(**changed: reg.FieldRule) -> reg.Registry:
        return _broken(
            document_types=tuple(
                replace(doc, field_rules={**rules, **changed}) if doc.id.value == "statblock" else doc
                for doc in reg.REGISTRY.document_types
            )
        )

    with pytest.raises(reg.RegistryError, match="is not an integer field"):
        reg.validate(broken(speed=reg.FieldRule("Speed", bounds=(0, 10))))
    with pytest.raises(reg.RegistryError, match="lowest is above its highest"):
        reg.validate(broken(ac=reg.FieldRule("Armor Class", bounds=(10, 0))))
    with pytest.raises(reg.RegistryError, match="outside the integer kind's own range"):
        reg.validate(broken(ac=reg.FieldRule("Armor Class", bounds=(0, 1_000_001))))
    # …and the shipped data satisfies all three.
    reg.validate(broken())


def test_a_field_flagged_not_editable_reads_as_not_editable() -> None:
    """Every shipped field is editable today, so the selector is exercised
    against a rule that is not — otherwise it could return ``True`` always."""
    frozen = replace(_doc("npc"), field_rules={**_NPC_RULES, "notes": reg.FieldRule("Notes", editable=False)})
    assert not reg.REGISTRY.is_editable(frozen, "notes")
    assert reg.REGISTRY.is_editable(frozen, "wants")


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


#: Decision ED-24, the reason this list is written out rather than derived:
#: taking a key **off** a type's allowlist is a narrowing that no transaction
#: carries — it ships with a stop-scan that stops every live display holding
#: that key — and this test is what prompts it. Adding a key is a widening and
#: only needs the line changed. Deriving the list from the registry would prove
#: nothing, so it is spelled out.
_REVEALABLE = {
    "npc": ["name", "qualifier", "portrait", "voice", "tell", "attitude", "wants", "leverage", "if_attacked", "notes"],
    "statblock": [
        "name", "qualifier", "ac", "ac_note", "hp", "hit_dice", "speed", "size", "creature_type", "alignment",
        "abilities", "saving_throws", "skills", "damage_immunities", "condition_immunities", "senses", "languages",
        "challenge_rating", "xp", "traits", "actions", "bonus_actions", "reactions", "legendary_actions",
    ],
    "handout": ["name", "qualifier", "portrait", "body"],
    "session-notes": ["name", "qualifier", "session", "date", "present", "recap", "beats", "loose_threads"],
    "quest-log": ["name", "qualifier", "open_threads", "cold_threads", "resolved_threads"],
    "character-sheet": [
        "name", "qualifier", "portrait", "ac", "hp", "speed", "abilities", "features", "equipment", "notes",
    ],
    "lore": ["name", "qualifier", "region", "era", "status", "summary", "history", "rumours"],
    "encounter": [
        "name", "qualifier", "difficulty", "xp_budget", "party_level", "setup", "combatants", "terrain", "outcome",
    ],
}


def test_every_types_revealable_allowlist_is_pinned() -> None:
    assert {doc.id.value: list(reg.REGISTRY.revealable_keys(doc)) for doc in reg.REGISTRY.document_types} == _REVEALABLE


#: What each type seeds, for its own audience alone. Pinned rather than derived
#: because these are the facts most likely to drift back to the handoff's:
#: REVEAL-23 made ``session-notes`` seed **empty** where the handoff seeded
#: ``recap``; AUD-12 makes the character sheet seed its **owner** and never the
#: table; and an encounter that seeded ``outcome`` would spoil the surprise it
#: carries a warning about.
_DEFAULT_REVEAL = {
    "npc": {"table": ["portrait", "name", "voice"]},
    "statblock": {"table": []},
    "handout": {"table": ["portrait", "name", "body"]},
    "session-notes": {"table": []},
    "quest-log": {"table": ["name", "open_threads", "resolved_threads"]},
    "character-sheet": {
        "owner": ["name", "qualifier", "portrait", "ac", "hp", "speed", "abilities", "features", "equipment", "notes"]
    },
    "lore": {"table": ["name", "summary"]},
    "encounter": {"table": []},
}


def test_every_types_default_reveal_is_pinned() -> None:
    assert {
        doc.id.value: {audience: list(keys) for audience, keys in doc.default_reveal.items()}
        for doc in reg.REGISTRY.document_types
    } == _DEFAULT_REVEAL


def test_only_the_owner_audience_type_seeds_anything_but_the_table() -> None:
    """REVEAL-4 and AUD-12, as a property over the whole registry rather than
    one type: a table can never be seeded from an owner-audience type, and an
    owner audience is never seeded by a table type."""
    for doc in reg.REGISTRY.document_types:
        assert set(doc.default_reveal) <= {doc.audience}
        assert reg.REGISTRY.default_reveal_for(doc, "table") == () or doc.audience == "table"


def test_the_wire_contracts_tables_are_the_registry() -> None:
    """``docs/workbench-wire-contract.md`` opens its per-type section by saying
    every row **is** ``registry.json``. Nothing checked that, and a label
    drifted within a day of the tables being written. This is the check.
    """
    doc_text = (FIXTURES.parents[2] / "docs" / "workbench-wire-contract.md").read_text(encoding="utf-8")
    common = REGISTRY_JSON["common_field_rules"]
    for doc in reg.REGISTRY.document_types:
        heading = f"#### `{doc.id.value}` —"
        assert heading in doc_text, f"{doc.id.value} has no table in the wire contract"
        section = doc_text[doc_text.index(heading) :]
        section = section[: section.index("\n#### ")] if "\n#### " in section else section
        rows = {
            row.split("|")[1].strip().strip("`"): [cell.strip() for cell in row.split("|")[2:-1]]
            for row in section.splitlines()
            if row.startswith("| `")
        }
        groups = {key: group.label for group in doc.reveal_groups for key in group.keys}
        seeded = set(doc.default_reveal.get(doc.audience, ()))
        expected = [
            (key, "common", rule["label"], rule["required"], rule["revealable"]) for key, rule in common.items()
        ]
        expected += [
            (key, f"`{doc.fields[key].value}`", rule.label, rule.required, rule.revealable)
            for key, rule in doc.field_rules.items()
        ]
        assert list(rows) == [key for key, *_ in expected], f"{doc.id.value}: the table's keys are not the registry's"
        for key, kind, label, required, revealable in expected:
            cells = rows[key]
            rule = reg.REGISTRY.rule_for(doc, key)
            assert rule is not None
            assert cells[0] == kind, f"{doc.id.value}.{key}: kind"
            warned = f"{label} ⚠ {rule.warning}" if rule.warning else label
            assert cells[1] == warned, f"{doc.id.value}.{key}: label"
            assert cells[2] == ("yes" if rule.editable else "no"), f"{doc.id.value}.{key}: editable"
            assert cells[3] == ("yes" if required else "—"), f"{doc.id.value}.{key}: required"
            assert cells[4] == ("yes" if revealable else "**never**"), f"{doc.id.value}.{key}: revealable"
            assert cells[5] == (f"*{groups[key]}*" if key in groups else "—"), f"{doc.id.value}.{key}: group"
            assert cells[6] == ("yes" if key in seeded else "—"), f"{doc.id.value}.{key}: seeded"


def test_the_integer_bounds_table_is_the_registry() -> None:
    """``bounds`` gets a table of its own rather than a ninth column: it is a pair
    of numbers, not a per-row yes or no, and it is ``null`` on 59 of the 66 rules.
    A separate table still has to be true, so it gets the same cell-by-cell check
    the per-type tables get — a registry fact with no column is otherwise a fact
    with no test.
    """
    doc_text = (FIXTURES.parents[2] / "docs" / "workbench-wire-contract.md").read_text(encoding="utf-8")
    heading = "#### Per-field integer bounds"
    assert heading in doc_text, "the integer-bounds table is missing from the wire contract"
    section = doc_text[doc_text.index(heading) :]
    section = section[: section.index("\n#### ")] if "\n#### " in section else section
    rows = {
        row.split("|")[1].strip().strip("`"): [cell.strip() for cell in row.split("|")[2:-1]]
        for row in section.splitlines()
        if row.startswith("| `")
    }
    expected = {
        f"{doc.id.value}.{key}": rule.bounds
        for doc in reg.REGISTRY.document_types
        for key, rule in doc.field_rules.items()
        if doc.fields[key] is reg.FieldKind.INTEGER
    }
    assert list(rows) == list(expected), "the bounds table lists other fields than the registry's integer ones"
    for name, bounds in expected.items():
        lowest, highest = bounds if bounds is not None else (reg.INTEGER_FIELD_MIN, reg.INTEGER_FIELD_MAX)
        assert rows[name] == [f"{lowest:,}", f"{highest:,}"], name


def test_nothing_off_the_allowlist_is_a_tag_a_source_an_id_or_an_identity_link() -> None:
    """REVEAL-10 and ED-5, stated as what the allowlists must *not* contain.
    Everything here is ``gm_only`` by construction and no action can widen it."""
    never = {"tags", "true_identity", "sources", "asset_id", "document_id", "campaign_id", "author", "changed_fields"}
    for doc in reg.REGISTRY.document_types:
        assert not set(reg.REGISTRY.revealable_keys(doc)) & never, doc.id.value


def test_every_problem_is_reported_at_once() -> None:
    loot = replace(_tool("loot"), aliases=("/NPC",), icon="!")
    broken = _broken(tools=_with_tool(loot), default_pinned=tuple(ToolId)[:6])
    with pytest.raises(reg.RegistryError) as caught:
        reg.validate(broken)
    assert str(caught.value).count(";") >= 2

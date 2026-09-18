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
            "label": doc.label,
            "icon": doc.icon,
            "renderer": doc.renderer,
            "printable": doc.printable,
            "cites_corpus": doc.cites_corpus,
            "field_labels": dict(doc.field_labels),
        }
        for doc in reg.REGISTRY.document_types
    ] == REGISTRY_JSON["document_types"]
    assert [
        {"id": cap.id, "label": cap.label, "disabled_reason": cap.disabled_reason} for cap in reg.REGISTRY.capabilities
    ] == REGISTRY_JSON["capabilities"]
    assert [pin.value for pin in reg.REGISTRY.default_pinned] == REGISTRY_JSON["default_pinned"]
    assert reg.REGISTRY.rail_limit == REGISTRY_JSON["rail_limit"] == reg.RAIL_LIMIT
    assert list(reg.REGISTRY.renderers) == REGISTRY_JSON["renderers"]
    assert dict(reg.REGISTRY.common_field_labels) == REGISTRY_JSON["common_field_labels"]


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


_NPC_LABELS = dict(reg.REGISTRY.document_types[0].field_labels)


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
        (_broken(document_types=_with_first_type(field_labels={**_NPC_LABELS, "xp_budget": "XP"})), "undeclared field"),
        (
            _broken(document_types=_with_first_type(field_labels={**_NPC_LABELS, "notes": "Terrain &amp; hazards"})),
            "HTML entity",
        ),
    ],
)
def test_a_registry_that_breaks_a_rule_cannot_be_built(registry: reg.Registry, problem: str) -> None:
    """The bead's AC, as one validator: duplicates and collisions (SLASH-2), unknown
    kinds, types, capabilities and renderers, unsafe defaults, too many pins (RAIL-11)."""
    with pytest.raises(reg.RegistryError, match=problem):
        reg.validate(registry)


def test_every_problem_is_reported_at_once() -> None:
    loot = replace(_tool("loot"), aliases=("/NPC",), icon="!")
    broken = _broken(tools=_with_tool(loot), default_pinned=tuple(ToolId)[:6])
    with pytest.raises(reg.RegistryError) as caught:
        reg.validate(broken)
    assert str(caught.value).count(";") >= 2

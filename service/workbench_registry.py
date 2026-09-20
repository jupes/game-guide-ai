"""The canonical GM tool and document-type registry (1kg.3.1).

One catalogue, three consumers: server policy (which tool may run, what it
creates, which capability it needs), the client's three tool surfaces (the
rail, the slash menu, More — ``ui/src/gm/registry.ts`` holds the same data), and
the wire contract's validators (``workbench_contracts.py`` pins the subset it
needs). ``contracts/workbench/v1/registry.json`` is the one fixture both test
suites compare their copy against, so the three cannot drift apart.

Every entry is validated when this module is imported (:func:`validate`), so a
registry that could not be built never serves a request: duplicate ids, a
command or alias that collides with another (SLASH-2), an unknown result kind,
document type, card kind, capability or renderer, a default pin that is not a
tool or that needs a capability which is off by default, more pins than the rail
holds (RAIL-11). Lookups return ``None`` for an unknown id: unknown is never
NPC (X-8).

What this registry does **not** carry yet, on purpose: which fields of a type a
table may see, reveal groups and warnings, audiences and per-audience default
masks. Those are `agent-forge-harness-1ir.1.2`'s decision and arrive with the
reveal family (`1kg.1.6`). The seven document types whose fields `1kg.5.3` has
yet to declare carry no field labels until it does.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from service.workbench_contracts import (
    COMMON_FIELDS,
    DOC_TYPE_FIELDS,
    DOC_TYPE_LIBRARY_CATEGORY,
    DOC_TYPE_VERSION,
    TOOL_BRIEF_POLICY,
    TOOL_CARD_KIND,
    TOOL_CREATES_DOC_TYPE,
    TOOL_RESULT_KIND,
    BriefPolicy,
    CardKind,
    DocumentTypeId,
    FieldKind,
    LibraryCategory,
    ResultKind,
    ToolId,
)

#: Decision RAIL-11: the rail holds at most five pins.
RAIL_LIMIT = 5
#: A slash command as the parser matches it (SLASH-2): lower-case, after one ``/``.
_COMMAND = re.compile(r"^/[a-z][a-z0-9-]{0,23}$")
#: A Material Symbols Rounded ligature name, as the contract's ``ToolSuggestion.icon``.
_ICON = re.compile(r"^[a-z0-9_]{1,40}$")


class RegistryError(ValueError):
    """The registry cannot be built. The message lists every problem found."""


@dataclass(frozen=True)
class Capability:
    """A deployment switch a tool may depend on (RAIL-10). ``off_by_default``
    because no provider is approved until the owner says so (record 3.3)."""

    id: str
    label: str
    disabled_reason: str
    off_by_default: bool = True


@dataclass(frozen=True)
class Tool:
    id: ToolId
    command: str
    aliases: tuple[str, ...]
    label: str
    icon: str
    blurb: str
    working_label: str
    result_kind: ResultKind
    brief: BriefPolicy
    creates_doc_type: DocumentTypeId | None
    card_kind: CardKind | None
    capability: str | None

    @property
    def commands(self) -> tuple[str, ...]:
        return (self.command, *self.aliases)


@dataclass(frozen=True)
class DocumentType:
    id: DocumentTypeId
    label: str
    icon: str
    renderer: str
    library_category: LibraryCategory
    type_version: int
    fields: Mapping[str, FieldKind]
    field_labels: Mapping[str, str]
    printable: bool
    cites_corpus: bool


@dataclass(frozen=True)
class ToolAvailability:
    """RAIL-10: a disabled tool stays visible, with its reason."""

    enabled: bool
    reason: str | None


@dataclass(frozen=True)
class Registry:
    tools: tuple[Tool, ...]
    document_types: tuple[DocumentType, ...]
    capabilities: tuple[Capability, ...]
    default_pinned: tuple[ToolId, ...]
    renderers: tuple[str, ...] = ("game_document", "stat_block_card")
    common_field_labels: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({"name": "Name", "qualifier": "Qualifier", "tags": "Tags"})
    )
    rail_limit: int = RAIL_LIMIT

    def tool(self, tool_id: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.id.value == tool_id), None)

    def document_type(self, type_id: str) -> DocumentType | None:
        """``None`` for an unknown type: never a fallback to NPC (X-8)."""
        return next((doc for doc in self.document_types if doc.id.value == type_id), None)

    def capability(self, capability_id: str) -> Capability | None:
        return next((cap for cap in self.capabilities if cap.id == capability_id), None)

    def availability(self, enabled: Mapping[str, bool] | None) -> dict[ToolId, ToolAvailability]:
        """Which tools may run, given the deployment's capability switches.

        ``None`` is the state before the lookup answered, or after it failed
        (AE-58): every tool is disabled with one message and the client offers
        Retry. A tool with no capability is enabled; one whose capability the
        deployment has not switched on carries that capability's reason.
        """
        if enabled is None:
            reason = "Couldn't check which tools are available"
            return {tool.id: ToolAvailability(False, reason) for tool in self.tools}
        out: dict[ToolId, ToolAvailability] = {}
        for tool in self.tools:
            if tool.capability is None or enabled.get(tool.capability, False):
                out[tool.id] = ToolAvailability(True, None)
            else:
                capability = self.capability(tool.capability)
                out[tool.id] = ToolAvailability(False, capability.disabled_reason if capability else "Not available")
        return out


def _tool(
    tool_id: ToolId,
    command: str,
    label: str,
    icon: str,
    blurb: str,
    working_label: str,
    *,
    aliases: tuple[str, ...] = (),
    capability: str | None = None,
) -> Tool:
    return Tool(
        id=tool_id,
        command=command,
        aliases=aliases,
        label=label,
        icon=icon,
        blurb=blurb,
        working_label=working_label,
        result_kind=TOOL_RESULT_KIND[tool_id],
        brief=TOOL_BRIEF_POLICY[tool_id],
        creates_doc_type=TOOL_CREATES_DOC_TYPE.get(tool_id),
        card_kind=TOOL_CARD_KIND.get(tool_id),
        capability=capability,
    )


def _document_type(
    type_id: DocumentTypeId,
    label: str,
    icon: str,
    *,
    renderer: str = "game_document",
    field_labels: Mapping[str, str] | None = None,
    printable: bool = False,
    cites_corpus: bool = False,
) -> DocumentType:
    return DocumentType(
        id=type_id,
        label=label,
        icon=icon,
        renderer=renderer,
        library_category=DOC_TYPE_LIBRARY_CATEGORY[type_id],
        type_version=DOC_TYPE_VERSION[type_id],
        fields=MappingProxyType(dict(DOC_TYPE_FIELDS[type_id])),
        field_labels=MappingProxyType(dict(field_labels or {})),
        printable=printable,
        cites_corpus=cites_corpus,
    )


#: Commands, labels, icons, blurbs and the default pins are the handoff's
#: (``tools/toolRegistry.js``); working labels, the ``/hooks`` alias and the
#: capabilities are the record's (3.3). Registry order is menu order (SLASH-9).
REGISTRY = Registry(
    tools=(
        _tool(ToolId.NPC, "/npc", "NPC", "person_add", "Generate an NPC dossier", "Writing the dossier…"),
        _tool(ToolId.MONSTER, "/monster", "Monster", "shield", "Generate a stat block", "Building the stat block…"),
        _tool(ToolId.LOOT, "/loot", "Loot", "diamond", "Roll treasure and hoards", "Rolling treasure…"),
        _tool(ToolId.NAMES, "/names", "Names", "badge", "Names by culture and role", "Gathering names…"),
        _tool(ToolId.RULES, "/rules", "Rules", "gavel", "Look up a rule, with citations", "Checking the rules…"),
        _tool(
            ToolId.PORTRAIT, "/portrait", "Portrait", "image", "Portrait or scene art", "Painting…",
            capability="image_generation",
        ),
        _tool(
            ToolId.ENCOUNTER, "/encounter", "Encounter", "swords", "Build a balanced encounter",
            "Balancing the encounter…",
        ),
        _tool(ToolId.HOOKS, "/hook", "Hooks", "flag", "Three plot hooks", "Finding hooks…", aliases=("/hooks",)),
        _tool(ToolId.RECAP, "/recap", "Recap", "history_edu", "Recap the session so far", "Recapping the session…"),
        _tool(
            ToolId.MAP, "/map", "Map", "map", "Sketch a battlemap", "Sketching the map…", capability="image_generation"
        ),
    ),
    document_types=(
        _document_type(
            DocumentTypeId.NPC, "NPC Dossier", "person",
            field_labels={
                "portrait": "Portrait", "voice": "Voice", "tell": "Tell", "attitude": "Attitude", "wants": "Wants",
                "leverage": "Leverage", "if_attacked": "If the party attacks", "notes": "Notes",
            },
        ),
        _document_type(
            DocumentTypeId.STATBLOCK, "Stat Block", "shield", renderer="stat_block_card",
            field_labels={"ac": "Armor Class", "abilities": "Ability scores", "traits": "Traits"},
        ),
        _document_type(DocumentTypeId.HANDOUT, "Player Handout", "mail", printable=True),
        _document_type(DocumentTypeId.SESSION_NOTES, "Session Notes", "history_edu"),
        _document_type(DocumentTypeId.QUEST_LOG, "Quest Log", "flag"),
        _document_type(DocumentTypeId.CHARACTER_SHEET, "Character Sheet", "contact_page"),
        _document_type(DocumentTypeId.LORE, "Lore Entry", "local_library", cites_corpus=True),
        _document_type(DocumentTypeId.ENCOUNTER, "Encounter", "swords"),
    ),
    capabilities=(
        Capability("image_generation", "Image generation", "Image generation isn't set up yet."),
        Capability("audio_cues", "Audio cues", "Audio cues aren't set up yet."),
    ),
    default_pinned=(ToolId.NPC, ToolId.MONSTER, ToolId.LOOT, ToolId.NAMES, ToolId.RULES),
)


def validate(registry: Registry) -> None:
    """Every rule a registry must satisfy, reported together. Raises :class:`RegistryError`."""
    problems: list[str] = []
    tool_ids = [tool.id for tool in registry.tools]
    if len(set(tool_ids)) != len(tool_ids):
        problems.append("duplicate tool ids")
    if set(tool_ids) != set(ToolId):
        problems.append("the registry must list every tool of the contract exactly once")
    seen_commands: dict[str, ToolId] = {}
    capability_ids = {cap.id for cap in registry.capabilities}
    for tool in registry.tools:
        for command in tool.commands:
            key = command.lower()
            if not _COMMAND.fullmatch(key):
                problems.append(f"{tool.id.value}: {command!r} is not a well-formed command")
            if key in seen_commands:
                problems.append(f"{tool.id.value}: {command!r} collides with {seen_commands[key].value}")
            seen_commands[key] = tool.id
        if not _ICON.fullmatch(tool.icon):
            problems.append(f"{tool.id.value}: icon {tool.icon!r} is not a ligature name")
        for text_name, text in (("label", tool.label), ("blurb", tool.blurb), ("working_label", tool.working_label)):
            if not 1 <= len(text) <= 60 or "&" in text and ";" in text:
                problems.append(f"{tool.id.value}: {text_name} is empty, too long or carries an HTML entity")
        if (tool.creates_doc_type is not None) != (tool.result_kind is ResultKind.DOCUMENT):
            problems.append(f"{tool.id.value}: only a document result names the type it creates")
        if tool.card_kind is not None and tool.result_kind is not ResultKind.CARD:
            problems.append(f"{tool.id.value}: only a card result names a card kind")
        if tool.capability is not None and tool.capability not in capability_ids:
            problems.append(f"{tool.id.value}: unknown capability {tool.capability!r}")
    type_ids = [doc.id for doc in registry.document_types]
    if len(set(type_ids)) != len(type_ids) or set(type_ids) != set(DocumentTypeId):
        problems.append("the registry must list every document type of the contract exactly once")
    for doc in registry.document_types:
        if doc.renderer not in registry.renderers:
            problems.append(f"{doc.id.value}: unknown renderer {doc.renderer!r}")
        if not _ICON.fullmatch(doc.icon):
            problems.append(f"{doc.id.value}: icon {doc.icon!r} is not a ligature name")
        declared = set(COMMON_FIELDS) | set(doc.fields)
        for key, label in doc.field_labels.items():
            if key not in declared:
                problems.append(f"{doc.id.value}: a label for an undeclared field {key!r}")
            if "&" in label and ";" in label:
                problems.append(f"{doc.id.value}: the label for {key!r} carries an HTML entity")
        if set(doc.field_labels) != set(doc.fields):
            problems.append(f"{doc.id.value}: every declared field has a label, and nothing else does")
    for tool in registry.tools:
        if tool.creates_doc_type is not None and registry.document_type(tool.creates_doc_type.value) is None:
            problems.append(f"{tool.id.value}: creates an unknown document type")
    pins = registry.default_pinned
    if len(pins) > registry.rail_limit or len(set(pins)) != len(pins):
        problems.append(f"default pins: at most {registry.rail_limit}, each once")
    for pin in pins:
        pinned = registry.tool(pin.value)
        if pinned is None:
            problems.append(f"default pin {pin.value!r} is not a tool")
        elif pinned.capability is not None and (
            registry.capability(pinned.capability) or Capability("", "", "")
        ).off_by_default:
            problems.append(f"default pin {pin.value!r} needs a capability that is off by default")
    if problems:
        raise RegistryError("; ".join(problems))


validate(REGISTRY)

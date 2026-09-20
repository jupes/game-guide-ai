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

Since `1kg.5.3` it also carries, per field, the **rule** that says how the field
is presented and who may ever see it — its label, whether it is editable, whether
it is on the type's **revealable allowlist** (REVEAL-10, and by ED-5 the same
list that can ever be classified), and the warning a reveal sheet shows above it
(REVEAL-11) — and, per type, the audience whose picker it offers (AUD-9), its
accent, its reveal groups, its per-audience default reveal (REVEAL-4) and the
keys it has retired (ED-24). What is **not** here: a reveal mask, a projection
or an eligibility row. Those are state, not registry, and belong to `1kg.1.6`
and `1ir.2.1`.
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


#: The audience a *type* has (AUD-9). Only an ``owner`` type offers the sheet's
#: audience picker; every other type is table-only in v1.
AUDIENCES: tuple[str, ...] = ("table", "owner")
#: A closed token the client maps to a custom property. The handoff stored raw
#: CSS; a stylesheet is the client's business, not the contract's.
ACCENTS: tuple[str, ...] = ("arcane",)


@dataclass(frozen=True)
class FieldRule:
    """How one field is presented, and whether it may ever be shown.

    ``revealable`` **is** the type's allowlist (REVEAL-10). By ED-5 it is also
    the list of keys that can ever be classified: everything off it — ``tags``,
    sources and citation text, ids, authorship, asset metadata, and any identity
    link (ED-20) — is ``gm_only`` by construction and cannot be widened by any
    action. ``warning`` is the sub-line a reveal sheet shows above the toggle,
    in the type's own words (REVEAL-11); a field that cannot be revealed cannot
    carry one.
    """

    label: str
    editable: bool = True
    revealable: bool = True
    warning: str | None = None


@dataclass(frozen=True)
class RevealGroup:
    """Decision REVEAL-11: one labelled row that toggles a fixed set of keys.
    The stored mask still lists the individual keys."""

    id: str
    label: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class DocumentType:
    id: DocumentTypeId
    label: str
    icon: str
    renderer: str
    library_category: LibraryCategory
    type_version: int
    fields: Mapping[str, FieldKind]
    #: One rule per **own** field; the common fields' rules are the registry's
    #: (:attr:`Registry.common_field_rules`), because they never vary by type.
    field_rules: Mapping[str, FieldRule]
    printable: bool
    cites_corpus: bool
    audience: str
    accent: str | None
    reveal_groups: tuple[RevealGroup, ...]
    #: Per audience, and only ever the type's own one (REVEAL-4). Read it through
    #: :meth:`Registry.default_reveal_for`, which answers ``()`` for any other.
    default_reveal: Mapping[str, tuple[str, ...]]
    #: Decision ED-24: keys this type has retired, which may never come back
    #: with another meaning. A test pins the list, so adding to it is deliberate.
    reserved_keys: tuple[str, ...]

    @property
    def field_labels(self) -> Mapping[str, str]:
        """The labels alone, derived — never stored twice, so they cannot drift."""
        return MappingProxyType({key: rule.label for key, rule in self.field_rules.items()})


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
    #: The common fields' rules, shared by every type. ``tags`` is never
    #: revealable, on any type (REVEAL-10, ED-5).
    common_field_rules: Mapping[str, FieldRule] = field(
        default_factory=lambda: MappingProxyType(
            {
                "name": FieldRule("Name"),
                "qualifier": FieldRule("Qualifier"),
                "tags": FieldRule("Tags", revealable=False),
            }
        )
    )
    accents: tuple[str, ...] = ACCENTS
    audiences: tuple[str, ...] = AUDIENCES
    rail_limit: int = RAIL_LIMIT

    @property
    def common_field_labels(self) -> Mapping[str, str]:
        """Derived, like a type's own: one place holds a label."""
        return MappingProxyType({key: rule.label for key, rule in self.common_field_rules.items()})

    def tool(self, tool_id: str) -> Tool | None:
        return next((tool for tool in self.tools if tool.id.value == tool_id), None)

    def document_type(self, type_id: str) -> DocumentType | None:
        """``None`` for an unknown type: never a fallback to NPC (X-8)."""
        return next((doc for doc in self.document_types if doc.id.value == type_id), None)

    def capability(self, capability_id: str) -> Capability | None:
        return next((cap for cap in self.capabilities if cap.id == capability_id), None)

    # ── Selectors over a document type ───────────────────────────────────────
    #
    # Every per-type flag is read through one of these, and
    # ``TYPE_FLAG_SELECTORS`` pins the mapping, so a flag nothing reads cannot
    # be added and a reader cannot be deleted without a suite going red.

    def renderer_for(self, doc: DocumentType) -> str:
        """Which renderer draws this type (`1kg.6.2` calls it)."""
        return doc.renderer

    def is_printable(self, doc: DocumentType) -> bool:
        """Whether the type offers a print or player-safe export affordance."""
        return doc.printable

    def cites_corpus(self, doc: DocumentType) -> bool:
        """Whether the renderer shows a corpus citation footer. Where citations
        are *stored* is `1kg.5.6`'s."""
        return doc.cites_corpus

    def audience_of(self, doc: DocumentType) -> str:
        return doc.audience

    def shows_audience_picker(self, doc: DocumentType) -> bool:
        """Decision AUD-9: only an owner-audience type offers the picker."""
        return doc.audience == "owner"

    def accent_token(self, doc: DocumentType) -> str | None:
        return doc.accent

    def ability_row_key(self, doc: DocumentType) -> str | None:
        """The key holding the ability block, or ``None``.

        **Derived, not declared.** The handoff carried a ``hasAbilityRow`` flag
        that nothing read and that could disagree with the fields; a lookup
        cannot. :func:`validate` allows at most one ``abilities`` field.
        """
        return next((key for key, kind in doc.fields.items() if kind is FieldKind.ABILITIES), None)

    def rule_for(self, doc: DocumentType, key: str) -> FieldRule | None:
        """A field's rule, common or the type's own. ``None`` for a key the type
        does not declare — never a default, because a default would be a
        visibility decision made by accident."""
        return doc.field_rules.get(key) or self.common_field_rules.get(key)

    def revealable_keys(self, doc: DocumentType) -> tuple[str, ...]:
        """The type's allowlist (REVEAL-10), commons first, in registry order."""
        common = tuple(key for key, rule in self.common_field_rules.items() if rule.revealable)
        return common + tuple(key for key, rule in doc.field_rules.items() if rule.revealable)

    def warning_for(self, doc: DocumentType, key: str) -> str | None:
        rule = self.rule_for(doc, key)
        return rule.warning if rule else None

    def reveal_group_for(self, doc: DocumentType, key: str) -> RevealGroup | None:
        """The group a key is toggled by, or ``None`` if it has a row of its own."""
        return next((group for group in doc.reveal_groups if key in group.keys), None)

    def default_reveal_for(self, doc: DocumentType, audience: str) -> tuple[str, ...]:
        """Decision REVEAL-4: a default seeds the type's **own** audience, and is
        empty for every other — so the table of an owner-audience type gets
        ``()``, not ``None`` and not an error (AUD-12)."""
        return tuple(doc.default_reveal.get(audience, ()))

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
    rules: Mapping[str, FieldRule] | None = None,
    printable: bool = False,
    cites_corpus: bool = False,
    audience: str = "table",
    accent: str | None = None,
    reveal_groups: tuple[RevealGroup, ...] = (),
    default_reveal: Mapping[str, tuple[str, ...]] | None = None,
    reserved_keys: tuple[str, ...] = (),
) -> DocumentType:
    return DocumentType(
        id=type_id,
        label=label,
        icon=icon,
        renderer=renderer,
        library_category=DOC_TYPE_LIBRARY_CATEGORY[type_id],
        type_version=DOC_TYPE_VERSION[type_id],
        fields=MappingProxyType(dict(DOC_TYPE_FIELDS[type_id])),
        field_rules=MappingProxyType(dict(rules or {})),
        printable=printable,
        cites_corpus=cites_corpus,
        audience=audience,
        accent=accent,
        reveal_groups=reveal_groups,
        default_reveal=MappingProxyType(dict(default_reveal or {})),
        reserved_keys=reserved_keys,
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
            rules={
                "portrait": FieldRule("Portrait"),
                "voice": FieldRule("Voice"),
                "tell": FieldRule("Tell"),
                "attitude": FieldRule("Attitude"),
                "wants": FieldRule("Wants", warning="Would spoil the lie"),
                "leverage": FieldRule("Leverage", warning="Would spoil the lie"),
                "if_attacked": FieldRule("If the party attacks"),
                "notes": FieldRule("Notes"),
            },
            reveal_groups=(
                RevealGroup("name_and_voice", "Name & voice", ("name", "voice")),
                RevealGroup("wants_and_leverage", "Wants & leverage", ("wants", "leverage")),
            ),
            default_reveal={"table": ("portrait", "name", "voice")},
        ),
        _document_type(
            DocumentTypeId.STATBLOCK, "Stat Block", "shield", renderer="stat_block_card",
            rules={
                "ac": FieldRule("Armor Class"),
                "abilities": FieldRule("Ability scores"),
                "traits": FieldRule("Traits"),
            },
        ),
        _document_type(DocumentTypeId.HANDOUT, "Player Handout", "mail", printable=True),
        _document_type(DocumentTypeId.SESSION_NOTES, "Session Notes", "history_edu"),
        _document_type(DocumentTypeId.QUEST_LOG, "Quest Log", "flag"),
        _document_type(DocumentTypeId.CHARACTER_SHEET, "Character Sheet", "contact_page", audience="owner"),
        _document_type(DocumentTypeId.LORE, "Lore Entry", "local_library", cites_corpus=True, accent="arcane"),
        _document_type(DocumentTypeId.ENCOUNTER, "Encounter", "swords"),
    ),
    capabilities=(
        Capability("image_generation", "Image generation", "Image generation isn't set up yet."),
        Capability("audio_cues", "Audio cues", "Audio cues aren't set up yet."),
    ),
    default_pinned=(ToolId.NPC, ToolId.MONSTER, ToolId.LOOT, ToolId.NAMES, ToolId.RULES),
)


#: A field key, as decision ED-2 / CANVAS-19 defines it. ``FieldKey`` in the
#: contract applies this to a *value* on the wire; here it applies to a
#: **declaration**, which nothing checked before ``1kg.5.3``.
_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
#: A label is for a person: it cannot be empty, cannot be a key that escaped
#: (``xp_budget``, ``ifAttacked``), and cannot smuggle markup.
_CAMEL_CASE = re.compile(r"[a-z][A-Z]")


def _label_problems(label: str, *, what: str = "label", limit: int = 60) -> list[str]:
    """The one rule, used for field labels, group labels and warning copy."""
    problems: list[str] = []
    if not label.strip():
        problems.append(f"the {what} is empty")
    if len(label) > limit:
        problems.append(f"the {what} is longer than {limit} characters")
    if "&" in label and ";" in label:
        problems.append(f"the {what} carries an HTML entity")
    if "_" in label:
        problems.append(f"the {what} {label!r} is snake_case, not words")
    if _CAMEL_CASE.search(label):
        problems.append(f"the {what} {label!r} is camelCase, not words")
    return problems


def _document_type_problems(registry: Registry, doc: DocumentType) -> list[str]:
    """Everything one type must satisfy. Messages are prefixed with its id."""
    problems: list[str] = []
    if doc.renderer not in registry.renderers:
        problems.append(f"unknown renderer {doc.renderer!r}")
    if not _ICON.fullmatch(doc.icon):
        problems.append(f"icon {doc.icon!r} is not a ligature name")
    if doc.audience not in registry.audiences:
        problems.append(f"unknown audience {doc.audience!r}")
    if doc.accent is not None and doc.accent not in registry.accents:
        problems.append(f"unknown accent {doc.accent!r}")

    for key in (*doc.fields, *doc.reserved_keys):
        if not _FIELD_KEY.fullmatch(key):
            problems.append(f"{key!r} is not a flat field key")
    if set(doc.reserved_keys) & set(doc.fields):
        problems.append("a reserved key cannot be declared as a field again (ED-24)")
    if len(set(doc.reserved_keys)) != len(doc.reserved_keys):
        problems.append("a reserved key is listed twice")
    if sum(1 for kind in doc.fields.values() if kind is FieldKind.ABILITIES) > 1:
        problems.append("a type has at most one ability block, because the ability row is derived from it")

    for key, rule in doc.field_rules.items():
        problems += _label_problems(rule.label)
        if rule.warning is not None:
            problems += _label_problems(rule.warning, what="warning", limit=80)
            if not rule.revealable:
                problems.append(f"{key!r} carries a reveal warning but can never be revealed")
    if set(doc.field_rules) != set(doc.fields):
        problems.append("every declared field has a rule, and nothing else does")

    allowlist = set(registry.revealable_keys(doc))
    declared = set(COMMON_FIELDS) | set(doc.fields)
    seen_in_a_group: set[str] = set()
    for group in doc.reveal_groups:
        problems += _label_problems(group.label, what="group label")
        if not _FIELD_KEY.fullmatch(group.id):
            problems.append(f"group id {group.id!r} is not a flat key")
        if len(group.keys) < 2:
            problems.append(f"group {group.id!r} toggles fewer than two keys, so it is not a group")
        for key in group.keys:
            if key not in declared:
                problems.append(f"group {group.id!r} names an undeclared field {key!r}")
            elif key not in allowlist:
                problems.append(f"group {group.id!r} names {key!r}, which can never be revealed")
            if key in seen_in_a_group:
                problems.append(f"{key!r} is in more than one reveal group")
            seen_in_a_group.add(key)
    if len({group.id for group in doc.reveal_groups}) != len(doc.reveal_groups):
        problems.append("two reveal groups share an id")

    # Decision REVEAL-4 / AUD-12, as a rule rather than a convention: a default
    # seeds the type's OWN audience, so the table can never be seeded with an
    # owner's fields by a registry edit.
    for audience, keys in doc.default_reveal.items():
        if audience != doc.audience:
            problems.append(f"a default reveal for {audience!r}, but the type's audience is {doc.audience!r}")
        for key in keys:
            if key not in declared:
                problems.append(f"the default reveal names an undeclared field {key!r}")
            elif key not in allowlist:
                problems.append(f"the default reveal names {key!r}, which can never be revealed")
        if len(set(keys)) != len(keys):
            problems.append(f"the default reveal for {audience!r} names a key twice")
    return problems


#: Decision "real semantics": every per-type flag is read by a selector, and
#: this table is what a test compares against ``registry.json``'s own keys. A
#: flag may have more than one reader; a flag with none cannot be added.
TYPE_FLAG_SELECTORS: dict[str, tuple[str, ...]] = {
    "renderer": ("renderer_for",),
    "printable": ("is_printable",),
    "cites_corpus": ("cites_corpus",),
    "audience": ("audience_of", "shows_audience_picker"),
    "accent": ("accent_token",),
}
#: The keys that *are* the type rather than a flag about it. Pinned by a test,
#: so moving a flag in here is as deliberate as retiring a field key.
TYPE_STRUCTURE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "label",
        "icon",
        "library_category",
        "type_version",
        "fields",
        "field_rules",
        "reveal_groups",
        "default_reveal",
        "reserved_keys",
    }
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
    for key, rule in registry.common_field_rules.items():
        problems += [f"a common field: {problem}" for problem in _label_problems(rule.label)]
        if key not in COMMON_FIELDS:
            problems.append(f"a rule for {key!r}, which is not a common field")
    if set(registry.common_field_rules) != set(COMMON_FIELDS):
        problems.append("every common field has a rule, and nothing else does")
    for doc in registry.document_types:
        problems += [f"{doc.id.value}: {problem}" for problem in _document_type_problems(registry, doc)]
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

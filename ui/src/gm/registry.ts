/**
 * The canonical GM tool and document-type registry (1kg.3.1) — the client half.
 *
 * One catalogue for the three tool surfaces: the rail renders the pinned
 * entries, the slash menu renders every entry filtered by command, More renders
 * the rest. `service/workbench_registry.py` holds the same data for server
 * policy, and `contracts/workbench/v1/registry.json` is the one fixture both
 * test suites compare their copy against, so the three cannot drift.
 *
 * The registry is validated when this module loads (`validateRegistry`), so a
 * bundle with a broken catalogue fails at build and test time, never in front
 * of a GM. Lookups return `undefined` for an unknown id: unknown is never NPC
 * (X-8), which is what the handoff's `documentType(id) || DOCUMENT_TYPES.npc`
 * got wrong.
 *
 * Since 1kg.5.3 it also carries, per field, the RULE that says how the field is
 * presented and who may ever see it — its label, whether it is editable, whether
 * it is on the type's revealable allowlist (REVEAL-10, and by ED-5 the same list
 * that can ever be classified), and the warning a reveal sheet shows above it
 * (REVEAL-11) — and, per type, the audience whose picker it offers (AUD-9), its
 * accent, its reveal groups, its per-audience default reveal (REVEAL-4) and the
 * keys it has retired (ED-24). Not here: a reveal mask, a projection or an
 * eligibility row. Those are state, not registry, and belong to 1kg.1.6 and
 * agent-forge-harness-1ir.2.1.
 */

import {
  BRIEF_POLICY,
  COMMON_FIELDS,
  DOC_TYPE_FIELDS,
  DOC_TYPE_LIBRARY_CATEGORY,
  DOC_TYPE_VERSION,
  DOCUMENT_TYPE_IDS,
  TOOL_CARD_KIND,
  TOOL_CREATES_DOC_TYPE,
  TOOL_IDS,
  TOOL_RESULT_KIND,
} from './contracts'
import type { BriefPolicy, CardKind, DocumentTypeId, FieldKind, LibraryCategory, ResultKind, ToolId } from './contracts'

/** Decision RAIL-11: the rail holds at most five pins. */
export const RAIL_LIMIT = 5

export const RENDERERS = ['game_document', 'stat_block_card'] as const
export type Renderer = (typeof RENDERERS)[number]

export const CAPABILITY_IDS = ['image_generation', 'audio_cues'] as const
export type CapabilityId = (typeof CAPABILITY_IDS)[number]

/** A deployment switch a tool may depend on (RAIL-10). Off until the owner approves a provider (record 3.3). */
export interface Capability {
  id: CapabilityId
  label: string
  disabled_reason: string
  off_by_default: boolean
}

export interface Tool {
  id: ToolId
  command: string
  aliases: readonly string[]
  label: string
  icon: string
  blurb: string
  working_label: string
  result_kind: ResultKind
  brief: BriefPolicy
  creates_doc_type: DocumentTypeId | null
  card_kind: CardKind | null
  capability: CapabilityId | null
}

/** The audience a TYPE has (AUD-9): only an `owner` type offers the sheet's
 * audience picker, and every other type is table-only in v1. */
export const AUDIENCES = ['table', 'owner'] as const
export type Audience = (typeof AUDIENCES)[number]

/** A closed token the client maps to a custom property. The handoff stored raw
 * CSS; a stylesheet is the client's business, not the contract's. */
export const ACCENTS = ['arcane'] as const
export type Accent = (typeof ACCENTS)[number]

/**
 * How one field is presented, and whether it may ever be shown.
 *
 * `revealable` IS the type's allowlist (REVEAL-10), and by ED-5 it is also the
 * list of keys that can ever be classified: everything off it — `tags`, sources
 * and citation text, ids, authorship, asset metadata, and any identity link
 * (ED-20) — is `gm_only` by construction. `warning` is the sub-line a reveal
 * sheet shows above the toggle, in the type's own words (REVEAL-11).
 */
export interface FieldRule {
  label: string
  editable: boolean
  revealable: boolean
  warning: string | null
}

/** REVEAL-11: one labelled row that toggles a fixed set of keys. The stored
 * mask still lists the individual keys. */
export interface RevealGroup {
  id: string
  label: string
  keys: readonly string[]
}

export interface DocumentType {
  id: DocumentTypeId
  label: string
  icon: string
  renderer: Renderer
  library_category: LibraryCategory
  type_version: number
  fields: Readonly<Record<string, FieldKind>>
  /** One rule per OWN field; the common fields' rules are the registry's. */
  field_rules: Readonly<Record<string, FieldRule>>
  /** Derived from `field_rules`, so a label is never stored twice. */
  field_labels: Readonly<Record<string, string>>
  printable: boolean
  cites_corpus: boolean
  audience: Audience
  accent: Accent | null
  reveal_groups: readonly RevealGroup[]
  /** Per audience, and only ever the type's own one (REVEAL-4). Read it through
   * `defaultRevealFor`, which answers `[]` for any other. */
  default_reveal: Readonly<Record<string, readonly string[]>>
  /** ED-24: keys this type has retired, which may never return with another
   * meaning. A test pins the list, so adding to it is deliberate. */
  reserved_keys: readonly string[]
}

/** RAIL-10: a disabled tool stays visible, with its reason. */
export interface ToolAvailability {
  enabled: boolean
  reason: string | null
}

export interface Registry {
  tools: readonly Tool[]
  document_types: readonly DocumentType[]
  capabilities: readonly Capability[]
  default_pinned: readonly ToolId[]
  renderers: readonly Renderer[]
  /** Shared by every type. `tags` is never revealable, on any type (REVEAL-10, ED-5). */
  common_field_rules: Readonly<Record<string, FieldRule>>
  /** Derived from `common_field_rules`, like a type's own. */
  common_field_labels: Readonly<Record<string, string>>
  accents: readonly Accent[]
  audiences: readonly Audience[]
  rail_limit: number
}

function tool(
  id: ToolId,
  command: string,
  label: string,
  icon: string,
  blurb: string,
  working_label: string,
  extra: { aliases?: readonly string[]; capability?: CapabilityId } = {},
): Tool {
  return {
    id,
    command,
    aliases: extra.aliases ?? [],
    label,
    icon,
    blurb,
    working_label,
    result_kind: TOOL_RESULT_KIND[id],
    brief: BRIEF_POLICY[id],
    creates_doc_type: TOOL_CREATES_DOC_TYPE[id] ?? null,
    card_kind: TOOL_CARD_KIND[id] ?? null,
    capability: extra.capability ?? null,
  }
}

/** The common fields' rules, shared by every type (REVEAL-10, ED-5). */
const COMMON_FIELD_RULES: Readonly<Record<string, FieldRule>> = {
  name: { label: 'Name', editable: true, revealable: true, warning: null },
  qualifier: { label: 'Qualifier', editable: true, revealable: true, warning: null },
  tags: { label: 'Tags', editable: true, revealable: false, warning: null },
}

/** A rule, with the defaults spelled once: editable, revealable, no warning. */
function rule(label: string, extra: { editable?: boolean; revealable?: boolean; warning?: string } = {}): FieldRule {
  return {
    label,
    editable: extra.editable ?? true,
    revealable: extra.revealable ?? true,
    warning: extra.warning ?? null,
  }
}

/** The labels alone, derived — never stored twice, so they cannot drift. */
function labelsOf(rules: Readonly<Record<string, FieldRule>>): Readonly<Record<string, string>> {
  return Object.fromEntries(Object.entries(rules).map(([key, value]) => [key, value.label]))
}

function documentType(
  id: DocumentTypeId,
  label: string,
  icon: string,
  extra: {
    renderer?: Renderer
    rules?: Readonly<Record<string, FieldRule>>
    printable?: boolean
    cites_corpus?: boolean
    audience?: Audience
    accent?: Accent
    reveal_groups?: readonly RevealGroup[]
    default_reveal?: Readonly<Record<string, readonly string[]>>
    reserved_keys?: readonly string[]
  } = {},
): DocumentType {
  const rules = extra.rules ?? {}
  return {
    id,
    label,
    icon,
    renderer: extra.renderer ?? 'game_document',
    library_category: DOC_TYPE_LIBRARY_CATEGORY[id],
    type_version: DOC_TYPE_VERSION[id],
    fields: DOC_TYPE_FIELDS[id],
    field_rules: rules,
    field_labels: labelsOf(rules),
    printable: extra.printable ?? false,
    cites_corpus: extra.cites_corpus ?? false,
    audience: extra.audience ?? 'table',
    accent: extra.accent ?? null,
    reveal_groups: extra.reveal_groups ?? [],
    default_reveal: extra.default_reveal ?? {},
    reserved_keys: extra.reserved_keys ?? [],
  }
}

/** Commands, labels, icons, blurbs and the default pins are the handoff's
 * (tools/toolRegistry.js); working labels, the /hooks alias and the capabilities
 * are the record's (3.3). Registry order is menu order (SLASH-9). */
export const REGISTRY: Registry = {
  tools: [
    tool('npc', '/npc', 'NPC', 'person_add', 'Generate an NPC dossier', 'Writing the dossier…'),
    tool('monster', '/monster', 'Monster', 'shield', 'Generate a stat block', 'Building the stat block…'),
    tool('loot', '/loot', 'Loot', 'diamond', 'Roll treasure and hoards', 'Rolling treasure…'),
    tool('names', '/names', 'Names', 'badge', 'Names by culture and role', 'Gathering names…'),
    tool('rules', '/rules', 'Rules', 'gavel', 'Look up a rule, with citations', 'Checking the rules…'),
    tool('portrait', '/portrait', 'Portrait', 'image', 'Portrait or scene art', 'Painting…', { capability: 'image_generation' }),
    tool('encounter', '/encounter', 'Encounter', 'swords', 'Build a balanced encounter', 'Balancing the encounter…'),
    tool('hooks', '/hook', 'Hooks', 'flag', 'Three plot hooks', 'Finding hooks…', { aliases: ['/hooks'] }),
    tool('recap', '/recap', 'Recap', 'history_edu', 'Recap the session so far', 'Recapping the session…'),
    tool('map', '/map', 'Map', 'map', 'Sketch a battlemap', 'Sketching the map…', { capability: 'image_generation' }),
  ],
  document_types: [
    documentType('npc', 'NPC Dossier', 'person', {
      rules: {
        portrait: rule('Portrait'),
        voice: rule('Voice'),
        tell: rule('Tell'),
        attitude: rule('Attitude'),
        wants: rule('Wants', { warning: 'Would spoil the lie' }),
        leverage: rule('Leverage', { warning: 'Would spoil the lie' }),
        if_attacked: rule('If the party attacks'),
        notes: rule('Notes'),
        true_identity: rule('True identity', { revealable: false }),
      },
      reveal_groups: [
        { id: 'name_and_voice', label: 'Name & voice', keys: ['name', 'voice'] },
        { id: 'wants_and_leverage', label: 'Wants & leverage', keys: ['wants', 'leverage'] },
      ],
      default_reveal: { table: ['portrait', 'name', 'voice'] },
    }),
    documentType('statblock', 'Stat Block', 'shield', {
      renderer: 'stat_block_card',
      rules: {
        ac: rule('Armor Class'),
        ac_note: rule('Armor Class note'),
        hp: rule('Hit Points'),
        hit_dice: rule('Hit dice'),
        speed: rule('Speed'),
        size: rule('Size'),
        creature_type: rule('Creature type'),
        alignment: rule('Alignment'),
        abilities: rule('Ability scores'),
        saving_throws: rule('Saving throws'),
        skills: rule('Skills'),
        damage_immunities: rule('Damage immunities'),
        condition_immunities: rule('Condition immunities'),
        senses: rule('Senses'),
        languages: rule('Languages'),
        challenge_rating: rule('Challenge rating'),
        xp: rule('XP'),
        traits: rule('Traits'),
        actions: rule('Actions'),
        bonus_actions: rule('Bonus actions'),
        reactions: rule('Reactions'),
        legendary_actions: rule('Legendary actions'),
      },
      default_reveal: { table: [] },
    }),
    documentType('handout', 'Player Handout', 'mail', {
      printable: true,
      rules: {
        portrait: rule('Illustration'),
        body: rule('Text'),
      },
      default_reveal: { table: ['portrait', 'name', 'body'] },
    }),
    documentType('session-notes', 'Session Notes', 'history_edu', {
      rules: {
        session: rule('Session number'),
        date: rule('Date'),
        present: rule('Present'),
        recap: rule('Recap', { warning: 'Summarises your private GM thread' }),
        beats: rule('Beats'),
        loose_threads: rule('Loose threads'),
      },
      default_reveal: { table: [] },
    }),
    documentType('quest-log', 'Quest Log', 'flag', {
      rules: {
        open_threads: rule('Open threads'),
        cold_threads: rule('Cold threads'),
        resolved_threads: rule('Resolved threads'),
      },
      default_reveal: { table: ['name', 'open_threads', 'resolved_threads'] },
    }),
    documentType('character-sheet', 'Character Sheet', 'contact_page', {
      audience: 'owner',
      rules: {
        portrait: rule('Portrait'),
        ac: rule('Armor Class'),
        hp: rule('Hit Points'),
        speed: rule('Speed'),
        abilities: rule('Ability scores'),
        features: rule('Features'),
        equipment: rule('Equipment'),
        notes: rule('Notes'),
      },
      default_reveal: { owner: ['name', 'qualifier', 'portrait', 'ac', 'hp', 'speed', 'abilities', 'features', 'equipment', 'notes'] },
    }),
    documentType('lore', 'Lore Entry', 'local_library', {
      cites_corpus: true,
      accent: 'arcane',
      rules: {
        region: rule('Region'),
        era: rule('Era'),
        status: rule('Status'),
        summary: rule('Summary'),
        history: rule('History'),
        rumours: rule('Rumours'),
      },
      default_reveal: { table: ['name', 'summary'] },
    }),
    documentType('encounter', 'Encounter', 'swords', {
      rules: {
        difficulty: rule('Difficulty'),
        xp_budget: rule('XP budget'),
        party_level: rule('Party level'),
        setup: rule('Setup'),
        combatants: rule('Combatants'),
        terrain: rule('Terrain & hazards'),
        outcome: rule('If it goes wrong', { warning: 'Would spoil the surprise' }),
      },
      default_reveal: { table: [] },
    }),
  ],
  capabilities: [
    { id: 'image_generation', label: 'Image generation', disabled_reason: "Image generation isn't set up yet.", off_by_default: true },
    { id: 'audio_cues', label: 'Audio cues', disabled_reason: "Audio cues aren't set up yet.", off_by_default: true },
  ],
  default_pinned: ['npc', 'monster', 'loot', 'names', 'rules'],
  renderers: RENDERERS,
  common_field_rules: COMMON_FIELD_RULES,
  common_field_labels: labelsOf(COMMON_FIELD_RULES),
  accents: ACCENTS,
  audiences: AUDIENCES,
  rail_limit: RAIL_LIMIT,
}

/** A slash command as the parser matches it (SLASH-2): lower-case, after one `/`. */
const COMMAND = /^\/[a-z][a-z0-9-]{0,23}$/
/** A Material Symbols Rounded ligature name. */
const ICON = /^[a-z0-9_]{1,40}$/
const hasEntity = (text: string) => text.includes('&') && text.includes(';')

/** A field key, as ED-2 / CANVAS-19 defines it — here applied to a DECLARATION,
 * which nothing checked before 1kg.5.3. */
const FIELD_KEY = /^[a-z][a-z0-9_]{0,39}$/
/** A label is for a person: never a key that escaped (`xp_budget`, `ifAttacked`). */
const CAMEL_CASE = /[a-z][A-Z]/
/** REVEAL-10: keys that are never revealable, whatever a registry says. Only
 * `tags` is a DECLARED field today; the rest of REVEAL-10's list — sources,
 * version history, authorship, changed-field lists, asset metadata, ids — never
 * becomes one, and a type that needs an identity link gives it its own key off
 * the allowlist (ED-20). */
export const NEVER_REVEALABLE: readonly string[] = ['tags']

function labelProblems(label: string, what = 'label', limit = 60): string[] {
  const problems: string[] = []
  if (label.trim() === '') problems.push(`the ${what} is empty`)
  if (label.length > limit) problems.push(`the ${what} is longer than ${limit} characters`)
  if (hasEntity(label)) problems.push(`the ${what} carries an HTML entity`)
  if (label.includes('_')) problems.push(`the ${what} ${JSON.stringify(label)} is snake_case, not words`)
  if (CAMEL_CASE.test(label)) problems.push(`the ${what} ${JSON.stringify(label)} is camelCase, not words`)
  return problems
}

/** Everything one type must satisfy. The caller prefixes its id. */
function documentTypeProblems(registry: Registry, d: DocumentType): string[] {
  const problems: string[] = []
  if (!registry.renderers.includes(d.renderer)) problems.push(`unknown renderer ${d.renderer}`)
  if (!ICON.test(d.icon)) problems.push(`icon ${JSON.stringify(d.icon)} is not a ligature name`)
  if (!registry.audiences.includes(d.audience)) problems.push(`unknown audience ${JSON.stringify(d.audience)}`)
  if (d.accent !== null && !registry.accents.includes(d.accent)) problems.push(`unknown accent ${JSON.stringify(d.accent)}`)

  for (const key of [...Object.keys(d.fields), ...d.reserved_keys]) {
    if (!FIELD_KEY.test(key)) problems.push(`${JSON.stringify(key)} is not a flat field key`)
  }
  if (d.reserved_keys.some((key) => Object.hasOwn(d.fields, key))) {
    problems.push('a reserved key cannot be declared as a field again (ED-24)')
  }
  if (new Set(d.reserved_keys).size !== d.reserved_keys.length) problems.push('a reserved key is listed twice')
  if (Object.values(d.fields).filter((kind) => kind === 'abilities').length > 1) {
    problems.push('a type has at most one ability block, because the ability row is derived from it')
  }

  for (const [key, r] of Object.entries(d.field_rules)) {
    problems.push(...labelProblems(r.label))
    if (r.warning !== null) {
      problems.push(...labelProblems(r.warning, 'warning', 80))
      if (!r.revealable) problems.push(`${key} carries a reveal warning but can never be revealed`)
    }
    // The same rule as for a common field, and it has to be here too: a type
    // that declared its own `tags` rule would otherwise put it on the
    // allowlist, and a group or a default reveal could then name it.
    if (NEVER_REVEALABLE.includes(key) && r.revealable) {
      problems.push(`${key} is never revealable, on any type (REVEAL-10, ED-5)`)
    }
    // A type's own key may not shadow a common one: two rules for one key
    // would make "which rule applies" a lookup-order accident.
    if (Object.hasOwn(COMMON_FIELDS, key)) {
      problems.push(`${key} is a common field, and a type cannot redeclare it`)
    }
  }
  if (Object.keys(d.field_rules).sort().join(',') !== Object.keys(d.fields).sort().join(',')) {
    problems.push('every declared field has a rule, and nothing else does')
  }

  const allowlist = new Set(revealableKeys(d, registry))
  const declared = new Set([...Object.keys(COMMON_FIELDS), ...Object.keys(d.fields)])
  const grouped = new Set<string>()
  for (const group of d.reveal_groups) {
    problems.push(...labelProblems(group.label, 'group label'))
    if (!FIELD_KEY.test(group.id)) problems.push(`group id ${JSON.stringify(group.id)} is not a flat key`)
    if (group.keys.length < 2) problems.push(`group ${group.id} toggles fewer than two keys, so it is not a group`)
    for (const key of group.keys) {
      if (!declared.has(key)) problems.push(`group ${group.id} names an undeclared field ${key}`)
      else if (!allowlist.has(key)) problems.push(`group ${group.id} names ${key}, which can never be revealed`)
      if (grouped.has(key)) problems.push(`${key} is in more than one reveal group`)
      grouped.add(key)
    }
  }
  if (new Set(d.reveal_groups.map((g) => g.id)).size !== d.reveal_groups.length) {
    problems.push('two reveal groups share an id')
  }

  // REVEAL-4 and AUD-12 as a rule rather than a convention: a default seeds the
  // type's OWN audience, so a registry edit can never seed the table with an
  // owner type's fields.
  for (const [audience, keys] of Object.entries(d.default_reveal)) {
    if (audience !== d.audience) {
      problems.push(`a default reveal for ${JSON.stringify(audience)}, but the type's audience is ${JSON.stringify(d.audience)}`)
    }
    for (const key of keys) {
      if (!declared.has(key)) problems.push(`the default reveal names an undeclared field ${key}`)
      else if (!allowlist.has(key)) problems.push(`the default reveal names ${key}, which can never be revealed`)
    }
    if (new Set(keys).size !== keys.length) problems.push(`the default reveal for ${audience} names a key twice`)
  }
  return problems
}

/** "Real semantics": every per-type flag is read by a selector, and this table
 * is what a test compares against registry.json's own keys. A flag may have
 * more than one reader; a flag with none cannot be added. */
export const TYPE_FLAG_SELECTORS: Readonly<Record<string, readonly string[]>> = {
  renderer: ['rendererFor'],
  printable: ['isPrintable'],
  cites_corpus: ['citesCorpus'],
  audience: ['audienceOf', 'showsAudiencePicker'],
  accent: ['accentToken'],
}

/** The keys that ARE the type rather than a flag about it. Pinned by a test, so
 * moving a flag in here is as deliberate as retiring a field key. */
/** The same rule one level down: every key of a FIELD RULE is read by a
 * selector too. Without this table `editable` was declared on every field and
 * read by nothing — exactly the defect TYPE_FLAG_SELECTORS exists to catch, one
 * level below where it was looking. */
export const FIELD_FLAG_SELECTORS: Readonly<Record<string, readonly string[]>> = {
  label: ['labelFor'],
  editable: ['isEditable'],
  revealable: ['isRevealable', 'revealableKeys'],
  warning: ['warningFor'],
}

export const TYPE_STRUCTURE_KEYS: readonly string[] = [
  'id',
  'label',
  'icon',
  'library_category',
  'type_version',
  'fields',
  'field_rules',
  'reveal_groups',
  'default_reveal',
  'reserved_keys',
]

// ── Selectors over a document type ───────────────────────────────────────────

export function rendererFor(d: DocumentType): Renderer {
  return d.renderer
}

export function isPrintable(d: DocumentType): boolean {
  return d.printable
}

/** Whether the renderer shows a corpus citation footer. Where citations are
 * STORED is 1kg.5.6's. */
export function citesCorpus(d: DocumentType): boolean {
  return d.cites_corpus
}

export function audienceOf(d: DocumentType): Audience {
  return d.audience
}

/** AUD-9: only an owner-audience type offers the picker. */
export function showsAudiencePicker(d: DocumentType): boolean {
  return d.audience === 'owner'
}

export function accentToken(d: DocumentType): Accent | null {
  return d.accent
}

/**
 * The key holding the ability block, or `undefined`.
 *
 * Derived, not declared: the handoff carried a `hasAbilityRow` flag that
 * nothing read and that could disagree with the fields. A lookup cannot, and
 * `validateRegistry` allows at most one `abilities` field.
 */
export function abilityRowKey(d: DocumentType): string | undefined {
  return Object.entries(d.fields).find(([, kind]) => kind === 'abilities')?.[0]
}

/**
 * A field's rule, common or the type's own. `undefined` for a key the type does
 * not declare — never a default, because a default visibility is a decision
 * made by accident. `Object.hasOwn`, so `constructor` reads as "not declared".
 *
 * `registry` defaults to the shipped one, but `validateRegistry` passes the
 * candidate it is checking: reading the module constant instead would let a
 * registry under validation be judged against the shipped common rules, and
 * the two languages' validators would then disagree.
 */
export function ruleFor(d: DocumentType, key: string, registry: Registry = REGISTRY): FieldRule | undefined {
  if (Object.hasOwn(d.field_rules, key)) return d.field_rules[key]
  if (Object.hasOwn(registry.common_field_rules, key)) return registry.common_field_rules[key]
  return undefined
}

/** The type's allowlist (REVEAL-10), commons first, in registry order. */
export function revealableKeys(d: DocumentType, registry: Registry = REGISTRY): string[] {
  const common = Object.entries(registry.common_field_rules).filter(([, r]) => r.revealable).map(([key]) => key)
  const own = Object.entries(d.field_rules).filter(([, r]) => r.revealable).map(([key]) => key)
  return [...common, ...own]
}

/** Whether the GM may type into this field. `false` for a key the type does not
 * declare — unknown is never editable (X-8). */
export function isEditable(d: DocumentType, key: string, registry: Registry = REGISTRY): boolean {
  return ruleFor(d, key, registry)?.editable ?? false
}

export function labelFor(d: DocumentType, key: string, registry: Registry = REGISTRY): string | undefined {
  return ruleFor(d, key, registry)?.label
}

/** The allowlist, per key (REVEAL-10). `false` for an undeclared key: by ED-5
 * anything off the list is `gm_only` by construction. */
export function isRevealable(d: DocumentType, key: string, registry: Registry = REGISTRY): boolean {
  return ruleFor(d, key, registry)?.revealable ?? false
}

export function warningFor(d: DocumentType, key: string, registry: Registry = REGISTRY): string | null {
  return ruleFor(d, key, registry)?.warning ?? null
}

/** The group a key is toggled by, or `undefined` if it has a row of its own. */
export function revealGroupFor(d: DocumentType, key: string): RevealGroup | undefined {
  return d.reveal_groups.find((group) => group.keys.includes(key))
}

/** REVEAL-4: a default seeds the type's OWN audience and is empty for every
 * other — so the table of an owner-audience type gets `[]`, not `undefined`
 * and not an error (AUD-12). */
export function defaultRevealFor(d: DocumentType, audience: string): readonly string[] {
  return Object.hasOwn(d.default_reveal, audience) ? d.default_reveal[audience] : []
}

export class RegistryError extends Error {}

/** Every rule a registry must satisfy, reported together. Throws `RegistryError`. */
export function validateRegistry(registry: Registry): void {
  const problems: string[] = []
  const toolIds = registry.tools.map((t) => t.id)
  if (new Set(toolIds).size !== toolIds.length) problems.push('duplicate tool ids')
  if (new Set(toolIds).size !== TOOL_IDS.length || !TOOL_IDS.every((id) => toolIds.includes(id))) {
    problems.push('the registry must list every tool of the contract exactly once')
  }
  const seen = new Map<string, ToolId>()
  const capabilityIds = new Set(registry.capabilities.map((c) => c.id))
  for (const t of registry.tools) {
    for (const command of [t.command, ...t.aliases]) {
      const key = command.toLowerCase()
      if (!COMMAND.test(key)) problems.push(`${t.id}: ${JSON.stringify(command)} is not a well-formed command`)
      const other = seen.get(key)
      if (other) problems.push(`${t.id}: ${JSON.stringify(command)} collides with ${other}`)
      seen.set(key, t.id)
    }
    if (!ICON.test(t.icon)) problems.push(`${t.id}: icon ${JSON.stringify(t.icon)} is not a ligature name`)
    for (const [name, text] of [['label', t.label], ['blurb', t.blurb], ['working_label', t.working_label]] as const) {
      if (text.length < 1 || text.length > 60 || hasEntity(text)) problems.push(`${t.id}: ${name} is empty, too long or carries an HTML entity`)
    }
    if ((t.creates_doc_type !== null) !== (t.result_kind === 'document')) problems.push(`${t.id}: only a document result names the type it creates`)
    if (t.card_kind !== null && t.result_kind !== 'card') problems.push(`${t.id}: only a card result names a card kind`)
    if (t.capability !== null && !capabilityIds.has(t.capability)) problems.push(`${t.id}: unknown capability ${t.capability}`)
    if (t.creates_doc_type !== null && !registry.document_types.some((d) => d.id === t.creates_doc_type)) {
      problems.push(`${t.id}: creates an unknown document type`)
    }
  }
  const typeIds = registry.document_types.map((d) => d.id)
  if (new Set(typeIds).size !== typeIds.length || typeIds.length !== DOCUMENT_TYPE_IDS.length || !DOCUMENT_TYPE_IDS.every((id) => typeIds.includes(id))) {
    problems.push('the registry must list every document type of the contract exactly once')
  }
  for (const [key, r] of Object.entries(registry.common_field_rules)) {
    problems.push(...labelProblems(r.label).map((p) => `a common field: ${p}`))
    if (!Object.hasOwn(COMMON_FIELDS, key)) problems.push(`a rule for ${key}, which is not a common field`)
    // REVEAL-10 names `tags` among the keys that are never revealable, and ED-5
    // makes everything off an allowlist gm_only by construction. A rule rather
    // than only a pinned list, because a registry that made it revealable would
    // otherwise be internally consistent and would happily seed it.
    if (NEVER_REVEALABLE.includes(key) && r.revealable) {
      problems.push(`${key} is never revealable, on any type (REVEAL-10, ED-5)`)
    }
  }
  if (Object.keys(registry.common_field_rules).sort().join(',') !== Object.keys(COMMON_FIELDS).sort().join(',')) {
    problems.push('every common field has a rule, and nothing else does')
  }
  for (const d of registry.document_types) {
    problems.push(...documentTypeProblems(registry, d).map((p) => `${d.id}: ${p}`))
  }
  const pins = registry.default_pinned
  if (pins.length > registry.rail_limit || new Set(pins).size !== pins.length) problems.push(`default pins: at most ${registry.rail_limit}, each once`)
  for (const pin of pins) {
    const pinned = registry.tools.find((t) => t.id === pin)
    if (!pinned) problems.push(`default pin ${pin} is not a tool`)
    else if (pinned.capability !== null && (registry.capabilities.find((c) => c.id === pinned.capability)?.off_by_default ?? true)) {
      problems.push(`default pin ${pin} needs a capability that is off by default`)
    }
  }
  if (problems.length > 0) throw new RegistryError(problems.join('; '))
}

validateRegistry(REGISTRY)

export function toolById(id: string): Tool | undefined {
  return REGISTRY.tools.find((t) => t.id === id)
}

/** `undefined` for an unknown type: never a fallback to NPC (X-8). */
export function documentTypeById(id: string): DocumentType | undefined {
  return REGISTRY.document_types.find((d) => d.id === id)
}

/**
 * Which tools may run, given the deployment's capability switches. `null` is the
 * state before the lookup answered, or after it failed (AE-58): every tool is
 * disabled with one message, and the rail offers Retry.
 */
export function toolAvailability(enabled: Readonly<Partial<Record<CapabilityId, boolean>>> | null): Record<ToolId, ToolAvailability> {
  const out = {} as Record<ToolId, ToolAvailability>
  for (const t of REGISTRY.tools) {
    if (enabled === null) out[t.id] = { enabled: false, reason: "Couldn't check which tools are available" }
    else if (t.capability === null || enabled[t.capability] === true) out[t.id] = { enabled: true, reason: null }
    else out[t.id] = { enabled: false, reason: REGISTRY.capabilities.find((c) => c.id === t.capability)?.disabled_reason ?? 'Not available' }
  }
  return out
}

/** SLASH-2: the token is matched exactly, case-insensitively, against each command and its aliases. */
export function matchCommand(token: string): Tool | undefined {
  const key = token.toLowerCase()
  return REGISTRY.tools.find((t) => t.command === key || t.aliases.includes(key))
}

/** SLASH-9: the menu lists the tools whose command, alias or label starts with the typed token, in registry order. */
export function menuOptions(token: string): Tool[] {
  const typed = token.replace(/^\//, '').toLowerCase()
  if (typed === '') return [...REGISTRY.tools]
  return REGISTRY.tools.filter(
    (t) => [t.command, ...t.aliases].some((c) => c.slice(1).startsWith(typed)) || t.label.toLowerCase().startsWith(typed),
  )
}

/**
 * RAIL-11: pins are an ordered list of 0–5 unique, known tool ids. Absent means
 * the defaults; an empty list means the GM emptied the rail. Unknown ids are
 * dropped on read, nothing is back-filled, and a stored pin that is now disabled
 * keeps its place (RAIL-10) — it is `toolAvailability` that says so.
 */
export function normalisePins(stored: unknown): ToolId[] {
  if (stored === undefined || stored === null) return [...REGISTRY.default_pinned]
  if (!Array.isArray(stored)) return [...REGISTRY.default_pinned]
  const pins: ToolId[] = []
  for (const item of stored) {
    if (typeof item !== 'string') continue
    const known = toolById(item)
    if (known && !pins.includes(known.id)) pins.push(known.id)
    if (pins.length === REGISTRY.rail_limit) break
  }
  return pins
}

/** The unpinned tools, for More (SLASH-14), in registry order. */
export function overflow(pins: readonly ToolId[]): Tool[] {
  return REGISTRY.tools.filter((t) => !pins.includes(t.id))
}

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
 * Not here yet, on purpose: which fields a table may see, reveal groups and
 * warnings, audiences and per-audience default masks — those are
 * agent-forge-harness-1ir.1.2's decision and arrive with the reveal family
 * (1kg.1.6). Field labels exist only for declared fields; the other seven types'
 * labels land with their declarations (1kg.5.3).
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

export interface DocumentType {
  id: DocumentTypeId
  label: string
  icon: string
  renderer: Renderer
  library_category: LibraryCategory
  type_version: number
  fields: Readonly<Record<string, FieldKind>>
  field_labels: Readonly<Record<string, string>>
  printable: boolean
  cites_corpus: boolean
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
  common_field_labels: Readonly<Record<string, string>>
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

function documentType(
  id: DocumentTypeId,
  label: string,
  icon: string,
  extra: { renderer?: Renderer; field_labels?: Readonly<Record<string, string>>; printable?: boolean; cites_corpus?: boolean } = {},
): DocumentType {
  return {
    id,
    label,
    icon,
    renderer: extra.renderer ?? 'game_document',
    library_category: DOC_TYPE_LIBRARY_CATEGORY[id],
    type_version: DOC_TYPE_VERSION[id],
    fields: DOC_TYPE_FIELDS[id],
    field_labels: extra.field_labels ?? {},
    printable: extra.printable ?? false,
    cites_corpus: extra.cites_corpus ?? false,
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
      field_labels: {
        portrait: 'Portrait',
        voice: 'Voice',
        tell: 'Tell',
        attitude: 'Attitude',
        wants: 'Wants',
        leverage: 'Leverage',
        if_attacked: 'If the party attacks',
        notes: 'Notes',
      },
    }),
    documentType('statblock', 'Stat Block', 'shield', { renderer: 'stat_block_card' }),
    documentType('handout', 'Player Handout', 'mail', { printable: true }),
    documentType('session-notes', 'Session Notes', 'history_edu'),
    documentType('quest-log', 'Quest Log', 'flag'),
    documentType('character-sheet', 'Character Sheet', 'contact_page'),
    documentType('lore', 'Lore Entry', 'local_library', { cites_corpus: true }),
    documentType('encounter', 'Encounter', 'swords'),
  ],
  capabilities: [
    { id: 'image_generation', label: 'Image generation', disabled_reason: "Image generation isn't set up yet.", off_by_default: true },
    { id: 'audio_cues', label: 'Audio cues', disabled_reason: "Audio cues aren't set up yet.", off_by_default: true },
  ],
  default_pinned: ['npc', 'monster', 'loot', 'names', 'rules'],
  renderers: RENDERERS,
  common_field_labels: { name: 'Name', qualifier: 'Qualifier', tags: 'Tags' },
  rail_limit: RAIL_LIMIT,
}

/** A slash command as the parser matches it (SLASH-2): lower-case, after one `/`. */
const COMMAND = /^\/[a-z][a-z0-9-]{0,23}$/
/** A Material Symbols Rounded ligature name. */
const ICON = /^[a-z0-9_]{1,40}$/
const hasEntity = (text: string) => text.includes('&') && text.includes(';')

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
  for (const d of registry.document_types) {
    if (!registry.renderers.includes(d.renderer)) problems.push(`${d.id}: unknown renderer ${d.renderer}`)
    if (!ICON.test(d.icon)) problems.push(`${d.id}: icon ${JSON.stringify(d.icon)} is not a ligature name`)
    const declared = new Set([...Object.keys(COMMON_FIELDS), ...Object.keys(d.fields)])
    for (const [key, label] of Object.entries(d.field_labels)) {
      if (!declared.has(key)) problems.push(`${d.id}: a label for an undeclared field ${key}`)
      if (hasEntity(label)) problems.push(`${d.id}: the label for ${key} carries an HTML entity`)
    }
    const labelled = Object.keys(d.field_labels).sort().join(',')
    if (labelled !== Object.keys(d.fields).sort().join(',')) problems.push(`${d.id}: every declared field has a label, and nothing else does`)
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

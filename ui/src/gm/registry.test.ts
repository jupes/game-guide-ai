/**
 * The canonical GM tool and document-type registry (1kg.3.1) — the client half.
 * `contracts/workbench/v1/registry.json` is the one fixture; this suite and
 * `service/tests/test_workbench_registry.py` both compare their copy against it.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  RAIL_LIMIT,
  REGISTRY,
  RegistryError,
  TYPE_FLAG_SELECTORS,
  TYPE_STRUCTURE_KEYS,
  abilityRowKey,
  accentToken,
  audienceOf,
  citesCorpus,
  defaultRevealFor,
  documentTypeById,
  isPrintable,
  matchCommand,
  menuOptions,
  normalisePins,
  overflow,
  rendererFor,
  revealGroupFor,
  revealableKeys,
  ruleFor,
  showsAudiencePicker,
  toolAvailability,
  toolById,
  validateRegistry,
  warningFor,
} from './registry'
import type { DocumentType, FieldRule, Registry, Tool } from './registry'
import { CapabilitiesSchema } from './contracts'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'contracts', 'workbench', 'v1')
const file = JSON.parse(readFileSync(join(FIXTURES, 'registry.json'), 'utf-8')) as Record<string, unknown>

describe('the registry is the shared file', () => {
  it('lists the same tools, in the same order, with the same facts', () => {
    expect(
      REGISTRY.tools.map((t) => ({
        id: t.id,
        result_kind: t.result_kind,
        brief: t.brief,
        creates_doc_type: t.creates_doc_type,
        card_kind: t.card_kind,
        command: t.command,
        aliases: [...t.aliases],
        label: t.label,
        icon: t.icon,
        blurb: t.blurb,
        working_label: t.working_label,
        capability: t.capability,
      })),
    ).toEqual(file.tools)
  })

  it('lists the same document types', () => {
    expect(
      REGISTRY.document_types.map((d) => ({
        id: d.id,
        library_category: d.library_category,
        type_version: d.type_version,
        fields: d.fields,
        field_rules: d.field_rules,
        label: d.label,
        icon: d.icon,
        renderer: d.renderer,
        printable: d.printable,
        cites_corpus: d.cites_corpus,
        audience: d.audience,
        accent: d.accent,
        reveal_groups: d.reveal_groups,
        default_reveal: d.default_reveal,
        reserved_keys: d.reserved_keys,
      })),
    ).toEqual(file.document_types)
  })

  it('names exactly the switches the wire shape answers (RAIL-10)', () => {
    const switches = Object.keys(CapabilitiesSchema.shape).filter((key) => key !== 'schema_version').sort()
    expect(switches).toEqual(REGISTRY.capabilities.map((c) => c.id).sort())
  })

  it('agrees on capabilities, pins, renderers and common labels', () => {
    expect(REGISTRY.capabilities.map(({ id, label, disabled_reason }) => ({ id, label, disabled_reason }))).toEqual(file.capabilities)
    expect([...REGISTRY.default_pinned]).toEqual(file.default_pinned)
    expect(REGISTRY.rail_limit).toBe(file.rail_limit)
    expect(RAIL_LIMIT).toBe(file.rail_limit)
    expect([...REGISTRY.renderers]).toEqual(file.renderers)
    expect(REGISTRY.common_field_rules).toEqual(file.common_field_rules)
    // The two vocabularies the new per-type flags are checked against. This
    // suite names its top-level facts one by one, so anything unnamed here has
    // no three-way parity check at all.
    expect([...REGISTRY.accents]).toEqual(file.accents)
    expect([...REGISTRY.audiences]).toEqual(file.audiences)
  })
})

describe('lookups', () => {
  it('never fall back to NPC (X-8)', () => {
    expect(documentTypeById('faction')).toBeUndefined()
    expect(toolById('npcs')).toBeUndefined()
    expect(toolById('npc')?.label).toBe('NPC')
  })

  it('match a command exactly, case-insensitively, including aliases (SLASH-2)', () => {
    expect(matchCommand('/hooks')?.id).toBe('hooks')
    expect(matchCommand('/HOOK')?.id).toBe('hooks')
    expect(matchCommand('/hoo')).toBeUndefined()
    expect(matchCommand('/npcs')).toBeUndefined()
  })

  it('list menu options by prefix on command, alias or label, in registry order (SLASH-9)', () => {
    expect(menuOptions('/').map((t) => t.id)).toEqual(REGISTRY.tools.map((t) => t.id))
    expect(menuOptions('/m').map((t) => t.id)).toEqual(['monster', 'map'])
    expect(menuOptions('/hooks').map((t) => t.id)).toEqual(['hooks'])
    expect(menuOptions('/Enc').map((t) => t.id)).toEqual(['encounter'])
    expect(menuOptions('/xyz')).toEqual([])
  })
})

describe('availability (RAIL-10, AE-58)', () => {
  it('disables every tool with one message while the lookup is unanswered', () => {
    const all = toolAvailability(null)
    expect(Object.keys(all)).toHaveLength(REGISTRY.tools.length)
    expect(all.npc).toEqual({ enabled: false, reason: "Couldn't check which tools are available" })
  })

  it('disables only the tools whose capability is off, with its reason', () => {
    const off = toolAvailability({})
    expect(off.npc).toEqual({ enabled: true, reason: null })
    expect(off.portrait).toEqual({ enabled: false, reason: "Image generation isn't set up yet." })
    expect(off.map.enabled).toBe(false)
    const on = toolAvailability({ image_generation: true })
    expect(on.portrait).toEqual({ enabled: true, reason: null })
  })
})

describe('pins (RAIL-11)', () => {
  it('use the defaults when nothing is stored, and an empty list when the GM emptied the rail', () => {
    expect(normalisePins(undefined)).toEqual(['npc', 'monster', 'loot', 'names', 'rules'])
    expect(normalisePins(null)).toEqual(['npc', 'monster', 'loot', 'names', 'rules'])
    expect(normalisePins([])).toEqual([])
  })

  it('drop unknown ids and duplicates, keep order, and stop at five without back-filling', () => {
    expect(normalisePins(['map', 'npcs', 'map', 42, 'npc'])).toEqual(['map', 'npc'])
    expect(normalisePins(['npc', 'monster', 'loot', 'names', 'rules', 'map'])).toEqual(['npc', 'monster', 'loot', 'names', 'rules'])
    expect(normalisePins('npc')).toEqual(['npc', 'monster', 'loot', 'names', 'rules'])
  })

  it('leave the unpinned tools for More, in registry order (SLASH-14)', () => {
    expect(overflow(['npc', 'monster', 'loot', 'names', 'rules']).map((t) => t.id)).toEqual(['portrait', 'encounter', 'hooks', 'recap', 'map'])
  })
})

describe('per-type flags and their selectors (1kg.5.3)', () => {
  const doc = (id: string) => {
    const found = documentTypeById(id)
    expect(found).toBeDefined()
    return found as DocumentType
  }
  const selectors: Record<string, (d: DocumentType) => unknown> = {
    rendererFor,
    isPrintable,
    citesCorpus,
    audienceOf,
    showsAudiencePicker,
    accentToken,
  }

  it('reads every per-type flag through a selector, and nothing else', () => {
    // The bead's AC: a flag nothing reads is removed. A type entry's keys split
    // in two, and both halves are pinned, so neither can grow quietly.
    const types = file.document_types as Array<Record<string, unknown>>
    const keys = new Set(types.flatMap((d) => Object.keys(d)))
    const flags = Object.keys(TYPE_FLAG_SELECTORS)
    expect([...keys].sort()).toEqual([...TYPE_STRUCTURE_KEYS, ...flags].sort())
    expect(flags.filter((flag) => TYPE_STRUCTURE_KEYS.includes(flag))).toEqual([])
    for (const [flag, names] of Object.entries(TYPE_FLAG_SELECTORS)) {
      expect(keys.has(flag)).toBe(true)
      for (const name of names) expect(typeof selectors[name]).toBe('function')
    }
  })

  it('each selector answers the flag it names', () => {
    expect(rendererFor(doc('statblock'))).toBe('stat_block_card')
    expect(isPrintable(doc('handout'))).toBe(true)
    expect(isPrintable(doc('npc'))).toBe(false)
    expect(citesCorpus(doc('lore'))).toBe(true)
    expect(citesCorpus(doc('npc'))).toBe(false)
    expect(accentToken(doc('lore'))).toBe('arcane')
    expect(accentToken(doc('npc'))).toBeNull()
  })

  it('offers the audience picker on the character sheet alone (AUD-9)', () => {
    expect(REGISTRY.document_types.filter(showsAudiencePicker).map((d) => d.id)).toEqual(['character-sheet'])
    expect(audienceOf(doc('character-sheet'))).toBe('owner')
  })

  it('answers an empty default reveal for an audience its type does not name (REVEAL-4, AUD-12)', () => {
    expect(defaultRevealFor(doc('character-sheet'), 'table')).toEqual([])
    expect(defaultRevealFor(doc('npc'), 'owner')).toEqual([])
    expect(defaultRevealFor(doc('npc'), 'table')).toEqual(['portrait', 'name', 'voice'])
  })

  it('derives the ability row from the fields rather than storing it', () => {
    expect(abilityRowKey(doc('statblock'))).toBe('abilities')
    expect(abilityRowKey(doc('npc'))).toBeUndefined()
  })

  it('never puts tags on an allowlist, on any type (REVEAL-10, ED-5)', () => {
    for (const d of REGISTRY.document_types) {
      expect(revealableKeys(d)).not.toContain('tags')
    }
  })

  it('finds a reveal group by any of its keys, and the warning copy per field (REVEAL-11)', () => {
    const npc = doc('npc')
    expect(revealGroupFor(npc, 'voice')?.label).toBe('Name & voice')
    expect(revealGroupFor(npc, 'name')?.id).toBe('name_and_voice')
    expect(revealGroupFor(npc, 'notes')).toBeUndefined()
    expect(warningFor(npc, 'wants')).toBe('Would spoil the lie')
    expect(warningFor(npc, 'notes')).toBeNull()
  })

  it('derives the labels from the rules, so fieldLabel and the rules cannot drift', () => {
    for (const d of REGISTRY.document_types) {
      expect(d.field_labels).toEqual(
        Object.fromEntries(Object.entries(d.field_rules).map(([key, r]) => [key, r.label])),
      )
    }
    expect(REGISTRY.common_field_labels).toEqual({ name: 'Name', qualifier: 'Qualifier', tags: 'Tags' })
  })

  it('never invents a rule for a key the type does not declare (X-8)', () => {
    expect(ruleFor(doc('npc'), 'nonesuch')).toBeUndefined()
    // Object.hasOwn: `constructor` must read as "not declared", not find a function.
    expect(ruleFor(doc('npc'), 'constructor')).toBeUndefined()
    expect(ruleFor(doc('npc'), 'tags')).toBeDefined()
  })

  it('pins the reserved-key lists, so retiring a key is deliberate (ED-24)', () => {
    expect(Object.fromEntries(REGISTRY.document_types.map((d) => [d.id, [...d.reserved_keys]]))).toEqual(
      Object.fromEntries(REGISTRY.document_types.map((d) => [d.id, []])),
    )
  })
})

describe('a registry that breaks a rule cannot be built', () => {
  const withTool = (patch: Partial<Tool>, id = 'loot'): Registry => ({
    ...REGISTRY,
    tools: REGISTRY.tools.map((t) => (t.id === id ? { ...t, ...patch } : t)),
  })
  const withFirstType = (patch: Record<string, unknown>): Registry => ({
    ...REGISTRY,
    document_types: REGISTRY.document_types.map((d, i) => (i === 0 ? { ...d, ...patch } : d)),
  })
  const rule = (label: string): FieldRule => ({ label, editable: true, revealable: true, warning: null })
  const withNpcRules = (patch: Record<string, FieldRule>): Registry =>
    withFirstType({ field_rules: { ...REGISTRY.document_types[0].field_rules, ...patch } })
  const withCharacterSheet = (patch: Record<string, unknown>): Registry => ({
    ...REGISTRY,
    document_types: REGISTRY.document_types.map((d) => (d.id === 'character-sheet' ? { ...d, ...patch } : d)),
  })
  const cases: Array<[string, Registry, string]> = [
    ['an alias that collides with a command', withTool({ aliases: ['/NPC'] }), 'collides with npc'],
    ['a command without its slash', withTool({ aliases: ['loot'] }), 'not a well-formed command'],
    ['an unknown capability', withTool({ capability: 'teleportation' as never }), 'unknown capability'],
    ['an icon that is not a ligature', withTool({ icon: 'Diamond!' }), 'not a ligature name'],
    ['an HTML entity in a blurb', withTool({ blurb: 'Treasure &amp; hoards' }), 'HTML entity'],
    ['a missing tool', { ...REGISTRY, tools: REGISTRY.tools.slice(0, -1) }, 'every tool of the contract'],
    ['a duplicated tool', { ...REGISTRY, tools: [...REGISTRY.tools, REGISTRY.tools[2]] }, 'duplicate tool ids'],
    ['a card tool that claims to create a document', withTool({ creates_doc_type: 'npc' }), 'only a document result'],
    ['a document tool with a card kind', withTool({ card_kind: 'stat_block' }, 'npc'), 'only a card result'],
    ['a missing document type', { ...REGISTRY, document_types: REGISTRY.document_types.slice(1) }, 'creates an unknown document type'],
    ['a type icon that is not a ligature', withFirstType({ icon: 'Person!' }), 'not a ligature name'],
    ['six default pins', { ...REGISTRY, default_pinned: ['npc', 'monster', 'loot', 'names', 'rules', 'recap'] }, 'at most 5'],
    ['a repeated default pin', { ...REGISTRY, default_pinned: ['npc', 'npc'] }, 'each once'],
    ['a default pin that needs an off-by-default capability', { ...REGISTRY, default_pinned: ['portrait'] }, 'off by default'],
    ['an unknown renderer', withFirstType({ renderer: 'table' }), 'unknown renderer'],
    ['a rule for an undeclared field', withNpcRules({ xp_budget: rule('XP budget') }), 'every declared field'],
    ['an HTML entity in a field label', withNpcRules({ notes: rule('Terrain &amp; hazards') }), 'HTML entity'],
    // 1kg.5.3: a label is for a person, and the validator now says so.
    ['an empty field label', withNpcRules({ notes: rule('') }), 'is empty'],
    ['an over-long field label', withNpcRules({ notes: rule('x'.repeat(61)) }), 'longer than 60'],
    ['a snake_case field label', withNpcRules({ notes: rule('loose_threads') }), 'snake_case'],
    ['a camelCase field label', withNpcRules({ notes: rule('ifAttacked') }), 'camelCase'],
    // ED-2 / CANVAS-19: a declared key is flat, snake_case and bounded.
    ['a nested reserved key', withFirstType({ reserved_keys: ['motives.hidden'] }), 'not a flat field key'],
    ['a reserved key that came back', withFirstType({ reserved_keys: ['notes'] }), 'cannot be declared as a field again'],
    // REVEAL-11: a warning is about revealing, so an unrevealable key cannot carry one.
    [
      'a warning on a field that can never be revealed',
      withNpcRules({ notes: { label: 'Notes', editable: true, revealable: false, warning: 'Careful' } }),
      'can never be revealed',
    ],
    [
      'a reveal group naming a key off the allowlist',
      withFirstType({ reveal_groups: [{ id: 'secrets', label: 'Secrets', keys: ['name', 'tags'] }] }),
      'can never be revealed',
    ],
    [
      'a reveal group of one key',
      withFirstType({ reveal_groups: [{ id: 'solo', label: 'Solo', keys: ['name'] }] }),
      'fewer than two keys',
    ],
    [
      'a key in two reveal groups',
      withFirstType({
        reveal_groups: [
          { id: 'a', label: 'Voice one', keys: ['name', 'voice'] },
          { id: 'b', label: 'Voice two', keys: ['voice', 'tell'] },
        ],
      }),
      'more than one reveal group',
    ],
    // REVEAL-4 and AUD-12: a default seeds the type's own audience, never another's.
    ['a table default on an owner-audience type', withCharacterSheet({ default_reveal: { table: [] } }), "the type's audience is \"owner\""],
    ['a default reveal naming tags', withFirstType({ default_reveal: { table: ['tags'] } }), 'can never be revealed'],
    ['a default reveal naming nothing real', withFirstType({ default_reveal: { table: ['nonesuch'] } }), 'undeclared field'],
    ['an unknown audience', withFirstType({ audience: 'everyone' }), 'unknown audience'],
    ['an unknown accent', withFirstType({ accent: 'neon' }), 'unknown accent'],
  ]

  it.each(cases)('rejects %s', (_name, registry, problem) => {
    expect(() => validateRegistry(registry)).toThrow(RegistryError)
    expect(() => validateRegistry(registry)).toThrow(problem)
  })

  it('reports every problem at once', () => {
    const broken: Registry = { ...withTool({ aliases: ['/NPC'], icon: '!' }), default_pinned: ['npc', 'monster', 'loot', 'names', 'rules', 'recap'] }
    let message = ''
    try {
      validateRegistry(broken)
    } catch (error) {
      message = (error as Error).message
    }
    expect(message.split(';').length).toBeGreaterThanOrEqual(3)
  })
})

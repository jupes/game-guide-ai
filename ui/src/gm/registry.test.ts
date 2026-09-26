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
  FIELD_FLAG_SELECTORS,
  TYPE_FLAG_SELECTORS,
  TYPE_STRUCTURE_KEYS,
  abilityRowKey,
  accentToken,
  audienceOf,
  citesCorpus,
  defaultRevealFor,
  documentTypeById,
  boundsFor,
  isEditable,
  isPrintable,
  isRequired,
  isRevealable,
  labelFor,
  matchCommand,
  menuOptions,
  normalisePins,
  overflow,
  rendererFor,
  revealGroupFor,
  revealableKeys,
  rule as registryRule,
  ruleFor,
  seedsOwnerDefault,
  toolAvailability,
  toolById,
  validateRegistry,
  warningFor,
} from './registry'
import type { DocumentType, FieldRule, Registry, Tool } from './registry'
import { CapabilitiesSchema, COMMON_FIELDS } from './contracts'

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
    seedsOwnerDefault,
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

  it('seeds an owner default on the character sheet alone (ED-14, A-19)', () => {
    // Owner decision O-2 amends AUD-9: the flag says whose default reveal a type
    // SEEDS, and nothing about who may receive one — any type may be revealed to
    // a participant, and every type but the sheet seeds that participant empty.
    expect(REGISTRY.document_types.filter(seedsOwnerDefault).map((d) => d.id)).toEqual(['character-sheet'])
    expect(audienceOf(doc('character-sheet'))).toBe('owner')
    expect(seedsOwnerDefault(doc('npc'))).toBe(false)
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

  it('reads every key of a field rule through a selector too', () => {
    // The same rule as TYPE_FLAG_SELECTORS, one level down. Without it,
    // `editable` was declared on every field and read by nothing.
    const types = file.document_types as Array<{ field_rules: Record<string, Record<string, unknown>> }>
    const keys = new Set([
      ...types.flatMap((d) => Object.values(d.field_rules).flatMap((r) => Object.keys(r))),
      ...Object.values(file.common_field_rules as Record<string, Record<string, unknown>>).flatMap((r) =>
        Object.keys(r),
      ),
    ])
    const fieldSelectors: Record<string, unknown> = {
      labelFor,
      isEditable,
      isRequired,
      isRevealable,
      revealableKeys,
      warningFor,
      boundsFor,
    }
    expect([...keys].sort()).toEqual(Object.keys(FIELD_FLAG_SELECTORS).sort())
    for (const names of Object.values(FIELD_FLAG_SELECTORS)) {
      for (const name of names) expect(typeof fieldSelectors[name]).toBe('function')
    }
  })

  it('each field selector answers the flag it names', () => {
    const npc = doc('npc')
    const statblock = doc('statblock')
    expect(labelFor(npc, 'if_attacked')).toBe('If the party attacks')
    expect(isEditable(npc, 'wants')).toBe(true)
    expect(isRevealable(npc, 'wants')).toBe(true)
    expect(isRevealable(npc, 'tags')).toBe(false)
    expect(warningFor(npc, 'wants')).toBe('Would spoil the lie')
    // LIB-12, as the flag rather than as an invention of the New dialog.
    expect(isRequired(statblock, 'ac')).toBe(true)
    expect(isRequired(statblock, 'hp')).toBe(true)
    expect(isRequired(statblock, 'name')).toBe(true) // the common rule answers through the type
    expect(isRequired(npc, 'voice')).toBe(false)
    // Per-use integer bounds, and the one integer field deliberately without them.
    expect(boundsFor(statblock, 'ac')).toEqual([0, 1_000_000])
    expect(boundsFor(doc('session-notes'), 'session')).toEqual([1, 1_000_000])
    expect(boundsFor(statblock, 'xp')).toBeNull()
    expect(boundsFor(npc, 'voice')).toBeNull()
    // X-8 again: an undeclared key is not editable, not revealable, not required,
    // has no bounds and has no label.
    expect(isEditable(npc, 'nonesuch')).toBe(false)
    expect(isRevealable(npc, 'nonesuch')).toBe(false)
    expect(isRequired(npc, 'nonesuch')).toBe(false)
    expect(boundsFor(npc, 'nonesuch')).toBeNull()
    expect(labelFor(npc, 'nonesuch')).toBeUndefined()
  })

  it('makes only three rules in the whole registry required (LIB-12, ruling 5.7#2)', () => {
    // Pinned as a list rather than derived, so widening it is a deliberate act —
    // a field that becomes required is a type_version bump for every stored
    // document. The character sheet's own `ac` and `hp` stay optional: LIB-12
    // speaks of stat blocks, and a character in progress is a legitimate state.
    const required = Object.fromEntries(
      REGISTRY.document_types.map((d) => [
        d.id,
        [...Object.keys(COMMON_FIELDS), ...Object.keys(d.fields)].filter((key) => isRequired(d, key)).sort(),
      ]),
    )
    expect(required).toEqual({
      npc: ['name'],
      statblock: ['ac', 'hp', 'name'],
      handout: ['name'],
      'session-notes': ['name'],
      'quest-log': ['name'],
      'character-sheet': ['name'],
      lore: ['name'],
      encounter: ['name'],
    })
  })

  it('lets only an integer field carry bounds, and only inside its kind range', () => {
    // Three rules, each with its own negative case, because `bounds` is the first
    // field-rule key whose value a validator has to make sense of rather than
    // merely read.
    const statblock = doc('statblock')
    const broken = (changed: Record<string, FieldRule>): Registry => ({
      ...REGISTRY,
      document_types: REGISTRY.document_types.map((d) =>
        d.id === 'statblock' ? { ...d, field_rules: { ...statblock.field_rules, ...changed } } : d,
      ),
    })
    const withBounds = (label: string, bounds: readonly [number, number]): FieldRule =>
      ({ label, editable: true, required: false, revealable: true, warning: null, bounds })

    expect(() => validateRegistry(broken({ speed: withBounds('Speed', [0, 10]) }))).toThrow(/is not an integer field/)
    expect(() => validateRegistry(broken({ ac: withBounds('Armor Class', [10, 0]) }))).toThrow(/lowest is above its highest/)
    expect(() => validateRegistry(broken({ ac: withBounds('Armor Class', [0, 1_000_001]) }))).toThrow(
      /outside the integer kind's own range/,
    )
    // …and the shipped data satisfies all three.
    expect(() => validateRegistry(broken({}))).not.toThrow()
  })

  it('reads a field flagged not editable as not editable', () => {
    // Every shipped field is editable, so the selector is exercised against a
    // rule that is not — otherwise it could return true always.
    const npc = doc('npc')
    const frozen: DocumentType = {
      ...npc,
      field_rules: { ...npc.field_rules, notes: { label: 'Notes', editable: false, required: false, revealable: true, warning: null, bounds: null } },
    }
    expect(isEditable(frozen, 'notes')).toBe(false)
    expect(isEditable(frozen, 'wants')).toBe(true)
  })

  it('pins the reserved-key lists, so retiring a key is deliberate (ED-24)', () => {
    expect(Object.fromEntries(REGISTRY.document_types.map((d) => [d.id, [...d.reserved_keys]]))).toEqual(
      Object.fromEntries(REGISTRY.document_types.map((d) => [d.id, []])),
    )
  })

  // ED-24, and the reason this is written out rather than derived: taking a key
  // OFF a type's allowlist is a narrowing that no transaction carries — it ships
  // with a stop-scan that stops every live display holding that key — and this
  // test is what prompts it. Deriving the list would prove nothing.
  const REVEALABLE: Record<string, string[]> = {
    npc: ['name', 'qualifier', 'portrait', 'voice', 'tell', 'attitude', 'wants', 'leverage', 'if_attacked', 'notes'],
    statblock: [
      'name', 'qualifier', 'ac', 'ac_note', 'hp', 'hit_dice', 'speed', 'size', 'creature_type', 'alignment',
      'abilities', 'saving_throws', 'skills', 'damage_immunities', 'condition_immunities', 'senses', 'languages',
      'challenge_rating', 'xp', 'traits', 'actions', 'bonus_actions', 'reactions', 'legendary_actions',
    ],
    handout: ['name', 'qualifier', 'portrait', 'body'],
    'session-notes': ['name', 'qualifier', 'session', 'date', 'present', 'recap', 'beats', 'loose_threads'],
    'quest-log': ['name', 'qualifier', 'open_threads', 'cold_threads', 'resolved_threads'],
    'character-sheet': ['name', 'qualifier', 'portrait', 'ac', 'hp', 'speed', 'abilities', 'features', 'equipment', 'notes'],
    lore: ['name', 'qualifier', 'region', 'era', 'status', 'summary', 'history', 'rumours'],
    encounter: ['name', 'qualifier', 'difficulty', 'xp_budget', 'party_level', 'setup', 'combatants', 'terrain', 'outcome'],
  }

  it("pins every type's revealable allowlist (ED-24)", () => {
    expect(Object.fromEntries(REGISTRY.document_types.map((d) => [d.id, revealableKeys(d)]))).toEqual(REVEALABLE)
  })

  // What each type seeds, for its own audience alone. Pinned rather than
  // derived because these are the facts most likely to drift back to the
  // handoff's: REVEAL-23 made session-notes seed EMPTY where the handoff seeded
  // `recap`; AUD-12 makes the character sheet seed its OWNER and never the
  // table; and an encounter that seeded `outcome` would spoil the surprise it
  // carries a warning about.
  const DEFAULT_REVEAL: Record<string, Record<string, string[]>> = {
    npc: { table: ['portrait', 'name', 'voice'] },
    statblock: { table: [] },
    handout: { table: ['portrait', 'name', 'body'] },
    'session-notes': { table: [] },
    'quest-log': { table: ['name', 'open_threads', 'resolved_threads'] },
    'character-sheet': {
      owner: ['name', 'qualifier', 'portrait', 'ac', 'hp', 'speed', 'abilities', 'features', 'equipment', 'notes'],
    },
    lore: { table: ['name', 'summary'] },
    encounter: { table: [] },
  }

  it("pins every type's default reveal (REVEAL-4, REVEAL-23, AUD-12)", () => {
    expect(Object.fromEntries(REGISTRY.document_types.map((d) => [d.id, d.default_reveal]))).toEqual(DEFAULT_REVEAL)
  })

  it('lets only an owner-audience type seed anything but the table', () => {
    for (const d of REGISTRY.document_types) {
      expect(Object.keys(d.default_reveal).filter((a) => a !== d.audience)).toEqual([])
      if (d.audience !== 'table') expect(defaultRevealFor(d, 'table')).toEqual([])
    }
  })

  it('keeps tags, sources, ids and identity links off every allowlist (REVEAL-10, ED-5, ED-20)', () => {
    const never = ['tags', 'true_identity', 'sources', 'asset_id', 'document_id', 'campaign_id', 'author', 'changed_fields']
    for (const d of REGISTRY.document_types) {
      expect(revealableKeys(d).filter((key) => never.includes(key))).toEqual([])
    }
  })

  it('reads the registry it is given, not the shipped one', () => {
    // The two validators disagreed once because these helpers reached for the
    // module-level COMMON_FIELD_RULES while Python read the registry under
    // validation. Reverting either helper turns this red.
    const npc = doc('npc')
    const candidate: Registry = {
      ...REGISTRY,
      common_field_rules: {
        ...REGISTRY.common_field_rules,
        qualifier: { label: 'Qualifier', editable: false, required: false, revealable: false, warning: 'Candidate only', bounds: null },
      },
    }
    expect(ruleFor(npc, 'qualifier', candidate)?.warning).toBe('Candidate only')
    expect(ruleFor(npc, 'qualifier')?.warning).toBeNull()
    expect(revealableKeys(npc, candidate)).not.toContain('qualifier')
    expect(revealableKeys(npc)).toContain('qualifier')
    expect(isEditable(npc, 'qualifier', candidate)).toBe(false)
    expect(isEditable(npc, 'qualifier')).toBe(true)
  })

  it('gives the identity link a home, and never reveals it (ED-20)', () => {
    // Unguarded on purpose: the key must EXIST for this to prove anything, and
    // a version that skipped when it did not would pass with the field deleted.
    const npc = doc('npc')
    expect(Object.hasOwn(npc.fields, 'true_identity')).toBe(true)
    expect(isRevealable(npc, 'true_identity')).toBe(false)
    expect(revealableKeys(npc)).not.toContain('true_identity')
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
  const rule = (label: string, extra: Partial<FieldRule> = {}): FieldRule => ({
    label,
    editable: true,
    required: false,
    revealable: false,
    warning: null,
    bounds: null,
    ...extra,
  })
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
      withNpcRules({ notes: { label: 'Notes', editable: true, required: false, revealable: false, warning: 'Careful', bounds: null } }),
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
    // The branches the first round of review found untested.
    ['a reserved key listed twice', withFirstType({ reserved_keys: ['gone', 'gone'] }), 'listed twice'],
    [
      'a group id that is not a flat key',
      withFirstType({ reveal_groups: [{ id: 'Name Voice', label: 'Name & voice', keys: ['name', 'voice'] }] }),
      'is not a flat key',
    ],
    [
      'a group naming a field that does not exist',
      withFirstType({ reveal_groups: [{ id: 'ghosts', label: 'Ghosts', keys: ['name', 'nonesuch'] }] }),
      'undeclared field',
    ],
    [
      'two groups sharing an id',
      withFirstType({
        reveal_groups: [
          { id: 'same', label: 'One', keys: ['name', 'voice'] },
          { id: 'same', label: 'Two', keys: ['tell', 'attitude'] },
        ],
      }),
      'share an id',
    ],
    ['a default reveal naming a key twice', withFirstType({ default_reveal: { table: ['name', 'name'] } }), 'names a key twice'],
    [
      'a rule for something that is not a common field',
      { ...REGISTRY, common_field_rules: { ...REGISTRY.common_field_rules, nonesuch: rule('Nonesuch') } },
      'not a common field',
    ],
    ['a missing common field rule', { ...REGISTRY, common_field_rules: { name: rule('Name') } }, 'every common field has a rule'],
    [
      'a camelCase common field label',
      { ...REGISTRY, common_field_rules: { ...REGISTRY.common_field_rules, tags: rule('tagList') } },
      'camelCase',
    ],
    // REVEAL-10 / ED-5, on both routes into the allowlist: the common rule and
    // a type's own. The second was the hole the first review's fix left.
    [
      'a revealable tags common rule',
      { ...REGISTRY, common_field_rules: { ...REGISTRY.common_field_rules, tags: rule('Tags', { revealable: true }) } },
      'never revealable',
    ],
    ['a type declaring its own revealable tags rule', withNpcRules({ tags: rule('Tags', { revealable: true }) }), 'never revealable'],
    ['a type redeclaring a common field', withNpcRules({ name: rule('Name') }), 'cannot redeclare it'],
    // 1kg.5.7.2. F-12 (a): a raw key or an entity cannot pass as a label, on
    // every route a label takes.
    ['a field label that is a raw key', withNpcRules({ notes: rule('voice') }), 'lower case, like a key'],
    ['a two-letter raw key', withNpcRules({ notes: rule('xp') }), 'lower case, like a key'],
    ['an entity without its semicolon', withNpcRules({ notes: rule('&amp') }), 'HTML entity'],
    ['a numeric entity', withNpcRules({ notes: rule('&#39;') }), 'HTML entity'],
    ['a hex entity', withNpcRules({ notes: rule('&#x27; quote') }), 'HTML entity'],
    ['a named entity mid-label', withNpcRules({ notes: rule('Terrain &amp hazards') }), 'HTML entity'],
    [
      'a common label that is a raw key',
      { ...REGISTRY, common_field_rules: { ...REGISTRY.common_field_rules, qualifier: rule('qualifier', { revealable: true }) } },
      'lower case, like a key',
    ],
    [
      'a warning carrying an entity',
      withNpcRules({ wants: rule('Wants', { revealable: true, warning: '&amp spoils' }) }),
      'HTML entity',
    ],
    ['a reveal group label that is a raw key', withFirstType({ reveal_groups: [{ id: 'lies', label: 'voice', keys: ['wants', 'leverage'] }] }), 'lower case, like a key'],
    // F-12 (d): a retired key cannot come back as a common field.
    ['a reserved key that is a common field', withFirstType({ reserved_keys: ['qualifier'] }), 'cannot be declared as a field again'],
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

describe('tests a constant cannot satisfy, and the validator gaps (1kg.5.7.2)', () => {
  const doc = (id: string): DocumentType => {
    const found = documentTypeById(id)
    if (!found) throw new Error(`no type ${id}`)
    return found
  }

  it('gives every per-type selector a second, different answer (F-7, AC 12)', () => {
    // A selector that returns one constant fails one of these or the first set.
    expect(rendererFor(doc('npc'))).toBe('game_document')
    expect(audienceOf(doc('npc'))).toBe('table')
    // The character sheet is the one other type with an ability block.
    expect(abilityRowKey(doc('character-sheet'))).toBe('abilities')
  })

  it('refuses a type with two ability blocks — the twin of the Python test (F-7)', () => {
    const statblock = doc('statblock')
    const two: Registry = {
      ...REGISTRY,
      document_types: REGISTRY.document_types.map((d) =>
        d.id === 'statblock'
          ? {
              ...d,
              fields: { ...statblock.fields, stats: 'abilities' },
              field_rules: { ...statblock.field_rules, stats: registryRule('Stats', { revealable: true }) },
            }
          : d,
      ),
    }
    expect(() => validateRegistry(two)).toThrow('at most one ability block')
  })

  it('pins the type-entry key split to literal lists (F-7, AC 13)', () => {
    // Not to the module's own constants: moving `printable` out of the flag table
    // and into the structure keys (review mutation M6) left every suite green.
    expect([...TYPE_STRUCTURE_KEYS].sort()).toEqual([
      'default_reveal',
      'field_rules',
      'fields',
      'icon',
      'id',
      'label',
      'library_category',
      'reserved_keys',
      'reveal_groups',
      'type_version',
    ])
    expect(Object.keys(TYPE_FLAG_SELECTORS).sort()).toEqual(['accent', 'audience', 'cites_corpus', 'printable', 'renderer'])
    expect(Object.keys(FIELD_FLAG_SELECTORS).sort()).toEqual(['bounds', 'editable', 'label', 'required', 'revealable', 'warning'])
  })

  it('builds a label-only rule as NOT revealable (F-12 c, AC 21)', () => {
    expect(registryRule('Voice').revealable).toBe(false)
    expect(registryRule('Voice', { revealable: true }).revealable).toBe(true)
  })

  it.each([['Terrain & hazards'], ['XP'], ['XP budget'], ['Name']])('still takes the label %s (F-12 a)', (label) => {
    const withLabel: Registry = {
      ...REGISTRY,
      document_types: REGISTRY.document_types.map((d, i) =>
        i === 0 ? { ...d, field_rules: { ...d.field_rules, notes: registryRule(label, { revealable: true }) } } : d,
      ),
    }
    expect(() => validateRegistry(withLabel)).not.toThrow()
  })

  it("keeps a tool's own entity check unchanged (AC 20)", () => {
    const withBlurb = (blurb: string): Registry => ({
      ...REGISTRY,
      tools: REGISTRY.tools.map((t) => (t.id === 'loot' ? { ...t, blurb } : t)),
    })
    expect(() => validateRegistry(withBlurb('roll &amp hoards'))).not.toThrow()
    expect(() => validateRegistry(withBlurb('Treasure &amp; hoards'))).toThrow('HTML entity')
  })

  it('cannot be mutated at run time, even through a cast (F-12 h, AC 23)', () => {
    const npc = doc('npc')
    const identity = ruleFor(npc, 'true_identity')
    expect(identity?.revealable).toBe(false)
    // justification: the cast IS the attack under test. `Readonly<>` is a
    // compile-time promise; this proves the run-time freeze holds where it does not.
    const writable = identity as unknown as { revealable: boolean }
    expect(() => {
      writable.revealable = true
    }).toThrow(TypeError)
    expect(ruleFor(npc, 'true_identity')?.revealable).toBe(false)
    expect(revealableKeys(npc)).not.toContain('true_identity')
    const types = REGISTRY.document_types as unknown as DocumentType[]
    expect(() => types.push(npc)).toThrow(TypeError)
  })
})

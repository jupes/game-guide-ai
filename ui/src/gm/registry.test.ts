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
  documentTypeById,
  matchCommand,
  menuOptions,
  normalisePins,
  overflow,
  toolAvailability,
  toolById,
  validateRegistry,
} from './registry'
import type { Registry, Tool } from './registry'
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
        label: d.label,
        icon: d.icon,
        renderer: d.renderer,
        printable: d.printable,
        cites_corpus: d.cites_corpus,
        field_labels: d.field_labels,
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
    expect(REGISTRY.common_field_labels).toEqual(file.common_field_labels)
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

describe('a registry that breaks a rule cannot be built', () => {
  const withTool = (patch: Partial<Tool>, id = 'loot'): Registry => ({
    ...REGISTRY,
    tools: REGISTRY.tools.map((t) => (t.id === id ? { ...t, ...patch } : t)),
  })
  const withFirstType = (patch: Record<string, unknown>): Registry => ({
    ...REGISTRY,
    document_types: REGISTRY.document_types.map((d, i) => (i === 0 ? { ...d, ...patch } : d)),
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
    ['a label for an undeclared field', withFirstType({ field_labels: { ...REGISTRY.document_types[0].field_labels, xp_budget: 'XP' } }), 'undeclared field'],
    ['an HTML entity in a field label', withFirstType({ field_labels: { ...REGISTRY.document_types[0].field_labels, notes: 'Terrain &amp; hazards' } }), 'HTML entity'],
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

/**
 * Workbench wire contract (1kg.1.2).
 *
 * The JSON files under `contracts/workbench/v1` are the specification. This
 * suite and `service/tests/test_workbench_contracts.py` read the SAME files, so
 * the Zod schemas and the Pydantic models cannot drift apart without one of the
 * two failing.
 *
 * An example may carry `applies_to`. That is how the one deliberate asymmetry
 * is written down: the server is strict about what it emits, while a client
 * tolerates additive fields and unknown error codes from a newer server.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { basename, dirname, join } from 'node:path'
import type { ZodType } from 'zod'
import {
  BRIEF_POLICY,
  CARD_KINDS,
  COMMON_FIELDS,
  CONTRACT_SCHEMAS,
  CONTRACT_VERSION,
  DOCUMENT_TYPE_IDS,
  DOC_TYPE_FIELDS,
  DOC_TYPE_LIBRARY_CATEGORY,
  DOC_TYPE_VERSION,
  DocumentSchema,
  FIELD_KINDS,
  FieldPatchRequestSchema,
  LIBRARY_CATEGORIES,
  RESULT_KINDS,
  TOOL_CARD_KIND,
  TOOL_CREATES_DOC_TYPE,
  TOOL_IDS,
  TOOL_RESULT_KIND,
  codePointLength,
  isKnownErrorCode,
  parseDocument,
  parseTimelineEntry,
  parseTimelinePage,
  parseToolInvocation,
  parseToolResult,
  readErrorBody,
} from './contracts'
import { ChatResponseSchema, MessagesResponseSchema } from '../schemas'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'contracts', 'workbench', 'v1')
const SIDE = 'client'

/** Legacy schemas ride the same harness as a guard (see the fixtures' notes). */
const SCHEMAS: Record<string, ZodType> = {
  ...CONTRACT_SCHEMAS,
  LegacyChatResponse: ChatResponseSchema,
  LegacyMessagesResponse: MessagesResponseSchema,
}

interface Example {
  name: string
  value: unknown
  applies_to?: string[]
}
interface Fixture {
  schema: string
  valid: Example[]
  invalid: Example[]
}

/** The fixtures' two directives: "@repeat:a:2000" is 2,000 "a"s, and
 * {"@repeat_value": x, "@count": 101} is a list of 101 x's. The `u` flag
 * matters — without it `.` would match half of a character outside the BMP. */
function expand(value: unknown): unknown {
  if (typeof value === 'string') {
    const match = /^@repeat:(.):(\d+)$/u.exec(value)
    return match ? match[1].repeat(Number(match[2])) : value
  }
  if (Array.isArray(value)) return value.map(expand)
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value)
    const repeated = Object.fromEntries(entries) as { '@repeat_value'?: unknown; '@count'?: unknown }
    if (entries.length === 2 && '@repeat_value' in repeated && typeof repeated['@count'] === 'number') {
      return Array.from({ length: repeated['@count'] }, () => expand(repeated['@repeat_value']))
    }
    return Object.fromEntries(entries.map(([k, v]) => [k, expand(v)]))
  }
  return value
}

function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, 'utf-8')) as T
}

function fixtureFiles(dir: string = FIXTURES): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...fixtureFiles(full))
    else if (entry.name.endsWith('.json') && !['schemas.json', 'registry.json'].includes(entry.name)) out.push(full)
  }
  return out.sort()
}

function examples(kind: 'valid' | 'invalid'): Array<[string, string, unknown]> {
  const out: Array<[string, string, unknown]> = []
  for (const file of fixtureFiles()) {
    const doc = readJson<Fixture>(file)
    for (const example of doc[kind]) {
      if ((example.applies_to ?? ['server', 'client']).includes(SIDE)) out.push([doc.schema, example.name, example.value])
    }
  }
  return out
}

describe('shared fixtures', () => {
  it.each(examples('valid'))('%s accepts: %s', (schema, _name, value) => {
    const result = SCHEMAS[schema].safeParse(expand(value))
    expect(result.success ? [] : result.error.issues).toEqual([])
  })

  it.each(examples('invalid'))('%s fails closed on: %s', (schema, _name, value) => {
    expect(SCHEMAS[schema].safeParse(expand(value)).success).toBe(false)
  })

  it('implements and exercises every listed schema', () => {
    const listed = readJson<{ contract_version: number; schemas: string[] }>(join(FIXTURES, 'schemas.json'))
    expect(listed.contract_version).toBe(CONTRACT_VERSION)
    expect(Object.keys(SCHEMAS).sort()).toEqual([...listed.schemas].sort())

    const seen = new Map<string, Fixture>()
    for (const file of fixtureFiles()) {
      const doc = readJson<Fixture>(file)
      expect(basename(file, '.json')).toBe(doc.schema)
      seen.set(doc.schema, doc)
    }
    expect([...seen.keys()].sort()).toEqual([...listed.schemas].sort())
    for (const doc of seen.values()) {
      expect(doc.valid.length).toBeGreaterThan(0)
      expect(doc.invalid.length).toBeGreaterThan(0)
    }
  })
})

describe('registry facts', () => {
  interface Registry {
    contract_version: number
    result_kinds: string[]
    card_kinds: string[]
    library_categories: string[]
    tools: Array<{ id: string; result_kind: string; creates_doc_type: string | null; card_kind: string | null; brief: string }>
    field_kinds: string[]
    common_fields: Record<string, string>
    document_types: Array<{ id: string; library_category: string; type_version: number; fields: Record<string, string> }>
  }
  const registry = readJson<Registry>(join(FIXTURES, 'registry.json'))

  it('match the shared registry, which 1kg.3.1 extends', () => {
    expect(registry.contract_version).toBe(CONTRACT_VERSION)
    expect([...RESULT_KINDS]).toEqual(registry.result_kinds)
    expect([...CARD_KINDS]).toEqual(registry.card_kinds)
    expect([...LIBRARY_CATEGORIES]).toEqual(registry.library_categories)
    expect([...TOOL_IDS]).toEqual(registry.tools.map((t) => t.id))
    expect([...DOCUMENT_TYPE_IDS]).toEqual(registry.document_types.map((d) => d.id))

    for (const tool of TOOL_IDS) {
      const row = registry.tools.find((t) => t.id === tool)
      expect(row).toBeDefined()
      expect(TOOL_RESULT_KIND[tool]).toBe(row?.result_kind)
      expect(BRIEF_POLICY[tool]).toBe(row?.brief)
      expect(TOOL_CREATES_DOC_TYPE[tool] ?? null).toBe(row?.creates_doc_type)
      expect(TOOL_CARD_KIND[tool] ?? null).toBe(row?.card_kind)
    }
    for (const type of DOCUMENT_TYPE_IDS) {
      expect(DOC_TYPE_LIBRARY_CATEGORY[type]).toBe(registry.document_types.find((d) => d.id === type)?.library_category)
    }
  })

  it('declare the same fields for every document type', () => {
    expect([...FIELD_KINDS]).toEqual(registry.field_kinds)
    expect(COMMON_FIELDS).toEqual(registry.common_fields)
    for (const type of DOCUMENT_TYPE_IDS) {
      const row = registry.document_types.find((d) => d.id === type)
      expect(DOC_TYPE_FIELDS[type]).toEqual(row?.fields)
      expect(DOC_TYPE_VERSION[type]).toBe(row?.type_version)
    }
  })
})

describe('codePointLength', () => {
  it('counts characters the way the server does', () => {
    expect(codePointLength('')).toBe(0)
    expect(codePointLength('abc')).toBe(3)
    expect('🎲'.length).toBe(2) // why .length cannot be used for a bound
    expect(codePointLength('🎲🎲')).toBe(2)
  })
})

describe('forward-version behaviour (RAIL-24, X-8)', () => {
  const working = {
    schema_version: 1,
    invocation_id: 'inv_9f2c4e1a7b3d4c5e',
    tool_id: 'npc',
    status: 'working',
    attempt: 1,
    cancel_requested: false,
    created_at: '2026-09-16T19:31:02Z',
    updated_at: '2026-09-16T19:31:02Z',
    result: null,
    error: null,
  }

  it('parses a payload it understands', () => {
    const parsed = parseToolInvocation(working)
    expect(parsed.kind).toBe('ok')
    if (parsed.kind === 'ok') expect(parsed.value.tool_id).toBe('npc')
  })

  it('turns a newer schema version into a placeholder, not a crash', () => {
    expect(parseToolInvocation({ ...working, schema_version: 2, status: 'paused' })).toEqual({
      kind: 'unknown',
      reason: 'newer_schema',
    })
  })

  it('turns an unknown result kind into a placeholder', () => {
    expect(parseToolResult({ result_kind: 'table', tool_id: 'loot', prose: '', rows: [], suggestions: [] })).toEqual({
      kind: 'unknown',
      reason: 'unknown_kind',
    })
  })

  it('reports a known kind with a broken payload as invalid', () => {
    expect(parseToolResult({ result_kind: 'document', tool_id: 'npc', prose: '', suggestions: [] })).toEqual({
      kind: 'unknown',
      reason: 'invalid',
    })
    expect(parseToolInvocation({ ...working, attempt: 0 })).toEqual({ kind: 'unknown', reason: 'invalid' })
  })

  it('never throws on junk', () => {
    for (const junk of [null, undefined, 42, 'x', [], { schema_version: 'one' }]) {
      expect(parseToolInvocation(junk).kind).toBe('unknown')
      expect(parseToolResult(junk).kind).toBe('unknown')
    }
  })

  it('strips an additive field rather than passing it through', () => {
    const parsed = parseToolResult({
      result_kind: 'media',
      tool_id: 'map',
      prose: '',
      asset: { asset_id: 'ast_77c1d0e2', media_type: 'image', alt: 'Map', url: 'https://cdn.example.test/map.png' },
      suggestions: [],
    })
    expect(parsed.kind).toBe('ok')
    // X-10: even if a server ever sent a URL, the client model cannot carry it.
    if (parsed.kind === 'ok') expect(JSON.stringify(parsed.value)).not.toContain('example.test')
  })
})

describe('reading a timeline (AE-43, RAIL-24)', () => {
  const entries = readJson<Fixture>(join(FIXTURES, 'TimelineEntry.json'))
  const valid = (name: string) => {
    const found = entries.valid.find((example) => example.name === name)
    if (!found) throw new Error(`no fixture example named ${name}`)
    return found.value as Record<string, unknown>
  }
  const toolTurn = valid('a tool turn is a tool and a brief, never the slash string (RAIL-9)')
  const divider = valid('a session starts: the boundary recap reads from')
  const invocation = toolTurn.invocation as Record<string, unknown>

  it('reads an entry it understands', () => {
    const item = parseTimelineEntry(toolTurn)
    expect(item.kind).toBe('ok')
    if (item.kind === 'ok' && item.value.entry_kind === 'tool') expect(item.value.invocation.tool_id).toBe('npc')
  })

  it("treats the server's own placeholder as a valid entry", () => {
    const item = parseTimelineEntry(valid('a stored row this server cannot read keeps its place in the thread'))
    expect(item).toMatchObject({ kind: 'ok', value: { entry_kind: 'opaque', reason: 'unreadable' } })
  })

  const unusable: Array<[string, Record<string, unknown>, string]> = [
    ['a newer entry version', { ...divider, schema_version: 2, boundary: 'paused' }, 'newer_schema'],
    ['a newer invocation inside a version-1 entry', { ...toolTurn, invocation: { ...invocation, schema_version: 3 } }, 'newer_schema'],
    ['an entry kind it does not know', { ...divider, entry_kind: 'player_turn' }, 'unknown_kind'],
    [
      'a result kind it does not know',
      { ...toolTurn, invocation: { ...invocation, result: { result_kind: 'table', tool_id: 'npc', prose: '', rows: [], suggestions: [] } } },
      'unknown_kind',
    ],
    ['a known kind with a broken payload', { ...divider, boundary: 'paused' }, 'invalid'],
  ]

  it.each(unusable)('turns %s into a placeholder that keeps its id', (_name, raw, reason) => {
    expect(parseTimelineEntry(raw)).toEqual({ kind: 'unknown', reason, entry_id: raw.entry_id })
  })

  it('does not keep an id it could not trust as a key', () => {
    expect(parseTimelineEntry({ ...divider, entry_id: 'ent/../398', entry_kind: 'player_turn' })).toEqual({
      kind: 'unknown',
      reason: 'unknown_kind',
      entry_id: null,
    })
  })

  it('never throws on junk, however deep', () => {
    let deep: unknown = { schema_version: 9 }
    for (let i = 0; i < 40; i += 1) deep = { nested: [deep] }
    for (const junk of [null, undefined, 42, 'x', [], {}, deep]) {
      expect(parseTimelineEntry(junk)).toEqual({ kind: 'unknown', reason: 'invalid', entry_id: null })
    }
  })

  it('strips what a newer server adds, so hidden context cannot reach a component', () => {
    const item = parseTimelineEntry({ ...divider, context: 'SYSTEM: the party must not learn that…' })
    expect(item.kind).toBe('ok')
    expect(JSON.stringify(item)).not.toContain('SYSTEM')
  })

  const page = (items: unknown[], extra: Record<string, unknown> = {}) => ({
    schema_version: 1,
    conversation_id: '0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d',
    items,
    next_cursor: null,
    ...extra,
  })

  it('renders the rest of the thread around one entry it cannot use', () => {
    const parsed = parseTimelinePage(page([toolTurn, { ...divider, entry_kind: 'player_turn' }, divider]))
    expect(parsed.kind).toBe('ok')
    if (parsed.kind !== 'ok') return
    expect(parsed.value.items.map((item) => item.kind)).toEqual(['ok', 'unknown', 'ok'])
    expect(parsed.value.items[1]).toEqual({ kind: 'unknown', reason: 'unknown_kind', entry_id: 'ent_5e55a001' })
    expect(parsed.value.next_cursor).toBeNull()
  })

  it('keeps the cursor and the conversation it belongs to', () => {
    const parsed = parseTimelinePage(page([], { next_cursor: 'eyJiZWZvcmUiOjQxMn0' }))
    expect(parsed).toEqual({
      kind: 'ok',
      value: { conversation_id: '0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d', items: [], next_cursor: 'eyJiZWZvcmUiOjQxMn0' },
    })
  })

  it('refuses an envelope it cannot trust', () => {
    expect(parseTimelinePage(page([], { schema_version: 2 }))).toEqual({ kind: 'unknown', reason: 'newer_schema' })
    expect(parseTimelinePage(page(Array.from({ length: 101 }, () => divider)))).toEqual({ kind: 'unknown', reason: 'invalid' })
    expect(parseTimelinePage({ schema_version: 1, items: [] })).toEqual({ kind: 'unknown', reason: 'invalid' })
    for (const junk of [null, undefined, 42, 'x', []]) expect(parseTimelinePage(junk).kind).toBe('unknown')
  })
})

describe('reading a document (X-8, CANVAS-19)', () => {
  const documents = readJson<Fixture>(join(FIXTURES, 'Document.json'))
  const dossier = documents.valid.find((example) => example.name === 'an NPC dossier after an assistant edit')
    ?.value as Record<string, unknown>
  const withData = (data: Record<string, unknown>) => ({ ...dossier, data: { name: 'Sister Ondrey Vashe', ...data } })

  it('reads a document it understands', () => {
    const parsed = parseDocument(dossier)
    expect(parsed.kind).toBe('ok')
    if (parsed.kind === 'ok') expect(parsed.value.data.wants).toBe('The signet of the drowned saint, returned to the reliquary.')
  })

  it('never falls back to an NPC for a type it does not know', () => {
    // The handoff's registry did exactly that: documentType(id) || DOCUMENT_TYPES.npc.
    expect(parseDocument({ ...dossier, type: 'faction' })).toEqual({ kind: 'unknown', reason: 'unknown_kind' })
  })

  it('shows a placeholder for newer field definitions rather than half a document', () => {
    expect(parseDocument({ ...dossier, type_version: 2 })).toEqual({ kind: 'unknown', reason: 'newer_schema' })
    expect(parseDocument({ ...dossier, schema_version: 2 })).toEqual({ kind: 'unknown', reason: 'newer_schema' })
  })

  it('strips a field a newer server added, so the type can grow without a version bump', () => {
    const parsed = parseDocument(withData({ pronouns: 'she/her' }))
    expect(parsed.kind).toBe('ok')
    if (parsed.kind === 'ok') expect(Object.keys(parsed.value.data)).toEqual(['name'])
  })

  it('reads a key named like a JavaScript built-in as "not declared", not as something on a prototype', () => {
    const hostile = JSON.parse(
      '{"name": "Sister Ondrey Vashe", "constructor": "x", "toString": "y", "__proto__": {"wants": "polluted"}, "hasOwnProperty": 1}',
    ) as Record<string, unknown>
    const parsed = parseDocument({ ...dossier, data: hostile })
    expect(parsed.kind).toBe('ok')
    if (parsed.kind !== 'ok') return
    expect(Object.keys(parsed.value.data)).toEqual(['name'])
    expect(parsed.value.data.wants).toBeUndefined()
    expect(({} as Record<string, unknown>).wants).toBeUndefined() // nothing leaked onto Object.prototype

    // In a request the same keys are a client bug, and say so.
    const patch = { schema_version: 1, type: 'npc', type_version: 1, base_write_revision: 3, fields: { constructor: 'x' } }
    const result = FieldPatchRequestSchema.safeParse(patch)
    expect(result.success).toBe(false)
    if (!result.success) expect(result.error.issues[0].path).toEqual(['fields', 'constructor'])
  })

  it('cannot carry a URL into a component, even if a server sent one (X-10)', () => {
    const portrait = { asset_id: 'ast_77c1d0e2', media_type: 'image', alt: 'Portrait', url: 'https://cdn.example.test/p.png' }
    const parsed = DocumentSchema.safeParse(withData({ portrait }))
    expect(parsed.success).toBe(true)
    expect(JSON.stringify(parsed.data)).not.toContain('example.test')
  })

  it('reports a broken field at the field', () => {
    const result = DocumentSchema.safeParse(withData({ voice: 'Low.\nUnhurried.', tags: ['ok', ''] }))
    expect(result.success).toBe(false)
    if (!result.success) {
      expect(result.error.issues.map((issue) => issue.path.join('.'))).toEqual(['data.voice', 'data.tags.1'])
    }
  })

  it('never throws on junk', () => {
    for (const junk of [null, undefined, 42, 'x', [], {}, { type: 'npc' }, { ...dossier, data: null }, { ...dossier, data: [] }]) {
      expect(parseDocument(junk).kind).toBe('unknown')
    }
  })
})

describe('readErrorBody — one reader for every error shape', () => {
  it('reads a Workbench error', () => {
    const body = { detail: { code: 'cap_reached', message: 'Two tools are already running.', retryable: true } }
    expect(readErrorBody(body)).toEqual({ kind: 'workbench', info: body.detail })
  })

  it('keeps an unknown code but says it is not a known one', () => {
    const read = readErrorBody({ detail: { code: 'some_future_code', message: 'New.', retryable: false } })
    expect(read.kind).toBe('workbench')
    if (read.kind === 'workbench') expect(isKnownErrorCode(read.info.code)).toBe(false)
    expect(isKnownErrorCode('cap_reached')).toBe(true)
  })

  it("reads a legacy route's string detail", () => {
    expect(readErrorBody({ detail: 'authorization backend unavailable' })).toEqual({
      kind: 'legacy',
      message: 'authorization backend unavailable',
    })
  })

  it("reads FastAPI's 422 list as the fields at fault", () => {
    const body = {
      detail: [
        { loc: ['body', 'brief'], msg: 'Field required', type: 'missing' },
        { loc: ['body', 'campaign_id'], msg: 'Field required', type: 'missing' },
      ],
    }
    expect(readErrorBody(body)).toEqual({ kind: 'validation', fields: ['brief', 'campaign_id'] })
  })

  it('is unreadable, never a throw, for anything else', () => {
    for (const junk of [null, 'oops', 7, {}, { detail: 9 }, { detail: [{ loc: 'nope' }] }]) {
      expect(readErrorBody(junk)).toEqual({ kind: 'unreadable' })
    }
  })
})

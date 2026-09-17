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
  CONTRACT_SCHEMAS,
  CONTRACT_VERSION,
  DOCUMENT_TYPE_IDS,
  DOC_TYPE_LIBRARY_CATEGORY,
  LIBRARY_CATEGORIES,
  RESULT_KINDS,
  TOOL_CARD_KIND,
  TOOL_CREATES_DOC_TYPE,
  TOOL_IDS,
  TOOL_RESULT_KIND,
  codePointLength,
  isKnownErrorCode,
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

/** The fixtures' one directive: "@repeat:a:2000" is 2,000 "a"s. The `u` flag
 * matters — without it `.` would match half of a character outside the BMP. */
function expand(value: unknown): unknown {
  if (typeof value === 'string') {
    const match = /^@repeat:(.):(\d+)$/u.exec(value)
    return match ? match[1].repeat(Number(match[2])) : value
  }
  if (Array.isArray(value)) return value.map(expand)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, expand(v)]))
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
    document_types: Array<{ id: string; library_category: string }>
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

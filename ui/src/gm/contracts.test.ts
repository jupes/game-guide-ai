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
import { z, type ZodType } from 'zod'
import {
  ABILITY_SCORE_MAX,
  ABILITY_SCORE_MIN,
  ASSET_KINDS,
  AUDIO_SLOTS,
  BRIEF_POLICY,
  CARD_KINDS,
  COMMON_FIELDS,
  CONTRACT_SCHEMAS,
  CONTRACT_VERSION,
  CUE_KINDS,
  DOCUMENT_TYPE_IDS,
  DOC_TYPE_FIELDS,
  DOC_TYPE_LIBRARY_CATEGORY,
  DOC_TYPE_VERSION,
  DocumentCreateRequestSchema,
  DocumentSchema,
  EditRequestSchema,
  FIELD_KINDS,
  CONTENT_KINDS,
  FieldPatchRequestSchema,
  GM_EVENT_KINDS,
  GmEventSchema,
  INTEGER_FIELD_MAX,
  INTEGER_FIELD_MIN,
  LIBRARY_CATEGORIES,
  LIST_FIELD_MAX_ITEMS,
  LIST_ITEM_MAX_CHARS,
  LibraryQuerySchema,
  MaskKeySchema,
  MEDIA_TYPES,
  ENTRY_DISCRIMINATORS,
  GM_EVENT_DISCRIMINATORS,
  PROSE_FIELD_MAX_CHARS,
  RESERVED_MASK_KEYS,
  RESULT_DISCRIMINATORS,
  REVEALABLE_COMMON_FIELDS,
  REVEALABLE_FIELDS,
  RESULT_KINDS,
  TEXT_FIELD_MAX_CHARS,
  TABLE_EVENT_KINDS,
  TOOL_CARD_KIND,
  TOOL_CREATES_DOC_TYPE,
  TOOL_IDS,
  TOOL_RESULT_KIND,
  TableEventSchema,
  TableProjectionSchema,
  ToolInvocationRequestSchema,
  codePointLength,
  isKnownErrorCode,
  isWellFormedText,
  parseDocument,
  parseGmEvent,
  parseGmSnapshot,
  parseTableEvent,
  parseTableSnapshot,
  parseTimelineEntry,
  parseTimelinePage,
  parseToolInvocation,
  parseToolResult,
  readErrorBody,
  revealableFields,
  trimWire,
} from './contracts'
import type { DocumentTypeId } from './contracts'
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
  /** `request`, `response` or `both`; it documents, it does not change a check. */
  direction?: string
  /** `table` marks a shape a table device sends or receives (threat model 8.2). */
  channel?: string
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

/** The first valid example of a schema's fixture, as a request or response to vary. */
function firstValid(schema: string): Record<string, unknown> {
  return readJson<Fixture>(join(FIXTURES, `${schema}.json`)).valid[0].value as Record<string, unknown>
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

describe('what v1 deliberately has no shape for', () => {
  it('names no participant in anything a table client sends or receives (threat model 8.2, SEC-15)', () => {
    // The table client is not a restricted view of the GM API, it is a separate,
    // smaller API with its own principal. A guest asking beyond the table slot
    // gets what an empty table gives, never a refusal — which is only possible
    // if there is no shape in which it can ask.
    //
    // The *word* is not the test: `TableRole` is the enum `participant | guest`
    // and belongs on the table channel (TABLE-13). The identifier is. And what
    // no textual guard can catch is an id under another name — this is a
    // tripwire against the shape drifting, not a proof; the fixtures pin the
    // actual content of each frame.
    const namesAParticipant = (schema: ZodType) => {
      const json = JSON.stringify(z.toJSONSchema(schema, { io: 'input', unrepresentable: 'any' }))
      return json.includes('participant_id') || json.includes('participant_ids')
    }
    // The list is read from the fixtures, not written here: a fixture file marks
    // itself `"channel": "table"` and `"direction": "request"`, so a table-side
    // request someone adds later joins this test by existing. A hard-coded pair
    // would go on passing while the new shape carried an id.
    const tableRequests = fixtureFiles()
      .map((file) => readJson<Fixture>(file))
      .filter((doc) => doc.channel === 'table' && doc.direction === 'request')
      .map((doc) => doc.schema)
    expect(tableRequests.length).toBeGreaterThan(0)
    for (const name of tableRequests) {
      expect([name, namesAParticipant(CONTRACT_SCHEMAS[name])]).toEqual([name, false])
    }
    // The frames a table client receives name their slot `table` or `mine`,
    // never a slot reference, so no id travels that way either.
    for (const option of TableEventSchema.options) {
      expect([option.shape.event.value, namesAParticipant(option)]).toEqual([option.shape.event.value, false])
    }
  })

  it('keeps content_kind at exactly one member in v1 (ADR 7.4)', () => {
    // What reserving the discriminator buys is that a v1 table client meets a
    // future member as its neutral placeholder; adding one IS a version bump.
    // The cardinality is the claim, so it is the assertion — and the literal is
    // read from the schema, so the constant beside it cannot drift.
    expect([...CONTENT_KINDS]).toEqual(['document'])
    const projection = z.toJSONSchema(TableProjectionSchema, { io: 'input', unrepresentable: 'any' }) as unknown as {
      properties: Record<string, { const?: unknown }>
    }
    expect(projection.properties.content_kind.const).toBe('document')
  })

  it('declares no eligibility field anywhere (ED-11, ED-25)', () => {
    // v1 ships mask-only: eligibility binds reveal from 1ir.11.1, and the refusal
    // it needs is an additive error code. Nothing about classes or revisions ever
    // reaches a table client (REVEAL-24).
    for (const [name, schema] of Object.entries(CONTRACT_SCHEMAS)) {
      const json = JSON.stringify(z.toJSONSchema(schema, { io: 'input', unrepresentable: 'any' }))
      for (const word of ['eligibility', 'classification', 'authz_revision', 'projection_revision']) {
        expect([name, json.includes(`"${word}"`)]).toEqual([name, false])
      }
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
    field_bounds: Record<string, number>
    common_fields: Record<string, string>
    common_field_rules: Record<string, { revealable: boolean }>
    document_types: Array<{
      id: string
      library_category: string
      type_version: number
      fields: Record<string, string>
      field_rules: Record<string, { revealable: boolean }>
    }>
    asset_kinds: string[]
    media_types: Record<string, string[]>
    cue_kinds: string[]
    audio_slots: string[]
  }
  const registry = readJson<Registry>(join(FIXTURES, 'registry.json'))

  it('pin the media, cue and slot vocabularies', () => {
    expect([...ASSET_KINDS]).toEqual(registry.asset_kinds)
    expect(MEDIA_TYPES).toEqual(registry.media_types)
    expect([...CUE_KINDS]).toEqual(registry.cue_kinds)
    expect([...AUDIO_SLOTS]).toEqual(registry.audio_slots)
  })

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

  it('pin the revealable allowlist to the registry, for every type', () => {
    // REVEAL-10, ED-5, ED-20: ONE answer to "may this field reach a player", and
    // it is an allowlist. `1kg.5.3`'s per-field rule is the source; this module
    // holds a copy only because registry.ts imports it. Pinning the copy for
    // EVERY type is what stops a field marked `revealable: false` on a type
    // nobody wrote an assertion for from reaching a table.
    const allowed = (rules: Record<string, { revealable: boolean }>): string[] =>
      Object.entries(rules)
        .filter(([, rule]) => rule.revealable)
        .map(([key]) => key)
        .sort()

    expect([...REVEALABLE_COMMON_FIELDS].sort()).toEqual(allowed(registry.common_field_rules))
    expect(Object.keys(REVEALABLE_FIELDS).sort()).toEqual(registry.document_types.map((d) => d.id).sort())
    for (const row of registry.document_types) {
      const type = row.id as DocumentTypeId
      expect([type, [...REVEALABLE_FIELDS[type]].sort()]).toEqual([type, allowed(row.field_rules)])
      // And the derived set — what a mask and a projection are checked against —
      // never names a key the registry withholds, on any type.
      const withheld = Object.keys(row.field_rules)
        .concat(Object.keys(registry.common_field_rules))
        .filter((key) => !allowed(row.field_rules).includes(key) && !allowed(registry.common_field_rules).includes(key))
      for (const key of withheld) {
        expect([type, key, Object.hasOwn(revealableFields(type), key)]).toEqual([type, key, false])
      }
    }

    // ED-20's worked case, spelled out: the link between a face and what wears it.
    expect(Object.hasOwn(DOC_TYPE_FIELDS.npc, 'true_identity')).toBe(true)
    expect(Object.hasOwn(revealableFields('npc'), 'true_identity')).toBe(false)
    for (const type of DOCUMENT_TYPE_IDS) {
      expect(Object.hasOwn(revealableFields(type), 'tags')).toBe(false)
      expect(Object.hasOwn(revealableFields(type), 'all')).toBe(false)
    }
  })

  it('refuse a wildcard where a mask key is expected', () => {
    // REVEAL-9, ED-8: `all` matches the field-key shape, so it is refused by name;
    // `*` and `%` never matched it in the first place.
    expect(MaskKeySchema.safeParse('notes').success).toBe(true)
    for (const wildcard of ['all', '*', '**', '%', 'ALL']) {
      expect(MaskKeySchema.safeParse(wildcard).success).toBe(false)
    }
    expect([...RESERVED_MASK_KEYS]).toEqual(['all'])
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

  it("take every field kind's bounds from the shared registry, not from a second copy", () => {
    // A bound kept as two independent constants can drift: each suite goes on
    // testing against its own, and the differential fuzz never reaches the
    // values in between. registry.json holds the number, and the boundary
    // examples in Document.json exercise it on both sides.
    expect(registry.field_bounds).toEqual({
      text_field_max_chars: TEXT_FIELD_MAX_CHARS,
      prose_field_max_chars: PROSE_FIELD_MAX_CHARS,
      list_field_max_items: LIST_FIELD_MAX_ITEMS,
      list_item_max_chars: LIST_ITEM_MAX_CHARS,
      integer_field_min: INTEGER_FIELD_MIN,
      integer_field_max: INTEGER_FIELD_MAX,
      ability_score_min: ABILITY_SCORE_MIN,
      ability_score_max: ABILITY_SCORE_MAX,
    })
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

describe('text on the wire', () => {
  const request = firstValid('ToolInvocationRequest')
  const query = firstValid('LibraryQuery')
  const edit = firstValid('EditRequest')
  const lone = `x${String.fromCharCode(0xd83c)}`

  it('refuses a lone surrogate wherever the server would: a 422, never a 500', () => {
    expect(isWellFormedText('🎲')).toBe(true)
    expect(isWellFormedText(lone)).toBe(false)
    expect(ToolInvocationRequestSchema.safeParse({ ...request, brief: lone }).success).toBe(false)
    expect(LibraryQuerySchema.safeParse({ ...query, search: lone }).success).toBe(false)
    expect(EditRequestSchema.safeParse({ ...edit, instruction: { kind: 'text', text: lone } }).success).toBe(false)
    expect(
      EditRequestSchema.safeParse({ ...edit, scope: { kind: 'selection', field: 'notes', start: 0, end: 2, text: lone } }).success,
    ).toBe(false)
    expect(DocumentSchema.safeParse({ ...firstValid('Document'), data: { name: lone } }).success).toBe(false)
  })

  it('trims exactly what the server trims', () => {
    const [bom, nel, ideographicSpace, nbsp, lineSeparator] = [0xfeff, 0x85, 0x3000, 0xa0, 0x2028].map((code) =>
      String.fromCharCode(code),
    )
    expect(trimWire(`${bom} CR 5${ideographicSpace}\t\n`)).toBe('CR 5')
    // NEL is not in the set, so it stays — on both sides.
    expect(trimWire(`${nel}CR 5${nel}`)).toBe(`${nel}CR 5${nel}`)
    expect(trimWire(bom)).toBe('')
    // The same answer as String.prototype.trim today, spelled out so that it cannot drift.
    for (const sample of [`${bom} CR 5${ideographicSpace}`, `${nel}x`, ` ${nbsp}x${lineSeparator}`, '', '  ']) {
      expect(trimWire(sample)).toBe(sample.trim())
    }
    // A brief of only a byte order mark is empty, and a required brief that is empty is refused.
    expect(ToolInvocationRequestSchema.safeParse({ ...request, brief: bom }).success).toBe(false)
    expect(ToolInvocationRequestSchema.safeParse({ ...request, tool_id: 'recap', brief: bom }).success).toBe(true)
  })

  it('refuses __proto__ among the fields of a request instead of dropping it', () => {
    const patch = (fields: string, beside = '') =>
      JSON.parse(
        `{"schema_version": 1, "type": "npc", "type_version": 1, "base_write_revision": 3, "fields": ${fields}${beside}}`,
      ) as unknown
    const inside = FieldPatchRequestSchema.safeParse(patch('{"wants": "x", "__proto__": {"notes": "polluted"}}'))
    expect(inside.success).toBe(false)
    if (!inside.success) expect(inside.error.issues[0].path).toEqual(['fields', '__proto__'])
    expect(FieldPatchRequestSchema.safeParse(patch('{"wants": "x"}', ', "__proto__": {"x": 1}')).success).toBe(false)
    expect(FieldPatchRequestSchema.safeParse(patch('{"wants": "x"}')).success).toBe(true)

    const create = JSON.parse(
      '{"schema_version": 1, "command_id": "cmd_4d1c2b3a9f8e7d6c", "campaign_id": "cmp_1", "type": "npc", "type_version": 1, "data": {"name": "x", "__proto__": {"wants": "y"}}}',
    ) as unknown
    expect(DocumentCreateRequestSchema.safeParse(create).success).toBe(false)
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

  it('turns an unknown card kind into the same placeholder (1kg.4.3 adds four)', () => {
    const loot = { result_kind: 'card', tool_id: 'loot', prose: '', suggestions: [], card: { card_kind: 'loot', items: [] } }
    expect(parseToolResult(loot)).toEqual({ kind: 'unknown', reason: 'unknown_kind' })
    expect(parseToolInvocation({ ...working, status: 'done', result: loot })).toEqual({ kind: 'unknown', reason: 'unknown_kind' })
  })

  it('reads a version as an integral number only', () => {
    expect(parseToolInvocation({ ...working, schema_version: 1.5 })).toEqual({ kind: 'unknown', reason: 'invalid' })
    expect(parseToolInvocation({ ...working, schema_version: '2' })).toEqual({ kind: 'unknown', reason: 'invalid' })
    expect(parseToolInvocation({ ...working, schema_version: 2.0 })).toEqual({ kind: 'unknown', reason: 'newer_schema' })
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
  const validStarting = (prefix: string) => {
    const found = entries.valid.find((example) => example.name.startsWith(prefix))
    if (!found) throw new Error(`no fixture example starting with ${prefix}`)
    return found.value as Record<string, unknown>
  }
  const toolTurn = valid('a tool turn is a tool and a brief, never the slash string (RAIL-9)')
  const divider = valid('a session starts: the boundary recap reads from')
  const chat = validStarting('a chat exchange with its complete outcome')
  const editTurn = validStarting('an edit')
  const invocation = toolTurn.invocation as Record<string, unknown>
  const editInvocation = editTurn.invocation as Record<string, unknown>

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
    [
      'a card kind it does not know',
      {
        ...toolTurn,
        invocation: {
          ...invocation,
          status: 'done',
          result: { result_kind: 'card', tool_id: 'monster', prose: '', suggestions: [], card: { card_kind: 'loot', items: [] } },
        },
      },
      'unknown_kind',
    ],
    [
      'an edit outcome it does not know',
      { ...editTurn, invocation: { ...editInvocation, status: 'done', error: null, result: { outcome: 'partial' } } },
      'unknown_kind',
    ],
    ['a scope kind it does not know', { ...editTurn, scope: { kind: 'entry', field: 'tags', index: 2 } }, 'unknown_kind'],
    ['an instruction kind it does not know', { ...editTurn, instruction: { kind: 'voice', clip: 'aud_1' } }, 'unknown_kind'],
    ['a known kind with a broken payload', { ...divider, boundary: 'paused' }, 'invalid'],
    // A version is an integral number; anything else is not a version, and not the future.
    ['a version that is not an integer', { ...divider, schema_version: 1.5 }, 'invalid'],
  ]

  it.each(unusable)('turns %s into a placeholder that keeps its id', (_name, raw, reason) => {
    expect(parseTimelineEntry(raw)).toEqual({ kind: 'unknown', reason, entry_id: raw.entry_id })
  })

  it('does not mistake a number deeper in the payload for a version', () => {
    const answer = chat.answer as Record<string, unknown>
    const stray = { ...chat, answer: { ...answer, schema_version: 9 } }
    // Stripped like any additive field: the entry is read.
    expect(parseTimelineEntry(stray).kind).toBe('ok')
    // And when the entry is broken as well, that is damage, not the future.
    expect(parseTimelineEntry({ ...stray, mode: 42 })).toEqual({ kind: 'unknown', reason: 'invalid', entry_id: chat.entry_id })
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
    // A version is an integral number; 1.5 is not a version, and not the future.
    expect(parseDocument({ ...dossier, type_version: 1.5 })).toEqual({ kind: 'unknown', reason: 'invalid' })
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

describe('the extra-field policy, per type (1kg.5.3)', () => {
  const document = (type: DocumentTypeId, data: Record<string, unknown>) => ({
    schema_version: 1,
    document_id: 'doc_9k2f7a1c',
    campaign_id: 'cmp_4b1d9e7a',
    type,
    type_version: 1,
    data,
    write_revision: 1,
    version: {
      number: 1,
      author: 'gm',
      summary: '',
      created_at: '2026-09-16T20:00:00Z',
      sealed: false,
      changed_fields: ['name'],
      restored_from: null,
    },
    archived: false,
    created_at: '2026-09-16T20:00:00Z',
    updated_at: '2026-09-16T20:00:00Z',
  })

  it.each(DOCUMENT_TYPE_IDS.map((id) => [id] as const))(
    '%s: strips a key the type does not declare, and still yields the document',
    (type) => {
      // The other half of the asymmetry the server enforces: a newer server may
      // add a field, and a client that has not learned it drops the key rather
      // than refusing the whole document (wire contract, "Versioning").
      const parsed = parseDocument(document(type, { name: 'A document', smuggled_key: 'Drown the harbourmaster.' }))
      expect(parsed.kind).toBe('ok')
      if (parsed.kind === 'ok') {
        expect(Object.keys(parsed.value.data)).toEqual(['name'])
        expect(JSON.stringify(parsed.value)).not.toContain('harbourmaster')
      }
    },
  )

  it.each(DOCUMENT_TYPE_IDS.map((id) => [id] as const))('%s: refuses the same key in a request', (type) => {
    const patch = { schema_version: 1, type, type_version: 1, base_write_revision: 1, fields: { smuggled_key: 'x' } }
    const result = FieldPatchRequestSchema.safeParse(patch)
    expect(result.success).toBe(false)
    if (!result.success) expect(result.error.issues[0].path).toEqual(['fields', 'smuggled_key'])
  })

  it.each(DOCUMENT_TYPE_IDS.map((id) => [id] as const))('%s: takes the common fields', (type) => {
    const parsed = parseDocument(document(type, { name: 'A document', qualifier: '', tags: [] }))
    expect(parsed.kind).toBe('ok')
  })
})

describe('the structured field kinds (1kg.5.3)', () => {
  const block = (data: Record<string, unknown>) => ({
    schema_version: 1,
    document_id: 'doc_5b1a2c3d',
    campaign_id: 'cmp_4b1d9e7a',
    type: 'statblock',
    type_version: 1,
    data: { name: 'Ondrey', ...data },
    write_revision: 1,
    version: {
      number: 1,
      author: 'gm',
      summary: '',
      created_at: '2026-09-16T20:00:00Z',
      sealed: false,
      changed_fields: ['name'],
      restored_from: null,
    },
    archived: false,
    created_at: '2026-09-16T20:00:00Z',
    updated_at: '2026-09-16T20:00:00Z',
  })
  const read = (data: Record<string, unknown>) => DocumentSchema.safeParse(block(data))
  const patch = (fields: Record<string, unknown>) =>
    FieldPatchRequestSchema.safeParse({ schema_version: 1, type: 'statblock', type_version: 1, base_write_revision: 1, fields })

  it.each([
    ['a whole number', 7, 7],
    ['a whole number written as a float, because JavaScript cannot tell them apart', 7.0, 7],
    ['zero', 0, 0],
    ['a negative', -1, -1],
    ['the ceiling', INTEGER_FIELD_MAX, INTEGER_FIELD_MAX],
    ['null, which clears it', null, null],
  ])('an integer field takes %s', (_name, given, stored) => {
    const parsed = read({ ac: given })
    expect(parsed.success).toBe(true)
    if (parsed.success) expect(parsed.data.data.ac).toEqual(stored)
  })

  it.each([[true], ['7'], [1.5], [[]], [{}], [INTEGER_FIELD_MAX + 1], [INTEGER_FIELD_MIN - 1]])(
    'an integer field refuses %o in a request',
    (given) => {
      expect(patch({ ac: given }).success).toBe(false)
    },
  )

  it.each([
    [{}],
    [{ str: 10 }],
    [{ str: 10, dex: 12, con: 14, int: 8, wis: 13, cha: 16 }],
    [{ str: null }],
    [null],
  ])('an abilities field takes %o — CANVAS-19, one field', (given) => {
    const parsed = read({ abilities: given })
    expect(parsed.success).toBe(true)
    if (parsed.success) expect(parsed.data.data.abilities).toEqual(given)
  })

  it.each([
    [{ strength: 10 }],
    [{ str: '10' }],
    [{ str: true }],
    [{ str: ABILITY_SCORE_MAX + 1 }],
    [{ str: ABILITY_SCORE_MIN - 1 }],
    [[]],
    ['10'],
  ])('an abilities field refuses %o in a request', (given) => {
    expect(patch({ abilities: given }).success).toBe(false)
  })

  it('an entry list takes named entries and clears to []', () => {
    const entries = [
      { name: 'Amphibious', text: 'She breathes water.' },
      { name: 'Silent', text: '' },
    ]
    const parsed = read({ traits: entries })
    expect(parsed.success).toBe(true)
    if (parsed.success) expect(parsed.data.data.traits).toEqual(entries)
    const cleared = read({ traits: [] })
    expect(cleared.success).toBe(true)
    if (cleared.success) expect(cleared.data.data.traits).toEqual([])
  })

  it.each([
    [[{ name: 'Amphibious' }]],
    [[{ text: 'no name' }]],
    [[{ name: '', text: 'x' }]],
    [[{ name: 'two\nlines', text: 'x' }]],
    [['Amphibious']],
    [{}],
    [null],
  ])('an entry list refuses %o in a request', (given) => {
    expect(patch({ traits: given }).success).toBe(false)
  })

  it('a request rejects an unknown sub-key; a response strips it (the AssetRef precedent)', () => {
    expect(patch({ traits: [{ name: 'Amphibious', text: 'x', damage: '1d6' }] }).success).toBe(false)
    expect(patch({ abilities: { str: 10, luck: 3 } }).success).toBe(false)
    const parsed = read({ traits: [{ name: 'Amphibious', text: 'x', damage: '1d6' }], abilities: { str: 10, luck: 3 } })
    expect(parsed.success).toBe(true)
    if (parsed.success) {
      expect(parsed.data.data.traits).toEqual([{ name: 'Amphibious', text: 'x' }])
      expect(parsed.data.data.abilities).toEqual({ str: 10 })
    }
  })

  it('refuses a prototype key smuggled into an ability block in a request', () => {
    const fields = JSON.parse('{"abilities": {"str": 10, "__proto__": {"polluted": true}}}') as Record<string, unknown>
    const result = patch(fields)
    expect(result.success).toBe(false)
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
  })

  it('bounds an entry list at its edges', () => {
    const one = { name: 'n', text: 't' }
    expect(read({ traits: Array.from({ length: LIST_FIELD_MAX_ITEMS }, () => one) }).success).toBe(true)
    expect(patch({ traits: Array.from({ length: LIST_FIELD_MAX_ITEMS + 1 }, () => one) }).success).toBe(false)
    expect(patch({ traits: [{ name: 'a'.repeat(TEXT_FIELD_MAX_CHARS + 1), text: 't' }] }).success).toBe(false)
    expect(patch({ traits: [{ name: 'n', text: 't'.repeat(LIST_ITEM_MAX_CHARS + 1) }] }).success).toBe(false)
  })
})

describe('reading a realtime frame (ADR RT-1, threat model 8.3)', () => {
  const gm = readJson<Fixture>(join(FIXTURES, 'GmEvent.json'))
  const table = readJson<Fixture>(join(FIXTURES, 'TableEvent.json'))
  const first = (doc: Fixture, prefix: string) => {
    const found = doc.valid.find((example) => example.name.startsWith(prefix))
    if (!found) throw new Error(`no fixture example starting with ${prefix}`)
    return found.value as Record<string, unknown>
  }

  it('pins each channel vocabulary to its union', () => {
    expect(GmEventSchema.options.map((option) => option.shape.event.value).sort()).toEqual([...GM_EVENT_KINDS].sort())
    expect(TableEventSchema.options.map((option) => option.shape.event.value).sort()).toEqual([...TABLE_EVENT_KINDS].sort())
  })

  it('reads the frames it understands', () => {
    for (const example of gm.valid) expect(parseGmEvent(expand(example.value)).kind).toBe('ok')
    for (const example of table.valid) expect(parseTableEvent(expand(example.value)).kind).toBe('ok')
  })

  it('turns a kind it does not know into a placeholder', () => {
    // `snapshot` and `slot` were the stand-ins here until the reveal family landed;
    // both are known kinds now, so the probe moves to one this contract does not define.
    expect(parseGmEvent({ schema_version: 1, event: 'excerpt', span: {} })).toEqual({ kind: 'unknown', reason: 'unknown_kind' })
    expect(parseTableEvent({ schema_version: 1, event: 'excerpt', span: {} })).toEqual({
      kind: 'unknown',
      reason: 'unknown_kind',
      slot: null,
      seq: null,
      slots: null,
    })
    // Presence never travels on the table channel; to a table client the kind is simply unknown.
    expect(parseTableEvent(first(gm, 'who is listening'))).toMatchObject({ kind: 'unknown', reason: 'unknown_kind' })
  })

  it('strips what a table client may never see, however deep it rides (SEC-15)', () => {
    // The client is deliberately tolerant of a field a newer server added, so it
    // cannot *refuse* these — but it must not pass them on either. A value typed
    // `unknown` would survive verbatim, which is how a GM-side asset id, a
    // filename and a GM note reached a component in an earlier revision.
    const slot = first(table, 'the table slot is now showing') as { content: { fields: Array<Record<string, unknown>> } }
    const portrait = slot.content.fields.find((field) => field.key === 'portrait')
    if (!portrait) throw new Error('the fixture needs an asset field')
    const smuggled = {
      ...slot,
      content: {
        ...slot.content,
        fields: [
          {
            ...portrait,
            value: { ...(portrait.value as object), asset_id: 'ast_77c1d0e2', filename: 'ondrey-true-face.webp', gm_note: 'she is the lich' },
          },
        ],
      },
    }
    const read = parseTableEvent(smuggled)
    expect(read.kind).toBe('ok')
    const rendered = JSON.stringify(read)
    for (const secret of ['ast_77c1d0e2', 'ondrey-true-face.webp', 'she is the lich', 'asset_id', 'filename', 'gm_note']) {
      expect([secret, rendered.includes(secret)]).toEqual([secret, false])
    }
  })

  it('strips every key the server is forbidden to emit, at every depth of a projection (SEC-15)', () => {
    // The eleven `applies_to: ["server"]` examples in TableProjection.json are
    // skipped by this suite by design — the server refuses them, a client
    // tolerates and strips. "Tolerates" was never pinned: with a loose object
    // they would have travelled to a player's device intact. This is that pin,
    // read from the same fixtures so it cannot fall behind them.
    const projection = readJson<Fixture>(join(FIXTURES, 'TableProjection.json'))
    const serverOnly = projection.invalid.filter((example) => example.applies_to?.length === 1 && example.applies_to[0] === 'server')
    expect(serverOnly.length).toBeGreaterThanOrEqual(11)

    // The whole vocabulary a projection may use, at every level — read from the
    // schema itself, so it cannot drift from what the shapes declare. Two of the
    // eleven examples smuggle their key *inside* a field object rather than at
    // the top, so a top-level check alone would assert nothing about them.
    const declaredBy = (node: unknown): string[] =>
      node !== null && typeof node === 'object'
        ? Object.entries(node).flatMap(([key, child]) =>
            key === 'properties' && child !== null && typeof child === 'object'
              ? [...Object.keys(child), ...declaredBy(child)]
              : declaredBy(child),
          )
        : []
    const declared = new Set(declaredBy(z.toJSONSchema(TableProjectionSchema, { io: 'output' })))
    expect(declared.size).toBeGreaterThan(0)

    const keysAtEveryDepth = (node: unknown): string[] =>
      Array.isArray(node)
        ? node.flatMap(keysAtEveryDepth)
        : node !== null && typeof node === 'object'
          ? Object.entries(node).flatMap(([key, child]) => [key, ...keysAtEveryDepth(child)])
          : []

    for (const example of serverOnly) {
      const parsed = TableProjectionSchema.safeParse(expand(example.value))
      expect([example.name, parsed.success]).toEqual([example.name, true])
      if (!parsed.success) continue
      const survived = [...new Set(keysAtEveryDepth(parsed.data))].filter((key) => !declared.has(key))
      expect([example.name, survived]).toEqual([example.name, []])
      // And the example really did carry something to strip, wherever it sat —
      // otherwise this would pass by asserting nothing.
      const sent = [...new Set(keysAtEveryDepth(expand(example.value)))].filter((key) => !declared.has(key))
      expect([example.name, sent]).not.toEqual([example.name, []])
    }
  })

  it('reads a content kind it does not know as a placeholder, in both frames that carry one (ADR 7.4)', () => {
    // Reserving `content_kind` is only worth something if a v1 client meets a
    // future member as a placeholder rather than as a parse failure. The snapshot
    // frame matters most: every stream opens with one and every reconnect takes a
    // fresh one, so `slots[].content.content_kind` is the path a future kind
    // actually arrives on (RT-4).
    const slot = first(table, 'the table slot is now showing') as { slot: string; seq: number; content: Record<string, unknown> }
    const future = { ...slot.content, content_kind: 'excerpt' }
    // X-4: the placeholder still says which region to blank and which mark to advance.
    expect(parseTableEvent({ ...slot, content: future })).toEqual({
      kind: 'unknown',
      reason: 'unknown_kind',
      slot: slot.slot,
      seq: slot.seq,
      slots: null,
    })

    const snapshot = first(table, "a participant's opening picture") as { slots: Array<Record<string, unknown>> }
    const [tableSlot, mine] = snapshot.slots
    // …and one unreadable entry does not discard the readable table slot beside it.
    expect(parseTableEvent({ ...snapshot, slots: [tableSlot, { ...mine, content: future }] })).toEqual({
      kind: 'unknown',
      reason: 'unknown_kind',
      slot: null,
      seq: null,
      slots: [
        { kind: 'ok', value: tableSlot },
        { kind: 'unknown', reason: 'unknown_kind', slot: mine.slot, seq: mine.seq },
      ],
    })
    // …and the object-only paths are untouched: a known kind still reads as itself,
    // which holds by construction because none of them names the array segment.
    expect(parseTableEvent(snapshot).kind).toBe('ok')
    for (const example of gm.valid) expect(parseGmEvent(expand(example.value)).kind).toBe('ok')
    for (const paths of [RESULT_DISCRIMINATORS, ENTRY_DISCRIMINATORS, GM_EVENT_DISCRIMINATORS]) {
      for (const [path] of paths) expect(path).not.toContain('[]')
    }
  })

  it('reads a newer version, or a newer result kind inside a lane frame, as the future', () => {
    expect(parseGmEvent({ schema_version: 2, event: 'reconnect' })).toEqual({ kind: 'unknown', reason: 'newer_schema' })
    const lane = first(gm, 'a tool lane finished')
    const invocation = lane.invocation as Record<string, unknown>
    expect(parseGmEvent({ ...lane, invocation: { ...invocation, schema_version: 2 } })).toEqual({ kind: 'unknown', reason: 'newer_schema' })
    expect(parseGmEvent({ ...lane, invocation: { ...invocation, result: { result_kind: 'table', tool_id: 'npc' } } })).toEqual({
      kind: 'unknown',
      reason: 'unknown_kind',
    })
  })

  it('reads a snapshot frame by frame, so one unknown kind is one placeholder (ADR RT-4)', () => {
    const snapshot = readJson<Fixture>(join(FIXTURES, 'TableSnapshot.json')).valid[0].value as { frames: unknown[] }
    // `slot` was the unknown kind here until the reveal family landed; the probe
    // moves to one this contract does not define. The expectation is computed
    // from the fixture, so growing it cannot make this assertion quietly wrong.
    const known = snapshot.frames.slice(0, -1)
    const withUnknown = { schema_version: 1, frames: [...known, { schema_version: 1, event: 'excerpt', span: {} }, { schema_version: 1, event: 'ready' }] }
    const read = parseTableSnapshot(withUnknown)
    expect(read.kind).toBe('ok')
    if (read.kind !== 'ok') return
    expect(read.value.frames.map((frame) => frame.kind)).toEqual([...known.map(() => 'ok'), 'unknown', 'ok'])
    expect(read.value.frames[known.length]).toEqual({ kind: 'unknown', reason: 'unknown_kind', slot: null, seq: null, slots: null })
    // Without its ready boundary a snapshot is not one (TABLE-7).
    expect(parseTableSnapshot({ schema_version: 1, frames: snapshot.frames.slice(0, -1) })).toEqual({ kind: 'unknown', reason: 'invalid' })
    expect(parseGmSnapshot({ schema_version: 2, frames: [] })).toEqual({ kind: 'unknown', reason: 'newer_schema' })
    for (const junk of [null, 42, {}, { schema_version: 1, frames: [] }]) expect(parseGmSnapshot(junk).kind).toBe('unknown')
  })

  it('applies the one-reveal-picture rule where the readers are, not only where the emitter is (REVEAL-13)', () => {
    // A GM tab in RT-9's polling mode gets a GmSnapshot whose picture failed to
    // build; if parseGmSnapshot answered `ok`, the indicator would find no
    // `snapshot` frame and render "nothing revealed" while the table shows a
    // dossier — the one state REVEAL-13 forbids. Every invalid example that
    // breaks the rule is refused by the reader as well as by the schema.
    const byName = (doc: Fixture, name: string): unknown => {
      const found = doc.invalid.find((example) => example.name === name)
      if (!found) throw new Error(`no invalid example named ${name}`)
      return expand(found.value)
    }
    const gmSnapshots = readJson<Fixture>(join(FIXTURES, 'GmSnapshot.json'))
    const tableSnapshots = readJson<Fixture>(join(FIXTURES, 'TableSnapshot.json'))

    for (const name of ['a live session whose snapshot carries no reveal picture', 'two snapshot frames', 'a reveal picture with no session running']) {
      expect([name, parseGmSnapshot(byName(gmSnapshots, name))]).toEqual([name, { kind: 'unknown', reason: 'invalid' }])
    }
    for (const name of ['a live table whose snapshot carries no reveal picture', 'two snapshot frames', 'an inactive table carrying a reveal picture']) {
      expect([name, parseTableSnapshot(byName(tableSnapshots, name))]).toEqual([name, { kind: 'unknown', reason: 'invalid' }])
    }
    // Counted on the RAW event values: a picture this bundle cannot parse is
    // still a picture, so a live snapshot whose one picture is unreadable stays
    // `ok` with one placeholder in it, rather than reading as "no picture".
    const live = tableSnapshots.valid[0].value as { frames: Array<Record<string, unknown>> }
    const unreadable = live.frames.map((frame) => (frame.event === 'snapshot' ? { ...frame, slots: 'not a list' } : frame))
    const read = parseTableSnapshot({ schema_version: 1, frames: unreadable })
    expect(read.kind).toBe('ok')
    if (read.kind !== 'ok') return
    expect(read.value.frames.some((frame) => frame.kind === 'unknown')).toBe(true)
    // And every valid example still reads.
    for (const example of gmSnapshots.valid) expect([example.name, parseGmSnapshot(expand(example.value)).kind]).toEqual([example.name, 'ok'])
    for (const example of tableSnapshots.valid) expect([example.name, parseTableSnapshot(expand(example.value)).kind]).toEqual([example.name, 'ok'])
  })

  it('applies the liveness rule where the readers are, so a dead table has nothing left to show (REVEAL-17, AE-51)', () => {
    // One rule, three places that agree: the Pydantic model, the Zod schema and
    // the reader. `docs/workbench-wire-contract.md` tells a table client to
    // blank a slot it cannot read; a reader that answered `ok` with the
    // projection intact would hand it nothing to blank. Every shipped fixture
    // the liveness rule refuses is refused here too, on the raw values.
    const byName = (doc: Fixture, kind: 'valid' | 'invalid', name: string): unknown => {
      const found = doc[kind].find((example) => example.name === name)
      if (!found) throw new Error(`no ${kind} example named ${name}`)
      return expand(found.value)
    }
    const tableSnapshots = readJson<Fixture>(join(FIXTURES, 'TableSnapshot.json'))
    for (const name of [
      'an inactive table carrying a reveal picture',
      'an inactive table carrying a mine slot frame',
      'an inactive table carrying a slot frame with content',
      'an inactive table whose picture still holds a private projection',
      'an inactive frame after the session frame',
      'three inactive frames beside a session and a buffered projection',
      'an inactive table whose guest session still shows the table slot',
      'an inactive table that also carries a session frame',
    ]) {
      expect([name, parseTableSnapshot(byName(tableSnapshots, 'invalid', name))]).toEqual([name, { kind: 'unknown', reason: 'invalid' }])
    }
    // The permitted half still reads: a cleared region reports that it holds
    // nothing, which is not the same as showing something.
    const empty = byName(tableSnapshots, 'valid', 'an inactive table reporting an empty table slot')
    expect(parseTableSnapshot(empty).kind).toBe('ok')
    // `inactive` is decisive wherever it sits, so appending it to a resource the
    // reader would otherwise accept turns that resource dead — order is not a
    // fact the reader reads differently from the schema.
    const stillLive = tableSnapshots.valid[1].value as { frames: Array<Record<string, unknown>> }
    expect(parseTableSnapshot(stillLive).kind).toBe('ok')
    const soured = [...stillLive.frames.slice(0, -1), { schema_version: 1, event: 'inactive' }, { schema_version: 1, event: 'ready' }]
    expect(parseTableSnapshot({ schema_version: 1, frames: soured })).toEqual({ kind: 'unknown', reason: 'invalid' })
  })

  it('reports a broken frame as invalid and never throws', () => {
    // An audio slot name is not a reveal slot name, so only the sequence reads.
    const audio = first(table, 'ambience is playing')
    expect(parseTableEvent({ ...audio, slot: 'one_shot' })).toEqual({
      kind: 'unknown',
      reason: 'invalid',
      slot: null,
      seq: audio.seq,
      slots: null,
    })
    for (const junk of [null, undefined, 42, 'x', [], {}, { event: 7 }]) {
      expect(parseGmEvent(junk).kind).toBe('unknown')
      expect(parseTableEvent(junk).kind).toBe('unknown')
    }
  })

  it('keeps the slot and the sequence when a stale bundle cannot read the frame (X-4)', () => {
    // Not a future-version problem: the client checks a projection key against
    // its OWN copy of the field definitions, so a server one deploy ahead of a
    // table bundle — a type gained a revealable field, "no bump" by the
    // versioning table — makes the frame unreadable today. If the placeholder
    // lost the slot, the page would have nothing to blank, the natural
    // implementation would skip the frame, and document A would stay on the
    // player's screen while the GM's indicator says B.
    const ahead = { content_kind: 'document', type: 'lore', fields: [{ key: 'a_field_this_bundle_has_never_heard_of', value: 'x' }] }
    expect(parseTableEvent({ schema_version: 1, event: 'slot', slot: 'table', seq: 31, content: ahead })).toEqual({
      kind: 'unknown',
      reason: 'invalid',
      slot: 'table',
      seq: 31,
      slots: null,
    })
    // …and in a picture, the readable table slot survives its unreadable neighbour.
    const readable = { slot: 'table', seq: 12, content: null }
    expect(
      parseTableEvent({ schema_version: 1, event: 'snapshot', slots: [readable, { slot: 'mine', seq: 3, content: ahead }] }),
    ).toEqual({
      kind: 'unknown',
      reason: 'invalid',
      slot: null,
      seq: null,
      slots: [
        { kind: 'ok', value: readable },
        { kind: 'unknown', reason: 'invalid', slot: 'mine', seq: 3 },
      ],
    })
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

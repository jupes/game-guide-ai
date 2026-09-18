/**
 * Workbench wire contract, v1 (1kg.1.2) — the Zod half.
 *
 * It mirrors `service/workbench_contracts.py`, and both are pinned by the shared
 * fixtures in `contracts/workbench/v1`. The decisions behind the shapes are in
 * `docs/adr/gm-workbench-interactions.md`; conventions and forward-version rules
 * are in `docs/workbench-wire-contract.md`.
 *
 * The one deliberate difference from the server: **a client tolerates what a
 * newer server may add.** Response objects strip unknown fields instead of
 * rejecting them, and an error `code` is any well-formed string. Anything the
 * client cannot understand becomes a placeholder through the `parse*` readers
 * at the end of this file — never a crash, and never a guess (X-8, RAIL-24).
 * Requests are the opposite: the client builds them, so they are strict.
 *
 * Wire keys stay snake_case here. `adapters.ts` is the one place they meet the
 * design system's camelCase props.
 */

import { z } from 'zod'
import type { ZodType } from 'zod'
import {
  ChatModeSchema,
  SourceSchema,
  SpellContentSchema,
  StatBlockContentSchema,
  SuggestionSchema,
} from '../schemas'

export const CONTRACT_VERSION = 1

/** Decision RAIL-6, counted in code points after trimming. */
export const BRIEF_MAX_CHARS = 2000
/** Decision RAIL-8. */
export const SUGGESTION_BRIEF_MAX_CHARS = 200
export const PROSE_MAX_CHARS = 4000
export const MAX_SUGGESTIONS = 3
/** A ceiling on a stored prompt or answer, so a page has a bounded size. */
export const CHAT_TEXT_MAX_CHARS = 100_000
export const MAX_SOURCES = 50
export const TIMELINE_PAGE_MAX_ITEMS = 100

/** Ceilings per field kind; 1kg.5.3 may set tighter caps per type. */
export const TEXT_FIELD_MAX_CHARS = 200
export const PROSE_FIELD_MAX_CHARS = 20_000
export const LIST_FIELD_MAX_ITEMS = 100
export const LIST_ITEM_MAX_CHARS = 2000
export const MAX_CHANGED_FIELDS = 64
/** CANVAS-27 pages history by 20 and LIB-23 the library by 25; a page may hold up to 50. */
export const HISTORY_PAGE_MAX_ITEMS = 50
export const LIBRARY_PAGE_MAX_ITEMS = 50
/** Decision LIB-20. */
export const SEARCH_MIN_CHARS = 2
export const SEARCH_MAX_CHARS = 100
export const VERSION_NUMBER_MAX = 1_000_000

// ── Closed vocabularies (pinned by contracts/workbench/v1/registry.json) ─────

export const TOOL_IDS = [
  'npc', 'monster', 'loot', 'names', 'rules', 'portrait', 'encounter', 'hooks', 'recap', 'map',
] as const
export type ToolId = (typeof TOOL_IDS)[number]

export const RESULT_KINDS = ['card', 'document', 'media'] as const
export type ResultKind = (typeof RESULT_KINDS)[number]

/** A closed union. 1kg.4.3 adds loot, names, rules and hooks. */
export const CARD_KINDS = ['stat_block'] as const
export type CardKind = (typeof CARD_KINDS)[number]

export const DOCUMENT_TYPE_IDS = [
  'npc', 'statblock', 'handout', 'session-notes', 'quest-log', 'character-sheet', 'lore', 'encounter',
] as const
export type DocumentTypeId = (typeof DOCUMENT_TYPE_IDS)[number]

export const LIBRARY_CATEGORIES = ['npcs', 'bestiary', 'documents', 'session-log', 'cues'] as const
export type LibraryCategory = (typeof LIBRARY_CATEGORIES)[number]

/** `unknown` is deliberately absent: it is a client-only state (RAIL-21). */
export const INVOCATION_STATUSES = ['working', 'done', 'failed', 'cancelled'] as const
export type InvocationStatus = (typeof INVOCATION_STATUSES)[number]

export type BriefPolicy = 'required' | 'optional'

/** The attached-cue entry joins with the cue family. */
export const ENTRY_KINDS = ['chat', 'tool', 'edit', 'session_divider', 'opaque'] as const
export type EntryKind = (typeof ENTRY_KINDS)[number]

/** What a document field holds. A kind is a registry fact and never appears on
 * the wire; 1kg.5.3 adds kinds as it defines the types that need them. */
export const FIELD_KINDS = ['text', 'prose', 'text_list', 'asset'] as const
export type FieldKind = (typeof FIELD_KINDS)[number]

/** Decision AUD-1: one GM per campaign, and players cannot write. */
export const AUTHORS = ['gm', 'assistant'] as const
/** The SelectionBar's complete requests (CANVAS-23). */
export const EDIT_ACTIONS = ['rewrite', 'shorter', 'darker'] as const
/** Decision LIB-22. */
export const LIBRARY_SORTS = ['recent', 'name'] as const

export const SESSION_BOUNDARIES = ['start', 'end'] as const
export const OPAQUE_REASONS = ['newer_version', 'unreadable'] as const

/** The discriminators an edit carries. A newer server may add to any of them. */
export const EDIT_OUTCOMES = ['changed', 'no_change'] as const
export const EDIT_SCOPE_KINDS = ['document', 'field', 'selection'] as const
export const EDIT_INSTRUCTION_KINDS = ['text', 'action'] as const

/** The codes this client knows. A newer server may send others; see `isKnownErrorCode`. */
export const KNOWN_ERROR_CODES = [
  'validation_failed', 'unsupported_schema_version', 'brief_required', 'brief_too_long', 'unknown_tool',
  'tool_disabled', 'campaign_required', 'nothing_to_recap', 'not_found', 'forbidden', 'conflict',
  'cap_reached', 'throttled_user', 'throttled_daily', 'provider_failed', 'provider_timeout',
  'attempt_expired', 'backend_unavailable',
] as const
export type KnownErrorCode = (typeof KNOWN_ERROR_CODES)[number]

export function isKnownErrorCode(code: string): code is KnownErrorCode {
  return (KNOWN_ERROR_CODES as readonly string[]).includes(code)
}

// ── Registry facts the validators need ───────────────────────────────────────
// Not the registry: 1kg.3.1 owns the full catalogue and extends registry.json.

export const TOOL_RESULT_KIND: Record<ToolId, ResultKind> = {
  npc: 'document',
  monster: 'card',
  loot: 'card',
  names: 'card',
  rules: 'card',
  portrait: 'media',
  encounter: 'document',
  hooks: 'card',
  recap: 'document',
  map: 'media',
}

/** Decision RAIL-5: only `recap` may run without a brief in v1. */
export const BRIEF_POLICY: Record<ToolId, BriefPolicy> = {
  npc: 'required',
  monster: 'required',
  loot: 'required',
  names: 'required',
  rules: 'required',
  portrait: 'required',
  encounter: 'required',
  hooks: 'required',
  recap: 'optional',
  map: 'required',
}

export const TOOL_CREATES_DOC_TYPE: Partial<Record<ToolId, DocumentTypeId>> = {
  npc: 'npc',
  encounter: 'encounter',
  recap: 'session-notes',
}

export const TOOL_CARD_KIND: Partial<Record<ToolId, CardKind>> = { monster: 'stat_block' }

/** Decisions LIB-1 to LIB-6: membership is a registry fact, not a free field. */
export const DOC_TYPE_LIBRARY_CATEGORY: Record<DocumentTypeId, LibraryCategory> = {
  npc: 'npcs',
  statblock: 'bestiary',
  handout: 'documents',
  'session-notes': 'session-log',
  'quest-log': 'documents',
  'character-sheet': 'documents',
  lore: 'documents',
  encounter: 'documents',
}

/** Every document type has these. `name` is the title everywhere, and the one
 * field that cannot be empty (LIB-12). */
export const COMMON_FIELDS: Record<string, FieldKind> = {
  name: 'text',
  qualifier: 'text',
  tags: 'text_list',
}

/** A type's own fields. `npc` is the worked example; 1kg.5.3 owns all eight, and
 * until it declares a type's fields that type has the common ones only. Nothing
 * here says who may SEE a field: that is agent-forge-harness-1ir.1.2's decision. */
export const DOC_TYPE_FIELDS: Record<DocumentTypeId, Record<string, FieldKind>> = {
  npc: {
    portrait: 'asset',
    voice: 'text',
    tell: 'text',
    attitude: 'text',
    wants: 'prose',
    leverage: 'prose',
    if_attacked: 'prose',
    notes: 'prose',
  },
  statblock: {},
  handout: {},
  'session-notes': {},
  'quest-log': {},
  'character-sheet': {},
  lore: {},
  encounter: {},
}

/** The revision of each type's field definitions that this client understands. */
export const DOC_TYPE_VERSION: Record<DocumentTypeId, number> = {
  npc: 1,
  statblock: 1,
  handout: 1,
  'session-notes': 1,
  'quest-log': 1,
  'character-sheet': 1,
  lore: 1,
  encounter: 1,
}

// ── Building blocks ──────────────────────────────────────────────────────────

/** Characters as the server counts them. `'🎲'.length` is 2; this is 1. */
export function codePointLength(value: string): number {
  // Spreading iterates by code point, not by UTF-16 unit.
  return [...value].length
}

/** JSON allows the escape of a lone surrogate; UTF-8, the database and the
 * server do not. (`String.prototype.isWellFormed` says the same, but is ES2024.) */
export function isWellFormedText(value: string): boolean {
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0
    if (code >= 0xd800 && code <= 0xdfff) return false
  }
  return true
}

const WELL_FORMED = { message: 'must be well-formed Unicode text' }

/** What `String.prototype.trim` removes, by code point, so that the server can
 * trim exactly the same set (`trim` in workbench_contracts.py): ASCII whitespace,
 * the Unicode space separators, the line and paragraph separators and the byte
 * order mark. Python's `strip()` would also take NEL and the ASCII separators,
 * and leave the mark — and then one side finds a brief empty that the other
 * finds two characters long. */
const TRIMMED = new Set<number>([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff,
])

/** Trim as the server does. Every character in the set is one UTF-16 unit. */
export function trimWire(value: string): string {
  let start = 0
  let end = value.length
  while (start < end && TRIMMED.has(value.charCodeAt(start))) start += 1
  while (end > start && TRIMMED.has(value.charCodeAt(end - 1))) end -= 1
  return value.slice(start, end)
}

/** A well-formed string bounded in code points, so both sides agree on "2,000 characters". */
function text(min: number, max: number) {
  return z
    .string()
    .refine(isWellFormedText, WELL_FORMED)
    .refine(
      (value) => {
        const length = codePointLength(value)
        return length >= min && length <= max
      },
      { message: `must be ${min} to ${max} characters` },
    )
}

/** Opaque, fragment-safe and log-safe. Conversation ids are UUIDs today and fit. */
const OpaqueIdSchema = z.string().regex(/^[A-Za-z0-9_-]{1,64}$/)
/** Client-minted, and an invocation's idempotency key (RAIL-18). */
const InvocationIdSchema = z.string().regex(/^[A-Za-z0-9_-]{16,64}$/)
/** One grammar for every timestamp (CANVAS-27): seconds, at most microseconds, and
 * `Z` or a `+HH:MM` offset. The server carries the same pattern; `z.iso.datetime`
 * then checks that the moment exists. Formatting for people is the client's job. */
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/
export const TimestampSchema = z.iso.datetime({ offset: true }).regex(ISO_TIMESTAMP)
/** Opaque, and base64url because a cursor may ride in a query string. */
const CursorSchema = z.string().regex(/^[A-Za-z0-9_-]{1,512}$/)
/** Client-minted idempotency key of a mutation that has no invocation. */
const CommandIdSchema = z.string().regex(/^[A-Za-z0-9_-]{16,64}$/)
/** Decision CANVAS-19: a field is a top-level key of a type's data — one flat,
 * snake_case namespace, and the unit of concurrency, change lists, field scopes
 * and reveal masks. There are no paths into a field. */
const FieldKeySchema = z.string().regex(/^[a-z][a-z0-9_]{0,39}$/)
/** Decision CANVAS-34: the concurrency token, never a history version. */
const WriteRevisionSchema = z.number().int().min(1).max(Number.MAX_SAFE_INTEGER)
const VersionNumberSchema = z.number().int().min(1).max(VERSION_NUMBER_MAX)

/** CR, LF and the Unicode line and paragraph separators — by code point, so that
 * no invisible character ever sits in this file. */
const LINE_BREAKS = [0x0a, 0x0d, 0x2028, 0x2029].map((code) => String.fromCharCode(code))

/** One line of text, bounded in code points. */
function oneLine(min: number, max: number) {
  return text(min, max).refine((value) => !LINE_BREAKS.some((mark) => value.includes(mark)), {
    message: 'must be a single line',
  })
}

const ToolIdSchema = z.enum(TOOL_IDS)

// ── Errors ───────────────────────────────────────────────────────────────────

/** What moved, for a document write that lost a race (CANVAS-19, CANVAS-20). It
 * names the fields and the revision to rebase on and never carries their text:
 * an error body is where logs and traces look (X-7). The client reads the latest
 * values through the document endpoint, then offers Keep mine / Use latest. */
const ConflictInfoSchema = z.object({
  write_revision: WriteRevisionSchema,
  fields: z.array(FieldKeySchema).min(1).max(MAX_CHANGED_FIELDS),
})
export type ConflictInfo = z.infer<typeof ConflictInfoSchema>

export const ErrorInfoSchema = z.object({
  // Any well-formed code: a newer server may know more than this client does.
  code: z.string().regex(/^[a-z][a-z_]{1,39}$/),
  message: text(1, 500),
  retryable: z.boolean(),
  field: text(1, 64).nullish(),
  retry_after_s: z.number().int().min(0).max(86_400).nullish(),
  in_flight: z.array(InvocationIdSchema).max(8).nullish(),
  /** Only for `conflict` on a document write or an AI edit. */
  conflict: ConflictInfoSchema.nullish(),
})
export type ErrorInfo = z.infer<typeof ErrorInfoSchema>

/** The Workbench error envelope. It keeps FastAPI's `detail` key. */
export const ErrorBodySchema = z.object({ detail: ErrorInfoSchema })

// ── Tool invocation ──────────────────────────────────────────────────────────

/** Decisions RAIL-5 to RAIL-9. Strict: the client builds this, so a stray key is
 * a client bug — and there is no free-form `context` (RAIL-7). */
export const ToolInvocationRequestSchema = refusingProtoKeys(
  z
    .strictObject({
      schema_version: z.literal(CONTRACT_VERSION),
      invocation_id: InvocationIdSchema,
      tool_id: ToolIdSchema,
      brief: z.string().refine(isWellFormedText, WELL_FORMED),
      campaign_id: OpaqueIdSchema,
      conversation_id: OpaqueIdSchema,
      source_entry_id: OpaqueIdSchema.nullish(),
    })
    .refine((request) => codePointLength(trimWire(request.brief)) <= BRIEF_MAX_CHARS, {
      path: ['brief'],
      message: `a brief can be at most ${BRIEF_MAX_CHARS} characters`,
    })
    .refine((request) => trimWire(request.brief) !== '' || BRIEF_POLICY[request.tool_id] === 'optional', {
      path: ['brief'],
      message: 'this tool needs a brief',
    }),
)
export type ToolInvocationRequest = z.infer<typeof ToolInvocationRequestSchema>

/** Decision RAIL-8: a suggestion arms the composer; it never runs. */
export const ToolSuggestionSchema = z.object({
  tool_id: ToolIdSchema,
  label: text(1, 60),
  icon: z.string().regex(/^[a-z0-9_]{1,40}$/).nullish(),
  brief: text(1, SUGGESTION_BRIEF_MAX_CHARS).nullish(),
})
export type ToolSuggestion = z.infer<typeof ToolSuggestionSchema>

/** Enough to link and title a document, and nothing of its body. */
export const DocumentLinkSchema = z
  .object({
    document_id: OpaqueIdSchema,
    type: z.enum(DOCUMENT_TYPE_IDS),
    title: text(1, 200),
    library_category: z.enum(LIBRARY_CATEGORIES),
  })
  .refine((link) => DOC_TYPE_LIBRARY_CATEGORY[link.type] === link.library_category, {
    path: ['library_category'],
    message: 'the category does not belong to the document type',
  })
export type DocumentLink = z.infer<typeof DocumentLinkSchema>

const assetRefShape = {
  asset_id: OpaqueIdSchema,
  media_type: z.literal('image'),
  alt: text(1, 300),
  width: z.number().int().min(1).max(20_000).nullish(),
  height: z.number().int().min(1).max(20_000).nullish(),
}

/** No URL, ever (X-10): the client builds a same-origin URL from the id. */
export const AssetRefSchema = z.object(assetRefShape)
export type AssetRef = z.infer<typeof AssetRefSchema>
/** In a request the client builds the reference, so a stray key — a URL — is an error. */
const StrictAssetRefSchema = z.strictObject(assetRefShape)

const StatBlockCardSchema = z.object({
  card_kind: z.literal('stat_block'),
  // The existing /chat stat-block contract, reused rather than re-declared.
  stat_block: StatBlockContentSchema,
})

const CardContentSchema = z.discriminatedUnion('card_kind', [StatBlockCardSchema])

const resultBase = {
  tool_id: ToolIdSchema,
  prose: text(0, PROSE_MAX_CHARS),
  suggestions: z.array(ToolSuggestionSchema).max(MAX_SUGGESTIONS),
}

const CardResultSchema = z
  .object({ result_kind: z.literal('card'), ...resultBase, card: CardContentSchema })
  .refine((result) => TOOL_RESULT_KIND[result.tool_id] === 'card', {
    path: ['result_kind'],
    message: 'this tool does not produce a card',
  })
  .refine((result) => TOOL_CARD_KIND[result.tool_id] === result.card.card_kind, {
    path: ['card', 'card_kind'],
    message: 'this tool has no card of that kind',
  })

const DocumentResultSchema = z
  .object({ result_kind: z.literal('document'), ...resultBase, document: DocumentLinkSchema })
  .refine((result) => TOOL_RESULT_KIND[result.tool_id] === 'document', {
    path: ['result_kind'],
    message: 'this tool does not produce a document',
  })
  .refine((result) => TOOL_CREATES_DOC_TYPE[result.tool_id] === result.document.type, {
    path: ['document', 'type'],
    message: 'this tool creates a different document type',
  })

const MediaResultSchema = z
  .object({ result_kind: z.literal('media'), ...resultBase, asset: AssetRefSchema })
  .refine((result) => TOOL_RESULT_KIND[result.tool_id] === 'media', {
    path: ['result_kind'],
    message: 'this tool does not produce media',
  })

/** "Size decides placement": the discriminator is the routing rule. */
export const ToolResultSchema = z.discriminatedUnion('result_kind', [
  CardResultSchema,
  DocumentResultSchema,
  MediaResultSchema,
])
export type ToolResult = z.infer<typeof ToolResultSchema>

const PAYLOAD_FOR_STATUS: Record<InvocationStatus, { result: boolean; error: boolean }> = {
  working: { result: false, error: false },
  done: { result: true, error: false },
  failed: { result: false, error: true },
  cancelled: { result: false, error: false },
}

/** The status resource behind a lane (RAIL-15 to RAIL-27). */
export const ToolInvocationSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    invocation_id: InvocationIdSchema,
    tool_id: ToolIdSchema,
    status: z.enum(INVOCATION_STATUSES),
    attempt: z.number().int().min(1).max(100),
    /** With `status: 'done'` this is "finished before it could be cancelled" (RAIL-23). */
    cancel_requested: z.boolean(),
    created_at: TimestampSchema,
    updated_at: TimestampSchema,
    result: ToolResultSchema.nullable(),
    error: ErrorInfoSchema.nullable(),
  })
  .refine(
    (invocation) => {
      const wanted = PAYLOAD_FOR_STATUS[invocation.status]
      return (invocation.result !== null) === wanted.result && (invocation.error !== null) === wanted.error
    },
    { path: ['status'], message: 'status and payload disagree' },
  )
  .refine((invocation) => invocation.result === null || invocation.result.tool_id === invocation.tool_id, {
    path: ['result', 'tool_id'],
    message: 'the result belongs to another tool',
  })
export type ToolInvocation = z.infer<typeof ToolInvocationSchema>

// ── Documents ────────────────────────────────────────────────────────────────
// A document's `data` is flat: one value per field key. Values are bare JSON;
// what each key must hold is the type's definition (DOC_TYPE_FIELDS). The server
// rejects a key the type does not declare. This client strips one instead, which
// is what lets a type gain fields without a version bump — except in a request,
// which the client builds itself, so there a stray key is a bug.

export type FieldValue = string | string[] | AssetRef | null
export type DocumentFields = Record<string, FieldValue>

/** Text and prose clear to `''`, a list to `[]`, and only an asset to `null`. */
function fieldValueSchema(kind: FieldKind, strict: boolean): ZodType<FieldValue> {
  switch (kind) {
    case 'text':
      return oneLine(0, TEXT_FIELD_MAX_CHARS)
    case 'prose':
      return text(0, PROSE_FIELD_MAX_CHARS)
    case 'text_list':
      return z.array(text(1, LIST_ITEM_MAX_CHARS)).max(LIST_FIELD_MAX_ITEMS)
    case 'asset':
      return (strict ? StrictAssetRefSchema : AssetRefSchema).nullable()
  }
}

interface TypedFields {
  type: DocumentTypeId
  type_version: number
}

/**
 * Check field values against a type's definition, failing closed, and return
 * only what this client understands. `whole` is a complete document, which must
 * have a name; otherwise the fields are a patch. Lookups use `Object.hasOwn`: a
 * key named `constructor` or `__proto__` must read as "not declared", not find
 * something on a prototype.
 */
function readFields(
  typed: TypedFields,
  raw: Record<string, unknown>,
  at: string,
  options: { whole: boolean; strict: boolean },
  ctx: z.RefinementCtx,
): DocumentFields {
  if (typed.type_version !== DOC_TYPE_VERSION[typed.type]) {
    ctx.addIssue({ code: 'custom', path: ['type_version'], message: 'unknown version of the field definitions for this type' })
  }
  const declared = { ...COMMON_FIELDS, ...DOC_TYPE_FIELDS[typed.type] }
  const fields: DocumentFields = {}
  for (const [key, value] of Object.entries(raw)) {
    if (!Object.hasOwn(declared, key)) {
      if (options.strict) ctx.addIssue({ code: 'custom', path: [at, key], message: 'this type does not declare that field' })
      continue
    }
    const parsed = fieldValueSchema(declared[key], options.strict).safeParse(value)
    if (parsed.success) fields[key] = parsed.data
    else for (const issue of parsed.error.issues) ctx.addIssue({ code: 'custom', path: [at, key, ...issue.path], message: issue.message })
  }
  const name = Object.hasOwn(fields, 'name') ? fields.name : undefined
  if ((options.whole && name === undefined) || (typeof name === 'string' && trimWire(name) === '')) {
    ctx.addIssue({ code: 'custom', path: [at, 'name'], message: 'a document has a name, and it cannot be blank' })
  }
  return fields
}

const typedShape = {
  type: z.enum(DOCUMENT_TYPE_IDS),
  type_version: z.number().int().min(1).max(1000),
}
const rawFields = z.record(z.string(), z.unknown())

/** The path of the first own key named `__proto__` anywhere in a value. */
function protoKeyPath(value: unknown, path: PropertyKey[] = [], depth = 0): PropertyKey[] | null {
  if (depth > 32) return null
  if (Array.isArray(value)) {
    for (const [index, item] of value.entries()) {
      const found = protoKeyPath(item, [...path, index], depth + 1)
      if (found) return found
    }
    return null
  }
  if (!isRecord(value)) return null
  if (Object.hasOwn(value, '__proto__')) return [...path, '__proto__']
  for (const [key, item] of Object.entries(value)) {
    const found = protoKeyPath(item, [...path, key], depth + 1)
    if (found) return found
  }
  return null
}

/** For a request. `JSON.parse` makes `__proto__` an ordinary own key, which Zod's
 * object and record parsers leave out rather than read — so a stray key the
 * server refuses as undeclared would otherwise vanish on the client instead of
 * failing here, where the client can still say what was wrong. */
function refusingProtoKeys<T extends ZodType>(schema: T) {
  return z.preprocess((input, ctx) => {
    const path = protoKeyPath(input)
    if (path) ctx.addIssue({ code: 'custom', path, message: 'a key named __proto__ is never a field' })
    return input
  }, schema)
}

/** One row of a document's history (CANVAS-27). The handoff's `label` and display
 * `time` are not on the wire; this client derives both. */
export const DocumentVersionSchema = z
  .object({
    number: VersionNumberSchema,
    author: z.enum(AUTHORS),
    summary: oneLine(0, TEXT_FIELD_MAX_CHARS),
    created_at: TimestampSchema,
    /** CANVAS-34: only a sealed version may be pinned by a reveal or exported. */
    sealed: z.boolean(),
    /** Against the version before it. It survives the gold wash (CANVAS-29). */
    changed_fields: z.array(FieldKeySchema).max(MAX_CHANGED_FIELDS),
    /** CANVAS-26: a restore appends a version equal to an earlier one. */
    restored_from: VersionNumberSchema.nullable(),
  })
  .refine((version) => version.restored_from === null || version.restored_from < version.number, {
    path: ['restored_from'],
    message: 'a version can only be restored from an earlier one',
  })
export type DocumentVersion = z.infer<typeof DocumentVersionSchema>

/** The GM-side read of a document, and the answer to every document write. It
 * carries its current version only, and nothing about reveal (CANVAS-33). */
export const DocumentSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    document_id: OpaqueIdSchema,
    campaign_id: OpaqueIdSchema,
    ...typedShape,
    data: rawFields,
    write_revision: WriteRevisionSchema,
    version: DocumentVersionSchema,
    /** Decision LIB-16. */
    archived: z.boolean(),
    created_at: TimestampSchema,
    updated_at: TimestampSchema,
  })
  .transform((doc, ctx) => ({ ...doc, data: readFields(doc, doc.data, 'data', { whole: true, strict: false }, ctx) }))
export type Document = z.infer<typeof DocumentSchema>

/** The content of one version. A read of history, so it has no write revision. */
export const DocumentVersionSnapshotSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    document_id: OpaqueIdSchema,
    ...typedShape,
    version: DocumentVersionSchema,
    data: rawFields,
  })
  .transform((doc, ctx) => ({ ...doc, data: readFields(doc, doc.data, 'data', { whole: true, strict: false }, ctx) }))
export type DocumentVersionSnapshot = z.infer<typeof DocumentVersionSnapshotSchema>

/** Newest first (CANVAS-27). */
export const DocumentHistoryPageSchema = z.object({
  schema_version: z.literal(CONTRACT_VERSION),
  document_id: OpaqueIdSchema,
  items: z.array(DocumentVersionSchema).max(HISTORY_PAGE_MAX_ITEMS),
  next_cursor: CursorSchema.nullable(),
})
export type DocumentHistoryPage = z.infer<typeof DocumentHistoryPageSchema>

/** CANVAS-10: one autosave. The author is always the GM and is never the client's
 * to state. Answered with a Document, or a 409 whose `conflict` names what moved. */
export const FieldPatchRequestSchema = refusingProtoKeys(
  z
    .strictObject({
      schema_version: z.literal(CONTRACT_VERSION),
      ...typedShape,
      base_write_revision: WriteRevisionSchema,
      fields: rawFields,
    })
    .refine((patch) => Object.keys(patch.fields).length >= 1 && Object.keys(patch.fields).length <= MAX_CHANGED_FIELDS, {
      path: ['fields'],
      message: 'a patch touches at least one field',
    })
    .transform((patch, ctx) => ({ ...patch, fields: readFields(patch, patch.fields, 'fields', { whole: false, strict: true }, ctx) })),
)
export type FieldPatchRequest = z.infer<typeof FieldPatchRequestSchema>

/** LIB-12: New in a library category. `command_id` makes a retry open the
 * document already made instead of making a second one. */
export const DocumentCreateRequestSchema = refusingProtoKeys(
  z
    .strictObject({
      schema_version: z.literal(CONTRACT_VERSION),
      command_id: CommandIdSchema,
      campaign_id: OpaqueIdSchema,
      ...typedShape,
      data: rawFields,
    })
    .transform((request, ctx) => ({ ...request, data: readFields(request, request.data, 'data', { whole: true, strict: true }, ctx) })),
)
export type DocumentCreateRequest = z.infer<typeof DocumentCreateRequestSchema>

/** CANVAS-26. Additive, so it needs no base revision, and naturally idempotent. */
export const RestoreRequestSchema = refusingProtoKeys(
  z.strictObject({
    schema_version: z.literal(CONTRACT_VERSION),
    version_number: VersionNumberSchema,
  }),
)
export type RestoreRequest = z.infer<typeof RestoreRequestSchema>

// ── AI edits ─────────────────────────────────────────────────────────────────

const documentScopeShape = { kind: z.literal('document') }
const fieldScopeShape = { kind: z.literal('field'), field: FieldKeySchema }
/** What the thread keeps of a selection: which field, never the text (EXPORT-12). */
const selectionSummaryShape = { kind: z.literal('selection'), field: FieldKeySchema }
/** A span of one field in CODE POINTS — not UTF-16 units, so convert before
 * sending — with the exact text the GM saw. The server refuses a span that no
 * longer matches before any provider work (1kg.5.5). */
const selectionScopeShape = {
  ...selectionSummaryShape,
  start: z.number().int().min(0).max(PROSE_FIELD_MAX_CHARS),
  end: z.number().int().min(1).max(PROSE_FIELD_MAX_CHARS),
  text: text(1, PROSE_FIELD_MAX_CHARS),
}

const EditScopeSchema = z.discriminatedUnion('kind', [
  z.strictObject(documentScopeShape),
  z.strictObject(fieldScopeShape),
  z.strictObject(selectionScopeShape).refine((scope) => scope.end - scope.start === codePointLength(scope.text), {
    path: ['text'],
    message: 'the selected text is not as long as its span',
  }),
])
export type EditScope = z.infer<typeof EditScopeSchema>

const EditScopeSummarySchema = z.discriminatedUnion('kind', [
  z.object(documentScopeShape),
  z.object(fieldScopeShape),
  z.object(selectionSummaryShape),
])

/** RAIL-6: an instruction shares the brief's bound, counted after trimming. */
const instructionText = z
  .string()
  .refine(isWellFormedText, WELL_FORMED)
  .refine(
    (value) => {
      const length = codePointLength(trimWire(value))
      return length >= 1 && length <= BRIEF_MAX_CHARS
    },
    { message: `an instruction is 1 to ${BRIEF_MAX_CHARS} characters` },
  )
const textInstructionShape = { kind: z.literal('text'), text: instructionText }
const actionInstructionShape = { kind: z.literal('action'), action: z.enum(EDIT_ACTIONS) }

const StrictEditInstructionSchema = z.discriminatedUnion('kind', [
  z.strictObject(textInstructionShape),
  z.strictObject(actionInstructionShape),
])
const EditInstructionSchema = z.discriminatedUnion('kind', [z.object(textInstructionShape), z.object(actionInstructionShape)])
export type EditInstruction = z.infer<typeof EditInstructionSchema>

/** CANVAS-23: this is what stops a one-line fix rewriting the dossier. */
const actionNeedsSelection = {
  check: (edit: { scope: { kind: string }; instruction: { kind: string } }) =>
    edit.instruction.kind !== 'action' || edit.scope.kind === 'selection',
  issue: { path: ['instruction', 'action'], message: 'a SelectionBar action is scoped to a selection' },
}

/** CANVAS-21 to CANVAS-25. The same lifecycle and cap as a tool (X-5). After a
 * conflict, Try again re-sends the same `invocation_id` with a fresh base. */
export const EditRequestSchema = refusingProtoKeys(
  z
    .strictObject({
      schema_version: z.literal(CONTRACT_VERSION),
      invocation_id: InvocationIdSchema,
      campaign_id: OpaqueIdSchema,
      conversation_id: OpaqueIdSchema,
      document_id: OpaqueIdSchema,
      base_write_revision: WriteRevisionSchema,
      scope: EditScopeSchema,
      instruction: StrictEditInstructionSchema,
    })
    .refine(actionNeedsSelection.check, actionNeedsSelection.issue),
)
export type EditRequest = z.infer<typeof EditRequestSchema>

const editResultBase = {
  prose: text(0, PROSE_MAX_CHARS),
  suggestions: z.array(ToolSuggestionSchema).max(MAX_SUGGESTIONS),
}

/** A conflict is a FAILED invocation, not an outcome. */
const EditResultSchema = z.discriminatedUnion('outcome', [
  z.object({
    outcome: z.literal('changed'),
    ...editResultBase,
    /** The lane's badge reads `EDIT · v<n>` (CANVAS-24). */
    version_number: VersionNumberSchema,
    write_revision: WriteRevisionSchema,
    /** What to wash gold (CANVAS-28). */
    changed_fields: z.array(FieldKeySchema).min(1).max(MAX_CHANGED_FIELDS),
  }),
  /** CANVAS-25: no version is created, so there is none to name. */
  z.object({ outcome: z.literal('no_change'), ...editResultBase }),
])
export type EditResult = z.infer<typeof EditResultSchema>

/** The status resource behind an edit lane. It never carries the document. */
export const EditInvocationSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    invocation_id: InvocationIdSchema,
    document_id: OpaqueIdSchema,
    status: z.enum(INVOCATION_STATUSES),
    attempt: z.number().int().min(1).max(100),
    cancel_requested: z.boolean(),
    created_at: TimestampSchema,
    updated_at: TimestampSchema,
    result: EditResultSchema.nullable(),
    error: ErrorInfoSchema.nullable(),
  })
  .refine(
    (invocation) => {
      const wanted = PAYLOAD_FOR_STATUS[invocation.status]
      return (invocation.result !== null) === wanted.result && (invocation.error !== null) === wanted.error
    },
    { path: ['status'], message: 'status and payload disagree' },
  )
export type EditInvocation = z.infer<typeof EditInvocationSchema>

// ── Campaign Library ─────────────────────────────────────────────────────────

/** LIB-5: cues are not documents; their listing belongs to the cue family. */
const DocumentCategorySchema = z.enum(LIBRARY_CATEGORIES).refine((category) => category !== 'cues', {
  message: 'cues are not documents',
})

/** LIB-20 to LIB-23. A request BODY even without a search: search text may never
 * travel in a URL (X-7), and one shape is simpler than two. */
export const LibraryQuerySchema = refusingProtoKeys(
  z
    .strictObject({
      schema_version: z.literal(CONTRACT_VERSION),
      campaign_id: OpaqueIdSchema,
      category: DocumentCategorySchema,
      /** Empty for no search; otherwise 2 to 100 characters after trimming. */
      search: z
        .string()
        .refine(isWellFormedText, WELL_FORMED)
        .refine(
          (value) => {
            const length = codePointLength(trimWire(value))
            return length === 0 || (length >= SEARCH_MIN_CHARS && length <= SEARCH_MAX_CHARS)
          },
          { message: `a search is ${SEARCH_MIN_CHARS} to ${SEARCH_MAX_CHARS} characters` },
        ),
      sort: z.enum(LIBRARY_SORTS),
      archived: z.boolean(),
      /** Only in Documents, the one category that holds more than one type (LIB-22). */
      type: z.enum(DOCUMENT_TYPE_IDS).nullish(),
      cursor: CursorSchema.nullish(),
      limit: z.number().int().min(1).max(LIBRARY_PAGE_MAX_ITEMS).nullish(),
    })
    .refine((query) => query.type == null || (query.category === 'documents' && DOC_TYPE_LIBRARY_CATEGORY[query.type] === 'documents'), {
      path: ['type'],
      message: 'only Documents can be filtered by type, and only by a type that lives there',
    }),
)
export type LibraryQuery = z.infer<typeof LibraryQuerySchema>

/** Enough to list, match and open a document, and nothing of its body. */
const LibraryItemSchema = z.object({
  document_id: OpaqueIdSchema,
  type: z.enum(DOCUMENT_TYPE_IDS),
  title: oneLine(1, TEXT_FIELD_MAX_CHARS),
  qualifier: oneLine(0, TEXT_FIELD_MAX_CHARS),
  tags: z.array(text(1, LIST_ITEM_MAX_CHARS)).max(LIST_FIELD_MAX_ITEMS),
  archived: z.boolean(),
  updated_at: TimestampSchema,
})
export type LibraryItem = z.infer<typeof LibraryItemSchema>

/** It echoes the campaign and category it answers, so that a response for a
 * campaign the GM has left is dropped and a stale row never flashes (LIB-25). */
export const LibraryPageSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    campaign_id: OpaqueIdSchema,
    category: DocumentCategorySchema,
    items: z.array(LibraryItemSchema).max(LIBRARY_PAGE_MAX_ITEMS),
    next_cursor: CursorSchema.nullable(),
  })
  .refine((page) => page.items.every((item) => DOC_TYPE_LIBRARY_CATEGORY[item.type] === page.category), {
    path: ['items'],
    message: 'a row does not belong in this category',
  })
export type LibraryPage = z.infer<typeof LibraryPageSchema>

// ── Timeline ─────────────────────────────────────────────────────────────────
// One entry per EXCHANGE: a turn carries its own outcome. A page boundary can
// therefore never separate a prompt from its result (1kg.4.2), results sit
// beneath the turn that asked for them however late they finish (RAIL-16), and
// the shape matches what useChat already keeps as an `Exchange`.

/** Mirrors service.models.RoutingInfo: which model answered (b8o.2). */
const RoutingInfoSchema = z.object({
  requested: z.string(),
  effective: z.string(),
  provider: z.string(),
  strategy: z.enum(['auto', 'manual']),
  task_class: z.string().nullish(),
  reason: z.string().nullish(),
  fallback_from: z.string().nullish(),
})

/** Mirrors service.models.SuggestionsRoutingInfo. */
const SuggestionsRoutingInfoSchema = z.object({
  effective: z.string(),
  provider: z.string(),
  reason: z.string().nullish(),
  fallback_from: z.string().nullish(),
})

/** A complete assistant outcome, built from the existing /chat pieces so that
 * what xiu.5.2 adds to them round-trips here instead of growing a second shape. */
const ChatAnswerSchema = z.object({
  /** Markdown, rendered without remote subresources (X-10). */
  text: text(0, CHAT_TEXT_MAX_CHARS),
  /** `null` is "not recorded" — a row older than the durable timeline. The key
   * is required, so absence can never be mistaken for `true`. */
  answerable: z.boolean().nullable(),
  /** `null` is "not recorded"; the empty list is "recorded, and none". */
  sources: z.array(SourceSchema).max(MAX_SOURCES).nullable(),
  created_at: TimestampSchema,
  suggestions: z.array(SuggestionSchema).max(MAX_SUGGESTIONS).nullish(),
  routing: RoutingInfoSchema.nullish(),
  suggestions_routing: SuggestionsRoutingInfoSchema.nullish(),
  spell_content: SpellContentSchema.nullish(),
  stat_block: StatBlockContentSchema.nullish(),
})
export type ChatAnswer = z.infer<typeof ChatAnswerSchema>

const entryBase = {
  /** Per entry, not only per page: entries are stored one by one. */
  schema_version: z.literal(CONTRACT_VERSION),
  entry_id: OpaqueIdSchema,
  /** When the turn was made. Order is the page's, never re-derived from this. */
  created_at: TimestampSchema,
}

/** A plain message and its answer, in any mode (RAIL-14). */
const ChatEntrySchema = z
  .object({
    ...entryBase,
    entry_kind: z.literal('chat'),
    mode: ChatModeSchema,
    /** `null` only for an old answer whose prompt was never recorded. */
    prompt: text(1, CHAT_TEXT_MAX_CHARS).nullable(),
    /** `null` while the turn has no stored answer: it failed, or is still running. */
    answer: ChatAnswerSchema.nullable(),
  })
  .refine((entry) => entry.prompt !== null || entry.answer !== null, {
    path: ['answer'],
    message: 'a chat entry needs a prompt or an answer',
  })

/** Decision RAIL-9: a tool and a brief, never the slash string. The tool is
 * `invocation.tool_id`, kept in one place so the turn and its lane cannot disagree. */
const ToolEntrySchema = z.object({
  ...entryBase,
  entry_kind: z.literal('tool'),
  /** Bounded on the way back too (RAIL-6); the brief *policy* is not re-judged. */
  brief: text(0, BRIEF_MAX_CHARS),
  /** The entry whose suggestion armed this turn (RAIL-8). */
  source_entry_id: OpaqueIdSchema.nullish(),
  invocation: ToolInvocationSchema,
})

/** An AI edit and its outcome. The GM's words are the turn, as a brief is for a
 * tool; the thread keeps the scope's kind and field, and never the selected text
 * or the document (EXPORT-12). */
const EditEntrySchema = z
  .object({
    ...entryBase,
    entry_kind: z.literal('edit'),
    /** The title as it was when the edit was asked for. */
    document: DocumentLinkSchema,
    scope: EditScopeSummarySchema,
    instruction: EditInstructionSchema,
    invocation: EditInvocationSchema,
  })
  .refine((entry) => entry.document.document_id === entry.invocation.document_id, {
    path: ['invocation', 'document_id'],
    message: 'the edit touched another document than the one the entry links',
  })
  .refine(actionNeedsSelection.check, actionNeedsSelection.issue)

/** The only thing that separates prep from play; `/recap` reads from the latest `start`. */
const SessionDividerEntrySchema = z.object({
  ...entryBase,
  entry_kind: z.literal('session_divider'),
  session_id: OpaqueIdSchema,
  boundary: z.enum(SESSION_BOUNDARIES),
})

/** Stands in for a stored entry the server could not read. It carries nothing of it. */
const OpaqueEntrySchema = z.object({
  ...entryBase,
  entry_kind: z.literal('opaque'),
  reason: z.enum(OPAQUE_REASONS),
})

export const TimelineEntrySchema = z.discriminatedUnion('entry_kind', [
  ChatEntrySchema,
  ToolEntrySchema,
  EditEntrySchema,
  SessionDividerEntrySchema,
  OpaqueEntrySchema,
])
export type TimelineEntry = z.infer<typeof TimelineEntrySchema>

const pageEnvelope = {
  schema_version: z.literal(CONTRACT_VERSION),
  conversation_id: OpaqueIdSchema,
  /** Required: the end of the list is `null`, never a missing key. */
  next_cursor: CursorSchema.nullable(),
}

/** The strict page. Items run NEWEST FIRST and `next_cursor` leads to older
 * entries. Components read a page through `parseTimelinePage`, never this. */
export const TimelinePageSchema = z.object({
  ...pageEnvelope,
  items: z.array(TimelineEntrySchema).max(TIMELINE_PAGE_MAX_ITEMS),
})
export type TimelinePage = z.infer<typeof TimelinePageSchema>

// ── Media assets and cues ────────────────────────────────────────────────────
// Bytes travel in two steps (docs/adr/gm-workbench-media-and-realtime.md, MS-3 to
// MS-5): a JSON request creates the asset and reserves its declared size, then
// one raw request — Content-Type the declared type, Content-Length required —
// carries the bytes. The caps are the threat model's (SEC-26).

export const IMAGE_MAX_BYTES = 10_000_000
export const AUDIO_MAX_BYTES = 20_000_000
export const IMAGE_MAX_SIDE = 8192
export const IMAGE_MAX_PIXELS = 25_000_000
export const AMBIENCE_MAX_MS = 600_000
export const ONE_SHOT_MAX_MS = 30_000
export const ALT_MAX_CHARS = 300
export const CUE_TITLE_MAX_CHARS = 200
export const CUE_PAGE_MAX_ITEMS = 50
export const PRESENCE_MAX_PARTICIPANTS = 100
/** `secrets.token_urlsafe(32)`: 32 CSPRNG bytes are 43 base64url characters (SEC-5). */
export const TABLE_SECRET_CHARS = 43

export const ASSET_KINDS = ['image', 'audio'] as const
export type AssetKind = (typeof ASSET_KINDS)[number]
/** ADR MS-3. `deleted` is a tombstone and is never served. */
export const ASSET_STATES = ['uploading', 'processing', 'ready', 'failed'] as const
/** Closed and free of user text, so a reason is safe as a metric label. */
export const ASSET_FAILURES = [
  'unsupported_type', 'too_large', 'too_many_pixels', 'too_long', 'unreadable', 'timed_out', 'quota_exceeded',
] as const
/** What a kind accepts, judged by magic bytes on upload (SEC-25); processed audio is always `audio/mpeg`. */
export const MEDIA_TYPES: Record<AssetKind, readonly string[]> = {
  image: ['image/png', 'image/jpeg', 'image/webp'],
  audio: ['audio/mpeg', 'audio/mp4', 'audio/ogg', 'audio/wav'],
}
const ASSET_MAX_BYTES: Record<AssetKind, number> = { image: IMAGE_MAX_BYTES, audio: AUDIO_MAX_BYTES }
/** AUDIO-1: one ambience slot, one one-shot slot; a cue's kind is immutable (AUDIO-4). */
export const CUE_KINDS = ['ambience', 'one_shot'] as const
export type CueKind = (typeof CUE_KINDS)[number]
export const AUDIO_SLOTS = ['ambience', 'one_shot'] as const
const CUE_MAX_MS: Record<CueKind, number> = { ambience: AMBIENCE_MAX_MS, one_shot: ONE_SHOT_MAX_MS }
export const TABLE_ROLES = ['participant', 'guest'] as const
export const JOIN_STATUSES = ['joined', 'full', 'inactive'] as const
export const ENROL_STATUSES = ['enrolled', 'inactive'] as const
export const SESSION_STATES = ['live', 'ended'] as const
export const SESSION_ACTIONS = ['start', 'end', 'rotate'] as const
/** AUDIO-21, AUDIO-22: listening means playing and unmuted. */
export const PRESENCE_AUDIO = ['listening', 'muted', 'pending', 'absent'] as const
export const GM_EVENT_KINDS = ['tool_lane', 'edit_lane', 'session', 'audio', 'presence', 'asset', 'ready', 'reconnect'] as const
export const TABLE_EVENT_KINDS = ['session', 'inactive', 'audio', 'ready', 'reconnect'] as const

const MediaTypeSchema = z.string().regex(/^(image|audio)\/[a-z0-9.+-]{1,32}$/)
const AltTextSchema = oneLine(1, ALT_MAX_CHARS)
const PixelsSchema = z.number().int().min(1).max(IMAGE_MAX_SIDE)
const DurationMsSchema = z.number().int().min(1).max(AMBIENCE_MAX_MS)
const CueTitleSchema = oneLine(1, CUE_TITLE_MAX_CHARS)
const mediaTypeFits = (kind: AssetKind, mediaType: string) => MEDIA_TYPES[kind].includes(mediaType)
/** An image needs alt text; an audio asset carries no text at all — its title is the cue's (AUDIO-29). */
const altFits = (kind: AssetKind, alt: string | null | undefined) => (kind === 'image') === (alt != null)
const MEDIA_TYPE_ISSUE = { path: ['media_type'], message: 'not a type this kind accepts' }
const ALT_ISSUE = { path: ['alt'], message: 'an image has alt text and an audio asset has none' }

/** Step one of an upload: what is coming, so the quota and the caps are checked before a byte (ADR MS-4). */
export const AssetCreateRequestSchema = refusingProtoKeys(
  z
    .strictObject({
      schema_version: z.literal(CONTRACT_VERSION),
      command_id: CommandIdSchema,
      campaign_id: OpaqueIdSchema,
      kind: z.enum(ASSET_KINDS),
      media_type: MediaTypeSchema,
      size_bytes: z.number().int().min(1).max(AUDIO_MAX_BYTES),
      alt: AltTextSchema.nullish(),
    })
    .refine((request) => mediaTypeFits(request.kind, request.media_type), MEDIA_TYPE_ISSUE)
    .refine((request) => altFits(request.kind, request.alt), ALT_ISSUE)
    .refine((request) => request.size_bytes <= ASSET_MAX_BYTES[request.kind], {
      path: ['size_bytes'],
      message: 'over the cap for its kind',
    }),
)
export type AssetCreateRequest = z.infer<typeof AssetCreateRequestSchema>

/** The GM-side asset resource. Dimensions and duration exist only once the bytes
 * are processed; a failure carries a closed reason and never a message. */
export const AssetSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    asset_id: OpaqueIdSchema,
    campaign_id: OpaqueIdSchema,
    kind: z.enum(ASSET_KINDS),
    state: z.enum(ASSET_STATES),
    media_type: MediaTypeSchema,
    size_bytes: z.number().int().min(1).max(AUDIO_MAX_BYTES),
    width: PixelsSchema.nullable(),
    height: PixelsSchema.nullable(),
    duration_ms: DurationMsSchema.nullable(),
    alt: AltTextSchema.nullable(),
    failure: z.enum(ASSET_FAILURES).nullable(),
    created_at: TimestampSchema,
    updated_at: TimestampSchema,
  })
  .refine((asset) => mediaTypeFits(asset.kind, asset.media_type), MEDIA_TYPE_ISSUE)
  .refine((asset) => altFits(asset.kind, asset.alt), ALT_ISSUE)
  .refine((asset) => (asset.failure !== null) === (asset.state === 'failed'), {
    path: ['failure'],
    message: 'only a failed asset carries a failure, and every failed asset does',
  })
  .refine(
    (asset) => {
      const ready = asset.state === 'ready'
      const measured = asset.width !== null && asset.height !== null
      return (asset.kind === 'image' && ready) === measured && (asset.kind === 'audio' && ready) === (asset.duration_ms !== null)
    },
    { path: ['state'], message: 'a ready image has its dimensions, a ready audio asset its duration, and nothing else has them' },
  )
  .refine((asset) => asset.width === null || asset.height === null || asset.width * asset.height <= IMAGE_MAX_PIXELS, {
    path: ['width'],
    message: `an image is at most ${IMAGE_MAX_PIXELS} pixels`,
  })
  .refine((asset) => !(asset.state === 'ready' && asset.kind === 'audio') || asset.media_type === 'audio/mpeg', {
    path: ['media_type'],
    message: 'processed audio is audio/mpeg',
  })
export type Asset = z.infer<typeof AssetSchema>

/** What a table client is given instead of an asset id (SEC-15): a per-slot handle
 * that dies with its slot, the type, and what a player needs to lay it out. */
export const TableAssetRefSchema = z
  .object({
    handle: OpaqueIdSchema,
    kind: z.enum(ASSET_KINDS),
    media_type: MediaTypeSchema,
    width: PixelsSchema.nullable(),
    height: PixelsSchema.nullable(),
    duration_ms: DurationMsSchema.nullable(),
  })
  .refine((ref) => mediaTypeFits(ref.kind, ref.media_type), MEDIA_TYPE_ISSUE)
  .refine(
    (ref) => {
      const image = ref.kind === 'image'
      return image === (ref.width !== null && ref.height !== null) && image !== (ref.duration_ms !== null)
    },
    { path: ['kind'], message: 'an image handle carries its dimensions and an audio handle its duration' },
  )
export type TableAssetRef = z.infer<typeof TableAssetRefSchema>

/** A cue record (LIB-26): a title and an immutable kind over a ready audio asset. */
export const CueSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    cue_id: OpaqueIdSchema,
    campaign_id: OpaqueIdSchema,
    title: CueTitleSchema,
    kind: z.enum(CUE_KINDS),
    asset_id: OpaqueIdSchema,
    duration_ms: DurationMsSchema,
    archived: z.boolean(),
    created_at: TimestampSchema,
    updated_at: TimestampSchema,
  })
  .refine((cue) => cue.duration_ms <= CUE_MAX_MS[cue.kind], { path: ['duration_ms'], message: 'too long for its kind' })
export type Cue = z.infer<typeof CueSchema>

export const CueCreateRequestSchema = refusingProtoKeys(
  z.strictObject({
    schema_version: z.literal(CONTRACT_VERSION),
    command_id: CommandIdSchema,
    campaign_id: OpaqueIdSchema,
    asset_id: OpaqueIdSchema,
    title: CueTitleSchema,
    kind: z.enum(CUE_KINDS),
  }),
)
export type CueCreateRequest = z.infer<typeof CueCreateRequestSchema>

/** The title can be edited; the kind cannot (AUDIO-4), so it is not here. */
export const CueRenameRequestSchema = refusingProtoKeys(
  z.strictObject({ schema_version: z.literal(CONTRACT_VERSION), title: CueTitleSchema }),
)
export type CueRenameRequest = z.infer<typeof CueRenameRequestSchema>

/** Empty for no search; otherwise 2 to 100 characters after trimming (LIB-20). */
const searchTextSchema = z
  .string()
  .refine(isWellFormedText, WELL_FORMED)
  .refine(
    (value) => {
      const length = codePointLength(trimWire(value))
      return length === 0 || (length >= SEARCH_MIN_CHARS && length <= SEARCH_MAX_CHARS)
    },
    { message: `a search is ${SEARCH_MIN_CHARS} to ${SEARCH_MAX_CHARS} characters` },
  )

/** The Cues category of the library (LIB-5, LIB-26), a request body like LibraryQuery. */
export const CueListQuerySchema = refusingProtoKeys(
  z.strictObject({
    schema_version: z.literal(CONTRACT_VERSION),
    campaign_id: OpaqueIdSchema,
    search: searchTextSchema,
    sort: z.enum(LIBRARY_SORTS),
    archived: z.boolean(),
    cursor: CursorSchema.nullish(),
    limit: z.number().int().min(1).max(CUE_PAGE_MAX_ITEMS).nullish(),
  }),
)
export type CueListQuery = z.infer<typeof CueListQuerySchema>

export const CuePageSchema = z.object({
  schema_version: z.literal(CONTRACT_VERSION),
  campaign_id: OpaqueIdSchema,
  items: z.array(CueSchema).max(CUE_PAGE_MAX_ITEMS),
  next_cursor: CursorSchema.nullable(),
})
export type CuePage = z.infer<typeof CuePageSchema>

/** AUDIO-8: a push always starts at zero; the field stays for forward compatibility. */
const StartOffsetSchema = z.literal(0)
/** The audio epoch (AUDIO-28): every Stop and every committed push advances it. */
const AudioEpochSchema = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER)
/** A slot's sequence (AUDIO-15): per slot, monotonic, assigned by the database. */
const SlotSequenceSchema = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER)
/** A link generation (SEC-9): every frame names the one it was produced under. */
const LinkGenerationSchema = z.number().int().min(1).max(Number.MAX_SAFE_INTEGER)

/** Play to table (AUDIO-8); idempotent by command id, refused with a stale epoch (AUDIO-28). */
export const CuePlayRequestSchema = refusingProtoKeys(
  z.strictObject({
    schema_version: z.literal(CONTRACT_VERSION),
    command_id: CommandIdSchema,
    cue_id: OpaqueIdSchema,
    audio_epoch: AudioEpochSchema,
    loop: z.boolean(),
    start_offset_ms: StartOffsetSchema.optional(),
  }),
)
export type CuePlayRequest = z.infer<typeof CuePlayRequestSchema>

/** AUDIO-9: a card's Stop names its cue; Stop all is `cue_id: null`. No epoch, never queued (X-3). */
export const CueStopRequestSchema = refusingProtoKeys(
  z.strictObject({
    schema_version: z.literal(CONTRACT_VERSION),
    command_id: CommandIdSchema,
    cue_id: OpaqueIdSchema.nullable(),
  }),
)
export type CueStopRequest = z.infer<typeof CueStopRequestSchema>

// ── Table sessions ───────────────────────────────────────────────────────────

/** A table token or an enrolment code as it travels — once, in a POST body (SEC-8, SEC-11). */
const TableSecretSchema = z.string().regex(/^[A-Za-z0-9_-]{43}$/)

export const TableJoinRequestSchema = refusingProtoKeys(
  z.strictObject({ schema_version: z.literal(CONTRACT_VERSION), token: TableSecretSchema }),
)
export type TableJoinRequest = z.infer<typeof TableJoinRequestSchema>

/** One shape for every outcome (SEC-8); the role comes only with a join. */
export const TableJoinResponseSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    status: z.enum(JOIN_STATUSES),
    role: z.enum(TABLE_ROLES).nullable(),
  })
  .refine((answer) => (answer.role !== null) === (answer.status === 'joined'), {
    path: ['role'],
    message: 'a role comes with a join, and only with a join',
  })
export type TableJoinResponse = z.infer<typeof TableJoinResponseSchema>

export const EnrolRequestSchema = refusingProtoKeys(
  z.strictObject({ schema_version: z.literal(CONTRACT_VERSION), code: TableSecretSchema }),
)
export type EnrolRequest = z.infer<typeof EnrolRequestSchema>

export const EnrolResponseSchema = z.object({
  schema_version: z.literal(CONTRACT_VERSION),
  status: z.enum(ENROL_STATUSES),
})
export type EnrolResponse = z.infer<typeof EnrolResponseSchema>

/** The GM's view of a session (REVEAL-2, REVEAL-17). The token is not here: it travels once. */
export const TableSessionSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    session_id: OpaqueIdSchema,
    campaign_id: OpaqueIdSchema,
    state: z.enum(SESSION_STATES),
    gen: LinkGenerationSchema,
    /** AUDIO-24: two GM tabs converge on the epoch the resource carries. */
    audio_epoch: AudioEpochSchema,
    started_at: TimestampSchema,
    ends_at: TimestampSchema,
    ended_at: TimestampSchema.nullable(),
    audio: z.boolean(),
    devices: z.number().int().min(0).max(1000),
  })
  .refine((session) => (session.ended_at !== null) === (session.state === 'ended'), {
    path: ['ended_at'],
    message: 'an ended session says when, and a live one does not',
  })
  .refine((session) => Date.parse(session.ends_at) > Date.parse(session.started_at), {
    path: ['ends_at'],
    message: 'a session ends after it starts',
  })
export type TableSession = z.infer<typeof TableSessionSchema>

/** Start, End and Rotate (REVEAL-17), idempotent by command id; only Rotate may reset personal links. */
export const TableSessionRequestSchema = refusingProtoKeys(
  z
    .strictObject({
      schema_version: z.literal(CONTRACT_VERSION),
      command_id: CommandIdSchema,
      campaign_id: OpaqueIdSchema,
      action: z.enum(SESSION_ACTIONS),
      reset_personal_links: z.boolean().optional(),
    })
    .refine((request) => !request.reset_personal_links || request.action === 'rotate', {
      path: ['reset_personal_links'],
      message: 'personal links are reset with a rotation',
    }),
)
export type TableSessionRequest = z.infer<typeof TableSessionRequestSchema>

/** A start or a rotation carries the new token, the one time it is in a body (SEC-8); an end carries none. */
export const TableSessionAnswerSchema = z
  .object({
    schema_version: z.literal(CONTRACT_VERSION),
    session: TableSessionSchema,
    token: TableSecretSchema.nullable(),
  })
  .refine((answer) => (answer.token !== null) === (answer.session.state === 'live'), {
    path: ['token'],
    message: 'a live session answers with its token, and an ended one with none',
  })
export type TableSessionAnswer = z.infer<typeof TableSessionAnswerSchema>

// ── Realtime events ──────────────────────────────────────────────────────────
// Two channels, two unions (ADR RT-1, threat model 8.3). Every frame carries its
// own schema_version; the heartbeat is an SSE comment, not an event. `snapshot`
// and `slot` arrive with the reveal family.

const eventBase = { schema_version: z.literal(CONTRACT_VERSION) }

/** AUDIO-3: a one-shot never loops, and is at most 30 s. */
const playingFitsSlot = (slot: string, playing: { loop: boolean; duration_ms: number } | null) =>
  playing === null || slot !== 'one_shot' || (!playing.loop && playing.duration_ms <= ONE_SHOT_MAX_MS)
const SLOT_ISSUE = { path: ['playing'], message: 'a one-shot never loops and is at most 30 seconds' }

/** What a slot holds, as the GM sees it: the cue by id and title (AUDIO-11). */
const GmPlayingSchema = z.object({
  cue_id: OpaqueIdSchema,
  title: CueTitleSchema,
  started_at: TimestampSchema,
  start_offset_ms: StartOffsetSchema,
  loop: z.boolean(),
  duration_ms: DurationMsSchema,
})

const ToolLaneEventSchema = z.object({
  ...eventBase,
  event: z.literal('tool_lane'),
  conversation_id: OpaqueIdSchema,
  entry_id: OpaqueIdSchema,
  invocation: ToolInvocationSchema,
})
const EditLaneEventSchema = z.object({
  ...eventBase,
  event: z.literal('edit_lane'),
  conversation_id: OpaqueIdSchema,
  entry_id: OpaqueIdSchema,
  invocation: EditInvocationSchema,
})
const GmSessionEventSchema = z.object({ ...eventBase, event: z.literal('session'), session: TableSessionSchema })
const GmAudioEventSchema = z
  .object({
    ...eventBase,
    event: z.literal('audio'),
    session_id: OpaqueIdSchema,
    gen: LinkGenerationSchema,
    audio_epoch: AudioEpochSchema,
    slot: z.enum(AUDIO_SLOTS),
    seq: SlotSequenceSchema,
    playing: GmPlayingSchema.nullable(),
  })
  .refine((frame) => playingFitsSlot(frame.slot, frame.playing), SLOT_ISSUE)
/** AUD-11: an alias travels only on the GM's channel. */
const ParticipantPresenceSchema = z.object({
  participant_id: OpaqueIdSchema,
  alias: oneLine(1, 60),
  audio: z.enum(PRESENCE_AUDIO),
})
const count = z.number().int().min(0).max(1000)
/** Guests are counted, never named (AUDIO-21). */
const GuestPresenceSchema = z
  .object({ connected: count, listening: count, muted: count, pending: count })
  .refine((guests) => guests.listening + guests.muted + guests.pending <= guests.connected, {
    path: ['connected'],
    message: 'guest states cannot exceed the guests connected',
  })
const PresenceEventSchema = z.object({
  ...eventBase,
  event: z.literal('presence'),
  session_id: OpaqueIdSchema,
  gen: LinkGenerationSchema,
  participants: z.array(ParticipantPresenceSchema).max(PRESENCE_MAX_PARTICIPANTS),
  guests: GuestPresenceSchema,
})
/** An asset changed state (ADR MS-3): the GM's `Still processing…` ends here. */
const GmAssetEventSchema = z.object({ ...eventBase, event: z.literal('asset'), asset: AssetSchema })
/** The snapshot is complete; what follows is live (ADR RT-4) — the boundary TABLE-7 needs. */
const GmReadyEventSchema = z.object({ ...eventBase, event: z.literal('ready') })
/** The server is closing this stream on purpose (ADR RT-3); reopen with backoff. */
const GmReconnectEventSchema = z.object({ ...eventBase, event: z.literal('reconnect') })

export const GmEventSchema = z.discriminatedUnion('event', [
  ToolLaneEventSchema,
  EditLaneEventSchema,
  GmSessionEventSchema,
  GmAudioEventSchema,
  PresenceEventSchema,
  GmAssetEventSchema,
  GmReadyEventSchema,
  GmReconnectEventSchema,
])
export type GmEvent = z.infer<typeof GmEventSchema>

/** A snapshot ends with `ready` and holds no `reconnect`: `ready` is the boundary a
 * client trusts nothing before (TABLE-7); a reconnect belongs to a stream. */
const endsWithReady = (frames: ReadonlyArray<{ event: string }>) =>
  frames.length > 0 && frames[frames.length - 1].event === 'ready' && !frames.some((frame) => frame.event === 'reconnect')
const SNAPSHOT_ISSUE = { path: ['frames'], message: 'a snapshot ends with ready and never carries a reconnect' }

/** The GM channel read as a resource — a stream's opening frames, and the polling mode of ADR RT-9. */
export const GmSnapshotSchema = z
  .object({ schema_version: z.literal(CONTRACT_VERSION), frames: z.array(GmEventSchema).min(1).max(200) })
  .refine((snapshot) => endsWithReady(snapshot.frames), SNAPSHOT_ISSUE)
export type GmSnapshot = z.infer<typeof GmSnapshotSchema>

/** A live session as a table client may know it (AUDIO-19), and this device's own role. */
const TableSessionEventSchema = z.object({
  ...eventBase,
  event: z.literal('session'),
  audio: z.boolean(),
  role: z.enum(TABLE_ROLES),
})
/** Ended, expired or rotated: one generic event, then the connection closes (TABLE-9). */
const TableInactiveEventSchema = z.object({ ...eventBase, event: z.literal('inactive') })
/** What a slot holds, as a table client sees it: a handle, never a title (AUDIO-29). */
const TablePlayingSchema = z
  .object({
    asset: TableAssetRefSchema,
    started_at: TimestampSchema,
    start_offset_ms: StartOffsetSchema,
    loop: z.boolean(),
    duration_ms: DurationMsSchema,
  })
  .refine((playing) => playing.asset.kind === 'audio', { path: ['asset', 'kind'], message: 'a slot plays audio' })
const TableAudioEventSchema = z
  .object({
    ...eventBase,
    event: z.literal('audio'),
    slot: z.enum(AUDIO_SLOTS),
    seq: SlotSequenceSchema,
    playing: TablePlayingSchema.nullable(),
  })
  .refine((frame) => playingFitsSlot(frame.slot, frame.playing), SLOT_ISSUE)
const TableReadyEventSchema = z.object({ ...eventBase, event: z.literal('ready') })
const TableReconnectEventSchema = z.object({ ...eventBase, event: z.literal('reconnect') })

export const TableEventSchema = z.discriminatedUnion('event', [
  TableSessionEventSchema,
  TableInactiveEventSchema,
  TableAudioEventSchema,
  TableReadyEventSchema,
  TableReconnectEventSchema,
])
export type TableEvent = z.infer<typeof TableEventSchema>

/** The table channel read as a resource: session, one audio frame per slot, later the reveal slots, then ready. */
export const TableSnapshotSchema = z
  .object({ schema_version: z.literal(CONTRACT_VERSION), frames: z.array(TableEventSchema).min(1).max(50) })
  .refine((snapshot) => endsWithReady(snapshot.frames), SNAPSHOT_ISSUE)
export type TableSnapshot = z.infer<typeof TableSnapshotSchema>

/** Name → schema, in the order `contracts/workbench/v1/schemas.json` lists them. */
export const CONTRACT_SCHEMAS: Record<string, ZodType> = {
  Timestamp: TimestampSchema,
  ErrorBody: ErrorBodySchema,
  ToolInvocationRequest: ToolInvocationRequestSchema,
  ToolSuggestion: ToolSuggestionSchema,
  DocumentLink: DocumentLinkSchema,
  AssetRef: AssetRefSchema,
  ToolResult: ToolResultSchema,
  ToolInvocation: ToolInvocationSchema,
  DocumentVersion: DocumentVersionSchema,
  Document: DocumentSchema,
  DocumentVersionSnapshot: DocumentVersionSnapshotSchema,
  DocumentHistoryPage: DocumentHistoryPageSchema,
  FieldPatchRequest: FieldPatchRequestSchema,
  DocumentCreateRequest: DocumentCreateRequestSchema,
  RestoreRequest: RestoreRequestSchema,
  EditRequest: EditRequestSchema,
  EditInvocation: EditInvocationSchema,
  LibraryQuery: LibraryQuerySchema,
  LibraryPage: LibraryPageSchema,
  TimelineEntry: TimelineEntrySchema,
  TimelinePage: TimelinePageSchema,
  AssetCreateRequest: AssetCreateRequestSchema,
  Asset: AssetSchema,
  TableAssetRef: TableAssetRefSchema,
  Cue: CueSchema,
  CueCreateRequest: CueCreateRequestSchema,
  CueRenameRequest: CueRenameRequestSchema,
  CueListQuery: CueListQuerySchema,
  CuePage: CuePageSchema,
  CuePlayRequest: CuePlayRequestSchema,
  CueStopRequest: CueStopRequestSchema,
  TableJoinRequest: TableJoinRequestSchema,
  TableJoinResponse: TableJoinResponseSchema,
  EnrolRequest: EnrolRequestSchema,
  EnrolResponse: EnrolResponseSchema,
  TableSession: TableSessionSchema,
  TableSessionRequest: TableSessionRequestSchema,
  TableSessionAnswer: TableSessionAnswerSchema,
  GmEvent: GmEventSchema,
  TableEvent: TableEventSchema,
  GmSnapshot: GmSnapshotSchema,
  TableSnapshot: TableSnapshotSchema,
}

// ── Forward-version behaviour ────────────────────────────────────────────────

/**
 * What a client does with a payload it cannot use. `newer_schema` and
 * `unknown_kind` are expected after a rollback or with a stale bundle, and
 * render the neutral "made by a newer version" placeholder (RAIL-24).
 * `invalid` renders the same placeholder but is a bug worth counting.
 */
export type UnknownReason = 'newer_schema' | 'unknown_kind' | 'invalid'

export type Parsed<T> = { kind: 'ok'; value: T } | { kind: 'unknown'; reason: UnknownReason }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** A version is an integral number; `1.5` and `'2'` are not. */
function versionNamed(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) ? value : null
}

/** Whether the payload's own `schema_version` — or its embedded invocation's, the
 * one other versioned object an entry holds — is beyond this client. A version
 * anywhere deeper is content, not a version. */
function namesNewerVersion(raw: unknown): boolean {
  if (!isRecord(raw)) return false
  const invocation = isRecord(raw.invocation) ? raw.invocation : null
  return [raw.schema_version, invocation?.schema_version].some((value) => {
    const version = versionNamed(value)
    return version !== null && version > CONTRACT_VERSION
  })
}

/** The value at a path into nested records, or `undefined`. */
function at(raw: unknown, path: readonly string[]): unknown {
  let node = raw
  for (const key of path) {
    if (!isRecord(node)) return undefined
    node = node[key]
  }
  return node
}

function hasUnknownKind(raw: unknown, path: readonly string[], known: readonly string[]): boolean {
  const value = at(raw, path)
  return typeof value === 'string' && !known.includes(value)
}

/** Every discriminator a result carries; 1kg.4.3 adds card kinds. */
const RESULT_DISCRIMINATORS: ReadonlyArray<[readonly string[], readonly string[]]> = [
  [['result_kind'], RESULT_KINDS],
  [['card', 'card_kind'], CARD_KINDS],
]

/** Every discriminator an entry carries. A value this client does not know at
 * any of them reads as "made by a newer version", never as damage. */
const ENTRY_DISCRIMINATORS: ReadonlyArray<[readonly string[], readonly string[]]> = [
  [['entry_kind'], ENTRY_KINDS],
  ...RESULT_DISCRIMINATORS.map(([path, known]): [readonly string[], readonly string[]] => [['invocation', 'result', ...path], known]),
  [['invocation', 'result', 'outcome'], EDIT_OUTCOMES],
  [['scope', 'kind'], EDIT_SCOPE_KINDS],
  [['instruction', 'kind'], EDIT_INSTRUCTION_KINDS],
]

function hasAnyUnknownKind(raw: unknown, discriminators: ReadonlyArray<[readonly string[], readonly string[]]>): boolean {
  return discriminators.some(([path, known]) => hasUnknownKind(raw, path, known))
}

export function parseToolInvocation(raw: unknown): Parsed<ToolInvocation> {
  if (namesNewerVersion(raw)) return { kind: 'unknown', reason: 'newer_schema' }
  if (isRecord(raw) && hasAnyUnknownKind(raw.result, RESULT_DISCRIMINATORS)) return { kind: 'unknown', reason: 'unknown_kind' }
  const result = ToolInvocationSchema.safeParse(raw)
  return result.success ? { kind: 'ok', value: result.data } : { kind: 'unknown', reason: 'invalid' }
}

export function parseToolResult(raw: unknown): Parsed<ToolResult> {
  if (hasAnyUnknownKind(raw, RESULT_DISCRIMINATORS)) return { kind: 'unknown', reason: 'unknown_kind' }
  const result = ToolResultSchema.safeParse(raw)
  return result.success ? { kind: 'ok', value: result.data } : { kind: 'unknown', reason: 'invalid' }
}

/** How the canvas reads a document. A type, or a version of a type's field
 * definitions, that this client does not know is a placeholder state — never an
 * NPC by default, which is what the handoff's registry fallback did (X-8). */
export function parseDocument(raw: unknown): Parsed<Document> {
  if (namesNewerVersion(raw)) return { kind: 'unknown', reason: 'newer_schema' }
  if (hasUnknownKind(raw, ['type'], DOCUMENT_TYPE_IDS)) return { kind: 'unknown', reason: 'unknown_kind' }
  if (isRecord(raw) && typeof raw.type === 'string') {
    const version = versionNamed(raw.type_version)
    if (version !== null && version > DOC_TYPE_VERSION[raw.type as DocumentTypeId]) return { kind: 'unknown', reason: 'newer_schema' }
  }
  const result = DocumentSchema.safeParse(raw)
  return result.success ? { kind: 'ok', value: result.data } : { kind: 'unknown', reason: 'invalid' }
}

const GM_EVENT_DISCRIMINATORS: ReadonlyArray<[readonly string[], readonly string[]]> = [
  [['event'], GM_EVENT_KINDS],
  ...RESULT_DISCRIMINATORS.map(([path, known]): [readonly string[], readonly string[]] => [['invocation', 'result', ...path], known]),
  [['invocation', 'result', 'outcome'], EDIT_OUTCOMES],
]
const TABLE_EVENT_DISCRIMINATORS: ReadonlyArray<[readonly string[], readonly string[]]> = [[['event'], TABLE_EVENT_KINDS]]

/** How a GM channel reads a frame: a kind this client does not know — `snapshot`
 * and `slot` until the reveal family lands, anything newer after — is a
 * placeholder, never a crash; the stream carries on with the next frame. */
export function parseGmEvent(raw: unknown): Parsed<GmEvent> {
  if (namesNewerVersion(raw)) return { kind: 'unknown', reason: 'newer_schema' }
  if (hasAnyUnknownKind(raw, GM_EVENT_DISCRIMINATORS)) return { kind: 'unknown', reason: 'unknown_kind' }
  const result = GmEventSchema.safeParse(raw)
  return result.success ? { kind: 'ok', value: result.data } : { kind: 'unknown', reason: 'invalid' }
}

/** The same for a table channel. */
export function parseTableEvent(raw: unknown): Parsed<TableEvent> {
  if (namesNewerVersion(raw)) return { kind: 'unknown', reason: 'newer_schema' }
  if (hasAnyUnknownKind(raw, TABLE_EVENT_DISCRIMINATORS)) return { kind: 'unknown', reason: 'unknown_kind' }
  const result = TableEventSchema.safeParse(raw)
  return result.success ? { kind: 'ok', value: result.data } : { kind: 'unknown', reason: 'invalid' }
}

export interface ReadSnapshot<T> {
  /** One item per frame the server sent, none dropped; the last is `ready`. */
  frames: Array<Parsed<T>>
}

const SnapshotEnvelopeSchema = z.object({
  schema_version: z.literal(CONTRACT_VERSION),
  frames: z.array(z.unknown()).min(1).max(200),
})

function parseSnapshot<T>(raw: unknown, frame: (item: unknown) => Parsed<T>): Parsed<ReadSnapshot<T>> {
  if (isRecord(raw)) {
    const version = versionNamed(raw.schema_version)
    if (version !== null && version > CONTRACT_VERSION) return { kind: 'unknown', reason: 'newer_schema' }
  }
  const envelope = SnapshotEnvelopeSchema.safeParse(raw)
  if (!envelope.success) return { kind: 'unknown', reason: 'invalid' }
  const last = envelope.data.frames[envelope.data.frames.length - 1]
  if (!isRecord(last) || last.event !== 'ready') return { kind: 'unknown', reason: 'invalid' }
  return { kind: 'ok', value: { frames: envelope.data.frames.map(frame) } }
}

/** How a channel reads its snapshot: the envelope strictly, each frame on its own,
 * so one frame from a newer server becomes one placeholder (ADR RT-4). */
export function parseGmSnapshot(raw: unknown): Parsed<ReadSnapshot<GmEvent>> {
  return parseSnapshot(raw, parseGmEvent)
}

export function parseTableSnapshot(raw: unknown): Parsed<ReadSnapshot<TableEvent>> {
  return parseSnapshot(raw, parseTableEvent)
}

/**
 * One slot in a thread. An entry this client cannot use still keeps its place
 * and, when it is readable, its id — so the rest of the thread renders around
 * one placeholder (AE-43). A server-sent `opaque` entry is `ok`: it is a valid
 * entry whose meaning is "placeholder".
 */
export type TimelineItem =
  | { kind: 'ok'; value: TimelineEntry }
  | { kind: 'unknown'; reason: UnknownReason; entry_id: string | null }

export function parseTimelineEntry(raw: unknown): TimelineItem {
  const id = isRecord(raw) ? OpaqueIdSchema.safeParse(raw.entry_id) : null
  const entry_id = id?.success ? id.data : null
  if (namesNewerVersion(raw)) return { kind: 'unknown', reason: 'newer_schema', entry_id }
  if (hasAnyUnknownKind(raw, ENTRY_DISCRIMINATORS)) return { kind: 'unknown', reason: 'unknown_kind', entry_id }
  const result = TimelineEntrySchema.safeParse(raw)
  return result.success ? { kind: 'ok', value: result.data } : { kind: 'unknown', reason: 'invalid', entry_id }
}

export interface ReadTimelinePage {
  conversation_id: string
  /** Newest first, one item per entry the server sent, none dropped. */
  items: TimelineItem[]
  next_cursor: string | null
}

const TimelineEnvelopeSchema = z.object({
  ...pageEnvelope,
  items: z.array(z.unknown()).max(TIMELINE_PAGE_MAX_ITEMS),
})

/** How a component reads a page: the envelope strictly, each entry on its own,
 * so one entry from a newer server cannot take the thread down with it. */
export function parseTimelinePage(raw: unknown): Parsed<ReadTimelinePage> {
  if (isRecord(raw)) {
    const version = versionNamed(raw.schema_version)
    if (version !== null && version > CONTRACT_VERSION) return { kind: 'unknown', reason: 'newer_schema' }
  }
  const envelope = TimelineEnvelopeSchema.safeParse(raw)
  if (!envelope.success) return { kind: 'unknown', reason: 'invalid' }
  const { conversation_id, items, next_cursor } = envelope.data
  return { kind: 'ok', value: { conversation_id, items: items.map(parseTimelineEntry), next_cursor } }
}

// ── One reader for every error shape ─────────────────────────────────────────

export type ErrorRead =
  | { kind: 'workbench'; info: ErrorInfo }
  /** A legacy route: `detail` is a sentence. */
  | { kind: 'legacy'; message: string }
  /** FastAPI's own 422: the request fields at fault, in order, without duplicates. */
  | { kind: 'validation'; fields: string[] }
  | { kind: 'unreadable' }

export function readErrorBody(body: unknown): ErrorRead {
  if (!isRecord(body)) return { kind: 'unreadable' }
  const { detail } = body
  if (typeof detail === 'string') return { kind: 'legacy', message: detail }

  if (Array.isArray(detail)) {
    const fields: string[] = []
    for (const item of detail) {
      if (!isRecord(item) || !Array.isArray(item.loc)) return { kind: 'unreadable' }
      const field = item.loc.filter((part) => typeof part === 'string' && part !== 'body').pop()
      if (typeof field === 'string' && !fields.includes(field)) fields.push(field)
    }
    return { kind: 'validation', fields }
  }

  const info = ErrorInfoSchema.safeParse(detail)
  return info.success ? { kind: 'workbench', info: info.data } : { kind: 'unreadable' }
}

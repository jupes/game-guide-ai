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
 * client cannot understand becomes a placeholder through `parseToolInvocation`
 * / `parseToolResult` — never a crash, and never a guess (X-8, RAIL-24).
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

/** AI edits and attached cues join with the documents and cue families. */
export const ENTRY_KINDS = ['chat', 'tool', 'session_divider', 'opaque'] as const
export type EntryKind = (typeof ENTRY_KINDS)[number]

export const SESSION_BOUNDARIES = ['start', 'end'] as const
export const OPAQUE_REASONS = ['newer_version', 'unreadable'] as const

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

// ── Building blocks ──────────────────────────────────────────────────────────

/** Characters as the server counts them. `'🎲'.length` is 2; this is 1. */
export function codePointLength(value: string): number {
  // Spreading iterates by code point, not by UTF-16 unit.
  return [...value].length
}

/** A string bounded in code points, so both sides agree on "2,000 characters". */
function text(min: number, max: number) {
  return z.string().refine(
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

const ToolIdSchema = z.enum(TOOL_IDS)

// ── Errors ───────────────────────────────────────────────────────────────────

export const ErrorInfoSchema = z.object({
  // Any well-formed code: a newer server may know more than this client does.
  code: z.string().regex(/^[a-z][a-z_]{1,39}$/),
  message: text(1, 500),
  retryable: z.boolean(),
  field: text(1, 64).nullish(),
  retry_after_s: z.number().int().min(0).max(86_400).nullish(),
  in_flight: z.array(InvocationIdSchema).max(8).nullish(),
})
export type ErrorInfo = z.infer<typeof ErrorInfoSchema>

/** The Workbench error envelope. It keeps FastAPI's `detail` key. */
export const ErrorBodySchema = z.object({ detail: ErrorInfoSchema })

// ── Tool invocation ──────────────────────────────────────────────────────────

/** Decisions RAIL-5 to RAIL-9. Strict: the client builds this, so a stray key is
 * a client bug — and there is no free-form `context` (RAIL-7). */
export const ToolInvocationRequestSchema = z
  .strictObject({
    schema_version: z.literal(CONTRACT_VERSION),
    invocation_id: InvocationIdSchema,
    tool_id: ToolIdSchema,
    brief: z.string(),
    campaign_id: OpaqueIdSchema,
    conversation_id: OpaqueIdSchema,
    source_entry_id: OpaqueIdSchema.nullish(),
  })
  .refine((request) => codePointLength(request.brief.trim()) <= BRIEF_MAX_CHARS, {
    path: ['brief'],
    message: `a brief can be at most ${BRIEF_MAX_CHARS} characters`,
  })
  .refine((request) => request.brief.trim() !== '' || BRIEF_POLICY[request.tool_id] === 'optional', {
    path: ['brief'],
    message: 'this tool needs a brief',
  })
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

/** No URL, ever (X-10): the client builds a same-origin URL from the id. */
export const AssetRefSchema = z.object({
  asset_id: OpaqueIdSchema,
  media_type: z.literal('image'),
  alt: text(1, 300),
  width: z.number().int().min(1).max(20_000).nullish(),
  height: z.number().int().min(1).max(20_000).nullish(),
})
export type AssetRef = z.infer<typeof AssetRefSchema>

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
  TimelineEntry: TimelineEntrySchema,
  TimelinePage: TimelinePageSchema,
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

/** Whether any `schema_version` inside a payload is beyond this client — the
 * payload's own, or one embedded in it (an entry's invocation). */
function mentionsNewerVersion(value: unknown, depth = 0): boolean {
  if (depth > 8) return false
  if (Array.isArray(value)) return value.some((item) => mentionsNewerVersion(item, depth + 1))
  if (!isRecord(value)) return false
  if (typeof value.schema_version === 'number' && value.schema_version > CONTRACT_VERSION) return true
  return Object.values(value).some((item) => mentionsNewerVersion(item, depth + 1))
}

function hasUnknownKind(raw: unknown, key: string, known: readonly string[]): boolean {
  return isRecord(raw) && typeof raw[key] === 'string' && !known.includes(raw[key])
}

export function parseToolInvocation(raw: unknown): Parsed<ToolInvocation> {
  if (mentionsNewerVersion(raw)) return { kind: 'unknown', reason: 'newer_schema' }
  const result = ToolInvocationSchema.safeParse(raw)
  return result.success ? { kind: 'ok', value: result.data } : { kind: 'unknown', reason: 'invalid' }
}

export function parseToolResult(raw: unknown): Parsed<ToolResult> {
  if (hasUnknownKind(raw, 'result_kind', RESULT_KINDS)) return { kind: 'unknown', reason: 'unknown_kind' }
  const result = ToolResultSchema.safeParse(raw)
  return result.success ? { kind: 'ok', value: result.data } : { kind: 'unknown', reason: 'invalid' }
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
  if (mentionsNewerVersion(raw)) return { kind: 'unknown', reason: 'newer_schema', entry_id }

  const invocation = isRecord(raw) && isRecord(raw.invocation) ? raw.invocation : null
  if (hasUnknownKind(raw, 'entry_kind', ENTRY_KINDS) || hasUnknownKind(invocation?.result, 'result_kind', RESULT_KINDS)) {
    return { kind: 'unknown', reason: 'unknown_kind', entry_id }
  }
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
  if (isRecord(raw) && typeof raw.schema_version === 'number' && raw.schema_version > CONTRACT_VERSION) {
    return { kind: 'unknown', reason: 'newer_schema' }
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

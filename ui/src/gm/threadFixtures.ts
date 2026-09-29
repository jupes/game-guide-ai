/**
 * Timeline entries for the GM thread's tests and stories (1kg.3.4).
 *
 * Each builder returns what `parseTimelineEntry` makes of a wire payload, and
 * throws if the payload could not have come off the wire, so no test or story
 * can prop itself up on a shape the server would never send. The payloads
 * follow `contracts/workbench/v1/TimelineEntry.json`.
 */

import { ToolInvocationRequestSchema, parseTimelineEntry } from './contracts'
import type { TimelineItem, ToolInvocationRequest } from './contracts'
import type { LoadTimelinePageFn } from './gmTimeline'

function entry(raw: Record<string, unknown>): TimelineItem {
  const item = parseTimelineEntry(raw)
  if (item.kind !== 'ok') throw new Error(`fixture ${String(raw.entry_id)} is not a valid entry: ${item.reason}`)
  return item
}

export const STAT_BLOCK = {
  name: 'Tidewarden Drowned',
  size: 'Large',
  type: 'undead',
  alignment: 'neutral evil',
  ac: 16,
  hp: 82,
  speed: '20 ft., swim 40 ft.',
  cr: '5',
  traits: [{ name: 'Undertow', text: 'Creatures within 10 feet are pulled 5 feet toward it.' }],
}

export const CREATIVE_ANSWER = {
  text: 'Here is a **drowned guardian** for the marsh.',
  answerable: false,
  sources: [],
  stat_block: STAT_BLOCK,
}

export const SOURCED_ANSWER = {
  text: 'A basilisk petrifies with its gaze [1].',
  answerable: true,
  sources: [
    { book: 'mm-5e', chapter: 'Bestiary', section: 'Stat Block', entity: 'Basilisk', page: 12, snippet: 'Armor Class 15 ...' },
  ],
}

export function chatEntry(overrides: Record<string, unknown> = {}): TimelineItem {
  const { answer = CREATIVE_ANSWER, ...rest } = overrides
  return entry({
    schema_version: 1,
    entry_kind: 'chat',
    entry_id: 'ent_10a4c2ea',
    created_at: '2026-09-16T19:24:40Z',
    mode: 'gm',
    prompt: 'Give me a drowned guardian for the marsh.',
    answer: answer === null ? null : { created_at: '2026-09-16T19:24:52Z', ...(answer as Record<string, unknown>) },
    ...rest,
  })
}

export function toolEntry(overrides: Record<string, unknown> = {}, invocation: Record<string, unknown> = {}): TimelineItem {
  return entry({
    schema_version: 1,
    entry_kind: 'tool',
    entry_id: 'ent_77aa12bd',
    created_at: '2026-09-16T19:35:00Z',
    brief: 'CR 5, drowned',
    invocation: {
      schema_version: 1,
      invocation_id: 'inv_0a1b2c3d4e5f6a7b',
      tool_id: 'monster',
      status: 'working',
      attempt: 1,
      cancel_requested: false,
      created_at: '2026-09-16T19:35:00Z',
      updated_at: '2026-09-16T19:35:00Z',
      result: null,
      error: null,
      ...invocation,
    },
    ...overrides,
  })
}

export const EDIT_ENTRY: TimelineItem = entry({
  schema_version: 1,
  entry_kind: 'edit',
  entry_id: 'ent_ed170001',
  created_at: '2026-09-16T19:35:40Z',
  document: { document_id: 'doc_9k2f7a1c', type: 'npc', title: 'Sister Ondrey Vashe', library_category: 'npcs' },
  scope: { kind: 'document' },
  instruction: { kind: 'text', text: 'make her want something the party can give her' },
  invocation: {
    schema_version: 1,
    invocation_id: 'inv_e1d2c3b4a5f60718',
    document_id: 'doc_9k2f7a1c',
    status: 'done',
    attempt: 1,
    cancel_requested: false,
    created_at: '2026-09-16T19:35:40Z',
    updated_at: '2026-09-16T19:36:02Z',
    result: { outcome: 'changed', prose: 'She wants the signet back now.', version_number: 3, write_revision: 14, changed_fields: ['wants'], suggestions: [] },
    error: null,
  },
})

export const DIVIDER_ENTRY: TimelineItem = entry({
  schema_version: 1,
  entry_kind: 'session_divider',
  entry_id: 'ent_5e55a001',
  created_at: '2026-09-16T19:00:00Z',
  session_id: 'ses_2c7d91aa',
  boundary: 'start',
})

/**
 * A session boundary as the divider job stores it (1kg.3.5): two ids and the
 * boundary's own time, nothing else. Defaults to the END of DIVIDER_ENTRY's
 * session, four hours after its start.
 */
export function dividerEntry(overrides: Record<string, unknown> = {}): TimelineItem {
  return entry({
    schema_version: 1,
    entry_kind: 'session_divider',
    entry_id: 'ent_5e55a002',
    created_at: '2026-09-16T23:00:00Z',
    session_id: 'ses_2c7d91aa',
    boundary: 'end',
    ...overrides,
  })
}

/** The end of DIVIDER_ENTRY's session. */
export const END_DIVIDER_ENTRY: TimelineItem = dividerEntry()

/** A second session, a week later, that left nothing in this thread: its
 * start, then its own end, with nothing between them (I-10's quiet session). */
export const QUIET_SESSION: readonly TimelineItem[] = [
  dividerEntry({ entry_id: 'ent_5e55b001', session_id: 'ses_7f3e0b12', boundary: 'start', created_at: '2026-09-23T19:00:00Z' }),
  dividerEntry({ entry_id: 'ent_5e55b002', session_id: 'ses_7f3e0b12', boundary: 'end', created_at: '2026-09-23T22:30:00Z' }),
]

export const OPAQUE_ENTRY: TimelineItem = entry({
  schema_version: 1,
  entry_kind: 'opaque',
  entry_id: 'ent_0f00d001',
  created_at: '2026-09-16T20:10:00Z',
  reason: 'newer_version',
})

/**
 * `count` chat entries, newest first (`ent_<offset>` .. `ent_<offset + count -
 * 1>`), for tests that need a full HYDRATE_TARGET-sized page without writing
 * out each entry by hand (1kg.3.4's original fixture, 1kg.3.6's Load earlier).
 *
 * Answerless by default (`answer: null` — a turn GmThread draws with no
 * assistant lane at all): these entries exist only to reach the target
 * count, and a suite that renders one through `ChatPane`/`GmThread` a dozen
 * times over should not also pay for a hundred `StatBlockCard`s it never
 * asserts on. Pass `overrides` for the one entry (if any) a test does assert
 * on.
 */
export function manyChatEntries(
  count: number,
  offset = 0,
  overrides: Record<string, unknown> = {},
): TimelineItem[] {
  return Array.from({ length: count }, (_, i) => chatEntry({ entry_id: `ent_${offset + i}`, answer: null, ...overrides }))
}

/**
 * A loader serving `pages` in order, newest first, each page's cursor leading
 * to the next. Records every cursor it was asked for.
 */
export function pagedTimeline(pages: readonly (readonly TimelineItem[])[]): LoadTimelinePageFn & { cursors: (string | null)[] } {
  const cursors: (string | null)[] = []
  const load = async (conversationId: string, cursor: string | null) => {
    cursors.push(cursor)
    const index = cursor === null ? 0 : Number(cursor.slice(1))
    const next = index + 1 < pages.length ? `p${index + 1}` : null
    return { kind: 'ok' as const, page: { conversation_id: conversationId, items: [...(pages[index] ?? [])], next_cursor: next } }
  }
  return Object.assign(load, { cursors })
}

/** The conversation and campaign the pending-work fixtures belong to (1kg.3.5). */
export const LIVE_CONVERSATION = '0b9c6f0e-6f3e-4a59-9a57-3a2f4f5b7c1d'
export const LIVE_CAMPAIGN = 'cmp_4b1d9e7a'

/**
 * A start as the client sends it (1kg.3.5's pending-work model), parsed by the
 * contract's own strict schema so no test or story can hold a request the
 * server would refuse as malformed. An `/npc` start in LIVE_CONVERSATION
 * unless overridden.
 */
export function toolRequest(overrides: Record<string, unknown> = {}): ToolInvocationRequest {
  return ToolInvocationRequestSchema.parse({
    schema_version: 1,
    invocation_id: 'inv_11ve000000000001',
    tool_id: 'npc',
    brief: 'the hooded stranger at the bar',
    campaign_id: LIVE_CAMPAIGN,
    conversation_id: LIVE_CONVERSATION,
    ...overrides,
  })
}

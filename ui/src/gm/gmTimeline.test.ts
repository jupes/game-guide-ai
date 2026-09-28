/**
 * The GM thread's model (1kg.3.4): the paging walk, the mapping of stored
 * entries and live turns onto one `GmTurn`, and the hook that reads a thread.
 */

import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ChatResponse } from '../api'
import type { Exchange } from '../useChat'
import {
  HYDRATE_TARGET,
  MAX_PAGES_PER_READ,
  answerFromResponse,
  exchangesForExport,
  readTimeline,
  turnFromExchange,
  turnsFromTimeline,
  useGmTimeline,
} from './gmTimeline'
import type { LoadTimelinePageFn } from './gmTimeline'
import {
  CREATIVE_ANSWER,
  DIVIDER_ENTRY,
  EDIT_ENTRY,
  OPAQUE_ENTRY,
  chatEntry,
  pagedTimeline,
  toolEntry,
} from './threadFixtures'

function ids(items: readonly { kind: string; value?: { entry_id: string } }[]): (string | undefined)[] {
  return items.map((item) => item.value?.entry_id)
}

describe('readTimeline — only a null cursor ends the list (2026-09-25 note)', () => {
  it('keeps following next_cursor through an EMPTY page', async () => {
    const newest = chatEntry({ entry_id: 'ent_new' })
    const oldest = chatEntry({ entry_id: 'ent_old' })
    const load = pagedTimeline([[newest], [], [oldest]])

    const read = await readTimeline('cnv_1', load)

    expect(load.cursors).toEqual([null, 'p1', 'p2'])
    expect(read.kind).toBe('ok')
    // Newest first on the wire; oldest first for display.
    if (read.kind === 'ok') expect(ids(read.items)).toEqual(['ent_old', 'ent_new'])
  })

  it('keeps following next_cursor through a SHORT page', async () => {
    const load = pagedTimeline([[chatEntry({ entry_id: 'ent_a' })], [chatEntry({ entry_id: 'ent_b' })]])
    const read = await readTimeline('cnv_1', load)
    expect(load.cursors).toEqual([null, 'p1'])
    if (read.kind === 'ok') expect(ids(read.items)).toEqual(['ent_b', 'ent_a'])
  })

  it('stops once it holds a full page of entries, leaving older ones for later', async () => {
    const full = Array.from({ length: HYDRATE_TARGET }, (_, i) => chatEntry({ entry_id: `ent_${i}` }))
    const load = pagedTimeline([full, [chatEntry({ entry_id: 'ent_older' })]])
    const read = await readTimeline('cnv_1', load)
    expect(load.cursors).toEqual([null])
    if (read.kind === 'ok') expect(read.items).toHaveLength(HYDRATE_TARGET)
  })

  it('reads a conversation the server does not know yet as empty, not as a failure', async () => {
    const read = await readTimeline('cnv_new', async () => ({ kind: 'missing' }))
    expect(read).toEqual({ kind: 'ok', items: [] })
  })

  it('passes a failed page through as the error', async () => {
    const read = await readTimeline('cnv_1', async () => ({ kind: 'error', message: 'Message history unavailable (503).' }))
    expect(read).toEqual({ kind: 'error', message: 'Message history unavailable (503).' })
  })

  it('stops rather than spin when a server hands back the cursor it was given', async () => {
    const load = vi.fn<LoadTimelinePageFn>(async (conversationId) => ({
      kind: 'ok',
      page: { conversation_id: conversationId, items: [], next_cursor: 'same' },
    }))
    await readTimeline('cnv_1', load)
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('bounds one walk even against a server that never ends the list', async () => {
    let n = 0
    const load = vi.fn<LoadTimelinePageFn>(async (conversationId) => ({
      kind: 'ok',
      page: { conversation_id: conversationId, items: [], next_cursor: `c${(n += 1)}` },
    }))
    await readTimeline('cnv_1', load)
    expect(load).toHaveBeenCalledTimes(MAX_PAGES_PER_READ)
  })
})

describe('turnsFromTimeline — one turn per exchange, in order', () => {
  it('maps each entry kind to its lane', () => {
    const turns = turnsFromTimeline([
      DIVIDER_ENTRY,
      chatEntry({ entry_id: 'ent_chat' }),
      toolEntry({ entry_id: 'ent_tool' }),
      EDIT_ENTRY,
      OPAQUE_ENTRY,
      { kind: 'unknown', reason: 'unknown_kind', entry_id: 'ent_future' },
      { kind: 'unknown', reason: 'invalid', entry_id: null },
    ])
    expect(turns.map((t) => [t.kind, t.key])).toEqual([
      // The divider is 1kg.3.5's to label; it adds no exchange here.
      ['chat', 'entry:ent_chat'],
      ['tool', 'entry:ent_tool'],
      ['unsupported', 'entry:ent_ed170001'],
      ['unreadable', 'entry:ent_0f00d001'],
      ['unreadable', 'entry:ent_future'],
      ['unreadable', 'unknown:6'],
    ])
  })

  it('keeps a turn with no stored answer, and an answer with no recorded prompt', () => {
    const [unanswered, orphan] = turnsFromTimeline([
      chatEntry({ entry_id: 'ent_a', answer: null }),
      chatEntry({ entry_id: 'ent_b', prompt: null }),
    ])
    expect(unanswered).toMatchObject({ kind: 'chat', answer: { state: 'none' } })
    expect(orphan).toMatchObject({ kind: 'chat', prompt: null, answer: { state: 'answered' } })
  })

  it('keeps a tool turn as a tool and a brief (RAIL-9)', () => {
    const [turn] = turnsFromTimeline([toolEntry()])
    expect(turn).toMatchObject({ kind: 'tool', entryId: 'ent_77aa12bd', brief: 'CR 5, drowned' })
    if (turn.kind === 'tool') expect(turn.invocation.tool_id).toBe('monster')
  })
})

describe('a live turn and its reload are the same turn', () => {
  const response: ChatResponse = {
    answer: CREATIVE_ANSWER.text,
    sources: [],
    answerable: false,
    stat_block: { ...CREATIVE_ANSWER.stat_block },
    // Echo fields the timeline never keeps, and must not change the lane.
    mode: 'gm',
    conversation_id: 'cnv_1',
  }

  it('maps /chat’s answer onto the timeline’s outcome, field for field', () => {
    const [hydrated] = turnsFromTimeline([chatEntry()])
    const live = turnFromExchange({ id: 7, prompt: 'Give me a drowned guardian for the marsh.', status: 'done', response })
    expect(hydrated.kind).toBe('chat')
    expect(live.kind).toBe('chat')
    if (hydrated.kind !== 'chat' || live.kind !== 'chat') return
    expect(live.prompt).toBe(hydrated.prompt)
    expect(live.answer).toEqual({
      state: 'answered',
      answer: answerFromResponse(response),
    })
    if (hydrated.answer.state !== 'answered') throw new Error('expected an answer')
    expect(answerFromResponse(response)).toEqual({
      text: hydrated.answer.answer.text,
      answerable: hydrated.answer.answer.answerable,
      sources: hydrated.answer.answer.sources,
      spell_content: undefined,
      stat_block: hydrated.answer.answer.stat_block,
    })
  })

  it('maps the pending and failed stages of a live turn', () => {
    expect(turnFromExchange({ id: 1, prompt: 'q', status: 'pending' })).toEqual({
      kind: 'chat', key: 'live:1', prompt: 'q', answer: { state: 'pending' },
    })
    expect(turnFromExchange({ id: 2, prompt: 'q', status: 'error', error: 'Answer failed.' })).toEqual({
      kind: 'chat', key: 'live:2', prompt: 'q', answer: { state: 'failed', message: 'Answer failed.' },
    })
  })
})

describe('exchangesForExport — a GM export keeps its history', () => {
  it('exports stored plain turns and leaves the rest out', () => {
    const exported: Exchange[] = exchangesForExport([
      chatEntry({ entry_id: 'ent_a' }),
      toolEntry(),
      chatEntry({ entry_id: 'ent_b', answer: null, prompt: 'Still running?' }),
    ])
    expect(exported).toEqual([
      {
        id: 0,
        prompt: 'Give me a drowned guardian for the marsh.',
        status: 'done',
        response: { answer: CREATIVE_ANSWER.text, sources: [], answerable: false },
      },
      { id: 1, prompt: 'Still running?', status: 'done' },
    ])
  })
})

describe('useGmTimeline', () => {
  it('reads nothing outside the GM channel', () => {
    const load = vi.fn<LoadTimelinePageFn>()
    const { result } = renderHook(() => useGmTimeline('cnv_1', false, load))
    expect(result.current).toEqual({ items: [], loading: false, error: null })
    expect(load).not.toHaveBeenCalled()
  })

  it('reads nothing before a conversation exists', () => {
    const load = vi.fn<LoadTimelinePageFn>()
    renderHook(() => useGmTimeline(null, true, load))
    expect(load).not.toHaveBeenCalled()
  })

  it('is loading, then holds the thread oldest first', async () => {
    const load = pagedTimeline([[chatEntry({ entry_id: 'ent_2' }), chatEntry({ entry_id: 'ent_1' })]])
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    expect(result.current.loading).toBe(true)
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(ids(result.current.items)).toEqual(['ent_1', 'ent_2'])
    expect(result.current.error).toBeNull()
  })

  it('reports a failed read and holds no stale entries', async () => {
    const load: LoadTimelinePageFn = async () => ({ kind: 'error', message: 'Message history unavailable (503).' })
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.error).toBe('Message history unavailable (503).'))
    expect(result.current.items).toEqual([])
  })

  it('treats a rejecting loader as a failed read', async () => {
    const load: LoadTimelinePageFn = async () => {
      throw new Error('boom')
    }
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.error).toBe('boom'))
  })

  it('never shows one conversation’s thread under another', async () => {
    const load: LoadTimelinePageFn = async (conversationId) => ({
      kind: 'ok',
      page: { conversation_id: conversationId, items: [chatEntry({ entry_id: `ent_${conversationId}` })], next_cursor: null },
    })
    const { result, rerender } = renderHook(({ id }) => useGmTimeline(id, true, load), {
      initialProps: { id: 'cnv_a' },
    })
    await waitFor(() => expect(ids(result.current.items)).toEqual(['ent_cnv_a']))
    rerender({ id: 'cnv_b' })
    expect(result.current).toEqual({ items: [], loading: true, error: null })
    await waitFor(() => expect(ids(result.current.items)).toEqual(['ent_cnv_b']))
  })
})

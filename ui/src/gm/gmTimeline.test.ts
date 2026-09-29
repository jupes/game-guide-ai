/**
 * The GM thread's model (1kg.3.4): the paging walk, the mapping of stored
 * entries and live turns onto one `GmTurn`, and the hook that reads a thread.
 */

import { describe, it, expect, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ChatResponse, TimelinePageResult } from '../api'
import type { Exchange } from '../useChat'
import {
  HYDRATE_TARGET,
  MAX_PAGES_PER_READ,
  answerFromResponse,
  exchangesForExport,
  mergeEarlierItems,
  readEarlierTimeline,
  readTimeline,
  turnFromExchange,
  turnsFromTimeline,
  useGmTimeline,
} from './gmTimeline'
import type { LoadTimelinePageFn } from './gmTimeline'
import type { TimelineItem } from './contracts'
import {
  CREATIVE_ANSWER,
  DIVIDER_ENTRY,
  EDIT_ENTRY,
  OPAQUE_ENTRY,
  chatEntry,
  manyChatEntries,
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
    expect(read).toEqual({ kind: 'ok', items: [], cursor: null })
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

describe('readEarlierTimeline — Load earlier continues the same walk from a kept cursor (1kg.3.6)', () => {
  it('starts from the given cursor and keeps following next_cursor through a short page', async () => {
    const load = pagedTimeline([
      [chatEntry({ entry_id: 'ent_new' })],
      [chatEntry({ entry_id: 'ent_mid' })],
      [chatEntry({ entry_id: 'ent_old' })],
    ])
    const read = await readEarlierTimeline('cnv_1', 'p1', load)
    expect(load.cursors).toEqual(['p1', 'p2'])
    expect(read.kind).toBe('ok')
    if (read.kind !== 'ok') return
    expect(ids(read.items)).toEqual(['ent_old', 'ent_mid'])
    expect(read.cursor).toBeNull()
  })

  it('stops once it holds a full page, leaving a cursor to resume the walk from', async () => {
    const load = pagedTimeline([manyChatEntries(HYDRATE_TARGET, 100), [chatEntry({ entry_id: 'ent_oldest' })]])
    const read = await readEarlierTimeline('cnv_1', 'p0', load)
    expect(load.cursors).toEqual(['p0'])
    expect(read.kind).toBe('ok')
    if (read.kind !== 'ok') return
    expect(read.items).toHaveLength(HYDRATE_TARGET)
    expect(read.cursor).toBe('p1')
  })

  it('follows next_cursor through an empty page: only a null cursor ends the list (review M4)', async () => {
    const load = pagedTimeline([[chatEntry({ entry_id: 'ent_new' })], [], [chatEntry({ entry_id: 'ent_old' })]])
    const read = await readEarlierTimeline('cnv_1', 'p1', load)
    expect(load.cursors).toEqual(['p1', 'p2'])
    expect(read.kind).toBe('ok')
    if (read.kind !== 'ok') return
    expect(ids(read.items)).toEqual(['ent_old'])
    expect(read.cursor).toBeNull()
  })

  it('passes a failed page through as the error, leaving nothing to merge', async () => {
    const read = await readEarlierTimeline(
      'cnv_1',
      'p1',
      async () => ({ kind: 'error', message: 'Message history unavailable (503).' }),
    )
    expect(read).toEqual({ kind: 'error', message: 'Message history unavailable (503).' })
  })
})

describe('mergeEarlierItems — never draws a turn already on screen (1kg.3.6)', () => {
  it('prepends older items above what is already drawn, oldest first', () => {
    const existing = [chatEntry({ entry_id: 'ent_new' })]
    const older = [chatEntry({ entry_id: 'ent_old' })]
    expect(ids(mergeEarlierItems(existing, older))).toEqual(['ent_old', 'ent_new'])
  })

  it('drops an older item that duplicates one already drawn', () => {
    const existing = [chatEntry({ entry_id: 'ent_dup' }), chatEntry({ entry_id: 'ent_new' })]
    const older = [chatEntry({ entry_id: 'ent_dup' }), chatEntry({ entry_id: 'ent_old' })]
    expect(ids(mergeEarlierItems(existing, older))).toEqual(['ent_old', 'ent_dup', 'ent_new'])
  })

  it('never treats two unreadable entries with no id as duplicates of each other', () => {
    const noId: TimelineItem = { kind: 'unknown', reason: 'invalid', entry_id: null }
    expect(mergeEarlierItems([noId], [noId])).toHaveLength(2)
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
    expect(result.current).toEqual({
      items: [],
      loading: false,
      error: null,
      hasEarlier: false,
      loadingEarlier: false,
      earlierError: null,
      loadEarlier: expect.any(Function),
    })
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
    expect(result.current).toEqual({
      items: [],
      loading: true,
      error: null,
      hasEarlier: false,
      loadingEarlier: false,
      earlierError: null,
      loadEarlier: expect.any(Function),
    })
    await waitFor(() => expect(ids(result.current.items)).toEqual(['ent_cnv_b']))
  })
})

describe('useGmTimeline — Load earlier (1kg.3.6)', () => {
  /** A hydrate that stops at HYDRATE_TARGET, leaving `older` behind it. */
  function longThread(older: readonly (readonly TimelineItem[])[]) {
    return pagedTimeline([manyChatEntries(HYDRATE_TARGET, 9000), ...older])
  }

  it('offers nothing to load once a thread fits in one page', async () => {
    const load = pagedTimeline([[chatEntry()]])
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.hasEarlier).toBe(false)
  })

  it('is true once the initial read stops with entries left behind it', async () => {
    const load = longThread([[chatEntry({ entry_id: 'ent_oldest' })]])
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.hasEarlier).toBe(true)
  })

  it('prepends older turns above what is already drawn, and clears hasEarlier once the list ends', async () => {
    const load = longThread([[chatEntry({ entry_id: 'ent_oldest' })]])
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.items).toHaveLength(HYDRATE_TARGET)

    act(() => result.current.loadEarlier())
    expect(result.current.loadingEarlier).toBe(true)
    await waitFor(() => expect(result.current.loadingEarlier).toBe(false))

    expect(result.current.items).toHaveLength(HYDRATE_TARGET + 1)
    expect(ids(result.current.items)).toContain('ent_oldest')
    // Oldest first — the newly-loaded turn leads the thread.
    expect(result.current.items[0]).toMatchObject({ value: { entry_id: 'ent_oldest' } })
    expect(result.current.hasEarlier).toBe(false)
    expect(result.current.earlierError).toBeNull()
  })

  it('retries in place on a failed walk: the same cursor, and items untouched meanwhile', async () => {
    let failNext = true
    const load = vi.fn<LoadTimelinePageFn>(async (conversationId, cursor) => {
      if (cursor === null) {
        return { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 9500), next_cursor: 'p1' } }
      }
      if (failNext) {
        failNext = false
        return { kind: 'error', message: 'Message history unavailable (503).' }
      }
      return { kind: 'ok', page: { conversation_id: conversationId, items: [chatEntry({ entry_id: 'ent_oldest' })], next_cursor: null } }
    })
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => result.current.loadEarlier())
    await waitFor(() => expect(result.current.earlierError).toBe('Message history unavailable (503).'))
    // STATE-1: a failed Load earlier never blanks what is already on screen.
    expect(result.current.items).toHaveLength(HYDRATE_TARGET)
    expect(result.current.hasEarlier).toBe(true)

    act(() => result.current.loadEarlier())
    await waitFor(() => expect(result.current.items).toHaveLength(HYDRATE_TARGET + 1))
    expect(result.current.earlierError).toBeNull()
    // Both attempts resumed from the same kept cursor — a retry, not a
    // second hop past it.
    expect(load.mock.calls.filter(([, cursor]) => cursor === 'p1')).toHaveLength(2)
  })

  it('drops a stale answer once the conversation switches before it resolves', async () => {
    let resolveEarlier: ((r: TimelinePageResult) => void) | null = null
    const loadA: LoadTimelinePageFn = async (conversationId, cursor) => {
      if (cursor === null) {
        return { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 9800), next_cursor: 'p1' } }
      }
      return new Promise((resolve) => {
        resolveEarlier = resolve
      })
    }
    const loadB = pagedTimeline([[chatEntry({ entry_id: 'ent_b' })]])
    const { result, rerender } = renderHook(
      ({ id, load }: { id: string; load: LoadTimelinePageFn }) => useGmTimeline(id, true, load),
      { initialProps: { id: 'cnv_a', load: loadA as LoadTimelinePageFn } },
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.loadEarlier())
    expect(result.current.loadingEarlier).toBe(true)

    rerender({ id: 'cnv_b', load: loadB })
    await waitFor(() => expect(ids(result.current.items)).toEqual(['ent_b']))

    await act(async () => {
      resolveEarlier?.({
        kind: 'ok',
        page: { conversation_id: 'cnv_a', items: [chatEntry({ entry_id: 'ent_old_a' })], next_cursor: null },
      })
    })
    // The stale answer for cnv_a never lands on cnv_b's thread.
    expect(ids(result.current.items)).toEqual(['ent_b'])
  })

  it('does nothing while the list has already ended', async () => {
    const load = pagedTimeline([[chatEntry()]])
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.loading).toBe(false))
    const callsBefore = load.cursors.length

    act(() => result.current.loadEarlier())

    expect(load.cursors).toHaveLength(callsBefore)
    expect(result.current.loadingEarlier).toBe(false)
  })

  it('walks back a second hop from the cursor the first hop kept (review H1)', async () => {
    const load = pagedTimeline([
      manyChatEntries(HYDRATE_TARGET, 9000),
      manyChatEntries(HYDRATE_TARGET, 9100),
      [chatEntry({ entry_id: 'ent_oldest' })],
    ])
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => result.current.loadEarlier())
    await waitFor(() => expect(result.current.items).toHaveLength(2 * HYDRATE_TARGET))
    expect(result.current.hasEarlier).toBe(true)

    act(() => result.current.loadEarlier())
    await waitFor(() => expect(result.current.loadingEarlier).toBe(false))
    // Each hop resumed from where the one before it stopped — never from the
    // first kept cursor again.
    expect(load.cursors).toEqual([null, 'p1', 'p2'])
    expect(result.current.items).toHaveLength(2 * HYDRATE_TARGET + 1)
    expect(result.current.items[0]).toMatchObject({ value: { entry_id: 'ent_oldest' } })
    expect(result.current.hasEarlier).toBe(false)
  })

  it('starts one walk however often it is pressed while one is out (review M3)', async () => {
    let resolveEarlier: ((r: TimelinePageResult) => void) | null = null
    const load = vi.fn<LoadTimelinePageFn>(async (conversationId, cursor) => {
      if (cursor === null) {
        return { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 9000), next_cursor: 'p1' } }
      }
      return new Promise<TimelinePageResult>((resolve) => {
        resolveEarlier = resolve
      })
    })
    const { result } = renderHook(() => useGmTimeline('cnv_1', true, load))
    await waitFor(() => expect(result.current.loading).toBe(false))

    act(() => {
      result.current.loadEarlier()
      result.current.loadEarlier()
    })
    // And again after the rerender, through the freshly bound callback.
    act(() => result.current.loadEarlier())
    expect(load.mock.calls.filter(([, cursor]) => cursor === 'p1')).toHaveLength(1)

    await act(async () => {
      resolveEarlier?.({
        kind: 'ok',
        page: { conversation_id: 'cnv_1', items: [chatEntry({ entry_id: 'ent_oldest' })], next_cursor: null },
      })
    })
    expect(result.current.items).toHaveLength(HYDRATE_TARGET + 1)
  })

  it('keeps the switched-to conversation’s own cursor when an old walk resolves late (review M3)', async () => {
    let resolveA: ((r: TimelinePageResult) => void) | null = null
    const loadA: LoadTimelinePageFn = async (conversationId, cursor) => {
      if (cursor === null) {
        return { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 9000), next_cursor: 'a1' } }
      }
      return new Promise((resolve) => {
        resolveA = resolve
      })
    }
    const loadB = vi.fn<LoadTimelinePageFn>(async (conversationId, cursor) => ({
      kind: 'ok',
      page:
        cursor === null
          ? { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 5000), next_cursor: 'b1' }
          : { conversation_id: conversationId, items: [chatEntry({ entry_id: 'ent_b_oldest' })], next_cursor: null },
    }))
    const { result, rerender } = renderHook(
      ({ id, load }: { id: string; load: LoadTimelinePageFn }) => useGmTimeline(id, true, load),
      { initialProps: { id: 'cnv_a', load: loadA } },
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.loadEarlier())

    rerender({ id: 'cnv_b', load: loadB })
    await waitFor(() => expect(ids(result.current.items)).toContain('ent_5000'))

    // cnv_a's walk lands now: a full page, so it stops there, carrying cnv_a's
    // next cursor.
    await act(async () => {
      resolveA?.({
        kind: 'ok',
        page: { conversation_id: 'cnv_a', items: manyChatEntries(HYDRATE_TARGET, 7000), next_cursor: 'a2' },
      })
    })
    expect(ids(result.current.items)).not.toContain('ent_7000')

    act(() => result.current.loadEarlier())
    await waitFor(() => expect(ids(result.current.items)).toContain('ent_b_oldest'))
    // cnv_b's Load earlier resumed from cnv_b's cursor — cnv_a's never
    // reached cnv_b's timeline.
    expect(loadB.mock.calls.map(([, cursor]) => cursor)).toEqual([null, 'b1'])
  })

  it('drops a walk from an earlier visit once the same conversation is read again (review M1)', async () => {
    // Visit 1 draws ent_100 (newest) .. ent_199 and keeps p1 behind them. By
    // visit 2, three newer turns have pushed ent_197..ent_199 out of the
    // window and behind a new cursor, q1 — reachable only from q1.
    let visits = 0
    let resolveStale: ((r: TimelinePageResult) => void) | null = null
    let resolveFresh: ((r: TimelinePageResult) => void) | null = null
    const loadA = vi.fn<LoadTimelinePageFn>(async (conversationId, cursor) => {
      if (cursor === null) {
        visits += 1
        return visits === 1
          ? { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 100), next_cursor: 'p1' } }
          : { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 97), next_cursor: 'q1' } }
      }
      return new Promise<TimelinePageResult>((resolve) => {
        if (cursor === 'p1') resolveStale = resolve
        else resolveFresh = resolve
      })
    })
    const loadB = pagedTimeline([[chatEntry({ entry_id: 'ent_b' })]])
    const { result, rerender } = renderHook(
      ({ id, load }: { id: string; load: LoadTimelinePageFn }) => useGmTimeline(id, true, load),
      { initialProps: { id: 'cnv_a', load: loadA as LoadTimelinePageFn } },
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.loadEarlier())

    rerender({ id: 'cnv_b', load: loadB })
    await waitFor(() => expect(ids(result.current.items)).toEqual(['ent_b']))
    rerender({ id: 'cnv_a', load: loadA })
    await waitFor(() => expect(ids(result.current.items)).toContain('ent_97'))

    act(() => result.current.loadEarlier())
    expect(result.current.loadingEarlier).toBe(true)

    // Visit 1's walk lands now — after visit 2's read, in the same scope.
    await act(async () => {
      resolveStale?.({
        kind: 'ok',
        page: { conversation_id: 'cnv_a', items: [chatEntry({ entry_id: 'ent_200' })], next_cursor: null },
      })
    })
    expect(result.current.items).toHaveLength(HYDRATE_TARGET)
    expect(ids(result.current.items)).not.toContain('ent_200')
    expect(result.current.hasEarlier).toBe(true)
    // Visit 2's own walk is still out, and still the only one.
    expect(result.current.loadingEarlier).toBe(true)
    act(() => result.current.loadEarlier())
    expect(loadA.mock.calls.filter(([, cursor]) => cursor === 'q1')).toHaveLength(1)

    await act(async () => {
      resolveFresh?.({
        kind: 'ok',
        page: {
          conversation_id: 'cnv_a',
          items: [...manyChatEntries(3, 197), chatEntry({ entry_id: 'ent_200' })],
          next_cursor: null,
        },
      })
    })
    expect(result.current.items).toHaveLength(HYDRATE_TARGET + 4)
    expect(ids(result.current.items).slice(0, 4)).toEqual(['ent_200', 'ent_199', 'ent_198', 'ent_197'])
    expect(result.current.hasEarlier).toBe(false)
  })

  it('drops a walk from an earlier visit that rejects once the same conversation is read again (PR #136 review L1)', async () => {
    // The same A→B→A shape, but visit 1's walk rejects (an injected loader can
    // throw; getTimelinePage never does). Its failure is not visit 2's: no
    // error, and visit 2's walk keeps the guard it holds.
    let visits = 0
    let rejectStale: ((reason: Error) => void) | null = null
    let resolveFresh: ((r: TimelinePageResult) => void) | null = null
    const loadA = vi.fn<LoadTimelinePageFn>(async (conversationId, cursor) => {
      if (cursor === null) {
        visits += 1
        return visits === 1
          ? { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 100), next_cursor: 'p1' } }
          : { kind: 'ok', page: { conversation_id: conversationId, items: manyChatEntries(HYDRATE_TARGET, 97), next_cursor: 'q1' } }
      }
      return new Promise<TimelinePageResult>((resolve, reject) => {
        if (cursor === 'p1') rejectStale = reject
        else resolveFresh = resolve
      })
    })
    const loadB = pagedTimeline([[chatEntry({ entry_id: 'ent_b' })]])
    const { result, rerender } = renderHook(
      ({ id, load }: { id: string; load: LoadTimelinePageFn }) => useGmTimeline(id, true, load),
      { initialProps: { id: 'cnv_a', load: loadA as LoadTimelinePageFn } },
    )
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.loadEarlier())

    rerender({ id: 'cnv_b', load: loadB })
    await waitFor(() => expect(ids(result.current.items)).toEqual(['ent_b']))
    rerender({ id: 'cnv_a', load: loadA })
    await waitFor(() => expect(ids(result.current.items)).toContain('ent_97'))

    act(() => result.current.loadEarlier())
    expect(result.current.loadingEarlier).toBe(true)

    // Visit 1's walk fails now — after visit 2's read, in the same scope.
    await act(async () => {
      rejectStale?.(new Error('Visit 1 lost its connection.'))
    })
    expect(result.current.earlierError).toBeNull()
    expect(result.current.loadingEarlier).toBe(true)
    act(() => result.current.loadEarlier())
    expect(loadA.mock.calls.filter(([, cursor]) => cursor === 'q1')).toHaveLength(1)

    await act(async () => {
      resolveFresh?.({
        kind: 'ok',
        page: { conversation_id: 'cnv_a', items: [...manyChatEntries(3, 197), chatEntry({ entry_id: 'ent_200' })], next_cursor: null },
      })
    })
    expect(result.current.items).toHaveLength(HYDRATE_TARGET + 4)
    expect(ids(result.current.items).slice(0, 4)).toEqual(['ent_200', 'ent_199', 'ent_198', 'ent_197'])
    expect(result.current.earlierError).toBeNull()
    expect(result.current.hasEarlier).toBe(false)
  })
})

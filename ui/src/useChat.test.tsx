import { describe, it, expect } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useChat } from './useChat'
import type { ChatResult, ChatMode, MessagesResult, StoredMessage } from './api'
import type { LoadHistoryFn, PostFn } from './useChat'

const GROUNDED: ChatResult = {
  kind: 'ok',
  response: {
    answer: 'A basilisk petrifies with its gaze [1].',
    sources: [],
    answerable: true,
  },
}

function deferredPost() {
  let resolve!: (r: ChatResult) => void
  const promise = new Promise<ChatResult>((r) => {
    resolve = r
  })
  const post: PostFn = () => promise
  return { post, resolve }
}

describe('useChat', () => {
  it('appends a pending exchange then resolves it to done', async () => {
    const { post, resolve } = deferredPost()
    const { result } = renderHook(() => useChat({ post, mode: 'sage', conversationId: null }))

    act(() => {
      result.current.send('What is a Basilisk?')
    })
    expect(result.current.exchanges).toHaveLength(1)
    expect(result.current.exchanges[0].status).toBe('pending')
    expect(result.current.pending).toBe(true)

    act(() => resolve(GROUNDED))
    await waitFor(() => expect(result.current.exchanges[0].status).toBe('done'))
    expect(result.current.exchanges[0].response?.answer).toMatch(/basilisk/i)
    expect(result.current.pending).toBe(false)
  })

  it('resolves to error on a failed result', async () => {
    const post: PostFn = async () => ({ kind: 'error', message: 'Service unavailable' })
    const { result } = renderHook(() => useChat({ post, mode: 'sage', conversationId: null }))

    act(() => {
      result.current.send('Q')
    })
    await waitFor(() => expect(result.current.exchanges[0].status).toBe('error'))
    expect(result.current.exchanges[0].error).toMatch(/unavailable/i)
  })

  it('marks the exchange errored (and unlocks) if post() rejects', async () => {
    const post: PostFn = () => Promise.reject(new Error('boom'))
    const { result } = renderHook(() => useChat({ post, mode: 'sage', conversationId: null }))

    act(() => {
      result.current.send('Q')
    })
    await waitFor(() => expect(result.current.exchanges[0].status).toBe('error'))
    expect(result.current.pending).toBe(false)

    // Composer must accept a follow-up send after the rejection.
    act(() => {
      result.current.send('second')
    })
    expect(result.current.exchanges).toHaveLength(2)
  })

  it('ignores sends while a request is pending (no double-submit)', async () => {
    const { post, resolve } = deferredPost()
    const { result } = renderHook(() => useChat({ post, mode: 'sage', conversationId: null }))

    act(() => {
      result.current.send('first')
      result.current.send('second — must be ignored')
    })
    expect(result.current.exchanges).toHaveLength(1)

    act(() => resolve(GROUNDED))
    await waitFor(() => expect(result.current.pending).toBe(false))
  })

  it('ignores empty / whitespace-only prompts', () => {
    const post: PostFn = async () => GROUNDED
    const { result } = renderHook(() => useChat({ post, mode: 'sage', conversationId: null }))
    act(() => {
      result.current.send('   ')
    })
    expect(result.current.exchanges).toHaveLength(0)
  })

  // ── channel-chats CP-B — history recall ────────────────────────────────────

  const stored = (id: number, role: 'user' | 'assistant', content: string): StoredMessage => ({
    id,
    role,
    content,
    mode: 'sage',
    created_at: '2026-07-08T12:00:00Z',
  })

  const historyOf =
    (byConv: Record<string, StoredMessage[]>): LoadHistoryFn =>
    async (conversationId) => ({ kind: 'ok', messages: byConv[conversationId] ?? [] })

  it('loads stored history when a conversation opens', async () => {
    const post: PostFn = async () => GROUNDED
    const loadHistory = historyOf({
      'conv-1': [stored(1, 'user', 'First question'), stored(2, 'assistant', 'First answer')],
    })
    const { result } = renderHook(() =>
      useChat({ post, loadHistory, mode: 'sage', conversationId: 'conv-1' }),
    )

    await waitFor(() => expect(result.current.exchanges).toHaveLength(1))
    expect(result.current.exchanges[0].prompt).toBe('First question')
    expect(result.current.exchanges[0].status).toBe('done')
    expect(result.current.exchanges[0].response?.answer).toBe('First answer')
  })

  it('swaps history when conversationId changes', async () => {
    const post: PostFn = async () => GROUNDED
    const loadHistory = historyOf({
      'conv-1': [stored(1, 'user', 'About goblins'), stored(2, 'assistant', 'Goblins…')],
      'conv-2': [stored(3, 'user', 'About dragons'), stored(4, 'assistant', 'Dragons…')],
    })
    const { result, rerender } = renderHook(
      ({ convId }: { convId: string | null }) =>
        useChat({ post, loadHistory, mode: 'sage', conversationId: convId }),
      { initialProps: { convId: 'conv-1' as string | null } },
    )

    await waitFor(() => expect(result.current.exchanges[0]?.prompt).toBe('About goblins'))

    rerender({ convId: 'conv-2' })
    await waitFor(() => expect(result.current.exchanges[0]?.prompt).toBe('About dragons'))
    expect(result.current.exchanges).toHaveLength(1)
  })

  it('degrades to an empty thread with a notice when the history fetch fails', async () => {
    const post: PostFn = async () => GROUNDED
    const loadHistory: LoadHistoryFn = async () => ({
      kind: 'error',
      message: 'Message history unavailable',
    })
    const { result } = renderHook(() =>
      useChat({ post, loadHistory, mode: 'sage', conversationId: 'conv-1' }),
    )

    await waitFor(() => expect(result.current.historyError).toMatch(/unavailable/i))
    expect(result.current.exchanges).toHaveLength(0)

    // Composer still works: a send goes through as usual.
    act(() => {
      result.current.send('Still works?')
    })
    await waitFor(() => expect(result.current.exchanges).toHaveLength(1))
    expect(result.current.exchanges[0].status).toBe('done')
  })

  it('does not clobber a live exchange sent while history is loading', async () => {
    const post: PostFn = async () => GROUNDED
    let resolveHistory!: (r: MessagesResult) => void
    const loadHistory: LoadHistoryFn = () =>
      new Promise<MessagesResult>((res) => {
        resolveHistory = res
      })
    const { result } = renderHook(() =>
      useChat({ post, loadHistory, mode: 'sage', conversationId: 'conv-1' }),
    )

    act(() => {
      result.current.send('Live question')
    })
    await waitFor(() => expect(result.current.exchanges).toHaveLength(1))

    act(() =>
      resolveHistory({
        kind: 'ok',
        messages: [stored(1, 'user', 'Old question'), stored(2, 'assistant', 'Old answer')],
      }),
    )
    // Seeded history lands BEFORE the live exchange; nothing is lost.
    await waitFor(() => expect(result.current.exchanges).toHaveLength(2))
    expect(result.current.exchanges[0].prompt).toBe('Old question')
    expect(result.current.exchanges[1].prompt).toBe('Live question')
  })

  it('skips an orphan assistant row (user turn cut off by the load limit)', async () => {
    const post: PostFn = async () => GROUNDED
    const loadHistory = historyOf({
      'conv-1': [
        stored(1, 'assistant', 'Orphan answer'),
        stored(2, 'user', 'Question'),
        stored(3, 'assistant', 'Answer'),
      ],
    })
    const { result } = renderHook(() =>
      useChat({ post, loadHistory, mode: 'sage', conversationId: 'conv-1' }),
    )

    await waitFor(() => expect(result.current.exchanges).toHaveLength(1))
    expect(result.current.exchanges[0].prompt).toBe('Question')
  })

  it('calls post with the correct mode and conversationId', async () => {
    const calls: Array<[string, ChatMode, string | null]> = []
    const post: PostFn = async (prompt, mode, conversationId) => {
      calls.push([prompt, mode, conversationId])
      return GROUNDED
    }
    const { result } = renderHook(() => useChat({ post, mode: 'spell', conversationId: 'conv-xyz' }))

    act(() => {
      result.current.send('Cast fireball')
    })
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0]).toEqual(['Cast fireball', 'spell', 'conv-xyz'])
  })
})

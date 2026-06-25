import { describe, it, expect } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useChat } from './useChat'
import type { ChatResult, ChatMode } from './api'
import type { PostFn } from './useChat'

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

  it('clears exchanges when conversationId changes', async () => {
    const post: PostFn = async () => GROUNDED
    const { result, rerender } = renderHook(
      ({ convId }: { convId: string | null }) =>
        useChat({ post, mode: 'sage', conversationId: convId }),
      { initialProps: { convId: 'conv-1' } },
    )

    act(() => {
      result.current.send('First question')
    })
    await waitFor(() => expect(result.current.exchanges).toHaveLength(1))

    rerender({ convId: 'conv-2' })
    await waitFor(() => expect(result.current.exchanges).toHaveLength(0))
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

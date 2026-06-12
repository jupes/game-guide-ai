import { describe, it, expect } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useChat } from './useChat'
import type { ChatResult } from './api'

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
  const post = () => promise
  return { post, resolve }
}

describe('useChat', () => {
  it('appends a pending exchange then resolves it to done', async () => {
    const { post, resolve } = deferredPost()
    const { result } = renderHook(() => useChat(post))

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
    const post = async (): Promise<ChatResult> => ({ kind: 'error', message: 'Service unavailable' })
    const { result } = renderHook(() => useChat(post))

    act(() => {
      result.current.send('Q')
    })
    await waitFor(() => expect(result.current.exchanges[0].status).toBe('error'))
    expect(result.current.exchanges[0].error).toMatch(/unavailable/i)
  })

  it('ignores sends while a request is pending (no double-submit)', async () => {
    const { post, resolve } = deferredPost()
    const { result } = renderHook(() => useChat(post))

    act(() => {
      result.current.send('first')
      result.current.send('second — must be ignored')
    })
    expect(result.current.exchanges).toHaveLength(1)

    act(() => resolve(GROUNDED))
    await waitFor(() => expect(result.current.pending).toBe(false))
  })

  it('ignores empty / whitespace-only prompts', () => {
    const post = async (): Promise<ChatResult> => GROUNDED
    const { result } = renderHook(() => useChat(post))
    act(() => {
      result.current.send('   ')
    })
    expect(result.current.exchanges).toHaveLength(0)
  })
})

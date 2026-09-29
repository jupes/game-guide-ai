/**
 * useChat's `inFlight` (1kg.3.5, I-14): whether the hook has a request out,
 * in ANY conversation. `pending` is the visible conversation's alone, so after
 * a switch it reads false while `send` would still refuse; `inFlight` is what
 * a composer asks before offering Send. It is additive: nothing else the hook
 * returns changes (useChat.test.tsx stays as it was).
 */

import { describe, it, expect } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useChat } from './useChat'
import type { ChatResult, ChatMode, MessagesResult } from './api'
import type { LoadHistoryFn, PostFn } from './useChat'

const ANSWER: ChatResult = { kind: 'ok', response: { answer: 'A basilisk.', sources: [], answerable: true } }
const EMPTY_HISTORY: LoadHistoryFn = async (): Promise<MessagesResult> => ({ kind: 'ok', messages: [] })

/** A `post` whose every call waits until the test settles it. */
function heldPost() {
  const calls: { prompt: string; resolve: (r: ChatResult) => void; reject: (e: unknown) => void }[] = []
  const post: PostFn = (prompt) =>
    new Promise<ChatResult>((resolve, reject) => {
      calls.push({ prompt, resolve, reject })
    })
  return { post, calls }
}

interface Props {
  conversationId: string | null
  mode: ChatMode
}

function renderChat(post: PostFn, initial: Props = { conversationId: 'cnv_1', mode: 'gm' }) {
  return renderHook((props: Props) => useChat({ post, loadHistory: EMPTY_HISTORY, ...props }), {
    initialProps: initial,
  })
}

describe('useChat — inFlight (1kg.3.5)', () => {
  it('is false at rest, and every other return value is as it was', async () => {
    const { result } = renderChat(heldPost().post)
    await waitFor(() => expect(result.current.loadingHistory).toBe(false))
    expect(result.current.inFlight).toBe(false)
    expect(Object.keys(result.current).sort()).toEqual(
      ['exchanges', 'historyError', 'inFlight', 'loadingHistory', 'pending', 'send'].sort(),
    )
  })

  it('is true from an accepted send until it settles with an answer', async () => {
    const held = heldPost()
    const { result } = renderChat(held.post)
    act(() => result.current.send('What is a basilisk?'))
    expect(result.current.inFlight).toBe(true)
    expect(result.current.pending).toBe(true)
    await act(async () => held.calls[0].resolve(ANSWER))
    expect(result.current.inFlight).toBe(false)
    expect(result.current.exchanges.at(-1)?.status).toBe('done')
  })

  it('is cleared by a failed answer', async () => {
    const held = heldPost()
    const { result } = renderChat(held.post)
    act(() => result.current.send('q'))
    expect(result.current.inFlight).toBe(true)
    await act(async () => held.calls[0].resolve({ kind: 'error', message: 'Service unavailable' }))
    expect(result.current.inFlight).toBe(false)
    expect(result.current.exchanges.at(-1)?.status).toBe('error')
  })

  it('is cleared by a rejected post, an aborted one included', async () => {
    const held = heldPost()
    const { result } = renderChat(held.post)
    act(() => result.current.send('q'))
    const aborted = new Error('The operation was aborted.')
    aborted.name = 'AbortError'
    await act(async () => held.calls[0].reject(aborted))
    expect(result.current.inFlight).toBe(false)
  })

  it('stays true across a conversation switch, and clears only when the settle arrives (not shown)', async () => {
    const held = heldPost()
    const { result, rerender } = renderChat(held.post)
    act(() => result.current.send('asked in cnv_1'))
    rerender({ conversationId: 'cnv_2', mode: 'gm' })
    // At once, before cnv_2's recall lands, and after it.
    expect(result.current.pending).toBe(false)
    expect(result.current.inFlight).toBe(true)
    await waitFor(() => expect(result.current.loadingHistory).toBe(false))
    // cnv_2 has nothing pending, but this hook still has a request out.
    expect(result.current.pending).toBe(false)
    expect(result.current.inFlight).toBe(true)
    // So a send from cnv_2 is refused, and nothing changes.
    act(() => result.current.send('asked in cnv_2'))
    expect(held.calls).toHaveLength(1)
    expect(result.current.exchanges).toEqual([])
    // cnv_1's turn settles off screen: the flag clears all the same.
    await act(async () => held.calls[0].resolve(ANSWER))
    expect(result.current.inFlight).toBe(false)
    expect(result.current.exchanges).toEqual([])
    // And cnv_2 can now send.
    act(() => result.current.send('asked in cnv_2'))
    expect(held.calls.map((call) => call.prompt)).toEqual(['asked in cnv_1', 'asked in cnv_2'])
    expect(result.current.inFlight).toBe(true)
  })

  it('never flips for a send it refuses: an empty prompt, or one while a turn is out', async () => {
    const held = heldPost()
    const { result } = renderChat(held.post)
    act(() => result.current.send('   '))
    expect(result.current.inFlight).toBe(false)
    expect(held.calls).toHaveLength(0)
    act(() => result.current.send('first'))
    act(() => result.current.send('second'))
    expect(held.calls).toHaveLength(1)
    await act(async () => held.calls[0].resolve(ANSWER))
    // The refused second send left nothing behind to keep the flag up.
    expect(result.current.inFlight).toBe(false)
  })
})

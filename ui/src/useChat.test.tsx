import { describe, it, expect, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { useChat } from './useChat'
import type { ChatResult, ChatMode, MessagesResult, StoredMessage } from './api'
import type { MetricPoint } from './metrics/metrics'
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
  // ── Adopting a server-minted conversation id (x5bz.3.2) ───────────────────
  // Sending with a null id used to mean "this turn goes unrecorded". The server
  // now mints one and persists under it, so a client that discards it opens a
  // fresh conversation on every message and the user can never get back to any
  // of them.

  it('adopts the conversation id the server minted when it had none', async () => {
    const adopted: string[] = []
    const post: PostFn = async () => ({
      kind: 'ok',
      response: { ...GROUNDED.kind === 'ok' ? GROUNDED.response : {}, conversation_id: 'srv-9f2' },
    }) as ChatResult
    const { result } = renderHook(() =>
      useChat({ post, mode: 'sage', conversationId: null,
                onConversationAdopted: (id) => adopted.push(id) }),
    )

    act(() => { result.current.send('What is a Basilisk?') })
    await waitFor(() => expect(adopted).toEqual(['srv-9f2']))
  })

  it('does not re-announce an id the client already had', async () => {
    // The server echoes whatever it was given. Treating that echo as an
    // adoption would churn navigation state on every single turn.
    const adopted: string[] = []
    const post: PostFn = async () => ({
      kind: 'ok',
      response: { ...GROUNDED.kind === 'ok' ? GROUNDED.response : {}, conversation_id: 'mine-1' },
    }) as ChatResult
    const { result } = renderHook(() =>
      useChat({ post, mode: 'sage', conversationId: 'mine-1',
                onConversationAdopted: (id) => adopted.push(id) }),
    )

    act(() => { result.current.send('and its damage?') })
    await waitFor(() => expect(result.current.exchanges[0].status).toBe('done'))
    expect(adopted).toEqual([])
  })

  // ── Healing off a retired manual pick (agent-forge-harness-j9w) ───────────
  // The server rebinds a conversation off a manual pick the catalog has
  // since retired, and says so via routing.fallback_from. onPreferenceRebound
  // is the one seam that tells the CLIENT: it is what lets ConversationStore
  // stop sending the retired id on the very next turn.

  function healedResult(fallbackFrom: string | null, effective: string): ChatResult {
    return {
      kind: 'ok',
      response: {
        ...(GROUNDED.kind === 'ok' ? GROUNDED.response : {}),
        conversation_id: 'conv-heal',
        routing: { requested: effective, effective, strategy: 'manual', fallback_from: fallbackFrom },
      },
    } as ChatResult
  }

  it('reports a heal when the response carries fallback_from', async () => {
    const rebound: Array<[string, string]> = []
    const post: PostFn = async () => healedResult('traveller', 'adventurer')
    const { result } = renderHook(() =>
      useChat({
        post, mode: 'sage', conversationId: 'conv-heal',
        onPreferenceRebound: (id, preference) => rebound.push([id, preference]),
      }),
    )

    act(() => { result.current.send('again') })
    await waitFor(() => expect(rebound).toEqual([['conv-heal', 'adventurer']]))
  })

  it('never reports a heal on an ordinary turn (fallback_from absent)', async () => {
    const rebound: Array<[string, string]> = []
    const post: PostFn = async () => healedResult(null, 'adventurer')
    const { result } = renderHook(() =>
      useChat({
        post, mode: 'sage', conversationId: 'conv-heal',
        onPreferenceRebound: (id, preference) => rebound.push([id, preference]),
      }),
    )

    act(() => { result.current.send('again') })
    await waitFor(() => expect(result.current.exchanges[0].status).toBe('done'))
    expect(rebound).toEqual([])
  })

  it('reports a heal against the server-adopted id when this turn started with none', async () => {
    // A first-ever turn can't already be a retired binding — but the seam
    // itself must not assume a non-null conversationId; it uses whatever
    // this turn actually resolved to.
    const rebound: Array<[string, string]> = []
    const post: PostFn = async () => healedResult('traveller', 'adventurer')
    const { result } = renderHook(() =>
      useChat({
        post, mode: 'sage', conversationId: null,
        onPreferenceRebound: (id, preference) => rebound.push([id, preference]),
      }),
    )

    act(() => { result.current.send('hi') })
    await waitFor(() => expect(rebound).toEqual([['conv-heal', 'adventurer']]))
  })

  it('does not throw when a heal happens with no onPreferenceRebound handler', async () => {
    const post: PostFn = async () => healedResult('traveller', 'adventurer')
    const { result } = renderHook(() => useChat({ post, mode: 'sage', conversationId: 'conv-heal' }))

    act(() => { result.current.send('again') })
    await waitFor(() => expect(result.current.exchanges[0].status).toBe('done'))
  })

  // ── Per-conversation model preference (b8o.2) ─────────────────────────────

  it('passes the modelPreference option through to post', async () => {
    const calls: unknown[][] = []
    const post: PostFn = async (...args) => {
      calls.push(args)
      return GROUNDED
    }
    const { result } = renderHook(() =>
      useChat({ post, mode: 'sage', conversationId: null, modelPreference: 'gpt-4o-mini' }),
    )
    act(() => { result.current.send('What is a Basilisk?') })
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0]).toEqual(['What is a Basilisk?', 'sage', null, 'gpt-4o-mini'])
  })

  it('defaults modelPreference to "auto" when the option is omitted', async () => {
    const calls: unknown[][] = []
    const post: PostFn = async (...args) => {
      calls.push(args)
      return GROUNDED
    }
    const { result } = renderHook(() => useChat({ post, mode: 'sage', conversationId: null }))
    act(() => { result.current.send('What is a Basilisk?') })
    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0][3]).toBe('auto')
  })

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

  it('records chat round-trip and outcome without conversation content', async () => {
    const points: MetricPoint[] = []
    const now = vi.fn().mockReturnValueOnce(10).mockReturnValueOnce(42)
    const { result } = renderHook(() =>
      useChat({
        post: async () => GROUNDED,
        loadHistory: async () => ({ kind: 'ok', messages: [] }),
        mode: 'spell',
        conversationId: 'private-conversation-id',
        now,
        recordMetric: (point) => points.push(point),
      }),
    )

    act(() => {
      result.current.send('private prompt text')
    })
    await waitFor(() => expect(result.current.pending).toBe(false))

    expect(points.map(({ name, value }) => [name, value])).toEqual([
      ['ui.interaction.chat_round_trip_ms', 32],
      ['ui.interaction.chat_outcome', 'success'],
    ])
    expect(points[0].labels.mode).toBe('spell')
    expect(JSON.stringify(points)).not.toMatch(/private prompt|private-conversation/)
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

  // ── Announcing arrival (agent-forge-harness-ekf) — additive option ────────
  // The seam ChatPane's announcer uses: fired once per turn THIS hook sent,
  // at the settle, never from the recall effect or a re-render.

  it('calls onTurnSettled with "done" on a successful post and "error" on a failed or rejecting one', async () => {
    const settled: Array<'done' | 'error'> = []
    const onTurnSettled = (outcome: 'done' | 'error') => settled.push(outcome)

    const okPost: PostFn = async () => GROUNDED
    const { result: okResult } = renderHook(() =>
      useChat({ post: okPost, mode: 'sage', conversationId: null, onTurnSettled }),
    )
    act(() => { okResult.current.send('What is a Basilisk?') })
    await waitFor(() => expect(okResult.current.exchanges[0].status).toBe('done'))

    const errorPost: PostFn = async () => ({ kind: 'error', message: 'Service unavailable' })
    const { result: errorResult } = renderHook(() =>
      useChat({ post: errorPost, mode: 'sage', conversationId: null, onTurnSettled }),
    )
    act(() => { errorResult.current.send('Q') })
    await waitFor(() => expect(errorResult.current.exchanges[0].status).toBe('error'))

    const rejectingPost: PostFn = () => Promise.reject(new Error('boom'))
    const { result: rejectingResult } = renderHook(() =>
      useChat({ post: rejectingPost, mode: 'sage', conversationId: null, onTurnSettled }),
    )
    act(() => { rejectingResult.current.send('Q') })
    await waitFor(() => expect(rejectingResult.current.exchanges[0].status).toBe('error'))

    expect(settled).toEqual(['done', 'error', 'error'])
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

  // ── settle() must not re-scope to a conversation the user left (agent-forge-harness-4pg) ──
  // A turn sent from conv-1 that settles AFTER the user has switched to (and
  // recalled) conv-2 used to stamp state.scopeId back to conv-1, stranding
  // conv-2 on "Recalling the conversation…" forever (its recall effect deps
  // hadn't changed, so it would never re-run to recover).

  it('does not re-scope to a conversation the user has left when a stale send settles', async () => {
    const { post, resolve: resolvePost } = deferredPost()
    let resolveConv2History!: (r: MessagesResult) => void
    const loadHistory: LoadHistoryFn = (conversationId) => {
      if (conversationId === 'conv-1') return Promise.resolve({ kind: 'ok', messages: [] })
      return new Promise<MessagesResult>((res) => {
        resolveConv2History = res
      })
    }

    const { result, rerender } = renderHook(
      ({ convId }: { convId: string | null }) =>
        useChat({ post, loadHistory, mode: 'sage', conversationId: convId }),
      { initialProps: { convId: 'conv-1' as string | null } },
    )
    await waitFor(() => expect(result.current.loadingHistory).toBe(false))

    // Send from conv-1; its post() is still in flight when the user switches away.
    act(() => {
      result.current.send('About goblins')
    })
    expect(result.current.exchanges).toHaveLength(1)

    // Switch to conv-2 before the goblins turn settles, then let its recall land.
    rerender({ convId: 'conv-2' })
    expect(result.current.loadingHistory).toBe(true)
    await act(async () => {
      resolveConv2History({
        kind: 'ok',
        messages: [stored(9, 'user', 'About dragons'), stored(10, 'assistant', 'Dragons…')],
      })
    })
    await waitFor(() => expect(result.current.loadingHistory).toBe(false))
    expect(result.current.exchanges[0]?.prompt).toBe('About dragons')

    // NOW the stale conv-1 turn settles — conv-2 must stay put, not strand.
    await act(async () => {
      resolvePost(GROUNDED)
    })
    expect(result.current.loadingHistory).toBe(false)
    expect(result.current.exchanges).toHaveLength(1)
    expect(result.current.exchanges[0]?.prompt).toBe('About dragons')
  })

  // ── onTurnSettled reports the SEND-TIME conversation (agent-forge-harness-swg) ──
  // pr114 M-1: settle() already drops the state WRITE for a stale turn (the
  // test above), but `onTurnSettled` used to still fire — and ChatPane has no
  // other way to tell a stale settle apart from a current one, since it is
  // never remounted on a conversation switch. Passing the conversation the
  // turn was actually sent for (not whatever the hook happens to be scoped to
  // right now) is what lets a consumer make that comparison itself.

  it('passes the send-time conversation id to onTurnSettled, even for a stale settle after the user switched away', async () => {
    const settled: Array<[string | null, 'done' | 'error']> = []
    const onTurnSettled = (outcome: 'done' | 'error', conversationId: string | null) =>
      settled.push([conversationId, outcome])

    const { post, resolve: resolvePost } = deferredPost()
    const loadHistory: LoadHistoryFn = async () => ({ kind: 'ok', messages: [] })

    const { result, rerender } = renderHook(
      ({ convId }: { convId: string | null }) =>
        useChat({ post, loadHistory, mode: 'sage', conversationId: convId, onTurnSettled }),
      { initialProps: { convId: 'conv-1' as string | null } },
    )
    await waitFor(() => expect(result.current.loadingHistory).toBe(false))

    // Sent from conv-1; its post() is still in flight when the user switches.
    act(() => {
      result.current.send('About goblins')
    })

    rerender({ convId: 'conv-2' })
    await waitFor(() => expect(result.current.loadingHistory).toBe(false))
    expect(settled).toEqual([]) // nothing settled yet — the switch alone must not fire it

    await act(async () => {
      resolvePost(GROUNDED)
    })

    // Settled for conv-1 — the conversation the turn was sent for — not
    // conv-2, which is merely what the hook is scoped to now.
    expect(settled).toEqual([['conv-1', 'done']])
  })

  // ── onTurnSettled says whether the settle was SHOWN (pr129 M-1) ───────────
  // Comparing ids is not enough: after A -> B -> A the ids match again, but
  // A's recall has replaced the exchange list, so the settle writes nothing
  // and no answer is drawn. `shown` is decided by the settle's own state
  // update (did it find and write its exchange?) and by the commit that
  // follows (is that exchange on screen?).

  it('reports shown=true for a settle drawn in the conversation on screen, and shown=false for one that is not (A -> B, A -> B -> A)', async () => {
    const shownFlags: boolean[] = []
    const onTurnSettled = (_outcome: 'done' | 'error', _conversationId: string | null, shown: boolean) =>
      shownFlags.push(shown)

    const resolvers: Array<(r: ChatResult) => void> = []
    const post: PostFn = () => new Promise<ChatResult>((res) => { resolvers.push(res) })
    const loadHistory: LoadHistoryFn = async (id) => ({
      kind: 'ok',
      messages: [
        stored(id === 'conv-1' ? 1 : 3, 'user', `Question ${id}`),
        stored(id === 'conv-1' ? 2 : 4, 'assistant', 'Stored'),
      ],
    })
    const { result, rerender } = renderHook(
      ({ convId }: { convId: string | null }) =>
        useChat({ post, loadHistory, mode: 'sage', conversationId: convId, onTurnSettled }),
      { initialProps: { convId: 'conv-1' as string | null } },
    )
    await waitFor(() => expect(result.current.loadingHistory).toBe(false))

    // 1. A current settle: drawn, shown.
    act(() => { result.current.send('first') })
    await act(async () => { resolvers[0](GROUNDED) })
    expect(result.current.exchanges.at(-1)?.status).toBe('done')
    expect(shownFlags).toEqual([true])

    // 2. A -> B, then the A turn settles: not shown.
    act(() => { result.current.send('second') })
    rerender({ convId: 'conv-2' })
    await waitFor(() => expect(result.current.exchanges[0]?.prompt).toBe('Question conv-2'))
    await act(async () => { resolvers[1](GROUNDED) })
    expect(shownFlags).toEqual([true, false])

    // 3. A -> B -> A, both recalls landed, then the A turn settles: the ids
    // match again, but the exchange it was sent for is gone — not shown.
    rerender({ convId: 'conv-1' })
    await waitFor(() => expect(result.current.exchanges[0]?.prompt).toBe('Question conv-1'))
    act(() => { result.current.send('third') })
    rerender({ convId: 'conv-2' })
    await waitFor(() => expect(result.current.exchanges[0]?.prompt).toBe('Question conv-2'))
    rerender({ convId: 'conv-1' })
    await waitFor(() => expect(result.current.exchanges[0]?.prompt).toBe('Question conv-1'))
    await act(async () => { resolvers[2](GROUNDED) })
    expect(result.current.exchanges.some((e) => e.prompt === 'third')).toBe(false)
    expect(shownFlags).toEqual([true, false, false])
  })

  it('reports shown=false for a settle that lands after A -> B while B is still recalling (applied to A, drawn nowhere)', async () => {
    // The settle still finds its exchange (state has not left A yet — B's
    // recall is in flight), so it is APPLIED; but the hook already returns
    // B's empty, loading view, so it is not DRAWN. Applied alone is not shown.
    const settled: Array<[string | null, boolean]> = []
    const onTurnSettled = (_outcome: 'done' | 'error', conversationId: string | null, shown: boolean) =>
      settled.push([conversationId, shown])
    const { post, resolve: resolvePost } = deferredPost()
    let resolveConv2History!: (r: MessagesResult) => void
    const loadHistory: LoadHistoryFn = (conversationId) =>
      conversationId === 'conv-1'
        ? Promise.resolve({ kind: 'ok', messages: [] })
        : new Promise<MessagesResult>((res) => { resolveConv2History = res })

    const { result, rerender } = renderHook(
      ({ convId }: { convId: string | null }) =>
        useChat({ post, loadHistory, mode: 'sage', conversationId: convId, onTurnSettled }),
      { initialProps: { convId: 'conv-1' as string | null } },
    )
    await waitFor(() => expect(result.current.loadingHistory).toBe(false))
    act(() => { result.current.send('About goblins') })

    rerender({ convId: 'conv-2' })
    expect(result.current.loadingHistory).toBe(true)
    await act(async () => { resolvePost(GROUNDED) })
    expect(result.current.exchanges).toEqual([])
    expect(settled).toEqual([['conv-1', false]])

    // B's recall landing afterwards reports nothing further.
    await act(async () => { resolveConv2History({ kind: 'ok', messages: [] }) })
    expect(result.current.loadingHistory).toBe(false)
    expect(settled).toEqual([['conv-1', false]])
  })

  it('reports the first turn of a NEW conversation as shown once the consumer adopts the minted id', async () => {
    const settled: Array<['done' | 'error', boolean]> = []
    const onTurnSettled = (outcome: 'done' | 'error', _conversationId: string | null, shown: boolean) =>
      settled.push([outcome, shown])
    const post: PostFn = async () => ({
      kind: 'ok',
      response: { ...GROUNDED.kind === 'ok' ? GROUNDED.response : {}, conversation_id: 'srv-new' },
    }) as ChatResult
    const loadHistory: LoadHistoryFn = async () => ({ kind: 'ok', messages: [] })

    const { result } = renderHook(() => {
      const [conversationId, setConversationId] = useState<string | null>(null)
      return {
        conversationId,
        chat: useChat({
          post,
          loadHistory,
          mode: 'sage',
          conversationId,
          onConversationAdopted: setConversationId,
          onTurnSettled,
        }),
      }
    })

    act(() => { result.current.chat.send('What is a Basilisk?') })
    await waitFor(() => expect(result.current.conversationId).toBe('srv-new'))
    await waitFor(() => expect(settled).toEqual([['done', true]]))
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
    await waitFor(() => expect(result.current.exchanges[0]?.status).toBe('done'))
    expect(result.current.exchanges).toHaveLength(1)
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

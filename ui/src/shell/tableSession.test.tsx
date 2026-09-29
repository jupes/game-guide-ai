/**
 * tableSession.test.tsx -- the provider's rules (agent-forge-harness-1kg.2.5.3;
 * brief section 9, T4-1 to T4-7, Critic 23). The stub records every request and
 * the test answers each by hand, so races really interleave. T4-5 is proven at
 * the parse (tableSessionApi.test); its page half is PR-4b's. The last block
 * mounts the real campaign context, which the provider follows by default.
 */

import { StrictMode, useLayoutEffect, type ReactNode } from 'react'
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AppNavProvider } from './AppNav'
import { CampaignProvider, useCampaign, type CampaignContextValue, type CampaignScope } from './campaignContext'
import { CurrentUserContext, STUB } from './currentUser'
import { TableSessionProvider, useTableSession, type TableSessionValue } from './tableSession'
import { EndCourier } from './tableSessionApi'

const SECOND = 1_000
const HOUR = 3_600 * SECOND
const stored = (patch: object = {}) => ({
  schema_version: 1, session_id: 'tss_1', campaign_id: 'cmp_A', state: 'live', gen: 1, audio_epoch: 2, reveal_epoch: 3,
  started_at: '2026-09-29T19:00:00Z', ends_at: '2026-09-29T21:00:00Z', ended_at: null, audio: false, screens: [], ...patch,
})
const LIVE = stored()
const ENDED = stored({ state: 'ended', ended_at: '2026-09-29T20:00:00Z' })
const envelope = (session: unknown) => ({ schema_version: 1, session })
const refusal = (code: string, extra: object = {}) => ({ detail: { code, message: 'x', retryable: true, ...extra } })
const A1: CampaignScope = { campaignId: 'cmp_A', key: 'scope-1' }
const B2: CampaignScope = { campaignId: 'cmp_B', key: 'scope-2' }
const A3: CampaignScope = { campaignId: 'cmp_A', key: 'scope-3' }
/** The one address an End for campaign A's session may go to (SEC-43). */
const TABLE_A = '/campaigns/cmp_A/table-session'

async function flush(): Promise<void> {
  for (let round = 0; round < 4; round += 1) {
    await act(async () => {
      await new Promise<void>((resolve) => setImmediate(resolve))
    })
  }
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms)
  })
  await flush()
}

interface Call {
  readonly url: string
  readonly method: string
  readonly body: Record<string, unknown> | null
}

interface Account {
  readonly user: string
  readonly role: 'dm' | 'player'
  /** `checking` is how CurrentUserProvider starts: as `guest`. */
  readonly status?: 'checking' | 'authenticated'
  readonly children: ReactNode
}

function SignedIn({ user, role, status = 'authenticated', children }: Account): ReactNode {
  const noop = () => undefined
  const id = status === 'checking' ? 'guest' : user
  const value = { user: { ...STUB, id, role }, authStatus: status, retryAuthCheck: noop, signIn: noop, setDisplayName: noop, setAvatarTone: noop }
  return <CurrentUserContext.Provider value={value}>{children}</CurrentUserContext.Provider>
}

/** Mounts the provider over a stub whose every request waits for `answer`
 * (first come first answered, unless `index` picks another), and keeps every
 * value the provider ever rendered. Status 0 is a network failure. */
function mount(options: { scope: CampaignScope | null; strict?: boolean; onStartRefused?: (code: string | null) => void }) {
  const calls: Call[] = []
  const waiting: ((answer: Response | Error) => void)[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => {
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : null
    calls.push({ url: String(input), method: init?.method ?? 'GET', body })
    return new Promise<Response>((resolve, reject) => waiting.push((a) => (a instanceof Error ? reject(a) : resolve(a))))
  }) as typeof fetch
  const courier = new EndCourier()
  const seen: TableSessionValue[] = []
  function Probe(): null {
    seen.push(useTableSession())
    return null
  }
  const tree = (scope: CampaignScope | null, user: string): ReactNode => {
    const provider = (
      <SignedIn user={user} role="dm">
        <TableSessionProvider scope={scope} fetchImpl={fetchImpl} courier={courier} onStartRefused={options.onStartRefused}>
          <Probe />
        </TableSessionProvider>
      </SignedIn>
    )
    return options.strict === true ? <StrictMode>{provider}</StrictMode> : provider
  }
  const view = render(tree(options.scope, 'ada@example.com'))
  return {
    calls,
    seen,
    posts: () => calls.filter((call) => call.method === 'POST'),
    gets: () => calls.filter((call) => call.method === 'GET'),
    now: (): TableSessionValue => seen[seen.length - 1],
    rerender: (scope: CampaignScope | null, user = 'ada@example.com') => view.rerender(tree(scope, user)),
    unmount: view.unmount,
    async answer(status: number, body?: unknown, index = 0): Promise<void> {
      const [settle] = waiting.splice(index, 1)
      settle(status === 0 ? new TypeError('Failed to fetch') : new Response(JSON.stringify(body ?? null), { status }))
      await flush()
    },
  }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
  vi.setSystemTime(Date.parse('2026-09-29T20:00:00Z'))
})
afterEach(() => {
  vi.useRealTimers()
})

describe('TableSessionProvider', () => {
  it('is idle and asks nothing without a scope (positive control: a scope reads once)', async () => {
    const t = mount({ scope: null })
    await flush()
    expect(t.now().state).toBe('idle')
    expect(t.calls).toHaveLength(0)
    t.rerender(A1)
    expect(t.now().state).toBe('loading')
    await flush()
    expect(t.calls).toEqual([{ url: '/campaigns/cmp_A/table-session', method: 'GET', body: null }])
  })

  it('reads once under StrictMode and derives the live session for its consumers', async () => {
    const t = mount({ scope: A1, strict: true })
    await flush()
    expect(t.calls).toHaveLength(1)
    await t.answer(200, envelope(LIVE))
    expect(t.now()).toMatchObject({
      state: 'live',
      session: { sessionId: 'tss_1', campaignId: 'cmp_A', state: 'live', endsAt: '2026-09-29T21:00:00Z' },
      liveSession: { sessionId: 'tss_1', campaignId: 'cmp_A', revealEpoch: 3, audioEpoch: 2 },
    })
  })

  it('T4-1: a Start is never retried by itself, and a manual retry reuses its command id', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(null))
    void t.now().start()
    await flush()
    expect(t.now()).toMatchObject({ state: 'none', pending: 'start' })
    await t.answer(503, refusal('backend_unavailable'))
    expect(t.now().problem).toEqual({ kind: 'start_failed' })
    await advance(HOUR)
    expect(t.posts()).toHaveLength(1)
    void t.now().retry()
    await flush()
    void t.now().start()
    await flush()
    expect(t.posts()).toHaveLength(2)
    await t.answer(200, envelope(LIVE))
    const [first, second] = t.posts()
    expect(first.body).toMatchObject({ action: 'start' })
    expect(second.body?.command_id).toBe(first.body?.command_id)
    expect(t.now().state).toBe('live')
  })

  it('T4-4: live_elsewhere is shown and never answered with an End', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(ENDED))
    expect(t.now()).toMatchObject({ state: 'ended', liveSession: null })
    void t.now().start()
    await flush()
    await t.answer(409, refusal('live_elsewhere'))
    await advance(HOUR)
    expect(t.now().problem).toEqual({ kind: 'live_elsewhere' })
    expect(t.posts().map((call) => call.body?.action)).toEqual(['start'])
  })

  it('a 403 on Start calls onStartRefused with its code and offers no retry', async () => {
    const refused = vi.fn()
    const t = mount({ scope: A1, onStartRefused: refused })
    await t.answer(200, envelope(null))
    void t.now().start()
    await flush()
    await t.answer(403, refusal('plan_required'))
    expect(refused).toHaveBeenCalledWith('plan_required')
    expect(t.now().problem).toEqual({ kind: 'start_refused' })
    expect(await t.now().retry()).toBe('skipped')
    expect(t.posts()).toHaveLength(1)
  })

  it('a failed read is re-read once per press; a throttled Start is retried with its command id', async () => {
    const t = mount({ scope: A1 })
    await t.answer(503)
    expect(t.now()).toMatchObject({ state: 'failed', problem: { kind: 'load_failed' } })
    void t.now().retry()
    void t.now().retry()
    await flush()
    expect(t.gets()).toHaveLength(2)
    await t.answer(200, envelope(null))
    void t.now().start()
    await flush()
    await t.answer(429, refusal('throttled_user', { retry_after_s: 30 }))
    expect(t.now().problem).toEqual({ kind: 'throttled', retryAfterS: 30 })
    void t.now().retry()
    await flush()
    expect(t.posts()[1].body?.command_id).toBe(t.posts()[0].body?.command_id)
  })

  it.each([404, 403])('a %s status read is the one unavailable state, with no Retry (positive control: a 503 is re-read above)', async (status) => {
    const t = mount({ scope: A1 })
    await t.answer(status, refusal(status === 404 ? 'not_found' : 'forbidden'))
    expect(t.now()).toMatchObject({ state: 'failed', problem: { kind: 'unavailable' } })
    await act(async () => {
      await expect(t.now().retry()).resolves.toBe('skipped')
    })
    await flush()
    expect(t.gets()).toHaveLength(1)
  })

  it('T4-2: End is retried on the backoff with one session and command id until a 2xx', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    void t.now().end()
    await flush()
    expect(t.now().pending).toBe('end')
    for (const [delay, failure] of [[1, 503], [2, 0], [4, 403], [8, 503], [16, 503], [30, 503], [30, 0]] as const) {
      await t.answer(failure)
      expect(t.now().endRetrying).toBe(true)
      const sent = t.posts().length
      await advance(delay * SECOND - 1)
      expect(t.posts()).toHaveLength(sent)
      await advance(1)
      expect(t.posts()).toHaveLength(sent + 1)
    }
    await t.answer(200, envelope(ENDED))
    const bodies = t.posts().map((call) => call.body)
    expect(bodies).toHaveLength(8)
    expect(t.posts().map((call) => call.url)).toEqual(Array<string>(8).fill(TABLE_A))
    expect(new Set(bodies.map((body) => body?.command_id)).size).toBe(1)
    expect(bodies.every((body) => body?.action === 'end' && body.session_id === 'tss_1')).toBe(true)
    expect(t.now()).toMatchObject({ state: 'ended', pending: null, endRetrying: false, liveSession: null })
  })

  it('End honours a longer retry_after_s, is sent at once on a second press, and stops on a 404', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    void t.now().end()
    await flush()
    await t.answer(429, refusal('throttled_user', { retry_after_s: 5 }))
    await advance(4 * SECOND)
    expect(t.posts()).toHaveLength(1)
    await advance(SECOND)
    expect(t.posts()).toHaveLength(2)
    await t.answer(503)
    void t.now().end()
    await flush()
    expect(t.posts()).toHaveLength(3)
    await t.answer(404, refusal('not_found'))
    await advance(HOUR)
    expect(t.posts()).toHaveLength(3)
    expect(t.now()).toMatchObject({ state: 'none', pending: null })
  })

  it('Critic 23a: End stops on a 401 and resolves signed_out', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    const ended = t.now().end()
    await flush()
    await t.answer(401, { detail: 'not signed in' })
    await advance(HOUR)
    expect(t.posts()).toHaveLength(1)
    await expect(ended).resolves.toBe('signed_out')
    expect(t.now()).toMatchObject({ pending: null, endRetrying: false })
  })

  it('T4-3: End goes out while a status read is still pending', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    await advance(HOUR)
    expect(t.gets()).toHaveLength(2)
    expect(t.now().state).toBe('live')
    void t.now().end()
    await flush()
    expect(t.posts().map((call) => call.body?.action)).toEqual(['end'])
  })

  it('T4-6: an answer for A is dropped after a switch to B', async () => {
    const t = mount({ scope: A1 })
    t.rerender(B2)
    await flush()
    await t.answer(200, envelope(LIVE))
    expect(t.now().state).toBe('loading')
    await t.answer(200, envelope(stored({ campaign_id: 'cmp_B', session_id: 'tss_B' })))
    expect(t.now().session?.campaignId).toBe('cmp_B')
    expect(t.seen.some((value) => value.session?.campaignId === 'cmp_A')).toBe(false)
  })

  it('T4-6: after A -> B -> A, the first A answer is still dropped', async () => {
    const t = mount({ scope: A1 })
    t.rerender(B2)
    await flush()
    t.rerender(A3)
    await flush()
    await t.answer(200, envelope(null), 2)
    await t.answer(200, envelope(LIVE))
    await t.answer(200, envelope(stored({ campaign_id: 'cmp_B', session_id: 'tss_B' })))
    expect(t.now()).toMatchObject({ state: 'none', session: null })
    expect(t.seen.some((value) => value.session !== null)).toBe(false)
  })

  it("T4-6: A's End answer, arriving after a switch to B, never lands on B's live session", async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    const ended = t.now().end()
    await flush()
    await t.answer(503)
    t.rerender(B2)
    await flush()
    await t.answer(200, envelope(stored({ campaign_id: 'cmp_B', session_id: 'tss_B' })))
    expect(t.now().liveSession).toMatchObject({ sessionId: 'tss_B', campaignId: 'cmp_B' })
    await advance(SECOND)
    expect(t.posts().map((call) => call.url)).toEqual([TABLE_A, TABLE_A])
    await t.answer(200, envelope(ENDED))
    await expect(ended).resolves.toBe('ended')
    expect(t.now()).toMatchObject({ state: 'live', session: { sessionId: 'tss_B' }, liveSession: { sessionId: 'tss_B', campaignId: 'cmp_B' } })
  })

  it('T4-6: an identity change clears what the last account saw', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    expect(t.now().state).toBe('live')
    const before = t.seen.length
    t.rerender(A1, 'bob@example.com')
    expect(t.seen.slice(before).every((value) => value.session === null)).toBe(true)
    await flush()
    expect(t.gets()).toHaveLength(2)
  })

  it('Critic 23a/23d: an End in retry survives a campaign switch and the provider unmounting', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    const ended = t.now().end()
    await flush()
    await t.answer(503)
    t.rerender(B2)
    await flush()
    await t.answer(200, envelope(null))
    t.unmount()
    await advance(SECOND)
    expect(t.posts()).toHaveLength(2)
    expect(t.posts().map((call) => call.url)).toEqual([TABLE_A, TABLE_A])
    await t.answer(200, envelope(ENDED))
    await expect(ended).resolves.toBe('ended')
  })

  it('T4-7: one status re-read when ends_at passes, then ended; never a poll', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    await advance(HOUR - 1)
    expect(t.calls).toHaveLength(1)
    await advance(1)
    expect(t.calls).toHaveLength(2)
    await t.answer(200, envelope(stored({ state: 'ended', ended_at: '2026-09-29T21:00:00Z' })))
    expect(t.now()).toMatchObject({ state: 'ended', liveSession: null })
    await advance(24 * HOUR)
    expect(t.calls).toHaveLength(2)
  })

  it('T4-7: a re-read that still says live is not repeated', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    await advance(HOUR)
    await t.answer(200, envelope(LIVE))
    await advance(24 * HOUR)
    expect(t.calls).toHaveLength(2)
  })

  it('SEC-42: past ends_at on this clock there is no live session, even if the re-read still says live; End stays offered', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    await advance(HOUR - 1)
    expect(t.now().liveSession).toMatchObject({ sessionId: 'tss_1' })
    await advance(1)
    expect(t.now()).toMatchObject({ state: 'live', liveSession: null })
    await t.answer(200, envelope(LIVE))
    await advance(24 * HOUR)
    expect(t.calls).toHaveLength(2)
    expect(t.now()).toMatchObject({ state: 'live', session: { sessionId: 'tss_1' }, liveSession: null })
    void t.now().end()
    await flush()
    expect(t.posts()).toMatchObject([{ url: TABLE_A, body: { action: 'end', session_id: 'tss_1' } }])
  })

  it('T4-7: after an expiry, a later ends_at is live again (one more re-read), and so is the next session', async () => {
    const t = mount({ scope: A1 })
    await t.answer(200, envelope(LIVE))
    await advance(HOUR)
    expect(t.now().liveSession).toBeNull()
    await t.answer(200, envelope(stored({ ends_at: '2026-09-29T23:00:00Z' })))
    expect(t.now().liveSession).toMatchObject({ sessionId: 'tss_1', campaignId: 'cmp_A' })
    await advance(2 * HOUR)
    expect(t.gets()).toHaveLength(3)
    await t.answer(200, envelope(stored({ state: 'ended', ends_at: '2026-09-29T23:00:00Z', ended_at: '2026-09-29T23:00:00Z' })))
    expect(t.now()).toMatchObject({ state: 'ended', liveSession: null })
    void t.now().start()
    await flush()
    await t.answer(200, envelope(stored({ session_id: 'tss_2', started_at: '2026-09-29T23:00:00Z', ends_at: '2026-09-30T01:00:00Z' })))
    expect(t.now()).toMatchObject({ state: 'live', liveSession: { sessionId: 'tss_2', campaignId: 'cmp_A' } })
  })

  it('is inert outside a provider', async () => {
    const values: TableSessionValue[] = []
    function Probe(): null {
      values.push(useTableSession())
      return null
    }
    render(<Probe />)
    expect(values[0]).toMatchObject({ state: 'idle', session: null, liveSession: null })
    await expect(values[0].start()).resolves.toBe('skipped')
  })
})

describe('TableSessionProvider in the app: the campaign context scopes it', () => {
  const CAMPAIGN = {
    schema_version: 1, campaign_id: 'cmp_A', name: 'The Sunken Crown', created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
    avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
  }

  afterEach(() => window.history.replaceState(null, '', '/'))

  /** AppRoot's tower on a cold load of `/workspace#campaign=cmp_A`, with no
   * `scope` prop; the account settles after the first render, as it does there. */
  async function inApp(role: 'dm' | 'player') {
    window.history.replaceState(null, '', '/workspace#campaign=cmp_A')
    const calls: string[] = []
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      calls.push(`${init?.method ?? 'GET'} ${url}`)
      return new Response(JSON.stringify(url === '/campaigns/cmp_A' ? CAMPAIGN : envelope(LIVE)), { status: 200 })
    }) as typeof fetch
    const live = {} as { table: TableSessionValue; campaign: CampaignContextValue }
    function Probe(): null {
      const table = useTableSession()
      const campaign = useCampaign()
      useLayoutEffect(() => {
        live.table = table
        live.campaign = campaign
      })
      return null
    }
    const tree = (status: 'checking' | 'authenticated') => (
      <AppNavProvider initialScreen="workspace" initialMode="gm">
        <SignedIn user="ada@example.com" role={role} status={status}>
          <CampaignProvider fetchImpl={fetchImpl} restore={{ campaignId: 'cmp_A', conversationId: null }}>
            <TableSessionProvider fetchImpl={fetchImpl} courier={new EndCourier()}>
              <Probe />
            </TableSessionProvider>
          </CampaignProvider>
        </SignedIn>
      </AppNavProvider>
    )
    const view = render(tree('checking'))
    await flush()
    view.rerender(tree('authenticated'))
    await flush()
    return { calls, live }
  }

  it('reads the table only after the server confirms the campaign, and goes idle when it is cleared', async () => {
    const { calls, live } = await inApp('dm')
    expect(calls).toEqual(['GET /campaigns/cmp_A', 'GET /campaigns/cmp_A/table-session'])
    expect(live.table.liveSession).toEqual({ sessionId: 'tss_1', campaignId: 'cmp_A', revealEpoch: 3, audioEpoch: 2 })
    await act(async () => {
      await expect(live.campaign.clearCampaign()).resolves.toBe('switched')
    })
    expect(live.table).toMatchObject({ state: 'idle', session: null, liveSession: null })
    expect(calls).toHaveLength(2)
  })

  it('inherits the dm gate: a player makes no request (positive control: a dm, same harness)', async () => {
    const player = await inApp('player')
    expect(player.live.table.state).toBe('idle')
    expect(player.calls).toEqual([])
    const dm = await inApp('dm')
    expect(dm.calls).toContain('GET /campaigns/cmp_A/table-session')
  })
})

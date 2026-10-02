/**
 * campaignContext.test.tsx -- the campaign state layer against the REAL
 * AppNav and CurrentUser providers (agent-forge-harness-1kg.2.5, PR-1b; brief
 * sections 7.4-7.6, 9 and the Critic's items 3, 4, 6, 7, 12, 14, 15, 22).
 *
 * Every request goes through a recording stub that can hold an answer back
 * (`defer`), so each race below really interleaves; a "no request" sits beside
 * a positive control in the same test wherever the mount allows one (the player
 * and abandon cases' controls are the dm mounts of the same calls).
 */

import * as React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import * as api from '../api'
import { CampaignSchema, type Campaign } from '../gm/contracts'
import { AppNavProvider, useAppNav, type AppNavState, type ChatMode } from './AppNav'
import { CurrentUserProvider, useCurrentUser, type CurrentUserContextValue } from './currentUser'
import {
  CampaignProvider, canUseCampaigns, useCampaign, useCampaignDocument, type CampaignContextValue,
  type CampaignDocumentValue, type SwitchGuard,
} from './campaignContext'
import type { IdentityChannelLike } from './identityBroadcast'
import type { CampaignRestore } from './workspaceFragment'

// ── Fixtures and harness ──────────────────────────────────────────────────────

const ADA = 'ada@example.com'
const BOB = 'bob@example.com'

function campaign(id: string): Campaign {
  return CampaignSchema.parse({
    schema_version: 1, campaign_id: id, name: `Name of ${id}`, created_at: '2026-09-16T19:20:11Z',
    updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
    avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0,
    last_activity_at: '2026-09-16T19:31:24Z', last_played_at: null, dormant: false,
  })
}
const A = campaign('cmp_A')
const B = campaign('cmp_B')
const NEW = campaign('cmp_new')
const page = (items: Campaign[], next: string | null = null) => ({ schema_version: 1, items, next_cursor: next })
const thread = (over: Record<string, unknown> = {}) => ({
  schema_version: 1, conversation_id: 'cnv_1', campaign_id: 'cmp_A', title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null, ...over,
})

type Reply = { status: number; body?: unknown } | 'network'
interface Call { url: string; method: string; body: string | null; signal: AbortSignal | null; reply: (r: Reply) => void }
type Route = (call: Call) => Reply | 'defer'

const defaultRoute: Route = ({ url, method }) => {
  if (method === 'POST' && url === '/campaigns') return { status: 201, body: NEW }
  if (url === '/campaigns') return { status: 200, body: page([A, B]) }
  const one = /^\/campaigns\/(cmp_\w+)$/.exec(url)
  if (one !== null) return { status: 200, body: campaign(one[1]) }
  if (url === '/conversations/cnv_1') return { status: 200, body: thread() }
  return { status: 404, body: {} }
}

function stubServer(route: Route = defaultRoute) {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null,
      signal: init?.signal ?? null,
      reply: (r) => (r === 'network'
        ? reject(new TypeError('Failed to fetch'))
        : resolve(new Response(JSON.stringify(r.body ?? {}), { status: r.status }))),
    }
    calls.push(call)
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  const lines = () => calls.map((c) => `${c.method} ${c.url}`)
  return { fetchImpl, calls, lines }
}

interface FakeChannel extends IdentityChannelLike { posts: unknown[]; closed: boolean }
function channels() {
  const opened: FakeChannel[] = []
  const factory = (): FakeChannel => {
    const channel: FakeChannel = {
      posts: [], closed: false, onmessage: null,
      postMessage: (message) => { channel.posts.push(message) },
      close: () => { channel.closed = true },
    }
    opened.push(channel)
    return channel
  }
  const posts = () => opened.flatMap((c) => c.posts)
  const receive = (data: unknown) => act(() => {
    for (const c of opened) c.onmessage?.(new MessageEvent('message', { data }))
  })
  return { factory, opened, posts, receive }
}

const live = {} as { c: CampaignContextValue; d: CampaignDocumentValue; nav: AppNavState; user: CurrentUserContextValue }
/** One entry per committed render: what the Probe's consumers were handed. */
const rendered: Array<{ account: string; mode: ChatMode; id: string | null; selection: string; names: string }> = []
function Probe(): React.JSX.Element {
  const c = useCampaign()
  const d = useCampaignDocument()
  const nav = useAppNav()
  const user = useCurrentUser()
  const names = c.list.kind === 'idle' ? '' : c.list.items.map((i) => i.name).join(',')
  React.useLayoutEffect(() => {
    rendered.push({ account: user.user.id, mode: nav.mode, id: nav.conversationId, selection: c.selection.kind, names })
    live.c = c
    live.d = d
    live.nav = nav
    live.user = user
  })
  return <p data-testid="probe">{`${c.selection.kind}|${names}`}</p>
}

interface MountOptions { role?: 'dm' | 'player'; restore?: CampaignRestore; route?: Route; hash?: string; me?: api.AuthResult }
async function mount(options: MountOptions = {}) {
  const server = stubServer(options.route)
  const signal = channels()
  window.history.replaceState(null, '', `/workspace${options.hash ?? ''}`)
  vi.spyOn(api, 'getMe').mockResolvedValue(options.me ?? { kind: 'ok', user: { email: ADA, role: options.role ?? 'dm' } })
  const view = render(
    <AppNavProvider initialScreen="workspace" initialMode="gm">
      <CurrentUserProvider identityChannelFactory={signal.factory}>
        <CampaignProvider fetchImpl={server.fetchImpl} restore={options.restore ?? null}>
          <Probe />
        </CampaignProvider>
      </CurrentUserProvider>
    </AppNavProvider>,
  )
  await waitFor(() => expect(live.user.authStatus).not.toBe('checking'))
  return { server, signal, view }
}

const flush = () => act(async () => {})
const run = <T,>(fn: () => Promise<T>): Promise<T> => act(fn)
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}
async function loaded(): Promise<void> {
  act(() => live.c.loadCampaigns())
  await waitFor(() => expect(live.c.list.kind).toBe('ready'))
}
async function signOut(ok = true): Promise<boolean> {
  vi.spyOn(api, 'logout').mockResolvedValue(ok)
  return run(() => live.user.user.signOut())
}
/** Another tab changed the session: the signal's background check answers `email`. */
async function switchAccount(signal: ReturnType<typeof channels>, email: string, role: 'dm' | 'player' = 'dm'): Promise<void> {
  vi.mocked(api.getMe).mockResolvedValue({ kind: 'ok', user: { email, role } })
  await signal.receive({ v: 1, kind: 'identity-changed' })
  await waitFor(() => expect([live.user.user.id, live.user.user.role]).toEqual([email, role]))
}

beforeEach(() => {
  rendered.length = 0
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

// ── The gate and the inert default ────────────────────────────────────────────

describe('the dm gate and the inert default', () => {
  it('canUseCampaigns is the dm role and nothing else (I-10)', () => {
    expect(canUseCampaigns('dm')).toBe(true)
    expect(canUseCampaigns('player')).toBe(false)
  })

  it('outside a provider useCampaign is inert and never fetches; inside one the same call does (T1-21)', async () => {
    const outside = stubServer()
    vi.stubGlobal('fetch', outside.fetchImpl)
    const held: { value?: CampaignContextValue } = {}
    function Bare(): null {
      const value = useCampaign()
      React.useLayoutEffect(() => {
        held.value = value
      })
      return null
    }
    render(<Bare />)
    const bare = held.value as CampaignContextValue
    bare.loadCampaigns()
    bare.retrySelection()
    expect(await bare.selectCampaign(A)).toBe('unchanged')
    expect(await bare.createCampaign('Name')).toEqual({ kind: 'failed' })
    expect(bare.enabled).toBe(false)
    expect(outside.calls).toHaveLength(0)
    const { server } = await mount()
    act(() => live.c.loadCampaigns())
    expect(server.lines()).toEqual(['GET /campaigns'])
  })

  it('a player gets a disabled provider that makes zero requests (M1-5)', async () => {
    const { server } = await mount({ role: 'player' })
    live.c.loadCampaigns()
    expect(await run(() => live.c.createCampaign('Name'))).toEqual({ kind: 'failed' })
    expect(live.c.enabled).toBe(false)
    expect(server.calls).toHaveLength(0)
  })
})

// ── The list ──────────────────────────────────────────────────────────────────

describe('the campaign list (critic 14e)', () => {
  it('loads once while loading, keeps items on a re-read and a failure, and pages on demand', async () => {
    let status = 200
    const { server } = await mount({
      route: (call) => {
        if (call.url === '/campaigns?cursor=c2') return status === 200 ? { status: 200, body: page([B]) } : 'network'
        if (call.url === '/campaigns') return status === 200 ? { status: 200, body: page([A], 'c2') } : { status: 503 }
        return defaultRoute(call)
      },
    })
    act(() => {
      live.c.loadCampaigns()
      live.c.loadCampaigns()
    })
    await waitFor(() => expect(live.c.list).toMatchObject({ kind: 'ready', nextCursor: 'c2' }))
    expect(server.lines()).toEqual(['GET /campaigns'])
    status = 503
    act(() => live.c.loadMoreCampaigns())
    await waitFor(() => expect(live.c.list).toMatchObject({ items: [A], loadingMore: false, moreFailed: true }))
    status = 200
    act(() => {
      live.c.loadMoreCampaigns()
      live.c.loadMoreCampaigns()
    })
    await waitFor(() => expect(live.c.list).toMatchObject({ nextCursor: null, loadingMore: false }))
    expect(server.lines()).toEqual(['GET /campaigns', 'GET /campaigns?cursor=c2', 'GET /campaigns?cursor=c2'])
    expect(screen.getByTestId('probe')).toHaveTextContent('Name of cmp_A,Name of cmp_B')
    act(() => live.c.loadMoreCampaigns())
    expect(server.calls).toHaveLength(3)
    status = 503
    act(() => live.c.loadCampaigns())
    expect(live.c.list).toEqual({ kind: 'loading', items: [A, B] })
    await waitFor(() => expect(live.c.list).toEqual({ kind: 'failed', items: [A, B] }))
  })
})

// ── Switching (section 7.6) ───────────────────────────────────────────────────

describe('switching and the guard (LIB-25, critic 14)', () => {
  it('A -> B -> A mints a new scope key every time, so an answer keyed to the first A is dropped (T1-9)', async () => {
    await mount()
    await loaded()
    await run(() => live.c.selectCampaign(A))
    const first = live.c.scope?.key ?? ''
    await run(() => live.c.selectCampaign(B))
    await run(() => live.c.selectCampaign(A))
    expect(live.c.scope?.campaignId).toBe('cmp_A')
    expect(live.c.scope?.key).not.toBe(first)
    expect(live.c.isCurrentScope(first)).toBe(false)
    expect(live.c.isCurrentScope(live.c.scope?.key ?? '')).toBe(true)
  })

  it.each([
    ['returns false', () => false],
    ['rejects', () => Promise.reject(new Error('no'))],
    ['throws', () => { throw new Error('no') }],
  ] as Array<[string, SwitchGuard]>)('a guard that %s vetoes and changes nothing (T1-10)', async (_label, guard) => {
    await mount({ hash: '#campaign=cmp_A' })
    await loaded()
    await run(() => live.c.selectCampaign(A))
    const before = { selection: live.c.selection, key: live.c.scope?.key, hash: window.location.hash, id: live.nav.conversationId }
    const spy = vi.fn(guard)
    let unregister = () => {}
    act(() => { unregister = live.c.registerSwitchGuard(spy) })
    expect(await run(() => live.c.selectCampaign(A))).toBe('unchanged')
    expect(spy).not.toHaveBeenCalled()
    expect(await run(() => live.c.selectCampaign(B))).toBe('vetoed')
    expect(await run(() => live.c.clearCampaign())).toBe('vetoed')
    expect(spy.mock.calls).toEqual([[{ campaignId: 'cmp_B' }], [{ campaignId: null }]])
    expect({ selection: live.c.selection, key: live.c.scope?.key, hash: window.location.hash, id: live.nav.conversationId })
      .toEqual(before)
    unregister()
    expect(await run(() => live.c.selectCampaign(B))).toBe('switched')
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('while one switch awaits its guards every other switch is vetoed; a late guard after sign-out applies nothing', async () => {
    await mount()
    await loaded()
    const gate = deferred<boolean>()
    const guard = vi.fn(() => gate.promise)
    act(() => { live.c.registerSwitchGuard(guard) })
    let pending: Promise<string> = Promise.resolve('')
    act(() => { pending = live.c.selectCampaign(A) })
    expect(await run(() => live.c.selectCampaign(B))).toBe('vetoed')
    expect(await run(() => live.c.createCampaign('Other'))).toEqual({ kind: 'vetoed' })
    expect(guard).toHaveBeenCalledTimes(1)
    await signOut()
    await act(async () => { gate.resolve(true) })
    expect(await pending).toBe('vetoed')
    expect(live.c.selection).toEqual({ kind: 'none' })
  })

  it('an object this provider did not list is re-read by id; a switch during that read drops its answer (14c, 14f)', async () => {
    const { server } = await mount({ route: (call) => (call.url === '/campaigns/cmp_X' ? 'defer' : defaultRoute(call)) })
    let pending: Promise<string> = Promise.resolve('')
    act(() => { pending = live.c.selectCampaign(campaign('cmp_X')) })
    expect(live.c.selection).toEqual({ kind: 'restoring', campaignId: 'cmp_X' })
    await loaded()
    expect(await run(() => live.c.selectCampaign(A))).toBe('switched')
    act(() => server.calls.find((c) => c.url === '/campaigns/cmp_X')?.reply({ status: 200, body: campaign('cmp_X') }))
    expect(await pending).toBe('vetoed')
    expect(live.c.selection).toEqual({ kind: 'selected', campaign: A })
    expect(await run(() => live.c.clearCampaign())).toBe('switched')
    expect(live.c.selection).toEqual({ kind: 'none' })
    expect(live.c.scope).toBeNull()
  })
})

// ── Create (critic 15) ────────────────────────────────────────────────────────

describe('create', () => {
  it('a bad name makes no request; 422 is invalid; 503 is one POST, then a GET re-read (T1-14)', async () => {
    let status = 422
    const { server } = await mount({
      route: (call) => (call.method === 'POST' ? { status, body: {} } : defaultRoute(call)),
    })
    const guard = vi.fn(() => true)
    act(() => { live.c.registerSwitchGuard(guard) })
    expect(await run(() => live.c.createCampaign('   '))).toEqual({ kind: 'invalid' })
    expect(server.calls).toHaveLength(0)
    expect(guard).not.toHaveBeenCalled()
    expect(await run(() => live.c.createCampaign('Named'))).toEqual({ kind: 'invalid' })
    status = 503
    expect(await run(() => live.c.createCampaign('Named'))).toEqual({ kind: 'failed' })
    await waitFor(() => expect(live.c.list.kind).toBe('ready'))
    expect(server.lines()).toEqual(['POST /campaigns', 'POST /campaigns', 'GET /campaigns'])
    expect(guard.mock.calls).toEqual([[{ campaignId: null }], [{ campaignId: null }]])
  })

  it('is single-flight, heads the list and selects the new campaign; a veto makes no request', async () => {
    const { server } = await mount({ route: (call) => (call.method === 'POST' ? 'defer' : defaultRoute(call)) })
    await loaded()
    let first: Promise<unknown> = Promise.resolve()
    let second: Promise<unknown> = Promise.resolve()
    act(() => {
      first = live.c.createCampaign('Named')
      second = live.c.createCampaign('Named')
    })
    await flush()
    expect(server.lines().filter((l) => l.startsWith('POST'))).toHaveLength(1)
    act(() => server.calls.find((c) => c.method === 'POST')?.reply({ status: 201, body: NEW }))
    expect(await first).toEqual({ kind: 'created', campaign: NEW })
    expect(await second).toEqual({ kind: 'created', campaign: NEW })
    expect(live.c.selection).toEqual({ kind: 'selected', campaign: NEW })
    expect(live.c.list.kind === 'ready' && live.c.list.items.map((c) => c.campaign_id)).toEqual(['cmp_new', 'cmp_A', 'cmp_B'])
    act(() => { live.c.registerSwitchGuard(() => false) })
    expect(await run(() => live.c.createCampaign('Other'))).toEqual({ kind: 'vetoed' })
    expect(server.lines().filter((l) => l.startsWith('POST'))).toHaveLength(1)
  })

  it('while a create is in flight a select, a clear and a campaign link are refused before any guard or request (P-F, decision 4)', async () => {
    const { server } = await mount({
      hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null },
      route: (call) => (call.method === 'POST' ? 'defer' : defaultRoute(call)),
    })
    await waitFor(() => expect(live.c.selection).toEqual({ kind: 'selected', campaign: A }))
    await loaded()
    const guard = vi.fn(() => true)
    act(() => { live.c.registerSwitchGuard(guard) })
    const link = (hash: string) => {
      window.history.replaceState(null, '', `/workspace${hash}`)
      act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')) })
    }
    let created: Promise<unknown> = Promise.resolve()
    act(() => { created = live.c.createCampaign('Named') })
    await waitFor(() => expect(server.lines()).toContain('POST /campaigns'))
    const key = live.c.scope?.key
    expect(await run(() => live.c.selectCampaign(B))).toBe('vetoed')
    expect(await run(() => live.c.clearCampaign())).toBe('vetoed')
    link('#campaign=cmp_B')
    await flush()
    expect(window.location.hash).toBe('#campaign=cmp_A')
    expect([live.c.selection, live.c.scope?.key]).toEqual([{ kind: 'selected', campaign: A }, key])
    expect(guard.mock.calls).toEqual([[{ campaignId: null }]])
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A', 'GET /campaigns', 'POST /campaigns'])
    act(() => server.calls.find((c) => c.method === 'POST')?.reply({ status: 201, body: NEW }))
    expect(await created).toEqual({ kind: 'created', campaign: NEW })
    expect(live.c.selection).toEqual({ kind: 'selected', campaign: NEW })
    expect(await run(() => live.c.selectCampaign(B))).toBe('switched')
    link('#campaign=cmp_A')
    await waitFor(() => expect(live.c.selection).toEqual({ kind: 'selected', campaign: A }))
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A', 'GET /campaigns', 'POST /campaigns', 'GET /campaigns/cmp_A'])
    expect(await run(() => live.c.clearCampaign())).toBe('switched')
  })
})

// ── Restore at the provider (section 7.4, critic 3, 6, 7) ─────────────────────

describe('restore', () => {
  it.each([
    ['404', { status: 404 }],
    ['403', { status: 403 }],
    ['an archived 200', { status: 200, body: { ...A, archived_at: '2026-09-17T00:00:00Z' } }],
  ] as Array<[string, Reply]>)('%s is the one generic unavailable state, keys stripped (T1-6)', async (_label, reply) => {
    await mount({
      hash: '#x=1&campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null },
      route: (call) => (call.url === '/campaigns/cmp_A' ? reply : defaultRoute(call)),
    })
    await waitFor(() => expect(live.c.selection).toStrictEqual({ kind: 'unavailable' }))
    await waitFor(() => expect(window.location.hash).toBe('#x=1'))
  })

  it('a 503 or a network failure is failed with the keys kept; one Retry press is one GET (T1-7, 14d)', async () => {
    let reply: Reply = { status: 503 }
    const { server } = await mount({
      hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
      route: (call) => (call.url === '/campaigns/cmp_A' ? reply : defaultRoute(call)),
    })
    await waitFor(() => expect(live.c.selection).toEqual({ kind: 'failed', campaignId: 'cmp_A' }))
    await flush()
    expect(window.location.hash).toBe('#campaign=cmp_A&conversation=cnv_1')
    reply = 'network'
    act(() => live.c.retrySelection())
    await waitFor(() => expect(live.c.selection.kind).toBe('failed'))
    reply = { status: 200, body: A }
    act(() => {
      live.c.retrySelection()
      live.c.retrySelection()
    })
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_1'))
    expect(server.lines()).toEqual([
      'GET /campaigns/cmp_A', 'GET /campaigns/cmp_A', 'GET /campaigns/cmp_A', 'GET /conversations/cnv_1',
    ])
  })

  it.each([
    ['another campaign', { status: 200, body: thread({ campaign_id: 'cmp_B' }) }],
    ['an archived thread', { status: 200, body: thread({ archived_at: '2026-09-17T00:00:00Z' }) }],
    ['a Sage conversation', { status: 200, body: thread({ started_mode: 'sage' }) }],
    ['a 404', { status: 404 }],
  ] as Array<[string, Reply]>)('a conversation key naming %s is dropped silently (T1-2)', async (_label, reply) => {
    await mount({
      hash: '#campaign=cmp_A&conversation=cnv_1', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
      route: (call) => (call.url === '/conversations/cnv_1' ? reply : defaultRoute(call)),
    })
    await waitFor(() => expect(window.location.hash).toBe('#campaign=cmp_A'))
    expect(live.c.selection).toEqual({ kind: 'selected', campaign: A })
    expect(live.nav.conversationId).toBeNull()
  })

  it('an unavailable first status and its checking retry write nothing: the deep link survives, then restores (critic 3, 7.4)', async () => {
    const DEEP = '#campaign=cmp_A&conversation=cnv_1'
    const { server } = await mount({
      hash: DEEP, restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' }, me: { kind: 'error', status: 503, message: 'down' },
    })
    await flush()
    expect([live.user.authStatus, window.location.hash, server.calls.length]).toEqual(['unavailable', DEEP, 0])
    const answer = deferred<api.AuthResult>()
    vi.mocked(api.getMe).mockReturnValue(answer.promise)
    act(() => live.user.retryAuthCheck())
    await flush()
    expect([live.user.authStatus, window.location.hash]).toEqual(['checking', DEEP])
    await act(async () => { answer.resolve({ kind: 'ok', user: { email: ADA, role: 'dm' } }) })
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_1'))
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A', 'GET /conversations/cnv_1'])
    expect(window.location.hash).toBe(DEEP)
  })

  it('the thread is read only after the campaign answers, and is set only when it is one of its GM threads (T1-2)', async () => {
    const { server } = await mount({
      hash: '#campaign=cmp_A&conversation=cnv_1', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
      route: (call) => (call.url === '/campaigns/cmp_A' ? 'defer' : defaultRoute(call)),
    })
    await flush()
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A'])
    expect(live.c.selection).toEqual({ kind: 'restoring', campaignId: 'cmp_A' })
    expect(window.location.hash).toBe('#campaign=cmp_A&conversation=cnv_1')
    act(() => server.calls[0].reply({ status: 200, body: A }))
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_1'))
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A', 'GET /conversations/cnv_1'])
    expect(window.location.hash).toBe('#campaign=cmp_A&conversation=cnv_1')
  })

  it.each([
    ['a player', { kind: 'ok', user: { email: ADA, role: 'player' } }],
    ['a signed-out page', { kind: 'error', status: 401, message: 'not signed in' }],
  ] as Array<[string, api.AuthResult]>)('for %s the restore is abandoned: keys stripped, Sage, Landing, no request (critic 6)', async (_label, me) => {
    const { server } = await mount({ hash: '#campaign=cmp_A&keep=1', restore: { campaignId: 'cmp_A', conversationId: null }, me })
    expect(window.location.hash).toBe('#keep=1')
    expect(live.nav.mode).toBe('sage')
    expect(live.nav.screen).toBe('landing')
    expect(live.c.selection).toEqual({ kind: 'none' })
    expect(server.calls).toHaveLength(0)
  })

  it('a campaign thread id never renders outside its GM channel, and leaving GM clears it (critic 7, I-13)', async () => {
    await mount({ hash: '#campaign=cmp_A&conversation=cnv_1', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' } })
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_1'))
    act(() => live.nav.setMode('sage'))
    await flush()
    expect(rendered.filter((r) => r.mode !== 'gm' && r.id === 'cnv_1')).toEqual([])
    act(() => live.nav.setMode('gm'))
    expect(live.nav.conversationId).toBeNull()
    expect(rendered.some((r) => r.mode === 'gm' && r.id === 'cnv_1')).toBe(true)
  })
})

// ── Identity (section 7.5) ────────────────────────────────────────────────────

describe('identity transitions', () => {
  it("ada's late list never reaches bob, on screen or in the context (T1-8)", async () => {
    const { server } = await mount({ route: (call) => (call.url === '/campaigns' ? 'defer' : defaultRoute(call)) })
    act(() => live.c.loadCampaigns())
    const seen: string[] = []
    const observer = new MutationObserver(() => seen.push(screen.queryByTestId('probe')?.textContent ?? ''))
    observer.observe(document.body, { subtree: true, childList: true, characterData: true })
    await signOut()
    act(() => live.user.signIn({ email: BOB, role: 'dm' }))
    act(() => server.calls[0].reply({ status: 200, body: page([A, B]) }))
    await flush()
    observer.disconnect()
    expect(live.user.user.id).toBe(BOB)
    expect(live.c.list).toEqual({ kind: 'idle' })
    expect([...seen, screen.getByTestId('probe').textContent].join()).not.toContain('Name of')
  })

  it('a successful sign-out clears everything and ignores guards; a refused one changes nothing (T1-11, T1-12)', async () => {
    await mount({ hash: '#campaign=cmp_A&conversation=cnv_1', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' } })
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_1'))
    act(() => { live.c.registerSwitchGuard(() => false) })
    act(() => live.c.loadCampaigns())
    await waitFor(() => expect(live.c.list.kind).toBe('ready'))
    expect(await signOut(false)).toBe(false)
    expect(live.c.selection.kind).toBe('selected')
    expect(window.location.hash).toBe('#campaign=cmp_A&conversation=cnv_1')
    expect(await signOut(true)).toBe(true)
    await flush()
    expect(live.c.selection).toEqual({ kind: 'none' })
    expect(live.c.list).toEqual({ kind: 'idle' })
    expect(live.c.scope).toBeNull()
    expect(live.nav.conversationId).toBeNull()
    expect(window.location.hash).toBe('')
  })

  it("a restore in flight when ada signs out never reaches bob, and bob's requests never name it (T1-23)", async () => {
    const { server } = await mount({
      hash: '#campaign=cmp_A&conversation=cnv_1', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
      route: (call) => (call.url.startsWith('/campaigns/') || call.url.startsWith('/conversations/') ? 'defer' : defaultRoute(call)),
    })
    act(() => server.calls[0].reply({ status: 200, body: A }))
    await waitFor(() => expect(server.calls).toHaveLength(2))
    await signOut()
    act(() => live.user.signIn({ email: BOB, role: 'dm' }))
    const before = server.calls.length
    act(() => server.calls[1].reply({ status: 200, body: thread() }))
    act(() => live.c.loadCampaigns())
    await flush()
    expect(server.calls.slice(before).map((c) => c.url)).toEqual(['/campaigns'])
    expect(live.c.selection).toEqual({ kind: 'none' })
    expect(live.nav.conversationId).toBeNull()
    expect(window.location.hash).toBe('')
  })

  it("ada's create answered after a direct switch to bob is aborted, then neither listed nor selected for bob (M-N2)", async () => {
    const { server, signal } = await mount({ route: (call) => (call.method === 'POST' ? 'defer' : defaultRoute(call)) })
    let outcome: Promise<unknown> = Promise.resolve()
    act(() => { outcome = live.c.createCampaign('Ada typed name') })
    await waitFor(() => expect(server.lines()).toEqual(['POST /campaigns']))
    await switchAccount(signal, BOB)
    const post = server.calls[0]
    expect(post.signal?.aborted).toBe(true)
    await loaded()
    act(() => post.reply({ status: 201, body: campaign('cmp_ada') }))
    expect(await outcome).toEqual({ kind: 'failed' })
    await flush()
    expect(live.c.selection).toEqual({ kind: 'none' })
    expect(live.c.scope).toBeNull()
    expect(live.c.list.kind === 'ready' && live.c.list.items.map((c) => c.campaign_id)).toEqual(['cmp_A', 'cmp_B'])
  })

  it("ada's Load more answered after a direct switch to bob never reaches bob's list (M-N4)", async () => {
    const { server, signal } = await mount({
      route: (call) => {
        if (call.url === '/campaigns?cursor=c2') return 'defer'
        return call.url === '/campaigns' ? { status: 200, body: page([A], 'c2') } : defaultRoute(call)
      },
    })
    await loaded()
    act(() => live.c.loadMoreCampaigns())
    await switchAccount(signal, BOB)
    const more = server.calls[1]
    expect([more.url, more.signal?.aborted]).toEqual(['/campaigns?cursor=c2', true])
    await loaded()
    act(() => more.reply({ status: 200, body: page([campaign('cmp_adaonly')]) }))
    await flush()
    expect(live.c.list).toEqual({ kind: 'ready', items: [A], nextCursor: 'c2', loadingMore: false, moreFailed: false })
  })

  it.each([
    ['a direct switch through the identity signal', (signal: ReturnType<typeof channels>) => switchAccount(signal, BOB)],
    ['a sign-out and then a sign-in as bob', async () => {
      await signOut()
      act(() => live.user.signIn({ email: BOB, role: 'dm' }))
    }],
  ] as Array<[string, (signal: ReturnType<typeof channels>) => Promise<void>]>)(
    "ada's restore read answered after %s is aborted and never reaches bob's state, renders, URL or requests (P-A1, P-A2)",
    async (_label, toBob) => {
      const { server, signal } = await mount({
        hash: '#campaign=cmp_A&conversation=cnv_1', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' },
        route: (call) => (call.url === '/campaigns/cmp_A' ? 'defer' : defaultRoute(call)),
      })
      await waitFor(() => expect(server.lines()).toEqual(['GET /campaigns/cmp_A']))
      expect(live.c.selection).toEqual({ kind: 'restoring', campaignId: 'cmp_A' })
      await toBob(signal)
      const held = server.calls[0]
      expect(held.signal?.aborted).toBe(true)
      act(() => held.reply({ status: 200, body: A }))
      await flush()
      expect(live.user.user.id).toBe(BOB)
      expect([live.c.selection, live.c.scope, live.nav.conversationId, window.location.hash]).toEqual([{ kind: 'none' }, null, null, ''])
      const bob = rendered.filter((r) => r.account === BOB)
      expect(bob.length).toBeGreaterThan(0)
      expect(bob.filter((r) => r.selection !== 'none' || r.id !== null)).toEqual([])
      await loaded()
      expect(server.lines()).toEqual(['GET /campaigns/cmp_A', 'GET /campaigns'])
    },
  )

  it("a direct switch from ada to bob never commits a render that carries ada's state (7.5, snapshotFor)", async () => {
    const { signal } = await mount({ hash: '#campaign=cmp_A&conversation=cnv_1', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' } })
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_1'))
    await loaded()
    expect(rendered.at(-1)).toEqual({ account: ADA, mode: 'gm', id: 'cnv_1', selection: 'selected', names: 'Name of cmp_A,Name of cmp_B' })
    await switchAccount(signal, BOB)
    const bob = rendered.filter((r) => r.account === BOB)
    expect(bob.length).toBeGreaterThan(0)
    expect(bob.filter((r) => r.selection !== 'none' || r.names !== '' || r.id !== null)).toEqual([])
  })

  it('nothing campaign-shaped is ever written to web storage (T1-13)', async () => {
    await mount({ hash: '#campaign=cmp_A&conversation=cnv_1', restore: { campaignId: 'cmp_A', conversationId: 'cnv_1' } })
    await waitFor(() => expect(live.nav.conversationId).toBe('cnv_1'))
    await loaded()
    await run(() => live.c.selectCampaign(B))
    await run(() => live.c.createCampaign('A typed secret name'))
    await signOut()
    const everything = [localStorage, sessionStorage].flatMap((s) =>
      Array.from({ length: s.length }, (_, i) => `${s.key(i)}=${s.getItem(s.key(i) ?? '')}`)).join('\n')
    for (const needle of ['cmp_A', 'cmp_B', 'cmp_new', 'cnv_1', 'Name of', 'typed secret']) {
      expect(everything).not.toContain(needle)
    }
  })
})

// ── The identity signal (I-9, critic 12) ──────────────────────────────────────

describe('the cross-tab identity signal', () => {
  const MESSAGE = { v: 1, kind: 'identity-changed' }

  it('posts exactly the bare message after sign-in, a sign-out the server accepted and the first 401 (T1-17)', async () => {
    const { signal } = await mount()
    expect(signal.posts()).toEqual([])
    await signOut(false)
    expect(signal.posts()).toEqual([])
    act(() => {
      api.notifyUnauthorized()
      api.notifyUnauthorized()
      api.notifyUnauthorized()
    })
    act(() => live.user.signIn({ email: BOB, role: 'dm' }))
    await signOut(true)
    expect(signal.posts()).toHaveLength(3)
    for (const post of signal.posts()) expect(post).toStrictEqual(MESSAGE)
  })

  it('a signal re-checks in the background: same account changes nothing, another account or a 401 replaces it (T1-18)', async () => {
    const { signal } = await mount()
    await loaded()
    const me = vi.mocked(api.getMe)
    me.mockClear()
    await signal.receive(MESSAGE)
    await flush()
    expect(me).toHaveBeenCalledTimes(1)
    expect(live.user.authStatus).toBe('authenticated')
    expect(live.c.list.kind).toBe('ready')
    await signal.receive({ ...MESSAGE, extra: 1 })
    await signal.receive('identity-changed')
    me.mockResolvedValue({ kind: 'error', status: 503, message: 'down' })
    await signal.receive(MESSAGE)
    await flush()
    expect(me).toHaveBeenCalledTimes(2)
    expect(live.user.authStatus).toBe('authenticated')
    me.mockResolvedValue({ kind: 'ok', user: { email: BOB, role: 'dm' } })
    await signal.receive(MESSAGE)
    await waitFor(() => expect(live.user.user.id).toBe(BOB))
    expect(live.c.list).toEqual({ kind: 'idle' })
    me.mockResolvedValue({ kind: 'error', status: 401, message: 'not signed in' })
    await signal.receive(MESSAGE)
    await waitFor(() => expect(live.user.authStatus).toBe('unauthenticated'))
    expect(signal.posts()).toEqual([])
  })

  it('a signal answering the same account with another role takes that role and clears the campaign state (T1-18, critic 12d)', async () => {
    const { signal } = await mount({ hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null } })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await loaded()
    await switchAccount(signal, ADA, 'player')
    expect([live.c.enabled, live.c.selection, live.c.list, live.c.scope]).toEqual([false, { kind: 'none' }, { kind: 'idle' }, null])
    await waitFor(() => expect(window.location.hash).toBe(''))
  })

  it('coalesces a burst into one check, and drops an answer that lands after a local sign-out (critic 12b, 12c)', async () => {
    const { signal } = await mount()
    const me = vi.mocked(api.getMe)
    const answer = deferred<api.AuthResult>()
    me.mockClear()
    me.mockReturnValue(answer.promise)
    await signal.receive({ v: 1, kind: 'identity-changed' })
    await signal.receive({ v: 1, kind: 'identity-changed' })
    await signal.receive({ v: 1, kind: 'identity-changed' })
    expect(me).toHaveBeenCalledTimes(1)
    await signOut()
    await act(async () => { answer.resolve({ kind: 'ok', user: { email: ADA, role: 'dm' } }) })
    expect(live.user.authStatus).toBe('unauthenticated')
  })

  it('closes its channel on unmount (T1-19)', async () => {
    const { signal, view } = await mount()
    expect(signal.opened.map((c) => c.closed)).toEqual([false])
    view.unmount()
    expect(signal.opened.map((c) => c.closed)).toEqual([true])
  })
})

// ── hashchange (critic 4, T1-25) ──────────────────────────────────────────────

describe('a fragment changed outside the app', () => {
  function edit(hash: string): void {
    window.history.replaceState(null, '', `/workspace${hash}`)
    act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')) })
  }

  it('switches by GET only, restores the fragment on a veto, and puts back a malformed one', async () => {
    const { server } = await mount({ hash: '#a=1&campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null } })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    edit('#a=1&campaign=cmp_B')
    await waitFor(() => expect(live.c.selection).toEqual({ kind: 'selected', campaign: B }))
    edit('#a=1&campaign=cmp_B&conversation=cnv_1')
    await waitFor(() => expect(server.lines()).toContain('GET /conversations/cnv_1'))
    await waitFor(() => expect(window.location.hash).toBe('#a=1&campaign=cmp_B'))
    let veto = true
    act(() => { live.c.registerSwitchGuard(() => !veto) })
    edit('#a=1&campaign=cmp_A')
    await waitFor(() => expect(window.location.hash).toBe('#a=1&campaign=cmp_B'))
    expect(live.c.selection).toEqual({ kind: 'selected', campaign: B })
    edit('#a=1&campaign=%41')
    expect(window.location.hash).toBe('#a=1&campaign=cmp_B')
    edit('#a=1')
    expect(window.location.hash).toBe('#a=1&campaign=cmp_B')
    veto = false
    edit('#campaign=cmp_A')
    await waitFor(() => expect(live.c.selection).toEqual({ kind: 'selected', campaign: A }))
    expect(server.calls.filter((c) => c.method !== 'GET')).toEqual([])
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A', 'GET /campaigns/cmp_B', 'GET /conversations/cnv_1', 'GET /campaigns/cmp_A'])
  })

  it.each(['/', '/profile'])('on %s it neither requests nor switches; the same edit on /workspace does (critic 4)', async (path) => {
    const { server } = await mount({ hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null } })
    await waitFor(() => expect(live.c.selection).toEqual({ kind: 'selected', campaign: A }))
    window.history.replaceState(null, '', `${path}#campaign=cmp_B`)
    act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')) })
    await flush()
    expect(live.c.selection).toEqual({ kind: 'selected', campaign: A })
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A'])
    edit('#campaign=cmp_B')
    await waitFor(() => expect(live.c.selection).toEqual({ kind: 'selected', campaign: B }))
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A', 'GET /campaigns/cmp_B'])
  })

  it('is ignored for a player, whose keys are stripped', async () => {
    const { server } = await mount({ role: 'player' })
    edit('#campaign=cmp_A')
    expect(window.location.hash).toBe('')
    expect(server.calls).toHaveLength(0)
  })
})

// ── The document key (agent-forge-harness-1kg.6.3, CANVAS-30) ─────────────────

describe('the document key (T-3)', () => {
  const RESTORE = { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_1' } as const
  const HASH = '#campaign=cmp_A&document=doc_1'
  function edit(hash: string): void {
    window.history.replaceState(null, '', `/workspace${hash}`)
    act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')) })
  }

  it('a cold restore keeps the document key through restoring -> selected, and the deep link stays in the URL', async () => {
    const { server } = await mount({
      hash: HASH, restore: RESTORE,
      route: (call) => (call.url === '/campaigns/cmp_A' ? 'defer' : defaultRoute(call)),
    })
    await flush()
    expect(live.c.selection).toEqual({ kind: 'restoring', campaignId: 'cmp_A' })
    expect(live.d.documentKey).toBe('doc_1')
    expect(window.location.hash).toBe(HASH)
    act(() => server.calls[0].reply({ status: 200, body: A }))
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    expect(live.d.documentKey).toBe('doc_1')
    expect(window.location.hash).toBe(HASH)
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A'])
  })

  it('a switch or a clear drops the document key from the state and the URL', async () => {
    await mount({ hash: HASH, restore: RESTORE })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await run(() => live.c.selectCampaign(B))
    expect(live.d.documentKey).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_B')
    act(() => live.d.setDocumentKey('doc_2'))
    expect(window.location.hash).toBe('#campaign=cmp_B&document=doc_2')
    await run(() => live.c.clearCampaign())
    expect(live.d.documentKey).toBeNull()
    expect(window.location.hash).toBe('')
  })

  it('an unavailable campaign drops the document key with it', async () => {
    await mount({
      hash: HASH, restore: RESTORE,
      route: (call) => (call.url === '/campaigns/cmp_A' ? { status: 404 } : defaultRoute(call)),
    })
    await waitFor(() => expect(live.c.selection).toStrictEqual({ kind: 'unavailable' }))
    expect(live.d.documentKey).toBeNull()
    await waitFor(() => expect(window.location.hash).toBe(''))
  })

  it('leaving GM removes the key from the URL but not from the state; returning writes it back (CANVAS-9)', async () => {
    await mount({ hash: HASH, restore: RESTORE })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    act(() => live.nav.setMode('sage'))
    await waitFor(() => expect(window.location.hash).toBe(''))
    expect(live.d.documentKey).toBe('doc_1')
    act(() => live.nav.setMode('gm'))
    await waitFor(() => expect(window.location.hash).toBe(HASH))
    expect(live.d.documentKey).toBe('doc_1')
  })

  it('a same-campaign hash naming another document notifies the listener first; the URL keeps the current id until setDocumentKey', async () => {
    const { server } = await mount({ hash: HASH, restore: RESTORE })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    const heard: Array<{ id: string; hash: string }> = []
    let off = (): void => {}
    act(() => { off = live.d.onDocumentLink((id) => heard.push({ id, hash: window.location.hash })) })
    edit('#campaign=cmp_A&document=doc_2')
    // The listener runs before the fragment is rewritten (it saw the edit), and the rewrite then restores doc_1.
    expect(heard).toEqual([{ id: 'doc_2', hash: '#campaign=cmp_A&document=doc_2' }])
    expect(window.location.hash).toBe(HASH)
    expect(live.d.documentKey).toBe('doc_1')
    act(() => live.d.setDocumentKey('doc_2'))
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_2')
    // The same id again is not a link; an unsubscribed listener hears nothing.
    edit('#campaign=cmp_A&document=doc_2')
    expect(heard).toHaveLength(1)
    off()
    edit('#campaign=cmp_A&document=doc_3')
    expect(heard).toHaveLength(1)
    expect(server.calls.filter((c) => c.method !== 'GET')).toEqual([])
    expect(server.lines()).toEqual(['GET /campaigns/cmp_A'])
  })

  it('a hash that only drops the document key is rewritten, not treated as a close (I-11)', async () => {
    await mount({ hash: HASH, restore: RESTORE })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    const heard: string[] = []
    act(() => { live.d.onDocumentLink((id) => heard.push(id)) })
    edit('#campaign=cmp_A')
    expect(window.location.hash).toBe(HASH)
    expect(live.d.documentKey).toBe('doc_1')
    expect(heard).toEqual([])
  })

  it('a link to another campaign that names a document restores both, by GET only', async () => {
    const { server } = await mount({ hash: HASH, restore: RESTORE })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    edit('#campaign=cmp_B&document=doc_9')
    await waitFor(() => expect(live.c.selection).toEqual({ kind: 'selected', campaign: B }))
    expect(live.d.documentKey).toBe('doc_9')
    expect(window.location.hash).toBe('#campaign=cmp_B&document=doc_9')
    expect(server.calls.filter((c) => c.method !== 'GET')).toEqual([])
  })

  it('setDocumentKey ignores a malformed id, and any id unless a campaign is selected', async () => {
    await mount({
      hash: HASH, restore: RESTORE,
      route: (call) => (call.url === '/campaigns/cmp_A' ? 'defer' : defaultRoute(call)),
    })
    await flush()
    act(() => live.d.setDocumentKey('doc_other'))
    expect(live.d.documentKey).toBe('doc_1')
    act(() => live.d.setDocumentKey(null))
    expect(live.d.documentKey).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_A')
  })

  it('setDocumentKey refuses an id that is not opaque and writes nothing', async () => {
    await mount({ hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null } })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    act(() => live.d.setDocumentKey('Ondrey the Wise'))
    act(() => live.d.setDocumentKey('doc/../x'))
    expect(live.d.documentKey).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_A')
  })

  it('a stray document key is stripped when no campaign is chosen, and for a player at settle with no request', async () => {
    await mount({ hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null } })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await run(() => live.c.clearCampaign())
    edit('#document=doc_1')
    expect(window.location.hash).toBe('')
  })

  it('a player gets the document key stripped at settle and makes no request (C-12d)', async () => {
    const { server } = await mount({
      role: 'player', hash: HASH, restore: RESTORE,
    })
    expect(window.location.hash).toBe('')
    expect(live.d.documentKey).toBeNull()
    edit(HASH)
    expect(window.location.hash).toBe('')
    expect(server.calls).toHaveLength(0)
  })

  it('an identity change replaces the document key with the new account\'s (none)', async () => {
    const { signal } = await mount({ hash: HASH, restore: RESTORE })
    await waitFor(() => expect(live.c.selection.kind).toBe('selected'))
    await switchAccount(signal, BOB)
    expect(live.d.documentKey).toBeNull()
  })

  it('outside a provider the document hook is inert', () => {
    const held: { value?: CampaignDocumentValue } = {}
    function Bare(): null {
      const value = useCampaignDocument()
      React.useLayoutEffect(() => {
        held.value = value
      })
      return null
    }
    render(<Bare />)
    const bare = held.value as CampaignDocumentValue
    expect(bare.documentKey).toBeNull()
    expect(() => bare.setDocumentKey('doc_1')).not.toThrow()
    expect(bare.onDocumentLink(() => {})).toBeTypeOf('function')
  })
})

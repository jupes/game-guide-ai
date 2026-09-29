/**
 * campaignThreads.test.ts -- a campaign's GM-thread calls and the thread store
 * (agent-forge-harness-1kg.2.5, PR-2; brief sections 7.2 and 7.7, I-12, SEC-4,
 * SEC-7 and the Critic's items 8 and 17).
 *
 * The calls run against a recording `fetch` that can hold an answer back; the
 * store runs against a host whose current scope the test moves by hand.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { createCampaignThread, listCampaignThreads, renameThread } from './campaignApi'
import { ThreadStore, type ThreadHost } from './campaignThreads'

type Reply = { status: number; body?: unknown } | 'network'
interface Call { url: string; method: string; headers: HeadersInit | undefined; body: unknown; reply: (r: Reply) => void }

function server(route: (call: Call) => Reply | 'defer') {
  const calls: Call[] = []
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const call: Call = {
      url: String(input), method: init?.method ?? 'GET', headers: init?.headers,
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
      reply: (r) => (r === 'network'
        ? reject(new TypeError('Failed to fetch'))
        : resolve(new Response(JSON.stringify(r.body ?? {}), { status: r.status }))),
    }
    calls.push(call)
    const answer = route(call)
    if (answer !== 'defer') call.reply(answer)
  })) as typeof fetch
  return { fetchImpl, calls, lines: () => calls.map((c) => `${c.method} ${c.url}`) }
}

const thread = (id: string, over: Record<string, unknown> = {}) => ({
  schema_version: 1, conversation_id: id, campaign_id: 'cmp_A', title: null, started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z', updated_at: null, archived_at: null, ...over,
})
const page = (items: unknown[], next: string | null = null) => ({ schema_version: 1, items, next_cursor: next })
const flush = () => new Promise((resolve) => setTimeout(resolve, 0))
/** The neutral title of a thread made at the fixtures' `created_at`, in this runtime's locale and zone. */
const NEUTRAL = `GM thread · ${new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date('2026-09-16T19:20:11Z'))}`

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the thread calls (section 7.2)', () => {
  it('lists by campaign only -- no started_mode (critic 17) -- keeping the readable rows of that campaign', async () => {
    const s = server(() => ({ status: 200, body: page([thread('cnv_1'), { junk: true }, thread('cnv_x', { campaign_id: 'cmp_B' }), thread('cnv_2', { started_mode: null })], 'cur_1') }))
    const result = await listCampaignThreads('cmp_A', 'cur_1', s.fetchImpl)
    expect(s.lines()).toEqual(['GET /conversations?campaign_id=cmp_A&cursor=cur_1'])
    expect(result).toMatchObject({ kind: 'ok', nextCursor: 'cur_1' })
    expect(result.kind === 'ok' && result.items.map((c) => c.conversation_id)).toEqual(['cnv_1', 'cnv_2'])
  })

  it.each([[503, 'failed'], [403, 'failed'], ['network', 'failed']] as const)('a %s list answer is %s', async (status, kind) => {
    const s = server(() => (status === 'network' ? 'network' : { status }))
    expect(await listCampaignThreads('cmp_A', null, s.fetchImpl)).toEqual({ kind })
  })

  it('creates a GM thread as JSON with no title (RAIL-26, SEC-7)', async () => {
    const s = server(() => ({ status: 201, body: thread('cnv_9') }))
    const result = await createCampaignThread('cmp_A', s.fetchImpl)
    expect(result).toMatchObject({ kind: 'created', conversation: { conversation_id: 'cnv_9' } })
    expect(s.calls[0]).toMatchObject({ method: 'POST', url: '/conversations', headers: { 'Content-Type': 'application/json' } })
    expect(s.calls[0].body).toStrictEqual({ schema_version: 1, started_mode: 'gm', campaign_id: 'cmp_A' })
  })

  it.each([[404, 'unavailable'], [403, 'unavailable'], [422, 'failed'], [503, 'failed']] as const)(
    'a %s create answer is %s', async (status, kind) => {
      expect(await createCampaignThread('cmp_A', server(() => ({ status })).fetchImpl)).toEqual({ kind })
    },
  )

  it('renames as JSON and reads the title back; 404 is gone, 422 invalid, 503 failed', async () => {
    const ok = server(() => ({ status: 200, body: thread('cnv_1', { title: 'The heist' }) }))
    expect(await renameThread('cnv_1', 'The heist', ok.fetchImpl)).toMatchObject({ kind: 'renamed', conversation: { title: 'The heist' } })
    expect(ok.calls[0]).toMatchObject({ method: 'PATCH', url: '/conversations/cnv_1', headers: { 'Content-Type': 'application/json' } })
    expect(ok.calls[0].body).toStrictEqual({ schema_version: 1, title: 'The heist' })
    for (const [status, kind] of [[404, 'gone'], [422, 'invalid'], [503, 'failed']] as const) {
      expect(await renameThread('cnv_1', 'The heist', server(() => ({ status })).fetchImpl)).toEqual({ kind })
    }
  })

  it('makes no request for a malformed id or a refused title (SEC-4); a 401 is the central sign-out', async () => {
    const s = server(() => ({ status: 401 }))
    const signOut = vi.spyOn(api, 'notifyUnauthorized').mockImplementation(() => {})
    expect(await listCampaignThreads('../x', null, s.fetchImpl)).toEqual({ kind: 'failed' })
    expect(await renameThread('a/b', 'Fine', s.fetchImpl)).toEqual({ kind: 'gone' })
    expect(await renameThread('cnv_1', '  ', s.fetchImpl)).toEqual({ kind: 'invalid' })
    expect(await renameThread('cnv_1', 'two\nlines', s.fetchImpl)).toEqual({ kind: 'invalid' })
    expect(s.calls).toEqual([])
    expect(await createCampaignThread('cmp_A', s.fetchImpl)).toEqual({ kind: 'unauthorized' })
    expect(s.lines()).toEqual(['POST /conversations'])
    expect(signOut).toHaveBeenCalledTimes(1)
  })
})

// ── The store ─────────────────────────────────────────────────────────────────

function harness(route: (call: Call) => Reply | 'defer') {
  const s = server(route)
  let current = 'k1'
  const claimed: string[] = []
  const unavailable: string[] = []
  const host: ThreadHost = {
    fetcher: () => s.fetchImpl,
    isCurrentScope: (key) => key === current,
    claim: (key, ids) => claimed.push(...ids.map((id) => `${key}:${id}`)),
    unavailable: (key) => unavailable.push(key),
  }
  const store = new ThreadStore(host)
  const A1 = { campaignId: 'cmp_A', key: 'k1' }
  const view = (key = current) => store.getSnapshot().get(key)
  const titles = (key = current) => view(key)?.rows.map((r) => r.title) ?? []
  return { s, store, A1, view, titles, claimed, unavailable, move: (key: string) => { current = key } }
}

describe('the thread store (critic 8)', () => {
  it('reads the first page once, titles rows neutrally, and claims them for the scope', async () => {
    const h = harness(() => ({ status: 200, body: page([thread('cnv_1', { title: 'Named' }), thread('cnv_2')], 'c2') }))
    h.store.load(h.A1)
    h.store.load(h.A1)
    await flush()
    expect(h.s.lines()).toEqual(['GET /conversations?campaign_id=cmp_A'])
    expect(h.titles()).toEqual(['Named', NEUTRAL])
    expect(h.claimed).toEqual(['k1:cnv_1', 'k1:cnv_2'])
    expect(h.view()).toMatchObject({ nextCursor: 'c2', reading: null, failed: null })
  })

  it('appends a further page without reordering, and retries the page that failed with the rows kept', async () => {
    let n = 0
    const h = harness(({ url }) => {
      n += 1
      if (!url.includes('cursor')) return { status: 200, body: page([thread('cnv_2'), thread('cnv_1', { title: 'One' })], 'c2') }
      return n === 2 ? { status: 503 } : { status: 200, body: page([thread('cnv_1'), thread('cnv_0', { title: 'Zero' })]) }
    })
    h.store.load(h.A1)
    await flush()
    h.store.loadMore(h.A1)
    await flush()
    expect(h.view()).toMatchObject({ failed: 'more', nextCursor: 'c2' })
    h.store.loadMore(h.A1)
    expect(h.s.calls).toHaveLength(2)
    h.store.retry(h.A1)
    await flush()
    expect(h.s.lines().at(-1)).toBe('GET /conversations?campaign_id=cmp_A&cursor=c2')
    expect(h.titles()).toEqual([NEUTRAL, 'One', 'Zero'])
    expect(h.view()).toMatchObject({ failed: null, nextCursor: null })
  })

  it('drops an answer requested under a scope that is no longer current (LIB-25)', async () => {
    const h = harness(() => 'defer')
    h.store.load(h.A1)
    h.move('k2')
    h.s.calls[0].reply({ status: 200, body: page([thread('cnv_1')]) })
    await flush()
    expect(h.claimed).toEqual([])
    expect(h.view('k1')?.rows).toEqual([])
  })

  it('creates one thread per scope however many sends ask, reuses it until opened, and heads the list with it', async () => {
    const h = harness(({ method }) => (method === 'POST' ? 'defer' : { status: 200, body: page([thread('cnv_1')]) }))
    h.store.load(h.A1)
    await flush()
    const first = h.store.ensureThread(h.A1)
    const second = h.store.ensureThread(h.A1)
    expect(h.s.lines().filter((l) => l.startsWith('POST'))).toHaveLength(1)
    h.s.calls[1].reply({ status: 201, body: thread('cnv_new') })
    expect([await first, await second]).toEqual(['cnv_new', 'cnv_new'])
    expect(await h.store.ensureThread(h.A1)).toBe('cnv_new')
    expect(h.s.lines().filter((l) => l.startsWith('POST'))).toHaveLength(1)
    expect(h.view()?.rows.map((r) => r.id)).toEqual(['cnv_new', 'cnv_1'])
    expect(h.claimed).toContain('k1:cnv_new')
    h.store.opened('cnv_new')
    void h.store.ensureThread(h.A1)
    expect(h.s.lines().filter((l) => l.startsWith('POST'))).toHaveLength(2)
  })

  it('a create refused with 404 marks the campaign unavailable; a 503 makes nothing and says null', async () => {
    const h = harness(({ method }) => (method === 'POST' ? { status: 404 } : { status: 200, body: page([]) }))
    expect(await h.store.ensureThread(h.A1)).toBeNull()
    expect(h.unavailable).toEqual(['k1'])
    const f = harness(() => ({ status: 503 }))
    expect(await f.store.ensureThread(f.A1)).toBeNull()
    expect(f.unavailable).toEqual([])
  })

  it('renames in place with the server title, removes a gone row, and changes nothing on invalid or failed', async () => {
    const answers: Reply[] = [{ status: 422 }, { status: 503 }, { status: 200, body: thread('cnv_1', { title: 'Server title' }) }, { status: 404 }]
    const h = harness(({ method }) => (method === 'PATCH' ? answers.shift() ?? { status: 500 } : { status: 200, body: page([thread('cnv_1'), thread('cnv_2')]) }))
    h.store.load(h.A1)
    await flush()
    expect(await h.store.rename(h.A1, 'cnv_1', 'Mine')).toBe('invalid')
    expect(await h.store.rename(h.A1, 'cnv_1', 'Mine')).toBe('failed')
    expect(h.titles()).toEqual([NEUTRAL, NEUTRAL])
    expect(await h.store.rename(h.A1, 'cnv_1', 'Mine')).toBe('renamed')
    expect(await h.store.rename(h.A1, 'cnv_2', 'Mine')).toBe('gone')
    expect(h.view()?.rows.map((r) => `${r.id}:${r.title}`)).toEqual(['cnv_1:Server title'])
  })

  it('an identity change forgets every list and the unopened thread', async () => {
    const h = harness(({ method }) => (method === 'POST' ? { status: 201, body: thread('cnv_new') } : { status: 200, body: page([thread('cnv_1')]) }))
    h.store.load(h.A1)
    await h.store.ensureThread(h.A1)
    h.store.reset()
    expect(h.store.getSnapshot().size).toBe(0)
    await h.store.ensureThread(h.A1)
    expect(h.s.lines().filter((l) => l.startsWith('POST'))).toHaveLength(2)
  })
})

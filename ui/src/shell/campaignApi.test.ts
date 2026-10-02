/**
 * campaignApi.test.ts -- every request's shape and every answer's reading
 * (agent-forge-harness-1kg.2.5, PR-1a; brief section 7.2, SEC-3, SEC-4, SEC-7).
 *
 * The recorder keeps every URL, method, header and body, and each "no request"
 * assertion sits next to a positive control in the same test.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { setUnauthorizedHandler } from '../api'
import {
  concludeCampaign,
  createCampaign,
  getCampaign,
  getConversation,
  listCampaigns,
  listSeats,
  listCampaignSeats,
  reopenCampaign,
} from './campaignApi'

interface Recorded {
  url: string
  method: string
  headers: Record<string, string>
  body: string | null
  credentials: RequestCredentials | undefined
}

type Answer = { status: number; body?: unknown; raw?: string } | 'network'

/** A fetch that records each call and answers from `answers` in order (the
 * last one repeats). */
function recorder(...answers: Answer[]): { fetchImpl: typeof fetch; calls: Recorded[] } {
  const calls: Recorded[] = []
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: init?.method ?? 'GET',
      headers: Object.fromEntries(new Headers(init?.headers).entries()),
      body: typeof init?.body === 'string' ? init.body : null,
      credentials: init?.credentials,
    })
    const answer = answers[Math.min(calls.length - 1, answers.length - 1)]
    if (answer === 'network') throw new TypeError('Failed to fetch')
    const text = answer.raw ?? (answer.body === undefined ? null : JSON.stringify(answer.body))
    return new Response(text, { status: answer.status, headers: { 'Content-Type': 'application/json' } })
  }) as typeof fetch
  return { fetchImpl, calls }
}

const CAMPAIGN = {
  schema_version: 1,
  campaign_id: 'cmp_A',
  name: 'The Nocturne Heist',
  created_at: '2026-09-16T19:20:11Z',
  updated_at: '2026-09-16T19:31:24Z',
  archived_at: null,
  concluded_at: null,
  tone: null,
  game_system: 'dnd5e',
  avatar_icon: 'sailing',
  avatar_tone: 'ember',
  badge: null,
  seat_count: 0,
  last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null,
  dormant: false,
}

const CONVERSATION = {
  schema_version: 1,
  conversation_id: 'cnv_1',
  campaign_id: 'cmp_A',
  title: null,
  started_mode: 'gm',
  created_at: '2026-09-16T19:20:11Z',
  updated_at: null,
  archived_at: null,
}

const REFUSAL = { detail: { code: 'not_found', message: 'secret server wording' } }

afterEach(() => {
  setUnauthorizedHandler(null)
})

describe('listCampaigns', () => {
  it('GETs /campaigns with the cookie and no body, and follows an opaque cursor', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: { schema_version: 1, items: [], next_cursor: null } })
    await listCampaigns(null, fetchImpl)
    await listCampaigns('Zm9v_-1', fetchImpl)

    expect(calls.map((c) => [c.method, c.url, c.credentials, c.body])).toEqual([
      ['GET', '/campaigns', 'include', null],
      ['GET', '/campaigns?cursor=Zm9v_-1', 'include', null],
    ])
  })

  it('reads each item on its own: an unreadable one is dropped and counted, never empties the page', async () => {
    const newer = { ...CAMPAIGN, campaign_id: 'cmp_N', schema_version: 2 }
    const broken = { ...CAMPAIGN, campaign_id: 'cmp_X', name: '' }
    const { fetchImpl } = recorder({
      status: 200,
      body: { schema_version: 1, items: [CAMPAIGN, newer, broken, { ...CAMPAIGN, campaign_id: 'cmp_B' }], next_cursor: 'next' },
    })

    const result = await listCampaigns(null, fetchImpl)

    expect(result.kind).toBe('ok')
    if (result.kind !== 'ok') return
    expect(result.items.map((c) => c.campaign_id)).toEqual(['cmp_A', 'cmp_B'])
    expect(result.dropped).toBe(2)
    expect(result.nextCursor).toBe('next')
  })

  it.each<[string, Answer]>([
    ['a newer envelope', { status: 200, body: { schema_version: 2, items: [], next_cursor: null } }],
    ['no next_cursor', { status: 200, body: { schema_version: 1, items: [] } }],
    ['more than 50 items', { status: 200, body: { schema_version: 1, items: Array(51).fill(CAMPAIGN), next_cursor: null } }],
    ['an HTML page', { status: 200, raw: '<!doctype html>' }],
    ['a 403 (the dm gate)', { status: 403, body: REFUSAL }],
    ['a 404', { status: 404, body: REFUSAL }],
    ['a 422', { status: 422, body: REFUSAL }],
    ['a 503', { status: 503 }],
    ['a network failure', 'network'],
  ])('%s is failed', async (_label, answer) => {
    const { fetchImpl } = recorder(answer)
    expect(await listCampaigns(null, fetchImpl)).toStrictEqual({ kind: 'failed' })
  })

  it('a 401 is the centralized sign-out; a 503 is not', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)

    expect(await listCampaigns(null, recorder({ status: 503 }).fetchImpl)).toStrictEqual({ kind: 'failed' })
    expect(onUnauthorized).not.toHaveBeenCalled()
    expect(await listCampaigns(null, recorder({ status: 401 }).fetchImpl)).toStrictEqual({ kind: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })
})

const SEAT = {
  schema_version: 1,
  campaign_id: 'cmp_S1',
  campaign_name: 'The Hollow Crown',
  alias: 'Brannoc',
  accepted_at: '2026-09-20T18:00:00Z',
  confirmed: true,
  tone: null,
  game_system: 'dnd5e',
  avatar_icon: 'sailing',
  avatar_tone: 'ember',
  concluded: false,
  last_played_at: null,
  live: false,
}
const seatPage = (items: unknown[], next: string | null = null) => ({ schema_version: 1, items, next_cursor: next })

describe('listSeats (30c PR-2)', () => {
  it('GETs /seats with the cookie and no body, and follows an opaque cursor', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: seatPage([]) })
    await listSeats(null, fetchImpl)
    await listSeats('Zm9v_-1', fetchImpl)

    expect(calls.map((c) => [c.method, c.url, c.credentials, c.body])).toEqual([
      ['GET', '/seats', 'include', null],
      ['GET', '/seats?cursor=Zm9v_-1', 'include', null],
    ])
  })

  it('reads each seat on its own: an unreadable one is dropped and counted, never empties the page', async () => {
    const newer = { ...SEAT, campaign_id: 'cmp_N', schema_version: 2 }
    const broken = { ...SEAT, campaign_id: 'cmp_X', campaign_name: '' }
    const { fetchImpl } = recorder({
      status: 200,
      body: seatPage([SEAT, newer, broken, { ...SEAT, campaign_id: 'cmp_S2', live: true }], 'next'),
    })

    const result = await listSeats(null, fetchImpl)

    expect(result.kind).toBe('ok')
    if (result.kind !== 'ok') return
    expect(result.items.map((s) => s.campaign_id)).toEqual(['cmp_S1', 'cmp_S2'])
    expect(result.items[1].live).toBe(true)
    expect(result.dropped).toBe(2)
    expect(result.nextCursor).toBe('next')
  })

  it.each<[string, Answer]>([
    ['a newer envelope', { status: 200, body: { schema_version: 2, items: [], next_cursor: null } }],
    ['no next_cursor', { status: 200, body: { schema_version: 1, items: [] } }],
    ['more than 50 items', { status: 200, body: seatPage(Array(51).fill(SEAT)) }],
    ['an HTML page', { status: 200, raw: '<!doctype html>' }],
    ['a 403', { status: 403, body: REFUSAL }],
    ['a 404', { status: 404, body: REFUSAL }],
    ['a 503', { status: 503 }],
    ['a network failure', 'network'],
  ])('%s is failed', async (_label, answer) => {
    const { fetchImpl } = recorder(answer)
    expect(await listSeats(null, fetchImpl)).toStrictEqual({ kind: 'failed' })
  })

  it('a 401 is the centralized sign-out; a 503 is not', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)

    expect(await listSeats(null, recorder({ status: 503 }).fetchImpl)).toStrictEqual({ kind: 'failed' })
    expect(onUnauthorized).not.toHaveBeenCalled()
    expect(await listSeats(null, recorder({ status: 401 }).fetchImpl)).toStrictEqual({ kind: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })
})

describe('getCampaign', () => {
  it('GETs /campaigns/{id} and reads a live campaign', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: CAMPAIGN })
    const result = await getCampaign('cmp_A', fetchImpl)

    expect(calls.map((c) => [c.method, c.url, c.credentials])).toEqual([['GET', '/campaigns/cmp_A', 'include']])
    expect(result).toEqual({ kind: 'ok', campaign: CAMPAIGN })
  })

  it('a 404, a 403 and an archived campaign are one state that names nothing (SEC-3)', async () => {
    const answers: Answer[] = [
      { status: 404, body: REFUSAL },
      { status: 403, body: REFUSAL },
      { status: 200, body: { ...CAMPAIGN, archived_at: '2026-09-17T00:00:00Z' } },
    ]
    for (const answer of answers) {
      expect(await getCampaign('cmp_A', recorder(answer).fetchImpl)).toStrictEqual({ kind: 'unavailable' })
    }
  })

  it.each<[string, Answer]>([
    ['a 503', { status: 503 }],
    ['a 500', { status: 500 }],
    ['a network failure', 'network'],
    ['an unreadable 200', { status: 200, body: { ...CAMPAIGN, seat_count: -1 } }],
    ['an HTML 200', { status: 200, raw: '<html>' }],
  ])('%s is failed (retryable)', async (_label, answer) => {
    expect(await getCampaign('cmp_A', recorder(answer).fetchImpl)).toStrictEqual({ kind: 'failed' })
  })

  it('a 401 is the centralized sign-out', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    expect(await getCampaign('cmp_A', recorder({ status: 401 }).fetchImpl)).toStrictEqual({ kind: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('a malformed id makes no request (SEC-4)', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: CAMPAIGN })
    for (const id of ['', '../auth/me', 'cmp A', 'cmp%2FA', 'a'.repeat(65)]) {
      expect(await getCampaign(id, fetchImpl)).toStrictEqual({ kind: 'unavailable' })
    }
    expect(calls).toHaveLength(0)
    await getCampaign('cmp_A', fetchImpl)
    expect(calls).toHaveLength(1)
  })
})

describe('createCampaign', () => {
  it('POSTs the name as JSON, with the JSON content type the origin check needs (SEC-7), and no tone key', async () => {
    const { fetchImpl, calls } = recorder({ status: 201, body: CAMPAIGN })
    const result = await createCampaign('The Nocturne Heist', undefined, fetchImpl)

    expect(result).toEqual({ kind: 'created', campaign: CAMPAIGN })
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ url: '/campaigns', method: 'POST', credentials: 'include' })
    expect(calls[0].headers['content-type']).toBe('application/json')
    const sent: unknown = JSON.parse(calls[0].body ?? 'null')
    expect(sent).toStrictEqual({ schema_version: 1, name: 'The Nocturne Heist' })
    expect(sent).not.toHaveProperty('tone')
  })

  it('sends a tone line only when one is given (A-31(d))', async () => {
    const { fetchImpl, calls } = recorder({ status: 201, body: CAMPAIGN })
    await createCampaign('Heist', 'Mystery', fetchImpl)
    expect(JSON.parse(calls[0].body ?? 'null')).toStrictEqual({ schema_version: 1, name: 'Heist', tone: 'Mystery' })
  })

  it('a client-invalid name makes no request', async () => {
    const { fetchImpl, calls } = recorder({ status: 201, body: CAMPAIGN })
    const nul = `a${String.fromCodePoint(0)}b`
    const loneSurrogate = `a${String.fromCharCode(0xd800)}b`
    for (const name of ['', '   ', 'x'.repeat(121), nul, loneSurrogate]) {
      expect(await createCampaign(name, undefined, fetchImpl)).toStrictEqual({ kind: 'invalid' })
    }
    expect(calls).toHaveLength(0)
    await createCampaign('ok', undefined, fetchImpl)
    expect(calls).toHaveLength(1)
  })

  it('a 422 is invalid', async () => {
    expect(await createCampaign('Heist', undefined, recorder({ status: 422, body: REFUSAL }).fetchImpl))
      .toStrictEqual({ kind: 'invalid' })
  })

  it.each<[string, Answer]>([
    ['a 503', { status: 503 }],
    ['a network failure', 'network'],
    ['a 403', { status: 403, body: REFUSAL }],
    ['an unreadable 201', { status: 201, body: { ...CAMPAIGN, campaign_id: 'no/pe' } }],
  ])('%s is failed after exactly ONE request: a create is never retried here', async (_label, answer) => {
    const { fetchImpl, calls } = recorder(answer, { status: 201, body: CAMPAIGN })
    expect(await createCampaign('Heist', undefined, fetchImpl)).toStrictEqual({ kind: 'failed' })
    expect(calls).toHaveLength(1)
  })

  it('a 401 is the centralized sign-out', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    expect(await createCampaign('Heist', undefined, recorder({ status: 401 }).fetchImpl))
      .toStrictEqual({ kind: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })
})

describe('getConversation', () => {
  it('GETs /conversations/{id} -- the non-claiming read -- and returns the row', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: CONVERSATION })
    const result = await getConversation('cnv_1', fetchImpl)

    expect(calls.map((c) => [c.method, c.url, c.credentials])).toEqual([['GET', '/conversations/cnv_1', 'include']])
    expect(result).toEqual({ kind: 'ok', conversation: CONVERSATION })
  })

  it.each<[string, Answer, string]>([
    ['a 404', { status: 404, body: REFUSAL }, 'gone'],
    ['a 403', { status: 403, body: REFUSAL }, 'gone'],
    ['a 503', { status: 503 }, 'failed'],
    ['a network failure', 'network', 'failed'],
    ['a newer row', { status: 200, body: { ...CONVERSATION, schema_version: 2 } }, 'failed'],
  ])('%s is %s', async (_label, answer, kind) => {
    expect(await getConversation('cnv_1', recorder(answer).fetchImpl)).toStrictEqual({ kind })
  })

  it('a 401 is the centralized sign-out', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    expect(await getConversation('cnv_1', recorder({ status: 401 }).fetchImpl)).toStrictEqual({ kind: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('a malformed id makes no request', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: CONVERSATION })
    expect(await getConversation('cnv/1', fetchImpl)).toStrictEqual({ kind: 'gone' })
    expect(calls).toHaveLength(0)
    await getConversation('cnv_1', fetchImpl)
    expect(calls).toHaveLength(1)
  })
})

// ── 1kg.7.3: the seats the reveal sheet names ────────────────────────────────

const GM_SEAT = {
  schema_version: 1,
  participant_id: 'par_brannSeat000000000001',
  alias: 'Brann',
  status: 'confirmed',
  address: null,
  created_at: '2026-09-16T19:20:11Z',
  offered_at: null,
  offer_expires_at: null,
  accepted_at: null,
  confirmed_at: '2026-09-16T19:25:00Z',
  removed_at: null,
}

describe('listCampaignSeats', () => {
  it('GETs one page of fifty, with the cookie and no body, and reads the seats', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: { schema_version: 1, items: [GM_SEAT], next_cursor: null } })
    const result = await listCampaignSeats('cmp_A', fetchImpl)
    expect(result.kind).toBe('ok')
    if (result.kind === 'ok') expect(result.items.map((seat) => seat.alias)).toEqual(['Brann'])
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ url: '/campaigns/cmp_A/participants?limit=50', method: 'GET', body: null, credentials: 'include' })
  })

  it('403, 404, 5xx, a network failure and an unreadable body are failed; a 401 signs out', async () => {
    const answers: Answer[] = [{ status: 403 }, { status: 404 }, { status: 503 }, 'network', { status: 200, raw: '<html>' }, { status: 200, body: { items: 'x' } }]
    for (const answer of answers) {
      expect(await listCampaignSeats('cmp_A', recorder(answer).fetchImpl)).toEqual({ kind: 'failed' })
    }
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    expect(await listCampaignSeats('cmp_A', recorder({ status: 401 }).fetchImpl)).toEqual({ kind: 'unauthorized' })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('a malformed campaign id makes no request', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: { schema_version: 1, items: [], next_cursor: null } })
    expect(await listCampaignSeats('../x', fetchImpl)).toEqual({ kind: 'failed' })
    expect(calls).toHaveLength(0)
    expect((await listCampaignSeats('cmp_A', fetchImpl)).kind).toBe('ok')
  })
})

// ── 30c PR-1: conclude and reopen (A-1 to A-6) ──────────────────────────────

const CONCLUDED = { ...CAMPAIGN, concluded_at: '2026-09-20T10:00:00Z' }

describe.each([
  ['concludeCampaign', concludeCampaign, 'conclude'],
  ['reopenCampaign', reopenCampaign, 'reopen'],
] as const)('%s', (_name, call, verb) => {
  it(`A-1 POSTs /campaigns/{id}/${verb} with the cookie, no body and no content type`, async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: CONCLUDED })
    await call('cmp_A', fetchImpl)
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ url: `/campaigns/cmp_A/${verb}`, method: 'POST', body: null, credentials: 'include' })
    expect(calls[0].headers['content-type']).toBeUndefined()
  })

  it('A-2 a malformed id makes no request (SEC-4), with a positive control', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: CONCLUDED })
    expect(await call('cmp/A', fetchImpl)).toStrictEqual({ kind: 'unavailable' })
    expect(await call('../campaigns', fetchImpl)).toStrictEqual({ kind: 'unavailable' })
    expect(calls).toHaveLength(0)
    await call('cmp_A', fetchImpl)
    expect(calls).toHaveLength(1)
  })

  it('A-3 a 401 is the centralized sign-out, once', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    expect(await call('cmp_A', recorder({ status: 401 }).fetchImpl)).toStrictEqual({ kind: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it.each<[string, Answer, string]>([
    ['a 404', { status: 404, body: REFUSAL }, 'unavailable'],
    ['a 403', { status: 403, body: REFUSAL }, 'unavailable'],
    ['a 503', { status: 503 }, 'failed'],
    ['a 500 with a body', { status: 500, body: REFUSAL }, 'failed'],
    ['a network failure', 'network', 'failed'],
    ['an unreadable body', { status: 200, raw: '<html>not json' }, 'failed'],
  ])('A-4 and A-5 %s is %s, and names nothing', async (_label, answer, kind) => {
    expect(await call('cmp_A', recorder(answer).fetchImpl)).toStrictEqual({ kind })
  })

  it('A-6 returns the campaign the server answered with, read through the contract', async () => {
    expect(await call('cmp_A', recorder({ status: 200, body: CONCLUDED }).fetchImpl)).toStrictEqual({ kind: 'ok', campaign: CONCLUDED })
    // A body the contract refuses is a failure, never a half-read campaign.
    expect(await call('cmp_A', recorder({ status: 200, body: { ...CONCLUDED, seat_count: -1 } }).fetchImpl)).toStrictEqual({ kind: 'failed' })
    expect(await call('cmp_A', recorder({ status: 200, body: { ...CONCLUDED, schema_version: 2 } }).fetchImpl)).toStrictEqual({ kind: 'failed' })
  })
})

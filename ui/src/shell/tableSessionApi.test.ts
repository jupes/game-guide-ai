/**
 * tableSessionApi.test.ts -- each request's shape and each answer's reading
 * (agent-forge-harness-1kg.2.5.3; brief section 7.9, SEC-4, SEC-7, SEC-43, T4-5).
 * Every "no request" assertion sits beside a positive control in the same test.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { setUnauthorizedHandler } from '../api'
import { mintCommandId, readTableSession, sendTableSession } from './tableSessionApi'

type Answer = { status: number; body?: unknown; raw?: string } | 'network'

/** A fetch that records each call and answers from `answers` in order (the last repeats). */
function recorder(...answers: Answer[]) {
  const calls: { url: string; method: string; headers: Record<string, string>; body: unknown; credentials?: RequestCredentials }[] = []
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null
    const headers = Object.fromEntries(new Headers(init?.headers).entries())
    calls.push({ url: String(input), method: init?.method ?? 'GET', headers, body, credentials: init?.credentials })
    const answer = answers[Math.min(calls.length - 1, answers.length - 1)]
    if (answer === 'network') throw new TypeError('Failed to fetch')
    return new Response(answer.raw ?? JSON.stringify(answer.body ?? null), { status: answer.status })
  }) as typeof fetch
  return { fetchImpl, calls }
}

const LIVE = {
  schema_version: 1, session_id: 'tss_1', campaign_id: 'cmp_A', state: 'live', gen: 1, audio_epoch: 2, reveal_epoch: 3,
  started_at: '2026-09-29T18:00:00Z', ends_at: '2026-09-30T06:00:00Z', ended_at: null, audio: false, screens: [],
}
const refusal = (status: number, code: string, extra: object = {}): Answer => ({
  status,
  body: { detail: { code, message: 'server words are never shown', retryable: status !== 403, ...extra } },
})

afterEach(() => setUnauthorizedHandler(null))

describe('readTableSession', () => {
  it('GETs the campaign address with the cookie and nothing else, and reads a null session', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: { schema_version: 1, session: LIVE } }, { status: 200, body: { schema_version: 1, session: null } })
    expect(await readTableSession('cmp_A', fetchImpl)).toEqual({ kind: 'ok', session: LIVE })
    expect(calls).toEqual([{ url: '/campaigns/cmp_A/table-session', method: 'GET', headers: {}, body: null, credentials: 'include' }])
    expect(await readTableSession('cmp_A', fetchImpl)).toEqual({ kind: 'ok', session: null })
  })

  it('T4-5: a token in an answer is dropped at the parse, never handed on', async () => {
    const body = { schema_version: 1, token: 'secret-1', session: { ...LIVE, token: 'secret-2', join_url: '/t/x' } }
    const result = await readTableSession('cmp_A', recorder({ status: 200, body }).fetchImpl)
    expect(result.kind).toBe('ok')
    expect(JSON.stringify(result)).not.toMatch(/secret|join_url|token/)
  })

  it('makes no request for a malformed campaign id (positive control: a good id does)', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: { schema_version: 1, session: null } })
    expect(await readTableSession('../seats/st_1', fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(calls).toHaveLength(0)
    await readTableSession('cmp_A', fetchImpl)
    expect(calls).toHaveLength(1)
  })

  it('maps 401 to the centralized sign-out, 404 to unavailable and 403 to refused', async () => {
    const signedOut = vi.fn()
    setUnauthorizedHandler(signedOut)
    const read = (answer: Answer) => readTableSession('cmp_A', recorder(answer).fetchImpl)
    expect(await read({ status: 401, body: { detail: 'not signed in' } })).toEqual({ kind: 'unauthorized' })
    expect(signedOut).toHaveBeenCalledTimes(1)
    expect(await read(refusal(404, 'not_found'))).toEqual({ kind: 'unavailable' })
    expect(await read(refusal(403, 'forbidden'))).toEqual({ kind: 'refused', code: 'forbidden' })
  })

  it.each<[string, Answer]>([
    ['a 503', refusal(503, 'backend_unavailable')],
    ['a network failure', 'network'],
    ['an unreadable body', { status: 200, raw: '<html>' }],
    ['a session that breaks the contract', { status: 200, body: { schema_version: 1, session: { ...LIVE, state: 'paused' } } }],
  ])('reads %s as failed', async (_label, answer) => {
    expect(await readTableSession('cmp_A', recorder(answer).fetchImpl)).toEqual({ kind: 'failed' })
  })
})

describe('sendTableSession', () => {
  it('POSTs JSON: a Start names no session, an End names its own', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: { schema_version: 1, session: LIVE } })
    await sendTableSession('cmp_A', { action: 'start', commandId: 'cmd_aaaaaaaaaaaaaaaa' }, fetchImpl)
    await sendTableSession('cmp_A', { action: 'end', commandId: 'cmd_bbbbbbbbbbbbbbbb', sessionId: 'tss_1' }, fetchImpl)
    for (const call of calls) {
      expect(call).toMatchObject({ url: '/campaigns/cmp_A/table-session', method: 'POST', credentials: 'include' })
      expect(call.headers['content-type']).toBe('application/json')
    }
    expect(calls.map((call) => call.body)).toStrictEqual([
      { schema_version: 1, command_id: 'cmd_aaaaaaaaaaaaaaaa', action: 'start' },
      { schema_version: 1, command_id: 'cmd_bbbbbbbbbbbbbbbb', action: 'end', session_id: 'tss_1' },
    ])
  })

  it('refuses a malformed command id with no request (positive control: a minted one is sent)', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: { schema_version: 1, session: LIVE } })
    expect(await sendTableSession('cmp_A', { action: 'start', commandId: 'short' }, fetchImpl)).toEqual({ kind: 'failed' })
    expect(calls).toHaveLength(0)
    await sendTableSession('cmp_A', { action: 'start', commandId: mintCommandId() }, fetchImpl)
    expect(calls).toHaveLength(1)
  })

  it.each<[string, Answer, unknown]>([
    ['409 live_elsewhere', refusal(409, 'live_elsewhere'), { kind: 'live_elsewhere' }],
    ['409 with another code', refusal(409, 'conflict'), { kind: 'failed' }],
    ['429 with a wait', refusal(429, 'throttled_user', { retry_after_s: 42 }), { kind: 'throttled', retryAfterS: 42 }],
    ['429 without one', refusal(429, 'throttled_user'), { kind: 'throttled', retryAfterS: null }],
    ['403 with an unknown code', refusal(403, 'plan_required'), { kind: 'refused', code: 'plan_required' }],
    ['403 unreadable', { status: 403, raw: 'no' }, { kind: 'refused', code: null }],
    ['422', refusal(422, 'validation_failed'), { kind: 'failed' }],
  ])('maps %s', async (_label, answer, expected) => {
    const result = await sendTableSession('cmp_A', { action: 'start', commandId: mintCommandId() }, recorder(answer).fetchImpl)
    expect(result).toEqual(expected)
  })

  it('mints a fresh command id the contract accepts', () => {
    const ids = new Set(Array.from({ length: 20 }, () => mintCommandId()))
    expect(ids.size).toBe(20)
    for (const id of ids) expect(id).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
  })
})

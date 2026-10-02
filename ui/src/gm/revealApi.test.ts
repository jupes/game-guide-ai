/**
 * revealApi.test.ts -- the reveal client (agent-forge-harness-1kg.7.3, brief 3.1,
 * tests 11 and 12, and the Critic's item 1: the 422 body is the Workbench ErrorBody).
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { confirmReveal, readReveals, stopReveal } from './revealApi'
import { liveFixture, pictureFixture } from './revealFixtures'

const CAMPAIGN = 'cmp_revealApiCampaign000001'
const DOCUMENT = 'doc_revealApiDocument0000001'
const SESSION = 'ses_revealApiSession00000001'
const COMMAND = 'cmd_revealApiCommand00000001'

interface Seen {
  url: string
  init: RequestInit
}

function server(reply: { status: number; body?: unknown; raw?: string } | 'network') {
  const seen: Seen[] = []
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    seen.push({ url: String(input), init: init ?? {} })
    if (reply === 'network') throw new TypeError('Failed to fetch')
    return new Response(reply.raw ?? JSON.stringify(reply.body ?? {}), { status: reply.status })
  }) as unknown as typeof fetch
  return { seen, fetchImpl }
}

const answer = (state: unknown) => ({ schema_version: 1, state })

const REQUEST = {
  command_id: COMMAND,
  document_id: DOCUMENT,
  session_id: SESSION,
  reveal_epoch: 3,
  version: 2,
  mask: ['name', 'voice'],
  audience: { kind: 'table' as const },
}

const errorBody = (code: string, extra: Record<string, unknown> = {}) => ({
  detail: { code, message: 'private words the client must not show', retryable: false, ...extra },
})

afterEach(() => vi.restoreAllMocks())

describe('readReveals', () => {
  it('reads the picture, or null when there is no live session', async () => {
    const picture = pictureFixture({ table: liveFixture(DOCUMENT, ['name']) })
    const live = server({ status: 200, body: answer(picture) })
    expect(await readReveals(CAMPAIGN, live.fetchImpl)).toEqual({ kind: 'ok', state: picture })
    expect(live.seen[0].url).toBe(`/campaigns/${CAMPAIGN}/reveals`)
    expect(live.seen[0].init.credentials).toBe('include')
    expect(await readReveals(CAMPAIGN, server({ status: 200, body: answer(null) }).fetchImpl)).toEqual({
      kind: 'ok',
      state: null,
    })
  })

  it('maps every status', async () => {
    const kind = async (reply: Parameters<typeof server>[0]) => (await readReveals(CAMPAIGN, server(reply).fetchImpl)).kind
    expect(await kind({ status: 403 })).toBe('unavailable')
    expect(await kind({ status: 404 })).toBe('unavailable')
    expect(await kind({ status: 503 })).toBe('failed')
    expect(await kind({ status: 500 })).toBe('failed')
    expect(await kind('network')).toBe('failed')
    expect(await kind({ status: 200, raw: 'not json' })).toBe('failed')
    // Fail closed: a body that does not parse is failed, never `ok` with nothing.
    expect(await kind({ status: 200, body: { schema_version: 1, state: { session_id: 'x' } } })).toBe('failed')
  })

  it('a 401 is the centralized sign-out', async () => {
    const out = vi.spyOn(api, 'notifyUnauthorized').mockImplementation(() => undefined)
    expect((await readReveals(CAMPAIGN, server({ status: 401 }).fetchImpl)).kind).toBe('unauthorized')
    expect(out).toHaveBeenCalledTimes(1)
  })

  it('a malformed id makes no request', async () => {
    const s = server({ status: 200, body: answer(null) })
    expect(await readReveals('../etc', s.fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(s.seen).toHaveLength(0)
  })
})

describe('confirmReveal: the request (test 11)', () => {
  it('sends exactly what the schema builds, as JSON, with the cookie', async () => {
    const s = server({ status: 200, body: answer(null) })
    await confirmReveal(CAMPAIGN, REQUEST, s.fetchImpl)
    expect(s.seen).toHaveLength(1)
    expect(s.seen[0].url).toBe(`/campaigns/${CAMPAIGN}/reveals`)
    expect(s.seen[0].init.method).toBe('POST')
    expect(s.seen[0].init.credentials).toBe('include')
    expect(s.seen[0].init.headers).toEqual({ 'Content-Type': 'application/json' })
    // Mutation: spreading extra fields into the body fails this exact comparison.
    expect(JSON.parse(String(s.seen[0].init.body))).toEqual({ schema_version: 1, ...REQUEST })
  })

  it('refuses to send a request the contract would refuse', async () => {
    const s = server({ status: 200, body: answer(null) })
    expect((await confirmReveal(CAMPAIGN, { ...REQUEST, mask: [] }, s.fetchImpl)).kind).toBe('failed')
    expect((await confirmReveal(CAMPAIGN, { ...REQUEST, mask: ['all'] }, s.fetchImpl)).kind).toBe('failed')
    expect((await confirmReveal(CAMPAIGN, { ...REQUEST, mask: ['name', 'name'] }, s.fetchImpl)).kind).toBe('failed')
    expect(
      (await confirmReveal(CAMPAIGN, { ...REQUEST, audience: { kind: 'participants', participant_ids: [] } }, s.fetchImpl)).kind,
    ).toBe('failed')
    expect(s.seen).toHaveLength(0)
  })

  it('a malformed campaign id makes no request', async () => {
    const s = server({ status: 200, body: answer(null) })
    expect(await confirmReveal('a b', REQUEST, s.fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(s.seen).toHaveLength(0)
  })

  it('carries no field text in a URL or a body: keys only', async () => {
    const CANARY = 'Harbour almoner CANARY-7731'
    const s = server({ status: 200, body: answer(null) })
    // The caller only ever holds keys; the canary lives in the document data, which this call never receives.
    await confirmReveal(CAMPAIGN, { ...REQUEST, mask: ['name'] }, s.fetchImpl)
    const seen = JSON.stringify(s.seen)
    expect(seen).not.toContain(CANARY)
    expect(seen).not.toContain('CANARY')
    expect(Object.keys(JSON.parse(String(s.seen[0].init.body))).sort()).toEqual(
      ['audience', 'command_id', 'document_id', 'mask', 'reveal_epoch', 'schema_version', 'session_id', 'version'].sort(),
    )
  })
})

describe('confirmReveal: every answer (test 12 and the Critic 1)', () => {
  const confirm = async (reply: Parameters<typeof server>[0]) => confirmReveal(CAMPAIGN, REQUEST, server(reply).fetchImpl)

  it('200 is ok with the picture; a picture that does not parse is failed', async () => {
    const picture = pictureFixture({ table: liveFixture(DOCUMENT, ['name', 'voice']) })
    expect(await confirm({ status: 200, body: answer(picture) })).toEqual({ kind: 'ok', state: picture })
    expect((await confirm({ status: 200, body: { schema_version: 1, state: { nope: 1 } } })).kind).toBe('failed')
  })

  it('409 is a conflict, 404 and 403 are unavailable, 5xx and a network error are failed', async () => {
    expect((await confirm({ status: 409, body: errorBody('conflict') })).kind).toBe('conflict')
    expect((await confirm({ status: 404, body: errorBody('not_found') })).kind).toBe('unavailable')
    expect((await confirm({ status: 403, body: errorBody('forbidden') })).kind).toBe('unavailable')
    expect((await confirm({ status: 503 })).kind).toBe('failed')
    expect((await confirm('network')).kind).toBe('failed')
  })

  it('a 429 carries retry_after_s when the server gave one', async () => {
    expect(await confirm({ status: 429, body: errorBody('throttled', { retry_after_s: 7 }) })).toEqual({
      kind: 'throttled',
      retryAfterS: 7,
    })
    expect(await confirm({ status: 429, body: {} })).toEqual({ kind: 'throttled', retryAfterS: null })
  })

  it('a 422 on the mask names the keys at fault', async () => {
    expect(await confirm({ status: 422, body: errorBody('validation', { field: 'mask', keys: ['wants'] }) })).toEqual({
      kind: 'mask_refused',
      keys: ['wants'],
    })
    expect(await confirm({ status: 422, body: errorBody('validation', { field: 'mask' }) })).toEqual({
      kind: 'mask_refused',
      keys: [],
    })
  })

  it.each(['audience', 'document_id', 'version'] as const)('a 422 on %s is refused with that field', async (field) => {
    // Mutation: mapping a 422 audience to `failed`, or reading FastAPI's `loc`, fails this.
    expect(await confirm({ status: 422, body: errorBody('validation', { field }) })).toEqual({ kind: 'refused', field })
  })

  it('a 422 with any other field, or a body this client cannot read, is refused with no field', async () => {
    expect(await confirm({ status: 422, body: errorBody('validation', { field: 'epoch' }) })).toEqual({ kind: 'refused', field: null })
    expect(await confirm({ status: 422, body: errorBody('validation') })).toEqual({ kind: 'refused', field: null })
    expect(await confirm({ status: 422, body: { detail: [{ loc: ['body', 'mask'], msg: 'x', type: 'y' }] } })).toEqual({
      kind: 'refused',
      field: null,
    })
    expect(await confirm({ status: 422, raw: '<html>' })).toEqual({ kind: 'refused', field: null })
  })

  it('never returns a server message', async () => {
    const result = await confirm({ status: 422, body: errorBody('validation', { field: 'audience' }) })
    expect(JSON.stringify(result)).not.toContain('private words')
  })

  it('a 401 is the centralized sign-out', async () => {
    const out = vi.spyOn(api, 'notifyUnauthorized').mockImplementation(() => undefined)
    expect((await confirm({ status: 401 })).kind).toBe('unauthorized')
    expect(out).toHaveBeenCalledTimes(1)
  })
})

describe('stopReveal (tests 11 and 12)', () => {
  const stop = (reply: Parameters<typeof server>[0], scope: Parameters<typeof stopReveal>[1] = { commandId: COMMAND, scope: 'document', documentId: DOCUMENT }) =>
    stopReveal(CAMPAIGN, scope, server(reply).fetchImpl)

  it('names the document and carries no epoch', async () => {
    const s = server({ status: 200, body: answer(null) })
    await stopReveal(CAMPAIGN, { commandId: COMMAND, scope: 'document', documentId: DOCUMENT }, s.fetchImpl)
    expect(s.seen[0].url).toBe(`/campaigns/${CAMPAIGN}/reveals/stop`)
    const body = JSON.parse(String(s.seen[0].init.body)) as Record<string, unknown>
    // Mutation: adding reveal_epoch to a Stop fails this.
    expect(body).toEqual({ schema_version: 1, command_id: COMMAND, scope: 'document', document_id: DOCUMENT })
    expect(body).not.toHaveProperty('reveal_epoch')
    expect(s.seen[0].init.credentials).toBe('include')
  })

  it('scope all names no document', async () => {
    const s = server({ status: 200, body: answer(null) })
    await stopReveal(CAMPAIGN, { commandId: COMMAND, scope: 'all' }, s.fetchImpl)
    expect(JSON.parse(String(s.seen[0].init.body))).toEqual({ schema_version: 1, command_id: COMMAND, scope: 'all' })
  })

  it('maps every answer', async () => {
    const picture = pictureFixture()
    expect(await stop({ status: 200, body: answer(picture) })).toEqual({ kind: 'ok', state: picture })
    expect(await stop({ status: 200, body: answer(null) })).toEqual({ kind: 'ok', state: null })
    expect((await stop({ status: 404 })).kind).toBe('gone')
    expect((await stop({ status: 403 })).kind).toBe('gone')
    expect((await stop({ status: 422, body: errorBody('validation') })).kind).toBe('invalid')
    expect(await stop({ status: 429, body: errorBody('throttled', { retry_after_s: 40 }) })).toEqual({ kind: 'throttled', retryAfterS: 40 })
    expect((await stop({ status: 503 })).kind).toBe('failed')
    expect((await stop('network')).kind).toBe('failed')
    expect((await stop({ status: 200, body: { nope: 1 } })).kind).toBe('failed')
  })

  it('a 401 signs out, and a malformed id sends nothing', async () => {
    const out = vi.spyOn(api, 'notifyUnauthorized').mockImplementation(() => undefined)
    expect((await stop({ status: 401 })).kind).toBe('unauthorized')
    expect(out).toHaveBeenCalledTimes(1)
    const s = server({ status: 200, body: answer(null) })
    expect(await stopReveal(CAMPAIGN, { commandId: COMMAND, scope: 'document', documentId: 'bad id' }, s.fetchImpl)).toEqual({
      kind: 'invalid',
    })
    expect(s.seen).toHaveLength(0)
  })
})

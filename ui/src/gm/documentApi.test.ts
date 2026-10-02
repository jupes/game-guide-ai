/**
 * documentApi.test.ts -- every request's shape and every answer's reading
 * (agent-forge-harness-1kg.6.3, T-4; SEC-3, SEC-4, SEC-7, X-7, X-8, LIB-25).
 *
 * The recorder keeps every URL, method, header and body, and each "no request"
 * assertion sits next to a positive control in the same test.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { setUnauthorizedHandler } from '../api'
import { LIBRARY_CATEGORIES, type LibraryQuery } from './contracts'
import { DOCUMENT_FIXTURES } from './documentFixtures'
import {
  getCharacterSheetLink,
  getDocument,
  getDocumentHistory,
  getDocumentVersion,
  queryLibrary,
  sealDocument,
} from './documentApi'

interface Recorded {
  url: string
  method: string
  headers: Record<string, string>
  body: string | null
  credentials: RequestCredentials | undefined
}

type Answer = { status: number; body?: unknown; raw?: string } | 'network' | 'abort'

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
    if (answer === 'abort') throw new DOMException('aborted', 'AbortError')
    const text = answer.raw ?? (answer.body === undefined ? null : JSON.stringify(answer.body))
    return new Response(text, { status: answer.status, headers: { 'Content-Type': 'application/json' } })
  }) as typeof fetch
  return { fetchImpl, calls }
}

const CID = 'cmp_e2eWorkbenchCampaign000001'
const DID = 'doc_e2eOndreyDocument0000001'
const NPC = { ...DOCUMENT_FIXTURES.npc, campaign_id: CID, document_id: DID }

const VERSION = {
  number: 2,
  author: 'gm',
  summary: 'Wants the signet',
  created_at: '2026-09-16T19:36:00Z',
  sealed: true,
  changed_fields: ['wants'],
  restored_from: null,
}
const HISTORY = { schema_version: 1, document_id: DID, items: [VERSION], next_cursor: null }

const LIBRARY_QUERY: LibraryQuery = {
  schema_version: 1,
  campaign_id: CID,
  category: 'npcs',
  search: '',
  sort: 'recent',
  archived: false,
  limit: 5,
}
const LIBRARY_PAGE = {
  schema_version: 1,
  campaign_id: CID,
  category: 'npcs',
  items: [
    {
      document_id: DID,
      type: 'npc',
      title: 'Ondrey',
      qualifier: '',
      tags: [],
      archived: false,
      updated_at: '2026-09-16T19:36:00Z',
    },
  ],
  next_cursor: null,
}

afterEach(() => {
  setUnauthorizedHandler(null)
})

describe('getDocument', () => {
  it('reads a document with a GET that carries the cookie and nothing else', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: NPC })
    const result = await getDocument(CID, DID, fetchImpl)
    expect(result.kind).toBe('ok')
    if (result.kind === 'ok') expect(result.document.data.name).toBe('Sister Ondrey Vashe')
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({
      url: `/campaigns/${CID}/documents/${DID}`,
      method: 'GET',
      body: null,
      credentials: 'include',
    })
  })

  it('a 401 is the centralized sign-out, once, and reads as unauthorized', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    const { fetchImpl } = recorder({ status: 401, body: {} })
    expect(await getDocument(CID, DID, fetchImpl)).toEqual({ kind: 'unauthorized' })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('403 and 404 are the same unavailable state, which never says which it was', async () => {
    const forbidden = await getDocument(CID, DID, recorder({ status: 403, body: { code: 'forbidden' } }).fetchImpl)
    const missing = await getDocument(CID, DID, recorder({ status: 404, body: { code: 'not_found' } }).fetchImpl)
    expect(forbidden).toEqual({ kind: 'unavailable' })
    expect(missing).toEqual(forbidden)
  })

  it.each([
    ['a 500', { status: 500, body: {} }],
    ['a 503', { status: 503, body: {} }],
    ['a network failure', 'network'],
    ['an aborted request', 'abort'],
    ['a body that is not JSON', { status: 200, raw: '<html>' }],
    ['a body that is not a document at all', { status: 200, body: { hello: 'world' } }],
    ['a document of another campaign', { status: 200, body: { ...NPC, campaign_id: 'cmp_somebodyElse0000000001' } }],
    ['another document than the one asked for', { status: 200, body: { ...NPC, document_id: 'doc_aDifferentDocument00001' } }],
  ] as Array<[string, Answer]>)('%s is failed', async (_label, answer) => {
    const result = await getDocument(CID, DID, recorder(answer).fetchImpl)
    expect(result).toEqual({ kind: 'failed' })
  })

  it.each([
    ['a newer schema version', { ...NPC, schema_version: 2 }],
    ['a type this client does not know', { ...NPC, type: 'dragon-hoard' }],
    ['a newer type version', { ...NPC, type_version: 99 }],
  ])('%s is unsupported, not an outage (X-8)', async (_label, body) => {
    expect(await getDocument(CID, DID, recorder({ status: 200, body }).fetchImpl)).toEqual({ kind: 'unsupported' })
  })

  it.each([
    ['a campaign id with a slash', 'cmp_a/../b', DID],
    ['a document id with a space', CID, 'doc 1'],
    ['an empty document id', CID, ''],
    ['a percent-encoded id', CID, 'doc%5F1'],
    ['an id past 64 characters', CID, `doc_${'a'.repeat(70)}`],
  ])('%s makes no request and is unavailable (SEC-4)', async (_label, campaignId, documentId) => {
    const { fetchImpl, calls } = recorder({ status: 200, body: NPC })
    expect(await getDocument(campaignId, documentId, fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(calls).toHaveLength(0)
    // Positive control: the same recorder does see a well-formed request.
    await getDocument(CID, DID, fetchImpl)
    expect(calls).toHaveLength(1)
  })
})

describe('getDocumentHistory', () => {
  it('asks for 20, newest first, with no cursor on the first page', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: HISTORY })
    const result = await getDocumentHistory(CID, DID, null, fetchImpl)
    expect(result.kind).toBe('ok')
    expect(calls[0].url).toBe(`/campaigns/${CID}/documents/${DID}/versions?limit=20`)
    expect(calls[0]).toMatchObject({ method: 'GET', credentials: 'include' })
  })

  it('sends the server’s own cursor, encoded, on the next page', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: HISTORY })
    await getDocumentHistory(CID, DID, 'abc_DEF-123', fetchImpl)
    expect(calls[0].url).toBe(`/campaigns/${CID}/documents/${DID}/versions?limit=20&cursor=abc_DEF-123`)
  })

  it('maps 401, 403, 404, 5xx, a network failure and a foreign or malformed page', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    expect(await getDocumentHistory(CID, DID, null, recorder({ status: 401 }).fetchImpl)).toEqual({ kind: 'unauthorized' })
    expect(handler).toHaveBeenCalledTimes(1)
    expect(await getDocumentHistory(CID, DID, null, recorder({ status: 403 }).fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(await getDocumentHistory(CID, DID, null, recorder({ status: 404 }).fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(await getDocumentHistory(CID, DID, null, recorder({ status: 500 }).fetchImpl)).toEqual({ kind: 'failed' })
    expect(await getDocumentHistory(CID, DID, null, recorder('network').fetchImpl)).toEqual({ kind: 'failed' })
    expect(await getDocumentHistory(CID, DID, null, recorder('abort').fetchImpl)).toEqual({ kind: 'failed' })
    expect(await getDocumentHistory(CID, DID, null, recorder({ status: 200, raw: 'nope' }).fetchImpl)).toEqual({ kind: 'failed' })
    const foreign = { ...HISTORY, document_id: 'doc_aDifferentDocument00001' }
    expect(await getDocumentHistory(CID, DID, null, recorder({ status: 200, body: foreign }).fetchImpl)).toEqual({ kind: 'failed' })
  })

  it('a malformed id makes no request', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: HISTORY })
    expect(await getDocumentHistory(CID, 'bad id', null, fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(calls).toHaveLength(0)
  })
})

describe('queryLibrary', () => {
  it('POSTs the query as a JSON body and never puts a search in the URL (X-7)', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: LIBRARY_PAGE })
    const query: LibraryQuery = { ...LIBRARY_QUERY, search: 'Ondrey the Wise' }
    const result = await queryLibrary(query, fetchImpl)
    expect(result.kind).toBe('ok')
    expect(calls).toHaveLength(1)
    expect(calls[0].url).toBe(`/campaigns/${CID}/library`)
    expect(calls[0].url).not.toMatch(/\?|Ondrey/)
    expect(calls[0]).toMatchObject({ method: 'POST', credentials: 'include' })
    expect(calls[0].headers['content-type']).toBe('application/json')
    expect(JSON.parse(calls[0].body ?? 'null')).toMatchObject({ campaign_id: CID, category: 'npcs', search: 'Ondrey the Wise' })
  })

  it('refuses a query the contract refuses, with no request', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: LIBRARY_PAGE })
    expect(await queryLibrary({ ...LIBRARY_QUERY, search: 'x' }, fetchImpl)).toEqual({ kind: 'failed' })
    expect(await queryLibrary({ ...LIBRARY_QUERY, category: 'cues' as never }, fetchImpl)).toEqual({ kind: 'failed' })
    expect(await queryLibrary({ ...LIBRARY_QUERY, campaign_id: 'bad id' }, fetchImpl)).toEqual({ kind: 'failed' })
    expect(calls).toHaveLength(0)
    expect((await queryLibrary(LIBRARY_QUERY, fetchImpl)).kind).toBe('ok')
    expect(calls).toHaveLength(1)
  })

  it('never lists cues as documents', () => {
    expect(LIBRARY_CATEGORIES).toContain('cues')
    const documentCategories = LIBRARY_CATEGORIES.filter((category) => category !== 'cues')
    expect(documentCategories).toEqual(['npcs', 'bestiary', 'documents', 'session-log'])
  })

  it('maps 401, 403, 404, 5xx, a network failure and a body that fails its schema', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    expect(await queryLibrary(LIBRARY_QUERY, recorder({ status: 401 }).fetchImpl)).toEqual({ kind: 'unauthorized' })
    expect(handler).toHaveBeenCalledTimes(1)
    expect(await queryLibrary(LIBRARY_QUERY, recorder({ status: 403 }).fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(await queryLibrary(LIBRARY_QUERY, recorder({ status: 404 }).fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(await queryLibrary(LIBRARY_QUERY, recorder({ status: 503 }).fetchImpl)).toEqual({ kind: 'failed' })
    expect(await queryLibrary(LIBRARY_QUERY, recorder('network').fetchImpl)).toEqual({ kind: 'failed' })
    expect(await queryLibrary(LIBRARY_QUERY, recorder('abort').fetchImpl)).toEqual({ kind: 'failed' })
    expect(await queryLibrary(LIBRARY_QUERY, recorder({ status: 200, body: { items: 'no' } }).fetchImpl)).toEqual({ kind: 'failed' })
  })

  it('keeps the echoed campaign and category on the page, so the caller can drop a stale one (LIB-25)', async () => {
    const other = { ...LIBRARY_PAGE, campaign_id: 'cmp_somebodyElse0000000001' }
    const result = await queryLibrary(LIBRARY_QUERY, recorder({ status: 200, body: other }).fetchImpl)
    expect(result.kind === 'ok' && result.page.campaign_id).toBe('cmp_somebodyElse0000000001')
  })
})

// ── 1kg.7.3: seal, one pinned version, and the character sheet's link ─────────

const PINNED = {
  schema_version: 1,
  document_id: DID,
  type: 'npc',
  type_version: 1,
  version: VERSION,
  data: { name: 'Ondrey', voice: 'Quiet' },
}
const LINK = { schema_version: 1, document_id: DID, participant_id: 'par_linkedSeat00000000001', seat_active: true }

describe('sealDocument', () => {
  it('POSTs to the seal route with an empty JSON body and reads the document it answers', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: NPC })
    const result = await sealDocument(CID, DID, fetchImpl)
    expect(result.kind).toBe('ok')
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({
      url: `/campaigns/${CID}/documents/${DID}/seal`,
      method: 'POST',
      credentials: 'include',
    })
    expect(calls[0].headers['content-type']).toBe('application/json')
    expect(JSON.parse(calls[0].body ?? 'null')).toEqual({})
  })

  it('reads every answer the way getDocument does', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    expect(await sealDocument(CID, DID, recorder({ status: 401, body: {} }).fetchImpl)).toEqual({ kind: 'unauthorized' })
    expect(handler).toHaveBeenCalledTimes(1)
    expect(await sealDocument(CID, DID, recorder({ status: 404, body: {} }).fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(await sealDocument(CID, DID, recorder({ status: 403, body: {} }).fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(await sealDocument(CID, DID, recorder({ status: 503, body: {} }).fetchImpl)).toEqual({ kind: 'failed' })
    expect(await sealDocument(CID, DID, recorder('network').fetchImpl)).toEqual({ kind: 'failed' })
    expect(await sealDocument(CID, DID, recorder({ status: 200, body: { ...NPC, schema_version: 2 } }).fetchImpl)).toEqual({
      kind: 'unsupported',
    })
    expect(
      await sealDocument(CID, DID, recorder({ status: 200, body: { ...NPC, document_id: 'doc_aDifferentDocument00001' } }).fetchImpl),
    ).toEqual({ kind: 'failed' })
  })

  it('a malformed id makes no request', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: NPC })
    expect(await sealDocument(CID, '../x', fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(calls).toHaveLength(0)
    expect((await sealDocument(CID, DID, fetchImpl)).kind).toBe('ok')
    expect(calls).toHaveLength(1)
  })
})

describe('getDocumentVersion', () => {
  it('GETs one version and reads its snapshot', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: PINNED })
    const result = await getDocumentVersion(CID, DID, 2, fetchImpl)
    expect(result.kind).toBe('ok')
    if (result.kind === 'ok') expect(result.snapshot.data.name).toBe('Ondrey')
    expect(calls[0]).toMatchObject({ url: `/campaigns/${CID}/documents/${DID}/versions/2`, method: 'GET', body: null, credentials: 'include' })
  })

  it('maps every answer, and a snapshot of another document is failed', async () => {
    const kind = async (answer: Answer) => (await getDocumentVersion(CID, DID, 2, recorder(answer).fetchImpl)).kind
    expect(await kind({ status: 404, body: {} })).toBe('unavailable')
    expect(await kind({ status: 403, body: {} })).toBe('unavailable')
    expect(await kind({ status: 503, body: {} })).toBe('failed')
    expect(await kind('network')).toBe('failed')
    expect(await kind({ status: 200, raw: '<html>' })).toBe('failed')
    expect(await kind({ status: 200, body: { ...PINNED, document_id: 'doc_aDifferentDocument00001' } })).toBe('failed')
    expect(await kind({ status: 200, body: { ...PINNED, type: 'dragon-hoard' } })).toBe('unsupported')
    expect(await kind({ status: 200, body: { ...PINNED, schema_version: 2 } })).toBe('unsupported')
    expect(await kind({ status: 200, body: { ...PINNED, type_version: 99 } })).toBe('unsupported')
  })

  it('a 401 signs out, and a bad id or version number makes no request', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    expect(await getDocumentVersion(CID, DID, 2, recorder({ status: 401, body: {} }).fetchImpl)).toEqual({ kind: 'unauthorized' })
    expect(handler).toHaveBeenCalledTimes(1)
    const { fetchImpl, calls } = recorder({ status: 200, body: PINNED })
    expect(await getDocumentVersion('bad id', DID, 2, fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(await getDocumentVersion(CID, DID, 0, fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(await getDocumentVersion(CID, DID, 1.5, fetchImpl)).toEqual({ kind: 'unavailable' })
    expect(calls).toHaveLength(0)
  })
})

describe('getCharacterSheetLink', () => {
  it('GETs the link and reads it, ids only', async () => {
    const { fetchImpl, calls } = recorder({ status: 200, body: LINK })
    expect(await getCharacterSheetLink(CID, DID, fetchImpl)).toEqual({ kind: 'ok', link: LINK })
    expect(calls[0]).toMatchObject({ url: `/campaigns/${CID}/documents/${DID}/link`, method: 'GET', credentials: 'include' })
  })

  it('maps every answer, and a link to another document is failed', async () => {
    const kind = async (answer: Answer) => (await getCharacterSheetLink(CID, DID, recorder(answer).fetchImpl)).kind
    expect(await kind({ status: 404, body: {} })).toBe('unavailable')
    expect(await kind({ status: 503, body: {} })).toBe('failed')
    expect(await kind('network')).toBe('failed')
    expect(await kind({ status: 200, body: { ...LINK, document_id: 'doc_aDifferentDocument00001' } })).toBe('failed')
    expect(await kind({ status: 200, body: { nope: true } })).toBe('failed')
    expect(await getCharacterSheetLink('bad id', DID, recorder({ status: 200, body: LINK }).fetchImpl)).toEqual({ kind: 'unavailable' })
  })
})

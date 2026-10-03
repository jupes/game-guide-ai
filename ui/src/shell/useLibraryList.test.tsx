/**
 * useLibraryList.test.tsx -- the Campaign Library's list state
 * (agent-forge-harness-1kg.6.4, L-2; LIB-20, LIB-22, LIB-23, LIB-24, LIB-25, X-7).
 *
 * The REAL campaign and canvas providers and a recording server. Timers are faked only
 * AFTER the mount (the harness waits on real ones), and only `setTimeout`/`clearTimeout`,
 * so promises and the providers behave as they do in the app. Each "no request"
 * assertion sits beside a positive control in the same test.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import {
  campaignFixture, flush, libraryBody, libraryRoute, live, mountSelected, run, type LibraryRow, type Route,
} from '../testing/workbenchHarness'
import { LIBRARY_PAGE_SIZE, SEARCH_DEBOUNCE_MS, useLibraryList, type LibraryListQuery } from './useLibraryList'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

const BASE: LibraryListQuery = { category: 'npcs', search: '', sort: 'recent', archived: false, type: null }

let list: ReturnType<typeof useLibraryList>
let setQuery: (next: Partial<LibraryListQuery>) => void

function Probe({ initial, fetchImpl }: { initial: LibraryListQuery; fetchImpl: typeof fetch }): null {
  const [query, update] = React.useState(initial)
  const result = useLibraryList(query, fetchImpl)
  React.useLayoutEffect(() => {
    list = result
    setQuery = (next) => update((previous) => ({ ...previous, ...next }))
  })
  return null
}

const mount = (rows: Readonly<Record<string, readonly LibraryRow[]>>, options: { route?: Route; strict?: boolean; initial?: LibraryListQuery } = {}) =>
  mountSelected((server) => <Probe initial={options.initial ?? BASE} fetchImpl={server.fetchImpl} />, {
    route: options.route ?? libraryRoute(rows), strict: options.strict,
  })

const ONDREY: LibraryRow = { id: 'doc_a', type: 'npc', title: 'Ondrey' }
const ROWS = { npcs: [ONDREY, { id: 'doc_b', type: 'npc', title: 'Brannoch' }] }

const many = (count: number): LibraryRow[] =>
  Array.from({ length: count }, (_, index) => ({ id: `doc_${index + 1}`, type: 'npc', title: `Npc ${index + 1}` }))

const items = (): string[] => (list.state.status === 'ready' ? list.state.items.map((item) => item.title) : [])

const ready = (): Promise<void> => waitFor(() => expect(list.state.status).toBe('ready'))

describe('what it asks for (LIB-20 to LIB-23, X-7)', () => {
  it('the first page is the exact body: 25, no search, newest first, active, no type, no cursor', async () => {
    const { server } = await mount(ROWS)
    await ready()
    expect(server.libraryCalls()).toHaveLength(1)
    expect(server.libraryCalls()[0].method).toBe('POST')
    expect(server.libraryCalls()[0].url).toBe('/campaigns/cmp_A/library')
    expect(server.libraryBodies()[0]).toEqual({
      schema_version: 1, campaign_id: 'cmp_A', category: 'npcs', search: '', sort: 'recent', archived: false, limit: LIBRARY_PAGE_SIZE,
    })
    expect(LIBRARY_PAGE_SIZE).toBe(25)
    expect(items()).toEqual(['Ondrey', 'Brannoch'])
  })

  it('sends exactly one request under StrictMode too', async () => {
    const { server } = await mount(ROWS, { strict: true })
    await ready()
    await flush()
    expect(server.libraryCalls()).toHaveLength(1)
  })

  it('a sort, a filter or a category applies at once, each as one request', async () => {
    const { server } = await mount(ROWS)
    await ready()
    await act(async () => setQuery({ sort: 'name' }))
    await ready()
    await act(async () => setQuery({ archived: true }))
    await ready()
    await act(async () => setQuery({ category: 'bestiary' }))
    await ready()
    expect(server.libraryBodies().map((body) => [body.sort, body.archived, body.category])).toEqual([
      ['recent', false, 'npcs'], ['name', false, 'npcs'], ['name', true, 'npcs'], ['name', true, 'bestiary'],
    ])
  })

  it('sends `type` only for Documents', async () => {
    const { server } = await mount({}, { initial: { ...BASE, category: 'documents', type: 'handout' } })
    await ready()
    expect(server.libraryBodies()[0]).toMatchObject({ category: 'documents', type: 'handout' })
    await act(async () => setQuery({ category: 'npcs' }))
    await ready()
    const last = server.libraryBodies().at(-1) as Record<string, unknown>
    expect(last.category).toBe('npcs')
    expect('type' in last).toBe(false)
  })

  it('never puts the search, the campaign or a cursor in the URL', async () => {
    vi.useRealTimers()
    const { server } = await mount({ npcs: many(30) })
    await ready()
    await act(async () => setQuery({ search: 'Npc 1' }))
    await waitFor(() => expect(server.libraryCalls()).toHaveLength(2))
    for (const call of server.libraryCalls()) expect(call.url).toBe('/campaigns/cmp_A/library')
  })
})

describe('the search (LIB-20)', () => {
  it('one character is not a search: no request; two is, after 250 ms', async () => {
    const { server } = await mount(ROWS)
    await ready()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => setQuery({ search: 'O' }))
    await act(async () => { vi.advanceTimersByTime(1000) })
    expect(server.libraryCalls()).toHaveLength(1)
    // One character is not an error either: the unfiltered list stays.
    expect(list.state.status).toBe('ready')
    // Positive control: the same recorder does see a two-character search, after the debounce.
    await act(async () => setQuery({ search: 'On' }))
    await act(async () => { vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 1) })
    expect(server.libraryCalls()).toHaveLength(1)
    await act(async () => { vi.advanceTimersByTime(1) })
    await flush()
    expect(server.libraryCalls()).toHaveLength(2)
    expect(server.libraryBodies()[1]).toMatchObject({ search: 'On' })
    expect(SEARCH_DEBOUNCE_MS).toBe(250)
  })

  it('typing quickly makes one request, for what was typed last', async () => {
    const { server } = await mount(ROWS)
    await ready()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    for (const text of ['On', 'Ond', 'Ondr']) {
      await act(async () => setQuery({ search: text }))
      await act(async () => { vi.advanceTimersByTime(100) })
    }
    expect(server.libraryCalls()).toHaveLength(1)
    await act(async () => { vi.advanceTimersByTime(250) })
    await flush()
    expect(server.libraryCalls()).toHaveLength(2)
    expect(server.libraryBodies()[1]).toMatchObject({ search: 'Ondr' })
  })

  it('trims what it sends, and a search of spaces is no search', async () => {
    const { server } = await mount(ROWS)
    await ready()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => setQuery({ search: '   Ond   ' }))
    await act(async () => { vi.advanceTimersByTime(250) })
    await flush()
    expect(server.libraryBodies().at(-1)).toMatchObject({ search: 'Ond' })
    const before = server.libraryCalls().length
    await act(async () => setQuery({ search: '      ' }))
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    // Clearing the search is one more request, for the unfiltered list.
    expect(server.libraryCalls()).toHaveLength(before + 1)
    expect(server.libraryBodies().at(-1)).toMatchObject({ search: '' })
  })

  it('clearing the field refetches the unfiltered list at once, without waiting for the debounce', async () => {
    const { server } = await mount(ROWS)
    await ready()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => setQuery({ search: 'Ond' }))
    await act(async () => { vi.advanceTimersByTime(250) })
    await flush()
    expect(server.libraryCalls()).toHaveLength(2)
    await act(async () => setQuery({ search: '' }))
    await flush()
    expect(server.libraryCalls()).toHaveLength(3)
    expect(server.libraryBodies()[2]).toMatchObject({ search: '' })
  })

  it('a search the contract refuses is `invalid` and makes no request', async () => {
    const { server } = await mount(ROWS)
    await ready()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => setQuery({ search: 'ab\u0000cd' }))
    await act(async () => { vi.advanceTimersByTime(1000) })
    await flush()
    expect(list.state).toEqual({ status: 'invalid' })
    expect(server.libraryCalls()).toHaveLength(1)
    // Positive control: a clean search does reach the wire.
    await act(async () => setQuery({ search: 'abcd' }))
    await act(async () => { vi.advanceTimersByTime(250) })
    await flush()
    expect(server.libraryCalls()).toHaveLength(2)
  })

  it('states the search an answer is for', async () => {
    await mount(ROWS)
    await ready()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await act(async () => setQuery({ search: 'Ond' }))
    await act(async () => { vi.advanceTimersByTime(250) })
    await flush()
    expect(list.state.status === 'ready' && list.state.search).toBe('Ond')
    expect(items()).toEqual(['Ondrey'])
  })
})

describe('whose answer it is (LIB-25)', () => {
  it('a page echoing another campaign is an error, never rows', async () => {
    await mount(ROWS, { route: libraryRoute(ROWS, { campaignOf: () => 'cmp_other' }) })
    await waitFor(() => expect(list.state.status).toBe('error'))
    expect(items()).toEqual([])
  })

  it('a page echoing another category is an error, never rows', async () => {
    const route: Route = (call) => {
      if (call.url.endsWith('/library')) return { status: 200, body: libraryBody('cmp_A', 'bestiary', [{ id: 'doc_t', type: 'statblock', title: 'Tidewarden' }]) }
      return libraryRoute(ROWS)(call)
    }
    await mount(ROWS, { route })
    await waitFor(() => expect(list.state.status).toBe('error'))
  })

  it('an answer that arrives after the key changed is dropped', async () => {
    const route: Route = (call) => {
      const body = JSON.parse(call.body ?? '{}') as { sort?: string }
      if (call.url.endsWith('/library') && body.sort === 'recent') return 'defer'
      return libraryRoute({ npcs: [{ id: 'doc_n', type: 'npc', title: 'Newer' }] })(call)
    }
    const { server } = await mount(ROWS, { route })
    expect(list.state).toEqual({ status: 'loading' })
    await act(async () => setQuery({ sort: 'name' }))
    await ready()
    expect(items()).toEqual(['Newer'])
    await act(async () => server.libraryCalls()[0].reply({ status: 200, body: libraryBody('cmp_A', 'npcs', [ONDREY]) }))
    await flush()
    expect(items()).toEqual(['Newer'])
  })

  it('a campaign switch shows loading with no old row, and the old answer never lands', async () => {
    const route: Route = (call) => {
      if (call.url === '/campaigns/cmp_B/library') return 'defer'
      return libraryRoute(ROWS)(call)
    }
    const { server } = await mount(ROWS, { route })
    await ready()
    expect(items()).toEqual(['Ondrey', 'Brannoch'])
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    expect(list.state).toEqual({ status: 'loading' })
    expect(items()).toEqual([])
    await waitFor(() => expect(server.libraryCalls().some((call) => call.url === '/campaigns/cmp_B/library')).toBe(true))
    const forB = server.libraryCalls().find((call) => call.url === '/campaigns/cmp_B/library')
    await act(async () => forB?.reply({ status: 200, body: libraryBody('cmp_B', 'npcs', [{ id: 'doc_x', type: 'npc', title: 'Xander' }]) }))
    await ready()
    expect(items()).toEqual(['Xander'])
  })

  it('switching A to B to A shows no old row (the scope key is new each time)', async () => {
    const { server } = await mount(ROWS)
    await ready()
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    await ready()
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_A')))
    await ready()
    expect(server.libraryCalls().map((call) => call.url)).toEqual([
      '/campaigns/cmp_A/library', '/campaigns/cmp_B/library', '/campaigns/cmp_A/library',
    ])
  })
})

describe('Load more (LIB-23, STATE-1)', () => {
  const dup: LibraryRow[] = [...many(25), { id: 'doc_25', type: 'npc', title: 'Npc 25 again' }, { id: 'doc_27', type: 'npc', title: 'Npc 27' }]

  it('sends the cursor, appends, and de-duplicates by document id', async () => {
    const { server } = await mount({ npcs: dup })
    await ready()
    expect(items()).toHaveLength(25)
    expect(list.state.status === 'ready' && list.state.nextCursor).toBe('c25')
    await act(async () => list.loadMore())
    await waitFor(() => expect(items()).toHaveLength(26))
    expect(server.libraryBodies()[1]).toMatchObject({ cursor: 'c25', limit: 25, category: 'npcs' })
    expect(items().at(-1)).toBe('Npc 27')
    expect(items()).not.toContain('Npc 25 again')
    expect(list.state.status === 'ready' && list.state.nextCursor).toBeNull()
  })

  it('is a no-op while one is running, and when there is nothing more', async () => {
    const route: Route = (call) => {
      const body = JSON.parse(call.body ?? '{}') as { cursor?: string }
      return call.url.endsWith('/library') && body.cursor !== undefined ? 'defer' : libraryRoute({ npcs: many(30) })(call)
    }
    const { server } = await mount({}, { route })
    await ready()
    await act(async () => list.loadMore())
    expect(list.state.status === 'ready' && list.state.more).toBe('loading')
    await act(async () => list.loadMore())
    expect(server.libraryCalls()).toHaveLength(2)
    await act(async () => server.libraryCalls()[1].reply({ status: 200, body: libraryBody('cmp_A', 'npcs', many(30).slice(25)) }))
    await waitFor(() => expect(items()).toHaveLength(30))
    await act(async () => list.loadMore())
    expect(server.libraryCalls()).toHaveLength(2)
  })

  it('a failure keeps every row, says so, and can be retried', async () => {
    let failing = true
    const route: Route = (call) => {
      const body = JSON.parse(call.body ?? '{}') as { cursor?: string }
      if (call.url.endsWith('/library') && body.cursor !== undefined && failing) return { status: 503, body: {} }
      return libraryRoute({ npcs: many(30) })(call)
    }
    await mount({}, { route })
    await ready()
    await act(async () => list.loadMore())
    await waitFor(() => expect(list.state.status === 'ready' && list.state.more).toBe('failed'))
    expect(items()).toHaveLength(25)
    failing = false
    await act(async () => list.loadMore())
    await waitFor(() => expect(items()).toHaveLength(30))
    expect(list.state.status === 'ready' && list.state.more).toBe('idle')
  })

  it('an answer for another campaign is a failure, not rows', async () => {
    const route: Route = (call) => {
      const body = JSON.parse(call.body ?? '{}') as { cursor?: string }
      if (call.url.endsWith('/library') && body.cursor !== undefined) return { status: 200, body: libraryBody('cmp_other', 'npcs', many(3)) }
      return libraryRoute({ npcs: many(30) })(call)
    }
    await mount({}, { route })
    await ready()
    await act(async () => list.loadMore())
    await waitFor(() => expect(list.state.status === 'ready' && list.state.more).toBe('failed'))
    expect(items()).toHaveLength(25)
  })
})

describe('Retry, remove and refresh (§12.2, LIB-24)', () => {
  it('Retry sends the same search, sort, filter and type again', async () => {
    const { server } = await mount({}, { route: libraryRoute({}, { failing: ['documents'] }), initial: { ...BASE, category: 'documents', type: 'handout', sort: 'name', archived: true } })
    await waitFor(() => expect(list.state.status).toBe('error'))
    await act(async () => list.retry())
    await waitFor(() => expect(server.libraryCalls()).toHaveLength(2))
    expect(server.libraryBodies()[1]).toEqual(server.libraryBodies()[0])
    expect(server.libraryBodies()[0]).toMatchObject({ sort: 'name', archived: true, type: 'handout' })
  })

  it('removeItem takes a row out in place, with no request', async () => {
    const { server } = await mount(ROWS)
    await ready()
    await act(async () => list.removeItem('doc_a'))
    expect(items()).toEqual(['Brannoch'])
    expect(server.libraryCalls()).toHaveLength(1)
  })

  it('a documentsVersion bump refetches the first page', async () => {
    const { server } = await mount(ROWS)
    await ready()
    await run(async () => live.actions.bumpDocumentsVersion())
    await ready()
    expect(server.libraryCalls()).toHaveLength(2)
    expect(server.libraryBodies()[1]).toEqual(server.libraryBodies()[0])
  })
})

/**
 * revealStoreDurability.test.tsx -- PR-2 of agent-forge-harness-1kg.7.3: the store's side of the
 * workspace indicator (titles, Stop all, the sheet's canvas origin) and of REVEAL-16 (the opaque
 * pending-stop marker, the unload guard, the 401 notice), plus the re-read when the GM comes back
 * to the tab. Real providers over the recording server, like `revealContext.test.tsx`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { notifyUnauthorized } from '../api'
import { BRANN, liveFixture } from '../gm/revealFixtures'
import {
  campaignFixture, defaultWorkbenchRoute, flush, live, mountSelected, mountWorkbench, revealPicture, run,
  type Call, type Reply, type Route,
} from '../testing/workbenchHarness'
import { revealSignOutNotice } from './revealSignOut'
import { pendingStopsKey, readPendingStops, writePendingStop } from './revealStopMarker'

const DOC = 'doc_a'
const OTHER_DOC = 'doc_b'
const ACCOUNT = 'ada@example.com'
const SESSION = 'ses_revealFixtureSession00001'

const isRevealGet = (call: Call): boolean => call.method === 'GET' && /\/reveals$/.test(call.url)
const isStopPost = (call: Call): boolean => call.method === 'POST' && /\/reveals\/stop$/.test(call.url)
const getsOf = (server: { calls: Call[] }): Call[] => server.calls.filter(isRevealGet)

const tableLive = (mask: string[], epoch: number) => revealPicture({ epoch, table: liveFixture(DOC, mask) })
const empty = (epoch: number) => revealPicture({ epoch })
const answer = (call: Call, body: unknown, status = 200): void => act(() => call.reply({ status, body }))

beforeEach(() => localStorage.clear())
afterEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('the titles the workspace indicator lists (REVEAL-14)', () => {
  const twoLive = revealPicture({
    table: liveFixture(DOC, ['name']),
    participants: { [BRANN.participant_id]: liveFixture(OTHER_DOC, ['name']) },
  })
  const titlesRoute = (picture: unknown): Route => (call) =>
    isRevealGet(call) ? { status: 200, body: picture } : defaultWorkbenchRoute(call)

  it('reads each live document once for its title, and no other', async () => {
    const { server } = await mountSelected(() => <></>, { route: titlesRoute(twoLive) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    expect(live.reveals.titles.get(DOC)).toBe('Ondrey')
    expect(live.reveals.titles.get(OTHER_DOC)).toBe('Brannoch')
    await run(() => live.reveals.refresh())
    expect(server.docCalls().map((call) => call.url).sort()).toEqual([
      `/campaigns/cmp_A/documents/${DOC}`,
      `/campaigns/cmp_A/documents/${OTHER_DOC}`,
    ])
  })

  it('reads nothing for a hidden document', async () => {
    const { server } = await mountSelected(() => <></>, { route: titlesRoute(empty(3)) })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    await flush()
    expect(server.docCalls()).toEqual([])
  })

  it('a title that cannot be read is absent, and a later picture asks again', async () => {
    const missing = revealPicture({ table: liveFixture('doc_missing', ['name']) })
    const { server } = await mountSelected(() => <></>, { route: titlesRoute(missing) })
    await waitFor(() => expect(server.docCalls()).toHaveLength(1))
    await flush()
    expect(live.reveals.titles.size).toBe(0)
    await run(() => live.reveals.refresh())
    await waitFor(() => expect(server.docCalls()).toHaveLength(2))
  })

  it('forgets the titles with the scope', async () => {
    // Only campaign A has anything live, so a titles map that survived the switch would show.
    const route: Route = (call) =>
      isRevealGet(call) && call.url.includes('cmp_A') ? { status: 200, body: twoLive } : defaultWorkbenchRoute(call)
    await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    expect(live.reveals.titles.size).toBe(0)
  })
})

describe('openSheet names the canvas document it was opened over (REVEAL-14)', () => {
  it('defaults to the document itself, and records another when the indicator opens a different one', async () => {
    await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    act(() => live.reveals.openSheet(DOC))
    expect(live.reveals.sheetFrom).toBe(DOC)
    act(() => live.reveals.openSheet(OTHER_DOC, null, DOC))
    expect(live.reveals.sheet).toEqual({ documentId: OTHER_DOC })
    expect(live.reveals.sheetFrom).toBe(DOC)
    act(() => live.reveals.openSheet(OTHER_DOC, null, null))
    expect(live.reveals.sheetFrom).toBeNull()
    act(() => live.reveals.closeSheet())
    expect(live.reveals.sheet).toBeNull()
    expect(live.reveals.sheetFrom).toBeNull()
  })
})

describe('Stop all', () => {
  it('sends scope all, with no document, and speaks the outcome', async () => {
    const route: Route = (call) => (isStopPost(call) ? 'defer' : defaultWorkbenchRoute(call))
    const { server } = await mountSelected(() => <></>, { route })
    act(() => live.reveals.stop('*', 'everything'))
    await flush()
    const stop = server.calls.find(isStopPost)
    expect(JSON.parse(stop?.body ?? '{}')).toMatchObject({ scope: 'all' })
    expect(JSON.parse(stop?.body ?? '{}')).not.toHaveProperty('document_id')
    expect(live.reveals.stopping.has('*')).toBe(true)
    if (stop !== undefined) answer(stop, empty(5))
    await waitFor(() => expect(live.reveals.announcement).toBe('Stopped showing everything'))
  })
})

describe('REVEAL-16: the opaque pending-stop marker', () => {
  const MARKER = { campaignId: 'cmp_A', documentId: DOC, sessionId: SESSION, epoch: 3, commandId: 'cmd_markerFromEarlierLoad01' }
  const liveRoute = (epoch: number, stop: Reply = { status: 200, body: empty(epoch + 1) }): Route => (call) => {
    if (isRevealGet(call)) return { status: 200, body: tableLive(['name'], epoch) }
    if (isStopPost(call)) return stop
    return defaultWorkbenchRoute(call)
  }

  it('a pressed Stop leaves a marker of ids and an epoch, with the command id the server saw, and an answer clears it', async () => {
    const route: Route = (call) =>
      isRevealGet(call) ? { status: 200, body: tableLive(['name'], 3) } : isStopPost(call) ? 'defer' : defaultWorkbenchRoute(call)
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await flush()
    const stop = server.calls.find(isStopPost)
    const [held] = readPendingStops(ACCOUNT)
    expect(held).toMatchObject({ campaignId: 'cmp_A', documentId: DOC, sessionId: SESSION, epoch: 3 })
    expect(held.commandId).toBe((JSON.parse(stop?.body ?? '{}') as { command_id: string }).command_id)
    expect(localStorage.getItem(pendingStopsKey(ACCOUNT))).not.toMatch(/Ondrey/)
    if (stop !== undefined) answer(stop, empty(4))
    await waitFor(() => expect(readPendingStops(ACCOUNT)).toEqual([]))
  })

  it.each([
    ['the document is gone (404)', { status: 404, body: {} }],
    ['the server refuses the request (422)', { status: 422, body: { detail: { code: 'validation', message: 'x', retryable: false } } }],
  ])('%s: the marker is cleared, nothing is left to replay', async (_name, stop) => {
    await mountSelected(() => <></>, { route: liveRoute(3, stop) })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await waitFor(() => expect(live.reveals.stopping.size).toBe(0))
    expect(readPendingStops(ACCOUNT)).toEqual([])
  })

  it('a 401 keeps the marker: Stop is replayed after the GM signs in again', async () => {
    await mountSelected(() => <></>, { route: liveRoute(3, { status: 401, body: {} }) })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await waitFor(() => expect(live.reveals.stopping.size).toBe(0))
    expect(readPendingStops(ACCOUNT)).toHaveLength(1)
  })

  it('replays a marker on load, with its own command id, when the session and epoch still match, and only once', async () => {
    writePendingStop(ACCOUNT, MARKER)
    const { server } = await mountSelected(() => <></>, { route: liveRoute(3) })
    await waitFor(() => expect(server.calls.filter(isStopPost)).toHaveLength(1))
    expect(JSON.parse(server.calls.find(isStopPost)?.body ?? '{}')).toMatchObject({
      command_id: MARKER.commandId, scope: 'document', document_id: DOC,
    })
    await waitFor(() => expect(readPendingStops(ACCOUNT)).toEqual([]))
    await run(() => live.reveals.refresh())
    expect(server.calls.filter(isStopPost)).toHaveLength(1)
  })

  it.each([
    ['the epoch moved on (a later, deliberate reveal)', 4, MARKER],
    ['another session began', 3, { ...MARKER, sessionId: 'ses_anotherSession0000000001' }],
  ])('drops a marker, and sends nothing, when %s', async (_name, epoch, marker) => {
    writePendingStop(ACCOUNT, marker)
    const { server } = await mountSelected(() => <></>, { route: liveRoute(epoch) })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    await flush()
    expect(server.calls.filter(isStopPost)).toEqual([])
    expect(readPendingStops(ACCOUNT)).toEqual([])
  })

  it('drops a marker when the session has ended (no picture)', async () => {
    writePendingStop(ACCOUNT, MARKER)
    const { server } = await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    await flush()
    expect(server.calls.filter(isStopPost)).toEqual([])
    expect(readPendingStops(ACCOUNT)).toEqual([])
  })

  it("leaves another campaign's marker alone until that campaign is opened", async () => {
    writePendingStop(ACCOUNT, { ...MARKER, campaignId: 'cmp_B' })
    const { server } = await mountSelected(() => <></>, { route: liveRoute(3) })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    await flush()
    expect(server.calls.filter(isStopPost)).toEqual([])
    expect(readPendingStops(ACCOUNT)).toHaveLength(1)
  })

  it('an unreadable picture does not replay: it waits for one it can compare with', async () => {
    writePendingStop(ACCOUNT, MARKER)
    const route: Route = (call) => (isRevealGet(call) ? { status: 503, body: {} } : defaultWorkbenchRoute(call))
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('unknown'))
    await flush()
    expect(server.calls.filter(isStopPost)).toEqual([])
    expect(readPendingStops(ACCOUNT)).toHaveLength(1)
  })
})

describe('REVEAL-16: an unacknowledged Stop blocks unload', () => {
  it('prevents unload while a Stop is waiting on the server, and not before or after', async () => {
    const route: Route = (call) => (isStopPost(call) ? 'defer' : defaultWorkbenchRoute(call))
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    const unload = (): Event => {
      const event = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(event)
      return event
    }
    expect(unload().defaultPrevented).toBe(false)
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await flush()
    expect(unload().defaultPrevented).toBe(true)
    const stop = server.calls.find(isStopPost)
    if (stop !== undefined) answer(stop, empty(5))
    await waitFor(() => expect(live.reveals.stopping.size).toBe(0))
    expect(unload().defaultPrevented).toBe(false)
  })
})

describe('the picture is read again when the GM comes back to the tab', () => {
  it('re-reads on focus and when the tab becomes visible, and not while it is hidden', async () => {
    let now = Date.now()
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    const { server } = await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    const before = getsOf(server).length
    now += 5_000
    await run(async () => {
      window.dispatchEvent(new Event('focus'))
    })
    await waitFor(() => expect(getsOf(server)).toHaveLength(before + 1))
    now += 5_000
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    await run(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(getsOf(server)).toHaveLength(before + 1)
    visibility.mockReturnValue('visible')
    await run(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(getsOf(server)).toHaveLength(before + 2))
  })

  it('a focus and a visibility change together are one read, not two', async () => {
    let now = Date.now()
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    const { server } = await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    const before = getsOf(server).length
    now += 5_000
    await run(async () => {
      window.dispatchEvent(new Event('focus'))
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await flush()
    expect(getsOf(server)).toHaveLength(before + 1)
  })

  it('asks nothing when there is no GM scope', async () => {
    const { server } = await mountWorkbench(() => <></>, { role: 'player', hash: '#campaign=cmp_A' })
    await flush()
    await run(async () => {
      window.dispatchEvent(new Event('focus'))
    })
    expect(server.calls.filter((call) => /\/reveals/.test(call.url))).toEqual([])
  })
})

describe('REVEAL-16: a 401 during a live reveal (the Login screen says what the table still sees)', () => {
  beforeEach(() => revealSignOutNotice.clear())
  afterEach(() => revealSignOutNotice.clear())

  it('records the titles that are live when a request is refused as signed out', async () => {
    const picture = revealPicture({ table: liveFixture(DOC, ['name']) })
    const route: Route = (call) => (isRevealGet(call) ? { status: 200, body: picture } : defaultWorkbenchRoute(call))
    await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.titles.get(DOC)).toBe('Ondrey'))
    act(() => notifyUnauthorized())
    expect(revealSignOutNotice.text).toBe('The table can still see Ondrey')
  })

  it('records nothing when nothing is live', async () => {
    await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    act(() => notifyUnauthorized())
    expect(revealSignOutNotice.text).toBeNull()
  })
})

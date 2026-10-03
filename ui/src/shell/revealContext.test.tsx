/**
 * revealContext.test.tsx -- the reveal store (agent-forge-harness-1kg.7.3, brief 3.2
 * and tests 14 to 18; the Critic's items 3, 4, 9 and 16). Real providers over the
 * recording server, with answers held back so each race really interleaves.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { ANA, BRANN, liveFixture } from '../gm/revealFixtures'
import {
  campaignFixture, defaultWorkbenchRoute, flush, live, mountSelected, mountWorkbench, revealPicture, run, seatBody,
  tableSessionBody, type Call, type Route,
} from '../testing/workbenchHarness'
import { revealAnnouncementText } from './revealContext'

const DOC = 'doc_a'
const OTHER_DOC = 'doc_b'
const COMMAND = 'cmd_revealContextCommand01'
const REQUEST = {
  document_id: DOC,
  session_id: 'ses_revealFixtureSession00001',
  reveal_epoch: 3,
  version: 2,
  mask: ['name', 'voice'],
  audience: { kind: 'table' as const },
}

const isRevealGet = (call: Call): boolean => call.method === 'GET' && /\/reveals$/.test(call.url)
const isRevealPost = (call: Call): boolean => call.method === 'POST' && /\/reveals$/.test(call.url)
const isStopPost = (call: Call): boolean => call.method === 'POST' && /\/reveals\/stop$/.test(call.url)

/** Holds back the reveal GET, the Confirm and the Stop, so a test answers each by hand. */
const deferringReveals: Route = (call) =>
  isRevealGet(call) || isRevealPost(call) || isStopPost(call) ? 'defer' : defaultWorkbenchRoute(call)

const tableLive = (mask: string[], epoch: number, session?: string) =>
  revealPicture({ epoch, ...(session === undefined ? {} : { sessionId: session }), table: liveFixture(DOC, mask) })
const empty = (epoch: number, session?: string) => revealPicture({ epoch, ...(session === undefined ? {} : { sessionId: session }) })

function answer(call: Call, body: unknown, status = 200): void {
  act(() => call.reply({ status, body }))
}

const getsOf = (server: { calls: Call[] }): Call[] => server.calls.filter(isRevealGet)

/**
 * Mounts with the FIRST picture read held back, then fakes the timers: RTL's `waitFor` hangs under fake
 * timers, so the mount happens on real ones, and every timer the store sets afterwards is the fake one.
 */
async function mountThenFake(route: Route) {
  let held = false
  const wrapped: Route = (call) => {
    if (isRevealGet(call) && !held) {
      held = true
      return 'defer'
    }
    return route(call)
  }
  const mounted = await mountSelected(() => <></>, { route: wrapped })
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const initial = mounted.server.calls.find(isRevealGet)
  if (initial === undefined) throw new Error('the first read was not sent')
  return { ...mounted, initial }
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('the scope gate (test 14)', () => {
  it('a player account makes no reveal request, and a dm with a campaign makes one', async () => {
    const player = await mountWorkbench(() => <></>, { role: 'player', hash: '#campaign=cmp_A' })
    await flush()
    expect(player.server.calls.filter((call) => /\/reveals/.test(call.url))).toEqual([])
    expect(live.reveals.status).toBe('idle')
    player.view.unmount()

    const gm = await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    expect(getsOf(gm.server)).toHaveLength(1)
    expect(gm.server.calls.find(isRevealGet)?.url).toBe('/campaigns/cmp_A/reveals')
  })

  it('no campaign selected makes no reveal request', async () => {
    const { server } = await mountWorkbench(() => <></>)
    await flush()
    expect(server.calls.filter((call) => /\/reveals/.test(call.url))).toEqual([])
    expect(live.reveals.status).toBe('idle')
  })
})

describe('status', () => {
  it('is loading, then none when the server says no live session', async () => {
    await mountSelected(() => <></>, { route: deferringReveals })
    expect(live.reveals.status).toBe('loading')
    await flush()
  })

  it('is live with the picture the server sent', async () => {
    const picture = tableLive(['name', 'voice'], 3)
    await mountSelected(() => <></>, { route: (call) => (isRevealGet(call) ? { status: 200, body: picture } : defaultWorkbenchRoute(call)) })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    expect(live.reveals.state?.reveal_epoch).toBe(3)
  })
})

describe('REVEAL-13: fail closed (test 16)', () => {
  it('a picture that does not parse is unknown, never none', async () => {
    const route: Route = (call) => (isRevealGet(call) ? { status: 200, body: { schema_version: 1, state: { session_id: 'x' } } } : defaultWorkbenchRoute(call))
    await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('unknown'))
    expect(live.reveals.status).not.toBe('none')
  })

  it('a refresh that fails after a good picture reads unknown, and a later good one heals it', async () => {
    let answers: Array<'ok' | 'fail'> = ['ok', 'fail', 'ok']
    const route: Route = (call) => {
      if (!isRevealGet(call)) return defaultWorkbenchRoute(call)
      return answers.shift() === 'fail' ? { status: 503, body: {} } : { status: 200, body: tableLive(['name'], 3) }
    }
    await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    await run(() => live.reveals.refresh())
    expect(live.reveals.status).toBe('unknown')
    answers = ['ok']
    await run(() => live.reveals.refresh())
    expect(live.reveals.status).toBe('live')
  })
})

describe('the re-read backoff (Critic 16, I-11)', () => {
  it('re-reads an outage after 2, 4, 8, 16 then every 30 seconds, and stops on success', async () => {
    let healthy = false
    const route: Route = (call) => {
      if (!isRevealGet(call)) return defaultWorkbenchRoute(call)
      return healthy ? { status: 200, body: revealPicture(null) } : { status: 503, body: {} }
    }
    const { server, initial } = await mountThenFake(route)
    answer(initial, {}, 503)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(getsOf(server)).toHaveLength(1)
    let expected = 1
    for (const wait of [2_000, 4_000, 8_000, 16_000, 30_000, 30_000]) {
      await act(async () => { await vi.advanceTimersByTimeAsync(wait - 1) })
      expect(getsOf(server)).toHaveLength(expected)
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expected += 1
      expect(getsOf(server)).toHaveLength(expected)
    }
    healthy = true
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(live.reveals.status).toBe('none')
    const settled = getsOf(server).length
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
    expect(getsOf(server)).toHaveLength(settled)
  })

  it('a 403 or 404 reads unknown but does not poll', async () => {
    const route: Route = (call) => (isRevealGet(call) ? { status: 404, body: {} } : defaultWorkbenchRoute(call))
    const { server, initial } = await mountThenFake(route)
    answer(initial, {}, 404)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(live.reveals.status).toBe('unknown')
    await act(async () => { await vi.advanceTimersByTimeAsync(300_000) })
    expect(getsOf(server)).toHaveLength(1)
  })

  it('the loop ends with the scope', async () => {
    const route: Route = (call) => (isRevealGet(call) ? { status: 503, body: {} } : defaultWorkbenchRoute(call))
    const { server, initial } = await mountThenFake(route)
    answer(initial, {}, 503)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const forA = () => server.calls.filter((call) => isRevealGet(call) && call.url.includes('cmp_A')).length
    const before = forA()
    await act(async () => { await vi.advanceTimersByTimeAsync(300_000) })
    expect(forA()).toBe(before)
  })
})

describe('ordering (test 15 and the Critic 4)', () => {
  it('a GET sent before a Stop answer was applied is dropped', async () => {
    const { server } = await mountSelected(() => <></>, { route: deferringReveals })
    const [first] = getsOf(server)
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await flush()
    const stop = server.calls.find(isStopPost)
    expect(stop).toBeDefined()
    if (stop === undefined) return
    answer(stop, empty(6))
    await waitFor(() => expect(live.reveals.state?.reveal_epoch).toBe(6))
    // The older read finally answers with a picture where the document is still live.
    answer(first, tableLive(['name'], 5))
    await flush()
    // Mutation: removing the epoch or seq guard lets it overwrite.
    expect(live.reveals.state?.reveal_epoch).toBe(6)
    expect(live.reveals.state?.slots.every((slot) => slot.live === null)).toBe(true)
  })

  it('a Confirm success with a lower epoch than the held Stop picture is dropped', async () => {
    const { server } = await mountSelected(() => <></>, { route: deferringReveals })
    answer(getsOf(server)[0], tableLive(['name', 'voice'], 4))
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    let confirmed: unknown = null
    act(() => { void live.reveals.confirm(REQUEST, COMMAND).then((outcome) => { confirmed = outcome }) })
    await flush()
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await flush()
    const stop = server.calls.find(isStopPost)
    const confirm = server.calls.find(isRevealPost)
    if (stop === undefined || confirm === undefined) throw new Error('requests were not sent')
    answer(stop, empty(6))
    await waitFor(() => expect(live.reveals.state?.reveal_epoch).toBe(6))
    answer(confirm, tableLive(['name', 'voice'], 5))
    await waitFor(() => expect(confirmed).not.toBeNull())
    expect(live.reveals.state?.reveal_epoch).toBe(6)
    // The late Confirm announces nothing: a Stop was pressed for it since.
    expect(confirmed).toMatchObject({ kind: 'ok', result: 'stopped' })
  })

  it('across sessions the later-SENT read wins: a late answer from an ended session never overwrites the new one', async () => {
    const { server } = await mountSelected(() => <></>, { route: deferringReveals })
    const [oldRead] = getsOf(server)
    act(() => { void live.reveals.refresh() })
    await flush()
    const newRead = getsOf(server)[1]
    answer(newRead, tableLive(['name'], 1, 'ses_newSession0000000000001'))
    await waitFor(() => expect(live.reveals.state?.session_id).toBe('ses_newSession0000000000001'))
    answer(oldRead, tableLive(['name'], 9, 'ses_oldSession0000000000001'))
    await flush()
    // Mutation: "different session replaces" would show the old one.
    expect(live.reveals.state?.session_id).toBe('ses_newSession0000000000001')
  })

  it('within a session the higher epoch wins whatever the order, and a tie goes to the later-sent', async () => {
    const { server } = await mountSelected(() => <></>, { route: deferringReveals })
    const [first] = getsOf(server)
    act(() => { void live.reveals.refresh() })
    await flush()
    const second = getsOf(server)[1]
    answer(first, tableLive(['name'], 4))
    await waitFor(() => expect(live.reveals.state?.reveal_epoch).toBe(4))
    answer(second, tableLive(['name', 'voice'], 4))
    await waitFor(() => expect(live.reveals.state?.slots[0].live?.mask).toEqual(['name', 'voice']))
  })

  it('an answer of no session applies when it is the later-sent one, and not when it is older', async () => {
    const { server } = await mountSelected(() => <></>, { route: deferringReveals })
    const [first] = getsOf(server)
    act(() => { void live.reveals.refresh() })
    await flush()
    const second = getsOf(server)[1]
    answer(second, revealPicture(null))
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    answer(first, tableLive(['name'], 2))
    await flush()
    expect(live.reveals.status).toBe('none')
  })
})

describe('confirm (the command id, REVEAL-22 and the Critic 9)', () => {
  it('sends the request with the id it was given, once, and reports a held picture', async () => {
    const route: Route = (call) => {
      if (isRevealPost(call)) return { status: 200, body: tableLive(['name', 'voice'], 4) }
      return defaultWorkbenchRoute(call)
    }
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    const outcome = await run(() => live.reveals.confirm(REQUEST, COMMAND))
    expect(outcome).toMatchObject({ kind: 'ok', result: 'held' })
    const posts = server.calls.filter(isRevealPost)
    expect(posts).toHaveLength(1)
    expect(JSON.parse(posts[0].body ?? '{}')).toMatchObject({ command_id: COMMAND, ...REQUEST })
    expect(live.reveals.status).toBe('live')
  })

  it('a 200 whose picture does not hold what was asked is changed, not success (a replay after a Stop)', async () => {
    const route: Route = (call) => (isRevealPost(call) ? { status: 200, body: empty(4) } : defaultWorkbenchRoute(call))
    await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    // Mutation: announcing on `ok` reports `held` here.
    expect(await run(() => live.reveals.confirm(REQUEST, COMMAND))).toMatchObject({ kind: 'ok', result: 'changed' })
    expect(await run(() => live.reveals.confirm({ ...REQUEST, mask: ['name'] }, COMMAND))).toMatchObject({ kind: 'ok', result: 'changed' })
  })

  it('a picture holding another mask or audience is changed', async () => {
    const route: Route = (call) => (isRevealPost(call) ? { status: 200, body: tableLive(['name'], 4) } : defaultWorkbenchRoute(call))
    await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    expect(await run(() => live.reveals.confirm(REQUEST, COMMAND))).toMatchObject({ result: 'changed' })
    expect(await run(() => live.reveals.confirm({ ...REQUEST, mask: ['name'] }, COMMAND))).toMatchObject({ result: 'held' })
    const toPlayers = { ...REQUEST, mask: ['name'], audience: { kind: 'participants' as const, participant_ids: ['par_one00000000000001'] } }
    expect(await run(() => live.reveals.confirm(toPlayers, COMMAND))).toMatchObject({ result: 'changed' })
  })

  it('a 409 reads the picture again and is never retried', async () => {
    let reads = 0
    const route: Route = (call) => {
      if (isRevealPost(call)) return { status: 409, body: { detail: { code: 'conflict', message: 'x', retryable: false } } }
      if (isRevealGet(call)) { reads += 1; return { status: 200, body: revealPicture(null) } }
      return defaultWorkbenchRoute(call)
    }
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    const before = reads
    expect(await run(() => live.reveals.confirm(REQUEST, COMMAND))).toEqual({ kind: 'conflict' })
    await waitFor(() => expect(reads).toBe(before + 1))
    expect(server.calls.filter(isRevealPost)).toHaveLength(1)
  })

  it.each([
    ['mask', { kind: 'mask_refused', keys: ['wants'] }],
    ['audience', { kind: 'refused', field: 'audience' }],
    ['version', { kind: 'refused', field: 'version' }],
  ])('a 422 on %s reads the picture again too', async (field, expected) => {
    let reads = 0
    const detail = { code: 'validation', message: 'x', retryable: false, field, ...(field === 'mask' ? { keys: ['wants'] } : {}) }
    const route: Route = (call) => {
      if (isRevealPost(call)) return { status: 422, body: { detail } }
      if (isRevealGet(call)) { reads += 1; return { status: 200, body: revealPicture(null) } }
      return defaultWorkbenchRoute(call)
    }
    await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    const before = reads
    expect(await run(() => live.reveals.confirm(REQUEST, COMMAND))).toEqual(expected)
    await waitFor(() => expect(reads).toBe(before + 1))
  })

  it('a failure and a 429 are reported, not retried, and read nothing again', async () => {
    let status = 503
    const route: Route = (call) => (isRevealPost(call) ? { status, body: {} } : defaultWorkbenchRoute(call))
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    expect(await run(() => live.reveals.confirm(REQUEST, COMMAND))).toEqual({ kind: 'failed' })
    status = 429
    expect(await run(() => live.reveals.confirm(REQUEST, COMMAND))).toEqual({ kind: 'throttled', retryAfterS: null })
    expect(server.calls.filter(isRevealPost)).toHaveLength(2)
  })
})

describe('Stop (X-3, REVEAL-22, Critic 3)', () => {
  it('sends at once, with no epoch, and marks the document as stopping until acknowledged', async () => {
    const { server } = await mountSelected(() => <></>, { route: deferringReveals })
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await flush()
    const stop = server.calls.find(isStopPost)
    expect(stop).toBeDefined()
    if (stop === undefined) return
    expect(JSON.parse(stop.body ?? '{}')).toMatchObject({ scope: 'document', document_id: DOC })
    expect(JSON.parse(stop.body ?? '{}')).not.toHaveProperty('reveal_epoch')
    expect(live.reveals.stopping.has(DOC)).toBe(true)
    answer(stop, empty(5))
    await waitFor(() => expect(live.reveals.stopping.size).toBe(0))
    expect(live.reveals.announcement).toBe('Stopped showing Ondrey')
  })

  it('REVEAL-22: confirm sends nothing while a Stop is unacknowledged', async () => {
    const { server } = await mountSelected(() => <></>, { route: deferringReveals })
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await flush()
    // Mutation: removing the gate would send a POST /reveals here.
    expect(await run(() => live.reveals.confirm(REQUEST, COMMAND))).toEqual({ kind: 'blocked' })
    expect(server.calls.filter(isRevealPost)).toHaveLength(0)
  })

  it('a Stop that fails is retried, speaks once, and keeps stopFailed until it lands', async () => {
    let healthy = false
    const route: Route = (call) => {
      if (isStopPost(call)) return healthy ? { status: 200, body: empty(5) } : { status: 503, body: {} }
      return defaultWorkbenchRoute(call)
    }
    const { server, initial } = await mountThenFake(route)
    answer(initial, empty(3))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(live.reveals.stopFailed).toBe(true)
    expect(live.reveals.announcement).toBe("Couldn't stop showing — retrying. The table may still see it.")
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(server.calls.filter(isStopPost)).toHaveLength(2)
    healthy = true
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(live.reveals.stopFailed).toBe(false)
    expect(live.reveals.stopping.size).toBe(0)
    expect(live.reveals.announcement).toBe('Stopped showing Ondrey')
  })

  it('a Stop the server refuses outright is not a silent success: stopFailed stays, and it is spoken', async () => {
    const route: Route = (call) =>
      isStopPost(call) ? { status: 422, body: { detail: { code: 'validation', message: 'x', retryable: false } } } : defaultWorkbenchRoute(call)
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await waitFor(() => expect(live.reveals.announcement).toBe("Couldn't stop showing Ondrey. The table may still see it."))
    expect(live.reveals.stopFailed).toBe(true)
    expect(live.reveals.stopping.size).toBe(0)
    // A new press starts a new job and clears the refusal for the document.
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await waitFor(() => expect(server.calls.filter(isStopPost)).toHaveLength(2))
  })

  it('survives a scope change: a Stop still retrying for campaign A is not forgotten when B is selected', async () => {
    const route: Route = (call) => (isStopPost(call) ? { status: 503, body: {} } : defaultWorkbenchRoute(call))
    const { server, initial } = await mountThenFake(route)
    answer(initial, empty(3))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    act(() => live.reveals.stop(DOC, 'Ondrey'))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    expect(live.reveals.stopping.size).toBe(0)
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    const stops = server.calls.filter(isStopPost)
    expect(stops.length).toBeGreaterThanOrEqual(2)
    expect(stops.every((call) => call.url === '/campaigns/cmp_A/reveals/stop')).toBe(true)
  })
})

describe('scope (test 18)', () => {
  it('a switch drops the picture, the sheet and the announcement in the same render, and an in-flight answer', async () => {
    const { server } = await mountSelected(() => <></>, { route: deferringReveals })
    answer(getsOf(server)[0], tableLive(['name'], 3))
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    act(() => live.reveals.openSheet(DOC))
    await flush()
    expect(live.reveals.sheet).toEqual({ documentId: DOC })
    // A read for campaign A is in flight when the GM switches.
    const inFlight = server.calls.filter((call) => isRevealGet(call) && !call.url.includes('cmp_B')).at(-1)
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    expect(live.reveals.sheet).toBeNull()
    expect(live.reveals.state).toBeNull()
    expect(live.reveals.announcement).toBe('')
    if (inFlight !== undefined) answer(inFlight, tableLive(['name'], 9))
    await flush()
    expect(live.reveals.state).toBeNull()
    expect(live.reveals.status).toBe('loading')
  })

  it('openSheet and closeSheet move the sheet, and opening reads the picture again', async () => {
    const { server } = await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    const before = getsOf(server).length
    act(() => live.reveals.openSheet(DOC))
    await waitFor(() => expect(getsOf(server)).toHaveLength(before + 1))
    expect(live.reveals.sheet).toEqual({ documentId: DOC })
    act(() => live.reveals.closeSheet())
    expect(live.reveals.sheet).toBeNull()
    void OTHER_DOC
  })
})

describe('the table session (brief 3)', () => {
  it('reads again when the session id or epoch moves, and not when the held picture already matches', async () => {
    let epoch = 3
    const route: Route = (call) => {
      if (call.method === 'GET' && /\/table-session$/.test(call.url)) return { status: 200, body: tableSessionBody({ reveal_epoch: epoch }) }
      if (isRevealGet(call)) return { status: 200, body: empty(epoch) }
      return defaultWorkbenchRoute(call)
    }
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.table.liveSession).not.toBeNull())
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    await flush()
    // The picture already matches the session's epoch: no second read for it.
    expect(live.reveals.state?.reveal_epoch).toBe(3)
    expect(getsOf(server)).toHaveLength(1)
    epoch = 4
    await run(() => live.table.retry())
    expect(live.reveals.state?.reveal_epoch).toBe(3)
  })

  it('reads again when the session started after the first read answered no session', async () => {
    const route: Route = (call) => {
      if (call.method === 'GET' && /\/table-session$/.test(call.url)) return { status: 200, body: tableSessionBody({}) }
      return defaultWorkbenchRoute(call)
    }
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(live.table.liveSession).not.toBeNull())
    await waitFor(() => expect(getsOf(server).length).toBeGreaterThanOrEqual(2))
  })
})

describe('the seats the header names (1kg.7.3)', () => {
  const participantsRoute = (picture: unknown, items: unknown[] = []): Route => (call) => {
    if (isRevealGet(call)) return { status: 200, body: picture }
    if (call.method === 'GET' && /participants/.test(call.url)) return { status: 200, body: seatBody(items as never[]) }
    return defaultWorkbenchRoute(call)
  }
  const forBrann = revealPicture({ participants: { [BRANN.participant_id]: liveFixture(DOC, ['name']) } })

  it('reads the seats once when a document is live to a participant, so the header can name them', async () => {
    const { server } = await mountSelected(() => <></>, { route: participantsRoute(forBrann, [BRANN]) })
    await waitFor(() => expect(live.reveals.seats).not.toBeNull())
    expect(live.reveals.seats?.map((seat) => seat.alias)).toEqual(['Brann'])
    await run(() => live.reveals.refresh())
    expect(server.calls.filter((call) => /participants/.test(call.url))).toHaveLength(1)
  })

  it('asks for no seats while only the table is shown to', async () => {
    const { server } = await mountSelected(() => <></>, { route: participantsRoute(tableLive(['name'], 3)) })
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    await flush()
    expect(server.calls.filter((call) => /participants/.test(call.url))).toEqual([])
    expect(live.reveals.seats).toBeNull()
  })

  it('a failed seat read leaves the names unknown and is not retried by itself', async () => {
    const route: Route = (call) => {
      if (isRevealGet(call)) return { status: 200, body: forBrann }
      if (/participants/.test(call.url)) return { status: 503, body: {} }
      return defaultWorkbenchRoute(call)
    }
    const { server } = await mountSelected(() => <></>, { route })
    await waitFor(() => expect(server.calls.some((call) => /participants/.test(call.url))).toBe(true))
    await run(() => live.reveals.refresh())
    expect(live.reveals.seats).toBeNull()
    expect(server.calls.filter((call) => /participants/.test(call.url))).toHaveLength(1)
  })

  it('the sheet publishes the seats it read, and a scope change forgets them', async () => {
    await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    act(() => live.reveals.noteSeats([BRANN, ANA]))
    expect(live.reveals.seats).toHaveLength(2)
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    expect(live.reveals.seats).toBeNull()
  })
})

describe('the opener the sheet returns focus to', () => {
  it('records what the caller passes, and forgets it when the sheet closes', async () => {
    await mountSelected(() => <></>)
    await waitFor(() => expect(live.reveals.status).toBe('none'))
    const button = document.createElement('button')
    act(() => live.reveals.openSheet(DOC, button))
    expect(live.reveals.openerRef.current).toBe(button)
    act(() => live.reveals.closeSheet())
    expect(live.reveals.sheet).toBeNull()
  })
})

describe('announcements', () => {
  it('alternate a trailing space so an identical message is read again', () => {
    expect(revealAnnouncementText({ announcement: '', announcementTick: 3 })).toBe('')
    const one = revealAnnouncementText({ announcement: 'Shown to the table', announcementTick: 1 })
    const two = revealAnnouncementText({ announcement: 'Shown to the table', announcementTick: 2 })
    expect(one).not.toBe(two)
    expect(one.trim()).toBe(two.trim())
  })
})

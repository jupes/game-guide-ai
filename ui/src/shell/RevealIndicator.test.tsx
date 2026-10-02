/**
 * RevealIndicator.test.tsx -- the workspace indicator (agent-forge-harness-1kg.7.3 PR-2, REVEAL-14,
 * REVEAL-13, REVEAL-16, REVEAL-8). It reads the same store the canvas does, so the two agree by
 * construction. Real providers over the recording server.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BRANN, liveFixture } from '../gm/revealFixtures'
import {
  defaultWorkbenchRoute, flush, live, mountSelected, mountWorkbench, revealPicture, run, seatBody,
  type Call, type Route,
} from '../testing/workbenchHarness'
import { RevealIndicator } from './RevealIndicator'

const DOC = 'doc_a'
const OTHER = 'doc_b'

const isRevealGet = (call: Call): boolean => call.method === 'GET' && /\/reveals$/.test(call.url)
const isStopPost = (call: Call): boolean => call.method === 'POST' && /\/reveals\/stop$/.test(call.url)
const stops = (server: { calls: Call[] }): Array<Record<string, unknown>> =>
  server.calls.filter(isStopPost).map((call) => JSON.parse(call.body ?? '{}') as Record<string, unknown>)

const one = revealPicture({ epoch: 3, table: liveFixture(DOC, ['name', 'voice']) })
const two = revealPicture({
  epoch: 3,
  table: liveFixture(DOC, ['name', 'voice']),
  participants: { [BRANN.participant_id]: liveFixture(OTHER, ['name']) },
})

const withPicture = (picture: unknown, stop: { status: number; body?: unknown } = { status: 200, body: revealPicture({ epoch: 4 }) }): Route =>
  (call) => {
    if (isRevealGet(call)) return { status: 200, body: picture }
    if (isStopPost(call)) return stop
    if (call.method === 'GET' && /\/participants/.test(call.url)) return { status: 200, body: seatBody([BRANN]) }
    return defaultWorkbenchRoute(call)
  }

const indicator = (): HTMLElement => screen.getByRole('group', { name: 'What the table sees' })

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('when it is shown', () => {
  it.each([
    ['nothing is live', revealPicture({ epoch: 3 })],
    ['there is no live session', revealPicture(null)],
  ])('renders nothing when %s, and renders once something is', async (_name, picture) => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(picture) })
    await waitFor(() => expect(['live', 'none']).toContain(live.reveals.status))
    expect(screen.queryByRole('group', { name: 'What the table sees' })).toBeNull()
  })

  it('renders nothing for a player account, which makes no reveal request', async () => {
    const { server } = await mountWorkbench(() => <RevealIndicator />, { role: 'player', hash: '#campaign=cmp_A' })
    await flush()
    expect(screen.queryByRole('group', { name: 'What the table sees' })).toBeNull()
    expect(server.calls.filter((call) => /\/reveals/.test(call.url))).toEqual([])
  })

  it('renders nothing while the first picture loads: it never reads as nothing revealed or as unknown', async () => {
    const route: Route = (call) => (isRevealGet(call) ? 'defer' : defaultWorkbenchRoute(call))
    await mountSelected(() => <RevealIndicator />, { route })
    await flush()
    expect(live.reveals.status).toBe('loading')
    expect(screen.queryByRole('group')).toBeNull()
  })

  it('REVEAL-13: an unreadable picture reads unknown, with Stop all and no list, never "nothing revealed"', async () => {
    const route: Route = (call) => (isRevealGet(call) ? { status: 503, body: {} } : defaultWorkbenchRoute(call))
    const { server } = await mountSelected(() => <RevealIndicator />, { route })
    await waitFor(() => expect(live.reveals.status).toBe('unknown'))
    const group = indicator()
    expect(within(group).getByText('Reveal state unknown — reconnecting')).toBeInTheDocument()
    expect(within(group).queryByRole('button', { name: /^Revealed/ })).toBeNull()
    await userEvent.setup().click(within(group).getByRole('button', { name: 'Stop all' }))
    await waitFor(() => expect(stops(server)).toHaveLength(1))
    expect(stops(server)[0]).toMatchObject({ scope: 'all' })
  })
})

describe('the summary (REVEAL-14)', () => {
  it('names the first live document by title and who sees it', async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(one) })
    await waitFor(() => expect(live.reveals.titles.get(DOC)).toBe('Ondrey'))
    expect(within(indicator()).getByRole('button', { name: 'Revealed · Ondrey (table)' })).toBeInTheDocument()
  })

  it('counts the others, and names a private audience once the seats are known', async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(two) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    expect(within(indicator()).getByRole('button', { name: 'Revealed · Ondrey (table) · +1 more' })).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(within(indicator()).getByRole('button', { name: /^Revealed/ }))
    const list = within(indicator()).getByRole('list', { name: 'Everything the table is shown' })
    await waitFor(() => expect(within(list).getByText('Brannoch (Brann)')).toBeInTheDocument())
    expect(within(list).getByText('Ondrey (table)')).toBeInTheDocument()
  })
})

describe('Stop (X-3): always separate, never confirmed by a dialog', () => {
  it('Stop all (n) counts what is live and sends scope all at once', async () => {
    const { server } = await mountSelected(() => <RevealIndicator />, { route: withPicture(two) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    await userEvent.setup().click(within(indicator()).getByRole('button', { name: 'Stop all (2)' }))
    await waitFor(() => expect(stops(server)).toHaveLength(1))
    expect(stops(server)[0]).toMatchObject({ scope: 'all' })
    expect(stops(server)[0]).not.toHaveProperty('document_id')
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(live.reveals.announcement).toBe('Stopped showing everything'))
  })

  it('a row has its own Stop, naming the document and sending only that document', async () => {
    const { server } = await mountSelected(() => <RevealIndicator />, { route: withPicture(two) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    const user = userEvent.setup()
    await user.click(within(indicator()).getByRole('button', { name: /^Revealed/ }))
    await user.click(within(indicator()).getByRole('button', { name: 'Stop showing Brannoch' }))
    await waitFor(() => expect(stops(server)).toHaveLength(1))
    expect(stops(server)[0]).toMatchObject({ scope: 'document', document_id: OTHER })
    expect(live.reveals.sheet).toBeNull()
  })

  it("REVEAL-16: a Stop that cannot reach the server says it is retrying, and the table may still see it", async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(one, { status: 503, body: {} }) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(1))
    await userEvent.setup().click(within(indicator()).getByRole('button', { name: 'Stop all (1)' }))
    expect(await within(indicator()).findByText("Couldn't stop showing — retrying")).toBeInTheDocument()
    // The control stays: a second press sends now.
    expect(within(indicator()).getByRole('button', { name: 'Stop all (1)' })).toBeEnabled()
  })
})

describe('in the GM channel a listed projection opens its sheet', () => {
  it("opens that document's sheet, remembering the control for focus and the canvas document it was opened over", async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(two) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    const user = userEvent.setup()
    await user.click(within(indicator()).getByRole('button', { name: /^Revealed/ }))
    const row = within(within(indicator()).getByRole('list')).getByRole('button', { name: /^Brannoch/ })
    await user.click(row)
    expect(live.reveals.sheet).toEqual({ documentId: OTHER })
    expect(live.reveals.openerRef.current).toBe(row)
    // No canvas document is open here, so the sheet is tied to "none".
    expect(live.reveals.sheetFrom).toBeNull()
  })

  it('with a canvas document open, the sheet is tied to it', async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(two) })
    await run(() => live.actions.openDocument({ documentId: DOC, title: null }, { gesture: true }))
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    const user = userEvent.setup()
    await user.click(within(indicator()).getByRole('button', { name: /^Revealed/ }))
    await user.click(within(within(indicator()).getByRole('list')).getByRole('button', { name: /^Brannoch/ }))
    expect(live.reveals.sheetFrom).toBe(DOC)
  })

  it('closes the list when a projection is chosen', async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(two) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    const user = userEvent.setup()
    const toggle = within(indicator()).getByRole('button', { name: /^Revealed/ })
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await user.click(within(within(indicator()).getByRole('list')).getByRole('button', { name: /^Brannoch/ }))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within(indicator()).queryByRole('list')).toBeNull()
  })

  it('Escape closes the list, returns focus to its toggle, and is handled here so the shell drawer never acts', async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(two) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    const user = userEvent.setup()
    const toggle = within(indicator()).getByRole('button', { name: /^Revealed/ })
    await user.click(toggle)
    within(within(indicator()).getByRole('list')).getAllByRole('button')[0].focus()
    const press = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    act(() => {
      document.activeElement?.dispatchEvent(press)
    })
    expect(press.defaultPrevented).toBe(true)
    expect(within(indicator()).queryByRole('list')).toBeNull()
    expect(toggle).toHaveFocus()
  })
})

describe('outside the GM channel it is Stop-only (X-9, AE-82)', () => {
  it('a listed projection takes the GM back to the GM channel and opens no sheet', async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(two), mode: 'sage' })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    expect(live.nav.mode).toBe('sage')
    const user = userEvent.setup()
    await user.click(within(indicator()).getByRole('button', { name: /^Revealed/ }))
    await user.click(within(within(indicator()).getByRole('list')).getByRole('button', { name: /^Go to the GM channel for Brannoch/ }))
    expect(live.nav.mode).toBe('gm')
    expect(live.reveals.sheet).toBeNull()
  })

  it('still stops, all at once and one at a time', async () => {
    const { server } = await mountSelected(() => <RevealIndicator />, { route: withPicture(two), mode: 'rules' })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    const user = userEvent.setup()
    await user.click(within(indicator()).getByRole('button', { name: 'Stop all (2)' }))
    await waitFor(() => expect(stops(server)).toHaveLength(1))
    expect(stops(server)[0]).toMatchObject({ scope: 'all' })
  })

  it('offers no Update… even for a stale copy: no reveal can be widened from outside the GM channel', async () => {
    const stale = revealPicture({ epoch: 3, table: liveFixture(DOC, ['name'], { stale_text: true }) })
    await mountSelected(() => <RevealIndicator />, { route: withPicture(stale), mode: 'spell' })
    await waitFor(() => expect(live.reveals.titles.size).toBe(1))
    expect(within(indicator()).getByText('Table is seeing an earlier version')).toBeInTheDocument()
    expect(within(indicator()).queryByRole('button', { name: 'Update…' })).toBeNull()
  })
})

describe('REVEAL-8: an earlier version', () => {
  const stale = revealPicture({ epoch: 3, table: liveFixture(DOC, ['name'], { stale_text: true }) })

  it('says the table is seeing an earlier version and offers Update…, which opens that document\'s sheet', async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(stale) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(1))
    expect(within(indicator()).getByText('Table is seeing an earlier version')).toBeInTheDocument()
    await userEvent.setup().click(within(indicator()).getByRole('button', { name: 'Update…' }))
    expect(live.reveals.sheet).toEqual({ documentId: DOC })
  })

  it('says nothing when no copy is behind', async () => {
    await mountSelected(() => <RevealIndicator />, { route: withPicture(one) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(1))
    expect(within(indicator()).queryByText('Table is seeing an earlier version')).toBeNull()
    expect(within(indicator()).queryByRole('button', { name: 'Update…' })).toBeNull()
  })
})

describe('a held copy is not claimed as seen', () => {
  it('names a waiting document as waiting', async () => {
    const waiting = revealPicture({
      epoch: 3,
      participants: { [BRANN.participant_id]: liveFixture(DOC, ['name'], { pending_delivery: true }) },
    })
    await mountSelected(() => <RevealIndicator />, { route: withPicture(waiting) })
    await waitFor(() => expect(live.reveals.titles.size).toBe(1))
    const user = userEvent.setup()
    await user.click(within(indicator()).getByRole('button', { name: /^Revealed/ }))
    expect(within(indicator()).getByText('Waiting until you confirm the seat')).toBeInTheDocument()
  })
})

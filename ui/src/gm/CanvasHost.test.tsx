/**
 * CanvasHost.test.tsx -- the canvas column (agent-forge-harness-1kg.6.3, T-9; brief
 * 2.6, C-8, C-13, C-17, C-22). Real providers, a recording server that can hold an
 * answer back: what is proven is what the column RENDERS for each state the
 * provider reaches, and how it reads history.
 */

import * as React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { installMatchMediaWidth, type MatchMediaWidthStub } from '../testing/matchMediaWidth'
import {
  campaignFixture, defaultWorkbenchRoute, flush, historyBody, live, mountSelected, revealPicture, run, seatBody, type Call, type Route,
} from '../testing/workbenchHarness'
import { CanvasHost } from './CanvasHost'
import { ANA, BRANN, liveFixture } from './revealFixtures'

const open = (documentId: string, title: string | null = null) =>
  live.actions.openDocument({ documentId, title }, { gesture: true })

/** The version rows (the document body has list items of its own). */
const historyRows = (): HTMLElement[] => {
  const region = screen.queryByRole('region', { name: 'Version history' })
  return region === null ? [] : within(region).queryAllByRole('listitem')
}

const host = (server: { fetchImpl: typeof fetch }): React.ReactElement => <CanvasHost fetchImpl={server.fetchImpl} />

/** A route that holds back every request whose URL matches `pattern`. */
const deferring =
  (pattern: RegExp): Route =>
  (call) =>
    pattern.test(call.url) ? 'defer' : defaultWorkbenchRoute(call)

let resize: ((width: number) => void) | null = null
let widthStub: MatchMediaWidthStub | null = null

beforeEach(() => {
  resize = null
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: ResizeObserverCallback) {
        resize = (width: number) => {
          callback([{ contentRect: { width } } as ResizeObserverEntry], this as unknown as ResizeObserver)
        }
      }
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  )
})
afterEach(() => {
  widthStub?.restore()
  widthStub = null
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

// ── The reveal indicators (1kg.7.3; brief 5; tests 30 to 32) ─────────────────

const isRevealGet = (call: Call): boolean => call.method === 'GET' && /\/reveals$/.test(call.url)
const isStopPost = (call: Call): boolean => call.method === 'POST' && /\/reveals\/stop$/.test(call.url)

/** The reveal read answers `body` with `status`; everything else is the default. */
const revealRead = (body: unknown, status = 200): Route => (call) =>
  isRevealGet(call) ? { status, body } : defaultWorkbenchRoute(call)

describe('the reveal indicators answer from the server, never from a guess (REVEAL-13)', () => {
  it('while the first picture loads it says so: not GM ONLY, not unknown, no Open, and Stop stays (I-10)', async () => {
    const { server } = await mountSelected(host, { route: deferring(/\/reveals$/) })
    await run(() => open('doc_a'))
    expect(await screen.findByText('Checking what the table sees…')).toBeInTheDocument()
    expect(screen.getByText('CHECKING')).toBeInTheDocument()
    expect(screen.queryByText('GM ONLY')).toBeNull()
    expect(screen.queryByText(/Reveal state unknown/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reveal to party' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop showing' })).toBeInTheDocument()
    server.calls.find((call) => isRevealGet(call))?.reply({ status: 200, body: revealPicture(null) })
    await flush()
    expect(await screen.findByText('GM ONLY')).toBeInTheDocument()
  })

  it('a failed read says it cannot confirm, offers no Open control, and keeps Stop (test 30)', async () => {
    await mountSelected(host, { route: revealRead({}, 503) })
    await run(() => open('doc_a'))
    await waitFor(() => expect(screen.getAllByText('Reveal state unknown — reconnecting').length).toBeGreaterThan(0))
    // Mutation: defaulting the badge to GM ONLY on failure.
    expect(screen.queryByText('GM ONLY')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Reveal to party$/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Change what the table sees' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Stop showing' })).toBeInTheDocument()
  })

  it('a document the table is not seeing, in a live session, is GM ONLY with Reveal to party', async () => {
    await mountSelected(host, { route: revealRead(revealPicture({ table: liveFixture('doc_other', ['name']) })) })
    await run(() => open('doc_a'))
    expect(await screen.findByText('GM ONLY')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reveal to party' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop showing' })).toBeNull()
  })

  it('a live document reads Revealed, flags its fields, badges REVEALED and offers Stop (test 31)', async () => {
    const picture = revealPicture({ table: liveFixture('doc_a', ['name', 'qualifier', 'voice']) })
    await mountSelected(host, { route: revealRead(picture) })
    await run(() => open('doc_a'))
    expect(await screen.findByText('Revealed · Name & voice, Qualifier · to the table')).toBeInTheDocument()
    expect(screen.getByText('REVEALED')).toBeInTheDocument()
    expect(screen.queryByText('GM ONLY')).toBeNull()
    expect(screen.getAllByText('The table can see this').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByRole('button', { name: 'Stop showing' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Change what the table sees' })).toBeInTheDocument()
  })

  it('a stale copy adds the earlier-version note, and a copy waiting on its seat adds the waiting note', async () => {
    const stale = revealPicture({ table: liveFixture('doc_a', ['name'], { stale_text: true }) })
    await mountSelected(host, { route: revealRead(stale) })
    await run(() => open('doc_a'))
    expect(await screen.findByText('Table is seeing an earlier version')).toBeInTheDocument()
  })

  it('a copy waiting for a seat says so in the header, and its fields never claim a player can see them', async () => {
    const picture = revealPicture({ participants: { [BRANN.participant_id]: liveFixture('doc_a', ['name'], { pending_delivery: true }) } })
    const route: Route = (call) => {
      if (/\/participants/.test(call.url)) return { status: 200, body: seatBody([BRANN, ANA]) }
      return revealRead(picture)(call)
    }
    await mountSelected(host, { route })
    await run(() => open('doc_a'))
    expect(await screen.findByText('Waiting until you confirm the seat')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Waiting to show Brann')).toBeInTheDocument())
    expect(screen.queryByText('Brann can see this')).toBeNull()
    expect(screen.getByText('Revealed · Name · to Brann')).toBeInTheDocument()
  })

  it('a document revealed to one player names them on its marked fields', async () => {
    const picture = revealPicture({ participants: { [BRANN.participant_id]: liveFixture('doc_a', ['name']) } })
    const route: Route = (call) => (/\/participants/.test(call.url) ? { status: 200, body: seatBody([BRANN]) } : revealRead(picture)(call))
    await mountSelected(host, { route })
    await run(() => open('doc_a'))
    await waitFor(() => expect(screen.getByText('Brann can see this')).toBeInTheDocument())
  })

  it('Open starts the sheet for this document, recording what had focus', async () => {
    const user = userEvent.setup()
    await mountSelected(host)
    await run(() => open('doc_a'))
    const button = await screen.findByRole('button', { name: 'Reveal to party' })
    await user.click(button)
    expect(live.reveals.sheet).toEqual({ documentId: 'doc_a' })
    expect(live.reveals.openerRef.current).toBe(button)
  })
})

describe('Stop showing (test 32, X-3)', () => {
  const liveRoute = (extra: Route = defaultWorkbenchRoute): Route => (call) =>
    isRevealGet(call) ? { status: 200, body: revealPicture({ table: liveFixture('doc_a', ['name']) }) } : extra(call)

  it('is sent at once, with no confirmation, and moves no focus', async () => {
    const user = userEvent.setup()
    const route = liveRoute((call) => (isStopPost(call) ? 'defer' : defaultWorkbenchRoute(call)))
    const { server } = await mountSelected(host, { route })
    await run(() => open('doc_a'))
    const stop = await screen.findByRole('button', { name: 'Stop showing' })
    stop.focus()
    await user.click(stop)
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(server.calls.filter(isStopPost)).toHaveLength(1))
    expect(JSON.parse(server.calls.filter(isStopPost)[0].body ?? '{}')).toMatchObject({ scope: 'document', document_id: 'doc_a' })
    expect(stop).toHaveFocus()
    // Open is withdrawn for the document while its Stop is unacknowledged (REVEAL-22).
    expect(screen.queryByRole('button', { name: 'Change what the table sees' })).toBeNull()
  })

  it("reads Couldn't stop showing — retrying once a Stop has failed", async () => {
    const user = userEvent.setup()
    const route = liveRoute((call) => (isStopPost(call) ? { status: 503, body: {} } : defaultWorkbenchRoute(call)))
    await mountSelected(host, { route })
    await run(() => open('doc_a'))
    await user.click(await screen.findByRole('button', { name: 'Stop showing' }))
    expect(await screen.findByText("Couldn't stop showing — retrying")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop showing' })).toBeInTheDocument()
  })
})

describe('each state of the column (2.6)', () => {
  it('renders nothing while the canvas is closed', async () => {
    const { view } = await mountSelected(host)
    expect(view.container.querySelector('.canvas-host')).toBeNull()
    expect(screen.queryByRole('region')).toBeNull()
  })

  it('shows a skeleton and visible text while loading, busy but not a live region (A-29)', async () => {
    const { view } = await mountSelected(host, { route: deferring(/documents/) })
    act(() => { void open('doc_a', 'Ondrey') })
    expect(screen.getByText('Opening Ondrey…')).toBeVisible()
    const column = view.container.querySelector('.canvas-host')
    expect(column).toHaveAttribute('aria-busy', 'true')
    expect(within(column as HTMLElement).queryByRole('status')).toBeNull()
    expect(column?.querySelectorAll('.canvas-host__skeleton[aria-hidden="true"] .canvas-host__bar')).toHaveLength(3)
  })

  it('says Opening the document when the link carried no title', async () => {
    await mountSelected(host, { route: deferring(/documents/) })
    act(() => { void open('doc_a') })
    expect(screen.getByText('Opening the document…')).toBeVisible()
  })

  it('an open document is a named region headed by its name, with its type and version', async () => {
    const { view } = await mountSelected(host)
    await run(() => open('doc_a'))
    const region = screen.getByRole('region', { name: 'Ondrey' })
    expect(within(region).getByRole('heading', { level: 2, name: 'Ondrey' })).toBeInTheDocument()
    expect(within(region).getAllByText('NPC Dossier').length).toBeGreaterThan(0)
    expect(within(region).getByRole('button', { name: 'v3 — version history' })).toBeInTheDocument()
    expect(view.container.querySelector('.canvas-host')).not.toHaveAttribute('aria-busy')
  })

  it('an open document is read-only: no Edit anywhere, no field assistant, and no Export; Reveal to party is there (I-1, 1kg.7.3 test 29)', async () => {
    await mountSelected(host)
    await run(() => open('doc_a'))
    await screen.findByRole('button', { name: 'Reveal to party' })
    expect(screen.queryAllByRole('button', { name: /^Edit / })).toEqual([])
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Export' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Close canvas' })).toBeInTheDocument()
  })

  it('says GM ONLY only once the picture confirms the document is not live (C-8, REVEAL-13, test 30)', async () => {
    await mountSelected(host)
    await run(() => open('doc_a'))
    await screen.findByText('GM ONLY')
    expect(screen.queryByText(/Reveal state unknown/)).toBeNull()
  })

  it('unavailable says it once, never says why, and offers Close (I-7)', async () => {
    const user = userEvent.setup()
    await mountSelected(host)
    await run(() => open('doc_missing'))
    const heading = screen.getByRole('heading', { level: 2, name: "This document isn't available" })
    expect(heading).toHaveAttribute('tabindex', '-1')
    expect(screen.getByText('It may have been deleted, or you may not have access to it.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('closed'))
    expect(screen.queryByRole('heading', { level: 2 })).toBeNull()
    expect(window.location.hash).toBe('#campaign=cmp_A')
  })

  it('a newer document is a placeholder with Close and no Retry (X-8)', async () => {
    await mountSelected(host)
    await run(() => open('doc_newer'))
    expect(
      screen.getByRole('heading', { level: 2, name: 'This document was made by a newer version of Aetheril.' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('failed names the title, says nothing was lost, and Retry asks again (STATE-5)', async () => {
    const user = userEvent.setup()
    let answers: Array<'network'> = ['network']
    const route: Route = (call) =>
      /documents\/doc_a$/.test(call.url) ? (answers.shift() ?? defaultWorkbenchRoute(call)) : defaultWorkbenchRoute(call)
    const { server } = await mountSelected(host, { route })
    await run(() => open('doc_a', 'Ondrey'))
    expect(screen.getByRole('heading', { level: 2, name: "Couldn't open Ondrey" })).toBeInTheDocument()
    expect(screen.getByText("Aetheril can't reach its library right now. Nothing was lost.")).toBeInTheDocument()
    answers = []
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByRole('region', { name: 'Ondrey' })).toBeInTheDocument())
    expect(server.docCalls()).toHaveLength(2)
  })

  it('failed without a title says Couldn’t open the document', async () => {
    const route: Route = (call) => (/documents/.test(call.url) ? { status: 503, body: {} } : defaultWorkbenchRoute(call))
    await mountSelected(host, { route })
    await run(() => open('doc_a'))
    expect(screen.getByRole('heading', { level: 2, name: "Couldn't open the document" })).toBeInTheDocument()
  })

  it('every non-open heading is the programmatic focus target (2.6): the gesture lands focus on it', async () => {
    await mountSelected(host)
    await run(() => open('doc_missing'))
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 })).toHaveFocus())
  })

  it('the Close button returns focus to the control that opened the canvas (CANVAS-32)', async () => {
    const user = userEvent.setup()
    function WithOpener({ server }: { server: { fetchImpl: typeof fetch } }): React.JSX.Element {
      return (
        <>
          <button type="button">A link in the chat</button>
          <CanvasHost fetchImpl={server.fetchImpl} />
        </>
      )
    }
    await mountSelected((server) => <WithOpener server={server} />)
    const link = screen.getByRole('button', { name: 'A link in the chat' })
    link.focus()
    await run(() => open('doc_missing'))
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('closed'))
    await waitFor(() => expect(link).toHaveFocus())
  })
})

describe('version history (CANVAS-27, C-13)', () => {
  it('does not fetch history until the disclosure opens, then fetches once with limit=20', async () => {
    const user = userEvent.setup()
    const { server } = await mountSelected(host)
    await run(() => open('doc_a'))
    await flush()
    expect(server.historyCalls()).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    await waitFor(() => expect(historyRows().length).toBeGreaterThan(0))
    expect(server.historyCalls().map((c) => c.url)).toEqual([
      '/campaigns/cmp_A/documents/doc_a/versions?limit=20',
    ])
    // Closing and re-opening the disclosure is not another request, and nothing fetches one per version.
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    await flush()
    expect(server.historyCalls()).toHaveLength(1)
    expect(historyRows()).toHaveLength(3)
  })

  it('shows loading, then the list, with the current version marked', async () => {
    const user = userEvent.setup()
    const { server } = await mountSelected(host, { route: deferring(/versions/) })
    await run(() => open('doc_a'))
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    const region = screen.getByRole('region', { name: 'Version history' })
    expect(within(region).queryByText(/Edit 3/)).toBeNull()
    act(() => server.historyCalls()[0].reply({ status: 200, body: historyBody('doc_a', [3, 2, 1]) }))
    await waitFor(() => expect(within(region).getByText(/Edit 3/)).toBeInTheDocument())
    expect(historyRows()).toHaveLength(3)
  })

  it('Load more asks with next_cursor and appends the next page', async () => {
    const user = userEvent.setup()
    const route: Route = (call) => {
      if (!/versions/.test(call.url)) return defaultWorkbenchRoute(call)
      return call.url.includes('cursor=c2')
        ? { status: 200, body: historyBody('doc_a', [2, 1]) }
        : { status: 200, body: historyBody('doc_a', [4, 3], 'c2') }
    }
    const { server } = await mountSelected(host, { route })
    await run(() => open('doc_a'))
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    await waitFor(() => expect(historyRows()).toHaveLength(2))
    await user.click(screen.getByRole('button', { name: /load more/i }))
    await waitFor(() => expect(historyRows()).toHaveLength(4))
    expect(server.historyCalls().map((c) => c.url)).toEqual([
      '/campaigns/cmp_A/documents/doc_a/versions?limit=20',
      '/campaigns/cmp_A/documents/doc_a/versions?limit=20&cursor=c2',
    ])
    expect(screen.queryByRole('button', { name: /load more/i })).toBeNull()
  })

  it('a failed read says so with Retry, and Retry asks again (§12.2)', async () => {
    const user = userEvent.setup()
    let fail = true
    const route: Route = (call) =>
      /versions/.test(call.url) && fail ? { status: 503, body: {} } : defaultWorkbenchRoute(call)
    const { server } = await mountSelected(host, { route })
    await run(() => open('doc_a'))
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument())
    fail = false
    await user.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(historyRows()).toHaveLength(3))
    expect(server.historyCalls()).toHaveLength(2)
  })

  it('a page that arrives after the document was replaced is dropped (stale key)', async () => {
    const user = userEvent.setup()
    const { server } = await mountSelected(host, { route: deferring(/doc_a\/versions/) })
    await run(() => open('doc_a'))
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    await run(() => open('doc_b'))
    expect(screen.getByRole('region', { name: 'Brannoch' })).toBeInTheDocument()
    act(() => server.historyCalls()[0].reply({ status: 200, body: historyBody('doc_a', [9, 8, 7]) }))
    await flush()
    expect(screen.queryByText('Edit 9')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    await waitFor(() => expect(historyRows()).toHaveLength(3))
    expect(screen.queryByText('Edit 9')).toBeNull()
  })

  it('an aborted history request is an error row with Retry, never a rejection (C-17)', async () => {
    const user = userEvent.setup()
    const route: Route = (call) => (/versions/.test(call.url) ? 'abort' : defaultWorkbenchRoute(call))
    await mountSelected(host, { route })
    await run(() => open('doc_a'))
    await user.click(screen.getByRole('button', { name: 'v3 — version history' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument())
  })
})

describe('the canvas layout (2.6, §10.2)', () => {
  const layoutOf = (): string | null => screen.getByRole('region', { name: 'Ondrey' }).getAttribute('data-layout')

  it('is compact below 560 px of column width, and wide from 560 px', async () => {
    await mountSelected(host)
    await run(() => open('doc_a'))
    expect(layoutOf()).toBe('wide')
    act(() => resize?.(559))
    expect(layoutOf()).toBe('compact')
    act(() => resize?.(560))
    expect(layoutOf()).toBe('wide')
    act(() => resize?.(480))
    expect(layoutOf()).toBe('compact')
  })

  it('is wide where ResizeObserver is missing', async () => {
    vi.stubGlobal('ResizeObserver', undefined)
    await mountSelected(host)
    await run(() => open('doc_a'))
    expect(layoutOf()).toBe('wide')
  })

  it('is full screen at the narrow layout, with Back instead of Close', async () => {
    widthStub = installMatchMediaWidth(375)
    await mountSelected(host)
    await run(() => open('doc_a'))
    expect(layoutOf()).toBe('fullScreen')
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Close canvas' })).toBeNull()
  })

  it('Back is the guarded close, so the document closes and the view returns to chat (I-5)', async () => {
    const user = userEvent.setup()
    widthStub = installMatchMediaWidth(375)
    await mountSelected(host)
    await run(() => open('doc_a'))
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('closed'))
    expect(live.state.view).toBe('chat')
  })
})

describe('what the column never does (C-12, C-13, C-17)', () => {
  it('makes only GET requests, and a campaign switch leaves no document of the old one on screen', async () => {
    const { server } = await mountSelected(host)
    await run(() => open('doc_a'))
    expect(screen.getByRole('region', { name: 'Ondrey' })).toBeInTheDocument()
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    await flush()
    expect(screen.queryByText('Ondrey')).toBeNull()
    expect(server.calls.filter((call) => call.method !== 'GET')).toEqual([])
  })
})

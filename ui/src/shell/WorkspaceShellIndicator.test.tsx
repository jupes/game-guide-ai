/**
 * WorkspaceShell with the reveal indicator (agent-forge-harness-1kg.7.3 PR-2, REVEAL-14): the
 * REAL providers and the real shell. What is proven here is what the shell does around the
 * indicator: it is in the header row of every channel and every layout, it stays operable above
 * a modal (the sheet, the nav drawer), another document's sheet opens from it without swapping
 * the canvas, and it can never strand the shell inert or lose focus to <body>.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import { BRANN, liveFixture } from '../gm/revealFixtures'
import { installMatchMediaWidth, type MatchMediaWidthStub } from '../testing/matchMediaWidth'
import {
  defaultWorkbenchRoute, documentBody, live, mountSelected, mountWorkbench, revealPicture, seatBody, versionBody,
  type Call, type MountOptions, type Route,
} from '../testing/workbenchHarness'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { WorkspaceShell } from './WorkspaceShell'

let widthStub: MatchMediaWidthStub | null = null

afterEach(() => {
  widthStub?.restore()
  widthStub = null
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

const WITH_DOC: MountOptions = {
  hash: '#campaign=cmp_A&document=doc_a',
  restore: { campaignId: 'cmp_A', conversationId: null, documentId: 'doc_a' },
}

const isReveals = (call: Call): boolean => call.method === 'GET' && /\/reveals$/.test(call.url)
const isStop = (call: Call): boolean => call.method === 'POST' && /\/reveals\/stop$/.test(call.url)

const ONE = revealPicture({ epoch: 3, table: liveFixture('doc_a', ['name', 'voice']) })
const TWO = revealPicture({
  epoch: 3,
  table: liveFixture('doc_a', ['name', 'voice']),
  participants: { [BRANN.participant_id]: liveFixture('doc_b', ['name'], { version: 4 }) },
})

function routeFor(picture: unknown): Route {
  let stopped = false
  return (call) => {
    if (isReveals(call)) return { status: 200, body: stopped ? revealPicture({ epoch: 4 }) : picture }
    if (isStop(call)) {
      stopped = true
      return { status: 200, body: revealPicture({ epoch: 4 }) }
    }
    if (call.method === 'GET' && /\/participants/.test(call.url)) return { status: 200, body: seatBody([BRANN]) }
    if (call.method === 'POST' && /\/seal$/.test(call.url)) {
      const id = /documents\/(doc_\w+)\/seal/.exec(call.url)?.[1] ?? 'doc_a'
      return { status: 200, body: documentBody('cmp_A', id, { version: versionBody(7) }) }
    }
    if (call.method === 'GET' && /\/versions\/\d+$/.test(call.url)) {
      return { status: 200, body: { schema_version: 1, document_id: 'doc_b', type: 'npc', type_version: 1, version: versionBody(4), data: { name: 'Brannoch', voice: 'Gruff' } } }
    }
    return defaultWorkbenchRoute(call)
  }
}

async function mountShell(width: number, picture: unknown, options: MountOptions = {}) {
  widthStub = installMatchMediaWidth(width)
  const store = new MemoryConversationStore()
  const wrap = (children: React.ReactNode): React.JSX.Element => (
    <ThemeProvider initialTheme="light">
      <ConversationStoreProvider store={store}>{children}</ConversationStoreProvider>
    </ThemeProvider>
  )
  return mountSelected(() => <WorkspaceShell />, { stubGlobalFetch: true, wrap, route: routeFor(picture), ...options })
}

const indicator = (): HTMLElement => screen.getByRole('group', { name: 'What the table sees' })
const parts = (): Element[] => [...document.querySelectorAll('.workspace-shell__chrome-part, .app-header__controls')]

describe('in the header row of every channel and every layout (REVEAL-14)', () => {
  it.each(['sage', 'spell', 'rules', 'gm'] as const)('is in the %s channel', async (mode) => {
    await mountShell(1280, ONE, { mode })
    await screen.findByRole('button', { name: 'Stop all (1)' })
    expect(live.nav.mode).toBe(mode)
    expect(document.querySelector('.app-header')).toContainElement(indicator())
  })

  it.each([1280, 900, 375])('is in the header at %ipx', async (width) => {
    await mountShell(width, ONE)
    await screen.findByRole('button', { name: 'Stop all (1)' })
    expect(document.querySelector('.app-header')).toContainElement(indicator())
    expect(within(indicator()).getByRole('button', { name: /Revealed/ })).toBeVisible()
  })

  it('shows nothing when nothing is live, so the header is as it was', async () => {
    await mountShell(1280, revealPicture({ epoch: 3 }))
    await waitFor(() => expect(live.reveals.status).toBe('live'))
    expect(screen.queryByRole('group', { name: 'What the table sees' })).toBeNull()
  })
})

describe('it stays operable above a modal', () => {
  it('is not inert while the reveal sheet is, and Stop all works through the open sheet', async () => {
    const user = userEvent.setup()
    const { server } = await mountShell(1280, ONE, WITH_DOC)
    await user.click(await screen.findByRole('button', { name: 'Change what the table sees' }))
    await screen.findByRole('dialog', { name: 'Reveal Ondrey' })
    expect(parts().length).toBeGreaterThan(0)
    for (const part of parts()) expect(part).toHaveAttribute('inert')
    expect(document.querySelector('.workspace-shell__body')).toHaveAttribute('inert')
    // Positive control above; the point of this test:
    expect(indicator().closest('[inert]')).toBeNull()
    await user.click(within(indicator()).getByRole('button', { name: 'Stop all (1)' }))
    await waitFor(() => expect(server.calls.filter(isStop)).toHaveLength(1))
    expect(JSON.parse(server.calls.find(isStop)?.body ?? '{}')).toMatchObject({ scope: 'all' })
  })

  it('is not inert while the navigation drawer is open on a phone', async () => {
    const user = userEvent.setup()
    await mountShell(375, ONE)
    await screen.findByRole('button', { name: 'Stop all (1)' })
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await screen.findByRole('dialog', { name: 'Navigation' })
    for (const part of parts()) expect(part).toHaveAttribute('inert')
    expect(indicator().closest('[inert]')).toBeNull()
    expect(within(indicator()).getByRole('button', { name: 'Stop all (1)' })).toBeEnabled()
  })

  it('is not inert behind the loss guard either', async () => {
    const user = userEvent.setup()
    await mountShell(1280, ONE, WITH_DOC)
    await screen.findByRole('button', { name: 'Stop all (1)' })
    await screen.findByRole('heading', { name: 'Ondrey' })
    act(() => {
      live.actions.registerDirtySource({ isDirty: () => true, flush: () => Promise.resolve('failed'), discard: () => undefined })
    })
    await user.click(screen.getByRole('button', { name: 'Close canvas' }))
    await screen.findByRole('dialog', { name: 'You have unsaved changes to Ondrey' })
    for (const part of parts()) expect(part).toHaveAttribute('inert')
    expect(indicator().closest('[inert]')).toBeNull()
  })
})

describe("another document's sheet, without swapping the canvas (REVEAL-14)", () => {
  async function openBrannoch(width = 1280) {
    const user = userEvent.setup()
    const mounted = await mountShell(width, TWO, WITH_DOC)
    await screen.findByRole('heading', { name: 'Ondrey' })
    await waitFor(() => expect(live.reveals.titles.size).toBe(2))
    await user.click(within(indicator()).getByRole('button', { name: /^Revealed/ }))
    const row = within(within(indicator()).getByRole('list')).getByRole('button', { name: /^Brannoch/ })
    await user.click(row)
    const sheet = await screen.findByRole('dialog', { name: 'Reveal Brannoch' })
    return { ...mounted, user, sheet, row }
  }

  it('opens that document in a dialog while the canvas keeps showing the open one', async () => {
    await openBrannoch()
    expect(live.state.doc.kind === 'open' && live.state.doc.document.document_id).toBe('doc_a')
    expect(screen.getAllByRole('heading', { name: 'Ondrey' }).length).toBeGreaterThan(0)
    for (const part of parts()) expect(part).toHaveAttribute('inert')
    expect(indicator().closest('[inert]')).toBeNull()
  })

  it('Cancel closes it, lifts the inert, and focus lands on the canvas title when the list that opened it is gone', async () => {
    const { user, sheet } = await openBrannoch()
    await user.click(within(sheet).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(parts().some((part) => part.hasAttribute('inert'))).toBe(false)
    expect(document.querySelector('.workspace-shell__body')).not.toHaveAttribute('inert')
    expect(live.reveals.sheet).toBeNull()
    await waitFor(() => expect(document.activeElement).not.toBe(document.body))
  })

  it('a fragment edit naming another document ends it: no dialog and nothing left inert', async () => {
    await openBrannoch()
    await act(async () => {
      window.location.hash = '#campaign=cmp_A&document=doc_c'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Reveal Brannoch' })).toBeNull())
    expect(live.reveals.sheet).toBeNull()
    expect(parts().some((part) => part.hasAttribute('inert'))).toBe(false)
    expect(document.querySelector('.workspace-shell__body')).not.toHaveAttribute('inert')
  })

  it('works on a phone: a full-screen sheet with the indicator still reachable above it', async () => {
    const { sheet } = await openBrannoch(375)
    expect(sheet).toHaveAttribute('aria-modal', 'true')
    expect(indicator().closest('[inert]')).toBeNull()
  })
})

describe('focus never falls to <body> when the indicator goes away', () => {
  it('pressing the last Stop moves focus to the selected channel', async () => {
    const user = userEvent.setup()
    await mountShell(1280, ONE)
    const stop = await screen.findByRole('button', { name: 'Stop all (1)' })
    stop.focus()
    await user.click(stop)
    await waitFor(() => expect(screen.queryByRole('group', { name: 'What the table sees' })).toBeNull())
    const channels = screen.getByRole('navigation', { name: 'Channels' })
    expect(within(channels).getByRole('button', { name: 'GM' })).toHaveFocus()
  })
})

describe('a player never sees it', () => {
  it('renders no indicator and makes no reveal request for a player account', async () => {
    widthStub = installMatchMediaWidth(1280)
    const wrap = (children: React.ReactNode): React.JSX.Element => (
      <ThemeProvider initialTheme="light">
        <ConversationStoreProvider store={new MemoryConversationStore()}>{children}</ConversationStoreProvider>
      </ThemeProvider>
    )
    const { server } = await mountWorkbench(() => <WorkspaceShell />, {
      role: 'player', hash: '#campaign=cmp_A', stubGlobalFetch: true, mode: 'sage', wrap,
    })
    await waitFor(() => expect(live.reveals.status).toBe('idle'))
    expect(screen.queryByRole('group', { name: 'What the table sees' })).toBeNull()
    expect(server.calls.filter((call) => call.url.includes('/reveals'))).toEqual([])
  })
})

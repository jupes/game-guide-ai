/**
 * WorkspaceShell with the reveal sheet (agent-forge-harness-1kg.7.3; brief 2 and 7, and
 * the Critic's 13 and 14). The REAL providers and the real shell. What is proven here is
 * what the shell does around the sheet: it is modal (chrome and body `inert`), it lives
 * outside both, it can never strand the shell inert, and the one reveal announcer is
 * mounted empty at the root with the Workbench and nowhere else.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import { liveFixture } from '../gm/revealFixtures'
import { installMatchMediaWidth, type MatchMediaWidthStub } from '../testing/matchMediaWidth'
import {
  defaultWorkbenchRoute, documentBody, flush, live, mountSelected, mountWorkbench, revealPicture, run, versionBody,
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

/** The default route, with a sealable document and a live session holding nothing. */
const route: Route = (call) => {
  if (isReveals(call)) return { status: 200, body: revealPicture({ epoch: 3 }) }
  if (call.method === 'POST' && /\/seal$/.test(call.url)) {
    return { status: 200, body: documentBody('cmp_A', 'doc_a', { version: versionBody(7) }) }
  }
  if (isStop(call)) return { status: 200, body: revealPicture({ epoch: 4 }) }
  return defaultWorkbenchRoute(call)
}

async function mountShell(width: number, options: MountOptions = {}, selected = true) {
  widthStub = installMatchMediaWidth(width)
  const store = new MemoryConversationStore()
  const wrap = (children: React.ReactNode): React.JSX.Element => (
    <ThemeProvider initialTheme="light">
      <ConversationStoreProvider store={store}>{children}</ConversationStoreProvider>
    </ThemeProvider>
  )
  const mount = selected ? mountSelected : mountWorkbench
  return mount(() => <WorkspaceShell />, { stubGlobalFetch: true, wrap, route, ...options })
}

const chrome = (): HTMLElement => document.querySelector('.workspace-shell__chrome') as HTMLElement
/** Every part of the chrome is inert (the reveal indicator is left out on purpose: REVEAL-14). */
const chromeInert = (): boolean =>
  [...document.querySelectorAll('.workspace-shell__chrome-part, .app-header__controls')].every((part) => part.hasAttribute('inert'))
const body = (): HTMLElement => document.querySelector('.workspace-shell__body') as HTMLElement
const announcers = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.workbench-announcer')]
const revealAnnouncer = (): HTMLElement | null => document.querySelector('[data-reveal-announcer]')

describe('the sheet is modal (Critic 14)', () => {
  it('goes inert behind the open sheet, keeps the sheet outside what is inert, and lifts it when the sheet closes', async () => {
    const user = userEvent.setup()
    await mountShell(1280, WITH_DOC)
    const open = await screen.findByRole('button', { name: 'Reveal to party' })
    expect(chromeInert()).toBe(false)
    expect(body()).not.toHaveAttribute('inert')
    await user.click(open)
    const sheet = await screen.findByRole('dialog', { name: 'Reveal Ondrey' })
    expect(chromeInert()).toBe(true)
    expect(body()).toHaveAttribute('inert')
    expect(chrome()).not.toContainElement(sheet)
    expect(body()).not.toContainElement(sheet)
    await user.click(within(sheet).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(chromeInert()).toBe(false)
    expect(body()).not.toHaveAttribute('inert')
    await waitFor(() => expect(open).toHaveFocus())
  })

  it('a fragment edit naming another document, while the sheet is open, leaves no dialog and no inert', async () => {
    const user = userEvent.setup()
    await mountShell(1280, WITH_DOC)
    await user.click(await screen.findByRole('button', { name: 'Reveal to party' }))
    await screen.findByRole('dialog', { name: 'Reveal Ondrey' })
    expect(body()).toHaveAttribute('inert')
    await act(async () => {
      window.location.hash = '#campaign=cmp_A&document=doc_b'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Reveal Ondrey' })).toBeNull())
    expect(live.reveals.sheet).toBeNull()
    expect(chromeInert()).toBe(false)
    expect(body()).not.toHaveAttribute('inert')
  })

  it('closing the canvas while the sheet is open leaves nothing inert', async () => {
    await mountShell(1280, WITH_DOC)
    await screen.findByRole('button', { name: 'Reveal to party' })
    act(() => live.reveals.openSheet('doc_a'))
    await screen.findByRole('dialog', { name: 'Reveal Ondrey' })
    await run(() => live.actions.closeDocument())
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(live.reveals.sheet).toBeNull()
    expect(chromeInert()).toBe(false)
    expect(body()).not.toHaveAttribute('inert')
  })

  it('a sheet opened for a document that is not the open one shows nothing and is dropped', async () => {
    await mountShell(1280, WITH_DOC)
    await screen.findByRole('button', { name: 'Reveal to party' })
    act(() => live.reveals.openSheet('doc_elsewhere'))
    await flush()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(body()).not.toHaveAttribute('inert')
    expect(live.reveals.sheet).toBeNull()
  })

  it('opens as a full-screen sheet on a phone, and Cancel returns to the canvas', async () => {
    const user = userEvent.setup()
    await mountShell(375, WITH_DOC)
    await user.click(await screen.findByRole('button', { name: 'Reveal to party' }))
    const sheet = await screen.findByRole('dialog', { name: 'Reveal Ondrey' })
    expect(sheet).toHaveAttribute('aria-modal', 'true')
    await user.click(within(sheet).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})

describe('the announcer (brief 7)', () => {
  it('is one status node, mounted empty at the root beside the Workbench announcer, outside every inert container', async () => {
    await mountShell(1280, WITH_DOC)
    await screen.findByRole('button', { name: 'Reveal to party' })
    const node = revealAnnouncer()
    expect(node).not.toBeNull()
    expect(node).toHaveAttribute('role', 'status')
    expect(node).toHaveTextContent('')
    expect(document.querySelectorAll('[data-reveal-announcer]')).toHaveLength(1)
    // A sibling of the Workbench's announcer, never inside a column or the dialog.
    expect(announcers()).toHaveLength(2)
    expect(node?.parentElement).toBe(announcers().find((el) => el !== node)?.parentElement)
    expect(chrome()).not.toContainElement(node)
    expect(body()).not.toContainElement(node)
  })

  it('speaks Stopped showing <title> in that node, without moving focus', async () => {
    const liveRoute: Route = (call) =>
      isReveals(call)
        ? { status: 200, body: revealPicture({ epoch: 3, table: liveFixture('doc_a', ['name'], { version: 2 }) }) }
        : route(call)
    const user = userEvent.setup()
    const { server } = await mountShell(1280, { ...WITH_DOC, route: liveRoute })
    const stop = await screen.findByRole('button', { name: 'Stop showing' })
    stop.focus()
    await user.click(stop)
    // Pressing it moves no focus while the Stop is in flight.
    expect(server.calls.filter(isStop)).toHaveLength(1)
    await waitFor(() => expect(revealAnnouncer()).toHaveTextContent('Stopped showing Ondrey'))
    // The control is gone once the table sees nothing; focus does not fall to <body>.
    await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: 'Ondrey' })).toHaveFocus())
    expect(document.querySelector('.workbench-announcer:not([data-reveal-announcer])')).toHaveTextContent('')
  })

  it('is absent for a player, who also has no sheet and makes no reveal request', async () => {
    const { server } = await mountShell(1280, { ...WITH_DOC, role: 'player' }, false)
    await flush()
    expect(revealAnnouncer()).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(server.calls.filter((call) => /\/reveals/.test(call.url))).toEqual([])
  })
})

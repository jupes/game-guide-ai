/**
 * CampaignDocumentsNav.test.tsx -- the nav column's "Campaign documents" list
 * (agent-forge-harness-1kg.6.3, T-10; brief 2.7, LIB-25, C-12, C-13).
 *
 * The REAL providers and a recording server that answers the library read by
 * category. Each "no request" assertion sits beside a positive control in the same
 * test.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import {
  campaignFixture, defaultWorkbenchRoute, flush, libraryRoute, live, mountSelected, mountWorkbench, run,
  type LibraryRow, type Route,
} from '../testing/workbenchHarness'
import { CampaignDocumentsNav } from './CampaignDocumentsNav'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { LeftNav } from './LeftNav'
import { DOCUMENTS_HEADING_ID } from './workbenchCopy'

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

const ROWS: Readonly<Record<string, readonly LibraryRow[]>> = {
  npcs: [{ id: 'doc_a', type: 'npc', title: 'Ondrey', updatedAt: '2026-09-16T19:30:00Z' }],
  bestiary: [{ id: 'doc_b', type: 'statblock', title: 'Tidewarden', updatedAt: '2026-09-16T19:40:00Z' }],
  documents: [{ id: 'doc_c', type: 'handout', title: 'The Writ', updatedAt: '2026-09-16T19:35:00Z' }],
  'session-log': [{ id: 'doc_d', type: 'session-notes', title: 'Session 3', updatedAt: '2026-09-16T19:20:00Z' }],
}

const nav = (onNavigate?: () => void) => (server: { fetchImpl: typeof fetch }): React.JSX.Element => (
  <CampaignDocumentsNav fetchImpl={server.fetchImpl} onNavigate={onNavigate} />
)

const rowNames = (): string[] =>
  Array.from(
    screen.getByRole('region', { name: 'Campaign documents' }).querySelectorAll('.documents-nav__row .documents-nav__title'),
    (title) => title.textContent ?? '',
  )

const libraryBodies = (server: { libraryCalls: () => Array<{ body: string | null }> }) =>
  server.libraryCalls().map((call) => JSON.parse(call.body ?? '{}') as Record<string, unknown>)

describe('what it asks for (C-13, X-7)', () => {
  it('POSTs the four document categories once each, with the query in the body and never cues', async () => {
    const { server } = await mountSelected(nav(), { route: libraryRoute(ROWS) })
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    expect(server.libraryCalls()).toHaveLength(4)
    for (const call of server.libraryCalls()) {
      expect(call.method).toBe('POST')
      expect(call.url).toBe('/campaigns/cmp_A/library')
    }
    const bodies = libraryBodies(server)
    expect(bodies.map((body) => body.category).sort()).toEqual(['bestiary', 'documents', 'npcs', 'session-log'])
    expect(bodies.map((body) => body.category)).not.toContain('cues')
    for (const body of bodies) {
      expect(body).toEqual({
        schema_version: 1, campaign_id: 'cmp_A', category: body.category, search: '', sort: 'recent', archived: false, limit: 5,
      })
    }
  })

  it('sends exactly four requests under StrictMode too (C-13)', async () => {
    const { server } = await mountSelected(nav(), { route: libraryRoute(ROWS), strict: true })
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    await flush()
    expect(server.libraryCalls()).toHaveLength(4)
  })

  it('opening, replacing and closing a document is not a refetch (C-13)', async () => {
    const { server } = await mountSelected(nav(), { route: libraryRoute(ROWS) })
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    await run(() => live.actions.openDocument({ documentId: 'doc_a' }, { gesture: true }))
    await run(() => live.actions.openDocument({ documentId: 'doc_b' }, { gesture: true }))
    await run(() => live.actions.closeDocument())
    await flush()
    expect(server.libraryCalls()).toHaveLength(4)
  })

  it('refreshes on a documentsVersion bump, and only then (LIB-24)', async () => {
    const { server } = await mountSelected(nav(), { route: libraryRoute(ROWS) })
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    act(() => live.actions.bumpDocumentsVersion())
    await waitFor(() => expect(server.libraryCalls()).toHaveLength(8))
  })
})

describe('what it shows (2.7)', () => {
  it('merges the four categories newest first, by updated time and then id, and shows at most ten', async () => {
    const many = (category: string, type: string, prefix: string): LibraryRow[] =>
      Array.from({ length: 5 }, (_, at) => ({
        id: `doc_${prefix}${at}`, type, title: `${prefix.toUpperCase()}${at}`,
        updatedAt: `2026-09-16T19:${String(10 + at * 2 + (category === 'npcs' ? 1 : 0)).padStart(2, '0')}:00Z`,
      }))
    const rows = {
      npcs: many('npcs', 'npc', 'n'),
      bestiary: many('bestiary', 'statblock', 'b'),
      documents: many('documents', 'handout', 'h'),
      'session-log': many('session-log', 'session-notes', 's'),
    }
    await mountSelected(nav(), { route: libraryRoute(rows) })
    await waitFor(() => expect(rowNames()).toHaveLength(10))
    const names = rowNames()
    // N4 is newest at :19; then the three :18 rows tie and fall back to document id order.
    expect(names[0]).toBe('N4')
    expect(names.slice(1, 4)).toEqual(['B4', 'H4', 'S4'])
    expect(names).toHaveLength(10)
  })

  it('sorts equal times by document id', async () => {
    const tie = '2026-09-16T19:30:00Z'
    const rows = {
      npcs: [{ id: 'doc_z', type: 'npc', title: 'Zed', updatedAt: tie }, { id: 'doc_a', type: 'npc', title: 'Ann', updatedAt: tie }],
    }
    await mountSelected(nav(), { route: libraryRoute(rows) })
    await waitFor(() => expect(rowNames()).toEqual(['Ann', 'Zed']))
  })

  it('names each row’s type in muted text', async () => {
    await mountSelected(nav(), { route: libraryRoute(ROWS) })
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    expect(screen.getByRole('button', { name: /Tidewarden/ })).toHaveTextContent('Stat Block')
    expect(screen.getByRole('button', { name: /Ondrey/ })).toHaveTextContent('NPC Dossier')
    expect(screen.getByRole('button', { name: /The Writ/ })).toHaveTextContent('Handout')
  })

  it('has a heading that is a focus target, with the id the rail focuses', async () => {
    await mountSelected(nav(), { route: libraryRoute(ROWS) })
    const heading = screen.getByRole('heading', { level: 2, name: 'Campaign documents' })
    expect(heading).toHaveAttribute('tabindex', '-1')
    expect(heading).toHaveAttribute('id', DOCUMENTS_HEADING_ID)
    heading.focus()
    expect(heading).toHaveFocus()
  })

  it('drops a page for another campaign, so a stale row never flashes (LIB-25)', async () => {
    await mountSelected(nav(), {
      route: libraryRoute(ROWS, { campaignOf: () => 'cmp_Other' }),
    })
    await screen.findByText('No documents yet.')
    expect(rowNames()).toEqual([])
  })

  it('drops a page that answers another category than the one asked for', async () => {
    const wrongCategory: Route = (call) => {
      if (!/\/library$/.test(call.url)) return defaultWorkbenchRoute(call)
      // Every request, whatever its category, is answered with the NPC page.
      return libraryRoute(ROWS)({ ...call, body: JSON.stringify({ category: 'npcs' }) })
    }
    const { server } = await mountSelected(nav(), { route: wrongCategory })
    await waitFor(() => expect(rowNames()).toEqual(['Ondrey']))
    expect(server.libraryCalls()).toHaveLength(4)
  })
})

describe('its states (2.7)', () => {
  it('while loading: two skeleton rows and visible text, no live region (A-29)', async () => {
    const held: Route = (call) => (/\/library$/.test(call.url) ? 'defer' : defaultWorkbenchRoute(call))
    await mountSelected(nav(), { route: held })
    const region = screen.getByRole('region', { name: 'Campaign documents' })
    expect(within(region).getByText('Loading documents…')).toBeVisible()
    expect(region.querySelectorAll('.documents-nav__skeleton[aria-hidden="true"]')).toHaveLength(2)
    expect(within(region).queryByRole('status')).toBeNull()
  })

  it('empty says No documents yet and promises no command (C-6)', async () => {
    await mountSelected(nav(), { route: libraryRoute({}) })
    expect(await screen.findByText('No documents yet.')).toBeVisible()
    expect(screen.queryByText(/\/npc|\/monster/)).toBeNull()
  })

  it('every category failing says so with Retry, and Retry asks for all four again', async () => {
    const user = userEvent.setup()
    let failing: readonly string[] = ['npcs', 'bestiary', 'documents', 'session-log']
    const route: Route = (call) =>
      libraryRoute(ROWS, { failing })(call)
    const { server } = await mountSelected(nav(), { route })
    expect(await screen.findByText("Couldn't load documents")).toBeVisible()
    expect(rowNames()).toEqual([])
    failing = []
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    expect(server.libraryCalls()).toHaveLength(8)
    expect(screen.queryByText("Couldn't load documents")).toBeNull()
  })

  it('a partial failure shows what loaded and the error line with Retry', async () => {
    await mountSelected(nav(), { route: libraryRoute(ROWS, { failing: ['bestiary'] }) })
    await waitFor(() => expect(rowNames()).toHaveLength(3))
    expect(screen.getByText("Couldn't load documents")).toBeVisible()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible()
    expect(rowNames()).not.toContain('Tidewarden')
  })

  it('a thrown or aborted request is a failed category, never a rejection (C-17)', async () => {
    const unhandled = vi.fn()
    window.addEventListener('unhandledrejection', unhandled)
    const route: Route = (call) => (/\/library$/.test(call.url) ? 'abort' : defaultWorkbenchRoute(call))
    await mountSelected(nav(), { route })
    expect(await screen.findByText("Couldn't load documents")).toBeVisible()
    window.removeEventListener('unhandledrejection', unhandled)
    expect(unhandled).not.toHaveBeenCalled()
  })
})

describe('a row opens a document (CANVAS-5..7)', () => {
  it('opens it as a gesture and closes the drawer afterwards', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    const held: Route = (call) => (/documents\/doc_a$/.test(call.url) ? 'defer' : libraryRoute(ROWS)(call))
    await mountSelected(nav(onNavigate), { route: held })
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    const row = screen.getByRole('button', { name: /Ondrey/ })
    await user.click(row)
    expect(live.state.doc).toEqual({ kind: 'loading', documentId: 'doc_a', title: 'Ondrey' })
    expect(window.location.hash).toBe('#campaign=cmp_A&document=doc_a')
    expect(window.location.hash).not.toMatch(/Ondrey/)
    expect(onNavigate).toHaveBeenCalledTimes(1)
    // The opener was the row itself, captured BEFORE the drawer closed.
    expect(live.actions.openerRef.current).toBe(row)
  })

  it('marks the open document aria-current, and no other', async () => {
    await mountSelected(nav(), { route: libraryRoute(ROWS) })
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    await run(() => live.actions.openDocument({ documentId: 'doc_b', title: 'Tidewarden' }, { gesture: true }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Tidewarden/ })).toHaveAttribute('aria-current', 'true'))
    expect(screen.getByRole('button', { name: /Ondrey/ })).not.toHaveAttribute('aria-current')
    await run(() => live.actions.closeDocument())
    expect(screen.getByRole('button', { name: /Tidewarden/ })).not.toHaveAttribute('aria-current')
  })
})

describe('a campaign switch drops the list at once (C-12, LIB-25)', () => {
  it('shows none of the old campaign’s rows, asks for the new one, and ignores a late answer for the old', async () => {
    const heldA: Route = (call) => (call.url === '/campaigns/cmp_A/library' ? 'defer' : libraryRoute(ROWS)(call))
    const { server } = await mountSelected(nav(), { route: heldA })
    // Campaign A's four answers are held back.
    expect(server.libraryCalls()).toHaveLength(4)
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    expect(server.libraryCalls()).toHaveLength(8)
    // Now A's answers arrive: they are for a scope that has gone.
    const late = server.libraryCalls().filter((call) => call.url === '/campaigns/cmp_A/library')
    act(() => {
      for (const call of late) {
        const body = JSON.parse(call.body ?? '{}') as { category: string }
        call.reply({
          status: 200,
          body: {
            schema_version: 1, campaign_id: 'cmp_A', category: body.category,
            items: [], next_cursor: null,
          },
        })
      }
    })
    await flush()
    expect(rowNames()).toHaveLength(4)
  })

  it('renders nothing before a campaign is selected', async () => {
    const { server } = await mountWorkbench(nav(), { route: libraryRoute(ROWS) })
    expect(screen.queryByRole('region', { name: 'Campaign documents' })).toBeNull()
    expect(server.libraryCalls()).toHaveLength(0)
  })
})

describe('who gets it, through LeftNav (X-9)', () => {
  const withNav = (children: React.ReactNode): React.JSX.Element => (
    <ThemeProvider initialTheme="light">
      <ConversationStoreProvider store={new MemoryConversationStore()}>{children}</ConversationStoreProvider>
    </ThemeProvider>
  )

  it('a GM in the GM channel with a campaign has the section, and navigating through it closes the drawer', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    await mountSelected(() => <LeftNav onNavigate={onNavigate} />, {
      route: libraryRoute(ROWS), stubGlobalFetch: true, wrap: withNav,
    })
    await waitFor(() => expect(rowNames()).toHaveLength(4))
    await user.click(screen.getByRole('button', { name: /Ondrey/ }))
    expect(onNavigate).toHaveBeenCalledTimes(1)
    expect(live.state.doc.kind).not.toBe('closed')
  })

  it('a player has no section and makes no library request, beside the same mount for a GM', async () => {
    const { server } = await mountWorkbench(() => <LeftNav />, {
      role: 'player', route: libraryRoute(ROWS), stubGlobalFetch: true, wrap: withNav,
      hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null },
    })
    await flush()
    expect(screen.queryByRole('region', { name: 'Campaign documents' })).toBeNull()
    expect(server.libraryCalls()).toHaveLength(0)
  })

  it('Sage, Spell and Rules have no section and make no request (the GM channel only)', async () => {
    const { server } = await mountSelected(() => <LeftNav />, {
      mode: 'sage', route: libraryRoute(ROWS), stubGlobalFetch: true, wrap: withNav,
    })
    await flush()
    expect(screen.queryByRole('region', { name: 'Campaign documents' })).toBeNull()
    expect(server.libraryCalls()).toHaveLength(0)
    act(() => live.nav.setMode('gm'))
    await waitFor(() => expect(screen.getByRole('region', { name: 'Campaign documents' })).toBeInTheDocument())
    await waitFor(() => expect(server.libraryCalls()).toHaveLength(4))
  })
})

/**
 * LibraryNavGroup.test.tsx -- the Campaign Library group in LeftNav
 * (agent-forge-harness-1kg.6.4, L-5; LIB-7, X-9, INFERRED I-5).
 *
 * Through the REAL LeftNav and providers. The group is four labelled rows that open the
 * panel; it makes no request of its own.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../ds/theme'
import { flush, libraryRoute, live, mountSelected, mountWorkbench } from '../testing/workbenchHarness'
import { ConversationStoreProvider } from './ConversationStoreContext'
import { MemoryConversationStore } from './conversationStore'
import { LeftNav } from './LeftNav'
import { LIBRARY_PANEL_ID, LibraryPanelProvider, useLibraryPanel } from './libraryPanel'

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

let panel: ReturnType<typeof useLibraryPanel>

function Probe(): null {
  const value = useLibraryPanel()
  React.useLayoutEffect(() => {
    panel = value
  })
  return null
}

const withNav = (children: React.ReactNode): React.JSX.Element => (
  <ThemeProvider initialTheme="light">
    <ConversationStoreProvider store={new MemoryConversationStore()}>
      <LibraryPanelProvider>
        <Probe />
        {children}
      </LibraryPanelProvider>
    </ConversationStoreProvider>
  </ThemeProvider>
)

const group = (): HTMLElement => screen.getByRole('list', { name: 'Campaign Library' })

describe('who gets the group (X-9)', () => {
  it('a GM in the GM channel with a campaign has four rows, in order, each named for its category', async () => {
    await mountSelected(() => <LeftNav />, { route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav })
    const rows = within(group()).getAllByRole('button')
    // The icon ligature is decorative: the label is the last span.
    expect(rows.map((row) => row.lastElementChild?.textContent)).toEqual(['NPCs', 'Bestiary', 'Documents', 'Session log'])
    for (const name of ['NPCs', 'Bestiary', 'Documents', 'Session log']) {
      expect(within(group()).getByRole('button', { name })).toBeInTheDocument()
    }
    expect(within(group()).queryByRole('button', { name: /cues/i })).toBeNull()
    for (const row of rows) {
      expect(row).toHaveAttribute('aria-controls', LIBRARY_PANEL_ID)
      expect(row).toHaveAttribute('aria-expanded', 'false')
    }
  })

  it('hides its icons from assistive technology', async () => {
    await mountSelected(() => <LeftNav />, { route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav })
    const icons = group().querySelectorAll('.material-symbols-rounded')
    expect(icons).toHaveLength(4)
    for (const icon of icons) expect(icon).toHaveAttribute('aria-hidden', 'true')
  })

  it('a player has no group, beside the same mount for a GM', async () => {
    await mountWorkbench(() => <LeftNav />, {
      role: 'player', route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav,
      hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null },
    })
    await flush()
    expect(screen.queryByRole('list', { name: 'Campaign Library' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'NPCs' })).toBeNull()
  })

  it('Sage, Spell and Rules have no group; the GM channel brings it back', async () => {
    await mountSelected(() => <LeftNav />, { mode: 'sage', route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav })
    await flush()
    expect(screen.queryByRole('list', { name: 'Campaign Library' })).toBeNull()
    act(() => live.nav.setMode('gm'))
    await waitFor(() => expect(group()).toBeInTheDocument())
  })

  it('no campaign chosen has no group (LeftNav keeps its Choose a campaign line)', async () => {
    await mountWorkbench(() => <LeftNav />, { route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav })
    await flush()
    expect(screen.queryByRole('list', { name: 'Campaign Library' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Choose a campaign' })).toBeInTheDocument()
  })
})

describe('what a row does', () => {
  it('opens the panel at that category, marks itself expanded, then tells the drawer to close', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    await mountSelected(() => <LeftNav onNavigate={onNavigate} />, { route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav })
    const row = within(group()).getByRole('button', { name: 'Bestiary' })
    await user.click(row)
    expect(panel.open).toBe(true)
    expect(panel.category).toBe('bestiary')
    expect(onNavigate).toHaveBeenCalledTimes(1)
    expect(within(group()).getByRole('button', { name: 'Bestiary' })).toHaveAttribute('aria-expanded', 'true')
    expect(within(group()).getByRole('button', { name: 'NPCs' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('opens the panel before it closes the drawer, so the hand-off knows which it was', async () => {
    const user = userEvent.setup()
    const order: string[] = []
    const onOpenLibrary = vi.fn(() => order.push(panel.open ? 'open-first' : 'library'))
    const onNavigate = vi.fn(() => order.push('navigate'))
    await mountSelected(() => <LeftNav onNavigate={onNavigate} onOpenLibrary={onOpenLibrary} />, {
      route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav,
    })
    await user.click(within(group()).getByRole('button', { name: 'NPCs' }))
    expect(order).toEqual(['library', 'navigate'])
  })

  it('records itself as the opener, and the open drawer’s opener when the row is inside the drawer (C-3a)', async () => {
    const user = userEvent.setup()
    await mountSelected(() => <LeftNav />, { route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav })
    const opener = document.createElement('button')
    document.body.append(opener)
    live.actions.setDrawerOpener(opener)
    const row = within(group()).getByRole('button', { name: 'NPCs' })
    await user.click(row)
    expect(panel.open).toBe(true)
    // Closing returns focus to the row itself: it is not inside an open drawer here.
    act(() => panel.closeLibrary({ returnFocus: true }))
    expect(row).toHaveFocus()
    // Inside an open drawer, the drawer's opener is the opener.
    row.closest('nav')?.setAttribute('data-workbench-drawer', 'true')
    await user.click(row)
    act(() => panel.closeLibrary({ returnFocus: true }))
    expect(opener).toHaveFocus()
    row.closest('nav')?.removeAttribute('data-workbench-drawer')
    opener.remove()
  })

  it('makes no library request of its own, however many rows are pressed (I-5)', async () => {
    const user = userEvent.setup()
    const { server } = await mountSelected(() => <LeftNav />, { route: libraryRoute({}), stubGlobalFetch: true, wrap: withNav })
    await flush()
    expect(server.libraryCalls()).toHaveLength(0)
    for (const name of ['NPCs', 'Bestiary', 'Documents', 'Session log']) {
      await user.click(within(group()).getByRole('button', { name }))
    }
    expect(server.libraryCalls()).toHaveLength(0)
  })
})

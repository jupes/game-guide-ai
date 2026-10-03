/**
 * libraryPanel.test.tsx -- the Campaign Library panel's open state
 * (agent-forge-harness-1kg.6.4, L-4; LIB-9, LIB-25, INFERRED I-4 and I-7).
 *
 * The REAL campaign, canvas and app-nav providers: the scope key, the channel and the
 * role behave as they do in the app. Focus is checked on real buttons.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import {
  campaignFixture, defaultWorkbenchRoute, flush, live, mountSelected, mountWorkbench, run, type Route,
} from '../testing/workbenchHarness'
import { LIBRARY_TABS, LibraryPanelProvider, useLibraryPanel } from './libraryPanel'

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

/** An opener, a rail button and a main, with the opener removable to test the fallback chain. */
function Surface({ withOpener = true }: { withOpener?: boolean }): React.JSX.Element {
  const rail = React.useRef<HTMLButtonElement>(null)
  const main = React.useRef<HTMLElement>(null)
  return (
    <LibraryPanelProvider fallback={{ rail, main }}>
      <Probe />
      {withOpener && <button type="button">Opener</button>}
      <button ref={rail} type="button">Rail</button>
      <main ref={main} tabIndex={-1}>Main</main>
    </LibraryPanelProvider>
  )
}

const opener = (): HTMLElement => screen.getByRole('button', { name: 'Opener' })

describe('open, toggle, close and the tab', () => {
  it('starts closed on NPCs, and has exactly the four document tabs (never cues)', async () => {
    await mountSelected(() => <Surface />)
    expect(panel.open).toBe(false)
    expect(panel.category).toBe('npcs')
    expect(LIBRARY_TABS).toEqual(['npcs', 'bestiary', 'documents', 'session-log'])
  })

  it('opens at a category, switches tab while open, and closes', async () => {
    await mountSelected(() => <Surface />)
    act(() => panel.openLibrary('bestiary', opener()))
    expect(panel.open).toBe(true)
    expect(panel.category).toBe('bestiary')
    act(() => panel.setCategory('documents'))
    expect(panel.category).toBe('documents')
    act(() => panel.openLibrary('session-log', opener()))
    expect(panel.category).toBe('session-log')
    act(() => panel.closeLibrary({ returnFocus: false }))
    expect(panel.open).toBe(false)
  })

  it('toggle closes an open panel, and reopens at the last category (the first time, NPCs)', async () => {
    await mountSelected(() => <Surface />)
    act(() => panel.toggleLibrary(opener()))
    expect(panel.open).toBe(true)
    expect(panel.category).toBe('npcs')
    act(() => panel.setCategory('documents'))
    act(() => panel.toggleLibrary(opener()))
    expect(panel.open).toBe(false)
    act(() => panel.toggleLibrary(opener()))
    expect(panel.open).toBe(true)
    expect(panel.category).toBe('documents')
  })
})

describe('whose panel it is (LIB-25, I-7)', () => {
  it('a campaign switch closes it, and switching back does not reopen it', async () => {
    await mountSelected(() => <Surface />)
    act(() => panel.openLibrary('npcs', opener()))
    expect(panel.open).toBe(true)
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    expect(panel.open).toBe(false)
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_A')))
    expect(panel.open).toBe(false)
  })

  it('a switch to a campaign already in the list (no restoring state in between) closes it on the scope key alone', async () => {
    const route: Route = (call) =>
      call.url === '/campaigns'
        ? { status: 200, body: { schema_version: 1, items: [campaignFixture('cmp_A'), campaignFixture('cmp_B')], next_cursor: null } }
        : defaultWorkbenchRoute(call)
    await mountSelected(() => <Surface />, { route })
    act(() => live.campaign.loadCampaigns())
    await waitFor(() => expect(live.campaign.list.kind).toBe('ready'))
    act(() => panel.openLibrary('npcs', opener()))
    expect(panel.open).toBe(true)
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    expect(live.active).toBe(true)
    expect(live.campaign.scope?.campaignId).toBe('cmp_B')
    expect(panel.open).toBe(false)
  })

  it('leaving the GM channel closes it, and returning does not reopen it', async () => {
    await mountSelected(() => <Surface />)
    act(() => panel.openLibrary('npcs', opener()))
    expect(panel.open).toBe(true)
    act(() => live.nav.setMode('sage'))
    expect(panel.open).toBe(false)
    act(() => live.nav.setMode('gm'))
    expect(panel.open).toBe(false)
    // Positive control: it does open again when asked.
    act(() => panel.openLibrary('npcs', opener()))
    expect(panel.open).toBe(true)
  })

  it('does not open for a player, or before a campaign is selected', async () => {
    await mountWorkbench(() => <Surface />, {
      role: 'player', hash: '#campaign=cmp_A', restore: { campaignId: 'cmp_A', conversationId: null },
    })
    await flush()
    act(() => panel.openLibrary('npcs', opener()))
    expect(panel.open).toBe(false)
  })

  it('does not open in a GM channel with no campaign chosen', async () => {
    await mountWorkbench(() => <Surface />)
    await flush()
    act(() => panel.openLibrary('npcs', opener()))
    expect(panel.open).toBe(false)
  })

  it('a clear of the campaign closes it', async () => {
    await mountSelected(() => <Surface />)
    act(() => panel.openLibrary('npcs', opener()))
    await run(() => live.campaign.clearCampaign())
    expect(panel.open).toBe(false)
  })
})

describe('where focus goes back to', () => {
  it('the opener, on a close that asks for it', async () => {
    await mountSelected(() => <Surface />)
    act(() => opener().focus())
    act(() => panel.openLibrary('npcs', opener()))
    act(() => screen.getByRole('button', { name: 'Rail' }).focus())
    act(() => panel.closeLibrary({ returnFocus: true }))
    expect(opener()).toHaveFocus()
  })

  it('nowhere, on a close that does not (a pointer press elsewhere)', async () => {
    await mountSelected(() => <Surface />)
    act(() => panel.openLibrary('npcs', opener()))
    act(() => screen.getByRole('button', { name: 'Rail' }).focus())
    act(() => panel.closeLibrary({ returnFocus: false }))
    expect(screen.getByRole('button', { name: 'Rail' })).toHaveFocus()
  })

  it('nowhere, when a campaign switch closed it', async () => {
    await mountSelected(() => <Surface />)
    act(() => panel.openLibrary('npcs', opener()))
    act(() => screen.getByRole('button', { name: 'Rail' }).focus())
    await run(() => live.campaign.selectCampaign(campaignFixture('cmp_B')))
    expect(screen.getByRole('button', { name: 'Rail' })).toHaveFocus()
  })

  it('the rail button when the opener is gone, then <main> when the rail is gone too', async () => {
    function Gone(): React.JSX.Element {
      const [state, setState] = React.useState({ opener: true, rail: true })
      const rail = React.useRef<HTMLButtonElement>(null)
      const main = React.useRef<HTMLElement>(null)
      return (
        <LibraryPanelProvider fallback={{ rail, main }}>
          <Probe />
          {state.opener && <button type="button">Opener</button>}
          {state.rail && <button ref={rail} type="button">Rail</button>}
          <main ref={main} tabIndex={-1}>Main</main>
          <button type="button" onClick={() => setState({ opener: false, rail: true })}>Remove opener</button>
          <button type="button" onClick={() => setState({ opener: false, rail: false })}>Remove both</button>
        </LibraryPanelProvider>
      )
    }
    const { view } = await mountSelected(() => <Gone />)
    act(() => panel.openLibrary('npcs', opener()))
    act(() => screen.getByRole('button', { name: 'Remove opener' }).click())
    act(() => panel.closeLibrary({ returnFocus: true }))
    expect(screen.getByRole('button', { name: 'Rail' })).toHaveFocus()

    act(() => panel.openLibrary('npcs', screen.getByRole('button', { name: 'Rail' })))
    act(() => screen.getByRole('button', { name: 'Remove both' }).click())
    act(() => panel.closeLibrary({ returnFocus: true }))
    expect(screen.getByRole('main')).toHaveFocus()
    view.unmount()
  })

  it('a hidden opener (display: none) falls back to the rail button', async () => {
    await mountSelected(() => <Surface />)
    const hidden = opener()
    Object.defineProperty(hidden, 'checkVisibility', { value: () => false })
    act(() => panel.openLibrary('npcs', hidden))
    act(() => panel.closeLibrary({ returnFocus: true }))
    expect(screen.getByRole('button', { name: 'Rail' })).toHaveFocus()
  })
})

describe('outside a provider (the inert value)', () => {
  it('is closed, and every action does nothing and does not throw', () => {
    render(<Probe />)
    expect(panel.open).toBe(false)
    expect(panel.category).toBe('npcs')
    expect(() => {
      panel.openLibrary('bestiary', null)
      panel.toggleLibrary(null)
      panel.setCategory('documents')
      panel.closeLibrary({ returnFocus: true })
    }).not.toThrow()
    expect(panel.open).toBe(false)
  })
})

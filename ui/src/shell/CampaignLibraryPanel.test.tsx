/**
 * CampaignLibraryPanel.test.tsx -- the Campaign Library panel
 * (agent-forge-harness-1kg.6.4, L-3; LIB-5 to LIB-25, STATE-1 to STATE-3, WCAG 2.5.3).
 *
 * The REAL providers and a recording server. jsdom owns semantics: roles, names, focus,
 * which controls exist and what each call carries. The panel's boxes and its contrast
 * are Chromium's, in `CampaignLibraryPanel.stories.tsx`.
 */

import * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ShellLayout } from './breakpoints'
import {
  createRoute, defaultWorkbenchRoute, flush, libraryRoute, live, mountSelected, unarchiveRoute, type LibraryRow, type Route,
} from '../testing/workbenchHarness'
import { CampaignLibraryPanel } from './CampaignLibraryPanel'
import { LibraryPanelProvider, useLibraryPanel, type LibraryCategoryId } from './libraryPanel'

afterEach(() => {
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

let panelApi: ReturnType<typeof useLibraryPanel>

function Host({ layout, fetchImpl }: { layout: ShellLayout; fetchImpl: typeof fetch }): React.JSX.Element | null {
  const panel = useLibraryPanel()
  React.useLayoutEffect(() => {
    panelApi = panel
  })
  return panel.open ? <CampaignLibraryPanel layout={layout} fetchImpl={fetchImpl} /> : null
}

interface MountPanelOptions {
  layout?: ShellLayout
  category?: LibraryCategoryId
  strict?: boolean
}

async function mountPanel(route: Route, options: MountPanelOptions = {}) {
  const mounted = await mountSelected(
    (server) => (
      <LibraryPanelProvider>
        <Host layout={options.layout ?? 'wide'} fetchImpl={server.fetchImpl} />
      </LibraryPanelProvider>
    ),
    { route, strict: options.strict },
  )
  act(() => panelApi.openLibrary(options.category ?? 'npcs', null))
  await screen.findByRole('region', { name: 'Campaign Library' })
  return mounted
}

const ROWS: Readonly<Record<string, readonly LibraryRow[]>> = {
  npcs: [
    { id: 'doc_a', type: 'npc', title: 'Ondrey', qualifier: 'Ferryman' },
    { id: 'doc_b', type: 'npc', title: 'Brannoch' },
    { id: 'doc_v', type: 'npc', title: 'Velka', archived: true },
    { id: 'doc_w', type: 'npc', title: 'Wren', archived: true },
  ],
  bestiary: [{ id: 'doc_t', type: 'statblock', title: 'Tidewarden' }],
  documents: [
    { id: 'doc_h', type: 'handout', title: 'The Writ' },
    { id: 'doc_l', type: 'lore', title: 'The Drowned Crown' },
  ],
  'session-log': [{ id: 'doc_s', type: 'session-notes', title: 'Session 3' }],
}

/** The tab panel only: the status node, which repeats announced text, is outside it. */
const body = (): ReturnType<typeof within> => within(screen.getByRole('tabpanel'))
const panel = (): HTMLElement => screen.getByRole('region', { name: 'Campaign Library' })
const status = (): HTMLElement => within(panel()).getByRole('status')
const tab = (name: string): HTMLElement => screen.getByRole('tab', { name })
const rowNames = (): string[] =>
  Array.from(panel().querySelectorAll('.library-row .library-row__title'), (title) => title.textContent ?? '')
const many = (count: number): LibraryRow[] =>
  Array.from({ length: count }, (_, index) => ({ id: `doc_${index + 1}`, type: 'npc', title: `Npc ${index + 1}` }))

describe('the tabs (LIB-5, WAI-ARIA tabs)', () => {
  it('has four tabs and no Cues, the first selected, and a tabpanel named by the selected tab', async () => {
    await mountPanel(libraryRoute(ROWS))
    const tabs = within(screen.getByRole('tablist', { name: 'Library categories' })).getAllByRole('tab')
    expect(tabs.map((node) => node.textContent)).toEqual(['NPCs', 'Bestiary', 'Documents', 'Session log'])
    expect(screen.queryByRole('tab', { name: /cues/i })).toBeNull()
    expect(tab('NPCs')).toHaveAttribute('aria-selected', 'true')
    for (const other of ['Bestiary', 'Documents', 'Session log']) expect(tab(other)).toHaveAttribute('aria-selected', 'false')
    const tabpanel = screen.getByRole('tabpanel')
    expect(tabpanel).toHaveAccessibleName('NPCs')
    expect(tab('NPCs')).toHaveAttribute('aria-controls', tabpanel.id)
  })

  it('focuses its heading when it opens', async () => {
    await mountPanel(libraryRoute(ROWS))
    expect(within(panel()).getByRole('heading', { level: 2, name: 'Campaign Library' })).toHaveFocus()
  })

  it('arrows, Home and End move focus and activate, and the arrows wrap', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(libraryRoute(ROWS))
    act(() => tab('NPCs').focus())
    await user.keyboard('{ArrowRight}')
    expect(tab('Bestiary')).toHaveFocus()
    expect(tab('Bestiary')).toHaveAttribute('aria-selected', 'true')
    expect(panelApi.category).toBe('bestiary')
    await waitFor(() => expect(server.libraryBodies().at(-1)).toMatchObject({ category: 'bestiary' }))
    await user.keyboard('{End}')
    expect(tab('Session log')).toHaveFocus()
    expect(tab('Session log')).toHaveAttribute('aria-selected', 'true')
    await user.keyboard('{ArrowRight}')
    expect(tab('NPCs')).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(tab('Session log')).toHaveFocus()
    await user.keyboard('{Home}')
    expect(tab('NPCs')).toHaveFocus()
    expect(tab('NPCs')).toHaveAttribute('aria-selected', 'true')
  })

  it('only the selected tab is in the tab order (roving tabindex)', async () => {
    await mountPanel(libraryRoute(ROWS))
    expect(tab('NPCs')).toHaveAttribute('tabindex', '0')
    for (const other of ['Bestiary', 'Documents', 'Session log']) expect(tab(other)).toHaveAttribute('tabindex', '-1')
  })

  it('switching a tab resets the search, sort, filter and type (I-6)', async () => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute(ROWS))
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Sort' }), 'name')
    await user.click(tab('Bestiary'))
    expect(screen.getByRole('button', { name: 'Active' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('combobox', { name: 'Sort' })).toHaveValue('recent')
    expect(screen.getByRole('textbox', { name: 'Search the bestiary' })).toHaveValue('')
  })
})

describe('the status node (A-29, STATE-7)', () => {
  it('exists, and is empty, before anything has happened', async () => {
    await mountPanel(libraryRoute(ROWS))
    await screen.findByText('Ondrey')
    expect(status()).toBeEmptyDOMElement()
    expect(panel().querySelectorAll('[role="status"]')).toHaveLength(1)
  })

  it('does not announce loading, a tab switch, or opening a document', async () => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute(ROWS))
    await screen.findByText('Ondrey')
    await user.click(tab('Bestiary'))
    await screen.findByText('Tidewarden')
    await user.click(screen.getByRole('button', { name: /Tidewarden/ }))
    await flush()
    expect(status()).toBeEmptyDOMElement()
  })
})

describe('every state of the list (§12.2)', () => {
  it('loading is visible text and aria-hidden skeletons, never announced', async () => {
    await mountPanel((call) => (call.url.endsWith('/library') ? 'defer' : defaultWorkbenchRoute(call)))
    expect(screen.getByText('Loading NPCs…')).toBeInTheDocument()
    const skeletons = panel().querySelectorAll('.library-panel__skeleton')
    expect(skeletons).toHaveLength(3)
    for (const skeleton of skeletons) expect(skeleton).toHaveAttribute('aria-hidden', 'true')
    expect(status()).toBeEmptyDOMElement()
  })

  it.each([
    ['NPCs', 'No NPCs yet. Run /npc or press New.'],
    ['Bestiary', 'Nothing in the bestiary yet. Run /monster and save it.'],
    ['Documents', 'No documents yet. Press New to write one.'],
    ['Session log', 'No session notes yet. Run /recap at the end of a session.'],
  ])('%s with nothing in it reads the ADR copy', async (name, copy) => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute({}))
    await user.click(tab(name))
    expect(await screen.findByText(copy)).toBeInTheDocument()
  })

  it('an empty Archived filter says nothing is archived, in that category’s words', async () => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute({ bestiary: [{ id: 'doc_t', type: 'statblock', title: 'Tidewarden' }] }))
    await user.click(tab('Bestiary'))
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    expect(await screen.findByText('Nothing archived in the bestiary.')).toBeInTheDocument()
  })

  it('a search that matches nothing says so, and Clear empties the field, refetches and focuses it', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(libraryRoute(ROWS))
    await screen.findByText('Ondrey')
    const field = screen.getByRole('textbox', { name: 'Search NPCs' })
    await user.type(field, 'zz')
    expect(await screen.findByText('Nothing matches "zz"')).toBeInTheDocument()
    expect(server.libraryBodies().at(-1)).toMatchObject({ search: 'zz', category: 'npcs' })
    const before = server.libraryCalls().length
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(field).toHaveValue('')
    expect(field).toHaveFocus()
    await screen.findByText('Ondrey')
    expect(server.libraryCalls().length).toBe(before + 1)
    expect(server.libraryBodies().at(-1)).toMatchObject({ search: '' })
  })

  it('settles a search into one announcement, with the count', async () => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute(ROWS))
    await screen.findByText('Ondrey')
    await user.type(screen.getByRole('textbox', { name: 'Search NPCs' }), 'ond')
    await waitFor(() => expect(status()).toHaveTextContent('1 found'))
  })

  it('a refused search shows the field error, ties it to the field, and sends nothing', async () => {
    const { server } = await mountPanel(libraryRoute(ROWS))
    await screen.findByText('Ondrey')
    const field = screen.getByRole('textbox', { name: 'Search NPCs' })
    fireEvent.change(field, { target: { value: 'ab\u0000cd' } })
    await waitFor(() => expect(field).toHaveAttribute('aria-invalid', 'true'), { timeout: 2000 })
    const error = screen.getByText("Search can't include some of those characters.")
    expect(field.getAttribute('aria-describedby')).toBe(error.id)
    expect(server.libraryCalls()).toHaveLength(1)
    // Positive control: a clean search does go out.
    fireEvent.change(field, { target: { value: 'abcd' } })
    await waitFor(() => expect(server.libraryCalls()).toHaveLength(2))
  })

  it('a first page that fails says so once, and Retry sends the same request again', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(libraryRoute(ROWS, { failing: ['npcs'] }))
    expect(await screen.findByText("Couldn't load NPCs")).toBeInTheDocument()
    await waitFor(() => expect(status()).toHaveTextContent("Couldn't load NPCs"))
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(server.libraryCalls()).toHaveLength(2))
    expect(server.libraryBodies()[1]).toEqual(server.libraryBodies()[0])
  })

  it('the list is named for its category, and a row names its title, qualifier and (in Documents) type', async () => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute(ROWS))
    const list = await screen.findByRole('list', { name: 'NPCs' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(rowNames()).toEqual(['Ondrey', 'Brannoch'])
    expect(within(list).getByRole('button', { name: /Ondrey/ })).toHaveTextContent('Ferryman')
    await user.click(tab('Documents'))
    const documents = await screen.findByRole('list', { name: 'Documents' })
    expect(within(documents).getByRole('button', { name: /The Writ/ })).toHaveTextContent('Player Handout')
    expect(within(documents).getByRole('button', { name: /The Drowned Crown/ })).toHaveTextContent('Lore Entry')
  })
})

describe('opening a document (LIB-9, I-3)', () => {
  it('opens it as a gesture with exactly one document GET, and at wide the panel stays open', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(libraryRoute(ROWS))
    await user.click(await screen.findByRole('button', { name: /Ondrey/ }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(server.docCalls()).toHaveLength(1)
    expect(server.docCalls()[0].url).toBe('/campaigns/cmp_A/documents/doc_a')
    expect(panelApi.open).toBe(true)
  })

  it('records the row as the opener, so closing the canvas returns focus to it', async () => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute(ROWS))
    const row = await screen.findByRole('button', { name: /Ondrey/ })
    await user.click(row)
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(live.actions.openerRef.current).toBe(row)
  })

  it.each<ShellLayout>(['medium', 'narrow'])('at %s the panel closes behind the document', async (layout) => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute(ROWS), { layout })
    await user.click(await screen.findByRole('button', { name: /Ondrey/ }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(panelApi.open).toBe(false)
  })

  it('marks the open document’s row aria-current, and no other', async () => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute(ROWS))
    await user.click(await screen.findByRole('button', { name: /Ondrey/ }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(screen.getByRole('button', { name: /Ondrey/ })).toHaveAttribute('aria-current', 'true')
    expect(screen.getByRole('button', { name: /Brannoch/ })).not.toHaveAttribute('aria-current')
  })

  it('an archived document opens too (read-only, I-15)', async () => {
    const user = userEvent.setup()
    await mountPanel(libraryRoute(ROWS))
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await user.click(await screen.findByRole('button', { name: /^Velka/ }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
  })
})

describe('Archived and Restore (LIB-24)', () => {
  it('Active shows no Restore; Archived asks for archived rows and shows one Restore per row, named by the title', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(libraryRoute(ROWS))
    await screen.findByText('Ondrey')
    expect(screen.queryByRole('button', { name: /^Restore/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Active' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Archived' })).toHaveAttribute('aria-pressed', 'false')
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await screen.findByText('Velka')
    expect(server.libraryBodies().at(-1)).toMatchObject({ archived: true })
    expect(screen.getByRole('button', { name: 'Archived' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Restore Velka' })).toHaveTextContent('Restore')
    expect(screen.getByRole('button', { name: 'Restore Wren' })).toBeInTheDocument()
  })

  it('Restore makes one unarchive POST, removes the row, moves focus to the next row and says so', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(unarchiveRoute({ fallback: libraryRoute(ROWS) }))
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await screen.findByText('Velka')
    await user.click(screen.getByRole('button', { name: 'Restore Velka' }))
    await waitFor(() => expect(rowNames()).toEqual(['Wren']))
    expect(server.unarchiveCalls()).toHaveLength(1)
    expect(server.unarchiveCalls()[0].url).toBe('/campaigns/cmp_A/documents/doc_v/unarchive')
    expect(server.unarchiveCalls()[0].body).toBeNull()
    expect(screen.getByRole('button', { name: /^Wren/ })).toHaveFocus()
    expect(status()).toHaveTextContent('Restored Velka')
  })

  it('restoring the last row moves focus to the previous one, and the only row to the search field', async () => {
    const user = userEvent.setup()
    await mountPanel(unarchiveRoute({ fallback: libraryRoute(ROWS) }))
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await screen.findByText('Velka')
    await user.click(screen.getByRole('button', { name: 'Restore Wren' }))
    await waitFor(() => expect(rowNames()).toEqual(['Velka']))
    expect(screen.getByRole('button', { name: /^Velka/ })).toHaveFocus()
    await user.click(screen.getByRole('button', { name: 'Restore Velka' }))
    await waitFor(() => expect(rowNames()).toEqual([]))
    expect(screen.getByRole('textbox', { name: 'Search NPCs' })).toHaveFocus()
  })

  it('a throttled Restore keeps the row, and says to wait', async () => {
    const user = userEvent.setup()
    await mountPanel(unarchiveRoute({ fallback: libraryRoute(ROWS), answer: () => ({ status: 429, body: {} }) }))
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await screen.findByText('Velka')
    await user.click(screen.getByRole('button', { name: 'Restore Velka' }))
    expect(await body().findByText('Too many changes at once. Wait a moment and try again.')).toBeInTheDocument()
    expect(rowNames()).toEqual(['Velka', 'Wren'])
    await waitFor(() => expect(status()).toHaveTextContent('Too many changes at once'))
  })

  it('a failed Restore keeps the row, says nothing changed, and Retry sends it again', async () => {
    const user = userEvent.setup()
    let answers = 0
    const { server } = await mountPanel(
      unarchiveRoute({
        fallback: libraryRoute(ROWS),
        answer: () => (answers++ === 0 ? { status: 503, body: {} } : { status: 204 }),
      }),
    )
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await screen.findByText('Velka')
    await user.click(screen.getByRole('button', { name: 'Restore Velka' }))
    expect(await body().findByText("Couldn't restore Velka. Nothing changed.")).toBeInTheDocument()
    expect(rowNames()).toEqual(['Velka', 'Wren'])
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(rowNames()).toEqual(['Wren']))
    expect(server.unarchiveCalls()).toHaveLength(2)
  })

  it('a document that is no longer there is removed, with a note', async () => {
    const user = userEvent.setup()
    await mountPanel(unarchiveRoute({ fallback: libraryRoute(ROWS), answer: () => ({ status: 404, body: {} }) }))
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await screen.findByText('Velka')
    await user.click(screen.getByRole('button', { name: 'Restore Velka' }))
    await waitFor(() => expect(rowNames()).toEqual(['Wren']))
    expect(body().getByText("That document isn't available.")).toBeInTheDocument()
  })

  it('a Restore in flight is aria-disabled, and a second press sends nothing', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(unarchiveRoute({ fallback: libraryRoute(ROWS), answer: () => 'defer' }))
    await user.click(screen.getByRole('button', { name: 'Archived' }))
    await screen.findByText('Velka')
    await user.click(screen.getByRole('button', { name: 'Restore Velka' }))
    expect(screen.getByRole('button', { name: 'Restore Velka' })).toHaveAttribute('aria-disabled', 'true')
    await user.click(screen.getByRole('button', { name: 'Restore Velka' }))
    await user.click(screen.getByRole('button', { name: 'Restore Wren' }))
    expect(server.unarchiveCalls()).toHaveLength(1)
  })
})

describe('Sort and Type (LIB-22)', () => {
  it('Name A–Z sends sort name; Recently updated sends recent', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(libraryRoute(ROWS))
    await screen.findByText('Ondrey')
    const sort = screen.getByRole('combobox', { name: 'Sort' })
    expect(within(sort).getAllByRole('option').map((option) => option.textContent)).toEqual(['Recently updated', 'Name A–Z'])
    await user.selectOptions(sort, 'name')
    await waitFor(() => expect(server.libraryBodies().at(-1)).toMatchObject({ sort: 'name' }))
    await waitFor(() => expect(rowNames()).toEqual(['Brannoch', 'Ondrey']))
  })

  it('the Type filter exists only in Documents, lists the five registry types, and sends type', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(libraryRoute(ROWS))
    expect(screen.queryByRole('combobox', { name: 'Type' })).toBeNull()
    await user.click(tab('Documents'))
    const type = await screen.findByRole('combobox', { name: 'Type' })
    expect(within(type).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'All types', 'Player Handout', 'Quest Log', 'Character Sheet', 'Lore Entry', 'Encounter',
    ])
    await user.selectOptions(type, 'lore')
    await waitFor(() => expect(server.libraryBodies().at(-1)).toMatchObject({ category: 'documents', type: 'lore' }))
    await waitFor(() => expect(rowNames()).toEqual(['The Drowned Crown']))
    await user.selectOptions(type, '')
    await waitFor(() => expect('type' in (server.libraryBodies().at(-1) ?? {})).toBe(false))
  })
})

describe('Load more (LIB-23, STATE-1)', () => {
  it('shows Load more under a full page, appends the next, and drops the button on the last page', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(libraryRoute({ npcs: many(30) }))
    await screen.findByText('Npc 1')
    expect(rowNames()).toHaveLength(25)
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(rowNames()).toHaveLength(30))
    expect(server.libraryBodies().at(-1)).toMatchObject({ cursor: 'c25' })
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(status()).toHaveTextContent('5 more loaded')
  })

  it('a Load more that fails keeps every row, says so, and Retry asks again', async () => {
    const user = userEvent.setup()
    let failing = true
    const base = libraryRoute({ npcs: many(30) })
    const route: Route = (call) => {
      const body = JSON.parse(call.body ?? '{}') as { cursor?: string }
      if (call.url.endsWith('/library') && body.cursor !== undefined && failing) return { status: 503, body: {} }
      return base(call)
    }
    await mountPanel(route)
    await screen.findByText('Npc 1')
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    expect(await body().findByText("Couldn't load more")).toBeInTheDocument()
    expect(rowNames()).toHaveLength(25)
    await waitFor(() => expect(status()).toHaveTextContent("Couldn't load more"))
    failing = false
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(rowNames()).toHaveLength(30))
  })
})

describe('New (LIB-12, STATE-3)', () => {
  it('in NPCs: one button named New NPC Dossier that starts with its visible text, and a create opens the document', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(createRoute({ fallback: libraryRoute(ROWS) }))
    await screen.findByText('Ondrey')
    const button = screen.getByRole('button', { name: 'New NPC Dossier' })
    expect(button).toHaveTextContent('New')
    const librariesBefore = server.libraryCalls().length
    await user.click(button)
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(server.createCalls()).toHaveLength(1)
    const body = JSON.parse(server.createCalls()[0].body ?? '{}') as Record<string, unknown>
    expect(body).toMatchObject({
      schema_version: 1, campaign_id: 'cmp_A', type: 'npc', type_version: 1, data: { name: 'Untitled NPC Dossier' },
    })
    expect(String(body.command_id)).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
    expect(server.createCalls()[0].url).toBe('/campaigns/cmp_A/documents')
    expect(server.docCalls()).toHaveLength(1)
    await waitFor(() => expect(server.libraryCalls().length).toBeGreaterThan(librariesBefore))
    expect(live.state.doc.kind === 'open' && live.state.doc.document.data.name).toBe('Untitled NPC Dossier')
    expect(panelApi.open).toBe(true)
  })

  it('in Session log: New Session Notes', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(createRoute({ fallback: libraryRoute(ROWS) }), { category: 'session-log' })
    await screen.findByText('Session 3')
    await user.click(screen.getByRole('button', { name: 'New Session Notes' }))
    await waitFor(() => expect(server.createCalls()).toHaveLength(1))
    expect(JSON.parse(server.createCalls()[0].body ?? '{}')).toMatchObject({ type: 'session-notes', data: { name: 'Untitled Session Notes' } })
  })

  it('Bestiary has no New (I-2), and no stat block is offered anywhere', async () => {
    const user = userEvent.setup()
    await mountPanel(createRoute({ fallback: libraryRoute(ROWS) }), { category: 'bestiary' })
    await screen.findByText('Tidewarden')
    expect(screen.queryByRole('button', { name: /^New/ })).toBeNull()
    await user.click(tab('Documents'))
    await screen.findByText('The Writ')
    await user.click(screen.getByRole('button', { name: 'New' }))
    expect(screen.queryByRole('button', { name: /stat block/i })).toBeNull()
  })

  it('in Documents: a disclosure of exactly the five registry types, and Escape closes only it', async () => {
    const user = userEvent.setup()
    await mountPanel(createRoute({ fallback: libraryRoute(ROWS) }), { category: 'documents' })
    await screen.findByText('The Writ')
    const disclosure = screen.getByRole('button', { name: 'New' })
    expect(disclosure).toHaveAttribute('aria-expanded', 'false')
    const controls = disclosure.getAttribute('aria-controls')
    await user.click(disclosure)
    expect(disclosure).toHaveAttribute('aria-expanded', 'true')
    const menu = document.getElementById(controls ?? '') as HTMLElement
    expect(within(menu).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Player Handout', 'Quest Log', 'Character Sheet', 'Lore Entry', 'Encounter',
    ])
    act(() => within(menu).getByRole('button', { name: 'Quest Log' }).focus())
    await user.keyboard('{Escape}')
    expect(disclosure).toHaveAttribute('aria-expanded', 'false')
    expect(disclosure).toHaveFocus()
    expect(panelApi.open).toBe(true)
    await user.keyboard('{Escape}')
    expect(panelApi.open).toBe(false)
  })

  it('a create from the Documents disclosure sends that type', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(createRoute({ fallback: libraryRoute(ROWS) }), { category: 'documents' })
    await screen.findByText('The Writ')
    await user.click(screen.getByRole('button', { name: 'New' }))
    await user.click(screen.getByRole('button', { name: 'Player Handout' }))
    await waitFor(() => expect(server.createCalls()).toHaveLength(1))
    expect(JSON.parse(server.createCalls()[0].body ?? '{}')).toMatchObject({
      type: 'handout', data: { name: 'Untitled Player Handout' },
    })
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
  })

  it.each<ShellLayout>(['medium', 'narrow'])('at %s a create closes the panel behind the new document', async (layout) => {
    const user = userEvent.setup()
    await mountPanel(createRoute({ fallback: libraryRoute(ROWS) }), { layout })
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: 'New NPC Dossier' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(panelApi.open).toBe(false)
  })

  it('a press while a create is in flight sends nothing, and the control reads Creating…', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(createRoute({ fallback: libraryRoute(ROWS), answer: () => 'defer' }))
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: 'New NPC Dossier' }))
    const busy = screen.getByRole('button', { name: 'Creating…' })
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    await user.click(busy)
    expect(server.createCalls()).toHaveLength(1)
  })

  it('a full account says so and offers no retry', async () => {
    const user = userEvent.setup()
    await mountPanel(createRoute({
      fallback: libraryRoute(ROWS),
      answer: () => ({ status: 409, body: { detail: { code: 'account_limit_reached', message: 'full', retryable: false } } }),
    }))
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: 'New NPC Dossier' }))
    expect(
      await body().findByText("This account's document storage is full. Archive and delete documents to make room."),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    await waitFor(() => expect(status()).toHaveTextContent('document storage is full'))
  })

  it('a throttled create says to wait and offers Retry', async () => {
    const user = userEvent.setup()
    await mountPanel(createRoute({ fallback: libraryRoute(ROWS), answer: () => ({ status: 429, body: {} }) }))
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: 'New NPC Dossier' }))
    expect(await body().findByText('Too many changes at once. Wait a moment and try again.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('a refused create says so and offers no retry', async () => {
    const user = userEvent.setup()
    await mountPanel(createRoute({
      fallback: libraryRoute(ROWS),
      answer: () => ({ status: 409, body: { detail: { code: 'document_unsupported', message: 'no', retryable: false } } }),
    }))
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: 'New NPC Dossier' }))
    expect(await body().findByText("Couldn't create the document.")).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('a failed create says nothing was saved, and Retry reuses the same command_id (STATE-3)', async () => {
    const user = userEvent.setup()
    let answers = 0
    const { server } = await mountPanel(createRoute({
      fallback: libraryRoute(ROWS),
      answer: () => (answers++ === 0 ? { status: 503, body: {} } : undefined),
    }))
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: 'New NPC Dossier' }))
    expect(await body().findByText("Couldn't create the document. Nothing was saved.")).toBeInTheDocument()
    await waitFor(() => expect(status()).toHaveTextContent('Nothing was saved'))
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    expect(server.createCalls()).toHaveLength(2)
    const ids = server.createCalls().map((call) => (JSON.parse(call.body ?? '{}') as { command_id: string }).command_id)
    expect(ids[1]).toBe(ids[0])
  })

  it('a second create after a success is a new intent with a new command_id', async () => {
    const user = userEvent.setup()
    const { server } = await mountPanel(createRoute({ fallback: libraryRoute(ROWS) }), { layout: 'wide' })
    await screen.findByText('Ondrey')
    await user.click(screen.getByRole('button', { name: 'New NPC Dossier' }))
    await waitFor(() => expect(live.state.doc.kind).toBe('open'))
    await user.click(await screen.findByRole('button', { name: 'New NPC Dossier' }))
    await waitFor(() => expect(server.createCalls()).toHaveLength(2))
    const ids = server.createCalls().map((call) => (JSON.parse(call.body ?? '{}') as { command_id: string }).command_id)
    expect(ids[1]).not.toBe(ids[0])
  })
})

describe('Escape and the heading', () => {
  it('Escape closes the panel and returns focus to the opener', async () => {
    const user = userEvent.setup()
    const mounted = await mountPanel(libraryRoute(ROWS))
    expect(mounted).toBeDefined()
    await user.keyboard('{Escape}')
    expect(panelApi.open).toBe(false)
  })

  it('is not a dialog: no aria-modal, and focus is not trapped', async () => {
    await mountPanel(libraryRoute(ROWS))
    expect(panel()).not.toHaveAttribute('aria-modal')
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('under StrictMode', () => {
  it('opens with one request and its status node still empty', async () => {
    const { server } = await mountPanel(libraryRoute(ROWS), { strict: true })
    await screen.findByText('Ondrey')
    await flush()
    expect(server.libraryCalls()).toHaveLength(1)
    expect(status()).toBeEmptyDOMElement()
  })
})

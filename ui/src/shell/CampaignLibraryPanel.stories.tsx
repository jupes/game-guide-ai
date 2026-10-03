/**
 * CampaignLibraryPanel -- the Campaign Library (agent-forge-harness-1kg.6.4, S-1).
 *
 * Every state of the list, in a real browser, at the wide layout (a 320 px column) and at
 * 375 px (the full-screen panel with Back), with one dark variant each. The REAL campaign,
 * canvas and library-panel providers; only the network is stubbed. Every story passes axe
 * (`test: 'error'`) and measures the 44 px floor of the tabs, rows, chips, selects and New.
 */
import * as React from 'react'
import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, fireEvent, userEvent, waitFor, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport, type ViewportName } from '../../.storybook/viewports'
import type { ShellLayout } from './breakpoints'
import { CampaignLibraryPanel } from './CampaignLibraryPanel'
import { CampaignProvider } from './campaignContext'
import { CanvasProvider, useWorkbenchActive } from './canvasContext'
import { LibraryPanelProvider, useLibraryPanel, type LibraryCategoryId } from './libraryPanel'

const CAMPAIGN_ID = 'cmp_StoryCampaign0000000001'

const CAMPAIGN = {
  schema_version: 1, campaign_id: CAMPAIGN_ID, name: 'The Drowned Crown', created_at: '2026-09-16T19:20:11Z',
  updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
  avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0, last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null, dormant: false,
}

interface Row {
  id: string
  type: string
  title: string
  qualifier?: string
  archived?: boolean
}

const ROWS: Readonly<Record<string, readonly Row[]>> = {
  npcs: [
    { id: 'doc_ondrey', type: 'npc', title: 'Sister Ondrey Vashe', qualifier: 'Harbour priestess' },
    { id: 'doc_brannoch', type: 'npc', title: 'Brannoch the Drowned, Warden of the Long Tide' },
    { id: 'doc_velka', type: 'npc', title: 'Velka', archived: true },
  ],
  bestiary: [{ id: 'doc_tidewarden', type: 'statblock', title: 'Tidewarden' }],
  documents: [
    { id: 'doc_writ', type: 'handout', title: 'The Writ of Passage' },
    { id: 'doc_crown', type: 'lore', title: 'The Drowned Crown' },
  ],
  'session-log': [{ id: 'doc_s3', type: 'session-notes', title: 'Session 3' }],
}

interface LibraryRequest {
  category: string
  search: string
  archived: boolean
  cursor?: string
}

type Library = (request: LibraryRequest) => Response | Promise<Response>

function page(category: string, rows: readonly Row[], next: string | null = null): Response {
  return json({
    schema_version: 1, campaign_id: CAMPAIGN_ID, category, next_cursor: next,
    items: rows.map((row) => ({
      document_id: row.id, type: row.type, title: row.title, qualifier: row.qualifier ?? '', tags: [],
      archived: row.archived ?? false, updated_at: '2026-09-16T19:40:00Z',
    })),
  })
}

const ready: Library = ({ category, archived, search }) =>
  page(
    category,
    (ROWS[category] ?? []).filter(
      (row) => (row.archived ?? false) === archived && row.title.toLowerCase().includes(search.toLowerCase()),
    ),
  )

const empty: Library = ({ category }) => page(category, [])

const manyRows = (count: number): Row[] =>
  Array.from({ length: count }, (_, index) => ({ id: `doc_n${index + 1}`, type: 'npc', title: `Npc ${index + 1}` }))

function api(library: Library, create: () => Response = () => json({ detail: 'unrouted' }, 404)) {
  return stubFetch((url, init) => {
    if (url === `/campaigns/${CAMPAIGN_ID}`) return json(CAMPAIGN)
    if (url === `/campaigns/${CAMPAIGN_ID}/library`) return library(JSON.parse(String(init?.body ?? '{}')) as LibraryRequest)
    if (url === `/campaigns/${CAMPAIGN_ID}/documents` && init?.method === 'POST') return create()
    if (init?.method === 'POST' && /\/documents\/doc_\w+\/(archive|unarchive|delete)$/.test(url)) return new Response(null, { status: 204 })
    return json({ detail: `unrouted: ${url}` }, 404)
  })
}

/** Opens the panel at `category` the first time the Workbench is active, as a rail icon or a nav row would. */
function OpenAt({ category, layout }: { category: LibraryCategoryId; layout: ShellLayout }): React.JSX.Element | null {
  const panel = useLibraryPanel()
  const active = useWorkbenchActive()
  const opened = React.useRef(false)
  const { openLibrary } = panel
  React.useEffect(() => {
    if (!active || opened.current) return
    opened.current = true
    openLibrary(category, null)
  }, [active, category, openLibrary])
  return panel.open ? (
    <div className="story-library-host" data-layout={layout}>
      <CampaignLibraryPanel layout={layout} />
    </div>
  ) : null
}

const meta = {
  title: 'Shell/CampaignLibraryPanel',
  component: CampaignLibraryPanel,
  parameters: { layout: 'fullscreen' },
  decorators: [withShell({ mode: 'gm', role: 'dm' })],
} satisfies Meta<typeof CampaignLibraryPanel>

export default meta
type Story = StoryObj<typeof meta>

interface Scenario {
  library: Library
  category?: LibraryCategoryId
  create?: () => Response
  play: (canvas: ReturnType<typeof within>) => Promise<void>
}

function scenario({ library, category = 'npcs', create, play }: Scenario): Story {
  return {
    args: { layout: 'wide' },
    beforeEach: api(library, create),
    render: (args) => (
      <CampaignProvider restore={{ campaignId: CAMPAIGN_ID, conversationId: null, documentId: null }}>
        <CanvasProvider>
          <LibraryPanelProvider>
            {/* The wide layout's column is 320 px; narrow is the whole screen (the shell's CSS owns both). */}
            <div
              style={{
                width: args.layout === 'narrow' ? '100%' : '320px',
                height: args.layout === 'narrow' ? '100dvh' : '640px',
                display: 'flex',
                background: 'var(--md-sys-color-surface)',
              }}
            >
              <style>{'.story-library-host{display:flex;flex:1;min-width:0;min-height:0}.story-library-host .library-panel{width:100%;max-width:none}'}</style>
              <OpenAt category={category} layout={args.layout} />
            </div>
          </LibraryPanelProvider>
        </CanvasProvider>
      </CampaignProvider>
    ),
    ...atViewport('wide1280'),
    play: async ({ canvasElement, args }) => {
      await expectViewport(args.layout === 'narrow' ? 'phone375' : 'wide1280')
      const canvas = within(canvasElement)
      await expect(await canvas.findByRole('region', { name: 'Campaign Library' })).toBeVisible()
      await expectTouchTargets(canvas, ['NPCs', 'Bestiary', 'Documents', 'Session log'], 'tab')
      await expectTouchTargets(canvas, ['Active', 'Archived'])
      await expectTouchTargets(canvas, ['Sort'], 'combobox')
      await play(canvas)
    },
  }
}

function asPhone(story: Story): Story {
  return {
    ...story,
    args: { layout: 'narrow' },
    ...atViewport('phone375'),
    play: async (context) => {
      await story.play?.(context)
      await expectNoPageOverflow()
    },
  }
}

function asDark(story: Story, name: ViewportName = 'wide1280'): Story {
  return {
    ...story,
    ...atViewport(name, 'dark'),
    play: async (context) => {
      await expectTheme('dark')
      await story.play?.(context)
    },
  }
}

const rowsAre = async (canvas: ReturnType<typeof within>, ...names: Array<string | RegExp>): Promise<void> => {
  for (const name of names) await expect(await canvas.findByRole('button', { name })).toBeVisible()
}

// ── The states ───────────────────────────────────────────────────────────────

export const Loading = scenario({
  library: () => pending(),
  play: async (canvas) => {
    await expect(await canvas.findByText('Loading NPCs…')).toBeVisible()
  },
})
export const LoadingPhone = asPhone(Loading)
export const LoadingDark = asDark(Loading)

export const EmptyNpcs = scenario({
  library: empty,
  play: async (canvas) => {
    await expect(await canvas.findByText('No NPCs yet. Run /npc or press New.')).toBeVisible()
    await expectTouchTargets(canvas, ['New NPC Dossier'])
  },
})
export const EmptyNpcsPhone = asPhone(EmptyNpcs)
export const EmptyNpcsDark = asDark(EmptyNpcs)

export const EmptyBestiary = scenario({
  library: empty,
  category: 'bestiary',
  play: async (canvas) => {
    await expect(await canvas.findByText('Nothing in the bestiary yet. Run /monster and save it, or press New.')).toBeVisible()
    await expectTouchTargets(canvas, ['New Stat Block'])
  },
})
export const EmptyBestiaryPhone = asPhone(EmptyBestiary)
export const EmptyBestiaryDark = asDark(EmptyBestiary)

export const EmptyDocuments = scenario({
  library: empty,
  category: 'documents',
  play: async (canvas) => {
    await expect(await canvas.findByText('No documents yet. Press New to write one.')).toBeVisible()
    await expectTouchTargets(canvas, ['New'])
    await expectTouchTargets(canvas, ['Type'], 'combobox')
  },
})
export const EmptyDocumentsPhone = asPhone(EmptyDocuments)
export const EmptyDocumentsDark = asDark(EmptyDocuments)

export const EmptySessionLog = scenario({
  library: empty,
  category: 'session-log',
  play: async (canvas) => {
    await expect(await canvas.findByText('No session notes yet. Run /recap at the end of a session.')).toBeVisible()
    await expectTouchTargets(canvas, ['New Session Notes'])
  },
})
export const EmptySessionLogPhone = asPhone(EmptySessionLog)
export const EmptySessionLogDark = asDark(EmptySessionLog)

export const ArchivedEmpty = scenario({
  library: ({ category, archived }) => page(category, archived ? [] : (ROWS[category] ?? []).filter((row) => row.archived !== true)),
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Archived' }))
    await expect(await canvas.findByText('Nothing archived in NPCs.')).toBeVisible()
  },
})
export const ArchivedEmptyPhone = asPhone(ArchivedEmpty)
export const ArchivedEmptyDark = asDark(ArchivedEmpty)

export const Error = scenario({
  library: () => json({}, 503),
  play: async (canvas) => {
    await expect(await canvas.findByText("Couldn't load NPCs")).toBeVisible()
    await expectTouchTargets(canvas, ['Retry'])
  },
})
export const ErrorPhone = asPhone(Error)
export const ErrorDark = asDark(Error)

export const Ready = scenario({
  library: ready,
  play: async (canvas) => {
    await rowsAre(canvas, /^Sister Ondrey Vashe/, /^Brannoch the Drowned/)
    await expect(canvas.getByRole('list', { name: 'NPCs' })).toBeVisible()
    await expect(canvas.getAllByRole('listitem')).toHaveLength(2)
    await expectTouchTargets(canvas, [/^Sister Ondrey Vashe/, /^Brannoch the Drowned/, 'New NPC Dossier'])
  },
})
export const ReadyPhone = asPhone(Ready)
export const ReadyDark = asDark(Ready)

export const SearchMiss = scenario({
  library: ready,
  play: async (canvas) => {
    await userEvent.type(await canvas.findByRole('textbox', { name: 'Search NPCs' }), 'zz')
    await expect(await canvas.findByText('Nothing matches "zz"')).toBeVisible()
    await expectTouchTargets(canvas, ['Clear'])
  },
})
export const SearchMissPhone = asPhone(SearchMiss)
export const SearchMissDark = asDark(SearchMiss)

export const InvalidSearch = scenario({
  library: ready,
  play: async (canvas) => {
    const field = await canvas.findByRole('textbox', { name: 'Search NPCs' })
    fireEvent.change(field, { target: { value: 'ab\u0000cd' } })
    await waitFor(() => expect(field).toHaveAttribute('aria-invalid', 'true'), { timeout: 2000 })
    await expect(canvas.getByText("Search can't include some of those characters.")).toBeVisible()
  },
})
export const InvalidSearchPhone = asPhone(InvalidSearch)
export const InvalidSearchDark = asDark(InvalidSearch)

export const Archived = scenario({
  library: ready,
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Archived' }))
    await expect(await canvas.findByRole('button', { name: 'Restore Velka' })).toBeVisible()
    await expectTouchTargets(canvas, ['Restore Velka', /^Velka/])
  },
})
export const ArchivedPhone = asPhone(Archived)
export const ArchivedDark = asDark(Archived)

export const DocumentsNewOpen = scenario({
  library: ready,
  category: 'documents',
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'New' }))
    await expect(await canvas.findByRole('button', { name: 'Player Handout' })).toBeVisible()
    await expect(canvas.getByRole('button', { name: 'New' })).toHaveAttribute('aria-expanded', 'true')
    await expectTouchTargets(canvas, ['Player Handout', 'Quest Log', 'Character Sheet', 'Lore Entry', 'Encounter'])
  },
})
export const DocumentsNewOpenPhone = asPhone(DocumentsNewOpen)
export const DocumentsNewOpenDark = asDark(DocumentsNewOpen)

export const CreateFailed = scenario({
  library: ready,
  create: () => json({}, 503),
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'New NPC Dossier' }))
    // The note, not the status node that repeats it for assistive technology.
    await expect(await within(canvas.getByRole('tabpanel')).findByText("Couldn't create the document. Nothing was saved.")).toBeVisible()
    await expectTouchTargets(canvas, ['Retry'])
  },
})
export const CreateFailedPhone = asPhone(CreateFailed)
export const CreateFailedDark = asDark(CreateFailed)

export const LoadMoreFailed = scenario({
  library: ({ category, cursor }) =>
    cursor === undefined ? page(category, manyRows(25), 'c25') : json({}, 503),
  play: async (canvas) => {
    await expect(await canvas.findByRole('button', { name: /^Npc 1$/ })).toBeVisible()
    await userEvent.click(await canvas.findByRole('button', { name: 'Load more' }))
    await expect(await within(canvas.getByRole('tabpanel')).findByText("Couldn't load more")).toBeVisible()
    await expectTouchTargets(canvas, ['Retry'])
  },
})
export const LoadMoreFailedPhone = asPhone(LoadMoreFailed)
export const LoadMoreFailedDark = asDark(LoadMoreFailed)

// ── The lifecycle (PR-2) ─────────────────────────────────────────────────────

export const RowOverflowOpen = scenario({
  library: ready,
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'More actions for Sister Ondrey Vashe' }))
    await expect(await canvas.findByRole('button', { name: 'Archive Sister Ondrey Vashe' })).toBeVisible()
    await expectTouchTargets(canvas, ['More actions for Sister Ondrey Vashe', 'Archive Sister Ondrey Vashe'])
  },
})
export const RowOverflowOpenPhone = asPhone(RowOverflowOpen)
export const RowOverflowOpenDark = asDark(RowOverflowOpen)

export const UndoToast = scenario({
  library: ready,
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'More actions for Sister Ondrey Vashe' }))
    await userEvent.click(await canvas.findByRole('button', { name: 'Archive Sister Ondrey Vashe' }))
    const toast = await canvas.findByRole('group', { name: 'Undo archive' })
    await expect(toast).toBeVisible()
    await expect(within(toast).getByText('Archived Sister Ondrey Vashe')).toBeVisible()
    await expectTouchTargets(within(toast), ['Undo'])
  },
})
export const UndoToastPhone = asPhone(UndoToast)
export const UndoToastDark = asDark(UndoToast)

export const DeleteDialog = scenario({
  library: ready,
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Archived' }))
    await userEvent.click(await canvas.findByRole('button', { name: 'More actions for Velka' }))
    await userEvent.click(await canvas.findByRole('button', { name: 'Delete Velka' }))
    const dialog = await canvas.findByRole('dialog', { name: 'Delete Velka?' })
    await expect(dialog).toBeVisible()
    await expect(within(dialog).getByText("This permanently deletes Velka and its whole history. This can't be undone.")).toBeVisible()
    await expect(within(dialog).getByLabelText('Your password')).toHaveFocus()
    await expectTouchTargets(within(dialog), ['Cancel', 'Delete'])
  },
})
export const DeleteDialogPhone = asPhone(DeleteDialog)
export const DeleteDialogDark = asDark(DeleteDialog)

export const StatBlockDialog = scenario({
  library: ready,
  category: 'bestiary',
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'New Stat Block' }))
    const dialog = await canvas.findByRole('dialog', { name: 'New stat block' })
    await expect(dialog).toBeVisible()
    await expect(within(dialog).getByLabelText('Name')).toHaveFocus()
    await expectTouchTargets(within(dialog), ['Cancel', 'Create'])
  },
})
export const StatBlockDialogPhone = asPhone(StatBlockDialog)
export const StatBlockDialogDark = asDark(StatBlockDialog)

export const StatBlockDialogInvalid = scenario({
  library: ready,
  category: 'bestiary',
  play: async (canvas) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'New Stat Block' }))
    const dialog = await canvas.findByRole('dialog', { name: 'New stat block' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }))
    await expect(await within(dialog).findByText('Give the stat block a name.')).toBeVisible()
    await expect(within(dialog).getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true')
  },
})
export const StatBlockDialogInvalidDark = asDark(StatBlockDialogInvalid)

// ── The keyboard ─────────────────────────────────────────────────────────────

/** Arrows walk the tabs and activate them; the heading was focused on open. */
export const TabsByKeyboard: Story = {
  ...Ready,
  play: async (context) => {
    const canvas = within(context.canvasElement)
    await expect(await canvas.findByRole('heading', { level: 2, name: 'Campaign Library' })).toHaveFocus()
    // The heading is a programmatic target; the next stops are Close, then the tab in the tab order.
    await userEvent.tab()
    await expect(canvas.getByRole('button', { name: 'Close library' })).toHaveFocus()
    await userEvent.tab()
    await expect(canvas.getByRole('tab', { name: 'NPCs' })).toHaveFocus()
    await userEvent.keyboard('{ArrowRight}')
    await expect(canvas.getByRole('tab', { name: 'Bestiary' })).toHaveFocus()
    await expect(canvas.getByRole('tab', { name: 'Bestiary' })).toHaveAttribute('aria-selected', 'true')
    await expect(await canvas.findByText('Tidewarden')).toBeVisible()
    await userEvent.keyboard('{End}')
    await expect(canvas.getByRole('tab', { name: 'Session log' })).toHaveFocus()
    const tab = canvas.getByRole('tab', { name: 'Session log' })
    await expect(parseFloat(getComputedStyle(tab).outlineWidth)).toBeGreaterThanOrEqual(2)
  },
}
export const TabsByKeyboardDark = asDark(TabsByKeyboard)

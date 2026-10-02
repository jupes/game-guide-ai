/**
 * CampaignDocumentsNav -- "Campaign documents" in the nav column
 * (agent-forge-harness-1kg.6.3, T-10; brief 2.7): loading, empty, error, ready, and
 * the open document marked current, in both themes.
 *
 * The REAL campaign and canvas providers; only the network is stubbed. Every story
 * passes axe (`test: 'error'`) and measures the 44 px floor of the rows and Retry.
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, waitFor, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { expectTheme } from '../../.storybook/viewports'
import { DOCUMENT_FIXTURES } from '../gm/documentFixtures'
import { CampaignDocumentsNav } from './CampaignDocumentsNav'
import { CampaignProvider } from './campaignContext'
import { CanvasProvider } from './canvasContext'

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
  at: string
}

const ROWS: Readonly<Record<string, readonly Row[]>> = {
  npcs: [
    { id: 'doc_ondrey', type: 'npc', title: 'Sister Ondrey Vashe', at: '2026-09-16T19:40:00Z' },
    { id: 'doc_brannoch', type: 'npc', title: 'Brannoch the Drowned, Warden of the Long Tide', at: '2026-09-16T19:10:00Z' },
  ],
  bestiary: [{ id: 'doc_tidewarden', type: 'statblock', title: 'Tidewarden', at: '2026-09-16T19:30:00Z' }],
  documents: [{ id: 'doc_writ', type: 'handout', title: 'The Writ of Passage', at: '2026-09-16T19:20:00Z' }],
  'session-log': [{ id: 'doc_s3', type: 'session-notes', title: 'Session 3', at: '2026-09-16T19:05:00Z' }],
}

type Library = (category: string) => Response | Promise<Response>

function page(category: string, rows: readonly Row[]): Response {
  return json({
    schema_version: 1, campaign_id: CAMPAIGN_ID, category, next_cursor: null,
    items: rows.map((row) => ({
      document_id: row.id, type: row.type, title: row.title, qualifier: '', tags: [], archived: false, updated_at: row.at,
    })),
  })
}

const ready: Library = (category) => page(category, ROWS[category] ?? [])

/** No portrait: the asset route is not stubbed, and an image that cannot load is noise, not a finding. */
const NPC_FIELDS = Object.fromEntries(
  Object.entries(DOCUMENT_FIXTURES.npc.data as Record<string, unknown>).filter(([key]) => key !== 'portrait'),
)

function api(library: Library) {
  return stubFetch((url, init) => {
    if (url === `/campaigns/${CAMPAIGN_ID}`) return json(CAMPAIGN)
    if (url === `/campaigns/${CAMPAIGN_ID}/library`) {
      return library((JSON.parse(String(init?.body ?? '{}')) as { category: string }).category)
    }
    if (url.includes('/documents/doc_ondrey')) {
      return json({ ...DOCUMENT_FIXTURES.npc, document_id: 'doc_ondrey', campaign_id: CAMPAIGN_ID, data: NPC_FIELDS })
    }
    return json({ detail: `unrouted: ${url}` }, 404)
  })
}

const withNav = (documentId: string | null): Decorator => {
  const Wrapped: Decorator = (Story) => (
    <CampaignProvider restore={{ campaignId: CAMPAIGN_ID, conversationId: null, documentId }}>
      <CanvasProvider>
        {/* The nav column's width (LAYOUT-1): 268px. */}
        <div style={{ width: '268px', background: 'var(--md-sys-color-surface-container)' }}>
          <Story />
        </div>
      </CanvasProvider>
    </CampaignProvider>
  )
  return Wrapped
}

const meta = {
  title: 'Shell/CampaignDocumentsNav',
  component: CampaignDocumentsNav,
  parameters: { layout: 'centered' },
  decorators: [withShell({ mode: 'gm', role: 'dm' })],
} satisfies Meta<typeof CampaignDocumentsNav>

export default meta
type Story = StoryObj<typeof meta>

function state(library: Library, play: (canvas: ReturnType<typeof within>) => Promise<void>, documentId: string | null = null): Story {
  return {
    decorators: [withNav(documentId)],
    beforeEach: api(library),
    play: async ({ canvasElement }) => {
      const canvas = within(canvasElement)
      await expect(await canvas.findByRole('heading', { level: 2, name: 'Campaign documents' })).toBeVisible()
      await play(canvas)
    },
  }
}

const dark = (story: Story): Story => ({
  ...story,
  globals: { theme: 'dark' },
  play: async (context) => {
    await expectTheme('dark')
    await story.play?.(context)
  },
})

export const Loading = state(() => pending(), async (canvas) => {
  await expect(canvas.getByText('Loading documents…')).toBeVisible()
})
export const LoadingDark = dark(Loading)

export const Empty = state((category) => page(category, []), async (canvas) => {
  await expect(await canvas.findByText('No documents yet.')).toBeVisible()
})
export const EmptyDark = dark(Empty)

export const Failed = state(() => json({}, 503), async (canvas) => {
  await expect(await canvas.findByText("Couldn't load documents")).toBeVisible()
  await expectTouchTargets(canvas, ['Retry'])
})
export const FailedDark = dark(Failed)

export const PartlyFailed = state((category) => (category === 'bestiary' ? json({}, 503) : ready(category)), async (canvas) => {
  await expect(await canvas.findByText("Couldn't load documents")).toBeVisible()
  await expect(canvas.getByRole('button', { name: /Sister Ondrey Vashe/ })).toBeVisible()
  await expectTouchTargets(canvas, ['Retry', /Sister Ondrey Vashe/])
})

export const Ready = state(ready, async (canvas) => {
  await expect(await canvas.findByRole('button', { name: /Sister Ondrey Vashe/ })).toBeVisible()
  await expect(canvas.getAllByRole('listitem')).toHaveLength(5)
  await expectTouchTargets(canvas, [/Sister Ondrey Vashe/, /Tidewarden/, /Session 3/])
})
export const ReadyDark = dark(Ready)

/** The open document's row is marked current. */
export const WithCurrentDocument = state(
  ready,
  async (canvas) => {
    const row = await canvas.findByRole('button', { name: /Sister Ondrey Vashe/ })
    await waitFor(() => expect(row).toHaveAttribute('aria-current', 'true'))
    await expect(canvas.getByRole('button', { name: /Tidewarden/ })).not.toHaveAttribute('aria-current')
  },
  'doc_ondrey',
)

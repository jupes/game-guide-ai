/**
 * CanvasHost -- the Workbench's canvas column in every state, both themes, three
 * widths (agent-forge-harness-1kg.6.3, brief 2.6, T-9).
 *
 * The REAL campaign and canvas providers open the document from a deep link; only the
 * network is stubbed. Each story passes axe (`test: 'error'`) and measures the 44 px
 * floor of the controls it shows.
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, waitFor, within } from 'storybook/test'

import { json, pending, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { CampaignProvider } from '../shell/campaignContext'
import { CanvasProvider } from '../shell/canvasContext'
import { CanvasHost } from './CanvasHost'
import { DOCUMENT_FIXTURES } from './documentFixtures'

const CAMPAIGN_ID = 'cmp_StoryCampaign0000000001'
const TITLE = 'Sister Ondrey Vashe'

const CAMPAIGN = {
  schema_version: 1, campaign_id: CAMPAIGN_ID, name: 'The Drowned Crown', created_at: '2026-09-16T19:20:11Z',
  updated_at: '2026-09-16T19:31:24Z', archived_at: null, concluded_at: null, tone: null, game_system: 'dnd5e',
  avatar_icon: 'sailing', avatar_tone: 'ember', badge: null, seat_count: 0, last_activity_at: '2026-09-16T19:31:24Z',
  last_played_at: null, dormant: false,
}

/** No portrait: the asset route is not stubbed, and an image that cannot load is noise, not a finding. */
const NPC_FIELDS = Object.fromEntries(
  Object.entries(DOCUMENT_FIXTURES.npc.data as Record<string, unknown>).filter(([key]) => key !== 'portrait'),
)
const documentFor = (id: string, over: Record<string, unknown> = {}) => ({
  ...DOCUMENT_FIXTURES.npc, document_id: id, campaign_id: CAMPAIGN_ID, data: NPC_FIELDS, ...over,
})

const versions = (count: number) => ({
  schema_version: 1, document_id: 'doc_ok', next_cursor: null,
  items: Array.from({ length: count }, (_, at) => ({
    number: count - at, author: at % 2 === 0 ? 'assistant' : 'gm', summary: `Revision ${count - at}`,
    created_at: '2026-09-16T19:36:00Z', sealed: true, changed_fields: ['wants'], restored_from: null,
  })),
})

type Route = (url: string) => Response | Promise<Response>

/** Documents by id: `doc_ok` opens, `doc_missing` is a 404, `doc_newer` a newer schema, `doc_down` a 503, `doc_slow` never answers. */
function api(history: Route = () => json(versions(3))): Route {
  return (url) => {
    if (url === `/campaigns/${CAMPAIGN_ID}`) return json(CAMPAIGN)
    const doc = /\/documents\/(doc_\w+)$/.exec(url)
    if (doc !== null) {
      switch (doc[1]) {
        case 'doc_missing': return json({ code: 'not_found' }, 404)
        case 'doc_newer': return json(documentFor('doc_newer', { schema_version: 2 }))
        case 'doc_down': return json({}, 503)
        case 'doc_slow': return pending()
        default: return json(documentFor(doc[1]))
      }
    }
    if (url.includes('/versions')) return history(url)
    return json({ detail: `unrouted: ${url}` }, 404)
  }
}

const withCanvas = (documentId: string, width?: number): Decorator => {
  const Wrapped: Decorator = (Story) => (
    <CampaignProvider restore={{ campaignId: CAMPAIGN_ID, conversationId: null, documentId }}>
      <CanvasProvider>
        <div style={{ display: 'flex', height: '100dvh', width: width === undefined ? undefined : `${width}px` }}>
          <Story />
        </div>
      </CanvasProvider>
    </CampaignProvider>
  )
  return Wrapped
}

const meta = {
  title: 'GM/CanvasHost',
  component: CanvasHost,
  parameters: { layout: 'fullscreen' },
  decorators: [withShell({ mode: 'gm', role: 'dm' })],
} satisfies Meta<typeof CanvasHost>

export default meta
type Story = StoryObj<typeof meta>

function state(documentId: string, route: Route, play: (canvas: ReturnType<typeof within>) => Promise<void>, width?: number): Story {
  return {
    decorators: [withCanvas(documentId, width)],
    beforeEach: stubFetch(route),
    play: async ({ canvasElement }) => play(within(canvasElement)),
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

export const Loading = state('doc_slow', api(), async (canvas) => {
  await expect(await canvas.findByText('Opening the document…')).toBeVisible()
})

export const Open = state('doc_ok', api(), async (canvas) => {
  await expect(await canvas.findByRole('heading', { level: 2, name: TITLE })).toBeVisible()
  await expect(canvas.getByText('GM ONLY')).toBeVisible()
  await expect(canvas.queryAllByRole('button', { name: /^Edit / })).toEqual([])
  await expectTouchTargets(canvas, ['Close canvas'])
})
export const OpenDark = dark(Open)

export const HistoryOpen = state('doc_ok', api(), async (canvas) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'v3 — version history' }))
  await expect(await canvas.findByText('Revision 2')).toBeVisible()
  await expect(canvas.getByRole('region', { name: 'Version history' })).toBeVisible()
})

export const HistoryLoading = state('doc_ok', api(() => pending()), async (canvas) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'v3 — version history' }))
  await expect(canvas.getByRole('region', { name: 'Version history' })).toBeVisible()
  await expect(canvas.queryByText('Revision 2')).toBeNull()
})

export const HistoryFailed = state('doc_ok', api(() => json({}, 503)), async (canvas) => {
  await userEvent.click(await canvas.findByRole('button', { name: 'v3 — version history' }))
  await expect(await canvas.findByRole('button', { name: /retry/i })).toBeVisible()
})

export const Unavailable = state('doc_missing', api(), async (canvas) => {
  const heading = await canvas.findByRole('heading', { level: 2, name: "This document isn't available" })
  await expect(heading).toBeVisible()
  await expect(canvas.getByText('It may have been deleted, or you may not have access to it.')).toBeVisible()
  await expectTouchTargets(canvas, ['Close'])
})
export const UnavailableDark = dark(Unavailable)

export const Unsupported = state('doc_newer', api(), async (canvas) => {
  await expect(
    await canvas.findByRole('heading', { level: 2, name: 'This document was made by a newer version of Aetheril.' }),
  ).toBeVisible()
  await expectTouchTargets(canvas, ['Close'])
})

export const Failed = state('doc_down', api(), async (canvas) => {
  await expect(await canvas.findByRole('heading', { level: 2, name: "Couldn't open the document" })).toBeVisible()
  await expect(canvas.getByText("Aetheril can't reach its library right now. Nothing was lost.")).toBeVisible()
  await expectTouchTargets(canvas, ['Retry', 'Close'])
})
export const FailedDark = dark(Failed)

/** Below 560px of column width the header goes compact, whatever the viewport. */
export const Compact480 = state(
  'doc_ok',
  api(),
  async (canvas) => {
    const region = await canvas.findByRole('region', { name: TITLE })
    await waitFor(() => expect(region).toHaveAttribute('data-layout', 'compact'))
  },
  480,
)

/** At the narrow layout the canvas is a full-screen view with Back. */
export const FullScreenPhone: Story = {
  ...atViewport('phone375'),
  decorators: [withCanvas('doc_ok')],
  beforeEach: stubFetch(api()),
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    const canvas = within(canvasElement)
    const region = await canvas.findByRole('region', { name: TITLE })
    await expect(region).toHaveAttribute('data-layout', 'fullScreen')
    await expect(canvas.getByRole('button', { name: 'Back' })).toBeVisible()
    await expectNoPageOverflow()
    await expectTouchTargets(canvas, ['Back'])
  },
}

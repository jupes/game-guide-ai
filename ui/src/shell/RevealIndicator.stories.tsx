/**
 * RevealIndicator -- the workspace indicator (agent-forge-harness-1kg.7.3 PR-2, REVEAL-14) in the
 * real WorkspaceShell, in a real browser, in both themes.
 *
 * jsdom has no layout, so this is where its boxes are measured: it sits in the header row, it is
 * above the reveal sheet's scrim and can be pressed there, and on a phone it takes its own row
 * without a horizontal scroll. The REAL campaign provider restores the campaign; only the network
 * is stubbed. Every story passes axe (`test: 'error'`).
 */
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, waitFor, within } from 'storybook/test'

import { json, stubFetch, withShell } from '../../.storybook/shellHarness'
import { expectTouchTargets } from '../../.storybook/touchTarget'
import { atViewport, expectNoPageOverflow, expectTheme, expectViewport } from '../../.storybook/viewports'
import { DOCUMENT_FIXTURES } from '../gm/documentFixtures'
import { liveFixture, pictureFixture, seatFixture } from '../gm/revealFixtures'
import { CampaignProvider } from './campaignContext'
import { WorkspaceShell } from './WorkspaceShell'

const CAMPAIGN_ID = 'cmp_StoryCampaign0000000001'
const DOC_ID = 'doc_StoryOndrey000000000001'
const OTHER_ID = 'doc_StoryMira00000000000001'
const SEAT_ID = 'par_StoryBrann00000000001'

const CATALOG = { default: 'auto', models: [{ id: 'auto', display_name: 'Automatic' }] }

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
const documentOf = (id: string, name: string) => ({
  ...DOCUMENT_FIXTURES.npc, document_id: id, campaign_id: CAMPAIGN_ID, data: { ...NPC_FIELDS, name },
})

const ONE = pictureFixture({ table: liveFixture(DOC_ID, ['name', 'voice']) })
const TWO = pictureFixture({
  table: liveFixture(DOC_ID, ['name', 'voice']),
  participants: { [SEAT_ID]: liveFixture(OTHER_ID, ['name']) },
})
const STALE = pictureFixture({ table: liveFixture(DOC_ID, ['name'], { stale_text: true }) })

/** The network: `picture` is what the server says is live, or `null` for an outage on that read. */
function api(picture: unknown): () => () => void {
  return stubFetch((url) => {
    if (url.includes('/models')) return json(CATALOG)
    if (url === `/campaigns/${CAMPAIGN_ID}/reveals`) {
      return picture === null ? json({ detail: 'down' }, 503) : json({ schema_version: 1, state: picture })
    }
    if (url === `/campaigns/${CAMPAIGN_ID}/table-session`) return json({ schema_version: 1, session: null })
    if (url.startsWith(`/campaigns/${CAMPAIGN_ID}/participants`)) {
      return json({ schema_version: 1, items: [seatFixture(SEAT_ID, 'Brann')], next_cursor: null })
    }
    if (url === `/campaigns/${CAMPAIGN_ID}`) return json(CAMPAIGN)
    if (url === `/campaigns/${CAMPAIGN_ID}/documents/${DOC_ID}`) return json(documentOf(DOC_ID, 'Sister Ondrey Vashe'))
    if (url === `/campaigns/${CAMPAIGN_ID}/documents/${OTHER_ID}`) return json(documentOf(OTHER_ID, 'Mira of the Tides'))
    if (url.startsWith(`/campaigns/${CAMPAIGN_ID}/documents/${DOC_ID}/versions`)) {
      return json({ schema_version: 1, document_id: DOC_ID, next_cursor: null, items: [] })
    }
    return json({ detail: `unrouted: ${url}` }, 404)
  })
}

const withCampaign = (documentId: string | null): Decorator => {
  const Wrapped: Decorator = (Story) => (
    <CampaignProvider restore={{ campaignId: CAMPAIGN_ID, conversationId: null, documentId }}>
      <Story />
    </CampaignProvider>
  )
  return Wrapped
}

const meta = {
  title: 'Shell/RevealIndicator',
  component: WorkspaceShell,
  parameters: { layout: 'fullscreen' },
  beforeEach: api(ONE),
  decorators: [withCampaign(null), withShell({ mode: 'gm', role: 'dm' })],
} satisfies Meta<typeof WorkspaceShell>

export default meta
type Story = StoryObj<typeof meta>

const indicatorIn = async (canvasElement: HTMLElement): Promise<HTMLElement> =>
  within(canvasElement).findByRole('group', { name: 'What the table sees' })

/** In the GM channel: the summary, and Stop all as its own button. */
export const LiveInGm: Story = {
  play: async ({ canvasElement }) => {
    const group = await indicatorIn(canvasElement)
    await expect(within(group).getByRole('button', { name: /^Revealed · .* \(table\)$/ })).toBeVisible()
    await expect(within(group).getByRole('button', { name: 'Stop all (1)' })).toBeEnabled()
    await expect(canvasElement.querySelector('.app-header')).toContainElement(group)
    await expectTouchTargets(within(group), [/^Revealed/, /^Stop all/])
  },
}

/** In Sage the indicator follows the GM, as a Stop-only control. */
export const LiveInSage: Story = {
  decorators: [withCampaign(null), withShell({ mode: 'sage', role: 'dm' })],
  play: async ({ canvasElement }) => {
    const group = await indicatorIn(canvasElement)
    await expect(group).toHaveAttribute('data-mode', 'stop-only')
    await expect(within(group).getByRole('button', { name: 'Stop all (1)' })).toBeEnabled()
  },
}

/** Every projection, with its title and who sees it, and a Stop of its own. */
export const ListOpen: Story = {
  beforeEach: api(TWO),
  play: async ({ canvasElement }) => {
    const group = await indicatorIn(canvasElement)
    await waitFor(() => expect(within(group).getByRole('button', { name: /\+1 more/ })).toBeVisible())
    await userEvent.click(within(group).getByRole('button', { name: /^Revealed/ }))
    const list = within(group).getByRole('list', { name: 'Everything the table is shown' })
    await waitFor(() => expect(within(list).getByText('Mira of the Tides (Brann)')).toBeVisible())
    await expect(within(list).getByRole('button', { name: 'Stop showing Mira of the Tides' })).toBeEnabled()
    await expectTouchTargets(within(group), [/^Revealed/, /^Stop all/])
  },
}

export const ListOpenDark: Story = {
  ...ListOpen,
  globals: { theme: 'dark' },
  play: async (context) => {
    await ListOpen.play?.(context)
    await expectTheme('dark')
  },
}

export const LiveDark: Story = {
  globals: { theme: 'dark' },
  play: async ({ canvasElement }) => {
    await expectTheme('dark')
    await expect(await indicatorIn(canvasElement)).toBeVisible()
  },
}

/** REVEAL-8: a copy is behind the document's latest text. */
export const EarlierVersion: Story = {
  beforeEach: api(STALE),
  play: async ({ canvasElement }) => {
    const group = await indicatorIn(canvasElement)
    await expect(within(group).getByText('Table is seeing an earlier version')).toBeVisible()
    await expect(within(group).getByRole('button', { name: 'Update…' })).toBeEnabled()
  },
}

/** REVEAL-13: a picture that cannot be read is unknown, never "nothing revealed". */
export const Unknown: Story = {
  beforeEach: api(null),
  play: async ({ canvasElement }) => {
    const group = await indicatorIn(canvasElement)
    await expect(within(group).getByText('Reveal state unknown — reconnecting')).toBeVisible()
    await expect(within(group).getByRole('button', { name: 'Stop all' })).toBeEnabled()
  },
}

export const UnknownDark: Story = { ...Unknown, globals: { theme: 'dark' } }

/** On the medium layout the header has no room beside the settings, so it takes its own row. */
export const Medium: Story = {
  ...atViewport('medium768'),
  beforeEach: api(TWO),
  play: async ({ canvasElement }) => {
    await expectViewport('medium768')
    const group = await indicatorIn(canvasElement)
    const header = canvasElement.querySelector('.app-header')?.getBoundingClientRect()
    const box = group.getBoundingClientRect()
    await expect(box.right).toBeLessThanOrEqual(768)
    await expect(box.top).toBeGreaterThanOrEqual((header?.top ?? 0) + 52)
    await expectNoPageOverflow()
  },
}

/** At the wide breakpoint it sits in the header row beside the channels. */
export const Wide: Story = {
  ...atViewport('wide1024'),
  play: async ({ canvasElement }) => {
    await expectViewport('wide1024')
    const group = await indicatorIn(canvasElement)
    const header = canvasElement.querySelector('.app-header')?.getBoundingClientRect()
    const box = group.getBoundingClientRect()
    await expect(box.bottom).toBeLessThanOrEqual((header?.bottom ?? 0) + 1)
    await expectNoPageOverflow()
  },
}

/** On a phone it takes its own full-width row under the channels, with no horizontal scroll. */
export const Phone: Story = {
  ...atViewport('phone375'),
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    const group = await indicatorIn(canvasElement)
    const box = group.getBoundingClientRect()
    await expect(box.left).toBeGreaterThanOrEqual(0)
    await expect(box.right).toBeLessThanOrEqual(375)
    await expectNoPageOverflow()
    await expectTouchTargets(within(group), [/^Revealed/, /^Stop all/])
  },
}

export const PhoneDark: Story = {
  ...atViewport('phone375', 'dark'),
  beforeEach: api(TWO),
  play: async ({ canvasElement }) => {
    await expectViewport('phone375')
    await expectTheme('dark')
    const group = await indicatorIn(canvasElement)
    await userEvent.click(within(group).getByRole('button', { name: /^Revealed/ }))
    await expect(within(group).getByRole('list', { name: 'Everything the table is shown' })).toBeVisible()
    await expectNoPageOverflow()
  },
}

/** REVEAL-14: above any modal. With the reveal sheet open, the indicator is what is under the pointer over its own box. */
export const AboveTheRevealSheet: Story = {
  decorators: [withCampaign(DOC_ID), withShell({ mode: 'gm', role: 'dm' })],
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    await userEvent.click(await canvas.findByRole('button', { name: 'Change what the table sees' }))
    await canvas.findByRole('dialog', { name: /^Reveal / })
    const group = await indicatorIn(canvasElement)
    const stop = within(group).getByRole('button', { name: 'Stop all (1)' })
    const at = stop.getBoundingClientRect()
    const top = document.elementFromPoint(at.left + at.width / 2, at.top + at.height / 2)
    await expect(top === stop || stop.contains(top)).toBe(true)
    await expect(group.closest('[inert]')).toBeNull()
  },
}
